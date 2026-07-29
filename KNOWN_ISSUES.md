# Known Issues

Current as of the `piece4-concurrency` branch.

## Documentation And Tests

- `test_vlm.py` still exercises the older `verify_detection()` / `verify_rooftop()` helpers. The active pipeline uses `verify_address()`.
- `test_geometry_math.py` is a diagnostic script, not a normal unit test. It calls external services and writes images.

## Runtime Behavior

- `/api/run` processes a batch sequentially inside one HTTP request. Very large batches can run for a long time and return large base64-embedded HTML.
- `/api/run` bypasses the browser UI's SQLite monthly usage accounting.
- Local development uses local filesystem storage. Production workbook
  manifests, checkpoints, review batches, and SQLite state must stay on the
  mounted Render disk through `STORAGE_DIR` and `JOBS_DB_PATH`.
- `MODEL_PATHS` is env-dependent. The repo ships two weights, but only `MODEL_PATH` is loaded unless `MODEL_PATHS` names both.
- Microsoft footprint fallback can use a large local `.ms_footprint_cache/` cache. It is ignored and safe to recreate.

## Data Quality

- Bad or partial addresses can still geocode incorrectly. Google Address Validation helps, but ambiguous sales-list rows should be normalized before analysis.
- Footprint coverage depends on OSM, NYC planimetric data, and Microsoft footprint availability. Missing footprints are surfaced as review cases rather than guessed.
- Dense urban imagery provider selection is heuristic. It exists because Google satellite imagery can be less roof-readable in some dense cores.

## Follow-Ups Worth Considering

- Add a current `verify_address()` integration harness.
- Add negative reference images under `reference_images/negative/`.

The repository retains old n8n workflow exports as inactive reference
artifacts. There is no current n8n deployment or migration requirement.
