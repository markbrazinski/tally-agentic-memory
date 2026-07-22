"""Redacted, synthetic-only Gate 2 retrieval execution runner.

The command deliberately writes detailed evidence only to a caller-supplied,
ignored private path.  stdout is a compact public-safe summary: no DSN,
object identity, hash, vector, tenant, carrier, clause, or plan detail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from src.core.receipt import parse_invoice_claim
from src.external.dal import DAL, Tenant
from src.external.db import connect
from src.external.gate2_vector_search import ClauseCarrierScope, CockroachClauseVectorSearch
from src.external.seed_demo_tenant import run_seed
from src.external.titan_embeddings import MODEL_ID, TitanEmbedding, TitanTextEmbeddingsV2
from src.external.versioned_source import RetainedObject, S3VersionedSource
from src.platform.private_artifacts import DEFAULT_PRIVATE_ROOT, write_private_json
from src.platform.receipt_pipeline import file_gate1_case, persist_verified_inputs
from src.platform.vector_receipt_pipeline import (
    VectorReceiptPipeline,
    VectorReceiptRequest,
    compose_query_embedding_input,
)
from src.platform.vector_seed import SeedClauseSpec, seed_synthetic_clauses


class Gate2RunnerError(RuntimeError):
    """A redacted runner failure; never include private values in this message."""


@dataclass(frozen=True)
class InventoryEntry:
    bucket: str
    key: str
    version_id: str


@dataclass(frozen=True)
class RunnerConfig:
    dsn: str
    fixture_path: Path
    inventory_path: Path
    carrier_scac: str
    lane: str
    hero_query_id: str
    dispute_date: date
    private_output_path: Path


@dataclass(frozen=True)
class PublicSummary:
    passed: bool
    hero_selected_250: bool
    idempotent: bool
    index_used: bool
    cross_tenant_no_candidate: bool
    masking_abstains: bool
    query_count: int
    selected_count: int
    abstained_count: int
    raw_top1_count: int = 0
    raw_topk_count: int = 0
    selection_match_count: int = 0
    expected_abstention_count: int = 0
    populated_row_count: int = 0
    index_bruteforce_agree: bool = False
    seed_idempotent: bool = False
    failure_stage: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "hero_selected_250": self.hero_selected_250,
            "idempotent": self.idempotent,
            "index_used": self.index_used,
            "cross_tenant_no_candidate": self.cross_tenant_no_candidate,
            "masking_abstains": self.masking_abstains,
            "query_count": self.query_count,
            "selected_count": self.selected_count,
            "abstained_count": self.abstained_count,
            "raw_top1_count": self.raw_top1_count,
            "raw_topk_count": self.raw_topk_count,
            "selection_match_count": self.selection_match_count,
            "expected_abstention_count": self.expected_abstention_count,
            "populated_row_count": self.populated_row_count,
            "index_bruteforce_agree": self.index_bruteforce_agree,
            "seed_idempotent": self.seed_idempotent,
            "failure_stage": self.failure_stage,
        }


class _MaskedSearch:
    """Test-only query/filter seam; it never mutates retained database state."""

    def __init__(self, search: CockroachClauseVectorSearch, clause_id: str):
        self._search = search
        self._clause_id = clause_id

    def search(self, **kwargs):
        return [hit for hit in self._search.search(**kwargs) if hit.clause_id != self._clause_id]


class _BruteForceSearch:
    """Adapt the exact primary-index oracle to the product pipeline search protocol."""

    def __init__(self, search: CockroachClauseVectorSearch):
        self._search = search

    def search(self, **kwargs):
        return self._search.search_brute_force(**kwargs)


class _CachingEmbeddings:
    """Reuse one exact Titan response for repeated seed and oracle inputs."""

    def __init__(self, embeddings: TitanTextEmbeddingsV2):
        self._embeddings = embeddings
        self._cache: dict[str, TitanEmbedding] = {}

    def embed(self, text: str) -> TitanEmbedding:
        if text not in self._cache:
            self._cache[text] = self._embeddings.embed(text)
        return self._cache[text]


def load_fixture(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Gate2RunnerError("fixture_load_failed") from exc
    if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
        raise Gate2RunnerError("fixture_shape_invalid")
    if not isinstance(value.get("evaluation_queries"), list):
        raise Gate2RunnerError("fixture_shape_invalid")
    return value


def load_inventory(path: Path) -> tuple[dict[str, InventoryEntry], InventoryEntry]:
    """Load private alias bindings without returning their raw mapping to stdout."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        invoice = value["invoice"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise Gate2RunnerError("source_inventory_load_failed") from exc
    if not isinstance(value, Mapping) or not isinstance(invoice, Mapping):
        raise Gate2RunnerError("source_inventory_shape_invalid")
    captures = {key: entry for key, entry in value.items() if str(key).startswith("tariff:")}
    if not captures:
        raise Gate2RunnerError("source_inventory_shape_invalid")
    return (
        {str(alias): _inventory_entry(entry) for alias, entry in captures.items()},
        _inventory_entry(invoice),
    )


