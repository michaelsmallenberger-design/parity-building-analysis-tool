"""Dual-VLM test harness for vlm.verify_detection (Gemini + Grok consensus).

Runs vlm.verify_detection end-to-end against two known-good raw satellite
tiles from pipeline_test_outputs/. Exits 0 only when both fixtures pass
structure AND semantics checks. Any other outcome (pre-flight failure,
structural mismatch, wrong verdict, API/network error in either sub-model)
exits nonzero.
"""

from __future__ import annotations

import functools
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import vlm

TEST_140_WEST_END = {
    "image_path": "pipeline_test_outputs/140_West_End_Avenue_New_York_NY_10023_raw.jpg",
    "address": "140 West End Avenue, New York, NY 10023",
    "lat": 40.775879,
    "lon": -73.986001,
    "footprint_metadata": {"osm_id": 269233314, "tags": {}, "contains_point": True},
    "expected_verdict": ["confirmed", "likely"],
}

TEST_22_NORTH_6TH = {
    "image_path": "pipeline_test_outputs/22_North_6th_Street_Brooklyn_NY_11249_raw.jpg",
    "address": "22 North 6th Street, Brooklyn, NY 11249",
    "lat": 40.720034,
    "lon": -73.963510,
    "footprint_metadata": {"osm_id": 279780014, "tags": {}, "contains_point": True},
    "expected_verdict": ["confirmed", "likely"],
}

_YOLO_MODEL_PATH = "models/rooftop_model.pt"
_YOLO_CONF = 0.18
_FULL_IMAGE_BBOX = (0, 0, 768, 768)
_VALID_VERDICTS = {"confirmed", "likely", "neighbor_only", "needs_review", "not_detected"}
_SUB_MODEL_KEYS = {"verdict", "confidence", "reasoning", "construction"}
_TOP_LEVEL_KEYS = {
    "verdict", "confidence", "reasoning", "construction",
    "gemini", "grok", "agreement",
}
_CONSENSUS_NEEDS_REVIEW_MARKERS = ("disagreed", "threshold", "timeout")
_API_ERROR_MARKERS = (
    "after 4 attempts",
    "Network timeout",
    "Network connection error",
    "API server error",
    "API throttled",
    "API authentication error",
    "API authorization error",
    "API rejected the request",
    "API client error",
    "VLM API error",
    "VLM returned an empty response",
    "schema mismatch",
    "Grok network timeout",
    "Grok network connection error",
    "Grok API transient error",
    "Grok API authentication error",
    "Grok API authorization error",
    "Grok API rejected the request",
    "Grok API client error",
    "Grok VLM API error",
    "Grok returned an empty response",
    "Grok response shape unexpected",
)


def _print_pass(label: str) -> None:
    print(f"  [PASS] {label}")


def _print_fail(label: str) -> None:
    print(f"  [FAIL] {label}")


def _preflight() -> bool:
    print("=== PRE-FLIGHT ===")
    for var in ("GEMINI_API_KEY", "XAI_API_KEY"):
        if os.environ.get(var):
            _print_pass(f"{var} is set")
        else:
            _print_fail(f"{var} is not set")
            return False

    paths = (
        ("140 West End raw image", TEST_140_WEST_END["image_path"]),
        ("22 North 6th raw image", TEST_22_NORTH_6TH["image_path"]),
        ("YOLO model", _YOLO_MODEL_PATH),
    )
    for label, path in paths:
        if Path(path).is_file():
            _print_pass(f"{label} exists: {path}")
        else:
            _print_fail(f"{label} missing: {path}")
            return False
    return True


@functools.lru_cache(maxsize=1)
def _get_yolo_model():
    print(f"Loading YOLO model from {_YOLO_MODEL_PATH} ...")
    from ultralytics import YOLO

    return YOLO(_YOLO_MODEL_PATH)


def _run_yolo(image_path: str) -> tuple[tuple[int, int, int, int], float, int]:
    """Run YOLO and return (best_bbox, best_conf, total_detections)."""
    model = _get_yolo_model()
    results = model.predict(image_path, conf=_YOLO_CONF, verbose=False)
    if not results:
        return _FULL_IMAGE_BBOX, 0.0, 0
    boxes = results[0].boxes
    if boxes is None or len(boxes) == 0:
        return _FULL_IMAGE_BBOX, 0.0, 0
    confs = boxes.conf.tolist()
    xyxy = boxes.xyxy.tolist()
    best_idx = max(range(len(confs)), key=lambda i: confs[i])
    bx = xyxy[best_idx]
    bbox = (int(bx[0]), int(bx[1]), int(bx[2]), int(bx[3]))
    return bbox, float(confs[best_idx]), len(boxes)


