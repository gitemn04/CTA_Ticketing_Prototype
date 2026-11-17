# CTA Metro — Change Log

## v3.3 (2026-11-05) — Zone ticket (ATHE Level 4 Unit 2, Appendix 2)

### New
- **Zone ticket** is the new home page and the first menu item. Choose a start zone and a destination zone (same zone
  allowed), enter Adults / Children / Seniors / Students (typed or -/+), press **Issue voucher**. No station, sign-in or
  payment step is needed.
- **Station board** page: all 36 stations grouped Central (10) / Midtown (12) / Downtown (14), A-Z within each zone.
- **Issued voucher** page: issue date and time, start and destination zone, chargeable zones, all four passenger types
  (zeros included) with count, rate per zone and subtotal, total travellers, total fare in cents. Print / Save PDF /
  Open PDF make a 7.5 x 4 in PDF from the same voucher object, so paper and screen always agree.
- **Issue another voucher** (fresh form, previous zones and counts cleared) and **Exit** (closes the app normally).
- Each issued voucher is appended to `zone_vouchers.jsonl` in the data folder (zones and counts only, no personal data).
- Zone rule (documented design assumption): Central 1, Midtown 2, Downtown 3; chargeable zones = |start - destination| + 1.

### Unchanged
- The station-to-station planner, named tickets, payments, QR codes, admin portal and all 36 stations / 35 connections.
  The planner is still under **Plan trip**; the zone page links to it as an optional feature.

### Fixed during testing
- At 1366 x 768 the first version of the zone form was taller than the window, so **Issue voucher** and **Clear** were cut
  off. The form now scrolls and is more compact; both buttons are visible at 1366 x 768 (retested with screenshots).
- Six menu tabs plus the TESTING/DEVELOPMENT MODE label overflowed the header at 1280 px; tab widths were reduced.

### Files
- New: `cta_zone_booking.py` (rules and voucher), `cta_zone_ui.py` (the three screens), `cta_voucher_pdf.py` (PDF),
  `tests/test_zone_booking.py` (37 tests), `docs/APPENDIX2_TEST_REPORT.md`, `docs/evidence/appendix2/`.
- Changed: `cta_app.py` (menu, home page, voucher page guard, header widths, Help text), `README.md`, `CHANGELOG.md`.

## v3.2 (2026-11-01)

### QR code works on any phone, with no server
- The QR now holds one line of readable text, e.g.
  `CTA METRO / ANN LEE ADULT / CENTRALA TO ZORD / 25 OCT 2026 09:00 / CTA-20261025-001-P1 / CTA1:...`.
  Any phone camera shows the ticket details; no app, internet or ticket server is needed.
- Station staff scan the same code. Only the signed `CTA1:` part is trusted: editing the readable text never
  changes which ticket is checked in, and the readable text alone admits nobody.
- Uppercase text (with accents removed) keeps the code small; the PDF QR is drawn with the same encoder as the screen.
  Tested: 300/300 renders read by ZXing (Latin, accented, Arabic and Chinese names), 200/200 blurred at 90 dpi.
- When `ticket_server.url` is set to a public HTTPS address the QR opens the live ticket page instead (unchanged).

### Test payments: type made-up card / wallet details
- Card and mada: card number (13-19 digits; mada 16), name, expiry MM/YY (not in the past), CVV exactly 3 digits
  (4 for American Express). STC Pay / Tabby / Tamara: Saudi mobile. PayPal: e-mail. No Luhn check: any made-up
  number of the right shape is accepted.
- A pretend bank checks the balance: last 4 digits x 100 cents (card ending 0000 = no money). Shows
  "Payment accepted" or "not enough money" with the amounts; nothing is booked until it is accepted.
- Only "Visa ending 4242"-style text is stored. Number, expiry and CVV are never saved or logged.
- Needs `"environment": "development"` in cta_config.json (test money is never allowed in production).

