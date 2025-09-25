import os
import io
import json
import math
import time
import logging
from typing import Optional, Tuple

import requests
from PIL import Image
from functools import lru_cache

MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

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

# Simple retry config for external calls
HTTP_TIMEOUT = 12
MAX_RETRIES = 3
RETRY_BACKOFF = 0.7

log = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


# -------------------------------------------------------------------------
# Geocoding: Google first (handles place names), fallback to Mapbox if needed
# -------------------------------------------------------------------------
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


def geocode_address_mapbox(query: str) -> Optional[Tuple[float, float]]:
    """
    Geocode a free-form query (e.g., 'The Octavia Condo') to (lat, lon).
    Strategy:
      1) Google Geocoding API (good with place names & fuzzy input)
      2) If no result, Mapbox geocoding as a fallback
    Returns (lat, lon) or None.
    """
    # 1) Google Geocoding
    try:
        g_url = "https://maps.googleapis.com/maps/api/geocode/json"
        g_params = {"address": query, "key": GOOGLE_API_KEY, "region": "us"}
        g_data = _http_get(g_url, g_params)
        if g_data and g_data.get("status") in ("OK", "ZERO_RESULTS"):
            results = g_data.get("results", [])
            if results:
                loc = results[0]["geometry"]["location"]
                lat, lon = loc["lat"], loc["lng"]
                return (lat, lon)
    except Exception as e:
        log.warning("Google Geocoding error: %s", e)

    # 2) Mapbox Geocoding fallback
    try:
        # Bias to NYC bbox if you want (minLon,minLat,maxLon,maxLat)
        # NYC bbox approx: -74.25909,40.477399,-73.700272,40.917577
        mb_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{requests.utils.quote(query)}.json"
        mb_params = {
            "access_token": MAPBOX_API_KEY,
            "limit": 1,
            "proximity": "-73.9857,40.7484",  # near Midtown as a helpful bias
            "types": "address,poi,place,neighborhood,locality",
            "autocomplete": "true",
            "country": "US",
            "bbox": "-74.25909,40.477399,-73.700272,40.917577",  # NYC bbox
        }
        mb_data = _http_get(mb_url, mb_params)
        if mb_data and mb_data.get("features"):
            center = mb_data["features"][0]["center"]
            lon, lat = center[0], center[1]
            return (lat, lon)
    except Exception as e:
        log.warning("Mapbox Geocoding error: %s", e)

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

        params = {"access_token": MAPBOX_API_KEY}
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
