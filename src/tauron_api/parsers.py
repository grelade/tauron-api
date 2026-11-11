"""Pure parsing functions: HTML / JSON from moj.tauron.pl -> models.

Nothing in here does I/O, so every function can be tested against saved fixtures.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from .exceptions import ApiError, ParseError
from .models import (
    Attachment,
    Balance,
    BalanceBreakdown,
    BankAccount,
    Contractor,
    CustomerDocument,
    Document,
    DocumentPage,
    Installment,
    InvoiceEmailSetting,
    Message,
    MessageSummary,
    MeterPoint,
    MonthlyConsumption,
    OpenInvoice,
    Payer,
    Payment,
    PaymentAllocation,
    PaymentPage,
    Reading,
    UsagePoint,
    ZoneReading,
)

NO_CUSTOM_NAME = "Nadaj nazwę własną"  # placeholder shown when a payer has no custom name

# ---- primitives --------------------------------------------------------------


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def text_of(el: Tag | None, sep: str = " ") -> str:
    """Element text with whitespace collapsed ("" for None)."""
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(sep)).strip()


def parse_pln(text: str | None) -> Decimal:
    """'1 234,56 zł' -> Decimal('1234.56'); '', '-', '---' -> 0."""
    if not text:
        return Decimal(0)
    num = re.sub(r"[^\d,.\-]", "", text).replace(",", ".")
    if num.count(".") > 1:  # thousands separators
        head, _, tail = num.rpartition(".")
        num = head.replace(".", "") + "." + tail
    if not re.search(r"\d", num):  # "", "-", "---" placeholders
        return Decimal(0)
    try:
        return Decimal(num)
    except InvalidOperation as e:
        raise ParseError(f"not an amount: {text!r}") from e


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def parse_pl_date(text: str | None) -> date | None:
    """First DD.MM.YYYY in the text."""
    if not text:
        return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def parse_iso_date(text: str | None) -> date | None:
    """'2026-08-04T00:00:00' -> date(2026, 8, 4)."""
    if not text:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _strip_label(text: str, label_pattern: str) -> str:
    return re.sub(label_pattern, "", text, flags=re.I).strip()


def _dl_pairs(dl: Tag) -> dict[str, str]:
    """<dl><dt>label</dt><dd>value</dd>...</dl> -> {label: value}. Labels lose a trailing colon."""
    out: dict[str, str] = {}
    label: str | None = None
    for child in dl.find_all(["dt", "dd"], recursive=False):
        if child.name == "dt":
            label = text_of(child).rstrip(":").strip()
        elif label is not None:
            # skip "Zmień"/"Kopiuj" action links, keep the value itself
            for a in child.select("a.action-link, a.copy-account, a.copy-data-link"):
                a.decompose()
            value = text_of(child)
            if label in out:
                if value:
                    out[label] = f"{out[label]} {value}".strip()
            else:
                out[label] = value
    return out


def _custom_name(header: Tag | None) -> str | None:
    name = text_of(header)
    return None if not name or name == NO_CUSTOM_NAME else name


def _address(el: Tag | None) -> str:
    return re.sub(r"\s*,\s*", ", ", text_of(el)).strip(" ,")


def _query_param(url: str, name: str) -> str | None:
    values = parse_qs(urlparse(url.replace("&amp;", "&")).query).get(name)
    return values[0] if values else None


def _payer_number(text: str) -> str | None:
    m = re.search(r"(?:płatnika|adresata)[^:]*:\s*(\d+)", text, re.I)
    return m.group(1) if m else None


def _usage_point_number(text: str) -> str | None:
    m = re.search(r"punktu poboru:\s*(\d+)", text, re.I)
    return m.group(1) if m else None


# ---- embedded page state -----------------------------------------------------


def extract_view_model(html: str) -> dict[str, Any]:
    """The ``var serializedViewModel = {...};`` object of the Faktury page."""
    marker = "var serializedViewModel = "
    i = html.find(marker)
    if i < 0:
        raise ParseError("serializedViewModel not found")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
    except json.JSONDecodeError as e:
        raise ParseError("serializedViewModel is not valid JSON") from e
    if not isinstance(obj, dict):
        raise ParseError("serializedViewModel is not an object")
    return obj


def extract_contract_list_ser(html: str) -> str | None:
    """The ``ebokContractListSer: '{...}'`` JSON string that list pages echo back to Search/LoadMore."""
    m = re.search(r"ebokContractListSer:\s*'(\{.*?\})'", html)
    return m.group(1) if m else None


def jquery_params(obj: Any, prefix: str) -> list[tuple[str, str]]:
    """Encode nested data the way jQuery's ``$.param`` does (``a[b][c]=v``)."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += jquery_params(v, f"{prefix}[{k}]")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += jquery_params(v, f"{prefix}[{i}]")
    elif obj is None:
        out.append((prefix, ""))
    elif isinstance(obj, bool):
        out.append((prefix, "true" if obj else "false"))
    else:
        out.append((prefix, str(obj)))
    return out


