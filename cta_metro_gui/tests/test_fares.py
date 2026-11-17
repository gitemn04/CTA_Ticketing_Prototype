"""Module 4 tests: one fare policy, used identically everywhere, and refusing to charge anything else."""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ['CTA_ENV'] = 'testing'
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)

import cta_core as core
import cta_db
import cta_fares as fares
import cta_payments as pay
import cta_routing as rt
import cta_stats as stats
import cta_tickets as T

NOW = datetime(2026, 10, 25, 9, 0)
STATIONS = sorted(rt.STATIONS)
ONE = {c: 1 for c in fares.CATEGORIES}


def one_way(a, b, cat):
    return fares.quote(a, b, {cat: 1}).total_cents


# ====================================================================== the matrix
class MatrixTests(unittest.TestCase):
    def test_every_station_pair_matches_the_fare_matrix(self):
        rows = fares.read_matrix()
        pairs = {(r['from'], r['to']) for r in rows}
        self.assertEqual(len(rows), len(STATIONS) * (len(STATIONS) - 1))
        self.assertEqual(pairs, {(a, b) for a in STATIONS for b in STATIONS if a != b})
        for r in rows:
            q = fares.quote(r['from'], r['to'], ONE)
            self.assertEqual(q.zones, int(r['zones']), r)
            self.assertEqual((r['from_zone'], r['to_zone']), (rt.ZONES[r['from']], rt.ZONES[r['to']]))
            for cat in fares.CATEGORIES:
                self.assertEqual(q.leg_fare(cat), int(r[cat.lower()]), (r['from'], r['to'], cat))
                self.assertEqual(one_way(r['from'], r['to'], cat), int(r[cat.lower()]))
        fares.check_against_matrix()

    def test_known_journeys(self):
        cases = [  # (from, to, zones, why)
            ('Centrala', 'Jaund', 1, 'same zone, Central'),
            ('Brunad', 'Erean', 1, 'same zone, Downtown, same branch'),
            ('Agralle', 'Docia', 1, 'same zone, Midtown'),
            ('Centrala', 'Quthiel', 2, 'crosses one boundary'),
            ('Yaen', 'Wicyt', 2, 'crosses one boundary on a branch'),
            ('Centrala', 'Zord', 3, 'passes through three zones'),
            ('Zord', 'Centrala', 3, 'the reverse direction'),
            ('Quthiel', 'Zord', 2, 'Midtown to Downtown on one branch'),
            ('Zord', 'Marend', 1, 'different branches, both Downtown: the band rule charges 1 zone'),
            ('Wicyt', 'Riladia', 1, 'different branches, both Midtown'),
        ]
        for a, b, zones, why in cases:
            q = fares.quote(a, b, {'Adult': 1})
            self.assertEqual(q.zones, zones, why)
            self.assertEqual(q.total_cents, 2105 * zones, why)

    def test_the_other_rule_is_reported_but_never_charged(self):
        r = rt.astar('Zord', 'Marend')
        self.assertEqual(fares.zones_by_rule('route', 'Zord', 'Marend', r), 3)
        self.assertEqual(fares.quote('Zord', 'Marend', {'Adult': 1}, route=r).zones, 1)
        diff = fares.compare_rules()
        self.assertEqual(len(diff), 584)
        self.assertTrue(all(y > x for _a, _b, x, y in diff))
        self.assertIn(('Adohad', 'Brunad', 1, 3), diff)
        self.assertEqual(fares.ACTIVE_RULE, 'band')
        self.assertEqual(fares.FARE_POLICY_STATUS, 'unconfirmed')            # nobody has approved it yet

    def test_fares_are_the_same_in_both_directions(self):
        for a in STATIONS:
            for b in STATIONS:
                if a < b:
                    self.assertEqual(fares.quote(a, b, ONE).total_cents, fares.quote(b, a, ONE).total_cents, (a, b))

    def test_matrix_tool(self):
        sys.path.insert(0, str(HERE.parent / 'tools'))
        import fare_matrix, io, contextlib
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out):
            self.assertEqual(fare_matrix.main(['--check']), 0)
            self.assertEqual(fare_matrix.main(['--compare', '--out', str(Path(d) / 'c.csv')]), 0)
            with open(Path(d) / 'c.csv', newline='') as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 584)
        self.assertIn('584 of 1260', out.getvalue())


