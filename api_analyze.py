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
import csv
import hmac
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
from werkzeug.utils import secure_filename

from job_queue import batch_size_limit, reserve_usage_limit
from tasks_local import (_process_one_address, _build_web_entry, ADDRESS_VARIANTS,
                         _pick_excel_tab)
from report_audit import build_audit_report
from review_render import HVAC_SYSTEMS, NONE_OPTION, FIT_OPTIONS, LEGACY_FIT_VALUES
import review_store
import sheets_writer
import intake_resolver
import workbook_runs

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


class MappingConfirmationRequired(ValueError):
    """An automated file source was intentionally stopped before analysis."""

    def __init__(self, resolution):
        super().__init__(resolution.get("reason") or "address-column mapping needs confirmation")
        self.resolution = resolution


def _file_row_limit() -> int:
    """Honor the legacy file limit without allowing it above the global batch cap."""
    try:
        configured = max(1, int(os.environ.get("ANALYZE_FILE_MAX_ROWS", batch_size_limit())))
    except ValueError:
        configured = batch_size_limit()
    return min(configured, batch_size_limit())


def _batch_limit_error(count: int):
    limit = batch_size_limit()
    if count > limit:
        return jsonify({
            "error": f"Batch has {count} rows, above the safety limit of {limit}.",
            "max_batch_rows": limit,
        }), 400
    return None


def _reserve_api_quota(address_count: int):
    """Reserve capacity before an in-request endpoint starts paid analysis."""
    allowed, current_usage, message = reserve_usage_limit(address_count)
    if not allowed:
        return jsonify({
            "error": message,
            "current_usage": current_usage,
            "retryable": False,
        }), 429
    return None


def _entry_is_failure(entry: dict) -> bool:
    return bool(entry.get("error") or "(no address in row)" in (entry.get("notes") or ""))


def _result_counts(results: list) -> tuple[int, int]:
    failed = sum(1 for entry in results if _entry_is_failure(entry))
    return len(results) - failed, failed


