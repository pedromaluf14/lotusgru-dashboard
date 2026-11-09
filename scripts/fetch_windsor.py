"""Busca os dados de Meta Ads (conta Lotus Condominio) e Google Ads (conta Condomínio Lotus)
no Windsor.ai e grava em metrics.json.

LOTUS: no Meta o painel compara CONJUNTOS (públicos), não campanhas. Por isso, em
todo lugar onde a Diálogo usa "campanha", aqui a chave é o nome do conjunto
(adset_name). O formato do JSON é o mesmo, só muda o que vai na chave.
O Google Ads vem por campanha, na chave "google" (ver abaixo).

Roda no GitHub Actions (ver .github/workflows/atualizar-dados.yml), de hora em hora.
A chave da API vem do secret WINDSOR_API_KEY — nunca coloque a chave no código.

Formato de saída (lido pelo index.html):
{
  "updatedAt": "2026-11-04T14:07:00Z",      # UTC
  "today": "2026-11-04",                     # data de hoje em São Paulo
  "account": {"name", "spendCap", "amountSpent", "balance"},   # em reais
  "daily": {"<campanha>": {"AAAA-MM-DD": [spend, impr, clicks, leads], ...}},
  "reach": {"last7"|"prev7"|"month": {"account": n, "campaigns": {"<campanha>": n}}},
  "breakdown": {"last7"|"month": {"gender", "age", "platform", "device"}},  # leads, conta inteira
  "budgets": {"<conjunto>": {"type": "ABO", "daily": reais, "adsets": 1}},
  "topAds": [{"campaign", "name", "leads", "spend", "link", "thumb"}],  # últimos 7 dias, conta inteira
  "adsByCampaign": {"last7"|"month": {"<campanha>": [mesmo formato de topAds]}},  # melhores de cada campanha
  "google": {"account", "conversionActions": {"<ação>": n},   # ações de conversão dos últimos 30 dias
             "daily": {"<campanha>": {"AAAA-MM-DD": [spend, impr, clicks, conversões]}},
             "status": {"<campanha>": "ENABLED"|"PAUSED"|...}}
}
"""
import glob
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ACCOUNT_ID = "1130066838541637"  # Lotus Condominio (Meta Ads)
GOOGLE_ACCOUNT_ID = "529-222-3794"  # Condomínio Lotus (Google Ads)
GROUP = "adset_name"            # chave do painel = conjunto de anúncios
# Lead = "Leads (formulário)" do Gerenciador de Anúncios. NÃO usar actions_lead
# (soma os leads do pixel do site e não bate com o Gerenciador).
LEAD_FIELD = "actions_onsite_conversion_lead_grouped"
START_DATE = "2026-01-01"       # primeiro dia da série diária (período personalizado)
TOP_ADS = 6
TOP_ADS_PER_CAMPAIGN = 6   # melhores anúncios de cada conjunto
TZ = timezone(timedelta(hours=-3))  # America/Sao_Paulo
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "metrics.json")
OUT_ENC = os.path.join(ROOT, "metrics.enc.json")
PBKDF2_ITER = 250_000


