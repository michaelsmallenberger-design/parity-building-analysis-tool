"""Reusable audit-card HTML report generator (EMAIL-SAFE).

Produces a single self-contained HTML string from a list of pipeline web_entry
dicts (the shape built by tasks_local._build_web_entry): per-verdict sections,
each address rendered as a card with the embedded annotated image, a maps link,
and BOTH VLM models' reasoning (Gemini in green, Grok in black).

EMAIL COMPATIBILITY (why this looks the way it does):
Email clients (Gmail, Outlook, Apple Mail) are NOT browsers. Outlook renders with
the Microsoft Word engine and ignores CSS `display:flex` / `display:grid`; Gmail
clips and partially strips `<style>` blocks. So this renderer follows email-HTML
discipline:
  * Layout is nested <table role="presentation">, not flex/grid.
  * Every visual style is INLINE on the element — not in a <style> block.
  * Fixed pixel widths + width attributes so columns hold in Outlook.

Two interactive touches degrade gracefully:
  * Click-to-expand image: a pure-CSS `:target` lightbox. The overlay is
    `display:none` INLINE (so email keeps it hidden) and only revealed by a
    <style> rule (which email strips -> stays hidden). Works in a browser; inert
    and harmless in email.
  * Maps links (Google Maps / Google Earth / Bing) are plain <a href> built from
    the address text, so they work everywhere including email.

Public API:
    build_audit_report(web_results, title=...) -> str   # self-contained HTML
"""
import html
import re
import urllib.parse
from typing import Any, Dict, List, Optional

POSITIVE_VERDICTS = {
    "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
    "registry_confirmed",
}
NEGATIVE_VERDICTS = {"not_detected", "neighbor_only", "no_cooling_tower"}
AMBIGUOUS_VERDICTS = {"needs_review"}

VERDICT_ORDER = [
    "registry_confirmed", "confirmed", "likely",
    "cooling_tower_present", "cooling_tower_possible",
    "needs_review",
    "no_cooling_tower", "not_detected", "neighbor_only",
    "footprint_missing",
    "",  # error / empty rows
]

VERDICT_COLOR = {
    "registry_confirmed": "#0a7f30", "confirmed": "#0a7f30", "likely": "#2e8b57",
    "cooling_tower_present": "#0a7f30", "cooling_tower_possible": "#2e8b57",
    "needs_review": "#b8860b",
    "no_cooling_tower": "#444444", "not_detected": "#444444", "neighbor_only": "#444444",
    "footprint_missing": "#555555", "": "#b00020",
}
VERDICT_LABEL = {"": "error / no verdict"}

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_IMG_W = 340  # annotated-tile display width in px

# Per-model colors (per spec: Gemini = green, Grok = black).
_GEMINI_COLOR = "#0a7f30"
_GROK_COLOR = "#111111"

# Dark theme: map the light inline palette -> black-background palette. Single-pass
# (replacements are never re-substituted). On black, Grok's #111111 flips to light
# grey so it stays readable; Gemini green is brightened. Applied when dark=True.
_DARK_MAP = {
    "#f7f7f8": "#0b0b0b", "#ffffff": "#181818", "#fafafa": "#141414",
    "#f0fff4": "#0e2a16", "#fff3cd": "#2a2410", "#eeeeff": "#1b2330",
    "#1a1a1a": "#f2f2f2", "#111111": "#e8e8e8", "#222222": "#ececec",
    "#333333": "#d6d6d6", "#444444": "#b8b8b8", "#555555": "#a6a6a6",
    "#666666": "#9a9a9a", "#777777": "#8a8a8a", "#888888": "#808080",
    "#999999": "#777777", "#dddddd": "#333333", "#eeeeee": "#2a2a2a",
    "#e6e6e6": "#2f2f2f", "#b7e1c2": "#1f5132", "#ffe066": "#5a4a10",
    "#664d03": "#e6c463", "#0a7f30": "#2ea043", "#2e8b57": "#3fb950",
    "#b8860b": "#d9a72a", "#b00020": "#ff5c5c", "#1a56c4": "#4d9fff",
    "#bbbbbb": "#5a5a5a",
}
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _darken(html_str: str) -> str:
    """Re-skin a light report_audit HTML string to the dark theme."""
    return _HEX_RE.sub(lambda m: _DARK_MAP.get(m.group(0).lower(), m.group(0)), html_str)

# Lightbox CSS — lives in <style> (browser-only; email strips it, leaving the
# overlay's inline display:none in force so it never shows in an inbox).
_LIGHTBOX_CSS = (
    ".ctlb:target{display:block !important;position:fixed;top:0;left:0;right:0;"
    "bottom:0;background:rgba(0,0,0,0.9);z-index:9999;padding:24px;text-align:center;}"
    ".ctlb img{max-width:96%;max-height:92vh;margin:0 auto;border:2px solid #ffffff;"
    "box-shadow:0 0 40px rgba(0,0,0,0.6);}"
    ".ctlb .ctlb-hint{color:#ddd;font:12px " + _FONT + ";margin-top:10px;}"
    ".ctthumb{cursor:zoom-in;}"
)


