"""Shared harness for the API/CLI tests, written from orchestrator/api/CONTRACT.md only.

The contract does NOT fix these; they are the test author's assumptions and the single
place to adjust if the implementation names them differently (report it as a contract
gap, never weaken an assertion):

    orchestrator.api.app:create_app(settings=Settings, token=str) -> FastAPI
    TOKEN_HEADER   the header carrying the loopback token
    orchestrator.api.cli:app   a Typer or Click command (see test_parity.py)
"""

from __future__ import annotations

import importlib
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from orchestrator.platform.settings import Settings

TOKEN = "t0k3n-correct-horse-battery-staple"
WRONG_TOKEN = "t0k3n-wrong-wrong-wrong-wrong-0"
TOKEN_HEADER = "X-Loopback-Token"
LOOPBACK_BASE = "http://127.0.0.1:8765"
EVIL_ORIGIN = "http://evil.example"
TEST_ROUTE_PREFIX = "/__test__"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
INFRA_PATHS = {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def make_app(settings: Settings) -> FastAPI:
    module = importlib.import_module("orchestrator.api.app")
    app = module.create_app(settings=settings, token=TOKEN)
    assert isinstance(app, FastAPI)
    return app


@dataclass(frozen=True)
class Op:
    """One (method, path) taken from the application's own route table."""

    method: str
    path: str
    name: str
    route: APIRoute

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def mutating(self) -> bool:
        return self.method in MUTATING

    def url(self) -> str:
        out = self.path
        for param in self.route.dependant.path_params:
            out = out.replace("{" + param.name + "}", "x")
        return out


def operations(app: FastAPI) -> list[Op]:
    ops: list[Op] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if route.path in INFRA_PATHS or route.path.startswith(TEST_ROUTE_PREFIX):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            ops.append(Op(method, route.path, route.name, route))
    return ops


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise any test taking `op_key` over the app's own route table.

    If the app cannot be built, one deliberately failing case is generated so the
    problem is visible rather than silently collecting zero tests.
    """
    if "op_key" not in metafunc.fixturenames:
        return
    with tempfile.TemporaryDirectory() as d:
        try:
            keys = [o.key for o in operations(make_app(Settings(project_root=Path(d))))]
        except Exception as exc:  # noqa: BLE001 - surfaced as a failing test, not hidden
            keys = [f"APP-UNBUILDABLE: {type(exc).__name__}: {exc}"]
    metafunc.parametrize("op_key", keys)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def settings(project_root: Path) -> Settings:
    return Settings(project_root=project_root)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return make_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=LOOPBACK_BASE, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN}


@pytest.fixture
def op(app: FastAPI, op_key: str) -> Op:
    return next(o for o in operations(app) if o.key == op_key)


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def sample_value(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> Any:
    schema = _resolve(schema, spec)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"]
            return sample_value(options[0], spec, depth + 1) if options else None
    if "allOf" in schema:
        return sample_value(schema["allOf"][0], spec, depth + 1)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        if depth > 4:
            return {}
        required = schema.get("required", [])
        return {
            k: sample_value(v, spec, depth + 1)
            for k, v in schema.get("properties", {}).items()
            if k in required
        }
    if kind == "array":
        return []
    if kind == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if kind == "number":
        return max(1.0, float(schema.get("minimum", 1.0)))
    if kind == "boolean":
        return False
    return "sample"


def valid_request_kwargs(app: FastAPI, op: Op) -> dict[str, Any]:
    """A schema-valid request built from the app's own OpenAPI, so validation (422)
    cannot pre-empt the behaviour under test."""
    spec = app.openapi()
    entry = spec["paths"][op.path][op.method.lower()]
    kwargs: dict[str, Any] = {}
    params = {
        p["name"]: sample_value(p.get("schema", {}), spec)
        for p in entry.get("parameters", [])
        if p["in"] == "query" and p.get("required")
    }
    if params:
        kwargs["params"] = params
    body = entry.get("requestBody", {}).get("content", {})
    if "application/json" in body:
        kwargs["json"] = sample_value(body["application/json"]["schema"], spec)
    return kwargs


def find_key(node: Any, predicate: Callable[[str], bool]) -> list[Any]:
    """Every value stored under a key matching `predicate`, anywhere in a JSON body."""
    found: list[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if predicate(str(k)):
                found.append(v)
            found.extend(find_key(v, predicate))
    elif isinstance(node, list):
        for item in node:
            found.extend(find_key(item, predicate))
    return found


def add_raising_routes(app: FastAPI, cases: dict[str, Callable[[], BaseException]]) -> None:
    """Mount test-only routes that raise a given exception, exercising the app-level
    error mapping independent of any particular business route."""
    for name, factory in cases.items():

        def _raise(factory: Callable[[], BaseException] = factory) -> None:
            raise factory()

        app.add_api_route(f"{TEST_ROUTE_PREFIX}/raise/{name}", _raise, methods=["GET"])
