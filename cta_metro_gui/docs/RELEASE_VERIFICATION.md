# CTA Metro 2.5 - release verification report

**Verdict: NOT production-ready.** The code changes are complete and all automated checks pass, but a live payment
provider, real credentials, two owner decisions (fares, permissions) and manual acceptance tests on the target computers
are still missing (sections 6-8). Passing tests are not proof of live payments, real GUI behaviour or security.

Date of verification: 25 October 2026. Starting point: the uploaded `CTA_Metro_App.zip` (version 2.3).

## 1. Test results (actual)

Clean environment built only from `requirements.lock` + `requirements-dev.txt` (+ optional OpenCV), Python 3.12.3, Linux.

| Check | Command | Result |
|---|---|---|
| Syntax | `python -m compileall -q .` | OK |
| Unit + integration tests, virtual display (Xvfb) | `python -m unittest discover -s tests -v` | **383 run, 383 passed, 0 skipped** |
| Same, no display | same, without `DISPLAY` | **290 run, 290 passed, 7 classes (53 tests) skipped** |
| Dependency consistency | `python -m pip check` | No broken requirements |
| Lint | `python -m ruff check .` | All checks passed (rules and exceptions in `pyproject.toml`) |
| Types (11 non-GUI modules) | `python -m mypy` | No issues |
| Security scan | `python -m bandit -c pyproject.toml -r .` | 0 high, 0 medium, 0 low (24 reviewed findings annotated, see 5) |
| Known vulnerabilities | `python -m pip_audit --require-hashes -r requirements.lock` | No known vulnerabilities |
| Coverage (statements + branches) | `python -m coverage run -m unittest discover -s tests` | **86.0 %** overall |
| Stability | full suite repeated 3x; concurrency tests 5x | all passed |

Tests per file: `test_all` 151 (the original 107 plus Modules 1-2), `test_tickets` 76, `test_auth` 33, `test_fares` 25,
`test_validation` 23, `test_routing` 22, `test_integration` 12, `test_support` 40.

Coverage by module: fares 97 %, routing 98 %, errors 97 %, pdf 95 %, stats 95 %, tickets 90 %, db 90 %, payments 92 %,
accounts 93 %, core 91 %, sms 90 %, storage 86 %, auth 86 %, recover 86 %, app 83 %, admin 78 %, dialogs 71 %,
staff CLI 67 %, widgets 59 %. The lowest figures are drawing code (widgets, dialogs) and interactive prompts, which the
smoke tests exercise only partly - covered by the manual checks in section 8.

Baseline before any change: the original 107 tests passed (91 run + GUI class skipped without a display; 107 passed
under Xvfb).

### Skipped tests
| Skipped | Why | When it runs |
|---|---|---|
| 7 GUI classes, 53 tests: `GuiSmokeTests` (22), `AppFailureTests` (9), `AppTicketTests` (6), `AppAuthTests` (5), `AppRoutingTests` (5), `AppFareTests` (4), `AppWorkflowTests` (2) | no display / Tkinter | ran and passed under Xvfb here; must still be run on a real Windows desktop (section 8) |
| 11 PDF-content tests | only if `pypdf` is missing | ran (pypdf is in `requirements-dev.txt`) |
| 1 QR-decoding test | only if OpenCV or `pdftoppm` is missing | ran (OpenCV optional, noted in `requirements-dev.txt`) |

GUI smoke tests ran in a **virtual** X display, not on a real desktop: Windows-only behaviour (DPI scaling, maximised
window, `os.startfile` printing, fonts) and real keyboard-wedge scanners were **not** tested.

## 2. Files changed and added

Unchanged: `assets/`, `edge_weights.csv`, `setup_sms.py`, `stations_geo.example.json`, `tools/make_logos.py`.
All 32 buttons/pages of 2.3 are still present (one button added: *Ticket actions*).

