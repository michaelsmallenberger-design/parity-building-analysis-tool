"""Interactive review page with imagery, HVAC, and product-fit decisions.

Current workbooks review Optimizer and Periscope independently. Previously
persisted single-Fit batches keep their versioned interface.
"""
import html as _html
import json
import urllib.parse

from review_contract import (
    CURRENT_REVIEW_SCHEMA,
    DUAL_FIT_COLUMNS,
    DUAL_FIT_OPTIONS,
    DUAL_FIT_SCHEMA,
    FIT_OPTIONS,
    SINGLE_FIT_SCHEMA,
    entry_is_reviewed,
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
    ("result_image_url_streetview", "Street View"),
]
VERDICT_COLOR = {
    "confirmed": "#37b24d", "registry_confirmed": "#37b24d", "likely": "#94d82d",
    "neighbor_only": "#f59f00", "needs_review": "#f59f00",
    "likely_residential": "#868e96", "not_detected": "#e8590c", "": "#868e96",
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


def _card(e, review_schema=CURRENT_REVIEW_SCHEMA):
    # A batch entry carries a "human" decision once reviewed (review_store); render
    # such cards in the exact state a live Submit leaves them in, so reopening the
    # page mid-batch shows what's already done.
    human = e.get("human") or {}
    reviewed = entry_is_reviewed(e, review_schema)
    writeback = e.get("writeback") or {}
    writeback_error = writeback.get("status") == "error"
    picked = {s.strip() for s in str(human.get("hvac_systems", "")).split(",") if s.strip()}
    rid = _html.escape(str(
        e.get("row_id") if e.get("row_id") is not None else e.get("i", "")
    ))
    addr = str(e.get("address", ""))
    addr_e = _html.escape(addr)
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
    for key, label in _IMG_SLOTS:
        v = e.get(key)
        # data: URIs (API path) or server-hosted /files/ paths & absolute URLs
        # (browser-upload worker path) — all render in the same carousel.
        if isinstance(v, str) and v.startswith(("data:", "/files/", "http")):
            safe_v = _html.escape(v, quote=True)
            source_attr = f'src="{safe_v}"' if not n else f'data-src="{safe_v}"'
            slides += (
                f'<img {source_attr} data-label="{label}" alt="{label}" '
                f'loading="lazy" decoding="async" onclick="zoom(this.src)"'
                f'{"" if n else " class=cur"}>'
            )
            n += 1
    if not n:
        carousel = '<div class="noimg">no imagery</div>'
    else:
        arrows = ('<button type="button" class="nav prev" onclick="nav(this,-1)">‹</button>'
                  '<button type="button" class="nav next" onclick="nav(this,1)">›</button>') if n > 1 else ''
        carousel = (f'<div class="carousel" data-n="{n}" data-i="0">{arrows}'
                    f'<div class="frame">{slides}</div>'
                    f'<div class="cap"></div></div>')

    controls_dis = " disabled" if reviewed else ""
    submit_dis = " disabled" if reviewed and not writeback_error else ""
    chips = ""
    for s in HVAC_SYSTEMS + [NONE_OPTION]:
        if human:
            pre = " sel" if s in picked else ""
        else:
            pre = " sel" if (s == "Cooling Tower" and ai_positive) else ""
        chips += f'<button type="button" class="chip{pre}"{controls_dis} data-sys="{_html.escape(s)}" onclick="toggle(this)">{_html.escape(s)}</button>'
    fitrows = ""
    if review_schema == DUAL_FIT_SCHEMA:
        fit_specs = [
            ("Optimizer Fit", "optimizer_fit", DUAL_FIT_OPTIONS),
            ("Periscope Fit", "periscope_fit", DUAL_FIT_OPTIONS),
        ]
    else:
        fit_specs = [("Fit", "fit", FIT_OPTIONS)]
    for col, fit_key, fit_options in fit_specs:
        picked_fit = str(human.get(fit_key) or "")
        if not human and ai_positive:
            if review_schema == DUAL_FIT_SCHEMA and fit_key == "optimizer_fit":
                picked_fit = "Good"
            elif review_schema == SINGLE_FIT_SCHEMA and fit_key == "fit":
                picked_fit = "Optimizer"
        chips_f = ""
        for fo in fit_options:
            pre = " sel" if fo == picked_fit else ""
            chips_f += (f'<button type="button" class="fitchip{pre}"{controls_dis} '
                        f'data-fit="{_html.escape(fo)}" onclick="pickFit(this)">{_html.escape(fo)}</button>')
        fitrows += (f'<div class="label">{_html.escape(col)}:</div>'
                    f'<div class="fitchips" data-col="{fit_key}">{chips_f}</div>')

    if writeback_error:
        status = (
            '<span class="status err">saved locally; Sheet write-back failed '
            '— retry</span>'
        )
    elif reviewed:
        saved = "✓ saved: " + str(human.get("hvac_systems", ""))
        for col, fit_key, _fit_options in fit_specs:
            v = human.get(fit_key)
            if v:
                saved += f" · {col}: {v}"
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
    button_text = "Retry Sheet write-back" if writeback_error else "Submit"

    return f'''
<article class="card{' done' if reviewed else ''}{' writeback-error' if writeback_error else ''}" data-rid="{rid}" data-reviewed="{'1' if reviewed else '0'}" data-addr="{addr_e}" data-ai="{_html.escape(ai)}">
  <div class="head">
    <div><div class="addr">{addr_e}</div>{source}</div>
    <div class="ai">model: <span class="badge" style="background:{ai_color}">{_html.escape(ai) or '—'}</span></div>
  </div>
  {carousel}
  <div class="links">
    <a href="{gmaps}" target="_blank" rel="noopener">\U0001f4cd Google Maps</a>
    <a href="{gearth}" target="_blank" rel="noopener">\U0001f30d Google Earth</a>
    <a href="{bing}" target="_blank" rel="noopener">\U0001f5fa️ Bing Maps</a>
  </div>
  <details class="why"><summary>What the models said</summary>{_model_boxes(e, reasoning)}</details>
  <div class="review">
    <div class="label">HVAC systems you see (check all):</div>
    <div class="chips">{chips}</div>
    {fitrows}
    <input class="note" type="text" placeholder="optional note…" value="{_html.escape(str(human.get("note", "")))}"{controls_dis}>
    <button type="button" class="submit"{submit_dis} onclick="submitCard(this)">{button_text}</button>
    {status}
  </div>
</article>'''


def build_review_page(
    entries, job_id, webhook_url="", title="Cooling Tower Review",
    csrf_token="", review_schema=CURRENT_REVIEW_SCHEMA,
    page=None, page_size=None, page_url="",
):
    """Render a review page, optionally limited to one server-selected page.

    ``entries`` must remain the complete batch so header and tab progress stay
    global. Passing a positive integer ``page_size`` enables pagination; callers
    may pass ``page_url`` to preserve a stable route in Previous/Next links.
    """
    entries = list(entries)
    total = len(entries)
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
        visible_entries = entries[page_start:page_start + page_size]
    else:
        total_pages = 1
        current_page = 1
        page_start = 0
        visible_entries = entries

    all_grouped = {}
    for entry in entries:
        all_grouped.setdefault(str(entry.get("source_tab") or ""), []).append(entry)
    visible_grouped = {}
    for entry in visible_entries:
        visible_grouped.setdefault(str(entry.get("source_tab") or ""), []).append(entry)
    if len(all_grouped) == 1 and "" in all_grouped:
        cards = "\n".join(_card(e, review_schema) for e in visible_entries)
    else:
        sections = []
        for tab, tab_entries in visible_grouped.items():
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
            sections.append(
                f'<section class="tab-group"><div class="tab-head">'
                f'<h2>{_html.escape(tab or "Other")}</h2>'
                f'<span class="tab-progress" data-total="{len(all_tab_entries)}" '
                f'data-reviewed="{tab_done}">{len(all_tab_entries)}/{len(all_tab_entries)} analyzed '
                f'· {tab_done}/{len(all_tab_entries)} reviewed{page_context}</span></div>'
                + "\n".join(_card(entry, review_schema) for entry in tab_entries)
                + "</section>"
            )
        cards = "\n".join(sections)
    done0 = sum(1 for e in entries if entry_is_reviewed(e, review_schema))
    attention0 = sum(
        1 for e in entries
        if e.get("error") and not entry_is_reviewed(e, review_schema)
    )
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
    wh = json.dumps(webhook_url)
    jid = json.dumps(str(job_id))
    csrf = json.dumps(str(csrf_token))
    schema_json = json.dumps(str(review_schema))
    fit_help = (
        "both Optimizer Fit and Periscope Fit choices"
        if review_schema == DUAL_FIT_SCHEMA
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
.tab-group{{margin:0 0 28px}}.tab-head{{display:flex;justify-content:space-between;align-items:center;
 gap:12px;margin:6px 2px 12px;padding-bottom:8px;border-bottom:1px solid #333b45}}
.tab-head h2{{margin:0;font-size:18px}}.tab-head span,.source{{color:#9aa3ad;font-size:12px}}
.card{{background:#161a20;border:1px solid #262b33;border-radius:12px;padding:16px;margin:0 0 16px}}
.card.done{{border-color:#2f9e44;opacity:.85}}
.card.writeback-error{{border-color:#f59f00;opacity:1}}
.head{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
.addr{{font-weight:600}}.badge{{color:#0d0f12;font-weight:700;font-size:12px;padding:2px 8px;border-radius:20px}}
.carousel{{position:relative;margin:12px 0;background:#0b0d10;border:1px solid #222833;border-radius:10px}}
.frame{{text-align:center;min-height:320px;display:flex;align-items:center;justify-content:center}}
.frame img{{display:none;max-width:100%;max-height:60vh;border-radius:8px;cursor:zoom-in}}
.frame img.cur{{display:block}}
.nav{{position:absolute;top:50%;transform:translateY(-50%);background:rgba(20,24,30,.8);color:#fff;border:1px solid #333b45;
 width:40px;height:52px;border-radius:8px;font-size:26px;cursor:pointer;z-index:2}}
.nav:hover{{background:rgba(40,48,60,.95)}}.prev{{left:8px}}.next{{right:8px}}
.cap{{text-align:center;color:#9aa3ad;font-size:12px;padding:6px}}
.noimg{{color:#6b7280;font-style:italic;padding:28px;text-align:center}}
.links{{display:flex;gap:14px;font-size:13px;margin:2px 0 10px}}.links a{{color:#4d9fff;text-decoration:none}}.links a:hover{{text-decoration:underline}}
.why{{margin:4px 0 12px}}.why summary{{cursor:pointer;color:#9aa3ad;font-size:13px}}.why p.combined{{margin:8px 0 0;font-size:13px;color:#aeb6bf}}
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
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:99;padding:24px;text-align:center;cursor:zoom-out}}
#lb img{{max-width:96%;max-height:92vh;border:2px solid #fff;border-radius:6px}}
@media(max-width:560px){{.pagination{{gap:8px}}.page-link{{font-size:12px}}.page-summary{{font-size:11px}}}}
</style></head><body>
<header><h1>{_html.escape(title)}</h1>
<div class="sub">{total}/{total} analyzed · <span id="done">{done0}</span>/{total} reviewed · {attention0} need attention · every Submit writes to the source tab</div></header>
<div class="wrap">{pagination}{cards}</div>
<div class="bulk-review">
  <div class="help"><strong>Done reviewing?</strong> Submit every unreviewed card{' on this page' if pagination_enabled else ''} that has an HVAC system selected (or <em>None</em>) and {fit_help}. Incomplete cards are skipped so nothing is guessed.</div>
  <button type="button" id="submit-all" class="bulk-submit" onclick="submitAll()">Submit all completed{' on this page' if pagination_enabled else ''}</button>
  <span id="bulk-status" class="bulk-status"></span>
</div>
<div id="lb" onclick="this.style.display='none'"><img id="lbi"></div>
<script>
const WEBHOOK={wh}, JOB={jid}, CSRF_TOKEN={csrf}, REVIEW_SCHEMA={schema_json};let done={done0};
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
document.querySelectorAll('.carousel').forEach(setCap);
async function submitCard(btn){{
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
  if(REVIEW_SCHEMA==='dual_product_fit_v1' && (!fits.optimizer_fit || !fits.periscope_fit)){{
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
      if(!r.ok)throw new Error('HTTP '+r.status);
      if(reply.sheet==='error' || reply.sheet==='row_not_found'){{
        status.textContent=reply.sheet==='row_not_found'
          ? 'saved locally; Sheet row not found - retry'
          : 'saved locally; Sheet update failed - retry';
        status.className='status err';btn.disabled=false;return 'sheet_error';
      }}
    }}
    else{{console.log('[DEMO] would POST to n8n:',payload);await new Promise(r=>setTimeout(r,250));}}
    status.textContent='✓ saved: '+payload.hvac_systems+(payload.fit?' · Fit: '+payload.fit:'')+(payload.optimizer_fit?' · Optimizer: '+payload.optimizer_fit:'')+(payload.periscope_fit?' · Periscope: '+payload.periscope_fit:'')+(WEBHOOK?'':' (demo)');status.className='status ok';
    const wasReviewed=card.dataset.reviewed==='1';
    card.classList.add('done');card.classList.remove('writeback-error');
    card.dataset.reviewed='1';card.querySelectorAll('.chip,.fitchip,.note').forEach(c=>c.disabled=true);
    if(!wasReviewed){{done++;document.getElementById('done').textContent=done;}}
    const group=card.closest('.tab-group');
    if(group&&!wasReviewed){{const progress=group.querySelector('.tab-progress');
      const totalInGroup=+progress.dataset.total;
      const doneInGroup=(+progress.dataset.reviewed)+1;
      progress.dataset.reviewed=doneInGroup;
      const onPage=group.querySelectorAll('.card').length;
      const pageContext=onPage!==totalInGroup?' · '+onPage+' on this page':'';
      progress.textContent=totalInGroup+'/'+totalInGroup+' analyzed · '+doneInGroup+'/'+totalInGroup+' reviewed'+pageContext;}}
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
    const result=await submitCard(card.querySelector('.submit'));
    if(result==='saved')saved++;else failed++;
  }}
  bulk.disabled=false;
  const parts=[saved+' saved'];
  if(skipped)parts.push(skipped+' skipped - needs HVAC or Fit');
  if(failed)parts.push(failed+' need retry');
  setBulkStatus(parts.join(' Â· '),(skipped||failed)?'err':'ok');
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
