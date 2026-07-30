"""
Flask application for Render deployment.
Uses local storage and SQLite job queue instead of Google Cloud services.
"""
import os
import json
import hashlib
import uuid
import logging
import threading
import time
import hmac
import secrets
from datetime import timedelta
import pandas as pd
from pathlib import Path
from flask import (
    Flask, request, render_template, redirect, url_for, jsonify, abort,
    Response, session, send_file,
)
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

# Local modules
from job_queue import (init_db, enqueue_job, get_job_status, cancel_job,
                       check_usage_limit, batch_size_limit)
from storage_helpers import init_storage, upload_file, get_file_path, read_result, file_exists
from worker import start_worker
from api_analyze import (api as api_blueprint, _read_csv_with_fallback,
                         _read_excel_with_worker_tab_selection, _read_excel_tabs)
from market_dashboard.routes import dashboard as market_dashboard_blueprint
from tasks_local import ADDRESS_VARIANTS
from review_render import (
    FIT_OPTIONS, HVAC_SYSTEMS, NONE_OPTION, build_review_page,
)
from review_contract import (
    CURRENT_REVIEW_SCHEMA,
    DUAL_FIT_OPTIONS,
    DUAL_FIT_SCHEMA,
    FIT_COL,
    SINGLE_FIT_SCHEMA,
    entry_is_reviewed,
)
import review_store
import sheets_writer
import intake_resolver
import workbook_runs
import requests

# Initialize Flask app
app = Flask(__name__)
app.register_blueprint(api_blueprint)
app.register_blueprint(market_dashboard_blueprint)


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a deliberately small set of common true values from the environment."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Browser access is intentionally separate from the API key used by n8n. On
# Render it is enabled and fails closed if either secret is missing; local
# development stays convenient unless explicitly opted in.
_session_secret = os.getenv("SITE_SESSION_SECRET", "").strip() or secrets.token_urlsafe(48)
app.config.update(
    SECRET_KEY=_session_secret,
    SITE_ACCESS_ENABLED=_env_flag("SITE_ACCESS_ENABLED"),
    SITE_ACCESS_PASSWORD=os.getenv("SITE_ACCESS_PASSWORD", ""),
    SITE_ACCESS_SESSION_HOURS=max(1, int(os.getenv("SITE_ACCESS_SESSION_HOURS", "12"))),
    SESSION_COOKIE_SECURE=_env_flag("SITE_COOKIE_SECURE"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.permanent_session_lifetime = timedelta(hours=app.config["SITE_ACCESS_SESSION_HOURS"])

_login_attempts = {}
_login_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 8


def _browser_access_enabled() -> bool:
    return bool(app.config.get("SITE_ACCESS_ENABLED"))


def _browser_access_configured() -> bool:
    return bool(app.config.get("SITE_ACCESS_PASSWORD", "").strip()
                and os.getenv("SITE_SESSION_SECRET", "").strip())


def _csrf_token() -> str:
    """Issue a session-bound token for browser state-changing requests."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def _template_security_context():
    return {"csrf_token": _csrf_token()}


def _csrf_is_valid() -> bool:
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _browser_route_is_exempt(path: str) -> bool:
    # The stateless API remains protected by its existing X-API-Key. The review
    # relay is browser-only and is deliberately covered by this gate.
    return (path in {"/access", "/health", "/api/health"}
            or path.startswith("/static/")
            or (path.startswith("/api/") and path != "/api/review"))


def _safe_next_url(value: str) -> str:
    """Accept same-site relative redirects only; avoid open redirect links."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("index")


def _login_client_key() -> str:
    # Do not trust a user-provided forwarded-for header as the brute-force
    # limiter identity.
    return request.remote_addr or "unknown"


def _login_retry_after(client_key: str) -> int:
    now = time.time()
    with _login_attempts_lock:
        attempts = [stamp for stamp in _login_attempts.get(client_key, [])
                    if stamp > now - _LOGIN_WINDOW_SECONDS]
        _login_attempts[client_key] = attempts
        if len(attempts) < _LOGIN_MAX_FAILURES:
            return 0
        return max(1, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])))


