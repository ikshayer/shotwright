"""FR-32 audio seed derivation contract."""

from __future__ import annotations

import hashlib
import importlib

import pytest

from orchestrator.domain.seeds import derive_seed


@pytest.fixture(scope="module")
def derive_audio_seed():
    """Resolve the new public seed helper at test time so the module still collects."""
    return getattr(importlib.import_module("orchestrator.domain.seeds"), "derive_audio_seed")


def test_audio_seed_is_deterministic_and_uses_the_audio_namespace(derive_audio_seed) -> None:
    parts = ("cctv-overdriven", "1.0.0", "noise", "take-001")
    seed = derive_audio_seed(*parts)

    assert seed == derive_audio_seed(*parts)
    expected = int(hashlib.sha256(("audio|" + "|".join(parts)).encode()).hexdigest()[:16], 16)
    assert seed == expected % (2**32)
    assert seed != derive_seed(*parts[:3])


@pytest.mark.parametrize(
    "changed",
    [
        ("cctv-overdriven-2", "1.0.0", "noise", "take-001"),
        ("cctv-overdriven", "1.0.1", "noise", "take-001"),
        ("cctv-overdriven", "1.0.0", "hum", "take-001"),
        ("cctv-overdriven", "1.0.0", "noise", "take-002"),
    ],
)
def test_audio_seed_changes_with_each_tuple_component(derive_audio_seed, changed) -> None:
    assert derive_audio_seed("cctv-overdriven", "1.0.0", "noise", "take-001") != derive_audio_seed(*changed)


def test_audio_seed_uses_the_full_unsigned_32_bit_range(derive_audio_seed) -> None:
    upper = 2**32 - 1
    seeds = [derive_audio_seed(f"profile-{i}", f"{i}.0.0", "noise", f"take-{i}") for i in range(2048)]

    assert all(0 <= seed <= upper for seed in seeds)
    assert min(seeds) < upper // 4
    assert max(seeds) > upper * 3 // 4
