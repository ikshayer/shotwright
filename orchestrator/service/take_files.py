"""Clean-up shared by every take-producing job (speech, audio post, performance edit).

A failed or cancelled run never leaves a `take.wav`, and a report written by a run that
finished just before it was cancelled must not go on claiming a take that was discarded.
"""

from __future__ import annotations

import json

from orchestrator.platform.containment import ProjectRoot

__all__ = ["discard_take", "mark_report_discarded"]


def discard_take(root: ProjectRoot, directory: str) -> bool:
    """Remove a run's `take.wav` (and any `take.partial.wav`). True if a take.wav existed."""
    found = False
    for name in ("take.wav", "take.partial.wav"):
        path = root.resolve(f"{directory}/{name}")
        if path.is_file():
            path.unlink()
            found = found or name == "take.wav"
    return found


def mark_report_discarded(root: ProjectRoot, report_rel: str, status: str) -> None:
    """Rewrite an existing report as `status` with `take_discarded: true`; no-op if absent."""
    if root.resolve(report_rel).is_file():
        report = json.loads(root.read_text(report_rel, encoding="utf-8"))
        report.update(status=status, take_discarded=True)
        root.write_text(report_rel, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
