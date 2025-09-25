import os
import json
import uuid
import tempfile
import pandas as pd
from flask import Flask, request, render_template, redirect, url_for, jsonify, abort
from werkzeug.utils import secure_filename

# GCP clients
from google.cloud import tasks_v2, storage
from tasks_serverless import process_address_list  # uses your existing utils.* inside
from gcp_helpers import (
    get_bucket, status_key, result_key, upload_local, write_status, read_status,
    make_signed_url
)

# Flask
app = Flask(__name__)

# --- Config (via env vars) ---
UPLOAD_FOLDER        = os.getenv('UPLOAD_FOLDER', '/tmp/uploads')
RESULTS_BUCKET_NAME  = os.getenv('RESULTS_BUCKET')
GCP_PROJECT          = os.getenv('GCP_PROJECT')
TASKS_QUEUE          = os.getenv('TASKS_QUEUE', 'default')
TASKS_LOCATION       = os.getenv('TASKS_LOCATION', 'us-central1')
TASKS_SA_EMAIL       = os.getenv('TASKS_SA_EMAIL')  # SA with Cloud Run Invoker
TASK_HANDLER_URL     = os.getenv('TASK_HANDLER_URL')  # e.g., https://<service-url>/tasks
TASK_AUTH_AUDIENCE   = os.getenv('TASK_AUTH_AUDIENCE')  # same as service URL
TASK_SHARED_SECRET   = os.getenv('TASK_SHARED_SECRET')  # random long string

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
bucket = get_bucket(RESULTS_BUCKET_NAME)

# ----------------------------- UI ROUTES -----------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return redirect(request.url)

    f = request.files['file']
    if f.filename == '':
        return redirect(request.url)

    filename = secure_filename(f.filename)
    local_path = os.path.join(UPLOAD_FOLDER, filename)
    f.save(local_path)

    # Try to count for UI
    try:
        df = pd.read_csv(local_path)
        total = len(df)
    except Exception:
        total = 0

    # Create job id and upload CSV to GCS
    job_id = str(uuid.uuid4())
    csv_blob = f"uploads/{job_id}/{filename}"
    upload_local(bucket, local_path, csv_blob)

    # Initialize status file
    write_status(bucket, job_id, status="processing", progress=0, total=max(total, 1), cancel_requested=False)

    # Enqueue Cloud Task
    enqueue_task(job_id=job_id, gcs_csv=csv_blob, total=total)

    return redirect(url_for('results', job_id=job_id, total=total))

def enqueue_task(*, job_id, gcs_csv, total):
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(GCP_PROJECT, TASKS_LOCATION, TASKS_QUEUE)

    payload = json.dumps({"job_id": job_id, "gcs_csv": gcs_csv, "total": total}).encode()
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{TASK_HANDLER_URL}/process",
            "headers": {
                "Content-Type": "application/json",
                "X-Task-Secret": TASK_SHARED_SECRET or ""
            },
            "body": payload,
        }
    }
    # Add OIDC token if configured
    if TASKS_SA_EMAIL and TASK_AUTH_AUDIENCE:
        task["http_request"]["oidc_token"] = {
            "service_account_email": TASKS_SA_EMAIL,
            "audience": TASK_AUTH_AUDIENCE
        }

    client.create_task(request={"parent": parent, "task": task})

@app.route('/results/<job_id>')
def results(job_id):
    total_count = request.args.get('total', 0, type=int)
    return render_template('results.html', job_id=job_id, total_count=total_count)

@app.route('/status/<job_id>')
def job_status(job_id):
    st = read_status(bucket, job_id)
    # If finished and we have a result JSON, include it
    rk = result_key(job_id)
    if st.get("status") == "finished" and bucket.blob(rk).exists():
        data = json.loads(bucket.blob(rk).download_as_text())
        st["result"] = data
    return jsonify(st)

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    st = read_status(bucket, job_id)
    st["cancel_requested"] = True
    # preserve progress/total/status unless failed/finished
    write_status(bucket, job_id, **st)
    return jsonify({'status': 'cancelled'})

# ----------------------------- TASK HANDLER -----------------------------

@app.route('/tasks/process', methods=['POST'])
def task_process():
    # Basic auth: shared secret header (plus optional OIDC on Cloud Tasks side)
    if (request.headers.get("X-Task-Secret") or "") != (TASK_SHARED_SECRET or ""):
        abort(403)

    payload = request.get_json(force=True) or {}
    job_id = payload.get("job_id")
    gcs_csv = payload.get("gcs_csv")
    total   = int(payload.get("total") or 1)

    if not job_id or not gcs_csv:
        abort(400, "Missing job_id or gcs_csv")

    # Download CSV to temp
    with tempfile.TemporaryDirectory() as tmpd:
        csv_path = os.path.join(tmpd, "addresses.csv")
        bucket.blob(gcs_csv).download_to_filename(csv_path)

        # Call your existing logic via the serverless-friendly wrapper
        def progress_cb(done, tot=total, message=None):
            write_status(bucket, job_id, status="processing", progress=done, total=max(tot,1), message=message)

        def should_cancel():
            st = read_status(bucket, job_id)
            return bool(st.get("cancel_requested"))

        def upload_fn(local_path, dest_blob):
            upload_local(bucket, local_path, dest_blob)
            return dest_blob

        def signed_url_fn(dest_blob, minutes=60*24*7):
            return make_signed_url(bucket, dest_blob, minutes=minutes)

        try:
            result = process_address_list(
                uploaded_filepath=csv_path,
                job_id=job_id,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                upload_file=upload_fn,
                make_signed_url=signed_url_fn,
            )
            # Persist final result
            bucket.blob(result_key(job_id)).upload_from_string(json.dumps(result), content_type="application/json")
            write_status(bucket, job_id, status="finished", progress=total, total=max(total,1))
        except Exception as e:
            write_status(bucket, job_id, status="failed", progress=0, total=max(total,1), message=str(e))
            abort(500, str(e))

    return jsonify({"ok": True})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", "8080")), debug=False)
