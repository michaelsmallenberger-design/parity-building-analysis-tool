"""Cheap no-spend regression checks for the low-risk hardening pass.

Run with ``python test_hardening.py``. These tests deliberately stub analysis
and Sheets services; they never call mapping, imagery, model, or VLM APIs.
"""
import io
import os
import tempfile
import threading
from pathlib import Path

import pandas as pd
from flask import Flask
from werkzeug.datastructures import FileStorage

import api_analyze
import job_queue
import review_store
import sheets_writer
import storage_helpers


class _IsolatedState:
    def __init__(self, root):
        self.root = Path(root)

    def __enter__(self):
        self.old_storage_base = storage_helpers.STORAGE_BASE
        self.old_uploads_dir = storage_helpers.UPLOADS_DIR
        self.old_results_dir = storage_helpers.RESULTS_DIR
        self.old_db_path = job_queue.DB_PATH
        storage_helpers.STORAGE_BASE = self.root / "storage"
        storage_helpers.UPLOADS_DIR = storage_helpers.STORAGE_BASE / "uploads"
        storage_helpers.RESULTS_DIR = storage_helpers.STORAGE_BASE / "results"
        job_queue.DB_PATH = self.root / "jobs.db"
        storage_helpers.init_storage()
        job_queue.init_db()
        return self

    def __exit__(self, *_args):
        storage_helpers.STORAGE_BASE = self.old_storage_base
        storage_helpers.UPLOADS_DIR = self.old_uploads_dir
        storage_helpers.RESULTS_DIR = self.old_results_dir
        job_queue.DB_PATH = self.old_db_path


def test_storage_and_review_writes(root):
    with _IsolatedState(root):
        try:
            storage_helpers.get_file_path("../outside.json")
            raise AssertionError("storage traversal was accepted")
        except ValueError:
            pass

        review_store.save_batch("batch", "Test", [{"i": 1}, {"i": 2}])
        start = threading.Barrier(3)

        def submit(row_id):
            start.wait()
            review_store.record_decision("batch", row_id, {"hvac_systems": "Cooling Tower"})

        first = threading.Thread(target=submit, args=(1,))
        second = threading.Thread(target=submit, args=(2,))
        first.start()
        second.start()
        start.wait()
        first.join()
        second.join()

        stored = review_store.load_batch("batch")
        assert stored["entries"][0]["human"]["hvac_systems"] == "Cooling Tower"
        assert stored["entries"][1]["human"]["hvac_systems"] == "Cooling Tower"
        assert not list((storage_helpers.STORAGE_BASE / "reviews").glob("*.tmp"))


def test_atomic_api_quota(root):
    with _IsolatedState(root):
        original_limit = job_queue.MONTHLY_ADDRESS_LIMIT
        try:
            job_queue.MONTHLY_ADDRESS_LIMIT = 2
            assert job_queue.reserve_usage_limit(1) == (True, 0, "")
            assert job_queue.reserve_usage_limit(1) == (True, 1, "")
            allowed, used, message = job_queue.reserve_usage_limit(1)
            assert not allowed and used == 2 and "Monthly limit" in message
        finally:
            job_queue.MONTHLY_ADDRESS_LIMIT = original_limit


