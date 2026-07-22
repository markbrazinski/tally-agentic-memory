"""Tenant-scoped exact-seal replay through CockroachDB SQL.

There is no current-read fallback for the historical half.  A replay is
returned only after the stored case HLC, configured retention, the exact AOST
rows, and their current counterparts all validate.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from src.core.temporal_replay import (
    TemporalReplayValidationError,
    build_replay_response,
    canonical_hlc_literal,
    validate_replay_rows,
)
from src.external.dal import DAL

REPLAY_TABLES = ("cases", "case_evidence", "tariff_clauses", "tariff_snapshots")
REQUIRED_GC_TTL_SECONDS = 7_776_000
_TTL_RE = re.compile(r"gc\.ttlseconds\s*=\s*([0-9]+)")

_ROW_COLUMNS = (
    "tenant_id",
    "case_id",
    "case_state",
    "sealed_txn_ts",
    "evidence_hash",
    "evidence_manifest",
    "manifest_version",
    "evidence_id",
    "evidence_kind",
    "source_table",
    "source_id",
    "evidence_content",
    "content_sha256",
    "evidence_sealed",
    "clause_id",
    "tariff_rate",
    "snapshot_id",
    "clause_sha256",
    "snapshot_source_sha256",
    "version_label",
)

_CURRENT_QUERY = """
SELECT ca.tenant_id::STRING, ca.id::STRING, ca.state,
       ca.sealed_txn_ts::STRING, ca.evidence_hash, ca.evidence_manifest,
       ca.manifest_version, ce.id::STRING, ce.kind, ce.source_table,
       ce.source_id::STRING, ce.content, ce.content_sha256, ce.sealed,
       tc.id::STRING, tc.rate_amount::STRING, tc.snapshot_id::STRING,
       tc.sha256, ts.doc_sha256, ts.version_label
FROM cases AS ca
LEFT JOIN case_evidence AS ce
  ON ce.tenant_id=ca.tenant_id AND ce.case_id=ca.id
LEFT JOIN tariff_clauses AS tc
  ON tc.tenant_id=ce.tenant_id
 AND ce.source_table='tariff_clauses' AND tc.id=ce.source_id
LEFT JOIN tariff_snapshots AS ts
  ON ts.tenant_id=tc.tenant_id AND ts.id=tc.snapshot_id
WHERE ca.tenant_id=%s AND ca.id=%s
ORDER BY ce.id;
"""

_RETENTION_QUERY = """
SELECT 'cases', raw_config_sql
FROM [SHOW ZONE CONFIGURATION FROM TABLE public.cases]
WHERE %s::UUID IS NOT NULL
UNION ALL
SELECT 'case_evidence', raw_config_sql
FROM [SHOW ZONE CONFIGURATION FROM TABLE public.case_evidence]
UNION ALL
SELECT 'tariff_clauses', raw_config_sql
FROM [SHOW ZONE CONFIGURATION FROM TABLE public.tariff_clauses]
UNION ALL
SELECT 'tariff_snapshots', raw_config_sql
FROM [SHOW ZONE CONFIGURATION FROM TABLE public.tariff_snapshots];
"""


class ReplayNotFoundError(LookupError):
    pass


class ReplayNotSealedError(RuntimeError):
    pass


class ReplayUnavailableError(RuntimeError):
    pass


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _historical_query(sealed_txn_ts: Any) -> str:
    """Build the sole literal-bearing query after strict HLC validation.

    CockroachDB rejects placeholders specifically in ``AS OF SYSTEM TIME``.
    This narrow exception embeds only ``canonical_hlc_literal``'s unsigned
    decimal grammar.  Tenant and case values remain bound parameters.
    """
    hlc = canonical_hlc_literal(sealed_txn_ts)
    return f"""
SELECT ca.tenant_id::STRING, ca.id::STRING, ca.state,
       ca.sealed_txn_ts::STRING, ca.evidence_hash, ca.evidence_manifest,
       ca.manifest_version, ce.id::STRING, ce.kind, ce.source_table,
       ce.source_id::STRING, ce.content, ce.content_sha256, ce.sealed,
       tc.id::STRING, tc.rate_amount::STRING, tc.snapshot_id::STRING,
       tc.sha256, ts.doc_sha256, ts.version_label
FROM cases AS ca
LEFT JOIN case_evidence AS ce
  ON ce.tenant_id=ca.tenant_id AND ce.case_id=ca.id
LEFT JOIN tariff_clauses AS tc
  ON tc.tenant_id=ce.tenant_id
 AND ce.source_table='tariff_clauses' AND tc.id=ce.source_id
LEFT JOIN tariff_snapshots AS ts
  ON ts.tenant_id=tc.tenant_id AND ts.id=tc.snapshot_id
