/*
 * Chromium smoke test for the OpenFreeMap-backed basemap.
 *
 * Playwright is a verification-only tool and is intentionally not an
 * application runtime dependency. With a local Playwright installation:
 *   $env:NODE_PATH = (Resolve-Path .map-build\node_modules)
 *   node scripts\browser_smoke.js
 */

const { chromium } = require("playwright");

const baseUrl = process.env.KTO_BASE_URL || "http://127.0.0.1:8765";
const chromePath =
  process.env.KTO_CHROME_PATH ||
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const fallbackView = { center: [127.8, 36.0], zoom: 5.3 };
const regions = [
  { name: "서울", lng: 126.978, lat: 37.5665 },
  { name: "부산", lng: 129.0756, lat: 35.1796 },
  { name: "대구", lng: 128.6014, lat: 35.8714 },
  { name: "대전", lng: 127.3845, lat: 36.3504 },
  { name: "광주", lng: 126.8514, lat: 35.1595 },
  { name: "인천", lng: 126.7052, lat: 37.4563 },
  { name: "울산", lng: 129.3114, lat: 35.5384 },
  { name: "세종", lng: 127.289, lat: 36.48 },
  { name: "제주", lng: 126.5312, lat: 33.4996 },
  { name: "강릉", lng: 128.8761, lat: 37.7519 },
  { name: "전주", lng: 127.148, lat: 35.824 },
  { name: "경주", lng: 129.2248, lat: 35.8562 },
];
const forbiddenRequestTerms = [
  "google",
  "kakao",
  "naver",
  "mapbox",
  "openstreetmap.org",
  "analytics",
  "telemetry",
  "doubleclick",
  "segment.io",
  "mixpanel",
  "hotjar",
  "plausible.io",
  "router.project-osrm.org",
  "openrouteservice",
  "graphhopper",
  "tmap",
];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
  console.log(`[PASS] ${message}`);
}

function closeEnough(first, second, tolerance = 0.03) {
  return Math.abs(first - second) <= tolerance;
}

async function waitForMapReady(page) {
  await page.waitForFunction(
    () => window.KoreaTripMap && window.KoreaTripMap.isReady(),
    null,
    { timeout: 30000 }
  );
}

async function waitForStyleIdle(page) {
  await page.waitForFunction(
    () => {
      const map = window.KoreaTripMap && window.KoreaTripMap.getMap();
      return (
        map &&
        map.isStyleLoaded() &&
        !map.isMoving() &&
        map.areTilesLoaded()
      );
    },
    null,
    { timeout: 30000 }
  );
  await page.waitForTimeout(500);
}

async function moveMapTo(page, target) {
  await page.evaluate((destination) => {
    const map = window.KoreaTripMap.getMap();
    return new Promise((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) {
          return;
        }
        settled = true;
        resolve();
      };
      const timeout = setTimeout(finish, 30000);
      map.once("idle", () => {
        clearTimeout(timeout);
        finish();
      });
      map.flyTo({
        center: [destination.lng, destination.lat],
        zoom: destination.zoom || 14.5,
        duration: 0,
        essential: true,
      });
    });
  }, target);
  await page.waitForTimeout(300);
}

async function clickMapAt(page, lng, lat) {
  const point = await page.evaluate(
    ({ longitude, latitude }) => {
      const map = window.KoreaTripMap.getMap();
      const projected = map.project([longitude, latitude]);
      const rectangle = map.getContainer().getBoundingClientRect();
      return {
        x: rectangle.left + projected.x,
        y: rectangle.top + projected.y,
      };
    },
    { longitude: lng, latitude: lat }
  );
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(150);
}

async function chooseOnMap(page, slot, lng, lat) {
  await page.locator(`#${slot}-map-button`).click();
  assert(
    (await page.evaluate(() => window.KoreaTripSelection.getState())).selectionMode ===
      slot,
    `${slot} selection mode entered`
  );
  await clickMapAt(page, lng, lat);
  const state = await page.evaluate(() => window.KoreaTripSelection.getState());
  assert(
    state[slot] &&
      closeEnough(state[slot].lng, lng) &&
      closeEnough(state[slot].lat, lat),
    `${slot} selected from map`
  );
  const coordinateDisplay = await page.locator("#coordinate-display").textContent();
  assert(
    coordinateDisplay ===
      `${state[slot].lat.toFixed(6)}, ${state[slot].lng.toFixed(6)}`,
    `${slot} click updates coordinate display`
  );
  assert(state.selectionMode === "none", `${slot} map selection exits mode`);
}

