from scripts.run_native_scx_experiment import (
    background_retention_percent,
    comparison_is_fair,
)


def test_background_retention_percent_handles_baseline_and_zero() -> None:
    assert background_retention_percent(100, 25) == 25.0
    assert background_retention_percent(0, 25) == 0.0


def test_native_comparison_requires_background_retention_floor() -> None:
    assert comparison_is_fair(100, 25, minimum_percent=25.0)
    assert not comparison_is_fair(100, 24, minimum_percent=25.0)
