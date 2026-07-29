"""
Simple SQLite-based job queue to replace Google Cloud Tasks.
Stores job metadata and status for background processing.
"""
import sqlite3
import json
import os
import threading
from datetime import datetime
from pathlib import Path


def _default_db_path() -> Path:
    configured = os.getenv("JOBS_DB_PATH", "").strip()
    if configured:
        return Path(configured)
    storage_dir = os.getenv("STORAGE_DIR", "").strip()
    if storage_dir:
        return Path(storage_dir) / "jobs.db"
    return Path("jobs.db")


DB_PATH = _default_db_path()
_lock = threading.Lock()


def _connect():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db():
    """Initialize the SQLite database with jobs table and usage tracking."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
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
        with _connect() as conn:
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
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("""
            SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


def recover_interrupted_workbook_jobs() -> int:
    """Requeue resumable workbook jobs left in processing after a restart.

    Legacy jobs do not have row-level checkpoints, so they are intentionally
    left untouched instead of risking duplicate paid calls.
    """
    recovered = 0
    now = datetime.utcnow().isoformat()
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT job_id, payload FROM jobs WHERE status = 'processing'"
            ).fetchall()
            for job_id, payload_json in rows:
                try:
                    payload = json.loads(payload_json or "{}")
                except (TypeError, ValueError):
                    continue
                if payload.get("kind") != "workbook":
                    continue
                conn.execute(
                    """UPDATE jobs
                       SET status = 'queued',
                           message = ?,
                           updated_at = ?
                       WHERE job_id = ?""",
                    ("Recovered after worker restart", now, job_id),
                )
                recovered += 1
            conn.commit()
    return recovered


