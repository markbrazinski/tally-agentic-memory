"""Gate 6 platform tests: gated controlled send.

Proves: send permitted only when all gates pass; a forced MCP/vector/source/
no-fallback failure blocks send and the provider is NEVER called; idempotent
replay returns the same attempt with no duplicate delivery; missing second
authorization blocks. Zero-network (in-memory fake DB + in-process provider).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.correspondence import GateResult, GateState, SealedFactPack
from src.external.controlled_mail import DemonstrationInboxProvider
from src.external.dal import DAL, Tenant
from src.platform import correspondence_repository as repo
from src.platform.correspondence_repository import (
    SendConflictError,
    approve_and_send,
    draft_from_sealed,
)

TENANT = "10000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        p = params or ()
        self.one = None
        if n == "SELECT now();":
            self.one = (NOW,)
        elif n.startswith("SELECT d.id, d.invoice_id, d.decision_seal_id"):
            d = self.conn.draft
            self.one = ("draft-1", "invoice-1", "seal-1", "sha256:s", "subj",
                        "prose", "{}", "sha256:lf", d["validation_state"])
        elif n.startswith("SELECT id, send_state, gate_state, provider_message_id"):
            self.one = self.conn.existing_send
        elif n.startswith("SELECT status_sequence FROM invoices"):
            self.one = (self.conn.seq,)
        elif n.startswith("INSERT INTO send_attempts"):
            self.conn.send_attempts += 1
        elif n.startswith("INSERT INTO send_gate_runs"):
            self.conn.gate_runs += 1
        elif n.startswith("UPDATE send_attempts"):
            self.conn.send_updates.append(n)
        elif n.startswith("INSERT INTO invoice_events"):
            self.conn.events.append("event")
        elif n.startswith("INSERT INTO event_outbox"):
            self.conn.outbox += 1
        elif n.startswith("UPDATE invoices"):
            self.conn.seq += p[3]
        return self

    def fetchone(self):
        return self.one


class FakeConn:
    def __init__(self, *, validation_state="VALIDATED", existing_send=None):
        self.draft = {"validation_state": validation_state}
        self.existing_send = existing_send
        self.seq = 5
        self.send_attempts = 0
        self.gate_runs = 0
        self.send_updates = []
        self.events = []
        self.outbox = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTxn()

    def close(self):
        pass


def _dal(conn):
    return DAL(conn, Tenant(TENANT, "second.approver"))


def _fresh_gates(mcp="VERIFIED", vector="VERIFIED", source="VERIFIED",
                 no_fallback="VERIFIED"):
    def _g(code, state):
        return lambda: GateResult(code, GateState(state), None)
    return {
        "APPROVED_MEMORY_MCP": _g("APPROVED_MEMORY_MCP", mcp),
        "VECTOR_CLAUSE_BINDING": _g("VECTOR_CLAUSE_BINDING", vector),
        "EXACT_S3_SOURCE": _g("EXACT_S3_SOURCE", source),
        "NO_FALLBACK": _g("NO_FALLBACK", no_fallback),
    }


def _send(conn, provider, *, gates=None, approver="second.approver", key="send-1"):
    return approve_and_send(
        _dal(conn), draft_id="draft-1", idempotency_key=key,
        second_approver_display=approver, gate_checks=gates or _fresh_gates(),
        provider=provider,
    )


class _DraftCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        self.one = None
        if n.startswith("SELECT id FROM correspondence_drafts"):
            self.one = None  # no existing draft
        elif n.startswith("INSERT INTO correspondence_drafts"):
            # body_prose is the 8th column value.
            self.conn.stored_prose = (params or ())[7]
        return self

    def fetchone(self):
        return self.one


class _DraftConn:
    def __init__(self):
        self.stored_prose = None

    def cursor(self):
        return _DraftCursor(self)

    def transaction(self):
        return FakeTxn()


_PACK = SealedFactPack(
    invoice_id="inv-1", decision_seal_id="seal-1", seal_digest="sha256:s",
    recommendation_type="DISPUTE", disputed_amount_minor=70000,
    supported_amount_minor=175000, currency="USD",
    charged_period_start="2026-06-08", charged_period_end="2026-06-14",
    container_ref="TLLU-482931-7", invoice_no="INV-1048", rule_ref="Clause 4.2",
)


def test_draft_auto_generates_prose_when_none_supplied(monkeypatch):
    # Demo v3: with no body_prose, draft_from_sealed generates it from the sealed
    # pack via the injected generator (default deterministic). Locked fields are
    # re-derived from the seal, so validation still passes.
    monkeypatch.setattr(repo, "load_sealed_fact_pack", lambda dal, **k: _PACK)
    conn = _DraftConn()

    class StubGen:
        def draft_body(self, pack):
            return f"GENERATED for {pack.invoice_no} disputing $700.00"

    result = draft_from_sealed(
        _dal(conn), decision_seal_id="seal-1", draft_generator=StubGen()
    )
    assert result.validation_state == "VALIDATED"
    assert conn.stored_prose == "GENERATED for INV-1048 disputing $700.00"


def test_draft_explicit_prose_still_honored(monkeypatch):
    monkeypatch.setattr(repo, "load_sealed_fact_pack", lambda dal, **k: _PACK)
    conn = _DraftConn()
    draft_from_sealed(_dal(conn), decision_seal_id="seal-1", body_prose="EXPLICIT")
    assert conn.stored_prose == "EXPLICIT"


def test_all_gates_pass_sends_to_controlled_inbox():
    conn = FakeConn()
    provider = DemonstrationInboxProvider()
    result = _send(conn, provider)
    assert result.send_state == "SENT"
    assert result.provider_message_id.startswith("demo-")
    assert conn.send_attempts == 1
    assert conn.gate_runs == 6  # all six gate runs persisted
    assert "event" in conn.events


def test_failed_mcp_gate_blocks_and_provider_never_called():
    conn = FakeConn()

    class ForbiddenProvider(DemonstrationInboxProvider):
        def send(self, **kwargs):
            raise AssertionError("provider must not be called when a gate fails")

    result = _send(conn, ForbiddenProvider(), gates=_fresh_gates(mcp="FAILED"))
    assert result.send_state == "SEND_BLOCKED"
    assert result.blocked_reason == "SEND_BLOCKED_MEMORY"
    assert result.provider_message_id is None


def test_failed_source_gate_blocks():
    conn = FakeConn()
    result = _send(conn, DemonstrationInboxProvider(),
                   gates=_fresh_gates(source="FAILED"))
    assert result.send_state == "SEND_BLOCKED"
    assert result.blocked_reason == "SEND_BLOCKED_SOURCE"


def test_failed_no_fallback_gate_blocks():
    conn = FakeConn()
    result = _send(conn, DemonstrationInboxProvider(),
                   gates=_fresh_gates(no_fallback="FAILED"))
    assert result.blocked_reason == "SEND_BLOCKED_FALLBACK"


def test_invalid_locked_fields_blocks():
    conn = FakeConn(validation_state="INVALID")
    result = _send(conn, DemonstrationInboxProvider())
    assert result.send_state == "SEND_BLOCKED"
    assert result.blocked_reason == "SEND_BLOCKED_LOCKED_FIELDS"


def test_missing_second_authorization_blocks():
    conn = FakeConn()
    result = _send(conn, DemonstrationInboxProvider(), approver="")
    assert result.blocked_reason == "SEND_BLOCKED_AUTHORIZATION"


def test_idempotent_replay_no_duplicate_send():
    existing = ("send-existing", "SENT", "PASSED", "demo-abc", None,
                _request_hash())
    conn = FakeConn(existing_send=existing)
    result = _send(conn, DemonstrationInboxProvider())
    assert result.duplicate
    assert result.provider_message_id == "demo-abc"
    assert conn.send_attempts == 0  # no second attempt row


def test_same_key_different_request_conflicts():
    existing = ("send-existing", "SENT", "PASSED", "demo-abc", None,
                "different-hash")
    conn = FakeConn(existing_send=existing)
    with pytest.raises(SendConflictError):
        _send(conn, DemonstrationInboxProvider())


def test_provider_idempotency_no_duplicate_delivery():
    provider = DemonstrationInboxProvider()
    r1 = provider.send(provider_idempotency_key="k1", subject="s", body="b",
                       recipient_class="CONTROLLED_DEMONSTRATION_INBOX")
    r2 = provider.send(provider_idempotency_key="k1", subject="s", body="b",
                       recipient_class="CONTROLLED_DEMONSTRATION_INBOX")
    assert r1.provider_message_id == r2.provider_message_id
    assert r2.duplicate


def _request_hash():
    from src.core.receipt import canonical_json_bytes, prefixed_sha256
    return prefixed_sha256(canonical_json_bytes({
        "draft": "draft-1", "seal_digest": "sha256:s",
    }))
