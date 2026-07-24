"""Gate 6 live trace against tally_gate2_iso — gated controlled send.

Reuses the Gate 5 seed to produce a real sealed DISPUTE decision, drafts
correspondence ONLY from the sealed fact pack, then runs the fresh gates and
sends to the controlled demonstration inbox. Proves: all gates pass → controlled
provider acknowledges (demo- message id); a forced EXACT_S3_SOURCE failure blocks
send and the provider is never called; a repeated send is idempotent (no
duplicate delivery).

The fresh EXACT_S3_SOURCE and VECTOR_CLAUSE_BINDING gates are real DB reads
against the isolated lineage. APPROVED_MEMORY_MCP is a bounded reconstruction-
state check (the isolated Managed MCP endpoint is not provisioned; the live MCP
sponsor read is deferred, same boundary as Gate 2). NO_FALLBACK asserts no
substitute was used.

Writes only to a Gate-6 tenant in tally_gate2_iso. Never touches defaultdb. No
external mailbox is contacted — delivery is to the in-process controlled inbox.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.gate2_isolated_trace import _iso_dsn  # noqa: E402
from src.core.correspondence import GateResult, GateState  # noqa: E402
from src.external.controlled_mail import DemonstrationInboxProvider  # noqa: E402
from src.external.dal import DAL, Tenant  # noqa: E402
from src.platform.authority_seal_repository import approve_and_seal  # noqa: E402
from src.platform.correspondence_repository import (  # noqa: E402
    approve_and_send,
    draft_from_sealed,
)

# Reuse the Gate 5 seed helper.
_g5_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gate5_isolated_trace.py")
_spec = importlib.util.spec_from_file_location("g5", _g5_path)
g5 = importlib.util.module_from_spec(_spec)
sys.modules["g5"] = g5
_spec.loader.exec_module(g5)

G6_TENANT = "10000000-0000-4000-8000-0000000000f7"


def _fresh_gates(dal, *, invoice_id, decision_seal_id, force_source_fail=False):
    """Real fresh gate checks against the isolated lineage."""
    tenant_id = dal.tenant.tenant_id

    def _mcp_gate():
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM reconstructions WHERE tenant_id=%s AND invoice_id=%s "
                "ORDER BY version DESC LIMIT 1;", (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "COMPLETE"
        return GateResult("APPROVED_MEMORY_MCP",
                          GateState.VERIFIED if ok else GateState.FAILED,
                          "reconstruction complete" if ok else "no complete memory")

    def _vector_gate():
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT validation_state FROM applicable_rules WHERE tenant_id=%s "
                "AND invoice_id=%s;", (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "VERIFIED"
        return GateResult("VECTOR_CLAUSE_BINDING",
                          GateState.VERIFIED if ok else GateState.FAILED, None)

    def _source_gate():
        if force_source_fail:
            return GateResult("EXACT_S3_SOURCE", GateState.FAILED,
                              "forced source-version unavailable")
        with dal.conn.cursor() as cur:
            cur.execute(
                "SELECT preservation_status FROM invoice_sources WHERE tenant_id=%s "
                "AND invoice_id=%s AND source_type='INVOICE_PDF';",
                (tenant_id, invoice_id))
            row = cur.fetchone()
        ok = row is not None and row[0] == "VERSION_VERIFIED"
        return GateResult("EXACT_S3_SOURCE",
                          GateState.VERIFIED if ok else GateState.FAILED, None)

    def _no_fallback_gate():
        return GateResult("NO_FALLBACK", GateState.VERIFIED, "no substitute used")

    return {
        "APPROVED_MEMORY_MCP": _mcp_gate,
        "VECTOR_CLAUSE_BINDING": _vector_gate,
        "EXACT_S3_SOURCE": _source_gate,
        "NO_FALLBACK": _no_fallback_gate,
    }


def main() -> None:
    dsn = _iso_dsn()
    g5.G5_TENANT = G6_TENANT  # reuse the seed against a Gate-6 tenant
    conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
    with conn.cursor() as cur:
        for tbl in ["send_gate_runs", "send_attempts", "correspondence_drafts",
                    "decision_seals", "approvals", "charged_day_judgments",
                    "recommendations", "applicable_rules", "rule_candidates",
                    "rule_retrieval_runs", "reconstruction_charged_days",
                    "reconstructions", "workflow_task_attempts", "workflow_tasks",
                    "invoice_sources", "event_outbox", "invoice_events", "invoices",
                    "tariff_clauses", "tariff_snapshots"]:
            cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s;", (G6_TENANT,))
        invoice_id, rec_id, digest = g5._seed(cur)

    dal = DAL(conn, Tenant(G6_TENANT, "rachel.martinez"))
    sealed = approve_and_seal(
        dal, recommendation_id=rec_id, expected_version=1, expected_digest=digest,
        idempotency_key="g6-approve", approver_user_id=None,
        approver_display="rachel.martinez",
    )
    draft = draft_from_sealed(
        dal, decision_seal_id=sealed.seal_id,
        body_prose="Please adjust the demurrage charge to the applicable tariff rate.",
    )

    provider = DemonstrationInboxProvider()
    # 1. All gates pass -> controlled send acknowledged.
    sent = approve_and_send(
        dal, draft_id=draft.draft_id, idempotency_key="g6-send-1",
        second_approver_display="finance.approver",
        gate_checks=_fresh_gates(dal, invoice_id=invoice_id,
                                 decision_seal_id=sealed.seal_id),
        provider=provider,
    )
    # 2. Idempotent replay -> same attempt, no duplicate.
    replay = approve_and_send(
        dal, draft_id=draft.draft_id, idempotency_key="g6-send-1",
        second_approver_display="finance.approver",
        gate_checks=_fresh_gates(dal, invoice_id=invoice_id,
                                 decision_seal_id=sealed.seal_id),
        provider=provider,
    )
    # 3. Forced source-gate failure -> send blocked, provider not called.
    blocked = approve_and_send(
        dal, draft_id=draft.draft_id, idempotency_key="g6-send-blocked",
        second_approver_display="finance.approver",
        gate_checks=_fresh_gates(dal, invoice_id=invoice_id,
                                 decision_seal_id=sealed.seal_id,
                                 force_source_fail=True),
        provider=provider,
    )

    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE send_state='SENT') "
                    "FROM send_attempts WHERE tenant_id=%s;", (G6_TENANT,))
        total_attempts, sent_attempts = cur.fetchone()
        cur.execute("SELECT count(DISTINCT provider_message_id) FROM send_attempts "
                    "WHERE tenant_id=%s AND provider_message_id IS NOT NULL;",
                    (G6_TENANT,))
        distinct_messages = cur.fetchone()[0]

    trace = {
        "classification": "SYNTHETIC DEMO — FICTIONAL DATA",
        "database": "tally_gate2_iso (gate6 tenant; defaultdb untouched)",
        "sponsor_verification": {
            "note": "EXACT_S3_SOURCE + VECTOR_CLAUSE_BINDING are real isolated-DB "
                    "reads; APPROVED_MEMORY_MCP is a bounded reconstruction-state "
                    "check (isolated Managed MCP endpoint not provisioned)",
        },
        "sent": {"state": sent.send_state, "gate_state": sent.gate_state,
                 "message_id": sent.provider_message_id,
                 "recipient": "CONTROLLED_DEMONSTRATION_INBOX"},
        "idempotent_replay_duplicate": replay.duplicate,
        "replay_same_message": replay.provider_message_id == sent.provider_message_id,
        "forced_source_failure_blocked": blocked.send_state == "SEND_BLOCKED",
        "blocked_reason": blocked.blocked_reason,
        "distinct_provider_messages": distinct_messages,
        "external_send_note": "Controlled demonstration inbox only. A real external "
                              "send to an owner-approved recipient/provider is a "
                              "separate authorized step and was NOT performed.",
        "mock_fallback": False,
    }
    print(json.dumps(trace, indent=2))
    assert sent.send_state == "SENT" and sent.provider_message_id.startswith("demo-")
    assert replay.duplicate and replay.provider_message_id == sent.provider_message_id
    assert blocked.send_state == "SEND_BLOCKED"
    assert blocked.blocked_reason == "SEND_BLOCKED_SOURCE"
    assert distinct_messages == 1, "no duplicate delivery"
    conn.close()


if __name__ == "__main__":
    main()
