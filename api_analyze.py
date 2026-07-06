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
import tempfile
import uuid
from functools import wraps
from pathlib import Path

import pandas as pd
from flask import Blueprint, jsonify, request, Response

from tasks_local import _process_one_address, _build_web_entry
from report_audit import build_audit_report

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

    html_str = build_audit_report(results, title=title)
    response = Response(html_str, mimetype="text/html")
    response.headers["X-Address-Count"] = str(total)
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
        addresses = _read_uploaded_address_file(uploaded)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    title = (request.form.get("title") or "Cooling Tower Analysis").strip()
    results = []
    total = len(addresses)
    for item in addresses:
        addr, boro, zc = _coerce_address_item(item)
        if not addr:
            continue
        results.append(_analyze_one(addr, boro, zc, total=total))

    html_str = build_audit_report(results, title=title)
    response = Response(html_str, mimetype="text/html")
    response.headers["X-Address-Count"] = str(total)
    response.headers["X-Uploaded-Filename"] = uploaded.filename
    return response


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
