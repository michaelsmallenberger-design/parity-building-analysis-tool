"""
Simple SQLite-based job queue to replace Google Cloud Tasks.
Stores job metadata and status for background processing.
"""
import sqlite3
import json
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path("jobs.db")
_lock = threading.Lock()

def init_db():
    """Initialize the SQLite database with jobs table and usage tracking."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                message TEXT,
                cancel_requested INTEGER DEFAULT 0,
                payload TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # Usage tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year_month TEXT NOT NULL,
                addresses_processed INTEGER DEFAULT 0,
                last_updated TEXT,
                UNIQUE(year_month)
            )
        """)
        conn.commit()

def enqueue_job(job_id: str, payload: dict):
    """Add a new job to the queue."""
    with _lock:
        with sqlite3.connect(DB_PATH) as conn:
            now = datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO jobs (job_id, status, progress, total, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                "queued",
                0,
                payload.get("total", 0),
                json.dumps(payload),
                now,
                now
            ))
            conn.commit()

def get_pending_jobs():
    """Get all jobs with status 'queued'."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC
        """)
        return [dict(row) for row in cursor.fetchall()]

def update_job_status(job_id: str, status: str = None, progress: int = None,
                      total: int = None, message: str = None):
    """Update job status and progress."""
    with _lock:
        with sqlite3.connect(DB_PATH) as conn:
            updates = []
            params = []

            if status is not None:
                updates.append("status = ?")
                params.append(status)
            if progress is not None:
                updates.append("progress = ?")
                params.append(progress)
            if total is not None:
                updates.append("total = ?")
                params.append(total)
            if message is not None:
                updates.append("message = ?")
                params.append(message)

            updates.append("updated_at = ?")
            params.append(datetime.utcnow().isoformat())
            params.append(job_id)

            conn.execute(f"""
                UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?
            """, params)
            conn.commit()

def get_job_status(job_id: str):
    """Get current status of a job."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if row:
            data = dict(row)
            # Convert cancel_requested from int to bool
            data['cancel_requested'] = bool(data.get('cancel_requested', 0))
            return data
        return None

def cancel_job(job_id: str):
    """Mark a job as cancelled."""
    with _lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                UPDATE jobs SET cancel_requested = 1, updated_at = ?
                WHERE job_id = ?
            """, (datetime.utcnow().isoformat(), job_id))
            conn.commit()

def should_cancel_job(job_id: str) -> bool:
    """Check if job has been marked for cancellation."""
    status = get_job_status(job_id)
    return status and status.get('cancel_requested', False)

def get_job_payload(job_id: str):
    """Get the original payload for a job."""
    status = get_job_status(job_id)
    if status and status.get('payload'):
        return json.loads(status['payload'])
    return None

# -----------------------------------------------------------------------------
# USAGE TRACKING (for monthly API limit enforcement)
# -----------------------------------------------------------------------------

MONTHLY_ADDRESS_LIMIT = 2500  # Conservative limit to stay under $5/month

def get_current_month() -> str:
    """Get current month in YYYY-MM format."""
    return datetime.utcnow().strftime("%Y-%m")

def get_monthly_usage() -> int:
    """Get total addresses processed this month."""
    current_month = get_current_month()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("""
            SELECT addresses_processed FROM monthly_usage WHERE year_month = ?
        """, (current_month,))
        row = cursor.fetchone()
        return row[0] if row else 0

def check_usage_limit(requested_addresses: int) -> tuple[bool, int, str]:
    """
    Check if processing this batch would exceed monthly limit.

    Returns:
        (can_process, current_usage, error_message)
    """
    current_usage = get_monthly_usage()
    remaining = MONTHLY_ADDRESS_LIMIT - current_usage

    if current_usage >= MONTHLY_ADDRESS_LIMIT:
        return False, current_usage, f"Monthly limit of {MONTHLY_ADDRESS_LIMIT} addresses reached. Resets on 1st of next month."

    if requested_addresses > remaining:
        return False, current_usage, f"Batch size ({requested_addresses}) exceeds remaining monthly quota ({remaining}). Try a smaller batch."

    return True, current_usage, ""

def increment_usage(addresses_processed: int):
    """Increment the monthly usage counter."""
    current_month = get_current_month()
    now = datetime.utcnow().isoformat()

    with _lock:
        with sqlite3.connect(DB_PATH) as conn:
            # Try to update existing record
            cursor = conn.execute("""
                UPDATE monthly_usage
                SET addresses_processed = addresses_processed + ?,
                    last_updated = ?
                WHERE year_month = ?
            """, (addresses_processed, now, current_month))

            # If no record exists, insert new one
            if cursor.rowcount == 0:
                conn.execute("""
                    INSERT INTO monthly_usage (year_month, addresses_processed, last_updated)
                    VALUES (?, ?, ?)
                """, (current_month, addresses_processed, now))

            conn.commit()
