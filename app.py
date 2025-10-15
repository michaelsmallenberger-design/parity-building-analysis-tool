import os
import json
import uuid
import tempfile
import logging
import pandas as pd
from flask import Flask, request, render_template, redirect, url_for, jsonify, abort, Response

# GCP clients
from google.cloud import tasks_v2
from tasks_serverless import process_address_list  # uses your existing utils.* inside
from gcp_helpers import (
    get_bucket, status_key, result_key, upload_local, write_status, read_status,
    make_signed_url
)

# -----------------------------------------------------------------------------
# Flask & logging
# -----------------------------------------------------------------------------
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

# -----------------------------------------------------------------------------
# Config (via env vars)
# -----------------------------------------------------------------------------
UPLOAD_FOLDER        = os.getenv('UPLOAD_FOLDER', '/tmp/uploads')
RESULTS_BUCKET_NAME  = os.getenv('RESULTS_BUCKET')
GCP_PROJECT          = os.getenv('GCP_PROJECT') or os.getenv('GOOGLE_CLOUD_PROJECT')
TASKS_QUEUE          = os.getenv('TASKS_QUEUE', 'default')
TASKS_LOCATION       = os.getenv('TASKS_LOCATION', 'us-central1')
TASKS_SA_EMAIL       = os.getenv('TASKS_SA_EMAIL')            # SA with Cloud Run Invoker
TASK_HANDLER_URL     = os.getenv('TASK_HANDLER_URL')          # e.g., https://<service-url>/tasks
TASK_AUTH_AUDIENCE   = os.getenv('TASK_AUTH_AUDIENCE')        # same as service URL
TASK_SHARED_SECRET   = os.getenv('TASK_SHARED_SECRET')        # random long string

# Basic sanity checks (logged once at boot)
if not RESULTS_BUCKET_NAME:
    log.warning("RESULTS_BUCKET is not set. Status & results writes will fail.")
if not GCP_PROJECT:
    log.warning("GCP_PROJECT/GOOGLE_CLOUD_PROJECT is not set. Cloud Tasks may fail.")
if not TASK_HANDLER_URL:
    log.warning("TASK_HANDLER_URL is not set. Task enqueue will fail.")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
bucket = get_bucket(RESULTS_BUCKET_NAME) if RESULTS_BUCKET_NAME else None

# -----------------------------------------------------------------------------
# UI ROUTES
# -----------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return redirect(request.url)

    f = request.files['file']
    if not f or f.filename == '':
        return redirect(request.url)

    filename = f.filename
    local_path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(local_path)

    # Try to count rows for UI progress
    try:
        df = pd.read_csv(local_path)
        total = len(df)
    except Exception as e:
        log.warning("Could not count CSV rows: %s", e)
        total = 0

    if not bucket:
        log.error("No bucket client (RESULTS_BUCKET not configured).")
        return "Server misconfigured: RESULTS_BUCKET not set", 500

    # Create job id and upload CSV to GCS
    job_id = str(uuid.uuid4())
    csv_blob = f"uploads/{job_id}/{filename}"
    upload_local(bucket, local_path, csv_blob)

    # Initialize status file
    write_status(
        bucket, job_id,
        status="processing",
        progress=0,
        total=max(total, 1),
        cancel_requested=False
    )

    # Enqueue Cloud Task
    try:
        enqueue_task(job_id=job_id, gcs_csv=csv_blob, total=total)
    except Exception as e:
        log.error("Failed to enqueue task: %s", e, exc_info=True)
        write_status(bucket, job_id, status="failed", progress=0,
                     total=max(total,1), message=f"enqueue-error: {e}")
        # Do NOT raise 500 here; show results page so user sees the error immediately.
        return redirect(url_for('results', job_id=job_id, total=total))

    return redirect(url_for('results', job_id=job_id, total=total))

def enqueue_task(*, job_id, gcs_csv, total):
    if not all([GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE, TASK_HANDLER_URL]):
        raise RuntimeError("Missing one of GCP_PROJECT/TASKS_LOCATION/TASKS_QUEUE/TASK_HANDLER_URL")

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE)

    payload = {"job_id": job_id, "gcs_csv": gcs_csv, "total": int(total or 0)}
    body = json.dumps(payload).encode()

    http_request = {
        "http_method": tasks_v2.HttpMethod.POST,
        "url": f"{TASK_HANDLER_URL}/process",
        "headers": {"Content-Type": "application/json"},
        "body": body,
    }
    # Shared-secret header (optional but recommended)
    if TASK_SHARED_SECRET:
        http_request["headers"]["X-Task-Secret"] = TASK_SHARED_SECRET

    # Add OIDC token if configured
    if TASKS_SA_EMAIL and TASK_AUTH_AUDIENCE:
        http_request["oidc_token"] = {
            "service_account_email": TASKS_SA_EMAIL,
            "audience": TASK_AUTH_AUDIENCE
        }

    task = {"http_request": http_request}
    created = client.create_task(request={"parent": parent, "task": task})
    log.info("Enqueued task: %s payload=%s", created.name, payload)

