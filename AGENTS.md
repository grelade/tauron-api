# AGENTS.md

Guidance for coding agents working on this repository.

## What this is

`tauron-api`: an unofficial Python SDK + CLI for TAURON eBOK (https://moj.tauron.pl). The
portal has no public API; the SDK logs in through Keycloak and scrapes server-rendered
Sitecore pages plus the jQuery AJAX endpoints under `/api/sitecore/{Controller}/{Action}`.
**API.md is the endpoint reference**: read it before touching the client, and update it
whenever you learn something new about the site.

## Layout

```
src/tauron_api/
  client.py      TauronClient: login, session/cookie handling, one method per feature (I/O only)
  parsers.py     pure functions HTML/JSON -> models; all scraping selectors live here
  models.py      dataclasses (Decimal money, date dates), enums for API filter values, to_jsonable()
  exceptions.py  TauronError > AuthenticationError | ApiError | ParseError | PayerNotFoundError
  cli.py         `tauron-api` argparse CLI, JSON on stdout
tests/
  fixtures/      ANONYMISED captures of real pages / API responses
  test_parsers.py  parsers against fixtures
  test_client.py   client flows with HTTP mocked by `responses` (asserts request params too)
  test_cli.py      CLI end to end with mocked HTTP
  test_live.py     read-only checks against the real site, skipped unless TAURON_LIVE=1
```

## Commands

```bash
uv sync                                   # install (uses uv.lock)
uv run pytest                             # offline test suite, must stay green
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict mode on src/
TAURON_LIVE=1 uv run pytest -m live       # needs TAURON_USER / TAURON_PASS (env or .env)
uv run tauron-api --help                 # CLI
```

Add dependencies with `uv add` / `uv add --dev` so `uv.lock` stays in sync. CI runs ruff,
mypy and pytest on Python 3.10–3.13 (`.github/workflows/ci.yml`).

## How a feature works (pattern to follow)

1. GET the page that hosts the feature (e.g. `/Home/Pages/archiwum-dokumentow?sid=T18&pp=<payer>`).
2. Extract the per-payer ids/checksums from `data-*` attributes or inline JS
   (`parsers.parse_table_contexts`, `parse_balances` → `InvoicesContext`, `extract_view_model`,
   `parse_energy_context`, …).
3. Call the endpoint through `TauronClient.request()` / `_get_json()` / `_post_json()`; they
   re-login once when the session has expired (redirect to `logowanie.tauron.pl`).
4. Parse with a pure function in `parsers.py` into a dataclass from `models.py`.

Keep I/O in `client.py` and parsing in `parsers.py`. New parsers get a fixture + test in
`test_parsers.py`; new client methods get a `responses` test that also asserts the query/body
sent (see existing tests).

## Site quirks worth knowing

- Checksums differ per page/feature (archive vs payments vs invoices vs instalments) even for
  the same payer; always take them from the page that belongs to the endpoint.
- `GetPayments` and the energy endpoints reject `from` dates older than 72 months; the client
  clamps. `GetDocuments` has no lower bound but requires both dates.
- Message list endpoints answer 400 unless `itemsPerPage=15` and all eight icon parameters are sent.
- `GetDocumentImage` returns base64 PDF inside JSON; message attachments are raw files.
- jQuery-style nested form encoding (`parentViewModel[A][B]=…`) is needed for
  `LoadMoreInvoicesProducts`, see `parsers.jquery_params`.
- Opening a message page marks it as read.

## Releasing

Releases go to PyPI from GitHub Actions (`.github/workflows/release.yml`) via trusted
publishing; there is no token to manage.

1. Bump the version: `uv version --bump patch` (or `minor` / `major`); this updates `pyproject.toml` and `uv.lock`.
2. Commit, then tag and push: `git tag v$(uv version --short) && git push && git push --tags`.
3. The workflow runs CI, checks the tag matches the version, builds and smoke-tests the wheel,
   publishes to https://pypi.org/project/tauron-api/ and creates a GitHub release with the files attached.

PyPI versions can never be re-uploaded, so a broken release needs a new version number.

## Safety rules

- **Never commit personal data.** `.env` holds real credentials and is gitignored. Fixtures must
  be anonymised: replace names, addresses, payer/PPE/meter numbers, e-mails, phone, PESEL, bank
  account numbers, document ids, and every hex checksum before saving into `tests/fixtures/`.
  Grep the fixtures for the real values before committing.
- Live exploration is read-only. Do not call payment endpoints (`*/Payment`), contact-data or
  consent updates (`UpdateEmail`, `UpdatePhoneNumber`, `UpdateData`, `InvoiceEmailUpdate`), or
  submit service request forms. Write endpoints that the SDK exposes (`SetName`,
  `ArchiveMessages`, notification toggles) are tested with mocks only.
- Keep request volume low when exploring (one login, reuse the session; Keycloak drops
  connections on rapid repeated logins).
