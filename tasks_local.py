"""
Local storage version of task processor (replaces tasks_serverless.py).
Processes address lists using local filesystem instead of GCS.
"""
import os
import re
import math
import tempfile
import logging
import threading
import time
import concurrent.futures
import pandas as pd
from typing import Callable, Dict, Any, List, Optional, Tuple
from shapely.geometry import MultiPolygon
from shapely.ops import transform

from utils import (
    geocode_address_mapbox, geocode_with_confidence,
    get_satellite_image, get_streetview_image_google, validate_address_google,
    is_fully_qualified_address,
    YOLO_CONF, _get_models, ct_class_indices, MAPBOX_ZOOM, MAPBOX_ZOOM_WIDE,
)
from geometry import (
    get_building_footprint, classify_detections, filter_detections,
    extract_detections_from_yolo, ensemble_dedupe_detections, geo_dedupe_detections,
    TransientFootprintError,
)
from nyc_opendata import lookup_nyc_registry, _in_nyc
from vlm import verify_detection, verify_rooftop, verify_address
from pipeline_render import render_annotated_image, render_marked_tile

log = logging.getLogger("tasks")

# Verdict classes — used by _pick_winner and _build_notes.
# Mirror vlm.py's consensus buckets, with AMBIGUOUS broken out for winner-picker tiers.
_POSITIVE_VERDICTS = frozenset({
    "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
    "registry_confirmed",
})
_AMBIGUOUS_VERDICTS = frozenset({"needs_review"})
_NEGATIVE_VERDICTS = frozenset({
    "not_detected", "neighbor_only", "no_cooling_tower",
})

# Area gate: footprints smaller than this (square meters) are gated as
# likely_residential before any tile fetch / YOLO / VLM. Floor sits well below
# the smallest measured real cooling-tower building (720.8 sq m; see
# scratch_footprint_areas.py) so OSM trace noise can't gate out a real lead --
# the VLM house-check backstops the 200-720 band.
MIN_COMMERCIAL_FOOTPRINT_SQM = 200

