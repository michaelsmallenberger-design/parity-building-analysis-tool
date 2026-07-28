"""No-spend regression coverage for safe intake and Gemini-first fallback.

Run with ``python test_intake_vlm_fallback.py``. Provider functions are replaced
with local fakes; this suite never makes a network or paid-model request.
"""
import os
import tempfile

import pandas as pd
from PIL import Image

import intake_resolver
from report_audit import build_audit_report
import sheets_writer
import vlm


def _mapping(confidence=0.96):
    return {
        "tab": "Imported buildings",
        "address_column": "Street",
        "city_column": "Town",
        "state_column": "Region",
        "zip_column": "Postal",
        "confidence": confidence,
    }


def _messy_tabs():
    return [{
        "tab": "Imported buildings",
        "headers": ["Street", "Town", "Region", "Postal", "Building"],
        "rows": [
            ["1 Test St", "Boston", "MA", "02101", "Alpha"],
            ["2 Test St", "Boston", "MA", "02102", "Beta"],
        ],
    }]


def _valid_result(verdict="confirmed", confidence=0.9, reasoning="Clear rooftop equipment."):
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": reasoning,
        "construction": False,
        "is_house": False,
        "image_unusable": False,
        "frame_inadequate": False,
    }


def test_intake_resolution():
    old_suggestion = intake_resolver._grok_suggestion
    old_key = os.environ.get("XAI_API_KEY")
    try:
        intake_resolver._resolution_cache.clear()
        # A recognized header must never reach Grok.
        intake_resolver._grok_suggestion = lambda _tabs: (_ for _ in ()).throw(
            AssertionError("Grok called for deterministic headers"))
        clean = [{"tab": "Clean", "headers": ["Property Address", "City"],
                  "rows": [["1 Test St", "Boston"]]}]
        result = intake_resolver.resolve_schema(clean, ["Address", "Property Address"])
        assert result["status"] == "deterministic"
        assert result["mapping"]["address_column"] == "Property Address"

        # A valid high-confidence fallback selects split fields without changing
        # the source table and supports all populated sample rows.
        intake_resolver._resolution_cache.clear()
        intake_resolver._grok_suggestion = lambda _tabs: (_mapping(), "")
        result = intake_resolver.resolve_schema(_messy_tabs(), ["Address"])
        assert result["status"] == "suggested" and result["auto_approved"]
        assert intake_resolver.compose_addresses(
            _messy_tabs()[0]["headers"], _messy_tabs()[0]["rows"], result["mapping"]
        ) == ["1 Test St, Boston, MA, 02101", "2 Test St, Boston, MA, 02102"]

        # Low confidence and malformed selections fail closed. The second call
        # also verifies the non-raw fingerprint cache avoids a repeat provider call.
        intake_resolver._resolution_cache.clear()
        calls = []
        intake_resolver._grok_suggestion = lambda _tabs: (calls.append(1) or _mapping(0.5), "")
        low = intake_resolver.resolve_schema(_messy_tabs(), ["Address"])
        assert low["status"] == "suggested" and not low["auto_approved"]
        again = intake_resolver.resolve_schema(_messy_tabs(), ["Address"])
        assert again["status"] == "suggested" and len(calls) == 1

        intake_resolver._resolution_cache.clear()
        intake_resolver._grok_suggestion = lambda _tabs: ({**_mapping(), "address_column": "Invented"}, "")
        assert intake_resolver.resolve_schema(_messy_tabs(), ["Address"])["status"] == "unresolved"
    finally:
        intake_resolver._grok_suggestion = old_suggestion
        intake_resolver._resolution_cache.clear()
        if old_key is None:
            os.environ.pop("XAI_API_KEY", None)
        else:
            os.environ["XAI_API_KEY"] = old_key


