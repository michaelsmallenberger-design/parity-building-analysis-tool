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
    assert DUAL_FIT_OPTIONS == [
        "Customer", "Good", "Okay", "Bad", "Not Sure",
    ]

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
    assert html.count(
        'data-fit="Okay" onclick="pickFit(this)">Maybe</button>'
    ) == 2
    assert 'data-fit="Okay" onclick="pickFit(this)">Okay</button>' not in html
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


def test_carousel_only_sources_first_image_until_navigation():
    page = build_review_page([_entry()], job_id="lazy-images")

    assert len(re.findall(r'<img src="data:image/jpeg;base64,AAAA"', page)) == 1
    assert len(re.findall(r'<img data-src="data:image/jpeg;base64,AAAA"', page)) == 2
    assert page.count('decoding="async"') == 3
    assert (
        "if(!current.getAttribute('src')&&current.dataset.src)"
        "current.src=current.dataset.src"
    ) in page


def test_review_card_shows_stories_and_alternate_exterior_view():
    entry = {
        **_entry(),
        "source_context": {
            "stories": "14",
            "building_info": {
                "property_name": "Synthetic Tower",
                "units": "250",
                "year_built": "1988",
                "private_notes": "must never render",
            },
        },
        "result_image_url_streetview_context": (
            "https://images.example/alternate-exterior.jpg"
        ),
    }
    page = build_review_page([entry], job_id="source-context")

    assert (
        '<span class="context-item">Sheet Stories/Floors: '
        '<strong>14</strong></span>'
    ) in page
    assert "Other building information from Sheet" in page
    assert "Property name" in page and "Synthetic Tower" in page
    assert "Units" in page and "250" in page
    assert "Year built" in page and "1988" in page
    assert "private_notes" not in page and "must never render" not in page
    assert 'data-label="Exterior / address"' in page
    assert 'data-label="Exterior / alternate angle"' in page
    assert "alternate-exterior.jpg" in page


def test_review_card_explains_when_no_exterior_view_is_available():
    entry = _entry()
    entry.pop("result_image_url_streetview")
    page = build_review_page([entry], job_id="no-exterior")

    assert "No exterior view available" in page
    assert "Use the map links below" in page

    with_exterior = build_review_page([_entry()], job_id="has-exterior")
    assert "No exterior view available" not in with_exterior


def test_server_pagination_limits_dom_and_keeps_global_tab_progress():
    entries = []
    for number in range(1, 16):
        entry = {
            **_entry(),
            "i": number,
            "row_id": f"row-{number}",
            "address": f"{number} Pagination Test Ave",
            "source_tab": "DC" if number <= 7 else "NYC",
            "source_row": number + 1,
            "result_image_url": f"https://images.example/image-{number:03d}-primary.jpg",
            "result_image_url_wide": f"https://images.example/image-{number:03d}-wide.jpg",
            "result_image_url_streetview": f"https://images.example/image-{number:03d}-street.jpg",
        }
        entries.append(entry)

    page = build_review_page(
        entries,
        job_id="paged",
        page=2,
        page_size=5,
        page_url="/review/paged?mode=compact&page=99",
    )

    assert page.count("<article ") == 5
    for number in range(6, 11):
        assert f"{number} Pagination Test Ave" in page
    assert "5 Pagination Test Ave" not in page
    assert "11 Pagination Test Ave" not in page
    assert "image-005-primary.jpg" not in page
    assert "image-011-primary.jpg" not in page
    assert len(re.findall(r'<img src="https://images\.example/', page)) == 5
    assert len(re.findall(r'<img data-src="https://images\.example/', page)) == 10

    assert '<span id="done">0</span>/15 reviewed' in page
    assert (
        'data-total="7" data-reviewed="0">7/7 analyzed '
        '· 0/7 reviewed · 2 on this page'
    ) in page
    assert (
        'data-total="8" data-reviewed="0">8/8 analyzed '
        '· 0/8 reviewed · 3 on this page'
    ) in page
    assert "Buildings 6–10 of 15 · page 2 of 3" in page
    assert 'href="/review/paged?mode=compact&amp;page=1"' in page
    assert 'href="/review/paged?mode=compact&amp;page=3"' in page
    assert page.count('class="pagination"') == 2
    assert "Submit all completed on this page" in page
    assert "const totalInGroup=+progress.dataset.total" in page


def test_pagination_is_opt_in_for_legacy_callers():
    entries = [{**_entry(), "i": number, "address": f"Legacy {number}"} for number in range(12)]
    page = build_review_page(entries, job_id="legacy-unpaged")

    assert page.count("<article ") == 12
    assert 'class="pagination"' not in page
    assert "Submit all completed on this page" not in page
    assert "Submit all completed</button>" in page


