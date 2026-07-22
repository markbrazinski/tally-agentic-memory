"""Clerk pipeline orchestration: steps 1 -> 2 -> 3 -> 7.

Verdict precedence (compute_verdict/build_summary) and the extraction
gate (apply_anti_hallucination_gate) are pure logic and live in
src/core/verdict.py and src/core/extraction.py respectively - this
module orchestrates them plus the DAL-bound, non-pure step 7 commit.

Field-to-role mapping for step 3's timing check: the 13-field canon has
no field literally named "invoice_date" - TDD §4 step 3 names
`invoice_date` (from the invoice header, separate from the 13-field
extraction) and `last_charge_date` (= free_time_end per the same section:
"free_time_end / last incurred date per invoice"). invoice_date is not
one of the 13 fields Bedrock extracts in this session's prompt (it's
sourced from POST /invoices' own upload metadata in the full design, per
§3.1's received_at/invoice_date columns) - B0-S2's local pipeline takes
it as an explicit caller-supplied argument rather than inventing a 14th
extracted field not in the TDD's canon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.core.clerk_steps import FieldResult, WindowResult, check_presence, check_timing
from src.core.receipt import canonical_json_bytes, sha256_hex
from src.core.verdict import (  # noqa: F401
    VERDICT_DEFECTIVE,
    VERDICT_NEEDS_REVIEW,
    VERDICT_VALID,
    build_summary,
    compute_verdict,
)
from src.external.bedrock_extract import ExtractionResult
from src.external.dal import DAL


@dataclass(frozen=True)
class ClerkResult:
    verdict: str
    cited_rule: str | None
    field_results: tuple[FieldResult, ...]
    window_result: WindowResult
    summary: str


def run_extraction_steps(
    extraction_result: ExtractionResult,
    *,
    billed_party_name: str | None,
    invoice_date_raw: str | None,
    date_format_hint: str | None = None,
) -> ClerkResult:
    """Steps 2-3 + verdict, given step 1's already-gated extraction.

    invoice_date_raw comes from the invoice's own upload metadata, not
    the 13-field extraction (see module docstring). last_charge_date is
    read from the extracted free_time_end field, per TDD §4 step 3.
    """
    extracted_dict = {
        key: {"value": f.value, "verbatim": f.verbatim, "confidence": f.confidence}
        for key, f in extraction_result.fields.items()
    }
    field_results = check_presence(extracted_dict, billed_party_name=billed_party_name)

    last_charge_date_field = extraction_result.fields.get("free_time_end")
    window_result = check_timing(
        invoice_date_raw,
        last_charge_date_field.value if last_charge_date_field else None,
        date_format_hint=date_format_hint,
    )

    verdict, cited_rule = compute_verdict(field_results, window_result)
    summary = build_summary(verdict, field_results)

    return ClerkResult(
        verdict=verdict,
        cited_rule=cited_rule,
        field_results=field_results,
        window_result=window_result,
        summary=summary,
    )


def _evidence_content_hash(content: dict) -> str:
    return sha256_hex(canonical_json_bytes(content))


def file_case(
    dal: DAL,
    *,
    invoice_id: str,
    clerk_run_id: str,
    carrier_id: str,
    pin_date: str,
    amount: float,
    clerk_result: ClerkResult,
    evidence_items: tuple[dict, ...] = (),
    receipt_binding: dict | None = None,
) -> dict:
    """Step 7: the atomic filing commit (TDD §2.21-A). One transaction:
    finding + case (ANALYZED) + evidence copies + invoice status + a
    query_log row for the commit itself, written INSIDE the same
    transaction (closing the gap DAL.run_with_retry's own docstring flags:
    that path normally has zero logging - this is the first real caller,
    so it logs for real rather than carrying the gap forward again).

    Returns {"case_id":..., "finding_id":...}. Raises on any failure -
    the whole transaction rolls back, zero partial rows (verified by
    tests/unit/test_clerk_pipeline.py's induced-failure test).
    """
    field_results_json = json.dumps(
        [{"key": r.key, "present": r.present, "how": r.how} for r in clerk_result.field_results]
    )
    window_result_json = json.dumps(
        {
            "invoice_date": str(clerk_result.window_result.invoice_date)
            if clerk_result.window_result.invoice_date else None,
            "last_charge_date": str(clerk_result.window_result.last_charge_date)
            if clerk_result.window_result.last_charge_date else None,
            "days": clerk_result.window_result.days,
            "within_30": clerk_result.window_result.within_30,
            "ambiguous": clerk_result.window_result.ambiguous,
        }
    )
    receipt_binding = receipt_binding or {}
    tariff_result = receipt_binding.get("tariff_result", {})
    calculation = receipt_binding.get("calculation", {})

    tenant_id = dal.tenant.tenant_id
    actor = dal.tenant.actor

    def _commit(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO findings
                    (tenant_id, invoice_id, clerk_run_id, verdict, cited_rule,
                     field_results, window_result, tariff_result, summary,
                     amount_disputed, tariff_clause_id, recorded_rate,
                     invoice_claimed_rate, rate_unit, charge_days,
                     recommendation, calculation, human_approval_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, 'NOT_PRESSED')
                RETURNING id;
                """,
                (
                    tenant_id, invoice_id, clerk_run_id,
                    clerk_result.verdict, clerk_result.cited_rule,
                    field_results_json, window_result_json,
                    json.dumps(tariff_result, default=str),
                    clerk_result.summary,
                    calculation.get("overcharge"),
                    receipt_binding.get("tariff_clause_id"),
                    calculation.get("recorded_rate"),
                    calculation.get("claimed_rate"),
                    receipt_binding.get("rate_unit"),
                    calculation.get("charge_days"),
                    calculation.get("recommendation"),
                    json.dumps(calculation, default=str),
                ),
            )
            finding_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO cases
                    (tenant_id, invoice_id, finding_id, carrier_id, state,
                     pin_date, draft_dispute, amount)
                VALUES (%s, %s, %s, %s, 'ANALYZED', %s, %s, %s)
                RETURNING id;
                """,
                (
                    tenant_id, invoice_id, finding_id, carrier_id,
                    pin_date, clerk_result.summary, amount,
                ),
            )
            case_id = cur.fetchone()[0]

            for item in evidence_items:
                content_json = canonical_json_bytes(item["content"]).decode("utf-8")
                cur.execute(
                    """
                    INSERT INTO case_evidence
                        (tenant_id, case_id, kind, source_table, source_id,
                         content, content_sha256)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        tenant_id, case_id, item["kind"], item["source_table"],
                        item["source_id"], content_json,
                        _evidence_content_hash(item["content"]),
                    ),
                )

            cur.execute(
                "UPDATE invoices SET status='ANALYZED' WHERE tenant_id=%s AND id=%s;",
                (tenant_id, invoice_id),
            )
            cur.execute(
                """
                INSERT INTO query_log
                    (tenant_id, kind, tag, sql_text, actor, ok)
                VALUES (%s, 'system', 'clerk.commit', %s, %s, true);
                """,
                (
                    tenant_id,
                    f"COMMIT (finding + evidence + case) · case {case_id}",
                    actor,
                ),
            )
        return {"case_id": str(case_id), "finding_id": str(finding_id)}

    return dal.run_with_retry(_commit)
