"""Network-free tests for the standalone market dashboard service."""

from __future__ import annotations

import os
import tempfile
import unittest

from market_dashboard.app import create_app


class StandaloneDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_env = {
            key: os.environ.get(key)
            for key in (
                "DASHBOARD_ACCESS_PASSWORD",
                "DASHBOARD_SESSION_SECRET",
                "DASHBOARD_COOKIE_SECURE",
                "MARKET_DASHBOARD_PUBLISH_SECRET",
                "MARKET_DASHBOARD_STORAGE_DIR",
            )
        }
        os.environ.update(
            {
                "DASHBOARD_ACCESS_PASSWORD": "standalone-test-password",
                "DASHBOARD_SESSION_SECRET": (
                    "standalone-test-session-secret-with-enough-entropy"
                ),
                "DASHBOARD_COOKIE_SECURE": "false",
                "MARKET_DASHBOARD_PUBLISH_SECRET": (
                    "standalone-test-publisher-secret-over-thirty-two-characters"
                ),
                "MARKET_DASHBOARD_STORAGE_DIR": self.temp_dir.name,
            }
        )
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _set_csrf(self, value: str = "test-csrf-token") -> str:
        with self.client.session_transaction() as browser_session:
            browser_session["csrf_token"] = value
        return value

    def test_health_is_public_and_checks_standalone_configuration(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["service"], "parity-market-dashboard")

        storage_health = self.client.get("/api/market-dashboard/health")
        self.assertEqual(storage_health.status_code, 200)
        self.assertTrue(storage_health.get_json()["publisher_configured"])

    def test_dashboard_requires_its_own_password(self):
        response = self.client.get("/market-runway")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/access", response.headers["Location"])

        csrf = self._set_csrf()
        rejected = self.client.post(
            "/access",
            data={
                "csrf_token": csrf,
                "password": "wrong-password",
                "next": "/market-runway",
            },
        )
        self.assertEqual(rejected.status_code, 401)

        csrf = self._set_csrf("second-test-csrf-token")
        accepted = self.client.post(
            "/access",
            data={
                "csrf_token": csrf,
                "password": "standalone-test-password",
                "next": "/market-runway",
            },
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(accepted.headers["Location"].endswith("/market-runway"))

        dashboard = self.client.get("/market-runway")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Parity Runway Dashboard", dashboard.data)
        dashboard.close()

    def test_analyzer_password_does_not_configure_dashboard(self):
        os.environ.pop("DASHBOARD_ACCESS_PASSWORD")
        os.environ["SITE_ACCESS_PASSWORD"] = "analyzer-only-password"
        try:
            app = create_app()
            app.testing = True
            response = app.test_client().get("/market-runway")
            self.assertEqual(response.status_code, 503)
        finally:
            os.environ.pop("SITE_ACCESS_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
