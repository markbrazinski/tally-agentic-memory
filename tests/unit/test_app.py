"""Unit tests for src/platform/app.py's routes.

Per CLAUDE.md ("zero network calls in the test suite"), src.platform.app._dal
is patched to return a fake DAL backed by an in-memory fake connection -
no real CockroachDB call happens here.

POST /invoices fires a background asyncio.create_task() that can
genuinely run its real BedrockExtractor() call even after the HTTP
response returns (confirmed: not reliably prevented by test-teardown
timing alone) - the autouse `_no_live_bedrock_in_background_task` fixture
below makes "never call real Bedrock in this test file" a structural
guarantee rather than an accident of timing, by patching
src.platform.app.BedrockExtractor process-wide for every test in this
module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.external.bedrock_extract import CannedResponseExtractor
from src.external.dal import DAL, Tenant
from src.platform.app import app, require_auth
from src.platform.auth import AuthedActor


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        self.description, self._rows = self._conn.dispatch(normalized, params)
        return self

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, responses: dict | None = None):
        self.executed: list[tuple] = []
        self.responses = responses or {}

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def dispatch(self, sql, params):
        for prefix, rows in self.responses.items():
            if sql.startswith(prefix):
                return [("col",)], rows
        return None, []


TENANT_ID = "10000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _no_live_bedrock_in_background_task():
    """Structural guarantee (not accidental timing) that this test file
    never calls real Bedrock, even via POST /invoices' fire-and-forget
    background task."""
    with patch("src.platform.app.BedrockExtractor", return_value=CannedResponseExtractor()):
        yield


def _fake_dal(responses: dict | None = None) -> DAL:
    return DAL(FakeConnection(responses), Tenant(tenant_id=TENANT_ID, actor="rachel.martinez"))


RACHEL_USER_ID = "00000000-0000-0000-0000-000000000042"


@pytest.fixture
def client():
    """Overrides the real bearer-auth dependency with a stub that always
    succeeds - route-level auth ENFORCEMENT is tested directly against
    the real dependency in tests/unit/test_auth.py; these tests exercise
    each route's own logic assuming auth already passed, matching how a
    real authenticated request behaves."""
    app.dependency_overrides[require_auth] = lambda: AuthedActor(
        user_id=RACHEL_USER_ID, display_name="rachel.martinez"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_post_invoices_rejects_missing_auth_with_real_dependency():
    """Confirms the real (non-overridden) auth dependency actually blocks
    an unauthenticated request - the override in the `client` fixture
    above must not be the only thing standing between a real deployment
    and an open write endpoint."""
    real_client = TestClient(app)
    with patch("src.platform.app._dal", return_value=_fake_dal()):
        response = real_client.post(
            "/invoices", files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")}
        )
    assert response.status_code == 401


def test_healthz_reports_db_ok_when_query_succeeds(client):
    with patch("src.platform.app._dal", return_value=_fake_dal()):
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["db"] == "ok"
    assert body["mcp"] == "not_configured"


def test_healthz_truth_labels_configured_mcp_without_claiming_live_health(client, monkeypatch):
    for name in (
        "TALLY_MCP_CLUSTER_ID",
        "TALLY_MCP_DATABASE",
        "TALLY_MCP_ACCESS_TOKEN",
        "TALLY_MCP_SERVICE_IDENTITY",
        "TALLY_MCP_PERMISSION_MODE",
    ):
        monkeypatch.setenv(name, "test-only-placeholder")
    with patch("src.platform.app._dal", return_value=_fake_dal()):
        response = client.get("/healthz")
    assert response.json()["mcp"] == "configured_not_checked"


def test_healthz_query_does_not_reference_nonexistent_tenant_id_column():
    """Regression test: healthz()'s query previously did
    `SELECT 1 FROM tenants WHERE tenant_id=%s` - tenants is the table
    that DEFINES tenant ids via its own `id` column, not a table scoped
    BY a tenant_id column, so this raised a real UndefinedColumn error
    live (confirmed by actually running the built Docker image locally -
    invisible to the mocked test suite, since the fake connection has no
    concept of real column names). Asserts the source doesn't regress to
    the wrong column name."""
    import inspect

    from src.platform.app import healthz

    source = inspect.getsource(healthz)
    assert "FROM tenants WHERE id=" in source
    assert "FROM tenants WHERE tenant_id=" not in source


def test_healthz_reports_db_error_when_query_fails(client):
    class RaisingConnection(FakeConnection):
        def cursor(self):
            raise RuntimeError("connection refused")

    dal = DAL(RaisingConnection(), Tenant(tenant_id=TENANT_ID, actor="rachel.martinez"))
    with patch("src.platform.app._dal", return_value=dal):
        response = client.get("/healthz")

    assert response.status_code == 200  # degraded, not a 500 - judges can hit it either way
    assert response.json()["db"] == "error"


def test_post_invoices_rejects_non_pdf_content_type(client):
    with patch("src.platform.app._dal", return_value=_fake_dal()):
        response = client.post(
            "/invoices", files={"file": ("test.txt", b"not a pdf", "text/plain")}
        )
    assert response.status_code == 422


def test_post_invoices_rejects_file_over_10mb(client):
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    with patch("src.platform.app._dal", return_value=_fake_dal()):
        response = client.post(
            "/invoices", files={"file": ("big.pdf", oversized, "application/pdf")}
        )
    assert response.status_code == 422


def test_post_invoices_accepts_valid_pdf_and_returns_202_shape(client):
    responses = {
        "SELECT id FROM invoices": [],
        "INSERT INTO invoices": [("10000000-0000-4000-8000-000000000001",)],
        "INSERT INTO clerk_runs": [("00000000-0000-0000-0000-000000000002",)],
    }
    with patch("src.platform.app._dal", return_value=_fake_dal(responses)):
        response = client.post(
            "/invoices", files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")}
        )

    assert response.status_code == 202
    body = response.json()
    assert set(body.keys()) == {"invoice_id", "clerk_run_id", "status", "sha256", "s3_key"}
    assert body["status"] == "RECEIVED"


def test_post_invoices_insert_binds_s3_key_and_sha256_to_the_correct_columns(client):
    """Regression test: the INSERT INTO invoices call previously had a
    parameter-slot-shift bug - a redundant manually-passed tenant_id (on
    top of DAL.execute()'s own auto-prepend) shifted every later binding
    by one slot, silently writing the sha256 hash into s3_key instead of
    the intended sentinel/NULL. This asserts the exact param tuple sent
    to the cursor has the right value in the right position, not just
    that *a* row got inserted."""
    fake_dal = _fake_dal(
        {
            "SELECT id FROM invoices": [],
            "INSERT INTO invoices": [("10000000-0000-4000-8000-000000000001",)],
            "INSERT INTO clerk_runs": [("00000000-0000-0000-0000-000000000002",)],
        }
    )
    with patch("src.platform.app._dal", return_value=fake_dal):
        response = client.post(
            "/invoices", files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")}
        )
    assert response.status_code == 202

    conn = fake_dal.conn
    insert_call = next(
        (sql, params) for sql, params in conn.executed if sql.startswith("INSERT INTO invoices")
    )
    sql, params = insert_call
    # full_params = (tenant_id_from_DAL, *caller_params) - the SQL has 4
    # placeholders (outer tenant_id, subquery tenant_id, s3_key, sha256),
    # so params must be exactly 4 values long with s3_key and sha256 in
    # their correct, DISTINCT positions - not the same value twice.
    assert len(params) == 4
    s3_key_value, sha256_value = params[2], params[3]
    assert s3_key_value != sha256_value
    assert s3_key_value == "local-only:no-s3-write-b0-s2"
    assert len(sha256_value) == 64  # a real sha256 hex digest, not a sentinel


def test_post_invoices_returns_duplicate_status_on_repeat_sha256(client):
    responses = {
        "SELECT id FROM invoices": [("00000000-0000-0000-0000-000000000099",)],
    }
    with patch("src.platform.app._dal", return_value=_fake_dal(responses)):
        response = client.post(
            "/invoices", files={"file": ("invoice.pdf", b"%PDF-1.4 fake", "application/pdf")}
        )

    assert response.status_code == 202
    assert response.json()["status"] == "DUPLICATE"


class _BackgroundTaskFakeCursor:
    """Supports both DAL.execute()'s pattern (description + fetchall())
    and run_with_retry's raw-cursor pattern (fetchone(), no description
    needed) - _run_clerk_pipeline_background uses both on the same
    underlying fake connection (one _dal() block for the carrier lookup
    via execute(), a second for file_case() via run_with_retry)."""

    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows: list[tuple] = []
        self._one: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self._conn.executed.append((normalized, params))
        self.description, self._rows, self._one = self._conn.dispatch(normalized, params)
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _BackgroundTaskFakeTransactionCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _BackgroundTaskFakeConnection:
    """One fake connection shared across both DAL instances
    _run_clerk_pipeline_background constructs (one per `with _dal()`
    block) - real code shares one psycopg connection per request/task in
    the same way (_dal() returns a fresh DAL wrapping a fresh connect()
    call in the real app, but for this test the SAME fake conn is
    injected into both via `fake_dal.conn = conn`, matching how the
    underlying data - findings/cases rows - must be visible across both
    blocks for the test's assertions to mean anything)."""

    def __init__(self):
        self.executed: list[tuple] = []
        self.findings: list[dict] = []
        self.cases: list[dict] = []
        self.query_log_rows: list[dict] = []
        self._next_id = 1

    def _new_id(self) -> str:
        val = f"00000000-0000-0000-0000-{self._next_id:012d}"
        self._next_id += 1
        return val

    def cursor(self):
        return _BackgroundTaskFakeCursor(self)

    def transaction(self):
        return _BackgroundTaskFakeTransactionCtx(self)

    def close(self):
        pass

    def dispatch(self, sql, params):
        """Returns (description, fetchall_rows, fetchone_row)."""
        if sql.startswith("SELECT c.id, c.date_format_hint"):
            row = ("00000000-0000-0000-0000-000000000009", None)
            return [("col",)], [row], row
        if sql.startswith("INSERT INTO findings"):
            finding_id = self._new_id()
            self.findings.append({"id": finding_id})
            return None, [], (finding_id,)
        if sql.startswith("INSERT INTO cases"):
            case_id = self._new_id()
            self.cases.append({"id": case_id})
            return None, [], (case_id,)
        if sql.startswith("INSERT INTO query_log"):
            self.query_log_rows.append({"params": params})
            return None, [], None
        return None, [], None


def test_run_clerk_pipeline_background_actually_runs_the_real_pipeline():
    """Regression test: this background task was previously a stub that
    broadcast one WS event and never called the real extraction/steps/
    filing pipeline at all - clerk_runs stayed QUEUED and invoices stayed
    RECEIVED forever on every real POST /invoices call. This awaits the
    function directly (not fire-and-forget) with a real fixture PDF and a
    CannedResponseExtractor, and asserts the atomic filing commit
    actually happened - the whole point of B0-S2's "Clerk background
    task steps 1-3+7 ... end-to-end locally" requirement.
    """
    import asyncio
    import os

    from src.platform.app import _run_clerk_pipeline_background

    fixture_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures", "defective_missing_field7.pdf",
    )
    with open(fixture_path, "rb") as f:
        pdf_bytes = f.read()

    conn = _BackgroundTaskFakeConnection()
    fake_dal = _fake_dal()
    fake_dal.conn = conn

    broadcasts = []

    async def _capture_broadcast(event, payload):
        broadcasts.append((event, payload))

    with patch("src.platform.app._dal", return_value=fake_dal), \
         patch("src.platform.app.feed.broadcast", side_effect=_capture_broadcast):
        asyncio.run(
            _run_clerk_pipeline_background(
                "inv-1", "run-1", pdf_bytes, "fake-sha256",
                extractor=CannedResponseExtractor(),
            )
        )

    events = [e for e, _ in broadcasts]
    assert "clerk.step" in events
    assert "clerk.filed" in events
    assert len(conn.findings) == 1
    assert len(conn.cases) == 1
    # 2 query_log rows: one from the carrier-lookup DAL.execute() call
    # (query-log middleware, automatic), one from file_case's own
    # manual commit-log INSERT (run_with_retry doesn't auto-log).
    assert len(conn.query_log_rows) == 2