def _inventory_entry(value: object) -> InventoryEntry:
    if not isinstance(value, Mapping):
        raise Gate2RunnerError("source_inventory_shape_invalid")
    try:
        entry = InventoryEntry(str(value["bucket"]), str(value["key"]), str(value["version_id"]))
    except KeyError as exc:
        raise Gate2RunnerError("source_inventory_shape_invalid") from exc
    if not all((entry.bucket.strip(), entry.key.strip(), entry.version_id.strip())):
        raise Gate2RunnerError("source_inventory_shape_invalid")
    return entry


def _seed_specs(
    fixture: Mapping[str, object], *, tenant_alias: str | None = None
) -> list[SeedClauseSpec]:
    specs: list[SeedClauseSpec] = []
    for document in fixture["documents"]:  # validated by load_fixture
        if not isinstance(document, Mapping):
            raise Gate2RunnerError("fixture_shape_invalid")
        if tenant_alias is not None and document.get("tenant_id") != tenant_alias:
            continue
        specs.append(
            SeedClauseSpec.from_mapping(
                {
                    "capture_alias": document["capture_id"],
                    "clause_ref": document["clause_id"],
                    "document_family": document["document_family"],
                    "source_text": document["source_text"],
                    "clause_text": document["clause_text"],
                    "effective_from": document["effective_from"],
                    "effective_to": document["effective_to"],
                    "equipment_type": document["equipment"],
                    "route_code": document["route"],
                    "service_context": document["service"],
                    "rate_amount": document["rate_amount"],
                    "rate_currency": document["rate_currency"],
                    "rate_unit": document["rate_unit"],
                }
            )
        )
    return specs


def _ensure_isolation_tenant(conn: object, scac: str, carrier_id: str) -> tuple[str, str]:
    """Create/reuse only a fictional tenant+carrier used for isolation proof."""
    with conn.cursor() as cur:  # type: ignore[union-attr]
        cur.execute("SELECT id FROM tenants WHERE name=%s;", ("Gate Two Fictional Isolation",))
        tenant = cur.fetchone()
        if tenant is None:
            cur.execute(
                "INSERT INTO tenants (name) VALUES (%s) RETURNING id;",
                ("Gate Two Fictional Isolation",),
            )
            tenant = cur.fetchone()
        tenant_id = str(tenant[0])
        cur.execute(
            "INSERT INTO carriers (tenant_id, id, scac, name) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, scac) DO NOTHING;",
            (tenant_id, carrier_id, scac, "Gate Two Fictional Isolation Carrier"),
        )
        cur.execute("SELECT id FROM carriers WHERE tenant_id=%s AND scac=%s;", (tenant_id, scac))
        carrier = cur.fetchone()
    if carrier is None:
        raise Gate2RunnerError("isolation_fixture_setup_failed")
    if str(carrier[0]) != carrier_id:
        raise Gate2RunnerError("isolation_carrier_identity_conflict")
    return tenant_id, str(carrier[0])