@app.route('/results/<job_id>')
def results(job_id):
    total_count = request.args.get('total', 0, type=int)
    return render_template('results.html', job_id=job_id, total_count=total_count)

@app.route('/status/<job_id>')
def job_status(job_id):
    if not bucket:
        return jsonify({"status": "failed", "message": "RESULTS_BUCKET not set"}), 200

    st = read_status(bucket, job_id) or {}
    # If finished and we have a result JSON, include it
    rk = result_key(job_id)
    try:
        if st.get("status") == "finished" and bucket.blob(rk).exists():
            data = json.loads(bucket.blob(rk).download_as_text())
            st["result"] = data
    except Exception as e:
        log.warning("Reading result failed: %s", e)
    return jsonify(st), 200

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    if not bucket:
        return jsonify({'status': 'failed', 'message': 'RESULTS_BUCKET not set'}), 200
    st = read_status(bucket, job_id) or {}
    st["cancel_requested"] = True
    # preserve progress/total/status unless failed/finished
    write_status(bucket, job_id, **st)
    return jsonify({'status': 'cancelled'})

# -----------------------------------------------------------------------------
# TASK HANDLER
# -----------------------------------------------------------------------------
@app.route('/tasks/process', methods=['POST'])
def task_process():
    """
    IMPORTANT:
    - We always return HTTP 200 (even on failures) to avoid Cloud Tasks retry storms.
    - We write a clear status JSON for the UI to show.
    """
    # 1) Lightweight auth (shared secret). If missing, log and ACK 200.
    if TASK_SHARED_SECRET:
        incoming = (request.headers.get("X-Task-Secret") or "")
        if incoming != TASK_SHARED_SECRET:
            # Do not 403; that causes retries. Just log and return 200 without doing work.
            app.logger.error("Task auth failed: bad X-Task-Secret")
            return jsonify({"ok": False, "error": "unauthorized"}), 200

    # 2) Parse payload
    try:
        payload = request.get_json(force=True) or {}
    except Exception as e:
        app.logger.error("Bad JSON payload: %s", e)
        return jsonify({"ok": False, "error": "bad-json"}), 200

    job_id = payload.get("job_id")
    gcs_csv = payload.get("gcs_csv")
    total   = int(payload.get("total") or 1)

    if not job_id or not gcs_csv:
        app.logger.error("Missing job_id or gcs_csv in payload: %s", payload)
        return jsonify({"ok": False, "error": "missing-fields"}), 200

    if not bucket:
        app.logger.error("RESULTS_BUCKET not configured.")
        return jsonify({"ok": False, "error": "no-bucket"}), 200

    # 3) Work
    try:
        with tempfile.TemporaryDirectory() as tmpd:
            csv_path = os.path.join(tmpd, "addresses.csv")
            bucket.blob(gcs_csv).download_to_filename(csv_path)

            def progress_cb(done, tot=total, message=None):
                write_status(bucket, job_id, status="processing",
                             progress=int(done), total=max(int(tot),1), message=message)

            def should_cancel():
                st = read_status(bucket, job_id) or {}
                return bool(st.get("cancel_requested"))

            def upload_fn(local_path, dest_blob):
                upload_local(bucket, local_path, dest_blob)
                return dest_blob

            def signed_url_fn(dest_blob, minutes=60*24*7):
                return make_signed_url(bucket, dest_blob, minutes=minutes)

            app.logger.info("Starting job %s (total=%s) CSV=%s", job_id, total, gcs_csv)
            result = process_address_list(
                uploaded_filepath=csv_path,
                job_id=job_id,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                upload_file=upload_fn,
                make_signed_url=signed_url_fn,
            )

            # Persist final result
            bucket.blob(result_key(job_id)).upload_from_string(
                json.dumps(result), content_type="application/json"
            )
            write_status(bucket, job_id, status="finished", progress=max(int(total),1), total=max(int(total),1))
            app.logger.info("Job %s finished.", job_id)
            return jsonify({"ok": True}), 200

    except Exception as e:
        # Catch-all: write failure status, ACK 200 so Cloud Tasks stops retrying
        app.logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        try:
            write_status(bucket, job_id, status="failed", progress=0, total=max(int(total),1), message=str(e))
        except Exception as inner:
            app.logger.error("write_status failed: %s", inner)
        return jsonify({"ok": False, "error": "exception", "detail": str(e)}), 200

# -----------------------------------------------------------------------------
# File proxy (workaround to avoid GCS signed URLs)
# -----------------------------------------------------------------------------
@app.route('/files/<path:blob_name>')
def proxy_file(blob_name):
    if not bucket:
        abort(404)
    try:
        b = bucket.blob(blob_name)
        if not b.exists():
            abort(404)
        data = b.download_as_bytes()
        lower = blob_name.lower()
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
            resp.headers['Content-Disposition'] = f"attachment; filename={os.path.basename(blob_name)}"
        return resp
    except Exception as e:
        app.logger.error("Proxy download failed for %s: %s", blob_name, e)
        abort(404)

# -----------------------------------------------------------------------------
# Dev entrypoint
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8080")), debug=False)
