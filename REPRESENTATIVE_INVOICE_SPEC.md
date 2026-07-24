# Representative Demurrage Invoice — Data Specification

## 1. Purpose & fiction marking

This document specifies the **data content** of a single representative demurrage
invoice PDF for the Tally demo. It is the shared contract for two steps: (a) a
PDF-generation step that renders these exact strings onto a page, and (b) the
extraction-validation step in `src/core/intake_claims.py`, which must locate each
field's *source anchor* verbatim and normalize it to the *expected extraction
value* below. It is **not** a visual design — no layout pixels, colors, or fonts
are specified beyond block placement hints (§3).

The invoice encodes the locked hero facts: carrier **Seaworthy Shipping**, charge
type **Demurrage**, seven daily lines at **$350.00/day**, claimed total
**$2,450.00**, charged period **2026-06-08 → 2026-06-14** (7 consecutive calendar
days), container **TLLU-482931-7**, bill of lading **OAK-77421**. These match the
existing normalized hero at `tests/fixtures/demo/INV-1048.expected-claims.json`.

**Explicit-fiction marking (must be visibly printed on the page):**

> **Representative demonstration — fictional.** Seaworthy Shipping, all parties,
> vessels, addresses, and bank details on this document are invented for a
> product demonstration. Not a real invoice. No payment is owed.

The invoice must also carry the machine-facing classification string used by the
hero fixture, printed in the footer:
`SYNTHETIC DEMO — FICTIONAL DATA`.

---

## 2. Master field table

### Conventions used in this table

- **Anchor = the exact printed text string an extractor locates.** Anchoring is
  token-based: `locate_pdf_anchor` normalizes each token via
  `_token = re.sub(r"[^A-Z0-9$.,-]", "", value.upper())` and matches the excerpt's
  token sequence against the page's word sequence. So anchor casing/punctuation is
  forgiving *except* that only `A–Z 0–9 $ . , -` survive tokenization — the
  excerpt must be a contiguous run of words as they appear on the page.
- **Required?** column: `REQUIRED` = one of the 10 `REQUIRED_CLAIM_FIELDS`
  (`invoice_number, container_number, bill_of_lading, charge_type, period_start,
  period_end, charged_days, daily_rate, total, issued_date`); everything else is
  `context` (surrounding realism, not validated by intake but present for a real
  extraction surface).
- Money normalization: `_parse_usd_minor` strips everything but `0-9 . -`, rejects
  negatives and >2 decimal places, returns `int(Decimal * 100)`.

### 2a. The 10 REQUIRED_CLAIM_FIELDS

| Field | Value on the invoice (as printed) | Normalized / expected extraction value | Extraction source anchor (exact text) | Validator it must satisfy (intake_claims.py rule) | Required? |
|---|---|---|---|---|---|
| `invoice_number` | `INV-1048` | `"INV-1048"` (STRING; `str(value).strip()`, non-empty) | `INV-1048` | Falls to generic branch: `normalized = str(value).strip(); if not normalized: raise` → `STRING`. | REQUIRED |
| `container_number` | `TLLU-482931-7` | `"TLLU4829317"` (IDENTIFIER) | `TLLU-482931-7` | `re.sub(r"[^A-Z0-9]", "", str(value).upper())` then `re.fullmatch(r"[A-Z]{4}\d{7}", normalized)`. TLLU4829317 = 4 letters + 7 digits ✓ | REQUIRED |
| `bill_of_lading` | `OAK-77421` | `"OAK-77421"` (IDENTIFIER) | `OAK-77421` | `str(value).strip().upper()` then `re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,30}", normalized)`. `OAK-77421` = 9 chars, valid start, only `[A-Z0-9-]` ✓ | REQUIRED |
| `charge_type` | `Demurrage` | `"DEMURRAGE"` (ENUM) | `Demurrage` | `str(value).strip().upper()` must `== "DEMURRAGE"` else raise. ✓ | REQUIRED |
| `period_start` | `2026-06-08` (printed `June 8, 2026`) | `"2026-06-08"` (DATE) | `June 8, 2026` | `date_parser.parse(str(value), fuzzy=False).date().isoformat()`. dateutil parses `June 8, 2026` → `2026-06-08`. | REQUIRED |
| `period_end` | `2026-06-14` (printed `June 14, 2026`) | `"2026-06-14"` (DATE) | `June 14, 2026` | Same DATE branch → `2026-06-14`. | REQUIRED |
| `charged_days` | `7` | `7` (INTEGER) | `7 days` (anchor on the `7`) | `int(value)`, reject bool, `0 < parsed <= 90`. `7` ✓ | REQUIRED |
| `daily_rate` | `$350.00` | `{"amount_minor": 35000, "currency": "USD"}` (MONEY) | `$350.00` | `_parse_usd_minor` → strips to `350.00`, exponent -2 OK, `int(350.00*100)=35000`; wrapped in `Money(35000,"USD")`. | REQUIRED |
| `total` | `$2,450.00` | `{"amount_minor": 245000, "currency": "USD"}` (MONEY) | `$2,450.00` | `_parse_usd_minor` → strips comma → `2450.00` → `245000`. | REQUIRED |
| `issued_date` | `2026-06-22` (printed `June 22, 2026`) | `"2026-06-22"` (DATE) | `June 22, 2026` | DATE branch → `2026-06-22`. | REQUIRED |

