"""Module 7 tests: invalid input is refused early and clearly; unexpected failures are recorded, never silent."""
import ast
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.environ['CTA_ENV'] = 'testing'
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)

from test_all import FakePSP, sign_in      # shared test settings
import cta_accounts as acct
import cta_auth as auth
import cta_core as core
import cta_db
import cta_errors as errors
import cta_fares as fares
import cta_payments as pay
import cta_routing as rt
import cta_sms
import cta_stats as stats
import cta_storage
import cta_tickets as T

NOW = datetime(2026, 10, 25, 9, 0)
BROAD = {'Exception', 'BaseException'}


def handler_names(node):
    if node.type is None:
        return {'<bare>'}
    items = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
    out = set()
    for it in items:
        out.add(it.attr if isinstance(it, ast.Attribute) else getattr(it, 'id', '?'))
    return out


# ====================================================================== no silent swallowing
class HandlerPolicyTests(unittest.TestCase):
    """Every broad handler re-raises, records the error through cta_errors, or states why it may continue."""

    def test_broad_handlers_are_never_silent(self):
        offenders = []
        for path in sorted(ROOT.glob('cta_*.py')):
            src = path.read_text(encoding='utf-8')
            lines = src.splitlines()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = handler_names(node)
                body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                silent = all(isinstance(b, ast.Pass) for b in node.body)
                if names & (BROAD | {'<bare>'}):
                    justified = 'noqa: BLE001' in lines[node.lineno - 1]
                    reraises = any(isinstance(n, ast.Raise) for b in node.body for n in ast.walk(b))
                    reports = 'cta_errors.report' in body_src or 'cta_errors.user_message' in body_src
                    if not (justified or reraises or reports) or (silent and not justified):
                        offenders.append(f'{path.name}:{node.lineno} except {sorted(names)}')
        self.assertEqual(offenders, [])

    def test_quiet_handlers_only_catch_narrow_expected_errors(self):
        allowed = {'TclError', 'OSError', 'Error', 'AttributeError', 'FileExistsError', 'SubprocessError'}
        found = []
        for path in sorted(ROOT.glob('cta_*.py')):
            src = path.read_text(encoding='utf-8')
            lines = src.splitlines()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.ExceptHandler) and all(isinstance(b, ast.Pass) for b in node.body):
                    if not handler_names(node) <= allowed and 'noqa: BLE001' not in lines[node.lineno - 1]:
                        found.append(f'{path.name}:{node.lineno} {sorted(handler_names(node))}')
        self.assertEqual(found, [])


# ====================================================================== the diagnostics log
class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        errors.configure(self.dir)
        self.addCleanup(errors.configure, None)

    def entries(self):
        path = errors.log_path()
        return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def test_an_unexpected_error_gets_a_reference_and_a_full_record_without_secrets(self):
        errors.register_secret('sk_live_TOPSECRET99')
        try:
            {}['missing']
        except KeyError as exc:
            shown = errors.user_message(exc, 'test action', NOW)
        self.assertRegex(shown, r'\(Reference ERR-20261025-[0-9A-F]{6}\.\)$')
        self.assertNotIn('missing', shown)                                     # no internals for the person
        self.assertNotIn('Traceback', shown)
        try:
            raise RuntimeError('failed with key sk_live_TOPSECRET99 and token=abc123def password: hunter22')
        except RuntimeError as exc:
            ref = errors.report(exc, 'calling the provider', NOW, url='https://x?api_key=SHOULDHIDE')
        entry = self.entries()[-1]
        self.assertEqual(entry['id'], ref)
        self.assertEqual(entry['type'], 'builtins.RuntimeError')
        self.assertIn('Traceback', entry['traceback'])
        text = json.dumps(self.entries())
        for secret in ('sk_live_TOPSECRET99', 'abc123def', 'hunter22', 'SHOULDHIDE'):
            self.assertNotIn(secret, text)
        self.assertIn('***', text)

    def test_expected_errors_show_their_own_message_and_are_not_logged_as_incidents(self):
        for exc in (fares.FareError('There is no route between these stations.'),
                    auth.PermissionDenied('You are not allowed to cancel.'),
                    cta_storage.DataCorrupted('The bookings file is damaged.'),
                    pay.PaymentUnavailable('Live online payments are not available.'),
                    rt.RoutingConfigError('Journey planning is not configured.'),
                    ValueError('Add at least one traveller.')):
            self.assertEqual(errors.user_message(exc), str(exc))
        self.assertEqual(self.entries(), [])

    def test_the_diagnostics_writer_never_raises(self):
        with mock.patch.object(Path, 'open', side_effect=PermissionError('read-only')):
            ref = errors.report(ValueError('x'), 'anything')
        self.assertTrue(ref.startswith('ERR-'))
        errors.configure(Path('/proc/definitely/not/writable'))
        self.assertTrue(errors.report(ValueError('x'), 'anything').startswith('ERR-'))

    def test_the_log_is_size_limited(self):
        with mock.patch.object(errors, 'MAX_LOG_BYTES', 2000):
            for _ in range(20):
                errors.note('filling', 'x' * 300)
        self.assertTrue(errors.log_path().with_suffix('.log.1').exists())
        self.assertLess(errors.log_path().stat().st_size, 6000)