def _find_query(fixture: Mapping[str, object], query_id: str) -> Mapping[str, object]:
    for query in fixture["evaluation_queries"]:
        if isinstance(query, Mapping) and query.get("query_id") == query_id:
            return query
    raise Gate2RunnerError("hero_query_not_found")


def _request(query: Mapping[str, object], *, carrier_id: str, bucket: str) -> VectorReceiptRequest:
    return VectorReceiptRequest(
        carrier_id=carrier_id,
        document_family=str(query["document_family"]),
        bucket=bucket,
        charge_date=date.fromisoformat(str(query["charge_date"])),
        equipment=str(query["equipment"]),
        route=str(query["route"]),
        service=str(query["service"]),
        invoice_context=(
            f"Synthetic invoice charge for {query['service']} {query['equipment']} "
            f"on {query['route']}."
        ),
        top_k=25,
    )


def _get_exact(source: S3VersionedSource, entry: InventoryEntry) -> RetainedObject:
    try:
        return source.get_exact(bucket=entry.bucket, key=entry.key, version_id=entry.version_id)
    except Exception as exc:  # noqa: BLE001 - public failure intentionally redacted
        raise Gate2RunnerError("exact_source_fetch_failed") from exc


def _require_fixture_bytes(retained: RetainedObject, expected: bytes, stage: str) -> None:
    if retained.body != expected:
        raise Gate2RunnerError(stage)


