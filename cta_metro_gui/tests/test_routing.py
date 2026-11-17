"""Module 6 tests: routing is never silently simulated, and the network it uses is checked."""
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

from test_all import sign_in  # noqa: F401  (also applies the shared fast test settings)
import cta_core as core
import cta_fares as fares
import cta_routing as rt
import cta_tickets as T

SECRET = 'AIza-SECRET-KEY-123'
NOW = datetime(2026, 10, 25, 9, 0)


def full_geo():
    """Distinct, valid coordinates for every station (a stand-in for a real survey)."""
    return {s: [24.0 + y / 10000, 46.0 + x / 10000] for s, (x, y) in rt.STATIONS.items()}


class Resp:
    def __init__(self, data): self.data = data
    def read(self): return json.dumps(self.data).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def ok_opener(req, timeout=0):
    return Resp({'routes': [{'distanceMeters': 9000, 'duration': '1200s'}]})


def failing_opener(req, timeout=0):
    raise OSError(f'connection refused while sending key {SECRET}')        # error text that contains the key


# ====================================================================== the network data
class NetworkTests(unittest.TestCase):
    def test_the_shipped_network_is_valid_connected_and_a_tree(self):
        self.assertEqual(rt.validate_network(), [])
        self.assertEqual(len(rt.LINKS), len(rt.STATIONS) - 1)                  # connected + n-1 links = one route each
        with open(HERE.parent / 'edge_weights.csv', encoding='utf-8') as fh:
            self.assertEqual(fh.read(), rt.weights_csv())                      # the published file is what the code uses

    def test_every_kind_of_bad_network_data_is_reported(self):
        st, links = dict(rt.STATIONS), tuple(rt.LINKS)
        cases = {
            'unknown station in a link': (st, links + (('Centrala', 'Atlantis'),), 'does not exist'),
            'self link': (st, links + (('Zord', 'Zord'),), 'itself'),
            'duplicate link': (st, links + (('Yaen', 'Centrala'),), 'more than once'),
            'two stations on one spot': (dict(st, Yaen=st['Centrala']), links, 'same position'),
            'missing position': (dict(st, Zord=None), links, 'no valid position'),
            'NaN position': (dict(st, Zord=(float('nan'), 1.0)), links, 'no valid position'),
            'disconnected station': (dict(st, Island=(5000, 5000)), links, 'cannot be reached'),
            'cut-off branch': (st, tuple(l for l in links if l != ('Centrala', 'Yaen')), 'cannot be reached'),
            'padded name': (dict(st, **{' Zord': (1, 1)}), links, 'spaces'),
        }
        for why, (stations, lk, needle) in cases.items():
            problems = rt.validate_network(stations, lk)
            self.assertTrue(any(needle in p for p in problems), (why, problems))
        self.assertEqual(rt.validate_network({}, ()), ['There are no stations.'])

    def test_invalid_network_stops_journey_planning(self):
        with mock.patch.object(rt, 'LINKS', rt.LINKS + (('Zord', 'Zord'),)):
            provider, health = rt.load_routing(environment='testing')
        self.assertIsNone(provider)
        self.assertEqual(health.status, 'error')
        self.assertIn('network data is invalid', health.message)


# ====================================================================== shortest paths
class ShortestPathTests(unittest.TestCase):
    def test_every_pair_has_one_valid_route_that_matches_a_plain_search(self):
        def bfs(a, b):                                                         # independent reference implementation
            prev, todo = {a: None}, [a]
            while todo:
                n = todo.pop(0)
                for m in rt.GRAPH[n]:
                    if m not in prev:
                        prev[m] = n; todo.append(m)
            path, n = [], b
            while n is not None:
                path.append(n); n = prev[n]
            return tuple(reversed(path))
        for a in rt.STATIONS:
            for b in rt.STATIONS:
                if a == b:
                    continue
                r = rt.astar(a, b)
                self.assertEqual(r.stations, bfs(a, b))
                self.assertEqual(rt.astar(b, a).stations, tuple(reversed(r.stations)))
                self.assertTrue(all(y in rt.GRAPH[x] for x, y in zip(r.stations, r.stations[1:])))
                self.assertGreater(r.km, 0)
                self.assertEqual(r.zones_entered, rt._zone_sequence(list(r.stations)))

    def test_disconnected_stations_give_a_clear_error_not_a_wrong_route(self):
        graph = {k: dict(v) for k, v in rt.GRAPH.items()}
        del graph['Centrala']['Yaen'], graph['Yaen']['Centrala']
        with mock.patch.object(rt, 'GRAPH', graph):
            for a, b in (('Centrala', 'Zord'), ('Zord', 'Marend'), ('Wicyt', 'Centrala')):
                with self.assertRaises(ValueError) as cm:
                    rt.astar(a, b)
                self.assertIn('No route', str(cm.exception))
            self.assertEqual(rt.astar('Zord', 'Wicyt').stations, ('Zord', 'Quthiel', 'Yaen', 'Wicyt'))   # same side works
            with self.assertRaises(fares.FareError):
                fares.quote('Centrala', 'Zord', {'Adult': 1})

    def test_invalid_track_lengths_are_refused_not_used(self):
        for bad in (float('nan'), float('inf'), 0.0, -1.0, '2'):
            graph = {k: dict(v) for k, v in rt.GRAPH.items()}
            graph['Centrala']['Yaen'] = bad
            with mock.patch.object(rt, 'GRAPH', graph):
                with self.assertRaises(ValueError, msg=repr(bad)) as cm:
                    rt.astar('Centrala', 'Zord')
                self.assertIn('invalid track length', str(cm.exception))

    def test_unknown_stations_are_refused(self):
        for a, b in (('Atlantis', 'Zord'), ('Zord', ''), (None, 'Zord')):
            with self.assertRaises(ValueError):
                rt.astar(a, b)