# ====================================================================== malformed input and boundaries
class InputBoundaryTests(unittest.TestCase):
    def test_traveller_counts_keep_their_limits(self):
        self.assertTrue(core.validate_counts({'Adult': 1})[0])
        self.assertTrue(core.validate_counts({'Adult': core.MAX_PER_CATEGORY})[0])
        for bad in ({'Adult': 0}, {}, {'Adult': -1}, {'Adult': core.MAX_PER_CATEGORY + 1}, {'Adult': True},
                    {'Adult': 1.0}, {'Adult': '1'}, {'Adult': None}, {'Pet': 1}):
            self.assertFalse(core.validate_counts(bad)[0], bad)
        self.assertTrue(core.validate_booking_size({'Adult': core.MAX_PASSENGERS_PER_BOOKING})[0])
        self.assertFalse(core.validate_booking_size({'Adult': core.MAX_PASSENGERS_PER_BOOKING + 1})[0])
        self.assertEqual((core.MAX_PER_CATEGORY, core.MAX_PASSENGERS_PER_BOOKING), (99, 20))     # unchanged limits

    def test_names_phones_usernames_and_passwords_at_their_edges(self):
        lo, hi = acct.NAME_MIN, acct.NAME_MAX
        self.assertEqual(acct.validate_person_name('Al')[1], '')
        self.assertEqual(acct.validate_person_name('A' * hi)[1], '')
        for bad in ('A' * (lo - 1), 'A' * (hi + 1), '', '   ', 'R2D2', 'Jane <b>', '\x00Jane', None, 12345, ['Jane']):
            self.assertTrue(acct.validate_person_name(bad)[1], repr(bad))
        self.assertEqual(acct.validate_phone('+44 7123 4567')[0], '+4471234567')         # 10 digits
        self.assertEqual(acct.validate_phone('+12345678')[0], '+12345678')               # 8: the minimum
        self.assertEqual(acct.validate_phone('+' + '1' * 15)[0], '+' + '1' * 15)         # 15: the maximum
        for bad in ('+1234567', '+' + '1' * 16, '07123 456789', '+44 71x3 456789', '+0447123456789', '++44123456789',
                    447123456789, None, ''):
            self.assertIsNone(acct.validate_phone(bad)[0], repr(bad))
        self.assertEqual(acct.validate_username('abc')[1], '')
        self.assertEqual(acct.validate_username('a' * 30)[1], '')
        for bad in ('ab', 'a' * 31, 'bad name', 'ü-user', None):
            self.assertTrue(acct.validate_username(bad)[1], repr(bad))
        self.assertEqual(acct.validate_password('abcdefg1'), '')
        self.assertEqual(acct.validate_password('a1' * 64), '')                          # 128 characters
        for bad in ('abcdef1', 'a1' * 64 + 'x', 'abcdefgh', '12345678', None, 12345678):
            self.assertTrue(acct.validate_password(bad), repr(bad))

    def test_payment_amounts_references_and_currencies(self):
        self.assertEqual(pay.pay('cash', 1, 'CTA-1', 'x', NOW)['amount_cents'], 1)
        for amount in (0, -1, True, 1.5, '100', None):
            with self.assertRaises(ValueError, msg=repr(amount)):
                pay.pay('cash', amount, 'CTA-1', 'x', NOW)
        for ref in ('', '   ', None, 'X' * 65, 42):
            with self.assertRaises(ValueError, msg=repr(ref)):
                pay.pay('card', 100, ref, 'x', NOW)
        for method in ('bitcoin', '', None, 'CASH'):
            with self.assertRaises(ValueError):
                pay.pay(method, 100, 'CTA-1', 'x', NOW)
        for cur in ('SR', 'SARX', 'S4R', '£'):
            with self.assertRaises(ValueError, msg=cur):
                pay.payment_settings({'payments': {'currency': cur}})
            with self.assertRaises(ValueError, msg=cur):
                pay.pay('cash', 100, 'CTA-1', 'x', NOW, currency=cur)
        self.assertEqual(pay.payment_settings({'payments': {'currency': ' usd '}})['currency'], 'USD')

    def test_booking_records_check_their_inputs_before_anything_is_built(self):
        route = rt.astar('Centrala', 'Zord')
        good = dict(route=route, counts={'Adult': 1}, zones=3, depart=NOW, issued_at=NOW, history=[], names=['Ann Lee'])
        core.make_record(**good)
        for change in ({'depart': '2026-10-25 09:00'}, {'issued_at': None}, {'legs': True}, {'legs': 3},
                       {'account_phone': '07123456789'}, {'account_phone': '+44 7123'}, {'names': 'Ann Lee'},
                       {'names': ['Ann Lee', 7]}, {'counts': {'Adult': 1.0}}, {'zones': 2}, {'names': ['R2D2']}):
            with self.assertRaises(ValueError, msg=change):
                core.make_record(**dict(good, **change))

    def test_ticket_and_recovery_inputs_cannot_break_the_checks(self):
        signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
        for text, code in (('x' * (T.MAX_PRESENTED + 1), T.MALFORMED), (None, T.EMPTY), (12345, T.EMPTY),
                           ('\x00\x07', T.MALFORMED), ('CTA-1\x1b[2J', T.MALFORMED), ('CTA1:' + ':' * 300, T.MALFORMED)):
            self.assertEqual(T.parse_presented(text, signer).code, code, repr(text)[:40])
        self.assertEqual(T.parse_presented('x' * T.MAX_PRESENTED, signer).code, '')     # long but allowed: unknown number
        hist = []
        r = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'])
        for ref, code in ((None, r['recovery_code']), (r['reference'], None), (123, 456),
                          (r['reference'], 'A' * 10_000), ('R' * 10_000, r['recovery_code'])):
            self.assertIsNone(core.find_ticket_with_code([r], ref, code))
        self.assertIs(core.find_ticket_with_code([r], r['reference'], r['recovery_code']), r)

    def test_report_periods_and_times_are_checked(self):
        self.assertEqual(stats.filter_period([], 'Today', NOW), [])
        for bad in ('Weekly', '', None, 'today'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                stats.filter_period([], bad, NOW)
        with self.assertRaises(ValueError):
            stats.period_start('Today', '2026-10-25')

    def test_absurd_sign_in_input_is_refused_and_stored_briefly(self):
        with tempfile.TemporaryDirectory() as d:
            store = cta_db.Store.open(Path(d), NOW)
            sign_in(store, 'admin')
            for name in ('x' * 10_000, '', None, 'admin\x00'):
                with self.assertRaises(auth.AuthError):
                    store.staff_login(name, 'whatever1', NOW)
            keys = [k for (k,) in store.conn.execute('SELECT username_key FROM login_attempts')]
            self.assertTrue(keys and all(len(k) <= 64 for k in keys))
            with self.assertRaises(auth.AuthError):
                store.staff_login('admin', 'p' * 100_000, NOW)                 # no hashing of huge input
            store.close()

    def test_damaged_or_wrong_settings_are_reported_by_every_layer(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'cta_config.json'
            cfg.write_text('{"routing": {"provider": ')
            provider, health = rt.load_routing(cfg, 'testing')
            self.assertIsNone(provider); self.assertIn('damaged', health.message)
            with self.assertRaises(cta_storage.DataCorrupted):
                cta_sms.load_sms_provider(cfg)
            with mock.patch.dict(os.environ):
                os.environ.pop('CTA_ENV', None)
                self.assertEqual(pay.current_environment(config_path=cfg), 'production')  # payments fail closed
            cfg.write_text(json.dumps({'sms': {'provider': 'twilio_verify', 'account_sid': 'AC1', 'auth_token': 't',
                                               'service_sid': 'VA1', 'channel': 'pigeon'}}))
            with self.assertRaises(cta_storage.DataInvalid) as cm:
                cta_sms.load_sms_provider(cfg)
            self.assertIn('channel', str(cm.exception))


# ====================================================================== the application under failure
class AppFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.root = tk.Tk()
        except Exception:  # noqa: BLE001
            raise unittest.SkipTest('no display / tkinter')
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.addCleanup(errors.configure, None)
        self.tmp = tempfile.mkdtemp()
        p = mock.patch('cta_app.messagebox')
        self.box = p.start(); self.addCleanup(p.stop)

    def app(self, **kw):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp, **kw)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def plan(self, app, adults=1):
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(adults)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None

    def log(self):
        p = Path(self.tmp) / 'logs' / errors.LOG_NAME
        return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []

    def test_an_error_in_any_button_is_shown_plainly_with_a_reference_and_logged(self):
        app = self.app()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord')
        self.root.after(0, lambda: {}['boom'])                                  # a bug inside a screen callback
        self.root.update()
        message = self.box.showerror.call_args[0][1]
        self.assertIn('Reference ERR-', message)
        self.assertNotIn('boom', message); self.assertNotIn('Traceback', message)
        entry = self.log()[-1]
        self.assertEqual(entry['type'], 'builtins.KeyError')
        self.assertIn(entry['id'], message)
        self.assertEqual((app.from_pick.get(), app.to_pick.get()), ('Centrala', 'Zord'))   # what was typed stays

    def test_invalid_input_never_reaches_payment_or_storage(self):
        app = self.app()
        self.plan(app)
        with mock.patch('cta_app.payments.pay') as charge:
            app._issue(['R2-D2'], 'card')                                       # bad name
            app._issue(['Ann Lee', 'Extra Person'], 'card')                     # wrong number of names
            app._issue(['Ann Lee'], 'bitcoin')                                  # unknown method: refused by pay itself
        self.assertLessEqual(charge.call_count, 1)
        self.assertEqual(app.history, [])
        app.steppers['Adult'].set(0)
        with mock.patch('cta_app.payments.pay') as charge:
            app._issue([], 'card')
        charge.assert_not_called()
        self.assertEqual(app.history, [])

    def test_pressing_pay_twice_does_not_charge_or_issue_twice(self):
        app = self.app()
        self.plan(app)
        charged = []
        real = pay.pay
        with mock.patch('cta_app.payments.pay', side_effect=lambda *a, **k: charged.append(a) or real(*a, **k)):
            app._issue(['Ann Lee'], 'card')
            app._issue(['Ann Lee'], 'card')                                     # a second press after success
        self.assertEqual(len(charged), 1)
        self.assertEqual(len(app.history), 1)

    def test_a_failure_after_payment_is_reported_with_the_booking_and_transaction(self):
        app = self.app()
        self.plan(app)
        with mock.patch.object(type(app), '_complete_booking', side_effect=RuntimeError('disk exploded at /home/x')):
            app._issue(['Ann Lee'], 'card')
        title, message = self.box.showerror.call_args[0][:2]
        self.assertEqual(title, 'Booking problem')
        self.assertIn('Booking reference: CTA-', message)
        self.assertIn('transaction TX-', message)
        self.assertIn('Reference ERR-', message)
        self.assertNotIn('/home/x', message)
        entry = self.log()[-1]
        self.assertEqual(entry['context'], 'completing a booking after payment')
        self.assertTrue(entry['facts']['transaction'].startswith('TX-'))

    def test_a_pdf_failure_keeps_the_booking_and_hides_internals(self):
        app = self.app()
        self.plan(app)
        with mock.patch.object(core, 'export_ticket_pdf', side_effect=RuntimeError('font cache at C:/Users/jane')):
            app._issue(['Ann Lee'], 'card')
        self.assertEqual(len(app.history), 1)                                   # the booking itself is saved
        import tkinter as tk
        status = tk.Label(self.root)
        with mock.patch.object(core, 'export_ticket_pdf', side_effect=RuntimeError('font cache at C:/Users/jane')):
            self.assertIsNone(app._export(app.history[0], Path(self.tmp) / 'x.pdf', status))
        self.assertIn('Reference ERR-', status.cget('text'))
        self.assertNotIn('jane', status.cget('text'))

    def test_a_failed_payment_keeps_the_journey_travellers_and_typed_names(self):
        app = self.app(pay_provider=FakePSP(claim='failed'), environment='production', provider=rt.SimulatedProvider())
        self.plan(app, adults=2)
        with mock.patch('cta_app.PaymentDialog'):
            app._names_done(['Ann Lee', 'Bo Lee'])
        app._issue(['Ann Lee', 'Bo Lee'], 'card')
        self.assertIn('Payment failed', self.box.showerror.call_args[0][0])
        self.assertEqual(app.history, [])
        self.assertEqual((app.counts['Adult'], app.route.stations[-1]), (2, 'Zord'))   # nothing reset
        with mock.patch('cta_app.PassengerDialog') as names:
            app.book()
        self.assertEqual(names.call_args.kwargs['prefill'], ['Ann Lee', 'Bo Lee'])    # no retyping
        app.pay_provider = rt.SimulatedProvider() and pay.SimulatedGateway()
        app.environment, app.online_ok = 'testing', True
        app._issue(['Ann Lee', 'Bo Lee'], 'card')
        self.assertEqual(len(app.history), 1)
        with mock.patch('cta_app.PassengerDialog') as names:
            app.steppers['Adult'].set(2)
            app.book()
        self.assertIsNone(names.call_args.kwargs['prefill'])                    # used up by the successful booking

    def test_a_provider_crash_is_never_a_payment_success(self):
        class Crashing(pay.PaymentProvider):
            name, live = 'crashpsp', True
            def charge(self, *a, **k): raise ZeroDivisionError('bug in provider adapter')
        app = self.app(pay_provider=Crashing(), environment='production', provider=rt.SimulatedProvider())
        self.plan(app)
        app._issue(['Ann Lee'], 'card')
        self.assertEqual(app.history[0]['payment']['status'], 'unknown')       # saved as unconfirmed, no ticket valid
        self.assertEqual(self.box.showwarning.call_args[0][0], 'Payment not confirmed')
        self.assertTrue(any(e['type'].endswith('ZeroDivisionError') for e in self.log()))

    def test_audit_and_sign_out_failures_are_recorded_not_ignored(self):
        app = self.app()
        app.staff = sign_in(app.store, 'admin')
        with mock.patch.object(app.staff_auth, 'logout', side_effect=cta_storage.DataWriteError('disk full')):
            app.sign_out()
        self.assertIsNone(app.staff)
        self.assertIn('could not be closed', self.box.showwarning.call_args[0][1])
        self.assertIn('Reference ERR-', self.box.showwarning.call_args[0][1])
        ledger = cta_db.StoreLedger(app.store)
        with mock.patch.object(app.store, 'audit', side_effect=cta_storage.DataWriteError('disk full')):
            ledger.log({'at': 'x', 'reference': 'CTA-1', 'status': 'paid'})     # must not raise into a booking
        contexts = [e['context'] for e in self.log()]
        self.assertIn('staff sign-out', contexts)
        self.assertIn('writing a payment audit event', contexts)

    def test_problem_reports_need_a_listed_type(self):
        app = self.app()
        app.issue_text.insert('1.0', 'The printer jammed.')
        app.issue_type.set('Something invented')
        app.send_issue()
        self.assertIn('Choose', app.issue_msg.cget('text'))
        app.issue_type.current(1)
        app.send_issue()
        self.assertIn('Thank you', app.issue_msg.cget('text'))


if __name__ == '__main__':
    unittest.main()
