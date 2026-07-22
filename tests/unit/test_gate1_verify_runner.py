from __future__ import annotations

import json
import stat

import pytest

from scripts import gate1_verify


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return None


def _args(path):
    return [
        "--dsn-env-var",
        "TEST_GATE1_DSN",
        "--aws-profile",
        "fixture-profile",
        "--tenant-id",
        "private-tenant-id",
        "--case-id",
        "private-case-id",
        "--private-output",
        str(path),
    ]


def test_verifier_command_writes_private_detail_and_aggregate_stdout(
    monkeypatch, tmp_path, capsys
):
    report = {
        "passed": True,
        "computed_manifest_hash": "sha256:private-hash",
        "reasons": [],
        "checks": [
            {"name": "manifest_hash", "passed": True},
            {"name": "source_hash", "passed": True},
        ],
    }
    calls = {}

    class Session:
        def __init__(self, *, profile_name):
            calls["profile"] = profile_name

        def client(self, service):
            calls["service"] = service
            return "s3-client"

    def verify(dal, s3_client, *, case_id):
        calls.update(
            tenant_id=dal.tenant.tenant_id,
            s3_client=s3_client,
            case_id=case_id,
        )
        return report

    monkeypatch.setenv("TEST_GATE1_DSN", "postgresql://private-connection")
    monkeypatch.setattr(gate1_verify.boto3, "Session", Session)
    monkeypatch.setattr(gate1_verify, "connect", lambda dsn: _Connection())
    monkeypatch.setattr(gate1_verify, "verify_case_receipt", verify)
    output = tmp_path / "private" / "verifier.json"

    assert gate1_verify.main(_args(output)) == 0

    public = json.loads(capsys.readouterr().out)
    private = json.loads(output.read_text())
    assert public == {
        "checks_passed": 2,
        "checks_total": 2,
        "passed": True,
        "private_details_written": True,
        "reason_count": 0,
    }
    assert "private-hash" not in json.dumps(public)
    assert "private-case-id" not in json.dumps(public)
    assert private["computed_manifest_hash"] == "sha256:private-hash"
    assert calls == {
        "profile": "fixture-profile",
        "service": "s3",
        "tenant_id": "private-tenant-id",
        "s3_client": "s3-client",
        "case_id": "private-case-id",
    }
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_missing_dsn_fails_before_aws(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_GATE1_DSN", raising=False)
    monkeypatch.setattr(
        gate1_verify.boto3,
        "Session",
        lambda **_kwargs: pytest.fail("AWS must not be used without the DSN"),
    )

    with pytest.raises(RuntimeError, match="TEST_GATE1_DSN"):
        gate1_verify.main(_args(tmp_path / "private.json"))
