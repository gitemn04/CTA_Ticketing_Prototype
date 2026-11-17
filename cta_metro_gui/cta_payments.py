"""Payment methods, payment verification and the payment record stored on every booking.

Card numbers are NEVER typed into or stored by this app. For card, mada, PayPal, STC Pay, Tabby and Tamara a
real payment provider (PSP) must take the money on its own hosted page.

Environments
------------
The application runs in one of three environments, chosen by the ``CTA_ENV`` environment variable or by
``"environment"`` in ``cta_config.json`` (the environment variable wins):

* ``development`` / ``testing`` - ``SimulatedGateway`` may be used. It moves NO money; every record it produces
  is flagged ``simulated: true`` and is shown as "test mode - no money taken" everywhere.
* ``production`` (the DEFAULT when nothing is set, so a mis-configured install fails closed) - the simulated
  gateway is refused. If no real provider is configured and implemented, online payment is switched off and
  the app says so; cash at the counter keeps working.

Real providers
--------------
No real PSP is implemented in this build. To add one, subclass ``PaymentProvider``, implement ``charge`` (start
the payment on the provider's side) and ``verify`` (ask the provider's server for the authoritative status of a
transaction, using credentials that are read from environment variables, never from this repository), and register
the class in ``REAL_PROVIDERS``. Do not register a class until it has been built and tested against the
provider's official API documentation.

Trust rules
-----------
* A ``charge`` result that says "paid" is only a claim. The booking becomes paid only after ``verify`` returns a
  record from the provider whose status, transaction id, amount, currency and booking reference all match.
* A transaction id can pay for exactly one booking (``PaymentLedger``), so repeated callbacks or a double
  submission cannot create a second payment.
* Pending, unknown, failed and cancelled payments never produce a paid ticket.
* Cash is reserved as "pay at the counter" and only a signed-in staff member can record that it was received
  (``cta_core.mark_paid``).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

# id, title, description, kind
METHODS = (
    ('card', 'Credit / debit card', 'Visa, Mastercard', 'online'),
    ('mada', 'mada', 'Saudi debit card', 'online'),
    ('stcpay', 'STC Pay', 'Pay with your wallet', 'online'),
    ('paypal', 'PayPal', 'Pay with your account', 'online'),
    ('tabby', 'Tabby', 'Buy now, pay later', 'online'),
    ('tamara', 'Tamara', 'Buy now, pay later', 'online'),
    ('cash', 'Cash - pay at the counter', 'Reserve now, pay before you travel', 'counter'),
)
METHOD_TITLE: Dict[str, str] = {m[0]: m[1] for m in METHODS}
METHOD_KIND: Dict[str, str] = {m[0]: m[3] for m in METHODS}

# Payment states. PAID is the only state that lets a ticket board.
PAID, PENDING_COUNTER, FAILED = 'paid', 'pending_counter', 'failed'
PENDING, CANCELLED, UNKNOWN = 'pending', 'cancelled', 'unknown'
STATES = (PAID, PENDING_COUNTER, PENDING, FAILED, CANCELLED, UNKNOWN)
UNCONFIRMED = (PENDING, UNKNOWN)                 # booking kept, no valid ticket, may still be confirmed later
REJECTED = (FAILED, CANCELLED)                   # nothing is issued

DEVELOPMENT, TESTING, PRODUCTION = 'development', 'testing', 'production'
ENVIRONMENTS = (DEVELOPMENT, TESTING, PRODUCTION)
DEFAULT_CURRENCY = 'SAR'
CONFIG_PATH = Path(__file__).with_name('cta_config.json')
MAX_MESSAGE = 200


class PaymentUnavailable(Exception):
    """Online payment cannot be taken here (e.g. production without a real provider)."""


# ------------------------------------------------------------------ configuration
def _read_config(config_path: Optional[Path] = None) -> dict:
    try:
        cfg = json.loads(Path(config_path or CONFIG_PATH).read_text(encoding='utf-8'))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def current_environment(config: Optional[dict] = None, config_path: Optional[Path] = None) -> str:
    """development | testing | production. Missing or unrecognised values mean production (fail closed)."""
    value = os.environ.get('CTA_ENV')
    if not value:
        cfg = config if config is not None else _read_config(config_path)
        value = cfg.get('environment', '')
    value = str(value or '').strip().lower()
    return value if value in ENVIRONMENTS else PRODUCTION


def payment_settings(config: Optional[dict] = None, config_path: Optional[Path] = None) -> dict:
    cfg = config if config is not None else _read_config(config_path)
    raw = cfg.get('payments')
    section: dict = raw if isinstance(raw, dict) else {}
    currency = str(section.get('currency') or DEFAULT_CURRENCY).strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):          # ISO 4217 shape; never guess another currency
        raise ValueError('The payment currency in the settings must be a three-letter code such as SAR.')
    return {'provider': str(section.get('provider') or '').strip().lower(), 'currency': currency}


# ------------------------------------------------------------------ provider contract
@dataclass
class PaymentResult:
    """What a provider says when a payment is started. A PAID status here is a claim, not proof."""
    status: str
    transaction_id: str = ''
    message: str = ''


@dataclass
class VerifiedPayment:
    """The provider's own server-side record of a transaction (fetched by ``verify``)."""
    status: str
    transaction_id: str
    amount_cents: int
    currency: str
    reference: str


