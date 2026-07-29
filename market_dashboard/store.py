"""Validation and durable snapshot storage for the market-runway dashboard."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_ROWS = 5_000
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{8,120}$")

SUMMARY_FIELDS = (
    "total_properties",
    "optimizer_good_fits",
    "periscope_good_fits",
    "current_customers",
    "tam_properties",
    "sam_properties",
    "tam_revenue",
    "sam_revenue",
    "current_revenue",
    "region_properties",
    "region_opt_fits",
    "region_per_fits",
)

ASSUMPTION_FIELDS = (
    "som_percent",
    "cad_rate",
    "opt_install",
    "opt_arr_annual",
    "opt_contract",
    "per_install",
    "per_arr_annual",
    "per_contract",
    "sync_arr_annual",
    "sync_contract",
)

METRO_FIELDS = (
    "metro",
    "opt",
    "per",
    "cust",
    "optRev",
    "perRev",
    "syncRev",
    "totalRev",
)

COMPANY_FIELDS = (
    "company",
    "total",
    "opt",
    "per",
    "cust",
    "optRev",
    "perRev",
    "totalRev",
    "penetration",
)

COMPANY_REGION_FIELDS = (
    "company",
    "total",
    "opt",
    "per",
    "cust",
    "penetration",
    "optRev",
    "perRev",
    "syncRev",
    "totalRev",
)


class SnapshotValidationError(ValueError):
    """Raised when a Sheet publish payload is unsafe or structurally invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _version_stamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).strftime("%Y%m%dT%H%M%S%f")


def _clean_text(value: Any, field: str, *, required: bool = True, limit: int = 240) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise SnapshotValidationError(f"{field} is required")
    if len(text) > limit:
        raise SnapshotValidationError(f"{field} is longer than {limit} characters")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise SnapshotValidationError(f"{field} contains control characters")
    return text


def _number(
    value: Any,
    field: str,
    *,
    minimum: float = 0,
    maximum: float = 10_000_000_000_000,
) -> int | float:
    if isinstance(value, bool):
        raise SnapshotValidationError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise SnapshotValidationError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    if number.is_integer():
        return int(number)
    return number


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{field} must be an object")
    return value


def _require_keys(mapping: dict[str, Any], required: tuple[str, ...], field: str) -> None:
    missing = [key for key in required if key not in mapping]
    if missing:
        raise SnapshotValidationError(f"{field} is missing: {', '.join(missing)}")


def _validate_summary(raw: Any) -> dict[str, int | float]:
    source = _require_mapping(raw, "summary")
    _require_keys(source, SUMMARY_FIELDS, "summary")
    result = {
        key: _number(source[key], f"summary.{key}")
        for key in SUMMARY_FIELDS
    }
    integer_fields = {
        "total_properties",
        "optimizer_good_fits",
        "periscope_good_fits",
        "current_customers",
        "tam_properties",
        "sam_properties",
        "region_properties",
        "region_opt_fits",
        "region_per_fits",
    }
    for key in integer_fields:
        if not isinstance(result[key], int):
            raise SnapshotValidationError(f"summary.{key} must be a whole number")
    if result["tam_properties"] != result["total_properties"]:
        raise SnapshotValidationError("TAM properties must equal total properties")
    if result["sam_properties"] != result["region_opt_fits"] + result["region_per_fits"]:
        raise SnapshotValidationError(
            "SAM properties must equal regional Optimizer plus Periscope fits"
        )
    if result["current_revenue"] > result["sam_revenue"]:
        raise SnapshotValidationError("current revenue cannot exceed SAM revenue")
    return result


def _validate_assumptions(raw: Any) -> dict[str, int | float]:
    source = _require_mapping(raw, "assumptions")
    _require_keys(source, ASSUMPTION_FIELDS, "assumptions")
    result = {
        key: _number(
            source[key],
            f"assumptions.{key}",
            minimum=0.01 if key == "cad_rate" else 0,
            maximum=100 if key == "som_percent" else 10_000_000,
        )
        for key in ASSUMPTION_FIELDS
    }
    return result


