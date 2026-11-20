"""Conformance tests for the MCP adapter, from MCP_CONTRACT.md only."""

from __future__ import annotations

import base64
import copy
import http.server
import io
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import Body, FastAPI, Query
from fastapi.responses import JSONResponse, Response
from starlette.testclient import TestClient

from orchestrator.api import manifest as manifest_module
from orchestrator.api import mcp
from orchestrator.platform.settings import Settings

from .conftest import LOOPBACK_BASE, TOKEN, TOKEN_HEADER


def _build_manifest(app: FastAPI) -> dict[str, Any]:
    result = manifest_module.build_manifest(app)
    assert isinstance(result, dict)
    assert isinstance(result.get("tools"), list)
    return result


def _tool_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in manifest["tools"]}


def _rpc(method: str, *, request_id: Any = 1, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _rpc_error(response: dict[str, Any], code: int) -> dict[str, Any]:
    assert response["jsonrpc"] == "2.0"
    assert response["error"]["code"] == code
    return response["error"]


def _tool_result(response: dict[str, Any]) -> dict[str, Any]:
    assert response["jsonrpc"] == "2.0"
    assert "result" in response
    return response["result"]


def _json_tool_body(result: dict[str, Any]) -> dict[str, Any]:
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


class AppTransport:
    """Forward MCP requests into the real app using the repository's token header."""

    def __init__(self, client: TestClient, token: str = TOKEN) -> None:
        self.client = client
        self.token = token
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> mcp.HttpResponse:
        call = {
            "method": method,
            "path": path,
            "query": query,
            "json_body": json_body,
            "headers": {TOKEN_HEADER: self.token},
        }
        self.calls.append(call)
        kwargs: dict[str, Any] = {"headers": call["headers"]}
        if query is not None:
            kwargs["params"] = query
        if json_body is not None:
            kwargs["json"] = json_body
        response = self.client.request(method, path, **kwargs)
        return mcp.HttpResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=response.content,
        )


class OverrideTransport:
    """Forward to the app except for an explicit response/error used to test failures."""

    def __init__(self, inner: AppTransport) -> None:
        self.inner = inner
        self.response: mcp.HttpResponse | None = None
        self.error: mcp.TransportError | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> mcp.HttpResponse:
        if path == "/tool-manifest" and (self.response is not None or self.error is not None):
            if self.error is not None:
                raise self.error
            assert self.response is not None
            return self.response
        return self.inner.request(method, path, query=query, json_body=json_body)


@pytest.fixture
def app_transport(client: TestClient) -> AppTransport:
    return AppTransport(client)


@pytest.fixture
def server(app_transport: AppTransport) -> mcp.McpServer:
    return mcp.McpServer(app_transport)


def test_tool_manifest_matches_builder_and_is_a_fresh_meta_route(
    app: FastAPI, client: TestClient
) -> None:
    first = client.get("/tool-manifest")
    assert first.status_code == 200
    assert first.json() == _build_manifest(app)
    assert "/tool-manifest" not in app.openapi()["paths"]
    assert "tool_manifest" not in _tool_map(first.json())
    assert all(tool["http"]["path"] != "/tool-manifest" for tool in first.json()["tools"])

    def added_after_first_request() -> dict[str, bool]:
        return {"fresh": True}

    app.add_api_route(
        "/__test__/added-after-manifest",
        added_after_first_request,
        methods=["GET"],
        name="added_after_first_request",
    )
    second = client.get("/tool-manifest")
    assert second.status_code == 200
    assert second.json() == _build_manifest(app)
    assert "added_after_first_request" in _tool_map(second.json())


def test_tool_manifest_is_a_read_and_needs_no_token(client: TestClient) -> None:
    response = client.get("/tool-manifest")
    assert response.status_code == 200


def test_manifest_publishes_query_parameters_for_all_asset_reads(app: FastAPI) -> None:
    manifest = _build_manifest(app)
    tools = _tool_map(manifest)
    expected = {
        "search_assets",
        "list_asset_categories",
        "asset_preview",
        "asset_browser",
    }
    assert expected <= tools.keys()

    schema = app.openapi()
    for name in sorted(expected):
        tool = tools[name]
        operation = schema["paths"][tool["http"]["path"]][tool["http"]["method"].lower()]
        query_params = [p for p in operation.get("parameters", []) if p["in"] == "query"]
        assert query_params, f"{name} is required to expose query parameters"
        properties = tool["input_schema"]["properties"]
        required = set(tool["input_schema"].get("required", []))
        for param in query_params:
            assert param["name"] in properties, f"{name} omitted query {param['name']}"
            assert properties[param["name"]] == param["schema"]
            assert (param["name"] in required) is bool(param.get("required", False))
        assert TOKEN_HEADER.lower() not in {key.lower() for key in properties}


def test_manifest_http_routing_metadata_matches_route_table(app: FastAPI) -> None:
    manifest = _build_manifest(app)
    for tool in manifest["tools"]:
        http = tool["http"]
        route = next(
            route
            for route in app.routes
            if getattr(route, "path", None) == http["path"]
            and http["method"] in getattr(route, "methods", set())
        )
        path_params = [parameter.name for parameter in route.dependant.path_params]
        openapi_operation = app.openapi()["paths"][http["path"]][http["method"].lower()]
        query_params = [
            parameter["name"]
            for parameter in openapi_operation.get("parameters", [])
            if parameter["in"] == "query"
        ]
        body_types = openapi_operation.get("requestBody", {}).get("content", {})
        if "multipart/form-data" in body_types:
            expected_body: str | None = "multipart"
        elif "application/json" in body_types:
            expected_body = "json"
        else:
            expected_body = None

        assert http["path_params"] == path_params
        assert set(http["query_params"]) == set(query_params)
        assert http["body"] == expected_body
        assert tool["asynchronous"] is (route.status_code == 202)


