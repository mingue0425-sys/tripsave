/* global maplibregl */

(function () {
  "use strict";

  const SOURCE_ID = "kto-route-source";
  const LAYER_ID = "kto-route-layer";
  let routeGeometry = null;

  function isValidCoordinate(coordinate) {
    return (
      Array.isArray(coordinate) &&
      coordinate.length === 2 &&
      typeof coordinate[0] === "number" &&
      typeof coordinate[1] === "number" &&
      Number.isFinite(coordinate[0]) &&
      Number.isFinite(coordinate[1]) &&
      coordinate[0] >= -180 &&
      coordinate[0] <= 180 &&
      coordinate[1] >= -90 &&
      coordinate[1] <= 90
    );
  }

  function isValidGeometry(geometry) {
    return (
      geometry &&
      geometry.type === "LineString" &&
      Array.isArray(geometry.coordinates) &&
      geometry.coordinates.length >= 2 &&
      geometry.coordinates.every(isValidCoordinate)
    );
  }

  function getMap() {
    return window.KoreaTripMap && window.KoreaTripMap.getMap
      ? window.KoreaTripMap.getMap()
      : null;
  }

  function removeMapRoute() {
    const map = getMap();
    if (!map || !map.isStyleLoaded()) {
      return false;
    }
    if (map.getLayer(LAYER_ID)) {
      map.removeLayer(LAYER_ID);
    }
    if (map.getSource(SOURCE_ID)) {
      map.removeSource(SOURCE_ID);
    }
    return true;
  }

  function setRouteGeometry(geometry) {
    if (!isValidGeometry(geometry)) {
      return false;
    }
    const map = getMap();
    if (!map || !map.isStyleLoaded() || !window.maplibregl) {
      return false;
    }
    const feature = {
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: geometry.coordinates.map((coordinate) => coordinate.slice()),
      },
    };
    const source = map.getSource(SOURCE_ID);
    if (source && typeof source.setData === "function") {
      source.setData(feature);
    } else {
      map.addSource(SOURCE_ID, {
        type: "geojson",
        data: feature,
      });
      map.addLayer({
        id: LAYER_ID,
        type: "line",
        source: SOURCE_ID,
        layout: {
          "line-cap": "round",
          "line-join": "round",
        },
        paint: {
          "line-color": "#e14d3d",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            5,
            2.5,
            10,
            4,
            16,
            7,
          ],
          "line-opacity": 0.9,
        },
      });
    }
    routeGeometry = feature.geometry;
    return true;
  }

  function clearRoute() {
    routeGeometry = null;
    return removeMapRoute();
  }

  function fitRouteBounds() {
    const map = getMap();
    if (
      !map ||
      !map.isStyleLoaded() ||
      !routeGeometry ||
      !routeGeometry.coordinates.length
    ) {
      return false;
    }
    const bounds = new maplibregl.LngLatBounds();
    routeGeometry.coordinates.forEach((coordinate) => bounds.extend(coordinate));
    if (bounds.isEmpty()) {
      return false;
    }
    map.fitBounds(bounds, {
      padding: { top: 90, right: 90, bottom: 90, left: 90 },
      maxZoom: 12,
      duration: 650,
    });
    return true;
  }

  window.KoreaTripRouteLayer = Object.freeze({
    SOURCE_ID,
    LAYER_ID,
    setRouteGeometry,
    clearRoute,
    fitRouteBounds,
    getGeometry: () =>
      routeGeometry
        ? {
            type: routeGeometry.type,
            coordinates: routeGeometry.coordinates.map((coordinate) =>
              coordinate.slice()
            ),
          }
        : null,
  });
})();
