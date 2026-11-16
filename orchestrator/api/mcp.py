"""Stdio MCP adapter over a running orchestrator server.

A pure client of ``orchestrator.api.app.serve``: tools are discovered from the server's
``GET /tool-manifest`` and each call is one loopback HTTP request carrying the loopback
token. It builds no app, job runner or store, so it cannot start a second job runner
beside the server's. See ``MCP_CONTRACT.md``.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import importlib.metadata
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO, NoReturn, Protocol
from urllib.parse import quote, urlencode

from orchestrator.platform.settings import Settings, SettingsError, load_settings

from .loopback_token import (
    INVALID_TOKEN_MESSAGE, TOKEN_ENV, TokenError, is_valid_token, read_token, redact,
)

TOOL_PREFIX = "pipeline_"
#: Newest first; the first entry is what a client asking for an unknown version gets.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05",
)

_DEFAULT_TIMEOUT_SECONDS = 120.0
_ASYNC_SUFFIX = " Asynchronous: answers 202 with a job record; poll pipeline_report_jobs for its state."
_COMPONENT_PREFIX = "#/components/schemas/"
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class HttpResponse:
    status: int
    content_type: str  # the Content-Type header, "" if absent
    body: bytes


class TransportError(Exception):
    """The server could not be reached or did not answer in time."""


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> HttpResponse:
        """Send one request; ``path`` is already substituted and percent-encoded.

        Any status, 4xx and 5xx included, is returned. Only failing to get a response
        at all raises ``TransportError``.
        """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect: urllib would forward the token header to its target.

    Returning None makes urllib raise ``HTTPError`` for the 3xx, which the transport
    turns back into an ordinary response.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LoopbackTransport:
    """HTTP to the orchestrator; the token only ever reaches ``base_url``."""

    def __init__(self, base_url: str, token: str, *, timeout: float) -> None:
        if not is_valid_token(token):
            raise ValueError(INVALID_TOKEN_MESSAGE)
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # ProxyHandler({}) because urllib otherwise honours HTTP_PROXY and, on Windows,
        # the registry proxy, either of which would hand the token to a third party.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        json_body: Any = None,
    ) -> HttpResponse:
        url = self._base_url + path
        if query:
            url += "?" + urlencode(query)
        headers = {"X-Loopback-Token": self._token, "Accept": "application/json"}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    return HttpResponse(
                        response.status, response.headers.get("Content-Type", ""), response.read()
                    )
            except urllib.error.HTTPError as error:
                # Every non-2xx, 3xx included, is a completed exchange rather than a failure.
                with error:
                    return HttpResponse(
                        error.code, error.headers.get("Content-Type", ""), error.read()
                    )
        except (OSError, http.client.HTTPException) as error:
            # URLError, refused connections and timeouts are all OSError. The token is
            # sent as a header and never appears in the URL or in these messages.
            raise TransportError(
                f"cannot reach the orchestrator server at {self._base_url}; it may not be "
                f"running ({type(error).__name__}: {error})"
            ) from error


def _localize_refs(node: Any, referenced: set[str]) -> Any:
    """Copy ``node`` with component references pointed at ``#/$defs/``, noting each name."""
    if isinstance(node, dict):
        copied: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith(_COMPONENT_PREFIX):
                name = value[len(_COMPONENT_PREFIX):]
                referenced.add(name)
                copied[key] = "#/$defs/" + name
            elif key == "discriminator" and isinstance(value, dict):
                # OpenAPI's discriminator mapping holds the same references as bare
                # strings under no "$ref" key, and would otherwise dangle.
                copied[key] = _localize_discriminator(value, referenced)
            else:
                copied[key] = _localize_refs(value, referenced)
        return copied
    if isinstance(node, list):
        return [_localize_refs(item, referenced) for item in node]
    return node


