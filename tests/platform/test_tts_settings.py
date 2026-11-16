"""Settings contract for the opt-in local TTS engines."""

from __future__ import annotations

import math

import pytest

from orchestrator.platform.settings import Settings, SettingsError, parse_settings


INDEX_RESERVATIONS = {
    "required_vram_gb": 8.0,
    "required_ram_gb": 12.0,
    "required_disk_gb": 1.0,
}


def test_both_tts_sections_are_disabled_by_default() -> None:
    settings = Settings()

    assert settings.piper.enabled is False
    assert settings.indextts2.enabled is False


@pytest.mark.parametrize(
    "section",
    [
        {"piper": {"not_a_setting": 1}},
        {"indextts2": {"not_a_setting": 1}},
    ],
)
def test_tts_sections_reject_unknown_keys(section: dict[str, object]) -> None:
    with pytest.raises(SettingsError):
        parse_settings(section)


@pytest.mark.parametrize("absent", INDEX_RESERVATIONS)
def test_enabled_indextts2_requires_each_reservation(absent: str) -> None:
    reservations = {key: value for key, value in INDEX_RESERVATIONS.items() if key != absent}

    with pytest.raises(SettingsError):
        parse_settings({"indextts2": {"enabled": True, **reservations}})


@pytest.mark.parametrize("key", INDEX_RESERVATIONS)
@pytest.mark.parametrize(
    "bad",
    [True, "8", float("nan"), float("inf"), float("-inf"), 0, -0.1],
)
def test_indextts2_reservations_are_strict_finite_positive_numbers(
    key: str, bad: object
) -> None:
    values = {**INDEX_RESERVATIONS, key: bad}

    with pytest.raises(SettingsError):
        parse_settings({"indextts2": {"enabled": True, **values}})


@pytest.mark.parametrize(
    "section,key",
    [
        ("piper", "timeout_seconds"),
        ("indextts2", "startup_allowance_seconds"),
        ("indextts2", "execution_timeout_seconds"),
    ],
)
@pytest.mark.parametrize("timeout", [0, -0.1])
def test_tts_timeouts_must_be_greater_than_zero(
    section: str, key: str, timeout: float
) -> None:
    values: dict[str, object] = {key: timeout}
    if section == "indextts2":
        values.update(INDEX_RESERVATIONS)

    with pytest.raises(SettingsError):
        parse_settings({section: values})


def test_tts_timeout_defaults_are_positive() -> None:
    settings = Settings()

    assert math.isfinite(settings.piper.timeout_seconds)
    assert settings.piper.timeout_seconds > 0
    assert math.isfinite(settings.indextts2.startup_allowance_seconds)
    assert settings.indextts2.startup_allowance_seconds > 0
    assert math.isfinite(settings.indextts2.execution_timeout_seconds)
    assert settings.indextts2.execution_timeout_seconds > 0
