"""Typed records returned by the SDK.

All money is ``Decimal`` (PLN), all dates are ``datetime.date``. ``to_dict()`` gives a
JSON-friendly ``dict`` (used by the CLI).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

_Date = date  # for annotations in classes that have a field called ``date``


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/Decimal/date/Enum into JSON-serialisable values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


class _Record:
    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = to_jsonable(self)
        return result


# ---- enums -------------------------------------------------------------------


class DocumentKind(str, Enum):
    """``docKind`` filter of the document archive."""

    ALL = "All"
    INVOICE = "invoice"
    CORRECTION = "correction"
    INTEREST = "interest"
    OTHER = "other"


class OpenInvoiceType(str, Enum):
    """``type`` filter of the open (unpaid) invoices list."""

    ALL = "all"
    INVOICE = "fv"
    CORRECTION = "fk"
    INTEREST_NOTE = "no"
    DEBIT_NOTE = "nb"


class PaymentKind(str, Enum):
    """``docKind`` filter of the payment history."""

    ALL = "All"
    PAYMENT = "wplata"
    WRITE_OFF = "odpisanie"
    REFUND = "zwrot"


class MessageFolder(str, Enum):
    RECEIVED = "received"
    SENT = "sent"
    ARCHIVED = "archived"


class MessageOrder(int, Enum):
    NEWEST_FIRST = 0
    OLDEST_FIRST = 1
    TITLE_AZ = 2
    TITLE_ZA = 3


# ---- account / payers --------------------------------------------------------


@dataclass
class Payer(_Record):
    """One payer ("płatnik / adresat") on the login, as listed on the dashboard."""

    number: str  # payer / addressee number, e.g. "12345678"
    agreement_id: str | None  # internal id used by the rename endpoints
    system: str  # billing system id, e.g. "T18"
    medium: str  # "Prąd" / "Gaz"
    custom_name: str | None  # user-assigned label, None when unset
    address: str
    contract: str | None = None
    service: str | None = None
    tariff: str | None = None
    invoice_delivery: str | None = None  # e.g. "E-FAKTURA"
    next_payment_amount: Decimal | None = None
    next_payment_due: date | None = None


@dataclass
class BalanceBreakdown(_Record):
    receivable: Decimal  # "Należność"
    interest_note: Decimal  # "Nota odsetkowa"
    total: Decimal  # "Łącznie"


@dataclass
class Balance(_Record):
    """Balance of one payer (Faktury i płatności page)."""

    payer_number: str
    medium: str
    address: str
    usage_point: str | None  # PPE number
    total: Decimal  # "Saldo" headline
    overdue: BalanceBreakdown | None  # "Przeterminowane"
    current: BalanceBreakdown | None  # "Bieżące"
    combined: BalanceBreakdown | None  # "Razem"


@dataclass
class OpenInvoice(_Record):
    """Unpaid document from the Faktury i płatności page."""

    number: str  # document signature, e.g. "E/TM2/AB123456/0003/26/5"
    type: str  # e.g. "Faktura"
    issue_date: date | None
    info: str | None
    due_date: date | None
    amount_to_pay: Decimal
    charge_id: str | None
    document_url: str | None  # GetDocumentImage URL (relative), None if no preview


@dataclass
class Installment(_Record):
    number: int
    documents: list[str]
    due_date: date | None
    amount: Decimal
    settled_amount: Decimal

    @property
    def paid(self) -> bool:
        return self.settled_amount >= self.amount


# ---- archive / payments ------------------------------------------------------


@dataclass
class Document(_Record):
    """Entry of the invoice & document archive (Archiwum dokumentów)."""

    id: str  # DocNumber, numeric id
    number: str  # DocSignature, the number printed on the document
    doc_type: str  # FV, FK, NO, NB, ...
    doc_group: str | None
    kind: int  # 0 invoices, 1 corrections, 2 interest notes, 3 other
    kind_label: str  # "Faktury", "Korekty", "Inne", ...
    is_correction: bool
    amount_gross: Decimal
    amount_net: Decimal | None
    issue_date: date | None
    due_date: date | None
    consumption_kwh: Decimal | None
    status: str  # "Opłacona", "Rozliczono", ...
    customer_number: str
    sod_id: str | None
    url: str | None  # GetDocumentImage URL (relative)
    details_url: str | None  # invoice statement (xlsx) URL, when the site offers one
    serial_number: str | None = None


@dataclass
class DocumentPage(_Record):
    total: int
    range_from: int
    range_to: int
    items: list[Document]


@dataclass
class PaymentAllocation(_Record):
    """Document a payment was booked against ("rozdysponowanie wpłaty")."""

    document_number: str
    address: str | None
    due_date: date | None
    document_amount: Decimal
    settled_amount: Decimal


@dataclass
class Payment(_Record):
    number: str  # booking document number
    date: date | None
    booking_date: _Date | None
    amount: Decimal
    type: str  # "Wpłata", ...
    customer_number: str
    allocations: list[PaymentAllocation] = field(default_factory=list)
    details_url: str | None = None


@dataclass
class PaymentPage(_Record):
    total: int
    range_from: int
    range_to: int
    items: list[Payment]


@dataclass
class DownloadedFile(_Record):
    filename: str | None
    content_type: str | None
    content: bytes = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "content_type": self.content_type, "size": len(self.content)}


# ---- messages ----------------------------------------------------------------


@dataclass
class MessageSummary(_Record):
    id: str
    title: str
    date: date | None
    unread: bool | None = None


@dataclass
class MessagePage(_Record):
    folder: MessageFolder
    page: int
    per_page: int
    total: int
    items: list[MessageSummary]


@dataclass
class Attachment(_Record):
    name: str
    url: str  # relative URL
    size: str | None = None  # as shown, e.g. "94,37KB"


@dataclass
class Message(_Record):
    id: str
    title: str
    received_at: datetime | None
    body: str  # plain text
    body_html: str = field(repr=False)
    attachments: list[Attachment] = field(default_factory=list)


# ---- contracts / usage points ------------------------------------------------


@dataclass
class UsagePoint(_Record):
    """Usage point (Punkt Poboru, PPE) with its contract and customer data."""

    payer_number: str
    number: str | None  # PPE number
    medium: str
    address: str
    sections: dict[str, dict[str, str]]  # raw label -> value, grouped by section header
    price_list: str | None = None

    def _get(self, label: str) -> str | None:
        for values in self.sections.values():
            if label in values:
                return values[label]
        return None

    @property
    def contract(self) -> str | None:
        return self._get("Umowa")

    @property
    def tariff(self) -> str | None:
        return self._get("Taryfa")

    @property
    def contracted_power(self) -> str | None:
        return self._get("Moc umowna")

    @property
    def meter_number(self) -> str | None:
        return self._get("Numer licznika")

    @property
    def distributor(self) -> str | None:
        return self._get("Nazwa OSD (dystrybutora)")


@dataclass
class Contractor(_Record):
    """Payer row of the contract lists (active or inactive contracts)."""

    number: str
    agreement_id: str | None
    medium: str
    custom_name: str | None
    address: str
    usage_points_url: str | None = None


@dataclass
class BankAccount(_Record):
    payer_number: str
    usage_point: str | None
    address: str
    purpose: str  # e.g. "Płatności za fakturę"
    account_number: str  # as shown, grouped in fours
    recipient: str | None
    transfer_title: str | None


@dataclass
class CustomerDocument(_Record):
    """Template / terms document from the Dokumenty klienta page."""

    category: str
    title: str
    url: str
    size: str | None = None


@dataclass
class InvoiceEmailSetting(_Record):
    payer_number: str
    email: str | None
    address: str | None
    custom_name: str | None


# ---- energy ------------------------------------------------------------------


@dataclass
class ZoneReading(_Record):
    zone: str  # "AggregatedZone" (all zones summed) or a zone name like "Całodobowa"
    consumption: Decimal  # kWh used since the previous reading
    indication: Decimal  # meter register value


@dataclass
class Reading(_Record):
    date: date | None
    type: int  # raw "Type" code from the API
    zones: list[ZoneReading]

    @property
    def consumption(self) -> Decimal:
        return sum((z.consumption for z in self.zones), Decimal(0))


@dataclass
class MeterPoint(_Record):
    number: str  # PPE number
    name: str | None
    meter_number: str | None
    tariff: str | None
    contracted_power: Decimal | None
    address: str | None
    readings: list[Reading]


@dataclass
class MonthlyConsumption(_Record):
    year: int
    month: int
    consumption: Decimal
