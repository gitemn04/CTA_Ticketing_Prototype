"""CTA routing engine: stations, A* path-finding, distance, time and timetable.

The CTA brief (Appendix 1) is a *schematic* diagram of a fictional network.
It gives no GPS positions, track lengths, speeds or timetables, so:

* Station positions are diagram pixels, NOT geographic coordinates.
* 100 diagram pixels = 1 SIMULATED km (editable below).
* Train speed, dwell time and train frequency are labelled assumptions.

Because the diagram is a tree, there is exactly one track route between any
two stations. "Alternatives" are therefore offered as LATER TRAINS, not as
invented tracks.

What is real and what is estimated
----------------------------------
* The NETWORK (stations, track links, zones) comes from the brief's diagram and is the same in every mode. It is
  validated (``validate_network``); if it is broken, journey planning stops.
* DISTANCE and TIME are ESTIMATES from the diagram ("schematic" provider: 100 px = 1 km, 30 km/h) unless the "google"
  provider is configured with an API key and real coordinates for every station, in which case they are LIVE.
* The ROUTE (which stations) and therefore the FARE never depend on the provider.

Choosing a provider (``load_routing``)
-------------------------------------
cta_config.json: {"routing": {"provider": "schematic" | "google", "station_geo_file": "...", "travel_mode": "TRANSIT"}}
(the older top-level keys "provider", "google_api_key", "station_geo_file", "travel_mode" are still read; "simulated" is
the old name of "schematic"). The API key may also come from CTA_GOOGLE_API_KEY.

* development / testing: no choice made -> schematic estimates (labelled as such).
* production: a choice MUST be made; none -> journey planning is unavailable with a clear message.
* a provider that was chosen but cannot start (no key, missing/invalid coordinates, unknown mode) is an ERROR in every
  mode. It is never silently replaced by estimates.
* If Google fails for one search, that route is shown with ESTIMATED values, clearly labelled, and the health status
  becomes "degraded". Nothing is presented as live unless the live service answered (``Route.verified``).
"""
from __future__ import annotations

import heapq
import json
import math
import os
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_PATH = Path(__file__).with_name('cta_config.json')      # the settings file (tests point this elsewhere)
ESTIMATE_SOURCE = 'Simulated from Appendix 1 diagram'
LIVE_SOURCE = 'Google Routes API'
ROUTING_PROVIDERS = ('schematic', 'google')
TRAVEL_MODES = ('TRANSIT', 'DRIVE', 'WALK', 'BICYCLE', 'TWO_WHEELER')


class RoutingConfigError(Exception):
    """Routing cannot be used as configured. ``str(exc)`` is safe to show and never contains the API key."""


