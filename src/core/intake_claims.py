"""Deterministic validation and PDF anchors for Intake carrier claims."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import parser as date_parser

from src.core.intake import Money

REQUIRED_CLAIM_FIELDS = (
    "invoice_number",
    "container_number",
    "bill_of_lading",
    "charge_type",
    "period_start",
    "period_end",
    "charged_days",
    "daily_rate",
    "total",
    "issued_date",
)


@dataclass(frozen=True)
class PdfAnchor:
    page_number: int
    bounding_box: dict[str, float]
    text_excerpt: str
    text_excerpt_sha256: str


@dataclass(frozen=True)
class ValidatedClaim:
    field_name: str
    value_type: str
    raw_value: str
    normalized_value: Any
    amount_minor: int | None
    currency: str | None
    anchor: PdfAnchor


@dataclass(frozen=True)
class ClaimValidation:
    claims: tuple[ValidatedClaim, ...]
    issue_codes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.issue_codes


def validate_extracted_claims(
    raw: dict[str, Any],
    pages: list[dict[str, Any]],
) -> ClaimValidation:
    raw_claims = raw.get("claims")
    if not isinstance(raw_claims, dict):
        return ClaimValidation((), ("INVALID_MODEL_SCHEMA",))

    claims: list[ValidatedClaim] = []
    issues: list[str] = []
    for field_name in REQUIRED_CLAIM_FIELDS:
        entry = raw_claims.get(field_name)
        if not isinstance(entry, dict):
            issues.append(f"MISSING_{field_name.upper()}")
            continue
        raw_value = entry.get("value")
        excerpt = entry.get("text_excerpt")
        page_number = entry.get("page_number")
        if raw_value is None or not isinstance(excerpt, str) or not excerpt.strip():
            issues.append(f"MISSING_{field_name.upper()}")
            continue
        anchor = locate_pdf_anchor(pages, page_number, excerpt)
        if anchor is None:
            issues.append(f"UNANCHORED_{field_name.upper()}")
            continue
        try:
            value_type, normalized, amount_minor, currency = _normalize_claim(
                field_name, raw_value
            )
        except (TypeError, ValueError, InvalidOperation):
            issues.append(f"INVALID_{field_name.upper()}")
            continue
        claims.append(
            ValidatedClaim(
                field_name=field_name,
                value_type=value_type,
                raw_value=str(raw_value),
                normalized_value=normalized,
                amount_minor=amount_minor,
                currency=currency,
                anchor=anchor,
            )
        )

    by_name = {claim.field_name: claim for claim in claims}
    if all(name in by_name for name in ("period_start", "period_end", "charged_days")):
        start = date.fromisoformat(by_name["period_start"].normalized_value)
        end = date.fromisoformat(by_name["period_end"].normalized_value)
        charged_days = int(by_name["charged_days"].normalized_value)
        if end < start or (end - start).days + 1 != charged_days:
            issues.append("CHARGE_PERIOD_DAY_COUNT_MISMATCH")
    if all(name in by_name for name in ("daily_rate", "charged_days", "total")):
        expected_total = (
            int(by_name["daily_rate"].amount_minor or 0)
            * int(by_name["charged_days"].normalized_value)
        )
        if expected_total != by_name["total"].amount_minor:
            issues.append("CLAIMED_TOTAL_ARITHMETIC_MISMATCH")

    return ClaimValidation(tuple(claims), tuple(sorted(set(issues))))


def locate_pdf_anchor(
    pages: list[dict[str, Any]],
    page_number: object,
    text_excerpt: str,
) -> PdfAnchor | None:
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        return None
    if page_number < 1 or page_number > len(pages):
        return None
    page = pages[page_number - 1]
    words = page.get("words") or []
    excerpt_tokens = _tokens(text_excerpt)
    if not excerpt_tokens:
        return None
    word_tokens = [_token(str(word.get("text", ""))) for word in words]
    for start in range(0, len(word_tokens) - len(excerpt_tokens) + 1):
        if word_tokens[start : start + len(excerpt_tokens)] != excerpt_tokens:
            continue
        matched = words[start : start + len(excerpt_tokens)]
        width = float(page["width"])
        height = float(page["height"])
        box = {
            "x0": min(float(word["x0"]) for word in matched) / width,
            "top": min(float(word["top"]) for word in matched) / height,
            "x1": max(float(word["x1"]) for word in matched) / width,
            "bottom": max(float(word["bottom"]) for word in matched) / height,
        }
        excerpt = " ".join(str(word["text"]) for word in matched)
        return PdfAnchor(
            page_number=page_number,
            bounding_box=box,
            text_excerpt=excerpt,
            text_excerpt_sha256=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        )
    return None


def _normalize_claim(
    field_name: str, value: object
) -> tuple[str, Any, int | None, str | None]:
    if field_name == "container_number":
        normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
        if not re.fullmatch(r"[A-Z]{4}\d{7}", normalized):
            raise ValueError("invalid container")
        return "IDENTIFIER", normalized, None, None
    if field_name == "bill_of_lading":
        normalized = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,30}", normalized):
            raise ValueError("invalid bill of lading")
        return "IDENTIFIER", normalized, None, None
    if field_name == "charge_type":
        normalized = str(value).strip().upper()
        if normalized != "DEMURRAGE":
            raise ValueError("unsupported charge type")
        return "ENUM", normalized, None, None
    if field_name in {"period_start", "period_end", "issued_date"}:
        parsed = date_parser.parse(str(value), fuzzy=False).date().isoformat()
        return "DATE", parsed, None, None
    if field_name == "charged_days":
        if isinstance(value, bool):
            raise TypeError("invalid day count")
        parsed = int(value)
        if parsed <= 0 or parsed > 90:
            raise ValueError("invalid day count")
        return "INTEGER", parsed, None, None
    if field_name in {"daily_rate", "total"}:
        amount_minor = _parse_usd_minor(value)
        money = Money(amount_minor, "USD")
        return (
            "MONEY",
            {"amount_minor": money.amount_minor, "currency": money.currency},
            money.amount_minor,
            money.currency,
        )
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("empty value")
    return "STRING", normalized, None, None


def _parse_usd_minor(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("invalid money")
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    decimal = Decimal(cleaned)
    if decimal < 0 or decimal.as_tuple().exponent < -2:
        raise ValueError("invalid money")
    return int(decimal * 100)


def _tokens(value: str) -> list[str]:
    return [token for token in (_token(item) for item in value.split()) if token]


def _token(value: str) -> str:
    return re.sub(r"[^A-Z0-9$.,-]", "", value.upper())
