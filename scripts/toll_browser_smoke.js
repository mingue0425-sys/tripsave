/*
 * Chromium smoke test for the V0.4 route/toll lifecycle.
 *
 * The browser talks only to FastAPI and the configured basemap.  The official
 * toll HTML request, when the route is supported, is made by FastAPI and is
 * audited separately by the live crawler test.
 */

const { chromium } = require("playwright");

const baseUrl = process.env.KTO_BASE_URL || "http://127.0.0.1:8765";
const chromePath =
  process.env.KTO_CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const forbiddenTerms = [
  "google",
  "kakao",
  "naver",
  "tmap",
  "mapbox",
  "router.project-osrm.org",
  "openrouteservice",
  "graphhopper",
  "analytics",
  "telemetry",
];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
  console.log(`[PASS] ${message}`);
}

async function waitForMap(page) {
  await page.waitForFunction(
    () => window.KoreaTripMap && window.KoreaTripMap.isReady(),
    null,
    { timeout: 30_000 }
  );
  await page.waitForFunction(
    () => {
      const map = window.KoreaTripMap.getMap();
      return map && map.isStyleLoaded() && map.areTilesLoaded();
    },
    null,
    { timeout: 30_000 }
  );
}

async function chooseSearch(page, slot, query, expectedName) {
  await page.locator(`#${slot}-search-toggle`).click();
  await page.locator(`#${slot}-search-input`).fill(query);
  await page.waitForSelector(`#${slot}-search-results .search-result`, {
    state: "visible",
    timeout: 10_000,
  });
  await page.locator(`#${slot}-search-results .search-result`).first().click();
  const state = await page.evaluate(() => window.KoreaTripSelection.getState());
  assert(state[slot] && state[slot].name === expectedName, `${slot} search selects ${expectedName}`);
}

async function waitForRoute(page) {
  await page.waitForFunction(
    () => {
      const state = window.KoreaTripRoute.getState();
      return state.status === "success" && state.route && state.route.distance_m > 0;
    },
    null,
    { timeout: 30_000 }
  );
}

async function waitForTollResult(page) {
  await page.waitForFunction(
    () => {
      const state = window.KoreaTripToll.getState();
      return state.status === "success" || state.status === "partial" || state.status === "error";
    },
    null,
    { timeout: 30_000 }
  );
}

async function run() {
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const requests = [];
  const consoleErrors = [];
  const pageErrors = [];

  page.on("request", (request) => requests.push(request.url()));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  try {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle", timeout: 30_000 });
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "networkidle", timeout: 30_000 });
    await waitForMap(page);

    await chooseSearch(page, "origin", "Seoul", "서울");
    await chooseSearch(page, "destination", "Daejeon", "대전");
    await page.locator("#route-calculate").click();
    await waitForRoute(page);
    const routeBeforeToll = await page.evaluate(() => window.KoreaTripRoute.getState().route);
    assert(routeBeforeToll && routeBeforeToll.route_id, "route has a stable route_id");

    assert(await page.locator("#toll-calculate").isEnabled(), "toll calculation enables after route success");
    await page.locator("#toll-calculate").click();
    await waitForTollResult(page);
    const tollState = await page.evaluate(() => window.KoreaTripToll.getState());
    assert(tollState.result !== null, "toll API returns a canonical result or an explicit partial result");
    assert(tollState.result.route_id === routeBeforeToll.route_id, "toll result is bound to the displayed route");
    assert(
      tollState.result.complete
        ? Number.isInteger(tollState.result.total_toll_krw) && tollState.result.total_toll_krw >= 0
        : tollState.result.total_toll_krw === null,
      "unknown toll is never displayed as zero"
    );
    assert((await page.locator(".toll-marker").count()) > 0, "detected OSM toll candidates render as TG markers");

    await page.locator("#vehicle-class").selectOption("compact");
    await page.waitForFunction(() => window.KoreaTripToll.getState().result === null);
    assert(await page.locator("#toll-calculate").isEnabled(), "vehicle change invalidates toll and allows recalculation");

    await chooseSearch(page, "destination", "Busan", "부산");
    await page.waitForFunction(
      () => window.KoreaTripRoute.getState().route === null && window.KoreaTripToll.getState().result === null
    );
    assert(true, "destination change invalidates route and toll together");

    const localHost = new URL(baseUrl).hostname;
    const allowedHosts = new Set([localHost, "tiles.openfreemap.org"]);
    const externalRequests = requests.filter((url) => {
      if (url.startsWith("blob:") || url.startsWith("data:")) return false;
      return !allowedHosts.has(new URL(url).hostname);
    });
    const forbiddenRequests = requests.filter((url) => {
      const lowered = url.toLowerCase();
      return forbiddenTerms.some((term) => lowered.includes(term));
    });
    assert(externalRequests.length === 0, "browser network has no unapproved external hosts");
    assert(forbiddenRequests.length === 0, "browser network has no forbidden routing/provider or telemetry hosts");
    assert(consoleErrors.length === 0 && pageErrors.length === 0, "Chromium has no console errors");
    console.log(JSON.stringify({ status: "PASS", requestCount: requests.length, externalRequests, forbiddenRequests, consoleErrors, pageErrors, tollState }, null, 2));
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
