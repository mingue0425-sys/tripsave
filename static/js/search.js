(function () {
  "use strict";

  const config = window.KTO_CONFIG || {};
  const slots = ["origin", "destination"];
  const requestSerial = { origin: 0, destination: 0 };

  function getElement(id) {
    return document.getElementById(id);
  }

  function formatCoordinate(place) {
    return `${place.lat.toFixed(6)}, ${place.lng.toFixed(6)}`;
  }

  function setPanelVisibility(slot, visible) {
    const panel = getElement(`${slot}-search-panel`);
    if (!panel) {
      return;
    }
    panel.hidden = !visible;
  }

  function clearResults(slot) {
    const results = getElement(`${slot}-search-results`);
    if (results) {
      results.replaceChildren();
    }
  }

  function close(slot) {
    const input = getElement(`${slot}-search-input`);
    setPanelVisibility(slot, false);
    clearResults(slot);
    if (input) {
      input.value = "";
    }
  }

  function closeOtherPanels(activeSlot) {
    slots.filter((slot) => slot !== activeSlot).forEach(close);
  }

  function toggle(slot) {
    const panel = getElement(`${slot}-search-panel`);
    const input = getElement(`${slot}-search-input`);
    if (!panel || !input) {
      return;
    }
    const willOpen = panel.hidden;
    closeOtherPanels(slot);
    setPanelVisibility(slot, willOpen);
    if (willOpen) {
      input.focus();
    } else {
      clearResults(slot);
    }
  }

  function renderMessage(slot, message, className) {
    const results = getElement(`${slot}-search-results`);
    if (!results) {
      return;
    }
    clearResults(slot);
    const item = document.createElement("li");
    item.className = className || "search-result-message";
    item.textContent = message;
    item.setAttribute("role", "option");
    results.appendChild(item);
  }

  function selectPlace(slot, place) {
    const location = {
      id: place.id,
      name: place.name,
      aliases: Array.isArray(place.aliases) ? place.aliases.slice() : [],
      lat: place.lat,
      lng: place.lng,
      label: place.name,
      source: "local_search",
      type: place.type || "city",
    };
    const selection = window.KoreaTripSelection;
    if (!selection) {
      return;
    }
    const succeeded =
      slot === "origin"
        ? selection.setOrigin(location)
        : selection.setDestination(location);
    if (succeeded) {
      close(slot);
    }
  }

  function renderResults(slot, places) {
    const results = getElement(`${slot}-search-results`);
    if (!results) {
      return;
    }
    clearResults(slot);
    if (places.length === 0) {
      renderMessage(slot, "검색 결과가 없습니다.", "search-result-message");
      return;
    }
    places.forEach((place) => {
      const item = document.createElement("li");
      item.setAttribute("role", "option");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "search-result";
      button.addEventListener("click", () => selectPlace(slot, place));
      const name = document.createElement("strong");
      name.textContent = place.name;
      const coordinate = document.createElement("small");
      coordinate.textContent = formatCoordinate(place);
      button.append(name, coordinate);
      item.appendChild(button);
      results.appendChild(item);
    });
  }

  async function search(slot, rawQuery) {
    const query = rawQuery.trim();
    const serial = ++requestSerial[slot];
    if (!query) {
      clearResults(slot);
      return;
    }
    if (!config.placesSearchUrl) {
      renderMessage(slot, "로컬 검색 경로가 설정되지 않았습니다.", "search-result-message search-result-message--error");
      return;
    }
    renderMessage(slot, "검색 중…", "search-result-message");
    try {
      const url = `${config.placesSearchUrl}?q=${encodeURIComponent(query)}&limit=8`;
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`Local place search failed with HTTP ${response.status}.`);
      }
      const payload = await response.json();
      if (serial !== requestSerial[slot]) {
        return;
      }
      renderResults(slot, Array.isArray(payload.results) ? payload.results : []);
    } catch (error) {
      if (serial !== requestSerial[slot]) {
        return;
      }
      console.error("Unable to search local places:", error);
      renderMessage(
        slot,
        "로컬 검색을 사용할 수 없습니다.",
        "search-result-message search-result-message--error"
      );
    }
  }

  function bindSlot(slot) {
    const input = getElement(`${slot}-search-input`);
    const closeButton = getElement(`${slot}-search-close`);
    if (!input || !closeButton) {
      return;
    }
    input.addEventListener("input", () => search(slot, input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        close(slot);
        return;
      }
      if (event.key === "Enter") {
        const firstResult = getElement(`${slot}-search-results`)?.querySelector("button");
        if (firstResult) {
          event.preventDefault();
          firstResult.click();
        }
      }
    });
    closeButton.addEventListener("click", () => close(slot));
  }

  window.KoreaTripSearch = Object.freeze({
    toggle,
    close,
    search,
  });

  document.addEventListener("DOMContentLoaded", () => {
    slots.forEach(bindSlot);
  });
})();
