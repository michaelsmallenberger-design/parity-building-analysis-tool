"""No-spend regression tests for review submission and write-back."""
import tempfile
from pathlib import Path

import app_railway
import api_analyze
import pandas as pd
import review_store
import sheets_writer
import storage_helpers
import workbook_runs
from review_contract import DUAL_FIT_SCHEMA


class IsolatedReviews:
    def __init__(self, root):
        self.root = Path(root)

    def __enter__(self):
        self.old_storage = storage_helpers.STORAGE_BASE
        self.old_uploads = storage_helpers.UPLOADS_DIR
        self.old_results = storage_helpers.RESULTS_DIR
        storage_helpers.STORAGE_BASE = self.root / "storage"
        storage_helpers.UPLOADS_DIR = storage_helpers.STORAGE_BASE / "uploads"
        storage_helpers.RESULTS_DIR = storage_helpers.STORAGE_BASE / "results"
        storage_helpers.init_storage()
        return self

    def __exit__(self, *_args):
        storage_helpers.STORAGE_BASE = self.old_storage
        storage_helpers.UPLOADS_DIR = self.old_uploads
        storage_helpers.RESULTS_DIR = self.old_results


def _csrf(client):
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_single_fit_submission_returns_200_and_updates_exact_source(root):
    with IsolatedReviews(root):
        review_store.save_batch(
            "w-review",
            "Regions",
            [{
                "row_id": "sabc:g10:r2",
                "source_grid_id": 10,
                "source_tab": "Washington",
                "source_row": 2,
                "address": "1 Main St",
            }],
            sheet_url="https://docs.google.com/spreadsheets/d/sheet/edit",
            sheet_bindings=[{
                "spreadsheet_id": "sheet",
                "grid_id": 10,
                "tab": "Washington",
                "headers": ["Property Address", "HVAC Systems", "Fit"],
            }],
            schema_version=2,
            run_id="w-review",
        )
        originals = {
            "enabled": sheets_writer.enabled,
            "ensure": sheets_writer.ensure_review_columns,
            "write": sheets_writer.write_decision_source,
            "metric": workbook_runs.record_metric,
            "progress": workbook_runs.update_review_progress,
        }
        writes = []
        progress = []
        try:
            sheets_writer.enabled = lambda: True

            def fake_ensure(binding, *_args, **_kwargs):
                binding["colmap"] = {"HVAC Systems": 1, "Fit": 2}
                return binding

            sheets_writer.ensure_review_columns = fake_ensure
            sheets_writer.write_decision_source = (
                lambda binding, row, **values:
                writes.append((binding["tab"], row, values)) or True
            )
            workbook_runs.record_metric = lambda *_args, **_kwargs: None
            workbook_runs.update_review_progress = (
                lambda *args, **kwargs: progress.append((args, kwargs))
            )

            client = app_railway.app.test_client()
            assert client.get("/review/w-review").status_code == 200
            response = client.post(
                "/api/review",
                json={
                    "job_id": "w-review",
                    "row_id": "sabc:g10:r2",
                    "hvac_systems": "Cooling Tower, AHU",
                    "fit": "Optimizer",
                    "note": "verified",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.json["sheet"] == "updated"
            assert writes == [(
                "Washington",
                2,
                {
                    "hvac": "Cooling Tower, AHU",
                    "optimizer_fit": "",
                    "periscope_fit": "",
                    "note": "verified",
                    "fit": "Optimizer",
                },
            )]
            human = review_store.load_batch("w-review")["entries"][0]["human"]
            assert human["fit"] == "Optimizer"
            assert "optimizer_fit" not in human and "periscope_fit" not in human
            assert progress and progress[-1][0][1:3] == (1, 1)

            invalid = client.post(
                "/api/review",
                json={
                    "job_id": "w-review",
                    "row_id": "sabc:g10:r2",
                    "hvac_systems": "AHU",
                    "optimizer_fit": "Good",
                    "periscope_fit": "Bad",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert invalid.status_code == 400
            assert invalid.json["error"] == "Fit is required"
        finally:
            sheets_writer.enabled = originals["enabled"]
            sheets_writer.ensure_review_columns = originals["ensure"]
            sheets_writer.write_decision_source = originals["write"]
            workbook_runs.record_metric = originals["metric"]
            workbook_runs.update_review_progress = originals["progress"]


def test_true_historical_dual_submission_remains_valid(root):
    with IsolatedReviews(root):
        review_store.save_batch(
            "legacy",
            "Legacy",
            [{"i": 1, "address": "1 Main St"}],
            table_headers=[
                "HVAC Systems", "Optimizer Fit", "Periscope Fit", "Row_ID",
            ],
            table_rows=[["", "", "", "1"]],
            review_schema=DUAL_FIT_SCHEMA,
        )
        client = app_railway.app.test_client()
        assert client.get("/review/legacy").status_code == 200
        response = client.post(
            "/api/review",
            json={
                "job_id": "legacy",
                "row_id": 1,
                "hvac_systems": "None",
                "optimizer_fit": "Bad",
                "periscope_fit": "Good",
            },
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert response.status_code == 200


def test_pre_fix_multitab_batch_infers_single_fit_without_guessing(root):
    with IsolatedReviews(root):
        storage_helpers.write_json("reviews/pre-fix.json", {
            "job_id": "pre-fix",
            "schema_version": 2,
            "sheet_bindings": [{
                "headers": [
                    "Property Address", "HVAC Systems", "Fit",
                    "Optimizer Fit", "Periscope Fit",
                ],
            }],
            "entries": [{
                "row_id": "g10:r2",
                "human": {
                    "hvac_systems": "AHU, VRF",
                    "optimizer_fit": "Bad",
                    "periscope_fit": "Good",
                },
            }],
        })
        batch = review_store.load_batch("pre-fix")
        assert batch["review_schema"] == "single_fit_v1"
        assert review_store.decisions_for("pre-fix") == []


def test_upload_mapping_preserves_single_fit_and_does_not_guess_dual_fields():
    headers, rows = api_analyze._table_from_df(pd.DataFrame({
        "Property Address": ["1 Main St"],
        "HVAC Systems": ["AHU"],
        "Fit": ["Periscope"],
    }))
    assert rows[0][headers.index("Fit")] == "Periscope"

    headers, rows = api_analyze._table_from_df(pd.DataFrame({
        "Property Address": ["1 Main St"],
        "HVAC Systems": ["AHU"],
        "Optimizer Fit": ["Good"],
        "Periscope Fit": ["Bad"],
    }))
    assert rows[0][headers.index("Fit")] == ""


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        test_single_fit_submission_returns_200_and_updates_exact_source(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_true_historical_dual_submission_remains_valid(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_pre_fix_multitab_batch_infers_single_fit_without_guessing(temp_dir)
    test_upload_mapping_preserves_single_fit_and_does_not_guess_dual_fields()
    print("OK: review API single-Fit and historical compatibility hold.")
