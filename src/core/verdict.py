"""Verdict precedence + summary generation: pure functions, zero I/O.

Per CLAUDE.md: "verdicts are computed in Python, the model only writes
prose" - this is the sole place a verdict gets decided. Per TDD §4:
steps 4-6 (tariff cross-reference, MCP timeline, prose generation) are
Bundle 2 scope, so this is a reduced version of §4's full precedence
rule (missing field -> DEFECTIVE, else late -> DEFECTIVE, else ambiguity
-> NEEDS_REVIEW, else VALID) with the tariff-mismatch tier omitted,
since step 4 doesn't exist yet. The reduction is documented here, not
silent.
"""

from __future__ import annotations

from src.core.clerk_steps import FieldResult, WindowResult
from src.core.fields import FIELDS_541_6

VERDICT_DEFECTIVE = "DEFECTIVE"
VERDICT_NEEDS_REVIEW = "NEEDS_REVIEW"
VERDICT_VALID = "VALID"

_FIELD_SPEC_BY_KEY = {f.key: f for f in FIELDS_541_6}


def _cite_for_field(key: str) -> str | None:
    spec = _FIELD_SPEC_BY_KEY.get(key)
    return spec.cite if spec else None


def compute_verdict(
    field_results: tuple[FieldResult, ...], window_result: WindowResult
) -> tuple[str, str | None]:
    """Reduced precedence rule (B0-S2 scope: steps 4-6 don't exist yet,
    so no tariff-mismatch tier). Returns (verdict, cited_rule)."""
    missing = [r for r in field_results if not r.present]
    if missing:
        return VERDICT_DEFECTIVE, _cite_for_field(missing[0].key)

    if window_result.ambiguous:
        return VERDICT_NEEDS_REVIEW, None

    if window_result.within_30 is False:
        return VERDICT_DEFECTIVE, "30-day window"

    return VERDICT_VALID, None


def build_summary(verdict: str, field_results: tuple[FieldResult, ...]) -> str:
    """Deterministic placeholder for step 6's LLM-written summary (out of
    scope this session - step 6 doesn't exist yet). Never fabricates
    prose beyond what the verdict/field_results already prove."""
    if verdict == VERDICT_VALID:
        return "All 13 required fields present; charge within the 30-day window."
    missing_keys = [r.key for r in field_results if not r.present]
    if missing_keys:
        return f"Missing required field(s): {', '.join(missing_keys)}."
    return "Timing window could not be confidently determined."
