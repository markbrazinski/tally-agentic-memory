"""Judge-demo Cognito auth enforcement tests (zero-network, JWT mocked).

Proves: the public allowlist (login, health, static) is reachable; every other
route (pages, /api reads, SSE, PDF, import) requires a valid JWT; missing/invalid
tokens 401 (API) or redirect to /login (page); a valid token passes; logout
clears the session.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def judge_app(monkeypatch, tmp_path):
    monkeypatch.setenv("TALLY_JUDGE_AUTH_ENABLED", "true")
    monkeypatch.setenv("TALLY_COGNITO_USER_POOL_ID", "us-east-1_TEST")
    monkeypatch.setenv("TALLY_COGNITO_CLIENT_ID", "testclient")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TALLY_TENANT_ID", "10000000-0000-4000-8000-000000000002")
    # Intake-demo mode is how the judge lane runs in production; its
    # restrict_public_demo_surface middleware 404s any path not in its intake
    # allowlist BEFORE the router runs. This once 404'd /login and /api/login
    # (auth enforced but no way to log in). Enable it here so that interaction
    # is exercised.
    monkeypatch.setenv("TALLY_INTAKE_DEMO_ENABLED", "true")
    # A present static dir mounts StaticFiles(html=True) at "/". That catch-all
    # shadowed /login and /api/login live (they 404'd) until the auth routes were
    # registered directly on `app` instead of via a lazily-materialized router.
    # Reproduce that condition so the regression can't come back.
    (tmp_path / "index.html").write_text("<html>root</html>")
    monkeypatch.setenv("TALLY_STATIC_DIR", str(tmp_path))
    # Mock JWT verification: "goodtoken" is valid, anything else raises 401.
    import src.platform.cognito_auth as cog

    def fake_verify(token, config):
        if token == "goodtoken":
            return {"token_use": "id", "email": "judge@demo.example", "aud": config.client_id}
        raise cog.CognitoAuthError("invalid_token")

    monkeypatch.setattr(cog, "verify_cognito_jwt", fake_verify)
    # judge_auth imports verify_cognito_jwt by reference; patch there too.
    import src.platform.judge_auth as ja
    monkeypatch.setattr(ja, "verify_cognito_jwt", fake_verify)

    app_mod = importlib.reload(importlib.import_module("src.platform.app"))
    return TestClient(app_mod.app)


def test_public_allowlist_reachable_without_auth(judge_app):
    assert judge_app.get("/login").status_code == 200
    assert judge_app.get("/healthz").status_code == 200
    assert judge_app.get("/readyz").status_code in (200, 503)  # 503 if DB unreachable


def test_login_routes_not_shadowed_by_static_mount(judge_app):
    # With a static dir present (mount at "/"), /login must still serve the login
    # page and /api/logout must still be reachable — proof the auth routes win
    # over the StaticFiles catch-all. (Regression: they 404'd live.)
    assert judge_app.get("/login").status_code == 200
    assert judge_app.post("/api/logout").status_code == 200


def test_login_survives_intake_public_surface_filter(judge_app):
    # With TALLY_INTAKE_DEMO_ENABLED=true, restrict_public_demo_surface must not
    # 404 the login endpoints before the auth middleware/router run. This was the
    # live root cause: /login and /api/login 404'd because they weren't in the
    # intake allowlist. /api/login reaching Cognito (not 404) proves it passes
    # the filter; the login page must render.
    assert judge_app.get("/login").status_code == 200
    # /api/login reaching its handler (any non-404 status, or a boto error from
    # the handler calling Cognito with no creds) proves it passed the public
    # surface filter. A 404 would mean the filter swallowed it — the live bug.
    try:
        status = judge_app.post("/api/login", json={"username": "x", "password": "y"}).status_code
        assert status != 404
    except Exception as exc:  # noqa: BLE001 - handler ran (reached boto) = passed the filter
        assert "cognito" in str(exc).lower() or "credential" in str(exc).lower() \
            or "endpoint" in str(exc).lower() or "connect" in str(exc).lower()


def test_api_read_requires_token(judge_app):
    assert judge_app.get("/api/invoices").status_code == 401


def test_sse_stream_requires_token(judge_app):
    assert judge_app.get("/api/stream").status_code == 401


def test_pdf_source_requires_token(judge_app):
    r = judge_app.get("/api/invoices/x/sources/y/content")
    assert r.status_code == 401


def test_import_endpoint_requires_token(judge_app):
    r = judge_app.post("/api/demo/invoices")
    assert r.status_code == 401


def test_page_navigation_redirects_to_login(judge_app):
    r = judge_app.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_invalid_token_rejected(judge_app):
    r = judge_app.get("/api/invoices", headers={"Authorization": "Bearer badtoken"})
    assert r.status_code == 401


def _auth_passed(client, **kwargs):
    """A valid token clears the auth middleware; the route then hits the DB,
    which isn't configured in unit tests and raises (surfacing as a raised
    RuntimeError through TestClient, not a 401). Either a non-401 response or a
    DB RuntimeError proves the auth gate let the request through."""
    try:
        r = client.get("/api/invoices", **kwargs)
        return r.status_code != 401
    except RuntimeError as exc:
        return "connection string" in str(exc)  # reached the DB layer = past auth


def test_valid_token_passes_auth_gate(judge_app):
    assert _auth_passed(judge_app, headers={"Authorization": "Bearer goodtoken"})


def test_valid_cookie_passes_auth_gate(judge_app):
    judge_app.cookies.set("tally_session", "goodtoken")
    assert _auth_passed(judge_app)


def test_logout_clears_cookie(judge_app):
    r = judge_app.post("/api/logout")
    assert r.status_code == 200
    # The Set-Cookie should expire the session.
    assert "tally_session" in r.headers.get("set-cookie", "")