def _workbook_api_payload(run):
    summary = workbook_runs.public_summary(run)
    tabs = summary.pop("tabs")
    base = (
        os.environ.get("APP_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")
    run_id = summary["run_id"]
    return {
        **summary,
        "tab_inventory": tabs,
        "address_count": summary["row_count"],
        "status_url": f"{base}/api/v2/workbook-runs/{run_id}",
        "approval_url": f"{base}/api/v2/workbook-runs/{run_id}/approval",
        "confirmation_url": f"{base}/api/v2/workbook-runs/{run_id}/confirmation",
        "retry_url": f"{base}/api/v2/workbook-runs/{run_id}/retry",
        "setup_url": f"{base}/workbook/{run_id}/setup",
    }


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
        if not provided or not hmac.compare_digest(provided, configured):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


@api.route("/health")
def health():
    required = ["GOOGLE_MAPS_API_KEY", "MAPBOX_API_KEY", "GEMINI_API_KEY", "ANALYZE_API_KEY"]
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    # This endpoint is Render's liveness probe, so keep it HTTP 200. The body
    # tells operators whether a live process is actually ready to analyze.
    try:
        from vlm import metrics_snapshot as _vlm_metrics_snapshot
        vlm_metrics = _vlm_metrics_snapshot()
    except Exception:  # Health must remain a liveness probe if optional VLM deps are unavailable.
        vlm_metrics = {}
    return jsonify({
        "status": "ok" if not missing else "degraded",
        "pipeline_ready": not missing,
        "missing_configuration": missing,
        "grok_exception_paths_available": bool(os.environ.get("XAI_API_KEY", "").strip()),
        "intake_metrics": intake_resolver.metrics_snapshot(),
        "review_metrics": vlm_metrics,
        "workbook_metrics": workbook_runs.metrics_snapshot(),
        "multi_tab_workbook_enabled": workbook_runs.enabled(),
        "max_batch_rows": batch_size_limit(),
    })


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
            # Restrict delimiter detection to actual CSV-like separators. Pandas'
            # unrestricted ``sep=None`` can mistake a character in a one-column
            # header (for example the "s" in "Address") for the delimiter.
            with open(path, "r", encoding=encoding, newline="") as f:
                sample = f.read(4096)
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
            return pd.read_csv(path, encoding=encoding, sep=delimiter)
        except Exception as e:
            last_error = e
    raise last_error


def _read_excel_with_worker_tab_selection(path, suffix):
    """Use the exact same leftmost-address-tab rule as queued browser jobs."""
    engine = "openpyxl" if suffix == ".xlsx" else None
    all_sheets = pd.read_excel(path, sheet_name=None, engine=engine)
    _tab, df = _pick_excel_tab(all_sheets)
    if df is None:
        raise ValueError("Excel file has no non-empty worksheet")
    return df


def _read_excel_tabs(path, suffix):
    """Return non-empty workbook tabs in source order for shared intake resolution."""
    engine = "openpyxl" if suffix == ".xlsx" else None
    all_sheets = pd.read_excel(path, sheet_name=None, engine=engine)
    tabs = [(str(name), df) for name, df in all_sheets.items()
            if df is not None and len(df.columns) and len(df)]
    if not tabs:
        raise ValueError("Excel file has no non-empty worksheet")
    return tabs


def _resolver_tab_from_dataframe(df, tab="Uploaded file"):
    headers = [str(column).strip() for column in df.columns]
    rows = []
    for values in df.head(intake_resolver.sample_row_limit()).itertuples(index=False, name=None):
        rows.append(["" if pd.isna(value) else str(value).strip() for value in values])
    return {"tab": tab, "headers": headers, "rows": rows}


def _resolve_dataframe_columns(df):
    """Return an analysis copy, preserving the original frame for output Sheets."""
    return _resolve_dataframe_tabs([("Uploaded file", df)])


def _resolve_dataframe_tabs(frames):
    """Resolve a selected workbook tab and its address columns once, safely."""
    resolver_tabs = [_resolver_tab_from_dataframe(df, tab=name) for name, df in frames]
    resolution = intake_resolver.resolve_schema(resolver_tabs, ADDRESS_VARIANTS)
    if resolution["status"] == "deterministic":
        mapping = resolution["mapping"]
        selected = next((df for name, df in frames if str(name) == mapping["tab"]), None)
        if selected is None:
            raise MappingConfirmationRequired({"reason": "Selected workbook tab disappeared"})
        return (selected, mapping["address_column"], mapping.get("city_column"),
                mapping.get("zip_column"))
    if resolution["status"] != "suggested" or not resolution.get("auto_approved"):
        raise MappingConfirmationRequired(resolution)

    mapping = resolution["mapping"]
    df = next((frame for name, frame in frames if str(name) == mapping["tab"]), None)
    if df is None:
        raise MappingConfirmationRequired({"reason": "Suggested workbook tab disappeared"})
    # Compose all rows, not only the bounded Grok sample. This field is internal
    # and the original DataFrame remains attached for the created result Sheet.
    all_rows = [["" if pd.isna(value) else str(value).strip() for value in values]
                for values in df.itertuples(index=False, name=None)]
    all_addresses = intake_resolver.compose_addresses(list(df.columns), all_rows, mapping)
    internal_col = "__parity_analysis_address"
    analysis_df = df.copy()
    analysis_df[internal_col] = all_addresses
    analysis_df.attrs["parity_source_df"] = df
    analysis_df.attrs["parity_intake_mapping"] = mapping
    # The internal address already contains city/state/ZIP.  Returning source
    # columns here would make tasks_local append them a second time.
    return analysis_df, internal_col, None, None


def _read_uploaded_address_file(uploaded_file):
    """Read uploaded CSV/XLS/XLSX and return /api/run-compatible address items."""
    filename = uploaded_file.filename or "addresses"
    suffix = Path(filename).suffix.lower()
    max_rows = _file_row_limit()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".xlsx") as tmp:
        uploaded_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        try:
            if suffix in {".xlsx", ".xls"}:
                frames = _read_excel_tabs(tmp_path, suffix)
            elif suffix == ".csv":
                frames = [("Uploaded file", _read_csv_with_fallback(tmp_path))]
            elif not suffix:
                try:
                    frames = _read_excel_tabs(tmp_path, ".xlsx")
                except Exception:
                    frames = [("Uploaded file", _read_csv_with_fallback(tmp_path))]
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

    if not frames:
        raise ValueError("Uploaded file has no rows")

    # Legacy helper retained for callers outside /api/run-file. It predates
    # multi-tab mapping and continues to use the first non-empty tab.
    df = frames[0][1]

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
    max_rows = _file_row_limit()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".xlsx") as tmp:
        uploaded_file.save(tmp.name)
        tmp_path = tmp.name
    try:
        try:
            if suffix in {".xlsx", ".xls"}:
                frames = _read_excel_tabs(tmp_path, suffix)
            elif suffix == ".csv":
                frames = [("Uploaded file", _read_csv_with_fallback(tmp_path))]
            elif not suffix:
                try:
                    frames = _read_excel_tabs(tmp_path, ".xlsx")
                except Exception:
                    frames = [("Uploaded file", _read_csv_with_fallback(tmp_path))]
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
    if not frames:
        raise ValueError("Uploaded file has no rows")
    if any(len(df) > max_rows for _name, df in frames):
        raise ValueError(
            f"Uploaded file has more than {max_rows} rows in a worksheet, above the safety limit. "
            "Split the file or raise ANALYZE_FILE_MAX_ROWS on the server.")
    return _resolve_dataframe_tabs(frames)


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
    quota_error = _reserve_api_quota(1)
    if quota_error:
        return quota_error
    return jsonify(_analyze_one(address, body.get("boro_area"), body.get("zip")))


