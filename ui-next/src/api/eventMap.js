// Maps the backend's public event_type strings (from GET /api/stream) onto the
// Workbench `wb` state-machine ranks. The design's state machine is the source
// of visual truth; this table is the only translation layer. Any event_type not
// listed leaves `wb` unchanged (progress is monotonic — never regress).
//
// Backend event_type -> wb state (see Workbench.rank()).
export const EVENT_TO_WB = {
  "invoice.received": "intake",
  "intake.extraction_started": "intake",
  "intake.claims_validated": "reconstructing",
  "invoice.reconstruction_started": "reconstructing",
  "reconstruction.memory_retrieval_started": "reconstructing",
  "reconstruction.completed": "reconstructed",
  "evidence.rule_search_started": "retrieving",
  "evidence.rule_verified": "ruleVerified",
  "evidence.rule_not_applicable": "insufficient",
  "evidence.rule_conflict": "insufficient",
  "decision.recommendation_ready": "recommendation",
  "decision.sealed": "readyToSend",
  "correspondence.send_blocked": "sending",
  "correspondence.sent": "sent",
};

// Aggregate status (invoice.aggregate_status) -> queue pill kind + label.
// Mirrors Workbench.statusMeta() so queue rows and the workbench agree.
export const STATUS_LABEL = {
  RECEIVED: { status: "RECEIVED", kind: "neutral" },
  INITIAL_PROCESSING: { status: "INITIAL PROCESSING", kind: "checking" },
  RECONSTRUCTING: { status: "RECONSTRUCTING", kind: "checking" },
  NEEDS_EVIDENCE: { status: "NEEDS EVIDENCE", kind: "checking" },
  READY_FOR_REVIEW: { status: "READY FOR REVIEW", kind: "neutral" },
  APPROVED: { status: "APPROVED", kind: "neutral" },
  READY_TO_SEND: { status: "READY TO SEND", kind: "neutral" },
  DISPUTED: { status: "DISPUTED", kind: "contested" },
  APPROVED_FOR_PAYMENT: { status: "APPROVED FOR PAYMENT", kind: "verified" },
  BLOCKED: { status: "BLOCKED", kind: "contested" },
  SEND_FAILED: { status: "SEND BLOCKED", kind: "contested" },
};

// Given the highest-ranked wb state seen so far and an incoming event, return
// the wb state to advance to (never regresses).
export function nextWb(currentWb, eventType, rankFn) {
  const mapped = EVENT_TO_WB[eventType];
  if (!mapped) return currentWb;
  return rankFn(mapped) > rankFn(currentWb) ? mapped : currentWb;
}
