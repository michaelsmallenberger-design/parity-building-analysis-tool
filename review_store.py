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
    batch_primary_review_complete,
    entry_needs_alex_review,
    entry_is_reviewed,
    human_review_version,
    infer_review_schema,
    is_dual_fit_schema,
)


_locks_guard = threading.Lock()
_batch_locks = {}
_MAX_HUMAN_REVISIONS = 20


class ReviewConflict(Exception):
    """A persisted review changed or no longer satisfies the requested stage."""

    def __init__(self, message: str, current_review_version: int | None = None):
        super().__init__(message)
        self.current_review_version = current_review_version


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


def _decision_matches(human: dict, decision: dict, review_schema: str) -> bool:
    keys = ["hvac_systems", "note"]
    if is_dual_fit_schema(review_schema):
        keys.extend(["optimizer_fit", "periscope_fit"])
    else:
        keys.append("fit")
    return all(
        str(human.get(key) or "") == str(decision.get(key) or "")
        for key in keys
    )


def _strict_review_version(human: dict) -> int:
    if "review_version" not in human:
        return 1
    value = human.get("review_version")
    if isinstance(value, int) and not isinstance(value, bool):
        version = value
    elif isinstance(value, str) and value.strip().isdigit():
        version = int(value.strip())
    else:
        raise ReviewConflict("invalid persisted review version")
    if version < 1:
        raise ReviewConflict("invalid persisted review version")
    return version


def _bounded_revisions(existing, snapshot: dict) -> list[dict]:
    revisions = [
        dict(item) for item in (existing or []) if isinstance(item, dict)
    ]
    revisions.append(dict(snapshot))
    if len(revisions) <= _MAX_HUMAN_REVISIONS:
        return revisions
    return [revisions[0], *revisions[-(_MAX_HUMAN_REVISIONS - 1):]]


def record_decision(
    job_id: str,
    row_id,
    decision: dict,
    *,
    versioned: bool = False,
    protect_completed: bool = False,
):
    """Stamp a reviewer's decision onto the matching entry so a page reload shows
    it as already reviewed. Returns the updated batch dict (so the caller can
    reach sheet_url/table without re-reading the JSON), or None if the batch/row
    is unknown."""
    with _lock_for(job_id):
        batch = load_batch(job_id)
        if not batch:
            return None
        matches = [
            entry for entry in batch.get("entries", [])
            if _rid(entry) == str(row_id)
        ]
        if not matches:
            return None
        if versioned and len(matches) != 1:
            raise ReviewConflict("review row identity is not unique")
        review_schema = infer_review_schema(batch)
        if not versioned:
            for entry in matches:
                entry["human"] = {
                    **decision,
                    "reviewed_at": datetime.utcnow().isoformat(),
                }
            write_json(_path(job_id), batch)
            return batch

        entry = matches[0]
        existing = entry.get("human") or {}
        complete = entry_is_reviewed(entry, review_schema)
        identical = bool(existing) and _decision_matches(
            existing, decision, review_schema
        )
        if (
            complete
            and protect_completed
            and str(existing.get("review_stage") or "primary") == "secondary"
        ):
            raise ReviewConflict(
                "secondary reviews cannot be submitted through the primary endpoint",
                human_review_version(entry),
            )
        if complete and protect_completed and not identical:
            raise ReviewConflict(
                "completed reviews require the secondary review controls",
                human_review_version(entry),
            )
        if (
            complete
            and protect_completed
            and identical
            and str((entry.get("writeback") or {}).get("status") or "")
            not in {"error", "pending"}
        ):
            raise ReviewConflict(
                "completed review has no failed Sheet write-back to retry",
                human_review_version(entry),
            )
        if identical:
            return batch
        version = (
            _strict_review_version(existing) + 1
            if existing and (complete or "review_version" in existing)
            else 1
        )
        entry["human"] = {
            **decision,
            "reviewed_at": datetime.utcnow().isoformat(),
            "review_version": version,
            "review_stage": "primary",
        }
        entry.pop("secondary_review", None)
        write_json(_path(job_id), batch)
        return batch


