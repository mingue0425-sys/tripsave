(function () {
  "use strict";

  function formatDistance(distanceM) {
    if (!Number.isFinite(distanceM) || distanceM < 0) {
      return "—";
    }
    if (distanceM < 1000) {
      return `${Math.round(distanceM)} m`;
    }
    return `${(distanceM / 1000).toFixed(1)} km`;
  }

  function formatDuration(durationS) {
    if (!Number.isFinite(durationS) || durationS < 0) {
      return "—";
    }
    const totalMinutes = Math.round(durationS / 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours === 0) {
      return `${minutes}분`;
    }
    if (minutes === 0) {
      return `${hours}시간`;
    }
    return `${hours}시간 ${String(minutes).padStart(2, "0")}분`;
  }

  window.KoreaTripRouteFormat = Object.freeze({
    formatDistance,
    formatDuration,
  });
})();
