# Building Analysis Tool

AI-powered cooling tower detection from satellite imagery for HVAC service prioritization.

[![Built with Claude Code](https://img.shields.io/badge/Built%20with-Claude%20Code-7C3AED)](https://claude.ai/code)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

Automatically detects cooling towers on building rooftops by analyzing satellite imagery. Built for Parity Inc. to streamline identification of buildings suitable for HVAC optimization services.

**Key Features:**
- 🎯 Dual-VLM verification (Gemini + Grok consensus, one `verify_address` pass per address) of YOLO-ensemble detections, with geometric footprint filtering against OSM building polygons (NYC-planimetric / Microsoft fallbacks)
- 🗽 NYC registry-first: addresses with a BIN-matched DOHMH cooling-tower registration skip YOLO+VLM entirely (`registry_confirmed`)
- 🏗️ Construction-activity flag as a separate sales-actionable signal (independent of cooling-tower presence)
- ⚡ ~15-25s per verified address; 5-way address concurrency overlaps the VLM wait; registry/gated rows cost seconds and no VLM spend
- 💰 $5/month Railway hosting; VLM API spend additional (~$0.10-$0.30 per verified address)
- 📊 CSV/Excel input, annotated images and 15-column audit trail out — plus a stateless `POST /api/run` endpoint that returns a finished HTML audit report (drives the n8n "sheet → emailed report" workflow, see `n8n/README.md`)
- 🤖 **Built entirely with Claude Code** (AI-generated codebase)

## Quick Start

### Deploy Your Own Instance

```bash
# 1. Clone repository
git clone https://github.com/mahkuhse/parity-building-analysis-tool.git
cd parity-building-analysis-tool

# 2. Get API keys:
#    - Google Maps Platform (Geocoding + Static Maps + Address Validation)
#    - Mapbox (free tier: 50k requests/month)
#    - Google AI Studio (Gemini) and xAI (Grok) for VLM verification

# 3. Deploy to Railway (first month free, then $5/month)
# Visit https://railway.app
# - New Project → Deploy from GitHub repo
# - Set environment variables:
#   GOOGLE_MAPS_API_KEY=<your-key>
#   MAPBOX_API_KEY=<your-token>
#   GEMINI_API_KEY=<your-key>
#   XAI_API_KEY=<your-key>
#   ANALYZE_API_KEY=<random-secret>   # only if using the /api/* + n8n path
#   PORT=8080

# 4. Upload CSV with addresses and get results
```

**Setup time:** 30-45 minutes

## Usage

### Input Format

CSV file with required `Address` column:

```csv
Address,Boro_Area,Zip
"123 Main St","Queens",11101
"456 Broadway","Manhattan",10013
```

### Results

- **Detection confidence:** High (≥70%), Needs Review (40-70%), No Detection (<40%)
- **Outputs:** Annotated satellite images with bounding boxes, CSV with clickable URLs
- **Copy-paste workflow:** Results paste directly into Google Sheets

## Technology Stack

- **Framework:** Flask (Python 3.11)
- **ML Model:** YOLO ensemble (Ultralytics, custom-trained on cooling towers; two committed weight files)
- **Verification:** Dual-VLM consensus — Gemini (default `gemini-3.5-flash`, Google GenAI SDK) + Grok (default `grok-4.3`, xAI via OpenAI SDK), one `verify_address` pass per address. Both models must agree on the bucket and clear a confidence threshold; otherwise the result routes to `needs_review`.
- **Geometry:** Shapely point-in-polygon filtering against building footprints — OSM Overpass primary, NYC planimetric and Microsoft Building Footprints fallbacks
- **Registry:** NYC DOHMH cooling-tower registration dataset (registry-first BIN match short-circuits YOLO+VLM)
- **Geocoding:** Google primary (with Address Validation gate); Mapbox cross-check/fallback; Nominatim agreement flag
- **Imagery:** Google Static Maps by default; Mapbox Static Images in dense urban cores and as the unusable-image retry (768x768, detail z19 + wide z18 + close-up z20, centered on the building centroid)
- **Hosting:** Railway.app
- **Database:** SQLite (ephemeral job queue)

## Built with Claude Code

**This entire project was developed using Claude Code, Anthropic's AI development tool.**

All application code (~2,000 lines), architecture decisions, accuracy optimizations, and deployment configurations were generated through natural language conversations with Claude Code. The only manual work was training the YOLO model on ~200 labeled images.

**Future development can continue using Claude Code** - no traditional programming required for maintenance, debugging, or feature additions.

Learn more: https://claude.ai/code

## Architecture

```
User Upload (CSV/Excel) → Flask → SQLite Queue → Background Worker
   — or —  n8n / curl → POST /api/run (stateless, X-API-Key)
                                                ↓
                          Geocode (Google) + Address Validation
                          + Mapbox/Nominatim cross-check
                                                ↓
                        Footprint: OSM Overpass → NYC planimetric
                                 → Microsoft Building Footprints
                                                ↓
                     Gates: footprint_missing / ambiguous_footprint /
                            likely_residential (<200 m²) — no VLM spend
                                                ↓
                     NYC registry-first (DOHMH BIN match) ──→ registry_confirmed
                                                ↓                (skip YOLO+VLM)
                  Centroid-Centered Tiles (detail z19 + wide z18;
                  dense cores: Mapbox imagery, roof-only, no wide tile)
                                                ↓
                     YOLO Ensemble on both tiles → IoU + geo dedupe
                                                ↓
                   Marked tiles (footprint + numbered boxes) + z20 close-up
                                                ↓
                     ONE verify_address dual-VLM pass (Gemini ∥ Grok)
                     → image_unusable? retry on alternate imagery once
                     → construction? force needs_review
                                                ↓
                     Annotated Images + 15-Column CSV / audit-card HTML
```

**Key design constraint:** Both Gemini and Grok must independently agree on the verdict bucket (positive / negative) AND clear the consensus confidence threshold (default 0.7). Any disagreement or low confidence → `needs_review`.

## Model & Design Rationale

**Training:**
- Dataset: ~200 rooftop images (NYC buildings)
- Framework: YOLO26m (custom-trained from Ultralytics)
- Training: 50-100 epochs
- Resources:
  - [YOLO Training Tutorial Video](https://www.youtube.com/watch?v=r0RspiLG260)
  - [Train YOLO Models Guide](https://www.ejtech.io/learn/train-yolo-models)

**Design choices (Phase 3):**
- **Low YOLO confidence threshold (0.18) by design.** YOLO is treated as a recall-oriented candidate generator; the dual-VLM pass filters false positives downstream. Lowering the threshold catches more candidates that the heuristic-era pipeline (0.40) would have dropped.
- **Dual-VLM consensus replaces neighbor-FP heuristics.** Instead of center-crop and distance-filter rules (the NYC-era approach), the system now uses geometric point-in-polygon filtering against OSM footprints PLUS independent verification from two VLMs that must agree.
- **`verify_rooftop` as last-line-of-defense.** When YOLO returns zero candidates inside the building footprint, the system scans the full tile with the VLMs in rooftop-mode — catching cooling towers the YOLO pass missed.

**Accuracy:** Not yet benchmarked at production scale. The design tradeoffs above are validated against fixture data (`test_vlm.py`, `test_pipeline_no_vlm.py`) and design-reviewed prompt rules; national-scale precision/recall measurements are pending.

## Design Choices

The current pipeline replaced an earlier heuristic system (center-crop, multi-scale, distance-filter) with a smaller set of stronger primitives:

**Active design choices:**
- ✅ **Geometric footprint filter.** OSM Overpass polygon + Shapely point-in-polygon. Deterministic attribution: a detection is either inside the target building's footprint or it isn't. Replaces NYC-era center-crop and distance-filter heuristics.
- ✅ **Dual-VLM consensus.** Independent verification from Gemini + Grok. Both must agree on the bucket and clear a confidence threshold; any divergence → `needs_review`. Filters false positives that geometric attribution alone wouldn't catch.
- ✅ **One `verify_address` pass per address.** The marked tile (footprint + numbered YOLO boxes) plus a z20 close-up go to both VLMs in a single consensus call that verifies the boxes AND scans for anything YOLO missed — replacing the per-box `verify_detection` / whole-tile `verify_rooftop` split (both retained in `vlm.py` for tests).
- ✅ **Registry-first for NYC.** A BIN-matched DOHMH cooling-tower registration confirms the address with zero YOLO/VLM spend.
- ✅ **Cheap gates before spend.** Missing/ambiguous footprints and sub-200 m² residential buildings route to review verdicts before any tile fetch or VLM call.
- ✅ **Construction flag as a separate signal.** The VLMs report construction activity independently of cooling-tower presence; a construction-flagged verdict is forced to `needs_review` (the imagery may predate the current building state).
- ✅ **Ground-mounted equipment support.** Prompts and verdict logic accept cooling equipment on adjacent concrete pads or mechanical yards (within ~30 ft of the target footprint). Critical for non-urban markets where rooftop deployment is rare.

**Removed in Phase 3:**
- ❌ Center-crop analysis — superseded by footprint filter
- ❌ Multi-scale dual-pass — superseded by footprint filter
- ❌ Distance-based filtering — superseded by footprint filter
- ❌ Aggressive crop mode — superseded by footprint filter

See [HANDOVER.md](HANDOVER.md) for the historical Phase 1-2 experiments that informed these choices.

## Cost

**Monthly operating cost: $5**

- Railway: $5/month Hobby plan (first month free with $5 credit)
- Mapbox: Free tier (50k requests/month)
- Current usage: ~500 addresses/month (~$0.50 compute)

Scales to 5,000 addresses/month at flat $5/month.

## Documentation

- **[HANDOVER.md](HANDOVER.md)** - Comprehensive quickstart guide for team takeover
- **[CLAUDE.md](CLAUDE.md)** - Technical documentation for developers
- **[RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md)** - Deployment guide
- **[ACCURACY_IMPROVEMENTS.md](ACCURACY_IMPROVEMENTS.md)** - Accuracy tuning details

## Configuration

Environment variables (Railway dashboard → Variables):

**Required:**
- `GOOGLE_MAPS_API_KEY` — Google Geocoding + Static Maps + Address Validation (geocoder and imagery default to Google)
- `MAPBOX_API_KEY` — Mapbox geocoding cross-check + Static Images (dense cores, imagery retry)
- `GEMINI_API_KEY` — Google AI Studio API key for Gemini verification
- `XAI_API_KEY` — xAI API key for Grok verification
- `ANALYZE_API_KEY` — shared secret for the stateless `/api/*` routes (n8n path); those routes return 503 without it

**Optional — Server:**
- `PORT` — Server port (default: 8080)

**Optional — YOLO tuning:**
- `YOLO_CONF` — YOLO confidence threshold (default: 0.18). Lower = more recall, more candidates routed to dual-VLM verification.
- `MODEL_PATHS` — comma-separated ensemble weight paths (default: `models/rooftop_model.pt`)

**Optional — VLM tuning:**
- `GEMINI_MODEL` — Gemini model ID (default: `gemini-3.5-flash`)
- `GROK_MODEL` — Grok model ID (default: `grok-4.3`)
- `GEMINI_THINKING_LEVEL` / `GROK_REASONING_EFFORT` — reasoning depth (default: `high` for both)
- `VLM_TIMEOUT_SECONDS` — Per-VLM wall-clock timeout (default: 120)
- `VLM_CONSENSUS_THRESHOLD` — Minimum confidence both models must clear for consensus (default: 0.7). Below this → `needs_review`.
- `VLM_ADDRESS_CONCURRENCY` — addresses processed at once (default: 5)

**Optional — Imagery:**
- `MAPBOX_ZOOM` — detail-tile zoom (default: 19); `MAPBOX_ZOOM_WIDE` — wide tile (default: 18); `CLOSEUP_ZOOM` — VLM close-up (default: 20)
- `MAPBOX_SIZE` — Tile dimensions (default: `768x768`)
- `MAPBOX_DPI` — @2x retina tiles (default: false). Note: env var name is `MAPBOX_DPI`, not `MAPBOX_HIGH_DPI`.

**Optional — Geocoding and Footprint:**
- `GEOCODER_PROVIDER` / `IMAGERY_PROVIDER` — `google` (default) or `mapbox`
- `GEOCODE_CONFIDENCE_THRESHOLD` — Mapbox relevance threshold before falling back to Nominatim (default: 0.70)
- `FOOTPRINT_SEARCH_RADIUS` — footprint search radius in meters (default: 50). Increase for non-urban / suburban markets.

See [CLAUDE.md](CLAUDE.md) for the full variable reference (gates, retries, fallback toggles).

## File Structure

```
├── app_railway.py              # Flask app entrypoint, routes, worker init
├── api_analyze.py              # Stateless /api/* blueprint (n8n path, X-API-Key auth)
├── worker.py                   # Background job processor (daemon thread)
├── job_queue.py                # SQLite job queue (thread-safe) + monthly limit
├── storage_helpers.py          # File storage abstraction (local FS)
├── tasks_local.py              # Per-address pipeline + concurrent orchestrator
├── utils.py                    # Geocoding, Address Validation, imagery, YOLO loader
├── geometry.py                 # Footprint lookup chain + Shapely filter
├── nyc_opendata.py             # NYC DOHMH registry + planimetric footprints
├── ms_footprints.py            # Microsoft Building Footprints fallback
├── vlm.py                      # Dual-VLM verify_address (single consensus pass)
├── pipeline_render.py          # Marked tiles + annotated-image rendering
├── report_audit.py             # Audit-card HTML report (production, API path)
├── html_report.py              # Legacy HTML report (browser-UI jobs)
├── csv_export.py               # Headless CSV export
├── zip_bundler.py              # Result ZIP packaging
├── test_vlm.py                 # Dual-VLM integration test (3 fixtures)
├── test_pipeline_no_vlm.py     # Geometry + YOLO regression test (no VLM)
├── test_geometry_math.py       # Footprint → pixel projection check
├── models/                     # YOLO ensemble weights (committed)
├── reference_images/           # Few-shot positive/negative examples
├── n8n/                        # n8n workflow + setup guide
├── templates/                  # HTML interface
├── Dockerfile.railway          # Container config
└── requirements_railway.txt    # Python dependencies
```

## Contributing

This project is in maintenance mode. For modifications:

**Option 1: Use Claude Code (Recommended)**
- Continue development through conversational AI
- No programming experience required

**Option 2: Traditional Development**
- Python 3.8+ required
- See [CLAUDE.md](CLAUDE.md) for technical details

## License

MIT License

## Attribution

- **Imagery:** © Maxar (via Mapbox)
- **Map Data:** © OpenStreetMap contributors
- **ML Framework:** Ultralytics YOLO (AGPL-3.0)
- **Built for:** Parity Inc.
- **Built with:** Claude Code by Anthropic

## Contact

For questions about this project, see [HANDOVER.md](HANDOVER.md) for support resources.

---

**Note:** Originally built for NYC addresses. Phase 3 added support for ground-mounted cooling equipment on non-urban buildings, expanding usable scope beyond dense urban markets. National-scale accuracy is design-validated but not yet benchmarked at production volume.
