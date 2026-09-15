/* global maplibregl */

(function () {
  "use strict";

  const CATEGORIES = Object.freeze([
    "accommodation",
    "restaurant",
    "attraction",
  ]);
  const CATEGORY_LABELS = Object.freeze({
    accommodation: "숙소",
    restaurant: "맛집",
    attraction: "관광지",
  });
  const CATEGORY_LETTERS = Object.freeze({
    accommodation: "H",
    restaurant: "R",
    attraction: "A",
  });
  const state = {
    accommodation: { items: [], markers: [] },
    restaurant: { items: [], markers: [] },
    attraction: { items: [], markers: [] },
  };
  let mapInstance = null;

  function validCategory(category) {
    return CATEGORIES.includes(category);
  }

  function validCoordinates(item) {
    return (
      item &&
      Number.isFinite(item.lat) &&
      Number.isFinite(item.lng) &&
      item.lat >= -90 &&
      item.lat <= 90 &&
      item.lng >= -180 &&
      item.lng <= 180
    );
  }

  function removeMarkers(category) {
    state[category].markers.forEach((marker) => marker.remove());
    state[category].markers = [];
  }

  function displayValue(value, fallback) {
    return typeof value === "string" && value ? value : fallback;
  }

  function createMarkerElement(category, item) {
    const element = document.createElement("div");
    element.className = `kto-place-pin kto-place-pin--${category}`;
    element.setAttribute("role", "img");
    const label = `${CATEGORY_LABELS[category]}: ${displayValue(
      item.name,
      "이름 정보 없음"
    )}`;
    element.setAttribute("aria-label", label);
    element.title = label;

    const letter = document.createElement("span");
    letter.className = "kto-place-pin__letter";
    letter.textContent = CATEGORY_LETTERS[category];
    element.appendChild(letter);
    return element;
  }

  function popupText(category, item) {
    const lines = [
      displayValue(item.name, "이름 정보 없음"),
      CATEGORY_LABELS[category],
    ];
    if (typeof item.address === "string" && item.address) {
      lines.push(item.address);
    }
    if (typeof item.rating === "number" && typeof item.rating_scale === "number") {
      lines.push(`평점 ${item.rating.toFixed(1)} / ${item.rating_scale}`);
    }
    if (typeof item.review_count === "number") {
      lines.push(`리뷰 ${new Intl.NumberFormat("ko-KR").format(item.review_count)}개`);
    }
    if (typeof item.price_label === "string" && item.price_label) {
      lines.push(item.price_label);
    }
    return lines.join("\n");
  }

  function renderCategory(category) {
    removeMarkers(category);
    if (!mapInstance || !window.maplibregl || !validCategory(category)) {
      return;
    }
    state[category].items.filter(validCoordinates).forEach((item) => {
      const marker = new maplibregl.Marker({
        anchor: "bottom",
        element: createMarkerElement(category, item),
      })
        .setLngLat([item.lng, item.lat])
        .addTo(mapInstance);
      if (typeof maplibregl.Popup === "function") {
        marker.setPopup(
          new maplibregl.Popup({ offset: 24, closeButton: true }).setText(
            popupText(category, item)
          )
        );
      }
      state[category].markers.push(marker);
    });
  }

  function setPlaces(category, items) {
    if (!validCategory(category)) {
      return 0;
    }
    state[category].items = Array.isArray(items)
      ? items.filter((item) => item && typeof item === "object").slice()
      : [];
    renderCategory(category);
    return state[category].items.length;
  }

  function clearPlaces(category) {
    if (!validCategory(category)) {
      return;
    }
    state[category].items = [];
    removeMarkers(category);
  }

  function clearAll() {
    CATEGORIES.forEach(clearPlaces);
  }

  function setMapInstance(map) {
    mapInstance = map || null;
    CATEGORIES.forEach(renderCategory);
  }

  function itemsFromDetail(detail, category) {
    if (detail && detail.resultsByCategory) {
      return detail.resultsByCategory[category] || [];
    }
    if (detail && Array.isArray(detail.markers)) {
      return detail.markers.filter((item) => item.category === category);
    }
    if (detail && Array.isArray(detail.results)) {
      return detail.results.filter((item) => item.category === category);
    }
    return [];
  }

  function handlePlacesResults(event) {
    const detail = event && event.detail ? event.detail : {};
    const categories = Array.isArray(detail.categories)
      ? detail.categories.filter(validCategory)
      : ["restaurant", "attraction"];
    if (detail.clearAll) {
      clearAll();
    }
    categories.forEach((category) => {
      setPlaces(category, itemsFromDetail(detail, category));
    });
  }

  function handleAccommodationResults(event) {
    const detail = event && event.detail ? event.detail : {};
    if (detail.clearAll) {
      clearPlaces("accommodation");
    }
    setPlaces("accommodation", itemsFromDetail(detail, "accommodation"));
  }

  window.addEventListener("kto:map-ready", (event) => {
    setMapInstance(event && event.detail ? event.detail.map : null);
  });
  window.addEventListener("kto:places-results", handlePlacesResults);
  window.addEventListener("kto:accommodation-results", handleAccommodationResults);

  window.KoreaTripPlaceLayers = Object.freeze({
    setMapInstance,
    setPlaces,
    clearPlaces,
    clearAll,
    getPlaces: (category) =>
      validCategory(category) ? state[category].items.slice() : [],
    getMarkerCounts: () =>
      Object.fromEntries(
        CATEGORIES.map((category) => [category, state[category].markers.length])
      ),
  });
})();
