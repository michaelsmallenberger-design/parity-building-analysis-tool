"""Safe, shared spreadsheet-intake resolution.

Deterministic header matching stays the normal path.  Grok is used only when
there is no recognizable address header, and it returns a *suggestion* that is
validated locally before an interactive user or an automation path may use it.
Raw spreadsheet samples are sent to Grok only for that exceptional call and are
never logged or persisted by this module.
"""
from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - production requirements include openai
    OpenAI = None


log = logging.getLogger("intake")
_metrics = collections.Counter()
_metrics_lock = threading.Lock()
_resolution_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_resolution_cache_lock = threading.Lock()


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _metric(name: str) -> None:
    with _metrics_lock:
        _metrics[name] += 1


def metrics_snapshot() -> dict[str, int]:
    """Return count-only observability data; no Sheet cells or source URLs."""
    with _metrics_lock:
        return dict(_metrics)


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("_", " ").strip().lower())


def schema_fingerprint(tabs: list[dict[str, Any]]) -> str:
    """Hash headers and a bounded sample without retaining the underlying cells."""
    compact = []
    for tab in tabs:
        compact.append({
            "tab": str(tab.get("tab") or ""),
            "headers": [str(h) for h in tab.get("headers", [])],
            "sample": [[str(c) for c in row] for row in tab.get("rows", [])[:sample_row_limit()]],
        })
    return hashlib.sha256(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def sample_row_limit() -> int:
    try:
        return max(1, min(25, int(os.getenv("SHEET_INTAKE_GROK_SAMPLE_ROWS", "25"))))
    except ValueError:
        return 25


def auto_confidence_threshold() -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv("SHEET_INTAKE_GROK_AUTO_CONFIDENCE", "0.90"))))
    except ValueError:
        return 0.90


def _header_lookup(headers: list[Any]) -> dict[str, str] | None:
    lookup: dict[str, str] = {}
    for header in headers:
        original = str(header).strip()
        key = normalize_header(original)
        if not key:
            continue
        if key in lookup:
            return None  # an LLM must not choose between duplicate labels
        lookup[key] = original
    return lookup


def _first_header(headers: list[Any], names: tuple[str, ...]) -> str | None:
    lookup = _header_lookup(headers)
    if lookup is None:
        return None
    for name in names:
        found = lookup.get(normalize_header(name))
        if found is not None:
            return found
    return None


def deterministic_mapping(tabs: list[dict[str, Any]], address_variants: list[str]) -> dict[str, Any] | None:
    """Preserve the historic leftmost-tab rule for ordinary Sheets."""
    # Preserve the configured header-precedence order. A set here would choose
    # unpredictably when a customer happens to have two recognized columns.
    targets = [normalize_header(value) for value in address_variants]
    for tab in tabs:
        headers = [str(h).strip() for h in tab.get("headers", [])]
        lookup = _header_lookup(headers)
        if not lookup:
            continue
        address = next((lookup[name] for name in targets if name in lookup), None)
        if address:
            return {
                "tab": str(tab.get("tab") or ""),
                "address_column": address,
                "city_column": _first_header(headers, ("City", "Borough", "Boro", "Boro Area")),
                "state_column": _first_header(headers, ("State", "Province")),
                "zip_column": _first_header(headers, ("ZIP", "Zip Code", "Postal Code", "Postcode")),
                "confidence": 1.0,
                "method": "deterministic",
            }
    return None


def _tab_for_mapping(tabs: list[dict[str, Any]], mapping: dict[str, Any]) -> dict[str, Any] | None:
    wanted = str(mapping.get("tab") or "")
    return next((tab for tab in tabs if str(tab.get("tab") or "") == wanted), None)