def encrypt_metrics(raw: bytes, password: str) -> dict:
    """Criptografa os dados com a senha de gestor (AES-256-GCM, chave por PBKDF2-SHA256).

    O navegador refaz a mesma derivação com a senha digitada e abre os dados (WebCrypto).
    Sem a senha, o arquivo publicado é ilegível."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, raw, None)  # texto cifrado + tag, no formato que o WebCrypto espera
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITER, "salt": b64(salt), "iv": b64(iv), "data": b64(ct)}


def previous_metrics() -> dict:
    """Último dado publicado (aberto ou criptografado), para manter o Google se ele falhar."""
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    password = os.environ.get("PAINEL_SENHA", "").strip()
    if password and os.path.exists(OUT_ENC):
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        with open(OUT_ENC, encoding="utf-8") as f:
            enc = json.load(f)
        d = base64.b64decode
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=d(enc["salt"]), iterations=enc["iter"]).derive(password.encode("utf-8"))
        return json.loads(AESGCM(key).decrypt(d(enc["iv"]), d(enc["data"]), None))
    return {}
ADS_DIR = os.path.join(ROOT, "assets", "ads")


class WindsorError(Exception):
    pass


def windsor(api_key, fields, date_from, date_to, connector="facebook", account=ACCOUNT_ID):
    params = urllib.parse.urlencode({
        "api_key": api_key,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": account,
    })
    url = f"https://connectors.windsor.ai/{connector}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=300) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise WindsorError(f"HTTP {e.code}: {body}")
    except Exception as e:  # rede, timeout, JSON inválido
        raise WindsorError(f"{type(e).__name__}: {e}"[:800])
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise WindsorError(f"resposta inesperada: {str(payload)[:500]}")
    return rows


def n(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def save_thumb(url, key):
    """Baixa a miniatura do anúncio para assets/ads (o link do Meta expira em poucos dias)."""
    if not url:
        return None
    name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    existing = glob.glob(os.path.join(ADS_DIR, name + ".*"))
    if existing:
        return "assets/ads/" + os.path.basename(existing[0])
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (painel)"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            data = resp.read()
    except Exception as e:
        print(f"::warning::miniatura não baixada ({key}): {type(e).__name__}")
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif",
           "image/gif": "gif"}.get(ctype)
    if not ext:
        path = urllib.parse.urlparse(url).path.lower()
        ext = next((e for e in ("jpg", "jpeg", "png", "webp", "avif", "gif") if path.endswith("." + e)), "jpg")
    os.makedirs(ADS_DIR, exist_ok=True)
    fn = f"{name}.{ext}"
    with open(os.path.join(ADS_DIR, fn), "wb") as f:
        f.write(data)
    return "assets/ads/" + fn


def main() -> int:
    api_key = (os.environ.get("WINDSOR_API_KEY") or "").strip()
    if not api_key:
        print("::error::WINDSOR_API_KEY não definido")
        return 1

    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    # "Últimos 7 dias" igual ao Meta: 7 dias completos, de ontem para trás (sem hoje)
    last7 = ((today - timedelta(days=7)).isoformat(), yesterday.isoformat())
    prev7 = ((today - timedelta(days=14)).isoformat(), (today - timedelta(days=8)).isoformat())
    # "Este mês": do dia 1 até ontem (no dia 1, mostra o mês anterior inteiro)
    month = (yesterday.replace(day=1).isoformat(), yesterday.isoformat())
    today_s = today.isoformat()
    recent_from = (today - timedelta(days=2)).isoformat()

    try:
        daily_rows = windsor(api_key,
            ["date", GROUP, "spend", "impressions", "link_clicks", LEAD_FIELD],
            START_DATE, today_s)

        # Alcance não pode ser somado dia a dia: busca o consolidado do Meta por período.
        reach = {}
        for key, (frm, to) in (("last7", last7), ("prev7", prev7), ("month", month)):
            acc_rows = windsor(api_key, ["account_name", "reach"], frm, to)
            camp_rows = windsor(api_key, [GROUP, "reach"], frm, to)
            reach[key] = {
                "account": int(sum(n(r.get("reach")) for r in acc_rows)),
                "campaigns": {r[GROUP]: int(n(r.get("reach"))) for r in camp_rows if r.get(GROUP)},
            }

        breakdown = {}
        for key, (frm, to) in (("last7", last7), ("month", month)):
            rows = windsor(api_key, ["gender", "age", LEAD_FIELD], frm, to)
            g, a = defaultdict(int), defaultdict(int)
            for r in rows:
                leads = int(n(r.get(LEAD_FIELD)))
                g[r.get("gender") or "unknown"] += leads
                a[r.get("age") or "unknown"] += leads
            rows = windsor(api_key, ["publisher_platform", "device_platform", LEAD_FIELD], frm, to)
            pl, dv = defaultdict(int), defaultdict(int)
            for r in rows:
                leads = int(n(r.get(LEAD_FIELD)))
                pl[r.get("publisher_platform") or "unknown"] += leads
                dv[r.get("device_platform") or "unknown"] += leads
            breakdown[key] = {"gender": dict(g), "age": dict(a), "platform": dict(pl), "device": dict(dv)}


        # Orçamento diário atual de cada conjunto (campanha ABO). Valores do Meta vêm em centavos.
        bud_rows = windsor(api_key, ["campaign_effective_status", GROUP, "adset_id",
                                     "adset_effective_status", "adset_daily_budget"],
                           recent_from, today_s)

        acc_rows = windsor(api_key, ["account_name", "spend_cap", "amount_spent", "account_balance"],
                           recent_from, today_s)

        ad_fields = [GROUP, "ad_id", "ad_name", "thumbnail_url",
                     "ad_preview_shareable_link", "instagram_permalink_url", "spend", LEAD_FIELD]
        ad_rows = {"last7": windsor(api_key, ad_fields, last7[0], last7[1]),
                   "month": windsor(api_key, ad_fields, month[0], month[1])}
    except WindsorError as e:
        print(f"::error::ERRO Windsor {e}")
        return 1

    if not daily_rows:
        # Não sobrescreve dados bons com um retorno vazio (falha temporária da API).
        print("Windsor retornou 0 linhas; mantendo metrics.json anterior")
        return 0

    # ---- série diária por campanha: [investimento, impressões, cliques no link, leads] ----
    daily = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0, 0]))
    for r in daily_rows:
        c, d = r.get(GROUP), r.get("date")
        if not c or not d:
            continue
        x = daily[c][d]
        x[0] += n(r.get("spend"))
        x[1] += int(n(r.get("impressions")))
        x[2] += int(n(r.get("link_clicks")))
        x[3] += int(n(r.get(LEAD_FIELD)))
    daily_out = {}
    for c, days in daily.items():
        kept = {d: [round(v[0], 2), v[1], v[2], v[3]] for d, v in sorted(days.items()) if any(v)}
        if kept:
            daily_out[c] = kept

    # ---- orçamentos ----
    budgets = {}
    for r in bud_rows:
        c = r.get(GROUP)
        if (not c or r.get("campaign_effective_status") != "ACTIVE"
                or r.get("adset_effective_status") != "ACTIVE" or r.get("adset_daily_budget") in (None, "")):
            continue
        budgets[c] = {"type": "ABO", "daily": round(n(r.get("adset_daily_budget")) / 100, 2), "adsets": 1}

    # ---- conta: limite de gastos, valor gasto e gasto ainda não cobrado (centavos) ----
    acc = acc_rows[0] if acc_rows else {}
    account = {
        "name": acc.get("account_name") or "Lotus Condominio",
        "spendCap": round(n(acc.get("spend_cap")) / 100, 2),
        "amountSpent": round(n(acc.get("amount_spent")) / 100, 2),
        "balance": round(n(acc.get("account_balance")) / 100, 2),
    }

    # ---- anúncios que mais trouxeram leads (mesmo anúncio em vários conjuntos = somado) ----
    # by_set=False: conta inteira, o mesmo anúncio em conjuntos diferentes vira um só (somado)
    def group_ads(rows, by_set=True):
        ads = {}
        for r in rows:
            c, name = r.get(GROUP), r.get("ad_name")
            if not c or not name:
                continue
            k = (c, name) if by_set else name
            a = ads.setdefault(k, {"campaign": c if by_set else "", "name": name, "leads": 0, "spend": 0.0,
                                           "_best": -1, "link": None, "ig": None, "_thumb": None, "_id": None})
            a.setdefault("_sets", set()).add(c)
            if not by_set:   # anúncio somado entre conjuntos: mostra o conjunto, ou "N conjuntos"
                a["campaign"] = c if len(a["_sets"]) == 1 else f"{len(a['_sets'])} conjuntos"
            sp, ld = n(r.get("spend")), int(n(r.get(LEAD_FIELD)))
            a["leads"] += ld
            a["spend"] += sp
            if sp > a["_best"]:
                a["_best"] = sp
                a["link"] = r.get("ad_preview_shareable_link") or a["link"]
                a["ig"] = r.get("instagram_permalink_url") or a["ig"]  # post do Instagram: o painel toca o vídeo
                a["_thumb"] = r.get("thumbnail_url") or a["_thumb"]
                a["_id"] = r.get("ad_id")
        return [a for a in ads.values() if a["leads"] > 0]

    keep_files = set()

    def publish(a):
        thumb = save_thumb(a["_thumb"], a["_id"] or (a["campaign"] + a["name"]))
        if thumb:
            keep_files.add(os.path.basename(thumb))
        return {"campaign": a["campaign"], "name": a["name"], "leads": a["leads"],
                "spend": round(a["spend"], 2), "link": a["link"], "ig": a["ig"], "thumb": thumb}

    rank = lambda xs: sorted(xs, key=lambda a: (-a["leads"], a["spend"]))
    top_ads = [publish(a) for a in rank(group_ads(ad_rows["last7"], by_set=False))[:TOP_ADS]]
    ads_by_campaign = {}
    for key, rows in ad_rows.items():
        per = defaultdict(list)
        for a in group_ads(rows):
            per[a["campaign"]].append(a)
        ads_by_campaign[key] = {c: [publish(a) for a in rank(xs)[:TOP_ADS_PER_CAMPAIGN]]
                                for c, xs in per.items()}
    # apaga miniaturas que saíram dos rankings
    for f in glob.glob(os.path.join(ADS_DIR, "*")):
        if os.path.basename(f) not in keep_files:
            os.remove(f)

    # ---- Google Ads (por campanha). Se falhar, mantém o último dado bom do Google. ----
    google = None
    try:
        g_rows = windsor(api_key, ["date", "campaign", "spend", "impressions", "clicks", "conversions"],
                         START_DATE, today_s, connector="google_ads", account=GOOGLE_ACCOUNT_ID)
        g_status = windsor(api_key, ["campaign", "campaign_status"], recent_from, today_s,
                           connector="google_ads", account=GOOGLE_ACCOUNT_ID)
        g_conv = windsor(api_key, ["conversion_action_name", "conversions"],
                         (today - timedelta(days=30)).isoformat(), today_s,
                         connector="google_ads", account=GOOGLE_ACCOUNT_ID)
        gd = defaultdict(lambda: defaultdict(lambda: [0.0, 0, 0, 0.0]))
        for r in g_rows:
            c, d = r.get("campaign"), r.get("date")
            if not c or not d:
                continue
            x = gd[c][d]
            x[0] += n(r.get("spend"))
            x[1] += int(n(r.get("impressions")))
            x[2] += int(n(r.get("clicks")))
            x[3] += n(r.get("conversions"))
        g_daily = {}
        for c, days in gd.items():
            kept = {d: [round(v[0], 2), v[1], v[2], round(v[3], 2)] for d, v in sorted(days.items()) if any(v)}
            if kept:
                g_daily[c] = kept
        conv = defaultdict(float)
        for r in g_conv:
            if r.get("conversion_action_name"):
                conv[r["conversion_action_name"]] += n(r.get("conversions"))
        google = {
            "account": "Condomínio Lotus",
            "daily": g_daily,
            "status": {r["campaign"]: r.get("campaign_status") for r in g_status if r.get("campaign")},
            "conversionActions": {k: round(v, 2) for k, v in conv.items() if v},
        }
    except WindsorError as e:
        print(f"::warning::Google Ads não atualizou ({e}); mantendo o último dado")
        try:
            google = previous_metrics().get("google")
        except Exception:
            google = None

    out = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "today": today_s,
        "account": account,
        "daily": daily_out,
        "reach": reach,
        "breakdown": breakdown,
        "budgets": budgets,
        "topAds": top_ads,
        "adsByCampaign": ads_by_campaign,
        "google": google,
    }
    raw = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    password = os.environ.get("PAINEL_SENHA", "").strip()
    if password:
        # Painel protegido: publica só a versão criptografada e apaga a aberta.
        with open(OUT_ENC, "w", encoding="utf-8") as f:
            json.dump(encrypt_metrics(raw, password), f, separators=(",", ":"))
        if os.path.exists(OUT):
            os.remove(OUT)
        dest = "metrics.enc.json (criptografado)"
    else:
        print("::warning::Secret PAINEL_SENHA não configurado: os dados ficam abertos em metrics.json")
        with open(OUT, "wb") as f:
            f.write(raw)
        if os.path.exists(OUT_ENC):
            os.remove(OUT_ENC)
        dest = "metrics.json"
    print(f"{len(daily_rows)} linhas diárias, {len(daily_out)} campanhas, {len(top_ads)} anúncios em {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
