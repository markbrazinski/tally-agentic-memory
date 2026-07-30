"""build_fresh_gate_checks — real-DB gate reads promoted from the Gate-6 trace.

Proves each of the four fresh gates reads the right table/column and maps the
expected value to VERIFIED, anything else to FAILED. Zero network: a tiny fake
cursor answers the four SELECTs by table name.
"""

from __future__ import annotations

from src.core.correspondence import GateState
from src.external.dal import DAL, Tenant
from src.platform.send_gates import build_fresh_gate_checks

TENANT = "10000000-0000-4000-8000-000000000009"


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        n = " ".join(sql.split())
        if n.startswith("SELECT state FROM reconstructions"):
            self.one = (self.conn.recon_state,)
        elif n.startswith("SELECT validation_state FROM applicable_rules"):
            self.one = (self.conn.rule_state,)
        elif n.startswith("SELECT preservation_status FROM invoice_sources"):
            self.one = (self.conn.source_state,)
        else:  # pragma: no cover
            self.one = None

    def fetchone(self):
        return self.one


class _Conn:
    def __init__(self, recon_state, rule_state, source_state):
        self.recon_state = recon_state
        self.rule_state = rule_state
        self.source_state = source_state

    def cursor(self):
        return _Cur(self)


def _checks(recon="COMPLETE", rule="VERIFIED", source="VERSION_VERIFIED"):
    dal = DAL(_Conn(recon, rule, source), Tenant(TENANT, "tester"))
    return build_fresh_gate_checks(dal, invoice_id="inv-1", decision_seal_id="seal-1")


def test_all_gates_verified_on_expected_values():
    checks = _checks()
    assert checks["APPROVED_MEMORY_MCP"]().state is GateState.VERIFIED
    assert checks["VECTOR_CLAUSE_BINDING"]().state is GateState.VERIFIED
    assert checks["EXACT_S3_SOURCE"]().state is GateState.VERIFIED
    assert checks["NO_FALLBACK"]().state is GateState.VERIFIED


def test_each_gate_fails_on_wrong_value():
    assert _checks(recon="RUNNING")["APPROVED_MEMORY_MCP"]().state is GateState.FAILED
    assert _checks(rule="UNVERIFIED")["VECTOR_CLAUSE_BINDING"]().state is GateState.FAILED
    assert _checks(source="PENDING")["EXACT_S3_SOURCE"]().state is GateState.FAILED


def test_gate_codes_are_the_four_injected_gates():
    assert set(_checks()) == {
        "APPROVED_MEMORY_MCP", "VECTOR_CLAUSE_BINDING",
        "EXACT_S3_SOURCE", "NO_FALLBACK",
    }
