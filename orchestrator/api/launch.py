"""``cctv-pipeline-serve``: start the orchestrator API with the token from the environment.

The token is read from ``CCTV_PIPELINE_TOKEN`` through the same module the MCP adapter
uses. It is never generated or printed: a printed token lands in terminal scrollback and
logs.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from orchestrator.platform.containment import ContainmentError, ProjectRoot
from orchestrator.platform.settings import Settings, SettingsError, check_paths_exist, load_settings

from .app import serve
from .loopback_token import TOKEN_ENV, TokenError, read_token, redact

_TOKEN_HINT = (
    f'make one once with: python -c "import secrets; print(secrets.token_hex(32))" '
    f"and keep it in ${TOKEN_ENV}"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cctv-pipeline-serve",
        description=f"Serve the orchestrator API; the token comes from ${TOKEN_ENV}.",
    )
    parser.add_argument("--settings", type=Path, help="settings.toml")
    parser.add_argument("--project-root", type=Path, help="Existing project root; overrides settings.")
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        token = read_token(arguments)
    except TokenError as error:
        print(f"{error}\n{_TOKEN_HINT}", file=sys.stderr)
        return 2
    args = parser.parse_args(arguments)
    try:
        # Paths are checked after the override, not inside load_settings: otherwise a
        # file whose own project_root does not exist fails even when --project-root
        # replaces it.
        settings = load_settings(args.settings, verify_paths=False) if args.settings else Settings()
        if args.project_root is not None:
            settings = settings.model_copy(update={"project_root": args.project_root})
        if args.settings:
            issues = check_paths_exist(settings)
            if issues:
                raise SettingsError(str(args.settings), issues)
        # Through the one containment helper, so a missing root is reported here, before
        # uvicorn starts, rather than surfacing from deep inside create_app.
        ProjectRoot(settings.project_root)
    except (SettingsError, ContainmentError) as error:
        print(redact(str(error), token), file=sys.stderr)
        return 2
    # Outside the try: a failure while serving is a real failure and must propagate.
    serve(settings, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
