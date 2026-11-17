"""CTA accounts: phone sign-up, name rules, simulated one-time code, account storage.

No GUI code lives here, so everything is unit-tested without a display.

Honest limits (also stated in the README):
* There is no SMS service. The 6-digit code is shown ON SCREEN and labelled as a demo.
  Replace ``OtpSession.demo_code`` delivery with a real SMS gateway to go live.
* Accounts are stored in the shared SQLite database (``cta.db``); ``accounts.json`` is only read for migration
  and by the stand-alone functions here.
"""
from __future__ import annotations

import hmac
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

# ------------------------------------------------------------------ phone numbers
PHONE_MIN_DIGITS = 8     # shortest international number we accept (country code included)
PHONE_MAX_DIGITS = 15    # E.164 maximum
# ------------------------------------------------------------------ typed digits and constant-time comparison
def normalize_digits(text: str) -> str:
    """Turn digits from any alphabet into ASCII 0-9 and full-width punctuation into ASCII.

    A passenger whose keyboard produces Arabic-Indic (٠١٢), Eastern Arabic-Indic (۰۱۲), Devanagari (०१२) or
    full-width (０１２) digits can type their phone number and verification code normally. ``unicodedata.decimal``
    knows every such digit, so no list has to be kept up to date here (and it deliberately does NOT match
    superscripts or other digit-like characters, so ``x²`` is never turned into ``x2``). Characters that are not digits are
    returned unchanged (apart from full-width + ( ) . - and the Arabic thousands separator), so this never
    repairs a value into something it was not.
    """
    if not isinstance(text, str):
        return ''
    out = []
    for ch in text:
        value = unicodedata.decimal(ch, None) if not ch.isascii() else None
        out.append(str(value) if value is not None else _WIDE_PUNCT.get(ch, ch))
    return ''.join(out)


# Full-width and typographic punctuation a phone keypad may produce, plus the invisible marks some Arabic
# keyboards insert around numbers. Written as escape sequences on purpose: a literal right-to-left mark in
# source code is a Trojan-Source hazard, because it can change how the line reads to a human reviewer.
_WIDE_PUNCT = {
    '\uff0b': '+', '\uff08': '(', '\uff09': ')', '\uff0e': '.', '\uff0d': '-',
    '\u2015': '-', '\u2013': '-', '\u2014': '-',
    '\u00a0': ' ',      # no-break space
    '\u066c': ' ',      # Arabic thousands separator
    '\u200f': '',       # RIGHT-TO-LEFT MARK
    '\u200e': '',       # LEFT-TO-RIGHT MARK
}