**Cross-field checks the printed values must satisfy** (post-normalization, in
`validate_extracted_claims`):

- `CHARGE_PERIOD_DAY_COUNT_MISMATCH` guard: `(end - start).days + 1 == charged_days`
  → `(2026-06-14 − 2026-06-08).days + 1 = 6 + 1 = 7 == 7` ✓
- `CLAIMED_TOTAL_ARITHMETIC_MISMATCH` guard:
  `daily_rate.amount_minor * charged_days == total.amount_minor`
  → `35000 × 7 = 245000` ✓

### 2b. Surrounding context fields (realism; not in REQUIRED_CLAIM_FIELDS)

| Field | Value on the invoice (as printed) | Normalized / expected value | Extraction source anchor (exact text) | Note | Required? |
|---|---|---|---|---|---|
| Due date | `2026-07-22` (printed `July 22, 2026`) | `2026-07-22` (Net 30 from issue) | `July 22, 2026` | Payment-terms consistency (issue + 30d). | context |
| Currency | `USD` | `"USD"` | `Currency: USD` | Matches Money currency; all amounts USD. | context |
| Carrier name | `Seaworthy Shipping` | `"Seaworthy Shipping"` | `Seaworthy Shipping` | Fictional carrier. | context |
| Carrier address | `1200 Harborline Way, Suite 400, Long Beach, CA 90802` | as printed | `1200 Harborline Way, Suite 400, Long Beach, CA 90802` | Fictional. | context |
| Carrier contact | `billing@seaworthy-demo.example · +1 (555) 0142` | as printed | `billing@seaworthy-demo.example` | `.example` reserved TLD. | context |
| Billing party (customer) name | `Cascade Import Partners LLC` | `"Cascade Import Partners LLC"` | `Cascade Import Partners LLC` | Fictional importer. | context |
| Billing address | `88 Waterfront Plaza, Oakland, CA 94607` | as printed | `88 Waterfront Plaza, Oakland, CA 94607` | Distinct from customer/ship-to. | context |
| Customer (ship-to) address | `4501 Cordelia Junction Rd, Fairfield, CA 94534` | as printed | `4501 Cordelia Junction Rd, Fairfield, CA 94534` | Distinct from billing address. | context |
| PO number | `PO-2026-3391` | `"PO-2026-3391"` | `PO-2026-3391` | Purchase-order context. | context |
| Vessel name | `MV Cascadia Star` | `"MV Cascadia Star"` | `MV Cascadia Star` | Fictional vessel. | context |
| Voyage number | `CSX-0619E` | `"CSX-0619E"` | `Voyage CSX-0619E` | Fictional voyage. | context |
| Terminal name | `Ben E. Nutter Terminal` | as printed | `Ben E. Nutter Terminal` | Fictional-styled terminal label. | context |
| Port | `Port of Oakland (USOAK)` | `"Port of Oakland"` / UN/LOCODE `USOAK` | `Port of Oakland` | Consistent with OAK BoL prefix. | context |
| Rate basis | `Per container, per day` | as printed | `Per container, per day` | Explains $350/day. | context |
| Free-time days | `4` | `4` | `Free time: 4 days` | Demurrage begins after free time; context for why days are billed. | context |
| Last-free-day | `2026-06-07` (printed `June 7, 2026`) | `2026-06-07` | `June 7, 2026` | Day before period_start; consistency with free-time. | context |
| Remit-to bank | `Harborline Demo Bank` | as printed | `Harborline Demo Bank` | Fictional. | context |
| Bank routing (ABA) | `DEMO-000000000` | as printed | `DEMO-000000000` | Obviously fake; not a valid routing number. | context |
| Bank account | `DEMO-ACCT-0000` | as printed | `DEMO-ACCT-0000` | Obviously fake. | context |
| Payment terms | `Net 30` | `"Net 30"` | `Net 30` | Ties issue→due. | context |
| Classification banner | `SYNTHETIC DEMO — FICTIONAL DATA` | as printed | `SYNTHETIC DEMO` | Matches hero fixture `classification`. | context |