def test_manifest_rejects_a_query_and_payload_name_collision(app: FastAPI) -> None:
    def colliding_operation(
        item: str,
        payload: dict[str, Any] = Body(...),
        query_payload: str = Query(..., alias="payload"),
    ) -> dict[str, Any]:
        return {"item": item, "payload": payload, "query": query_payload}

    app.add_api_route(
        "/__test__/collision/{item}",
        colliding_operation,
        methods=["POST"],
        name="colliding_operation",
    )
    with pytest.raises(ValueError) as exc_info:
        manifest_module.build_manifest(app)
    assert "colliding_operation" in str(exc_info.value)
    assert "payload" in str(exc_info.value)


def test_manifest_rejects_a_path_and_query_name_collision(app: FastAPI) -> None:
    def path_query_collision(item: str, query_item: str = Query(..., alias="item")) -> dict[str, str]:
        return {"item": item, "query": query_item}

    app.add_api_route(
        "/__test__/path-query-collision/{item}",
        path_query_collision,
        methods=["GET"],
        name="path_query_collision",
    )
    with pytest.raises(ValueError) as exc_info:
        manifest_module.build_manifest(app)
    assert "path_query_collision" in str(exc_info.value)
    assert "item" in str(exc_info.value)


def test_manifest_rejects_a_path_and_payload_name_collision(app: FastAPI) -> None:
    def path_payload_collision(payload: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {"payload": payload, "body": body}

    app.add_api_route(
        "/__test__/path-payload-collision/{payload}",
        path_payload_collision,
        methods=["POST"],
        name="path_payload_collision",
    )
    with pytest.raises(ValueError) as exc_info:
        manifest_module.build_manifest(app)
    assert "path_payload_collision" in str(exc_info.value)
    assert "payload" in str(exc_info.value)


def _synthetic_manifest() -> dict[str, Any]:
    return {
        "components": {
            "schemas": {
                "NodeA": {
                    "type": "object",
                    "properties": {
                        "child": {"$ref": "#/components/schemas/NodeB"},
                        "again": {"$ref": "#/components/schemas/NodeA"},
                    },
                },
                "NodeB": {
                    "type": "object",
                    "properties": {
                        "parent": {"$ref": "#/components/schemas/NodeA"},
                        "label": {"type": "string"},
                    },
                },
                "Unused": {"type": "string"},
            }
        },
        "tools": [
            {
                "name": "read_tree",
                "description": "Read a tree.",
                "input_schema": {
                    "type": "object",
                    "properties": {"root": {"$ref": "#/components/schemas/NodeA"}},
                },
                "http": {
                    "method": "GET",
                    "path": "/trees",
                    "path_params": [],
                    "query_params": [],
                    "body": None,
                },
                "asynchronous": False,
            },
            {
                "name": "submit_job",
                "description": "Submit a job.",
                "input_schema": {"type": "object", "properties": {"label": {"type": "string"}}},
                "http": {
                    "method": "POST",
                    "path": "/jobs",
                    "path_params": [],
                    "query_params": [],
                    "body": "json",
                },
                "asynchronous": True,
            },
            {
                "name": "create_asset",
                "description": "Upload an asset.",
                "input_schema": {"type": "object", "properties": {}},
                "http": {
                    "method": "POST",
                    "path": "/assets",
                    "path_params": [],
                    "query_params": [],
                    "body": "multipart",
                },
                "asynchronous": False,
            },
        ],
    }


def test_tools_from_manifest_rewrites_transitive_recursive_refs_without_mutating_input() -> None:
    manifest = _synthetic_manifest()
    original = copy.deepcopy(manifest)

    tools = mcp.tools_from_manifest(manifest)

    assert manifest == original
    assert [tool["name"] for tool in tools] == ["pipeline_read_tree", "pipeline_submit_job"]
    read = tools[0]
    schema = read["inputSchema"]
    assert schema["properties"]["root"]["$ref"] == "#/$defs/NodeA"
    assert set(schema["$defs"]) == {"NodeA", "NodeB"}
    assert schema["$defs"]["NodeA"]["properties"]["child"]["$ref"] == "#/$defs/NodeB"
    assert schema["$defs"]["NodeA"]["properties"]["again"]["$ref"] == "#/$defs/NodeA"
    assert schema["$defs"]["NodeB"]["properties"]["parent"]["$ref"] == "#/$defs/NodeA"
    assert not any(
        isinstance(node, str) and "#/components/" in node
        for node in _walk(schema)
    )
    assert tools[1]["description"].endswith(
        " Asynchronous: answers 202 with a job record; poll pipeline_report_jobs for its state."
    )
    assert "readOnlyHint" in tools[0]["annotations"] and tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[1]["annotations"] == {"readOnlyHint": False}
    assert "$defs" not in tools[1]["inputSchema"]


def test_tools_from_manifest_rejects_a_missing_component_reference() -> None:
    manifest = {
        "components": {"schemas": {}},
        "tools": [
            {
                "name": "broken",
                "description": "Broken schema.",
                "input_schema": {"$ref": "#/components/schemas/Missing"},
                "http": {
                    "method": "GET",
                    "path": "/broken",
                    "path_params": [],
                    "query_params": [],
                    "body": None,
                },
                "asynchronous": False,
            }
        ],
    }
    with pytest.raises(ValueError, match="Missing"):
        mcp.tools_from_manifest(manifest)


def test_every_published_tool_name_is_prefixed_and_self_contained(server: mcp.McpServer) -> None:
    response = server.handle(_rpc("tools/list"))
    assert response is not None
    tools = response["result"]["tools"]
    assert tools
    assert all(tool["name"].startswith(mcp.TOOL_PREFIX) for tool in tools)
    for tool in tools:
        assert not any(
            isinstance(node, str) and "#/components/" in node
            for node in _walk(tool["inputSchema"])
        )
    assert "pipeline_create_asset" not in {tool["name"] for tool in tools}


def test_create_asset_is_a_multipart_manifest_operation_but_not_an_mcp_tool(
    app: FastAPI, server: mcp.McpServer
) -> None:
    manifest = _build_manifest(app)
    assert _tool_map(manifest)["create_asset"]["http"]["body"] == "multipart"
    listing = server.handle(_rpc("tools/list"))
    assert listing is not None
    assert "pipeline_create_asset" not in {tool["name"] for tool in listing["result"]["tools"]}


def test_mcp_interface_constants_are_pinned() -> None:
    assert mcp.TOOL_PREFIX == "pipeline_"
    assert mcp.TOKEN_ENV == "CCTV_PIPELINE_TOKEN"
    assert mcp.SUPPORTED_PROTOCOL_VERSIONS == (
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
    )


def test_http_response_is_a_frozen_value_object() -> None:
    response = mcp.HttpResponse(status=200, content_type="", body=b"")
    with pytest.raises((AttributeError, TypeError)):
        response.status = 201  # type: ignore[misc]


def test_importing_api_package_does_not_import_the_mcp_adapter() -> None:
    check = (
        "import sys, orchestrator.api; "
        "assert 'orchestrator.api.mcp' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", check],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("2025-06-18", "2025-06-18"),
        ("not-supported", "2025-11-25"),
        (None, "2025-11-25"),
    ],
)
def test_initialize_negotiates_protocol_and_identifies_server(
    server: mcp.McpServer, requested: str | None, expected: str
) -> None:
    response = server.handle(
        _rpc("initialize", params={"protocolVersion": requested, "capabilities": {}})
    )
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"] == expected
    # Part D added read-only resources beside the tools.
    assert result["capabilities"] == {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
    }
    assert result["serverInfo"]["name"] == "cctv-pipeline"
    assert isinstance(result["serverInfo"]["version"], str)
    assert result["serverInfo"]["version"]