def record_secondary_review(
    job_id: str,
    row_id,
    *,
    action: str,
    expected_review_version: int,
    decision: dict | None = None,
    sheet_write_required: bool = False,
):
    """CAS-protected secondary action with bounded, original-first history.

    The exact eligibility and version checks occur under the same batch lock as
    the mutation. An identical failed Sheet retry is the only allowed exception
    to the clean-completion gate, and it never adds another history snapshot.
    """
    with _lock_for(job_id):
        batch = load_batch(job_id)
        if not batch:
            return None
        matches = [
            entry for entry in batch.get("entries", [])
            if _rid(entry) == str(row_id)
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ReviewConflict("review row identity is not unique")
        entry = matches[0]
        review_schema = infer_review_schema(batch)
        if not is_dual_fit_schema(review_schema) or not entry_is_reviewed(
            entry, review_schema
        ):
            raise ReviewConflict("row is not eligible for secondary review")
        human = entry.get("human") or {}
        current_version = _strict_review_version(human)
        if expected_review_version != current_version:
            raise ReviewConflict("review changed in another tab", current_version)

        secondary = entry.get("secondary_review") or {}
        writeback_status = str(
            (entry.get("writeback") or {}).get("status") or ""
        ).casefold()
        retry_decision = decision or {}
        try:
            retry_source_version = int(secondary.get("source_review_version"))
        except (TypeError, ValueError):
            retry_source_version = -1
        is_retry = (
            action == "revise"
            and writeback_status in {"error", "pending"}
            and str(human.get("review_stage") or "") == "secondary"
            and str(secondary.get("action") or "") == "revise"
            and retry_source_version + 1 == current_version
            and _decision_matches(
                human,
                {
                    "hvac_systems": human.get("hvac_systems", ""),
                    "optimizer_fit": retry_decision.get("optimizer_fit", ""),
                    "periscope_fit": retry_decision.get("periscope_fit", ""),
                    "note": retry_decision.get("note", ""),
                },
                review_schema,
            )
        )
        if is_retry:
            if sheet_write_required:
                entry["writeback"] = {
                    "status": "pending",
                    "error": "",
                    "updated_at": datetime.utcnow().isoformat(),
                }
                write_json(_path(job_id), batch)
            return {
                "batch": batch,
                "entry": entry,
                "is_retry": True,
                "review_version": current_version,
            }

        if not batch_primary_review_complete(
            batch.get("entries", []), review_schema
        ):
            raise ReviewConflict(
                "secondary review opens only after clean primary completion",
                current_version,
            )
        if not entry_needs_alex_review(entry, review_schema):
            raise ReviewConflict(
                "row is no longer in Needs Alex Review",
                current_version,
            )

        now = datetime.utcnow().isoformat()
        if action == "confirm_uncertain":
            entry["secondary_review"] = {
                "action": action,
                "completed_at": now,
                "source_review_version": current_version,
            }
        elif action == "revise":
            snapshot = dict(human)
            snapshot.setdefault("review_version", current_version)
            snapshot.setdefault("review_stage", "primary")
            entry["human_revisions"] = _bounded_revisions(
                entry.get("human_revisions"), snapshot
            )
            entry["human"] = {
                **human,
                "optimizer_fit": retry_decision.get("optimizer_fit", ""),
                "periscope_fit": retry_decision.get("periscope_fit", ""),
                "note": retry_decision.get("note", ""),
                "reviewed_at": now,
                "review_version": current_version + 1,
                "review_stage": "secondary",
            }
            entry["secondary_review"] = {
                "action": action,
                "completed_at": now,
                "source_review_version": current_version,
            }
            if sheet_write_required:
                entry["writeback"] = {
                    "status": "pending",
                    "error": "",
                    "updated_at": now,
                }
        else:
            raise ReviewConflict("invalid secondary review action", current_version)

        write_json(_path(job_id), batch)
        return {
            "batch": batch,
            "entry": entry,
            "is_retry": False,
            "review_version": human_review_version(entry),
        }


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
