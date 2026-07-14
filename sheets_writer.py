"""Google Sheets writer (service account) for the review write-back loop.

Creates the batch's Google Sheet up front at run time and writes each reviewer
decision into its row as it is submitted, so the sheet is finished the moment
review is finished — no operator step.

Env:
    GOOGLE_SERVICE_ACCOUNT_JSON  full service-account key JSON (Render)
    GOOGLE_SERVICE_ACCOUNT_FILE  path to the key file (local dev alternative)
    SHEET_PARENT_FOLDER_ID       Drive folder (shared to the service account as
                                 Editor) where sheets are created. REQUIRED in
                                 practice: service accounts have zero Drive
                                 storage quota of their own, so without a human
                                 owner's folder, creation 403s. For consumer
                                 Gmail folders the folder owner owns the files.
    SHEET_SHARE_WITH             comma-separated emails granted writer access

If neither credential var is set, enabled() is False and callers skip Sheets
entirely (the operator/skill flow via GET /api/batch remains the fallback).
"""
import json
import logging
import os
import re
import threading

log = logging.getLogger("sheets")

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_lock = threading.Lock()
_services = None

HVAC_COL, FIT_COL, NOTES_COL, ID_COL = "HVAC Systems", "Fit", "Notes", "Row_ID"


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
                or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"))


def _get_services():
    """Build (sheets, drive) API clients once; thread-safe for concurrent Submits."""
    global _services
    with _lock:
        if _services is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
            if raw:
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(raw), scopes=_SCOPES)
            else:
                creds = service_account.Credentials.from_service_account_file(
                    os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=_SCOPES)
            _services = (
                build("sheets", "v4", credentials=creds, cache_discovery=False),
                build("drive", "v3", credentials=creds, cache_discovery=False),
            )
    return _services


