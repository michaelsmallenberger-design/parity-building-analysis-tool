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
