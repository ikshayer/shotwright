"""Conformance tests for MCP Part D (reference documents as resources), from MCP_CONTRACT.md only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from orchestrator.api import mcp

from .test_mcp_adapter import AppTransport, _rpc, _rpc_error

SCENE_REALISM_URI = "cctv-pipeline://references/scene-realism"
RESOURCES_DIR = Path(mcp.__file__).resolve().parent / "mcp_resources"


class ForbiddenTransport:
    """Fails the test if the adapter makes any request at all."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> mcp.HttpResponse:
        self.calls.append((method, path))
        pytest.fail(f"resource operation contacted the orchestrator: {method} {path}")


@pytest.fixture
def offline() -> tuple[mcp.McpServer, ForbiddenTransport]:
    transport = ForbiddenTransport()
    return mcp.McpServer(transport), transport


def _result(response: dict[str, Any] | None) -> dict[str, Any]:
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert "error" not in response, response
    return response["result"]


def _resource_uri(resource: mcp.ReferenceResource) -> str:
    return mcp.RESOURCE_URI_PREFIX + resource.slug


def test_uri_prefix_is_contracted() -> None:
    assert mcp.RESOURCE_URI_PREFIX == "cctv-pipeline://references/"


def test_registry_holds_scene_realism() -> None:
    entries = {r.slug: r for r in mcp.REFERENCE_RESOURCES}
    assert "scene-realism" in entries
    assert entries["scene-realism"].file == "scene_realism.md"
    assert entries["scene-realism"].title
    assert entries["scene-realism"].description
    assert _resource_uri(entries["scene-realism"]) == SCENE_REALISM_URI
    assert len({r.slug for r in mcp.REFERENCE_RESOURCES}) == len(mcp.REFERENCE_RESOURCES)


def test_initialize_advertises_resources_and_names_scene_realism(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
) -> None:
    server, transport = offline
    result = _result(
        server.handle(_rpc("initialize", params={"protocolVersion": "2025-06-18", "capabilities": {}}))
    )
    assert result["capabilities"]["resources"] == {"subscribe": False, "listChanged": False}
    instructions = result["instructions"]
    assert isinstance(instructions, str)
    assert SCENE_REALISM_URI in instructions
    lowered = instructions.lower()
    for word in ("before", "building", "dressing", "lighting"):
        assert word in lowered
    for resource in mcp.REFERENCE_RESOURCES:
        assert _resource_uri(resource) in instructions
    assert transport.calls == []


def test_resources_list_matches_registry_without_transport(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
) -> None:
    server, transport = offline
    result = _result(server.handle(_rpc("resources/list")))
    listed = result["resources"]
    assert [item["uri"] for item in listed] == [_resource_uri(r) for r in mcp.REFERENCE_RESOURCES]
    for item, resource in zip(listed, mcp.REFERENCE_RESOURCES, strict=True):
        assert item["name"] == resource.slug
        assert item["title"] == resource.title
        assert item["description"] == resource.description
        assert item["mimeType"] == "text/markdown"
    assert "nextCursor" not in result
    assert transport.calls == []


def test_resources_read_returns_exact_packaged_text_without_transport(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
) -> None:
    server, transport = offline
    expected = (RESOURCES_DIR / "scene_realism.md").read_text(encoding="utf-8")
    assert expected.strip()
    result = _result(server.handle(_rpc("resources/read", params={"uri": SCENE_REALISM_URI})))
    assert result["contents"] == [
        {"uri": SCENE_REALISM_URI, "mimeType": "text/markdown", "text": expected}
    ]
    assert transport.calls == []


def test_every_registered_resource_reads_its_file(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
) -> None:
    server, _ = offline
    for resource in mcp.REFERENCE_RESOURCES:
        expected = (RESOURCES_DIR / resource.file).read_text(encoding="utf-8")
        contents = _result(
            server.handle(_rpc("resources/read", params={"uri": _resource_uri(resource)}))
        )["contents"]
        assert contents[0]["text"] == expected