@dataclass
class TableContext:
    """Attributes of a ``[data-ajaxfill]`` block (archive / payments) needed to call its JSON API."""

    api_url: str
    client_number: str
    checksum: str
    system: str
    excel_url: str | None


def parse_table_contexts(html: str) -> list[TableContext]:
    out = []
    for el in soup_of(html).select("[data-ajaxfill]"):
        try:
            out.append(
                TableContext(
                    api_url=str(el["data-api-url"]),
                    client_number=str(el["data-client-number"]),
                    checksum=str(el["data-client-checksum"]),
                    system=str(el.get("data-system") or "T18"),
                    excel_url=str(el["data-api-excel-url"]) if el.get("data-api-excel-url") else None,
                )
            )
        except KeyError as e:
            raise ParseError(f"data-ajaxfill block without {e}") from e
    return out


@dataclass
class InvoicesContext:
    """Per-payer attributes from the Faktury page needed for GetInvoices / GetInstallmentSchedule."""

    contractor_number: str
    checksum: str
    source_id: str | None
    invoices_url: str
    excel_url: str | None
    installments_url: str | None


@dataclass
class EnergyContext:
    """Attributes of the energy consumption page (historia odczytów licznika)."""

    billing_number: str
    by_point_url: str
    by_point_and_zone_url: str
    monthly_url: str
    export_url: str
    usage_points: list[str]
    max_months: int = 72


def parse_energy_context(html: str) -> EnergyContext:
    soup = soup_of(html)
    root = soup.select_one("[data-api-by-point]")
    if root is None:
        raise ParseError("energy consumption container not found")
    points = []
    for cb in soup.select("[data-point-id]"):
        pid = str(cb["data-point-id"])
        if pid not in points:
            points.append(pid)
    months = soup.select_one("[data-before-months]")
    return EnergyContext(
        billing_number=str(root.get("data-billing-number") or ""),
        by_point_url=str(root["data-api-by-point"]),
        by_point_and_zone_url=str(root.get("data-api-by-point-and-zone") or root["data-api-by-point"]),
        monthly_url=str(root.get("data-api-monthly") or "/api/sitecore/EnergyConsumption/Monthly"),
        export_url=str(root.get("data-api-export") or "/api/sitecore/EnergyConsumption/Export"),
        usage_points=points,
        max_months=int(str(months["data-before-months"])) if months else 72,
    )


def parse_energy_detail_links(html: str) -> dict[str, str]:
    """Meter-reading list page -> {payer number: details page URL (with checksum)}."""
    out: dict[str, str] = {}
    for a in soup_of(html).select("a[href*='historia-odczytow-licznika'][href*='szczegoly']"):
        href = str(a["href"]).replace("&amp;", "&")
        cid = _query_param(href, "cid")
        if cid:
            out.setdefault(cid, href)
    return out


# ---- dashboard / payers ------------------------------------------------------


