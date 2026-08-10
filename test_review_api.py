"""No-spend regression tests for review submission and write-back."""
import json
import tempfile
import threading
from pathlib import Path

import app_railway
import api_analyze
import pandas as pd
import review_store
import sheets_writer
import storage_helpers
import workbook_runs
from review_contract import DUAL_FIT_SCHEMA, SINGLE_FIT_SCHEMA


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
            review_schema=SINGLE_FIT_SCHEMA,
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


def test_unreviewed_misstamped_run_upgrades_from_source_tab_inventory(root):
    with IsolatedReviews(root):
        storage_helpers.write_json("reviews/misstamped.json", {
            "job_id": "misstamped",
            "schema_version": 2,
            "review_schema": SINGLE_FIT_SCHEMA,
            "tab_inventory": [{
                "tab": "DC",
                "status": "selected",
                "headers": [
                    "Optimizer Fit", "Periscope Fit", "HVAC systems",
                    "Notes", "Property Address",
                ],
            }],
            "sheet_bindings": [{
                "tab": "DC",
                "headers": [
                    "Optimizer Fit", "Periscope Fit", "HVAC systems",
                    "Notes", "Property Address",
                ],
                "colmap": {
                    "Fit": 5,
                    "HVAC Systems": 2,
                    "Notes": 3,
                },
            }],
            "entries": [{"row_id": "g10:r2", "verdict": "confirmed"}],
        })
        batch = review_store.load_batch("misstamped")
        assert batch["review_schema"] == DUAL_FIT_SCHEMA


def test_reviewed_single_fit_run_is_never_auto_migrated(root):
    with IsolatedReviews(root):
        storage_helpers.write_json("reviews/reviewed-single.json", {
            "job_id": "reviewed-single",
            "schema_version": 2,
            "review_schema": SINGLE_FIT_SCHEMA,
            "tab_inventory": [{
                "tab": "DC",
                "status": "selected",
                "headers": [
                    "Optimizer Fit", "Periscope Fit", "HVAC systems",
                    "Notes", "Property Address",
                ],
            }],
            "entries": [{
                "row_id": "g10:r2",
                "human": {"hvac_systems": "RTU", "fit": "Optimizer"},
            }],
        })
        batch = review_store.load_batch("reviewed-single")
        assert batch["review_schema"] == SINGLE_FIT_SCHEMA


def test_upload_mapping_preserves_dual_fields_and_does_not_guess_from_single_fit():
    headers, rows = api_analyze._table_from_df(pd.DataFrame({
        "Property Address": ["1 Main St"],
        "HVAC Systems": ["AHU"],
        "Fit": ["Periscope"],
    }))
    assert rows[0][headers.index("Optimizer Fit")] == ""
    assert rows[0][headers.index("Periscope Fit")] == ""

    headers, rows = api_analyze._table_from_df(pd.DataFrame({
        "Property Address": ["1 Main St"],
        "HVAC Systems": ["AHU"],
        "Optimizer Fit": ["Good"],
        "Periscope Fit": ["Bad"],
    }))
    assert rows[0][headers.index("Optimizer Fit")] == "Good"
    assert rows[0][headers.index("Periscope Fit")] == "Bad"


