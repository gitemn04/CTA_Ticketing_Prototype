"""Module 3 tests: ticket authenticity, QR validation, atomic single-use check-in and the ticket life cycle."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.environ['CTA_ENV'] = 'testing'
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)           # throw-away key used only by the tests

import cta_core as core
import cta_db
import cta_payments as pay
import cta_routing as rt
import cta_stats as stats
import cta_storage
import cta_tickets as T
from test_all import FakePSP, good, legacy_install, sign_in, TEST_PASSWORD
import cta_auth as auth


def test_key() -> bytes:
    return bytes.fromhex(os.environ['CTA_TICKET_KEY'])


NOW = datetime(2026, 10, 25, 9, 0)


class StoreCase(unittest.TestCase):
    """A fresh database folder plus helpers to issue tickets and scan them."""
    NOW = NOW

    def setUp(self):
        T.set_default_signer(None)
        self.dir = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.dir, self.NOW)
        self.signer = T.TicketSigner([test_key()])
        self.addCleanup(self.store.close)
        self.addCleanup(T.set_default_signer, None)
        self.admin = sign_in(self.store, 'admin')          # real signed-in staff: plain names are no longer accepted
        self.gate = sign_in(self.store, 'gate1')

    def as_(self, name):
        return sign_in(self.store, name)

    def issue(self, names=('Ann Lee',), legs=1, method='card', when=None, a='Centrala', b='Zord', provider=None,
              environment='testing', store=None):
        store = store or self.store
        when = when or self.NOW
        hist = store.load_history()
        route = rt.astar(a, b)
        zones = core.chargeable_zones(a, b, route)
        rec = core.make_record(route, {'Adult': len(names)}, zones, when, when, hist, names=list(names),
                               account_phone='+447123456789', legs=legs)
        rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], 'x', when, provider,
                                 environment=environment)
        hist.append(rec)
        store.save_history(hist, when)
        return rec

    def tok(self, rec, i=0, leg='out'):
        p = rec['passengers'][i]
        return self.signer.sign(p['return_ticket_id'] if leg == 'return' else p['ticket_id'])

    def no(self, rec, i=0, leg='out'):
        p = rec['passengers'][i]
        return p['return_ticket_no'] if leg == 'return' else p['ticket_no']

    def scan(self, text, now=None, operator=None, store=None, **kw):
        kw.setdefault('environment', 'testing')
        operator = self.gate if operator is None else operator
        return (store or self.store).check_in(text, signer=self.signer, now=now or self.NOW, operator=operator, **kw)

    def fresh(self, store=None):
        return (store or self.store).load_history()

    def paid_by_staff(self, rec, store=None):
        ok, msg = (store or self.store).mark_paid(rec['passengers'][0]['ticket_no'], self.admin, self.NOW)
        self.assertTrue(ok, msg)


# ====================================================================== the QR token
class TokenTests(unittest.TestCase):
    def setUp(self):
        self.signer = T.TicketSigner([test_key()])

    def test_token_shape_and_signature(self):
        tid = core.new_ticket_id()
        tok = self.signer.sign(tid)
        self.assertRegex(tok, r'^CTA1:[0-9A-F]{4}:[A-Z2-7]{26}:[A-Z2-7]{26}$')
        self.assertEqual(self.signer.verify(tok), tid)
        self.assertEqual(self.signer.sign(tid), tok)                       # deterministic for one key
        self.assertIsNone(self.signer.verify(tok.lower()))                 # verify() itself is strict...
        self.assertEqual(T.parse_presented(tok.lower(), self.signer).ticket_id, tid)   # ...the reader tolerates case
        with self.assertRaises(ValueError):
            self.signer.sign('not-a-ticket-id')

    def paid_ticket(self):
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, [], names=['Ann Lee'])
        rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
        return rec

    def admitted(self, rec, text):
        return T.check_in_memory([rec], text, NOW, 'gate1', signer=self.signer, environment='testing')

    def test_every_single_character_change_is_rejected(self):
        rec = self.paid_ticket()
        tok = self.signer.sign(rec['passengers'][0]['ticket_id'])
        base32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
        for i, ch in enumerate(tok):
            pool = '0123456789ABCDEF' if i in range(5, 9) else base32
            other = next(c for c in pool if c != ch)
            forged = tok[:i] + other + tok[i + 1:]
            res = self.admitted(rec, forged)
            self.assertFalse(res.ok, f'position {i}')
            self.assertEqual(rec['passengers'][0]['status'], 'issued', f'position {i}')      # nothing consumed
            if i >= 5:                                   # a real token with something altered inside it
                self.assertIn(res.code, (T.FORGED, T.MALFORMED), f'position {i}')
            else:                                        # the CTA1: prefix itself changed: just an unknown typed number
                self.assertEqual(res.code, T.UNKNOWN, f'position {i}')
            self.assertEqual(T.parse_presented(forged, self.signer).ticket_id, '', f'position {i}')
        self.assertTrue(self.admitted(rec, tok).ok)                                          # the untouched one works

    def test_other_keys_unknown_key_ids_truncation_and_extra_parts_are_rejected(self):
        rec = self.paid_ticket()
        tid = rec['passengers'][0]['ticket_id']
        tok = self.signer.sign(tid)
        stranger = T.TicketSigner([bytes.fromhex('a1' * 32)])
        self.assertEqual(T.parse_presented(stranger.sign(tid), self.signer).code, T.FORGED)
        self.assertIsNone(stranger.verify(tok))
        for bad in (tok[:-3], tok + 'A', tok + ':EXTRA', 'CTA1::' + tid + ':' + 'A' * 26,
                    'CTA1:ZZZZ:' + tid + ':' + 'A' * 26):
            self.assertIn(T.parse_presented(bad, self.signer).code, (T.FORGED, T.MALFORMED), bad)
            self.assertFalse(self.admitted(rec, bad).ok, bad)
        wrong_version = tok.replace('CTA1', 'CTA2')
        self.assertFalse(self.admitted(rec, wrong_version).ok)                                # not a token: unknown number
        self.assertEqual(self.admitted(rec, wrong_version).code, T.UNKNOWN)
        self.assertEqual(rec['passengers'][0]['status'], 'issued')

    def test_token_carries_nothing_personal(self):
        with tempfile.TemporaryDirectory() as d:
            store = cta_db.Store.open(Path(d), NOW)
            hist = []
            route = rt.astar('Centrala', 'Zord')
            rec = core.make_record(route, {'Adult': 1}, 3, NOW, NOW, hist, names=['Nguyễn Thị Hồng Ánh'],
                                   account_phone='+447123456789', legs=2)
            rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
            store.close()
        for leg in ('out', 'return'):
            text = core.ticket_qr_payload(rec, rec['passengers'][0], leg, self.signer).upper()
            for private in ('NGUY', 'HỒNG', 'CENTRALA', 'ZORD', '447123', rec['recovery_code'], rec['reference'],
                            rec['passengers'][0]['ticket_no']):
                self.assertNotIn(private.upper(), text)

    def test_ticket_ids_are_unique_random_and_unrelated_to_numbers(self):
        ids = {core.new_ticket_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)
        self.assertTrue(all(core.TICKET_ID_RE.match(i) for i in ids))
        hist = []
        route = rt.astar('Centrala', 'Zord')
        rec = core.make_record(route, {'Adult': 3}, 3, NOW, NOW, hist, names=['Ann Lee', 'Bo Lee', 'Cy Lee'], legs=2)
        found = []
        for p in rec['passengers']:
            found += [p['ticket_id'], p['return_ticket_id']]
            for tid in (p['ticket_id'], p['return_ticket_id']):
                self.assertNotIn(tid, p['ticket_no'] + rec['reference'])
        self.assertEqual(len(set(found)), 6)                               # every journey has its own id

    def test_old_style_qr_garbage_and_missing_key_are_not_accepted_as_codes(self):
        legacy = 'CTA METRO TICKET\nTicket: CTA-20261025-001-P1\nPassenger: Ann Lee\nTrip: Centrala to Zord'
        res = T.parse_presented(legacy, self.signer)
        self.assertEqual((res.code, res.ticket_no), (T.LEGACY_QR, ''))
        self.assertNotIn('Ann', res.log_ref)
        self.assertEqual(T.parse_presented('   ', self.signer).code, T.EMPTY)
        manual = T.parse_presented(' cta-20261025-001-p1 ', self.signer)
        self.assertEqual((manual.code, manual.method, manual.ticket_no), ('', 'manual', 'CTA-20261025-001-P1'))
        tok = self.signer.sign(core.new_ticket_id())
        self.assertEqual(T.parse_presented(tok, None).code, T.KEY_UNAVAILABLE)
        self.assertIn('Type the ticket number', T.parse_presented(tok, None).message)

    def test_log_reference_never_contains_the_signature(self):
        tid = core.new_ticket_id()
        tok = self.signer.sign(tid)
        mac = tok.split(':')[3]
        for text in (tok, tok[:-1] + ('A' if tok[-1] != 'A' else 'B'), 'CTA1:' + 'X' * 500):
            ref = T.parse_presented(text, self.signer).log_ref
            self.assertNotIn(mac, ref)
            self.assertLessEqual(len(ref), 40)


# ====================================================================== signing keys
class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        T.set_default_signer(None)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(T.set_default_signer, None)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CTA_TICKET_KEY', None)
        os.environ.pop('CTA_TICKET_KEY_PREVIOUS', None)

    def test_key_is_created_once_outside_the_source_and_kept_private(self):
        store = cta_db.Store.open(self.dir, NOW)
        a = T.load_signer(self.dir, store, NOW)
        b = T.load_signer(self.dir, store, NOW)
        self.assertEqual(a.current_kid, b.current_kid)
        path = self.dir / T.KEY_FILE
        self.assertTrue(path.is_file())
        self.assertFalse(str(self.dir).startswith(str(ROOT)))
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o077, 0)                # owner-only
        text = path.read_text()
        self.assertEqual(len(json.loads(text)['keys']), 1)
        for source in ROOT.glob('*.py'):                                    # the secret is in no source file
            self.assertNotIn(json.loads(text)['keys'][0]['secret'], source.read_text(encoding='utf-8'))
        self.assertEqual(store.meta_get('signing_started'), '1')
        store.close()

    def test_a_missing_key_file_is_reported_not_silently_replaced(self):
        store = cta_db.Store.open(self.dir, NOW)
        T.load_signer(self.dir, store, NOW)
        (self.dir / T.KEY_FILE).unlink()
        with self.assertRaises(T.KeyUnavailable) as cm:
            T.load_signer(self.dir, store, NOW)
        self.assertIn('missing', str(cm.exception))
        self.assertFalse((self.dir / T.KEY_FILE).exists())                  # no new key was invented
        store.close()

    def test_a_different_key_file_than_the_one_that_signed_is_reported(self):
        store = cta_db.Store.open(self.dir, NOW)
        T.load_signer(self.dir, store, NOW)
        (self.dir / T.KEY_FILE).unlink()
        T._create_key_file(self.dir, NOW)                                   # someone put a fresh, different key there
        with self.assertRaises(T.KeyUnavailable):
            T.load_signer(self.dir, store, NOW)
        store.close()

    def test_environment_key_wins_and_is_validated(self):
        os.environ['CTA_TICKET_KEY'] = 'ab' * 32
        signer = T.load_signer(self.dir, None, NOW)
        self.assertEqual(signer.kids, [T.key_id(bytes.fromhex('ab' * 32))])
        self.assertFalse((self.dir / T.KEY_FILE).exists())
        for bad in ('xyz', 'ab' * 10):
            os.environ['CTA_TICKET_KEY'] = bad
            with self.assertRaises(T.KeyUnavailable):
                T.signer_from_env()
        os.environ['CTA_TICKET_KEY'] = 'cd' * 32
        os.environ['CTA_TICKET_KEY_PREVIOUS'] = 'ab' * 32
        both = T.signer_from_env()
        old = T.TicketSigner([bytes.fromhex('ab' * 32)]).sign(core.new_ticket_id())
        self.assertIsNotNone(both.verify(old))                              # previous key still verifies

    def test_damaged_key_file_is_an_error_and_is_left_alone(self):
        (self.dir / T.KEY_FILE).write_text('{"keys": [')
        with self.assertRaises(cta_storage.DataCorrupted):
            T.load_signer(self.dir, None, NOW)
        self.assertEqual((self.dir / T.KEY_FILE).read_text(), '{"keys": [')
        (self.dir / T.KEY_FILE).write_text(json.dumps({'keys': [{'secret': 'abcd'}]}))
        with self.assertRaises(cta_storage.DataInvalid):
            T.load_signer(self.dir, None, NOW)

    def test_rotation_keeps_old_tickets_valid_until_retired(self):
        T.load_signer(self.dir, None, NOW)
        before = T.TicketSigner(T.read_key_file(self.dir))
        tid = core.new_ticket_id()
        old_token = before.sign(tid)
        new_kid = T.rotate_key(self.dir, NOW)
        after = T.TicketSigner(T.read_key_file(self.dir))
        self.assertEqual(after.current_kid, new_kid)
        self.assertNotEqual(after.sign(tid), old_token)                     # new tickets use the new key
        self.assertEqual(after.verify(old_token), tid)                      # old printed tickets still verify
        self.assertEqual(len(T.key_summary(self.dir)), 2)
        self.assertEqual(T.retire_old_keys(self.dir), 1)
        retired = T.TicketSigner(T.read_key_file(self.dir))
        self.assertIsNone(retired.verify(old_token))                        # retired key: reprint needed
        self.assertTrue(all('secret' not in str(k) for k in T.key_summary(self.dir)))

    def test_read_only_loading_never_creates_a_key(self):
        self.assertIsNone(T.load_signer_readonly(self.dir))
        self.assertFalse((self.dir / T.KEY_FILE).exists())

    def test_two_windows_starting_together_end_up_with_the_same_key(self):
        stores = [cta_db.Store.open(self.dir, NOW)]
        kids, errors = [], []
        barrier = threading.Barrier(6)

        def run():
            try:
                barrier.wait()
                kids.append(T.load_signer(self.dir, None, NOW).current_kid)
            except Exception as exc:                                        # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(set(kids)), 1)
        self.assertEqual(len(json.loads((self.dir / T.KEY_FILE).read_text())['keys']), 1)
        stores[0].close()

    def test_a_window_starting_mid_creation_never_sees_a_half_written_key_file(self):
        """Deterministic version of the start-up race: another window reads the key file at the exact moment the
        first window is writing it. It must see no file or a complete one - never an empty, 'damaged' file."""
        real_fdopen = os.fdopen
        seen = []

        def writing(fd, *a, **k):
            seen.append(T.read_key_file(self.dir))          # the other window looks now (raises if half-written)
            return real_fdopen(fd, *a, **k)
        with mock.patch.object(T.os, 'fdopen', side_effect=writing):
            T._create_key_file(self.dir, NOW)
        self.assertEqual(seen, [None])                      # nothing published yet while the key was being written
        self.assertEqual(len(T.read_key_file(self.dir)), 1)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith('.tmp')], [])   # no litter

    def test_recovery_tool_key_commands(self):
        import cta_recover, io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cta_recover.main(['key-status', '--folder', str(self.dir)]), 1)       # none yet
            T.load_signer(self.dir, None, NOW)
            self.assertEqual(cta_recover.main(['key-status', '--folder', str(self.dir)]), 0)
            self.assertEqual(cta_recover.main(['rotate-key', '--folder', str(self.dir), '--yes']), 0)
            self.assertEqual(cta_recover.main(['retire-keys', '--folder', str(self.dir), '--yes']), 0)
        text = out.getvalue()
        secret = json.loads((self.dir / T.KEY_FILE).read_text())['keys'][0]['secret']
        self.assertNotIn(secret, text)                                      # ids only, never secrets
        self.assertIn('1 older key(s) removed', text)


# ====================================================================== accepting and rejecting tickets
class CheckInOutcomeTests(StoreCase):
    def test_a_genuine_paid_ticket_is_accepted_within_its_period_by_qr_and_by_number(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'))
        res = self.scan(self.tok(rec, 0))
        self.assertTrue(res.ok, res.message)
        self.assertEqual((res.code, res.method, res.leg), ('OK', 'qr', 'out'))
        self.assertIn('OK to travel', res.message)
        res = self.scan(self.no(rec, 1).lower())                          # typed, any case
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.method, 'manual')
        again = self.fresh()[0]['passengers']
        self.assertEqual([p['status'] for p in again], ['boarded', 'boarded'])
        self.assertEqual(again[0]['boarded_by'], 'gate1')
        self.assertEqual(again[0]['boarded_at'], '2026-10-25 09:00:00')

    def test_unknown_forged_modified_and_foreign_codes_are_rejected(self):
        rec = self.issue()
        tok = self.tok(rec)
        cases = {
            'unknown number': ('CTA-20991231-001-P1', T.UNKNOWN),
            'guessed neighbour': ('CTA-20261025-002-P1', T.UNKNOWN),
            'modified id': (tok.replace(rec['passengers'][0]['ticket_id'][:4], 'AAAA', 1), T.FORGED),
            'modified signature': (tok[:-1] + ('A' if tok[-1] != 'A' else 'B'), T.FORGED),
            'other install key': (T.TicketSigner([bytes.fromhex('a1' * 32)]).sign(rec['passengers'][0]['ticket_id']), T.FORGED),
            'garbled': ('CTA1:oops', T.MALFORMED),
            'old style qr': ('CTA METRO TICKET\nTicket: ' + self.no(rec), T.LEGACY_QR),
            'empty': ('', T.EMPTY),
        }
        for why, (text, code) in cases.items():
            res = self.scan(text)
            self.assertFalse(res.ok, why)
            self.assertEqual(res.code, code, why)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')        # nothing was consumed
        self.assertTrue(self.scan(tok).ok)                                            # and the real one still works

    def test_a_genuine_signature_for_a_ticket_that_is_not_in_the_records_is_unknown(self):
        res = self.scan(self.signer.sign(core.new_ticket_id()))
        self.assertEqual((res.ok, res.code, res.method), (False, T.UNKNOWN, 'qr'))
        self.assertIn('genuine', res.message)

    def test_no_signing_key_means_qr_is_refused_but_typing_the_number_still_works(self):
        rec = self.issue()
        res = self.store.check_in(self.tok(rec), signer=None, now=NOW, operator=self.gate, environment='testing')
        self.assertEqual(res.code, T.KEY_UNAVAILABLE)
        res = self.store.check_in(self.no(rec), signer=None, now=NOW, operator=self.gate, environment='testing')
        self.assertTrue(res.ok, res.message)

    def test_unpaid_tickets_are_rejected_until_staff_record_the_payment(self):
        rec = self.issue(method='cash')
        for text in (self.tok(rec), self.no(rec)):
            res = self.scan(text)
            self.assertEqual((res.ok, res.code), (False, T.UNPAID))
            self.assertIn('NOT PAID', res.message)
        self.paid_by_staff(rec)
        self.assertTrue(self.scan(self.tok(rec)).ok)                                  # reflected immediately

    def test_unconfirmed_online_and_test_payments_are_rejected_in_production(self):
        pending = self.issue(method='card', provider=FakePSP(claim='pending'), environment='production')
        res = self.scan(self.tok(pending), environment='production')
        self.assertEqual(res.code, T.UNCONFIRMED)
        failed_to_confirm = self.issue(method='card', provider=FakePSP(claim='paid', verify_error=OSError('x')),
                                       environment='production')
        self.assertEqual(self.scan(self.no(failed_to_confirm), environment='production').code, T.UNCONFIRMED)
        simulated = self.issue(method='card')                                         # test-mode money
        self.assertEqual(self.scan(self.tok(simulated), environment='production').code, T.TEST_PAYMENT)
        self.assertTrue(self.scan(self.tok(simulated), environment='testing').ok)

    def test_a_verified_live_payment_is_accepted_in_production(self):
        hist = self.fresh()
        route = rt.astar('Centrala', 'Zord')
        rec = core.make_record(route, {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'])
        psp = FakePSP(verified=good(rec['total_cents'], rec['reference']))
        rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, psp, environment='production')
        hist.append(rec)
        self.store.save_history(hist, NOW)
        self.assertEqual(rec['payment']['status'], 'paid')
        self.assertTrue(self.scan(rec['passengers'][0]['ticket_no'], environment='production').ok)

    def test_not_yet_valid_valid_and_expired_periods(self):
        rec = self.issue(when=datetime(2026, 10, 25, 14, 2))
        start, until = datetime(2026, 10, 25, 0, 0), datetime(2026, 10, 26, 5, 0)
        self.assertEqual(core.ticket_valid_from(rec), start)
        self.assertEqual(core.ticket_valid_until(rec), until)
        early = self.scan(self.tok(rec), now=start - timedelta(minutes=1))
        self.assertEqual((early.ok, early.code), (False, T.NOT_YET_VALID))
        late = self.scan(self.no(rec), now=until + timedelta(minutes=1))
        self.assertEqual((late.ok, late.code), (False, T.EXPIRED))
        self.assertIn('expired', late.message)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')
        self.assertTrue(self.scan(self.tok(rec), now=until).ok)                       # last minute is still valid
        other = self.issue(when=datetime(2026, 10, 25, 14, 2))
        self.assertTrue(self.scan(self.tok(other), now=start).ok)                     # first minute of the day

    def test_a_ticket_without_a_readable_travel_date_is_not_admitted(self):
        rec = self.issue()
        hist = self.fresh()
        hist[0]['depart_at'] = 'sometime'
        res = T.check_in_memory(hist, self.no(rec), NOW, 'gate1', signer=self.signer, environment='testing')
        self.assertEqual(res.code, T.BAD_RECORD)

    def test_cancelled_and_refunded_tickets_are_rejected(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'))
        ok, msg = self.store.cancel_ticket(self.no(rec, 0), self.admin, NOW, 'changed plans')
        self.assertTrue(ok, msg)
        for text in (self.tok(rec, 0), self.no(rec, 0)):
            res = self.scan(text)
            self.assertEqual((res.ok, res.code), (False, T.CANCELLED))
        self.assertTrue(self.scan(self.tok(rec, 1)).ok)                                # the other passenger is unaffected
        ok, msg = self.store.record_refund(self.no(rec, 0), self.admin, NOW)
        self.assertTrue(ok, msg)
        for text in (self.tok(rec, 0), self.no(rec, 0)):
            res = self.scan(text)
            self.assertEqual((res.ok, res.code), (False, T.REFUNDED))

    def test_a_used_single_use_ticket_is_rejected_by_either_route(self):
        rec = self.issue()
        self.assertTrue(self.scan(self.tok(rec)).ok)
        for text in (self.tok(rec), self.no(rec)):
            res = self.scan(text, now=NOW + timedelta(minutes=5))
            self.assertEqual((res.ok, res.code), (False, T.USED))
            self.assertIn('ALREADY USED at 2026-10-25 09:00:00', res.message)
        first = self.issue()
        self.assertTrue(self.scan(self.no(first)).ok)
        self.assertEqual(self.scan(self.tok(first)).code, T.USED)                      # typed first, scanned second

    def test_station_restriction_when_a_gate_names_its_station(self):
        rec = self.issue(legs=2)
        wrong = self.scan(self.tok(rec), station='Zord')
        self.assertEqual((wrong.ok, wrong.code), (False, T.WRONG_STATION))
        self.assertTrue(self.scan(self.tok(rec), station='centrala').ok)
        back_wrong = self.scan(self.tok(rec, leg='return'), station='Centrala')
        self.assertEqual(back_wrong.code, T.WRONG_STATION)                             # the way back starts at Zord
        self.assertTrue(self.scan(self.tok(rec, leg='return'), station='Zord').ok)
        self.assertEqual(self.scan(self.no(rec), station='Centrala').code, T.USED)

    def test_only_a_signed_in_operator_can_check_tickets_in(self):
        rec = self.issue()
        for operator in ('', '   '):
            res = self.scan(self.tok(rec), operator=operator)
            self.assertEqual((res.ok, res.code), (False, T.NOT_AUTHORIZED))
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')
        self.assertTrue(self.scan(self.tok(rec), operator=self.gate).ok)

    def test_pre_2_1_placeholder_tickets_cannot_be_checked_in(self):
        old = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, [])
        old['payment'] = pay.legacy_payment(old['total_cents'])
        self.store.save_history([old], NOW, actor=self.admin)       # an imported old record: a direct data edit
        res = self.scan(old['reference'])
        self.assertEqual((res.ok, res.code), (False, T.LEGACY_TICKET))

    def test_recovery_codes_cannot_be_used_to_get_in(self):
        rec = self.issue(method='cash')                                               # unpaid
        code, ref = rec['recovery_code'], rec['reference']
        for text in (code, f'{ref} {code}', f'{ref}{code}', f'{ref}:{code}', f'CTA1:0000:{code}:{code}'):
            res = self.scan(text)
            self.assertFalse(res.ok, text)
            self.assertIn(res.code, (T.UNKNOWN, T.MALFORMED, T.FORGED), text)
        found = core.find_ticket_with_code(self.fresh(), ref, code)                   # the code does open the booking...
        self.assertIsNotNone(found)
        self.assertEqual(self.scan(self.no(rec)).code, T.UNPAID)                       # ...but never gets anyone through
        self.paid_by_staff(rec)
        until = core.ticket_valid_until(rec)
        self.assertEqual(self.scan(self.no(rec), now=until + timedelta(minutes=1)).code, T.EXPIRED)
        self.assertTrue(self.store.cancel_ticket(self.no(rec), self.admin, NOW)[0])
        self.assertEqual(self.scan(self.no(rec)).code, T.CANCELLED)
        self.assertIsNotNone(core.find_ticket_with_code(self.fresh(), ref, code))     # still opens, still refused


# ====================================================================== the scan log
class ScanLogTests(StoreCase):
    def test_every_attempt_is_logged_with_outcome_operator_and_device_but_no_personal_data(self):
        rec = self.issue(names=('Zoë Ünal-Svensson',), legs=2, method='cash')
        tok = self.tok(rec)
        mac = tok.split(':')[3]
        self.scan(tok, operator=self.as_('alice'), device='GATE-7')                             # unpaid
        self.paid_by_staff(rec)
        self.scan(tok, operator=self.as_('alice'), device='GATE-7', station='Centrala')
        self.scan(self.no(rec), operator=self.as_('bob'), device='GATE-8')                       # used
        self.scan(tok[:-1] + ('A' if tok[-1] != 'A' else 'B'), operator=self.as_('bob'))         # forged
        self.scan('CTA-NOPE', operator=self.as_('bob'))
        self.scan('', operator='')
        rows = self.store.scans()
        self.assertEqual([r['result'] for r in rows], ['UNPAID', 'OK', 'USED', 'FORGED', 'UNKNOWN', 'EMPTY'][:5] + ['NOT_AUTHORIZED'])
        self.assertEqual([r['ok'] for r in rows], [0, 1, 0, 0, 0, 0])
        self.assertEqual((rows[1]['staff'], rows[1]['device'], rows[1]['station'], rows[1]['method'], rows[1]['leg']),
                         ('alice', 'GATE-7', 'Centrala', 'qr', 'out'))
        self.assertEqual(rows[2]['staff'], 'bob')
        self.assertEqual(rows[1]['ticket_id'], rec['passengers'][0]['ticket_id'])
        dump = json.dumps(rows, ensure_ascii=False)
        for private in ('Zoë', 'Ünal', 'Svensson', '447123', rec['recovery_code'], mac, 'Centrala to', 'Zord to'):
            self.assertNotIn(private, dump)
        raw = ' '.join(str(v) for row in self.store.conn.execute('SELECT * FROM scans') for v in row)
        self.assertNotIn('Zoë', raw)
        self.assertNotIn(mac, raw)

    def test_the_scan_audit_and_ticket_records_cannot_be_rewritten_or_deleted(self):
        rec = self.issue()
        self.scan(self.tok(rec))
        for sql in ("UPDATE scans SET ok = 0", "DELETE FROM scans", "UPDATE audit SET kind = 'x'", "DELETE FROM audit",
                    "DELETE FROM tickets", "UPDATE tickets SET ticket_id = 'AAAAAAAAAAAAAAAAAAAAAAAAAA'",
                    "UPDATE tickets SET uses = 5"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.store.conn.execute(sql)
        self.assertEqual(len(self.store.scans()), 1)
        self.assertEqual(self.store.counts()['tickets'], 1)

    def test_a_failed_database_write_means_the_scan_is_not_accepted(self):
        rec = self.issue()
        with mock.patch.object(cta_db.Store, '_insert_rows', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(cta_storage.StorageError):
                self.scan(self.tok(rec))
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')        # not used, not logged as OK
        self.assertEqual(self.store.scans(), [])
        self.assertTrue(self.scan(self.tok(rec)).ok)

    def test_read_only_database_refuses_check_in(self):
        rec = self.issue()
        self.store.close()
        with mock.patch('cta_db.os.access', return_value=False):
            ro = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(ro.close)
        self.assertTrue(ro.read_only)
        with self.assertRaises(cta_storage.ReadOnlyStorage):
            ro.check_in(self.no(rec), signer=self.signer, now=NOW, operator=self.gate, environment='testing')
        self.store = cta_db.Store.open(self.dir, NOW)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')


# ====================================================================== simultaneous scans
SCAN_SCRIPT = r'''
import os, sys, time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import cta_db, cta_tickets as T
folder, text, go, who = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), sys.argv[5]
store = cta_db.Store.open(folder)
signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
while not go.exists():
    time.sleep(0.002)
me = store.staff_login('gate' + who, os.environ['CTA_TEST_PASSWORD'], datetime(2026, 10, 25, 8, 0))
res = store.check_in(text, signer=signer, now=datetime(2026, 10, 25, 9, 0), operator=me,
                     environment='testing')
print(res.code)
'''


class SimultaneousScanTests(StoreCase):
    def test_sequential_duplicate_scan_is_refused(self):
        rec = self.issue()
        results = [self.scan(self.tok(rec)) for _ in range(3)]
        self.assertEqual([r.code for r in results], ['OK', 'USED', 'USED'])

    def test_many_threads_scanning_the_same_ticket_only_one_gets_in(self):
        rec = self.issue()
        tok, number = self.tok(rec), self.no(rec)
        n = 10
        barrier = threading.Barrier(n)
        results, errors = [], []
        gates = [self.as_(f'gate{i}') for i in range(n)]

        def run(i):
            try:
                store = cta_db.Store.open(self.dir)
                try:
                    barrier.wait(timeout=10)
                    res = store.check_in(tok if i % 2 else number, signer=self.signer, now=NOW,
                                         operator=gates[i], environment='testing')
                    results.append((res.code, res.method))
                finally:
                    store.close()
            except Exception as exc:                                                 # pragma: no cover
                errors.append(repr(exc))
        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        self.assertEqual(errors, [])
        codes = sorted(c for c, _m in results)
        self.assertEqual(codes, ['OK'] + ['USED'] * (n - 1))
        self.assertEqual({m for _c, m in results}, {'qr', 'manual'})                  # both routes took part
        rows = self.store.scans()
        self.assertEqual(len(rows), n)
        self.assertEqual(sum(r['ok'] for r in rows), 1)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'boarded')
        self.assertEqual(self.store.conn.execute('SELECT uses FROM tickets').fetchone()[0], 1)

    def test_separate_processes_scanning_at_the_same_moment_only_one_gets_in(self):
        rec = self.issue()
        tok = self.tok(rec)
        go = self.dir / 'go.flag'
        env = dict(os.environ, CTA_TEST_PASSWORD=TEST_PASSWORD)
        for i in range(4):
            self.as_(f'gate{i}')
        procs = [subprocess.Popen([sys.executable, '-c', SCAN_SCRIPT, str(ROOT), str(self.dir),
                                   tok if i % 2 else self.no(rec), str(go), str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for i in range(4)]
        import time
        time.sleep(1.5)                                                               # let every process open its connection
        go.write_text('go')
        outs = [p.communicate(timeout=60) for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), [o[1] for o in outs])
        codes = sorted(o[0].strip() for o in outs)
        self.assertEqual(codes, ['OK', 'USED', 'USED', 'USED'])
        self.assertEqual(len(self.store.scans()), 4)

    def test_threads_on_a_multi_ride_ticket_use_exactly_the_rides_it_has(self):
        rec = self.issue()
        self.store.conn.execute("UPDATE tickets SET max_uses = 3")                    # a future multi-ride product
        self.store.conn.execute("UPDATE bookings SET version = version + 1")
        tok = self.tok(rec)
        n = 8
        barrier = threading.Barrier(n)
        codes = []

        def run(i):
            store = cta_db.Store.open(self.dir)
            try:
                barrier.wait(timeout=10)
                codes.append(store.check_in(tok, signer=self.signer, now=NOW, operator=self.gate,
                                            environment='testing').code)
            finally:
                store.close()
        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        self.assertEqual(sorted(codes), ['OK'] * 3 + ['USED'] * 5)
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual((p['uses'], p['max_uses'], p['status']), (3, 3, 'boarded'))

    def test_multi_ride_ticket_counts_rides_and_stays_valid_until_the_last(self):
        rec = self.issue()
        self.store.conn.execute("UPDATE tickets SET max_uses = 2")
        self.store.conn.execute("UPDATE bookings SET version = version + 1")
        first = self.scan(self.tok(rec))
        self.assertTrue(first.ok)
        self.assertIn('ride 1 of 2', first.message)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')
        second = self.scan(self.no(rec))
        self.assertIn('ride 2 of 2', second.message)
        self.assertEqual(self.scan(self.tok(rec)).code, T.USED)
        self.assertTrue(self.store.allow_reentry(self.no(rec), self.admin, NOW, 'gate fault')[0])
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual((p['uses'], p['status']), (1, 'issued'))                      # one ride given back, not two
        self.assertTrue(self.scan(self.tok(rec)).ok)


# ====================================================================== return tickets
class ReturnTicketRuleTests(StoreCase):
    def test_both_journeys_work_in_either_order_and_each_only_once(self):
        a = self.issue(legs=2)
        self.assertTrue(self.scan(self.tok(a)).ok)                                    # outbound first...
        self.assertEqual(self.scan(self.tok(a)).code, T.USED)
        self.assertTrue(self.scan(self.tok(a, leg='return')).ok)                      # ...the way back is still valid
        self.assertEqual(self.scan(self.no(a, leg='return')).code, T.USED)
        b = self.issue(legs=2)
        self.assertTrue(self.scan(self.no(b, leg='return'), now=NOW).ok)               # return first is also allowed
        self.assertTrue(self.scan(self.tok(b)).ok)
        p = self.fresh()[1]['passengers'][0]
        self.assertEqual((p['status'], p['return_status']), ('boarded', 'boarded'))

    def test_using_one_journey_never_touches_the_other(self):
        rec = self.issue(legs=2, names=('Ann Lee', 'Bo Lee'))
        self.scan(self.tok(rec, 0))
        pax = self.fresh()[0]['passengers']
        self.assertEqual([(p['status'], p['return_status']) for p in pax], [('boarded', 'issued'), ('issued', 'issued')])
        self.assertNotEqual(pax[0]['ticket_id'], pax[0]['return_ticket_id'])

    def test_return_leg_is_valid_all_travel_day_and_expires_with_the_ticket(self):
        rec = self.issue(legs=2, when=datetime(2026, 10, 25, 8, 0))
        self.assertTrue(self.scan(self.tok(rec), now=datetime(2026, 10, 25, 8, 10)).ok)
        self.assertTrue(self.scan(self.tok(rec, leg='return'), now=datetime(2026, 10, 25, 23, 50)).ok)
        other = self.issue(legs=2, when=datetime(2026, 10, 25, 8, 0))
        late = datetime(2026, 10, 26, 5, 1)
        self.assertEqual(self.scan(self.tok(other, leg='return'), now=late).code, T.EXPIRED)
        self.assertEqual(self.scan(self.tok(other), now=late).code, T.EXPIRED)

    def test_cancelling_one_journey_leaves_the_other_valid_and_refunds_only_that_journey(self):
        rec = self.issue(legs=2)
        leg_fare = rec['passengers'][0]['leg_fare_cents']
        ok, msg = self.store.cancel_ticket(self.no(rec, leg='return'), self.admin, NOW, 'not coming back')
        self.assertTrue(ok, msg)
        self.assertIn(f'{leg_fare:,}', msg)
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual((p['status'], p['return_status'], p['return_refund_cents']), ('issued', 'cancelled', leg_fare))
        self.assertTrue(self.scan(self.tok(rec)).ok)
        self.assertEqual(self.scan(self.tok(rec, leg='return')).code, T.CANCELLED)

    def test_cancelling_a_booking_after_the_outbound_trip_only_cancels_the_unused_way_back(self):
        rec = self.issue(legs=2)
        self.scan(self.tok(rec))
        ok, msg = self.store.cancel_booking(rec['reference'], self.admin, NOW, 'changed plans')
        self.assertTrue(ok, msg)
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual((p['status'], p['return_status']), ('boarded', 'cancelled'))
        self.assertNotIn('refund_cents', p)                                            # the used journey is not refunded
        self.assertEqual(p['return_refund_cents'], p['leg_fare_cents'])
        self.assertFalse(self.store.cancel_booking(rec['reference'], self.admin, NOW)[0])  # nothing left to cancel

    def test_re_entry_on_one_journey_does_not_change_the_other(self):
        rec = self.issue(legs=2)
        self.scan(self.tok(rec))
        self.scan(self.tok(rec, leg='return'))
        self.assertTrue(self.store.allow_reentry(self.no(rec), self.admin, NOW, 'left through wrong gate')[0])
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual((p['status'], p['return_status']), ('issued', 'boarded'))
        self.assertTrue(self.scan(self.tok(rec)).ok)
        self.assertEqual(self.scan(self.tok(rec, leg='return')).code, T.USED)


# ====================================================================== QR and manual agree
class ConsistencyTests(StoreCase):
    SCENARIOS = {}

    def two(self, method='card', legs=1, **kw):
        return self.issue(names=('Ann Lee', 'Bo Lee'), method=method, legs=legs, **kw)

    def compare(self, rec, now=None, **kw):
        qr = self.scan(self.tok(rec, 0), now=now, **kw)
        manual = self.scan(self.no(rec, 1), now=now, **kw)
        strip = lambda r: r.message.replace('Ann Lee', 'X').replace('Bo Lee', 'X').replace(self.no(rec, 0), 'N') \
            .replace(self.no(rec, 1), 'N')
        self.assertEqual((qr.ok, qr.code, strip(qr)), (manual.ok, manual.code, strip(manual)))
        return qr

    def test_same_ticket_state_gives_the_same_answer_by_qr_and_by_number(self):
        outcomes = {}
        outcomes['valid'] = self.compare(self.two())
        outcomes['unpaid'] = self.compare(self.two(method='cash'))
        outcomes['not yet valid'] = self.compare(self.two(), now=NOW - timedelta(days=2))
        outcomes['expired'] = self.compare(self.two(), now=NOW + timedelta(days=3))
        outcomes['test payment'] = self.compare(self.two(), environment='production')
        cancelled = self.two()
        for i in (0, 1):
            self.assertTrue(self.store.cancel_ticket(self.no(cancelled, i), self.admin, NOW)[0])
        outcomes['cancelled'] = self.compare(cancelled)
        refunded = self.two()
        for i in (0, 1):
            self.store.cancel_ticket(self.no(refunded, i), self.admin, NOW)
            self.store.record_refund(self.no(refunded, i), self.admin, NOW)
        outcomes['refunded'] = self.compare(refunded)
        used = self.two()
        self.scan(self.no(used, 0)); self.scan(self.no(used, 1))
        outcomes['used'] = self.compare(used)
        wrong = self.two()
        outcomes['wrong station'] = self.compare(wrong, station='Zord')
        self.assertEqual({k: v.code for k, v in outcomes.items()}, {
            'valid': 'OK', 'unpaid': 'UNPAID', 'not yet valid': 'NOT_YET_VALID', 'expired': 'EXPIRED',
            'test payment': 'TEST_PAYMENT', 'cancelled': 'CANCELLED', 'refunded': 'REFUNDED', 'used': 'USED',
            'wrong station': 'WRONG_STATION'})

    def test_in_memory_and_database_decisions_agree(self):
        rec = self.issue(method='cash')
        memory = lambda text, now=NOW: T.check_in_memory(self.fresh(), text, now, 'gate1', signer=self.signer,
                                                         environment='testing')
        self.assertEqual(memory(self.tok(rec)).code, self.scan(self.tok(rec)).code)    # both UNPAID
        self.paid_by_staff(rec)
        late = NOW + timedelta(days=3)
        self.assertEqual(memory(self.no(rec), late).code, self.scan(self.no(rec), now=late).code)
        self.assertEqual(memory(self.no(rec)).code, 'OK')                               # in-memory consumes its own copy
        self.assertTrue(self.scan(self.no(rec)).ok)                                     # the database copy is separate
        ok, msg, _ = core.validate_ticket(self.fresh(), self.tok(rec), NOW, 'gate1', signer=self.signer)
        self.assertFalse(ok); self.assertIn('ALREADY USED', msg)


class ImmediateReflectionTests(StoreCase):
    """Two windows (two connections) on the same database: a change in one is seen by the very next scan in the other."""

    def setUp(self):
        super().setUp()
        self.other = cta_db.Store.open(self.dir, self.NOW)
        self.addCleanup(self.other.close)

    def test_cancel_in_one_window_blocks_the_other_at_once(self):
        rec = self.issue()
        self.other.load_history()                                     # the second window has the ticket on screen
        self.assertTrue(self.store.cancel_ticket(self.no(rec), self.admin, NOW)[0])
        res = self.scan(self.tok(rec), store=self.other)
        self.assertEqual((res.ok, res.code), (False, T.CANCELLED))

    def test_payment_recorded_in_one_window_lets_the_other_admit(self):
        rec = self.issue(method='cash')
        self.assertEqual(self.scan(self.tok(rec), store=self.other).code, T.UNPAID)
        self.paid_by_staff(rec)
        self.assertTrue(self.scan(self.tok(rec), store=self.other).ok)

    def test_use_in_one_window_blocks_the_other_and_re_entry_reopens_it(self):
        rec = self.issue()
        self.assertTrue(self.scan(self.tok(rec)).ok)
        self.assertEqual(self.scan(self.no(rec), store=self.other).code, T.USED)
        self.assertTrue(self.store.allow_reentry(self.no(rec), self.admin, NOW + timedelta(minutes=2), 'gate fault')[0])
        self.assertTrue(self.scan(self.tok(rec), store=self.other, now=NOW + timedelta(minutes=3)).ok)

    def test_a_stale_window_cannot_un_use_a_ticket_by_saving_its_old_copy(self):
        rec = self.issue(method='cash')
        stale = self.other.load_history()                              # shows the ticket as unused and unpaid
        self.paid_by_staff(rec)
        self.assertTrue(self.scan(self.tok(rec)).ok)                   # used in the first window
        core.mark_paid(stale[0], NOW, 'admin')                         # the stale window now tries to save its copy
        with self.assertRaises(cta_storage.ConcurrentUpdate):
            self.other.save_history(stale, NOW, actor=self.admin)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'boarded')


# ====================================================================== cancellations, refunds, re-entry, corrections
class LifecycleTests(StoreCase):
    def test_cancelling_a_paid_ticket_makes_a_refund_due_and_recording_it_closes_it(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'))
        fare = rec['passengers'][0]['leg_fare_cents']
        ok, msg = self.store.cancel_ticket(self.no(rec, 0), self.admin, NOW, 'ill')
        self.assertTrue(ok, msg)
        self.assertIn(f'Refund due: {fare:,}', msg)
        h = self.fresh()[0]
        p = h['passengers'][0]
        self.assertEqual((p['status'], p['refund_state'], p['refund_cents'], p['cancelled_by'], p['cancel_reason']),
                         ('cancelled', 'due', fare, 'admin', 'ill'))
        s = stats.summarize([h], 'testing')
        self.assertEqual((s['refund_due_cents'], s['cancelled_tickets'], s['tickets']), (fare, 1, 1))
        self.assertEqual(s['collected_cents'], h['total_cents'])                        # money is still held until refunded
        ok, msg = self.store.record_refund(self.no(rec, 0), self.admin, NOW)
        self.assertTrue(ok, msg)
        h = self.fresh()[0]
        self.assertEqual((h['passengers'][0]['status'], h['payment']['refunded_cents']), ('refunded', fare))
        s = stats.summarize([h], 'testing')
        self.assertEqual((s['refund_due_cents'], s['collected_cents']), (0, h['total_cents'] - fare))
        self.assertEqual(s['revenue_cents'], h['total_cents'] - fare)
        self.assertFalse(self.store.record_refund(self.no(rec, 0), self.admin, NOW)[0])    # cannot refund twice
        self.assertFalse(self.store.record_refund(self.no(rec, 1), self.admin, NOW)[0])    # nothing due on a live ticket
        self.assertEqual(self.fresh()[0]['payment']['refunded_cents'], fare)

    def test_cancelled_and_refunded_tickets_are_not_no_shows(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'))
        self.store.cancel_ticket(self.no(rec, 0), self.admin, NOW)
        later = NOW + timedelta(hours=3)
        rows = stats.no_shows(self.fresh(), later)
        self.assertEqual([r['ticket_no'] for r in rows], [self.no(rec, 1)])

    def test_unpaid_counter_booking_cancellation_reduces_the_amount_due(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'), method='cash')
        fare = rec['passengers'][0]['leg_fare_cents']
        self.assertTrue(self.store.cancel_ticket(self.no(rec, 0), self.admin, NOW)[0])
        h = self.fresh()[0]
        self.assertEqual((h['payment']['amount_cents'], h['payment']['original_amount_cents'], h['payment']['status']),
                         (fare, fare * 2, 'pending_counter'))
        self.assertNotIn('refund_state', h['passengers'][0])                           # nothing was paid, nothing to refund
        self.assertEqual(stats.summarize([h], 'testing')['to_collect_cents'], fare)
        self.assertTrue(self.store.cancel_ticket(self.no(rec, 1), self.admin, NOW)[0])
        h = self.fresh()[0]
        self.assertEqual(h['payment']['status'], 'cancelled')
        self.assertFalse(core.mark_paid(h, NOW, 'admin')[0])                           # cannot pay for a cancelled booking
        self.assertEqual(stats.summarize([h], 'testing')['to_collect_cents'], 0)

    def test_used_cancelled_and_unconfirmed_tickets_cannot_be_cancelled(self):
        rec = self.issue()
        self.scan(self.tok(rec))
        ok, msg = self.store.cancel_ticket(self.no(rec), self.admin, NOW)
        self.assertFalse(ok); self.assertIn('already been used', msg)
        other = self.issue()
        self.assertTrue(self.store.cancel_ticket(self.no(other), self.admin, NOW)[0])
        self.assertIn('already cancelled', self.store.cancel_ticket(self.no(other), self.admin, NOW)[1])
        pending = self.issue(provider=FakePSP(claim='pending'), environment='production')
        ok, msg = self.store.cancel_ticket(self.no(pending), self.admin, NOW)
        self.assertFalse(ok); self.assertIn('not confirmed', msg)
        self.assertFalse(self.store.cancel_booking(pending['reference'], self.admin, NOW)[0])

    def test_refunding_money_taken_by_a_live_provider_needs_the_providers_reference(self):
        hist = self.fresh()
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'])
        psp = FakePSP(verified=good(rec['total_cents'], rec['reference']))
        rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, psp, environment='production')
        hist.append(rec)
        self.store.save_history(hist, NOW)
        no = rec['passengers'][0]['ticket_no']
        self.assertTrue(self.store.cancel_ticket(no, self.admin, NOW)[0])
        ok, msg = self.store.record_refund(no, self.admin, NOW)
        self.assertFalse(ok); self.assertIn('refund reference', msg)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'cancelled')
        self.assertTrue(self.store.record_refund(no, self.admin, NOW, 're_123')[0])
        self.assertEqual(self.fresh()[0]['passengers'][0]['refund_ref'], 're_123')

    def test_re_entry_needs_a_reason_a_used_ticket_and_the_travel_period(self):
        rec = self.issue()
        no = self.no(rec)
        self.assertIn('nothing to undo', self.store.allow_reentry(no, self.admin, NOW, 'gate fault')[1])
        self.scan(self.tok(rec))
        self.assertFalse(self.store.allow_reentry(no, self.admin, NOW, '   ')[0])
        with self.assertRaises(auth.PermissionDenied):
            self.store.allow_reentry(no, '', NOW, 'gate fault')
        ok, msg = self.store.allow_reentry(no, self.admin, datetime(2026, 10, 27, 9, 0), 'late')
        self.assertFalse(ok); self.assertIn('outside its travel period', msg)
        self.assertTrue(self.store.allow_reentry(no, self.admin, NOW, 'gate fault\nat Zord')[0])
        p = self.fresh()[0]['passengers'][0]
        self.assertEqual(p['status'], 'issued')
        self.assertNotIn('boarded_at', p)
        self.assertEqual(p['reentries'], [{'at': '2026-10-25 09:00:00', 'by': 'admin', 'reason': 'gate fault at Zord'}])
        self.assertTrue(self.scan(self.tok(rec)).ok)
        self.assertEqual(self.scan(self.tok(rec)).code, T.USED)                        # still single-use afterwards
        cancelled = self.issue()
        self.scan(self.no(cancelled))
        self.store.conn.execute("UPDATE tickets SET status = 'cancelled' WHERE ticket_no = ?", (self.no(cancelled),))
        self.assertIn('cancelled', self.store.allow_reentry(self.no(cancelled), self.admin, NOW, 'x')[1])

    def test_name_corrections_keep_the_same_qr_and_are_refused_once_used_or_cancelled(self):
        rec = self.issue(names=('Ann Lee', 'Bo Lee'))
        tok = self.tok(rec)
        ok, msg = self.store.correct_name(rec['reference'], 1, 'Anne Lee', self.admin, NOW)
        self.assertTrue(ok, msg)
        h = self.fresh()[0]
        self.assertEqual(h['passengers'][0]['name'], 'Anne Lee')
        self.assertEqual(h['passengers'][0]['corrections'], [{'at': '2026-10-25 09:00:00', 'by': 'admin', 'was': 'Ann Lee'}])
        self.assertEqual(core.ticket_qr_payload(h, h['passengers'][0], 'out', self.signer), tok)   # QR did not change
        self.assertIn('Anne Lee', self.scan(tok).message)
        self.assertFalse(self.store.correct_name(rec['reference'], 1, 'Anne Leigh', self.admin, NOW)[0])  # used
        self.assertTrue(self.store.cancel_ticket(self.no(rec, 1), self.admin, NOW)[0])
        self.assertFalse(self.store.correct_name(rec['reference'], 2, 'Bob Lee', self.admin, NOW)[0])     # cancelled
        fresh = self.issue()
        self.assertFalse(self.store.correct_name(fresh['reference'], 1, '12345', self.admin, NOW)[0])
        self.assertFalse(self.store.correct_name(fresh['reference'], 1, 'Ann Lee', self.admin, NOW)[0])    # unchanged
        with self.assertRaises(auth.PermissionDenied):
            self.store.correct_name(fresh['reference'], 1, 'Ann Leigh', '', NOW)
        self.assertFalse(self.store.correct_name(fresh['reference'], 9, 'Ann Leigh', self.admin, NOW)[0])

    def test_every_action_needs_a_staff_member_and_is_audited_without_personal_data(self):
        rec = self.issue(names=('Zoë Ünal',))
        no = self.no(rec)
        for action in (lambda: self.store.cancel_ticket(no, '', NOW), lambda: self.store.cancel_booking(rec['reference'], ' ', NOW),
                       lambda: self.store.record_refund(no, '', NOW), lambda: self.store.allow_reentry(no, '', NOW, 'x'),
                       lambda: self.store.correct_name(rec['reference'], 1, 'Zoe Unal', '', NOW)):
            with self.assertRaises(auth.PermissionDenied):
                action()
        self.assertTrue(self.store.correct_name(rec['reference'], 1, 'Zoe Unal', self.admin, NOW)[0])
        self.assertTrue(self.store.cancel_ticket(no, self.admin, NOW, 'ill')[0])
        self.assertTrue(self.store.record_refund(no, self.admin, NOW)[0])
        kinds = [r[0] for r in self.store.conn.execute("SELECT kind FROM audit WHERE kind LIKE 'ticket.%'")]
        self.assertEqual(kinds, ['ticket.name_corrected', 'ticket.cancelled', 'ticket.refunded'])
        audit = json.dumps(list(self.store.conn.execute('SELECT actor, detail FROM audit')), ensure_ascii=False)
        for private in ('Zoë', 'Ünal', 'Zoe', 'Unal'):
            self.assertNotIn(private, audit)

    def test_lifecycle_changes_are_atomic_if_the_write_fails(self):
        rec = self.issue()
        with mock.patch.object(cta_db.Store, '_insert_rows', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(cta_storage.StorageError):
                self.store.cancel_ticket(self.no(rec), self.admin, NOW)
        self.assertEqual(self.fresh()[0]['passengers'][0]['status'], 'issued')
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM audit WHERE kind = 'ticket.cancelled'").fetchone()[0], 0)

    def test_history_validation_knows_the_new_fields(self):
        rec = self.issue(legs=2)
        good_rec = json.loads(json.dumps(self.fresh()[0]))
        core.validate_history([good_rec])
        def variant(change):
            r = json.loads(json.dumps(good_rec)); change(r); return [r]
        bad = {
            'malformed id': variant(lambda r: r['passengers'][0].update(ticket_id='short')),
            'repeated id': variant(lambda r: r['passengers'][0].update(return_ticket_id=r['passengers'][0]['ticket_id'])),
            'status': variant(lambda r: r['passengers'][0].update(status='void')),
            'uses over max': variant(lambda r: r['passengers'][0].update(uses=2, max_uses=1)),
            'refund state': variant(lambda r: r['passengers'][0].update(refund_state='maybe')),
            'refund amount': variant(lambda r: r['passengers'][0].update(refund_cents=-5)),
        }
        for why, data in bad.items():
            with self.assertRaises(cta_storage.DataInvalid, msg=why):
                core.validate_history(data)
        self.assertEqual(rec['reference'], good_rec['reference'])


# ====================================================================== ids, migration and upgrade
V1_REWIND = """
DROP TRIGGER tickets_never_deleted; DROP TRIGGER tickets_id_fixed;
DROP TRIGGER scans_no_update; DROP TRIGGER scans_no_delete;
DROP TRIGGER audit_no_update; DROP TRIGGER audit_no_delete;
DROP INDEX tickets_id_unique; DROP INDEX tickets_by_booking;
CREATE TABLE tickets_v1 (ticket_no TEXT PRIMARY KEY COLLATE NOCASE, reference TEXT NOT NULL,
    passenger_no INTEGER NOT NULL, leg TEXT NOT NULL CHECK (leg IN ('out', 'return')),
    status TEXT CHECK (status IS NULL OR status IN ('issued', 'boarded')), boarded_at TEXT, boarded_by TEXT,
    FOREIGN KEY (reference, passenger_no) REFERENCES passengers(reference, no));
