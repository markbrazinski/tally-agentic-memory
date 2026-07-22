// LiveProvider contract tests (B1FE-S3). node:test — stdlib, no new
// dependency. Mocks globalThis.fetch; no real network calls, matching the
// project's "zero network calls in the test suite" discipline (the
// live-smoke.mjs / live-seal-check.mjs scripts are the real-network checks,
// intentionally kept out of this suite).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createLiveProvider } from "./live-provider.js";

// Safe default for tests that exercise synchronous getters only. Individual
// fetch tests replace this with their own canned responses; no test may reach
// the network merely because LiveProvider refreshes in its constructor.
globalThis.fetch = async () => ({
  ok: true,
  status: 200,
  json: async () => ({ items: [], next_cursor: null }),
  text: async () => "",
});

function mockFetch(responses) {
  let call = 0;
  return async (url, opts) => {
    const r = responses[call++];
    if (!r) throw new Error(`mockFetch: no response queued for call ${call}`);
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      json: async () => r.body,
      text: async () => JSON.stringify(r.body),
    };
  };
}

// Each createLiveProvider() call fires an uncancellable background
// refreshCases() on construction. Tests that install their own
// globalThis.fetch mock must wait for the PREVIOUS test's background work
// to drain first, or a stray retry from an earlier provider consumes the
// current test's mocked response and produces a flaky call count.
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

test("getCommitLog throws — Law 2 fence, live mode must never synthesize a log line", () => {
  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  assert.throws(() => provider.getCommitLog(Date.now()), /must never be called/);
});

test("getClockMode is live, getInitialClock/getRecordingStart are current time", () => {
  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  assert.equal(provider.getClockMode(), "live");
  const now = Date.now();
  assert.ok(Math.abs(provider.getInitialClock() - now) < 5000);
  assert.ok(Math.abs(provider.getRecordingStart() - now) < 5000);
});

test("seed-shaped getters with no backing route return honest empties, not fabricated data", () => {
  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  assert.deepEqual(provider.getCredits(), []);
  assert.deepEqual(provider.getPaidSeed(), []);
  assert.deepEqual(provider.getNotPressedSeed(), []);
  assert.deepEqual(provider.getConduct(), []);
  assert.equal(provider.getHeroCaseId(), null);
  assert.equal(provider.getEvalRun(), null);
  const tariff = provider.getTariffCapture();
  assert.equal(tariff.rate, null);
  assert.match(provider.getDisclosure().label, /^LIVE API/);
});

test("getCases() maps real GET /invoices shape to the fields *Vals() methods read", async (t) => {
  await settle();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = mockFetch([
    {
      status: 200,
      body: {
        items: [{
          id: "inv-1", case_id: "case-1",
          carrier: { scac: "DEMO", name: "API Example Carrier" },
          container_no: null, amount: null, verdict: "DEFECTIVE", cited_rule: null,
          received_at: "2026-07-09T02:19:13.699409+00:00",
        }],
        next_cursor: null,
      },
    },
  ]);
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  await new Promise((resolve) => provider.onDataChange(resolve));

  const cases = provider.getCases();
  assert.equal(cases.length, 1);
  assert.equal(cases[0].id, "case-1"); // case_id preferred over invoice id
  assert.equal(cases[0].carrier, "API Example Carrier");
  assert.equal(cases[0].verdict, "DEFECTIVE"); // real vocabulary, not owe0/over/valid/unver
  assert.equal(cases[0].amount, null); // honest null, not coerced to 0
  assert.ok(cases[0].aT > 0);
});

test("getCases() retries once on initial fetch failure", async (t) => {
  await settle();
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    if (calls === 1) throw new Error("network blip");
    return { ok: true, status: 200, json: async () => ({ items: [], next_cursor: null }) };
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(calls, 2, "expected exactly one retry after the first failure");
});

test("approveCase() sends the bearer token and refreshes cases after sealing", async (t) => {
  await settle();
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    if (String(url).endsWith("/invoices")) {
      return { ok: true, status: 200, json: async () => ({ items: [], next_cursor: null }) };
    }
    if (String(url).includes("/approve")) {
      return { ok: true, status: 200, json: async () => ({ already_sealed: false, state: "FILED", sealed_txn_ts: "123" }) };
    }
    throw new Error("unexpected call: " + url);
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "secret-token" });
  await new Promise((resolve) => setTimeout(resolve, 20)); // let the initial refreshCases settle

  const result = await provider.approveCase("case-1");
  assert.equal(result.state, "FILED");
  assert.equal(result.sealed_txn_ts, "123");

  const approveCall = calls.find((c) => String(c.url).includes("/approve"));
  assert.equal(approveCall.opts.method, "POST");
  assert.equal(approveCall.opts.headers.Authorization, "Bearer secret-token");

  const invoiceCalls = calls.filter((c) => String(c.url).endsWith("/invoices"));
  assert.equal(invoiceCalls.length, 2, "expected the initial load plus one refresh after seal");
});

test("replayCase() invokes the real authenticated replay route", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    calls.push({ url, opts });
    if (url.endsWith("/invoices")) {
      return { ok: true, status: 200, json: async () => ({ items: [] }) };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        then: { state: "FILED" },
        now: { state: "CONTESTED" },
        retention: { ttl_days: 90 },
      }),
    };
  };
  t.after(() => { globalThis.fetch = originalFetch; });
  const provider = createLiveProvider({ apiBase: "https://api.example", bearerToken: "ephemeral" });
  await settle();

  const replay = await provider.replayCase("case/one");

  assert.equal(replay.then.state, "FILED");
  const request = calls.find(call => call.url.includes("/replay"));
  assert.equal(request.url, "https://api.example/cases/case%2Fone/replay");
  assert.equal(request.opts.headers.Authorization, "Bearer ephemeral");
});

test("apiFetch throws with status and path on a non-2xx response", async (t) => {
  await settle();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 404, text: async () => "case not found" });
  t.after(() => { globalThis.fetch = originalFetch; });

  const provider = createLiveProvider({ apiBase: "http://x", bearerToken: "t" });
  await assert.rejects(
    () => provider.fetchCase("missing"),
    (err) => {
      assert.match(err.message, /GET \/cases\/missing -> 404/);
      assert.doesNotMatch(err.message, /case not found/);
      return true;
    },
  );
  await settle();
  assert.equal(provider.getDisclosure().label, "LIVE API — UNAVAILABLE");
});
