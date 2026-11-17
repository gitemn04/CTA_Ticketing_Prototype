"""Ticket authenticity and entry rules (no GUI code here).

What a QR code is
-----------------
The QR code is ONE line of text. A phone camera shows it as readable text with no app and no internet, and
a keyboard-style scanner types it in one go:

    CTA METRO / <NAME> <CATEGORY> / <FROM> TO <TO> / <DATE TIME> / <TICKET NUMBER> / CTA1:<key id>:<ticket id>:<signature>

Only the last part is trusted. The scanner reads the signed *token* (``CTA1:...``) and ignores everything
else, so changing the readable part of a code never changes who is let in:

    CTA1:<key id>:<ticket id>:<signature>

* ``ticket id``  - 128 random bits, unique per ticket (outbound and return legs get one each), never reused.
* ``signature``  - HMAC-SHA256 (Python standard library, nothing home-made) over the ticket id, truncated to 128 bits.
  It proves the code was made by this system. It does NOT say the ticket is paid, valid or unused.
* The readable part shows the passenger's name, route and time (like an airline boarding pass), so anybody who
  photographs a ticket can read them. It never contains the price, phone number or payment details.

The stored ticket record is the only authority. Scanning a QR code and typing a ticket number both end in the same
function (``check_ticket``) on the same stored record. The QR adds one extra proof (the signature); it never
replaces a record check, and nothing printed on a ticket or typed by the operator is believed without it.

Signing keys
------------
Kept outside the source code: ``CTA_TICKET_KEY`` (hex, at least 32 bytes) or a ``ticket_keys.json`` file in the data
folder (created once, owner-only permissions where the OS supports it). The database remembers that signing has
started, so a *missing* key file is reported instead of silently replaced (which would make every printed QR fail).
Back the key file up separately from ``cta.db``; ``cta_recover.py rotate-key`` adds a new key and keeps the old ones
verifying until you retire them.

Ticket life cycle (per journey leg)
-----------------------------------
``issued`` -> ``boarded`` (used)                       normal single-use entry
``issued`` -> ``cancelled`` -> ``refunded``             cancelled before use; refund recorded when the money is returned
``boarded`` -> ``issued``                               staff-authorised re-entry (reason required, audited)

* One-way: one ticket. Return: two independent single-use tickets (outbound, return) with their own ids, numbers
  and QR codes; both are valid on the whole travel day in either order, and using one never touches the other.
* A per-ticket use counter (``max_uses``, default 1) is what makes a ticket single-use. A multi-ride product would
  simply be issued with ``max_uses = N``; no such product exists today.
* Corrections: a misspelt name can be corrected by staff while the ticket is unused (the QR stays valid because it
  carries no name). A wrong route, date or fare is handled by cancelling the ticket and booking again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cta_core as core
import cta_payments as payments

TOKEN_PREFIX = 'CTA1'  # token format prefix, not a password  # nosec B105
KEY_FILE = 'ticket_keys.json'
MIN_KEY_BYTES = 32
MAC_BYTES = 16
MAX_REASON = 120
MAX_PRESENTED = 512
_B32 = '[A-Z2-7]'
_TOKEN = re.compile(rf'^{TOKEN_PREFIX}:([0-9A-F]{{4}}):({_B32}{{26}}):({_B32}{{26}})$')
_LEGACY_QR = ('CTA METRO TICKET', 'CTA METRO RETURN TICKET')

# outcome codes (stored in the scan log; never contain personal data)
OK, UNKNOWN, FORGED, MALFORMED, LEGACY_QR, KEY_UNAVAILABLE = 'OK', 'UNKNOWN', 'FORGED', 'MALFORMED', 'LEGACY_QR', 'KEY_UNAVAILABLE'
UNPAID, TEST_PAYMENT, UNCONFIRMED = 'UNPAID', 'TEST_PAYMENT', 'UNCONFIRMED'
NOT_YET_VALID, EXPIRED, USED, CANCELLED, REFUNDED = 'NOT_YET_VALID', 'EXPIRED', 'USED', 'CANCELLED', 'REFUNDED'
WRONG_STATION, NOT_AUTHORIZED, EMPTY, LEGACY_TICKET, BAD_RECORD = 'WRONG_STATION', 'NOT_AUTHORIZED', 'EMPTY', 'LEGACY_TICKET', 'BAD_RECORD'
CODES = (OK, UNKNOWN, FORGED, MALFORMED, LEGACY_QR, KEY_UNAVAILABLE, UNPAID, TEST_PAYMENT, UNCONFIRMED, NOT_YET_VALID,
         EXPIRED, USED, CANCELLED, REFUNDED, WRONG_STATION, NOT_AUTHORIZED, EMPTY, LEGACY_TICKET, BAD_RECORD)


class KeyUnavailable(Exception):
    """No signing key can be used. ``str(exc)`` is safe to show to an operator."""


# ------------------------------------------------------------------ signing keys
def key_id(secret: bytes) -> str:
    """Short public fingerprint of a key (reveals nothing useful about a 256-bit secret)."""
    return hashlib.sha256(b'CTA-KEYID|' + secret).hexdigest()[:4].upper()


def _decode_env_key(text: str) -> bytes:
    raw = bytes.fromhex(text.strip())
    if len(raw) < MIN_KEY_BYTES:
        raise ValueError('too short')
    return raw


class TicketSigner:
    """Signs and verifies ticket tokens. The first key signs; every key verifies."""

    def __init__(self, secrets_: Sequence[bytes]):
        if not secrets_ or any(len(s) < MIN_KEY_BYTES for s in secrets_):
            raise KeyUnavailable('The ticket signing key is missing or too short.')
        self._keys: List[Tuple[str, bytes]] = [(key_id(s), bytes(s)) for s in secrets_]

    @property
    def current_kid(self) -> str:
        return self._keys[0][0]

    @property
    def kids(self) -> List[str]:
        return [k for k, _ in self._keys]

    @staticmethod
    def _mac(secret: bytes, ticket_id: str) -> str:
        digest = hmac.new(secret, b'CTA-TICKET-v1|' + ticket_id.encode('ascii'), hashlib.sha256).digest()
        return base64.b32encode(digest[:MAC_BYTES]).decode('ascii').rstrip('=')

    def sign(self, ticket_id: str) -> str:
        if not core.TICKET_ID_RE.match(ticket_id or ''):
            raise ValueError('Not a ticket id.')
        kid, secret = self._keys[0]
        return f'{TOKEN_PREFIX}:{kid}:{ticket_id}:{self._mac(secret, ticket_id)}'

    def verify(self, token: str) -> Optional[str]:
        """The ticket id if the token is well-formed and correctly signed by a key we hold, else None."""
        m = _TOKEN.match(token or '')
        if not m:
            return None
        kid, ticket_id, mac = m.groups()
        good = False
        for k, secret in self._keys:
            if k == kid and hmac.compare_digest(self._mac(secret, ticket_id), mac):
                good = True               # keep looping: constant work whichever key matched
        return ticket_id if good else None


def signer_from_env() -> Optional[TicketSigner]:
    current = os.environ.get('CTA_TICKET_KEY')
    if not current:
        return None
    try:
        keys = [_decode_env_key(current)]
        previous = os.environ.get('CTA_TICKET_KEY_PREVIOUS')
        if previous:
            keys.append(_decode_env_key(previous))
    except ValueError:
        raise KeyUnavailable('CTA_TICKET_KEY must be hexadecimal text of at least 64 characters.')
    return TicketSigner(keys)


def _key_file(folder: Path) -> Path:
    return Path(folder) / KEY_FILE


def _validate_keyfile(data) -> None:
    from cta_storage import DataInvalid
    if not isinstance(data, dict) or not isinstance(data.get('keys'), list) or not data['keys']:
        raise DataInvalid('the key file must list at least one key.')
    for k in data['keys']:
        try:
            ok = isinstance(k, dict) and len(bytes.fromhex(k['secret'])) >= MIN_KEY_BYTES
        except (KeyError, ValueError, TypeError):
            ok = False
        if not ok:
            raise DataInvalid('the key file contains a malformed key.')


def read_key_file(folder: Path) -> Optional[List[bytes]]:
    """None when there is no key file; StorageError when it is damaged (never silently replaced)."""
    from cta_storage import read_json
    path = _key_file(folder)
    if not path.exists():
        return None
    data = read_json(path, dict, _validate_keyfile, 'ticket signing keys')
    return [bytes.fromhex(k['secret']) for k in data['keys']]


def _new_key_entry(now: Optional[datetime] = None) -> dict:
    secret = secrets.token_bytes(32)
    return {'kid': key_id(secret), 'secret': secret.hex(), 'created': (now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')}


def _create_key_file(folder: Path, now: Optional[datetime] = None) -> None:
    """Create-if-absent, owner-only, never visible half-written. If another window wins the race, its key is used.

    The complete key is written to a private temporary file first and then published with an atomic hard link,
    which fails (FileExistsError) if the file already exists. A window that starts at the same moment therefore sees
    either no file or a complete one - never an empty file that looks damaged."""
    import tempfile
    from cta_storage import DataWriteError
    path = _key_file(folder)
    payload = json.dumps({'version': 1, 'keys': [_new_key_entry(now)]}, indent=2).encode('utf-8')
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.ticket_keys.', suffix='.tmp', dir=str(path.parent))   # mode 0600
        with os.fdopen(fd, 'wb') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            return                                        # another window published its key first: use that one
        except (AttributeError, NotImplementedError, PermissionError):
            _create_key_file_exclusive(path, payload)     # file system without hard links
        except OSError as exc:
            if getattr(exc, 'errno', None) in (1, 18, 38, 95):     # EPERM, EXDEV, ENOSYS, EOPNOTSUPP: no hard links
                _create_key_file_exclusive(path, payload)
            else:
                raise
    except DataWriteError:
        raise
    except OSError:
        raise DataWriteError('The ticket signing key could not be created (folder not writable).', path)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _create_key_file_exclusive(path: Path, payload: bytes) -> None:
    """Fallback without hard links: exclusive create, then write (a very short window where the file is empty)."""
    from cta_storage import DataWriteError
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise DataWriteError('The ticket signing key could not be saved.', path)


def rotate_key(folder: Path, now: Optional[datetime] = None) -> str:
    """Add a new current key; older keys keep verifying printed tickets. Returns the new key id."""
    from cta_storage import atomic_write_bytes
    existing = read_key_file(folder)
    entries = []
    if existing is not None:
        from cta_storage import read_json
        entries = read_json(_key_file(folder), dict, _validate_keyfile, 'ticket signing keys')['keys']
    entry = _new_key_entry(now)
    atomic_write_bytes(_key_file(folder), json.dumps({'version': 1, 'keys': [entry] + entries}, indent=2).encode('utf-8'))
    return entry['kid']


def retire_old_keys(folder: Path) -> int:
    """Keep only the current key. Tickets printed with a retired key stop verifying until they are reprinted."""
    from cta_storage import atomic_write_bytes, read_json
    data = read_json(_key_file(folder), dict, _validate_keyfile, 'ticket signing keys')
    dropped = len(data['keys']) - 1
    if dropped > 0:
        data['keys'] = data['keys'][:1]
        atomic_write_bytes(_key_file(folder), json.dumps(data, indent=2).encode('utf-8'))
    return dropped


def key_summary(folder: Path) -> List[Tuple[str, str]]:
    """[(key id, created)] - never the secrets."""
    from cta_storage import read_json
    path = _key_file(folder)
    if not path.exists():
        return []
    data = read_json(path, dict, _validate_keyfile, 'ticket signing keys')
    return [(k.get('kid') or key_id(bytes.fromhex(k['secret'])), str(k.get('created', ''))) for k in data['keys']]


def load_signer(folder: Path, store=None, now: Optional[datetime] = None) -> TicketSigner:
    """The signer for this data folder.

    Order: CTA_TICKET_KEY, then ticket_keys.json. A key file is only created on a genuinely new installation: if the
    database says signing already started and no key can be found, ``KeyUnavailable`` is raised instead, because a
    brand-new key would invalidate every ticket already printed.
    """
    signer = signer_from_env()
    if signer is None:
        keys = read_key_file(folder)                      # StorageError if damaged
        if keys is None:
            if store is not None and store.meta_get('signing_started'):
                raise KeyUnavailable('The ticket signing key file is missing, although tickets have been signed before. '
                                     'QR codes cannot be issued or verified. Restore ticket_keys.json from your backup '
                                     '(typing a ticket number still works). If it is lost for good, an administrator can '
                                     'run "python cta_recover.py rotate-key": old printed QR codes will then need reprinting.')
            _create_key_file(folder, now)
            keys = read_key_file(folder)
        signer = TicketSigner(keys or [])
    if store is not None and not store.read_only:
        kid = store.meta_get('signing_kid')
        if store.meta_get('signing_started') and kid not in (None, '') and kid not in signer.kids:
            raise KeyUnavailable('The ticket signing key in use is not the one that signed recent tickets. Restore the '
                                 'matching ticket_keys.json (or CTA_TICKET_KEY) from your backup.')
        store.meta_set('signing_started', '1')
        store.meta_set('signing_kid', signer.current_kid)
    return signer


def load_signer_readonly(folder: Path) -> Optional[TicketSigner]:
    """Keys for verifying and printing only: never creates a key file. None if no key is available."""
    signer = signer_from_env()
    if signer is not None:
        return signer
    keys = read_key_file(folder)
    return TicketSigner(keys) if keys else None


# ------------------------------------------------------------------ QR payload
def qr_payload(record: dict, passenger: dict, leg: str = 'out', signer: Optional[TicketSigner] = None) -> str:
    """The text inside the QR for one ticket leg. Raises KeyUnavailable if no signing key can be used.
    Pre-2.1 placeholder tickets (no ticket id) keep their old plain booking reference; they cannot be checked in."""
    if passenger.get('legacy'):
        return record.get('reference', 'CTA')
    ticket_id = passenger.get('return_ticket_id' if leg == 'return' else 'ticket_id')
    if not ticket_id:
        raise KeyUnavailable('This ticket has no ticket id yet. Open the database once to assign it.')
    if signer is None:
        signer = default_signer()
    return signer.sign(ticket_id)


def _plain(text: str, limit: int) -> str:
    """Printable text for the readable part of a QR: no separators or control characters, shortened."""
    import unicodedata
    plain = ''.join(ch for ch in unicodedata.normalize('NFKD', str(text or ''))
                    if unicodedata.category(ch) != 'Mn')        # José -> Jose: accents make some scanners give up
    cleaned = ' '.join(''.join(ch if ch.isprintable() and ch not in '|/()' else ' ' for ch in plain).split())
    cleaned = re.sub(TOKEN_PREFIX + r'\s*:', TOKEN_PREFIX + ' ', cleaned, flags=re.IGNORECASE)   # never looks like a token
    return cleaned if len(cleaned) <= limit else cleaned[:limit - 1].rstrip() + '.'


def qr_text(record: dict, passenger: dict, leg: str = 'out', signer: Optional[TicketSigner] = None) -> str:
    """What the printed / on-screen QR holds: readable ticket details followed by the signed token (see top of file).
    Raises KeyUnavailable like ``qr_payload``. Old placeholder tickets keep their old plain reference."""
    token = qr_payload(record, passenger, leg, signer)
    if passenger.get('legacy'):
        return token
    back = leg == 'return'
    a, b = ((record.get('to_station', ''), record.get('from_station', '')) if back
            else (record.get('from_station', ''), record.get('to_station', '')))
    try:
        dep = datetime.strptime(record['depart_at'], '%Y-%m-%d %H:%M')
        when = f'VALID {dep:%d %b %Y}' if back else f'{dep:%d %b %Y %H:%M}'
    except (KeyError, ValueError, TypeError):
        when = ''
    ticket_no = passenger.get('return_ticket_no', passenger.get('ticket_no', '')) if back else passenger.get('ticket_no', '')
    who = _plain(passenger.get('name', ''), 22)
    cat = _plain(passenger.get('category', ''), 8)
    # UPPER CASE with ' / ' between parts: QR codes store A-Z, 0-9, space and $%*+-./: about 40 % more densely,
    # so the printed code has fewer, bigger squares and scans more easily.
    parts = ['CTA METRO' + (' RETURN' if back else ''), f'{who} {cat}' if cat else who,
             f'{_plain(a, 18)} TO {_plain(b, 18)}', when, _plain(ticket_no, 28)]
    return ' / '.join(p for p in parts if p).upper() + ' / ' + token


_DEFAULT: Dict[str, Optional[TicketSigner]] = {'signer': None}


def set_default_signer(signer: Optional[TicketSigner]) -> None:
    """The app registers its signer so PDF export (which has no database handle) signs with the same key."""
    _DEFAULT['signer'] = signer


def default_signer() -> TicketSigner:
    if _DEFAULT['signer'] is not None:
        return _DEFAULT['signer']
    signer = signer_from_env()
    if signer is None:
        raise KeyUnavailable('No ticket signing key is loaded.')
    return signer


# ------------------------------------------------------------------ what was presented at the gate
@dataclass
class Presented:
    """Result of reading what the operator typed or the scanner sent. ``code`` is empty when it is usable."""
    method: str                       # 'qr' | 'manual'
    log_ref: str                      # safe, short text for the scan log (never a full token, never a signature)
    ticket_id: str = ''
    ticket_no: str = ''
    code: str = ''
    message: str = ''


def _printable(text: str, limit: int) -> str:
    cleaned = ''.join(ch for ch in text if ch.isalnum() or ch in ':-._')[:limit]
    return cleaned or '(unreadable)'


def parse_presented(text: str, signer: Optional[TicketSigner]) -> Presented:
    """Classify the input and, for a QR token, check its signature. No database access."""
    raw = text if isinstance(text, str) else ''
    if len(raw) <= MAX_PRESENTED and (' / ' in raw or ' | ' in raw):   # readable QR: only the signed token is used
        for part in re.split(r'[|/]', raw):
            if part.strip().upper().startswith(TOKEN_PREFIX + ':'):
                raw = part.strip()
                break
    if len(raw) > MAX_PRESENTED:                          # far longer than any ticket number or QR token
        return Presented('manual', '(too long)', code=MALFORMED, message='REJECTED: that is not a ticket number or CTA QR code.')
    squashed = ''.join(raw.split()).upper()
    if any(not ch.isprintable() for ch in squashed):     # control characters: never a ticket number or token
        return Presented('manual', '(unreadable)', code=MALFORMED,
                         message='REJECTED: that is not a ticket number or CTA QR code.')
    if not squashed:
        return Presented('manual', '(empty)', code=EMPTY, message='Enter or scan a ticket number first.')
    if raw.strip().upper().startswith(_LEGACY_QR):
        return Presented('qr', '(old-style QR)', code=LEGACY_QR,
                         message='LEGACY QR: this code was printed before signed QR codes. It cannot be trusted as a '
                                 'code. Type the ticket number printed under it instead.')
    if squashed.startswith(TOKEN_PREFIX + ':'):
        parts = squashed.split(':')
        shown = ':'.join(parts[:3])[:40]                  # prefix, key id and ticket id only: never the signature
        m = _TOKEN.match(squashed)
        if not m:
            return Presented('qr', _printable(shown, 40), code=MALFORMED,
                             message='REJECTED: this is not a valid CTA QR code (damaged or altered).')
        if signer is None:
            return Presented('qr', _printable(shown, 40), code=KEY_UNAVAILABLE,
                             message='QR codes cannot be verified right now (signing key unavailable). '
                                     'Type the ticket number instead.')
        ticket_id = signer.verify(squashed)
        if ticket_id is None:
            return Presented('qr', _printable(shown, 40), code=FORGED,
                             message='FORGED OR ALTERED: this QR code was not issued by CTA (or has been changed). '
                                     'Do not admit.')
        return Presented('qr', f'{TOKEN_PREFIX}:{m.group(1)}:{ticket_id}', ticket_id=ticket_id)
    return Presented('manual', _printable(squashed, 40), ticket_no=squashed)


# ------------------------------------------------------------------ the one decision
@dataclass
class CheckResult:
    ok: bool
    code: str
    message: str
    reference: str = ''
    ticket_no: str = ''
    ticket_id: str = ''
    leg: str = ''
    method: str = 'manual'
    record: Optional[dict] = field(default=None, repr=False)


def leg_fields(p: dict, leg: str) -> Tuple[str, str, int, int]:
    """(ticket number, status, uses, max_uses) of one leg, from the stored record only."""
    pre = 'return_' if leg == 'return' else ''
    status = p.get(pre + 'status', 'issued')
    max_uses = p.get(pre + 'max_uses', 1)
    uses = p.get(pre + 'uses', 1 if status == 'boarded' else 0)
    tno = p.get('return_ticket_no') if leg == 'return' else p.get('ticket_no')
    return tno or '', status, uses, max_uses


def check_ticket(rec: dict, p: dict, leg: str, now: datetime, *, operator: str, environment: Optional[str] = None,
                 station: str = '', consume: bool = True, method: str = 'manual') -> CheckResult:
    """Decide whether this ticket leg may enter now, using only the stored record. On success (and ``consume``) the
    use is recorded in ``rec`` in place. Order: operator, legacy, cancelled/refunded, payment, travel window,
    already used, station. QR scans and typed numbers both come here."""
    tno, status, uses, max_uses = leg_fields(p, leg)
    pre = 'return_' if leg == 'return' else ''
    base = dict(reference=rec.get('reference', ''), ticket_no=tno, ticket_id=p.get(pre + 'ticket_id', ''), leg=leg,
                method=method, record=rec)
    who = f"{p.get('name', '?')} ({tno})"

    def no(code: str, message: str) -> CheckResult:
        return CheckResult(False, code, message, **base)

    if not (operator or '').strip():
        return no(NOT_AUTHORIZED, 'Only a signed-in staff member can check tickets in.')
    if p.get('legacy'):
        return no(LEGACY_TICKET, 'Tickets from version 2.0 have no per-passenger status and cannot be checked in.')
    if status == 'cancelled':
        return no(CANCELLED, f"{who}: CANCELLED{' ' + p[pre + 'cancelled_at'][:16] if p.get(pre + 'cancelled_at') else ''}. "
                             'Not valid for travel.')
    if status == 'refunded':
        return no(REFUNDED, f'{who}: REFUNDED. Not valid for travel.')
    if status not in ('issued', 'boarded'):
        return no(BAD_RECORD, f'{who}: the ticket record is damaged (unknown status). Do not admit; tell an administrator.')

    pay = core.payment_of(rec)
    if pay.get('status') == payments.PAID and not payments.is_settled(pay, environment):
        return no(TEST_PAYMENT, f'{who}: TEST PAYMENT ONLY - no money was taken. Not valid for travel in production.')
    if pay.get('status') in payments.UNCONFIRMED:
        return no(UNCONFIRMED, f'{who}: ONLINE PAYMENT NOT CONFIRMED. Not valid for travel until the provider confirms it.')
    if pay.get('status') in payments.REJECTED:
        return no(UNPAID, f"{who}: PAYMENT {'CANCELLED' if pay.get('status') == payments.CANCELLED else 'FAILED'}. "
                          'Not valid for travel.')
    if pay.get('status') != payments.PAID:
        return no(UNPAID, f"{who}: NOT PAID. Collect {pay.get('amount_cents', 0):,} cents at the counter first.")

    start, until = core.ticket_valid_from(rec), core.ticket_valid_until(rec)
    if start is None or until is None:
        return no(BAD_RECORD, f'{who}: the ticket has no valid travel date. Do not admit; tell an administrator.')
    if now < start:
        return no(NOT_YET_VALID, f'{who}: NOT VALID YET. Valid from {start:%d %b %H:%M}.')
    if now > until:
        return no(EXPIRED, f'{who}: this ticket expired on {until:%d %b %H:%M}.')
    if status == 'boarded' or uses >= max_uses:
        at = p.get(pre + 'boarded_at', '?')
        return no(USED, f'{who}: ALREADY USED at {at}.')
    if station:
        origin = rec.get('to_station' if leg == 'return' else 'from_station', '')
        if station.strip().lower() != str(origin).strip().lower():
            return no(WRONG_STATION, f'{who}: wrong station. This ticket starts at {origin}.')

    if consume:
        uses += 1
        if max_uses > 1:
            p[pre + 'uses'] = uses
        if uses >= max_uses:
            p[pre + 'status'] = 'boarded'
        p[pre + 'boarded_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
        p[pre + 'boarded_by'] = operator
    extra = f' - ride {uses} of {max_uses}' if max_uses > 1 else ''
    return CheckResult(True, OK, f"{who}: OK to travel ({'return' if leg == 'return' else 'outbound'}){extra}.", **base)


def locate(history: List[dict], presented: Presented) -> Optional[Tuple[dict, dict, str]]:
    """(booking, passenger, leg) from the stored records for an authentic ticket id or a typed number."""
    if presented.ticket_id:
        return core.find_ticket_by_id(history, presented.ticket_id)
    return core.find_ticket(history, presented.ticket_no)


def unknown_result(presented: Presented) -> CheckResult:
    if presented.method == 'qr':
        return CheckResult(False, UNKNOWN, 'UNKNOWN TICKET: the code is genuine but no such ticket exists in the records. '
                                           'Do not admit; tell an administrator.', ticket_id=presented.ticket_id,
                           method='qr')
    return CheckResult(False, UNKNOWN, 'No ticket with that number.', method='manual')


def refusal(presented: Presented) -> CheckResult:
    return CheckResult(False, presented.code, presented.message, ticket_id=presented.ticket_id, method=presented.method)


def check_in_memory(history: List[dict], text: str, now: datetime, operator: str, *,
                    signer: Optional[TicketSigner] = None, environment: Optional[str] = None,
                    station: str = '') -> CheckResult:
    """Same decision as the database check-in, on a list of bookings held in memory (tests and tools)."""
    presented = parse_presented(text, signer)
    if presented.code:
        return refusal(presented)
    found = locate(history, presented)
    if not found:
        return unknown_result(presented)
    rec, p, leg = found
    return check_ticket(rec, p, leg, now, operator=operator, environment=environment, station=station,
                        method=presented.method)


# ------------------------------------------------------------------ cancellations, refunds, re-entry, corrections
def _clean_reason(reason: str) -> str:
    text = ' '.join(str(reason or '').split())[:MAX_REASON]
    return ''.join(ch for ch in text if ch.isprintable())


def _legs_of(rec: dict) -> List[Tuple[dict, str]]:
    out = []
    for p in core.passengers_of(rec):
        if p.get('legacy'):
            continue
        out.append((p, 'out'))
        if rec.get('legs', 1) == 2:
            out.append((p, 'return'))
    return out


def unused_journeys(rec: dict) -> Tuple[int, int]:
    """(how many journeys can still be cancelled, how many cents would be refunded for them). Only a PAID booking
    refunds money; an unpaid counter booking just stops owing it. Used or cancelled journeys never count."""
    n = cents = 0
    paid = core.payment_of(rec).get('status') == payments.PAID
    for p, leg in _legs_of(rec):
        _no, status, uses, _max = leg_fields(p, leg)
        if status == 'issued' and uses == 0:
            n += 1
            cents += _leg_fare(p) if paid else 0
    return n, cents


def _leg_fare(p: dict) -> int:
    return int(p.get('leg_fare_cents', p.get('fare_cents', 0)))


def _cancel_leg(rec: dict, p: dict, leg: str, staff: str, now: datetime, reason: str) -> Tuple[bool, str]:
    pre = 'return_' if leg == 'return' else ''
    tno, status, uses, _max = leg_fields(p, leg)
    if status == 'cancelled' or status == 'refunded':
        return False, f'{tno} is already cancelled.'
    if status == 'boarded' or uses > 0:
        return False, f'{tno} has already been used and cannot be cancelled.'
    pay = core.payment_of(rec)
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    fare = _leg_fare(p)
    if pay.get('status') in payments.UNCONFIRMED:
        return False, 'The online payment is not confirmed yet. Wait for the provider to confirm or fail it first.'
    if pay.get('status') == payments.PAID:
        p[pre + 'refund_cents'] = fare
        p[pre + 'refund_state'] = 'due'
    elif pay.get('status') == payments.PENDING_COUNTER:
        new_pay = dict(pay)                           # nothing was collected: just reduce what is still due
        new_pay.setdefault('original_amount_cents', pay.get('amount_cents', 0))
        new_pay['amount_cents'] = max(0, pay.get('amount_cents', 0) - fare)
        rec['payment'] = new_pay
    p[pre + 'status'] = 'cancelled'
    p[pre + 'cancelled_at'] = stamp
    p[pre + 'cancelled_by'] = staff
    if reason:
        p[pre + 'cancel_reason'] = reason
    return True, tno


def _finish_cancel(rec: dict, now: datetime) -> None:
    """When nothing is left to travel on an unpaid counter booking, the payment itself is cancelled."""
    pay = core.payment_of(rec)
    if pay.get('status') == payments.PENDING_COUNTER and pay.get('amount_cents', 0) == 0:
        updated = dict(pay)
        updated['status'] = payments.CANCELLED
        updated['message'] = 'Booking cancelled'
        updated['events'] = list(pay.get('events', [])) + [{'at': now.strftime('%Y-%m-%d %H:%M:%S'),
                                                             'status': payments.CANCELLED, 'source': 'cancellation'}]
        rec['payment'] = updated


def cancel_ticket(rec: dict, ticket_no: str, staff: str, now: datetime, reason: str = '') -> Tuple[bool, str]:
    """Cancel one unused journey. Paid: a refund becomes due. Unpaid counter booking: the amount due drops."""
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can cancel a ticket.'
    found = core.find_ticket(rec_list(rec), ticket_no)
    if not found:
        return False, 'No ticket with that number in this booking.'
    _rec, p, leg = found
    ok, msg = _cancel_leg(rec, p, leg, staff, now, _clean_reason(reason))
    if not ok:
        return False, msg
    _finish_cancel(rec, now)
    due = p.get(('return_' if leg == 'return' else '') + 'refund_cents')
    return True, f'{msg} cancelled.' + (f' Refund due: {due:,} cents.' if due else '')


def rec_list(rec: dict) -> List[dict]:
    return [rec]


def cancel_booking(rec: dict, staff: str, now: datetime, reason: str = '') -> Tuple[bool, str]:
    """Cancel every unused journey of the booking (used journeys stay used and are not refunded)."""
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can cancel a booking.'
    pay = core.payment_of(rec)
    if pay.get('status') in payments.UNCONFIRMED:
        return False, 'The online payment is not confirmed yet. Wait for the provider to confirm or fail it first.'
    clean = _clean_reason(reason)
    done, refund = 0, 0
    for p, leg in _legs_of(rec):
        if leg_fields(p, leg)[1] != 'issued' or leg_fields(p, leg)[2] > 0:
            continue
        ok, _ = _cancel_leg(rec, p, leg, staff, now, clean)
        if ok:
            done += 1
            refund += p.get(('return_' if leg == 'return' else '') + 'refund_cents', 0) or 0
    if not done:
        return False, 'Nothing in this booking can be cancelled (every journey is used or already cancelled).'
    _finish_cancel(rec, now)
    return True, f'{done} journey(s) cancelled.' + (f' Refund due: {refund:,} cents.' if refund else '')


def record_refund(rec: dict, ticket_no: str, staff: str, now: datetime, ref: str = '') -> Tuple[bool, str]:
    """Staff confirm the money was handed back. Cash needs no reference; money returned through an online provider
    needs the provider's refund reference (this app does not call a provider refund API)."""
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can record a refund.'
    found = core.find_ticket([rec], ticket_no)
    if not found:
        return False, 'No ticket with that number in this booking.'
    _r, p, leg = found
    pre = 'return_' if leg == 'return' else ''
    tno, status, _u, _m = leg_fields(p, leg)
    if status == 'refunded':
        return False, f'{tno} was already refunded.'
    if status != 'cancelled' or p.get(pre + 'refund_state') != 'due':
        return False, f'{tno} has no refund due.'
    pay = core.payment_of(rec)
    ref = _clean_reason(ref)[:60]
    online_live = payments.METHOD_KIND.get(str(pay.get('method'))) == 'online' and not payments.is_simulated(pay)
    if online_live and not ref:
        return False, 'Enter the payment provider\'s refund reference (refund the money in the provider\'s dashboard first).'
    amount = int(p.get(pre + 'refund_cents', 0))
    p[pre + 'status'] = 'refunded'
    p[pre + 'refund_state'] = 'done'
    p[pre + 'refunded_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
    p[pre + 'refunded_by'] = staff
    if ref:
        p[pre + 'refund_ref'] = ref
    new_pay = dict(pay)
    new_pay['refunded_cents'] = int(pay.get('refunded_cents', 0)) + amount
    new_pay['events'] = list(pay.get('events', [])) + [{'at': p[pre + 'refunded_at'], 'status': 'refund', 'source': 'counter',
                                                         'by': staff}]
    rec['payment'] = new_pay
    return True, f'Refund of {amount:,} cents recorded for {tno}.'


def allow_reentry(rec: dict, ticket_no: str, staff: str, now: datetime, reason: str) -> Tuple[bool, str]:
    """Give back one use of a ticket that was scanned (gate fault, left the station by mistake...). Needs a reason,
    never works on cancelled/refunded tickets, and only inside the travel window."""
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can allow re-entry.'
    clean = _clean_reason(reason)
    if not clean:
        return False, 'Give a short reason for allowing re-entry.'
    found = core.find_ticket([rec], ticket_no)
    if not found:
        return False, 'No ticket with that number in this booking.'
    _r, p, leg = found
    pre = 'return_' if leg == 'return' else ''
    tno, status, uses, max_uses = leg_fields(p, leg)
    if status in ('cancelled', 'refunded'):
        return False, f'{tno} is {status}; re-entry is not possible.'
    if uses <= 0:
        return False, f'{tno} has not been used, so there is nothing to undo.'
    start, until = core.ticket_valid_from(rec), core.ticket_valid_until(rec)
    if until is None or now > until or (start is not None and now < start):
        return False, f'{tno} is outside its travel period, so re-entry is not possible.'
    uses -= 1
    p[pre + 'status'] = 'issued'
    if max_uses > 1:
        p[pre + 'uses'] = uses
    for key in ('boarded_at', 'boarded_by'):
        p.pop(pre + key, None)
    log = list(p.get(pre + 'reentries', []))
    log.append({'at': now.strftime('%Y-%m-%d %H:%M:%S'), 'by': staff, 'reason': clean})
    p[pre + 'reentries'] = log
    return True, f'{tno}: re-entry allowed. The ticket can be scanned once more.'


def correct_name(rec: dict, passenger_no: int, new_name: str, staff: str, now: datetime) -> Tuple[bool, str]:
    """Fix a misspelt name while no journey of that passenger is used or cancelled. The ticket ids and QR codes do not
    change (they carry no name); reprint the PDF so the printed name is right."""
    from cta_accounts import validate_person_name
    if not (staff or '').strip():
        return False, 'Only a signed-in staff member can correct a name.'
    target = next((p for p in rec.get('passengers', []) if p.get('no') == passenger_no), None)
    if target is None:
        return False, 'No such passenger in this booking.'
    for leg in ('out', 'return') if rec.get('legs', 1) == 2 else ('out',):
        if leg_fields(target, leg)[1] != 'issued' or leg_fields(target, leg)[2] > 0:
            return False, 'Names can only be corrected while every journey of the passenger is unused and not cancelled.'
    name, err = validate_person_name(new_name, f'passenger {passenger_no}')
    if err:
        return False, err
    if name == target['name']:
        return False, 'That is already the name on the ticket.'
    log = list(target.get('corrections', []))
    log.append({'at': now.strftime('%Y-%m-%d %H:%M:%S'), 'by': staff, 'was': target['name']})
    target['name'] = name
    target['corrections'] = log
    return True, f'Name corrected for passenger {passenger_no}. Reprint the PDF.'
