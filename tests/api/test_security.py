"""FR-25 / AT-17: loopback only, cross-origin denied, token on every mutation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.platform.settings import ServerSettings, Settings

from .conftest import (
    EVIL_ORIGIN,
    LOOPBACK_BASE,
    TOKEN,
    TOKEN_HEADER,
    WRONG_TOKEN,
    Op,
    operations,
    valid_request_kwargs,
)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_non_loopback_bind_is_a_startup_error(host: str) -> None:
    with pytest.raises(ValidationError):
        ServerSettings(host=host)


def test_default_bind_is_loopback(settings: Settings) -> None:
    assert settings.server.host == "127.0.0.1"


def test_non_loopback_origin_is_refused(client: TestClient, app, op: Op) -> None:
    resp = client.request(
        op.method,
        op.url(),
        headers={"Origin": EVIL_ORIGIN, TOKEN_HEADER: TOKEN},
        **valid_request_kwargs(app, op),
    )
    # Amendment 1: a refused origin is 403, never 400. 400 means malformed; refusing an
    # origin is a policy decision. Tightened from "in (400, 403)", which was weaker than
    # the contract it tests.
    assert resp.status_code == 403, f"{op.key}: cross-origin got {resp.status_code}"
    assert resp.headers.get("access-control-allow-origin") != EVIL_ORIGIN
    assert TOKEN not in resp.text


def test_preflight_from_foreign_origin_grants_nothing(client: TestClient, op: Op) -> None:
    resp = client.options(
        op.url(),
        headers={"Origin": EVIL_ORIGIN, "Access-Control-Request-Method": op.method},
    )
    assert resp.headers.get("access-control-allow-origin") not in (EVIL_ORIGIN, "*")


def test_loopback_origin_is_not_refused_as_cross_origin(client: TestClient, app) -> None:
    read = next(o for o in operations(app) if not o.mutating)
    resp = client.request(read.method, read.url(), headers={"Origin": LOOPBACK_BASE})
    assert resp.status_code not in (401, 403)


def test_mutation_without_token_is_401(client: TestClient, app, op: Op) -> None:
    if not op.mutating:
        pytest.skip("read operation")
    resp = client.request(op.method, op.url(), **valid_request_kwargs(app, op))
    assert resp.status_code == 401
    assert TOKEN not in resp.text


def test_mutation_with_wrong_token_is_401_and_never_echoed(client: TestClient, app, op: Op) -> None:
    if not op.mutating:
        pytest.skip("read operation")
    resp = client.request(
        op.method, op.url(), headers={TOKEN_HEADER: WRONG_TOKEN}, **valid_request_kwargs(app, op)
    )
    assert resp.status_code == 401
    assert TOKEN not in resp.text
    assert WRONG_TOKEN not in resp.text


def test_mutation_with_correct_token_passes_auth(client: TestClient, app, op: Op) -> None:
    if not op.mutating:
        pytest.skip("read operation")
    resp = client.request(
        op.method, op.url(), headers={TOKEN_HEADER: TOKEN}, **valid_request_kwargs(app, op)
    )
    assert resp.status_code not in (401, 403)
    assert TOKEN not in resp.text


def test_read_without_token_is_not_auth_refused(client: TestClient, app, op: Op) -> None:
    if op.mutating:
        pytest.skip("mutating operation")
    resp = client.request(op.method, op.url(), **valid_request_kwargs(app, op))
    assert resp.status_code not in (401, 403)


def test_route_table_only_uses_known_verbs(app) -> None:
    assert {o.method for o in operations(app)} <= {"GET", "POST", "PUT", "PATCH", "DELETE"}
