import pytest

from backend.recommendations.normalization import (
    normalize_higher_is_better,
    normalize_lower_is_better,
    renormalize_weights,
    saturating_count_score,
)


def test_robust_lower_and_higher_normalization_clips_outlier() -> None:
    values = [100_000, 110_000, 120_000, 2_000_000]

    assert normalize_lower_is_better(100_000, values) == 1.0
    assert normalize_lower_is_better(2_000_000, values) == 0.0
    assert normalize_higher_is_better(100_000, values) == 0.0
    assert normalize_higher_is_better(2_000_000, values) == 1.0


def test_constant_feature_is_neutral_and_null_stays_null() -> None:
    assert normalize_lower_is_better(100, [100, 100, 100]) == 0.5
    assert normalize_higher_is_better(100, [100]) == 0.5
    assert normalize_lower_is_better(None, [100]) is None


def test_count_saturation_is_not_linear() -> None:
    ten = saturating_count_score(10, scale=10)
    fifty = saturating_count_score(50, scale=10)
    hundred = saturating_count_score(100, scale=10)

    assert ten is not None and fifty is not None and hundred is not None
    assert 0.0 < ten < fifty < hundred <= 1.0
    assert fifty / ten < 5.0


def test_weight_renormalization_drops_only_unavailable_features() -> None:
    result = renormalize_weights(
        {"cost": 0.4, "restaurant": 0.1, "driving": 0.5},
        ["cost", "driving"],
    )

    assert result == {"cost": pytest.approx(4 / 9), "driving": pytest.approx(5 / 9)}