def sheet_id_from_url(url: str) -> str:
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _col_letter(idx: int) -> str:
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def create_batch_sheet(title, headers, rows, hvac_options, fit_options,
                       review_url="") -> str:
    """Create the output Sheet: user's table + bold frozen header, HVAC/Fit
    dropdowns, hidden Row_ID column. HVAC validation is warn-only because the
    review page submits comma-joined multi-selects. When review_url is given,
    an "Open review page" link is written next to the header row so the sheet
    itself leads back to the review. Returns the sheet URL."""
    sheets, drive = _get_services()
    folder = os.environ.get("SHEET_PARENT_FOLDER_ID", "").strip()
    if folder:
        sid = drive.files().create(
            body={"name": title,
                  "mimeType": "application/vnd.google-apps.spreadsheet",
                  "parents": [folder]},
            fields="id", supportsAllDrives=True).execute()["id"]
    else:
        # Only works if the account has its own Drive quota (service accounts
        # generally don't anymore — set SHEET_PARENT_FOLDER_ID).
        sid = sheets.spreadsheets().create(
            body={"properties": {"title": title}}, fields="spreadsheetId"
        ).execute()["spreadsheetId"]
    grid = sheets.spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties.sheetId"
    ).execute()["sheets"][0]["properties"]["sheetId"]
    values = [list(headers)] + [[str(c) for c in r] for r in rows]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range="A1", valueInputOption="RAW",
        body={"values": values}).execute()
    if review_url:
        link_col = _col_letter(len(headers))  # first column past the table (0-based)
        sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=f"{link_col}1", valueInputOption="USER_ENTERED",
            body={"values": [[f'=HYPERLINK("{review_url}", "▸ Open review page")']]}).execute()

    n = len(rows)
    # Multi-select dropdowns can't be created via the API (DataValidationRule has
    # no such field — UI-only), but the setting SURVIVES a copy. When
    # SHEET_TEMPLATE_ID names a spreadsheet whose first tab holds a multi-select
    # HVAC validation cell (A2, checkbox ticked once by hand), copy that
    # validation onto the HVAC column instead of building a single-select rule.
    hvac_from_template = False
    template_id = os.environ.get("SHEET_TEMPLATE_ID", "").strip()
    if template_id and HVAC_COL in headers and n:
        try:
            tgrid = sheets.spreadsheets().get(
                spreadsheetId=template_id, fields="sheets.properties.sheetId"
            ).execute()["sheets"][0]["properties"]["sheetId"]
            copied = sheets.spreadsheets().sheets().copyTo(
                spreadsheetId=template_id, sheetId=tgrid,
                body={"destinationSpreadsheetId": sid}).execute()["sheetId"]
            c = headers.index(HVAC_COL)
            sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
                {"copyPaste": {
                    "source": {"sheetId": copied, "startRowIndex": 1, "endRowIndex": 2,
                               "startColumnIndex": 0, "endColumnIndex": 1},
                    "destination": {"sheetId": grid, "startRowIndex": 1, "endRowIndex": n + 1,
                                    "startColumnIndex": c, "endColumnIndex": c + 1},
                    "pasteType": "PASTE_DATA_VALIDATION"}},
                {"deleteSheet": {"sheetId": copied}},
            ]}).execute()
            hvac_from_template = True
        except Exception as e:
            log.warning("Template validation copy failed (%s); falling back to "
                        "single-select HVAC dropdown", e)
    reqs = [
        {"repeatCell": {"range": {"sheetId": grid, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": grid,
                                                  "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]
    for col_name, options in ((HVAC_COL, hvac_options), (FIT_COL, fit_options)):
        if col_name in headers and n:
            if col_name == HVAC_COL and hvac_from_template:
                continue
            c = headers.index(col_name)
            reqs.append({"setDataValidation": {
                "range": {"sheetId": grid, "startRowIndex": 1, "endRowIndex": n + 1,
                          "startColumnIndex": c, "endColumnIndex": c + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": o} for o in options]},
                         "strict": False, "showCustomUi": True}}})
    if ID_COL in headers:
        c = headers.index(ID_COL)
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": grid, "dimension": "COLUMNS",
                      "startIndex": c, "endIndex": c + 1},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()

    emails = [e.strip() for e in os.environ.get("SHEET_SHARE_WITH", "").split(",") if e.strip()]
    for email in emails:
        try:
            drive.permissions().create(fileId=sid, sendNotificationEmail=False,
                                       supportsAllDrives=True,
                                       body={"type": "user", "role": "writer",
                                             "emailAddress": email}).execute()
        except Exception as e:  # e.g. email is already the folder owner
            log.warning("Could not share sheet with %s: %s", email, e)
    if not emails and not folder:
        log.warning("SHEET_SHARE_WITH unset — granting anyone-with-link writer access")
        drive.permissions().create(fileId=sid, supportsAllDrives=True,
                                   body={"type": "anyone", "role": "writer"}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


def write_row_values(sheet_url, headers, rows, row_id, mapping) -> bool:
    """Write arbitrary {column header: value} cells into the sheet row matched by
    the hidden Row_ID column. Returns False if the sheet/row/columns can't be
    found. Unknown headers in mapping are skipped."""
    sid = sheet_id_from_url(sheet_url)
    if not sid or ID_COL not in headers:
        return False
    id_col = headers.index(ID_COL)
    target = None
    for i, r in enumerate(rows):
        if id_col < len(r) and str(r[id_col]).strip() == str(row_id).strip():
            target = i + 2  # +1 for the header row, +1 for 1-based sheet rows
            break
    if target is None:
        return False
    data = [{"range": f"{_col_letter(headers.index(k))}{target}", "values": [[v]]}
            for k, v in mapping.items() if k in headers]
    if not data:
        return False
    sheets, _ = _get_services()
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={"valueInputOption": "RAW", "data": data}).execute()
    return True


def write_decision(sheet_url, headers, rows, row_id, hvac, fit, note) -> bool:
    """Write one reviewer decision into its sheet row. Notes only written when
    non-empty so an uploaded sheet's existing note text is never wiped."""
    mapping = {HVAC_COL: hvac, FIT_COL: fit}
    if note:
        mapping[NOTES_COL] = note
    return write_row_values(sheet_url, headers, rows, row_id, mapping)