def _require_invoice_template(
    retained: RetainedObject,
    template: bytes,
    tariffs: Sequence[RetainedObject],
) -> None:
    try:
        actual = json.loads(retained.body)
        expected = json.loads(template)
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            raise TypeError("invoice values must be objects")
        actual_received = str(actual.pop("received_at"))
        expected.pop("received_at")
        received_at = datetime.fromisoformat(actual_received.replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Gate2RunnerError("invoice_template_invalid") from exc
    if actual != expected or expected.get("classification") != "synthetic demonstration data":
        raise Gate2RunnerError("invoice_template_mismatch")
    if received_at.tzinfo is None:
        raise Gate2RunnerError("invoice_received_at_unzoned")
    received_at = received_at.astimezone(UTC)
    invoice_observed_at = retained.observed_at.astimezone(UTC)
    if any(item.observed_at.astimezone(UTC) > invoice_observed_at for item in tariffs):
        raise Gate2RunnerError("invoice_observed_before_tariff")
    if (
        any(item.observed_at.astimezone(UTC) > received_at for item in tariffs)
        or invoice_observed_at > received_at
    ):
        raise Gate2RunnerError("invoice_received_before_source_observation")


def _lookup_carrier(dal: DAL, scac: str) -> str:
    rows = dal.execute(
        "SELECT id FROM carriers WHERE tenant_id=%s AND scac=%s;",
        (scac,),
        tag="gate2.runner.carrier_lookup",
    )
    if len(rows) != 1 or rows[0][0] is None:
        raise Gate2RunnerError("carrier_lookup_failed")
    return str(rows[0][0])


def _receipt_counts(dal: DAL, invoice_id: str) -> tuple[int, int, int, int]:
    rows = dal.execute(
        """
        WITH target (tenant_id, invoice_id) AS (VALUES (%s::UUID, %s::UUID))
        SELECT
          (SELECT count(*) FROM invoices, target
             WHERE invoices.tenant_id=target.tenant_id AND invoices.id=target.invoice_id),
          (SELECT count(*) FROM findings, target
             WHERE findings.tenant_id=target.tenant_id AND findings.invoice_id=target.invoice_id),
          (SELECT count(*) FROM cases, target
             WHERE cases.tenant_id=target.tenant_id AND cases.invoice_id=target.invoice_id),
          (SELECT count(*) FROM case_evidence ce JOIN cases c ON c.tenant_id=ce.tenant_id
             AND c.id=ce.case_id, target
             WHERE c.tenant_id=target.tenant_id AND c.invoice_id=target.invoice_id);
        """,
        (invoice_id,),
        tag="gate2.runner.receipt_counts",
    )
    if len(rows) != 1 or len(rows[0]) != 4:
        raise Gate2RunnerError("receipt_count_readback_failed")
    return tuple(int(value) for value in rows[0])  # type: ignore[return-value]


def _populated_vector_count(dal: DAL, carrier_id: str) -> int:
    rows = dal.execute(
        """
        SELECT count(*) FROM tariff_clauses
        WHERE tenant_id=%s AND carrier_id=%s AND embedding IS NOT NULL;
        """,
        (carrier_id,),
        tag="gate2.runner.populated_vector_count",
    )
    if len(rows) != 1 or len(rows[0]) != 1:
        raise Gate2RunnerError("populated_vector_count_readback_failed")
    return int(rows[0][0])


def _schema_readback(dal: DAL) -> dict[str, bool]:
    rows = dal.execute(
        """
        WITH authenticated AS (SELECT %s::UUID AS tenant_id)
        SELECT create_statement
        FROM [SHOW CREATE TABLE tariff_clauses], authenticated
        WHERE authenticated.tenant_id IS NOT NULL;
        """,
        tag="gate2.runner.schema_readback",
    )
    if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
        raise Gate2RunnerError("schema_readback_failed")
    ddl = " ".join(rows[0][0].split())
    return {
        "vector_1024": "VECTOR(1024)" in ddl,
        "named_vector_index": "tariff_clause_embedding_search_idx" in ddl,
        "tenant_carrier_prefix": "tenant_id, carrier_id, embedding" in ddl,
        "embedding_provenance": all(
            column in ddl
            for column in (
                "embedding_model",
                "embedding_input_sha256",
                "embedding_sha256",
            )
        ),
    }


def _sanitized_candidates(
    result: object,
    *,
    clause_aliases: Mapping[str, str],
    capture_aliases: Mapping[str, str],
) -> list[dict[str, object]]:
    retrieval = getattr(result, "retrieval")
    return [
        {
            "rank": rank,
            "clause_fixture_id": clause_aliases.get(candidate.clause_id, "unknown-fixture"),
            "capture_fixture_id": capture_aliases.get(candidate.capture_id, "unknown-fixture"),
            "source_version": "<private-exact-version>",
            "normalized_l2_distance": candidate.normalized_l2_distance,
            "temporal_status": candidate.temporal_status,
            "exact_source_status": candidate.exact_source_status,
            "selected": candidate.selected,
            "rejection_reasons": list(candidate.rejection_reasons),
        }
        for rank, candidate in enumerate(retrieval.candidates, start=1)
    ]


def sanitize_plan_lines(lines: Sequence[str]) -> list[str]:
    """Reduce plans to structural facts; never preserve bound or infrastructure values."""
    summaries: list[str] = []
    for position, line in enumerate(lines, start=1):
        lowered = line.lower()
        node = next(
            (
                name
                for name in ("scan", "lookup join", "sort", "limit", "render")
                if name in lowered
            ),
            "other",
        )
        summaries.append(
            "plan_line="
            f"{position};node={node};named_vector_index="
            f"{str('tariff_clause_embedding_search_idx' in line).lower()};"
            f"vector_distance={str('<->' in line).lower()}"
        )
    return summaries


def write_private_output(
    path: Path,
    value: Mapping[str, object],
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> None:
    write_private_json(path, value, private_root=private_root)


def run(config: RunnerConfig, *, source: S3VersionedSource | None = None) -> PublicSummary:
    """Execute synthetic retrieval/file proof twice; all failures remain stage-redacted."""
    stage = "fixture"
    private_failure: dict[str, str] = {}
    try:
        fixture = load_fixture(config.fixture_path)
        captures, invoice_entry = load_inventory(config.inventory_path)
        invoice_template = Path(
            config.fixture_path.parent / "northstar-invoice.json"
        ).read_bytes()
        hero = _find_query(fixture, config.hero_query_id)
        hero_tenant_alias = str(hero["tenant_id"])
        specs = _seed_specs(fixture, tenant_alias=hero_tenant_alias)
        primary_aliases = {item.capture_alias for item in specs}
        isolation_specs = [
            spec for spec in _seed_specs(fixture) if spec.capture_alias not in primary_aliases
        ]
        stage = "seed"
        tenant_id = run_seed(config.dsn)
        with connect(config.dsn) as conn:
            dal = DAL(conn, Tenant(tenant_id=tenant_id, actor="gate2-retrieval-runner"))
            carrier_id = _lookup_carrier(dal, config.carrier_scac)
            if source is None:
                import boto3

                source = S3VersionedSource(boto3.client("s3"))
            retained_by_alias: dict[str, RetainedObject] = {}
            operator_versions: dict[str, str] = {}
            for spec in [*specs, *isolation_specs]:
                entry = captures.get(f"tariff:{spec.capture_alias}")
                if entry is None:
                    raise Gate2RunnerError("source_inventory_alias_missing")
                retained = _get_exact(source, entry)
                _require_fixture_bytes(
                    retained, spec.source_text.encode("utf-8"), "tariff_body_mismatch"
                )
                retained_by_alias[spec.capture_alias] = retained
                operator_versions[spec.capture_alias] = entry.version_id
            invoice = _get_exact(source, invoice_entry)
            _require_invoice_template(
                invoice, invoice_template, list(retained_by_alias.values())
            )
            if len({item.bucket for item in retained_by_alias.values()}) != 1:
                raise Gate2RunnerError("tariff_bucket_scope_invalid")
            bucket = next(iter(retained_by_alias.values())).bucket
            embedder = _CachingEmbeddings(TitanTextEmbeddingsV2())
            seed_result = seed_synthetic_clauses(
                dal, specs=specs, retained_by_alias=retained_by_alias,
                operator_version_ids=operator_versions, carrier_id=carrier_id,
                lane=config.lane, embedding_model=MODEL_ID, embedder=embedder,
            )
            seed_reuse = seed_synthetic_clauses(
                dal, specs=specs, retained_by_alias=retained_by_alias,
                operator_version_ids=operator_versions, carrier_id=carrier_id,
                lane=config.lane, embedding_model=MODEL_ID, embedder=embedder,
            )
            seed_idempotent = (
                seed_reuse.snapshot_ids_by_alias == seed_result.snapshot_ids_by_alias
                and seed_reuse.clause_ids_by_alias == seed_result.clause_ids_by_alias
                and seed_reuse.snapshots_inserted == 0
                and seed_reuse.clauses_inserted == 0
                and seed_reuse.snapshots_reused == len(specs)
                and seed_reuse.clauses_reused == len(specs)
            )
            if isolation_specs:
                isolation_tenant_id, isolation_carrier_id = _ensure_isolation_tenant(
                    conn, config.carrier_scac, carrier_id
                )
                isolation_dal = DAL(
                    conn, Tenant(tenant_id=isolation_tenant_id, actor="gate2-isolation-runner")
                )
                isolation_seed = seed_synthetic_clauses(
                    isolation_dal, specs=isolation_specs, retained_by_alias=retained_by_alias,
                    operator_version_ids=operator_versions, carrier_id=isolation_carrier_id,
                    lane=config.lane, embedding_model=MODEL_ID, embedder=embedder,
                )
                isolation_reuse = seed_synthetic_clauses(
                    isolation_dal, specs=isolation_specs,
                    retained_by_alias=retained_by_alias,
                    operator_version_ids=operator_versions,
                    carrier_id=isolation_carrier_id, lane=config.lane,
                    embedding_model=MODEL_ID, embedder=embedder,
                )
                seed_idempotent = (
                    seed_idempotent
                    and isolation_reuse.snapshot_ids_by_alias
                    == isolation_seed.snapshot_ids_by_alias
                    and isolation_reuse.clause_ids_by_alias
                    == isolation_seed.clause_ids_by_alias
                    and isolation_reuse.snapshots_inserted == 0
                    and isolation_reuse.clauses_inserted == 0
                    and isolation_reuse.snapshots_reused == len(isolation_specs)
                    and isolation_reuse.clauses_reused == len(isolation_specs)
                )
            clause_aliases = {
                seed_result.clause_ids_by_alias[spec.capture_alias]: spec.clause_ref
                for spec in specs
            }
            capture_aliases = {
                seed_result.snapshot_ids_by_alias[spec.capture_alias]: spec.capture_alias
                for spec in specs
            }
            request = _request(hero, carrier_id=carrier_id, bucket=bucket)
            search = CockroachClauseVectorSearch(dal)
            pipeline = VectorReceiptPipeline(
                dal, embeddings=embedder, search=search, source=source
            )
            brute_pipeline = VectorReceiptPipeline(
                dal,
                embeddings=embedder,
                search=_BruteForceSearch(search),
                source=source,
            )
            stage = "retrieve_file"
            stored_clause_ids: list[str] = []
            filed_count = 0
            filing_results: list[dict[str, object]] = []
            receipt_counts: list[tuple[int, int, int, int]] = []
            for _ in range(2):
                result = pipeline.retrieve(request)
                hit = result.selected_hit
                if hit is None or hit.rate_amount != Decimal("250.00"):
                    raise Gate2RunnerError("hero_retrieval_failed")
                if (
                    result.selected_source is None
                    or result.extraction is None
                    or result.verification is None
                ):
                    raise Gate2RunnerError("hero_verification_failed")
                stored = persist_verified_inputs(
                    dal, carrier_id=carrier_id, lane=config.lane, tariff=result.selected_source,
                    tariff_source_url="retained-object://exact-version",
                    extraction=result.extraction, verification=result.verification,
                    invoice=invoice, invoice_claim=parse_invoice_claim(invoice.body),
                )
                if stored.clause_id != hit.clause_id:
                    raise Gate2RunnerError("selected_clause_binding_failed")
                filed = file_gate1_case(
                    dal, carrier_id=carrier_id, stored=stored, tariff=result.selected_source,
                    invoice=invoice, extraction=result.extraction, verification=result.verification,
                    invoice_claim=parse_invoice_claim(invoice.body), pin_date=config.dispute_date,
                )
                stored_clause_ids.append(stored.clause_id)
                filed_count += int(bool(filed.get("filed")))
                filing_results.append(filed)
                receipt_counts.append(_receipt_counts(dal, stored.invoice_id))
            brute_hero = brute_pipeline.retrieve(request)
            index_bruteforce_agree = result.retrieval == brute_hero.retrieval
            query_input = compose_query_embedding_input(
                document_family=request.document_family,
                equipment=request.equipment,
                route=request.route,
                service=request.service,
                invoice_context=request.invoice_context,
            )
            proof = search.explain_index_use(
                scope=ClauseCarrierScope(carrier_id=carrier_id),
                query_embedding=embedder.embed(query_input).values, limit=request.top_k,
            )
            primary_candidate_ids = {
                candidate.clause_id for candidate in result.retrieval.candidates
            }
            cross_tenant_no_candidate = False
            if isolation_specs:
                cross_pipeline = VectorReceiptPipeline(
                    isolation_dal,
                    embeddings=embedder,
                    search=CockroachClauseVectorSearch(isolation_dal),
                    source=source,
                )
                cross_result = cross_pipeline.retrieve(request)
                cross_ids = {candidate.clause_id for candidate in cross_result.retrieval.candidates}
                isolation_ids = set(isolation_seed.clause_ids_by_alias.values())
                cross_tenant_no_candidate = (
                    bool(cross_ids & isolation_ids)
                    and not (primary_candidate_ids & isolation_ids)
                )
            masked_pipeline = VectorReceiptPipeline(
                dal,
                embeddings=embedder,
                search=_MaskedSearch(search, stored_clause_ids[0]),
                source=source,
            )
            masking_abstains = masked_pipeline.retrieve(request).retrieval.abstained
            selected_count = abstained_count = 0
            raw_top1_count = raw_topk_count = selection_match_count = expected_abstention_count = 0
            evaluation_output: list[dict[str, object]] = []
            for query in fixture["evaluation_queries"]:
                if not isinstance(query, Mapping):
                    raise Gate2RunnerError("fixture_shape_invalid")
                fixture_carrier = str(query.get("carrier_id", ""))
                try:
                    evaluation_request = _request(
                        query,
                        carrier_id=carrier_id if fixture_carrier.strip() else "",
                        bucket=bucket,
                    )
                    evaluation = pipeline.retrieve(evaluation_request)
                except ValueError:
                    if fixture_carrier.strip():
                        raise
                    try:
                        brute_pipeline.retrieve(evaluation_request)
                    except ValueError:
                        pass
                    else:
                        index_bruteforce_agree = False
                    abstained_count += 1
                    expected_abstention_count += 1
                    selection_match_count += 1
                    evaluation_output.append(
                        {
                            "query_id": str(query.get("query_id")),
                            "expected_clause_id": None,
                            "selected_clause_id": None,
                            "abstained": True,
                            "candidate_count": 0,
                        }
                    )
                    continue
                brute_evaluation = brute_pipeline.retrieve(evaluation_request)
                index_bruteforce_agree = (
                    index_bruteforce_agree
                    and evaluation.retrieval == brute_evaluation.retrieval
                )
                selected_count += int(evaluation.retrieval.selected is not None)
                abstained_count += int(evaluation.retrieval.abstained)
                expected_alias = query.get("expected_clause_id")
                expected_ids_by_ref = {
                    spec.clause_ref: seed_result.clause_ids_by_alias[spec.capture_alias]
                    for spec in specs
                }
                expected_id = expected_ids_by_ref.get(str(expected_alias))
                candidates = evaluation.retrieval.candidates
                raw_top1_count += int(bool(candidates) and candidates[0].clause_id == expected_id)
                raw_topk_count += int(expected_id in {item.clause_id for item in candidates[:25]})
                selected = evaluation.retrieval.selected
                selection_match_count += int(
                    (selected.clause_id if selected else None) == expected_id
                )
                expected_abstention_count += int(
                    expected_id is None and evaluation.retrieval.abstained
                )
                selected_alias = (
                    clause_aliases.get(evaluation.retrieval.selected.clause_id)
                    if evaluation.retrieval.selected
                    else None
                )
                evaluation_output.append(
                    {
                        "query_id": str(query.get("query_id")),
                        "expected_clause_id": expected_alias,
                        "selected_clause_id": selected_alias,
                        "abstained": evaluation.retrieval.abstained,
                        "candidate_count": len(evaluation.retrieval.candidates),
                    }
                )
            populated_row_count = _populated_vector_count(dal, carrier_id)
            if isolation_specs:
                populated_row_count += _populated_vector_count(
                    isolation_dal, isolation_carrier_id
                )
            schema_readback = _schema_readback(dal)
            idempotent = (
                seed_idempotent
                and len(set(stored_clause_ids)) == 1
                and filed_count == 2
                and len(filing_results) == 2
                and filing_results[0].get("case_id") == filing_results[1].get("case_id")
                and filing_results[0].get("finding_id") == filing_results[1].get("finding_id")
                and filing_results[1].get("already_filed") is True
                and receipt_counts == [(1, 1, 1, 1), (1, 1, 1, 1)]
            )
            summary = PublicSummary(
                passed=False, hero_selected_250=True, idempotent=idempotent,
                index_used=proof.uses_named_vector_index,
                cross_tenant_no_candidate=cross_tenant_no_candidate,
                masking_abstains=masking_abstains, query_count=len(fixture["evaluation_queries"]),
                selected_count=selected_count, abstained_count=abstained_count,
                raw_top1_count=raw_top1_count, raw_topk_count=raw_topk_count,
                selection_match_count=selection_match_count,
                expected_abstention_count=expected_abstention_count,
                populated_row_count=populated_row_count,
                index_bruteforce_agree=index_bruteforce_agree,
                seed_idempotent=seed_idempotent,
            )
            summary = PublicSummary(
                passed=all((
                    summary.hero_selected_250,
                    summary.idempotent,
                    summary.index_used,
                    summary.cross_tenant_no_candidate,
                    summary.masking_abstains,
                    summary.query_count == 4,
                    summary.raw_topk_count == 2,
                    summary.selection_match_count == 4,
                    summary.expected_abstention_count == 2,
                    summary.populated_row_count == len(specs) + len(isolation_specs),
                    summary.index_bruteforce_agree,
                    summary.seed_idempotent,
                    all(schema_readback.values()),
                )),
                hero_selected_250=summary.hero_selected_250,
                idempotent=summary.idempotent,
                index_used=summary.index_used,
                cross_tenant_no_candidate=summary.cross_tenant_no_candidate,
                masking_abstains=summary.masking_abstains,
                query_count=summary.query_count,
                selected_count=summary.selected_count,
                abstained_count=summary.abstained_count,
                raw_top1_count=summary.raw_top1_count,
                raw_topk_count=summary.raw_topk_count,
                selection_match_count=summary.selection_match_count,
                expected_abstention_count=summary.expected_abstention_count,
                populated_row_count=summary.populated_row_count,
                index_bruteforce_agree=summary.index_bruteforce_agree,
                seed_idempotent=summary.seed_idempotent,
            )
            write_private_output(
                config.private_output_path,
                {
                    "summary": summary.as_dict(),
                    "schema_readback": schema_readback,
                    "sanitized_plan_lines": sanitize_plan_lines(proof.plan_lines),
                    "private_plan_lines": list(proof.plan_lines),
                    "hero_top_k": _sanitized_candidates(
                        result,
                        clause_aliases=clause_aliases,
                        capture_aliases=capture_aliases,
                    ),
                    "evaluation": evaluation_output,
                    "filed_attempts": filed_count,
                },
            )
            return summary
    except Gate2RunnerError as exc:
        private_failure = {"error_type": type(exc).__name__, "error_message": str(exc)}
        summary = PublicSummary(
            passed=False,
            hero_selected_250=False,
            idempotent=False,
            index_used=False,
            cross_tenant_no_candidate=False,
            masking_abstains=False,
            query_count=0,
            selected_count=0,
            abstained_count=0,
            failure_stage=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - details stay in ignored mode-0600 evidence
        private_failure = {"error_type": type(exc).__name__, "error_message": str(exc)}
        summary = PublicSummary(
            passed=False,
            hero_selected_250=False,
            idempotent=False,
            index_used=False,
            cross_tenant_no_candidate=False,
            masking_abstains=False,
            query_count=0,
            selected_count=0,
            abstained_count=0,
            failure_stage=stage,
        )
    write_private_output(
        config.private_output_path,
        {"summary": summary.as_dict(), "private_failure": private_failure},
    )
    return summary


def parse_args(argv: Sequence[str] | None = None) -> RunnerConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--source-inventory", type=Path, required=True)
    parser.add_argument("--carrier-scac", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--hero-query-id", required=True)
    parser.add_argument("--dispute-date", type=date.fromisoformat, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--dsn-env", default="TALLY_GATE2_CRDB_DSN")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error("required DSN environment variable is not set")
    return RunnerConfig(
        dsn, args.fixture, args.source_inventory, args.carrier_scac, args.lane,
        args.hero_query_id, args.dispute_date, args.private_output,
    )


def main(argv: Sequence[str] | None = None) -> int:
    summary = run(parse_args(argv))
    print(json.dumps(summary.as_dict(), sort_keys=True))
    return 0 if summary.passed else 1


if __name__ == "__main__":
    sys.exit(main())
