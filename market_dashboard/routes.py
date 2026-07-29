"""Flask routes for the protected market-runway dashboard."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory

from .store import ReplayGuard, SnapshotStore, SnapshotValidationError


dashboard = Blueprint("market_dashboard", __name__)
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,160}$")
_MAX_CLOCK_SKEW_SECONDS = 300
_MAX_PUBLISH_BYTES = 2 * 1024 * 1024


def _storage_root() -> Path:
    base = os.getenv("MARKET_DASHBOARD_STORAGE_DIR", "").strip()
    if not base:
        base = str(Path(os.getenv("STORAGE_DIR", "storage")) / "market_dashboard")
    return Path(base)


def _store() -> SnapshotStore:
    store = current_app.extensions.get("market_dashboard_store")
    if store is None:
        store = SnapshotStore(_storage_root())
        current_app.extensions["market_dashboard_store"] = store
    return store


def _replay_guard() -> ReplayGuard:
    guard = current_app.extensions.get("market_dashboard_replay_guard")
    if guard is None:
        guard = ReplayGuard(_storage_root() / "publish_nonces.sqlite3")
        current_app.extensions["market_dashboard_replay_guard"] = guard
    return guard


def _publish_secret() -> bytes:
    value = os.getenv("MARKET_DASHBOARD_PUBLISH_SECRET", "").strip()
    return value.encode("utf-8")


def _verify_publish_request(raw: bytes) -> tuple[bool, str]:
    secret = _publish_secret()
    if len(secret) < 32:
        return False, "publisher is not configured"
    if len(raw) > _MAX_PUBLISH_BYTES:
        return False, "payload is too large"

    timestamp_text = request.headers.get("X-Parity-Timestamp", "").strip()
    nonce = request.headers.get("X-Parity-Nonce", "").strip()
    supplied = request.headers.get("X-Parity-Signature", "").strip().lower()
    if supplied.startswith("sha256="):
        supplied = supplied[7:]
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        return False, "invalid timestamp"
    if abs(int(time.time()) - timestamp) > _MAX_CLOCK_SKEW_SECONDS:
        return False, "expired timestamp"
    if not _NONCE_RE.fullmatch(nonce):
        return False, "invalid nonce"
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False, "invalid signature"

    signed = timestamp_text.encode("ascii") + b"." + nonce.encode("ascii") + b"." + raw
    expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return False, "invalid signature"
    if not _replay_guard().use_once(nonce, timestamp):
        return False, "request was already used"
    return True, ""


@dashboard.get("/market-runway")
@dashboard.get("/market-runway/")
def dashboard_index():
    dist = Path(__file__).with_name("dist")
    return send_from_directory(dist, "index.html")


@dashboard.get("/market-runway/assets/<path:filename>")
def dashboard_asset(filename: str):
    dist = Path(__file__).with_name("dist") / "assets"
    response = send_from_directory(dist, filename)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@dashboard.get("/market-runway/data")
def dashboard_data():
    record = _store().current()
    if record is None:
        return jsonify(
            {
                "status": "using_embedded_default",
                "version": None,
                "data": None,
            }
        )
    return jsonify(
        {
            "status": "published",
            "version": {
                "version_id": record["version_id"],
                "published_at": record.get("published_at"),
                "received_at": record.get("received_at"),
                "source": record.get("source", {}),
                "rollback_of": record.get("rollback_of"),
            },
            "data": record["data"],
        }
    )


@dashboard.get("/market-runway/versions")
def dashboard_versions():
    return jsonify({"versions": _store().versions()})


@dashboard.post("/market-runway/rollback/<version_id>")
def dashboard_rollback(version_id: str):
    # The parent Flask app's browser gate applies the shared session and CSRF
    # checks before this route is reached.
    try:
        record = _store().rollback(version_id)
    except (FileNotFoundError, ValueError):
        return jsonify({"error": "version not found"}), 404
    return jsonify(
        {
            "status": "rolled_back",
            "version_id": record["version_id"],
            "rollback_of": record["rollback_of"],
        }
    )


@dashboard.post("/api/market-dashboard/publish")
def dashboard_publish():
    raw = request.get_data(cache=True, as_text=False)
    verified, reason = _verify_publish_request(raw)
    if not verified:
        status = 503 if reason == "publisher is not configured" else 401
        return jsonify({"error": reason}), status
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "body must be valid UTF-8 JSON"}), 400
    try:
        record, created = _store().publish(payload)
    except SnapshotValidationError as exc:
        return jsonify({"error": str(exc)}), 422
    return (
        jsonify(
            {
                "status": "published" if created else "unchanged",
                "version_id": record["version_id"],
                "published_at": record.get("published_at"),
            }
        ),
        201 if created else 200,
    )


@dashboard.get("/api/market-dashboard/health")
def dashboard_health():
    root = _storage_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        ready = root.is_dir() and os.access(root, os.W_OK)
    except OSError:
        ready = False
    configured = len(_publish_secret()) >= 32
    status = 200 if ready else 503
    return (
        jsonify(
            {
                "status": "ok" if ready else "degraded",
                "storage_ready": ready,
                "publisher_configured": configured,
                "has_published_snapshot": _store().current() is not None if ready else False,
            }
        ),
        status,
    )


@dashboard.after_request
def dashboard_headers(response: Response):
    if request.path.startswith("/market-runway"):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
    return response

