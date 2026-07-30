"""No-spend regression checks for the compact, non-overlapping status poller."""
from pathlib import Path

import app_railway


def _status_response(status, result, query=""):
    original_status = app_railway.get_job_status
    original_result = app_railway.read_result
    try:
        app_railway.get_job_status = lambda _job_id: status
        app_railway.read_result = lambda _job_id: result
        query_parts = ["compact=1"]
        if query:
            query_parts.append(query.lstrip("?"))
        path = f"/status/w-test?{'&'.join(query_parts)}"
        with app_railway.app.test_request_context(path):
            response, response_status = app_railway.job_status_route("w-test")
            assert response_status == 200
            return response.get_json()
    finally:
        app_railway.get_job_status = original_status
        app_railway.read_result = original_result


def test_status_payload_is_compact_and_conditionally_omits_rows():
    status = {
        "status": "finished",
        "progress": 1,
        "total": 1,
        "cancel_requested": False,
        "message": "Ready for review",
    }
    result = {
        "live_rows": [{
            "row_id": "17:2",
            "address": "1 Test St",
            "tab": "Washington",
            "source_row": 2,
            "state": "complete",
            "message": "Machine analysis finished",
            "error": "",
            "model_result": "must not be returned",
            "images": ["must-not-be-returned.jpg"],
        }],
        "review_url": "/review/w-test",
        "sheet_url": "https://docs.google.com/spreadsheets/d/test/edit",
        "web_results": [{"large": "analysis payload must not be returned"}],
        "private_provider_payload": {"large": "must not be returned"},
    }

    first = _status_response(status, result)
    assert first["rows_changed"] is True
    assert first["rows"][0] == {
        "row_id": "17:2",
        "address": "1 Test St",
        "tab": "Washington",
        "source_row": 2,
        "state": "complete",
        "message": "Machine analysis finished",
        "error": "",
    }
    assert first["result"] == {
        "review_url": "/review/w-test",
        "sheet_url": "https://docs.google.com/spreadsheets/d/test/edit",
    }
    assert first["progress"] == 1
    assert first["total"] == 1

    unchanged = _status_response(
        status, result, f"?rows_version={first['rows_version']}",
    )
    assert unchanged["rows_changed"] is False
    assert "rows" not in unchanged
    assert unchanged["progress"] == 1
    assert unchanged["total"] == 1

    changed_result = dict(result)
    changed_result["live_rows"] = [
        {**result["live_rows"][0], "state": "attention", "error": "No imagery"},
    ]
    changed = _status_response(
        status, changed_result, f"?rows_version={first['rows_version']}",
    )
    assert changed["rows_changed"] is True
    assert changed["rows_version"] != first["rows_version"]
    assert changed["rows"][0]["error"] == "No imagery"


def test_failed_status_only_returns_diagnostic_fields_used_by_page():
    status = {
        "status": "failed",
        "progress": 0,
        "total": 1,
        "cancel_requested": False,
        "message": "Run failed",
    }
    result = {
        "address_states": [{
            "index": 0,
            "address": "2 Test St",
            "state": "failed",
            "message": "Imagery failed",
            "error": "Imagery failed",
            "provider_trace": "must not be returned",
        }],
        "diagnostic": {
            "diagnostic_summary": "Imagery provider failed.",
            "claude_code_context": "Sanitized repair context.",
            "raw_stack": "must not be returned",
        },
    }

    payload = _status_response(status, result)
    assert payload["rows"][0]["row_id"] == "0"
    assert payload["result"]["diagnostic"] == {
        "diagnostic_summary": "Imagery provider failed.",
        "claude_code_context": "Sanitized repair context.",
    }
    assert "raw_stack" not in str(payload)
    assert "provider_trace" not in str(payload)


def test_legacy_status_contract_still_returns_the_full_result():
    status = {
        "status": "finished",
        "progress": 1,
        "total": 1,
        "cancel_requested": False,
    }
    result = {
        "web_results": [{"address": "1 Test St", "verdict": "confirmed"}],
        "html_url": "/files/results/report.html",
        "review_url": "/review/w-test",
    }
    original_status = app_railway.get_job_status
    original_result = app_railway.read_result
    try:
        app_railway.get_job_status = lambda _job_id: status
        app_railway.read_result = lambda _job_id: result
        with app_railway.app.test_request_context("/status/w-test"):
            response, response_status = app_railway.job_status_route("w-test")
        assert response_status == 200
        payload = response.get_json()
        assert payload["result"] == result
        assert payload["rows"] == []
        assert "rows_version" not in payload
    finally:
        app_railway.get_job_status = original_status
        app_railway.read_result = original_result


def test_results_page_uses_a_self_scheduling_visibility_aware_poller():
    source = (
        Path(__file__).parent / "templates" / "results.html"
    ).read_text(encoding="utf-8")

    assert "setInterval(" not in source
    assert "requestInFlight" in source
    assert "setTimeout(" in source
    assert "visibilitychange" in source
    assert "HIDDEN_POLL_DELAY_MS = 15000" in source
    assert "STATUS_REQUEST_TIMEOUT_MS = 15000" in source
    assert "new AbortController()" in source
    assert "{ signal: controller.signal }" in source
    assert "clearTimeout(requestTimeout)" in source
    assert "searchParams.set('compact', '1')" in source
    assert "rows_version" in source
    assert "data.rows_changed !== false" in source
    assert "currentRowsSignature" in source
    assert "finishedRowsSignature" in source


def run():
    test_status_payload_is_compact_and_conditionally_omits_rows()
    test_failed_status_only_returns_diagnostic_fields_used_by_page()
    test_legacy_status_contract_still_returns_the_full_result()
    test_results_page_uses_a_self_scheduling_visibility_aware_poller()
    print("status polling tests passed")


if __name__ == "__main__":
    run()
