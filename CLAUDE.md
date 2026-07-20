# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A building analysis tool that uses YOLO computer vision plus dual-VLM verification to detect cooling towers on rooftops from satellite imagery. Users submit addresses (CSV upload in the browser, or a JSON list via the stateless API); the system geocodes them (Google primary, Mapbox/Nominatim fallback + cross-check), looks up the building footprint (OSM Overpass → NYC planimetric → Microsoft Building Footprints), fetches centroid-centered satellite tiles (Google Static Maps; Mapbox in dense urban cores), runs a YOLO ensemble across two zoom levels, filters detections against the footprint, and verifies each address with **one dual-VLM consensus pass** (`vlm.verify_address`, Gemini + Grok in parallel). NYC addresses with a BIN-matched cooling-tower registry record skip YOLO+VLM entirely (`registry_confirmed`).

**Deployment:** Railway.app (migrated from Google Cloud Run)
**Usage:** Low-traffic internal tool (~1000 requests/month; hard cap 2500 addresses/month via `job_queue.MONTHLY_ADDRESS_LIMIT`)
**Phase:** Phase 4 is merged to `main` and shipped: Google geocoding + imagery, one-call dual-VLM `verify_address` (replaces per-box `verify_detection`/`verify_rooftop` as the production path), OSM→NYC-planimetric→Microsoft footprint fallback chain, dense-urban roof-only gate, registry-first NYC skip, area gate, address-level concurrency with transient retry, and the stateless `/api/*` + n8n orchestration path. **Required env var: `GOOGLE_MAPS_API_KEY`** (geocoder + imagery default to `google`).

## Development Commands

### Local Development
```bash
# Install dependencies
pip install -r requirements_railway.txt

# Set required environment variables
export GOOGLE_MAPS_API_KEY="your-key"
export MAPBOX_API_KEY="your-key"
export GEMINI_API_KEY="your-key"
export XAI_API_KEY="your-key"
export ANALYZE_API_KEY="shared-secret"   # only needed for the /api/* routes

# Run application
python app_railway.py

# Or with Gunicorn (production-like)
gunicorn --bind :8080 --workers 1 --threads 8 --timeout 0 app:app
```

### Docker
```bash
# Build
docker build -f Dockerfile.railway -t building-analysis-tool .

# Run locally
docker run -p 8080:8080 \
  -e GOOGLE_MAPS_API_KEY="key" \
  -e MAPBOX_API_KEY="key" \
  -e GEMINI_API_KEY="key" \
  -e XAI_API_KEY="key" \
  building-analysis-tool

# Health check
curl http://localhost:8080/health
```

### Deployment
```bash
# Railway auto-deploys from GitHub on push to main
git push origin main

# Monitor build progress in Railway dashboard
# Build time: ~6-8 minutes (first build), ~2-3 minutes (cached)
```

## Architecture Overview

### Two front doors

There are two ways to drive the same per-address pipeline (`tasks_local._process_one_address`):

1. **Browser UI (legacy):** CSV/Excel upload → SQLite job queue → background worker → results page + downloadable report. Routes in `app_railway.py`, processing in `worker.py` → `tasks_local.py`.
2. **Stateless API (n8n path):** `api_analyze.py` (Flask blueprint, registered in `app_railway.py`) exposes `POST /api/run` (a list of addresses → finished audit-card HTML report — **the batch endpoint the n8n workflow calls**), `POST /api/analyze` (one address → self-contained `web_entry` JSON with images as `data:` URIs), `POST /api/report` (collected entries → audit-card HTML via `report_audit.py`), and `GET /api/health`. No queue, no worker, no file storage — images are base64 inline (`Base64Sink`). Guarded by the `X-API-Key` header (`ANALYZE_API_KEY`); if that env var is unset the routes return 503 (fail closed — they spend VLM money). This is what **n8n Cloud** orchestrates: Webhook → Google Sheet → Grok LLM-chain normalize → `POST /api/run` → Gmail → Slack. See `n8n/README.md` and `n8n/parity_cooling_tower.workflow.json`. The emitted report uses the **audit-card** format (`report_audit.py`), not the deprecated `html_report.py` (which buckets by confidence not verdict and hides per-VLM detail; still used by the browser-UI job report for batches ≤200).

### System Design

This is a **single-process Flask application** with a background worker thread that processes jobs from a SQLite queue. It uses **local filesystem storage** (ephemeral) instead of object storage.

