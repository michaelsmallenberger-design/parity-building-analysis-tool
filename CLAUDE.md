# CLAUDE.md

Repository guidance for Claude Code and other coding agents.

## Project Reality

This repository is the Parity cooling-tower analyzer. It is currently a Render-hosted Flask app with two front doors:

1. Browser UI: CSV upload -> SQLite queue -> background worker -> results page/files.
2. Stateless API: `/api/run` accepts a batch of addresses and returns self-contained audit-card HTML for n8n.

The active pipeline is the Phase 4 shape on branch `piece4-concurrency`: Google defaults for geocoding/imagery, Mapbox fallback and dense-core imagery, OSM -> NYC -> Microsoft footprint fallback, two zoom levels, optional YOLO model ensemble through `MODEL_PATHS`, and one address-level `vlm.verify_address()` call.

Do not rely on older docs or code comments as current behavior; verify against the code.

## Active Files

Runtime:

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

Assets/config:

- `models/rooftop_model.pt`
- `models/rooftop_model_prev.pt`
- `reference_images/`
- `templates/`
- `static/images/`
- `Dockerfile.railway`
- `requirements_railway.txt`
- `n8n/README.md`
- `n8n/parity_cooling_tower.workflow.json`

## Current Pipeline

1. Compose address from CSV/API row.
2. Geocode with `GEOCODER_PROVIDER` defaulting to `google`.
3. Validate address with Google Address Validation where available.
4. Resolve footprint via OSM Overpass, NYC planimetric fallback, then Microsoft fallback.
5. Fetch detail and wide satellite tiles using `IMAGERY_PROVIDER` defaulting to `google`; dense urban cores can use Mapbox.
6. Run YOLO detections via `_get_models()`. `MODEL_PATHS` controls whether this is single-model or ensemble.
7. Filter detections in geo-space against the target footprint.
8. Render marked tiles and a close-up.
9. Call `verify_address()` once. Gemini and Grok run in parallel and combine by consensus.
10. Produce CSV rows plus audit-card web entries.

The older `verify_detection()` and `verify_rooftop()` functions remain in `vlm.py`, but they are not the active per-address path in `tasks_local.py`.

## Environment

Required for production:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`
- `ANALYZE_API_KEY` for stateless API routes

Important defaults:

- `GEOCODER_PROVIDER=google`
- `IMAGERY_PROVIDER=google`
- `MODEL_PATH=models/rooftop_model.pt`
- `MODEL_PATHS` unset means only `MODEL_PATH` is used
- `YOLO_CONF=0.18`
- `MAPBOX_ZOOM=19`
- `MAPBOX_ZOOM_WIDE=18`
- `VLM_ADDRESS_CONCURRENCY=5`
- `GEMINI_MODEL=gemini-3.5-flash`
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
python -m compileall app_railway.py api_analyze.py tasks_local.py utils.py geometry.py vlm.py pipeline_render.py report_audit.py job_queue.py worker.py storage_helpers.py
```

Run spend/network tests only when the user asks for them or provides explicit approval/context.
