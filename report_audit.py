"""Reusable audit-card HTML report generator.

Produces a single self-contained HTML string from a list of pipeline web_entry
dicts (the shape built by tasks_local._build_web_entry): per-verdict sections,
each address rendered as a card with the embedded annotated image and both VLM
verdicts side-by-side.

This is the production successor to scratch_build_audit.py's card renderer, with
the answer-key / confusion-matrix / log-parsing stripped out (no ground truth in
production). It deliberately replaces html_report.py for the emailed deliverable:
html_report.py buckets rows by confidence percent rather than verdict literal, so
needs_review rows (confidence 0.0 by design) silently vanish and per-VLM detail is
never rendered (see scratch_build_audit.py:426-431).

Images are expected to already be embeddable: each web_entry's result_image_url /
original_image_url is either a `data:` URI (the api_analyze base64 path) or a
plain URL (rendered as an <img src> as-is). No filesystem reads happen here.

Public API:
    build_audit_report(web_results, title=...) -> str   # self-contained HTML
"""
import html
from typing import Any, Dict, List, Optional

# Verdict literals, kept in sync with tasks_local._POSITIVE_VERDICTS /
# _NEGATIVE_VERDICTS / _AMBIGUOUS_VERDICTS plus the footprint_missing special row.
POSITIVE_VERDICTS = {
    "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
    "registry_confirmed",
}
NEGATIVE_VERDICTS = {"not_detected", "neighbor_only", "no_cooling_tower"}
AMBIGUOUS_VERDICTS = {"needs_review"}

# Display order for the per-verdict sections (positives first, then undecided,
# then negatives, then special / error buckets last).
VERDICT_ORDER = [
    "registry_confirmed", "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
    "needs_review",
    "no_cooling_tower", "not_detected", "neighbor_only",
    "footprint_missing",
    "",  # error / empty rows
]

VERDICT_COLOR = {
    "registry_confirmed": "#0a7f30",
    "confirmed": "#0a7f30",
    "likely": "#2e8b57",
    "cooling_tower_present": "#0a7f30",
    "cooling_tower_possible": "#2e8b57",
    "needs_review": "#b8860b",
    "no_cooling_tower": "#444",
    "not_detected": "#444",
    "neighbor_only": "#444",
    "footprint_missing": "#555",
    "": "#b00020",
}

VERDICT_LABEL = {"": "error / no verdict"}

_STYLE = """
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color:#222; background:#f7f7f8; margin:0; padding:24px; }
h1 { margin:0 0 4px 0; }
h2 { margin: 28px 0 8px 0; padding: 8px 12px; background:#eef; border-left:6px solid #557; }
h2 small { color:#777; font-weight:normal; }
.sub { color:#666; margin-bottom:16px; }
.summary { display:grid; grid-template-columns: 1fr 1fr; gap:16px; margin:12px 0 24px; }
.summary > div { background:#fff; padding:12px 16px; border:1px solid #ddd; border-radius:6px; }
table { border-collapse: collapse; width:100%; }
td, th { padding:4px 8px; border-bottom:1px solid #eee; text-align:left; }
.card { background:#fff; border:1px solid #ddd; border-radius:6px; margin: 10px 0; padding: 12px; }
.card.pos { border-left: 6px solid #0a7f30; }
.card.neg { border-left: 6px solid #444; }
.card.rev { border-left: 6px solid #b8860b; }
.card.special { border-left: 6px solid #555; }
.card.err { border-left: 6px solid #b00020; }
.card-top { display:flex; justify-content:space-between; align-items:center; gap:12px; }
.addr { font-weight:600; font-size:15px; }
.badges { display:flex; gap:8px; align-items:center; }
.badge { color:#fff; padding:2px 8px; border-radius:4px; font-size:11px; letter-spacing:0.5px; }
.verdict { font-size:12px; }
.meta { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:#555; margin:6px 0; }
.flag { display:inline-block; background:#fff3cd; color:#664d03; border:1px solid #ffe066; padding:2px 8px; border-radius:4px; font-size:11px; margin:4px 0; }
.body { display:grid; grid-template-columns: 420px 1fr; gap:14px; margin-top:8px; }
.img-wrap img { width:100%; border:1px solid #ddd; border-radius:4px; }
.noimg { padding:30px; background:#eee; text-align:center; color:#777; border-radius:4px; }
.vlm-row { display:grid; grid-template-columns: 1fr 1fr; gap:10px; }
.vlm { background:#fafafa; border:1px solid #e3e3e3; padding:8px 10px; border-radius:4px; }
.vlm.gemini { border-left:4px solid #1a73e8; }
.vlm.grok   { border-left:4px solid #d93025; }
.vlm-head { font-size:12px; color:#333; margin-bottom:4px; }
.vlm-body { font-size:13px; color:#222; white-space:pre-wrap; }
.consensus { margin-top:8px; background:#f0f4ff; border:1px solid #d3def7; padding:8px 10px; border-radius:4px; }
.notes { margin-top:6px; font-size:12px; color:#555; }
nav a { display:inline-block; margin-right:10px; font-size:12px; color:#246; text-decoration:none; }
nav a:hover { text-decoration:underline; }
@media (max-width: 720px) { .body { grid-template-columns: 1fr; } .vlm-row { grid-template-columns: 1fr; } .summary { grid-template-columns: 1fr; } }
"""


