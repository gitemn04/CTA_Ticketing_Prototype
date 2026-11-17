"""Tests for the zone-only booking (ATHE Unit 2, Appendix 2): station board, zone fares, issued voucher, repeat/exit.

Run:  python -m pytest tests/test_zone_booking.py -v        (or)  python -m unittest tests.test_zone_booking -v
The GUI tests need a display (they are skipped, not failed, without one).
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['CTA_ENV'] = 'testing'                            # same as the other suites: schematic routing, test key
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)           # throw-away signing key for tests only

import cta_fares as fares                                    # noqa: E402
import cta_routing as rt                                     # noqa: E402
import cta_zone_booking as zb                                # noqa: E402

NOW = datetime(2026, 11, 5, 13, 52, 10)
RATES = {'Adult': 2105, 'Child': 1410, 'Senior': 1025, 'Student': 1750}


def counts(adult=0, child=0, senior=0, student=0):
    return {'Adult': str(adult), 'Child': str(child), 'Senior': str(senior), 'Student': str(student)}


def pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return '\n'.join(p.extract_text() for p in PdfReader(str(path)).pages)
    except ImportError:
        pass
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        return '\n'.join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    except ImportError:
        raise unittest.SkipTest('pypdf (or pypdfium2) is needed to read the PDF text')


# ------------------------------------------------------------------ requirement 1: station board
class StationBoardTests(unittest.TestCase):
    def test_board_has_all_36_stations_grouped_by_zone_and_sorted_a_to_z(self):
        board = zb.station_board()
        self.assertEqual([z for z, _ in board], ['Central', 'Midtown', 'Downtown'])
        sizes = {z: len(names) for z, names in board}
        self.assertEqual(sizes, {'Central': 10, 'Midtown': 12, 'Downtown': 14})
        every = [s for _, names in board for s in names]
        self.assertEqual(len(every), 36)
        self.assertEqual(set(every), set(rt.STATIONS))              # nothing lost, nothing added
        self.assertEqual(len(set(every)), 36)                       # no station in two zones
        for zone, names in board:
            self.assertEqual(names, sorted(names, key=str.casefold), zone)
            for s in names:
                self.assertEqual(rt.ZONES[s], zone, s)

    def test_wicyt_is_in_midtown(self):
        self.assertIn('Wicyt', dict(zb.station_board())['Midtown'])

    def test_network_keeps_36_stations_and_35_connections(self):
        self.assertEqual(zb.network_counts(), (36, 35))
        self.assertEqual(rt.validate_network(), [])
        self.assertEqual(len({frozenset(link) for link in rt.LINKS}), 35)

    def test_exact_board_contents(self):
        self.assertEqual(dict(zb.station_board()), {
            'Central': ['Bylyn', 'Centrala', 'Frestin', 'Jaund', 'Lomil', 'Ninia', 'Rede', 'Soth', 'Tallan', 'Yaen'],
            'Midtown': ['Agralle', 'Docia', 'Garion', 'Obelyn', 'Oloadus', 'Quthiel', 'Ralith', 'Riclya', 'Riladia',
                        'Stonyam', 'Sylas', 'Wicyt'],
            'Downtown': ['Adohad', 'Brunad', 'Ederif', 'Elyot', 'Erean', 'Holmer', 'Keivia', 'Marend', 'Perinad',
                         'Pryn', 'Ruril', 'Ryall', 'Vertwall', 'Zord']})


# ------------------------------------------------------------------ requirement 2: zones, counts, fares
class ZoneRuleTests(unittest.TestCase):
    def test_rates_are_the_assignment_rates_in_cents(self):
        self.assertEqual(dict(fares.FARE_RATES_CENTS), RATES)

    def test_chargeable_zones_for_all_nine_pairs(self):
        want = {('Central', 'Central'): 1, ('Central', 'Midtown'): 2, ('Central', 'Downtown'): 3,
                ('Midtown', 'Central'): 2, ('Midtown', 'Midtown'): 1, ('Midtown', 'Downtown'): 2,
                ('Downtown', 'Central'): 3, ('Downtown', 'Midtown'): 2, ('Downtown', 'Downtown'): 1}
        self.assertEqual({(a, b): n for a, b, n in zb.zone_matrix()}, want)

    def test_same_start_and_destination_zone_is_allowed(self):
        v = zb.issue_voucher('Downtown', 'Downtown', counts(adult=1), NOW)
        self.assertEqual((v.zones, v.total_cents), (1, 2105))


class AssignmentExampleTests(unittest.TestCase):
    def test_central_to_downtown_family(self):
        v = zb.issue_voucher('Central', 'Downtown', counts(2, 1, 1, 1), NOW)
        self.assertEqual(v.zones, 3)
        self.assertEqual(v.travellers, 5)
        self.assertEqual(v.total_cents, 25185)
        self.assertEqual([ln.subtotal_cents for ln in v.lines], [12630, 4230, 3075, 5250])

    def test_midtown_to_midtown_one_child(self):
        v = zb.issue_voucher('Midtown', 'Midtown', counts(child=1), NOW)
        self.assertEqual((v.zones, v.travellers, v.total_cents), (1, 1, 1410))

    def test_central_to_midtown_one_adult(self):
        v = zb.issue_voucher('Central', 'Midtown', counts(adult=1), NOW)
        self.assertEqual((v.zones, v.travellers, v.total_cents), (2, 1, 4210))

    def test_reverse_direction_costs_the_same(self):
        a = zb.issue_voucher('Downtown', 'Central', counts(2, 1, 1, 1), NOW)
        self.assertEqual((a.zones, a.total_cents), (3, 25185))


class ValidationTests(unittest.TestCase):
    def refused(self, start, dest, raw, words):
        with self.assertRaises(zb.ZoneBookingError) as cm:
            zb.issue_voucher(start, dest, raw, NOW)
        self.assertIn(words, str(cm.exception))

    def test_missing_zones_are_refused(self):
        self.refused(None, None, counts(adult=1), 'Choose a start zone and a destination zone')
        self.refused(None, 'Midtown', counts(adult=1), 'Choose a start zone')
        self.refused('Central', None, counts(adult=1), 'Choose a destination zone')
        self.refused('Centrala', 'Midtown', counts(adult=1), 'valid start zone')      # a station is not a zone

    def test_all_zero_group_is_refused_with_a_clear_message(self):
        self.refused('Central', 'Midtown', counts(), 'Enter at least one traveller')

    def test_bad_counts_are_refused(self):
        for bad, words in (('', 'enter a number'), ('-1', 'not a whole number'), ('1.5', 'not a whole number'),
                           ('two', 'not a whole number'), ('100', 'from 0 to 99'), ('٣', 'not a whole number')):
            with self.subTest(bad=bad):
                self.refused('Central', 'Midtown', {'Adult': bad, 'Child': '0', 'Senior': '0', 'Student': '0'}, words)
        self.refused('Central', 'Midtown', {'Adult': True, 'Child': 0, 'Senior': 0, 'Student': 0}, 'whole number')

    def test_spaces_around_a_number_are_accepted(self):
        self.assertEqual(zb.parse_counts({'Adult': ' 2 ', 'Child': '0', 'Senior': '0', 'Student': '0'})['Adult'], 2)

    def test_after_correcting_the_input_the_booking_proceeds(self):
        self.refused(None, 'Downtown', counts(2, 1, 1, 1), 'start zone')
        self.refused('Central', 'Downtown', counts(), 'at least one traveller')
        v = zb.issue_voucher('Central', 'Downtown', counts(2, 1, 1, 1), NOW)
        self.assertEqual(v.total_cents, 25185)


# ------------------------------------------------------------------ requirement 3: the voucher
class VoucherTests(unittest.TestCase):
    def setUp(self):
        self.v = zb.issue_voucher('Midtown', 'Midtown', counts(child=1), NOW, number='ZV-TEST-0001')

    def test_voucher_lists_all_four_categories_including_zero_counts(self):
        self.assertEqual([ln.category for ln in self.v.lines], ['Adult', 'Child', 'Senior', 'Student'])
        self.assertEqual([ln.count for ln in self.v.lines], [0, 1, 0, 0])
        self.assertEqual([ln.rate_cents for ln in self.v.lines], [2105, 1410, 1025, 1750])
        self.assertEqual([ln.subtotal_cents for ln in self.v.lines], [0, 1410, 0, 0])

    def test_subtotal_is_count_times_rate_times_zones(self):
        v = zb.issue_voucher('Central', 'Downtown', counts(2, 1, 1, 1), NOW)
        for ln in v.lines:
            self.assertEqual(ln.subtotal_cents, ln.count * ln.rate_cents * v.zones)
        self.assertEqual(v.total_cents, sum(ln.subtotal_cents for ln in v.lines))

    def test_voucher_text_has_every_required_field(self):
        text = self.v.text()
        for want in ('Issued: 05 Nov 2026, 13:52:10', 'From zone: Midtown (2)', 'To zone: Midtown (2)',
                     'Chargeable zones: 1', 'Adults: 0 x 2,105 cents x 1 zone(s) = 0 cents',
                     'Children: 1 x 1,410 cents x 1 zone(s) = 1,410 cents', 'Seniors: 0 x 1,025 cents',
                     'Students: 0 x 1,750 cents', 'Travellers: 1', 'Total fare: 1,410 cents'):
            self.assertIn(want, text)

    def test_voucher_number_is_unique_and_dated(self):
        a = zb.issue_voucher('Central', 'Central', counts(adult=1), NOW)
        b = zb.issue_voucher('Central', 'Central', counts(adult=1), NOW)
        self.assertTrue(a.number.startswith('ZV-20261105-135210-'))
        self.assertNotEqual(a.number, b.number)

    def test_log_entry_holds_zones_and_counts_only(self):
        d = self.v.as_dict()
        self.assertEqual(d['total_cents'], 1410)
        self.assertEqual(d['chargeable_zones'], 1)
        self.assertEqual(json.loads(json.dumps(d)), d)


class VoucherPdfTests(unittest.TestCase):
    def test_pdf_agrees_with_the_voucher_and_is_not_a4(self):
        import cta_voucher_pdf
        v = zb.issue_voucher('Central', 'Downtown', counts(2, 1, 1, 1), NOW, number='ZV-TEST-0002')
        with tempfile.TemporaryDirectory() as d:
            path = cta_voucher_pdf.build_voucher_pdf(v, Path(d) / 'v.pdf')
            text = pdf_text(path)
            try:
                from pypdf import PdfReader
                box = PdfReader(str(path)).pages[0].mediabox
                self.assertEqual((float(box.width), float(box.height)), (540.0, 288.0))   # 7.5 x 4 in, not A4
            except ImportError:
                pass
        flat = ' '.join(text.split())
        for want in ('ZV-TEST-0002', 'Issued 05 Nov 2026, 13:52:10', 'Central (1)', 'Downtown (3)',
                     'Adults', 'Children', 'Seniors', 'Students', '2,105 cents', '1,410 cents', '1,025 cents',
                     '1,750 cents', '12,630 cents', '4,230 cents', '3,075 cents', '5,250 cents',
                     'Total travellers: 5', 'Total fare: 25,185 cents'):
            self.assertIn(want, flat)


# ------------------------------------------------------------------ the optional station planner still works
class StationPlannerRegressionTests(unittest.TestCase):
    def test_station_planner_quotes_still_work(self):
        q = fares.quote('Centrala', 'Quthiel', {'Adult': 1})                    # Central -> Midtown
        self.assertEqual((q.zones, q.total_cents), (2, 4210))
        q = fares.quote('Yaen', 'Zord', {'Adult': 2, 'Child': 1, 'Senior': 1, 'Student': 1})
        self.assertEqual((q.zones, q.total_cents), (3, 25185))                  # Central -> Downtown
        fares.check_against_matrix()                                            # reviewed fare matrix unchanged

    def test_own_edge_case_zord_to_marend_is_kept_separate(self):
        """Owner's edge case for the STATION planner (not an assignment example): both stations are Downtown, but the
        only track runs through Midtown and Central. The planner's documented band rule charges 1 zone; the route
        passes through 3 different zones. This test records today's behaviour so any change is deliberate."""
        route = rt.astar('Zord', 'Marend')
        self.assertEqual(route.stations, ('Zord', 'Quthiel', 'Yaen', 'Centrala', 'Bylyn', 'Agralle', 'Docia',
                                          'Marend'))
        self.assertEqual(route.zones_entered, ('Downtown', 'Midtown', 'Central', 'Midtown', 'Downtown'))
        q = fares.quote('Zord', 'Marend', {'Adult': 1})
        self.assertEqual((q.zones, q.total_cents), (1, 2105))
        self.assertEqual(fares.route_zones(route), 3)


