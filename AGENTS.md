# AGENTS.md

Guidance for coding agents working in this repository.

## Current Project State

This is the Parity building analysis tool for detecting cooling towers from satellite imagery. The active implementation is the `piece4-concurrency` branch.

The current system has two front doors:

1. Browser CSV upload through `app_railway.py`, `worker.py`, `job_queue.py`, and `tasks_local.py`.
2. Stateless n8n/API flow through `api_analyze.py`, especially `POST /api/run`.
3. File-upload automation through `POST /api/run-file`, which accepts `.xlsx`, `.xls`, or `.csv` and returns the same audit-card HTML.

The active per-address pipeline uses Google geocoding/imagery by default, Mapbox fallback/dense-core imagery, OSM -> NYC -> Microsoft footprint fallback, two zoom levels, optional YOLO ensemble through `MODEL_PATHS`, and one address-level `vlm.verify_address()` call.

## Files That Matter

Keep and treat as active:

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
- `models/`
- `reference_images/`
- `templates/`
- `static/images/`
- `Dockerfile.railway`
- `requirements_railway.txt`
- `n8n/README.md`
- `n8n/parity_cooling_tower.workflow.json`
- `n8n/parity_excel_upload.workflow.json`
- `n8n/EXCEL_UPLOAD_WORKFLOW.md`
- `n8n/address_upload_template.xlsx`

Historical docs live in `docs/archive/`. Presentation docs live in `docs/presentation/`.

## Environment

Required in production:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`
- `ANALYZE_API_KEY`

Important defaults:

- `GEOCODER_PROVIDER=google`
- `IMAGERY_PROVIDER=google`
- `MODEL_PATH=models/rooftop_model.pt`
- `MODEL_PATHS` unset means single-model inference
- `YOLO_CONF=0.18`
- `MAPBOX_ZOOM=19`
- `MAPBOX_ZOOM_WIDE=18`
- `VLM_ADDRESS_CONCURRENCY=5`
- `GEMINI_MODEL=gemini-3.5-flash`
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
python -m compileall app_railway.py api_analyze.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py
```
