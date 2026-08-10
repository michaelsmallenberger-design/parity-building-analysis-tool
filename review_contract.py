"""Versioned human-review contracts shared by rendering, storage, and Sheets.

New Parity workbooks use separate ``Optimizer Fit`` and ``Periscope Fit``
columns. Each product is reviewed independently using the source Sheet's five
allowed values. Previously persisted single-``Fit`` batches remain readable
and submittable.
"""
from __future__ import annotations

import re
from typing import Any


SINGLE_FIT_SCHEMA = "single_fit_v1"
DUAL_FIT_SCHEMA = "dual_product_fit_v1"
VALID_REVIEW_SCHEMAS = {SINGLE_FIT_SCHEMA, DUAL_FIT_SCHEMA}

FIT_COL = "Fit"
FIT_OPTIONS = ["Optimizer", "Periscope", "Unclear", "Bad"]

OPT_FIT_COL = "Optimizer Fit"
PERI_FIT_COL = "Periscope Fit"
DUAL_FIT_COLUMNS = [OPT_FIT_COL, PERI_FIT_COL]
DUAL_FIT_OPTIONS = ["Customer", "Good", "Okay", "Bad", "Not Sure"]
CURRENT_REVIEW_SCHEMA = DUAL_FIT_SCHEMA
ALEX_REVIEW_FIT_VALUES = frozenset({"Okay", "Not Sure"})
MACHINE_ATTENTION_VERDICTS = frozenset({
    "ambiguous_footprint",
    "likely_residential",
    "needs_review",
})


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def infer_review_schema(batch: dict[str, Any] | None) -> str:
    """Infer old persisted batches without rewriting their stored JSON.

    An explicit version normally wins. One production release incorrectly
    stamped untouched multi-tab runs as single-Fit even when every selected
    source tab already had the two product columns. Such a run can be upgraded
    safely from its immutable tab inventory, but only before any human decision
    exists. A source ``Fit`` header otherwise keeps a previously persisted
    single-product-choice batch on its original interface.
    """
    batch = batch or {}
    explicit = str(batch.get("review_schema") or "")
    if explicit == DUAL_FIT_SCHEMA:
        return explicit

    selected_inventory_headers = []
    for tab in batch.get("tab_inventory") or []:
        if not isinstance(tab, dict):
            continue
        status = _norm(tab.get("status"))
        if status and status != "selected":
            continue
        headers = list(tab.get("headers") or [])
        if headers:
            selected_inventory_headers.append({_norm(header) for header in headers})

    inventory_proves_dual = bool(selected_inventory_headers) and all(
        all(_norm(column) in headers for column in DUAL_FIT_COLUMNS)
        and _norm(FIT_COL) not in headers
        for headers in selected_inventory_headers
    )
    has_human_decisions = any(
        bool(entry.get("human"))
        for entry in batch.get("entries") or []
        if isinstance(entry, dict)
    )
    if (
        explicit == SINGLE_FIT_SCHEMA
        and inventory_proves_dual
        and not has_human_decisions
    ):
        return DUAL_FIT_SCHEMA
    if explicit == SINGLE_FIT_SCHEMA:
        return explicit

    header_sets: list[list[Any]] = []
    header_sets.extend(
        list(binding.get("headers") or [])
        for binding in batch.get("sheet_bindings") or []
        if isinstance(binding, dict)
    )
    binding = batch.get("sheet_binding")
    if isinstance(binding, dict):
        header_sets.append(list(binding.get("headers") or []))
    header_sets.append(list(batch.get("table_headers") or []))

    normalized = {_norm(header) for headers in header_sets for header in headers}
    if _norm(FIT_COL) in normalized:
        return SINGLE_FIT_SCHEMA
    if all(_norm(column) in normalized for column in DUAL_FIT_COLUMNS):
        return DUAL_FIT_SCHEMA
    return CURRENT_REVIEW_SCHEMA


def human_is_complete(human: dict[str, Any] | None, review_schema: str) -> bool:
    human = human or {}
    if not str(human.get("hvac_systems") or "").strip():
        return False
    if review_schema == DUAL_FIT_SCHEMA:
        return all(
            str(human.get(key) or "").strip()
            for key in ("optimizer_fit", "periscope_fit")
        )
    return bool(str(human.get("fit") or "").strip())


def entry_is_reviewed(entry: dict[str, Any], review_schema: str) -> bool:
    return human_is_complete(entry.get("human"), review_schema)


def entry_has_machine_attention(entry: dict[str, Any] | None) -> bool:
    """Whether the machine result needs a human disposition.

    A completed human review resolves this condition; Sheet write-back errors
    are tracked separately because they remain actionable after review.
    """
    entry = entry or {}
    if str(entry.get("error") or "").strip():
        return True
    if not str(entry.get("address") or "").strip():
        return True
    verdict = _norm(entry.get("verdict"))
    return not verdict or verdict in MACHINE_ATTENTION_VERDICTS


def entry_needs_attention(
    entry: dict[str, Any] | None,
    review_schema: str,
) -> bool:
    """Machine attention that has not yet been human-dispositioned."""
    entry = entry or {}
    return (
        entry_has_machine_attention(entry)
        and not entry_is_reviewed(entry, review_schema)
    )


def human_review_version(entry: dict[str, Any] | None) -> int:
    """Return the optimistic-lock version for one persisted human decision.

    Completed decisions created before version metadata existed are revision 1
    in memory. They are not rewritten until a reviewer performs a new action.
    """
    human = (entry or {}).get("human") or {}
    if not human:
        return 0
    try:
        version = int(human.get("review_version"))
    except (TypeError, ValueError):
        version = 0
    return version if version > 0 else 1


def batch_primary_review_complete(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    review_schema: str,
) -> bool:
    """Whether a dual-fit batch is safe to expose for secondary review."""
    entries = list(entries or [])
    if review_schema != DUAL_FIT_SCHEMA or not entries:
        return False
    if not all(entry_is_reviewed(entry, review_schema) for entry in entries):
        return False
    return not any(
        str((entry.get("writeback") or {}).get("status") or "").casefold()
        == "error"
        for entry in entries
    )


def _secondary_review_matches_current(entry: dict[str, Any]) -> bool:
    secondary = entry.get("secondary_review") or {}
    action = str(secondary.get("action") or "")
    if action not in {"revise", "confirm_uncertain"}:
        return False
    try:
        source_version = int(secondary.get("source_review_version"))
    except (TypeError, ValueError):
        return False
    current_version = human_review_version(entry)
    if action == "confirm_uncertain":
        return source_version == current_version
    human = entry.get("human") or {}
    return (
        str(human.get("review_stage") or "") == "secondary"
        and current_version == source_version + 1
    )


def entry_needs_alex_review(
    entry: dict[str, Any] | None,
    review_schema: str,
) -> bool:
    """Return the derived secondary-review eligibility for one row."""
    entry = entry or {}
    if review_schema != DUAL_FIT_SCHEMA or not entry_is_reviewed(
        entry, review_schema
    ):
        return False
    human = entry.get("human") or {}
    uncertain = any(
        str(human.get(key) or "").strip() in ALEX_REVIEW_FIT_VALUES
        for key in ("optimizer_fit", "periscope_fit")
    )
    return uncertain and not _secondary_review_matches_current(entry)


def alex_review_remaining(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    review_schema: str,
) -> int:
    """Count candidates only after the clean primary-completion gate opens."""
    entries = list(entries or [])
    if not batch_primary_review_complete(entries, review_schema):
        return 0
    return sum(
        1 for entry in entries
        if entry_needs_alex_review(entry, review_schema)
    )
