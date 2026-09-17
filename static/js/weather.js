(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.weatherApiUrl || "/api/weather/forecast";
  const TIMEZONE = config.weatherTimezone || "Asia/Seoul";
  const WEATHER_SLOTS = Object.freeze(["origin", "destination"]);
  const CLIENT_CACHE_LIMIT = 16;
  const state = {
    status: "idle",
    responses: { origin: null, destination: null },
    errors: { origin: null, destination: null },
    response: null,
    error: null,
    requestId: 0,
    startDate: "",
    endDate: "",
  };
  let activeController = null;
  let latestSelection = { origin: null, destination: null };
  const responseCache = new Map();
  const inFlightRequests = new Map();

  function element(id) {
    return document.getElementById(id);
  }

  function selection() {
    const api = window.KoreaTripSelection;
    if (!api) {
      return { origin: null, destination: null };
    }
    return {
      origin: typeof api.getOrigin === "function" ? api.getOrigin() : null,
      destination:
        typeof api.getDestination === "function" ? api.getDestination() : null,
    };
  }

  function dateValue(id) {
    return element(id)?.value || "";
  }

  function dateKey(start = dateValue("weather-start-date"), end = dateValue("weather-end-date")) {
    return `${start}|${end}`;
  }

  function locationFingerprint(location) {
    if (!location || !Number.isFinite(location.lat) || !Number.isFinite(location.lng)) {
      return "none";
    }
    return `${location.lat}|${location.lng}`;
  }

  function selectionFingerprint(value = latestSelection, dates = dateKey()) {
    return `${locationFingerprint(value.origin)}::${locationFingerprint(value.destination)}|${dates}`;
  }

  function requestKey(location, start, end) {
    return `${locationFingerprint(location)}|${start}|${end}|${TIMEZONE}`;
  }

  function setStatus(message, type) {
    const target = element("weather-status");
    if (!target) {
      return;
    }
    target.textContent = message;
    target.classList.toggle("weather-status--error", type === "error");
    target.classList.toggle("weather-status--success", type === "success");
    target.classList.toggle("weather-status--partial", type === "partial");
    target.classList.toggle("weather-status--stale", type === "stale");
  }

  function render() {
    const button = element("weather-search");
    const summary = element("weather-summary");
    if (button) {
      button.disabled =
        !latestSelection.origin ||
        !latestSelection.destination ||
        !state.startDate ||
        !state.endDate ||
        state.status === "loading";
      button.textContent = state.status === "loading" ? "날씨 확인 중…" : "날씨 다시 확인";
    }
    if (summary) {
      if (!latestSelection.origin || !latestSelection.destination) {
        summary.textContent = "출발지와 목적지를 선택하면 지도 위에 날씨를 표시합니다.";
      } else if (state.status === "loading") {
        summary.textContent = "출발지와 목적지의 날씨를 지도에 표시하는 중입니다.";
      } else {
        summary.textContent = "지도 위 날씨 marker를 클릭하면 상세 예보를 확인할 수 있습니다.";
      }
    }
  }

  function validDateRange(start, end) {
    return Boolean(start && end && end >= start);
  }

  function localDateValue(value) {
    return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
  }

  function setDefaultDates() {
    const accommodationStart = element("accommodation-checkin")?.value || "";
    const accommodationEnd = element("accommodation-checkout")?.value || "";
    const start = element("weather-start-date");
    const end = element("weather-end-date");
    if (!start || !end) {
      return;
    }
    if (accommodationStart) {
      start.value = accommodationStart;
    }
    if (accommodationEnd) {
      end.value = accommodationEnd;
    }
    if (!start.value) {
      const today = new Date();
      today.setDate(today.getDate() + 1);
      start.value = localDateValue(today);
    }
    if (!end.value) {
      const date = new Date(`${start.value}T00:00:00`);
      date.setDate(date.getDate() + 2);
      end.value = localDateValue(date);
    }
    state.startDate = start.value;
    state.endDate = end.value;
  }

  function cacheResponse(key, payload) {
    responseCache.delete(key);
    responseCache.set(key, payload);
    while (responseCache.size > CLIENT_CACHE_LIMIT) {
      responseCache.delete(responseCache.keys().next().value);
    }
  }

  function fetchLocationWeather(location, start, end, signal) {
    const key = requestKey(location, start, end);
    if (responseCache.has(key)) {
      return Promise.resolve(responseCache.get(key));
    }
    const existing = inFlightRequests.get(key);
    if (existing && existing.signal === signal && !signal.aborted) {
      return existing.promise;
    }
    const request = fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lat: location.lat,
        lng: location.lng,
        start_date: start,
        end_date: end,
        timezone: TIMEZONE,
      }),
      signal,
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => null);
        if (!response.ok || !payload || !Array.isArray(payload.forecast)) {
          throw new Error("날씨 확인에 실패했습니다.");
        }
        cacheResponse(key, payload);
        return payload;
      })
      .finally(() => {
        if (inFlightRequests.get(key)?.promise === request) {
          inFlightRequests.delete(key);
        }
      });
    inFlightRequests.set(key, { promise: request, signal });
    return request;
  }

  function stateDetail() {
    return {
      status: state.status,
      responses: {
        origin: state.responses.origin,
        destination: state.responses.destination,
      },
      errors: {
        origin: state.errors.origin,
        destination: state.errors.destination,
      },
      locations: {
        origin: latestSelection.origin,
        destination: latestSelection.destination,
      },
      startDate: state.startDate,
      endDate: state.endDate,
      forecastDate: state.startDate,
      requestId: state.requestId,
      fingerprint: selectionFingerprint(latestSelection, dateKey(state.startDate, state.endDate)),
    };
  }

  function publish(eventName = "kto:weather-state") {
    window.dispatchEvent(new CustomEvent(eventName, { detail: stateDetail() }));
  }

  function clear(message) {
    state.requestId += 1;
    if (activeController) {
      activeController.abort();
    }
    activeController = null;
    state.status = "idle";
    state.responses = { origin: null, destination: null };
    state.errors = { origin: null, destination: null };
    state.response = null;
    state.error = null;
    publish();
    render();
    if (message) {
      setStatus(message, "active");
    }
  }

  function aggregateStatus(responses, errors) {
    const loaded = WEATHER_SLOTS.filter((slot) => responses[slot]);
    const failed = WEATHER_SLOTS.filter((slot) => errors[slot]);
    if (!loaded.length && failed.length) {
      return "error";
    }
    if (failed.length || loaded.length !== WEATHER_SLOTS.length) {
      return "partial";
    }
    if (loaded.some((slot) => responses[slot].status === "stale" || responses[slot].stale)) {
      return "stale";
    }
    if (loaded.every((slot) => responses[slot].status === "ok")) {
      return "success";
    }
    return "partial";
  }

  function errorMessage(error) {
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return String(error || "날씨를 확인하지 못했습니다.");
  }

  async function search() {
    const nextSelection = selection();
    latestSelection = {
      origin: nextSelection.origin || null,
      destination: nextSelection.destination || null,
    };
    const start = dateValue("weather-start-date") || state.startDate;
    const end = dateValue("weather-end-date") || state.endDate;
    state.startDate = start;
    state.endDate = end;
    if (!latestSelection.origin || !latestSelection.destination) {
      setStatus("출발지와 목적지를 먼저 선택해 주세요.", "error");
      render();
      return false;
    }
    if (!validDateRange(start, end)) {
      setStatus("날짜 범위를 올바르게 입력해 주세요.", "error");
      render();
      return false;
    }

    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const fingerprint = selectionFingerprint(latestSelection, dateKey(start, end));
    activeController = new AbortController();
    state.status = "loading";
    state.responses = { origin: null, destination: null };
    state.errors = { origin: null, destination: null };
    state.response = null;
    state.error = null;
    render();
    publish();
    setStatus("출발지와 목적지의 예보를 확인하고 있습니다.", "active");

    const results = await Promise.all(
      WEATHER_SLOTS.map(async (slot) => {
        try {
          const payload = await fetchLocationWeather(
            latestSelection[slot],
            start,
            end,
            activeController.signal,
          );
          return { slot, payload, error: null };
        } catch (error) {
          return { slot, payload: null, error };
        }
      }),
    );

    if (
      requestId !== state.requestId ||
      fingerprint !== selectionFingerprint(selection(), dateKey())
    ) {
      return false;
    }

    const responses = { origin: null, destination: null };
    const errors = { origin: null, destination: null };
    results.forEach(({ slot, payload, error }) => {
      responses[slot] = payload;
      errors[slot] = error ? errorMessage(error) : null;
    });
    state.responses = responses;
    state.errors = errors;
    state.response = responses.destination;
    state.error = errors.origin || errors.destination || null;
    state.status = aggregateStatus(responses, errors);
    publish("kto:weather-state");
    publish("kto:weather-result");
    render();
    if (state.status === "success") {
      setStatus("출발지와 목적지의 날씨를 지도에 표시했습니다.", "success");
    } else if (state.status === "stale") {
      setStatus("최근 확인한 예보를 지도에 표시했습니다.", "stale");
    } else if (state.status === "partial") {
      setStatus("일부 날씨를 확인하지 못했습니다. 지도에서 확인 가능한 위치만 표시합니다.", "partial");
    } else {
      setStatus("날씨를 확인하지 못했습니다. 지도의 경로와 다른 기능은 계속 사용할 수 있습니다.", "error");
    }
    if (requestId === state.requestId) {
      activeController = null;
      render();
    }
    return state.status !== "error";
  }

  function handleSelectionChanged(event) {
    const next = event && event.detail ? event.detail : selection();
    const nextSelection = {
      origin: next.origin || null,
      destination: next.destination || null,
    };
    const changed =
      locationFingerprint(nextSelection.origin) !== locationFingerprint(latestSelection.origin) ||
      locationFingerprint(nextSelection.destination) !== locationFingerprint(latestSelection.destination);
    latestSelection = nextSelection;
    if (!changed) {
      render();
      return;
    }
    if (!latestSelection.origin || !latestSelection.destination) {
      clear("출발지와 목적지를 모두 선택하면 지도에 날씨를 표시합니다.");
      return;
    }
    if (validDateRange(dateValue("weather-start-date"), dateValue("weather-end-date"))) {
      void search();
    } else {
      clear("날씨 날짜를 준비하는 중입니다.");
    }
  }

  window.addEventListener("kto:selection-changed", handleSelectionChanged);

  window.KoreaTripWeather = Object.freeze({
    search,
    clear: () => clear("날씨 결과를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      response: state.response,
      responses: { ...state.responses },
      errors: { ...state.errors },
      error: state.error,
      startDate: state.startDate,
      endDate: state.endDate,
    }),
    getForDate: (value) =>
      state.responses.destination?.forecast?.find((day) => day.date === value) || null,
    getForLocation: (slot, value = state.startDate) =>
      state.responses[slot]?.forecast?.find((day) => day.date === value) || null,
  });

  document.addEventListener("DOMContentLoaded", () => {
    latestSelection = selection();
    setDefaultDates();
    ["weather-start-date", "weather-end-date"].forEach((id) => {
      element(id)?.addEventListener("change", () => {
        state.startDate = dateValue("weather-start-date");
        state.endDate = dateValue("weather-end-date");
        if (latestSelection.origin && latestSelection.destination && validDateRange(state.startDate, state.endDate)) {
          void search();
        } else {
          clear("날짜가 변경되어 날씨를 초기화했습니다.");
        }
      });
    });
    ["accommodation-checkin", "accommodation-checkout"].forEach((id) => {
      element(id)?.addEventListener("change", () => {
        if (state.status === "idle" || state.status === "error") {
          setDefaultDates();
          if (latestSelection.origin && latestSelection.destination && validDateRange(state.startDate, state.endDate)) {
            void search();
          } else {
            render();
          }
        }
      });
    });
    element("weather-search")?.addEventListener("click", search);
    render();
    if (latestSelection.origin && latestSelection.destination && validDateRange(state.startDate, state.endDate)) {
      void search();
    }
  });
})();
