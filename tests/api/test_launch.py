"""Tests for the shared loopback token and server launch entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator.api.launch as launch
import orchestrator.api.loopback_token as loopback_token
import orchestrator.api.mcp as mcp
from orchestrator.platform.settings import Settings, check_paths_exist


def test_is_valid_token_accepts_only_nonempty_visible_ascii() -> None:
    assert not loopback_token.is_valid_token("")

    for codepoint in range(128):
        character = chr(codepoint)
        expected = 0x21 <= codepoint <= 0x7E
        assert loopback_token.is_valid_token(character) is expected, repr(character)

    assert not loopback_token.is_valid_token("é")
    assert not loopback_token.is_valid_token("snowman-☃")


def test_loopback_transport_validation_matches_token_predicate() -> None:
    candidates = ["", *(chr(codepoint) for codepoint in range(128)), "é", "token-☃"]

    for token in candidates:
        if loopback_token.is_valid_token(token):
            mcp.LoopbackTransport("http://127.0.0.1:8765", token, timeout=1)
        else:
            with pytest.raises(ValueError):
                mcp.LoopbackTransport("http://127.0.0.1:8765", token, timeout=1)


def test_mcp_token_env_is_the_shared_token_env_object() -> None:
    assert mcp.TOKEN_ENV is loopback_token.TOKEN_ENV


@pytest.mark.parametrize("value", [None, ""])
def test_read_token_reports_absent_or_empty_environment_token(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(loopback_token.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(loopback_token.TOKEN_ENV, value)

    with pytest.raises(loopback_token.TokenError) as raised:
        loopback_token.read_token(["--unrelated", "argument"])

    assert loopback_token.TOKEN_ENV in str(raised.value)


def test_read_token_checks_invalid_before_argv_and_never_echoes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "invalid-secret\n"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)

    with pytest.raises(loopback_token.TokenError) as raised:
        loopback_token.read_token([f"--token={token}"])

    message = str(raised.value)
    assert message == loopback_token.INVALID_TOKEN_MESSAGE
    assert loopback_token.TOKEN_ENV in message
    assert "ascii" in message.casefold()
    assert token not in message
    assert "command line" not in message.casefold()


def test_read_token_rejects_valid_token_in_argv_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "visible-secret-456"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)

    with pytest.raises(loopback_token.TokenError) as raised:
        loopback_token.read_token(["--option", f"value-{token}-suffix"])

    message = str(raised.value)
    assert loopback_token.TOKEN_ENV in message
    assert "command line" in message.casefold()
    assert "must not" in message.casefold()
    assert token not in message


def test_read_token_returns_valid_token_absent_from_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "visible-secret-789"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)

    assert loopback_token.read_token(["--project-root", "somewhere-else"]) == token


def _install_serve_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Settings, str]]:
    calls: list[tuple[Settings, str]] = []

    def record_serve(settings: Settings, token: str) -> None:
        calls.append((settings, token))

    monkeypatch.setattr(launch, "serve", record_serve)
    return calls


def test_launch_uses_project_root_and_does_not_print_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "launch-success-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)

    result = launch.main(["--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 0
    assert len(calls) == 1
    settings, served_token = calls[0]
    assert isinstance(settings, Settings)
    assert served_token == token
    assert settings.project_root == tmp_path
    assert token not in captured.out + captured.err


def test_launch_loads_settings_file_and_passes_server_address_to_serve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "launch-settings-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)

    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(
        f"project_root = {json.dumps(str(tmp_path))}\n"
        "\n[server]\n"
        'host = "127.0.0.1"\n'
        "port = 18765\n",
        encoding="utf-8",
    )

    # load_settings(..., verify_paths=True) also checks the configured Blender,
    # FFmpeg, ComfyUI, project-root, and optional model paths. Defaults match this
    # machine; skip explicitly if a checkout runs without those prerequisites.
    issues = check_paths_exist(Settings(project_root=tmp_path))
    if issues:
        pytest.skip("settings verify_paths prerequisites unavailable: " + "; ".join(map(str, issues)))

    result = launch.main(["--settings", str(settings_file)])
    captured = capsys.readouterr()

    assert result == 0
    assert len(calls) == 1
    settings, served_token = calls[0]
    assert isinstance(settings, Settings)
    assert served_token == token
    assert settings.project_root == tmp_path
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 18765
    assert token not in captured.out + captured.err


@pytest.mark.parametrize("case", ["missing", "invalid", "argv"])
def test_launch_token_errors_return_two_without_outputting_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    case: str,
) -> None:
    if case == "missing":
        token = None
        monkeypatch.delenv(loopback_token.TOKEN_ENV, raising=False)
        argv: list[str] = []
    elif case == "invalid":
        token = "invalid-launch-secret\r"
        monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
        argv = []
    else:
        token = "argv-launch-secret"
        monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
        argv = ["--project-root", str(tmp_path), token]

    calls = _install_serve_recorder(monkeypatch)
    result = launch.main(argv)
    captured = capsys.readouterr()

    assert result == 2
    assert calls == []
    assert captured.out == ""
    if token is not None:
        assert token not in captured.err
    if case == "missing":
        assert loopback_token.TOKEN_ENV in captured.err
        assert "secrets.token_hex(32)" in captured.err


def test_launch_missing_project_root_returns_two_without_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "missing-root-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)

    result = launch.main(["--project-root", str(tmp_path / "does-not-exist")])

    captured = capsys.readouterr()
    assert result == 2
    assert calls == []
    assert token not in captured.out + captured.err


def test_launch_malformed_settings_returns_two_without_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "malformed-settings-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)
    settings_file = tmp_path / "malformed.toml"
    settings_file.write_text("[server\nport = 18765\n", encoding="utf-8")

    result = launch.main(["--settings", str(settings_file)])

    captured = capsys.readouterr()
    assert result == 2
    assert calls == []
    assert token not in captured.out + captured.err


def test_launch_propagates_serve_exception_without_printing_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "serve-exception-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)

    def fail_serve(settings: Settings, served_token: str) -> None:
        raise RuntimeError("serve failed")

    monkeypatch.setattr(launch, "serve", fail_serve)

    with pytest.raises(RuntimeError, match="serve failed"):
        launch.main(["--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


def test_launch_project_root_override_precedes_settings_path_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "override-settings-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)

    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 18766\n",
        encoding="utf-8",
    )
    assert not (tmp_path / "projects").exists()

    issues = check_paths_exist(Settings(project_root=tmp_path))
    if issues:
        pytest.skip("settings verify_paths prerequisites unavailable: " + "; ".join(map(str, issues)))

    result = launch.main(["--settings", str(settings_file), "--project-root", str(tmp_path)])

    assert result == 0
    assert len(calls) == 1
    settings, served_token = calls[0]
    assert settings.project_root == tmp_path
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 18766
    assert served_token == token

    result = launch.main(
        ["--settings", str(settings_file), "--project-root", str(tmp_path / "missing-project")]
    )

    assert result == 2
    assert len(calls) == 1


def test_launch_verifies_binary_paths_from_settings_before_serving(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv(loopback_token.TOKEN_ENV, "path-verification-secret")
    calls = _install_serve_recorder(monkeypatch)

    missing_blender = tmp_path / "missing-blender.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_text("", encoding="utf-8")
    comfyui_root = tmp_path / "comfyui"
    comfyui_root.mkdir()
    settings_file = tmp_path / "paths.toml"
    settings_file.write_text(
        "[binaries]\n"
        f"blender_exe = {json.dumps(str(missing_blender))}\n"
        f"ffmpeg_exe = {json.dumps(str(ffmpeg))}\n"
        f"comfyui_root = {json.dumps(str(comfyui_root))}\n",
        encoding="utf-8",
    )

    result = launch.main(["--settings", str(settings_file), "--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert calls == []
    assert "binaries.blender_exe" in captured.err


def test_launch_redacts_token_from_settings_validation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "launch-host-validation-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    calls = _install_serve_recorder(monkeypatch)
    settings_file = tmp_path / "invalid-host.toml"
    settings_file.write_text(
        "[server]\n"
        f"host = {json.dumps(token)}\n",
        encoding="utf-8",
    )

    result = launch.main(["--settings", str(settings_file), "--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert result == 2
    assert calls == []
    assert captured.out == ""
    assert token not in captured.err
    assert "server.host" in captured.err


def test_mcp_main_redacts_token_from_settings_validation_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    token = "mcp-host-validation-secret"
    monkeypatch.setenv(loopback_token.TOKEN_ENV, token)
    settings_file = tmp_path / "invalid-host.toml"
    settings_file.write_text(
        "[server]\n"
        f"host = {json.dumps(token)}\n",
        encoding="utf-8",
    )

    result = mcp.main(["--settings", str(settings_file)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert token not in captured.err
    assert "server.host" in captured.err


def test_loopback_token_redact_replaces_every_occurrence_and_preserves_unmatched_text() -> None:
    token = "redact-this-secret"
    text = f"{token} first; {token} second; {token} third"

    assert loopback_token.redact(text, token) == (
        "<redacted token> first; <redacted token> second; <redacted token> third"
    )
    unchanged = "no secret appears in this text"
    assert loopback_token.redact(unchanged, "") == unchanged
    assert loopback_token.redact(unchanged, token) == unchanged