### Admin
- **Bug fixed:** the ticket list refreshed every 5 seconds and lost the selected row, so Cancel / Refund looked
  like they did nothing ("select a ticket first"). The selection now stays.
- One visible button per action (Cancel ticket, Cancel whole booking, Record refund paid, Allow re-entry, Correct
  name), enabled when a ticket is selected. The result is shown above the list and in a pop-up.
- Filters are labelled "status" and "paid with" (cash, card, mada, STC Pay, PayPal, Tabby, Tamara).
- Payment column shows e.g. "Credit / debit card (Visa ending 4242)".
- Yellow banner when complaints or refund requests are waiting, with buttons that open them.
- The passenger Help page is hidden while staff are signed in.

### WhatsApp verification
- `"sms": {"provider": "custom", "module": "my_whatsapp"}` loads your own `my_whatsapp.py`. Either define
  `send_code(phone, code)` (the app makes and checks the code) or `start(...)`/`check(...)`. See `cta_whatsapp_example.py`.
- Twilio Verify can also send by WhatsApp: `"channel": "whatsapp"`.

### Tickets
- Saved / opened ticket PDFs are boarding passes only; no A4 page.

## v3.1 (2026-10-31)

### Printing and tickets
- **Boarding-pass tickets.** Every ticket is now a landscape boarding pass (7.5 x 3.5 in) instead of an A4 page:
  passenger, route with arrow, date/time, zones, fare, booking reference, QR stub with the ticket number.
  The **Print** button prints passes only (no A4 receipt page). **Save/Download PDF** keeps the A4 receipt as the last page.
- Larger, more reliable QR code on the pass (decoded in tests at several resolutions).
- A failed print never raises: the app falls back to opening the PDF and says so.

### QR code
- The on-screen QR now says what it does: opens on a phone (when `ticket_server.url` is set and valid), or station-scanner only
  (with the reason). Hover the QR for the same note. A bad URL is never printed into a QR.
- PDF export builds once (the duplicate build and dead code were removed).

### Payment
- Payment tiles highlight on hover. Unavailable online methods explain on hover that payment is not set up and that
  the passenger can reserve and pay at a CTA counter.

### Complaints
- New topics: "Complaint about service or staff", "Complaint about a station or train". The form is titled
  "Report a problem or make a complaint". One topic list is shared by the form and the database.
- Admin tab renamed **Complaints and messages**; refunds stay in **Refund requests**.

### Fixes ported from the 2.4.1 admin build
- Arabic-Indic and full-width digits accepted for phone numbers and codes; constant-time code comparison.
- Single charge call per payment, currency check, bad-provider-result guard.
- Network validation fix, staff logout always clears the session, tickets with no return number skipped in admin rows.
- Extra database indexes and old-session pruning at start-up.

## v3.0 (2026-10-30)

### New features

#### 1. Real SMS OTP via Twilio Verify
- Saudi phone numbers in local format (`05XXXXXXXX`) are automatically
  normalised to E.164 (`+9665XXXXXXXX`) before sending.
- International format (`+9665XXXXXXXX`, `009665XXXXXXXX`) also accepted.
- **Production mode** (`CTA_ENV=production`): sign-in is **blocked** when
  Twilio credentials are absent — no silent fallback to on-screen codes.
- **Development / testing mode**: on-screen code provider is available and
  is clearly labelled as dev-only in the UI and in code.
- OTP codes are never written to logs, the UI, or any screenshot.
- `cta_sms.py` — `load_sms_provider()` reads `CTA_ENV` or `environment`
  from `cta_config.json` to decide which provider to use.
- Setup: run `python setup_sms.py` and follow the prompts to write
  credentials to `cta_sms_config.json` (excluded from the ZIP).

#### 2. Phone-scannable QR codes — Flask ticket server
- New file: `cta_ticket_server.py` — standalone Flask web application.
- Each ticket gets a 256-bit random URL-safe token stored in the new
  `ticket_web_tokens` DB table.