def test_ping_and_notifications(server: mcp.McpServer) -> None:
    ping = server.handle(_rpc("ping"))
    assert ping is not None
    assert ping["result"] == {}
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "anything/notification"}) is None


def test_json_rpc_validation_and_unknown_method(server: mcp.McpServer) -> None:
    for invalid in (
        None,
        [],
        "message",
        17,
        False,
        {"jsonrpc": "1.0", "method": "ping"},
        {"jsonrpc": "2.0"},
    ):
        response = server.handle(invalid)
        assert response is not None
        _rpc_error(response, -32600)

    string_id = server.handle({"jsonrpc": "1.0", "method": "ping", "id": "request"})
    assert string_id is not None
    _rpc_error(string_id, -32600)
    assert string_id["id"] == "request"
    integer_id = server.handle({"jsonrpc": "2.0", "method": 3, "id": 17})
    assert integer_id is not None
    _rpc_error(integer_id, -32600)
    assert integer_id["id"] == 17
    no_valid_id = server.handle({"jsonrpc": "2.0", "method": None, "id": {"bad": True}})
    assert no_valid_id is not None
    _rpc_error(no_valid_id, -32600)
    assert no_valid_id["id"] is None

    unknown = server.handle(_rpc("not/a/method"))
    assert unknown is not None
    _rpc_error(unknown, -32601)


def test_tools_list_fetches_fresh_manifest_and_call_reuses_it(
    app: FastAPI, server: mcp.McpServer, app_transport: AppTransport
) -> None:
    first = server.handle(_rpc("tools/list"))
    assert first is not None
    assert len([call for call in app_transport.calls if call["path"] == "/tool-manifest"]) == 1

    def fresh_echo() -> dict[str, str]:
        return {"fresh": "yes"}

    app.add_api_route("/__test__/fresh-echo", fresh_echo, methods=["GET"], name="fresh_echo")
    second = server.handle(_rpc("tools/list", request_id=2))
    assert second is not None
    assert "pipeline_fresh_echo" in {tool["name"] for tool in second["result"]["tools"]}
    assert len([call for call in app_transport.calls if call["path"] == "/tool-manifest"]) == 2

    call = server.handle(
        _rpc("tools/call", request_id=3, params={"name": "pipeline_fresh_echo", "arguments": {}})
    )
    assert call is not None
    assert _json_tool_body(_tool_result(call)) == {"http_status": 200, "body": {"fresh": "yes"}}
    assert len([entry for entry in app_transport.calls if entry["path"] == "/tool-manifest"]) == 2


def test_tools_call_fetches_manifest_if_list_has_not_been_called(
    app: FastAPI, app_transport: AppTransport, server: mcp.McpServer
) -> None:
    def direct_echo() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/__test__/direct-echo", direct_echo, methods=["GET"], name="direct_echo")
    response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_direct_echo", "arguments": {}})
    )
    assert response is not None
    assert _json_tool_body(_tool_result(response)) == {"http_status": 200, "body": {"ok": True}}
    assert len([call for call in app_transport.calls if call["path"] == "/tool-manifest"]) == 1


def test_tools_call_defaults_absent_arguments_to_an_empty_object(
    app: FastAPI, server: mcp.McpServer
) -> None:
    def no_argument_echo() -> dict[str, bool]:
        return {"called": True}

    app.add_api_route(
        "/__test__/no-arguments", no_argument_echo, methods=["GET"], name="no_argument_echo"
    )
    response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_no_argument_echo"})
    )
    assert response is not None
    assert _json_tool_body(_tool_result(response)) == {
        "http_status": 200,
        "body": {"called": True},
    }


