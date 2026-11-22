"""API/CLI parity (AT-10, FR-14), tool manifest and OpenAPI, all driven from the route table.

ASSUMED (contract does not name them): the CLI lives at `orchestrator.api.cli:app` as a
Typer or Click command, a CLI command is named after its route function with `_`->`-`
(either spelling accepted), and the manifest builder is `orchestrator.api.manifest`
exposing `build_manifest(app)` or `tool_manifest(app)` returning a list of tool dicts
(or a dict with a `tools` list), each carrying a `name`.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

from fastapi import FastAPI

from .conftest import Op, operations

# Contract operation tables, every row. A sanity check that the route table is complete
# against the contracts; the parity tests themselves iterate the route table, not this.
CONTRACT_OPERATIONS = {
    "create project": r"create.*project|project.*create",
    "list projects": r"list.*project|projects",
    "load project": r"(load|get|open).*project|project.*(load|get)",
    "draft script": r"draft",
    "revise script": r"revise.*script|script.*revise",
    "propose shots": r"propos",
    "revise shot": r"revise.*shot|shot.*revise",
    "search assets": r"search",
    "create asset": r"asset",
    "build scene": r"scene",
    "render preview": r"preview",
    "generate media": r"generat",
    "assemble cut": r"assemble.*cut|cut.*assemble",
    "sample references": r"sample|reference",
    "report jobs": r"job",
    "apply feedback": r"feedback",
    "score rubric": r"rubric|score",
    "record review": r"record.*review",
    "list reviews": r"list.*review",
    "approve framing": r"approve.*framing|framing.*approve",
    "approve shot": r"approve.*shot|shot.*approve",
    "approve cut": r"approve.*cut|cut.*approve",
    "resume invalidations": r"resume.*invalidation",
    "revise camera profile": r"revise.*camera.*profile|camera.*profile.*revise",
    "cancel job": r"cancel.*job|job.*cancel",
    "list shot outputs": r"list.*shot.*output|shot.*output.*list",
    "list cuts": r"list.*cut|cuts",
    "inspect output": r"inspect.*output|output.*inspect",
    "contact sheet": r"contact.*sheet|sheet.*contact",
    "rubric sample": r"rubric.*sample|sample.*rubric",
    "evaluate rubric": r"evaluate.*rubric|rubric.*evaluate",
    "measure output": r"measure.*output|output.*measure",
    "measure asset": r"measure.*asset|asset.*measure",
    "review audio": r"review.*audio|audio.*review",
    "get audio review": r"get.*audio.*review|audio.*review.*get",
    "list audio reviews": r"list.*audio.*review|audio.*review.*list",
    "export": r"export",
}


def _cli() -> Any:
    module = importlib.import_module("orchestrator.api.cli")
    return module.app


def _runner_and_command(cli: Any):
    import click

    if isinstance(cli, click.Command):
        from click.testing import CliRunner

        return CliRunner(), cli
    from typer.testing import CliRunner as TyperRunner

    return TyperRunner(), cli


def _command_names(cli: Any) -> set[str]:
    if hasattr(cli, "list_commands"):
        command = cli
    else:
        # Only a Typer application needs converting; the CLI is Click, and importing
        # typer unconditionally made it a hard test dependency nothing declared.
        import typer

        command = typer.main.get_command(cli)
    names: set[str] = set()

    def walk(cmd: Any, prefix: str = "") -> None:
        if hasattr(cmd, "commands"):
            for name, sub in cmd.commands.items():
                walk(sub, f"{prefix}{name} ")
        else:
            names.add(prefix.strip())

    walk(command)
    return names


def _spellings(op: Op) -> set[str]:
    base = op.name.lower()
    return {base, base.replace("_", "-"), base.replace("_", " ")}


def test_route_table_covers_every_contract_operation(app: FastAPI) -> None:
    haystack = [f"{o.path} {o.name}" for o in operations(app)]
    missing = [
        name for name, pat in CONTRACT_OPERATIONS.items() if not any(re.search(pat, h, re.IGNORECASE) for h in haystack)
    ]
    assert not missing, f"contract operations with no HTTP route: {missing}"


def test_every_route_has_a_cli_command(app: FastAPI, op: Op) -> None:
    names = _command_names(_cli())
    flat = {n.replace(" ", "-") for n in names} | names
    assert _spellings(op) & flat, f"{op.key} ({op.name}) has no CLI command; CLI has {sorted(names)}"


def test_every_cli_command_has_a_route(app: FastAPI) -> None:
    route_names = {s for o in operations(app) for s in _spellings(o)}
    orphans = [n for n in _command_names(_cli()) if n not in route_names and n.replace(" ", "-") not in route_names]
    assert not orphans, f"CLI commands with no HTTP route (a hidden second path, FR-14): {orphans}"


def test_cli_commands_are_machine_readable(app: FastAPI, op: Op) -> None:
    cli = _cli()
    runner, command = _runner_and_command(cli)
    names = [n for n in _command_names(cli) if n.replace(" ", "-") in {s.replace(" ", "-") for s in _spellings(op)}]
    assert names, f"{op.key} has no CLI command"
    result = runner.invoke(command, [*names[0].split(), "--help"])
    assert result.exit_code == 0, result.output
    root = runner.invoke(command, ["--help"])
    assert "--json" in result.output or "--json" in root.output


def test_cli_failure_is_nonzero_with_error_on_stderr(app: FastAPI) -> None:
    runner, command = _runner_and_command(_cli())
    try:
        runner = type(runner)(mix_stderr=False)  # click < 8.2
    except TypeError:
        pass  # click >= 8.2 always separates stderr
    result = runner.invoke(command, ["definitely-not-a-command"])
    assert result.exit_code != 0
    assert result.stderr.strip(), "errors must go to stderr"


def test_openapi_lists_every_route(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    for op in operations(app):
        assert op.method.lower() in paths[op.path], op.key


def _manifest(app: FastAPI) -> list[dict[str, Any]]:
    module = importlib.import_module("orchestrator.api.manifest")
    builder = getattr(module, "build_manifest", None) or module.tool_manifest
    result = builder(app)
    return result["tools"] if isinstance(result, dict) else list(result)


def test_manifest_is_derived_from_the_route_table(app: FastAPI, op: Op) -> None:
    names = {str(t.get("name", "")).lower().replace("-", "_") for t in _manifest(app)}
    # Named independently of the production flag, so marking any other operation
    # creator-only (and so hiding it from MCP) fails here.
    creator_only = {"approve_framing", "approve_shot", "approve_cut"}
    expected = op.name not in creator_only or app.state.settings.approvals.agent_may_approve
    assert (op.name.lower() in names) is expected, (
        f"{op.key} manifest presence should be {expected}; tools are {sorted(names)}"
    )


def test_manifest_gains_a_new_route_without_a_second_edit(app: FastAPI) -> None:
    def brand_new_operation() -> dict[str, str]:
        return {}

    app.add_api_route("/brand-new-operation", brand_new_operation, methods=["GET"])
    names = {str(t.get("name", "")).lower().replace("-", "_") for t in _manifest(app)}
    assert "brand_new_operation" in names
    assert "/brand-new-operation" in app.openapi()["paths"]
