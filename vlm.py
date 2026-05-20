"""Phase 3 dual-VLM verification of YOLO cooling-tower detections.

Public surface: a single function ``verify_detection`` that takes a satellite
tile path, a YOLO bounding box, and a building-context dict, and returns a
result dict describing whether the candidate is a real cooling tower on the
target rooftop. Every call runs Gemini 3.1 Pro and Grok 4.3 in parallel and
combines their verdicts via consensus: bucket-agreement on a confident answer
= final verdict; disagreement OR below-threshold confidence = ``needs_review``.
All recoverable failures map to ``needs_review``; configuration errors
(missing ``GEMINI_API_KEY`` or ``XAI_API_KEY``) propagate as ``KeyError``.
"""

from __future__ import annotations

import base64
import concurrent.futures
import functools
import io
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from openai import OpenAI
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError

_LOGGER = logging.getLogger(__name__)

_REFERENCE_DIR_POSITIVE = "reference_images/positive"
_REFERENCE_DIR_NEGATIVE = "reference_images/negative"
_REFERENCE_IMAGE_EXTS = (".jpg", ".jpeg", ".png")
_REFERENCE_IMAGE_CAP_PER_CATEGORY = 5
_CROP_PAD_PX = 50
_RETRY_BACKOFFS_S = (1, 2, 4)

_DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"
_DEFAULT_GROK_MODEL = "grok-4.3"
_GROK_BASE_URL = "https://api.x.ai/v1"
_DEFAULT_TIMEOUT_S = 120
_DEFAULT_CONSENSUS_THRESHOLD = 0.7

_POSITIVE_VERDICTS = frozenset({"confirmed", "likely"})
_NEGATIVE_VERDICTS = frozenset({"not_detected", "neighbor_only"})

_SYSTEM_PROMPT = """You are a senior rooftop HVAC equipment detection specialist.

Your task is to verify whether a candidate detection by a YOLO computer-vision model on satellite imagery is a real cooling tower on a specific target building. Your verdict drives a B2B sales pipeline; accuracy matters, and ambiguous cases should be flagged honestly rather than guessed.

A cooling tower in this domain is a rooftop heat-rejection unit, typically rectangular or cylindrical, with louvered air intakes on the sides, fan housings or fan stacks on top, and visible piping or condenser coils. Older or open-design cooling towers may instead appear as a square or rectangular enclosure with a visible centrifugal or radial fan blade pattern inside, viewed from directly above. They sit on the rooftops of commercial, multifamily, or institutional buildings.

They are NOT:
- Rooftop air handler units (AHUs) — flat boxes without prominent fan stacks
- Solar panels (rectangular, dark, flush with the roof)
- Skylights or roof hatches
- Rooftop water tanks (cylindrical wooden, or stainless-steel domed)
- Elevator penthouses or stairwell bulkheads (windowless rooms on the roof)
- Roof-mounted satellite dishes, antennas, or signage

You will receive a tight crop of the candidate plus a wider satellite tile that shows the entire target building and its neighbors. Use the tight crop for fine detail of the candidate object. Use the wider tile to confirm whether the candidate sits on the TARGET rooftop or on an adjacent building.

Return a structured JSON response with four fields: verdict, confidence, reasoning, construction. Be calibrated and honest about uncertainty."""

