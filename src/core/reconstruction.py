"""Pure domain contracts and validators for Gate 2 sourced reconstruction.

Zero external I/O. Everything a reconstruction needs to decide — cutoff
eligibility, time-domain consistency, allowlisted event types, deterministic
free-time/gate-out boundary policy, per-day coverage, and the frozen downstream
projection consumed by Gate 3/4 — is computed here and exhaustively unit-tested.

Money is integer minor units + ISO currency (backend plan §4.1). The model never
decides a verdict; Python classifies coverage and the Decision Engine (Gate 4)
owns the final recommendation. A returned MCP row cannot become a fact without a
verified exact source binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class ReconstructionState(StrEnum):
    QUEUED = "QUEUED"
    RETRIEVING_MEMORY = "RETRIEVING_MEMORY"
    ASSEMBLING_EVENTS = "ASSEMBLING_EVENTS"
    ADJUDICATING_DAYS = "ADJUDICATING_DAYS"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    BLOCKED_MEMORY_UNAVAILABLE = "BLOCKED_MEMORY_UNAVAILABLE"
    BLOCKED_SOURCE_GAP = "BLOCKED_SOURCE_GAP"
    FAILED = "FAILED"


class ShipmentEventType(StrEnum):
    DISCHARGED = "DISCHARGED"
    AVAILABLE = "AVAILABLE"
    FREE_TIME_START = "FREE_TIME_START"
    FREE_TIME_END = "FREE_TIME_END"
    GATE_OUT = "GATE_OUT"


# The five event types the locked hero timeline requires. An MCP row whose type
# is outside this set is rejected, never coerced.
ALLOWED_EVENT_TYPES = frozenset(t.value for t in ShipmentEventType)


class ChargedDayState(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    SOURCING = "SOURCING"
    SOURCE_COMPLETE = "SOURCE_COMPLETE"
    SUPPORTED = "SUPPORTED"
    RATE_DISCREPANCY = "RATE_DISCREPANCY"
    OPERATIONAL_EXCEPTION = "OPERATIONAL_EXCEPTION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    EXCLUDED_NOT_CHARGEABLE = "EXCLUDED_NOT_CHARGEABLE"


class CoverageState(StrEnum):
    PRESENT_VERIFIED = "PRESENT_VERIFIED"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTED = "CONFLICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EventUseState(StrEnum):
    USED = "USED"
    CONTEXT_ONLY = "CONTEXT_ONLY"
    REJECTED = "REJECTED"


# Invoice-level source-coverage requirement classes (categorical, not confidence).
COVERAGE_REQUIREMENTS = (
    "INVOICE_SOURCE",
    "CONTAINER_IDENTITY",
    "CHARGED_DATES",
    "INVOICE_RATE",
    "AVAILABILITY",
    "FREE_TIME_START",
    "FREE_TIME_END",
    "GATE_OUT",
)

# Which shipment event type satisfies which coverage requirement.
_REQUIREMENT_EVENT_TYPE = {
    "AVAILABILITY": ShipmentEventType.AVAILABLE,
    "FREE_TIME_START": ShipmentEventType.FREE_TIME_START,
    "FREE_TIME_END": ShipmentEventType.FREE_TIME_END,
    "GATE_OUT": ShipmentEventType.GATE_OUT,
}


@dataclass(frozen=True)
class NormalizedEvent:
    """A validated, source-bound pre-invoice event ready to persist.

    Times are distinct domains: ``occurred_at`` is when it happened,
    ``recorded_at`` is when Tally stored the source, ``observed_at`` is when the
    source system observed it (optional). ``recorded_before_cutoff`` is derived.
    """

    public_ref: str
    event_type: ShipmentEventType
    shipment_ref: str
    container_ref: str
    source_public_ref: str
    display_anchor: str
    provenance_classification: str
    occurred_at: datetime
    recorded_at: datetime
    observed_at: datetime | None
    effective_from: date | None
    effective_to: date | None
    normalized_facts: dict


@dataclass(frozen=True)
class RawEventRow:
    """One row as returned by the Managed MCP reconstruction read.

    All fields are strings/None exactly as MCP hands them back; validation and
    time parsing happen in :func:`validate_events`, never in the adapter.
    """

    public_ref: str
    event_type: str
    shipment_ref: str
    container_ref: str
    source_public_ref: str
    source_verification_state: str
    display_anchor: str
    provenance_classification: str
    occurred_at: str
    recorded_at: str
    observed_at: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    normalized_facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EventValidation:
    accepted: tuple[NormalizedEvent, ...]
    rejected: tuple[tuple[str, str], ...]  # (public_ref, issue_code)

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(code for _, code in self.rejected)


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return parsed


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(value)


def validate_events(
    rows: list[RawEventRow],
    *,
    knowledge_cutoff: datetime,
    shipment_ref: str,
    container_ref: str,
) -> EventValidation:
    """Deterministically validate MCP rows into accepted/rejected events.

    An event is rejected (not silently dropped) when it fails any rule:
    unverified source binding, non-allowlisted type, cross-shipment/container,
    missing/naive/malformed timestamps, or — the knowledge-cutoff rule — a
    ``recorded_at`` after the invoice's ``received_at`` cutoff. Duplicate
    ``(event_type, occurred_at)`` collapses to the first-seen, rest rejected.
    """
    accepted: list[NormalizedEvent] = []
    rejected: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for row in rows:
        ref = row.public_ref
        if row.source_verification_state != "VERIFIED":
            rejected.append((ref, "EVENT_SOURCE_MISSING"))
            continue
        if row.event_type not in ALLOWED_EVENT_TYPES:
            rejected.append((ref, "EVENT_TYPE_NOT_ALLOWED"))
            continue
        if row.shipment_ref != shipment_ref or row.container_ref != container_ref:
            rejected.append((ref, "EVENT_OWNERSHIP_MISMATCH"))
            continue
        try:
            occurred = _parse_dt(row.occurred_at)
            recorded = _parse_dt(row.recorded_at)
            observed = _parse_dt(row.observed_at) if row.observed_at else None
            effective_from = _parse_date(row.effective_from)
            effective_to = _parse_date(row.effective_to)
        except ValueError:
            rejected.append((ref, "EVENT_TIME_INVALID"))
            continue
        # Knowledge-cutoff rule: no event learned after the invoice arrived.
        if recorded > knowledge_cutoff:
            rejected.append((ref, "EVENT_AFTER_KNOWLEDGE_CUTOFF"))
            continue
        dedupe_key = (row.event_type, occurred.isoformat())
        if dedupe_key in seen:
            rejected.append((ref, "EVENT_DUPLICATE"))
            continue
        seen.add(dedupe_key)
        accepted.append(
            NormalizedEvent(
                public_ref=ref,
                event_type=ShipmentEventType(row.event_type),
                shipment_ref=row.shipment_ref,
                container_ref=row.container_ref,
                source_public_ref=row.source_public_ref,
                display_anchor=row.display_anchor,
                provenance_classification=row.provenance_classification,
                occurred_at=occurred,
                recorded_at=recorded,
                observed_at=observed,
                effective_from=effective_from,
                effective_to=effective_to,
                normalized_facts=dict(row.normalized_facts),
            )
        )

    accepted.sort(key=lambda event: (event.occurred_at, event.public_ref))
    return EventValidation(tuple(accepted), tuple(rejected))


@dataclass(frozen=True)
class ChargeBoundary:
    """Deterministic, versioned free-time / gate-out boundary for the hero."""

    first_chargeable_date: date
    last_chargeable_date: date


# The scenario's declared business timezone. Calendar-day math (free-time and
# gate-out boundaries, charged days) is computed in THIS zone, never by
# truncating a UTC timestamp to a date — commission §5.2. A UTC-stored
# 2026-06-07T23:59-07:00 is 06-08T06:59Z; naive .date() would wrongly read it
# as June 8 and shift the whole chargeable window.
EFFECTIVE_TIMEZONE = "America/Los_Angeles"


def _local_date(value: datetime, tz_name: str = EFFECTIVE_TIMEZONE) -> date:
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo(tz_name)).date()


def resolve_charge_boundary(
    events: tuple[NormalizedEvent, ...],
    *,
    tz_name: str = EFFECTIVE_TIMEZONE,
) -> ChargeBoundary | None:
    """Locked boundary policy (backend plan / commission §7.3):

    first chargeable date = calendar day AFTER free_time_end;
    last chargeable date  = gate_out calendar date, inclusive.

    Dates are the scenario-timezone calendar dates of the events, never UTC
    truncations. Returns ``None`` if the source events do not exactly establish
    both bounds — the caller then records a gap rather than inferring a boundary.
    """
    free_time_end = next(
        (e for e in events if e.event_type is ShipmentEventType.FREE_TIME_END), None
    )
    gate_out = next(
        (e for e in events if e.event_type is ShipmentEventType.GATE_OUT), None
    )
    if free_time_end is None or gate_out is None:
        return None
    from datetime import timedelta

    first = _local_date(free_time_end.occurred_at, tz_name) + timedelta(days=1)
    last = _local_date(gate_out.occurred_at, tz_name)
    if last < first:
        return None
    return ChargeBoundary(first, last)


@dataclass(frozen=True)
class ChargedDayResult:
    charge_date: date
    state: ChargedDayState
    chargeability: str
    coverage_state: CoverageState
    invoice_rate_minor: int
    currency: str
    event_refs: tuple[str, ...]
    missing_requirements: tuple[str, ...]


def adjudicate_charged_days(
    *,
    charge_dates: list[date],
    invoice_rate_minor: int,
    currency: str,
    events: tuple[NormalizedEvent, ...],
    boundary: ChargeBoundary | None,
) -> tuple[ChargedDayResult, ...]:
    """Assign each billed date its coverage state and bound events.

    Gate 2 establishes SOURCE_COMPLETE vs INSUFFICIENT_EVIDENCE; it does NOT set
    the financial outcome (SUPPORTED/RATE_DISCREPANCY) — that needs the Gate 3
    applicable rate and is Gate 4's job. A day is SOURCE_COMPLETE only when the
    required availability/free-time-end/gate-out boundary events are present and
    the date falls within [first_chargeable, last_chargeable].
    """
    availability = [
        e for e in events if e.event_type is ShipmentEventType.AVAILABLE
    ]
    free_time_end = [
        e for e in events if e.event_type is ShipmentEventType.FREE_TIME_END
    ]
    gate_out = [e for e in events if e.event_type is ShipmentEventType.GATE_OUT]

    results: list[ChargedDayResult] = []
    for charge_date in charge_dates:
        missing: list[str] = []
        if not availability:
            missing.append("AVAILABILITY")
        if not free_time_end:
            missing.append("FREE_TIME_END")
        if not gate_out:
            missing.append("GATE_OUT")

        in_window = (
            boundary is not None
            and boundary.first_chargeable_date <= charge_date <= boundary.last_chargeable_date
        )
        if not missing and boundary is not None and not in_window:
            # A billed date outside the sourced chargeable window is excluded,
            # not silently disputed.
            results.append(
                ChargedDayResult(
                    charge_date=charge_date,
                    state=ChargedDayState.EXCLUDED_NOT_CHARGEABLE,
                    chargeability="NOT_CHARGEABLE",
                    coverage_state=CoverageState.PRESENT_VERIFIED,
                    invoice_rate_minor=invoice_rate_minor,
                    currency=currency,
                    event_refs=_boundary_refs(availability, free_time_end, gate_out),
                    missing_requirements=(),
                )
            )
            continue
        if missing or boundary is None:
            results.append(
                ChargedDayResult(
                    charge_date=charge_date,
                    state=ChargedDayState.INSUFFICIENT_EVIDENCE,
                    chargeability="UNRESOLVED",
                    coverage_state=CoverageState.MISSING,
                    invoice_rate_minor=invoice_rate_minor,
                    currency=currency,
                    event_refs=_boundary_refs(availability, free_time_end, gate_out),
                    missing_requirements=tuple(missing) or ("CHARGE_BOUNDARY",),
                )
            )
            continue
        results.append(
            ChargedDayResult(
                charge_date=charge_date,
                state=ChargedDayState.SOURCE_COMPLETE,
                chargeability="CHARGEABLE",
                coverage_state=CoverageState.PRESENT_VERIFIED,
                invoice_rate_minor=invoice_rate_minor,
                currency=currency,
                event_refs=_boundary_refs(availability, free_time_end, gate_out),
                missing_requirements=(),
            )
        )
    return tuple(results)


def _boundary_refs(*event_groups: list[NormalizedEvent]) -> tuple[str, ...]:
    refs: list[str] = []
    for group in event_groups:
        refs.extend(e.public_ref for e in group)
    return tuple(refs)


def classify_coverage(
    *,
    events: tuple[NormalizedEvent, ...],
    have_invoice_source: bool,
    have_container_identity: bool,
    have_charged_dates: bool,
    have_invoice_rate: bool,
) -> dict[str, CoverageState]:
    """Categorical coverage per requirement class. Never a confidence score."""
    present_types = {e.event_type for e in events}
    coverage: dict[str, CoverageState] = {
        "INVOICE_SOURCE": _present(have_invoice_source),
        "CONTAINER_IDENTITY": _present(have_container_identity),
        "CHARGED_DATES": _present(have_charged_dates),
        "INVOICE_RATE": _present(have_invoice_rate),
    }
    for requirement, event_type in _REQUIREMENT_EVENT_TYPE.items():
        coverage[requirement] = _present(event_type in present_types)
    return coverage


def _present(flag: bool) -> CoverageState:
    return CoverageState.PRESENT_VERIFIED if flag else CoverageState.MISSING


def resolve_terminal_state(
    days: tuple[ChargedDayResult, ...],
) -> ReconstructionState:
    """A reconstruction is COMPLETE only if every charged day is SOURCE_COMPLETE.

    Any insufficient day → NEEDS_EVIDENCE (never a silent high-confidence pass);
    an empty day set → NEEDS_EVIDENCE.
    """
    if not days:
        return ReconstructionState.NEEDS_EVIDENCE
    if all(d.state is ChargedDayState.SOURCE_COMPLETE for d in days):
        return ReconstructionState.COMPLETE
    return ReconstructionState.NEEDS_EVIDENCE
