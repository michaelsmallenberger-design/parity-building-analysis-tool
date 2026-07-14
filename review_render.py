"""Interactive review page: dark cards with a ‹ › image carousel, map links, and
a multi-select of the HVAC system taxonomy used in the Washington Gas sheet
(Cooling Tower, Exhaust Fan, RTU, VRF, AHU, PTAC, Heat Pump, Unclear). Each Submit
POSTs {job_id,row_id,address,ai_verdict,hvac_systems,note} to an n8n webhook, which
writes the comma-joined systems back to the sheet's HVAC Systems column.

Prototype renderer — moves into the Render app once the shape is approved.
"""
import html as _html
import json
import urllib.parse

# Exact HVAC Systems data-validation dropdown from the Washington Gas sheet
# (Good fits / All Buildings / Building Analysis / Tracker), in the sheet's order.
# NOTE: 'Unclear' is NOT here — in the sheet that belongs to the separate fit/status
# column (Optimizer/Periscope/Unclear/Bad), not the HVAC Systems column.
HVAC_SYSTEMS = ["Cooling Tower", "Chiller", "Exhaust Fan", "RTU", "AHU", "PTAC", "Fan Coil", "Heat Pump", "VRF"]
# Reviewers sometimes see no relevant HVAC at all; "None" is a submittable answer,
# mutually exclusive with the systems above. Not part of the taxonomy itself.
NONE_OPTION = "None"
# Two per-product fit classifications (single choice each) — the sheet's
# "Optimizer Fit" and "Periscope Fit" dropdowns, mirroring the team's TAM
# sheet format (2026-07-13 directive; replaces the old single Fit column).
FIT_COLUMNS = ["Optimizer Fit", "Periscope Fit"]
FIT_OPTIONS = ["Good", "Bad", "Not Sure"]
# Vocabulary of the RETIRED single Fit column — still needed to recognize a
# legacy fit column in re-uploaded sheets (so it isn't mistaken for HVAC).
LEGACY_FIT_VALUES = ["Optimizer", "Periscope", "Unclear", "Bad"]
# AI verdicts that mean "cooling tower present" -> pre-check Cooling Tower for the reviewer.
_AI_POSITIVE = {"confirmed", "registry_confirmed", "likely", "cooling_tower_present"}
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
    """Gemini (green) + Grok (black) reasoning boxes; fall back to the combined
    consensus string for rows with no per-model split (registry / area-gate)."""
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
    return out


def _card(e):
    # A batch entry carries a "human" decision once reviewed (review_store); render
    # such cards in the exact state a live Submit leaves them in, so reopening the
    # page mid-batch shows what's already done.
    human = e.get("human") or {}
    reviewed = bool(human)
    picked = {s.strip() for s in str(human.get("hvac_systems", "")).split(",") if s.strip()}
    rid = _html.escape(str(e.get("i") or e.get("row_id") or ""))
    addr = str(e.get("address", ""))
    addr_e = _html.escape(addr)
    ai = str(e.get("verdict") or "")
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
            slides += (f'<img src="{v}" data-label="{label}" alt="{label}" loading="lazy" '
                       f'onclick="zoom(this.src)"{"" if n else " class=cur"}>')
            n += 1
    if not n:
        carousel = '<div class="noimg">no imagery</div>'
    else:
        arrows = ('<button type="button" class="nav prev" onclick="nav(this,-1)">‹</button>'
                  '<button type="button" class="nav next" onclick="nav(this,1)">›</button>') if n > 1 else ''
        carousel = (f'<div class="carousel" data-n="{n}" data-i="0">{arrows}'
                    f'<div class="frame">{slides}</div>'
                    f'<div class="cap"></div></div>')

    dis = " disabled" if reviewed else ""
    chips = ""
    for s in HVAC_SYSTEMS + [NONE_OPTION]:
        if reviewed:
            pre = " sel" if s in picked else ""
        else:
            pre = " sel" if (s == "Cooling Tower" and ai in _AI_POSITIVE) else ""
        chips += f'<button type="button" class="chip{pre}"{dis} data-sys="{_html.escape(s)}" onclick="toggle(this)">{_html.escape(s)}</button>'
    fit_keys = {"Optimizer Fit": "optimizer_fit", "Periscope Fit": "periscope_fit"}
    fitrows = ""
    for col in FIT_COLUMNS:
        picked_fit = human.get(fit_keys[col], "") if reviewed else ""
        chips_f = ""
        for fo in FIT_OPTIONS:
            pre = " sel" if fo == picked_fit else ""
            chips_f += (f'<button type="button" class="fitchip{pre}"{dis} '
                        f'data-fit="{_html.escape(fo)}" onclick="pickFit(this)">{_html.escape(fo)}</button>')
        fitrows += (f'<div class="label">{_html.escape(col)}:</div>'
                    f'<div class="fitchips" data-col="{fit_keys[col]}">{chips_f}</div>')

    if reviewed:
        saved = "✓ saved: " + str(human.get("hvac_systems", ""))
        for col in FIT_COLUMNS:
            v = human.get(fit_keys[col])
            if v:
                saved += f" · {col.split()[0]}: {v}"
        status = f'<span class="status ok">{_html.escape(saved)}</span>'
    else:
        status = '<span class="status"></span>'

    return f'''
<article class="card{' done' if reviewed else ''}" data-rid="{rid}" data-addr="{addr_e}" data-ai="{_html.escape(ai)}">
  <div class="head">
    <div class="addr">{addr_e}</div>
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
    <input class="note" type="text" placeholder="optional note…" value="{_html.escape(str(human.get("note", "")))}">
    <button type="button" class="submit"{dis} onclick="submitCard(this)">Submit</button>
    {status}
  </div>
</article>'''


