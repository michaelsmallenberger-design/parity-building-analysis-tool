import os
import io
import time
import re
import logging
from typing import Optional, Tuple, List

import requests
from PIL import Image
from functools import lru_cache
from geopy.geocoders import Nominatim

MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY")

# YOLO model path (can override with env)
MODEL_PATH = os.getenv("MODEL_PATH", os.path.join("models", "rooftop_model.pt"))

# Ensemble paths: comma-separated. Falls back to single MODEL_PATH when unset.
# Each model produces detections, filtered to cooling-tower class names only, then
# IoU-deduped across models in geometry.ensemble_dedupe_detections.
MODEL_PATHS = os.getenv("MODEL_PATHS", MODEL_PATH)

# YOLO inference confidence threshold (0.0-1.0). Lower = more detections, more false positives.
YOLO_CONF = float(os.getenv("YOLO_CONF", "0.18"))

# Mapbox static image settings
MAPBOX_STYLE = "mapbox/satellite-v9"  # satellite basemap
MAPBOX_ZOOM = int(os.getenv("MAPBOX_ZOOM", "19"))  # detail tile; 18–20 good for roofs, 19 optimal
MAPBOX_ZOOM_WIDE = int(os.getenv("MAPBOX_ZOOM_WIDE", "18"))  # wide tile for ground-mounted CTs / parcel context
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
    Caller is responsible for providing complete address context
    (city, state) — this function does not append geographic context.

    Examples:
        "52 East 72nd St (Claremont House), New York, NY" →
            ["52 East 72nd St (Claremont House), New York, NY",
             "52 East 72nd St, New York, NY"]

        "790 E Broward Blvd, Fort Lauderdale, FL" →
            ["790 E Broward Blvd, Fort Lauderdale, FL"]
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

    # If address has street number, try street-only as a fallback variation
    if re.search(r'^\d+', address):
        street_only = re.split(r'[\(]', address)[0].strip()
        if street_only not in queries and street_only:
            queries.append(street_only)

    return queries


def is_fully_qualified_address(address: str) -> bool:
    """True if the address carries a 5-digit ZIP, which disambiguates it.

    Used to gate the registry-first skip: that path has no VLM safety net, so a
    misgeocode would silently confirm the wrong building. A ZIP forces the
    geocoder onto the right block (e.g. '641 Fifth Avenue, New York, NY' is
    ambiguous between Midtown and Park Slope; '...NY 10022' is not).

    A leading street number is stripped first so '10000 Main St' (no ZIP) is not
    a false positive.
    """
    body = re.sub(r'^\s*\d+(?:-\d+)?\s+', '', address or '')
    return bool(re.search(r'\b\d{5}(?:-\d{4})?\b', body))


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
            "types": "address,poi,place,neighborhood,locality",
            "autocomplete": "false",
            "country": "US",
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

        location = geolocator.geocode(address, timeout=HTTP_TIMEOUT)
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
      3. Fall back to Nominatim (free, good with building names)
      4. If Nominatim fails, fall back to low-confidence Mapbox result

    Caller is responsible for providing complete address context
    (city, state) — this function does not constrain results to any
    geographic region.

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

            # If high confidence, use it
            if relevance >= GEOCODE_CONFIDENCE_THRESHOLD:
                lat, lon = coords
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
        log.info(f"Nominatim geocoded '{original_query}' → ({lat:.6f}, {lon:.6f})")
        return coords

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
def get_satellite_image_mapbox(lat: float, lon: float, out_path: str, zoom: int = None) -> bool:
    """
    Downloads a satellite image centered at (lat, lon) using Mapbox Static Images API.
    Saves to out_path (JPEG/PNG depending on API response content-type).
    zoom defaults to MAPBOX_ZOOM (the detail tile); pass MAPBOX_ZOOM_WIDE for the
    wider parcel/ground-equipment tile.
    Returns True on success, False otherwise.
    """
    try:
        z = MAPBOX_ZOOM if zoom is None else zoom
        coords = f"{lon:.7f},{lat:.7f},{z}"
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
# YOLO inference (lazy-load model)
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


@lru_cache(maxsize=1)
def _get_models():
    """Load all ensemble models per MODEL_PATHS env (comma-separated paths).

    Returns a tuple of (label, YOLO_model) pairs. label is the file basename,
    useful for provenance tagging on detections downstream. Falls back to a
    single-entry tuple when MODEL_PATHS is unset (= MODEL_PATH).
    """
    from ultralytics import YOLO
    paths = [p.strip() for p in MODEL_PATHS.split(",") if p.strip()]
    if not paths:
        paths = [MODEL_PATH]
    loaded = []
    for path in paths:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"YOLO model not found at {path}. "
                f"Set MODEL_PATHS to a comma-separated list of existing weights."
            )
        log.info("Loading YOLO model from %s", path)
        loaded.append((os.path.basename(path), YOLO(path)))
    return tuple(loaded)


def ct_class_indices(model) -> set:
    """Return the set of class indices whose class name contains 'cooling'.

    Handles the new single-class model (returns {0}) and the prior 2-class model
    (returns {1}, filtering out the junk class 0 named '0'). Robust to future
    class-name additions.
    """
    return {idx for idx, name in model.names.items() if "cooling" in str(name).lower()}
