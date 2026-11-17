"""Zone-only booking (ATHE Unit 2, Appendix 2): station board, zone fare, issued voucher. No GUI code here.

The passenger chooses a START ZONE and a DESTINATION ZONE (not stations) and how many Adults, Children, Seniors and
Students are travelling. The app then issues a voucher.

Documented design assumption (zone-only flow)
---------------------------------------------
* Zone numbers: Central = 1, Midtown = 2, Downtown = 3 (the same numbers as ``cta_routing.ZONE_BAND``).
* Chargeable zones = abs(start zone number - destination zone number) + 1.
  So a journey inside one zone is 1 zone, adjacent zones are 2, Central <-> Downtown is 3.
* The same start and destination zone is allowed: a journey can stay inside one zone.
* Category subtotal = count x rate per zone (cents) x chargeable zones. Total = sum of the four subtotals.
* Rates (whole cents per passenger per chargeable zone) come from ``cta_fares.FARE_RATES_CENTS``:
  Adult 2,105 - Child 1,410 - Senior 1,025 - Student 1,750.

Zone-only input does not identify a route, so the track a train would really take is not (and cannot be) used here.
The optional station-to-station planner (``cta_fares.quote``) keeps its own checks.

Money is whole cents multiplied by whole numbers only: no rounding happens anywhere.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Tuple

import cta_fares as fares
import cta_routing as rt

ZONE_ORDER: Tuple[str, ...] = ('Central', 'Midtown', 'Downtown')
ZONE_NUMBER: Dict[str, int] = dict(rt.ZONE_BAND)                 # Central 1, Midtown 2, Downtown 3
CATEGORIES: Tuple[str, ...] = fares.CATEGORIES                     # Adult, Child, Senior, Student
CATEGORY_PLURAL: Dict[str, str] = {'Adult': 'Adults', 'Child': 'Children', 'Senior': 'Seniors', 'Student': 'Students'}
MAX_PER_CATEGORY = 99
RULE_TEXT = 'Chargeable zones = |start zone number - destination zone number| + 1 (Central 1, Midtown 2, Downtown 3)'


class ZoneBookingError(ValueError):
    """Input that cannot be booked. ``str(exc)`` is a clear message that is safe to show to the passenger."""


# ------------------------------------------------------------------ requirement 1: station board
def station_board() -> List[Tuple[str, List[str]]]:
    """[(zone, stations A-Z)] in the order Central, Midtown, Downtown, built from the verified network data."""
    return [(z, sorted(rt.ZONE_NAMES[z], key=str.casefold)) for z in ZONE_ORDER]


def network_counts() -> Tuple[int, int]:
    """(number of stations, number of track connections) of the network the board is built from."""
    return len(rt.STATIONS), len(rt.LINKS)


# ------------------------------------------------------------------ requirement 2: zones and counts
def chargeable_zones(start_zone: Optional[str], destination_zone: Optional[str]) -> int:
    if not start_zone and not destination_zone:
        raise ZoneBookingError('Choose a start zone and a destination zone.')
    if not start_zone:
        raise ZoneBookingError('Choose a start zone.')
    if not destination_zone:
        raise ZoneBookingError('Choose a destination zone.')
    for label, z in (('start', start_zone), ('destination', destination_zone)):
        if z not in ZONE_NUMBER:
            raise ZoneBookingError(f'Choose a valid {label} zone: Central, Midtown or Downtown.')
    return abs(ZONE_NUMBER[start_zone] - ZONE_NUMBER[destination_zone]) + 1


def parse_counts(raw: Mapping[str, object]) -> Dict[str, int]:
    """Turn what was typed into {category: count}. Every category must be a whole number from 0 to 99 and at least one
    traveller is needed. Raises ZoneBookingError with one clear message (the first problem found, in screen order)."""
    out: Dict[str, int] = {}
    for cat in CATEGORIES:
        label = CATEGORY_PLURAL[cat]
        value = raw.get(cat, 0)
        if isinstance(value, bool):
            raise ZoneBookingError(f'{label}: enter a whole number from 0 to {MAX_PER_CATEGORY}.')
        if isinstance(value, int):
            n = value
        else:
            text = str(value).strip()
            if text == '':
                raise ZoneBookingError(f'{label}: enter a number (use 0 if none are travelling).')
            if not (text.isascii() and text.isdigit()):
                raise ZoneBookingError(f'{label}: "{text[:10]}" is not a whole number. Enter 0 to {MAX_PER_CATEGORY}.')
            n = int(text)
        if n < 0 or n > MAX_PER_CATEGORY:
            raise ZoneBookingError(f'{label}: enter a whole number from 0 to {MAX_PER_CATEGORY}.')
        out[cat] = n
    if sum(out.values()) == 0:
        raise ZoneBookingError('Enter at least one traveller: all four counts are 0.')
    return out


# ------------------------------------------------------------------ requirement 3: the voucher
@dataclass(frozen=True)
class VoucherLine:
    category: str
    count: int
    rate_cents: int
    zones: int

    @property
    def label(self) -> str:
        return CATEGORY_PLURAL[self.category]

    @property
    def subtotal_cents(self) -> int:
        return self.count * self.rate_cents * self.zones


@dataclass(frozen=True)
class Voucher:
    """Everything the screen, the PDF and the print-out show. Built only by ``issue_voucher``."""
    number: str
    issued_at: datetime
    start_zone: str
    destination_zone: str
    zones: int
    lines: Tuple[VoucherLine, ...]               # always all four categories, zero counts included

    @property
    def travellers(self) -> int:
        return sum(line.count for line in self.lines)

    @property
    def total_cents(self) -> int:
        return sum(line.subtotal_cents for line in self.lines)

    @property
    def issued_text(self) -> str:
        return self.issued_at.strftime('%d %b %Y, %H:%M:%S')

    def as_dict(self) -> dict:
        """Plain data for the voucher log (no personal data: zones and counts only)."""
        return {'voucher': self.number, 'issued_at': self.issued_at.isoformat(timespec='seconds'),
                'start_zone': self.start_zone, 'destination_zone': self.destination_zone,
                'chargeable_zones': self.zones, 'travellers': self.travellers, 'total_cents': self.total_cents,
                'lines': [{'category': ln.category, 'count': ln.count, 'rate_cents': ln.rate_cents,
                           'subtotal_cents': ln.subtotal_cents} for ln in self.lines]}

    def text(self) -> str:
        """The voucher as plain text (used by tests and as a fallback)."""
        rows = [f'CTA Metro - zone fare voucher {self.number}',
                f'Issued: {self.issued_text}',
                f'From zone: {self.start_zone} ({ZONE_NUMBER[self.start_zone]})',
                f'To zone: {self.destination_zone} ({ZONE_NUMBER[self.destination_zone]})',
                f'Chargeable zones: {self.zones}']
        for ln in self.lines:
            rows.append(f'{ln.label}: {ln.count} x {ln.rate_cents:,} cents x {ln.zones} zone(s) = '
                        f'{ln.subtotal_cents:,} cents')
        rows += [f'Travellers: {self.travellers}', f'Total fare: {self.total_cents:,} cents']
        return '\n'.join(rows)


def new_voucher_number(now: datetime) -> str:
    return f"ZV-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


def issue_voucher(start_zone: Optional[str], destination_zone: Optional[str], raw_counts: Mapping[str, object],
                  now: datetime, number: Optional[str] = None) -> Voucher:
    """Validate everything, then build the voucher. Nothing is issued if any input is wrong."""
    zones = chargeable_zones(start_zone, destination_zone)
    counts = parse_counts(raw_counts)
    try:
        fares.validate_policy()                  # the same rates the station planner uses; never charge a broken policy
    except fares.FarePolicyError as exc:
        raise ZoneBookingError(f'Bookings are paused: {exc}') from None
    lines = tuple(VoucherLine(c, counts[c], fares.FARE_RATES_CENTS[c], zones) for c in CATEGORIES)
    return Voucher(number or new_voucher_number(now), now, str(start_zone), str(destination_zone), zones, lines)


def zone_matrix() -> List[Tuple[str, str, int]]:
    """Every (start zone, destination zone, chargeable zones) pair: 9 rows, shown on screen and in the README."""
    return [(a, b, chargeable_zones(a, b)) for a in ZONE_ORDER for b in ZONE_ORDER]
