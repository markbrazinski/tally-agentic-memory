// ponytail: throwaway smoke test for LiveProvider (B1FE-S3), not a CI gate.
// Loads ?live, waits for the async GET /invoices to land, confirms no
// console/page errors and that at least the Queue shell renders.
// Usage: node scripts/live-smoke.mjs [url]
import { chromium } from "playwright";

const [, , url = "http://localhost:5173/?live"] = process.argv;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
const consoleLines = [];
page.on("pageerror", (err) => errors.push(String(err)));
page.on("console", (msg) => {
  consoleLines.push(`[${msg.type()}] ${msg.text()}`);
  if (msg.type() === "error") errors.push(msg.text());
});

await page.goto(url, { waitUntil: "networkidle" });
await page.waitForTimeout(2500); // boot + async GET /invoices round trip

const queueVisible = await page.locator('text="Queue"').count() > 0;
const bodyText = await page.locator("body").innerText();

// WS /feed handshake 403 is a known, documented infra gap (see
// s3-view-triage-proposal.md addendum) — not a code bug, doesn't fail the
// smoke test. Every other console/page error does.
const unexpectedErrors = errors.filter((e) => !String(e).includes("Unexpected response code: 403"));

console.log("console output:");
consoleLines.forEach((l) => console.log("  " + l));
console.log(`\npage/console errors: ${errors.length} (${unexpectedErrors.length} unexpected)`, errors);
console.log(`Queue heading visible: ${queueVisible}`);
console.log(`body text sample (first 300 chars): ${bodyText.slice(0, 300).replace(/\n+/g, " | ")}`);
console.log(`verdict: ${unexpectedErrors.length === 0 && queueVisible ? "PASS" : "FAIL — inspect output above"}`);

await browser.close();
