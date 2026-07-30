"""No-spend coverage for human-reviewed RTU negative references.

Run with ``python test_vlm_rtu_references.py``. This suite builds prompts and
provider payloads locally; it never calls Gemini, Grok, or any other network API.
"""
import io
from pathlib import Path

from PIL import Image

import vlm


def test_human_reviewed_negative_images_are_loaded():
    positive_images, negative_images = vlm._load_reference_images()

    assert len(positive_images) == 5
    assert len(negative_images) == 3
    positive_dimensions = []
    for image_bytes in positive_images:
        with Image.open(io.BytesIO(image_bytes)) as image:
            positive_dimensions.append(image.size)
            image.verify()
    assert positive_dimensions == [
        (309, 328),
        (361, 360),
        (292, 271),
        (954, 533),
        (540, 321),
    ]

    negative_dimensions = []
    for image_bytes in negative_images:
        assert len(image_bytes) > 10_000
        with Image.open(io.BytesIO(image_bytes)) as image:
            negative_dimensions.append(image.size)
            image.verify()
            assert image.format == "JPEG"
            assert not image.getexif()
    assert negative_dimensions == [(633, 146), (397, 171), (158, 183)]


def test_negative_guidance_does_not_depend_on_positive_references():
    prompt = vlm._build_address_prompt(
        {"address": "Anonymous test building", "lat": 1.0, "lon": 2.0},
        n_boxes=1,
        n_pos=0,
        n_neg=3,
    )

    assert "human-reviewed production results" in prompt
    assert "draws every candidate box green regardless" in prompt
    assert "not because the boxes are green" in prompt
    assert "RTUs, rooftop condensers, exhaust fans, or air handlers" in prompt
    assert 'Use "not_detected" only when no separate real cooling tower' in prompt


def test_every_vlm_path_gets_the_rtu_false_positive_guard():
    for base_prompt in (
        vlm._SYSTEM_PROMPT,
        vlm._ROOFTOP_SYSTEM_PROMPT,
        vlm._ADDRESS_SYSTEM_PROMPT,
    ):
        instruction = vlm._system_instruction(base_prompt)
        assert "RTU / PACKAGED ROOFTOP UNIT vs COOLING TOWER" in instruction
        assert "at least TWO" in instruction
        assert "One circular fan, several small fan grilles" in instruction
        assert "louvered" in instruction
        assert "are NOT mutually exclusive" in instruction
        assert "negative-reference match rejects only" in instruction
        assert "every numbered box when present" in instruction
        assert "even when RTUs are also present" in instruction
        assert "Every numbered YOLO candidate box is green regardless" in instruction
        assert "green is a neutral locator" in instruction


def test_whole_roof_negative_references_do_not_veto_a_real_tower():
    rooftop_prompt = vlm._build_rooftop_prompt(
        {"address": "Anonymous test building", "lat": 1.0, "lon": 2.0},
        n_pos=1,
        n_neg=1,
    )
    address_prompt = vlm._build_address_prompt(
        {"address": "Anonymous test building", "lat": 1.0, "lon": 2.0},
        n_boxes=2,
        n_pos=1,
        n_neg=1,
    )

    assert "not for the roof as a whole" in rooftop_prompt
    assert "continue scanning the rest of the target" in rooftop_prompt
    assert 'Use "no_cooling_tower" only when no separate real cooling tower' in rooftop_prompt
    assert "not for the address as a whole" in address_prompt
    assert "continue checking every other box and unboxed roof area" in address_prompt
    assert 'Use "not_detected" only when no separate real cooling tower' in address_prompt


def test_reference_payloads_label_rejected_yolo_boxes():
    gemini_parts = []
    vlm._append_gemini_reference_parts(gemini_parts, [b"positive"], [b"negative"])
    assert len(gemini_parts) == 4
    assert gemini_parts[0] == vlm._POSITIVE_REFERENCE_LABEL
    assert gemini_parts[2] == vlm._NEGATIVE_REFERENCE_LABEL
    assert "not because its neutral YOLO candidate box is green" in gemini_parts[2]
    assert (
        gemini_parts[1].media_resolution.level
        == vlm._REFERENCE_MEDIA_RESOLUTION
    )
    assert (
        gemini_parts[3].media_resolution.level
        == vlm._REFERENCE_MEDIA_RESOLUTION
    )

    openai_parts = []
    vlm._append_openai_reference_parts(openai_parts, [b"positive"], [b"negative"])
    assert len(openai_parts) == 4
    assert openai_parts[0] == {
        "type": "text",
        "text": vlm._POSITIVE_REFERENCE_LABEL,
    }
    assert openai_parts[2] == {
        "type": "text",
        "text": vlm._NEGATIVE_REFERENCE_LABEL,
    }
    assert openai_parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert openai_parts[3]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_prompt_descriptions_do_not_claim_a_conflicting_image_order():
    detection_prompt = vlm._build_prompt(
        {"address": "Anonymous test building", "lat": 1.0, "lon": 2.0},
        (1, 2, 3, 4),
        n_pos=1,
        n_neg=1,
    )
    rooftop_prompt = vlm._build_rooftop_prompt(
        {"address": "Anonymous test building", "lat": 1.0, "lon": 2.0},
        n_pos=1,
        n_neg=1,
    )
    assert "Along with Image A and Image B" in detection_prompt
    assert "Along with the satellite tile" in rooftop_prompt
    assert "After Image A and Image B" not in detection_prompt
    assert "After the satellite tile" not in rooftop_prompt


def test_reference_assets_are_anonymous():
    negative_dir = Path("reference_images") / "negative"
    names = sorted(path.name for path in negative_dir.glob("*.jpg"))
    assert names == [
        "01_human_reviewed_rtu_false_positive.jpg",
        "02_human_reviewed_rtu_false_positive.jpg",
        "03_human_reviewed_rtu_false_positive.jpg",
    ]
    readme = (Path("reference_images") / "README.md").read_text(encoding="utf-8")
    assert "customer addresses or other source-sheet contents" in readme


if __name__ == "__main__":
    test_human_reviewed_negative_images_are_loaded()
    test_negative_guidance_does_not_depend_on_positive_references()
    test_every_vlm_path_gets_the_rtu_false_positive_guard()
    test_whole_roof_negative_references_do_not_veto_a_real_tower()
    test_reference_payloads_label_rejected_yolo_boxes()
    test_prompt_descriptions_do_not_claim_a_conflicting_image_order()
    test_reference_assets_are_anonymous()
    print("OK: Gemini RTU negative references are loaded and used without paid calls.")
