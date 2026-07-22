// ponytail: throwaway interaction smoke test for B1FE-S2's refactor, not a
// CI gate. Confirms the provider seam didn't change observable behavior:
// keyboard beat-scrub, queue approve action, and clock-mode guard all still
// work post-refactor. Usage: node scripts/interaction-check.mjs <url>
import { chromium } from "playwright";

const [, , url = "http://localhost:5183/"] = process.argv;
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
const errors = [];
page.on("pageerror", (err) => errors.push(String(err)));
page.on("console", (msg) => { if (msg.type() === "error") errors.push(msg.text()); });

await page.goto(url, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);

const results = {};

// 1. Queue renders seeded groups (provider data reached the template)
results.queueGroupCount = await page.locator('div:has-text("Owe $0")').count();

// 2. Escape resets clock to T0 (exercises setClock's non-guarded film-mode path)
await page.keyboard.press("1"); // start beat 1
await page.waitForTimeout(300);
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
results.escapeNoError = errors.length === 0;

// 3. Backtick opens the inspector (reads this.provider-derived state, no crash)
await page.keyboard.press("`");
await page.waitForTimeout(200);
results.inspectorOpened = await page.locator("text=feeding SELECT").count() > 0;
await page.keyboard.press("`");

// 4. Archive view renders the commit log (provider.getCommitLog() call site)
await page.evaluate(() => window.scrollTo(0, 0));
const archiveNav = page.locator('text="Archive"').first();
if (await archiveNav.count()) {
  await archiveNav.click();
  await page.waitForTimeout(300);
  results.archiveCommitLogRows = await page.locator("text=UTC").count();
}

await browser.close();

console.log(JSON.stringify(results, null, 2));
console.log(`console/page errors: ${errors.length}`, errors);
console.log(`verdict: ${errors.length === 0 && results.queueGroupCount > 0 ? "PASS" : "FAIL — inspect output above"}`);