def secure_equal(a: str, b: str) -> bool:
    """Constant-time text comparison that cannot raise.

    ``hmac.compare_digest`` refuses ``str`` values holding non-ASCII characters (TypeError), so comparing anything
    a person typed with it directly crashes the handler. Encoding both sides to UTF-8 first keeps the constant-time
    guarantee and accepts any text.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode('utf-8', 'surrogatepass'), b.encode('utf-8', 'surrogatepass'))



PHONE_HINT = 'Include the country code, for example +966 5X XXX XXXX or 05XXXXXXXX'

_PHONE_CHARS = re.compile(r'^[\d\s().\-+]+$')
# Saudi local format: 05XXXXXXXX (10 digits starting with 05)
_SAUDI_LOCAL = re.compile(r'^0(5[0-9]{8})$')


def normalise_saudi(raw: str) -> Optional[str]:
    """Convert Saudi local format 05XXXXXXXX -> +9665XXXXXXXX.  Returns None if not Saudi local."""
    digits_only = re.sub(r'\D', '', (raw or '').strip())
    m = _SAUDI_LOCAL.match(digits_only)
    if m:
        return '+966' + m.group(1)
    return None


def validate_phone(raw: str) -> Tuple[Optional[str], str]:
    """Return (normalised '+digits' number, '') or (None, error message).

    Saudi local numbers (05XXXXXXXX) are automatically converted to E.164 (+9665XXXXXXXX).
    International numbers must start with '+' or '00'.
    Stored in one canonical form so '+966 50 123 4567' and '0501234567' are the same account.
    """
    if raw is not None and not isinstance(raw, str):
        return None, 'Enter the mobile number as text, for example +966 5X XXX XXXX or 05XXXXXXXX.'
    s = normalize_digits(raw or '').strip()          # Arabic-Indic / full-width digits are accepted
    if not s:
        return None, 'Enter your mobile number.'
    # Try Saudi local normalisation first (05XXXXXXXX)
    saudi = normalise_saudi(s)
    if saudi:
        return saudi, ''
    if not _PHONE_CHARS.match(s):
        return None, 'Phone numbers can only contain digits, spaces, brackets, dots, hyphens and a leading +.'
    if '+' in s[1:]:
        return None, 'The + sign can only be at the start of the number.'
    if s.startswith('+'):
        digits = re.sub(r'\D', '', s)
    elif s.startswith('00'):
        digits = re.sub(r'\D', '', s)[2:]
    else:
        return None, f'Start with your country code (+ or 00), or enter a Saudi local number (05XXXXXXXX).'
    if digits.startswith('0'):
        return None, 'A country code never starts with 0. ' + PHONE_HINT + '.'
    if not (PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS):
        return None, f'That number has {len(digits)} digits. Use {PHONE_MIN_DIGITS} to {PHONE_MAX_DIGITS} digits.'
    return '+' + digits, ''


def mask_phone(phone: str, mask_char: str = '•') -> str:
    """'+447123456789' -> '+••••••••6789' (last four digits only)."""
    digits = phone.lstrip('+')
    if len(digits) <= 4:
        return phone
    return '+' + mask_char * (len(digits) - 4) + digits[-4:]


# ------------------------------------------------------------------ person names
NAME_MIN, NAME_MAX = 2, 60
_NAME_PUNCT = set(" -'’.")


def clean_name(raw: str) -> str:
    return ' '.join((raw or '').split())


def validate_person_name(raw: str, who: str = 'the passenger') -> Tuple[str, str]:
    """Return (cleaned name, '') or ('', error message).

    Letters from any alphabet are fine (José, Zoë, Nguyễn, Müller, Айдар, ...), plus
    spaces, hyphens, apostrophes, full stops and combining accent marks. Digits,
    symbols and control characters (which could break the PDF or QR text) are rejected.
    """
    if raw is not None and not isinstance(raw, str):
        return '', f'Enter the name of {who} as text.'
    name = clean_name(raw)
    if not name:
        return '', f'Enter {who}\u2019s name.'
    if len(name) < NAME_MIN:
        return '', 'The name is too short.'
    if len(name) > NAME_MAX:
        return '', f'The name is too long (maximum {NAME_MAX} characters).'
    for ch in name:
        if ch.isalpha() or ch in _NAME_PUNCT or unicodedata.category(ch).startswith('M'):
            continue
        return '', 'Names can only contain letters, spaces, hyphens, apostrophes and full stops.'
    if sum(1 for ch in name if ch.isalpha()) < 2:
        return '', 'The name needs at least two letters.'
    return name, ''


# ------------------------------------------------------------------ account storage
def validate_accounts(data) -> None:
    from cta_storage import DataInvalid
    if not isinstance(data, list):
        raise DataInvalid('the accounts must be a list.')
    seen = set()
    for i, a in enumerate(data, 1):
        if not isinstance(a, dict) or not isinstance(a.get('phone'), str) or not a['phone'].strip():
            raise DataInvalid(f'account #{i} has no phone number.')
        if a['phone'] in seen:
            raise DataInvalid(f'account #{i} repeats a phone number.')
        seen.add(a['phone'])
        if not isinstance(a.get('name'), str):
            raise DataInvalid(f'account #{i} has no name.')


def load_accounts(path) -> List[dict]:
    """Missing file -> [] (first run). Damaged, unreadable or invalid -> cta_storage.StorageError.
    ``path`` may also be a store object (cta_db.Store) with its own ``load_accounts``."""
    if hasattr(path, 'load_accounts'):
        return path.load_accounts()
    from cta_storage import read_json
    return read_json(Path(path), list, validate_accounts, 'accounts')


def save_accounts(path, accounts: List[dict]) -> None:
    if hasattr(path, 'save_accounts'):
        path.save_accounts(accounts)
        return
    from cta_storage import atomic_write_json
    atomic_write_json(Path(path), accounts, validate_accounts, 'accounts')


def find_account(accounts: List[dict], phone: str) -> Optional[dict]:
    for a in accounts:
        if a.get('phone') == phone:
            return a
    return None


def create_account(accounts: List[dict], phone: str, name: str, now: datetime) -> dict:
    if find_account(accounts, phone) is not None:
        raise ValueError('An account with this phone number already exists.')
    cleaned, err = validate_person_name(name, 'your')
    if err:
        raise ValueError(err)
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    account = {'phone': phone, 'name': cleaned, 'created_at': stamp, 'last_login': stamp}
    accounts.append(account)
    return account


def touch_login(account: dict, now: datetime) -> None:
    account['last_login'] = now.strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------------ one-time code (simulated SMS)
class OtpSession:
    """One 6-digit code for one phone number: expires after 5 min, 3 wrong tries, single use."""
    LENGTH = 6
    TTL = timedelta(minutes=5)
    MAX_ATTEMPTS = 3

    def __init__(self, phone: str, now: datetime):
        self.phone = phone
        self._code = ''.join(str(secrets.randbelow(10)) for _ in range(self.LENGTH))
        self.expires_at = now + self.TTL
        self.attempts_left = self.MAX_ATTEMPTS
        self.used = False

    @property
    def demo_code(self) -> str:
        """Shown on screen because this demo has no SMS gateway."""
        return self._code

    def verify(self, entered: str, now: datetime) -> Tuple[bool, str]:
        if self.used:
            return False, 'This code was already used. Ask for a new one.'
        if now > self.expires_at:
            return False, 'This code has expired. Ask for a new one.'
        if self.attempts_left <= 0:
            return False, 'Too many wrong codes. Ask for a new one.'
        typed = ''.join(normalize_digits(entered or '').split())
        if not typed:
            return False, f'Enter the {self.LENGTH}-digit code.'
        if secure_equal(typed, self._code):
            self.used = True
            return True, ''
        self.attempts_left -= 1
        if self.attempts_left <= 0:
            return False, 'Wrong code. No tries left - ask for a new code.'
        left = self.attempts_left
        return False, f'Wrong code. {left} {"try" if left == 1 else "tries"} left.'


# ------------------------------------------------------------------ staff / admin accounts
import hashlib  # noqa: E402  (kept next to the code that uses it)

PASSWORD_MIN = 8
PASSWORD_MAX = 128               # long enough for passphrases; bounds the hashing work an attacker can request
PBKDF2_ITERATIONS = 600_000      # PBKDF2-HMAC-SHA256, OWASP 2023 recommendation. Older hashes are upgraded at sign-in.
PBKDF2_MAX_ITERATIONS = 5_000_000   # a stored hash asking for more is treated as damaged, not obeyed
_USERNAME = re.compile(r'^[A-Za-z0-9._-]{3,30}$')


def validate_username(raw: str) -> Tuple[str, str]:
    name = (raw or '').strip()
    if not _USERNAME.match(name):
        return '', 'Username: 3-30 letters, digits, dots, hyphens or underscores.'
    return name, ''


def validate_password(pw: str) -> str:
    """Return '' if the password is acceptable, else the reason it is not."""
    if pw is not None and not isinstance(pw, str):
        return 'Enter the password as text.'
    if len(pw or '') < PASSWORD_MIN:
        return f'Use at least {PASSWORD_MIN} characters.'
    if len(pw) > PASSWORD_MAX:
        return f'Use at most {PASSWORD_MAX} characters.'
    if not any(ch.isalpha() for ch in pw) or not any(ch.isdigit() for ch in pw):
        return 'Use at least one letter and one digit.'
    return ''


def hash_password(password: str, salt: Optional[bytes] = None, iterations: Optional[int] = None) -> dict:
    """Salted PBKDF2-HMAC-SHA256. Only the salt, the hash and the iteration count are kept - never the password."""
    iterations = PBKDF2_ITERATIONS if iterations is None else iterations
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return {'salt': salt.hex(), 'hash': digest.hex(), 'iterations': iterations}


def check_password(password: str, stored: dict) -> bool:
    try:
        iterations = int(stored['iterations'])
        if not 1 <= iterations <= PBKDF2_MAX_ITERATIONS or len(password or '') > PASSWORD_MAX:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(stored['salt']), iterations)
        return hmac.compare_digest(digest.hex(), stored['hash'])
    except (KeyError, ValueError, TypeError):
        return False


def needs_rehash(stored: dict) -> bool:
    """True for hashes made with fewer iterations than today's setting (upgraded at the next correct sign-in)."""
    try:
        return int(stored.get('iterations', 0)) < PBKDF2_ITERATIONS
    except (TypeError, ValueError):
        return True