def requeue_failed_workbook_job(job_id: str) -> tuple[bool, str]:
    """Requeue one failed workbook job without reserving monthly usage again."""
    now = datetime.utcnow().isoformat()
    with _lock:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, payload FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row:
                conn.rollback()
                return False, "Workbook job was not found"
            status, payload_json = row
            try:
                payload = json.loads(payload_json or "{}")
            except (TypeError, ValueError):
                conn.rollback()
                return False, "Workbook job payload is invalid"
            if payload.get("kind") != "workbook":
                conn.rollback()
                return False, "Only workbook jobs can resume from row checkpoints"
            if status != "failed":
                conn.rollback()
                return False, f"Workbook job cannot be retried while status is '{status}'"
            cursor = conn.execute(
                """UPDATE jobs
                   SET status = 'queued',
                       cancel_requested = 0,
                       message = ?,
                       updated_at = ?
                   WHERE job_id = ? AND status = 'failed'""",
                ("Retry requested; completed checkpoints will be skipped", now, job_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False, "Workbook job changed before it could be retried"
            conn.commit()
    return True, ""


def enqueue_job_with_usage_reservation(
    job_id: str, payload: dict, requested_addresses: int, *,
    allow_over_limit: bool = False,
) -> tuple[bool, int, str]:
    """Atomically reserve monthly usage and create a queued workbook job."""
    requested_addresses = int(requested_addresses)
    if requested_addresses <= 0:
        raise ValueError("requested_addresses must be positive")
    current_month = get_current_month()
    now = datetime.utcnow().isoformat()
    with _lock:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing:
                row = conn.execute(
                    "SELECT addresses_processed FROM monthly_usage WHERE year_month = ?",
                    (current_month,),
                ).fetchone()
                current_usage = row[0] if row else 0
                conn.rollback()
                return False, current_usage, "Workbook run is already queued"
            row = conn.execute(
                "SELECT addresses_processed FROM monthly_usage WHERE year_month = ?",
                (current_month,),
            ).fetchone()
            current_usage = row[0] if row else 0
            limit = monthly_address_limit()
            remaining = limit - current_usage
            if not allow_over_limit and current_usage >= limit:
                conn.rollback()
                return (
                    False, current_usage,
                    f"Monthly limit of {limit} addresses reached. Resets on 1st of next month.",
                )
            if not allow_over_limit and requested_addresses > remaining:
                conn.rollback()
                return (
                    False, current_usage,
                    f"Batch size ({requested_addresses}) exceeds remaining monthly quota "
                    f"({remaining}). Try a smaller batch.",
                )
            conn.execute(
                """INSERT INTO monthly_usage (year_month, addresses_processed, last_updated)
                   VALUES (?, ?, ?)
                   ON CONFLICT(year_month) DO UPDATE SET
                     addresses_processed = monthly_usage.addresses_processed + excluded.addresses_processed,
                     last_updated = excluded.last_updated""",
                (current_month, requested_addresses, now),
            )
            conn.execute(
                """INSERT INTO jobs
                   (job_id, status, progress, total, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, "queued", 0, payload.get("total", 0),
                    json.dumps(payload), now, now,
                ),
            )
            conn.commit()
    return True, current_usage, ""

def update_job_status(job_id: str, status: str = None, progress: int = None,
                      total: int = None, message: str = None):
    """Update job status and progress."""
    with _lock:
        with _connect() as conn:
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
    with _connect() as conn:
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
        with _connect() as conn:
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

MONTHLY_ADDRESS_LIMIT = 2500  # Backward-compatible default.


def monthly_address_limit() -> int:
    """Configured safety ceiling for address analyses reserved this month."""
    raw = os.environ.get("ANALYZE_MONTHLY_ADDRESS_LIMIT", str(MONTHLY_ADDRESS_LIMIT))
    try:
        return max(1, int(raw))
    except ValueError:
        return MONTHLY_ADDRESS_LIMIT


def batch_size_limit() -> int:
    """Maximum number of addresses accepted in one interactive/API batch.

    Keep this configurable so an operator can raise it deliberately, while a
    malformed or accidentally huge upload cannot monopolize the single worker.
    """
    raw = os.environ.get("ANALYZE_MAX_BATCH_ROWS", "250")
    try:
        return max(1, int(raw))
    except ValueError:
        return 250

def get_current_month() -> str:
    """Get current month in YYYY-MM format."""
    return datetime.utcnow().strftime("%Y-%m")

def get_monthly_usage() -> int:
    """Get total addresses processed this month."""
    current_month = get_current_month()
    with _connect() as conn:
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
    limit = monthly_address_limit()
    remaining = limit - current_usage

    if current_usage >= limit:
        return False, current_usage, f"Monthly limit of {limit} addresses reached. Resets on 1st of next month."

    if requested_addresses > remaining:
        return False, current_usage, f"Batch size ({requested_addresses}) exceeds remaining monthly quota ({remaining}). Try a smaller batch."

    return True, current_usage, ""


def reserve_usage_limit(
    requested_addresses: int, *, allow_over_limit: bool = False
) -> tuple[bool, int, str]:
    """Atomically check and reserve monthly capacity for a chargeable run.

    Stateless API calls reserve here. Durable workbook jobs use
    ``enqueue_job_with_usage_reservation`` so their quota reservation and queue
    insertion occur in the same database transaction.
    """
    requested_addresses = int(requested_addresses)
    if requested_addresses < 0:
        raise ValueError("requested_addresses must be non-negative")
    if requested_addresses == 0:
        return True, get_monthly_usage(), ""

    current_month = get_current_month()
    now = datetime.utcnow().isoformat()
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT addresses_processed FROM monthly_usage WHERE year_month = ?",
                (current_month,),
            ).fetchone()
            current_usage = row[0] if row else 0
            limit = monthly_address_limit()
            remaining = limit - current_usage
            if not allow_over_limit and current_usage >= limit:
                return (False, current_usage,
                        f"Monthly limit of {limit} addresses reached. "
                        "Resets on 1st of next month.")
            if not allow_over_limit and requested_addresses > remaining:
                return (False, current_usage,
                        f"Batch size ({requested_addresses}) exceeds remaining monthly quota "
                        f"({remaining}). Try a smaller batch.")

            conn.execute(
                """INSERT INTO monthly_usage (year_month, addresses_processed, last_updated)
                   VALUES (?, ?, ?)
                   ON CONFLICT(year_month) DO UPDATE SET
                     addresses_processed = monthly_usage.addresses_processed + excluded.addresses_processed,
                     last_updated = excluded.last_updated""",
                (current_month, requested_addresses, now),
            )
            conn.commit()
    return True, current_usage, ""

def increment_usage(addresses_processed: int):
    """Increment the monthly usage counter."""
    current_month = get_current_month()
    now = datetime.utcnow().isoformat()

    with _lock:
        with _connect() as conn:
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
