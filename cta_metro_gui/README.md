# CTA Metro - zone tickets, journey planner, ticketing and admin portal (v3.3)

## Zone ticket - ATHE Level 4 Unit 2, Appendix 2 (new in v3.3)
The app now opens on **Zone ticket**. A passenger books by zone only: no station has to be chosen, no sign-in is needed.

1. **Station board** (menu: *Station board*): a table of all 36 stations grouped into Central (10), Midtown (12) and
   Downtown (14), A-Z within each zone (Wicyt is in Midtown). Built from the same verified network data as the map:
   36 stations, 35 track connections, nothing removed.
2. **Zone booking** (menu: *Zone ticket*, the home page): choose a **start zone** and a **destination zone** (the same
   zone for both is allowed), then type or use -/+ for the number of **Adults, Children, Seniors and Students**
   (whole numbers 0-99). Both zones are required; a count that is blank, negative, a decimal or text is refused with a
   message naming the field; an all-zero group is refused with "Enter at least one traveller". Nothing is issued until
   the input is correct.
3. **Issued voucher**: pressing **Issue voucher** shows the voucher with the issue date and time, start and destination
   zone, chargeable zones, **all four passenger types (zeros included)** with count, rate per zone and subtotal, the total
   number of travellers and the total fare in cents. **Print voucher / Save PDF / Open PDF** produce a one-page PDF
   (7.5 x 4 in, not A4) drawn from the same voucher, so the totals on paper equal the totals on screen.
4. **Repeat or exit**: under the voucher, **Issue another voucher** returns to a fresh form (zones and counts cleared)
   and **Exit** closes the app normally.

### Fare rule used by the zone booking (documented design assumption)
| | |
|---|---|
| Zone numbers | Central = 1, Midtown = 2, Downtown = 3 |
| Chargeable zones | `abs(start zone number - destination zone number) + 1` |
| Rates (cents per passenger per chargeable zone) | Adult 2,105 · Child 1,410 · Senior 1,025 · Student 1,750 |
| Category subtotal | `count x rate x chargeable zones` |
| Total fare | sum of the four subtotals |

| from \ to | Central | Midtown | Downtown |
|---|---|---|---|
| **Central** | 1 | 2 | 3 |
| **Midtown** | 2 | 1 | 2 |
| **Downtown** | 3 | 2 | 1 |

The assignment brief gives examples that are consistent with this rule (one zone inside a zone, two for neighbouring
zones, three for Central to Downtown or back). It is a **design assumption** because zone-only input does not identify a
specific route: the booking knows the two zones, not which stations or which track the passenger will use, so the
zones a train actually passes through cannot be counted. Worked examples (all checked by automated tests):

| Journey | Travellers | Chargeable zones | Subtotals (cents) | Total |
|---|---|---|---|---|
| Central -> Downtown | 2 Adults, 1 Child, 1 Senior, 1 Student (5) | 3 | 12,630 · 4,230 · 3,075 · 5,250 | **25,185 cents** |
| Midtown -> Midtown | 1 Child | 1 | 0 · 1,410 · 0 · 0 | **1,410 cents** |
| Central -> Midtown | 1 Adult | 2 | 2,105 x 2 = 4,210 · 0 · 0 · 0 | **4,210 cents** |

**Owner's edge case, kept separate from the assignment examples:** *Zord to Marend* is a test for the optional
**station** planner, not for the zone booking. Both stations are in Downtown, but the only track between them runs
Zord - Quthiel - Yaen - Centrala - Bylyn - Agralle - Docia - Marend (through Midtown and Central). The planner's
existing rule charges by zone numbers, so it charges **1 zone (2,105 cents for one adult)**, while the route passes
through 3 different zones. `tests/test_zone_booking.py::StationPlannerRegressionTests` records this behaviour so any change
to it is a deliberate decision (see `docs/FARE_POLICY.md`). The zone booking is unaffected: it never sees a route.

### Run it on Windows
```
cd "%USERPROFILE%\Downloads\CTA_Metro_App_v3_3\CTA_Metro_App_v3"
python -m pip install -r requirements.txt
python cta_app.py
```
(PowerShell: the same commands; or double-click `launch.bat`.) The zone ticket, station board and voucher work straight
away. The **optional station planner** needs routing switched on: copy your existing `cta_config.json` from your old
folder into this folder, or run `copy cta_config.example.json cta_config.json` and keep `"routing": {"provider":
"schematic"}`. Without it the planner says "Journey planning is not configured" (unchanged behaviour) and a red badge
appears in the header; the zone ticket still works.

