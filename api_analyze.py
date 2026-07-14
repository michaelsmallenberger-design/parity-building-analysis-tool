"""Stateless analyzer HTTP API (Flask Blueprint).

These endpoints exist so an external orchestrator (n8n Cloud) can drive the
cooling-tower pipeline per address and assemble an emailed report, WITHOUT the
SQLite job queue, background worker, or local file storage that the browser UI
(app_railway.py) relies on. n8n cannot run the ML itself (its Python node is a
sandboxed Pyodide runtime — no PyTorch/YOLO), so it calls these endpoints.

Endpoints:
    POST /api/analyze  {address, boro_area?, zip?}  -> web_entry JSON (images as data: URIs)
    POST /api/report   {results: [web_entry,...], title?} -> text/html audit report
    GET  /api/health   -> {status: ok}   (unauthenticated)

Auth: every spend-incurring route requires header `X-API-Key` matching env
`ANALYZE_API_KEY`. If `ANALYZE_API_KEY` is unset the routes refuse to run (503)
rather than silently accepting unauthenticated VLM spend.

Images: the per-address pipeline writes annotated JPGs to tempdir and calls
upload_file(local, blob) + make_signed_url(blob)->url. We inject a Base64Sink so
those become inline `data:image/jpeg;base64,...` URIs — the returned web_entry is
fully self-contained and report_audit embeds it directly. No object storage.
"""
import base64
import logging
import os
import re
import tempfile
import uuid
from functools import wraps
from pathlib import Path

import pandas as pd
import requests
from flask import Blueprint, jsonify, request, Response

from tasks_local import _process_one_address, _build_web_entry
from report_audit import build_audit_report
from review_render import HVAC_SYSTEMS, NONE_OPTION, FIT_OPTIONS
import review_store
import sheets_writer

log = logging.getLogger("api")

api = Blueprint("api", __name__, url_prefix="/api")

ADDRESS_COLUMNS = [
    "Address", "address", "ADDRESS",
    "Property Address", "property address", "PROPERTY ADDRESS",
    "PropertyAddress", "propertyaddress", "PROPERTYADDRESS",
    "Street Address", "street address", "STREET ADDRESS",
    "StreetAddress", "streetaddress", "STREETADDRESS",
    "Building Address", "building address", "BUILDING ADDRESS",
    "BuildingAddress", "buildingaddress", "BUILDINGADDRESS",
    "Property_Address", "property_address", "PROPERTY_ADDRESS",
    "Street_Address", "street_address", "STREET_ADDRESS",
    "Building_Address", "building_address", "BUILDING_ADDRESS",
]
BORO_COLUMNS = ["Boro_Area", "boro_area", "Borough", "borough", "City", "city"]
ZIP_COLUMNS = ["Zip", "ZIP", "zip", "Zip Code", "zip code", "Postal Code", "postal code"]


class Base64Sink:
    """Drop-in replacement for storage_helpers.upload_file / make_signed_url that
    keeps annotated tiles in memory and hands them back as data: URIs, so the
    pipeline produces a self-contained web_entry with zero filesystem storage."""

    def __init__(self):
        self.store = {}

    def upload_file(self, local_path: str, blob_name: str) -> str:
        try:
            with open(local_path, "rb") as f:
                self.store[blob_name] = base64.b64encode(f.read()).decode("ascii")
        except OSError as e:
            log.warning("Base64Sink could not read %s: %s", local_path, e)
        return blob_name

    def make_signed_url(self, blob_name: str) -> str:
        b64 = self.store.get(blob_name)
        if not b64:
            return ""
        return f"data:image/jpeg;base64,{b64}"


