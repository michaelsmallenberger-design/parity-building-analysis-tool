# Known Issues

Current as of the `piece4-concurrency` branch.

## Documentation And Tests

- `test_vlm.py` still exercises the older `verify_detection()` / `verify_rooftop()` helpers. The active pipeline uses `verify_address()`.
- `test_geometry_math.py` is a diagnostic script, not a normal unit test. It calls external services and writes images.
- Some archived docs under `docs/archive/` describe older Mapbox-first and per-box-VLM behavior. Treat them as historical notes only.

## Runtime Behavior

- `/api/run` processes a batch sequentially inside one HTTP request. Very large batches can run for a long time and return large base64-embedded HTML.
- `/api/run` bypasses the browser UI's SQLite monthly usage accounting.
- Local browser results are written to ephemeral filesystem storage. Redeploys can remove them.
- `MODEL_PATHS` is env-dependent. The repo ships two weights, but only `MODEL_PATH` is loaded unless `MODEL_PATHS` names both.
- Microsoft footprint fallback can use a large local `.ms_footprint_cache/` cache. It is ignored and safe to recreate.

## Data Quality

- Bad or partial addresses can still geocode incorrectly. Google Address Validation helps, but ambiguous sales-list rows should be normalized before analysis.
- Footprint coverage depends on OSM, NYC planimetric data, and Microsoft footprint availability. Missing footprints are surfaced as review cases rather than guessed.
- Dense urban imagery provider selection is heuristic. It exists because Google satellite imagery can be less roof-readable in some dense cores.

## Follow-Ups Worth Considering

- Add a current `verify_address()` integration harness.
- Add chunked or asynchronous `/api/run` for large n8n batches.
- Return tabular data from `/api/run` if n8n needs an Excel/CSV attachment.
- Add negative reference images under `reference_images/negative/`.
