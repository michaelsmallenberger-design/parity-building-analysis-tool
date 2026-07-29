"""No-network tests for the completed-run review-schema migration."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile

from migrate_review_batch_schema import (
    DUAL_FIT_SCHEMA,
    MigrationRefused,
    SINGLE_FIT_SCHEMA,
    migrate,
)
from review_render import build_review_page


RUN_ID = "w-9952c459e8a9"


def _entry(row: int, verdict: str = "confirmed") -> dict:
    source_key = f"sabc:g10:r{row}"
    return {
        "row_id": source_key,
        "i": source_key,
        "source_key": source_key,
        "source_grid_id": 10,
        "source_tab": "DC",
        "source_row": row,
        "analysis_key": f"analysis-{row}",
        "address": f"{row} Example Street, Washington, DC",
        "verdict": verdict,
        "reasoning": "machine result retained",
    }


def _write_fixture(root: Path, entries: list[dict]) -> None:
    review = {
        "job_id": RUN_ID,
        "title": "DMV Hotels Analysis",
        "sheet_url": "https://docs.google.com/spreadsheets/d/sheet/edit",
        "sheet_binding": None,
        "sheet_bindings": [
            {
                "spreadsheet_id": "sheet",
                "grid_id": 10,
                "tab": "DC",
                "headers": [
                    "Optimizer Fit",
                    "Periscope Fit",
                    "HVAC systems",
                    "Notes",
                    "Property Address",
                ],
                "row_numbers": [entry["source_row"] for entry in entries],
                "colmap": {
                    "HVAC Systems": 2,
                    "Fit": 5,
                    "Notes": 3,
                },
            }
        ],
        "tab_inventory": [{"tab": "DC", "status": "selected"}],
        "schema_version": 2,
        "review_schema": SINGLE_FIT_SCHEMA,
        "run_id": RUN_ID,
        "created": "2026-07-29T00:00:00",
        "table_headers": [],
        "table_rows": [],
        "entries": entries,
    }
    review_path = root / "reviews" / f"{RUN_ID}.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(json.dumps(review), encoding="utf-8")

    result_blob = f"workbook_runs/{RUN_ID}/chunks/0000.result.json"
    chunk_path = root / result_blob
    chunk_path.parent.mkdir(parents=True)
    chunk_path.write_text(
        json.dumps({"schema_version": 2, "entries": entries}),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "run_id": RUN_ID,
        "status": "review_open",
        "row_count": len(entries),
        "analysis_count": len(entries),
        "completed_rows": len(entries),
        "reviewed_rows": 0,
        "target_order": [entry["source_key"] for entry in entries],
        "chunks": [
            {
                "index": 0,
                "status": "complete",
                "result_blob": result_blob,
                "items": [],
            }
        ],
    }
    (root / "workbook_runs" / RUN_ID / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def test_dry_run_and_apply_preserve_analysis_and_rebind_dual_columns(root):
    entries = [_entry(2), _entry(3, verdict="not_detected")]
    original_entries = copy.deepcopy(entries)
    _write_fixture(root, entries)
    review_path = root / "reviews" / f"{RUN_ID}.json"
    before = review_path.read_bytes()

    dry_run = migrate(root, RUN_ID, 2)
    assert dry_run["dry_run"] is True
    assert dry_run["would_change"] is True
    assert dry_run["entry_count"] == 2
    assert review_path.read_bytes() == before

    applied = migrate(root, RUN_ID, 2, apply=True)
    assert applied["applied"] is True
    assert Path(applied["backup_path"]).read_bytes() == before

    migrated = json.loads(review_path.read_text(encoding="utf-8"))
    assert migrated["review_schema"] == DUAL_FIT_SCHEMA
    assert migrated["entries"] == original_entries
    assert migrated["sheet_bindings"][0]["colmap"] == {
        "HVAC Systems": 2,
        "Optimizer Fit": 0,
        "Periscope Fit": 1,
        "Notes": 3,
    }

    # Once the deployed dual renderer reads the migrated batch, a positive
    # machine result starts with Cooling Tower + Optimizer Good selected.
    page = build_review_page(
        migrated["entries"],
        RUN_ID,
        review_schema=migrated["review_schema"],
    )
    assert 'class="chip sel" data-sys="Cooling Tower"' in page
    assert (
        'data-col="optimizer_fit"><button type="button" '
        'class="fitchip sel" data-fit="Good"'
    ) in page
    periscope = page.split('data-col="periscope_fit">', 1)[1].split("</div>", 1)[0]
    assert 'class="fitchip sel"' not in periscope


def test_existing_human_review_refuses_instead_of_guessing(root):
    entries = [_entry(2)]
    entries[0]["human"] = {
        "hvac_systems": "Cooling Tower",
        "fit": "Optimizer",
        "reviewed_at": "2026-07-29T00:01:00",
    }
    _write_fixture(root, entries)

    try:
        migrate(root, RUN_ID, 1, apply=True)
    except MigrationRefused as exc:
        assert "human decision" in str(exc)
    else:
        raise AssertionError("migration should refuse an existing human decision")


def test_mismatched_checkpoint_refuses_without_writing(root):
    entries = [_entry(2), _entry(3)]
    _write_fixture(root, entries)
    chunk_path = (
        root
        / "workbook_runs"
        / RUN_ID
        / "chunks"
        / "0000.result.json"
    )
    chunk_path.write_text(
        json.dumps({"schema_version": 2, "entries": [entries[0]]}),
        encoding="utf-8",
    )
    review_path = root / "reviews" / f"{RUN_ID}.json"
    before = review_path.read_bytes()

    try:
        migrate(root, RUN_ID, 2, apply=True)
    except MigrationRefused as exc:
        assert "chunk results" in str(exc)
    else:
        raise AssertionError("migration should refuse mismatched checkpoints")
    assert review_path.read_bytes() == before


def test_deduplicated_analysis_checkpoint_order_may_differ(root):
    # Duplicate-address reuse emits all targets for the first analysis item
    # together in a chunk, while the final batch is restored to source-row order.
    entries = [_entry(2), _entry(3), _entry(4)]
    _write_fixture(root, entries)
    chunk_path = (
        root
        / "workbook_runs"
        / RUN_ID
        / "chunks"
        / "0000.result.json"
    )
    chunk_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "entries": [entries[0], entries[2], entries[1]],
            }
        ),
        encoding="utf-8",
    )
    receipt = migrate(root, RUN_ID, 3)
    assert receipt["entry_count"] == 3
    assert receipt["would_change"] is True


if __name__ == "__main__":
    tests = [
        test_dry_run_and_apply_preserve_analysis_and_rebind_dual_columns,
        test_existing_human_review_refuses_instead_of_guessing,
        test_mismatched_checkpoint_refuses_without_writing,
        test_deduplicated_analysis_checkpoint_order_may_differ,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory(prefix="parity-schema-migration-") as tmp:
            test(Path(tmp))
    print(f"{len(tests)} review batch schema migration tests passed")