@pytest.mark.parametrize(
    ("override", "expected_fragment"),
    [
        (mcp.HttpResponse(503, "application/json", b'{"error":"offline"}'), "503"),
        (mcp.HttpResponse(200, "application/json", b"not-json"), ""),
        (mcp.HttpResponse(200, "application/json", b"{}"), ""),
    ],
)
def test_tools_list_failures_are_internal_errors_not_empty_lists(
    client: TestClient,
    override: mcp.HttpResponse,
    expected_fragment: str,
) -> None:
    transport = OverrideTransport(AppTransport(client))
    transport.response = override
    response = mcp.McpServer(transport).handle(_rpc("tools/list"))
    assert response is not None
    error = _rpc_error(response, -32603)
    assert error["message"].strip()
    if expected_fragment:
        assert expected_fragment.lower() in error["message"].lower()
    assert "result" not in response


def test_tools_list_transport_failure_names_why(client: TestClient) -> None:
    transport = OverrideTransport(AppTransport(client))
    transport.error = mcp.TransportError("server may not be running at http://127.0.0.1:8765")
    response = mcp.McpServer(transport).handle(_rpc("tools/list"))
    assert response is not None
    assert "server may not be running" in _rpc_error(response, -32603)["message"]


def test_unknown_unprefixed_and_excluded_upload_tools_are_invalid_params(server: mcp.McpServer) -> None:
    for name in ("render_preview", "pipeline_missing_tool", "pipeline_create_asset"):
        response = server.handle(_rpc("tools/call", params={"name": name, "arguments": {}}))
        assert response is not None
        _rpc_error(response, -32602)


def test_tool_call_arguments_validation_returns_tool_errors(
    app: FastAPI, server: mcp.McpServer
) -> None:
    def path_echo(value: str) -> dict[str, str]:
        return {"value": value}

    app.add_api_route(
        "/__test__/path/{value}", path_echo, methods=["GET"], name="path_echo"
    )
    missing_path = server.handle(
        _rpc("tools/call", params={"name": "pipeline_path_echo", "arguments": {}})
    )
    assert missing_path is not None
    missing_result = _tool_result(missing_path)
    assert missing_result["isError"] is True
    assert "value" in json.loads(missing_result["content"][0]["text"])["error"]
    assert "error" not in missing_path

    extra_arg = server.handle(
        _rpc(
            "tools/call",
            request_id=2,
            params={"name": "pipeline_path_echo", "arguments": {"value": "ok", "surprise": 1}},
        )
    )
    assert extra_arg is not None
    extra_result = _tool_result(extra_arg)
    assert extra_result["isError"] is True
    assert "surprise" in extra_result["content"][0]["text"]

    non_object = server.handle(
        _rpc("tools/call", request_id=3, params={"name": "pipeline_path_echo", "arguments": []})
    )
    assert non_object is not None
    _rpc_error(non_object, -32602)


def test_path_parameters_with_slashes_are_rejected_before_transport(
    app: FastAPI, app_transport: AppTransport, server: mcp.McpServer
) -> None:
    reached_escape_route: list[bool] = []

    def item_echo(value: str) -> dict[str, str]:
        return {"value": value}

    def escape_route() -> dict[str, bool]:
        reached_escape_route.append(True)
        return {"escaped": True}

    app.add_api_route("/__test__/items/{value}", item_echo, methods=["GET"], name="item_echo")
    app.add_api_route("/__test__/x", escape_route, methods=["GET"], name="escape_route")
    listing = server.handle(_rpc("tools/list"))
    assert listing is not None
    app_transport.calls.clear()
    response = server.handle(
        _rpc(
            "tools/call",
            params={"name": "pipeline_item_echo", "arguments": {"value": "../x"}},
        )
    )
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is True
    assert "value" in result["content"][0]["text"]
    assert app_transport.calls == []
    assert reached_escape_route == []


def test_query_routing_repeats_lists_serializes_booleans_and_omits_none(
    app: FastAPI, app_transport: AppTransport, server: mcp.McpServer
) -> None:
    def query_echo(
        project_id: str,
        tags: list[str] = Query(default=[]),
        enabled: bool = Query(default=False),
        note: str | None = Query(default="default"),
    ) -> dict[str, Any]:
        return {"project_id": project_id, "tags": tags, "enabled": enabled, "note": note}

    app.add_api_route(
        "/__test__/query/{project_id}", query_echo, methods=["GET"], name="query_echo"
    )
    response = server.handle(
        _rpc(
            "tools/call",
            params={
                "name": "pipeline_query_echo",
                "arguments": {
                    "project_id": "p1",
                    "tags": ["first", "second"],
                    "enabled": True,
                    "note": None,
                },
            },
        )
    )
    assert response is not None
    body = _json_tool_body(_tool_result(response))
    assert body == {
        "http_status": 200,
        "body": {"project_id": "p1", "tags": ["first", "second"], "enabled": True, "note": "default"},
    }
    routed = next(call for call in app_transport.calls if call["path"] == "/__test__/query/p1")
    assert routed["query"] == [
        ("tags", "first"),
        ("tags", "second"),
        ("enabled", "true"),
    ]


