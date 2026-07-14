"""Persist analysis batches so the interactive review page (/review/<job_id>) can
be re-opened by the team later, and record each human decision.

Backed by storage_helpers (local disk; point STORAGE_DIR at a Render persistent
disk for durability across redeploys). The Google Sheet is the system of record
for the tabular data + human review; this store holds the batch's cards/images so
the review page can render, plus a local copy of each decision so a reload shows
what's already been reviewed.
"""
from datetime import datetime

from storage_helpers import write_json, read_json, get_file_path


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
        out.append({
            "batch_id": b.get("job_id", p.stem),
            "title": b.get("title", ""),
            "created": b.get("created", ""),
            "sheet_url": b.get("sheet_url", ""),
            "count": len(entries),
            "reviewed": sum(1 for e in entries if e.get("human")),
        })
    return out


def save_batch(job_id: str, title: str, entries: list, sheet_url: str = "",
               table_headers: list = None, table_rows: list = None) -> None:
    """Persist a batch's web_entries (with images) for the review page, plus the
    ORIGINAL uploaded table (headers + rows, all the user's columns) so the finished
    Google Sheet can be assembled from the human picks later."""
    write_json(_path(job_id), {
        "job_id": job_id,
        "title": title,
        "sheet_url": sheet_url,
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
    out = []
    for e in batch.get("entries", []):
        h = e.get("human")
        if h:
            out.append({"row_id": _rid(e), "hvac_systems": h.get("hvac_systems", ""),
                        "fit": h.get("fit", ""), "note": h.get("note", "")})
    return out


def load_batch(job_id: str):
    return read_json(_path(job_id))


def save_raw(job_id: str, batch: dict) -> None:
    """Persist an already-mutated batch dict (re-run merges). Callers own the
    mutation; this just writes it back."""
    write_json(_path(job_id), batch)


def _rid(entry) -> str:
    return str(entry.get("i") if entry.get("i") is not None else entry.get("row_id", ""))


def record_decision(job_id: str, row_id, decision: dict):
    """Stamp a reviewer's decision onto the matching entry so a page reload shows
    it as already reviewed. Returns the updated batch dict (so the caller can
    reach sheet_url/table without re-reading the JSON), or None if the batch/row
    is unknown."""
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
