"""
Local storage version of task processor (replaces tasks_serverless.py).
Processes address lists using local filesystem instead of GCS.
"""
import os
import re
import math
import tempfile
import logging
import pandas as pd
from typing import Callable, Dict, Any, List, Optional, Tuple
from shapely.geometry import MultiPolygon
from shapely.ops import transform

from utils import (
    geocode_address_mapbox, get_satellite_image_mapbox, is_fully_qualified_address,
    YOLO_CONF, _get_models, ct_class_indices, MAPBOX_ZOOM, MAPBOX_ZOOM_WIDE,
)
from geometry import (
    get_building_footprint, classify_detections, filter_detections,
    extract_detections_from_yolo, ensemble_dedupe_detections, geo_dedupe_detections,
)
from nyc_opendata import lookup_nyc_registry
from vlm import verify_detection, verify_rooftop
from pipeline_render import render_annotated_image
from html_report import generate_html_report

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


def _detect_on_tile(
    tile_path: str,
    tile_zoom: int,
    footprint: Dict[str, Any],
    centroid_lat: float,
    centroid_lon: float,
) -> List[Dict[str, Any]]:
    """Run the YOLO ensemble on one tile, classify against the footprint, and
    return kept detections tagged with tile provenance.

    Both models run on the tile; their outputs are deduped within the tile by
    pixel IoU (valid at a single zoom). Cross-tile dedupe across zoom levels is
    done later in geo space via geo_dedupe_detections. filter_detections honors
    YOLO_KEEP_OUTSIDE so off-building (ground) candidates pass through to the VLM.
    """
    ensemble_dets: List[Dict[str, Any]] = []
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
    kept, _rejected = filter_detections(classified)
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
        return "No OSM footprint found, manual verification needed"
    if verdict == 'likely_residential':
        return (f"Footprint below commercial size floor ({MIN_COMMERCIAL_FOOTPRINT_SQM} sq m) "
                "- likely residential; manual verification recommended")
    if verdict == 'ambiguous_footprint':
        return ("Geocoded point falls outside any building footprint "
                "(nearest-match area unreliable); manual verification recommended")
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
) -> Dict[str, Any]:
    """Build a web_results entry. Keeps backward-compatible keys for html_report.py
    (address, confidence_score, result_image_url, original_image_url, error) plus
    the new pipeline fields surfaced for downstream consumers. result_url_wide is
    the annotated wide-context tile (None for non-VLM rows).
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
    else:
        confidence_score = None
        reasoning = ''
        agreement = False
        gemini_verdict = ''
        gemini_confidence = None
        grok_verdict = ''
        grok_confidence = None

    entry = {
        "address": full_address,
        "confidence_score": confidence_score,
        "result_image_url": result_url,
        "result_image_url_wide": result_url_wide,
        "original_image_url": original_url,
        "verdict": verdict,
        "detection_count": detection_count,
        "construction": construction,
        "reasoning": reasoning,
        "agreement": agreement,
        "gemini_verdict": gemini_verdict,
        "gemini_confidence": gemini_confidence,
        "grok_verdict": grok_verdict,
        "grok_confidence": grok_confidence,
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


def process_address_list(
    uploaded_filepath: str,
    job_id: str,
    progress_cb: Callable[[int, int, Optional[str]], None],
    should_cancel: Callable[[], bool],
    upload_file: Callable[[str, str], str],       # (local_path, dest_blob) -> blob_path
    make_signed_url: Callable[[str], str],        # (blob_path) -> url
    write_partial_result: Callable[[Dict[str, Any]], None] = None,  # Optional callback for streaming results
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
    if file_ext in ['.xlsx', '.xls']:
        try:
            df = pd.read_excel(uploaded_filepath, engine='openpyxl' if file_ext == '.xlsx' else None)

            # Validate: must have at least 1 column and 1 row
            if len(df.columns) >= 1 and len(df) > 0:
                log.info(f"Excel file loaded successfully: {len(df)} rows, {len(df.columns)} columns")
            else:
                df = None
                error_msg = "Excel file is empty or has no valid data"
                log.error(error_msg)
                return {"error": error_msg}
        except Exception as e:
            error_msg = f"Failed to read Excel file: {str(e)}"
            log.error(error_msg)
            return {"error": error_msg}

    # Handle CSV files with comprehensive fallback handling
    else:
        # Try multiple encoding and delimiter combinations
        encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']
        delimiters = [',', ';', '\t']

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
    address_variants = [
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

    log.info(f"Starting processing loop for {total} addresses")
    done = 0
    for i, row in df.iterrows():
        if should_cancel():
            raise Exception("Job cancelled by user.")

        done += 1
        progress_cb(done, total, None)

        # Empty-address guard
        if pd.isna(row['Address']) or str(row['Address']).strip() == '':
            log.warning(f"Row {i}: Empty address, skipping")
            web_results.append(_build_web_entry(
                full_address="(Empty)", verdict="", consensus_dict=None,
                detection_count=0, construction=False,
                notes="Empty address in CSV",
                original_url=None, result_url=None,
                error="Empty Address",
            ))
            csv_rows.append(_build_csv_row(
                full_address="(Empty)", verdict="", consensus_dict=None,
                detection_count=0, construction=False,
                notes="Empty address in CSV",
                original_url=None, result_url=None,
            ))
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        # Build address string (no NY append — Step 4 stripped it)
        parts = [str(row['Address']).strip()]
        if 'Boro_Area' in df.columns and pd.notna(row.get('Boro_Area')):
            parts.append(str(row['Boro_Area']).strip())
        if 'Zip' in df.columns and pd.notna(row.get('Zip')):
            z = row['Zip']
            parts.append(str(int(z)) if isinstance(z, float) else str(z))
        full_address = ", ".join(parts)

        # Geocode
        log.info(f"Row {i+1}/{total}: Geocoding '{full_address}'")
        coords = geocode_address_mapbox(full_address)
        if not coords:
            log.warning(f"Row {i+1}/{total}: Geocoding failed for '{full_address}'")
            notes = _build_notes({'geocode_failed': True})
            web_results.append(_build_web_entry(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
                error="Geocoding Failed",
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
            ))
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue
        geo_lat, geo_lon = coords

        # Footprint lookup BEFORE any Mapbox tile fetch (one Mapbox call per address)
        log.info(f"Row {i+1}/{total}: Looking up OSM building footprint")
        footprint = get_building_footprint(geo_lat, geo_lon)

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
            ok = get_satellite_image_mapbox(geo_lat, geo_lon, original_local)
            if not ok:
                log.warning(f"Row {i+1}/{total}: Footprint missing AND imagery failed")
                notes = _build_notes({'imagery_failed': True})
                web_results.append(_build_web_entry(
                    full_address=full_address, verdict="", consensus_dict=None,
                    detection_count=0, construction=False, notes=notes,
                    original_url=None, result_url=None,
                    error="Image Download Failed",
                ))
                csv_rows.append(_build_csv_row(
                    full_address=full_address, verdict="", consensus_dict=None,
                    detection_count=0, construction=False, notes=notes,
                    original_url=None, result_url=None,
                ))
                if write_partial_result:
                    write_partial_result({"web_results": web_results})
                continue

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

            notes = _build_notes({'verdict': 'footprint_missing'})
            web_results.append(_build_web_entry(
                full_address=full_address,
                verdict='footprint_missing',
                consensus_dict=None,
                detection_count=0,
                construction=False,
                notes=notes,
                original_url=original_url,
                result_url=result_url,
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address,
                verdict='footprint_missing',
                consensus_dict=None,
                detection_count=0,
                construction=False,
                notes=notes,
                original_url=original_url,
                result_url=result_url,
            ))

            for p in (original_local, annotated_local):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    log.warning(f"Could not clean up temp file {p}: {e}")

            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        # ====================================================================
        # BRANCH B — footprint found: centroid-centered tile + YOLO + VLM
        # ====================================================================
        log.info(f"Row {i+1}/{total}: Footprint found (OSM ID {footprint.get('osm_id')})")
        centroid_lat, centroid_lon = _centroid_latlon(footprint)

        # ====================================================================
        # AREA GATE -- cheapest exit, BEFORE any Mapbox tile fetch / YOLO / VLM.
        # Mirrors the footprint_missing branch: emit a verdict, no rendered
        # tile, route to manual review.
        #   - contains_point False (nearest-building fallback): the area is
        #     untrustworthy -- a real tower building can match a tiny adjacent
        #     polygon (e.g. 530 East 76 -> 159 sq m wrong polygon) -- so do NOT
        #     gate on area; route to review as ambiguous_footprint.
        #   - contains_point True AND area < floor: gate as likely_residential.
        # OPEN: this sits before the registry-first check below; whether an
        # ambiguous_footprint should still get a registry shot is flagged for
        # review (a BIN match off a wrong footprint is itself unreliable).
        # ====================================================================
        if not footprint.get('contains_point'):
            log.info(f"Row {i+1}/{total}: Footprint is a nearest-building fallback "
                     f"(contains_point=False); routing to review as ambiguous_footprint")
            notes = _build_notes({'verdict': 'ambiguous_footprint'})
            web_results.append(_build_web_entry(
                full_address=full_address, verdict='ambiguous_footprint',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address, verdict='ambiguous_footprint',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ))
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        footprint_area = _footprint_area_m2(footprint)
        if footprint_area < MIN_COMMERCIAL_FOOTPRINT_SQM:
            log.info(f"Row {i+1}/{total}: Footprint {footprint_area:.0f} sq m < "
                     f"{MIN_COMMERCIAL_FOOTPRINT_SQM} sq m floor; gating as likely_residential")
            notes = _build_notes({'verdict': 'likely_residential'})
            web_results.append(_build_web_entry(
                full_address=full_address, verdict='likely_residential',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address, verdict='likely_residential',
                consensus_dict=None, detection_count=0, construction=False,
                notes=notes, original_url=None, result_url=None,
            ))
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        log.info(f"Row {i+1}/{total}: Downloading centroid-centered satellite image")
        ok = get_satellite_image_mapbox(centroid_lat, centroid_lon, original_local)
        if not ok:
            log.warning(f"Row {i+1}/{total}: Image download failed")
            notes = _build_notes({'imagery_failed': True})
            web_results.append(_build_web_entry(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
                error="Image Download Failed",
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address, verdict="", consensus_dict=None,
                detection_count=0, construction=False, notes=notes,
                original_url=None, result_url=None,
            ))
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        original_blob = f"uploads/{job_id}/{os.path.basename(original_local)}"
        upload_file(original_local, original_blob)
        original_url = make_signed_url(original_blob)

        # ====================================================================
        # Registry-first: BIN-matched NYC OpenData hit → confirm, skip YOLO+VLM
        # The skip has no VLM safety net, so require a fully-qualified (ZIP-bearing)
        # address — a misgeocode would otherwise confirm the wrong building.
        # Under-qualified addresses fall through to the full pipeline.
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

            notes = _build_notes({
                'verdict': 'registry_confirmed',
                'registry_citation': registry['citation'],
            })
            web_results.append(_build_web_entry(
                full_address=full_address,
                verdict='registry_confirmed',
                consensus_dict=None,
                detection_count=0,
                construction=False,
                notes=notes,
                original_url=original_url,
                result_url=result_url,
            ))
            csv_rows.append(_build_csv_row(
                full_address=full_address,
                verdict='registry_confirmed',
                consensus_dict=None,
                detection_count=0,
                construction=False,
                notes=notes,
                original_url=original_url,
                result_url=result_url,
            ))

            for p in (original_local, annotated_local):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as e:
                    log.warning(f"Could not clean up temp file {p}: {e}")

            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        # Fetch the wider tile (ground-mounted CTs / parcel context). original_local is
        # the detail tile (z19); wide_local is z17. Both centered on the same centroid.
        wide_local = os.path.join(
            tempfile.gettempdir(),
            f"{job_id}_{i}_{clean_addr}_wide.jpg",
        )
        log.info(f"Row {i+1}/{total}: Downloading wide (z{MAPBOX_ZOOM_WIDE}) satellite image")
        if not get_satellite_image_mapbox(centroid_lat, centroid_lon, wide_local, zoom=MAPBOX_ZOOM_WIDE):
            log.warning(f"Row {i+1}/{total}: Wide tile fetch failed; proceeding with detail tile only")
            wide_local = None

        log.info(f"Row {i+1}/{total}: Running YOLO ensemble at conf={YOLO_CONF} on both zooms")
        detail_kept = _detect_on_tile(original_local, MAPBOX_ZOOM, footprint, centroid_lat, centroid_lon)
        wide_kept = (
            _detect_on_tile(wide_local, MAPBOX_ZOOM_WIDE, footprint, centroid_lat, centroid_lon)
            if wide_local else []
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

        enriched: List[Dict[str, Any]] = []
        winner: Optional[Dict[str, Any]] = None
        if merged:
            log.info(
                f"Row {i+1}/{total}: {len(merged)} kept detection(s) across zooms; "
                f"running verify_detection per candidate"
            )
            for det in merged:
                det_tile = det['tile_path']
                det_zoom = det['tile_zoom']
                # Context = the opposite-zoom tile of the same target (if available).
                if det_tile == original_local and wide_local:
                    context_path, context_zoom = wide_local, MAPBOX_ZOOM_WIDE
                elif det_tile == wide_local:
                    context_path, context_zoom = original_local, MAPBOX_ZOOM
                else:
                    context_path, context_zoom = None, None
                det_ctx = {
                    **base_ctx,
                    "detection_location": det.get('location', 'unknown'),
                    "tile_zoom": det_zoom,
                    "context_zoom": context_zoom,
                }
                vlm_result = verify_detection(
                    det_tile, det['bbox'], det_ctx, context_image_path=context_path
                )
                enriched.append({**det, 'vlm_result': vlm_result})
            winner = _pick_winner(enriched)
            consensus_dict = winner['vlm_result']
            rooftop_path = False
            detection_count = len(merged)
            winner_is_boundary = (winner.get('location') == 'boundary')
            render_tile, render_zoom = winner['tile_path'], winner['tile_zoom']
        else:
            # Rooftop scan: lead with the wide tile (ground CTs are the concern),
            # passing the detail tile as cross-zoom context.
            log.info(f"Row {i+1}/{total}: No kept detections; running verify_rooftop")
            scan_path = wide_local or original_local
            scan_zoom = MAPBOX_ZOOM_WIDE if wide_local else MAPBOX_ZOOM
            context_path = original_local if wide_local else None
            rooftop_ctx = {
                **base_ctx,
                "tile_zoom": scan_zoom,
                "context_zoom": MAPBOX_ZOOM if wide_local else None,
            }
            consensus_dict = verify_rooftop(scan_path, rooftop_ctx, context_image_path=context_path)
            rooftop_path = True
            detection_count = 0
            winner_is_boundary = False
            render_tile, render_zoom = scan_path, scan_zoom

        # Render BOTH tiles so manual review always has a clear close-up (z19
        # detail, primary) plus the wider context (secondary). Each tile is
        # annotated only with its own detections — cross-zoom pixel coords don't
        # transfer — and the winner is highlighted on whichever tile it came from.
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

        result_url = _render_tile(original_local, MAPBOX_ZOOM, annotated_local)
        result_url_wide = _render_tile(wide_local, MAPBOX_ZOOM_WIDE, annotated_wide_local)

        # Construction → needs_review: active construction means the Mapbox tile may
        # predate the current building state, so the cooling-tower call isn't reliable.
        # Route to manual review (operator verifies against fresher sources). The
        # underlying per-VLM verdicts/reasoning are preserved in consensus_dict.
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

        web_results.append(_build_web_entry(
            full_address=full_address,
            verdict=effective_verdict,
            consensus_dict=consensus_dict,
            detection_count=detection_count,
            construction=construction,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
            result_url_wide=result_url_wide,
        ))
        csv_rows.append(_build_csv_row(
            full_address=full_address,
            verdict=effective_verdict,
            consensus_dict=consensus_dict,
            detection_count=detection_count,
            construction=construction,
            notes=notes,
            original_url=original_url,
            result_url=result_url,
        ))

        for p in (original_local, wide_local, annotated_local, annotated_wide_local):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                log.warning(f"Could not clean up temp file {p}: {e}")

        if done % 50 == 0 and total > 100:
            import gc
            gc.collect()
            log.info(f"Memory cleanup at {done}/{total} addresses")

        if write_partial_result:
            write_partial_result({"web_results": web_results})

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
            generate_html_report(
                web_results=web_results,
                job_id=job_id,
                output_path=html_local,
                get_local_path_func=blob_to_local
            )

            # Upload HTML report
            html_blob = f"results/{job_id}/Report_{job_id}.html"
            upload_file(html_local, html_blob)
            html_url = make_signed_url(html_blob)
        except Exception as e:
            log.warning(f"Failed to generate HTML report: {e}")
            html_url = None

    return {
        "web_results": web_results,
        "html_url": html_url
    }
