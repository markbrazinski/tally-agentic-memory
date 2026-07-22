"""Unit tests for the redacted, pre-approval Gate 1 receipt runner."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import gate1_receipt
from src.external.versioned_source import RetainedObject

REQUIRED_ARGS = [
    "--bucket",
    "fixture-receipts",
    "--tariff-key",
    "synthetic/tariff.txt",
    "--tariff-version-id",
    "example-tariff-version",
    "--invoice-key",
    "synthetic/invoice.json",
    "--invoice-version-id",
    "example-invoice-version",
    "--dispute-date",
    "2026-07-20",
]

EXPECTED_COUNTS = {
    "tariff_snapshots": 1,
    "tariff_clauses": 1,
    "invoices": 1,
    "clerk_runs": 1,
    "findings": 1,
    "cases": 1,
    "case_evidence": 1,
    "ledger_events": 0,
}


@pytest.mark.parametrize(
    "required_option",
    (
        "--tariff-key",
        "--tariff-version-id",
        "--invoice-key",
        "--invoice-version-id",
        "--dispute-date",
    ),
)
def test_parse_args_requires_exact_source_refs_and_dispute_date(required_option):
    args = list(REQUIRED_ARGS)
    option_index = args.index(required_option)
    del args[option_index : option_index + 2]

    with pytest.raises(SystemExit) as exc_info:
        gate1_receipt._parse_args(args)

    assert exc_info.value.code == 2


def test_main_missing_named_dsn_fails_before_aws(monkeypatch, tmp_path):
    monkeypatch.delenv("MISSING_GATE1_DSN", raising=False)
    monkeypatch.setattr(
        gate1_receipt.boto3,
        "client",
        lambda *_args, **_kwargs: pytest.fail("AWS must not be called without a DSN"),
    )
    monkeypatch.setattr(
        gate1_receipt,
        "run_seed",
        lambda **_kwargs: pytest.fail("database seed must not run without a DSN"),
    )

    with pytest.raises(RuntimeError, match="MISSING_GATE1_DSN"):
        gate1_receipt.main(
            [
                *REQUIRED_ARGS,
                "--dsn-env-var",
                "MISSING_GATE1_DSN",
                "--private-output",
                str(tmp_path / "private.json"),
            ]
        )


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return None


def _patch_run(
    monkeypatch,
    *,
    second_counts_match: bool = True,
    overcharge: bool = True,
    approval_state: str = "NOT_PRESSED",
    case_state: str = "ANALYZED",
    sealed_artifacts_present: bool = False,
):
    tenant_id = "example-tenant-sensitive"
    carrier_id = "example-carrier-sensitive"
    tariff = RetainedObject(
        bucket="fixture-receipts",
        key="synthetic/tariff.txt",
        version_id="example-tariff-version",
        body=b"Synthetic rate USD 100 per day.",
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    invoice = RetainedObject(
        bucket="fixture-receipts",
        key="synthetic/invoice.json",
        version_id="example-invoice-version",
        body=b'{"synthetic": true}',
        observed_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    retained_by_key = {tariff.key: tariff, invoice.key: invoice}
    exact_reads: list[dict] = []
    extraction_calls: list[str] = []
    verification_calls = []
    parse_calls = []
    persist_calls = []
    filing_calls = []
    count_calls = []

    class FakeVersionedSource:
        def __init__(self, client):
            assert client == "fake-s3-client"

        def get_exact(self, **kwargs):
            exact_reads.append(kwargs)
            return retained_by_key[kwargs["key"]]

    class FakeExtractor:
        def extract(self, text):
            extraction_calls.append(text)
            return SimpleNamespace(rate_amount="100.00")

    verification = SimpleNamespace(
        eligible=True,
        reasons=(),
        source_sha256="example-source-hash-sensitive",
    )
    stored = SimpleNamespace(
        snapshot_id="example-snapshot-sensitive",
        clause_id="example-clause-sensitive",
        invoice_id="example-invoice-sensitive",
        clerk_run_id="example-clerk-sensitive",
    )
    filed_first = {
        "filed": overcharge,
        "already_filed": False,
        "calculation": {
            "recorded_rate": "100.00",
            "claimed_rate": "130.00" if overcharge else "100.00",
            "charge_days": 3,
            "overcharge": "90.00" if overcharge else "0.00",
            "recommendation": "FILE" if overcharge else "NO_ACTION",
        },
    }
    if overcharge:
        filed_first.update(
            {
                "case_id": "example-case-sensitive",
                "finding_id": "example-finding-sensitive",
            }
        )
    filed_second = {**filed_first, "already_filed": True}

    def fake_verify(*args):
        verification_calls.append(args)
        return verification

    def fake_parse(body):
        parse_calls.append(body)
        return SimpleNamespace(invoice_no="SYNTHETIC-001")

    def fake_persist(*_args, **_kwargs):
        persist_calls.append(True)
        return stored

    def fake_file(*_args, **_kwargs):
        filing_calls.append(True)
        return filed_first if len(filing_calls) == 1 else filed_second

    def fake_counts(*_args, **_kwargs):
        count_calls.append(True)
        counts = dict(EXPECTED_COUNTS)
        if approval_state == "APPROVED" and case_state == "FILED":
            counts["ledger_events"] = 1
        if len(count_calls) == 2 and not second_counts_match:
            counts["case_evidence"] = 2
        return counts

    monkeypatch.setenv("TEST_GATE1_DSN", "postgresql://example-sensitive-connection")
    monkeypatch.setattr(gate1_receipt, "run_seed", lambda *, dsn: tenant_id)
    monkeypatch.setattr(gate1_receipt.boto3, "client", lambda service: "fake-s3-client")
    monkeypatch.setattr(gate1_receipt, "S3VersionedSource", FakeVersionedSource)
    monkeypatch.setattr(gate1_receipt, "BedrockTariffExtractor", FakeExtractor)
    monkeypatch.setattr(gate1_receipt, "verify_tariff_extraction", fake_verify)
    monkeypatch.setattr(gate1_receipt, "parse_invoice_claim", fake_parse)
    monkeypatch.setattr(gate1_receipt, "connect", lambda dsn: _Connection())
    monkeypatch.setattr(gate1_receipt, "_carrier_id", lambda *_args: carrier_id)
    monkeypatch.setattr(gate1_receipt, "persist_verified_inputs", fake_persist)
    monkeypatch.setattr(gate1_receipt, "file_gate1_case", fake_file)
    monkeypatch.setattr(gate1_receipt, "_counts", fake_counts)
    monkeypatch.setattr(
        gate1_receipt,
        "_database_evidence",
        lambda *_args: {
            "verification_status": "VERIFIED",
            "clause_hash_present": True,
            "source_version_present": True,
            "source_hash_present": True,
            "human_approval_state": approval_state,
            "case_state": case_state,
            "evidence_hash_present": sealed_artifacts_present,
            "manifest_version": 1 if sealed_artifacts_present else None,
            "evidence_manifest_present": sealed_artifacts_present,
            "sealed_txn_ts_present": sealed_artifacts_present,
        },
    )
    return {
        "exact_reads": exact_reads,
        "extraction_calls": extraction_calls,
        "verification_calls": verification_calls,
        "parse_calls": parse_calls,
        "persist_calls": persist_calls,
        "filing_calls": filing_calls,
        "count_calls": count_calls,
        "private_values": (
            tenant_id,
            carrier_id,
            tariff.key,
            tariff.version_id,
            invoice.key,
            invoice.version_id,
            verification.source_sha256,
            stored.snapshot_id,
            stored.case_id if hasattr(stored, "case_id") else filed_first.get("case_id", ""),
        ),
    }


def _runner_args(private_output):
    return [
        *REQUIRED_ARGS,
        "--dsn-env-var",
        "TEST_GATE1_DSN",
        "--private-output",
        str(private_output),
    ]


def test_main_runs_full_attempt_twice_without_approval_and_redacts_stdout(
    monkeypatch, tmp_path, capsys
):
    calls = _patch_run(monkeypatch)
    private_output = tmp_path / "gate-1-private" / "receipt.json"

    assert gate1_receipt.main(_runner_args(private_output)) == 0

    public_result = json.loads(capsys.readouterr().out)
    private_result = json.loads(private_output.read_text())
    assert public_result == {
        "approval_or_seal_performed_by_runner": False,
        "expected_counts_met": True,
        "full_attempts_executed": 2,
        "human_approval_required": True,
        "idempotent_rerun": True,
        "workflow_replay_passed": True,
        "private_details_written": True,
        "receipt_state": "PREPARED",
        "receipt_is_unsealed": True,
        "row_counts": EXPECTED_COUNTS,
        "stage": "prepared_for_human_approval",
        "tariff_extraction_eligible": True,
    }
    assert len(calls["exact_reads"]) == 4
    assert [read["key"] for read in calls["exact_reads"]] == [
        "synthetic/tariff.txt",
        "synthetic/invoice.json",
        "synthetic/tariff.txt",
        "synthetic/invoice.json",
    ]
    assert len(calls["extraction_calls"]) == 2
    assert len(calls["verification_calls"]) == 2
    assert len(calls["parse_calls"]) == 2
    assert len(calls["persist_calls"]) == 2
    assert len(calls["filing_calls"]) == 2
    assert len(calls["count_calls"]) == 2

    public_text = json.dumps(public_result, sort_keys=True)
    assert all(value not in public_text for value in calls["private_values"] if value)
    assert private_result["tariff"]["version_id"] == "example-tariff-version"
    assert private_result["invoice"]["version_id"] == "example-invoice-version"
    assert private_result["approval_or_seal_performed_by_runner"] is False
    assert private_result["counts_after_first"] == private_result["counts_after_second"]
    assert stat.S_IMODE(private_output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_output.stat().st_mode) == 0o600


def test_main_treats_no_overcharge_as_hard_failure(monkeypatch, tmp_path, capsys):
    calls = _patch_run(monkeypatch, overcharge=False)
    private_output = tmp_path / "private.json"

    assert gate1_receipt.main(_runner_args(private_output)) == 1

    assert json.loads(capsys.readouterr().out) == {
        "workflow_replay_passed": False,
        "reason_count": 0,
        "stage": "no_overcharge",
    }
    assert len(calls["filing_calls"]) == 1
    assert len(calls["count_calls"]) == 0
    private_result = json.loads(private_output.read_text())
    assert private_result["stage"] == "no_overcharge"
    assert private_result["calculation"]["overcharge"] == "0.00"
    assert stat.S_IMODE(private_output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(private_output.stat().st_mode) == 0o600


def test_main_fails_gate_when_rerun_row_counts_change(monkeypatch, tmp_path, capsys):
    calls = _patch_run(monkeypatch, second_counts_match=False)

    assert gate1_receipt.main(_runner_args(tmp_path / "private" / "receipt.json")) == 1

    public_result = json.loads(capsys.readouterr().out)
    assert public_result["workflow_replay_passed"] is False
    assert public_result["idempotent_rerun"] is False
    assert public_result["expected_counts_met"] is False
    assert len(calls["count_calls"]) == 2


def test_main_revalidates_existing_human_sealed_receipt(monkeypatch, tmp_path, capsys):
    calls = _patch_run(
        monkeypatch,
        approval_state="APPROVED",
        case_state="FILED",
        sealed_artifacts_present=True,
    )

    assert gate1_receipt.main(_runner_args(tmp_path / "private" / "receipt.json")) == 0

    public_result = json.loads(capsys.readouterr().out)
    assert public_result["workflow_replay_passed"] is True
    assert public_result["stage"] == "human_sealed_receipt_revalidated"
    assert public_result["receipt_state"] == "SEALED"
    assert public_result["receipt_is_unsealed"] is False
    assert public_result["human_approval_required"] is False
    assert public_result["row_counts"]["ledger_events"] == 1
    assert len(calls["exact_reads"]) == 4
    assert len(calls["extraction_calls"]) == 2
    assert len(calls["persist_calls"]) == 2
    assert len(calls["filing_calls"]) == 2
    assert len(calls["count_calls"]) == 2


@pytest.mark.parametrize(
    ("approval_state", "case_state", "sealed_artifacts_present"),
    (
        ("APPROVED", "ANALYZED", True),
        ("NOT_PRESSED", "FILED", False),
        ("APPROVED", "FILED", False),
    ),
)
def test_main_rejects_incoherent_receipt_states(
    monkeypatch, tmp_path, capsys, approval_state, case_state, sealed_artifacts_present
):
    _patch_run(
        monkeypatch,
        approval_state=approval_state,
        case_state=case_state,
        sealed_artifacts_present=sealed_artifacts_present,
    )

    assert gate1_receipt.main(_runner_args(tmp_path / "private" / "receipt.json")) == 1

    public_result = json.loads(capsys.readouterr().out)
    assert public_result["workflow_replay_passed"] is False
    assert public_result["stage"] == "incoherent_receipt_state"
    assert public_result["receipt_state"] == "INCOHERENT"
    assert public_result["receipt_is_unsealed"] is False
    assert public_result["approval_or_seal_performed_by_runner"] is False


def test_counts_queries_every_gate1_receipt_table():
    executed_sql = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return None

        def execute(self, sql, params):
            executed_sql.append((" ".join(sql.split()), params))

        def fetchone(self):
            return (0,) if "ledger_events" in executed_sql[-1][0] else (1,)

    class Connection:
        def cursor(self):
            return Cursor()

    attempt = SimpleNamespace(
        tariff=SimpleNamespace(key="synthetic/tariff.txt", version_id="tariff-version"),
        invoice=SimpleNamespace(key="synthetic/invoice.json", version_id="invoice-version"),
        stored=SimpleNamespace(invoice_id="invoice-id"),
    )

    counts = gate1_receipt._counts(Connection(), "tenant-id", attempt)

    assert counts == EXPECTED_COUNTS
    statements = "\n".join(sql for sql, _params in executed_sql)
    for table in (
        "tariff_snapshots",
        "tariff_clauses",
        "invoices",
        "clerk_runs",
        "findings",
        "cases",
        "case_evidence",
        "ledger_events",
    ):
        assert table in statements