async function chooseFromSearch(page, slot, query, expectedName) {
  await page.locator(`#${slot}-search-toggle`).click();
  await page.locator(`#${slot}-search-input`).fill(query);
  await page.waitForSelector(`#${slot}-search-results .search-result`, {
    state: "visible",
  });
  await page.locator(`#${slot}-search-results .search-result`).first().click();
  await page.waitForFunction(
    () => {
      const map = window.KoreaTripMap && window.KoreaTripMap.getMap();
      return map && !map.isMoving();
    },
    null,
    { timeout: 10000 }
  );
  await page.waitForTimeout(100);
  const state = await page.evaluate(() => window.KoreaTripSelection.getState());
  assert(
    state[slot] && state[slot].name === expectedName,
    `${slot} selected from local search: ${expectedName}`
  );
  assert(
    state[slot].source === "local_search",
    `${slot} search source is local_search`
  );
}

async function waitForRouteSuccess(page) {
  await page.waitForFunction(
    () => {
      const state = window.KoreaTripRoute && window.KoreaTripRoute.getState();
      return Boolean(
        state &&
          state.status === "success" &&
          state.route &&
          state.route.distance_m > 0 &&
          state.route.duration_s > 0
      );
    },
    null,
    { timeout: 30000 }
  );
  await page.waitForTimeout(900);
}

async function assertRouteCleared(page, message) {
  const observation = await page.evaluate(() => {
    const map = window.KoreaTripMap.getMap();
    const routeState = window.KoreaTripRoute.getState();
    return {
      status: routeState.status,
      route: routeState.route,
      sourcePresent: Boolean(map.getSource("kto-route-source")),
      layerPresent: Boolean(map.getLayer("kto-route-layer")),
    };
  });
  assert(
    observation.route === null &&
      observation.sourcePresent === false &&
      observation.layerPresent === false,
    message
  );
}

async function testRouteFlow(page) {
  let routeButton = page.locator("#route-calculate");
  assert(await routeButton.isEnabled(), "route calculation enables for two locations");
  await routeButton.click();
  await waitForRouteSuccess(page);

  let observation = await page.evaluate(() => {
    const map = window.KoreaTripMap.getMap();
    const state = window.KoreaTripSelection.getState();
    const routeState = window.KoreaTripRoute.getState();
    const geometry = routeState.route && routeState.route.geometry;
    return {
      state,
      routeState,
      geometry,
      sourcePresent: Boolean(map.getSource("kto-route-source")),
      layerPresent: Boolean(map.getLayer("kto-route-layer")),
      originMarkerCount: document.querySelectorAll(".trip-marker--origin").length,
      destinationMarkerCount: document.querySelectorAll(".trip-marker--destination").length,
      boundsContainSelections:
        map.getBounds().contains([state.origin.lng, state.origin.lat]) &&
        map.getBounds().contains([state.destination.lng, state.destination.lat]),
    };
  });
  assert(observation.routeState.status === "success", "local OSRM route succeeds in Chromium");
  assert(
    observation.geometry &&
      observation.geometry.type === "LineString" &&
      observation.geometry.coordinates.length >= 2,
    "route geometry is a non-empty GeoJSON LineString"
  );
  assert(observation.sourcePresent && observation.layerPresent, "route source and layer are rendered");
  assert(
    observation.originMarkerCount === 1 && observation.destinationMarkerCount === 1,
    "A/B markers remain visible with the route"
  );
  assert(observation.boundsContainSelections, "route fitBounds contains both selections");
  assert(await page.locator("#route-metrics").isVisible(), "route metrics are visible");
  assert(
    (await page.locator("#route-distance").textContent()).trim().length > 0 &&
      (await page.locator("#route-duration").textContent()).trim().length > 0,
    "route distance and duration are displayed"
  );

  await chooseFromSearch(page, "origin", "Seoul", "서울");
  await assertRouteCleared(page, "changing origin removes the stale route");
  await routeButton.click();
  await waitForRouteSuccess(page);
  assert(
    (await page.evaluate(() => window.KoreaTripRoute.getState())).status === "success",
    "route can be recalculated after changing origin"
  );

  await chooseFromSearch(page, "destination", "Busan", "부산");
  await assertRouteCleared(page, "changing destination removes the stale route");
  await chooseFromSearch(page, "destination", "Gangneung", "강릉");
  await routeButton.click();
  await waitForRouteSuccess(page);
  await page.locator("#swap-button").click();
  await assertRouteCleared(page, "swapping A/B invalidates the route");

  await page.locator("#clear-button").click();
  await assertRouteCleared(page, "clearing selections removes the route");
  assert(
    (await page.locator(".trip-marker--origin").count()) === 0 &&
      (await page.locator(".trip-marker--destination").count()) === 0,
    "clearing selections removes both route markers"
  );
}

