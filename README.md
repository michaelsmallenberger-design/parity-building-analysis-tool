# Parity Building Analysis Tool

Internal cooling-tower analysis tool for building address lists. The app geocodes each address, fetches roof-centered satellite imagery, runs a cooling-tower YOLO detector, filters candidates against the target building footprint, and asks Gemini + Grok to verify the final address-level result.

## Current Status

- Active branch: `piece4-concurrency`
- Deployment target: Render
- Runtime: single-process Flask app with one Gunicorn worker and threaded request handling
- Main deployment entry: `app_railway.py` copied to `app.py` in `Dockerfile.railway`
- Current report format: audit-card HTML from `report_audit.py`
- Current automation paths: n8n calls `POST /api/run-file` for Excel upload or `POST /api/run` for address lists

Older handoff and accuracy notes are preserved in `docs/archive/`. Presentation material is in `docs/presentation/`.

## Main Flows

### Browser UI

CSV upload -> SQLite job queue -> background worker -> results page and downloadable files.

Primary files:

- `app_railway.py`
- `worker.py`
- `job_queue.py`
- `storage_helpers.py`
- `tasks_local.py`

### Stateless API

External automations can call the app without the queue or local result storage.

- `POST /api/run`: list of addresses -> finished self-contained audit HTML
- `POST /api/run-file`: uploaded Excel/CSV file -> finished self-contained audit HTML
- `POST /api/analyze`: one address -> one self-contained result entry
- `POST /api/report`: result entries -> audit HTML
- `GET /api/health`: API health check

Spend-incurring API routes require `X-API-Key: <ANALYZE_API_KEY>`.

For the Excel upload flow, send a multipart form upload to `/api/run-file` with a `file` field containing `.xlsx`, `.xls`, or `.csv`. The file must have an address column such as `Address`, `Property Address`, `Street Address`, or `Building Address`.

Primary files:

- `api_analyze.py`
- `report_audit.py`
- `n8n/README.md`
- `n8n/parity_cooling_tower.workflow.json`

## Current Pipeline

For each address:

1. Compose and clean the input address.
2. Geocode with the selected provider. Default is Google; Mapbox remains available and Nominatim is used as a corroborating/fallback signal.
3. Validate address precision with Google Address Validation when available.
4. Find the target building footprint through OSM Overpass, then NYC planimetric data, then Microsoft Building Footprints.
5. Fetch two roof-centered satellite tiles: detail zoom and wide/context zoom.
6. Run YOLO detections. `MODEL_PATHS` can enable an ensemble; if unset, only `MODEL_PATH` is used.
7. Convert detections into geo-space and filter them against the footprint.
8. Render marked detail/wide imagery and a close-up.
9. Call `vlm.verify_address()` once for the address. Gemini and Grok run in parallel and are combined by consensus.
10. Emit CSV-compatible data plus audit-card web entries.

The older per-box `verify_detection()` and whole-roof `verify_rooftop()` functions still exist in `vlm.py` for compatibility/testing, but the active per-address pipeline uses `verify_address()`.

## Required Environment Variables

- `GOOGLE_MAPS_API_KEY`: Google geocoding, Static Maps imagery, and Address Validation
- `MAPBOX_API_KEY`: Mapbox fallback geocoding/imagery and dense-core imagery
- `GEMINI_API_KEY`: Gemini verification
- `XAI_API_KEY`: Grok verification
- `ANALYZE_API_KEY`: required for `/api/analyze`, `/api/run`, `/api/run-file`, and `/api/report`

## Important Optional Environment Variables

- `PORT`: default `8080`
- `GEOCODER_PROVIDER`: `google` by default; set `mapbox` for the legacy path
- `IMAGERY_PROVIDER`: `google` by default; dense-core addresses can override to Mapbox
- `MODEL_PATH`: default `models/rooftop_model.pt`
- `MODEL_PATHS`: comma-separated model paths for ensemble inference
- `YOLO_CONF`: default `0.18`
- `MAPBOX_ZOOM`: detail tile zoom, default `19`
- `MAPBOX_ZOOM_WIDE`: wide/context tile zoom, default `18`
- `MAPBOX_SIZE`: default `768x768`
- `VLM_ADDRESS_CONCURRENCY`: address-level concurrency, default `5`
- `VLM_TIMEOUT_SECONDS`: default `120`
- `VLM_CONSENSUS_THRESHOLD`: default `0.7`
- `GEMINI_MODEL`: default `gemini-3.5-flash`
- `GROK_MODEL`: default `grok-4.3`
- `GEMINI_THINKING_LEVEL`: default `high`
- `GROK_REASONING_EFFORT`: default `high`

## Local Development

```bash
pip install -r requirements_railway.txt
python app_railway.py
```

Health checks:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/health
```

## Docker / Render

```bash
docker build -f Dockerfile.railway -t parity-building-analysis-tool .
docker run -p 8080:8080 --env-file .env parity-building-analysis-tool
```

Render builds from `Dockerfile.railway`. Keep both production model weights committed:

- `models/rooftop_model.pt`
- `models/rooftop_model_prev.pt`

`MODEL_PATHS` must be set if both should be used at runtime.

## Render

Render deployment is configured by `render.yaml`. Create a new Render Blueprint from this repo/branch, then fill the five secret values Render asks for:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`
- `ANALYZE_API_KEY`

See `RENDER_DEPLOYMENT.md` for the exact dashboard steps and smoke test.

For the no-touch Excel workflow, import `n8n/parity_excel_upload.workflow.json` into n8n and test with `n8n/address_upload_template.xlsx`. The short operating guide is `RUNBOOK.md`.

## Tests and Harnesses

These are not pure unit tests; most call external services, use model weights, or write generated images.

- `python -m compileall app_railway.py api_analyze.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py`
- `python test_pipeline_no_vlm.py`: geocoding, imagery, footprints, YOLO; no VLM spend
- `python test_vlm.py`: legacy VLM harness using real Gemini/Grok calls
- `python test_geometry_math.py`: geometry/image diagnostic, not a normal unit test

## Active Runtime Files

- `app_railway.py`
- `api_analyze.py`
- `worker.py`
- `job_queue.py`
- `storage_helpers.py`
- `tasks_local.py`
- `utils.py`
- `geometry.py`
- `nyc_opendata.py`
- `ms_footprints.py`
- `vlm.py`
- `pipeline_render.py`
- `report_audit.py`
- `html_report.py`
- `zip_bundler.py`
- `templates/`
- `static/images/`
- `reference_images/`
- `models/`

Generated outputs, caches, scratch files, local uploads, local queues, and one-off demo artifacts are intentionally ignored.
