"""V1.0 recommendation contracts.

The ranking layer consumes assembled ``TripCandidate`` objects.  It never
fetches source data and it keeps preference score, evidence confidence, and
exclusion reasons as separate values.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.trips.models import TripCandidate

RANKING_VERSION = "v1.0.0"
FEATURE_NAMES = ("cost", "accommodation", "restaurant", "attraction", "driving")


class RecommendationMode(str, Enum):
    BALANCED = "balanced"
    LOWEST_COST = "lowest_cost"
    VALUE = "value"
    ACCOMMODATION_QUALITY = "accommodation_quality"
    SIGHTSEEING = "sightseeing"
    LOW_DRIVING = "low_driving"
    CUSTOM = "custom"


class RecommendationWeights(BaseModel):
    """Non-negative user preference weights before normalization."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cost: float = Field(default=0.0, ge=0.0, le=1.0)
    accommodation: float = Field(default=0.0, ge=0.0, le=1.0)
    restaurant: float = Field(default=0.0, ge=0.0, le=1.0)
    attraction: float = Field(default=0.0, ge=0.0, le=1.0)
    driving: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_non_zero(self) -> RecommendationWeights:
        if sum(self.model_dump().values()) <= 0.0:
            raise ValueError("at least one recommendation weight must be positive")
        return self

    def normalized(self) -> dict[str, float]:
        values = self.model_dump()
        total = sum(values.values())
        return {name: value / total for name, value in values.items()}


class RecommendationFeatureScores(BaseModel):
    """Comparable 0..1 feature scores; null means unavailable, not zero."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cost_score: float | None = Field(default=None, ge=0.0, le=1.0)
    accommodation_score: float | None = Field(default=None, ge=0.0, le=1.0)
    restaurant_score: float | None = Field(default=None, ge=0.0, le=1.0)
    attraction_score: float | None = Field(default=None, ge=0.0, le=1.0)
    driving_score: float | None = Field(default=None, ge=0.0, le=1.0)
    data_confidence_score: float = Field(ge=0.0, le=1.0)

    def as_feature_map(self) -> dict[str, float | None]:
        return {
            "cost": self.cost_score,
            "accommodation": self.accommodation_score,
            "restaurant": self.restaurant_score,
            "attraction": self.attraction_score,
            "driving": self.driving_score,
        }


class Recommendation(BaseModel):
    """One explainable, eligible recommendation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rank: int = Field(strict=True, ge=1)
    candidate_id: str = Field(min_length=1, max_length=128)
    recommendation_mode: RecommendationMode
    final_score: float = Field(ge=0.0, le=1.0)
    feature_scores: RecommendationFeatureScores
    confidence: float = Field(ge=0.0, le=1.0)
    strengths: list[str] = Field(default_factory=list, max_length=20)
    weaknesses: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=50)
    eligible: bool = True
    exclusion_reasons: list[str] = Field(default_factory=list, max_length=20)
    explanation: str = Field(min_length=1, max_length=2_000)
    raw_features: dict[str, float | int | None] = Field(default_factory=dict)
    normalized_features: dict[str, float | None] = Field(default_factory=dict)
    weighted_contributions: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_eligibility(self) -> Recommendation:
        if not self.eligible and not self.exclusion_reasons:
            raise ValueError("an ineligible recommendation needs exclusion reasons")
        if self.eligible and self.exclusion_reasons:
            raise ValueError("an eligible recommendation cannot have exclusion reasons")
        if self.eligible and self.rank < 1:
            raise ValueError("eligible recommendations need a positive rank")
        return self