def test_unresolved_rows_are_appended_with_reasons_and_links():
    completed = {
        **_entry(),
        "row_id": "g1:r2",
        "source_tab": "DC",
        "source_row": 2,
        "address": "1 Completed Ave",
        "human": {
            "hvac_systems": "None",
            "optimizer_fit": "Bad",
            "periscope_fit": "Bad",
        },
    }
    pending = {
        **_entry(),
        "row_id": "g1:r3",
        "source_tab": "DC",
        "source_row": 3,
        "address": "2 Pending Ave",
    }
    missing = {
        **_entry(),
        "row_id": "g1:r4",
        "source_tab": "DC",
        "source_row": 4,
        "address": "3 Missing Ave",
        "verdict": "needs_review",
        "error": "No analysis result was saved for this source row",
    }
    gated = {
        **_entry(),
        "row_id": "g1:r5",
        "source_tab": "DC",
        "source_row": 5,
        "address": "4 Gated Ave",
        "verdict": "likely_residential",
        "reasoning": "",
        "gemini_reasoning": "",
        "notes": "Footprint size gate requires manual verification",
    }
    page = build_review_page(
        [missing, pending, completed, gated],
        job_id="unresolved-order",
    )

    assert page.index("1 Completed Ave") < page.index("2 Pending Ave")
    assert page.index("2 Pending Ave") < page.index("3 Missing Ave")
    assert page.index("3 Missing Ave") < page.index("4 Gated Ave")
    assert '<section class="review-state-group pending">' in page
    assert '<section class="review-state-group attention">' in page
    assert "Analysis is complete, but this row has not been submitted" in page
    assert "No analysis result was saved for this source row" in page
    assert "Footprint size gate requires manual verification" in page
    assert "Tab: DC · row 4" in page
    assert page.count("google.com/maps") == 4
    assert page.count("earth.google.com") == 4
    assert page.count("bing.com/maps") == 4
    assert '<span id="pending-count">1</span> still need review' in page
    assert '<span id="attention-count">2</span> need attention' in page
    assert "async function submitCard(btn,reloadAfterSave=true)" in page
    assert "if(reloadAfterSave)window.location.reload();" in page
    assert "submitCard(card.querySelector('.submit'),false)" in page
    assert "if(saved)window.location.reload();" in page


def test_human_disposition_resolves_machine_attention_but_not_writeback_error():
    human = {
        "hvac_systems": "None",
        "optimizer_fit": "Bad",
        "periscope_fit": "Bad",
    }
    dispositioned = {
        **_entry(),
        "row_id": "g1:r10",
        "address": "10 Dispositioned Ave",
        "verdict": "needs_review",
        "error": "Visual result required human confirmation",
        "human": human,
        "writeback": {"status": "updated"},
    }
    writeback_failed = {
        **_entry(),
        "row_id": "g1:r11",
        "address": "11 Writeback Failed Ave",
        "verdict": "needs_review",
        "error": "Visual result required human confirmation",
        "human": human,
        "writeback": {
            "status": "error",
            "error": "Source row could not be updated",
        },
    }
    page = build_review_page(
        [writeback_failed, dispositioned],
        job_id="reviewed-attention",
    )

    dispositioned_card = re.search(
        r'<article class="card done"[^>]*data-review-state="reviewed"'
        r'[^>]*data-addr="10 Dispositioned Ave"',
        page,
    )
    assert dispositioned_card
    failed_card = re.search(
        r'<article class="card done writeback-error attention"'
        r'[^>]*data-review-state="attention"'
        r'[^>]*data-addr="11 Writeback Failed Ave"',
        page,
    )
    assert failed_card
    assert page.index("10 Dispositioned Ave") < page.index(
        "11 Writeback Failed Ave"
    )
    assert "Review saved locally, but the Sheet was not updated" in page
    assert '<span id="attention-count">1</span> need attention' in page
    assert "card.classList.remove('writeback-error','pending','attention')" in page
    assert "const issue=card.querySelector('.issue');if(issue)issue.remove()" in page
    assert "card.dataset.reviewState='reviewed'" in page


def test_191_building_page_stays_bounded():
    entries = [
        {
            **_entry(),
            "i": number,
            "row_id": f"large-{number}",
            "address": f"Pagination Large {number}",
            "result_image_url": f"https://images.example/{number}-primary.jpg",
            "result_image_url_wide": f"https://images.example/{number}-wide.jpg",
            "result_image_url_streetview": f"https://images.example/{number}-street.jpg",
        }
        for number in range(1, 192)
    ]
    page = build_review_page(
        entries,
        job_id="large",
        page=1,
        page_size=10,
        page_url="/review/large",
    )

    assert page.count("<article ") == 10
    assert "Pagination Large 10" in page
    assert "Pagination Large 11" not in page
    assert len(page.encode("utf-8")) < 100_000
    assert len(re.findall(r'<img src="https://images\.example/', page)) == 10
    assert len(re.findall(r'<img data-src="https://images\.example/', page)) == 20


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


def test_saved_okay_choice_is_presented_as_maybe_without_changing_canonical_value():
    saved = {
        **_entry(),
        "human": {
            "hvac_systems": "RTU",
            "optimizer_fit": "Okay",
            "periscope_fit": "Bad",
        },
    }
    page = build_review_page([saved], job_id="saved-maybe")

    assert (
        'class="fitchip sel" disabled data-fit="Okay" '
        'onclick="pickFit(this)">Maybe</button>'
        in _fit_group(page, "optimizer_fit")
    )
    assert "Optimizer Fit: Maybe" in page


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
    test_carousel_only_sources_first_image_until_navigation()
    test_review_card_shows_stories_and_alternate_exterior_view()
    test_review_card_explains_when_no_exterior_view_is_available()
    test_server_pagination_limits_dom_and_keeps_global_tab_progress()
    test_pagination_is_opt_in_for_legacy_callers()
    test_unresolved_rows_are_appended_with_reasons_and_links()
    test_human_disposition_resolves_machine_attention_but_not_writeback_error()
    test_191_building_page_stays_bounded()
    test_positive_result_defaults_cooling_tower_and_optimizer_only()
    test_possible_cooling_tower_uses_the_same_review_defaults()
    test_saved_human_choices_override_positive_defaults()
    test_saved_okay_choice_is_presented_as_maybe_without_changing_canonical_value()
    test_partial_human_choices_are_never_filled_by_ai_defaults()
    test_negative_result_does_not_default_review_choices()
    test_positive_legacy_result_defaults_to_optimizer()
    test_reviewed_cards_rehydrate()
    test_legacy_single_contract_does_not_accept_dual_decision()
    test_current_dual_batch_is_complete_only_with_both_products()
    test_legacy_single_batch_stays_compatible()
    print("OK: dual-product review contract and single-Fit compatibility hold.")
