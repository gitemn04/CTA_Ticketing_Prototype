"""Module 8: end-to-end flows across storage, payments, tickets, fares, permissions and recovery, plus concurrency."""
import os
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
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)

from test_all import FakePSP, good, legacy_install, sign_in
import cta_core as core
import cta_db
import cta_fares as fares
import cta_payments as pay
import cta_routing as rt
import cta_storage
import cta_tickets as T

NOW = datetime(2026, 10, 25, 9, 0)
SIGNER = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])


def make_booking(store, method='card', provider=None, env='testing', legs=1, names=('Ann Lee',), reference=None,
                 a='Centrala', b='Zord', when=NOW):
    hist = store.load_history()
    route = rt.astar(a, b)
    rec = core.make_record(route, {'Adult': len(names)}, fares.quote(a, b, {'Adult': 1}).zones, when, when, hist,
                           names=list(names), account_phone='+447123456789', legs=legs,
                           reference=reference or store.reserve_reference(when))
    rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], '+447123456789', when, provider,
                             environment=env, ledger=cta_db.StoreLedger(store))
    store.save_history(hist + [rec], when)
    return rec


class StoreCase(unittest.TestCase):
    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.dir = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(self.store.close)
        self.admin = sign_in(self.store, 'admin')

    def scan(self, text, env='testing', store=None, when=NOW):
        return (store or self.store).check_in(text, signer=SIGNER, now=when, operator=self.admin, environment=env)


