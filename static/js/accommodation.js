(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.accommodationApiUrl || "/api/accommodations/search";
  const state = {
    status: "idle",
    results: [],
    response: null,
    error: null,
    requestId: 0,
  };
  let activeController = null;
  let latestDestination = null;

  function getElement(id) {
    return document.getElementById(id);
  }

  function currentDestination() {
    const selection = window.KoreaTripSelection;
    return selection && typeof selection.getDestination === "function"
      ? selection.getDestination()
      : null;
  }

  function destinationFingerprint(destination) {
    if (!destination) {
      return "none";
    }
    return [
      destination.lat,
      destination.lng,
      destination.label || "",
      destination.name || "",
      destination.source || "",
    ].join("|");
  }

  function localDateValue(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function setDefaultDates() {
    const checkin = getElement("accommodation-checkin");
    const checkout = getElement("accommodation-checkout");
    if (!checkin || !checkout) {
      return;
    }
    const today = new Date();
    const firstNight = new Date(today);
    firstNight.setDate(today.getDate() + 7);
    const lastNight = new Date(firstNight);
    lastNight.setDate(firstNight.getDate() + 1);
    checkin.min = localDateValue(today);
    checkout.min = localDateValue(today);
    if (!checkin.value) {
      checkin.value = localDateValue(firstNight);
    }
    if (!checkout.value) {
      checkout.value = localDateValue(lastNight);
    }
  }

  function setStatus(message, statusType) {
    const element = getElement("accommodation-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle("accommodation-status--error", statusType === "error");
    element.classList.toggle("accommodation-status--success", statusType === "success");
  }

  function displayName(destination) {
    if (!destination) {
      return "목적지";
    }
    return destination.name || destination.label || "선택한 목적지";
  }

  function readInteger(id, minimum, maximum) {
    const element = getElement(id);
    if (!element || element.value.trim() === "") {
      return null;
    }
    const value = Number(element.value);
    return Number.isInteger(value) && value >= minimum && value <= maximum
      ? value
      : null;
  }

  function buildRequest() {
    const destination = latestDestination || currentDestination();
    const checkin = getElement("accommodation-checkin")?.value || "";
    const checkout = getElement("accommodation-checkout")?.value || "";
    const adults = readInteger("accommodation-adults", 1, 20);
    const children = readInteger("accommodation-children", 0, 20);
    if (!destination) {
      setStatus("목적지를 먼저 선택하세요.", "error");
      return null;
    }
    if (!checkin || !checkout || checkout <= checkin) {
      setStatus("체크인·체크아웃 날짜를 올바르게 선택하세요.", "error");
      return null;
    }
    if (adults === null || children === null) {
      setStatus("성인·아동 인원을 올바르게 입력하세요.", "error");
      return null;
    }
    return {
      destination: {
        lat: destination.lat,
        lng: destination.lng,
        label: destination.name || destination.label || null,
        source: destination.source || "map",
      },
      checkin,
      checkout,
      adults,
      children,
      radius_km: 20,
    };
  }

  function formatPrice(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "가격 미확인";
    }
    return `₩${new Intl.NumberFormat("ko-KR").format(value)}`;
  }

  function hasKnownPrice(value) {
    return Number.isFinite(value) && value > 0;
  }

  function formatRating(place) {
    if (
      !Number.isFinite(place.rating) ||
      !Number.isFinite(place.rating_scale)
    ) {
      return "평점 미확인";
    }
    return `평점 ${place.rating.toFixed(1)} / ${place.rating_scale}`;
  }

  function formatReviews(place) {
    return Number.isFinite(place.review_count)
      ? `리뷰 ${new Intl.NumberFormat("ko-KR").format(place.review_count)}개`
      : "리뷰 수 미확인";
  }

  function safeBookingUrl(value) {
    if (typeof value !== "string" || value.length > 2000) {
      return null;
    }
    try {
      const url = new URL(value);
      if (
        url.protocol !== "https:" ||
        !["booking.com", "www.booking.com"].includes(url.hostname) ||
        url.username ||
        url.password ||
        url.hash
      ) {
        return null;
      }
      return url.href;
    } catch (error) {
      return null;
    }
  }

  function renderResult(result) {
    const place = result && result.place ? result.place : {};
    const offer = result && Array.isArray(result.offers) ? result.offers[0] || {} : {};
    const card = document.createElement("article");
    card.className = "accommodation-result";

    const heading = document.createElement("div");
    heading.className = "accommodation-result__heading";
    const name = document.createElement("h3");
    name.className = "accommodation-result__name";
    name.textContent = typeof place.name === "string" ? place.name : "이름 미확인 숙소";
    const price = document.createElement("span");
    price.className = "accommodation-result__price";
    if (hasKnownPrice(offer.final_price_krw)) {
      price.textContent = formatPrice(offer.final_price_krw);
    } else if (offer.availability === false) {
      price.textContent = "예약 불가";
    } else {
      price.textContent = "가격 미확인";
    }
    heading.append(name, price);
    card.appendChild(heading);

    const metadata = document.createElement("p");
    metadata.className = "accommodation-result__meta";
    const metaValues = [formatRating(place), formatReviews(place)];
    if (typeof place.address === "string" && place.address) {
      metaValues.push(place.address);
    }
    if (typeof result.distance_text === "string" && result.distance_text) {
      metaValues.push(`위치 ${result.distance_text}`);
    }
    metadata.textContent = metaValues.join(" · ");
    card.appendChild(metadata);

    if (typeof offer.room_name === "string" && offer.room_name) {
      const room = document.createElement("p");
      room.className = "accommodation-result__room";
      room.textContent = `객실: ${offer.room_name}`;
      card.appendChild(room);
    }

    if (hasKnownPrice(offer.base_price_krw)) {
      const breakdown = document.createElement("p");
      breakdown.className = "accommodation-result__meta";
      const values = [`기본 ${formatPrice(offer.base_price_krw)}`];
      if (Number.isFinite(offer.taxes_krw) && offer.taxes_krw >= 0) {
        values.push(`세금·수수료 ${formatPrice(offer.taxes_krw)}`);
      }
      breakdown.textContent = values.join(" · ");
      card.appendChild(breakdown);
    }

    const sourceLine = document.createElement("p");
    sourceLine.className = "accommodation-result__source";
    sourceLine.textContent = `source: ${typeof place.source === "string" ? place.source : "unknown"}`;
    const sourceUrl = safeBookingUrl(place.source_url);
    if (sourceUrl) {
      const separator = document.createTextNode(" · ");
      const link = document.createElement("a");
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "원문";
      sourceLine.append(separator, link);
    }
    card.appendChild(sourceLine);
    return card;
  }

  function renderResults() {
    const container = getElement("accommodation-results");
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (!state.results.length) {
      if (state.status === "success") {
        const empty = document.createElement("p");
        empty.className = "accommodation-empty";
        empty.textContent = "조건에 맞는 숙소가 없습니다.";
        container.appendChild(empty);
      }
      return;
    }
    state.results.forEach((result) => container.appendChild(renderResult(result)));
  }

  function validCoordinates(place) {
    return (
      place &&
      Number.isFinite(place.lat) &&
      Number.isFinite(place.lng) &&
      place.lat >= -90 &&
      place.lat <= 90 &&
      place.lng >= -180 &&
      place.lng <= 180
    );
  }

  function accommodationMarkers(results) {
    return results
      .map((result, index) => {
        const place = result && result.place;
        if (!validCoordinates(place)) {
          return null;
        }
        const offer = result && Array.isArray(result.offers) ? result.offers[0] : null;
        return {
          id: `accommodation:${place.source || "source"}:${
            place.source_id || index
          }`,
          category: "accommodation",
          lat: place.lat,
          lng: place.lng,
          name: place.name,
          address: place.address,
          source: place.source,
          rating: place.rating,
          rating_scale: place.rating_scale,
          review_count: place.review_count,
          price_label:
            offer && hasKnownPrice(offer.final_price_krw)
              ? formatPrice(offer.final_price_krw)
              : "가격 미확인",
        };
      })
      .filter(Boolean);
  }

  function publishMarkers(results = state.results, clearAll = false) {
    window.dispatchEvent(
      new CustomEvent("kto:accommodation-results", {
        detail: {
          markers: accommodationMarkers(results),
          clearAll,
        },
      })
    );
  }

  function currentInputFingerprint() {
    const destination = latestDestination || currentDestination();
    return [
      destinationFingerprint(destination),
      getElement("accommodation-checkin")?.value || "",
      getElement("accommodation-checkout")?.value || "",
      getElement("accommodation-adults")?.value || "",
      getElement("accommodation-children")?.value || "",
      "20",
    ].join("|");
  }

  function render() {
    const button = getElement("accommodation-search");
    const summary = getElement("accommodation-summary");
    if (!button || !summary) {
      return;
    }
    const destination = latestDestination;
    const hasDates = Boolean(
      getElement("accommodation-checkin")?.value &&
        getElement("accommodation-checkout")?.value
    );
    button.disabled = !destination || !hasDates || state.status === "loading";
    button.textContent = state.status === "loading" ? "숙소 검색 중…" : "숙소 검색";
    if (!destination) {
      summary.textContent = "목적지를 선택하면 날짜별 공개 숙소 가격을 검색할 수 있습니다.";
    } else {
      summary.textContent = `${displayName(destination)} 주변 숙소 · 공개 웹 결과`;
    }
    renderResults();
  }

  function clearResult(message) {
    state.requestId += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    state.status = "idle";
    state.results = [];
    state.response = null;
    state.error = null;
    publishMarkers([], true);
    if (message) {
      setStatus(message, "active");
    }
    render();
  }

  async function search() {
    const request = buildRequest();
    if (!request) {
      return false;
    }
    if (activeController) {
      activeController.abort();
    }
    const requestId = ++state.requestId;
    const destinationAtRequest = destinationFingerprint(
      latestDestination || currentDestination()
    );
    const inputAtRequest = currentInputFingerprint();
    activeController = new AbortController();
    state.status = "loading";
    state.results = [];
    state.response = null;
    state.error = null;
    publishMarkers([], true);
    render();
    setStatus("공개 숙박 페이지에서 조건에 맞는 숙소를 확인하고 있습니다.", "active");
    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (
        requestId !== state.requestId ||
        destinationAtRequest !== destinationFingerprint(latestDestination) ||
        inputAtRequest !== currentInputFingerprint()
      ) {
        return false;
      }
      if (!response.ok || !payload || !Array.isArray(payload.results)) {
        const issue = payload && Array.isArray(payload.issues) ? payload.issues[0] : null;
        throw new Error(
          issue && typeof issue.message === "string"
            ? issue.message
            : "숙소 검색에 실패했습니다."
        );
      }
      state.response = payload;
      state.results = payload.results;
      state.status = payload.complete ? "success" : "partial";
      render();
      publishMarkers(state.results);
      if (payload.complete) {
        setStatus(
          `${payload.results.length}개의 숙소 후보를 확인했습니다${payload.cache_hit ? " (캐시)" : ""}.`,
          "success"
        );
      } else {
        const issue = Array.isArray(payload.issues) ? payload.issues[0] : null;
        setStatus(
          issue && typeof issue.message === "string"
            ? `일부 결과만 확인했습니다: ${issue.message}`
            : "일부 결과만 확인했습니다. 가격이 없는 항목은 예약 전에 원문을 확인하세요.",
          "error"
        );
      }
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (
        requestId !== state.requestId ||
        destinationAtRequest !== destinationFingerprint(latestDestination) ||
        inputAtRequest !== currentInputFingerprint()
      ) {
        return false;
      }
      state.status = "error";
      state.results = [];
      state.response = null;
      state.error = error instanceof Error ? error.message : String(error);
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

  function handleSelectionChanged(event) {
    const destination = event && event.detail
      ? event.detail.destination || null
      : currentDestination();
    const changed = destinationFingerprint(destination) !== destinationFingerprint(latestDestination);
    latestDestination = destination;
    if (changed && (state.results.length || state.status === "loading")) {
      clearResult("목적지가 변경되어 숙소 결과를 초기화했습니다.");
      return;
    }
    render();
  }

  window.addEventListener("kto:selection-changed", handleSelectionChanged);

  window.KoreaTripAccommodation = Object.freeze({
    search,
    clear: () => clearResult("숙소 결과를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      results: state.results.slice(),
      response: state.response,
      error: state.error,
    }),
  });

  document.addEventListener("DOMContentLoaded", () => {
    latestDestination = currentDestination();
    setDefaultDates();
    [
      "accommodation-checkin",
      "accommodation-checkout",
      "accommodation-adults",
      "accommodation-children",
    ].forEach((id) => {
      const element = getElement(id);
      if (element) {
        element.addEventListener("input", () => {
          if (state.results.length || state.status === "loading") {
            clearResult("검색 조건이 변경되어 숙소 결과를 초기화했습니다.");
          } else {
            render();
          }
        });
      }
    });
    const button = getElement("accommodation-search");
    if (button) {
      button.addEventListener("click", search);
    }
    render();
  });
})();