# ====================================================================== validation
class ValidationTests(unittest.TestCase):
    def test_invalid_stations_are_rejected(self):
        for a, b in (('Atlantis', 'Zord'), ('Zord', 'Atlantis'), ('centrala', 'Zord'), ('', 'Zord'), (None, 'Zord'),
                     ('Zord', 'Zord'), ('Centrala', ' Zord')):
            with self.assertRaises(fares.FareError, msg=(a, b)):
                fares.quote(a, b, {'Adult': 1})

    def test_impossible_and_mismatched_routes_are_rejected(self):
        graph = {k: dict(v) for k, v in rt.GRAPH.items()}
        del graph['Centrala']['Yaen'], graph['Yaen']['Centrala']               # cut the Zord branch off
        with mock.patch.object(rt, 'GRAPH', graph):
            with self.assertRaises(fares.FareError) as cm:
                fares.quote('Centrala', 'Zord', {'Adult': 1})
            self.assertIn('no route', str(cm.exception))
        other = rt.astar('Centrala', 'Marend')
        with self.assertRaises(fares.FareError):
            fares.quote('Centrala', 'Zord', {'Adult': 1}, route=other)           # a route for another journey
        fake = rt.Route(('Centrala', 'Zord'), (), 1.0, 1.0, ('Central', 'Downtown'))
        with self.assertRaises(fares.FareError):
            fares.quote('Centrala', 'Zord', {'Adult': 1}, route=fake)            # a link that does not exist
        with self.assertRaises(ValueError):
            core.make_record(rt.astar('Zord', 'Zord'), {'Adult': 1}, 1, NOW, NOW, [], names=['Ann Lee'])

    def test_passenger_types_counts_and_journeys_are_checked(self):
        for counts in ({'Pet': 1}, {'Adult': 0}, {}, {'Adult': -1}, {'Adult': 1.5}, {'Adult': True}, {'Adult': 100},
                       'Adult'):
            with self.assertRaises(fares.FareError, msg=counts):
                fares.quote('Centrala', 'Zord', counts)
        for legs in (0, 3, True, '2'):
            with self.assertRaises(fares.FareError, msg=legs):
                fares.quote('Centrala', 'Zord', {'Adult': 1}, legs)

    def test_a_booking_with_the_wrong_zone_count_is_refused(self):
        route = rt.astar('Centrala', 'Zord')
        for wrong in (1, 2, 4):
            with self.assertRaises(ValueError) as cm:
                core.make_record(route, {'Adult': 1}, wrong, NOW, NOW, [], names=['Ann Lee'])
            self.assertIn('Fare mismatch', str(cm.exception))
        self.assertEqual(core.make_record(route, {'Adult': 1}, 3, NOW, NOW, [], names=['Ann Lee'])['total_cents'], 6315)


# ====================================================================== returns, rounding, categories
class RuleTests(unittest.TestCase):
    def test_return_is_two_one_way_journeys_with_the_same_zones(self):
        for a in STATIONS:
            for b in STATIONS:
                if a == b:
                    continue
                out, ret = fares.quote(a, b, ONE, 1), fares.quote(a, b, ONE, 2)
                self.assertEqual(ret.total_cents, 2 * out.total_cents)
                self.assertEqual(ret.zones, fares.quote(b, a, ONE).zones)          # the way back costs the same
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1, 'Child': 1}, 3, NOW, NOW, [],
                               names=['Ann Lee', 'Kid Lee'], legs=2)
        for p in rec['passengers']:
            self.assertEqual(p['fare_cents'], 2 * p['leg_fare_cents'])
            self.assertEqual(p['leg_fare_cents'], fares.FARE_RATES_CENTS[p['category']] * 3)
        self.assertEqual(rec['total_cents'], 2 * (2105 + 1410) * 3)

    def test_money_is_whole_cents_and_every_passenger_adds_up_exactly(self):
        groups = [{'Adult': 2, 'Child': 1, 'Senior': 1, 'Student': 1}, {'Senior': 20}, {'Child': 7, 'Student': 13}]
        for counts in groups:
            for legs in (1, 2):
                for a, b in (('Centrala', 'Jaund'), ('Centrala', 'Quthiel'), ('Centrala', 'Zord')):
                    q = fares.quote(a, b, counts, legs)
                    self.assertIsInstance(q.total_cents, int)
                    expected = sum(fares.FARE_RATES_CENTS[c] * n for c, n in counts.items()) * q.zones * legs
                    self.assertEqual(q.total_cents, expected)
                    self.assertEqual(sum(q.passenger_fare(c) * n for c, n in counts.items()), q.total_cents)
                    shown = sum(int(line.rsplit('= ', 1)[1].split()[0].replace(',', ''))
                                for line in q.summary().splitlines())
                    self.assertEqual(shown, q.total_cents)                         # "Fare details" adds up too
        self.assertEqual(core.trip_total({'Child': 1, 'Student': 1}, 3), 9480)      # original kiosk values unchanged
        self.assertEqual(core.trip_total({'Adult': 2}, 1), 4210)

    def test_category_rates_are_applied_per_passenger(self):
        q = fares.quote('Centrala', 'Quthiel', {'Adult': 1, 'Child': 1, 'Senior': 1, 'Student': 1})
        self.assertEqual({c: q.leg_fare(c) for c in fares.CATEGORIES},
                         {'Adult': 4210, 'Child': 2820, 'Senior': 2050, 'Student': 3500})