def validate_staff(data) -> None:
    from cta_storage import DataInvalid
    if not isinstance(data, list):
        raise DataInvalid('the staff list must be a list.')
    seen = set()
    for i, s in enumerate(data, 1):
        if not isinstance(s, dict) or not isinstance(s.get('username'), str) or not s['username'].strip():
            raise DataInvalid(f'staff account #{i} has no username.')
        if s['username'].lower() in seen:
            raise DataInvalid(f'staff account #{i} repeats a username.')
        seen.add(s['username'].lower())
        pw = s.get('password')
        if not isinstance(pw, dict) or not all(k in pw for k in ('salt', 'hash', 'iterations')):
            raise DataInvalid(f'staff account #{i} has no valid password record.')


def load_staff(path) -> List[dict]:
    if hasattr(path, 'load_staff'):
        return path.load_staff()
    from cta_storage import read_json
    return read_json(Path(path), list, validate_staff, 'staff')


def save_staff(path, staff: List[dict]) -> None:
    if hasattr(path, 'save_staff'):
        path.save_staff(staff)
        return
    from cta_storage import atomic_write_json
    atomic_write_json(Path(path), staff, validate_staff, 'staff')


def create_staff(staff: List[dict], username: str, password: str, now: datetime, role: str = 'admin') -> dict:
    name, err = validate_username(username)
    if err:
        raise ValueError(err)
    err = validate_password(password)
    if err:
        raise ValueError(err)
    if any(s['username'].lower() == name.lower() for s in staff):
        raise ValueError('That username is taken.')
    rec = {'username': name, 'role': role, 'created_at': now.strftime('%Y-%m-%d %H:%M:%S'),
           'password': hash_password(password)}
    staff.append(rec)
    return rec


def DUMMY_HASH() -> dict:
    """Same work as a real check, so a wrong username takes as long as a wrong password."""
    return hash_password('x' * 8, salt=b'\0' * 16)


def authenticate_staff(staff: List[dict], username: str, password: str) -> Optional[dict]:
    """Always does the hashing work, so timing does not reveal whether a username exists."""
    found = next((s for s in staff if s['username'].lower() == (username or '').strip().lower()), None)
    stored = found['password'] if found else DUMMY_HASH()
    ok = check_password(password or '', stored)
    return found if (found and ok) else None