def _contractor_rows(soup: BeautifulSoup | Tag) -> list[Tag]:
    container = soup.select_one("#ebokItems") if isinstance(soup, BeautifulSoup) else soup
    if container is None:
        container = soup
    return [r for r in container.find_all("div", class_="row", recursive=False)]


def _medium(row: Tag) -> str:
    span = row.select_one(".col-sm-1 span")
    return text_of(span)


def _system_from_links(row: Tag) -> str | None:
    for a in row.select("a[href*='sid=']"):
        sid = _query_param(str(a["href"]), "sid")
        if sid:
            return sid
    return None


def count_payer_rows(html: str) -> int:
    """Number of payer rows in a list page / Search / LoadMore HTML fragment."""
    return sum(1 for row in _contractor_rows(soup_of(html)) if _payer_number(text_of(row)))


def parse_payers(html: str) -> list[Payer]:
    """Dashboard (Pulpit) contract list, or the HTML returned by EbokShortListPP/Search."""
    soup = soup_of(html)
    payers = []
    for row in _contractor_rows(soup):
        number = _payer_number(text_of(row))
        if not number:
            continue
        agreement = row.select_one("[data-agreement-id]")
        dl = row.select_one("dl")
        info = _dl_pairs(dl) if dl else {}
        amount_col = row.select_one(".amount-column")
        amount = amount_col.select_one(".amount") if amount_col else None
        due = amount_col.select_one(".date") if amount_col else None
        payers.append(
            Payer(
                number=number,
                agreement_id=str(agreement["data-agreement-id"]) if agreement else None,
                system=_system_from_links(row) or "T18",
                medium=_medium(row),
                custom_name=_custom_name(row.select_one(".header-container h3")),
                address=_address(row.select_one(".float-box p")),
                contract=info.get("Umowa"),
                service=info.get("Usługa"),
                tariff=info.get("Taryfa"),
                invoice_delivery=info.get("Faktura"),
                next_payment_amount=parse_pln(text_of(amount)) if amount else None,
                next_payment_due=parse_pl_date(text_of(due)),
            )
        )
    return payers


def parse_contractors(html: str) -> list[Contractor]:
    """Contract list (Umowa i dane / Umowy nieaktywne) or the HTML of their Search endpoints."""
    soup = soup_of(html)
    out = []
    for row in _contractor_rows(soup):
        number = _payer_number(text_of(row))
        if not number:
            continue
        agreement = row.select_one("[data-agreement-id]")
        link = row.select_one("a[href*='punkty-poboru']")
        out.append(
            Contractor(
                number=number,
                agreement_id=str(agreement["data-agreement-id"]) if agreement else None,
                medium=_medium(row),
                custom_name=_custom_name(row.select_one(".header-container h3")),
                address=_address(row.select_one(".float-box p")),
                usage_points_url=str(link["href"]).replace("&amp;", "&") if link else None,
            )
        )
    return out


# ---- balance / open invoices -------------------------------------------------


def _breakdown(dl: Tag | None) -> BalanceBreakdown | None:
    if dl is None:
        return None
    pairs = _dl_pairs(dl)
    return BalanceBreakdown(
        receivable=parse_pln(pairs.get("Należność")),
        interest_note=parse_pln(pairs.get("Nota odsetkowa")),
        total=parse_pln(pairs.get("Łącznie")),
    )


def _section_dl(container: Tag | None, title: str) -> Tag | None:
    if container is None:
        return None
    for sub in container.select(".subheader"):
        if text_of(sub) == title:
            dl = sub.find_next_sibling("dl")
            return dl if isinstance(dl, Tag) else None
    return None


def _next_div(el: Tag, cls: str) -> Tag | None:
    found = el.find_next_sibling("div", class_=cls)
    return found if isinstance(found, Tag) else None