async function testMapInteractions(page) {
  const mapPoint = await page.evaluate(() => {
    const map = window.KoreaTripMap.getMap();
    const rectangle = map.getContainer().getBoundingClientRect();
    return {
      x: rectangle.left + rectangle.width / 2,
      y: rectangle.top + rectangle.height / 2,
    };
  });
  const beforeZoom = await page.evaluate(() =>
    window.KoreaTripMap.getMap().getZoom()
  );
  await page.mouse.move(mapPoint.x, mapPoint.y);
  await page.mouse.wheel(0, -260);
  await page.waitForTimeout(400);
  const afterWheelZoom = await page.evaluate(() =>
    window.KoreaTripMap.getMap().getZoom()
  );
  assert(
    Math.abs(afterWheelZoom - beforeZoom) > 0.01,
    "mouse wheel changes map zoom"
  );

  await page.evaluate(({ center, zoom }) => {
    window.KoreaTripMap.getMap().jumpTo({ center, zoom });
  }, fallbackView);
  const beforeDrag = await page.evaluate(() =>
    window.KoreaTripMap.getMap().getCenter().toArray()
  );
  await page.mouse.move(mapPoint.x, mapPoint.y);
  await page.mouse.down();
  await page.mouse.move(mapPoint.x + 70, mapPoint.y + 25, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(400);
  const afterDrag = await page.evaluate(() =>
    window.KoreaTripMap.getMap().getCenter().toArray()
  );
    assert(
      Math.abs(afterDrag[0] - beforeDrag[0]) > 0.001 ||
        Math.abs(afterDrag[1] - beforeDrag[1]) > 0.001,
    "mouse drag changes map viewport"
    );
    const resizeSucceeded = await page.evaluate(() => {
      const map = window.KoreaTripMap.getMap();
      map.resize();
      const rectangle = map.getContainer().getBoundingClientRect();
      return rectangle.width > 0 && rectangle.height > 0;
    });
    assert(resizeSucceeded, "map resize remains safe");
    await page.evaluate(({ center, zoom }) => {
    window.KoreaTripMap.getMap().jumpTo({ center, zoom });
  }, fallbackView);
}

async function runSmokeTest() {
  const browser = await chromium.launch({
    headless: true,
    executablePath: chromePath,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  const requests = [];
  const failedRequests = [];
  const badResponses = [];
  const consoleErrors = [];
  const pageErrors = [];

  page.on("request", (request) => requests.push(request.url()));
  page.on("requestfailed", (request) =>
    failedRequests.push({
      url: request.url(),
      error: request.failure()?.errorText || "failed",
    })
  );
  page.on("response", (response) => {
    if (response.status() >= 400) {
      badResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  try {
    await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
    await page.evaluate(() => localStorage.clear());
    await page.reload({ waitUntil: "networkidle" });
    await waitForMapReady(page);
    await waitForStyleIdle(page);

    const basemapInfo = await page.evaluate(() => {
      const config = window.KTO_CONFIG;
      const map = window.KoreaTripMap.getMap();
      const definition = config.basemaps[config.defaultBasemap];
      const style = map.getStyle();
      const sourceLayerNames = [
        ...new Set(
          style.layers
            .map((layer) => layer["source-layer"])
            .filter(Boolean)
        ),
      ];
      return {
        provider: definition.provider,
        basemapId: config.defaultBasemap,
        styleUrl: definition.styleUrl,
        requestHostnames: definition.requestHostnames,
        sourceType: style.sources.openmaptiles?.type,
        sourceLayerNames,
      };
    });
    assert(basemapInfo.provider === "OpenFreeMap", "OpenFreeMap provider is active");
    assert(basemapInfo.basemapId === "liberty", "Liberty style is active");
    const selectionMarkerApiAvailable = await page.evaluate(
      () => typeof window.KoreaTripMap.setSelectionMarker === "function"
    );
    assert(
      selectionMarkerApiAvailable,
      "V0.1 selection marker API remains available"
    );
    assert(
      basemapInfo.sourceType === "vector",
      "OpenFreeMap vector source is active"
    );
    for (const layerName of [
      "transportation",
      "transportation_name",
      "building",
      "park",
      "water",
      "waterway",
      "boundary",
      "place",
    ]) {
      assert(
        basemapInfo.sourceLayerNames.includes(layerName),
        `basemap style includes ${layerName} layer`
      );
    }

    const attribution = await page.locator(".maplibregl-ctrl-attrib").innerText();
    assert(attribution.includes("OpenFreeMap"), "OpenFreeMap attribution is visible");
    assert(attribution.includes("OpenMapTiles"), "OpenMapTiles attribution is visible");
    assert(attribution.includes("OpenStreetMap"), "OpenStreetMap attribution is visible");

    const touchZoomEnabled = await page.evaluate(() => {
      const map = window.KoreaTripMap.getMap();
      return Boolean(map.touchZoomRotate && map.touchZoomRotate.isEnabled());
    });
    assert(touchZoomEnabled, "touch pinch zoom remains enabled");

    await testMapInteractions(page);

    for (const region of regions) {
      await moveMapTo(page, region);
      const observation = await page.evaluate(() => {
        const map = window.KoreaTripMap.getMap();
        const center = map.getCenter();
        const sourceLayers = [
          ...new Set(
            map
              .queryRenderedFeatures()
              .map((feature) => feature.sourceLayer)
              .filter(Boolean)
          ),
        ];
        return {
          center: [center.lng, center.lat],
          zoom: map.getZoom(),
          featureCount: map.queryRenderedFeatures().length,
          sourceLayers,
        };
      });
      assert(
        closeEnough(observation.center[0], region.lng) &&
          closeEnough(observation.center[1], region.lat),
        `${region.name} viewport centers correctly`
      );
      assert(
        observation.sourceLayers.includes("transportation"),
        `${region.name} renders road features`
      );
      assert(
        observation.sourceLayers.includes("place") ||
          observation.sourceLayers.includes("transportation_name"),
        `${region.name} renders label features`
      );
      assert(
        observation.sourceLayers.includes("building") ||
          observation.sourceLayers.includes("park") ||
          observation.sourceLayers.includes("water") ||
          observation.sourceLayers.includes("waterway"),
        `${region.name} renders detailed OSM features`
      );
    }

    await page.evaluate(({ center, zoom }) => {
      window.KoreaTripMap.getMap().jumpTo({ center, zoom });
    }, fallbackView);
    await waitForStyleIdle(page);

    let state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(
      state.origin === null &&
        state.destination === null &&
        state.selectionMode === "none",
      "initial selection state is empty"
    );
    await chooseOnMap(page, "origin", 129.0756, 35.1796);
    await chooseOnMap(page, "destination", 128.8761, 37.7519);
    assert(
      (await page.locator(".trip-marker--origin").count()) === 1 &&
        (await page.locator(".trip-marker--destination").count()) === 1,
      "origin and destination markers coexist"
    );
    await chooseOnMap(page, "origin", 126.978, 37.5665);
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(
      closeEnough(state.destination.lng, 128.8761) &&
        closeEnough(state.destination.lat, 37.7519),
      "changing origin preserves destination"
    );
    await chooseOnMap(page, "destination", 127.7, 37.6);
    await page.locator("#swap-button").click();
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(
      closeEnough(state.origin.lng, 127.7) &&
        closeEnough(state.destination.lng, 126.978),
      "swap exchanges origin and destination"
    );
    await page.locator("#destination-remove-button").click();
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(state.destination === null && state.origin !== null, "destination can be deleted");
    await page.locator("#origin-remove-button").click();
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(state.origin === null && state.destination === null, "origin can be deleted");

    await chooseFromSearch(page, "origin", "Seoul", "서울");
    await chooseFromSearch(page, "destination", "Gangneung", "강릉");
    await page.locator("#destination-search-toggle").click();
    await page.locator("#destination-search-input").fill("Seoul");
    await page.waitForSelector("#destination-search-results .search-result", {
      state: "visible",
    });
    await page.locator("#destination-search-results .search-result").first().click();
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(state.destination.name === "강릉", "same-location search is rejected");
    assert(
      (await page.locator("#selection-status").textContent()).includes("동일") ||
        (await page.locator("#selection-status").textContent()).includes("가깝"),
      "same-location error is visible"
    );

    await page.locator("#clear-button").click();
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(state.origin === null && state.destination === null, "clear resets selection");
    await chooseFromSearch(page, "origin", "Busan", "부산");
    await chooseFromSearch(page, "destination", "Gangneung", "강릉");
    const beforeReload = await page.evaluate(() => window.KoreaTripSelection.getState());
    await page.reload({ waitUntil: "networkidle" });
    await waitForMapReady(page);
    await waitForStyleIdle(page);
    state = await page.evaluate(() => window.KoreaTripSelection.getState());
    assert(
      state.origin.name === beforeReload.origin.name &&
        state.destination.name === beforeReload.destination.name,
      "reload restores localStorage locations"
    );
    assert(
      (await page.locator(".trip-marker--origin").count()) === 1 &&
        (await page.locator(".trip-marker--destination").count()) === 1,
      "reload restores both markers"
    );

    await testRouteFlow(page);
    state = await page.evaluate(() => window.KoreaTripSelection.getState());

    const localHost = new URL(baseUrl).hostname;
    const allowedBasemapHosts = new Set(basemapInfo.requestHostnames);
    const externalRequests = requests.filter((requestUrl) => {
      if (requestUrl.startsWith("blob:") || requestUrl.startsWith("data:")) {
        return false;
      }
      const parsed = new URL(requestUrl);
      return (
        parsed.hostname !== localHost &&
        !allowedBasemapHosts.has(parsed.hostname)
      );
    });
    const forbiddenRequests = requests.filter((requestUrl) => {
      const lowered = requestUrl.toLowerCase();
      return forbiddenRequestTerms.some((term) => lowered.includes(term));
    });
    const expectedBasemapCancellations = failedRequests.filter(
      ({ url, error }) =>
        error.includes("ERR_ABORTED") &&
        allowedBasemapHosts.has(new URL(url).hostname)
    );
    const blockingRequestFailures = failedRequests.filter(
      (failure) => !expectedBasemapCancellations.includes(failure)
    );
    assert(externalRequests.length === 0, "all non-local requests are approved basemap requests");
    assert(forbiddenRequests.length === 0, "no forbidden provider or telemetry request occurred");
    assert(
      blockingRequestFailures.length === 0,
      "no non-cancelled browser requests failed"
    );
    assert(badResponses.length === 0, "no browser request returned HTTP 4xx/5xx");
    assert(consoleErrors.length === 0 && pageErrors.length === 0, "browser console has no errors");

    const hostCounts = {};
    requests.forEach((requestUrl) => {
      if (requestUrl.startsWith("blob:") || requestUrl.startsWith("data:")) {
        return;
      }
      const hostname = new URL(requestUrl).hostname;
      hostCounts[hostname] = (hostCounts[hostname] || 0) + 1;
    });
    console.log(
      JSON.stringify(
        {
          status: "PASS",
          requestCount: requests.length,
          hostCounts,
          externalRequests,
          forbiddenRequests,
          failedRequests,
          expectedBasemapCancellations,
          blockingRequestFailures,
          badResponses,
          consoleErrors,
          pageErrors,
          finalState: state,
        },
        null,
        2
      )
    );
  } finally {
    await browser.close();
  }
}

runSmokeTest().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
