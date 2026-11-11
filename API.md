# TAURON eBOK (moj.tauron.pl): reverse-engineered API reference

Mapped in November 2026 against a live household account (one payer, electricity, G11).
Unofficial: TAURON can change any of this without notice. Implemented in
`src/tauron_ebok/client.py` (HTTP) and `src/tauron_ebok/parsers.py` (HTML/JSON parsing).
All example values below are fake.

> **Legacy note.** Until autumn 2026 the eBOK lived at `ebok.tauron.pl` (a PHP app with
> `/content/platnosci/...` pages and `/podgladFaktury/id/{id}` PDFs). That host now answers
> every URL with `301 → moj.tauron.pl`, so only the API below exists.

## Architecture

- `moj.tauron.pl` is a **Sitecore (ASP.NET MVC)** site. Pages are server-rendered HTML;
  dynamic parts are loaded by jQuery from `/api/sitecore/{Controller}/{Action}`.
- Endpoints need **per-payer identifiers and checksums** that are rendered into the page
  (`data-*` attributes or inline JS). Workflow for every feature: GET the page → extract the
  ids/checksums → call the endpoint. Checksums are stable across sessions (they are derived
  from the payer number), but the SDK always scrapes them fresh.
- Response shapes vary: some endpoints return JSON data, some return JSON wrapping an HTML
  fragment (`ModelString`, `ResultHtml`, `html`), files come back as base64 inside JSON
  (`{Status, Document, FileName}`), except message attachments and the CSV export.
- Dates: archive/payments use `DD.MM.YYYY`; invoices and energy use `YYYY-MM-DD`.
- Most data endpoints accept GET; list/paging endpoints are POST with form-encoded bodies.
- **Payer** = "płatnik / adresat", identified by an 8-digit number (`pp`, `cid`,
  `clientNumber`, `contractorNumber`, `billingNumber` are all this number). `sid` is the
  billing system (`T18`). A login can have several payers.

## Authentication

Shared Keycloak at `logowanie.tauron.pl`, realm `ext`, CAS protocol, service `https://moj.tauron.pl`. No captcha.

1. `GET https://moj.tauron.pl/` → 302 → `logowanie.tauron.pl/realms/ext/protocol/cas/login?service=https://moj.tauron.pl`.
2. Parse `form#kc-form-login[action]`; POST `username`, `password`, `credentialId=` (empty).
3. 302 → `https://moj.tauron.pl?ticket=ST-…` → session cookie `ASP.NET_SessionId` (+ load-balancer cookies).

An expired session redirects any request to `logowanie.tauron.pl`; log in again.
Wrong credentials keep you on Keycloak with `#input-error`.
Logout: `GET /api/sitecore/Home/LogOut`.

## Pages (HTML)

| Page | Path | What the SDK extracts |
|---|---|---|
| Dashboard | `/Home/Pages/Pulpit` | payer rows: number, agreement id, address, contract, tariff, invoice delivery, next payment |
| Invoices & balance | `/Home/Pages/Faktury[?sid=&pp=]` | `var serializedViewModel = {...}` (needed by `LoadMoreInvoicesProducts`) |
| Document archive | `/Home/Pages/archiwum-dokumentow[?sid=&pp=]` | `[data-ajaxfill]`: `data-api-url`, `data-client-number`, `data-client-checksum`, `data-system`, `data-api-excel-url` |
| Payments | `/Home/Pages/lista-wplat[?sid=&pp=]` | same `[data-ajaxfill]` attributes for the payments API |
| Contracts | `/Home/Pages/umowa` | payer rows + link `/Home/Pages/umowa/punkty-poboru?sid=&cid=&checksum=` |
| Usage points | `/Home/Pages/umowa/punkty-poboru?sid=&cid=&checksum=` | per PPE `<div class=subheader>` + `<dl>`: contract, tariff, contracted power, meter number, OSD, customer name, masked PESEL, price list |
| Inactive contracts | `/Home/Pages/Umowy-nieaktywne` | payer rows (empty: "Brak nieaktywnych umów do wyświetlenia") |
| Bank accounts | `/Home/Pages/numery-kont[?sid=&pp=]` | per payer `<dl>`: account number, recipient, transfer title |
| Customer documents | `/Home/Pages/dokumenty-klienta` | `.accordion-files` links to static PDFs on tauron.pl |
| E-invoice e-mail | `/Home/Pages/ustawienia/ustawienia-konta/zarzadzanieefaktura` | `.changeInvoiceEmail-tableRow`: payer number, e-mail |
| Energy history list | `/Home/Pages/historia-odczytow-licznika-prad` | link to `…/szczegoly?sid=&cid=&checksum=` per payer |
| Energy history | `…/historia-odczytow-licznika-prad/szczegoly?sid=&cid=&checksum=` | `#historia-odczytow[data-api-*][data-billing-number]`, PPE ids in `[data-point-id]`, `data-before-months="72"` |
| Messages | `/Home/Pages/Wiadomosci` | (list comes from the API below) |
| Message | `/Home/Pages/szczegolywiadomosci?messageId=` | `.message-details-view .panel[data-message-id]`: title, `Odebrane: DD.MM.YYYY, HH:MM`, body, attachment links. **Opening marks the message read.** |
| Payment allocation | `/Home/Pages/rozdysponowanie-wplat?clientNr=&docNumber=&checkSum=` | same data as `GetPayments.Details` (not used) |