# ------------------------------------------------------------------ GUI (needs a display)
class ZoneGuiTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception:
            self.skipTest('no display / tkinter')
        self.root.withdraw()
        self.tmp = tempfile.mkdtemp()
        from cta_app import CTAApp
        import cta_zone_ui
        self.saved = []
        cta_zone_ui.core.open_pdf = lambda p: (True, f'Opened {p}')             # never launch a real viewer
        cta_zone_ui.core.print_pdf = lambda p: (self.saved.append(p) or True, 'Sent to your default printer.')
        self.app = CTAApp(self.root, clock=lambda: NOW, folder=self.tmp, environment='testing')

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def fill(self, start, dest, adult=0, child=0, senior=0, student=0):
        app = self.app
        app.zone_from.select(start)
        app.zone_to.select(dest)
        for cat, n in zip(zb.CATEGORIES, (adult, child, senior, student)):
            app.zone_count_vars[cat].set(str(n))

    def log_lines(self):
        p = Path(self.tmp) / 'zone_vouchers.jsonl'
        return p.read_text(encoding='utf-8').splitlines() if p.exists() else []

    def test_zone_ticket_is_the_home_page_and_first_in_the_menu(self):
        self.assertEqual(self.app.current_page, 'zone')
        self.assertEqual(next(iter(self.app.nav)), 'zone')
        self.assertEqual(self.app.nav['zone'].text, 'Zone ticket')
        self.assertIsNone(self.app.zone_from.value)
        self.assertIsNone(self.app.zone_to.value)

    def test_station_board_page_shows_all_stations_grouped_and_sorted(self):
        self.app.show_page('board')
        self.assertEqual(self.app.current_page, 'board')
        self.assertEqual(self.app.board_cells, dict(zb.station_board()))
        self.assertEqual(sum(len(v) for v in self.app.board_cells.values()), 36)

    def test_missing_zone_is_refused_without_a_voucher_then_corrected(self):
        app = self.app
        self.fill(None, 'Downtown', 2, 1, 1, 1)
        self.assertIsNone(app.issue_zone_voucher())
        self.assertEqual(app.current_page, 'zone')
        self.assertIn('Choose a start zone', app.zone_err.cget('text'))
        self.assertIsNone(app.voucher)
        self.assertEqual(self.log_lines(), [])
        app.zone_from.select('Central')                                     # correct it
        v = app.issue_zone_voucher()
        self.assertIsNotNone(v)
        self.assertEqual(app.current_page, 'voucher')
        self.assertEqual(v.total_cents, 25185)
        self.assertEqual(len(self.log_lines()), 1)

    def test_all_zero_group_is_refused_then_corrected(self):
        app = self.app
        self.fill('Midtown', 'Midtown')
        self.assertIsNone(app.issue_zone_voucher())
        self.assertIn('Enter at least one traveller', app.zone_err.cget('text'))
        self.assertEqual(app.current_page, 'zone')
        self.assertEqual(self.log_lines(), [])
        app.zone_count_vars['Child'].set('1')
        v = app.issue_zone_voucher()
        self.assertEqual((v.zones, v.total_cents), (1, 1410))

    def test_plus_and_minus_buttons_change_the_counts(self):
        app = self.app
        app._zone_step('Adult', 1)
        app._zone_step('Adult', 1)
        app._zone_step('Adult', -1)
        app._zone_step('Child', -1)
        self.assertEqual(app.zone_counts_raw(), {'Adult': '1', 'Child': '0', 'Senior': '0', 'Student': '0'})

    def test_issued_voucher_screen_shows_every_field_and_matches_the_pdf(self):
        app = self.app
        self.fill('Central', 'Downtown', 2, 1, 1, 1)
        v = app.issue_zone_voucher()
        lab = {k: w.cget('text') for k, w in app.voucher_labels.items()}
        self.assertEqual(lab['issued'], '05 Nov 2026, 13:52:10')
        self.assertEqual(lab['start'], 'Central (1)')
        self.assertEqual(lab['destination'], 'Downtown (3)')
        self.assertEqual(lab['zones'], '3')
        want = {'Adult': ('Adults', '2', '2,105 cents', '12,630 cents'),
                'Child': ('Children', '1', '1,410 cents', '4,230 cents'),
                'Senior': ('Seniors', '1', '1,025 cents', '3,075 cents'),
                'Student': ('Students', '1', '1,750 cents', '5,250 cents')}
        for cat, (name, n, rate, sub) in want.items():
            self.assertEqual((lab[f'{cat}.label'], lab[f'{cat}.count'], lab[f'{cat}.rate'], lab[f'{cat}.zones'],
                              lab[f'{cat}.subtotal']), (name, n, rate, '3', sub))
        self.assertEqual(lab['travellers'], 'Total travellers: 5')
        self.assertEqual(lab['total'], 'Total fare: 25,185 cents')
        app.print_voucher()                                              # the print copy is this PDF
        self.assertEqual(len(self.saved), 1)
        flat = ' '.join(pdf_text(self.saved[0]).split())
        for text in [lab['issued'], lab['start'], lab['destination'], lab['travellers'], lab['total'], v.number] + [
                lab[f'{c}.subtotal'] for c in want] + [lab[f'{c}.rate'] for c in want]:
            self.assertIn(text, flat)
        self.assertIn('Sent to your default printer', app.voucher_status.cget('text'))

    def test_zero_categories_are_shown_on_the_voucher(self):
        app = self.app
        self.fill('Central', 'Midtown', adult=1)
        app.issue_zone_voucher()
        lab = {k: w.cget('text') for k, w in app.voucher_labels.items()}
        self.assertEqual((lab['zones'], lab['total']), ('2', 'Total fare: 4,210 cents'))
        for cat in ('Child', 'Senior', 'Student'):
            self.assertEqual((lab[f'{cat}.count'], lab[f'{cat}.subtotal']), ('0', '0 cents'))

    def test_issue_another_voucher_resets_the_form(self):
        app = self.app
        self.fill('Central', 'Downtown', 2, 1, 1, 1)
        app.issue_zone_voucher()
        self.assertEqual(app.issue_another_btn.text, 'Issue another voucher')
        app.issue_another_btn._click()
        self.assertEqual(app.current_page, 'zone')
        self.assertIsNone(app.voucher)
        self.assertIsNone(app.zone_from.value)
        self.assertIsNone(app.zone_to.value)
        self.assertEqual(app.zone_counts_raw(), {'Adult': '0', 'Child': '0', 'Senior': '0', 'Student': '0'})
        self.assertEqual(app.zone_err.cget('text'), '')
        self.fill('Midtown', 'Midtown', child=1)                          # and a second voucher can be issued
        self.assertEqual(app.issue_zone_voucher().total_cents, 1410)

    def test_exit_closes_the_app(self):
        import tkinter as tk
        app = self.app
        self.fill('Central', 'Midtown', adult=1)
        app.issue_zone_voucher()
        self.assertEqual(app.exit_btn.text, 'Exit')
        app.exit_btn._click()
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()

    def test_voucher_page_cannot_open_without_a_voucher(self):
        self.app.show_page('voucher')
        self.assertEqual(self.app.current_page, 'zone')

    def test_station_planner_still_works_in_the_app(self):
        app = self.app
        app.show_page('plan')
        app.from_pick.set('Centrala')
        app.to_pick.set('Quthiel')
        app.search()
        app.steppers['Adult'].set(1)
        self.assertEqual(app.route.stations[-1], 'Quthiel')
        self.assertTrue(app.book_btn.enabled)
        self.assertIn('4,210', app.bar_total.cget('text'))

    def test_zone_booking_works_even_when_journey_planning_is_not_configured(self):
        """A fresh copy without cta_config.json: the station planner reports 'not configured', the zone flow still works
        because it needs no route."""
        import tkinter as tk
        from unittest import mock
        import cta_routing
        from cta_app import CTAApp
        root2 = tk.Tk()
        root2.withdraw()
        try:
            with mock.patch.object(cta_routing, 'CONFIG_PATH', Path(self.tmp) / 'missing_config.json'):
                app = CTAApp(root2, clock=lambda: NOW, folder=tempfile.mkdtemp(), environment='production')
            self.assertTrue(app.routing_problem)                         # planner off, as before this change
            app.zone_from.select('Central')
            app.zone_to.select('Downtown')
            for cat, n in zip(zb.CATEGORIES, (2, 1, 1, 1)):
                app.zone_count_vars[cat].set(str(n))
            self.assertEqual(app.issue_zone_voucher().total_cents, 25185)
        finally:
            root2.destroy()

    def test_every_page_opens(self):
        for k in ('zone', 'board', 'plan', 'map', 'tickets', 'help'):
            self.app.show_page(k)
            self.root.update_idletasks()
            self.assertEqual(self.app.current_page, k)


if __name__ == '__main__':
    unittest.main(verbosity=2)
