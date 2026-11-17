import heapq, json, math, os, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ['CTA_ENV'] = 'testing'      # the suite runs in testing mode; production behaviour is tested explicitly
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)   # fixed throw-away signing key for the tests (never a real key)

import cta_accounts as _acct
import cta_auth as _auth
import cta_payments as _pay
import cta_routing as _rt
import cta_sms as _sms
import tempfile as _tempfile
from pathlib import Path as _Path

# The suite must never read the operator's real cta_config.json (the README tells people to create one next to the
# app): point every default settings path at a file that does not exist. Tests that need settings pass their own.
_NO_SETTINGS = _Path(_tempfile.mkdtemp(prefix='cta-tests-')) / 'cta_config.json'
_pay.CONFIG_PATH = _rt.CONFIG_PATH = _sms.CONFIG_PATH = _NO_SETTINGS
PRODUCTION_PBKDF2_ITERATIONS = _acct.PBKDF2_ITERATIONS
_acct.PBKDF2_ITERATIONS = 1000      # fast hashing in tests only; the production value is asserted separately
_auth.SESSION_HOURS = 24 * 400       # tests jump days ahead; session expiry has its own test with the real value
TEST_PASSWORD = 'test-pass-1'
_ROOT_ADMINS = {}


def sign_in(store, username='admin', role='admin', now=None):
    """Sign a staff member in through the real, audited services (creating the account first if needed)."""
    from datetime import datetime as _dt
    now = now or _dt(2026, 10, 1, 8, 0)
    if store.staff_count() == 0:
        first = username if role == 'admin' else 'root'
        _ROOT_ADMINS[str(store.path)] = (first, store.bootstrap_admin(first, TEST_PASSWORD, now))
        if first == username:
            return _ROOT_ADMINS[str(store.path)][1]
    known = {s['username'].lower() for s in store.load_staff()}
    if username.lower() not in known:
        admin_name = next(s['username'] for s in store.load_staff() if s.get('role') == 'admin')
        admin = store.staff_login(admin_name, TEST_PASSWORD, now)
        assignable = _auth.ASSIGNABLE_ROLES
        _auth.ASSIGNABLE_ROLES = tuple(_auth.ROLE_PERMISSIONS)       # tests may use the proposed roles
        try:
            store.create_staff_account(admin, username, TEST_PASSWORD, role, now)
        finally:
            _auth.ASSIGNABLE_ROLES = assignable
    return store.staff_login(username, TEST_PASSWORD, now)


import cta_accounts as acct
import cta_core as core
import cta_db
import cta_storage
import cta_payments as pay
import cta_routing as rt
import cta_sms
import cta_stats as stats


class NetworkTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(rt.STATIONS), 36)
        self.assertEqual(len(rt.LINKS), 35)
        self.assertEqual({z: len(s) for z, s in rt.ZONE_NAMES.items()}, {'Central': 10, 'Midtown': 12, 'Downtown': 14})
        self.assertEqual(set(rt.ZONES), set(rt.STATIONS))

    def test_connected_tree(self):
        for s in rt.STATIONS:
            self.assertEqual(rt.astar('Centrala', s).destination, s)
        self.assertEqual(len(rt.LINKS), len(rt.STATIONS) - 1)

    def test_edges_symmetric_positive(self):
        for a, b in rt.LINKS:
            self.assertAlmostEqual(rt.GRAPH[a][b], rt.GRAPH[b][a])
            self.assertGreater(rt.GRAPH[a][b], 0)


class RouteTests(unittest.TestCase):
    def test_zord(self):
        r = rt.astar('Centrala', 'Zord')
        self.assertEqual(r.stations, ('Centrala', 'Yaen', 'Quthiel', 'Zord'))
        self.assertAlmostEqual(r.km, 4.71, places=2)
        self.assertEqual(r.zones_entered, ('Central', 'Midtown', 'Downtown'))

    def test_same_station(self):
        r = rt.astar('Jaund', 'Jaund')
        self.assertEqual((r.km, r.ride_minutes, r.stops), (0, 0, 0))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            rt.astar('London', 'Zord')

    def test_symmetry(self):
        a, b = rt.astar('Erean', 'Ruril'), rt.astar('Ruril', 'Erean')
        self.assertEqual(a.stations, tuple(reversed(b.stations)))
        self.assertAlmostEqual(a.km, b.km)

    def test_matches_dijkstra_all_pairs(self):
        def dij(a, b):
            heap, best = [(0, a)], {a: 0}
            while heap:
                c, n = heapq.heappop(heap)
                if c > best[n] + 1e-9: continue
                if n == b: return c
                for m, w in rt.GRAPH[n].items():
                    if c + w < best.get(m, math.inf) - 1e-9:
                        best[m] = c + w; heapq.heappush(heap, (c + w, m))
        for a in rt.STATIONS:
            for b in rt.STATIONS:
                self.assertAlmostEqual(rt.astar(a, b).km, dij(a, b), places=7)

    def test_time_is_distance_over_speed_plus_dwell(self):
        r = rt.astar('Centrala', 'Zord')
        self.assertAlmostEqual(r.ride_minutes, r.km / rt.TRAIN_SPEED_KMH * 60 + 2 * rt.DWELL_MIN)


class TimetableTests(unittest.TestCase):
    def test_next_departures_spacing(self):
        d = rt.next_departures(datetime(2026, 10, 24, 14, 2, 10), 4)
        self.assertEqual([x.strftime('%H:%M') for x in d], ['14:06', '14:12', '14:18', '14:24'])

    def test_exact_slot_is_included(self):
        self.assertEqual(rt.next_departures(datetime(2026, 10, 24, 14, 6), 1)[0].strftime('%H:%M'), '14:06')

    def test_before_service(self):
        self.assertEqual(rt.next_departures(datetime(2026, 10, 24, 3, 30), 1)[0].strftime('%H:%M'), '05:00')

    def test_after_last_train_rolls_to_next_morning(self):
        d = rt.next_departures(datetime(2026, 10, 24, 23, 56), 2)
        self.assertEqual([x.strftime('%d %H:%M') for x in d], ['25 05:00', '25 05:06'])

    def test_stop_times_monotonic_and_match_ride(self):
        r = rt.astar('Zord', 'Marend')
        dep = datetime(2026, 10, 24, 10, 0)
        rows = rt.stop_times(r, dep)
        self.assertEqual(len(rows), len(r.stations))
        self.assertEqual([x[0] for x in rows], list(r.stations))
        times = [x[1] for x in rows]
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual((times[-1] - dep).total_seconds() / 60, r.ride_minutes, places=6)
        self.assertAlmostEqual(rows[-1][2], r.km)


