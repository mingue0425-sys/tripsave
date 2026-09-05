/* global maplibregl */

(function () {
  "use strict";

  let mapInstance = null;
  let originMarker = null;
  let destinationMarker = null;
  let markerState = { origin: null, destination: null };

  function markerForSlot(slot) {
    return slot === "origin" ? originMarker : destinationMarker;
  }

  function setMarkerForSlot(slot, marker) {
    if (slot === "origin") {
      originMarker = marker;
    } else {
      destinationMarker = marker;
    }
  }

  function slotLabel(slot) {
    return slot === "origin" ? "출발지" : "목적지";
  }

  function removeMarker(slot) {
    const marker = markerForSlot(slot);
    if (marker) {
      marker.remove();
      setMarkerForSlot(slot, null);
    }
  }

  function createMarkerElement(slot, location) {
    const element = document.createElement("div");
    element.className = `trip-marker trip-marker--${slot}`;
    element.setAttribute("role", "img");
    element.setAttribute(
      "aria-label",
      `${slotLabel(slot)}: ${location.label || `${location.lat}, ${location.lng}`}`
    );
    element.title = element.getAttribute("aria-label");

    const shape = document.createElement("span");
    shape.className = "trip-marker__shape";
    const letter = document.createElement("span");
    letter.className = "trip-marker__letter";
    letter.textContent = slot === "origin" ? "A" : "B";
    shape.appendChild(letter);
    element.appendChild(shape);
    return element;
  }

  function renderMarker(slot) {
    removeMarker(slot);
    const location = markerState[slot];
    if (!mapInstance || !location || !window.maplibregl) {
      return;
    }
    const marker = new maplibregl.Marker({
      anchor: "bottom",
      element: createMarkerElement(slot, location),
    })
      .setLngLat([location.lng, location.lat])
      .addTo(mapInstance);
    setMarkerForSlot(slot, marker);
  }

  function renderMarkers() {
    renderMarker("origin");
    renderMarker("destination");
  }

  function setMapInstance(map) {
    mapInstance = map;
    renderMarkers();
  }

  function sync(selectionState) {
    markerState = {
      origin: selectionState.origin || null,
      destination: selectionState.destination || null,
    };
    renderMarkers();
  }

  function setOriginMarker(location) {
    markerState.origin = location || null;
    renderMarker("origin");
  }

  function setDestinationMarker(location) {
    markerState.destination = location || null;
    renderMarker("destination");
  }

  function removeOriginMarker() {
    markerState.origin = null;
    removeMarker("origin");
  }

  function removeDestinationMarker() {
    markerState.destination = null;
    removeMarker("destination");
  }

  function clearTripMarkers() {
    markerState = { origin: null, destination: null };
    removeMarker("origin");
    removeMarker("destination");
  }

  window.KoreaTripMarkerManager = Object.freeze({
    setMapInstance,
    sync,
    setOriginMarker,
    setDestinationMarker,
    removeOriginMarker,
    removeDestinationMarker,
    clearTripMarkers,
  });
})();
