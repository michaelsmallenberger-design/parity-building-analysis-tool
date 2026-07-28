# Parity Building Analysis Tool

Internal cooling-tower analysis tool for building address lists. The app geocodes each address, fetches roof-centered satellite imagery, runs a cooling-tower YOLO detector, filters candidates against the target building footprint, and asks Gemini to verify the final address-level result. Grok is limited to one emergency request after a technical Gemini failure.

## Current Status

- Active branch: `piece4-concurrency`
- Deployment target: Render
- Runtime: single-process Flask app with one Gunicorn worker and threaded request handling
- Main deployment entry: `app_railway.py` copied to `app.py` in `Dockerfile.railway`
- Current report format: audit-card HTML from `report_audit.py`; interactive dark review page from `review_render.py`
- Current automation paths: browser, Drive inbox, and the versioned API use the durable workbook engine; the n8n workbook workflow targets `POST /api/v2/workbook-runs`
- Current operator path: the Claude skill `.claude/skills/parity-cooling-tower` runs a batch, hands the team a review page, then writes the reviewed HVAC/Fit picks into a Google Sheet

Presentation material is in `docs/presentation/`.

## Main Flows

### Browser UI

`.xlsx`/CSV upload -> tab/dropdown preflight -> one converted Google Sheet ->
durable 100-address chunks -> grouped review and exact-tab write-back.

Primary files:

- `app_railway.py`
- `worker.py`
- `job_queue.py`
- `storage_helpers.py`
- `tasks_local.py`

### Stateless API

External automations can call the app without the queue or local result storage.

