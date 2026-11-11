"""``tauron-ebok`` command line interface. Prints JSON to stdout; files are written to disk.

Credentials come from ``TAURON_USER`` / ``TAURON_PASS`` (environment or a ``.env`` file in
the current directory). The session is cached in ``~/.cache/tauron-ebok/session.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from . import __version__
from .client import TauronClient
from .exceptions import TauronError
from .models import (
    DocumentKind,
    DownloadedFile,
    MessageFolder,
    OpenInvoiceType,
    PaymentKind,
    to_jsonable,
)

DEFAULT_SESSION_FILE = Path("~/.cache/tauron-ebok/session.json")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _print(data: Any) -> None:
    json.dump(to_jsonable(data), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _save(file: DownloadedFile, out: str) -> Path:
    dest = Path(out)
    if dest.is_dir() or out.endswith(("/", os.sep)):
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest / (file.filename or "download.bin")
    dest.write_bytes(file.content)
    return dest


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tauron-ebok", description="Unofficial TAURON eBOK (moj.tauron.pl) client"
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--payer", help="payer number (needed only when the login has several payers)")
    ap.add_argument(
        "--session-file",
        default=os.environ.get("TAURON_SESSION_FILE", str(DEFAULT_SESSION_FILE)),
        help="where to cache session cookies ('' disables)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def dates(p: argparse.ArgumentParser) -> None:
        p.add_argument("--from", dest="date_from", type=date.fromisoformat, help="YYYY-MM-DD")
        p.add_argument("--to", dest="date_to", type=date.fromisoformat, help="YYYY-MM-DD")

    sub.add_parser("payers", help="payers / contracts on the login (dashboard)")
    sub.add_parser("balance", help="balance of every payer (overdue / current / total)")

    p = sub.add_parser("invoices", help="open (unpaid) invoices")
    dates(p)
    p.add_argument("--type", choices=[t.value for t in OpenInvoiceType], default="all")
    p.add_argument("--number", help="document number")
    p.add_argument("--xlsx", metavar="PATH", help="save the Excel export instead of printing")

    sub.add_parser("installments", help="instalment schedule")

    p = sub.add_parser("documents", help="invoice & document archive")
    dates(p)
    p.add_argument("--kind", choices=[k.value for k in DocumentKind], default="All")
    p.add_argument("--number", help="document number prefix")
    p.add_argument("--limit", type=int, default=0, help="max rows (0 = all)")
    p.add_argument("--xlsx", metavar="PATH", help="save the Excel export instead of printing")

    p = sub.add_parser("pdf", help="download a document PDF by number or id")
    p.add_argument("number", help="e.g. E/TM2/AB123456/0003/26 or 551000001")
    p.add_argument("--out", default=".", help="file or directory (default: current directory)")

    p = sub.add_parser("payments", help="payments received and their allocation")
    dates(p)
    p.add_argument("--kind", choices=[k.value for k in PaymentKind], default="All")
    p.add_argument("--xlsx", metavar="PATH", help="save the Excel export instead of printing")

    p = sub.add_parser("messages", help="list eBOK messages")
    p.add_argument("--folder", choices=[f.value for f in MessageFolder], default="received")

    p = sub.add_parser("message", help="show one message (marks it as read)")
    p.add_argument("id")
    p.add_argument("--attachments", metavar="DIR", help="also download attachments into DIR")

    sub.add_parser("contracts", help="active contracts")
    sub.add_parser("inactive-contracts", help="terminated contracts")
    sub.add_parser("usage-points", help="usage points (PPE) with contract and meter data")
    sub.add_parser("accounts", help="bank accounts to pay into")
    sub.add_parser("customer-documents", help="terms, price lists and templates")
    sub.add_parser("einvoice", help="e-invoice e-mail address per payer")

    p = sub.add_parser("readings", help="meter readings and consumption (last 72 months)")
    dates(p)
    p.add_argument("--per-zone", action="store_true", help="split multi-zone tariffs into zones")
    p.add_argument("--csv", metavar="PATH", help="save the site's CSV export instead of printing")

    p = sub.add_parser("monthly", help="consumption per month")
    dates(p)
    p.add_argument("--year", type=int, action="append", dest="years", help="repeatable")

    return ap


def run(args: argparse.Namespace, client: TauronClient) -> None:
    payer = args.payer
    cmd = args.cmd
    if cmd == "payers":
        _print(client.get_payers())
    elif cmd == "balance":
        _print(client.get_balances() if payer is None else [client.get_balance(payer)])
    elif cmd == "invoices":
        if args.xlsx:
            print(_save(client.export_open_invoices(payer), args.xlsx))
        else:
            _print(
                client.get_open_invoices(
                    payer, date_from=args.date_from, date_to=args.date_to, type=args.type, number=args.number
                )
            )
    elif cmd == "installments":
        _print(client.get_installments(payer))
    elif cmd == "documents":
        filters = {
            "date_from": args.date_from,
            "date_to": args.date_to,
            "kind": args.kind,
            "number": args.number,
        }
        if args.xlsx:
            print(_save(client.export_documents(payer, **filters), args.xlsx))
        else:
            docs = []
            for d in client.iter_documents(payer, **filters):
                docs.append(d)
                if args.limit and len(docs) >= args.limit:
                    break
            _print(docs)
    elif cmd == "pdf":
        doc = client.find_document(args.number, payer)
        if doc is None:
            raise TauronError(f"document {args.number} not found in the archive")
        print(_save(client.download_document(doc), args.out))
    elif cmd == "payments":
        filters = {"date_from": args.date_from, "date_to": args.date_to, "kind": args.kind}
        if args.xlsx:
            print(_save(client.export_payments(payer, **filters), args.xlsx))
        else:
            _print(list(client.iter_payments(payer, **filters)))
    elif cmd == "messages":
        _print(list(client.iter_messages(args.folder)))
    elif cmd == "message":
        msg = client.get_message(args.id)
        out = msg.to_dict()
        out.pop("body_html", None)
        if args.attachments:
            out["saved"] = [
                str(_save(client.download_attachment(a), args.attachments + "/")) for a in msg.attachments
            ]
        _print(out)
    elif cmd == "contracts":
        _print(client.get_contracts())
    elif cmd == "inactive-contracts":
        _print(client.get_inactive_contracts())
    elif cmd == "usage-points":
        _print(
            [
                {**u.to_dict(), "tariff": u.tariff, "meter_number": u.meter_number}
                for u in client.get_usage_points(payer)
            ]
        )
    elif cmd == "accounts":
        _print(client.get_bank_accounts(payer))
    elif cmd == "customer-documents":
        _print(client.get_customer_documents())
    elif cmd == "einvoice":
        _print(client.get_invoice_email_settings())
    elif cmd == "readings":
        if args.csv:
            print(
                _save(
                    client.export_meter_readings(payer, date_from=args.date_from, date_to=args.date_to),
                    args.csv,
                )
            )
        else:
            _print(
                client.get_meter_readings(
                    payer, date_from=args.date_from, date_to=args.date_to, per_zone=args.per_zone
                )
            )
    elif cmd == "monthly":
        _print(
            client.get_monthly_consumption(
                payer, years=args.years, date_from=args.date_from, date_to=args.date_to
            )
        )
    else:  # pragma: no cover - argparse rejects unknown commands
        raise SystemExit(f"unknown command {cmd}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(Path.cwd() / ".env")
    user, password = os.environ.get("TAURON_USER"), os.environ.get("TAURON_PASS")
    session_file = args.session_file or None
    if not (user and password) and not (session_file and Path(session_file).expanduser().exists()):
        print("error: set TAURON_USER and TAURON_PASS (environment or .env)", file=sys.stderr)
        return 2
    try:
        with TauronClient(user, password, session_file=session_file) as client:
            run(args, client)
    except TauronError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
