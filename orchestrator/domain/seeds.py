"""Seed derivation for FR-21's stochastic stages.

Determinism is a product feature (AT-05), so seeds are derived, never drawn:
the same `(shot_id, degrade_seed, stage)` always yields the same value.

The range clamp is not defensive tidiness. Spike 01 measured FFmpeg's `noise`
filter rejecting a derived 32-bit seed outright:

    Value 2574725062.000000 for parameter 'all_seed' out of range [-1 - 2.14748e+09]

It fails the job rather than silently ignoring the seed, which is the good
failure mode, but it means the accepted range differs per filter and belongs
with the stage definition rather than being assumed.
"""

from __future__ import annotations

import hashlib

#: FFmpeg's `noise` filter, and several others, cap at INT32_MAX.
INT32_MAX = 0x7FFFFFFF


def derive_seed(
    shot_id: str,
    degrade_seed: int,
    stage_name: str,
    max_value: int = INT32_MAX,
) -> int:
    """Derive a stage's seed, clamped to that filter's accepted range.

    `max_value` is the filter's documented maximum; pass the real one rather
    than relying on the default, because an out-of-range seed is a hard job
    failure.
    """
    if max_value < 1:
        raise ValueError(f"max_value must be positive, got {max_value}")
    material = f"{shot_id}|{degrade_seed}|{stage_name}".encode()
    # Modulo, not a bitwise AND. `x & max_value` happens to work when
    # max_value is 2^k-1 (INT32_MAX is), but for any other bound it can only
    # produce values whose bits are a subset of the bound's - with
    # max_value=1000 most of the range is unreachable. Take 16 hex digits so
    # modulo bias is negligible for any realistic filter range.
    digest = hashlib.sha256(material).hexdigest()[:16]
    return int(digest, 16) % (max_value + 1)


#: FFmpeg's `anoisesrc` seed accepts 0..UINT32_MAX.
UINT32_MAX = 0xFFFFFFFF


def derive_audio_seed(profile_name: str, semver: str, stage_name: str, line_id: str) -> int:
    """Derive an audio stage's seed from FR-32's `(profile, stage, line)` tuple.

    Follows `derive_seed`'s method (SHA-256 of a `|`-joined string, first 16 hex digits,
    modulo the range) with an `audio|` prefix so an audio seed cannot collide with a video
    seed, reduced modulo `UINT32_MAX + 1` and never otherwise wrapped or masked.
    """
    material = f"audio|{profile_name}|{semver}|{stage_name}|{line_id}".encode()
    return int(hashlib.sha256(material).hexdigest()[:16], 16) % (UINT32_MAX + 1)


def derive_unit_interval(shot_id: str, degrade_seed: int, stage_name: str) -> float:
    """A deterministic float in [0, 1) for jitter that is not a filter seed."""
    return derive_seed(shot_id, degrade_seed, stage_name, INT32_MAX) / (INT32_MAX + 1)


def jitter(
    shot_id: str,
    degrade_seed: int,
    stage_name: str,
    low: float,
    high: float,
) -> float:
    """Seeded value in [low, high). Used for gap and gain variation (FR-32)."""
    if high < low:
        raise ValueError(f"empty range: [{low}, {high})")
    return low + derive_unit_interval(shot_id, degrade_seed, stage_name) * (high - low)
