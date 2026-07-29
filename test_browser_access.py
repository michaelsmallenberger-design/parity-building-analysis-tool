"""Offline checks for the shared-password browser gate. No provider calls."""
import os
import re

# Must be set before importing the Flask app, just as Render does at startup.
os.environ["SITE_ACCESS_ENABLED"] = "true"
os.environ["SITE_ACCESS_PASSWORD"] = "test-only-password"
os.environ["SITE_SESSION_SECRET"] = "test-only-session-secret"
os.environ["SITE_COOKIE_SECURE"] = "false"

import app_railway

app = app_railway.app


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
    assert match, "expected a CSRF token in the browser form"
    return match.group(1)


def run():
    app.config.update(TESTING=True)
    client = app.test_client()

    blocked = client.get("/")
    assert blocked.status_code == 302
    assert "/access" in blocked.headers["Location"]
    blocked_file = client.get("/files/results/private.jpg")
    assert blocked_file.status_code == 302
    assert "/access" in blocked_file.headers["Location"]

    access = client.get("/access")
    assert access.status_code == 200
    token = _csrf(access)

    wrong = client.post("/access", data={
        "password": "not-the-password", "csrf_token": token, "next": "/"
    })
    assert wrong.status_code == 401

    granted = client.post("/access", data={
        "password": "test-only-password", "csrf_token": token, "next": "/"
    })
    assert granted.status_code == 302
    assert granted.headers["Location"].endswith("/")

    home = client.get("/")
    assert home.status_code == 200
    token = _csrf(home)

    original_exists = app_railway.file_exists
    original_read = app_railway.read_file
    try:
        app_railway.file_exists = lambda _path: True
        app_railway.read_file = lambda _path: b"private customer artifact"
        private_file = client.get("/files/results/private.jpg")
        assert private_file.status_code == 200
        cache_control = private_file.headers.get("Cache-Control", "")
        assert "private" in cache_control
        assert "no-store" in cache_control
        assert "public" not in cache_control
        assert private_file.headers.get("Pragma") == "no-cache"
        assert private_file.headers.get("Expires") == "0"
    finally:
        app_railway.file_exists = original_exists
        app_railway.read_file = original_read

    no_csrf_review = client.post("/api/review", json={"job_id": "missing", "row_id": 1})
    assert no_csrf_review.status_code == 403
    valid_csrf_review = client.post(
        "/api/review", json={"job_id": "missing", "row_id": 1},
        headers={"X-CSRF-Token": token},
    )
    assert valid_csrf_review.status_code == 404

    # External health checks remain available without the shared browser password.
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json["large_workbook_approval_ready"] is True
    assert isinstance(
        health.json["large_workbook_cost_estimate_configured"], bool
    )
    assert "configured" in health.json["large_workbook_cost_configuration"]

    bad_logout = client.post("/logout")
    assert bad_logout.status_code == 403
    logged_out = client.post("/logout", data={"csrf_token": token})
    assert logged_out.status_code == 302
    assert client.get("/").status_code == 302

    print("browser access tests passed")


if __name__ == "__main__":
    run()
