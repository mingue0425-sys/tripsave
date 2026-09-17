(function () {
  "use strict";

  const SelectionMode = Object.freeze({
    NONE: "none",
    ORIGIN: "origin",
    DESTINATION: "destination",
  });
  const SAME_LOCATION_THRESHOLD_METERS = 20;
  const state = {
    origin: null,
    destination: null,
    selectionMode: SelectionMode.NONE,
  };
  let mapAvailable = true;

  function getStorage() {
    return window.KoreaTripStorage;
  }

  function getMarkerManager() {
    return window.KoreaTripMarkerManager;
  }

  function getElement(id) {
    return document.getElementById(id);
  }

  function formatCoordinate(lat, lng) {
    return `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
  }

  function slotTitle(slot) {
    return slot === "origin" ? "출발지" : "목적지";
  }

  function otherSlot(slot) {
    return slot === "origin" ? "destination" : "origin";
  }

  function cloneLocation(location) {
    const storage = getStorage();
    const normalized = storage ? storage.normalizeLocation(location) : null;
    return normalized ? { ...normalized } : null;
  }

  function getState() {
    return {
      origin: cloneLocation(state.origin),
      destination: cloneLocation(state.destination),
      selectionMode: state.selectionMode,
    };
  }

  function setStatus(message, statusType) {
    const element = getElement("selection-status");
    if (!element) {
      return;
    }
    element.textContent = message;
    element.classList.toggle("selection-status--error", statusType === "error");
    element.classList.toggle("selection-status--success", statusType === "success");
  }

  function publishState() {
    const currentState = getState();
    const markerManager = getMarkerManager();
    if (markerManager) {
      markerManager.sync(currentState);
    }
    window.dispatchEvent(
      new CustomEvent("kto:selection-changed", { detail: currentState })
    );
  }

  function saveState() {
    const storage = getStorage();
    return storage ? storage.saveSelectionState(state) : false;
  }

  function displayName(location) {
    if (!location) {
      return "선택 안 됨";
    }
    return location.name || location.label || formatCoordinate(location.lat, location.lng);
  }

  function sourceName(source) {
    return source === "local_search" ? "로컬 검색" : "지도 선택";
  }

  function renderLocation(slot) {
    const location = state[slot];
    const card = getElement(`${slot}-card`);
    const summary = getElement(`${slot}-summary`);
    const coordinate = getElement(`${slot}-coordinate`);
    const source = getElement(`${slot}-source`);
    const mapButton = getElement(`${slot}-map-button`);
    const removeButton = getElement(`${slot}-remove-button`);
    if (!card || !summary || !coordinate || !source || !mapButton || !removeButton) {
      return;
    }

    const isSelecting = state.selectionMode === slot;
    card.dataset.selected = location ? "true" : "false";
    card.dataset.selectionMode = isSelecting ? "active" : "inactive";
    summary.textContent = displayName(location);
    coordinate.hidden = !location;
    source.hidden = !location;
    if (location) {
      coordinate.textContent = `위도 ${location.lat.toFixed(6)} · 경도 ${location.lng.toFixed(6)}`;
      source.textContent = `입력: ${sourceName(location.source)}`;
    } else {
      coordinate.textContent = "";
      source.textContent = "";
    }
    mapButton.disabled = !mapAvailable;
    mapButton.textContent = isSelecting
      ? "선택 취소"
      : location
        ? "변경"
        : "지도에서 선택";
    mapButton.setAttribute("aria-pressed", String(isSelecting));
    removeButton.disabled = !location;
  }

  function renderControls() {
    renderLocation("origin");
    renderLocation("destination");
    const swapButton = getElement("swap-button");
    if (swapButton) {
      swapButton.disabled = !state.origin && !state.destination;
    }
  }

  function render() {
    renderControls();
    publishState();
  }

  function setMapAvailable(available) {
    mapAvailable = Boolean(available);
    renderControls();
    if (!mapAvailable && state.selectionMode !== SelectionMode.NONE) {
      state.selectionMode = SelectionMode.NONE;
      setStatus("지도를 사용할 수 없어 지도 선택 모드를 종료했습니다.", "error");
      publishState();
    }
  }

  function setSelectionMode(mode) {
    if (!Object.values(SelectionMode).includes(mode)) {
      return false;
    }
    if (mode !== SelectionMode.NONE && !mapAvailable) {
      setStatus("지도를 사용할 수 없습니다. basemap provider 연결을 확인하세요.", "error");
      return false;
    }
    state.selectionMode = mode;
    renderControls();
    publishState();
    if (mode === SelectionMode.NONE) {
      setStatus("선택 모드를 종료했습니다.", "active");
    } else {
      setStatus(`${slotTitle(mode)} 선택 중 — 지도에서 위치를 클릭하세요.`, "active");
    }
    return true;
  }

  function makeMapLocation(lat, lng) {
    const storage = getStorage();
    if (!storage) {
      return null;
    }
    return storage.normalizeLocation({
      lat,
      lng,
      label: formatCoordinate(lat, lng),
      source: "map",
    });
  }

  function validateAgainstOther(slot, location) {
    const otherLocation = state[otherSlot(slot)];
    const storage = getStorage();
    if (!storage || !otherLocation) {
      return true;
    }
    const distance = storage.haversineDistanceMeters(location, otherLocation);
    if (distance <= SAME_LOCATION_THRESHOLD_METERS) {
      setStatus(
        "출발지와 목적지가 동일하거나 너무 가깝습니다. 다른 위치를 선택하세요.",
        "error"
      );
      state.selectionMode = slot;
      renderControls();
      publishState();
      return false;
    }
    return true;
  }

  function setLocation(slot, rawLocation, successMessage) {
    const storage = getStorage();
    if (!storage || (slot !== "origin" && slot !== "destination")) {
      return false;
    }
    const location = storage.normalizeLocation(rawLocation);
    if (!location) {
      setStatus("위치 좌표가 유효하지 않습니다.", "error");
      return false;
    }
    if (!validateAgainstOther(slot, location)) {
      return false;
    }

    state[slot] = location;
    state.selectionMode = SelectionMode.NONE;
    const saved = saveState();
    render();
    if (window.KoreaTripSearch && typeof window.KoreaTripSearch.close === "function") {
      window.KoreaTripSearch.close(slot);
    }
    if (window.KoreaTripMap) {
      window.KoreaTripMap.updateCoordinateDisplay(location.lat, location.lng);
      const locations = [state.origin, state.destination].filter(Boolean);
      if (locations.length === 2) {
        window.KoreaTripMap.fitLocations(locations);
      } else if (location.source === "local_search") {
        // Search results may be outside the current viewport. A map click is
        // already visible, so preserve that viewport until the next choice.
        window.KoreaTripMap.flyToLocation(location);
      }
    }
    setStatus(
      saved ? successMessage : `${successMessage} (이 브라우저에 저장하지 못했습니다.)`,
      saved ? "success" : "error"
    );
    return true;
  }

  function setOrigin(location) {
    return setLocation("origin", location, "출발지가 설정되었습니다.");
  }

  function setDestination(location) {
    return setLocation("destination", location, "목적지가 설정되었습니다.");
  }

  function handleMapClick(location) {
    if (!location || typeof location !== "object") {
      setStatus("클릭한 좌표가 유효하지 않습니다.", "error");
      return false;
    }

    const normalized = makeMapLocation(location.lat, location.lng);
    if (!normalized) {
      setStatus("클릭한 좌표가 유효하지 않습니다.", "error");
      return false;
    }

    // Explicit selection mode always wins.
    if (state.selectionMode === SelectionMode.ORIGIN) {
      return setOrigin(normalized);
    }
    if (state.selectionMode === SelectionMode.DESTINATION) {
      return setDestination(normalized);
    }

    // Zero-click setup UX:
    // first ordinary map click = origin
    // second ordinary map click = destination
    if (!state.origin) {
      return setOrigin(normalized);
    }

    if (!state.destination) {
      return setDestination(normalized);
    }

    // Both slots are already populated. Avoid accidentally replacing a
    // location while the user is panning/clicking around the map.
    setStatus(
      "출발지와 목적지가 이미 설정되어 있습니다. 변경하려면 해당 위치의 변경 버튼을 사용하세요.",
      "active"
    );
    return false;
  }

  function removeLocation(slot) {
    if (slot !== "origin" && slot !== "destination") {
      return false;
    }
    state[slot] = null;
    if (state.selectionMode === slot) {
      state.selectionMode = SelectionMode.NONE;
    }
    const saved = saveState();
    render();
    setStatus(
      saved ? `${slotTitle(slot)}가 삭제되었습니다.` : `${slotTitle(slot)}를 삭제했지만 저장소를 갱신하지 못했습니다.`,
      saved ? "success" : "error"
    );
    return true;
  }

  function swap() {
    const origin = cloneLocation(state.origin);
    const destination = cloneLocation(state.destination);
    state.origin = destination;
    state.destination = origin;
    state.selectionMode = SelectionMode.NONE;
    const saved = saveState();
    render();
    setStatus(
      saved ? "출발지와 목적지를 교환했습니다." : "위치는 교환했지만 저장소를 갱신하지 못했습니다.",
      saved ? "success" : "error"
    );
    return true;
  }

  function clear() {
    state.origin = null;
    state.destination = null;
    state.selectionMode = SelectionMode.NONE;
    const storage = getStorage();
    const cleared = storage ? storage.clearSelectionState() : false;
    render();
    setStatus(
      cleared ? "출발지와 목적지를 모두 초기화했습니다." : "화면은 초기화했지만 저장된 상태를 삭제하지 못했습니다.",
      cleared ? "success" : "error"
    );
    return cleared;
  }

  function restore() {
    const storage = getStorage();
    if (!storage) {
      return;
    }
    const result = storage.loadSelectionState();
    if (result.state) {
      state.origin = result.state.origin;
      state.destination = result.state.destination;
    }
    render();
    if (result.reason === "invalid") {
      setStatus("저장된 위치 데이터가 손상되어 초기화했습니다.", "error");
    } else if (result.reason === "unavailable") {
      setStatus("브라우저 저장소를 사용할 수 없습니다. 새 선택은 이번 화면에서만 유지됩니다.", "error");
    } else if (result.state) {
      setStatus("저장된 출발지와 목적지를 복원했습니다.", "success");
    }
  }

  function bindControls() {
    ["origin", "destination"].forEach((slot) => {
      getElement(`${slot}-map-button`).addEventListener("click", () => {
        setSelectionMode(
          state.selectionMode === slot ? SelectionMode.NONE : slot
        );
      });
      getElement(`${slot}-remove-button`).addEventListener("click", () => {
        removeLocation(slot);
      });
      getElement(`${slot}-search-toggle`).addEventListener("click", () => {
        if (state.selectionMode !== SelectionMode.NONE) {
          setSelectionMode(SelectionMode.NONE);
        }
        if (window.KoreaTripSearch) {
          window.KoreaTripSearch.toggle(slot);
        }
      });
    });
    getElement("swap-button").addEventListener("click", swap);
    getElement("clear-button").addEventListener("click", clear);
  }

  window.addEventListener("kto:map-ready", () => setMapAvailable(true));
  window.addEventListener("kto:map-error", () => setMapAvailable(false));

  window.KoreaTripSelection = Object.freeze({
    SelectionMode,
    SAME_LOCATION_THRESHOLD_METERS,
    getState,
    getOrigin: () => cloneLocation(state.origin),
    getDestination: () => cloneLocation(state.destination),
    setOrigin,
    setDestination,
    setSelectionMode,
    handleMapClick,
    removeOrigin: () => removeLocation("origin"),
    removeDestination: () => removeLocation("destination"),
    swap,
    clear,
  });

  document.addEventListener("DOMContentLoaded", () => {
    mapAvailable = document.body.dataset.mapAvailable !== "false";
    bindControls();
    restore();
  });
})();