# --------------------------------------------------------------------------
# Network data (traced from Appendix 1)
# --------------------------------------------------------------------------
STATIONS: Dict[str, Tuple[int, int]] = {
    'Centrala': (604, 520), 'Yaen': (440, 405), 'Jaund': (440, 520),
    'Tallan': (440, 635), 'Rede': (630, 445), 'Bylyn': (770, 405),
    'Frestin': (770, 500), 'Soth': (881, 553), 'Lomil': (770, 635),
    'Ninia': (619, 625), 'Quthiel': (303, 363), 'Wicyt': (310, 461),
    'Riladia': (300, 554), 'Oloadus': (290, 690), 'Riclya': (619, 341),
    'Agralle': (874, 341), 'Docia': (1019, 347), 'Stonyam': (1018, 461),
    'Obelyn': (1013, 602), 'Garion': (899, 706), 'Ralith': (994, 677),
    'Sylas': (619, 708), 'Zord': (176, 370), 'Perinad': (169, 579),
    'Keivia': (176, 721), 'Erean': (354, 230), 'Brunad': (509, 259),
    'Marend': (1197, 299), 'Ryall': (1206, 419), 'Pryn': (1305, 507),
    'Ederif': (1207, 540), 'Holmer': (1108, 651),
    'Vertwall': (1198, 704), 'Ruril': (1291, 637),
    'Elyot': (724, 787), 'Adohad': (820, 820),
}
ZONE_NAMES = {
    'Central': ('Bylyn', 'Centrala', 'Frestin', 'Jaund', 'Lomil', 'Ninia', 'Rede', 'Soth', 'Tallan', 'Yaen'),
    'Midtown': ('Agralle', 'Docia', 'Garion', 'Obelyn', 'Oloadus', 'Quthiel', 'Ralith', 'Riclya', 'Riladia', 'Stonyam', 'Sylas', 'Wicyt'),
    'Downtown': ('Adohad', 'Brunad', 'Ederif', 'Elyot', 'Erean', 'Holmer', 'Keivia', 'Marend', 'Perinad', 'Pryn', 'Ruril', 'Ryall', 'Vertwall', 'Zord'),
}
ZONES: Dict[str, str] = {s: z for z, ss in ZONE_NAMES.items() for s in ss}
ZONE_BAND = {'Central': 1, 'Midtown': 2, 'Downtown': 3}
LINKS = (
    ('Centrala', 'Yaen'), ('Yaen', 'Quthiel'), ('Quthiel', 'Zord'), ('Yaen', 'Wicyt'),
    ('Centrala', 'Jaund'), ('Jaund', 'Riladia'), ('Riladia', 'Perinad'),
    ('Centrala', 'Tallan'), ('Tallan', 'Oloadus'), ('Oloadus', 'Keivia'),
    ('Centrala', 'Rede'), ('Rede', 'Riclya'), ('Riclya', 'Brunad'), ('Brunad', 'Erean'),
    ('Centrala', 'Bylyn'), ('Bylyn', 'Agralle'), ('Agralle', 'Docia'), ('Docia', 'Marend'),
    ('Agralle', 'Stonyam'), ('Stonyam', 'Ryall'), ('Ryall', 'Pryn'),
    ('Centrala', 'Frestin'), ('Frestin', 'Soth'), ('Soth', 'Obelyn'), ('Obelyn', 'Ederif'),
    ('Centrala', 'Lomil'), ('Lomil', 'Garion'), ('Garion', 'Ralith'),
    ('Ralith', 'Holmer'), ('Holmer', 'Vertwall'), ('Vertwall', 'Ruril'),
    ('Centrala', 'Ninia'), ('Ninia', 'Sylas'), ('Sylas', 'Elyot'), ('Elyot', 'Adohad'),
)

# --------------------------------------------------------------------------
# Labelled assumptions (edit here)
# --------------------------------------------------------------------------
PIXELS_PER_SIMULATED_KM = 100.0
TRAIN_SPEED_KMH = 30.0          # simulated
DWELL_MIN = 0.5                 # minutes stopped at each intermediate station
HEADWAY_MIN = 6                 # a train leaves every station every 6 minutes
FIRST_TRAIN = (5, 0)            # 05:00
SERVICE_SPAN = timedelta(hours=19)   # last train leaves before 00:00


def distance_km(a: str, b: str) -> float:
    x1, y1 = STATIONS[a]
    x2, y2 = STATIONS[b]
    return math.hypot(x2 - x1, y2 - y1) / PIXELS_PER_SIMULATED_KM


