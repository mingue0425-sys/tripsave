/* global maplibregl */

(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.poiApiUrl || "/api/poi/search";
  const CATEGORIES = Object.freeze([
    "hospital", "emergency", "convenience_store", "port", "passenger_terminal",
    "fuel_station", "pharmacy", "parking", "ev_charger",
  ]);
  const LABELS = Object.freeze({
    hospital: "병원", emergency: "응급실", convenience_store: "편의점",
    port: "항구", passenger_terminal: "여객터미널", fuel_station: "주유소",
    pharmacy: "약국", parking: "주차장", ev_charger: "EV 충전소",
  });
  const LETTERS = Object.freeze({
    hospital: "H", emergency: "E", convenience_store: "C", port: "P",
    passenger_terminal: "PT", fuel_station: "F", pharmacy: "Rx", parking: "P", ev_charger: "EV",
  });
  const state = {
    status: "idle",
    resultsByCategory: Object.fromEntries(CATEGORIES.map((category) => [category, []])),
    statuses: {},
    response: null,
    error: null,
    requestId: 0,
    visible: Object.fromEntries(CATEGORIES.map((category) => [category, true])),
  };
  let activeController = null;
  let latestDestination = null;
  let latestRoute = null;
  let mapInstance = null;
  const markersByCategory = Object.fromEntries(CATEGORIES.map((category) => [category, []]));

  function element(id) { return document.getElementById(id); }
  function currentDestination() {
    const selection = window.KoreaTripSelection;
    return selection && typeof selection.getDestination === "function" ? selection.getDestination() : null;
  }
  function fingerprint(value) {
    if (!value) return "none";
    return JSON.stringify(value);
  }
  function selectedCategories() {
    return CATEGORIES.filter((category) => {
      const checkbox = element(`poi-category-${category}`);
      return checkbox && checkbox.checked;
    });
  }
  function validCoordinates(item) {
    return item && Number.isFinite(item.lat) && Number.isFinite(item.lng)
      && item.lat >= -90 && item.lat <= 90 && item.lng >= -180 && item.lng <= 180;
  }
  function setStatus(message, type) {
    const target = element("poi-status");
    if (!target) return;
    target.textContent = message;
    target.classList.toggle("poi-status--error", type === "error");
    target.classList.toggle("poi-status--success", type === "success");
    target.classList.toggle("poi-status--partial", type === "partial");
  }
  function formatDistance(value, label) {
    return Number.isFinite(value) ? `${label} ${(value / 1000).toFixed(1)}km` : null;
  }
  function statusLabel(status) {
    if (status === "unavailable") return "확인 불가";
    if (status === "empty") return "0개";
    if (status === "partial") return "일부 확인";
    return status === "ok" ? "확인됨" : "확인 전";
  }
  function createMarkerElement(category, item) {
    const node = document.createElement("div");
    node.className = `kto-poi-pin kto-poi-pin--${category}`;
    node.textContent = LETTERS[category] || "POI";
    node.setAttribute("role", "img");
    node.setAttribute("aria-label", `${LABELS[category] || "시설"}: ${item.name || "이름 정보 없음"}`);
    return node;
  }
  function popupText(item) {
    const lines = [item.name || "이름 정보 없음", LABELS[item.category] || item.category || "시설"];
    if (typeof item.address === "string" && item.address) lines.push(item.address);
    const destinationDistance = formatDistance(item.distance_to_destination_m, "목적지 직선거리");
    const routeDistance = formatDistance(item.distance_to_route_m, "경로 corridor 거리");
    if (destinationDistance) lines.push(destinationDistance);
    if (routeDistance) lines.push(routeDistance);
    return lines.join("\n");
  }
  function markerLimit() {
    if (!mapInstance || typeof mapInstance.getZoom !== "function") return 50;
    const zoom = mapInstance.getZoom();
    return zoom < 10 ? 3 : zoom < 13 ? 15 : 50;
  }
  function clearMarkers(category) {
    markersByCategory[category].forEach((marker) => marker.remove());
    markersByCategory[category] = [];
  }
  function renderCategory(category) {
    clearMarkers(category);
    if (!mapInstance || !window.maplibregl || !state.visible[category]) return;
    const items = state.resultsByCategory[category]
      .filter(validCoordinates)
      .slice()
      .sort((first, second) => {
        const firstDistance = Math.min(...[first.distance_to_destination_m, first.distance_to_route_m].filter(Number.isFinite));
        const secondDistance = Math.min(...[second.distance_to_destination_m, second.distance_to_route_m].filter(Number.isFinite));
        return (firstDistance - secondDistance) || String(first.name || "").localeCompare(String(second.name || ""), "ko") || String(first.id || "").localeCompare(String(second.id || ""));
      })
      .slice(0, markerLimit());
    items.forEach((item) => {
      const marker = new maplibregl.Marker({ anchor: "bottom", element: createMarkerElement(category, item) })
        .setLngLat([item.lng, item.lat]).addTo(mapInstance);
      if (typeof maplibregl.Popup === "function") marker.setPopup(new maplibregl.Popup({ offset: 20 }).setText(popupText(item)));
      markersByCategory[category].push(marker);
    });
  }
  function renderMarkers() { CATEGORIES.forEach(renderCategory); }
  function clearAllMarkers() { CATEGORIES.forEach(clearMarkers); }
  function renderResults() {
    const target = element("poi-results");
    if (!target) return;
    target.replaceChildren();
    if (!state.response) return;
    CATEGORIES.forEach((category) => {
      if (!selectedCategories().includes(category)) return;
      const status = state.statuses[category];
      const heading = document.createElement("p");
      heading.className = "poi-result__meta";
      heading.textContent = `${LABELS[category]} · ${statusLabel(status)}`;
      target.appendChild(heading);
      state.resultsByCategory[category].forEach((item) => {
        const card = document.createElement("article");
        card.className = "poi-result";
        const title = document.createElement("h3");
        title.className = "poi-result__name";
        title.textContent = typeof item.name === "string" ? item.name : "이름 정보 없음";
        card.appendChild(title);
        const meta = document.createElement("p");
        meta.className = "poi-result__meta";
        const values = [LABELS[category]];
        const destinationDistance = formatDistance(item.distance_to_destination_m, "목적지");
        if (destinationDistance) values.push(destinationDistance);
        const routeDistance = formatDistance(item.distance_to_route_m, "경로");
        if (routeDistance) values.push(routeDistance);
        meta.textContent = values.join(" · ");
        card.appendChild(meta);
        if (item.address) {
          const address = document.createElement("p");
          address.className = "poi-result__meta";
          address.textContent = item.address;
          card.appendChild(address);
        }
        target.appendChild(card);
      });
    });
    if (!target.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "poi-empty";
      empty.textContent = "선택한 범위에서 시설을 찾지 못했습니다.";
      target.appendChild(empty);
    }
  }
  function render() {
    const button = element("poi-search");
    const summary = element("poi-summary");
    if (!button || !summary) return;
    button.disabled = !latestDestination || !selectedCategories().length || state.status === "loading";
    button.textContent = state.status === "loading" ? "주변 시설 확인 중…" : "주변 시설 검색";
    summary.textContent = latestDestination
      ? `${latestDestination.name || latestDestination.label || "선택한 목적지"} 주변 시설과 현재 경로 주변 시설을 확인합니다.`
      : "목적지를 선택하면 주변 시설을 지도에서 확인할 수 있습니다.";
    renderResults();
  }
  function clear(message) {
    state.requestId += 1;
    if (activeController) activeController.abort();
    activeController = null;
    state.status = "idle";
    state.response = null;
    state.error = null;
    state.statuses = {};
    CATEGORIES.forEach((category) => { state.resultsByCategory[category] = []; });
    clearAllMarkers();
    if (message) setStatus(message, "active");
    render();
  }
  function buildRequest() {
    const destination = latestDestination || currentDestination();
    const categories = selectedCategories();
    if (!destination || !categories.length) return null;
    return {
      destination: { lat: destination.lat, lng: destination.lng, label: destination.name || destination.label || "destination", source: "selection" },
      route: latestRoute || null,
      categories,
      destination_radius_m: 3000,
      route_corridor_m: 1000,
      limit_per_category: 50,
    };
  }
  function isCurrent(requestId, requestKey) {
    return requestId === state.requestId && requestKey === fingerprint({ destination: latestDestination || currentDestination(), route: latestRoute, categories: selectedCategories() });
  }
  async function search() {
    const request = buildRequest();
    if (!request) { setStatus("목적지와 시설 종류를 선택해 주세요.", "error"); return false; }
    if (activeController) activeController.abort();
    const requestId = ++state.requestId;
    const requestKey = fingerprint({ destination: latestDestination || currentDestination(), route: latestRoute, categories: request.categories });
    activeController = new AbortController();
    state.status = "loading"; state.response = null; state.error = null; render();
    setStatus("로컬 POI 인덱스에서 시설을 확인하고 있습니다.", "active");
    try {
      const response = await fetch(API_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request), signal: activeController.signal });
      const payload = await response.json().catch(() => null);
      if (!isCurrent(requestId, requestKey)) return false;
      if (!response.ok || !payload || !Array.isArray(payload.results)) throw new Error("주변 시설 검색에 실패했습니다.");
      state.statuses = payload.category_statuses || {};
      request.categories.forEach((category) => { state.resultsByCategory[category] = payload.results.filter((item) => item && item.category === category); });
      state.response = payload; state.status = payload.complete ? "success" : "partial"; render(); renderMarkers();
      setStatus(payload.complete ? "주변 시설을 확인했습니다." : "일부 시설 데이터는 확인할 수 없습니다.", payload.complete ? "success" : "partial");
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") return false;
      if (!isCurrent(requestId, requestKey)) return false;
      state.status = "error"; state.error = error instanceof Error ? error.message : String(error); state.response = null;
      request.categories.forEach((category) => { state.resultsByCategory[category] = []; });
      render(); renderMarkers(); setStatus(state.error, "error"); return false;
    } finally {
      if (requestId === state.requestId) { activeController = null; render(); }
    }
  }
  function handleSelectionChanged(event) {
    const destination = event && event.detail ? event.detail.destination || null : currentDestination();
    const changed = fingerprint(destination) !== fingerprint(latestDestination);
    latestDestination = destination;
    if (changed && (state.response || state.status === "loading")) clear("목적지가 변경되어 주변 시설을 초기화했습니다.");
    else render();
  }
  function handleRouteChanged(event) {
    latestRoute = event && event.detail ? event.detail.route || null : null;
    if (state.response || state.status === "loading") clear("경로가 변경되어 경로 주변 시설을 초기화했습니다.");
  }
  window.addEventListener("kto:selection-changed", handleSelectionChanged);
  window.addEventListener("kto:route-changed", handleRouteChanged);
  window.addEventListener("kto:map-ready", (event) => {
    mapInstance = event && event.detail ? event.detail.map : null;
    if (mapInstance && typeof mapInstance.on === "function") mapInstance.on("zoomend", renderMarkers);
    renderMarkers();
  });
  window.KoreaTripPoi = Object.freeze({
    search, clear: () => clear("주변 시설을 초기화했습니다."),
    setCategoryVisible(category, visible) { if (!CATEGORIES.includes(category)) return false; state.visible[category] = Boolean(visible); renderCategory(category); return true; },
    getState: () => ({ status: state.status, resultsByCategory: Object.fromEntries(CATEGORIES.map((category) => [category, state.resultsByCategory[category].slice()])), statuses: { ...state.statuses }, response: state.response, error: state.error }),
    formatStatus: statusLabel,
  });
  document.addEventListener("DOMContentLoaded", () => {
    latestDestination = currentDestination();
    CATEGORIES.forEach((category) => {
      const checkbox = element(`poi-category-${category}`);
      if (checkbox) checkbox.addEventListener("change", () => { state.visible[category] = checkbox.checked; if (state.status === "loading") { state.requestId += 1; if (activeController) activeController.abort(); activeController = null; state.status = "idle"; } renderCategory(category); render(); });
    });
    const button = element("poi-search");
    if (button) button.addEventListener("click", search);
    render();
  });
})();