def test_api_uses_worker_excel_tab_and_reports_failures(root):
    with _IsolatedState(root):
        original_read_excel = api_analyze.pd.read_excel
        original_analyze = api_analyze._analyze_one
        original_finalize = api_analyze._finalize_batch
        original_report = api_analyze.build_audit_report
        original_reserve = api_analyze.reserve_usage_limit
        old_key = os.environ.get("ANALYZE_API_KEY")
        old_limit = os.environ.get("ANALYZE_MAX_BATCH_ROWS")
        try:
            def fake_read_excel(_path, sheet_name=None, engine=None):
                assert sheet_name is None
                return {
                    "Read me": pd.DataFrame({"Notes": ["instructions"]}),
                    "Properties": pd.DataFrame({"Property Address": ["1 Test St"]}),
                }

            api_analyze.pd.read_excel = fake_read_excel
            uploaded = FileStorage(io.BytesIO(b"not used by fake reader"), filename="multi-tab.xlsx")
            df, address_col, _, _ = api_analyze._read_uploaded_dataframe(uploaded)
            assert list(df[address_col]) == ["1 Test St"]

            api_analyze._analyze_one = lambda *_args, **_kwargs: {
                "address": "1 Test St", "error": "imagery unavailable", "notes": ""
            }
            api_analyze._finalize_batch = lambda *_args, **_kwargs: ("b-test", "/review/b-test", "")
            api_analyze.build_audit_report = lambda *_args, **_kwargs: "<html>report</html>"
            api_analyze.reserve_usage_limit = lambda count: (True, 0, "")
            os.environ["ANALYZE_API_KEY"] = "test-key"
            os.environ["ANALYZE_MAX_BATCH_ROWS"] = "2"

            app = Flask("hardening-test")
            app.register_blueprint(api_analyze.api)
            client = app.test_client()
            response = client.post(
                "/api/run",
                json={"addresses": ["1 Test St"]},
                headers={"X-API-Key": "test-key"},
            )
            assert response.status_code == 200
            assert response.headers["X-Success-Count"] == "0"
            assert response.headers["X-Failure-Count"] == "1"
            assert response.headers["X-Completion-Status"] == "completed_with_errors"

            file_response = client.post(
                "/api/run-file",
                data={"file": (io.BytesIO(b"Address\n1 Test St\n"), "addresses.csv")},
                headers={"X-API-Key": "test-key"},
            )
            assert file_response.status_code == 200, file_response.get_json()
            assert file_response.json["status"] == "completed_with_errors"
            assert file_response.json["success_count"] == 0
            assert file_response.json["failure_count"] == 1

            too_large = client.post(
                "/api/run",
                json={"addresses": ["1", "2", "3"]},
                headers={"X-API-Key": "test-key"},
            )
            assert too_large.status_code == 400
        finally:
            api_analyze.pd.read_excel = original_read_excel
            api_analyze._analyze_one = original_analyze
            api_analyze._finalize_batch = original_finalize
            api_analyze.build_audit_report = original_report
            api_analyze.reserve_usage_limit = original_reserve
            if old_key is None:
                os.environ.pop("ANALYZE_API_KEY", None)
            else:
                os.environ["ANALYZE_API_KEY"] = old_key
            if old_limit is None:
                os.environ.pop("ANALYZE_MAX_BATCH_ROWS", None)
            else:
                os.environ["ANALYZE_MAX_BATCH_ROWS"] = old_limit


def test_sheet_creation_requires_private_access():
    old_folder = os.environ.pop("SHEET_PARENT_FOLDER_ID", None)
    old_emails = os.environ.pop("SHEET_SHARE_WITH", None)
    original_services = sheets_writer._get_services
    try:
        sheets_writer._get_services = lambda: (_ for _ in ()).throw(
            AssertionError("must fail before making a Sheets API call"))
        try:
            sheets_writer.create_batch_sheet("Test", ["Row_ID"], [["1"]], [], [])
            raise AssertionError("public Sheet creation was accepted")
        except RuntimeError as e:
            assert "Refusing to create" in str(e)
    finally:
        sheets_writer._get_services = original_services
        if old_folder is not None:
            os.environ["SHEET_PARENT_FOLDER_ID"] = old_folder
        if old_emails is not None:
            os.environ["SHEET_SHARE_WITH"] = old_emails


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp_dir:
        test_storage_and_review_writes(temp_dir)
        test_atomic_api_quota(temp_dir)
        test_api_uses_worker_excel_tab_and_reports_failures(temp_dir)
    test_sheet_creation_requires_private_access()
    print("OK: low-risk hardening contracts hold without external API calls.")
