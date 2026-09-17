const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, handler) {
      const current = listeners.get(type) || [];
      current.push(handler);
      listeners.set(type, current);
    },
    dispatchEvent(event) {
      (listeners.get(event.type) || []).slice().forEach((handler) => handler(event));
      return true;
    },
  };
}

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this.attributes = {};
    this.dataset = {};
    this.textContent = "";
    this.title = "";
    this.tabIndex = -1;
    this._classes = new Set();
    this.classList = {
      add: (...names) => {
        this.syncClasses();
        names.forEach((name) => this._classes.add(name));
        this.syncClasses();
      },
      toggle: (name, enabled) => {
        this.syncClasses();
        if (enabled) this._classes.add(name);
        else this._classes.delete(name);
        this.syncClasses();
      },
    };
  }

  syncClasses() {
    if (this.className) {
      this.className.split(/\s+/).filter(Boolean).forEach((name) => this._classes.add(name));
    }
    this.className = Array.from(this._classes).join(" ");
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }
}

function descendantsWithClass(node, className) {
  const result = [];
  node.children.forEach((child) => {
    if (child.className.split(/\s+/).includes(className)) result.push(child);
    result.push(...descendantsWithClass(child, className));
  });
  return result;
}

function loadWeatherLayer() {
  const window = eventTarget();
  window.KTO_CONFIG = { weatherTimezone: "Asia/Seoul" };
  const document = {
    createElement: (tagName) => new FakeElement(tagName),
    addEventListener() {},
    getElementById() {
      return null;
    },
  };
  class FakeMarker {
    constructor(options) {
      this.options = options;
      this.removed = false;
    }

    setLngLat(value) {
      this.lngLat = value;
      return this;
    }

    addTo(map) {
      this.map = map;
      map.markers.push(this);
      return this;
    }

    setPopup(popup) {
      this.popup = popup;
      return this;
    }

    remove() {
      this.removed = true;
    }
  }
  class FakePopup {
    setText(value) {
      this.text = value;
      return this;
    }
  }
  const maplibregl = { Marker: FakeMarker, Popup: FakePopup };
  window.maplibregl = maplibregl;
  const context = { console, document, maplibregl, window };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "static", "js", "weather_layer.js"), "utf8"),
    context,
    { filename: "weather_layer.js" },
  );
  return { layer: window.KoreaTripWeatherLayer, window };
}

function day(condition, overrides = {}) {
  return {
    date: "2026-09-17",
    condition,
    normalized_condition: condition,
    temp_min_c: 18,
    temp_max_c: 25,
    precipitation_probability_pct: 40,
    humidity_pct: 68,
    wind_speed_mps: 2.3,
    fetched_at: "2026-09-17T09:00:00+09:00",
    ...overrides,
  };
}

function loadWeatherController({ selection, fetchImpl }) {
  const window = eventTarget();
  window.KTO_CONFIG = { weatherTimezone: "Asia/Seoul" };
  window.KoreaTripSelection = {
    getOrigin: () => selection.origin,
    getDestination: () => selection.destination,
  };
  const nodes = new Map();
  ["weather-start-date", "weather-end-date"].forEach((id) => {
    const node = new FakeElement("input");
    node.value = id.endsWith("start-date") ? "2026-09-17" : "2026-09-19";
    node.addEventListener = () => {};
    nodes.set(id, node);
  });
  ["weather-search", "weather-summary", "weather-status", "accommodation-checkin", "accommodation-checkout"].forEach((id) => {
    const node = new FakeElement("div");
    node.addEventListener = () => {};
    nodes.set(id, node);
  });
  const domReady = [];
  const document = {
    getElementById: (id) => nodes.get(id) || null,
    addEventListener: (type, handler) => {
      if (type === "DOMContentLoaded") domReady.push(handler);
    },
  };
  class TestCustomEvent {
    constructor(type, init = {}) {
      this.type = type;
      this.detail = init.detail;
    }
  }
  window.CustomEvent = TestCustomEvent;
  const context = {
    AbortController,
    CustomEvent: TestCustomEvent,
    console,
    document,
    fetch: fetchImpl,
    window,
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "..", "static", "js", "weather.js"), "utf8"),
    context,
    { filename: "weather.js" },
  );
  return {
    weather: window.KoreaTripWeather,
    window,
    nodes,
    runDomReady() {
      domReady.forEach((handler) => handler());
    },
  };
}

test("weather conditions map to clear, cloudy, and rain marker states", () => {
  const { layer } = loadWeatherLayer();
  assert.equal(layer.normalizeCondition("맑음"), "clear");
  assert.equal(layer.normalizeCondition("구름많음"), "cloudy");
  assert.equal(layer.normalizeCondition("비"), "rain");
});

