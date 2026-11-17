"""CTA fares, bookings, tickets, history and PDF export (no GUI code here).

Fare rules are unchanged from the original kiosk (ATHE Unit 2, Appendix 2):
rate per passenger category x number of chargeable zones.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess  # only to open/print the app's own PDF, with fixed programs and no shell  # nosec B404
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cta_payments as payments
from cta_accounts import normalize_digits, secure_equal, validate_person_name
from cta_routing import ZONES, Route

__version__ = '2.4.0'

# Fares live in cta_fares.py (the single source). These names are kept so existing callers keep working.
import cta_fares as fares
from cta_fares import (CATEGORIES, FARE_POLICY_VERSION, FARE_RATES_CENTS, CategoryCharge, band_zones,  # noqa: F401
                       route_zones)

CATEGORY_HINT = {'Adult': '18 – 64', 'Child': 'Under 18', 'Senior': '65 and over', 'Student': 'With student ID'}
MAX_PER_CATEGORY = 99
MAX_PASSENGERS_PER_BOOKING = 20      # one named ticket per passenger; make a second booking for larger groups
FARE_MODE = fares.ACTIVE_RULE        # read-only: the rule is set (and approved) in cta_fares.py, not here


def format_cents(amount: int) -> str:
    """Amounts stay labelled in cents so they cannot be mistaken for whole currency units."""
    return f'{amount:,} cents'


# ------------------------------------------------------------------ fares (thin wrappers around cta_fares)
def chargeable_zones(origin: str, destination: str, route: Optional[Route] = None,
                     mode: str = FARE_MODE) -> int:
    """Zones charged under the active policy. ``mode`` other than the active rule is for comparison reports only."""
    return fares.zones_by_rule(mode, origin, destination, route)


def quote(origin: str, destination: str, counts: Dict[str, int], legs: int = 1,
          route: Optional[Route] = None) -> 'fares.FareQuote':
    return fares.quote(origin, destination, counts, legs, route)


def validate_counts(counts: Dict[str, int]) -> Tuple[bool, str]:
    for name, n in counts.items():
        if name not in FARE_RATES_CENTS:
            return False, f'Unknown passenger type: {name}.'
        if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > MAX_PER_CATEGORY:
            return False, f'{name}: enter a whole number from 0 to {MAX_PER_CATEGORY}.'
    if sum(counts.values()) == 0:
        return False, 'Add at least one traveller.'
    return True, ''


def validate_booking_size(counts: Dict[str, int]) -> Tuple[bool, str]:
    total = sum(counts.values())
    if total > MAX_PASSENGERS_PER_BOOKING:
        return False, (f'A booking can have up to {MAX_PASSENGERS_PER_BOOKING} travellers because every traveller gets '
                       f'a named ticket. You have {total}; please make a second booking for the rest.')
    return True, ''


def calculate_charges(counts: Dict[str, int], zones: int) -> Tuple[CategoryCharge, ...]:
    return tuple(CategoryCharge(c, counts.get(c, 0), FARE_RATES_CENTS[c], zones) for c in CATEGORIES)


def grand_total(charges) -> int:
    return sum(c.total_cents for c in charges)


def trip_total(counts: Dict[str, int], zones: int, legs: int = 1) -> int:
    """Booking total. A return ticket (legs=2) costs two single journeys."""
    return grand_total(calculate_charges(counts, zones)) * legs


def booking_summary(counts: Dict[str, int], zones: int, legs: int = 1) -> str:
    times = ' × 2 journeys' if legs == 2 else ''
    return '\n'.join(
        f'{name}: {n} × {FARE_RATES_CENTS[name]:,} cents × {zones} zone(s){times} = '
        f'{n * FARE_RATES_CENTS[name] * zones * legs:,} cents'
        for name, n in counts.items() if n > 0)


def people_label(counts: Dict[str, int]) -> str:
    parts = [f'{n} {name.lower()}{"" if n == 1 else "s"}' for name, n in counts.items() if n > 0]
    return ', '.join(parts) if parts else 'No travellers yet'


# ------------------------------------------------------------------ tickets
def new_recovery_code() -> str:
    return secrets.token_hex(8).upper()


TICKET_ID_RE = re.compile(r'^[A-Z2-7]{26}$')


def new_ticket_id() -> str:
    """128 random bits as 26 base-32 characters. One per ticket journey, never reused, never derived from the
    booking reference, so it cannot be guessed. This (not the printed ticket number) is what the QR token carries."""
    return base64.b32encode(secrets.token_bytes(16)).decode('ascii').rstrip('=')


def build_reference(issued_at: datetime, sequence: int) -> str:
    return f'CTA-{issued_at:%Y%m%d}-{sequence:03d}'


def next_sequence(history: List[dict], issued_at: datetime) -> int:
    """Per-day sequence that survives restarts (the old per-session counter repeated)."""
    prefix = f'CTA-{issued_at:%Y%m%d}-'
    return 1 + sum(1 for r in history if str(r.get('reference', '')).startswith(prefix))


def passenger_slots(counts: Dict[str, int]) -> List[Tuple[int, str]]:
    """[(1, 'Adult'), (2, 'Adult'), (3, 'Child')]: one slot per traveller, in category order."""
    slots: List[Tuple[int, str]] = []
    for cat in CATEGORIES:
        for _ in range(counts.get(cat, 0)):
            slots.append((len(slots) + 1, cat))
    return slots


def validate_passenger_names(slots: Sequence[Tuple[int, str]], raw_names: Sequence[str]
                             ) -> Tuple[List[str], Dict[int, str]]:
    """Clean every name. Returns (cleaned names, {slot number: error}); no errors means all names are fine."""
    cleaned: List[str] = []
    errors: Dict[int, str] = {}
    for (no, cat), raw in zip(slots, raw_names):
        name, err = validate_person_name(raw, f'passenger {no}')
        cleaned.append(name)
        if err:
            errors[no] = err
    if len(raw_names) != len(slots):
        errors[0] = 'The number of names does not match the number of travellers.'
    return cleaned, errors


def build_passengers(reference: str, counts: Dict[str, int], names: Sequence[str], zones: int,
                     legs: int = 1) -> List[dict]:
    slots = passenger_slots(counts)
    cleaned, errors = validate_passenger_names(slots, names)
    if errors:
        raise ValueError(next(iter(errors.values())))
    out = []
    for (no, cat), name in zip(slots, cleaned):
        leg_fare = FARE_RATES_CENTS[cat] * zones
        p = {'no': no, 'name': name, 'category': cat, 'ticket_no': f'{reference}-P{no}',
             'ticket_id': new_ticket_id(),
             'leg_fare_cents': leg_fare, 'fare_cents': leg_fare * legs, 'status': 'issued'}
        if legs == 2:
            p['return_ticket_no'] = f'{reference}-P{no}R'
            p['return_ticket_id'] = new_ticket_id()
            p['return_status'] = 'issued'
        out.append(p)
    return out


def make_record(route: Route, counts: Dict[str, int], zones: int, depart: datetime,
                issued_at: datetime, history: List[dict], names: Optional[Sequence[str]] = None,
                account_phone: Optional[str] = None, legs: int = 1, payment: Optional[dict] = None,
                reference: Optional[str] = None) -> dict:
    """Build a ticket record. Pass ``names`` (one per traveller) to put a named ticket on every passenger."""
    if legs not in (1, 2) or isinstance(legs, bool):
        raise ValueError('A booking is one way (1) or return (2).')
    if not isinstance(depart, datetime) or not isinstance(issued_at, datetime):
        raise ValueError('The departure and booking times must be dates and times.')
    if account_phone is not None and not re.fullmatch(r'\+[1-9]\d{7,14}', str(account_phone)):
        raise ValueError('The account phone number is not in the stored +digits form.')
    if names is not None and (isinstance(names, str) or not all(isinstance(n, str) for n in names)):
        raise ValueError('Passenger names must be a list of text values.')
    fare = fares.quote(route.origin, route.destination, counts, legs, route)   # FareError is a ValueError
    if zones != fare.zones:
        raise ValueError(f'Fare mismatch: {zones} zone(s) were shown but the fare policy charges {fare.zones}. '
                         'Nothing was booked.')
    if reference is None:
        reference = build_reference(issued_at, next_sequence(history, issued_at))
    elif not isinstance(reference, str) or not re.fullmatch(rf'CTA-{issued_at:%Y%m%d}-\d{{3,6}}', reference):
        raise ValueError('The booking reference does not belong to this booking day.')
    record: Dict[str, Any] = {
        'reference': reference,
        'recovery_code': new_recovery_code(),
        'fare_policy_version': FARE_POLICY_VERSION,
        'issued_at': issued_at.strftime('%Y-%m-%d %H:%M:%S'),
        'depart_at': depart.strftime('%Y-%m-%d %H:%M'),
        'from_station': route.origin,
        'to_station': route.destination,
        'origin_zone': ZONES[route.origin],
        'destination_zone': ZONES[route.destination],
        'route': list(route.stations),
        'km': round(route.km, 3),
        'ride_minutes': round(route.ride_minutes, 3),
        'distance_source': route.source,
        'distance_verified': bool(getattr(route, 'verified', False)),   # False = estimated, never shown as live
        'zones': zones,
        'counts': {k: v for k, v in counts.items() if v},
        'total_cents': fare.total_cents,
        'legs': legs,
        'trip_type': 'return' if legs == 2 else 'one-way',
    }
    if payment is not None:
        record['payment'] = payment
    if names is not None:
        record['passengers'] = build_passengers(reference, counts, names, zones, legs)
        if sum(p['fare_cents'] for p in record['passengers']) != record['total_cents']:
            raise ValueError('Internal error: passenger fares do not add up to the total.')
        if any(p['fare_cents'] != fare.passenger_fare(p['category']) for p in record['passengers']):
            raise ValueError('Internal error: a passenger fare differs from the fare policy.')
    if account_phone:
        record['account_phone'] = account_phone
    return record


def passengers_of(record: dict) -> List[dict]:
    """Named passengers of a record. Tickets saved by version 2.0 had no names: they get one placeholder entry."""
    pax = record.get('passengers')
    if pax:
        return list(pax)
    people = ', '.join(f'{v} {k.lower()}' for k, v in record.get('counts', {}).items() if v) or 'traveller(s)'
    return [{'no': 1, 'name': 'Name not recorded', 'category': people,
             'ticket_no': record.get('reference', 'CTA'), 'fare_cents': record.get('total_cents', 0),
             'legacy': True}]


def ticket_qr_payload(record: dict, passenger: dict, leg: str = 'out', signer=None) -> str:
    """The text inside the QR code: a signed token (see cta_tickets). It carries the ticket id and a signature and
    nothing else - no name, route, price or recovery code. Raises cta_tickets.KeyUnavailable without a signing key."""
    import cta_tickets
    return cta_tickets.qr_payload(record, passenger, leg, signer)


def ticket_qr_text(record: dict, passenger: dict, leg: str = 'out', signer=None) -> str:
    """The one line of text inside the QR code: readable details a phone camera can show, then the signed token."""
    import cta_tickets
    return cta_tickets.qr_text(record, passenger, leg, signer)


def payment_of(record: dict) -> dict:
    """The booking's payment record (bookings made before payments existed count as paid)."""
    return record.get('payment') or payments.legacy_payment(record.get('total_cents', 0))