### 2c. The seven daily charge line items

All seven rows are printed in the line-items block. Each line: `Date` ·
`Description` · `Amount`. The seven `$350.00` amounts printed here are the
component evidence behind the `daily_rate` and `total` REQUIRED fields; the
extractor anchors `daily_rate` on any single `$350.00` and `total` on the
`$2,450.00` summary line.

| # | Line date (printed) | Line date (ISO) | Description (as printed) | Amount (printed) | Amount minor |
|---|---|---|---|---|---|
| 1 | `June 8, 2026` | `2026-06-08` | `Demurrage — container TLLU-482931-7, day 1 of 7` | `$350.00` | 35000 |
| 2 | `June 9, 2026` | `2026-06-09` | `Demurrage — container TLLU-482931-7, day 2 of 7` | `$350.00` | 35000 |
| 3 | `June 10, 2026` | `2026-06-10` | `Demurrage — container TLLU-482931-7, day 3 of 7` | `$350.00` | 35000 |
| 4 | `June 11, 2026` | `2026-06-11` | `Demurrage — container TLLU-482931-7, day 4 of 7` | `$350.00` | 35000 |
| 5 | `June 12, 2026` | `2026-06-12` | `Demurrage — container TLLU-482931-7, day 5 of 7` | `$350.00` | 35000 |
| 6 | `June 13, 2026` | `2026-06-13` | `Demurrage — container TLLU-482931-7, day 6 of 7` | `$350.00` | 35000 |
| 7 | `June 14, 2026` | `2026-06-14` | `Demurrage — container TLLU-482931-7, day 7 of 7` | `$350.00` | 35000 |
| — | — | — | **`Total demurrage due`** | **`$2,450.00`** | 245000 |

Sum check: `7 × 35000 = 245000` = printed total ✓.

---

## 3. Layout hint (block placement only — no visual design)

Single page, top-to-bottom reading order so word-sequence anchoring stays
contiguous within each block:

- **Header (top band):** carrier name `Seaworthy Shipping`, carrier address +
  contact, then `INV-1048`, `Issue date: June 22, 2026`, `Due date: July 22, 2026`,
  `Currency: USD`, `PO-2026-3391`. The fiction banner (§1) sits here, prominent.
- **Billing block (upper-left):** `Bill to: Cascade Import Partners LLC` + billing
  address. **Ship-to block (upper-right):** customer/ship-to address (distinct).
- **Shipment block (below billing):** `Container TLLU-482931-7`, `B/L OAK-77421`,
  `MV Cascadia Star`, `Voyage CSX-0619E`, `Ben E. Nutter Terminal`,
  `Port of Oakland (USOAK)`.
- **Charge summary line (above the table):** `Charge type: Demurrage`,
  `Charge period: June 8, 2026 — June 14, 2026`, `7 days`, `Free time: 4 days`,
  `Last free day: June 7, 2026`, `Rate basis: Per container, per day`,
  `Daily rate: $350.00`.
- **Line-items table (page middle):** the seven rows from §2c in date order, then
  the `Total demurrage due  $2,450.00` line.
- **Footer:** `Payment terms: Net 30`; remittance block (`Harborline Demo Bank`,
  `DEMO-000000000`, `DEMO-ACCT-0000`); classification `SYNTHETIC DEMO — FICTIONAL DATA`.

Keep each anchor string as one uninterrupted run of words on the page (no column
break or line wrap mid-anchor), so the extracted `text_excerpt` matches the word
sequence `locate_pdf_anchor` scans.

---

## 4. Fiction & safety

- Every party, vessel, terminal-as-billed, address, email, and phone number is
  invented. Emails use the reserved `.example` TLD; phone uses the `555-01xx`
  fictional exchange.
- **No real trademarks or real carrier names.** "Seaworthy Shipping" is invented;
  do not substitute any real ocean carrier's name or marks.
- **USD only.** Every monetary amount is US dollars; the printed `Currency: USD`
  matches `Money(..., "USD")`. No other currency appears.
- **No real bank routing/account data.** Routing is the obviously-fake
  `DEMO-000000000`; account is `DEMO-ACCT-0000`. These are not valid ABA/account
  numbers and must never be replaced with real ones.
- The page carries both the human fiction notice (§1) and the machine
  classification `SYNTHETIC DEMO — FICTIONAL DATA`, so neither a reader nor an
  automated consumer can mistake it for a genuine invoice. No payment is owed.
```
