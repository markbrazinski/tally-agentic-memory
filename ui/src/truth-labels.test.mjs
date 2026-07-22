import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
const mockProvider = await readFile(new URL("./data/mock-provider.js", import.meta.url), "utf8");
const liveProvider = await readFile(new URL("./data/live-provider.js", import.meta.url), "utf8");

test("the shell renders a persistent data-mode badge", () => {
  assert.match(html, /title="\{\{ modeDetail \}\}"/);
  assert.match(html, />\{\{ modeLabel \}\}<\/div>/);
});

test("the System view does not publish fabricated evaluation totals", () => {
  assert.doesNotMatch(html, /41 hostile invoices/);
  assert.doesNotMatch(html, /300 of 300 pass/);
  assert.match(html, /Evaluation unavailable/);
  assert.match(html, /Re-bill lineage and business-day\/LFD arithmetic are not evaluated/);
});

test("default film does not claim recovered money or conduct ratios", () => {
  assert.match(mockProvider, /const credits = \[\];/);
  assert.match(mockProvider, /const conduct = \[\];/);
  assert.match(html, /No recovered money claimed/);
  assert.match(html, /no carrier credit or external resolution is claimed/);
  assert.doesNotMatch(html, /credits a synthetic \$700/);
});

test("synthetic query examples are labeled not executed and do not claim credential isolation", () => {
  assert.match(html, /synthetic preview · not executed/);
  assert.match(html, /No executed query-log feed is connected/);
  assert.match(html, /Credentials are not tenant-restricted/);
  assert.doesNotMatch(html, /scoped credentials/);
  assert.doesNotMatch(html, /isolated tenant/);
});

test("live bearer credentials are never bundled through Vite build variables", () => {
  assert.doesNotMatch(html, /VITE_TALLY_BEARER_TOKEN/);
  assert.match(html, /window\.__TALLY_RUNTIME_CONFIG__/);
});

test("live case view uses the real replay route rather than the film timeline", () => {
  assert.match(liveProvider, /\/cases\/\$\{encodeURIComponent\(id\)\}\/replay/);
  assert.match(html, /this\.provider\.replayCase\(id\)/);
  assert.match(html, /LIVE COCKROACH REPLAY/);
});

test("public film uses explicitly fictional carrier and terminal names", () => {
  for (const prohibited of ["Northstar Ocean Lines", "Bluehaven Maritime", "Horizon Ocean", "Seabrook Shipping", "Crescent Marine", "Pier 400", "TTI"]) {
    assert.doesNotMatch(html, new RegExp(prohibited));
    assert.doesNotMatch(mockProvider, new RegExp(prohibited));
  }
  assert.match(html, /Fictional Northstar Lines/);
  assert.match(html, /Fictional Pier Alpha/);
});
