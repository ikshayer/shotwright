"""Real-process checks for the CLI entry point (Amendment 3).

These tests deliberately import and call ``main`` in a child interpreter. Calling
``app`` through Click's test runner does not exercise the production stream/exit
handling described by the API contract.

Amendment 3 pins path parameters as named options (``--project-id``), the upload
spelling as ``--file``, and the entry point as ``python -m orchestrator.api``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
DOCUMENTED_ERROR_KEYS = {
    "kind",
    "retryable",
    "expected",
    "actual",
    "current",
    "limit",
    "observed",
    "remediation",
}


def _run_cli(project_root: Path, *arguments: str, extra_env: dict[str, str] | None = None):
    """Run the production CLI entry point with the repository importable."""
    env = os.environ.copy()
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + old_pythonpath if old_pythonpath else "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        # Invoked as `python -m orchestrator.api`, which is the module's real entry
        # point: it exits with main()'s status, exactly as the console script does.
        # Calling main() inline would discard that status and make every exit code 0.
        [
            sys.executable,
            "-m",
            "orchestrator.api",
            "--project-root",
            str(project_root),
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_missing_project_failure_is_json_on_stderr_and_nonzero(project_root: Path) -> None:
    result = _run_cli(project_root, "load-project", "--project-id", "does-not-exist", "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    body = json.loads(result.stderr)
    assert isinstance(body, dict)
    assert "does-not-exist" in result.stderr


def test_cli_error_carries_the_contract_shared_shape_fields(project_root: Path) -> None:
    result = _run_cli(project_root, "load-project", "--project-id", "does-not-exist", "--json")
    assert result.returncode != 0
    body = json.loads(result.stderr)

    # Amendment 1 names these shared fields. The contract does not enumerate any
    # additional fields (for example, a human-readable message), so do not forbid them.
    assert {"kind", "retryable"} <= set(body)
    assert set(body) & DOCUMENTED_ERROR_KEYS >= {"kind", "retryable"}


def test_unknown_command_is_a_usage_failure_on_stderr(project_root: Path) -> None:
    result = _run_cli(project_root, "no-such-operation")
    assert result.returncode != 0
    assert result.stderr.strip()


def test_help_succeeds_on_stdout(project_root: Path) -> None:
    result = _run_cli(project_root, "--help")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert result.stderr == ""


def test_successful_read_emits_json_only_on_stdout(project_root: Path) -> None:
    result = _run_cli(project_root, "list-projects", "--json")
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    assert result.stderr == ""


def test_upload_with_contradictory_magic_bytes_fails_and_names_problem(project_root: Path, tmp_path: Path) -> None:
    upload = tmp_path / "frame.png"
    upload.write_bytes(JPEG_BYTES)
    result = _run_cli(project_root, "create-asset", "--project-id", "p1", "--file", str(upload), "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "frame.png" in result.stderr or "magic" in result.stderr.lower() or "png" in result.stderr.lower()


def test_well_formed_upload_reaches_absent_adapter(project_root: Path, tmp_path: Path) -> None:
    upload = tmp_path / "frame.png"
    upload.write_bytes(PNG_BYTES)
    result = _run_cli(project_root, "create-asset", "--project-id", "p1", "--file", str(upload), "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "adapter" in result.stderr.lower()


@pytest.mark.parametrize("command", ["list-projects", "load-project"])
def test_loopback_token_is_not_leaked_to_cli_output(
    project_root: Path, command: str
) -> None:
    token = "cli-process-token-must-not-leak-7f93a1"
    arguments = (command, "--json") if command == "list-projects" else (command, "--project-id", "does-not-exist", "--json")
    result = _run_cli(
        project_root,
        *arguments,
        extra_env={"CCTV_LOOPBACK_TOKEN": token, "LOOPBACK_TOKEN": token},
    )
    assert token not in result.stdout
    assert token not in result.stderr
