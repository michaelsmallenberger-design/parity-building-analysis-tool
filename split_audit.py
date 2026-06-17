"""scratch_split_audit.py — per-building split audit: annotated YOLO image (footprint +
green boxes) on the LEFT; the two models' outputs on the RIGHT — Gemini in a GREEN box,
Grok in a BLACK box. Reads <prefix>_results.jsonl, embeds images base64, parses the
per-model reasoning out of the combined consensus string.

Usage: python scratch_split_audit.py [prefix]   (default scratch_run150c)
"""
import base64
import html
import json
import os
import sys

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "scratch_run150c"
JSONL = f"{PREFIX}_results.jsonl"
OUT = f"{PREFIX}_split_audit.html"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def embed(url):
    if not isinstance(url, str) or not url.startswith("/files/"):
        return None
    p = os.path.join("storage", url.replace("/files/", "", 1))
    if not os.path.exists(p):
        return None
    try:
        return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()
    except OSError:
        return None


def split_reasoning(r):
    """Pull Gemini's and Grok's own reasoning out of the combined consensus string.
    Consensus form: 'Consensus (..). Gemini: <g> Grok: <k>'. Disagreement form has
    'Gemini detail: <g> Grok detail: <k>' (preferred when present)."""
    r = str(r or "")

    def grab(model, other):
        marker = f"{model} detail:" if f"{model} detail:" in r else (f"{model}:" if f"{model}:" in r else None)
        if not marker:
            return ""
        seg = r.split(marker, 1)[1]
        for om in (f"{other} detail:", f"{other}:"):
            if om in seg:
                seg = seg.split(om, 1)[0]
        return seg.strip()

    return grab("Gemini", "Grok"), grab("Grok", "Gemini")


def pct(v):
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


POSITIVE = {"confirmed", "likely", "registry_confirmed", "cooling_tower_present", "cooling_tower_possible"}


def main():
    rows = [json.loads(l) for l in open(JSONL, encoding="utf-8")]
    cards = []
    for e in rows:
        img = embed(e.get("result_image_url")) or embed(e.get("original_image_url"))
        img_html = f'<img src="{img}">' if img else '<div class="noimg">no image (gated / no tile)</div>'
        gem_r, grok_r = split_reasoning(e.get("reasoning"))
        notes = str(e.get("notes") or "")
        flags = ""
        if "IMAGERY RETRY" in notes:
            flags += '<span class="flag">⟳ Mapbox imagery retry</span>'
        if "GEOCODER FALLBACK" in notes:
            flags += '<span class="flag">⟳ Mapbox geocoder</span>'
        verdict = e.get("verdict") or "—"
        vcls = "vpos" if verdict in POSITIVE else "vneg"
        err = f'<div class="err">{html.escape(str(e.get("error")))}</div>' if e.get("error") else ""
        cards.append(f"""
      <div class="card">
        <div class="head">
          <span class="addr">{html.escape(str(e.get('address','')))}</span>
          <span class="verdict {vcls}">{html.escape(verdict)}</span>{flags}
        </div>
        <div class="body">
          <div class="imgcol">{img_html}</div>
          <div class="modelcol">
            <div class="mbox gemini">
              <div class="mh">GEMINI &nbsp;·&nbsp; {html.escape(str(e.get('gemini_verdict') or '—'))} @ {pct(e.get('gemini_confidence'))}</div>
              <div class="mr">{html.escape(gem_r) or '<i>—</i>'}</div>
            </div>
            <div class="mbox grok">
              <div class="mh">GROK &nbsp;·&nbsp; {html.escape(str(e.get('grok_verdict') or '—'))} @ {pct(e.get('grok_confidence'))}</div>
              <div class="mr">{html.escape(grok_r) or '<i>—</i>'}</div>
            </div>
            <div class="notes">Notes: {html.escape(notes)}</div>{err}
          </div>
        </div>
      </div>""")

    style = """
      body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#f4f5f7;margin:22px;color:#1a1a1a}
      h1{font-size:20px}
      .card{background:#fff;border:1px solid #e0e0e0;border-radius:10px;margin-bottom:18px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06)}
      .head{display:flex;align-items:center;gap:12px;padding:11px 16px;border-bottom:1px solid #eee}
      .addr{font-weight:600;font-size:15px}
      .verdict{color:#fff;padding:2px 10px;border-radius:5px;font-size:12px;font-weight:600}
      .verdict.vpos{background:#0a7f30}.verdict.vneg{background:#9aa0a6}
      .flag{background:#eef4ff;color:#1a4e9e;border:1px solid #cfe0ff;border-radius:5px;padding:2px 8px;font-size:11.5px;margin-left:4px}
      .body{display:grid;grid-template-columns:minmax(360px,1fr) minmax(360px,1fr);gap:0}
      .imgcol{border-right:1px solid #eee}
      .imgcol img{width:100%;display:block}
      .noimg{padding:80px 20px;text-align:center;color:#999;background:#fafafa}
      .modelcol{padding:14px;display:flex;flex-direction:column;gap:12px}
      .mbox{border-radius:8px;padding:10px 12px}
      .mbox.gemini{background:#e6f4ea;border:2px solid #0a7f30}
      .mbox.gemini .mh{color:#0a7f30;font-weight:700;font-size:13px;margin-bottom:5px}
      .mbox.grok{background:#1a1a1a;border:2px solid #000;color:#f0f0f0}
      .mbox.grok .mh{color:#fff;font-weight:700;font-size:13px;margin-bottom:5px}
      .mr{font-size:13px;white-space:pre-wrap}
      .notes{font-size:12px;color:#555;margin-top:2px}
      .err{margin-top:6px;background:#fdecea;color:#b00020;padding:4px 8px;border-radius:4px;font-size:12px}
    """
    out_html = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Split audit — {PREFIX}</title>"
        f"<style>{style}</style></head><body>"
        f"<h1>Model split — {PREFIX} ({len(rows)} buildings)</h1>"
        f"<p style='color:#555'>Left: YOLO detections (green) + OSM footprint (red). "
        f"Right: <b style='color:#0a7f30'>Gemini (green)</b> and <b>Grok (black)</b>, each with its own verdict + reasoning.</p>"
        f"{''.join(cards)}</body></html>"
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(out_html)
    print(f"Wrote {os.path.abspath(OUT)} ({len(rows)} cards)")
    try:
        os.startfile(os.path.abspath(OUT))
    except Exception:
        pass


if __name__ == "__main__":
    main()
