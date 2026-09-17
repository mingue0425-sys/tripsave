/* global maplibregl */

(function () {
  "use strict";

  const SLOTS = Object.freeze(["origin", "destination"]);
  const RAIN_DROP_COUNT = 4;
  const SNOWFLAKE_COUNT = 4;
  const SLEET_DROP_COUNT = 3;
  const SLEET_SNOWFLAKE_COUNT = 2;
  const config = window.KTO_CONFIG || {};
  const timezone = config.weatherTimezone || "Asia/Seoul";
  const state = {
    map: null,
    detail: {
      status: "idle",
      locations: { origin: null, destination: null },
      responses: { origin: null, destination: null },
      errors: { origin: null, destination: null },
      forecastDate: "",
    },
    markers: { origin: null, destination: null },
  };

  const CONDITION_ALIASES = Object.freeze({
    clear: "clear",
    sunny: "clear",
    맑음: "clear",
    청명: "clear",
    cloudy: "cloudy",
    partly_cloudy: "cloudy",
    partlycloudy: "cloudy",
    overcast: "cloudy",
    구름조금: "cloudy",
    구름많음: "cloudy",
    흐림: "cloudy",
    rain: "rain",
    shower: "rain",
    showers: "rain",
    drizzle: "rain",
    비: "rain",
    소나기: "rain",
    빗방울: "rain",
    snow: "snow",
    눈: "snow",
    눈날림: "snow",
    sleet: "sleet",
    rain_snow: "sleet",
    rainsnow: "sleet",
    "비/눈": "sleet",
    진눈깨비: "sleet",
    "구름많음/비": "rain",
    "구름많음/눈": "snow",
    "구름조금/비": "rain",
    "구름조금/눈": "snow",
  });

  const CONDITION_LABELS = Object.freeze({
    clear: "맑음",
    cloudy: "구름",
    rain: "비",
    snow: "눈",
    sleet: "진눈깨비",
    unknown: "확인 불가",
  });

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function validLocation(location) {
    return Boolean(
      location &&
        finite(location.lat) &&
        finite(location.lng) &&
        location.lat >= -90 &&
        location.lat <= 90 &&
        location.lng >= -180 &&
        location.lng <= 180,
    );
  }

  function normalizeCondition(value) {
    if (typeof value !== "string" || !value.trim()) {
      return "unknown";
    }
    const key = value.trim().toLowerCase().replace(/\s+/g, "");
    return CONDITION_ALIASES[key] || "unknown";
  }

  function formatNumber(value, digits = 0) {
    if (!finite(value)) {
      return null;
    }
    return Number(value.toFixed(digits)).toString();
  }

  function temperatureText(day) {
    if (!day || typeof day !== "object") {
      return null;
    }
    const min = formatNumber(day.temp_min_c);
    const max = formatNumber(day.temp_max_c);
    if (min !== null && max !== null) {
      return `${min}~${max}°`;
    }
    if (max !== null) {
      return `${max}°`;
    }
    if (min !== null) {
      return `${min}°`;
    }
    return null;
  }

  function temperatureDetail(day) {
    if (!day || typeof day !== "object") {
      return null;
    }
    const min = formatNumber(day.temp_min_c, 1);
    const max = formatNumber(day.temp_max_c, 1);
    if (min !== null && max !== null) {
      return `${min}°C ~ ${max}°C`;
    }
    if (max !== null) {
      return `${max}°C`;
    }
    if (min !== null) {
      return `${min}°C`;
    }
    return null;
  }

  function forecastFor(response, forecastDate) {
    if (!response || !Array.isArray(response.forecast)) {
      return null;
    }
    return (
      response.forecast.find((day) => day && day.date === forecastDate) ||
      response.forecast[0] ||
      null
    );
  }

  function displayName(slot, location) {
    if (location && typeof location.name === "string" && location.name.trim()) {
      return location.name.trim();
    }
    if (location && typeof location.label === "string" && location.label.trim()) {
      return location.label.trim();
    }
    return slot === "origin" ? "출발지" : "목적지";
  }

  function markerModel(slot, location, response, error, forecastDate) {
    const day = forecastFor(response, forecastDate);
    const condition = normalizeCondition(day?.normalized_condition || day?.condition);
    return {
      slot,
      location,
      response,
      day,
      error: error || null,
      condition,
      conditionLabel: CONDITION_LABELS[condition],
      temperature: temperatureText(day),
      name: displayName(slot, location),
      stale: Boolean(response?.stale || day?.stale || day?.status === "stale"),
    };
  }

  function appendCloud(visual) {
    const cloud = document.createElement("span");
    cloud.className = "weather-cloud";
    cloud.setAttribute("aria-hidden", "true");
    visual.appendChild(cloud);
  }

  function appendDrops(visual, count) {
    const rain = document.createElement("span");
    rain.className = "weather-rain";
    rain.setAttribute("aria-hidden", "true");
    for (let index = 0; index < count; index += 1) {
      const drop = document.createElement("i");
      drop.setAttribute("aria-hidden", "true");
      rain.appendChild(drop);
    }
    visual.appendChild(rain);
  }

  function appendSnowflakes(visual, count) {
    const snow = document.createElement("span");
    snow.className = "weather-snow";
    snow.setAttribute("aria-hidden", "true");
    for (let index = 0; index < count; index += 1) {
      const flake = document.createElement("i");
      flake.setAttribute("aria-hidden", "true");
      snow.appendChild(flake);
    }
    visual.appendChild(snow);
  }

  function appendVisual(visual, condition) {
    if (condition === "clear") {
      const sun = document.createElement("span");
      sun.className = "weather-sun";
      sun.setAttribute("aria-hidden", "true");
      visual.appendChild(sun);
      return;
    }
    if (condition === "cloudy") {
      appendCloud(visual);
      return;
    }
    if (condition === "rain") {
      appendCloud(visual);
      appendDrops(visual, RAIN_DROP_COUNT);
      return;
    }
    if (condition === "snow") {
      appendCloud(visual);
      appendSnowflakes(visual, SNOWFLAKE_COUNT);
      return;
    }
    if (condition === "sleet") {
      appendCloud(visual);
      appendDrops(visual, SLEET_DROP_COUNT);
      appendSnowflakes(visual, SLEET_SNOWFLAKE_COUNT);
      return;
    }
    const unknown = document.createElement("span");
    unknown.className = "weather-unknown";
    unknown.textContent = "?";
    unknown.setAttribute("aria-hidden", "true");
    visual.appendChild(unknown);
  }

  function createMarkerElement(model) {
    const element = document.createElement("div");
    element.className = `weather-map-marker weather-map-marker--${model.condition}`;
    if (model.stale) {
      element.classList.add("weather-map-marker--stale");
    }
    if (model.error) {
      element.classList.add("weather-map-marker--error");
    }
    element.setAttribute("role", "img");
    const temperatureLabel = model.temperature ? `, ${model.temperature}` : "";
    const ariaLabel = `${model.name} 날씨: ${model.conditionLabel}${temperatureLabel}`;
    element.setAttribute("aria-label", ariaLabel);
    element.title = ariaLabel;
    element.tabIndex = 0;
    element.dataset.weatherSlot = model.slot;

    const visual = document.createElement("span");
    visual.className = "weather-map-marker__visual";
    visual.setAttribute("aria-hidden", "true");
    appendVisual(visual, model.condition);
    element.appendChild(visual);

    if (model.temperature) {
      const temperature = document.createElement("span");
      temperature.className = "weather-temperature";
      temperature.textContent = model.temperature;
      temperature.setAttribute("aria-hidden", "true");
      element.appendChild(temperature);
    }
    return element;
  }

  function formatDateTime(value) {
    if (typeof value !== "string" || !value) {
      return null;
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return null;
    }
    return date.toLocaleString("ko-KR", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function popupText(model) {
    const lines = [model.name, `${model.conditionLabel}`];
    if (model.day?.condition && model.condition === "unknown") {
      lines.push(`원문 상태 ${model.day.condition}`);
    }
    const temperature = temperatureDetail(model.day);
    if (temperature) {
      lines.push(`기온 ${temperature}`);
    }
    if (finite(model.day?.precipitation_probability_pct)) {
      lines.push(`강수확률 ${formatNumber(model.day.precipitation_probability_pct)}%`);
    }
    if (finite(model.day?.humidity_pct)) {
      lines.push(`습도 ${formatNumber(model.day.humidity_pct)}%`);
    }
    if (finite(model.day?.wind_speed_mps)) {
      lines.push(`풍속 ${formatNumber(model.day.wind_speed_mps, 1)}m/s`);
    }
    if (model.day?.date) {
      lines.push(`예보 날짜 ${model.day.date}`);
    }
    const fetchedAt = formatDateTime(model.day?.fetched_at || model.response?.fetched_at);
    if (fetchedAt) {
      lines.push(`예보 기준 ${fetchedAt}`);
    }
    if (model.stale) {
      lines.push("최근 확인한 예보");
    }
    if (model.error) {
      lines.push(`조회 실패: ${model.error}`);
    }
    return lines.join("\n");
  }

  function removeMarker(slot) {
    const marker = state.markers[slot];
    if (marker && typeof marker.remove === "function") {
      marker.remove();
    }
    state.markers[slot] = null;
  }

  function renderWeatherMarker(slot) {
    removeMarker(slot);
    const location = state.detail.locations[slot];
    if (!state.map || !window.maplibregl || !validLocation(location)) {
      return false;
    }
    if (!state.detail.responses[slot] && !state.detail.errors[slot]) {
      return false;
    }
    const model = markerModel(
      slot,
      location,
      state.detail.responses[slot],
      state.detail.errors[slot],
      state.detail.forecastDate,
    );
    const marker = new maplibregl.Marker({
      anchor: "bottom",
      offset: [0, -22],
      element: createMarkerElement(model),
    })
      .setLngLat([location.lng, location.lat])
      .addTo(state.map);
    if (typeof maplibregl.Popup === "function" && typeof marker.setPopup === "function") {
      marker.setPopup(
        new maplibregl.Popup({ offset: 26, closeButton: true }).setText(
          popupText(model),
        ),
      );
    }
    state.markers[slot] = marker;
    return true;
  }

  function renderWeatherMarkers() {
    SLOTS.forEach(renderWeatherMarker);
    return SLOTS.filter((slot) => state.markers[slot]).length;
  }

  function clearWeatherMarkers() {
    SLOTS.forEach(removeMarker);
  }

  function setMapInstance(map) {
    if (state.map !== map) {
      clearWeatherMarkers();
    }
    state.map = map || null;
    renderWeatherMarkers();
  }

  function setWeatherMarkers(detail) {
    const next = detail && typeof detail === "object" ? detail : {};
    state.detail = {
      status: next.status || "idle",
      locations: {
        origin: next.locations?.origin || null,
        destination: next.locations?.destination || null,
      },
      responses: {
        origin: next.responses?.origin || null,
        destination: next.responses?.destination || null,
      },
      errors: {
        origin: next.errors?.origin || null,
        destination: next.errors?.destination || null,
      },
      forecastDate: next.forecastDate || next.startDate || "",
    };
    return renderWeatherMarkers();
  }

  window.addEventListener("kto:map-ready", (event) => {
    setMapInstance(event && event.detail ? event.detail.map : null);
  });
  window.addEventListener("kto:map-error", () => {
    setMapInstance(null);
  });
  window.addEventListener("kto:weather-state", (event) => {
    setWeatherMarkers(event ? event.detail : null);
  });

  window.KoreaTripWeatherLayer = Object.freeze({
    normalizeCondition,
    markerModel,
    createMarkerElement,
    setMapInstance,
    setWeatherMarkers,
    renderWeatherMarkers,
    clearWeatherMarkers,
    getMarkerCount: () => SLOTS.filter((slot) => state.markers[slot]).length,
    getState: () => ({
      map: state.map,
      detail: state.detail,
      markerCount: SLOTS.filter((slot) => state.markers[slot]).length,
    }),
  });
})();
