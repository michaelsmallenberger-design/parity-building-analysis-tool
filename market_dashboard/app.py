"""Standalone Flask application for the private Parity market dashboard."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from datetime import timedelta

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .routes import dashboard as market_dashboard_blueprint


_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 8
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app() -> Flask:
    """Create the dashboard-only service without importing the analyzer."""
    configured_session_secret = os.getenv(
        "DASHBOARD_SESSION_SECRET", ""
    ).strip()
    configured_access_password = os.getenv(
        "DASHBOARD_ACCESS_PASSWORD", ""
    ).strip()
    app = Flask(
        __name__,
        static_folder=None,
        template_folder="templates",
    )
    app.config.update(
        SECRET_KEY=configured_session_secret or secrets.token_urlsafe(48),
        DASHBOARD_ACCESS_PASSWORD=configured_access_password,
        DASHBOARD_SESSION_CONFIGURED=len(configured_session_secret) >= 32,
        PERMANENT_SESSION_LIFETIME=timedelta(
            hours=max(1, int(os.getenv("DASHBOARD_SESSION_HOURS", "12")))
        ),
        SESSION_COOKIE_SECURE=_env_flag("DASHBOARD_COOKIE_SECURE"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    app.register_blueprint(market_dashboard_blueprint)

    def access_configured() -> bool:
        return bool(
            len(app.config["DASHBOARD_ACCESS_PASSWORD"]) >= 12
            and app.config["DASHBOARD_SESSION_CONFIGURED"]
        )

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def csrf_is_valid() -> bool:
        expected = session.get("csrf_token", "")
        supplied = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        return bool(
            expected and supplied and hmac.compare_digest(expected, supplied)
        )

    def safe_next_url(value: str) -> str:
        if value and value.startswith("/") and not value.startswith("//"):
            return value
        return url_for("root")

    def login_client_key() -> str:
        return request.remote_addr or "unknown"

    def login_retry_after(client_key: str) -> int:
        now = time.time()
        with _login_attempts_lock:
            attempts = [
                stamp
                for stamp in _login_attempts.get(client_key, [])
                if stamp > now - _LOGIN_WINDOW_SECONDS
            ]
            _login_attempts[client_key] = attempts
            if len(attempts) < _LOGIN_MAX_FAILURES:
                return 0
            return max(
                1,
                int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])),
            )

    def record_login_failure(client_key: str) -> None:
        now = time.time()
        with _login_attempts_lock:
            attempts = [
                stamp
                for stamp in _login_attempts.get(client_key, [])
                if stamp > now - _LOGIN_WINDOW_SECONDS
            ]
            attempts.append(now)
            _login_attempts[client_key] = attempts

    def clear_login_failures(client_key: str) -> None:
        with _login_attempts_lock:
            _login_attempts.pop(client_key, None)

    def route_is_exempt(path: str) -> bool:
        return path in {
            "/access",
            "/health",
            "/api/market-dashboard/health",
            "/api/market-dashboard/publish",
        }

    @app.context_processor
    def template_security_context():
        return {"csrf_token": csrf_token()}

    @app.before_request
    def require_dashboard_password():
        if route_is_exempt(request.path):
            return None
        if not access_configured():
            if request.path.startswith("/api/"):
                return jsonify({"error": "dashboard access is not configured"}), 503
            return (
                render_template(
                    "access.html",
                    configured=False,
                    error=(
                        "Dashboard access is not configured. "
                        "Contact the Parity administrator."
                    ),
                    next_url=safe_next_url(request.full_path),
                ),
                503,
            )
        if not session.get("dashboard_access_granted"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "dashboard password required"}), 401
            return redirect(
                url_for("access", next=safe_next_url(request.full_path))
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not csrf_is_valid():
                return jsonify({"error": "invalid CSRF token"}), 403
        return None

    @app.get("/")
    def root():
        return redirect(url_for("market_dashboard.dashboard_index"))

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "ok" if access_configured() else "degraded",
                "service": "parity-market-dashboard",
                "access_configured": access_configured(),
            }
        ), (200 if access_configured() else 503)

    @app.route("/access", methods=["GET", "POST"])
    def access():
        next_url = safe_next_url(request.values.get("next", ""))
        if not access_configured():
            return (
                render_template(
                    "access.html",
                    configured=False,
                    error=(
                        "Dashboard access is not configured. "
                        "Contact the Parity administrator."
                    ),
                    next_url=next_url,
                ),
                503,
            )
        if request.method == "GET":
            if session.get("dashboard_access_granted"):
                return redirect(next_url)
            return render_template(
                "access.html",
                configured=True,
                error="",
                next_url=next_url,
            )

        if not csrf_is_valid():
            return (
                render_template(
                    "access.html",
                    configured=True,
                    error="Please refresh the page and try again.",
                    next_url=next_url,
                ),
                403,
            )
        client_key = login_client_key()
        retry_after = login_retry_after(client_key)
        if retry_after:
            return (
                render_template(
                    "access.html",
                    configured=True,
                    error=(
                        "Too many attempts. Try again in "
                        f"{retry_after // 60 + 1} minutes."
                    ),
                    next_url=next_url,
                ),
                429,
            )

        submitted = request.form.get("password", "")
        configured = app.config["DASHBOARD_ACCESS_PASSWORD"]
        if not hmac.compare_digest(submitted, configured):
            record_login_failure(client_key)
            return (
                render_template(
                    "access.html",
                    configured=True,
                    error="That password is not correct.",
                    next_url=next_url,
                ),
                401,
            )

        clear_login_failures(client_key)
        session.clear()
        session.permanent = True
        session["dashboard_access_granted"] = True
        csrf_token()
        return redirect(next_url)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("access"))

    @app.after_request
    def security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'",
        )
        if request.path == "/access":
            response.headers["Cache-Control"] = "no-store"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    return app


app = create_app()