class ProviderTests(unittest.TestCase):
    class Resp:
        def __init__(self, data): self.data = data
        def read(self): return json.dumps(self.data).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def provider(self, opener):
        geo = {'Centrala': (0.0, 0.0), 'Zord': (0.1, 0.1)}
        return rt.GoogleRoutesProvider('KEY', geo, opener=opener)

    def test_google_values_replace_simulated(self):
        seen = {}
        def opener(req, timeout=0):
            seen['key'] = req.get_header('X-goog-api-key'); seen['body'] = json.loads(req.data)
            return self.Resp({'routes': [{'distanceMeters': 12000, 'duration': '1500s'}]})
        r = self.provider(opener).plan('Centrala', 'Zord')
        self.assertEqual(seen['key'], 'KEY')
        self.assertEqual(seen['body']['travelMode'], 'TRANSIT')
        self.assertAlmostEqual(r.km, 12.0); self.assertAlmostEqual(r.ride_minutes, 25.0)
        self.assertAlmostEqual(sum(l.km for l in r.legs), 12.0)
        self.assertEqual(r.source, 'Google Routes API')

    def test_failure_falls_back_to_simulated(self):
        def opener(req, timeout=0): raise OSError('offline')
        r = self.provider(opener).plan('Centrala', 'Zord')
        self.assertAlmostEqual(r.km, rt.astar('Centrala', 'Zord').km)
        self.assertIn('simulated', r.note)

    def test_missing_coordinates_fall_back(self):
        r = self.provider(lambda *a, **k: None).plan('Centrala', 'Yaen')
        self.assertIn('simulated', r.note.lower())

    def test_default_provider_is_simulated(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsInstance(rt.load_provider(Path(d) / 'none.json'), rt.SimulatedProvider)


class FareTests(unittest.TestCase):
    def test_zone_rules(self):
        self.assertEqual(core.band_zones('Centrala', 'Zord'), 3)
        self.assertEqual(core.band_zones('Jaund', 'Soth'), 1)
        self.assertEqual(core.band_zones('Jaund', 'Quthiel'), 2)
        self.assertEqual(core.band_zones('Zord', 'Centrala'), 3)

    def test_route_mode_differs_for_cross_branch(self):
        r = rt.astar('Zord', 'Marend')
        self.assertEqual(core.band_zones('Zord', 'Marend'), 1)
        self.assertEqual(core.route_zones(r), 3)
        self.assertEqual(core.chargeable_zones('Zord', 'Marend', r, mode='route'), 3)

    def test_rates(self):
        self.assertEqual(core.FARE_RATES_CENTS, {'Adult': 2105, 'Child': 1410, 'Senior': 1025, 'Student': 1750})

    def test_mixed_group(self):
        ch = core.calculate_charges({'Adult': 2, 'Child': 1, 'Senior': 1, 'Student': 1}, 3)
        self.assertEqual(core.grand_total(ch), (2 * 2105 + 1410 + 1025 + 1750) * 3)

    def test_original_regression_values(self):
        self.assertEqual(core.grand_total(core.calculate_charges({'Child': 1, 'Student': 1}, 3)), 9480)
        self.assertEqual(core.grand_total(core.calculate_charges({'Adult': 2}, 1)), 4210)

    def test_validation(self):
        self.assertFalse(core.validate_counts({'Adult': 0, 'Child': 0})[0])
        self.assertFalse(core.validate_counts({'Adult': -1})[0])
        self.assertFalse(core.validate_counts({'Adult': 100})[0])
        self.assertFalse(core.validate_counts({'Adult': 1.5})[0])
        self.assertFalse(core.validate_counts({'Pet': 1})[0])
        self.assertTrue(core.validate_counts({'Adult': 1})[0])

    def test_amounts_labelled(self):
        self.assertEqual(core.format_cents(4210), '4,210 cents')


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.route = rt.astar('Centrala', 'Zord')
        self.now = datetime(2026, 10, 24, 14, 2, 10)

    def rec(self, hist):
        return core.make_record(self.route, {'Adult': 2}, 3, self.now, self.now, hist)

    def test_reference_sequence_survives_restart(self):
        h = []
        for _ in range(3):
            h.append(self.rec(h))
        self.assertEqual([r['reference'] for r in h], ['CTA-20261024-001', 'CTA-20261024-002', 'CTA-20261024-003'])
        self.assertEqual(len({r['recovery_code'] for r in h}), 3)

    def test_lookup_needs_secret(self):
        r = self.rec([])
        self.assertIsNone(core.find_ticket_with_code([r], r['reference'], 'wrong'))
        self.assertIsNone(core.find_ticket_with_code([r], r['reference'], ''))
        self.assertIs(core.find_ticket_with_code([r], r['reference'].lower(), r['recovery_code'].lower()), r)

    def test_history_roundtrip_and_damaged_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sub' / 'h.json'
            core.save_history(p, [self.rec([])])
            self.assertEqual(len(core.load_history(p)), 1)
            p.write_text('{broken')
            import cta_storage
            with self.assertRaises(cta_storage.DataCorrupted):      # damaged is NOT the same as empty
                core.load_history(p)
            with self.assertRaises(cta_storage.DataCorrupted):      # and it cannot be overwritten
                core.save_history(p, [])
            self.assertEqual(p.read_text(), '{broken')

    def test_pdf_created(self):
        try:
            import reportlab  # noqa
        except ImportError:
            self.skipTest('reportlab missing')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.pdf'
            core.export_ticket_pdf(self.rec([]), p)
            self.assertTrue(p.read_bytes().startswith(b'%PDF'))

    def test_record_total(self):
        self.assertEqual(self.rec([])['total_cents'], 2 * 2105 * 3)


class AccountTests(unittest.TestCase):
    def test_phone_formats_normalise_to_one_form(self):
        for raw in ('+44 7123 456789', '0044 7123-456789', '+44 (7123) 456.789', '  +447123456789 '):
            self.assertEqual(acct.validate_phone(raw), ('+447123456789', ''), raw)

    def test_bad_phones_rejected(self):
        for raw in ('', '   ', 'abc', '+44 71x3 456789', '07123456789', '+0123456789', '+1234567',
                    '+1234567890123456', '+44+7123456789', '4471234567890'):
            number, err = acct.validate_phone(raw)
            self.assertIsNone(number, raw)
            self.assertTrue(err, raw)

    def test_phone_length_limits(self):
        self.assertEqual(acct.validate_phone('+12345678')[0], '+12345678')              # 8 digits: shortest allowed
        self.assertIsNone(acct.validate_phone('+1234567')[0])                          # 7 digits
        self.assertEqual(acct.validate_phone('+123456789012345')[0], '+123456789012345')  # 15 digits: E.164 maximum
        self.assertIsNone(acct.validate_phone('+1234567890123456')[0])                 # 16 digits

    def test_mask_phone(self):
        self.assertEqual(acct.mask_phone('+447123456789', '*'), '+********6789')
        self.assertEqual(acct.mask_phone('+1234'), '+1234')

    def test_good_names(self):
        for raw in ('Jane Doe', 'José Álvarez', "Sean O'Brien", 'Anne-Marie Smith', 'Nguyễn Thị Hồng Ánh',
                    'Айдар Нурланов', 'Dr. Ng', 'Zoë'):
            name, err = acct.validate_person_name(raw)
            self.assertEqual(err, '', raw)
            self.assertEqual(name, ' '.join(raw.split()))

    def test_name_whitespace_collapsed(self):
        self.assertEqual(acct.validate_person_name('  Jane \t  Doe\n')[0], 'Jane Doe')

    def test_bad_names(self):
        for raw in ('', '   ', 'A', 'John3', 'Bob <b>', 'Jo\x07hn', '.-', '--', 'x' * 61, 'Jane_Doe', 'a@b.c'):
            name, err = acct.validate_person_name(raw)
            self.assertEqual(name, '', raw)
            self.assertTrue(err, raw)

    def test_name_length_boundary(self):
        self.assertEqual(acct.validate_person_name('A' * 60)[1], '')
        self.assertNotEqual(acct.validate_person_name('A' * 61)[1], '')

    def test_otp_success_is_single_use(self):
        now = datetime(2026, 10, 24, 10, 0)
        o = acct.OtpSession('+447123456789', now)
        self.assertEqual(len(o.demo_code), 6)
        self.assertTrue(o.demo_code.isdigit())
        self.assertTrue(o.verify(' '.join([o.demo_code[:3], o.demo_code[3:]]), now)[0])   # spaces tolerated
        self.assertFalse(o.verify(o.demo_code, now)[0])                                    # cannot be reused

    def test_otp_three_wrong_tries_lock_the_code(self):
        now = datetime(2026, 10, 24, 10, 0)
        o = acct.OtpSession('+447123456789', now)
        wrong = '000000' if o.demo_code != '000000' else '111111'
        for _ in range(3):
            self.assertFalse(o.verify(wrong, now)[0])
        self.assertFalse(o.verify(o.demo_code, now)[0])        # even the right code fails now

    def test_otp_expires_after_five_minutes(self):
        now = datetime(2026, 10, 24, 10, 0)
        o = acct.OtpSession('+447123456789', now)
        self.assertFalse(o.verify(o.demo_code, now + timedelta(minutes=5, seconds=1))[0])
        o2 = acct.OtpSession('+447123456789', now)
        self.assertTrue(o2.verify(o2.demo_code, now + timedelta(minutes=4, seconds=59))[0])

    def test_otp_empty_entry_does_not_burn_an_attempt(self):
        now = datetime(2026, 10, 24, 10, 0)
        o = acct.OtpSession('+447123456789', now)
        self.assertFalse(o.verify('  ', now)[0])
        self.assertEqual(o.attempts_left, 3)

    def test_accounts_roundtrip_duplicate_and_damaged_file(self):
        now = datetime(2026, 10, 24, 10, 0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'sub' / 'accounts.json'
            accounts = acct.load_accounts(p)
            self.assertEqual(accounts, [])
            a = acct.create_account(accounts, '+447123456789', '  Jane   Doe ', now)
            self.assertEqual(a['name'], 'Jane Doe')
            with self.assertRaises(ValueError):
                acct.create_account(accounts, '+447123456789', 'Someone Else', now)
            with self.assertRaises(ValueError):
                acct.create_account(accounts, '+447000000000', '12345', now)
            acct.save_accounts(p, accounts)
            again = acct.load_accounts(p)
            self.assertEqual(acct.find_account(again, '+447123456789')['name'], 'Jane Doe')
            self.assertIsNone(acct.find_account(again, '+447999999999'))
            p.write_text('{broken')
            import cta_storage
            with self.assertRaises(cta_storage.DataCorrupted):
                acct.load_accounts(p)
            self.assertEqual(p.read_text(), '{broken')


class PassengerTicketTests(unittest.TestCase):
    NAMES = ['Jane Doe', 'Nguyễn Thị Hồng Ánh', 'Айдар Нурланов', 'Sean O\'Brien']

    def setUp(self):
        self.route = rt.astar('Centrala', 'Zord')
        self.now = datetime(2026, 10, 24, 14, 2, 10)
        self.counts = {'Adult': 2, 'Child': 1, 'Student': 1}

    def rec(self, **kw):
        return core.make_record(self.route, self.counts, 3, self.now, self.now, [], names=self.NAMES,
                                account_phone='+447123456789', **kw)

    def test_slots_follow_category_order(self):
        self.assertEqual(core.passenger_slots({'Student': 1, 'Adult': 2, 'Child': 1}),
                         [(1, 'Adult'), (2, 'Adult'), (3, 'Child'), (4, 'Student')])

    def test_named_ticket_numbers_fares_and_total(self):
        r = self.rec()
        pax = r['passengers']
        self.assertEqual([p['ticket_no'] for p in pax], [f"{r['reference']}-P{i}" for i in (1, 2, 3, 4)])
        self.assertEqual([p['name'] for p in pax], self.NAMES)
        self.assertEqual([p['fare_cents'] for p in pax], [2105 * 3, 2105 * 3, 1410 * 3, 1750 * 3])
        self.assertEqual(sum(p['fare_cents'] for p in pax), r['total_cents'])
        self.assertEqual(r['total_cents'], (2 * 2105 + 1410 + 1750) * 3)
        self.assertEqual(r['account_phone'], '+447123456789')

    def test_bad_or_missing_names_refuse_to_issue(self):
        for names in (['Jane Doe'], ['Jane Doe', 'X1', 'A B', 'C D'], ['Jane Doe', '', 'A B', 'C D'],
                      self.NAMES + ['Extra Person']):
            with self.assertRaises(ValueError, msg=str(names)):
                core.make_record(self.route, self.counts, 3, self.now, self.now, [], names=names)

    def test_validate_passenger_names_reports_slot_numbers(self):
        slots = core.passenger_slots({'Adult': 3})
        names, errors = core.validate_passenger_names(slots, ['Jane Doe', '', 'Bob7'])
        self.assertEqual(set(errors), {2, 3})
        self.assertEqual(names[0], 'Jane Doe')
        names, errors = core.validate_passenger_names(slots, ['Jane Doe', ' Joe  Bloggs ', 'Ann Lee'])
        self.assertEqual((errors, names[1]), ({}, 'Joe Bloggs'))

    def test_booking_size_limit(self):
        self.assertTrue(core.validate_booking_size({'Adult': 20})[0])
        self.assertTrue(core.validate_booking_size({'Adult': 10, 'Child': 10})[0])
        self.assertFalse(core.validate_booking_size({'Adult': 20, 'Child': 1})[0])

    def test_old_unnamed_ticket_still_opens(self):
        r = core.make_record(self.route, {'Adult': 2}, 3, self.now, self.now, [])
        self.assertNotIn('passengers', r)
        pax = core.passengers_of(r)
        self.assertEqual(len(pax), 1)
        self.assertTrue(pax[0]['legacy'])
        self.assertEqual(core.ticket_qr_payload(r, pax[0]), r['reference'])

    def test_qr_is_a_signed_token_with_no_personal_data(self):
        import cta_tickets
        r = self.rec()
        p = r['passengers'][1]
        text = core.ticket_qr_payload(r, p)
        self.assertRegex(text, r'^CTA1:[0-9A-F]{4}:[A-Z2-7]{26}:[A-Z2-7]{26}$')
        for private in (p['name'], 'Nguyễn', p['ticket_no'], r['reference'], 'Centrala', 'Zord', r['recovery_code'],
                        r['account_phone'] if 'account_phone' in r else 'no-phone-here'):
            self.assertNotIn(private.upper(), text.upper())
        self.assertEqual(cta_tickets.default_signer().verify(text), p['ticket_id'])

    def _pdf_text(self, path):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('pypdf missing')
        return [pg.extract_text() or '' for pg in PdfReader(str(path)).pages]

    def test_pdf_has_one_named_page_per_passenger_plus_receipt(self):
        try:
            import reportlab  # noqa
        except ImportError:
            self.skipTest('reportlab missing')
        r = self.rec()
        with tempfile.TemporaryDirectory() as d:
            full, tickets_only = Path(d) / 'full.pdf', Path(d) / 'print.pdf'
            core.export_ticket_pdf(r, full)
            core.export_ticket_pdf(r, tickets_only, include_receipt=False)
            pages = self._pdf_text(full)
            only = self._pdf_text(tickets_only)
        self.assertEqual(len(pages), 5)
        self.assertEqual(len(only), 4)
        for i, pax in enumerate(r['passengers']):
            self.assertIn(pax['ticket_no'], pages[i])
            self.assertIn(f'{i + 1} of 4', pages[i])
            self.assertIn('DEPARTS', pages[i])
            self.assertIn(f"{pax['fare_cents']:,} cents", pages[i])
        self.assertIn('Jane Doe', pages[0])
        self.assertIn("Sean O'Brien", pages[3])
        # the private recovery code is on the receipt page only, never on a ticket page
        self.assertIn(r['recovery_code'], pages[4])
        for t in pages[:4] + only:
            self.assertNotIn(r['recovery_code'], t)
        self.assertIn('22,110 cents', pages[4])

    def test_pdf_unicode_names_when_a_unicode_font_exists(self):
        import cta_pdf
        if not cta_pdf.fonts()[2]:
            self.skipTest('no Unicode TrueType font on this machine; names fall back to Western European letters')
        r = self.rec()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'u.pdf'
            core.export_ticket_pdf(r, p)
            pages = self._pdf_text(p)
        self.assertIn('Nguyễn Thị Hồng Ánh', pages[1])
        self.assertIn('Айдар Нурланов', pages[2])

    def test_pdf_very_long_name_is_not_cut(self):
        long_name = "Mary-Jane O'Connor Smith-Whittington-Featherstonehaugh III"
        r = core.make_record(self.route, {'Adult': 1}, 3, self.now, self.now, [], names=[long_name])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'l.pdf'
            core.export_ticket_pdf(r, p)
            first = self._pdf_text(p)[0]
        self.assertNotIn('...', first.split('DEPARTS')[0])
        self.assertIn('Featherstonehaugh III', first)

    def test_pdf_for_old_unnamed_ticket(self):
        r = core.make_record(self.route, {'Adult': 1}, 3, self.now, self.now, [])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'o.pdf'
            core.export_ticket_pdf(r, p)
            self.assertEqual(len(self._pdf_text(p)), 2)

    def test_qr_in_pdf_decodes_to_ticket_text(self):
        try:
            import cv2, subprocess, shutil
        except ImportError:
            self.skipTest('opencv missing')
        if not shutil.which('pdftoppm'):
            self.skipTest('pdftoppm missing')
        r = self.rec()
        # A FIXED ticket id makes this test deterministic. With random ids, OpenCV's standard detector could not read
        # about 1 code in 50 - even as a clean image straight from the qrcode library - while the WeChat decoder read
        # every one: a detector limitation on some patterns, not a printing fault. The fallbacks below cover other
        # OpenCV versions; the decoded text must still be exactly the signed token.
        r['passengers'][1]['ticket_id'] = 'QRTESTFIXEDTICKETIDABCDEF2'
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / 'q.pdf'
            core.export_ticket_pdf(r, pdf, include_receipt=False)
            text = ''
            for dpi in ('200', '300'):
                out = Path(d) / f'pg{dpi}'
                subprocess.run(['pdftoppm', '-r', dpi, '-png', '-f', '2', '-l', '2', str(pdf), str(out)], check=True)
                img = cv2.imread(next(Path(d).glob(f'pg{dpi}*.png')).as_posix())
                h, w = img.shape[:2]
                for candidate in (img, img[int(h * 0.12):int(h * 0.42), int(w * 0.60):int(w * 0.97)]):
                    text = cv2.QRCodeDetector().detectAndDecode(candidate)[0]
                    if text:
                        break
                if not text and hasattr(cv2, 'wechat_qrcode_WeChatQRCode'):
                    found = cv2.wechat_qrcode_WeChatQRCode().detectAndDecode(img)[0]
                    text = found[0] if found else ''
                if text:
                    break
        # The QR now holds readable details followed by the signed token; the token part is what the scanner trusts.
        self.assertEqual(text, core.ticket_qr_text(r, r['passengers'][1]))
        self.assertTrue(text.endswith(' / ' + core.ticket_qr_payload(r, r['passengers'][1])))


class OutputTests(unittest.TestCase):
    """Print / open helpers, with the operating system calls replaced by fakes."""
    PATH = Path('/tmp/CTA-test.pdf')

    def test_print_uses_lp_when_available(self):
        calls = []
        def fake_run(args, **kw):
            calls.append(args)
            return SimpleNamespace(returncode=0)
        with mock.patch.object(sys, 'platform', 'linux'), mock.patch('cta_core.shutil.which', return_value='/usr/bin/lp'), \
                mock.patch('cta_core.subprocess.run', fake_run):
            ok, msg = core.print_pdf(self.PATH)
        self.assertTrue(ok)
        self.assertEqual(calls, [['/usr/bin/lp', str(self.PATH)]])
        self.assertIn('printer', msg)

    def test_print_falls_back_to_opening_the_pdf(self):
        calls = []
        def fake_run(args, **kw):
            calls.append(args[0])
            return SimpleNamespace(returncode=1 if args[0] == '/usr/bin/lp' else 0)
        with mock.patch.object(sys, 'platform', 'linux'), mock.patch('cta_core.shutil.which', return_value='/usr/bin/lp'), \
                mock.patch('cta_core.subprocess.run', fake_run):
            ok, msg = core.print_pdf(self.PATH)
        self.assertEqual(calls, ['/usr/bin/lp', 'xdg-open'])
        self.assertTrue(ok)
        self.assertIn('Ctrl+P', msg)

    def test_print_reports_failure_when_nothing_works(self):
        with mock.patch.object(sys, 'platform', 'linux'), mock.patch('cta_core.shutil.which', return_value=None), \
                mock.patch('cta_core.subprocess.run', side_effect=FileNotFoundError('xdg-open')):
            ok, msg = core.print_pdf(self.PATH)
        self.assertFalse(ok)
        self.assertIn(str(self.PATH), msg)

    def test_windows_print_uses_the_print_verb_and_falls_back(self):
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch('cta_core.os.startfile', create=True) as sf:
            ok, _ = core.print_pdf(self.PATH)
        sf.assert_called_once_with(str(self.PATH), 'print')
        self.assertTrue(ok)
        seen = []
        def flaky(path, verb=None):
            seen.append(verb)
            if verb == 'print':
                raise OSError('no print handler')
        with mock.patch.object(sys, 'platform', 'win32'), mock.patch('cta_core.os.startfile', flaky, create=True):
            ok, msg = core.print_pdf(self.PATH)
        self.assertEqual(seen, ['print', None])
        self.assertTrue(ok)
        self.assertIn('Ctrl+P', msg)

    def test_downloads_dir_falls_back(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch('cta_core.Path.home', return_value=Path(home)):
                self.assertEqual(core.downloads_dir(Path('/x')), Path('/x'))
                (Path(home) / 'Downloads').mkdir()
                self.assertEqual(core.downloads_dir(Path('/x')), Path(home) / 'Downloads')


class StaffTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 9, 0)

    def test_username_and_password_rules(self):
        self.assertEqual(acct.validate_username(' admin.1 ')[0], 'admin.1')
        for bad in ('', 'ab', 'a b c', 'x' * 31, 'jo<hn>'):
            self.assertTrue(acct.validate_username(bad)[1], bad)
        self.assertEqual(acct.validate_password('abc12345'), '')
        for bad in ('', 'short1', 'allletters', '12345678'):
            self.assertTrue(acct.validate_password(bad), bad)

    def test_password_is_hashed_salted_and_checked(self):
        a, b = acct.hash_password('secret123'), acct.hash_password('secret123')
        self.assertNotEqual(a['salt'], b['salt'])
        self.assertNotIn('secret123', json.dumps(a))
        self.assertTrue(acct.check_password('secret123', a))
        self.assertFalse(acct.check_password('Secret123', a))
        self.assertFalse(acct.check_password('x', {'salt': 'zz'}))        # damaged record never crashes

    def test_create_authenticate_save_load(self):
        staff = []
        rec = acct.create_staff(staff, 'Admin', 'secret123', self.NOW)
        self.assertEqual(rec['role'], 'admin')
        with self.assertRaises(ValueError):
            acct.create_staff(staff, 'admin', 'another123', self.NOW)      # same name, any case
        with self.assertRaises(ValueError):
            acct.create_staff(staff, 'second', 'weak', self.NOW)
        self.assertIsNotNone(acct.authenticate_staff(staff, 'ADMIN', 'secret123'))
        self.assertIsNone(acct.authenticate_staff(staff, 'admin', 'wrong-pass1'))
        self.assertIsNone(acct.authenticate_staff(staff, 'nobody', 'secret123'))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'staff.json'
            acct.save_staff(p, staff)
            self.assertNotIn('secret123', p.read_text())
            self.assertIsNotNone(acct.authenticate_staff(acct.load_staff(p), 'admin', 'secret123'))


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False


class SmsTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 9, 0)

    def provider(self, status, body, seen):
        def opener(req, timeout=None):
            seen.append(req)
            if status >= 400:
                import io, urllib.error
                raise urllib.error.HTTPError(req.full_url, status, 'x', {}, io.BytesIO(json.dumps(body).encode()))
            return FakeResponse(status, body)
        return cta_sms.TwilioVerify('ACtest', 'token', 'VAservice', opener=opener)

    def test_twilio_start_sends_sms_request(self):
        seen = []
        ok, msg = self.provider(201, {'status': 'pending'}, seen).start('+447123456789', self.NOW)
        self.assertTrue(ok)
        req = seen[0]
        self.assertEqual(req.full_url, 'https://verify.twilio.com/v2/Services/VAservice/Verifications')
        self.assertIn('To=%2B447123456789', req.data.decode())
        self.assertIn('Channel=sms', req.data.decode())
        import base64
        self.assertEqual(req.get_header('Authorization'), 'Basic ' + base64.b64encode(b'ACtest:token').decode())

    def test_twilio_start_errors_are_readable(self):
        ok, msg = self.provider(429, {}, []).start('+447123456789', self.NOW)
        self.assertFalse(ok); self.assertIn('wait', msg)
        ok, msg = self.provider(400, {'message': 'Invalid parameter: To'}, []).start('+1', self.NOW)
        self.assertEqual((ok, msg), (False, 'Invalid parameter: To'))
        def offline(req, timeout=None): raise OSError('no network')
        ok, msg = cta_sms.TwilioVerify('a', 'b', 'c', opener=offline).start('+447123456789', self.NOW)
        self.assertFalse(ok); self.assertIn('internet', msg)

    def test_twilio_check_results(self):
        seen = []
        self.assertTrue(self.provider(200, {'status': 'approved'}, seen).check('+447123456789', '12 34 56', self.NOW)[0])
        self.assertIn('Code=123456', seen[0].data.decode())
        self.assertTrue(seen[0].full_url.endswith('/VerificationCheck'))
        self.assertFalse(self.provider(200, {'status': 'pending'}, []).check('+447123456789', '000000', self.NOW)[0])
        ok, msg = self.provider(404, {}, []).check('+447123456789', '123456', self.NOW)
        self.assertFalse(ok); self.assertIn('expired', msg)
        self.assertFalse(self.provider(200, {'status': 'approved'}, []).check('+447123456789', '  ', self.NOW)[0])

    def test_on_screen_fallback_flow(self):
        p = cta_sms.OnScreenCodes()
        self.assertTrue(p.shows_code)
        self.assertFalse(p.check('+447123456789', '123456', self.NOW)[0])          # no code requested yet
        p.start('+447123456789', self.NOW)
        self.assertTrue(p.check('+447123456789', p.code_for('+447123456789'), self.NOW)[0])

    def test_channel_choice_is_sent_and_bad_channels_fall_back_to_sms(self):
        seen = []
        p = cta_sms.TwilioVerify('AC', 't', 'VA', channel='whatsapp',
                                 opener=lambda req, timeout=None: (seen.append(req), FakeResponse(201, {'status': 'pending'}))[1])
        p.start('+966512345678', self.NOW)
        self.assertIn('Channel=whatsapp', seen[0].data.decode())
        self.assertEqual(cta_sms.TwilioVerify('AC', 't', 'VA', channel='fax').channel, 'sms')

    def test_setup_helper_writes_config_and_keeps_other_settings(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / 'cta_config.json'
            cfg.write_text(json.dumps({'provider': 'simulated', 'google_api_key': 'k'}))
            cta_sms.write_sms_config(cfg, ' AC1 ', 'tok', 'VA1', 'call')
            saved = json.loads(cfg.read_text())
            self.assertEqual(saved['google_api_key'], 'k')
            self.assertEqual(saved['sms'], {'provider': 'twilio_verify', 'account_sid': 'AC1', 'auth_token': 'tok',
                                            'service_sid': 'VA1', 'channel': 'call'})
            p = cta_sms.load_sms_provider(cfg)
            self.assertIsInstance(p, cta_sms.TwilioVerify)
            self.assertEqual(p.channel, 'call')

    def test_provider_loading(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
                os.environ.pop(k, None)
            cfg = Path(d) / 'cta_config.json'
            self.assertIsInstance(cta_sms.load_sms_provider(cfg), cta_sms.OnScreenCodes)       # no file
            cfg.write_text(json.dumps({'sms': {'provider': 'twilio_verify', 'account_sid': 'AC1'}}))
            self.assertIsInstance(cta_sms.load_sms_provider(cfg), cta_sms.OnScreenCodes)       # incomplete
            cfg.write_text(json.dumps({'sms': {'provider': 'twilio_verify', 'account_sid': 'AC1',
                                               'auth_token': 't', 'service_sid': 'VA1'}}))
            self.assertIsInstance(cta_sms.load_sms_provider(cfg), cta_sms.TwilioVerify)
            cfg.write_text('{broken')
            import cta_storage
            with self.assertRaises(cta_storage.DataCorrupted):           # never silently show codes on screen
                cta_sms.load_sms_provider(cfg)
            with self.assertRaises(cta_storage.DataCorrupted):           # and never overwrite the damaged file
                cta_sms.write_sms_config(cfg, 'AC', 't', 'VA')
            self.assertEqual(cfg.read_text(), '{broken')
            cfg.write_text('{}')
            os.environ.update(TWILIO_ACCOUNT_SID='ACenv', TWILIO_AUTH_TOKEN='t', TWILIO_VERIFY_SERVICE_SID='VAenv')
            self.assertEqual(cta_sms.load_sms_provider(cfg).account_sid, 'ACenv')


class PaymentTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 14, 2, 10)

    def test_every_requested_method_exists(self):
        ids = [m[0] for m in pay.methods()]
        self.assertEqual(ids, ['card', 'mada', 'stcpay', 'paypal', 'tabby', 'tamara', 'cash'])
        self.assertEqual(pay.METHOD_KIND['cash'], 'counter')
        self.assertTrue(all(pay.METHOD_KIND[m] == 'online' for m in ids[:-1]))

    def test_online_methods_are_recorded_as_paid_with_the_gateway_name(self):
        for m in ('card', 'mada', 'stcpay', 'paypal', 'tabby', 'tamara'):
            rec = pay.pay(m, 12630, 'CTA-1', '+447123456789', self.NOW)
            self.assertEqual((rec['status'], rec['method'], rec['amount_cents']), ('paid', m, 12630))
            self.assertTrue(rec['transaction_id'].startswith('TX-'))
            self.assertEqual(rec['gateway'], 'simulated')
            self.assertEqual(rec['paid_at'], '2026-10-24 14:02:10')

    def test_cash_is_reserved_until_staff_mark_it_paid(self):
        rec = pay.pay('cash', 12630, 'CTA-1', '+447123456789', self.NOW)
        self.assertEqual((rec['status'], rec['gateway']), ('pending_counter', 'counter'))
        self.assertNotIn('paid_at', rec)

    def test_bad_requests_and_failing_provider(self):
        with self.assertRaises(ValueError): pay.pay('bitcoin', 100, 'x', 'x', self.NOW)
        with self.assertRaises(ValueError): pay.pay('card', 0, 'x', 'x', self.NOW)
        class Declined:
            name = 'testpsp'
            def charge(self, *a): return pay.PaymentResult(pay.FAILED, '', 'Card declined')
        rec = pay.pay('card', 100, 'x', 'x', self.NOW, provider=Declined())
        self.assertEqual((rec['status'], rec['gateway'], rec['message']), ('failed', 'testpsp', 'Card declined'))
        self.assertNotIn('paid_at', rec)



class FakePSP(pay.PaymentProvider):
    """A stand-in for a real provider: what charge() claims and what verify() reports are set separately."""
    name, live = 'fakepsp', True

    def __init__(self, claim=pay.PAID, verified=None, tx='PSP-1', verify_error=None):
        self.claim, self.verified, self.tx, self.verify_error = claim, verified, tx, verify_error
        self.charges, self.verifies = [], []

    def charge(self, method, amount_cents, reference, phone, currency='SAR', idempotency_key=''):
        self.charges.append((method, amount_cents, reference, currency, idempotency_key))
        return pay.PaymentResult(self.claim, self.tx, 'provider says ' + self.claim)

    def verify(self, transaction_id):
        self.verifies.append(transaction_id)
        if self.verify_error:
            raise self.verify_error
        return self.verified


def good(amount, ref, status=pay.PAID, tx='PSP-1', currency='SAR'):
    return pay.VerifiedPayment(status, tx, amount, currency, ref)


class PaymentEnvironmentTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 9, 0, 0)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_environment_comes_from_env_var_then_config_and_defaults_to_production(self):
        cfg = self.tmp / 'cfg.json'
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CTA_ENV', None)
            self.assertEqual(pay.current_environment(config_path=cfg), 'production')            # no file at all
            cfg.write_text(json.dumps({'environment': 'development'}))
            self.assertEqual(pay.current_environment(config_path=cfg), 'development')
            cfg.write_text(json.dumps({'environment': 'staging-ish'}))
            self.assertEqual(pay.current_environment(config_path=cfg), 'production')            # unknown -> fail closed
            os.environ['CTA_ENV'] = 'Testing'
            self.assertEqual(pay.current_environment(config_path=cfg), 'testing')                # env var wins

    def test_simulated_payments_work_in_development_and_testing(self):
        for env in ('development', 'testing'):
            rec = pay.pay('card', 4210, 'CTA-1', '+447123456789', self.NOW, pay.SimulatedGateway(), environment=env)
            self.assertEqual(rec['status'], 'paid')
            self.assertTrue(rec['simulated']); self.assertEqual(rec['environment'], env)
            self.assertIn('verified_at', rec)
            self.assertIn('test mode', pay.status_text(rec))
            self.assertTrue(pay.is_settled(rec, env))
            self.assertFalse(pay.is_settled(rec, 'production'))

    def test_simulated_gateway_is_refused_in_production(self):
        with self.assertRaises(pay.PaymentUnavailable) as cm:
            pay.pay('card', 4210, 'CTA-1', '+447123456789', self.NOW, pay.SimulatedGateway(), environment='production')
        self.assertIn('not available', str(cm.exception))
        self.assertIsNone(pay.load_payment_provider(config={'payments': {'provider': 'simulated'}}, environment='production'))

    def test_missing_provider_switches_online_payment_off_but_cash_still_works(self):
        provider = pay.load_payment_provider(config={}, environment='production')
        self.assertIsNone(provider)
        ok, msg = pay.online_availability(provider, 'production')
        self.assertFalse(ok); self.assertIn('Live online payments are not available', msg)
        with self.assertRaises(pay.PaymentUnavailable):
            pay.pay('mada', 4210, 'CTA-1', 'x', self.NOW, environment='production',
                    provider=pay.load_payment_provider(config={}, environment='production'))
        unknown = pay.load_payment_provider(config={'payments': {'provider': 'moyasar'}}, environment='production')
        self.assertIsNone(unknown)                       # a named but unimplemented provider is not invented
        cash = pay.pay('cash', 4210, 'CTA-1', 'x', self.NOW, environment='production')
        self.assertEqual(cash['status'], 'pending_counter')

    def test_simulated_ticket_cannot_board_in_production(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist, method='card')
        with mock.patch.dict(os.environ, {'CTA_ENV': 'production'}):
            ok, msg, _ = core.validate_ticket(hist, r['passengers'][0]['ticket_no'], self.NOW, 'admin')
            self.assertFalse(ok); self.assertIn('TEST PAYMENT', msg)
            self.assertEqual(stats.summarize(hist)['collected_cents'], 0)
        old_style = {'status': 'paid', 'gateway': 'simulated'}         # stored before the 'simulated' flag existed
        self.assertFalse(pay.is_settled(old_style, 'production'))


class PaymentVerificationTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 9, 0, 0)

    def charge(self, psp, amount=4210, ref='CTA-1', ledger=None, env='production'):
        return pay.pay('card', amount, ref, '+447123456789', self.NOW, psp, environment=env, ledger=ledger)

    def test_paid_only_after_the_provider_record_matches(self):
        psp = FakePSP(verified=good(4210, 'CTA-1'))
        rec = self.charge(psp)
        self.assertEqual(rec['status'], 'paid')
        self.assertEqual(psp.verifies, ['PSP-1'])                       # it asked the provider, not just believed it
        self.assertEqual(psp.charges[0][4], 'CTA-1')                    # idempotency key = booking reference
        self.assertFalse(rec['simulated']); self.assertEqual(rec['currency'], 'SAR')
        self.assertEqual([e['status'] for e in rec['events']], ['started', 'paid'])
        self.assertTrue(pay.is_settled(rec, 'production'))

    def test_a_success_claim_without_verification_is_not_paid(self):
        class NoVerify:
            name, live = 'noverify', True
            def charge(self, *a, **k): return pay.PaymentResult(pay.PAID, 'X1', 'Approved!')
        self.assertEqual(self.charge(NoVerify())['status'], 'unknown')
        self.assertEqual(self.charge(FakePSP(verify_error=OSError('timeout')))['status'], 'unknown')
        self.assertEqual(self.charge(FakePSP(tx=''))['status'], 'unknown')
        self.assertEqual(self.charge(FakePSP(verified=good(4210, 'CTA-1', status='pending')))['status'], 'pending')
        self.assertEqual(self.charge(FakePSP(verified='{"status":"paid"}'))['status'], 'unknown')
        for rec in (self.charge(NoVerify()),):
            self.assertNotIn('paid_at', rec)

    def test_wrong_amount_currency_reference_or_transaction_is_rejected(self):
        cases = {'amount': good(1, 'CTA-1'), 'currency': good(4210, 'CTA-1', currency='USD'),
                 'booking': good(4210, 'CTA-OTHER'), 'transaction': good(4210, 'CTA-1', tx='PSP-999'),
                 'bool amount': pay.VerifiedPayment('paid', 'PSP-1', True, 'SAR', 'CTA-1')}
        for why, verified in cases.items():
            rec = self.charge(FakePSP(verified=verified))
            self.assertEqual(rec['status'], 'failed', why)
            self.assertTrue(rec.get('review_required'), why)
            self.assertNotIn('paid_at', rec, why)

    def test_failed_cancelled_and_pending_payments_give_no_valid_ticket(self):
        for claim in ('failed', 'cancelled', 'pending'):
            rec = self.charge(FakePSP(claim=claim))
            self.assertEqual(rec['status'], claim)
            self.assertNotIn('paid_at', rec)
            self.assertFalse(pay.is_settled(rec, 'production'))
        self.assertEqual(self.charge(FakePSP(claim='weird'))['status'], 'unknown')
        class Boom(FakePSP):
            def charge(self, *a, **k): raise ConnectionError('down')
        self.assertEqual(self.charge(Boom())['status'], 'unknown')
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist)
        r['payment'] = self.charge(FakePSP(claim='pending'), amount=r['total_cents'], ref=r['reference'])
        ok, msg, _ = core.validate_ticket(hist, r['passengers'][0]['ticket_no'], self.NOW, 'admin')
        self.assertFalse(ok); self.assertIn('NOT CONFIRMED', msg)
        self.assertFalse(core.mark_paid(r, self.NOW, 'admin')[0])          # staff cannot hand-mark an online payment

    def test_duplicate_callbacks_confirm_once_and_a_transaction_pays_one_booking_only(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = pay.PaymentLedger(tmp / 'payments_ledger.json')
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist)
        psp = FakePSP(claim='pending')
        r['payment'] = self.charge(psp, amount=r['total_cents'], ref=r['reference'], ledger=ledger)
        psp.verified = good(r['total_cents'], r['reference'])
        spoof = {'reference': r['reference'], 'transaction_id': 'PSP-1', 'status': 'paid', 'amount': 1}
        ok, msg, rec = pay.handle_callback(hist, spoof, psp, self.NOW, ledger, environment='production')
        self.assertTrue(ok, msg); self.assertEqual(rec['payment']['status'], 'paid')
        events = len(rec['payment']['events'])
        for _ in range(3):                                                 # the provider retries its notification
            ok, msg, _ = pay.handle_callback(hist, spoof, psp, self.NOW, ledger, environment='production')
            self.assertFalse(ok); self.assertIn('Already paid', msg)
        self.assertEqual(len(rec['payment']['events']), events)
        self.assertEqual(len(hist), 1)
        self.assertEqual(len(json.loads((tmp / 'payments_ledger.json').read_text())), 1)
        # the same provider transaction presented for a different booking is refused (fresh ledger from disk)
        other = booking('Zord', 'Centrala', {'Adult': 1}, ['John Roe'], self.NOW, hist)
        psp2 = FakePSP(verified=good(other['total_cents'], other['reference']))
        again = pay.pay('card', other['total_cents'], other['reference'], 'x', self.NOW, psp2,
                        environment='production', ledger=pay.PaymentLedger(tmp / 'payments_ledger.json'))
        self.assertEqual(again['status'], 'failed'); self.assertIn('another booking', again['message'])
        log = (tmp / 'payment_events.jsonl').read_text()
        self.assertIn(r['reference'], log)

    def test_callback_payload_status_is_never_trusted(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist)
        psp = FakePSP(claim='pending')
        r['payment'] = self.charge(psp, amount=r['total_cents'], ref=r['reference'])
        psp.verified = good(r['total_cents'], r['reference'], status='pending')
        ok, _, _ = pay.handle_callback(hist, {'reference': r['reference'], 'transaction_id': 'PSP-1', 'status': 'paid'},
                                       psp, self.NOW, environment='production')
        self.assertFalse(ok); self.assertEqual(r['payment']['status'], 'pending')
        ok, msg, _ = pay.handle_callback(hist, {'reference': r['reference'], 'transaction_id': 'PSP-FORGED'},
                                         psp, self.NOW, environment='production')
        self.assertFalse(ok); self.assertIn('does not match', msg)
        self.assertFalse(pay.handle_callback(hist, {'reference': 'CTA-NOPE'}, psp, self.NOW)[0])
        sim = pay.SimulatedGateway(); sim.name = 'fakepsp'
        self.assertFalse(pay.handle_callback(hist, {'reference': r['reference'], 'transaction_id': 'PSP-1'}, sim,
                                             self.NOW, environment='production')[0])

    def test_simulated_gateway_is_idempotent(self):
        sim = pay.SimulatedGateway()
        a = sim.charge('card', 100, 'CTA-1', 'x', idempotency_key='CTA-1')
        b = sim.charge('card', 100, 'CTA-1', 'x', idempotency_key='CTA-1')
        self.assertEqual(a.transaction_id, b.transaction_id)

    def test_records_and_logs_hold_no_card_data_or_secrets(self):
        tmp = Path(tempfile.mkdtemp())
        ledger = pay.PaymentLedger(tmp / 'payments_ledger.json')
        rec = self.charge(FakePSP(verified=good(4210, 'CTA-1')), ledger=ledger)
        ledger.log({'at': 'now', 'reference': 'CTA-1', 'cvv': '123', 'card_number': '4111111111111111',
                    'api_key': 'sk_live_x'})
        text = json.dumps(rec) + (tmp / 'payment_events.jsonl').read_text() + (tmp / 'payments_ledger.json').read_text()
        for bad in ('cvv', 'card_number', '4111', 'api_key', 'sk_live'):
            self.assertNotIn(bad, text)


class CashPaymentTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 9, 0, 0)

    def test_cash_needs_a_signed_in_staff_member(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist, method='cash')
        ok, msg = core.mark_paid(r, self.NOW, '')
        self.assertFalse(ok); self.assertIn('staff', msg)
        self.assertEqual(r['payment']['status'], 'pending_counter')
        ok, _ = core.mark_paid(r, self.NOW, 'admin')
        self.assertTrue(ok)
        self.assertEqual(r['payment']['events'][-1], {'at': '2026-10-25 09:00:00', 'status': 'paid',
                                                       'source': 'counter', 'by': 'admin'})
        with mock.patch.dict(os.environ, {'CTA_ENV': 'production'}):
            self.assertTrue(core.validate_ticket(hist, r['passengers'][0]['ticket_no'], self.NOW, 'admin')[0])

    def test_pdf_never_presents_a_test_or_unconfirmed_payment_as_genuine(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('pypdf missing')
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, [], method='card')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 't.pdf'
            core.export_ticket_pdf(r, p)
            self.assertIn('TEST MODE', PdfReader(str(p)).pages[0].extract_text())
            r['payment'] = dict(r['payment'], status='pending', simulated=False, gateway='fakepsp')
            core.export_ticket_pdf(r, p)
            self.assertIn('NOT CONFIRMED', PdfReader(str(p)).pages[0].extract_text())

