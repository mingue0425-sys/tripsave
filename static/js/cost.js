(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const state = {
    status: "idle",
    result: null,
    error: null,
    requestId: 0,
    route: null,
  };
  let activeController = null;
  let latestSelection = { origin: null, destination: null };

  const fuelLabels = Object.freeze({
    gasoline: "휘발유",
    diesel: "경유",
    lpg: "LPG",
  });

  function getElement(id) {
    return document.getElementById(id);
  }

  function fuelType() {
    const select = getElement("fuel-type");
    return select && fuelLabels[select.value] ? select.value : "gasoline";
  }

  function vehicleClass() {
    const select = getElement("vehicle-class");
    return select ? select.value : "class_1";
  }

  function efficiency() {
    const input = getElement("fuel-efficiency");
    if (!input || input.value.trim() === "") {
      return null;
    }
    const value = Number(input.value);
    return Number.isFinite(value) && value >= 0.1 && value <= 100 ? value : null;
  }

  function setStatus(id, message, type) {
    const element = getElement(id);
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle(`${id.replace("-status", "")}-status--error`, type === "error");
    element.classList.toggle(`${id.replace("-status", "")}-status--success`, type === "success");
  }

  function formatWon(value) {
    return Number.isFinite(value) && value >= 0
      ? `${Math.round(value).toLocaleString("ko-KR")}원`
      : "확인 불가";
  }

  function formatPrice(value) {
    return Number.isFinite(value) && value > 0
      ? `${value.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}원/L`
      : "확인 불가";
  }

  function formatLitres(value) {
    return Number.isFinite(value) && value >= 0
      ? `${value.toLocaleString("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}L`
      : "확인 불가";
  }

  function formatFetchedAt(value) {
    if (!value) {
      return "확인 시각 없음";
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "확인 시각 없음"
      : date.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  }

  function locationFingerprint(location) {
    if (!location) {
      return "none";
    }
    return `${location.lat}|${location.lng}|${location.label || ""}|${location.source || ""}`;
  }

  function selectionFingerprint(selection) {
    return `${locationFingerprint(selection.origin)}::${locationFingerprint(selection.destination)}`;
  }

  function currentSelection() {
    const selection = window.KoreaTripSelection;
    return selection
      ? { origin: selection.getOrigin(), destination: selection.getDestination() }
      : { origin: null, destination: null };
  }

  function locationPayload(location) {
    return {
      lat: location.lat,
      lng: location.lng,
      label: location.label || null,
      source: location.source || "map",
    };
  }

  function clearResult(message) {
    state.requestId += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    state.status = "idle";
    state.result = null;
    state.error = null;
    if (message) {
      setStatus("fuel-status", message, "active");
      setStatus("driving-cost-status", message, "active");
    }
    render();
  }

  function dispatchAggregateResult(result) {
    window.dispatchEvent(
      new CustomEvent("kto:driving-cost-result", {
        detail: { aggregate: result, toll: result.toll || null },
      })
    );
  }

  function renderLegValue(leg, key) {
    return leg && Number.isFinite(leg[key]) ? formatWon(leg[key]) : "확인 불가";
  }

  function render() {
    const button = getElement("driving-cost-calculate");
    const fuelMetrics = getElement("fuel-metrics");
    const costMetrics = getElement("driving-cost-metrics");
    const priceSource = getElement("fuel-price-source");
    const ready = Boolean(state.route && latestSelection.origin && latestSelection.destination && efficiency() !== null);
    if (!button || !fuelMetrics || !costMetrics) {
      return;
    }
    button.disabled = !ready || state.status === "loading";
    button.textContent = state.status === "loading" ? "통행료·유가 확인 중…" : "편도·왕복 이동비 계산";

    const result = state.result;
    const fuel = result && result.fuel;
    const driving = result && result.driving_cost;
    const oneWay = driving && driving.one_way;
    const roundTrip = driving && driving.round_trip;
    if (result) {
      fuelMetrics.hidden = false;
      costMetrics.hidden = false;
      const price = fuel && fuel.price;
      getElement("fuel-price").textContent = price && fuel.complete
        ? `${formatPrice(fuel.price_krw_per_l)}${price.source_status === "stale" ? " (최근 확인)" : ""}`
        : "확인 불가";
      getElement("fuel-volume").textContent = fuel && fuel.complete
        ? formatLitres(fuel.fuel_volume_l)
        : "확인 불가";
      getElement("fuel-one-way").textContent = fuel && fuel.complete
        ? formatWon(fuel.one_way_krw)
        : "확인 불가";
      getElement("fuel-round-trip").textContent = fuel && fuel.complete
        ? formatWon(fuel.round_trip_krw)
        : "확인 불가";
      if (priceSource) {
        if (fuel && fuel.complete && price) {
          const sourceLabel = price.source_status === "stale"
            ? "최근 확인 가격 사용"
            : "공식 웹 확인";
          priceSource.hidden = false;
          priceSource.textContent = `${sourceLabel} · ${formatFetchedAt(price.fetched_at)}`;
        } else {
          priceSource.hidden = true;
          priceSource.textContent = "";
        }
      }

      getElement("cost-one-way-fuel").textContent = renderLegValue(oneWay, "fuel_krw");
      getElement("cost-one-way-toll").textContent = renderLegValue(oneWay, "toll_krw");
      getElement("cost-one-way-total").textContent = renderLegValue(oneWay, "total_krw");
      getElement("cost-round-trip-fuel").textContent = renderLegValue(roundTrip, "fuel_krw");
      getElement("cost-round-trip-toll").textContent = renderLegValue(roundTrip, "toll_krw");
      getElement("cost-round-trip-total").textContent = renderLegValue(roundTrip, "total_krw");

      if (!fuel || !fuel.complete) {
        setStatus("fuel-status", "공식 웹 유가를 확인하지 못해 유류비를 계산하지 않았습니다.", "error");
      } else if (fuel.price && fuel.price.source_status === "stale") {
        setStatus("fuel-status", "최근 확인된 공식 웹 유가를 사용했습니다.", "active");
      } else {
        setStatus("fuel-status", "공식 웹 유가 확인 완료 · 입력한 실제 연비 기준", "success");
      }
      if (driving && driving.complete) {
        const mode = driving.round_trip_toll_mode === "directional_official"
          ? "왕복 방향별 공식 통행료"
          : "편도 기준 왕복 환산";
        setStatus("driving-cost-status", `자동차 이동비 계산 완료 · ${mode}`, "success");
      } else {
        setStatus("driving-cost-status", "유류비 또는 통행료를 확인하지 못해 총 이동비를 계산하지 않았습니다.", "error");
      }
      return;
    }

    fuelMetrics.hidden = true;
    costMetrics.hidden = true;
    if (priceSource) {
      priceSource.hidden = true;
      priceSource.textContent = "";
    }
    if (!state.route) {
      setStatus("fuel-status", "경로 계산 후 연료 종류와 실제 연비를 입력하세요.", "active");
      setStatus("driving-cost-status", "통행료와 유가를 확인하면 이동비를 계산합니다.", "active");
    } else if (state.status === "loading") {
      setStatus("fuel-status", "공식 웹 유가를 확인 중입니다…", "active");
      setStatus("driving-cost-status", "통행료와 유가를 확인해 이동비를 계산 중입니다…", "active");
    } else if (efficiency() === null) {
      setStatus("fuel-status", "실제 연비를 0.1~100 km/L 범위로 입력하세요.", "error");
      setStatus("driving-cost-status", "실제 연비 입력 후 이동비를 계산할 수 있습니다.", "active");
    } else {
      setStatus("fuel-status", "계산 버튼을 누르면 공식 웹 유가를 확인합니다.", "active");
      setStatus("driving-cost-status", "통행료와 유가를 확인하면 이동비를 계산합니다.", "active");
    }
  }

  function roundKrw(value) {
    return Math.floor(value + 0.5);
  }

  function recalculateEfficiency() {
    const result = state.result;
    const nextEfficiency = efficiency();
    if (!result || !result.fuel || !result.fuel.complete || nextEfficiency === null) {
      render();
      return;
    }
    const price = Number(result.fuel.price_krw_per_l);
    const distanceKm = Number(state.route && state.route.distance_m) / 1000;
    const roundDistanceKm = Number(result.fuel.round_trip_distance_km) || distanceKm * 2;
    if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(distanceKm) || distanceKm <= 0) {
      clearResult("연비가 변경되어 이동비를 다시 계산하세요.");
      return;
    }
    const oneVolume = distanceKm / nextEfficiency;
    const roundVolume = roundDistanceKm / nextEfficiency;
    const oneFuel = roundKrw(oneVolume * price);
    const roundFuel = roundKrw(roundVolume * price);
    const nextFuel = {
      ...result.fuel,
      fuel_efficiency_km_per_l: nextEfficiency,
      fuel_volume_l: oneVolume,
      fuel_cost_krw: oneFuel,
      one_way_krw: oneFuel,
      round_trip_fuel_volume_l: roundVolume,
      round_trip_fuel_cost_krw: roundFuel,
      round_trip_krw: roundFuel,
    };
    const nextOneWay = { ...result.driving_cost.one_way, fuel_krw: oneFuel };
    const nextRoundTrip = { ...result.driving_cost.round_trip, fuel_krw: roundFuel };
    if (Number.isFinite(nextOneWay.toll_krw)) {
      nextOneWay.total_krw = nextOneWay.complete ? oneFuel + nextOneWay.toll_krw : null;
      nextOneWay.known_minimum_krw = nextOneWay.complete ? null : oneFuel + nextOneWay.toll_krw;
    } else {
      nextOneWay.total_krw = null;
      nextOneWay.known_minimum_krw = nextOneWay.complete ? null : oneFuel;
    }
    if (Number.isFinite(nextRoundTrip.toll_krw)) {
      nextRoundTrip.total_krw = nextRoundTrip.complete ? roundFuel + nextRoundTrip.toll_krw : null;
      nextRoundTrip.known_minimum_krw = nextRoundTrip.complete ? null : roundFuel + nextRoundTrip.toll_krw;
    } else {
      nextRoundTrip.total_krw = null;
      nextRoundTrip.known_minimum_krw = nextRoundTrip.complete ? null : roundFuel;
    }
    state.result = {
      ...result,
      fuel: nextFuel,
      driving_cost: {
        ...result.driving_cost,
        one_way: nextOneWay,
        round_trip: nextRoundTrip,
        complete: nextOneWay.complete && nextRoundTrip.complete,
      },
    };
    render();
  }

  async function calculateCost() {
    if (!state.route || !latestSelection.origin || !latestSelection.destination) {
      setStatus("driving-cost-status", "먼저 출발지와 목적지의 경로를 계산하세요.", "error");
      return false;
    }
    const nextEfficiency = efficiency();
    if (nextEfficiency === null) {
      setStatus("fuel-status", "실제 연비를 0.1~100 km/L 범위로 입력하세요.", "error");
      return false;
    }
    if (!config.drivingCostApiUrl) {
      setStatus("driving-cost-status", "이동비 API 설정이 없습니다.", "error");
      return false;
    }
    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const routeAtRequest = state.route;
    const selectionAtRequest = selectionFingerprint(latestSelection);
    activeController = new AbortController();
    state.status = "loading";
    state.result = null;
    state.error = null;
    render();
    try {
      const response = await fetch(config.drivingCostApiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          route_id: routeAtRequest.route_id || null,
          origin: locationPayload(latestSelection.origin),
          destination: locationPayload(latestSelection.destination),
          route: routeAtRequest,
          fuel_type: fuelType(),
          fuel_efficiency_km_per_l: nextEfficiency,
          vehicle_class: vehicleClass(),
          round_trip_mode: "directional",
        }),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (
        requestId !== state.requestId ||
        state.route !== routeAtRequest ||
        selectionFingerprint(latestSelection) !== selectionAtRequest
      ) {
        return false;
      }
      if (!response.ok || !payload || !payload.driving_cost || !payload.fuel || !payload.toll) {
        throw new Error(
          payload && payload.error && payload.error.message
            ? payload.error.message
            : "이동비 계산에 실패했습니다. 확인되지 않은 비용을 0원으로 처리하지 않았습니다."
        );
      }
      state.result = payload;
      state.status = payload.status === "partial" ? "partial" : "success";
      render();
      dispatchAggregateResult(payload);
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (
        requestId !== state.requestId ||
        state.route !== routeAtRequest ||
        selectionFingerprint(latestSelection) !== selectionAtRequest
      ) {
        return false;
      }
      state.status = "error";
      state.error = error instanceof Error ? error.message : String(error);
      state.result = null;
      render();
      setStatus("driving-cost-status", state.error, "error");
      return false;
    } finally {
      if (requestId === state.requestId) {
        activeController = null;
        render();
      }
    }
  }

  function handleRouteChanged(event) {
    state.route = event && event.detail ? event.detail.route || null : null;
    clearResult(state.route ? "새 경로의 이동비를 다시 계산하세요." : "경로가 없어 이동비 결과를 초기화했습니다.");
    state.route = event && event.detail ? event.detail.route || null : null;
    render();
  }

  function handleSelectionChanged(event) {
    const nextSelection = event && event.detail
      ? { origin: event.detail.origin || null, destination: event.detail.destination || null }
      : { origin: null, destination: null };
    const changed = selectionFingerprint(nextSelection) !== selectionFingerprint(latestSelection);
    latestSelection = nextSelection;
    if (changed && (state.result || state.status === "loading")) {
      clearResult("출발지 또는 목적지가 변경되어 이동비를 다시 계산하세요.");
      return;
    }
    render();
  }

  function handleFuelTypeChanged() {
    if (state.result || state.status === "loading") {
      clearResult("연료 종류가 변경되어 유가와 이동비를 다시 확인하세요.");
    } else {
      render();
    }
  }

  function handleVehicleChanged() {
    if (state.result || state.status === "loading") {
      clearResult("차종이 변경되어 통행료와 이동비를 다시 계산하세요.");
    } else {
      render();
    }
  }

  function handleEfficiencyChanged() {
    if (state.result && state.result.fuel && state.result.fuel.complete) {
      recalculateEfficiency();
      return;
    }
    render();
  }

  window.addEventListener("kto:route-changed", handleRouteChanged);
  window.addEventListener("kto:selection-changed", handleSelectionChanged);

  document.addEventListener("DOMContentLoaded", () => {
    latestSelection = currentSelection();
    const button = getElement("driving-cost-calculate");
    const fuelSelect = getElement("fuel-type");
    const efficiencyInput = getElement("fuel-efficiency");
    const vehicleSelect = getElement("vehicle-class");
    if (button) {
      button.addEventListener("click", calculateCost);
    }
    if (fuelSelect) {
      fuelSelect.addEventListener("change", handleFuelTypeChanged);
    }
    if (efficiencyInput) {
      efficiencyInput.addEventListener("input", render);
      efficiencyInput.addEventListener("change", handleEfficiencyChanged);
    }
    if (vehicleSelect) {
      vehicleSelect.addEventListener("change", handleVehicleChanged);
    }
    const route = window.KoreaTripRoute;
    if (route && typeof route.getState === "function") {
      state.route = route.getState().route || null;
    }
    render();
  });

  window.KoreaTripCost = Object.freeze({
    calculateCost,
    clear: () => clearResult("이동비 결과를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      result: state.result,
      error: state.error,
      route: state.route,
      fuelType: fuelType(),
      fuelEfficiency: efficiency(),
    }),
  });
})();
