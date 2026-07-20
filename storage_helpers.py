"""
Local file storage helper to replace Google Cloud Storage.
Uses local filesystem for storing uploads, results, and status files.
"""
import os
import json
import shutil
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger(__name__)

# Base storage directory
STORAGE_BASE = Path(os.getenv("STORAGE_DIR", "storage")).resolve()

# Subdirectories
UPLOADS_DIR = STORAGE_BASE / "uploads"
RESULTS_DIR = STORAGE_BASE / "results"

def init_storage():
    """Initialize storage directories."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Log URL detection for debugging
    app_url = os.getenv('APP_URL')
    render_url = os.getenv('RENDER_EXTERNAL_URL')

    if app_url:
        log.info(f"Using APP_URL for file URLs: {app_url}")
    elif render_url:
        log.info(f"Using RENDER_EXTERNAL_URL for file URLs: {render_url}")
    else:
        log.warning("No base URL detected - file URLs will be relative paths. Set APP_URL env var for absolute URLs.")


def _storage_path(blob_path: str) -> Path:
    """Resolve a caller-provided storage path without allowing it to escape the
    configured storage directory.  Blob paths are persisted and later exposed by
    /files, so this check is the common boundary for reads and writes alike."""
    if not isinstance(blob_path, str) or not blob_path.strip():
        raise ValueError("storage path must be a non-empty relative path")
    candidate = (STORAGE_BASE / blob_path).resolve()
    try:
        candidate.relative_to(STORAGE_BASE)
    except ValueError as e:
        raise ValueError("storage path must stay inside STORAGE_DIR") from e
    return candidate

def upload_file(local_path: str, dest_path: str) -> str:
    """
    Upload a file to local storage.

    Args:
        local_path: Path to the local file
        dest_path: Destination path relative to storage base (e.g., "uploads/job123/file.csv")

    Returns:
        The dest_path for reference
    """
    dest_full = _storage_path(dest_path)
    dest_full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_path, dest_full)
    return Path(dest_path).as_posix()

def get_file_path(blob_path: str) -> Path:
    """
    Get the full local path for a blob.

    Args:
        blob_path: Relative path (e.g., "uploads/job123/file.csv")

    Returns:
        Full local Path object
    """
    return _storage_path(blob_path)

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
        base_url: Optional base URL (defaults to auto-detect from APP_URL / RENDER_EXTERNAL_URL)

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
            # Priority 2: RENDER_EXTERNAL_URL (Render injects this automatically)
            render_url = os.getenv('RENDER_EXTERNAL_URL')
            if render_url:
                base_url = render_url
            else:
                # Last resort: Use relative path (works for web UI, not for external use)
                return f"/files/{blob_path}"

    return f"{base_url.rstrip('/')}/files/{blob_path}"

def write_json(blob_path: str, data: dict):
    """Atomically replace a JSON file.

    A process crash or a concurrent reader must never observe a half-written
    review/result document.  ``os.replace`` is atomic when the temporary file
    lives beside the destination (as it does here).
    """
    dest_full = get_file_path(blob_path)
    dest_full.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest_full.name}.", suffix=".tmp",
                                   dir=dest_full.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, dest_full)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

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
