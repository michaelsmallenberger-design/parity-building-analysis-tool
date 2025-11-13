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
MAPBOX_ZOOM = int(os.getenv("MAPBOX_ZOOM", "19"))  # 18–20 are usually good for roofs; 19 is optimal
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

# Accuracy improvement: Center-crop analysis to reduce false positives
# When enabled, YOLO only analyzes the center portion of the image to focus on target building
CENTER_ANALYSIS_ENABLED = os.getenv("CENTER_ANALYSIS_ENABLED", "true").lower() == "true"

# Multi-scale analysis: Run detection at two crop sizes to balance false positives and false negatives
# Wide pass catches edge towers, tight pass provides high-precision center detections
MULTI_SCALE_ANALYSIS = os.getenv("MULTI_SCALE_ANALYSIS", "false").lower() == "true"
MULTI_SCALE_WIDE_PERCENT = float(os.getenv("MULTI_SCALE_WIDE_PERCENT", "0.60"))  # Wide: 60%
MULTI_SCALE_TIGHT_PERCENT = float(os.getenv("MULTI_SCALE_TIGHT_PERCENT", "0.30"))  # Tight: 30%

# Aggressive crop mode: Use 30% crop instead of 45% to reduce false positives from nearby buildings
# Trade-off: May miss off-center geocoding, but significantly reduces neighbor building detections
# Note: Superseded by MULTI_SCALE_ANALYSIS when enabled
AGGRESSIVE_CROP = os.getenv("AGGRESSIVE_CROP", "false").lower() == "true"

# Set crop percentage based on mode (only used when MULTI_SCALE_ANALYSIS is disabled)
if AGGRESSIVE_CROP:
    CENTER_CROP_PERCENT = float(os.getenv("CENTER_CROP_PERCENT", "0.30"))  # Aggressive: center 30%
else:
    CENTER_CROP_PERCENT = float(os.getenv("CENTER_CROP_PERCENT", "0.45"))  # Standard: center 45%

SHOW_TARGET_ZONE = os.getenv("SHOW_TARGET_ZONE", "true").lower() == "true"  # Draw target zone indicator

# Distance-based filtering: Filter detections based on distance from image center
# Eliminates false positives from neighboring buildings that appear in crop zones
DISTANCE_FILTER_ENABLED = os.getenv("DISTANCE_FILTER_ENABLED", "false").lower() == "true"
DISTANCE_FILTER_HIGH_CONF_PX = float(os.getenv("DISTANCE_FILTER_HIGH_CONF_PX", "150"))  # Inner zone: high confidence
DISTANCE_FILTER_REVIEW_PX = float(os.getenv("DISTANCE_FILTER_REVIEW_PX", "200"))  # Middle zone: needs review
DISTANCE_FILTER_MAX_PX = float(os.getenv("DISTANCE_FILTER_MAX_PX", "250"))  # Outer boundary: ignore beyond this
SHOW_FILTER_ZONES = os.getenv("SHOW_FILTER_ZONES", "true").lower() == "true"  # Draw distance filter zones

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


def _center_crop_image(img: Image.Image, crop_percent: float) -> Tuple[Image.Image, Tuple[int, int, int, int]]:
    """
    Crop image to center portion to focus on target building.

    Args:
        img: PIL Image
        crop_percent: Percentage of image to keep (0.0-1.0). E.g., 0.45 = keep center 45%

    Returns:
        (cropped_image, crop_box) where crop_box is (left, top, right, bottom) in original coords
    """
    width, height = img.size

    # Calculate crop box (center region)
    crop_width = int(width * crop_percent)
    crop_height = int(height * crop_percent)

    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    right = left + crop_width
    bottom = top + crop_height

    crop_box = (left, top, right, bottom)
    cropped = img.crop(crop_box)

    return cropped, crop_box


