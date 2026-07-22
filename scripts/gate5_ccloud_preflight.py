"""Bounded ccloud control-plane preflight for the Gate 5 deployment agent.

This is not a database or MCP health check. It proves only that the ccloud
control plane authenticated, the one server-selected cluster is discoverable,
and its structured state/plan satisfy the deployment guard. Raw identifiers
and CLI error text are never emitted.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PreflightReceipt:
    tool: str
    operation: str
    structured_json: bool
    control_plane_authenticated: bool
    target_count: int
    target_operational: bool
    target_plan_allowed: bool
    passed: bool
    error_code: str | None = None


def _json_value(output: str) -> Any:
    candidate = output.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        starts = [index for index in (candidate.find("["), candidate.find("{")) if index >= 0]
        if not starts:
            raise ValueError("structured output missing") from None
        return json.loads(candidate[min(starts) :])


def _clusters(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("clusters", "items", "data"):
            items = value.get(key)
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, dict)]
    raise ValueError("cluster list shape unsupported")


def run_preflight(
    target_cluster_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> PreflightReceipt:
    if not target_cluster_id.strip():
        return PreflightReceipt(
            "ccloud CLI", "cluster list", True, False, 0, False, False, False,
            "target_not_configured",
        )
    try:
        completed = runner(
            ["ccloud", "cluster", "list", "-o", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return PreflightReceipt(
            "ccloud CLI", "cluster list", True, False, 0, False, False, False,
            "control_plane_unavailable",
        )
    if completed.returncode != 0:
        return PreflightReceipt(
            "ccloud CLI", "cluster list", True, False, 0, False, False, False,
            "control_plane_rejected",
        )
    try:
        clusters = _clusters(_json_value(completed.stdout))
    except (ValueError, json.JSONDecodeError):
        return PreflightReceipt(
            "ccloud CLI", "cluster list", False, True, 0, False, False, False,
            "structured_output_invalid",
        )
    matches = [item for item in clusters if str(item.get("id", "")) == target_cluster_id]
    operational = len(matches) == 1 and matches[0].get("state") == "CREATED"
    plan_allowed = len(matches) == 1 and str(matches[0].get("plan", "")).upper() == "BASIC"
    passed = len(matches) == 1 and operational and plan_allowed
    return PreflightReceipt(
        tool="ccloud CLI",
        operation="cluster list",
        structured_json=True,
        control_plane_authenticated=True,
        target_count=len(matches),
        target_operational=operational,
        target_plan_allowed=plan_allowed,
        passed=passed,
        error_code=None if passed else "target_not_ready",
    )


def main() -> int:
    receipt = run_preflight(os.environ.get("TALLY_CCLOUD_CLUSTER_ID", ""))
    print(json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")))
    return 0 if receipt.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