def test_json_payload_is_sent_as_the_http_body(
    app: FastAPI, app_transport: AppTransport, server: mcp.McpServer
) -> None:
    def payload_echo(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {"received": payload}

    app.add_api_route("/__test__/payload", payload_echo, methods=["POST"], name="payload_echo")
    payload = {"title": "night café"}
    response = server.handle(
        _rpc(
            "tools/call",
            params={"name": "pipeline_payload_echo", "arguments": {"payload": payload}},
        )
    )
    assert response is not None
    assert _json_tool_body(_tool_result(response)) == {
        "http_status": 200,
        "body": {"received": payload},
    }
    assert next(call for call in app_transport.calls if call["path"] == "/__test__/payload")["json_body"] == payload


def test_mutation_uses_transport_token_and_http_refuses_missing_token(
    app: FastAPI,
    client: TestClient,
    app_transport: AppTransport,
    server: mcp.McpServer,
) -> None:
    def mutate() -> dict[str, bool]:
        return {"mutated": True}

    app.add_api_route("/__test__/mutate", mutate, methods=["POST"], name="test_mutation")

    unauthenticated = client.post("/__test__/mutate")
    assert unauthenticated.status_code == 401
    assert TOKEN not in unauthenticated.text

    response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_test_mutation", "arguments": {}})
    )
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is False
    assert _json_tool_body(result) == {"http_status": 200, "body": {"mutated": True}}
    assert app_transport.calls[-1]["headers"] == {TOKEN_HEADER: TOKEN}
    assert TOKEN not in json.dumps(response, ensure_ascii=False)


def test_http_4xx_body_is_preserved_as_an_error_tool_result(
    app: FastAPI, server: mcp.McpServer
) -> None:
    error_body = {"kind": "validation", "retryable": False, "detail": "bad input"}

    def rejected() -> Response:
        return JSONResponse(status_code=422, content=error_body)

    app.add_api_route("/__test__/rejected", rejected, methods=["GET"], name="rejected")
    response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_rejected", "arguments": {}})
    )
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is True
    assert _json_tool_body(result) == {"http_status": 422, "body": error_body}
    assert result["content"][0]["text"] == json.dumps(
        {"http_status": 422, "body": error_body}, ensure_ascii=False
    )


def test_async_202_is_listed_and_survives_in_tool_result(
    app: FastAPI, server: mcp.McpServer
) -> None:
    def submit() -> dict[str, str]:
        return {"job_id": "job-1", "state": "queued"}

    app.add_api_route(
        "/__test__/submit",
        submit,
        methods=["POST"],
        name="submit_async",
        status_code=202,
    )
    listing = server.handle(_rpc("tools/list"))
    assert listing is not None
    tool = next(tool for tool in listing["result"]["tools"] if tool["name"] == "pipeline_submit_async")
    assert "Asynchronous: answers 202 with a job record; poll pipeline_report_jobs for its state." in tool[
        "description"
    ]
    response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_submit_async", "arguments": {}})
    )
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is False
    assert _json_tool_body(result) == {
        "http_status": 202,
        "body": {"job_id": "job-1", "state": "queued"},
    }


@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "image/webp"])
def test_image_responses_use_base64_content_blocks(
    app: FastAPI, server: mcp.McpServer, content_type: str
) -> None:
    suffix = content_type.split("/")[1]
    image_bytes = b"image-data-" + suffix.encode("ascii")

    def image() -> Response:
        return Response(content=image_bytes, media_type=content_type)

    app.add_api_route(
        f"/__test__/image-{suffix}", image, methods=["GET"], name=f"asset_preview_{suffix}"
    )
    image_response = server.handle(
        _rpc(
            "tools/call",
            params={"name": f"pipeline_asset_preview_{suffix}", "arguments": {}},
        )
    )
    assert image_response is not None
    image_result = _tool_result(image_response)
    assert image_result["isError"] is False
    assert image_result["content"] == [
        {
            "type": "image",
            "data": base64.b64encode(image_bytes).decode("ascii"),
            "mimeType": content_type,
        }
    ]


def test_other_content_types_are_decoded_as_utf8_with_replacement(
    app: FastAPI, server: mcp.McpServer
) -> None:
    def html() -> Response:
        return Response(content=b"<b>caf\xff</b>", media_type="text/html")

    app.add_api_route("/__test__/html", html, methods=["GET"], name="asset_browser_test")
    html_response = server.handle(
        _rpc("tools/call", params={"name": "pipeline_asset_browser_test", "arguments": {}})
    )
    assert html_response is not None
    html_result = _tool_result(html_response)
    assert html_result["content"] == [{"type": "text", "text": "<b>caf\ufffd</b>"}]


def test_tool_transport_failure_is_a_tool_error_and_does_not_leak_token(
    app: FastAPI, client: TestClient
) -> None:
    def echo() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/__test__/transport-failure", echo, methods=["GET"], name="transport_failure")
    inner = AppTransport(client)

    class FailsOnce:
        def __init__(self) -> None:
            self.manifest_loaded = False

        def request(
            self,
            method: str,
            path: str,
            *,
            query: list[tuple[str, str]] | None = None,
            json_body: Any = None,
        ) -> mcp.HttpResponse:
            if path == "/tool-manifest":
                self.manifest_loaded = True
                return inner.request(method, path, query=query, json_body=json_body)
            raise mcp.TransportError("server may not be running at http://127.0.0.1:8765")

    response = mcp.McpServer(FailsOnce()).handle(
        _rpc("tools/call", params={"name": "pipeline_transport_failure", "arguments": {}})
    )
    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == {
        "error": "server may not be running at http://127.0.0.1:8765"
    }
    assert TOKEN not in json.dumps(response, ensure_ascii=False)


