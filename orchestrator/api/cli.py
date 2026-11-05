"""Non-interactive CLI over the same operation registry and dispatcher as HTTP."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from orchestrator.domain.state import ReviewApprovalError, TransitionError
from orchestrator.platform.containment import ContainmentError, ProjectRoot, UploadRejected
from orchestrator.platform.errors import JobError
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import VersionConflict

from .app import MissingAdapter, OperationContext, TokenConflict
from .routes import OPERATIONS


def command_names() -> tuple[str, ...]:
    """CLI operation names, derived from the shared route table."""
    return tuple(operation.name for operation in OPERATIONS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cctv-pipeline", description="CCTV production pipeline")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in OPERATIONS:
        subparser = subparsers.add_parser(operation.name, help=operation.summary)
        subparser.add_argument("--data", default="{}", help="JSON object passed to the operation")
        subparser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
        path_names = [part[1:-1] for part in operation.path.split("/")
                      if part.startswith("{") and part.endswith("}")]
        for path_name in path_names:
            subparser.add_argument(f"--{path_name.replace('_', '-')}", dest=path_name)
    return parser


def main(argv: Sequence[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = json.loads(args.data)
        if not isinstance(data, dict):
            raise TypeError("--data must decode to a JSON object")
        for key, value in vars(args).items():
            if key not in {"operation", "data", "json"} and value is not None:
                data[key] = value
        configured = settings or Settings()
        context = OperationContext(configured, ProjectRoot(configured.project_root))
        result = next(item for item in OPERATIONS if item.name == args.operation).invoke(context, data)
    except Exception as exc:  # noqa: BLE001 - CLI failures must always be reported with a nonzero exit.
        print(json.dumps(_error_payload(exc), ensure_ascii=False)
              if args.json else str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=_json_default))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _error_payload(exc: Exception) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if isinstance(exc, VersionConflict):
        details = {"expected": exc.expected, "actual": exc.actual,
                   "current": exc.current.model_dump(mode="json"), "record": exc.record}
    elif isinstance(exc, TokenConflict):
        details = {"expected": exc.expected, "actual": exc.actual}
    elif isinstance(exc, JobError):
        details = {"kind": exc.kind.value, "retryable": exc.retryable,
                   "remediation": exc.remediation}
    elif isinstance(exc, MissingAdapter):
        details = {"adapter": exc.adapter}
    elif isinstance(exc, (ContainmentError, UploadRejected)):
        details = {"field": "path" if isinstance(exc, ContainmentError) else "file"}
    elif isinstance(exc, (TransitionError, ReviewApprovalError)):
        details = {"reason": str(exc)}
    return {"error": type(exc).__name__, "message": str(exc), "details": details}


if __name__ == "__main__":
    raise SystemExit(main())
