from pathlib import Path

import pytest

from orchestrator.platform.settings import (
    Settings,
    SettingsError,
    load_settings,
    parse_settings,
)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return path


def keys(error: SettingsError) -> set[str]:
    return {issue.key for issue in error.issues}


def test_empty_file_yields_documented_defaults(tmp_path):
    s = load_settings(write(tmp_path, ""), verify_paths=False)
    assert s.hosted_video_tier == 1
    assert s.server.host == "127.0.0.1"
    assert s.upload.max_image_bytes == 25 * 1024 * 1024
    assert s.upload.max_image_long_edge_px == 8192
    assert s.upload.max_video_bytes == 500 * 1024 * 1024
    assert s.upload.max_video_seconds == 120
    assert s.retention.hosted_metadata_max_age_days == 30


def test_every_prd_listed_area_has_a_named_section():
    fields = set(Settings.model_fields)
    assert {"project_root", "hosted_video_tier", "binaries", "models", "upload",
            "budget", "retention", "resources", "credentials", "server"} <= fields


def test_every_leaf_setting_has_a_default():
    # A missing key must never be an error: every configurable resolves to a default.
    assert parse_settings({}).model_dump()


def test_overrides_are_applied(tmp_path):
    s = load_settings(
        write(tmp_path, "hosted_video_tier = 2\n[upload]\nmax_image_bytes = 1000\n[budget]\nretries_per_shot = 5\n"),
        verify_paths=False,
    )
    assert (s.hosted_video_tier, s.upload.max_image_bytes, s.budget.retries_per_shot) == (2, 1000, 5)


def test_invalid_value_names_the_key(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, "hosted_video_tier = 4\n[upload]\nmax_image_bytes = -1\n"), verify_paths=False)
    assert keys(caught.value) == {"hosted_video_tier", "upload.max_image_bytes"}
    text = str(caught.value)
    assert "hosted_video_tier" in text and "upload.max_image_bytes" in text
    assert "Traceback" not in text


def test_wrong_type_names_the_key(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, '[binaries]\ncomfyui_port = "eighty"\n'), verify_paths=False)
    assert keys(caught.value) == {"binaries.comfyui_port"}


def test_port_range(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, "[server]\nport = 70000\n"), verify_paths=False)
    assert keys(caught.value) == {"server.port"}


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com"])
def test_non_loopback_bind_refused(tmp_path, host):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, f'[server]\nhost = "{host}"\n'), verify_paths=False)
    assert keys(caught.value) == {"server.host"}
    assert "127.0.0.1" in str(caught.value)


def test_loopback_variants_accepted(tmp_path):
    for host in ("127.0.0.1", "::1", "localhost"):
        load_settings(write(tmp_path, f'[server]\nhost = "{host}"\n'), verify_paths=False)


def test_cross_field_rule(tmp_path):
    with pytest.raises(SettingsError, match="min_free_disk_gb"):
        load_settings(write(tmp_path, "[budget]\ndisk_ceiling_gb = 5\nmin_free_disk_gb = 9\n"), verify_paths=False)


def test_metadata_retention_cannot_exceed_30_days(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, "[retention]\nhosted_metadata_max_age_days = 90\n"), verify_paths=False)
    assert keys(caught.value) == {"retention.hosted_metadata_max_age_days"}


def test_unknown_key_rejected_so_secrets_cannot_hide(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, 'api_key = "sk-secret"\n'), verify_paths=False)
    assert keys(caught.value) == {"api_key"}
    assert "sk-secret" not in str(caught.value)


def test_credentials_must_be_env_var_names_not_secrets(tmp_path):
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, '[credentials]\nyoutube = "AIza-this is a secret!"\n'), verify_paths=False)
    assert keys(caught.value) == {"credentials"}
    assert "AIza-this" not in str(caught.value)


def test_credential_resolves_from_environment(tmp_path, monkeypatch):
    s = load_settings(write(tmp_path, '[credentials]\nyoutube = "YT_KEY"\n'), verify_paths=False)
    with pytest.raises(KeyError, match="YT_KEY"):
        s.resolve_credential("youtube")
    monkeypatch.setenv("YT_KEY", "value")
    assert s.resolve_credential("youtube") == "value"
    with pytest.raises(KeyError, match="No credential configured"):
        s.resolve_credential("other")


def test_secrets_not_in_dumped_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_KEY", "super-secret")
    s = load_settings(write(tmp_path, '[credentials]\nyoutube = "YT_KEY"\n'), verify_paths=False)
    s.resolve_credential("youtube")
    assert "super-secret" not in s.model_dump_json()


def test_invalid_toml_is_actionable(tmp_path):
    with pytest.raises(SettingsError, match="not valid TOML"):
        load_settings(write(tmp_path, "this is = = bad"), verify_paths=False)


def test_missing_file(tmp_path):
    with pytest.raises(SettingsError, match="not found"):
        load_settings(tmp_path / "nope.toml", verify_paths=False)


def test_missing_paths_reported_at_startup_with_keys(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    text = (
        f'project_root = "{project.as_posix()}"\n'
        f'[binaries]\nblender_exe = "{(tmp_path / "no_blender.exe").as_posix()}"\n'
        f'ffmpeg_exe = "{(tmp_path / "no_ffmpeg.exe").as_posix()}"\n'
        f'comfyui_root = "{(tmp_path / "no_comfy").as_posix()}"\n'
    )
    with pytest.raises(SettingsError) as caught:
        load_settings(write(tmp_path, text))
    assert keys(caught.value) == {"binaries.blender_exe", "binaries.ffmpeg_exe", "binaries.comfyui_root"}


def test_valid_paths_pass_verification(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "comfy").mkdir()
    blender = tmp_path / "blender.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    blender.write_bytes(b"")
    ffmpeg.write_bytes(b"")
    text = (
        f'project_root = "{(tmp_path / "proj").as_posix()}"\n'
        f'[binaries]\nblender_exe = "{blender.as_posix()}"\nffmpeg_exe = "{ffmpeg.as_posix()}"\n'
        f'comfyui_root = "{(tmp_path / "comfy").as_posix()}"\n'
    )
    assert load_settings(write(tmp_path, text)).project_root == tmp_path / "proj"