Tests for this part (from the same folder):
```
python -m pip install -r requirements-dev.txt
python -m pytest tests\test_zone_booking.py -v
python -m unittest discover -s tests -p "test_zone_booking.py" -v
```
Full results, evidence screenshots and the Windows checks still to do: [`docs/APPENDIX2_TEST_REPORT.md`](docs/APPENDIX2_TEST_REPORT.md).

---

## Full app reference (station planner, tickets, payments, admin)

> **Status:** safety release. Payments, data storage, ticket validation, fares, staff permissions, routing and error
> handling were reworked and are covered by 383 automated tests. **Not production-ready yet:** see
> [`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md) for what still needs a real payment provider,
> credentials, owner decisions and manual testing.

## Setup
Python 3.10 or newer (tested on 3.12), with Tkinter.

```
python -m pip install --require-hashes -r requirements.lock      # exact, hash-checked versions tested for this release
copy cta_config.example.json cta_config.json                     # (cp on Mac/Linux) then edit it
python cta_app.py
```

The app starts in **production** mode unless told otherwise. In production online payment is off (no real payment
provider is built in) and a routing choice is required (the example settings choose the diagram estimates). To try the
whole flow with the test payment gateway: `CTA_ENV=development python cta_app.py` (PowerShell:
`$env:CTA_ENV="development"; python cta_app.py`), or set `"environment": "development"` in `cta_config.json`.
A yellow **DEVELOPMENT MODE** badge then shows in the header.

| Setting | Where | Purpose |
|---|---|---|
| `CTA_ENV` | environment (or `"environment"` in the settings) | `development`, `testing` or `production` (default) |
| `CTA_DATA_DIR` | environment | data folder (default `Documents/CTA_Vouchers`) |
| `CTA_TICKET_KEY` | environment (or `ticket_keys.json`, created once) | QR signing key; **back it up separately** |
| `CTA_GOOGLE_API_KEY` | environment (or `routing.google_api_key`) | only for live Google routing |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE_SID`, `TWILIO_VERIFY_CHANNEL` | environment (or `sms`) | real text-message sign-in |
| `CTA_DEVICE_ID` | environment | name of this gate/counter in the scan log |
| `routing`, `payments`, `sms` sections | `cta_config.json` | see the sections below |

Never put real keys or passwords in files that are shared or committed; prefer environment variables.

**Upgrading from 2.3** happens by itself on first start: the JSON files are backed up and copied into `cta.db`, then the
database is upgraded (with its own backup). See *Data files* and *Recovery* below.

## What passengers can do
Plan trip: **tap your start station on the map, then tap your destination** (or type the names) -> choose
**One way** or **Return** -> add travellers -> **Book ticket** -> sign in with a mobile number
-> type every passenger's name -> **choose how to pay** -> tickets are issued -> **Download PDF / Print / Open PDF**.

* **Sign-in:** mobile number with country code (`+44 7123 456789`), then a verification code by text message.
* **Names:** one name per traveller, printed on that traveller's ticket (max 20 travellers per booking).
* **Payment, asked before any ticket exists** (tap a picture): Visa/Mastercard card, mada, STC Pay, PayPal, Tabby, Tamara,
  or **Cash - pay at the counter**. A counter booking is reserved but **cannot board until staff mark it paid**.
* **Return ticket:** two single journeys; the way back is valid on the day of travel on any train. Each passenger gets
  `...-P1` (out) and `...-P1R` (return).
* **PDF:** one boarding-pass page (7.5 x 3.5 in) per passenger and journey (name, route, times, fare, QR code, large
  ticket number) plus an optional receipt page with the private recovery code. **Print** prints the ticket pages only. If a person cannot scan a QR
  code, staff can type the ticket number printed under it: both are checked against the same stored ticket.
  A cancelled or refunded journey is stamped on its page, and the ticket screen shows whether each journey is unused,
  used or cancelled.
* **My ticket:** signed-in passengers see their latest 4 bookings; anyone can use reference + recovery code.
* **Request a refund:** on a booking opened under **My ticket**, **Request refund** sends staff a short message (needs
  an unused journey; the person must be signed in with the booking's phone number or have entered its reference and
  recovery code). Nothing is cancelled until staff approve it. **The app has no refund policy of its own** (who is
  eligible, fees, deadlines): staff decide each request, and the owner still has to write that policy.
* **Report a problem** (Help page) goes to staff. Signed in, the reply appears under **My ticket** -> *Your messages*
  (open the page again to refresh); signed out, staff still receive it but cannot answer. Staff do not see this form.

## Admin portal
On the sign-in window press **Staff / admin sign-in**. On a new installation the first person creates the administrator
account; after that only an administrator can add staff (`python cta_staff.py add <name>`), disable or re-enable them,
change roles or reset passwords. Passwords are stored as salted PBKDF2 hashes (600,000 iterations). Five wrong passwords
lock that username for 30 s on every window and computer. Every sensitive action is checked against the signed-in
person's role in the database layer and written to the audit log - see [`docs/PERMISSIONS.md`](docs/PERMISSIONS.md).
**The role matrix there (supervisor / cashier / gate) is a proposal awaiting approval; until then only `admin` exists.**
After signing in an **Admin** tab appears:

* Period buttons: Today / This week (Mon-Sun) / This month / This year / All time (by booking time).
* Live numbers (page re-reads the booking file every 5 s): bookings, passengers, tickets (journeys), booked value,
  paid, **to collect at counter**, checked in.
* **Zone to zone:** a table of tickets from the start zone to the end zone, and a list of how many journeys moved into
  the next zone (Central -> Midtown, Midtown -> Downtown, and back).
* Passengers booked per day (14 days), busiest routes, how people paid.
* **Check in a ticket** (scan the QR code with a keyboard-style scanner, or type the ticket number). See *Ticket
  authenticity and entry rules* below: unknown, forged, unpaid, not-yet-valid, expired, cancelled, refunded and
  already-used tickets are all refused.
* **All tickets** list with search; select a row -> **Mark paid (counter)**, **Check in selected**, or **Ticket actions**
  (cancel a ticket or a whole booking, record a refund paid out, allow re-entry, correct a name).
* **Filters:** under the ticket list, **Show** picks a status (not used yet, checked in, paid, not paid yet, payment
  failed, cancelled, refund to pay out, refunded) and a payment method (card, mada, STC Pay, PayPal, Tabby, Tamara,
  cash); both combine, and **Clear** resets them. The search box also finds payment methods and states ("tamara",
  "cash", "refund due", "checked in"); every word typed must match. The line beside the filters shows "Showing N of M".
* **Passenger messages** and **Refund requests** tabs (the number of waiting items is in the tab title): select one to
  read the conversation, type a reply and press **Send reply**. On a refund request **Approve refund** cancels every unused
  journey of the booking (the same cancellation as *Ticket actions*, so a paid booking then shows *Refund to pay out*
  and you record the payout afterwards) and **Decline** changes no ticket; either way the passenger is told. Needs the
  `support.reply` permission (admin only until the owner approves the role matrix) and, to approve, `ticket.cancel`.
* **Booked but not travelled:** tickets whose departure passed with nobody checked in, with the booking **phone
  number** so staff can call, find out if the passenger still wants to travel and offer a new time or a return ticket.
* **Export CSV** (opens correctly in Excel).

## Ticket authenticity and entry rules
**What the QR code is.** A signed token, `CTA1:<key id>:<ticket id>:<signature>`, and nothing else. The ticket id is 128
random bits, different for every journey of every passenger, never reused and not derived from the booking number. The
signature is HMAC-SHA256 (Python standard library) over that id, so a code that was not made by this system, or was
altered by even one character, is refused. No name, route, price, date or recovery code is inside the code, so a photo of
a QR code reveals nothing about the passenger.

**The stored ticket is the authority.** The signature only proves a code is genuine; it does not say the ticket is paid,
valid or unused. Scanning and typing both end in the same function on the same stored record, inside one database
transaction. Names, ticket numbers, QR text and anything printed are never believed on their own. The recovery code is
only for viewing and reprinting a booking; it is not a ticket and is refused at the gate.

**Checks, in this order** (the first that fails decides): signed-in operator, old-style ticket without a status,
cancelled, refunded, payment (unpaid counter booking, online payment not confirmed, test-mode payment in production,
failed or cancelled payment), travel period (valid from the start of the travel day until 05:00 the next morning), already
used, and - when a gate names its station - that the journey starts there.

**Single use, atomically.** Check-in verifies and marks the ticket used in one transaction that holds the database write
lock. Two scans at the same moment, from two windows, computers or a scanner and a keyboard, are handled one after the
other, so only one gets in (tested with threads and separate processes). If the write fails, the ticket is **not** used.

**Return tickets.** A return booking gives each passenger two independent single-use tickets (`...-P1` and `...-P1R`),
each with its own id and QR code. Both are valid for the whole travel day, in either order; using one never touches the
other, and cancelling or re-admitting one leaves the other alone. One-way tickets are one such ticket. Every ticket has a
use counter (`max_uses`, 1 today); a future multi-ride product would be issued with `max_uses = N` and each scan would
use one ride atomically. No multi-ride product exists yet.

**Cancellations and refunds.** *Ticket actions > Cancel* works on an unused journey (never on a used one): paid money
becomes **refund due** for that journey's fare; an unpaid counter booking just owes less (and the payment is cancelled when
nothing is left). *Record refund paid out* closes it when staff have handed the money back. This app does not call a payment
provider's refund API (none is connected): for online payments staff refund in the provider's dashboard first and enter
its refund reference. A cancelled or refunded ticket is refused at once, in every window.