CANON_HVAC, CANON_NOTES, CANON_ID = "HVAC Systems", "Notes", "Row_ID"
CANON_ADDR = "Property Address"
# THE output sheet format (2026-07-13 directive): every batch sheet has exactly
# these columns in this order, matching the team's TAM sheet. Upload columns are
# mapped in by synonym; anything unrecognized is left blank — "we only care
# about the ones we fill out" (Optimizer/Periscope Fit, HVAC Systems, Notes).
CANONICAL_COLUMNS = [
    "Optimizer Fit", "Periscope Fit", CANON_HVAC, CANON_NOTES,
    "Property Name", CANON_ADDR, "City", "Market Name", "Units", "Stories",
    "Total Buildings", "Year Built", "Year Renovated", "Property Type",
    "Secondary Type", "Affordable Type", "True Owner Name",
    "Recorded Owner Name", "Property Manager Name",
]
# Lower-cased upload-header synonyms for each canonical column. The canonical
# name itself always matches; HVAC and the address column get special handling.
COLUMN_SYNONYMS = {
    "Optimizer Fit": ["optimizer", "optimizer fit?"],
    "Periscope Fit": ["periscope", "periscope fit?"],
    CANON_NOTES: ["note", "comments", "comment"],
    "Property Name": ["building name", "name", "property"],
    "City": [],
    "Market Name": ["submarket", "submarket name", "market"],
    "Units": ["# of units", "unit count", "number of units"],
    "Stories": ["floors", "# of stories"],
    "Total Buildings": ["buildings", "# of buildings", "number of buildings"],
    "Year Built": ["built"],
    "Year Renovated": ["renovated", "year renov"],
    "Property Type": ["primary property type", "type"],
    "Secondary Type": ["secondary property type"],
    "Affordable Type": [],
    "True Owner Name": ["owner", "owner name", "true owner"],
    "Recorded Owner Name": ["recorded owner"],
    "Property Manager Name": ["property manager", "property manager name", "pm name", "manager"],
}
# Header-name variants for locating the HVAC / legacy-Fit columns in an
# arbitrary upload (fallback when the columns are blank; content detection
# below handles filled ones).
HVAC_SYSTEM_COL_NAMES = ["HVAC Systems", "HVAC System", "HVAC", "HVAC Equipment", "HVAC Type"]
FIT_COL_NAMES = ["Fit", "Fit Type", "Product Fit"]


