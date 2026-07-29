"""Cheap, network-free tests for the Sheet-published market dashboard."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from copy import deepcopy

from flask import Flask

from market_dashboard.routes import dashboard
from market_dashboard.store import (
    SnapshotStore,
    SnapshotValidationError,
    validate_snapshot,
)


PUBLISH_SECRET = "test-only-secret-that-is-more-than-thirty-two-characters"


def sample_snapshot() -> dict:
    return {
        "schema_version": 1,
        "source": {
            "spreadsheet_id": "test-sheet-123",
            "spreadsheet_title": "Market Dashboard Test",
            "published_at": "2026-07-29T20:00:00.000Z",
            "published_by": "test@example.com",
            "publish_note": "Network-free test",
        },
        "assumptions": {
            "som_percent": 50,
            "cad_rate": 1.37,
            "opt_install": 60,
            "opt_arr_annual": 8,
            "opt_contract": 100,
            "per_install": 25,
            "per_arr_annual": 5,
            "per_contract": 50,
            "sync_arr_annual": 4,
            "sync_contract": 20,
        },
        "summary": {
            "total_properties": 10,
            "optimizer_good_fits": 2,
            "periscope_good_fits": 3,
            "current_customers": 1,
            "tam_properties": 10,
            "sam_properties": 5,
            "tam_revenue": 1_000,
            "sam_revenue": 390,
            "current_revenue": 100,
            "region_properties": 8,
            "region_opt_fits": 2,
            "region_per_fits": 3,
        },
        "metros": [
            {
                "metro": "Test Metro",
                "opt": 2,
                "per": 3,
                "cust": 1,
                "optRev": 200,
                "perRev": 150,
                "syncRev": 40,
                "totalRev": 390,
            }
        ],
        "companies": [
            {
                "company": "Test Company",
                "total": 10,
                "opt": 2,
                "per": 3,
                "cust": 1,
                "optRev": 200,
                "perRev": 150,
                "totalRev": 350,
                "penetration": 10,
            }
        ],
        "company_regions": [
            {
                "company": "Test Company",
                "total": 8,
                "opt": 2,
                "per": 3,
                "cust": 1,
                "penetration": 10,
                "optRev": 200,
                "perRev": 150,
                "syncRev": 40,
                "totalRev": 390,
            }
        ],
    }


class SnapshotStoreTests(unittest.TestCase):
    def test_validation_normalizes_valid_snapshot(self):
        normalized = validate_snapshot(sample_snapshot())
        self.assertEqual(normalized["summary"]["sam_revenue"], 390)
        self.assertEqual(normalized["metros"][0]["metro"], "Test Metro")

    def test_validation_rejects_inconsistent_revenue(self):
        payload = sample_snapshot()
        payload["metros"][0]["totalRev"] = 400
        with self.assertRaisesRegex(SnapshotValidationError, "totalRev"):
            validate_snapshot(payload)

    def test_publish_is_idempotent_and_rollback_creates_a_new_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(temp_dir)
            first, created = store.publish(sample_snapshot())
            self.assertTrue(created)
            repeated_payload = sample_snapshot()
            repeated_payload["source"]["published_at"] = "2026-07-29T20:05:00.000Z"
            repeated_payload["source"]["published_by"] = "another-editor@example.com"
            repeated, created = store.publish(repeated_payload)
            self.assertFalse(created)
            self.assertEqual(repeated["version_id"], first["version_id"])

            changed = sample_snapshot()
            changed["source"]["publish_note"] = "Second version"
            second, created = store.publish(changed)
            self.assertTrue(created)
            self.assertNotEqual(second["version_id"], first["version_id"])

            rollback = store.rollback(first["version_id"])
            self.assertEqual(rollback["rollback_of"], first["version_id"])
            self.assertNotEqual(rollback["version_id"], first["version_id"])
            self.assertEqual(store.current()["data"], first["data"])
            self.assertEqual(len(store.versions()), 3)


class PublishRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_storage = os.environ.get("MARKET_DASHBOARD_STORAGE_DIR")
        self.original_secret = os.environ.get("MARKET_DASHBOARD_PUBLISH_SECRET")
        os.environ["MARKET_DASHBOARD_STORAGE_DIR"] = self.temp_dir.name
        os.environ["MARKET_DASHBOARD_PUBLISH_SECRET"] = PUBLISH_SECRET
        app = Flask(__name__)
        app.register_blueprint(dashboard)
        app.testing = True
        self.client = app.test_client()

    def tearDown(self):
        if self.original_storage is None:
            os.environ.pop("MARKET_DASHBOARD_STORAGE_DIR", None)
        else:
            os.environ["MARKET_DASHBOARD_STORAGE_DIR"] = self.original_storage
        if self.original_secret is None:
            os.environ.pop("MARKET_DASHBOARD_PUBLISH_SECRET", None)
        else:
            os.environ["MARKET_DASHBOARD_PUBLISH_SECRET"] = self.original_secret
        self.temp_dir.cleanup()

    @staticmethod
    def signed_request(payload: dict, *, nonce: str = "nonce_1234567890_secure"):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        signed = timestamp.encode() + b"." + nonce.encode() + b"." + body
        signature = hmac.new(
            PUBLISH_SECRET.encode(), signed, hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Parity-Timestamp": timestamp,
            "X-Parity-Nonce": nonce,
            "X-Parity-Signature": f"sha256={signature}",
        }
        return body, headers

    def test_signed_publish_is_visible_and_replay_is_rejected(self):
        body, headers = self.signed_request(sample_snapshot())
        response = self.client.post(
            "/api/market-dashboard/publish", data=body, headers=headers
        )
        self.assertEqual(response.status_code, 201)
        version_id = response.get_json()["version_id"]

        data_response = self.client.get("/market-runway/data")
        self.assertEqual(data_response.status_code, 200)
        self.assertEqual(data_response.get_json()["version"]["version_id"], version_id)

        replay = self.client.post(
            "/api/market-dashboard/publish", data=body, headers=headers
        )
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.get_json()["error"], "request was already used")

    def test_invalid_snapshot_does_not_replace_current(self):
        body, headers = self.signed_request(sample_snapshot())
        first = self.client.post(
            "/api/market-dashboard/publish", data=body, headers=headers
        )
        current_version = first.get_json()["version_id"]

        invalid = deepcopy(sample_snapshot())
        invalid["summary"]["sam_revenue"] = 999
        bad_body, bad_headers = self.signed_request(
            invalid, nonce="nonce_0987654321_secure"
        )
        rejected = self.client.post(
            "/api/market-dashboard/publish", data=bad_body, headers=bad_headers
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(
            self.client.get("/market-runway/data").get_json()["version"]["version_id"],
            current_version,
        )


if __name__ == "__main__":
    unittest.main()
