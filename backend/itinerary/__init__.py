"""Deterministic multi-stop route planning contracts and service."""

from .models import (
    ItineraryVehicle,
    MatrixEntry,
    OptimizationMode,
    OptimizedRoute,
    OptimizeRouteRequest,
    RoutePoint,
    RouteSegment,
    RouteWaypoint,
)
from .optimizer import ItineraryValidationError, optimize_matrix
from .service import ItineraryService, ItineraryServiceUnavailable

__all__ = [
    "ItineraryService",
    "ItineraryServiceUnavailable",
    "ItineraryValidationError",
    "ItineraryVehicle",
    "MatrixEntry",
    "OptimizationMode",
    "OptimizeRouteRequest",
    "OptimizedRoute",
    "RoutePoint",
    "RouteSegment",
    "RouteWaypoint",
    "optimize_matrix",
]