def _localize_discriminator(discriminator: dict[str, Any], referenced: set[str]) -> dict[str, Any]:
    mapping = discriminator.get("mapping")
    if not isinstance(mapping, dict):
        return dict(discriminator)
    localized: dict[str, Any] = {}
    for tag, target in mapping.items():
        if isinstance(target, str) and target.startswith(_COMPONENT_PREFIX):
            name = target[len(_COMPONENT_PREFIX):]
            referenced.add(name)
            localized[tag] = "#/$defs/" + name
        else:
            localized[tag] = target
    return {**discriminator, "mapping": localized}


def _self_contained(tool: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """The tool's input schema with the transitive closure of its components as ``$defs``."""
    referenced: set[str] = set()
    schema = _localize_refs(tool["input_schema"], referenced)
    defs: dict[str, Any] = {}
    pending = list(referenced)
    while pending:
        name = pending.pop()
        if name in defs:
            continue
        if name not in schemas:
            raise ValueError(
                f"tool {tool['name']!r} references component {name!r}, which the manifest "
                "does not define"
            )
        found: set[str] = set()
        defs[name] = _localize_refs(schemas[name], found)
        pending.extend(found - defs.keys())
    if defs:
        schema["$defs"] = dict(sorted(defs.items()))
    return schema


def _callable_tools(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Manifest tools an MCP client may call: uploads are excluded by design."""
    return [tool for tool in manifest["tools"] if tool["http"].get("body") != "multipart"]


def _require_routable(tool: Any) -> None:
    """Fail discovery, rather than the later call, when a tool cannot be routed."""
    http = tool.get("http") if isinstance(tool, dict) else None
    routable = (
        isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and isinstance(http, dict)
        and isinstance(http.get("method"), str)
        and isinstance(http.get("path"), str)
        and isinstance(http.get("path_params"), list)
        and isinstance(http.get("query_params"), list)
        and "body" in http
    )
    if not routable:
        name = tool.get("name") if isinstance(tool, dict) else None
        raise ValueError(
            f"manifest tool {name!r} lacks a name or a complete http routing block "
            "(method, path, path_params, query_params, body)"
        )


def tools_from_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a tool manifest into MCP tool definitions without mutating it."""
    for tool in manifest["tools"]:
        _require_routable(tool)
    schemas = manifest.get("components", {}).get("schemas", {})
    tools: list[dict[str, Any]] = []
    for tool in _callable_tools(manifest):
        description = tool.get("description", "")
        if tool.get("asynchronous"):
            description += _ASYNC_SUFFIX
        tools.append({
            "name": TOOL_PREFIX + tool["name"],
            "description": description,
            "inputSchema": _self_contained(tool, schemas),
            "annotations": {"readOnlyHint": tool["http"]["method"] == "GET"},
        })
    return tools


def _package_version() -> str:
    try:
        return importlib.metadata.version("cctv-pipeline")
    except importlib.metadata.PackageNotFoundError:
        # Running from a checkout that was never installed; the version is advisory.
        return "0+unknown"


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}],
        "isError": True,
    }


def _query_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _query_pairs(name: str, value: Any) -> list[tuple[str, str]]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [(name, _query_text(item)) for item in items if item is not None]


