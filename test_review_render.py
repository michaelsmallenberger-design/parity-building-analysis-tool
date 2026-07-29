"""No-spend contract guards for the interactive review page."""
from review_contract import DUAL_FIT_SCHEMA
from review_render import build_review_page, HVAC_SYSTEMS, FIT_OPTIONS, FIT_COLUMNS


EXPECTED_HVAC = [
    "Cooling Tower", "Chiller", "Exhaust Fan", "RTU", "AHU", "PTAC",
    "Fan Coil", "Heat Pump", "VRF",
]
EXPECTED_FIT = ["Optimizer", "Periscope", "Unclear", "Bad"]
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


def test_review_contract():
    assert HVAC_SYSTEMS == EXPECTED_HVAC
    assert FIT_OPTIONS == EXPECTED_FIT
    assert FIT_COLUMNS == ["Fit"]

    html = build_review_page([_entry()], job_id="t", title="Test")

    for system in EXPECTED_HVAC:
        assert f'data-sys="{system}"' in html
    assert 'data-sys="None"' in html
    assert "c.dataset.sys==='None'" in html
    assert '<div class="label">Fit:</div>' in html
    assert 'data-col="fit"' in html
    assert "Optimizer Fit:" not in html
    assert "Periscope Fit:" not in html
    for fit in EXPECTED_FIT:
        assert html.count(f'data-fit="{fit}"') == 1
    for host in MAP_HOSTS:
        assert host in html

    assert 'class="mbox gemini"' in html and "gemini box text" in html
    assert 'class="mbox grok"' in html and "grok box text" in html
    assert 'class="carousel"' in html and "nav prev" in html and "nav next" in html
    assert 'onclick="submitCard' in html
    assert 'id="submit-all"' in html and 'onclick="submitAll()"' in html
    assert "saved locally; Sheet update failed" in html
    assert "Sheet row not found" in html
    assert "hvac_systems:" in html and "payload.fit=fits.fit" in html
    assert "choose one Fit: Optimizer, Periscope, Unclear, or Bad" in html


def test_reviewed_cards_rehydrate():
    fresh = _entry()
    done = {
        **_entry(),
        "i": 2,
        "human": {
            "hvac_systems": "Cooling Tower, RTU",
            "fit": "Optimizer",
            "note": "checked from roof photo",
        },
    }
    html = build_review_page([fresh, done], job_id="t", title="Test")

    assert html.count('class="card done"') == 1
    assert '<span id="done">1</span>/2 reviewed' in html
    assert "let done=1;" in html
    assert 'class="chip sel" disabled data-sys="RTU"' in html
    assert 'class="fitchip sel" disabled data-fit="Optimizer"' in html
    assert 'value="checked from roof photo"' in html
    assert "✓ saved: Cooling Tower, RTU · Fit: Optimizer" in html
    assert 'class="submit" onclick' in html


def test_incompatible_dual_decision_keeps_hvac_but_requires_fit():
    partial = {
        **_entry(),
        "human": {
            "hvac_systems": "AHU, VRF",
            "optimizer_fit": "Bad",
            "periscope_fit": "Good",
            "note": "keep me",
        },
    }
    html = build_review_page([partial], job_id="t", title="Test")
    assert 'class="card done"' not in html
    assert '<span id="done">0</span>/1 reviewed' in html
    assert 'class="chip sel" data-sys="AHU"' in html
    assert 'class="chip sel" data-sys="VRF"' in html
    assert 'value="keep me"' in html
    assert "HVAC saved; choose Fit to complete" in html


def test_true_historical_dual_batch_stays_compatible():
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
        job_id="legacy",
        review_schema=DUAL_FIT_SCHEMA,
    )
    assert "Optimizer Fit:" in html and "Periscope Fit:" in html
    assert html.count('data-fit="Good"') == 2
    assert html.count('data-fit="Bad"') == 2
    assert '<span id="done">1</span>/1 reviewed' in html


if __name__ == "__main__":
    test_review_contract()
    test_reviewed_cards_rehydrate()
    test_incompatible_dual_decision_keeps_hvac_but_requires_fit()
    test_true_historical_dual_batch_stays_compatible()
    print("OK: single-Fit review contract and historical compatibility hold.")