- `POST /api/run`: list of addresses -> finished self-contained audit HTML (also persists a review batch; returns its `/review/<batch_id>` URL in the `X-Review-URL` header and, when the Sheets credential is configured, the live sheet link in `X-Sheet-URL`)
- `POST /api/run-file`: uploaded Excel/CSV file -> JSON `{review_url, sheet_url, count}` (all original columns preserved; `sheet_url` is the live Google Sheet created at run time when `GOOGLE_SERVICE_ACCOUNT_JSON` is configured, else `""`)
- `POST /api/v2/workbook-runs`: asynchronous `.xlsx`/CSV/Google-Sheet intake -> durable run metadata, tab inventory, approval/status links, Sheet link, and eventual grouped review link
- `GET /api/v2/workbook-runs/<run_id>`: current workbook state and exact overall/tab denominators
- `POST /api/v2/workbook-runs/<run_id>/confirmation`: confirm all exceptional tab mappings before spend
- `POST /api/v2/workbook-runs/<run_id>/approval`: record one whole-workbook cost approval when the eligible row count exceeds the automatic threshold
- `POST /api/v2/workbook-runs/<run_id>/retry`: resume a failed/cancelled analysis from durable row checkpoints without a second reservation
- `POST /api/analyze`: one address -> one self-contained result entry
- `POST /api/report`: result entries -> audit HTML
- `GET /review/<batch_id>`: interactive dark review page for the team (imagery carousel + AI guidance, HVAC multi-select + Fit single-select, Submit per building; each Submit is recorded server-side AND written live into the batch's Google Sheet row when one exists)
- `GET /api/batch/<batch_id>`: the original uploaded table plus the human review decisions recorded so far (no AI columns) — the fallback for building the final Google Sheet by hand
- `GET /api/batch/<batch_id>/failures`: the rows that failed analysis (imagery/geocode/analyzer errors) for the cleanup skill
- `POST /api/batch/<batch_id>/rerun`: re-run failed rows in place (optionally with corrected addresses) and merge fresh results into the same review page/sheet
- `GET /api/health`: API health check

Spend-incurring API routes require `X-API-Key: <ANALYZE_API_KEY>`.

Use `/api/v2/workbook-runs` for multi-tab `.xlsx` files. Legacy `.xls` is rejected
with a Save As `.xlsx` instruction because its dropdown behavior cannot be
guaranteed. `/api/run-file` remains compatible for CSV and one eligible Excel
address tab, but fails closed instead of silently choosing one tab from a
multi-tab workbook.

Primary files:

- `api_analyze.py`
- `report_audit.py`
- `n8n/README.md`
- `n8n/parity_cooling_tower.workflow.json`
- `n8n/parity_workbook_async.workflow.json`

### Team Review + Google Sheet

The primary flow needs no operator in the middle (requires `GOOGLE_SERVICE_ACCOUNT_JSON`
on the server — see `RENDER_DEPLOYMENT.md` for the one-time setup):

1. A run (`POST /api/run` or `POST /api/run-file`, or the `parity-cooling-tower` skill)
   creates the output Google Sheet up front and returns BOTH links: the `/review/<batch_id>`
   page and the `sheet_url`.
2. The team opens the dark review page, clicks through each building (imagery + AI guidance),
   checks the **HVAC systems** they see, picks a **Fit**, and hits Submit per building. Each
   Submit is recorded server-side by `review_store.py` AND written live into that row of the
   Google Sheet — when review is done, the sheet is already done. **Human picks only, no AI
   verdict/confidence columns.**
3. Failed buildings (no imagery, geocode misses) are fixed with the `parity-cleanup-failed`
   skill, which diagnoses and re-runs them in place via `/api/batch/<id>/failures` + `/rerun`.

Fallback (credential not configured): `sheet_url` comes back empty, picks are only recorded
server-side, and the `parity-cooling-tower` skill builds the sheet from `GET /api/batch`
after review — the pre-existing operator flow.

Primary files:

- `.claude/skills/parity-cooling-tower/SKILL.md` (run + review runbook)
- `.claude/skills/parity-cleanup-failed/SKILL.md` (failed-row cleanup runbook)
- `review_render.py` (review page + `HVAC_SYSTEMS` / `FIT_OPTIONS` taxonomy)
- `review_store.py` (batch + decision storage)
- `sheets_writer.py` (service-account sheet creation + live row writes)
- `api_analyze.py` (`/api/run*`, `/review/*`, `/api/batch/*`)

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
9. Call `vlm.verify_address()` once for the address. Gemini is the normal reviewer; Grok is allowed one emergency request only after a technical Gemini failure.
10. Emit CSV-compatible data plus audit-card web entries.

The older per-box `verify_detection()` and whole-roof `verify_rooftop()` functions still exist in `vlm.py` for compatibility/testing, but the active per-address pipeline uses `verify_address()`.

## Required Environment Variables

- `GOOGLE_MAPS_API_KEY`: Google geocoding, Static Maps imagery, and Address Validation
- `MAPBOX_API_KEY`: Mapbox fallback geocoding/imagery and dense-core imagery
- `GEMINI_API_KEY`: Gemini verification
- `XAI_API_KEY` (optional): enables exceptional Grok Sheet mapping and the one-request Gemini emergency fallback; normal analysis and health do not require it
- `ANALYZE_API_KEY`: required for `/api/analyze`, `/api/run`, `/api/run-file`, `/api/report`, and `/api/batch/*`
- `GOOGLE_SERVICE_ACCOUNT_JSON` (optional): service-account key enabling `sheets_writer.py` — sheet created at run time, review Submits written live; without it `sheet_url` stays empty
- `SHEET_SHARE_WITH` (optional): comma-separated emails granted writer access to created sheets
- `SHEET_PARENT_FOLDER_ID`: Shared Drive/folder receiving the converted workbook copy

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
- `MULTI_TAB_WORKBOOK_ENABLED`: master workbook-engine feature flag
- `MULTI_TAB_BROWSER_ENABLED`, `MULTI_TAB_DRIVE_ENABLED`, `MULTI_TAB_API_ENABLED`: staged surface flags
- `WORKBOOK_AUTO_APPROVAL_ROWS`: default `250`; larger workbooks wait for one recorded approval and are never truncated
- `WORKBOOK_CHUNK_ROWS`: durable chunk size, default `100`
- `ANALYZE_ESTIMATED_MIN_COST_PER_ADDRESS` / `ANALYZE_ESTIMATED_MAX_COST_PER_ADDRESS`: measured/configured rates required for large-workbook approval
- `STORAGE_DIR` / `JOBS_DB_PATH`: point manifests, checkpoints, reviews, and SQLite at the Render persistent disk
- `VLM_TIMEOUT_SECONDS`: default `120`
- `GEMINI_MODEL`: default `gemini-3.6-flash`
- `GROK_MODEL`: default `grok-4.3`
- `GEMINI_THINKING_LEVEL`: default `high`
- `GROK_REASONING_EFFORT`: default `high`
- `SHEET_INTAKE_GROK_ENABLED`: default `true` when `XAI_API_KEY` is configured
- `SHEET_INTAKE_GROK_SAMPLE_ROWS`: capped at `25`, default `25`
- `SHEET_INTAKE_GROK_AUTO_CONFIDENCE`: Drive/API auto-run threshold, default `0.90`
- `GEMINI_GROK_EMERGENCY_FALLBACK_ENABLED`: default `true`; limits a technical Gemini fallback to one Grok request

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

Render deployment is configured by `render.yaml`. Create a new Render Blueprint from this repo/branch, then fill the required secret values:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `ANALYZE_API_KEY`

`XAI_API_KEY` is optional and is only needed for exceptional Grok Sheet mapping or emergency fallback.

See `RENDER_DEPLOYMENT.md` for the exact dashboard steps and smoke test.

## Tests and Harnesses

These are not pure unit tests; most call external services, use model weights, or write generated images.

- `python -m compileall app_railway.py api_analyze.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py`
- `python test_pipeline_no_vlm.py`: geocoding, imagery, footprints, YOLO; no VLM spend
- `python test_vlm.py`: fixture harness (uses Gemini; Grok only after a technical Gemini failure)
- `python test_intake_vlm_fallback.py`: mocked no-spend coverage for Sheet mapping and Gemini-first fallback
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
- `review_render.py`
- `review_store.py`
- `zip_bundler.py`
- `templates/`
- `static/images/`
- `reference_images/`
- `models/`

Generated outputs, caches, scratch files, local uploads, local queues, and one-off demo artifacts are intentionally ignored.