def booking(route_from, route_to, counts, names, when, history, legs=1, method='card', phone='+447123456789'):
    """Helper: a complete booking record like the app makes."""
    route = rt.astar(route_from, route_to)
    zones = core.chargeable_zones(route.origin, route.destination, route)
    rec = core.make_record(route, counts, zones, when, when, history, names=names, account_phone=phone, legs=legs)
    rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], phone, when)
    history.append(rec)
    return rec


class ReturnTicketTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 14, 2, 10)

    def test_return_costs_two_journeys_and_gives_two_tickets_each(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 2, 'Child': 1}, ['Jane Doe', 'John Roe', 'Kid Roe'], self.NOW, hist, legs=2)
        self.assertEqual(r['trip_type'], 'return')
        self.assertEqual(r['total_cents'], (2 * 2105 + 1410) * 3 * 2)
        p = r['passengers'][2]
        self.assertEqual((p['leg_fare_cents'], p['fare_cents']), (1410 * 3, 1410 * 3 * 2))
        self.assertEqual(p['return_ticket_no'], r['reference'] + '-P3R')
        self.assertEqual(sum(x['fare_cents'] for x in r['passengers']), r['total_cents'])
        self.assertEqual(core.trip_total({'Adult': 1}, 3, 2), 2 * core.trip_total({'Adult': 1}, 3, 1))

    def test_legs_must_be_one_or_two(self):
        route = rt.astar('Centrala', 'Zord')
        for legs in (0, 3):
            with self.assertRaises(ValueError):
                core.make_record(route, {'Adult': 1}, 3, self.NOW, self.NOW, [], names=['Jane Doe'], legs=legs)

    def test_summary_mentions_two_journeys(self):
        self.assertIn('× 2 journeys', core.booking_summary({'Adult': 1}, 3, 2))
        self.assertNotIn('journeys', core.booking_summary({'Adult': 1}, 3, 1))

    def test_each_journey_has_its_own_qr_token(self):
        import cta_tickets
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, [], legs=2)
        p = r['passengers'][0]
        out, back = core.ticket_qr_payload(r, p), core.ticket_qr_payload(r, p, 'return')
        self.assertNotEqual(out, back)
        signer = cta_tickets.default_signer()
        self.assertEqual((signer.verify(out), signer.verify(back)), (p['ticket_id'], p['return_ticket_id']))
        self.assertNotEqual(p['ticket_id'], p['return_ticket_id'])

    def test_return_pdf_has_a_page_per_journey_and_no_demo_wording(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('pypdf missing')
        r = booking('Centrala', 'Zord', {'Adult': 2}, ['Jane Doe', 'John Roe'], self.NOW, [], legs=2, method='cash')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'r.pdf'
            core.export_ticket_pdf(r, p)
            pages = [pg.extract_text() or '' for pg in PdfReader(str(p)).pages]
            only = len(PdfReader(str(p)).pages)
        self.assertEqual(only, 5)                                   # 2 passengers x 2 journeys + receipt
        self.assertIn(r['passengers'][0]['return_ticket_no'], pages[1])
        self.assertIn('RETURN BOARDING PASS', pages[1])
        self.assertIn('PAY AT THE COUNTER', pages[0])               # cash booking carries the amount due
        self.assertIn(f"{r['total_cents']:,} cents due", pages[0])
        alltext = ' '.join(pages).lower()
        for banned in ('demo', 'simulated', 'not valid for travel', 'no payment was'):
            self.assertNotIn(banned, alltext)

    def test_paid_booking_shows_paid_banner(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('pypdf missing')
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, [], method='paypal')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'p.pdf'
            core.export_ticket_pdf(r, p)
            first = PdfReader(str(p)).pages[0].extract_text()
        self.assertIn('PAID', first); self.assertIn('PayPal', first); self.assertNotIn('PAY AT THE COUNTER', first)


class LifecycleTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 14, 2, 10)

    def test_unpaid_counter_ticket_cannot_board_until_marked_paid(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist, method='cash')
        tno = r['passengers'][0]['ticket_no']
        ok, msg, _ = core.validate_ticket(hist, tno, self.NOW, 'admin')
        self.assertFalse(ok); self.assertIn('NOT PAID', msg)
        ok, msg = core.mark_paid(r, self.NOW, 'admin')
        self.assertTrue(ok); self.assertEqual(r['payment']['status'], 'paid'); self.assertEqual(r['payment']['received_by'], 'admin')
        self.assertFalse(core.mark_paid(r, self.NOW, 'admin')[0])            # cannot be paid twice
        ok, msg, _ = core.validate_ticket(hist, tno.lower(), self.NOW + timedelta(minutes=5), 'admin')
        self.assertTrue(ok, msg)
        ok, msg, _ = core.validate_ticket(hist, tno, self.NOW + timedelta(minutes=6), 'admin')
        self.assertFalse(ok); self.assertIn('ALREADY USED', msg)

    def test_outbound_and_return_are_separate_tickets(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist, legs=2)
        p = r['passengers'][0]
        self.assertTrue(core.validate_ticket(hist, p['ticket_no'], self.NOW, 'a')[0])
        self.assertEqual((p['status'], p['return_status']), ('boarded', 'issued'))
        self.assertTrue(core.validate_ticket(hist, p['return_ticket_no'], self.NOW, 'a')[0])
        self.assertFalse(core.validate_ticket(hist, p['return_ticket_no'], self.NOW, 'a')[0])

    def test_expiry_and_unknown_numbers(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Jane Doe'], self.NOW, hist)
        tno = r['passengers'][0]['ticket_no']
        self.assertEqual(core.ticket_valid_until(r), datetime(2026, 10, 25, 5, 0))
        ok, msg, _ = core.validate_ticket(hist, tno, datetime(2026, 10, 25, 5, 1), 'a')
        self.assertFalse(ok); self.assertIn('expired', msg)
        self.assertTrue(core.validate_ticket(hist, tno, datetime(2026, 10, 25, 4, 59), 'a')[0])
        self.assertFalse(core.validate_ticket(hist, 'CTA-NOPE', self.NOW, 'a')[0])
        self.assertFalse(core.validate_ticket(hist, '', self.NOW, 'a')[0])

    def test_old_tickets_still_open_and_count_as_paid(self):
        old = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, self.NOW, self.NOW, [])
        self.assertEqual(core.payment_of(old)['status'], 'paid')
        self.assertEqual(core.payment_of(old)['method'], 'legacy')
        ok, msg, _ = core.validate_ticket([old], old['reference'], self.NOW, 'a')
        self.assertFalse(ok)                                          # no per-passenger status to update


