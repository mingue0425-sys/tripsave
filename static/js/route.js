(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const routeState = {
    status: "idle",
    route: null,
    error: null,
    requestId: 0,
  };
  let mapReady = false;
  let selectionRevision = 0;
  let activeController = null;
  let latestSelection = { origin: null, destination: null };

  function getElement(id) {
    return document.getElementById(id);
  }

  function getSelection() {
    return window.KoreaTripSelection;
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

  function currentSelection() {
    const selection = getSelection();
    return selection
      ? {
          origin: selection.getOrigin(),
          destination: selection.getDestination(),
        }
      : { origin: null, destination: null };
  }

  function setStatus(message, statusType) {
    const element = getElement("route-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle("route-status--error", statusType === "error");
    element.classList.toggle("route-status--success", statusType === "success");
  }

  function setMetricVisibility(visible) {
    const metrics = getElement("route-metrics");
    if (metrics) {
      metrics.hidden = !visible;
    }
  }

  function render() {
    const selection = latestSelection;
    const button = getElement("route-calculate");
    const summary = getElement("route-summary");
    const distance = getElement("route-distance");
    const duration = getElement("route-duration");
    const format = window.KoreaTripRouteFormat;
    if (!button || !summary || !distance || !duration || !format) {
      return;
    }

    const readyToCalculate = Boolean(
      mapReady && selection.origin && selection.destination
    );
    button.disabled = !readyToCalculate || routeState.status === "loading";
    button.textContent =
      routeState.status === "loading" ? "경로 계산 중…" : "경로 계산";

    if (routeState.route) {
      summary.textContent = "현재 출발지와 목적지의 자동차 경로";
      distance.textContent = format.formatDistance(routeState.route.distance_m);
      duration.textContent = format.formatDuration(routeState.route.duration_s);
      setMetricVisibility(true);
      return;
    }
    setMetricVisibility(false);
    if (!selection.origin || !selection.destination) {
      summary.textContent = "출발지와 목적지를 모두 선택하세요.";
    } else if (!mapReady) {
      summary.textContent = "지도를 사용할 수 있을 때 경로를 계산할 수 있습니다.";
    } else if (routeState.status === "loading") {
      summary.textContent = "로컬 OSRM에서 자동차 경로를 계산하고 있습니다.";
    } else if (routeState.status === "error") {
      summary.textContent = "경로를 계산하지 못했습니다.";
    } else {
      summary.textContent = "자동차 경로를 계산할 준비가 되었습니다.";
    }
  }

  function clearRenderedRoute() {
    if (
      window.KoreaTripRouteLayer &&
      typeof window.KoreaTripRouteLayer.clearRoute === "function"
    ) {
      window.KoreaTripRouteLayer.clearRoute();
    }
  }

  function publishRouteChange(route) {
    window.dispatchEvent(
      new CustomEvent("kto:route-changed", {
        detail: { route: route || null },
      })
    );
  }

  function invalidateRoute(message) {
    routeState.requestId += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    routeState.status = "idle";
    routeState.route = null;
    routeState.error = null;
    clearRenderedRoute();
    publishRouteChange(null);
    render();
    if (message) {
      setStatus(message, "active");
    }
  }

  function errorMessage(payload) {
    if (payload && payload.error && typeof payload.error.message === "string") {
      return payload.error.message;
    }
    if (payload && typeof payload.detail === "string") {
      return payload.detail;
    }
    return "로컬 OSRM 경로 계산에 실패했습니다.";
  }

  function requestLocation(location) {
    return {
      lat: location.lat,
      lng: location.lng,
      label: location.label || null,
      source: location.source,
    };
  }

  async function calculateRoute() {
    const selection = currentSelection();
    if (!selection.origin || !selection.destination) {
      setStatus("출발지와 목적지를 먼저 선택하세요.", "error");
      return false;
    }
    if (!mapReady || !config.routeApiUrl) {
      setStatus("지도를 사용할 수 없어 경로를 계산하지 못했습니다.", "error");
      return false;
    }

    if (activeController) {
      activeController.abort();
    }
    const requestId = ++routeState.requestId;
    const revision = selectionRevision;
    const fingerprint = selectionFingerprint(selection);
    activeController = new AbortController();
    routeState.status = "loading";
    routeState.route = null;
    routeState.error = null;
    clearRenderedRoute();
    publishRouteChange(null);
    render();
    setStatus("로컬 OSRM에서 자동차 경로를 계산 중입니다…", "active");

    try {
      const response = await fetch(config.routeApiUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          origin: requestLocation(selection.origin),
          destination: requestLocation(selection.destination),
        }),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (
        requestId !== routeState.requestId ||
        revision !== selectionRevision ||
        fingerprint !== selectionFingerprint(currentSelection())
      ) {
        return false;
      }
      if (!response.ok || !payload || payload.status !== "ok" || !payload.route) {
        throw new Error(errorMessage(payload));
      }
      if (
        !window.KoreaTripRouteLayer ||
        !window.KoreaTripRouteLayer.setRouteGeometry(payload.route.geometry)
      ) {
        throw new Error("경로 geometry를 지도에 표시하지 못했습니다.");
      }
      routeState.route = payload.route;
      routeState.status = "success";
      routeState.error = null;
      publishRouteChange(payload.route);
      render();
      setStatus("자동차 경로를 표시했습니다.", "success");
      window.KoreaTripRouteLayer.fitRouteBounds();
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (
        requestId !== routeState.requestId ||
        revision !== selectionRevision
      ) {
        return false;
      }
      routeState.status = "error";
      routeState.route = null;
      routeState.error = error instanceof Error ? error.message : String(error);
      clearRenderedRoute();
      publishRouteChange(null);
      render();
      setStatus(routeState.error, "error");
      return false;
    } finally {
      if (requestId === routeState.requestId) {
        activeController = null;
        render();
      }
    }
  }

  function handleSelectionChanged(event) {
    const nextSelection = event && event.detail ? event.detail : currentSelection();
    const nextFingerprint = selectionFingerprint(nextSelection);
    const previousFingerprint = selectionFingerprint(latestSelection);
    latestSelection = {
      origin: nextSelection.origin || null,
      destination: nextSelection.destination || null,
    };
    selectionRevision += 1;
    if (nextFingerprint !== previousFingerprint) {
      if (routeState.route || routeState.status === "loading") {
        invalidateRoute("출발지 또는 목적지가 변경되어 경로를 다시 계산하세요.");
        return;
      }
    }
    render();
  }

  window.addEventListener("kto:selection-changed", handleSelectionChanged);
  window.addEventListener("kto:map-ready", () => {
    mapReady = true;
    render();
  });
  window.addEventListener("kto:map-error", () => {
    mapReady = false;
    invalidateRoute("지도를 사용할 수 없어 경로 계산을 종료했습니다.");
  });

  window.KoreaTripRoute = Object.freeze({
    calculateRoute,
    clearRoute: () => invalidateRoute("경로를 초기화했습니다."),
    getState: () => ({
      status: routeState.status,
      route: routeState.route,
      error: routeState.error,
    }),
  });

  document.addEventListener("DOMContentLoaded", () => {
    latestSelection = currentSelection();
    const button = getElement("route-calculate");
    if (button) {
      button.addEventListener("click", calculateRoute);
    }
    render();
  });
})();
