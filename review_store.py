"""Persist analysis batches so the interactive review page (/review/<job_id>) can
be re-opened by the team later, and record each human decision.

Backed by storage_helpers (local disk; point STORAGE_DIR at a Render persistent
disk for durability across redeploys). The Google Sheet is the system of record
for the tabular data + human review; this store holds the batch's cards/images so
the review page can render, plus a local copy of each decision so a reload shows
what's already been reviewed.
"""
from datetime import datetime
import threading

from storage_helpers import write_json, read_json, get_file_path
from review_contract import (
    CURRENT_REVIEW_SCHEMA,
    SINGLE_FIT_SCHEMA,
    entry_is_reviewed,
    infer_review_schema,
)


_locks_guard = threading.Lock()
_batch_locks = {}


def _lock_for(job_id: str) -> threading.RLock:
    """Return the process-local lock for one batch's read-modify-write cycle.

    The deployed service uses one Gunicorn worker, so this closes the realistic
    race between two browser submits (especially Submit all) without serializing
    decisions for unrelated batches. Atomic storage writes below also protect
    readers if the process stops while a save is happening.
    """
    key = str(job_id)
    with _locks_guard:
        lock = _batch_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _batch_locks[key] = lock
        return lock


def _path(job_id: str) -> str:
    return f"reviews/{job_id}.json"


def list_batches() -> list:
    """Summaries of every persisted batch, newest first. This is the recovery
    path when an /api/run response is lost in transit: batch ids are random and
    appear nowhere else, so without this listing a lost response means a lost
    review URL."""
    base = get_file_path("reviews")
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("*.json"), key=lambda q: q.stat().st_mtime, reverse=True):
        b = read_json(f"reviews/{p.name}")
        if not b:
            continue
        entries = b.get("entries", [])
        review_schema = infer_review_schema(b)
        out.append({
            "batch_id": b.get("job_id", p.stem),
            "title": b.get("title", ""),
            "created": b.get("created", ""),
            "sheet_url": b.get("sheet_url", ""),
            "count": len(entries),
            "reviewed": sum(
                1 for entry in entries
                if entry_is_reviewed(entry, review_schema)
            ),
        })
    return out


def save_batch(job_id: str, title: str, entries: list, sheet_url: str = "",
               table_headers: list = None, table_rows: list = None,
               sheet_binding: dict = None, sheet_bindings: list = None,
               tab_inventory: list = None, schema_version: int = 1,
               run_id: str = None,
               review_schema: str = CURRENT_REVIEW_SCHEMA) -> None:
    """Persist a batch's web_entries (with images) for the review page, plus the
    ORIGINAL uploaded table (headers + rows, all the user's columns) so the finished
    Google Sheet can be assembled from the human picks later. sheet_binding is set
    for run-in-place batches: decisions write into the team's own sheet."""
    with _lock_for(job_id):
        write_json(_path(job_id), {
            "job_id": job_id,
            "title": title,
            "sheet_url": sheet_url,
            "sheet_binding": sheet_binding,
            "sheet_bindings": sheet_bindings or [],
            "tab_inventory": tab_inventory or [],
            "schema_version": int(schema_version),
            "review_schema": str(review_schema or CURRENT_REVIEW_SCHEMA),
            "run_id": run_id,
            "created": datetime.utcnow().isoformat(),
            "table_headers": table_headers or [],
            "table_rows": table_rows or [],
            "entries": entries,
        })


def decisions_for(job_id: str) -> list:
    """Return the human review decisions recorded so far for a batch, as a list of
    {row_id, hvac_systems, fit, note} — what an operator merges into the sheet."""
    batch = load_batch(job_id)
    if not batch:
        return []
    review_schema = infer_review_schema(batch)
    out = []
    for e in batch.get("entries", []):
        h = e.get("human")
        if not entry_is_reviewed(e, review_schema):
            continue
        decision = {
            "row_id": _rid(e),
            "hvac_systems": h.get("hvac_systems", ""),
            "note": h.get("note", ""),
        }
        if review_schema == SINGLE_FIT_SCHEMA:
            decision["fit"] = h.get("fit", "")
        else:
            decision["optimizer_fit"] = h.get("optimizer_fit", "")
            decision["periscope_fit"] = h.get("periscope_fit", "")
        out.append(decision)
    return out


def load_batch(job_id: str):
    batch = read_json(_path(job_id))
    if batch:
        batch["review_schema"] = infer_review_schema(batch)
    return batch


def save_raw(job_id: str, batch: dict) -> None:
    """Persist an already-mutated batch dict (re-run merges). Callers own the
    mutation; this just writes it back."""
    with _lock_for(job_id):
        write_json(_path(job_id), batch)


def merge_source_contexts(job_id: str, contexts: dict) -> dict:
    """Merge allowlisted source context by exact grid/tab/physical-row key."""
    with _lock_for(job_id):
        batch = load_batch(job_id)
        if not batch:
            return {"matched": 0, "updated": 0, "batch": None}
        matched = 0
        updated = 0
        for entry in batch.get("entries", []):
            try:
                source_row = int(entry.get("source_row"))
            except (TypeError, ValueError):
                continue
            key = (
                str(entry.get("source_grid_id")),
                str(entry.get("source_tab") or ""),
                source_row,
            )
            incoming = contexts.get(key) or {}
            stories = str(incoming.get("stories") or "").strip()[:80]
            if not stories:
                continue
            matched += 1
            existing = dict(entry.get("source_context") or {})
            if existing.get("stories") == stories:
                continue
            existing["stories"] = stories
            entry["source_context"] = existing
            updated += 1
        if updated:
            write_json(_path(job_id), batch)
        return {"matched": matched, "updated": updated, "batch": batch}


def _rid(entry) -> str:
    return str(
        entry.get("row_id")
        if entry.get("row_id") is not None
        else entry.get("i", "")
    )


def record_decision(job_id: str, row_id, decision: dict):
    """Stamp a reviewer's decision onto the matching entry so a page reload shows
    it as already reviewed. Returns the updated batch dict (so the caller can
    reach sheet_url/table without re-reading the JSON), or None if the batch/row
    is unknown."""
    with _lock_for(job_id):
        batch = load_batch(job_id)
        if not batch:
            return None
        hit = False
        for e in batch.get("entries", []):
            if _rid(e) == str(row_id):
                e["human"] = {**decision, "reviewed_at": datetime.utcnow().isoformat()}
                hit = True
        if not hit:
            return None
        write_json(_path(job_id), batch)
        return batch


def record_writeback(job_id: str, row_id, status: str, error: str = ""):
    """Persist whether the local decision reached its exact Sheet source row."""
    with _lock_for(job_id):
        batch = load_batch(job_id)
        if not batch:
            return None
        for entry in batch.get("entries", []):
            if _rid(entry) != str(row_id):
                continue
            entry["writeback"] = {
                "status": str(status),
                "error": str(error or ""),
                "updated_at": datetime.utcnow().isoformat(),
            }
            write_json(_path(job_id), batch)
            return batch
        return None