# ====================================================================== choosing and loading a provider
class LoadingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg = self.dir / 'cta_config.json'
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CTA_GOOGLE_API_KEY', None)

    def load(self, cfg=None, env='production', geo=None, geo_text=None):
        if cfg is not None:
            self.cfg.write_text(json.dumps(cfg) if not isinstance(cfg, str) else cfg)
        if geo is not None:
            (self.dir / 'stations_geo.json').write_text(json.dumps(geo))
        if geo_text is not None:
            (self.dir / 'stations_geo.json').write_text(geo_text)
        return rt.load_routing(self.cfg, env)

    def assertRefused(self, result, needle):
        provider, health = result
        self.assertIsNone(provider, health)                                    # never a quiet substitute
        self.assertEqual(health.status, 'error')
        self.assertFalse(health.live)
        self.assertIn(needle, health.message)
        self.assertNotIn(SECRET, health.message)
        self.assertEqual(health.label(), 'Journey planning unavailable')

    def test_development_and_testing_default_to_labelled_estimates(self):
        for env in ('development', 'testing'):
            provider, health = self.load(env=env)
            self.assertIsInstance(provider, rt.SimulatedProvider)
            self.assertEqual((health.status, health.provider, health.live, health.explicit), ('ok', 'schematic', False, False))
            self.assertIn('Estimated', health.label())
            self.assertIn('not live', health.label())

    def test_production_needs_an_explicit_choice(self):
        self.assertRefused(self.load(env='production'), 'not configured')
        with mock.patch.dict(os.environ, {'CTA_ENV': 'production'}):
            with self.assertRaises(rt.RoutingConfigError):
                rt.load_provider(self.cfg)                                     # the old entry point refuses too
        for cfg in ({'routing': {'provider': 'schematic'}}, {'provider': 'simulated'}, {'routing': {'provider': 'SCHEMATIC'}}):
            provider, health = self.load(cfg)
            self.assertIsInstance(provider, rt.SimulatedProvider, cfg)
            self.assertEqual((health.status, health.live, health.explicit), ('ok', False, True))
            self.assertIn('not live', health.message)

    def test_malformed_settings_are_errors(self):
        self.assertRefused(self.load('{"routing": '), 'damaged')
        self.assertRefused(self.load({'routing': 'google'}), 'JSON object')
        self.assertRefused(self.load({'routing': {'provider': 'mapquest'}}), 'Unknown routing provider')
        self.assertRefused(self.load({'routing': {'provider': 'google', 'google_api_key': SECRET,
                                                  'travel_mode': 'TELEPORT'}}, geo=full_geo()), 'Unknown travel mode')

    def test_google_without_a_key_or_complete_valid_coordinates_does_not_start(self):
        g = {'routing': {'provider': 'google', 'google_api_key': SECRET}}
        self.assertRefused(self.load(g), 'missing')                            # no coordinates file at all
        self.assertRefused(self.load({'routing': {'provider': 'google'}}, geo=full_geo()), 'no API key')
        self.assertRefused(self.load(g, geo_text='{"Centrala": [1, 2'), 'damaged')
        self.assertRefused(self.load(g, geo=[1, 2]), 'must map')
        partial = full_geo(); del partial['Zord']
        self.assertRefused(self.load(g, geo=partial), 'no coordinates for 1 station')
        self.assertRefused(self.load(g, geo=dict(full_geo(), Atlantis=[1, 2])), 'do not exist')
        for bad in ([95.0, 10.0], [10.0, 200.0], ['north', 1], [1.0], None, [float('nan'), 1.0]):
            geo = full_geo(); geo['Zord'] = bad
            text = json.dumps(geo)
            self.assertRefused(self.load(g, geo_text=text), 'not a valid')
        same = full_geo(); same['Zord'] = same['Centrala']
        self.assertRefused(self.load(g, geo=same), 'same coordinates')
        example = json.loads((HERE.parent / 'stations_geo.example.json').read_text())
        self.assertRefused(self.load(g, geo=example), 'no coordinates for')    # the example is only a template

    def test_a_complete_google_setup_is_live_and_never_prints_the_key(self):
        provider, health = self.load({'routing': {'provider': 'google', 'google_api_key': SECRET,
                                                  'travel_mode': 'walk'}}, geo=full_geo())
        self.assertIsInstance(provider, rt.GoogleRoutesProvider)
        self.assertEqual((health.status, health.live, health.explicit), ('ok', True, True))
        self.assertEqual(provider.mode, 'WALK')
        self.assertNotIn(SECRET, repr(provider))
        self.assertNotIn(SECRET, str(provider))
        os.environ['CTA_GOOGLE_API_KEY'] = 'ENV-KEY'
        provider, _ = self.load()
        self.assertEqual(provider.api_key, 'ENV-KEY')                          # the environment wins over the file

    def test_legacy_top_level_settings_still_work(self):
        provider, health = self.load({'provider': 'google', 'google_api_key': SECRET, 'station_geo_file': 'g.json'})
        self.assertIsNone(provider)                                            # g.json does not exist
        (self.dir / 'g.json').write_text(json.dumps(full_geo()))
        provider, health = self.load()
        self.assertIsInstance(provider, rt.GoogleRoutesProvider)