def test_get_invoices_returns_items_shape(client):
    responses = {
        "SELECT i.id": [
            (
                "10000000-0000-4000-8000-000000000001",
                "DEMO",
                "Asterline Demo Shipping (fictional)",
                1050.00,
                "DEFECTIVE",
                "541.6(a)(7)",
                datetime(2026, 8, 2, 14, 11, tzinfo=timezone.utc),
                "00000000-0000-0000-0000-000000000002",
            )
        ],
    }
    with patch("src.platform.app._dal", return_value=_fake_dal(responses)):
        response = client.get("/invoices")

    assert response.status_code == 200
    body = response.json()
    assert "items" in body and "next_cursor" in body
    assert body["items"][0]["carrier"] == {
        "scac": "DEMO",
        "name": "Asterline Demo Shipping (fictional)",
    }
    assert body["items"][0]["verdict"] == "DEFECTIVE"


def test_get_case_returns_404_when_not_found(client):
    with patch("src.platform.app._dal", return_value=_fake_dal({"SELECT ca.id": []})):
        response = client.get("/cases/nonexistent")
    assert response.status_code == 404


def test_get_case_returns_case_shape_when_found(client):
    from datetime import date

    responses = {
        "SELECT ca.id": [
            (
                "00000000-0000-0000-0000-000000000002", "ANALYZED", date(2026, 5, 1), 1050.00,
                "10000000-0000-4000-8000-000000000001", "DEFECTIVE", "541.6(a)(7)",
                "Missing required field(s): proper_party_basis.", None,
            )
        ],
    }
    with patch("src.platform.app._dal", return_value=_fake_dal(responses)):
        response = client.get("/cases/00000000-0000-0000-0000-000000000002")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ANALYZED"
    assert body["finding"]["verdict"] == "DEFECTIVE"
    assert body["decision_reason"] is None


