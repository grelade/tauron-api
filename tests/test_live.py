"""Read-only smoke tests against the real moj.tauron.pl.

Skipped unless TAURON_LIVE=1 and TAURON_USER / TAURON_PASS are set (a .env file in the
project root works too). Run with: ``TAURON_LIVE=1 uv run pytest -m live``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tauron_api import TauronClient
from tauron_api.cli import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (
            os.environ.get("TAURON_LIVE") == "1"
            and os.environ.get("TAURON_USER")
            and os.environ.get("TAURON_PASS")
        ),
        reason="set TAURON_LIVE=1 and TAURON_USER/TAURON_PASS to run live tests",
    ),
]


@pytest.fixture(scope="module")
def live() -> Iterator[TauronClient]:
    with TauronClient(os.environ["TAURON_USER"], os.environ["TAURON_PASS"]) as c:
        yield c


def test_payers_and_balance(live: TauronClient) -> None:
    payers = live.get_payers()
    assert payers
    balance = live.get_balance(payers[0].number)
    assert balance.payer_number == payers[0].number


def test_open_invoices_and_installments(live: TauronClient) -> None:
    payer = live.get_payers()[0].number
    assert isinstance(live.get_open_invoices(payer), list)
    assert isinstance(live.get_installments(payer), list)


def test_documents_and_pdf(live: TauronClient) -> None:
    payer = live.get_payers()[0].number
    page = live.get_documents(payer, limit=5)
    assert page.total >= len(page.items)
    with_pdf = next((d for d in page.items if d.url), None)
    if with_pdf is not None:
        assert live.download_document(with_pdf).content.startswith(b"%PDF")


def test_payments(live: TauronClient) -> None:
    page = live.get_payments(live.get_payers()[0].number, limit=5)
    assert page.total >= len(page.items)


def test_messages(live: TauronClient) -> None:
    page = live.get_messages()
    assert page.total >= len(page.items)


def test_contract_data(live: TauronClient) -> None:
    payer = live.get_payers()[0].number
    assert live.get_contracts()
    assert live.get_usage_points(payer)
    assert live.get_bank_accounts(payer)
    assert live.get_customer_documents()
    assert isinstance(live.get_invoice_email_settings(), list)


def test_energy(live: TauronClient) -> None:
    payer = live.get_payers()[0].number
    points = live.get_meter_readings(payer)
    assert points and points[0].number
    assert isinstance(live.get_monthly_consumption(payer), list)
    assert live.export_meter_readings(payer).content
