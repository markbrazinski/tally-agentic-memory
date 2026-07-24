"""Pure-domain tests for Gate 2 sourced reconstruction (zero I/O).

Covers the commission acceptance points that live in pure logic: knowledge
cutoff (RE-05), recorded-before-invoice (RE-06), time-domain distinction
(RE-07), seven charged days (RE-08), the locked free-time/gate-out boundary
(§13.3 June 7 free-time end / June 8 first charged / June 14 gate-out), and the
named negatives (post-cutoff, ownership, duplicate, missing source, malformed
time, insufficient coverage).
"""

from __future__ import annotations

from datetime import date, datetime

from src.core.reconstruction import (
    ChargedDayState,
    CoverageState,
    RawEventRow,
    ReconstructionState,
    ShipmentEventType,
    adjudicate_charged_days,
    classify_coverage,
    resolve_charge_boundary,
    resolve_terminal_state,
    validate_events,
)

CUTOFF = datetime.fromisoformat("2026-06-22T08:00:00+00:00")  # invoice received_at
SHIP = "TLLU4829317"
CONT = "TLLU4829317"


def _row(
    ref: str,
    event_type: str,
    occurred: str,
    *,
    recorded: str = "2026-06-20T00:00:00+00:00",
    verified: str = "VERIFIED",
    shipment: str = SHIP,
    container: str = CONT,
) -> RawEventRow:
    return RawEventRow(
        public_ref=ref,
        event_type=event_type,
        shipment_ref=shipment,
        container_ref=container,
        source_public_ref=f"SRC-{ref}",
        source_verification_state=verified,
        display_anchor=f"row {ref}",
        provenance_classification="DEMO_SCENARIO",
        occurred_at=occurred,
        recorded_at=recorded,
    )


def _hero_rows() -> list[RawEventRow]:
    return [
        _row("SE-001", "DISCHARGED", "2026-06-02T15:00:00+00:00"),
        _row("SE-002", "AVAILABLE", "2026-06-03T09:00:00+00:00"),
        _row("SE-003", "FREE_TIME_START", "2026-06-03T09:00:00+00:00"),
        _row("SE-004", "FREE_TIME_END", "2026-06-07T23:59:00+00:00"),
        _row("SE-005", "GATE_OUT", "2026-06-14T17:00:00+00:00"),
    ]


def _validate(rows):
    return validate_events(
        rows, knowledge_cutoff=CUTOFF, shipment_ref=SHIP, container_ref=CONT
    )


def test_hero_events_all_accepted_and_ordered():
    v = _validate(_hero_rows())
    assert not v.rejected
    assert [e.event_type for e in v.accepted] == [
        ShipmentEventType.DISCHARGED,
        ShipmentEventType.AVAILABLE,
        ShipmentEventType.FREE_TIME_START,
        ShipmentEventType.FREE_TIME_END,
        ShipmentEventType.GATE_OUT,
    ]
    # RE-06: recorded before the invoice cutoff.
    assert all(e.recorded_at <= CUTOFF for e in v.accepted)


def test_event_after_knowledge_cutoff_is_rejected_not_labelled():
    # RE-05 / §13.1: an event recorded AFTER the cutoff cannot enter.
    late = _row("SE-LATE", "GATE_OUT", "2026-06-14T17:00:00+00:00",
                recorded="2026-06-23T00:00:00+00:00")
    v = _validate([*_hero_rows()[:4], late])
    assert ("SE-LATE", "EVENT_AFTER_KNOWLEDGE_CUTOFF") in v.rejected
    assert all(e.public_ref != "SE-LATE" for e in v.accepted)


def test_unverified_source_binding_rejects_event():
    row = _row("SE-BAD", "AVAILABLE", "2026-06-03T09:00:00+00:00", verified="UNAVAILABLE")
    v = _validate([row])
    assert v.rejected == (("SE-BAD", "EVENT_SOURCE_MISSING"),)
    assert not v.accepted


def test_cross_shipment_or_container_rejected():
    row = _row("SE-X", "AVAILABLE", "2026-06-03T09:00:00+00:00", container="MSCU0000000")
    v = _validate([row])
    assert v.rejected == (("SE-X", "EVENT_OWNERSHIP_MISMATCH"),)


def test_non_allowlisted_event_type_rejected():
    row = _row("SE-Y", "CUSTOMS_HOLD", "2026-06-03T09:00:00+00:00")
    v = _validate([row])
    assert v.rejected == (("SE-Y", "EVENT_TYPE_NOT_ALLOWED"),)


def test_naive_or_malformed_timestamp_rejected():
    naive = _row("SE-N", "AVAILABLE", "2026-06-03T09:00:00")  # no tz
    v = _validate([naive])
    assert v.rejected == (("SE-N", "EVENT_TIME_INVALID"),)


def test_duplicate_same_type_and_time_collapses():
    rows = _hero_rows()
    dup = _row("SE-DUP", "GATE_OUT", "2026-06-14T17:00:00+00:00")
    v = _validate([*rows, dup])
    assert ("SE-DUP", "EVENT_DUPLICATE") in v.rejected
    assert sum(1 for e in v.accepted if e.event_type is ShipmentEventType.GATE_OUT) == 1


