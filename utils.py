import os
import io
import time
import re
import logging
import threading
import math
from typing import Optional, Tuple, List

import requests
from PIL import Image, ImageDraw
from functools import lru_cache
from geopy.geocoders import Nominatim

MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")  # geocoding + Static Maps imagery
# Google bakes a logo (bottom-left) + "©... Maxar" credit (bottom-right) into Static Maps
# tiles; YOLO detects that text as equipment. Mask the bottom strip this many px (0=off).
GOOGLE_WATERMARK_PX = int(os.getenv("GOOGLE_WATERMARK_PX", "40"))

# Google Address Validation API — upstream address-quality gate (separate API from
# Geocoding; must be enabled on the same project/key). Fail-open: any API error returns
# None so the pipeline behaves exactly as before. NOTE: HELD/unshipped — verdict adds
# ~nil value on clean address lists (see memory project_parity_google_geocoder_ab).
ADDRESS_VALIDATION_ENABLED = os.getenv("ADDRESS_VALIDATION_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

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

# Geocoder-agreement trust flag: corroborate the Mapbox point against an
# independent Nominatim geocode. Agreement within the threshold -> 'high';
# divergence (or no Nominatim match) -> 'low' = the location is worth a manual
# look (not necessarily wrong). Set GEOCODE_CONFIDENCE=0 to skip the extra call.
GEOCODE_CONFIDENCE_ENABLED = os.environ.get("GEOCODE_CONFIDENCE", "1").strip().lower() not in ("0", "false", "no", "off")
GEOCODE_DIVERGENCE_THRESHOLD_M = float(os.getenv("GEOCODE_DIVERGENCE_THRESHOLD_M", "60"))

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# -------------------------------------------------------------------------
# Geocoding: Smart Mapbox + Nominatim fallback (no Google Maps API)
# -------------------------------------------------------------------------

# Nominatim client (lazy-initialized with rate limiting)
_nominatim_client = None
_last_nominatim_call = 0.0
# Serializes Nominatim calls: the public endpoint requires a hard 1 req/s, and
# the _last_nominatim_call timestamp is otherwise racy under concurrent geocodes.
_nominatim_lock = threading.Lock()

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


def _http_post(url: str, json_body: dict, params: dict = None) -> Optional[dict]:
    """POST sibling of _http_get for JSON APIs (e.g. Address Validation, which is POST-only).
    Same retry/backoff/timeout policy. Returns parsed JSON dict or None on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, params=params, json=json_body, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            else:
                log.warning("HTTP %s from POST %s %s", r.status_code, url, r.text[:200])
        except requests.RequestException as e:
            log.warning("HTTP error on POST %s: %s", url, e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return None


def validate_address_google(query: str) -> Optional[dict]:
    """Google Address Validation API — upstream address-quality check, BEFORE geocoding.
    Returns a compact dict or None (None = disabled / API error / no result; fail-open).

    {
      "verdict": "confirmed" | "coarse" | "unconfirmed",
      "has_inferred": bool,      # Google had to guess a component (informational only)
      "has_unconfirmed": bool,   # Google could not confirm a component
      "granularity": str,        # validationGranularity (e.g. PREMISE, ROUTE, OTHER)
      "formatted": str,          # standardized address
    }

    verdict: "unconfirmed" if any component is unconfirmed; else "coarse" if granularity
    is coarser than PREMISE/SUB_PREMISE; else "confirmed".

    NOTE: hasInferredComponents is deliberately NOT part of the verdict — it is ~always
    true on well-formed addresses (Google infers ZIP+4 / subpremises on virtually every
    real address; measured true on 9/9 varied real addresses incl. landmarks), so using
    it as a trigger would fire on nearly everything. The discriminating signals are
    unconfirmed components and coarse granularity, which correlate with our
    ambiguous_footprint / neighbor_only failures; callers use a non-"confirmed" verdict to
    trigger the geocoder cross-check early."""
    if not ADDRESS_VALIDATION_ENABLED:
        return None
    if not GOOGLE_MAPS_API_KEY:
        log.error("GOOGLE_MAPS_API_KEY not set; cannot validate address")
        return None
    data = _http_post(
        "https://addressvalidation.googleapis.com/v1:validateAddress",
        {"address": {"addressLines": [query]}},
        params={"key": GOOGLE_MAPS_API_KEY},
    )
    if not data or "result" not in data:
        return None
    result = data["result"]
    verdict_obj = result.get("verdict", {})
    has_inferred = bool(verdict_obj.get("hasInferredComponents"))
    has_unconfirmed = bool(verdict_obj.get("hasUnconfirmedComponents"))
    granularity = verdict_obj.get("validationGranularity", "")
    fine = granularity in ("PREMISE", "SUB_PREMISE")
    if has_unconfirmed:
        verdict = "unconfirmed"
    elif not fine:
        verdict = "coarse"
    else:
        verdict = "confirmed"
    formatted = result.get("address", {}).get("formattedAddress", "")
    log.info("Address Validation '%s' -> %s (granularity=%s inferred=%s unconfirmed=%s)",
             query, verdict, granularity, has_inferred, has_unconfirmed)
    return {
        "verdict": verdict,
        "has_inferred": has_inferred,
        "has_unconfirmed": has_unconfirmed,
        "granularity": granularity,
        "formatted": formatted,
    }


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

    with _nominatim_lock:
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


def _haversine_m(lat1, lon1, lat2, lon2):
    r1, n1, r2, n2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = r2 - r1, n2 - n1
    h = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def _active_geocode(query: str) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) from the geocoder selected by GEOCODER_PROVIDER.
    Default 'mapbox' = the existing production path (unchanged). Set
    GEOCODER_PROVIDER=google to feed Google's point into the pipeline instead;
    everything downstream (tiles, YOLO, VLM) is identical either way."""
    provider = os.getenv("GEOCODER_PROVIDER", "google").strip().lower()
    if provider == "mapbox":
        return geocode_address_mapbox(query)
    return geocode_address_google(query)


def geocode_with_confidence(query: str):
    """Geocode + a free agreement-based trust flag.

    Returns (lat, lon, confidence, divergence_m). lat/lon come from the geocoder
    selected by GEOCODER_PROVIDER (default Mapbox; 'google' to A/B the geocoder),
    so tile centering and everything downstream are identical regardless.
    An independent Nominatim geocode corroborates it: agreement within
    GEOCODE_DIVERGENCE_THRESHOLD_M -> 'high'; beyond it (or no Nominatim match)
    -> 'low'. confidence is None when geocoding fails or the check is disabled.
    """
    coords = _active_geocode(query)
    if not coords:
        return None, None, None, None
    lat, lon = coords
    if not GEOCODE_CONFIDENCE_ENABLED:
        return lat, lon, None, None
    nm = _geocode_nominatim(query)
    if not nm:
        return lat, lon, "low", None
    d = _haversine_m(lat, lon, nm[0], nm[1])
    return lat, lon, ("high" if d <= GEOCODE_DIVERGENCE_THRESHOLD_M else "low"), round(d, 1)


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


def geocode_address_google(query: str) -> Optional[Tuple[float, float]]:
    """Geocode via the Google Maps Geocoding API. Drop-in for
    geocode_address_mapbox: returns (lat, lon) or None. Selected by
    GEOCODER_PROVIDER=google. Requires GOOGLE_MAPS_API_KEY in env."""
    if not GOOGLE_MAPS_API_KEY:
        log.error("GOOGLE_MAPS_API_KEY not set; cannot geocode via Google")
        return None
    data = _http_get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        {"address": query, "key": GOOGLE_MAPS_API_KEY},
    )
    if data and data.get("status") == "OK" and data.get("results"):
        g = data["results"][0]
        loc = g["geometry"]["location"]
        lat, lon = loc["lat"], loc["lng"]
        log.info("Google geocoded '%s' -> (%.6f, %.6f) [location_type=%s]",
                 query, lat, lon, g["geometry"].get("location_type"))
        return (lat, lon)
    status = (data or {}).get("status", "no-response")
    log.warning("Google geocode returned no usable result for '%s' (status=%s)", query, status)
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


