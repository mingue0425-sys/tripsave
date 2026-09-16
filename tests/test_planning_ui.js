const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function load(fileName, globalName) {
  const window = { KTO_CONFIG: {}, addEventListener() {}, dispatchEvent() {} };
  const document = { addEventListener() {}, getElementById() { return null; } };
  const context = { AbortController, console, document, URL, window };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "static", "js", fileName), "utf8"),
    context,
    { filename: fileName },
  );
  return window[globalName];
}

test("POI states keep unavailable and empty labels distinct", () => {
  const poi = load("poi.js", "KoreaTripPoi");
  assert.equal(poi.formatStatus("unavailable"), "확인 불가");
  assert.equal(poi.formatStatus("empty"), "0개");
  assert.equal(poi.formatStatus("partial"), "일부 확인");
});

test("itinerary rejects duplicate waypoint additions", () => {
  const itinerary = load("itinerary.js", "KoreaTripItinerary");
  assert.equal(itinerary.addWaypoint({ id: "a", name: "A", lat: 36, lng: 127 }), true);
  assert.equal(itinerary.addWaypoint({ id: "a", name: "A", lat: 36, lng: 127 }), false);
  assert.equal(itinerary.getState().waypoints.length, 1);
});

test("weather API preserves unknown state before a forecast is loaded", () => {
  const weather = load("weather.js", "KoreaTripWeather");
  assert.equal(weather.getState().response, null);
  assert.equal(weather.getForDate("2026-10-03"), null);
});
