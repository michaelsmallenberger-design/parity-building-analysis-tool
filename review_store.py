"""Persist analysis batches so the interactive review page (/review/<job_id>) can
be re-opened by the team later, and record each human decision.

Backed by storage_helpers (local disk; point STORAGE_DIR at a Render persistent
disk for durability across redeploys). The Google Sheet is the system of record
for the tabular data + human review; this store holds the batch's cards/images so
the review page can render, plus a local copy of each decision so a reload shows
what's already been reviewed.
"""
from datetime import datetime

from storage_helpers import write_json, read_json


def _path(job_id: str) -> str:
    return f"reviews/{job_id}.json"


def save_batch(job_id: str, title: str, entries: list, sheet_url: str = "") -> None:
    """Persist a batch's web_entries (with images) for later review."""
    write_json(_path(job_id), {
        "job_id": job_id,
        "title": title,
        "sheet_url": sheet_url,
        "created": datetime.utcnow().isoformat(),
        "entries": entries,
    })


def load_batch(job_id: str):
    return read_json(_path(job_id))


def _rid(entry) -> str:
    return str(entry.get("i") if entry.get("i") is not None else entry.get("row_id", ""))


def record_decision(job_id: str, row_id, decision: dict) -> bool:
    """Stamp a reviewer's decision onto the matching entry so a page reload shows
    it as already reviewed. Returns False if the batch/row is unknown."""
    batch = load_batch(job_id)
    if not batch:
        return False
    hit = False
    for e in batch.get("entries", []):
        if _rid(e) == str(row_id):
            e["human"] = {**decision, "reviewed_at": datetime.utcnow().isoformat()}
            hit = True
    if hit:
        write_json(_path(job_id), batch)
    return hit
