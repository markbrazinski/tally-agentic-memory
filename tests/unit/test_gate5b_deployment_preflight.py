from __future__ import annotations

import json

from scripts.gate5b_deployment_preflight import run_preflight
from src.external.oauth_tokens import OAuthTokenError, TokenBundle


class Store:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def load(self):
        if self.fail:
            raise OAuthTokenError("oauth_token_store_read_failed")
        return TokenBundle(
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
            token_type="Bearer",
            scopes=frozenset({"mcp:read"}),
            expires_at=4_600,
            token_endpoint="https://cockroachlabs.cloud/mcp/oauth/token",
            client_id="synthetic-client",
            resource="https://cockroachlabs.cloud/mcp",
        )


def _evidence(tmp_path, *, immediate=True, soak=True):
    immediate_path = tmp_path / "immediate.private.json"
    soak_path = tmp_path / "soak.private.json"
    immediate_path.write_text(
        json.dumps(
            {
                "claim": "OBSERVED-LIVE",
                "tokens_retained_in_artifact": False,
                "summary": {
                    "passed_immediate_phase": immediate,
                    "refresh_grants_succeeded": 2,
                    "rotation_observed": True,
                },
            }
        )
    )
    soak_path.write_text(
        json.dumps(
            {
                "claim": "OBSERVED-LIVE",
                "tokens_retained_in_artifact": False,
                "summary": {
                    "passed": soak,
                    "near_expiry_refresh_triggered": True,
                    "rotation_observed": True,
                },
            }
        )
    )
    return immediate_path, soak_path


def test_completed_private_evidence_and_renewable_bundle_pass(tmp_path):
    immediate, soak = _evidence(tmp_path)
    result = run_preflight(
        store=Store(), immediate_path=immediate, soak_path=soak, now=1_000
    )
    assert result.passed is True
    assert result.renewable_bundle_available is True


def test_missing_soak_or_bundle_fails_closed_without_secret_detail(tmp_path):
    immediate, soak = _evidence(tmp_path, soak=False)
    result = run_preflight(
        store=Store(), immediate_path=immediate, soak_path=soak, now=1_000
    )
    assert result.passed is False
    assert result.error_code == "gate5b_evidence_incomplete"

    failed = run_preflight(
        store=Store(fail=True), immediate_path=immediate, soak_path=soak, now=1_000
    )
    assert failed.error_code == "gate5b_preflight_failed"
    assert "synthetic-refresh" not in str(failed)
