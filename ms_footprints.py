"""Microsoft Building Footprints fallback for footprint lookup.

Used by geometry.get_building_footprint when OSM has no polygon at the geocoded
point and the address is outside NYC (NYC uses the planimetric layer instead).

Microsoft GlobalMLBuildingFootprints is open data (ODbL), served from public Azure
blob storage with NO auth/key. The dataset is partitioned by Bing **zoom-9 quadkey**:
a `dataset-links.csv` maps each QuadKey to the URL of a gzipped file. Those files
carry the `.csv.gz` extension but actually contain **GeoJSONL** — one GeoJSON
Feature per line. To resolve a point we: compute its z9 quadkey, look up the tile
URL(s), download+cache the tile, and spatial-search its polygons.

PERFORMANCE: a US z9 tile can be tens of MB gzipped / hundreds of thousands of
features, so the FIRST lookup in a new quadkey pays a download + an O(features)
line scan. This is acceptable for a low-traffic (~1000/mo) batch tool where the
addresses in a batch cluster by quadkey (the on-disk cache amortizes within a run).
On Railway the cache dir is ephemeral (wiped on redeploy) — a within-deploy
accelerator, not durable storage. Point MS_FOOTPRINT_CACHE_DIR at a volume if that
becomes painful.

Best-effort: every failure (network, format drift, missing coverage) returns None,
never raises — the caller then falls through to footprint_missing as before.
No new dependencies (requests, shapely + stdlib only).
"""

import csv
import gzip
import io
import json
import logging
import math
import os

import requests
from shapely.geometry import Point, shape

log = logging.getLogger(__name__)

_DATASET_LINKS_URL = (
    "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
)
_QUADKEY_ZOOM = 9
_HTTP_TIMEOUT_LINKS = 60
_HTTP_TIMEOUT_TILE = 120
# Don't accept an MS nearest-polygon further than this from the point (degrees ~ a
# bit over 60m at US latitudes). The metric containment/tolerance is recomputed by
# geometry._classify_containment; this is just a sanity cap so we never return a
# wildly distant building from a sparse tile.
_NEAREST_CAP_DEG = 0.0006

_CACHE_DIR = os.getenv(
    "MS_FOOTPRINT_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ms_footprint_cache"),
)


def _latlon_to_quadkey(lat, lon, zoom=_QUADKEY_ZOOM):
    """Bing Maps tile-system quadkey (string) for (lat, lon) at the given zoom."""
    sin = math.sin(math.radians(lat))
    sin = min(max(sin, -0.9999), 0.9999)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((0.5 - math.log((1 + sin) / (1 - sin)) / (4 * math.pi)) * n)
    x = min(max(x, 0), n - 1)
    y = min(max(y, 0), n - 1)
    qk = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        qk.append(str(digit))
    return "".join(qk)


def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _links_path():
    return os.path.join(_CACHE_DIR, "dataset-links.csv")


def _ensure_dataset_links():
    """Download+cache the dataset-links.csv once. Returns the local path or None."""
    path = _links_path()
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        with requests.get(_DATASET_LINKS_URL, timeout=_HTTP_TIMEOUT_LINKS, stream=True) as r:
            if r.status_code != 200:
                log.warning("MS dataset-links HTTP %s", r.status_code)
                return None
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, path)
        return path
    except Exception as e:
        log.warning("MS dataset-links download failed: %s", e)
        return None


def _tile_urls_for_quadkey(quadkey):
    """All tile URLs whose QuadKey matches (a quadkey can span >1 region row)."""
    links = _ensure_dataset_links()
    if not links:
        return []
    urls = []
    try:
        with open(links, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # US quadkeys start with 0; the CSV may store QuadKey as an int that
                # drops the leading zero — zero-pad to the fixed zoom-9 width to match.
                if (row.get("QuadKey") or "").strip().zfill(_QUADKEY_ZOOM) == quadkey:
                    url = (row.get("Url") or "").strip()
                    if url:
                        urls.append(url)
    except Exception as e:
        log.warning("MS dataset-links parse failed: %s", e)
    return urls


def _ensure_tile(quadkey, url):
    """Download+cache one quadkey tile (gzipped GeoJSONL). Returns local path or None."""
    name = f"{quadkey}_{os.path.basename(url.split('?')[0])}"
    if not name.endswith(".gz"):
        name += ".gz"
    path = os.path.join(_CACHE_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        with requests.get(url, timeout=_HTTP_TIMEOUT_TILE, stream=True) as r:
            if r.status_code != 200:
                log.warning("MS tile HTTP %s for %s", r.status_code, url)
                return None
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, path)
        return path
    except Exception as e:
        log.warning("MS tile download failed (%s): %s", url, e)
        return None


def _search_tile(path, pt):
    """Scan one gzipped GeoJSONL tile. Returns (contains_poly, nearest_poly, nearest_d)."""
    contains_poly = None
    nearest_poly = None
    nearest_d = float("inf")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    geom = json.loads(line).get("geometry")
                    if not geom:
                        continue
                    poly = shape(geom)
                except Exception:
                    continue
                if poly.contains(pt):
                    return poly, None, 0.0
                d = poly.distance(pt)
                if d < nearest_d:
                    nearest_d, nearest_poly = d, poly
    except Exception as e:
        log.warning("MS tile read failed (%s): %s", path, e)
    return contains_poly, nearest_poly, nearest_d


def ms_building_footprint(lat, lon):
    """Microsoft footprint polygon containing (lat, lon), else the nearest within a
    ~60m cap, else None. Best-effort — never raises."""
    try:
        _ensure_cache_dir()
        quadkey = _latlon_to_quadkey(lat, lon)
        urls = _tile_urls_for_quadkey(quadkey)
        if not urls:
            return None
        pt = Point(lon, lat)
        best_nearest = None
        best_nearest_d = float("inf")
        for url in urls:
            tile = _ensure_tile(quadkey, url)
            if not tile:
                continue
            contains_poly, nearest_poly, nearest_d = _search_tile(tile, pt)
            if contains_poly is not None:
                return contains_poly
            if nearest_poly is not None and nearest_d < best_nearest_d:
                best_nearest_d, best_nearest = nearest_d, nearest_poly
        if best_nearest is not None and best_nearest_d <= _NEAREST_CAP_DEG:
            return best_nearest
        return None
    except Exception as e:
        log.warning("MS building footprint failed at (%.6f,%.6f): %s", lat, lon, e)
        return None