_USER_PROMPT_TEMPLATE = """=== BUILDING CONTEXT ===
Address: {address}
Geocoded coordinates: ({lat}, {lon})
OSM building id: {osm_id}
OSM tags: {osm_tags}
Geocoded centroid is inside building footprint: {contains_point}

=== DETECTION CONTEXT ===
The full satellite tile is 768x768 pixels at zoom 19 from Mapbox, centered on the OSM building centroid above. Pixel (0,0) is the top-left of the tile.
The TARGET building is outlined by a RED polygon drawn on Image B, centered around pixel (384, 384). Use the red outline as the authoritative boundary of the target building. Anything inside the red polygon is on the TARGET rooftop. Anything outside the red polygon is on a NEIGHBOR rooftop — cooling towers on neighbor rooftops should be classified as "neighbor_only", not "confirmed".
The YOLO model proposed a candidate at pixel bounding box: ({x1}, {y1}) to ({x2}, {y2}).

=== IMAGES YOU WILL RECEIVE ===
- Image A: a tight crop of the candidate detection with ~50px padding (clipped to tile edges). Use this for fine detail of the candidate object itself.
- Image B: the full 768x768 satellite tile showing the entire target building and its neighbors. Use this to confirm whether the candidate sits on the TARGET rooftop or a neighbor's.{reference_block}

=== YOUR TASK ===
Pick the single verdict that best describes the candidate:

- "confirmed"      — clearly a cooling tower AND clearly on the target rooftop. Use confidence > 0.8.
- "likely"         — probably a cooling tower with minor ambiguity (partial occlusion, marginal image quality, similar but not certain). Use confidence 0.5-0.8.
- "neighbor_only"  — appears to be a cooling tower but located on an adjacent building, not the target rooftop.
- "not_detected"   — the candidate is not a cooling tower at all (false positive: AHU, skylight, water tank, shadow artifact, generic mechanical box).
- "needs_review"   — you cannot decide with reasonable confidence. The reasoning field MUST explain what is preventing a decision.

Set "construction": true ONLY if you can see active construction — cranes, exposed rebar, partial framing, scaffolding, or an obvious construction zone on the roof or adjacent area. Do NOT set true just because the building looks modern, recently built, or well-maintained. Completed buildings = false.

Write 2-5 sentences in the "reasoning" field that a non-technical sales rep can read and understand. Reference what you actually see (e.g. "louvered intake panels visible on top of the unit", "candidate is on the southeast corner of the target rooftop, separated from the neighbor by a clear gap"). Avoid technical jargon they would not recognize. If your verdict is "neighbor_only", specify which direction the cooling tower actually is relative to the target building (e.g., "on the building immediately north of the target" or "on the adjacent building to the southwest")."""

_REFERENCE_BLOCK_POSITIVE = """

=== REFERENCE IMAGES ===
After Image A and Image B you will receive {n_pos} confirmed-positive reference image(s) from prior verified cases. These come from the same 768x768 zoom-19 Mapbox satellite imagery you are analyzing now.

Each positive has a yellow bounding box drawn around the cooling tower (the original training-data label from Roboflow). The yellow box marks the object — it is NOT a visual feature of cooling towers themselves. Use the equipment inside the yellow box as your visual anchor: fan pattern, enclosure shape, scale relative to the rooftop, and overhead appearance.

When evaluating the candidate in Image A, compare its features against the positives. A candidate that shares the fan pattern, scale, and enclosure characteristics of the positives should lean toward "confirmed" or "likely"."""

_REFERENCE_BLOCK_NEGATIVE_ADDITION = """

You will also receive {n_neg} confirmed-negative reference image(s) showing rooftop objects commonly mistaken for cooling towers but which are NOT cooling towers (for example: rooftop air handlers, exhaust fans, skylights, satellite dishes, water tanks). Treat these as exclusion anchors — if the candidate in Image A more closely resembles a negative reference than any positive reference, lean toward "not_detected"."""


class _VerificationResponse(BaseModel):
    verdict: Literal["confirmed", "likely", "neighbor_only", "needs_review", "not_detected"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    construction: bool


def _truncate(s, n: int = 120) -> str:
    if s is None:
        return ""
    return str(s).replace("\n", " ").replace("\r", " ")[:n]


def _needs_review(reasoning: str) -> dict:
    return {
        "verdict": "needs_review",
        "confidence": 0.0,
        "reasoning": reasoning,
        "construction": False,
    }


@functools.lru_cache(maxsize=1)
def _get_gemini_client(api_key: str):
    return genai.Client(api_key=api_key)


@functools.lru_cache(maxsize=1)
def _get_grok_client(api_key: str):
    return OpenAI(api_key=api_key, base_url=_GROK_BASE_URL)


def _strip_md_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    parts = s.split("\n", 1)
    s = parts[1] if len(parts) > 1 else s[3:]
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3]
    return s.strip()


def _to_image_url_part(jpeg_bytes: bytes) -> dict:
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
    }


