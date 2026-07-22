from __future__ import annotations

import pytest

from scripts.gate5_evidence_iam_inputs import _database_dsn, derive_object_arns


def manifest():
    return {
        "evidence": [
            {
                "s3_bucket": "fictional-tariff-bucket",
                "s3_key": "synthetic/tariff.pdf",
                "s3_version_id": "synthetic-tariff-version",
                "invoice_s3_bucket": "fictional-invoice-bucket",
                "invoice_s3_key": "synthetic/invoice.pdf",
                "invoice_s3_version_id": "synthetic-invoice-version",
            }
        ]
    }


def test_exact_arns_are_derived_only_from_version_bound_manifest_items():
    assert derive_object_arns(manifest()) == (
        "arn:aws:s3:::fictional-invoice-bucket/synthetic/invoice.pdf",
        "arn:aws:s3:::fictional-tariff-bucket/synthetic/tariff.pdf",
    )


def test_database_override_preserves_connection_fields_without_rendering_them():
    dsn = _database_dsn(
        "postgresql://"
        + "synthetic-user:synthetic-password"
        + "@db.example.test:26257/old",
        "synthetic_gate5",
    )
    assert dsn.endswith("/synthetic_gate5")
    assert "@db.example.test:26257/" in dsn


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("s3_version_id", ""),
        ("invoice_s3_key", "synthetic/*"),
        ("s3_bucket", "fictional/bucket"),
    ),
)
def test_missing_version_or_wildcard_scope_fails_closed(field, value):
    value_manifest = manifest()
    value_manifest["evidence"][0][field] = value
    with pytest.raises(ValueError, match="source_binding_invalid"):
        derive_object_arns(value_manifest)