def get_satellite_image_google(lat: float, lon: float, out_path: str, zoom: int = None) -> bool:
    """Google Static Maps satellite tile — drop-in for get_satellite_image_mapbox.
    size=(MAPBOX_SIZE/2) scale=2 zoom=(zoom-1) reproduces the Mapbox 768@zoom tile's
    exact ground coverage + metres/pixel, so the hardcoded 768px geometry math and
    tile_zoom downstream stay valid. Selected by IMAGERY_PROVIDER=google.
    Note: Google bakes a small attribution/logo watermark into the bottom corners
    (TOS-required; cannot be disabled like Mapbox logo=false)."""
    if not GOOGLE_MAPS_API_KEY:
        log.error("GOOGLE_MAPS_API_KEY not set; cannot use Google imagery")
        return False
    try:
        mz = MAPBOX_ZOOM if zoom is None else zoom
        w, h = MAPBOX_SIZE.lower().split("x")
        logical = f"{int(w) // 2}x{int(h) // 2}"  # 768 -> 384; scale=2 doubles back to 768
        url = "https://maps.googleapis.com/maps/api/staticmap"
        params = {
            "center": f"{lat:.7f},{lon:.7f}",
            # size=(MAPBOX_SIZE/2) scale=2 renders MAPBOX_SIZE device px; Google's coverage
            # scales by DEVICE px, so zoom=mz matches Mapbox MAPBOX_SIZE@mz coverage+resolution.
            # (zoom=mz-1 rendered ~2x too wide -> rooftop equipment too small for the VLMs.)
            "zoom": mz,
            "size": logical,
            "scale": 2,
            "maptype": "satellite",
            "format": "jpg",
            "key": GOOGLE_MAPS_API_KEY,
        }
        with requests.get(url, params=params, timeout=HTTP_TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                log.warning("Google Static error %s: %s", r.status_code, r.text[:200])
                return False
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            # Paint over the bottom watermark strip so YOLO can't detect the logo/credit
            # text as cooling-tower equipment (the cause of the full-Google false positives).
            if GOOGLE_WATERMARK_PX > 0:
                ImageDraw.Draw(img).rectangle(
                    [0, img.height - GOOGLE_WATERMARK_PX, img.width, img.height], fill=(0, 0, 0))
            img.save(out_path, format="JPEG", quality=92)
        return True
    except Exception as e:
        log.error("Google satellite fetch failed: %s", e)
        return False


def get_streetview_image_google(lat: float, lon: float, out_path: str,
                                fov: int = 100, pitch: int = 15) -> bool:
    """Google Street View Static API ground-level photo. Heading is intentionally
    omitted -- Google then auto-aims the camera at (lat, lon) from the nearest
    panorama, which tracks the existing geocoded point with no extra bearing math.
    Checks /streetview/metadata first (free) so a location with no coverage costs
    nothing and returns False instead of saving Google's gray "no imagery" tile.
    source=outdoor excludes indoor business "Photo Sphere" panoramas (e.g. a shop's
    interior tour) that the default search will otherwise return as the "nearest"
    match when they happen to sit closer than any car-mounted street imagery."""
    if not GOOGLE_MAPS_API_KEY:
        log.error("GOOGLE_MAPS_API_KEY not set; cannot use Street View")
        return False
    try:
        meta = requests.get(
            "https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": f"{lat:.7f},{lon:.7f}", "source": "outdoor",
                    "key": GOOGLE_MAPS_API_KEY},
            timeout=HTTP_TIMEOUT,
        ).json()
        if meta.get("status") != "OK":
            log.info("No outdoor Street View coverage near (%.6f, %.6f): status=%s",
                     lat, lon, meta.get("status"))
            return False
        url = "https://maps.googleapis.com/maps/api/streetview"
        params = {
            "size": "640x640",
            "location": f"{lat:.7f},{lon:.7f}",
            "fov": fov,
            "pitch": pitch,
            "source": "outdoor",
            "key": GOOGLE_MAPS_API_KEY,
        }
        with requests.get(url, params=params, timeout=HTTP_TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                log.warning("Street View Static error %s: %s", r.status_code, r.text[:200])
                return False
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.save(out_path, format="JPEG", quality=92)
        return True
    except Exception as e:
        log.error("Street View fetch failed: %s", e)
        return False


def get_satellite_image(lat: float, lon: float, out_path: str, zoom: int = None,
                        provider: str = None) -> bool:
    """Dispatch to the imagery provider. An explicit `provider` (per-address override,
    e.g. dense urban → 'mapbox') wins; otherwise fall back to the IMAGERY_PROVIDER env
    (default 'google' = Google Static Maps; 'mapbox' = legacy path)."""
    prov = (provider or os.getenv("IMAGERY_PROVIDER", "google")).strip().lower()
    if prov == "mapbox":
        return get_satellite_image_mapbox(lat, lon, out_path, zoom=zoom)
    return get_satellite_image_google(lat, lon, out_path, zoom=zoom)


# -------------------------------------------------------------------------
# YOLO inference (lazy-load model)
# -------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _get_model():
    """
    Load the YOLO model once, on first call.
    Using lru_cache avoids an import-time model load (a past OOM cause on memory-constrained hosts).
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
