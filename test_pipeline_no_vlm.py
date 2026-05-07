"""
test_pipeline_no_vlm.py — sanity check for geometry.py + YOLO + Mapbox
without VLM verification.

Reads test_addresses_local.csv, runs each address through:
    geocode -> footprint -> centroid -> satellite image -> YOLO -> footprint filter
Writes labeled output images and a results CSV to pipeline_test_outputs/.
"""

import os
import sys
import csv
import re
import logging
import traceback
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv
from shapely.geometry import MultiPolygon

# Load .env BEFORE importing utils — utils reads MAPBOX_API_KEY at import time.
load_dotenv()

import utils
import geometry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_pipeline")

REPO_ROOT = Path(__file__).resolve().parent
INPUT_CSV = REPO_ROOT / "test_addresses_local.csv"
OUT_DIR = REPO_ROOT / "pipeline_test_outputs"
RESULTS_CSV = OUT_DIR / "results.csv"
MODEL_PATH = REPO_ROOT / "models" / "rooftop_model.pt"

YOLO_CONF = 0.18
ZOOM = 19
IMG_W, IMG_H = 768, 768

# BGR colors for cv2
COLOR_POLYGON = (0, 0, 255)        # red
COLOR_INSIDE   = (0, 255, 0)       # green
COLOR_OUTSIDE  = (0, 255, 255)     # yellow
COLOR_BOUNDARY = (255, 0, 0)       # blue
COLOR_UNKNOWN  = (128, 128, 128)   # gray (no-footprint passthrough fallback)

CSV_FIELDS = [
    "Address", "Expected", "Lat", "Lon",
    "Footprint_Found", "OSM_ID",
    "YOLO_Detection_Count", "OnTarget_Count",
    "Neighbor_Count", "Boundary_Count", "Notes",
]


