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

_DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
_DEFAULT_GROK_MODEL = "grok-4.3"
_GROK_BASE_URL = "https://api.x.ai/v1"
# Reasoning depth. Grok 4.3 defaults to "low" if unset; Gemini 3.x to "medium".
# Default both to "high" here — accuracy is prioritized over cost/latency on this tool.
_GROK_REASONING_EFFORT = os.environ.get("GROK_REASONING_EFFORT", "high")
_GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "high")
_DEFAULT_TIMEOUT_S = 120
_DEFAULT_CONSENSUS_THRESHOLD = 0.7

_POSITIVE_VERDICTS = frozenset({"confirmed", "likely", "cooling_tower_present", "cooling_tower_possible"})
_NEGATIVE_VERDICTS = frozenset({"not_detected", "neighbor_only", "no_cooling_tower"})

_SYSTEM_PROMPT = """You are a senior rooftop HVAC equipment detection specialist.

Your task is to verify whether a candidate detection by a YOLO computer-vision model on satellite imagery is a real cooling tower on a specific target building. Your verdict drives a B2B sales pipeline; accuracy matters, and ambiguous cases should be flagged honestly rather than guessed.

A cooling tower in this domain is a rooftop heat-rejection unit, typically rectangular or cylindrical, with louvered air intakes on the sides, fan housings or fan stacks on top, and visible piping or condenser coils. Older or open-design cooling towers may instead appear as a square or rectangular enclosure with a visible centrifugal or radial fan blade pattern inside, viewed from directly above. They sit on the rooftops of commercial, multifamily, or institutional buildings.

GROUND-MOUNTED COOLING EQUIPMENT — a first-line consideration, not an edge case: outside dense urban cores (most of the U.S. except cities like New York, Chicago, Boston, and San Francisco), cooling equipment serving a building is frequently ground-mounted rather than on the roof — on a concrete pad next to the building, inside a fenced enclosure, or in a mechanical yard adjacent to the structure. A ground-mounted cooling tower shows the SAME visual signatures (louvers, fan stacks, fan-blade pattern from above, coil banks) as a rooftop unit, just at ground level beside the building. The candidate you are verifying may be a ground-mounted unit, not only a rooftop one — treat ground-mounted cooling equipment serving the target building the same as a rooftop cooling tower for the verdict.

They are NOT:
- Rooftop air handler units (AHUs) — flat boxes without prominent fan stacks
- Solar panels (rectangular, dark, flush with the roof)
- Skylights or roof hatches
- Rooftop water tanks (cylindrical wooden, or stainless-steel domed)
- Elevator penthouses or stairwell bulkheads (windowless rooms on the roof)
- Roof-mounted satellite dishes, antennas, or signage

WATER TANK / WATER TOWER vs COOLING TOWER — the most common error in this domain:

A NYC-style rooftop wooden water tank, viewed from directly above, appears as a DARK CIRCLE inside a square wooden cradle. The dark circle is the open or covered top of the tank — it is NOT a fan blade pattern, NOT a cooling tower, and NOT mechanical equipment. Stainless-steel water tanks appear as a bright domed or conical shape, also NOT a cooling tower.

A cooling tower's circular top, when present, shows DISCRETE FAN BLADES that you can count (typically 4-8), a hub at the center, and protective metal grating. A water tank top shows none of these — just a uniform dark or reflective surface.

If you cannot count individual fan blades and identify a central hub, it is not a cooling tower fan. Default to "not_detected" / "no_cooling_tower" rather than guessing on a borderline circular feature.

You will receive a tight crop of the candidate plus a wider satellite tile that shows the entire target building and its neighbors, and you may also receive a second tile of the same target at a different zoom level for added context. Use the tight crop for fine detail of the candidate object. Use the wider/second tiles to confirm whether the candidate sits on (or, if ground-mounted, immediately beside and serving) the TARGET building rather than an adjacent one.

Return a structured JSON response with six fields: verdict, confidence, reasoning, construction, is_house, image_unusable. Be calibrated and honest about uncertainty."""

_USER_PROMPT_TEMPLATE = """=== BUILDING CONTEXT ===
Address: {address}
Geocoded coordinates: ({lat}, {lon})
OSM building id: {osm_id}
OSM tags: {osm_tags}
Geocoded centroid is inside building footprint: {contains_point}

=== DETECTION CONTEXT ===
The full satellite tile is 768x768 pixels at zoom {tile_zoom} from Mapbox, centered on the OSM building centroid above. Pixel (0,0) is the top-left of the tile.

The red polygon on Image B is the OSM building footprint at the geocoded address. Geocoding is not always perfect — sometimes the polygon outlines a neighboring building rather than the actual target. The YOLO model proposes candidates ANYWHERE in the tile, not only inside the red polygon. The current candidate at pixel bbox ({x1}, {y1}) to ({x2}, {y2}) is classified geometrically as: {detection_location} (inside / boundary / outside relative to the red polygon).

If the candidate is OUTSIDE the red polygon, do NOT auto-reject. Use the satellite view to judge: does the building under the candidate plausibly match the address ({address})? If yes, treat it as on-target and proceed to verdict. If clearly a different building (a school across the street, a different apartment block, a building with a noticeably different footprint shape than the addressed one), classify as "neighbor_only".

=== IMAGES YOU WILL RECEIVE ===
- Image A: a tight crop of the candidate detection with ~50px padding (clipped to tile edges). Use this for fine detail of the candidate object itself.
- Image B: the full 768x768 satellite tile (zoom {tile_zoom}) showing the entire target building and its neighbors. Use this to confirm whether the candidate sits on the TARGET rooftop or a neighbor's.{context_block}{reference_block}

=== YOUR TASK ===
Pick the single verdict that best describes the candidate:

- "confirmed"      — clearly a cooling tower AND clearly serving the target building (on the target rooftop, OR ground-mounted on a pad / in an enclosure / in a mechanical yard immediately beside the target). Use confidence > 0.8.
- "likely"         — probably a cooling tower (rooftop or ground-mounted) serving the target with minor ambiguity (partial occlusion, marginal image quality, similar but not certain). Use confidence 0.5-0.8.
- "neighbor_only"  — appears to be a cooling tower but located on or beside an adjacent building, not serving the target.
- "not_detected"   — the candidate is not a cooling tower at all (false positive: AHU, skylight, water tank, shadow artifact, generic mechanical box).
- "needs_review"   — you cannot decide with reasonable confidence. The reasoning field MUST explain what is preventing a decision.

Set "construction": true ONLY if you can see active construction — cranes, exposed rebar, partial framing, scaffolding, or an obvious construction zone on the roof or adjacent area. Do NOT set true just because the building looks modern, recently built, or well-maintained. Completed buildings = false.

Set "is_house": true ONLY if the TARGET building is clearly a single-family house or small residential dwelling — a small footprint with a pitched/gabled roof, a driveway or yard, the look of a detached or attached row home — i.e. a building that would not carry commercial cooling-tower equipment. Set false for apartment blocks, commercial, institutional, mixed-use, or any building large or ambiguous enough to plausibly have a cooling tower. This is a separate signal from the cooling-tower verdict.

Write 2-5 sentences in the "reasoning" field that a non-technical sales rep can read and understand. Reference what you actually see (e.g. "louvered intake panels visible on top of the unit", "candidate is on the southeast corner of the target rooftop, separated from the neighbor by a clear gap"). Avoid technical jargon they would not recognize. If your verdict is "neighbor_only", specify which direction the cooling tower actually is relative to the target building (e.g., "on the building immediately north of the target" or "on the adjacent building to the southwest")."""

