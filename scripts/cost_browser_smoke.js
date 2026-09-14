/*
 * Chromium E2E for the V0.5 driving-cost lifecycle.
 *
 * The browser talks to the local FastAPI app and configured basemap only. The
 * server-side Opinet request is covered by the live official integration test.
 */

const { chromium } = require("playwright");

const baseUrl = process.env.KTO_BASE_URL || "http://127.0.0.1:8765";
const chromePath = process.env.KTO_CHROME_PATH || null;
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

async function chooseSearch(page, slot, query, expectedName) {
  await page.locator(`#${slot}-search-toggle`).click();
  await page.locator(`#${slot}-search-input`).fill(query);
  await page.waitForSelector(`#${slot}-search-results .search-result`, {
    state: "visible",
    timeout: 10_000,
  });
  await page.locator(`#${slot}-search-results .search-result`).first().click();
  await page.waitForFunction(
    () => {
      const map = window.KoreaTripMap && window.KoreaTripMap.getMap();
      return map && !map.isMoving();
    },
    null,
    { timeout: 10_000 }
  );
  const state = await page.evaluate(() => window.KoreaTripSelection.getState());
  assert(state[slot] && state[slot].name === expectedName, `${slot} selects ${expectedName}`);
}

async function waitForMap(page) {
  await page.waitForFunction(
    () => window.KoreaTripMap && window.KoreaTripMap.isReady(),
    null,
    { timeout: 30_000 }
  );
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

async function waitForCost(page) {
  await page.waitForFunction(
    () => {
      const state = window.KoreaTripCost.getState();
      return state.status === "success" || state.status === "partial" || state.status === "error";
    },
    null,
    { timeout: 120_000 }
  );
}

async function calculateCost(page) {
  await page.locator("#driving-cost-calculate").click();
  await waitForCost(page);
  return page.evaluate(() => window.KoreaTripCost.getState());
}

async function run() {
  const launchOptions = { headless: true };
  if (chromePath) {
    launchOptions.executablePath = chromePath;
  }
  const browser = await chromium.launch(launchOptions);
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
    await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30_000 });
    await waitForMap(page);

    await chooseSearch(page, "origin", "Seoul", "서울");
    await chooseSearch(page, "destination", "Daejeon", "대전");
    await page.locator("#route-calculate").click();
    await waitForRoute(page);
    const route = await page.evaluate(() => window.KoreaTripRoute.getState().route);
    assert(route && route.distance_m > 0, "OSRM route distance is available to the cost engine");

    await page.locator("#fuel-efficiency").fill("13.5");
    await page.locator("#fuel-type").selectOption("gasoline");
    assert(await page.locator("#driving-cost-calculate").isEnabled(), "cost button enables after route and efficiency");
    const first = await calculateCost(page);
    assert(first.result && first.result.fuel.complete, "official web gasoline price produces complete fuel cost");
    assert(first.result.fuel.price_krw_per_l > 0, "gasoline price is positive and not a fallback zero");
    assert(first.result.fuel.fuel_volume_l > 0, "fuel volume uses the canonical route distance");
    assert(first.result.driving_cost.cost_complete, "toll plus fuel produces a complete driving cost");
    assert(Number.isInteger(first.result.driving_cost.outbound.total_krw), "outbound total is an integer KRW value");
    assert(Number.isInteger(first.result.driving_cost.return.total_krw), "return total is an integer KRW value");
    assert(Number.isInteger(first.result.driving_cost.round_trip.total_krw), "round-trip total is an integer KRW value");
    const costLegText = await page.locator(".driving-cost-metrics .cost-leg").allTextContents();
    assert(costLegText.length === 3, "UI renders outbound, return, and round-trip sections");
    assert(costLegText.some((text) => text.includes("가는 길")), "UI labels the outbound leg");
    assert(costLegText.some((text) => text.includes("오는 길")), "UI labels the return leg");
    assert(costLegText.some((text) => text.includes("왕복 합계")), "UI labels the round-trip aggregate");
    assert((await page.locator("#cost-outbound-fuel").textContent()).trim() !== "확인 불가", "UI renders outbound fuel cost");
    assert((await page.locator("#cost-return-fuel").textContent()).trim() !== "확인 불가", "UI renders return fuel cost");
    assert((await page.locator("#cost-outbound-total").textContent()).trim() === `${first.result.driving_cost.outbound.total_krw.toLocaleString("ko-KR")}원`, "UI outbound total matches the API JSON");
    assert((await page.locator("#cost-return-total").textContent()).trim() === `${first.result.driving_cost.return.total_krw.toLocaleString("ko-KR")}원`, "UI return total matches the API JSON");
    assert((await page.locator("#cost-round-trip-total").textContent()).trim() === `${first.result.driving_cost.round_trip.total_krw.toLocaleString("ko-KR")}원`, "UI round-trip total matches the API JSON");
    if (first.result.driving_cost.round_trip_toll.estimated) {
      assert((await page.locator("#cost-return-toll").textContent()).includes("추정"), "UI labels doubled outbound return toll as an estimate");
    }
    assert(
      first.result.driving_cost.round_trip.total_krw ===
        first.result.driving_cost.outbound.total_krw + first.result.driving_cost.return.total_krw,
      "round-trip total equals outbound plus return",
    );
    const tollState = await page.evaluate(() => window.KoreaTripToll.getState());
    assert(tollState.result && tollState.result.route_id === route.route_id, "aggregate result synchronizes the official toll panel");

    const gasolinePrice = first.result.fuel.price_krw_per_l;
    const previousFuel = first.result.fuel.one_way_krw;
    await page.locator("#fuel-efficiency").fill("14.0");
    await page.locator("#fuel-efficiency").dispatchEvent("change");
    await page.waitForFunction(
      (previous) => {
        const result = window.KoreaTripCost.getState().result;
        return result && result.fuel && result.fuel.fuel_efficiency_km_per_l === 14 && result.fuel.one_way_krw !== previous;
      },
      previousFuel,
      { timeout: 10_000 }
    );
    const efficiencyChanged = await page.evaluate(() => window.KoreaTripCost.getState().result);
    assert(efficiencyChanged.fuel.price_krw_per_l === gasolinePrice, "efficiency change reuses the verified price cache");
    assert(efficiencyChanged.driving_cost.outbound.toll_krw === first.result.driving_cost.outbound.toll_krw, "efficiency change preserves official toll");

    await page.locator("#fuel-type").selectOption("diesel");
    await page.waitForFunction(() => window.KoreaTripCost.getState().result === null, null, { timeout: 10_000 });
    const diesel = await calculateCost(page);
    assert(diesel.result && diesel.result.fuel.fuel_type === "diesel", "fuel type change invalidates and recalculates fuel");
    assert(diesel.result.fuel.complete && diesel.result.fuel.price_krw_per_l > 0, "official diesel price is used after fuel change");

    await chooseSearch(page, "destination", "Busan", "부산");
    await page.waitForFunction(
      () => window.KoreaTripRoute.getState().route === null && window.KoreaTripCost.getState().result === null,
      null,
      { timeout: 10_000 }
    );
    assert(true, "route change invalidates the stale aggregate cost");

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
    assert(forbiddenRequests.length === 0, "browser network has no prohibited providers or telemetry");
    assert(consoleErrors.length === 0 && pageErrors.length === 0, "Chromium has no console errors");
    console.log(JSON.stringify({ status: "PASS", requestCount: requests.length, externalRequests, forbiddenRequests, consoleErrors, pageErrors }, null, 2));
  } finally {
    await browser.close();
  }
}

run().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