def test_gemini_first_fallback():
    originals = (vlm._verify_gemini_address, vlm._verify_grok_address)
    old_gemini = os.environ.get("GEMINI_API_KEY")
    old_xai = os.environ.get("XAI_API_KEY")
    try:
        os.environ["GEMINI_API_KEY"] = "mock-key"
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "test.png")
            Image.new("RGB", (16, 16), "white").save(image_path)
            context = {"address": "1 Test St", "lat": 1.0, "lon": 1.0}

            # Valid low-confidence / needs-review is still a Gemini result and
            # must never invoke Grok.
            grok_calls = []
            vlm._verify_gemini_address = lambda *_args, **_kwargs: _valid_result(
                "needs_review", 0.2, "The roof is too ambiguous to decide.")
            vlm._verify_grok_address = lambda *_args, **_kwargs: grok_calls.append(1)
            normal = vlm.verify_address(image_path, context)
            assert normal["model_path"] == "gemini_only" and normal["grok"] == {}
            assert not grok_calls

            # A technical Gemini failure permits exactly one emergency Grok call.
            vlm._verify_gemini_address = lambda *_args, **_kwargs: _valid_result(
                "needs_review", 0.0, "Network timeout after 4 attempts.")
            def grok_once(*_args, **kwargs):
                grok_calls.append(kwargs.get("max_attempts"))
                return _valid_result("likely", 0.6, "Fallback found louvers.")
            vlm._verify_grok_address = grok_once
            os.environ["XAI_API_KEY"] = "mock-key"
            fallback = vlm.verify_address(image_path, context)
            assert fallback["model_path"] == "grok_emergency_fallback"
            assert fallback["grok_fallback_used"] and grok_calls == [1]

            # Missing XAI is not a normal-operation configuration failure.
            os.environ.pop("XAI_API_KEY", None)
            unavailable = vlm.verify_address(image_path, context)
            assert unavailable["verdict"] == "needs_review"
            assert unavailable["model_path"] == "grok_fallback_unavailable"
            assert unavailable["grok"] == {}
    finally:
        vlm._verify_gemini_address, vlm._verify_grok_address = originals
        if old_gemini is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_gemini
        if old_xai is None:
            os.environ.pop("XAI_API_KEY", None)
        else:
            os.environ["XAI_API_KEY"] = old_xai


def test_multi_tab_file_resolution():
    frames = [
        ("Read me", pd.DataFrame({"Instructions": ["ignore"]})),
        ("Imported buildings", pd.DataFrame({"Property Address": ["1 Test St"]})),
    ]
    df, address_col, _city, _zip = __import__("api_analyze")._resolve_dataframe_tabs(frames)
    assert list(df[address_col]) == ["1 Test St"]


def test_existing_sheet_dropdowns_are_untouched():
    """Live-sheet safety: no existing customer validation range is changed."""
    class FakeSheets:
        def __init__(self):
            self.requests = []
        def spreadsheets(self):
            return self
        def values(self):
            return self
        def update(self, **kwargs):
            self.requests.append(("values.update", kwargs))
            return self
        def batchUpdate(self, **kwargs):
            self.requests.append(("batchUpdate", kwargs))
            return self
        def execute(self):
            return {}

    fake = FakeSheets()
    original_services = sheets_writer._get_services
    try:
        sheets_writer._get_services = lambda: (fake, None)
        # Sales Stage represents an unrelated dropdown supplied by the customer.
        # HVAC Systems represents an existing Parity-facing multi-select. Both
        # validation rules must survive while missing review columns are appended.
        headers = ["Property Address", "Sales Stage", "HVAC Systems"]
        binding = {"spreadsheet_id": "test", "grid_id": 1, "tab": "Buildings",
                   "row_numbers": [2, 3]}
        sheets_writer.ensure_review_columns(
            binding, headers, ["Cooling Tower"], ["Good", "Bad", "Not Sure"])
        batch_requests = [item for kind, request in fake.requests
                          if kind == "batchUpdate"
                          for item in request["body"]["requests"]]
        set_validation = [item["setDataValidation"] for item in batch_requests
                          if "setDataValidation" in item]
        assert set_validation, "new fit columns should receive Parity dropdowns"
        assert all(rule["range"]["startColumnIndex"] >= len(headers)
                   for rule in set_validation), (
            "Parity must never replace a dropdown on an existing customer column"
        )
        copy_pastes = [item["copyPaste"] for item in batch_requests if "copyPaste" in item]
        assert all(item["destination"]["startColumnIndex"] >= len(headers)
                   for item in copy_pastes), (
            "formatting for appended columns must not target existing customer columns"
        )
        value_updates = [request for kind, request in fake.requests if kind == "values.update"]
        assert all(request["range"].split("!")[1][0] >= sheets_writer._col_letter(len(headers))
                   for request in value_updates), (
            "header/link writes must stay to the right of the incoming Sheet"
        )
        assert binding["colmap"]["HVAC Systems"] == 2
    finally:
        sheets_writer._get_services = original_services


def test_grok_is_hidden_when_no_fallback_ran():
    html = build_audit_report([{
        "address": "1 Test St", "verdict": "confirmed", "confidence_score": 0.9,
        "gemini_verdict": "confirmed", "gemini_confidence": 0.9,
        "gemini_reasoning": "Visible fan stack.",
        "grok_verdict": "", "grok_reasoning": "", "notes": "",
    }])
    assert "Grok &mdash;" not in html


if __name__ == "__main__":
    test_intake_resolution()
    test_gemini_first_fallback()
    test_multi_tab_file_resolution()
    test_existing_sheet_dropdowns_are_untouched()
    test_grok_is_hidden_when_no_fallback_ran()
    print("OK: intake resolution and Gemini-first fallback are covered with local mocks only.")
