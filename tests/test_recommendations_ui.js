const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadBrowserModule(fileName, globalName) {
  const window = {
    KTO_CONFIG: {},
    addEventListener() {},
    dispatchEvent() {},
  };
  const document = {
    addEventListener() {},
    getElementById() {
      return null;
    },
  };
  const context = {
    AbortController,
    console,
    document,
    URL,
    window,
  };
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "js", fileName),
    "utf8"
  );
  vm.runInNewContext(source, context, { filename: fileName });
  assert.ok(window[globalName]);
  return window[globalName];
}

function loadInteractiveRecommendations(fetchImplementation) {
  const listeners = new Map();
  const domReady = [];
  const elements = new Map();
  const ids = [
    "recommendations-status",
    "recommendations-results",
    "recommendations-count",
    "recommendations-excluded",
    "recommendations-rank",
    "recommendation-mode",
    "recommendations-custom-toggle",
    "recommendations-custom-weights",
    "recommendation-weight-cost",
    "recommendation-weight-accommodation",
    "recommendation-weight-restaurant",
    "recommendation-weight-attraction",
    "recommendation-weight-driving",
    "recommendation-weight-cost-value",
    "recommendation-weight-accommodation-value",
    "recommendation-weight-restaurant-value",
    "recommendation-weight-attraction-value",
    "recommendation-weight-driving-value",
  ];
  ids.forEach((id) => {
    elements.set(id, {
      value: id === "recommendation-mode" ? "balanced" : "0",
      checked: false,
      hidden: false,
      disabled: false,
      textContent: "",
      classList: { toggle() {} },
      addEventListener() {},
      replaceChildren() {},
      appendChild() {},
    });
  });
  elements.get("recommendations-custom-toggle").checked = true;
  elements.get("recommendation-weight-cost").value = "100";

  const window = {
    KTO_CONFIG: {},
    addEventListener(name, callback) {
      const callbacks = listeners.get(name) || [];
      callbacks.push(callback);
      listeners.set(name, callbacks);
    },
    dispatchEvent() {},
    emit(name, detail) {
      (listeners.get(name) || []).forEach((callback) => callback({ detail }));
    },
  };
  const document = {
    addEventListener(name, callback) {
      if (name === "DOMContentLoaded") {
        domReady.push(callback);
      }
    },
    getElementById(id) {
      return elements.get(id) || null;
    },
  };
  const context = {
    AbortController,
    CustomEvent: class CustomEvent {
      constructor(type, init) {
        this.type = type;
        this.detail = init && init.detail;
      }
    },
    console,
    document,
    fetch: fetchImplementation,
    URL,
    window,
  };
  const source = fs.readFileSync(
    path.join(__dirname, "..", "static", "js", "recommendations.js"),
    "utf8"
  );
  vm.runInNewContext(source, context, { filename: "recommendations.js" });
  domReady.forEach((callback) => callback());
  return { api: window.KoreaTripRecommendations, window, elements };
}

test("unavailable and unknown place counts never render as zero", () => {
  const recommendations = loadBrowserModule(
    "recommendations.js",
    "KoreaTripRecommendations"
  );
  const candidate = {
    component_statuses: {
      restaurants: "unavailable",
      attractions: "unknown",
    },
  };
  assert.equal(recommendations.formatPlaceCount(candidate, "restaurants", 0), "확인 불가");
  assert.equal(recommendations.formatPlaceCount(candidate, "attractions", 0), "확인 불가");
});

test("empty and partial place counts preserve their source semantics", () => {
  const trips = loadBrowserModule("trips.js", "KoreaTripCandidates");
  assert.equal(
    trips.formatPlaceCount(
      { component_statuses: { restaurants: "empty" } },
      "restaurants",
      0
    ),
    "0곳"
  );
  assert.equal(
    trips.formatPlaceCount(
      { component_statuses: { attractions: "partial" } },
      "attractions",
      2
    ),
    "2곳 · 일부만 확인"
  );
  assert.equal(
    trips.formatPlaceCount({}, "restaurants", 0),
    "확인 불가"
  );
});

test("ranking binds the request to its candidate set and ignores stale responses", async () => {
  const calls = [];
  let resolveRequest;
  const request = new Promise((resolve) => {
    resolveRequest = resolve;
  });
  const { api, window } = loadInteractiveRecommendations((url, options) => {
    calls.push({ url, options });
    return request;
  });
  const candidate = {
    id: "tc_a",
    trip_type: "DAY_TRIP",
    costs: { status: "VERIFIED_COMPLETE", total_krw: 1000 },
    quality: {},
    component_statuses: {},
  };
  const fingerprintA = "a".repeat(64);
  const fingerprintB = "b".repeat(64);
  window.emit("kto:trip-candidates", {
    candidate_set_id: "cs_set_a_1234567890123456",
    request_fingerprint: fingerprintA,
    candidates: [candidate],
  });

  const ranking = api.rank();
  assert.equal(calls.length, 1);
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.mode, "custom");
  assert.equal(payload.candidate_set_id, "cs_set_a_1234567890123456");
  assert.equal(payload.request_fingerprint, fingerprintA);
  assert.deepEqual(payload.candidate_ids, [candidate.id]);
  assert.equal(Object.hasOwn(payload, "candidates"), false);

  window.emit("kto:trip-candidates", {
    candidate_set_id: "cs_set_b_1234567890123456",
    request_fingerprint: fingerprintB,
    candidates: [{ ...candidate, id: "tc_b" }],
  });
  resolveRequest({
    ok: true,
    async json() {
      return {
        candidate_set_id: "cs_set_a_1234567890123456",
        request_fingerprint: fingerprintA,
        recommendations: [],
        excluded_candidates: [],
        eligible_count: 0,
      };
    },
  });

  assert.equal(await ranking, false);
  assert.equal(api.getState().candidateSetId, "cs_set_b_1234567890123456");
  assert.equal(api.getState().candidateFingerprint, fingerprintB);
  assert.equal(api.getState().response, null);
});