class StatsTests(unittest.TestCase):
    NOW = datetime(2026, 10, 24, 15, 0)          # a Saturday; the week started Monday 19 Oct

    def setUp(self):
        self.hist = []
        d = lambda *a: datetime(*a)
        self.today = booking('Centrala', 'Zord', {'Adult': 2}, ['Ann Lee', 'Bob Lee'], d(2026, 10, 24, 10, 0), self.hist, method='card')
        self.week = booking('Zord', 'Centrala', {'Child': 1}, ['Cy Lee'], d(2026, 10, 20, 9, 30), self.hist, method='cash', phone='+447000000001')
        self.month = booking('Centrala', 'Zord', {'Student': 1}, ['Di Lee'], d(2026, 10, 5, 8, 0), self.hist, legs=2, method='paypal')
        self.year = booking('Centrala', 'Zord', {'Adult': 1}, ['Ed Lee'], d(2026, 3, 1, 8, 0), self.hist)
        self.old = booking('Centrala', 'Zord', {'Adult': 1}, ['Flo Lee'], d(2025, 12, 31, 8, 0), self.hist)

    def refs(self, period):
        return {r['reference'] for r in stats.filter_period(self.hist, period, self.NOW)}

    def test_period_filters(self):
        ref = lambda *rs: {r['reference'] for r in rs}
        self.assertEqual(self.refs('Today'), ref(self.today))
        self.assertEqual(self.refs('This week'), ref(self.today, self.week))
        self.assertEqual(self.refs('This month'), ref(self.today, self.week, self.month))
        self.assertEqual(self.refs('This year'), ref(self.today, self.week, self.month, self.year))
        self.assertEqual(self.refs('All time'), ref(self.today, self.week, self.month, self.year, self.old))

    def test_summary_counts_people_tickets_and_money(self):
        s = stats.summarize(stats.filter_period(self.hist, 'This month', self.NOW))
        self.assertEqual((s['bookings'], s['passengers'], s['tickets']), (3, 4, 5))       # the return booking has 2 tickets
        self.assertEqual(s['return_bookings'], 1)
        total = self.today['total_cents'] + self.week['total_cents'] + self.month['total_cents']
        self.assertEqual(s['revenue_cents'], total)
        self.assertEqual(s['to_collect_cents'], self.week['total_cents'])                 # the cash booking
        self.assertEqual(s['collected_cents'], total - self.week['total_cents'])
        self.assertEqual(s['by_category'], {'Adult': 2, 'Child': 1, 'Student': 1})

    def test_zone_to_zone_matrix_and_crossings(self):
        recs = stats.filter_period(self.hist, 'This month', self.NOW)
        m = stats.zone_matrix(recs)
        self.assertEqual(m[('Central', 'Downtown')], 2 + 1)          # today x2 passengers + month outbound leg
        self.assertEqual(m[('Downtown', 'Central')], 1 + 1)          # week booking + month return leg
        cross = stats.zone_crossings(recs)
        self.assertEqual(cross[('Central', 'Midtown')], 3)
        self.assertEqual(cross[('Midtown', 'Downtown')], 3)
        self.assertEqual(cross[('Downtown', 'Midtown')], 2)
        self.assertEqual(cross[('Midtown', 'Central')], 2)

    def test_top_routes_and_daily_counts(self):
        recs = stats.filter_period(self.hist, 'All time', self.NOW)
        self.assertEqual(stats.top_routes(recs, 1), [('Centrala', 'Zord', 5)])
        days = stats.daily_counts(self.hist, 7, self.NOW)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1], (datetime(2026, 10, 24).date(), 2))
        self.assertEqual(dict(days)[datetime(2026, 10, 20).date()], 1)
        self.assertEqual(sum(stats.hourly_counts(recs)), 6)

    def test_no_shows_are_unboarded_tickets_after_departure(self):
        rows = stats.no_shows(self.hist, self.NOW)
        self.assertEqual(len(rows), 7)       # every ticket except none boarded yet; today's 10:00 trains already left
        self.assertTrue(all(r['phone'] for r in rows))
        core.mark_paid(self.week, self.NOW, 'a')
        at_gate = datetime(2026, 10, 20, 9, 40)                       # ten minutes after that train left
        self.assertTrue(core.validate_ticket(self.hist, self.week['passengers'][0]['ticket_no'], at_gate, 'a')[0])
        rows = stats.no_shows(self.hist, self.NOW)
        self.assertEqual(len(rows), 6)
        future = booking('Centrala', 'Zord', {'Adult': 1}, ['Gus Lee'], self.NOW, self.hist)
        self.assertNotIn(future['passengers'][0]['ticket_no'], [r['ticket_no'] for r in stats.no_shows(self.hist, self.NOW - timedelta(hours=1))])

    def test_csv_export_has_one_row_per_ticket_and_keeps_names_intact(self):
        import csv, io
        h = []
        booking('Centrala', 'Zord', {'Adult': 1}, ["Zoë O'Brien Jr."], self.NOW, h, legs=2)
        text = stats.rows_to_csv(stats.ticket_rows(h))
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['passenger'] for r in rows}, {"Zoë O'Brien Jr."})
        self.assertEqual({r['leg'] for r in rows}, {'out', 'return'})
        self.assertIn('payment_status', rows[0])


class LogoTests(unittest.TestCase):
    def test_every_payment_method_has_a_picture(self):
        from PIL import Image
        for mid, *_ in pay.methods():
            path = Path(__file__).resolve().parent.parent / 'assets' / 'logos' / f'{mid}.png'
            self.assertTrue(path.is_file(), mid)
            with Image.open(path) as im:
                self.assertEqual(im.size, (300, 100), mid)