def _load_reference_dir(dir_path: str) -> list[bytes]:
    p = Path(dir_path)
    if not p.is_dir():
        return []
    images: list[bytes] = []
    try:
        entries = sorted(p.iterdir())
    except OSError as e:
        _LOGGER.warning("Could not list reference dir %s: %s", dir_path, _truncate(e))
        return []
    for entry in entries:
        if len(images) >= _REFERENCE_IMAGE_CAP_PER_CATEGORY:
            break
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in _REFERENCE_IMAGE_EXTS:
            continue
        try:
            with open(entry, "rb") as f:
                images.append(f.read())
        except OSError as e:
            _LOGGER.warning("Skipping unreadable reference image %s: %s", entry, _truncate(e))
    return images


def _load_reference_images() -> tuple[list[bytes], list[bytes]]:
    return _load_reference_dir(_REFERENCE_DIR_POSITIVE), _load_reference_dir(_REFERENCE_DIR_NEGATIVE)


def _encode_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _make_crop_bytes(image_path: str, bbox: tuple[int, int, int, int]) -> bytes:
    x1, y1, x2, y2 = bbox
    with Image.open(image_path) as img:
        w, h = img.size
        pad_x1 = max(0, x1 - _CROP_PAD_PX)
        pad_y1 = max(0, y1 - _CROP_PAD_PX)
        pad_x2 = min(w, x2 + _CROP_PAD_PX)
        pad_y2 = min(h, y2 + _CROP_PAD_PX)
        crop = img.crop((pad_x1, pad_y1, pad_x2, pad_y2))
        return _encode_jpeg(crop)


def _read_full_tile_bytes(image_path: str) -> bytes:
    with Image.open(image_path) as img:
        return _encode_jpeg(img)


def _build_prompt(
    building_context: dict,
    detection_bbox: tuple[int, int, int, int],
    n_pos: int,
    n_neg: int,
) -> str:
    fm = building_context.get("footprint_metadata")
    if not isinstance(fm, dict):
        fm = {}

    address = building_context.get("address") or "(not provided)"
    lat = building_context.get("lat")
    lon = building_context.get("lon")
    lat_s = "?" if lat is None else lat
    lon_s = "?" if lon is None else lon

    osm_id = fm.get("osm_id")
    osm_id_s = "?" if osm_id is None else osm_id

    tags = fm.get("tags")
    tags_s = "(not provided)" if not tags else tags

    contains = fm.get("contains_point")
    contains_s = "unknown" if contains is None else str(bool(contains)).lower()

    x1, y1, x2, y2 = detection_bbox

    reference_block = ""
    if n_pos > 0:
        reference_block = _REFERENCE_BLOCK_POSITIVE.format(n_pos=n_pos)
        if n_neg > 0:
            reference_block += _REFERENCE_BLOCK_NEGATIVE_ADDITION.format(n_neg=n_neg)

    return _USER_PROMPT_TEMPLATE.format(
        address=address,
        lat=lat_s,
        lon=lon_s,
        osm_id=osm_id_s,
        osm_tags=tags_s,
        contains_point=contains_s,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        reference_block=reference_block,
    )


def _result_from_validated(parsed: _VerificationResponse) -> dict:
    return {
        "verdict": parsed.verdict,
        "confidence": parsed.confidence,
        "reasoning": parsed.reasoning,
        "construction": parsed.construction,
    }


def _parse_response(response) -> dict:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, _VerificationResponse):
        return _result_from_validated(parsed)

    raw_text = getattr(response, "text", None)
    if not raw_text:
        return _needs_review("VLM returned an empty response (no parsed object, no raw text).")

    try:
        data = json.loads(raw_text)
        validated = _VerificationResponse(**data)
    except (ValueError, ValidationError, TypeError):
        _LOGGER.warning("VLM schema mismatch. Full raw output: %s", raw_text)
        return _needs_review(
            f"VLM verification failed (schema mismatch). Operator: see raw output in logs. Truncated raw: {raw_text[:120]!r}"
        )
    return _result_from_validated(validated)