def require_key(fn):
    """Reject requests without a valid X-API-Key. 503 if the server has no key
    configured (fail closed — these routes spend VLM money)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        configured = os.environ.get("ANALYZE_API_KEY", "").strip()
        if not configured:
            return jsonify({"error": "ANALYZE_API_KEY not configured on server"}), 503
        provided = (request.headers.get("X-API-Key") or "").strip()
        if provided != configured:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@api.route("/health")
def health():
    return jsonify({"status": "ok"})


def _analyze_one(address, boro_area=None, zip_code=None, total=1):
    """Run the per-address pipeline once and return a self-contained web_entry
    (images as data: URIs). Never raises — pipeline failures are encoded in the
    entry's error/verdict fields so a batch keeps going on a bad address."""
    address = (address or "").strip()
    record = {"Address": address}
    if boro_area:
        record["Boro_Area"] = str(boro_area).strip()
    if zip_code:
        record["Zip"] = str(zip_code).strip()
    df = pd.DataFrame([record])
    row = df.iloc[0]
    columns = list(df.columns)
    job_id = f"api-{uuid.uuid4().hex[:8]}"
    sink = Base64Sink()
    try:
        web_entry, _csv_row = _process_one_address(
            row, 0, columns, total, job_id, sink.upload_file, sink.make_signed_url
        )
    except Exception as e:  # transient footprint / unexpected — surface, don't crash
        log.error("analyze failed for %r: %s", address, e, exc_info=True)
        web_entry = _build_web_entry(
            full_address=address, verdict="", consensus_dict=None,
            detection_count=0, construction=False,
            notes=f"Analyzer error: {e}",
            original_url=None, result_url=None,
            error="Analyzer Error",
        )
    return web_entry


def _coerce_address_item(item):
    """Accept either a plain address string or a {address, boro_area, zip} dict."""
    if isinstance(item, str):
        return item.strip(), None, None
    if isinstance(item, dict):
        return (
            (item.get("address") or "").strip(),
            item.get("boro_area"),
            item.get("zip"),
        )
    return "", None, None


def _norm_column_name(value):
    return " ".join(str(value).strip().lower().replace("_", " ").split())


def _first_present(columns, candidates):
    lookup = {_norm_column_name(column): column for column in columns}
    for name in candidates:
        match = lookup.get(_norm_column_name(name))
        if match is not None:
            return match
    return None


def _read_csv_with_fallback(path):
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding, sep=None, engine="python")
        except Exception as e:
            last_error = e
    raise last_error


def _read_uploaded_address_file(uploaded_file):
    """Read uploaded CSV/XLS/XLSX and return /api/run-compatible address items."""
    filename = uploaded_file.filename or "addresses"
    suffix = Path(filename).suffix.lower()
    max_rows = int(os.environ.get("ANALYZE_FILE_MAX_ROWS", "250"))

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".xlsx") as tmp:
        uploaded_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        try:
            if suffix in {".xlsx", ".xls"}:
                df = pd.read_excel(tmp_path)
            elif suffix == ".csv":
                df = _read_csv_with_fallback(tmp_path)
            elif not suffix:
                try:
                    df = pd.read_excel(tmp_path)
                except Exception:
                    df = _read_csv_with_fallback(tmp_path)
            else:
                raise ValueError("Upload must be an .xlsx, .xls, or .csv file")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Could not read uploaded file: {e}") from e
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if df.empty:
        raise ValueError("Uploaded file has no rows")

    address_col = _first_present(df.columns, ADDRESS_COLUMNS)
    if not address_col:
        raise ValueError(
            "Uploaded file must include an address column. Supported names include "
            "Address, Property Address, Street Address, and Building Address. "
            f"Found columns: {list(df.columns)}"
        )

    boro_col = _first_present(df.columns, BORO_COLUMNS)
    zip_col = _first_present(df.columns, ZIP_COLUMNS)

    items = []
    for _, row in df.iterrows():
        address = row.get(address_col)
        if pd.isna(address) or not str(address).strip():
            continue
        item = {"address": str(address).strip()}
        if boro_col and pd.notna(row.get(boro_col)) and str(row.get(boro_col)).strip():
            item["boro_area"] = str(row.get(boro_col)).strip()
        if zip_col and pd.notna(row.get(zip_col)) and str(row.get(zip_col)).strip():
            item["zip"] = str(row.get(zip_col)).strip()
        items.append(item)

    if not items:
        raise ValueError("Uploaded file has no usable address rows")
    if len(items) > max_rows:
        raise ValueError(
            f"Uploaded file has {len(items)} address rows, above the safety limit of "
            f"{max_rows}. Split the file or raise ANALYZE_FILE_MAX_ROWS on the server."
        )
    return items


