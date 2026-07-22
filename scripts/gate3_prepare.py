"""Prepare bounded synthetic Gate 3 workflow inputs in the recovery database.

The hero contest is recorded through the normal transactional contest workflow.
The unsealed negative is created through the existing Clerk ``file_case`` path
and deliberately not approved.  Detailed identifiers stay in ignored private
storage; stdout contains booleans only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from scripts.gate3_mcp_retrieval import _load_gate1_seal
from src.external.bedrock_extract import CannedResponseExtractor, apply_anti_hallucination_gate
from src.external.dal import DAL, Tenant
from src.external.db import connect
from src.platform.clerk_pipeline import file_case, run_extraction_steps
from src.platform.contest_workflow import ContestWorkflowError, record_later_contest
from src.platform.private_artifacts import write_private_json

DEFAULT_DSN_PATH = Path("runtime-artifacts/gate-2/target-dsn.private")
DEFAULT_OUTPUT = Path("runtime-artifacts/gate-3/prepared-inputs.private.json")
NEGATIVE_FIXTURE_BODY = b"Demonstration data: Gate 3 unsealed negative fixture."


class Gate3PrepareError(RuntimeError):
    pass


def _reference_database_dsn(base_dsn: str, seal_path: Path) -> tuple[str, str]:
    seal = _load_gate1_seal(seal_path)
    with connect(base_dsn) as conn:
        databases = [str(row[0]) for row in conn.execute("SHOW DATABASES").fetchall()]
        matches = []
        for name in databases:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", name):
                continue
            quoted = name.replace('"', '""')
            try:
                table_exists = conn.execute(
                    f'SELECT count(*) FROM "{quoted}".information_schema.tables '
                    "WHERE table_schema='public' AND table_name='cases'"
                ).fetchone()[0]
                if (
                    table_exists
                    and conn.execute(
                        f'SELECT count(*) FROM "{quoted}".public.cases '
                        "WHERE tenant_id=%s AND id=%s",
                        (seal["tenant_id"], seal["case_id"]),
                    ).fetchone()[0]
                    == 1
                ):
                    matches.append(name)
            except Exception:  # noqa: BLE001 - inaccessible databases are not candidates
                continue
    if len(matches) != 1:
        raise Gate3PrepareError("reference_database_not_unique")
    parsed = urlsplit(base_dsn)
    resolved = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/" + quote(matches[0], safe=""),
            parsed.query,
            parsed.fragment,
        )
    )
    return resolved, matches[0]


def _load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate3PrepareError("existing_private_setup_unreadable") from exc
    if not isinstance(value, dict):
        raise Gate3PrepareError("existing_private_setup_invalid")
    return value


def _id(existing: dict, name: str) -> str:
    value = existing.get(name)
    return str(value) if value else str(uuid4())


def _ensure_unsealed_case(conn, *, tenant_id: str, carrier_id: str) -> str:
    fixture_hash = hashlib.sha256(NEGATIVE_FIXTURE_BODY).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM invoices WHERE tenant_id=%s AND sha256=%s;",
            (tenant_id, fixture_hash),
        )
        invoice = cur.fetchone()
        if invoice is None:
            cur.execute(
                """
                INSERT INTO invoices
                    (tenant_id, carrier_id, invoice_no, received_at, s3_key,
                     sha256, raw_text, amount, currency, status)
                VALUES (%s, %s, %s, now(), %s, %s, %s, 0, 'USD', 'RECEIVED')
                RETURNING id;
                """,
                (
                    tenant_id,
                    carrier_id,
                    "GATE3-UNSEALED-DEMONSTRATION",
                    "synthetic-negative:no-retained-source",
                    fixture_hash,
                    NEGATIVE_FIXTURE_BODY.decode("utf-8"),
                ),
            )
            invoice = cur.fetchone()
        invoice_id = str(invoice[0])
        cur.execute(
            "SELECT id, state FROM cases WHERE tenant_id=%s AND invoice_id=%s;",
            (tenant_id, invoice_id),
        )
        existing_case = cur.fetchone()
        if existing_case is not None:
            if existing_case[1] != "ANALYZED":
                raise Gate3PrepareError("negative_fixture_was_sealed")
            return str(existing_case[0])
        cur.execute(
            "SELECT id FROM clerk_runs WHERE tenant_id=%s AND invoice_id=%s LIMIT 1;",
            (tenant_id, invoice_id),
        )
        run = cur.fetchone()
        if run is None:
            cur.execute(
                """
                INSERT INTO clerk_runs (tenant_id, invoice_id, status)
                VALUES (%s, %s, 'QUEUED') RETURNING id;
                """,
                (tenant_id, invoice_id),
            )
            run = cur.fetchone()
    raw = CannedResponseExtractor().extract(NEGATIVE_FIXTURE_BODY.decode("utf-8"))
    extraction = apply_anti_hallucination_gate(raw, NEGATIVE_FIXTURE_BODY.decode("utf-8"))
    clerk_result = run_extraction_steps(
        extraction,
        billed_party_name=None,
        invoice_date_raw=None,
    )
    dal = DAL(conn, Tenant(tenant_id=tenant_id, actor="gate3-negative-fixture"))
    result = file_case(
        dal,
        invoice_id=invoice_id,
        clerk_run_id=str(run[0]),
        carrier_id=carrier_id,
        pin_date=datetime.now(UTC).date().isoformat(),
        amount=0.0,
        clerk_result=clerk_result,
    )
    return str(result["case_id"])


def _ensure_unsealed_adversarial_contest(
    conn,
    *,
    tenant_id: str,
    carrier_id: str,
    case_id: str,
    contest_id: str,
) -> tuple[bool, bool]:
    """Bind a contest to an unsealed case only as an adversarial negative fixture.

    The normal workflow must reject this state first.  A direct synthetic row
    is then inserted so the MCP retrieval's seal predicates—not merely an
    absent-contest join—reject the unsealed case.
    """
    workflow_rejected = False
    try:
        record_later_contest(
            DAL(conn, Tenant(tenant_id=tenant_id, actor="gate3-negative-probe")),
            contest_id=contest_id,
            case_id=case_id,
            received_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            sender="Synthetic Adversarial Contest Fixture",
            claim_text="Demonstration data: contest against an unsealed case.",
            claimed_rate="0",
        )
    except ContestWorkflowError as exc:
        workflow_rejected = str(exc) == "case_not_sealed_for_contest"
    if not workflow_rejected:
        raise Gate3PrepareError("unsealed_workflow_did_not_reject")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contests
                    (tenant_id, id, case_id, carrier_id, received_at,
                     sender, claim_text, claimed_rate, status)
                VALUES (%s, %s, %s, %s, now(), %s, %s, 0, 'OPEN')
                ON CONFLICT (tenant_id, id) DO NOTHING;
                """,
                (
                    tenant_id,
                    contest_id,
                    case_id,
                    carrier_id,
                    "Synthetic Adversarial Contest Fixture",
                    "Demonstration data: contest against an unsealed case.",
                ),
            )
            cur.execute(
                """
                SELECT case_id, carrier_id
                FROM contests WHERE tenant_id=%s AND id=%s;
                """,
                (tenant_id, contest_id),
            )
            row = cur.fetchone()
            fixture_bound = bool(
                row and str(row[0]) == case_id and str(row[1]) == carrier_id
            )
            if not fixture_bound:
                raise Gate3PrepareError("unsealed_adversarial_fixture_not_bound")
            cur.execute(
                """
                INSERT INTO query_log
                    (tenant_id, kind, tag, sql_text, actor, ok)
                VALUES (%s, 'audit', 'gate3.negative-fixture', %s, %s, true);
                """,
                (
                    tenant_id,
                    "ENSURE synthetic adversarial unsealed-contest fixture",
                    "gate3-negative-fixture",
                ),
            )
    return workflow_rejected, fixture_bound


