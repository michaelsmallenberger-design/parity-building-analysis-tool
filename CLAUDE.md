# CLAUDE.md

Repository guidance for Claude Code and other coding agents.

## Project Reality

This repository is the Parity cooling-tower analyzer. It is currently a
Render-hosted Flask app with three workbook-capable front doors:

1. Browser `.xlsx`/CSV upload or live Google Sheet link.
2. Shared Drive inbox.
3. Versioned asynchronous API intake through `/api/v2/workbook-runs`.

All three use one resumable, fail-closed workbook engine. `/api/run` and
`/api/run-file` remain legacy single-table compatibility routes.

The active pipeline is the Phase 4 shape on branch `piece4-concurrency`: Google defaults for geocoding/imagery, Mapbox fallback and dense-core imagery, OSM -> NYC -> Microsoft footprint fallback, two zoom levels, optional YOLO model ensemble through `MODEL_PATHS`, and one address-level `vlm.verify_address()` call.

Do not rely on older docs or code comments as current behavior; verify against the code.

## Active Files

Runtime:

- `app_railway.py`
- `api_analyze.py`
- `worker.py`
- `job_queue.py`
- `storage_helpers.py`
- `workbook_runs.py`
- `intake_resolver.py`
- `sheets_writer.py`
- `drive_inbox.py`
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
- `sheets_writer.py`
- `zip_bundler.py`

Assets/config:

- `models/rooftop_model.pt`
- `models/rooftop_model_prev.pt`
- `reference_images/`
- `templates/`
- `static/images/`
- `Dockerfile.railway`
- `requirements_railway.txt`
The `n8n/` directory is inactive reference material. Current production does
not use or require n8n.

## Current Pipeline

1. Compose address from CSV/API row.
2. Geocode with `GEOCODER_PROVIDER` defaulting to `google`.
3. Validate address with Google Address Validation where available.
4. Resolve footprint via OSM Overpass, NYC planimetric fallback, then Microsoft fallback.
5. Fetch detail and wide satellite tiles using `IMAGERY_PROVIDER` defaulting to `google`; dense urban cores can use Mapbox.
6. Run YOLO detections via `_get_models()`. `MODEL_PATHS` controls whether this is single-model or ensemble.
7. Filter detections in geo-space against the target footprint.
8. Render marked tiles and a close-up.
9. Call `verify_address()` once. Gemini is the normal reviewer; Grok is limited to one emergency request after a technical Gemini failure.
10. Produce CSV rows plus audit-card web entries.

The older `verify_detection()` and `verify_rooftop()` functions remain in `vlm.py`, but they are not the active per-address path in `tasks_local.py`.

## Environment

Required for production:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `ANALYZE_API_KEY` for stateless API routes
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `SHEET_PARENT_FOLDER_ID`

`XAI_API_KEY` is optional. It is used only for ambiguous redacted workbook
schema assistance and the one-request emergency VLM fallback.

Optional (live review-to-sheet loop; without them `sheet_url` stays empty and the
operator/skill flow is the fallback):

- `GOOGLE_SERVICE_ACCOUNT_JSON` (or `GOOGLE_SERVICE_ACCOUNT_FILE` locally) — enables
  `sheets_writer.py`: the batch's Google Sheet is created at run time and each
  review Submit writes its row live
- `SHEET_PARENT_FOLDER_ID` — Drive folder (shared to the service account as Editor)
  where sheets are created; required in practice because service accounts have zero
  Drive storage quota of their own
- `SHEET_SHARE_WITH` — comma-separated emails granted writer access to created sheets

Important defaults:

- `GEOCODER_PROVIDER=google`
- `IMAGERY_PROVIDER=google`
- `MODEL_PATH=models/rooftop_model.pt`
- `MODEL_PATHS` unset means only `MODEL_PATH` is used
- `YOLO_CONF=0.18`
- `MAPBOX_ZOOM=19`
- `MAPBOX_ZOOM_WIDE=18`
- `VLM_ADDRESS_CONCURRENCY=5`
- `MULTI_TAB_WORKBOOK_ENABLED=true`
- `MULTI_TAB_BROWSER_ENABLED=true`
- `MULTI_TAB_DRIVE_ENABLED=true`
- `MULTI_TAB_API_ENABLED=true`
- `WORKBOOK_CHUNK_ROWS=100`
- `WORKBOOK_AUTO_APPROVAL_ROWS=250`
- `GEMINI_MODEL=gemini-3.6-flash`
- `GROK_MODEL=grok-4.3`

## Development Notes

- Use `rg`/`rg --files` for search.
- Treat untracked or ignored generated files as local artifacts unless the task says otherwise.
- Do not delete `.env`.
- Do not delete model weights, `nyc_opendata.py`, or `ms_footprints.py`.
- Be careful with `.ms_footprint_cache/`: it is safe to recreate, but deleting it may slow the next Microsoft-footprint fallback.
- `storage/`, `temp_uploads/`, `jobs.db`, `pipeline_test_outputs/`, `.local_archive/`, and `scratch_*` files are local runtime/output clutter.
- `test_vlm.py` spends Gemini/Grok API calls.
- `test_pipeline_no_vlm.py` still uses external geocoding/imagery/footprint services and the YOLO model.

## Verification

Cheap syntax check:

```bash
python -m compileall app_railway.py api_analyze.py workbook_runs.py intake_resolver.py sheets_writer.py drive_inbox.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py
python test_workbook_multitab.py
```

Run spend/network tests only when the user asks for them or provides explicit approval/context.
