"""Annotated-image rendering for the address-analysis pipeline.

Single public function: render_annotated_image. Branches internally on
(footprint_missing / verify_rooftop / verify_detection) per Fork 1 color
rules from the design plan.
"""

from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from shapely.geometry import MultiPolygon

import geometry


# Color scheme (BGR for cv2)
_COLOR_POLYGON = (0, 0, 255)             # red
_COLOR_WINNER = (0, 200, 0)              # green (thick)
_COLOR_POSITIVE_NONWINNER = (0, 200, 0)  # green (thin)
_COLOR_NEGATIVE = (128, 128, 128)        # gray
_COLOR_AMBIGUOUS = (0, 165, 255)         # orange
_COLOR_BOUNDARY = (0, 255, 255)          # yellow

_THICKNESS_WINNER = 4
_THICKNESS_NONWINNER = 2
_POLYGON_THICKNESS = 2


# Verdict classes — must stay in sync with tasks_local.py / vlm.py.
_POSITIVE_VERDICTS = frozenset({
    "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
})
_NEGATIVE_VERDICTS = frozenset({
    "not_detected", "neighbor_only", "no_cooling_tower",
})


def render_annotated_image(
    raw_image_path: str,
    output_path: str,
    footprint: Optional[Dict[str, Any]],
    centroid_lat: Optional[float],
    centroid_lon: Optional[float],
    enriched_detections: List[Dict[str, Any]],
    winner: Optional[Dict[str, Any]],
    zoom: int = 19,
    img_width: int = 768,
    img_height: int = 768,
) -> None:
    """Render an annotated JPG to output_path per Fork 1 color rules.

    Three branches:
      1. footprint_missing (footprint is None): raw tile written as-is.
      2. verify_rooftop (footprint set, enriched_detections empty):
         tile + red footprint polygon, no detection boxes.
      3. verify_detection (footprint set, enriched_detections non-empty):
         tile + red polygon + color-coded detection boxes per Fork 1.

    On any cv2.imread failure (corrupted/unreadable raw tile), copies the
    raw bytes through to output_path so the consumer still has a file.
    """
    img = cv2.imread(raw_image_path)
    if img is None:
        with open(raw_image_path, 'rb') as src, open(output_path, 'wb') as dst:
            dst.write(src.read())
        return

    if footprint is None:
        cv2.imwrite(output_path, img)
        return

    _draw_footprint(
        img, footprint['polygon'],
        centroid_lat, centroid_lon,
        zoom, img_width, img_height,
    )

    if not enriched_detections:
        cv2.imwrite(output_path, img)
        return

    _draw_detections_color_coded(img, enriched_detections, winner)
    cv2.imwrite(output_path, img)


def _draw_footprint(
    img: np.ndarray,
    polygon: Any,
    center_lat: float,
    center_lon: float,
    zoom: int,
    img_width: int,
    img_height: int,
) -> None:
    """Draw the OSM footprint outline in red. Handles Polygon and MultiPolygon."""
    polys = list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
    for poly in polys:
        pixel_coords = []
        for lon_v, lat_v in poly.exterior.coords:
            px, py = geometry.latlon_to_pixel(
                lat_v, lon_v, center_lat, center_lon,
                zoom, img_width, img_height,
            )
            pixel_coords.append((int(round(px)), int(round(py))))
        if len(pixel_coords) < 2:
            continue
        pts = np.array(pixel_coords, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(
            img, [pts], isClosed=True,
            color=_COLOR_POLYGON, thickness=_POLYGON_THICKNESS,
        )


def _draw_detections_color_coded(
    img: np.ndarray,
    enriched_detections: List[Dict[str, Any]],
    winner: Optional[Dict[str, Any]],
) -> None:
    """Draw color-coded bboxes per Fork 1 rules.

    Precedence for each detection:
      1. winner             → GREEN THICK (always, overrides verdict/location)
      2. boundary location  → YELLOW (geometric ambiguity overrides verdict)
      3. POSITIVE verdict   → GREEN THIN
      4. NEGATIVE verdict   → GRAY THIN
      5. AMBIGUOUS verdict  → ORANGE THIN
    """
    for det in enriched_detections:
        x1, y1, x2, y2 = (int(round(v)) for v in det['bbox'])
        is_winner = (winner is not None and det is winner)
        location = det.get('location', '')
        verdict = det['vlm_result'].get('verdict', '')

        if is_winner:
            color, thickness = _COLOR_WINNER, _THICKNESS_WINNER
        elif location == 'boundary':
            color, thickness = _COLOR_BOUNDARY, _THICKNESS_NONWINNER
        elif verdict in _POSITIVE_VERDICTS:
            color, thickness = _COLOR_POSITIVE_NONWINNER, _THICKNESS_NONWINNER
        elif verdict in _NEGATIVE_VERDICTS:
            color, thickness = _COLOR_NEGATIVE, _THICKNESS_NONWINNER
        else:
            color, thickness = _COLOR_AMBIGUOUS, _THICKNESS_NONWINNER

        cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