def _read_uploaded_dataframe(uploaded_file):
    """Read the uploaded CSV/XLS/XLSX into a DataFrame with ALL columns preserved,
    and locate the address / boro / zip columns. Enforces ANALYZE_FILE_MAX_ROWS.
    Returns (df, address_col, boro_col, zip_col)."""
    filename = uploaded_file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    max_rows = int(os.environ.get("ANALYZE_FILE_MAX_ROWS", "250"))
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".xlsx") as tmp:
        uploaded_file.save(tmp.name)
        tmp_path = tmp.name
    try:
        try:
            if suffix in {".xlsx", ".xls"}:
                df = pd.read_excel(tmp_path)
            elif suffix == ".csv":
                df = _read_csv_with_fallback(tmp_path)
            elif not suffix:
                try:
                    df = pd.read_excel(tmp_path)
                except Exception:
                    df = _read_csv_with_fallback(tmp_path)
            else:
                raise ValueError("Upload must be an .xlsx, .xls, or .csv file")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Could not read uploaded file: {e}") from e
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    if df.empty:
        raise ValueError("Uploaded file has no rows")
    address_col = _first_present(df.columns, ADDRESS_COLUMNS)
    if not address_col:
        raise ValueError(
            "Uploaded file must include an address column (e.g. Address, Property "
            f"Address, Street Address). Found columns: {list(df.columns)}")
    if len(df) > max_rows:
        raise ValueError(
            f"Uploaded file has {len(df)} rows, above the safety limit of {max_rows}. "
            "Split the file or raise ANALYZE_FILE_MAX_ROWS on the server.")
    boro_col = _first_present(df.columns, BORO_COLUMNS)
    zip_col = _first_present(df.columns, ZIP_COLUMNS)
    return df, address_col, boro_col, zip_col


@api.route("/analyze", methods=["POST"])
@require_key
def analyze():
    """Analyze a single address. Always returns 200 with a web_entry (errors are
    encoded in the entry's `error`/`verdict` fields) so an n8n per-row loop keeps
    going on a bad address instead of aborting the batch."""
    body = request.get_json(silent=True) or {}
    address = (body.get("address") or "").strip()
    if not address:
        return jsonify({"error": "missing 'address'"}), 400
    return jsonify(_analyze_one(address, body.get("boro_area"), body.get("zip")))


CANON_HVAC, CANON_FIT, CANON_NOTES, CANON_ID = "HVAC Systems", "Fit", "Notes", "Row_ID"
# Header-name variants for locating the HVAC / Fit columns in an arbitrary upload
# (fallback when the columns are blank; content detection below handles filled ones).
HVAC_SYSTEM_COL_NAMES = ["HVAC Systems", "HVAC System", "HVAC", "HVAC Equipment", "HVAC Type"]
FIT_COL_NAMES = ["Fit", "Fit Type", "Product Fit"]


def _detect_hvac_fit_columns(df):
    """Find the HVAC Systems and Fit columns in ANY uploaded format. Primary signal is
    CONTENT — a column whose values match the HVAC or Fit vocabulary — which catches
    mislabeled/duplicate headers (e.g. a 2nd 'HVAC Systems' column that actually holds
    Optimizer/Periscope = Fit). Falls back to header-name variants for fresh uploads
    where the columns are still blank. Returns (hvac_col, fit_col); either may be None."""
    hvac_vocab = {s.lower() for s in HVAC_SYSTEMS}
    fit_vocab = {s.lower() for s in FIT_OPTIONS}

    def content_score(col, vocab):
        vals = [str(v) for v in df[col].dropna().tolist() if str(v).strip()]
        if not vals:
            return 0.0
        hits = sum(1 for v in vals
                   if any(p.strip().lower() in vocab for p in re.split(r"[,/;]", v)))
        return hits / len(vals)

    def best_by_content(vocab, exclude):
        best, col = 0.4, None
        for c in df.columns:
            if c in exclude:
                continue
            s = content_score(c, vocab)
            if s > best:
                best, col = s, c
        return col

    fit_col = best_by_content(fit_vocab, exclude=set())
    hvac_col = best_by_content(hvac_vocab, exclude={fit_col} if fit_col else set())
    if hvac_col is None:
        hvac_col = _first_present(df.columns, HVAC_SYSTEM_COL_NAMES)
    if fit_col is None:
        fit_col = _first_present(df.columns, FIT_COL_NAMES)
        if fit_col == hvac_col:
            fit_col = None
    return hvac_col, fit_col