# ====================================================================== whole journeys
class LifecycleTests(StoreCase):
    def test_online_booking_from_payment_to_refund_and_recovery(self):
        psp = FakePSP(claim='pending')
        rec = make_booking(self.store, provider=psp, env='production', legs=2, names=('Ann Lee', 'Bo Lee'))
        out, back = rec['passengers'][0]['ticket_no'], rec['passengers'][0]['return_ticket_no']
        self.assertEqual(self.scan(out, 'production').code, T.UNCONFIRMED)          # no ticket before confirmation
        psp.verified = good(rec['total_cents'], rec['reference'])
        ok, msg = self.store.handle_payment_callback({'reference': rec['reference'], 'transaction_id': 'PSP-1',
                                                      'status': 'whatever'}, psp, NOW, 'production')
        self.assertTrue(ok, msg)
        stored = self.store.get_booking(rec['reference'])
        self.assertEqual(stored['payment']['status'], 'paid')
        self.assertEqual(fares.fare_problems(stored), [])
        self.assertTrue(self.scan(SIGNER.sign(stored['passengers'][0]['ticket_id']), 'production').ok)   # QR
        self.assertEqual(self.scan(out, 'production').code, T.USED)
        self.assertTrue(self.store.cancel_ticket(back, self.admin, NOW, 'not returning')[0])
        self.assertEqual(self.scan(back, 'production').code, T.CANCELLED)
        self.assertTrue(self.store.record_refund(back, self.admin, NOW, 'psp-refund-1')[0])
        before = self.store.load_history()
        scans = len(self.store.scans())
        self.assertEqual(self.store.verify(), [])
        backup = self.store.backup('manual', now=NOW)
        self.store.close()
        (self.dir / 'cta.db').write_bytes(b'\x00' * 8192)                          # the database is destroyed
        with self.assertRaises(cta_storage.DataCorrupted):
            cta_db.Store.open(self.dir)
        cta_db.restore_backup(backup, self.dir, NOW)
        self.store = cta_db.Store.open(self.dir, NOW)
        self.assertEqual([cta_db.canonical(r) for r in self.store.load_history()], [cta_db.canonical(r) for r in before])
        self.assertEqual(len(self.store.scans()), scans)
        self.assertEqual(self.store.verify(), [])
        admin = self.store.staff_login('admin', 'test-pass-1', NOW)                  # staff accounts restored too
        self.assertEqual(self.store.check_in(back, signer=SIGNER, now=NOW, operator=admin,
                                             environment='production').code, T.REFUNDED)

    def test_a_confirmation_for_the_wrong_amount_never_makes_a_valid_ticket(self):
        psp = FakePSP(claim='pending')
        rec = make_booking(self.store, provider=psp, env='production')
        psp.verified = good(1, rec['reference'])                                      # provider says: 1 cent was paid
        ok, _ = self.store.confirm_online_payment(rec['reference'], psp, NOW, environment='production')
        self.assertFalse(ok)
        stored = self.store.get_booking(rec['reference'])
        self.assertEqual(stored['payment']['status'], 'failed')
        self.assertTrue(stored['payment']['review_required'])
        self.assertEqual(self.scan(rec['passengers'][0]['ticket_no'], 'production').code, T.UNPAID)
        events = [r for r in self.store.read_audit(self.admin, NOW) if r['kind'] == 'payment.confirmed']
        self.assertEqual(events[0]['outcome'], 'failure')
        for bad in (None, 'x' * 99, 12, {'reference': 5}, {'reference': rec['reference'], 'transaction_id': 7}):
            payload = bad if isinstance(bad, dict) or bad is None or isinstance(bad, int) else {'reference': bad}
            self.assertFalse(self.store.handle_payment_callback(payload, psp, NOW, 'production')[0])

    def test_cash_booking_through_mark_paid_check_in_and_re_entry(self):
        rec = make_booking(self.store, method='cash')
        tno = rec['passengers'][0]['ticket_no']
        self.assertEqual(self.scan(tno).code, T.UNPAID)
        self.assertTrue(self.store.mark_paid(tno, self.admin, NOW)[0])
        self.assertTrue(self.scan(tno).ok)
        self.assertEqual(self.scan(tno).code, T.USED)
        self.assertTrue(self.store.allow_reentry(tno, self.admin, NOW + timedelta(minutes=5), 'gate fault')[0])
        self.assertTrue(self.scan(tno, when=NOW + timedelta(minutes=6)).ok)
        kinds = [r['kind'] for r in self.store.read_audit(self.admin, NOW)]
        for k in ('booking.created', 'payment.status_changed', 'ticket.reentry'):
            self.assertIn(k, kinds)

    def test_an_old_json_installation_upgrades_and_keeps_working(self):
        folder = Path(tempfile.mkdtemp())
        hist, accounts, staff = legacy_install(folder)
        store = cta_db.Store.open(folder, NOW)
        self.addCleanup(store.close)
        self.assertEqual([r['reference'] for r in store.load_history()], [r['reference'] for r in hist])
        admin = store.staff_login('admin', 'test-pass-1', NOW)
        cash = next(r for r in store.load_history() if r['payment']['status'] == 'pending_counter')
        tno = cash['passengers'][0]['ticket_no']
        self.assertTrue(store.mark_paid(tno, admin, datetime(2026, 10, 24, 10, 0))[0])
        self.assertTrue(store.check_in(tno, signer=SIGNER, now=datetime(2026, 10, 24, 10, 0), operator=admin,
                                       environment='testing').ok)
        new = make_booking(store, when=datetime(2026, 10, 24, 11, 0))
        self.assertNotIn(new['reference'], [r['reference'] for r in hist])           # numbering continues, no clash
        self.assertEqual(store.verify(), [])


# ====================================================================== concurrency
def run_together(n, work, folder):
    """Run ``work(i, store)`` in n threads at once; each thread opens its OWN connection, like separate windows."""
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def go(i):
        try:
            store = cta_db.Store.open(folder, NOW)
            try:
                barrier.wait(timeout=10)
                results[i] = work(i, store)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - collected and asserted by the test
            errors.append(repr(exc))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    return results, errors