# ====================================================================== configuration fails safely
class PolicyConfigTests(unittest.TestCase):
    def test_bad_rates_stop_every_fare(self):
        for bad in ({'Adult': 21.05}, {'Adult': 0}, {'Adult': -5}, {'Adult': True}, {'Adult': '2105'}):
            with mock.patch.dict(fares.FARE_RATES_CENTS, bad):
                with self.assertRaises(fares.FarePolicyError, msg=bad):
                    fares.quote('Centrala', 'Zord', {'Child': 1})
        with mock.patch.dict(fares.FARE_RATES_CENTS, {'Pensioner': 900}):
            with self.assertRaises(fares.FarePolicyError):
                fares.validate_policy()
        broken = dict(fares.FARE_RATES_CENTS); del broken['Student']
        with self.assertRaises(fares.FarePolicyError):
            fares.validate_policy(broken)
        with mock.patch.object(fares, 'ACTIVE_RULE', 'distance'):
            with self.assertRaises(fares.FarePolicyError):
                fares.quote('Centrala', 'Zord', {'Adult': 1})

    def test_bad_zones_stop_every_fare(self):
        zones = dict(rt.ZONES); del zones['Zord']
        with mock.patch.object(rt, 'ZONES', zones):
            with self.assertRaises(fares.FarePolicyError):
                fares.quote('Centrala', 'Jaund', {'Adult': 1})
        with mock.patch.dict(rt.ZONE_BAND, {'Downtown': 4}):
            with self.assertRaises(fares.FarePolicyError):
                fares.validate_policy()
        with mock.patch.dict(rt.ZONE_BAND, {'Suburbs': 4}):
            with self.assertRaises(fares.FarePolicyError):
                fares.validate_policy()
        names = {k: tuple(v) for k, v in rt.ZONE_NAMES.items()}
        names['Midtown'] = names['Midtown'] + ('Zord',)
        with mock.patch.object(rt, 'ZONE_NAMES', names):
            with self.assertRaises(fares.FarePolicyError):
                fares.validate_policy()

    def test_missing_damaged_or_different_matrix_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'fare_matrix.csv'
            with self.assertRaises(fares.FarePolicyError) as cm:
                fares.check_against_matrix(path)
            self.assertIn('missing', str(cm.exception))
            path.write_text('from,to\nA,B\n')
            with self.assertRaises(fares.FarePolicyError):
                fares.check_against_matrix(path)
            path.write_bytes(b'\xff\xfe\x00')
            with self.assertRaises(fares.FarePolicyError):
                fares.check_against_matrix(path)
            fares.write_matrix(path)
            fares.check_against_matrix(path)
            text = path.read_text().replace('Adohad,Brunad,Downtown,Downtown,1,2105', 'Adohad,Brunad,Downtown,Downtown,3,6315')
            path.write_text(text)                                          # someone edits one fare
            with self.assertRaises(fares.FarePolicyError) as cm:
                fares.check_against_matrix(path)
            self.assertIn('Adohad -> Brunad', str(cm.exception))
            fares.write_matrix(path)
            with mock.patch.dict(fares.FARE_RATES_CENTS, {'Adult': 2200}):  # someone edits a rate in the code
                with self.assertRaises(fares.FarePolicyError):
                    fares.check_against_matrix(path)
            lines = path.read_text().splitlines()
            path.write_text('\n'.join(lines[:-1]) + '\n')                  # a journey missing from the matrix
            with self.assertRaises(fares.FarePolicyError):
                fares.check_against_matrix(path)

    def test_the_shipped_matrix_is_what_the_code_charges(self):
        fares.check_against_matrix()


