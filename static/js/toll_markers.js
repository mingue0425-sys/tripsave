/* global maplibregl */

(function () {
  "use strict";

  let mapInstance = null;
  let matchedGates = [];
  let markers = [];

  function removeMarkers() {
    markers.forEach((marker) => marker.remove());
    markers = [];
  }

  function validGate(match) {
    const gate = match && match.gate;
    return (
      gate &&
      Number.isFinite(gate.lng) &&
      Number.isFinite(gate.lat) &&
      gate.lng >= -180 &&
      gate.lng <= 180 &&
      gate.lat >= -90 &&
      gate.lat <= 90
    );
  }

  function createMarkerElement(match) {
    const gate = match.gate;
    const element = document.createElement("div");
    element.className = "toll-marker";
    element.setAttribute("role", "img");
    element.textContent = "TG";
    const name = gate.name || "이름 없는 톨게이트 후보";
    element.setAttribute("aria-label", `톨게이트 후보: ${name}`);
    element.title = element.getAttribute("aria-label");
    return element;
  }

  function popupText(match) {
    const gate = match.gate;
    const lines = [
      gate.name || "이름 없는 톨게이트 후보",
      "OSM 톨게이트 후보",
      `경로 거리 ${Math.round(match.distance_to_route_m)}m`,
    ];
    if (gate.operator) {
      lines.push(`운영: ${gate.operator}`);
    }
    return lines.join("\n");
  }

  function render() {
    removeMarkers();
    if (!mapInstance || !window.maplibregl) {
      return;
    }
    matchedGates.filter(validGate).forEach((match) => {
      const marker = new maplibregl.Marker({
        anchor: "bottom",
        element: createMarkerElement(match),
      })
        .setLngLat([match.gate.lng, match.gate.lat])
        .addTo(mapInstance);
      if (typeof maplibregl.Popup === "function") {
        marker.setPopup(
          new maplibregl.Popup({ offset: 24, closeButton: true }).setText(
            popupText(match)
          )
        );
      }
      markers.push(marker);
    });
  }

  function setMapInstance(map) {
    mapInstance = map || null;
    render();
  }

  function setTollGates(nextGates) {
    matchedGates = Array.isArray(nextGates) ? nextGates.slice() : [];
    render();
    return matchedGates.length;
  }

  function clearTollMarkers() {
    matchedGates = [];
    removeMarkers();
  }

  window.addEventListener("kto:map-ready", (event) => {
    setMapInstance(event && event.detail ? event.detail.map : null);
  });

  window.KoreaTripTollMarkers = Object.freeze({
    setMapInstance,
    setTollGates,
    clearTollMarkers,
    getGates: () => matchedGates.slice(),
  });
})();
