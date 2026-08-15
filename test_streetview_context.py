"""No-spend contracts for reviewer-only Street View context."""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

from PIL import Image

import tasks_local
import utils


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _jpeg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=(30, 60, 90)).save(output, format="JPEG")
    return output.getvalue()


def test_address_targeted_streetview_uses_selected_panorama():
    calls = []
    original_get = utils.requests.get
    original_key = utils.GOOGLE_MAPS_API_KEY
    try:
        utils.GOOGLE_MAPS_API_KEY = "test-key"

        def fake_get(url, params=None, **_kwargs):
            calls.append((url, dict(params or {})))
            if url.endswith("/metadata"):
                return FakeResponse(payload={
                    "status": "OK",
                    "pano_id": "front-pano",
                    "location": {"lat": 38.1, "lng": -76.2},
                })
            return FakeResponse(content=_jpeg_bytes())

        utils.requests.get = fake_get
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "front.jpg"
            metadata = utils.get_streetview_image_google(
                38.2,
                -76.3,
                str(output),
                location="100 Example Avenue, Test City, MD 20000",
                fov=90,
                pitch=5,
            )
            assert output.exists()

        assert metadata["pano_id"] == "front-pano"
        assert calls[0][1]["location"] == (
            "100 Example Avenue, Test City, MD 20000"
        )
        assert calls[1][1]["pano"] == "front-pano"
        assert "location" not in calls[1][1]
        assert calls[1][1]["fov"] == 90
        assert calls[1][1]["pitch"] == 5
    finally:
        utils.requests.get = original_get
        utils.GOOGLE_MAPS_API_KEY = original_key


def test_capture_adds_at_most_two_views_and_rotates_duplicate_panorama():
    metadata_calls = []
    image_calls = []
    uploads = []
    original_metadata = tasks_local.get_streetview_metadata_google
    original_image = tasks_local.get_streetview_image_google
    original_enabled = tasks_local.STREETVIEW_SECONDARY_ENABLED
    try:
        tasks_local.STREETVIEW_SECONDARY_ENABLED = True

        def fake_metadata(location):
            metadata_calls.append(location)
            return {
                "status": "OK",
                "pano_id": "shared-pano",
                "location": {"lat": 0.0, "lng": 0.0},
            }

        def fake_image(lat, lon, out_path, **kwargs):
            image_calls.append({"lat": lat, "lon": lon, "path": out_path, **kwargs})
            return kwargs["metadata"]

        tasks_local.get_streetview_metadata_google = fake_metadata
        tasks_local.get_streetview_image_google = fake_image
        primary, alternate, paths = tasks_local._capture_review_streetviews(
            "100 Example Avenue, Test City, MD 20000",
            0.0,
            1.0,
            "job-test",
            3,
            "100_Example",
            lambda local, blob: uploads.append((local, blob)),
            lambda blob: f"/files/{blob}",
        )

        assert metadata_calls == [
            "100 Example Avenue, Test City, MD 20000",
            "0.0000000,1.0000000",
        ]
        assert len(image_calls) == 2
        assert image_calls[0]["location"] == (
            "100 Example Avenue, Test City, MD 20000"
        )
        assert 89.0 < image_calls[0]["heading"] < 91.0
        assert 144.0 < image_calls[1]["heading"] < 146.0
        assert primary.endswith("_streetview_address.jpg")
        assert alternate.endswith("_streetview_context.jpg")
        assert len(paths) == 2
        assert len(uploads) == 2
    finally:
        tasks_local.get_streetview_metadata_google = original_metadata
        tasks_local.get_streetview_image_google = original_image
        tasks_local.STREETVIEW_SECONDARY_ENABLED = original_enabled


if __name__ == "__main__":
    test_address_targeted_streetview_uses_selected_panorama()
    test_capture_adds_at_most_two_views_and_rotates_duplicate_panorama()
    print("OK: Street View reviewer context is address-targeted and bounded.")
