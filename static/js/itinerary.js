(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.itineraryApiUrl || "/api/routes/optimize";
  const state = { status: "idle", waypoints: [], result: null, error: null, requestId: 0 };
  let activeController = null;
  let latestSelection = { origin: null, destination: null };

  function element(id) { return document.getElementById(id); }
  function selection() {
    const api = window.KoreaTripSelection;
    return api ? { origin: api.getOrigin(), destination: api.getDestination() } : { origin: null, destination: null };
  }
  function pointFingerprint(point) { return point ? `${point.lat}|${point.lng}|${point.label || point.name || ""}` : "none"; }
  function inputFingerprint() {
    return JSON.stringify({ origin: pointFingerprint(latestSelection.origin), destination: pointFingerprint(latestSelection.destination), waypoints: state.waypoints, mode: element("itinerary-mode")?.value || "", start: element("itinerary-start-datetime")?.value || "", limit: element("itinerary-daily-limit")?.value || "" });
  }
  function setStatus(message, type) {
    const target = element("itinerary-status");
    if (!target) return;
    target.textContent = message;
    target.classList.toggle("itinerary-status--error", type === "error");
    target.classList.toggle("itinerary-status--success", type === "success");
    target.classList.toggle("itinerary-status--partial", type === "partial");
  }
  function invalidate(message) {
    state.requestId += 1;
    if (activeController) activeController.abort();
    activeController = null;
    state.result = null;
    state.status = "idle";
    state.error = null;
    if (window.KoreaTripItineraryLayers) window.KoreaTripItineraryLayers.clearRoute();
    if (message) setStatus(message, "active");
    render();
  }
  function moveWaypoint(index, direction) {
    const target = index + direction;
    if (target < 0 || target >= state.waypoints.length) return;
    const next = state.waypoints.slice();
    [next[index], next[target]] = [next[target], next[index]];
    state.waypoints = next;
    invalidate("경유지 순서가 변경되었습니다. 다시 최적화하세요.");
  }
  function renderWaypoints() {
    const target = element("itinerary-waypoints");
    if (!target) return;
    target.replaceChildren();
    state.waypoints.forEach((waypoint, index) => {
      const row = document.createElement("div"); row.className = "itinerary-waypoint";
      const name = document.createElement("span"); name.className = "itinerary-waypoint__name"; name.textContent = `${index + 1}. ${waypoint.name}`; row.appendChild(name);
      const controls = document.createElement("div"); controls.className = "itinerary-waypoint__controls";
      const duration = document.createElement("input"); duration.className = "itinerary-waypoint__duration"; duration.type = "number"; duration.min = "0"; duration.max = "1440"; duration.step = "5"; duration.value = String(waypoint.visit_duration_min || 0); duration.setAttribute("aria-label", `${waypoint.name} 방문 시간(분)`);
      duration.addEventListener("change", () => { const value = Number(duration.value); if (Number.isInteger(value) && value >= 0 && value <= 1440) { state.waypoints[index] = { ...state.waypoints[index], visit_duration_min: value }; invalidate("방문 시간이 변경되었습니다. 다시 최적화하세요."); } });
      controls.appendChild(duration);
      [["↑", -1, "위로 이동"], ["↓", 1, "아래로 이동"]].forEach(([label, direction, aria]) => { const button = document.createElement("button"); button.type = "button"; button.textContent = label; button.setAttribute("aria-label", aria); button.disabled = (direction < 0 && index === 0) || (direction > 0 && index === state.waypoints.length - 1); button.addEventListener("click", () => moveWaypoint(index, direction)); controls.appendChild(button); });
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "삭제"; remove.setAttribute("aria-label", `${waypoint.name} 경유지 삭제`); remove.addEventListener("click", () => { state.waypoints = state.waypoints.filter((_item, itemIndex) => itemIndex !== index); invalidate("경유지를 삭제했습니다."); }); controls.appendChild(remove);
      row.appendChild(controls); target.appendChild(row);
    });
  }
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds)) return "확인 불가";
    const minutes = Math.round(seconds / 60); return `${Math.floor(minutes / 60)}시간 ${minutes % 60}분`;
  }
  function formatCost(value, complete) { return Number.isFinite(value) && complete ? `${new Intl.NumberFormat("ko-KR").format(value)}원` : "확인 불가"; }
  function formatTime(value) { if (typeof value !== "string") return "시각 미확인"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ko-KR", { timeZone: config.weatherTimezone || "Asia/Seoul", hour: "2-digit", minute: "2-digit" }); }
  function weatherLabel(value) {
    if (!value || typeof value !== "string" || !window.KoreaTripWeather || typeof window.KoreaTripWeather.getForDate !== "function") return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    const day = window.KoreaTripWeather.getForDate(date.toLocaleDateString("en-CA", { timeZone: config.weatherTimezone || "Asia/Seoul" }));
    if (!day) return null;
    const temperature = Number.isFinite(Number(day.temp_min_c)) && Number.isFinite(Number(day.temp_max_c)) ? ` ${Number(day.temp_min_c).toFixed(0)}~${Number(day.temp_max_c).toFixed(0)}°C` : "";
    return ` · 날씨 ${day.condition || "확인 불가"}${temperature}`;
  }
  function renderResult() {
    const target = element("itinerary-result"); if (!target) return; target.replaceChildren();
    const result = state.result; if (!result) return;
    const summary = document.createElement("div"); summary.className = "itinerary-result__summary";
    const title = document.createElement("strong"); title.textContent = result.feasible ? "최적화된 경유지 순서" : "실행할 수 없는 경로"; summary.appendChild(title);
    const values = [`거리 ${Number.isFinite(result.total_distance_m) ? (result.total_distance_m / 1000).toFixed(1) + "km" : "확인 불가"}`, `시간 ${formatDuration(result.total_duration_s)}`, `비용 ${formatCost(result.total_cost_krw, result.cost_complete)}`];
    const metrics = document.createElement("span"); metrics.textContent = values.join(" · "); summary.appendChild(metrics); target.appendChild(summary);
    (Array.isArray(result.warnings) ? result.warnings : []).forEach((warning) => { const node = document.createElement("p"); node.className = "itinerary-warning"; node.textContent = `주의: ${warning}`; target.appendChild(node); });
    (Array.isArray(result.segments) ? result.segments : []).forEach((segment, index) => { const node = document.createElement("div"); node.className = "itinerary-segment"; node.textContent = `${index + 1}. ${segment.from_id} → ${segment.to_id} · ${(segment.distance_m / 1000).toFixed(1)}km · ${formatDuration(segment.duration_s)} · 도착 ${formatTime(segment.arrival_time)}${weatherLabel(segment.arrival_time) || ""}${segment.visit_duration_min ? ` · 방문 ${segment.visit_duration_min}분` : ""}`; target.appendChild(node); });
  }
  function render() {
    const button = element("itinerary-optimize"); const summary = element("itinerary-summary"); if (!button || !summary) return;
    button.disabled = !latestSelection.origin || !latestSelection.destination || state.status === "loading";
    button.textContent = state.status === "loading" ? "경로 최적화 중…" : "경유지 최적화";
    summary.textContent = state.waypoints.length ? `${state.waypoints.length}개 경유지를 ${latestSelection.destination ? (latestSelection.destination.name || latestSelection.destination.label || "목적지") : "목적지"}까지 연결합니다.` : "맛집·관광지 카드에서 경유지를 추가하세요.";
    renderWaypoints(); renderResult();
  }
  function addWaypoint(value) {
    if (!value || !Number.isFinite(value.lat) || !Number.isFinite(value.lng)) return false;
    if (state.waypoints.some((waypoint) => waypoint.id === value.id)) { setStatus("이미 추가한 경유지입니다.", "active"); return false; }
    if (state.waypoints.length >= 20) { setStatus("경유지는 최대 20개까지 추가할 수 있습니다.", "error"); return false; }
    state.waypoints = [...state.waypoints, { id: String(value.id), name: String(value.name || "경유지"), lat: value.lat, lng: value.lng, category: value.category || null, visit_duration_min: Number.isInteger(value.visit_duration_min) ? value.visit_duration_min : 0 }];
    invalidate("경유지를 추가했습니다. 최적화 기준을 선택하세요."); return true;
  }
  function requestPayload() {
    const start = element("itinerary-start-datetime")?.value || null; const limit = Number(element("itinerary-daily-limit")?.value || 360);
    return { origin: { id: "origin", name: latestSelection.origin.name || latestSelection.origin.label || "출발지", lat: latestSelection.origin.lat, lng: latestSelection.origin.lng }, destination: { id: "destination", name: latestSelection.destination.name || latestSelection.destination.label || "목적지", lat: latestSelection.destination.lat, lng: latestSelection.destination.lng }, waypoints: state.waypoints, mode: element("itinerary-mode")?.value || "fastest", start_datetime: start, max_daily_driving_min: Number.isInteger(limit) && limit > 0 ? limit : 360, include_geometry: true };
  }
  async function optimize() {
    if (!latestSelection.origin || !latestSelection.destination) { setStatus("출발지와 목적지를 먼저 선택하세요.", "error"); return false; }
    if (activeController) activeController.abort();
    const requestId = ++state.requestId; const key = inputFingerprint(); activeController = new AbortController(); state.status = "loading"; state.result = null; render(); setStatus("로컬 OSRM에서 경유지 매트릭스를 계산하고 있습니다.", "active");
    try {
      const response = await fetch(API_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestPayload()), signal: activeController.signal }); const payload = await response.json().catch(() => null);
      if (requestId !== state.requestId || key !== inputFingerprint()) return false;
      if (!response.ok || !payload || !Array.isArray(payload.segments)) throw new Error(payload?.detail?.message || "경유지 경로 최적화에 실패했습니다.");
      state.result = payload; state.status = payload.feasible ? "success" : "partial"; if (window.KoreaTripItineraryLayers) window.KoreaTripItineraryLayers.setGeometry(payload.geometry || null); window.dispatchEvent(new CustomEvent("kto:itinerary-result", { detail: payload })); render(); setStatus(payload.feasible ? "경유지 순서를 계산했습니다." : "현재 조건에서 실행 가능한 경로가 아닙니다.", payload.feasible ? "success" : "partial"); return true;
    } catch (error) { if (error && error.name === "AbortError") return false; if (requestId !== state.requestId || key !== inputFingerprint()) return false; state.status = "error"; state.error = error instanceof Error ? error.message : String(error); render(); setStatus(state.error, "error"); return false; }
    finally { if (requestId === state.requestId) { activeController = null; render(); } }
  }
  function handleSelectionChanged(event) { const next = event && event.detail ? { origin: event.detail.origin || null, destination: event.detail.destination || null } : selection(); const changed = pointFingerprint(next.origin) !== pointFingerprint(latestSelection.origin) || pointFingerprint(next.destination) !== pointFingerprint(latestSelection.destination); latestSelection = next; if (changed) { state.waypoints = []; invalidate("출발지 또는 목적지가 변경되어 일정을 초기화했습니다."); } else render(); }
  function handleInputChanged() { if (state.result || state.status === "loading") invalidate("최적화 조건이 변경되었습니다. 다시 계산하세요."); }
  window.addEventListener("kto:selection-changed", handleSelectionChanged);
  window.addEventListener("kto:waypoint-added", (event) => addWaypoint(event?.detail));
  window.addEventListener("kto:route-changed", () => { if (state.result) invalidate("기본 경로가 변경되어 경유지 결과를 초기화했습니다."); });
  window.addEventListener("kto:weather-result", () => { if (state.result) render(); });
  window.KoreaTripItinerary = Object.freeze({ optimize, addWaypoint, clear: () => { state.waypoints = []; invalidate("경유지 일정을 초기화했습니다."); }, getState: () => ({ status: state.status, waypoints: state.waypoints.slice(), result: state.result, error: state.error }) });
  document.addEventListener("DOMContentLoaded", () => { latestSelection = selection(); ["itinerary-mode", "itinerary-start-datetime", "itinerary-daily-limit"].forEach((id) => element(id)?.addEventListener("change", handleInputChanged)); element("itinerary-optimize")?.addEventListener("click", optimize); render(); });
})();