def _safe_filename(s: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", s)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:80] or "address"


def _empty_result(address: str, expected: str) -> dict:
    return {
        "Address": address,
        "Expected": expected,
        "Lat": "",
        "Lon": "",
        "Footprint_Found": "False",
        "OSM_ID": "",
        "YOLO_Detection_Count": 0,
        "OnTarget_Count": 0,
        "Neighbor_Count": 0,
        "Boundary_Count": 0,
        "Notes": "",
    }


def _draw_footprint(img, polygon, center_lat: float, center_lon: float) -> None:
    """Draw OSM footprint outline in red. Handles Polygon and MultiPolygon."""
    polys = list(polygon.geoms) if isinstance(polygon, MultiPolygon) else [polygon]
    for poly in polys:
        # shapely stores exterior coords as (lon, lat)
        pixel_coords = []
        for lon_v, lat_v in poly.exterior.coords:
            px, py = geometry.latlon_to_pixel(
                lat_v, lon_v,
                center_lat, center_lon,
                ZOOM, IMG_W, IMG_H,
            )
            pixel_coords.append((int(round(px)), int(round(py))))
        if len(pixel_coords) < 2:
            continue
        pts = np.array(pixel_coords, dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(img, [pts], isClosed=True, color=COLOR_POLYGON, thickness=2)


def _draw_detections(img, all_classified: list) -> None:
    """Color-code YOLO bboxes by det['location']: inside/outside/boundary/unknown."""
    for det in all_classified:
        x1, y1, x2, y2 = (int(round(v)) for v in det["bbox"])
        loc = det.get("location")
        if loc == "inside":
            color = COLOR_INSIDE
        elif loc == "outside":
            color = COLOR_OUTSIDE
        elif loc == "boundary":
            color = COLOR_BOUNDARY
        else:
            color = COLOR_UNKNOWN
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)


def _load_test_addresses() -> list:
    if not INPUT_CSV.exists():
        msg = (
            f"ERROR: {INPUT_CSV.name} not found at {INPUT_CSV}\n\n"
            "Create test_addresses_local.csv with 8-10 addresses to validate the\n"
            "Phase 3 pipeline before VLM is wired in. Suggested mix:\n"
            "  - 4 NYC controls drawn from addresses.csv\n"
            "  - 4-6 non-NYC addresses from the TAM spreadsheet\n\n"
            "Required column: Address\n"
            "Optional column: Expected (positive | negative | unknown)\n"
        )
        print(msg, file=sys.stderr)
        sys.exit(2)

    rows = []
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            addr = (r.get("Address") or "").strip()
            if not addr:
                continue
            rows.append({
                "Address": addr,
                "Expected": (r.get("Expected") or "").strip().lower() or "unknown",
            })

    if not rows:
        print(f"ERROR: {INPUT_CSV.name} has no usable rows.", file=sys.stderr)
        sys.exit(2)

    return rows


def _process_address(model, row: dict, idx: int, total: int) -> dict:
    address = row["Address"]
    expected = row["Expected"]
    print(f"Processing {idx}/{total}: {address}...")

    result = _empty_result(address, expected)

    # 1. Geocode
    coords = utils.geocode_address_mapbox(address)
    if not coords:
        result["Notes"] = "Geocoding failed"
        log.warning("Geocoding failed: %s", address)
        return result
    geo_lat, geo_lon = coords
    result["Lat"] = f"{geo_lat:.6f}"
    result["Lon"] = f"{geo_lon:.6f}"

    # 2. Footprint
    footprint = geometry.get_building_footprint(geo_lat, geo_lon)
    if not footprint:
        result["Notes"] = "No OSM footprint"
        log.warning("No OSM footprint: %s", address)
        return result
    result["Footprint_Found"] = "True"
    if footprint.get("osm_id") is not None:
        result["OSM_ID"] = str(footprint["osm_id"])

    # 3. Centroid (works on Polygon and MultiPolygon)
    polygon = footprint["polygon"]
    centroid = polygon.centroid
    centroid_lat, centroid_lon = centroid.y, centroid.x
    result["Lat"] = f"{centroid_lat:.6f}"
    result["Lon"] = f"{centroid_lon:.6f}"

    # 4. Satellite image — absolute path to dodge utils.py:329 bare-filename bug
    stem = _safe_filename(address)
    raw_path = os.path.abspath(str(OUT_DIR / f"{stem}_raw.jpg"))
    ok = utils.get_satellite_image_mapbox(centroid_lat, centroid_lon, raw_path)
    if not ok:
        result["Notes"] = "Mapbox satellite fetch failed"
        log.warning("Satellite fetch failed: %s", address)
        return result

    # 5. Raw YOLO inference (bypass run_prediction — we need the result object)
    yolo_result = model.predict(source=raw_path, conf=YOLO_CONF, verbose=False)[0]

    # 6. Footprint filter
    pipeline = geometry.footprint_filter_pipeline(
        yolo_result, centroid_lat, centroid_lon,
        zoom=ZOOM, img_width=IMG_W, img_height=IMG_H,
    )
    all_classified = pipeline["all_classified"]

    on_target = sum(1 for d in all_classified if d.get("location") == "inside")
    neighbor  = sum(1 for d in all_classified if d.get("location") == "outside")
    boundary  = sum(1 for d in all_classified if d.get("location") == "boundary")
    result["YOLO_Detection_Count"] = len(all_classified)
    result["OnTarget_Count"] = on_target
    result["Neighbor_Count"] = neighbor
    result["Boundary_Count"] = boundary

    # 7. Labeled output image
    img = cv2.imread(raw_path)
    if img is None:
        result["Notes"] = "cv2 could not read raw image"
        return result
    _draw_footprint(img, polygon, centroid_lat, centroid_lon)
    _draw_detections(img, all_classified)
    labeled_path = os.path.abspath(str(OUT_DIR / f"{stem}_labeled.jpg"))
    cv2.imwrite(labeled_path, img)

    return result


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not MODEL_PATH.exists():
        print(
            f"ERROR: YOLO weights not found at {MODEL_PATH}\n"
            "Restore models/rooftop_model.pt from Google Drive before running.",
            file=sys.stderr,
        )
        return 2

    rows = _load_test_addresses()

    # Lazy-load ultralytics + model once, after pre-flight checks pass
    from ultralytics import YOLO
    log.info("Loading YOLO model from %s", MODEL_PATH)
    model = YOLO(str(MODEL_PATH))

    results = []
    total = len(rows)
    for idx, row in enumerate(rows, start=1):
        try:
            results.append(_process_address(model, row, idx, total))
        except Exception as e:
            log.error("Address crashed: %s\n%s", row["Address"], traceback.format_exc())
            crashed = _empty_result(row["Address"], row["Expected"])
            crashed["Notes"] = f"EXCEPTION: {e!r}"
            results.append(crashed)

    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    n = len(results)
    fp_found = sum(1 for r in results if r["Footprint_Found"] == "True")
    total_dets = sum(int(r["YOLO_Detection_Count"]) for r in results)
    on_target = sum(int(r["OnTarget_Count"]) for r in results)
    neighbor = sum(int(r["Neighbor_Count"]) for r in results)
    boundary = sum(int(r["Boundary_Count"]) for r in results)

    pct = (fp_found / n * 100.0) if n else 0.0
    print()
    print(f"Footprint coverage rate: {fp_found}/{n} ({pct:.1f}%)")
    print(f"Total YOLO detections: {total_dets} (across all addresses)")
    print(f"On-target / Neighbor / Boundary breakdown: {on_target} / {neighbor} / {boundary}")
    print(f"\nResults CSV: {RESULTS_CSV}")
    print(f"Labeled images: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
