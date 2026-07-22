// Provider contract tests (B1FE-S2). node:test — stdlib, no runner dependency.
// Checks the explicitly synthetic MockProvider's public film shape and its
// truth-label fences. No test performs live I/O.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createMockProvider } from "./mock-provider.js";

const provider = createMockProvider();

test("getCases returns the full 38-case seed set", () => {
  const cases = provider.getCases();
  assert.equal(cases.length, 38);
  for (const c of cases) {
    assert.equal(typeof c.id, "string");
    assert.equal(typeof c.carrier, "string");
    assert.ok(["owe0", "over", "valid", "unver"].includes(c.verdict), `unexpected verdict ${c.verdict}`);
  }
});

test("getHeroCaseId points at a real case in getCases()", () => {
  const heroId = provider.getHeroCaseId();
  const cases = provider.getCases();
  assert.ok(cases.some((c) => c.id === heroId));
});

test("getCredits does not fabricate externally confirmed recovery", () => {
  assert.deepEqual(provider.getCredits(), []);
});

test("getConduct is empty until a computed conduct harness exists", () => {
  assert.deepEqual(provider.getConduct(), []);
});

test("getEvalRun is unavailable until a computed public harness result exists", () => {
  assert.equal(provider.getEvalRun(), null);
});

test("film mode carries an obvious synthetic and fictional-data disclosure", () => {
  const disclosure = provider.getDisclosure();
  assert.match(disclosure.label, /SYNTHETIC FILM — NOT LIVE/);
  assert.match(disclosure.label, /FICTIONAL DATA/);
  assert.match(disclosure.detail, /Every entity, identifier, event, query, and outcome/);
  assert.match(disclosure.detail, /not recovered money/);
});

test("getCommitLog is deterministic and bounded by the clock argument (Law 2 fence)", () => {
  const rec = provider.getRecordingStart();
  const day = 86400000;
  const oneDay = provider.getCommitLog(rec + day - 1);
  const twoDays = provider.getCommitLog(rec + day * 2 - 1);
  assert.ok(twoDays.length > oneDay.length, "log should grow as the clock advances");
  for (const entry of oneDay) {
    assert.ok(entry.at <= rec + day - 1, "no entry may be later than the clock passed in");
    assert.equal(typeof entry.hash, "string");
  }
});

test("getClockMode is film for MockProvider — live-mode pin is a different provider's job", () => {
  assert.equal(provider.getClockMode(), "film");
});

test("getInitialClock (T0) and getRecordingStart (REC) are in chronological order", () => {
  assert.ok(provider.getInitialClock() > provider.getRecordingStart());
});
