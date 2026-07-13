"""Contract guard for the review page (review_render.build_review_page).

This output is the BLESSED review surface — locked by user directive: "I want this
every single time, make sure there's no chance this isn't the output every time."
Any change that drops a required element MUST fail this test. Run before deploy.

Guarantees every review page contains:
  - the full 9-item HVAC Systems taxonomy from the Washington Gas sheet
  - the 4 Fit options (Optimizer / Periscope / Unclear / Bad)
  - all three map services (Google Maps, Google Earth, Bing Maps)
  - the split model reasoning boxes (Gemini = green, Grok = black)
  - the ‹ › image carousel
  - the HVAC multi-select, single-select Fit, note, and Submit controls
"""
from review_render import build_review_page, HVAC_SYSTEMS, FIT_OPTIONS

EXPECTED_HVAC = ["Cooling Tower", "Chiller", "Exhaust Fan", "RTU", "AHU", "PTAC", "Fan Coil", "Heat Pump", "VRF"]
EXPECTED_FIT = ["Optimizer", "Periscope", "Unclear", "Bad"]
MAP_HOSTS = ["google.com/maps", "earth.google.com", "bing.com/maps"]


def _entry():
    img = "data:image/jpeg;base64,AAAA"
    return {
        "i": 1, "address": "1 Test St, Somewhere, ST 10001", "verdict": "confirmed",
        "reasoning": "combined consensus text",
        "gemini_verdict": "confirmed", "gemini_confidence": 0.9, "gemini_reasoning": "gemini box text",
        "grok_verdict": "likely", "grok_confidence": 0.8, "grok_reasoning": "grok box text",
        "result_image_url": img, "result_image_url_wide": img, "result_image_url_streetview": img,
    }


def test_review_contract():
    assert HVAC_SYSTEMS == EXPECTED_HVAC, f"HVAC taxonomy drifted: {HVAC_SYSTEMS}"
    assert FIT_OPTIONS == EXPECTED_FIT, f"Fit options drifted: {FIT_OPTIONS}"

    html = build_review_page([_entry()], job_id="t", title="Test")

    for s in EXPECTED_HVAC:
        assert f'data-sys="{s}"' in html, f"missing HVAC system chip: {s}"
    assert 'data-sys="None"' in html, "missing the None chip (reviewer sees no HVAC)"
    assert "c.dataset.sys==='None'" in html, "None chip must be mutually exclusive with system chips"
    for f in EXPECTED_FIT:
        assert f'data-fit="{f}"' in html, f"missing fit option: {f}"
    for host in MAP_HOSTS:
        assert host in html, f"missing map link: {host}"

    assert 'class="mbox gemini"' in html and "gemini box text" in html, "missing Gemini (green) box"
    assert 'class="mbox grok"' in html and "grok box text" in html, "missing Grok (black) box"
    assert 'class="carousel"' in html and "nav prev" in html and "nav next" in html, "missing image carousel"

    assert 'onclick="submitCard' in html, "missing Submit"
    assert "hvac_systems:" in html and "fit:" in html, "payload must carry hvac_systems + fit"
    print("OK: review-page contract holds — 9 HVAC systems, 4 fit options, 3 map services, "
          "Gemini/Grok split boxes, carousel, and full controls.")


def test_reviewed_cards_rehydrate():
    """Reopening the page must show already-reviewed cards as done (green card,
    picked chips selected+disabled, note prefilled, counter counts them)."""
    fresh = _entry()
    done = {**_entry(), "i": 2,
            "human": {"hvac_systems": "Cooling Tower, RTU", "fit": "Optimizer",
                      "note": "checked from roof photo"}}
    html = build_review_page([fresh, done], job_id="t", title="Test")

    assert html.count('class="card done"') == 1, "reviewed card must render pre-done"
    assert '<span id="done">1</span>/2 reviewed' in html, "counter must include prior decisions"
    assert "let done=1;" in html, "JS counter must start at prior-decision count"
    assert 'class="chip sel" disabled data-sys="RTU"' in html, "picked chip must be selected+disabled"
    assert 'class="fitchip sel" disabled data-fit="Optimizer"' in html, "picked fit must be selected+disabled"
    assert 'value="checked from roof photo"' in html, "note must prefill"
    assert "✓ saved: Cooling Tower, RTU · Optimizer" in html, "saved status must render"
    # The fresh card must be untouched: its Submit stays enabled.
    assert 'class="submit" onclick' in html, "unreviewed card lost its live Submit"
    print("OK: reviewed cards rehydrate as done; fresh cards unaffected.")


if __name__ == "__main__":
    test_review_contract()
    test_reviewed_cards_rehydrate()
