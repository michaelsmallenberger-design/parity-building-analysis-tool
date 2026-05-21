# Building Analysis Tool

AI-powered cooling tower detection from satellite imagery for HVAC service prioritization.

[![Built with Claude Code](https://img.shields.io/badge/Built%20with-Claude%20Code-7C3AED)](https://claude.ai/code)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

Automatically detects cooling towers on building rooftops by analyzing satellite imagery. Built for Parity Inc. to streamline identification of buildings suitable for HVAC optimization services.

**Key Features:**
- 🎯 Dual-VLM verification (Gemini 3.1 Pro + Grok 4.3 consensus) of YOLO26m detections, with geometric footprint filtering against OSM building polygons
- 🏗️ Construction-activity flag as a separate sales-actionable signal (independent of cooling-tower presence)
- ⚡ ~15s per address (geocode + OSM footprint + YOLO + dual-VLM verification)
- 💰 $5/month Railway hosting; VLM API spend additional (~$0.10-$0.30 per address)
- 📊 CSV input/output with annotated images and 15-column audit trail
- 🤖 **Built entirely with Claude Code** (AI-generated codebase)

## Quick Start

### Deploy Your Own Instance

```bash
# 1. Clone repository
git clone https://github.com/mahkuhse/parity-building-analysis-tool.git
cd parity-building-analysis-tool

# 2. Get Mapbox API key (free tier: 50k requests/month)
# Sign up at https://mapbox.com

# 3. Deploy to Railway (first month free, then $5/month)
# Visit https://railway.app
# - New Project → Deploy from GitHub repo
# - Set environment variables:
#   MAPBOX_API_KEY=<your-token>
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

- **Framework:** Flask (Python 3.8+)
- **ML Model:** YOLO26m (Ultralytics, custom-trained on cooling towers)
- **Verification:** Dual-VLM consensus — Gemini 3.1 Pro (Google GenAI SDK) + Grok 4.3 (xAI via OpenAI SDK). Both models must agree on the bucket and clear a confidence threshold; otherwise the result routes to `needs_review`.
- **Geometry:** Shapely point-in-polygon filtering against OSM Overpass building footprints (replaces NYC-era heuristic crops)
- **Geocoding:** Mapbox primary; Nominatim fallback for low-confidence or unrecognized Mapbox results (no geographic constraint)
- **Imagery:** Mapbox Static Images API (768x768 @ zoom 19, centered on OSM building centroid)
- **Hosting:** Railway.app
- **Database:** SQLite (ephemeral job queue)

## Built with Claude Code

**This entire project was developed using Claude Code, Anthropic's AI development tool.**

All application code (~2,000 lines), architecture decisions, accuracy optimizations, and deployment configurations were generated through natural language conversations with Claude Code. The only manual work was training the YOLO model on ~200 labeled images.

**Future development can continue using Claude Code** - no traditional programming required for maintenance, debugging, or feature additions.

Learn more: https://claude.ai/code

## Architecture

```
User Upload (CSV) → Flask → SQLite Queue → Background Worker
                                                ↓
                                         Geocode Address
                                                ↓
                                    OSM Overpass Footprint
                                                ↓
                              Centroid-Centered Satellite Tile
                                                ↓
                                       YOLO26m Detection
                                                ↓
                              Shapely Point-in-Polygon Filter
                                                ↓
                                  ┌─────────────┴─────────────┐
                                  ↓                           ↓
                       verify_detection per                verify_rooftop
                       kept candidate                       (whole tile)
                       (Gemini + Grok agree)               (zero-detection
                                  ↓                         last-line scan)
                                  └─────────────┬─────────────┘
                                                ↓
                                  Class-Rank Winner Selection
                                                ↓
                              Annotated Image + 15-Column CSV
```

**Key design constraint:** Both Gemini 3.1 Pro and Grok 4.3 must independently agree on the verdict bucket (positive / negative) AND clear the consensus confidence threshold (default 0.7). Any disagreement or low confidence → `needs_review`.

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
- ✅ **Dual-VLM consensus.** Independent verification from Gemini 3.1 Pro + Grok 4.3. Both must agree on the bucket and clear a confidence threshold; any divergence → `needs_review`. Filters false positives that geometric attribution alone wouldn't catch.
- ✅ **Construction flag as a separate signal.** The VLMs report construction activity independently of cooling-tower presence. A "no cooling tower, but construction visible" row is a sales-actionable lead for follow-up via CoStar.
- ✅ **`verify_rooftop` fallback.** When YOLO returns zero candidates inside the footprint, the VLMs scan the tile directly. Catches cooling towers YOLO whiffed on.
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
- `MAPBOX_API_KEY` — Mapbox geocoding + Static Images API token
- `GEMINI_API_KEY` — Google AI Studio API key for Gemini 3.1 Pro verification
- `XAI_API_KEY` — xAI API key for Grok 4.3 verification

**Optional — Server:**
- `PORT` — Server port (default: 8080)

**Optional — YOLO tuning:**
- `YOLO_CONF` — YOLO confidence threshold (default: 0.18). Lower = more recall, more candidates routed to dual-VLM verification.

**Optional — VLM tuning:**
- `GEMINI_MODEL` — Gemini model ID (default: `gemini-3.1-pro-preview`)
- `GROK_MODEL` — Grok model ID (default: `grok-4.3`)
- `VLM_TIMEOUT_SECONDS` — Per-VLM wall-clock timeout (default: 120)
- `VLM_CONSENSUS_THRESHOLD` — Minimum confidence both models must clear for consensus (default: 0.7). Below this → `needs_review`.

**Optional — Imagery:**
- `MAPBOX_ZOOM` — Satellite zoom level (default: 19, range: 18-20). 20+ may produce blurry tiles.
- `MAPBOX_SIZE` — Tile dimensions (default: `768x768`)
- `MAPBOX_DPI` — @2x retina tiles (default: false). Note: env var name is `MAPBOX_DPI`, not `MAPBOX_HIGH_DPI`.

**Optional — Geocoding and Footprint:**
- `GEOCODE_CONFIDENCE_THRESHOLD` — Mapbox relevance threshold before falling back to Nominatim (default: 0.70). Lowering this reduces Nominatim fallbacks.
- `FOOTPRINT_SEARCH_RADIUS` — OSM Overpass search radius in meters (default: 50). Increase for non-urban / suburban markets where the geocoded point may sit further from the building footprint.

## File Structure

```
├── app_railway.py              # Flask app entrypoint, routes, worker init
├── worker.py                   # Background job processor (daemon thread)
├── job_queue.py                # SQLite job queue (thread-safe)
├── storage_helpers.py          # File storage abstraction (local FS)
├── tasks_local.py              # Per-address pipeline orchestrator
├── utils.py                    # Geocoding, Mapbox imagery, YOLO loader
├── geometry.py                 # OSM Overpass footprint lookup + Shapely filter
├── vlm.py                      # Dual-VLM verify_detection + verify_rooftop
├── pipeline_render.py          # Annotated-image rendering (footprint + bboxes)
├── html_report.py              # Self-contained HTML report (embedded images)
├── zip_bundler.py              # Result ZIP packaging
├── test_vlm.py                 # Dual-VLM integration test (3 fixtures)
├── test_pipeline_no_vlm.py     # Geometry + YOLO regression test (no VLM)
├── models/
│   └── rooftop_model.pt        # Custom YOLO26m (44 MB, cooling-tower-trained)
├── reference_images/
│   ├── positive/               # Confirmed cooling tower examples (few-shot)
│   └── negative/               # Confirmed non-cooling-tower examples
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
