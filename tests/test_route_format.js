const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

global.window = {};
vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, "..", "static", "js", "route_format.js"), "utf8")
);

const format = window.KoreaTripRouteFormat;

test("formats distance at metre and kilometre thresholds", () => {
  assert.equal(format.formatDistance(999), "999 m");
  assert.equal(format.formatDistance(355214.5), "355.2 km");
});

test("formats driving duration naturally", () => {
  assert.equal(format.formatDuration(53 * 60), "53분");
  assert.equal(format.formatDuration(64 * 60), "1시간 04분");
  assert.equal(format.formatDuration(4 * 3600), "4시간");
});