# ====================================================================== stored bookings must match
class IntegrityTests(unittest.TestCase):
    def rec(self, method='card', legs=1):
        r = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1, 'Child': 1}, 3, NOW, NOW, [],
                             names=['Ann Lee', 'Kid Lee'], legs=legs)
        r['payment'] = pay.pay(method, r['total_cents'], r['reference'], 'x', NOW, environment='testing')
        return r

    def test_a_consistent_booking_has_no_problems(self):
        for legs in (1, 2):
            self.assertEqual(fares.fare_problems(self.rec(legs=legs)), [])
        old = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, [])   # pre-2.1 shape
        self.assertEqual(fares.fare_problems(old), [])

    def test_every_kind_of_mismatch_is_detected(self):
        changes = {
            'total': lambda r: r.update(total_cents=r['total_cents'] - 1),
            'passenger fare': lambda r: r['passengers'][0].update(fare_cents=r['passengers'][0]['fare_cents'] + 5),
            'payment amount': lambda r: r['payment'].update(amount_cents=1),
            'zones': lambda r: r.update(zones=1),
        }
        for why, change in changes.items():
            r = self.rec(); change(r)
            self.assertTrue(fares.fare_problems(r), why)

    def test_an_audited_cancellation_is_an_allowed_adjustment(self):
        r = self.rec(method='cash')
        self.assertTrue(T.cancel_ticket(r, r['passengers'][0]['ticket_no'], 'admin', NOW, 'ill')[0])
        self.assertLess(r['payment']['amount_cents'], r['total_cents'])
        self.assertEqual(r['payment']['original_amount_cents'], r['total_cents'])
        self.assertEqual(fares.fare_problems(r), [])

    def test_bookings_from_an_older_policy_are_only_checked_for_internal_consistency(self):
        r = self.rec()
        r['fare_policy_version'] = 'CTA-FARES-1'
        r['zones'] = 2
        self.assertEqual(fares.fare_problems(r), [])                         # sold under its own rules; still adds up
        r['payment']['amount_cents'] = 5
        self.assertTrue(fares.fare_problems(r))

    def test_database_verify_reports_a_tampered_fare_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            store = cta_db.Store.open(Path(d), NOW)
            r = self.rec()
            store.save_history([r], NOW)
            self.assertEqual(store.verify(), [])
            doc = json.loads(store.conn.execute('SELECT doc FROM bookings').fetchone()[0])
            doc['total_cents'] = 1
            store.conn.execute('UPDATE bookings SET doc = ?, total_cents = 1', (json.dumps(doc),))
            problems = store.verify()
            self.assertTrue(any(r['reference'] in p and 'total' in p for p in problems), problems)
            self.assertEqual(store.load_history()[0]['total_cents'], 1)    # reported, not "repaired"
            store.close()