def _replay_response():
    return {
        "then": {
            "as_of": "1784578667004573103.0000000000",
            "state": "FILED",
            "tariff_rate": 250.0,
            "version_label": "v2026-04-03",
            "evidence_hash": "sha256:" + "a" * 64,
            "source": "AS OF SYSTEM TIME",
        },
        "now": {
            "state": "CONTESTED",
            "tariff_rate": 250.0,
            "version_label": "v2026-04-03",
            "evidence_hash_recomputed": "sha256:" + "a" * 64,
            "source": "current read",
        },
        "tamper_check": {"match": True},
        "sealed_copy": {
            "rate": 250.0,
            "content_sha256": "sha256:" + "b" * 64,
            "source": "case_evidence (sealed evidence copy)",
        },
        "retention": {
            "ttl_seconds": 7_776_000,
            "ttl_days": 90,
            "target_queryable": True,
            "language": (
                "Versioned S3 retains the dated source artifact. Within CockroachDB’s "
                "configured MVCC window, Tally can also replay the transactional case "
                "state at filing."
            ),
        },
        "queries": ["replay.then", "replay.now"],
    }


def test_get_case_replay_requires_authentication():
    real_client = TestClient(app)
    response = real_client.get(
        "/cases/10000000-0000-4000-8000-000000000003/replay"
    )
    assert response.status_code == 401


