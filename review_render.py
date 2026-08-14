"""Interactive review page with imagery, HVAC, and product-fit decisions.

Current workbooks review Optimizer and Periscope independently. Previously
persisted single-Fit batches keep their versioned interface.
"""
import html as _html
import json
import urllib.parse

from failure_diagnostics import sanitize_failure_text
from review_contract import (
    CURRENT_REVIEW_SCHEMA,
    DUAL_FIT_COLUMNS,
    FIT_OPTIONS,
    MACHINE_ATTENTION_VERDICTS,
    SINGLE_FIT_SCHEMA,
    batch_primary_review_complete,
    entry_has_machine_attention,
    entry_needs_alex_review,
    entry_is_reviewed,
    human_review_version,
    fit_options_for_schema,
    is_dual_fit_schema,
)

# Exact HVAC Systems data-validation dropdown from the Washington Gas sheet
# (Good fits / All Buildings / Building Analysis / Tracker), in the sheet's order.
# NOTE: 'Unclear' is NOT here — in the sheet that belongs to the separate fit/status
# column (Optimizer/Periscope/Unclear/Bad), not the HVAC Systems column.
HVAC_SYSTEMS = ["Cooling Tower", "Chiller", "Exhaust Fan", "RTU", "AHU", "PTAC", "Fan Coil", "Heat Pump", "VRF"]
# Reviewers sometimes see no relevant HVAC at all; "None" is a submittable answer,
# mutually exclusive with the systems above. Not part of the taxonomy itself.
NONE_OPTION = "None"
# Current workbooks use one independently reviewed column per product.
FIT_COLUMNS = DUAL_FIT_COLUMNS
# Compatibility export used by upload schema detection.
LEGACY_FIT_VALUES = FIT_OPTIONS
# AI verdicts that mean "cooling tower present" -> pre-check Cooling Tower for the reviewer.
_AI_POSITIVE = {
    "confirmed",
    "registry_confirmed",
    "likely",
    "cooling_tower_present",
    "cooling_tower_possible",
}
_IMG_SLOTS = [
    ("result_image_url", "Aerial (detections)"),
    ("result_image_url_wide", "Wide / context"),
    ("result_image_url_streetview", "Exterior / address"),
    ("result_image_url_streetview_context", "Exterior / alternate angle"),
]
_EXTERIOR_IMAGE_KEYS = {
    "result_image_url_streetview",
    "result_image_url_streetview_context",
}
_SHEET_BUILDING_INFO_FIELDS = (
    ("property_name", "Property name"),
    ("property_type", "Property type"),
    ("secondary_type", "Secondary type"),
    ("building_class", "Building class"),
    ("construction_type", "Construction type"),
    ("units", "Units"),
    ("total_buildings", "Total buildings"),
    ("square_feet", "Square feet"),
    ("year_built", "Year built"),
    ("year_renovated", "Year renovated"),
    ("affordable_type", "Affordable type"),
    ("city", "City"),
    ("market_name", "Market"),
)
VERDICT_COLOR = {
    "confirmed": "#37b24d", "registry_confirmed": "#37b24d", "likely": "#94d82d",
    "neighbor_only": "#f59f00", "needs_review": "#f59f00",
    "likely_residential": "#868e96", "not_detected": "#e8590c", "": "#868e96",
}

# Keep the legacy Sheet/API value ``Okay`` for old batches while giving
# reviewers the requested ``Maybe`` label. New batches store ``Maybe`` itself.
_DUAL_FIT_DISPLAY_LABELS = {"Okay": "Maybe"}


def _fit_display_label(value, review_schema):
    value = str(value or "")
    if is_dual_fit_schema(review_schema):
        return _DUAL_FIT_DISPLAY_LABELS.get(value, value)
    return value


def _valid_image_source(value):
    return isinstance(value, str) and value.startswith(
        ("data:", "/files/", "http")
    )


_REVIEW_STATE_ORDER = ("reviewed", "pending", "attention")
_REVIEW_STATE_LABELS = {
    "alex_review": "Needs Alex Review",
    "reviewed": "Completed reviews",
    "pending": "Still needs review",
    "attention": "Needs attention",
}


def _fmt_conf(c):
    try:
        return f"({float(c) * 100:.0f}%)"
    except (TypeError, ValueError):
        return ""


def _model_boxes(e, reasoning):
    """Show Gemini and only show Grok when the emergency fallback actually ran."""
    gv = _html.escape(str(e.get("gemini_verdict") or "").strip())
    gr = _html.escape(str(e.get("gemini_reasoning") or "").strip())
    kv = _html.escape(str(e.get("grok_verdict") or "").strip())
    kr = _html.escape(str(e.get("grok_reasoning") or "").strip())
    if not (gr or kr):
        return f'<p class="combined">{reasoning}</p>'
    out = ""
    if gr or gv:
        out += (f'<div class="mbox gemini"><div class="mh">Gemini · {gv or "—"} '
                f'{_fmt_conf(e.get("gemini_confidence"))}</div>{gr or "(no detail)"}</div>')
    if kr or kv:
        out += (f'<div class="mbox grok"><div class="mh">Grok · {kv or "—"} '
                f'{_fmt_conf(e.get("grok_confidence"))}</div>{kr or "(no detail)"}</div>')
    path = _html.escape(str(e.get("model_path") or "").replace("_", " "))
    if path:
        out += f'<div class="model-path">Model path: {path}</div>'
    return out