class FlushCountingBytesIO(io.BytesIO):
    def __init__(self, initial_bytes: bytes = b"") -> None:
        super().__init__(initial_bytes)
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def test_serve_stdio_parses_utf8_lines_skips_blanks_notifications_and_flushes(
    app: FastAPI, server: mcp.McpServer
) -> None:
    def unicode_echo() -> dict[str, str]:
        return {"message": "café"}

    app.add_api_route("/__test__/unicode", unicode_echo, methods=["GET"], name="unicode_echo")
    lines = [
        b"\n",
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
        b"{bad json}\n",
        b"\xff\n",
        b"[]\n",
        b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"pipeline_unicode_echo","arguments":{}}}\n',
    ]
    stdout = FlushCountingBytesIO()
    mcp.serve_stdio(server, io.BytesIO(b"".join(lines)), stdout)

    raw = stdout.getvalue()
    assert raw.endswith(b"\n")
    assert "café".encode("utf-8") in raw
    assert b"\\u00e9" not in raw
    decoded = [json.loads(line) for line in raw.splitlines()]
    assert len(decoded) == 5
    assert decoded[0]["result"] == {}
    assert decoded[1]["error"]["code"] == -32700
    assert decoded[2]["error"]["code"] == -32700
    assert decoded[3]["error"]["code"] == -32600
    assert decoded[4]["result"]["content"][0]["text"] == (
        '{"http_status": 200, "body": {"message": "café"}}'
    )
    assert stdout.flush_count == len(decoded)
    assert TOKEN.encode() not in raw


def test_serve_stdio_notification_alone_writes_nothing(server: mcp.McpServer) -> None:
    stdin = io.BytesIO(b'{"jsonrpc":"2.0","method":"notifications/cancelled"}\n')
    stdout = FlushCountingBytesIO()
    mcp.serve_stdio(server, stdin, stdout)
    assert stdout.getvalue() == b""
    assert stdout.flush_count == 0


@pytest.mark.parametrize("token_value", [None, ""])
def test_main_without_token_returns_two_without_reading_stdin_or_writing_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token_value: str | None,
) -> None:
    if token_value is None:
        monkeypatch.delenv(mcp.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(mcp.TOKEN_ENV, token_value)

    class NoReadBuffer(io.BytesIO):
        read_count = 0

        def read(self, *args: Any, **kwargs: Any) -> bytes:
            self.read_count += 1
            raise AssertionError("main must check the token before reading stdin")

        def readline(self, *args: Any, **kwargs: Any) -> bytes:
            self.read_count += 1
            raise AssertionError("main must check the token before reading stdin")

    class Stream:
        def __init__(self, buffer: io.BytesIO) -> None:
            self.buffer = buffer

        def write(self, text: str) -> int:
            return self.buffer.write(text.encode("utf-8"))

        def flush(self) -> None:
            self.buffer.flush()

    input_buffer = NoReadBuffer()
    output_buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", Stream(input_buffer))
    monkeypatch.setattr(sys, "stdout", Stream(output_buffer))

    return_code = mcp.main([])

    captured = capsys.readouterr()
    assert return_code == 2
    assert output_buffer.getvalue() == b""
    assert input_buffer.read_count == 0
    assert len(captured.err.strip().splitlines()) == 1
    assert mcp.TOKEN_ENV in captured.err
    assert TOKEN not in captured.err


def test_main_does_not_accept_the_token_from_argv(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(mcp.TOKEN_ENV, raising=False)
    output_buffer = io.BytesIO()

    class Stream:
        def __init__(self, buffer: io.BytesIO) -> None:
            self.buffer = buffer

        def write(self, text: str) -> int:
            return self.buffer.write(text.encode("utf-8"))

        def flush(self) -> None:
            self.buffer.flush()

    monkeypatch.setattr(sys, "stdin", Stream(io.BytesIO()))
    monkeypatch.setattr(sys, "stdout", Stream(output_buffer))

    return_code = mcp.main(["--token", TOKEN])

    captured = capsys.readouterr()
    assert return_code == 2
    assert output_buffer.getvalue() == b""
    assert mcp.TOKEN_ENV in captured.err
    assert TOKEN not in captured.err


def test_main_uses_settings_loopback_address_timeout_and_does_not_contact_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(mcp.TOKEN_ENV, TOKEN)
    captured: dict[str, Any] = {"requests": 0}

    class CaptureTransport:
        def __init__(self, base_url: str, token: str, *, timeout: float) -> None:
            captured.update(base_url=base_url, token=token, timeout=timeout)

        def request(self, *args: Any, **kwargs: Any) -> mcp.HttpResponse:
            captured["requests"] += 1
            raise AssertionError("main must not contact the server at startup")

    class Stream:
        def __init__(self) -> None:
            self.buffer = io.BytesIO()

    input_stream = Stream()
    output_stream = Stream()
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)

    monkeypatch.setattr(mcp, "LoopbackTransport", CaptureTransport)
    monkeypatch.setattr(
        mcp,
        "serve_stdio",
        lambda server, stdin, stdout: captured.update(stdin=stdin, stdout=stdout),
    )
    assert mcp.main([]) == 0

    settings = Settings()
    host = settings.server.host
    formatted_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    assert captured["base_url"] == f"http://{formatted_host}:{settings.server.port}"
    assert captured["token"] == TOKEN
    assert captured["timeout"] == 120
    assert captured["requests"] == 0
    assert captured["stdin"] is input_stream.buffer
    assert captured["stdout"] is output_stream.buffer


def test_main_brackets_ipv6_settings_host_and_honours_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mcp.TOKEN_ENV, TOKEN)
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text('[server]\nhost = "::1"\nport = 8765\n', encoding="utf-8")
    captured: dict[str, Any] = {}

    class CaptureTransport:
        def __init__(self, base_url: str, token: str, *, timeout: float) -> None:
            captured.update(base_url=base_url, token=token, timeout=timeout)

        def request(self, *args: Any, **kwargs: Any) -> mcp.HttpResponse:
            raise AssertionError("empty stdin must not issue an HTTP request")

    monkeypatch.setattr(mcp, "LoopbackTransport", CaptureTransport)
    monkeypatch.setattr(mcp, "serve_stdio", lambda server, stdin, stdout: None)
    assert mcp.main(["--settings", str(settings_path), "--timeout", "3.5"]) == 0
    assert captured == {"base_url": "http://[::1]:8765", "token": TOKEN, "timeout": 3.5}