def test_get_case_replay_returns_frozen_contract_shape(client):
    with patch("src.platform.app._dal", return_value=_fake_dal()), patch(
        "src.platform.app.replay_case", return_value=_replay_response()
    ):
        response = client.get(
            "/cases/10000000-0000-4000-8000-000000000003/replay"
        )
    assert response.status_code == 200
    assert response.json() == _replay_response()


def test_get_case_replay_wrong_tenant_or_unknown_case_is_404(client):
    from src.platform.temporal_replay import ReplayNotFoundError

    with patch("src.platform.app._dal", return_value=_fake_dal()), patch(
        "src.platform.app.replay_case", side_effect=ReplayNotFoundError()
    ):
        response = client.get(
            "/cases/10000000-0000-4000-8000-000000000003/replay"
        )
    assert response.status_code == 404


def test_get_case_replay_rejects_non_uuid_case_id(client):
    with patch("src.platform.app._dal", return_value=_fake_dal()), patch(
        "src.platform.app.replay_case", side_effect=ValueError("case_id must be a UUID")
    ):
        response = client.get("/cases/not-a-uuid/replay")
    assert response.status_code == 422


def test_get_case_replay_unsealed_case_is_409(client):
    from src.platform.temporal_replay import ReplayNotSealedError

    with patch("src.platform.app._dal", return_value=_fake_dal()), patch(
        "src.platform.app.replay_case", side_effect=ReplayNotSealedError()
    ):
        response = client.get(
            "/cases/10000000-0000-4000-8000-000000000003/replay"
        )
    assert response.status_code == 409


