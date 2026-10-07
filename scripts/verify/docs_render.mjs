// T10.1.d (plan.md): the documentation pages render in a real browser under
// their Content-Security-Policy. Headless Chromium through Playwright:
//
//   NODE_PATH=$(npm root -g) node scripts/verify/docs_render.mjs http://127.0.0.1:8000 [--mirror DIR]
//
// For /docs and /redoc: the page loads, Swagger UI (or Redoc) draws the API's
// operations from /openapi.json, and the browser reports no policy violation.
// `--mirror DIR` answers requests for https://cdn.jsdelivr.net/npm/<pkg>@<v>/<file>
// from DIR/<pkg>/<file> (the same npm package jsDelivr serves), for a host
// that cannot reach the CDN; other outside requests (fonts, the favicon) are then
// cut off, which is not a policy violation. The policy is enforced by the browser
// before any request is made, so answering from a mirror changes nothing about
// what it allows. One JSON line per page; exits 1 unless every page passed.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const args = process.argv.slice(2);
const base = args[0];
const mirrorAt = args.indexOf("--mirror");
const mirror = mirrorAt >= 0 ? args[mirrorAt + 1] : null;
const TYPES = { ".js": "application/javascript", ".css": "text/css", ".map": "application/json" };

const PAGES = {
  "/docs": { ready: "#swagger-ui .opblock", count: "#swagger-ui .opblock" },
  "/redoc": { ready: "redoc [data-section-id^='operation/']", count: "redoc [data-section-id^='operation/']" },
};

const browser = await chromium.launch({ executablePath: process.env.RW_CHROMIUM || undefined });
let failed = false;
for (const [route, want] of Object.entries(PAGES)) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const violations = [];
  const cutOff = [];
  const consoleErrors = [];
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.__csp.push(`${e.effectiveDirective} blocked ${e.blockedURI || "(inline)"}`));
  });
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
  if (mirror) {
    await page.route((url) => !url.href.startsWith(base), async (r) => {
      const m = r.request().url().match(/^https:\/\/cdn\.jsdelivr\.net\/npm\/((?:@[^/]+\/)?[^@/]+)@[^/]+\/(.+)$/);
      if (!m) { cutOff.push(r.request().url()); return r.abort(); }
      try {
        const body = await readFile(path.join(mirror, m[1], m[2]));
        return r.fulfill({ status: 200, body, contentType: TYPES[path.extname(m[2])] || "application/octet-stream",
                           headers: { "access-control-allow-origin": "*" } });
      } catch {
        cutOff.push(r.request().url());
        return r.abort();
      }
    });
  }
  const started = Date.now();
  const response = await page.goto(base + route, { waitUntil: "load" });
  let rendered = false;
  try {
    await page.waitForSelector(want.ready, { timeout: 15000 });
    rendered = true;
  } catch { /* reported below */ }
  const operations = await page.locator(want.count).count();
  violations.push(...(await page.evaluate(() => window.__csp)));
  const result = {
    page: route, status: response.status(), rendered, operations, ms: Date.now() - started,
    csp: response.headers()["content-security-policy"], violations, cut_off: cutOff, console_errors: consoleErrors,
  };
  console.log(JSON.stringify(result));
  failed ||= !(response.status() === 200 && rendered && operations > 0 && violations.length === 0);
  await context.close();
}
await browser.close();
process.exit(failed ? 1 : 0);
