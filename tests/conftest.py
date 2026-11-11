"""Shared fixtures. All HTTP is mocked with ``responses``; fixtures under tests/fixtures are
anonymised captures of real moj.tauron.pl pages and API responses."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import responses

from tauron_api import TauronClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://moj.tauron.pl"
PAYER = "12345678"
PPE = "590000000000000001"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


@pytest.fixture
def mocked() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def client(mocked: responses.RequestsMock) -> TauronClient:
    """A client that believes it is logged in (no login round-trip)."""
    c = TauronClient("user@example.com", "secret")
    c._logged_in = True
    return c


def add_html(rsps: responses.RequestsMock, path: str, fixture: str, method: str = "GET") -> None:
    rsps.add(method, BASE + path, body=fixture_text(fixture), content_type="text/html; charset=utf-8")


def add_json(rsps: responses.RequestsMock, path: str, data: Any, method: str = "GET") -> None:
    rsps.add(method, BASE + path, json=data)


@pytest.fixture
def dashboard(mocked: responses.RequestsMock) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