def _lean_table(results):
    """No uploaded sheet (addresses only): minimal table = address + review columns."""
    headers = ["Property Address", CANON_HVAC, CANON_FIT, CANON_NOTES, CANON_ID]
    rows = [[r.get("address", ""), "", "", "", i] for i, r in enumerate(results, 1)]
    return headers, rows


def _table_from_df(df):
    """Preserve EVERY uploaded column; ensure HVAC Systems / Fit / Notes exist (reuse
    if already present) and stamp a hidden Row_ID = 1-based row index. Returns
    (headers, rows) as plain strings for the sheet. No AI columns are added."""
    df = df.copy()
    hvac_col, fit_col = _detect_hvac_fit_columns(df)
    # Normalize the detected HVAC/Fit columns to canonical names (dedupes messy or
    # duplicate headers); drop any stray same-named canonical column first.
    for canon, detected in ((CANON_HVAC, hvac_col), (CANON_FIT, fit_col)):
        if detected is not None and detected != canon:
            df = df.drop(columns=[c for c in df.columns if c == canon])
            df = df.rename(columns={detected: canon})
    for col in (CANON_HVAC, CANON_FIT, CANON_NOTES):
        if col not in df.columns:
            df[col] = ""
    df[CANON_ID] = range(1, len(df) + 1)
    headers = [str(c) for c in df.columns]

    def _cell(v):
        if v == "" or v is None:
            return ""
        if isinstance(v, float) and v.is_integer():  # 12.0 -> "12", not "12.0"
            return str(int(v))
        return str(v)

    filled = df.where(df.notna(), "")
    rows = [[_cell(v) for v in rec] for rec in filled.values.tolist()]
    return headers, rows


def _finalize_batch(results, title, headers=None, rows=None):
    """Stamp row ids and persist the batch for /review, storing the ORIGINAL uploaded
    table (all the user's columns). When a Sheets service-account credential is
    configured, the output Google Sheet is created HERE, up front — each review
    Submit then writes straight into it (/api/review). Without the credential,
    sheet_url stays "" and the operator/skill flow via GET /api/batch is the
    fallback. Sheet-creation failure is logged, not fatal: the batch and review
    page must survive a Sheets outage. Returns (batch_id, review_url, sheet_url)."""
    for i, e in enumerate(results, 1):
        # Normalize BOTH ids to the 1-based input position: worker-path entries
        # carry the pandas index label as "i" (0-based, gappy after dropna), and
        # the review page matches sheet rows by this id against the sheet's
        # 1-based hidden Row_ID column.
        e["row_id"] = i
        e["i"] = i
    batch_id = f"b-{uuid.uuid4().hex[:10]}"
    if headers is None:
        headers, rows = _lean_table(results)
    base = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    review_url = f"{base}/review/{batch_id}" if base else f"/review/{batch_id}"
    sheet_url = ""
    if sheets_writer.enabled():
        try:
            sheet_url = sheets_writer.create_batch_sheet(
                title, headers, rows, HVAC_SYSTEMS + [NONE_OPTION], FIT_OPTIONS,
                review_url=review_url if base else "")
        except Exception as e:
            log.error("Sheet creation failed for %s: %s", batch_id, e, exc_info=True)
    review_store.save_batch(batch_id, title, results, sheet_url=sheet_url,
                            table_headers=headers, table_rows=rows)
    log.info("Batch %s finalized: %d rows, review %s, sheet %s",
             batch_id, len(results), review_url, sheet_url or "(none)")
    return batch_id, review_url, sheet_url


@api.route("/run", methods=["POST"])
@require_key
def run():
    """Batch endpoint: analyze a whole list of addresses and return the finished
    self-contained HTML audit report. This is the single Render call the n8n
    workflow makes (Sheet -> normalize -> /api/run -> email), so n8n doesn't have
    to loop per address. Body: {addresses: [str | {address,boro_area,zip}], title?}.
    Returns text/html."""
    body = request.get_json(silent=True) or {}
    addresses = body.get("addresses")
    if not isinstance(addresses, list) or not addresses:
        return jsonify({"error": "'addresses' must be a non-empty list"}), 400
    title = (body.get("title") or "Cooling Tower Analysis").strip()

    results = []
    total = len(addresses)
    for item in addresses:
        addr, boro, zc = _coerce_address_item(item)
        if not addr:
            continue
        results.append(_analyze_one(addr, boro, zc, total=total))

    batch_id, review_url, sheet_url = _finalize_batch(results, title)
    html_str = build_audit_report(results, title=title)
    response = Response(html_str, mimetype="text/html")
    response.headers["X-Address-Count"] = str(total)
    response.headers["X-Review-URL"] = review_url
    if sheet_url:
        response.headers["X-Sheet-URL"] = sheet_url
    return response


