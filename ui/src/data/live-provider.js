// LiveProvider (B1FE-S3) — speaks the real API. Implements DataProvider
// (provider.js) for the seed-shaped getters, plus fetchCase/replayCase/approveCase/
// subscribeFeed for the routes that exist: GET /invoices, GET /cases/{id},
// POST /cases/{id}/approve, WS /feed (confirmed live against
// bundle-0-S3/bundle-2-S0 — see ui/docs/s3-view-triage-proposal.md).
//
// Routes that do NOT exist yet (ledger, recordings, contests, rebuttal,
// carriers, clerk run detail, evals) have no LiveProvider method —
// Component must not call one that isn't here. Per S1/S3's view-triage
// ruling those views stay fixture-disclosed or film-only until a later
// backend bundle ships their routes.
//
// getCases() is synchronous (interface parity with MockProvider) but starts
// empty — the constructor kicks off a background GET /invoices and calls
// onDataChange's listener when it lands, instead of blocking mount on a
// network round trip (see provider.js's onDataChange doc for why: no clean
// way to gate support.js's boot from outside it).
//
// implements DataProvider (see provider.js)
export function createLiveProvider({ apiBase, bearerToken }) {
  const authHeaders = { Authorization: `Bearer ${bearerToken}` };
  let cases = [];
  let changeListeners = [];
  let loadState = "loading";

  function notifyChange() {
    changeListeners.forEach((fn) => fn());
  }

  async function apiFetch(path, opts = {}) {
    const res = await fetch(`${apiBase}${path}`, opts);
    if (!res.ok) {
      // Do not surface an arbitrary response body in the browser console; it
      // may contain private identifiers or server diagnostics.
      throw new Error(`${opts.method || "GET"} ${path} -> ${res.status}`);
    }
    return res.status === 204 ? null : res.json();
  }

  // Maps GET /invoices' real shape to the field names Component's *Vals()
  // methods read (id/container/carrier/invoiceNo/amount/verdict/aT etc.) —
  // see ui/docs/s3-view-triage-proposal.md's verdict-mapping note: the real
  // backend only computes DEFECTIVE/NEEDS_REVIEW/VALID (no tier-3 rate
  // check yet), so this does NOT invent owe0/over/unver — queueVals()'s
  // live-mode branch (index.html) handles that distinction honestly.
  function mapInvoiceItem(item) {
    return {
      id: item.case_id || item.id,
      invoiceId: item.id,
      container: item.container_no,
      carrier: item.carrier?.name ?? null,
      invoiceNo: item.id,
      amount: item.amount,
      dispute: item.amount,
      verdict: item.verdict, // real: DEFECTIVE | NEEDS_REVIEW | VALID
      citedRule: item.cited_rule,
      aT: item.received_at ? Date.parse(item.received_at) : null,
      cT: item.received_at ? Date.parse(item.received_at) : null, // no separate "checked" timestamp yet
      sT: null, kT: null, rT: null, // sealed/contested/resolved timestamps come from GET /cases/{id}, not this list
    };
  }

  async function refreshCases() {
    const data = await apiFetch("/invoices");
    cases = data.items.map(mapInvoiceItem);
    loadState = "ready";
    notifyChange();
  }
  // ponytail: one retry, not a backoff loop — the initial load has been
  // observed to transiently fail against the live App Runner endpoint
  // (cold start), and a single retry clears it every time seen so far.
  // Escalate to real backoff only if this proves insufficient in practice.
  refreshCases().catch(() => refreshCases()).catch(() => {
    loadState = "unavailable";
    console.error("[LiveProvider] initial GET /invoices unavailable after one retry; synthetic data was not substituted.");
    notifyChange();
  });

  // ---------- seed-shaped getters ----------
  const getCases = () => cases;
  const getCredits = () => [];
  const getPaidSeed = () => [];
  const getNotPressedSeed = () => [];
  const getConduct = () => [];
  const getEvalRun = () => null;
  const getTariffCapture = () => ({ at: null, atS: null, rate: null, carrier: null, lane: null });
  const getHeroCaseId = () => null;

  // Law 2 fence: LiveProvider must never synthesize a log line. Live mode
  // renders only feed-delivered lines (subscribeFeed), never this.
  function getCommitLog() {
    throw new Error("LiveProvider.getCommitLog: live mode renders only feed-delivered lines (Law 2) — this must never be called");
  }

  const getClockMode = () => "live";
  const getDisclosure = () => loadState === "unavailable" ? ({
    label: "LIVE API — UNAVAILABLE",
    detail: "The initial live data load failed. Synthetic film data was not substituted.",
    tone: "error",
  }) : ({
    label: loadState === "ready" ? "LIVE API" : "LIVE API — LOADING",
    detail: "Data shown in live-backed views comes from the configured API. Views without a backing route remain unavailable.",
    tone: "live",
  });
  const getInitialClock = () => Date.now();
  const getRecordingStart = () => Date.now();
  const onDataChange = (fn) => { changeListeners.push(fn); };

  // ---------- real routes ----------
  async function fetchCase(id) {
    return apiFetch(`/cases/${encodeURIComponent(id)}`);
  }

  async function replayCase(id) {
    return apiFetch(`/cases/${encodeURIComponent(id)}/replay`, {
      headers: authHeaders,
    });
  }

  async function approveCase(caseId) {
    const result = await apiFetch(`/cases/${encodeURIComponent(caseId)}/approve`, {
      method: "POST",
      headers: authHeaders,
    });
    await refreshCases(); // seal changes case state — refresh the list so Queue/status reflect it
    return result;
  }

  // ponytail: no reconnect/backoff loop — S3's scope is "wire the feed",
  // not build resilience for a connection that (as of this session) the
  // App Runner deploy rejects outright (confirmed: fails even from a bare
  // Node WebSocket client, no browser/CORS involved — see
  // ui/docs/s3-view-triage-proposal.md addendum). Add retry when the
  // underlying block is diagnosed and fixed; retrying against a
  // structurally-failing connection would just be a noisier no-op.
  function subscribeFeed(onEvent) {
    const wsBase = apiBase.replace(/^http/, "ws");
    const ws = new WebSocket(`${wsBase}/feed`);
    ws.addEventListener("message", (ev) => {
      try {
        onEvent(JSON.parse(ev.data));
      } catch {
        // malformed frame — drop it, never crash the feed listener over one bad frame
      }
    });
    ws.addEventListener("error", () => {
      console.warn("[LiveProvider] WS /feed connection failed — case.sealed live updates unavailable this session, seal still works via the direct POST response.");
    });
    return () => ws.close();
  }

  return {
    getCases, getCredits, getPaidSeed, getNotPressedSeed, getConduct,
    getEvalRun, getTariffCapture, getHeroCaseId, getCommitLog,
    getClockMode, getDisclosure, getInitialClock, getRecordingStart, onDataChange,
    fetchCase, replayCase, approveCase, subscribeFeed,
  };
}
