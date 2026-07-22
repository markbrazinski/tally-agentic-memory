import { test } from "node:test";
import assert from "node:assert/strict";
import { createLiveProvider, normalizeProjection } from "./live-provider.js";

const completeProjection = {
  status: "executed",
  case: {
    reference: "GATE5-SYNTHETIC-HERO",
    importer: "Northstar Imports (fictional)",
    carrier: "Asterline Demo Shipping (fictional)",
  },
  replay: {
    then: { state: "FILED", recorded_rate: 250 },
    now: { state: "CONTESTED", recorded_rate: 300 },
    retention: { ttl_days: 90, language: "Within the configured retention window." },
    receipt: { bindings_unchanged: true, exact_versioned_s3_verified: true },
  },
};

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

test("normalizes only public display and replay fields", () => {
  const normalized = normalizeProjection({
    ...completeProjection,
    tenant_id: "must-not-flow-to-components",
    case_id: "must-not-flow-to-components",
    sealed_txn_ts: "must-not-flow-to-components",
    query: "must-not-flow-to-components",
  });

  assert.equal(normalized.case.id, "public-demo-hero");
  assert.equal(normalized.case.carrier, "Asterline Demo Shipping (fictional)");
  assert.deepEqual(normalized.replay.then, { state: "FILED", tariff_rate: 250 });
  assert.equal(JSON.stringify(normalized).includes("must-not-flow"), false);
});

test("rejects unavailable or incomplete projections instead of fabricating fields", () => {
  assert.equal(normalizeProjection({ status: "unavailable" }), null);
  assert.equal(normalizeProjection({ then: { state: "FILED" }, now: { state: "CONTESTED" } }), null);
  assert.equal(normalizeProjection({ ...completeProjection, replay: { ...completeProjection.replay, receipt: {} } }), null);
});

test("loads the hero with one same-origin GET and no bearer", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, json: async () => completeProjection };
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider();
  await new Promise((resolve) => provider.onDataChange(resolve));

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/public/demo/hero");
  assert.equal(calls[0].opts.method, "GET");
  assert.equal(calls[0].opts.credentials, "same-origin");
  assert.equal("Authorization" in calls[0].opts.headers, false);
  assert.equal(provider.getHeroCaseId(), "public-demo-hero");
  assert.equal(provider.getCases().length, 1);
  assert.match(provider.getDisclosure().label, /LIVE READ-ONLY/);

  const replay = await provider.replayCase("public-demo-hero");
  assert.equal(replay.now.state, "CONTESTED");
  assert.equal(calls.length, 1, "opening the hero must use the already-sanitized projection");
});

test("has no generic read, mutation, bearer, or feed capabilities", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => completeProjection });
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider();
  await new Promise((resolve) => provider.onDataChange(resolve));

  assert.equal(provider.fetchCase, undefined);
  assert.equal(provider.approveCase, undefined);
  assert.equal(provider.subscribeFeed, undefined);
  assert.throws(() => provider.getCommitLog(), /no synthetic log/);
});

test("a failed projection retries once, renders unavailable, and never returns film data", async (t) => {
  await settle();
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return { ok: false, status: 503, json: async () => ({}) };
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider();
  await new Promise((resolve) => provider.onDataChange(resolve));

  assert.equal(calls, 2);
  assert.deepEqual(provider.getCases(), []);
  assert.equal(provider.getHeroCaseId(), null);
  assert.match(provider.getDisclosure().label, /UNAVAILABLE/);
  assert.match(provider.getDisclosure().detail, /Synthetic film data was not substituted/);
  await assert.rejects(() => provider.replayCase("public-demo-hero"), /unavailable/);
});
