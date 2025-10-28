import os
import io
import json
import math
import time
import re
import logging
from typing import Optional, Tuple, List

import requests
from PIL import Image
from functools import lru_cache
from geopy.geocoders import Nominatim

MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY")

# --- Paths / constants --- 
STATIC_DIR = os.path.join("static")
RESULTS_DIR = os.path.join(STATIC_DIR, "results")
UPLOADS_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# YOLO model path (can override with env)
MODEL_PATH = os.getenv("MODEL_PATH", os.path.join("models", "rooftop_model.pt"))

# Mapbox static image settings
MAPBOX_STYLE = "mapbox/satellite-v9"  # satellite basemap
MAPBOX_ZOOM = int(os.getenv("MAPBOX_ZOOM", "19"))  # 18–20 are usually good for roofs
MAPBOX_SIZE = os.getenv("MAPBOX_SIZE", "768x768")   # WxH; <= 1280x1280
MAPBOX_HIGH_DPI = os.getenv("MAPBOX_DPI", "false").lower() == "true"  # @2x images
# Optional bottom crop in pixels to remove API watermarks/logos; set via env
MAPBOX_CROP_BOTTOM_PX = int(os.getenv("MAPBOX_CROP_BOTTOM_PX", "0"))

# Simple retry config for external calls
HTTP_TIMEOUT = 12
MAX_RETRIES = 3
RETRY_BACKOFF = 0.7

# Geocoding configuration
GEOCODE_CONFIDENCE_THRESHOLD = float(os.getenv("GEOCODE_CONFIDENCE_THRESHOLD", "0.70"))
NYC_BBOX = "-74.25909,40.477399,-73.700272,40.917577"  # NYC bounding box
NOMINATIM_USER_AGENT = "ParityBuildingAnalysisTool/1.0"
NOMINATIM_RATE_LIMIT = 1.0  # seconds between requests

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# -------------------------------------------------------------------------
# Geocoding: Smart Mapbox + Nominatim fallback (no Google Maps API)
# -------------------------------------------------------------------------

# Nominatim client (lazy-initialized with rate limiting)
_nominatim_client = None
_last_nominatim_call = 0.0

def _get_nominatim_client():
    """Lazy-initialize Nominatim geocoder."""
    global _nominatim_client
    if _nominatim_client is None:
        _nominatim_client = Nominatim(user_agent=NOMINATIM_USER_AGENT)
    return _nominatim_client


