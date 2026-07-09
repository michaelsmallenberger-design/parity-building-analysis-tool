"""Google Sheets writer (service account) for the review write-back loop.

Creates the batch's Google Sheet up front at run time and writes each reviewer
decision into its row as it is submitted, so the sheet is finished the moment
review is finished — no operator step.

Env:
    GOOGLE_SERVICE_ACCOUNT_JSON  full service-account key JSON (Render)
    GOOGLE_SERVICE_ACCOUNT_FILE  path to the key file (local dev alternative)
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


def create_batch_sheet(title, headers, rows, hvac_options, fit_options) -> str:
    """Create the output Sheet: user's table + bold frozen header, HVAC/Fit
    dropdowns, hidden Row_ID column. HVAC validation is warn-only because the
    review page submits comma-joined multi-selects. Returns the sheet URL."""
    sheets, drive = _get_services()
    sid = sheets.spreadsheets().create(
        body={"properties": {"title": title}}, fields="spreadsheetId"
    ).execute()["spreadsheetId"]
    values = [list(headers)] + [[str(c) for c in r] for r in rows]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid, range="A1", valueInputOption="RAW",
        body={"values": values}).execute()

    n = len(rows)
    reqs = [
        {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": 0,
                                                  "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]
    for col_name, options in ((HVAC_COL, hvac_options), (FIT_COL, fit_options)):
        if col_name in headers and n:
            c = headers.index(col_name)
            reqs.append({"setDataValidation": {
                "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": n + 1,
                          "startColumnIndex": c, "endColumnIndex": c + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": o} for o in options]},
                         "strict": False, "showCustomUi": True}}})
    if ID_COL in headers:
        c = headers.index(ID_COL)
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS",
                      "startIndex": c, "endIndex": c + 1},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()

    emails = [e.strip() for e in os.environ.get("SHEET_SHARE_WITH", "").split(",") if e.strip()]
    for email in emails:
        drive.permissions().create(fileId=sid, sendNotificationEmail=False,
                                   body={"type": "user", "role": "writer",
                                         "emailAddress": email}).execute()
    if not emails:
        log.warning("SHEET_SHARE_WITH unset — granting anyone-with-link writer access")
        drive.permissions().create(fileId=sid,
                                   body={"type": "anyone", "role": "writer"}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


def write_decision(sheet_url, headers, rows, row_id, hvac, fit, note) -> bool:
    """Write one reviewer decision into its sheet row (matched by the hidden
    Row_ID column). Notes only written when non-empty so an uploaded sheet's
    existing note text is never wiped. Returns False if the row can't be found."""
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
    data = []
    for col_name, value in ((HVAC_COL, hvac), (FIT_COL, fit), (NOTES_COL, note)):
        if col_name in headers and (value or col_name != NOTES_COL):
            data.append({"range": f"{_col_letter(headers.index(col_name))}{target}",
                         "values": [[value]]})
    if not data:
        return False
    sheets, _ = _get_services()
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sid, body={"valueInputOption": "RAW", "data": data}).execute()
    return True
