"""
Background worker thread that processes jobs from the queue.
Replaces Google Cloud Tasks with in-process threading.
"""
import os
import time
import logging
import threading
import tempfile
from pathlib import Path

from job_queue import (
    init_db, get_pending_jobs, update_job_status,
    should_cancel_job, get_job_payload, increment_usage
)
from storage_helpers import init_storage, get_file_path, upload_file, make_url, write_result
from tasks_local import process_address_list

log = logging.getLogger("worker")

class BackgroundWorker:
    """Background worker that processes jobs in a separate thread."""

    def __init__(self, poll_interval=2):
        self.poll_interval = poll_interval
        self.running = False
        self.thread = None

    def start(self):
        """Start the background worker thread."""
        if self.running:
            log.warning("Worker already running")
            return

        # Initialize database and storage
        init_db()
        init_storage()

        self.running = True
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()
        log.info("Background worker started")

    def stop(self):
        """Stop the background worker thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        log.info("Background worker stopped")

    def _worker_loop(self):
        """Main worker loop that polls for pending jobs."""
        while self.running:
            try:
                jobs = get_pending_jobs()
                for job_data in jobs:
                    if not self.running:
                        break
                    self._process_job(job_data)
            except Exception as e:
                log.error(f"Worker loop error: {e}", exc_info=True)

            time.sleep(self.poll_interval)

    def _process_job(self, job_data):
        """Process a single job."""
        job_id = job_data['job_id']
        payload = get_job_payload(job_id)

        if not payload:
            log.error(f"No payload found for job {job_id}")
            update_job_status(job_id, status="failed", message="No payload")
            return

        log.info(f"Processing job {job_id}")
        update_job_status(job_id, status="processing")

        try:
            csv_path = payload.get('csv_path')
            total = int(payload.get('total', 0))

            if not csv_path:
                raise ValueError("No csv_path in payload")

            # Get full path to uploaded CSV
            full_csv_path = get_file_path(csv_path)
            log.info(f"Resolved CSV path: {full_csv_path}")

            if not full_csv_path.exists():
                raise FileNotFoundError(f"CSV not found: {csv_path}")

            # Progress callback
            def progress_cb(done, tot=total, message=None):
                update_job_status(job_id, status="processing",
                                progress=int(done), total=max(int(tot), 1),
                                message=message)

            # Cancellation check
            def should_cancel():
                return should_cancel_job(job_id)

            # Upload file helper
            def upload_fn(local_path, dest_blob):
                return upload_file(local_path, dest_blob)

            # URL generator
            def make_url_fn(dest_blob, minutes=None):
                return make_url(dest_blob)

            # Partial result writer for real-time updates
            def write_partial_fn(partial_data):
                write_result(job_id, partial_data)

            # Process the address list
            log.info(f"Starting address processing for job {job_id}, CSV: {csv_path}")
            result = process_address_list(
                uploaded_filepath=str(full_csv_path),
                job_id=job_id,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                upload_file=upload_fn,
                make_signed_url=make_url_fn,
                write_partial_result=write_partial_fn,
            )

            # Check if result contains an error (from early CSV parsing failures)
            if isinstance(result, dict) and "error" in result:
                error_msg = result["error"]
                log.error(f"Job {job_id} failed during processing: {error_msg}")
                write_result(job_id, result)  # Save error result for user visibility
                update_job_status(job_id, status="failed",
                                progress=0,
                                total=max(int(total), 1),
                                message=error_msg)
                return

            # Save result
            write_result(job_id, result)

            # Increment usage counter for monthly tracking
            increment_usage(max(int(total), 1))

            # Mark as finished
            update_job_status(job_id, status="finished",
                            progress=max(int(total), 1),
                            total=max(int(total), 1))
            log.info(f"Job {job_id} completed successfully")

        except Exception as e:
            log.error(f"Job {job_id} failed: {e}", exc_info=True)
            update_job_status(job_id, status="failed",
                            progress=0,
                            total=max(int(payload.get('total', 1)), 1),
                            message=str(e))

# Global worker instance
_worker = None

def start_worker():
    """Start the global background worker."""
    global _worker
    if _worker is None:
        _worker = BackgroundWorker()
        _worker.start()
    return _worker

def get_worker():
    """Get the global worker instance."""
    return _worker
