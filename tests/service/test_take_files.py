"""Contract tests for cleanup of generated voice takes and their reports."""

from __future__ import annotations

import json

from orchestrator.platform.containment import ProjectRoot
from orchestrator.service.take_files import discard_take, mark_report_discarded


def test_discard_take_removes_complete_and_partial_files_and_reports_complete_take(
    tmp_path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    root = ProjectRoot(project_path)
    directory = "generated/perfedit/s1/v0001/run-1"
    root.write_bytes(f"{directory}/take.wav", b"complete take")
    root.write_bytes(f"{directory}/take.partial.wav", b"partial take")

    had_complete_take = discard_take(root, directory)

    assert had_complete_take is True
    assert not root.resolve(f"{directory}/take.wav").exists()
    assert not root.resolve(f"{directory}/take.partial.wav").exists()

    root.write_bytes(f"{directory}/take.partial.wav", b"partial only")
    had_complete_take = discard_take(root, directory)

    assert had_complete_take is False
    assert not root.resolve(f"{directory}/take.partial.wav").exists()
    assert discard_take(root, directory) is False


def test_mark_report_discarded_updates_existing_report_and_ignores_absent_report(
    tmp_path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    root = ProjectRoot(project_path)
    report_rel = "generated/perfedit/s1/v0001/run-2/report.json"
    original = {"status": "running", "algorithm_version": "1.0.0", "notes": ["kept"]}
    root.write_bytes(report_rel, json.dumps(original).encode("utf-8"))

    mark_report_discarded(root, report_rel, status="cancelled")

    report = json.loads(root.read_text(report_rel, encoding="utf-8"))
    assert report == {
        "status": "cancelled",
        "algorithm_version": "1.0.0",
        "notes": ["kept"],
        "take_discarded": True,
    }

    missing_report = "generated/perfedit/s1/v0001/run-3/report.json"
    mark_report_discarded(root, missing_report, status="failed")
    assert not root.resolve(missing_report).exists()