```
Flask App (app_railway.py)
   ├── Routes: /, /upload, /results, /status, /cancel, /files/*, /health
   ├── API Blueprint (api_analyze.py): /api/run, /api/analyze, /api/report, /api/health
   ├── Job Queue (SQLite) — job_queue.py  (incl. 2500-address monthly usage limit)
   ├── Storage (local FS) — storage_helpers.py
   └── Background Worker Thread — worker.py
         └── Task Processor — tasks_local.py  (concurrent orchestrator, default 5 addresses at once)
               ├── utils.py            — geocoding (Google/Mapbox/Nominatim), Address Validation,
               │                          Google/Mapbox imagery, YOLO ensemble loader
               ├── geometry.py         — footprint lookup (Overpass + mirror fallback) + Shapely filter
               ├── nyc_opendata.py     — NYC DOHMH registry + DoITT planimetric footprints (BIN match)
               ├── ms_footprints.py    — Microsoft Building Footprints fallback (quadkey tile cache)
               ├── vlm.py              — dual-VLM verify_address (single consensus pass per address)
               └── pipeline_render.py  — marked tiles (VLM input) + annotated images (review output)
```

### Key Architectural Patterns

#### 1. Background Job Processing (No Cloud Tasks)

**Pattern:** SQLite-based job queue + polling worker thread (replaces Google Cloud Tasks)

- User uploads CSV → `enqueue_job()` creates row in SQLite with `status='queued'` (after `check_usage_limit` — 2500 addresses/month cap)
- Background worker polls database every 2 seconds for pending jobs
- Worker processes job → updates progress in real-time → marks as `finished`
- Frontend polls `/status/{job_id}` endpoint for progress updates (partial results stream in as they finish)

**Files:**
- `job_queue.py` — SQLite CRUD (thread-safe with locks) + monthly usage limit
- `worker.py` — `BackgroundWorker` class, starts on Flask init
- `app_railway.py` — Starts worker via `start_worker()` at module level

**Important:** Single worker only (no horizontal scaling). Worker runs in daemon thread and dies with Flask process.

#### 2. Ephemeral Local Storage (No Cloud Storage)

**Pattern:** Local filesystem with Flask serving route (replaces Google Cloud Storage)

- Files stored in `storage/` directory (created on init)
- Structure: `storage/uploads/{job_id}/`, `storage/results/{job_id}/`
- Served via `/files/<path>` route with proper MIME types
- Files persist until next Railway redeploy (ephemeral)
- The stateless `/api/*` path bypasses storage entirely (`Base64Sink` → `data:` URIs)

**Trade-off:** Acceptable for this use case because users download results immediately. Not suitable for long-term storage.

#### 3. Per-Address Pipeline (Phase 4)

**Flow:** For each address (order matters — cheap gates run before any tile fetch / YOLO / VLM spend):

1. **Geocode + agreement flag** (`utils.geocode_with_confidence`) — provider from `GEOCODER_PROVIDER` (default `google`); an independent Nominatim geocode cross-checks the point (within `GEOCODE_DIVERGENCE_THRESHOLD_M`, default 60 m → confidence `high`, else `low`). Failure → error row.
2. **Address Validation gate** (`utils.validate_address_google`) — Google Address Validation API; a non-`confirmed` verdict is surfaced in the notes and force-runs the Mapbox cross-check in step 4. Fail-open on API error. Toggle: `ADDRESS_VALIDATION_ENABLED`.
3. **Footprint lookup** (`geometry.get_building_footprint`) — Overpass (rate-limited, mirror fallback with sticky last-known-good endpoint) → Shapely polygon. Contains-point test has `GATE_TOLERANCE_M` (6 m) slack. When OSM has no usable polygon: NYC planimetric (inside NYC bbox) → Microsoft Building Footprints (`_secondary_footprint`).
4. **Geocoder fallback** — if the Google geocode yielded no/ambiguous footprint (or Address Validation flagged the address), re-geocode with Mapbox and adopt that point only when it lands *inside* a footprint; a divergence to a *different* building is flagged for review, not auto-adopted.
5. **Gates (cheapest exits):**
   - No footprint → `footprint_missing` row (tile fetched for the reviewer, no VLM).
   - Footprint is a nearest-building fallback (`contains_point=False`) → `ambiguous_footprint` (needs manual review).
   - Footprint area < 200 m² (`MIN_COMMERCIAL_FOOTPRINT_SQM`) → `likely_residential`. Master switch: `AREA_GATE_ENABLED`.
