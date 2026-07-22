"""Bounded, public-safe projection for the synthetic Gate 5 judge path.

The browser cannot select a tenant, case, contest, timestamp, object version,
SQL statement, or model prompt. Those values are server configuration only.
This module executes the already-verified direct replay, exact S3 receipt
verification, and Managed MCP receipt retrieval, then returns only the small
fictional projection a logged-out judge may inspect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from src.external.cockroach_mcp import (
    CockroachManagedMCP,
    ManagedMCPConfig,
    MCPAuthenticationError,
    MCPPermissionError,
)
from src.external.dal import DAL
from src.external.oauth_tokens import OAuthTokenManager
from src.platform.contest_memory import retrieve_contest_memory
from src.platform.receipt_verifier import verify_case_receipt
from src.platform.temporal_replay import replay_case

SYNTHETIC_DISCLOSURE = "SYNTHETIC DEMO — FICTIONAL DATA"
RETENTION_LANGUAGE = (
    "Versioned S3 retains the dated source artifact. Within CockroachDB's "
    "configured MVCC window, Tally can also replay the transactional case "
    "state at filing."
)


class PublicDemoUnavailableError(RuntimeError):
    """The live judge path could not produce a verified result."""

    def __init__(self, safe_code: str):
        super().__init__(safe_code)
        self.safe_code = safe_code


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise PublicDemoUnavailableError("configuration_unavailable") from exc


@dataclass(frozen=True)
class PublicDemoConfig:
    tenant_id: str
    case_id: str
    contest_id: str

    def validate(self) -> None:
        _uuid(self.tenant_id, field="tenant_id")
        _uuid(self.case_id, field="case_id")
        _uuid(self.contest_id, field="contest_id")


def unavailable_projection(code: str) -> dict[str, Any]:
    """Return a stable safe error body without private exception text."""
    return {
        "classification": SYNTHETIC_DISCLOSURE,
        "status": "unavailable",
        "error_code": code,
        "mock_fallback": False,
    }


def _public_projection(
    replay: dict[str, Any], receipt: dict[str, Any], memory_outcome: Any
) -> dict[str, Any]:
    if receipt.get("passed") is not True:
        raise PublicDemoUnavailableError("evidence_verification_failed")
    if memory_outcome.status != "found" or memory_outcome.memory is None:
        code = "mcp_memory_unavailable"
        if memory_outcome.status == "not_found":
            code = "mcp_memory_not_found"
        raise PublicDemoUnavailableError(code)

    then = replay.get("then")
    now = replay.get("now")
    tamper = replay.get("tamper_check")
    retention = replay.get("retention")
    if not all(isinstance(value, dict) for value in (then, now, tamper, retention)):
        raise PublicDemoUnavailableError("replay_validation_failed")
    if retention.get("ttl_days") != 90 or retention.get("target_queryable") is not True:
        raise PublicDemoUnavailableError("retention_validation_failed")

    memory = memory_outcome.memory
    bindings_unchanged = tamper.get("match") is True
    if not bindings_unchanged:
        raise PublicDemoUnavailableError("evidence_binding_mismatch")

    return {
        "classification": SYNTHETIC_DISCLOSURE,
        "status": "executed",
        "mock_fallback": False,
        "case": {
            "reference": "GATE5-SYNTHETIC-HERO",
            "importer": "Northstar Imports (fictional)",
            "carrier": "Asterline Demo Shipping (fictional)",
            "state": str(now.get("state")),
        },
        "replay": {
            "then": {
                "state": str(then.get("state")),
                "recorded_rate": float(then.get("tariff_rate")),
                "anchor": "stored CockroachDB filing transaction (exact value redacted)",
            },
            "now": {
                "state": str(now.get("state")),
                "recorded_rate": float(now.get("tariff_rate")),
            },
            "receipt": {
                "bindings_unchanged": True,
                "exact_versioned_s3_verified": True,
            },
            "retention": {
                "ttl_days": 90,
                "target_queryable": True,
                "language": RETENTION_LANGUAGE,
            },
        },
        "managed_mcp": {
            "status": "verified_read",
            "current_state": memory.current_state,
            "recorded_rate": float(memory.recorded_rate),
            "later_claimed_rate": float(memory.claimed_rate),
            "rate_currency": memory.rate_currency,
            "rate_unit": memory.rate_unit,
            "sealed_receipt_verified": True,
        },
        "claims": {
            "external_send": False,
            "carrier_credit": False,
            "legal_determination": False,
            "production_data": False,
        },
    }


def _retrieve_memory(
    config: PublicDemoConfig,
    *,
    access_token: str,
    mcp_factory: Callable[[str], CockroachManagedMCP],
) -> Any:
    """Run the one fixed retrieval with a manager-supplied access token."""
    with mcp_factory(access_token) as mcp:
        return retrieve_contest_memory(
            mcp,
            tenant_id=config.tenant_id,
            case_id=config.case_id,
            contest_id=config.contest_id,
        )


def _runtime_mcp(access_token: str) -> CockroachManagedMCP:
    """Build the public MCP client from OAuth-manager state only."""
    config = ManagedMCPConfig.from_env(access_token=access_token)
    if config.permission_mode != "oauth-read-only":
        raise MCPPermissionError("public MCP runtime requires OAuth read-only mode")
    return CockroachManagedMCP(config)


def run_public_demo(
    config: PublicDemoConfig,
    *,
    dal_factory: Callable[[], DAL],
    s3_client: Any,
    token_manager: OAuthTokenManager,
    mcp_factory: Callable[[str], CockroachManagedMCP] | None = None,
) -> dict[str, Any]:
    """Execute the fixed live path and return only its public-safe projection.

    The caller supplies one shared OAuth token manager.  A 401 has exactly one
    refresh/replay opportunity; every other MCP failure, including 403, stays
    unavailable without a refresh attempt.
    """
    config.validate()
    try:
        with dal_factory() as dal:
            replay = replay_case(dal, case_id=config.case_id)
        with dal_factory() as dal:
            receipt = verify_case_receipt(dal, s3_client, case_id=config.case_id)
    except PublicDemoUnavailableError:
        raise
    except Exception as exc:
        raise PublicDemoUnavailableError("cockroach_or_s3_unavailable") from exc

    factory = mcp_factory or _runtime_mcp
    try:
        access_token, _ = token_manager.access_token()
        try:
            memory = _retrieve_memory(
                config, access_token=access_token, mcp_factory=factory
            )
        except MCPAuthenticationError:
            refreshed, _ = token_manager.refresh_after_unauthorized(access_token)
            memory = _retrieve_memory(
                config,
                access_token=refreshed.access_token,
                mcp_factory=factory,
            )
    except Exception as exc:
        raise PublicDemoUnavailableError("mcp_memory_unavailable") from exc
    return _public_projection(replay, receipt, memory)
