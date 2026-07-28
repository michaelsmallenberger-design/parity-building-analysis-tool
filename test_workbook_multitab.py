"""No-spend contracts for the multi-tab workbook engine.

Every external provider and Google API surface is replaced with local fakes.
Run with ``python test_workbook_multitab.py``.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

from flask import Flask
from openpyxl import Workbook
from openpyxl.worksheet.datavalidation import DataValidation

import api_analyze
import intake_resolver
import job_queue
import review_render
import sheets_writer
import storage_helpers
import tasks_local
import workbook_runs


class IsolatedState:
    def __init__(self, root):
        self.root = Path(root)

    def __enter__(self):
        self.old_storage = storage_helpers.STORAGE_BASE
        self.old_uploads = storage_helpers.UPLOADS_DIR
        self.old_results = storage_helpers.RESULTS_DIR
        self.old_db = job_queue.DB_PATH
        storage_helpers.STORAGE_BASE = self.root / "storage"
        storage_helpers.UPLOADS_DIR = storage_helpers.STORAGE_BASE / "uploads"
        storage_helpers.RESULTS_DIR = storage_helpers.STORAGE_BASE / "results"
        job_queue.DB_PATH = self.root / "jobs.db"
        storage_helpers.init_storage()
        job_queue.init_db()
        return self

    def __exit__(self, *_args):
        storage_helpers.STORAGE_BASE = self.old_storage
        storage_helpers.UPLOADS_DIR = self.old_uploads
        storage_helpers.RESULTS_DIR = self.old_results
        job_queue.DB_PATH = self.old_db


def make_regional_workbook(path, hidden=False, ambiguous=False):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in ("Washington", "Virginia", "New York City"):
        sheet = workbook.create_sheet(name)
        sheet.append(["Street" if ambiguous else "Property Address", "Status"])
        sheet.append([f"1 {name} St", "New"])
        sheet.append([f"2 {name} St", "Done"])
        validation = DataValidation(
            type="list", formula1='"New,Done"', allow_blank=True,
            showErrorMessage=True, errorStyle="stop",
        )
        sheet.add_data_validation(validation)
        validation.add("B2:B3")
    if hidden:
        workbook["Virginia"].sheet_state = "hidden"
    workbook.save(path)
    workbook.close()


def test_inventory_and_grok_guardrails(root):
    path = Path(root) / "regions.xlsx"
    make_regional_workbook(path)
    snapshot = workbook_runs.inspect_xlsx(path)
    original = intake_resolver._grok_workbook_suggestions
    try:
        intake_resolver._grok_workbook_suggestions = lambda _tabs: (
            _ for _ in ()
        ).throw(AssertionError("Grok must not run for standard headers"))
        resolution = intake_resolver.resolve_workbook_schema(
            snapshot["tabs"], tasks_local.ADDRESS_VARIANTS,
        )
        assert resolution["status"] == "ready"
        assert [tab["tab"] for tab in resolution["tabs"] if tab["status"] == "selected"] == [
            "Washington", "Virginia", "New York City",
        ]
        assert [tab["row_count"] for tab in resolution["tabs"]] == [2, 2, 2]
        assert snapshot["dropdowns"][0]["inline_options"] == ["New", "Done"]

        hidden_path = Path(root) / "hidden.xlsx"
        make_regional_workbook(hidden_path, hidden=True)
        hidden = intake_resolver.resolve_workbook_schema(
            workbook_runs.inspect_xlsx(hidden_path)["tabs"],
            tasks_local.ADDRESS_VARIANTS,
        )
        assert hidden["status"] == "confirmation_required"
        assert next(tab for tab in hidden["tabs"] if tab["tab"] == "Virginia")[
            "status"
        ] == "confirmation_required"

        ambiguous_path = Path(root) / "ambiguous.xlsx"
        make_regional_workbook(ambiguous_path, ambiguous=True)
        ambiguous_tabs = workbook_runs.inspect_xlsx(ambiguous_path)["tabs"]
        intake_resolver._grok_workbook_suggestions = lambda tabs: (
            [{
                "tab": tabs[0]["tab"],
                "classification": "address",
                "address_column": "Street",
                "city_column": None,
                "state_column": None,
                "zip_column": None,
                "confidence": 0.99,
                "reason": "street-shaped values",
            }],
            "",
        )
        unresolved = intake_resolver.resolve_workbook_schema(
            ambiguous_tabs, tasks_local.ADDRESS_VARIANTS,
        )
        assert unresolved["status"] == "confirmation_required"
        assert all(
            tab["status"] == "unresolved" for tab in unresolved["tabs"]
        ), "Grok may not omit workbook tabs"
    finally:
        intake_resolver._grok_workbook_suggestions = original


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeConvertedSheets:
    def __init__(self, mismatch=False):
        self.mismatch = mismatch
        self.last_get = None

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        if "includeGridData" in kwargs:
            options = ["New", "Changed" if self.mismatch else "Done"]
            rule = {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": value} for value in options],
                },
                "strict": True,
            }
            return FakeRequest({
                "sheets": [{
                    "data": [{
                        "rowData": [
                            {"values": [{"dataValidation": rule}]},
                            {"values": [{"dataValidation": rule}]},
                        ]
                    }]
                }]
            })
        if kwargs.get("fields", "").startswith("sheets.properties"):
            return FakeRequest({
                "sheets": [{
                    "properties": {
                        "sheetId": 10, "title": "Washington", "hidden": False,
                    }
                }]
            })
        return FakeRequest({"values": [["Property Address", "Status"]]})


def test_dropdown_conversion_fails_closed():
    snapshot = {
        "sheet_states": [{"tab": "Washington", "hidden": False}],
        "tabs": [{
            "tab": "Washington", "hidden": False,
            "headers": ["Property Address", "Status"], "row_count": 2,
        }],
        "dropdowns": [{
            "tab": "Washington",
            "ranges": ["B2:B3"],
            "formula1": '"New,Done"',
            "inline_options": ["New", "Done"],
            "resolved_options": None,
            "show_error": True,
            "error_style": "stop",
            "source_max_row": 3,
            "source_max_col": 2,
        }],
    }
    original = sheets_writer._get_services
    try:
        sheets_writer._get_services = lambda: (FakeConvertedSheets(), None)
        assert workbook_runs.verify_converted_sheet("sheet", snapshot)[
            "dropdown_cells"
        ] == 2
        sheets_writer._get_services = lambda: (
            FakeConvertedSheets(mismatch=True), None,
        )
        try:
            workbook_runs.verify_converted_sheet("sheet", snapshot)
            raise AssertionError("changed dropdown options were accepted")
        except ValueError as exc:
            assert "dropdown options" in str(exc)
    finally:
        sheets_writer._get_services = original


def test_queue_chunking_duplicate_reuse_and_atomic_approval(root):
    with IsolatedState(root):
        old_threshold = os.environ.get("WORKBOOK_AUTO_APPROVAL_ROWS")
        old_min = os.environ.get("ANALYZE_ESTIMATED_MIN_COST_PER_ADDRESS")
        old_max = os.environ.get("ANALYZE_ESTIMATED_MAX_COST_PER_ADDRESS")
        original_read = sheets_writer.read_bound_sheet_mappings
        original_columns = sheets_writer.ensure_review_columns
        try:
            os.environ["WORKBOOK_AUTO_APPROVAL_ROWS"] = "2"
            os.environ["ANALYZE_ESTIMATED_MIN_COST_PER_ADDRESS"] = "0.01"
            os.environ["ANALYZE_ESTIMATED_MAX_COST_PER_ADDRESS"] = "0.02"
            sheets_writer.ensure_review_columns = lambda binding, *_args, **_kwargs: (
                binding.update({"colmap": {"HVAC Systems": 1}}) or binding
            )
            sheets_writer.read_bound_sheet_mappings = lambda _url, _mappings: [
                {
                    "spreadsheet_id": "sheet", "grid_id": 10, "tab": "Washington",
                    "headers": ["Address"], "row_numbers": [2, 3],
                    "targets": [
                        {"source_key": "g10:r2", "grid_id": 10, "tab": "Washington",
                         "source_row": 2, "address": "1 Main St"},
                        {"source_key": "g10:r3", "grid_id": 10, "tab": "Washington",
                         "source_row": 3, "address": "2 Main St"},
                    ],
                },
                {
                    "spreadsheet_id": "sheet", "grid_id": 20, "tab": "Virginia",
                    "headers": ["Address"], "row_numbers": [2],
                    "targets": [
                        {"source_key": "g20:r2", "grid_id": 20, "tab": "Virginia",
                         "source_row": 2, "address": " 1  MAIN ST "},
                    ],
                },
            ]
            run = {
                "run_id": "w-test",
                "sheet_url": "https://docs.google.com/spreadsheets/d/sheet/edit",
                "tabs": [
                    {"tab": "Washington", "status": "selected",
                     "mapping": {"tab": "Washington", "address_column": "Address"}},
                    {"tab": "Virginia", "status": "selected",
                     "mapping": {"tab": "Virginia", "address_column": "Address"}},
                ],
                "created_at": "now",
            }
            built = workbook_runs._build_queue(run)
            assert built["row_count"] == 3
            assert built["analysis_count"] == 2
            assert built["status"] == "approval_required"
            approved = workbook_runs._enqueue(built, approved=True)
            assert approved["status"] == "queued"
            assert job_queue.get_job_status("w-test")["total"] == 2
            assert job_queue.get_monthly_usage() == 3

            job_queue.update_job_status("w-test", status="processing")
            assert job_queue.recover_interrupted_workbook_jobs() == 1
            assert job_queue.get_job_status("w-test")["status"] == "queued"
            job_queue.update_job_status("w-test", status="failed")
            workbook_runs.mutate_run(
                "w-test",
                lambda current: current.update({
                    "status": "needs_attention",
                    "error": "simulated worker stop",
                }),
            )
            retried = workbook_runs.retry("w-test")
            assert retried["status"] == "queued"
            assert job_queue.get_job_status("w-test")["status"] == "queued"
            assert job_queue.get_monthly_usage() == 3
            workbook_runs.update_review_progress(
                "w-test", 3, 3, writeback_failures=1, needs_attention=0,
            )
            assert workbook_runs.load_run("w-test")["status"] == "needs_attention"
            workbook_runs.update_review_progress(
                "w-test", 3, 3, writeback_failures=0, needs_attention=0,
            )
            assert workbook_runs.load_run("w-test")["status"] == "complete"
        finally:
            sheets_writer.read_bound_sheet_mappings = original_read
            sheets_writer.ensure_review_columns = original_columns
            for name, value in (
                ("WORKBOOK_AUTO_APPROVAL_ROWS", old_threshold),
                ("ANALYZE_ESTIMATED_MIN_COST_PER_ADDRESS", old_min),
                ("ANALYZE_ESTIMATED_MAX_COST_PER_ADDRESS", old_max),
            ):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_resume_skips_checkpointed_rows(root):
    csv_path = Path(root) / "chunk.csv"
    csv_path.write_text("Address\n1 Main St\n2 Main St\n", encoding="utf-8")
    old_key = os.environ.get("MAPBOX_API_KEY")
    original = tasks_local._process_one_address
    calls = []
    try:
        os.environ["MAPBOX_API_KEY"] = "mock"

        def fake_process(row, index, *_args, **_kwargs):
            calls.append(int(index))
            address = str(row["Address"])
            return (
                {"address": address, "verdict": "not_detected", "error": ""},
                {"Address": address},
            )

        tasks_local._process_one_address = fake_process
        result = tasks_local.process_address_list(
            str(csv_path), "resume-test",
            progress_cb=lambda *_args: None,
            should_cancel=lambda: False,
            upload_file=lambda _local, blob: blob,
            make_signed_url=lambda blob: f"/files/{blob}",
            concurrency=1,
            resume_results={
                0: {
                    "web_entry": {
                        "address": "1 Main St", "verdict": "not_detected", "error": "",
                    },
                    "csv_row": {"Address": "1 Main St"},
                }
            },
        )
        assert calls == [1]
        assert [entry["address"] for entry in result["web_results"]] == [
            "1 Main St", "2 Main St",
        ]
    finally:
        tasks_local._process_one_address = original
        if old_key is None:
            os.environ.pop("MAPBOX_API_KEY", None)
        else:
            os.environ["MAPBOX_API_KEY"] = old_key


def test_grouped_review_and_exact_tab_writeback():
    entries = [
        {"row_id": "g10:r2", "source_tab": "Washington", "source_row": 2,
         "address": "1 Main St", "verdict": "not_detected"},
        {"row_id": "g20:r2", "source_tab": "Virginia", "source_row": 2,
         "address": "2 Main St", "verdict": "not_detected"},
    ]
    html = review_render.build_review_page(entries, "w-test")
    assert "<h2>Washington</h2>" in html and "<h2>Virginia</h2>" in html
    assert 'data-rid="g10:r2"' in html and 'data-rid="g20:r2"' in html
    assert "choose both Optimizer Fit and Periscope Fit" in html

    class FakeSheets:
        def __init__(self):
            self.updates = []

        def spreadsheets(self):
            return self

        def values(self):
            return self

        def batchUpdate(self, **kwargs):
            self.updates.append(kwargs)
            return FakeRequest({})

    fake = FakeSheets()
    original = sheets_writer._get_services
    try:
        sheets_writer._get_services = lambda: (fake, None)
        for tab, grid in (("Washington", 10), ("Virginia", 20)):
            assert sheets_writer.write_decision_source(
                {
                    "spreadsheet_id": "sheet", "grid_id": grid, "tab": tab,
                    "colmap": {
                        "HVAC Systems": 2, "Optimizer Fit": 3, "Periscope Fit": 4,
                    },
                },
                2, "None", "Bad", "Good",
            )
        ranges = [
            item["range"]
            for update in fake.updates
            for item in update["body"]["data"]
        ]
        assert any("'Washington'!C2" == value for value in ranges)
        assert any("'Virginia'!C2" == value for value in ranges)
    finally:
        sheets_writer._get_services = original


def test_legacy_run_file_fails_closed_on_multiple_tabs(root):
    path = Path(root) / "multi.xlsx"
    make_regional_workbook(path)
    old_key = os.environ.get("ANALYZE_API_KEY")
    try:
        os.environ["ANALYZE_API_KEY"] = "test"
        app = Flask("workbook-legacy-test")
        app.register_blueprint(api_analyze.api)
        with path.open("rb") as handle:
            response = app.test_client().post(
                "/api/run-file",
                data={"file": (io.BytesIO(handle.read()), "multi.xlsx")},
                headers={"X-API-Key": "test"},
            )
        assert response.status_code == 409
        assert response.json["status"] == "workbook_endpoint_required"
    finally:
        if old_key is None:
            os.environ.pop("ANALYZE_API_KEY", None)
        else:
            os.environ["ANALYZE_API_KEY"] = old_key


def test_versioned_async_api_contract():
    old_values = {
        name: os.environ.get(name) for name in (
            "ANALYZE_API_KEY", "MULTI_TAB_WORKBOOK_ENABLED",
            "MULTI_TAB_API_ENABLED",
        )
    }
    original_prepare = workbook_runs.prepare_local_file
    try:
        os.environ["ANALYZE_API_KEY"] = "test"
        os.environ["MULTI_TAB_WORKBOOK_ENABLED"] = "true"
        os.environ["MULTI_TAB_API_ENABLED"] = "true"
        workbook_runs.prepare_local_file = lambda *_args, **_kwargs: {
            "schema_version": 2,
            "run_id": "w-contract",
            "status": "queued",
            "source_name": "regions.csv",
            "created_at": "now",
            "updated_at": "now",
            "tabs": [{
                "tab": "Uploaded file", "headers": ["Address"],
                "status": "selected", "row_count": 2,
                "mapping": {"address_column": "Address"},
            }],
            "row_count": 2,
            "analysis_count": 2,
            "sheet_url": "https://docs.google.com/spreadsheets/d/sheet/edit",
        }
        app = Flask("workbook-v2-test")
        app.register_blueprint(api_analyze.api)
        response = app.test_client().post(
            "/api/v2/workbook-runs",
            data={"file": (io.BytesIO(b"Address\n1 Main St\n"), "regions.csv")},
            headers={"X-API-Key": "test"},
        )
        assert response.status_code == 202
        assert response.json["schema_version"] == 2
        assert response.json["run_id"] == "w-contract"
        assert response.json["status_url"].endswith(
            "/api/v2/workbook-runs/w-contract"
        )
        assert response.json["retry_url"].endswith(
            "/api/v2/workbook-runs/w-contract/retry"
        )
        assert response.json["tab_inventory"][0]["headers"] == ["Address"]
    finally:
        workbook_runs.prepare_local_file = original_prepare
        for name, value in old_values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        test_inventory_and_grok_guardrails(temp_dir)
        test_queue_chunking_duplicate_reuse_and_atomic_approval(temp_dir)
        test_resume_skips_checkpointed_rows(temp_dir)
        test_legacy_run_file_fails_closed_on_multiple_tabs(temp_dir)
    test_dropdown_conversion_fails_closed()
    test_grouped_review_and_exact_tab_writeback()
    test_versioned_async_api_contract()
    print("OK: multi-tab workbook contracts hold without paid or network calls.")