def legacy_install(folder, when=datetime(2026, 10, 24, 9, 0)):
    """A version-2.3 data folder: JSON files only, with paid, cash, boarded, return and pre-2.1 bookings."""
    folder = Path(folder)
    hist = []
    booking('Centrala', 'Zord', {'Adult': 2}, ['Ann Lee', 'Bob Lee'], when, hist, method='card')
    booking('Zord', 'Centrala', {'Child': 1}, ['Cy Lee'], when, hist, legs=2, method='cash', phone='+447000000001')
    hist[0]['passengers'][0]['status'] = 'boarded'
    hist[0]['passengers'][0]['boarded_at'] = '2026-10-24 09:10:00'
    hist[0]['passengers'][0]['boarded_by'] = 'admin'
    hist.append(core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, when, when, hist))   # v2.0 style
    core.save_history(folder / 'history.json', hist)
    accounts = []
    acct.create_account(accounts, '+447123456789', 'Ann Lee', when)
    acct.create_account(accounts, '+447000000001', 'Cy Lee', when)
    acct.save_accounts(folder / 'accounts.json', accounts)
    staff = []
    acct.create_staff(staff, 'admin', TEST_PASSWORD, when)
    acct.save_staff(folder / 'staff.json', staff)
    return hist, accounts, staff


class JsonFileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = self.dir / 'history.json'

    def test_missing_file_is_a_first_run(self):
        self.assertEqual(core.load_history(self.p), [])
        self.assertEqual(acct.load_accounts(self.dir / 'accounts.json'), [])
        self.assertEqual(cta_storage.load_config(self.dir / 'cta_config.json'), {})

    def test_malformed_files_raise_a_clear_error_and_are_kept(self):
        for content in (b'{broken', b'', b'   \n', b'\xff\xfe\x00garbage', b'[{"reference": "CTA-1",'):
            self.p.write_bytes(content)
            with self.assertRaises(cta_storage.DataCorrupted) as cm:
                core.load_history(self.p)
            msg = str(cm.exception)
            self.assertIn('history.json', msg); self.assertIn('Nothing has been changed', msg)
            self.assertNotIn(str(self.dir), msg)                       # no full paths (they contain user names)
            with self.assertRaises(cta_storage.StorageError):
                core.save_history(self.p, [])
            self.assertEqual(self.p.read_bytes(), content)

    def test_invalid_structures_are_rejected(self):
        hist = []
        r = booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], datetime(2026, 10, 24, 9, 0), hist, legs=2)
        good = json.loads(json.dumps(r))
        def variant(change):
            rec = json.loads(json.dumps(good)); change(rec); return [rec]
        bad = {
            'not a list': {'reference': 'x'},
            'not a record': ['CTA-1'],
            'no reference': variant(lambda r: r.pop('reference')),
            'bad issue time': variant(lambda r: r.update(issued_at='yesterday')),
            'total is text': variant(lambda r: r.update(total_cents='100')),
            'total is bool': variant(lambda r: r.update(total_cents=True)),
            'bad legs': variant(lambda r: r.update(legs=3)),
            'empty passengers': variant(lambda r: r.update(passengers=[])),
            'ticket status': variant(lambda r: r['passengers'][0].update(status='used')),
            'return status': variant(lambda r: r['passengers'][0].update(return_status='gone')),
            'payment status': variant(lambda r: r['payment'].update(status='approved')),
            'payment amount': variant(lambda r: r['payment'].update(amount_cents=-1)),
            'duplicate ticket': variant(lambda r: r['passengers'][0].update(return_ticket_no=r['passengers'][0]['ticket_no'])),
            'duplicate booking': [good, json.loads(json.dumps(good))],
        }
        for why, data in bad.items():
            with self.assertRaises(cta_storage.DataInvalid, msg=why):
                core.validate_history(data)
            self.p.write_text(json.dumps(data))
            with self.assertRaises(cta_storage.DataInvalid, msg=why):
                core.load_history(self.p)
        core.validate_history([good])                                         # the real thing passes
        with self.assertRaises(cta_storage.DataInvalid):
            acct.validate_accounts([{'phone': '+44', 'name': 'A'}, {'phone': '+44', 'name': 'B'}])
        with self.assertRaises(cta_storage.DataInvalid):
            acct.validate_staff([{'username': 'admin', 'role': 'admin'}])      # no password record
        p = self.dir / 'new.json'
        with self.assertRaises(cta_storage.DataInvalid):
            core.save_history(p, [{'reference': 'x'}])                         # invalid data is never written
        self.assertFalse(p.exists())

    def test_read_permission_failure_is_not_an_empty_file(self):
        self.p.write_text('[]')
        with mock.patch.object(Path, 'read_bytes', side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(cta_storage.DataUnreadable) as cm:
                core.load_history(self.p)
        self.assertIn('permission denied', str(cm.exception))
        with mock.patch.object(Path, 'read_bytes', side_effect=OSError(5, 'I/O error')):
            with self.assertRaises(cta_storage.DataUnreadable):
                core.load_history(self.p)

    def test_failed_saves_keep_the_original_file(self):
        hist = []
        booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], datetime(2026, 10, 24, 9, 0), hist)
        core.save_history(self.p, hist)
        before = self.p.read_bytes()
        bigger = hist + []
        booking('Zord', 'Centrala', {'Adult': 1}, ['Bo Lee'], datetime(2026, 10, 24, 9, 5), bigger)
        for target, err in (('os.replace', PermissionError(13, 'denied')), ('os.fsync', OSError(28, 'disk full'))):
            with mock.patch(target, side_effect=err):
                with self.assertRaises(cta_storage.DataWriteError) as cm:
                    core.save_history(self.p, bigger)
            self.assertIn('previous version was kept', str(cm.exception))
            self.assertEqual(self.p.read_bytes(), before)
            self.assertEqual([f.name for f in self.dir.iterdir() if f.name.endswith('.tmp')], [])   # no litter
        core.save_history(self.p, bigger)
        self.assertEqual(len(core.load_history(self.p)), 2)
        self.assertEqual(len(json.loads((self.dir / 'history.json.bak').read_text())), 1)   # previous good copy

    def test_json_backup_restore(self):
        hist = []
        booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], datetime(2026, 10, 24, 9, 0), hist)
        core.save_history(self.p, hist)
        copy = cta_storage.backup_file(self.p, cta_storage.backup_dir(self.dir), 'manual')
        self.p.write_text('{damaged')
        kept = cta_storage.restore_json_backup(copy, self.p, core.validate_history, 'bookings')
        self.assertEqual(core.load_history(self.p)[0]['reference'], hist[0]['reference'])
        self.assertEqual(kept.read_text(), '{damaged')                         # the damaged file was kept, not lost
        bad = self.dir / 'bad.json'; bad.write_text('nope')
        with self.assertRaises(cta_storage.DataCorrupted):
            cta_storage.restore_json_backup(bad, self.p, core.validate_history)
        self.assertEqual(len(core.load_history(self.p)), 1)

    def test_payment_ledger_damage_does_not_allow_double_payment(self):
        ledger_file = self.dir / 'payments_ledger.json'
        ledger_file.write_text('{oops')
        with self.assertRaises(cta_storage.DataCorrupted):
            pay.PaymentLedger(ledger_file)


class DatabaseTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 9, 0)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        for st in getattr(self, '_stores', []):
            st.close()

    def open(self, **kw):
        st = cta_db.Store.open(self.dir, self.NOW, **kw)
        self._stores = getattr(self, '_stores', []) + [st]
        return st

    def test_first_run_creates_an_empty_database(self):
        st = self.open()
        self.assertEqual(st.load_history(), [])
        self.assertTrue((self.dir / 'cta.db').is_file())
        self.assertEqual(st.verify(), [])

    def test_missing_database_with_backups_is_not_a_first_run(self):
        st = self.open()
        st.backup('auto'); st.close()
        (self.dir / 'cta.db').unlink()
        with self.assertRaises(cta_storage.DataMissing) as cm:
            cta_db.Store.open(self.dir, self.NOW)
        self.assertIn('backups', str(cm.exception))
        self.assertFalse((self.dir / 'cta.db').exists())                      # no empty database was created

    def test_damaged_or_foreign_database_is_reported(self):
        (self.dir / 'cta.db').write_bytes(b'this is not sqlite' * 100)
        with self.assertRaises(cta_storage.DataCorrupted):
            cta_db.Store.open(self.dir)
        (self.dir / 'cta.db').unlink()
        import sqlite3
        sqlite3.connect(str(self.dir / 'cta.db')).execute('CREATE TABLE x (y)').connection.close()
        with self.assertRaises(cta_storage.DataInvalid):
            cta_db.Store.open(self.dir)

    def test_read_only_database_can_be_read_but_not_written(self):
        hist, *_ = legacy_install(self.dir)
        self.open().close()
        with mock.patch('cta_db.os.access', return_value=False):
            st = self.open()
        self.assertTrue(st.read_only)
        loaded = st.load_history()
        self.assertEqual(len(loaded), 3)
        loaded[0]['passengers'][1]['status'] = 'boarded'
        with self.assertRaises(cta_storage.ReadOnlyStorage):
            st.save_history(loaded, actor=sign_in(st))

    def test_migration_preserves_counts_identifiers_states_and_originals(self):
        hist, accounts, staff = legacy_install(self.dir)
        before = {n: cta_storage.sha256(self.dir / n) for n in ('history.json', 'accounts.json', 'staff.json')}
        st = self.open()
        got = st.load_history()
        self.assertEqual([r['reference'] for r in got], [r['reference'] for r in hist])
        self.assertEqual([cta_db.canonical(r) for r in got], [cta_db.canonical(r) for r in hist])   # exact copy
        tickets = lambda rs: sorted(t for r in rs for p in core.passengers_of(r) if not p.get('legacy')
                                    for t in (p['ticket_no'], p.get('return_ticket_no')) if t)
        self.assertEqual(tickets(got), tickets(hist))
        self.assertEqual([core.payment_of(r)['status'] for r in got], ['paid', 'pending_counter', 'paid'])
        self.assertEqual(got[0]['passengers'][0]['status'], 'boarded')
        self.assertEqual(st.load_accounts(), accounts)
        self.assertEqual(st.load_staff(), staff)
        self.assertEqual(st.counts()['tickets'], 4)                           # 2 one-way + 1 passenger x 2 legs
        for n, digest in before.items():                                      # originals untouched...
            self.assertEqual(cta_storage.sha256(self.dir / n), digest)
        saved = list((self.dir / 'backups').glob('pre-migration-*/history-*-original.json'))
        self.assertEqual(cta_storage.sha256(saved[0]), before['history.json'])   # ...and backed up

    def test_repeated_migration_adds_nothing_and_never_overwrites(self):
        legacy_install(self.dir)
        st = self.open()
        counts = st.counts()
        report = cta_db.migrate_json(st, self.dir, self.NOW)
        self.assertTrue(report.verified)
        self.assertEqual(report.added, {'bookings': 0, 'accounts': 0, 'staff': 0, 'claims': 0})
        self.assertEqual(report.unchanged['bookings'], 3)
        after = st.counts()
        self.assertEqual({k: v for k, v in after.items() if k != 'audit'}, {k: v for k, v in counts.items() if k != 'audit'})
        # The database moves on (a check-in); the old JSON still has the earlier state: it is reported, not applied.
        h = st.load_history()
        h[1]['passengers'][0]['status'] = 'boarded'
        st.save_history(h, actor=sign_in(st))
        report = cta_db.migrate_json(st, self.dir, self.NOW)
        self.assertEqual(report.conflicts, [h[1]['reference']])
        self.assertEqual(st.load_history()[1]['passengers'][0]['status'], 'boarded')
        self.assertEqual(st.counts()['bookings'], 3)
        self.assertTrue(list((self.dir / 'backups').glob('cta-*-pre-migration.db')))   # DB backed up first

    def test_migration_of_payment_ledger_and_events_is_repeatable(self):
        legacy_install(self.dir)
        ledger = pay.PaymentLedger(self.dir / 'payments_ledger.json')
        ledger.claim('fakepsp', 'PSP-9', 'CTA-X', 100, 'SAR', '2026-10-24 09:00:00')
        ledger.log({'at': 'a', 'reference': 'CTA-X', 'status': 'paid'})
        with (self.dir / 'payment_events.jsonl').open('a') as fh:
            fh.write('{half a line')
        st = self.open()
        self.assertEqual(st.counts()['payment_claims'], 1)
        audit = st.counts()['audit']
        report = cta_db.migrate_json(st, self.dir, self.NOW)
        self.assertEqual(report.skipped_lines, 1)
        self.assertEqual(st.counts()['payment_claims'], 1)
        self.assertEqual(st.counts()['audit'], audit + 1)                     # only the new 'migration' entry
        self.assertEqual(cta_db.StoreLedger(st).claim('fakepsp', 'PSP-9', 'CTA-Y', 100, 'SAR', 'x'), 'conflict')

    def test_damaged_source_stops_migration_before_anything_is_created(self):
        legacy_install(self.dir)
        (self.dir / 'accounts.json').write_text('[{"phone": ')
        with self.assertRaises(cta_storage.DataCorrupted):
            cta_db.Store.open(self.dir, self.NOW)
        self.assertFalse((self.dir / 'cta.db').exists())
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == '.db'], [])

    def test_backup_and_restore_recover_the_records(self):
        hist, *_ = legacy_install(self.dir)
        st = self.open()
        good = st.backup('manual', now=self.NOW)
        h = st.load_history()
        h[2]['total_cents'] = 1                                              # a later bad edit...
        st.save_history(h, actor=sign_in(st))
        st.close()
        (self.dir / 'cta.db').write_bytes(b'\x00' * 4096)                    # ...then the file is destroyed
        with self.assertRaises(cta_storage.DataCorrupted):
            cta_db.Store.open(self.dir)
        kept = cta_db.restore_backup(good, self.dir, self.NOW)
        restored = self.open()
        self.assertEqual([cta_db.canonical(r) for r in restored.load_history()], [cta_db.canonical(r) for r in hist])
        self.assertEqual(kept.read_bytes(), b'\x00' * 4096)                  # the damaged file was kept
        (self.dir / 'junk.db').write_text('not a db')
        with self.assertRaises(cta_storage.StorageError):
            cta_db.restore_backup(self.dir / 'junk.db', self.dir)
        self.assertEqual(len(restored.load_history()), 3)

    def test_recovery_tool(self):
        legacy_install(self.dir)
        import cta_recover, io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cta_recover.main(['status', '--folder', str(self.dir)]), 1)   # no DB yet: nothing made
            self.assertFalse((self.dir / 'cta.db').exists())
            self.assertEqual(cta_recover.main(['migrate', '--folder', str(self.dir)]), 0)
            self.assertEqual(cta_recover.main(['migrate', '--folder', str(self.dir)]), 0)   # repeatable
            self.assertEqual(cta_recover.main(['backup', '--folder', str(self.dir)]), 0)
            self.assertEqual(cta_recover.main(['verify', '--folder', str(self.dir)]), 0)
            (self.dir / 'cta.db').write_bytes(b'garbage' * 500)
            self.assertEqual(cta_recover.main(['status', '--folder', str(self.dir)]), 1)
            self.assertEqual(cta_recover.main(['restore', '--folder', str(self.dir), '--yes']), 0)
            self.assertEqual(cta_recover.main(['status', '--folder', str(self.dir)]), 0)
        self.assertIn('damaged', out.getvalue())
        self.assertEqual(len(cta_db.Store.open(self.dir).load_history()), 3)

    def test_failed_save_writes_nothing(self):
        st = self.open()
        hist = []
        booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], self.NOW, hist)
        st.save_history(hist)
        hist2 = st.load_history()
        booking('Zord', 'Centrala', {'Adult': 1}, ['Bo Lee'], self.NOW, hist2)
        hist2[0]['passengers'][0]['status'] = 'boarded'
        with mock.patch.object(cta_db.Store, '_insert_rows', side_effect=[None, __import__('sqlite3').OperationalError('disk I/O error')]):
            with self.assertRaises(cta_storage.StorageError):
                st.save_history(hist2, actor=sign_in(st))
        fresh = self.open().load_history()
        self.assertEqual(len(fresh), 1)                                      # neither change was applied
        self.assertEqual(fresh[0]['passengers'][0]['status'], 'issued')


