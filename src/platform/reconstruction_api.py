"""Public reconstruction projection — the ONE downstream contract for Gate 3/4.

`GET /api/invoices/{invoice_id}/reconstruction` returns the latest reconstruction
version's public-safe projection: the sourced timeline, the seven charged days
with coverage, and the categorical coverage set. It exposes verification state
and public refs only — never S3 buckets/keys/versions, private anchors, the MCP
SQL, or correlation internals. Gate 3 (applicable rule) and Gate 4 (judgment)
consume exactly this shape.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from src.external.dal import DAL, Tenant

SOURCE_DISCLOSURE = "Representative demonstration data"


def load_reconstruction_projection(
    dal: DAL, *, invoice_id: str
) -> dict[str, Any] | None:
    """Build the public projection for the latest reconstruction version."""
    tenant_id = dal.tenant.tenant_id
    with dal.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version, state, knowledge_cutoff_at, effective_timezone,
                   event_count, days_total, days_complete, public_summary
            FROM reconstructions
            WHERE tenant_id=%s AND invoice_id=%s
            ORDER BY version DESC
            LIMIT 1;
            """,
            (tenant_id, invoice_id),
        )
        head = cur.fetchone()
        if head is None:
            return None
        reconstruction_id = str(head[0])

        cur.execute(
            """
            SELECT public_ref, event_type, occurred_at, recorded_at,
                   recorded_before_cutoff, verification_state,
                   display_anchor_public, provenance_classification,
                   display_sequence
            FROM reconstruction_events
            WHERE tenant_id=%s AND reconstruction_id=%s
            ORDER BY display_sequence;
            """,
            (tenant_id, reconstruction_id),
        )
        timeline = [
            {
                "event_ref": row[0],
                "type": row[1],
                "occurred_at": row[2].isoformat(),
                "recorded_at": row[3].isoformat(),
                "recorded_before_invoice": bool(row[4]),
                "verification_state": row[5],
                "anchor": row[6],
                "provenance_class": row[7],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT id, charge_date, chargeability, coverage_state, state,
                   invoice_rate_minor, applicable_rate_minor, currency, outcome,
                   dispute_amount_minor, missing_requirements
            FROM reconstruction_charged_days
            WHERE tenant_id=%s AND reconstruction_id=%s
            ORDER BY charge_date;
            """,
            (tenant_id, reconstruction_id),
        )
        day_rows = cur.fetchall()

        # Event refs bound to each charged day (public refs only).
        cur.execute(
            """
            SELECT b.charged_day_id, e.public_ref, b.role
            FROM reconstruction_day_event_bindings b
            JOIN reconstruction_events e
              ON e.tenant_id=b.tenant_id AND e.id=b.reconstruction_event_id
            WHERE b.tenant_id=%s
              AND b.charged_day_id IN (
                SELECT id FROM reconstruction_charged_days
                WHERE tenant_id=%s AND reconstruction_id=%s
              );
            """,
            (tenant_id, tenant_id, reconstruction_id),
        )
        event_refs_by_day: dict[str, list[str]] = {}
        for day_id, event_ref, _role in cur.fetchall():
            event_refs_by_day.setdefault(str(day_id), []).append(event_ref)

        charged_days = [
            {
                "date": row[1].isoformat(),
                "chargeability": row[2],
                "coverage": row[3],
                "state": row[4],
                "invoice_rate_minor": int(row[5]),
                "applicable_rate_minor": (
                    int(row[6]) if row[6] is not None else None
                ),
                "currency": row[7],
                "outcome": row[8],
                "dispute_amount_minor": (
                    int(row[9]) if row[9] is not None else None
                ),
                "event_refs": sorted(set(event_refs_by_day.get(str(row[0]), []))),
                "missing_requirements": _json_list(row[10]),
            }
            for row in day_rows
        ]

        cur.execute(
            """
            SELECT requirement_code, coverage_state, detail
            FROM reconstruction_coverage
            WHERE tenant_id=%s AND reconstruction_id=%s
            ORDER BY requirement_code;
            """,
            (tenant_id, reconstruction_id),
        )
        coverage = [
            {"requirement": row[0], "state": row[1], "detail": row[2]}
            for row in cur.fetchall()
        ]

        # Gate 3 applicable rule (public-safe: no private source_locator).
        cur.execute(
            """
            SELECT public_ref, clause_ref, display_excerpt, rate_minor, currency,
                   unit, effective_from, effective_to, scope_code, validation_state
            FROM applicable_rules
            WHERE tenant_id=%s AND reconstruction_id=%s AND validation_state='VERIFIED';
            """,
            (tenant_id, reconstruction_id),
        )
        rule_row = cur.fetchone()

        # Gate 4 frozen recommendation (public-safe amounts + coverage).
        cur.execute(
            """
            SELECT id, version, recommendation_type, disputed_amount_minor,
                   supported_amount_minor, claimed_amount_minor, currency,
                   days_total, days_covered, evidence_coverage, state, public_summary,
                   digest
            FROM recommendations
            WHERE tenant_id=%s AND reconstruction_id=%s AND superseded_by IS NULL
            ORDER BY version DESC LIMIT 1;
            """,
            (tenant_id, reconstruction_id),
        )
        rec_row = cur.fetchone()

    recommendation = None
    if rec_row is not None:
        recommendation = {
            "recommendation_id": str(rec_row[0]),
            "version": int(rec_row[1]),
            "recommendation_type": rec_row[2],
            "disputed_amount_minor": int(rec_row[3]),
            "supported_amount_minor": int(rec_row[4]),
            "claimed_amount_minor": int(rec_row[5]),
            "currency": rec_row[6],
            "days_total": int(rec_row[7]),
            "days_covered": int(rec_row[8]),
            "evidence_coverage": rec_row[9],
            "state": rec_row[10],
            "summary": rec_row[11],
            "digest": rec_row[12],
            "approval_etag": f'"rec-{rec_row[0]}-v{int(rec_row[1])}-{rec_row[12]}"',
        }

    applicable_rule = None
    if rule_row is not None:
        applicable_rule = {
            "rule_ref": rule_row[0],
            "clause_ref": rule_row[1],
            "display_excerpt": rule_row[2],
            "rate_minor": int(rule_row[3]),
            "currency": rule_row[4],
            "unit": rule_row[5],
            "effective_from": rule_row[6].isoformat(),
            "effective_to": rule_row[7].isoformat() if rule_row[7] else None,
            "scope_code": rule_row[8],
            "validation_state": rule_row[9],
            "retrieval": {
                "tool": "CockroachDB Distributed Vector Indexing",
                "state": "RETRIEVED",
            },
        }

    missing = sorted(
        {
            requirement
            for day in charged_days
            for requirement in day["missing_requirements"]
        }
    )
    return {
        "reconstruction_id": reconstruction_id,
        "version": int(head[1]),
        "state": head[2],
        "knowledge_cutoff": head[3].isoformat(),
        "effective_timezone": head[4],
        "source_disclosure": SOURCE_DISCLOSURE,
        "summary": head[8],
        "timeline": timeline,
        "charged_days": charged_days,
        "applicable_rule": applicable_rule,
        "recommendation": recommendation,
        "coverage": {
            "days_complete": int(head[7]),
            "days_total": int(head[6]),
            "requirements": coverage,
            "missing_requirements": missing,
        },
    }


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    parsed = value if isinstance(value, list) else json.loads(value)
    return [str(item) for item in parsed]


def register_reconstruction_routes(router: APIRouter, *, tenant_id_getter) -> None:
    @router.get("/api/invoices/{invoice_id}/reconstruction")
    def get_reconstruction(invoice_id: str) -> dict[str, Any]:
        with DAL.connect(
            Tenant(tenant_id_getter(), "public-reconstruction-reader")
        ) as dal:
            projection = load_reconstruction_projection(dal, invoice_id=invoice_id)
        if projection is None:
            raise HTTPException(status_code=404, detail={"error": "NOT_FOUND"})
        return projection
