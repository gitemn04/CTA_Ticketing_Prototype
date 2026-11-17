"""Shared SQLite database (``cta.db``) for bookings, passengers, tickets, payments, staff, accounts, scans and audit.

Why: several windows or computers share one data folder. Rewriting a whole JSON file on every save means the last
writer silently wins. Here every booking row has a ``version``; a save only succeeds if the booking still has the
version this window loaded, otherwise ``ConcurrentUpdate`` is raised and nothing is written.

The rest of the app keeps working with the same booking dictionaries as before: ``load_history`` rebuilds them from
the tables and ``save_history`` writes back only the bookings that changed.

Start-up (``Store.open``):
* ``cta.db`` exists               -> integrity check, then use it (read-only if the file is not writable).
* ``cta.db`` missing, backups exist -> ``DataMissing`` (restore a backup; this is not a first run).
* ``cta.db`` missing, JSON files  -> back the JSON files up, migrate into a NEW temporary database, verify every
                                     record round-trips exactly, and only then move it into place. The JSON files
                                     are never modified or deleted.
* nothing at all                  -> first run: create an empty database.

Note: SQLite locking is reliable on a local disk. On a network share it depends on the file server; for several
computers a database server is the right long-term home (see README).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import cta_core as core
import cta_auth as auth
import cta_fares as fares
import cta_payments as payments
import cta_tickets as tickets
import cta_accounts as acct
from cta_accounts import validate_accounts, validate_staff
from cta_core import validate_history
from cta_storage import (ConcurrentUpdate, DataCorrupted, DataInvalid, DataMissing, DataUnreadable,
                         DataWriteError, ReadOnlyStorage, StorageError, backup_dir, backup_file, list_backups,
                         prune_backups, read_json, stamp, RECOVERY_HINT)

DB_NAME = 'cta.db'
SCHEMA_VERSION = 3
JSON_SOURCES = ('history.json', 'accounts.json', 'staff.json', 'payments_ledger.json', 'payment_events.jsonl')
AUTO_BACKUP_EVERY = timedelta(hours=24)
# Per-journey fields. The outbound journey stores them as ``status``, ``ticket_id``...; the return journey with a
# ``return_`` prefix. They live in the ``tickets`` table (the authoritative record), not in the booking document.
_LEG_KEYS = ('ticket_id', 'status', 'uses', 'max_uses', 'boarded_at', 'boarded_by', 'cancelled_at', 'cancelled_by',
             'cancel_reason', 'refund_cents', 'refund_state', 'refunded_at', 'refunded_by', 'refund_ref', 'reentries')
_TICKET_KEYS = ('ticket_no', 'return_ticket_no') + _LEG_KEYS + tuple('return_' + k for k in _LEG_KEYS)
_TCOLS = ('ticket_no', 'reference', 'passenger_no', 'leg') + _LEG_KEYS

TICKETS_V2 = """
CREATE TABLE {name} (
    ticket_no TEXT PRIMARY KEY COLLATE NOCASE,
    reference TEXT NOT NULL,
    passenger_no INTEGER NOT NULL,
    leg TEXT NOT NULL CHECK (leg IN ('out', 'return')),
    ticket_id TEXT,
    status TEXT CHECK (status IS NULL OR status IN ('issued', 'boarded', 'cancelled', 'refunded')),
    uses INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 1,
    boarded_at TEXT, boarded_by TEXT,
    cancelled_at TEXT, cancelled_by TEXT, cancel_reason TEXT,
    refund_cents INTEGER CHECK (refund_cents IS NULL OR refund_cents >= 0),
    refund_state TEXT CHECK (refund_state IS NULL OR refund_state IN ('due', 'done')),
    refunded_at TEXT, refunded_by TEXT, refund_ref TEXT,
    reentries TEXT,
    CHECK (max_uses >= 1 AND uses >= 0 AND uses <= max_uses),
    FOREIGN KEY (reference, passenger_no) REFERENCES passengers(reference, no));
"""
TICKET_INDEXES = """
CREATE UNIQUE INDEX tickets_id_unique ON tickets(ticket_id) WHERE ticket_id IS NOT NULL;
CREATE INDEX tickets_by_booking ON tickets(reference);
"""
SCANS_V2 = """
CREATE TABLE scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticket_no TEXT NOT NULL,
    staff TEXT, ok INTEGER NOT NULL, result TEXT NOT NULL,
    ticket_id TEXT, leg TEXT, method TEXT, device TEXT, station TEXT);
"""
# Sign-in sessions (only a hash of each token is stored) and per-account failed-password counters.
AUTH_TABLES = """
CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, ended_at TEXT, device TEXT);
CREATE TABLE login_attempts (
    username_key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, locked_until TEXT);
"""
AUDIT_GUARDS = (
    "CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END",
    "CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END")

# Tickets and their ids are never deleted or renumbered; the scan and audit logs only ever grow.
GUARDS_V2 = """
CREATE TRIGGER tickets_never_deleted BEFORE DELETE ON tickets
    BEGIN SELECT RAISE(ABORT, 'tickets are never deleted'); END;
CREATE TRIGGER tickets_id_fixed BEFORE UPDATE OF ticket_id ON tickets
    WHEN OLD.ticket_id IS NOT NULL AND NEW.ticket_id IS NOT OLD.ticket_id
    BEGIN SELECT RAISE(ABORT, 'a ticket id never changes'); END;