# ------------------------------------------------------------------ ticket lifecycle (staff actions)
def find_ticket(history: List[dict], ticket_no: str) -> Optional[Tuple[dict, dict, str]]:
    """(booking, passenger, 'out' | 'return') for a ticket number, ignoring case and spaces."""
    wanted = ''.join((ticket_no or '').split()).upper()
    if not wanted:
        return None
    for rec in history:
        for p in passengers_of(rec):
            if p['ticket_no'].upper() == wanted:
                return rec, p, 'out'
            if p.get('return_ticket_no', '').upper() == wanted:
                return rec, p, 'return'
        if str(rec.get('reference', '')).upper() == wanted and len(passengers_of(rec)) == 1:
            return rec, passengers_of(rec)[0], 'out'
    return None


def find_ticket_by_id(history: List[dict], ticket_id: str) -> Optional[Tuple[dict, dict, str]]:
    """(booking, passenger, 'out' | 'return') for a ticket id (the value inside a verified QR token)."""
    if not ticket_id:
        return None
    for rec in history:
        for p in rec.get('passengers') or []:
            if p.get('ticket_id') == ticket_id:
                return rec, p, 'out'
            if p.get('return_ticket_id') == ticket_id:
                return rec, p, 'return'
    return None


def _departure(record: dict) -> Optional[datetime]:
    try:
        return datetime.strptime(record['depart_at'], '%Y-%m-%d %H:%M')
    except (KeyError, ValueError, TypeError):
        return None


