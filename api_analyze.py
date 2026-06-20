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
import uuid
from functools import wraps

import pandas as pd
from flask import Blueprint, jsonify, request, Response

from tasks_local import _process_one_address, _build_web_entry
from report_audit import build_audit_report

log = logging.getLogger("api")

api = Blueprint("api", __name__, url_prefix="/api")


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

    # Build a one-row frame matching the CSV schema the pipeline expects.
    record = {"Address": address}
    if body.get("boro_area"):
        record["Boro_Area"] = str(body["boro_area"]).strip()
    if body.get("zip"):
        record["Zip"] = str(body["zip"]).strip()
    df = pd.DataFrame([record])
    row = df.iloc[0]
    columns = list(df.columns)

    job_id = f"api-{uuid.uuid4().hex[:8]}"
    sink = Base64Sink()
    try:
        web_entry, _csv_row = _process_one_address(
            row, 0, columns, 1, job_id, sink.upload_file, sink.make_signed_url
        )
    except Exception as e:  # transient footprint / unexpected — surface, don't 500
        log.error("analyze failed for %r: %s", address, e, exc_info=True)
        web_entry = _build_web_entry(
            full_address=address, verdict="", consensus_dict=None,
            detection_count=0, construction=False,
            notes=f"Analyzer error: {e}",
            original_url=None, result_url=None,
            error="Analyzer Error",
        )
    return jsonify(web_entry)


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