def _machine_attention_reason(e):
    """Return a safe, operator-facing reason when a row is not review-ready."""
    writeback = e.get("writeback") or {}
    if writeback.get("status") == "error":
        detail = sanitize_failure_text(
            writeback.get("error") or "Google Sheet write-back failed",
            limit=240,
        )
        return f"Review saved locally, but the Sheet was not updated: {detail}"
    if writeback.get("status") == "pending":
        return (
            "Review is saved locally, but its Sheet update is still pending. "
            "Retry the write-back before continuing."
        )

    error = str(e.get("error") or "").strip()
    if error:
        return sanitize_failure_text(error, limit=240)

    address = str(e.get("address") or "").strip()
    if not address:
        return "No usable address was available for this source row."

    verdict = str(e.get("verdict") or "").strip().casefold()
    if not verdict:
        return "No machine verdict was produced for this address."
    if verdict in MACHINE_ATTENTION_VERDICTS:
        detail = str(e.get("notes") or e.get("reasoning") or "").strip()
        if detail:
            return sanitize_failure_text(detail, limit=240)
        return {
            "ambiguous_footprint": (
                "The address could not be matched confidently to one building footprint."
            ),
            "likely_residential": (
                "The footprint size gate skipped visual model verification; "
                "manual confirmation is required."
            ),
            "needs_review": "The machine result requires manual confirmation.",
        }[verdict]
    return ""


def _review_state(e, review_schema, alex_review_enabled=False):
    """Classify without mutating persisted entry order or human decisions."""
    writeback_status = (e.get("writeback") or {}).get("status")
    if writeback_status == "error" or (
        alex_review_enabled
        and writeback_status == "pending"
        and str((e.get("human") or {}).get("review_stage") or "") == "secondary"
    ):
        return "attention", _machine_attention_reason(e)
    if entry_is_reviewed(e, review_schema):
        return "reviewed", ""
    if entry_has_machine_attention(e):
        return "attention", _machine_attention_reason(e)
    return (
        "pending",
        "Analysis is complete, but this row has not been submitted to the Sheet.",
    )


