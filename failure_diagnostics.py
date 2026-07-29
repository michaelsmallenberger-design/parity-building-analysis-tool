"""Safe, optional LLM explanations for run-level failures.

Only a small deterministic packet is sent to Gemini or Grok. Customer rows,
addresses, Sheet contents, credentials, URLs with query strings, and raw logs
are deliberately excluded.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("failure_diagnostics")

_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9 .,'#'\-]{2,60}\b"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Court|Ct|Parkway|Pkwy|Place|Pl|Square|Highway|Hwy)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_RE = re.compile(
    r"(?i)\b(api[_ -]?key|token|secret|password|authorization)\b"
    r"\s*[:=]\s*[^\s,;]+"
)
_GOOGLE_DOC_ID_RE = re.compile(r"(?i)(/d/)[A-Za-z0-9_-]{20,}")
_LONG_ID_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")
_SPACE_RE = re.compile(r"\s+")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def sanitize_failure_text(value: Any, limit: int = 600) -> str:
    """Remove likely customer data and secrets from an exception message."""
    text = str(value or "").replace("\x00", " ")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _GOOGLE_DOC_ID_RE.sub(r"\1[REDACTED]", text)

    def redact_url(match: re.Match) -> str:
        url = match.group(0)
        clean = url.split("?", 1)[0].split("#", 1)[0]
        return f"{clean}?[REDACTED]"

    text = _URL_RE.sub(redact_url, text)
    text = _STREET_RE.sub("[REDACTED ADDRESS]", text)
    text = _LONG_ID_RE.sub("[REDACTED ID]", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        return "No safe error message was available."
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _state_counts(rows: list[dict[str, Any]] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state") or "unknown").lower()
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


def _code_pointers(stage: str, error_text: str) -> list[str]:
    pointers = ["worker.py", "job_queue.py", "storage_helpers.py"]
    haystack = f"{stage} {error_text}".lower()
    if "workbook" in haystack or "manifest" in haystack or "chunk" in haystack:
        pointers.extend(["workbook_runs.py", "sheets_writer.py"])
    if "sheet" in haystack or "google" in haystack or "writeback" in haystack:
        pointers.append("sheets_writer.py")
    if "schema" in haystack or "column" in haystack or "mapping" in haystack:
        pointers.append("intake_resolver.py")
    if any(word in haystack for word in ("gemini", "grok", "vlm", "model")):
        pointers.extend(["tasks_local.py", "vlm.py"])
    if any(word in haystack for word in ("image", "mapbox", "geocod", "footprint", "yolo")):
        pointers.extend(["tasks_local.py", "utils.py", "geometry.py"])
    return list(dict.fromkeys(pointers))


def _fallback_summary(stage: str, error_type: str, error_text: str) -> str:
    return (
        f"The run stopped in {stage} with {error_type}. "
        f"Start with the recorded error and the listed code pointers. "
        f"Safe error: {error_text}"
    )


def _gemini_summary(packet: dict[str, Any]) -> tuple[str, str]:
    from google import genai
    from google.genai import types

    model = (
        os.getenv("FAILURE_DIAGNOSTICS_GEMINI_MODEL")
        or os.getenv("GEMINI_MODEL")
        or "gemini-3.6-flash"
    )
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = (
        "Explain this sanitized application failure to a software engineer in "
        "at most 5 short sentences. State the likely failure class, the first "
        "two checks to perform, and whether retrying is reasonable. Do not "
        "invent customer data, addresses, logs, commands, or a confirmed root "
        "cause. This is advisory only.\n\n"
        + json.dumps(packet, ensure_ascii=True, sort_keys=True)
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=260,
            http_options=types.HttpOptions(
                timeout=int(os.getenv("FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS", "12"))
                * 1000
            ),
        ),
    )
    text = getattr(response, "text", "") or ""
    if not text.strip():
        raise ValueError("Gemini returned an empty diagnostic")
    return sanitize_failure_text(text, limit=900), model


def _grok_summary(packet: dict[str, Any]) -> tuple[str, str]:
    from openai import OpenAI

    model = (
        os.getenv("FAILURE_DIAGNOSTICS_GROK_MODEL")
        or os.getenv("GROK_MODEL")
        or "grok-4.3"
    )
    client = OpenAI(
        api_key=os.environ["XAI_API_KEY"],
        base_url="https://api.x.ai/v1",
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You explain sanitized application failures. Be concise, "
                    "advisory, and explicit about uncertainty."
                ),
            },
            {
                "role": "user",
                "content": (
                    "In at most 5 short sentences, state the likely failure "
                    "class, the first two checks, and whether retrying is "
                    "reasonable. Do not invent customer data, addresses, logs, "
                    "commands, or a confirmed root cause.\n\n"
                    + json.dumps(packet, ensure_ascii=True, sort_keys=True)
                ),
            },
        ],
        reasoning_effort="low",
        max_tokens=260,
        timeout=int(os.getenv("FAILURE_DIAGNOSTICS_TIMEOUT_SECONDS", "12")),
    )
    text = response.choices[0].message.content or ""
    if not text.strip():
        raise ValueError("Grok returned an empty diagnostic")
    return sanitize_failure_text(text, limit=900), model


def _provider_order(error_text: str) -> list[str]:
    lower = error_text.lower()
    gemini_implicated = any(
        marker in lower
        for marker in ("gemini", "google_genai", "generativelanguage")
    )
    if gemini_implicated:
        return ["grok", "gemini"]
    return ["gemini", "grok"]


def _failure_signals(stage: str, error_text: str) -> list[str]:
    """Return only coarse allow-listed signals suitable for an external model."""
    haystack = f"{stage} {error_text}".lower()
    vocabulary = {
        "authentication": ("401", "403", "auth", "credential", "permission"),
        "rate_limit": ("429", "rate limit", "quota"),
        "timeout": ("timeout", "timed out", "deadline"),
        "network": ("connect", "network", "dns", "socket"),
        "provider_gemini": ("gemini", "google_genai", "generativelanguage"),
        "provider_grok": ("grok", "xai"),
        "sheet_access": ("sheet", "drive", "writeback"),
        "schema_mapping": ("schema", "column", "mapping", "header"),
        "workbook_checkpoint": ("workbook", "manifest", "chunk", "checkpoint"),
        "imagery": ("image", "imagery", "mapbox", "satellite"),
        "geocoding": ("geocod", "latitude", "longitude"),
        "model_inference": ("yolo", "model", "vlm", "inference"),
        "storage": ("storage", "file", "sqlite", "database"),
    }
    return [
        label
        for label, markers in vocabulary.items()
        if any(marker in haystack for marker in markers)
    ] or ["unclassified"]


def _llm_safe_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Create an allow-listed packet with no free-form exception text."""
    return {
        "schema_version": packet["schema_version"],
        "stage": packet["stage"],
        "status": packet["status"],
        "error_type": packet["error_type"],
        "failure_signals": packet["failure_signals"],
        "progress": packet["progress"],
        "total": packet["total"],
        "row_state_counts": packet["row_state_counts"],
        "code_pointers": packet["code_pointers"],
    }