def _fmt_conf(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return html.escape(str(v))


def _card_class(verdict: str) -> str:
    if verdict in POSITIVE_VERDICTS:
        return "pos"
    if verdict in NEGATIVE_VERDICTS:
        return "neg"
    if verdict in AMBIGUOUS_VERDICTS:
        return "rev"
    if verdict == "footprint_missing":
        return "special"
    return "err"


def _img_html(url: Optional[str]) -> str:
    """Render an <img> for a data: URI or plain URL, or a placeholder if absent."""
    if not url or not isinstance(url, str):
        return '<div class="noimg">no image</div>'
    return f'<img src="{html.escape(url, quote=True)}" alt="annotated satellite tile">'


def _render_card(entry: Dict[str, Any]) -> str:
    addr = html.escape(str(entry.get("address", "(unknown)")))
    verdict = str(entry.get("verdict") or "")
    verdict_disp = verdict if verdict else "(none)"
    err = entry.get("error")
    color = VERDICT_COLOR.get(verdict, "#444")

    gv = html.escape(str(entry.get("gemini_verdict") or "—"))
    gc = _fmt_conf(entry.get("gemini_confidence"))
    kv = html.escape(str(entry.get("grok_verdict") or "—"))
    kc = _fmt_conf(entry.get("grok_confidence"))

    consensus_conf = _fmt_conf(entry.get("confidence_score"))
    det_count = entry.get("detection_count", 0)
    construction = bool(entry.get("construction", False))
    agreement = bool(entry.get("agreement", False))
    is_house = entry.get("is_house")
    notes = html.escape(str(entry.get("notes") or ""))
    reasoning = html.escape(str(entry.get("reasoning") or ""))

    badge = ""
    if err:
        badge = f'<span class="badge" style="background:#b00020">{html.escape(str(err))}</span>'

    house_flag = (
        '<span class="flag">VLM FLAGGED AS LIKELY RESIDENTIAL (house)</span>'
        if is_house else ""
    )

    return f"""
<div class="card {_card_class(verdict)}">
  <div class="card-top">
    <div class="addr">{addr}</div>
    <div class="badges">
      {badge}
      <span class="verdict" style="color:{color}">verdict: <b>{html.escape(verdict_disp)}</b></span>
    </div>
  </div>
  <div class="meta">
    <span>consensus conf: <b>{consensus_conf}</b></span>
    <span>YOLO detections: <b>{det_count}</b></span>
    <span>agreement: <b>{str(agreement).lower()}</b></span>
    <span>construction: <b>{str(construction).lower()}</b></span>
  </div>
  {house_flag}
  <div class="body">
    <div class="img-wrap">{_img_html(entry.get("result_image_url"))}</div>
    <div class="text-wrap">
      <div class="vlm-row">
        <div class="vlm gemini">
          <div class="vlm-head">Gemini: <b>{gv}</b> @ {gc}</div>
        </div>
        <div class="vlm grok">
          <div class="vlm-head">Grok: <b>{kv}</b> @ {kc}</div>
        </div>
      </div>
      <div class="consensus">
        <div class="vlm-head">Reasoning</div>
        <div class="vlm-body">{reasoning or '<em>(none)</em>'}</div>
      </div>
      <div class="notes">Notes: {notes or '—'}</div>
    </div>
  </div>
</div>
"""


def build_audit_report(
    web_results: List[Dict[str, Any]],
    title: str = "Cooling Tower Analysis",
) -> str:
    """Build a self-contained HTML report from a list of pipeline web_entry dicts.

    Args:
        web_results: list of entries shaped like tasks_local._build_web_entry output
            (address, verdict, confidence_score, gemini_*, grok_*, reasoning, notes,
            result_image_url as a data: URI or URL, etc.).
        title: report headline.

    Returns:
        A complete, self-contained HTML document string (images embedded inline).
    """
    web_results = list(web_results or [])
    total = len(web_results)

    # Verdict tallies (counted by verdict literal — this is the html_report.py bug fix).
    counts: Dict[str, int] = {}
    for e in web_results:
        counts[str(e.get("verdict") or "")] = counts.get(str(e.get("verdict") or ""), 0) + 1

    positive = sum(c for v, c in counts.items() if v in POSITIVE_VERDICTS)
    negative = sum(c for v, c in counts.items() if v in NEGATIVE_VERDICTS)
    review = sum(c for v, c in counts.items() if v in AMBIGUOUS_VERDICTS)
    footprint_missing = counts.get("footprint_missing", 0)
    errored = sum(c for v, c in counts.items() if v == "" )

    # Sections in canonical verdict order; any unexpected verdict trails at the end.
    seen_order = [v for v in VERDICT_ORDER if counts.get(v)]
    extra = [v for v in counts if v not in VERDICT_ORDER and counts.get(v)]
    section_order = seen_order + extra

    sections = []
    nav_links = []
    for verdict in section_order:
        chunk = [e for e in web_results if str(e.get("verdict") or "") == verdict]
        if not chunk:
            continue
        label = VERDICT_LABEL.get(verdict, verdict or "(none)")
        anchor = f"v_{verdict or 'none'}"
        cards = "\n".join(_render_card(e) for e in chunk)
        sections.append(
            f'<h2 id="{anchor}">Verdict: {html.escape(label)} '
            f'<small>({len(chunk)})</small></h2>\n{cards}'
        )
        nav_links.append(f'<a href="#{anchor}">{html.escape(label)} ({len(chunk)})</a>')

    vc_rows = "".join(
        f"<tr><td>{html.escape(VERDICT_LABEL.get(v, v or '(none)'))}</td>"
        f"<td>{counts.get(v, 0)}</td></tr>"
        for v in section_order
    )

    safe_title = html.escape(title)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{_STYLE}</style>
</head><body>
<h1>{safe_title}</h1>
<div class="sub">{total} address{'es' if total != 1 else ''} analyzed · annotated satellite imagery embedded.</div>

<div class="summary">
  <div>
    <h3 style="margin:0 0 6px 0">Summary</h3>
    <table>
      <tr><th>Cooling tower (positive)</th><td><b>{positive}</b></td></tr>
      <tr><th>No cooling tower (negative)</th><td>{negative}</td></tr>
      <tr><th>Needs review</th><td>{review}</td></tr>
      <tr><th>Footprint missing</th><td>{footprint_missing}</td></tr>
      <tr><th>Errors</th><td>{errored}</td></tr>
      <tr><th>Total</th><td><b>{total}</b></td></tr>
    </table>
  </div>
  <div>
    <h3 style="margin:0 0 6px 0">Verdict breakdown</h3>
    <table><tr><th>Verdict</th><th>Count</th></tr>{vc_rows}</table>
  </div>
</div>

<nav>{' '.join(nav_links)}</nav>

{'<hr>'.join(sections)}

</body></html>
"""
