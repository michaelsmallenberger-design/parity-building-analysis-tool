# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A building analysis tool that uses YOLO computer vision plus dual-VLM verification to detect cooling towers on rooftops from satellite imagery. Users upload a CSV of addresses; the system geocodes them (Google Maps, Mapbox fallback), fetches centroid-centered satellite tiles (Google Static Maps; Mapbox in dense urban cores), runs a YOLO ensemble, filters detections against OSM building footprints (with NYC-planimetric / Microsoft fallbacks), and verifies each address with one dual-VLM pass (Gemini + Grok) in parallel.

**Deployment:** Railway.app (migrated from Google Cloud Run)
**Usage:** Low-traffic internal tool (~1000 requests/month)
**Phase:** Phase 3 shipped 2026-05-21 (commit 75af037). Phase 4 (branch `piece4-concurrency`, pending merge to main): Google geocoding + imagery (dense cores use Mapbox via the `DENSE_CORE_BBOXES`/`_in_nyc` gate), one-call dual-VLM `verify_address` (replaces per-box `verify_detection`/`verify_rooftop`), OSM→NYC-planimetric→Microsoft footprint fallback chain, and a dense-urban roof-only gate. **New required env var: `GOOGLE_MAPS_API_KEY`.**

## Development Commands

### Local Development
```bash
# Install dependencies
pip install -r requirements_railway.txt

# Set required environment variables
export MAPBOX_API_KEY="your-key"
export GEMINI_API_KEY="your-key"
export XAI_API_KEY="your-key"

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

### System Design

This is a **single-process Flask application** with a background worker thread that processes jobs from a SQLite queue. It uses **local filesystem storage** (ephemeral) instead of object storage. Per-address work fans out across multiple modules: geometry for footprint filtering, vlm for dual-model verification, pipeline_render for annotated images.

```
Flask App (app_railway.py)
   ├── Routes: /, /upload, /results, /status, /files/*
   ├── Job Queue (SQLite) — job_queue.py
   ├── Storage (local FS) — storage_helpers.py
   └── Background Worker Thread — worker.py
         └── Task Processor — tasks_local.py
               ├── utils.py            — geocoding + Mapbox imagery + YOLO loader
               ├── geometry.py         — OSM Overpass footprint + Shapely filter
               ├── vlm.py              — dual-VLM verify_detection + verify_rooftop
               └── pipeline_render.py  — annotated image rendering
```

### Key Architectural Patterns

#### 1. Background Job Processing (No Cloud Tasks)

**Pattern:** SQLite-based job queue + polling worker thread (replaces Google Cloud Tasks)

- User uploads CSV → `enqueue_job()` creates row in SQLite with `status='queued'`
- Background worker polls database every 2 seconds for pending jobs
- Worker processes job → updates progress in real-time → marks as `finished`
- Frontend polls `/status/{job_id}` endpoint for progress updates

**Files:**
- `job_queue.py` — SQLite CRUD (thread-safe with locks)
- `worker.py` — `BackgroundWorker` class, starts on Flask init
- `app_railway.py` — Starts worker via `start_worker()` at module level

**Important:** Single worker only (no horizontal scaling). Worker runs in daemon thread and dies with Flask process.

#### 2. Ephemeral Local Storage (No Cloud Storage)

**Pattern:** Local filesystem with Flask serving route (replaces Google Cloud Storage)

- Files stored in `storage/` directory (created on init)
- Structure: `storage/uploads/{job_id}/`, `storage/results/{job_id}/`
- Served via `/files/<path>` route with proper MIME types
- Files persist until next Railway redeploy (ephemeral)

**Files:**
- `storage_helpers.py` — File operations (upload, read, make_url)
- `app_railway.py:serve_file()` — Streams files with caching headers

**Trade-off:** Acceptable for this use case because users download results immediately. Not suitable for long-term storage.

#### 3. Per-Address Pipeline (Phase 3)

**Flow:** For each address in the CSV:
1. **Geocode** (`utils.geocode_address_mapbox`) — Mapbox primary; Nominatim fallback for low-confidence or unrecognized results
2. **OSM Footprint Lookup** (`geometry.get_building_footprint`) — Overpass API → Shapely Polygon/MultiPolygon for the building
3. **Centroid-Centered Tile** (`utils.get_satellite_image_mapbox`) — Mapbox Static Images, 768x768 @ zoom 19, centered on the footprint centroid (NOT the geocoded address point)
4. **YOLO Inference** (`utils._get_model` + `tasks_local.py`) — lazy-loaded YOLO26m at `YOLO_CONF` (default 0.18)
5. **Footprint Filter** (`geometry.footprint_filter_pipeline`) — Shapely point-in-polygon classifies each detection as inside / boundary / outside
6. **Branch:**
   - **kept detections non-empty:** call `vlm.verify_detection(tile, bbox, ctx)` per kept candidate; pick winner via `_pick_winner` (class-rank then confidence)
   - **kept detections empty:** call `vlm.verify_rooftop(tile, ctx)` once on the whole tile (last-line-of-defense scan)
7. **Render Annotated Image** (`pipeline_render.render_annotated_image`) — red footprint polygon + color-coded bboxes (winner green-thick, positive green-thin, negative gray, ambiguous orange, boundary yellow)
8. **Upload + CSV Row** (`storage_helpers.upload_file` + `tasks_local._build_csv_row`) — 15-column CSV row with verdict / reasoning / construction / per-VLM detail

**Files:**
- `tasks_local.py:process_address_list()` — main orchestrator
- `utils.py` — geocoding, Mapbox imagery, YOLO model loader
- `geometry.py` — Overpass footprint lookup, Shapely point-in-polygon classification
- `vlm.py` — dual-VLM verification (Gemini + Grok consensus)
- `pipeline_render.py` — annotated image rendering

#### 4. Lazy-Loaded ML Model

**Pattern:** `@lru_cache` prevents loading YOLO at import time (avoids OOM on startup)

```python
# utils.py
@lru_cache(maxsize=1)
def _get_model():
    from ultralytics import YOLO
    return YOLO(MODEL_PATH)  # Loads only on first prediction call

# Model cached in memory for subsequent calls
```

**Model:** `models/rooftop_model.pt` (44 MB, YOLO26m custom-trained on cooling towers)

## Critical Configuration

### Environment Variables (Required)
- `GOOGLE_MAPS_API_KEY` — Google geocoding + Static Maps imagery + Address Validation. Required by default since `GEOCODER_PROVIDER` and `IMAGERY_PROVIDER` default to `google`. (Set both to `mapbox` to run the legacy path without this key.)
- `MAPBOX_API_KEY` — Mapbox geocoding (fallback) + Static Images (used for dense-urban-core imagery and the image_unusable retry)
- `GEMINI_API_KEY` — Google AI Studio API key for Gemini verification (model via `GEMINI_MODEL`, default `gemini-3.5-flash`)
- `XAI_API_KEY` — xAI API key for Grok verification (model via `GROK_MODEL`, default `grok-4.3`)

### Environment Variables (Optional)

**Server:**
- `PORT` — Server port (default: 8080)
- `UPLOAD_FOLDER` — Upload directory (default: `temp_uploads`)
- `LOG_LEVEL` — Python logging level (default: `INFO`)

**YOLO inference:**
- `YOLO_CONF` — YOLO confidence threshold (default: 0.18). Lower = more recall, more candidates routed to dual-VLM verification.
- `MODEL_PATH` — YOLO model file path (default: `models/rooftop_model.pt`)

**VLM tuning:**
- `GEMINI_MODEL` — Gemini model ID (default: `gemini-3.1-pro-preview`)
- `GROK_MODEL` — Grok model ID (default: `grok-4.3`)
- `GROK_REASONING_EFFORT` — Grok reasoning depth: `none`/`low`/`medium`/`high` (default: `high`). xAI's own default is `low`; raised here for accuracy. Note: the `grok-*-fast` model variants reject this param (HTTP 400).
- `GEMINI_THINKING_LEVEL` — Gemini thinking depth: `minimal`/`low`/`medium`/`high` (default: `high`). Google's own default is `medium`. Replaces the deprecated `thinking_budget`; Gemini 3.x only.
- `VLM_TIMEOUT_SECONDS` — Per-VLM wall-clock timeout in seconds (default: 120)
- `VLM_CONSENSUS_THRESHOLD` — Minimum confidence both models must clear for consensus (default: 0.7). Below this → `needs_review`.

**Imagery:**
- `MAPBOX_ZOOM` — Detail-tile zoom level (default: 19, range: 18-20). 20+ may produce blurry tiles.
- `MAPBOX_ZOOM_WIDE` — Wide-tile zoom level (default: 17). Each address fetches **two** centroid-centered tiles: the detail tile (`MAPBOX_ZOOM`) and a wider tile (`MAPBOX_ZOOM_WIDE`, ~920 m across) so YOLO can catch ground-mounted cooling equipment that sits in adjacent yards/pads outside the z19 frame. Both YOLO models run on both tiles; detections are merged across zooms by `geometry.geo_dedupe_detections` (geo-space, ~10 m threshold — pixel IoU is invalid across zoom levels). Each VLM verification also receives the opposite-zoom tile as cross-zoom context.
- `MAPBOX_SIZE` — Tile dimensions (default: `768x768`)
- `MAPBOX_DPI` — @2x retina tiles (default: false). Note: env var name is `MAPBOX_DPI`; the Python variable in utils.py is `MAPBOX_HIGH_DPI`.
- `MAPBOX_CROP_BOTTOM_PX` — Trim N pixels from bottom of fetched tile (default: 0). Obsolete since `logo=false` / `attribution=false` params disable Mapbox overlays.

**Geocoding and Footprint:**
- `GEOCODE_CONFIDENCE_THRESHOLD` — Mapbox relevance threshold before falling back to Nominatim (default: 0.70)
- `OVERPASS_URL` — OSM Overpass API endpoint (default: `https://overpass-api.de/api/interpreter`)
- `OVERPASS_TIMEOUT` — Overpass query timeout in seconds (default: 15)
- `FOOTPRINT_SEARCH_RADIUS` — OSM Overpass search radius in meters (default: 50). Increase for non-urban / suburban markets.

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
| `app_railway.py` | Flask routes, worker init | job_queue, storage_helpers, worker |
| `worker.py` | Background job processor (daemon thread) | job_queue, storage_helpers, tasks_local |
| `job_queue.py` | SQLite job queue (thread-safe) | None (stdlib only) |
| `storage_helpers.py` | File storage abstraction (local FS) | None (stdlib only) |
| `tasks_local.py` | Per-address pipeline orchestrator + 5 in-module helpers (`_pick_winner`, `_build_notes`, `_centroid_latlon`, `_build_web_entry`, `_build_csv_row`) | utils, geometry, vlm, pipeline_render, html_report, shapely, pandas |
| `utils.py` | Geocoding (Mapbox + Nominatim), Mapbox imagery, YOLO model loader | requests, geopy, PIL, ultralytics |
| `geometry.py` | OSM Overpass building footprint lookup, Shapely point-in-polygon classification, pixel ↔ lat/lon conversion (Web Mercator) | requests, shapely |
| `vlm.py` | Dual-VLM verification — `verify_detection` (per-candidate) and `verify_rooftop` (whole-tile fallback). Gemini 3.1 Pro + Grok 4.3 consensus. | google-genai, openai, httpx, PIL, pydantic |
| `pipeline_render.py` | Annotated image rendering — red footprint polygon + color-coded detection bboxes | cv2, numpy, shapely |
| `html_report.py` | Self-contained HTML report with base64-embedded images | None (stdlib only) |
| `zip_bundler.py` | Result ZIP packaging (HTML + CSV + images) | None (stdlib only) |
| `test_vlm.py` | Dual-VLM integration test against 3 fixtures (2 detection, 1 rooftop) | utils, vlm, ultralytics |
| `test_pipeline_no_vlm.py` | Geometry + YOLO regression test (no VLM, no API spend) | utils, geometry, cv2, ultralytics |

### Data Flow

```
1. Upload CSV → app_railway.py:/upload
    ├── storage_helpers.upload_file() → storage/uploads/{job_id}/file.csv
    └── job_queue.enqueue_job() → SQLite row (status='queued')

2. Worker picks up job → worker.py:_worker_loop()
    ├── job_queue.get_pending_jobs()
    ├── job_queue.update_job_status(status='processing')
    └── tasks_local.process_address_list()
        FOR EACH address:
           ├── utils.geocode_address_mapbox() → (lat, lon)
           ├── geometry.get_building_footprint() → footprint or None
           │     ├── If None: footprint_missing CSV row, skip VLM
           │     └── If found: continue
           ├── utils.get_satellite_image_mapbox() → centroid-centered tile
           ├── utils._get_model().predict() → YOLO detections
           ├── geometry.footprint_filter_pipeline() → kept / rejected / boundary
           ├── Branch:
           │     ├── kept ≠ ∅: vlm.verify_detection() per candidate, _pick_winner()
           │     └── kept = ∅: vlm.verify_rooftop() once
           ├── pipeline_render.render_annotated_image() → annotated JPG
           ├── storage_helpers.upload_file() → storage/results/{job_id}/
           └── progress_cb() → job_queue.update_job_status(progress=N)

3. Frontend polls → app_railway.py:/status/{job_id}
    └── job_queue.get_job_status() → {status, progress, total, result}

4. Serve files → app_railway.py:/files/<path>
    └── storage_helpers.read_file() → stream with MIME type
```

## External API Usage

### Geocoding Strategy

**Tier 1: Mapbox Geocoding (Primary)**
1. Generate query variations:
   - Original: `"52 East 72nd St (Claremont House), New York, NY"`
   - Cleaned: `"52 East 72nd St, New York, NY"` (parentheses + corporate suffixes removed)
   - Street-only: `"52 East 72nd St"` (first variation if address starts with a number)
2. Try each variation with Mapbox; track the highest-relevance result
3. If any variation hits `GEOCODE_CONFIDENCE_THRESHOLD` (default 0.70) → return immediately

**Tier 2: Nominatim/OpenStreetMap (Fallback)**
- Triggered when: Mapbox returns nothing OR best Mapbox relevance < `GEOCODE_CONFIDENCE_THRESHOLD`
- Rate limited: 1 request per second (enforced via lock)
- Good with building names and POIs
- No API key required
- No geographic constraint — works anywhere in the US (Mapbox `country: "US"`) or wherever the address resolves

**Tier 3: Low-Confidence Mapbox (Last Resort)**
- If Nominatim also fails, use the best Mapbox result even if below threshold
- Logs warning for manual review

### OSM Overpass Footprint Lookup
- Query: `way["building"](around:radius,lat,lon)` + relation variant
- Default search radius: 50 meters (override via `FOOTPRINT_SEARCH_RADIUS`)
- Rate limited: 1 request per second per process (thread-safe lock)
- Retry policy: 4 attempts on transient codes (429, 5xx) with exponential backoff
- Selection: if a building polygon **contains** the geocoded point, that's the target; otherwise the **nearest** polygon within search radius

### Mapbox Static Images
- **Style:** `mapbox/satellite-v9`
- **Resolution:** 768x768 @ zoom 19
- **Centering:** OSM building centroid (NOT the geocoded address point — important for accuracy on large building complexes)
- **Config:** `logo=false`, `attribution=false` (attribution shown in UI footer)
- **Retry:** 3 attempts with exponential backoff

### Dual-VLM Verification
- **Models:** Gemini 3.1 Pro (Google GenAI SDK) + Grok 4.3 (xAI via OpenAI SDK)
- **Parallelism:** Both models called concurrently via `ThreadPoolExecutor`
- **Consensus rule:** Both models must agree on the bucket (positive/negative) AND both must clear `VLM_CONSENSUS_THRESHOLD` (default 0.7). Otherwise → `needs_review`.
- **Schemas:** `verify_detection` uses 5-verdict schema (confirmed / likely / neighbor_only / not_detected / needs_review). `verify_rooftop` uses 4-verdict schema (cooling_tower_present / cooling_tower_possible / no_cooling_tower / needs_review).
- **Reference images:** Few-shot examples in `reference_images/positive/` and `reference_images/negative/` attached to each call

### Error Handling
All API calls use small helper functions with:
- Timeout (12s default for `_http_get`, 120s for VLM calls)
- Retry attempts (3 for HTTP, 4 for Overpass and VLM)
- Exponential backoff
- Graceful degradation (returns None or routes to `needs_review`)

### Performance Characteristics
- **Typical happy-path address (1 detection):** ~15s (geocode + Overpass + Mapbox tile + YOLO + 1 dual-VLM call)
- **Multi-detection address:** ~15s + ~10s per additional kept detection (each triggers its own dual-VLM)
- **Zero-detection (rooftop scan):** ~15s (geocode + Overpass + Mapbox tile + YOLO + 1 dual-VLM rooftop call)
- **Footprint-missing:** ~5s (geocode + 1 Mapbox tile + no VLM)

## Migration from Google Cloud

This codebase was migrated from Google Cloud Run to Railway. Key changes:

### Replaced Components
- **Cloud Tasks** → SQLite + background thread (`worker.py`, `job_queue.py`)
- **Cloud Storage** → Local filesystem (`storage_helpers.py`)
- **service.yaml** → `railway.json`
- **GCP SDK** → Removed all `google-cloud-*` dependencies

### Removed Files
- `app.py` → `app_railway.py`
- `gcp_helpers.py` → `storage_helpers.py`
- `tasks_serverless.py` → `tasks_local.py`
- Old Dockerfile, requirements.txt, service.yaml

### Trade-offs
- ✅ Zero cost hosting ($5 Railway credits cover usage)
- ✅ Simpler architecture (no managed services)
- ✅ No credit card required
- ⚠️ Ephemeral storage (files deleted on redeploy)
- ⚠️ Single worker (no horizontal scaling)
- ⚠️ VLM API spend is separate from hosting (~$0.10-$0.30 per address at current Gemini + Grok rates)

Suitable for low-traffic internal tools where users consume results immediately.

## Important Development Notes

### Docker Build Optimization
- **CPU-only PyTorch:** Installed from `https://download.pytorch.org/whl/cpu` (200MB vs 800MB CUDA)
- **Layer caching:** Requirements installed before code copy
- **Minimal system deps:** Only `libglib2.0-0`, `libgl1` (no build-essential in final image)

### Threading & Concurrency
- **Gunicorn:** 1 worker, 8 threads (single process to share SQLite safely)
- **Worker thread:** Daemon thread, polls queue every 2 seconds
- **SQLite locking:** `_lock = threading.Lock()` in job_queue.py for thread-safe writes
- **Model caching:** YOLO model loaded once per process, shared across threads
- **VLM concurrency:** Per-address dual-VLM calls run in parallel via `ThreadPoolExecutor(max_workers=2)`
- **Overpass rate limit:** Module-level lock in geometry.py enforces 1 req/s

### CSV Format Requirements
- **Required column:** `Address`
- **Optional columns:** `Boro_Area`, `Zip`
- **Address construction:** Combines columns as: `"{Address}, {Boro_Area}, {Zip}"` (no implicit state/region append; the address column should be complete on its own)

### Testing Strategy
1. **Health check:** `curl http://localhost:8080/health` (should return `worker_running: true`)
2. **Geometry + YOLO regression (no VLM, no API spend):** `python test_pipeline_no_vlm.py`
3. **Dual-VLM integration test (real Gemini + Grok calls):** `python test_vlm.py` — requires `GEMINI_API_KEY` and `XAI_API_KEY`. Tests three fixtures: two detection-mode (140 West End Ave, 22 North 6th St) and one rooftop-mode (4-74 48 Avenue).
4. **Monitor logs:** Watch for "Background worker started" and "Job {id} completed successfully"
5. **Check storage:** Verify files created in `storage/results/{job_id}/`

## Common Issues

### Worker Not Starting
- Check logs for "Background worker started" message
- Verify SQLite database can be created (write permissions)
- Ensure `start_worker()` called at module level in `app_railway.py`

### Job Stuck in 'queued'
- Confirm worker thread is running (`/health` endpoint returns `worker_running: true`)
- Check for exceptions in worker logs
- Verify job was actually enqueued: `sqlite3 jobs.db "SELECT * FROM jobs;"`

### Geocoding Failures
- Verify `MAPBOX_API_KEY` is set
- Test Mapbox API key: `curl "https://api.mapbox.com/geocoding/v5/mapbox.places/empire%20state%20building.json?access_token=YOUR_KEY"`
- Check Mapbox quota limits (50k free requests/month on the free tier)
- If many addresses fail: check logs for "Nominatim" mentions (rate-limited to 1 req/s; bulk runs may slow)
- Adjust threshold via `GEOCODE_CONFIDENCE_THRESHOLD` env var (0.60-0.80 range)

### Footprint Misses
- The OSM Overpass database lacks footprints for some buildings (especially in rural areas or recent construction)
- Expand search radius via `FOOTPRINT_SEARCH_RADIUS` env var (default 50m → try 100m for suburban markets)
- `footprint_missing` rows are surfaced in the CSV with explicit verdict; they require manual verification
- Overpass rate-limiting (HTTP 429) is handled with retry+backoff; persistent 429s suggest the public Overpass endpoint is under load — consider self-hosting

### VLM Errors / Inconclusive Verdicts
- `needs_review` verdicts with reasoning containing "Network timeout" / "API server error" / "API throttled" mean the VLM didn't respond — the address may need re-running
- Tune `VLM_TIMEOUT_SECONDS` (default 120) and `VLM_CONSENSUS_THRESHOLD` (default 0.7) per workload
- Per-VLM detail is preserved in the CSV's `Gemini_*` and `Grok_*` columns for debugging

### Model Loading OOM
- Ensure using CPU-only PyTorch (no CUDA dependencies)
- Verify Railway instance has adequate RAM (at least 512MB recommended)
- Check model file exists and is readable: `ls -lh models/rooftop_model.pt`

## File Structure

```
.
├── app_railway.py              # Flask app entrypoint, routes, worker init
├── worker.py                   # Background job processor (daemon thread)
├── job_queue.py                # SQLite job queue (thread-safe)
├── storage_helpers.py          # File storage abstraction (local FS)
├── tasks_local.py              # Per-address pipeline orchestrator + helpers
├── utils.py                    # Geocoding, Mapbox imagery, YOLO loader
├── geometry.py                 # OSM Overpass footprint lookup + Shapely filter
├── vlm.py                      # Dual-VLM verify_detection + verify_rooftop
├── pipeline_render.py          # Annotated-image rendering (footprint + bboxes)
├── html_report.py              # Self-contained HTML report (base64 images)
├── zip_bundler.py              # Result ZIP packaging
├── test_vlm.py                 # Dual-VLM integration test (3 fixtures)
├── test_pipeline_no_vlm.py     # Geometry + YOLO regression test (no VLM)
├── models/
│   └── rooftop_model.pt        # YOLO26m (44 MB, cooling-tower-trained)
├── reference_images/
│   ├── positive/               # Confirmed cooling tower examples (few-shot)
│   └── negative/               # Confirmed non-cooling-tower examples
├── templates/                  # HTML interface
│   ├── index.html              # Upload form
│   ├── results.html            # Results page with polling
│   └── base.html               # Layout template
├── static/
│   └── images/
│       ├── Parity-Logo.png
│       └── favicon.ico
├── Dockerfile.railway          # Optimized Docker build
├── railway.json                # Railway deployment config
├── requirements_railway.txt    # Python dependencies (no GCP)
└── RAILWAY_DEPLOYMENT.md       # Deployment guide
```

## Attribution

- **Imagery:** © Maxar (via Mapbox)
- **Map Data:** © OpenStreetMap contributors
- **ML Framework:** Ultralytics YOLO (AGPL-3.0)
- **Built for:** Parity Housing