def _detect_hvac_fit_columns(df):
    """Find the HVAC Systems and Fit columns in ANY uploaded format. Primary signal is
    CONTENT — a column whose values match the HVAC or Fit vocabulary — which catches
    mislabeled/duplicate headers (e.g. a 2nd 'HVAC Systems' column that actually holds
    Optimizer/Periscope = Fit). Falls back to header-name variants for fresh uploads
    where the columns are still blank. Returns (hvac_col, fit_col); either may be None."""
    hvac_vocab = {s.lower() for s in HVAC_SYSTEMS}
    # Legacy single-Fit vocabulary (Optimizer/Periscope/...) plus the current
    # Good/Bad/Not Sure — so a filled fit column in ANY vintage of re-uploaded
    # sheet is recognized and never mistaken for the HVAC column.
    fit_vocab = {s.lower() for s in LEGACY_FIT_VALUES + FIT_OPTIONS}

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


def _cell(v):
    if v == "" or v is None:
        return ""
    if isinstance(v, float) and v.is_integer():  # 12.0 -> "12", not "12.0"
        return str(int(v))
    return str(v)


def _norm_header(h):
    return re.sub(r"\s+", " ", str(h)).strip().lower()


def _lean_table(results):
    """No uploaded sheet (addresses only): the canonical format with just the
    address filled in."""
    headers = CANONICAL_COLUMNS + [CANON_ID]
    ai = CANONICAL_COLUMNS.index(CANON_ADDR)
    rows = []
    for i, r in enumerate(results, 1):
        row = [""] * len(CANONICAL_COLUMNS) + [str(i)]
        row[ai] = r.get("address", "")
        rows.append(row)
    return headers, rows


def _table_from_df(df):
    """Map ANY upload into THE canonical sheet format: exactly CANONICAL_COLUMNS
    in order (+ hidden Row_ID), regardless of what was uploaded. Recognized
    upload columns (by synonym, or by content for HVAC) carry their values
    over; unrecognized ones are dropped; missing ones come out blank. A legacy
    single-Fit column (Optimizer/Periscope values) is detected only so it is
    never mistaken for HVAC — its values are not carried."""
    hvac_col, fit_col = _detect_hvac_fit_columns(df)
    norm_cols = {}
    for c in df.columns:
        norm_cols.setdefault(_norm_header(c), c)

    # fit_col is NOT excluded here: a column actually named "Optimizer Fit" /
    # "Periscope" maps by name below; only a fit-content column with no
    # recognizable name (legacy "Fit", "HVAC Systems.1") drops out naturally.
    used = {hvac_col} if hvac_col is not None else set()
    mapping = {}
    if hvac_col is not None:
        mapping[CANON_HVAC] = hvac_col
    addr_norms = [v.lower() for v in ADDRESS_VARIANTS]
    for canon in CANONICAL_COLUMNS:
        if canon in mapping:
            continue
        candidates = [_norm_header(canon)]
        if canon == CANON_ADDR:
            candidates += addr_norms
        candidates += COLUMN_SYNONYMS.get(canon, [])
        for name in candidates:
            src = norm_cols.get(name)
            if src is not None and src not in used:
                mapping[canon] = src
                used.add(src)
                break

    n = len(df)
    filled = df.where(df.notna(), "")
    cols_out = {}
    for canon in CANONICAL_COLUMNS:
        src = mapping.get(canon)
        vals = filled[src].tolist() if src is not None else [""] * n
        cols_out[canon] = [_cell(v) for v in vals]
    headers = CANONICAL_COLUMNS + [CANON_ID]
    rows = [[cols_out[c][i] for c in CANONICAL_COLUMNS] + [str(i + 1)]
            for i in range(n)]
    return headers, rows


