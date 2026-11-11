from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from tauron_ebok import parsers as P
from tauron_ebok.exceptions import ApiError, ParseError
from tauron_ebok.models import Installment, MessageFolder, MessagePage, to_jsonable

from .conftest import PAYER, PPE, fixture_json, fixture_text

# ---- primitives --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("150,96 zł", Decimal("150.96")),
        ("1 234,56 zł", Decimal("1234.56")),
        ("1\xa0234,56\xa0zł", Decimal("1234.56")),
        ("-125,34 zł", Decimal("-125.34")),
        ("0,00 zł", Decimal("0.00")),
        ("---", Decimal(0)),
        ("", Decimal(0)),
        (None, Decimal(0)),
    ],
)
def test_parse_pln(text: str | None, expected: Decimal) -> None:
    assert P.parse_pln(text) == expected


def test_parse_pln_rejects_garbage() -> None:
    with pytest.raises(ParseError):
        P.parse_pln("1-2-3")


def test_dates() -> None:
    assert P.parse_pl_date("Termin: 21.12.2026") == date(2026, 12, 21)
    assert P.parse_pl_date("brak") is None
    assert P.parse_iso_date("2026-08-04T00:00:00") == date(2026, 8, 4)
    assert P.parse_iso_date(None) is None


def test_jquery_params_nested() -> None:
    data = {"A": 1, "B": {"C": "x", "D": None}, "E": [True, False]}
    assert P.jquery_params(data, "vm") == [
        ("vm[A]", "1"),
        ("vm[B][C]", "x"),
        ("vm[B][D]", ""),
        ("vm[E][0]", "true"),
        ("vm[E][1]", "false"),
    ]


def test_to_jsonable() -> None:
    inst = Installment(1, ["E/1"], date(2026, 1, 2), Decimal("10.50"), Decimal(0))
    assert to_jsonable(inst) == {
        "number": 1,
        "documents": ["E/1"],
        "due_date": "2026-01-02",
        "amount": "10.50",
        "settled_amount": "0",
    }
    page = MessagePage(MessageFolder.SENT, 1, 15, 0, [])
    assert to_jsonable(page)["folder"] == "sent"


# ---- embedded state ----------------------------------------------------------


def test_extract_view_model() -> None:
    vm = P.extract_view_model(fixture_text("invoices_page.html"))
    assert vm["DataSourceId"] == "614e0b1a-d41f-4a03-957d-1cb7cfb1abf3"
    assert vm["PodgladFakturyLink"]["Url"] == "/podgladFaktury/id/"


def test_extract_view_model_missing() -> None:
    with pytest.raises(ParseError):
        P.extract_view_model("<html></html>")


def test_extract_contract_list_ser() -> None:
    ser = P.extract_contract_list_ser(fixture_text("dashboard.html"))
    assert ser is not None and ser.startswith('{"NaglowekPulpit"')
    assert P.extract_contract_list_ser("<html></html>") is None


def test_table_contexts() -> None:
    (archive,) = P.parse_table_contexts(fixture_text("archive_page.html"))
    assert archive.api_url == "/api/sitecore/EbokArchiveDocuments/GetDocuments"
    assert archive.client_number == PAYER
    assert archive.system == "T18"
    assert len(archive.checksum) == 64
    assert archive.excel_url == "/api/sitecore/EbokArchiveDocuments/GetDocumentsExcel"
    (payments,) = P.parse_table_contexts(fixture_text("payments_page.html"))
    assert payments.api_url.endswith("/GetPayments")


def test_energy_context_and_links() -> None:
    ctx = P.parse_energy_context(fixture_text("energy_page.html"))
    assert ctx.billing_number == PAYER
    assert ctx.usage_points == [PPE]
    assert ctx.by_point_url.endswith("/ByPoint")
    assert ctx.by_point_and_zone_url.endswith("/ByPointAndZone")
    assert ctx.max_months == 72
    links = P.parse_energy_detail_links(fixture_text("energy_list.html"))
    assert list(links) == [PAYER]
    assert "checksum=" in links[PAYER] and "&amp;" not in links[PAYER]
    with pytest.raises(ParseError):
        P.parse_energy_context("<html></html>")


# ---- pages -------------------------------------------------------------------