# ====================================================================== live provider at run time
class LiveFailureTests(unittest.TestCase):
    def provider(self, opener):
        return rt.GoogleRoutesProvider(SECRET, {k: tuple(v) for k, v in full_geo().items()}, opener=opener)

    def test_a_failed_request_is_labelled_as_an_estimate_and_degrades_health(self):
        p = self.provider(failing_opener)
        r = p.plan('Centrala', 'Zord')
        self.assertFalse(r.verified)
        self.assertEqual(r.source, rt.ESTIMATE_SOURCE)
        self.assertIn('ESTIMATED', r.note)
        self.assertAlmostEqual(r.km, rt.astar('Centrala', 'Zord').km)
        h = p.health()
        self.assertEqual((h.status, h.live), ('degraded', True))
        for text in (r.note, h.message, h.label(), p.last_failure, repr(p)):
            self.assertNotIn(SECRET, text)                                     # the exception text held the key
            self.assertNotIn('connection refused', text)
        p._open = ok_opener
        r = p.plan('Centrala', 'Zord')
        self.assertTrue(r.verified)
        self.assertEqual((r.source, r.km), (rt.LIVE_SOURCE, 9.0))
        self.assertEqual(p.health().status, 'ok')

    def test_unusable_answers_are_not_shown_as_live(self):
        answers = [{'routes': [{'distanceMeters': 0, 'duration': '600s'}]},
                   {'routes': [{'distanceMeters': -5, 'duration': '600s'}]},
                   {'routes': [{'distanceMeters': 5000, 'duration': '0s'}]},
                   {'routes': [{'distanceMeters': 'NaN', 'duration': '600s'}]},
                   {'routes': []}, {'error': 'quota'}, {'routes': [{'distanceMeters': 5000}]}]
        for data in answers:
            p = self.provider(lambda req, timeout=0, d=data: Resp(d))
            r = p.plan('Centrala', 'Zord')
            self.assertFalse(r.verified, data)
            self.assertEqual(p.health().status, 'degraded', data)

    def test_the_route_and_fare_do_not_depend_on_the_provider(self):
        live = self.provider(ok_opener).plan('Zord', 'Marend')
        estimate = rt.SimulatedProvider().plan('Zord', 'Marend')
        self.assertEqual(live.stations, estimate.stations)
        self.assertEqual(live.zones_entered, estimate.zones_entered)
        self.assertEqual(fares.quote('Zord', 'Marend', {'Adult': 1}, route=live).total_cents,
                         fares.quote('Zord', 'Marend', {'Adult': 1}, route=estimate).total_cents)

    def test_a_route_whose_details_do_not_match_its_stations_is_refused(self):
        r = rt.astar('Centrala', 'Zord')
        from dataclasses import replace
        for bad in (replace(r, zones_entered=('Central',)), replace(r, km=float('nan')), replace(r, ride_minutes=-1.0),
                    replace(r, stations=('Centrala', 'Quthiel', 'Zord'))):
            with self.assertRaises(fares.FareError):
                fares.quote('Centrala', 'Zord', {'Adult': 1}, route=bad)
            with self.assertRaises(ValueError):
                core.make_record(bad, {'Adult': 1}, 3, NOW, NOW, [], names=['Ann Lee'])