# Area-gate master switch. Default ON. Set AREA_GATE_ENABLED=0 to disable the
# gate entirely so EVERY non-registry address gets VLM eyes (registry-only
# exclusion) -- safer recall at higher VLM cost.
AREA_GATE_ENABLED = os.environ.get("AREA_GATE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# Self-correcting imagery retry: when the VLM flags the target roof as not visible
# (image_unusable) and the verdict isn't a clear positive, re-pull the detail tile from
# Mapbox (a different satellite capture, often more top-down on tall buildings) and re-run.
IMAGERY_RETRY_ENABLED = os.environ.get("IMAGERY_RETRY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# Zoom for the high-zoom equipment close-up fed to verify_address (the detail tile is
# MAPBOX_ZOOM=19, too coarse to count fan blades — the close-up lets the VLM separate a
# cooling tower from a water tank).
CLOSEUP_ZOOM = int(os.getenv("CLOSEUP_ZOOM", "20"))

# Geocoder fallback: when the primary (Google) geocode yields no footprint or an
# ambiguous (non-containing) one, retry with the Mapbox geocode of the same address and
# adopt it if it lands inside a building.
GEOCODER_FALLBACK_ENABLED = os.environ.get("GEOCODER_FALLBACK_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# YOLO ensemble models are @lru_cache-shared singletons; ultralytics .predict()
# is not thread-safe. Serialize inference so concurrent addresses can't corrupt
# each other's detections. YOLO is CPU-bound (effectively serial anyway); the
# overlapped VLM wait is what the concurrency actually buys.
_yolo_lock = threading.Lock()

# Address-level concurrency: how many addresses' pipelines run at once. The win
# is overlapping the dual-VLM wait (YOLO and Overpass are lock-serialized). The
# web worker uses this default; the audit harness overrides per-run to diff 1 vs 5.
DEFAULT_VLM_ADDRESS_CONCURRENCY = 5
# Transient failures (throttled Overpass, transient VLM verdict) are re-queued and
# retried this many extra rounds before being emitted as unverified.
MAX_RETRY_ROUNDS = int(os.environ.get("VLM_RETRY_ROUNDS", "2"))
RETRY_BACKOFF_SECONDS = float(os.environ.get("VLM_RETRY_BACKOFF", "5"))
# Markers vlm.py stamps into reasoning on transient failures (timeout / server
# error / throttle). Present in consensus reasoning → retryable; a genuine
# low-confidence/disagreement/construction needs_review has none of these.
_VLM_TRANSIENT_MARKERS = (
    "network timeout", "network connection error",
    "server error (http", "throttled", "after 4 attempts",
)


# Dense urban cores where cooling towers are roof-only and off-building detections
# are neighbor false positives. NYC is handled via nyc_opendata._in_nyc; these are
# downtown bounding boxes (lat_min, lat_max, lon_min, lon_max) seeded from the cities
# Mike named (Boston, SF, Chicago) and extended per-city when neighbor FPs show up.
# Everywhere else defaults to ground-check ON (suburban DC/VA office parks etc., where
# ground-mounted units are real). Tune boxes against the 53-row set.
DENSE_CORE_BBOXES = [
    # name, lat_min, lat_max, lon_min, lon_max — TIGHT high-rise CBD boxes only. Kept
    # tight on purpose: outside the downtown core, ground-mounted units are real, so a
    # too-wide box would wrongly suppress the ground scan. A curated OSM density signal
    # was tested and rejected — suburban office parks (Herndon, Santa Clara) overlap
    # downtowns in built-up ratio, and height-limited downtowns (DC) score low — so this
    # stays a hand-maintained list of genuinely dense high-rise downtowns. Extend as new
    # cities appear; everywhere else defaults to ground-check ON.
    ("boston",        42.340, 42.366, -71.110, -71.045),
    ("chicago",       41.855, 41.910, -87.660, -87.605),
    ("sf",            37.765, 37.815, -122.435, -122.390),
    ("los_angeles",   34.036, 34.064, -118.272, -118.232),
    ("washington_dc", 38.888, 38.912, -77.052, -77.012),
    ("philadelphia",  39.942, 39.964, -75.176, -75.148),
    ("seattle",       47.594, 47.622, -122.348, -122.318),
    ("houston",       29.745, 29.770, -95.382, -95.352),
    ("miami",         25.758, 25.798, -80.205, -80.178),
    ("atlanta",       33.746, 33.792, -84.398, -84.376),
    ("dallas",        32.770, 32.794, -96.812, -96.785),
    ("denver",        39.733, 39.758, -105.005, -104.975),
    ("minneapolis",   44.963, 44.990, -93.285, -93.255),
    ("pittsburgh",    40.432, 40.452, -80.010, -79.986),
    ("baltimore",     39.280, 39.300, -76.628, -76.600),
    ("detroit",       42.320, 42.340, -83.060, -83.035),
    ("portland_or",   45.505, 45.530, -122.690, -122.665),
    ("austin",        30.258, 30.280, -97.750, -97.728),
    ("charlotte",     35.216, 35.238, -80.855, -80.832),
]


def _in_dense_core(lat: float, lon: float) -> bool:
    """True if the point is in a curated dense downtown core (suppress ground/neighbor
    detection). NYC is checked separately via nyc_opendata._in_nyc."""
    return any(la0 <= lat <= la1 and lo0 <= lon <= lo1
               for _, la0, la1, lo0, lo1 in DENSE_CORE_BBOXES)


def _detect_on_tile(
    tile_path: str,
    tile_zoom: int,
    footprint: Dict[str, Any],
    centroid_lat: float,
    centroid_lon: float,
    keep_outside: bool = None,
) -> List[Dict[str, Any]]:
    """Run the YOLO ensemble on one tile, classify against the footprint, and
    return kept detections tagged with tile provenance.

    Both models run on the tile; their outputs are deduped within the tile by
    pixel IoU (valid at a single zoom). Cross-tile dedupe across zoom levels is
    done later in geo space via geo_dedupe_detections. filter_detections honors
    YOLO_KEEP_OUTSIDE so off-building (ground) candidates pass through to the VLM.
    """
    ensemble_dets: List[Dict[str, Any]] = []
    with _yolo_lock:
        for label, model in _get_models():
            ct_idx = ct_class_indices(model)
            if not ct_idx:
                log.warning(f"  [z{tile_zoom}] {label}: no cooling_tower class in {model.names}; skipping")
                continue
            yolo_result = model.predict(source=tile_path, conf=YOLO_CONF, verbose=False)[0]
            raw = extract_detections_from_yolo(yolo_result)
            filtered = [d for d in raw if d.get('class') in ct_idx]
            for d in filtered:
                d['source_model'] = label
            ensemble_dets.extend(filtered)

    deduped = ensemble_dedupe_detections(ensemble_dets, iou_threshold=0.5)
    classified = classify_detections(
        deduped, footprint, centroid_lat, centroid_lon, tile_zoom, 768, 768
    )
    kept, _rejected = filter_detections(classified, keep_outside=keep_outside)
    for d in kept:
        d['tile_zoom'] = tile_zoom
        d['tile_path'] = tile_path
    log.info(
        f"  [z{tile_zoom}] ensemble union {len(ensemble_dets)} → dedupe {len(deduped)} → kept {len(kept)}"
    )
    return kept


def _pick_winner(enriched_detections: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the winning detection per class-rank-then-confidence (Fork 4).

    Each input dict has 'bbox', 'confidence' (YOLO), 'location', 'det_latlon',
    'distance_to_building', and 'vlm_result' (7-key consensus dict).

    Sort within a class: (consensus_conf DESC, yolo_conf DESC, index ASC).
    All-AMBIGUOUS fallback: consensus_conf is uniformly 0.0, so sort by
    (yolo_conf DESC, index ASC).
    """
    positives = [(i, d) for i, d in enumerate(enriched_detections)
                 if d['vlm_result']['verdict'] in _POSITIVE_VERDICTS]
    if positives:
        positives.sort(key=lambda x: (
            -x[1]['vlm_result']['confidence'],
            -x[1]['confidence'],
            x[0],
        ))
        return positives[0][1]

    negatives = [(i, d) for i, d in enumerate(enriched_detections)
                 if d['vlm_result']['verdict'] in _NEGATIVE_VERDICTS]
    if negatives:
        negatives.sort(key=lambda x: (
            -x[1]['vlm_result']['confidence'],
            -x[1]['confidence'],
            x[0],
        ))
        return negatives[0][1]

    candidates = list(enumerate(enriched_detections))
    candidates.sort(key=lambda x: (-x[1]['confidence'], x[0]))
    return candidates[0][1]


def _build_notes(row_state: Dict[str, Any]) -> str:
    """Synthesize the Notes column per Fork 5 rules.

    Tier 1 (terminal, first-match-wins): footprint_missing / geocode failure /
        imagery failure / needs_review.
    Tier 2 (base note from verdict + path): POSITIVE rooftop / POSITIVE
        detection / NEGATIVE detection / NEGATIVE rooftop without construction.
    Tier 3 (append): NEGATIVE + construction → CoStar follow-up note.
    Tier 4 (append): winner is a boundary detection → geometric-ambiguity
        suffix.
    """
    verdict = row_state.get('verdict')

    if verdict == 'registry_confirmed':
        return row_state.get('registry_citation', 'Confirmed via NYC OpenData registry')
    if verdict == 'footprint_missing':
        both = " (Google + Mapbox geocoders both tried)" if row_state.get('geocoder_retried') else ""
        return f"No OSM footprint found, manual verification needed{both}"
    if verdict == 'likely_residential':
        return (f"Footprint below commercial size floor ({MIN_COMMERCIAL_FOOTPRINT_SQM} sq m) "
                "- likely residential; manual verification recommended")
    if verdict == 'ambiguous_footprint':
        edge = row_state.get('edge_distance_m')
        src = row_state.get('footprint_source')
        both = " Google + Mapbox geocoders both tried." if row_state.get('geocoder_retried') else ""
        if edge is not None:
            return (f"Geocoded point falls {edge:.0f} m outside the nearest building footprint "
                    f"(source: {src}; OSM + planimetric/Microsoft fallback checked).{both} No dataset "
                    f"places the address inside a building - likely an UPSTREAM geocode/footprint "
                    f"alignment issue rather than a missing rooftop. Manual verification recommended.")
        return ("Geocoded point falls outside any building footprint "
                f"(nearest-match area unreliable); manual verification recommended.{both}")
    if row_state.get('geocode_failed'):
        return "Address could not be geocoded"
    if row_state.get('imagery_failed'):
        return "Satellite image could not be downloaded"
    if row_state.get('construction_review'):
        return "Construction visible — Mapbox imagery may be stale; flagged for manual review/lookup"
    if verdict == 'needs_review':
        return "Manual review recommended"

    detection_count = row_state.get('detection_count', 0)
    rooftop_path = row_state.get('rooftop_path', False)
    construction = row_state.get('construction', False)
    winner_is_boundary = row_state.get('winner_is_boundary', False)

    base = ""
    if verdict in _POSITIVE_VERDICTS:
        if rooftop_path:
            base = "Detected via rooftop scan (no YOLO candidates)"
        elif detection_count > 0:
            base = f"High confidence detection ({detection_count} candidates evaluated)"
    elif verdict in _NEGATIVE_VERDICTS:
        if not rooftop_path and detection_count > 0:
            base = f"YOLO found {detection_count} candidates, VLM rejected all"
        elif rooftop_path and not construction:
            base = "No cooling tower detected"

    if verdict in _NEGATIVE_VERDICTS and construction:
        construction_note = "No cooling tower, but construction visible — investigate via CoStar"
        base = f"{base}. {construction_note}" if base else construction_note

    if winner_is_boundary:
        boundary_suffix = " (geometrically ambiguous — boundary detection)"
        base = f"{base}{boundary_suffix}" if base else boundary_suffix.lstrip()

    return base


def _centroid_latlon(footprint: Dict[str, Any]) -> Tuple[float, float]:
    """Compute centroid lat/lon. Handles MultiPolygon by picking largest sub-polygon."""
    polygon = footprint['polygon']
    if isinstance(polygon, MultiPolygon):
        polygon = max(polygon.geoms, key=lambda p: p.area)
    centroid = polygon.centroid
    return (centroid.y, centroid.x)


def _closeup_center(dets, fallback_lat, fallback_lon):
    """Center for the high-zoom equipment close-up: the strongest YOLO detection's
    location, else the footprint centroid (when YOLO found nothing)."""
    cands = [d for d in dets if d.get('det_latlon')]
    if cands:
        return max(cands, key=lambda d: d.get('confidence', 0.0))['det_latlon']
    return fallback_lat, fallback_lon


def _footprint_area_m2(footprint: Dict[str, Any]) -> float:
    """Footprint area in square meters via a local equirectangular projection
    about the centroid (Option A -- no pyproj). Accurate to <0.1% at building
    scale. Projects the whole geometry, so MultiPolygon areas sum correctly."""
    polygon = footprint['polygon']
    c = polygon.centroid
    lat0, lon0 = c.y, c.x
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    projected = transform(
        lambda lon, lat: ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat),
        polygon,
    )
    return projected.area


def _build_web_entry(
    full_address: str,
    verdict: str,
    consensus_dict: Optional[Dict[str, Any]],
    detection_count: int,
    construction: bool,
    notes: str,
    original_url: Optional[str],
    result_url: Optional[str],
    error: Optional[str] = None,
    result_url_wide: Optional[str] = None,
    result_url_streetview: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a web_results entry. Keeps the shared keys the report/CSV renderers expect
    (address, confidence_score, result_image_url, original_image_url, error) plus
    the new pipeline fields surfaced for downstream consumers. result_url_wide is
    the annotated wide-context tile (None for non-VLM rows). result_url_streetview
    is a ground-level Google Street View photo (None when there's no coverage).
    """
    if consensus_dict is not None:
        confidence_score = consensus_dict.get('confidence')
        reasoning = consensus_dict.get('reasoning', '')
        agreement = bool(consensus_dict.get('agreement', False))
        gemini = consensus_dict.get('gemini') if isinstance(consensus_dict.get('gemini'), dict) else {}
        grok = consensus_dict.get('grok') if isinstance(consensus_dict.get('grok'), dict) else {}
        gemini_verdict = gemini.get('verdict', '')
        gemini_confidence = gemini.get('confidence')
        grok_verdict = grok.get('verdict', '')
        grok_confidence = grok.get('confidence')
        is_house = bool(consensus_dict.get('is_house'))
        gemini_is_house = gemini.get('is_house')
        grok_is_house = grok.get('is_house')
        gemini_reasoning = gemini.get('reasoning', '')
        grok_reasoning = grok.get('reasoning', '')
        model_path = consensus_dict.get('model_path', '')
        grok_fallback_used = bool(consensus_dict.get('grok_fallback_used', False))
    else:
        confidence_score = None
        reasoning = ''
        agreement = False
        gemini_verdict = ''
        gemini_confidence = None
        grok_verdict = ''
        grok_confidence = None
        is_house = None
        gemini_is_house = None
        grok_is_house = None
        gemini_reasoning = ''
        grok_reasoning = ''
        model_path = ''
        grok_fallback_used = False

    entry = {
        "address": full_address,
        "confidence_score": confidence_score,
        "result_image_url": result_url,
        "result_image_url_wide": result_url_wide,
        "result_image_url_streetview": result_url_streetview,
        "original_image_url": original_url,
        "verdict": verdict,
        "detection_count": detection_count,
        "construction": construction,
        "reasoning": reasoning,
        "agreement": agreement,
        "gemini_verdict": gemini_verdict,
        "gemini_confidence": gemini_confidence,
        "gemini_reasoning": gemini_reasoning,
        "grok_verdict": grok_verdict,
        "grok_confidence": grok_confidence,
        "grok_reasoning": grok_reasoning,
        "model_path": model_path,
        "grok_fallback_used": grok_fallback_used,
        "is_house": is_house,
        "gemini_is_house": gemini_is_house,
        "grok_is_house": grok_is_house,
        "notes": notes,
    }
    if error:
        entry["error"] = error
    return entry


def _build_csv_row(
    full_address: str,
    verdict: str,
    consensus_dict: Optional[Dict[str, Any]],
    detection_count: int,
    construction: bool,
    notes: str,
    original_url: Optional[str],
    result_url: Optional[str],
) -> Dict[str, Any]:
    """Build a CSV row dict matching the 15-column Fork 5 schema. Also consumed by csv_export.web_results_to_csv_rows for headless runners; do not delete as dead code."""
    if consensus_dict is not None:
        confidence = consensus_dict.get('confidence')
        conf_display = f"{(confidence * 100):.1f}%" if isinstance(confidence, (int, float)) else "N/A"
        reasoning = consensus_dict.get('reasoning', '')
        agreement = bool(consensus_dict.get('agreement', False))
        gemini = consensus_dict.get('gemini') if isinstance(consensus_dict.get('gemini'), dict) else {}
        grok = consensus_dict.get('grok') if isinstance(consensus_dict.get('grok'), dict) else {}
        gemini_verdict = gemini.get('verdict', '')
        gemini_confidence = gemini.get('confidence')
        grok_verdict = grok.get('verdict', '')
        grok_confidence = grok.get('confidence')
    else:
        conf_display = "N/A"
        reasoning = ''
        agreement = False
        gemini_verdict = ''
        gemini_confidence = None
        grok_verdict = ''
        grok_confidence = None

    detected = "Yes" if verdict in _POSITIVE_VERDICTS else "No"

    return {
        'Address': full_address,
        'Detected': detected,
        'Confidence': conf_display,
        'Verdict': verdict,
        'Detection_Count': detection_count,
        'Construction': construction,
        'Reasoning': reasoning,
        'Notes': notes,
        'Agreement': agreement,
        'Gemini_Verdict': gemini_verdict,
        'Gemini_Confidence': gemini_confidence,
        'Grok_Verdict': grok_verdict,
        'Grok_Confidence': grok_confidence,
        'Original_Image': original_url or '',
        'Detection_Result': result_url or '',
    }


def _compose_address(row, columns) -> str:
    """Assemble the full address string from the row (Address + optional
    Boro_Area + Zip). Shared by _process_one_address and the unresolved-failure
    emit so they label the same address identically."""
    parts = [str(row['Address']).strip()]
    if 'Boro_Area' in columns and pd.notna(row.get('Boro_Area')):
        parts.append(str(row['Boro_Area']).strip())
    if 'Zip' in columns and pd.notna(row.get('Zip')):
        z = row['Zip']
        parts.append(str(int(z)) if isinstance(z, float) else str(z))
    return ", ".join(parts)


def _is_transient_vlm(entry: Dict[str, Any]) -> bool:
    """True if a needs_review verdict was caused by a transient VLM failure
    (timeout / server error / throttle) rather than genuine uncertainty --
    detected via the marker strings vlm.py puts in the reasoning. Retryable;
    a real low-confidence/disagreement/construction needs_review is not."""
    if entry.get('verdict') != 'needs_review':
        return False
    reason = (entry.get('reasoning') or '').lower()
    return any(m in reason for m in _VLM_TRANSIENT_MARKERS)


def _process_one_address(
    row,
    i: int,
    columns,
    total: int,
    job_id: str,
    upload_file: Callable[[str, str], str],
    make_signed_url: Callable[[str], str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Thin wrapper around the per-address pipeline that injects the geocoder
    agreement flag (geocode_confidence / geocode_divergence_m) at a single point,
    so the core's many return branches don't each have to thread it through."""
    geo: Dict[str, Any] = {}
    web_entry, csv_row = _process_one_address_core(
        row, i, columns, total, job_id, upload_file, make_signed_url, geo
    )
    web_entry["geocode_confidence"] = geo.get("confidence")
    web_entry["geocode_divergence_m"] = geo.get("divergence_m")
    csv_row["geocode_confidence"] = geo.get("confidence")
    return web_entry, csv_row


def _process_one_address_core(
    row,
    i: int,
    columns,
    total: int,
    job_id: str,
    upload_file: Callable[[str, str], str],
    make_signed_url: Callable[[str], str],
    geo: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Process a single address row and return its (web_entry, csv_row) pair.

    Extracted verbatim from the per-address body of process_address_list so the
    orchestrator can run addresses concurrently. Owns its own temp-file cleanup
    and does NOT touch shared accumulators, progress, or partial-result
    streaming -- those stay in the orchestrator. Lets TransientFootprintError
    (from get_building_footprint on throttle-exhausted Overpass) propagate so the
    orchestrator can re-queue; a genuine miss still returns a footprint_missing
    pair as before.
    """
    # Empty-address guard
    if pd.isna(row['Address']) or str(row['Address']).strip() == '':
        log.warning(f"Row {i}: Empty address, skipping")
        return (
            _build_web_entry(
                full_address="(Empty)", verdict="", consensus_dict=None,
                detection_count=0, construction=False,
                notes="Empty address in CSV",
                original_url=None, result_url=None,
                error="Empty Address",
            ),
            _build_csv_row(
                full_address="(Empty)", verdict="", consensus_dict=None,
                detection_count=0, construction=False,
                notes="Empty address in CSV",
                original_url=None, result_url=None,
            ),
        )

    # Build address string (no NY append — Step 4 stripped it)
    full_address = _compose_address(row, columns)

    # Geocode (+ free geocoder-agreement confidence flag, stashed into `geo`)
    log.info(f"Row {i+1}/{total}: Geocoding source address")
    geo_lat, geo_lon, geo["confidence"], geo["divergence_m"] = geocode_with_confidence(full_address)
    if geo_lat is None:
        log.warning(f"Row {i+1}/{total}: Geocoding failed")
        notes = _build_notes({'geocode_failed': True})
        return (
            _build_web_entry(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
                error="Geocoding Failed",
            ),
            _build_csv_row(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
            ),
        )

    # Address Validation gate (upstream, before any spend): Google's Address Validation
    # API flags whether it had to infer/could-not-confirm address components. A non-
    # "confirmed" verdict correlates with our ambiguous_footprint / neighbor_only failures,
    # so it (a) gets surfaced in the notes and (b) force-runs the Mapbox geocoder cross-
    # check below even when Google's point happens to land inside a (possibly wrong)
    # building. Fail-open: validate_address_google returns None on any API error.
    addr_val = validate_address_google(full_address)
    addr_poor = bool(addr_val and addr_val.get("verdict") != "confirmed")
    if addr_poor:
        log.info(f"Row {i+1}/{total}: Address Validation verdict "
                 f"'{addr_val['verdict']}' (granularity={addr_val.get('granularity')})")

    # Footprint lookup BEFORE any Mapbox tile fetch (one Mapbox call per address)
    log.info(f"Row {i+1}/{total}: Looking up OSM building footprint")
    footprint = get_building_footprint(geo_lat, geo_lon)

    # Geocoder fallback: if the Google geocode led to NO footprint or a non-containing
    # (ambiguous) one — OR Address Validation flagged the address as poor — try the Mapbox
    # geocode of the same address — it often resolves to a different point that DOES sit
    # inside a building. Adopt it only when it lands inside a footprint. geocoder_retried
    # records that both geocoders were tried (for the reviewer-facing notes).
    # TransientFootprintError still propagates for re-queue.
    geocoder_retried = False
    addr_val_diverged = False
    google_uncontained = (footprint is None or not footprint.get('contains_point'))
    if GEOCODER_FALLBACK_ENABLED and (google_uncontained or addr_poor):
        try:
            mb_point = geocode_address_mapbox(full_address)
        except Exception:
            mb_point = None
        if mb_point and (abs(mb_point[0] - geo_lat) > 1e-6 or abs(mb_point[1] - geo_lon) > 1e-6):
            mb_fp = get_building_footprint(mb_point[0], mb_point[1])
            mb_inside = mb_fp is not None and mb_fp.get('contains_point')
            if google_uncontained:
                # Google missed / sat outside any building: adopt Mapbox only when it lands
                # inside one (unchanged conservative behavior).
                geocoder_retried = True
                if mb_inside:
                    log.info(f"Row {i+1}/{total}: Google geocode gave "
                             f"{'no footprint' if footprint is None else 'an ambiguous footprint'}; "
                             f"Mapbox geocode lands inside a building — adopting it")
                    geo_lat, geo_lon = mb_point
                    footprint = mb_fp
            elif mb_inside and mb_fp.get('osm_id') != footprint.get('osm_id'):
                # Google IS inside a footprint but Address Validation flagged the address,
                # and Mapbox lands inside a DIFFERENT building. Don't auto-adopt (Google's
                # containment may well be correct) — flag the divergence for the human pile.
                addr_val_diverged = True
                log.info(f"Row {i+1}/{total}: Address Validation flagged '{addr_val['verdict']}'; "
                         f"Mapbox geocode lands in a different building "
                         f"(OSM {mb_fp.get('osm_id')} vs {footprint.get('osm_id')}) — flagging divergence")

    # Reviewer-facing Address Validation note, built once and prepended at every exit below.
    addr_val_note = ""
    if addr_poor:
        addr_val_note = (
            f"[ADDRESS VALIDATION: {addr_val['verdict']}; inferred={addr_val['has_inferred']} "
            f"unconfirmed={addr_val['has_unconfirmed']} granularity={addr_val.get('granularity') or 'n/a'}"
            + ("; Mapbox geocode diverged to a different building" if addr_val_diverged else "")
            + "]"
        )

    clean_addr = re.sub(r'[\\/*?:"<>| ,]', '_', str(row['Address'])[:50])
    original_local = os.path.join(
        tempfile.gettempdir(),
        f"{job_id}_{i}_{clean_addr}_original.jpg",
    )
    annotated_local = os.path.join(
        tempfile.gettempdir(),
        f"{job_id}_{i}_{clean_addr}_annotated.jpg",
    )
    annotated_wide_local = os.path.join(
        tempfile.gettempdir(),
        f"{job_id}_{i}_{clean_addr}_annotated_wide.jpg",
    )

    # ====================================================================
    # BRANCH A — footprint_missing: fetch geocoded-point tile, no VLM
    # ====================================================================
    if footprint is None:
        log.info(f"Row {i+1}/{total}: No OSM footprint found, recording footprint_missing")
        ok = get_satellite_image(geo_lat, geo_lon, original_local)
        if not ok:
            log.warning(f"Row {i+1}/{total}: Footprint missing AND imagery failed")
            notes = _build_notes({'imagery_failed': True})
            return (
                _build_web_entry(
                    full_address=full_address, verdict="", consensus_dict=None,
                    detection_count=0, construction=False, notes=notes,
                    original_url=None, result_url=None,
                    error="Image Download Failed",
                ),
                _build_csv_row(
                    full_address=full_address, verdict="", consensus_dict=None,
                    detection_count=0, construction=False, notes=notes,
                    original_url=None, result_url=None,
                ),
            )

        original_blob = f"uploads/{job_id}/{os.path.basename(original_local)}"
        upload_file(original_local, original_blob)
        original_url = make_signed_url(original_blob)

        render_annotated_image(
            raw_image_path=original_local,
            output_path=annotated_local,
            footprint=None,
            centroid_lat=None,
            centroid_lon=None,
            enriched_detections=[],
            winner=None,
        )
        result_blob = f"results/{job_id}/{os.path.basename(annotated_local)}"
        upload_file(annotated_local, result_blob)
        result_url = make_signed_url(result_blob)

        notes = _build_notes({'verdict': 'footprint_missing', 'geocoder_retried': geocoder_retried})
        if addr_val_note:
            notes = f"{addr_val_note} {notes}".strip()
        web_entry = _build_web_entry(
            full_address=full_address,
            verdict='footprint_missing',
            consensus_dict=None,
            detection_count=0,
            construction=False,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
        )
        csv_row = _build_csv_row(
            full_address=full_address,
            verdict='footprint_missing',
            consensus_dict=None,
            detection_count=0,
            construction=False,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
        )

        for p in (original_local, annotated_local):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                log.warning(f"Could not clean up temp file {p}: {e}")

        return web_entry, csv_row

    # ====================================================================
    # BRANCH B — footprint found: centroid-centered tile + YOLO + VLM
    # ====================================================================
    log.info(f"Row {i+1}/{total}: Footprint found (OSM ID {footprint.get('osm_id')})")
    centroid_lat, centroid_lon = _centroid_latlon(footprint)

    # ====================================================================
    # AREA GATE -- cheapest exit, BEFORE any Mapbox tile fetch / YOLO / VLM.
    # ====================================================================
    if AREA_GATE_ENABLED and not footprint.get('contains_point'):
        log.info(f"Row {i+1}/{total}: Footprint is a nearest-building fallback "
                 f"(contains_point=False); routing to review as ambiguous_footprint")
        notes = _build_notes({
            'verdict': 'ambiguous_footprint',
            'edge_distance_m': footprint.get('edge_distance_m'),
            'footprint_source': footprint.get('source'),
            'geocoder_retried': geocoder_retried,
        })
        if addr_val_note:
            notes = f"{addr_val_note} {notes}".strip()
        return (
            _build_web_entry(
                full_address=full_address, verdict='ambiguous_footprint',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ),
            _build_csv_row(
                full_address=full_address, verdict='ambiguous_footprint',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ),
        )

    footprint_area = _footprint_area_m2(footprint) if AREA_GATE_ENABLED else 0.0
    if AREA_GATE_ENABLED and footprint_area < MIN_COMMERCIAL_FOOTPRINT_SQM:
        log.info(f"Row {i+1}/{total}: Footprint {footprint_area:.0f} sq m < "
                 f"{MIN_COMMERCIAL_FOOTPRINT_SQM} sq m floor; gating as likely_residential")
        notes = _build_notes({'verdict': 'likely_residential'})
        # Still fetch + render detail + wide tiles so a human can eyeball the gate
        # decision — a small/wrong footprint on a real commercial building (e.g. a
        # supertall geocoded to a tiny polygon) is caught by looking at the imagery
        # (requirement: every report carries imagery incl. the wide view).
        gate_provider = "mapbox" if (_in_nyc(centroid_lat, centroid_lon)
                                     or _in_dense_core(centroid_lat, centroid_lon)) else None
        original_url = result_url = result_url_wide = None
        if get_satellite_image(centroid_lat, centroid_lon, original_local, provider=gate_provider):
            original_blob = f"uploads/{job_id}/{os.path.basename(original_local)}"
            upload_file(original_local, original_blob)
            original_url = make_signed_url(original_blob)
            render_annotated_image(
                raw_image_path=original_local, output_path=annotated_local,
                footprint=footprint, centroid_lat=centroid_lat, centroid_lon=centroid_lon,
                enriched_detections=[], winner=None)
            result_blob = f"results/{job_id}/{os.path.basename(annotated_local)}"
            upload_file(annotated_local, result_blob)
            result_url = make_signed_url(result_blob)
        gate_wide = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_wide.jpg")
        if get_satellite_image(centroid_lat, centroid_lon, gate_wide, zoom=MAPBOX_ZOOM_WIDE, provider=gate_provider):
            render_annotated_image(
                raw_image_path=gate_wide, output_path=annotated_wide_local,
                footprint=footprint, centroid_lat=centroid_lat, centroid_lon=centroid_lon,
                enriched_detections=[], winner=None, zoom=MAPBOX_ZOOM_WIDE)
            wide_blob = f"results/{job_id}/{os.path.basename(annotated_wide_local)}"
            upload_file(annotated_wide_local, wide_blob)
            result_url_wide = make_signed_url(wide_blob)
        return (
            _build_web_entry(
                full_address=full_address, verdict='likely_residential',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=original_url, result_url=result_url,
                result_url_wide=result_url_wide,
            ),
            _build_csv_row(
                full_address=full_address, verdict='likely_residential',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=original_url, result_url=result_url,
            ),
        )

    # Per-address routing off the dense-urban gate (NYC cores + curated downtowns):
    #  - imagery: dense → Mapbox (true nadir; Google's 3D photogrammetry distorts dense
    #    rooftops → false negatives, +7 net on last session's 85-address A/B). Suburban
    #    stays Google (IMAGERY_PROVIDER env default).
    #  - detection: dense → roof-only (skip wide ground tile, drop off-building boxes as
    #    neighbor FPs). Suburban keeps both (ground-mounted units are real there).
    # alt_provider = the opposite, used as the image_unusable retry's alternate capture.
    dense = _in_nyc(centroid_lat, centroid_lon) or _in_dense_core(centroid_lat, centroid_lon)
    keep_outside = False if dense else None  # None = honor YOLO_KEEP_OUTSIDE env
    img_provider = "mapbox" if dense else None  # None = IMAGERY_PROVIDER env default (Google)
    alt_provider = "google" if dense else "mapbox"

    log.info(f"Row {i+1}/{total}: Downloading centroid-centered satellite image"
             + (" (dense → Mapbox)" if dense else ""))
    ok = get_satellite_image(centroid_lat, centroid_lon, original_local, provider=img_provider)
    if not ok:
        log.warning(f"Row {i+1}/{total}: Image download failed")
        notes = _build_notes({'imagery_failed': True})
        return (
            _build_web_entry(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
                error="Image Download Failed",
            ),
            _build_csv_row(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
            ),
        )

    original_blob = f"uploads/{job_id}/{os.path.basename(original_local)}"
    upload_file(original_local, original_blob)
    original_url = make_signed_url(original_blob)

    # ====================================================================
    # Registry-first: BIN-matched NYC OpenData hit → confirm, skip YOLO+VLM
    # ====================================================================
    registry = {'confirmed': False}
    if is_fully_qualified_address(full_address):
        registry = lookup_nyc_registry(geo_lat, geo_lon, footprint)
    else:
        log.info(
            f"Row {i+1}/{total}: Address not fully-qualified (no ZIP); "
            f"registry skip ineligible, running full pipeline"
        )
    if registry['confirmed']:
        log.info(
            f"Row {i+1}/{total}: Registry-confirmed "
            f"({registry['source']}, BIN {registry['bin']}); skipping YOLO+VLM"
        )
        render_annotated_image(
            raw_image_path=original_local,
            output_path=annotated_local,
            footprint=footprint,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            enriched_detections=[],
            winner=None,
        )
        result_blob = f"results/{job_id}/{os.path.basename(annotated_local)}"
        upload_file(annotated_local, result_blob)
        result_url = make_signed_url(result_blob)

        result_url_streetview = None
        streetview_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_streetview.jpg")
        if get_streetview_image_google(centroid_lat, centroid_lon, streetview_local):
            sv_blob = f"results/{job_id}/{os.path.basename(streetview_local)}"
            upload_file(streetview_local, sv_blob)
            result_url_streetview = make_signed_url(sv_blob)

        # Wide/aerial context tile so registry-confirmed rows also carry the wide view
        # for human review (requirement: every report has one, no matter the path).
        result_url_wide = None
        wide_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_wide.jpg")
        annotated_wide_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_annotated_wide.jpg")
        if get_satellite_image(centroid_lat, centroid_lon, wide_local, zoom=MAPBOX_ZOOM_WIDE, provider=img_provider):
            render_annotated_image(
                raw_image_path=wide_local,
                output_path=annotated_wide_local,
                footprint=footprint,
                centroid_lat=centroid_lat,
                centroid_lon=centroid_lon,
                enriched_detections=[],
                winner=None,
                zoom=MAPBOX_ZOOM_WIDE,
            )
            wide_blob = f"results/{job_id}/{os.path.basename(annotated_wide_local)}"
            upload_file(annotated_wide_local, wide_blob)
            result_url_wide = make_signed_url(wide_blob)

        notes = _build_notes({
            'verdict': 'registry_confirmed',
            'registry_citation': registry['citation'],
        })
        web_entry = _build_web_entry(
            full_address=full_address,
            verdict='registry_confirmed',
            consensus_dict=None,
            detection_count=0,
            construction=False,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
            result_url_streetview=result_url_streetview,
            result_url_wide=result_url_wide,
        )
        csv_row = _build_csv_row(
            full_address=full_address,
            verdict='registry_confirmed',
            consensus_dict=None,
            detection_count=0,
            construction=False,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
        )

        for p in (original_local, annotated_local, streetview_local, wide_local, annotated_wide_local):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                log.warning(f"Could not clean up temp file {p}: {e}")

        return web_entry, csv_row

    # Always fetch the wide/context tile so EVERY report carries an aerial view for human
    # review (requirement: wide view in every report, no matter what). The dense-core gate
    # governs DETECTION/VLM, not display: in dense cores cooling towers are roof-only and
    # off-building detections are neighbor false positives, so below we skip YOLO on the
    # wide tile and don't feed it to the VLM — it is display-only there. Outside dense cores
    # the wide tile also drives ground-mounted-CT detection and VLM context.
    wide_local = os.path.join(
        tempfile.gettempdir(),
        f"{job_id}_{i}_{clean_addr}_wide.jpg",
    )
    log.info(f"Row {i+1}/{total}: Downloading wide (z{MAPBOX_ZOOM_WIDE}) satellite image"
             + (" (dense: display-only)" if dense else ""))
    if not get_satellite_image(centroid_lat, centroid_lon, wide_local, zoom=MAPBOX_ZOOM_WIDE, provider=img_provider):
        log.warning(f"Row {i+1}/{total}: Wide tile fetch failed; proceeding with detail tile only")
        wide_local = None

    log.info(f"Row {i+1}/{total}: Running YOLO ensemble at conf={YOLO_CONF}"
             + (" (dense: roof-only)" if dense else " on both zooms"))
    detail_kept = _detect_on_tile(
        original_local, MAPBOX_ZOOM, footprint, centroid_lat, centroid_lon, keep_outside=keep_outside)
    # Detect on the wide tile only outside dense cores (in dense it is display-only).
    wide_kept = (
        _detect_on_tile(wide_local, MAPBOX_ZOOM_WIDE, footprint, centroid_lat, centroid_lon, keep_outside=keep_outside)
        if (wide_local and not dense) else []
    )
    merged = geo_dedupe_detections(detail_kept + wide_kept, dist_threshold_m=10.0)
    log.info(
        f"  Cross-zoom: detail {len(detail_kept)} + wide {len(wide_kept)} "
        f"→ {len(merged)} after geo dedupe"
    )

    base_ctx = {
        "address": full_address,
        "lat": centroid_lat,
        "lon": centroid_lon,
        "footprint_metadata": {
            "osm_id": footprint.get('osm_id'),
            "tags": footprint.get('tags', {}),
            "contains_point": footprint.get('contains_point'),
        },
    }

    # Single VLM pass per address (was: one dual-VLM call per box). Mark the tile(s)
    # with the red footprint + numbered YOLO boxes from both models and ask ONE
    # consensus question — verify the boxes AND scan for anything YOLO missed. The
    # detail tile is always sent; the wide tile only when one was fetched (sparse rows).
    detail_dets = [d for d in merged if d.get('tile_path') == original_local]
    wide_dets = [d for d in merged if d.get('tile_path') == wide_local] if wide_local else []
    detection_count = len(merged)

    marked_detail = os.path.join(
        tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_marked.jpg")
    render_marked_tile(original_local, marked_detail, footprint,
                       centroid_lat, centroid_lon, detail_dets, zoom=MAPBOX_ZOOM)
    # The VLM gets the wide tile as context only outside dense cores (preserve dense
    # roof-only verify behavior); the wide tile still renders into the report below.
    marked_wide = None
    if wide_local and not dense:
        marked_wide = os.path.join(
            tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_marked_wide.jpg")
        render_marked_tile(wide_local, marked_wide, footprint,
                           centroid_lat, centroid_lon, wide_dets, zoom=MAPBOX_ZOOM_WIDE)

    addr_ctx = {
        **base_ctx,
        "tile_zoom": MAPBOX_ZOOM,
        "context_zoom": MAPBOX_ZOOM_WIDE if marked_wide else None,
    }
    # High-zoom close-up of the strongest candidate (or roof center) so the VLM can make
    # the fan-blade / water-tank identity call it can't make at the coarse detail zoom.
    cu_lat, cu_lon = _closeup_center(merged, centroid_lat, centroid_lon)
    closeup_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_closeup.jpg")
    if not get_satellite_image(cu_lat, cu_lon, closeup_local, zoom=CLOSEUP_ZOOM, provider=img_provider):
        closeup_local = None
    log.info(
        f"Row {i+1}/{total}: {detection_count} candidate box(es); "
        f"running single verify_address pass"
    )
    consensus_dict = verify_address(
        marked_detail, addr_ctx, n_boxes=detection_count,
        context_image_path=marked_wide, closeup_image_path=closeup_local,
    )
    rooftop_path = (detection_count == 0)
    winner_is_boundary = False
    # For the audit render, tag every box with the single consensus verdict so the
    # output tiles colour-code consistently (no per-box winner in the 1-call design).
    enriched = [{**det, 'vlm_result': consensus_dict} for det in merged]
    winner = None

    # Self-correcting imagery retry: if the VLM couldn't see the target's roof (e.g. a
    # supertall leaning in Google's oblique capture) and the verdict isn't a clear
    # positive, re-fetch the detail tile from Mapbox (a different satellite capture,
    # often more top-down) and re-run YOLO + verify_address once. Adopt if it resolves.
    render_src, render_wide = original_local, wide_local
    retry_detail = retry_marked = retry_closeup = None
    imagery_retried = False
    if (IMAGERY_RETRY_ENABLED and consensus_dict.get('image_unusable')
            and consensus_dict.get('verdict') not in _POSITIVE_VERDICTS):
        log.info(f"Row {i+1}/{total}: VLM flagged image unusable; retrying with {alt_provider} imagery")
        retry_detail = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_retry.jpg")
        if get_satellite_image(centroid_lat, centroid_lon, retry_detail, zoom=MAPBOX_ZOOM, provider=alt_provider):
            retry_dets = _detect_on_tile(retry_detail, MAPBOX_ZOOM, footprint,
                                         centroid_lat, centroid_lon, keep_outside=keep_outside)
            retry_marked = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_retry_marked.jpg")
            render_marked_tile(retry_detail, retry_marked, footprint,
                               centroid_lat, centroid_lon, retry_dets, zoom=MAPBOX_ZOOM)
            retry_ctx = {**base_ctx, "tile_zoom": MAPBOX_ZOOM, "context_zoom": None}
            rcu_lat, rcu_lon = _closeup_center(retry_dets, centroid_lat, centroid_lon)
            retry_closeup = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_retry_closeup.jpg")
            if not get_satellite_image(rcu_lat, rcu_lon, retry_closeup, zoom=CLOSEUP_ZOOM, provider=alt_provider):
                retry_closeup = None
            retry_result = verify_address(
                retry_marked, retry_ctx, n_boxes=len(retry_dets),
                context_image_path=None, closeup_image_path=retry_closeup)
            if not retry_result.get('image_unusable'):
                log.info(f"Row {i+1}/{total}: Mapbox retry usable (verdict={retry_result.get('verdict')})")
                consensus_dict = retry_result
                detection_count = len(retry_dets)
                rooftop_path = (detection_count == 0)
                enriched = [{**d, 'vlm_result': consensus_dict} for d in retry_dets]
                render_src, render_wide = retry_detail, wide_local  # keep wide tile for the report
                imagery_retried = True

    # Frame-inadequate zoom-out retry (Gemini-only signal): if Gemini judged the detail
    # frame too tight to rule out a GROUND-MOUNTED cooling tower sitting just outside it,
    # and we didn't already do the provider-swap retry, re-pull a WIDER tile so the adjacent
    # ground / pads / yards are in frame and re-run once. Skipped on a positive verdict — we
    # already have what we need. Adopt the wider-view verdict (resolves, or lands needs_review).
    wide_retry = wr_marked = None
    frame_retried = False
    if (IMAGERY_RETRY_ENABLED and not imagery_retried
            and consensus_dict.get('frame_inadequate')
            and consensus_dict.get('verdict') not in _POSITIVE_VERDICTS):
        log.info(f"Row {i+1}/{total}: Gemini flagged frame too tight for a ground-CT call; "
                 f"retrying at wider zoom {MAPBOX_ZOOM_WIDE}")
        wide_retry = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_widefit.jpg")
        if get_satellite_image(centroid_lat, centroid_lon, wide_retry, zoom=MAPBOX_ZOOM_WIDE, provider=img_provider):
            wr_dets = _detect_on_tile(wide_retry, MAPBOX_ZOOM_WIDE, footprint,
                                      centroid_lat, centroid_lon, keep_outside=keep_outside)
            wr_marked = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_widefit_marked.jpg")
            render_marked_tile(wide_retry, wr_marked, footprint,
                               centroid_lat, centroid_lon, wr_dets, zoom=MAPBOX_ZOOM_WIDE)
            # Hand the original tight z19 tile back in as cross-zoom context for fine detail.
            wr_ctx = {**base_ctx, "tile_zoom": MAPBOX_ZOOM_WIDE, "context_zoom": MAPBOX_ZOOM}
            wr_result = verify_address(
                wr_marked, wr_ctx, n_boxes=len(wr_dets),
                context_image_path=original_local, closeup_image_path=None)
            log.info(f"Row {i+1}/{total}: wider-view retry verdict={wr_result.get('verdict')}")
            consensus_dict = wr_result
            detection_count = len(wr_dets)
            rooftop_path = (detection_count == 0)
            enriched = [{**d, 'vlm_result': consensus_dict} for d in wr_dets]
            render_src, render_wide = wide_retry, wide_local  # keep wide tile for the report
            frame_retried = True

    # Render BOTH tiles so manual review always has a clear close-up plus context.
    def _render_tile(tile_path, tile_zoom, out_path):
        if not tile_path:
            return None
        dets = [e for e in enriched if e.get('tile_path') == tile_path]
        win = winner if (winner and winner.get('tile_path') == tile_path) else None
        render_annotated_image(
            raw_image_path=tile_path,
            output_path=out_path,
            footprint=footprint,
            centroid_lat=centroid_lat,
            centroid_lon=centroid_lon,
            enriched_detections=dets,
            winner=win,
            zoom=tile_zoom,
        )
        blob = f"results/{job_id}/{os.path.basename(out_path)}"
        upload_file(out_path, blob)
        return make_signed_url(blob)

    result_url = _render_tile(render_src, MAPBOX_ZOOM, annotated_local)
    result_url_wide = _render_tile(render_wide, MAPBOX_ZOOM_WIDE, annotated_wide_local)

    # Ground-level Street View photo for faster human review (separate from the
    # detect/verify pipeline above -- failure here never affects the verdict).
    # Heading is auto-aimed by Google from the centroid we already computed for
    # the satellite tiles, so this costs one extra (cheap, free-if-no-coverage) call.
    result_url_streetview = None
    streetview_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_streetview.jpg")
    if get_streetview_image_google(centroid_lat, centroid_lon, streetview_local):
        sv_blob = f"results/{job_id}/{os.path.basename(streetview_local)}"
        upload_file(streetview_local, sv_blob)
        result_url_streetview = make_signed_url(sv_blob)

    # Construction → needs_review: active construction means the Mapbox tile may
    # predate the current building state, so the cooling-tower call isn't reliable.
    construction = consensus_dict.get('construction', False)
    effective_verdict = consensus_dict['verdict']
    construction_review = construction and effective_verdict != 'needs_review'
    if construction_review:
        log.info(
            f"Row {i+1}/{total}: construction flagged (VLM verdict "
            f"'{effective_verdict}') → routing to needs_review for manual lookup"
        )
        effective_verdict = 'needs_review'

    row_state = {
        'verdict': effective_verdict,
        'detection_count': detection_count,
        'rooftop_path': rooftop_path,
        'construction': construction,
        'winner_is_boundary': winner_is_boundary,
        'construction_review': construction_review,
    }
    notes = _build_notes(row_state)
    if geocoder_retried:
        notes = (f"[GEOCODER FALLBACK: Google geocode was missing/ambiguous; re-geocoded with "
                 f"Mapbox, which landed inside a building] {notes}").strip()
    if imagery_retried:
        # Surface model-triggered fallbacks for the reviewer: the VLM flagged the primary
        # tile (Mapbox in dense cores, else Google) as unusable, so this address was
        # re-analyzed on the alternate provider's imagery.
        _prim = "Mapbox" if dense else "Google"
        notes = (f"[IMAGERY RETRY: VLM flagged the {_prim} tile unusable (e.g. tall building "
                 f"shown at an oblique angle); re-analyzed on {alt_provider.capitalize()} imagery] {notes}").strip()
    if frame_retried:
        # Tell the reviewer why the analysis looks at a wider tile than usual.
        notes = (f"[WIDER-VIEW RETRY: the close-up was too zoomed in to rule out a ground-mounted "
                 f"cooling tower next to the building, so it was re-checked on a wider view that "
                 f"includes the surrounding ground] {notes}").strip()
    if addr_val_note:
        notes = f"{addr_val_note} {notes}".strip()

    web_entry = _build_web_entry(
        full_address=full_address,
        verdict=effective_verdict,
        consensus_dict=consensus_dict,
        detection_count=detection_count,
        construction=construction,
        notes=notes,
        original_url=original_url,
        result_url=result_url,
        result_url_wide=result_url_wide,
        result_url_streetview=result_url_streetview,
    )
    csv_row = _build_csv_row(
        full_address=full_address,
        verdict=effective_verdict,
        consensus_dict=consensus_dict,
        detection_count=detection_count,
        construction=construction,
        notes=notes,
        original_url=original_url,
        result_url=result_url,
    )

    for p in (original_local, wide_local, annotated_local, annotated_wide_local,
              marked_detail, marked_wide, retry_detail, retry_marked,
              closeup_local, retry_closeup, wide_retry, wr_marked, streetview_local):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception as e:
            log.warning(f"Could not clean up temp file {p}: {e}")

    return web_entry, csv_row


ADDRESS_VARIANTS = [
    'Address', 'address', 'ADDRESS',
    'Property Address', 'property address', 'PROPERTY ADDRESS',
    'PropertyAddress', 'propertyaddress', 'PROPERTYADDRESS',
    'Street Address', 'street address', 'STREET ADDRESS',
    'StreetAddress', 'streetaddress', 'STREETADDRESS',
    'Building Address', 'building address', 'BUILDING ADDRESS',
    'BuildingAddress', 'buildingaddress', 'BUILDINGADDRESS',
    'Property_Address', 'property_address', 'PROPERTY_ADDRESS',
    'Street_Address', 'street_address', 'STREET_ADDRESS',
    'Building_Address', 'building_address', 'BUILDING_ADDRESS'
]


def _pick_excel_tab(all_sheets):
    """Choose the worksheet to analyze from a multi-tab workbook. Real sales
    workbooks lead with Read Me / notes tabs (e.g. the Washington Gas campaign
    file: Read Me, then four different tabs holding addresses), so take the
    LEFTMOST non-empty tab with a recognizable address column; fall back to the
    first non-empty tab. Returns (tab_name, df) or (None, None)."""
    variants = {v.lower() for v in ADDRESS_VARIANTS}
    fallback = (None, None)
    for name, d in all_sheets.items():
        if d is None or not len(d.columns) or not len(d):
            continue
        if fallback == (None, None):
            fallback = (name, d)
        if {str(c).strip().lower() for c in d.columns} & variants:
            return name, d
    return fallback


def process_address_list(
    uploaded_filepath: str,
    job_id: str,
    progress_cb: Callable[[int, int, Optional[str]], None],
    should_cancel: Callable[[], bool],
    upload_file: Callable[[str, str], str],       # (local_path, dest_blob) -> blob_path
    make_signed_url: Callable[[str], str],        # (blob_path) -> url
    write_partial_result: Callable[[Dict[str, Any]], None] = None,  # Optional callback for streaming results
    concurrency: int = None,  # addresses processed at once; None → VLM_ADDRESS_CONCURRENCY env (default 5)
    resume_results: Dict[int, Any] = None,
) -> Dict[str, Any]:
    """
    Process a CSV of addresses through the geometry + dual-VLM pipeline.

    This is the local storage version - works with filesystem instead of GCS.
    """
    # Verify API keys are configured
    import os
    if not os.getenv('MAPBOX_API_KEY'):
        error_msg = "MAPBOX_API_KEY environment variable is not set. Cannot process addresses."
        log.error(error_msg)
        return {"error": error_msg}

    web_results, csv_rows = [], []

    # Detect file type and load accordingly
    file_ext = os.path.splitext(uploaded_filepath)[1].lower()
    log.info(f"Reading file: {uploaded_filepath} (type: {file_ext})")
    df = None

    # Handle Excel files (.xlsx, .xls)
    chosen_tab = None
    if file_ext in ['.xlsx', '.xls']:
        try:
            all_sheets = pd.read_excel(uploaded_filepath, sheet_name=None,
                                       engine='openpyxl' if file_ext == '.xlsx' else None)
            tab, df = _pick_excel_tab(all_sheets)
            if df is None:
                error_msg = "Excel file is empty or has no valid data"
                log.error(error_msg)
                return {"error": error_msg}
            if len(all_sheets) > 1:
                chosen_tab = tab
                others = [n for n in all_sheets if n != tab]
                log.info(f"Workbook has {len(all_sheets)} tabs; analyzing '{tab}' "
                         f"({len(df)} rows). Skipped: {others}")
            else:
                log.info(f"Excel file loaded successfully: {len(df)} rows, {len(df.columns)} columns")
        except Exception as e:
            error_msg = f"Failed to read Excel file: {str(e)}"
            log.error(error_msg)
            return {"error": error_msg}

    # Handle CSV files with comprehensive fallback handling
    else:
        # Try multiple encoding and delimiter combinations
        encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']
        delimiters = [',', ';', '\t']

        saw_multiindex = False  # delimiter split a single-column row into extra fields

        for encoding in encodings:
            if df is not None:
                break

            for delimiter in delimiters:
                try:
                    # Try to read with this combination
                    test_df = pd.read_csv(
                        uploaded_filepath,
                        encoding=encoding,
                        sep=delimiter,
                        skipinitialspace=True,   # Remove leading whitespace
                        skip_blank_lines=True,   # Skip empty rows
                        on_bad_lines='warn',     # Warn but don't fail on malformed rows
                        engine='python'          # More flexible parser for edge cases
                    )

                    # A MultiIndex means this delimiter found MORE fields than there are
                    # header columns (e.g. unquoted commas in an Address field split it
                    # into extra columns that pandas folds into the row index). The read
                    # "succeeds" but the data is mis-aligned — only the trailing field
                    # lands in the real column. Reject so we keep trying other delimiters
                    # instead of locking in garbage; if nothing else parses, surface a
                    # clear error below rather than crashing downstream on a tuple label.
                    if isinstance(test_df.index, pd.MultiIndex):
                        saw_multiindex = True
                        log.warning(
                            f"CSV read with encoding='{encoding}', delimiter='{repr(delimiter)}' "
                            f"produced a MultiIndex (delimiter mismatch / unquoted commas); rejecting"
                        )
                        continue

                    # Validate: must have at least 1 column and 1 row
                    if len(test_df.columns) >= 1 and len(test_df) > 0:
                        df = test_df
                        log.info(f"CSV loaded successfully with encoding='{encoding}', delimiter='{repr(delimiter)}'")
                        break

                except Exception as e:
                    # Continue trying other combinations
                    continue

        # If all attempts failed
        if df is None:
            if saw_multiindex:
                error_msg = (
                    "Address field contains unquoted commas; please double-quote "
                    "multi-part addresses (e.g. \"140 West End Ave, New York, NY\")."
                )
            else:
                error_msg = f"Failed to read CSV file. Tried encodings: {encodings}, delimiters: [comma, semicolon, tab]. Please ensure the file is a valid CSV."
            log.error(error_msg)
            return {"error": error_msg}

    # Strip whitespace from column names
    df.columns = df.columns.str.strip()

    # Remove completely empty rows
    df = df.dropna(how='all')

    total = len(df)
    log.info(f"CSV loaded successfully: {total} rows, {len(df.columns)} columns")
    log.info(f"CSV columns: {list(df.columns)}")

    # Auto-detect address column (support common variations)
    address_col = None
    address_variants = ADDRESS_VARIANTS

    for variant in address_variants:
        if variant in df.columns:
            address_col = variant
            break

    if not address_col:
        error_msg = f"CSV must contain an address column. Supported column names: 'Address', 'Property Address', 'Street Address', 'Building Address' (case-insensitive). Found columns: {list(df.columns)}"
        log.error(error_msg)
        return {"error": error_msg}

    log.info(f"Using address column: '{address_col}'")

    # Rename to standard 'Address' for consistent processing
    if address_col != 'Address':
        df = df.rename(columns={address_col: 'Address'})

    # Check if DataFrame has any valid rows
    if total == 0:
        error_msg = "CSV file is empty (no rows to process)"
        log.error(error_msg)
        return {"error": error_msg}

    # ------------------------------------------------------------------
    # Concurrent orchestrator: run up to `concurrency` addresses at once to
    # overlap the dual-VLM wait. Per-address work is isolated in
    # _process_one_address; shared state (progress, partial-result streaming,
    # ordering) is handled here under one lock. Transient failures (throttled
    # Overpass → TransientFootprintError, transient VLM verdict) are re-queued
    # and retried in later rounds; nothing is silently dropped.
    # ------------------------------------------------------------------
    if concurrency is None:
        concurrency = int(os.environ.get("VLM_ADDRESS_CONCURRENCY", str(DEFAULT_VLM_ADDRESS_CONCURRENCY)))
    concurrency = max(1, concurrency)
    log.info(f"Starting processing loop for {total} addresses ({concurrency}-way concurrent)")

    columns = df.columns
    results_by_index = {}   # i -> (web_entry, csv_row)
    rows_by_index = {int(i): row for i, row in df.iterrows()}
    state_by_index = {
        index: {
            "index": index,
            "address": _compose_address(row, columns),
            "state": "queued",
            "message": "Waiting to be analyzed",
            "error": "",
        }
        for index, row in rows_by_index.items()
    }
    for raw_index, stored in (resume_results or {}).items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if isinstance(stored, dict):
            web_entry = stored.get("web_entry")
            csv_row = stored.get("csv_row")
        elif isinstance(stored, (list, tuple)) and len(stored) == 2:
            web_entry, csv_row = stored
        else:
            continue
        if isinstance(web_entry, dict) and isinstance(csv_row, dict):
            results_by_index[index] = (web_entry, csv_row)
            error = str(web_entry.get("error") or "")
            state_by_index[index] = {
                "index": index,
                "address": (
                    web_entry.get("address")
                    or state_by_index.get(index, {}).get("address")
                    or ""
                ),
                "state": "attention" if error else "complete",
                "message": error or "Machine analysis finished",
                "error": error,
            }
    last_seen = {}          # i -> last (web_entry, csv_row) for transient-VLM rows
    commit_lock = threading.RLock()
    done = len(results_by_index)

    def _partial_payload_locked():
        return {
            "schema_version": 2,
            "web_results": [
                results_by_index[k][0] for k in sorted(results_by_index)
            ],
            "row_results": [
                {
                    "index": int(k),
                    "web_entry": results_by_index[k][0],
                    "csv_row": results_by_index[k][1],
                }
                for k in sorted(results_by_index)
            ],
            "address_states": [
                state_by_index[k] for k in sorted(state_by_index)
            ],
        }

    def _publish_locked():
        if write_partial_result:
            write_partial_result(_partial_payload_locked())

    def _set_state(i, row, state, message="", error=""):
        index = int(i)
        with commit_lock:
            state_by_index[index] = {
                "index": index,
                "address": _compose_address(row, columns),
                "state": state,
                "message": str(message or ""),
                "error": str(error or ""),
            }
            _publish_locked()

    if done:
        progress_cb(done, total, f"Resumed {done} checkpointed address(es)")
    with commit_lock:
        _publish_locked()

    def _commit(i, web_entry, csv_row):
        nonlocal done
        with commit_lock:
            index = int(i)
            if index in results_by_index:
                return
            results_by_index[index] = (web_entry, csv_row)
            error = str(web_entry.get("error") or "")
            state_by_index[index] = {
                "index": index,
                "address": (
                    web_entry.get("address")
                    or state_by_index.get(index, {}).get("address")
                    or ""
                ),
                "state": "attention" if error else "complete",
                "message": error or "Machine analysis finished",
                "error": error,
            }
            done += 1
            progress_cb(done, total, None)
            _publish_locked()
            if done % 50 == 0 and total > 100:
                import gc
                gc.collect()
                log.info(f"Memory cleanup at {done}/{total} addresses")

    def _run_round(batch):
        """Process a batch concurrently; return the rows that need retry."""
        retry = []

        def _run_one(i, row):
            _set_state(i, row, "analyzing", "Analyzing this address now")
            return _process_one_address(
                row, i, columns, total, job_id, upload_file, make_signed_url,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {
                ex.submit(_run_one, i, row): (i, row)
                for (i, row) in batch
            }
            for fut in concurrent.futures.as_completed(futs):
                i, row = futs[fut]
                if should_cancel():
                    ex.shutdown(wait=False, cancel_futures=True)
                    raise Exception("Job cancelled by user.")
                try:
                    web_entry, csv_row = fut.result()
                except TransientFootprintError:
                    log.warning(f"Row {i+1}/{total}: Overpass throttled (transient); queued for retry")
                    _set_state(
                        i, row, "retrying",
                        "Building-footprint service was busy; retrying",
                    )
                    retry.append((i, row))
                    continue
                except Exception as e:
                    log.warning(f"Row {i+1}/{total}: unexpected error ({type(e).__name__}: {e}); queued for retry")
                    detail = f"{type(e).__name__}: {e}"
                    _set_state(i, row, "retrying", detail, detail)
                    retry.append((i, row))
                    continue
                if _is_transient_vlm(web_entry):
                    last_seen[i] = (web_entry, csv_row)  # keep as fallback if retries don't clear it
                    log.info(f"Row {i+1}/{total}: transient VLM verdict; queued for retry")
                    _set_state(
                        i, row, "retrying",
                        "Visual analysis was inconclusive; retrying",
                    )
                    retry.append((i, row))
                    continue
                _commit(i, web_entry, csv_row)
        return retry

    pending = [
        (i, row) for i, row in df.iterrows()
        if int(i) not in results_by_index
    ]
    for round_num in range(MAX_RETRY_ROUNDS + 1):
        if not pending:
            break
        if round_num > 0:
            log.info(f"Retry round {round_num}/{MAX_RETRY_ROUNDS}: {len(pending)} address(es) after backoff")
            time.sleep(RETRY_BACKOFF_SECONDS * round_num)
        pending = _run_round(pending)

    # No silent drops. If a transient VLM result was seen, keep it (a legitimate
    # needs_review row); if the footprint never resolved, emit footprint_missing
    # tagged unverified so a throttle-induced miss is never logged as a clean
    # genuine negative.
    for i, row in pending:
        if i in last_seen:
            _commit(i, *last_seen[i])
            continue
        full_address = _compose_address(row, columns)
        log.warning(f"Row {i+1}/{total}: still failing after {MAX_RETRY_ROUNDS} retries; emitting unverified")
        notes = "Transient failure (Overpass/VLM) unresolved after retries; manual verification needed"
        _commit(
            i,
            _build_web_entry(
                full_address=full_address, verdict='footprint_missing',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
                error="Unresolved transient failure",
            ),
            _build_csv_row(
                full_address=full_address, verdict='footprint_missing',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ),
        )

    # Assemble the final accumulators in input order.
    for k in sorted(results_by_index):
        web_results.append(results_by_index[k][0])
        csv_rows.append(results_by_index[k][1])

    # Log summary statistics
    successful = sum(1 for r in web_results if not r.get('error'))
    failed = sum(1 for r in web_results if r.get('error'))
    detections = sum(1 for r in web_results if r.get('verdict') in _POSITIVE_VERDICTS)

    log.info(f"Processing complete: {total} total addresses")
    log.info(f"  ✓ Successful: {successful}")
    log.info(f"  ✗ Failed: {failed}")
    log.info(f"  📡 Cooling towers detected: {detections}")

    # Generate HTML report with embedded images (skip for large batches to save memory)
    skip_html = total > 200
    html_local = os.path.join(tempfile.gettempdir(), f"Report_{job_id}.html")
    html_url = None

    if skip_html:
        log.info(f"Skipping HTML generation for large batch ({total} addresses) to conserve memory")
    else:
        # We need a function to get local paths from blob paths for image encoding
        # This lambda will be passed to the HTML generator
        def blob_to_local(blob_path):
            # For local storage, blob paths are relative to storage base
            # The make_signed_url returns /files/<blob_path>
            # We need to reconstruct the actual local path
            from storage_helpers import get_file_path
            return get_file_path(blob_path)

        try:
            # Default report format: report_audit audit cards (clickable lightbox +
            # Google Maps/Earth/Bing location links), dark theme. Annotated tiles are
            # embedded as base64 data URIs so the downloaded HTML works offline.
            import base64
            import copy as _copy
            from report_audit import build_audit_report
            report_results = _copy.deepcopy(web_results)
            for _e in report_results:
                for _k in ("result_image_url", "result_image_url_wide", "original_image_url"):
                    _u = _e.get(_k)
                    if not isinstance(_u, str) or not _u:
                        continue
                    _bp = _u[len("/files/"):] if _u.startswith("/files/") else _u
                    try:
                        _b = blob_to_local(_bp).read_bytes()
                        _e[_k] = "data:image/jpeg;base64," + base64.b64encode(_b).decode("ascii")
                    except (OSError, AttributeError):
                        pass
            _report_html = build_audit_report(
                report_results,
                title=f"Cooling Tower Analysis — {job_id}",
                email_mode=False,
                dark=True,
            )
            with open(html_local, "w", encoding="utf-8") as _f:
                _f.write(_report_html)

            # Upload HTML report
            html_blob = f"results/{job_id}/Report_{job_id}.html"
            upload_file(html_local, html_blob)
            html_url = make_signed_url(html_blob)
        except Exception as e:
            log.warning(f"Failed to generate HTML report: {e}")
            html_url = None

    return {
        "web_results": web_results,
        "html_url": html_url,
        # The parsed upload (post dropna, address column normalized), 1:1 and
        # in order with web_results — the caller finalizes it into the review
        # batch + Google Sheet. Not JSON-serializable; pop before write_result.
        "table_df": df,
        # Set when a multi-tab workbook was uploaded: which tab was analyzed.
        "sheet_tab": chosen_tab,
    }
