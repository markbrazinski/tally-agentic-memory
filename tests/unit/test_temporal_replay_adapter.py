from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.platform.temporal_replay import (
    _ROW_COLUMNS,
    REPLAY_TABLES,
    ReplayNotFoundError,
    ReplayNotSealedError,
    ReplayUnavailableError,
    _historical_query,
    replay_case,
)
from tests.unit.test_temporal_replay import CASE_ID, HLC, TENANT_ID, replay_rows


def _tuples(rows):
    return [tuple(row[column] for column in _ROW_COLUMNS) for row in rows]


@dataclass
class _Tenant:
    tenant_id: str = TENANT_ID
    actor: str = "synthetic-reviewer"


class FakeDAL:
    def __init__(self, *, current=None, historical=None, retention_ttl=7_776_000, aost_error=None):
        self.tenant = _Tenant()
        self.current = _tuples(replay_rows(state="CONTESTED")) if current is None else current
        self.historical = _tuples(replay_rows()) if historical is None else historical
        self.retention_ttl = retention_ttl
        self.aost_error = aost_error
        self.log_failure_count = 0
        self.calls = []

    def execute(self, sql, params, **kwargs):
        self.calls.append((sql, params, kwargs))
        tag = kwargs["tag"]
        if tag == "replay.now":
            return self.current
        if tag == "replay.retention":
            return [
                (
                    table,
                    f"ALTER TABLE {table} CONFIGURE ZONE USING "
                    f"gc.ttlseconds = {self.retention_ttl}",
                )
                for table in REPLAY_TABLES
            ]
        if tag == "replay.then":
            if self.aost_error:
                raise self.aost_error
            return self.historical
        raise AssertionError(tag)


def test_application_adapter_uses_stored_hlc_in_one_fixed_aost_query():
    dal = FakeDAL()

    result = replay_case(dal, case_id=CASE_ID)

    assert result["tamper_check"]["match"] is True
    assert [call[2]["tag"] for call in dal.calls] == [
        "replay.now",
        "replay.retention",
        "replay.then",
    ]
    aost_sql, params, kwargs = dal.calls[2]
    assert aost_sql.count("AS OF SYSTEM TIME") == 1
    assert f"AS OF SYSTEM TIME {HLC}" in aost_sql
    assert "ca.tenant_id=%s AND ca.id=%s" in aost_sql
    assert params == (CASE_ID,)
    assert HLC not in kwargs["audit_sql_text"]
    assert "sha256:" not in kwargs["audit_sql_text"]
    assert all(HLC not in call[2]["audit_sql_text"] for call in dal.calls)


def test_historical_builder_rejects_injection_before_sql_construction():
    with pytest.raises(Exception):
        _historical_query(HLC + "; DROP TABLE cases")


def test_wrong_tenant_or_unknown_case_is_not_found_without_aost_fallback():
    dal = FakeDAL(current=[])
    with pytest.raises(ReplayNotFoundError):
        replay_case(dal, case_id=CASE_ID)
    assert [call[2]["tag"] for call in dal.calls] == ["replay.now"]


def test_unsealed_case_is_rejected_without_retention_or_aost_read():
    current = replay_rows(state="ANALYZED")
    current[0]["sealed_txn_ts"] = None
    dal = FakeDAL(current=_tuples(current))
    with pytest.raises(ReplayNotSealedError):
        replay_case(dal, case_id=CASE_ID)
    assert [call[2]["tag"] for call in dal.calls] == ["replay.now"]


def test_malformed_stored_hlc_never_reaches_aost_query():
    current = replay_rows(state="CONTESTED")
    current[0]["sealed_txn_ts"] = HLC + " OR true"
    dal = FakeDAL(current=_tuples(current))
    with pytest.raises(ReplayUnavailableError):
        replay_case(dal, case_id=CASE_ID)
    assert [call[2]["tag"] for call in dal.calls] == ["replay.now"]


def test_misconfigured_retention_fails_closed_before_aost():
    dal = FakeDAL(retention_ttl=4_500)
    with pytest.raises(ReplayUnavailableError, match="90 days"):
        replay_case(dal, case_id=CASE_ID)
    assert [call[2]["tag"] for call in dal.calls] == ["replay.now", "replay.retention"]


@pytest.mark.parametrize(
    "error", [RuntimeError("history expired"), RuntimeError("database offline")]
)
def test_expired_or_unavailable_aost_has_no_current_read_fallback(error):
    dal = FakeDAL(aost_error=error)
    with pytest.raises(ReplayUnavailableError, match="historical"):
        replay_case(dal, case_id=CASE_ID)
    assert [call[2]["tag"] for call in dal.calls] == [
        "replay.now",
        "replay.retention",
        "replay.then",
    ]


def test_audit_log_failure_makes_replay_unavailable():
    class UnloggedDAL(FakeDAL):
        def execute(self, sql, params, **kwargs):
            rows = super().execute(sql, params, **kwargs)
            self.log_failure_count += 1
            return rows

    with pytest.raises(ReplayUnavailableError, match="audit-visible"):
        replay_case(UnloggedDAL(), case_id=CASE_ID)
