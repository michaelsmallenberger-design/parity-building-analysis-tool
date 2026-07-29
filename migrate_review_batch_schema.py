"""Safely migrate one completed review batch to the dual-product contract.

This utility is deliberately narrow.  It does not call Google APIs, re-run
analysis, alter workbook checkpoints, or edit a Sheet.  It updates only the
persisted review batch after proving that its entries match a completed workbook
manifest and the durable chunk results.

Dry-run is the default.  A migration refuses to guess how a historical single
Fit decision should map to two independent product decisions, so any existing
human decision or write-back stops the operation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SINGLE_FIT_SCHEMA = "single_fit_v1"
DUAL_FIT_SCHEMA = "dual_product_fit_v1"
FIT_COL = "Fit"
HVAC_COL = "HVAC Systems"
OPT_FIT_COL = "Optimizer Fit"
PERI_FIT_COL = "Periscope Fit"
NOTES_COL = "Notes"
TOOL_VERSION = 1

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_COLUMN_ALIASES = {
    HVAC_COL: ("hvac systems", "hvac system", "hvac"),
    OPT_FIT_COL: ("optimizer fit", "optimizer", "optimizer fit?"),
    PERI_FIT_COL: ("periscope fit", "periscope", "periscope fit?"),
    NOTES_COL: ("notes", "note"),
}


class MigrationRefused(RuntimeError):
    """Raised when the stored run does not satisfy the migration invariants."""


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _object_sha256(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value))


def _read_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise MigrationRefused(f"{label} is missing: {path}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationRefused(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise MigrationRefused(f"{label} must contain a JSON object: {path}")
    return value, raw


def _safe_relative_path(storage_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise MigrationRefused(f"{label} is missing")
    root = storage_dir.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MigrationRefused(f"{label} escapes the storage directory") from exc
    return candidate


def _row_id(entry: dict[str, Any]) -> str:
    value = (
        entry.get("row_id")
        if entry.get("row_id") is not None
        else entry.get("i")
    )
    return str(value or "")


def _find_column(headers: list[Any], canonical: str) -> int:
    accepted = {_norm(canonical), *(_norm(v) for v in _COLUMN_ALIASES[canonical])}
    matches = [
        index
        for index, header in enumerate(headers)
        if _norm(header) in accepted
    ]
    if len(matches) != 1:
        raise MigrationRefused(
            f"expected exactly one {canonical!r} column, found {len(matches)}"
        )
    return matches[0]


def _dual_colmap(binding: dict[str, Any]) -> dict[str, int]:
    headers = binding.get("headers")
    if not isinstance(headers, list) or not headers:
        raise MigrationRefused(
            f"tab {binding.get('tab')!r} has no persisted header inventory"
        )
    resolved = {
        canonical: _find_column(headers, canonical)
        for canonical in (HVAC_COL, OPT_FIT_COL, PERI_FIT_COL, NOTES_COL)
    }
    old = binding.get("colmap") or {}
    if not isinstance(old, dict):
        raise MigrationRefused(f"tab {binding.get('tab')!r} has an invalid colmap")
    # Preserve any future/non-review keys, but the obsolete single Fit mapping
    # must not remain: dual write-back passes an empty legacy Fit value.
    updated = {
        str(key): value
        for key, value in old.items()
        if _norm(key) != _norm(FIT_COL)
    }
    updated.update(resolved)
    return updated


def _validate_entries(
    batch: dict[str, Any],
    expected_count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    entries = batch.get("entries")
    if not isinstance(entries, list) or len(entries) != expected_count:
        raise MigrationRefused(
            f"review batch has {len(entries) if isinstance(entries, list) else 'invalid'} "
            f"entries; expected {expected_count}"
        )
    ids: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MigrationRefused(f"review entry {index} is not an object")
        row_id = _row_id(entry)
        if not row_id:
            raise MigrationRefused(f"review entry {index} has no row id")
        ids.append(row_id)
        if not str(entry.get("verdict") or "").strip() and not str(
            entry.get("error") or ""
        ).strip():
            raise MigrationRefused(
                f"review entry {row_id!r} has neither a verdict nor an error"
            )
        if entry.get("human"):
            raise MigrationRefused(
                f"review entry {row_id!r} already has a human decision; "
                "manual reconciliation is required"
            )
        if entry.get("writeback"):
            raise MigrationRefused(
                f"review entry {row_id!r} already has Sheet write-back state; "
                "manual reconciliation is required"
            )
    if len(set(ids)) != len(ids):
        raise MigrationRefused("review batch contains duplicate row ids")
    return entries, ids


def _validate_bindings(
    batch: dict[str, Any],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    bindings = batch.get("sheet_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise MigrationRefused("review batch has no multi-tab Sheet bindings")
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise MigrationRefused("review batch contains an invalid Sheet binding")
        key = (str(binding.get("grid_id")), str(binding.get("tab") or ""))
        if not key[0] or not key[1] or key in seen:
            raise MigrationRefused("Sheet bindings have missing or duplicate tab identities")
        seen.add(key)
        _dual_colmap(binding)
    for entry in entries:
        key = (
            str(entry.get("source_grid_id")),
            str(entry.get("source_tab") or ""),
        )
        if key not in seen:
            raise MigrationRefused(
                f"review entry {_row_id(entry)!r} has no exact source-tab binding"
            )
        try:
            source_row = int(entry.get("source_row"))
        except (TypeError, ValueError) as exc:
            raise MigrationRefused(
                f"review entry {_row_id(entry)!r} has an invalid source row"
            ) from exc
        if source_row < 2:
            raise MigrationRefused(
                f"review entry {_row_id(entry)!r} targets a header row"
            )
    return bindings


def _validate_manifest(
    storage_dir: Path,
    run_id: str,
    manifest: dict[str, Any],
    expected_count: int,
    batch_ids: list[str],
) -> dict[str, Any]:
    if str(manifest.get("run_id") or "") != run_id:
        raise MigrationRefused("workbook manifest run id does not match")
    if int(manifest.get("row_count") or -1) != expected_count:
        raise MigrationRefused("workbook manifest row count does not match")
    if int(manifest.get("completed_rows") or -1) != expected_count:
        raise MigrationRefused("workbook analysis is not complete")
    if int(manifest.get("reviewed_rows") or 0) != 0:
        raise MigrationRefused("workbook manifest reports existing human reviews")
    if str(manifest.get("status") or "") not in {"review_open", "complete"}:
        raise MigrationRefused(
            f"workbook status is {manifest.get('status')!r}, not review_open/complete"
        )
    target_order = [str(value) for value in manifest.get("target_order") or []]
    if target_order != batch_ids:
        raise MigrationRefused(
            "review entry order does not exactly match the workbook target order"
        )

    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise MigrationRefused("workbook manifest has no durable chunks")
    checkpoint_ids: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or chunk.get("status") != "complete":
            raise MigrationRefused("not every workbook chunk is complete")
        result_path = _safe_relative_path(
            storage_dir, chunk.get("result_blob"), "chunk result path"
        )
        result, _raw = _read_object(result_path, "chunk result")
        chunk_entries = result.get("entries")
        if not isinstance(chunk_entries, list):
            raise MigrationRefused(f"chunk result has no entries: {result_path}")
        for entry in chunk_entries:
            if not isinstance(entry, dict):
                raise MigrationRefused(f"chunk result has an invalid entry: {result_path}")
            checkpoint_ids.append(str(entry.get("source_key") or _row_id(entry)))
    if (
        len(checkpoint_ids) != len(batch_ids)
        or len(set(checkpoint_ids)) != len(checkpoint_ids)
        or set(checkpoint_ids) != set(batch_ids)
    ):
        raise MigrationRefused(
            "durable chunk results do not exactly match the review batch rows"
        )
    return {
        "status": manifest.get("status"),
        "chunk_count": len(chunks),
        "manifest_sha256": _object_sha256(manifest),
    }


def build_migration(
    storage_dir: str | os.PathLike[str],
    run_id: str,
    expected_count: int,
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, Any]]:
    """Validate and prepare a migration without changing disk state."""
    if not _RUN_ID.fullmatch(str(run_id or "")):
        raise MigrationRefused("run id contains invalid characters")
    if expected_count <= 0:
        raise MigrationRefused("expected count must be positive")

    root = Path(storage_dir).resolve()
    review_path = root / "reviews" / f"{run_id}.json"
    manifest_path = root / "workbook_runs" / run_id / "manifest.json"
    batch, batch_raw = _read_object(review_path, "review batch")
    manifest, _manifest_raw = _read_object(manifest_path, "workbook manifest")
    if str(batch.get("job_id") or "") != run_id:
        raise MigrationRefused("review batch job id does not match")
    if str(batch.get("run_id") or "") != run_id:
        raise MigrationRefused("review batch run id does not match")
    current_schema = str(batch.get("review_schema") or "")
    if current_schema not in {SINGLE_FIT_SCHEMA, DUAL_FIT_SCHEMA}:
        raise MigrationRefused(
            f"review schema is {current_schema!r}, not an explicit supported schema"
        )

    entries, batch_ids = _validate_entries(batch, expected_count)
    bindings = _validate_bindings(batch, entries)
    manifest_receipt = _validate_manifest(
        root, run_id, manifest, expected_count, batch_ids
    )

    migrated = copy.deepcopy(batch)
    migrated["review_schema"] = DUAL_FIT_SCHEMA
    for binding in migrated["sheet_bindings"]:
        binding["colmap"] = _dual_colmap(binding)

    # Entries carry the paid analysis output and are intentionally byte-for-byte
    # equivalent as JSON values across this metadata-only migration.
    if migrated.get("entries") != batch.get("entries"):
        raise AssertionError("migration attempted to alter analysis entries")

    changed = migrated != batch
    if changed:
        migrated["review_schema_migration"] = {
            "tool_version": TOOL_VERSION,
            "from": current_schema,
            "to": DUAL_FIT_SCHEMA,
            "expected_rows": expected_count,
            "analysis_entries_sha256": _object_sha256(entries),
            "source_review_sha256": _sha256_bytes(batch_raw),
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }

    receipt = {
        "run_id": run_id,
        "dry_run": True,
        "would_change": changed,
        "from_schema": current_schema,
        "to_schema": DUAL_FIT_SCHEMA,
        "entry_count": len(entries),
        "human_decision_count": 0,
        "writeback_count": 0,
        "binding_count": len(bindings),
        "analysis_entries_sha256": _object_sha256(entries),
        "source_review_sha256": _sha256_bytes(batch_raw),
        **manifest_receipt,
    }
    return migrated, receipt, batch_raw, batch


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def migrate(
    storage_dir: str | os.PathLike[str],
    run_id: str,
    expected_count: int,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Dry-run or atomically apply the validated metadata-only migration."""
    root = Path(storage_dir).resolve()
    migrated, receipt, source_raw, source_batch = build_migration(
        root, run_id, expected_count
    )
    if not apply or not receipt["would_change"]:
        return receipt

    review_path = root / "reviews" / f"{run_id}.json"
    # Refuse a stale write if the live batch changed after validation.
    current_raw = review_path.read_bytes()
    if _sha256_bytes(current_raw) != receipt["source_review_sha256"]:
        raise MigrationRefused("review batch changed during migration; nothing was written")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = (
        root / "migration_backups" / run_id / timestamp / "review_batch.json"
    )
    _atomic_write(backup_path, source_raw)
    migrated_raw = _json_bytes(migrated)
    try:
        _atomic_write(review_path, migrated_raw)
        verified, _verified_raw = _read_object(review_path, "migrated review batch")
        if verified.get("review_schema") != DUAL_FIT_SCHEMA:
            raise MigrationRefused("post-write schema verification failed")
        if verified.get("entries") != source_batch.get("entries"):
            raise MigrationRefused("post-write analysis-entry verification failed")
        _validate_entries(verified, expected_count)
        _validate_bindings(verified, verified["entries"])
    except Exception:
        _atomic_write(review_path, source_raw)
        raise

    receipt.update(
        {
            "dry_run": False,
            "applied": True,
            "backup_path": str(backup_path),
            "migrated_review_sha256": _sha256_bytes(migrated_raw),
        }
    )
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and migrate one completed review batch from single Fit "
            "to separate Optimizer/Periscope fields. Dry-run is the default."
        )
    )
    parser.add_argument("run_id")
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument(
        "--storage-dir",
        default=os.getenv("STORAGE_DIR", "storage"),
        help="storage root (defaults to STORAGE_DIR or ./storage)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the migration after all checks pass",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        receipt = migrate(
            args.storage_dir,
            args.run_id,
            args.expected_count,
            apply=args.apply,
        )
    except MigrationRefused as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps({"ok": True, **receipt}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