def ticket_valid_from(record: dict) -> Optional[datetime]:
    """A ticket works from the start of its travel day (before this it is 'not valid yet')."""
    dep = _departure(record)
    return dep.replace(hour=0, minute=0, second=0, microsecond=0) if dep else None


def ticket_valid_until(record: dict) -> Optional[datetime]:
    """A ticket works on its travel day and until the first train next morning (05:00)."""
    dep = _departure(record)
    return (dep + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0) if dep else None


def validate_ticket(history: List[dict], ticket_no: str, now: datetime, staff: str = '', *, signer=None,
                    environment: Optional[str] = None, station: str = '') -> Tuple[bool, str, Optional[dict]]:
    """Staff check-in on bookings held in memory: tests and tools. The app itself uses ``cta_db.Store.check_in``,
    which runs the same decision (cta_tickets.check_ticket) inside one database transaction so that two scans of the
    same ticket cannot both succeed. ``ticket_no`` may be a typed number or a scanned QR token."""
    import cta_tickets
    res = cta_tickets.check_in_memory(history, ticket_no, now, staff, signer=signer, environment=environment,
                                      station=station)
    return res.ok, res.message, res.record


def mark_paid(record: dict, now: datetime, staff: str = '') -> Tuple[bool, str]:
    """Counter payment received: turns 'pay at the counter' into paid. Only a signed-in staff member can do this,
    and only for cash bookings - an online payment is never marked paid by hand."""
    pay = payment_of(record)
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can record a counter payment.'
    if pay.get('status') == payments.PAID:
        return False, 'This booking is already paid.'
    if pay.get('status') != payments.PENDING_COUNTER or payments.METHOD_KIND.get(str(pay.get('method'))) != 'counter':
        return False, 'Only bookings waiting for counter payment can be marked paid.'
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    updated = dict(pay)
    updated.update(status=payments.PAID, paid_at=stamp, received_by=staff)
    updated['events'] = list(pay.get('events', [])) + [{'at': stamp, 'status': payments.PAID,
                                                        'source': 'counter', 'by': staff}]
    record['payment'] = updated
    return True, f"Payment of {pay.get('amount_cents', 0):,} cents recorded for {record.get('reference', '')}."