# ====================================================================== the application
class AppRoutingTests(unittest.TestCase):
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
        p = mock.patch('cta_app.messagebox')
        self.box = p.start(); self.addCleanup(p.stop)

    def app(self, **kw):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: datetime(2026, 10, 24, 14, 2, 10), folder=self.tmp, **kw)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def labels(self, widget):
        import tkinter as tk
        out = []
        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, tk.Label):
                    out.append(str(c.cget('text')))
                walk(c)
        walk(widget)
        return out

    def book(self, app, a='Centrala', b='Zord'):
        app.from_pick.set(a); app.to_pick.set(b); app.search()
        app.steppers['Adult'].set(1)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(['Ann Lee'], 'card')
        return app.history[-1]

    def test_production_without_routing_settings_pauses_planning_with_a_clear_reason(self):
        app = self.app(environment='production')
        self.assertIsNone(app.provider)
        self.assertIn('not configured', app.routing_problem)
        self.assertIn(app.routing_problem, app.problems)
        self.assertFalse(app.can_issue())
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        self.assertIsNone(app.route)
        self.assertIn('Journey planning is unavailable', app.err.cget('text'))
        events = [r for r in app.store.read_audit(sign_in(app.store), app.clock()) if r['kind'] == 'routing.unavailable']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['outcome'], 'failure')
        badges = [t for t in self.labels(self.root) if 'UNAVAILABLE' in t]
        self.assertIn('JOURNEY PLANNING UNAVAILABLE', badges)

    def test_estimated_routes_are_labelled_on_screen_on_the_ticket_and_in_the_pdf(self):
        app = self.app()
        rec = self.book(app)
        self.assertFalse(rec['distance_verified'])
        self.assertEqual(rec['distance_source'], rt.ESTIMATE_SOURCE)
        self.assertTrue(any('estimates from the CTA network diagram (not live)' in t for t in self.labels(app.summary.body)))
        frame = app.ticket_view(self.root, rec)
        self.assertTrue(any('(estimated)' in t for t in self.labels(frame)))
        frame.destroy()
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = PdfReader(str(Path(self.tmp) / f"{rec['reference']}.pdf")).pages[0].extract_text()
        self.assertIn('(estimate)', text)

    def test_live_routes_say_live_and_a_failure_is_shown_and_recorded_once(self):
        geo = {k: tuple(v) for k, v in full_geo().items()}
        provider = rt.GoogleRoutesProvider(SECRET, geo, opener=ok_opener)
        app = self.app(provider=provider)
        live = self.book(app)
        self.assertTrue(live['distance_verified'])
        self.assertEqual(live['distance_source'], rt.LIVE_SOURCE)
        self.assertTrue(any('live from Google Routes' in t for t in self.labels(app.summary.body)))
        provider._open = failing_opener
        app.search(); app.search()                                            # two failing searches
        self.assertFalse(app.route.verified)
        warning = [t for t in self.labels(app.summary.body) if 'ESTIMATED' in t]
        self.assertTrue(warning)
        self.assertNotIn(SECRET, ' '.join(warning))
        estimate = self.book(app, 'Centrala', 'Marend')
        self.assertFalse(estimate['distance_verified'])
        provider._open = ok_opener
        app.search()
        admin = sign_in(app.store)
        kinds = [r['kind'] for r in reversed(app.store.read_audit(admin, app.clock()))]
        self.assertEqual(kinds.count('routing.live_failed'), 1)
        self.assertEqual(kinds.count('routing.live_restored'), 1)
        self.assertNotIn(SECRET, json.dumps(app.store.read_audit(admin, app.clock())))

    def test_the_route_on_screen_is_the_route_that_was_priced_and_stored(self):
        app = self.app()
        app.from_pick.set('Zord'); app.to_pick.set('Marend'); app.search()
        shown = app.route
        chip = [t for t in self.labels(app.summary.body) if 'charged' in t][0]
        rec = self.book(app, 'Zord', 'Marend')
        self.assertEqual(rec['route'], list(shown.stations))
        self.assertEqual(chip, f"{rec['zones']} zone{'' if rec['zones'] == 1 else 's'} charged")
        self.assertEqual(rec['total_cents'], fares.quote('Zord', 'Marend', {'Adult': 1}, route=shown).total_cents)

    def test_the_routing_choice_is_part_of_the_recorded_configuration(self):
        app = self.app()
        rows = app.store.read_audit(sign_in(app.store), app.clock())
        routing = [r for r in rows if r['kind'] == 'config.recorded' and r['target'] == 'routing']
        self.assertEqual(len(routing), 1)
        self.assertIn('schematic:estimated:default:ok', routing[0]['detail'])


if __name__ == '__main__':
    unittest.main()
