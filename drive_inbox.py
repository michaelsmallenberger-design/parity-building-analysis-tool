"""Drive inbox watcher: the zero-click front door.

The team drops a building list into the Shared Drive folder named in
DRIVE_INBOX_FOLDER_ID; this daemon notices it, enqueues an analysis job, and the
run-in-place flow writes the review answers back into the sheet itself (a
review-page link appears in the sheet when analysis starts). Excel/CSV drops
are first converted to a Google Sheet in the same folder so they too can be
written back in place. No websites, no link-pasting, no per-sheet sharing —
the Shared Drive is already shared with the service account.

Processed file ids persist via storage_helpers so restarts/redeploys never
re-run (and re-bill) an old drop.
"""
import os
import csv
import uuid
import time
import logging
import tempfile
import threading

import sheets_writer
import intake_resolver
import workbook_runs
from job_queue import enqueue_job, check_usage_limit
from storage_helpers import upload_file, write_json, read_json

log = logging.getLogger("drive_inbox")

POLL_SECONDS = 60
_STATE_PATH = "drive_inbox/processed.json"
_PENDING_PATH = "drive_inbox/pending_mapping.json"
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_CONVERTIBLE = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
}


def _load_state():
    return read_json(_STATE_PATH) or {"ids": []}


def _mark(state, file_id):
    if file_id not in state["ids"]:
        state["ids"].append(file_id)
    write_json(_STATE_PATH, state)


class MappingNeedsSetup(ValueError):
    """A file was intentionally stopped before any paid address processing."""


def _record_pending(file_id, name, reason, resolution=None):
    """Persist only non-sensitive setup metadata for a stopped Drive source."""
    pending = read_json(_PENDING_PATH) or {"items": []}
    if not any(item.get("id") == file_id for item in pending["items"]):
        pending["items"].append({
            "id": file_id,
            "name": name,
            "reason": reason,
            "status": (resolution or {}).get("status", "preflight"),
            "run_id": (resolution or {}).get("run_id"),
            "schema_fingerprint": (resolution or {}).get("fingerprint"),
            "mapping": (resolution or {}).get("mapping"),
        })
        write_json(_PENDING_PATH, pending)


def pending_mappings():
    return (read_json(_PENDING_PATH) or {"items": []}).get("items", [])


def clear_pending_run(run_id):
    pending = read_json(_PENDING_PATH) or {"items": []}
    items = [
        item for item in pending.get("items", [])
        if str(item.get("run_id") or "") != str(run_id)
    ]
    if len(items) != len(pending.get("items", [])):
        write_json(_PENDING_PATH, {"items": items})


