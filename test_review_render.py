"""No-spend contract guards for the interactive review page."""
import re

from review_contract import (
    DUAL_FIT_OPTIONS,
    DUAL_FIT_SCHEMA,
    SINGLE_FIT_SCHEMA,
)
from review_render import build_review_page, HVAC_SYSTEMS, FIT_OPTIONS, FIT_COLUMNS


EXPECTED_HVAC = [
    "Cooling Tower", "Chiller", "Exhaust Fan", "RTU", "AHU", "PTAC",
    "Fan Coil", "Heat Pump", "VRF",
]
EXPECTED_LEGACY_FIT = ["Optimizer", "Periscope", "Unclear", "Bad"]
MAP_HOSTS = ["google.com/maps", "earth.google.com", "bing.com/maps"]


def _entry():
    img = "data:image/jpeg;base64,AAAA"
    return {
        "i": 1,
        "address": "1 Test St, Somewhere, ST 10001",
        "verdict": "confirmed",
        "reasoning": "combined consensus text",
        "gemini_verdict": "confirmed",
        "gemini_confidence": 0.9,
        "gemini_reasoning": "gemini box text",
        "grok_verdict": "likely",
        "grok_confidence": 0.8,
        "grok_reasoning": "grok box text",
        "result_image_url": img,
        "result_image_url_wide": img,
        "result_image_url_streetview": img,
    }


def _fit_group(page, key):
    match = re.search(
        rf'<div class="fitchips" data-col="{re.escape(key)}">(.*?)</div>',
        page,
        re.DOTALL,
    )
    assert match, f"missing fit group {key}"
    return match.group(1)


def test_review_contract():
    assert HVAC_SYSTEMS == EXPECTED_HVAC
    assert FIT_OPTIONS == EXPECTED_LEGACY_FIT
    assert FIT_COLUMNS == ["Optimizer Fit", "Periscope Fit"]

    html = build_review_page([_entry()], job_id="t", title="Test")

    for system in EXPECTED_HVAC:
        assert f'data-sys="{system}"' in html
    assert 'data-sys="None"' in html
    assert "c.dataset.sys==='None'" in html
    assert "Optimizer Fit:" in html
    assert "Periscope Fit:" in html
    assert 'data-col="optimizer_fit"' in html
    assert 'data-col="periscope_fit"' in html
    for fit in DUAL_FIT_OPTIONS:
        assert html.count(f'data-fit="{fit}"') == 2
    for host in MAP_HOSTS:
        assert host in html

    assert 'class="mbox gemini"' in html and "gemini box text" in html
    assert 'class="mbox grok"' in html and "grok box text" in html
    assert 'class="carousel"' in html and "nav prev" in html and "nav next" in html
    assert 'onclick="submitCard' in html
    assert 'id="submit-all"' in html and 'onclick="submitAll()"' in html
    assert "saved locally; Sheet update failed" in html
    assert "Sheet row not found" in html
    assert "hvac_systems:" in html
    assert "payload.optimizer_fit=fits.optimizer_fit" in html
    assert "payload.periscope_fit=fits.periscope_fit" in html
    assert "choose both Optimizer Fit and Periscope Fit" in html
    assert "both Optimizer Fit and Periscope Fit choices" in html


def test_positive_result_defaults_cooling_tower_and_optimizer_only():
    page = build_review_page([_entry()], job_id="positive")

    assert 'class="chip sel" data-sys="Cooling Tower"' in page
    assert 'class="fitchip sel" data-fit="Good"' in _fit_group(
        page, "optimizer_fit"
    )
    assert 'class="fitchip sel"' not in _fit_group(page, "periscope_fit")
    assert '<span id="done">0</span>/1 reviewed' in page


def test_possible_cooling_tower_uses_the_same_review_defaults():
    possible = {**_entry(), "verdict": "cooling_tower_possible"}
    page = build_review_page([possible], job_id="possible")

    assert 'class="chip sel" data-sys="Cooling Tower"' in page
    assert 'class="fitchip sel" data-fit="Good"' in _fit_group(
        page, "optimizer_fit"
    )
    assert 'class="fitchip sel"' not in _fit_group(page, "periscope_fit")


def test_saved_human_choices_override_positive_defaults():
    saved = {
        **_entry(),
        "human": {
            "hvac_systems": "RTU",
            "optimizer_fit": "Bad",
            "periscope_fit": "Good",
        },
    }
    page = build_review_page([saved], job_id="saved")

    assert 'class="chip sel" disabled data-sys="Cooling Tower"' not in page
    assert 'class="chip sel" disabled data-sys="RTU"' in page
    assert 'class="fitchip sel" disabled data-fit="Bad"' in _fit_group(
        page, "optimizer_fit"
    )
    assert 'class="fitchip sel" disabled data-fit="Good"' in _fit_group(
        page, "periscope_fit"
    )