def _draw_target_zone(img: Image.Image, crop_box: Tuple[int, int, int, int], color: str = "#00FF00") -> Image.Image:
    """
    Draw a rectangle showing the target analysis zone.

    Args:
        img: PIL Image to draw on
        crop_box: (left, top, right, bottom) coordinates of target zone
        color: Color for the rectangle (hex or named color)

    Returns:
        Image with target zone indicator
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    left, top, right, bottom = crop_box

    # Draw rectangle with dashed effect
    line_width = 3
    dash_length = 15
    gap_length = 10

    # Top edge
    for x in range(left, right, dash_length + gap_length):
        draw.line([(x, top), (min(x + dash_length, right), top)], fill=color, width=line_width)

    # Bottom edge
    for x in range(left, right, dash_length + gap_length):
        draw.line([(x, bottom), (min(x + dash_length, right), bottom)], fill=color, width=line_width)

    # Left edge
    for y in range(top, bottom, dash_length + gap_length):
        draw.line([(left, y), (left, min(y + dash_length, bottom))], fill=color, width=line_width)

    # Right edge
    for y in range(top, bottom, dash_length + gap_length):
        draw.line([(right, y), (right, min(y + dash_length, bottom))], fill=color, width=line_width)

    # Add label
    from PIL import ImageFont
    try:
        # Try to use a nice font if available
        font = ImageFont.truetype("arial.ttf", 16)
    except:
        # Fall back to default font
        font = ImageFont.load_default()

    label = "TARGET ZONE"
    # Position label at top of zone
    label_pos = (left + 10, top + 10)

    # Draw background for label
    try:
        bbox = draw.textbbox(label_pos, label, font=font)
        draw.rectangle(bbox, fill="black")
        draw.text(label_pos, label, fill=color, font=font)
    except:
        # Fallback for older PIL versions
        draw.text(label_pos, label, fill=color, font=font)

    return img


def _calculate_detection_distance(bbox: Tuple[float, float, float, float], img_width: int, img_height: int) -> float:
    """
    Calculate Euclidean distance of detection center from image center.

    Args:
        bbox: YOLO bounding box (x1, y1, x2, y2) in pixel coordinates
        img_width: Image width in pixels
        img_height: Image height in pixels

    Returns:
        Distance in pixels from image center
    """
    import math

    x1, y1, x2, y2 = bbox

    # Calculate detection center
    det_center_x = (x1 + x2) / 2
    det_center_y = (y1 + y2) / 2

    # Image center
    img_center_x = img_width / 2
    img_center_y = img_height / 2

    # Euclidean distance
    distance = math.sqrt((det_center_x - img_center_x)**2 + (det_center_y - img_center_y)**2)

    return distance


def _draw_filter_zones(img: Image.Image, high_conf_radius: float, review_radius: float, max_radius: float) -> Image.Image:
    """
    Draw concentric circles showing distance filter zones.

    Args:
        img: PIL Image to draw on
        high_conf_radius: Inner circle radius (high confidence zone)
        review_radius: Middle circle radius (needs review zone)
        max_radius: Outer circle radius (filter boundary)

    Returns:
        Image with filter zone indicators
    """
    from PIL import ImageDraw, ImageFont
    import math

    draw = ImageDraw.Draw(img)
    width, height = img.size
    center_x, center_y = width / 2, height / 2

    # Draw circles with dashed effect
    def draw_dashed_circle(cx, cy, radius, color, dash_length=10, line_width=2):
        """Draw a dashed circle"""
        circumference = 2 * math.pi * radius
        num_dashes = int(circumference / (dash_length * 2))

        for i in range(num_dashes):
            angle_start = (i * 2 * math.pi) / num_dashes
            angle_end = ((i + 0.5) * 2 * math.pi) / num_dashes

            # Calculate arc points
            x1 = cx + radius * math.cos(angle_start)
            y1 = cy + radius * math.sin(angle_start)
            x2 = cx + radius * math.cos(angle_end)
            y2 = cy + radius * math.sin(angle_end)

            draw.line([(x1, y1), (x2, y2)], fill=color, width=line_width)

    # Draw zones
    if max_radius > 0 and max_radius <= min(center_x, center_y):
        draw_dashed_circle(center_x, center_y, max_radius, "#FF0000", line_width=2)  # Red outer

    if review_radius > 0 and review_radius <= min(center_x, center_y):
        draw_dashed_circle(center_x, center_y, review_radius, "#FFA500", line_width=2)  # Orange middle

    if high_conf_radius > 0 and high_conf_radius <= min(center_x, center_y):
        draw_dashed_circle(center_x, center_y, high_conf_radius, "#00FF00", line_width=2)  # Green inner

    # Add labels
    try:
        font = ImageFont.truetype("arial.ttf", 14)
    except:
        font = ImageFont.load_default()

    # Label positions (top-right quadrant)
    label_x = center_x + 10
    labels = [
        (high_conf_radius, "HIGH CONFIDENCE", "#00FF00"),
        (review_radius, "NEEDS REVIEW", "#FFA500"),
        (max_radius, "FILTER BOUNDARY", "#FF0000")
    ]

    for i, (radius, label_text, color) in enumerate(labels):
        if radius > 0 and radius <= min(center_x, center_y):
            label_y = center_y - radius + (i * 20)
            try:
                bbox = draw.textbbox((label_x, label_y), label_text, font=font)
                draw.rectangle(bbox, fill="black")
                draw.text((label_x, label_y), label_text, fill=color, font=font)
            except:
                draw.text((label_x, label_y), label_text, fill=color, font=font)

    return img


def _run_inference_on_crop(model, original_img: Image.Image, crop_percent: float, conf_thr: float) -> Tuple[any, Tuple[int, int, int, int], Optional[float]]:
    """
    Helper function to run YOLO inference on a cropped portion of the image.

    Returns:
        (yolo_result, crop_box, confidence)
    """
    import tempfile

    cropped_img, crop_box = _center_crop_image(original_img, crop_percent)

    # Save cropped image to temp file for YOLO
    temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(temp_fd)
    cropped_img.save(temp_path, format="JPEG", quality=92)

    try:
        # Run YOLO inference
        results = model.predict(source=temp_path, conf=conf_thr, verbose=False)

        # Compute confidence
        det_conf = None
        if results and len(results) > 0:
            r0 = results[0]
            if hasattr(r0, "boxes") and r0.boxes is not None and len(r0.boxes) > 0:
                try:
                    det_conf = float(r0.boxes.conf.max().item())
                except Exception:
                    det_conf = float(max(r0.boxes.conf)) if len(r0.boxes.conf) else None

        return (results[0] if results and len(results) > 0 else None, crop_box, det_conf)
    finally:
        # Cleanup temp file
        try:
            os.unlink(temp_path)
        except:
            pass


def run_prediction(image_path: str) -> Tuple[str, Optional[float]]:
    """
    Runs YOLO on the given image_path with optional center-crop or multi-scale analysis.
    Saves an annotated image under static/results/<base>_pred.jpg
    Returns (web_relative_url, confidence_float_or_None)

    - web_relative_url is a path like 'results/xxx_pred.jpg' (to be joined with 'static/' by caller)
    - confidence is max confidence among detections (0..1), or None if no detections.

    Multi-scale mode: Runs inference at two crop sizes (wide + tight) to balance
    false positives and false negatives across varying image zoom levels.
    """
    # Ensure result folder exists
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Output file path
    base = _safe_basename(image_path)
    out_filename = f"{base}_pred.jpg"
    out_path = os.path.join(RESULTS_DIR, out_filename)

    # Load original image
    original_img = Image.open(image_path).convert("RGB")

    # Run model
    model = _get_model()
    # YOLO confidence threshold: Higher = fewer detections but higher precision
    # 0.40 reduces false positives from low-confidence phantom detections
    conf_thr = float(os.getenv("YOLO_CONF", "0.40"))

    # Multi-scale analysis: Run two passes at different crop sizes
    if MULTI_SCALE_ANALYSIS and CENTER_ANALYSIS_ENABLED:
        log.info(f"Multi-scale analysis: wide={MULTI_SCALE_WIDE_PERCENT*100:.0f}%, tight={MULTI_SCALE_TIGHT_PERCENT*100:.0f}%")

        # Wide pass: catches edge towers
        wide_result, wide_box, wide_conf = _run_inference_on_crop(
            model, original_img, MULTI_SCALE_WIDE_PERCENT, conf_thr
        )

        # Tight pass: high precision center
        tight_result, tight_box, tight_conf = _run_inference_on_crop(
            model, original_img, MULTI_SCALE_TIGHT_PERCENT, conf_thr
        )

        # Merge results
        detected_wide = wide_conf is not None
        detected_tight = tight_conf is not None

        if detected_wide and detected_tight:
            # Detection in BOTH zones = High confidence (use tighter zone result)
            log.info(f"Multi-scale: Detected in BOTH zones (wide={wide_conf:.3f}, tight={tight_conf:.3f}) → HIGH CONFIDENCE")
            results = [tight_result] if tight_result else None
            crop_box = tight_box
            det_conf = max(wide_conf, tight_conf)
        elif detected_tight:
            # Detection ONLY in tight zone = High confidence center detection
            log.info(f"Multi-scale: Detected ONLY in tight zone ({tight_conf:.3f}) → HIGH CONFIDENCE")
            results = [tight_result]
            crop_box = tight_box
            det_conf = tight_conf
        elif detected_wide:
            # Detection ONLY in wide zone = Possible edge tower or neighbor
            log.info(f"Multi-scale: Detected ONLY in wide zone ({wide_conf:.3f}) → NEEDS REVIEW")
            results = [wide_result]
            crop_box = wide_box
            det_conf = wide_conf
        else:
            # No detection in either zone
            log.info("Multi-scale: No detection in either zone")
            results = None
            crop_box = wide_box  # Show wide zone for context
            det_conf = None

    # Standard single-pass analysis
    elif CENTER_ANALYSIS_ENABLED:
        # Center-crop approach: Only analyze center portion
        log.info(f"Center analysis enabled (crop_percent={CENTER_CROP_PERCENT})")

        result, crop_box, det_conf = _run_inference_on_crop(
            model, original_img, CENTER_CROP_PERCENT, conf_thr
        )
        results = [result] if result else None

    else:
        # Full image analysis (no cropping)
        results = model.predict(source=image_path, conf=conf_thr, verbose=False)
        crop_box = None
        det_conf = None
        if results and len(results) > 0:
            r0 = results[0]
            if hasattr(r0, "boxes") and r0.boxes is not None and len(r0.boxes) > 0:
                try:
                    det_conf = float(r0.boxes.conf.max().item())
                except Exception:
                    det_conf = float(max(r0.boxes.conf)) if len(r0.boxes.conf) else None

    # Apply distance-based filtering if enabled
    if DISTANCE_FILTER_ENABLED and results and len(results) > 0:
        r0 = results[0]
        if hasattr(r0, "boxes") and r0.boxes is not None and len(r0.boxes) > 0:
            import torch

            # Get bounding boxes and confidences
            boxes = r0.boxes.xyxy  # (x1, y1, x2, y2) format
            confs = r0.boxes.conf
            classes = r0.boxes.cls if hasattr(r0.boxes, "cls") else None

            # Filter boxes based on distance from center
            filtered_indices = []
            img_width, img_height = original_img.size

            for i in range(len(boxes)):
                bbox = boxes[i].tolist() if hasattr(boxes[i], "tolist") else boxes[i]
                distance = _calculate_detection_distance(bbox, img_width, img_height)

                # Check if detection passes distance filter
                if distance <= DISTANCE_FILTER_MAX_PX:
                    filtered_indices.append(i)

                    # Log distance classification
                    if distance <= DISTANCE_FILTER_HIGH_CONF_PX:
                        log.info(f"Distance filter: Detection at {distance:.1f}px → HIGH CONFIDENCE (≤{DISTANCE_FILTER_HIGH_CONF_PX}px)")
                    elif distance <= DISTANCE_FILTER_REVIEW_PX:
                        log.info(f"Distance filter: Detection at {distance:.1f}px → NEEDS REVIEW ({DISTANCE_FILTER_HIGH_CONF_PX}-{DISTANCE_FILTER_REVIEW_PX}px)")
                    else:
                        log.info(f"Distance filter: Detection at {distance:.1f}px → LIKELY NEIGHBOR ({DISTANCE_FILTER_REVIEW_PX}-{DISTANCE_FILTER_MAX_PX}px)")
                else:
                    log.info(f"Distance filter: Detection at {distance:.1f}px → FILTERED OUT (>{DISTANCE_FILTER_MAX_PX}px)")

            # Update results with filtered detections
            if filtered_indices:
                # Keep only filtered detections
                filtered_boxes = boxes[filtered_indices]
                filtered_confs = confs[filtered_indices]
                filtered_classes = classes[filtered_indices] if classes is not None else None

                # Update r0.boxes
                r0.boxes.xyxy = filtered_boxes
                r0.boxes.conf = filtered_confs
                if filtered_classes is not None:
                    r0.boxes.cls = filtered_classes

                # Update confidence
                try:
                    det_conf = float(filtered_confs.max().item())
                except:
                    det_conf = float(max(filtered_confs)) if len(filtered_confs) else None

                log.info(f"Distance filter: Kept {len(filtered_indices)} of {len(boxes)} detections")
            else:
                # No detections passed filter
                log.info("Distance filter: All detections filtered out")
                results = None
                det_conf = None

    # Generate annotated image
    try:
        if results and len(results) > 0:
            r0 = results[0]
            if CENTER_ANALYSIS_ENABLED or MULTI_SCALE_ANALYSIS:
                # For center-crop mode: Show full image with target zone indicator
                # Draw YOLO detections on cropped region, then overlay on full image
                import numpy as np

                # Get annotated crop from YOLO
                plotted_crop = r0.plot()  # ndarray HxWxC in BGR
                if plotted_crop is not None:
                    rgb_crop = plotted_crop[:, :, ::-1]

                    # Paste annotated crop back onto full image
                    full_img_with_annotations = original_img.copy()
                    crop_img_pil = Image.fromarray(rgb_crop)
                    full_img_with_annotations.paste(crop_img_pil, (crop_box[0], crop_box[1]))

                    # Draw target zone indicator if enabled
                    if SHOW_TARGET_ZONE:
                        full_img_with_annotations = _draw_target_zone(full_img_with_annotations, crop_box)

                    # Draw distance filter zones if enabled
                    if DISTANCE_FILTER_ENABLED and SHOW_FILTER_ZONES:
                        full_img_with_annotations = _draw_filter_zones(
                            full_img_with_annotations,
                            DISTANCE_FILTER_HIGH_CONF_PX,
                            DISTANCE_FILTER_REVIEW_PX,
                            DISTANCE_FILTER_MAX_PX
                        )

                    full_img_with_annotations.save(out_path, format="JPEG", quality=92)
                else:
                    # Fallback: save original with target zone
                    final_img = original_img.copy()
                    if SHOW_TARGET_ZONE and crop_box:
                        final_img = _draw_target_zone(final_img, crop_box)
                    if DISTANCE_FILTER_ENABLED and SHOW_FILTER_ZONES:
                        final_img = _draw_filter_zones(
                            final_img,
                            DISTANCE_FILTER_HIGH_CONF_PX,
                            DISTANCE_FILTER_REVIEW_PX,
                            DISTANCE_FILTER_MAX_PX
                        )
                    final_img.save(out_path, format="JPEG", quality=92)
            else:
                # Standard mode: Save YOLO annotated image directly
                plotted = r0.plot()  # ndarray HxWxC in BGR
                if plotted is not None:
                    import numpy as np
                    rgb = plotted[:, :, ::-1]
                    Image.fromarray(rgb).save(out_path, format="JPEG", quality=92)
                else:
                    original_img.save(out_path, format="JPEG", quality=92)
        else:
            # No detections
            log.info("No detections found")
            if (CENTER_ANALYSIS_ENABLED or DISTANCE_FILTER_ENABLED) and (SHOW_TARGET_ZONE or SHOW_FILTER_ZONES):
                # Show target zone and/or filter zones even when no detections
                final_img = original_img.copy()
                if SHOW_TARGET_ZONE and crop_box:
                    final_img = _draw_target_zone(final_img, crop_box)
                if DISTANCE_FILTER_ENABLED and SHOW_FILTER_ZONES:
                    final_img = _draw_filter_zones(
                        final_img,
                        DISTANCE_FILTER_HIGH_CONF_PX,
                        DISTANCE_FILTER_REVIEW_PX,
                        DISTANCE_FILTER_MAX_PX
                    )
                final_img.save(out_path, format="JPEG", quality=92)
            else:
                original_img.save(out_path, format="JPEG", quality=92)

    except Exception as e:
        log.error("YOLO processing failed: %s", e, exc_info=True)
        # Try to at least save the original image
        try:
            original_img.save(out_path, format="JPEG", quality=92)
        except Exception:
            pass

    # Return a **web path relative to /static**, matching your tasks code expectations
    web_rel = os.path.join("results", out_filename).replace("\\", "/")
    return web_rel, det_conf
