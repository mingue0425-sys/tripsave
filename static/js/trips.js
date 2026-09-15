(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.tripCandidatesApiUrl || "/api/trips/candidates";
  const state = {
    status: "idle",
    response: null,
    error: null,
    requestId: 0,
  };
  let activeController = null;
  let latestInputKey = null;

  function getElement(id) {
    return document.getElementById(id);
  }

  function setStatus(message, type) {
    const element = getElement("trip-candidates-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    ["error", "success", "partial"].forEach((value) => {
      element.classList.toggle(`trip-candidates-status--${value}`, type === value);
    });
  }

  function formatWon(value) {
    return Number.isFinite(value) && value >= 0
      ? `${Math.round(value).toLocaleString("ko-KR")}원`
      : "확인 불가";
  }

  function formatRating(value, confidence) {
    if (!Number.isFinite(value)) {
      return "평점 미확인";
    }
    const text = `평점 ${(value * 100).toFixed(1)} / 100`;
    return Number.isFinite(confidence)
      ? `${text} · 신뢰도 ${(confidence * 100).toFixed(0)}%`
      : text;
  }

  function selection() {
    const value = window.KoreaTripSelection;
    return value && typeof value.getOrigin === "function"
      ? { origin: value.getOrigin(), destination: value.getDestination() }
      : { origin: null, destination: null };
  }

  function locationPayload(value) {
    return value
      ? {
          lat: value.lat,
          lng: value.lng,
          label: value.label || value.name || null,
          source: value.source || "map",
        }
      : null;
  }

  function readInteger(id, minimum, maximum) {
    const element = getElement(id);
    if (!element || element.value.trim() === "") {
      return null;
    }
    const value = Number(element.value);
    return Number.isInteger(value) && value >= minimum && value <= maximum ? value : null;
  }

  function vehiclePayload(costState) {
    const efficiency = costState && Number.isFinite(costState.fuelEfficiency)
      ? costState.fuelEfficiency
      : Number(getElement("fuel-efficiency")?.value);
    const vehicleClass = getElement("vehicle-class")?.value || "class_1";
    const fuelType = costState && typeof costState.fuelType === "string"
      ? costState.fuelType
      : getElement("fuel-type")?.value || "gasoline";
    return {
      fuel_type: fuelType,
      efficiency_km_per_l: efficiency,
      vehicle_class: vehicleClass,
    };
  }

  function currentInput() {
    const selected = selection();
    const routeState = window.KoreaTripRoute && window.KoreaTripRoute.getState
      ? window.KoreaTripRoute.getState()
      : { route: null };
    const costState = window.KoreaTripCost && window.KoreaTripCost.getState
      ? window.KoreaTripCost.getState()
      : { result: null };
    const startDate = getElement("accommodation-checkin")?.value || "";
    const endDate = getElement("accommodation-checkout")?.value || "";
    const adults = readInteger("accommodation-adults", 1, 20);
    const children = readInteger("accommodation-children", 0, 20);
    const vehicle = vehiclePayload(costState);
    const drivingCost = costState.result && costState.result.driving_cost
      ? costState.result.driving_cost
      : null;
    return {
      selected,
      route: routeState.route || null,
      drivingCost,
      startDate,
      endDate,
      adults,
      children,
      vehicle,
    };
  }

  function inputKey(input) {
    return JSON.stringify({
      origin: input.selected.origin,
      destination: input.selected.destination,
      routeId: input.route && input.route.route_id,
      startDate: input.startDate,
      endDate: input.endDate,
      adults: input.adults,
      children: input.children,
      vehicle: input.vehicle,
    });
  }

  function buildPayload(input) {
    const payload = {
      origin: locationPayload(input.selected.origin),
      destination: locationPayload(input.selected.destination),
      start_date: input.startDate,
      end_date: input.endDate,
      adults: input.adults,
      children: input.children,
      vehicle: {
        fuel_type: input.vehicle.fuel_type,
        fuel_efficiency_km_per_l: input.vehicle.efficiency_km_per_l,
        vehicle_class: input.vehicle.vehicle_class,
        round_trip_mode: "directional",
      },
    };
    if (input.route) {
      payload.route = input.route;
    }
    if (input.drivingCost) {
      payload.driving_cost = input.drivingCost;
    }
    return payload;
  }

  function renderSource(card, candidate) {
    const sources = candidate && candidate.accommodation && Array.isArray(candidate.accommodation.sources)
      ? candidate.accommodation.sources
      : [];
    const source = sources.find((value) => value && typeof value.source_url === "string");
    if (!source) {
      return;
    }
    let url;
    try {
      url = new URL(source.source_url);
    } catch (error) {
      return;
    }
    const allowedHosts = ["booking.com", "www.booking.com", "english.visitkorea.or.kr"];
    if (url.protocol !== "https:" || !allowedHosts.includes(url.hostname) || url.username || url.password || url.hash) {
      return;
    }
    const line = document.createElement("p");
    line.className = "trip-candidate-card__source";
    line.textContent = `source: ${typeof source.source === "string" ? source.source : "unknown"} · `;
    const link = document.createElement("a");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "원문";
    line.appendChild(link);
    card.appendChild(line);
  }

  function renderCandidate(candidate) {
    const card = document.createElement("article");
    card.className = "trip-candidate-card";
    const heading = document.createElement("div");
    heading.className = "trip-candidate-card__heading";
    const name = document.createElement("h3");
    name.className = "trip-candidate-card__name";
    name.textContent = candidate.accommodation && candidate.accommodation.name
      ? candidate.accommodation.name
      : candidate.trip_type === "DAY_TRIP" ? "당일 여행" : "숙소 미정";
    const total = document.createElement("span");
    total.className = "trip-candidate-card__total";
    if (candidate.costs && Number.isFinite(candidate.costs.total_krw)) {
      total.textContent = candidate.costs.status === "ESTIMATED_COMPLETE"
        ? `예상 총비용 ${formatWon(candidate.costs.total_krw)}`
        : `총비용 ${formatWon(candidate.costs.total_krw)}`;
    } else {
      total.textContent = "총비용 확인 불가";
    }
    heading.append(name, total);
    card.appendChild(heading);

    const cost = document.createElement("div");
    cost.className = "trip-candidate-card__cost";
    const costList = document.createElement("dl");
    const components = [
      ["자동차 이동비", candidate.costs && candidate.costs.driving_krw],
      ["숙박비", candidate.costs && candidate.costs.accommodation_krw],
      ["확인된 비용", candidate.costs && candidate.costs.known_subtotal_krw],
    ];
    components.forEach(([label, amount]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const value = document.createElement("dd");
      value.textContent = label === "숙박비" && candidate.trip_type === "DAY_TRIP"
        ? "숙박 없음"
        : formatWon(amount);
      costList.append(term, value);
    });
    cost.appendChild(costList);
    card.appendChild(cost);

    const quality = document.createElement("p");
    quality.className = "trip-candidate-card__meta";
    const candidateQuality = candidate.quality || {};
    quality.textContent = [
      formatRating(candidateQuality.accommodation_rating, candidateQuality.accommodation_rating_confidence),
      `주변 맛집 ${Number(candidateQuality.nearby_restaurant_count) || 0}곳`,
      `주변 관광지 ${Number(candidateQuality.nearby_attraction_count) || 0}곳`,
    ].join(" · ");
    card.appendChild(quality);

    if (Array.isArray(candidate.costs && candidate.costs.missing_components) && candidate.costs.missing_components.length) {
      const missing = document.createElement("p");
      missing.className = "trip-candidate-card__warning";
      missing.textContent = `확인 불가: ${candidate.costs.missing_components.join(", ")}`;
      card.appendChild(missing);
    }
    renderSource(card, candidate);
    return card;
  }

  function render() {
    const button = getElement("trip-candidates-generate");
    const results = getElement("trip-candidates-results");
    const count = getElement("trip-candidates-count");
    if (!button || !results || !count) {
      return;
    }
    const input = currentInput();
    const ready = Boolean(
      input.selected.origin &&
      input.selected.destination &&
      input.startDate &&
      input.endDate &&
      input.endDate >= input.startDate &&
      input.adults !== null &&
      input.children !== null &&
      Number.isFinite(input.vehicle.efficiency_km_per_l) &&
      input.vehicle.efficiency_km_per_l >= 0.1
    );
    button.disabled = !ready || state.status === "loading";
    button.textContent = state.status === "loading" ? "여행 후보 조립 중…" : "여행 후보 조립";
    count.hidden = !state.response;
    count.textContent = state.response ? `${state.response.candidates.length}개 후보` : "";
    results.replaceChildren();
    if (!state.response) {
      return;
    }
    if (!state.response.candidates.length) {
      const empty = document.createElement("p");
      empty.className = "trip-candidates-empty";
      empty.textContent = "현재 조건으로 조립할 여행 후보가 없습니다.";
      results.appendChild(empty);
      return;
    }
    state.response.candidates.forEach((candidate) => results.appendChild(renderCandidate(candidate)));
  }

  function clear(message) {
    state.requestId += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    state.status = "idle";
    state.response = null;
    state.error = null;
    latestInputKey = null;
    if (message) {
      setStatus(message, "active");
    }
    render();
  }

  async function generate() {
    const input = currentInput();
    if (!input.selected.origin || !input.selected.destination) {
      setStatus("출발지와 목적지를 먼저 선택하세요.", "error");
      return false;
    }
    if (!input.startDate || !input.endDate || input.endDate < input.startDate) {
      setStatus("날짜를 올바르게 입력하세요.", "error");
      return false;
    }
    if (input.adults === null || input.children === null || !Number.isFinite(input.vehicle.efficiency_km_per_l)) {
      setStatus("인원과 차량 효율을 올바르게 입력하세요.", "error");
      return false;
    }
    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const requestKey = inputKey(input);
    latestInputKey = requestKey;
    activeController = new AbortController();
    state.status = "loading";
    state.response = null;
    state.error = null;
    setStatus("구성 요소를 확인하고 후보를 조립하는 중입니다…", "active");
    render();
    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildPayload(input)),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (requestId !== state.requestId || latestInputKey !== inputKey(currentInput())) {
        return false;
      }
      if (!response.ok || !payload || !Array.isArray(payload.candidates)) {
        throw new Error(payload && payload.error && payload.error.message ? payload.error.message : "여행 후보 조립에 실패했습니다.");
      }
      state.response = payload;
      state.status = payload.status === "partial" ? "partial" : "success";
      setStatus(
        payload.status === "partial" ? "일부 비용 또는 원본을 확인하지 못했습니다. 확인된 값만 표시합니다." : "여행 후보를 조립했습니다.",
        payload.status === "partial" ? "partial" : "success"
      );
      render();
      window.dispatchEvent(new CustomEvent("kto:trip-candidates", { detail: payload }));
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (requestId !== state.requestId || latestInputKey !== inputKey(currentInput())) {
        return false;
      }
      state.status = "error";
      state.error = error;
      setStatus(error && error.message ? error.message : "여행 후보 조립에 실패했습니다.", "error");
      render();
      return false;
    } finally {
      if (requestId === state.requestId) {
        activeController = null;
      }
    }
  }

  function invalidateOnInput(message) {
    if (state.response || state.status === "loading") {
      clear(message);
    } else {
      render();
    }
  }

  window.addEventListener("kto:selection-changed", () => invalidateOnInput("위치가 변경되어 여행 후보를 초기화했습니다."));
  window.addEventListener("kto:route-changed", () => invalidateOnInput("경로가 변경되어 여행 후보를 초기화했습니다."));
  window.addEventListener("kto:driving-cost-result", () => invalidateOnInput("이동비가 변경되어 여행 후보를 초기화했습니다."));
  window.addEventListener("kto:accommodation-results", () => invalidateOnInput("숙소 조건이 변경되어 여행 후보를 초기화했습니다."));
  window.addEventListener("kto:places-results", () => invalidateOnInput("주변 장소가 변경되어 여행 후보를 초기화했습니다."));

  document.addEventListener("DOMContentLoaded", () => {
    const button = getElement("trip-candidates-generate");
    if (button) {
      button.addEventListener("click", generate);
    }
    [
      "accommodation-checkin",
      "accommodation-checkout",
      "accommodation-adults",
      "accommodation-children",
      "fuel-type",
      "fuel-efficiency",
      "vehicle-class",
    ].forEach((id) => {
      const element = getElement(id);
      if (element) {
        element.addEventListener("input", () => invalidateOnInput("여행 조건이 변경되었습니다. 다시 조립하세요."));
        element.addEventListener("change", () => invalidateOnInput("여행 조건이 변경되었습니다. 다시 조립하세요."));
      }
    });
    render();
  });

  window.KoreaTripCandidates = Object.freeze({
    generate,
    clear: () => clear("여행 후보를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      response: state.response,
      error: state.error,
    }),
  });
})();
