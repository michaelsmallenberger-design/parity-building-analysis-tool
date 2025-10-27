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
    """Initialize the SQLite database with jobs table."""
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