def _fmt_conf(v: Any) -> str:
    if v is None or v == "":
        return "&mdash;"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return html.escape(str(v))


def _accent(verdict: str) -> str:
    if verdict in POSITIVE_VERDICTS:
        return "#0a7f30"
    if verdict in NEGATIVE_VERDICTS:
        return "#444444"
    if verdict in AMBIGUOUS_VERDICTS:
        return "#b8860b"
    if verdict == "footprint_missing":
        return "#555555"
    return "#b00020"


def _gmaps_url(address: str) -> str:
    """Google Maps search URL for an address (zoomable map, works in email)."""
    if not address or address == "(unknown)":
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(address)}"


def _maps_links(address: str) -> str:
    """Google Maps / Google Earth / Bing links built from the address text."""
    if not address or address == "(unknown)":
        return ""
    q = urllib.parse.quote(address)
    gmaps = f"https://www.google.com/maps/search/?api=1&query={q}"
    gearth = f"https://earth.google.com/web/search/{q}"
    bing = f"https://www.bing.com/maps?q={q}&style=h"
    a = (f'font:12px {_FONT};color:#1a56c4;text-decoration:none;')
    sep = '<span style="color:#bbbbbb;"> &nbsp;|&nbsp; </span>'
    return (
        f'<div style="margin-top:6px;font:12px {_FONT};color:#666666;">View location: '
        f'<a href="{gmaps}" target="_blank" style="{a}">Google&nbsp;Maps</a>{sep}'
        f'<a href="{gearth}" target="_blank" style="{a}">Google&nbsp;Earth</a>{sep}'
        f'<a href="{bing}" target="_blank" style="{a}">Bing&nbsp;Maps</a></div>'
    )


def _image_block(entry: Dict[str, Any], idx: int, email_mode: bool = False,
                 img_w: int = _IMG_W) -> str:
    """Image + maps links. In browser mode the image opens a CSS lightbox; in
    email_mode the image is plain (no lightbox / no duplicated overlay) so emails
    stay small and there's nothing for email clients to mis-render. img_w controls
    the displayed image width (large for the browser report, small for email)."""
    url = entry.get("result_image_url")
    address = str(entry.get("address", ""))
    if not url or not isinstance(url, str):
        thumb = (
            f'<div style="width:{img_w}px;padding:40px 0;background:#eeeeee;'
            f'text-align:center;color:#777777;font:13px {_FONT};border-radius:4px;">'
            f'no image</div>'
        )
        return thumb + _maps_links(address)

    safe = html.escape(url, quote=True)
    thumb_img = (
        f'<img src="{safe}" width="{img_w}" alt="annotated satellite tile" '
        f'style="width:{img_w}px;max-width:100%;height:auto;display:block;'
        f'border:1px solid #dddddd;border-radius:4px;">'
    )

    if email_mode:
        # Option 1: in email, the image itself links to the zoomable Google Map
        # (email can't do an in-page lightbox, but a plain link works everywhere).
        gmaps = _gmaps_url(address)
        img_linked = (
            f'<a href="{gmaps}" target="_blank" style="text-decoration:none;">{thumb_img}</a>'
            if gmaps else thumb_img
        )
        caption = (
            f'<div style="margin-top:4px;font:11px {_FONT};color:#999999;">'
            f'Click image to open in Google Maps &middot; red outline = footprint, '
            f'boxes = detections</div>'
        )
        return img_linked + caption + _maps_links(address)

    lb_id = f"lb{idx}"
    thumb = f'<a href="#{lb_id}" style="text-decoration:none;"><img src="{safe}" ' \
            f'width="{img_w}" alt="annotated satellite tile" class="ctthumb" ' \
            f'style="width:{img_w}px;max-width:100%;height:auto;display:block;' \
            f'border:1px solid #dddddd;border-radius:4px;"></a>'
    caption = (
        f'<div style="margin-top:4px;font:13px {_FONT};color:#999999;">'
        f'Click image to enlarge &middot; red outline = building footprint, '
        f'boxes = detections</div>'
    )
    overlay = (
        f'<a id="{lb_id}" class="ctlb" href="#_" style="display:none;">'
        f'<img src="{safe}" alt="enlarged satellite tile">'
        f'<div class="ctlb-hint">click anywhere to close</div></a>'
    )
    return thumb + caption + _maps_links(address) + overlay


