// PublicDemoProvider — the logged-out, read-only judge surface.
//
// It consumes exactly one same-origin projection. It intentionally has no
// generic invoice/case client, mutation method, bearer-token support, or live
// feed. The server owns hero selection and returns only public display fields.

const PUBLIC_HERO_HANDLE = "public-demo-hero";

function presentString(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function presentRate(value) {
  return (typeof value === "number" && Number.isFinite(value)) || presentString(value)
    ? value
    : null;
}

function normalizeProjection(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (payload.available === false || /unavailable|error/i.test(String(payload.status || ""))) return null;

  const replay = payload.replay && typeof payload.replay === "object" ? payload.replay : payload;
  const then = replay.then && typeof replay.then === "object" ? replay.then : {};
  const now = replay.now && typeof replay.now === "object" ? replay.now : {};
  const retention = replay.retention && typeof replay.retention === "object" ? replay.retention : {};
  const tamper = replay.tamper_check && typeof replay.tamper_check === "object" ? replay.tamper_check : {};
  const receipt = replay.receipt && typeof replay.receipt === "object" ? replay.receipt : {};

  const thenState = presentString(then.state ?? replay.then_state ?? replay.historical_state);
  const nowState = presentString(now.state ?? replay.now_state ?? replay.current_state);
  const thenRate = presentRate(then.tariff_rate ?? then.recorded_rate ?? replay.then_rate ?? replay.historical_rate);
  const nowRate = presentRate(now.tariff_rate ?? now.recorded_rate ?? replay.now_rate ?? replay.current_rate);
  const ttlDays = retention.ttl_days ?? replay.retention_days;
  const retentionLanguage = presentString(retention.language ?? replay.retention_language);
  const tamperMatch = tamper.match ?? receipt.bindings_unchanged ?? replay.tamper_match;

  // An incomplete 200 response is unavailable, not permission to invent or
  // fall back to the synthetic film.
  if (!thenState || !nowState || thenRate == null || nowRate == null) return null;
  if (!Number.isFinite(ttlDays) || !retentionLanguage || typeof tamperMatch !== "boolean") return null;

  const display = payload.display && typeof payload.display === "object"
    ? payload.display
    : (payload.case && typeof payload.case === "object"
      ? payload.case
      : (payload.hero && typeof payload.hero === "object" ? payload.hero : {}));

  return {
    case: {
      // This local handle is deliberately unrelated to a database identifier.
      id: PUBLIC_HERO_HANDLE,
      container: presentString(display.container ?? display.container_label ?? display.importer),
      carrier: presentString(display.carrier ?? display.carrier_label) || "Public demo",
      invoiceNo: presentString(display.invoice ?? display.invoice_label ?? display.reference) || "Replay projection",
      amount: null,
      dispute: null,
      verdict: "PUBLIC_DEMO",
      citedRule: "Read-only replay projection",
      aT: null,
      cT: null,
      sT: null,
      kT: null,
      rT: null,
    },
    replay: {
      then: { state: thenState, tariff_rate: thenRate },
      now: { state: nowState, tariff_rate: nowRate },
      retention: { ttl_days: ttlDays, language: retentionLanguage },
      tamper_check: { match: tamperMatch },
    },
  };
}

export function createLiveProvider() {
  let projection = null;
  let changeListeners = [];
  let loadState = "loading";

  function notifyChange() {
    changeListeners.forEach((fn) => fn());
  }

  async function loadHero() {
    const response = await fetch("/public/demo/hero", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`GET /public/demo/hero -> ${response.status}`);

    const normalized = normalizeProjection(await response.json());
    if (!normalized) throw new Error("GET /public/demo/hero -> incomplete public projection");
    projection = normalized;
    loadState = "ready";
    notifyChange();
  }

  // One cold-start retry is bounded and never changes the source of truth.
  loadHero().catch(() => loadHero()).catch(() => {
    projection = null;
    loadState = "unavailable";
    console.error("[PublicDemoProvider] public hero unavailable; synthetic film data was not substituted.");
    notifyChange();
  });

  const getCases = () => projection ? [projection.case] : [];
  const getCredits = () => [];
  const getPaidSeed = () => [];
  const getNotPressedSeed = () => [];
  const getConduct = () => [];
  const getEvalRun = () => null;
  const getTariffCapture = () => ({ at: null, atS: null, rate: null, carrier: null, lane: null });
  const getHeroCaseId = () => projection ? PUBLIC_HERO_HANDLE : null;
  const getCommitLog = () => {
    throw new Error("PublicDemoProvider.getCommitLog: no synthetic log may be rendered");
  };
  const getClockMode = () => "live";
  const getInitialClock = () => Date.now();
  const getRecordingStart = () => Date.now();
  const getDisclosure = () => loadState === "unavailable" ? ({
    label: "PUBLIC DEMO — UNAVAILABLE",
    detail: "The live public replay projection could not be loaded. Synthetic film data was not substituted.",
    tone: "error",
  }) : ({
    label: loadState === "ready" ? "PUBLIC DEMO — LIVE READ-ONLY" : "PUBLIC DEMO — LOADING",
    detail: "This logged-out view reads one server-selected public replay projection. Mutations and live feeds are disabled.",
    tone: "live",
  });
  const onDataChange = (fn) => { changeListeners.push(fn); };

  async function replayCase(id) {
    if (id !== PUBLIC_HERO_HANDLE || !projection) {
      throw new Error("Public replay projection unavailable");
    }
    return projection.replay;
  }

  return {
    getCases,
    getCredits,
    getPaidSeed,
    getNotPressedSeed,
    getConduct,
    getEvalRun,
    getTariffCapture,
    getHeroCaseId,
    getCommitLog,
    getClockMode,
    getDisclosure,
    getInitialClock,
    getRecordingStart,
    onDataChange,
    replayCase,
  };
}

export { normalizeProjection };