**Re-entry.** A used ticket normally stays used. If a gate faults or a passenger leaves by mistake, staff use *Allow
re-entry*: a reason is required, it only works on a used, non-cancelled ticket inside its travel period, it gives back one
use, and it is written to the audit log.

**Corrections.** A misspelt name can be corrected by staff while no journey of that passenger is used or cancelled; the
QR code does not change (it holds no name), so just reprint the PDF. A wrong route, date or fare is handled by cancelling
the ticket and booking again.

**The scan log** (`scans` table, append-only) keeps, for every attempt, accepted or refused: time, outcome code, ticket
number and id, which journey, QR or typed, the operator, the device (`CTA_DEVICE_ID`, otherwise the computer name) and
the station if given. It never stores passenger names, phone numbers, recovery codes or signatures. The audit log is
append-only too, and tickets and their ids can never be deleted or renumbered (database triggers enforce it).

**Signing key.** Kept outside the source code: set `CTA_TICKET_KEY` (64+ hex characters), or the app creates
`ticket_keys.json` in the data folder the first time (owner-only permissions). **Back this file up separately from
`cta.db`, and keep it private: anyone who has it can make tickets that verify.** The database remembers that signing has
started, so if the key file goes missing the app says so and pauses new bookings instead of silently making a new key
(which would invalidate every printed QR); typing ticket numbers still works. `python cta_recover.py rotate-key` adds a
new key (older keys keep verifying printed tickets), `retire-keys` drops the old ones, `key-status` lists key ids.

