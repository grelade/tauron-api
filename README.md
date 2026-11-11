# tauron-ebok

Unofficial Python SDK and command-line client for **TAURON eBOK**
([moj.tauron.pl](https://moj.tauron.pl)), the customer portal of the Polish energy company TAURON.

It logs in with your eBOK credentials and gives typed access to everything the portal shows:

- payers and contracts, balance (overdue / current / total), open invoices, instalment schedule
- the full invoice & document archive, **PDF downloads** (paper-invoice accounts included), Excel exports
- payment history with allocation to documents
- messages (inbox / sent / archive) with attachments
- usage points (PPE) with tariff, contracted power, meter number; bank accounts; e-invoice e-mail
- energy consumption: meter readings, per-zone readings, monthly totals, CSV export (72 months)

> Not affiliated with TAURON. The portal has no public API; this library scrapes the same
> pages and AJAX endpoints your browser uses and can break when TAURON changes the site.
> See [API.md](API.md) for the reverse-engineered endpoint reference.

## Install

```bash
uv add git+https://github.com/grelade/tauron-ebok      # or: pip install git+https://…
```

Requires Python 3.10+.

## Command line

```bash
export TAURON_USER=you@example.com TAURON_PASS='…'     # or put them in ./.env
uvx --from git+https://github.com/grelade/tauron-ebok tauron-ebok balance
```

| Command | Output |
|---|---|
| `payers` | payers on the login with tariff and next payment |
| `balance` | balance breakdown per payer |
| `invoices [--type fv\|fk\|no\|nb] [--xlsx PATH]` | open invoices |
| `installments` | instalment schedule |
| `documents [--from D] [--to D] [--kind invoice\|correction\|interest\|other] [--number N] [--limit N] [--xlsx PATH]` | archive |
| `pdf NUMBER_OR_ID [--out DIR_OR_FILE]` | save a document PDF |
| `payments [--from D] [--to D] [--kind wplata\|odpisanie\|zwrot] [--xlsx PATH]` | payments |
| `messages [--folder received\|sent\|archived]` / `message ID [--attachments DIR]` | messages |
| `contracts`, `inactive-contracts`, `usage-points`, `accounts`, `customer-documents`, `einvoice` | account data |
| `readings [--from D] [--to D] [--per-zone] [--csv PATH]` / `monthly [--year Y ...]` | consumption |

Output is JSON (amounts are strings to keep `Decimal` precision). Global options: `--payer N`
(needed only for logins with several payers) and `--session-file PATH` (default
`~/.cache/tauron-ebok/session.json`; cookies are reused so repeated runs don't log in again).

## Library

```python
from datetime import date
from tauron_ebok import TauronClient

with TauronClient("you@example.com", "secret", session_file="~/.cache/tauron-ebok/session.json") as c:
    for payer in c.get_payers():
        print(payer.number, payer.address, payer.next_payment_amount, payer.next_payment_due)

    balance = c.get_balance()                     # payer= can be omitted for single-payer logins
    print(balance.total, balance.overdue.total if balance.overdue else 0)

    for doc in c.iter_documents(date_from=date(2025, 1, 1)):
        pdf = c.download_document(doc)            # DownloadedFile(filename, content_type, content)
        open(pdf.filename, "wb").write(pdf.content)

    for point in c.get_meter_readings():
        for r in point.readings:
            print(r.date, r.consumption, "kWh")
```

All methods: `get_payers`, `get_payer`, `get_balances`, `get_balance`, `get_open_invoices`,
`export_open_invoices`, `get_installments`, `get_documents`, `iter_documents`, `find_document`,
`download_document`, `export_documents`, `get_payments`, `iter_payments`, `export_payments`,
`get_messages`, `iter_messages`, `get_message`, `download_attachment`, `download_sod_attachment`,
`get_contracts`, `get_inactive_contracts`, `get_usage_points`, `get_bank_accounts`,
`get_customer_documents`, `get_invoice_email_settings`, `get_meter_readings`,
`get_monthly_consumption`, `export_meter_readings`; account changes: `set_payer_name`,
`archive_messages`, `set_email_notifications`; session: `login`, `logout`, `save_session`.

Errors derive from `tauron_ebok.TauronError` (`AuthenticationError`, `ApiError`, `ParseError`,
`PayerNotFoundError`).

## Development

```bash
uv sync                          # creates .venv with dev tools
uv run pytest                    # offline tests (HTTP mocked, anonymised fixtures)
uv run ruff check . && uv run ruff format --check . && uv run mypy
TAURON_LIVE=1 uv run pytest -m live   # read-only smoke tests against the real site (.env credentials)
```

See [AGENTS.md](AGENTS.md) for the project layout and conventions.

## License

MIT