def test_time_domains_are_distinct():
    # RE-07: occurred_at vs recorded_at are independent domains.
    row = _row("SE-T", "AVAILABLE", "2026-06-03T09:00:00+00:00",
               recorded="2026-06-05T12:00:00+00:00")
    v = _validate([row])
    event = v.accepted[0]
    assert event.occurred_at != event.recorded_at
    assert event.occurred_at.date() == date(2026, 6, 3)
    assert event.recorded_at.date() == date(2026, 6, 5)


def test_boundary_policy_locked_dates():
    # §13.3: free-time end June 7 -> first charged June 8; gate-out June 14 inclusive.
    v = _validate(_hero_rows())
    boundary = resolve_charge_boundary(v.accepted)
    assert boundary is not None
    assert boundary.first_chargeable_date == date(2026, 6, 8)
    assert boundary.last_chargeable_date == date(2026, 6, 14)


def test_boundary_uses_scenario_timezone_not_utc():
    # Regression: a Pacific 2026-06-07T23:59-07:00 free-time-end is 06-08T06:59Z.
    # Naive UTC .date() would read June 8 and push first-chargeable to June 9,
    # dropping June 8 from the window. Business-day math must use the scenario tz.
    rows = [
        _row("SE-FTE", "FREE_TIME_END", "2026-06-07T23:59:00-07:00",
             recorded="2026-06-07T23:59:00-07:00"),
        _row("SE-GO", "GATE_OUT", "2026-06-14T17:00:00-07:00",
             recorded="2026-06-14T18:00:00-07:00"),
    ]
    v = _validate(rows)
    boundary = resolve_charge_boundary(v.accepted)
    assert boundary is not None
    assert boundary.first_chargeable_date == date(2026, 6, 8)
    assert boundary.last_chargeable_date == date(2026, 6, 14)


def test_boundary_none_when_bounds_missing():
    partial = [r for r in _hero_rows() if r.event_type != "GATE_OUT"]
    v = _validate(partial)
    assert resolve_charge_boundary(v.accepted) is None


def _hero_charge_dates():
    return [date(2026, 6, d) for d in range(8, 15)]  # June 8..14 inclusive = 7 days


def test_seven_days_all_source_complete():
    # RE-08 / RE-11: seven charged days, each SOURCE_COMPLETE with bound events.
    v = _validate(_hero_rows())
    boundary = resolve_charge_boundary(v.accepted)
    days = adjudicate_charged_days(
        charge_dates=_hero_charge_dates(),
        invoice_rate_minor=35000,
        currency="USD",
        events=v.accepted,
        boundary=boundary,
    )
    assert len(days) == 7
    assert all(d.state is ChargedDayState.SOURCE_COMPLETE for d in days)
    assert all(d.coverage_state is CoverageState.PRESENT_VERIFIED for d in days)
    assert resolve_terminal_state(days) is ReconstructionState.COMPLETE


def test_missing_gate_out_yields_insufficient_days():
    partial = [r for r in _hero_rows() if r.event_type != "GATE_OUT"]
    v = _validate(partial)
    boundary = resolve_charge_boundary(v.accepted)
    days = adjudicate_charged_days(
        charge_dates=_hero_charge_dates(),
        invoice_rate_minor=35000,
        currency="USD",
        events=v.accepted,
        boundary=boundary,
    )
    assert all(d.state is ChargedDayState.INSUFFICIENT_EVIDENCE for d in days)
    assert all("GATE_OUT" in d.missing_requirements for d in days)
    assert resolve_terminal_state(days) is ReconstructionState.NEEDS_EVIDENCE


def test_date_outside_window_excluded_not_disputed():
    v = _validate(_hero_rows())
    boundary = resolve_charge_boundary(v.accepted)
    days = adjudicate_charged_days(
        charge_dates=[date(2026, 6, 15)],  # after gate-out
        invoice_rate_minor=35000,
        currency="USD",
        events=v.accepted,
        boundary=boundary,
    )
    assert days[0].state is ChargedDayState.EXCLUDED_NOT_CHARGEABLE


def test_empty_days_is_needs_evidence():
    assert resolve_terminal_state(()) is ReconstructionState.NEEDS_EVIDENCE


def test_coverage_classification_is_categorical():
    v = _validate(_hero_rows())
    coverage = classify_coverage(
        events=v.accepted,
        have_invoice_source=True,
        have_container_identity=True,
        have_charged_dates=True,
        have_invoice_rate=True,
    )
    assert coverage["AVAILABILITY"] is CoverageState.PRESENT_VERIFIED
    assert coverage["GATE_OUT"] is CoverageState.PRESENT_VERIFIED
    assert coverage["INVOICE_RATE"] is CoverageState.PRESENT_VERIFIED


def test_coverage_missing_events_marked_missing():
    partial = [r for r in _hero_rows() if r.event_type == "DISCHARGED"]
    v = _validate(partial)
    coverage = classify_coverage(
        events=v.accepted,
        have_invoice_source=True,
        have_container_identity=True,
        have_charged_dates=True,
        have_invoice_rate=False,
    )
    assert coverage["GATE_OUT"] is CoverageState.MISSING
    assert coverage["INVOICE_RATE"] is CoverageState.MISSING