| File | Change | Why |
|---|---|---|
| `cta_payments.py` | rewritten | environments; no simulated money in production; server-side verification; idempotent ledger; explicit states |
| `cta_core.py` | changed | strict storage; history validation; ticket ids; fares through `cta_fares`; input checks; reserved references |
| `cta_app.py` | changed | database store; safe modes; signed QR; atomic check-in; one fare quote; sessions; routing health; error handling; reserved references |
| `cta_admin.py` | changed | atomic check-in; *Ticket actions* (cancel, refund, re-entry, name); audited export; permission errors |
| `cta_dialogs.py` | changed | staff sign-in through the audited service; passenger sign-in audit; names kept after a failed payment |
| `cta_accounts.py` | changed | strict JSON reads; validators; PBKDF2 600,000 rounds, upgrade on sign-in; non-text input handled |
| `cta_pdf.py` | changed | signed QR; cancelled/refunded banners; estimated-distance label |
| `cta_stats.py` | changed | cancelled/refunded journeys; refunds due; unknown periods refused |
| `cta_routing.py` | changed | network validation; providers never swapped silently; routing health; key never shown |
| `cta_sms.py` | changed | damaged settings reported; unknown channel refused; crash-safe settings writes |
| `cta_widgets.py`, `cta_logos.py` | 3 lines each | narrow exception handlers; unused import/variable |
| `cta_config.example.json` | changed | `environment`, `routing` and `payments` sections |
| `requirements.txt` | changed | bounded versions; pillow raised to 13 (security) |
| `README.md` | changed | setup, settings, migration, backup/restore, payments, tickets, fares, routing, tests, limits |
| `tests/test_all.py` | changed | original tests kept (2 replaced, see section 4); Modules 1-2 tests; fixtures sign in properly |
| **added** `cta_storage.py` | new | strict JSON reads, crash-safe writes, backups, restore |
| **added** `cta_db.py` | new | SQLite store: versions, migration, upgrades, atomic operations, audit chain, sessions |
| **added** `cta_tickets.py` | new | signed QR tokens, signing keys, the one entry decision, cancel/refund/re-entry/correction rules |
| **added** `cta_fares.py`, `fare_matrix.csv`, `tools/fare_matrix.py` | new | the single fare function and its reviewed matrix |
| **added** `cta_auth.py`, `cta_staff.py` | new | permissions, sessions, staff administration CLI |
| **added** `cta_errors.py` | new | one error-handling path, diagnostics log with references, secret scrubbing |
| **added** `cta_recover.py` | new | status, verify, backup, restore, migrate, signing-key commands |
| **added** `docs/FARE_POLICY.md`, `docs/PERMISSIONS.md`, this file | new | documents needing owner approval; release record |
| **added** `requirements.lock`, `requirements-dev.txt`, `pyproject.toml` | new | hash-pinned tested versions; checking tools and their settings |
| **added** 6 test files, `tests/ORIGINAL_TESTS.txt` | new | tests for every module; record of the original tests |

## 3. Bugs fixed (summary; details in the README and module documents)

**Payments** - online payments were "paid" because the simulated gateway said so: production now refuses the simulated
gateway and turns online payment off without a real provider; a provider's "approved" is accepted only after the
provider's own record matches transaction, amount, currency and booking; one transaction pays one booking; pending,
unknown, failed and cancelled are explicit and never issue a valid ticket; only signed-in staff record cash.

**Data** - unreadable or damaged files loaded as an empty database and the next save erased them: damaged, unreadable
and invalid files now stop writes with a clear message; crash-safe writes; validation of every record; data moved to
SQLite with per-booking versions so two windows cannot overwrite each other; automatic backups; verified migration;
restore tool.

**Tickets** - the QR code was readable text that anyone could copy or invent, and check-in was not atomic: QR codes are
signed tokens without personal data; the stored record decides; check-in verifies and uses a ticket in one locked
transaction (simultaneous scans: exactly one wins); unpaid, not-yet-valid, expired, cancelled, refunded, used, forged
and unknown are all refused; scan log without personal data; tickets valid only from their travel day.

**Fares** - two zone formulas existed and the zone count was taken on trust: one fare function; the booking is refused if
its zones or amount differ from the policy or from what was shown; the reviewed matrix stops sales if code and matrix
disagree. No fare was changed (584 of 1,260 journeys would differ under the other formula: owner decision pending).

**Permissions** - any name string was accepted as "staff": every sensitive operation checks a live signed-in session and
the current role in the database; lockout shared by all windows; audited sign-ins, refusals and staff changes;
tamper-evident audit log; passwords hashed with 600,000 rounds.