def validate_mapping(tabs: list[dict[str, Any]], mapping: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    """Accept only exact existing headers and a usable composed sample address."""
    if not isinstance(mapping, dict):
        return False, "mapping was not an object", None
    tab = _tab_for_mapping(tabs, mapping)
    if tab is None:
        return False, "suggested tab does not exist", None
    headers = [str(h).strip() for h in tab.get("headers", [])]
    lookup = _header_lookup(headers)
    if lookup is None:
        return False, "selected tab has duplicate normalized header names", None

    normalized: dict[str, Any] = {"tab": str(tab.get("tab") or ""), "method": "grok"}
    for key in ("address_column", "city_column", "state_column", "zip_column"):
        raw = mapping.get(key)
        if raw in (None, "", "null"):
            normalized[key] = None
            continue
        found = lookup.get(normalize_header(raw))
        if found is None:
            return False, f"suggested {key} does not exist", None
        normalized[key] = found
    if not normalized["address_column"]:
        return False, "an address/street column is required", None
    selected = [value for key, value in normalized.items() if key.endswith("_column") and value]
    if len(selected) != len(set(selected)):
        return False, "the same source column was assigned to more than one address field", None
    try:
        confidence = float(mapping.get("confidence"))
    except (TypeError, ValueError):
        return False, "confidence was not numeric", None
    if not 0.0 <= confidence <= 1.0:
        return False, "confidence was outside 0-1", None
    normalized["confidence"] = confidence

    addresses = compose_addresses(headers, tab.get("rows", [])[:sample_row_limit()], normalized)
    populated = [row for row in tab.get("rows", [])[:sample_row_limit()] if any(str(cell).strip() for cell in row)]
    usable_ratio = (sum(bool(value) for value in addresses) / len(populated)) if populated else 0.0
    normalized["usable_sample_ratio"] = usable_ratio
    if not populated or usable_ratio <= 0:
        return False, "the selected columns did not produce a usable sample address", None
    return True, "", normalized


def compose_addresses(headers: list[Any], rows: list[list[Any]], mapping: dict[str, Any]) -> list[str]:
    indices = {str(header).strip(): idx for idx, header in enumerate(headers)}
    fields = ("address_column", "city_column", "state_column", "zip_column")
    wanted = [mapping.get(field) for field in fields if mapping.get(field)]
    values: list[str] = []
    for row in rows:
        parts = []
        for name in wanted:
            idx = indices.get(str(name))
            raw = row[idx] if idx is not None and idx < len(row) else ""
            text = str(raw).strip()
            if text and text.lower() != "nan":
                parts.append(text)
        values.append(", ".join(parts))
    return values


def _grok_enabled() -> bool:
    return _flag("SHEET_INTAKE_GROK_ENABLED", True) and bool(os.getenv("XAI_API_KEY", "").strip()) and OpenAI is not None


def _grok_suggestion(tabs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    if not _grok_enabled():
        return None, "Grok sheet assistance is unavailable"
    _metric("grok_sheet_intake_used")
    payload = [{
        "tab": str(tab.get("tab") or ""),
        "headers": [str(h) for h in tab.get("headers", [])],
        "first_rows": [[str(cell) for cell in row] for row in tab.get("rows", [])[:sample_row_limit()]],
    } for tab in tabs]
    prompt = (
        "Choose the one tab and existing columns that identify a building address. "
        "Use address_column for street/full address; city/state/zip are optional. "
        "Return JSON only with tab, address_column, city_column, state_column, zip_column, "
        "and confidence (0-1). Never invent a tab or column name.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url="https://api.x.ai/v1")
        response = client.chat.completions.create(
            model=os.getenv("SHEET_INTAKE_GROK_MODEL") or os.getenv("GROK_MODEL") or "grok-4.3",
            messages=[
                {"role": "system", "content": "You are a spreadsheet schema mapper. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            reasoning_effort="low",
            timeout=int(os.getenv("SHEET_INTAKE_GROK_TIMEOUT_SECONDS", "30")),
        )
        raw = response.choices[0].message.content
        suggestion = json.loads(raw or "")
    except Exception as exc:  # provider errors must fail closed without raw-cell logs
        log.warning("Grok sheet mapping unavailable: %s", type(exc).__name__)
        return None, "Grok could not identify the Sheet columns"
    return suggestion, ""


def resolve_schema(tabs: list[dict[str, Any]], address_variants: list[str]) -> dict[str, Any]:
    """Return deterministic, suggested, or unresolved schema state.

    ``tabs`` must contain only tab names, headers, and at most the configured
    first sample rows.  The returned object deliberately excludes raw sample
    cells so callers may persist it safely for browser confirmation.
    """
    deterministic = deterministic_mapping(tabs, address_variants)
    fingerprint = schema_fingerprint(tabs)
    if deterministic:
        _metric("deterministic_sheet_mapping")
        return {"status": "deterministic", "mapping": deterministic, "fingerprint": fingerprint,
                "auto_approved": True}
    try:
        cache_ttl = max(60, int(os.getenv("SHEET_INTAKE_RESOLUTION_CACHE_SECONDS", "86400")))
    except ValueError:
        cache_ttl = 86400
    with _resolution_cache_lock:
        cached = _resolution_cache.get(fingerprint)
        if cached and cached[0] > time.time():
            _metric("sheet_mapping_cached")
            return {**cached[1], "fingerprint": fingerprint}
        if cached:
            _resolution_cache.pop(fingerprint, None)
    suggestion, error = _grok_suggestion(tabs)
    if suggestion is None:
        _metric("sheet_mapping_needs_setup")
        result = {"status": "unresolved", "reason": error,
                  "auto_approved": False}
    else:
        valid, reason, mapping = validate_mapping(tabs, suggestion)
        if not valid or mapping is None:
            _metric("sheet_mapping_needs_setup")
            result = {"status": "unresolved", "reason": reason,
                      "auto_approved": False}
        else:
            auto = mapping["confidence"] >= auto_confidence_threshold() and mapping["usable_sample_ratio"] >= 0.90
            _metric("sheet_mapping_auto_approved" if auto else "sheet_mapping_confirmation_required")
            result = {"status": "suggested", "mapping": mapping, "auto_approved": auto}
    # Cache only a schema hash and selected columns/status. Customer sample
    # cells are neither logged nor written to this cache.
    with _resolution_cache_lock:
        _resolution_cache[fingerprint] = (time.time() + cache_ttl, dict(result))
    return {**result, "fingerprint": fingerprint}
