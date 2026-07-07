"""Persist analysis batches so the interactive review page (/review/<job_id>) can
be re-opened by the team later, and record each human decision.

Backed by storage_helpers (local disk; point STORAGE_DIR at a Render persistent
disk for durability across redeploys). The Google Sheet is the system of record
for the tabular data + human review; this store holds the batch's cards/images so
the review page can render, plus a local copy of each decision so a reload shows
what's already been reviewed.
"""
from datetime import datetime

import requests

from storage_helpers import write_json, read_json


def post_appscript(url: str, payload: dict, timeout: int = 90) -> dict:
    """POST to a Google Apps Script /exec web app and return its JSON.

    Apps Script answers a POST with a 302 to a script.googleusercontent.com "echo"
    URL that carries the actual response; a plain requests.post mishandles that hop
    (404). So POST without auto-follow, then GET the redirect target on the SAME
    session (cookies carry over). Returns the parsed JSON, or an {"error": ...} dict.
    """
    s = requests.Session()
    r = s.post(url, json=payload, allow_redirects=False, timeout=timeout)
    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
        r = s.get(r.headers["Location"], timeout=timeout)
    try:
        return r.json()
    except ValueError:
        return {"error": f"non-json response (HTTP {r.status_code})", "body": r.text[:200]}


def _path(job_id: str) -> str:
    return f"reviews/{job_id}.json"


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
