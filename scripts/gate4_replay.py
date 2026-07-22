"""Execute the Gate 4 replay route against private recovery state.

Detailed route data stays in ignored mode-0600 storage. Standard output is
limited to booleans and counts suitable for the sanitized Gate Report.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

import src.platform.app as app_module
from src.external.dal import DAL, Tenant
from src.platform.auth import AuthedActor
from src.platform.private_artifacts import write_private_json

DEFAULT_OUTPUT = Path("runtime-artifacts/gate-4/executed-replay.private.json")
REPLAY_TAGS = ("replay.now", "replay.retention", "replay.then")


class Gate4ReplayError(RuntimeError):
    """Public-safe runner error; messages never include private values."""


@dataclass(frozen=True)
class Gate4Inputs:
    dsn: str
    tenant_id: str
    hero_case_id: str
    wrong_tenant_id: str
    unknown_case_id: str
    unsealed_case_id: str
    private_output: Path

    @classmethod
    def from_env(cls) -> "Gate4Inputs":
        names = {
            "dsn": "TALLY_GATE4_CRDB_DSN",
            "tenant_id": "TALLY_GATE4_TENANT_ID",
            "hero_case_id": "TALLY_GATE4_HERO_CASE_ID",
            "wrong_tenant_id": "TALLY_GATE4_WRONG_TENANT_ID",
            "unknown_case_id": "TALLY_GATE4_UNKNOWN_CASE_ID",
            "unsealed_case_id": "TALLY_GATE4_UNSEALED_CASE_ID",
        }
        values = {field: os.environ.get(name, "") for field, name in names.items()}
        if any(not value for value in values.values()):
            raise Gate4ReplayError("missing_private_gate4_inputs")
        for field in (
            "tenant_id",
            "hero_case_id",
            "wrong_tenant_id",
            "unknown_case_id",
            "unsealed_case_id",
        ):
            try:
                values[field] = str(UUID(values[field]))
            except ValueError as exc:
                raise Gate4ReplayError(f"invalid_uuid_input:{field}") from exc
        return cls(
            **values,
            private_output=Path(
                os.environ.get("TALLY_GATE4_PRIVATE_OUTPUT", str(DEFAULT_OUTPUT))
            ),
        )


def _dal(inputs: Gate4Inputs, tenant_id: str, *, actor: str) -> DAL:
    return DAL.connect(Tenant(tenant_id=tenant_id, actor=actor), inputs.dsn)


def _audit_counts(inputs: Gate4Inputs) -> dict[str, int]:
    with _dal(inputs, inputs.tenant_id, actor="gate4-audit-verifier") as dal:
        rows = dal.execute(
            """
            SELECT tag, count(*)::INT
            FROM query_log
            WHERE tenant_id=%s AND tag IN ('replay.now', 'replay.retention', 'replay.then')
            GROUP BY tag;
            """,
            (),
            tag="gate4.audit.snapshot",
        )
    return {str(tag): int(count) for tag, count in rows}


def _route_get(inputs: Gate4Inputs, *, tenant_id: str, case_id: str):
    def factory() -> DAL:
        return _dal(inputs, tenant_id, actor="gate4-route-verifier")

    with patch.object(app_module, "_dal", factory):
        with TestClient(app_module.app) as client:
            return client.get(f"/cases/{case_id}/replay")


def _outside_retention_rejected(inputs: Gate4Inputs) -> tuple[bool, bool, str | None]:
    try:
        with _dal(inputs, inputs.tenant_id, actor="gate4-retention-probe") as dal:
            dal.execute(
                """
                SELECT id
                FROM cases AS OF SYSTEM TIME '-91d'
                WHERE tenant_id=%s
                LIMIT 1;
                """,
                (),
                tag="replay.outside_retention",
                kind="temporal_replay",
                render_source="as_of_system_time",
                audit_sql_text="fixed outside-retention rejection probe; interval redacted",
            )
    except Exception as exc:  # noqa: BLE001 - rejection type is evidence, text stays private
        return True, bool(getattr(exc, "sqlstate", None)), type(exc).__name__
    return False, False, None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def execute(inputs: Gate4Inputs) -> tuple[dict[str, bool | int], dict[str, Any]]:
    before = _audit_counts(inputs)
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: AuthedActor(
        user_id="74000000-0000-4000-8000-000000000099",
        display_name="gate4-route-verifier",
    )
    try:
        hero = _route_get(
            inputs,
            tenant_id=inputs.tenant_id,
            case_id=inputs.hero_case_id,
        )
        wrong_tenant = _route_get(
            inputs,
            tenant_id=inputs.wrong_tenant_id,
            case_id=inputs.hero_case_id,
        )
        unknown = _route_get(
            inputs,
            tenant_id=inputs.tenant_id,
            case_id=inputs.unknown_case_id,
        )
        unsealed = _route_get(
            inputs,
            tenant_id=inputs.tenant_id,
            case_id=inputs.unsealed_case_id,
        )
    finally:
        app_module.app.dependency_overrides.clear()
    after = _audit_counts(inputs)
    outside_rejected, outside_sqlstate, outside_error_class = (
        _outside_retention_rejected(inputs)
    )

    body = _mapping(hero.json()) if hero.status_code == 200 else {}
    then = _mapping(body.get("then"))
    now = _mapping(body.get("now"))
    retention = _mapping(body.get("retention"))
    tamper = _mapping(body.get("tamper_check"))
    queries = body.get("queries") if isinstance(body.get("queries"), list) else []
    audit_increments = {
        tag: after.get(tag, 0) - before.get(tag, 0) for tag in REPLAY_TAGS
    }
    summary: dict[str, bool | int] = {
        "route_passed": hero.status_code == 200,
        "stored_timestamp_replay": bool(then.get("as_of"))
        and then.get("source") == "AS OF SYSTEM TIME",
        "historical_state_filed": then.get("state") == "FILED",
        "current_state_contested": now.get("state") == "CONTESTED",
        "meaningful_state_difference": then.get("state") != now.get("state"),
        "receipt_unchanged": tamper.get("match") is True,
        "retention_90_days": retention.get("ttl_seconds") == 7_776_000
        and retention.get("ttl_days") == 90,
        "target_timestamp_queryable": retention.get("target_queryable") is True,
        "approved_retention_language_present": retention.get("language")
        == (
            "Versioned S3 retains the dated source artifact. Within CockroachDB’s "
            "configured MVCC window, Tally can also replay the transactional case "
            "state at filing."
        ),
        "wrong_tenant_not_found": wrong_tenant.status_code == 404,
        "unknown_not_found": unknown.status_code == 404,
        "unsealed_rejected": unsealed.status_code == 409,
        "outside_retention_rejected": outside_rejected,
        "outside_retention_sqlstate_present": outside_sqlstate,
        "executed_query_lines": len(queries),
        "audit_tags_advanced": all(audit_increments[tag] >= 1 for tag in REPLAY_TAGS),
    }
    required_booleans = [value for value in summary.values() if isinstance(value, bool)]
    summary["passed"] = all(required_booleans) and len(queries) == 2
    private = {
        "classification": "private Gate 4 recovery evidence",
        "hero_status": hero.status_code,
        "hero_response": body,
        "negative_statuses": {
            "wrong_tenant": wrong_tenant.status_code,
            "unknown": unknown.status_code,
            "unsealed": unsealed.status_code,
        },
        "audit_increments": audit_increments,
        "outside_retention": {
            "rejected": outside_rejected,
            "sqlstate_present": outside_sqlstate,
            "error_class": outside_error_class,
        },
        "summary": summary,
    }
    return summary, private


def main() -> int:
    try:
        inputs = Gate4Inputs.from_env()
        summary, private = execute(inputs)
        write_private_json(inputs.private_output, private)
    except Exception as exc:  # noqa: BLE001 - CLI boundary must redact all private failures
        print(f"gate4_replay_error={type(exc).__name__}")
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