def test_existing_batch_can_load_stories_without_reanalysis(root):
    with IsolatedReviews(root):
        review_store.save_batch(
            "stories-refresh",
            "Synthetic review",
            [{
                "row_id": "sabc:g10:r2",
                "source_grid_id": 10,
                "source_tab": "Washington",
                "source_row": 2,
                "address": "1 Example Ave",
                "human": {
                    "hvac_systems": "RTU",
                    "optimizer_fit": "Good",
                    "periscope_fit": "Okay",
                    "note": "keep this decision",
                },
            }],
            sheet_bindings=[{
                "spreadsheet_id": "sheet",
                "grid_id": 10,
                "tab": "Washington",
                "headers": ["Address", "Stories"],
                "row_numbers": [2],
            }],
        )
        original_enabled = sheets_writer.enabled
        original_read = sheets_writer.read_review_contexts_for_bindings
        try:
            sheets_writer.enabled = lambda: True
            sheets_writer.read_review_contexts_for_bindings = lambda _bindings: {
                ("10", "Washington", 2): {"stories": "14"},
            }
            client = app_railway.app.test_client()
            page = client.get("/review/stories-refresh")
            assert page.status_code == 200
            assert "Load stories from Sheet" in page.get_data(as_text=True)
            response = client.post(
                "/review/stories-refresh/source-context/refresh"
            )
            assert response.status_code == 200
            assert response.json["matched"] == 1
            assert response.json["updated"] == 1
            batch = review_store.load_batch("stories-refresh")
            assert batch["entries"][0]["source_context"] == {
                "stories": "14",
            }
            assert batch["entries"][0]["human"]["note"] == (
                "keep this decision"
            )
        finally:
            sheets_writer.enabled = original_enabled
            sheets_writer.read_review_contexts_for_bindings = original_read


def _save_clean_alex_batch(job_id):
    review_store.save_batch(
        job_id,
        "Alex review",
        [
            {
                "row_id": "sabc:g10:r2",
                "source_grid_id": 10,
                "source_tab": "Maryland",
                "source_row": 2,
                "address": "1 Main St",
                "human": {
                    "hvac_systems": "Cooling Tower, AHU",
                    "optimizer_fit": "Okay",
                    "periscope_fit": "Good",
                    "note": "primary note",
                    "reviewed_at": "2026-08-10T10:00:00",
                },
            },
            {
                "row_id": "sabc:g10:r3",
                "source_grid_id": 10,
                "source_tab": "Maryland",
                "source_row": 3,
                "address": "2 Main St",
                "human": {
                    "hvac_systems": "RTU",
                    "optimizer_fit": "Good",
                    "periscope_fit": "Bad",
                    "note": "final",
                },
            },
        ],
        sheet_bindings=[{
            "spreadsheet_id": "sheet",
            "grid_id": 10,
            "tab": "Maryland",
            "headers": [
                "Property Address", "HVAC Systems", "Optimizer Fit",
                "Periscope Fit", "Notes",
            ],
            "colmap": {
                "HVAC Systems": 1,
                "Optimizer Fit": 2,
                "Periscope Fit": 3,
                "Notes": 4,
            },
        }],
        review_schema=DUAL_FIT_SCHEMA,
    )


