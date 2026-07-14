"""
Flask application for Render deployment.
Uses local storage and SQLite job queue instead of Google Cloud services.
"""
import os
import json
import uuid
import logging
import pandas as pd
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for, jsonify, abort, Response

# Local modules
from job_queue import init_db, enqueue_job, get_job_status, cancel_job, check_usage_limit
from storage_helpers import init_storage, upload_file, get_file_path, read_result, file_exists, read_file
from worker import start_worker
from api_analyze import api as api_blueprint
from review_render import build_review_page
import review_store
import sheets_writer
import requests

# Initialize Flask app
app = Flask(__name__)
app.register_blueprint(api_blueprint)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

# Configuration
UPLOAD_FOLDER = Path(os.getenv('UPLOAD_FOLDER', 'temp_uploads'))
UPLOAD_FOLDER.mkdir(exist_ok=True)

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

@app.route('/upload', methods=['POST'])
def upload():
    """Handle CSV upload and enqueue processing job."""
    if 'file' not in request.files:
        return redirect(request.url)

    f = request.files['file']
    if not f or f.filename == '':
        return redirect(request.url)

    filename = f.filename
    local_path = UPLOAD_FOLDER / filename
    f.save(local_path)

    # Count rows for progress tracking
    try:
        df = pd.read_csv(local_path)
        total = len(df)
    except Exception as e:
        log.warning(f"Could not count CSV rows: {e}")
        total = 0

    # Check monthly usage limit
    can_process, current_usage, error_msg = check_usage_limit(total)
    if not can_process:
        log.warning(f"Usage limit check failed: {error_msg}")
        local_path.unlink(missing_ok=True)  # Clean up uploaded file
        return render_template('error.html',
                             error_title="Monthly Limit Reached",
                             error_message=error_msg,
                             current_usage=current_usage), 403

    # Create job ID
    job_id = str(uuid.uuid4())

    # Upload CSV to storage
    csv_blob_path = f"uploads/{job_id}/{filename}"
    upload_file(str(local_path), csv_blob_path)

    # Enqueue job for background processing
    try:
        enqueue_job(job_id, {
            "job_id": job_id,
            "csv_path": csv_blob_path,
            "total": total
        })
        log.info(f"Job {job_id} enqueued with {total} addresses")
    except Exception as e:
        log.error(f"Failed to enqueue job: {e}", exc_info=True)
        return f"Error enqueuing job: {e}", 500

    return redirect(url_for('results', job_id=job_id, total=total))


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
    from tasks_local import ADDRESS_VARIANTS
    try:
        binding, headers, data_rows = sheets_writer.read_bound_sheet(link, ADDRESS_VARIANTS)
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

    total = len(data_rows)
    if not total:
        return render_template('error.html', error_title="No rows found",
                               error_message=f"Tab '{binding['tab']}' has a header but no data rows."), 400
    can_process, current_usage, error_msg = check_usage_limit(total)
    if not can_process:
        return render_template('error.html', error_title="Monthly Limit Reached",
                               error_message=error_msg, current_usage=current_usage), 403

    job_id = str(uuid.uuid4())
    local_path = UPLOAD_FOLDER / f"{job_id}.csv"
    import csv as _csv
    with open(local_path, 'w', newline='', encoding='utf-8') as fh:
        w = _csv.writer(fh)
        w.writerow(binding["headers"])
        w.writerows(data_rows)
    csv_blob_path = f"uploads/{job_id}/{job_id}.csv"
    upload_file(str(local_path), csv_blob_path)
    try:
        enqueue_job(job_id, {
            "job_id": job_id,
            "csv_path": csv_blob_path,
            "total": total,
            "sheet_binding": binding,
        })
        log.info(f"Job {job_id} enqueued from live sheet '{binding['title']}' ({total} rows)")
    except Exception as e:
        log.error(f"Failed to enqueue sheet job: {e}", exc_info=True)
        return f"Error enqueuing job: {e}", 500
    return redirect(url_for('results', job_id=job_id, total=total))

@app.route('/results/<job_id>')
def results(job_id):
    """Results page with progress polling."""
    total_count = request.args.get('total', 0, type=int)
    return render_template('results.html', job_id=job_id, total_count=total_count)

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

    # Include result data (both partial and final)
    # This allows frontend to display results as they come in
    if status['status'] in ['processing', 'finished']:
        result = read_result(job_id)
        if result:
            response['result'] = result

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
    """Serve files from local storage."""
    try:
        if not file_exists(blob_name):
            abort(404)

        data = read_file(blob_name)
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

        resp = Response(data, mimetype=mimetype)
        resp.headers['Cache-Control'] = 'public, max-age=604800'

        # Force download for CSV and ZIP files
        if lower.endswith('.csv') or lower.endswith('.zip'):
            filename = os.path.basename(blob_name)
            resp.headers['Content-Disposition'] = f"attachment; filename={filename}"

        return resp
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
    html = build_review_page(
        batch.get("entries", []),
        job_id=job_id,
        webhook_url="/api/review",  # same-origin relay -> no browser CORS
        title=batch.get("title", "Cooling Tower Review"),
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

    batch = review_store.record_decision(job_id, row_id, {
        "hvac_systems": payload.get("hvac_systems", ""),
        "optimizer_fit": payload.get("optimizer_fit", ""),
        "periscope_fit": payload.get("periscope_fit", ""),
        "note": payload.get("note", ""),
    })
    if not batch:
        return jsonify({"error": "unknown batch/row"}), 404

    sheet = "none"
    if batch.get("sheet_binding") and sheets_writer.enabled():
        # Run-in-place batch: write straight into the team's own sheet row.
        try:
            ok = sheets_writer.write_decision_bound(
                batch["sheet_binding"], row_id,
                hvac=payload.get("hvac_systems", ""),
                optimizer_fit=payload.get("optimizer_fit", ""),
                periscope_fit=payload.get("periscope_fit", ""),
                note=payload.get("note", ""))
            sheet = "updated" if ok else "row_not_found"
        except Exception as e:
            log.error(f"Bound sheet write failed for {job_id}/{row_id}: {e}", exc_info=True)
            sheet = "error"
    elif batch.get("sheet_url") and sheets_writer.enabled():
        try:
            ok = sheets_writer.write_decision(
                batch["sheet_url"], batch.get("table_headers", []),
                batch.get("table_rows", []), row_id,
                hvac=payload.get("hvac_systems", ""),
                optimizer_fit=payload.get("optimizer_fit", ""),
                periscope_fit=payload.get("periscope_fit", ""),
                note=payload.get("note", ""))
            sheet = "updated" if ok else "row_not_found"
        except Exception as e:
            log.error(f"Sheet write failed for {job_id}/{row_id}: {e}", exc_info=True)
            sheet = "error"
    return jsonify({"ok": True, "sheet": sheet})


# -----------------------------------------------------------------------------
# HEALTH CHECK
# -----------------------------------------------------------------------------
@app.route('/health')
def health():
    """Health check endpoint for Render."""
    return jsonify({
        "status": "healthy",
        "worker_running": worker is not None
    })

# -----------------------------------------------------------------------------
# RUN
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.getenv("PORT", "8080"))
    app.run(host='0.0.0.0', port=port, debug=False)
