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
        detail: { aggregate: result, toll: result.outbound_toll || result.toll || null },
      })
    );
  }

  function renderLegValue(leg, key) {
    return leg && Number.isFinite(leg[key]) ? formatWon(leg[key]) : "확인 불가";
  }

  function formatDistance(value) {
    return Number.isFinite(value) && value > 0
      ? `${(value / 1000).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}km`
      : "확인 불가";
  }

  function renderTollValue(leg) {
    if (!leg || !Number.isFinite(leg.toll_krw)) {
      return "확인 불가";
    }
    if (leg.toll_status === "estimated" || leg.toll_mode === "estimated_doubled_outbound") {
      return `약 ${formatWon(leg.toll_krw)} (가는 길 공식 요금 기반 추정)`;
    }
    return formatWon(leg.toll_krw);
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
    const outbound = driving && driving.outbound;
    const returnLeg = driving && (driving.return || driving.return_leg);
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
      getElement("fuel-round-trip").textContent = fuel && fuel.complete && fuel.round_trip_complete
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

      getElement("cost-outbound-distance").textContent = formatDistance(outbound && outbound.distance_m);
      getElement("cost-outbound-fuel").textContent = renderLegValue(outbound, "fuel_cost_krw");
      getElement("cost-outbound-toll").textContent = renderTollValue(outbound);
      getElement("cost-outbound-total").textContent = renderLegValue(outbound, "total_krw");
      getElement("cost-return-distance").textContent = formatDistance(returnLeg && returnLeg.distance_m);
      getElement("cost-return-fuel").textContent = renderLegValue(returnLeg, "fuel_cost_krw");
      getElement("cost-return-toll").textContent = renderTollValue(returnLeg);
      getElement("cost-return-total").textContent = renderLegValue(returnLeg, "total_krw");
      getElement("cost-round-trip-distance").textContent = formatDistance(roundTrip && roundTrip.distance_m);
      getElement("cost-round-trip-fuel").textContent = renderLegValue(roundTrip, "fuel_cost_krw");
      getElement("cost-round-trip-toll").textContent = renderTollValue(roundTrip);
      getElement("cost-round-trip-total").textContent = renderLegValue(roundTrip, "total_krw");

      if (!fuel || !fuel.complete) {
        setStatus("fuel-status", "공식 웹 유가를 확인하지 못해 유류비를 계산하지 않았습니다.", "error");
      } else if (fuel.price && fuel.price.source_status === "stale") {
        setStatus("fuel-status", "최근 확인된 공식 웹 유가를 사용했습니다.", "active");
      } else {
        setStatus("fuel-status", "공식 웹 유가 확인 완료 · 입력한 실제 연비 기준", "success");
      }
      if (driving && driving.cost_complete) {
        const mode = driving.officially_verified
          ? "왕복 공식 확인"
          : driving.contains_estimate
            ? "추정 포함(공식 왕복 확인 전)"
            : "왕복 계산 완료";
        setStatus("driving-cost-status", `자동차 이동비 계산 완료 · ${mode}`, "success");
      } else {
        setStatus("driving-cost-status", "오는 길 정보가 확인되지 않아 왕복 합계를 완성하지 못했습니다.", "error");
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

  function recalculateLegFuel(leg, fuelVolume, fuelCost, fuelStatus) {
    const next = {
      ...leg,
      fuel_volume_l: fuelVolume,
      fuel_cost_krw: fuelCost,
      fuel_status: fuelStatus,
    };
    if (next.complete && Number.isFinite(next.toll_krw)) {
      next.total_krw = fuelCost + next.toll_krw;
      next.known_minimum_krw = null;
    } else {
      next.total_krw = null;
      const known = [fuelCost, next.toll_krw].filter(Number.isFinite);
      next.known_minimum_krw = known.length ? known.reduce((sum, value) => sum + value, 0) : null;
    }
    return next;
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
    const returnLeg = result.driving_cost && (result.driving_cost.return || result.driving_cost.return_leg);
    const returnDistanceKm = Number(result.fuel.return_distance_km)
      || Number(returnLeg && returnLeg.distance_m) / 1000;
    if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(distanceKm) || distanceKm <= 0) {
      clearResult("연비가 변경되어 이동비를 다시 계산하세요.");
      return;
    }

    const oneVolume = distanceKm / nextEfficiency;
    const oneFuel = roundKrw(oneVolume * price);
    const hasReturn = Number.isFinite(returnDistanceKm) && returnDistanceKm > 0;
    const returnVolume = hasReturn ? returnDistanceKm / nextEfficiency : null;
    const returnFuel = hasReturn ? roundKrw(returnVolume * price) : null;
    const roundVolume = hasReturn ? oneVolume + returnVolume : null;
    const roundFuel = hasReturn ? oneFuel + returnFuel : null;
    const fuelStatus = result.fuel.price && result.fuel.price.source_status === "fresh"
      ? "verified"
      : "estimated";
    const nextFuel = {
      ...result.fuel,
      fuel_efficiency_km_per_l: nextEfficiency,
      fuel_volume_l: oneVolume,
      fuel_cost_krw: oneFuel,
      one_way_krw: oneFuel,
      return_distance_km: hasReturn ? returnDistanceKm : null,
      return_fuel_volume_l: returnVolume,
      return_fuel_cost_krw: returnFuel,
      round_trip_complete: hasReturn,
      round_trip_distance_km: hasReturn ? distanceKm + returnDistanceKm : null,
      round_trip_fuel_volume_l: roundVolume,
      round_trip_fuel_cost_krw: roundFuel,
      round_trip_krw: roundFuel,
      round_trip_distance_mode: hasReturn ? result.fuel.round_trip_distance_mode : "unknown",
    };
    const outbound = recalculateLegFuel(
      result.driving_cost.outbound,
      oneVolume,
      oneFuel,
      fuelStatus,
    );
    const nextReturn = hasReturn
      ? recalculateLegFuel(returnLeg, returnVolume, returnFuel, fuelStatus)
      : {
          ...returnLeg,
          distance_m: null,
          fuel_volume_l: null,
          fuel_cost_krw: null,
          total_krw: null,
          known_minimum_krw: Number.isFinite(returnLeg && returnLeg.toll_krw)
            ? returnLeg.toll_krw
            : null,
          status: "unknown",
          fuel_status: "unknown",
        };
    const nextRoundTrip = hasReturn
      ? recalculateLegFuel(
          result.driving_cost.round_trip,
          roundVolume,
          roundFuel,
          fuelStatus,
        )
      : {
          ...result.driving_cost.round_trip,
          distance_m: null,
          fuel_volume_l: null,
          fuel_cost_krw: null,
          total_krw: null,
          known_minimum_krw: null,
          status: "unknown",
          fuel_status: "unknown",
        };
    if (hasReturn) {
      nextRoundTrip.distance_m = (distanceKm + returnDistanceKm) * 1000;
    }
    const costComplete = outbound.complete && nextReturn.complete && nextRoundTrip.complete;
    state.result = {
      ...result,
      fuel: nextFuel,
      driving_cost: {
        ...result.driving_cost,
        outbound,
        return: nextReturn,
        round_trip: nextRoundTrip,
        complete: costComplete,
        cost_complete: costComplete,
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