def _card(
    e,
    review_schema=CURRENT_REVIEW_SCHEMA,
    review_state=None,
    issue_reason="",
    alex_review_enabled=False,
):
    # A batch entry carries a "human" decision once reviewed (review_store); render
    # such cards in the exact state a live Submit leaves them in, so reopening the
    # page mid-batch shows what's already done.
    human = e.get("human") or {}
    reviewed = entry_is_reviewed(e, review_schema)
    writeback = e.get("writeback") or {}
    writeback_error = writeback.get("status") == "error"
    writeback_pending = writeback.get("status") == "pending"
    writeback_problem = writeback_error or writeback_pending
    review_state = review_state or _review_state(
        e, review_schema, alex_review_enabled
    )[0]
    review_stage = str(human.get("review_stage") or ("primary" if human else ""))
    review_version = human_review_version(e)
    secondary_writeback = bool(
        reviewed and writeback_problem and review_stage == "secondary"
    )
    secondary_retry = bool(
        alex_review_enabled
        and secondary_writeback
        and str((e.get("secondary_review") or {}).get("action") or "") == "revise"
    )
    alex_review = review_state == "alex_review"
    picked = {s.strip() for s in str(human.get("hvac_systems", "")).split(",") if s.strip()}
    rid = _html.escape(str(
        e.get("row_id") if e.get("row_id") is not None else e.get("i", "")
    ))
    addr = str(e.get("address", "")).strip()
    addr_display = addr or "(address unavailable)"
    addr_e = _html.escape(addr_display)
    addr_attr = _html.escape(addr, quote=True)
    ai = str(e.get("verdict") or "")
    ai_positive = ai.strip().casefold() in _AI_POSITIVE
    ai_color = VERDICT_COLOR.get(ai, "#868e96")
    reasoning = _html.escape(str(e.get("reasoning") or "") or "(no model reasoning)")
    q = urllib.parse.quote(addr)
    gmaps = f"https://www.google.com/maps/search/?api=1&query={q}"
    gearth = f"https://earth.google.com/web/search/{q}"
    bing = f"https://www.bing.com/maps?q={q}&style=a"

    slides = ""
    n = 0
    has_exterior = False
    for key, label in _IMG_SLOTS:
        v = e.get(key)
        # data: URIs (API path) or server-hosted /files/ paths & absolute URLs
        # (browser-upload worker path) — all render in the same carousel.
        if _valid_image_source(v):
            safe_v = _html.escape(v, quote=True)
            source_attr = f'src="{safe_v}"' if not n else f'data-src="{safe_v}"'
            slides += (
                f'<img {source_attr} data-label="{label}" alt="{label}" '
                f'loading="lazy" decoding="async" onclick="zoom(this.src)"'
                f'{"" if n else " class=cur"}>'
            )
            n += 1
            if key in _EXTERIOR_IMAGE_KEYS:
                has_exterior = True
    if not n:
        carousel = '<div class="noimg">no imagery</div>'
    else:
        arrows = ('<button type="button" class="nav prev" onclick="nav(this,-1)">‹</button>'
                  '<button type="button" class="nav next" onclick="nav(this,1)">›</button>') if n > 1 else ''
        carousel = (f'<div class="carousel" data-n="{n}" data-i="0">{arrows}'
                    f'<div class="frame">{slides}</div>'
                    f'<div class="cap"></div></div>')
    exterior_status = ""
    if not has_exterior:
        exterior_status = (
            '<div class="exterior-status missing"><strong>No exterior view '
            'available.</strong> Use the map links below to verify the building.'
            '</div>'
        )

    hvac_dis = " disabled" if reviewed else ""
    fit_dis = " disabled" if reviewed else ""
    note_dis = " disabled" if reviewed else ""
    submit_dis = " disabled" if reviewed and not writeback_problem else ""
    chips = ""
    for s in HVAC_SYSTEMS + [NONE_OPTION]:
        if human:
            pre = " sel" if s in picked else ""
        else:
            pre = " sel" if (s == "Cooling Tower" and ai_positive) else ""
        chips += f'<button type="button" class="chip{pre}"{hvac_dis} data-sys="{_html.escape(s)}" onclick="toggle(this)">{_html.escape(s)}</button>'
    fitrows = ""
    if is_dual_fit_schema(review_schema):
        dual_options = fit_options_for_schema(review_schema)
        fit_specs = [
            ("Optimizer Fit", "optimizer_fit", dual_options),
            ("Periscope Fit", "periscope_fit", dual_options),
        ]
    else:
        fit_specs = [("Fit", "fit", FIT_OPTIONS)]
    for col, fit_key, fit_options in fit_specs:
        picked_fit = str(human.get(fit_key) or "")
        if not human and ai_positive:
            if is_dual_fit_schema(review_schema) and fit_key == "optimizer_fit":
                picked_fit = "Good"
            elif review_schema == SINGLE_FIT_SCHEMA and fit_key == "fit":
                picked_fit = "Optimizer"
        chips_f = ""
        for fo in fit_options:
            pre = " sel" if fo == picked_fit else ""
            chips_f += (f'<button type="button" class="fitchip{pre}"{fit_dis} '
                        f'data-fit="{_html.escape(fo)}" onclick="pickFit(this)">'
                        f'{_html.escape(_fit_display_label(fo, review_schema))}</button>')
        fitrows += (f'<div class="label">{_html.escape(col)}:</div>'
                    f'<div class="fitchips" data-col="{fit_key}">{chips_f}</div>')

    if writeback_problem:
        status = (
            '<span class="status err">saved locally; Sheet write-back '
            + ("pending" if writeback_pending else "failed")
            + ' '
            '— retry</span>'
        )
    elif reviewed:
        saved = "✓ saved: " + str(human.get("hvac_systems", ""))
        for col, fit_key, _fit_options in fit_specs:
            v = human.get(fit_key)
            if v:
                saved += f" · {col}: {_fit_display_label(v, review_schema)}"
        status = f'<span class="status ok">{_html.escape(saved)}</span>'
    elif human and review_schema == SINGLE_FIT_SCHEMA:
        status = '<span class="status err">HVAC saved; choose Fit to complete</span>'
    else:
        status = '<span class="status"></span>'

    source = ""
    if e.get("source_tab"):
        source = (
            f'<div class="source">Tab: {_html.escape(str(e["source_tab"]))} '
            f'· row {_html.escape(str(e.get("source_row") or ""))}</div>'
        )
    source_context = e.get("source_context") or {}
    stories = str(source_context.get("stories") or "").strip()
    context = ""
    if stories:
        context = (
            '<div class="source-context">'
            f'<span class="context-item">Sheet Stories/Floors: <strong>'
            f'{_html.escape(stories)}</strong></span></div>'
        )
    sheet_details = ""
    building_info = source_context.get("building_info") or {}
    if isinstance(building_info, dict):
        info_rows = []
        for key, label in _SHEET_BUILDING_INFO_FIELDS:
            value = str(building_info.get(key) or "").strip()
            if not value:
                continue
            info_rows.append(
                '<div class="sheet-info-row">'
                f'<dt>{_html.escape(label)}</dt>'
                f'<dd>{_html.escape(value[:160])}</dd></div>'
            )
        if info_rows:
            sheet_details = (
                '<details class="sheet-details"><summary>'
                'Other building information from Sheet</summary>'
                f'<dl>{"".join(info_rows)}</dl></details>'
            )
    button_text = "Retry Sheet write-back" if writeback_problem else "Submit"
    state_class = (
        f" {review_state}"
        if review_state in {"alex_review", "pending", "attention"}
        else ""
    )
    issue = ""
    if issue_reason:
        issue_label = (
            "Needs attention"
            if review_state == "attention"
            else "Still needs review"
        )
        issue = (
            f'<div class="issue {review_state}"><strong>{issue_label}:</strong> '
            f'{_html.escape(issue_reason)}</div>'
        )
    if alex_review:
        actions = '''
    <button type="button" class="alex-unlock" onclick="beginAlexReview(this)">Review qualification</button>
    <div class="alex-actions" hidden>
      <button type="button" class="submit alex-save" onclick="submitSecondary(this,'revise')">Save Alex review</button>
      <button type="button" class="alex-confirm" onclick="submitSecondary(this,'confirm_uncertain')">Confirm current qualification</button>
      <button type="button" class="alex-cancel" onclick="cancelAlexReview(this)">Cancel</button>
    </div>'''
    elif secondary_retry:
        actions = (
            '<button type="button" class="submit secondary-retry" '
            'onclick="submitSecondary(this,\'revise\')">'
            f'{button_text}</button>'
        )
    elif secondary_writeback:
        actions = (
            '<button type="button" class="submit" disabled>'
            'Enable Alex review to retry Sheet write-back</button>'
        )
    else:
        actions = (
            f'<button type="button" class="submit"{submit_dis} '
            f'onclick="submitCard(this)">{button_text}</button>'
        )

    return f'''
<article class="card{' done' if reviewed else ''}{' writeback-error' if writeback_problem else ''}{state_class}" data-rid="{rid}" data-reviewed="{'1' if reviewed else '0'}" data-review-state="{review_state}" data-review-stage="{_html.escape(review_stage)}" data-review-version="{review_version}" data-addr="{addr_attr}" data-ai="{_html.escape(ai)}">
  <div class="head">
    <div><div class="addr">{addr_e}</div>{source}{context}</div>
    <div class="ai">model: <span class="badge" style="background:{ai_color}">{_html.escape(ai) or '—'}</span></div>
  </div>
  {issue}
  {carousel}
  {exterior_status}
  <div class="links">
    <a href="{gmaps}" target="_blank" rel="noopener">\U0001f4cd Google Maps</a>
    <a href="{gearth}" target="_blank" rel="noopener">\U0001f30d Google Earth</a>
    <a href="{bing}" target="_blank" rel="noopener">\U0001f5fa️ Bing Maps</a>
  </div>
  <details class="why"><summary>What the models said</summary>{_model_boxes(e, reasoning)}</details>
  {sheet_details}
  <div class="review">
    <div class="label">HVAC systems you see (check all):</div>
    <div class="chips">{chips}</div>
    {fitrows}
    <input class="note" type="text" placeholder="optional note…" value="{_html.escape(str(human.get("note", "")))}"{note_dis}>
    {actions}
    {status}
  </div>
</article>'''