def _validate_table(
    raw: Any,
    field: str,
    fields: tuple[str, ...],
    label_key: str,
) -> list[dict[str, int | float | str]]:
    if not isinstance(raw, list):
        raise SnapshotValidationError(f"{field} must be an array")
    if not raw:
        raise SnapshotValidationError(f"{field} must have at least one row")
    if len(raw) > MAX_ROWS:
        raise SnapshotValidationError(f"{field} exceeds {MAX_ROWS} rows")

    result: list[dict[str, int | float | str]] = []
    labels: set[str] = set()
    for index, item in enumerate(raw, start=2):
        row = _require_mapping(item, f"{field} row {index}")
        _require_keys(row, fields, f"{field} row {index}")
        cleaned: dict[str, int | float | str] = {}
        for key in fields:
            if key == label_key:
                label = _clean_text(row[key], f"{field} row {index} {key}", limit=160)
                normalized = label.casefold()
                if normalized in labels:
                    raise SnapshotValidationError(f"{field} contains duplicate {label_key}: {label}")
                labels.add(normalized)
                cleaned[key] = label
            elif key == "penetration":
                cleaned[key] = _number(
                    row[key], f"{field} row {index} {key}", maximum=100
                )
            else:
                cleaned[key] = _number(row[key], f"{field} row {index} {key}")

        revenue_parts = [
            float(cleaned.get("optRev", 0)),
            float(cleaned.get("perRev", 0)),
            float(cleaned.get("syncRev", 0)),
        ]
        total_revenue = float(cleaned.get("totalRev", 0))
        if abs(sum(revenue_parts) - total_revenue) > max(2, total_revenue * 0.000001):
            raise SnapshotValidationError(
                f"{field} row {index} totalRev does not equal its revenue components"
            )
        result.append(cleaned)
    return result


def validate_snapshot(payload: Any) -> dict[str, Any]:
    """Return a normalized snapshot or raise a precise validation error."""
    source = _require_mapping(payload, "payload")
    if source.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotValidationError(
            f"schema_version must be {SCHEMA_VERSION}"
        )

    source_meta = _require_mapping(source.get("source"), "source")
    normalized_source = {
        "spreadsheet_id": _clean_text(
            source_meta.get("spreadsheet_id"),
            "source.spreadsheet_id",
            limit=180,
        ),
        "spreadsheet_title": _clean_text(
            source_meta.get("spreadsheet_title"),
            "source.spreadsheet_title",
            limit=180,
        ),
        "published_at": _clean_text(
            source_meta.get("published_at"),
            "source.published_at",
            limit=50,
        ),
        "published_by": _clean_text(
            source_meta.get("published_by"),
            "source.published_by",
            required=False,
            limit=180,
        )
        or "Google Sheet publisher",
        "publish_note": _clean_text(
            source_meta.get("publish_note"),
            "source.publish_note",
            required=False,
            limit=500,
        ),
    }

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "source": normalized_source,
        "assumptions": _validate_assumptions(source.get("assumptions")),
        "summary": _validate_summary(source.get("summary")),
        "metros": _validate_table(
            source.get("metros"), "metros", METRO_FIELDS, "metro"
        ),
        "companies": _validate_table(
            source.get("companies"), "companies", COMPANY_FIELDS, "company"
        ),
        "company_regions": _validate_table(
            source.get("company_regions"),
            "company_regions",
            COMPANY_REGION_FIELDS,
            "company",
        ),
    }

    expected_sam = (
        normalized["summary"]["region_opt_fits"]
        * (
            normalized["assumptions"]["opt_contract"]
            + normalized["assumptions"]["sync_contract"]
        )
        + normalized["summary"]["region_per_fits"]
        * normalized["assumptions"]["per_contract"]
    )
    actual_sam = normalized["summary"]["sam_revenue"]
    if abs(float(expected_sam) - float(actual_sam)) > max(2, float(actual_sam) * 0.000001):
        raise SnapshotValidationError(
            "SAM revenue does not match the regional fits and contract assumptions"
        )
    return normalized


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def snapshot_content_hash(snapshot: dict[str, Any]) -> str:
    """Hash business content while ignoring publish-attempt metadata."""
    material = deepcopy(snapshot)
    material["source"].pop("published_at", None)
    material["source"].pop("published_by", None)
    return hashlib.sha256(canonical_json(material)).hexdigest()


