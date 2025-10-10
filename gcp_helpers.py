import os
import datetime
import google.auth
from google.auth.transport.requests import Request
from google.auth.iam import Signer
from google.cloud import storage
import json

SIGNER_EMAIL = os.getenv("SIGNING_SERVICE_ACCOUNT")

def get_bucket(bucket_name: str):
    if not bucket_name:
        raise RuntimeError("RESULTS_BUCKET env var not set")
    client = storage.Client()
    return client.bucket(bucket_name)

def status_key(job_id: str) -> str:
    return f"status/{job_id}.json"

def result_key(job_id: str) -> str:
    return f"results/{job_id}.json"

def upload_local(bucket, local_path: str, dest_blob: str):
    blob = bucket.blob(dest_blob)
    blob.upload_from_filename(local_path)
    return dest_blob

def write_status(bucket, job_id: str, status="processing", progress=0, total=1, message=None, cancel_requested=False):
    payload = {"status": status, "progress": progress, "total": total, "cancel_requested": cancel_requested}
    if message:
        payload["message"] = message
    bucket.blob(status_key(job_id)).upload_from_string(json.dumps(payload), content_type="application/json")

def read_status(bucket, job_id: str):
    b = bucket.blob(status_key(job_id))
    if not b.exists():
        return {"status":"processing","progress":0,"total":1,"cancel_requested":False}
    return json.loads(b.download_as_text())

def make_signed_url(bucket_name: str, blob_name: str, minutes: int = 15, method: str = "GET") -> str:
    # Default ADC from Cloud Run
    credentials, _ = google.auth.default()

    # Service account that will sign (you already set this env var in Cloud Run)
    sa_email = os.environ.get("SIGNING_SERVICE_ACCOUNT")  # e.g. 9034744...-compute@developer.gserviceaccount.com

    # Create an IAM Signer which uses the iamcredentials API (no private key needed)
    signer = Signer(Request(), credentials, sa_email)

    client = storage.Client(credentials=credentials)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    # V4 signed URL
    return blob.generate_signed_url(
        expiration=datetime.timedelta(minutes=minutes),
        method=method,
        version="v4",
        # The following two lines let the library use IAM-based signing:
        service_account_email=sa_email,
        signer=signer,
    )