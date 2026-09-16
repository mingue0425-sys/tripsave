(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.recommendationsApiUrl || "/api/recommendations/rank";
  const PRESETS = Object.freeze({
    balanced: { cost: 30, accommodation: 25, restaurant: 10, attraction: 15, driving: 20 },
    lowest_cost: { cost: 100, accommodation: 0, restaurant: 0, attraction: 0, driving: 0 },
    value: { cost: 40, accommodation: 25, restaurant: 10, attraction: 15, driving: 10 },
    accommodation_quality: { cost: 15, accommodation: 65, restaurant: 5, attraction: 5, driving: 10 },
    sightseeing: { cost: 10, accommodation: 15, restaurant: 15, attraction: 50, driving: 10 },
    low_driving: { cost: 15, accommodation: 10, restaurant: 5, attraction: 0, driving: 70 },
  });
  const WEIGHT_NAMES = ["cost", "accommodation", "restaurant", "attraction", "driving"];
  const state = {
    candidates: [],
    candidateSetId: null,
    candidateFingerprint: null,
    response: null,
    status: "idle",
    error: null,
    requestId: 0,
  };
  let activeController = null;

  function getElement(id) {
    return document.getElementById(id);
  }

  function setStatus(message, type) {
    const element = getElement("recommendations-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    ["error", "success", "partial"].forEach((value) => {
      element.classList.toggle(`recommendations-status--${value}`, type === value);
    });
  }

  function formatWon(value) {
    return Number.isFinite(value) && value >= 0
      ? `${Math.round(value).toLocaleString("ko-KR")}원`
      : "확인 불가";
  }

  function formatScore(value) {
    return Number.isFinite(value) ? `${(value * 100).toFixed(1)}점` : "확인 불가";
  }

  function formatConfidence(value) {
    return Number.isFinite(value) ? `데이터 신뢰도 ${(value * 100).toFixed(0)}%` : "신뢰도 확인 불가";
  }

  function formatPlaceCount(candidate, key, value) {
    const rawStatus = candidate && candidate.component_statuses
      ? candidate.component_statuses[key]
      : null;
    const status = typeof rawStatus === "string" ? rawStatus.toLowerCase() : "unknown";
    const count = Number.isInteger(value) && value >= 0 ? value : null;
    if (!["ok", "empty", "partial"].includes(status)) {
      return "확인 불가";
    }
    if (status === "partial") {
      return count === null ? "확인 불가 · 일부만 확인" : `${count}곳 · 일부만 확인`;
    }
    return count === null ? "확인 불가" : `${count}곳`;
  }

  function knownSubtotalLabel(candidate) {
    const costs = candidate && candidate.costs ? candidate.costs : {};
    const status = typeof costs.status === "string" ? costs.status.toUpperCase() : "UNKNOWN";
    return status === "PARTIAL" && Number.isFinite(costs.known_subtotal_krw)
      ? `총비용 확인 불가 · 확인된 비용 ${formatWon(costs.known_subtotal_krw)}`
      : "총비용 확인 불가 · 확인된 비용 없음";
  }

  function appendBadge(card, text) {
    const badge = document.createElement("span");
    badge.className = "recommendation-card__badge recommendation-card__badge--warning";
    badge.textContent = text;
    card.appendChild(badge);
  }

  function appendList(card, className, title, values) {
    if (!Array.isArray(values) || !values.length) {
      return;
    }
    const wrapper = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = title;
    wrapper.appendChild(heading);
    const list = document.createElement("ul");
    list.className = className;
    values.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = typeof value === "string" ? value : String(value);
      list.appendChild(item);
    });
    wrapper.appendChild(list);
    card.appendChild(wrapper);
  }

  function candidateName(candidate) {
    if (candidate && candidate.accommodation && candidate.accommodation.name) {
      return candidate.accommodation.name;
    }
    return candidate && candidate.trip_type === "DAY_TRIP" ? "당일 여행" : "숙소 미정";
  }

  function renderRecommendation(recommendation, candidateById) {
    const card = document.createElement("article");
    card.className = "recommendation-card";

    const heading = document.createElement("div");
    heading.className = "recommendation-card__heading";
    const rank = document.createElement("span");
    rank.className = "recommendation-card__rank";
    rank.textContent = `${recommendation.rank}위`;
    const name = document.createElement("h3");
    name.className = "recommendation-card__name";
    name.textContent = candidateName(candidateById.get(recommendation.candidate_id));
    const score = document.createElement("span");
    score.className = "recommendation-card__score";
    score.textContent = `추천점수 ${formatScore(recommendation.final_score)}`;
    heading.append(rank, name, score);
    card.appendChild(heading);

    const candidate = candidateById.get(recommendation.candidate_id);
    const candidateCosts = candidate && candidate.costs ? candidate.costs : null;
    if (Number.isFinite(recommendation.confidence) && recommendation.confidence < 0.5) {
      appendBadge(card, "잠정 추천 · 데이터 신뢰도 낮음");
    }
    if (candidateCosts && candidateCosts.status === "ESTIMATED_COMPLETE") {
      appendBadge(card, "비용 일부 추정");
    }
    const metrics = document.createElement("div");
    metrics.className = "recommendation-card__metrics";
    const total = candidate && candidate.costs ? candidate.costs.total_krw : null;
    const totalLabel = Number.isFinite(total)
      ? candidate && candidate.costs && candidate.costs.status === "ESTIMATED_COMPLETE"
        ? `예상 총비용 ${formatWon(total)}`
        : `총비용 ${formatWon(total)}`
      : knownSubtotalLabel(candidate);
    [
      totalLabel,
      formatConfidence(recommendation.confidence),
      candidate && candidate.quality && Number.isFinite(candidate.quality.driving_duration_min)
        ? `운전 ${candidate.quality.driving_duration_min.toFixed(0)}분`
        : "운전시간 확인 불가",
      candidate && candidate.quality
        ? `관광지 ${formatPlaceCount(candidate, "attractions", candidate.quality.nearby_attraction_count)}`
        : "관광지 확인 불가",
    ].forEach((value) => {
      const item = document.createElement("span");
      item.textContent = value;
      metrics.appendChild(item);
    });
    card.appendChild(metrics);

    const explanation = document.createElement("p");
    explanation.className = "recommendation-card__explanation";
    explanation.textContent = recommendation.explanation || "현재 후보 집합에서 비교 가능한 feature로 계산했습니다.";
    card.appendChild(explanation);
    appendList(card, "recommendation-card__list", "강점", recommendation.strengths);
    appendList(card, "recommendation-card__list", "주의", recommendation.weaknesses);
    appendList(card, "recommendation-card__list recommendation-card__list--warning", "데이터 주의", recommendation.warnings);
    return card;
  }

  function renderExcluded(response, candidateById) {
    const container = getElement("recommendations-excluded");
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (!Array.isArray(response && response.excluded_candidates) || !response.excluded_candidates.length) {
      container.hidden = true;
      return;
    }
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `추천 모드에서 제외된 후보 ${response.excluded_candidates.length}개`;
    details.appendChild(summary);
    const list = document.createElement("ul");
    response.excluded_candidates.forEach((excluded) => {
      const item = document.createElement("li");
      const candidate = candidateById.get(excluded.candidate_id);
      item.textContent = `${candidateName(candidate)}: ${(excluded.reasons || []).join(", ")}`;
      list.appendChild(item);
    });
    details.appendChild(list);
    container.appendChild(details);
    container.hidden = false;
  }

  function render() {
    const button = getElement("recommendations-rank");
    const results = getElement("recommendations-results");
    const count = getElement("recommendations-count");
    if (!button || !results || !count) {
      return;
    }
    button.disabled = !state.candidates.length || state.status === "loading";
    button.textContent = state.status === "loading" ? "추천 순위 계산 중…" : "추천 순위 생성";
    count.hidden = !state.response;
    count.textContent = state.response
      ? `${state.response.eligible_count}개 후보 비교 · ${state.response.recommendations.length}개 표시`
      : "";
    results.replaceChildren();
    if (!state.response) {
      renderExcluded(null, new Map());
      return;
    }
    const candidateById = new Map(state.candidates.map((candidate) => [candidate.id, candidate]));
    if (!state.response.recommendations.length) {
      const empty = document.createElement("p");
      empty.className = "recommendations-empty";
      empty.textContent = "현재 모드에서 추천 가능한 후보가 없습니다.";
      results.appendChild(empty);
    } else {
      state.response.recommendations.forEach((recommendation) => {
        results.appendChild(renderRecommendation(recommendation, candidateById));
      });
    }
    renderExcluded(state.response, candidateById);
  }

  function selectedMode() {
    return getElement("recommendation-mode")?.value || "balanced";
  }

  function customWeights() {
    const toggle = getElement("recommendations-custom-toggle");
    if (!toggle || !toggle.checked) {
      return null;
    }
    const result = {};
    WEIGHT_NAMES.forEach((name) => {
      const element = getElement(`recommendation-weight-${name}`);
      const value = element ? Number(element.value) : 0;
      result[name] = Number.isFinite(value) && value >= 0 ? value / 100 : 0;
    });
    return result;
  }

  function syncWeightLabels() {
    WEIGHT_NAMES.forEach((name) => {
      const input = getElement(`recommendation-weight-${name}`);
      const output = getElement(`recommendation-weight-${name}-value`);
      if (input && output) {
        output.textContent = `${Number(input.value) || 0}%`;
      }
    });
  }

  function setPreset(mode) {
    const preset = PRESETS[mode] || PRESETS.balanced;
    WEIGHT_NAMES.forEach((name) => {
      const input = getElement(`recommendation-weight-${name}`);
      if (input) {
        input.value = String(preset[name]);
      }
    });
    syncWeightLabels();
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
    if (message) {
      setStatus(message, "active");
    }
    render();
  }

  async function rank() {
    if (!state.candidates.length) {
      setStatus("먼저 여행 후보를 조립하세요.", "error");
      return false;
    }
    if (!state.candidateSetId || !state.candidateFingerprint) {
      setStatus("후보 집합 식별자를 확인할 수 없습니다. 여행 후보를 다시 조립하세요.", "error");
      return false;
    }
    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const candidateSetId = state.candidateSetId;
    const candidateFingerprint = state.candidateFingerprint;
    activeController = new AbortController();
    state.status = "loading";
    state.response = null;
    state.error = null;
    setStatus("후보의 비용·품질·신뢰도를 비교하는 중입니다…", "active");
    render();
    const selectedWeights = customWeights();
    const payload = {
      mode: selectedWeights ? "custom" : selectedMode(),
      candidate_set_id: candidateSetId,
      candidate_ids: state.candidates.map((candidate) => candidate.id),
      request_fingerprint: candidateFingerprint,
      custom_weights: selectedWeights,
      limit: 10,
    };
    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: activeController.signal,
      });
      const body = await response.json().catch(() => null);
      if (
        requestId !== state.requestId ||
        candidateSetId !== state.candidateSetId ||
        candidateFingerprint !== state.candidateFingerprint
      ) {
        return false;
      }
      if (
        response.ok &&
        (!body || body.candidate_set_id !== candidateSetId || body.request_fingerprint !== candidateFingerprint)
      ) {
        return false;
      }
      if (!response.ok || !body || !Array.isArray(body.recommendations)) {
        throw new Error(body && body.error && body.error.message
          ? body.error.message
          : "추천 순위 생성에 실패했습니다.");
      }
      state.response = body;
      state.status = body.recommendations.length ? "success" : "partial";
      setStatus(
        body.recommendations.length
          ? "현재 후보 집합 내 비교 점수입니다. 데이터 신뢰도와 주의사항을 함께 확인하세요."
          : "현재 모드에서 추천 가능한 후보가 없습니다.",
        body.recommendations.length ? "success" : "partial"
      );
      render();
      window.dispatchEvent(new CustomEvent("kto:recommendations", { detail: body }));
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (
        requestId !== state.requestId ||
        candidateSetId !== state.candidateSetId ||
        candidateFingerprint !== state.candidateFingerprint
      ) {
        return false;
      }
      state.status = "error";
      state.error = error;
      setStatus(error && error.message ? error.message : "추천 순위 생성에 실패했습니다.", "error");
      render();
      return false;
    } finally {
      if (requestId === state.requestId) {
        activeController = null;
      }
    }
  }

  function setCandidates(payload) {
    clear("새 여행 후보가 반영되었습니다. 추천 순위를 다시 생성하세요.");
    state.candidates = Array.isArray(payload && payload.candidates) ? payload.candidates : [];
    state.candidateSetId = payload && typeof payload.candidate_set_id === "string"
      ? payload.candidate_set_id
      : null;
    state.candidateFingerprint = payload && payload.request_fingerprint ? payload.request_fingerprint : null;
    if (state.candidates.length) {
      setStatus("여행 후보가 준비되었습니다. 추천 모드를 선택하세요.", "active");
    }
    render();
  }

  function invalidate(message) {
    if (state.response || state.status === "loading") {
      clear(message);
    }
  }

  window.addEventListener("kto:trip-candidates", (event) => setCandidates(event.detail || {}));
  [
    "kto:selection-changed",
    "kto:route-changed",
    "kto:driving-cost-result",
    "kto:accommodation-results",
    "kto:places-results",
  ].forEach((eventName) => {
    window.addEventListener(eventName, () => {
      state.candidates = [];
      state.candidateSetId = null;
      state.candidateFingerprint = null;
      invalidate("여행 조건이 변경되어 추천 결과를 초기화했습니다.");
      render();
    });
  });

  document.addEventListener("DOMContentLoaded", () => {
    const button = getElement("recommendations-rank");
    const mode = getElement("recommendation-mode");
    const customToggle = getElement("recommendations-custom-toggle");
    const customWeightsPanel = getElement("recommendations-custom-weights");
    if (button) {
      button.addEventListener("click", rank);
    }
    if (mode) {
      mode.addEventListener("change", () => {
        if (customToggle && customWeightsPanel) {
          customToggle.checked = false;
          customWeightsPanel.hidden = true;
        }
        setPreset(mode.value);
        clear("추천 모드가 변경되었습니다. 다시 계산하세요.");
      });
      setPreset(mode.value);
    }
    if (customToggle && customWeightsPanel) {
      customToggle.addEventListener("change", () => {
        customWeightsPanel.hidden = !customToggle.checked;
        clear("사용자 가중치가 변경되었습니다. 다시 계산하세요.");
      });
    }
    WEIGHT_NAMES.forEach((name) => {
      const input = getElement(`recommendation-weight-${name}`);
      if (input) {
        input.addEventListener("input", () => {
          syncWeightLabels();
          clear("사용자 가중치가 변경되었습니다. 다시 계산하세요.");
        });
      }
    });
    const candidateState = window.KoreaTripCandidates && window.KoreaTripCandidates.getState
      ? window.KoreaTripCandidates.getState()
      : null;
    if (candidateState && candidateState.response) {
      setCandidates(candidateState.response);
    }
    syncWeightLabels();
    render();
  });

  window.KoreaTripRecommendations = Object.freeze({
    rank,
    clear: () => clear("추천 결과를 초기화했습니다."),
    formatPlaceCount,
    getState: () => ({
      candidates: state.candidates,
      candidateSetId: state.candidateSetId,
      candidateFingerprint: state.candidateFingerprint,
      response: state.response,
      status: state.status,
      error: state.error,
    }),
  });
})();
