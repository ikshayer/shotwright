"""Provider settings are bounded and never expose credential contents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.platform.settings import Settings, SettingsError, parse_settings


def test_provider_settings_defaults_are_operationally_bounded() -> None:
    configured = parse_settings({}).asset_providers
    assert configured.user_agent == "cctv-pipeline/0.1"
    assert configured.sketchfab_token_file is None
    assert configured.request_timeout_seconds == 30
    assert configured.max_attempts == 3
    assert configured.max_search_pages == 3
    assert configured.max_archive_files == 128


@pytest.mark.parametrize("value", ["other/1.0", "cctv-pipeline/1.0\rX", "cctv-pipeline/1.0\nX"])
def test_provider_user_agent_must_identify_application_and_reject_newlines(value: str) -> None:
    with pytest.raises(SettingsError):
        parse_settings({"asset_providers": {"user_agent": value}})


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_timeout_seconds", 0),
        ("request_timeout_seconds", float("inf")),
        ("max_attempts", 0),
        ("max_attempts", 6),
        ("max_search_pages", 0),
        ("max_search_pages", 4),
        ("max_archive_files", 0),
    ],
)
def test_provider_bounds_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(SettingsError):
        parse_settings({"asset_providers": {field: value}})


def test_provider_unknown_setting_is_rejected_without_echoing_values() -> None:
    secret = "never-print-this-token"
    with pytest.raises(SettingsError) as caught:
        parse_settings({"asset_providers": {"unexpected": secret}})
    assert secret not in str(caught.value)


def test_sketchfab_secret_is_not_part_of_settings_repr_or_serialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sf-private-token-value"
    token_file = tmp_path / "token.txt"
    token_file.write_text(secret, encoding="utf-8")
    settings = Settings(project_root=tmp_path, asset_providers={"sketchfab_token_file": token_file})
    # The path is configuration; the secret is resolved only at use time.
    dumped = settings.model_dump_json()
    assert secret not in dumped
    assert secret not in repr(settings)
    assert json.dumps(settings.model_dump(mode="json"))
