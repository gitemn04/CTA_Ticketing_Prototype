"""The one place where CTA fares are calculated (no GUI code here).

STATUS: the rule below is the rule the app has always used. It has NOT been confirmed as the official CTA fare
policy - see docs/FARE_POLICY.md for the open questions. Do not change it without written approval from the project
owner; a change must also bump FARE_POLICY_VERSION and regenerate fare_matrix.csv (tools/fare_matrix.py), and the
tests will fail until both are done.

Current rule ("band", policy CTA-FARES-2)
-----------------------------------------
* Zones: Central = band 1, Midtown = band 2, Downtown = band 3 (station membership in cta_routing.ZONE_NAMES).
* Chargeable zones = |band(origin) - band(destination)| + 1, so 1, 2 or 3. The track actually travelled is NOT used.
* One-way fare per passenger = category rate x chargeable zones.
  Rates (cents): Adult 2105, Child 1410, Senior 1025, Student 1750.
* Return = two one-way journeys (x 2), no return discount. The way back has the same zones (the rule is symmetric).
* Money is whole cents and only ever multiplied by whole numbers, so no rounding happens anywhere. Rates that are not a
  whole, positive number of cents are rejected. A rounding rule must be agreed before any percentage-based fare.
* No minimum or maximum fare beyond what the rule gives (one-way: 1,025 cents Senior 1 zone up to 6,315 Adult 3 zones).
* Not a journey: the same station as start and end, unknown stations, or a pair with no track between them.

The alternative "route" rule (distinct zones the train passes through) is kept ONLY so the difference can be reported
(tools/fare_matrix.py --compare). No booking can use it unless the policy itself is changed.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

import cta_routing as rt

FARE_POLICY_VERSION = 'CTA-FARES-2'
FARE_POLICY_STATUS = 'unconfirmed'               # becomes 'approved' only when the project owner signs it off
CATEGORIES: Tuple[str, ...] = ('Adult', 'Child', 'Senior', 'Student')
FARE_RATES_CENTS: Dict[str, int] = {'Adult': 2105, 'Child': 1410, 'Senior': 1025, 'Student': 1750}
ACTIVE_RULE = 'band'
RULES = ('band', 'route')                        # 'route' exists for comparison reports only
LEGS_ALLOWED = (1, 2)                            # one way, return
MATRIX_FILE = Path(__file__).with_name('fare_matrix.csv')
MATRIX_COLUMNS = ('from', 'to', 'from_zone', 'to_zone', 'zones') + tuple(c.lower() for c in CATEGORIES)


class FareError(ValueError):
    """A journey or passenger list that cannot be priced. ``str(exc)`` is safe to show to a passenger."""


class FarePolicyError(Exception):
    """The fare policy itself is missing or invalid: no fare may be charged until it is fixed."""


# ------------------------------------------------------------------ zone rules
def band_zones(origin: str, destination: str) -> int:
    return abs(rt.ZONE_BAND[rt.ZONES[origin]] - rt.ZONE_BAND[rt.ZONES[destination]]) + 1


def route_zones(route: rt.Route) -> int:
    return len(set(route.zones_entered))


def zones_by_rule(rule: str, origin: str, destination: str, route: Optional[rt.Route] = None) -> int:
    if rule == 'band':
        return band_zones(origin, destination)
    if rule == 'route':
        return route_zones(route if route is not None else rt.astar(origin, destination))
    raise FarePolicyError(f'Unknown fare rule {rule!r}.')


# ------------------------------------------------------------------ policy validation
def validate_policy(rates: Optional[Mapping[str, int]] = None, rule: Optional[str] = None) -> None:
    """Raise FarePolicyError unless the rates, zones and rule form a complete, usable policy."""
    rates = FARE_RATES_CENTS if rates is None else rates
    rule = ACTIVE_RULE if rule is None else rule
    if rule not in RULES:
        raise FarePolicyError(f'The fare rule {rule!r} is not a known rule.')
    if set(rates) != set(CATEGORIES):
        raise FarePolicyError('Fare rates must be given for exactly these passenger types: ' + ', '.join(CATEGORIES) + '.')
    for cat in CATEGORIES:
        v = rates[cat]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise FarePolicyError(f'The {cat} rate must be a whole, positive number of cents.')
    unzoned = sorted(set(rt.STATIONS) - set(rt.ZONES))
    if unzoned:
        raise FarePolicyError(f'{len(unzoned)} station(s) have no fare zone: ' + ', '.join(unzoned[:5]) + '.')
    stray = sorted(set(rt.ZONES) - set(rt.STATIONS))
    if stray:
        raise FarePolicyError('The zone list names stations that do not exist: ' + ', '.join(stray[:5]) + '.')
    if set(rt.ZONE_NAMES) != set(rt.ZONE_BAND):
        raise FarePolicyError('Every fare zone needs exactly one band number.')
    bands = sorted(rt.ZONE_BAND.values())
    if any(not isinstance(b, int) or isinstance(b, bool) for b in bands) or bands != list(range(1, len(bands) + 1)):
        raise FarePolicyError('Zone bands must be the whole numbers 1, 2, 3... with no gaps or repeats.')
    dup = [s for z, ss in rt.ZONE_NAMES.items() for s in ss if sum(s in other for other in rt.ZONE_NAMES.values()) > 1]
    if dup:
        raise FarePolicyError('A station is in more than one zone: ' + ', '.join(sorted(set(dup))[:5]) + '.')


# ------------------------------------------------------------------ the quote
@dataclass(frozen=True)
class CategoryCharge:
    category: str
    passengers: int
    rate_cents: int
    zones: int

    @property
    def total_cents(self) -> int:
        return self.passengers * self.rate_cents * self.zones


@dataclass(frozen=True)
class FareQuote:
    """Everything needed to show, charge and record a fare. Built only by ``quote``."""
    origin: str
    destination: str
    zones: int
    legs: int
    lines: Tuple[CategoryCharge, ...]
    policy_version: str
    rule: str

    @property
    def one_way_cents(self) -> int:
        return sum(line.total_cents for line in self.lines)

    @property
    def total_cents(self) -> int:
        return self.one_way_cents * self.legs

    def leg_fare(self, category: str) -> int:
        """One passenger, one journey."""
        return FARE_RATES_CENTS[category] * self.zones

    def passenger_fare(self, category: str) -> int:
        """One passenger, all journeys of the booking."""
        return self.leg_fare(category) * self.legs

    def summary(self) -> str:
        times = ' × 2 journeys' if self.legs == 2 else ''
        return '\n'.join(f'{c.category}: {c.passengers} × {c.rate_cents:,} cents × {c.zones} zone(s){times} = '
                         f'{c.total_cents * self.legs:,} cents' for c in self.lines if c.passengers)


def validate_counts(counts: Mapping[str, int], max_per_category: int = 99) -> None:
    if not isinstance(counts, Mapping):
        raise FareError('Choose the travellers first.')
    for name, n in counts.items():
        if name not in FARE_RATES_CENTS:
            raise FareError(f'Unknown passenger type: {name}.')
        if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > max_per_category:
            raise FareError(f'{name}: enter a whole number from 0 to {max_per_category}.')
    if sum(counts.values()) == 0:
        raise FareError('Add at least one traveller.')


def check_journey(origin: str, destination: str, route: Optional[rt.Route] = None) -> rt.Route:
    """The route for a valid journey, or FareError. A supplied route must really join these two stations."""
    for label, s in (('start', origin), ('destination', destination)):
        if not isinstance(s, str) or s not in rt.STATIONS:
            raise FareError(f'Choose a valid CTA station as the {label}.')
        if s not in rt.ZONES:
            raise FareError(f'{s} has no fare zone, so it cannot be priced.')
    if origin == destination:
        raise FareError('The start and destination are the same station.')
    if route is None:
        try:
            route = rt.astar(origin, destination)
        except ValueError:
            raise FareError('There is no route between these stations.') from None
    stations = tuple(route.stations)
    if len(stations) < 2 or stations[0] != origin or stations[-1] != destination:
        raise FareError('The route does not match the chosen stations.')
    for a, b in zip(stations, stations[1:]):
        if b not in rt.GRAPH.get(a, {}):
            raise FareError('The route uses a link that does not exist on the network.')
    if tuple(route.zones_entered) != rt._zone_sequence(list(stations)):
        raise FareError('The zones shown for this route do not match its stations.')     # what is shown = what is priced
    import math
    for value in (route.km, route.ride_minutes):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise FareError('The route has an impossible distance or travel time.')
    return route


def quote(origin: str, destination: str, counts: Mapping[str, int], legs: int = 1,
          route: Optional[rt.Route] = None) -> FareQuote:
    """THE fare. Every screen, record, payment, receipt and report uses the result of this function."""
    validate_policy()
    if legs not in LEGS_ALLOWED or isinstance(legs, bool):
        raise FareError('A booking is one way or return.')
    validate_counts(counts)
    route = check_journey(origin, destination, route)
    zones = zones_by_rule(ACTIVE_RULE, origin, destination, route)
    lines = tuple(CategoryCharge(c, counts.get(c, 0), FARE_RATES_CENTS[c], zones) for c in CATEGORIES)
    return FareQuote(origin, destination, zones, legs, lines, FARE_POLICY_VERSION, ACTIVE_RULE)


# ------------------------------------------------------------------ the booking must match the quote
def fare_problems(record: dict) -> List[str]:
    """Reasons a stored booking does not match its own fare (empty list = consistent).

    Checks that the passengers' fares add up to the total, that the payment is for the booked amount (an amount lowered
    by a cancellation keeps the original in ``original_amount_cents``), and - for bookings made under the current
    policy - that the stored zones and fares are what the policy gives for that journey."""
    out: List[str] = []
    total = record.get('total_cents')
    pax = record.get('passengers') or []
    if pax and sum(p.get('fare_cents', 0) for p in pax) != total:
        out.append('the passengers\' fares do not add up to the booking total')
    pay = record.get('payment')
    if isinstance(pay, dict) and pay.get('method') != 'legacy':
        charged = pay.get('original_amount_cents', pay.get('amount_cents'))
        if charged != total:
            out.append('the payment amount differs from the booking total')
    if record.get('fare_policy_version') == FARE_POLICY_VERSION and pax:
        try:
            counts = {c: n for c, n in (record.get('counts') or {}).items()}
            q = quote(record.get('from_station', ''), record.get('to_station', ''), counts, record.get('legs', 1))
        except (FareError, FarePolicyError) as exc:
            return out + [f'the journey cannot be priced any more ({exc})']
        if record.get('zones') != q.zones:
            out.append('the stored zones differ from the fare policy')
        if total != q.total_cents:
            out.append('the stored total differs from the fare policy')
        for p in pax:
            if p.get('category') in FARE_RATES_CENTS and p.get('fare_cents') != q.passenger_fare(p['category']):
                out.append(f"passenger {p.get('no')} has a fare that differs from the fare policy")
                break
    return out


# ------------------------------------------------------------------ the station-to-station matrix (approval lock)
def matrix_rows(rule: str = ACTIVE_RULE) -> List[Dict[str, str]]:
    """One row per ordered pair of different stations: zones and one-way fare per passenger type."""
    rows = []
    for a in sorted(rt.STATIONS):
        for b in sorted(rt.STATIONS):
            if a == b:
                continue
            z = zones_by_rule(rule, a, b)
            row = {'from': a, 'to': b, 'from_zone': rt.ZONES[a], 'to_zone': rt.ZONES[b], 'zones': str(z)}
            row.update({c.lower(): str(FARE_RATES_CENTS[c] * z) for c in CATEGORIES})
            rows.append(row)
    return rows


def write_matrix(path: Optional[Path] = None, rule: str = ACTIVE_RULE) -> int:
    path = path or MATRIX_FILE
    from cta_storage import atomic_write_bytes
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MATRIX_COLUMNS, lineterminator='\n')
    w.writeheader()
    rows = matrix_rows(rule)
    w.writerows(rows)
    atomic_write_bytes(Path(path), buf.getvalue().encode('utf-8'))
    return len(rows)


def read_matrix(path: Optional[Path] = None) -> List[Dict[str, str]]:
    path = Path(path or MATRIX_FILE)
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise FarePolicyError(f'The fare matrix ({path.name}) is missing, so fares cannot be checked. '
                              'Bookings are paused until it is restored.') from None
    except (OSError, UnicodeDecodeError):
        raise FarePolicyError(f'The fare matrix ({path.name}) cannot be read. Bookings are paused.') from None
    reader = csv.DictReader(text.splitlines())
    if tuple(reader.fieldnames or ()) != MATRIX_COLUMNS:
        raise FarePolicyError(f'The fare matrix ({path.name}) has the wrong columns. Bookings are paused.')
    return list(reader)


def check_against_matrix(path: Optional[Path] = None) -> None:
    """Raise FarePolicyError unless the code's fares equal the matrix row for row. This is what stops a formula
    from being changed quietly: the matrix is the reviewed artefact, the code must agree with it."""
    validate_policy()
    want = {(r['from'], r['to']): r for r in read_matrix(path)}
    have = {(r['from'], r['to']): r for r in matrix_rows()}
    if set(want) != set(have):
        raise FarePolicyError('The fare matrix does not list exactly the station pairs of the network. '
                              'Bookings are paused until fares are reviewed.')
    for key, row in have.items():
        if want[key] != row:
            raise FarePolicyError(f'The fare for {key[0]} -> {key[1]} in the code differs from the fare matrix. '
                                  'Bookings are paused until the fare change is approved.')


def compare_rules() -> List[Tuple[str, str, int, int]]:
    """(from, to, zones by the active rule, zones by the other rule) for every pair where the two rules disagree."""
    other = 'route' if ACTIVE_RULE == 'band' else 'band'
    out = []
    for a in sorted(rt.STATIONS):
        for b in sorted(rt.STATIONS):
            if a != b:
                x, y = zones_by_rule(ACTIVE_RULE, a, b), zones_by_rule(other, a, b)
                if x != y:
                    out.append((a, b, x, y))
    return out
