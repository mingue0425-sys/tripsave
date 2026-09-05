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

  const vehicleLabels = Object.freeze({
    class_1: "1종 승용/소형차",
    compact: "1종 경차",
    class_2: "2종 중형차",
    class_3: "3종 대형차",
    class_4: "4종 대형화물차",
    class_5: "5종 특수화물차",
  });

  function getElement(id) {
    return document.getElementById(id);
  }

  function vehicleClass() {
    const select = getElement("vehicle-class");
    return select && vehicleLabels[select.value] ? select.value : "class_1";
  }

  function setStatus(message, type) {
    const element = getElement("toll-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle("toll-status--error", type === "error");
    element.classList.toggle("toll-status--success", type === "success");
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
    if (window.KoreaTripTollMarkers) {
      window.KoreaTripTollMarkers.clearTollMarkers();
    }
    if (message) {
      setStatus(message, "active");
    }
    render();
  }

  function formatWon(value) {
    return Number.isFinite(value) && value >= 0
      ? `${Math.round(value).toLocaleString("ko-KR")}원`
      : "확인 불가";
  }

  function render() {
    const button = getElement("toll-calculate");
    const summary = getElement("toll-summary");
    const metrics = getElement("toll-metrics");
    const total = getElement("toll-total");
    const vehicle = getElement("toll-vehicle");
    const count = getElement("toll-gates-count");
    if (!button || !summary || !metrics || !total || !vehicle || !count) {
      return;
    }

    const ready = Boolean(state.route && latestSelection.origin && latestSelection.destination);
    button.disabled = !ready || state.status === "loading";
    button.textContent = state.status === "loading" ? "통행료 확인 중…" : "통행료 확인";
    vehicle.textContent = vehicleLabels[vehicleClass()];

    const gates = state.result && Array.isArray(state.result.detected_toll_gates)
      ? state.result.detected_toll_gates
      : [];
    count.hidden = gates.length === 0;
    count.textContent = gates.length ? `TG 후보 ${gates.length}개` : "";

    if (state.result) {
      metrics.hidden = false;
      if (state.result.complete) {
        total.textContent = formatWon(state.result.total_toll_krw);
        const sourceMessage = state.result.source_status === "stale"
          ? "공식 조회 캐시(마지막 확인일 기준)"
          : state.result.source_status === "not_applicable"
            ? "확인된 유료도로 없음"
            : "공식 웹 조회 확인";
        summary.textContent = sourceMessage;
      } else {
        total.textContent = state.result.known_toll_krw == null
          ? "확인 불가"
          : `최소 ${formatWon(state.result.known_toll_krw)}`;
        summary.textContent = "일부 유료구간 또는 공식 요금을 확인하지 못했습니다.";
      }
      return;
    }

    metrics.hidden = true;
    if (!state.route) {
      summary.textContent = "경로 계산 후 공식 요금을 확인할 수 있습니다.";
    } else if (state.status === "loading") {
      summary.textContent = "공식 한국도로공사 페이지에서 요금을 확인 중입니다.";
    } else if (state.status === "error") {
      summary.textContent = "통행료를 확인하지 못했습니다.";
    } else {
      summary.textContent = "자동차 경로의 유료도로 요금을 확인할 준비가 되었습니다.";
    }
  }

  function locationPayload(location) {
    return {
      lat: location.lat,
      lng: location.lng,
      label: location.label || null,
      source: location.source || "map",
    };
  }

  function locationFingerprint(location) {
    if (!location) {
      return "none";
    }
    return `${location.lat}|${location.lng}|${location.label || ""}|${location.source || ""}`;
  }

  function selectionFingerprint(selection) {
    return `${locationFingerprint(selection.origin)}::${locationFingerprint(
      selection.destination
    )}`;
  }

  function errorMessage(payload) {
    if (payload && payload.error && typeof payload.error.message === "string") {
      return payload.error.message;
    }
    return "통행료 확인에 실패했습니다. 알 수 없는 요금을 0원으로 처리하지 않았습니다.";
  }

  async function calculateToll() {
    if (!state.route || !latestSelection.origin || !latestSelection.destination) {
      setStatus("먼저 출발지와 목적지의 경로를 계산하세요.", "error");
      return false;
    }
    if (!config.tollApiUrl) {
      setStatus("통행료 API 설정이 없습니다.", "error");
      return false;
    }
    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const routeAtRequest = state.route;
    const selectionFingerprintAtRequest = selectionFingerprint(latestSelection);
    const routeId = routeAtRequest.route_id || null;
    activeController = new AbortController();
    state.status = "loading";
    state.result = null;
    state.error = null;
    if (window.KoreaTripTollMarkers) {
      window.KoreaTripTollMarkers.clearTollMarkers();
    }
    render();
    setStatus("공식 웹페이지에서 통행료를 확인 중입니다…", "active");
    try {
      const response = await fetch(config.tollApiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          route_id: routeId,
          origin: locationPayload(latestSelection.origin),
          destination: locationPayload(latestSelection.destination),
          route: routeAtRequest,
          vehicle_class: vehicleClass(),
        }),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (
        requestId !== state.requestId ||
        state.route !== routeAtRequest ||
        selectionFingerprint(latestSelection) !== selectionFingerprintAtRequest
      ) {
        return false;
      }
      if (!response.ok || !payload || !payload.toll) {
        throw new Error(errorMessage(payload));
      }
      state.result = payload.toll;
      state.status = payload.status === "partial" ? "partial" : "success";
      render();
      if (window.KoreaTripTollMarkers) {
        window.KoreaTripTollMarkers.setTollGates(state.result.detected_toll_gates || []);
      }
      if (state.status === "partial") {
        setStatus("일부 통행료를 확인하지 못했습니다. 확인 불가 구간을 0원으로 계산하지 않았습니다.", "error");
      } else if (state.result.source_status === "stale") {
        setStatus("공식 조회에 실패하여 마지막 확인된 캐시를 표시합니다.", "active");
      } else {
        setStatus("공식 웹 조회 결과를 반영했습니다.", "success");
      }
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (
        requestId !== state.requestId ||
        state.route !== routeAtRequest ||
        selectionFingerprint(latestSelection) !== selectionFingerprintAtRequest
      ) {
        return false;
      }
      state.status = "error";
      state.error = error instanceof Error ? error.message : String(error);
      state.result = null;
      if (window.KoreaTripTollMarkers) {
        window.KoreaTripTollMarkers.clearTollMarkers();
      }
      render();
      setStatus(state.error, "error");
      return false;
    } finally {
      if (requestId === state.requestId) {
        activeController = null;
        render();
      }
    }
  }

  function handleRouteChanged(event) {
    state.route = event && event.detail ? event.detail.route : null;
    clearResult(state.route ? "새 경로의 통행료를 다시 확인하세요." : "경로가 없어 통행료 결과를 초기화했습니다.");
    // Store the current route after clearResult; this reference is used by the
    // request race guard and is never read from the DOM.
    state.route = event && event.detail ? event.detail.route : null;
    render();
  }

  function handleSelectionChanged(event) {
    const nextSelection = event && event.detail
      ? { origin: event.detail.origin || null, destination: event.detail.destination || null }
      : { origin: null, destination: null };
    const changed = selectionFingerprint(nextSelection) !== selectionFingerprint(latestSelection);
    latestSelection = nextSelection;
    if (changed && (state.result || state.status === "loading")) {
      clearResult("출발지 또는 목적지가 변경되어 통행료를 다시 확인하세요.");
      return;
    }
    if (!latestSelection.origin || !latestSelection.destination) {
      clearResult("출발지와 목적지가 모두 필요합니다.");
    } else {
      render();
    }
  }

  function handleVehicleChanged() {
    if (state.result || state.status === "loading") {
      clearResult("차종이 변경되어 통행료를 다시 확인하세요.");
    } else {
      render();
    }
  }

  window.addEventListener("kto:route-changed", handleRouteChanged);
  window.addEventListener("kto:selection-changed", handleSelectionChanged);

  document.addEventListener("DOMContentLoaded", () => {
    const button = getElement("toll-calculate");
    const select = getElement("vehicle-class");
    if (button) {
      button.addEventListener("click", calculateToll);
    }
    if (select) {
      select.addEventListener("change", handleVehicleChanged);
    }
    const selection = window.KoreaTripSelection;
    if (selection) {
      latestSelection = {
        origin: selection.getOrigin(),
        destination: selection.getDestination(),
      };
    }
    const route = window.KoreaTripRoute;
    if (route && typeof route.getState === "function") {
      state.route = route.getState().route || null;
    }
    render();
  });

  window.KoreaTripToll = Object.freeze({
    calculateToll,
    clear: () => clearResult("통행료 결과를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      result: state.result,
      error: state.error,
      route: state.route,
      vehicleClass: vehicleClass(),
    }),
  });
})();
