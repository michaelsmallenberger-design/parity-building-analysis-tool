"""
Building footprint lookup and geometric filtering.

Replaces center-crop, multi-scale, and distance-filter heuristics with
actual building polygon geometry. Uses OpenStreetMap Overpass API for
building footprints and Shapely for point-in-polygon filtering.

Pipeline position:
    Geocode → Satellite image → YOLO detection → **THIS MODULE** → VLM verification
"""

import os
import math
import time
import logging
from typing import Optional, Tuple, List, Dict, Any

import requests
from shapely.geometry import Point, Polygon, MultiPolygon, shape
from shapely.ops import nearest_points

log = logging.getLogger(__name__)

# Overpass API endpoint (public, no API key needed)
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OVERPASS_TIMEOUT = int(os.getenv("OVERPASS_TIMEOUT", "15"))

# Search radius in meters for building footprint lookup
FOOTPRINT_SEARCH_RADIUS = int(os.getenv("FOOTPRINT_SEARCH_RADIUS", "50"))

# Rate limiting for Overpass (be respectful to public API)
_last_overpass_call = 0.0
OVERPASS_RATE_LIMIT = 1.0  # seconds between requests


# -------------------------------------------------------------------------
# Web Mercator math: convert between pixel coordinates and lat/lon
# -------------------------------------------------------------------------

def _lat_to_y_mercator(lat: float) -> float:
    """Convert latitude to Mercator Y (in radians)."""
    lat_rad = math.radians(lat)
    return math.log(math.tan(math.pi / 4 + lat_rad / 2))


def _y_mercator_to_lat(y: float) -> float:
    """Convert Mercator Y back to latitude."""
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def pixel_to_latlon(
    px_x: float, px_y: float,
    center_lat: float, center_lon: float,
    zoom: int,
    img_width: int, img_height: int
) -> Tuple[float, float]:
    """
    Convert pixel coordinates in a Mapbox static image to lat/lon.

    Mapbox static images use Web Mercator projection (EPSG:3857).
    At a given zoom level, each pixel represents a fixed distance in
    Mercator space.

    Args:
        px_x, px_y: Pixel coordinates (0,0 = top-left)
        center_lat, center_lon: Center of the satellite image
        zoom: Mapbox zoom level
        img_width, img_height: Image dimensions in pixels

    Returns:
        (latitude, longitude) tuple
    """
    # Total pixels across the world at this zoom level
    # Mapbox uses 512px tiles (not 256)
    world_px = 512 * (2 ** zoom)

    # Convert center to world pixel coordinates
    center_world_x = (center_lon + 180) / 360 * world_px
    center_world_y = (1 - _lat_to_y_mercator(center_lat) / math.pi) / 2 * world_px

    # Calculate world pixel for the target point
    # Offset from image center
    offset_x = px_x - img_width / 2
    offset_y = px_y - img_height / 2

    target_world_x = center_world_x + offset_x
    target_world_y = center_world_y + offset_y

    # Convert back to lat/lon
    lon = target_world_x / world_px * 360 - 180
    merc_y = math.pi * (1 - 2 * target_world_y / world_px)
    lat = _y_mercator_to_lat(merc_y)

    return (lat, lon)


def latlon_to_pixel(
    lat: float, lon: float,
    center_lat: float, center_lon: float,
    zoom: int,
    img_width: int, img_height: int
) -> Tuple[float, float]:
    """
    Convert lat/lon to pixel coordinates in a Mapbox static image.
    Inverse of pixel_to_latlon.

    Returns:
        (px_x, px_y) tuple — pixel coordinates (0,0 = top-left)
    """
    world_px = 512 * (2 ** zoom)

    # Center in world pixels
    center_world_x = (center_lon + 180) / 360 * world_px
    center_world_y = (1 - _lat_to_y_mercator(center_lat) / math.pi) / 2 * world_px

    # Target in world pixels
    target_world_x = (lon + 180) / 360 * world_px
    target_world_y = (1 - _lat_to_y_mercator(lat) / math.pi) / 2 * world_px

    # Offset from center = pixel position
    px_x = (target_world_x - center_world_x) + img_width / 2
    px_y = (target_world_y - center_world_y) + img_height / 2

    return (px_x, px_y)


