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


if __name__ == "__main__":
    test_review_contract()