def _http_get(url: str, params: dict) -> Optional[dict]:
    """Small helper with basic retries for JSON APIs."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            else:
                log.warning("HTTP %s from %s %s", r.status_code, url, r.text[:200])
        except requests.RequestException as e:
            log.warning("HTTP error on %s: %s", url, e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return None


def _clean_address(address: str) -> List[str]:
    """
    Generate multiple query variations for geocoding.

    Returns list of query strings to try, in priority order.

    Examples:
        "52 East 72nd St (Claremont House)" →
            ["52 East 72nd St (Claremont House)",
             "52 East 72nd St",
             "52 East 72nd St, NY"]

        "The Octavia Condo" →
            ["The Octavia Condo",
             "The Octavia Condo, New York, NY"]
    """
    queries = []

    # Always try original first
    queries.append(address)

    # Clean parenthetical building names and corporate suffixes
    cleaned = re.sub(r'\([^)]*\)', '', address)  # Remove (...)
    cleaned = re.sub(r',\s*Inc\.?$', '', cleaned, flags=re.IGNORECASE)  # Remove ", Inc"
    cleaned = cleaned.strip()

    if cleaned != address and cleaned:
        queries.append(cleaned)

    # If address has street number, try street-only
    if re.search(r'^\d+', address):
        street_only = re.split(r'[\(]', address)[0].strip()
        if street_only not in queries and street_only:
            queries.append(street_only)
            # Add NYC context to street address
            if 'NY' not in street_only.upper():
                queries.append(f"{street_only}, NY")
    else:
        # Building name only - add NYC context
        if 'NY' not in address.upper() and 'NEW YORK' not in address.upper():
            queries.append(f"{address}, New York, NY")

    return queries


def _is_in_nyc(lat: float, lon: float) -> bool:
    """Check if coordinates are within NYC bounding box."""
    # NYC bbox: -74.25909,40.477399,-73.700272,40.917577
    return (40.477399 <= lat <= 40.917577 and
            -74.25909 <= lon <= -73.700272)


def _geocode_mapbox(query: str) -> Optional[Tuple[Tuple[float, float], float]]:
    """
    Geocode using Mapbox API.

    Returns ((lat, lon), relevance_score) or None.
    """
    try:
        mb_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{requests.utils.quote(query)}.json"
        mb_params = {
            "access_token": MAPBOX_API_KEY,
            "limit": 1,
            "proximity": "-73.9857,40.7484",  # Midtown Manhattan bias
            "types": "address,poi,place,neighborhood,locality",
            "autocomplete": "true",
            "country": "US",
            "bbox": NYC_BBOX,  # NYC bounding box (strict filter)
        }
        mb_data = _http_get(mb_url, mb_params)

        if mb_data and mb_data.get("features"):
            feature = mb_data["features"][0]
            center = feature["center"]
            lon, lat = center[0], center[1]
            relevance = feature.get("relevance", 0.0)

            return ((lat, lon), relevance)
    except Exception as e:
        log.warning("Mapbox Geocoding error for '%s': %s", query, e)

    return None


def _geocode_nominatim(address: str) -> Optional[Tuple[float, float]]:
    """
    Geocode using Nominatim (OpenStreetMap) - free, good with building names.

    Rate limited to 1 request per second.
    Returns (lat, lon) or None.
    """
    global _last_nominatim_call

    try:
        # Rate limiting: enforce 1 second between calls
        elapsed = time.time() - _last_nominatim_call
        if elapsed < NOMINATIM_RATE_LIMIT:
            time.sleep(NOMINATIM_RATE_LIMIT - elapsed)

        geolocator = _get_nominatim_client()

        # Add NYC bias if not already in query
        query = address
        if 'NY' not in address.upper() and 'NEW YORK' not in address.upper():
            query = f"{address}, New York, NY, USA"

        location = geolocator.geocode(query, timeout=HTTP_TIMEOUT)
        _last_nominatim_call = time.time()

        if location:
            return (location.latitude, location.longitude)
    except Exception as e:
        log.warning("Nominatim geocoding error for '%s': %s", address, e)
        _last_nominatim_call = time.time()  # Update even on error to maintain rate limit

    return None


def geocode_address_mapbox(query: str) -> Optional[Tuple[float, float]]:
    """
    Smart geocoding with multiple strategies.

    Strategy:
      1. Try Mapbox with multiple query variations (cleaned addresses)
      2. Check confidence threshold (default 0.70)
      3. Validate result is in NYC
      4. Fall back to Nominatim (free, good with building names)

    Returns (lat, lon) or None.
    """
    original_query = query

    # Generate query variations
    queries = _clean_address(query)

    # Try Mapbox with all variations
    best_result = None
    best_relevance = 0.0

    for q in queries:
        result = _geocode_mapbox(q)
        if result:
            coords, relevance = result

            # Keep track of best result
            if relevance > best_relevance:
                best_result = coords
                best_relevance = relevance

            # If high confidence and in NYC, use it
            if relevance >= GEOCODE_CONFIDENCE_THRESHOLD:
                lat, lon = coords
                if _is_in_nyc(lat, lon):
                    log.info(f"Mapbox geocoded '{original_query}' → ({lat:.6f}, {lon:.6f}) "
                            f"[relevance: {relevance:.2f}, query: '{q}']")
                    return coords

    # If we have a result but low confidence, log it
    if best_result:
        lat, lon = best_result
        log.warning(f"Mapbox low confidence ({best_relevance:.2f} < {GEOCODE_CONFIDENCE_THRESHOLD}) "
                   f"for '{original_query}', trying Nominatim...")
    else:
        log.info(f"Mapbox found nothing for '{original_query}', trying Nominatim...")

    # Fall back to Nominatim (slow but good with building names)
    coords = _geocode_nominatim(original_query)
    if coords:
        lat, lon = coords
        # Double-check it's in NYC
        if _is_in_nyc(lat, lon):
            log.info(f"Nominatim geocoded '{original_query}' → ({lat:.6f}, {lon:.6f})")
            return coords
        else:
            log.warning(f"Nominatim result outside NYC for '{original_query}': ({lat:.6f}, {lon:.6f})")

    # If Nominatim failed but we had a Mapbox result, use it as last resort
    if best_result:
        lat, lon = best_result
        log.warning(f"Using low-confidence Mapbox result for '{original_query}': "
                   f"({lat:.6f}, {lon:.6f}) [relevance: {best_relevance:.2f}]")
        return best_result

    return None


# -------------------------------------------------------------------------
# Satellite image fetch (Mapbox Static Images)
# -------------------------------------------------------------------------
def get_satellite_image_mapbox(lat: float, lon: float, out_path: str) -> bool:
    """
    Downloads a satellite image centered at (lat, lon) using Mapbox Static Images API.
    Saves to out_path (JPEG/PNG depending on API response content-type).
    Returns True on success, False otherwise.
    """
    try:
        coords = f"{lon:.7f},{lat:.7f},{MAPBOX_ZOOM}"
        size = MAPBOX_SIZE
        dpi_suffix = "@2x" if MAPBOX_HIGH_DPI else ""
        url = (
            f"https://api.mapbox.com/styles/v1/{MAPBOX_STYLE}/static/"
            f"{coords}/{size}{dpi_suffix}"
        )

        params = {
            "access_token": MAPBOX_API_KEY,
            # Remove Mapbox logo and attribution overlays; provide attribution elsewhere in UI
            "logo": "false",
            "attribution": "false",
        }
        # Stream to avoid loading large images into memory
        with requests.get(url, params=params, timeout=HTTP_TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                log.warning("Mapbox Static error %s: %s", r.status_code, r.text[:200])
                return False
            # Ensure folders exist
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            # Some responses are PNG; normalize to JPG to keep YOLO happy
            img_bytes = io.BytesIO(r.content)
            img = Image.open(img_bytes).convert("RGB")
            # Optionally crop bottom strip to remove any residual marks
            crop_px = MAPBOX_CROP_BOTTOM_PX
            if crop_px and crop_px > 0:
                # If high-DPI requested, scale crop accordingly unless explicitly sized
                eff_crop = crop_px * (2 if MAPBOX_HIGH_DPI else 1)
                if eff_crop < img.height:
                    img = img.crop((0, 0, img.width, img.height - eff_crop))
            img.save(out_path, format="JPEG", quality=92)
        return True
    except Exception as e:
        log.error("Satellite image fetch failed: %s", e)
        return False


# -------------------------------------------------------------------------
# YOLO inference (lazy-load model; save annotated result; return confidence)
# -------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_model():
    """
    Load the YOLO model once, on first call.
    Using lru_cache avoids import-time model load (which caused Cloud Run OOM).
    """
    from ultralytics import YOLO  # import here to avoid heavy import at module load
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(
            f"YOLO model not found at {MODEL_PATH}. "
            "Ensure models/rooftop_model.pt is in the image, or set MODEL_PATH env."
        )
    log.info("Loading YOLO model from %s", MODEL_PATH)
    return YOLO(MODEL_PATH)


def _safe_basename(path: str) -> str:
    base = os.path.basename(path)
    base = base.replace(" ", "_")
    return os.path.splitext(base)[0]


def run_prediction(image_path: str) -> Tuple[str, Optional[float]]:
    """
    Runs YOLO on the given image_path.
    Saves an annotated image under static/results/<base>_pred.jpg
    Returns (web_relative_url, confidence_float_or_None)

    - web_relative_url is a path like 'results/xxx_pred.jpg' (to be joined with 'static/' by caller)
    - confidence is max confidence among detections (0..1), or None if no detections.
    """
    # Ensure result folder exists
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Output file path
    base = _safe_basename(image_path)
    out_filename = f"{base}_pred.jpg"
    out_path = os.path.join(RESULTS_DIR, out_filename)

    # Run model
    model = _get_model()

    # Ultralytics predict:
    # - conf: default is fine; tweak via env YOLO_CONF if desired
    # - imgsz: optional (controls inference size)
    conf_thr = float(os.getenv("YOLO_CONF", "0.25"))

    # We draw boxes ourselves if needed; but using save=True gives us an annotated image.
    # However, save=True writes to a run directory; to control the exact output path,
    # we'll run predict and then save the plotted image manually.
    results = model.predict(source=image_path, conf=conf_thr, verbose=False)

    # Compute a reasonable confidence: max over boxes (if any)
    det_conf: Optional[float] = None
    try:
        if results and len(results) > 0:
            r0 = results[0]
            # r0.boxes.conf is a tensor; get max if there are boxes
            if hasattr(r0, "boxes") and r0.boxes is not None and len(r0.boxes) > 0:
                # .conf is a Tensor[N,1] - take max
                try:
                    det_conf = float(r0.boxes.conf.max().item())
                except Exception:
                    # older versions: r0.boxes.conf may already be a list/ndarray
                    det_conf = float(max(r0.boxes.conf)) if len(r0.boxes.conf) else None

            # Save annotated image
            # r0.plot() returns a numpy array (BGR) with annotations
            plotted = r0.plot()  # ndarray HxWxC in BGR
            if plotted is not None:
                # Convert BGR to RGB for PIL
                import numpy as np
                from PIL import Image
                rgb = plotted[:, :, ::-1]
                Image.fromarray(rgb).save(out_path, format="JPEG", quality=92)
            else:
                # Fallback: copy original if plotting failed
                Image.open(image_path).convert("RGB").save(out_path, format="JPEG", quality=92)
        else:
            # No result object; just copy the original
            Image.open(image_path).convert("RGB").save(out_path, format="JPEG", quality=92)
    except Exception as e:
        log.error("YOLO post-processing failed: %s", e)
        # Try to at least copy the original image to the output
        try:
            Image.open(image_path).convert("RGB").save(out_path, format="JPEG", quality=92)
        except Exception:
            pass

    # Return a **web path relative to /static**, matching your tasks code expectations
    web_rel = os.path.join("results", out_filename).replace("\\", "/")
    return web_rel, det_conf