# -------------------------------------------------------------------------
# Building footprint lookup via Overpass API
# -------------------------------------------------------------------------

def get_building_footprint(
    lat: float, lon: float,
    search_radius: int = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch the building footprint polygon at/near the given coordinates.

    Uses OpenStreetMap Overpass API to find building polygons.
    Returns the building whose polygon contains the point, or the
    nearest building if none contains it.

    Args:
        lat: Latitude
        lon: Longitude
        search_radius: Search radius in meters (default from env)

    Returns:
        Dict with:
            - 'polygon': Shapely Polygon or MultiPolygon
            - 'source': 'osm_overpass'
            - 'osm_id': OpenStreetMap element ID
            - 'tags': OSM tags (building type, name, etc.)
            - 'contains_point': True if building polygon contains the input point
        or None if no building found
    """
    global _last_overpass_call

    if search_radius is None:
        search_radius = FOOTPRINT_SEARCH_RADIUS

    # Rate limiting
    elapsed = time.time() - _last_overpass_call
    if elapsed < OVERPASS_RATE_LIMIT:
        time.sleep(OVERPASS_RATE_LIMIT - elapsed)

    # Overpass query: find building polygons near the point
    # 'way' covers most buildings; 'relation' covers complex multipolygon buildings
    query = f"""
    [out:json][timeout:{OVERPASS_TIMEOUT}];
    (
      way["building"](around:{search_radius},{lat},{lon});
      relation["building"](around:{search_radius},{lat},{lon});
    );
    out body geom;
    """

    try:
        response = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=OVERPASS_TIMEOUT + 5
        )
        _last_overpass_call = time.time()

        if response.status_code != 200:
            log.warning(f"Overpass API returned {response.status_code}: {response.text[:200]}")
            return None

        data = response.json()
        elements = data.get("elements", [])

        if not elements:
            log.info(f"No buildings found within {search_radius}m of ({lat:.6f}, {lon:.6f})")
            return None

        log.info(f"Found {len(elements)} building(s) near ({lat:.6f}, {lon:.6f})")

        # Convert OSM elements to Shapely polygons
        target_point = Point(lon, lat)  # Shapely uses (x, y) = (lon, lat)
        best_building = None
        best_distance = float('inf')
        contains_match = None

        for element in elements:
            polygon = _osm_element_to_polygon(element)
            if polygon is None:
                continue

            # Check if this building contains our target point
            if polygon.contains(target_point):
                contains_match = {
                    'polygon': polygon,
                    'source': 'osm_overpass',
                    'osm_id': element.get('id'),
                    'osm_type': element.get('type'),
                    'tags': element.get('tags', {}),
                    'contains_point': True,
                }
                log.info(f"Building {element.get('id')} contains target point")
                break  # Exact match, no need to keep looking

            # Track nearest building as fallback
            dist = polygon.distance(target_point)
            if dist < best_distance:
                best_distance = dist
                best_building = {
                    'polygon': polygon,
                    'source': 'osm_overpass',
                    'osm_id': element.get('id'),
                    'osm_type': element.get('type'),
                    'tags': element.get('tags', {}),
                    'contains_point': False,
                }

        if contains_match:
            return contains_match

        if best_building:
            log.info(f"No building contains point; using nearest "
                     f"(OSM ID {best_building['osm_id']}, distance: {best_distance:.6f}°)")
            return best_building

        return None

    except requests.exceptions.Timeout:
        log.warning(f"Overpass API timeout for ({lat:.6f}, {lon:.6f})")
        _last_overpass_call = time.time()
        return None
    except Exception as e:
        log.error(f"Overpass API error: {e}", exc_info=True)
        _last_overpass_call = time.time()
        return None


def _osm_element_to_polygon(element: Dict) -> Optional[Polygon]:
    """
    Convert an OSM element (way or relation) to a Shapely Polygon.

    Handles:
        - Simple ways (closed polygons)
        - Relations with type=multipolygon (outer/inner rings)
    """
    elem_type = element.get("type")

    if elem_type == "way":
        # Simple polygon from way geometry
        geom = element.get("geometry", [])
        if not geom or len(geom) < 4:
            return None

        coords = [(node["lon"], node["lat"]) for node in geom]

        # Ensure polygon is closed
        if coords[0] != coords[-1]:
            coords.append(coords[0])

        try:
            poly = Polygon(coords)
            if poly.is_valid:
                return poly
            # Try to fix invalid polygon
            poly = poly.buffer(0)
            if poly.is_valid and not poly.is_empty:
                return poly if isinstance(poly, Polygon) else None
        except Exception:
            return None

    elif elem_type == "relation":
        # Multipolygon relation — extract outer and inner rings
        members = element.get("members", [])
        outers = []
        inners = []

        for member in members:
            if member.get("type") != "way":
                continue
            geom = member.get("geometry", [])
            if not geom or len(geom) < 4:
                continue

            coords = [(node["lon"], node["lat"]) for node in geom]
            if coords[0] != coords[-1]:
                coords.append(coords[0])

            role = member.get("role", "outer")
            if role == "outer":
                outers.append(coords)
            elif role == "inner":
                inners.append(coords)

        if not outers:
            return None

        try:
            # Build polygon from first outer ring with inner rings as holes
            poly = Polygon(outers[0], holes=inners if inners else None)
            if poly.is_valid:
                return poly
            poly = poly.buffer(0)
            if poly.is_valid and not poly.is_empty:
                return poly if isinstance(poly, Polygon) else None
        except Exception:
            return None

    return None


# -------------------------------------------------------------------------
# Geometric filtering: check YOLO detections against building footprint
# -------------------------------------------------------------------------

def classify_detections(
    detections: List[Dict[str, Any]],
    footprint: Dict[str, Any],
    center_lat: float, center_lon: float,
    zoom: int,
    img_width: int, img_height: int
) -> List[Dict[str, Any]]:
    """
    Classify each YOLO detection as inside/outside/boundary relative
    to the target building footprint.

    Args:
        detections: List of dicts, each with:
            - 'bbox': (x1, y1, x2, y2) in pixel coordinates
            - 'confidence': float
            - 'class': int or str
        footprint: Dict from get_building_footprint()
        center_lat, center_lon: Satellite image center
        zoom: Mapbox zoom level
        img_width, img_height: Image dimensions

    Returns:
        List of dicts, each detection enriched with:
            - 'location': 'inside' | 'outside' | 'boundary'
            - 'det_latlon': (lat, lon) of detection center
            - 'distance_to_building': distance in degrees (0 if inside)
    """
    polygon = footprint['polygon']

    # Small buffer for "boundary" classification (detections near the edge)
    # ~2 meters in degrees at mid-latitudes
    boundary_buffer = 0.00002

    results = []
    for det in detections:
        x1, y1, x2, y2 = det['bbox']

        # Detection center in pixels
        det_cx = (x1 + x2) / 2
        det_cy = (y1 + y2) / 2

        # Convert to lat/lon
        det_lat, det_lon = pixel_to_latlon(
            det_cx, det_cy,
            center_lat, center_lon,
            zoom, img_width, img_height
        )

        det_point = Point(det_lon, det_lat)

        # Classify
        if polygon.contains(det_point):
            location = 'inside'
            distance = 0.0
        elif polygon.buffer(boundary_buffer).contains(det_point):
            location = 'boundary'
            distance = polygon.distance(det_point)
        else:
            location = 'outside'
            distance = polygon.distance(det_point)

        enriched = {
            **det,
            'location': location,
            'det_latlon': (det_lat, det_lon),
            'distance_to_building': distance,
        }

        log.info(
            f"Detection at pixel ({det_cx:.0f}, {det_cy:.0f}) → "
            f"({det_lat:.6f}, {det_lon:.6f}) → {location.upper()} "
            f"(conf: {det['confidence']:.3f}, dist: {distance:.6f}°)"
        )

        results.append(enriched)

    return results


def filter_detections(
    classified: List[Dict[str, Any]],
    keep_boundary: bool = True
) -> Tuple[List[Dict], List[Dict]]:
    """
    Split classified detections into kept (on-building) and rejected (off-building).

    Args:
        classified: Output from classify_detections()
        keep_boundary: Whether to keep detections on the boundary (default True)

    Returns:
        (kept, rejected) — two lists of detection dicts
    """
    kept = []
    rejected = []

    for det in classified:
        loc = det['location']
        if loc == 'inside':
            kept.append(det)
        elif loc == 'boundary' and keep_boundary:
            kept.append(det)
        else:
            rejected.append(det)
            log.info(
                f"Filtered out detection at ({det['det_latlon'][0]:.6f}, "
                f"{det['det_latlon'][1]:.6f}) — {loc}, "
                f"distance: {det['distance_to_building']:.6f}°"
            )

    log.info(f"Footprint filter: {len(kept)} kept, {len(rejected)} rejected "
             f"out of {len(classified)} total detections")

    return kept, rejected


def extract_detections_from_yolo(yolo_result) -> List[Dict[str, Any]]:
    """
    Convert YOLO result object to a list of detection dicts.

    Args:
        yolo_result: Ultralytics YOLO result object

    Returns:
        List of dicts with 'bbox', 'confidence', 'class' keys
    """
    detections = []

    if not hasattr(yolo_result, 'boxes') or yolo_result.boxes is None:
        return detections

    boxes = yolo_result.boxes

    for i in range(len(boxes)):
        try:
            bbox = boxes.xyxy[i].tolist()
            conf = float(boxes.conf[i].item())
            cls = int(boxes.cls[i].item()) if hasattr(boxes, 'cls') else 0

            detections.append({
                'bbox': tuple(bbox),
                'confidence': conf,
                'class': cls,
            })
        except Exception as e:
            log.warning(f"Error extracting detection {i}: {e}")

    return detections


# -------------------------------------------------------------------------
# High-level convenience function for the pipeline
# -------------------------------------------------------------------------

def footprint_filter_pipeline(
    yolo_result,
    center_lat: float, center_lon: float,
    zoom: int = 19,
    img_width: int = 768, img_height: int = 768,
    search_radius: int = None,
) -> Dict[str, Any]:
    """
    Full footprint filtering pipeline: fetch building → classify detections → filter.

    This is the main entry point for the pipeline. Call this after YOLO inference.

    Args:
        yolo_result: Ultralytics YOLO result object
        center_lat, center_lon: Satellite image center
        zoom: Mapbox zoom level
        img_width, img_height: Image dimensions
        search_radius: Building search radius in meters

    Returns:
        Dict with:
            - 'footprint': building footprint dict (or None)
            - 'kept': list of detections on the target building
            - 'rejected': list of detections on neighboring buildings
            - 'all_classified': all detections with location labels
            - 'footprint_found': bool
    """
    # Step 1: Extract detections from YOLO
    detections = extract_detections_from_yolo(yolo_result)

    if not detections:
        log.info("No YOLO detections to filter")
        return {
            'footprint': None,
            'kept': [],
            'rejected': [],
            'all_classified': [],
            'footprint_found': False,
        }

    log.info(f"Filtering {len(detections)} YOLO detection(s) against building footprint")

    # Step 2: Fetch building footprint
    footprint = get_building_footprint(center_lat, center_lon, search_radius)

    if not footprint:
        log.warning("No building footprint found — passing all detections through unfiltered")
        # Can't filter without a footprint; pass everything through
        for det in detections:
            det['location'] = 'unknown'
            det['det_latlon'] = pixel_to_latlon(
                (det['bbox'][0] + det['bbox'][2]) / 2,
                (det['bbox'][1] + det['bbox'][3]) / 2,
                center_lat, center_lon, zoom, img_width, img_height
            )
            det['distance_to_building'] = None
        return {
            'footprint': None,
            'kept': detections,  # Pass through when no footprint available
            'rejected': [],
            'all_classified': detections,
            'footprint_found': False,
        }

    # Step 3: Classify detections
    classified = classify_detections(
        detections, footprint,
        center_lat, center_lon,
        zoom, img_width, img_height
    )

    # Step 4: Filter
    kept, rejected = filter_detections(classified)

    return {
        'footprint': footprint,
        'kept': kept,
        'rejected': rejected,
        'all_classified': classified,
        'footprint_found': True,
    }