class PaymentProvider:
    """Base class for real providers. ``live = True`` means real money moves."""
    name = 'provider'
    live = True

    def charge(self, method: str, amount_cents: int, reference: str, phone: str,
               currency: str = DEFAULT_CURRENCY, idempotency_key: str = '') -> PaymentResult:
        raise NotImplementedError

    def verify(self, transaction_id: str) -> VerifiedPayment:
        raise NotImplementedError


class SimulatedGateway(PaymentProvider):
    """Development / testing only. Approves every online payment instantly. No money moves.
    It keeps its own in-memory ledger so the normal verification path runs against it."""
    name = 'simulated'
    live = False

    def __init__(self):
        self._tx: Dict[str, VerifiedPayment] = {}
        self._by_key: Dict[str, str] = {}

    def charge(self, method: str, amount_cents: int, reference: str, phone: str,
               currency: str = DEFAULT_CURRENCY, idempotency_key: str = '') -> PaymentResult:
        if idempotency_key and idempotency_key in self._by_key:          # same request twice -> same transaction
            return PaymentResult(PAID, self._by_key[idempotency_key], 'Payment approved (test mode)')
        tx = 'TX-' + secrets.token_hex(5).upper()
        self._tx[tx] = VerifiedPayment(PAID, tx, amount_cents, currency, reference)
        if idempotency_key:
            self._by_key[idempotency_key] = tx
        return PaymentResult(PAID, tx, 'Payment approved (test mode - no money taken)')

    def verify(self, transaction_id: str) -> VerifiedPayment:
        found = self._tx.get(transaction_id)
        if found is None:
            raise LookupError('Unknown transaction')
        return found


# Real provider classes, keyed by the name used in cta_config.json -> "payments": {"provider": "..."}.
# Deliberately empty: no real PSP integration exists in this build.
REAL_PROVIDERS: Dict[str, type] = {}


def load_payment_provider(config: Optional[dict] = None, environment: Optional[str] = None,
                          config_path: Optional[Path] = None):
    """The provider to use, or None when online payment is not available in this environment."""
    cfg = config if config is not None else _read_config(config_path)
    env = environment or current_environment(cfg)
    wanted = payment_settings(cfg)['provider']
    if wanted in REAL_PROVIDERS:
        return REAL_PROVIDERS[wanted]()
    if env == PRODUCTION:
        return None                                      # never fall back to the simulated gateway
    return SimulatedGateway()