**Routing** - a broken Google set-up silently fell back to estimates: no silent fallback; production needs an explicit
choice; live failures are labelled and audited; network data validated; displayed route = priced route.

**Errors and input** - broad handlers swallowed errors, raw exception text reached users, a failure after payment could
lose a booking: one error path with references and a diagnostics log; nothing after a payment vanishes without a
trace; validators no longer crash on wrong types; invalid input refused before any write or charge.

**Found and fixed during final verification (Module 8)**
- Two windows booking at the same moment got the same reference; the second was refused only after paying -> references
  are reserved atomically before payment.
- A provider's payment confirmation could not be saved (only a manual data edit could) -> `confirm_online_payment` /
  `handle_payment_callback`, idempotent, audited.
- Two windows starting a new installation together could see a half-written signing-key file and report it as damaged
  -> the key file is published atomically (hard link); deterministic test added.
- Staff sign-in, account creation and password resets hashed passwords (~0.2 s) while holding the database write lock,
  pausing every booking and check-in -> hashing moved outside the lock; a password changed mid-sign-in is refused.
- pillow 12.1.1 had 55 published advisories -> pillow 13.0.0 (compatibility verified: full suite, PDF rendering).
- A QR-decoding test failed intermittently (OpenCV's standard detector cannot read some valid codes) -> deterministic
  ticket id; the printed codes were confirmed valid with a second decoder (150/150).
- **Two more test defects found while taking the evidence screenshots (26 Oct 2026):** (a) one test altered the last two
  characters of a random QR signature to `AA`; the signature's last character carries only 3 bits, so about 1 run in 270 it
  was already `AA`, the "altered" code equalled the real one and the test failed -> the test now always really changes
  the code (0 unchanged in 200,000 tries); (b) the admin page's 30-second refresh timer could fire after its window was
  closed and print a harmless `invalid command name` message at the end of a test run -> the page cancels its timer when it is
  destroyed. Neither affected the app's behaviour.
- **Known pre-existing quirk (not changed):** if you type in a station box and click another tab without choosing a station
  or pressing Escape, the suggestion list stays floating over the next page. The code is identical to version 2.3.
- SQL text could be passed into an internal reader -> parameter removed; small lint and type fixes.
- **Found when the app was installed the way the README describes (25 Oct 2026, after the first release build):** two
  routing tests failed for anyone who had created `cta_config.json` next to the app, because the test suite read that
  file. The release verification above ran in a folder without one and missed it. Fixed: every default settings path
  (`cta_routing`, `cta_payments`, `cta_sms`) is now an overridable `CONFIG_PATH`, the suite points all of them at a file
  that does not exist, and a test enforces it. Verified with no settings file, with the example file and with a hostile
  one: 343/343 each time.

## 4. Changes to existing tests
- 105 of the 107 original tests keep their names and pass. Two were replaced because they asserted behaviour removed on
  purpose (the QR code no longer contains the passenger's name, route or ticket number): see `tests/ORIGINAL_TESTS.txt`.
- Assertions changed where the old behaviour was the bug: a damaged file now raises instead of loading as empty;
  persistence is checked in the database instead of `history.json`; staff act through signed-in sessions; production
  tests choose a routing provider explicitly; two fixtures used a zone count the fare policy does not give.

## 5. Security review notes
- Bandit findings reviewed individually: 13 "SQL built from strings" (only code constants - table/column lists - are
  inserted; every value is a bound `?` parameter) and 11 low (fixed programs to open/print the app's own PDF, optional font
  probing, the diagnostics writer, a token prefix). Each carries `# reason  # nosec <id>` on its line.
- Secrets: none in source or fixtures (test keys and passwords are throw-away values). Keys come from the environment or
  owner-only files; passwords, codes, tokens and keys are never written to the audit or diagnostics logs (tested).
- Not done: penetration test, code review by a second person, review of the Windows file permissions of the data folder.

## 6. Implemented vs. still needing external set-up or decisions

| Area | Implemented and tested | Needs before going live |
|---|---|---|
| Card / mada / STC Pay / PayPal / Tabby / Tamara | provider interface, verification, idempotency, callbacks, refund recording | a real provider contract, its official API integration (`PaymentProvider` subclass), credentials, a callback endpoint (server), provider refund API, live testing |
| Cash at the counter | complete | staff accounts and training |
| SMS sign-in | Twilio Verify client, on-screen fallback for development | Twilio account; Saudi sender-ID registration may block SMS to +966 numbers |
| Live routing | Google Routes client, validation, labelled fallback | real coordinates for all 36 stations, API key - or accept labelled estimates |
| Fares | single policy, matrix lock | **owner decision**: which zone rule, rates, return/discount rules, currency (`docs/FARE_POLICY.md`) |
| Staff roles | enforcement, sessions, audit; only `admin` assignable | **owner approval** of the role matrix (`docs/PERMISSIONS.md`) |
| Signing keys | created, rotated, verified | an off-site backup of `ticket_keys.json` |
| Multi-computer use | SQLite with locking, versions | a database server if several computers share a network folder |

## 7. Migration, backup and restore
- **Upgrade from 2.3:** automatic on first start. `history.json`, `accounts.json`, `staff.json` (+ ledgers) are copied
  to `backups/pre-migration-<time>/`, migrated into a temporary database, verified record by record, then put in place.
  The JSON files are never changed or deleted. Damaged JSON stops the migration before anything is written.
- **Database upgrades** (schema 1 -> 2 -> 3): automatic, after a checked `backups/cta-<time>-pre-upgrade.db`, in one
  transaction.
- **Backups:** daily automatic (newest 30 kept) plus before every migration, upgrade and restore;
  `python cta_recover.py backup` for a manual one. `ticket_keys.json` is **not** inside database backups.
- **Restore:** close every CTA window, then `python cta_recover.py restore` (newest) or `restore <file>`. The backup is
  checked first; the replaced database is kept in `backups/replaced/`. `python cta_recover.py status` / `verify` check health.
- **Going back to 2.3** is not supported by the app: keep the `pre-migration` copy of the JSON files to do it by hand.

## 8. Remaining risks and manual verification before deployment
1. **Windows desktop test** of every screen: booking, payment choice, PDF download/print/open, My ticket, admin portal,
   staff sign-in/lockout, Ticket actions, DPI scaling, fonts for non-Latin names.
2. **Real scanner test**: keyboard-wedge QR scanner at the check-in box; printed and on-phone tickets; poor lighting.
3. **Payment provider certification** in the provider's test environment, including failed, cancelled, duplicate and
   late callbacks, then a small live transaction and refund.
4. **Shared-folder test** if more than one computer is used (SQLite locking over the network share).
5. **Restore drill**: restore a backup and the signing key on a spare computer.
6. **Owner sign-off** on fares and permissions; then create real staff accounts and remove test data.
7. **Operational limits**: no two-factor sign-in for staff; data files readable by anyone with the Windows account;
   typed ticket numbers are guessable (staff should check ID); HMAC keys can also create tickets; the last audit rows
   could be deleted without trace by someone with file access; bookings for any future date are accepted.


## 9. Changes in 2.5 (requested after the first Windows run)

| Change | How it is tested | Not verified here |
|---|---|---|
| Admin status and payment-method filters; search now matches payment method and state | `TicketFilterTests` (7), `ScreenTests` end-to-end | - |
| Passenger refund request; admin approve / decline; passenger sees the decision | `RefundRequestTests` (10), `ScreenTests` | The owner's refund policy (eligibility, fees, deadlines) does not exist yet |
| Passenger messages with staff replies; problem form hidden for staff | `MessageTests` (7), `OlderDatabaseTests` (3), `ScreenTests` | Notifications (none exist) |
| New permission `support.reply` (admin only) | `test_auth` role matrix covers it for every role | Approval of the matrix for other roles |
| QR drawn at whole-pixel squares, about 250 px | `QrCodeTests`: exact decode, decode after blur 1.5 and after halving, decode of the PDF page | A phone camera on a real screen |
| Print voucher | `PrintTests`: page count, command sent, fallback chain | A real printer |

Found while testing and fixed: a passenger with no bookings never saw staff replies (the list returned early).
Checks run on this build: compileall, ruff, mypy (11 modules), bandit (no issues), pip-audit (none), pip check - all clean.
