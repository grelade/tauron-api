from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from responses import matchers

from tauron_api import (
    ApiError,
    AuthenticationError,
    DocumentKind,
    MessageFolder,
    ParseError,
    PayerNotFoundError,
    TauronClient,
    TauronError,
)
from tauron_api.client import _months_ago

from .conftest import BASE, PAYER, PPE, add_html, add_json, fixture_json, fixture_text

LOGIN_URL = "https://logowanie.tauron.pl/realms/ext/protocol/cas/login?service=https://moj.tauron.pl"
LOGIN_ACTION = "https://logowanie.tauron.pl/realms/ext/login-actions/authenticate?session_code=abc"
LOGIN_FORM = f'<html><form id="kc-form-login" action="{LOGIN_ACTION}" method="post"></form></html>'
PDF_B64 = base64.b64encode(b"%PDF-1.4 fake").decode()


def query(call: responses.Call) -> dict[str, list[str]]:
    return parse_qs(urlparse(call.request.url).query, keep_blank_values=True)


def body(call: responses.Call) -> dict[str, list[str]]:
    raw = call.request.body or ""
    return parse_qs(raw if isinstance(raw, str) else raw.decode(), keep_blank_values=True)


def calls_to(rsps: responses.RequestsMock, path: str) -> list[responses.Call]:
    return [c for c in rsps.calls if urlparse(c.request.url).path == path]


def add_login(rsps: responses.RequestsMock, ok: bool = True) -> None:
    rsps.add("GET", BASE + "/", status=302, headers={"Location": LOGIN_URL})
    rsps.add("GET", LOGIN_URL, body=LOGIN_FORM, content_type="text/html")
    if ok:
        rsps.add(
            "POST",
            LOGIN_ACTION,
            status=302,
            headers={"Location": BASE + "/?ticket=ST-1"},
            match=[
                matchers.urlencoded_params_matcher(
                    {"username": "u", "password": "p", "credentialId": ""}, allow_blank=True
                )
            ],
        )
        rsps.add("GET", BASE + "/?ticket=ST-1", body="<html>pulpit</html>", content_type="text/html")
    else:
        rsps.add(
            "POST",
            LOGIN_ACTION,
            body='<span id="input-error">Nieprawidłowa nazwa użytkownika lub hasło.</span>',
            content_type="text/html; charset=utf-8",
        )


# ---- auth --------------------------------------------------------------------


def test_login_success(mocked: responses.RequestsMock) -> None:
    add_login(mocked)
    c = TauronClient("u", "p")
    c.login()
    assert c._logged_in


def test_login_bad_password(mocked: responses.RequestsMock) -> None:
    add_login(mocked, ok=False)
    with pytest.raises(AuthenticationError, match="Nieprawidłowa"):
        TauronClient("u", "p").login()


def test_login_requires_credentials() -> None:
    with pytest.raises(AuthenticationError):
        TauronClient().login()


def test_login_form_missing(mocked: responses.RequestsMock) -> None:
    mocked.add("GET", BASE + "/", status=302, headers={"Location": LOGIN_URL})
    mocked.add("GET", LOGIN_URL, body="<html>maintenance</html>", content_type="text/html")
    with pytest.raises(AuthenticationError, match="login form not found"):
        TauronClient("u", "p").login()


def test_request_logs_in_lazily(mocked: responses.RequestsMock) -> None:
    add_login(mocked)
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    payers = TauronClient("u", "p").get_payers()
    assert payers[0].number == PAYER
    assert len(calls_to(mocked, "/realms/ext/login-actions/authenticate")) == 1


def test_expired_session_relogs_once(mocked: responses.RequestsMock) -> None:
    mocked.add("GET", BASE + "/Home/Pages/Pulpit", status=302, headers={"Location": LOGIN_URL})
    add_login(mocked)
    add_html(mocked, "/Home/Pages/Pulpit", "dashboard.html")
    c = TauronClient("u", "p")
    c._logged_in = True  # e.g. restored from a stale session file
    assert c.get_payers()[0].number == PAYER
    assert len(calls_to(mocked, "/Home/Pages/Pulpit")) == 2