List pages render the first 10 payer rows and page the rest with
`POST /api/sitecore/{Controller}/LoadMoreProduct` (`size`, `ebokContractListSer` = the JSON
string embedded in the page script, `searchName`, `pickupMediumType`) → `{ModelString, ModelCount}`.
`/{Controller}/Search` takes the same body and filters by name/address. Controllers:
`EbokShortListPP` (dashboard), `EbokFullListPP` (contracts), `EbokInactiveContractors`,
`EbokAccountNumberPPList`, `EbokUsagePoints` (+ `systemId`, `contractorNumber`, `checksum`),
`MeterReadingContractorsElectricity`. The archive/payment/e-invoice pages use
`SearchContractor` / `LoadMoreDocuments` instead.

## Endpoints

### Balance: `POST /api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts[?sid=&pp=]`

Body: `startingIndex=0`, `searchTerm=`, `PickupMediumType=`, and the page's
`serializedViewModel` encoded jQuery-style (`parentViewModel[Key]=…`, `parentViewModel[Link][Url]=…`).
Response `{ResultHtml, TotalCount, ReturnedCount}`; page with `startingIndex`.

Per payer, `ResultHtml` has three sibling rows:
- `div.invoice-row`: medium, address, `Numer adresata: N`, `Numer Punktu Poboru: PPE`, headline `Saldo` amount.
- `div.row-details-container`: `.subheader` `Przeterminowane` / `Bieżące` / `Razem`, each a `<dl>` with `Należność`, `Nota odsetkowa`, `Łącznie`.
- `div.row-table-container`: `[data-invoices]` with `data-contractor-number`, `data-contractor-number-checksum`,
  `data-source-id`, `data-invoices-url`; `[data-download-excel-url]`; `.installment-schedule-wrapper[data-api-url]`.

### Open invoices: `GET /api/sitecore/EbokInvoices/GetInvoices`

Query: `sourceId`, `contractorNumber`, `checkSum` (from `[data-invoices]`), `take`, `skip`,
optional `type` (`fv` invoice, `fk` correction, `no` interest note, `nb` debit note),
`docNumber`, `usagePointNumber`, `from`/`to` (`YYYY-MM-DD`, issue date).
Response `{status: "OK", totalCount, html}`; `html` = `<tr>` rows:

| td | content |
|---|---|
| 0 | `<a data-base="/api/sitecore/EbokArchiveDocuments/GetDocumentImage?...&docMode=1">E/TM2/AB123456/0003/26/5</a><div>Faktura</div>` |
| 1 | issue date `DD.MM.YYYY` |
| 2 | info (`-` if none) |
| 3 | due date |
| 4 | `<input data-charge-id data-charge-value>` + `.amount` amount to pay, pay button |

Excel: `GET /api/sitecore/EbokInvoices/GetInvoicesExcel?contractorNumber=&checkSum=[&type=&docNumber=&usagePointNumber=&from=&to=]` → `{Status, Document (base64 xlsx), FileName}`.

### Instalment schedule: `GET /api/sitecore/EbokInvoices/GetInstallmentSchedule?contractorNumber=&checkSum=`

URL taken verbatim from `.installment-schedule-wrapper[data-api-url]` (its checksum differs from
the invoices one). Response: JSON array (empty when there is no schedule) of
`{InstallmentNumber, Documents: [str], InstallmentPaymentDate, InstallmentAmount, SettledInstallmentAmount}`.

### Document archive: `GET /api/sitecore/EbokArchiveDocuments/GetDocuments`

Query: `rangeFrom`, `rangeTo` (1-based, inclusive), `clientNumber`, `clientNumberCheckSum`, `system`,
`from`, `to` (`DD.MM.YYYY`, issue date, **required**), `docKind` (`All`, `invoice`, `correction`,
`interest`, `other`), `docNumber` (prefix match: `E/TM2/AB123456/0003/26` also returns `/26/1`…).
No lower date bound in practice (the page UI limits itself to 72 months, the API returned
documents back to 2019 with `from=01.01.2000`).