def _model_block(name: str, verdict: str, conf: str, reasoning: str, color: str) -> str:
    """One model's verdict + reasoning, colored per spec (Gemini green / Grok black)."""
    rtext = reasoning or "<em style='color:#888;'>(no reasoning returned)</em>"
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:0 0 8px 0;background:#fafafa;border:1px solid #e6e6e6;'
        f'border-left:4px solid {color};border-radius:4px;"><tr><td style="padding:8px 10px;">'
        f'<div style="font:600 12px {_FONT};color:{color};margin-bottom:3px;">'
        f'{name} &mdash; {verdict} @ {conf}</div>'
        f'<div style="font:13px/1.5 {_FONT};color:{color};white-space:pre-wrap;">{rtext}</div>'
        f'</td></tr></table>'
    )


def _render_card(entry: Dict[str, Any], idx: int, email_mode: bool = False,
                 img_w: int = _IMG_W) -> str:
    addr = html.escape(str(entry.get("address", "(unknown)")))
    verdict = str(entry.get("verdict") or "")
    verdict_disp = html.escape(verdict if verdict else "(none)")
    err = entry.get("error")
    color = VERDICT_COLOR.get(verdict, "#444444")
    accent = _accent(verdict)

    gv = html.escape(str(entry.get("gemini_verdict") or "&mdash;"))
    gc = _fmt_conf(entry.get("gemini_confidence"))
    kv = html.escape(str(entry.get("grok_verdict") or "&mdash;"))
    kc = _fmt_conf(entry.get("grok_confidence"))

    # Per-model reasoning; fall back to the combined reasoning if a model's is absent.
    combined = str(entry.get("reasoning") or "")
    g_reason = html.escape(str(entry.get("gemini_reasoning") or "") or "")
    k_reason = html.escape(str(entry.get("grok_reasoning") or "") or "")
    if not g_reason and not k_reason and combined:
        g_reason = html.escape(combined)  # legacy entries: show combined under Gemini

    consensus_conf = _fmt_conf(entry.get("confidence_score"))
    det_count = entry.get("detection_count", 0)
    construction = str(bool(entry.get("construction", False))).lower()
    agreement = str(bool(entry.get("agreement", False))).lower()
    is_house = entry.get("is_house")
    notes = html.escape(str(entry.get("notes") or "")) or "&mdash;"

    badge = ""
    if err:
        badge = (
            f'<span style="display:inline-block;background:#b00020;color:#ffffff;'
            f'padding:2px 8px;border-radius:4px;font:11px {_FONT};margin-right:6px;">'
            f'{html.escape(str(err))}</span>'
        )
    house_flag = ""
    if is_house:
        house_flag = (
            f'<div style="display:inline-block;background:#fff3cd;color:#664d03;'
            f'border:1px solid #ffe066;padding:2px 8px;border-radius:4px;'
            f'font:11px {_FONT};margin:4px 0;">VLM FLAGGED AS LIKELY RESIDENTIAL (house)</div>'
        )

    small = f"font:12px {_FONT};color:#555555;"

    if verdict == "registry_confirmed":
        text_blocks = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 8px 0;background:#f0fff4;border:1px solid #b7e1c2;'
            f'border-left:4px solid #0a7f30;border-radius:4px;"><tr><td style="padding:10px 12px;">'
            f'<div style="font:600 13px {_FONT};color:#0a7f30;margin-bottom:4px;">'
            f'Confirmed via NYC cooling-tower registry</div>'
            f'<div style="font:13px/1.5 {_FONT};color:#0a7f30;">{notes}</div>'
            f'<div style="margin-top:8px;{small}">No AI detection was run for this building &mdash; the verdict '
            f'comes from the official NYC DOHMH / planimetric record matched by building ID (BIN), '
            f'so it costs zero compute.</div></td></tr></table>'
        )
    else:
        text_blocks = (
            _model_block("Gemini", gv, gc, g_reason, _GEMINI_COLOR)
            + _model_block("Grok", kv, kc, k_reason, _GROK_COLOR)
            + f'<div style="margin-top:4px;{small}">Notes: {notes}</div>'
        )

    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="border:1px solid #dddddd;border-left:6px solid {accent};background:#ffffff;
              border-radius:6px;margin:0 0 14px 0;">
  <tr><td style="padding:14px 16px;">

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td style="font:600 15px {_FONT};color:#222222;">{addr}</td>
      <td align="right" style="white-space:nowrap;">{badge}<span style="font:12px {_FONT};
          color:{color};">verdict: <b>{verdict_disp}</b></span></td>
    </tr></table>

    <div style="{small}margin:8px 0;">
      consensus conf: <b>{consensus_conf}</b> &nbsp;&middot;&nbsp;
      YOLO detections: <b>{det_count}</b> &nbsp;&middot;&nbsp;
      agreement: <b>{agreement}</b> &nbsp;&middot;&nbsp;
      construction: <b>{construction}</b>
    </div>
    {house_flag}

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
      <td width="{img_w + 14}" valign="top" style="padding-right:14px;">
        {_image_block(entry, idx, email_mode, img_w)}
      </td>
      <td valign="top">
        {text_blocks}
      </td>
    </tr></table>

  </td></tr>