def build_review_page(
    entries, job_id, webhook_url="", title="Cooling Tower Review",
    csrf_token="", review_schema=CURRENT_REVIEW_SCHEMA,
    page=None, page_size=None, page_url="", source_context_refresh_url="",
    alex_review_enabled=False,
):
    """Render a review page, optionally limited to one server-selected page.

    ``entries`` must remain the complete batch so header and tab progress stay
    global. Passing a positive integer ``page_size`` enables pagination; callers
    may pass ``page_url`` to preserve a stable route in Previous/Next links.
    """
    entries = list(entries)
    total = len(entries)
    alex_queue_active = bool(
        alex_review_enabled
        and batch_primary_review_complete(entries, review_schema)
    )
    entry_records = []
    for entry in entries:
        state, reason = _review_state(
            entry, review_schema, alex_review_enabled
        )
        if (
            alex_queue_active
            and state == "reviewed"
            and entry_needs_alex_review(entry, review_schema)
        ):
            state = "alex_review"
        entry_records.append((entry, state, reason))
    state_order = (
        ("alex_review", "reviewed", "pending", "attention")
        if alex_queue_active
        else _REVIEW_STATE_ORDER
    )
    ordered_records = [
        record
        for state in state_order
        for record in entry_records
        if record[1] == state
    ]
    pagination_enabled = (
        isinstance(page_size, int)
        and not isinstance(page_size, bool)
        and page_size > 0
    )
    if pagination_enabled:
        total_pages = max(1, (total + page_size - 1) // page_size)
        try:
            current_page = int(page or 1)
        except (TypeError, ValueError):
            current_page = 1
        current_page = min(max(current_page, 1), total_pages)
        page_start = (current_page - 1) * page_size
        visible_records = ordered_records[page_start:page_start + page_size]
    else:
        total_pages = 1
        current_page = 1
        page_start = 0
        visible_records = ordered_records
    visible_entries = [record[0] for record in visible_records]

    all_grouped = {}
    for entry in entries:
        all_grouped.setdefault(str(entry.get("source_tab") or ""), []).append(entry)
    sections = []
    for state in state_order:
        state_records = [record for record in visible_records if record[1] == state]
        if not state_records:
            continue
        state_total = sum(1 for record in entry_records if record[1] == state)
        state_visible = len(state_records)
        state_context = (
            f" · {state_visible} on this page"
            if pagination_enabled and state_visible != state_total
            else ""
        )
        visible_grouped = {}
        for entry, _entry_state, reason in state_records:
            visible_grouped.setdefault(
                str(entry.get("source_tab") or ""), []
            ).append((entry, reason))
        state_sections = []
        for tab, tab_records in visible_grouped.items():
            tab_entries = [record[0] for record in tab_records]
            all_tab_entries = all_grouped[tab]
            tab_done = sum(
                1 for entry in all_tab_entries
                if entry_is_reviewed(entry, review_schema)
            )
            page_context = (
                f' · {len(tab_entries)} on this page'
                if pagination_enabled and len(tab_entries) != len(all_tab_entries)
                else ""
            )
            tab_heading = ""
            if len(all_grouped) > 1 or tab:
                tab_heading = (
                    f'<div class="tab-head"><h2>{_html.escape(tab or "Other")}</h2>'
                    f'<span class="tab-progress" data-total="{len(all_tab_entries)}" '
                    f'data-reviewed="{tab_done}">{len(all_tab_entries)}/{len(all_tab_entries)} analyzed '
                    f'· {tab_done}/{len(all_tab_entries)} reviewed{page_context}</span></div>'
                )
            state_sections.append(
                f'<section class="tab-group">{tab_heading}'
                + "\n".join(
                    _card(
                        entry,
                        review_schema,
                        state,
                        reason,
                        alex_review_enabled=alex_review_enabled,
                    )
                    for entry, reason in tab_records
                )
                + "</section>"
            )
        sections.append(
            f'<section class="review-state-group {state}">'
            f'<div class="state-head"><h2>{"Other completed reviews" if alex_queue_active and state == "reviewed" else _REVIEW_STATE_LABELS[state]}</h2>'
            f'<span>{state_total}{state_context}</span></div>'
            + "\n".join(state_sections)
            + "</section>"
        )
    cards = "\n".join(sections)
    done0 = sum(1 for e in entries if entry_is_reviewed(e, review_schema))
    pending0 = sum(1 for record in entry_records if record[1] == "pending")
    attention0 = sum(1 for record in entry_records if record[1] == "attention")
    alex0 = sum(1 for record in entry_records if record[1] == "alex_review")
    if pagination_enabled:
        base_url = str(page_url or "")

        def page_href(number):
            if not base_url:
                return f"?page={number}"
            parsed = urllib.parse.urlsplit(base_url)
            query = [
                (key, value)
                for key, value in urllib.parse.parse_qsl(
                    parsed.query, keep_blank_values=True
                )
                if key != "page"
            ]
            query.append(("page", str(number)))
            return urllib.parse.urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urllib.parse.urlencode(query),
                    parsed.fragment,
                )
            )

        first_visible = page_start + 1 if visible_entries else 0
        last_visible = page_start + len(visible_entries)
        previous = (
            f'<a class="page-link" href="{_html.escape(page_href(current_page - 1), quote=True)}">'
            "← Previous</a>"
            if current_page > 1
            else '<span class="page-link disabled">← Previous</span>'
        )
        following = (
            f'<a class="page-link" href="{_html.escape(page_href(current_page + 1), quote=True)}">'
            "Next →</a>"
            if current_page < total_pages
            else '<span class="page-link disabled">Next →</span>'
        )
        pagination = (
            '<nav class="pagination" aria-label="Review pages">'
            f'{previous}<span class="page-summary">Buildings {first_visible}–{last_visible} '
            f'of {total} · page {current_page} of {total_pages}</span>{following}</nav>'
        )
    else:
        pagination = ""
    first_page_url = (
        page_href(1)
        if pagination_enabled
        else str(page_url or "")
    )
    wh = json.dumps(webhook_url)
    jid = json.dumps(str(job_id))
    csrf = json.dumps(str(csrf_token))
    context_refresh = json.dumps(str(source_context_refresh_url or ""))
    schema_json = json.dumps(str(review_schema))
    first_page_json = json.dumps(first_page_url)
    alex_header = (
        f' · Needs Alex Review: <span id="alex-count">{alex0}</span>'
        if alex_review_enabled and is_dual_fit_schema(review_schema)
        else ""
    )
    source_context_toolbar = ""
    if source_context_refresh_url:
        source_context_toolbar = '''
<div class="context-refresh">
  <div><strong>Stories missing?</strong><span> Load only the Stories/Floors column from the source Sheet. Analysis and review decisions are unchanged.</span></div>
  <button type="button" id="refresh-source-context" onclick="refreshSourceContext()">Load stories from Sheet</button>
  <span id="context-refresh-status" class="context-refresh-status"></span>
</div>'''
    fit_help = (
        "both Optimizer Fit and Periscope Fit choices"
        if is_dual_fit_schema(review_schema)
        else "a Fit choice"
    )
    return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{margin:0;background:#0d0f12;color:#e6e8eb;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#12151a;border-bottom:1px solid #262b33;padding:14px 20px}}
header h1{{margin:0;font-size:17px}}header .sub{{color:#9aa3ad;font-size:13px;margin-top:2px}}
.wrap{{max-width:900px;margin:0 auto;padding:18px}}
.review-state-group{{margin:0 0 28px}}
.state-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;
 margin:6px 2px 14px;padding:10px 12px;border-radius:10px;background:#161a20;border:1px solid #262b33}}
.state-head h2{{margin:0;font-size:18px}}.state-head span{{color:#9aa3ad;font-size:12px}}
.review-state-group.pending .state-head{{border-color:#375a7f}}
.review-state-group.attention .state-head{{border-color:#c92a2a;background:#271416}}
.review-state-group.alex_review .state-head{{border-color:#d4a017;background:#2a2412}}
.tab-group{{margin:0 0 28px}}.tab-head{{display:flex;justify-content:space-between;align-items:center;
 gap:12px;margin:6px 2px 12px;padding-bottom:8px;border-bottom:1px solid #333b45}}
.tab-head h2{{margin:0;font-size:18px}}.tab-head span,.source{{color:#9aa3ad;font-size:12px}}
.source-context{{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}}
.context-item{{color:#d8dde4;font-size:13px}}.context-item strong{{color:#fff}}
.card{{background:#161a20;border:1px solid #262b33;border-radius:12px;padding:16px;margin:0 0 16px}}
.card.done{{border-color:#2f9e44;opacity:.85}}
.card.writeback-error{{border-color:#f59f00;opacity:1}}
.card.pending{{border-color:#375a7f}}
.card.attention{{border-color:#e03131;opacity:1}}
.card.alex_review{{border-color:#d4a017;opacity:1}}
.head{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
.addr{{font-weight:600}}.badge{{color:#0d0f12;font-weight:700;font-size:12px;padding:2px 8px;border-radius:20px}}
.issue{{margin:12px 0;padding:10px 12px;border-radius:8px;font-size:13px}}
.issue.pending{{background:#10243a;border:1px solid #375a7f;color:#d7e9fb}}
.issue.attention{{background:#321719;border:1px solid #e03131;color:#ffd8d8}}
.carousel{{position:relative;margin:12px 0;background:#0b0d10;border:1px solid #222833;border-radius:10px}}
.frame{{text-align:center;min-height:440px;display:flex;align-items:center;justify-content:center}}
.frame img{{display:none;width:100%;max-width:100%;max-height:72vh;object-fit:contain;border-radius:8px;cursor:zoom-in}}
.frame img.cur{{display:block}}
.nav{{position:absolute;top:50%;transform:translateY(-50%);background:rgba(20,24,30,.8);color:#fff;border:1px solid #333b45;
 width:40px;height:52px;border-radius:8px;font-size:26px;cursor:pointer;z-index:2}}
.nav:hover{{background:rgba(40,48,60,.95)}}.prev{{left:8px}}.next{{right:8px}}
.cap{{text-align:center;color:#9aa3ad;font-size:12px;padding:6px}}
.noimg{{color:#6b7280;font-style:italic;padding:28px;text-align:center}}
.exterior-status{{margin:-2px 0 10px;padding:8px 10px;border-radius:8px;font-size:13px}}
.exterior-status.missing{{background:#2b2415;border:1px solid #8a6d2f;color:#f5deb3}}
.links{{display:flex;gap:14px;font-size:13px;margin:2px 0 10px}}.links a{{color:#4d9fff;text-decoration:none}}.links a:hover{{text-decoration:underline}}
.why{{margin:4px 0 12px}}.why summary{{cursor:pointer;color:#9aa3ad;font-size:13px}}.why p.combined{{margin:8px 0 0;font-size:13px;color:#aeb6bf}}
.sheet-details{{margin:4px 0 12px;border:1px solid #333b45;border-radius:8px;padding:8px 10px}}
.sheet-details summary{{cursor:pointer;color:#c7ccd2;font-size:13px;font-weight:600}}
.sheet-details dl{{margin:10px 0 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px}}
.sheet-info-row{{background:#101318;border-radius:6px;padding:7px 9px}}
.sheet-info-row dt{{color:#8f98a3;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
.sheet-info-row dd{{margin:2px 0 0;color:#eef1f4;font-size:13px;overflow-wrap:anywhere}}
.mbox{{margin:8px 0 0;border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.45}}
.mbox .mh{{font-weight:700;font-size:12px;margin-bottom:5px;letter-spacing:.02em}}
.mbox.gemini{{background:#0f2a1a;border:1px solid #2f9e44;color:#d3f0dd}}.mbox.gemini .mh{{color:#69db7c}}
.mbox.grok{{background:#000;border:1px solid #3a4150;color:#ccd2da}}.mbox.grok .mh{{color:#aab2bd}}
.review{{border-top:1px solid #262b33;padding-top:12px}}
.label{{font-size:13px;color:#c7ccd2;margin-bottom:8px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
.chip{{background:#1e232b;color:#cfd4da;border:1px solid #333b45;border-radius:20px;padding:7px 14px;cursor:pointer;font-size:13px}}
.chip:hover{{border-color:#4a5563}}
.chip.sel{{background:#0f5132;color:#fff;border-color:#37b24d;font-weight:600}}
.chip.sel::before{{content:"✓ "}}
.fitchips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}}
.fitchip{{background:#1e232b;color:#cfd4da;border:1px solid #333b45;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px}}
.fitchip:hover{{border-color:#4a5563}}
.fitchip.sel{{background:#173a66;color:#fff;border-color:#4d9fff;font-weight:600}}
.review .note{{background:#0f1216;color:#e6e8eb;border:1px solid #333b45;border-radius:8px;padding:8px;width:100%;margin-bottom:10px}}
.submit{{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 18px;cursor:pointer}}
.submit:disabled{{background:#2a3340;color:#6b7280;cursor:not-allowed}}
.alex-unlock,.alex-confirm,.alex-cancel{{border-radius:8px;padding:9px 14px;cursor:pointer}}
.alex-unlock{{background:#9c6f00;color:#fff;border:1px solid #d4a017}}
.alex-actions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.alex-actions[hidden]{{display:none}}
.alex-confirm{{background:#173a66;color:#fff;border:1px solid #4d9fff}}
.alex-cancel{{background:#1e232b;color:#cfd4da;border:1px solid #333b45}}
.card.alex-editing{{box-shadow:0 0 0 2px rgba(212,160,23,.22)}}
.status{{margin-left:12px;color:#9aa3ad;font-size:13px}}.status.ok{{color:#37b24d}}.status.err{{color:#ff6b6b}}
.bulk-review{{background:#161a20;border:1px solid #2f9e44;border-radius:12px;padding:16px;margin:24px 0 8px}}
.bulk-review .help{{color:#9aa3ad;font-size:13px;margin-bottom:10px}}
.bulk-submit{{background:#2f9e44;color:#fff;border:0;border-radius:8px;padding:10px 18px;cursor:pointer;font-weight:600}}
.bulk-submit:disabled{{background:#2a3340;color:#6b7280;cursor:not-allowed}}
.bulk-status{{margin-left:12px;color:#9aa3ad;font-size:13px}}.bulk-status.ok{{color:#37b24d}}.bulk-status.err{{color:#ff6b6b}}
.pagination{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 16px;
 background:#161a20;border:1px solid #262b33;border-radius:10px;padding:10px 12px}}
.page-link{{color:#4d9fff;text-decoration:none;white-space:nowrap}}.page-link:hover{{text-decoration:underline}}
.page-link.disabled{{color:#59616c;cursor:default}}.page-link.disabled:hover{{text-decoration:none}}
.page-summary{{color:#c7ccd2;text-align:center;font-size:13px}}
.context-refresh{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 16px;
 background:#161a20;border:1px solid #375a7f;border-radius:10px;padding:10px 12px}}
.context-refresh div{{flex:1;min-width:240px}}.context-refresh div span{{color:#9aa3ad;font-size:13px}}
.context-refresh button{{background:#173a66;color:#fff;border:1px solid #4d9fff;border-radius:8px;padding:8px 12px;cursor:pointer}}
.context-refresh button:disabled{{background:#2a3340;color:#6b7280;border-color:#333b45;cursor:not-allowed}}
.context-refresh-status{{color:#9aa3ad;font-size:13px}}.context-refresh-status.err{{color:#ff6b6b}}
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:99;padding:24px;text-align:center;cursor:zoom-out}}
#lb img{{max-width:96%;max-height:92vh;border:2px solid #fff;border-radius:6px}}
@media(max-width:560px){{.pagination{{gap:8px}}.page-link{{font-size:12px}}.page-summary{{font-size:11px}}}}
</style></head><body>
<header><h1>{_html.escape(title)}</h1>
<div class="sub">{total}/{total} analyzed · <span id="done">{done0}</span>/{total} reviewed{alex_header} · <span id="pending-count">{pending0}</span> still need review · <span id="attention-count">{attention0}</span> need attention · every Submit writes to the source tab</div></header>
<div class="wrap">{source_context_toolbar}{pagination}{cards}{pagination}</div>
<div class="bulk-review">
  <div class="help"><strong>Done reviewing?</strong> Submit every unreviewed card{' on this page' if pagination_enabled else ''} that has an HVAC system selected (or <em>None</em>) and {fit_help}. Incomplete cards are skipped so nothing is guessed.</div>
  <button type="button" id="submit-all" class="bulk-submit" onclick="submitAll()">Submit all completed{' on this page' if pagination_enabled else ''}</button>
  <span id="bulk-status" class="bulk-status"></span>
</div>
<div id="lb" onclick="this.style.display='none'"><img id="lbi"></div>
<script>
const WEBHOOK={wh}, JOB={jid}, CSRF_TOKEN={csrf}, REVIEW_SCHEMA={schema_json}, SOURCE_CONTEXT_REFRESH={context_refresh}, FIRST_PAGE_URL={first_page_json};let done={done0};
let primaryAlexReady=false;
async function refreshSourceContext(){{
  if(!SOURCE_CONTEXT_REFRESH)return;
  const button=document.getElementById('refresh-source-context');
  const status=document.getElementById('context-refresh-status');
  button.disabled=true;status.textContent='Loading stories...';status.className='context-refresh-status';
  try{{
    const headers={{}};if(CSRF_TOKEN)headers['X-CSRF-Token']=CSRF_TOKEN;
    const response=await fetch(SOURCE_CONTEXT_REFRESH,{{method:'POST',headers}});
    let reply={{}};try{{reply=await response.json();}}catch(e){{reply={{}};}}
    if(!response.ok)throw new Error(reply.error||('HTTP '+response.status));
    status.textContent=(reply.updated||0)+' card'+((reply.updated||0)===1?'':'s')+' updated';
    window.location.reload();
  }}catch(e){{status.textContent=e.message+' (retry)';status.className='context-refresh-status err';button.disabled=false;}}
}}
function zoom(s){{document.getElementById('lbi').src=s;document.getElementById('lb').style.display='block';}}
function setCap(c){{const i=+c.dataset.i,imgs=c.querySelectorAll('.frame img');
  const current=imgs[i];if(!current.getAttribute('src')&&current.dataset.src)current.src=current.dataset.src;
  imgs.forEach((im,k)=>im.classList.toggle('cur',k==i));
  c.querySelector('.cap').textContent=current.dataset.label+' · '+(i+1)+'/'+imgs.length+' · click image to enlarge';}}
function nav(btn,d){{const c=btn.closest('.carousel');const n=+c.dataset.n;c.dataset.i=((+c.dataset.i+d)%n+n)%n;setCap(c);}}
function toggle(chip){{
  if(!chip.classList.toggle('sel'))return;
  const none=chip.dataset.sys==='None';
  chip.parentElement.querySelectorAll('.chip.sel').forEach(c=>{{
    if(c!==chip&&(none||c.dataset.sys==='None'))c.classList.remove('sel');}});
}}
function pickFit(chip){{chip.parentElement.querySelectorAll('.fitchip').forEach(c=>c.classList.remove('sel'));chip.classList.add('sel');}}
function selectedFits(card){{const fits={{}};card.querySelectorAll('.fitchips').forEach(g=>{{
  const selected=g.querySelector('.fitchip.sel');fits[g.dataset.col]=selected?selected.dataset.fit:'';}});return fits;}}
function goToFirstPage(){{if(FIRST_PAGE_URL)window.location.href=FIRST_PAGE_URL;else window.location.reload();}}
function beginAlexReview(btn){{
  const card=btn.closest('.card');
  card.dataset.alexSnapshot=JSON.stringify({{fits:selectedFits(card),note:card.querySelector('.note').value}});
  card.querySelectorAll('.fitchip,.note').forEach(control=>control.disabled=false);
  card.classList.add('alex-editing');btn.hidden=true;card.querySelector('.alex-actions').hidden=false;
}}
function cancelAlexReview(btn){{
  const card=btn.closest('.card');let snapshot={{fits:{{}},note:''}};
  try{{snapshot=JSON.parse(card.dataset.alexSnapshot||'{{}}');}}catch(e){{}}
  card.querySelectorAll('.fitchips').forEach(group=>group.querySelectorAll('.fitchip').forEach(chip=>
    chip.classList.toggle('sel',chip.dataset.fit===(snapshot.fits||{{}})[group.dataset.col])));
  card.querySelector('.note').value=snapshot.note||'';
  card.querySelectorAll('.fitchip,.note').forEach(control=>control.disabled=true);
  card.classList.remove('alex-editing');card.querySelector('.alex-actions').hidden=true;
  card.querySelector('.alex-unlock').hidden=false;
}}
async function submitSecondary(btn,action){{
  const card=btn.closest('.card'),status=card.querySelector('.status'),fits=selectedFits(card);
  const payload={{job_id:JOB,row_id:card.dataset.rid,review_stage:'secondary',
    secondary_action:action,expected_review_version:+card.dataset.reviewVersion}};
  if(action==='revise'){{
    if(!fits.optimizer_fit||!fits.periscope_fit){{status.textContent='choose both Optimizer Fit and Periscope Fit';status.className='status err';return;}}
    payload.optimizer_fit=fits.optimizer_fit;payload.periscope_fit=fits.periscope_fit;
    payload.note=card.querySelector('.note').value;
  }}
  const buttons=[...card.querySelectorAll('.alex-actions button,.secondary-retry')];
  buttons.forEach(control=>control.disabled=true);status.textContent='saving…';status.className='status';
  try{{
    if(!WEBHOOK)throw new Error('secondary review requires the live review service');
    const headers={{'Content-Type':'application/json'}};if(CSRF_TOKEN)headers['X-CSRF-Token']=CSRF_TOKEN;
    const response=await fetch(WEBHOOK,{{method:'POST',headers,body:JSON.stringify(payload)}});
    let reply={{}};try{{reply=await response.json();}}catch(e){{reply={{}};}}
    if(!response.ok)throw new Error(reply.error||('HTTP '+response.status));
    if(reply.sheet==='error'||reply.sheet==='row_not_found'){{goToFirstPage();return;}}
    status.textContent=action==='revise'?'✓ Alex review saved':'✓ current qualification confirmed';
    status.className='status ok';goToFirstPage();
  }}catch(e){{status.textContent='✗ '+e.message+' (retry)';status.className='status err';buttons.forEach(control=>control.disabled=false);}}
}}
document.querySelectorAll('.carousel').forEach(setCap);
async function submitCard(btn,reloadAfterSave=true){{
  const card=btn.closest('.card');
  const sys=[...card.querySelectorAll('.chip.sel')].map(c=>c.dataset.sys);
  const status=card.querySelector('.status');
  if(!sys.length){{status.textContent='pick at least one system (or None)';status.className='status err';return 'incomplete';}}
  const fits={{}};
  card.querySelectorAll('.fitchips').forEach(g=>{{const s=g.querySelector('.fitchip.sel');fits[g.dataset.col]=s?s.dataset.fit:'';}});
  if(REVIEW_SCHEMA==='single_fit_v1' && !fits.fit){{
    status.textContent='choose one Fit: Optimizer, Periscope, Unclear, or Bad';
    status.className='status err';return 'incomplete';
  }}
  if(REVIEW_SCHEMA.startsWith('dual_product_fit_') && (!fits.optimizer_fit || !fits.periscope_fit)){{
    status.textContent='choose both Optimizer Fit and Periscope Fit';
    status.className='status err';return 'incomplete';
  }}
  const payload={{job_id:JOB,row_id:card.dataset.rid,address:card.dataset.addr,ai_verdict:card.dataset.ai,
    hvac_systems:sys.join(', '),note:card.querySelector('.note').value}};
  if(REVIEW_SCHEMA==='single_fit_v1')payload.fit=fits.fit||'';
  else{{payload.optimizer_fit=fits.optimizer_fit||'';payload.periscope_fit=fits.periscope_fit||'';}}
  btn.disabled=true;status.textContent='saving…';status.className='status';
  try{{
    let reply={{}};
    if(WEBHOOK){{const headers={{'Content-Type':'application/json'}};if(CSRF_TOKEN)headers['X-CSRF-Token']=CSRF_TOKEN;const r=await fetch(WEBHOOK,{{method:'POST',headers,body:JSON.stringify(payload)}});
      try{{reply=await r.json();}}catch(e){{reply={{}};}}
      if(!r.ok)throw new Error(reply.error||('HTTP '+r.status));
      if(reply.sheet==='error' || reply.sheet==='row_not_found'){{
        status.textContent=reply.sheet==='row_not_found'
          ? 'saved locally; Sheet row not found - retry'
          : 'saved locally; Sheet update failed - retry';
        status.className='status err';btn.disabled=false;return 'sheet_error';
      }}
    }}
    else{{console.log('[DEMO] would POST to n8n:',payload);await new Promise(r=>setTimeout(r,250));}}
    if(reply.primary_review_complete&&reply.alex_review_remaining>0)primaryAlexReady=true;
    status.textContent='✓ saved: '+payload.hvac_systems+(payload.fit?' · Fit: '+payload.fit:'')+(payload.optimizer_fit?' · Optimizer: '+payload.optimizer_fit:'')+(payload.periscope_fit?' · Periscope: '+payload.periscope_fit:'')+(WEBHOOK?'':' (demo)');status.className='status ok';
    const wasReviewed=card.dataset.reviewed==='1';
    const previousState=card.dataset.reviewState||'';
    card.classList.add('done');card.classList.remove('writeback-error','pending','attention');
    card.dataset.reviewState='reviewed';
    const issue=card.querySelector('.issue');if(issue)issue.remove();
    if(previousState==='pending'||previousState==='attention'){{
      const stateCount=document.getElementById(previousState+'-count');
      if(stateCount)stateCount.textContent=Math.max(0,(+stateCount.textContent||0)-1);
    }}
    card.dataset.reviewed='1';card.querySelectorAll('.chip,.fitchip,.note').forEach(c=>c.disabled=true);
    if(!wasReviewed){{done++;document.getElementById('done').textContent=done;}}
    const group=card.closest('.tab-group');
    if(group&&!wasReviewed){{const progress=group.querySelector('.tab-progress');
      if(progress){{const totalInGroup=+progress.dataset.total;
        const doneInGroup=(+progress.dataset.reviewed)+1;
        progress.dataset.reviewed=doneInGroup;
        const onPage=group.querySelectorAll('.card').length;
        const pageContext=onPage!==totalInGroup?' · '+onPage+' on this page':'';
        progress.textContent=totalInGroup+'/'+totalInGroup+' analyzed · '+doneInGroup+'/'+totalInGroup+' reviewed'+pageContext;}}}}
    if(reloadAfterSave&&primaryAlexReady)goToFirstPage();
    else if(reloadAfterSave)window.location.reload();
    return 'saved';
  }}catch(e){{status.textContent='✗ '+e.message+' (retry)';status.className='status err';btn.disabled=false;}}
}}
function setBulkStatus(message, kind){{
  const s=document.getElementById('bulk-status');s.textContent=message;s.className='bulk-status'+(kind?' '+kind:'');
}}
async function submitAll(){{
  const bulk=document.getElementById('submit-all');
  const pending=[...document.querySelectorAll('.card')].filter(c=>!c.classList.contains('done'));
  const ready=pending.filter(c=>{{
    if(!c.querySelectorAll('.chip.sel').length)return false;
    const fits=[...c.querySelectorAll('.fitchips')];
    return fits.length>0 && fits.every(g=>g.querySelector('.fitchip.sel'));
  }});
  const skipped=pending.length-ready.length;
  if(!ready.length){{
    setBulkStatus(pending.length?'Nothing submitted - choose HVAC and Fit first.':'Everything is already reviewed.','err');
    return;
  }}
  bulk.disabled=true;setBulkStatus('Saving '+ready.length+' completed card'+(ready.length===1?'':'s')+'...');
  let saved=0,failed=0;
  for(const card of ready){{
    const result=await submitCard(card.querySelector('.submit'),false);
    if(result==='saved')saved++;else failed++;
  }}
  bulk.disabled=false;
  const parts=[saved+' saved'];
  if(skipped)parts.push(skipped+' skipped - needs HVAC or Fit');
  if(failed)parts.push(failed+' need retry');
  setBulkStatus(parts.join(' Â· '),(skipped||failed)?'err':'ok');
  if(saved&&primaryAlexReady)goToFirstPage();
  else if(saved)window.location.reload();
}}
</script></body></html>'''


if __name__ == "__main__":
    import sys
    src, out = sys.argv[1], sys.argv[2]
    webhook = sys.argv[3] if len(sys.argv) > 3 else ""
    data = json.load(open(src, encoding="utf-8"))
    open(out, "w", encoding="utf-8").write(
        build_review_page(data, job_id="demo-mixed-2026-07-06", webhook_url=webhook,
                          title="Cooling Tower Review — Mixed Set (12 buildings)"))
    print("wrote", out)
