# Deploying Your Flask + YOLO App to Google Cloud Run (Free-tier Friendly)

This keeps your **Google→Mapbox** geocoding, **Mapbox Static Images**, and **YOLO** logic intact.
We only replace Redis/RQ + local `static/` writes with **Cloud Tasks** + **Cloud Storage**.

## Files in this patch
- `app.py` — Flask app with:
  - `/upload` enqueues a **Cloud Task**.
  - `/tasks/process` executes your job (download CSV, process rows, upload artifacts).
  - `/status/<job_id>` + `/cancel/<job_id>` read/write status in **GCS**.
- `tasks_serverless.py` — wraps your existing `utils.*` functions in a serverless-friendly loop.
- `gcp_helpers.py` — small helpers for GCS status, signed URLs, and uploads.
- `requirements.additions.txt` — packages to append to your existing requirements.
- `Dockerfile.cloudrun` — reference Dockerfile tuned for Cloud Run.

> Keep your existing `utils.py`, `config.py`, `templates/`, and the YOLO model.

## Prereqs
- GCP project with **Cloud Run**, **Cloud Tasks**, **Cloud Storage** enabled.
- A private **GCS bucket** for results (e.g., `parity-build-tool-results`).
- Your Mapbox + Google API keys in `config.py` (unchanged).

## Install deps
Append these to `requirements.txt`:
```
google-cloud-storage>=2.10.0
google-cloud-tasks>=2.12.0
gunicorn>=21.2.0
```

## Build & push image
From project root (where Dockerfile is). If you use `Dockerfile.cloudrun`, rename to `Dockerfile` or specify `-f`:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT

# Build
gcloud builds submit --tag gcr.io/YOUR_PROJECT/parity-building-tool:latest

# Deploy
gcloud run deploy parity-building-tool \
  --image gcr.io/YOUR_PROJECT/parity-building-tool:latest \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --timeout 3600
```

> If you want `/tasks/*` to be **private**, you can either:
> - Keep the shared secret + OIDC (already in code), and/or
> - Add an ingress rule / Cloud Run IAM (Authenticated callers only) and keep the UI public (via a separate service or path-based proxy).

## Create Cloud Tasks queue
```bash
gcloud tasks queues create default --location=us-central1
```

## Service Account for Cloud Tasks → Cloud Run
Create (or reuse) a service account, grant **Cloud Run Invoker**:
```bash
gcloud iam service-accounts create tasks-invoker --display-name="Tasks Invoker"
gcloud run services add-iam-policy-binding parity-building-tool \
  --member=serviceAccount:tasks-invoker@YOUR_PROJECT.iam.gserviceaccount.com \
  --role=roles/run.invoker \
  --region=us-central1
```

## Set environment variables (Cloud Run → Variables & Secrets)
```
GCP_PROJECT=YOUR_PROJECT
RESULTS_BUCKET=parity-build-tool-results
TASKS_QUEUE=default
TASKS_LOCATION=us-central1
TASKS_SA_EMAIL=tasks-invoker@YOUR_PROJECT.iam.gserviceaccount.com
TASK_HANDLER_URL=https://<your-cloud-run-url>/tasks
TASK_AUTH_AUDIENCE=https://<your-cloud-run-url>
TASK_SHARED_SECRET=<random-long-string>
UPLOAD_FOLDER=/tmp/uploads
```

Your existing `config.py` should continue to provide:
```
MAPBOX_API_KEY=...
GOOGLE_API_KEY=...
```

## Test flow
1. Open the service URL (`GET /`) and upload a CSV.
2. You should be redirected to `/results/<job_id>` where the page polls `/status/<job_id>`.
3. On completion, `/status/<job_id>` will include signed URLs for images and the results CSV.

## Long jobs (>60min)
If a single run might exceed Cloud Run’s request timeout, switch to **Cloud Run Jobs**. The loop & storage stay the same; only the launcher changes.

## Security
- UI can be public or gated via Cloudflare Access (free up to 50 users).
- `/tasks/*` calls are authenticated via **OIDC** from Cloud Tasks plus a shared secret header.
- Artifacts are in a private bucket and served via **signed URLs** (time-limited).