def _build_building_context(fixture: dict) -> dict:
    return {
        "address": fixture["address"],
        "lat": fixture["lat"],
        "lon": fixture["lon"],
        "footprint_metadata": fixture["footprint_metadata"],
    }


def _validate_sub_dict(name: str, sub) -> list[tuple[bool, str]]:
    checks: list[tuple[bool, str]] = []
    sub_is_dict = isinstance(sub, dict)
    checks.append((sub_is_dict, f"{name} sub-dict is a dict (got: {type(sub).__name__})"))
    if not sub_is_dict:
        return checks
    sub_keys = set(sub.keys())
    checks.append((
        sub_keys == _SUB_MODEL_KEYS,
        f"{name} sub-dict has exactly four keys (got: {sorted(sub_keys)})",
    ))
    v = sub.get("verdict")
    checks.append((v in _VALID_VERDICTS, f"{name}.verdict is a valid literal (got: {v!r})"))
    c = sub.get("confidence")
    checks.append((
        isinstance(c, float) and 0.0 <= c <= 1.0,
        f"{name}.confidence is float in [0.0, 1.0] (got: {c!r})",
    ))
    r = sub.get("reasoning")
    checks.append((
        isinstance(r, str) and len(r) > 0,
        f"{name}.reasoning is a non-empty string (length: {len(r) if isinstance(r, str) else None})",
    ))
    k = sub.get("construction")
    checks.append((isinstance(k, bool), f"{name}.construction is a bool (got: {k!r})"))
    return checks


def _validate_structure(resp) -> tuple[bool, list[tuple[bool, str]]]:
    checks: list[tuple[bool, str]] = []

    is_dict = isinstance(resp, dict)
    checks.append((is_dict, f"response is a dict (got: {type(resp).__name__})"))
    if not is_dict:
        return False, checks

    keys = set(resp.keys())
    checks.append((
        keys == _TOP_LEVEL_KEYS,
        f"response has exactly seven keys (got: {sorted(keys)})",
    ))

    verdict = resp.get("verdict")
    checks.append(
        (verdict in _VALID_VERDICTS, f"verdict is a valid literal (got: {verdict!r})")
    )

    confidence = resp.get("confidence")
    conf_ok = isinstance(confidence, float) and 0.0 <= confidence <= 1.0
    checks.append((conf_ok, f"confidence is float in [0.0, 1.0] (got: {confidence!r})"))

    reasoning = resp.get("reasoning")
    reason_len = len(reasoning) if isinstance(reasoning, str) else None
    reason_ok = isinstance(reasoning, str) and len(reasoning) > 0
    checks.append((reason_ok, f"reasoning is a non-empty string (length: {reason_len})"))

    construction = resp.get("construction")
    checks.append(
        (isinstance(construction, bool), f"construction is a bool (got: {construction!r})")
    )

    agreement = resp.get("agreement")
    checks.append(
        (isinstance(agreement, bool), f"agreement is a bool (got: {agreement!r})")
    )

    checks.extend(_validate_sub_dict("gemini", resp.get("gemini")))
    checks.extend(_validate_sub_dict("grok", resp.get("grok")))

    # Conditional structure check: when top verdict is needs_review and
    # neither sub-model itself failed with an API error, the synthesized
    # reasoning must lead with a recognizable marker.
    if verdict == "needs_review":
        gemini_sub = resp.get("gemini") if isinstance(resp.get("gemini"), dict) else {}
        grok_sub = resp.get("grok") if isinstance(resp.get("grok"), dict) else {}
        sub_failed = _is_api_inconclusive(gemini_sub.get("reasoning")) or _is_api_inconclusive(
            grok_sub.get("reasoning")
        )
        if not sub_failed and isinstance(reasoning, str):
            has_marker = any(m in reasoning.lower() for m in _CONSENSUS_NEEDS_REVIEW_MARKERS)
            checks.append((
                has_marker,
                "needs_review reasoning contains 'disagreed'/'threshold'/'timeout' marker",
            ))

    all_ok = all(p for p, _ in checks)
    return all_ok, checks


def _is_api_inconclusive(reasoning) -> bool:
    if not isinstance(reasoning, str):
        return False
    return any(m in reasoning for m in _API_ERROR_MARKERS)


