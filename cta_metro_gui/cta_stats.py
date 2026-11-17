"""Numbers for the admin portal. Pure functions over the saved bookings, so they are unit-tested without a GUI.

Counting rules (also shown in the README):
* A *booking* is one reference. A *passenger* is one person on a booking. A *ticket* is one journey leg
  for one passenger, so a return booking gives every passenger two tickets.
* Period filters use the time the booking was made.
* Zone flows count tickets (journey legs). A return ticket counts in the reverse direction too.
"""
from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import cta_core as core
import cta_payments as pay
from cta_routing import ZONES

PERIODS = ('Today', 'This week', 'This month', 'This year', 'All time')
ZONE_ORDER = ('Central', 'Midtown', 'Downtown')


def _dt(text: str, fmt: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text, fmt)
    except (TypeError, ValueError):
        return None


def issued_at(rec: dict) -> Optional[datetime]:
    return _dt(rec.get('issued_at', ''), '%Y-%m-%d %H:%M:%S')


def depart_at(rec: dict) -> Optional[datetime]:
    return _dt(rec.get('depart_at', ''), '%Y-%m-%d %H:%M')


def period_start(period: str, now: datetime) -> Optional[datetime]:
    if period not in PERIODS:
        raise ValueError(f'Unknown report period. Choose one of: {", ".join(PERIODS)}.')
    if not isinstance(now, datetime):
        raise ValueError('The report time is not a date and time.')
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'Today':
        return midnight
    if period == 'This week':                       # weeks start on Monday
        return midnight - timedelta(days=midnight.weekday())
    if period == 'This month':
        return midnight.replace(day=1)
    if period == 'This year':
        return midnight.replace(month=1, day=1)
    return None                                     # All time


def filter_period(history: Iterable[dict], period: str, now: datetime) -> List[dict]:
    start = period_start(period, now)
    out = []
    for rec in history:
        t = issued_at(rec)
        if t is None:
            continue
        if start is None or (start <= t <= now + timedelta(minutes=1)):
            out.append(rec)
    return out


def ticket_rows(history: Iterable[dict]) -> List[dict]:
    """One row per journey leg per passenger: what the admin tables and the CSV show."""
    rows: List[dict] = []
    for rec in history:
        payment = core.payment_of(rec)
        legs = [('out', rec.get('from_station', ''), rec.get('to_station', ''), list(rec.get('route', [])))]
        if rec.get('legs', 1) == 2:
            legs.append(('return', rec.get('to_station', ''), rec.get('from_station', ''), list(reversed(rec.get('route', [])))))
        for p in core.passengers_of(rec):
            for leg, a, b, path in legs:
                # Only ever show a ticket number that really exists on the record. A two-leg booking whose return
                # ticket was never written (damaged or part-migrated data) used to get an invented '<number>R',
                # which staff could select but never check in.
                ticket_no = p['ticket_no'] if leg == 'out' else p.get('return_ticket_no')
                if not ticket_no:
                    continue
                pre = '' if leg == 'out' else 'return_'
                status = p.get(pre + 'status', 'issued')
                boarded = p.get(pre + 'boarded_at', '')
                rows.append({
                    'reference': rec.get('reference', ''), 'ticket_no': ticket_no, 'passenger': p['name'],
                    'category': p['category'], 'phone': rec.get('account_phone', ''), 'leg': leg,
                    'from': a, 'to': b, 'from_zone': ZONES.get(a, ''), 'to_zone': ZONES.get(b, ''),
                    'path': path, 'booked_at': rec.get('issued_at', ''),
                    'departs': rec.get('depart_at', ''), 'leg_fare_cents': p.get('leg_fare_cents', p['fare_cents']),
                    'payment_method': payment.get('label', ''), 'method': payment.get('method', ''),
                    'payment_detail': payment.get('detail', ''),
                    'payment_status': payment.get('status', ''),
                    'gateway': payment.get('gateway', ''), 'status': status, 'boarded_at': boarded,
                    'refund_state': p.get(pre + 'refund_state', ''),
                    'cancelled_fare_cents': (p.get('leg_fare_cents', p['fare_cents'])
                                             if status in ('cancelled', 'refunded') else 0),
                })
    return rows