def test_partial_human_choices_are_never_filled_by_ai_defaults():
    partial = {
        **_entry(),
        "human": {
            "hvac_systems": "RTU",
            "optimizer_fit": "",
            "periscope_fit": "Good",
        },
    }
    page = build_review_page([partial], job_id="partial")

    assert 'class="chip sel" data-sys="Cooling Tower"' not in page
    assert 'class="chip sel" data-sys="RTU"' in page
    assert 'class="fitchip sel"' not in _fit_group(page, "optimizer_fit")
    assert 'class="fitchip sel" data-fit="Good"' in _fit_group(
        page, "periscope_fit"
    )
    assert '<span id="done">0</span>/1 reviewed' in page


def test_negative_result_does_not_default_review_choices():
    negative = {**_entry(), "verdict": "not_detected"}
    page = build_review_page([negative], job_id="negative")

    assert 'class="chip sel"' not in page
    assert 'class="fitchip sel"' not in page


def test_positive_legacy_result_defaults_to_optimizer():
    page = build_review_page(
        [_entry()],
        job_id="legacy-positive",
        review_schema=SINGLE_FIT_SCHEMA,
    )
    assert 'class="chip sel" data-sys="Cooling Tower"' in page
    assert 'class="fitchip sel" data-fit="Optimizer"' in _fit_group(page, "fit")
    assert "a Fit choice" in page

    saved = {
        **_entry(),
        "human": {"hvac_systems": "RTU", "fit": "Periscope"},
    }
    saved_page = build_review_page(
        [saved],
        job_id="legacy-saved",
        review_schema=SINGLE_FIT_SCHEMA,
    )
    assert 'class="fitchip sel" disabled data-fit="Periscope"' in _fit_group(
        saved_page, "fit"
    )
    assert 'class="fitchip sel" disabled data-fit="Optimizer"' not in _fit_group(
        saved_page, "fit"
    )


def test_reviewed_cards_rehydrate():
    fresh = _entry()
    done = {
        **_entry(),
        "i": 2,
        "human": {
            "hvac_systems": "Cooling Tower, RTU",
            "optimizer_fit": "Good",
            "periscope_fit": "Bad",
            "note": "checked from roof photo",
        },
    }
    html = build_review_page([fresh, done], job_id="t", title="Test")

    assert html.count('class="card done"') == 1
    assert '<span id="done">1</span>/2 reviewed' in html
    assert "let done=1;" in html
    assert 'class="chip sel" disabled data-sys="RTU"' in html
    assert 'class="fitchip sel" disabled data-fit="Good"' in html
    assert 'class="fitchip sel" disabled data-fit="Bad"' in html
    assert 'value="checked from roof photo"' in html
    assert "Optimizer Fit: Good" in html
    assert "Periscope Fit: Bad" in html
    assert 'class="submit" onclick' in html


def test_legacy_single_contract_does_not_accept_dual_decision():
    partial = {
        **_entry(),
        "human": {
            "hvac_systems": "AHU, VRF",
            "optimizer_fit": "Bad",
            "periscope_fit": "Good",
            "note": "keep me",
        },
    }
    html = build_review_page(
        [partial],
        job_id="legacy-single",
        title="Test",
        review_schema=SINGLE_FIT_SCHEMA,
    )
    assert 'class="card done"' not in html
    assert '<span id="done">0</span>/1 reviewed' in html
    assert 'class="chip sel" data-sys="AHU"' in html
    assert 'class="chip sel" data-sys="VRF"' in html
    assert 'value="keep me"' in html
    assert "HVAC saved; choose Fit to complete" in html


def test_current_dual_batch_is_complete_only_with_both_products():
    done = {
        **_entry(),
        "human": {
            "hvac_systems": "Cooling Tower",
            "optimizer_fit": "Good",
            "periscope_fit": "Bad",
        },
    }
    html = build_review_page(
        [done],
        job_id="current",
        review_schema=DUAL_FIT_SCHEMA,
    )
    assert "Optimizer Fit:" in html and "Periscope Fit:" in html
    assert html.count('data-fit="Good"') == 2
    assert html.count('data-fit="Bad"') == 2
    assert '<span id="done">1</span>/1 reviewed' in html


def test_legacy_single_batch_stays_compatible():
    done = {
        **_entry(),
        "human": {
            "hvac_systems": "Cooling Tower",
            "fit": "Optimizer",
        },
    }
    html = build_review_page(
        [done],
        job_id="legacy-single",
        review_schema=SINGLE_FIT_SCHEMA,
    )
    assert '<div class="label">Fit:</div>' in html
    assert "Optimizer Fit:" not in html
    assert '<span id="done">1</span>/1 reviewed' in html


if __name__ == "__main__":
    test_review_contract()
    test_positive_result_defaults_cooling_tower_and_optimizer_only()
    test_possible_cooling_tower_uses_the_same_review_defaults()
    test_saved_human_choices_override_positive_defaults()
    test_partial_human_choices_are_never_filled_by_ai_defaults()
    test_negative_result_does_not_default_review_choices()
    test_positive_legacy_result_defaults_to_optimizer()
    test_reviewed_cards_rehydrate()
    test_legacy_single_contract_does_not_accept_dual_decision()
    test_current_dual_batch_is_complete_only_with_both_products()
    test_legacy_single_batch_stays_compatible()
    print("OK: dual-product review contract and single-Fit compatibility hold.")
