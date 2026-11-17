"""Test-mode payment entry: input checks plus a pretend bank. NOTHING here is real.

Used only while the app runs with the simulated (test) payment gateway, i.e. never when a live provider is
configured. The passenger types made-up payment details; this module

1. checks that they LOOK right (a card number of the right length, a CVV of exactly 3 digits, an expiry in the
   future, a real-looking mobile number or e-mail address). It does not check them against any bank, and it does
   not apply the Luhn check, so any made-up number of the right shape is fine;
2. asks a pretend bank whether there is enough money, using a rule the tester can control (below).

Test balance rule (shown on the payment form so nobody has to guess):

* card / mada            balance = last 4 digits of the card number x 100 cents   (ending 0000 = nothing at all)
* STC Pay / Tabby / Tamara   balance = last 4 digits of the mobile number x 100 cents
* PayPal                 plenty of money, unless the e-mail starts with "empty" (then none)

What is kept: only a short description such as "Visa ending 4242". The full number, expiry and CVV are never
saved, logged, written to the audit trail or put in a ticket; they live in the form until it closes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import cta_accounts as acct

CARD_METHODS = ('card', 'mada')
PHONE_METHODS = ('stcpay', 'tabby', 'tamara')
EMAIL_METHODS = ('paypal',)
ONLINE_METHODS = CARD_METHODS + PHONE_METHODS + EMAIL_METHODS

APPROVED, INSUFFICIENT, INVALID = 'APPROVED', 'INSUFFICIENT_FUNDS', 'INVALID'
PAYPAL_PLENTY_CENTS = 100_000_000
_EMAIL = re.compile(r'^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$')
_SEPARATORS = re.compile(r'[\s\-.]')


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hint: str = ''
    secret: bool = False       # show dots while typing
    width: int = 24


@dataclass(frozen=True)
class TestAuth:
    """Outcome of the pretend bank. ``detail`` is the only thing that may be stored with the booking."""
    approved: bool
    code: str
    message: str
    detail: str = ''


def fields_for(method: str) -> List[Field]:
    if method in CARD_METHODS:
        return [Field('number', 'Card number', 'Any made-up number, 16 digits' if method == 'mada'
                      else 'Any made-up number, 13 to 19 digits', width=26),
                Field('name', 'Name on card', 'Letters only, as on the card'),
                Field('expiry', 'Expiry (MM/YY)', 'A month in the future, for example 12/29', width=10),
                Field('cvv', 'CVV', 'Exactly 3 digits (4 for American Express)', secret=True, width=6)]
    if method in PHONE_METHODS:
        return [Field('phone', 'Mobile number', 'Saudi mobile, for example 05XXXXXXXX')]
    if method in EMAIL_METHODS:
        return [Field('email', 'PayPal e-mail', 'Any e-mail address, for example name@example.com', width=30)]
    raise ValueError(f'No test form for {method}.')


def balance_rule(method: str) -> str:
    """One sentence for the form, so the tester knows how to get an approval or a refusal."""
    if method in CARD_METHODS:
        return ('Test bank: the balance is the last 4 digits of the card number x 100 cents. '
                'A card ending 0000 has no money, 5000 has 500,000 cents.')
    if method in PHONE_METHODS:
        return 'Test wallet: the balance is the last 4 digits of the mobile number x 100 cents.'
    return 'Test PayPal: always has enough money, unless the e-mail starts with "empty".'


def _digits(text: str) -> str:
    return _SEPARATORS.sub('', acct.normalize_digits(text or ''))


def card_brand(number: str) -> str:
    d = _digits(number)
    if d.startswith('4'):
        return 'Visa'
    if re.match(r'^(5[1-5]|2[2-7])', d):
        return 'Mastercard'
    if d.startswith(('34', '37')):
        return 'American Express'
    return 'Card'


def _only_digits(raw: str) -> str:
    return re.sub(r'\D', '', acct.normalize_digits(raw or ''))


def format_card_number(raw: str) -> str:
    """Groups of four for typing comfort (4-6-5 for American Express). Digits only, at most 19."""
    d = _only_digits(raw)[:19]
    if d.startswith(('34', '37')) and len(d) <= 15:
        return ' '.join(p for p in (d[:4], d[4:10], d[10:15]) if p)
    return ' '.join(d[i:i + 4] for i in range(0, len(d), 4))


def format_expiry(raw: str) -> str:
    d = _only_digits(raw)[:4]
    return d if len(d) <= 2 else f'{d[:2]}/{d[2:]}'


def _check_expiry(text: str, now: datetime) -> str:
    m = re.fullmatch(r'(\d{1,2})\s*/\s*(\d{2}|\d{4})', acct.normalize_digits(text or '').strip())
    if not m:
        return 'Enter the expiry as MM/YY, for example 12/29.'
    month, year = int(m.group(1)), int(m.group(2))
    year += 2000 if year < 100 else 0
    if not 1 <= month <= 12:
        return 'The expiry month must be between 01 and 12.'
    if (year, month) < (now.year, now.month):
        return 'This card has expired. Use a date this month or later.'
    if year > now.year + 20:
        return 'That expiry date is too far in the future.'
    return ''


def validate(method: str, values: Dict[str, str], now: datetime) -> Dict[str, str]:
    """{field key: problem} for everything wrong; an empty dict means the details look right."""
    errors: Dict[str, str] = {}
    if method in CARD_METHODS:
        number = _digits(values.get('number', ''))
        if not number:
            errors['number'] = 'Enter a card number.'
        elif not number.isdigit():
            errors['number'] = 'The card number can only contain digits.'
        elif method == 'mada' and len(number) != 16:
            errors['number'] = 'A mada card number has exactly 16 digits.'
        elif not 13 <= len(number) <= 19:
            errors['number'] = 'A card number has 13 to 19 digits.'
        name, why = acct.validate_person_name(values.get('name', ''), 'the card holder')
        if why:
            errors['name'] = why
        why = _check_expiry(values.get('expiry', ''), now)
        if why:
            errors['expiry'] = why
        cvv = _digits(values.get('cvv', ''))
        need = 4 if card_brand(number) == 'American Express' else 3
        if not cvv:
            errors['cvv'] = f'Enter the {need}-digit CVV.'
        elif not cvv.isdigit() or len(cvv) != need:
            errors['cvv'] = f'The CVV must be exactly {need} digits.'
    elif method in PHONE_METHODS:
        phone, why = acct.validate_phone(values.get('phone', ''))
        if why:
            errors['phone'] = why
    elif method in EMAIL_METHODS:
        email = (values.get('email', '') or '').strip()
        if not _EMAIL.match(email) or len(email) > 80:
            errors['email'] = 'Enter a valid e-mail address, for example name@example.com.'
    else:
        errors['method'] = 'This payment method has no test form.'
    return errors


def _balance_cents(method: str, values: Dict[str, str]) -> int:
    if method in CARD_METHODS:
        return int(_digits(values['number'])[-4:]) * 100
    if method in PHONE_METHODS:
        phone, _why = acct.validate_phone(values['phone'])
        return int((phone or '0000')[-4:]) * 100
    email = (values.get('email', '') or '').strip().lower()
    return 0 if email.startswith('empty') else PAYPAL_PLENTY_CENTS


def _detail(method: str, values: Dict[str, str]) -> str:
    if method in CARD_METHODS:
        return f"{card_brand(values['number'])} ending {_digits(values['number'])[-4:]}"
    if method in PHONE_METHODS:
        phone, _why = acct.validate_phone(values['phone'])
        return f'mobile ending {(phone or "")[-4:]}'
    return 'PayPal account'


def authorize(method: str, values: Dict[str, str], amount_cents: int, now: datetime) -> TestAuth:
    """Check the details, then ask the pretend bank. Never raises for bad user input."""
    errors = validate(method, values, now)
    if errors:
        return TestAuth(False, INVALID, ' '.join(errors.values()))
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        return TestAuth(False, INVALID, 'There is nothing to pay.')
    detail = _detail(method, values)
    balance = _balance_cents(method, values)
    if balance < amount_cents:
        return TestAuth(False, INSUFFICIENT,
                        f'Payment declined: not enough money. Available {balance:,} cents, '
                        f'this booking needs {amount_cents:,} cents. Try another number or method.', detail)
    return TestAuth(True, APPROVED, 'Payment accepted', detail)
