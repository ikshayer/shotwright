"""Standalone CLI for advisory local Qwen analysis."""
from __future__ import annotations

import argparse
import json
import sys

from orchestrator.adapters.qwen import AnalysisRequest, QwenAdapter
from orchestrator.adapters.qwen.core import QwenRequestError, QwenSetupError


class _UsageError(ValueError):
    pass


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(prog="python -m orchestrator.adapters.qwen")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_JSONArgumentParser)
    for name in ("analyze", "review"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--media", required=True)
        brief = cmd.add_mutually_exclusive_group()
        brief.add_argument("--brief")
        brief.add_argument("--brief-file")
        cmd.add_argument("--mode", choices=("joint", "audio"), default="joint")
        cmd.add_argument("--start", type=float, default=0)
        cmd.add_argument("--duration", type=float)
        cmd.add_argument("--source-offset", type=float, default=0)
        cmd.add_argument("--window-seconds", type=float, default=25)
        cmd.add_argument("--overlap-seconds", type=float, default=2)
        cmd.add_argument("--max-new-tokens", type=int, default=500)
        cmd.add_argument("--dry-run", action="store_true")
    sub.add_parser("inspect", help="show pinned model and runtime files without CUDA or model loading")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print(json.dumps({"error": {"family": "request", "type": "usage", "message": str(exc)}}, ensure_ascii=True), file=sys.stderr)
        return 2
    if args.command == "inspect":
        from orchestrator.adapters.qwen.runner import BASE, ROOT, SPIKE
        model = ROOT / BASE / "model"
        print(json.dumps({"model": "Qwen/Qwen2.5-Omni-7B", "revision": "ae9e1690543ffd5c0221dc27f79834d0294cba00",
            "analyzer": f"{SPIKE}/analyze.py", "analyzer_exists": (ROOT / SPIKE / "analyze.py").is_file(),
            "isolated_python_exists": (ROOT / BASE / "env" / "Scripts" / "python.exe").is_file(),
            "model_directory_exists": model.is_dir(), "model_policy": "pinned offline local model; inspect does not load CUDA"}, indent=2))
        return 0
    try:
        brief = args.brief
        if args.brief_file:
            from orchestrator.platform.containment import ProjectRoot
            root = ProjectRoot(__import__("orchestrator.adapters.qwen.runner", fromlist=["ROOT"]).ROOT)
            raw = root.read_bytes(args.brief_file)
            brief = raw.decode("utf-8", errors="strict")
        purpose = "audio_review" if args.command == "review" else "reference"
        request = AnalysisRequest(media=args.media, purpose=purpose,
            brief=brief, modality=args.mode, start_seconds=args.start, duration_seconds=args.duration,
            source_offset_seconds=args.source_offset, window_seconds=args.window_seconds,
            overlap_seconds=args.overlap_seconds, max_new_tokens=args.max_new_tokens, dry_run=args.dry_run)
        report = QwenAdapter().run(request)
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 130 if report["status"] == "cancelled" else (0 if report["status"] in {"planned", "completed"} else 1)
    except (QwenRequestError, QwenSetupError, ValueError, OSError) as exc:
        print(json.dumps({"error": {"family": "startup_or_request", "type": type(exc).__name__, "message": str(exc)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())