def parse_balances(result_html: str) -> list[tuple[Balance, InvoicesContext | None]]:
    """``ResultHtml`` of EbokInvoiceList/LoadMoreInvoicesProducts -> balances (+ API context per payer)."""
    soup = soup_of(result_html)
    out = []
    for row in soup.select("div.invoice-row"):
        text = text_of(row)
        number = _payer_number(text)
        if not number:
            raise ParseError("invoice row without payer number")
        details = _next_div(row, "row-details-container")
        table = _next_div(row, "row-table-container")
        nxt = _next_div(row, "invoice-row")
        # only take siblings that belong to this row
        if nxt is not None:
            if details is not None and nxt in details.find_previous_siblings("div"):
                details = None
            if table is not None and nxt in table.find_previous_siblings("div"):
                table = None
        amount_col = row.select_one(".amount-column")
        headline = amount_col.select(".amount") if amount_col else []
        balance = Balance(
            payer_number=number,
            medium=_medium(row),
            address=_address(row.select_one(".float-box p")),
            usage_point=_usage_point_number(text),
            total=parse_pln(text_of(headline[-1])) if headline else Decimal(0),
            overdue=_breakdown(_section_dl(details, "Przeterminowane")),
            current=_breakdown(_section_dl(details, "Bieżące")),
            combined=_breakdown(_section_dl(details, "Razem")),
        )
        ctx = None
        inv = table.select_one("[data-invoices]") if table else None
        if inv is not None:
            excel = table.select_one("[data-download-excel-url]") if table else None
            inst = table.select_one(".installment-schedule-wrapper[data-api-url]") if table else None
            ctx = InvoicesContext(
                contractor_number=str(inv.get("data-contractor-number") or number),
                checksum=str(inv.get("data-contractor-number-checksum") or ""),
                source_id=str(inv["data-source-id"]) if inv.get("data-source-id") else None,
                invoices_url=str(inv.get("data-invoices-url") or "/api/sitecore/EbokInvoices/GetInvoices"),
                excel_url=str(excel["data-download-excel-url"]) if excel else None,
                installments_url=str(inst["data-api-url"]).replace("&amp;", "&") if inst else None,
            )
        out.append((balance, ctx))
    return out


def parse_open_invoices(rows_html: str) -> list[OpenInvoice]:
    """``html`` field of EbokInvoices/GetInvoices (table rows)."""
    out = []
    for tr in soup_of(rows_html).find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        link = tds[0].find("a")
        type_div = tds[0].find("div")
        charge = tr.select_one("[data-charge-id]")
        amount = tds[4].select_one(".amount")
        info = text_of(tds[2])
        out.append(
            OpenInvoice(
                number=text_of(link) if link else text_of(tds[0]),
                type=text_of(type_div),
                issue_date=parse_pl_date(text_of(tds[1])),
                info=None if info in ("", "-", "---") else info,
                due_date=parse_pl_date(text_of(tds[3])),
                amount_to_pay=parse_pln(text_of(amount) if amount else text_of(tds[4])),
                charge_id=str(charge["data-charge-id"]) if charge else None,
                document_url=str(link["data-base"]).replace("&amp;", "&")
                if link and link.get("data-base")
                else None,
            )
        )
    return out


def parse_installments(data: Any) -> list[Installment]:
    if not data:
        return []
    return [
        Installment(
            number=int(item.get("InstallmentNumber") or 0),
            documents=list(item.get("Documents") or []),
            due_date=parse_iso_date(item.get("InstallmentPaymentDate")),
            amount=to_decimal(item.get("InstallmentAmount")) or Decimal(0),
            settled_amount=to_decimal(item.get("SettledInstallmentAmount")) or Decimal(0),
        )
        for item in data
    ]


# ---- archive / payments JSON -------------------------------------------------