def test_read_is_not_cached(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Same file, different contents between two reads: an edited document is served
    # without a restart. The package directory is redirected to tmp_path, never written.
    document = tmp_path / "mcp_resources" / "scene_realism.md"
    document.parent.mkdir()
    monkeypatch.setattr(mcp.importlib.resources, "files", lambda _package: tmp_path)
    server, _ = offline

    def read_text() -> str:
        result = _result(server.handle(_rpc("resources/read", params={"uri": SCENE_REALISM_URI})))
        return result["contents"][0]["text"]

    document.write_text("version A", encoding="utf-8")
    assert read_text() == "version A"
    document.write_text("version B, longer", encoding="utf-8")
    assert read_text() == "version B, longer"


@pytest.mark.parametrize(
    "uri",
    [
        "cctv-pipeline://references/does-not-exist",
        "cctv-pipeline://references/",
        "https://example.invalid/scene-realism",
        SCENE_REALISM_URI + "/",
    ],
)
def test_unknown_uri_is_resource_not_found(
    offline: tuple[mcp.McpServer, ForbiddenTransport], uri: str
) -> None:
    server, transport = offline
    error = _rpc_error(server.handle(_rpc("resources/read", params={"uri": uri})), -32002)
    assert uri in error["message"]
    assert transport.calls == []


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"uri": None},
        {"uri": 7},
        {"uri": ["a"]},
        {"uri": {"x": 1}},
        {"uri": True},
        {"other": SCENE_REALISM_URI},
        [SCENE_REALISM_URI],
        SCENE_REALISM_URI,
    ],
)
def test_read_with_missing_or_non_string_uri_is_invalid_params(
    offline: tuple[mcp.McpServer, ForbiddenTransport], params: Any
) -> None:
    server, transport = offline
    _rpc_error(server.handle(_rpc("resources/read", params=params)), -32602)
    assert transport.calls == []


def test_resource_templates_list_is_empty(
    offline: tuple[mcp.McpServer, ForbiddenTransport],
) -> None:
    server, transport = offline
    assert _result(server.handle(_rpc("resources/templates/list"))) == {"resourceTemplates": []}
    assert transport.calls == []


@pytest.mark.parametrize("method", ["resources/subscribe", "resources/unsubscribe"])
def test_subscribe_and_unsubscribe_are_method_not_found(
    offline: tuple[mcp.McpServer, ForbiddenTransport], method: str
) -> None:
    server, _ = offline
    _rpc_error(server.handle(_rpc(method, params={"uri": SCENE_REALISM_URI})), -32601)


def test_missing_packaged_file_is_internal_error_naming_the_file(
    offline: tuple[mcp.McpServer, ForbiddenTransport], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, transport = offline
    missing = "no_such_document_xyz.md"
    assert not (RESOURCES_DIR / missing).exists()
    ghost = mcp.ReferenceResource("ghost", missing, "Ghost", "A document that is not packaged.")
    monkeypatch.setattr(mcp, "REFERENCE_RESOURCES", (*mcp.REFERENCE_RESOURCES, ghost))
    uri = mcp.RESOURCE_URI_PREFIX + "ghost"
    response = server.handle(_rpc("resources/read", params={"uri": uri}))
    error = _rpc_error(response, -32603)
    assert missing in error["message"]
    assert "result" not in response
    assert transport.calls == []


def test_tools_list_contains_no_resource(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = mcp.McpServer(AppTransport(client))
    result = _result(server.handle(_rpc("tools/list")))
    names = {tool["name"] for tool in result["tools"]}
    assert names
    forbidden = {r.slug for r in mcp.REFERENCE_RESOURCES} | {
        r.slug.replace("-", "_") for r in mcp.REFERENCE_RESOURCES
    }
    for name in names:
        assert not any(slug in name for slug in forbidden)
    for tool in result["tools"]:
        assert SCENE_REALISM_URI not in str(tool)


def test_every_shipped_resource_file_exists() -> None:
    assert mcp.REFERENCE_RESOURCES
    for resource in mcp.REFERENCE_RESOURCES:
        path = RESOURCES_DIR / resource.file
        assert path.is_file(), f"{resource.file} missing from {RESOURCES_DIR}"
        assert path.read_text(encoding="utf-8").strip()