@pytest.mark.parametrize("reason", ["malformed", "history expired", "database unavailable"])
def test_get_case_replay_malformed_expired_or_unavailable_is_503(client, reason):
    from src.platform.temporal_replay import ReplayUnavailableError

    with patch("src.platform.app._dal", return_value=_fake_dal()), patch(
        "src.platform.app.replay_case", side_effect=ReplayUnavailableError(reason)
    ):
        response = client.get(
            "/cases/10000000-0000-4000-8000-000000000003/replay"
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "exact historical replay unavailable"}


def test_get_case_replay_redacts_connection_failures_as_generic_503(client):
    with patch(
        "src.platform.app._dal",
        side_effect=RuntimeError("PRIVATE_DB_HOST_SENTINEL"),
    ):
        response = client.get(
            "/cases/10000000-0000-4000-8000-000000000003/replay"
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "exact historical replay unavailable"}
    assert "PRIVATE_DB_HOST_SENTINEL" not in response.text


@pytest.mark.parametrize("state", ["NOT_PRESSED", "ACCEPTED"])
def test_get_case_serializes_cleanly_with_new_state_vocabulary(client, state):
    """bundle-2-S0: NOT_PRESSED/ACCEPTED are plain strings, not a
    pydantic Literal/Enum (this route returns a hand-built dict, no
    response_model) - confirms a case in either new state still
    round-trips through GET /cases/{id} with its decision_reason."""
    from datetime import date

    responses = {
        "SELECT ca.id": [
            (
                "00000000-0000-0000-0000-000000000003", state, date(2026, 5, 1), 1050.00,
                "10000000-0000-4000-8000-000000000001", "DEFECTIVE", "541.6(a)(7)",
                "Missing required field(s): proper_party_basis.", "carrier declined to pursue",
            )
        ],
    }
    with patch("src.platform.app._dal", return_value=_fake_dal(responses)):
        response = client.get("/cases/00000000-0000-0000-0000-000000000003")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == state
    assert body["decision_reason"] == "carrier declined to pursue"


def test_approve_case_rejects_missing_auth_with_real_dependency():
    real_client = TestClient(app)
    response = real_client.post("/cases/some-id/approve")
    assert response.status_code == 401


def test_approve_case_seals_and_returns_contract_shape(client):
    from tests.unit.test_seal import FakeConnection as SealFakeConnection

    conn = SealFakeConnection(case_state="ANALYZED")
    fake_dal = _fake_dal()
    fake_dal.conn = conn

    with patch("src.platform.app._dal", return_value=fake_dal):
        response = client.post("/cases/10000000-0000-4000-8000-000000000001/approve")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "FILED"
    assert body["already_sealed"] is False
    assert "commit_line" in body