class ReplayGuard:
    """Durable nonce registry that rejects replayed publish requests."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publish_nonces (
                    nonce TEXT PRIMARY KEY,
                    request_timestamp INTEGER NOT NULL,
                    used_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def use_once(self, nonce: str, request_timestamp: int) -> bool:
        cutoff = request_timestamp - 86_400
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM publish_nonces WHERE request_timestamp < ?",
                (cutoff,),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO publish_nonces (nonce, request_timestamp, used_at)
                    VALUES (?, ?, ?)
                    """,
                    (nonce, request_timestamp, _iso_utc()),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            conn.commit()
        return True


class SnapshotStore:
    """Atomic, append-only snapshot store with explicit rollback versions."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.snapshots_dir = self.root / "snapshots"
        self.current_path = self.root / "current.json"
        self._lock = threading.RLock()
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _version_path(self, version_id: str) -> Path:
        if not _VERSION_RE.fullmatch(version_id):
            raise ValueError("invalid version id")
        path = (self.snapshots_dir / f"{version_id}.json").resolve()
        if self.snapshots_dir not in path.parents:
            raise ValueError("invalid version path")
        return path

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.current_path.exists():
                return None
            with self.current_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

    def publish(self, payload: Any) -> tuple[dict[str, Any], bool]:
        normalized = validate_snapshot(payload)
        body_hash = snapshot_content_hash(normalized)
        now = _utc_now()
        received_at = _iso_utc(now)
        stamp = _version_stamp(now)
        version_id = f"{stamp}-{body_hash[:12]}"

        with self._lock:
            current = self.current()
            if current and current.get("content_hash") == body_hash:
                return current, False

            record = {
                "version_id": version_id,
                "content_hash": body_hash,
                "received_at": received_at,
                "published_at": normalized["source"]["published_at"],
                "source": deepcopy(normalized["source"]),
                "rollback_of": None,
                "data": normalized,
            }
            version_path = self._version_path(version_id)
            if version_path.exists():
                with version_path.open("r", encoding="utf-8") as handle:
                    return json.load(handle), False
            self._atomic_json(version_path, record)
            self._atomic_json(self.current_path, record)
            return record, True

    def versions(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with self._lock:
            for path in self.snapshots_dir.glob("*.json"):
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        record = json.load(handle)
                    items.append(
                        {
                            "version_id": record["version_id"],
                            "content_hash": record["content_hash"],
                            "received_at": record["received_at"],
                            "published_at": record.get("published_at"),
                            "source": record.get("source", {}),
                            "rollback_of": record.get("rollback_of"),
                        }
                    )
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        return sorted(items, key=lambda item: item["received_at"], reverse=True)

    def rollback(self, version_id: str) -> dict[str, Any]:
        source_path = self._version_path(version_id)
        with self._lock:
            if not source_path.exists():
                raise FileNotFoundError(version_id)
            with source_path.open("r", encoding="utf-8") as handle:
                source = json.load(handle)
            data = validate_snapshot(source["data"])
            content_hash = snapshot_content_hash(data)
            now = _utc_now()
            received_at = _iso_utc(now)
            stamp = _version_stamp(now)
            new_version_id = f"{stamp}-rollback-{content_hash[:8]}"
            record = {
                "version_id": new_version_id,
                "content_hash": content_hash,
                "received_at": received_at,
                "published_at": source.get("published_at"),
                "source": deepcopy(source.get("source", {})),
                "rollback_of": version_id,
                "data": data,
            }
            self._atomic_json(self._version_path(new_version_id), record)
            self._atomic_json(self.current_path, record)
            return record