_REFERENCE_BLOCK_POSITIVE = """

=== REFERENCE IMAGES ===
After Image A and Image B you will receive {n_pos} confirmed-positive reference image(s) from prior verified cases. These come from 768x768 zoom-19 Mapbox satellite imagery (the same source you are analyzing); the candidate tile may be at a different zoom, so match on equipment features (fan pattern, louvers, enclosure) rather than absolute scale.

Each positive has a yellow bounding box drawn around the cooling tower (the original training-data label from Roboflow). The yellow box marks the object — it is NOT a visual feature of cooling towers themselves. Use the equipment inside the yellow box as your visual anchor: fan pattern, enclosure shape, scale relative to the rooftop, and overhead appearance.

When evaluating the candidate in Image A, compare its features against the positives. A candidate that shares the fan pattern, scale, and enclosure characteristics of the positives should lean toward "confirmed" or "likely"."""

_REFERENCE_BLOCK_NEGATIVE_ADDITION = """

You will also receive {n_neg} confirmed-negative reference image(s) showing rooftop objects commonly mistaken for cooling towers but which are NOT cooling towers (for example: rooftop air handlers, exhaust fans, skylights, satellite dishes, water tanks). Treat these as exclusion anchors — if the candidate in Image A more closely resembles a negative reference than any positive reference, lean toward "not_detected"."""


_ROOFTOP_SYSTEM_PROMPT = """You are a senior rooftop HVAC equipment detection specialist.

Your task is to scan a target building's rooftop AND its immediate surroundings in a satellite image, and report whether a cooling tower (rooftop or ground-mounted, per the definition below) is present serving the target building. There is no candidate detection from a prior model — an automated YOLO pass already ran on this image and found no cooling towers on the target rooftop, so you are the last line of defense. Your verdict drives a B2B sales pipeline; accuracy matters, and ambiguous cases should be flagged honestly rather than guessed.

A cooling tower in this domain is a rooftop heat-rejection unit, typically rectangular or cylindrical, with louvered air intakes on the sides, fan housings or fan stacks on top, and visible piping or condenser coils. Older or open-design cooling towers may instead appear as a square or rectangular enclosure with a visible centrifugal or radial fan blade pattern inside, viewed from directly above. They sit on the rooftops of commercial, multifamily, or institutional buildings. Outside dense urban areas (most of the U.S. except cities like New York, Chicago, Boston, and San Francisco), cooling equipment serving a building is often ground-mounted instead — on a concrete pad next to the building, in a fenced enclosure, or in a mechanical yard. Treat ground-mounted cooling equipment serving the target building the same way you treat rooftop cooling towers for purposes of this verdict.

They are NOT:
- Rooftop air handler units (AHUs) — flat boxes without prominent fan stacks
- Solar panels (rectangular, dark, flush with the roof)
- Skylights or roof hatches
- Rooftop water tanks (cylindrical wooden, or stainless-steel domed)
- Elevator penthouses or stairwell bulkheads (windowless rooms on the roof)
- Roof-mounted satellite dishes, antennas, or signage

WATER TANK / WATER TOWER vs COOLING TOWER — the most common error in this domain:

A NYC-style rooftop wooden water tank, viewed from directly above, appears as a DARK CIRCLE inside a square wooden cradle. The dark circle is the open or covered top of the tank — it is NOT a fan blade pattern, NOT a cooling tower, and NOT mechanical equipment. Stainless-steel water tanks appear as a bright domed or conical shape, also NOT a cooling tower.

A cooling tower's circular top, when present, shows DISCRETE FAN BLADES that you can count (typically 4-8), a hub at the center, and protective metal grating. A water tank top shows none of these — just a uniform dark or reflective surface.

If you cannot count individual fan blades and identify a central hub, it is not a cooling tower fan. Default to "not_detected" / "no_cooling_tower" rather than guessing on a borderline circular feature.

You will receive ONE satellite tile that shows the target building and its neighbors. The target building is the structure centered in the tile — its OSM footprint is described in the user prompt. Only equipment serving the target building should influence your cooling-tower verdict — this means equipment on the target building's rooftop, OR ground-mounted cooling equipment immediately adjacent to the target building per the user prompt. Anything on a neighboring building's rooftop should be ignored for the cooling-tower verdict.

You also need to flag visible active construction (cranes, exposed rebar, partial framing, scaffolding, or an obvious construction zone) on the target rooftop or the surrounding area — this is a separate signal the sales team uses to prioritize follow-up, distinct from whether a cooling tower is present.

Return a structured JSON response with six fields: verdict, confidence, reasoning, construction, is_house, image_unusable. Be calibrated and honest about uncertainty."""

