/* global maplibregl */

(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const fallbackView = {
    center: [127.8, 36.0],
    zoom: 5.3,
  };

  let mapInstance = null;
  let mapReady = false;
  let initializationStarted = false;
  let legacySelectionMarker = null;
  let activeBasemapDefinition = null;

  function getElement(id) {
    return document.getElementById(id);
  }

  function setMessageVisibility(id, visible) {
    const element = getElement(id);
    if (!element) {
      return;
    }
    element.hidden = !visible;
    element.classList.toggle("is-hidden", !visible);
  }

  function setMapState(message, isError) {
    const state = getElement("map-state");
    if (!state) {
      return;
    }
    state.textContent = message;
    state.classList.toggle("map-state--error", Boolean(isError));
  }

  function updateCoordinateDisplay(lat, lng) {
    const display = getElement("coordinate-display");
    if (
      !display ||
      !Number.isFinite(lat) ||
      !Number.isFinite(lng)
    ) {
      return false;
    }
    display.textContent = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
    return true;
  }

  function getBasemapDefinition() {
    const basemaps = config.basemaps;
    const basemapId = config.defaultBasemap;
    const definition = basemaps && basemaps[basemapId];
    if (
      !definition ||
      typeof definition.id !== "string" ||
      typeof definition.provider !== "string" ||
      typeof definition.styleUrl !== "string" ||
      !definition.styleUrl ||
      typeof definition.attribution !== "string"
    ) {
      throw new Error(`Basemap configuration is missing for '${basemapId}'.`);
    }
    return definition;
  }

  function basemapTitle(definition) {
    return `${definition.provider} · ${definition.id}`;
  }

  function showMapError(message) {
    setMessageVisibility("map-loading", false);
    setMessageVisibility("map-error", true);
    const detail = getElement("map-error-detail");
    if (detail) {
      detail.textContent = message;
    }
    setMapState("Basemap unavailable", true);
  }

  function handleMapClick(event) {
    if (!event || !event.lngLat) {
      return false;
    }
    const { lat, lng } = event.lngLat;
    updateCoordinateDisplay(lat, lng);
    if (
      window.KoreaTripSelection &&
      typeof window.KoreaTripSelection.handleMapClick === "function"
    ) {
      return window.KoreaTripSelection.handleMapClick({ lat, lng });
    }
    // Keep the V0.1 public fallback when map.js is used without V0.2 modules.
    return setSelectionMarker(lng, lat);
  }

  function handleMapError(event) {
    const detail = event && event.error ? event.error : event;
    if (!mapReady) {
      console.error("Basemap failed to load:", detail);
      const provider = activeBasemapDefinition
        ? activeBasemapDefinition.provider
        : "Configured basemap";
      showMapError(
        `${provider} basemap could not be loaded. Check the configured provider connection.`
      );
      window.dispatchEvent(
        new CustomEvent("kto:map-error", { detail: { error: detail } })
      );
      return;
    }
    // A tile can fail after the style is usable. Keep the application usable
    // while making the provider problem visible to developers.
    console.warn("Basemap tile warning:", detail);
  }

  function getLocationBounds(locations) {
    const bounds = new maplibregl.LngLatBounds();
    locations.forEach((location) => {
      if (
        location &&
        Number.isFinite(location.lng) &&
        Number.isFinite(location.lat)
      ) {
        bounds.extend([location.lng, location.lat]);
      }
    });
    return bounds;
  }

  function setSelectionMarker(lng, lat) {
    if (
      !mapInstance ||
      !Number.isFinite(lng) ||
      !Number.isFinite(lat)
    ) {
      return false;
    }
    if (legacySelectionMarker) {
      legacySelectionMarker.remove();
    }
    legacySelectionMarker = new maplibregl.Marker({ color: "#b33b35" })
      .setLngLat([lng, lat])
      .addTo(mapInstance);
    return true;
  }

  function removeSelectionMarker() {
    if (legacySelectionMarker) {
      legacySelectionMarker.remove();
      legacySelectionMarker = null;
    }
  }

  function flyToLocation(location) {
    if (
      !mapInstance ||
      !mapReady ||
      !location ||
      !Number.isFinite(location.lng) ||
      !Number.isFinite(location.lat)
    ) {
      return false;
    }
    mapInstance.flyTo({
      center: [location.lng, location.lat],
      zoom: Math.max(mapInstance.getZoom(), 7.2),
      essential: true,
    });
    return true;
  }

  function fitLocations(locations) {
    if (!mapInstance || !mapReady || !Array.isArray(locations)) {
      return false;
    }
    const validLocations = locations.filter(
      (location) =>
        location &&
        Number.isFinite(location.lng) &&
        Number.isFinite(location.lat)
    );
    if (validLocations.length === 0) {
      return false;
    }
    if (validLocations.length === 1) {
      return flyToLocation(validLocations[0]);
    }

    const bounds = getLocationBounds(validLocations);
    if (bounds.isEmpty()) {
      return false;
    }
    mapInstance.fitBounds(bounds, {
      padding: { top: 90, right: 90, bottom: 90, left: 90 },
      maxZoom: 8.5,
      duration: 650,
    });
    return true;
  }

  function notifyMapReady(definition) {
    mapReady = true;
    if (
      window.KoreaTripMarkerManager &&
      typeof window.KoreaTripMarkerManager.setMapInstance === "function"
    ) {
      window.KoreaTripMarkerManager.setMapInstance(mapInstance);
    }
    window.dispatchEvent(
      new CustomEvent("kto:map-ready", {
        detail: { map: mapInstance, basemap: definition },
      })
    );
  }

  function initializeMap() {
    if (initializationStarted) {
      return;
    }
    initializationStarted = true;
    mapReady = false;

    if (!window.maplibregl) {
      const message = "Local MapLibre GL JS asset is not available.";
      console.error(message);
      showMapError(message);
      window.dispatchEvent(new CustomEvent("kto:map-error"));
      return;
    }

    try {
      const definition = getBasemapDefinition();
      activeBasemapDefinition = definition;
      mapInstance = new maplibregl.Map({
        container: "map",
        style: definition.styleUrl,
        center: fallbackView.center,
        zoom: fallbackView.zoom,
        attributionControl: false,
      });
      mapInstance.addControl(new maplibregl.NavigationControl(), "top-right");
      mapInstance.addControl(
        new maplibregl.AttributionControl({
          compact: false,
          customAttribution: definition.attribution,
        }),
        "bottom-right"
      );
      mapInstance.on("click", handleMapClick);
      mapInstance.on("error", handleMapError);
      mapInstance.once("load", function () {
        setMessageVisibility("map-loading", false);
        setMessageVisibility("map-error", false);
        setMapState(basemapTitle(definition), false);
        notifyMapReady(definition);
      });
    } catch (error) {
      console.error("Unable to initialize the basemap:", error);
      showMapError(
        "The configured basemap could not be initialized. Check the provider configuration."
      );
      window.dispatchEvent(
        new CustomEvent("kto:map-error", { detail: { error } })
      );
    }
  }

  window.KoreaTripMap = Object.freeze({
    initializeMap,
    getMap: () => mapInstance,
    isReady: () => mapReady,
    getBasemap: () => {
      try {
        return { ...getBasemapDefinition() };
      } catch (error) {
        return null;
      }
    },
    setSelectionMarker,
    removeSelectionMarker,
    handleMapClick,
    updateCoordinateDisplay,
    flyToLocation,
    fitLocations,
  });

  document.addEventListener("DOMContentLoaded", initializeMap);
})();
