// Full FE click-audit. Crawls every reachable view of the Tally workbench and
// clicks every interactive control, asserting after each click that the app
// stayed healthy: no console errors, no uncaught page exceptions, no failed
// network requests, and the React root is still rendered (no white-screen).
//
// Runs against the MOCK provider (?provider=mock) so it is deterministic and
// needs no backend or database. Run: `npm run audit`.

import { expect, test } from "@playwright/test";

const BASE = process.env.AUDIT_BASE || "http://localhost:5173";

// Interactive elements: the app uses onClick handlers on <a href="#">, buttons,
// and cursor:pointer nodes (queue rows, day cells, chips). The app sets cursor
// via React inline-style objects, so it is only visible in COMPUTED style, not
// the style attribute — we tag those elements in the DOM before crawling.
async function tagClickables(page) {
  return await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("body *"));
    let n = 0;
    for (const el of nodes) {
      const cs = getComputedStyle(el);
      const isClickable =
        el.tagName === "A" ||
        el.tagName === "BUTTON" ||
        el.getAttribute("role") === "button" ||
        (cs.cursor === "pointer" &&
          // Only leaf-ish clickables: skip a pointer container that only wraps
          // other pointer nodes (avoids clicking the same action many times).
          !Array.from(el.children).some(
            (c) => getComputedStyle(c).cursor === "pointer",
          ));
      if (isClickable && el.offsetParent !== null) {
        el.setAttribute("data-audit-click", String(n++));
      }
    }
    return n;
  });
}

// Attach health listeners to a page; returns a live error collector.
function watch(page) {
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const t = msg.text();
      // Ignore benign dev noise (favicon, React devtools hint).
      if (/favicon|Download the React DevTools/i.test(t)) return;
      errors.push({ kind: "console", text: t });
    }
  });
  page.on("pageerror", (err) => errors.push({ kind: "pageerror", text: String(err) }));
  page.on("requestfailed", (req) => {
    const u = req.url();
    if (/favicon|__vite|node_modules|\.map$/.test(u)) return;
    errors.push({ kind: "requestfailed", text: `${req.method()} ${u} :: ${req.failure()?.errorText}` });
  });
  return errors;
}

async function rootHealthy(page) {
  // Root exists and has rendered content (not a blank error boundary).
  const root = page.locator("#root");
  await expect(root).toBeVisible();
  const text = (await root.innerText().catch(() => "")).trim();
  return text.length > 0;
}

// Describe a control for the report without relying on ids.
async function describe(el) {
  const tag = await el.evaluate((n) => n.tagName.toLowerCase()).catch(() => "?");
  let label = (await el.innerText().catch(() => "")) || "";
  label = label.replace(/\s+/g, " ").trim().slice(0, 48);
  return `${tag}${label ? ` "${label}"` : ""}`;
}