def find_ticket_with_code(history: List[dict], reference: str, code: str) -> Optional[dict]:
    """The booking for a reference + recovery code, or None. Never raises on what the person typed: text holding
    characters from another alphabet is simply no match (it used to crash the lookup, so the wrong attempt was
    never counted towards the lockout)."""
    if not isinstance(reference, str) or not isinstance(code, str):
        return None
    reference = ''.join(normalize_digits(reference).split()).upper()
    code = ''.join(normalize_digits(code).split()).upper()
    if not reference or not code or len(reference) > 40 or len(code) > 64:
        return None
    for record in reversed(history):
        stored = record.get('recovery_code')
        if (stored and secure_equal(str(record.get('reference', '')).upper(), reference)
                and secure_equal(str(stored).upper(), code)):
            return record
    return None


# ------------------------------------------------------------------ storage
def data_dir() -> Path:
    env = os.environ.get('CTA_DATA_DIR')
    return Path(env) if env else Path.home() / 'Documents' / 'CTA_Vouchers'


TICKET_STATES = ('issued', 'boarded', 'cancelled', 'refunded')
REFUND_STATES = ('due', 'done')
MAX_USES_LIMIT = 100
_TS = '%Y-%m-%d %H:%M:%S'


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_history(entries) -> None:
    """Raise storage.DataInvalid unless ``entries`` is a list of well-formed bookings with unique identifiers.
    Messages name the booking and field, never personal data."""
    from cta_storage import DataInvalid
    if not isinstance(entries, list):
        raise DataInvalid('the bookings must be a list.')
    refs, tickets, ids = set(), set(), set()
    for i, rec in enumerate(entries, 1):
        where = f'booking #{i}'
        if not isinstance(rec, dict):
            raise DataInvalid(f'{where} is not a record.')
        ref = rec.get('reference')
        if not isinstance(ref, str) or not ref.strip():
            raise DataInvalid(f'{where} has no booking reference.')
        where = f'booking {ref}'
        if ref.upper() in refs:
            raise DataInvalid(f'{where} appears more than once.')
        refs.add(ref.upper())
        try:
            datetime.strptime(str(rec.get('issued_at')), _TS)
        except ValueError:
            raise DataInvalid(f'{where} has a missing or invalid issue time.') from None
        if not _is_int(rec.get('total_cents')) or rec['total_cents'] < 0:
            raise DataInvalid(f'{where} has an invalid total.')
        if rec.get('legs', 1) not in (1, 2) or isinstance(rec.get('legs', 1), bool):
            raise DataInvalid(f'{where} has an invalid trip type.')
        if 'passengers' in rec:
            pax = rec['passengers']
            if not isinstance(pax, list) or not pax:
                raise DataInvalid(f'{where} has an invalid passenger list.')
            nos = set()
            for p in pax:
                if not isinstance(p, dict) or not _is_int(p.get('no')) or p['no'] in nos:
                    raise DataInvalid(f'{where} has an invalid or repeated passenger number.')
                nos.add(p['no'])
                if not isinstance(p.get('name'), str) or not isinstance(p.get('category'), str):
                    raise DataInvalid(f'{where} has a passenger without a name or category.')
                if not _is_int(p.get('fare_cents')) or p['fare_cents'] < 0:
                    raise DataInvalid(f'{where} has a passenger with an invalid fare.')
                for key, st, pre in (('ticket_no', 'status', ''), ('return_ticket_no', 'return_status', 'return_')):
                    if key not in p:
                        continue
                    tno = p[key]
                    if not isinstance(tno, str) or not tno.strip():
                        raise DataInvalid(f'{where} has an empty ticket number.')
                    if tno.upper() in tickets:
                        raise DataInvalid(f'ticket {tno} appears more than once.')
                    tickets.add(tno.upper())
                    if st in p and p[st] not in TICKET_STATES:
                        raise DataInvalid(f'ticket {tno} has an unknown status.')
                    tid = p.get(pre + 'ticket_id')
                    if tid is not None:
                        if not isinstance(tid, str) or not TICKET_ID_RE.match(tid):
                            raise DataInvalid(f'ticket {tno} has a malformed ticket id.')
                        if tid in ids:
                            raise DataInvalid(f'ticket {tno} repeats a ticket id.')
                        ids.add(tid)
                    max_uses = p.get(pre + 'max_uses', 1)
                    uses = p.get(pre + 'uses', 0)
                    if (not _is_int(max_uses) or not 1 <= max_uses <= MAX_USES_LIMIT or not _is_int(uses)
                            or not 0 <= uses <= max_uses):
                        raise DataInvalid(f'ticket {tno} has an invalid use count.')
                    if pre + 'refund_state' in p and p[pre + 'refund_state'] not in REFUND_STATES:
                        raise DataInvalid(f'ticket {tno} has an unknown refund state.')
                    if pre + 'refund_cents' in p and (not _is_int(p[pre + 'refund_cents']) or p[pre + 'refund_cents'] < 0):
                        raise DataInvalid(f'ticket {tno} has an invalid refund amount.')
                if 'ticket_no' not in p:
                    raise DataInvalid(f'{where} has a passenger without a ticket number.')
        if 'payment' in rec:
            pay = rec['payment']
            if not isinstance(pay, dict) or not isinstance(pay.get('method'), str):
                raise DataInvalid(f'{where} has an invalid payment record.')
            if pay.get('status') not in payments.STATES:
                raise DataInvalid(f'{where} has an unknown payment status.')
            if not _is_int(pay.get('amount_cents')) or pay['amount_cents'] < 0:
                raise DataInvalid(f'{where} has an invalid payment amount.')