def build_review_page(entries, job_id, webhook_url="", title="Cooling Tower Review"):
    cards = "\n".join(_card(e) for e in entries)
    total = len(entries)
    done0 = sum(1 for e in entries if e.get("human"))
    wh = json.dumps(webhook_url)
    jid = json.dumps(str(job_id))
    return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{margin:0;background:#0d0f12;color:#e6e8eb;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#12151a;border-bottom:1px solid #262b33;padding:14px 20px}}
header h1{{margin:0;font-size:17px}}header .sub{{color:#9aa3ad;font-size:13px;margin-top:2px}}
.wrap{{max-width:900px;margin:0 auto;padding:18px}}
.card{{background:#161a20;border:1px solid #262b33;border-radius:12px;padding:16px;margin:0 0 16px}}
.card.done{{border-color:#2f9e44;opacity:.85}}
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
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:99;padding:24px;text-align:center;cursor:zoom-out}}
#lb img{{max-width:96%;max-height:92vh;border:2px solid #fff;border-radius:6px}}
</style></head><body>
<header><h1>{_html.escape(title)}</h1>
<div class="sub"><span id="done">{done0}</span>/{total} reviewed · check the HVAC systems you see on each building; it writes back to the sheet</div></header>
<div class="wrap">{cards}</div>
<div id="lb" onclick="this.style.display='none'"><img id="lbi"></div>
<script>
const WEBHOOK={wh}, JOB={jid};let done={done0};
function zoom(s){{document.getElementById('lbi').src=s;document.getElementById('lb').style.display='block';}}
function setCap(c){{const i=+c.dataset.i,imgs=c.querySelectorAll('.frame img');
  imgs.forEach((im,k)=>im.classList.toggle('cur',k==i));
  c.querySelector('.cap').textContent=imgs[i].dataset.label+' · '+(i+1)+'/'+imgs.length+' · click image to enlarge';}}
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
  if(!sys.length){{status.textContent='pick at least one system (or None)';status.className='status err';return;}}
  const fits={{}};
  card.querySelectorAll('.fitchips').forEach(g=>{{const s=g.querySelector('.fitchip.sel');fits[g.dataset.col]=s?s.dataset.fit:'';}});
  const payload={{job_id:JOB,row_id:card.dataset.rid,address:card.dataset.addr,ai_verdict:card.dataset.ai,
    hvac_systems:sys.join(', '),optimizer_fit:fits.optimizer_fit||'',periscope_fit:fits.periscope_fit||'',
    note:card.querySelector('.note').value}};
  btn.disabled=true;status.textContent='saving…';status.className='status';
  try{{
    if(WEBHOOK){{const r=await fetch(WEBHOOK,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});
      if(!r.ok)throw new Error('HTTP '+r.status);}}
    else{{console.log('[DEMO] would POST to n8n:',payload);await new Promise(r=>setTimeout(r,250));}}
    status.textContent='✓ saved: '+payload.hvac_systems+(payload.optimizer_fit?' · Optimizer: '+payload.optimizer_fit:'')+(payload.periscope_fit?' · Periscope: '+payload.periscope_fit:'')+(WEBHOOK?'':' (demo)');status.className='status ok';
    card.classList.add('done');card.querySelectorAll('.chip,.fitchip').forEach(c=>c.disabled=true);
    done++;document.getElementById('done').textContent=done;
  }}catch(e){{status.textContent='✗ '+e.message+' (retry)';status.className='status err';btn.disabled=false;}}
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