class ConcurrencyTests(StoreCase):
    def test_simultaneous_bookings_get_different_references_and_are_all_kept(self):
        n = 8

        def book(i, store):
            return make_booking(store, names=(f'Person {"ABCDEFGH"[i]}ee',))['reference']
        refs, errors = run_together(n, book, self.dir)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(refs)), n)
        self.assertEqual(sorted(r['reference'] for r in self.store.load_history()), sorted(refs))
        tickets = [p['ticket_no'] for r in self.store.load_history() for p in r['passengers']]
        self.assertEqual(len(tickets), len(set(tickets)))

    def test_the_old_way_of_numbering_would_have_clashed(self):
        a, b = self.store.load_history(), self.store.load_history()
        route = rt.astar('Centrala', 'Zord')
        ra = core.make_record(route, {'Adult': 1}, 3, NOW, NOW, a, names=['Ann Lee'])
        rb = core.make_record(route, {'Adult': 1}, 3, NOW, NOW, b, names=['Bo Lee'])
        self.assertEqual(ra['reference'], rb['reference'])                          # why reservation is needed
        self.assertNotEqual(self.store.reserve_reference(NOW), self.store.reserve_reference(NOW))

    def test_repeated_and_simultaneous_payment_callbacks_confirm_once(self):
        psp = FakePSP(claim='pending')
        rec = make_booking(self.store, provider=psp, env='production')
        psp.verified = good(rec['total_cents'], rec['reference'])
        n = 6
        payload = {'reference': rec['reference'], 'transaction_id': 'PSP-1'}
        results, errors = run_together(n, lambda i, st: st.handle_payment_callback(payload, psp, NOW, 'production'),
                                       self.dir)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for ok, _ in results if ok), 1, results)
        for _ in range(3):                                                           # and later retries
            self.assertFalse(self.store.handle_payment_callback(payload, psp, NOW, 'production')[0])
        confirmed = [r for r in self.store.read_audit(self.admin, NOW) if r['kind'] == 'payment.confirmed']
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(self.store.counts()['payment_claims'], 1)
        self.assertEqual(self.store.get_booking(rec['reference'])['payment']['status'], 'paid')

    def test_scans_racing_a_cancellation_leave_one_consistent_outcome(self):
        for _ in range(5):
            rec = make_booking(self.store)
            tno = rec['passengers'][0]['ticket_no']
            def work(i, store):
                if i == 0:
                    try:
                        return ('cancel', store.cancel_ticket(tno, self.admin, NOW)[0])
                    except cta_storage.ConcurrentUpdate:
                        return ('cancel', False)
                return ('scan', store.check_in(tno, signer=SIGNER, now=NOW, operator=self.admin,
                                               environment='testing').ok)
            results, errors = run_together(4, work, self.dir)
            self.assertEqual(errors, [])
            cancelled = any(ok for kind, ok in results if kind == 'cancel')
            admitted = sum(1 for kind, ok in results if kind == 'scan' and ok)
            status = self.store.get_booking(rec['reference'])['passengers'][0]['status']
            self.assertTrue((cancelled and admitted == 0 and status == 'cancelled')
                            or (not cancelled and admitted == 1 and status == 'boarded'), (results, status))


# ====================================================================== nothing from version 2.3 was lost
class RegressionInventoryTests(unittest.TestCase):
    def test_every_original_test_still_exists_or_has_a_documented_replacement(self):
        import ast
        tree = ast.parse((HERE / 'test_all.py').read_text(encoding='utf-8'))
        present = {f'{c.name}.{f.name}' for c in tree.body if isinstance(c, ast.ClassDef)
                   for f in c.body if isinstance(f, ast.FunctionDef)}
        listed = [l.strip() for l in (HERE / 'ORIGINAL_TESTS.txt').read_text().splitlines()
                  if l.strip() and not l.startswith('#')]
        self.assertEqual(len(listed), 107)
        for line in listed:
            name = line.split(' -> ')[-1]
            self.assertIn(name, present, line)

    def test_the_suite_never_reads_the_operators_settings_file(self):
        """The README tells operators to create cta_config.json next to the app. Two routing tests once failed for
        anyone who did, because the suite read that file. Every default settings path must point elsewhere."""
        import cta_sms
        for module in (rt, pay, cta_sms):
            self.assertNotEqual(module.CONFIG_PATH.parent, ROOT, module.__name__)
            self.assertFalse(module.CONFIG_PATH.exists(), module.__name__)

    def test_user_visible_formats_and_choices_are_unchanged(self):
        self.assertEqual([m[0] for m in pay.methods()], ['card', 'mada', 'stcpay', 'paypal', 'tabby', 'tamara', 'cash'])
        self.assertEqual(core.FARE_RATES_CENTS, {'Adult': 2105, 'Child': 1410, 'Senior': 1025, 'Student': 1750})
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, [], names=['Ann Lee'], legs=2)
        p = rec['passengers'][0]
        self.assertRegex(rec['reference'], r'^CTA-\d{8}-\d{3}$')
        self.assertEqual((p['ticket_no'], p['return_ticket_no']), (rec['reference'] + '-P1', rec['reference'] + '-P1R'))
        self.assertEqual(len(rec['recovery_code']), 16)
        self.assertEqual(len(rt.STATIONS), 36)
        import cta_stats
        self.assertEqual(cta_stats.PERIODS, ('Today', 'This week', 'This month', 'This year', 'All time'))


