from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.platform.contest_workflow import ContestWorkflowError, record_later_contest

TENANT_ID = "60000000-0000-4000-8000-000000000001"
CASE_ID = "60000000-0000-4000-8000-000000000002"
CONTEST_ID = "60000000-0000-4000-8000-000000000003"
CARRIER_ID = UUID("60000000-0000-4000-8000-000000000004")
RECEIVED = "2026-07-21T19:00:00Z"


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if normalized.startswith("SELECT carrier_id, state"):
            self.row = self.connection.case_row
        elif normalized.startswith("SELECT case_id, carrier_id"):
            self.row = self.connection.contest_row
        else:
            self.row = None

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, *, state="FILED", contest_row=None):
        self.case_row = (
            CARRIER_ID,
            state,
            "sha256:" + "a" * 64,
            {"manifest_version": 1},
            UUID("60000000-0000-4000-8000-000000000005"),
            datetime(2026, 7, 20, tzinfo=UTC),
            Decimal("1.0"),
        )
        self.contest_row = contest_row
        self.executed = []

    def cursor(self):
        return Cursor(self)


class DAL:
    def __init__(self, connection):
        self.connection = connection
        self.tenant = SimpleNamespace(tenant_id=TENANT_ID, actor="gate3-runtime")

    def run_with_retry(self, fn):
        return fn(self.connection)


def _record(dal):
    return record_later_contest(
        dal,
        contest_id=CONTEST_ID,
        case_id=CASE_ID,
        received_at=RECEIVED,
        sender="Synthetic Carrier Review Desk",
        claim_text="Synthetic later contest of the recorded rate.",
        claimed_rate="350.00",
    )


def test_records_contest_and_transitions_sealed_case_atomically():
    connection = Connection()
    result = _record(DAL(connection))
    statements = [sql for sql, _ in connection.executed]
    assert result == {
        "contest_id": CONTEST_ID,
        "case_id": CASE_ID,
        "state": "CONTESTED",
        "already_recorded": False,
    }
    assert any(sql.startswith("INSERT INTO contests") for sql in statements)
    assert any(sql.startswith("UPDATE cases SET state='CONTESTED'") for sql in statements)
    assert any("'contest.record'" in sql for sql in statements)


def test_replay_is_idempotent_and_does_not_insert_second_contest():
    existing = (
        UUID(CASE_ID),
        CARRIER_ID,
        datetime(2026, 7, 21, 19, tzinfo=UTC),
        "Synthetic Carrier Review Desk",
        "Synthetic later contest of the recorded rate.",
        Decimal("350.00"),
    )
    connection = Connection(state="CONTESTED", contest_row=existing)
    result = _record(DAL(connection))
    statements = [sql for sql, _ in connection.executed]
    assert result["already_recorded"] is True
    assert not any(sql.startswith("INSERT INTO contests") for sql in statements)
    assert not any(sql.startswith("UPDATE cases") for sql in statements)


def test_unsealed_case_is_rejected_before_contest_insert():
    connection = Connection(state="ANALYZED")
    with pytest.raises(ContestWorkflowError, match="not_sealed"):
        _record(DAL(connection))
    assert not any(sql.startswith("INSERT INTO contests") for sql, _ in connection.executed)


def test_conflicting_idempotency_key_is_rejected():
    existing = (
        UUID(CASE_ID),
        CARRIER_ID,
        datetime(2026, 7, 21, 19, tzinfo=UTC),
        "Different sender",
        "Synthetic later contest of the recorded rate.",
        Decimal("350.00"),
    )
    connection = Connection(state="CONTESTED", contest_row=existing)
    with pytest.raises(ContestWorkflowError, match="contest_id_conflict"):
        _record(DAL(connection))


@pytest.mark.parametrize("claimed_rate", ["not-a-number", "NaN", "Infinity", "-1"])
def test_invalid_claimed_rate_is_rejected(claimed_rate):
    dal = DAL(Connection())
    with pytest.raises(ContestWorkflowError, match="claimed_rate_invalid|contest_input_invalid"):
        record_later_contest(
            dal,
            contest_id=CONTEST_ID,
            case_id=CASE_ID,
            received_at=RECEIVED,
            sender="Synthetic Carrier Review Desk",
            claim_text="Synthetic later contest of the recorded rate.",
            claimed_rate=claimed_rate,
        )
