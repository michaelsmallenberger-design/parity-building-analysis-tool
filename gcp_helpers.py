import os, json, datetime
from google.cloud import storage

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

def make_signed_url(bucket, blob_path: str, minutes=60*24*7) -> str:
    blob = bucket.blob(blob_path)
    return blob.generate_signed_url(expiration=datetime.timedelta(minutes=minutes))