test("rain marker uses a finite set of animated drops", () => {
  const { layer } = loadWeatherLayer();
  const model = layer.markerModel(
    "destination",
    { name: "부산", lat: 35.1796, lng: 129.0756 },
    { forecast: [day("비")] },
    null,
    "2026-09-17",
  );
  const marker = layer.createMarkerElement(model);
  const drops = descendantsWithClass(marker, "weather-rain");
  assert.equal(drops.length, 1);
  assert.equal(drops[0].children.length, 4);
});

test("unknown condition stays unknown and does not become a sunny marker", () => {
  const { layer } = loadWeatherLayer();
  const model = layer.markerModel(
    "origin",
    { name: "서울", lat: 37.5665, lng: 126.978 },
    { forecast: [day(null, { normalized_condition: null, temp_min_c: null, temp_max_c: null })] },
    null,
    "2026-09-17",
  );
  const marker = layer.createMarkerElement(model);
  assert.equal(model.condition, "unknown");
  assert.equal(descendantsWithClass(marker, "weather-sun").length, 0);
  assert.equal(descendantsWithClass(marker, "weather-unknown").length, 1);
  assert.match(marker.attributes["aria-label"], /확인 불가/);
});

test("weather state survives until map-ready and old markers are removed on replacement", () => {
  const { layer } = loadWeatherLayer();
  const locations = {
    origin: { name: "서울", lat: 37.5665, lng: 126.978 },
    destination: { name: "부산", lat: 35.1796, lng: 129.0756 },
  };
  layer.setWeatherMarkers({
    status: "success",
    locations,
    responses: {
      origin: { status: "ok", forecast: [day("맑음")] },
      destination: { status: "ok", forecast: [day("비")] },
    },
    errors: { origin: null, destination: null },
    forecastDate: "2026-09-17",
  });
  assert.equal(layer.getMarkerCount(), 0);
  assert.equal(layer.getState().detail.responses.destination.status, "ok");

  const map = { markers: [] };
  layer.setMapInstance(map);
  assert.equal(layer.getMarkerCount(), 2);
  const firstOriginMarker = map.markers[0];
  layer.setWeatherMarkers({
    status: "success",
    locations: {
      origin: { name: "대전", lat: 36.3504, lng: 127.3845 },
      destination: locations.destination,
    },
    responses: {
      origin: { status: "ok", forecast: [day("맑음")] },
      destination: { status: "ok", forecast: [day("비")] },
    },
    errors: { origin: null, destination: null },
    forecastDate: "2026-09-17",
  });
  assert.equal(layer.getMarkerCount(), 2);
  assert.equal(firstOriginMarker.removed, true);

  layer.setWeatherMarkers({
    status: "success",
    locations: { origin: { name: "대전", lat: 36.3504, lng: 127.3845 }, destination: null },
    responses: { origin: { status: "ok", forecast: [day("맑음")] }, destination: null },
    errors: { origin: null, destination: null },
    forecastDate: "2026-09-17",
  });
  assert.equal(layer.getMarkerCount(), 1);
  assert.equal(map.markers.filter((marker) => marker.removed).length, 4);
});

test("automatic weather fetch covers origin and destination and ignores stale selection responses", async () => {
  const selection = {
    origin: { name: "서울", lat: 37.5665, lng: 126.978 },
    destination: { name: "부산", lat: 35.1796, lng: 129.0756 },
  };
  const pending = [];
  const fetchImpl = (_url, options) =>
    new Promise((resolve) => {
      pending.push({ resolve, options });
    });
  const controller = loadWeatherController({ selection, fetchImpl });
  controller.runDomReady();
  assert.equal(pending.length, 2);

  selection.destination = { name: "강릉", lat: 37.7519, lng: 128.8761 };
  controller.window.dispatchEvent(
    new controller.window.CustomEvent("kto:selection-changed", {
      detail: { origin: selection.origin, destination: selection.destination },
    }),
  );
  assert.equal(pending.length, 4);

  const currentPayload = {
    status: "ok",
    forecast: [day("맑음")],
  };
  pending[2].resolve({ ok: true, json: async () => currentPayload });
  pending[3].resolve({ ok: true, json: async () => currentPayload });
  await Promise.resolve();
  await Promise.resolve();
  pending[0].resolve({ ok: true, json: async () => ({ status: "ok", forecast: [day("비")] }) });
  pending[1].resolve({ ok: true, json: async () => ({ status: "ok", forecast: [day("비")] }) });
  await new Promise((resolve) => setImmediate(resolve));

  const state = controller.weather.getState();
  assert.equal(state.status, "success");
  assert.equal(state.responses.destination.forecast[0].condition, "맑음");
  assert.equal(state.responses.origin.forecast[0].condition, "맑음");
});