_ROOFTOP_USER_PROMPT_TEMPLATE = """=== BUILDING CONTEXT ===
Address: {address}
Geocoded coordinates: ({lat}, {lon})
OSM building id: {osm_id}
OSM tags: {osm_tags}
Geocoded centroid is inside building footprint: {contains_point}

=== DETECTION CONTEXT ===
The satellite tile is 768x768 pixels at zoom {tile_zoom} from Mapbox, centered on the OSM building centroid above. Pixel (0,0) is the top-left of the tile. The TARGET building is the structure centered around pixel (384, 384) — its footprint corresponds to the OSM building described above. Equipment on a neighboring building's rooftop should be ignored. However, ground-mounted cooling equipment immediately adjacent to the target building — on a concrete pad, in a fenced enclosure, or in a mechanical yard within roughly 30 feet of the target building — counts as serving the target and should be reported.

An automated YOLO computer-vision pass already ran on this tile. No cooling-tower detections were found. You are the last line of defense — scan the target building's rooftop and its immediate surroundings, and decide whether a cooling tower (rooftop or ground-mounted) is actually present.

=== IMAGES YOU WILL RECEIVE ===
- The full 768x768 satellite tile (zoom {tile_zoom}) showing the target building (centered) and its neighbors.{context_block}{reference_block}

=== YOUR TASK ===
Pick the single verdict that best describes whether a cooling tower is on the target rooftop:

- "cooling_tower_present"  — a cooling tower is clearly visible on the target building (rooftop or ground-mounted). Use confidence > 0.7.
- "cooling_tower_possible" — something that might be a cooling tower is visible on the target building (rooftop or ground-mounted), with ambiguity (partial occlusion, marginal image quality, similar but not certain). Use confidence 0.4-0.7.
- "no_cooling_tower"       — confident no cooling tower is on the target building (rooftop or ground-mounted).
- "needs_review"           — you cannot decide with reasonable confidence. The reasoning field MUST explain what is preventing a decision.

If your verdict is "cooling_tower_present" or "cooling_tower_possible", your reasoning MUST cite specific visible features from the cooling tower definition in the system prompt. Acceptable feature citations include: louvers, fan stacks, condenser coils, a visible centrifugal or axial fan blade pattern from above, finned heat-exchanger coil banks, or visible piping consistent with chilled-water or refrigerant lines. Vague descriptions like "mechanical equipment on the roof", "rooftop structure", or "equipment on the pad" are not sufficient justification. If you cannot cite specific features, the correct verdict is "no_cooling_tower" (if you are confident in the absence) or "needs_review" (if you are uncertain).

Set "construction": true ONLY if you can see active construction — cranes, exposed rebar, partial framing, scaffolding, or an obvious construction zone on the target rooftop or adjacent area. Do NOT set true just because the building looks modern, recently built, or well-maintained. Completed buildings = false.

Set "is_house": true ONLY if the TARGET building is clearly a single-family house or small residential dwelling — a small footprint with a pitched/gabled roof, a driveway or yard, the look of a detached or attached row home — i.e. a building that would not carry commercial cooling-tower equipment. Set false for apartment blocks, commercial, institutional, mixed-use, or any building large or ambiguous enough to plausibly have a cooling tower. This is a separate signal from the cooling-tower verdict.

Write 2-5 sentences in the "reasoning" field that a non-technical sales rep can read and understand. Reference what you actually see on the target rooftop. Avoid technical jargon. IMPORTANT: if your verdict is "no_cooling_tower" AND construction is true, the reasoning MUST describe the construction activity in concrete terms (where on the building, what you see) — this is the lead signal the sales team uses for follow-up."""

_ROOFTOP_REFERENCE_BLOCK_POSITIVE = """

=== REFERENCE IMAGES ===
After the satellite tile you will receive {n_pos} confirmed-positive reference image(s) from prior verified cases. These come from 768x768 zoom-19 Mapbox satellite imagery (the same source you are analyzing); the tile you are scanning may be at a different zoom, so match on equipment features (fan pattern, louvers, enclosure) rather than absolute scale.

Each positive has a yellow bounding box drawn around the cooling tower (the original training-data label from Roboflow). The yellow box marks the object — it is NOT a visual feature of cooling towers themselves. Use the equipment inside the yellow box as your visual anchor: fan pattern, enclosure shape, scale relative to the rooftop, and overhead appearance.

When scanning the target rooftop in the satellite tile, compare what you see against the positives. Equipment that shares the fan pattern, scale, and enclosure characteristics of the positives should lean toward "cooling_tower_present" or "cooling_tower_possible"."""

_ROOFTOP_REFERENCE_BLOCK_NEGATIVE_ADDITION = """

You will also receive {n_neg} confirmed-negative reference image(s) showing rooftop objects commonly mistaken for cooling towers but which are NOT cooling towers (for example: rooftop air handlers, exhaust fans, skylights, satellite dishes, water tanks). Treat these as exclusion anchors — if equipment on the target rooftop more closely resembles a negative reference than any positive reference, lean toward "no_cooling_tower"."""


class _VerificationResponse(BaseModel):
    verdict: Literal["confirmed", "likely", "neighbor_only", "needs_review", "not_detected"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    construction: bool
    is_house: bool
    image_unusable: bool = False
    frame_inadequate: bool = False


class _RooftopResponse(BaseModel):
    verdict: Literal["cooling_tower_present", "cooling_tower_possible", "no_cooling_tower", "needs_review"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)
    construction: bool
    is_house: bool


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
        "is_house": False,
        "image_unusable": False,
        "frame_inadequate": False,
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


def _maybe_read_context_tile(context_image_path):
    """Read an optional cross-zoom context tile. Returns JPEG bytes or None.

    The context image is supplementary; if it's missing or unreadable we skip
    it silently rather than failing the whole verification.
    """
    if not context_image_path:
        return None
    try:
        return _read_full_tile_bytes(context_image_path)
    except (UnidentifiedImageError, OSError):
        return None


def _context_block(tile_zoom: int, context_zoom) -> str:
    """Build the 'Image C' context-image clause for the user prompt.

    Returns '' when no context image is present. Phrasing depends on whether
    the context tile is wider (good for ground-mounted equipment) or closer
    (good for fine rooftop detail) than the primary tile.
    """
    if context_zoom is None:
        return ""
    if context_zoom < tile_zoom:
        desc = (
            "a WIDER view — use it to spot ground-mounted cooling equipment "
            "(on a concrete pad, in a fenced enclosure, or a mechanical yard) "
            "immediately beside the target building, not only rooftop units"
        )
    else:
        desc = "a CLOSER view — use it for finer detail of the equipment and the target rooftop"
    return f"\n- Image C: the same target building at zoom {context_zoom} ({desc})."


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

    detection_location = building_context.get("detection_location") or "unknown"

    tile_zoom = building_context.get("tile_zoom", 19)
    context_zoom = building_context.get("context_zoom")

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
        detection_location=detection_location,
        tile_zoom=tile_zoom,
        context_block=_context_block(tile_zoom, context_zoom),
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        reference_block=reference_block,
    )


