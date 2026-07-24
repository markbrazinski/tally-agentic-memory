"""Pure seal-manifest tests: deterministic digest, staleness, request hash."""

from __future__ import annotations

from src.core.seal_manifest import (
    SealInputs,
    approval_request_hash,
    build_seal_manifest,
    is_stale,
    seal_digest,
)


def _inputs(**over):
    base = dict(
        invoice_id="inv-1", reconstruction_id="recon-1", reconstruction_version=1,
        recommendation_id="rec-1", recommendation_version=1,
        recommendation_type="DISPUTE", recommendation_digest="sha256:abc",
        disputed_amount_minor=70000, supported_amount_minor=175000,
        claimed_amount_minor=245000, currency="USD",
        applicable_rule_id="rule-1", applicable_rule_digest="sha256:rule",
        claim_set_version=1, invoice_source_ref="s3://private/inv-1@v1",
        day_judgment_digests=tuple(f"d{i}" for i in range(7)),
        approver_display="rachel.martinez", approver_kind="SYNTHETIC_DEMO",
    )
    base.update(over)
    return SealInputs(**base)


def test_manifest_binds_all_versions():
    m = build_seal_manifest(_inputs(), revision=1)
    assert m["recommendation"]["version"] == 1
    assert m["reconstruction"]["version"] == 1
    assert m["claim_set_version"] == 1
    assert m["applicable_rule"]["id"] == "rule-1"
    assert len(m["day_judgments"]) == 7
    assert m["approver"]["kind"] == "SYNTHETIC_DEMO"


def test_source_ref_is_hashed_not_exposed():
    m = build_seal_manifest(_inputs(), revision=1)
    blob = str(m)
    assert "s3://private" not in blob  # raw locator never in the manifest
    assert "invoice_source_binding_sha256" in m


def test_digest_deterministic():
    a = seal_digest(build_seal_manifest(_inputs(), revision=1))
    b = seal_digest(build_seal_manifest(_inputs(), revision=1))
    assert a == b
    assert a.startswith("sha256:")


def test_digest_changes_on_any_input_drift():
    base = seal_digest(build_seal_manifest(_inputs(), revision=1))
    assert base != seal_digest(build_seal_manifest(
        _inputs(disputed_amount_minor=60000), revision=1))
    assert base != seal_digest(build_seal_manifest(
        _inputs(recommendation_version=2), revision=1))
    assert base != seal_digest(build_seal_manifest(_inputs(), revision=2))


def test_is_stale_on_version_or_digest_move():
    assert is_stale(expected_version=1, current_version=2,
                    expected_digest="a", current_digest="a")
    assert is_stale(expected_version=1, current_version=1,
                    expected_digest="a", current_digest="b")
    assert not is_stale(expected_version=1, current_version=1,
                        expected_digest="a", current_digest="a")


def test_request_hash_conflicts_on_different_decision():
    a = approval_request_hash(recommendation_id="rec-1", recommendation_version=1,
                              recommendation_digest="d", decision="APPROVE")
    b = approval_request_hash(recommendation_id="rec-1", recommendation_version=1,
                              recommendation_digest="d", decision="REJECT")
    assert a != b
    c = approval_request_hash(recommendation_id="rec-1", recommendation_version=1,
                              recommendation_digest="d", decision="APPROVE")
    assert a == c