def _check_status(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiError(f"{what}: unexpected response", data)
    status = data.get("Status") or data.get("status")
    if status != "OK":
        raise ApiError(f"{what}: status {status!r}", data)
    return data


def parse_document(d: dict[str, Any]) -> Document:
    return Document(
        id=str(d.get("DocNumber") or ""),
        number=str(d.get("DocSignature") or ""),
        doc_type=str(d.get("DocType") or ""),
        doc_group=d.get("DocGroup"),
        kind=int(d.get("DocKind") or 0),
        kind_label=str(d.get("DocKindValue") or ""),
        is_correction=bool(d.get("IsDocumentCorrection")),
        amount_gross=to_decimal(d.get("AmountGross")) or Decimal(0),
        amount_net=to_decimal(d.get("AmountNet")),
        issue_date=parse_iso_date(d.get("IssueDate")),
        due_date=parse_iso_date(d.get("PaymentDate")),
        consumption_kwh=to_decimal(d.get("ConsumeEnergy")),
        status=str(d.get("Status") or ""),
        customer_number=str(d.get("CustomerNumber") or ""),
        sod_id=d.get("SodId") or None,
        url=d.get("Url") or None,
        details_url=d.get("UrlDetails") or None,
        serial_number=d.get("SerialNumber"),
    )


def parse_documents(data: Any) -> DocumentPage:
    """EbokArchiveDocuments/GetDocuments JSON."""
    data = _check_status(data, "GetDocuments")
    return DocumentPage(
        total=int(data.get("TotalNumber") or 0),
        range_from=int(data.get("RangeFrom") or 0),
        range_to=int(data.get("RangeTo") or 0),
        items=[parse_document(d) for d in data.get("Documents") or []],
    )


def parse_payment(p: dict[str, Any]) -> Payment:
    return Payment(
        number=str(p.get("DocNumber") or ""),
        date=parse_iso_date(p.get("Date")),
        booking_date=parse_iso_date(p.get("PaymentDateSystem")),
        amount=to_decimal(p.get("Amount")) or Decimal(0),
        type=str(p.get("Type") or ""),
        customer_number=str(p.get("CustomerNumber") or ""),
        allocations=[
            PaymentAllocation(
                document_number=str(a.get("DocSignature") or ""),
                address=a.get("Address"),
                due_date=parse_iso_date(a.get("PaymentDate")),
                document_amount=to_decimal(a.get("PaymentAmount")) or Decimal(0),
                settled_amount=to_decimal(a.get("PaymentSettlementAmount")) or Decimal(0),
            )
            for a in p.get("Details") or []
        ],
        details_url=p.get("Url") or None,
    )


def parse_payments(data: Any) -> PaymentPage:
    """EbokPaymentsHistory/GetPayments JSON."""
    data = _check_status(data, "GetPayments")
    return PaymentPage(
        total=int(data.get("TotalNumber") or 0),
        range_from=int(data.get("RangeFrom") or 0),
        range_to=int(data.get("RangeTo") or 0),
        items=[parse_payment(p) for p in data.get("Documents") or []],
    )


def parse_base64_file(data: Any, what: str) -> tuple[str | None, str]:
    """``{Status: OK, Document: <base64>, FileName?}`` -> (filename, base64 payload)."""
    data = _check_status(data, what)
    payload = data.get("Document") or data.get("document")
    if not payload:
        raise ApiError(f"{what}: empty document", data)
    name = data.get("FileName") or data.get("fileName")
    return (str(name).strip() or None) if name else None, str(payload)


# ---- messages ----------------------------------------------------------------

UNREAD_ICON_MARKER = "unread"


def parse_message_list(model_html: str) -> list[MessageSummary]:
    """``ModelString`` of EbokWiadomosci/Get*Messages.

    The client passes icon URLs containing ``unread`` for the unread variants, so the
    icon tells whether a message is unread.
    """
    out = []
    for li in soup_of(model_html).find_all("li"):
        head = li.select_one(".email-header-details[data-message-id]")
        if head is None:
            continue
        title = head.select_one(".email-title")
        divs = [d for d in head.find_all("div", recursive=False) if d is not title]
        icon = li.select_one("img.email-icon")
        src = str(icon.get("src") or "") if icon else ""
        out.append(
            MessageSummary(
                id=str(head["data-message-id"]),
                title=text_of(title),
                date=parse_pl_date(" ".join(text_of(d) for d in divs)),
                unread=(UNREAD_ICON_MARKER in src) if src else None,
            )
        )
    return out


def parse_message(html: str, message_id: str | None = None) -> Message:
    """Message details page (szczegolywiadomosci)."""
    soup = soup_of(html)
    panel = soup.select_one(".message-details-view .panel[data-message-id]") or soup.select_one(
        ".message-details-view"
    )
    if panel is None:
        raise ParseError("message details not found")
    title = panel.select_one(".email-title")
    received: datetime | None = None
    header = panel.select_one(".email-header-details")
    if header is not None:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4}),?\s*(\d{1,2}):(\d{2})", text_of(header))
        if m:
            d, mo, y, h, mi = (int(x) for x in m.groups())
            received = datetime(y, mo, d, h, mi)
    body = panel.select_one(".panel-body") or panel
    attachments = []
    att_list = body.select_one(".email-attachment-list")
    if att_list is not None:
        for div in att_list.find_all("div", recursive=False):
            link = div.find("a", href=True)
            if link is None:
                continue
            label = text_of(link)
            m = re.match(r"(.*?)\s*\(([\d,.]+\s*[KMG]?B)\)\s*$", label)
            attachments.append(
                Attachment(
                    name=m.group(1) if m else label,
                    size=m.group(2) if m else None,
                    url=str(link["href"]).replace("&amp;", "&"),
                )
            )
        att_list.extract()
    for junk in body.select("script, style, .toggle-button"):
        junk.decompose()
    if header is not None and header in body.descendants:
        header.decompose()
    body_html = body.decode_contents().strip()
    lines = [ln.strip() for ln in body.get_text("\n").splitlines()]
    body_text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return Message(
        id=str(panel.get("data-message-id") or message_id or ""),
        title=text_of(title),
        received_at=received,
        body=body_text,
        body_html=body_html,
        attachments=attachments,
    )