def online_availability(provider, environment: str) -> Tuple[bool, str]:
    """(True, '') if online payments may be taken, else (False, a message for the passenger)."""
    if provider is None:
        return False, ('Live online payments are not available at the moment because no payment provider is set up. '
                       'You can still reserve and pay in cash at a CTA counter.')
    if environment == PRODUCTION and not getattr(provider, 'live', False):
        return False, ('Live online payments are not available: only a test payment gateway is installed. '
                       'You can still reserve and pay in cash at a CTA counter.')
    return True, ''


# ------------------------------------------------------------------ idempotency / persistent ledger
class Ledger(Protocol):
    """What a payment needs from a ledger: claim a transaction for one booking, and log events.
    Implemented by PaymentLedger (JSON / memory) and cta_db.StoreLedger (database)."""
    def claim(self, gateway: str, transaction_id: str, reference: str, amount_cents: int,
              currency: str, at: str) -> str: ...

    def log(self, event: dict) -> None: ...


class PaymentLedger:
    """Which provider transaction paid which booking, plus an append-only log of payment status changes.

    ``path`` None keeps everything in memory (used by unit tests and when no folder is given). The ledger
    holds only references, amounts and statuses - never card data, credentials or provider secrets."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self.claims: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """A damaged ledger raises (cta_storage.StorageError): an empty ledger would allow double payments."""
        if self.path is None:
            return
        from cta_storage import read_json
        self.claims = read_json(self.path, dict, _validate_claims, 'payment ledger')

    def _save(self) -> None:
        if self.path is None:
            return
        from cta_storage import atomic_write_json
        atomic_write_json(self.path, self.claims, _validate_claims, 'payment ledger')

    def claim(self, gateway: str, transaction_id: str, reference: str, amount_cents: int,
              currency: str, at: str) -> str:
        """'new' (first time), 'duplicate' (same booking again: do nothing) or 'conflict' (paid another booking)."""
        self._load()                                    # pick up claims made by another window since we started
        key = f'{gateway}:{transaction_id}'
        old = self.claims.get(key)
        if old is not None:
            return 'duplicate' if old.get('reference') == reference else 'conflict'
        self.claims[key] = {'reference': reference, 'amount_cents': amount_cents, 'currency': currency, 'at': at}
        self._save()                                    # if this raises, the caller must NOT mark the booking paid
        return 'new'

    def log(self, event: dict) -> None:
        if self.path is None:
            return
        try:
            with self.path.with_name('payment_events.jsonl').open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(_safe_event(event), ensure_ascii=False) + '\n')
        except OSError as exc:                          # must never block a booking, but is logged for maintenance
            import cta_errors
            cta_errors.report(exc, 'writing payment_events.jsonl', reference=event.get('reference', ''))


def _validate_claims(data) -> None:
    from cta_storage import DataInvalid
    if not isinstance(data, dict):
        raise DataInvalid('the ledger must be a JSON object.')
    for key, claim in data.items():
        if ':' not in key or not isinstance(claim, dict) or not isinstance(claim.get('reference'), str):
            raise DataInvalid('a ledger entry is malformed.')


_EVENT_FIELDS = ('at', 'reference', 'method', 'gateway', 'environment', 'status', 'transaction_id',
                 'amount_cents', 'currency', 'source', 'message', 'by')


def _safe_event(event: dict) -> dict:
    """Whitelist: only these fields are ever written to disk."""
    return {k: event[k] for k in _EVENT_FIELDS if k in event}


def _clean(text) -> str:
    return str(text or '').replace('\n', ' ').strip()[:MAX_MESSAGE]


# ------------------------------------------------------------------ verification
def verify_transaction(provider, transaction_id: str, *, amount_cents: int, currency: str,
                       reference: str) -> Tuple[str, str]:
    """Ask the provider for its own record and check it field by field. Returns (status, message)."""
    if not transaction_id:
        return UNKNOWN, 'The payment provider did not return a transaction reference.'
    verify = getattr(provider, 'verify', None)
    if verify is None:
        return UNKNOWN, 'This payment provider cannot confirm payments, so the payment was not accepted.'
    try:
        v = verify(transaction_id)
    except Exception as exc:  # noqa: BLE001 - any provider failure means "not confirmed", never "paid"
        import cta_errors
        cta_errors.report(exc, 'verifying a payment with the provider', transaction=transaction_id)
        return UNKNOWN, 'The payment could not be confirmed with the provider yet.'
    if not isinstance(v, VerifiedPayment):
        return UNKNOWN, 'The payment provider gave an unreadable confirmation.'
    if v.transaction_id != transaction_id:
        return FAILED, 'Payment rejected: the transaction reference does not match.'
    if str(v.reference) != str(reference):
        return FAILED, 'Payment rejected: the booking reference does not match.'
    if str(v.currency).upper() != str(currency).upper():
        return FAILED, 'Payment rejected: the currency does not match.'
    if not isinstance(v.amount_cents, int) or isinstance(v.amount_cents, bool) or v.amount_cents != amount_cents:
        return FAILED, 'Payment rejected: the amount paid does not match the fare.'
    if v.status in STATES and v.status != PENDING_COUNTER:
        return v.status, ''
    return UNKNOWN, 'The payment provider reported an unrecognised status.'


def _accepts_currency(charge) -> bool:
    """Whether a provider's ``charge`` takes the newer ``currency`` / ``idempotency_key`` arguments.

    This is decided from the signature BEFORE calling, never by catching TypeError afterwards: a TypeError raised
    by a bug *inside* a live provider's adapter is indistinguishable from a signature mismatch, and retrying on it
    would send a second charge for the same booking. A provider that cannot be inspected (a C extension or an
    exotic callable) is called the old way, which every provider accepts.
    """
    import inspect
    try:
        sig = inspect.signature(charge)
    except (TypeError, ValueError):
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return {'currency', 'idempotency_key'} <= set(sig.parameters)


def _charge_once(provider, method: str, amount_cents: int, reference: str, phone: str, currency: str) -> PaymentResult:
    """Start the payment with EXACTLY ONE call to the provider, whichever signature it has."""
    charge = provider.charge
    if _accepts_currency(charge):
        return charge(method, amount_cents, reference, phone, currency=currency, idempotency_key=reference)
    return charge(method, amount_cents, reference, phone)       # older providers: (method, amount, reference, phone)


def _event(record: dict, status: str, at: str, source: str, transaction_id: str = '', by: str = '') -> None:
    entry = {'at': at, 'status': status, 'source': source}
    if transaction_id:
        entry['transaction_id'] = transaction_id
    if by:
        entry['by'] = by
    record.setdefault('events', []).append(entry)


def _finish_verified(record: dict, provider, ledger: Ledger, status: str, message: str,
                     transaction_id: str, stamp: str, source: str) -> dict:
    """Apply a verified status to a payment record, enforcing one booking per transaction."""
    if status == PAID:
        try:
            outcome = ledger.claim(record['gateway'], transaction_id, record['reference'],
                                   record['amount_cents'], record['currency'], stamp)
        except OSError:
            status, message = UNKNOWN, 'The payment was confirmed but could not be recorded safely. Staff will check it.'
            outcome = 'error'
        if outcome == 'conflict':
            status, message = FAILED, 'Payment rejected: that transaction has already paid for another booking.'
        if status == PAID:
            record['paid_at'] = record.get('paid_at') or stamp
            record['verified_at'] = stamp
    record.update(status=status, transaction_id=transaction_id, message=_clean(message or record.get('message')))
    if status == FAILED and transaction_id and source != 'provider':
        record['review_required'] = True               # verification mismatch: money may have moved, staff must check
    _event(record, status, stamp, source, transaction_id)
    ledger.log({'at': stamp, 'reference': record['reference'], 'method': record['method'],
                'gateway': record['gateway'], 'environment': record.get('environment', ''), 'status': status,
                'transaction_id': transaction_id, 'amount_cents': record['amount_cents'],
                'currency': record['currency'], 'source': source, 'message': record['message']})
    return record


# ------------------------------------------------------------------ taking a payment
def pay(method: str, amount_cents: int, reference: str, phone: str, now: datetime, provider=None,
        environment: Optional[str] = None, ledger: Optional[Ledger] = None,
        currency: Optional[str] = None) -> dict:
    """Run the chosen payment method and return the payment record to store with the booking.

    Raises ValueError for bad input and PaymentUnavailable when online payment cannot be taken here."""
    if method not in METHOD_KIND:
        raise ValueError(f'Unknown payment method: {method}')
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ValueError('There is nothing to pay.')
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 64:
        raise ValueError('A payment needs a booking reference.')
    env = environment or current_environment()
    currency = (currency or payment_settings()['currency']).upper()
    if not re.fullmatch(r'[A-Z]{3}', currency):
        raise ValueError('The payment currency must be a three-letter code such as SAR.')
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    ledger = ledger if ledger is not None else PaymentLedger(None)
    record = {'method': method, 'label': METHOD_TITLE[method], 'amount_cents': amount_cents,
              'currency': currency, 'reference': reference, 'environment': env}
    if METHOD_KIND[method] == 'counter':
        record.update(status=PENDING_COUNTER, transaction_id='', gateway='counter', message='Pay at the counter',
                      simulated=False)
        _event(record, PENDING_COUNTER, stamp, 'booking')
        ledger.log({'at': stamp, 'reference': reference, 'method': method, 'gateway': 'counter',
                    'environment': env, 'status': PENDING_COUNTER, 'amount_cents': amount_cents,
                    'currency': currency, 'source': 'booking'})
        return record

    if provider is None:
        provider = load_payment_provider(environment=env)
    ok, why = online_availability(provider, env)
    if not ok:
        raise PaymentUnavailable(why)
    record.update(gateway=getattr(provider, 'name', 'gateway'), simulated=not getattr(provider, 'live', False),
                  idempotency_key=reference, transaction_id='', message='')
    _event(record, 'started', stamp, 'booking')
    try:
        result = _charge_once(provider, method, amount_cents, reference, phone, currency)
    except PaymentUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any provider failure means "unknown", never "paid"
        import cta_errors
        cta_errors.report(exc, 'starting a payment with the provider', reference=reference)
        result = PaymentResult(UNKNOWN, '', 'The payment provider did not answer. You have not been charged '
                                            'unless the provider confirms it later.')
    if not isinstance(result, PaymentResult):        # a misbehaving adapter: treat as unknown, never as paid
        import cta_errors
        cta_errors.report(TypeError(f'charge() returned {type(result).__name__}, not PaymentResult'),
                          'starting a payment with the provider', reference=reference)
        result = PaymentResult(UNKNOWN, '', 'The payment provider gave an unreadable answer. It was not accepted.')
    claimed = result.status if result.status in STATES and result.status != PENDING_COUNTER else UNKNOWN
    tx = _clean(result.transaction_id)
    if claimed in (FAILED, CANCELLED, PENDING):
        return _finish_verified(record, provider, ledger, claimed, result.message, tx, stamp, 'provider')
    # The provider claims PAID (or something we do not understand): confirm with the provider's own record.
    status, msg = verify_transaction(provider, tx, amount_cents=amount_cents, currency=currency, reference=reference)
    return _finish_verified(record, provider, ledger, status, msg or result.message, tx, stamp, 'verified')


def confirm_payment(booking: dict, provider, now: datetime, ledger: Optional[Ledger] = None,
                    transaction_id: str = '', source: str = 'callback',
                    environment: Optional[str] = None) -> Tuple[bool, str]:
    """Re-check an unconfirmed online payment with the provider (provider callback or a staff 'check again').

    Only the provider's verified record is trusted; the notification itself is not. Calling this again for a
    booking that is already paid changes nothing, so repeated callbacks are harmless."""
    payment = booking.get('payment')
    if not isinstance(payment, dict) or METHOD_KIND.get(str(payment.get('method'))) != 'online':
        return False, 'Not an online payment.'
    env = environment or current_environment()
    ok, why = online_availability(provider, env)
    if not ok:
        return False, why
    tx = _clean(transaction_id or payment.get('transaction_id'))
    if payment.get('status') == PAID:
        same = not transaction_id or tx == payment.get('transaction_id')
        return False, 'Already paid - nothing changed.' if same else 'Already paid by a different transaction.'
    if payment.get('status') not in UNCONFIRMED:
        return False, 'This payment was not accepted and cannot be confirmed.'
    if getattr(provider, 'name', '') != payment.get('gateway'):
        return False, 'That provider did not handle this payment.'
    if payment.get('transaction_id') and tx != payment.get('transaction_id'):
        return False, 'The transaction reference does not match this booking.'
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    status, msg = verify_transaction(provider, tx, amount_cents=payment.get('amount_cents', 0),
                                     currency=payment.get('currency', DEFAULT_CURRENCY),
                                     reference=booking.get('reference', ''))
    updated = dict(payment)
    updated['events'] = list(payment.get('events', []))
    updated.setdefault('reference', booking.get('reference', ''))
    _finish_verified(updated, provider, ledger if ledger is not None else PaymentLedger(None),
                     status, msg, tx, stamp, source)
    booking['payment'] = updated
    return updated['status'] == PAID, status_text(updated)


def handle_callback(history: List[dict], payload: dict, provider, now: datetime,
                    ledger: Optional[Ledger] = None,
                    environment: Optional[str] = None) -> Tuple[bool, str, Optional[dict]]:
    """A provider notification arrived. ``payload`` is untrusted: it is only used to find the booking and the
    transaction; the status, amount and currency come from ``provider.verify``."""
    if not isinstance(payload, dict):
        return False, 'Bad notification.', None
    reference = str(payload.get('reference') or '').strip()
    tx = str(payload.get('transaction_id') or '').strip()
    booking = next((r for r in history if str(r.get('reference', '')) == reference), None) if reference else None
    if booking is None:
        return False, 'No booking with that reference.', None
    ok, msg = confirm_payment(booking, provider, now, ledger, transaction_id=tx, source='callback',
                              environment=environment)
    return ok, msg, booking


# ------------------------------------------------------------------ reading payment records
def legacy_payment(total_cents: int) -> dict:
    """Bookings made before payments existed count as paid so old tickets keep working."""
    return {'method': 'legacy', 'label': 'Paid (before payment methods were added)', 'status': PAID,
            'amount_cents': total_cents, 'transaction_id': '', 'gateway': 'legacy'}


def is_simulated(payment: dict) -> bool:
    """True for test-mode payments, including ones stored before the ``simulated`` flag existed."""
    return bool(payment.get('simulated')) or payment.get('gateway') == 'simulated'


def is_settled(payment: dict, environment: Optional[str] = None) -> bool:
    """Money is genuinely in: paid, and not a simulated payment when running in production."""
    if payment.get('status') != PAID:
        return False
    if is_simulated(payment):
        return (environment or current_environment()) != PRODUCTION
    return True


def status_text(payment: dict) -> str:
    status = payment.get('status', '')
    if status == PAID and is_simulated(payment):
        return 'Paid (test mode - no money taken)'
    return {PAID: 'Paid', PENDING_COUNTER: 'awaiting payment at the counter', FAILED: 'Payment failed',
            PENDING: 'Payment pending - not valid for travel yet', CANCELLED: 'Payment cancelled',
            UNKNOWN: 'Payment not confirmed - not valid for travel yet'}.get(status, '?')


def methods() -> List[tuple]:
    return list(METHODS)
