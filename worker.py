"""
Background worker thread that processes jobs from the queue.
Replaces Google Cloud Tasks with in-process threading.
"""
import os
import time
import logging
import threading
import tempfile
from copy import deepcopy
from pathlib import Path

from job_queue import (
    init_db, get_pending_jobs, update_job_status,
    should_cancel_job, get_job_payload, increment_usage,
    recover_interrupted_workbook_jobs, get_job_status,
)
from storage_helpers import (
    init_storage, get_file_path, upload_file, make_url, write_result,
    read_json, write_json, read_result,
)
from tasks_local import process_address_list
from failure_diagnostics import build_failure_diagnostic

log = logging.getLogger("worker")


def _advanced_row_checkpoint(data, checkpointed_count):
    """Return a compact durable checkpoint only when another row completed."""
    row_results = [
        item for item in data.get("row_results", [])
        if isinstance(item, dict) and "index" in item
    ]
    if len(row_results) <= checkpointed_count:
        return None, checkpointed_count
    return {
        "schema_version": 2,
        "row_results": row_results,
    }, len(row_results)


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
        recovered = recover_interrupted_workbook_jobs()
        if recovered:
            log.warning("Recovered %d interrupted workbook job(s)", recovered)

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
            self._save_failure_diagnostic(
                job_id, {}, RuntimeError("No queued job payload was available"),
                stage="queue_payload",
            )
            update_job_status(job_id, status="failed", message="No payload")
            return

        log.info(f"Processing job {job_id}")
        update_job_status(job_id, status="processing")

        try:
            if payload.get("kind") == "workbook":
                self._process_workbook_job(job_id, payload)
                return

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
                self._save_failure_diagnostic(
                    job_id, payload, str(error_msg),
                    stage="address_processing", result=result,
                )
                update_job_status(job_id, status="failed",
                                progress=0,
                                total=max(int(total), 1),
                                message=error_msg)
                return

            # Finalize into the review loop: same path as /api/run — persist the
            # batch for /review(s) and create the live Google Sheet from the
            # ORIGINAL uploaded table. Non-fatal: the classic report must still
            # come out if Sheets is down.
            table_df = result.pop("table_df", None)
            web_results = result.get("web_results") or []
            if web_results:
                try:
                    from api_analyze import _finalize_batch, _table_from_df
                    headers = rows = None
                    # Browser intake may derive a private Address-only work
                    # file from split columns.  The review surface must still
                    # show (and write against) the untouched uploaded table.
                    source_table = payload.get("original_table") or {}
                    if source_table.get("headers") is not None and source_table.get("rows") is not None:
                        headers = source_table["headers"]
                        rows = source_table["rows"]
                    elif table_df is not None:
                        headers, rows = _table_from_df(table_df)
                    binding = payload.get('sheet_binding')
                    if binding:
                        title = binding.get("title") or Path(csv_path).stem or job_id
                    else:
                        title = Path(csv_path).stem or job_id
                        if result.get("sheet_tab"):
                            # Multi-tab workbook: make WHICH tab ran unmissable.
                            title = f"{title} — {result['sheet_tab']}"
                    _bid, review_url, sheet_url = _finalize_batch(
                        web_results, title, headers, rows, binding=binding)
                    result["review_url"] = review_url
                    result["sheet_url"] = sheet_url
                except Exception as e:
                    log.error(f"Review finalize failed for {job_id}: {e}", exc_info=True)

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
            if payload.get("kind") == "workbook":
                try:
                    import workbook_runs

                    def mark_attention(run):
                        run["status"] = "needs_attention"
                        run["error"] = str(e)

                    workbook_runs.mutate_run(job_id, mark_attention)
                except Exception:
                    log.exception("Could not update failed workbook manifest %s", job_id)
            self._save_failure_diagnostic(
                job_id, payload, e,
                stage=(
                    "workbook_processing"
                    if payload.get("kind") == "workbook"
                    else "address_processing"
                ),
            )
            update_job_status(job_id, status="failed",
                            progress=0,
                            total=max(int(payload.get('total', 1)), 1),
                            message=str(e))

    def _save_failure_diagnostic(
        self, job_id, payload, error, *, stage, result=None,
    ):
        """Attach a sanitized diagnostic without masking the original failure."""
        try:
            current = (
                deepcopy(result)
                if isinstance(result, dict)
                else (read_result(job_id) or {})
            )
            status = get_job_status(job_id) or {}
            rows = current.get("live_rows") or current.get("address_states") or []
            diagnostic = build_failure_diagnostic(
                run_id=job_id,
                stage=stage,
                error=error,
                progress=int(status.get("progress") or 0),
                total=int(status.get("total") or payload.get("total") or 0),
                rows=rows,
            )
            current["diagnostic"] = diagnostic
            write_result(job_id, current)
        except Exception as diagnostic_error:
            log.warning(
                "Could not persist failure diagnostic for %s: %s",
                job_id,
                type(diagnostic_error).__name__,
            )

    def _process_workbook_job(self, job_id, payload):
        """Resume a durable workbook one checkpointed analysis chunk at a time."""
        import workbook_runs

        run = workbook_runs.load_run(job_id)
        if not run:
            raise ValueError("Workbook run manifest is missing")

        def mark_analyzing(current):
            current["status"] = "analyzing"
            current.pop("error", None)

        workbook_runs.mutate_run(job_id, mark_analyzing)
        analysis_done = sum(
            len(chunk.get("items", []))
            for chunk in run.get("chunks", [])
            if chunk.get("status") == "complete"
        )
        live_state_lock = threading.Lock()
        live_rows_by_key = {}
        for manifest_chunk in run.get("chunks", []):
            for item in manifest_chunk.get("items", []):
                for target in item.get("targets", []):
                    source_key = str(target.get("source_key") or "")
                    live_rows_by_key[source_key] = {
                        "row_id": source_key,
                        "address": item.get("address") or "",
                        "tab": target.get("tab") or "",
                        "source_row": target.get("source_row"),
                        "state": "queued",
                        "message": "Waiting to be analyzed",
                        "error": "",
                        "model_result": "",
                    }
            if manifest_chunk.get("status") == "complete":
                completed = read_json(manifest_chunk.get("result_blob") or "") or {}
                for entry in completed.get("entries", []):
                    source_key = str(entry.get("source_key") or "")
                    if source_key not in live_rows_by_key:
                        continue
                    error = str(entry.get("error") or "")
                    live_rows_by_key[source_key].update({
                        "state": "attention" if error else "complete",
                        "message": error or "Machine analysis finished",
                        "error": error,
                        "model_result": str(entry.get("verdict") or ""),
                    })

        def _live_payload():
            return {
                "schema_version": 2,
                "run_id": job_id,
                "live_rows": list(live_rows_by_key.values()),
                "count": len(live_rows_by_key),
                "review_url": "",
                "sheet_url": run.get("sheet_url") or "",
            }

        write_result(job_id, _live_payload())

        for chunk in run.get("chunks", []):
            if chunk.get("status") == "complete":
                continue
            chunk_index = int(chunk["index"])
            chunk_job_id = f"{job_id}-c{chunk_index:04d}"
            csv_path = get_file_path(chunk["csv_blob"])
            if not csv_path.exists():
                raise FileNotFoundError(f"Workbook chunk is missing: {chunk_index}")

            partial = read_json(chunk["partial_blob"]) or {}
            resume = {
                int(item["index"]): item
                for item in partial.get("row_results", [])
                if isinstance(item, dict) and "index" in item
            }
            checkpointed_count = len(resume)

            def progress_cb(done, total, message=None):
                update_job_status(
                    job_id,
                    status="processing",
                    progress=analysis_done + int(done),
                    total=max(int(run.get("analysis_count") or total), 1),
                    message=message or (
                        f"Analyzing chunk {chunk_index + 1} of {len(run.get('chunks', []))}"
                    ),
                )

            def write_workbook_partial(data):
                nonlocal checkpointed_count
                checkpoint, checkpointed_count = _advanced_row_checkpoint(
                    data, checkpointed_count,
                )
                if checkpoint is not None:
                    write_json(chunk["partial_blob"], checkpoint)
                states = {
                    int(item["index"]): item
                    for item in data.get("address_states", [])
                    if isinstance(item, dict) and "index" in item
                }
                results = {
                    int(item["index"]): item.get("web_entry") or {}
                    for item in data.get("row_results", [])
                    if isinstance(item, dict) and "index" in item
                }
                with live_state_lock:
                    for index, state in states.items():
                        if index < 0 or index >= len(chunk.get("items", [])):
                            continue
                        analysis_item = chunk["items"][index]
                        web_entry = results.get(index) or {}
                        row_state = str(state.get("state") or "queued")
                        error = str(state.get("error") or web_entry.get("error") or "")
                        for target in analysis_item.get("targets", []):
                            source_key = str(target.get("source_key") or "")
                            if source_key not in live_rows_by_key:
                                continue
                            live_rows_by_key[source_key].update({
                                "state": row_state,
                                "message": str(state.get("message") or ""),
                                "error": error,
                                "model_result": str(web_entry.get("verdict") or ""),
                            })
                    write_result(job_id, _live_payload())

            result = process_address_list(
                uploaded_filepath=str(csv_path),
                job_id=chunk_job_id,
                progress_cb=progress_cb,
                should_cancel=lambda: should_cancel_job(job_id),
                upload_file=lambda local_path, dest_blob: upload_file(local_path, dest_blob),
                make_signed_url=lambda dest_blob, minutes=None: make_url(dest_blob),
                write_partial_result=write_workbook_partial,
                resume_results=resume,
                include_web_results_in_partials=False,
                generate_html_report=False,
            )
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(result["error"])
            result.pop("table_df", None)
            web_results = result.get("web_results") or []
            if len(web_results) != len(chunk.get("items", [])):
                raise RuntimeError(
                    f"Chunk {chunk_index} returned {len(web_results)} of "
                    f"{len(chunk.get('items', []))} analysis results"
                )

            entries = []
            for item, web_entry in zip(chunk["items"], web_results):
                for target in item.get("targets", []):
                    entry = deepcopy(web_entry)
                    entry["row_id"] = target["source_key"]
                    entry["i"] = target["source_key"]
                    entry["source_key"] = target["source_key"]
                    entry["source_grid_id"] = target["grid_id"]
                    entry["source_tab"] = target["tab"]
                    entry["source_row"] = target["source_row"]
                    entry["analysis_key"] = item["analysis_key"]
                    entries.append(entry)
            write_json(chunk["result_blob"], {"schema_version": 2, "entries": entries})
            workbook_runs.mark_chunk_complete(job_id, chunk_index)
            workbook_runs.record_metric("workbook_chunks_completed")
            analysis_done += len(chunk.get("items", []))

        run = workbook_runs.load_run(job_id) or run
        by_source = {}
        for chunk in run.get("chunks", []):
            data = read_json(chunk["result_blob"]) or {}
            for entry in data.get("entries", []):
                by_source[str(entry.get("source_key") or "")] = entry
        entries = [
            by_source[source_key]
            for source_key in run.get("target_order", [])
            if source_key in by_source
        ]
        if len(entries) != int(run.get("row_count") or 0):
            raise RuntimeError(
                f"Workbook produced {len(entries)} of {run.get('row_count', 0)} source rows"
            )

        from api_analyze import _finalize_batch
        _batch_id, review_url, sheet_url = _finalize_batch(
            entries,
            run.get("source_name") or job_id,
            batch_id=job_id,
            sheet_bindings=run.get("sheet_bindings") or [],
            tab_inventory=run.get("tabs") or [],
            run_id=job_id,
        )
        needs_attention = sum(1 for entry in entries if entry.get("error"))
        workbook_runs.update_analysis_result(
            job_id,
            review_url=review_url,
            completed_rows=len(entries),
            needs_attention=needs_attention,
        )
        workbook_runs.record_metric("workbook_rows_machine_terminal", len(entries))
        if needs_attention:
            workbook_runs.record_metric("workbook_rows_needs_attention", needs_attention)
        write_result(job_id, {
            "schema_version": 2,
            "run_id": job_id,
            "review_url": review_url,
            "sheet_url": sheet_url,
            "count": len(entries),
            "needs_attention": needs_attention,
            "live_rows": list(live_rows_by_key.values()),
        })
        update_job_status(
            job_id,
            status="finished",
            progress=max(int(run.get("analysis_count") or 0), 1),
            total=max(int(run.get("analysis_count") or 0), 1),
            message=(
                f"{needs_attention} row(s) need attention"
                if needs_attention else "Ready for human review"
            ),
        )
        log.info("Workbook job %s finished with %d review row(s)", job_id, len(entries))

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
