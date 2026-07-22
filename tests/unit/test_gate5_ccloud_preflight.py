from __future__ import annotations

import json
import subprocess

from scripts.gate5_ccloud_preflight import run_preflight

TARGET = "synthetic-cluster-control-plane-id"


def _runner(value, *, returncode=0):
    def run(args, **kwargs):
        assert args == ["ccloud", "cluster", "list", "-o", "json"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(args, returncode, stdout=value, stderr="private error")

    return run


def test_structured_operational_basic_cluster_passes_without_emitting_identity():
    value = json.dumps([{"id": TARGET, "state": "CREATED", "plan": "BASIC"}])
    receipt = run_preflight(TARGET, runner=_runner(value))
    assert receipt.passed is True
    assert receipt.target_count == 1
    assert TARGET not in json.dumps(receipt.__dict__)


def test_noise_before_structured_json_is_tolerated():
    value = "update notice\n" + json.dumps(
        {"clusters": [{"id": TARGET, "state": "CREATED", "plan": "BASIC"}]}
    )
    assert run_preflight(TARGET, runner=_runner(value)).passed is True


def test_cli_failure_fails_closed_without_returning_raw_error():
    receipt = run_preflight(TARGET, runner=_runner("", returncode=7))
    assert receipt.passed is False
    assert receipt.error_code == "control_plane_rejected"
    assert "private" not in json.dumps(receipt.__dict__)


def test_wrong_missing_or_non_basic_target_fails_closed():
    clusters = [
        {"id": "different", "state": "CREATED", "plan": "BASIC"},
        {"id": TARGET, "state": "CREATING", "plan": "STANDARD"},
    ]
    receipt = run_preflight(TARGET, runner=_runner(json.dumps(clusters)))
    assert receipt.passed is False
    assert receipt.target_operational is False
    assert receipt.target_plan_allowed is False


def test_unstructured_output_is_not_treated_as_readiness():
    receipt = run_preflight(TARGET, runner=_runner("human text only"))
    assert receipt.passed is False
    assert receipt.structured_json is False
    assert receipt.error_code == "structured_output_invalid"