def _build_rooftop_prompt(
    building_context: dict,
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

    tile_zoom = building_context.get("tile_zoom", 19)
    context_zoom = building_context.get("context_zoom")

    reference_block = ""
    if n_pos > 0:
        reference_block = _ROOFTOP_REFERENCE_BLOCK_POSITIVE.format(n_pos=n_pos)
        if n_neg > 0:
            reference_block += _ROOFTOP_REFERENCE_BLOCK_NEGATIVE_ADDITION.format(n_neg=n_neg)

    return _ROOFTOP_USER_PROMPT_TEMPLATE.format(
        address=address,
        lat=lat_s,
        lon=lon_s,
        osm_id=osm_id_s,
        osm_tags=tags_s,
        contains_point=contains_s,
        tile_zoom=tile_zoom,
        context_block=_context_block(tile_zoom, context_zoom),
        reference_block=reference_block,
    )


_ADDRESS_SYSTEM_PROMPT = """You are a senior rooftop HVAC equipment detection specialist.

Your task is to determine whether a real cooling tower is present on — or, if ground-mounted, immediately beside and serving — a specific TARGET building in a satellite image. An automated YOLO computer-vision model has already run and drawn numbered candidate boxes on the image; you are the expert reviewer who decides the truth. Your verdict drives a B2B sales pipeline; accuracy matters, and ambiguous cases should be flagged honestly rather than guessed.

A cooling tower in this domain is a rooftop heat-rejection unit, typically rectangular or cylindrical, with louvered air intakes on the sides, fan housings or fan stacks on top, and visible piping or condenser coils. Older or open-design cooling towers may instead appear as a square or rectangular enclosure with a visible centrifugal or radial fan blade pattern inside, viewed from directly above. They sit on the rooftops of commercial, multifamily, or institutional buildings.

GROUND-MOUNTED COOLING EQUIPMENT — a first-line consideration, not an edge case: outside dense urban cores (most of the U.S. except cities like New York, Chicago, Boston, and San Francisco), cooling equipment serving a building is frequently ground-mounted rather than on the roof — on a concrete pad next to the building, inside a fenced enclosure, or in a mechanical yard adjacent to the structure. A ground-mounted cooling tower shows the SAME visual signatures (louvers, fan stacks, fan-blade pattern from above, coil banks) as a rooftop unit, just at ground level beside the building. Treat ground-mounted cooling equipment serving the target building the same as a rooftop cooling tower for the verdict.

They are NOT:
- Rooftop air handler units (AHUs) — flat boxes without prominent fan stacks
- Solar panels (rectangular, dark, flush with the roof)
- Skylights or roof hatches
- Rooftop water tanks (cylindrical wooden, or stainless-steel domed)
- Elevator penthouses or stairwell bulkheads (windowless rooms on the roof)
- Roof-mounted satellite dishes, antennas, or signage

WATER TANK / WATER TOWER vs COOLING TOWER — the most common error in this domain:

A NYC-style rooftop wooden water tank, viewed from directly above, appears as a DARK CIRCLE inside a square wooden cradle. The dark circle is the open or covered top of the tank — it is NOT a fan blade pattern, NOT a cooling tower, and NOT mechanical equipment. Stainless-steel water tanks appear as a bright domed or conical shape, also NOT a cooling tower.

A cooling tower's circular top, when present, shows DISCRETE FAN BLADES that you can count (typically 4-8), a hub at the center, and protective metal grating. A water tank top shows none of these — just a uniform dark or reflective surface.

If you cannot count individual fan blades and identify a central hub, it is not a cooling tower fan. Default to "not_detected" rather than guessing on a borderline circular feature.

=== HOW TO READ THIS IMAGE ===
The TARGET building's footprint is outlined in RED. The red outline is the building at the address. A cooling tower counts for the target ONLY if it sits on the red building's roof, or is ground-mounted immediately beside the red building. Equipment on a neighboring building — anything outside the red outline, on a different roof — does NOT count for the target.

The numbered boxes can land on DIFFERENT buildings — some on the target (red), some on neighbors. Judge each box on its own building. A real cooling tower on a neighbor does NOT cancel one on the target: if even a single box (or anything you spot yourself) is a real cooling tower on the target, the verdict is "confirmed", no matter how many other towers sit on neighboring roofs. Treat it as a neighbor case ONLY when EVERY real cooling tower in view is on a neighbor and the target itself has none.

YOLO has drawn one or more NUMBERED boxes around things it guessed might be cooling towers. Treat each numbered box as nothing more than a suggestion from an automated model that is frequently wrong. A box may contain an air handler, a skylight, a water tank, a shadow, or nothing at all. Do NOT assume a numbered box contains a cooling tower — check each one against the definition above and reject the ones that fail it.

Two jobs, equally important:
1. VERIFY the numbered boxes — decide which, if any, contain a real cooling tower serving the target building.
2. FIND what YOLO MISSED — scan the rest of the target's roof and its immediate surroundings for any cooling tower with NO box around it. A real cooling tower that YOLO failed to box still counts — report it.

Return a structured JSON response with seven fields: verdict, confidence, reasoning, construction, is_house, image_unusable, frame_inadequate. Be calibrated and honest about uncertainty."""

_ADDRESS_USER_PROMPT_TEMPLATE = """=== BUILDING CONTEXT ===
Address: {address}
Geocoded coordinates: ({lat}, {lon})
OSM building id: {osm_id}
OSM tags: {osm_tags}
Geocoded centroid is inside building footprint: {contains_point}

=== WHAT IS IN THE IMAGE ===
The satellite tile is 768x768 pixels at zoom {tile_zoom}, centered on the target building. Pixel (0,0) is the top-left. The TARGET building's footprint is outlined in RED. {boxes_clause}

Use judgment on the red outline — minor misalignment is expected, not a problem. The red footprint comes from map data and is frequently imperfect: it may sit a few metres off, only partially overlap the real structure, or trace the building's shape loosely. If the red outline is roughly on the building and at least approximates the shape of what you are looking at — even with a weird or partial overlap — treat that building as the target and proceed with your verdict normally. Only when the red outline clearly traces a COMPLETELY DIFFERENT building — a distinctly different footprint shape, a structure across the street, an obviously unrelated building — treat it as a wrong-building case: if a cooling tower sits on that different building, call it "neighbor_only" (and name the direction); if you genuinely cannot tell which building the address refers to, use "needs_review".{context_block}{closeup_block}{reference_block}

=== YOUR TASK ===
Considering BOTH the numbered boxes AND your own scan of the target roof and its immediate surroundings, pick the single verdict that best describes the TARGET building:

- "confirmed"      — at least one real cooling tower is clearly present and clearly serving the target building (on the red building's rooftop, OR ground-mounted on a pad / in an enclosure / in a mechanical yard immediately beside it). It does not matter whether YOLO boxed it or you found it yourself, and it does not matter if OTHER cooling towers also sit on neighboring buildings — one real tower on the target is enough. Use confidence > 0.8.
- "likely"         — a cooling tower probably serves the target with minor ambiguity (partial occlusion, marginal image quality, similar but not certain). Use confidence 0.5-0.8.
- "neighbor_only"  — you can see real cooling tower(s), but EVERY one of them is on or beside an ADJACENT building and the target itself has none. If even one real tower is on the target, use "confirmed" instead, not "neighbor_only". Specify the direction of the neighbor tower(s) relative to the target.
- "not_detected"   — no cooling tower serves the target building. Every numbered box, if any, is a false positive (AHU, skylight, water tank, shadow artifact, generic mechanical box), and your own scan of the target roof and surroundings finds none.
- "needs_review"   — you cannot decide with reasonable confidence. The reasoning field MUST explain what is preventing a decision.

Set "construction": true ONLY if you can see active construction — cranes, exposed rebar, partial framing, scaffolding, or an obvious construction zone on the roof or adjacent area. Do NOT set true just because the building looks modern, recently built, or well-maintained. Completed buildings = false.

Set "is_house": true ONLY if the TARGET building is clearly a single-family house or small residential dwelling — a small footprint with a pitched/gabled roof, a driveway or yard, the look of a detached or attached row home — i.e. a building that would not carry commercial cooling-tower equipment. Set false for apartment blocks, commercial, institutional, mixed-use, or any building large or ambiguous enough to plausibly have a cooling tower. This is a separate signal from the cooling-tower verdict.

Set "image_unusable": true ONLY if you cannot properly judge the target building because its roof is not clearly visible from directly above in THIS image — for example a tall tower shown leaning at a steep oblique angle so you see its glass facade instead of its roof, or the target's roof is cut off at the edge of the frame. This tells the system to retry with a different satellite source. If you can see the target's roof clearly (even if it simply has no cooling tower on it), set it false.

Set "frame_inadequate": true ONLY when you are about to call "not_detected" or "neighbor_only" AND the image is zoomed in tightly enough that a GROUND-MOUNTED cooling tower serving the target could be sitting just outside the frame — i.e. the target building fills most of the view and you cannot see the immediately-adjacent ground, pads, yards, alleys, or mechanical enclosures where such a unit would sit. This tells the system to re-pull a WIDER view and look again. Think this through deliberately before setting it: if the surroundings you can ALREADY see are enough to rule out a ground-mounted unit, set false. And if you have ALREADY found a real cooling tower serving the target (a "confirmed" or "likely" verdict), set it false — you already have the information you need, so there is no reason to look elsewhere. This is separate from "image_unusable" (which is about the target's roof not being visible at all).

Write 2-5 sentences in the "reasoning" field that a non-technical sales rep can read and understand. Reference what you actually see, and when you rely on a box, name it (e.g. "box 2 is a real cooling tower on the target's southeast corner; boxes 1 and 3 are rooftop air handlers"). If your verdict is "neighbor_only", specify which direction the cooling tower actually is relative to the target building."""


_ADDRESS_CLOSEUP_BLOCK = """

You are also given a separate HIGH-ZOOM CLOSE-UP of the main candidate equipment. Use it for the fine IDENTITY call: count discrete fan blades and look for a central hub and louvered enclosure (cooling tower) versus a uniform dark or domed circular top with no countable blades (water tank). When the close-up and the wide tile seem to disagree, trust the CLOSE-UP for what the equipment IS, and the wide tile for which building it sits ON. (The close-up is zoomed in on one spot, so it does not show neighbors — do not use it to decide target-vs-neighbor.)"""


def _build_address_prompt(
    building_context: dict,
    n_boxes: int,
    n_pos: int,
    n_neg: int,
    has_closeup: bool = False,
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

    tile_zoom = building_context.get("tile_zoom", 19)
    context_zoom = building_context.get("context_zoom")

    if n_boxes > 0:
        boxes_clause = (
            f"YOLO has drawn {n_boxes} numbered candidate box(es) on the tile — "
            "each marks something it guessed might be a cooling tower."
        )
    else:
        boxes_clause = (
            "YOLO drew no candidate boxes on this tile — rely entirely on your own "
            "scan of the target roof and its immediate surroundings."
        )

    reference_block = ""
    if n_pos > 0:
        reference_block = _ROOFTOP_REFERENCE_BLOCK_POSITIVE.format(n_pos=n_pos)
        if n_neg > 0:
            reference_block += _ROOFTOP_REFERENCE_BLOCK_NEGATIVE_ADDITION.format(n_neg=n_neg)

    return _ADDRESS_USER_PROMPT_TEMPLATE.format(
        address=address,
        lat=lat_s,
        lon=lon_s,
        osm_id=osm_id_s,
        osm_tags=tags_s,
        contains_point=contains_s,
        tile_zoom=tile_zoom,
        boxes_clause=boxes_clause,
        context_block=_context_block(tile_zoom, context_zoom),
        closeup_block=(_ADDRESS_CLOSEUP_BLOCK if has_closeup else ""),
        reference_block=reference_block,
    )


def _result_from_validated(parsed: _VerificationResponse) -> dict:
    return {
        "verdict": parsed.verdict,
        "confidence": parsed.confidence,
        "reasoning": parsed.reasoning,
        "construction": parsed.construction,
        "is_house": parsed.is_house,
        # Only the address schema carries image_unusable; rooftop schema lacks it.
        "image_unusable": bool(getattr(parsed, "image_unusable", False)),
        # Gemini-only ground-CT "look wider" signal; rooftop schema lacks it.
        "frame_inadequate": bool(getattr(parsed, "frame_inadequate", False)),
    }


def _parse_response(response, model_cls) -> dict:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, model_cls):
        return _result_from_validated(parsed)

    raw_text = getattr(response, "text", None)
    if not raw_text:
        return _needs_review("VLM returned an empty response (no parsed object, no raw text).")

    try:
        data = json.loads(raw_text)
        validated = model_cls(**data)
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
    context_image_path: str = None,
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

    context_bytes = _maybe_read_context_tile(context_image_path)

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
    if context_bytes is not None:
        contents.append(f"--- Image C (same target, zoom {building_context.get('context_zoom')}) ---")
        contents.append(types.Part.from_bytes(data=context_bytes, mime_type="image/jpeg"))

    client = _get_gemini_client(api_key)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_VerificationResponse,
        thinking_config=types.ThinkingConfig(thinking_level=_GEMINI_THINKING_LEVEL),
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

        return _parse_response(response, _VerificationResponse)

    return last_transient_result or _needs_review("Network timeout after 4 attempts.")


def _verify_gemini_rooftop(
    image_path: str,
    building_context: dict,
    timeout_s: int,
    context_image_path: str = None,
) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise KeyError("GEMINI_API_KEY")
    model_id = os.environ.get("GEMINI_MODEL") or _DEFAULT_GEMINI_MODEL

    try:
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    context_bytes = _maybe_read_context_tile(context_image_path)

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_rooftop_prompt(building_context, len(pos_imgs), len(neg_imgs))

    contents: list = [prompt]
    for img_bytes in pos_imgs:
        contents.append("--- Reference: positive example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    for img_bytes in neg_imgs:
        contents.append("--- Reference: negative example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    contents.append("--- Satellite tile (target building centered) ---")
    contents.append(types.Part.from_bytes(data=tile_bytes, mime_type="image/jpeg"))
    if context_bytes is not None:
        contents.append(f"--- Context tile (same target, zoom {building_context.get('context_zoom')}) ---")
        contents.append(types.Part.from_bytes(data=context_bytes, mime_type="image/jpeg"))

    client = _get_gemini_client(api_key)
    config = types.GenerateContentConfig(
        system_instruction=_ROOFTOP_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_RooftopResponse,
        thinking_config=types.ThinkingConfig(thinking_level=_GEMINI_THINKING_LEVEL),
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
            _LOGGER.debug("Rooftop attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review("Network timeout after 4 attempts.")
            continue
        except httpx.ConnectError as e:
            _LOGGER.debug("Rooftop attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except genai_errors.ServerError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None) or 500
            _LOGGER.debug("Rooftop attempt %d server error (HTTP %s): %s", attempt + 1, code, _truncate(e))
            last_transient_result = _needs_review(
                f"API server error (HTTP {code}) after 4 attempts: {_truncate(e)}"
            )
            continue
        except genai_errors.ClientError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code in (408, 429):
                name = "Request Timeout" if code == 408 else "Too Many Requests"
                _LOGGER.debug("Rooftop attempt %d throttled (HTTP %s): %s", attempt + 1, code, _truncate(e))
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

        return _parse_response(response, _RooftopResponse)

    return last_transient_result or _needs_review("Network timeout after 4 attempts.")


def _verify_grok(
    image_path: str,
    detection_bbox: tuple[int, int, int, int],
    building_context: dict,
    timeout_s: int,
    context_image_path: str = None,
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

    context_bytes = _maybe_read_context_tile(context_image_path)

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
    if context_bytes is not None:
        content_parts.append({"type": "text", "text": f"--- Image C (same target, zoom {building_context.get('context_zoom')}) ---"})
        content_parts.append(_to_image_url_part(context_bytes))

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
                reasoning_effort=_GROK_REASONING_EFFORT,
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
        return _parse_response(shim, _VerificationResponse)

    return last_transient_result or _needs_review(
        "Grok network timeout after 4 attempts."
    )


def _verify_grok_rooftop(
    image_path: str,
    building_context: dict,
    timeout_s: int,
    context_image_path: str = None,
) -> dict:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise KeyError("XAI_API_KEY")
    model_id = os.environ.get("GROK_MODEL") or _DEFAULT_GROK_MODEL

    try:
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    context_bytes = _maybe_read_context_tile(context_image_path)

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_rooftop_prompt(building_context, len(pos_imgs), len(neg_imgs))

    content_parts: list = [{"type": "text", "text": prompt}]
    for img_bytes in pos_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: positive example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    for img_bytes in neg_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: negative example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    content_parts.append({"type": "text", "text": "--- Satellite tile (target building centered) ---"})
    content_parts.append(_to_image_url_part(tile_bytes))
    if context_bytes is not None:
        content_parts.append({"type": "text", "text": f"--- Context tile (same target, zoom {building_context.get('context_zoom')}) ---"})
        content_parts.append(_to_image_url_part(context_bytes))

    messages = [
        {"role": "system", "content": _ROOFTOP_SYSTEM_PROMPT},
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
                reasoning_effort=_GROK_REASONING_EFFORT,
                timeout=timeout_s,
            )
        except openai.APITimeoutError as e:
            _LOGGER.debug("Grok rooftop attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                "Grok network timeout after 4 attempts."
            )
            continue
        except openai.RateLimitError as e:
            _LOGGER.debug("Grok rooftop attempt %d rate-limited: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                "Grok API throttled (HTTP 429 Too Many Requests) after 4 attempts; retry later."
            )
            continue
        except openai.AuthenticationError:
            return _needs_review(
                "Grok API authentication error (HTTP 401): XAI_API_KEY may be invalid or revoked."
            )
        except openai.APIConnectionError as e:
            _LOGGER.debug("Grok rooftop attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Grok network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except openai.APIStatusError as e:
            code = getattr(e, "status_code", None) or 0
            if code in (408, 429) or 500 <= code < 600:
                _LOGGER.debug(
                    "Grok rooftop attempt %d transient (HTTP %s): %s",
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
        return _parse_response(shim, _RooftopResponse)

    return last_transient_result or _needs_review(
        "Grok network timeout after 4 attempts."
    )


def _verify_gemini_address(
    image_path: str,
    building_context: dict,
    n_boxes: int,
    timeout_s: int,
    context_image_path: str = None,
    closeup_image_path: str = None,
) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise KeyError("GEMINI_API_KEY")
    model_id = os.environ.get("GEMINI_MODEL") or _DEFAULT_GEMINI_MODEL

    try:
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    context_bytes = _maybe_read_context_tile(context_image_path)
    closeup_bytes = _maybe_read_context_tile(closeup_image_path)

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_address_prompt(building_context, n_boxes, len(pos_imgs), len(neg_imgs),
                                   has_closeup=closeup_bytes is not None)

    contents: list = [prompt]
    for img_bytes in pos_imgs:
        contents.append("--- Reference: positive example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    for img_bytes in neg_imgs:
        contents.append("--- Reference: negative example ---")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
    contents.append("--- Satellite tile (target footprint in red, YOLO candidates numbered) ---")
    contents.append(types.Part.from_bytes(data=tile_bytes, mime_type="image/jpeg"))
    if context_bytes is not None:
        contents.append(f"--- Context tile (same target, zoom {building_context.get('context_zoom')}) ---")
        contents.append(types.Part.from_bytes(data=context_bytes, mime_type="image/jpeg"))
    if closeup_bytes is not None:
        contents.append("--- High-zoom close-up of the main candidate equipment (identify fan blades vs water tank) ---")
        contents.append(types.Part.from_bytes(data=closeup_bytes, mime_type="image/jpeg"))

    client = _get_gemini_client(api_key)
    config = types.GenerateContentConfig(
        system_instruction=_ADDRESS_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=_VerificationResponse,
        thinking_config=types.ThinkingConfig(thinking_level=_GEMINI_THINKING_LEVEL),
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
            _LOGGER.debug("Address attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review("Network timeout after 4 attempts.")
            continue
        except httpx.ConnectError as e:
            _LOGGER.debug("Address attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except genai_errors.ServerError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None) or 500
            _LOGGER.debug("Address attempt %d server error (HTTP %s): %s", attempt + 1, code, _truncate(e))
            last_transient_result = _needs_review(
                f"API server error (HTTP {code}) after 4 attempts: {_truncate(e)}"
            )
            continue
        except genai_errors.ClientError as e:
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code in (408, 429):
                name = "Request Timeout" if code == 408 else "Too Many Requests"
                _LOGGER.debug("Address attempt %d throttled (HTTP %s): %s", attempt + 1, code, _truncate(e))
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

        return _parse_response(response, _VerificationResponse)

    return last_transient_result or _needs_review("Network timeout after 4 attempts.")


def _verify_grok_address(
    image_path: str,
    building_context: dict,
    n_boxes: int,
    timeout_s: int,
    context_image_path: str = None,
    closeup_image_path: str = None,
) -> dict:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise KeyError("XAI_API_KEY")
    model_id = os.environ.get("GROK_MODEL") or _DEFAULT_GROK_MODEL

    try:
        tile_bytes = _read_full_tile_bytes(image_path)
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
        )

    context_bytes = _maybe_read_context_tile(context_image_path)
    closeup_bytes = _maybe_read_context_tile(closeup_image_path)

    pos_imgs, neg_imgs = _load_reference_images()
    prompt = _build_address_prompt(building_context, n_boxes, len(pos_imgs), len(neg_imgs),
                                   has_closeup=closeup_bytes is not None)

    content_parts: list = [{"type": "text", "text": prompt}]
    for img_bytes in pos_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: positive example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    for img_bytes in neg_imgs:
        content_parts.append({"type": "text", "text": "--- Reference: negative example ---"})
        content_parts.append(_to_image_url_part(img_bytes))
    content_parts.append({"type": "text", "text": "--- Satellite tile (target footprint in red, YOLO candidates numbered) ---"})
    content_parts.append(_to_image_url_part(tile_bytes))
    if context_bytes is not None:
        content_parts.append({"type": "text", "text": f"--- Context tile (same target, zoom {building_context.get('context_zoom')}) ---"})
        content_parts.append(_to_image_url_part(context_bytes))
    if closeup_bytes is not None:
        content_parts.append({"type": "text", "text": "--- High-zoom close-up of the main candidate equipment (identify fan blades vs water tank) ---"})
        content_parts.append(_to_image_url_part(closeup_bytes))

    messages = [
        {"role": "system", "content": _ADDRESS_SYSTEM_PROMPT},
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
                reasoning_effort=_GROK_REASONING_EFFORT,
                timeout=timeout_s,
            )
        except openai.APITimeoutError as e:
            _LOGGER.debug("Grok address attempt %d timeout: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review("Grok network timeout after 4 attempts.")
            continue
        except openai.RateLimitError as e:
            _LOGGER.debug("Grok address attempt %d rate-limited: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                "Grok API throttled (HTTP 429 Too Many Requests) after 4 attempts; retry later."
            )
            continue
        except openai.AuthenticationError:
            return _needs_review(
                "Grok API authentication error (HTTP 401): XAI_API_KEY may be invalid or revoked."
            )
        except openai.APIConnectionError as e:
            _LOGGER.debug("Grok address attempt %d connect error: %s", attempt + 1, _truncate(e))
            last_transient_result = _needs_review(
                f"Grok network connection error after 4 attempts: {type(e).__name__}: {_truncate(e)}"
            )
            continue
        except openai.APIStatusError as e:
            code = getattr(e, "status_code", None) or 0
            if code in (408, 429) or 500 <= code < 600:
                _LOGGER.debug("Grok address attempt %d transient (HTTP %s): %s", attempt + 1, code, _truncate(e))
                last_transient_result = _needs_review(
                    f"Grok API transient error (HTTP {code}) after 4 attempts: {_truncate(e)}"
                )
                continue
            if code == 403:
                return _needs_review(f"Grok API authorization error (HTTP 403): {_truncate(e)}")
            if code == 400:
                return _needs_review(
                    f"Grok API rejected the request (HTTP 400): {_truncate(e)}. "
                    f"This usually indicates a malformed prompt or unsupported format."
                )
            return _needs_review(f"Grok API client error (HTTP {code}): {_truncate(e)}")
        except openai.APIError as e:
            return _needs_review(f"Grok VLM API error: {type(e).__name__}: {_truncate(e)}")

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as e:
            return _needs_review(f"Grok response shape unexpected: {_truncate(e)}")

        if not content:
            return _needs_review("Grok returned an empty response (no content).")

        shim = SimpleNamespace(parsed=None, text=_strip_md_fences(content))
        return _parse_response(shim, _VerificationResponse)

    return last_transient_result or _needs_review("Grok network timeout after 4 attempts.")


def verify_address(
    image_path: str,
    building_context: dict,
    n_boxes: int = 0,
    context_image_path: str = None,
    closeup_image_path: str = None,
) -> dict:
    """One dual-VLM pass per address. The VLM sees a single marked-up tile (target
    footprint in red, YOLO candidate boxes numbered) plus reference images, and
    returns one consensus verdict — verifying the boxes AND scanning for anything
    YOLO missed. Replaces the per-box verify_detection loop + the verify_rooftop
    scan. Same output shape as verify_detection (the 7-key consensus dict)."""
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
        with Image.open(image_path) as img:
            _ = img.size
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
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
            _verify_gemini_address, image_path, building_context, n_boxes, timeout_s,
            context_image_path, closeup_image_path,
        )
        grok_future = ex.submit(
            _verify_grok_address, image_path, building_context, n_boxes, timeout_s,
            context_image_path, closeup_image_path,
        )

        try:
            gemini_result = gemini_future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            gemini_result = _needs_review(f"Gemini exceeded {timeout_s}s wall-clock timeout.")

        try:
            grok_result = grok_future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            grok_result = _needs_review(f"Grok exceeded {timeout_s}s wall-clock timeout.")

    return _combine_verdicts(gemini_result, grok_result, threshold)


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
        final_is_house = bool(
            gemini_result["is_house"] and grok_result["is_house"]
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
        final_is_house = False

    return {
        "verdict": final_verdict,
        "confidence": final_confidence,
        "reasoning": final_reasoning,
        "construction": final_construction,
        "is_house": final_is_house,
        # If EITHER model couldn't see the target roof, flag for an imagery retry.
        "image_unusable": bool(gemini_result.get("image_unusable") or grok_result.get("image_unusable")),
        # Gemini-only: re-pull a wider view to rule out a ground-mounted CT outside a tight
        # frame. Deliberately NOT ORed with Grok — this judgment is Gemini's job alone.
        "frame_inadequate": bool(gemini_result.get("frame_inadequate")),
        "gemini": gemini_result,
        "grok": grok_result,
        "agreement": agree and confident,
    }


def verify_detection(
    image_path: str,
    detection_bbox: tuple[int, int, int, int],
    building_context: dict,
    context_image_path: str = None,
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
            _verify_gemini, image_path, detection_bbox, building_context, timeout_s,
            context_image_path,
        )
        grok_future = ex.submit(
            _verify_grok, image_path, detection_bbox, building_context, timeout_s,
            context_image_path,
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


def verify_rooftop(
    image_path: str,
    building_context: dict,
    context_image_path: str = None,
) -> dict:
    """Scan a target rooftop for cooling towers when YOLO found nothing.

    This is the "last line of defense" path: it fires when an earlier YOLO
    pass returned zero kept detections on the target rooftop. Instead of
    verifying a candidate detection, it asks the VLMs to scan the rooftop
    directly and report whether a cooling tower is present, plus whether
    active construction is visible.

    Args:
        image_path: Path to the full 768x768 satellite tile (JPEG/PNG),
            centered on the OSM building centroid.
        building_context: Dict describing the target building. Must contain
            either ``address`` (truthy str) or both ``lat`` and ``lon``
            (numeric). May additionally contain a ``footprint_metadata``
            sub-dict with ``osm_id``, ``tags``, and ``contains_point``.

    Returns:
        A dict with exactly seven keys, matching the shape of
        ``verify_detection``. The verdict literals come from the rooftop
        schema: ``cooling_tower_present``, ``cooling_tower_possible``,
        ``no_cooling_tower``, or ``needs_review``. Construction is True only
        when both models flagged active construction (same consensus rule
        as ``verify_detection``).

    Raises:
        KeyError: If either ``GEMINI_API_KEY`` or ``XAI_API_KEY`` is unset
            or empty.
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
        with Image.open(image_path) as img:
            _ = img.size
    except (UnidentifiedImageError, OSError) as e:
        return _needs_review(
            f"Image file unreadable: {os.path.basename(image_path)}: {_truncate(e)}"
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
            _verify_gemini_rooftop, image_path, building_context, timeout_s,
            context_image_path,
        )
        grok_future = ex.submit(
            _verify_grok_rooftop, image_path, building_context, timeout_s,
            context_image_path,
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