def test_expired_session_without_credentials(mocked: responses.RequestsMock) -> None:
    mocked.add("GET", BASE + "/Home/Pages/Pulpit", status=302, headers={"Location": LOGIN_URL})
    mocked.add("GET", LOGIN_URL, body=LOGIN_FORM, content_type="text/html")
    c = TauronClient()
    c._logged_in = True
    with pytest.raises(AuthenticationError):
        c.get_payers()


def test_session_file_roundtrip(mocked: responses.RequestsMock, tmp_path: Path) -> None:
    add_login(mocked)
    path = tmp_path / "s" / "session.json"
    c = TauronClient("u", "p", session_file=path)
    c.login()
    c.session.cookies.set("ASP.NET_SessionId", "abc", domain="moj.tauron.pl", path="/")
    c.close()
    saved = json.loads(path.read_text())
    assert any(ck["name"] == "ASP.NET_SessionId" for ck in saved)
    assert path.stat().st_mode & 0o777 == 0o600
    restored = TauronClient(session_file=path)
    assert restored._logged_in
    assert restored.session.cookies.get("ASP.NET_SessionId") == "abc"


def test_unreadable_session_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{not json")
    assert not TauronClient("u", "p", session_file=path)._logged_in


def test_logout(mocked: responses.RequestsMock, client: TauronClient, tmp_path: Path) -> None:
    mocked.add("GET", BASE + "/api/sitecore/Home/LogOut", body="")
    client.session_file = tmp_path / "session.json"
    client.session_file.write_text("[]")
    client.logout()
    assert not client._logged_in
    assert not client.session_file.exists()


# ---- payers ------------------------------------------------------------------


def test_get_payer_single(client: TauronClient, dashboard: None) -> None:
    assert client.get_payer().number == PAYER
    assert client.get_payer(PAYER).tariff == "G11"
    with pytest.raises(PayerNotFoundError):
        client.get_payer("999")


def test_get_payer_ambiguous(mocked: responses.RequestsMock, client: TauronClient) -> None:
    html = fixture_text("dashboard.html")
    start = html.index('<div class="row')
    end = html.index('<div id="ebokItemsEnd"')
    second = html[start:end].replace(PAYER, "87654321")
    mocked.add("GET", BASE + "/Home/Pages/Pulpit", body=html[:end] + second + html[end:])
    assert [p.number for p in client.get_payers()] == [PAYER, "87654321"]
    with pytest.raises(PayerNotFoundError, match="several payers"):
        client.get_payer()


def test_payers_load_more(mocked: responses.RequestsMock, client: TauronClient) -> None:
    """Accounts with a full first page (10 payers) fetch the rest via LoadMoreProduct."""
    html = fixture_text("dashboard.html")
    start = html.index('<div class="row')
    end = html.index('<div id="ebokItemsEnd"')
    row = html[start:end]
    rows = "".join(row.replace(PAYER, f"1000000{i}") for i in range(10))
    mocked.add("GET", BASE + "/Home/Pages/Pulpit", body=html[:start] + rows + html[end:])
    mocked.add(
        "POST",
        BASE + "/api/sitecore/EbokShortListPP/LoadMoreProduct",
        json={"ModelString": row.replace(PAYER, "20000000"), "ModelCount": 11},
    )
    payers = client.get_payers()
    assert len(payers) == 11 and payers[-1].number == "20000000"
    (call,) = calls_to(mocked, "/api/sitecore/EbokShortListPP/LoadMoreProduct")
    assert body(call)["size"] == ["10"]
    assert body(call)["ebokContractListSer"][0].startswith("{")


def test_set_payer_name(mocked: responses.RequestsMock, client: TauronClient, dashboard: None) -> None:
    mocked.add("POST", BASE + "/api/sitecore/EbokShortListPP/SetName", body='"Dom"')
    assert client.set_payer_name(None, "Dom") == "Dom"
    (call,) = calls_to(mocked, "/api/sitecore/EbokShortListPP/SetName")
    assert body(call) == {"contractorId": ["100000001"], "newName": ["Dom"]}


# ---- balance & open invoices -------------------------------------------------


@pytest.fixture
def invoices(mocked: responses.RequestsMock, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/Faktury", "invoices_page.html")
    add_json(
        mocked,
        "/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts",
        fixture_json("invoice_list.json"),
        "POST",
    )


def test_get_balances(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    (b,) = client.get_balances()
    assert b.total == Decimal("150.96")
    (call,) = calls_to(mocked, "/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts")
    form = body(call)
    assert form["startingIndex"] == ["0"]
    assert form["parentViewModel[DataSourceId]"] == ["614e0b1a-d41f-4a03-957d-1cb7cfb1abf3"]
    assert form["parentViewModel[BrakFakturLink][Url]"] == ["/Home/Pages/archiwum dokumentow"]


def test_get_balance_for_payer(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    assert client.get_balance().payer_number == PAYER
    page = calls_to(mocked, "/Home/Pages/Faktury")[0]
    assert query(page) == {"sid": ["T18"], "pp": [PAYER]}


def test_get_open_invoices(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    add_json(mocked, "/api/sitecore/EbokInvoices/GetInvoices", fixture_json("open_invoices.json"))
    (inv,) = client.get_open_invoices(type="fv", date_from=date(2026, 1, 1), number="E/1")
    assert inv.amount_to_pay == Decimal("150.96")
    q = query(calls_to(mocked, "/api/sitecore/EbokInvoices/GetInvoices")[0])
    assert q["contractorNumber"] == [PAYER]
    assert q["type"] == ["fv"]
    assert q["from"] == ["2026-01-01"]
    assert q["docNumber"] == ["E/1"]
    assert q["skip"] == ["0"] and q["take"] == ["50"]
    assert len(q["checkSum"][0]) == 64


def test_get_open_invoices_error(
    mocked: responses.RequestsMock, client: TauronClient, invoices: None
) -> None:
    add_json(mocked, "/api/sitecore/EbokInvoices/GetInvoices", {"status": "ERROR"})
    with pytest.raises(ApiError):
        client.get_open_invoices()


def test_export_open_invoices(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    add_json(
        mocked,
        "/api/sitecore/EbokInvoices/GetInvoicesExcel",
        {"Status": "OK", "Document": base64.b64encode(b"PK\x03\x04").decode(), "FileName": "Faktury.xlsx"},
    )
    f = client.export_open_invoices()
    assert f.filename == "Faktury.xlsx" and f.content == b"PK\x03\x04"


def test_get_installments(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    add_json(
        mocked,
        "/api/sitecore/EbokInvoices/GetInstallmentSchedule",
        [{"InstallmentNumber": 1, "Documents": [], "InstallmentAmount": 10, "SettledInstallmentAmount": 0}],
    )
    (inst,) = client.get_installments()
    assert inst.amount == Decimal(10) and not inst.paid
    q = query(calls_to(mocked, "/api/sitecore/EbokInvoices/GetInstallmentSchedule")[0])
    assert q["contractorNumber"] == [PAYER]


# ---- archive -----------------------------------------------------------------


@pytest.fixture
def archive(mocked: responses.RequestsMock, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/archiwum-dokumentow", "archive_page.html")
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", fixture_json("documents.json"))


def test_get_documents(mocked: responses.RequestsMock, client: TauronClient, archive: None) -> None:
    page = client.get_documents(
        date_from=date(2020, 1, 2), date_to=date(2026, 3, 4), kind=DocumentKind.INVOICE
    )
    assert page.total == 4
    q = query(calls_to(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments")[0])
    assert q["from"] == ["02.01.2020"] and q["to"] == ["04.03.2026"]
    assert q["docKind"] == ["invoice"]
    assert q["rangeFrom"] == ["1"] and q["rangeTo"] == ["100"]
    assert q["clientNumber"] == [PAYER] and q["system"] == ["T18"]


def test_get_documents_default_range(
    mocked: responses.RequestsMock, client: TauronClient, archive: None
) -> None:
    client.get_documents()
    q = query(calls_to(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments")[0])
    assert q["from"] == ["01.01.2000"]
    assert q["docKind"] == ["All"]


def test_iter_documents_pages(mocked: responses.RequestsMock, client: TauronClient, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/archiwum-dokumentow", "archive_page.html")
    data = fixture_json("documents.json")
    first = {**data, "Documents": data["Documents"][:2], "RangeTo": 2}
    second = {**data, "Documents": data["Documents"][2:], "RangeFrom": 3}
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", first)
    add_json(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments", second)
    docs = list(client.iter_documents(page_size=2))
    assert len(docs) == 4
    ranges = [
        query(c)["rangeFrom"][0] for c in calls_to(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocuments")
    ]
    assert ranges == ["1", "3"]


def test_find_and_download_document(
    mocked: responses.RequestsMock, client: TauronClient, archive: None
) -> None:
    add_json(
        mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentImage", {"Status": "OK", "Document": PDF_B64}
    )
    doc = client.find_document("E/TM2/AB345678/0003/26/1")
    assert doc is not None and doc.id == "551000195"
    f = client.download_document(doc)
    assert f.content.startswith(b"%PDF")
    assert f.filename == "E_TM2_AB345678_0003_26_1.pdf"
    by_id = client.find_document("551000182")
    assert by_id is not None and by_id.number == "E/TM2/AB345678/0003/26"
    assert client.find_document("E/NOPE") is None


def test_download_document_by_number(
    mocked: responses.RequestsMock, client: TauronClient, archive: None
) -> None:
    add_json(
        mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentImage", {"Status": "OK", "Document": PDF_B64}
    )
    assert client.download_document("E/TM2/AB345678/0003/26").content.startswith(b"%PDF")
    with pytest.raises(TauronError):
        client.download_document("E/NOPE")


def test_download_document_failure(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_json(
        mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentImage", {"Status": "Error", "StatusEnum": 1}
    )
    with pytest.raises(ApiError):
        client.download_document("/api/sitecore/EbokArchiveDocuments/GetDocumentImage?docNumber=1")


def test_download_open_invoice(mocked: responses.RequestsMock, client: TauronClient, invoices: None) -> None:
    add_json(mocked, "/api/sitecore/EbokInvoices/GetInvoices", fixture_json("open_invoices.json"))
    add_json(
        mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentImage", {"Status": "OK", "Document": PDF_B64}
    )
    (inv,) = client.get_open_invoices()
    assert client.download_document(inv).filename == "E_TM2_AB345678_0003_26_5.pdf"
    q = query(calls_to(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentImage")[0])
    assert q["docNumber"] == ["551000207"]


def test_export_documents(mocked: responses.RequestsMock, client: TauronClient, archive: None) -> None:
    add_json(
        mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentsExcel", {"Status": "OK", "Document": "UEs="}
    )
    f = client.export_documents(kind="correction")
    assert f.filename == "Lista_faktur.xlsx" and f.content == b"PK"
    q = query(calls_to(mocked, "/api/sitecore/EbokArchiveDocuments/GetDocumentsExcel")[0])
    assert q["docKind"] == ["correction"]


def test_table_context_missing(mocked: responses.RequestsMock, client: TauronClient, dashboard: None) -> None:
    mocked.add("GET", BASE + "/Home/Pages/archiwum-dokumentow", body="<html></html>")
    with pytest.raises(ParseError):
        client.get_documents()


# ---- payments ----------------------------------------------------------------


@pytest.fixture
def payments(mocked: responses.RequestsMock, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/lista-wplat", "payments_page.html")
    add_json(mocked, "/api/sitecore/EbokPaymentsHistory/GetPayments", fixture_json("payments.json"))


def test_get_payments_clamps_range(
    mocked: responses.RequestsMock, client: TauronClient, payments: None
) -> None:
    page = client.get_payments(date_from=date(2001, 1, 1), kind="wplata", amount_from=100)
    assert page.items[0].amount == Decimal("445.06")
    q = query(calls_to(mocked, "/api/sitecore/EbokPaymentsHistory/GetPayments")[0])
    assert q["from"] == [_months_ago(72).strftime("%d.%m.%Y")]
    assert q["docKind"] == ["wplata"]
    assert q["paymentFrom"] == ["100"] and q["paymentTo"] == [""]


def test_iter_payments(client: TauronClient, payments: None) -> None:
    assert [p.amount for p in client.iter_payments()] == [Decimal("445.06"), Decimal("304.0")]


def test_export_payments(mocked: responses.RequestsMock, client: TauronClient, payments: None) -> None:
    add_json(
        mocked, "/api/sitecore/EbokPaymentsHistory/GetPaymentsExcel", {"Status": "OK", "Document": "UEs="}
    )
    assert client.export_payments().filename == "Lista_wplat.xlsx"


def test_months_ago() -> None:
    assert _months_ago(72, date(2026, 11, 11)) == date(2020, 11, 12)
    assert _months_ago(1, date(2026, 3, 31)) == date(2026, 3, 1)  # day clamped to 28, then +1


# ---- messages ----------------------------------------------------------------


def test_get_messages(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_json(mocked, "/api/sitecore/EbokWiadomosci/GetSentMessages", fixture_json("messages.json"), "POST")
    page = client.get_messages(MessageFolder.SENT, search="ab")
    assert page.total == 3 and len(page.items) == 3
    form = body(calls_to(mocked, "/api/sitecore/EbokWiadomosci/GetSentMessages")[0])
    assert form["itemsPerPage"] == ["15"]
    assert "searchQuery" not in form  # shorter than 3 characters
    assert form["nowaWiadomoscNieprzeczytanaIkonaSrc"] == ["/icon/new-unread"]


def test_iter_messages(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_json(
        mocked, "/api/sitecore/EbokWiadomosci/GetReceivedMessages", fixture_json("messages.json"), "POST"
    )
    assert len(list(client.iter_messages())) == 3


def test_get_message_and_attachment(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_html(mocked, "/Home/Pages/szczegolywiadomosci", "message.html")
    mocked.add(
        "GET",
        BASE + "/api/sitecore/EbokSzczegolyWiadomosci/GetSystemAttachment",
        body=b"%PDF-1.7",
        content_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="Informacja.pdf"'},
    )
    msg = client.get_message(18685172)
    assert query(calls_to(mocked, "/Home/Pages/szczegolywiadomosci")[0]) == {"messageId": ["18685172"]}
    f = client.download_attachment(msg.attachments[0])
    assert f.filename == "Informacja.pdf" and f.content == b"%PDF-1.7" and f.content_type == "application/pdf"


def test_download_sod_attachment(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_json(
        mocked,
        "/api/sitecore/EbokSzczegolyWiadomosci/GetReceivedSodAttachment",
        {"status": "OK", "file": base64.b64encode(b"data").decode(), "name": "a.txt"},
    )
    f = client.download_sod_attachment("1", "abc")
    assert (f.filename, f.content) == ("a.txt", b"data")
    add_json(mocked, "/api/sitecore/EbokSzczegolyWiadomosci/GetReceivedSodAttachment", {"status": "ERROR"})
    with pytest.raises(ApiError):
        client.download_sod_attachment("2", "abc")


def test_archive_messages(mocked: responses.RequestsMock, client: TauronClient) -> None:
    mocked.add("POST", BASE + "/api/sitecore/EbokWiadomosci/ArchiveMessages", body="")
    client.archive_messages([1, "2"], archived=False)
    form = body(calls_to(mocked, "/api/sitecore/EbokWiadomosci/ArchiveMessages")[0])
    assert form == {"messageIds[]": ["1", "2"], "newArchiveStatus": ["false"]}


@pytest.mark.parametrize(
    ("enabled", "path"), [(True, "TurnOnEmailNotification"), (False, "TurnOffEmailNotification")]
)
def test_set_email_notifications(
    mocked: responses.RequestsMock, client: TauronClient, enabled: bool, path: str
) -> None:
    mocked.add("POST", BASE + f"/api/sitecore/EbokWiadomosci/{path}", body="")
    client.set_email_notifications(enabled)
    assert len(mocked.calls) == 1


# ---- contracts & co ----------------------------------------------------------


def test_contracts_and_usage_points(
    mocked: responses.RequestsMock, client: TauronClient, dashboard: None
) -> None:
    add_html(mocked, "/Home/Pages/umowa", "contracts.html")
    add_html(mocked, "/en/Home/Pages/umowa/punkty-poboru", "usage_points.html")
    add_html(mocked, "/Home/Pages/Umowy-nieaktywne", "inactive_contracts.html")
    assert client.get_contracts()[0].number == PAYER
    assert client.get_inactive_contracts() == []
    (up,) = client.get_usage_points()
    assert up.number == PPE
    q = query(calls_to(mocked, "/en/Home/Pages/umowa/punkty-poboru")[0])
    assert q["cid"] == [PAYER] and len(q["checksum"][0]) == 64


def test_bank_accounts(mocked: responses.RequestsMock, client: TauronClient, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/numery-kont", "accounts.html")
    assert client.get_bank_accounts()[0].account_number.startswith("00 1020")
    assert client.get_bank_accounts(PAYER)[0].payer_number == PAYER


def test_customer_documents_and_einvoice(mocked: responses.RequestsMock, client: TauronClient) -> None:
    add_html(mocked, "/Home/Pages/dokumenty-klienta", "customer_documents.html")
    add_html(mocked, "/Home/Pages/ustawienia/ustawienia-konta/zarzadzanieefaktura", "einvoice.html")
    assert len(client.get_customer_documents()) == 11
    assert client.get_invoice_email_settings()[0].email == "JAN.KOWALSKI@EXAMPLE.COM"


# ---- energy ------------------------------------------------------------------


@pytest.fixture
def energy(mocked: responses.RequestsMock, dashboard: None) -> None:
    add_html(mocked, "/Home/Pages/historia-odczytow-licznika-prad", "energy_list.html")
    add_html(mocked, "/Home/Pages/historia-odczytow-licznika-prad/szczegoly", "energy_page.html")


def test_meter_readings(mocked: responses.RequestsMock, client: TauronClient, energy: None) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/ByPoint", fixture_json("meter_points.json"))
    (point,) = client.get_meter_readings(date_from=date(2025, 1, 1), date_to=date(2026, 11, 11))
    assert len(point.readings) == 5
    q = query(calls_to(mocked, "/api/sitecore/EnergyConsumption/ByPoint")[0])
    assert q == {"billingNumber": [PAYER], "from": ["2025-01-01"], "to": ["2026-11-11"], "Pods": [PPE]}


def test_meter_readings_per_zone_clamped(
    mocked: responses.RequestsMock, client: TauronClient, energy: None
) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/ByPointAndZone", fixture_json("meter_points_zone.json"))
    client.get_meter_readings(date_from=date(2000, 1, 1), per_zone=True, usage_points=[])
    q = query(calls_to(mocked, "/api/sitecore/EnergyConsumption/ByPointAndZone")[0])
    assert q["from"] == [_months_ago(72).isoformat()]
    assert "Pods" not in q


def test_meter_readings_api_error(mocked: responses.RequestsMock, client: TauronClient, energy: None) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/ByPoint", {"success": False, "error": "Błąd"})
    with pytest.raises(ApiError):
        client.get_meter_readings()


def test_monthly_consumption_years(
    mocked: responses.RequestsMock, client: TauronClient, energy: None
) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/Monthly", fixture_json("monthly.json"))
    year = date.today().year
    client.get_monthly_consumption(years=[year, year - 1])
    calls = calls_to(mocked, "/api/sitecore/EnergyConsumption/Monthly")
    assert [query(c)["from"][0] for c in calls] == [f"{year - 1}-01-01", f"{year}-01-01"]
    assert all(query(c)["yearToYear"] == ["true"] for c in calls)
    assert query(calls[1])["to"] == [date.today().isoformat()]


def test_monthly_consumption_range(
    mocked: responses.RequestsMock, client: TauronClient, energy: None
) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/Monthly", fixture_json("monthly.json"))
    months = client.get_monthly_consumption(date_from=date(2025, 1, 1), date_to=date(2025, 12, 31))
    assert len(months) == 3
    assert "yearToYear" not in query(calls_to(mocked, "/api/sitecore/EnergyConsumption/Monthly")[0])


def test_export_meter_readings(mocked: responses.RequestsMock, client: TauronClient, energy: None) -> None:
    mocked.add(
        "GET",
        BASE + "/api/sitecore/EnergyConsumption/Export",
        body=fixture_text("energy.csv").encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=energy.csv"},
    )
    f = client.export_meter_readings()
    assert f.filename == "energy.csv"
    assert f.content.decode("utf-8-sig").startswith("DATA_ODCZYTU,")


def test_export_meter_readings_error(
    mocked: responses.RequestsMock, client: TauronClient, energy: None
) -> None:
    add_json(mocked, "/api/sitecore/EnergyConsumption/Export", {"success": False, "error": "Brak danych"})
    with pytest.raises(ApiError, match="Brak danych"):
        client.export_meter_readings()


def test_energy_page_missing_for_payer(
    mocked: responses.RequestsMock, client: TauronClient, dashboard: None
) -> None:
    mocked.add("GET", BASE + "/Home/Pages/historia-odczytow-licznika-prad", body="<html></html>")
    with pytest.raises(ParseError):
        client.get_meter_readings()
