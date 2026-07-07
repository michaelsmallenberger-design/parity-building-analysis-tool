/**
 * Parity — Google Sheet writer for the cooling-tower review loop.
 *
 * A standalone Apps Script web app (deployed once by a team member; runs as them,
 * under your Workspace — NO service-account key, NO n8n). The Render app calls it
 * server-side for two actions:
 *   • create → make a NEW sheet for a batch, write the analysis rows, share it
 *              with the team, return the sheet URL.
 *   • update → when a reviewer submits, write HVAC Systems + Fit + Note into the row.
 *
 * Setup (see apps_script/README.md):
 *   1. Extensions ▸ Apps Script in any sheet, or script.google.com ▸ New project.
 *   2. Paste this file.
 *   3. Project Settings ▸ Script Properties — add:
 *        SHARED_TOKEN = a long random string (must match Render SHEET_WEBHOOK_TOKEN)
 *        TEAM_EMAILS  = comma-separated emails to auto-share each new sheet with
 *        FOLDER_ID    = (optional) a Drive folder ID to drop new sheets into
 *   4. Deploy ▸ New deployment ▸ Web app ▸ Execute as: Me, Access: Anyone with the link.
 *   5. Copy the /exec URL → give it to Render as SHEET_WEBHOOK_URL.
 */

var SHEET_NAME = 'Analysis';
var HEADERS = ['Row_ID', 'Address', 'AI Verdict', 'AI Confidence', 'AI Reasoning',
               'HVAC Systems', 'Fit', 'Note', 'Reviewed At'];
// Match the Washington Gas sheet dropdowns.
var HVAC_OPTIONS = ['Cooling Tower', 'Chiller', 'Exhaust Fan', 'RTU', 'AHU', 'PTAC', 'Fan Coil', 'Heat Pump', 'VRF'];
var FIT_OPTIONS = ['Optimizer', 'Periscope', 'Unclear', 'Bad'];

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);
    if (body.token !== _prop('SHARED_TOKEN')) return _json({ error: 'unauthorized' });
    if (body.action === 'create') return _create(body);
    if (body.action === 'update') return _update(body);
    return _json({ error: 'unknown action: ' + body.action });
  } catch (err) {
    return _json({ error: String(err) });
  }
}

function _create(body) {
  var title = body.title || ('Cooling Tower Analysis ' + _today());
  var ss = SpreadsheetApp.create(title);
  var sh = ss.getActiveSheet();
  sh.setName(SHEET_NAME);

  var rows = [HEADERS];
  (body.rows || []).forEach(function (r) {
    rows.push([r.row_id, r.address, r.ai_verdict, r.ai_confidence, r.ai_reasoning, '', '', '', '']);
  });
  sh.getRange(1, 1, rows.length, HEADERS.length).setValues(rows);

  var lastRow = Math.max(rows.length, 300); // format + dropdowns cover spare rows too

  // Header styling + frozen panes
  sh.getRange(1, 1, 1, HEADERS.length)
    .setFontWeight('bold').setFontColor('#ffffff').setBackground('#0f5132').setVerticalAlignment('middle');
  sh.setFrozenRows(1);
  sh.setFrozenColumns(2);
  sh.setRowHeight(1, 30);

  // Column widths + wrapping + confidence as %
  [70, 260, 130, 95, 380, 210, 115, 220, 150].forEach(function (w, i) { sh.setColumnWidth(i + 1, w); });
  sh.getRange(2, 5, lastRow, 1).setWrap(true);            // AI Reasoning
  sh.getRange(2, 8, lastRow, 1).setWrap(true);            // Note
  sh.getRange(2, 4, lastRow, 1).setNumberFormat('0%');    // AI Confidence

  // Dropdowns — HVAC (multi-value, so warn-not-reject) and Fit (single, strict)
  var cH = HEADERS.indexOf('HVAC Systems') + 1;
  var cF = HEADERS.indexOf('Fit') + 1;
  sh.getRange(2, cH, lastRow, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(HVAC_OPTIONS, true).setAllowInvalid(true)
      .setHelpText('Pick from: ' + HVAC_OPTIONS.join(', ') + ' (multiple allowed, comma-separated)').build());
  sh.getRange(2, cF, lastRow, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(FIT_OPTIONS, true).setAllowInvalid(false).build());

  // Alternating row banding on the data rows (keep the green header)
  try {
    sh.getRange(2, 1, lastRow - 1, HEADERS.length).applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false);
  } catch (e) {}

  var folderId = _prop('FOLDER_ID');
  if (folderId) {
    try { DriveApp.getFileById(ss.getId()).moveTo(DriveApp.getFolderById(folderId)); } catch (e) {}
  }
  _prop('TEAM_EMAILS').split(',').map(function (s) { return s.trim(); }).filter(String)
    .forEach(function (em) { try { ss.addEditor(em); } catch (e) {} });

  return _json({ ok: true, sheet_url: ss.getUrl(), sheet_id: ss.getId() });
}

function _update(body) {
  if (!body.sheet_url) return _json({ error: 'no sheet_url for update' });
  var ss = SpreadsheetApp.openByUrl(body.sheet_url);
  var sh = ss.getSheetByName(SHEET_NAME) || ss.getActiveSheet();
  var data = sh.getDataRange().getValues();
  var head = data[0];
  var cId = head.indexOf('Row_ID'), cH = head.indexOf('HVAC Systems'),
      cF = head.indexOf('Fit'), cN = head.indexOf('Note'), cR = head.indexOf('Reviewed At');
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][cId]) === String(body.row_id)) {
      if (cH >= 0) sh.getRange(i + 1, cH + 1).setValue(body.hvac_systems || '');
      if (cF >= 0) sh.getRange(i + 1, cF + 1).setValue(body.fit || '');
      if (cN >= 0) sh.getRange(i + 1, cN + 1).setValue(body.note || '');
      if (cR >= 0) sh.getRange(i + 1, cR + 1).setValue(new Date());
      return _json({ ok: true, row: i + 1 });
    }
  }
  return _json({ error: 'row not found', row_id: body.row_id });
}

function _prop(k) { return PropertiesService.getScriptProperties().getProperty(k) || ''; }
function _json(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
function _today() {
  return Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd');
}