def _verify_gemini(
    image_path: str,
    detection_bbox: tuple[int, int, int, int],
    building_context: dict,
    timeout_s: int,
) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise KeyError("GEMINI_API_KEY")
    model_id = os.environ.get("GEMINI_MODEL") or _DEFAULT_GEMINI_MODEL

    try:
        crop_bytes = _make_crop_bytes(image_path, detection_bbox)
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_prompt(building_context, detection_bbox, len(pos_imgs), len(neg_imgs))

    contents: list = [prompt]
    for img_bytes in pos_imgs:
        contents.append("--- Reference: positive example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    for img_bytes in neg_imgs:
        contents.append("--- Reference: negative example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    contents.append("--- Image A (candidate crop) ---")
    contents.append(types.Part.from_bytes(data=crop_bytes, mime_type="image/jpeg"))
    contents.append("--- Image B (full satellite tile) ---")
    contents.append(types.Part.from_bytes(data=tile_bytes, mime_type="image/jpeg"))

    client = _get_gemini_client(api_key)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_VerificationResponse,
        http_options=types.HttpOptions(timeout=timeout_s * 1000),
    )

    last_transient_result: dict | None = None

    for attempt in range(4):
        if attempt > 0:
            time.sleep(_RETRY_BACKOFFS_S[attempt - 1])
        try:
            response = client.models.generate_content(
                model=model_id,
                contents=contents,
                config=config,
            )
        except httpx.TimeoutException as e:
            _LOGGER.debug("Attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review("Network timeout after 4 attempts.")
            continue
        except httpx.ConnectError as e:
            _LOGGER.debug("Attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except genai_errors.ServerError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None) or 500
            _LOGGER.debug("Attempt %d server error (HTTP %s): %s", attempt + 1, code, _truncate(e))
            last_transient_result = _needs_review(
                f"API server error (HTTP {code}) after 4 attempts: {_truncate(e)}"
            )
            continue
        except genai_errors.ClientError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code in (408, 429):
                name = "Request Timeout" if code == 408 else "Too Many Requests"
                _LOGGER.debug("Attempt %d throttled (HTTP %s): %s", attempt + 1, code, _truncate(e))
                last_transient_result = _needs_review(
                    f"API throttled (HTTP {code} {name}) after 4 attempts; retry later."
                )
                continue
            if code == 401:
                return _needs_review(
                    "API authentication error (HTTP 401): GEMINI_API_KEY may be invalid or revoked."
                )
            if code == 403:
                return _needs_review(f"API authorization error (HTTP 403): {_truncate(e)}")
            if code == 400:
                return _needs_review(
                    f"API rejected the request (HTTP 400): {_truncate(e)}. This usually indicates a malformed prompt or unsupported schema."
                )
            return _needs_review(f"API client error (HTTP {code}): {_truncate(e)}")
        except genai_errors.APIError as e:
            return _needs_review(f"VLM API error: {type(e).__name__}: {_truncate(e)}")

        return _parse_response(response)

    return last_transient_result or _needs_review("Network timeout after 4 attempts.")


def _verify_grok(
    image_path: str,
    detection_bbox: tuple[int, int, int, int],
    building_context: dict,
    timeout_s: int,
) -> dict:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise KeyError("XAI_API_KEY")
    model_id = os.environ.get("GROK_MODEL") or _DEFAULT_GROK_MODEL

    try:
        crop_bytes = _make_crop_bytes(image_path, detection_bbox)
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_prompt(building_context, detection_bbox, len(pos_imgs), len(neg_imgs))

    content_parts: list = [{"type": "text", "text": prompt}]
    for img_bytes in pos_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: positive example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    for img_bytes in neg_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: negative example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    content_parts.append({"type": "text", "text": "--- Image A (candidate crop) ---"})
    content_parts.append(_to_image_url_part(crop_bytes))
    content_parts.append({"type": "text", "text": "--- Image B (full satellite tile) ---"})
    content_parts.append(_to_image_url_part(tile_bytes))

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": content_parts},
    ]

    client = _get_grok_client(api_key)
    last_transient_result: dict | None = None

    for attempt in range(4):
        if attempt > 0:
            time.sleep(_RETRY_BACKOFFS_S[attempt - 1])
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=messages,
                response_format={"type": "json_object"},
                timeout=timeout_s,
            )
        except openai.APITimeoutError as e:
            _LOGGER.debug("Grok attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                "Grok network timeout after 4 attempts."
            )
            continue
        except openai.RateLimitError as e:
            _LOGGER.debug("Grok attempt %d rate-limited: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                "Grok API throttled (HTTP 429 Too Many Requests) after 4 attempts; retry later."
            )
            continue
        except openai.AuthenticationError:
            return _needs_review(
                "Grok API authentication error (HTTP 401): XAI_API_KEY may be invalid or revoked."
            )
        except openai.APIConnectionError as e:
            _LOGGER.debug("Grok attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Grok network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except openai.APIStatusError as e:
            code = getattr(e, "status_code", None) or 0
            if code in (408, 429) or 500 <= code < 600:
                _LOGGER.debug(
                    "Grok attempt %d transient (HTTP %s): %s",
                    attempt + 1, code, _truncate(e),
                )
                last_transient_result = _needs_review(
                    f"Grok API transient error (HTTP {code}) after 4 attempts: {_truncate(e)}"
                )
                continue
            if code == 403:
                return _needs_review(
                    f"Grok API authorization error (HTTP 403): {_truncate(e)}"
                )
            if code == 400:
                return _needs_review(
                    f"Grok API rejected the request (HTTP 400): {_truncate(e)}. "
                    f"This usually indicates a malformed prompt or unsupported format."
                )
            return _needs_review(
                f"Grok API client error (HTTP {code}): {_truncate(e)}"
            )
        except openai.APIError as e:
            return _needs_review(
                f"Grok VLM API error: {type(e).__name__}: {_truncate(e)}"
            )

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as e:
            return _needs_review(f"Grok response shape unexpected: {_truncate(e)}")

        if not content:
            return _needs_review("Grok returned an empty response (no content).")

        shim = SimpleNamespace(parsed=None, text=_strip_md_fences(content))
        return _parse_response(shim)

    return last_transient_result or _needs_review(
        "Grok network timeout after 4 attempts."
    )


def _combine_verdicts(
    gemini_result: dict, grok_result: dict, threshold: float
) -> dict:
    """Apply consensus rule. Returns the full 7-key dual-verify dict."""

    def bucket(verdict: str) -> str:
        if verdict in _POSITIVE_VERDICTS:
            return "positive"
        if verdict in _NEGATIVE_VERDICTS:
            return "negative"
        return "abstain"

    g_bucket = bucket(gemini_result["verdict"])
    k_bucket = bucket(grok_result["verdict"])
    g_conf = gemini_result["confidence"]
    k_conf = grok_result["confidence"]

    agree = (g_bucket == k_bucket) and (g_bucket != "abstain")
    confident = (g_conf >= threshold) and (k_conf >= threshold)

    if agree and confident:
        final_verdict = (
            gemini_result["verdict"] if g_conf >= k_conf else grok_result["verdict"]
        )
        final_confidence = min(g_conf, k_conf)
        final_reasoning = (
            f"Consensus ({g_bucket}). "
            f"Gemini: {gemini_result['reasoning']} "
            f"Grok: {grok_result['reasoning']}"
        )
        final_construction = bool(
            gemini_result["construction"] and grok_result["construction"]
        )
    else:
        final_verdict = "needs_review"
        final_confidence = 0.0
        if not agree:
            reason = (
                f"Models disagreed. "
                f"Gemini: {gemini_result['verdict']} "
                f"({g_bucket}, conf={g_conf:.2f}). "
                f"Grok: {grok_result['verdict']} "
                f"({k_bucket}, conf={k_conf:.2f}). "
                f"Manual review required."
            )
        else:
            reason = (
                f"Below confidence threshold ({threshold}). "
                f"Gemini conf={g_conf:.2f}, Grok conf={k_conf:.2f}. "
                f"Manual review required."
            )
        final_reasoning = (
            f"{reason} "
            f"Gemini detail: {gemini_result['reasoning']} "
            f"Grok detail: {grok_result['reasoning']}"
        )
        final_construction = False

    return {
        "verdict": final_verdict,
        "confidence": final_confidence,
        "reasoning": final_reasoning,
        "construction": final_construction,
        "gemini": gemini_result,
        "grok": grok_result,
        "agreement": agree and confident,
    }


def verify_detection(
    image_path: str,
    detection_bbox: tuple[int, int, int, int],
    building_context: dict,
) -> dict:
    """Verify a YOLO cooling-tower detection using parallel Gemini + Grok consensus.

    Args:
        image_path: Path to the full 768x768 satellite tile (JPEG/PNG).
        detection_bbox: ``(x1, y1, x2, y2)`` pixel bounding box from YOLO,
            in the coordinate frame of the tile at ``image_path``.
        building_context: Dict describing the target building. Must contain
            either ``address`` (truthy str) or both ``lat`` and ``lon``
            (numeric). May additionally contain a ``footprint_metadata``
            sub-dict with ``osm_id``, ``tags``, and ``contains_point``
            (a bool indicating whether the geocoded centroid lies inside
            the OSM footprint).

    Returns:
        A dict with exactly seven keys. The first four are the consensus
        result (backward-compatible with the prior single-model shape);
        the remaining three expose per-model detail.

        - ``verdict`` (str): consensus verdict, or ``"needs_review"`` when
          the two models disagree, either falls below the confidence
          threshold, or either failed via timeout/transport error.
        - ``confidence`` (float): ``min(gemini_conf, grok_conf)`` when
          consensus reached; ``0.0`` otherwise.
        - ``reasoning`` (str): synthesized explanation that embeds both
          models' raw reasoning. For ``needs_review`` it leads with the
          reason ("Models disagreed.", "Below confidence threshold.", or
          a per-model timeout/transport failure embedded in sub-detail).
        - ``construction`` (bool): ``True`` only when both models flagged
          active construction.
        - ``gemini`` (dict): Gemini's own 4-key result dict.
        - ``grok`` (dict): Grok's own 4-key result dict.
        - ``agreement`` (bool): ``True`` iff the two models confidently
          agreed on a bucket.

        All recoverable failures (bad inputs, network errors, schema
        mismatches, per-model timeouts, etc.) are mapped to ``needs_review``
        in the relevant sub-dict; that naturally routes the top-level result
        to ``needs_review`` via the disagreement rule. Pre-flight validation
        failures (bad inputs, unreadable image, degenerate bbox) short-circuit
        and return ``needs_review`` directly without calling either model
        (and without the per-model sub-dicts).

    Raises:
        KeyError: If either ``GEMINI_API_KEY`` or ``XAI_API_KEY`` is unset
            or empty. Both are required configuration; missing keys are
            surfaced loudly rather than routed to ``needs_review``.
    """
    if not isinstance(building_context, dict):
        return _needs_review(
            f"building_context must be a dict, got {type(building_context).__name__}."
        )

    has_address = bool(building_context.get("address"))
    lat = building_context.get("lat")
    lon = building_context.get("lon")
    has_valid_coords = isinstance(lat, (int, float)) and not isinstance(lat, bool) \
        and isinstance(lon, (int, float)) and not isinstance(lon, bool)
    if not has_address and not has_valid_coords:
        return _needs_review(
            "building_context provided no identifying information (no address, no coordinates)."
        )

    if not os.path.isfile(image_path):
        return _needs_review(f"Image file not found: {image_path}")

    try:
        x1, y1, x2, y2 = detection_bbox
    except (TypeError, ValueError):
        return _needs_review(
            f"Detection bbox is degenerate: {detection_bbox} (zero or negative area)."
        )
    if x2 <= x1 or y2 <= y1:
        return _needs_review(
            f"Detection bbox is degenerate: {detection_bbox} (zero or negative area)."
        )

    try:
        with Image.open(image_path) as img:
            w, h = img.size
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    if x2 <= 0 or y2 <= 0 or x1 >= w or y1 >= h:
        return _needs_review(
            f"Detection bbox {detection_bbox} is entirely outside image bounds (image is {w}x{h})."
        )

    if not os.environ.get("GEMINI_API_KEY"):
        raise KeyError("GEMINI_API_KEY")
    if not os.environ.get("XAI_API_KEY"):
        raise KeyError("XAI_API_KEY")

    timeout_s = int(os.environ.get("VLM_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_S)))
    threshold = float(
        os.environ.get("VLM_CONSENSUS_THRESHOLD", str(_DEFAULT_CONSENSUS_THRESHOLD))
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        gemini_future = ex.submit(
            _verify_gemini, image_path, detection_bbox, building_context, timeout_s
        )
        grok_future = ex.submit(
            _verify_grok, image_path, detection_bbox, building_context, timeout_s
        )

        try:
            gemini_result = gemini_future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            gemini_result = _needs_review(
                f"Gemini exceeded {timeout_s}s wall-clock timeout."
            )

        try:
            grok_result = grok_future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            grok_result = _needs_review(
                f"Grok exceeded {timeout_s}s wall-clock timeout."
            )

    return _combine_verdicts(gemini_result, grok_result, threshold)
