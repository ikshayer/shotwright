from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

import pytest

from orchestrator.adapters.comfyui.runtime import HttpTransport, build_command
from orchestrator.platform.errors import JobError, JobErrorKind
from orchestrator.platform.settings import Settings


class ResponseServer:
    def __init__(self, status: int, body: bytes) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_http_transport_speaks_only_to_loopback_and_parses_object_response() -> None:
    server = ResponseServer(200, b'{"system":{"python_version":"3.12"}}')
    try:
        assert HttpTransport(server.port).request("GET", "/system_stats", timeout=1) == {
            "system": {"python_version": "3.12"}}
    finally:
        server.close()


@pytest.mark.parametrize("body", [b"{", b"[]", b'"string"'])
def test_http_transport_rejects_malformed_or_non_object_json(body: bytes) -> None:
    server = ResponseServer(200, body)
    try:
        with pytest.raises(JobError) as caught:
            HttpTransport(server.port).request("GET", "/history/x", timeout=1)
        assert caught.value.kind is JobErrorKind.CORRUPT_ARTIFACT
    finally:
        server.close()


@pytest.mark.parametrize(("status", "kind"), [
    (400, JobErrorKind.PROVIDER_HTTP_4XX), (404, JobErrorKind.PROVIDER_HTTP_4XX),
    (429, JobErrorKind.PROVIDER_RATE_LIMIT), (503, JobErrorKind.WORKER_UNREACHABLE),
])
def test_http_statuses_map_to_closed_job_errors(status: int, kind: JobErrorKind) -> None:
    server = ResponseServer(status, b"failure")
    try:
        with pytest.raises(JobError) as caught:
            HttpTransport(server.port).request("GET", "/queue", timeout=1)
        assert caught.value.kind is kind
    finally:
        server.close()


def test_http_transport_rejects_untrusted_path_and_invalid_port() -> None:
    for port in (0, 65536):
        with pytest.raises((ValueError, JobError)):
            HttpTransport(port)
    transport = HttpTransport(8188)
    for path in ("http://example.com/x", "//example.com/x", "../history", "/../history"):
        with pytest.raises((ValueError, JobError)):
            transport.request("GET", path, timeout=0.1)
    for timeout in (float("inf"), float("nan"), 0, -1):
        with pytest.raises((ValueError, JobError)):
            transport.request("GET", "/system_stats", timeout=timeout)


def test_build_command_is_offline_contained_and_disables_ambient_features(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    runtime = tmp_path / "project" / "runtime"
    config = runtime / "model-paths.yaml"
    settings = Settings.model_validate({"project_root": str(tmp_path / "project"),
        "binaries": {"comfyui_root": str(root), "comfyui_port": 8191}})
    argv = build_command(settings, runtime_dir=runtime, model_config=config,
                         gpu_device=2, memory_mode="lowvram")
    assert argv[0] == str(root / "python_embeded" / "python.exe")
    assert "-s" in argv and "-B" in argv
    assert argv[3] == str(root / "ComfyUI" / "main.py")
    pairs = dict(pairwise(argv))
    assert pairs["--database-url"] == "sqlite:///:memory:"
    assert pairs["--front-end-root"] == str(runtime / "frontend")
    assert pairs["--base-directory"] == str(runtime)
    assert pairs["--extra-model-paths-config"] == str(config)
    for flag in ("--disable-auto-launch", "--disable-all-custom-nodes", "--disable-api-nodes",
                 "--preview-method", "--cuda-device", "--input-directory", "--output-directory",
                 "--temp-directory", "--user-directory", "--cache-none"):
        assert flag in argv
    assert argv[argv.index("--preview-method") + 1] == "none"
    assert argv[argv.index("--cuda-device") + 1] == "2"
    assert "--lowvram" in argv and "--novram" not in argv
    assert "--windows-standalone-build" not in argv
    assert "--whitelist-custom-nodes" not in argv or argv[argv.index("--whitelist-custom-nodes") + 1] == ""