@pytest.fixture
def stdlib_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    records: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self, body: bytes = b"") -> None:
            record = {
                "path": self.path,
                "method": self.command,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
            with lock:
                records.append(record)

        def do_GET(self) -> None:
            self._record()
            if urlsplit(self.path).path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            self._record(self.rfile.read(size))
            body = b'{"posted":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield f"http://{host}:{port}", records
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_loopback_transport_uses_token_utf8_json_and_ignores_proxy_environment(
    stdlib_server: tuple[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    base_url, records = stdlib_server
    for key in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    transport = mcp.LoopbackTransport(base_url, TOKEN, timeout=2.0)
    get_response = transport.request("GET", "/echo", query=[("tag", "one"), ("tag", "two")])
    post_response = transport.request("POST", "/submit", json_body={"message": "café"})

    assert get_response.status == 200
    assert get_response.content_type.startswith("application/json")
    assert json.loads(get_response.body) == {"ok": True}
    assert post_response.status == 200
    assert post_response.content_type.startswith("application/json")
    assert json.loads(post_response.body) == {"posted": True}
    assert len(records) == 2
    for record in records:
        assert record["headers"]["x-loopback-token"] == TOKEN
        assert record["headers"]["accept"] == "application/json"
        assert "origin" not in record["headers"]
    assert records[0]["path"] == "/echo?tag=one&tag=two"
    assert records[1]["headers"]["content-type"].startswith("application/json")
    assert json.loads(records[1]["body"].decode("utf-8")) == {"message": "café"}


def test_loopback_transport_returns_redirect_without_following_it(
    stdlib_server: tuple[str, list[dict[str, Any]]],
) -> None:
    base_url, records = stdlib_server
    response = mcp.LoopbackTransport(base_url, TOKEN, timeout=2.0).request("GET", "/redirect")
    assert response.status == 302
    assert len(records) == 1
    assert records[0]["path"] == "/redirect"


def test_loopback_transport_errors_name_server_url_without_leaking_token() -> None:
    base_url = "http://127.0.0.1:0"
    transport = mcp.LoopbackTransport(base_url, TOKEN, timeout=0.2)
    with pytest.raises(mcp.TransportError) as exc_info:
        transport.request("GET", "/unreachable")
    message = str(exc_info.value)
    assert base_url in message
    assert "may not be running" in message.lower()
    assert TOKEN not in message


class _NoReadBuffer(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.read_count = 0

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        self.read_count += 1
        raise AssertionError("main must reject invalid configuration before reading stdin")

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        self.read_count += 1
        raise AssertionError("main must reject invalid configuration before reading stdin")

    def __iter__(self) -> _NoReadBuffer:
        return self

    def __next__(self) -> bytes:
        self.read_count += 1
        raise AssertionError("main must reject invalid configuration before reading stdin")


class _BinaryStream:
    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer

    def write(self, text: str) -> int:
        return self.buffer.write(text.encode("utf-8"))

    def flush(self) -> None:
        self.buffer.flush()


@pytest.mark.parametrize("bad_token", [f"{TOKEN}\r\n", f"{TOKEN} ", f"{TOKEN}é"])
def test_main_rejects_non_visible_ascii_tokens_without_disclosing_them(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_token: str,
) -> None:
    monkeypatch.setenv(mcp.TOKEN_ENV, bad_token)
    stdin_buffer = _NoReadBuffer()
    stdout_buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _BinaryStream(stdin_buffer))
    monkeypatch.setattr(sys, "stdout", _BinaryStream(stdout_buffer))
    monkeypatch.setattr(mcp, "serve_stdio", lambda *args: None)

    return_code = mcp.main([])

    captured = capsys.readouterr()
    assert return_code == 2
    assert stdout_buffer.getvalue() == b""
    assert stdin_buffer.read_count == 0
    assert TOKEN not in captured.err
    assert bad_token not in captured.err


@pytest.mark.parametrize("bad_token", [f"{TOKEN}\r\n", f"{TOKEN} ", f"{TOKEN}é"])
def test_loopback_transport_rejects_non_visible_ascii_tokens_without_disclosing_them(
    bad_token: str,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        mcp.LoopbackTransport("http://127.0.0.1:8765", bad_token, timeout=1.0)

    message = str(exc_info.value)
    assert TOKEN not in message
    assert bad_token not in message


@pytest.mark.parametrize(
    "argv",
    [
        ("--settings", TOKEN),
        (f"--settings=C:\\x\\{TOKEN}.toml",),
    ],
)
def test_main_rejects_token_in_any_argv_argument_without_disclosing_or_reading_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    monkeypatch.setenv(mcp.TOKEN_ENV, TOKEN)
    stdin_buffer = _NoReadBuffer()
    stdout_buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _BinaryStream(stdin_buffer))
    monkeypatch.setattr(sys, "stdout", _BinaryStream(stdout_buffer))

    return_code = mcp.main(argv)

    captured = capsys.readouterr()
    assert return_code == 2
    assert stdout_buffer.getvalue() == b""
    assert stdin_buffer.read_count == 0
    assert TOKEN not in captured.err
    assert "token" in captured.err.lower()
    assert "command" in captured.err.lower()
    assert "not" in captured.err.lower()


@pytest.mark.parametrize("project_id", ["p/assets/categories", "", ".", "..", "a/b"])
def test_load_project_rejects_path_segment_breakers_without_sending_a_request(
    app_transport: AppTransport,
    server: mcp.McpServer,
    project_id: str,
) -> None:
    listing = server.handle(_rpc("tools/list"))
    assert listing is not None
    assert "pipeline_load_project" in {tool["name"] for tool in listing["result"]["tools"]}
    app_transport.calls.clear()

    response = server.handle(
        _rpc(
            "tools/call",
            params={
                "name": "pipeline_load_project",
                "arguments": {"project_id": project_id},
            },
        )
    )

    assert response is not None
    result = _tool_result(response)
    assert result["isError"] is True
    assert "project_id" in result["content"][0]["text"]
    assert app_transport.calls == []


def test_load_project_percent_escape_is_encoded_and_sent_through_the_real_app(
    app: FastAPI,
    app_transport: AppTransport,
    server: mcp.McpServer,
) -> None:
    listing = server.handle(_rpc("tools/list"))
    assert listing is not None
    manifest_tool = _tool_map(_build_manifest(app))["load_project"]
    app_transport.calls.clear()

    response = server.handle(
        _rpc(
            "tools/call",
            params={
                "name": "pipeline_load_project",
                "arguments": {"project_id": "p%2Fx"},
            },
        )
    )

    assert response is not None
    assert "result" in response
    assert len(app_transport.calls) == 1
    expected_path = manifest_tool["http"]["path"].replace("{project_id}", "p%252Fx")
    assert app_transport.calls[0]["path"] == expected_path


def test_tools_call_treats_null_arguments_as_empty_and_rejects_other_non_objects(
    app: FastAPI,
    app_transport: AppTransport,
    server: mcp.McpServer,
) -> None:
    def no_argument_echo() -> dict[str, bool]:
        return {"called": True}

    app.add_api_route(
        "/__test__/null-arguments", no_argument_echo, methods=["GET"], name="null_arguments_echo"
    )
    response = server.handle(
        _rpc(
            "tools/call",
            params={"name": "pipeline_null_arguments_echo", "arguments": None},
        )
    )

    assert response is not None
    assert _json_tool_body(_tool_result(response)) == {
        "http_status": 200,
        "body": {"called": True},
    }
    calls_after_null = len(app_transport.calls)

    for request_id, invalid_arguments in enumerate(([], "not-an-object", 7), start=2):
        invalid = server.handle(
            _rpc(
                "tools/call",
                request_id=request_id,
                params={
                    "name": "pipeline_null_arguments_echo",
                    "arguments": invalid_arguments,
                },
            )
        )
        assert invalid is not None
        _rpc_error(invalid, -32602)

    assert len(app_transport.calls) == calls_after_null


@pytest.mark.parametrize(
    "invalid_field",
    [
        "name_not_string",
        "http_block_missing",
        "http_method_missing",
        "http_path_missing",
        "http_path_params_not_list",
        "http_query_params_not_list",
        "http_body_missing",
    ],
)
def test_tools_list_rejects_manifests_missing_required_routing_metadata(
    app: FastAPI,
    client: TestClient,
    invalid_field: str,
) -> None:
    manifest = copy.deepcopy(_build_manifest(app))
    tool = manifest["tools"][0]
    if invalid_field == "name_not_string":
        tool["name"] = 17
    elif invalid_field == "http_block_missing":
        del tool["http"]
    elif invalid_field == "http_method_missing":
        del tool["http"]["method"]
    elif invalid_field == "http_path_missing":
        del tool["http"]["path"]
    elif invalid_field == "http_path_params_not_list":
        tool["http"]["path_params"] = None
    elif invalid_field == "http_query_params_not_list":
        tool["http"]["query_params"] = "not-a-list"
    elif invalid_field == "http_body_missing":
        del tool["http"]["body"]
    else:
        raise AssertionError(f"unhandled manifest defect: {invalid_field}")

    transport = OverrideTransport(AppTransport(client))
    transport.response = mcp.HttpResponse(
        200,
        "application/json",
        json.dumps(manifest).encode("utf-8"),
    )
    response = mcp.McpServer(transport).handle(_rpc("tools/list"))

    assert response is not None
    error = _rpc_error(response, -32603)
    assert error["message"].strip()
    assert "result" not in response


def test_serve_stdio_lone_surrogate_id_does_not_stop_the_loop(server: mcp.McpServer) -> None:
    input_bytes = (
        b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    stdout = io.BytesIO()

    mcp.serve_stdio(server, io.BytesIO(input_bytes), stdout)

    output_lines = stdout.getvalue().decode("utf-8").splitlines()
    responses = [json.loads(line) for line in output_lines]
    assert len(responses) == 2
    assert responses[0]["id"] == "\ud800"
    assert responses[0]["result"] == {}
    assert responses[1]["id"] == 2
    assert responses[1]["result"] == {}


def test_serve_stdio_rejects_non_json_constants_and_continues(server: mcp.McpServer) -> None:
    input_bytes = b"".join(
        [
            b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"value":NaN}}\n',
            b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"value":Infinity}}\n',
            b'{"jsonrpc":"2.0","id":3,"method":"ping","params":{"value":-Infinity}}\n',
            b'{"jsonrpc":"2.0","id":4,"method":"ping"}\n',
        ]
    )
    stdout = io.BytesIO()

    mcp.serve_stdio(server, io.BytesIO(input_bytes), stdout)

    output_lines = stdout.getvalue().decode("utf-8").splitlines()
    responses = [json.loads(line) for line in output_lines]
    assert len(responses) == 4
    for response in responses[:3]:
        _rpc_error(response, -32700)
        assert response["id"] is None
    assert responses[3]["id"] == 4
    assert responses[3]["result"] == {}