</table>
"""


def build_audit_report(
    web_results: List[Dict[str, Any]],
    title: str = "Cooling Tower Analysis",
    email_mode: bool = False,
    dark: bool = False,
) -> str:
    """Build a self-contained, EMAIL-SAFE HTML report from pipeline web_entry dicts.

    email_mode=True drops the click-to-expand lightbox (and its duplicated image)
    so the email payload stays small and there's nothing for inbox clients to
    mis-handle. Use email_mode=False for the browser/demo version (clickable zoom).
    """
    web_results = list(web_results or [])
    total = len(web_results)

    # Browser report goes wide with large images; email keeps the email-safe width.
    img_w = _IMG_W if email_mode else 720
    container_attr = (
        'width="680" style="width:680px;max-width:680px;"' if email_mode
        else 'width="100%" style="width:96%;max-width:1500px;"'
    )

    counts: Dict[str, int] = {}
    for e in web_results:
        counts[str(e.get("verdict") or "")] = counts.get(str(e.get("verdict") or ""), 0) + 1

    positive = sum(c for v, c in counts.items() if v in POSITIVE_VERDICTS)
    negative = sum(c for v, c in counts.items() if v in NEGATIVE_VERDICTS)
    review = sum(c for v, c in counts.items() if v in AMBIGUOUS_VERDICTS)
    footprint_missing = counts.get("footprint_missing", 0)
    errored = counts.get("", 0)

    seen_order = [v for v in VERDICT_ORDER if counts.get(v)]
    extra = [v for v in counts if v not in VERDICT_ORDER and counts.get(v)]
    section_order = seen_order + extra

    font = _FONT
    sections = []
    card_idx = 0  # global, so lightbox ids are unique across all sections
    for verdict in section_order:
        chunk = [e for e in web_results if str(e.get("verdict") or "") == verdict]
        if not chunk:
            continue
        label = html.escape(VERDICT_LABEL.get(verdict, verdict or "(none)"))
        accent = _accent(verdict)
        cards = []
        for e in chunk:
            cards.append(_render_card(e, card_idx, email_mode, img_w))
            card_idx += 1
        sections.append(
            f'<h2 style="font:600 16px {font};color:#222222;margin:28px 0 10px 0;'
            f'padding:8px 12px;background:#eeeeff;border-left:6px solid {accent};">'
            f'Verdict: {label} <span style="color:#777777;font-weight:normal;">'
            f'({len(chunk)})</span></h2>\n' + "\n".join(cards)
        )

    def _row(lbl, val, bold=False):
        v = f"<b>{val}</b>" if bold else str(val)
        return (f'<tr><td style="padding:4px 8px;border-bottom:1px solid #eeeeee;'
                f'font:13px {font};color:#333333;">{lbl}</td>'
                f'<td style="padding:4px 8px;border-bottom:1px solid #eeeeee;'
                f'font:13px {font};color:#222222;" align="right">{v}</td></tr>')

    summary_table = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:#ffffff;border:1px solid #dddddd;border-radius:6px;">'
        + _row("Cooling tower (positive)", positive, bold=True)
        + _row("No cooling tower (negative)", negative)
        + _row("Needs review", review)
        + _row("Footprint missing", footprint_missing)
        + _row("Errors", errored)
        + _row("Total", total, bold=True)
        + "</table>"
    )

    safe_title = html.escape(title)
    head_style = "body{margin:0;padding:0;background:#f7f7f8;}"
    if not email_mode:
        head_style += " " + _LIGHTBOX_CSS
    _doc = f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>{head_style}</style>
</head>
<body style="margin:0;padding:0;background:#f7f7f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f7f7f8">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" {container_attr} cellpadding="0" cellspacing="0" border="0">

<tr><td>
  <h1 style="font:700 22px {_FONT};color:#1a1a1a;margin:0 0 4px 0;">{safe_title}</h1>
  <div style="font:13px {_FONT};color:#666666;margin-bottom:16px;">
    {total} address{'es' if total != 1 else ''} analyzed &middot; annotated satellite imagery embedded.
  </div>
  {summary_table}
</td></tr>

<tr><td>
{''.join(sections)}
</td></tr>

<tr><td style="padding:18px 4px;font:11px {_FONT};color:#999999;">
  Imagery &copy; Maxar via Mapbox / Google &middot; Map data &copy; OpenStreetMap contributors.
  Each building is independently judged by two AI vision models (Gemini + Grok);
  when they disagree the row is flagged for human review.
</td></tr>

</table>
</td></tr></table>
</body></html>
"""
    return _darken(_doc) if dark else _doc