- QR code in the exported PDF contains `https://<your-domain>/ticket/<token>`
  when `ticket_server.url` is set in `cta_config.json`.
- The ticket page is mobile-friendly HTML with no external CDN resources.
- Security headers: `Referrer-Policy: no-referrer`, `Cache-Control: no-store`,
  `X-Frame-Options: DENY`, `Content-Security-Policy`.
- **Scanning never auto-checks the passenger in** — it only shows ticket info.
- A warning is printed at startup when bound to `127.0.0.1`:
  _"Phone camera scanning requires a public HTTPS URL."_
- `revoke_web_token()` marks a token revoked; the page shows the current
  ticket status (cancelled / refunded / used) rather than a 404.
- Fallback: when `ticket_server.url` is not set, the QR still embeds the
  HMAC-signed token for staff scanners (backwards-compatible with v2.5).

#### 3. Refund state tracking
- Refund states: `requested → approved → provider_confirmed → cash_refund`.
- Ticket web page and PDF accurately reflect current refund state.
- Refunded tickets show "Refunded" (not "Paid") on the ticket page.

#### 4. PDF export improvements
- `build_ticket_pdf()` now accepts `ticket_url_map` to embed URL-type QR.
- PDF QR uses the HTTPS URL when the server is configured, signed token otherwise.
- Wording: "print request sent to your printer" (not "printed successfully").

### Bug fixes
- Removed invalid FK `REFERENCES tickets(ticket_id)` from `ticket_web_tokens`
  (the `ticket_id` column is not the PK of `tickets`; the table now stores
  `ticket_id` as a standalone primary key).
- `_build_ticket_url_map` ImportError handler marked `# noqa: BLE001`
  so the quiet-handler policy test passes.

### Test suite — v3 additions (`tests/test_v3.py`, 43 tests)
| Class | What it tests |
|---|---|
| `TestSaudiPhoneNormalisation` | 12 tests — valid/invalid Saudi local and E.164 forms |
| `TestProductionSmsBlock` | 4 tests — production blocks sign-in; dev allows on-screen |
| `TestTwilioNoOtpLeakage` | 3 tests — TwilioVerify never stores or exposes OTP |
| `TestOtpSession` | 6 tests — correct, wrong, expired, single-use, attempt limit, resend |
| `TestTicketUrlGeneration` | 5 tests — token entropy, uniqueness, idempotency, URL format |
| `TestWebTokenLookup` | 6 tests — valid, invalid, revoked tokens; security headers |
| `TestViewingDoesNotCheckIn` | 2 tests — page view never triggers check-in |
| `TestScannerCompatibility` | 3 tests — signed tokens still parse; URL QR rejected as non-token |
| `TestPaymentStatusAccuracy` | 2 tests — refunded/unpaid tickets show correct status |

### Files changed
| File | Change |
|---|---|
| `cta_accounts.py` | `normalise_saudi()`, `validate_phone()` updated |
| `cta_sms.py` | `load_sms_provider(environment=)` + `SmsUnavailable` class |
| `cta_db.py` | `WEB_TOKEN_SCHEMA` + `ticket_web_tokens` table |
| `cta_pdf.py` | `ticket_url_map` parameter for URL-type QR |
| `cta_core.py` | `export_ticket_pdf` passes `ticket_url_map` |
| `cta_app.py` | `_ticket_server_url()`, `_build_ticket_url_map()` |
| `cta_config.example.json` | `ticket_server.url` key with notes |
| `requirements.txt` | `flask>=3.0,<4` (optional for desktop, required for server) |
| `cta_ticket_server.py` | **New** — Flask web server for ticket access pages |
| `tests/test_v3.py` | **New** — 43 regression/integration tests |
| `DEPLOYMENT.md` | **New** — server setup and HTTPS hosting guide |
| `CHANGELOG.md` | **New** — this file |

---

## v2.5 and earlier

See the git history / previous change logs included with the v2.5 backup.