class ExcludedCandidate(BaseModel):
    """A candidate deliberately omitted from primary ranking."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    candidate_id: str = Field(min_length=1, max_length=128)
    reasons: list[str] = Field(min_length=1, max_length=20)


class RecommendationRankRequest(BaseModel):
    """Production ranking request addressed to one persisted candidate set."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    candidate_set_id: str = Field(min_length=1, max_length=128)
    mode: RecommendationMode = RecommendationMode.BALANCED
    candidate_ids: list[str] | None = Field(default=None, min_length=1, max_length=200)
    request_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    custom_weights: RecommendationWeights | None = None
    limit: int = Field(default=10, strict=True, ge=1, le=200)

    @model_validator(mode="after")
    def validate_candidate_selection(self) -> RecommendationRankRequest:
        if not self.candidate_set_id.strip():
            raise ValueError("candidate_set_id must not be blank")
        if self.candidate_ids is not None:
            if len(set(self.candidate_ids)) != len(self.candidate_ids):
                raise ValueError("candidate_ids must be unique")
            if any(not value.strip() for value in self.candidate_ids):
                raise ValueError("candidate_ids must not be blank")
        return self


class RecommendationDebugRankRequest(BaseModel):
    """Full-candidate ranking contract available only in explicit debug mode."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: RecommendationMode = RecommendationMode.BALANCED
    candidates: list[TripCandidate] = Field(default_factory=list, max_length=200)
    candidate_ids: list[str] | None = Field(default=None, min_length=1, max_length=200)
    custom_weights: RecommendationWeights | None = None
    limit: int = Field(default=10, strict=True, ge=1, le=200)

    @model_validator(mode="after")
    def validate_candidate_selection(self) -> RecommendationDebugRankRequest:
        if not self.candidates and not self.candidate_ids:
            raise ValueError("candidates or candidate_ids are required")
        if self.candidate_ids:
            if len(set(self.candidate_ids)) != len(self.candidate_ids):
                raise ValueError("candidate_ids must be unique")
            if any(not value.strip() for value in self.candidate_ids):
                raise ValueError("candidate_ids must not be blank")
            known = {candidate.id for candidate in self.candidates}
            missing = [value for value in self.candidate_ids if value not in known]
            if missing:
                raise ValueError("candidate_ids must refer to supplied candidates")
        ids = [candidate.id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        return self


class RankingResult(BaseModel):
    """Deterministic ranking response with explicit exclusions."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: RecommendationMode
    ranking_version: str = Field(default=RANKING_VERSION, min_length=1, max_length=40)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=200)
    excluded_candidates: list[ExcludedCandidate] = Field(default_factory=list, max_length=200)
    candidate_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    candidate_set_fingerprint: str = Field(min_length=64, max_length=64)
    candidate_set_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    weights: RecommendationWeights
    warnings: list[str] = Field(default_factory=list, max_length=50)
    generated_at: datetime

    @model_validator(mode="after")
    def validate_ranking_state(self) -> RankingResult:
        if (self.candidate_set_id is None) != (self.request_fingerprint is None):
            raise ValueError("candidate_set_id and request_fingerprint must be provided together")
        if self.eligible_count < len(self.recommendations):
            raise ValueError("eligible_count cannot be smaller than returned recommendations")
        ranks = [recommendation.rank for recommendation in self.recommendations]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("recommendation ranks must be sequential")
        ids = [recommendation.candidate_id for recommendation in self.recommendations]
        excluded_ids = [candidate.candidate_id for candidate in self.excluded_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("recommendation candidate IDs must be unique")
        if set(ids) & set(excluded_ids):
            raise ValueError("a candidate cannot be both recommended and excluded")
        if self.candidate_count < self.eligible_count + len(self.excluded_candidates):
            raise ValueError("candidate counts do not describe the result")
        for recommendation in self.recommendations:
            if recommendation.recommendation_mode is not self.mode:
                raise ValueError("recommendation mode must match result mode")
        return self


# A descriptive alias keeps API naming flexible without duplicating the model.
RecommendationResponse = RankingResult


__all__ = [
    "FEATURE_NAMES",
    "RANKING_VERSION",
    "ExcludedCandidate",
    "RankingResult",
    "Recommendation",
    "RecommendationDebugRankRequest",
    "RecommendationFeatureScores",
    "RecommendationMode",
    "RecommendationRankRequest",
    "RecommendationResponse",
    "RecommendationWeights",
]