# ====================================================================== the application, end to end
class AppWorkflowTests(unittest.TestCase):
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
        self.tmp = tempfile.mkdtemp()
        p = mock.patch('cta_app.messagebox')
        self.box = p.start(); self.addCleanup(p.stop)

    def app(self, **kw):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp, **kw)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def test_two_windows_booking_at_the_same_moment_both_succeed(self):
        one, two = self.app(), self.app()
        for app, name in ((one, 'Ann Lee'), (two, 'Bo Lee')):
            app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
            app.steppers['Adult'].set(1)
            app.account = {'phone': '+447123456789', 'name': name}
            app.show_confirmation = lambda rec, saved=None: None
        one._issue(['Ann Lee'], 'card')
        two._issue(['Bo Lee'], 'card')                                                # its history did not have one's yet
        refs = sorted(r['reference'] for r in cta_db.Store.open(Path(self.tmp)).load_history())
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(refs[0], refs[1])
        self.box.showerror.assert_not_called()

    def test_passenger_and_staff_workflow_through_the_screens(self):
        app = self.app()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.trip.select('Return')
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        shown = []
        app.show_confirmation = lambda rec, saved=None: shown.append(rec)
        with mock.patch('cta_app.PassengerDialog') as names:
            app.book()
        names.call_args[0][3](['Ann Lee'])                                            # names entered -> payment screen
        with mock.patch('cta_app.PaymentDialog') as dialog:
            names.call_args[0][3](['Ann Lee'])
        dialog.call_args[0][3]('cash')                                               # pay at the counter
        rec = shown[0]
        self.assertTrue((Path(self.tmp) / f"{rec['reference']}.pdf").is_file())
        app.ref_var.set(rec['reference']); app.code_var.set(rec['recovery_code'])    # My ticket: reference + code
        app.lookup()
        self.assertEqual(app.lookup_msg.cget('text'), '')
        app.staff = sign_in(app.store, 'admin')
        app._paint_account()
        app.show_page('admin'); app.admin.refresh()
        tno = rec['passengers'][0]['ticket_no']
        app.admin.tree.selection = lambda: (tno,)
        app.admin._check_in(tno)
        self.assertIn('NOT PAID', app.admin.result.cget('text'))
        app.admin.mark_selected_paid()
        app.admin._check_in(app.signer.sign(app.history[0]['passengers'][0]['ticket_id']))
        self.assertIn('OK to travel', app.admin.result.cget('text'))
        app.admin.tree.selection = lambda: (rec['passengers'][0]['return_ticket_no'],)
        app.admin.cancel_selected('not returning')
        app.admin.refund_selected('')
        self.assertIn('Refund of', app.admin.result.cget('text'))
        out = Path(self.tmp) / 'export.csv'
        app.admin._export_to(out, 'ticket_no\nx\n', app.clock())
        self.assertTrue(out.is_file())
        app.sign_out()
        self.assertIsNone(app.staff)
        self.assertEqual(app.store.verify(), [])


if __name__ == '__main__':
    unittest.main()