def prepare(*, dsn: str, seal_path: Path, output_path: Path) -> dict[str, object]:
    seal = _load_gate1_seal(seal_path)
    existing = _load_existing(output_path)
    tenant_id = str(seal["tenant_id"])
    case_id = str(seal["case_id"])
    contest_id = _id(existing, "hero_contest_id")
    unsealed_contest_id = _id(existing, "unsealed_contest_id")
    received_at = str(
        existing.get("hero_contest_received_at")
        or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    reference_dsn, database = _reference_database_dsn(dsn, seal_path)
    with connect(reference_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT carrier_id FROM cases WHERE tenant_id=%s AND id=%s;",
                (tenant_id, case_id),
            )
            hero = cur.fetchone()
            if hero is None:
                raise Gate3PrepareError("hero_case_missing")
            carrier_id = str(hero[0])
        contest = record_later_contest(
            DAL(conn, Tenant(tenant_id=tenant_id, actor="gate3-contest-runtime")),
            contest_id=contest_id,
            case_id=case_id,
            received_at=received_at,
            sender="Synthetic Carrier Review Desk",
            claim_text="Demonstration data: later contest of the recorded rate.",
            claimed_rate="350.00",
        )
        unsealed_case_id = _ensure_unsealed_case(conn, tenant_id=tenant_id, carrier_id=carrier_id)
        workflow_rejected, adversarial_fixture_bound = _ensure_unsealed_adversarial_contest(
            conn,
            tenant_id=tenant_id,
            carrier_id=carrier_id,
            case_id=unsealed_case_id,
            contest_id=unsealed_contest_id,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenants WHERE id<>%s ORDER BY id LIMIT 1;", (tenant_id,))
            wrong_tenant = cur.fetchone()
            if wrong_tenant is None:
                cur.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id;",
                    ("Gate Three Fictional Isolation",),
                )
                wrong_tenant = cur.fetchone()
            wrong_tenant_id = str(wrong_tenant[0])
            unknown_case_id = _id(existing, "unknown_case_id")
            unknown_contest_id = _id(existing, "unknown_contest_id")
            cur.execute(
                "SELECT count(*) FROM cases WHERE tenant_id=%s AND id=%s;",
                (tenant_id, unknown_case_id),
            )
            unknown_absent = cur.fetchone()[0] == 0
            cur.execute(
                """
                SELECT state, evidence_hash IS NULL, sealed_by IS NULL,
                       sealed_at_display IS NULL, sealed_txn_ts IS NULL
                FROM cases WHERE tenant_id=%s AND id=%s;
                """,
                (tenant_id, unsealed_case_id),
            )
            unsealed = cur.fetchone()
            unsealed_precondition = bool(
                unsealed and unsealed[0] == "ANALYZED" and all(unsealed[1:])
            )
    result = {
        "classification": "private synthetic Gate 3 workflow inputs",
        "database": database,
        "tenant_id": tenant_id,
        "hero_case_id": case_id,
        "hero_contest_id": contest_id,
        "hero_contest_received_at": received_at,
        "wrong_tenant_id": wrong_tenant_id,
        "unknown_case_id": unknown_case_id,
        "unknown_contest_id": unknown_contest_id,
        "unsealed_case_id": unsealed_case_id,
        "unsealed_contest_id": unsealed_contest_id,
        "preconditions": {
            "hero_contest_recorded": contest["state"] == "CONTESTED",
            "wrong_tenant_exists": wrong_tenant_id != tenant_id,
            "unknown_case_absent": unknown_absent,
            "unsealed_case_exists_and_is_unsealed": unsealed_precondition,
            "normal_workflow_rejected_unsealed_contest": workflow_rejected,
            "adversarial_unsealed_contest_bound": adversarial_fixture_bound,
        },
    }
    if not all(result["preconditions"].values()):  # type: ignore[union-attr]
        raise Gate3PrepareError("fixture_precondition_failed")
    write_private_json(output_path, result)
    return result


def main() -> int:
    dsn_path = Path(os.environ.get("TALLY_GATE3_DSN_PATH", str(DEFAULT_DSN_PATH)))
    output_path = Path(os.environ.get("TALLY_GATE3_PREPARED_OUTPUT", str(DEFAULT_OUTPUT)))
    seal_path = Path(
        os.environ.get(
            "TALLY_GATE3_REFERENCE_SEAL",
            "runtime-artifacts/gate-1/private-seal.json",
        )
    )
    try:
        dsn = dsn_path.read_text(encoding="utf-8").strip()
        if not dsn:
            raise Gate3PrepareError("dsn_missing")
        result = prepare(dsn=dsn, seal_path=seal_path, output_path=output_path)
    except Gate3PrepareError as exc:
        print(f"gate3_prepare_error={exc}", file=sys.stderr)
        return 1
    except OSError:
        print("gate3_prepare_error=private_input_output_failure", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - redact driver diagnostics and connection metadata
        print("gate3_prepare_error=unexpected_private_setup_failure", file=sys.stderr)
        return 1
    for key, value in result["preconditions"].items():  # type: ignore[union-attr]
        print(f"{key}={str(value).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