class ConcurrencyTests(unittest.TestCase):
    NOW = datetime(2026, 10, 25, 9, 0)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.a = cta_db.Store.open(self.dir, self.NOW)
        hist = []
        booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], self.NOW, hist, method='cash')
        self.a.save_history(hist)
        self.b = cta_db.Store.open(self.dir, self.NOW)                       # a second window / computer

    def tearDown(self):
        self.a.close(); self.b.close()

    def test_second_writer_of_the_same_booking_is_refused_not_lost(self):
        win_a, win_b = self.a.load_history(), self.b.load_history()
        tno = win_a[0]['passengers'][0]['ticket_no']
        self.assertTrue(core.mark_paid(win_a[0], self.NOW, 'alice')[0])
        self.a.save_history(win_a, actor=sign_in(self.a))
        win_b[0]['passengers'][0]['status'] = 'boarded'                       # B still has the old copy
        with self.assertRaises(cta_storage.ConcurrentUpdate):
            self.b.save_history(win_b, actor=sign_in(self.b))
        latest = self.b.load_history()
        self.assertEqual(latest[0]['payment']['received_by'], 'alice')        # A's change survived
        self.assertEqual(latest[0]['passengers'][0]['status'], 'issued')      # B's was not half-applied
        self.assertTrue(core.validate_ticket(latest, tno, self.NOW, 'bob')[0])
        self.b.save_history(latest, actor=sign_in(self.b))                                           # after reloading, B can save
        self.assertEqual(self.a.load_history()[0]['passengers'][0]['status'], 'boarded')

    def test_different_bookings_from_two_windows_are_both_kept(self):
        win_a, win_b = self.a.load_history(), self.b.load_history()
        booking('Zord', 'Centrala', {'Adult': 1}, ['Bo Lee'], self.NOW, win_a)
        booking('Centrala', 'Zord', {'Adult': 1}, ['Cy Lee'], self.NOW + timedelta(days=1), win_b)   # other reference
        self.a.save_history(win_a, actor=sign_in(self.a))
        self.b.save_history(win_b, actor=sign_in(self.b))
        self.assertEqual(len(self.a.load_history()), 3)
        self.assertTrue(self.a.has_changes() is False)

    def test_same_new_reference_from_two_windows_is_refused(self):
        win_a, win_b = self.a.load_history(), self.b.load_history()
        booking('Zord', 'Centrala', {'Adult': 1}, ['Bo Lee'], self.NOW, win_a)
        booking('Zord', 'Centrala', {'Adult': 1}, ['Di Lee'], self.NOW, win_b)
        self.assertEqual(win_a[-1]['reference'], win_b[-1]['reference'])      # same per-day sequence number
        self.a.save_history(win_a, actor=sign_in(self.a))
        with self.assertRaises(cta_storage.ConcurrentUpdate):
            self.b.save_history(win_b, actor=sign_in(self.b))
        self.assertEqual(self.b.load_history()[-1]['passengers'][0]['name'], 'Bo Lee')

    def test_change_detection_between_windows(self):
        self.b.load_history()
        self.assertFalse(self.b.has_changes())
        h = self.a.load_history(); h[0]['passengers'][0]['status'] = 'boarded'
        self.a.save_history(h, actor=sign_in(self.a))
        self.assertTrue(self.b.has_changes())

    def test_payment_claim_race(self):
        la, lb = cta_db.StoreLedger(self.a), cta_db.StoreLedger(self.b)
        self.assertEqual(la.claim('psp', 'T1', 'CTA-1', 100, 'SAR', 'now'), 'new')
        self.assertEqual(lb.claim('psp', 'T1', 'CTA-2', 100, 'SAR', 'now'), 'conflict')
        self.assertEqual(lb.claim('psp', 'T1', 'CTA-1', 100, 'SAR', 'now'), 'duplicate')


