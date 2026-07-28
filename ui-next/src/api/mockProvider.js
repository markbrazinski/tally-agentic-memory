// Mock provider — same interface + shapes as liveProvider, driven by the locked
// hero (INV-1048 $700 dispute) + the valid INV-1050 $875 path. Used for offline
// design/dev and as the shape reference. It advances the pipeline on a timer to
// mimic the live event stream so the UI plays without a backend.
//
// This is the ONLY place fictional data lives on the frontend. The live provider
// never falls back to it (the handoff's fail-closed rule).

const HERO_EVENTS = [
  "invoice.received",
  "reconstruction.memory_retrieval_started",
  "reconstruction.completed",
  "evidence.rule_search_started",
  "evidence.rule_verified",
  "decision.recommendation_ready",
];

export function createMockProvider() {
  const hero = {
    invoiceId: "INV-TLY-1048",
    name: "INV-1048.pdf",
    container: "TLLU-482931-7",
    amountMinor: 245000,
    disputedMinor: 70000,
    aggregateStatus: "RECEIVED",
    recommendationType: null,
    receivedAt: "2026-06-22T08:00:00Z",
  };

  return {
    kind: "mock",

    async listInvoices() {
      return [
        {
          invoiceId: "INV-1041", name: "INV-1041.pdf", container: "MRKU-701882-3",
          amountMinor: 54000, aggregateStatus: "APPROVED_FOR_PAYMENT",
          recommendationType: "APPROVE_FOR_PAYMENT", receivedAt: "2026-06-02T00:00:00Z",
        },
        {
          invoiceId: "INV-1039", name: "INV-1039.pdf", container: "TCLU-559120-1",
          amountMinor: 112000, aggregateStatus: "DISPUTED",
          recommendationType: "DISPUTE", receivedAt: "2026-05-28T00:00:00Z",
        },
        {
          // Persisted refusal row (v3): governing tariff unresolved, so no
          // financial action is authorized. Invoice total $875 is the AMOUNT;
          // the unresolved reason is the outcome detail.
          invoiceId: "INV-TLY-1047", name: "INV-1047.pdf", container: "HLXU-223874-9",
          amountMinor: 87500, aggregateStatus: "NEEDS_EVIDENCE",
          recommendationType: "REQUEST_EVIDENCE",
          unresolvedReason: "Governing tariff not verified",
          receivedAt: "2026-06-11T00:00:00Z",
        },
        { ...hero },
      ];
    },

    async getInvoice() {
      return { invoice: { invoice_id: hero.invoiceId, aggregate_status: hero.aggregateStatus }, etag: '"mock"' };
    },

    async getReconstruction() {
      return MOCK_RECONSTRUCTION;
    },

    async getDecision() {
      return null;
    },

    // Replays the hero event beats on a timer (stand-in for SSE).
    subscribe(onEvent) {
      let i = 0;
      const id = setInterval(() => {
        if (i >= HERO_EVENTS.length) { clearInterval(id); return; }
        onEvent({
          event_id: `mock-${i}`, event_type: HERO_EVENTS[i], invoice_id: hero.invoiceId,
          sequence: i + 1, occurred_at: new Date().toISOString(),
        });
        i += 1;
      }, 1200);
      return () => clearInterval(id);
    },

    async approve() {
      return { recommendation_type: "DISPUTE", revision: 1, seal_digest: "sha256:mock" };
    },
  };
}

// The reconstruction projection shape (mirrors GET /api/invoices/{id}/reconstruction).
export const MOCK_RECONSTRUCTION = {
  reconstruction_id: "RECON-mock", version: 1, state: "COMPLETE",
  knowledge_cutoff: "2026-06-22T08:00:00Z", effective_timezone: "America/Los_Angeles",
  source_disclosure: "Representative demonstration data",
  timeline: [
    { event_ref: "SE-001", type: "DISCHARGED", occurred_at: "2026-06-02T15:00:00-07:00", recorded_before_invoice: true, verification_state: "VERIFIED" },
    { event_ref: "SE-002", type: "AVAILABLE", occurred_at: "2026-06-03T09:00:00-07:00", recorded_before_invoice: true, verification_state: "VERIFIED" },
    { event_ref: "SE-003", type: "FREE_TIME_START", occurred_at: "2026-06-03T09:00:00-07:00", recorded_before_invoice: true, verification_state: "VERIFIED" },
    { event_ref: "SE-004", type: "FREE_TIME_END", occurred_at: "2026-06-07T23:59:00-07:00", recorded_before_invoice: true, verification_state: "VERIFIED" },
    { event_ref: "SE-005", type: "GATE_OUT", occurred_at: "2026-06-14T17:00:00-07:00", recorded_before_invoice: true, verification_state: "VERIFIED" },
  ],
  charged_days: [8, 9, 10, 11, 12, 13, 14].map((d) => ({
    date: `2026-06-${d}`, chargeability: "CHARGEABLE", coverage: "PRESENT_VERIFIED",
    state: "SOURCE_COMPLETE", invoice_rate_minor: 35000, applicable_rate_minor: 25000,
    currency: "USD", outcome: "RATE_DISCREPANCY", dispute_amount_minor: 10000,
    event_refs: ["SE-002", "SE-004", "SE-005"], missing_requirements: [],
  })),
  applicable_rule: {
    rule_ref: "RULE-Clause 4.2", clause_ref: "Clause 4.2",
    display_excerpt: "Demurrage rate: $250 per calendar day",
    rate_minor: 25000, currency: "USD", unit: "CALENDAR_DAY",
    effective_from: "2026-06-01", effective_to: null, scope_code: "DEMURRAGE:USOAK:DRY",
    validation_state: "VERIFIED",
    retrieval: { tool: "CockroachDB Distributed Vector Indexing", state: "RETRIEVED" },
  },
  recommendation: {
    recommendation_id: "REC-mock", version: 1, recommendation_type: "DISPUTE",
    disputed_amount_minor: 70000, supported_amount_minor: 175000,
    claimed_amount_minor: 245000, currency: "USD", days_total: 7, days_covered: 7,
    evidence_coverage: "7 of 7 days", state: "FROZEN",
    summary: "Dispute $700.00 across 7 sourced days.",
    approval_etag: '"rec-REC-mock-v1-sha256:mock"',
  },
  coverage: { days_complete: 7, days_total: 7, requirements: [], missing_requirements: [] },
};
