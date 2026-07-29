# AGENTS.md

Guidance for coding agents working in this repository.

## Current Project State

This is the Parity building analysis tool for detecting cooling towers from satellite imagery. The active implementation is the `piece4-concurrency` branch.

The current system has three front doors backed by the same durable workbook
engine where applicable:

1. Browser `.xlsx`/CSV upload and live Google Sheet intake.
2. Drive inbox intake.
3. Versioned API intake through `POST /api/v2/workbook-runs`.

`POST /api/run` and `/api/run-file` remain legacy single-table compatibility
paths. Multi-tab workbooks fail closed there instead of selecting one tab.

The active per-address pipeline uses Google geocoding/imagery by default, Mapbox fallback/dense-core imagery, OSM -> NYC -> Microsoft footprint fallback, two zoom levels, optional YOLO ensemble through `MODEL_PATHS`, and one address-level `vlm.verify_address()` call.

## Files That Matter

Keep and treat as active:

- `app_railway.py`
- `api_analyze.py`
- `worker.py`
- `job_queue.py`
- `storage_helpers.py`
- `workbook_runs.py`
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
- `models/`
- `reference_images/`
- `templates/`
- `static/images/`
- `Dockerfile.railway`
- `requirements_railway.txt`
- `.claude/skills/parity-cooling-tower/SKILL.md`

The `n8n/` directory is retained as inactive reference material. There is no
deployed n8n service or active n8n runtime dependency.

Presentation docs live in `docs/presentation/`.

## Environment

Required in production:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `ANALYZE_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `SHEET_PARENT_FOLDER_ID`

`XAI_API_KEY` is optional for ordinary deterministic workbooks and is required
only when Grok must assist with an ambiguous schema or the emergency VLM
fallback is enabled.

Important defaults:

- `GEOCODER_PROVIDER=google`
- `IMAGERY_PROVIDER=google`
- `MODEL_PATH=models/rooftop_model.pt`
- `MODEL_PATHS` unset means single-model inference
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

## Working Rules

- Use `rg` or `rg --files` first for search.
- Do not delete or revert user changes.
- Do not delete `.env`.
- Do not delete model weights.
- Do not assume ignored files are disposable without checking the task; many are generated outputs, but some may be local evidence from previous experiments.
- Treat `.local_archive/`, `scratch_*`, `storage/`, `temp_uploads/`, `pipeline_test_outputs/`, `jobs.db`, and `.ms_footprint_cache/` as local-only clutter unless the user asks to inspect them.
- Avoid paid/API-spend tests unless the user explicitly asks.

## Cheap Verification

```bash
python -m compileall app_railway.py api_analyze.py workbook_runs.py intake_resolver.py sheets_writer.py drive_inbox.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py
python test_workbook_multitab.py
```