class _RpcError(Exception):
    """A JSON-RPC protocol error raised inside dispatch and rendered by ``handle``."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class McpServer:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._manifest: dict[str, Any] | None = None

    def handle(self, message: Any) -> dict[str, Any] | None:
        """One decoded JSON-RPC message in; the response, or None for a notification.

        Never raises: the last-resort ``except`` maps an unexpected exception to -32603
        with its message, which is safe because no string reachable here holds the token.
        """
        try:
            if (
                not isinstance(message, dict)
                or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
            ):
                return _error_response(_echoable_id(message), _INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            if "id" not in message:
                return None
            request_id = message["id"]
            if not _is_id(request_id):
                return _error_response(None, _INVALID_REQUEST, "id must be a string or an integer")
            try:
                result = self._dispatch(message["method"], message.get("params"))
            except _RpcError as error:
                return _error_response(request_id, error.code, error.message)
            except TransportError as error:
                return _error_response(request_id, _INTERNAL_ERROR, str(error))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:  # noqa: BLE001 - last resort, see the docstring
            if not isinstance(message, dict) or "id" not in message:
                # A notification has no reply channel, so stderr is the only place the
                # failure can be reported; dropping it would hide a bug.
                print(f"notification failed: {type(error).__name__}: {error}", file=sys.stderr)
                return None
            return _error_response(
                _echoable_id(message), _INTERNAL_ERROR, f"internal error: {type(error).__name__}: {error}"
            )

    def _dispatch(self, method: str, params: Any) -> dict[str, Any]:
        arguments = params if isinstance(params, dict) else {}
        if method == "initialize":
            requested = arguments.get("protocolVersion")
            return {
                "protocolVersion": (
                    requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                    else SUPPORTED_PROTOCOL_VERSIONS[0]
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cctv-pipeline", "version": _package_version()},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._refresh_manifest()}
        if method == "tools/call":
            if not isinstance(params, dict):
                raise _RpcError(_INVALID_PARAMS, "tools/call params must be an object")
            return self._call_tool(params)
        raise _RpcError(_METHOD_NOT_FOUND, f"method not found: {method}")

    def _refresh_manifest(self) -> list[dict[str, Any]]:
        """Fetch the manifest fresh, keep it for routing, and return the MCP tools."""
        response = self._transport.request("GET", "/tool-manifest")
        if response.status != 200:
            raise _RpcError(
                _INTERNAL_ERROR,
                f"GET /tool-manifest answered HTTP {response.status}; cannot list tools",
            )
        try:
            manifest = json.loads(response.body.decode("utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("tools"), list):
                raise ValueError("it is not an object with a 'tools' list")
            tools = tools_from_manifest(manifest)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise _RpcError(
                _INTERNAL_ERROR, f"GET /tool-manifest did not return a usable manifest: {error}"
            ) from error
        self._manifest = manifest
        return tools

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _RpcError(_INVALID_PARAMS, "tools/call requires a string 'name'")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(_INVALID_PARAMS, "'arguments' must be an object")
        if self._manifest is None:
            self._refresh_manifest()
        assert self._manifest is not None
        tool = next(
            (item for item in _callable_tools(self._manifest) if TOOL_PREFIX + item["name"] == name),
            None,
        )
        if tool is None:
            raise _RpcError(_INVALID_PARAMS, f"unknown tool: {name}")
        try:
            method, path, query, body = _route(tool["http"], arguments)
        except ValueError as error:
            return _tool_error(str(error))
        try:
            response = self._transport.request(method, path, query=query, json_body=body)
        except TransportError as error:
            return _tool_error(str(error))
        return _render(response)


def _route(
    http: dict[str, Any], arguments: dict[str, Any]
) -> tuple[str, str, list[tuple[str, str]], Any]:
    """Place each argument where the tool's ``http`` block says it goes.

    Raises ``ValueError`` naming the argument when the call cannot be routed.
    """
    path_params: list[str] = http["path_params"]
    query_params: list[str] = http["query_params"]
    has_body = http["body"] == "json"
    declared = {*path_params, *query_params, *(["payload"] if has_body else [])}
    for key in arguments:
        if key not in declared:
            raise ValueError(f"unknown argument {key!r}; this tool accepts {sorted(declared)}")
    path = http["path"]
    for key in path_params:
        value = arguments.get(key)
        if value is None:
            raise ValueError(f"missing required argument {key!r}")
        segment = str(value)
        # Encoding alone does not keep a value inside its segment: the server decodes
        # %2F back to "/" before matching routes, so "p/assets/categories" would reach a
        # different operation with the same verb than the tool the client approved.
        if segment in {"", ".", ".."} or "/" in segment:
            raise ValueError(
                f"argument {key!r} must be a single path segment: not empty, '.' or '..', "
                "and without '/'"
            )
        path = path.replace("{" + key + "}", quote(segment, safe=""))
    query: list[tuple[str, str]] = []
    for key in query_params:
        query.extend(_query_pairs(key, arguments.get(key)))
    body = arguments.get("payload") if has_body else None
    return http["method"], path, query, body


def _render(response: HttpResponse) -> dict[str, Any]:
    media_type = response.content_type.split(";", 1)[0].strip().lower()
    is_error = not 200 <= response.status < 300
    if media_type in _IMAGE_TYPES:
        block: dict[str, Any] = {
            "type": "image",
            "data": base64.b64encode(response.body).decode("ascii"),
            "mimeType": media_type,
        }
    else:
        text = response.body.decode("utf-8", errors="replace")
        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                text = json.dumps(
                    {"http_status": response.status, "body": json.loads(text)}, ensure_ascii=False
                )
            except ValueError:
                pass  # declared JSON but is not: the raw text is the honest answer
        block = {"type": "text", "text": text}
    return {"content": [block], "isError": is_error}


def _is_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _echoable_id(message: Any) -> Any:
    if isinstance(message, dict) and _is_id(message.get("id")):
        return message["id"]
    return None


def _reject_constant(name: str) -> Any:
    # Python's json accepts NaN and Infinity by default; JSON-RPC requires real JSON.
    raise ValueError(f"{name} is not valid JSON")


def serve_stdio(server: McpServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Newline-delimited JSON-RPC over byte streams until end of input.

    Only JSON-RPC lines are ever written to ``stdout``; diagnostics belong on stderr.
    """

    def send(response: dict[str, Any]) -> None:
        # A lone surrogate ("\ud800" is a legal JSON escape) cannot be encoded as UTF-8
        # and would end the loop. It can only occur inside a JSON string, where
        # backslashreplace writes exactly its JSON escape back, so the line stays valid.
        text = json.dumps(response, ensure_ascii=False)
        stdout.write(text.encode("utf-8", errors="backslashreplace") + b"\n")
        stdout.flush()

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError) as error:  # UnicodeDecodeError is a ValueError
            send(_error_response(None, _PARSE_ERROR, f"parse error: {error}"))
            continue
        if isinstance(message, list):
            send(_error_response(None, _INVALID_REQUEST, "batch requests are not supported"))
            continue
        response = server.handle(message)
        if response is not None:
            send(response)


