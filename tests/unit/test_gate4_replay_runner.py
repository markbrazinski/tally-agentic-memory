from __future__ import annotations

import pytest

from scripts import gate4_replay
from scripts.gate4_replay import Gate4Inputs, Gate4ReplayError


def test_inputs_require_private_environment(monkeypatch):
    for name in (
        "TALLY_GATE4_CRDB_DSN",
        "TALLY_GATE4_TENANT_ID",
        "TALLY_GATE4_HERO_CASE_ID",
        "TALLY_GATE4_WRONG_TENANT_ID",
        "TALLY_GATE4_UNKNOWN_CASE_ID",
        "TALLY_GATE4_UNSEALED_CASE_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(Gate4ReplayError, match="missing_private_gate4_inputs"):
        Gate4Inputs.from_env()


def test_inputs_validate_uuid_without_echoing_private_value(monkeypatch):
    values = {
        "TALLY_GATE4_CRDB_DSN": "postgresql://private.example/ignored",
        "TALLY_GATE4_TENANT_ID": "74000000-0000-4000-8000-000000000001",
        "TALLY_GATE4_HERO_CASE_ID": "not-a-uuid",
        "TALLY_GATE4_WRONG_TENANT_ID": "74000000-0000-4000-8000-000000000002",
        "TALLY_GATE4_UNKNOWN_CASE_ID": "74000000-0000-4000-8000-000000000003",
        "TALLY_GATE4_UNSEALED_CASE_ID": "74000000-0000-4000-8000-000000000004",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(Gate4ReplayError) as error:
        Gate4Inputs.from_env()

    assert str(error.value) == "invalid_uuid_input:hero_case_id"
    assert "not-a-uuid" not in str(error.value)


def test_source_contains_no_public_private_value_prints():
    source = __import__("inspect").getsource(__import__("scripts.gate4_replay", fromlist=["*"]))

    assert "print(inputs" not in source
    assert "print(private" not in source
    assert "print(dsn" not in source


def test_cli_redacts_unexpected_private_exception_details(monkeypatch, capsys):
    monkeypatch.setattr(
        gate4_replay.Gate4Inputs,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(
        gate4_replay,
        "execute",
        lambda inputs: (_ for _ in ()).throw(RuntimeError("PRIVATE_DB_HOST_SENTINEL")),
    )

    assert gate4_replay.main() == 2
    captured = capsys.readouterr()
    assert captured.out.strip() == "gate4_replay_error=RuntimeError"
    assert captured.err == ""
    assert "PRIVATE_DB_HOST_SENTINEL" not in captured.out
