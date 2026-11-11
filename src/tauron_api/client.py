"""HTTP client for moj.tauron.pl (TAURON eBOK).

The site is a Sitecore (ASP.NET) app. Pages are server-rendered HTML that embed the
per-payer ids and checksums needed by the AJAX endpoints under ``/api/sitecore/...``.
Each method fetches the page it needs, extracts that state, then calls the endpoint.
See API.md for the endpoint reference.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import parsers
from .exceptions import ApiError, AuthenticationError, ParseError, PayerNotFoundError, TauronError
from .models import (
    Balance,
    BankAccount,
    Contractor,
    CustomerDocument,
    Document,
    DocumentKind,
    DocumentPage,
    DownloadedFile,
    Installment,
    InvoiceEmailSetting,
    Message,
    MessageFolder,
    MessageOrder,
    MessagePage,
    MessageSummary,
    MeterPoint,
    MonthlyConsumption,
    OpenInvoice,
    OpenInvoiceType,
    Payer,
    Payment,
    PaymentKind,
    PaymentPage,
    UsagePoint,
)

log = logging.getLogger(__name__)

BASE_URL = "https://moj.tauron.pl"
LOGIN_HOST = "logowanie.tauron.pl"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
)

# pages
PAGE_DASHBOARD = "/Home/Pages/Pulpit"
PAGE_INVOICES = "/Home/Pages/Faktury"
PAGE_ARCHIVE = "/Home/Pages/archiwum-dokumentow"
PAGE_PAYMENTS = "/Home/Pages/lista-wplat"
PAGE_CONTRACTS = "/Home/Pages/umowa"
PAGE_INACTIVE = "/Home/Pages/Umowy-nieaktywne"
PAGE_ACCOUNTS = "/Home/Pages/numery-kont"
PAGE_DOCUMENTS = "/Home/Pages/dokumenty-klienta"
PAGE_ENERGY = "/Home/Pages/historia-odczytow-licznika-prad"
PAGE_MESSAGES = "/Home/Pages/Wiadomosci"
PAGE_MESSAGE = "/Home/Pages/szczegolywiadomosci"
PAGE_EINVOICE = "/Home/Pages/ustawienia/ustawienia-konta/zarzadzanieefaktura"

# endpoints
API = "/api/sitecore"
EP_INVOICE_LIST = f"{API}/EbokInvoiceList/LoadMoreInvoicesProducts"
EP_INVOICES = f"{API}/EbokInvoices/GetInvoices"
EP_INVOICES_EXCEL = f"{API}/EbokInvoices/GetInvoicesExcel"
EP_DOCUMENTS = f"{API}/EbokArchiveDocuments/GetDocuments"
EP_DOCUMENTS_EXCEL = f"{API}/EbokArchiveDocuments/GetDocumentsExcel"
EP_DOCUMENT_IMAGE = f"{API}/EbokArchiveDocuments/GetDocumentImage"
EP_PAYMENTS = f"{API}/EbokPaymentsHistory/GetPayments"
EP_PAYMENTS_EXCEL = f"{API}/EbokPaymentsHistory/GetPaymentsExcel"
EP_MESSAGES = {
    MessageFolder.RECEIVED: f"{API}/EbokWiadomosci/GetReceivedMessages",
    MessageFolder.SENT: f"{API}/EbokWiadomosci/GetSentMessages",
    MessageFolder.ARCHIVED: f"{API}/EbokWiadomosci/GetArchivedMessages",
}
EP_ARCHIVE_MESSAGES = f"{API}/EbokWiadomosci/ArchiveMessages"
EP_NOTIFICATIONS_ON = f"{API}/EbokWiadomosci/TurnOnEmailNotification"
EP_NOTIFICATIONS_OFF = f"{API}/EbokWiadomosci/TurnOffEmailNotification"
EP_SOD_ATTACHMENT = f"{API}/EbokSzczegolyWiadomosci/GetReceivedSodAttachment"
EP_SET_NAME = f"{API}/EbokShortListPP/SetName"
EP_LOGOUT = f"{API}/Home/LogOut"

# list pages that page their payer rows through {Controller}/Search + LoadMoreProduct
LIST_CONTROLLERS = {
    PAGE_DASHBOARD: "EbokShortListPP",
    PAGE_CONTRACTS: "EbokFullListPP",
    PAGE_INACTIVE: "EbokInactiveContractors",
    PAGE_ACCOUNTS: "EbokAccountNumberPPList",
    PAGE_ENERGY: "MeterReadingContractorsElectricity",
}

# The messages endpoint renders icons from URLs we pass in; marking the unread ones lets
# the parser tell read from unread.
_MESSAGE_ICON_PARAMS = {
    "nowaWiadomoscTlo": "#4461a8",
    "nowaWiadomoscIkonaSrc": "/icon/new",
    "wiadomoscAlertIkonaSrc": "/icon/alert",
    "wiadomoscWatekIkonaSrc": "/icon/thread",
    "nowaWiadomoscNieprzeczytanaIkonaSrc": "/icon/new-unread",
    "wiadomoscAlertNieprzeczytanyIkonaSrc": "/icon/alert-unread",
    "wiadomoscWatekNieprzeczytanyIkonaSrc": "/icon/thread-unread",
    "nowaWiadomoscPrzeczytanaIkonaSrc": "/icon/new-read",
}

LIST_PAGE_SIZE = 10
MESSAGES_PER_PAGE = 15  # GetReceivedMessages answers 400 for any other itemsPerPage
ARCHIVE_EPOCH = date(2000, 1, 1)  # the archive has no lower bound; this predates any eBOK data
PAYMENTS_MAX_MONTHS = 72  # GetPayments returns Status "Error" for older "from" dates


def _months_ago(months: int, today: date | None = None) -> date:
    today = today or date.today()
    y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
    day = min(today.day, 28)
    return date(y, m + 1, day) + timedelta(days=1)


def _pl(d: date) -> str:
    return d.strftime("%d.%m.%Y")


class TauronClient:
    """Logged-in session against moj.tauron.pl.

    >>> with TauronClient("user@example.com", "secret") as c:
    ...     for b in c.get_balances():
    ...         print(b.payer_number, b.total)

    Most methods take an optional ``payer`` (payer/addressee number). It can be omitted
    when the login has a single payer.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        session: requests.Session | None = None,
        session_file: str | Path | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", user_agent)
        self.session.headers["User-Agent"] = user_agent
        self.session_file = Path(session_file).expanduser() if session_file else None
        self._payers: list[Payer] | None = None
        self._logged_in = False
        if self.session_file and self.session_file.exists():
            self._load_cookies()

    # ---- context manager -----------------------------------------------------

    def __enter__(self) -> TauronClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.save_session()
        self.session.close()

    # ---- session persistence -------------------------------------------------

    def _load_cookies(self) -> None:
        assert self.session_file is not None
        try:
            cookies = json.loads(self.session_file.read_text())
        except (OSError, ValueError):
            log.warning("ignoring unreadable session file %s", self.session_file)
            return
        for c in cookies:
            self.session.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/")
            )
        self._logged_in = bool(cookies)

    def save_session(self) -> None:
        """Write cookies to ``session_file`` (if set) so the next run can skip logging in."""
        if not self.session_file or not self._logged_in:
            return
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.session.cookies
            if c.value is not None
        ]
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(json.dumps(cookies))
        self.session_file.chmod(0o600)

    # ---- auth ----------------------------------------------------------------

    def login(self) -> None:
        """Log in through the shared Keycloak (realm ``ext``, CAS protocol, service moj.tauron.pl)."""
        if not self.username or not self.password:
            raise AuthenticationError("username and password are required to log in")
        self.session.cookies.clear()
        self._payers = None
        r = self.session.get(self.base_url + "/", timeout=self.timeout)
        r.raise_for_status()
        form = parsers.soup_of(r.text).select_one("form#kc-form-login")
        if form is None or not form.get("action"):
            if urlparse(r.url).hostname == urlparse(self.base_url).hostname:
                self._logged_in = True  # SSO cookie was still valid
                return
            raise AuthenticationError(f"login form not found at {r.url}")
        r = self.session.post(
            str(form["action"]),
            data={"username": self.username, "password": self.password, "credentialId": ""},
            timeout=self.timeout,
        )
        if urlparse(r.url).hostname != urlparse(self.base_url).hostname:
            err = parsers.soup_of(r.text).select_one("#input-error, .kc-feedback-text, .alert-error")
            raise AuthenticationError("login failed: " + (parsers.text_of(err) or r.url))
        self._logged_in = True
        self.save_session()

    def logout(self) -> None:
        """End the server-side session and forget cookies."""
        try:
            self.session.get(self.base_url + EP_LOGOUT, timeout=self.timeout)
        finally:
            self.session.cookies.clear()
            self._logged_in = False
            self._payers = None
            if self.session_file and self.session_file.exists():
                self.session_file.unlink()

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    @staticmethod
    def _bounced(r: requests.Response) -> bool:
        """Expired sessions are redirected to the Keycloak login page."""
        return urlparse(r.url).hostname == LOGIN_HOST

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Authenticated request with one transparent re-login when the session has expired."""
        self._ensure_login()
        kwargs.setdefault("timeout", self.timeout)
        url = urljoin(self.base_url + "/", path)
        for attempt in (1, 2):
            r = self.session.request(method, url, **kwargs)
            if self._bounced(r):
                if attempt == 1 and self.username:
                    log.info("session expired, logging in again")
                    self.login()
                    continue
                raise AuthenticationError("session expired and could not be re-established")
            r.raise_for_status()
            return r
        raise AuthenticationError("session could not be re-established")  # pragma: no cover

    def _get_html(self, path: str, **params: Any) -> str:
        return self.request("GET", path, params=params or None).text

    def _json(self, r: requests.Response, what: str) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise ApiError(f"{what}: expected JSON, got {r.headers.get('content-type')}") from e

    def _get_json(self, path: str, what: str, params: Any = None) -> Any:
        return self._json(self.request("GET", path, params=params), what)

    def _post_json(self, path: str, what: str, data: Any = None, **kw: Any) -> Any:
        return self._json(self.request("POST", path, data=data, **kw), what)

    # ---- payers --------------------------------------------------------------

    def _list_rows(self, page: str, params: dict[str, Any] | None = None) -> list[str]:
        """HTML of every payer row on a list page.

        List pages render the first 10 payers; when the page is full, the rest are fetched
        through ``{Controller}/LoadMoreProduct`` (accounts with more than 10 payers).
        """
        html = self._get_html(page, **(params or {}))
        chunks = [html]
        controller = LIST_CONTROLLERS.get(page)
        ser = parsers.extract_contract_list_ser(html)
        rows = parsers.count_payer_rows(html)
        while controller and ser and rows and rows % LIST_PAGE_SIZE == 0:
            data = self._post_json(
                f"{API}/{controller}/LoadMoreProduct",
                "LoadMoreProduct",
                data={"size": rows, "ebokContractListSer": ser, "searchName": "", "pickupMediumType": ""},
            )
            more = data.get("ModelString") if isinstance(data, dict) else None
            added = parsers.count_payer_rows(more) if more else 0
            if not added:
                break
            chunks.append(str(more))
            rows += added
        return chunks

    def get_payers(self, refresh: bool = False) -> list[Payer]:
        """Payers (contracts) on this login, with next payment and tariff, from the dashboard."""
        if self._payers is None or refresh:
            self._payers = parsers.merge_payers(
                *(parsers.parse_payers(h) for h in self._list_rows(PAGE_DASHBOARD))
            )
        return self._payers

    def get_payer(self, payer: str | None = None) -> Payer:
        payers = self.get_payers()
        if payer is None:
            if len(payers) == 1:
                return payers[0]
            if not payers:
                raise PayerNotFoundError("no payers on this account")
            raise PayerNotFoundError(
                "several payers on this account, pass payer= one of " + ", ".join(p.number for p in payers)
            )
        for p in payers:
            if p.number == str(payer):
                return p
        raise PayerNotFoundError(f"payer {payer} not found on this account")

    def set_payer_name(self, payer: str | None, name: str) -> str:
        """Set the custom label of a payer (``EbokShortListPP/SetName``). Returns the stored name."""
        p = self.get_payer(payer)
        if not p.agreement_id:
            raise TauronError(f"payer {p.number} has no agreement id")
        r = self.request("POST", EP_SET_NAME, data={"contractorId": p.agreement_id, "newName": name})
        self._payers = None
        return r.text.strip().strip('"')

    # ---- balance & open invoices ---------------------------------------------

    def _invoice_rows(self, payer: str | None) -> list[tuple[Balance, parsers.InvoicesContext | None]]:
        params: dict[str, Any] = {}
        if payer is not None:
            p = self.get_payer(payer)
            params = {"sid": p.system, "pp": p.number}
        html = self._get_html(PAGE_INVOICES, **params)
        vm = parsers.extract_view_model(html)
        rows: list[tuple[Balance, parsers.InvoicesContext | None]] = []
        while True:
            data = [("startingIndex", str(len(rows))), ("searchTerm", ""), ("PickupMediumType", "")]
            data += parsers.jquery_params(vm, "parentViewModel")
            res = self._post_json(
                EP_INVOICE_LIST, "LoadMoreInvoicesProducts", data=data, params=params or None
            )
            if not isinstance(res, dict):
                raise ApiError("LoadMoreInvoicesProducts: unexpected response", res)
            batch = parsers.parse_balances(res.get("ResultHtml") or "")
            rows += batch
            total = int(res.get("TotalCount") or 0)
            if not batch or len(rows) >= total:
                return rows

    def get_balances(self) -> list[Balance]:
        """Balance (overdue / current / total) of every payer."""
        return [b for b, _ in self._invoice_rows(None)]

    def get_balance(self, payer: str | None = None) -> Balance:
        payer = self.get_payer(payer).number
        for b, _ in self._invoice_rows(payer):
            if b.payer_number == payer:
                return b
        raise PayerNotFoundError(f"no balance for payer {payer}")

    def _invoices_context(self, payer: str | None) -> parsers.InvoicesContext:
        number = self.get_payer(payer).number
        for b, ctx in self._invoice_rows(number):
            if b.payer_number == number and ctx is not None:
                return ctx
        raise ParseError(f"open-invoices context for payer {number} not found")

    def get_open_invoices(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        type: OpenInvoiceType | str = OpenInvoiceType.ALL,
        number: str | None = None,
        usage_point: str | None = None,
        page_size: int = 50,
    ) -> list[OpenInvoice]:
        """Unpaid documents (``EbokInvoices/GetInvoices``), all pages."""
        ctx = self._invoices_context(payer)
        base: dict[str, Any] = {"contractorNumber": ctx.contractor_number, "checkSum": ctx.checksum}
        if ctx.source_id:
            base["sourceId"] = ctx.source_id
        kind = OpenInvoiceType(type).value
        if kind != "all":
            base["type"] = kind
        if number:
            base["docNumber"] = number
        if usage_point:
            base["usagePointNumber"] = usage_point
        if date_from:
            base["from"] = date_from.isoformat()
        if date_to:
            base["to"] = date_to.isoformat()
        out: list[OpenInvoice] = []
        while True:
            res = self._get_json(
                ctx.invoices_url, "GetInvoices", params={**base, "take": page_size, "skip": len(out)}
            )
            if not isinstance(res, dict) or res.get("status") != "OK":
                raise ApiError("GetInvoices failed", res)
            batch = parsers.parse_open_invoices(res.get("html") or "")
            out += batch
            if not batch or len(out) >= int(res.get("totalCount") or 0):
                return out

    def export_open_invoices(self, payer: str | None = None) -> DownloadedFile:
        """Open invoices as an Excel file (``EbokInvoices/GetInvoicesExcel``)."""
        ctx = self._invoices_context(payer)
        data = self._get_json(
            ctx.excel_url or EP_INVOICES_EXCEL,
            "GetInvoicesExcel",
            params={"contractorNumber": ctx.contractor_number, "checkSum": ctx.checksum},
        )
        name, payload = parsers.parse_base64_file(data, "GetInvoicesExcel")
        return DownloadedFile(name or "Faktury.xlsx", "application/vnd.ms-excel", base64.b64decode(payload))

    def get_installments(self, payer: str | None = None) -> list[Installment]:
        """Instalment schedule (``EbokInvoices/GetInstallmentSchedule``); empty when there is none."""
        ctx = self._invoices_context(payer)
        if not ctx.installments_url:
            return []
        return parsers.parse_installments(self._get_json(ctx.installments_url, "GetInstallmentSchedule"))

    # ---- archive -------------------------------------------------------------

    def _table_context(self, page: str, payer: str | None) -> parsers.TableContext:
        p = self.get_payer(payer)
        html = self._get_html(page, sid=p.system, pp=p.number)
        for ctx in parsers.parse_table_contexts(html):
            if ctx.client_number == p.number:
                return ctx
        raise ParseError(f"no data table for payer {p.number} on {page}")

    def get_documents(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        kind: DocumentKind | str = DocumentKind.ALL,
        number: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> DocumentPage:
        """One page of the invoice & document archive (``EbokArchiveDocuments/GetDocuments``).

        Dates filter on issue date and default to everything. ``number`` matches document
        numbers by prefix (``E/TM2/AB123456/0003/26`` also returns ``.../26/1`` etc.).
        """
        ctx = self._table_context(PAGE_ARCHIVE, payer)
        params = {
            "rangeFrom": offset + 1,
            "rangeTo": offset + limit,
            "clientNumber": ctx.client_number,
            "clientNumberCheckSum": ctx.checksum,
            "system": ctx.system,
            "from": _pl(date_from or ARCHIVE_EPOCH),
            "to": _pl(date_to or date.today()),
            "docKind": DocumentKind(kind).value,
            "docNumber": number or "",
        }
        return parsers.parse_documents(self._get_json(ctx.api_url, "GetDocuments", params=params))

    def iter_documents(
        self, payer: str | None = None, *, page_size: int = 100, **filters: Any
    ) -> Iterator[Document]:
        """Every archive document matching the filters, newest first."""
        offset = 0
        while True:
            page = self.get_documents(payer, offset=offset, limit=page_size, **filters)
            yield from page.items
            offset += len(page.items)
            if not page.items or offset >= page.total:
                return

    def find_document(self, number: str, payer: str | None = None) -> Document | None:
        """Archive document with exactly this number (signature) or numeric id."""
        if number.isdigit():
            return next((d for d in self.iter_documents(payer) if d.id == number), None)
        page = self.get_documents(payer, number=number)
        return next((d for d in page.items if d.number == number), None)

    def download_document(self, document: Document | OpenInvoice | str) -> DownloadedFile:
        """PDF of an archive document or open invoice (``EbokArchiveDocuments/GetDocumentImage``).

        Accepts a ``Document``, an ``OpenInvoice``, a GetDocumentImage URL, or a document number.
        """
        if isinstance(document, Document):
            url, name = document.url, document.number
        elif isinstance(document, OpenInvoice):
            url, name = document.document_url, document.number
        elif document.startswith(("/", "http")):
            url, name = document, None
        else:
            found = self.find_document(document)
            if found is None:
                raise TauronError(f"document {document} not found in the archive")
            url, name = found.url, found.number
        if not url:
            raise TauronError(f"document {name} has no downloadable image")
        filename, payload = parsers.parse_base64_file(
            self._get_json(url, "GetDocumentImage"), "GetDocumentImage"
        )
        content = base64.b64decode(payload)
        if filename is None and name:
            filename = re.sub(r"[^\w.-]+", "_", name) + ".pdf"
        return DownloadedFile(filename, "application/pdf", content)

    def export_documents(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        kind: DocumentKind | str = DocumentKind.ALL,
        number: str | None = None,
    ) -> DownloadedFile:
        """Archive list as Excel (``EbokArchiveDocuments/GetDocumentsExcel``)."""
        ctx = self._table_context(PAGE_ARCHIVE, payer)
        params = {
            "clientNumber": ctx.client_number,
            "clientNumberCheckSum": ctx.checksum,
            "system": ctx.system,
            "from": _pl(date_from or ARCHIVE_EPOCH),
            "to": _pl(date_to or date.today()),
            "docKind": DocumentKind(kind).value,
            "docNumber": number or "",
            "paymentFrom": "",
            "paymentTo": "",
        }
        data = self._get_json(ctx.excel_url or EP_DOCUMENTS_EXCEL, "GetDocumentsExcel", params=params)
        name, payload = parsers.parse_base64_file(data, "GetDocumentsExcel")
        return DownloadedFile(
            name or "Lista_faktur.xlsx", "application/vnd.ms-excel", base64.b64decode(payload)
        )

    # ---- payments ------------------------------------------------------------

    def _payment_params(
        self,
        ctx: parsers.TableContext,
        date_from: date | None,
        date_to: date | None,
        kind: PaymentKind | str,
        amount_from: float | None,
        amount_to: float | None,
    ) -> dict[str, Any]:
        earliest = _months_ago(PAYMENTS_MAX_MONTHS)
        start = max(date_from or earliest, earliest)
        return {
            "clientNumber": ctx.client_number,
            "clientNumberCheckSum": ctx.checksum,
            "system": ctx.system,
            "from": _pl(start),
            "to": _pl(date_to or date.today()),
            "docKind": PaymentKind(kind).value,
            "paymentFrom": "" if amount_from is None else str(amount_from),
            "paymentTo": "" if amount_to is None else str(amount_to),
        }

    def get_payments(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        kind: PaymentKind | str = PaymentKind.ALL,
        amount_from: float | None = None,
        amount_to: float | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> PaymentPage:
        """Payments received and how they were allocated (``EbokPaymentsHistory/GetPayments``).

        The site only serves the last 72 months; earlier ``date_from`` values are clamped.
        """
        ctx = self._table_context(PAGE_PAYMENTS, payer)
        params = self._payment_params(ctx, date_from, date_to, kind, amount_from, amount_to)
        params.update(rangeFrom=offset + 1, rangeTo=offset + limit)
        return parsers.parse_payments(self._get_json(ctx.api_url, "GetPayments", params=params))

    def iter_payments(
        self, payer: str | None = None, *, page_size: int = 100, **filters: Any
    ) -> Iterator[Payment]:
        offset = 0
        while True:
            page = self.get_payments(payer, offset=offset, limit=page_size, **filters)
            yield from page.items
            offset += len(page.items)
            if not page.items or offset >= page.total:
                return

    def export_payments(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        kind: PaymentKind | str = PaymentKind.ALL,
        amount_from: float | None = None,
        amount_to: float | None = None,
    ) -> DownloadedFile:
        """Payment list as Excel (``EbokPaymentsHistory/GetPaymentsExcel``)."""
        ctx = self._table_context(PAGE_PAYMENTS, payer)
        params = self._payment_params(ctx, date_from, date_to, kind, amount_from, amount_to)
        data = self._get_json(ctx.excel_url or EP_PAYMENTS_EXCEL, "GetPaymentsExcel", params=params)
        name, payload = parsers.parse_base64_file(data, "GetPaymentsExcel")
        return DownloadedFile(
            name or "Lista_wplat.xlsx", "application/vnd.ms-excel", base64.b64decode(payload)
        )

    # ---- messages ------------------------------------------------------------

    def get_messages(
        self,
        folder: MessageFolder | str = MessageFolder.RECEIVED,
        *,
        page: int = 1,
        order: MessageOrder | int = MessageOrder.NEWEST_FIRST,
        search: str | None = None,
    ) -> MessagePage:
        """Inbox / sent / archived messages (``EbokWiadomosci/Get{Received,Sent,Archived}Messages``).

        Pages hold 15 messages; the server rejects any other page size.
        """
        folder = MessageFolder(folder)
        data: dict[str, Any] = {
            "pageNumber": page,
            "itemsPerPage": MESSAGES_PER_PAGE,
            "messageOrder": int(MessageOrder(order)),
            **_MESSAGE_ICON_PARAMS,
        }
        if search and 3 <= len(search) <= 255:
            data["searchQuery"] = search
        res = self._post_json(EP_MESSAGES[folder], f"messages/{folder.value}", data=data)
        if not isinstance(res, dict):
            raise ApiError("messages: unexpected response", res)
        return MessagePage(
            folder=folder,
            page=page,
            per_page=MESSAGES_PER_PAGE,
            total=int(res.get("TotalCount") or 0),
            items=parsers.parse_message_list(res.get("ModelString") or ""),
        )

    def iter_messages(
        self, folder: MessageFolder | str = MessageFolder.RECEIVED, **kw: Any
    ) -> Iterator[MessageSummary]:
        page = 1
        while True:
            res = self.get_messages(folder, page=page, **kw)
            yield from res.items
            if not res.items or page * res.per_page >= res.total:
                return
            page += 1

    def get_message(self, message_id: str | int) -> Message:
        """Full message with body and attachment links. Opening a message marks it as read."""
        html = self._get_html(PAGE_MESSAGE, messageId=message_id)
        return parsers.parse_message(html, str(message_id))

    def download_attachment(self, attachment: Any) -> DownloadedFile:
        """Download a message attachment (``Attachment`` or its URL)."""
        url = attachment if isinstance(attachment, str) else attachment.url
        r = self.request("GET", url)
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:  # GetReceivedSodAttachment answers {status, file, name}
            data = self._json(r, "attachment")
            if data.get("status") != "OK" or not data.get("file"):
                raise ApiError("attachment download failed", data)
            return DownloadedFile(
                data.get("name"), "application/octet-stream", base64.b64decode(data["file"])
            )
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', r.headers.get("content-disposition", ""))
        name = m.group(1) if m else getattr(attachment, "name", None)
        return DownloadedFile(name, ctype.split(";")[0] or None, r.content)

    def download_sod_attachment(self, attachment_id: str, checksum: str) -> DownloadedFile:
        """Attachment of a SOD message (``data-attachment-id`` / ``data-attachment-checksum`` links)."""
        return self.download_attachment(
            f"{EP_SOD_ATTACHMENT}?attachmentId={attachment_id}&checksum={checksum}"
        )

    def archive_messages(self, message_ids: list[str | int], archived: bool = True) -> None:
        """Move messages to (or, with ``archived=False``, out of) the archive."""
        data = [("messageIds[]", str(i)) for i in message_ids]
        data.append(("newArchiveStatus", "true" if archived else "false"))
        self.request("POST", EP_ARCHIVE_MESSAGES, data=data)

    def set_email_notifications(self, enabled: bool) -> None:
        """Turn e-mail notifications about new eBOK messages on or off."""
        self.request("POST", EP_NOTIFICATIONS_ON if enabled else EP_NOTIFICATIONS_OFF)

    # ---- contracts, usage points, accounts, documents ------------------------

    def get_contracts(self) -> list[Contractor]:
        """Active contracts (one row per payer) from the Umowa i dane page."""
        rows: list[Contractor] = []
        for html in self._list_rows(PAGE_CONTRACTS):
            rows += parsers.parse_contractors(html)
        return rows

    def get_inactive_contracts(self) -> list[Contractor]:
        """Terminated contracts (Umowy nieaktywne)."""
        rows: list[Contractor] = []
        for html in self._list_rows(PAGE_INACTIVE):
            rows += parsers.parse_contractors(html)
        return rows

    def get_usage_points(self, payer: str | None = None) -> list[UsagePoint]:
        """Usage points (PPE) of a payer with contract, tariff, meter and customer data."""
        p = self.get_payer(payer)
        for c in self.get_contracts():
            if c.number == p.number and c.usage_points_url:
                return parsers.parse_usage_points(self._get_html(c.usage_points_url), p.number)
        raise ParseError(f"no usage point page for payer {p.number}")

    def get_bank_accounts(self, payer: str | None = None) -> list[BankAccount]:
        """Bank accounts to pay into (Numery kont do wpłat)."""
        accounts: list[BankAccount] = []
        params = None
        if payer is not None:
            p = self.get_payer(payer)
            params = {"sid": p.system, "pp": p.number}
        for html in self._list_rows(PAGE_ACCOUNTS, params):
            accounts += parsers.parse_bank_accounts(html)
        if payer is not None:
            accounts = [a for a in accounts if a.payer_number == str(payer)]
        return accounts

    def get_customer_documents(self) -> list[CustomerDocument]:
        """Terms, price lists and templates offered on the Dokumenty klienta page."""
        return parsers.parse_customer_documents(self._get_html(PAGE_DOCUMENTS))

    def get_invoice_email_settings(self) -> list[InvoiceEmailSetting]:
        """E-mail address each payer's e-invoices are sent to."""
        return parsers.parse_invoice_email_settings(self._get_html(PAGE_EINVOICE))

    # ---- energy --------------------------------------------------------------

    def _energy_context(self, payer: str | None) -> parsers.EnergyContext:
        p = self.get_payer(payer)
        links: dict[str, str] = {}
        for html in self._list_rows(PAGE_ENERGY):
            links.update(parsers.parse_energy_detail_links(html))
        url = links.get(p.number)
        if url is None:
            raise ParseError(f"no energy consumption page for payer {p.number}")
        return parsers.parse_energy_context(self._get_html(url))

    def _energy_params(
        self,
        ctx: parsers.EnergyContext,
        date_from: date | None,
        date_to: date | None,
        usage_points: list[str] | None,
    ) -> list[tuple[str, str]]:
        earliest = _months_ago(ctx.max_months)
        start = max(date_from or earliest, earliest)
        params = [
            ("billingNumber", ctx.billing_number),
            ("from", start.isoformat()),
            ("to", (date_to or date.today()).isoformat()),
        ]
        params += [("Pods", pp) for pp in (usage_points if usage_points is not None else ctx.usage_points)]
        return params

    def get_meter_readings(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        usage_points: list[str] | None = None,
        per_zone: bool = False,
    ) -> list[MeterPoint]:
        """Meter readings and consumption between readings (``EnergyConsumption/ByPoint[AndZone]``).

        The site keeps 72 months of history; ``date_from`` defaults to (and is clamped at) that.
        ``per_zone=True`` splits multi-zone tariffs (G12 etc.) into separate zone readings.
        """
        ctx = self._energy_context(payer)
        url = ctx.by_point_and_zone_url if per_zone else ctx.by_point_url
        params = self._energy_params(ctx, date_from, date_to, usage_points)
        return parsers.parse_meter_points(self._get_json(url, "EnergyConsumption", params=params))

    def get_monthly_consumption(
        self,
        payer: str | None = None,
        *,
        years: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[MonthlyConsumption]:
        """Consumption per month (``EnergyConsumption/Monthly``), only months with a reading.

        Pass ``years`` (the site's year-to-year view) or a date range.
        """
        ctx = self._energy_context(payer)
        earliest = _months_ago(ctx.max_months)
        ranges: list[tuple[date, date]] = []
        if years:
            today = date.today()
            for y in sorted(set(years)):
                ranges.append((max(date(y, 1, 1), earliest), min(date(y, 12, 31), today)))
        else:
            ranges.append((max(date_from or earliest, earliest), date_to or date.today()))
        out: list[MonthlyConsumption] = []
        for start, end in ranges:
            if start > end:
                continue
            params = {"billingNumber": ctx.billing_number, "from": start.isoformat(), "to": end.isoformat()}
            if years:
                params["yearToYear"] = "true"
            out += parsers.parse_monthly(
                self._get_json(ctx.monthly_url, "EnergyConsumption/Monthly", params=params)
            )
        return out

    def export_meter_readings(
        self,
        payer: str | None = None,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        usage_points: list[str] | None = None,
    ) -> DownloadedFile:
        """Meter readings as CSV (``EnergyConsumption/Export``)."""
        ctx = self._energy_context(payer)
        r = self.request(
            "GET", ctx.export_url, params=self._energy_params(ctx, date_from, date_to, usage_points)
        )
        ctype = r.headers.get("content-type", "")
        if "csv" not in ctype:
            data = self._json(r, "EnergyConsumption/Export")
            raise ApiError(
                f"energy export failed: {data.get('error') if isinstance(data, dict) else data}", data
            )
        m = re.search(r'filename="?([^";]+)', r.headers.get("content-disposition", ""))
        return DownloadedFile(m.group(1) if m else "energy.csv", "text/csv", r.content)