INSERT INTO tickets_v1 SELECT ticket_no, reference, passenger_no, leg, status, boarded_at, boarded_by FROM tickets;
DROP TABLE tickets; ALTER TABLE tickets_v1 RENAME TO tickets;
CREATE INDEX tickets_by_booking ON tickets(reference);
CREATE TABLE scans_v1 (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, ticket_no TEXT NOT NULL,
    staff TEXT, ok INTEGER NOT NULL, result TEXT NOT NULL);
INSERT INTO scans_v1 (at, ticket_no, staff, ok, result) SELECT at, ticket_no, staff, ok, result FROM scans;
DROP TABLE scans; ALTER TABLE scans_v1 RENAME TO scans;
ALTER TABLE audit DROP COLUMN chain; ALTER TABLE audit DROP COLUMN target; ALTER TABLE audit DROP COLUMN outcome;
ALTER TABLE staff DROP COLUMN disabled; DROP TABLE sessions; DROP TABLE login_attempts;
UPDATE meta SET value = '1' WHERE key = 'schema_version';
"""


class MigrationAndUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_json_installs_get_ticket_ids_after_migration_and_repeating_it_changes_nothing(self):
        hist, accounts, staff = legacy_install(self.dir)
        for rec in hist:                                                       # a real 2.3 install has no ticket ids
            for p in rec.get('passengers', []):
                p.pop('ticket_id', None); p.pop('return_ticket_id', None)
        core.save_history(self.dir / 'history.json', hist)
        store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(store.close)
        got = store.load_history()
        ids = [p[k] for r in got for p in r.get('passengers', []) for k in ('ticket_id', 'return_ticket_id') if k in p]
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(core.TICKET_ID_RE.match(i) for i in ids))
        self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM tickets WHERE ticket_id IS NULL').fetchone()[0], 0)
        self.assertEqual([r['reference'] for r in got], [r['reference'] for r in hist])      # identifiers preserved
        report = cta_db.migrate_json(store, self.dir, NOW)
        self.assertTrue(report.verified)
        self.assertEqual((report.conflicts, report.added['bookings'], report.unchanged['bookings']), ([], 0, 3))
        self.assertEqual(store.backfill_ticket_ids(), 0)
        self.assertEqual([p['ticket_id'] for r in store.load_history() for p in r.get('passengers', [])],
                         [p['ticket_id'] for r in got for p in r.get('passengers', [])])      # ids did not change

    def test_old_database_is_upgraded_with_a_backup_and_nothing_is_lost(self):
        sample = cta_db.Store.open(self.dir, NOW)
        signer = T.TicketSigner([test_key()])
        hist = []
        for names, legs in ((('Ann Lee', 'Bo Lee'), 1), (('Cy Lee',), 2)):
            route = rt.astar('Centrala', 'Zord')
            rec = core.make_record(route, {'Adult': len(names)}, 3, NOW, NOW, hist, names=list(names), legs=legs,
                                   account_phone='+447123456789')
            rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
            hist.append(rec)
        sample.save_history(hist, NOW)
        self.assertTrue(sample.check_in(hist[0]['passengers'][0]['ticket_no'], signer=signer, now=NOW,
                                        operator=sign_in(sample, 'gate1'),
                                        environment='testing').ok)
        before = {r['reference']: cta_db._comparable(r) for r in sample.load_history()}
        sample.conn.execute("INSERT INTO scans (at, ticket_no, staff, ok, result) VALUES "
                            "('2026-10-24 10:00:00', 'X', 'a', 1, 'Zoë Ünal: OK to travel')")
        sample.close()
        raw = sqlite3.connect(str(self.dir / 'cta.db'), isolation_level=None)
        raw.executescript(V1_REWIND)
        raw.close()

        with mock.patch('cta_db.os.access', return_value=False):                         # read-only cannot upgrade
            with self.assertRaises(cta_storage.DataInvalid) as cm:
                cta_db.Store.open(self.dir, NOW)
        self.assertIn('upgraded', str(cm.exception))

        store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version, cta_db.SCHEMA_VERSION)                 # all the way to current
        self.assertEqual(store.meta_get('schema_version'), str(cta_db.SCHEMA_VERSION))
        backups = list((self.dir / 'backups').glob('cta-*-pre-upgrade.db'))
        self.assertEqual(len(backups), 1)
        old = cta_db.Store(backups[0], read_only=True, allow_old=True)
        self.assertEqual(old.schema_version, 1)
        old.close()
        after = {r['reference']: r for r in store.load_history()}
        self.assertEqual({k: cta_db._comparable(v) for k, v in after.items()}, before)   # same bookings, states, payments
        self.assertEqual(after[hist[0]['reference']]['passengers'][0]['status'], 'boarded')
        ids = [p[k] for r in after.values() for p in r['passengers'] for k in ('ticket_id', 'return_ticket_id') if k in p]
        self.assertEqual((len(ids), len(set(ids))), (4, 4))
        self.assertEqual(store.conn.execute('SELECT uses FROM tickets WHERE status = ?', ('boarded',)).fetchone()[0], 1)
        self.assertEqual({r['result'] for r in store.scans()}, {'OK'})                   # the name is gone from the log
        self.assertNotIn('Zoë', json.dumps(store.scans(), ensure_ascii=False))
        with self.assertRaises(sqlite3.DatabaseError):
            store.conn.execute('DELETE FROM tickets')                                    # guards exist after the upgrade
        store.close()
        again = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(again.close)
        self.assertEqual(len(list((self.dir / 'backups').glob('cta-*-pre-upgrade.db'))), 1)   # not upgraded twice
        self.assertEqual(again.verify(), [])

    def test_an_old_backup_can_be_restored_and_is_upgraded_when_opened(self):
        store = cta_db.Store.open(self.dir, NOW)
        hist = []
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'])
        rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
        store.save_history([rec], NOW)
        store.close()
        raw = sqlite3.connect(str(self.dir / 'cta.db'), isolation_level=None)
        raw.executescript(V1_REWIND)
        raw.close()
        old_backup = self.dir / 'old-v1-backup.db'
        old_backup.write_bytes((self.dir / 'cta.db').read_bytes())
        (self.dir / 'cta.db').write_bytes(b'\x00' * 4096)                                # destroyed
        cta_db.restore_backup(old_backup, self.dir, NOW)
        restored = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(restored.close)
        self.assertEqual(restored.schema_version, cta_db.SCHEMA_VERSION)
        self.assertEqual(restored.load_history()[0]['reference'], rec['reference'])


# ====================================================================== PDF
class PdfTests(StoreCase):
    def setUp(self):
        super().setUp()
        try:
            import pypdf  # noqa: F401
            import reportlab  # noqa: F401
        except ImportError:
            self.skipTest('pypdf / reportlab missing')

    def pages(self, rec, **kw):
        from pypdf import PdfReader
        path = self.dir / f"{rec['reference']}.pdf"
        core.export_ticket_pdf(rec, path, **kw)
        return [pg.extract_text() or '' for pg in PdfReader(str(path)).pages]

    def test_every_page_prints_the_signed_token_of_its_own_journey(self):
        import cta_pdf
        rec = self.issue(names=('Ann Lee', 'Bo Lee'), legs=2)
        drawn = []
        real = cta_pdf._qr
        with mock.patch.object(cta_pdf, '_qr', side_effect=lambda c, payload, *a: (drawn.append(payload), real(c, payload, *a))[1]):
            self.pages(rec, signer=self.signer)
        wanted = [self.tok(rec, i, leg) for i in (0, 1) for leg in ('out', 'return')]
        tokens = [text.rsplit(' / ', 1)[-1] for text in drawn]          # readable details, then the signed token
        self.assertEqual(tokens, wanted)
        for text, token in zip(drawn, tokens):
            self.assertIsNotNone(self.signer.verify(token))
            self.assertEqual(T.parse_presented(text, self.signer).ticket_id, self.signer.verify(token))
        self.assertIn('ANN LEE', drawn[0])                                  # a phone shows whose ticket it is
        self.assertIn('BO LEE', drawn[2])
        for text in drawn:
            self.assertNotIn('cents', text)                                 # never the price or phone number
            self.assertNotIn('+44', text)

    def test_cancelled_and_refunded_journeys_are_marked_on_the_ticket(self):
        rec = self.issue(legs=2)
        self.store.cancel_ticket(self.no(rec, leg='return'), self.admin, NOW)
        pages = self.pages(self.fresh()[0], signer=self.signer)
        self.assertNotIn('CANCELLED', pages[0])
        self.assertIn('CANCELLED', pages[1])
        self.store.record_refund(self.no(rec, leg='return'), self.admin, NOW)
        self.assertIn('REFUNDED', self.pages(self.fresh()[0], signer=self.signer)[1])

    def test_without_a_signing_key_no_qr_is_printed_but_the_ticket_number_is(self):
        import cta_pdf
        rec = self.issue()
        with mock.patch.dict(os.environ):
            os.environ.pop('CTA_TICKET_KEY', None)
            T.set_default_signer(None)
            with mock.patch.object(cta_pdf, '_qr') as qr:
                pages = self.pages(rec)
        qr.assert_not_called()
        self.assertIn('QR unavailable', pages[0])
        self.assertIn(self.no(rec), pages[0])

    def test_pdf_pages_do_not_expose_the_recovery_code_or_signature(self):
        rec = self.issue()
        pages = self.pages(rec, signer=self.signer)
        self.assertNotIn(rec['recovery_code'], pages[0])                             # only the private receipt page has it
        self.assertIn(rec['recovery_code'], pages[-1])
        self.assertNotIn(self.tok(rec).split(':')[3], ' '.join(pages))


# ====================================================================== the application
class AppTicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.root = tk.Tk()
        except Exception:
            raise unittest.SkipTest('no display / tkinter')
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.tmp = tempfile.mkdtemp()
        self.clock = lambda: datetime(2026, 10, 24, 14, 2, 10)

    def app(self):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=self.clock, folder=self.tmp)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def book(self, app, method='card', names=('Ann Lee',)):
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(len(names))
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(list(names), method)
        return app.history[-1]

    def test_the_app_loads_a_signer_and_new_tickets_carry_ids(self):
        app = self.app()
        self.assertIsNotNone(app.signer)
        self.assertTrue(app.can_issue())
        rec = self.book(app)
        p = rec['passengers'][0]
        self.assertTrue(core.TICKET_ID_RE.match(p['ticket_id']))
        self.assertTrue((Path(self.tmp) / f"{rec['reference']}.pdf").is_file())

    def test_admin_check_in_accepts_a_scanned_token_once_and_logs_no_names(self):
        app = self.app()
        rec = self.book(app, names=('Zoë Ünal',))
        app.staff = sign_in(app.store, 'alice')
        token = app.signer.sign(rec['passengers'][0]['ticket_id'])
        app.admin._check_in(token + '\r\n')                                           # a scanner adds a line ending
        self.assertIn('OK to travel', app.admin.result.cget('text'))
        self.assertIn('Zoë Ünal', app.admin.result.cget('text'))                      # staff see the name on screen
        app.admin._check_in(rec['passengers'][0]['ticket_no'])
        self.assertIn('ALREADY USED', app.admin.result.cget('text'))
        app.admin._check_in(token[:-2] + ('BB' if token.endswith('AA') else 'AA'))      # always really different
        self.assertIn('FORGED', app.admin.result.cget('text'))
        rows = app.store.scans()
        self.assertEqual([(r['result'], r['method'], r['staff']) for r in rows],
                         [('OK', 'qr', 'alice'), ('USED', 'manual', 'alice'), ('FORGED', 'qr', 'alice')])
        self.assertEqual(rows[0]['device'], app.device_id)
        self.assertNotIn('Zoë', json.dumps(rows, ensure_ascii=False))
        self.assertEqual(app.history[0]['passengers'][0]['status'], 'boarded')        # the window reloaded the new state

    def test_admin_actions_cancel_refund_reentry_and_correct_name(self):
        app = self.app()
        rec = self.book(app)
        app.staff = sign_in(app.store, 'alice')
        tno = rec['passengers'][0]['ticket_no']
        app.admin.tree.selection = lambda: (tno,)
        app.admin._check_in(tno)
        app.admin.reentry_selected('scanned by mistake')
        self.assertIn('re-entry allowed', app.admin.result.cget('text'))
        app.admin.correct_name_selected('Anne Lee')
        self.assertIn('Name corrected', app.admin.result.cget('text'))
        self.assertEqual(app.history[0]['passengers'][0]['name'], 'Anne Lee')
        app.admin.cancel_selected('ill')
        self.assertIn('cancelled', app.admin.result.cget('text'))
        self.assertEqual(app.admin._status_text({'status': 'cancelled', 'refund_state': 'due'}), 'cancelled - refund due')
        app.admin.refund_selected('')
        self.assertIn('Refund of', app.admin.result.cget('text'))
        app.admin._check_in(tno)
        self.assertIn('REFUNDED', app.admin.result.cget('text'))
        self.assertEqual(app.history[0]['passengers'][0]['status'], 'refunded')
        app.admin.cancel_selected('again')
        self.assertIn('already', app.admin.result.cget('text'))

    def test_a_damaged_database_blocks_check_in_with_a_clear_message(self):
        app = self.app()
        rec = self.book(app)
        app.staff = sign_in(app.store, 'alice')
        app.storage_problem = 'The bookings file is damaged.'
        app.admin._check_in(rec['passengers'][0]['ticket_no'])
        self.assertIn('damaged', app.admin.result.cget('text'))
        self.assertEqual(app.store.scans(), [])

    def test_a_missing_key_file_pauses_new_bookings_but_manual_check_in_still_works(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('CTA_TICKET_KEY', None)
            first = self.app()
            self.assertIsNotNone(first.signer)
            rec = self.book(first)
            first.store.close()
            (Path(self.tmp) / T.KEY_FILE).unlink()                                    # the key file is lost
            T.set_default_signer(None)
            app = self.app()
        self.assertIsNone(app.signer)
        self.assertIn('missing', app.key_problem)
        self.assertFalse(app.can_issue())
        self.assertFalse((Path(self.tmp) / T.KEY_FILE).exists())
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        with mock.patch('cta_app.messagebox') as box:
            app.book()
        self.assertEqual(box.showerror.call_args[0][0], 'Bookings are paused')
        self.assertEqual(len(app.history), 1)
        app.staff = sign_in(app.store, 'alice')
        app.admin._check_in('CTA1:8E85:' + 'A' * 26 + ':' + 'B' * 26)
        self.assertIn('cannot be verified', app.admin.result.cget('text'))
        app.admin._check_in(rec['passengers'][0]['ticket_no'])
        self.assertIn('OK to travel', app.admin.result.cget('text'))

    def test_ticket_view_shows_the_journey_status(self):
        app = self.app()
        rec = self.book(app)
        app.staff = sign_in(app.store, 'alice')
        import tkinter as tk
        def labels(record):
            frame = app.ticket_view(self.root, record)
            texts = []
            def walk(w):
                for c in w.winfo_children():
                    if isinstance(c, tk.Label):
                        texts.append(str(c.cget('text')))
                    walk(c)
            walk(frame)
            frame.destroy()
            return ' | '.join(texts)
        self.assertIn('Not used yet', labels(app.history[0]))
        app.admin._check_in(rec['passengers'][0]['ticket_no'])
        self.assertIn('USED - already checked in', labels(app.history[0]))
        app.admin.tree.selection = lambda: (rec['passengers'][0]['ticket_no'],)
        app.admin.reentry_selected('gate fault')
        app.admin.cancel_selected('ill')
        self.assertIn('CANCELLED - not valid for travel', labels(app.history[0]))


if __name__ == '__main__':
    unittest.main()