def validate_network(stations: Optional[Dict[str, Any]] = None,
                     links: Optional[Tuple[Tuple[str, str], ...]] = None) -> List[str]:
    """Every problem with the network data (empty list = usable): station names and positions, unknown or repeated
    links, self-links, impossible track lengths, and stations that cannot be reached."""
    stations = STATIONS if stations is None else stations
    links = LINKS if links is None else links
    problems: List[str] = []
    if not stations:
        return ['There are no stations.']
    seen_pos: Dict[Tuple[float, float], str] = {}
    placed: Dict[str, Tuple[float, float]] = {}        # stations whose position could be read
    for name, pos in stations.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            problems.append(f'Station name {name!r} is empty or has spaces around it.')
        try:
            x, y = float(pos[0]), float(pos[1])
            if len(pos) != 2 or not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError
        except (TypeError, ValueError, IndexError):
            problems.append(f'Station {name} has no valid position.')
            continue
        if (x, y) in seen_pos:
            problems.append(f'Stations {seen_pos[(x, y)]} and {name} are at the same position.')
        seen_pos[(x, y)] = name
        placed[name] = (x, y)
    pairs = set()
    adjacency: Dict[str, set] = {s: set() for s in stations}
    for link in links:
        if not isinstance(link, tuple) or len(link) != 2:
            problems.append(f'Track link {link!r} is not a pair of stations.')
            continue
        a, b = link
        if a not in stations or b not in stations:
            problems.append(f'Track link {a} - {b} names a station that does not exist.')
            continue
        if a == b:
            problems.append(f'Track link {a} - {b} joins a station to itself.')
            continue
        key = frozenset((a, b))
        if key in pairs:
            problems.append(f'Track link {a} - {b} is listed more than once.')
        pairs.add(key)
        adjacency[a].add(b)                        # the link exists whatever its length: do not also call the
        adjacency[b].add(a)                        # stations at its ends "unreachable"
        if a in placed and b in placed:            # an unreadable position is already reported above
            w = math.hypot(placed[a][0] - placed[b][0], placed[a][1] - placed[b][1]) / PIXELS_PER_SIMULATED_KM
            if not math.isfinite(w) or w <= 0:
                problems.append(f'Track link {a} - {b} has an impossible length.')
    start = next(iter(stations))
    reached, todo = {start}, [start]
    while todo:
        for nxt in adjacency[todo.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                todo.append(nxt)
    cut_off = sorted(set(stations) - reached)
    if cut_off:
        problems.append(f'{len(cut_off)} station(s) cannot be reached from {start}: ' + ', '.join(cut_off[:6]) + '.')
    return problems


def build_graph() -> Dict[str, Dict[str, float]]:
    graph: Dict[str, Dict[str, float]] = {s: {} for s in STATIONS}
    for a, b in LINKS:
        w = distance_km(a, b)
        graph[a][b] = w
        graph[b][a] = w
    return graph


GRAPH = build_graph()


# --------------------------------------------------------------------------
# Route objects
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Leg:
    a: str
    b: str
    km: float
    minutes: float


@dataclass(frozen=True)
class Route:
    stations: Tuple[str, ...]
    legs: Tuple[Leg, ...]
    km: float
    ride_minutes: float
    zones_entered: Tuple[str, ...]      # zone names in travel order, consecutive duplicates removed
    source: str = ESTIMATE_SOURCE
    note: str = ''
    verified: bool = False              # True ONLY when distance and time came from a live service for this route

    @property
    def origin(self) -> str:
        return self.stations[0]

    @property
    def destination(self) -> str:
        return self.stations[-1]

    @property
    def stops(self) -> int:
        """Number of station-to-station hops."""
        return len(self.legs)


def _zone_sequence(path: List[str]) -> Tuple[str, ...]:
    seq: List[str] = []
    for s in path:
        z = ZONES[s]
        if not seq or seq[-1] != z:
            seq.append(z)
    return tuple(seq)


def astar(start: str, destination: str) -> Route:
    """A* shortest path with a consistent straight-line heuristic."""
    if start not in GRAPH or destination not in GRAPH:
        raise ValueError('Select valid CTA stations for both endpoints.')
    frontier = [(distance_km(start, destination), 0.0, start)]
    best = {start: 0.0}
    prev: Dict[str, str] = {}
    while frontier:
        _, cost, node = heapq.heappop(frontier)
        if cost > best[node] + 1e-9:
            continue
        if node == destination:
            break
        for nxt, w in GRAPH[node].items():
            if not isinstance(w, (int, float)) or isinstance(w, bool) or not math.isfinite(w) or w <= 0:
                raise ValueError('The network data has an invalid track length; journeys cannot be planned.')
            new = cost + w
            if new + 1e-9 < best.get(nxt, math.inf):
                best[nxt] = new
                prev[nxt] = node
                heapq.heappush(frontier, (new + distance_km(nxt, destination), new, nxt))
    if destination not in best:
        raise ValueError('No route available between these stations.')
    path = [destination]
    while path[-1] != start:
        path.append(prev[path[-1]])
    path.reverse()
    legs = tuple(
        Leg(a, b, GRAPH[a][b], GRAPH[a][b] / TRAIN_SPEED_KMH * 60)
        for a, b in zip(path, path[1:])
    )
    km = sum(l.km for l in legs)
    ride = sum(l.minutes for l in legs) + max(0, len(path) - 2) * DWELL_MIN
    return Route(tuple(path), legs, km, ride, _zone_sequence(path))


# --------------------------------------------------------------------------
# Timetable (simulated)
# --------------------------------------------------------------------------
def next_departures(after: datetime, count: int = 4) -> List[datetime]:
    """The next `count` simulated departures at or after `after`."""
    base = after.replace(hour=FIRST_TRAIN[0], minute=FIRST_TRAIN[1], second=0, microsecond=0)
    if after <= base:
        t = base
    else:
        k = math.ceil((after - base).total_seconds() / (HEADWAY_MIN * 60) - 1e-9)
        t = base + timedelta(minutes=k * HEADWAY_MIN)
    out: List[datetime] = []
    while len(out) < count:
        if t - base >= SERVICE_SPAN:
            base += timedelta(days=1)
            t = base
        out.append(t)
        t += timedelta(minutes=HEADWAY_MIN)
    return out


def stop_times(route: Route, depart: datetime) -> List[Tuple[str, datetime, float]]:
    """(station, arrival time, cumulative km) for every station on the route."""
    rows = [(route.stations[0], depart, 0.0)]
    minutes = 0.0
    km = 0.0
    for i, leg in enumerate(route.legs):
        minutes += leg.minutes + (DWELL_MIN if i > 0 else 0)
        km += leg.km
        rows.append((leg.b, depart + timedelta(minutes=minutes), km))
    return rows


def round_minute(t: datetime) -> datetime:
    return (t + timedelta(seconds=30)).replace(second=0, microsecond=0)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
class SimulatedProvider:
    """Estimated distance and time from the network diagram. Never live, and always says so."""
    name = 'Simulated (schematic diagram)'
    kind = 'schematic'
    live = False

    def plan(self, a: str, b: str) -> Route:
        return astar(a, b)


@dataclass(frozen=True)
class RoutingHealth:
    """What the operator should be told about routing right now."""
    status: str            # 'ok' | 'degraded' | 'error'
    provider: str          # 'schematic' | 'google' | 'none'
    live: bool             # distances/times come from a live service
    explicit: bool         # chosen in the settings (False = development default)
    message: str

    def label(self) -> str:
        if self.status == 'error':
            return 'Journey planning unavailable'
        if self.live:
            return 'Live distances and times (Google Routes)' + (' - currently failing' if self.status == 'degraded' else '')
        return 'Estimated distances and times (network diagram, not live)'


class GoogleRoutesProvider:
    """Live distance/time from the Google Routes API for stations with REAL, verified coordinates.

    The route itself (which stations) still comes from the network, so fares never depend on Google. If a request fails
    or the answer is unusable, that route is returned with ESTIMATED values, ``verified=False`` and a note saying so, and
    ``health()`` reports 'degraded' until a request succeeds again."""
    name = 'Google Routes API'
    kind = 'google'
    live = True
    URL = 'https://routes.googleapis.com/directions/v2:computeRoutes'

    def __init__(self, api_key: str, geo: Dict[str, Tuple[float, float]],
                 mode: str = 'TRANSIT', timeout: float = 8.0, opener=urllib.request.urlopen):
        self.api_key, self.geo, self.mode = api_key, geo, mode
        self.timeout, self._open = timeout, opener
        self.last_failure = ''               # short, secret-free reason of the most recent failed request
        import cta_errors
        cta_errors.register_secret(api_key)

    def __repr__(self) -> str:               # never print the key
        return f'GoogleRoutesProvider(mode={self.mode!r}, stations={len(self.geo)})'

    def _query(self, a: str, b: str) -> Tuple[float, float]:
        def point(s):
            lat, lng = self.geo[s]
            return {'location': {'latLng': {'latitude': lat, 'longitude': lng}}}
        body = json.dumps({'origin': point(a), 'destination': point(b),
                           'travelMode': self.mode}).encode()
        req = urllib.request.Request(self.URL, data=body, method='POST', headers={
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': self.api_key,
            'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters'})
        with self._open(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        r = data['routes'][0]
        km = float(r['distanceMeters']) / 1000.0
        minutes = float(str(r['duration']).rstrip('s')) / 60.0
        if not (math.isfinite(km) and math.isfinite(minutes) and km > 0 and minutes > 0):
            raise ValueError('unusable distance or duration')
        return km, minutes

    def health(self) -> RoutingHealth:
        if self.last_failure:
            return RoutingHealth('degraded', 'google', True, True,
                                 f'Google Routes is failing ({self.last_failure}); estimated values are being shown.')
        return RoutingHealth('ok', 'google', True, True, 'Live distances and times from Google Routes.')

    def plan(self, a: str, b: str) -> Route:
        base = astar(a, b)
        if a == b or a not in self.geo or b not in self.geo:
            return replace(base, note='No real coordinates for these stations: distance and time are ESTIMATED '
                                      '(simulated from the network diagram), not live.')
        try:
            km, minutes = self._query(a, b)
        except Exception as exc:  # network, quota, bad key, bad JSON, unusable answer ...
            self.last_failure = type(exc).__name__                   # the class name only: no URL, key or body
            import cta_errors
            cta_errors.report(exc, 'Google Routes request', stations=f'{a} -> {b}')
            return replace(base, note=f'Live routing unavailable ({self.last_failure}): distance and time are '
                                      'ESTIMATED (simulated from the network diagram), not live.')
        self.last_failure = ''
        kf = km / base.km if base.km else 1.0
        mf = minutes / base.ride_minutes if base.ride_minutes else 1.0
        legs = tuple(Leg(l.a, l.b, l.km * kf, l.minutes * mf) for l in base.legs)
        return replace(base, legs=legs, km=km, ride_minutes=minutes, source=LIVE_SOURCE, verified=True)


def _read_geo(path: Path) -> Dict[str, Tuple[float, float]]:
    """Real coordinates for EVERY station, or RoutingConfigError naming what is wrong."""
    from cta_storage import StorageError, read_json
    try:
        raw = read_json(path, lambda: None, None, 'station coordinates')
    except StorageError as exc:
        raise RoutingConfigError(str(exc)) from None
    if raw is None:
        raise RoutingConfigError(f'The station coordinates file ({path.name}) is missing.')
    if not isinstance(raw, dict):
        raise RoutingConfigError(f'{path.name} must map each station name to [latitude, longitude].')
    unknown = sorted(set(raw) - set(STATIONS))
    if unknown:
        raise RoutingConfigError(f'{path.name} names stations that do not exist: ' + ', '.join(map(str, unknown[:5])) + '.')
    missing = sorted(set(STATIONS) - set(raw))
    if missing:
        raise RoutingConfigError(f'{path.name} has no coordinates for {len(missing)} station(s): '
                                 + ', '.join(missing[:5]) + '. Live routing needs every station.')
    geo: Dict[str, Tuple[float, float]] = {}
    seen: Dict[Tuple[float, float], str] = {}
    for name, v in raw.items():
        try:
            lat, lng = float(v[0]), float(v[1])
            ok = len(v) == 2 and math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        except (TypeError, ValueError, IndexError):
            ok = False
        if not ok:
            raise RoutingConfigError(f'{path.name}: the coordinates for {name} are not a valid [latitude, longitude].')
        if (lat, lng) in seen:
            raise RoutingConfigError(f'{path.name}: {seen[(lat, lng)]} and {name} have the same coordinates.')
        seen[(lat, lng)] = name
        geo[name] = (lat, lng)
    return geo


def load_routing(config_path: Optional[Path] = None, environment: Optional[str] = None):
    """(provider, RoutingHealth). Never raises and never swaps providers quietly: on an error the provider is None and
    the health message says exactly why, without secrets."""
    import cta_payments
    from cta_storage import StorageError, load_config
    path = config_path or CONFIG_PATH
    env = environment or cta_payments.current_environment()

    def error(msg: str) -> RoutingHealth:
        return RoutingHealth('error', 'none', False, False, msg)

    problems = validate_network()
    if problems:
        return None, error('The CTA network data is invalid: ' + problems[0])
    try:
        cfg = load_config(path)
    except StorageError as exc:
        return None, error(str(exc))
    section = cfg.get('routing', {})
    if not isinstance(section, dict):
        return None, error('The "routing" settings must be a JSON object.')
    choice = section.get('provider', cfg.get('provider'))
    if choice in (None, ''):
        if env == cta_payments.PRODUCTION:
            return None, error('Journey planning is not configured. Set "routing": {"provider": "schematic"} '
                               '(estimated distances and times from the network diagram) or "google" (live, needs '
                               'an API key and real station coordinates) in cta_config.json.')
        return SimulatedProvider(), RoutingHealth('ok', 'schematic', False, False,
                                                  f'{env} mode: estimated distances and times (network diagram).')
    choice = str(choice).strip().lower()
    choice = 'schematic' if choice == 'simulated' else choice
    if choice not in ROUTING_PROVIDERS:
        return None, error(f'Unknown routing provider "{choice[:30]}". Use "schematic" or "google".')
    if choice == 'schematic':
        return SimulatedProvider(), RoutingHealth('ok', 'schematic', False, True,
                                                  'Estimated distances and times from the network diagram (chosen in '
                                                  'the settings; not live).')
    key = os.environ.get('CTA_GOOGLE_API_KEY') or section.get('google_api_key') or cfg.get('google_api_key') or ''
    if not isinstance(key, str) or not key.strip():
        return None, error('Google routing is selected but no API key is set (CTA_GOOGLE_API_KEY or google_api_key).')
    mode = str(section.get('travel_mode', cfg.get('travel_mode', 'TRANSIT'))).upper()
    if mode not in TRAVEL_MODES:
        return None, error(f'Unknown travel mode "{mode[:20]}". Use one of: ' + ', '.join(TRAVEL_MODES) + '.')
    geo_name = section.get('station_geo_file', cfg.get('station_geo_file', 'stations_geo.json'))
    try:
        geo = _read_geo(path.parent / str(geo_name))
    except RoutingConfigError as exc:
        return None, error(f'Google routing is selected but cannot start: {exc}')
    provider = GoogleRoutesProvider(key.strip(), geo, mode=mode)
    return provider, provider.health()


def load_provider(config_path: Optional[Path] = None):
    """The configured provider, or RoutingConfigError (never a silent substitute)."""
    provider, health = load_routing(config_path)
    if provider is None:
        raise RoutingConfigError(health.message)
    return provider


def weights_csv() -> str:
    rows = ['Edge,Station A,Zone A,Station B,Zone B,Simulated km']
    for i, (a, b) in enumerate(LINKS, 1):
        rows.append(f'{i},{a},{ZONES[a]},{b},{ZONES[b]},{distance_km(a, b):.3f}')
    return '\n'.join(rows) + '\n'


if __name__ == '__main__':
    import sys
    a, b = (sys.argv[1:3] + ['Centrala', 'Zord'])[:2] if len(sys.argv) > 2 else ('Centrala', 'Zord')
    r = astar(a, b)
    print(' -> '.join(r.stations))
    print(f'{r.km:.2f} simulated km, {r.ride_minutes:.1f} simulated ride minutes')
