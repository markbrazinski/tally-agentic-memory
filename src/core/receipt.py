"""Pure Gate 1 receipt logic: verify retained bytes, then calculate.

The model may propose a tariff extraction, but only these deterministic checks
decide whether it can be used. No function branches on a carrier, filename,
case identifier, or expected hero amount.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SUPPORTED_CURRENCIES = {"USD"}
SUPPORTED_RATE_UNITS = {"per_day"}


def canonical_json_bytes(value: Any) -> bytes:
    """Version-stable JSON bytes used for manifests and their hashes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prefixed_sha256(data: bytes) -> str:
    return f"sha256:{sha256_hex(data)}"


def normalize_source_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class TariffExtraction:
    rate_amount: Decimal
    rate_currency: str
    rate_unit: str
    rate_text: str
    effective_from: date
    effective_to: date | None
    clause_text: str
    source_locator: str
    confidence: Decimal

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "TariffExtraction":
        required = {
            "rate_amount",
            "rate_currency",
            "rate_unit",
            "rate_text",
            "effective_from",
            "effective_to",
            "clause_text",
            "source_locator",
            "confidence",
        }
        missing = sorted(required - set(value))
        if missing:
            raise ValueError(f"missing tariff extraction fields: {', '.join(missing)}")
        try:
            return cls(
                rate_amount=Decimal(str(value["rate_amount"])),
                rate_currency=str(value["rate_currency"]).upper(),
                rate_unit=str(value["rate_unit"]),
                rate_text=str(value["rate_text"]),
                effective_from=date.fromisoformat(str(value["effective_from"])),
                effective_to=(
                    date.fromisoformat(str(value["effective_to"]))
                    if value["effective_to"] is not None
                    else None
                ),
                clause_text=str(value["clause_text"]),
                source_locator=str(value["source_locator"]),
                confidence=Decimal(str(value["confidence"])),
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid tariff extraction: {exc}") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate_amount": format(self.rate_amount, ".2f"),
            "rate_currency": self.rate_currency,
            "rate_unit": self.rate_unit,
            "rate_text": self.rate_text,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "clause_text": self.clause_text,
            "source_locator": self.source_locator,
            "confidence": str(self.confidence),
        }


@dataclass(frozen=True)
class TariffVerification:
    eligible: bool
    reasons: tuple[str, ...]
    source_sha256: str
    clause_sha256: str

    @property
    def reason(self) -> str:
        return "verified" if self.eligible else "; ".join(self.reasons)


def _amount_from_rate_text(rate_text: str) -> Decimal | None:
    match = re.search(r"(?<!\d)(\d[\d,]*(?:\.\d{1,2})?)(?!\d)", rate_text)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def verify_tariff_extraction(
    source_bytes: bytes, extraction: TariffExtraction
) -> TariffVerification:
    """Verify model output against exact retained bytes; abstain on any fault."""
    source_sha = sha256_hex(source_bytes)
    clause_sha = sha256_hex(extraction.clause_text.encode("utf-8"))
    reasons: list[str] = []
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return TariffVerification(
            eligible=False,
            reasons=("source_not_utf8",),
            source_sha256=source_sha,
            clause_sha256=clause_sha,
        )

    normalized_source = normalize_source_text(source_text)
    normalized_clause = normalize_source_text(extraction.clause_text)
    normalized_rate_text = normalize_source_text(extraction.rate_text)

    if not normalized_clause or normalized_clause not in normalized_source:
        reasons.append("clause_absent_from_source")
    if not normalized_rate_text or normalized_rate_text not in normalized_clause:
        reasons.append("rate_text_absent_from_clause")

    text_amount = _amount_from_rate_text(extraction.rate_text)
    if text_amount is None or text_amount != extraction.rate_amount:
        reasons.append("rate_amount_mismatch")

    if extraction.rate_currency not in SUPPORTED_CURRENCIES:
        reasons.append("unsupported_currency")
    elif not re.search(r"(?:\bUSD\b|\$)", extraction.rate_text, re.IGNORECASE):
        reasons.append("currency_absent_from_rate_text")

    if extraction.rate_unit not in SUPPORTED_RATE_UNITS:
        reasons.append("unsupported_rate_unit")
    elif not re.search(r"(?:/\s*day\b|\bper\s+day\b)", extraction.rate_text, re.IGNORECASE):
        reasons.append("unit_absent_from_rate_text")

    if extraction.effective_to and extraction.effective_to < extraction.effective_from:
        reasons.append("invalid_effective_interval")
    if not extraction.source_locator.strip():
        reasons.append("missing_source_locator")
    if extraction.confidence < 0 or extraction.confidence > 1:
        reasons.append("confidence_out_of_range")

    return TariffVerification(
        eligible=not reasons,
        reasons=tuple(reasons),
        source_sha256=source_sha,
        clause_sha256=clause_sha,
    )


@dataclass(frozen=True)
class InvoiceClaim:
    invoice_no: str
    claimed_rate: Decimal
    rate_currency: str
    rate_unit: str
    charge_days: int
    invoice_date: date
    received_at: str


def parse_invoice_claim(source_bytes: bytes) -> InvoiceClaim:
    """Read a structured invoice claim from retained JSON bytes.

    The parser validates a generic schema; it has no fixture-, carrier-, or
    amount-specific branch.
    """
    try:
        value = json.loads(source_bytes)
        rate = value["claimed_rate"]
        claim = InvoiceClaim(
            invoice_no=str(value["invoice_no"]),
            claimed_rate=Decimal(str(rate["amount"])),
            rate_currency=str(rate["currency"]).upper(),
            rate_unit=str(rate["unit"]),
            charge_days=int(value["charge_days"]),
            invoice_date=date.fromisoformat(str(value["invoice_date"])),
            received_at=str(value["received_at"]),
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid retained invoice claim: {exc}") from exc
    if claim.rate_currency not in SUPPORTED_CURRENCIES:
        raise ValueError("unsupported invoice currency")
    if claim.rate_unit not in SUPPORTED_RATE_UNITS:
        raise ValueError("unsupported invoice rate unit")
    if claim.charge_days <= 0:
        raise ValueError("charge_days must be positive")
    return claim


@dataclass(frozen=True)
class OverchargeCalculation:
    recorded_rate: Decimal
    claimed_rate: Decimal
    charge_days: int
    overcharge: Decimal
    recommendation: str

    @property
    def should_file(self) -> bool:
        return self.overcharge > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded_rate": format(self.recorded_rate, ".2f"),
            "claimed_rate": format(self.claimed_rate, ".2f"),
            "charge_days": self.charge_days,
            "overcharge": format(self.overcharge, ".2f"),
            "recommendation": self.recommendation,
        }


def calculate_overcharge(
    *, recorded_rate: Decimal, claimed_rate: Decimal, charge_days: int
) -> OverchargeCalculation:
    if charge_days <= 0:
        raise ValueError("charge_days must be positive")
    difference = (claimed_rate - recorded_rate) * charge_days
    overcharge = max(Decimal("0.00"), difference).quantize(Decimal("0.01"))
    recommendation = "dispute_overcharge" if overcharge else "no_overcharge"
    return OverchargeCalculation(
        recorded_rate=recorded_rate,
        claimed_rate=claimed_rate,
        charge_days=charge_days,
        overcharge=overcharge,
        recommendation=recommendation,
    )