**Upgrading.** Opening an existing database makes a checked backup (`backups/..-pre-upgrade.db`), then adds ticket ids to
every existing ticket and the new columns, in one transaction. Tickets printed before this version carry the old readable
QR text, which is **not** accepted as a code: staff type the ticket number, or the passenger reprints the PDF from *My
ticket* to get a signed QR. Old tickets for a future date can no longer be used before their travel day.

## Setting up real text messages (Twilio Verify)
Without this the app still works: the code is shown on the sign-in window with a note that text messages are not set up.

Quickest way: `python setup_sms.py` - it asks for your Twilio Account SID, Auth Token and Verify Service SID, texts a code to
**your own phone**, and saves the settings (`cta_config.json`) once it works. Or set the environment variables
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_VERIFY_SERVICE_SID` (optional `TWILIO_VERIFY_CHANNEL` = sms | whatsapp | call).

What to expect (checked against Twilio's pages in October 2026, so re-check before relying on it):
* A **trial** account sends only to numbers you have verified in Twilio (the number you signed up with is verified
  automatically, so your own Saudi number works for testing), includes 40 free verifications and expires after a trial period.
* **Saudi Arabia is a restricted route.** Twilio's Saudi guidelines say a registered alphanumeric sender ID is required there and that
  Twilio cannot register one for a domestic Saudi brand, so SMS to +966 numbers can be blocked or delayed. The only way to know is
  to try `python setup_sms.py` with your number. If SMS does not arrive, try the `whatsapp` or `call` channel in the same script.
* Twilio generates and checks the code (expiry, attempt limits); the app never sees it.

## Payments - what is real and what is not
**Environment.** `CTA_ENV` (or `"environment"` in `cta_config.json`; the variable wins) is `development`, `testing` or
`production`. Anything missing or unrecognised counts as `production`, so a mis-configured install fails safe.

* **Cash/counter** is real in every environment: the booking is stored as `pending_counter` and only a signed-in staff
  member can mark it paid in the admin portal (the name of the staff member and the time are recorded).
* **Card, mada, STC Pay, PayPal, Tabby, Tamara**
  * **Development / testing:** the built-in `SimulatedGateway` approves instantly and **moves no money**. Every record it
    makes is flagged `simulated`, and the app, tickets, receipts and admin portal all say *test mode - no money taken*.
  * **Production:** the simulated gateway is refused. **No real payment provider is implemented in this build**, so
    online payment is switched off: the payment screen says live online payments are unavailable and only cash can be
    chosen. Tickets that were paid with the test gateway are refused at check-in and are not counted as money collected.
* **Verification.** A provider saying "approved" is never enough. A booking becomes paid only after the app asks the
  provider for its own record of the transaction (`verify`) and the status, transaction id, amount, currency and booking
  reference all match. Otherwise the payment is `failed` (mismatch, flagged for staff review), `pending` or `unknown`
  (booking kept, **no valid ticket, no PDF**) and can be confirmed later with `cta_payments.confirm_payment` or
  `cta_payments.handle_callback`, which re-verify with the provider and ignore any status sent in the notification itself.
* **No double payments.** The booking reference is sent as the idempotency key, and the payment-claims table in `cta.db` lets one
  provider transaction pay for exactly one booking, so repeated callbacks change nothing.
* **Adding a real provider.** Subclass `PaymentProvider` in `cta_payments.py`, implement `charge` and `verify` against
  the provider's official API (card details are entered on the provider's own page; credentials come from environment
  variables, never from files in this folder), register it in `REAL_PROVIDERS`, and set
  `"payments": {"provider": "<name>", "currency": "SAR"}`. A desktop app has no web address, so provider callbacks need a
  small server you run that calls `handle_callback`. Nothing is registered by default.
* Card numbers, security codes and provider secrets are never typed into, stored or logged by this app.

## Payment pictures
`assets/logos/*.png` are simple look-alike badges drawn by `tools/make_logos.py`; they are **not the official brand artwork**. For the
real logos, save your own PNG files (about 300x100) over `card.png`, `mada.png`, `stcpay.png`, `paypal.png`, `tabby.png`,
`tamara.png`, `cash.png`; the payment screen uses whatever is in that folder.

## Data files (folder `Documents\CTA_Vouchers`, or `CTA_DATA_DIR`)
Everything the app keeps is in one SQLite database, **`cta.db`**: bookings, passengers, tickets (with check-in state),
payments, payment claims (one transaction = one booking), staff (hashed passwords), passenger accounts, check-in scans and
an audit trail. `<reference>.pdf` is a copy of each booking. `backups/` holds checked copies of the database.
**`ticket_keys.json`** (unless `CTA_TICKET_KEY` is used) holds the QR signing key: it is *not* in the database backups, so
back it up separately and keep it private.

* **Several windows or computers** can share the folder. Each booking has a version number; if two people change the
  same booking at the same time, the second save is refused with a clear message and that window reloads the latest
  data, so nobody's change is silently overwritten. SQLite locking is reliable on a local disk; on a network share it
  depends on the file server, so for several computers a database server is the right long-term home.
* **Upgrading from 2.3 (JSON files).** On first start the app copies `history.json`, `accounts.json`, `staff.json`,
  `payments_ledger.json` and `payment_events.jsonl` into `backups/pre-migration-<time>/`, builds the database in a
  temporary file, checks that every booking comes back exactly as it was, and only then puts `cta.db` in place. The JSON
  files are never changed or deleted. If any of them is damaged the migration does not start.
* **A damaged or unreadable file is never treated as an empty installation.** The app starts in a safe mode with a red
  **DATA PROBLEM** badge: bookings, check-ins, payments and sign-ups are paused, nothing is overwritten, and the message
  says which file is affected. A read-only folder works the same way, but existing tickets can still be viewed.
* **Backups:** one automatic backup a day (the newest 30 are kept), plus a backup before every migration and before
  every restore. A missing `cta.db` when backups exist is reported instead of starting an empty database.

### Recovery (`cta_recover.py`)
```
python cta_recover.py status        # what is wrong, and which backups exist (changes nothing)
python cta_recover.py verify        # full integrity check
python cta_recover.py backup        # checked backup now
python cta_recover.py restore       # newest backup back in place (or give a file); close every CTA window first
python cta_recover.py migrate       # copy the old JSON files in again (safe to repeat; no duplicates, no overwrites)
```
A restore checks the backup first and keeps the database it replaces (even a damaged one) under `backups/replaced/`.

## Fares
**The fare rule has not been confirmed by the project owner yet** - see [`docs/FARE_POLICY.md`](docs/FARE_POLICY.md).
Today: chargeable zones = |band(start) - band(destination)| + 1 (Central 1, Midtown 2, Downtown 3), times the rate per
passenger type (Adult 2,105 · Child 1,410 · Senior 1,025 · Student 1,750 cents); a return is exactly two one-way fares.
On 584 of the 1,260 journeys (trips between different branches) this is cheaper than counting the zones the train
passes through; the app tells passengers so on those routes.

* One function, `cta_fares.quote`, prices everything. Screens, the payment amount, the booking, tickets, receipts and
  reports all use its result, and a booking is refused if the amount charged would differ from the amount shown.
* `fare_matrix.csv` lists every journey's fare. If the code ever disagrees with it (or the file is missing or damaged),
  new bookings pause with a message; nothing else stops. `python tools/fare_matrix.py --check` checks it,
  `--compare` lists every journey where the two zone rules differ.
* Fares are not set from a settings file. They change only in code, with approval, a new policy version and a new
  matrix (see the policy document, section 5).

## Counting rules (admin numbers)
A booking = one reference. A passenger = one person. A ticket = one journey for one passenger (a return booking gives each
passenger two). Zone flows count tickets, so a return counts in the reverse direction too. Fare = rate x charged zones x
journeys (1 or 2); each passenger's fares add up exactly to the booking total (tested). A cancelled or refunded journey is
not counted as a ticket and its fare leaves *Booked value*; money already paid stays in *Paid* until the refund is recorded,
and *Refunds to pay out* lists what is still owed.

## Distances, times and routing
The CTA network is fictional. **Stations, track links and zones** come from the Appendix 1 diagram; they are checked at
start-up (unknown stations, repeated or self links, impossible lengths, stations that cannot be reached) and journey
planning stops if they are wrong. Which stations a journey uses - and therefore its fare - is the same whatever provider
is chosen. `edge_weights.csv` is an export of the track lengths the code uses (kept identical by a test).

**Distance and time** are either *estimated* from the diagram (100 px = 1 km, 30 km/h, a train every 6 minutes,
05:00-midnight) or *live* from Google Routes. Choose in `cta_config.json` (see `cta_config.example.json`):

```json
"routing": {"provider": "schematic"}                     // estimated, labelled as such everywhere
"routing": {"provider": "google", "station_geo_file": "stations_geo.json", "travel_mode": "TRANSIT"}
```

* **Development / testing:** with no choice, estimates are used (labelled).
* **Production:** a choice is **required**; without one journey planning is unavailable, with a red
  *JOURNEY PLANNING UNAVAILABLE* badge and the reason. Copy `cta_config.example.json` to `cta_config.json` to start.
* **Google** needs an API key (`CTA_GOOGLE_API_KEY`, or `google_api_key`) and real coordinates for **every** station
  (`stations_geo.example.json` is only a template). A missing key, missing or invalid coordinates or an unknown travel
  mode stops journey planning with a message; it is never quietly replaced by estimates.
* If Google fails for a search, that route is shown with a yellow "Live routing unavailable: ESTIMATED" warning, the
  booking records `distance_verified: false`, and the change is written to the audit log once (and again when it
  recovers). Tickets and PDFs mark estimated distances. The API key never appears in messages or logs.
* `python cta_recover.py status` shows the routing health.

## When something goes wrong
* **Mistakes in what someone typed** (a name with digits, a phone number without a country code, 21 travellers, an
  unknown report period, a ticket number with control characters...) are refused with a plain message before anything is
  saved, charged, issued or exported. What was typed stays on screen; after a failed payment the passenger names are
  offered again, so nothing has to be retyped. Business limits are unchanged (99 per passenger type, 20 per booking,
  names 2-60 characters, phone numbers 8-15 digits, passwords 8-128 characters).
* **Unexpected failures** show a short message with a reference such as `ERR-20261025-7F3A2C` - never a stack trace, a
  file path or a key - and the details go to **`logs/diagnostics.log`** in the data folder (about 1 MB, plus one older
  file). API keys, tokens and anything that looks like `password=...` are replaced by `***` before writing.
* If anything fails **after a payment attempt**, the passenger is told the booking reference, the payment state and the
  transaction id to show staff, and the incident is logged. A crashing payment provider is recorded as *unknown*, never
  as paid. Pressing *Pay* twice cannot charge or issue twice.
* Problems that must not stop a booking (an audit row that could not be written, a sign-out the database did not accept)
  are logged instead of ignored; a failed sign-out also warns the person.
* A test (`tests/test_validation.py`) fails the build if any broad `except` in the app swallows an error without
  recording it or a written justification.

## Tests and checks
```
python -m pip install -r requirements.lock -r requirements-dev.txt
python -m compileall -q .
python -m unittest discover -s tests -v          # 383 tests; runs in testing mode
python -m pip check
python -m ruff check .                           # lint (settings in pyproject.toml)
python -m mypy                                   # types, non-GUI modules
python -m bandit -c pyproject.toml -r .          # security scan; each "# nosec" carries its reason
python -m pip_audit --require-hashes -r requirements.lock
python -m coverage run -m unittest discover -s tests && python -m coverage report
```
The screen tests need a display: without one, 7 test classes (53 tests) are reported as skipped (on Linux CI, run under
`xvfb-run`). Without `pypdf` 11 PDF-content tests skip, and without OpenCV one QR-decoding test skips.
Results of this release: [`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md).
`tests/ORIGINAL_TESTS.txt` lists the 107 tests of version 2.3; a test fails if any of them disappears.

## Known limits
* Passenger messages are kept in the database (tables `support_cases` and `support_messages`, created automatically the
  first time 2.5 opens a database; the schema version stays 3 and backups and restores include them). Messages cannot be
  edited or deleted, and the audit log records who answered and which case, never the message text. There is no
  notification: a passenger sees a reply when they open **My ticket**.
* The on-screen QR code is drawn at whole pixels per square (at least 6 px, about 250 px wide) because 2.4.x smeared
  it by resizing. It was tested here by decoding it with OpenCV, also after blur and after being halved, and the QR in
  the PDF was decoded after rendering the page. A phone camera in front of your own screen has **not** been tested.
* **Print** hands the PDF to Windows with the `print` command. That works only if your default PDF app has a Print
  action; if not, the app opens the PDF instead so you can press Ctrl+P. The tests prove what is sent and the fallback,
  not that a printer produced paper.
* Tickets from versions before 2.1 have no names and cannot be checked in; they still open and print.
* **Typing a ticket number is weaker than scanning a QR code.** Ticket numbers are readable and follow a sequence
  (`CTA-20261025-002-P1`), so someone who guesses a valid, paid, unused number could be admitted by a member of staff who
  does not look at who is presenting it. Typed entries are staff-only, logged as `manual`, and the screen shows the
  passenger's name so staff can compare it with their ID. Prefer scanning; if typed entry matters to you, add a printed
  check code to the ticket.
* The QR signature is symmetric (HMAC): every device that can verify codes holds the key and could also make them.
  Keep the key on trusted computers only. Verifying without being able to issue would need public-key signatures, which
  need a third-party package this project does not use.
* A scanner must act like a keyboard (it types the code into the check-in box, usually followed by Enter). There is no
  camera scanning inside the app. The signature alone can be checked offline, but single-use and payment status need the
  shared database, so a gate that cannot reach it cannot admit anyone.
* Gate stations are supported by the check-in function (`station=`) and by the scan log, but the admin screen does not
  have a station selector yet.
* Names print in any alphabet only if a Unicode font is found (Segoe UI / Arial on Windows); otherwise letters outside
  Western European print as `?`.
* Direct printing uses the default PDF app's print command on Windows and `lp` on Mac/Linux; if nothing prints use
  **Open PDF** then Ctrl+P.
* A damaged `cta_config.json` is reported instead of ignored: sign-in by text message is switched off (codes are never
  shown on screen as a fallback) and journey planning uses the built-in distances until the file is fixed.
* Staff sign-in is by password only (no two-factor sign-in). Permissions protect the app's operations and records,
  not the files: keep the data folder readable only by the account that runs the app (see `docs/PERMISSIONS.md`).
