from __future__ import annotations

from uuid import UUID

import pytest

import scripts.gate3_prepare as gate3_prepare
from scripts.gate3_prepare import (
    Gate3PrepareError,
    _ensure_unsealed_adversarial_contest,
)
from src.platform.contest_workflow import ContestWorkflowError

TENANT_ID = "70000000-0000-4000-8000-000000000001"
CARRIER_ID = "70000000-0000-4000-8000-000000000002"
CASE_ID = "70000000-0000-4000-8000-000000000003"
CONTEST_ID = "70000000-0000-4000-8000-000000000004"


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        assert not self.connection.in_transaction
        self.connection.in_transaction = True
        return self

    def __exit__(self, exc_type, *_exc_info):
        self.connection.transaction_commits.append(exc_type is None)
        self.connection.in_transaction = False
        return False


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=()):
        assert self.connection.in_transaction
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if normalized.startswith("SELECT case_id, carrier_id"):
            self.row = self.connection.fixture_row
        else:
            self.row = None

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, fixture_row=None):
        self.fixture_row = fixture_row or (UUID(CASE_ID), UUID(CARRIER_ID))
        self.executed = []
        self.transaction_commits = []
        self.in_transaction = False

    def transaction(self):
        return _Transaction(self)

    def cursor(self):
        return _Cursor(self)


def _reject_unsealed(*_args, **_kwargs):
    raise ContestWorkflowError("case_not_sealed_for_contest")


def _ensure(connection):
    return _ensure_unsealed_adversarial_contest(
        connection,
        tenant_id=TENANT_ID,
        carrier_id=CARRIER_ID,
        case_id=CASE_ID,
        contest_id=CONTEST_ID,
    )


def test_unsealed_fixture_is_idempotent_and_audited_atomically(monkeypatch):
    monkeypatch.setattr(gate3_prepare, "record_later_contest", _reject_unsealed)
    connection = _Connection()

    assert _ensure(connection) == (True, True)
    assert _ensure(connection) == (True, True)

    statements = [sql for sql, _ in connection.executed]
    assert connection.transaction_commits == [True, True]
    assert sum("ON CONFLICT (tenant_id, id) DO NOTHING" in sql for sql in statements) == 2
    assert sum("'gate3.negative-fixture'" in sql for sql in statements) == 2


def test_conflicting_fixture_binding_rolls_back_before_audit(monkeypatch):
    monkeypatch.setattr(gate3_prepare, "record_later_contest", _reject_unsealed)
    connection = _Connection(
        fixture_row=(
            UUID("70000000-0000-4000-8000-000000000099"),
            UUID(CARRIER_ID),
        )
    )

    with pytest.raises(Gate3PrepareError, match="fixture_not_bound"):
        _ensure(connection)

    statements = [sql for sql, _ in connection.executed]
    assert connection.transaction_commits == [False]
    assert not any("INSERT INTO query_log" in sql for sql in statements)
