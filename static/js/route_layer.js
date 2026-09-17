/* global maplibregl */

(function () {
  "use strict";

  const SOURCE_ID = "kto-route-source";
  const LAYER_ID = "kto-route-layer";
  let routeGeometry = null;
  let drawRetryScheduled = false;

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

  function scheduleStoredRouteDraw() {
    const map = getMap();

    if (
      !map ||
      drawRetryScheduled ||
      typeof map.once !== "function"
    ) {
      return false;
    }

    drawRetryScheduled = true;

    map.once("idle", () => {
      drawRetryScheduled = false;
      drawStoredRoute();
    });

    return true;
  }

  function drawStoredRoute() {
    if (!routeGeometry) {
      return false;
    }

    const map = getMap();

    if (!map || !window.maplibregl) {
      return false;
    }

    if (!map.isStyleLoaded()) {
      scheduleStoredRouteDraw();
      return false;
    }

    const feature = {
      type: "Feature",
      properties: {},
      geometry: {
        type: "LineString",
        coordinates: routeGeometry.coordinates.map(
          (coordinate) => coordinate.slice()
        ),
      },
    };

    try {
      const source = map.getSource(SOURCE_ID);

      if (source && typeof source.setData === "function") {
        source.setData(feature);
      } else {
        map.addSource(SOURCE_ID, {
          type: "geojson",
          data: feature,
        });
      }

      if (!map.getLayer(LAYER_ID)) {
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

      return true;
    } catch (error) {
      console.warn(
        "경로 선 렌더링을 재시도합니다.",
        error
      );
      scheduleStoredRouteDraw();
      return false;
    }
  }

  function setRouteGeometry(geometry) {
    if (!isValidGeometry(geometry)) {
      return false;
    }

    routeGeometry = {
      type: "LineString",
      coordinates: geometry.coordinates.map(
        (coordinate) => coordinate.slice()
      ),
    };

    // Rendering is best-effort. A temporary MapLibre/style state must never
    // invalidate an already successful OSRM route.
    drawStoredRoute();

    return true;
  }

  function clearRoute() {
    routeGeometry = null;
    drawRetryScheduled = false;

    const removed = removeMapRoute();

    if (!removed) {
      const map = getMap();

      if (map && typeof map.once === "function") {
        map.once("idle", removeMapRoute);
      }
    }

    return removed;
  }

  function fitRouteBounds() {
    const map = getMap();

    if (
      !map ||
      !routeGeometry ||
      !routeGeometry.coordinates.length ||
      !window.maplibregl
    ) {
      return false;
    }

    const bounds = new maplibregl.LngLatBounds();

    routeGeometry.coordinates.forEach(
      (coordinate) => bounds.extend(coordinate)
    );

    if (bounds.isEmpty()) {
      return false;
    }

    map.fitBounds(bounds, {
      padding: {
        top: 90,
        right: 90,
        bottom: 90,
        left: 90,
      },
      maxZoom: 12,
      duration: 650,
    });

    return true;
  }

  window.addEventListener("kto:map-ready", () => {
    if (!routeGeometry) {
      return;
    }

    drawStoredRoute();
    fitRouteBounds();
  });

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