# ====================================================================== the app: one fare everywhere
class AppFareTests(unittest.TestCase):
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
        guard = mock.patch('cta_app.messagebox')                  # a refusal must fail a test, never block it
        guard.start()
        self.addCleanup(guard.stop)

    def app(self):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def plan(self, app, a='Zord', b='Marend', counts=(('Adult', 2), ('Child', 1)), trip='Return'):
        app.from_pick.set(a); app.to_pick.set(b); app.search()
        for cat, n in counts:
            app.steppers[cat].set(n)
        app.trip.select(trip)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}

    def test_the_same_journey_shows_and_charges_the_same_fare_everywhere(self):
        app = self.app()
        self.plan(app)
        expected = fares.quote('Zord', 'Marend', {'Adult': 2, 'Child': 1}, 2).total_cents
        self.assertEqual(expected, (2 * 2105 + 1410) * 1 * 2)
        text = f'{expected:,} cents'
        self.assertEqual(app.bar_total.cget('text'), text)                                  # 1 the fare bar
        with mock.patch('cta_app.messagebox') as box:
            app.fare_details()
        self.assertIn(f'= {2 * 2105 * 2:,} cents', box.showinfo.call_args[0][1])          # 2 fare details
        with mock.patch('cta_app.PaymentDialog') as dialog:
            app._names_done(['Ann Lee', 'Bo Lee', 'Kid Lee'])
        self.assertEqual(dialog.call_args[0][1], expected)                                 # 3 payment screen
        charged = []
        real_pay = pay.pay

        def spy(method, amount, *a, **k):
            charged.append(amount)
            return real_pay(method, amount, *a, **k)
        app.show_confirmation = lambda rec, saved=None: None
        with mock.patch('cta_app.payments.pay', side_effect=spy):
            dialog.call_args[0][3]('card')
        rec = app.history[-1]
        self.assertEqual(charged, [expected])                                              # 4 amount sent to payment
        self.assertEqual(rec['total_cents'], expected)                                     # 5 booking record
        self.assertEqual(rec['payment']['amount_cents'], expected)                         # 6 payment record
        self.assertEqual(sum(p['fare_cents'] for p in rec['passengers']), expected)        # 7 per passenger
        self.assertEqual(stats.summarize([rec], 'testing')['revenue_cents'], expected)    # 8 reports
        self.assertEqual(fares.fare_problems(rec), [])
        import tkinter as tk
        frame = app.ticket_view(self.root, rec)
        labels = []
        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, tk.Label):
                    labels.append(str(c.cget('text')))
                walk(c)
        walk(frame); frame.destroy()
        self.assertIn(f'Total  {text}', labels)                                            # 9 ticket screen
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        pdf = Path(self.tmp) / f"{rec['reference']}.pdf"
        receipt = PdfReader(str(pdf)).pages[-1].extract_text()
        self.assertIn(f'{expected:,}', receipt)                                            # 10 PDF receipt
        first = PdfReader(str(pdf)).pages[0].extract_text()
        self.assertIn(f"{rec['passengers'][0]['leg_fare_cents']:,} cents", first)          # 11 PDF ticket

    def test_a_fare_that_changed_after_it_was_shown_is_not_charged(self):
        app = self.app()
        self.plan(app, 'Centrala', 'Zord', (('Adult', 1),), 'One way')
        with mock.patch('cta_app.PaymentDialog') as dialog:
            app._names_done(['Ann Lee'])
        app._shown_total -= 100                                    # what the passenger saw is not what would be charged
        with mock.patch('cta_app.messagebox') as box, mock.patch('cta_app.payments.pay') as charge:
            dialog.call_args[0][3]('card')
        charge.assert_not_called()
        self.assertIn('fare changed', box.showerror.call_args[0][1])
        self.assertEqual(app.history, [])

    def test_a_payment_for_a_different_amount_issues_no_ticket(self):
        app = self.app()
        self.plan(app, 'Centrala', 'Zord', (('Adult', 1),), 'One way')
        app.show_confirmation = lambda rec, saved=None: None
        wrong = {'method': 'card', 'label': 'Card', 'amount_cents': 1, 'status': 'paid', 'gateway': 'simulated'}
        with mock.patch('cta_app.messagebox') as box, mock.patch('cta_app.payments.pay', return_value=wrong):
            app._issue(['Ann Lee'], 'card')
        self.assertEqual(app.history, [])
        self.assertIn('payment amount differs', box.showerror.call_args[0][1])

    def test_a_missing_fare_matrix_pauses_sales_but_not_the_rest_of_the_app(self):
        with mock.patch.object(fares, 'MATRIX_FILE', Path(self.tmp) / 'nope.csv'):
            app = self.app()
        self.assertIn('fare matrix', app.fare_problem)
        self.assertIn(app.fare_problem, app.problems)
        self.assertFalse(app.can_issue())
        self.assertTrue(app.can_save())                                       # check-in, payments etc. still work
        self.plan(app, 'Centrala', 'Zord', (('Adult', 1),), 'One way')
        self.assertEqual(app.bar_total.cget('text'), '—')                     # no fare is shown...
        self.assertFalse(app.book_btn.enabled)                                # ...and nothing can be booked
        with mock.patch('cta_app.messagebox') as box:
            app.book()
            app.fare_details()
        self.assertEqual(box.showerror.call_args_list[0][0][0], 'Bookings are paused')
        self.assertIn('fare matrix', box.showerror.call_args_list[1][0][1])


if __name__ == '__main__':
    unittest.main()