def load_history(path: Path) -> List[dict]:
    """Bookings from a JSON file. Missing file -> [] (first run). Damaged/unreadable/invalid -> StorageError."""
    from cta_storage import read_json
    return read_json(Path(path), list, validate_history, 'bookings')


def save_history(path: Path, entries: List[dict]) -> None:
    """Validated, crash-safe write. Refuses to overwrite a damaged file; keeps the previous version as .bak."""
    from cta_storage import atomic_write_json
    atomic_write_json(Path(path), entries, validate_history, 'bookings')


def log_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + '\n')


# ------------------------------------------------------------------ QR + PDF + printing
def qr_image(text: str, size: int = 200):
    """PIL image of a QR code (for the on-screen ticket), or None if qrcode/Pillow are missing.

    ``size`` is the SMALLEST width in pixels wanted. The picture is drawn with a whole number of pixels per QR square
    and is never resized afterwards: scaling a QR code by a fraction smears the squares into grey, which phone
    cameras and scanners read badly (2.4.1 drew 3.7 px squares and stopped scanning with a little blur)."""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        squares = qr.modules_count + 2 * 4                       # the code plus its 4-square white margin
        box = max(5, -(-int(size) // squares))                   # at least 5 px per square, rounded UP
        qr.box_size = box
        return qr.make_image(fill_color='black', back_color='white').convert('RGB')
    except Exception as exc:  # noqa: BLE001 - preview only; the screen says so and the PDF has its own QR
        import cta_errors
        cta_errors.report(exc, 'drawing a QR preview')
        return None


def export_ticket_pdf(record: dict, path: Path, include_receipt: bool = True, signer=None,
                      ticket_server_url: str = '', ticket_url_map: Optional[dict] = None) -> None:
    """Write the ticket PDF: one boarding-pass-sized page per ticket + an optional A4 receipt page.

    *ticket_url_map* ({ticket_id: https URL}) makes a ticket's QR open its page on a phone; tickets not in
    the map get the signed station-scanner code. Building the map needs the database, so the caller does
    it (see cta_app._build_ticket_url_map). *ticket_server_url* is accepted for old callers and not used.
    """
    import cta_pdf
    cta_pdf.build_ticket_pdf(record, Path(path), include_receipt=include_receipt, signer=signer,
                             ticket_url_map=dict(ticket_url_map or {}))


def downloads_dir(fallback: Optional[Path] = None) -> Path:
    d = Path.home() / 'Downloads'
    return d if d.is_dir() else (fallback or data_dir())


def open_pdf(path: Path) -> Tuple[bool, str]:
    """Open the PDF in the computer's default PDF viewer."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path))  # type: ignore[attr-defined]  # default viewer for our own PDF  # nosec B606
        elif sys.platform == 'darwin':
            subprocess.run(['open', str(path)], check=True, timeout=15)  # system viewer, list args, our own file  # nosec B603 B607
        else:
            subprocess.run(['xdg-open', str(path)], check=True, timeout=15)  # system viewer, list args, our own file  # nosec B603 B607
        return True, f'Opened {path}'
    except (OSError, subprocess.SubprocessError, AttributeError) as exc:
        return False, f'Could not open the PDF automatically ({exc}). It is saved at {path}'


def print_pdf(path: Path) -> Tuple[bool, str]:
    """Send the PDF to the default printer. If that is not possible, open it so the person can press Ctrl+P."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(path), 'print')  # type: ignore[attr-defined]  # print our own PDF  # nosec B606
            return True, ('Sent to your default printer. If nothing prints, press Open PDF and use Ctrl+P.')
        lp = shutil.which('lp')
        if lp:
            done = subprocess.run([lp, str(path)], capture_output=True, text=True, timeout=30)  # full path from which()  # nosec B603
            if done.returncode == 0:
                return True, 'Sent to your default printer.'
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    ok, msg = open_pdf(path)
    if ok:
        return True, 'Opened the ticket. Press Ctrl+P (Cmd+P on Mac) to print it.'
    return False, msg