# ---------------------------------------------------------------- admin filters
# (key, label) pairs shown in the drop-downs. The first entry of each means "no filter".
STATUS_FILTERS = (('any', 'All tickets'), ('not_used', 'Not used yet'), ('checked_in', 'Checked in'),
                  ('paid', 'Paid'), ('unpaid', 'Not paid yet (counter)'), ('payment_problem', 'Payment failed / unconfirmed'),
                  ('cancelled', 'Cancelled'), ('refund_due', 'Refund to pay out'), ('refunded', 'Refunded'))
METHOD_FILTERS = (('any', 'Every payment method'),) + tuple((m[0], m[1]) for m in pay.METHODS)


def ticket_state(row: dict) -> set:
    """Every filter key a ticket row belongs to. A ticket can be in several (e.g. paid AND refunded)."""
    keys = {'any'}
    st = row.get('status', 'issued')
    if st == 'boarded':
        keys.add('checked_in')
    elif st == 'issued':
        keys.add('not_used')
    elif st in ('cancelled', 'refunded'):
        keys.add('cancelled')
        if st == 'refunded' or row.get('refund_state') == 'done':
            keys.add('refunded')
        elif row.get('refund_state') == 'due':
            keys.add('refund_due')
    ps = row.get('payment_status', '')
    if ps == pay.PAID:
        keys.add('paid')
    elif ps == pay.PENDING_COUNTER:
        keys.add('unpaid')
    elif ps in (pay.FAILED, pay.PENDING, pay.UNKNOWN, pay.CANCELLED):
        keys.add('payment_problem')
    return keys


def search_text(row: dict) -> str:
    """Everything a person might type into the search box about one ticket, lower-case."""
    state = ' '.join(sorted(ticket_state(row) - {'any'})).replace('_', ' ')
    parts = [row.get(k, '') for k in ('ticket_no', 'reference', 'passenger', 'phone', 'from', 'to', 'category',
                                      'payment_method', 'method', 'status', 'leg')]
    parts.append(pay.status_text({'status': row.get('payment_status', ''), 'gateway': row.get('gateway', '')}))
    parts.append(state)
    return ' '.join(str(x) for x in parts).lower()


def filter_rows(rows: Iterable[dict], text: str = '', status: str = 'any', method: str = 'any') -> List[dict]:
    """Rows matching ALL of: every word typed in ``text`` (anywhere in the row, so 'tamara', 'refund' or 'cash' work),
    the chosen status and the chosen payment method. Unknown filter keys raise ValueError (never silently show all)."""
    if status not in dict(STATUS_FILTERS):
        raise ValueError('Unknown ticket filter.')
    if method not in dict(METHOD_FILTERS):
        raise ValueError('Unknown payment-method filter.')
    words = [w for w in (text or '').lower().split() if w]
    out = []
    for r in rows:
        if status != 'any' and status not in ticket_state(r):
            continue
        if method != 'any' and r.get('method') != method:
            continue
        hay = search_text(r)
        if all(w in hay for w in words):
            out.append(r)
    return out


