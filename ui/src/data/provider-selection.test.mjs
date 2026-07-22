import { test } from "node:test";
import assert from "node:assert/strict";
import { selectProvider } from "./provider-selection.js";

test("default mode selects the explicitly synthetic provider", () => {
  const mock = { kind: "mock" };
  const selected = selectProvider(
    { wantsLive: false },
    { mockFactory: () => mock, liveFactory: () => assert.fail("live factory must not run") },
  );
  assert.equal(selected, mock);
});

test("incomplete live configuration is unavailable and never falls back to mock", () => {
  let mockCalls = 0;
  const selected = selectProvider(
    { wantsLive: true, apiBase: "", bearerToken: "" },
    { mockFactory: () => { mockCalls++; }, liveFactory: () => assert.fail("live factory must not run") },
  );

  assert.equal(mockCalls, 0);
  assert.equal(selected.getClockMode(), "live");
  assert.deepEqual(selected.getCases(), []);
  assert.match(selected.getDisclosure().label, /LIVE UNAVAILABLE/);
  assert.match(selected.getDisclosure().detail, /Synthetic film data was not substituted/);
  assert.throws(() => selected.getCommitLog(), /no synthetic log/);
});

test("complete live configuration is passed only to the live factory", () => {
  const live = { kind: "live" };
  const selected = selectProvider(
    { wantsLive: true, apiBase: "https://api.example", bearerToken: "token" },
    {
      mockFactory: () => assert.fail("mock factory must not run"),
      liveFactory: (config) => {
        assert.deepEqual(config, { apiBase: "https://api.example", bearerToken: "token" });
        return live;
      },
    },
  );
  assert.equal(selected, live);
});