AS OF SYSTEM TIME {hlc}
WHERE ca.tenant_id=%s AND ca.id=%s
ORDER BY ce.id;
"""


def _execute(
    dal: DAL,
    sql: str,
    params: tuple[Any, ...],
    *,
    tag: str,
    render_source: str,
    audit_sql_text: str,
) -> tuple[list[tuple[Any, ...]], int]:
    prior_log_failures = dal.log_failure_count
    started = time.monotonic()
    rows = dal.execute(
        sql,
        params,
        tag=tag,
        kind="temporal_replay",
        render_source=render_source,
        audit_sql_text=audit_sql_text,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if dal.log_failure_count != prior_log_failures:
        raise ReplayUnavailableError("temporal replay query was not audit-visible")
    return rows, elapsed_ms


def _row_mappings(rows: Sequence[tuple[Any, ...]]) -> list[dict[str, Any]]:
    mapped = []
    for row in rows:
        if len(row) != len(_ROW_COLUMNS):
            raise ReplayUnavailableError("temporal replay returned an unexpected row shape")
        mapped.append(dict(zip(_ROW_COLUMNS, row, strict=True)))
    return mapped


def _validate_retention(rows: Sequence[tuple[Any, ...]]) -> None:
    configured: dict[str, int] = {}
    for row in rows:
        if len(row) != 2 or row[0] not in REPLAY_TABLES or not isinstance(row[1], str):
            raise ReplayUnavailableError("history retention readback was malformed")
        match = _TTL_RE.search(row[1])
        if match is None:
            raise ReplayUnavailableError("history retention has no GC TTL")
        configured[str(row[0])] = int(match.group(1))
    if set(configured) != set(REPLAY_TABLES) or any(
        configured[table] != REQUIRED_GC_TTL_SECONDS for table in REPLAY_TABLES
    ):
        raise ReplayUnavailableError("history retention is not configured for 90 days")


def _query_line(*, tag: str, elapsed_ms: int, sql: str) -> str:
    normalized = " ".join(sql.split())
    return f"{tag} · via cockroachdb · elapsed_ms={elapsed_ms} · {normalized}"


def replay_case(dal: DAL, *, case_id: str) -> dict[str, Any]:
    """Reconstruct one case exactly at its stored seal HLC and compare now."""
    trusted_tenant = _uuid(dal.tenant.tenant_id, field="tenant_id")
    requested_case = _uuid(case_id, field="case_id")
    try:
        current_rows, current_elapsed = _execute(
            dal,
            _CURRENT_QUERY,
            (requested_case,),
            tag="replay.now",
            render_source="current",
            audit_sql_text="fixed replay current-read template; sensitive values bound",
        )
    except ReplayUnavailableError:
        raise
    except Exception as exc:
        raise ReplayUnavailableError("current replay read failed") from exc
    if not current_rows:
        raise ReplayNotFoundError(requested_case)
    first = current_rows[0]
    if len(first) < 7:
        raise ReplayUnavailableError("current replay row was malformed")
    if first[2] not in {"FILED", "CONTESTED", "RESOLVED"} or first[3] is None:
        raise ReplayNotSealedError(requested_case)
    try:
        seal_hlc = canonical_hlc_literal(first[3])
        current = validate_replay_rows(
            _row_mappings(current_rows),
            expected_tenant_id=trusted_tenant,
            expected_case_id=requested_case,
            expected_hlc=seal_hlc,
            historical=False,
        )
    except TemporalReplayValidationError as exc:
        raise ReplayUnavailableError("current sealed receipt is invalid") from exc

    try:
        retention_rows, _ = _execute(
            dal,
            _RETENTION_QUERY,
            (),
            tag="replay.retention",
            render_source="configuration",
            audit_sql_text="fixed replay retention-readback template",
        )
        _validate_retention(retention_rows)
        aost_sql = _historical_query(seal_hlc)
        historical_rows, historical_elapsed = _execute(
            dal,
            aost_sql,
            (requested_case,),
            tag="replay.then",
            render_source="as_of_system_time",
            audit_sql_text="fixed replay AOST template; seal HLC redacted",
        )
        historical = validate_replay_rows(
            _row_mappings(historical_rows),
            expected_tenant_id=trusted_tenant,
            expected_case_id=requested_case,
            expected_hlc=seal_hlc,
            historical=True,
        )
        return build_replay_response(
            historical=historical,
            current=current,
            queries=[
                _query_line(tag="replay.then", elapsed_ms=historical_elapsed, sql=aost_sql),
                _query_line(tag="replay.now", elapsed_ms=current_elapsed, sql=_CURRENT_QUERY),
            ],
        )
    except ReplayUnavailableError:
        raise
    except Exception as exc:
        raise ReplayUnavailableError("exact historical replay failed") from exc