@api.route("/run-file", methods=["POST"])
@require_key
def run_file():
    """Analyze an uploaded Excel/CSV file and return the finished audit report.

    Intended for n8n Form/Webhook upload flows: n8n receives the file, forwards it
    as multipart/form-data field `file`, then emails this endpoint's HTML body.
    """
    uploaded = request.files.get("file") or request.files.get("data")
    if not uploaded or uploaded.filename == "":
        return jsonify({"error": "missing uploaded file field named 'file'"}), 400

    try:
        df, addr_col, boro_col, zip_col = _read_uploaded_dataframe(uploaded)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    title = (request.form.get("title") or Path(uploaded.filename).stem or "Cooling Tower Analysis").strip()
    results = []
    total = len(df)
    for _, row in df.iterrows():
        addr = str(row.get(addr_col) or "").strip()
        if not addr or addr.lower() == "nan":
            results.append(_build_web_entry(
                full_address="", verdict="", consensus_dict=None, detection_count=0,
                construction=False, notes="(no address in row)",
                original_url=None, result_url=None))
            continue
        boro = str(row.get(boro_col)).strip() if boro_col and pd.notna(row.get(boro_col)) else None
        zc = str(row.get(zip_col)).strip() if zip_col and pd.notna(row.get(zip_col)) else None
        results.append(_analyze_one(addr, boro, zc, total=total))

    headers, rows = _table_from_df(df)
    batch_id, review_url, sheet_url = _finalize_batch(results, title, headers, rows)
    resp = jsonify({"ok": True, "count": total, "review_url": review_url, "sheet_url": sheet_url})
    resp.headers["X-Review-URL"] = review_url
    if sheet_url:
        resp.headers["X-Sheet-URL"] = sheet_url
    resp.headers["X-Uploaded-Filename"] = uploaded.filename
    return resp


@api.route("/batches", methods=["GET"])
@require_key
def batches():
    """List persisted batches (newest first) with review URLs — the recovery path
    when an /api/run response never reaches the client."""
    base = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    out = review_store.list_batches()
    for b in out:
        b["review_url"] = (f"{base}/review/{b['batch_id']}" if base
                           else f"/review/{b['batch_id']}")
    return jsonify({"batches": out})


@api.route("/batch/<batch_id>", methods=["GET"])
@require_key
def batch(batch_id):
    """Operator/skill endpoint: return the ORIGINAL uploaded table (all the user's
    columns) plus the human review decisions recorded so far, so the operator can
    assemble the finished Google Sheet from the human HVAC/Fit picks. No AI columns."""
    b = review_store.load_batch(batch_id)
    if not b:
        return jsonify({"error": "batch not found"}), 404
    decisions = review_store.decisions_for(batch_id)
    return jsonify({
        "title": b.get("title", ""),
        "headers": b.get("table_headers", []),
        "rows": b.get("table_rows", []),
        "decisions": decisions,
        "count": len(b.get("table_rows", [])),
        "reviewed": len(decisions),
    })


def _entry_failures(b):
    """Rows that failed analysis: pipeline errors plus run-file's no-address
    placeholders. needs_review rows are NOT failures — they're the review
    page's job — but /rerun accepts explicit row_ids for them."""
    out = []
    for e in b.get("entries", []):
        err = e.get("error") or ""
        no_addr = "(no address in row)" in (e.get("notes") or "")
        if err or no_addr:
            out.append({
                "row_id": e.get("row_id") if e.get("row_id") is not None else e.get("i"),
                "address": e.get("address", ""),
                "error": err or "No Address",
                "verdict": e.get("verdict", ""),
                "notes": e.get("notes", ""),
                "has_image": bool(e.get("result_image_url")),
            })
    return out


