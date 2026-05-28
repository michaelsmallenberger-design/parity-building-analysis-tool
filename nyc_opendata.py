"""
NYC OpenData registry-first confirmation for cooling tower detection.

Registry-first rule: if a building's BIN is registered in NYC OpenData — the
DOHMH cooling tower registrations (Local Law 77) OR the planimetric cooling
tower layer — the building is a confident POSITIVE and the caller skips
YOLO+VLM. A registry hit means cooling-tower infrastructure exists or existed
at that building. Active vs decommissioned status is surfaced in the citation
for information only; it NEVER gates the verdict.

A confident skip requires ALL of:
  - the geocoded point is inside the NYC bounding box
  - the OSM footprint that CONTAINS the geocoded point carries a BIN tag
    (contains_point == True; a nearest-building fallback is NOT trusted, since
    that BIN could belong to a neighbor)
  - a DOHMH or planimetric row within 50m has a BIN equal to that target BIN

No registry hit, a distance-only match (no BIN match), or a BIN match on a
nearest-fallback footprint → confirmed=False, and the caller runs the full
pipeline unchanged. This module never produces a negative.

No new production dependencies: requests, shapely, geopy are already used by
the pipeline. The planimetric the_geom ships from Socrata in WGS84, so there is
no reprojection step.
"""

import logging
from typing import Any, Dict

import requests
from geopy.distance import distance as geopy_distance
from shapely.geometry import shape

log = logging.getLogger("nyc_opendata")

DOHMH_ENDPOINT = "https://data.cityofnewyork.us/resource/y4fw-iqfr.json"
PLANIMETRIC_ENDPOINT = "https://data.cityofnewyork.us/resource/x748-37q7.json"

HIT_RADIUS_M = 50
HTTP_TIMEOUT = 15
DOHMH_BBOX_PAD_DEG = 0.0005  # ~55m at NYC latitude; DOHMH has no geom column

OSM_BIN_TAG_KEYS = ("nycdoitt:bin", "addr:bin", "ref:nycdoitt:bin")

# NYC bounding box (WGS84). Outside this, the registry does not apply.
NYC_LAT_MIN, NYC_LAT_MAX = 40.4774, 40.9176
NYC_LON_MIN, NYC_LON_MAX = -74.2591, -73.7004


def _in_nyc(lat: float, lon: float) -> bool:
    return NYC_LAT_MIN <= lat <= NYC_LAT_MAX and NYC_LON_MIN <= lon <= NYC_LON_MAX


def _canonical_bin(val: Any) -> str:
    """Normalize a BIN to canonical string form.

    DOHMH serves bin as a stringified float ('1018503.0'); planimetric and OSM
    serve clean strings. Canonicalize so equality works across sources.
    """
    if val is None or val == "":
        return ""
    try:
        return str(int(float(val)))
    except (TypeError, ValueError):
        return str(val).strip()


def _extract_osm_bin(tags: Dict[str, Any]) -> str:
    for key in OSM_BIN_TAG_KEYS:
        val = tags.get(key)
        if val:
            return _canonical_bin(val)
    return ""


def _query_dohmh(lat: float, lon: float) -> list:
    """DOHMH has latitude/longitude columns (no geom). Bbox prefilter, refine to 50m."""
    where = (
        f"latitude between {lat - DOHMH_BBOX_PAD_DEG} and {lat + DOHMH_BBOX_PAD_DEG} "
        f"and longitude between {lon - DOHMH_BBOX_PAD_DEG} and {lon + DOHMH_BBOX_PAD_DEG}"
    )
    r = requests.get(
        DOHMH_ENDPOINT,
        params={"$where": where, "$limit": 1000},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for row in r.json():
        try:
            rlat = float(row["latitude"])
            rlon = float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        d_m = geopy_distance((lat, lon), (rlat, rlon)).meters
        if d_m <= HIT_RADIUS_M:
            row["_distance_m"] = d_m
            out.append(row)
    return out


def _query_planimetric(lat: float, lon: float) -> list:
    """Planimetric the_geom ships in WGS84; within_circle queries it directly."""
    where = f"within_circle(the_geom, {lat}, {lon}, {HIT_RADIUS_M})"
    r = requests.get(
        PLANIMETRIC_ENDPOINT,
        params={"$where": where, "$limit": 1000},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for row in r.json():
        geom = row.get("the_geom")
        if not geom:
            continue
        centroid = shape(geom).centroid  # WGS84 (x=lon, y=lat)
        d_m = geopy_distance((lat, lon), (centroid.y, centroid.x)).meters
        if d_m <= HIT_RADIUS_M:
            row["_distance_m"] = d_m
            out.append(row)
    return out


def lookup_nyc_registry(lat: float, lon: float, footprint: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether (lat, lon)'s building is registry-confirmed.

    Returns a dict:
      {'confirmed': bool, 'source': 'dohmh'|'planimetric'|'both'|None,
       'bin': str|None, 'distance_m': float|None, 'citation': str|None}

    confirmed=True only on a BIN-matched hit with contains_point. Registry query
    failures are swallowed (the registry is a confidence boost, not a hard
    dependency) — a failure degrades to confirmed=False and the caller runs the
    full pipeline.
    """
    result = {"confirmed": False, "source": None, "bin": None, "distance_m": None, "citation": None}

    if not _in_nyc(lat, lon):
        return result

    # Trust the BIN only if the geocoded point is actually inside this footprint.
    if not footprint.get("contains_point"):
        return result
    target_bin = _extract_osm_bin(footprint.get("tags", {}))
    if not target_bin:
        return result

    try:
        dohmh = _query_dohmh(lat, lon)
    except Exception as e:
        log.warning(f"DOHMH query failed at ({lat:.6f},{lon:.6f}): {e}")
        dohmh = []
    try:
        planim = _query_planimetric(lat, lon)
    except Exception as e:
        log.warning(f"Planimetric query failed at ({lat:.6f},{lon:.6f}): {e}")
        planim = []

    dohmh_match = [r for r in dohmh if _canonical_bin(r.get("bin")) == target_bin]
    planim_match = [r for r in planim if _canonical_bin(r.get("bin")) == target_bin]

    if not dohmh_match and not planim_match:
        return result  # no BIN match (distance-only or nothing) → fall through

    citations, dists = [], []
    if dohmh_match:
        nearest = min(dohmh_match, key=lambda r: r["_distance_m"])
        dists.append(nearest["_distance_m"])
        active = nearest.get("activeequipment")
        active_str = active if (active is not None and active != "") else "unknown"
        citations.append(f"Registered under LL77 (DOHMH), BIN {target_bin}, active equipment: {active_str}")
    if planim_match:
        nearest = min(planim_match, key=lambda r: r["_distance_m"])
        dists.append(nearest["_distance_m"])
        status = nearest.get("status") or "unknown"
        citations.append(f"Captured in NYC Planimetric (status: {status}), BIN {target_bin}")

    result["confirmed"] = True
    result["bin"] = target_bin
    result["source"] = "both" if (dohmh_match and planim_match) else ("dohmh" if dohmh_match else "planimetric")
    result["distance_m"] = min(dists) if dists else None
    result["citation"] = " | ".join(citations)
    return result