def _base_url(settings: Settings) -> str:
    host = settings.server.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.server.port}"


class _NonEchoingParser(argparse.ArgumentParser):
    """Report a bad command line without repeating what was on it.

    argparse's own messages quote the offending values, so a token passed on argv by
    mistake would be copied to stderr, which MCP clients commonly write to a log file.
    The usage line still names every valid option. Raises rather than exiting, so
    ``main`` returns its status like every other failure path.
    """

    def error(self, message: str) -> NoReturn:
        raise _UsageError()


class _UsageError(Exception):
    """The command line was invalid; deliberately carries none of its values."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = _NonEchoingParser(
        prog="cctv-pipeline-mcp",
        description=f"MCP stdio server for a running orchestrator; the token comes from ${TOKEN_ENV}.",
    )
    parser.add_argument("--settings", help="settings.toml; the server address is read from it")
    parser.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS,
        help="seconds to wait for the orchestrator per request (default: %(default)s)",
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        token = read_token(arguments)
    except TokenError as error:
        print(error, file=sys.stderr)
        return 2
    try:
        args = parser.parse_args(arguments)
    except _UsageError:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: invalid arguments (values are not echoed). The token is read "
              f"from ${TOKEN_ENV}, never from the command line.", file=sys.stderr)
        return 2
    if not args.timeout > 0:
        print("--timeout must be a positive number of seconds.", file=sys.stderr)
        return 2
    try:
        # Only the address is needed, so the binary and directory checks that guard
        # starting the server itself are skipped.
        settings = load_settings(args.settings, verify_paths=False) if args.settings else Settings()
    except SettingsError as error:
        print(redact(str(error), token), file=sys.stderr)
        return 2
    transport = LoopbackTransport(_base_url(settings), token, timeout=args.timeout)
    try:
        serve_stdio(McpServer(transport), sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        pass  # the client closed its end; there is nobody left to answer
    return 0


if __name__ == "__main__":
    sys.exit(main())