def _claude_context(packet: dict[str, Any], summary: str, model: str) -> str:
    counts = json.dumps(packet["row_state_counts"], sort_keys=True)
    pointers = ", ".join(packet["code_pointers"])
    return "\n".join(
        [
            "Investigate this Parity analyzer run failure.",
            "Treat all supplied details as sanitized and verify the root cause in current code/logs.",
            f"Run ID: {packet['run_id']}",
            f"Stage: {packet['stage']}",
            f"Failure: {packet['error_type']}: {packet['error_message']}",
            f"Progress: {packet['progress']} / {packet['total']}",
            f"Row states: {counts}",
            f"Start with: {pointers}",
            f"Diagnostic source: {model}",
            f"Advisory summary: {summary}",
            "Do not expose secrets or customer cell contents. Diagnose first; do not mutate production data.",
        ]
    )


def build_failure_diagnostic(
    *,
    run_id: str,
    stage: str,
    error: Exception | str,
    progress: int = 0,
    total: int = 0,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic packet and optionally add a safe LLM explanation."""
    error_type = type(error).__name__ if isinstance(error, Exception) else "RunError"
    error_text = sanitize_failure_text(error)
    packet = {
        "schema_version": 1,
        "run_id": sanitize_failure_text(run_id, limit=100),
        "stage": sanitize_failure_text(stage, limit=100),
        "status": "failed",
        "error_type": sanitize_failure_text(error_type, limit=100),
        "error_message": error_text,
        "progress": max(int(progress or 0), 0),
        "total": max(int(total or 0), 0),
        "row_state_counts": _state_counts(rows),
        "code_pointers": _code_pointers(stage, error_text),
        "failure_signals": _failure_signals(stage, error_text),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    summary = _fallback_summary(packet["stage"], packet["error_type"], error_text)
    source = "deterministic"

    if _flag("FAILURE_DIAGNOSTICS_LLM_ENABLED", True):
        for provider in _provider_order(error_text):
            if provider == "gemini" and os.getenv("GEMINI_API_KEY", "").strip():
                call = _gemini_summary
            elif provider == "grok" and os.getenv("XAI_API_KEY", "").strip():
                call = _grok_summary
            else:
                continue
            try:
                summary, model = call(_llm_safe_packet(packet))
                source = f"{provider}:{model}"
                break
            except Exception as exc:
                # Never log the prompt or raw provider response.
                log.warning(
                    "%s failure diagnostic unavailable: %s",
                    provider.capitalize(),
                    type(exc).__name__,
                )

    packet["diagnostic_summary"] = summary
    packet["diagnostic_model"] = source
    packet["claude_code_context"] = _claude_context(packet, summary, source)
    return packet
