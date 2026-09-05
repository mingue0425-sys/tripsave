const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const storage = new MemoryStorage();
global.window = {
  KTO_CONFIG: { selectionStorageKey: "koreaTrip.selection.v1" },
  localStorage: storage,
};
vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "static", "js", "storage.js"), "utf8")
);
const api = window.KoreaTripStorage;

function validLocation(overrides = {}) {
  return {
    lat: 35.1796,
    lng: 129.0756,
    label: "부산",
    source: "local_search",
    ...overrides,
  };
}

test.beforeEach(() => {
  storage.values.clear();
});

test("rejects invalid coordinates and unsafe source values", () => {
  assert.equal(api.isValidLocation(validLocation({ lat: 91 })), false);
  assert.equal(api.isValidLocation(validLocation({ lng: -181 })), false);
  assert.equal(api.isValidLocation(validLocation({ lat: "35.1" })), false);
  assert.equal(api.isValidLocation(validLocation({ lat: Number.NaN })), false);
  assert.equal(api.isValidLocation(validLocation({ lat: Number.POSITIVE_INFINITY })), false);
  assert.equal(api.isValidLocation(validLocation({ source: "javascript:alert(1)" })), false);
});

test("saves and restores versioned origin and destination", () => {
  const state = {
    origin: validLocation({ label: "부산", id: "city-busan" }),
    destination: validLocation({ lat: 37.7519, lng: 128.8761, label: "강릉" }),
  };

  assert.equal(api.saveSelectionState(state), true);
  const restored = api.loadSelectionState();
  assert.equal(restored.reason, null);
  assert.deepEqual(restored.state.origin, state.origin);
  assert.equal(restored.state.destination.label, "강릉");
  assert.equal(JSON.parse(storage.getItem(api.STORAGE_KEY)).version, 1);
});

test("discards malformed and unknown-version payloads", () => {
  storage.setItem(api.STORAGE_KEY, "not-json");
  assert.equal(api.loadSelectionState().reason, "invalid");
  assert.equal(storage.getItem(api.STORAGE_KEY), null);

  storage.setItem(
    api.STORAGE_KEY,
    JSON.stringify({ version: 99, origin: null, destination: null })
  );
  assert.equal(api.loadSelectionState().reason, "invalid");
  assert.equal(storage.getItem(api.STORAGE_KEY), null);
});

test("uses a small haversine threshold for nearby locations", () => {
  const same = validLocation();
  const nearby = validLocation({ lat: same.lat + 0.0001 });
  const farAway = validLocation({ lat: 37.7519, lng: 128.8761 });

  assert.ok(api.haversineDistanceMeters(same, nearby) > 0);
  assert.ok(api.haversineDistanceMeters(same, nearby) < 20);
  assert.ok(api.haversineDistanceMeters(same, farAway) > 20);
});
