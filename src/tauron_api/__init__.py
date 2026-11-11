"""Unofficial Python SDK for TAURON eBOK (moj.tauron.pl)."""

from importlib.metadata import PackageNotFoundError, version

from .client import BASE_URL, TauronClient
from .exceptions import (
    ApiError,
    AuthenticationError,
    ParseError,
    PayerNotFoundError,
    TauronError,
)
from .models import (
    Attachment,
    Balance,
    BalanceBreakdown,
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
    PaymentAllocation,
    PaymentKind,
    PaymentPage,
    Reading,
    UsagePoint,
    ZoneReading,
)

try:
    __version__ = version("tauron-api")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "BASE_URL",
    "ApiError",
    "Attachment",
    "AuthenticationError",
    "Balance",
    "BalanceBreakdown",
    "BankAccount",
    "Contractor",
    "CustomerDocument",
    "Document",
    "DocumentKind",
    "DocumentPage",
    "DownloadedFile",
    "Installment",
    "InvoiceEmailSetting",
    "Message",
    "MessageFolder",
    "MessageOrder",
    "MessagePage",
    "MessageSummary",
    "MeterPoint",
    "MonthlyConsumption",
    "OpenInvoice",
    "OpenInvoiceType",
    "ParseError",
    "Payer",
    "PayerNotFoundError",
    "Payment",
    "PaymentAllocation",
    "PaymentKind",
    "PaymentPage",
    "Reading",
    "TauronClient",
    "TauronError",
    "UsagePoint",
    "ZoneReading",
    "__version__",
]
