(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const API_URL = config.placesSearchUrl || "/api/places/search";
  const CATEGORIES = Object.freeze(["restaurant", "attraction"]);
  const CATEGORY_LABELS = Object.freeze({
    restaurant: "맛집",
    attraction: "관광지",
  });
  const state = {
    status: "idle",
    resultsByCategory: {
      restaurant: [],
      attraction: [],
    },
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

  function categoryFingerprint(categories) {
    return categories.slice().sort().join(",");
  }

  function displayName(destination) {
    return destination
      ? destination.name || destination.label || "선택한 목적지"
      : "목적지";
  }

  function setStatus(message, statusType) {
    const element = getElement("places-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle("places-status--error", statusType === "error");
    element.classList.toggle("places-status--success", statusType === "success");
    element.classList.toggle("places-status--partial", statusType === "partial");
  }

  function selectedCategories() {
    return CATEGORIES.filter((category) => {
      const checkbox = getElement(`places-category-${category}`);
      return checkbox && checkbox.checked;
    });
  }

  function allResults() {
    return CATEGORIES.flatMap((category) => state.resultsByCategory[category]);
  }

  function replaceResults() {
    state.results = allResults();
  }

  function groupResults(results) {
    const grouped = {
      restaurant: [],
      attraction: [],
    };
    (Array.isArray(results) ? results : []).forEach((place) => {
      if (place && CATEGORIES.includes(place.category)) {
        grouped[place.category].push(place);
      }
    });
    return grouped;
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

  function markersFor(items) {
    return items
      .filter(validCoordinates)
      .map((place, index) => ({
        id: `${place.category || "place"}:${place.source || "source"}:${
          place.source_id || index
        }`,
        lat: place.lat,
        lng: place.lng,
        name: place.name,
        address: place.address,
        category: place.category,
        source: place.source,
        rating: place.rating,
        rating_scale: place.rating_scale,
        review_count: place.review_count,
      }));
  }

  function formatRating(place) {
    if (
      typeof place.rating !== "number" ||
      typeof place.rating_scale !== "number"
    ) {
      return "평점 정보 없음";
    }
    return `평점 ${place.rating.toFixed(1)} / ${place.rating_scale}`;
  }

  function formatReviews(place) {
    return typeof place.review_count === "number"
      ? `리뷰 ${new Intl.NumberFormat("ko-KR").format(place.review_count)}개`
      : "리뷰 수 정보 없음";
  }

  function distanceKm(destination, place) {
    if (
      !destination ||
      !Number.isFinite(destination.lat) ||
      !Number.isFinite(destination.lng) ||
      !Number.isFinite(place.lat) ||
      !Number.isFinite(place.lng)
    ) {
      return null;
    }
    const radians = (value) => (value * Math.PI) / 180;
    const lat1 = radians(destination.lat);
    const lat2 = radians(place.lat);
    const deltaLat = radians(place.lat - destination.lat);
    const deltaLng = radians(place.lng - destination.lng);
    const a =
      Math.sin(deltaLat / 2) ** 2 +
      Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLng / 2) ** 2;
    return 6371.0088 * 2 * Math.asin(Math.sqrt(Math.min(1, Math.max(0, a))));
  }

  function safeSourceUrl(value) {
    if (typeof value !== "string" || value.length > 2000) {
      return null;
    }
    try {
      const url = new URL(value);
      if (
        url.protocol !== "https:" ||
        url.hostname !== "english.visitkorea.or.kr" ||
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

  function renderResult(place) {
    const card = document.createElement("article");
    card.className = "places-result";

    const heading = document.createElement("div");
    heading.className = "places-result__heading";
    const name = document.createElement("h3");
    name.className = "places-result__name";
    name.textContent =
      typeof place.name === "string" ? place.name : "이름 정보 없음";
    const category = document.createElement("span");
    category.className = "places-result__category";
    category.textContent = CATEGORY_LABELS[place.category] || "장소";
    heading.append(name, category);
    card.appendChild(heading);

    const metadata = document.createElement("p");
    metadata.className = "places-result__meta";
    const values = [formatRating(place), formatReviews(place)];
    const distance = distanceKm(latestDestination, place);
    if (distance !== null) {
      values.push(`목적지에서 약 ${distance.toFixed(1)} km`);
    }
    metadata.textContent = values.join(" · ");
    card.appendChild(metadata);

    if (typeof place.address === "string" && place.address) {
      const address = document.createElement("p");
      address.className = "places-result__meta";
      address.textContent = place.address;
      card.appendChild(address);
    }

    const sourceLine = document.createElement("p");
    sourceLine.className = "places-result__source";
    sourceLine.textContent = `source: ${
      typeof place.source === "string" ? place.source : "unknown"
    }`;
    const sourceUrl = safeSourceUrl(place.source_url);
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

    if (validCoordinates(place)) {
      const waypointButton = document.createElement("button");
      waypointButton.type = "button";
      waypointButton.className = "button button--waypoint-add";
      waypointButton.textContent = "경유지에 추가";
      waypointButton.addEventListener("click", () => {
        window.dispatchEvent(
          new CustomEvent("kto:waypoint-added", {
            detail: {
              id: `place:${place.category || "place"}:${place.id || place.source_id || place.name}`,
              name: typeof place.name === "string" ? place.name : "선택한 장소",
              lat: place.lat,
              lng: place.lng,
              category: place.category || null,
              visit_duration_min: 60,
            },
          })
        );
      });
      card.appendChild(waypointButton);
    }
    return card;
  }

  function renderResults() {
    const container = getElement("places-results");
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (!state.results.length) {
      if (state.status === "success" || state.status === "partial") {
        const empty = document.createElement("p");
        empty.className = "places-empty";
        empty.textContent = "선택한 범위에서 공개된 장소를 찾지 못했습니다.";
        container.appendChild(empty);
      }
      return;
    }
    state.results.forEach((place) => container.appendChild(renderResult(place)));
  }

  function publishMarkers(categories = CATEGORIES, clearAll = false) {
    const changedCategories = categories.filter((category) =>
      CATEGORIES.includes(category)
    );
    const resultsByCategory = {};
    CATEGORIES.forEach((category) => {
      resultsByCategory[category] = state.resultsByCategory[category].slice();
    });
    const markers = changedCategories.flatMap((category) =>
      markersFor(state.resultsByCategory[category])
    );
    window.dispatchEvent(
      new CustomEvent("kto:places-results", {
        detail: {
          results: state.results.slice(),
          resultsByCategory,
          categories: changedCategories,
          markers,
          clearAll,
        },
      })
    );
  }

  function render() {
    const button = getElement("places-search");
    const summary = getElement("places-summary");
    if (!button || !summary) {
      return;
    }
    button.disabled =
      !latestDestination ||
      selectedCategories().length === 0 ||
      state.status === "loading";
    button.textContent =
      state.status === "loading" ? "주변 장소 확인 중…" : "주변 장소 검색";
    summary.textContent = latestDestination
      ? `${displayName(latestDestination)} 주변의 공개 장소를 확인합니다.`
      : "목적지를 선택하면 주변 공개 웹페이지에서 장소를 검색할 수 있습니다.";
    renderResults();
  }

  function clearResult(message) {
    state.requestId += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    state.status = "idle";
    CATEGORIES.forEach((category) => {
      state.resultsByCategory[category] = [];
    });
    replaceResults();
    state.response = null;
    state.error = null;
    publishMarkers(CATEGORIES, true);
    if (message) {
      setStatus(message, "active");
    }
    render();
  }

  function buildRequest() {
    const destination = latestDestination || currentDestination();
    const categories = selectedCategories();
    if (!destination) {
      setStatus("목적지를 먼저 선택해 주세요.", "error");
      return null;
    }
    if (!categories.length) {
      setStatus("맛집 또는 관광지를 하나 이상 선택해 주세요.", "error");
      return null;
    }
    return {
      destination: {
        lat: destination.lat,
        lng: destination.lng,
        label: destination.name || destination.label || "destination",
      },
      categories,
    };
  }

  function requestIsCurrent(requestId, destinationAtRequest, categoriesAtRequest) {
    return (
      requestId === state.requestId &&
      destinationAtRequest ===
        destinationFingerprint(latestDestination || currentDestination()) &&
      categoryFingerprint(categoriesAtRequest) ===
        categoryFingerprint(selectedCategories())
    );
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
    const categoriesAtRequest = request.categories.slice();
    const destinationAtRequest = destinationFingerprint(
      latestDestination || currentDestination()
    );
    activeController = new AbortController();
    state.status = "loading";
    state.response = null;
    state.error = null;
    render();
    setStatus("공개 웹페이지에서 주변 장소를 확인하고 있습니다.", "active");
    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
        signal: activeController.signal,
      });
      const payload = await response.json().catch(() => null);
      if (!requestIsCurrent(requestId, destinationAtRequest, categoriesAtRequest)) {
        return false;
      }
      if (!response.ok || !payload || !Array.isArray(payload.results)) {
        const issue = payload && Array.isArray(payload.issues) ? payload.issues[0] : null;
        throw new Error(
          issue && typeof issue.message === "string"
            ? issue.message
            : "장소 검색에 실패했습니다."
        );
      }
      const grouped = groupResults(payload.results);
      categoriesAtRequest.forEach((category) => {
        state.resultsByCategory[category] = grouped[category];
      });
      replaceResults();
      state.response = { ...payload, results: state.results.slice() };
      state.status = payload.complete ? "success" : "partial";
      render();
      publishMarkers(categoriesAtRequest);
      if (payload.complete) {
        setStatus(
          `${payload.results.length}개의 장소 후보를 확인했습니다${
            payload.cache_hit ? " (캐시)" : ""
          }.`,
          "success"
        );
      } else {
        const issue = Array.isArray(payload.issues) ? payload.issues[0] : null;
        setStatus(
          issue && typeof issue.message === "string"
            ? `일부 결과만 확인했습니다: ${issue.message}`
            : "일부 결과만 확인했습니다.",
          "partial"
        );
      }
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") {
        return false;
      }
      if (!requestIsCurrent(requestId, destinationAtRequest, categoriesAtRequest)) {
        return false;
      }
      categoriesAtRequest.forEach((category) => {
        state.resultsByCategory[category] = [];
      });
      replaceResults();
      state.status = "error";
      state.response = null;
      state.error = error instanceof Error ? error.message : String(error);
      render();
      publishMarkers(categoriesAtRequest);
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
    const destination =
      event && event.detail
        ? event.detail.destination || null
        : currentDestination();
    const changed =
      destinationFingerprint(destination) !== destinationFingerprint(latestDestination);
    latestDestination = destination;
    if (
      changed &&
      (state.results.length || state.status === "loading" || activeController)
    ) {
      clearResult("목적지가 변경되어 장소 결과를 초기화했습니다.");
      return;
    }
    render();
  }

  function handleCategoryChanged() {
    if (activeController) {
      activeController.abort();
      activeController = null;
      state.requestId += 1;
      state.status = "idle";
      state.response = null;
      state.error = null;
      setStatus("장소 종류가 변경되었습니다. 필요하면 다시 검색하세요.", "active");
    }
    render();
  }

  window.addEventListener("kto:selection-changed", handleSelectionChanged);

  window.KoreaTripPlaces = Object.freeze({
    search,
    clear: () => clearResult("장소 결과를 초기화했습니다."),
    getState: () => ({
      status: state.status,
      results: state.results.slice(),
      resultsByCategory: {
        restaurant: state.resultsByCategory.restaurant.slice(),
        attraction: state.resultsByCategory.attraction.slice(),
      },
      response: state.response,
      error: state.error,
    }),
    getMarkers: () => CATEGORIES.flatMap((category) =>
      markersFor(state.resultsByCategory[category])
    ),
  });

  document.addEventListener("DOMContentLoaded", () => {
    latestDestination = currentDestination();
    CATEGORIES.forEach((category) => {
      const checkbox = getElement(`places-category-${category}`);
      if (checkbox) {
        checkbox.addEventListener("change", handleCategoryChanged);
      }
    });
    const button = getElement("places-search");
    if (button) {
      button.addEventListener("click", search);
    }
    render();
  });
})();