# ---- usage points, bank accounts, documents, e-invoice settings ----------------


def parse_usage_points(html: str, payer_number: str | None = None) -> list[UsagePoint]:
    """Usage point page (umowa/punkty-poboru) or the HTML of EbokUsagePoints/Search."""
    soup = soup_of(html)
    container = soup.select_one("#ebokItems") or soup
    if payer_number is None:
        h1 = soup.find("h1")
        payer_number = _payer_number(text_of(h1)) if isinstance(h1, Tag) else None
    out = []
    for row in container.find_all("div", class_="row", recursive=False):
        h3 = row.find("h3")
        if h3 is None:
            continue
        sections: dict[str, dict[str, str]] = {}
        for sub in row.select(".subheader"):
            dl = sub.find_next_sibling("dl")
            if isinstance(dl, Tag):
                sections[text_of(sub)] = _dl_pairs(dl)
        price = None
        m = re.search(r"Cennik:\s*([^\n]+?)\s*(?:Zwiń|Rozwiń|$)", text_of(row))
        if m:
            price = m.group(1).strip()
        out.append(
            UsagePoint(
                payer_number=payer_number or "",
                number=_usage_point_number(text_of(row)),
                medium=_medium(row),
                address=_address(h3),
                sections=sections,
                price_list=price,
            )
        )
    return out


def parse_bank_accounts(html: str) -> list[BankAccount]:
    """Numery kont do wpłat page or the HTML of EbokAccountNumberPPList/Search."""
    out = []
    for row in _contractor_rows(soup_of(html)):
        text = text_of(row)
        number = _payer_number(text)
        if not number:
            continue
        for sub in row.select(".subheader"):
            dl = sub.find_next_sibling("dl")
            if not isinstance(dl, Tag):
                continue
            pairs = _dl_pairs(dl)
            account = pairs.get("Nr rachunku odbiorcy")
            if not account:
                continue
            out.append(
                BankAccount(
                    payer_number=number,
                    usage_point=_usage_point_number(text),
                    address=_address(row.select_one(".float-box p")),
                    purpose=text_of(sub),
                    account_number=account,
                    recipient=pairs.get("Dane odbiorcy"),
                    transfer_title=pairs.get("Tytuł przelewu"),
                )
            )
    return out


