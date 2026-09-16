/* global maplibregl */

(function () {
  "use strict";
  const SOURCE_ID = "kto-itinerary-route-source";
  const LAYER_ID = "kto-itinerary-route-layer";
  let mapInstance = null;
  let geometry = null;

  function removeLayerAndSource() {
    if (!mapInstance) return;
    if (typeof mapInstance.getLayer === "function" && mapInstance.getLayer(LAYER_ID)) mapInstance.removeLayer(LAYER_ID);
    if (typeof mapInstance.getSource === "function" && mapInstance.getSource(SOURCE_ID)) mapInstance.removeSource(SOURCE_ID);
  }
  function render() {
    if (!mapInstance || !geometry || !window.maplibregl) return;
    removeLayerAndSource();
    mapInstance.addSource(SOURCE_ID, { type: "geojson", data: { type: "Feature", properties: {}, geometry } });
    mapInstance.addLayer({ id: LAYER_ID, type: "line", source: SOURCE_ID, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#7c3aed", "line-width": 5, "line-opacity": .8, "line-dasharray": [1.5, 1] } });
  }
  function setMapInstance(map) { mapInstance = map || null; render(); }
  function setGeometry(value) { geometry = value && value.type === "LineString" ? value : null; render(); }
  function clearRoute() { geometry = null; removeLayerAndSource(); }
  window.addEventListener("kto:map-ready", (event) => setMapInstance(event && event.detail ? event.detail.map : null));
  window.KoreaTripItineraryLayers = Object.freeze({ setMapInstance, setGeometry, clearRoute });
})();