def enqueue_bound_sheet(sheet_url, source="drive-inbox"):
    """Read a live Google Sheet, dump its address tab to CSV, and enqueue the
    standard pipeline with a run-in-place binding. Returns (job_id, total).
    Raises on unreadable sheets / no address tab / usage limit."""
    from tasks_local import ADDRESS_VARIANTS
    inspected = sheets_writer.inspect_bound_sheet(sheet_url)
    workbook_resolution = intake_resolver.resolve_workbook_schema(
        inspected["tabs"], ADDRESS_VARIANTS,
    )
    selected_tabs = [
        tab for tab in workbook_resolution["tabs"] if tab.get("status") == "selected"
    ]
    if workbook_resolution["status"] != "ready" or len(selected_tabs) > 1:
        error = MappingNeedsSetup(
            "Workbook has multiple or exceptional address tabs; no tab was selected"
        )
        error.resolution = workbook_resolution
        raise error
    resolution = intake_resolver.resolve_schema(inspected["tabs"], ADDRESS_VARIANTS)
    if resolution["status"] == "deterministic":
        binding, _headers, _data_rows = sheets_writer.read_bound_sheet(sheet_url, ADDRESS_VARIANTS)
    elif resolution["status"] == "suggested" and resolution.get("auto_approved"):
        binding, _headers, _data_rows = sheets_writer.read_bound_sheet_mapping(
            sheet_url, resolution["mapping"], resolution["fingerprint"])
    else:
        error = MappingNeedsSetup(resolution.get("reason") or "address-column mapping needs setup")
        error.resolution = resolution
        raise error
    total = len(binding.get("processing_rows", []))
    if not total:
        raise ValueError(f"tab '{binding['tab']}' has no data rows")
    ok, current_usage, err = check_usage_limit(total)
    if not ok:
        raise RuntimeError(f"usage limit: {err} (current {current_usage})")
    job_id = str(uuid.uuid4())
    local = os.path.join(tempfile.gettempdir(), f"{job_id}.csv")
    with open(local, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(binding.get("processing_headers", binding["headers"]))
        w.writerows(binding.get("processing_rows", []))
    blob = f"uploads/{job_id}/{job_id}.csv"
    upload_file(local, blob)
    enqueue_job(job_id, {
        "job_id": job_id,
        "csv_path": blob,
        "total": total,
        "sheet_binding": binding,
    })
    log.info("[%s] enqueued %s rows from '%s' as job %s", source, total, binding["title"], job_id)
    return job_id, total


def _handle_file(f, state):
    fid, name, mime = f["id"], f.get("name", "?"), f.get("mimeType", "")
    if workbook_runs.surface_enabled("drive", default=True):
        if mime == "application/vnd.ms-excel":
            _record_pending(
                fid, name,
                "Save this legacy .xls workbook as .xlsx before analysis",
            )
            return
        if mime == _SHEET_MIME:
            run = workbook_runs.prepare_google_sheet(
                f"https://docs.google.com/spreadsheets/d/{fid}/edit",
                source_name=name, source_kind="drive_inbox",
            )
        elif mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            run = workbook_runs.prepare_drive_xlsx(fid, name)
            if run.get("sheet_id"):
                _mark(state, run["sheet_id"])
        elif mime == "text/csv":
            fd, local = tempfile.mkstemp(prefix="parity-drive-", suffix=".csv")
            os.close(fd)
            try:
                workbook_runs._download_drive_xlsx(fid, local)
                run = workbook_runs.prepare_local_csv(
                    local, name, source_kind="drive_inbox",
                )
            finally:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            if run.get("sheet_id"):
                _mark(state, run["sheet_id"])
        else:
            log.info("[drive-inbox] ignoring '%s' (%s)", name, mime)
            return
        if (
            run.get("confirmation_required")
            or run.get("preflight_failed")
            or run.get("status") == "approval_required"
        ):
            _record_pending(
                fid, name,
                run.get("error") or (
                    "Workbook approval is required"
                    if run.get("status") == "approval_required"
                    else "Workbook tab confirmation is required"
                ),
                workbook_runs.public_summary(run),
            )
        log.info(
            "[drive-inbox] workbook '%s' is %s as run %s",
            name, run.get("status"), run.get("run_id"),
        )
        return
    if mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }:
        _record_pending(
            fid, name,
            "Workbook conversion is waiting for the protected multi-tab engine",
        )
        return
    if mime == _SHEET_MIME:
        try:
            enqueue_bound_sheet(f"https://docs.google.com/spreadsheets/d/{fid}/edit",
                                source=f"drive-inbox:{name}")
        except MappingNeedsSetup as e:
            _record_pending(fid, name, str(e), getattr(e, "resolution", None))
            log.warning("[drive-inbox] '%s' needs address-column setup; no job queued", name)
        return
    if mime in _CONVERTIBLE:
        # Convert in place to a Google Sheet (same folder) so answers can be
        # written back; the converted copy is marked processed immediately so
        # the next scan doesn't double-run it.
        _, drive = sheets_writer._get_services()
        copy = drive.files().copy(
            fileId=fid,
            body={"name": name.rsplit(".", 1)[0], "mimeType": _SHEET_MIME},
            fields="id", supportsAllDrives=True).execute()
        _mark(state, copy["id"])
        log.info("[drive-inbox] converted '%s' -> sheet %s", name, copy["id"])
        try:
            enqueue_bound_sheet(f"https://docs.google.com/spreadsheets/d/{copy['id']}/edit",
                                source=f"drive-inbox:{name}")
        except MappingNeedsSetup as e:
            _record_pending(copy["id"], name, str(e), getattr(e, "resolution", None))
            log.warning("[drive-inbox] converted '%s' needs address-column setup; no job queued", name)
        return
    log.info("[drive-inbox] ignoring '%s' (%s)", name, mime)


def _scan(folder_id):
    _, drive = sheets_writer._get_services()
    res = drive.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id,name,mimeType,appProperties)").execute()
    state = _load_state()
    for f in res.get("files", []):
        if f["id"] in state["ids"]:
            continue
        if (f.get("appProperties") or {}).get("parityGenerated") == "true":
            _mark(state, f["id"])
            continue
        # Mark BEFORE handling: a crashing file must never retry-bill forever.
        _mark(state, f["id"])
        try:
            _handle_file(f, state)
        except Exception as e:
            log.error("[drive-inbox] failed on '%s': %s", f.get("name"), e, exc_info=True)


def _loop(folder_id):
    log.info("Drive inbox watcher started on folder %s", folder_id)
    while True:
        try:
            _scan(folder_id)
        except Exception as e:
            log.error("Drive inbox scan failed: %s", e)
        time.sleep(POLL_SECONDS)


def start_watcher():
    """Start the inbox watcher daemon if configured. Safe no-op otherwise."""
    folder_id = os.environ.get("DRIVE_INBOX_FOLDER_ID", "").strip()
    if not folder_id or not sheets_writer.enabled():
        log.info("Drive inbox watcher disabled (no DRIVE_INBOX_FOLDER_ID / credential)")
        return None
    t = threading.Thread(target=_loop, args=(folder_id,), daemon=True)
    t.start()
    return t