CREATE TRIGGER scans_no_update BEFORE UPDATE ON scans BEGIN SELECT RAISE(ABORT, 'the scan log is append-only'); END;
CREATE TRIGGER scans_no_delete BEFORE DELETE ON scans BEGIN SELECT RAISE(ABORT, 'the scan log is append-only'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
"""

SCHEMA = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL UNIQUE COLLATE NOCASE,
    issued_at TEXT NOT NULL,
    depart_at TEXT,
    account_phone TEXT,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    legs INTEGER NOT NULL CHECK (legs IN (1, 2)),
    doc TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL);
CREATE TABLE passengers (
    reference TEXT NOT NULL REFERENCES bookings(reference),
    no INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    fare_cents INTEGER NOT NULL CHECK (fare_cents >= 0),
    doc TEXT NOT NULL,
    PRIMARY KEY (reference, no));
@@TICKETS@@
CREATE TABLE payments (
    reference TEXT PRIMARY KEY REFERENCES bookings(reference),
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({', '.join(repr(s) for s in payments.STATES)})),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    gateway TEXT,
    transaction_id TEXT,
    doc TEXT NOT NULL);
CREATE TABLE payment_claims (
    gateway TEXT NOT NULL, transaction_id TEXT NOT NULL, reference TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (gateway, transaction_id));
@@SCANS@@
CREATE TABLE audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, reference TEXT,
    actor TEXT, detail TEXT, source_key TEXT UNIQUE,
    outcome TEXT NOT NULL DEFAULT 'success', target TEXT, chain TEXT);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE, name TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL, doc TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
@@AUTH_TABLES@@
@@TICKET_INDEXES@@
CREATE INDEX scans_by_ticket ON scans(ticket_no);
@@GUARDS@@
"""
SCHEMA = (SCHEMA.replace('@@TICKETS@@', TICKETS_V2.format(name='tickets'))
          .replace('@@SCANS@@', SCANS_V2).replace('@@TICKET_INDEXES@@', TICKET_INDEXES)
          .replace('@@GUARDS@@', GUARDS_V2).replace('@@AUTH_TABLES@@', AUTH_TABLES))
TABLES = ('meta', 'bookings', 'passengers', 'tickets', 'payments', 'payment_claims', 'scans', 'audit',
          'accounts', 'staff')

# v3.0: web access tokens for the ticket page server (CREATE IF NOT EXISTS — safe for existing DBs)
WEB_TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_web_tokens (
    ticket_id  TEXT PRIMARY KEY,
    token      TEXT NOT NULL UNIQUE,
    issued_at  TEXT NOT NULL,
    revoked_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS web_tokens_by_token ON ticket_web_tokens(token);
"""

# Passenger messages and refund requests. Created on first open of a writable database with CREATE ... IF NOT EXISTS,
# so databases made by earlier versions keep working and need no upgrade step (the schema version stays 3, and an older
# copy of the app simply ignores these tables). Messages are never edited or deleted.
SUPPORT_KINDS = ('problem', 'refund_request')
SUPPORT_STATUSES = ('open', 'answered', 'approved', 'declined')
SUPPORT_TOPICS = ('Fare issue', 'Receipt or printer', 'Wrong route or time', 'Accessibility',
                  'Complaint about service or staff', 'Complaint about a station or train', 'Other')
MAX_MESSAGE = 1000
SUPPORT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS support_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('problem', 'refund_request')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        account_phone TEXT, reference TEXT, topic TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'answered', 'approved', 'declined')),
        handled_by TEXT)""",
    """CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL REFERENCES support_cases(id),
        at TEXT NOT NULL, author TEXT NOT NULL, from_staff INTEGER NOT NULL CHECK (from_staff IN (0, 1)),
        body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 1000))""",
    'CREATE INDEX IF NOT EXISTS support_by_phone ON support_cases(account_phone)',
    'CREATE INDEX IF NOT EXISTS support_by_case ON support_messages(case_id)',
    """CREATE TRIGGER IF NOT EXISTS support_messages_no_update BEFORE UPDATE ON support_messages
        BEGIN SELECT RAISE(ABORT, 'messages are never edited'); END""",
    """CREATE TRIGGER IF NOT EXISTS support_messages_no_delete BEFORE DELETE ON support_messages
        BEGIN SELECT RAISE(ABORT, 'messages are never deleted'); END""",
)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


_AUDIT_HASHED = ('at', 'kind', 'reference', 'actor', 'target', 'outcome', 'detail', 'source_key')


def _chain(prev: str, row: dict) -> str:
    """Each audit row's hash covers the previous row's hash, so changing or removing a row breaks every later one."""
    body = canonical([row.get(k) for k in _AUDIT_HASHED])
    return hashlib.sha256((prev + '|' + body).encode('utf-8')).hexdigest()


def _now(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


def _map_error(exc: sqlite3.Error, path: Path) -> StorageError:
    text = str(exc).lower()
    if 'not a database' in text or 'malformed' in text or 'corrupt' in text:
        return DataCorrupted(f'The database ({path.name}) is damaged. {RECOVERY_HINT}', path)
    if 'readonly' in text or 'read-only' in text:
        return ReadOnlyStorage(f'The database ({path.name}) is read-only, so nothing can be saved.', path)
    if 'locked' in text or 'busy' in text:
        return DataWriteError('The database is busy (another window is saving). Please try again.', path)
    if 'unable to open' in text or 'disk i/o' in text or 'permission' in text:
        return DataUnreadable(f'The database ({path.name}) cannot be opened (permissions or disk problem). '
                              f'{RECOVERY_HINT}', path)
    if 'unique' in text or 'constraint' in text or 'foreign key' in text:
        return DataInvalid('Not saved: a record would break a uniqueness or consistency rule '
                           '(for example a ticket number already used by another booking).', path)
    return DataWriteError(f'Database error while using {path.name}. Nothing was changed.', path)


# ------------------------------------------------------------------ record <-> rows
def _leg_row(rec: dict, p: dict, leg: str) -> tuple:
    pre = 'return_' if leg == 'return' else ''
    status = p.get(pre + 'status')
    max_uses = p.get(pre + 'max_uses', 1)
    uses = p.get(pre + 'uses', 1 if status == 'boarded' else 0)
    vals = {k: p.get(pre + k) for k in _LEG_KEYS}
    vals.update(uses=uses, max_uses=max_uses)
    if vals['reentries'] is not None:
        vals['reentries'] = canonical(vals['reentries'])
    return ((p[pre + 'ticket_no'], rec['reference'], p['no'], leg) + tuple(vals[k] for k in _LEG_KEYS))


def _split(rec: dict):
    doc = {k: v for k, v in rec.items() if k not in ('passengers', 'payment')}
    pax, tickets = [], []
    for p in rec.get('passengers') or []:
        pax.append((rec['reference'], p['no'], p['name'], p['category'], p['fare_cents'],
                    canonical({k: v for k, v in p.items() if k not in _TICKET_KEYS})))
        tickets.append(_leg_row(rec, p, 'out'))
        if 'return_ticket_no' in p:
            tickets.append(_leg_row(rec, p, 'return'))
    pay = rec.get('payment')
    pay_row = None if pay is None else (rec['reference'], pay['method'], pay['status'], pay['amount_cents'],
                                        pay.get('gateway'), pay.get('transaction_id'), canonical(pay))
    return doc, pax, tickets, pay_row


def _build(doc_json: str, pax_rows, ticket_rows, pay_doc: Optional[str]) -> dict:
    rec = json.loads(doc_json)
    if pax_rows:
        by_no: Dict[int, dict] = {}
        for no, pdoc in pax_rows:
            by_no[no] = json.loads(pdoc)
        for row in ticket_rows:
            t = dict(zip(_TCOLS, row))
            p = by_no[t['passenger_no']]
            pre = 'return_' if t['leg'] == 'return' else ''
            p[pre + 'ticket_no'] = t['ticket_no']
            for k in _LEG_KEYS:
                if k in ('uses', 'max_uses') or t[k] is None:
                    continue
                p[pre + k] = json.loads(t[k]) if k == 'reentries' else t[k]
            if t['max_uses'] > 1:                               # single-use tickets keep the original record shape
                p[pre + 'max_uses'], p[pre + 'uses'] = t['max_uses'], t['uses']
        rec['passengers'] = [by_no[n] for n in sorted(by_no)]
    if pay_doc is not None:
        rec['payment'] = json.loads(pay_doc)
    return rec


def _fresh_booking(rec: dict) -> bool:
    """True if a booking looks exactly as the booking screen creates it (so no staff permission is needed)."""
    for p in rec.get('passengers') or []:
        for pre in ('', 'return_'):
            if (pre + 'ticket_no') not in p:
                continue
            if p.get(pre + 'status', 'issued') != 'issued' or p.get(pre + 'max_uses', 1) != 1:
                return False
            if any(k in p for k in (pre + 'boarded_at', pre + 'uses', pre + 'cancelled_at', pre + 'refund_state',
                                    pre + 'reentries')):
                return False
        if p.get('corrections'):
            return False
    pay = rec.get('payment')
    if pay is None:
        return 'passengers' in rec                 # an unnamed, unpaid record is an old-format import, not a booking
    if pay.get('status') not in (payments.PENDING_COUNTER, payments.PENDING, payments.UNKNOWN, payments.PAID):
        return False
    if pay.get('status') == payments.PAID and payments.METHOD_KIND.get(pay.get('method')) != 'online':
        return False                              # cash is only ever paid at the counter, through mark_paid
    if any(k in pay for k in ('received_by', 'refunded_cents', 'original_amount_cents')):
        return False
    return True


def _comparable(rec: dict) -> dict:
    """A booking without the ticket ids, so an old JSON copy (no ids) can be compared with the database copy."""
    out = json.loads(json.dumps(rec))
    for p in out.get('passengers') or []:
        p.pop('ticket_id', None)
        p.pop('return_ticket_id', None)
    return out


# ------------------------------------------------------------------ the store
class Store:
    def __init__(self, path: Path, read_only: bool = False, allow_old: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._snap: Dict[str, Tuple[int, str]] = {}          # reference -> (version, canonical) as last loaded
        self.revision_seen: Optional[int] = None
        self._no_bump = False
        try:
            if read_only:
                self.conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                            timeout=10, isolation_level=None)
            else:
                self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            self.conn.execute('PRAGMA foreign_keys = ON')
            if not read_only:
                self.conn.execute('PRAGMA synchronous = FULL')
            version = self._check()
            self.schema_version = version
            if version < SCHEMA_VERSION:
                if read_only:
                    if not allow_old:
                        raise DataInvalid(f'The database ({self.path.name}) was made by an older version and has to be '
                                          'upgraded. Open it with write access once (a backup is made first).', self.path)
                else:
                    self._upgrade()
                    self.schema_version = SCHEMA_VERSION
            if not read_only and self.schema_version >= 3:
                self._ensure_support()
        except sqlite3.Error as exc:
            self._close_quietly()
            raise _map_error(exc, self.path) from None
        except StorageError:
            self._close_quietly()
            raise

    def _ensure_support(self) -> None:
        """Make sure the passenger-message and web-token tables exist (cheap and safe to repeat)."""
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if 'support_cases' not in have or 'support_messages' not in have:
            for stmt in SUPPORT_SCHEMA:
                self.conn.execute(stmt)
        # Indexes for the scan-history, my-bookings and permission-check paths (safe to repeat on every open).
        for stmt in ('CREATE INDEX IF NOT EXISTS scans_by_ticket_id ON scans(ticket_id)',
                     'CREATE INDEX IF NOT EXISTS bookings_by_phone ON bookings(account_phone)',
                     'CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(username)'):
            self.conn.execute(stmt)
        # v3.0: web access tokens table (CREATE IF NOT EXISTS — safe on every open)
        for stmt in WEB_TOKEN_SCHEMA.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                self.conn.execute(stmt)

    def _close_quietly(self):
        try:
            self.conn.close()
        except (sqlite3.Error, AttributeError):          # never connected, or already closed
            pass

    def close(self):
        self._close_quietly()

    def _check(self) -> int:
        if self.conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise DataCorrupted(f'The database ({self.path.name}) failed its integrity check. {RECOVERY_HINT}',
                                self.path)
        have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not set(TABLES) <= have:
            raise DataInvalid(f'{self.path.name} is not a CTA Metro database. {RECOVERY_HINT}', self.path)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None or int(row[0]) > SCHEMA_VERSION:
            raise DataInvalid(f'{self.path.name} was made by a newer or unknown version of the app.', self.path)
        return int(row[0])

    def _upgrade(self) -> None:
        """Version 1 -> 2: ticket ids, use counters, cancel/refund fields, scan-log columns, guard triggers.
        A checked backup is taken first; the whole change is one transaction (all or nothing)."""
        self.backup('pre-upgrade')
        try:
            self.conn.execute('BEGIN IMMEDIATE')
        except sqlite3.Error as exc:
            raise _map_error(exc, self.path) from None
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if int(row[0]) >= SCHEMA_VERSION:                     # another window finished the upgrade first
                self.conn.execute('COMMIT')
                return
            if int(row[0]) >= 2:
                self._upgrade_to_3(self.conn)
                self.conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")
                self.conn.execute('COMMIT')
                return
            c = self.conn
            c.execute(TICKETS_V2.format(name='tickets_new'))
            c.execute('INSERT INTO tickets_new (ticket_no, reference, passenger_no, leg, status, uses, boarded_at, '
                      'boarded_by) SELECT ticket_no, reference, passenger_no, leg, status, '
                      "CASE WHEN status = 'boarded' THEN 1 ELSE 0 END, boarded_at, boarded_by FROM tickets")
            c.execute('DROP TABLE tickets')
            c.execute('ALTER TABLE tickets_new RENAME TO tickets')
            for stmt in TICKET_INDEXES.strip().split(';'):
                if stmt.strip():
                    c.execute(stmt)
            for col in ('ticket_id', 'leg', 'method', 'device', 'station'):
                c.execute(f'ALTER TABLE scans ADD COLUMN {col} TEXT')
            # Version-1 scan rows kept the whole message, which contains the passenger's name: keep only the outcome.
            c.execute("UPDATE scans SET result = CASE WHEN ok = 1 THEN 'OK' ELSE 'REFUSED' END")
            for stmt in GUARDS_V2.strip().split(';\n'):
                if stmt.strip():
                    c.execute(stmt.strip().rstrip(';'))
            c.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
            self._upgrade_to_3(c)
            c.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")
            c.execute('COMMIT')
        except BaseException as exc:
            try:
                self.conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise _map_error(exc, self.path) from None
            raise

    def _upgrade_to_3(self, c) -> None:
        """Version 2 -> 3 (inside the caller's transaction): audit outcome/target/hash chain, staff on/off switch,
        sign-in sessions and failed-password counters. Existing audit rows get their chain computed once."""
        for col in ("outcome TEXT NOT NULL DEFAULT 'success'", 'target TEXT', 'chain TEXT'):
            c.execute(f'ALTER TABLE audit ADD COLUMN {col}')
        c.execute('DROP TRIGGER IF EXISTS audit_no_update')
        c.execute('DROP TRIGGER IF EXISTS audit_no_delete')
        prev = ''
        for row in c.execute(f'SELECT id, {", ".join(_AUDIT_HASHED)} FROM audit ORDER BY id').fetchall():  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
            prev = _chain(prev, dict(zip(_AUDIT_HASHED, row[1:])))
            c.execute('UPDATE audit SET chain = ? WHERE id = ?', (prev, row[0]))
        for stmt in AUDIT_GUARDS:
            c.execute(stmt)
        c.execute('ALTER TABLE staff ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')
        for stmt in AUTH_TABLES.strip().split(';'):
            if stmt.strip():
                c.execute(stmt)
        c.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")

    @staticmethod
    def create(path: Path, now: Optional[datetime] = None) -> 'Store':
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            conn.executescript(SCHEMA)                           # new temporary file: nothing to protect yet
            conn.execute('BEGIN')
            conn.executemany('INSERT INTO meta VALUES (?, ?)', [('schema_version', str(SCHEMA_VERSION)),
                                                                ('revision', '0'), ('created_at', _now(now))])
            conn.execute('COMMIT')
        finally:
            conn.close()
        return Store(path)

    # ---------------------------------------------------------------- meta
    def meta_get(self, key: str) -> Optional[str]:
        row = self.conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        if self.meta_get(key) == value:
            return
        with self._write() as c:
            c.execute('INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                      (key, value))

    # ---------------------------------------------------------------- transactions
    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise ReadOnlyStorage(f'The database ({self.path.name}) is read-only, so nothing can be saved. '
                                  f'{RECOVERY_HINT}', self.path)
        try:
            self.conn.execute('BEGIN IMMEDIATE')                 # takes the write lock now: no lost updates
        except sqlite3.Error as exc:
            raise _map_error(exc, self.path) from None
        self._no_bump = False
        try:
            yield self.conn
            if not self._no_bump:                                # nothing changed -> other windows need not reload
                self.conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")
            self.conn.execute('COMMIT')
        except BaseException as exc:
            try:
                self.conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise _map_error(exc, self.path) from None
            raise

    def revision(self) -> int:
        try:
            return int(self.conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])
        except sqlite3.Error as exc:
            raise _map_error(exc, self.path) from None

    def has_changes(self) -> bool:
        """True when another window saved something since this window last loaded."""
        return self.revision_seen is None or self.revision() != self.revision_seen

    # ---------------------------------------------------------------- bookings
    def _read_all(self, conn, reference: Optional[str] = None) -> List[Tuple[int, dict]]:
        """(version, booking) for every booking, or for one booking reference. No SQL text is ever passed in."""
        where, args = ('WHERE reference = ?', (reference,)) if reference is not None else ('', ())
        rows = conn.execute(f'SELECT reference, doc, version FROM bookings {where} ORDER BY id', args).fetchall()  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
        refs = [r[0] for r in rows]
        pax: Dict[str, list] = {r: [] for r in refs}
        tix: Dict[str, list] = {r: [] for r in refs}
        pays: Dict[str, str] = {}
        if refs:
            marks = ','.join('?' * len(refs)) if where else ''
            sel = f' WHERE reference IN ({marks})' if where else ''
            for ref, no, doc in conn.execute(f'SELECT reference, no, doc FROM passengers{sel}', refs if where else ()):  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                pax.setdefault(ref, []).append((no, doc))
            for row in conn.execute(f'SELECT {", ".join(_TCOLS)} FROM tickets{sel}', refs if where else ()):  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                tix.setdefault(row[1], []).append(row)
            for ref, doc in conn.execute(f'SELECT reference, doc FROM payments{sel}', refs if where else ()):  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                pays[ref] = doc
        return [(ver, _build(doc, pax.get(ref), tix.get(ref, []), pays.get(ref))) for ref, doc, ver in rows]

    def load_history(self) -> List[dict]:
        try:
            self.conn.execute('BEGIN')                           # one consistent snapshot of all tables
            try:
                rev = self.revision()
                rows = self._read_all(self.conn)
            finally:
                self.conn.execute('COMMIT')
        except sqlite3.Error as exc:
            raise _map_error(exc, self.path) from None
        except (ValueError, KeyError, TypeError):
            raise DataCorrupted(f'A booking in {self.path.name} could not be decoded. {RECOVERY_HINT}', self.path)
        out = [rec for _v, rec in rows]
        self._snap = {rec['reference'].upper(): (ver, canonical(rec)) for ver, rec in rows}
        self.revision_seen = rev
        return out

    def get_booking(self, reference: str) -> Optional[dict]:
        rows = self._read_all(self.conn, reference)
        return rows[0][1] if rows else None

    def _insert_rows(self, conn, rec: dict, now: str, version: int = 1, update: bool = False,
                     expect_version: Optional[int] = None):
        """Write a booking. New bookings use plain INSERTs, so a clash with another booking's ticket number or id is an
        error, never an overwrite. Updates are UPSERTs: tickets and passengers are never deleted, and an existing
        ticket id is never replaced."""
        doc, pax, tickets, pay_row = _split(rec)
        ref = rec['reference']
        cols = (rec['issued_at'], rec.get('depart_at'), rec.get('account_phone'), rec['total_cents'],
                rec.get('legs', 1), canonical(doc), version, now)
        if update:
            cur = conn.execute(
                'UPDATE bookings SET issued_at=?, depart_at=?, account_phone=?, total_cents=?, legs=?, doc=?, '  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                'version=?, updated_at=? WHERE reference = ?' + ('' if expect_version is None else ' AND version = ?'),
                cols + (ref,) + (() if expect_version is None else (expect_version,)))
            if cur.rowcount != 1:
                raise ConcurrentUpdate(f'Booking {ref} was changed by another window or staff member. Nothing was '
                                       'saved; please try again.', self.path)
        else:
            conn.execute('INSERT INTO bookings (issued_at, depart_at, account_phone, total_cents, legs, doc, version, '
                         'updated_at, reference) VALUES (?,?,?,?,?,?,?,?,?)', cols + (ref,))
        if update:
            conn.executemany('INSERT INTO passengers VALUES (?,?,?,?,?,?) ON CONFLICT(reference, no) DO UPDATE SET '
                             'name = excluded.name, category = excluded.category, fare_cents = excluded.fare_cents, '
                             'doc = excluded.doc', pax)
            sets = ', '.join(f'{k} = excluded.{k}' for k in _LEG_KEYS if k != 'ticket_id')
            conn.executemany(
                f'INSERT INTO tickets ({", ".join(_TCOLS)}) VALUES ({",".join("?" * len(_TCOLS))}) '  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                f'ON CONFLICT(ticket_no) DO UPDATE SET {sets}, '
                'ticket_id = COALESCE(tickets.ticket_id, excluded.ticket_id) '
                'WHERE tickets.reference = excluded.reference AND tickets.passenger_no = excluded.passenger_no',
                tickets)
            if pay_row:
                conn.execute('INSERT INTO payments VALUES (?,?,?,?,?,?,?) ON CONFLICT(reference) DO UPDATE SET '
                             'method = excluded.method, status = excluded.status, amount_cents = excluded.amount_cents, '
                             'gateway = excluded.gateway, transaction_id = excluded.transaction_id, '
                             'doc = excluded.doc', pay_row)
        else:
            conn.executemany('INSERT INTO passengers VALUES (?,?,?,?,?,?)', pax)
            conn.executemany(f'INSERT INTO tickets ({", ".join(_TCOLS)}) VALUES ({",".join("?" * len(_TCOLS))})', tickets)  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
            if pay_row:
                conn.execute('INSERT INTO payments VALUES (?,?,?,?,?,?,?)', pay_row)

    def save_history(self, records: List[dict], now: Optional[datetime] = None, actor=None) -> int:
        """Write new and changed bookings in one transaction. Returns how many were written.

        * A NEW booking may be saved by the booking screen without a staff sign-in, but only in its just-booked
          state: every ticket unused, nothing cancelled or refunded, cash not yet received.
        * CHANGING a stored booking this way is a direct data edit and needs a signed-in 'records.edit' permission.
          Normal staff work goes through the dedicated operations (check_in, mark_paid, cancel_ticket...).
        Raises ConcurrentUpdate (nothing written) if another window changed one of them since it was loaded, and
        PermissionDenied (nothing written, denial audited) for an unauthorised change."""
        validate_history(records)
        stamp_ = _now(now or datetime.now())
        written: Dict[str, Tuple[int, str]] = {}
        edits = [r['reference'] for r in records if self._snap.get(r['reference'].upper())
                 and self._snap[r['reference'].upper()][1] != canonical(r)]
        unusual = [r['reference'] for r in records if not self._snap.get(r['reference'].upper()) and not _fresh_booking(r)]
        who = ''
        if edits or unusual:
            who = self.authorize(actor, 'records.edit', now or datetime.now(), target=(edits + unusual)[0])
        elif isinstance(actor, auth.Principal):
            who = actor.username
        with self._write() as c:
            rev_before = int(c.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])
            for rec in records:
                ref = rec['reference']
                canon = canonical(rec)
                snap = self._snap.get(ref.upper())
                if snap and snap[1] == canon:
                    continue
                row = c.execute('SELECT version FROM bookings WHERE reference = ?', (ref,)).fetchone()
                if snap is None:
                    if row is not None:
                        raise ConcurrentUpdate(f'Booking {ref} was created by another window at the same time. '
                                               'Nothing was saved; please try again.', self.path)
                    self._insert_rows(c, rec, stamp_)
                    self._audit_insert(c, 'booking.created', stamp_, ref, who)
                    written[ref.upper()] = (1, canon)
                else:
                    if row is None or row[0] != snap[0]:
                        raise ConcurrentUpdate(f'Booking {ref} was changed by another window or staff member after '
                                               'you opened it. Nothing was saved; the latest data has been reloaded.',
                                               self.path)
                    self._insert_rows(c, rec, stamp_, version=snap[0] + 1, update=True, expect_version=snap[0])
                    self._audit_insert(c, 'booking.edited', stamp_, ref, who, {'note': 'direct edit of a stored booking'})
                    written[ref.upper()] = (snap[0] + 1, canon)
        self._snap.update(written)
        if self.revision_seen == rev_before:                     # nobody else wrote in between: still current
            self.revision_seen = self.revision()
        return len(written)

    # ---------------------------------------------------------------- accounts / staff
    def load_accounts(self) -> List[dict]:
        return [json.loads(d) for (d,) in self.conn.execute('SELECT doc FROM accounts ORDER BY id')]

    def save_accounts(self, accounts: List[dict]) -> None:
        validate_accounts(accounts)
        with self._write() as c:
            for a in accounts:
                c.execute('INSERT INTO accounts (phone, name, doc) VALUES (?,?,?) ON CONFLICT(phone) DO UPDATE SET '
                          'name = excluded.name, doc = excluded.doc WHERE doc != excluded.doc',
                          (a['phone'], a['name'], canonical(a)))

    def load_staff(self) -> List[dict]:
        return [json.loads(d) for (d,) in self.conn.execute('SELECT doc FROM staff ORDER BY id')]

    def save_staff(self, staff: List[dict]) -> None:
        """Not allowed: staff accounts change only through the audited services (bootstrap_admin,
        create_staff_account, set_staff_enabled ...). A whole-list save would bypass every check."""
        raise auth.PermissionDenied('Staff accounts are managed through the staff administration functions.')

    # ---------------------------------------------------------------- booking references and provider confirmations
    def reserve_reference(self, issued_at: datetime) -> str:
        """The next booking reference for that day, reserved for this caller in one transaction BEFORE any payment.
        Two windows booking at the same moment therefore never get the same reference. A reservation that is not used
        (the payment failed) simply leaves a gap in the day's numbering."""
        day = issued_at.strftime('%Y%m%d')
        key = f'refseq:{day}'
        with self._write() as c:
            top = 0
            for (ref,) in c.execute('SELECT reference FROM bookings WHERE reference LIKE ?', (f'CTA-{day}-%',)):
                tail = ref.rsplit('-', 1)[-1]
                if tail.isdigit():
                    top = max(top, int(tail))
            row = c.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
            nxt = max(top, int(row[0]) if row and str(row[0]).isdigit() else 0) + 1
            c.execute('INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(nxt)))
        return core.build_reference(issued_at, nxt)

    def confirm_online_payment(self, reference: str, provider, now: datetime, transaction_id: str = '',
                               environment: Optional[str] = None, source: str = 'callback') -> Tuple[bool, str]:
        """Record a payment provider's confirmation of a pending online payment.

        Trust comes from the provider's own server-side record (payments.confirm_payment re-verifies the transaction,
        amount, currency and reference); the notification itself is never believed, so no staff permission is involved.
        The booking change and its audit row are saved together. Repeated or simultaneous callbacks change nothing
        after the first: the payment is already paid, the transaction can pay only this booking, and a stale second
        writer is refused by the version check."""
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 64:
            return False, 'No booking with that reference.'
        rows = self._read_all(self.conn, reference.strip())
        if not rows:
            return False, 'No booking with that reference.'
        version, rec = rows[0]
        before = canonical(rec.get('payment'))
        ok, msg = payments.confirm_payment(rec, provider, now, StoreLedger(self), transaction_id=transaction_id,
                                           source=source, environment=environment)   # network call: outside the lock
        if canonical(rec.get('payment')) == before:
            return ok, msg                                       # nothing to record (already paid, refused, ...)
        at = _now(now)
        try:
            with self._write() as c:
                self._insert_rows(c, rec, at, version=version + 1, update=True, expect_version=version)
                self._audit_insert(c, 'payment.confirmed', at, rec['reference'],
                                   f"provider:{getattr(provider, 'name', '?')}",
                                   {'status': rec['payment']['status'], 'transaction_id': rec['payment'].get('transaction_id'),
                                    'source': source}, outcome='success' if ok else 'failure',
                                   target=rec['payment'].get('transaction_id', ''))
        except ConcurrentUpdate:
            again = self.get_booking(rec['reference']) or {}
            if (again.get('payment') or {}).get('status') == payments.PAID:
                return False, 'Already paid - nothing changed.'   # another callback recorded it first
            raise
        return ok, msg

    def handle_payment_callback(self, payload, provider, now: datetime, environment: Optional[str] = None):
        """A provider notification: only its booking reference and transaction id are used, to know what to re-check."""
        if not isinstance(payload, dict):
            return False, 'Bad notification.'
        ref, tx = payload.get('reference'), payload.get('transaction_id')
        if not isinstance(ref, str) or (tx is not None and not isinstance(tx, str)) or len(tx or '') > 128:
            return False, 'Bad notification.'
        return self.confirm_online_payment(ref, provider, now, tx or '', environment, 'callback')

    # ---------------------------------------------------------------- authorization
    def _authorize(self, c, actor, permission: str, now: datetime, target: str = '') -> Tuple[Optional[str], str]:
        """(username, '') if ``actor`` is a live signed-in staff member whose CURRENT role has ``permission``;
        otherwise (None, reason) and a 'permission.denied' audit row in this transaction."""
        at = _now(now)
        claimed = actor.username if isinstance(actor, auth.Principal) else ''
        reason = ''
        if not isinstance(actor, auth.Principal):
            reason = 'not signed in'
        else:
            sess = c.execute('SELECT username, expires_at, ended_at FROM sessions WHERE token_hash = ?',
                             (actor.token_hash,)).fetchone()
            if sess is None or sess[2] is not None:
                reason = 'signed out'
            elif sess[1] < at:
                reason = 'session expired'
            elif sess[0].lower() != actor.username.lower():
                reason = 'session does not belong to this user'
            else:
                row = c.execute('SELECT role, disabled FROM staff WHERE username = ?', (actor.username,)).fetchone()
                if row is None:
                    reason = 'account no longer exists'
                elif row[1]:
                    reason = 'account disabled'
                elif not auth.role_allows(row[0], permission):
                    reason = f'role {row[0]} may not do this'
        if reason:
            self._audit_insert(c, 'permission.denied', at, actor=auth.clean_actor(claimed) if claimed else '(not signed in)',
                               detail={'permission': permission, 'reason': reason}, outcome='denied', target=target)
            return None, reason
        return actor.username, ''

    def _deny(self, permission: str, reason: str) -> auth.PermissionDenied:
        text = auth.PERMISSIONS.get(permission, permission).lower()
        if reason in ('not signed in', 'signed out', 'session expired', 'session does not belong to this user'):
            return auth.PermissionDenied(f'Please sign in as staff again ({reason}).')
        return auth.PermissionDenied(f'You are not allowed to {text[0].lower() + text[1:]} ({reason}).')

    def authorize(self, actor, permission: str, now: datetime, target: str = '') -> str:
        """Check a permission for an operation that does not itself write to the database (e.g. a CSV export).
        Returns the username, or raises PermissionDenied (the denial is committed to the audit log)."""
        with self._write() as c:
            who, why = self._authorize(c, actor, permission, now, target)
            if who:
                self._no_bump = True
        if not who:
            raise self._deny(permission, why)
        return who

    def has_permission(self, actor, permission: str, now: datetime) -> bool:
        """Read-only question for screens (show or hide a button). Never a substitute for ``authorize``."""
        if not isinstance(actor, auth.Principal):
            return False
        at = _now(now)
        row = self.conn.execute('SELECT s.expires_at, s.ended_at, f.role, f.disabled FROM sessions s JOIN staff f '
                                'ON f.username = s.username WHERE s.token_hash = ?', (actor.token_hash,)).fetchone()
        return bool(row and row[1] is None and row[0] >= at and not row[3] and auth.role_allows(row[2], permission))

    # ---------------------------------------------------------------- staff sign-in and accounts
    def staff_count(self) -> int:
        return self.conn.execute('SELECT COUNT(*) FROM staff').fetchone()[0]

    def _start_session(self, c, username: str, role: str, now: datetime, device: str) -> auth.Principal:
        token = auth.new_token()
        c.execute('INSERT INTO sessions (token_hash, username, created_at, expires_at, device) VALUES (?,?,?,?,?)',
                  (auth.token_hash(token), username, _now(now), auth.session_expiry(now), (device or '')[:40] or None))
        return auth.Principal(username, role, token)

    def bootstrap_admin(self, username: str, password: str, now: datetime, device: str = '') -> auth.Principal:
        """Create the very first administrator. Refused (and audited) once any staff account exists.
        The password is validated and hashed before the transaction, so no lock is held while hashing."""
        at = _now(now)
        rec = acct.create_staff([], username, password, now, role='admin')   # ValueError: bad name/password
        with self._write() as c:
            if c.execute('SELECT COUNT(*) FROM staff').fetchone()[0]:
                self._audit_insert(c, 'staff.bootstrap', at, actor=auth.clean_actor(username), outcome='denied',
                                   detail={'reason': 'staff accounts already exist'})
                denied = True
            else:
                denied = False
                c.execute('INSERT INTO staff (username, role, doc) VALUES (?,?,?)', (rec['username'], 'admin', canonical(rec)))
                principal = self._start_session(c, rec['username'], 'admin', now, device)
                self._audit_insert(c, 'staff.bootstrap', at, actor=rec['username'], target=rec['username'],
                                   detail={'role': 'admin', 'device': device})
                self._audit_insert(c, 'auth.login', at, actor=rec['username'], detail={'device': device})
        if denied:
            raise auth.PermissionDenied('An administrator already exists. Ask them to create your account.')
        return principal

    def staff_login(self, username: str, password: str, now: datetime, device: str = '') -> auth.Principal:
        """Check a password. Five wrong passwords in a row lock that account for 30 s, for every window and computer.
        Every attempt is audited; the password never is.

        The slow part (PBKDF2, about 0.2 s) runs with NO database lock held, so a sign-in never pauses bookings or
        check-ins in other windows. The lockout counter and the account are then re-read inside a short write
        transaction; if the password changed in the meantime, the attempt is refused and has to be repeated."""
        at = _now(now)
        key = str(username or '').strip().lower()[:64]   # usernames are at most 30 characters; never store more
        name = auth.clean_actor(username)
        lock_q = 'SELECT failures, locked_until FROM login_attempts WHERE username_key = ?'
        staff_q = 'SELECT username, role, doc, disabled FROM staff WHERE username = ?'
        lock = self.conn.execute(lock_q, (key,)).fetchone()
        row = None if lock and lock[1] and lock[1] > at else self.conn.execute(staff_q, (key,)).fetchone()
        good, new_hash = False, None
        if not (lock and lock[1] and lock[1] > at):
            stored = json.loads(row[2])['password'] if row else acct.DUMMY_HASH()
            good = acct.check_password(password or '', stored)           # outside any transaction
            if row and good and not row[3] and acct.needs_rehash(stored):
                new_hash = acct.hash_password(password)                  # upgrade old hashes, still outside
        error: Optional[str] = None
        with self._write() as c:
            lock = c.execute(lock_q, (key,)).fetchone()
            if lock and lock[1] and lock[1] > at:
                wait = int((datetime.strptime(lock[1], '%Y-%m-%d %H:%M:%S') - now).total_seconds()) + 1
                self._audit_insert(c, 'auth.login', at, actor=name, outcome='locked', detail={'device': device})
                error = f'Too many wrong passwords. Try again in {wait} s.'
            else:
                current = c.execute(staff_q, (key,)).fetchone()
                if row is not None and (current is None or current[2] != row[2]):
                    self._audit_insert(c, 'auth.login', at, actor=name, outcome='failure',
                                       detail={'device': device, 'reason': 'account changed during sign-in'})
                    error = 'The account was changed while you were signing in. Please sign in again.'
                elif current and good and not current[3]:
                    c.execute('DELETE FROM login_attempts WHERE username_key = ?', (key,))
                    if new_hash is not None:
                        doc = json.loads(current[2])
                        doc['password'] = new_hash
                        c.execute('UPDATE staff SET doc = ? WHERE username = ?', (canonical(doc), current[0]))
                    principal = self._start_session(c, current[0], current[1], now, device)
                    self._audit_insert(c, 'auth.login', at, actor=current[0],
                                       detail={'device': device, 'role': current[1]})
                elif current and good and current[3]:
                    self._audit_insert(c, 'auth.login', at, actor=current[0], outcome='disabled',
                                       detail={'device': device})
                    error = 'This staff account is disabled. Ask an administrator.'
                else:
                    failures = (lock[0] if lock else 0) + 1
                    locked = failures >= auth.MAX_FAILURES
                    c.execute('INSERT INTO login_attempts (username_key, failures, locked_until) VALUES (?,?,?) '
                              'ON CONFLICT(username_key) DO UPDATE SET failures = excluded.failures, '
                              'locked_until = excluded.locked_until',
                              (key, 0 if locked else failures,
                               _now(now + timedelta(seconds=auth.LOCK_SECONDS)) if locked else None))
                    self._audit_insert(c, 'auth.login', at, actor=name, outcome='failure',
                                       detail={'device': device, 'locked_now': locked})
                    error = ('Too many wrong passwords. Try again in 30 s.' if locked
                             else 'Wrong username or password.')
        if error:
            raise auth.AuthError(error)
        return principal

    def staff_logout(self, actor, now: datetime) -> None:
        if not isinstance(actor, auth.Principal):
            return
        with self._write() as c:
            cur = c.execute('UPDATE sessions SET ended_at = ? WHERE token_hash = ? AND ended_at IS NULL',
                            (_now(now), actor.token_hash))
            if cur.rowcount:
                self._audit_insert(c, 'auth.logout', _now(now), actor=actor.username)
            else:
                self._no_bump = True

    def _enabled_admins(self, c) -> int:
        return c.execute("SELECT COUNT(*) FROM staff WHERE role = 'admin' AND disabled = 0").fetchone()[0]

    def _staff_change(self, actor, now: datetime, target: str, kind: str, fn, detail=None):
        """Run ``fn(c, row, who) -> (ok, message)`` for one staff account with 'staff.manage' permission, audited."""
        at = _now(now)
        result: Optional[Tuple[bool, str]] = None
        with self._write() as c:
            who, why = self._authorize(c, actor, 'staff.manage', now, target)
            if who:
                row = c.execute('SELECT username, role, doc, disabled FROM staff WHERE username = ?',
                                (target,)).fetchone()
                ok, msg = fn(c, row, who) if row else (False, 'No staff account with that username.')
                self._audit_insert(c, kind, at, actor=who, target=row[0] if row else auth.clean_actor(target),
                                   outcome='success' if ok else 'failure', detail=dict(detail or {}, message=msg[:120]))
                result = (ok, msg)
        if not who or result is None:
            raise self._deny('staff.manage', why)
        if not result[0]:
            raise ValueError(result[1])
        return result[1]

    def create_staff_account(self, actor, username: str, password: str, role: str, now: datetime) -> dict:
        at = _now(now)
        self.authorize(actor, 'staff.manage', now, auth.clean_actor(username))   # refused (and audited) before hashing
        error, rec = '', None
        if role not in auth.ROLE_PERMISSIONS:
            error = f'Unknown role "{role}".'
        elif role not in auth.ASSIGNABLE_ROLES:
            error = (f'The role "{role}" is part of the proposed permission matrix and cannot be used until it '
                     'is approved.')
        else:
            existing = [json.loads(d) for (d,) in self.conn.execute('SELECT doc FROM staff')]
            try:
                rec = acct.create_staff(existing, username, password, now, role=role)   # hashes, no lock held
            except ValueError as exc:
                error = str(exc)
        with self._write() as c:
            who, why = self._authorize(c, actor, 'staff.manage', now, auth.clean_actor(username))
            if who:
                if rec is not None and not error and c.execute('SELECT 1 FROM staff WHERE username = ?',
                                                               (rec['username'],)).fetchone():
                    error = 'That username is taken.'             # created by someone else a moment ago
                if rec is not None and not error:
                    c.execute('INSERT INTO staff (username, role, doc) VALUES (?,?,?)',
                              (rec['username'], role, canonical(rec)))
                self._audit_insert(c, 'staff.created', at, actor=who, target=auth.clean_actor(username),
                                   outcome='failure' if error else 'success', detail={'role': role, 'message': error})
        if not who:
            raise self._deny('staff.manage', why)
        if error or rec is None:
            raise ValueError(error or 'The account could not be created.')
        return {'username': rec['username'], 'role': role, 'created_at': rec['created_at']}

    def set_staff_enabled(self, actor, username: str, enabled: bool, now: datetime) -> str:
        def change(c, row, who):
            if row[0].lower() == who.lower() and not enabled:
                return False, 'You cannot disable your own account.'
            if not enabled and row[1] == 'admin' and not row[3] and self._enabled_admins(c) <= 1:
                return False, 'This is the last active administrator; it cannot be disabled.'
            c.execute('UPDATE staff SET disabled = ? WHERE username = ?', (0 if enabled else 1, row[0]))
            if not enabled:                                       # signed-in windows lose access at once
                c.execute('UPDATE sessions SET ended_at = ? WHERE username = ? AND ended_at IS NULL', (_now(now), row[0]))
            return True, f"{row[0]} {'enabled' if enabled else 'disabled'}."
        return self._staff_change(actor, now, username, 'staff.enabled' if enabled else 'staff.disabled', change)

    def set_staff_role(self, actor, username: str, role: str, now: datetime) -> str:
        def change(c, row, who):
            if role not in auth.ASSIGNABLE_ROLES:
                return False, f'The role "{role}" cannot be assigned (unknown, or awaiting approval).'
            if row[1] == 'admin' and role != 'admin' and not row[3] and self._enabled_admins(c) <= 1:
                return False, 'This is the last active administrator; its role cannot be changed.'
            doc = json.loads(row[2])
            doc['role'] = role
            c.execute('UPDATE staff SET role = ?, doc = ? WHERE username = ?', (role, canonical(doc), row[0]))
            return True, f'{row[0]} is now {role}.'
        return self._staff_change(actor, now, username, 'staff.role_changed', change, {'role': role})

    def reset_staff_password(self, actor, username: str, new_password: str, now: datetime) -> str:
        self.authorize(actor, 'staff.manage', now, auth.clean_actor(username))   # refused (and audited) before hashing
        err = acct.validate_password(new_password)
        hashed = None if err else acct.hash_password(new_password)            # no lock held while hashing

        def change(c, row, who):
            if err:
                return False, err
            doc = json.loads(row[2])
            doc['password'] = hashed
            c.execute('UPDATE staff SET doc = ? WHERE username = ?', (canonical(doc), row[0]))
            c.execute('UPDATE sessions SET ended_at = ? WHERE username = ? AND ended_at IS NULL', (_now(now), row[0]))
            c.execute('DELETE FROM login_attempts WHERE username_key = ?', (row[0].lower(),))
            return True, f'Password reset for {row[0]}; their open sessions were ended.'
        return self._staff_change(actor, now, username, 'staff.password_reset', change)

    def list_staff(self, actor, now: datetime) -> List[dict]:
        self.authorize(actor, 'staff.manage', now)
        return [{'username': u, 'role': r, 'disabled': bool(d)} for u, r, d in
                self.conn.execute('SELECT username, role, disabled FROM staff ORDER BY id')]

    # ---------------------------------------------------------------- reading the audit log
    AUDIT_COLUMNS = ('event_id', 'at', 'kind', 'outcome', 'actor', 'target', 'reference', 'detail')

    def read_audit(self, actor, now: datetime, limit: int = 500) -> List[dict]:
        """Newest first. Needs 'audit.read'; reading is itself audited."""
        at = _now(now)
        with self._write() as c:
            who, why = self._authorize(c, actor, 'audit.read', now)
            if who:
                rows = c.execute('SELECT id, at, kind, outcome, actor, target, reference, detail FROM audit '
                                 'ORDER BY id DESC LIMIT ?', (max(1, min(limit, 10_000)),)).fetchall()
                self._audit_insert(c, 'audit.read', at, actor=who, detail={'rows': len(rows)})
        if not who:
            raise self._deny('audit.read', why)
        return [dict(zip(self.AUDIT_COLUMNS, (f'AUD-{r[0]:08d}',) + tuple(r[1:]))) for r in rows]

    def export_audit_csv(self, actor, path: Path, now: datetime) -> int:
        import csv
        import io
        from cta_storage import atomic_write_bytes
        who = self.authorize(actor, 'audit.export', now, target=Path(path).name)
        rows = [dict(zip(self.AUDIT_COLUMNS, (f'AUD-{r[0]:08d}',) + tuple(r[1:]))) for r in self.conn.execute(
            'SELECT id, at, kind, outcome, actor, target, reference, detail FROM audit ORDER BY id')]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.AUDIT_COLUMNS)
        w.writeheader()
        w.writerows(rows)
        atomic_write_bytes(Path(path), buf.getvalue().encode('utf-8-sig'))
        self.audit('audit.exported', _now(now), actor=who, target=Path(path).name, detail={'rows': len(rows)})
        return len(rows)

    # ---------------------------------------------------------------- ticket entry (atomic) and life cycle
    def check_in(self, presented: str, *, signer, now: datetime, operator, device: str = '', station: str = '',
                 environment: Optional[str] = None) -> tickets.CheckResult:
        """Verify a ticket and mark it used in ONE transaction.

        ``presented`` is a scanned QR token or a typed ticket number; both are resolved to the stored ticket record
        and judged by the same function (cta_tickets.check_ticket). The transaction takes the database write lock
        first (BEGIN IMMEDIATE), so two simultaneous scans are processed one after the other and the second sees
        the ticket already used. Every attempt, accepted or refused, is written to the scan log in the same
        transaction: time, outcome code, operator, device, station - and no passenger data.
        Raises StorageError (nothing recorded) if the database cannot be written."""
        at = _now(now)
        presented_ = tickets.parse_presented(presented, signer)
        with self._write() as c:
            who, why = self._authorize(c, operator, 'ticket.check_in', now, target=presented_.log_ref)
            if not who:
                res = tickets.CheckResult(False, tickets.NOT_AUTHORIZED,
                                          str(self._deny('ticket.check_in', why)), method=presented_.method)
            elif presented_.code:
                res = tickets.refusal(presented_)
            else:
                res = self._check_in_locked(c, presented_, now, who, environment, station, at)
            c.execute('INSERT INTO scans (at, ticket_no, staff, ok, result, ticket_id, leg, method, device, station) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (at, res.ticket_no or presented_.log_ref,
                       who or (auth.clean_actor(operator.username) if isinstance(operator, auth.Principal) else '(not signed in)'),
                       int(res.ok), res.code,
                       res.ticket_id or presented_.ticket_id or None, res.leg or None, res.method,
                       (device or '')[:40] or None, (station or '')[:40] or None))
        res.record = None                                        # callers get the outcome, not the live record
        return res

    def _booking_for(self, c, presented: 'tickets.Presented') -> Optional[str]:
        """The booking reference that holds the presented ticket (from the database, inside the transaction)."""
        if presented.ticket_id:
            row = c.execute('SELECT reference FROM tickets WHERE ticket_id = ?', (presented.ticket_id,)).fetchone()
        else:
            row = c.execute('SELECT reference FROM tickets WHERE ticket_no = ?', (presented.ticket_no,)).fetchone()
            if row is None:                                       # a single-passenger booking may be entered by reference
                row = c.execute('SELECT reference FROM bookings WHERE reference = ?', (presented.ticket_no,)).fetchone()
        return row[0] if row else None

    def _check_in_locked(self, c, presented, now: datetime, operator: str, environment: Optional[str], station: str,
                         at: str) -> tickets.CheckResult:
        ref = self._booking_for(c, presented)
        rows = self._read_all(c, ref) if ref else []
        found = tickets.locate([rows[0][1]], presented) if rows else None
        if not found:
            return tickets.unknown_result(presented)
        version, rec = rows[0]
        _rec, p, leg = found
        res = tickets.check_ticket(rec, p, leg, now, operator=operator, environment=environment, station=station,
                                   method=presented.method)
        if res.ok:                                               # rec now carries the new use; persist it, same transaction
            self._insert_rows(c, rec, at, version=version + 1, update=True, expect_version=version)
        return res

    def _mutate(self, *, ticket_no: str = '', reference: str = '', fn, now: datetime, actor, permission: str,
                kind: str, detail: Optional[dict] = None, after=None) -> Tuple[bool, str]:
        """Authorize, locate one booking, apply ``fn(record, username) -> (ok, message)`` to the fresh stored copy and
        save it - all in one transaction together with its audit entry (so neither can exist without the other).
        A refusal by the business rules is audited as a failure; a missing permission raises PermissionDenied."""
        at = _now(now)
        target = ticket_no or reference
        result: Tuple[bool, str] = (False, '')
        with self._write() as c:
            who, why = self._authorize(c, actor, permission, now, target)
            if who:
                if ticket_no:
                    ref = self._booking_for(c, tickets.Presented('manual', '', ticket_no=ticket_no))
                else:
                    ref = reference
                rows = self._read_all(c, ref) if ref else []
                if not rows:
                    result = (False, 'No such ticket or booking.')
                else:
                    version, rec = rows[0]
                    result = fn(rec, who)
                    if result[0]:
                        self._insert_rows(c, rec, at, version=version + 1, update=True, expect_version=version)
                self._audit_insert(c, kind, at, rec['reference'] if rows else '', who, detail if detail is not None else {},
                                   outcome='success' if result[0] else 'failure', target=target)
                if after is not None and result[0]:           # extra rows that must exist together with this change
                    after(c, who)
        if not who:
            raise self._deny(permission, why)
        return result

    def cancel_ticket(self, ticket_no: str, actor, now: datetime, reason: str = '') -> Tuple[bool, str]:
        no = ''.join(ticket_no.split()).upper()
        return self._mutate(ticket_no=no, fn=lambda r, who: tickets.cancel_ticket(r, no, who, now, reason), now=now,
                            actor=actor, permission='ticket.cancel', kind='ticket.cancelled',
                            detail={'reason': tickets._clean_reason(reason)})

    def cancel_booking(self, reference: str, actor, now: datetime, reason: str = '') -> Tuple[bool, str]:
        return self._mutate(reference=reference, fn=lambda r, who: tickets.cancel_booking(r, who, now, reason), now=now,
                            actor=actor, permission='ticket.cancel', kind='booking.cancelled',
                            detail={'reason': tickets._clean_reason(reason)})

    def record_refund(self, ticket_no: str, actor, now: datetime, ref: str = '') -> Tuple[bool, str]:
        no = ''.join(ticket_no.split()).upper()
        return self._mutate(ticket_no=no, fn=lambda r, who: tickets.record_refund(r, no, who, now, ref), now=now,
                            actor=actor, permission='ticket.refund', kind='ticket.refunded')

    def allow_reentry(self, ticket_no: str, actor, now: datetime, reason: str) -> Tuple[bool, str]:
        no = ''.join(ticket_no.split()).upper()
        return self._mutate(ticket_no=no, fn=lambda r, who: tickets.allow_reentry(r, no, who, now, reason), now=now,
                            actor=actor, permission='ticket.reentry', kind='ticket.reentry',
                            detail={'reason': tickets._clean_reason(reason)})

    def correct_name(self, reference: str, passenger_no: int, new_name: str, actor,
                     now: datetime) -> Tuple[bool, str]:
        return self._mutate(reference=reference,
                            fn=lambda r, who: tickets.correct_name(r, passenger_no, new_name, who, now),
                            now=now, actor=actor, permission='ticket.correct_name', kind='ticket.name_corrected',
                            detail={'passenger_no': passenger_no})

    def mark_paid(self, ticket_or_reference: str, actor, now: datetime) -> Tuple[bool, str]:
        """Counter cash received: the only way a payment status is changed by staff. Audited with the amount."""
        no = ''.join((ticket_or_reference or '').split()).upper()

        info: dict = {}

        def apply(rec, who):
            before = core.payment_of(rec).get('status')
            ok, msg = core.mark_paid(rec, now, who)
            if ok:                                                # recorded in the SAME audit row, same transaction
                info.update({'from': before, 'to': rec['payment']['status'],
                             'amount_cents': rec['payment'].get('amount_cents')})
            return ok, msg
        return self._mutate(ticket_no=no, fn=apply, now=now, actor=actor, permission='payment.mark_paid',
                            kind='payment.status_changed', detail=info)

    # ---------------------------------------------------------------- passenger messages and refund requests
    @staticmethod
    def _clean_message(text, limit: int = MAX_MESSAGE) -> str:
        body = ''.join(ch for ch in str(text or '') if ch == '\n' or ch.isprintable()).strip()
        if not body:
            raise ValueError('Please write a message first.')
        if len(body) > limit:
            raise ValueError(f'Please keep it under {limit} characters (it is {len(body)}).')
        return body

    def open_case(self, kind: str, text: str, now: datetime, *, topic: str = '', account_phone: str = '',
                  reference: str = '') -> int:
        """A passenger's problem report or refund request. Returns the case number. A refund request needs the
        passenger's signed-in phone number and one of their own bookings that still has an unused journey; the
        decision (and any refund policy) is staff's - nothing is cancelled or paid here."""
        if kind not in SUPPORT_KINDS:
            raise ValueError('Unknown kind of message.')
        body = self._clean_message(text)
        at = _now(now)
        if kind == 'problem':
            if topic not in SUPPORT_TOPICS:
                raise ValueError('Choose what the problem is about.')
            ref = ''
        else:
            topic, ref = 'Refund request', (reference or '').strip().upper()
        with self._write() as c:
            if kind == 'refund_request':
                if not account_phone:
                    raise ValueError('Please sign in to ask for a refund.')
                rows = self._read_all(c, ref) if ref else []
                if not rows or rows[0][1].get('account_phone') != account_phone:
                    raise ValueError('That booking was not found on your account.')
                booking = rows[0][1]
                if core.payment_of(booking).get('status') in payments.UNCONFIRMED:
                    raise ValueError('The payment for this booking is not confirmed yet, so it cannot be refunded yet.')
                if tickets.unused_journeys(booking)[0] == 0:
                    raise ValueError('Every journey in this booking is already used or cancelled.')
                if c.execute("SELECT 1 FROM support_cases WHERE kind = 'refund_request' AND reference = ? "
                             "AND status = 'open'", (ref,)).fetchone():
                    raise ValueError('A refund request for this booking is already waiting for staff.')
            cur = c.execute('INSERT INTO support_cases (kind, created_at, updated_at, account_phone, reference, topic, '
                            "status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
                            (kind, at, at, account_phone or None, ref or None, topic))
            case_id = cur.lastrowid or 0                           # always set after an INSERT
            c.execute('INSERT INTO support_messages (case_id, at, author, from_staff, body) VALUES (?, ?, ?, 0, ?)',
                      (case_id, at, 'passenger', body))
            self._audit_insert(c, 'support.opened', at, ref, 'passenger', {'case': case_id, 'kind': kind}, target=f'case {case_id}')
        return case_id

    def _case_dicts(self, where: str, args: tuple) -> List[dict]:
        try:
            rows = self.conn.execute('SELECT id, kind, created_at, updated_at, account_phone, reference, topic, status, '  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                                     f'handled_by FROM support_cases {where} '
                                     "ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END, updated_at DESC, id DESC "
                                     'LIMIT 500', args).fetchall()
            cols = ('id', 'kind', 'created_at', 'updated_at', 'account_phone', 'reference', 'topic', 'status', 'handled_by')
            cases = [dict(zip(cols, r)) for r in rows]
            for case in cases:
                case['messages'] = [
                    {'at': m[0], 'author': m[1], 'from_staff': bool(m[2]), 'body': m[3]} for m in self.conn.execute(
                        'SELECT at, author, from_staff, body FROM support_messages WHERE case_id = ? ORDER BY id',
                        (case['id'],))]
            return cases
        except sqlite3.OperationalError:                      # a read-only older database that has no such tables yet
            return []
        except sqlite3.Error as exc:
            raise _map_error(exc, self.path) from None

    def list_cases(self, actor, now: datetime, status: Optional[str] = None) -> List[dict]:
        """Staff inbox (newest activity first, open ones on top). Needs 'support.reply'."""
        if not self.has_permission(actor, 'support.reply', now):
            self.authorize(actor, 'support.reply', now, target='passenger messages')      # raises, and audits the denial
        if status is None:
            return self._case_dicts('', ())
        return self._case_dicts('WHERE status = ?', (status,))

    def cases_for_account(self, phone: str) -> List[dict]:
        """What a signed-in passenger sees: their own messages and the staff replies."""
        if not phone:
            return []
        return self._case_dicts('WHERE account_phone = ?', (phone,))

    def reply_case(self, case_id: int, actor, text: str, now: datetime) -> Tuple[bool, str]:
        body = self._clean_message(text)
        at = _now(now)
        with self._write() as c:
            who, why = self._authorize(c, actor, 'support.reply', now, f'case {case_id}')
            if who:
                row = c.execute('SELECT kind, status FROM support_cases WHERE id = ?', (case_id,)).fetchone()
                if row is None:
                    result = (False, 'No such message.')
                else:
                    c.execute('INSERT INTO support_messages (case_id, at, author, from_staff, body) VALUES (?, ?, ?, 1, ?)',
                              (case_id, at, who, body))
                    if row[0] == 'problem':
                        c.execute("UPDATE support_cases SET status = 'answered', handled_by = ?, updated_at = ? WHERE id = ?",
                                  (who, at, case_id))
                    else:
                        c.execute('UPDATE support_cases SET updated_at = ? WHERE id = ?', (at, case_id))
                    result = (True, 'Reply sent.')
                self._audit_insert(c, 'support.replied', at, '', who, {'case': case_id},
                                   outcome='success' if result[0] else 'failure', target=f'case {case_id}')
        if not who:
            raise self._deny('support.reply', why)
        return result

    def _finish_case(self, c, case_id: int, status: str, who: str, at: str, body: str) -> None:
        c.execute('UPDATE support_cases SET status = ?, handled_by = ?, updated_at = ? WHERE id = ?',
                  (status, who, at, case_id))
        c.execute('INSERT INTO support_messages (case_id, at, author, from_staff, body) VALUES (?, ?, ?, 1, ?)',
                  (case_id, at, who, body))

    def decide_refund_request(self, case_id: int, actor, approve: bool, note: str, now: datetime) -> Tuple[bool, str]:
        """Staff decide a passenger's refund request. APPROVE cancels every unused journey of that booking (the same
        cancellation as the Ticket actions menu, so a paid booking then shows 'Refund to pay out'); staff still record
        the payout afterwards. DECLINE changes no ticket. Either way the passenger is told. The app has no refund
        policy of its own (who may be refunded, fees, deadlines): staff decide each request."""
        self.authorize(actor, 'support.reply', now, target=f'case {case_id}')
        note = ' '.join(str(note or '').split())[:300]
        at = _now(now)
        row = self.conn.execute('SELECT kind, status, reference FROM support_cases WHERE id = ?', (case_id,)).fetchone()
        if row is None or row[0] != 'refund_request':
            return False, 'No such refund request.'
        if row[1] != 'open':
            return False, 'This request has already been dealt with.'
        ref = row[2]
        if not approve:
            body = 'Your refund request was declined.' + (f' {note}' if note else '')
            with self._write() as c:
                who, why = self._authorize(c, actor, 'support.reply', now, f'case {case_id}')
                if who:
                    if c.execute("SELECT status FROM support_cases WHERE id = ?", (case_id,)).fetchone()[0] != 'open':
                        return False, 'This request has already been dealt with.'
                    self._finish_case(c, case_id, 'declined', who, at, body)
                    self._audit_insert(c, 'refund_request.declined', at, ref, who, {'case': case_id}, target=f'case {case_id}')
            if not who:
                raise self._deny('support.reply', why)
            return True, 'Refund request declined; the passenger will see your message.'
        said: List[str] = []

        def cancel(rec, who):
            ok, msg = tickets.cancel_booking(rec, who, now, 'Passenger refund request')
            said.append(msg)
            return ok, msg

        def close(c, who):
            if c.execute('SELECT status FROM support_cases WHERE id = ?', (case_id,)).fetchone()[0] != 'open':
                raise ConcurrentUpdate('Another staff member dealt with this request first.', self.path)
            body = 'Your refund request was approved. ' + said[-1] + ' Staff will pay the refund out and tell you.'
            self._finish_case(c, case_id, 'approved', who, at, body + (f' {note}' if note else ''))
        return self._mutate(reference=ref, fn=cancel, now=now, actor=actor, permission='ticket.cancel',
                            kind='refund_request.approved', detail={'case': case_id}, after=close)

    def backfill_ticket_ids(self, now: Optional[datetime] = None) -> int:
        """Give every ticket that has no id one (tickets from before version 2.4). Bookings touched get a new version,
        so a stale window cannot save over them. Returns how many ids were assigned."""
        if self.read_only:
            return 0
        missing = self.conn.execute('SELECT ticket_no, reference FROM tickets WHERE ticket_id IS NULL').fetchall()
        if not missing:
            return 0
        at = _now(now)
        with self._write() as c:
            missing = c.execute('SELECT ticket_no, reference FROM tickets WHERE ticket_id IS NULL').fetchall()
            for tno, _ref in missing:
                c.execute('UPDATE tickets SET ticket_id = ? WHERE ticket_no = ? AND ticket_id IS NULL',
                          (core.new_ticket_id(), tno))
            for ref in {r for _t, r in missing}:
                c.execute('UPDATE bookings SET version = version + 1, updated_at = ? WHERE reference = ?', (at, ref))
            self._audit_insert(c, 'tickets.ids_assigned', at, actor='system', detail={'count': len(missing)})
        return len(missing)

    def record_scan(self, ticket_no: str, at: str, staff: str, ok: bool, result: str) -> None:
        """Low-level scan-log insert (check_in does this for you). ``result`` must be an outcome code, not a message."""
        with self._write() as c:
            c.execute('INSERT INTO scans (at, ticket_no, staff, ok, result) VALUES (?,?,?,?,?)',
                      (at, ticket_no, staff, int(bool(ok)), result[:40]))

    def _audit_insert(self, c, kind: str, at: str, reference: str = '', actor: str = '', detail=None,
                      source_key: Optional[str] = None, outcome: str = 'success', target: str = '') -> Optional[str]:
        """THE way to write an audit row (inside the caller's transaction). Secrets are stripped from ``detail``,
        the row is chained to the previous one, and its reference id (AUD-00000042) is returned."""
        if source_key and c.execute('SELECT 1 FROM audit WHERE source_key = ?', (source_key,)).fetchone():
            return None
        row = {'at': at, 'kind': kind, 'reference': reference or '', 'actor': (actor or '')[:60],
               'target': (target or '')[:80], 'outcome': outcome,
               'detail': canonical(auth.scrub(detail)) if detail else None, 'source_key': source_key}
        last = c.execute('SELECT chain FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        chain = _chain(last[0] or '' if last else '', row)
        cur = c.execute(f'INSERT INTO audit ({", ".join(_AUDIT_HASHED)}, chain) VALUES (?,?,?,?,?,?,?,?,?)',  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                        tuple(row[k] for k in _AUDIT_HASHED) + (chain,))
        return f'AUD-{cur.lastrowid:08d}'

    def audit(self, kind: str, at: str, reference: str = '', actor: str = '', detail: Optional[dict] = None,
              source_key: Optional[str] = None, outcome: str = 'success', target: str = '') -> Optional[str]:
        with self._write() as c:
            return self._audit_insert(c, kind, at, reference, actor, detail, source_key, outcome, target)

    def audit_problems(self) -> List[str]:
        """Rows whose hash chain does not check out (changed, removed or inserted out of turn)."""
        prev, out = '', []
        cols = ', '.join(_AUDIT_HASHED)
        for row in self.conn.execute(f'SELECT id, {cols}, chain FROM audit ORDER BY id'):  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
            expected = _chain(prev, dict(zip(_AUDIT_HASHED, row[1:-1])))
            if row[-1] != expected:
                out.append(f'Audit record AUD-{row[0]:08d} does not match the audit chain (changed or a record before '
                           'it was removed).')
                break
            prev = row[-1]
        return out

    def scans(self) -> List[dict]:
        cols = ('at', 'ticket_no', 'staff', 'ok', 'result', 'ticket_id', 'leg', 'method', 'device', 'station')
        return [dict(zip(cols, r)) for r in self.conn.execute(f'SELECT {", ".join(cols)} FROM scans ORDER BY id')]  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608

    def counts(self) -> Dict[str, int]:
        return {t: self.conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES if t != 'meta'}  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608

    # ---------------------------------------------------------------- checks and backups
    def verify(self) -> List[str]:
        """Problems found (empty list = healthy)."""
        problems = []
        if self.conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            problems.append('SQLite integrity check failed.')
        if self.schema_version >= 3:
            problems += self.audit_problems()
        if self.conn.execute('PRAGMA foreign_key_check').fetchall():
            problems.append('Some tickets, passengers or payments point at missing bookings.')
        for ref, status, method, amount, doc in self.conn.execute(
                'SELECT reference, status, method, amount_cents, doc FROM payments'):
            d = json.loads(doc)
            if (d.get('status'), d.get('method'), d.get('amount_cents')) != (status, method, amount):
                problems.append(f'Payment of booking {ref}: stored columns disagree with the payment record.')
        if self.schema_version < SCHEMA_VERSION:                 # an older backup: the full check runs after the upgrade
            return problems
        records = [rec for _v, rec in self._read_all(self.conn)]
        try:
            validate_history(records)
        except DataInvalid as exc:
            problems.append(f'Booking data is invalid: {exc.message}')
            return problems
        for rec in records:                                      # reported, never changed automatically
            for why in fares.fare_problems(rec):
                problems.append(f"Booking {rec.get('reference')}: {why}.")
        return problems

    def backup(self, label: str = 'manual', dest: Optional[Path] = None, now: Optional[datetime] = None) -> Path:
        """Consistent online copy (SQLite backup API), checked before it is trusted."""
        folder = Path(dest) if dest else backup_dir(self.path.parent)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f'cta-{stamp(now)}-{label}.db'
        try:
            out = sqlite3.connect(str(target))
            try:
                self.conn.backup(out)
            finally:
                out.close()
            copy = Store(target, read_only=True, allow_old=True)
            try:
                same = copy.counts() == self.counts()
            finally:
                copy.close()
            if not same:
                raise sqlite3.DatabaseError('backup differs')
        except (sqlite3.Error, StorageError):
            try:
                target.unlink()
            except OSError:
                pass
            raise DataWriteError('Could not make a backup of the database. Nothing was changed.', self.path)
        if label == 'auto':
            prune_backups(folder, 'cta', '.db', 'auto')
        return target

    def prune_sessions(self, now: Optional[datetime] = None, keep_days: int = 30) -> int:
        """Delete sign-in sessions that expired or were signed out more than ``keep_days`` ago.

        Sessions are only ever added, so on a busy gate the table grows for ever and every permission check has more
        rows to look past. Nothing auditable is lost: who signed in and out is in the audit log, which is never
        pruned. Returns how many rows were removed."""
        if self.read_only:
            return 0
        cut = _now((now or datetime.now()) - timedelta(days=max(1, keep_days)))
        with self._write() as c:
            cur = c.execute('DELETE FROM sessions WHERE expires_at < ? AND (ended_at IS NULL OR ended_at < ?)',
                            (cut, cut))
            if not cur.rowcount:
                self._no_bump = True                 # nothing changed: other windows need not reload
            return cur.rowcount

    def auto_backup(self, now: Optional[datetime] = None) -> Optional[Path]:
        """At most one routine backup per day."""
        if self.read_only:
            return None
        now = now or datetime.now()
        latest = [p for p in list_backups(backup_dir(self.path.parent), 'cta', '.db') if p.name.endswith('-auto.db')]
        if latest and now - datetime.fromtimestamp(latest[0].stat().st_mtime) < AUTO_BACKUP_EVERY:
            return None
        return self.backup('auto', now=now)

    # ---------------------------------------------------------------- opening
    @classmethod
    def open(cls, folder: Path, now: Optional[datetime] = None, create: bool = True) -> 'Store':
        folder = Path(folder)
        db = folder / DB_NAME
        if db.exists():
            writable = os.access(db, os.W_OK) and os.access(folder, os.W_OK)
            store = cls(db, read_only=not writable)
            if writable:
                store.backfill_ticket_ids(now)
            return store
        if not create:
            raise DataMissing(f'There is no database ({DB_NAME}) in this folder yet.', db)
        if list_backups(backup_dir(folder), 'cta', '.db'):
            raise DataMissing(f'The database ({DB_NAME}) is missing, but backups of it exist, so this is not a new '
                              f'installation. Restore the latest backup with "python cta_recover.py restore". '
                              f'Nothing has been changed.', db)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise DataUnreadable('The data folder cannot be created (permissions or disk problem).', folder)
        tmp = folder / f'.cta-new-{stamp(now)}.db'
        try:
            store = cls.create(tmp, now)
            if any((folder / name).exists() for name in JSON_SOURCES):
                report = migrate_json(store, folder, now)
                if not report.verified:
                    raise DataInvalid('The migrated database did not match the original files, so it was not used. '
                                      f'Your original files are unchanged. {RECOVERY_HINT}', tmp)
            store.close()
            if db.exists():                                      # another window finished first: use that one
                tmp.unlink()
            else:
                os.replace(str(tmp), str(db))
        except sqlite3.Error as exc:
            raise _map_error(exc, tmp) from None
        finally:
            for leftover in (tmp, tmp.with_name(tmp.name + '-journal')):
                if leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        store = cls(db)
        store.backfill_ticket_ids(now)
        return store


class StoreLedger:
    """payments.PaymentLedger backed by the database: the UNIQUE (gateway, transaction_id) key makes a transaction
    pay for one booking only, even with several windows or computers."""

    def __init__(self, store: Store):
        self.store = store

    def claim(self, gateway: str, transaction_id: str, reference: str, amount_cents: int, currency: str, at: str) -> str:
        with self.store._write() as c:
            row = c.execute('SELECT reference FROM payment_claims WHERE gateway = ? AND transaction_id = ?',
                            (gateway, transaction_id)).fetchone()
            if row is not None:
                return 'duplicate' if row[0] == reference else 'conflict'
            c.execute('INSERT INTO payment_claims VALUES (?,?,?,?,?,?)',
                      (gateway, transaction_id, reference, amount_cents, currency, at))
            return 'new'

    def log(self, event: dict) -> None:
        safe = payments._safe_event(event)
        try:
            self.store.audit('payment', safe.get('at', _now()), safe.get('reference', ''), safe.get('by', ''), safe)
        except StorageError as exc:                              # must never block a booking, but must not vanish
            import cta_errors
            cta_errors.report(exc, 'writing a payment audit event', reference=safe.get('reference', ''),
                              status=safe.get('status', ''))


# ------------------------------------------------------------------ migration
@dataclass
class MigrationReport:
    found: Dict[str, int] = field(default_factory=dict)
    added: Dict[str, int] = field(default_factory=dict)
    unchanged: Dict[str, int] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)
    skipped_lines: int = 0
    backup_folder: Optional[Path] = None
    verified: bool = False

    def summary(self) -> str:
        parts = [f"{k}: {self.found.get(k, 0)} found, {self.added.get(k, 0)} added, "
                 f"{self.unchanged.get(k, 0)} already there" for k in ('bookings', 'accounts', 'staff', 'claims')]
        if self.conflicts:
            parts.append(f'{len(self.conflicts)} record(s) differ from the database and were NOT overwritten: '
                         + ', '.join(self.conflicts[:10]))
        if self.skipped_lines:
            parts.append(f'{self.skipped_lines} unreadable line(s) in payment_events.jsonl were skipped')
        parts.append('verified' if self.verified else 'NOT verified')
        return '; '.join(parts)


