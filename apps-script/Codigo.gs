/**
 * Planilha de leads da corretora · LOTUS (LUZIAC)
 *
 * O painel (aba "Leads da corretora") lê e grava os leads nesta planilha por meio
 * deste script, publicado como "App da Web".
 *
 *   GET  ?action=list&pin=CODIGO            → { ok, leads: [...] }
 *   POST { action:'add', pin, lead:{...} }  → { ok, id }
 *
 * O código de acesso fica nas Propriedades do script (chave PIN), nunca no painel
 * nem no GitHub. Para trocar o código, é só mudar a propriedade: não precisa publicar de novo.
 */

const ABA = 'Leads';
const CABECALHO = ['ID', 'Data', 'Nome', 'Telefone', 'E-mail', 'Temperatura', 'Feedback', 'Quem atendeu'];

function aba_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(ABA);
  if (!sh) sh = ss.insertSheet(ABA);
  if (sh.getLastRow() === 0) {
    sh.appendRow(CABECALHO);
    sh.setFrozenRows(1);
    sh.getRange(1, 1, 1, CABECALHO.length).setFontWeight('bold');
  }
  return sh;
}

function resposta_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function codigoOk_(pin) {
  const certo = PropertiesService.getScriptProperties().getProperty('PIN');
  return !!certo && String(pin || '').trim() === String(certo).trim();
}

/* Texto que começa com = + - @ vira fórmula no Sheets; o apóstrofo força texto. */
function texto_(v, max) {
  const s = String(v == null ? '' : v).trim().slice(0, max || 300);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function doGet(e) {
  const p = (e && e.parameter) || {};
  if (!codigoOk_(p.pin)) return resposta_({ ok: false, error: 'pin' });
  const linhas = aba_().getDataRange().getValues();
  linhas.shift(); // cabeçalho
  const leads = linhas.filter(r => r[0] !== '' && r[2] !== '').map(r => ({
    id: String(r[0]),
    data: r[1] instanceof Date ? r[1].toISOString() : String(r[1]),
    nome: String(r[2]),
    telefone: String(r[3]),
    email: String(r[4]),
    temperatura: String(r[5]),
    feedback: String(r[6]),
    corretora: String(r[7]),
  }));
  return resposta_({ ok: true, leads: leads });
}

function doPost(e) {
  let corpo;
  try {
    corpo = JSON.parse(e.postData.contents);
  } catch (err) {
    return resposta_({ ok: false, error: 'formato' });
  }
  if (!codigoOk_(corpo.pin)) return resposta_({ ok: false, error: 'pin' });
  if (corpo.action !== 'add') return resposta_({ ok: false, error: 'acao' });

  const l = corpo.lead || {};
  if (!String(l.nome || '').trim() || (!String(l.telefone || '').trim() && !String(l.email || '').trim())) {
    return resposta_({ ok: false, error: 'campos' });
  }
  const trava = LockService.getScriptLock();
  trava.waitLock(10000);
  try {
    const id = Utilities.getUuid().slice(0, 8);
    aba_().appendRow([
      id, new Date(), texto_(l.nome, 120), texto_(l.telefone, 40), texto_(l.email, 160),
      texto_(l.temperatura, 20), texto_(l.feedback, 1000), texto_(l.corretora, 80),
    ]);
    return resposta_({ ok: true, id: id });
  } finally {
    trava.releaseLock();
  }
}