def summarize(records: List[dict], environment: Optional[str] = None) -> dict:
    env = environment or pay.current_environment()
    people: Counter = Counter()
    revenue = collected = to_collect = unconfirmed = test_mode = refund_due = cancelled_tickets = 0
    by_method: Counter = Counter()
    by_method_cents: Counter = Counter()
    tickets = boarded = returns = 0
    for rec in records:
        total = rec.get('total_cents', 0)
        payment = core.payment_of(rec)
        legs = [(p, leg) for p in core.passengers_of(rec) if not p.get('legacy')
                for leg in (('out', 'return') if rec.get('legs', 1) == 2 else ('out',))]
        cancelled = sum(p.get('leg_fare_cents', p['fare_cents']) for p, leg in legs
                        if p.get('return_status' if leg == 'return' else 'status') in ('cancelled', 'refunded'))
        owed = sum(p.get(('return_' if leg == 'return' else '') + 'refund_cents', 0) or 0 for p, leg in legs
                   if p.get(('return_' if leg == 'return' else '') + 'refund_state') == 'due')
        net = total - cancelled
        revenue += net
        if pay.is_settled(payment, env):                 # simulated payments are never real money in production
            collected += total - int(payment.get('refunded_cents', 0))
            refund_due += owed
        elif payment.get('status') == pay.PENDING_COUNTER:
            to_collect += payment.get('amount_cents', total)     # already reduced if some journeys were cancelled
        elif payment.get('status') in pay.UNCONFIRMED:
            unconfirmed += total
        if payment.get('status') == pay.PAID and pay.is_simulated(payment):
            test_mode += net
        by_method[payment.get('label', '?')] += 1
        by_method_cents[payment.get('label', '?')] += net
        if rec.get('legs', 1) == 2:
            returns += 1
        for p in core.passengers_of(rec):
            people[p['category'] if not p.get('legacy') else 'Unnamed'] += 1
    for row in ticket_rows(records):
        if row['status'] in ('cancelled', 'refunded'):   # cancelled journeys are not tickets that can still be used
            cancelled_tickets += 1
            continue
        tickets += 1
        boarded += row['status'] == 'boarded'
    return {'bookings': len(records), 'passengers': sum(people.values()), 'tickets': tickets,
            'return_bookings': returns, 'revenue_cents': revenue, 'collected_cents': collected,
            'to_collect_cents': to_collect, 'unconfirmed_cents': unconfirmed, 'test_mode_cents': test_mode,
            'refund_due_cents': refund_due, 'cancelled_tickets': cancelled_tickets,
            'boarded': boarded, 'not_boarded': tickets - boarded,
            'by_category': dict(people), 'by_method': dict(by_method), 'by_method_cents': dict(by_method_cents)}


def zone_matrix(records: List[dict]) -> Dict[Tuple[str, str], int]:
    """Tickets from the zone of the start station to the zone of the end station."""
    out: Counter = Counter()
    for row in ticket_rows(records):
        if row['from_zone'] and row['to_zone']:
            out[(row['from_zone'], row['to_zone'])] += 1
    return dict(out)


def zone_crossings(records: List[dict]) -> Dict[Tuple[str, str], int]:
    """Every time a journey moves from one zone into the next, e.g. Central -> Midtown, counted per ticket."""
    out: Counter = Counter()
    for row in ticket_rows(records):
        seq = [ZONES.get(s) for s in row['path']]
        for a, b in zip(seq, seq[1:]):
            if a and b and a != b:
                out[(a, b)] += 1
    return dict(out)


def top_routes(records: List[dict], n: int = 5) -> List[Tuple[str, str, int]]:
    c: Counter = Counter()
    for row in ticket_rows(records):
        c[(row['from'], row['to'])] += 1
    return [(a, b, k) for (a, b), k in c.most_common(n)]


def daily_counts(records: List[dict], days: int, now: datetime) -> List[Tuple[date, int]]:
    """Passengers booked per day for the last ``days`` days (oldest first)."""
    c: Counter = Counter()
    for rec in records:
        t = issued_at(rec)
        if t:
            c[t.date()] += len(core.passengers_of(rec))
    start = now.date() - timedelta(days=days - 1)
    return [(start + timedelta(days=i), c.get(start + timedelta(days=i), 0)) for i in range(days)]


def hourly_counts(records: List[dict]) -> List[int]:
    out = [0] * 24
    for rec in records:
        t = issued_at(rec)
        if t:
            out[t.hour] += len(core.passengers_of(rec))
    return out


def no_shows(history: Iterable[dict], now: datetime) -> List[dict]:
    """Tickets whose departure time has passed without anyone boarding. The admin phones these passengers."""
    out = []
    for row in ticket_rows(history):
        dep = _dt(row['departs'], '%Y-%m-%d %H:%M')
        if row['status'] == 'issued' and dep and dep < now:
            out.append(row)
    return sorted(out, key=lambda r: r['departs'], reverse=True)


def rows_to_csv(rows: List[dict]) -> str:
    cols = ['ticket_no', 'reference', 'passenger', 'category', 'phone', 'leg', 'from', 'to', 'booked_at', 'departs',
            'leg_fare_cents', 'payment_method', 'payment_status', 'gateway', 'status', 'refund_state', 'boarded_at']
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
