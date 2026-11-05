from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import LOOPBACK_BASE, TOKEN, Op, operations, valid_request_kwargs


def _read_op(app: FastAPI) -> Op:
    # The contract guarantees reads exist and are unauthenticated; selecting from
    # the route table avoids pinning a particular endpoint name.
    return next(op for op in operations(app) if op.method == "GET")


def _assert_forbidden_without_token(response) -> None:
    assert response.status_code == 403
    assert TOKEN not in response.text


def test_attacker_host_is_forbidden(client: TestClient, app: FastAPI) -> None:
    op = _read_op(app)
    kwargs = valid_request_kwargs(app, op)
    response = client.request(
        "GET",
        op.url(),
        headers={"Host": "attacker.com", "Origin": LOOPBACK_BASE},
        **kwargs,
    )
    _assert_forbidden_without_token(response)


def test_foreign_host_without_origin_is_forbidden(
    client: TestClient, app: FastAPI
) -> None:
    op = _read_op(app)
    kwargs = valid_request_kwargs(app, op)
    response = client.request(
        "GET", op.url(), headers={"Host": "attacker.com"}, **kwargs
    )
    _assert_forbidden_without_token(response)


def test_foreign_host_is_refused_on_unauthenticated_read(
    client: TestClient, app: FastAPI
) -> None:
    op = _read_op(app)
    kwargs = valid_request_kwargs(app, op)
    response = client.request(
        "GET",
        op.url(),
        headers={"Host": "attacker.com", "Origin": LOOPBACK_BASE},
        **kwargs,
    )
    _assert_forbidden_without_token(response)


@pytest.mark.parametrize(
    ("host", "with_port"),
    [
        ("127.0.0.1", False),
        ("127.0.0.1", True),
        ("localhost", False),
        ("localhost", True),
        ("[::1]", False),
        ("[::1]", True),
    ],
)
def test_loopback_hosts_are_accepted(
    client: TestClient, app: FastAPI, settings, host: str, with_port: bool
) -> None:
    op = _read_op(app)
    kwargs = valid_request_kwargs(app, op)
    requested_host = f"{host}:{settings.server.port}" if with_port else host
    response = client.request(
        "GET", op.url(), headers={"Host": requested_host}, **kwargs
    )
    assert response.status_code != 403, (
        f"loopback Host {requested_host!r} was refused"
    )


def test_foreign_host_is_rejected_before_routing_or_handler(
    client: TestClient, app: FastAPI
) -> None:
    handler_calls: list[bool] = []

    def probe_handler() -> dict[str, str]:
        handler_calls.append(True)
        return {"result": "handler ran"}

    probe_path = "/__host_validation_handler_probe__"
    app.add_api_route(probe_path, probe_handler, methods=["GET"])
    response = client.get(probe_path, headers={"Host": "attacker.com"})
    _assert_forbidden_without_token(response)
    assert handler_calls == []

    # A missing route must be rejected by Host policy before it can become a 404.
    missing = client.get(
        "/__host_validation_path_that_does_not_exist__",
        headers={"Host": "attacker.com"},
    )
    _assert_forbidden_without_token(missing)
