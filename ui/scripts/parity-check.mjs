// ponytail: throwaway fidelity check, not a CI gate. Screenshots two URLs at
// 1920x1080, diffs with pixelmatch, prints a verdict. reducedMotion is a
// data-props value baked into both pages identically, not set by this script.
// Usage: node scripts/parity-check.mjs <rawUrl> <mountedUrl> [outDir]
import { chromium } from "playwright";
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";
import { writeFileSync, readFileSync, mkdirSync } from "node:fs";

const [, , rawUrl, mountedUrl, outDir = "scripts/parity-out"] = process.argv;
if (!rawUrl || !mountedUrl) {
  console.error("usage: node scripts/parity-check.mjs <rawUrl> <mountedUrl> [outDir]");
  process.exit(1);
}
mkdirSync(outDir, { recursive: true });

async function shoot(browser, url, outPath) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(String(err)));
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500); // dc-runtime boot + first paint
  await page.screenshot({ path: outPath });
  await page.close();
  return errors;
}

const browser = await chromium.launch();
const rawErrors = await shoot(browser, rawUrl, `${outDir}/raw.png`);
const mountedErrors = await shoot(browser, mountedUrl, `${outDir}/mounted.png`);
await browser.close();

const img1 = PNG.sync.read(readFileSync(`${outDir}/raw.png`));
const img2 = PNG.sync.read(readFileSync(`${outDir}/mounted.png`));
const { width, height } = img1;
const diff = new PNG({ width, height });
const sameSize = img2.width === width && img2.height === height;
const diffPixels = sameSize
  ? pixelmatch(img1.data, img2.data, diff.data, width, height, { threshold: 0.1 })
  : -1;
if (sameSize) writeFileSync(`${outDir}/diff.png`, PNG.sync.write(diff));

console.log(`raw console errors:     ${rawErrors.length}`, rawErrors);
console.log(`mounted console errors: ${mountedErrors.length}`, mountedErrors);
console.log(`same dimensions: ${sameSize} (${img1.width}x${img1.height} vs ${img2.width}x${img2.height})`);
console.log(`diff pixels: ${diffPixels} / ${width * height} (${sameSize ? ((100 * diffPixels) / (width * height)).toFixed(3) : "n/a"}%)`);
console.log(`verdict: ${sameSize && diffPixels === 0 ? "PIXEL-IDENTICAL" : sameSize && diffPixels < width * height * 0.001 ? "PARITY (negligible diff, likely animation timing)" : "MISMATCH — inspect diff.png"}`);
