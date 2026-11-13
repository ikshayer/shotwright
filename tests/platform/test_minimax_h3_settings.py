"""Independent H3 admission policy tests, authored against CONTRACT.md."""
import pytest

from orchestrator.platform.settings import Settings, SettingsError, parse_settings

DEMANDS = {'required_vram_gb': 8.0, 'required_ram_gb': 10.0,
               'required_disk_gb': 3.0, 'throughput_floor_per_second': 0.01}


def test_h3_default_is_disabled_and_has_no_invented_capacity():
    settings = Settings()
    h3 = settings.minimax_h3
    assert h3.enabled is False
    for key in DEMANDS:
        assert getattr(h3, key) is None
    assert h3.startup_allowance_seconds == 600
    assert h3.execution_timeout_seconds == 1800
    assert h3.wall_clock_seconds == 2400


def test_enabled_requires_each_explicit_reservation():
    for absent in DEMANDS:
        values = {k: v for k, v in DEMANDS.items() if k != absent}
        with pytest.raises(SettingsError):
            parse_settings({'minimax_h3': {'enabled': True, **values}})


@pytest.mark.parametrize('key', DEMANDS)
@pytest.mark.parametrize('bad', [0, -1, True, '8', float('inf'), float('nan')])
def test_reservations_are_finite_strict_positive(key, bad):
    with pytest.raises(SettingsError):
        parse_settings({'minimax_h3': {'enabled': True, **DEMANDS, key: bad}})


def test_explicit_h3_policy_does_not_change_base_budgets():
    baseline = Settings()
    configured = parse_settings({'minimax_h3': {'enabled': True, **DEMANDS}})
    assert configured.minimax_h3.enabled is True
    assert configured.budget == baseline.budget
    assert configured.resources == baseline.resources
