"""Headless CSV export for process_address_list web_results."""
from typing import Optional


def _web_entry_to_consensus(web_entry: dict) -> Optional[dict]:
    if web_entry.get('confidence_score') is None and not web_entry.get('gemini_verdict'):
        return None
    return {
        'confidence': web_entry.get('confidence_score'),
        'reasoning': web_entry.get('reasoning', ''),
        'agreement': web_entry.get('agreement', False),
        'gemini': {
            'verdict': web_entry.get('gemini_verdict', ''),
            'confidence': web_entry.get('gemini_confidence'),
        },
        'grok': {
            'verdict': web_entry.get('grok_verdict', ''),
            'confidence': web_entry.get('grok_confidence'),
        },
    }


def web_results_to_csv_rows(web_results: list[dict]) -> list[dict]:
    """Derive the 15-column CSV schema from process_address_list web_results.

    Used by headless/test runners. Production Flask UI doesn't need this
    (copy-paste from HTML table).
    """
    from tasks_local import _build_csv_row

    rows = []
    for entry in web_results:
        rows.append(_build_csv_row(
            full_address=entry.get('address', ''),
            verdict=entry.get('verdict', ''),
            consensus_dict=_web_entry_to_consensus(entry),
            detection_count=entry.get('detection_count', 0),
            construction=entry.get('construction', False),
            notes=entry.get('notes', ''),
            original_url=entry.get('original_image_url'),
            result_url=entry.get('result_image_url'),
        ))
    return rows