def test_parse_payers() -> None:
    (payer,) = P.parse_payers(fixture_text("dashboard.html"))
    assert payer.number == PAYER
    assert payer.agreement_id == "100000001"
    assert payer.system == "T18"
    assert payer.medium == "Prąd"
    assert payer.custom_name is None
    assert payer.address == "Ul. TESTOWA 1 / 2, 00-001 WARSZAWA"
    assert payer.contract == "UMOWA BEZ GWARANCJI CENY"
    assert payer.tariff == "G11"
    assert payer.invoice_delivery == "E-FAKTURA"
    assert payer.next_payment_amount == Decimal("150.96")
    assert payer.next_payment_due == date(2026, 12, 21)
    assert P.count_payer_rows(fixture_text("dashboard.html")) == 1


def test_parse_payers_custom_name() -> None:
    html = fixture_text("dashboard.html").replace("Nadaj nazwę własną", "Mieszkanie", 1)
    assert P.parse_payers(html)[0].custom_name == "Mieszkanie"


def test_parse_contractors() -> None:
    (c,) = P.parse_contractors(fixture_text("contracts.html"))
    assert c.number == PAYER
    assert c.usage_points_url is not None and "punkty-poboru" in c.usage_points_url
    assert "&amp;" not in c.usage_points_url
    assert P.parse_contractors(fixture_text("inactive_contracts.html")) == []


def test_parse_balances() -> None:
    ((balance, ctx),) = P.parse_balances(fixture_json("invoice_list.json")["ResultHtml"])
    assert balance.payer_number == PAYER
    assert balance.usage_point == PPE
    assert balance.total == Decimal("150.96")
    assert balance.overdue is not None and balance.overdue.total == Decimal("0.00")
    assert balance.current is not None and balance.current.receivable == Decimal("150.96")
    assert balance.combined is not None and balance.combined.interest_note == Decimal("0.00")
    assert ctx is not None
    assert ctx.contractor_number == PAYER
    assert ctx.source_id == "614e0b1a-d41f-4a03-957d-1cb7cfb1abf3"
    assert ctx.excel_url == "/api/sitecore/EbokInvoices/GetInvoicesExcel"
    assert ctx.installments_url is not None and "&amp;" not in ctx.installments_url


def test_parse_open_invoices() -> None:
    (inv,) = P.parse_open_invoices(fixture_json("open_invoices.json")["html"])
    assert inv.number == "E/TM2/AB345678/0003/26/5"
    assert inv.type == "Faktura"
    assert inv.issue_date == date(2026, 8, 4)
    assert inv.info is None
    assert inv.due_date == date(2026, 12, 21)
    assert inv.amount_to_pay == Decimal("150.96")
    assert inv.charge_id == "547030706"
    assert inv.document_url is not None and "GetDocumentImage" in inv.document_url


def test_parse_installments() -> None:
    items = P.parse_installments(
        [
            {
                "InstallmentNumber": 1,
                "Documents": ["E/1", "E/2"],
                "InstallmentPaymentDate": "2026-01-20T00:00:00",
                "InstallmentAmount": 100.5,
                "SettledInstallmentAmount": 100.5,
            },
            {"InstallmentNumber": 2, "InstallmentAmount": 99, "SettledInstallmentAmount": 0},
        ]
    )
    assert [i.number for i in items] == [1, 2]
    assert items[0].paid and not items[1].paid
    assert items[0].due_date == date(2026, 1, 20)
    assert P.parse_installments([]) == []


def test_parse_documents() -> None:
    page = P.parse_documents(fixture_json("documents.json"))
    assert page.total == 4
    first, _, correction, note = page.items
    assert first.id == "551000182"
    assert first.number == "E/TM2/AB345678/0003/26"
    assert first.amount_gross == Decimal("49.8")
    assert first.consumption_kwh == Decimal("19.0")
    assert first.issue_date == date(2026, 8, 4)
    assert first.url is not None and "GetDocumentImage" in first.url
    assert correction.doc_type == "FK" and correction.kind == 1 and correction.status == "Rozliczono"
    assert note.doc_type == "NB" and note.kind == 3


def test_parse_documents_error_status() -> None:
    with pytest.raises(ApiError):
        P.parse_documents({"Status": "Error", "StatusEnum": 1})
    with pytest.raises(ApiError):
        P.parse_documents("nope")


def test_parse_payments() -> None:
    page = P.parse_payments(fixture_json("payments.json"))
    assert page.total == 2
    pay = page.items[0]
    assert pay.number == "KP/RAP/TM_PKO/26/000830/05/000123"
    assert pay.amount == Decimal("445.06")
    assert pay.date == date(2026, 10, 19)
    assert len(pay.allocations) == 3
    alloc = pay.allocations[0]
    assert alloc.document_number == "E/TM2/AB345678/0003/26/2"
    assert alloc.settled_amount == Decimal("147.05")