def test_approve_case_returns_404_when_case_not_found(client):
    from tests.unit.test_seal import FakeConnection as SealFakeConnection

    conn = SealFakeConnection(case_exists=False)
    fake_dal = _fake_dal()
    fake_dal.conn = conn

    with patch("src.platform.app._dal", return_value=fake_dal):
        response = client.post("/cases/nonexistent/approve")

    assert response.status_code == 404


def test_approve_case_returns_409_when_contested(client):
    from tests.unit.test_seal import FakeConnection as SealFakeConnection

    conn = SealFakeConnection(case_state="CONTESTED")
    fake_dal = _fake_dal()
    fake_dal.conn = conn

    with patch("src.platform.app._dal", return_value=fake_dal):
        response = client.post("/cases/10000000-0000-4000-8000-000000000001/approve")

    assert response.status_code == 409


def test_approve_case_double_press_is_idempotent_200(client):
    from decimal import Decimal

    from tests.unit.test_seal import FakeConnection as SealFakeConnection

    conn = SealFakeConnection(
        case_state="FILED",
        existing_sealed_txn_ts=Decimal("1783534098823702432.0000000000"),
    )
    fake_dal = _fake_dal()
    fake_dal.conn = conn

    with patch("src.platform.app._dal", return_value=fake_dal):
        response = client.post("/cases/10000000-0000-4000-8000-000000000001/approve")

    assert response.status_code == 200
    assert response.json()["already_sealed"] is True


def test_cors_preflight_from_allowed_origin_echoes_origin_and_methods():
    real_client = TestClient(app)
    response = real_client.options(
        "/healthz",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_cors_preflight_from_disallowed_origin_has_no_acao_header():
    real_client = TestClient(app)
    response = real_client.options(
        "/healthz",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_cors_get_from_allowed_origin_has_acao_header():
    real_client = TestClient(app)
    response = real_client.get("/healthz", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_preflight_for_bearer_authed_post_succeeds_without_auth_header():
    """Preflight != auth: the browser's own OPTIONS request never carries
    the Authorization header, so CORSMiddleware must approve it before
    the real POST (still 401 without a token) is ever sent."""
    real_client = TestClient(app)
    response = real_client.options(
        "/cases/some-id/approve",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_cors_allowed_origins_parses_comma_separated_env_and_trims_whitespace(monkeypatch):
    from src.platform.app import _cors_allowed_origins

    monkeypatch.setenv("TALLY_CORS_ORIGINS", " http://localhost:5173 , http://a.example ")
    monkeypatch.delenv("TALLY_STATIC_ORIGIN", raising=False)
    assert _cors_allowed_origins() == ["http://localhost:5173", "http://a.example"]


def test_cors_allowed_origins_defaults_to_localhost_when_env_unset(monkeypatch):
    from src.platform.app import _cors_allowed_origins

    monkeypatch.delenv("TALLY_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("TALLY_STATIC_ORIGIN", raising=False)
    assert _cors_allowed_origins() == ["http://localhost:5173"]


def test_cors_allowed_origins_folds_in_static_origin_when_set(monkeypatch):
    from src.platform.app import _cors_allowed_origins

    monkeypatch.setenv("TALLY_CORS_ORIGINS", "http://localhost:5173")
    monkeypatch.setenv("TALLY_STATIC_ORIGIN", "https://tally-fe.example")
    assert _cors_allowed_origins() == ["http://localhost:5173", "https://tally-fe.example"]


def test_ws_feed_accepts_connection_and_broadcasts_a_message(client):
    with client.websocket_connect("/feed") as websocket:
        import asyncio

        from src.platform.app import feed

        async def _broadcast():
            await feed.broadcast("clerk.step", {"run_id": "run-1", "step": 1})

        asyncio.run(_broadcast())
        message = websocket.receive_json()

    assert message["event"] == "clerk.step"
    assert message["payload"]["run_id"] == "run-1"