def test_alex_revision_is_versioned_and_writes_only_allowed_cells(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-revise")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        old_enabled = sheets_writer.enabled
        old_write = getattr(sheets_writer, "write_secondary_decision_source", None)
        writes = []
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True
            sheets_writer.enabled = lambda: True
            sheets_writer.write_secondary_decision_source = (
                lambda binding, row, **values:
                writes.append((binding["tab"], row, values)) or True
            )
            client = app_railway.app.test_client()
            assert client.get("/review/alex-revise").status_code == 200
            response = client.post(
                "/api/review",
                json={
                    "job_id": "alex-revise",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "revise",
                    "expected_review_version": 1,
                    "optimizer_fit": "Customer",
                    "periscope_fit": "Bad",
                    "note": "Alex revised",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.json["sheet"] == "updated"
            assert response.json["primary_review_complete"] is True
            assert response.json["alex_review_remaining"] == 0
            assert writes == [(
                "Maryland",
                2,
                {
                    "optimizer_fit": "Customer",
                    "periscope_fit": "Bad",
                    "note": "Alex revised",
                },
            )]

            entry = review_store.load_batch("alex-revise")["entries"][0]
            assert entry["human"]["hvac_systems"] == "Cooling Tower, AHU"
            assert entry["human"]["optimizer_fit"] == "Customer"
            assert entry["human"]["periscope_fit"] == "Bad"
            assert entry["human"]["note"] == "Alex revised"
            assert entry["human"]["review_version"] == 2
            assert entry["human"]["review_stage"] == "secondary"
            assert len(entry["human_revisions"]) == 1
            assert entry["human_revisions"][0]["optimizer_fit"] == "Okay"
            assert entry["human_revisions"][0]["hvac_systems"] == "Cooling Tower, AHU"
            assert entry["secondary_review"]["action"] == "revise"
            assert entry["secondary_review"]["source_review_version"] == 1

            before = json.dumps(
                review_store.load_batch("alex-revise"), sort_keys=True
            )
            stale = client.post(
                "/api/review",
                json={
                    "job_id": "alex-revise",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "revise",
                    "expected_review_version": 1,
                    "optimizer_fit": "Bad",
                    "periscope_fit": "Bad",
                    "note": "stale",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert stale.status_code == 409
            assert json.dumps(
                review_store.load_batch("alex-revise"), sort_keys=True
            ) == before
            assert len(writes) == 1
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag
            sheets_writer.enabled = old_enabled
            if old_write is None:
                delattr(sheets_writer, "write_secondary_decision_source")
            else:
                sheets_writer.write_secondary_decision_source = old_write


def test_alex_confirmation_is_local_only_and_hvac_is_rejected(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-confirm")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        old_enabled = sheets_writer.enabled
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True

            def unexpected_sheet_check():
                raise AssertionError("confirmation must not inspect or write Sheets")

            sheets_writer.enabled = unexpected_sheet_check
            client = app_railway.app.test_client()
            assert client.get("/review/alex-confirm").status_code == 200
            missing_csrf = client.post(
                "/api/review",
                json={
                    "job_id": "alex-confirm",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "confirm_uncertain",
                    "expected_review_version": 1,
                },
            )
            assert missing_csrf.status_code == 403
            response = client.post(
                "/api/review",
                json={
                    "job_id": "alex-confirm",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "confirm_uncertain",
                    "expected_review_version": 1,
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.json["sheet"] == "none"
            assert response.json["alex_review_remaining"] == 0
            entry = review_store.load_batch("alex-confirm")["entries"][0]
            assert entry["human"]["optimizer_fit"] == "Okay"
            assert "human_revisions" not in entry
            assert entry["secondary_review"]["action"] == "confirm_uncertain"

            rejected = client.post(
                "/api/review",
                json={
                    "job_id": "alex-confirm",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "revise",
                    "expected_review_version": 1,
                    "hvac_systems": "None",
                    "optimizer_fit": "Bad",
                    "periscope_fit": "Bad",
                    "note": "must fail",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert rejected.status_code in {400, 409}
            assert review_store.load_batch("alex-confirm")["entries"][0][
                "human"
            ]["hvac_systems"] == "Cooling Tower, AHU"
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag
            sheets_writer.enabled = old_enabled


def test_alex_sheet_failure_retry_does_not_duplicate_history(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-retry")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        old_enabled = sheets_writer.enabled
        old_write = getattr(sheets_writer, "write_secondary_decision_source", None)
        attempts = []
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True
            sheets_writer.enabled = lambda: True

            def flaky_write(_binding, _row, **values):
                attempts.append(values)
                if len(attempts) == 1:
                    raise RuntimeError("temporary Sheet outage")
                return True

            sheets_writer.write_secondary_decision_source = flaky_write
            client = app_railway.app.test_client()
            assert client.get("/review/alex-retry").status_code == 200
            payload = {
                "job_id": "alex-retry",
                "row_id": "sabc:g10:r2",
                "review_stage": "secondary",
                "secondary_action": "revise",
                "expected_review_version": 1,
                "optimizer_fit": "Good",
                "periscope_fit": "Not Sure",
                "note": "retain uncertainty",
            }
            failed = client.post(
                "/api/review",
                json=payload,
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert failed.status_code == 200
            assert failed.json["local_saved"] is True
            assert failed.json["sheet"] == "error"
            entry = review_store.load_batch("alex-retry")["entries"][0]
            assert entry["human"]["review_version"] == 2
            assert len(entry["human_revisions"]) == 1
            assert entry["writeback"]["status"] == "error"
            completed_at = entry["secondary_review"]["completed_at"]
            reviewed_at = entry["human"]["reviewed_at"]

            changed = dict(payload)
            changed["expected_review_version"] = 2
            changed["note"] = "changed retry must fail"
            rejected = client.post(
                "/api/review",
                json=changed,
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert rejected.status_code == 409
            assert len(attempts) == 1

            payload["expected_review_version"] = 2
            retried = client.post(
                "/api/review",
                json=payload,
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert retried.status_code == 200
            assert retried.json["sheet"] == "updated"
            entry = review_store.load_batch("alex-retry")["entries"][0]
            assert entry["human"]["review_version"] == 2
            assert len(entry["human_revisions"]) == 1
            assert entry["writeback"]["status"] == "updated"
            assert entry["secondary_review"]["completed_at"] == completed_at
            assert entry["human"]["reviewed_at"] == reviewed_at
            assert len(attempts) == 2
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag
            sheets_writer.enabled = old_enabled
            if old_write is None:
                delattr(sheets_writer, "write_secondary_decision_source")
            else:
                sheets_writer.write_secondary_decision_source = old_write


def test_completed_primary_cannot_bypass_secondary_when_enabled(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-primary-guard")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        old_enabled = sheets_writer.enabled
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True

            def unexpected_sheet_check():
                raise AssertionError("conflicted primary request reached Sheets")

            sheets_writer.enabled = unexpected_sheet_check
            client = app_railway.app.test_client()
            assert client.get("/review/alex-primary-guard").status_code == 200
            before = json.dumps(
                review_store.load_batch("alex-primary-guard"), sort_keys=True
            )
            response = client.post(
                "/api/review",
                json={
                    "job_id": "alex-primary-guard",
                    "row_id": "sabc:g10:r2",
                    "hvac_systems": "None",
                    "optimizer_fit": "Bad",
                    "periscope_fit": "Bad",
                    "note": "bypass",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 409
            assert json.dumps(
                review_store.load_batch("alex-primary-guard"), sort_keys=True
            ) == before
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag
            sheets_writer.enabled = old_enabled


def test_disabled_flag_preserves_primary_behavior_and_blocks_secondary(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-disabled")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = False
            client = app_railway.app.test_client()
            page = client.get("/review/alex-disabled").get_data(as_text=True)
            assert '<span id="alex-count">' not in page
            assert "Review qualification" not in page

            secondary = client.post(
                "/api/review",
                json={
                    "job_id": "alex-disabled",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "confirm_uncertain",
                    "expected_review_version": 1,
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert secondary.status_code == 409

            primary = client.post(
                "/api/review",
                json={
                    "job_id": "alex-disabled",
                    "row_id": "sabc:g10:r2",
                    "hvac_systems": "None",
                    "optimizer_fit": "Bad",
                    "periscope_fit": "Bad",
                    "note": "legacy primary behavior",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert primary.status_code == 200
            human = review_store.load_batch("alex-disabled")["entries"][0]["human"]
            assert human["hvac_systems"] == "None"
            assert "review_version" not in human
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag


def test_last_primary_response_exposes_alex_queue(root):
    with IsolatedReviews(root):
        review_store.save_batch(
            "alex-last-primary",
            "Last primary",
            [
                {
                    "i": 1,
                    "address": "1 Main St",
                    "human": {
                        "hvac_systems": "AHU",
                        "optimizer_fit": "Okay",
                        "periscope_fit": "Good",
                    },
                },
                {"i": 2, "address": "2 Main St"},
            ],
            review_schema=DUAL_FIT_SCHEMA,
        )
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True
            client = app_railway.app.test_client()
            assert client.get("/review/alex-last-primary").status_code == 200
            response = client.post(
                "/api/review",
                json={
                    "job_id": "alex-last-primary",
                    "row_id": 2,
                    "hvac_systems": "RTU",
                    "optimizer_fit": "Good",
                    "periscope_fit": "Bad",
                    "note": "done",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 200
            assert response.json["primary_review_complete"] is True
            assert response.json["alex_review_remaining"] == 1
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag


def test_secondary_writer_targets_only_fit_and_notes_cells():
    calls = []

    class Request:
        def execute(self):
            return {}

    class Values:
        def batchUpdate(self, **kwargs):
            calls.append(kwargs)
            return Request()

    class Spreadsheets:
        def values(self):
            return Values()

    class Sheets:
        def spreadsheets(self):
            return Spreadsheets()

    original_services = sheets_writer._get_services
    try:
        sheets_writer._get_services = lambda: (Sheets(), object())
        ok = sheets_writer.write_secondary_decision_source(
            {
                "spreadsheet_id": "sheet",
                "tab": "Maryland Review",
                "colmap": {
                    "HVAC Systems": 0,
                    "Optimizer Fit": 2,
                    "Periscope Fit": 4,
                    "Notes": 5,
                },
            },
            7,
            optimizer_fit="Good",
            periscope_fit="Not Sure",
            note="",
        )
        assert ok is True
        assert calls[0]["body"]["valueInputOption"] == "RAW"
        data = calls[0]["body"]["data"]
        assert [item["range"] for item in data] == [
            "'Maryland Review'!C7",
            "'Maryland Review'!E7",
            "'Maryland Review'!F7",
        ]
        assert [item["values"] for item in data] == [
            [["Good"]], [["Not Sure"]], [[""]],
        ]
        assert all("A7" not in item["range"] for item in data)
    finally:
        sheets_writer._get_services = original_services


def test_secondary_write_fails_closed_when_binding_is_incomplete(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-incomplete-binding")
        batch = review_store.load_batch("alex-incomplete-binding")
        batch["sheet_bindings"][0]["colmap"].pop("Notes")
        review_store.save_raw("alex-incomplete-binding", batch)
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        old_enabled = sheets_writer.enabled
        old_ensure = sheets_writer.ensure_review_columns
        old_write = sheets_writer.write_secondary_decision_source
        calls = []
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True
            sheets_writer.enabled = lambda: True

            def forbidden_ensure(*_args, **_kwargs):
                calls.append("ensure")
                raise AssertionError("secondary review must not alter Sheet schema")

            def forbidden_write(*_args, **_kwargs):
                calls.append("write")
                raise AssertionError("incomplete binding must not write partial cells")

            sheets_writer.ensure_review_columns = forbidden_ensure
            sheets_writer.write_secondary_decision_source = forbidden_write
            client = app_railway.app.test_client()
            assert client.get("/review/alex-incomplete-binding").status_code == 200
            response = client.post(
                "/api/review",
                json={
                    "job_id": "alex-incomplete-binding",
                    "row_id": "sabc:g10:r2",
                    "review_stage": "secondary",
                    "secondary_action": "revise",
                    "expected_review_version": 1,
                    "optimizer_fit": "Good",
                    "periscope_fit": "Bad",
                    "note": "local only after binding failure",
                },
                headers={"X-CSRF-Token": _csrf(client)},
            )
            assert response.status_code == 200
            assert response.json["sheet"] == "error"
            assert response.json["local_saved"] is True
            assert "not bound" in response.json["sheet_error"]
            assert calls == []
            entry = review_store.load_batch("alex-incomplete-binding")["entries"][0]
            assert entry["human"]["review_version"] == 2
            assert entry["writeback"]["status"] == "error"
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag
            sheets_writer.enabled = old_enabled
            sheets_writer.ensure_review_columns = old_ensure
            sheets_writer.write_secondary_decision_source = old_write


def test_history_cap_preserves_original_and_latest_nineteen():
    revisions = [{"review_version": index} for index in range(1, 21)]
    bounded = review_store._bounded_revisions(
        revisions, {"review_version": 21}
    )
    assert len(bounded) == 20
    assert bounded[0]["review_version"] == 1
    assert [item["review_version"] for item in bounded[1:]] == list(range(3, 22))


def test_alex_review_get_is_read_only(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-read-only")
        old_flag = app_railway.app.config.get("ALEX_REVIEW_QUEUE_ENABLED")
        review_path = storage_helpers.get_file_path("reviews/alex-read-only.json")
        before = review_path.read_bytes()
        before_order = [
            entry["row_id"]
            for entry in review_store.load_batch("alex-read-only")["entries"]
        ]
        try:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = True
            client = app_railway.app.test_client()
            response = client.get("/review/alex-read-only?page=1")
            assert response.status_code == 200
            assert "Needs Alex Review" in response.get_data(as_text=True)
            assert review_path.read_bytes() == before
            assert [
                entry["row_id"]
                for entry in review_store.load_batch("alex-read-only")["entries"]
            ] == before_order
        finally:
            app_railway.app.config["ALEX_REVIEW_QUEUE_ENABLED"] = old_flag


def test_secondary_compare_and_set_allows_only_one_concurrent_revision(root):
    with IsolatedReviews(root):
        _save_clean_alex_batch("alex-cas")
        barrier = threading.Barrier(2)
        outcomes = []

        def revise(note):
            barrier.wait()
            try:
                review_store.record_secondary_review(
                    "alex-cas",
                    "sabc:g10:r2",
                    action="revise",
                    expected_review_version=1,
                    decision={
                        "optimizer_fit": "Good",
                        "periscope_fit": "Bad",
                        "note": note,
                    },
                )
                outcomes.append("saved")
            except review_store.ReviewConflict:
                outcomes.append("conflict")

        threads = [
            threading.Thread(target=revise, args=(f"thread {index}",))
            for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not any(thread.is_alive() for thread in threads)
        assert sorted(outcomes) == ["conflict", "saved"]
        entry = review_store.load_batch("alex-cas")["entries"][0]
        assert entry["human"]["review_version"] == 2
        assert len(entry["human_revisions"]) == 1


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        test_single_fit_submission_returns_200_and_updates_exact_source(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_true_historical_dual_submission_remains_valid(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_pre_fix_multitab_batch_infers_single_fit_without_guessing(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_unreviewed_misstamped_run_upgrades_from_source_tab_inventory(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_reviewed_single_fit_run_is_never_auto_migrated(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_existing_batch_can_load_stories_without_reanalysis(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_alex_revision_is_versioned_and_writes_only_allowed_cells(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_alex_confirmation_is_local_only_and_hvac_is_rejected(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_alex_sheet_failure_retry_does_not_duplicate_history(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_completed_primary_cannot_bypass_secondary_when_enabled(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_disabled_flag_preserves_primary_behavior_and_blocks_secondary(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_last_primary_response_exposes_alex_queue(temp_dir)
    test_secondary_writer_targets_only_fit_and_notes_cells()
    with tempfile.TemporaryDirectory() as temp_dir:
        test_secondary_write_fails_closed_when_binding_is_incomplete(temp_dir)
    test_history_cap_preserves_original_and_latest_nineteen()
    with tempfile.TemporaryDirectory() as temp_dir:
        test_alex_review_get_is_read_only(temp_dir)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_secondary_compare_and_set_allows_only_one_concurrent_revision(temp_dir)
    test_upload_mapping_preserves_dual_fields_and_does_not_guess_from_single_fit()
    print("OK: review API dual-product mapping and single-Fit compatibility hold.")
