"""
Local file storage helper to replace Google Cloud Storage.
Uses local filesystem for storing uploads, results, and status files.
"""
import os
import json
import shutil
import logging
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

# Base storage directory
STORAGE_BASE = Path(os.getenv("STORAGE_DIR", "storage"))

# Subdirectories
UPLOADS_DIR = STORAGE_BASE / "uploads"
RESULTS_DIR = STORAGE_BASE / "results"

def init_storage():
    """Initialize storage directories."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Log URL detection for debugging
    app_url = os.getenv('APP_URL')
    railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
    static_url = os.getenv('RAILWAY_STATIC_URL')

    if app_url:
        log.info(f"Using APP_URL for file URLs: {app_url}")
    elif railway_domain:
        log.info(f"Using RAILWAY_PUBLIC_DOMAIN for file URLs: https://{railway_domain}")
    elif static_url:
        log.info(f"Using RAILWAY_STATIC_URL for file URLs: {static_url}")
    else:
        log.warning("No base URL detected - file URLs will be relative paths. Set APP_URL env var for absolute URLs.")

def upload_file(local_path: str, dest_path: str) -> str:
    """
    Upload a file to local storage.

    Args:
        local_path: Path to the local file
        dest_path: Destination path relative to storage base (e.g., "uploads/job123/file.csv")

    Returns:
        The dest_path for reference
    """
    dest_full = STORAGE_BASE / dest_path
    dest_full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, dest_full)
    return dest_path

def get_file_path(blob_path: str) -> Path:
    """
    Get the full local path for a blob.

    Args:
        blob_path: Relative path (e.g., "uploads/job123/file.csv")

    Returns:
        Full local Path object
    """
    return STORAGE_BASE / blob_path

def file_exists(blob_path: str) -> bool:
    """Check if a file exists in storage."""
    return get_file_path(blob_path).exists()

def read_file(blob_path: str) -> bytes:
    """Read a file from storage."""
    with open(get_file_path(blob_path), 'rb') as f:
        return f.read()

def write_file(blob_path: str, content: bytes):
    """Write content to a file in storage."""
    dest_full = get_file_path(blob_path)
    dest_full.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_full, 'wb') as f:
        f.write(content)

def delete_file(blob_path: str):
    """Delete a file from storage."""
    path = get_file_path(blob_path)
    if path.exists():
        path.unlink()

def make_url(blob_path: str, base_url: str = None) -> str:
    """
    Create a URL for accessing a file.

    Args:
        blob_path: Relative path (e.g., "results/job123/image.jpg")
        base_url: Optional base URL (defaults to auto-detect from Railway env)

    Returns:
        Full URL or relative path for accessing the file
    """
    # Auto-detect base URL from environment
    if base_url is None:
        # Priority 1: Manual override via APP_URL env var
        app_url = os.getenv('APP_URL')
        if app_url:
            base_url = app_url
        else:
            # Priority 2: Railway public domain
            railway_domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
            if railway_domain:
                base_url = f"https://{railway_domain}"
            else:
                # Priority 3: RAILWAY_STATIC_URL
                static_url = os.getenv('RAILWAY_STATIC_URL')
                if static_url:
                    base_url = static_url
                else:
                    # Last resort: Use relative path (works for web UI, not for external use)
                    return f"/files/{blob_path}"

    return f"{base_url.rstrip('/')}/files/{blob_path}"

def write_json(blob_path: str, data: dict):
    """Write JSON data to a file."""
    write_file(blob_path, json.dumps(data).encode('utf-8'))

def read_json(blob_path: str) -> dict:
    """Read JSON data from a file."""
    if not file_exists(blob_path):
        return None
    return json.loads(read_file(blob_path).decode('utf-8'))

# Job-specific helpers (to match GCS interface)
def result_path(job_id: str) -> str:
    """Get the result JSON path for a job."""
    return f"results/{job_id}.json"

def write_result(job_id: str, data: dict):
    """Write result JSON for a job."""
    write_json(result_path(job_id), data)

def read_result(job_id: str) -> dict:
    """Read result JSON for a job."""
    return read_json(result_path(job_id))
