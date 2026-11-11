from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from tauron_ebok import cli

from .conftest import BASE, PAYER, add_html, add_json, fixture_json


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("TAURON_USER", "u")
    monkeypatch.setenv("TAURON_PASS", "p")
    monkeypatch.setattr(cli.TauronClient, "_ensure_login", lambda self: None)
    return tmp_path


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli.main(["--session-file", "", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def test_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TAURON_USER", raising=False)
    monkeypatch.delenv("TAURON_PASS", raising=False)
    code, _, err = run(["payers"], capsys)
    assert code == 2 and "TAURON_USER" in err


def test_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TAURON_USER", raising=False)
    (tmp_path / ".env").write_text("# comment\nTAURON_USER='a@b.pl'\nbroken line\n")
    cli.load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["TAURON_USER"] == "a@b.pl"


def test_payers(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    code, out, _ = run(["payers"], capsys)
    assert code == 0
    data = json.loads(out)
    assert data[0]["number"] == PAYER
    assert data[0]["next_payment_amount"] == "150.96"


def test_balance(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Faktury", "invoices_page.html")
    add_json(
        mocked,
        "/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts",
        fixture_json("invoice_list.json"),
        "POST",
    )
    code, out, _ = run(["balance"], capsys)
    assert code == 0 and json.loads(out)[0]["total"] == "150.96"


def test_pdf(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    add_html(mocked, "/Home/Pages/archiwum-dokumentow", "archive_page.html")
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", fixture_json("documents.json"))
    add_json(
        mocked,
        "/api/sitecore/EbokArchiveDocuments/GetDocumentImage",
        {"Status": "OK", "Document": "JVBERg=="},
    )
    code, out, _ = run(["pdf", "E/TM2/AB345678/0003/26", "--out", str(env) + "/"], capsys)
    assert code == 0
    saved = Path(out.strip())
    assert saved.name == "E_TM2_AB345678_0003_26.pdf" and saved.read_bytes() == b"%PDF"


def test_pdf_not_found(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    add_html(mocked, "/Home/Pages/archiwum-dokumentow", "archive_page.html")
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", fixture_json("documents.json"))
    code, _, err = run(["pdf", "E/NOPE"], capsys)
    assert code == 1 and "not found" in err


def test_documents_limit(
    env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]
) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    add_html(mocked, "/Home/Pages/archiwum-dokumentow", "archive_page.html")
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", fixture_json("documents.json"))
    code, out, _ = run(["documents", "--limit", "2", "--kind", "invoice"], capsys)
    assert code == 0 and len(json.loads(out)) == 2


def test_messages(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_json(
        mocked, "/api/sitecore/EbokWiadomosci/GetReceivedMessages", fixture_json("messages.json"), "POST"
    )
    code, out, _ = run(["messages"], capsys)
    assert code == 0 and [m["unread"] for m in json.loads(out)] == [False, True, True]


def test_message_with_attachments(
    env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]
) -> None:
    add_html(mocked, "/Home/Pages/szczegolywiadomosci", "message.html")
    mocked.add(
        "GET",
        BASE + "/api/sitecore/EbokSzczegolyWiadomosci/GetSystemAttachment",
        body=b"%PDF",
        content_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="info.pdf"'},
    )
    code, out, _ = run(["message", "18685172", "--attachments", str(env / "att")], capsys)
    data = json.loads(out)
    assert code == 0 and "body_html" not in data
    assert Path(data["saved"][0]).read_bytes() == b"%PDF"


def test_readings_csv(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    add_html(mocked, "/Home/Pages/historia-odczytow-licznika-prad", "energy_list.html")
    add_html(mocked, "/Home/Pages/historia-odczytow-licznika-prad/szczegoly", "energy_page.html")
    mocked.add(
        "GET",
        BASE + "/api/sitecore/EnergyConsumption/Export",
        body=b"DATA_ODCZYTU\n",
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=energy.csv"},
    )
    code, out, _ = run(["readings", "--csv", str(env / "out.csv")], capsys)
    assert code == 0 and Path(out.strip()).read_bytes() == b"DATA_ODCZYTU\n"


def test_usage_points(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    add_html(mocked, "/Home/Pages/umowa", "contracts.html")
    add_html(mocked, "/en/Home/Pages/umowa/punkty-poboru", "usage_points.html")
    code, out, _ = run(["usage-points"], capsys)
    data = json.loads(out)
    assert code == 0 and data[0]["tariff"] == "G11" and data[0]["meter_number"] == "S000000000001"


def test_payer_option(env: Path, mocked: responses.RequestsMock, capsys: pytest.CaptureFixture[str]) -> None:
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    code, _, err = run(["--payer", "999", "installments"], capsys)
    assert code == 1 and "999" in err
