import os
from unittest.mock import patch

import failure_diagnostics


def test_sanitizer_removes_customer_and_secret_material():
    raw = (
        "Failed at 123 Main Street using API_KEY=super-secret "
        "https://example.com/callback?token=private "
        "and abcdefghijklmnopqrstuvwxyz1234567890"
    )
    safe = failure_diagnostics.sanitize_failure_text(raw)
    assert "123 Main" not in safe
    assert "super-secret" not in safe
    assert "token=private" not in safe
    assert "abcdefghijklmnopqrstuvwxyz1234567890" not in safe
    assert "[REDACTED ADDRESS]" in safe


def test_deterministic_packet_contains_no_rows_or_addresses():
    with patch.dict(
        os.environ,
        {"FAILURE_DIAGNOSTICS_LLM_ENABLED": "false"},
        clear=False,
    ):
        packet = failure_diagnostics.build_failure_diagnostic(
            run_id="w-safe-test",
            stage="workbook_processing",
            error=RuntimeError("Chunk checkpoint failed at 500 Park Avenue"),
            progress=3,
            total=8,
            rows=[
                {
                    "address": "500 Park Avenue",
                    "state": "complete",
                    "error": "private row error",
                },
                {"address": "1 Broadway", "state": "analyzing"},
            ],
        )
    serialized = str(packet)
    assert "500 Park" not in serialized
    assert "1 Broadway" not in serialized
    assert "private row error" not in serialized
    assert packet["row_state_counts"] == {"analyzing": 1, "complete": 1}
    assert packet["diagnostic_model"] == "deterministic"
    assert "workbook_runs.py" in packet["code_pointers"]
    assert (
        "Investigate this Parity analyzer run failure."
        in packet["claude_code_context"]
    )


def test_grok_is_first_when_gemini_is_implicated():
    env = {
        "FAILURE_DIAGNOSTICS_LLM_ENABLED": "true",
        "GEMINI_API_KEY": "test-gemini",
        "XAI_API_KEY": "test-grok",
    }
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(
            failure_diagnostics,
            "_grok_summary",
            return_value=("Grok safe summary", "grok-test"),
        ) as grok,
        patch.object(failure_diagnostics, "_gemini_summary") as gemini,
    ):
        packet = failure_diagnostics.build_failure_diagnostic(
            run_id="w-provider-test",
            stage="address_processing",
            error=RuntimeError("Gemini request timed out"),
            total=4,
        )
    grok.assert_called_once()
    gemini.assert_not_called()
    model_packet = grok.call_args.args[0]
    assert "error_message" not in model_packet
    assert "run_id" not in model_packet
    assert model_packet["failure_signals"] == ["timeout", "provider_gemini"]
    assert packet["diagnostic_model"] == "grok:grok-test"
    assert packet["diagnostic_summary"] == "Grok safe summary"


if __name__ == "__main__":
    test_sanitizer_removes_customer_and_secret_material()
    test_deterministic_packet_contains_no_rows_or_addresses()
    test_grok_is_first_when_gemini_is_implicated()
    print("failure diagnostic tests passed")