```json
{"StatusEnum": 0, "Status": "OK", "RangeFrom": 1, "RangeTo": 10, "TotalNumber": 86,
 "Documents": [{"CustomerNumber": 12345678, "DocGroup": "E", "DocNumber": "551000001",
   "DocSignature": "E/TM2/AB123456/0003/26", "DocType": "FV", "IsDocumentCorrection": false,
   "AmountGross": 49.80, "AmountNet": 40.46, "IssueDate": "2026-08-04T00:00:00",
   "PaymentDate": "2026-08-20T00:00:00", "DocKind": 0, "DocKindValue": "Faktury",
   "ConsumeEnergy": 19.0, "Status": "Opłacona", "SodId": "AUMSKFV551000001O02",
   "Url": "/api/sitecore/EbokArchiveDocuments/GetDocumentImage?clientNumber=…&&clientNumberCheckSum=…&&system=T18&&docNumber=551000001&&docType=FV&&sodId=…&&mode=1",
   "UrlDetails": null, "SerialNumber": null}]}
```

`DocType`/`DocKind` seen: `FV`/0 Faktury, `FK`/1 Korekty, `NB`/3 Inne (interest notes are kind 2).
Errors: `{"StatusEnum": 1, "Status": "Error", "TotalNumber": null}` (e.g. missing dates).

Excel: `GET …/GetDocumentsExcel` with the same filters minus `rangeFrom`/`rangeTo` (plus empty
`docNumber`, `paymentFrom`, `paymentTo`) → `{Status, Document}` (xlsx).

### Document PDF: `GET {Document.Url}` (`EbokArchiveDocuments/GetDocumentImage`)

Response `{"StatusEnum": 0, "Status": "OK", "Document": "<base64 PDF>"}` (no `FileName`).
Works for paper-invoice accounts too. `mode=0/1/2` return slightly different renderings of
the same document; open invoices use `docMode=1`. The SDK uses the URL exactly as given.

### Payments: `GET /api/sitecore/EbokPaymentsHistory/GetPayments`

Query: `rangeFrom`, `rangeTo`, `clientNumber`, `clientNumberCheckSum` (from the payments page,
different from the archive one), `system`, `from`, `to` (`DD.MM.YYYY`, **max 72 months back**,
older → `Status: "Error"`), `docKind` (`All`, `wplata`, `odpisanie`, `zwrot`), `paymentFrom`, `paymentTo` (amounts).

```json
{"Status": "OK", "TotalNumber": 47, "RangeFrom": 1, "RangeTo": 10,
 "Documents": [{"Amount": 445.06, "Date": "2026-10-19T00:00:00", "Type": "Wpłata",
   "CustomerNumber": 12345678, "DocNumber": "KP/RAP/TM_PKO/26/000830/05/000123",
   "PaymentDateSystem": "2026-10-19T00:00:00", "AmountText": "445,06 zł",
   "Url": "/en/Home/Pages/rozdysponowanie-wplat?clientNr=…&&docNumber=…&&checkSum=…",
   "Details": [{"DocSignature": "E/TM2/AB123456/0003/26/2", "Address": "UL.TESTOWA 1/2 00-001 WARSZAWA",
     "PaymentDate": "2026-09-21T00:00:00", "PaymentAmount": 147.05, "PaymentSettlementAmount": 147.05}]}]}
```

Excel: `GET …/GetPaymentsExcel` with the same filters minus ranges.

### Messages

- `POST /api/sitecore/EbokWiadomosci/GetReceivedMessages` (also `GetSentMessages`, `GetArchivedMessages`).
  Body: `pageNumber`, `itemsPerPage` (**must be 15**, anything else → 400), `messageOrder`
  (0 newest, 1 oldest, 2 title A-Z, 3 title Z-A), optional `searchQuery` / `searchNumberQuery` (3–255 chars),
  and **all eight icon parameters** (`nowaWiadomoscTlo`, `nowaWiadomoscIkonaSrc`, `wiadomoscAlertIkonaSrc`,
  `wiadomoscWatekIkonaSrc`, `nowaWiadomoscNieprzeczytanaIkonaSrc`, `wiadomoscAlertNieprzeczytanyIkonaSrc`,
  `wiadomoscWatekNieprzeczytanyIkonaSrc`, `nowaWiadomoscPrzeczytanaIkonaSrc`), otherwise 400.
  The server echoes these into `<img class="email-icon" src>`, so passing distinctive values
  reveals read/unread state. Response `{ModelString, TotalCount, RetrievedCount}`; each `<li>` has
  `.email-header-details[data-message-id]` with `.email-title` and a date `<div>`.
