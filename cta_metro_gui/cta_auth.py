"""Staff sign-in, roles, permissions and the security audit trail (no GUI code here).

Who may do what is decided HERE and checked by the database service layer (cta_db.Store) for every sensitive
operation - not by hiding buttons. A staff member acts through a ``Principal``, which only this module creates, after
a correct password. It carries a random session token; the database keeps only its SHA-256 hash and re-checks on every
operation that the session is live, the account still exists and is enabled, and the account's CURRENT role has the
permission. A plain username string is never accepted as proof of anything.

Roles (see docs/PERMISSIONS.md)
-------------------------------
Today the app has ONE role, ``admin``, which may do everything - that has not changed. The other roles below are a
PROPOSAL awaiting the project owner's approval: the checks already work for them, but no account can be given one of
those roles until ``ASSIGNABLE_ROLES`` is extended after approval.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Tuple

PERMISSIONS: Dict[str, str] = {
    'ticket.check_in': 'Check tickets in at the gate',
    'ticket.reentry': 'Allow re-entry on a ticket that was already used',
    'ticket.cancel': 'Cancel a ticket or a whole booking',
    'ticket.refund': 'Record that a refund was paid out',
    'ticket.correct_name': 'Correct the spelling of a passenger name',
    'payment.mark_paid': 'Record cash received at the counter (changes payment status)',
    'report.view': 'Open the admin portal: bookings, passenger phone numbers, statistics',
    'report.export': 'Export the ticket list as CSV (contains phone numbers)',
    'audit.read': 'Read the audit log',
    'audit.export': 'Export the audit log',
    'support.reply': 'Read passenger messages, reply to them and decide refund requests',
    'staff.manage': 'Create, disable, re-enable staff accounts, change roles, reset passwords',
    'records.edit': 'Edit stored bookings directly (data repair)',
}
_ALL = frozenset(PERMISSIONS)
ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    'admin': _ALL,                                                               # current behaviour: everything
    'supervisor': frozenset({'ticket.check_in', 'ticket.reentry', 'ticket.cancel', 'ticket.refund',  # proposed
                             'ticket.correct_name', 'payment.mark_paid', 'report.view', 'report.export',
                             'audit.read'}),
    'cashier': frozenset({'ticket.check_in', 'payment.mark_paid', 'report.view'}),                    # proposed
    'gate': frozenset({'ticket.check_in'}),                                                           # proposed
}
ROLE_TITLES = {'admin': 'Administrator', 'supervisor': 'Station supervisor', 'cashier': 'Counter cashier',
               'gate': 'Gate / ticketing staff'}
MATRIX_STATUS = 'proposed'                   # becomes 'approved' when the project owner signs the matrix off
ASSIGNABLE_ROLES: Tuple[str, ...] = ('admin',)   # roles that may be given to accounts today

SESSION_HOURS = 12                           # a sign-in lasts one shift; signing out ends it at once
MAX_FAILURES = 5                             # wrong passwords in a row before the account is locked ...
LOCK_SECONDS = 30                            # ... for this long (same numbers as before, now per account and shared)
SECRET_WORDS = re.compile(r'pass|pwd|otp|code|token|secret|recovery|pin|cvv|cvc|card|salt|hash|key', re.I)


class AuthError(Exception):
    """Sign-in failed. ``str(exc)`` is safe to show (never says whether the username exists)."""


class PermissionDenied(Exception):
    """The signed-in person may not do this. ``str(exc)`` is safe to show. The denial is already in the audit log."""


@dataclass(frozen=True)
class Principal:
    """A signed-in staff member. Created only by sign-in; useless without its live session in the database."""
    username: str
    role: str
    token: str = field(repr=False, compare=False)

    def __getitem__(self, key: str) -> str:          # older code reads app.staff['username']
        return {'username': self.username, 'role': self.role}[key]

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    @property
    def token_hash(self) -> str:
        return token_hash(self.token)


def token_hash(token: str) -> str:
    return hashlib.sha256(('CTA-SESSION|' + token).encode('utf-8')).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def role_allows(role: str, permission: str) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def stamp(now: datetime) -> str:
    return now.strftime('%Y-%m-%d %H:%M:%S')


def session_expiry(now: datetime) -> str:
    return stamp(now + timedelta(hours=SESSION_HOURS))


def scrub(value, depth: int = 0):
    """Remove anything that looks like a secret from data headed for the audit log, and shorten long text."""
    if depth > 4:
        return '(nested)'
    if isinstance(value, dict):
        return {k: scrub(v, depth + 1) for k, v in value.items() if not SECRET_WORDS.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        return value[:200]
    return value


def clean_actor(name) -> str:
    """What may appear in the 'actor' column for an attempted username: short, printable, no spaces."""
    text = ''.join(ch for ch in str(name or '') if ch.isprintable() and not ch.isspace())[:40]
    return text or '(blank)'


# ------------------------------------------------------------------ staff sign-in and management (service layer)
class StaffAuth:
    """All staff authentication and account management, on top of a cta_db.Store."""

    def __init__(self, store):
        self.store = store

    # -------- sign-in
    def needs_bootstrap(self) -> bool:
        return self.store.staff_count() == 0

    def bootstrap_admin(self, username: str, password: str, now: datetime, device: str = '') -> Principal:
        """First run only: create the first administrator. Refused once any staff account exists."""
        return self.store.bootstrap_admin(username, password, now, device)

    def login(self, username: str, password: str, now: datetime, device: str = '') -> Principal:
        return self.store.staff_login(username, password, now, device)

    def logout(self, actor: Principal, now: datetime) -> None:
        self.store.staff_logout(actor, now)

    # -------- account management (needs 'staff.manage')
    def create_staff(self, actor: Principal, username: str, password: str, role: str, now: datetime) -> dict:
        return self.store.create_staff_account(actor, username, password, role, now)

    def set_enabled(self, actor: Principal, username: str, enabled: bool, now: datetime) -> str:
        return self.store.set_staff_enabled(actor, username, enabled, now)

    def set_role(self, actor: Principal, username: str, role: str, now: datetime) -> str:
        return self.store.set_staff_role(actor, username, role, now)

    def reset_password(self, actor: Principal, username: str, new_password: str, now: datetime) -> str:
        return self.store.reset_staff_password(actor, username, new_password, now)

    def list_staff(self, actor: Principal, now: datetime):
        return self.store.list_staff(actor, now)
