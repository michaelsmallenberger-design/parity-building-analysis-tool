"""
Flask application for Railway deployment.
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
from job_queue import init_db, enqueue_job, get_job_status, cancel_job
from storage_helpers import init_storage, upload_file, get_file_path, read_result, file_exists, read_file
from worker import start_worker

# Initialize Flask app
app = Flask(__name__)

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

    # If finished, include result data
    if status['status'] == 'finished':
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
        else:
            mimetype = 'application/octet-stream'

        resp = Response(data, mimetype=mimetype)
        resp.headers['Cache-Control'] = 'public, max-age=604800'

        if lower.endswith('.csv'):
            filename = os.path.basename(blob_name)
            resp.headers['Content-Disposition'] = f"attachment; filename={filename}"

        return resp
    except Exception as e:
        log.error(f"Error serving file {blob_name}: {e}")
        abort(404)

# -----------------------------------------------------------------------------
# HEALTH CHECK
# -----------------------------------------------------------------------------
@app.route('/health')
def health():
    """Health check endpoint for Railway."""
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