def _record_login_failure(client_key: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        attempts = [stamp for stamp in _login_attempts.get(client_key, [])
                    if stamp > now - _LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        _login_attempts[client_key] = attempts


def _clear_login_failures(client_key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(client_key, None)


def _access_error_response(status: int, message: str):
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), status
    return render_template("access.html", configured=False, error=message,
                           next_url=_safe_next_url(request.full_path)), status


@app.before_request
def require_browser_password():
    """Protect interactive routes without changing the n8n API contract."""
    if not _browser_access_enabled() or _browser_route_is_exempt(request.path):
        return None
    if not _browser_access_configured():
        return _access_error_response(503, "Site access is not configured. Contact the Parity administrator.")
    if not session.get("browser_access_granted"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "site password required"}), 401
        return redirect(url_for("access", next=_safe_next_url(request.full_path)))
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_is_valid():
        return _access_error_response(403, "Your session needs to be refreshed. Please sign in again.")
    return None

# Flask applies this before parsing multipart uploads or a large JSON body. Keep
# it configurable: 50 MiB comfortably covers normal spreadsheets while bounding
# memory/disk use on the single Render instance.
try:
    _max_request_bytes = max(1, int(os.getenv("MAX_REQUEST_BYTES", str(50 * 1024 * 1024))))
except ValueError:
    _max_request_bytes = 50 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = _max_request_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

# Configuration
UPLOAD_FOLDER = Path(os.getenv('UPLOAD_FOLDER', 'temp_uploads'))
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
_SHEET_PREFLIGHT_TTL_SECONDS = max(60, int(os.getenv("SHEET_INTAKE_CONFIRM_TTL_SECONDS", "900")))
_sheet_preflights = {}
_sheet_preflights_lock = threading.Lock()


def _safe_upload_filename(filename: str) -> str:
    """Return a harmless supported spreadsheet filename or reject it early."""
    safe_name = secure_filename(filename or "")
    if not safe_name or Path(safe_name).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
        raise ValueError("Please upload a .csv, .xlsx, or .xls file.")
    return safe_name


def _remember_sheet_preflight(link, resolution):
    """Keep only mapping metadata server-side for the current browser handoff."""
    token = uuid.uuid4().hex
    record = {
        "sheet_link": link,
        "mapping": resolution["mapping"],
        "fingerprint": resolution["fingerprint"],
        "expires_at": time.time() + _SHEET_PREFLIGHT_TTL_SECONDS,
    }
    with _sheet_preflights_lock:
        now = time.time()
        for key, value in list(_sheet_preflights.items()):
            if value.get("expires_at", 0) <= now:
                _sheet_preflights.pop(key, None)
        _sheet_preflights[token] = record
    return token, record


def _remember_file_preflight(local_path, filename, resolution):
    """Keep a browser upload pending until its exceptional mapping is approved.

    The upload itself is already in the app's temporary storage.  This record
    intentionally contains only its local handle plus mapping metadata -- not
    the sampled customer rows sent to the resolver.
    """
    token = uuid.uuid4().hex
    record = {
        "kind": "file",
        "local_path": str(local_path),
        "filename": filename,
        "mapping": resolution["mapping"],
        "fingerprint": resolution["fingerprint"],
        "expires_at": time.time() + _SHEET_PREFLIGHT_TTL_SECONDS,
    }
    with _sheet_preflights_lock:
        now = time.time()
        for key, value in list(_sheet_preflights.items()):
            if value.get("expires_at", 0) <= now:
                _sheet_preflights.pop(key, None)
        _sheet_preflights[token] = record
    return token, record


def _get_sheet_preflight(token):
    with _sheet_preflights_lock:
        record = _sheet_preflights.get(token)
        if not record or record.get("expires_at", 0) <= time.time():
            _sheet_preflights.pop(token, None)
            return None
        return dict(record)


def _discard_sheet_preflight(token):
    with _sheet_preflights_lock:
        _sheet_preflights.pop(token, None)


def _enqueue_bound_sheet(binding):
    """Queue a live Sheet without ever rewriting its source address columns."""
    processing_rows = binding.get("processing_rows", [])
    total = len(processing_rows)
    if not total:
        return None, render_template('error.html', error_title="No rows found",
                                     error_message=f"Tab '{binding['tab']}' has a header but no data rows."), 400
    max_rows = batch_size_limit()
    if total > max_rows:
        return None, render_template('error.html', error_title="Batch too large",
                                     error_message=(f"Tab '{binding['tab']}' has {total} rows. The per-batch "
                                                    f"limit is {max_rows}; split it into smaller batches.")), 400
    can_process, current_usage, error_msg = check_usage_limit(total)
    if not can_process:
        return None, render_template('error.html', error_title="Monthly Limit Reached",
                                     error_message=error_msg, current_usage=current_usage), 403
    job_id = str(uuid.uuid4())
    local_path = UPLOAD_FOLDER / f"{job_id}.csv"
    import csv as _csv
    with open(local_path, 'w', newline='', encoding='utf-8') as fh:
        writer = _csv.writer(fh)
        writer.writerow(binding.get("processing_headers", binding["headers"]))
        writer.writerows(processing_rows)
    csv_blob_path = f"uploads/{job_id}/{job_id}.csv"
    upload_file(str(local_path), csv_blob_path)
    try:
        enqueue_job(job_id, {
            "job_id": job_id,
            "csv_path": csv_blob_path,
            "total": total,
            "sheet_binding": binding,
        })
        log.info("Job %s enqueued from live sheet %s (%d rows, mapping=%s)",
                 job_id, binding["title"], total, binding.get("intake_mapping", {}).get("method", "deterministic"))
    except Exception as e:
        log.error("Failed to enqueue bound sheet job: %s", e, exc_info=True)
        return None, (f"Error enqueuing job: {e}", 500)
    return job_id, None


def _load_browser_dataframe(path, filename):
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return _read_excel_with_worker_tab_selection(path, suffix)
    return _read_csv_with_fallback(path)


def _load_browser_frames(path, filename):
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return _read_excel_tabs(path, suffix)
    return [("Uploaded file", _read_csv_with_fallback(path))]


def _dataframe_intake_tab(df, tab="Uploaded file"):
    headers = [str(column).strip() for column in df.columns]
    rows = []
    for values in df.head(intake_resolver.sample_row_limit()).itertuples(index=False, name=None):
        rows.append(["" if pd.isna(value) else str(value).strip() for value in values])
    return {"tab": str(tab), "headers": headers, "rows": rows}


def _selected_browser_frame(frames, mapping):
    selected = next((df for tab, df in frames if str(tab) == str(mapping.get("tab") or "")), None)
    if selected is None:
        raise ValueError("The selected worksheet is no longer available")
    return selected


def _enqueue_file_dataframe(df, filename, mapping):
    """Queue a derived Address-only work file while retaining the untouched table."""
    headers = [str(column).strip() for column in df.columns]
    rows = [["" if pd.isna(value) else str(value) for value in values]
            for values in df.itertuples(index=False, name=None)]
    addresses = intake_resolver.compose_addresses(headers, rows, mapping)
    total = len(rows)
    if not total:
        return None, render_template('error.html', error_title="No rows found",
                                     error_message="The uploaded file has a header but no data rows."), 400
    max_rows = batch_size_limit()
    if total > max_rows:
        return None, render_template('error.html', error_title="Batch too large",
                                     error_message=(f"This file has {total} rows. The per-batch limit is {max_rows}; "
                                                    "split it into smaller files.")), 400
    can_process, current_usage, error_msg = check_usage_limit(total)
    if not can_process:
        return None, render_template('error.html', error_title="Monthly Limit Reached",
                                     error_message=error_msg, current_usage=current_usage), 403
    job_id = str(uuid.uuid4())
    local_path = UPLOAD_FOLDER / f"{job_id}.csv"
    import csv as _csv
    with open(local_path, 'w', newline='', encoding='utf-8') as fh:
        writer = _csv.writer(fh)
        writer.writerow(["Address"])
        writer.writerows([[address] for address in addresses])
    csv_blob_path = f"uploads/{job_id}/{job_id}.csv"
    upload_file(str(local_path), csv_blob_path)
    try:
        enqueue_job(job_id, {
            "job_id": job_id,
            "csv_path": csv_blob_path,
            "total": total,
            "original_table": {"headers": headers, "rows": rows},
            "intake_mapping": mapping,
        })
    except Exception as e:
        log.error("Failed to enqueue uploaded file: %s", e, exc_info=True)
        return None, (f"Error enqueuing job: {e}", 500)
    return job_id, None


def _workbook_run_response(run):
    status = run.get("status")
    run_id = run["run_id"]
    if run.get("confirmation_required"):
        return redirect(url_for("workbook_setup", run_id=run_id))
    if status == "approval_required":
        return redirect(url_for("workbook_approval", run_id=run_id))
    if status == "needs_attention" and run.get("preflight_failed"):
        return render_template(
            "error.html",
            error_title="Workbook stopped before analysis",
            error_message=run.get("error") or "The workbook could not pass preflight.",
        ), 409
    if status in {"queued", "analyzing", "review_open", "needs_attention", "complete"}:
        return redirect(url_for(
            "results", job_id=run_id, total=int(run.get("analysis_count") or 0),
        ))
    return render_template(
        "error.html",
        error_title="Workbook stopped before analysis",
        error_message=run.get("error") or "The workbook could not pass preflight.",
    ), 409


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    message = (f"Request is too large. The maximum request size is "
               f"{app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MiB.")
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 413
    return render_template("error.html", error_title="Upload too large",
                           error_message=message), 413

# Initialize on startup
init_db()
init_storage()
worker = start_worker()

from drive_inbox import start_watcher
start_watcher()

log.info("Application initialized - database and worker started")

# -----------------------------------------------------------------------------
# UI ROUTES
# -----------------------------------------------------------------------------
@app.route('/')
def index():
    """Upload form page."""
    return render_template('index.html')


@app.route('/access', methods=['GET', 'POST'])
def access():
    """One shared-password entry point for the internal browser workflow."""
    if not _browser_access_enabled():
        return redirect(url_for("index"))

    next_url = _safe_next_url(request.values.get("next", ""))
    if not _browser_access_configured():
        return render_template("access.html", configured=False,
                               error="Site access is not configured. Contact the Parity administrator.",
                               next_url=next_url), 503
    if request.method == "GET":
        if session.get("browser_access_granted"):
            return redirect(next_url)
        return render_template("access.html", configured=True, error="", next_url=next_url)

    if not _csrf_is_valid():
        return render_template("access.html", configured=True,
                               error="Please refresh the page and try again.", next_url=next_url), 403
    client_key = _login_client_key()
    retry_after = _login_retry_after(client_key)
    if retry_after:
        return render_template("access.html", configured=True,
                               error=f"Too many attempts. Try again in {retry_after // 60 + 1} minutes.",
                               next_url=next_url), 429
    submitted = request.form.get("password", "")
    configured = app.config["SITE_ACCESS_PASSWORD"].strip()
    if not hmac.compare_digest(submitted, configured):
        _record_login_failure(client_key)
        return render_template("access.html", configured=True,
                               error="That password is not correct.", next_url=next_url), 401

    _clear_login_failures(client_key)
    session.clear()
    session.permanent = True
    session["browser_access_granted"] = True
    _csrf_token()
    return redirect(next_url)


@app.route('/logout', methods=['POST'])
def logout():
    """End the shared browser session without touching API credentials."""
    session.clear()
    return redirect(url_for("access"))

@app.route('/upload', methods=['POST'])
def upload():
    """Handle a browser spreadsheet without mutating its source columns."""
    if 'file' not in request.files:
        return redirect(request.url)

    f = request.files['file']
    if not f or f.filename == '':
        return redirect(request.url)

    try:
        filename = _safe_upload_filename(f.filename)
    except ValueError as e:
        return render_template("error.html", error_title="Unsupported upload",
                               error_message=str(e)), 400

    # Keep the human-readable filename, but make the local path unique even
    # when two people upload a same-named CSV at the same time.
    upload_token = str(uuid.uuid4())
    local_path = UPLOAD_FOLDER / f"{upload_token}-{filename}"
    f.save(local_path)

    suffix = Path(filename).suffix.lower()
    if suffix == ".xls":
        local_path.unlink(missing_ok=True)
        return render_template(
            "error.html",
            error_title="Save this workbook as .xlsx",
            error_message=(
                "Legacy .xls files cannot carry the dropdown-preservation guarantee. "
                "Open it in Excel, choose Save As, select .xlsx, and upload that copy."
            ),
        ), 400
    if workbook_runs.surface_enabled("browser", default=True):
        try:
            run = workbook_runs.prepare_local_file(
                local_path, filename, source_kind="browser",
            )
        except Exception as e:
            log.warning("Workbook intake stopped for %s: %s", filename, e)
            return render_template(
                "error.html",
                error_title="Workbook stopped before analysis",
                error_message=str(e),
            ), 409
        finally:
            local_path.unlink(missing_ok=True)
        return _workbook_run_response(run)
    if suffix == ".xlsx":
        try:
            snapshot = workbook_runs.inspect_xlsx(local_path)
            workbook_resolution = intake_resolver.resolve_workbook_schema(
                snapshot["tabs"], ADDRESS_VARIANTS,
            )
            selected_tabs = [
                tab for tab in workbook_resolution["tabs"]
                if tab.get("status") == "selected"
            ]
            if workbook_resolution["status"] != "ready" or len(selected_tabs) > 1:
                local_path.unlink(missing_ok=True)
                return render_template(
                    "error.html",
                    error_title="Multi-tab workbook processing is not enabled yet",
                    error_message=(
                        "This workbook has multiple or exceptional address tabs. "
                        "Parity stopped before analysis so no tab could be silently omitted."
                    ),
                ), 409
        except Exception as e:
            local_path.unlink(missing_ok=True)
            return render_template(
                "error.html",
                error_title="Workbook preflight failed",
                error_message=str(e),
            ), 409

    # Parse before taking a quota or queueing work.  This accepts the same
    # CSV/XLSX/XLS formats as /api/run-file and runs the shared resolver first.
    try:
        frames = _load_browser_frames(local_path, filename)
    except Exception as e:
        log.warning(f"Could not read browser upload: {e}")
        local_path.unlink(missing_ok=True)
        return render_template("error.html", error_title="Can't read spreadsheet",
                               error_message="The uploaded file is not a readable CSV or Excel workbook."), 400

    if not frames:
        local_path.unlink(missing_ok=True)
        return render_template("error.html", error_title="No rows found",
                               error_message="The upload has a header but no data rows."), 400

    from tasks_local import ADDRESS_VARIANTS
    tabs = [_dataframe_intake_tab(df, tab) for tab, df in frames]
    resolution = intake_resolver.resolve_schema(tabs, ADDRESS_VARIANTS)
    if resolution["status"] == "suggested":
        token, _record = _remember_file_preflight(local_path, filename, resolution)
        return redirect(url_for('confirm_sheet_mapping', token=token))
    if resolution["status"] != "deterministic":
        local_path.unlink(missing_ok=True)
        return render_template(
            'error.html', error_title="We couldn't identify the address column",
            error_message=(resolution.get("reason") or
                           "Use an Address, Property Address, Street Address, or Building Address column and try again.")), 400

    try:
        df = _selected_browser_frame(frames, resolution["mapping"])
    except ValueError as e:
        return render_template('error.html', error_title="Can't use that workbook",
                               error_message=str(e)), 400
    job_id, error_response = _enqueue_file_dataframe(df, filename, resolution["mapping"])
    if error_response:
        return error_response
    return redirect(url_for('results', job_id=job_id, total=len(df)))


@app.route('/upload-sheet', methods=['POST'])
def upload_sheet():
    """Run-in-place mode (the primary flow): paste a link to the team's live
    Google Sheet; the analyzer reads it, runs the pipeline, and review Submits
    write back into THAT sheet's own rows. No new sheet is created."""
    link = (request.form.get('sheet_link') or '').strip()
    if not link:
        return redirect(url_for('index'))
    if not sheets_writer.enabled():
        return render_template('error.html', error_title="Sheets not configured",
                               error_message="The server has no Google credential."), 500
    if workbook_runs.surface_enabled("browser", default=True):
        try:
            run = workbook_runs.prepare_google_sheet(
                link, source_kind="browser_sheet",
            )
        except Exception as e:
            log.warning("Multi-tab Sheet intake stopped: %s", e)
            return render_template(
                "error.html",
                error_title="Workbook stopped before analysis",
                error_message=str(e),
            ), 409
        return _workbook_run_response(run)
    from tasks_local import ADDRESS_VARIANTS
    try:
        inspected = sheets_writer.inspect_bound_sheet(link)
        resolution = intake_resolver.resolve_schema(inspected["tabs"], ADDRESS_VARIANTS)
        if resolution["status"] == "deterministic":
            binding, _headers, _data_rows = sheets_writer.read_bound_sheet(link, ADDRESS_VARIANTS)
        elif resolution["status"] == "suggested":
            # Browser users always approve a Grok suggestion before any quota,
            # queue, review-column, or Sheet write action can happen.
            token, _record = _remember_sheet_preflight(link, resolution)
            return redirect(url_for('confirm_sheet_mapping', token=token))
        else:
            return render_template(
                'error.html', error_title="We couldn't identify the address column",
                error_message=(resolution.get("reason") or
                               "Rename the address column to Address, Property Address, Street Address, or Building Address and try again.")), 400
    except ValueError as e:
        return render_template('error.html', error_title="Can't use that link",
                               error_message=str(e)), 400
    except Exception as e:
        log.warning(f"Bound-sheet read failed for {link}: {e}")
        return render_template(
            'error.html', error_title="Can't open that Google Sheet",
            error_message=("The analyzer couldn't read it. Make sure the sheet is shared with "
                           "the analyzer robot as Editor: sheet-writer@gen-lang-client-0702830838"
                           ".iam.gserviceaccount.com — then paste the link again.")), 400

    job_id, error_response = _enqueue_bound_sheet(binding)
    if error_response:
        return error_response
    return redirect(url_for('results', job_id=job_id, total=len(binding.get("processing_rows", []))))


@app.route("/workbook/<run_id>/setup", methods=["GET", "POST"])
def workbook_setup(run_id):
    """One fail-closed confirmation screen for every exceptional workbook tab."""
    run = workbook_runs.load_run(run_id)
    if not run:
        abort(404)
    if run.get("status") != "preflight" or not run.get("confirmation_required"):
        return _workbook_run_response(run)
    pending = [
        tab for tab in run.get("tabs", [])
        if tab.get("status") in {"confirmation_required", "ambiguous", "unresolved"}
    ]
    if request.method == "GET":
        return render_template(
            "workbook_setup.html", run=run, pending=pending,
        )
    selections = []
    for index, tab in enumerate(pending):
        selections.append({
            "tab": tab["tab"],
            "classification": request.form.get(f"classification_{index}", ""),
            "address_column": request.form.get(f"address_column_{index}") or None,
            "city_column": request.form.get(f"city_column_{index}") or None,
            "state_column": request.form.get(f"state_column_{index}") or None,
            "zip_column": request.form.get(f"zip_column_{index}") or None,
        })
    try:
        updated = workbook_runs.confirm_mappings(run_id, selections)
    except Exception as e:
        return render_template(
            "workbook_setup.html", run=run, pending=pending, error=str(e),
        ), 400
    try:
        from drive_inbox import clear_pending_run
        clear_pending_run(run_id)
    except Exception:
        log.warning("Could not clear Drive pending marker for %s", run_id)
    return _workbook_run_response(updated)


@app.route("/workbook/<run_id>/approval", methods=["GET", "POST"])
def workbook_approval(run_id):
    run = workbook_runs.load_run(run_id)
    if not run:
        abort(404)
    if run.get("status") != "approval_required":
        return _workbook_run_response(run)
    if request.method == "GET":
        return render_template(
            "workbook_approval.html",
            run=workbook_runs.public_summary(run),
        )
    try:
        approved = workbook_runs.approve(run_id)
    except Exception as e:
        return render_template(
            "workbook_approval.html",
            run=workbook_runs.public_summary(run),
            error=str(e),
        ), 409
    try:
        from drive_inbox import clear_pending_run
        clear_pending_run(run_id)
    except Exception:
        log.warning("Could not clear Drive pending marker for %s", run_id)
    return _workbook_run_response(approved)


@app.route("/workbook/<run_id>/retry", methods=["POST"])
def workbook_retry(run_id):
    try:
        run = workbook_runs.retry(run_id)
    except ValueError as e:
        return render_template(
            "error.html",
            error_title="Workbook could not be retried",
            error_message=str(e),
        ), 409
    return _workbook_run_response(run)


@app.route('/confirm-sheet-mapping/<token>', methods=['GET', 'POST'])
def confirm_sheet_mapping(token):
    """Browser-only confirmation for an exceptional Grok sheet mapping."""
    record = _get_sheet_preflight(token)
    if not record:
        return render_template('error.html', error_title="Mapping confirmation expired",
                               error_message="Open the Sheet through Parity again to create a fresh mapping."), 410
    if request.method == 'GET':
        return render_template('confirm_mapping.html', mapping=record["mapping"])
    try:
        if record.get("kind") == "file":
            source_path = Path(record["local_path"])
            if not source_path.is_file():
                raise ValueError("The temporary upload is no longer available")
            frames = _load_browser_frames(source_path, record["filename"])
            tabs = [_dataframe_intake_tab(frame, tab) for tab, frame in frames]
            fingerprint = intake_resolver.schema_fingerprint(tabs)
            if fingerprint != record["fingerprint"]:
                raise ValueError("The uploaded file changed before confirmation")
            valid, _reason, _ratio = intake_resolver.validate_mapping(
                tabs, record["mapping"])
            if not valid:
                raise ValueError("The suggested columns are no longer usable")
            df = _selected_browser_frame(frames, record["mapping"])
            job_id, error_response = _enqueue_file_dataframe(
                df, record["filename"], record["mapping"])
        else:
            binding, _headers, _rows = sheets_writer.read_bound_sheet_mapping(
                record["sheet_link"], record["mapping"], record["fingerprint"])
            job_id, error_response = _enqueue_bound_sheet(binding)
    except Exception as e:
        log.warning("Sheet mapping confirmation failed: %s", e)
        return render_template('error.html', error_title="Mapping needs another look",
                               error_message="The Sheet changed or the suggested columns are no longer usable. Start again from the Sheet link."), 409
    if error_response:
        return error_response
    _discard_sheet_preflight(token)
    total = len(df) if record.get("kind") == "file" else len(binding.get("processing_rows", []))
    return redirect(url_for('results', job_id=job_id, total=total))

@app.route('/results/<job_id>')
def results(job_id):
    """Results page with progress polling."""
    total_count = request.args.get('total', 0, type=int)
    return render_template('results.html', job_id=job_id, total_count=total_count)


def _compact_status_rows(result):
    """Project a persisted result down to the fields used by the progress page."""
    rows = result.get("live_rows") or []
    if not rows and result.get("address_states"):
        rows = result["address_states"]

    compact_rows = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row_id = item.get("row_id")
        if row_id is None:
            row_id = item.get("index", "")
        compact_rows.append({
            "row_id": str(row_id),
            "address": str(item.get("address") or ""),
            "tab": str(item.get("tab") or ""),
            "source_row": item.get("source_row"),
            "state": str(item.get("state") or "queued"),
            "message": str(item.get("message") or ""),
            "error": str(item.get("error") or ""),
        })
    return compact_rows


def _status_rows_version(rows):
    """Return a stable content version so unchanged row lists need not be resent."""
    payload = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compact_status_result(result, status_name):
    """Keep the legacy ``result`` envelope without returning analysis payloads."""
    summary = {}
    for key in ("review_url", "sheet_url"):
        if result.get(key):
            summary[key] = result[key]

    diagnostic = result.get("diagnostic")
    if status_name == "failed" and isinstance(diagnostic, dict):
        compact_diagnostic = {
            key: diagnostic[key]
            for key in ("diagnostic_summary", "claude_code_context")
            if diagnostic.get(key)
        }
        if compact_diagnostic:
            summary["diagnostic"] = compact_diagnostic
    return summary


@app.route('/status/<job_id>')
def job_status_route(job_id):
    """API endpoint for job status polling."""
    status = get_job_status(job_id)

    if not status:
        return jsonify({
            "status": "not_found",
            "message": "Job not found"
        }), 404

    # Build response
    response = {
        "status": status['status'],
        "progress": status.get('progress', 0),
        "total": status.get('total', 0),
        "cancel_requested": status.get('cancel_requested', False)
    }

    if status.get('message'):
        response['message'] = status['message']

    if status['status'] in ['processing', 'finished', 'failed']:
        result = read_result(job_id) or {}
        if result:
            if request.args.get("compact") == "1":
                # Browser polling needs row state and final links, not the full
                # model/image payload. Compact mode is explicit so legacy API
                # consumers retain the original response contract.
                rows = _compact_status_rows(result)
                rows_version = _status_rows_version(rows)
                response["rows_version"] = rows_version

                client_rows_version = request.args.get("rows_version", "")
                rows_changed = client_rows_version != rows_version
                response["rows_changed"] = rows_changed
                if rows_changed:
                    response["rows"] = rows

                result_summary = _compact_status_result(
                    result, status["status"],
                )
                if result_summary:
                    response["result"] = result_summary
            else:
                response["result"] = result
                rows = result.get("live_rows") or []
                if not rows and result.get("address_states"):
                    rows = [
                        {
                            "row_id": str(item.get("index", "")),
                            "address": item.get("address") or "",
                            "tab": "",
                            "source_row": None,
                            "state": item.get("state") or "queued",
                            "message": item.get("message") or "",
                            "error": item.get("error") or "",
                            "model_result": "",
                        }
                        for item in result.get("address_states", [])
                        if isinstance(item, dict)
                    ]
                response["rows"] = rows

            if result.get("live_rows") is not None:
                response["total"] = len(rows)
                response["progress"] = sum(
                    str(item.get("state") or "") in {
                        "complete", "attention", "failed",
                    }
                    for item in rows
                    if isinstance(item, dict)
                )

    return jsonify(response), 200

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job_route(job_id):
    """Cancel a running job."""
    try:
        cancel_job(job_id)
        return jsonify({'status': 'cancelled'})
    except Exception as e:
        log.error(f"Failed to cancel job {job_id}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# -----------------------------------------------------------------------------
# FILE SERVING
# -----------------------------------------------------------------------------
@app.route('/files/<path:blob_name>')
def serve_file(blob_name):
    """Stream a private artifact from local storage."""
    try:
        if not file_exists(blob_name):
            abort(404)

        lower = blob_name.lower()

        # Determine MIME type
        if lower.endswith('.jpg') or lower.endswith('.jpeg'):
            mimetype = 'image/jpeg'
        elif lower.endswith('.png'):
            mimetype = 'image/png'
        elif lower.endswith('.csv'):
            mimetype = 'text/csv'
        elif lower.endswith('.html') or lower.endswith('.htm'):
            mimetype = 'text/html'
        elif lower.endswith('.zip'):
            mimetype = 'application/zip'
        else:
            mimetype = 'application/octet-stream'

        attachment = lower.endswith(('.csv', '.zip'))
        resp = send_file(
            get_file_path(blob_name),
            mimetype=mimetype,
            as_attachment=attachment,
            download_name=os.path.basename(blob_name) if attachment else None,
            conditional=True,
            etag=False,
            max_age=0,
        )
        # These files can contain customer addresses, roof imagery, and review
        # reports. Even though the browser password protects this route, marking
        # a response ``public`` allows shared proxies/CDNs to retain it outside
        # the authenticated session. Keep every stored artifact private and
        # prevent browser or intermediary reuse after logout.
        resp.headers['Cache-Control'] = 'private, no-store, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'

        return resp
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error serving file {blob_name}: {e}")
        abort(404)

# -----------------------------------------------------------------------------
# REVIEW LOOP (team confirms HVAC + fit per building; writes back to the sheet)
# -----------------------------------------------------------------------------
@app.route('/reviews')
def reviews_index():
    """One bookmarkable page listing every batch, newest first — the team's
    front door. No auth, same as the review pages themselves; batch ids stay
    unguessable elsewhere but this page trades that for accessibility."""
    import html as _h
    rows = ""
    try:
        from drive_inbox import pending_mappings
        for item in pending_mappings():
            title = _h.escape(item.get("name") or "Incoming Sheet")
            reason = _h.escape(item.get("reason") or "Address column needs setup")
            run_id = item.get("run_id")
            action = ""
            if run_id:
                endpoint = (
                    "workbook_approval"
                    if item.get("status") == "approval_required"
                    else "workbook_setup"
                )
                action = (
                    f'<div class="acts"><a class="btn" href="'
                    f'{_h.escape(url_for(endpoint, run_id=run_id))}">Continue</a></div>'
                )
            rows += (f'<div class="row"><div class="meta"><div class="t">Needs setup: {title}</div>'
                     f'<div class="s">{reason}.</div>'
                     f'</div>{action}</div>')
    except Exception as e:
        log.warning("Could not read pending Drive mappings: %s", e)
    for b in review_store.list_batches():
        title = _h.escape(b.get("title") or b["batch_id"])
        created = _h.escape((b.get("created") or "")[:10])
        prog = f"{b['reviewed']}/{b['count']} reviewed"
        done = ' style="color:#37b24d"' if b["count"] and b["reviewed"] >= b["count"] else ""
        sheet = (f'<a class="btn ghost" href="{_h.escape(b["sheet_url"])}" target="_blank" '
                 f'rel="noopener">Sheet</a>' if b.get("sheet_url") else "")
        rows += (f'<div class="row"><div class="meta"><div class="t">{title}</div>'
                 f'<div class="s">{created} · <span{done}>{prog}</span></div></div>'
                 f'<div class="acts"><a class="btn" href="/review/{_h.escape(b["batch_id"])}">'
                 f'Review</a>{sheet}</div></div>')
    if not rows:
        rows = '<div class="row"><div class="meta"><div class="s">No batches yet.</div></div></div>'
    html = f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cooling Tower Reviews</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{margin:0;background:#0d0f12;color:#e6e8eb;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
header{{background:#12151a;border-bottom:1px solid #262b33;padding:14px 20px}}
header h1{{margin:0;font-size:17px}}header .sub{{color:#9aa3ad;font-size:13px;margin-top:2px}}
.wrap{{max-width:760px;margin:0 auto;padding:18px}}
.row{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
 background:#161a20;border:1px solid #262b33;border-radius:12px;padding:14px 16px;margin:0 0 12px}}
.t{{font-weight:600}}.s{{color:#9aa3ad;font-size:13px;margin-top:2px}}
.acts{{display:flex;gap:8px}}
.btn{{background:#2563eb;color:#fff;text-decoration:none;border-radius:8px;padding:8px 16px;font-size:14px}}
.btn.ghost{{background:#1e232b;color:#cfd4da;border:1px solid #333b45}}
.btn:hover{{filter:brightness(1.15)}}
</style></head><body>
<header><h1>Cooling Tower Reviews</h1>
<div class="sub">every analysis batch, newest first · bookmark this page</div></header>
<div class="wrap">{rows}</div>
</body></html>'''
    return Response(html, mimetype="text/html")


@app.route('/review/<job_id>')
def review_page(job_id):
    """Serve the blessed interactive review page for a persisted batch."""
    batch = review_store.load_batch(job_id)
    if not batch:
        abort(404)
    try:
        page_size = int(os.getenv("REVIEW_PAGE_SIZE", "10"))
    except (TypeError, ValueError):
        page_size = 10
    page_size = min(max(page_size, 1), 50)
    html = build_review_page(
        batch.get("entries", []),
        job_id=job_id,
        webhook_url="/api/review",  # same-origin relay -> no browser CORS
        title=batch.get("title", "Cooling Tower Review"),
        csrf_token=_csrf_token(),
        review_schema=batch.get("review_schema", CURRENT_REVIEW_SCHEMA),
        page=request.args.get("page", 1, type=int),
        page_size=page_size,
        page_url=request.path,
    )
    return Response(html, mimetype="text/html")


@app.route('/api/review', methods=['POST'])
def api_review():
    """Receive one reviewer decision from the review page: stamp it into the local
    batch store AND write it live into the batch's Google Sheet row (when the batch
    has a sheet). The local record stays authoritative for GET /api/batch, so a
    Sheets outage never loses a decision or fails the reviewer's Submit. No API key
    required from the reviewer."""
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    row_id = payload.get("row_id")
    if not job_id or row_id is None:
        return jsonify({"error": "missing job_id/row_id"}), 400
    existing_batch = review_store.load_batch(job_id)
    if not existing_batch or not any(
        str(
            item.get("row_id")
            if item.get("row_id") is not None
            else item.get("i", "")
        ) == str(row_id)
        for item in existing_batch.get("entries", [])
    ):
        return jsonify({"error": "unknown batch/row"}), 404
    review_schema = existing_batch.get("review_schema", CURRENT_REVIEW_SCHEMA)
    hvac_value = str(payload.get("hvac_systems") or "").strip()
    if not hvac_value:
        return jsonify({"error": "choose at least one HVAC system or None"}), 400
    systems = [value.strip() for value in hvac_value.split(",") if value.strip()]
    if (
        not systems
        or any(value not in HVAC_SYSTEMS + [NONE_OPTION] for value in systems)
        or (NONE_OPTION in systems and len(systems) > 1)
    ):
        return jsonify({"error": "invalid HVAC selection"}), 400
    if review_schema == DUAL_FIT_SCHEMA:
        if payload.get("optimizer_fit") not in DUAL_FIT_OPTIONS:
            return jsonify({"error": "Optimizer Fit is required"}), 400
        if payload.get("periscope_fit") not in DUAL_FIT_OPTIONS:
            return jsonify({"error": "Periscope Fit is required"}), 400
    elif payload.get("fit") not in FIT_OPTIONS:
        return jsonify({"error": "Fit is required"}), 400
    if len(str(payload.get("note") or "")) > 2000:
        return jsonify({"error": "note is too long"}), 400

    decision = {
        "hvac_systems": payload.get("hvac_systems", ""),
        "note": payload.get("note", ""),
    }
    if review_schema == DUAL_FIT_SCHEMA:
        decision.update({
            "optimizer_fit": payload.get("optimizer_fit", ""),
            "periscope_fit": payload.get("periscope_fit", ""),
        })
    else:
        decision["fit"] = payload.get("fit", "")
    batch = review_store.record_decision(job_id, row_id, decision)
    if not batch:
        return jsonify({"error": "unknown batch/row"}), 404

    sheet = "none"
    sheet_error = ""
    if batch.get("sheet_bindings"):
        if not sheets_writer.enabled():
            sheet = "error"
            sheet_error = "Google Sheets write-back is not configured"
            entry = None
            binding = None
        else:
            entry = next(
                (
                    item for item in batch.get("entries", [])
                    if str(item.get("row_id") if item.get("row_id") is not None else item.get("i", ""))
                    == str(row_id)
                ),
                None,
            )
            binding = None
            if entry:
                binding = next(
                    (
                        item for item in batch["sheet_bindings"]
                        if (
                            str(item.get("grid_id")) == str(entry.get("source_grid_id"))
                            and item.get("tab") == entry.get("source_tab")
                        )
                    ),
                    None,
                )
        if sheet != "error" and (not entry or not binding):
            sheet = "row_not_found"
            sheet_error = "Source tab binding was not found"
        elif sheet != "error":
            try:
                needs_columns = not binding.get("colmap")
                if review_schema == SINGLE_FIT_SCHEMA:
                    needs_columns = needs_columns or FIT_COL not in binding.get("colmap", {})
                else:
                    needs_columns = needs_columns or any(
                        column not in binding.get("colmap", {})
                        for column in (
                            sheets_writer.OPT_FIT_COL,
                            sheets_writer.PERI_FIT_COL,
                        )
                    )
                if needs_columns:
                    sheets_writer.ensure_review_columns(
                        binding, binding.get("headers", []),
                        HVAC_SYSTEMS + [NONE_OPTION],
                        FIT_OPTIONS if review_schema == SINGLE_FIT_SCHEMA else DUAL_FIT_OPTIONS,
                        review_schema=review_schema,
                    )
                ok = sheets_writer.write_decision_source(
                    binding, entry.get("source_row"),
                    hvac=payload.get("hvac_systems", ""),
                    optimizer_fit=payload.get("optimizer_fit", ""),
                    periscope_fit=payload.get("periscope_fit", ""),
                    note=payload.get("note", ""),
                    fit=payload.get("fit", ""),
                )
                sheet = "updated" if ok else "row_not_found"
                if not ok:
                    sheet_error = "Source row could not be updated"
            except Exception as e:
                log.error(
                    f"Workbook sheet write failed for {job_id}/{row_id}: {e}",
                    exc_info=True,
                )
                sheet = "error"
                sheet_error = str(e)
    elif batch.get("sheet_binding") and sheets_writer.enabled():
        # Run-in-place batch: write straight into the team's own sheet row.
        try:
            binding = batch["sheet_binding"]
            if (
                not binding.get("colmap")
                or (
                    review_schema == SINGLE_FIT_SCHEMA
                    and FIT_COL not in binding.get("colmap", {})
                )
                or (
                    review_schema == DUAL_FIT_SCHEMA
                    and any(
                        column not in binding.get("colmap", {})
                        for column in (
                            sheets_writer.OPT_FIT_COL,
                            sheets_writer.PERI_FIT_COL,
                        )
                    )
                )
            ):
                sheets_writer.ensure_review_columns(
                    binding,
                    binding.get("headers", []),
                    HVAC_SYSTEMS + [NONE_OPTION],
                    FIT_OPTIONS if review_schema == SINGLE_FIT_SCHEMA else DUAL_FIT_OPTIONS,
                    review_schema=review_schema,
                )
            ok = sheets_writer.write_decision_bound(
                binding, row_id,
                hvac=payload.get("hvac_systems", ""),
                optimizer_fit=payload.get("optimizer_fit", ""),
                periscope_fit=payload.get("periscope_fit", ""),
                note=payload.get("note", ""),
                fit=payload.get("fit", ""))
            sheet = "updated" if ok else "row_not_found"
        except Exception as e:
            log.error(f"Bound sheet write failed for {job_id}/{row_id}: {e}", exc_info=True)
            sheet = "error"
            sheet_error = str(e)
    elif batch.get("sheet_url") and sheets_writer.enabled():
        try:
            ok = sheets_writer.write_decision(
                batch["sheet_url"], batch.get("table_headers", []),
                batch.get("table_rows", []), row_id,
                hvac=payload.get("hvac_systems", ""),
                optimizer_fit=payload.get("optimizer_fit", ""),
                periscope_fit=payload.get("periscope_fit", ""),
                note=payload.get("note", ""),
                fit=payload.get("fit", ""))
            sheet = "updated" if ok else "row_not_found"
        except Exception as e:
            log.error(f"Sheet write failed for {job_id}/{row_id}: {e}", exc_info=True)
            sheet = "error"
            sheet_error = str(e)
    if sheet != "none":
        batch = review_store.record_writeback(
            job_id, row_id, "updated" if sheet == "updated" else "error",
            sheet_error or sheet,
        ) or batch
        if batch.get("run_id"):
            workbook_runs.record_metric(
                "workbook_writebacks_updated"
                if sheet == "updated" else "workbook_writeback_failures"
            )

    run_id = batch.get("run_id")
    if run_id:
        try:
            entries = batch.get("entries", [])
            reviewed = sum(
                1 for entry in entries
                if entry_is_reviewed(entry, review_schema)
            )
            writeback_failures = sum(
                1 for entry in entries
                if (entry.get("writeback") or {}).get("status") == "error"
            )
            unresolved_attention = sum(
                1 for entry in entries
                if entry.get("error") and not entry_is_reviewed(entry, review_schema)
            )
            workbook_runs.update_review_progress(
                run_id, reviewed, len(entries),
                writeback_failures=writeback_failures,
                needs_attention=unresolved_attention,
            )
        except Exception as e:
            log.error("Workbook progress update failed for %s: %s", run_id, e)
    # The local decision is saved even when the live Sheet write fails. Keep
    # that distinction explicit so the review page cannot report a false
    # "saved" state to the operator.
    return jsonify({
        "ok": True,
        "local_saved": True,
        "sheet": sheet,
        "sheet_error": sheet_error,
    })


# -----------------------------------------------------------------------------
# HEALTH CHECK
# -----------------------------------------------------------------------------
@app.route('/health')
def health():
    """Liveness plus the two local dependencies the browser flow needs.

    Keep HTTP 200 for Render's liveness probe; callers can use ``status`` and
    the component flags to distinguish a merely-running process from a ready
    one without exposing any secret configuration.
    """
    worker_thread = getattr(worker, "thread", None)
    worker_running = bool(getattr(worker, "running", False)
                          and worker_thread and worker_thread.is_alive())
    try:
        storage_ready = get_file_path("results").is_dir()
    except Exception:
        storage_ready = False
    ready = worker_running and storage_ready
    return jsonify({
        "status": "healthy" if ready else "degraded",
        "worker_running": worker_running,
        "storage_ready": storage_ready,
    })

# -----------------------------------------------------------------------------
# RUN
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.getenv("PORT", "8080"))
    app.run(host='0.0.0.0', port=port, debug=False)
