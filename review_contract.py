"""Versioned human-review contracts shared by rendering, storage, and Sheets.

Current Washington Gas workbooks use one mutually-exclusive ``Fit`` dropdown:
Optimizer, Periscope, Unclear, or Bad.  Historical Parity batches that genuinely
targeted separate Optimizer/Periscope columns remain readable and submittable.
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
DUAL_FIT_OPTIONS = ["Good", "Bad", "Not Sure"]


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def infer_review_schema(batch: dict[str, Any] | None) -> str:
    """Infer old persisted batches without rewriting their stored JSON.

    An explicit version always wins.  A source ``Fit`` header identifies the
    current contract even if a broken earlier run appended two product columns
    later.  Truly historical dual-column batches keep their old interface.
    Batches with no usable header metadata default to the historical contract;
    every newly-created batch is explicitly stamped as single-fit.
    """
    batch = batch or {}
    explicit = str(batch.get("review_schema") or "")
    if explicit in VALID_REVIEW_SCHEMAS:
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
    return DUAL_FIT_SCHEMA


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
