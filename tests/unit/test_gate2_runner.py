"""Gate 2 runner safety tests; all external paths are replaced with fakes."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest

from scripts.gate2_retrieval import (
    Gate2RunnerError,
    InventoryEntry,
    PublicSummary,
    _receipt_counts,
    _require_invoice_template,
    load_inventory,
    sanitize_plan_lines,
    write_private_output,
)
from src.external.versioned_source import RetainedObject


class _CaptureDAL:
    def __init__(self):
        self.call = None

    def execute(self, sql, params=(), **kwargs):
        self.call = (sql, params, kwargs)
        return [(1, 1, 1, 1)]


def test_inventory_requires_private_capture_and_invoice_bindings(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(
        json.dumps(
            {
                "tariff:capture-a": {"bucket": "b", "key": "k", "version_id": "v"},
                "invoice": {"bucket": "b", "key": "i", "version_id": "iv"},
            }
        )
    )

    captures, invoice = load_inventory(path)

    assert captures == {"tariff:capture-a": InventoryEntry("b", "k", "v")}
    assert invoice == InventoryEntry("b", "i", "iv")


@pytest.mark.parametrize(
    "value", [{}, {"invoice": {}}, {"tariff:capture-a": [], "invoice": {}}]
)
def test_inventory_rejects_missing_or_malformed_bindings(tmp_path, value):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(value))

    with pytest.raises(Gate2RunnerError):
        load_inventory(path)


def test_sanitize_plan_lines_retains_only_structural_facts():
    lines = sanitize_plan_lines(
        [
            "scan tariff_clause_embedding_search_idx key='private/key' id 12345",
            "render distance <-> double-quoted-private-value",
        ]
    )

    assert lines == [
        "plan_line=1;node=scan;named_vector_index=true;vector_distance=false",
        "plan_line=2;node=render;named_vector_index=false;vector_distance=true",
    ]
    assert "private" not in repr(lines)


def test_private_output_is_mode_0600_and_does_not_require_stdout(tmp_path):
    path = tmp_path / "private" / "gate2.json"
    summary = PublicSummary(True, True, True, True, True, True, 4, 2, 2)

    write_private_output(
        path,
        {"summary": summary.as_dict(), "sanitized_plan_lines": ["scan"]},
        private_root=tmp_path,
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["summary"]["passed"] is True


def test_private_output_overwrites_with_restricted_permissions(tmp_path):
    path = tmp_path / "private.json"
    path.write_text("old")
    path.chmod(0o644)

    write_private_output(path, {"stage": "redacted"}, private_root=tmp_path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"stage": "redacted"}


def test_private_output_rejects_paths_outside_the_dedicated_root(tmp_path):
    with pytest.raises(ValueError, match="runtime-artifacts"):
        write_private_output(
            tmp_path / "public" / "evidence.json",
            {"stage": "redacted"},
            private_root=tmp_path / "private",
        )


def test_receipt_count_query_uses_one_injected_tenant_and_one_invoice_parameter():
    dal = _CaptureDAL()

    assert _receipt_counts(dal, "20000000-0000-4000-8000-000000000001") == (1, 1, 1, 1)

    sql, params, _ = dal.call
    assert sql.count("%s") == 2
    assert params == ("20000000-0000-4000-8000-000000000001",)


def test_failure_stage_is_not_stored_in_a_numeric_metric():
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
        failure_stage="seed",
    )

    assert summary.raw_top1_count == 0
    assert summary.as_dict()["failure_stage"] == "seed"


def test_dynamic_invoice_matches_template_and_follows_source_observations():
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    template = b'{"classification":"synthetic demonstration data",'
    template += b'"invoice_no":"SYN-1","received_at":"<assigned-at-private-upload>"}'
    actual = b'{"classification":"synthetic demonstration data",'
    actual += b'"invoice_no":"SYN-1","received_at":"2026-01-02T00:00:00Z"}'
    tariff = RetainedObject("b", "t", "v1", b"tariff", observed)
    invoice = RetainedObject("b", "i", "v2", actual, observed + timedelta(seconds=1))

    _require_invoice_template(invoice, template, [tariff])


def test_dynamic_invoice_rejects_receipt_before_authoritative_observation():
    observed = datetime(2026, 1, 2, tzinfo=UTC)
    template = b'{"classification":"synthetic demonstration data",'
    template += b'"received_at":"<assigned-at-private-upload>"}'
    actual = b'{"classification":"synthetic demonstration data",'
    actual += b'"received_at":"2026-01-01T00:00:00Z"}'
    tariff = RetainedObject("b", "t", "v1", b"tariff", observed)
    invoice = RetainedObject("b", "i", "v2", actual, observed)

    with pytest.raises(Gate2RunnerError, match="received_before"):
        _require_invoice_template(invoice, template, [tariff])