def _finalize_batch(
    results, title, headers=None, rows=None, binding=None, *,
    batch_id=None, sheet_bindings=None, tab_inventory=None, run_id=None,
):
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
        if e.get("row_id") is None:
            e["row_id"] = i
        if e.get("i") is None:
            e["i"] = e["row_id"]
    batch_id = batch_id or f"b-{uuid.uuid4().hex[:10]}"
    if headers is None:
        headers, rows = _lean_table(results)
    base = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    review_url = f"{base}/review/{batch_id}" if base else f"/review/{batch_id}"
    sheet_url = ""
    if sheet_bindings and sheets_writer.enabled():
        prepared = []
        for multi_binding in sheet_bindings:
            try:
                required = {
                    sheets_writer.HVAC_COL,
                    sheets_writer.OPT_FIT_COL,
                    sheets_writer.PERI_FIT_COL,
                    sheets_writer.NOTES_COL,
                }
                if not required.issubset(
                    set((multi_binding.get("colmap") or {}).keys())
                ):
                    sheets_writer.ensure_review_columns(
                        multi_binding, multi_binding.get("headers", []),
                        HVAC_SYSTEMS + [NONE_OPTION], FIT_OPTIONS,
                    )
                multi_binding.pop("writeback_error", None)
            except Exception as e:
                log.error(
                    "Bound workbook tab setup failed for %s/%s: %s",
                    batch_id, multi_binding.get("tab", ""), e, exc_info=True,
                )
                multi_binding["writeback_error"] = str(e)
            prepared.append(multi_binding)
        sheet_bindings = prepared
        if prepared:
            sid = prepared[0]["spreadsheet_id"]
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    elif binding and sheets_writer.enabled():
        # Run-in-place: no new sheet — add the fill-out columns + dropdowns to
        # the team's own sheet and write decisions back into it.
        try:
            sheets_writer.ensure_review_columns(
                binding, binding.get("headers", []), HVAC_SYSTEMS + [NONE_OPTION],
                FIT_OPTIONS, review_url=review_url if base else "")
            sheet_url = binding["sheet_url"]
        except Exception as e:
            log.error("Bound-sheet setup failed for %s: %s", batch_id, e, exc_info=True)
            binding = None  # fall back to review-page-only; decisions stay local
    elif sheets_writer.enabled():
        try:
            sheet_url = sheets_writer.create_batch_sheet(
                title, headers, rows, HVAC_SYSTEMS + [NONE_OPTION], FIT_OPTIONS,
                review_url=review_url if base else "")
        except Exception as e:
            log.error("Sheet creation failed for %s: %s", batch_id, e, exc_info=True)
    review_store.save_batch(batch_id, title, results, sheet_url=sheet_url,
                            table_headers=headers, table_rows=rows,
                            sheet_binding=binding,
                            sheet_bindings=sheet_bindings,
                            tab_inventory=tab_inventory,
                            schema_version=2 if sheet_bindings else 1,
                            run_id=run_id)
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
    limit_error = _batch_limit_error(len(addresses))
    if limit_error:
        return limit_error
    title = (body.get("title") or "Cooling Tower Analysis").strip()

    usable_addresses = []
    for item in addresses:
        addr, boro, zc = _coerce_address_item(item)
        if addr:
            usable_addresses.append((addr, boro, zc))
    if not usable_addresses:
        return jsonify({"error": "'addresses' contains no usable addresses"}), 400
    quota_error = _reserve_api_quota(len(usable_addresses))
    if quota_error:
        return quota_error

    results = []
    total = len(usable_addresses)
    for addr, boro, zc in usable_addresses:
        results.append(_analyze_one(addr, boro, zc, total=total))

    batch_id, review_url, sheet_url = _finalize_batch(results, title)
    html_str = build_audit_report(results, title=title)
    successful, failed = _result_counts(results)
    response = Response(html_str, mimetype="text/html")
    response.headers["X-Address-Count"] = str(total)
    response.headers["X-Success-Count"] = str(successful)
    response.headers["X-Failure-Count"] = str(failed)
    response.headers["X-Completion-Status"] = "completed_with_errors" if failed else "completed"
    response.headers["X-Review-URL"] = review_url
    if sheet_url:
        response.headers["X-Sheet-URL"] = sheet_url
    return response