@api.route("/batch/<batch_id>/failures", methods=["GET"])
@require_key
def batch_failures(batch_id):
    """Cleanup-skill endpoint: list the rows that failed analysis (imagery /
    geocode / analyzer errors, no-address placeholders) for diagnosis."""
    b = review_store.load_batch(batch_id)
    if not b:
        return jsonify({"error": "batch not found"}), 404
    failures = _entry_failures(b)
    return jsonify({"batch_id": batch_id, "count": len(failures), "failures": failures})


@api.route("/batch/<batch_id>/rerun", methods=["POST"])
@require_key
def batch_rerun(batch_id):
    """Re-run rows in place and merge the fresh results into the batch, so the
    existing /review page and Google Sheet reflect them on reload. Body:
    {rows: [{row_id, address?}, ...]} — an address value overrides the stored
    one (the cleanup skill's fix for geocode misses); with no rows given, every
    currently-failed row is re-run as-is. Replaced entries drop any prior human
    decision (it was made against the failed result). When an address was
    overridden and the batch has a live sheet, the sheet's address cell is
    updated too. Not safe to run concurrently with active reviewing of the SAME
    rows (last write wins on the batch JSON)."""
    b = review_store.load_batch(batch_id)
    if not b:
        return jsonify({"error": "batch not found"}), 404
    body = request.get_json(silent=True) or {}
    rows_req = body.get("rows") or [{"row_id": f["row_id"]} for f in _entry_failures(b)]
    if not rows_req:
        return jsonify({"ok": True, "batch_id": batch_id, "rerun": [],
                        "remaining_failures": 0})

    by_rid = {review_store._rid(e): e for e in b.get("entries", [])}
    headers = b.get("table_headers", [])
    addr_header = _first_present(headers, ADDRESS_COLUMNS)
    fresh, statuses, table_updates = {}, [], {}
    for r in rows_req:
        rid = str(r.get("row_id", "")).strip()
        old = by_rid.get(rid)
        if not old:
            statuses.append({"row_id": rid, "status": "unknown_row"})
            continue
        addr = (r.get("address") or old.get("address") or "").strip()
        if not addr:
            statuses.append({"row_id": rid, "status": "no_address"})
            continue
        entry = _analyze_one(addr, total=len(rows_req))
        entry["row_id"] = old.get("row_id")
        if old.get("i") is not None:
            entry["i"] = old.get("i")
        fresh[rid] = entry
        statuses.append({"row_id": rid, "status": "ok",
                         "verdict": entry.get("verdict", ""),
                         "error": entry.get("error") or ""})
        if (r.get("address") or "").strip() and addr_header:
            table_updates[rid] = {addr_header: addr}

    if fresh:
        entries = b.get("entries", [])
        for idx, e in enumerate(entries):
            rid = review_store._rid(e)
            if rid in fresh:
                entries[idx] = fresh[rid]
        if table_updates and CANON_ID in headers:
            idc = headers.index(CANON_ID)
            for row in b.get("table_rows", []):
                rid = str(row[idc]).strip() if idc < len(row) else ""
                if rid in table_updates:
                    for h, v in table_updates[rid].items():
                        row[headers.index(h)] = v
        review_store.save_raw(batch_id, b)
        if b.get("sheet_url") and sheets_writer.enabled():
            for rid, upd in table_updates.items():
                try:
                    sheets_writer.write_row_values(
                        b["sheet_url"], headers, b.get("table_rows", []), rid, upd)
                except Exception as e:
                    log.error("Sheet address update failed for %s/%s: %s", batch_id, rid, e)

    remaining = len(_entry_failures(b))
    return jsonify({"ok": True, "batch_id": batch_id, "rerun": statuses,
                    "remaining_failures": remaining})


@api.route("/report", methods=["POST"])
@require_key
def report():
    """Assemble a self-contained HTML audit report from collected web_entry rows."""
    body = request.get_json(silent=True) or {}
    results = body.get("results")
    if not isinstance(results, list):
        return jsonify({"error": "'results' must be a list of analyze entries"}), 400
    title = (body.get("title") or "Cooling Tower Analysis").strip()
    html_str = build_audit_report(results, title=title)
    return Response(html_str, mimetype="text/html")
