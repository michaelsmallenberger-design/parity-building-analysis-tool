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
    """Receive one reviewer decision, stamp it locally, and relay it to the Google
    Sheet writer (Apps Script web app) server-side. No API key required from the
    reviewer; the shared-secret token protects the actual sheet write."""
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    row_id = payload.get("row_id")
    if not job_id or row_id is None:
        return jsonify({"error": "missing job_id/row_id"}), 400

    batch = review_store.load_batch(job_id)
    review_store.record_decision(job_id, row_id, {
        "hvac_systems": payload.get("hvac_systems", ""),
        "fit": payload.get("fit", ""),
        "note": payload.get("note", ""),
    })

    writer = os.getenv("SHEET_WEBHOOK_URL")
    if writer:
        try:
            r = requests.post(writer, json={
                "action": "update",
                "token": os.getenv("SHEET_WEBHOOK_TOKEN", ""),
                "sheet_url": (batch or {}).get("sheet_url", ""),
                **payload,
            }, timeout=20)
            if r.status_code >= 300:
                log.error(f"Sheet writer returned {r.status_code}: {r.text[:200]}")
                return jsonify({"error": "sheet write failed"}), 502
        except Exception as e:
            log.error(f"Sheet writer call failed: {e}")
            return jsonify({"error": "sheet writer unreachable"}), 502
    else:
        log.warning("SHEET_WEBHOOK_URL not set; decision stored locally only")

    return jsonify({"ok": True})


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