@api.route("/workbook-runs", methods=["GET"])
@api.route("/v2/workbook-runs", methods=["GET"])
@require_key
def workbook_run_list():
    if not workbook_runs.surface_enabled("api", default=False):
        return jsonify({"error": "multi-tab workbook processing is disabled"}), 503
    return jsonify({"runs": workbook_runs.list_runs()})


@api.route("/workbook-runs", methods=["POST"])
@api.route("/v2/workbook-runs", methods=["POST"])
@require_key
def workbook_run_create():
    """Create one asynchronous, fail-closed workbook run."""
    if not workbook_runs.surface_enabled("api", default=False):
        return jsonify({"error": "multi-tab workbook processing is disabled"}), 503
    uploaded = request.files.get("file") or request.files.get("data")
    sheet_url = (request.form.get("sheet_url") or "").strip()
    if not sheet_url and request.is_json:
        sheet_url = str((request.get_json(silent=True) or {}).get("sheet_url") or "").strip()
    try:
        if sheet_url:
            run = workbook_runs.prepare_google_sheet(
                sheet_url, source_kind="api_sheet",
            )
        elif uploaded and uploaded.filename:
            filename = secure_filename(uploaded.filename) or "workbook"
            suffix = Path(filename).suffix.lower()
            if suffix == ".xls":
                return jsonify({
                    "error": "Save the legacy Excel workbook as .xlsx and upload it again."
                }), 400
            if suffix not in {".xlsx", ".csv"}:
                return jsonify({"error": "Upload must be an .xlsx or .csv file"}), 400
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                uploaded.save(handle.name)
                temp_path = handle.name
            try:
                run = workbook_runs.prepare_local_file(
                    temp_path, filename, source_kind="api",
                )
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        else:
            return jsonify({
                "error": "Provide multipart field 'file' or JSON/form field 'sheet_url'"
            }), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.error("Workbook run creation failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 409
    code = 409 if run.get("confirmation_required") or run.get("preflight_failed") else 202
    return jsonify(_workbook_api_payload(run)), code


@api.route("/workbook-runs/<run_id>", methods=["GET"])
@api.route("/v2/workbook-runs/<run_id>", methods=["GET"])
@require_key
def workbook_run_status(run_id):
    run = workbook_runs.load_run(run_id)
    if not run:
        return jsonify({"error": "workbook run not found"}), 404
    return jsonify(_workbook_api_payload(run))


@api.route("/workbook-runs/<run_id>/confirmation", methods=["POST"])
@api.route("/v2/workbook-runs/<run_id>/confirmation", methods=["POST"])
@require_key
def workbook_run_confirmation(run_id):
    body = request.get_json(silent=True) or {}
    selections = body.get("tabs")
    if not isinstance(selections, list):
        return jsonify({"error": "'tabs' must be a list of confirmed mappings"}), 400
    try:
        run = workbook_runs.confirm_mappings(run_id, selections)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    try:
        from drive_inbox import clear_pending_run
        clear_pending_run(run_id)
    except Exception:
        log.warning("Could not clear Drive pending marker for %s", run_id)
    code = 409 if run.get("preflight_failed") else 202
    return jsonify(_workbook_api_payload(run)), code


@api.route("/workbook-runs/<run_id>/approval", methods=["POST"])
@api.route("/v2/workbook-runs/<run_id>/approval", methods=["POST"])
@require_key
def workbook_run_approval(run_id):
    try:
        run = workbook_runs.approve(run_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    try:
        from drive_inbox import clear_pending_run
        clear_pending_run(run_id)
    except Exception:
        log.warning("Could not clear Drive pending marker for %s", run_id)
    return jsonify(_workbook_api_payload(run)), 202


@api.route("/workbook-runs/<run_id>/retry", methods=["POST"])
@api.route("/v2/workbook-runs/<run_id>/retry", methods=["POST"])
@require_key
def workbook_run_retry(run_id):
    try:
        run = workbook_runs.retry(run_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(_workbook_api_payload(run)), 202


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
    suffix = Path(uploaded.filename or "").suffix.lower()
    if suffix == ".xls":
        return jsonify({
            "error": "Save the legacy Excel workbook as .xlsx and upload it again."
        }), 400
    if suffix == ".xlsx":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as handle:
            uploaded.save(handle.name)
            guard_path = handle.name
        try:
            snapshot = workbook_runs.inspect_xlsx(guard_path)
            resolution = intake_resolver.resolve_workbook_schema(
                snapshot["tabs"], ADDRESS_VARIANTS,
            )
        finally:
            try:
                os.remove(guard_path)
            except OSError:
                pass
        uploaded.stream.seek(0)
        selected = [
            tab for tab in resolution["tabs"] if tab.get("status") == "selected"
        ]
        if resolution["status"] != "ready" or len(selected) > 1:
            return jsonify({
                "error": (
                    "This workbook contains multiple or ambiguous address tabs. "
                    "Use POST /api/v2/workbook-runs so every eligible tab is accounted for."
                ),
                "status": "workbook_endpoint_required",
                "tab_inventory": workbook_runs.public_summary({
                    "tabs": resolution["tabs"],
                })["tabs"],
            }), 409

    try:
        df, addr_col, boro_col, zip_col = _read_uploaded_dataframe(uploaded)
    except MappingConfirmationRequired as e:
        # n8n/API sources can auto-run only a locally validated high-confidence
        # mapping.  Returning a clear non-2xx result prevents a guessed column
        # from ever starting paid address processing.
        return jsonify({
            "ok": False,
            "status": "mapping_confirmation_required",
            "error": str(e),
            "mapping": e.resolution.get("mapping"),
        }), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    usable_addresses = sum(
        1 for value in df[addr_col]
        if pd.notna(value) and str(value).strip() and str(value).strip().lower() != "nan"
    )
    quota_error = _reserve_api_quota(usable_addresses)
    if quota_error:
        return quota_error

    uploaded_name = secure_filename(uploaded.filename or "") or "upload"
    title = (request.form.get("title") or Path(uploaded_name).stem or "Cooling Tower Analysis").strip()
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

    # A Grok-assisted file may have an internal derived address field.  The
    # result Sheet always receives the untouched original table.
    source_df = df.attrs.get("parity_source_df", df)
    headers, rows = _table_from_df(source_df)
    batch_id, review_url, sheet_url = _finalize_batch(results, title, headers, rows)
    successful, failed = _result_counts(results)
    completion_status = "completed_with_errors" if failed else "completed"
    resp = jsonify({
        "ok": True,
        "status": completion_status,
        "count": total,
        "success_count": successful,
        "failure_count": failed,
        "review_url": review_url,
        "sheet_url": sheet_url,
    })
    resp.headers["X-Review-URL"] = review_url
    resp.headers["X-Success-Count"] = str(successful)
    resp.headers["X-Failure-Count"] = str(failed)
    resp.headers["X-Completion-Status"] = completion_status
    if sheet_url:
        resp.headers["X-Sheet-URL"] = sheet_url
    resp.headers["X-Uploaded-Filename"] = uploaded_name
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
        "count": (
            len(b.get("entries", []))
            if int(b.get("schema_version") or 1) >= 2
            else len(b.get("table_rows", []))
        ),
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
    explicit_rows = body.get("rows")
    if explicit_rows is not None and not isinstance(explicit_rows, list):
        return jsonify({"error": "'rows' must be a list when provided"}), 400
    rows_req = explicit_rows or [{"row_id": f["row_id"]} for f in _entry_failures(b)]
    if not rows_req:
        return jsonify({"ok": True, "batch_id": batch_id, "rerun": [],
                        "remaining_failures": 0})
    limit_error = _batch_limit_error(len(rows_req))
    if limit_error:
        return limit_error
    if any(not isinstance(row, dict) for row in rows_req):
        return jsonify({"error": "every item in 'rows' must be an object"}), 400

    by_rid = {review_store._rid(e): e for e in b.get("entries", [])}
    chargeable = 0
    for r in rows_req:
        old = by_rid.get(str(r.get("row_id", "")).strip())
        addr = (r.get("address") or (old or {}).get("address") or "").strip()
        if old and addr:
            chargeable += 1
    quota_error = _reserve_api_quota(chargeable)
    if quota_error:
        return quota_error

    headers = b.get("table_headers", [])
    addr_header = _first_present(headers, ADDRESS_COLUMNS)
    fresh, statuses, table_updates = {}, [], {}
    source_address_updates = {}
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
        for key in (
            "source_key", "source_grid_id", "source_tab", "source_row",
            "analysis_key",
        ):
            if old.get(key) is not None:
                entry[key] = old[key]
        fresh[rid] = entry
        statuses.append({"row_id": rid, "status": "ok",
                         "verdict": entry.get("verdict", ""),
                         "error": entry.get("error") or ""})
        if (r.get("address") or "").strip() and addr_header:
            table_updates[rid] = {addr_header: addr}
            source_address_updates[rid] = addr

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
                    if b.get("sheet_bindings"):
                        source_entry = by_rid.get(rid) or {}
                        source_binding = next(
                            (
                                item for item in b["sheet_bindings"]
                                if (
                                    str(item.get("grid_id"))
                                    == str(source_entry.get("source_grid_id"))
                                    and item.get("tab")
                                    == source_entry.get("source_tab")
                                )
                            ),
                            None,
                        )
                        source_header = (
                            (source_binding or {}).get("intake_mapping") or {}
                        ).get("address_column")
                        if (
                            not source_binding
                            or not source_header
                            or rid not in source_address_updates
                            or not sheets_writer.write_source_values(
                                source_binding,
                                source_entry.get("source_row"),
                                {source_header: source_address_updates[rid]},
                            )
                        ):
                            raise ValueError(
                                "Exact source tab/row address write-back failed"
                            )
                    else:
                        sheets_writer.write_row_values(
                            b["sheet_url"], headers, b.get("table_rows", []),
                            rid, upd,
                        )
                except Exception as e:
                    log.error(
                        "Sheet address update failed for %s/%s: %s",
                        batch_id, rid, e,
                    )

    remaining = len(_entry_failures(b))
    if b.get("run_id"):
        try:
            def complete_human(entry):
                human = entry.get("human") or {}
                return all(str(human.get(key) or "").strip() for key in (
                    "hvac_systems", "optimizer_fit", "periscope_fit",
                ))

            entries = b.get("entries", [])
            workbook_runs.update_review_progress(
                b["run_id"],
                sum(1 for entry in entries if complete_human(entry)),
                len(entries),
                writeback_failures=sum(
                    1 for entry in entries
                    if (entry.get("writeback") or {}).get("status") == "error"
                ),
                needs_attention=sum(
                    1 for entry in entries
                    if _entry_is_failure(entry) and not complete_human(entry)
                ),
            )
        except Exception:
            log.exception("Could not update workbook rerun progress for %s", batch_id)
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