class GuiSmokeTests(unittest.TestCase):
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

    def make(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        return CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)

    def test_search_fare_and_booking(self):
        app = self.make()
        self.assertFalse(app.book_btn.enabled)
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        self.assertEqual(app.route.stations[-1], 'Zord')
        self.assertFalse(app.book_btn.enabled)           # no travellers yet
        app.steppers['Adult'].set(2)
        self.assertTrue(app.book_btn.enabled)
        self.assertIn('12,630', app.bar_total.cget('text'))
        app.trip.select('Return')                        # a return costs two single journeys
        self.assertIn('25,260', app.bar_total.cget('text'))
        app.trip.select('One way')
        self.assertIn('12,630', app.bar_total.cget('text'))
        app.pick_departure(2)
        self.assertEqual(app.dep_idx, 2)

        # 1) not signed in: Book opens the phone sign-in and nothing is issued
        opened = []
        real_open = app.open_auth
        app.open_auth = lambda on_success=None, intro='': opened.append(on_success)
        app.book()
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(app.history), 0)
        app.open_auth = real_open

        # 2) signed in: Book asks for the passenger names (one slot per traveller)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        shown = []
        app.show_confirmation = lambda rec, saved=None: shown.append((rec, saved))
        with mock.patch('cta_app.PassengerDialog') as names_dialog:
            app.book()
        _root, slots, first_name, on_names = names_dialog.call_args[0]
        self.assertEqual(slots, [(1, 'Adult'), (2, 'Adult')])
        self.assertEqual(first_name, 'Jane Doe')
        self.assertEqual(len(app.history), 0)

        # 3) names entered: the payment choice comes BEFORE any ticket exists
        with mock.patch('cta_app.PaymentDialog') as pay_dialog:
            on_names(['Jane Doe', 'John Roe'])
        _root, total, legs, on_pay = pay_dialog.call_args[0]
        self.assertEqual((total, legs), (12630, 1))
        self.assertEqual(len(app.history), 0)

        # 4) cash chosen: tickets are reserved, unpaid, saved, and a PDF copy is written
        on_pay('cash')
        self.assertEqual(len(app.history), 1)
        rec = app.history[0]
        self.assertEqual([p['name'] for p in rec['passengers']], ['Jane Doe', 'John Roe'])
        self.assertEqual(rec['payment']['status'], 'pending_counter')
        self.assertEqual(rec['account_phone'], '+447123456789')
        self.assertEqual(rec['from_station'], 'Centrala')
        self.assertTrue((Path(self.tmp) / f"{rec['reference']}.pdf").is_file())
        self.assertEqual(len(shown), 1)
        self.assertEqual(sum(app.counts.values()), 0)    # reset after booking
        other = cta_db.Store.open(Path(self.tmp))                           # what another window sees
        self.assertEqual(other.load_history()[0]['reference'], rec['reference'])
        other.close()

    def test_card_payment_and_return_booking(self):
        app = self.make()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.trip.select('Return')
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(['Jane Doe'], 'card')
        rec = app.history[0]
        self.assertEqual((rec['legs'], rec['total_cents']), (2, 2105 * 3 * 2))
        self.assertEqual(rec['payment']['status'], 'paid')
        self.assertIn('return_ticket_no', rec['passengers'][0])

    def test_admin_tab_is_staff_only_and_shows_live_numbers(self):
        app = self.make()
        app.show_page('admin')
        self.assertEqual(app.current_page, 'plan')       # a passenger cannot open it
        self.assertFalse(app._admin_shown)
        booking('Centrala', 'Zord', {'Adult': 2}, ['Ann Lee', 'Bob Lee'], app.clock(), app.history, method='cash')
        self.assertTrue(app.save_history())                # a just-made booking is saved without a staff sign-in
        app.staff = sign_in(app.store, 'admin')
        app._paint_account()
        self.assertTrue(app._admin_shown)
        app.show_page('admin')
        self.assertEqual(app.current_page, 'admin')
        app.admin.refresh()
        self.assertEqual(app.admin.kpi['bookings'].cget('text'), '1')
        self.assertEqual(app.admin.kpi['passengers'].cget('text'), '2')
        self.assertIn('12,630', app.admin.kpi['to_collect'].cget('text'))
        app.staff = None
        app._paint_account()
        self.assertEqual(app.current_page, 'plan')       # signing out closes the portal

    def test_admin_check_in_and_mark_paid(self):
        app = self.make()
        rec = booking('Centrala', 'Zord', {'Adult': 1}, ['Ann Lee'], app.clock(), app.history, method='cash')
        app.save_history()
        app.staff = sign_in(app.store, 'admin')
        tno = rec['passengers'][0]['ticket_no']
        app.admin._check_in(tno)
        self.assertIn('NOT PAID', app.admin.result.cget('text'))
        app.admin.tree.selection = lambda: (tno,)
        app.admin.mark_selected_paid()
        self.assertEqual(app.history[0]['payment']['status'], 'paid')
        app.admin._check_in(tno)
        self.assertIn('OK to travel', app.admin.result.cget('text'))
        saved = cta_db.Store.open(Path(self.tmp)).load_history()            # persisted for other windows
        self.assertEqual(saved[0]['passengers'][0]['status'], 'boarded')
        self.assertEqual(saved[0]['payment']['status'], 'paid')

    def test_staff_dialog_creates_admin_then_signs_in_with_lockout(self):
        from cta_dialogs import AuthDialog
        self.tmp = tempfile.mkdtemp()
        staff, got = [], []
        path = Path(self.tmp) / 'staff.json'
        clock = lambda: datetime(2026, 10, 24, 10, 0)
        mk = lambda: AuthDialog(self.root, [], Path(self.tmp) / 'a.json', clock, lambda a: None,
                                staff=staff, staff_path=path, on_staff=got.append)
        d = mk(); d._staff_step()
        d.user_var.set('admin'); d.pw_var.set('admin1234'); d.pw2_var.set('different1'); d._staff_next()
        self.assertIn('not the same', d.err.cget('text'))
        d.pw2_var.set('admin1234'); d._staff_next()
        self.assertEqual(got[0]['username'], 'admin')
        self.assertNotIn('admin1234', path.read_text())
        d2 = mk(); d2._staff_step()
        d2.user_var.set('admin'); d2.pw_var.set('wrong-pass1')
        for _ in range(5):
            d2._staff_next()
        self.assertIn('Too many', d2.err.cget('text'))
        d2.pw_var.set('admin1234'); d2._staff_next()
        self.assertEqual(len(got), 1)                       # still locked: even the right password waits

    def test_sign_in_with_real_sms_never_shows_the_code(self):
        from cta_dialogs import AuthDialog
        self.tmp = tempfile.mkdtemp()
        sent, got = [], []
        class Fake(cta_sms.TwilioVerify):
            def _post(self, endpoint, fields):
                sent.append(endpoint)
                return 200, {'status': 'approved'}
        d = AuthDialog(self.root, [], Path(self.tmp) / 'a.json', lambda: datetime(2026, 10, 24, 10, 0), got.append,
                       sms=Fake('AC', 't', 'VA'))
        d.phone_var.set('+44 7123 456789'); d._phone_next()
        d.name_var.set('Jane Doe'); d._name_next()
        self.assertEqual(sent, ['Verifications'])
        self.assertFalse(d.sms.shows_code)
        d.code_var.set('123456'); d._verify()
        self.assertEqual(got[0]['phone'], '+447123456789')
        self.assertEqual(sent, ['Verifications', 'VerificationCheck'])

    def test_payment_dialog_needs_a_choice_and_reports_it(self):
        from cta_dialogs import PaymentDialog
        chosen = []
        d = PaymentDialog(self.root, 12630, 1, chosen.append)
        self.assertFalse(d.confirm.enabled)
        d._select('cash')
        self.assertIn('pay at the counter', d.confirm.text)
        d._select('tabby')
        self.assertIn('Tabby', d.confirm.text)
        d._confirm()
        self.assertEqual(chosen, ['tabby'])

    def test_production_without_provider_offers_cash_only(self):
        from cta_app import CTAApp
        from cta_dialogs import PaymentDialog
        self.tmp = tempfile.mkdtemp()
        with mock.patch.object(pay, 'CONFIG_PATH', Path(self.tmp) / 'none.json'):
            app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp,
                         environment='production', provider=rt.SimulatedProvider())   # routing chosen explicitly
        self.assertIsNone(app.pay_provider)
        self.assertFalse(app.online_ok)
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        with mock.patch('cta_app.PaymentDialog') as dlg:
            app._names_done(['Jane Doe'])
        self.assertIn('not available', dlg.call_args.kwargs['online_unavailable'])
        with mock.patch('cta_app.messagebox') as box:
            app._issue(['Jane Doe'], 'card')                       # even if the GUI were bypassed
        self.assertEqual(app.history, [])
        self.assertEqual(box.showerror.call_args[0][0], 'Online payment unavailable')
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(['Jane Doe'], 'cash')
        self.assertEqual(app.history[0]['payment']['status'], 'pending_counter')
        d = PaymentDialog(self.root, 4210, 1, [].append, online_unavailable=app.online_msg)
        d._select('card')
        self.assertIsNone(d.selected); self.assertFalse(d.confirm.enabled)
        d._select('cash')
        self.assertEqual(d.selected, 'cash')
        d.win.destroy()

    def test_simulated_gateway_passed_in_is_still_refused_in_production(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp,
                     pay_provider=pay.SimulatedGateway(), environment='production', provider=rt.SimulatedProvider())
        self.assertFalse(app.online_ok)
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        with mock.patch('cta_app.messagebox'):
            app._issue(['Jane Doe'], 'card')
        self.assertEqual(app.history, [])

    def test_pending_online_payment_saves_booking_without_issuing_a_ticket(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp,
                     pay_provider=FakePSP(claim='pending'), environment='production', provider=rt.SimulatedProvider())
        self.assertTrue(app.online_ok)
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        shown = []
        app.show_confirmation = lambda rec, saved=None: shown.append(rec)
        with mock.patch('cta_app.messagebox') as box:
            app._issue(['Jane Doe'], 'card')
        self.assertEqual(app.history[0]['payment']['status'], 'pending')
        self.assertEqual(shown, [])                                 # no "ticket issued" screen
        self.assertFalse(list(Path(self.tmp).glob('CTA-*.pdf')))     # no PDF ticket written
        self.assertEqual(box.showwarning.call_args[0][0], 'Payment not confirmed')
        with mock.patch('cta_app.messagebox'):
            app.pay_provider.claim = 'failed'
            app._issue(['Jane Doe'], 'card')
        self.assertEqual(len(app.history), 1)                       # a failed payment stores nothing

    def test_damaged_data_puts_the_app_in_safe_mode(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        legacy_install(self.tmp)
        (Path(self.tmp) / 'history.json').write_text('{"half": ')
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)
        self.root.after_cancel(app._problems_after)
        self.assertIn('history.json', app.storage_problem)
        self.assertFalse(app.can_save())
        self.assertFalse((Path(self.tmp) / 'cta.db').exists())                # nothing created over the damage
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(1)
        with mock.patch('cta_app.messagebox') as box:
            app.book()
            app._show_problems()
        self.assertEqual(box.showerror.call_args_list[0][0][0], 'Bookings are paused')
        self.assertIn('history.json', box.showerror.call_args_list[1][0][1])
        self.assertFalse(app.save_history())
        self.assertEqual((Path(self.tmp) / 'history.json').read_text(), '{"half": ')
        with self.assertRaises(cta_storage.StorageError):
            acct.save_accounts(app.data_target, [])                           # sign-up cannot write either

    def test_existing_json_install_is_migrated_on_start(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        hist, accounts, staff = legacy_install(self.tmp)
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)
        self.assertIsNone(app.storage_problem)
        self.assertEqual([r['reference'] for r in app.history], [r['reference'] for r in hist])
        self.assertEqual(app.staff_accounts[0]['username'], 'admin')
        self.assertTrue(list((Path(self.tmp) / 'backups').glob('cta-*-auto.db')))   # daily backup made

    def test_two_windows_do_not_overwrite_each_other(self):
        from cta_app import CTAApp
        self.tmp = tempfile.mkdtemp()
        legacy_install(self.tmp)
        mk = lambda: CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp)
        one, two = mk(), mk()
        for app in (one, two):
            app.staff = sign_in(app.store, 'admin')
        tno = one.history[1]['passengers'][0]['ticket_no']                    # the cash booking
        one.admin.tree.selection = lambda: (tno,)
        one.admin.mark_selected_paid()
        self.assertIn('recorded', one.admin.result.cget('text'))
        two.history[1]['passengers'][0]['status'] = 'boarded'                 # stale edit in the other window
        with mock.patch.object(two.store, 'has_changes', return_value=False):
            self.assertFalse(two.save_history())
        self.assertIn('changed by another window', two.save_error)
        self.assertEqual(two.history[1]['payment']['status'], 'paid')         # reloaded: sees window one's change
        two.admin._check_in(tno)
        self.assertIn('OK to travel', two.admin.result.cget('text'))
        self.assertEqual(two.store.scans()[-1]['ticket_no'], tno.upper())

    def test_tapping_the_map_picks_start_then_destination(self):
        app = self.make()
        self.assertEqual(app.pick_step, 'to')                    # Centrala is pre-filled as the start
        app._plan_map_click('Zord')
        self.assertEqual((app.from_pick.get(), app.to_pick.get()), ('Centrala', 'Zord'))
        self.assertEqual(app.route.stations[-1], 'Zord')
        self.assertEqual(app.pick_step, 'done')
        self.assertIn('Zord', app.step_labels[1].cget('text'))
        app._plan_map_click('Sylas')                             # a tap after both are set starts a new journey
        self.assertEqual((app.from_pick.get(), app.to_pick.get()), ('Sylas', None))
        self.assertEqual(app.pick_step, 'to')
        app._plan_map_click('Sylas')                             # same station as the start is refused
        self.assertIn('different station', app.err.cget('text'))
        self.assertIsNone(app.to_pick.get())
        app._set_step('from')                                    # tapping the first chip lets you change the start
        app._plan_map_click('Zord')
        self.assertEqual(app.from_pick.get(), 'Zord')

    def test_step_chips_follow_the_journey(self):
        app = self.make()
        self.assertIn('Tap your destination', app.step_labels[1].cget('text'))
        app._plan_map_click('Zord')
        self.assertIn('Add travellers', app.step_labels[2].cget('text'))
        app.steppers['Adult'].set(2)
        self.assertIn('2 adults', app.step_labels[2].cget('text'))

    def test_payment_dialog_has_one_picture_tile_per_method(self):
        from cta_dialogs import PaymentDialog
        d = PaymentDialog(self.root, 8420, 1, lambda m: None)
        self.assertEqual(set(d.tiles), {m[0] for m in pay.methods()})
        d._select('mada')
        self.assertIn('mada', d.confirm.text)

    def test_book_is_blocked_above_twenty_travellers(self):
        app = self.make()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(21)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        with mock.patch('cta_app.messagebox.showwarning') as warn, mock.patch('cta_app.PassengerDialog') as dialog:
            app.book()
        warn.assert_called_once()
        dialog.assert_not_called()

    def test_my_bookings_list_only_shows_own_tickets(self):
        app = self.make()
        route = rt.astar('Yaen', 'Zord')
        mine = core.make_record(route, {'Adult': 1}, core.chargeable_zones(route.origin, route.destination, route), app.clock(), app.clock(), [], names=['Jane Doe'],
                                account_phone='+447123456789')
        theirs = core.make_record(route, {'Adult': 1}, core.chargeable_zones(route.origin, route.destination, route), app.clock(), app.clock(), [mine], names=['Other Person'],
                                  account_phone='+447000000000')
        app.history.extend([mine, theirs])
        app.refresh_my_tickets()
        self.assertEqual(len(app.my_list.winfo_children()), 2)       # signed out: message + Sign in button
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.refresh_my_tickets()
        self.assertEqual(len(app.my_list.winfo_children()), 3)       # heading + one booking + footnote

    def test_same_station_error_and_swap(self):
        app = self.make()
        app.from_pick.set('Zord'); app.to_pick.set('Zord'); app.search()
        self.assertIn('different', app.err.cget('text'))
        self.assertIsNone(app.route)
        app.to_pick.set('Marend'); app.swap()
        self.assertEqual((app.from_pick.get(), app.to_pick.get()), ('Marend', 'Zord'))

    def test_bad_time_rejected(self):
        app = self.make()
        app.from_pick.set('Yaen'); app.to_pick.set('Zord')
        app.when.select('Later', notify=False)
        app.time_var.set('25:99'); app.search()
        self.assertIn('HH:MM', app.err.cget('text'))
        app.time_var.set('14:30'); app.search()
        self.assertEqual(app.deps[0].strftime('%H:%M'), '14:30')

    def test_ticket_lookup_and_lockout(self):
        app = self.make()
        rec = core.make_record(rt.astar('Yaen', 'Zord'), {'Student': 1}, core.band_zones('Yaen', 'Zord'), app.clock(), app.clock(), [])
        app.history.append(rec)
        app.ref_var.set(rec['reference']); app.code_var.set('nope'); app.lookup()
        self.assertIn('No ticket', app.lookup_msg.cget('text'))
        app.code_var.set(rec['recovery_code']); app.lookup()
        self.assertEqual(app.lookup_msg.cget('text'), '')
        self.assertTrue(app.lookup_result.winfo_children())
        app.code_var.set('bad')
        for _ in range(5):
            app.lookup()
        self.assertIn('Too many', (app.lookup(), app.lookup_msg.cget('text'))[1])

    def test_all_pages_open(self):
        app = self.make()
        for k in ('map', 'tickets', 'help', 'plan'):
            app.show_page(k); self.root.update_idletasks()


if __name__ == '__main__':
    unittest.main(verbosity=2)
