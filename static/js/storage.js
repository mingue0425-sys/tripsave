(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const STORAGE_KEY = config.selectionStorageKey || "koreaTrip.selection.v1";
  const SCHEMA_VERSION = 1;
  const EARTH_RADIUS_METERS = 6371008.8;
  const OPTIONAL_LOCATION_FIELDS = [
    "id",
    "name",
    "address",
    "region",
    "type",
  ];

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isSafeText(value, maxLength) {
    return typeof value === "string" && value.length <= maxLength;
  }

  function isValidCoordinate(lat, lng) {
    return (
      typeof lat === "number" &&
      typeof lng === "number" &&
      Number.isFinite(lat) &&
      Number.isFinite(lng) &&
      lat >= -90 &&
      lat <= 90 &&
      lng >= -180 &&
      lng <= 180
    );
  }

  function isValidLocation(value) {
    if (!isPlainObject(value) || !isValidCoordinate(value.lat, value.lng)) {
      return false;
    }
    if (value.label !== null && value.label !== undefined && !isSafeText(value.label, 200)) {
      return false;
    }
    if (!isSafeText(value.source, 64)) {
      return false;
    }
    if (value.source !== "map" && value.source !== "local_search") {
      return false;
    }
    if (Array.isArray(value.aliases)) {
      if (
        value.aliases.length > 20 ||
        value.aliases.some((alias) => !isSafeText(alias, 100))
      ) {
        return false;
      }
    } else if (value.aliases !== undefined) {
      return false;
    }
    return OPTIONAL_LOCATION_FIELDS.every(
      (field) => value[field] === undefined || isSafeText(value[field], 200)
    );
  }

  function normalizeLocation(value) {
    if (!isValidLocation(value)) {
      return null;
    }
    const normalized = {
      lat: value.lat,
      lng: value.lng,
      label: value.label === undefined ? null : value.label,
      source: value.source,
    };
    OPTIONAL_LOCATION_FIELDS.forEach((field) => {
      if (value[field] !== undefined) {
        normalized[field] = value[field];
      }
    });
    if (value.aliases !== undefined) {
      normalized.aliases = value.aliases.slice();
    }
    return normalized;
  }

  function cloneLocation(location) {
    const normalized = normalizeLocation(location);
    return normalized ? { ...normalized } : null;
  }

  function isValidPersistedPayload(value) {
    return (
      isPlainObject(value) &&
      value.version === SCHEMA_VERSION &&
      (value.origin === null || isValidLocation(value.origin)) &&
      (value.destination === null || isValidLocation(value.destination))
    );
  }

  function safeRemoveItem() {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch (error) {
      console.warn("Could not remove saved trip selection:", error);
    }
  }

  function saveSelectionState(state) {
    const origin =
      state && state.origin === null ? null : cloneLocation(state && state.origin);
    const destination =
      state && state.destination === null
        ? null
        : cloneLocation(state && state.destination);
    if (
      !state ||
      (state.origin !== null && !origin) ||
      (state.destination !== null && !destination)
    ) {
      return false;
    }
    const payload = {
      version: SCHEMA_VERSION,
      origin,
      destination,
    };
    if (!isValidPersistedPayload(payload)) {
      return false;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
      return true;
    } catch (error) {
      console.warn("Could not save trip selection:", error);
      return false;
    }
  }

  function loadSelectionState() {
    let rawValue;
    try {
      rawValue = window.localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      console.warn("Could not read saved trip selection:", error);
      return { state: null, reason: "unavailable" };
    }
    if (rawValue === null) {
      return { state: null, reason: "empty" };
    }

    try {
      const parsed = JSON.parse(rawValue);
      if (!isValidPersistedPayload(parsed)) {
        safeRemoveItem();
        return { state: null, reason: "invalid" };
      }
      return {
        state: {
          version: SCHEMA_VERSION,
          origin: cloneLocation(parsed.origin),
          destination: cloneLocation(parsed.destination),
        },
        reason: null,
      };
    } catch (error) {
      console.warn("Saved trip selection was not valid JSON:", error);
      safeRemoveItem();
      return { state: null, reason: "invalid" };
    }
  }

  function clearSelectionState() {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
      return true;
    } catch (error) {
      console.warn("Could not clear saved trip selection:", error);
      return false;
    }
  }

  function toRadians(degrees) {
    return (degrees * Math.PI) / 180;
  }

  function haversineDistanceMeters(first, second) {
    if (!isValidLocation(first) || !isValidLocation(second)) {
      return Number.POSITIVE_INFINITY;
    }
    const deltaLat = toRadians(second.lat - first.lat);
    const deltaLng = toRadians(second.lng - first.lng);
    const firstLat = toRadians(first.lat);
    const secondLat = toRadians(second.lat);
    const haversine =
      Math.sin(deltaLat / 2) ** 2 +
      Math.cos(firstLat) * Math.cos(secondLat) * Math.sin(deltaLng / 2) ** 2;
    return 2 * EARTH_RADIUS_METERS * Math.asin(Math.sqrt(Math.min(1, haversine)));
  }

  window.KoreaTripStorage = Object.freeze({
    STORAGE_KEY,
    SCHEMA_VERSION,
    isValidCoordinate,
    isValidLocation,
    normalizeLocation,
    saveSelectionState,
    loadSelectionState,
    clearSelectionState,
    haversineDistanceMeters,
  });
})();