def _read_sources(folder: Path) -> dict:
    """Strict reads: a damaged source stops the migration before anything is written."""
    src = {
        'history': read_json(folder / 'history.json', list, validate_history, 'bookings'),
        'accounts': read_json(folder / 'accounts.json', list, validate_accounts, 'accounts'),
        'staff': read_json(folder / 'staff.json', list, validate_staff, 'staff'),
        'claims': read_json(folder / 'payments_ledger.json', dict, payments._validate_claims, 'payment ledger'),
        'events': [],
    }
    ev = folder / 'payment_events.jsonl'
    if ev.exists():
        try:
            src['events'] = ev.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            raise DataUnreadable('payment_events.jsonl cannot be read. Nothing was migrated.', ev)
    return src


def backup_json_sources(folder: Path, now: Optional[datetime] = None) -> Optional[Path]:
    present = [folder / n for n in JSON_SOURCES if (folder / n).exists()]
    if not present:
        return None
    dest = backup_dir(folder) / f'pre-migration-{stamp(now)}'
    for p in present:
        backup_file(p, dest, 'original', now)
    return dest


def migrate_json(store: Store, folder: Path, now: Optional[datetime] = None) -> MigrationReport:
    """Copy JSON data into ``store``. Repeatable: records already present and identical are skipped, records that
    differ are reported and never overwritten. Backs everything up first; one transaction; verified afterwards."""
    folder = Path(folder)
    src = _read_sources(folder)
    report = MigrationReport()
    if store.counts()['bookings'] or store.counts()['accounts'] or store.counts()['staff']:
        store.backup('pre-migration', now=now)                   # the database already has data: copy it first
    report.backup_folder = backup_json_sources(folder, now)
    at = _now(now)
    with store._write() as c:
        for kind in ('bookings', 'accounts', 'staff', 'claims'):
            report.found[kind] = report.added[kind] = report.unchanged[kind] = 0
        for rec in src['history']:
            report.found['bookings'] += 1
            existing = store._read_all(c, rec['reference'])
            if existing:
                if canonical(_comparable(existing[0][1])) == canonical(_comparable(rec)):
                    report.unchanged['bookings'] += 1
                else:
                    report.conflicts.append(rec['reference'])
                continue
            store._insert_rows(c, rec, at)
            report.added['bookings'] += 1
        for kind, table, key in (('accounts', 'accounts', 'phone'), ('staff', 'staff', 'username')):
            for item in src[kind]:
                report.found[kind] += 1
                row = c.execute(f'SELECT doc FROM {table} WHERE {key} = ?', (item[key],)).fetchone()  # SQL built only from code constants (table/column lists); values are bound ? parameters  # nosec B608
                if row is not None:
                    if row[0] == canonical(item):
                        report.unchanged[kind] += 1
                    else:
                        report.conflicts.append(f'{kind}:{item[key]}' if kind == 'staff' else f'{kind}:#{report.found[kind]}')
                    continue
                if kind == 'accounts':
                    c.execute('INSERT INTO accounts (phone, name, doc) VALUES (?,?,?)',
                              (item['phone'], item['name'], canonical(item)))
                else:
                    c.execute('INSERT INTO staff (username, role, doc) VALUES (?,?,?)',
                              (item['username'], item.get('role', 'admin'), canonical(item)))
                report.added[kind] += 1
        for key, claim in src['claims'].items():
            report.found['claims'] += 1
            gateway, tx = key.split(':', 1)
            cur = c.execute('INSERT OR IGNORE INTO payment_claims VALUES (?,?,?,?,?,?)',
                            (gateway, tx, claim['reference'], int(claim.get('amount_cents', 0)),
                             str(claim.get('currency', '')), str(claim.get('at', at))))
            report.added['claims'] += cur.rowcount
            report.unchanged['claims'] += 1 - cur.rowcount
        for n, line in enumerate(src['events'], 1):
            try:
                ev = payments._safe_event(json.loads(line))
            except ValueError:
                report.skipped_lines += 1
                continue
            store._audit_insert(c, 'payment', ev.get('at', at), ev.get('reference', ''), ev.get('by', ''), ev,
                                source_key=f'payment_events.jsonl:{n}:{canonical(ev)}')
        store._audit_insert(c, 'migration', at, actor='system', detail={'found': report.found, 'added': report.added,
                                                                        'conflicts': len(report.conflicts)})
        c.execute("INSERT INTO meta VALUES ('json_migrated_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (at,))
    report.verified = verify_migration(store, src, report.conflicts)
    return report


def verify_migration(store: Store, src: dict, conflicts: List[str]) -> bool:
    """Every JSON record that was not a reported conflict must come back from the database exactly as it was."""
    db = {r['reference']: r for r in store.load_history()}
    for rec in src['history']:
        if rec['reference'] in conflicts:
            continue
        if rec['reference'] not in db or canonical(_comparable(db[rec['reference']])) != canonical(_comparable(rec)):
            return False
    accounts = {a['phone']: canonical(a) for a in store.load_accounts()}
    staff = {s['username'].lower(): canonical(s) for s in store.load_staff()}
    for a in src['accounts']:
        if a['phone'] not in accounts:
            return False
    for s in src['staff']:
        if s['username'].lower() not in staff:
            return False
    return not store.verify()


# ------------------------------------------------------------------ restore
def restore_backup(backup: Path, folder: Path, now: Optional[datetime] = None) -> Optional[Path]:
    """Replace cta.db with a checked backup. Close every CTA window first.
    The current database (even if damaged) and its journal files are kept under backups/replaced/."""
    backup, folder = Path(backup), Path(folder)
    if not backup.is_file():
        raise DataMissing(f'The backup {backup.name} does not exist.', backup)
    check = Store(backup, read_only=True, allow_old=True)        # refuses a damaged or foreign file
    try:
        problems = check.verify()
    finally:
        check.close()
    if problems:
        raise DataInvalid(f'The backup {backup.name} is not usable: {problems[0]}', backup)
    db = folder / DB_NAME
    kept = None
    keep_dir = backup_dir(folder) / 'replaced'
    for extra in (db, db.with_name(db.name + '-journal'), db.with_name(db.name + '-wal'),
                  db.with_name(db.name + '-shm')):
        if extra.exists():
            copy = backup_file(extra, keep_dir, 'before-restore', now)
            kept = kept or copy
    tmp = folder / f'.cta-restore-{stamp(now)}.db'
    try:
        shutil.copy2(str(backup), str(tmp))
        for extra in (db.with_name(db.name + '-journal'), db.with_name(db.name + '-wal'),
                      db.with_name(db.name + '-shm')):
            if extra.exists():
                extra.unlink()                                   # a stale journal must not be replayed onto the backup
        os.replace(str(tmp), str(db))
    except OSError:
        raise DataWriteError('Could not put the backup in place. The current database was kept.', db)
    finally:
        if tmp.exists():
            tmp.unlink()
    restored = Store(db)
    try:
        restored.audit('restore', _now(now), actor='operator', detail={'from': backup.name})
    finally:
        restored.close()
    return kept
