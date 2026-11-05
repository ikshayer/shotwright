"""Adapter-absent operations are routed and typed, and answer 501 naming the adapter.

Only the 501 contract is tested; there is no behaviour behind these routes.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import TOKEN, TOKEN_HEADER, find_key, operations, valid_request_kwargs

# CONTRACT.md operation table, rows marked "adapter absent". One pattern per operation,
# matched against route path + function name.
ADAPTER_ABSENT = {
    "search assets": r"search.*asset|asset.*search",
    "create asset": r"(create|add|new).*asset|asset.*(create|add|new)",
    "build scene": r"scene",
    "render preview": r"preview|render",
    "generate media": r"generat",
    "sample references": r"sample|reference",
    "export": r"export",
}


def _matches(app: FastAPI, pattern: str):
    rx = re.compile(pattern, re.IGNORECASE)
    return [o for o in operations(app) if rx.search(f"{o.path} {o.name}")]


@pytest.mark.parametrize("operation", list(ADAPTER_ABSENT))
def test_adapter_absent_operation_is_routed_not_omitted(app: FastAPI, operation: str) -> None:
    assert _matches(app, ADAPTER_ABSENT[operation]), f"no route for contract operation {operation!r}"


@pytest.mark.parametrize("operation", list(ADAPTER_ABSENT))
def test_adapter_absent_operation_returns_501_naming_the_adapter(
    client: TestClient, app: FastAPI, operation: str
) -> None:
    ops = _matches(app, ADAPTER_ABSENT[operation])
    assert ops, f"no route for contract operation {operation!r}"
    for op in ops:
        resp = client.request(
            op.method, op.url(), headers={TOKEN_HEADER: TOKEN}, **valid_request_kwargs(app, op)
        )
        assert resp.status_code == 501, f"{op.key} answered {resp.status_code}: {resp.text}"
        names = [v for v in find_key(resp.json(), lambda k: "adapter" in k.lower()) if isinstance(v, str) and v]
        assert names, f"{op.key}: 501 body does not name the missing adapter: {resp.text}"
        assert TOKEN not in resp.text