6. **Dense-urban routing** — NYC bbox (`nyc_opendata._in_nyc`) or a curated downtown box (`tasks_local.DENSE_CORE_BBOXES`: Boston, Chicago, SF, LA, DC, …) → **Mapbox imagery** (true nadir; Google's 3D photogrammetry distorts dense rooftops) and **roof-only scan** (no wide tile, off-building detections dropped as neighbor FPs). Everywhere else: Google imagery (`IMAGERY_PROVIDER` default), wide tile fetched, ground-mounted equipment kept.
7. **Registry-first (NYC)** (`nyc_opendata.lookup_nyc_registry`) — for fully-qualified addresses: DOHMH cooling-tower registry + planimetric BIN match against the footprint. Hit → `registry_confirmed`, **skip YOLO + VLM entirely**.
8. **Tiles** — detail tile (`MAPBOX_ZOOM`, default 19) + wide tile (`MAPBOX_ZOOM_WIDE`, default 18; skipped in dense cores), both centered on the **footprint centroid** (NOT the geocoded point).
9. **YOLO ensemble** (`utils._get_models`) — every model in `MODEL_PATHS` runs on both tiles at `YOLO_CONF` (default 0.18), lock-serialized (`_yolo_lock`; ultralytics `.predict()` is not thread-safe). Per-tile IoU dedupe across models (`geometry.ensemble_dedupe_detections`), then cross-zoom merge in geo-space (`geometry.geo_dedupe_detections`, ~10 m — pixel IoU is invalid across zoom levels).
10. **One dual-VLM pass** (`vlm.verify_address`) — the tile(s) are rendered with the red footprint + numbered candidate boxes (`pipeline_render.render_marked_tile`), plus a high-zoom close-up (`CLOSEUP_ZOOM`, default 20) of the strongest candidate so the VLM can make the fan-blade vs water-tank call. Gemini + Grok answer ONE consensus question — verify the boxes AND scan for anything YOLO missed.
11. **Self-correcting imagery retry** — if the VLM flags `image_unusable` (e.g. supertall shown oblique) and the verdict isn't positive, re-fetch the detail tile from the *alternate* provider, re-run YOLO + `verify_address` once, adopt if usable. Toggle: `IMAGERY_RETRY_ENABLED`.
12. **Construction override** — `construction=True` with a non-review verdict → forced to `needs_review` (the tile may predate current building state).
13. **Render + emit** — annotated detail (+ wide) images (`pipeline_render.render_annotated_image`), then `_build_web_entry` + `_build_csv_row` (15-column schema, see below).

**Concurrent orchestrator** (`tasks_local.process_address_list`): runs up to `VLM_ADDRESS_CONCURRENCY` (default 5) addresses at once to overlap the dual-VLM wait (YOLO and Overpass are lock-serialized anyway). Transient failures (throttled Overpass → `TransientFootprintError`, transient VLM verdicts detected via marker strings in reasoning) are re-queued for up to `VLM_RETRY_ROUNDS` (default 2) extra rounds with backoff; unresolved rows are emitted as explicitly-unverified `footprint_missing` — **nothing is silently dropped**. Accepts CSV and Excel; auto-detects the address column among common variants ("Property Address", "Street Address", etc.); HTML report generation is skipped for batches >200 to conserve memory.

#### 4. Lazy-Loaded ML Ensemble

**Pattern:** `@lru_cache` prevents loading YOLO at import time (avoids OOM on startup)

- `utils._get_models()` loads every path in `MODEL_PATHS` (comma-separated; defaults to the single `MODEL_PATH`) on first prediction call; cached per process.
- Weights committed in `models/`: `rooftop_model.pt` (43 MB) and `rooftop_model_prev.pt` (42 MB) — both YOLO custom-trained on cooling towers — so GitHub-sourced Railway builds are self-contained.

## Critical Configuration

### Environment Variables (Required)
- `GOOGLE_MAPS_API_KEY` — Google Geocoding + Static Maps imagery + Address Validation. Required by default since `GEOCODER_PROVIDER` and `IMAGERY_PROVIDER` default to `google`. (Set both to `mapbox` to run the legacy path without this key.)
- `MAPBOX_API_KEY` — Mapbox geocoding (fallback/cross-check) + Static Images (dense-urban-core imagery, image_unusable retry)
- `GEMINI_API_KEY` — Google AI Studio API key for Gemini verification (model via `GEMINI_MODEL`, default `gemini-3.5-flash`)
- `XAI_API_KEY` — xAI API key for Grok verification (model via `GROK_MODEL`, default `grok-4.3`)
- `ANALYZE_API_KEY` — shared secret guarding the stateless `/api/run`, `/api/analyze`, `/api/report` endpoints (the n8n path). Required for those routes (they incur VLM spend); if unset they return 503. Not needed for the browser-upload UI.

### Environment Variables (Optional)

**Server:**
- `PORT` — Server port (default: 8080)
- `UPLOAD_FOLDER` — Upload directory (default: `temp_uploads`)
- `LOG_LEVEL` — Python logging level (default: `INFO`)

**Provider selection:**
- `GEOCODER_PROVIDER` — `google` (default) or `mapbox`
- `IMAGERY_PROVIDER` — `google` (default) or `mapbox`. Dense-core addresses override to Mapbox per-address regardless.

**YOLO inference:**
- `YOLO_CONF` — YOLO confidence threshold (default: 0.18). Lower = more recall, more candidates routed to dual-VLM verification.
- `MODEL_PATH` — single YOLO model path (default: `models/rooftop_model.pt`)
- `MODEL_PATHS` — comma-separated ensemble paths (default: just `MODEL_PATH`)
- `YOLO_KEEP_OUTSIDE` — keep outside-footprint detections (ground-mounted equipment); forced off per-address in dense cores

**VLM tuning:**
- `GEMINI_MODEL` — Gemini model ID (default: `gemini-3.5-flash`)
- `GROK_MODEL` — Grok model ID (default: `grok-4.3`)
- `GROK_REASONING_EFFORT` — Grok reasoning depth: `none`/`low`/`medium`/`high` (default: `high`). xAI's own default is `low`; raised here for accuracy. Note: the `grok-*-fast` model variants reject this param (HTTP 400).
- `GEMINI_THINKING_LEVEL` — Gemini thinking depth: `minimal`/`low`/`medium`/`high` (default: `high`). Google's own default is `medium`. Replaces the deprecated `thinking_budget`; Gemini 3.x only.
- `VLM_TIMEOUT_SECONDS` — Per-VLM wall-clock timeout in seconds (default: 120)
- `VLM_CONSENSUS_THRESHOLD` — Minimum confidence both models must clear for consensus (default: 0.7). Below this → `needs_review`.

**Concurrency & retries:**
- `VLM_ADDRESS_CONCURRENCY` — addresses processed at once (default: 5)
- `VLM_RETRY_ROUNDS` — extra rounds for transient failures (default: 2)
- `VLM_RETRY_BACKOFF` — seconds of backoff per retry round (default: 5)

**Imagery:**
- `MAPBOX_ZOOM` — detail-tile zoom (default: 19, range 18-20; 20+ may blur)
- `MAPBOX_ZOOM_WIDE` — wide-tile zoom (default: 18). Each non-dense address fetches **two** centroid-centered tiles so YOLO can catch ground-mounted cooling equipment outside the detail frame; detections are merged across zooms in geo-space. Each VLM verification also receives the opposite-zoom tile as cross-zoom context.
- `CLOSEUP_ZOOM` — high-zoom equipment close-up fed to `verify_address` (default: 20)
- `MAPBOX_SIZE` — tile dimensions (default: `768x768`)
- `MAPBOX_DPI` — @2x retina tiles (default: false). Note: env var name is `MAPBOX_DPI`; the Python variable in utils.py is `MAPBOX_HIGH_DPI`.
- `MAPBOX_CROP_BOTTOM_PX` — trim N pixels from bottom of fetched Mapbox tile (default: 0; obsolete since `logo=false`/`attribution=false`)
- `GOOGLE_WATERMARK_PX` — mask the bottom strip of Google tiles this many px (default: 40) — YOLO detects the baked-in logo/credit text as equipment
- `IMAGERY_RETRY_ENABLED` — image_unusable alternate-provider retry (default: on)

**Geocoding:**
- `GEOCODE_CONFIDENCE_THRESHOLD` — Mapbox relevance threshold before falling back to Nominatim (default: 0.70)
- `GEOCODE_CONFIDENCE` — geocoder-agreement cross-check via Nominatim (default: on; set `0` to skip the extra call)
- `GEOCODE_DIVERGENCE_THRESHOLD_M` — agreement distance for the `high`/`low` confidence flag (default: 60)
- `GEOCODER_FALLBACK_ENABLED` — Mapbox re-geocode when Google's point yields no/ambiguous footprint (default: on)
- `ADDRESS_VALIDATION_ENABLED` — Google Address Validation gate (default: on; fail-open)

**Footprint:**
- `OVERPASS_URL` — primary Overpass endpoint (default: `https://overpass-api.de/api/interpreter`)
- `OVERPASS_URLS` — comma-separated mirror list (default: primary + `overpass.openstreetmap.fr`); a sticky pointer remembers the last-known-good endpoint
- `OVERPASS_TIMEOUT` — Overpass query timeout in seconds (default: 15)
- `FOOTPRINT_SEARCH_RADIUS` — search radius in meters (default: 50). Increase for non-urban / suburban markets.
- `GATE_TOLERANCE_M` — contains-point slack in meters (default: 6.0) — geocoders/OSM/imagery each carry a few meters of error
- `FOOTPRINT_FALLBACK_ENABLED` — NYC-planimetric / Microsoft fallback chain (default: on)
- `MS_FOOTPRINT_CACHE_DIR` — Microsoft footprint tile cache directory (default: `.ms_footprint_cache/` next to the module)

**Gates:**
- `AREA_GATE_ENABLED` — area gate master switch (default: on). Set `0` so every non-registry address gets VLM eyes — safer recall at higher VLM cost.

### Important Railway Settings
- **Dockerfile:** `Dockerfile.railway` (uses CPU-only PyTorch for faster builds)
- **Workers:** 1 worker, 8 threads (single process shares SQLite safely)
- **Timeout:** 0 (disabled — jobs can take 30+ minutes)
- **Health Check:** `/health` endpoint, 300s timeout

### File Locations
- **App entrypoint:** `app_railway.py` (note: copied to `app.py` in Docker for import compatibility)
- **Requirements:** `requirements_railway.txt` (GCP dependencies removed)
- **Config:** `railway.json` (points to Dockerfile.railway)

## Code Organization & Interactions

### Component Responsibilities

| File | Responsibility | Key Dependencies |
|------|----------------|------------------|
| `app_railway.py` | Flask routes, worker init, API blueprint registration | job_queue, storage_helpers, worker, api_analyze |
| `api_analyze.py` | Stateless `/api/*` blueprint (n8n path): `run`, `analyze`, `report`, `health`; `Base64Sink`; `X-API-Key` auth | tasks_local, report_audit, pandas |
| `worker.py` | Background job processor (daemon thread) | job_queue, storage_helpers, tasks_local |
| `job_queue.py` | SQLite job queue (thread-safe) + monthly usage limit (2500) | None (stdlib only) |
| `storage_helpers.py` | File storage abstraction (local FS) | None (stdlib only) |
| `tasks_local.py` | Per-address pipeline (`_process_one_address_core`) + concurrent orchestrator (`process_address_list`) + gates, dense-core boxes, entry/CSV builders | utils, geometry, nyc_opendata, vlm, pipeline_render, html_report, shapely, pandas |
| `utils.py` | Geocoding (Google/Mapbox/Nominatim + agreement flag), Address Validation, Google/Mapbox imagery, YOLO ensemble loader | requests, geopy, PIL, ultralytics |
| `geometry.py` | Footprint lookup (Overpass + mirrors, planimetric/MS fallback), Shapely classification, pixel ↔ lat/lon (Web Mercator), ensemble + geo dedupe | requests, shapely, nyc_opendata, ms_footprints |
| `nyc_opendata.py` | NYC DOHMH cooling-tower registry + DoITT planimetric footprints, BIN matching, `_in_nyc` bbox | requests, geopy, shapely |
| `ms_footprints.py` | Microsoft Building Footprints fallback (quadkey tiles, on-disk cache) | requests, shapely |
| `vlm.py` | Dual-VLM verification — **`verify_address`** (production: one consensus pass per address); legacy `verify_detection`/`verify_rooftop` retained for tests | google-genai, openai, httpx, PIL, pydantic |
| `pipeline_render.py` | `render_marked_tile` (VLM input: footprint + numbered boxes) + `render_annotated_image` (review output) | cv2, numpy, shapely |
| `report_audit.py` | Audit-card HTML report (per-verdict sections, per-VLM detail) — **the production report** for the API/n8n path | None (stdlib only) |
| `html_report.py` | Legacy self-contained HTML report (confidence buckets) — browser-UI job report only | None (stdlib only) |
| `csv_export.py` | Headless CSV export from `web_results` (test/headless runners) | tasks_local |
| `split_audit.py` | Dev tool: per-building split audit HTML from scratch-run JSONL | None (stdlib only) |
| `zip_bundler.py` | Result ZIP packaging (HTML + CSV + images) | None (stdlib only) |
| `test_vlm.py` | Dual-VLM integration test against 3 fixtures (real API spend) | utils, vlm, ultralytics |
| `test_pipeline_no_vlm.py` | Geometry + YOLO regression test (no VLM, no API spend) | utils, geometry, cv2, ultralytics |
| `test_geometry_math.py` | Visual check of footprint → pixel projection on a fixture tile | utils, geometry, PIL |

### Data Flow

```
1. Upload CSV → app_railway.py:/upload
    ├── check_usage_limit() → reject if over 2500/month
    ├── storage_helpers.upload_file() → storage/uploads/{job_id}/file.csv
    └── job_queue.enqueue_job() → SQLite row (status='queued')

2. Worker picks up job → worker.py:_worker_loop()
    └── tasks_local.process_address_list()   ← concurrent, default 5 at once
        FOR EACH address (in _process_one_address_core):
           ├── geocode_with_confidence() → (lat, lon, agreement flag)
           ├── validate_address_google() → address-quality note (fail-open)
           ├── get_building_footprint() → OSM → planimetric → MS chain
           ├── geocoder fallback (Mapbox point) if missing/ambiguous
           ├── GATES: footprint_missing | ambiguous_footprint | likely_residential
           ├── dense-core routing → Mapbox imagery + roof-only (else Google + wide tile)
           ├── lookup_nyc_registry() → registry_confirmed? skip YOLO+VLM
           ├── YOLO ensemble on detail+wide tiles → dedupe (IoU per-tile, geo cross-zoom)
           ├── render_marked_tile(s) + close-up tile (z20)
           ├── vlm.verify_address() → single dual-VLM consensus verdict
           ├── image_unusable? → alternate-provider retry once
           ├── construction? → needs_review
           ├── render_annotated_image(s) → storage/results/{job_id}/
           └── _commit() → progress + partial-result streaming
        Transient failures → retry rounds → explicit unverified rows (no silent drops)

3. Frontend polls → app_railway.py:/status/{job_id}
    └── job_queue.get_job_status() + read_result() → {status, progress, result}

4. Serve files → app_railway.py:/files/<path>

--- OR (stateless) ---

n8n → POST /api/run {addresses:[...]} → per-address pipeline with Base64Sink
    → report_audit.build_audit_report() → text/html response (images inline)
```

### Verdict Vocabulary

- **Positive:** `registry_confirmed` (NYC registry hit, no VLM), `confirmed`, `likely`, `cooling_tower_present`, `cooling_tower_possible`
- **Ambiguous:** `needs_review` (VLM disagreement, low confidence, transient VLM failure, or construction override)
- **Negative:** `not_detected`, `neighbor_only`, `no_cooling_tower`
- **Gate/error rows (no VLM spend):** `footprint_missing`, `ambiguous_footprint`, `likely_residential`, plus `error`-tagged rows (geocode failed, imagery failed, empty address)

### CSV Schema (15 columns)

`Address, Detected, Confidence, Verdict, Detection_Count, Construction, Reasoning, Notes, Agreement, Gemini_Verdict, Gemini_Confidence, Grok_Verdict, Grok_Confidence, Original_Image, Detection_Result` — built by `tasks_local._build_csv_row`; `web_entry` additionally carries `geocode_confidence` / `geocode_divergence_m`.

## External API Usage

### Geocoding Strategy

**Primary: Google Geocoding** (`GEOCODER_PROVIDER=google`, the default) — plus the Google **Address Validation** API as an upstream quality gate (fail-open).

**Cross-checks & fallbacks:**
- **Geocoder-agreement flag:** an independent Nominatim geocode corroborates the point; divergence > 60 m (or no match) → confidence `low` in the output (worth a manual look, not necessarily wrong).
- **Mapbox re-geocode fallback:** when the primary point yields no footprint or an ambiguous one, the Mapbox geocode of the same address is tried and adopted only if it lands inside a building.
- **Legacy Mapbox-primary path** (`GEOCODER_PROVIDER=mapbox`): query variations (original / cleaned / street-only) against Mapbox, Nominatim fallback below `GEOCODE_CONFIDENCE_THRESHOLD` (0.70), last-resort low-confidence Mapbox result.
- Nominatim is rate-limited to 1 req/s (thread-safe, serialized state).

### Footprint Lookup Chain
1. **OSM Overpass** — `way["building"](around:radius,lat,lon)` + relation variant; 50 m default radius; 1 req/s lock; 4 retries with backoff on 429/5xx; automatic mirror fallback (`OVERPASS_URLS`) with a sticky last-known-good pointer. Selection: containing polygon wins (with 6 m `GATE_TOLERANCE_M` slack), else nearest within radius.
2. **NYC planimetric** (`nyc_opendata.planimetric_footprint`) — DoITT building polygons via Socrata, inside the NYC bbox; carries the BIN so registry-first can confirm buildings OSM never had.
3. **Microsoft Building Footprints** (`ms_footprints.ms_building_footprint`) — quadkey-tiled GeoJSONL from the public dataset, cached on disk.

### NYC Registry (registry-first)
- DOHMH cooling-tower registration dataset, matched by BIN (footprint tags / planimetric) or proximity.
- A confirmed match short-circuits the entire YOLO+VLM pipeline → `registry_confirmed` with citation in notes.
- Only for fully-qualified addresses (with ZIP); NYC bbox only.

### Imagery
- **Google Static Maps** (default): centroid-centered, bottom `GOOGLE_WATERMARK_PX` (40 px) masked so YOLO doesn't detect the baked-in credit text.
- **Mapbox Static Images** (`satellite-v9`): dense urban cores (true nadir vs Google's oblique photogrammetry), the `image_unusable` retry, and the legacy path. `logo=false`, `attribution=false`.
- Both: 768x768, centroid-centered (NOT the geocoded point — important on large complexes), 3 retries with backoff.

### Dual-VLM Verification
- **Models:** Gemini (default `gemini-3.5-flash`, Google GenAI SDK) + Grok (default `grok-4.3`, xAI via OpenAI SDK)
- **Parallelism:** Both models called concurrently via `ThreadPoolExecutor`
- **Production entry point:** `verify_address` — one call per address with the marked detail tile, optional marked wide tile, and the z20 close-up. Verifies the numbered YOLO boxes AND scans for anything YOLO missed; also returns `construction` and `image_unusable` flags.
- **Consensus rule:** Both models must agree on the bucket (positive/negative) AND both must clear `VLM_CONSENSUS_THRESHOLD` (default 0.7). Otherwise → `needs_review`.
- **Reference images:** Few-shot examples in `reference_images/positive/` and `reference_images/negative/` attached to each call
- Legacy `verify_detection` (per-box) and `verify_rooftop` (whole-tile) remain in `vlm.py` for `test_vlm.py` but are no longer the production path.

### Error Handling
All API calls use small helper functions with:
- Timeout (12s default for `_http_get`, 120s for VLM calls)
- Retry attempts (3 for HTTP, 4 for Overpass and VLM)
- Exponential backoff
- Graceful degradation (returns None or routes to `needs_review`)
- Orchestrator-level retry rounds for transient Overpass/VLM failures; unresolved rows emitted explicitly unverified

### Performance Characteristics
- **Registry-confirmed NYC address:** ~5s (no YOLO/VLM)
- **Gated address (missing/ambiguous/residential footprint):** ~5s (no VLM)
- **Typical verified address:** ~15-25s (geocode + validation + footprint + 2-3 tiles + YOLO ensemble + 1 dual-VLM call)
- **Batch throughput:** ~5x the serial rate (default 5-way address concurrency overlapping the VLM wait)
- **VLM spend:** ~$0.10-$0.30 per verified address (registry/gated rows are free)

## Migration from Google Cloud

This codebase was migrated from Google Cloud Run to Railway. Key changes:

### Replaced Components
- **Cloud Tasks** → SQLite + background thread (`worker.py`, `job_queue.py`)
- **Cloud Storage** → Local filesystem (`storage_helpers.py`)
- **service.yaml** → `railway.json`
- **GCP SDK** → Removed all `google-cloud-*` dependencies

### Trade-offs
- ✅ Zero cost hosting ($5 Railway credits cover usage)
- ✅ Simpler architecture (no managed services)
- ⚠️ Ephemeral storage (files deleted on redeploy)
- ⚠️ Single worker (no horizontal scaling)
- ⚠️ VLM API spend is separate from hosting (~$0.10-$0.30 per address at current Gemini + Grok rates)

Suitable for low-traffic internal tools where users consume results immediately.

## Important Development Notes

### Docker Build Optimization
- **CPU-only PyTorch:** Installed from `https://download.pytorch.org/whl/cpu` (200MB vs 800MB CUDA)
- **Layer caching:** Requirements installed before code copy
- **Minimal system deps:** Only `libglib2.0-0`, `libgl1` (no build-essential in final image)
- **Keep `Dockerfile.railway`'s COPY list in sync** when adding modules — it copies files individually (`nyc_opendata.py`, `ms_footprints.py`, `report_audit.py`, `api_analyze.py` were each added explicitly).

### Threading & Concurrency
- **Gunicorn:** 1 worker, 8 threads (single process to share SQLite safely)
- **Worker thread:** Daemon thread, polls queue every 2 seconds
- **SQLite locking:** `_lock = threading.Lock()` in job_queue.py for thread-safe writes
- **Address-level concurrency:** `ThreadPoolExecutor` in `process_address_list`, default 5; the win is overlapping the dual-VLM wait
- **YOLO lock:** `tasks_local._yolo_lock` serializes inference — ultralytics `.predict()` is not thread-safe and the ensemble models are `@lru_cache` singletons
- **VLM concurrency:** Per-address dual-VLM calls run in parallel via `ThreadPoolExecutor(max_workers=2)`
- **Overpass rate limit:** Module-level lock in geometry.py enforces 1 req/s; Nominatim likewise 1 req/s with serialized state

### CSV Format Requirements
- **Address column:** auto-detected among `Address`, `Property Address`, `Street Address`, `Building Address` (and case/underscore variants)
- **Optional columns:** `Boro_Area`, `Zip`
- **Address construction:** `"{Address}, {Boro_Area}, {Zip}"` (no implicit state/region append; the address column should be complete on its own)
- **Excel (.xlsx/.xls) also accepted**; CSVs are tried across encodings and delimiters, and unquoted-comma addresses are rejected with a clear error instead of mis-parsing

### Testing Strategy
1. **Health check:** `curl http://localhost:8080/health` (should return `worker_running: true`); API path: `curl http://localhost:8080/api/health`
2. **Geometry + YOLO regression (no VLM, no API spend):** `python test_pipeline_no_vlm.py`
3. **Footprint → pixel projection visual check:** `python test_geometry_math.py`
4. **Dual-VLM integration test (real Gemini + Grok calls):** `python test_vlm.py` — requires `GEMINI_API_KEY` and `XAI_API_KEY`. Tests three fixtures: two detection-mode (140 West End Ave, 22 North 6th St) and one rooftop-mode (4-74 48 Avenue).
5. **Monitor logs:** Watch for "Background worker started" and "Job {id} completed successfully"
6. **Check storage:** Verify files created in `storage/results/{job_id}/`

## Common Issues

### Worker Not Starting
- Check logs for "Background worker started" message
- Verify SQLite database can be created (write permissions)
- Ensure `start_worker()` called at module level in `app_railway.py`

### Job Stuck in 'queued'
- Confirm worker thread is running (`/health` endpoint returns `worker_running: true`)
- Check for exceptions in worker logs
- Verify job was actually enqueued: `sqlite3 jobs.db "SELECT * FROM jobs;"`

### "Monthly Limit Reached" on upload
- `job_queue.MONTHLY_ADDRESS_LIMIT` (2500) tracks enqueued addresses per calendar month; resets on the 1st

### /api/* returns 503
- `ANALYZE_API_KEY` is not set on the server — the spend-incurring routes fail closed without it

### Geocoding Failures
- Verify `GOOGLE_MAPS_API_KEY` (default provider) and `MAPBOX_API_KEY` are set
- Test Mapbox key: `curl "https://api.mapbox.com/geocoding/v5/mapbox.places/empire%20state%20building.json?access_token=YOUR_KEY"`
- Check quota limits (Mapbox: 50k free requests/month)
- If many addresses fail: check logs for "Nominatim" mentions (rate-limited to 1 req/s; bulk runs may slow)
- Adjust threshold via `GEOCODE_CONFIDENCE_THRESHOLD` env var (0.60-0.80 range)

### Footprint Misses
- OSM lacks footprints for some buildings; the planimetric (NYC) / Microsoft fallback chain covers most gaps — check logs for `nyc_planimetric` / `ms_buildings` sources
- Expand search radius via `FOOTPRINT_SEARCH_RADIUS` (default 50m → try 100m for suburban markets)
- `footprint_missing` and `ambiguous_footprint` rows are surfaced with explicit verdicts; they require manual verification
- Overpass 429s are handled with retry+backoff and automatic mirror failover; persistent throttling across mirrors → consider self-hosting

### VLM Errors / Inconclusive Verdicts
- `needs_review` verdicts with reasoning containing "Network timeout" / "server error (HTTP" / "throttled" mean the VLM didn't respond — the orchestrator auto-retries these up to `VLM_RETRY_ROUNDS`; rows still marked transient after retries need re-running
- Tune `VLM_TIMEOUT_SECONDS` (default 120) and `VLM_CONSENSUS_THRESHOLD` (default 0.7) per workload
- Per-VLM detail is preserved in the CSV's `Gemini_*` and `Grok_*` columns for debugging

### Model Loading OOM
- Ensure using CPU-only PyTorch (no CUDA dependencies)
- Verify Railway instance has adequate RAM (at least 512MB recommended; the two-model ensemble holds both in memory)
- Check model files exist: `ls -lh models/*.pt`

## File Structure

```
.
├── app_railway.py              # Flask app entrypoint, routes, worker init
├── api_analyze.py              # Stateless /api/* blueprint (n8n path, X-API-Key auth)
├── worker.py                   # Background job processor (daemon thread)
├── job_queue.py                # SQLite job queue (thread-safe) + monthly limit
├── storage_helpers.py          # File storage abstraction (local FS)
├── tasks_local.py              # Per-address pipeline + concurrent orchestrator + gates
├── utils.py                    # Geocoding, Address Validation, imagery, YOLO ensemble loader
├── geometry.py                 # Footprint lookup chain + Shapely filter + dedupe
├── nyc_opendata.py             # NYC DOHMH registry + planimetric footprints (BIN)
├── ms_footprints.py            # Microsoft Building Footprints fallback
├── vlm.py                      # Dual-VLM verify_address (+ legacy verify_detection/rooftop)
├── pipeline_render.py          # Marked tiles (VLM input) + annotated images (review)
├── report_audit.py             # Audit-card HTML report (production, API path)
├── html_report.py              # Legacy HTML report (browser-UI jobs)
├── csv_export.py               # Headless CSV export from web_results
├── split_audit.py              # Dev tool: split-audit HTML from scratch JSONL
├── zip_bundler.py              # Result ZIP packaging
├── test_vlm.py                 # Dual-VLM integration test (3 fixtures, real spend)
├── test_pipeline_no_vlm.py     # Geometry + YOLO regression test (no VLM)
├── test_geometry_math.py       # Footprint → pixel projection visual check
├── models/
│   ├── rooftop_model.pt        # YOLO (43 MB, cooling-tower-trained)
│   └── rooftop_model_prev.pt   # Previous weights (42 MB) — ensemble member
├── reference_images/
│   ├── positive/               # Confirmed cooling tower examples (few-shot)
│   └── negative/               # Confirmed non-cooling-tower examples
├── n8n/
│   ├── README.md               # n8n orchestration setup + endpoint reference
│   └── parity_cooling_tower.workflow.json
├── templates/                  # HTML interface (index, results, base, error)
├── static/images/              # Logo + favicon
├── Dockerfile.railway          # Optimized Docker build (keep COPY list in sync!)
├── railway.json                # Railway deployment config
├── requirements_railway.txt    # Python dependencies (no GCP)
└── RAILWAY_DEPLOYMENT.md       # Deployment guide
```

## Attribution

- **Imagery:** © Google / © Maxar (via Mapbox)
- **Map Data:** © OpenStreetMap contributors; NYC Open Data; Microsoft Building Footprints (ODbL)
- **ML Framework:** Ultralytics YOLO (AGPL-3.0)
- **Built for:** Parity Housing