test.describe("Tally FE full click-audit (mock provider)", () => {
  test("every control on every view stays healthy when clicked", async ({ page }) => {
    const errors = watch(page);
    const report = [];
    const clickedLabels = new Set();

    await page.goto(`${BASE}/?provider=mock&internal=1`, { waitUntil: "networkidle" });
    // Let the mock arrival timer fire so the hero row + workbench exist.
    await page.waitForTimeout(2000);
    expect(await rootHealthy(page), "app should render on load").toBeTruthy();

    // The nav destinations that expose whole views. Visit each, then within each
    // click every control once.
    const navProbes = [
      { name: "Invoices (queue)", open: async () => { await clickText(page, "Invoices"); } },
      { name: "Source coverage", open: async () => { await clickText(page, "Source coverage"); } },
    ];

    // 1) Walk the two global views.
    for (const nav of navProbes) {
      await nav.open().catch(() => {});
      await page.waitForTimeout(400);
      const healthy = await rootHealthy(page);
      report.push({ view: nav.name, ok: healthy, errorsBefore: errors.length });
      expect(healthy, `${nav.name} renders`).toBeTruthy();
      await auditControlsOnView(page, nav.name, errors, report, clickedLabels);
    }

    // 2) Open the hero invoice → workbench, and walk it (this is the richest view:
    //    pipeline chips, ledger day cells, evidence drawer tabs, action rail).
    await clickText(page, "Invoices").catch(() => {});
    await page.waitForTimeout(300);
    await clickText(page, "INV-1048").catch(() => {});
    await page.waitForTimeout(1200);
    expect(await rootHealthy(page), "workbench opens").toBeTruthy();
    await auditControlsOnView(page, "Workbench (INV-1048)", errors, report, clickedLabels);

    // 3) Reviewer scenes (jump straight to each workbench state) — verifies the
    //    recommendation / send-gate / sent / insufficient renders don't throw.
    for (const scene of ["recommendation", "sendGate", "sendBlocked", "sent", "insufficient"]) {
      const scoped = watch(page);
      await page.goto(`${BASE}/?provider=mock&scene=${scene}&internal=1`, { waitUntil: "networkidle" });
      await page.waitForTimeout(600);
      const healthy = await rootHealthy(page);
      report.push({ view: `scene=${scene}`, ok: healthy, errorsBefore: scoped.length });
      expect(healthy, `scene ${scene} renders`).toBeTruthy();
      expect(scoped, `scene ${scene} has no errors`).toHaveLength(0);
    }

    // 4) Deep drill-downs: open the evidence drawer from the recommendation
    //    scene and click each drawer tab; open a charged-day cell. These are the
    //    nested surfaces the top-level crawl does not reach.
    await page.goto(`${BASE}/?provider=mock&scene=recommendation&internal=1`, { waitUntil: "networkidle" });
    await page.waitForTimeout(700);
    for (const daySel of ["Jun 10", "Jun 11", "Jun 8"]) {
      const before = errors.length;
      const cell = page.getByText(daySel, { exact: false }).first();
      if (await cell.count()) {
        await cell.click({ force: true }).catch(() => {});
        await page.waitForTimeout(200);
        // Click drawer tabs if the drawer opened.
        for (const tab of ["Source", "Used by", "Verification"]) {
          const t = page.getByText(tab, { exact: false }).first();
          if (await t.count()) { await t.click({ force: true }).catch(() => {}); await page.waitForTimeout(120); }
        }
        expect(await rootHealthy(page), `day drawer ${daySel} healthy`).toBeTruthy();
        expect(errors.length, `no errors opening ${daySel} drawer`).toBe(before);
        await page.keyboard.press("Escape").catch(() => {});
      }
    }
    report.push({ view: "Evidence drawer + day chain + tabs", ok: true });

    // Final report + hard assertion on accumulated errors.
    // eslint-disable-next-line no-console
    console.log("\n=== FE AUDIT REPORT ===");
    report.forEach((r) => console.log(`  [${r.ok ? "OK" : "FAIL"}] ${r.view}`));
    console.log(`  controls clicked: ${clickedLabels.size}`);
    if (errors.length) {
      console.log("  --- errors ---");
      errors.forEach((e) => console.log(`   * [${e.kind}] ${e.text}`));
    }
    expect(errors, "no console/page/network errors across the whole audit").toHaveLength(0);
  });
});

// A stable signature for a control so we click each distinct one once even as
// the DOM re-renders (many controls share text like "›", so include geometry).
async function signature(el) {
  return await el.evaluate((n) => {
    const r = n.getBoundingClientRect();
    const txt = (n.innerText || n.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().slice(0, 40);
    return `${n.tagName}|${Math.round(r.left)},${Math.round(r.top)}|${txt}`;
  });
}

// Click every distinct clickable control reachable from this view once; after
// each, assert the app stayed healthy. Snapshots the control list fresh on each
// pass (the DOM changes as drawers open) and stops when there is nothing new.
async function auditControlsOnView(page, viewName, errors, report, clickedLabels) {
  let clicked = 0;
  let guard = 0;
  for (;;) {
    if (guard++ > 80) break; // hard stop against pathological loops
    const total = await tagClickables(page);
    // Find the first tagged control whose signature we have not clicked yet.
    let picked = null;
    let pickedSig = null;
    for (let i = 0; i < total; i++) {
      const el = page.locator(`[data-audit-click="${i}"]`).first();
      if ((await el.count()) === 0 || !(await el.isVisible().catch(() => false))) continue;
      const sig = `${viewName}::${await signature(el)}`;
      if (clickedLabels.has(sig)) continue;
      picked = el; pickedSig = sig; break;
    }
    if (!picked) break; // nothing new to click on this view
    clickedLabels.add(pickedSig);
    const label = await describe(picked);
    const before = errors.length;
    await picked.click({ timeout: 1500, force: true }).catch(() => {});
    await page.waitForTimeout(110);
    const healthy = await rootHealthy(page);
    clicked += 1;
    if (!healthy || errors.length > before) {
      report.push({ view: `${viewName} → ${label}`, ok: healthy && errors.length === before });
    }
    expect(healthy, `after clicking "${label}" in ${viewName}, root still renders`).toBeTruthy();
    // Return to a clean state for the view so the next pick is reachable: close
    // any drawer and re-open the view root.
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(60);
  }
  report.push({ view: `${viewName} [${clicked} controls exercised]`, ok: true });
}

async function clickText(page, text) {
  // Click the first visible element containing this text.
  const loc = page.getByText(text, { exact: false }).first();
  await loc.click({ timeout: 2000 });
}