- `POST …/ArchiveMessages`: `messageIds[]=…` (jQuery array encoding), `newArchiveStatus=true|false`.
- `POST …/TurnOnEmailNotification` / `TurnOffEmailNotification` (no body).
- Attachments: links on the message page, `GET /api/sitecore/EbokSzczegolyWiadomosci/GetSystemAttachment?attachmentId=&checksum=`
  → raw file with `Content-Disposition`. SOD messages use `GET …/GetReceivedSodAttachment?attachmentId=&checksum=`
  (from `data-attachment-id` / `data-attachment-checksum`) → `{status: "OK", file: base64, name}`.

### Energy consumption (`EnergyConsumption`)

Context from the details page; `billingNumber` = payer number, `Pods` = PPE number (repeatable).
`from`/`to` are `YYYY-MM-DD`; the page clamps `from` to 72 months back, older ranges return
`{"success": false, "error": "Błąd podczas pobierania danych"}`. Without `from`/`to` you get an HTML error page.

- `GET /api/sitecore/EnergyConsumption/ByPoint?billingNumber=&from=&to=[&Pods=]` → readings, zones summed (`Zone: "AggregatedZone"`):

  ```json
  [{"TariffGroup": "G11", "ContractedPower": 5, "Number": "590000000000000001",
    "MeterPointName": "KOWALSKI JAN", "MeterNumber": "590000000000000001",
    "Address": {"City": "WARSZAWA", "ZipCode": "00-001", "StreetName": "Ul. TESTOWA", "HouseNumber": "1",
                "FlatNumber": "2", "FullAddress": "00-001 WARSZAWA, Ul. TESTOWA, 1/2", "…": "…"},
    "Readings": [{"LastReadingDate": "02.01.2025", "Type": 2,
                  "Details": [{"Zone": "AggregatedZone", "Consumption": 737, "ConsumptionIndication": 21633}]}]}]
  ```

  `Type` is 1 or 2; the meaning is not documented (the CSV labels both "Rzeczywisty").
- `GET …/ByPointAndZone` same query and shape, `Zone` = zone name (`Całodobowa`, `Dzienna`, …).
- `GET …/Monthly?billingNumber=&from=&to=[&yearToYear=true]` → `[{"Month": 1, "Year": 2025, "ConsumptionTotal": 737}]`
  (only months that have a reading).
- `GET …/Export` same query as ByPoint → `text/csv` (UTF-8 with BOM, `attachment; filename=energy.csv`), columns
  `DATA_ODCZYTU, ZUZYCIE_BIEZACE, JEDNOSTKA, DATA_POPRZEDNIEGO_ODCZYTU, ODCZYT_BIEZACY, JEDNOSTKA, ODCZYT_POPRZEDNI, JEDNOSTKA, STREFA_EC, ADRES_PPE, TYP_ODCZYTU, NUMER_LICZNIKA, SKLADNIK`.

### Account changes

| Endpoint | Body | SDK |
|---|---|---|
| `POST /api/sitecore/EbokShortListPP/SetName` (also `EbokFullListPP`, `EbokInvoiceList`, …) | `contractorId` (= `data-agreement-id`), `newName` (≤30 chars) | `set_payer_name()`; returns the stored name |
| `POST /api/sitecore/EbokWiadomosci/ArchiveMessages` | see above | `archive_messages()` |
| `POST /api/sitecore/EbokWiadomosci/TurnOn/OffEmailNotification` | none | `set_email_notifications()` |

The SDK does not call these write endpoints against the live site in tests (mocked only).

## Deliberately not implemented

| Endpoint | Why |
|---|---|
| `GET /api/sitecore/EbokShortListPP/Payment?ids=<chargeId>` (also `EbokInvoiceList/Payment`) | starts an online payment |
| `POST /api/sitecore/UpdateEmail/*`, `UpdatePhoneNumber/*`, `UpdateData/ConfirmEmail\|ConfirmPhone\|UpdateConsent`, `InvoiceEmailUpdate/Update` | change contact data / marketing consents; need SMS/e-mail confirmation codes |
| `PUT /api/sitecore/EbokHeader/SetSessionMessage`, `POST /api/sitecore/EbokPopup/SetClosedPopup`, `EbokShortListPP/SetHiddenFavorite` | UI state only (banners, popups, favourites) |
| `/api/sitecore/EbokChangeInvoiceEmail/Details` | answers "Komponent nie został skonfigurowany" |
| Service request forms (`/Home/Pages/obsluga/*`, `zamow-dane-pomiarowe`, `pzo/*`, `Nowa-wiadomosc`, …) | multi-step forms that file requests with TAURON |
| Chat widget (`moj-chat.tauron.pl`), tariff calculator (`api/getPrices`) | not account data |

The meter-reading submission page (`/Home/Pages/odczyt-licznika`) says the service has been
switched off ("Usługa odczytu licznika została wyłączona"); readings now come from the distributor.
