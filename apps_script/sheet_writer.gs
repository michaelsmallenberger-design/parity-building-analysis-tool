/**
 * Parity — Google Sheet writer for the cooling-tower review loop.
 *
 * Standalone Apps Script web app (deployed once by a team member; runs as them,
 * no service-account key, no n8n). The Render app calls it server-side:
 *   • create → make a NEW sheet that is a COPY of the uploaded building sheet
 *              (ALL the user's columns preserved), then add HVAC/Fit dropdowns.
 *              NO AI columns — the AI is only guidance on the review web page.
 *   • update → when a reviewer submits, write HVAC Systems + Fit (+ Notes) into
 *              that building's row (matched by the hidden Row_ID column).
 *
 * The tool guarantees the columns "HVAC Systems", "Fit", "Notes", and "Row_ID"
 * exist in the headers it sends (reusing the user's if already present), so this
 * script always finds them by name.
 *
 * Setup / redeploy: see apps_script/README.md. Script Properties needed:
 *   SHARED_TOKEN (must match Render SHEET_WEBHOOK_TOKEN), TEAM_EMAILS, FOLDER_ID?
 */

var SHEET_NAME = 'Analysis';
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
  var headers = body.headers || [];
  var rows = body.rows || [];
  var nCols = headers.length;

  var ss = SpreadsheetApp.create(title);
  var sh = ss.getActiveSheet();
  sh.setName(SHEET_NAME);

  var all = [headers].concat(rows);
  if (nCols) sh.getRange(1, 1, all.length, nCols).setValues(all);
  var lastRow = Math.max(all.length, 300);

  // Header styling + freeze
  sh.getRange(1, 1, 1, nCols).setFontWeight('bold').setFontColor('#ffffff').setBackground('#0f5132');
  sh.setFrozenRows(1);
  sh.setRowHeight(1, 30);

  // Dropdowns on HVAC Systems (multi, warn-not-reject) and Fit (single, strict)
  var cH = headers.indexOf('HVAC Systems') + 1;
  var cF = headers.indexOf('Fit') + 1;
  if (cH > 0) sh.getRange(2, cH, lastRow, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(HVAC_OPTIONS, true).setAllowInvalid(true)
      .setHelpText('Pick from: ' + HVAC_OPTIONS.join(', ') + ' (multiple allowed, comma-separated)').build());
  if (cF > 0) sh.getRange(2, cF, lastRow, 1).setDataValidation(
    SpreadsheetApp.newDataValidation().requireValueInList(FIT_OPTIONS, true).setAllowInvalid(false).build());

  // Readability: banding on data rows, sensible widths, hide the Row_ID key column
  try { sh.getRange(2, 1, lastRow - 1, nCols).applyRowBanding(SpreadsheetApp.BandingTheme.LIGHT_GREY, false, false); } catch (e) {}
  try { sh.autoResizeColumns(1, nCols); } catch (e) {}
  var cId = headers.indexOf('Row_ID') + 1;
  if (cId > 0) { try { sh.hideColumns(cId); } catch (e) {} }

  // Move to folder + share with the team
  var folderId = _prop('FOLDER_ID');
  if (folderId) { try { DriveApp.getFileById(ss.getId()).moveTo(DriveApp.getFolderById(folderId)); } catch (e) {} }
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
      cF = head.indexOf('Fit'), cN = head.indexOf('Notes');
  if (cId < 0) return _json({ error: 'no Row_ID column' });
  for (var i = 1; i < data.length; i++) {
    if (String(data[i][cId]) === String(body.row_id)) {
      if (cH >= 0) sh.getRange(i + 1, cH + 1).setValue(body.hvac_systems || '');
      if (cF >= 0) sh.getRange(i + 1, cF + 1).setValue(body.fit || '');
      if (cN >= 0 && body.note) sh.getRange(i + 1, cN + 1).setValue(body.note);
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