def _run_fixture(name: str, fixture: dict) -> dict:
    """Run a single fixture. Returns dict with structure_pass, semantics_pass, inconclusive."""
    print()
    print(f"=== TEST: {name} ===")
    print(f"  address: {fixture['address']}")
    print(f"  image:   {fixture['image_path']}")

    bbox, yolo_conf, total = _run_yolo(fixture["image_path"])
    if total == 0:
        print(
            f"  [WARN] YOLO produced zero detections at conf={_YOLO_CONF}; "
            f"falling back to full-image bbox {_FULL_IMAGE_BBOX}"
        )
    else:
        print(f"  YOLO: {total} detection(s); using best bbox {bbox} (conf={yolo_conf:.3f})")

    ctx = _build_building_context(fixture)

    print("  Calling vlm.verify_detection() ...")
    t0 = time.perf_counter()
    response = vlm.verify_detection(fixture["image_path"], bbox, ctx)
    elapsed = time.perf_counter() - t0
    print(f"  VLM call returned in {elapsed:.2f}s")

    print()
    print("  --- VLM consensus response ---")
    if isinstance(response, dict):
        for k in ("verdict", "confidence", "reasoning", "construction", "agreement"):
            if k in response:
                print(f"  {k}: {response[k]!r}")
        for sub_name in ("gemini", "grok"):
            sub = response.get(sub_name)
            if isinstance(sub, dict):
                print(f"  --- {sub_name} ---")
                for k in ("verdict", "confidence", "reasoning", "construction"):
                    if k in sub:
                        print(f"    {k}: {sub[k]!r}")
        for k in sorted(response.keys()):
            if k not in _TOP_LEVEL_KEYS:
                print(f"  [unexpected key] {k}: {response[k]!r}")
    else:
        print(f"  response (not a dict): {response!r}")

    print()
    print("  Structure checks:")
    structure_pass, checks = _validate_structure(response)
    for passed, label in checks:
        (_print_pass if passed else _print_fail)(label)

    print("  Semantics check:")
    inconclusive = False
    semantics_pass = False
    if isinstance(response, dict):
        verdict = response.get("verdict")
        reasoning = response.get("reasoning", "")
        if verdict == "needs_review" and _is_api_inconclusive(reasoning):
            inconclusive = True
            _print_fail(
                f"verdict='needs_review' from an API/network/schema condition (INCONCLUSIVE). "
                f"reasoning: {reasoning!r}"
            )
        elif verdict in fixture["expected_verdict"]:
            semantics_pass = True
            _print_pass(
                f"verdict {verdict!r} is in expected {fixture['expected_verdict']}"
            )
        else:
            _print_fail(
                f"verdict {verdict!r} NOT in expected {fixture['expected_verdict']}"
            )
    else:
        _print_fail("cannot check semantics (response is not a dict)")

    return {
        "name": name,
        "structure_pass": structure_pass,
        "semantics_pass": semantics_pass,
        "inconclusive": inconclusive,
        "elapsed": elapsed,
    }


def main() -> int:
    t_start = time.perf_counter()
    print("test_vlm.py - dual-VLM verification harness (Gemini + Grok)")
    print()

    if not _preflight():
        print()
        print("FAILURE: pre-flight check failed; see [FAIL] line above.")
        return 2

    fixtures = (
        ("140 West End Avenue, New York, NY 10023", TEST_140_WEST_END),
        ("22 North 6th Street, Brooklyn, NY 11249", TEST_22_NORTH_6TH),
    )

    results = []
    for name, fx in fixtures:
        results.append(_run_fixture(name, fx))

    total_elapsed = time.perf_counter() - t_start

    print()
    print("=== SUMMARY ===")
    print(f"Total runtime: {total_elapsed:.2f}s")

    failures: list[str] = []
    for r in results:
        s_label = "PASS" if r["structure_pass"] else "FAIL"
        if r["inconclusive"]:
            sem_label = "INCONCLUSIVE"
        elif r["semantics_pass"]:
            sem_label = "PASS"
        else:
            sem_label = "FAIL"
        print(
            f"  {r['name']}: structure={s_label} semantics={sem_label} "
            f"({r['elapsed']:.2f}s)"
        )
        if not r["structure_pass"]:
            failures.append(f"{r['name']} structure")
        if r["inconclusive"]:
            failures.append(f"{r['name']} semantics INCONCLUSIVE (API/network/schema)")
        elif not r["semantics_pass"]:
            failures.append(f"{r['name']} semantics")

    print()
    if not failures:
        print("SUCCESS: both fixtures passed structure AND semantics.")
        return 0
    print(f"FAILURE: {'; '.join(failures)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
