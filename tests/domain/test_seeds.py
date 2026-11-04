"""FR-21 seed derivation."""

import pytest

from orchestrator.domain.seeds import (
    INT32_MAX,
    derive_seed,
    derive_unit_interval,
    jitter,
)


def test_int32_max_constant():
    assert INT32_MAX == 2**31 - 1


def test_deterministic_for_same_inputs():
    assert derive_seed("S1", 7, "noise") == derive_seed("S1", 7, "noise")


def test_golden_values_pin_the_algorithm():
    # AT-05: changing the derivation silently changes every previously rendered shot.
    assert derive_seed("S1", 7, "noise") == 1553908607
    assert derive_seed("S1", 7, "noise", max_value=255) == 127


@pytest.mark.parametrize(
    "other",
    [("S2", 7, "noise"), ("S1", 8, "noise"), ("S1", 7, "grain")],
)
def test_differs_when_any_input_changes(other):
    assert derive_seed("S1", 7, "noise") != derive_seed(*other)


def test_field_boundaries_do_not_collide():
    assert derive_seed("a", 11, "b") != derive_seed("a1", 1, "b")


def test_never_exceeds_int32_max_across_many_inputs():
    # Spike 01: FFmpeg `noise` fails the job on all_seed > INT32_MAX. An unclamped
    # 32-bit value exceeds it ~50% of the time, so many samples catch a missing clamp.
    seeds = [derive_seed(f"shot-{i}", i, "noise") for i in range(2000)]
    assert max(seeds) <= INT32_MAX
    assert min(seeds) >= 0
    assert max(seeds) > INT32_MAX // 2  # clamp is not squashing to a tiny range


def test_spike_01_failing_value_is_unreachable():
    assert 2574725062 > INT32_MAX
    assert all(derive_seed(f"s{i}", 0, "noise") != 2574725062 for i in range(500))


@pytest.mark.parametrize("max_value", [1, 7, 255, 1000, 65535, 2**24 - 1, INT32_MAX])
def test_never_exceeds_stated_maximum(max_value):
    for i in range(500):
        v = derive_seed(f"shot-{i}", i, "stage", max_value=max_value)
        assert 0 <= v <= max_value


def test_default_max_is_int32():
    assert all(derive_seed(f"s{i}", i, "x") <= INT32_MAX for i in range(200))


@pytest.mark.parametrize("bad", [0, -1, -INT32_MAX])
def test_nonpositive_max_value_rejected(bad):
    with pytest.raises(ValueError):
        derive_seed("S", 0, "x", max_value=bad)


def test_unit_interval_in_half_open_range_and_deterministic():
    vals = [derive_unit_interval(f"s{i}", i, "gap") for i in range(500)]
    assert all(0.0 <= v < 1.0 for v in vals)
    assert derive_unit_interval("s", 1, "gap") == derive_unit_interval("s", 1, "gap")
    assert len(set(vals)) > 450


def test_jitter_within_bounds_and_deterministic():
    for i in range(300):
        assert -3.0 <= jitter(f"s{i}", i, "gain", -3.0, 3.0) < 3.0
    assert jitter("s", 1, "g", 1.0, 2.0) == jitter("s", 1, "g", 1.0, 2.0)


def test_jitter_varies_with_inputs():
    assert jitter("a", 1, "g", 0, 10) != jitter("b", 1, "g", 0, 10)


def test_jitter_degenerate_range_returns_low():
    assert jitter("s", 1, "g", 2.5, 2.5) == 2.5


def test_jitter_rejects_inverted_range():
    with pytest.raises(ValueError):
        jitter("s", 1, "g", 2.0, 1.0)


def test_non_power_of_two_bound_is_well_distributed():
    # The old bitwise-AND derivation could only reach values whose bits were a
    # subset of the bound's, leaving most of [0, 1000] unreachable.
    values = {derive_seed(f"shot-{i}", i, "stage", max_value=1000) for i in range(3000)}
    assert len(values) > 900
    assert max(values) > 950