def test_parse_base64_file() -> None:
    assert P.parse_base64_file({"Status": "OK", "Document": "QUJD", "FileName": " a.xlsx "}, "x") == (
        "a.xlsx",
        "QUJD",
    )
    assert P.parse_base64_file({"Status": "OK", "Document": "QUJD"}, "x") == (None, "QUJD")
    with pytest.raises(ApiError):
        P.parse_base64_file({"Status": "OK", "Document": ""}, "x")


def test_parse_message_list() -> None:
    items = P.parse_message_list(fixture_json("messages.json")["ModelString"])
    assert [m.id for m in items] == ["18685172", "15397199", "12455472"]
    assert items[0].date == date(2026, 5, 15)
    assert items[0].unread is False and items[1].unread is True


def test_parse_message() -> None:
    msg = P.parse_message(fixture_text("message.html"))
    assert msg.id == "18685172"
    assert msg.title == "Informacja wynikająca z Ustawy Prawo Energetyczne"
    assert msg.received_at == datetime(2026, 5, 15, 12, 56)
    assert msg.body.startswith("Dzień dobry,")
    assert "Wysłane załączniki" not in msg.body
    (att,) = msg.attachments
    assert att.name.endswith(".pdf")
    assert att.size == "94,37KB"
    assert att.url.startswith("/api/sitecore/EbokSzczegolyWiadomosci/GetSystemAttachment?attachmentId=3869&")


def test_parse_message_missing() -> None:
    with pytest.raises(ParseError):
        P.parse_message("<html><body></body></html>")


def test_parse_usage_points() -> None:
    (up,) = P.parse_usage_points(fixture_text("usage_points.html"))
    assert up.payer_number == PAYER
    assert up.number == PPE
    assert up.tariff == "G11"
    assert up.contracted_power == "5 kW"
    assert up.meter_number == "S000000000001"
    assert up.distributor == "TAURON Dystrybucja S.A."
    assert up.contract == "UMOWA BEZ GWARANCJI CENY"
    assert up.sections["Twoje dane"]["PESEL"] == "90*******12"
    assert up.sections["Dane z umowy"]["Adres e-mail do kontaktu"] == ""  # "Zmień" link stripped
    assert up.price_list == "OBROT"


def test_parse_bank_accounts() -> None:
    (acc,) = P.parse_bank_accounts(fixture_text("accounts.html"))
    assert acc.payer_number == PAYER
    assert acc.usage_point == PPE
    assert acc.purpose == "Płatności za fakturę"
    assert acc.account_number == "00 1020 0000 0000 1234 5678 0000"
    assert acc.recipient is not None and acc.recipient.startswith("TAURON Sprzedaż")
    assert acc.transfer_title == f"Numer płatnika: {PAYER}"


def test_parse_customer_documents() -> None:
    docs = P.parse_customer_documents(fixture_text("customer_documents.html"))
    assert len(docs) == 11
    assert docs[0].title == "Regulamin e-faktury"
    assert docs[0].size == "0,48 MB"
    assert docs[0].url.startswith("https://tauron.pl/-/media/")
    assert docs[-1].category.endswith("/ Prąd")


def test_parse_invoice_email_settings() -> None:
    (s,) = P.parse_invoice_email_settings(fixture_text("einvoice.html"))
    assert s.payer_number == PAYER
    assert s.email == "JAN.KOWALSKI@EXAMPLE.COM"
    assert s.address == "Ul. TESTOWA 1 2, 00-001 WARSZAWA"


def test_parse_meter_points() -> None:
    (point,) = P.parse_meter_points(fixture_json("meter_points.json"))
    assert point.number == PPE
    assert point.tariff == "G11"
    assert point.contracted_power == Decimal(5)
    assert len(point.readings) == 5
    first = point.readings[0]
    assert first.date == date(2025, 1, 2)
    assert first.consumption == Decimal(737)
    assert first.zones[0].zone == "AggregatedZone"
    assert first.zones[0].indication == Decimal(21633)
    (zoned,) = P.parse_meter_points(fixture_json("meter_points_zone.json"))
    assert zoned.readings[0].zones[0].zone == "Całodobowa"


def test_parse_energy_errors() -> None:
    with pytest.raises(ApiError, match="Błąd"):
        P.parse_meter_points({"success": False, "error": "Błąd podczas pobierania danych"})
    with pytest.raises(ApiError):
        P.parse_monthly("<html>")


def test_parse_monthly() -> None:
    months = P.parse_monthly(fixture_json("monthly.json"))
    assert [(m.year, m.month, m.consumption) for m in months] == [
        (2025, 1, Decimal(737)),
        (2025, 4, Decimal(463)),
        (2025, 7, Decimal(338)),
    ]
