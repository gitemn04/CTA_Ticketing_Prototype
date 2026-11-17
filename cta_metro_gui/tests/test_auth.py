"""Module 5 tests: every sensitive operation is authorised at the service layer and leaves an audit trail."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ['CTA_ENV'] = 'testing'
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)

from test_all import PRODUCTION_PBKDF2_ITERATIONS, TEST_PASSWORD, sign_in      # also sets the fast test settings
import cta_accounts as acct
import cta_auth as auth
import cta_core as core
import cta_db
import cta_payments as pay
import cta_routing as rt
import cta_storage
import cta_tickets as T

NOW = datetime(2026, 10, 25, 9, 0)
ALL_ROLES = tuple(auth.ROLE_PERMISSIONS)


def audit_rows(store, kind=None):
    sql = 'SELECT id, at, kind, outcome, actor, target, reference, detail FROM audit'
    rows = store.conn.execute(sql + (' WHERE kind = ?' if kind else '') + ' ORDER BY id', (kind,) if kind else ()).fetchall()
    return [dict(zip(('id', 'at', 'kind', 'outcome', 'actor', 'target', 'reference', 'detail'), r)) for r in rows]


def everything_stored(store) -> str:
    """Every value in every table, as one string (to look for leaked secrets)."""
    out = []
    for (table,) in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        if table == 'staff':                    # holds the password HASHES by design; checked separately
            continue
        for row in store.conn.execute(f'SELECT * FROM "{table}"'):
            out.append(' '.join(str(v) for v in row))
    return '\n'.join(out)


class AuthCase(unittest.TestCase):
    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.dir = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(self.store.close)
        self.signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
        self.admin = sign_in(self.store, 'admin')
        roles = mock.patch.object(auth, 'ASSIGNABLE_ROLES', ALL_ROLES)       # the PROPOSED roles, for testing only
        roles.start()
        self.addCleanup(roles.stop)

    def issue(self, method='card', names=('Ann Lee',), legs=1):
        hist = self.store.load_history()
        route = rt.astar('Centrala', 'Zord')
        rec = core.make_record(route, {'Adult': len(names)}, 3, NOW, NOW, hist, names=list(names), legs=legs,
                               account_phone='+447123456789')
        rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
        hist.append(rec)
        self.store.save_history(hist, NOW)
        return rec

    def user(self, role):
        return sign_in(self.store, f'{role}1', role)


# ====================================================================== the permission matrix
class RoleMatrixTests(AuthCase):
    """Every role x every guarded operation: allowed exactly when the matrix says so."""

    def operations(self):
        """permission -> (prepare(), act(actor, prepared) -> raises PermissionDenied or returns)."""
        def check_in(actor, rec):
            res = self.store.check_in(rec['passengers'][0]['ticket_no'], signer=self.signer, now=NOW, operator=actor,
                                      environment='testing')
            if res.code == T.NOT_AUTHORIZED:
                raise auth.PermissionDenied(res.message)
            return res
        def used():
            rec = self.issue()
            self.store.check_in(rec['passengers'][0]['ticket_no'], signer=self.signer, now=NOW, operator=self.admin,
                                environment='testing')
            return rec
        def refund_due():
            rec = self.issue()
            self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], self.admin, NOW)
            return rec
        def edited(actor, rec):
            hist = self.store.load_history()
            hist[-1]['passengers'][0]['name'] = 'Edited Name'
            return self.store.save_history(hist, NOW, actor=actor)
        no = lambda rec: rec['passengers'][0]['ticket_no']
        return {
            'ticket.check_in': (self.issue, check_in),
            'ticket.reentry': (used, lambda a, r: self.store.allow_reentry(no(r), a, NOW, 'gate fault')),
            'ticket.cancel': (self.issue, lambda a, r: self.store.cancel_ticket(no(r), a, NOW, 'x')),
            'ticket.refund': (refund_due, lambda a, r: self.store.record_refund(no(r), a, NOW)),
            'ticket.correct_name': (self.issue, lambda a, r: self.store.correct_name(r['reference'], 1, 'Anne Lee', a, NOW)),
            'payment.mark_paid': (lambda: self.issue('cash'), lambda a, r: self.store.mark_paid(no(r), a, NOW)),
            'report.view': (lambda: None, lambda a, r: self.store.authorize(a, 'report.view', NOW)),
            'report.export': (lambda: None, lambda a, r: self.store.authorize(a, 'report.export', NOW)),
            'audit.read': (lambda: None, lambda a, r: self.store.read_audit(a, NOW)),
            'audit.export': (lambda: None, lambda a, r: self.store.export_audit_csv(a, self.dir / 'a.csv', NOW)),
            'support.reply': (lambda: self.store.open_case('problem', 'Printer jammed', NOW, topic='Other'),
                              lambda a, r: self.store.reply_case(r, a, 'Thanks, we are looking at it.', NOW)),
            'staff.manage': (lambda: None, lambda a, r: self.store.create_staff_account(
                a, f'new{len(self.store.load_staff())}', 'abc12345', 'gate', NOW)),
            'records.edit': (self.issue, edited),
        }

    def test_every_permission_has_a_guarded_operation(self):
        self.assertEqual(set(self.operations()), set(auth.PERMISSIONS))

    def test_each_role_can_do_exactly_what_the_matrix_allows(self):
        ops = self.operations()
        for role in ALL_ROLES:
            actor = self.user(role)
            for perm, (prepare, act) in ops.items():
                prepared = prepare()
                allowed = auth.role_allows(role, perm)
                with self.subTest(role=role, permission=perm):
                    if allowed:
                        act(actor, prepared)                              # must not raise
                    else:
                        with self.assertRaises(auth.PermissionDenied):
                            act(actor, prepared)

    def test_the_matrix_as_documented(self):
        self.assertEqual(auth.ROLE_PERMISSIONS['admin'], frozenset(auth.PERMISSIONS))
        self.assertEqual(auth.ROLE_PERMISSIONS['gate'], {'ticket.check_in'})
        self.assertEqual(auth.ROLE_PERMISSIONS['cashier'], {'ticket.check_in', 'payment.mark_paid', 'report.view'})
        self.assertNotIn('staff.manage', auth.ROLE_PERMISSIONS['supervisor'])
        self.assertNotIn('records.edit', auth.ROLE_PERMISSIONS['supervisor'])
        doc = (HERE.parent / 'docs' / 'PERMISSIONS.md').read_text(encoding='utf-8')
        for perm in auth.PERMISSIONS:
            self.assertIn(f'`{perm}`', doc)

    def test_unapproved_roles_cannot_be_given_to_anyone_yet(self):
        with mock.patch.object(auth, 'ASSIGNABLE_ROLES', ('admin',)):        # the real setting today
            for role in ('supervisor', 'cashier', 'gate'):
                with self.assertRaises(ValueError) as cm:
                    self.store.create_staff_account(self.admin, f'x{role}', 'abc12345', role, NOW)
                self.assertIn('approved', str(cm.exception))
        self.assertEqual(auth.MATRIX_STATUS, 'proposed')
        with self.assertRaises(ValueError):
            self.store.create_staff_account(self.admin, 'odd', 'abc12345', 'superuser', NOW)


# ====================================================================== calling the services directly
class BypassTests(AuthCase):
    def test_names_dicts_and_made_up_principals_are_not_staff(self):
        rec = self.issue()
        no = rec['passengers'][0]['ticket_no']
        fakes = ['admin', '', None, {'username': 'admin', 'role': 'admin'},
                 auth.Principal('admin', 'admin', 'made-up-token'),
                 auth.Principal('admin', 'admin', self.admin.token + 'x')]
        for fake in fakes:
            with self.subTest(actor=repr(fake)):
                with self.assertRaises(auth.PermissionDenied):
                    self.store.cancel_ticket(no, fake, NOW)
                with self.assertRaises(auth.PermissionDenied):
                    self.store.mark_paid(no, fake, NOW)
                res = self.store.check_in(no, signer=self.signer, now=NOW, operator=fake, environment='testing')
                self.assertEqual(res.code, T.NOT_AUTHORIZED)
        self.assertEqual(self.store.load_history()[0]['passengers'][0]['status'], 'issued')
        self.assertEqual(len(audit_rows(self.store, 'permission.denied')), len(fakes) * 3)

    def test_a_principal_cannot_promote_itself(self):
        gate = self.user('gate')
        forged = auth.Principal(gate.username, 'admin', gate.token)            # real session, claims a bigger role
        rec = self.issue()
        with self.assertRaises(auth.PermissionDenied):
            self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], forged, NOW)
        stolen = auth.Principal('admin', 'admin', gate.token)                  # someone else's session token
        with self.assertRaises(auth.PermissionDenied):
            self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], stolen, NOW)

    def test_editing_a_booking_in_memory_and_saving_it_is_refused(self):
        rec = self.issue('cash')
        hist = self.store.load_history()
        core.mark_paid(hist[0], NOW, 'admin')                                   # the old way: change the dict ...
        with self.assertRaises(auth.PermissionDenied):
            self.store.save_history(hist, NOW)                                  # ... and save it: refused
        with self.assertRaises(auth.PermissionDenied):
            self.store.save_history(hist, NOW, actor=self.user('cashier'))       # even cashiers use mark_paid
        self.assertEqual(self.store.load_history()[0]['payment']['status'], 'pending_counter')
        T.cancel_ticket(hist[0], rec['passengers'][0]['ticket_no'], 'admin', NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.save_history(hist, NOW)

    def test_new_bookings_cannot_arrive_already_used_paid_in_cash_or_refunded(self):
        route = rt.astar('Centrala', 'Zord')
        def fresh():
            hist = self.store.load_history()
            r = core.make_record(route, {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'])
            r['payment'] = pay.pay('cash', r['total_cents'], r['reference'], 'x', NOW, environment='testing')
            return hist, r
        tweaks = {
            'already used': lambda r: r['passengers'][0].update(status='boarded', boarded_at='x'),
            'cash already paid': lambda r: core.mark_paid(r, NOW, 'someone'),
            'already cancelled': lambda r: r['passengers'][0].update(status='cancelled', cancelled_at='x'),
            'multi-ride': lambda r: r['passengers'][0].update(max_uses=50),
        }
        for why, tweak in tweaks.items():
            hist, r = fresh()
            tweak(r)
            with self.assertRaises(auth.PermissionDenied, msg=why):
                self.store.save_history(hist + [r], NOW)
        hist, r = fresh()
        self.assertEqual(self.store.save_history(hist + [r], NOW), 1)          # the normal booking still works

    def test_staff_list_cannot_be_overwritten_directly(self):
        staff = self.store.load_staff()
        staff[0]['role'] = 'admin'
        with self.assertRaises(auth.PermissionDenied):
            self.store.save_staff(staff + [acct.hash_password('x') and {'username': 'evil', 'role': 'admin',
                                                                        'password': acct.hash_password('evil1234')}])
        self.assertEqual([s['username'] for s in self.store.load_staff()], ['admin'])
        with self.assertRaises(auth.PermissionDenied):
            self.store.bootstrap_admin('second', 'abc12345', NOW)              # only the very first admin

    def test_sessions_end_on_sign_out_expiry_disable_and_password_reset(self):
        rec = self.issue()
        no = rec['passengers'][0]['ticket_no']
        a = sign_in(self.store, 'admin')
        self.store.staff_logout(a, NOW)
        with self.assertRaises(auth.PermissionDenied) as cm:
            self.store.cancel_ticket(no, a, NOW)
        self.assertIn('sign in', str(cm.exception))
        with mock.patch.object(auth, 'SESSION_HOURS', 12):
            short = self.store.staff_login('admin', TEST_PASSWORD, NOW)
        self.store.authorize(short, 'report.view', NOW + timedelta(hours=11))
        with self.assertRaises(auth.PermissionDenied):
            self.store.authorize(short, 'report.view', NOW + timedelta(hours=13))
        sup = self.user('supervisor')
        self.store.authorize(sup, 'ticket.cancel', NOW)
        self.store.set_staff_enabled(self.admin, sup.username, False, NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.authorize(sup, 'ticket.cancel', NOW)                    # signed out everywhere at once
        self.store.set_staff_enabled(self.admin, sup.username, True, NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.authorize(sup, 'ticket.cancel', NOW)                    # old session stays dead
        sup2 = self.store.staff_login(sup.username, TEST_PASSWORD, NOW)
        self.store.reset_staff_password(self.admin, sup.username, 'n3w-pass-word', NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.authorize(sup2, 'ticket.cancel', NOW)

    def test_a_role_change_takes_effect_on_the_next_operation(self):
        sup = self.user('supervisor')
        rec = self.issue()
        self.store.set_staff_role(self.admin, sup.username, 'gate', NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], sup, NOW)  # its Principal still says supervisor

    def test_the_cli_needs_a_real_admin_password(self):
        import cta_staff
        lines = []
        answers = iter(['wrong-password'])
        rc = cta_staff.main(['list', '--folder', str(self.dir), '--as', 'admin'], ask=lambda p: next(answers),
                            out=lines.append)
        self.assertEqual(rc, 1); self.assertIn('Wrong username or password', lines[-1])
        gate = self.user('gate')
        answers = iter([TEST_PASSWORD])
        rc = cta_staff.main(['add', 'eve1', '--folder', str(self.dir), '--as', gate.username],
                            ask=lambda p: next(answers), out=lines.append)
        self.assertEqual(rc, 1); self.assertIn('not allowed', lines[-1])
        answers = iter([TEST_PASSWORD, 'abc12345', 'abc12345'])
        rc = cta_staff.main(['add', 'carol1', '--folder', str(self.dir), '--as', 'admin'], ask=lambda p: next(answers),
                            out=lines.append)
        self.assertEqual(rc, 0, lines)
        answers = iter([TEST_PASSWORD])
        cta_staff.main(['audit', '--folder', str(self.dir), '--as', 'admin', '--out', str(self.dir / 'x.csv')],
                       ask=lambda p: next(answers), out=lines.append)
        self.assertTrue((self.dir / 'x.csv').is_file())
        self.assertNotIn(TEST_PASSWORD, (self.dir / 'x.csv').read_text(encoding='utf-8-sig'))


# ====================================================================== sign-in, lockout, passwords, OTP
class SignInTests(AuthCase):
    def test_wrong_passwords_lock_the_account_for_every_window(self):
        other = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(other.close)
        for i in range(auth.MAX_FAILURES - 1):
            with self.assertRaises(auth.AuthError) as cm:
                (self.store if i % 2 else other).staff_login('admin', 'nope-1234', NOW)
            self.assertEqual(str(cm.exception), 'Wrong username or password.')
        with self.assertRaises(auth.AuthError) as cm:
            self.store.staff_login('admin', 'nope-1234', NOW)
        self.assertIn('Too many', str(cm.exception))
        for store in (self.store, other):                                      # the right password waits too
            with self.assertRaises(auth.AuthError):
                store.staff_login('admin', TEST_PASSWORD, NOW + timedelta(seconds=10))
        self.store.staff_login('admin', TEST_PASSWORD, NOW + timedelta(seconds=auth.LOCK_SECONDS + 1))
        outcomes = [r['outcome'] for r in audit_rows(self.store, 'auth.login')]
        self.assertEqual(outcomes.count('failure'), auth.MAX_FAILURES)
        self.assertEqual(outcomes.count('locked'), 2)

    def test_unknown_usernames_behave_like_wrong_passwords(self):
        with self.assertRaises(auth.AuthError) as cm:
            self.store.staff_login('nobody', 'whatever1', NOW)
        self.assertEqual(str(cm.exception), 'Wrong username or password.')
        for _ in range(auth.MAX_FAILURES):
            try:
                self.store.staff_login('ghost', 'whatever1', NOW)
            except auth.AuthError as exc:
                last = str(exc)
        self.assertIn('Too many', last)                                       # same lockout, so no way to tell

    def test_disabled_accounts_cannot_sign_in(self):
        self.user('cashier')
        self.store.set_staff_enabled(self.admin, 'cashier1', False, NOW)
        with self.assertRaises(auth.AuthError) as cm:
            self.store.staff_login('cashier1', TEST_PASSWORD, NOW)
        self.assertIn('disabled', str(cm.exception))

    def test_password_hashing_parameters_and_upgrade(self):
        self.assertEqual(PRODUCTION_PBKDF2_ITERATIONS, 600_000)                # the shipped setting (tests use fewer)
        h = acct.hash_password('abc12345')
        self.assertEqual(set(h), {'salt', 'hash', 'iterations'})
        self.assertEqual(len(bytes.fromhex(h['salt'])), 16)
        self.assertNotIn('abc12345', json.dumps(h))
        self.assertFalse(acct.check_password('abc12345', dict(h, iterations=10 ** 9)))   # a tampered count is refused
        self.assertFalse(acct.check_password('x' * 500, h))
        self.assertIn('at most', acct.validate_password('a1' * 100))
        old = acct.hash_password(TEST_PASSWORD, iterations=10)                 # an account from an older version
        doc = json.loads(self.store.conn.execute("SELECT doc FROM staff WHERE username = 'admin'").fetchone()[0])
        doc['password'] = old
        self.store.conn.execute("UPDATE staff SET doc = ? WHERE username = 'admin'", (json.dumps(doc),))
        self.store.staff_login('admin', TEST_PASSWORD, NOW)
        stored = json.loads(self.store.conn.execute("SELECT doc FROM staff WHERE username = 'admin'").fetchone()[0])
        self.assertEqual(stored['password']['iterations'], acct.PBKDF2_ITERATIONS)  # upgraded while signing in
        self.store.staff_login('admin', TEST_PASSWORD, NOW)

    def test_staff_management_rules(self):
        self.store.create_staff_account(self.admin, 'bob1', 'abc12345', 'cashier', NOW)
        for bad, why in ((('bob1', 'abc12345', 'cashier'), 'taken'), (('b', 'abc12345', 'cashier'), 'Username'),
                         (('bob2', 'short', 'cashier'), 'characters'), (('bob3', 'abcdefgh', 'cashier'), 'digit')):
            with self.assertRaises(ValueError) as cm:
                self.store.create_staff_account(self.admin, *bad, NOW)
            self.assertIn(why, str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            self.store.set_staff_enabled(self.admin, 'admin', False, NOW)
        self.assertIn('your own', str(cm.exception))
        other_admin = self.user('admin')                                       # 'admin1'
        with self.assertRaises(ValueError):
            self.store.set_staff_enabled(other_admin, 'admin1', False, NOW)
        self.store.set_staff_enabled(other_admin, 'admin', False, NOW)         # two admins: one may go
        with self.assertRaises(ValueError) as cm:
            self.store.set_staff_role(other_admin, 'admin1', 'gate', NOW)      # but never the last one
        self.assertIn('last active administrator', str(cm.exception))
        listing = {r['username']: r for r in self.store.list_staff(other_admin, NOW)}
        self.assertTrue(listing['admin']['disabled'])
        self.assertEqual(listing['bob1']['role'], 'cashier')
        self.assertNotIn('password', json.dumps(listing))
        with self.assertRaises(ValueError):
            self.store.set_staff_enabled(other_admin, 'nobody', True, NOW)

    def test_password_hashing_never_holds_the_database_lock(self):
        """PBKDF2 takes ~0.2 s at 600,000 rounds; doing it inside a write transaction would pause every booking and
        check-in in every window while someone signs in."""
        calls = []
        real_hash, real_check = acct.hash_password, acct.check_password

        def watch(fn):
            def inner(*a, **k):
                calls.append(self.store.conn.in_transaction)
                return fn(*a, **k)
            return inner
        old = acct.hash_password(TEST_PASSWORD, iterations=10)                 # makes the next sign-in re-hash
        doc = json.loads(self.store.conn.execute("SELECT doc FROM staff WHERE username = 'admin'").fetchone()[0])
        doc['password'] = old
        self.store.conn.execute("UPDATE staff SET doc = ? WHERE username = 'admin'", (json.dumps(doc),))
        with mock.patch.object(acct, 'hash_password', side_effect=watch(real_hash)), \
                mock.patch.object(acct, 'check_password', side_effect=watch(real_check)):
            admin = self.store.staff_login('admin', TEST_PASSWORD, NOW)                 # check + re-hash
            try:
                self.store.staff_login('admin', 'wrong-pass-1', NOW)                    # failed check
            except auth.AuthError:
                pass
            self.store.create_staff_account(admin, 'newbie1', 'abc12345', 'gate', NOW)  # hash
            self.store.reset_staff_password(admin, 'newbie1', 'abc123456', NOW)         # hash
            with tempfile.TemporaryDirectory() as d:
                fresh = cta_db.Store.open(Path(d), NOW)
                calls_before = len(calls)
                fresh.bootstrap_admin('boss', 'abc12345', NOW)
                self.assertGreater(len(calls), calls_before)
                fresh.close()
        self.assertTrue(calls)
        self.assertEqual(calls, [False] * len(calls))

    def test_a_password_changed_during_sign_in_is_not_accepted(self):
        real_check = acct.check_password

        def check_then_reset(password, stored):
            ok = real_check(password, stored)                                  # verified against the OLD hash ...
            doc = json.loads(self.store.conn.execute("SELECT doc FROM staff WHERE username = 'admin'").fetchone()[0])
            doc['password'] = acct.hash_password('brand-new-1')               # ... while an admin resets it
            self.store.conn.execute("UPDATE staff SET doc = ? WHERE username = 'admin'", (json.dumps(doc),))
            return ok
        with mock.patch.object(acct, 'check_password', side_effect=check_then_reset):
            with self.assertRaises(auth.AuthError) as cm:
                self.store.staff_login('admin', TEST_PASSWORD, NOW)
        self.assertIn('changed while you were signing in', str(cm.exception))
        with self.assertRaises(auth.AuthError):
            self.store.staff_login('admin', TEST_PASSWORD, NOW)                 # the old password no longer works
        self.store.staff_login('admin', 'brand-new-1', NOW)

    def test_passenger_codes_still_expire_are_single_use_and_lock_out(self):
        o = acct.OtpSession('+447123456789', NOW)
        self.assertEqual((o.TTL, o.MAX_ATTEMPTS, o.LENGTH), (timedelta(minutes=5), 3, 6))
        code = o.demo_code
        self.assertFalse(o.verify(code, NOW + timedelta(minutes=5, seconds=1))[0])   # expired
        o = acct.OtpSession('+447123456789', NOW)
        code = o.demo_code
        self.assertTrue(o.verify(code, NOW)[0])
        self.assertIn('already used', o.verify(code, NOW)[1])                         # single use
        o = acct.OtpSession('+447123456789', NOW)
        wrong = '000000' if o.demo_code != '000000' else '111111'
        for _ in range(3):
            o.verify(wrong, NOW)
        self.assertIn('Too many', o.verify(o.demo_code, NOW)[1])                      # locked even with the right code


# ====================================================================== the audit trail
class AuditTrailTests(AuthCase):
    def test_required_events_are_written_with_who_what_target_outcome_and_an_id(self):
        cash = self.issue('cash')
        paid = self.issue()
        sup = self.user('supervisor')
        try:
            self.store.staff_login('admin', 'bad-pass-1', NOW)
        except auth.AuthError:
            pass
        self.store.mark_paid(cash['passengers'][0]['ticket_no'], self.admin, NOW)
        self.store.cancel_ticket(paid['passengers'][0]['ticket_no'], sup, NOW, 'ill')
        self.store.record_refund(paid['passengers'][0]['ticket_no'], sup, NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.create_staff_account(sup, 'zed1', 'abc12345', 'gate', NOW)
        self.store.set_staff_role(self.admin, sup.username, 'cashier', NOW)
        self.store.set_staff_enabled(self.admin, sup.username, False, NOW)
        self.store.set_staff_enabled(self.admin, sup.username, True, NOW)
        self.store.reset_staff_password(self.admin, sup.username, 'n3w-pass-word', NOW)
        self.store.export_audit_csv(self.admin, self.dir / 'audit.csv', NOW)
        rows = self.store.read_audit(self.admin, NOW)
        self.store.staff_logout(self.admin, NOW)
        kinds = {r['kind'] for r in audit_rows(self.store)}
        for needed in ('staff.bootstrap', 'auth.login', 'auth.logout', 'payment.status_changed', 'ticket.cancelled',
                       'ticket.refunded', 'permission.denied', 'staff.created', 'staff.role_changed', 'staff.disabled',
                       'staff.enabled', 'staff.password_reset', 'audit.exported', 'audit.read', 'booking.created'):
            self.assertIn(needed, kinds)
        self.assertRegex(rows[0]['event_id'], r'^AUD-\d{8}$')
        by_kind = {r['kind']: r for r in audit_rows(self.store)}
        pay_evt = by_kind['payment.status_changed']
        self.assertEqual((pay_evt['actor'], pay_evt['outcome'], pay_evt['reference']), ('admin', 'success', cash['reference']))
        self.assertEqual(json.loads(pay_evt['detail']), {'from': 'pending_counter', 'to': 'paid',
                                                         'amount_cents': cash['total_cents']})
        denied = by_kind['permission.denied']
        self.assertEqual((denied['actor'], denied['outcome'], denied['target']), (sup.username, 'denied', 'zed1'))
        self.assertEqual(json.loads(denied['detail'])['permission'], 'staff.manage')
        logins = audit_rows(self.store, 'auth.login')
        self.assertIn('failure', [r['outcome'] for r in logins])
        self.assertEqual(by_kind['staff.disabled']['target'], sup.username)
        for r in audit_rows(self.store):
            self.assertTrue(r['at'] and r['kind'] and r['outcome'], r)

    def test_a_business_refusal_is_audited_as_a_failure(self):
        rec = self.issue()
        self.store.check_in(rec['passengers'][0]['ticket_no'], signer=self.signer, now=NOW, operator=self.admin,
                            environment='testing')
        ok, _ = self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], self.admin, NOW)   # used: cannot cancel
        self.assertFalse(ok)
        last = audit_rows(self.store, 'ticket.cancelled')[-1]
        self.assertEqual(last['outcome'], 'failure')

    def test_passwords_otps_and_session_tokens_never_reach_the_logs(self):
        with tempfile.TemporaryDirectory() as d:
            store = cta_db.Store.open(Path(d), NOW)
            secret, guess = 'Sup3r-Secret-pw', 'Wr0ng-Guess-77'
            boss = store.bootstrap_admin('boss', secret, NOW)
            for _ in range(3):
                try:
                    store.staff_login('boss', guess, NOW)
                except auth.AuthError:
                    pass
            store.staff_login('boss', secret, NOW)
            store.create_staff_account(boss, 'gate9', 'Gate-pass-99', 'admin', NOW)
            store.reset_staff_password(boss, 'gate9', 'Gate-pass-100', NOW)
            store.audit('test.sensitive', '2026-10-25 09:00:00', actor='boss',
                        detail={'password': secret, 'otp': '123456', 'code': '654321', 'token': boss.token,
                                'recovery_code': 'ABCDEF0123456789', 'nested': {'Pin': '9999', 'ok': 'visible'}})
            dump = everything_stored(store)
            for leak in (secret, guess, 'Gate-pass-99', 'Gate-pass-100', '123456', '654321', boss.token,
                         'ABCDEF0123456789', '9999'):
                self.assertNotIn(leak, dump)
            self.assertIn('visible', dump)                                     # ordinary detail is kept
            self.assertIn(boss.token_hash, dump)                               # only the token's hash is stored
            staff_text = ' '.join(str(v) for row in store.conn.execute('SELECT * FROM staff') for v in row)
            self.assertNotIn(secret, staff_text)
            self.assertNotIn('Gate-pass', staff_text)
            store.close()

    def test_audit_records_cannot_be_changed_and_tampering_is_detected(self):
        self.issue()
        for sql in ("UPDATE audit SET actor = 'someone else'", 'DELETE FROM audit',
                    "UPDATE audit SET outcome = 'success' WHERE id = 1"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.store.conn.execute(sql)
        self.assertEqual(self.store.audit_problems(), [])
        # someone with direct access to the file removes the guard and edits a record: the chain shows it
        self.store.conn.execute('DROP TRIGGER audit_no_update')
        self.store.conn.execute("UPDATE audit SET actor = 'nobody' WHERE id = 2")
        problems = self.store.verify()
        self.assertTrue(any('AUD-00000002' in p for p in problems), problems)

    def test_deleting_a_record_from_the_middle_is_detected(self):
        self.issue(); self.issue()
        self.store.conn.execute('DROP TRIGGER audit_no_delete')
        self.store.conn.execute('DELETE FROM audit WHERE id = 2')
        self.assertTrue(self.store.audit_problems())

    def test_only_permitted_roles_may_read_or_export_the_audit_log(self):
        self.assertTrue(self.store.read_audit(self.admin, NOW))
        self.assertTrue(self.store.read_audit(self.user('supervisor'), NOW))
        for role in ('cashier', 'gate'):
            with self.assertRaises(auth.PermissionDenied):
                self.store.read_audit(self.user(role), NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.export_audit_csv(self.user('supervisor'), self.dir / 'no.csv', NOW)
        self.assertFalse((self.dir / 'no.csv').exists())

    def test_the_operation_and_its_audit_record_succeed_or_fail_together(self):
        rec = self.issue()
        with mock.patch.object(cta_db.Store, '_audit_insert', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(cta_storage.StorageError):
                self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], self.admin, NOW)
        self.assertEqual(self.store.load_history()[0]['passengers'][0]['status'], 'issued')
        with mock.patch.object(cta_db.Store, '_insert_rows', side_effect=sqlite3.OperationalError('disk I/O error')):
            with self.assertRaises(cta_storage.StorageError):
                self.store.cancel_ticket(rec['passengers'][0]['ticket_no'], self.admin, NOW)
        self.assertEqual(audit_rows(self.store, 'ticket.cancelled'), [])

    def test_upgrading_a_version_2_database_builds_the_chain_and_auth_tables(self):
        self.issue()
        self.store.close()
        raw = sqlite3.connect(str(self.dir / 'cta.db'), isolation_level=None)
        raw.executescript('''
            DROP TRIGGER audit_no_update; DROP TRIGGER audit_no_delete;
            ALTER TABLE audit DROP COLUMN chain; ALTER TABLE audit DROP COLUMN target; ALTER TABLE audit DROP COLUMN outcome;
            ALTER TABLE staff DROP COLUMN disabled; DROP TABLE sessions; DROP TABLE login_attempts;
            CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'x'); END;
            CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'x'); END;
            UPDATE meta SET value = '2' WHERE key = 'schema_version';''')
        raw.close()
        self.store = cta_db.Store.open(self.dir, NOW)
        self.assertEqual(self.store.schema_version, 3)
        self.assertEqual(self.store.verify(), [])
        self.assertTrue(list((self.dir / 'backups').glob('cta-*-pre-upgrade.db')))
        self.assertTrue(all(r[0] for r in self.store.conn.execute('SELECT chain FROM audit')))
        self.store.staff_login('admin', TEST_PASSWORD, NOW)                    # accounts carried over, auth tables exist


# ====================================================================== the application
class AppAuthTests(unittest.TestCase):
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
        for p in (mock.patch('cta_app.messagebox'), mock.patch.object(auth, 'ASSIGNABLE_ROLES', ALL_ROLES)):
            p.start(); self.addCleanup(p.stop)

    def app(self):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def dialog(self, app):
        from cta_dialogs import AuthDialog
        got = []
        d = AuthDialog(self.root, app.accounts, app.data_target, app.clock, lambda a: None, staff=app.staff_accounts,
                       staff_path=app.data_target, on_staff=got.append, staff_auth=app.staff_auth, device='test')
        d._staff_step()
        return d, got

    def test_staff_sign_in_through_the_dialog_with_a_lockout_that_survives_reopening(self):
        app = self.app()
        d, got = self.dialog(app)
        d.user_var.set('boss'); d.pw_var.set('Boss-pass-1'); d.pw2_var.set('Boss-pass-1'); d._staff_next()
        self.assertIsInstance(got[0], auth.Principal)                          # first run created the admin
        for _ in range(auth.MAX_FAILURES):
            d, got = self.dialog(app)                                          # closing and reopening used to reset it
            d.user_var.set('boss'); d.pw_var.set('wrong-pass-1'); d._staff_next()
        self.assertIn('Too many', d.err.cget('text'))
        d, got = self.dialog(app)
        d.user_var.set('boss'); d.pw_var.set('Boss-pass-1'); d._staff_next()
        self.assertEqual(got, [])
        self.assertIn('Too many', d.err.cget('text'))
        self.assertEqual(d.pw_var.get(), '')                                   # the typed password is not kept

    def test_sign_out_ends_the_session_and_a_kept_principal_is_useless(self):
        app = self.app()
        app.staff = sign_in(app.store, 'admin')
        kept = app.staff
        app.sign_out()
        self.assertIsNone(app.staff)
        with self.assertRaises(auth.PermissionDenied):
            app.store.authorize(kept, 'report.view', app.clock())
        self.assertIn('auth.logout', {r['kind'] for r in audit_rows(app.store)})

    def test_admin_portal_and_actions_follow_the_role(self):
        app = self.app()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(['Ann Lee'], 'cash')
        tno = app.history[0]['passengers'][0]['ticket_no']
        sign_in(app.store, 'admin')
        app.staff = sign_in(app.store, 'gate1', 'gate')
        app._paint_account()
        app.show_page('admin')
        self.assertEqual(app.current_page, 'plan')                             # gate staff do not see the portal
        app.admin.tree.selection = lambda: (tno,)
        app.admin.mark_selected_paid()                                         # even if they reach the button
        self.assertIn('not allowed', app.admin.result.cget('text'))
        self.assertEqual(app.store.load_history()[0]['payment']['status'], 'pending_counter')
        app.staff = sign_in(app.store, 'cash1', 'cashier')
        app.show_page('admin')
        self.assertEqual(app.current_page, 'admin')
        app.admin.mark_selected_paid()
        self.assertIn('recorded', app.admin.result.cget('text'))
        self.assertEqual(app.store.load_history()[0]['payment']['status'], 'paid')
        out = Path(self.tmp) / 'export.csv'
        app.admin._export_to(out, 'ticket_no\nX\n', app.clock())               # cashiers may not export phones
        self.assertFalse(out.exists())
        self.assertIn('not allowed', app.admin.result.cget('text'))
        app.admin.cancel_selected('ill')
        self.assertIn('not allowed', app.admin.result.cget('text'))
        app.staff = sign_in(app.store, 'sup1', 'supervisor')
        app.admin._export_to(out, 'ticket_no\nX\n', app.clock())
        self.assertTrue(out.exists())
        exported = audit_rows(app.store, 'report.exported')[-1]
        self.assertEqual((exported['actor'], json.loads(exported['detail'])['rows']), ('sup1', 1))

    def test_passenger_sign_in_is_audited_without_the_code_or_full_phone(self):
        app = self.app()
        from cta_dialogs import AuthDialog
        done = []
        d = AuthDialog(self.root, app.accounts, app.data_target, app.clock, done.append, sms=app.sms,
                       audit=app._audit_passenger_sign_in)
        d.phone_var.set('+44 7123 456789'); d._phone_next()
        d.name_var.set('Jane Doe'); d._name_next()
        code = app.sms.code_for('+447123456789')
        wrong = '000000' if code != '000000' else '111111'
        d.code_var.set(wrong); d._verify()
        d.code_var.set(code); d._verify()
        self.assertEqual(len(done), 1)
        rows = audit_rows(app.store, 'auth.passenger_login')
        self.assertEqual([r['outcome'] for r in rows], ['failure', 'success'])
        text = json.dumps(rows)
        for leak in (code, wrong, '7123456789', '+447123456789'):
            self.assertNotIn(leak, text)
        self.assertTrue(rows[0]['actor'].endswith('789'))

    def test_fare_and_key_changes_are_recorded_when_the_app_starts(self):
        app = self.app()
        app.store.close()
        first = {r['target'] for r in audit_rows(cta_db.Store.open(Path(self.tmp)), 'config.recorded')}
        self.assertTrue({'fare_policy', 'signing_keys', 'settings', 'environment'} <= first)
        import cta_fares
        with mock.patch.object(cta_fares, 'FARE_POLICY_VERSION', 'CTA-FARES-9'):
            again = self.app()
        changed = [r for r in audit_rows(again.store, 'config.changed') if r['target'] == 'fare_policy']
        self.assertEqual(len(changed), 1)
        self.assertIn('CTA-FARES-9', changed[0]['detail'])
        self.assertEqual(changed[0]['actor'], 'system')


if __name__ == '__main__':
    unittest.main()