def parse_customer_documents(html: str) -> list[CustomerDocument]:
    """Dokumenty klienta page (static terms, price lists, templates)."""
    out = []
    for item in soup_of(html).select(".accordion-files .accordion-item"):
        header = item.select_one(".accordion-item-header")
        category = ""
        if header is not None:
            category = next((s.strip() for s in header.strings if s.strip()), "")
        heading = item.find_parent("ul")
        group_el = heading.find_previous(["h2", "h3"]) if heading else None
        group = text_of(group_el) if isinstance(group_el, Tag) else ""
        if group and group not in ("Dokumenty klienta", category):
            category = f"{group} / {category}"
        for a in item.select("a[href]"):
            name = a.select_one(".accordion-files-name")
            size = a.select_one(".accordion-files-size")
            out.append(
                CustomerDocument(
                    category=category,
                    title=text_of(name) if name else text_of(a),
                    url=str(a["href"]),
                    size=text_of(size).strip("()") if size else None,
                )
            )
    return out


def parse_invoice_email_settings(html: str) -> list[InvoiceEmailSetting]:
    """E-invoice e-mail management page (zarzadzanieefaktura)."""
    out = []
    for row in soup_of(html).select(".changeInvoiceEmail-tableRow"):
        number = re.search(r"(\d+)", text_of(row.select_one(".recipient-number")))
        email = _strip_label(text_of(row.select_one(".email-address")), r"^Adres e-mail:\s*")
        street = text_of(row.select_one(".address-column .street"))
        postal = text_of(row.select_one(".address-column .postal-code"))
        out.append(
            InvoiceEmailSetting(
                payer_number=number.group(1) if number else "",
                email=email or None,
                address=", ".join(p for p in (street, postal) if p) or None,
                custom_name=_custom_name(row.select_one(".payer-column h2")),
            )
        )
    return out


# ---- energy ------------------------------------------------------------------


def _check_energy(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and data.get("success") is False:
        raise ApiError(f"energy API: {data.get('error')}", data)
    if not isinstance(data, list):
        raise ApiError("energy API: unexpected response", data)
    return data


def parse_meter_points(data: Any) -> list[MeterPoint]:
    """EnergyConsumption/ByPoint or ByPointAndZone JSON."""
    out = []
    for p in _check_energy(data):
        addr = p.get("Address") or {}
        out.append(
            MeterPoint(
                number=str(p.get("Number") or ""),
                name=p.get("MeterPointName"),
                meter_number=p.get("MeterNumber"),
                tariff=p.get("TariffGroup"),
                contracted_power=to_decimal(p.get("ContractedPower")),
                address=addr.get("FullAddress") if isinstance(addr, dict) else None,
                readings=[
                    Reading(
                        date=parse_pl_date(r.get("LastReadingDate"))
                        or parse_iso_date(r.get("LastReadingDate")),
                        type=int(r.get("Type") or 0),
                        zones=[
                            ZoneReading(
                                zone=str(z.get("Zone") or ""),
                                consumption=to_decimal(z.get("Consumption")) or Decimal(0),
                                indication=to_decimal(z.get("ConsumptionIndication")) or Decimal(0),
                            )
                            for z in r.get("Details") or []
                        ],
                    )
                    for r in p.get("Readings") or []
                ],
            )
        )
    return out


def parse_monthly(data: Any) -> list[MonthlyConsumption]:
    """EnergyConsumption/Monthly JSON."""
    return [
        MonthlyConsumption(
            year=int(m["Year"]),
            month=int(m["Month"]),
            consumption=to_decimal(m.get("ConsumptionTotal")) or Decimal(0),
        )
        for m in _check_energy(data)
    ]


def merge_payers(*lists: Iterable[Payer]) -> list[Payer]:
    seen: dict[str, Payer] = {}
    for lst in lists:
        for p in lst:
            seen.setdefault(p.number, p)
    return list(seen.values())
