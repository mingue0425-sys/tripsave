(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.weatherApiUrl || "/api/weather/forecast";
  const state = { status: "idle", response: null, error: null, requestId: 0 };
  let activeController = null;
  let latestDestination = null;

  function element(id) { return document.getElementById(id); }
  function destination() { const api = window.KoreaTripSelection; return api && typeof api.getDestination === "function" ? api.getDestination() : null; }
  function dateValue(id) { return element(id)?.value || ""; }
  function dateKey() { return `${dateValue("weather-start-date")}|${dateValue("weather-end-date")}`; }
  function setStatus(message, type) { const target = element("weather-status"); if (!target) return; target.textContent = message; target.classList.toggle("weather-status--error", type === "error"); target.classList.toggle("weather-status--success", type === "success"); target.classList.toggle("weather-status--partial", type === "partial"); }
  function currentFingerprint() { return `${latestDestination ? `${latestDestination.lat}|${latestDestination.lng}|${latestDestination.label || latestDestination.name || ""}` : "none"}|${dateKey()}`; }
  function known(value, formatter) { return value === null || value === undefined || !Number.isFinite(Number(value)) ? "확인 불가" : formatter(Number(value)); }
  function formatDate(value) { if (typeof value !== "string") return "날짜 미확인"; const date = new Date(`${value}T00:00:00+09:00`); return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("ko-KR", { timeZone: config.weatherTimezone || "Asia/Seoul", month: "long", day: "numeric", weekday: "short" }); }
  function render() {
    const button = element("weather-search"); const summary = element("weather-summary"); if (!button || !summary) return;
    button.disabled = !latestDestination || !dateValue("weather-start-date") || !dateValue("weather-end-date") || state.status === "loading";
    button.textContent = state.status === "loading" ? "날씨 확인 중…" : "날씨 확인";
    summary.textContent = latestDestination ? `${latestDestination.name || latestDestination.label || "선택한 목적지"}의 날짜별 기상정보를 표시합니다.` : "목적지와 여행 날짜를 선택하면 예보를 확인합니다.";
    renderResults();
  }
  function renderResults() {
    const target = element("weather-results"); if (!target) return; target.replaceChildren(); if (!state.response) return;
    if (state.response.available_until) { const note = document.createElement("p"); note.className = "weather-day__values"; note.textContent = `예보 가능 기간: ${formatDate(state.response.available_until)}까지`; target.appendChild(note); }
    (Array.isArray(state.response.forecast) ? state.response.forecast : []).forEach((day) => {
      const card = document.createElement("article"); card.className = "weather-day"; if (day.stale) card.classList.add("weather-day--stale");
      const heading = document.createElement("div"); heading.className = "weather-day__heading";
      const date = document.createElement("h3"); date.className = "weather-day__date"; date.textContent = formatDate(day.date); heading.appendChild(date);
      const condition = document.createElement("span"); condition.className = "weather-day__condition"; condition.textContent = day.condition || (day.status === "not_available_yet" ? "아직 예보 없음" : day.status === "unavailable" ? "확인 불가" : "상태 미확인"); heading.appendChild(condition); card.appendChild(heading);
      const values = document.createElement("p"); values.className = "weather-day__values";
      values.textContent = [`기온 ${known(day.temp_min_c, (value) => `${value.toFixed(1)}°C`)} ~ ${known(day.temp_max_c, (value) => `${value.toFixed(1)}°C`)}`, `강수확률 ${known(day.precipitation_probability_pct, (value) => `${value.toFixed(0)}%`)}`, `예상 강수량 ${known(day.precipitation_mm, (value) => `${value.toFixed(1)}mm`)}`, `풍속 ${known(day.wind_speed_mps, (value) => `${value.toFixed(1)}m/s`)}`].join(" · "); card.appendChild(values);
      if (day.stale) { const stale = document.createElement("p"); stale.className = "weather-day__values"; stale.textContent = "오래된 캐시를 표시 중입니다."; card.appendChild(stale); }
      target.appendChild(card);
    });
  }
  function setDefaultDates() {
    const accommodationStart = element("accommodation-checkin")?.value || ""; const accommodationEnd = element("accommodation-checkout")?.value || "";
    const start = element("weather-start-date"); const end = element("weather-end-date"); if (!start || !end) return;
    if (accommodationStart) start.value = accommodationStart;
    if (accommodationEnd) end.value = accommodationEnd;
    const localDateValue = (value) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
    if (!start.value) { const today = new Date(); today.setDate(today.getDate() + 1); start.value = localDateValue(today); }
    if (!end.value) { const date = new Date(`${start.value}T00:00:00`); date.setDate(date.getDate() + 2); end.value = localDateValue(date); }
  }
  function clear(message) { state.requestId += 1; if (activeController) activeController.abort(); activeController = null; state.status = "idle"; state.response = null; state.error = null; if (message) setStatus(message, "active"); render(); }
  async function search() {
    if (!latestDestination) { setStatus("목적지를 먼저 선택해 주세요.", "error"); return false; }
    const start = dateValue("weather-start-date"); const end = dateValue("weather-end-date"); if (!start || !end || end < start) { setStatus("날짜 범위를 올바르게 입력해 주세요.", "error"); return false; }
    if (activeController) activeController.abort(); const requestId = ++state.requestId; const key = currentFingerprint(); activeController = new AbortController(); state.status = "loading"; state.response = null; render(); setStatus("공개 기상 provider에서 예보를 확인하고 있습니다.", "active");
    try {
      const response = await fetch(API_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lat: latestDestination.lat, lng: latestDestination.lng, start_date: start, end_date: end, timezone: config.weatherTimezone || "Asia/Seoul" }), signal: activeController.signal }); const payload = await response.json().catch(() => null);
      if (requestId !== state.requestId || key !== currentFingerprint()) return false; if (!response.ok || !payload || !Array.isArray(payload.forecast)) throw new Error("날씨 확인에 실패했습니다.");
      state.response = payload; state.status = payload.status === "ok" ? "success" : "partial"; window.dispatchEvent(new CustomEvent("kto:weather-result", { detail: payload })); render(); setStatus(payload.status === "ok" ? "날씨를 확인했습니다." : "일부 날짜의 예보를 확인할 수 없습니다.", payload.status === "ok" ? "success" : "partial"); return true;
    } catch (error) { if (error && error.name === "AbortError") return false; if (requestId !== state.requestId || key !== currentFingerprint()) return false; state.status = "error"; state.error = error instanceof Error ? error.message : String(error); state.response = null; render(); setStatus(state.error, "error"); return false; }
    finally { if (requestId === state.requestId) { activeController = null; render(); } }
  }
  function handleSelectionChanged(event) { const next = event && event.detail ? event.detail.destination || null : destination(); const changed = currentFingerprint().split("|").slice(0, 2).join("|") !== (next ? `${next.lat}|${next.lng}` : "none"); latestDestination = next; if (changed && (state.response || state.status === "loading")) clear("목적지가 변경되어 날씨를 초기화했습니다."); else render(); }
  window.addEventListener("kto:selection-changed", handleSelectionChanged);
  window.KoreaTripWeather = Object.freeze({ search, clear: () => clear("날씨 결과를 초기화했습니다."), getState: () => ({ status: state.status, response: state.response, error: state.error }), getForDate: (value) => state.response?.forecast?.find((day) => day.date === value) || null });
  document.addEventListener("DOMContentLoaded", () => { latestDestination = destination(); setDefaultDates(); ["weather-start-date", "weather-end-date"].forEach((id) => element(id)?.addEventListener("change", () => { if (state.response || state.status === "loading") clear("날짜가 변경되어 날씨를 초기화했습니다."); else render(); })); ["accommodation-checkin", "accommodation-checkout"].forEach((id) => element(id)?.addEventListener("change", () => { if (!state.response && state.status !== "loading") { setDefaultDates(); render(); } })); element("weather-search")?.addEventListener("click", search); render(); });
})();
