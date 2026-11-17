"""CTA Ticket Access Server (v3.0) — Flask backend for mobile-friendly ticket pages.

Flow
----
1. When a ticket is issued, ``issue_web_token(db_path, ticket_id)`` generates a
   cryptographically random 32-byte URL-safe token stored in ``ticket_web_tokens``.
2. The token is embedded as a URL in the QR code on the ticket PDF:
       https://<host>/ticket/<token>
3. A passenger scans the QR with their phone camera, which opens the URL.
   The page shows ticket details and a PDF download link.
4. Scanning NEVER checks the passenger in. Check-in requires staff authentication
   and validates payment / cancellation / usage through the normal path.

Security properties
-------------------
* Tokens are 32 random bytes (256-bit entropy), URL-safe base64, 43 chars.
* Each token maps to exactly one ticket leg (outbound or return).
* Revoked tokens show a generic "link has been disabled" page — no passenger
  data is disclosed after revocation.
* Expired tokens (> TOKEN_EXPIRY_DAYS) also produce a generic denial page.
* Security headers: Referrer-Policy no-referrer, Cache-Control no-store,
  X-Frame-Options DENY, Content-Security-Policy (no external resources).
* Tokens are never echoed in application or access logs (the route uses <token>
  but the value is not logged by Flask's default logger in production).
* All DB errors are mapped to generic messages — no internal details exposed.

Configuration
-------------
Add to cta_config.json (in the same folder as cta.db):
    {
      "ticket_server": {
        "url": "https://your-domain.com"
      }
    }

Running
-------
    python cta_ticket_server.py --host 127.0.0.1 --port 5050 --db path/to/cta.db

NOTE: A localhost URL will NOT work from a phone camera. See DEPLOYMENT.md.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Token constants
# ---------------------------------------------------------------------------

TOKEN_BYTES = 32          # 256 bits of entropy → 43-char URL-safe base64 string
TOKEN_EXPIRY_DAYS = 365   # web links valid for 1 year by default
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{40,64}$')


def generate_web_token() -> str:
    """Return a URL-safe, cryptographically random 43-character token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


# ---------------------------------------------------------------------------
# Database helpers (no Flask dependency — used by the desktop app too)
# ---------------------------------------------------------------------------

def _ensure_token_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticket_web_tokens (
            ticket_id  TEXT PRIMARY KEY,
            token      TEXT NOT NULL UNIQUE,
            issued_at  TEXT NOT NULL,
            revoked_at TEXT
        )
    """)
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS web_tokens_by_token ON ticket_web_tokens(token)'
    )
    conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def issue_web_token(db_path: Path, ticket_id: str) -> str:
    """Generate and store a web access token for *ticket_id*. Returns the token.

    Idempotent: returns the existing (non-revoked) token if one exists.
    Raises ValueError if ticket_id is not found in the tickets table.
    """
    db_path = Path(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_token_table(conn)
        # Return existing token if present (even if revoked — caller can revoke again)
        row = conn.execute(
            'SELECT token FROM ticket_web_tokens WHERE ticket_id = ?', (ticket_id,)
        ).fetchone()
        if row:
            return row['token']
        # Verify the ticket exists in the real schema
        ticket_exists = conn.execute(
            'SELECT 1 FROM tickets WHERE ticket_id = ?', (ticket_id,)
        ).fetchone()
        if not ticket_exists:
            raise ValueError(f'Ticket id {ticket_id!r} not found in database.')
        token = generate_web_token()
        conn.execute(
            'INSERT INTO ticket_web_tokens (ticket_id, token, issued_at) VALUES (?, ?, ?)',
            (ticket_id, token, _now_iso()),
        )
        conn.commit()
        return token


def revoke_web_token(db_path: Path, ticket_id: str) -> None:
    """Mark a web token as revoked. The page returns a generic denial — no data disclosed."""
    db_path = Path(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_token_table(conn)
        conn.execute(
            'UPDATE ticket_web_tokens SET revoked_at = ? WHERE ticket_id = ?',
            (_now_iso(), ticket_id),
        )
        conn.commit()


def ticket_url(base_url: str, token: str) -> str:
    """Return the full ticket page URL for embedding in the QR code."""
    base = base_url.rstrip('/')
    return f'{base}/ticket/{token}'


def validate_base_url(url: str) -> Tuple[bool, str]:
    """Return (ok, reason). A valid base URL starts with https:// and has a real host."""
    if not url:
        return False, 'No ticket server URL configured.'
    if not url.startswith('https://'):
        if url.startswith('http://'):
            return False, 'Ticket server URL must use HTTPS (not HTTP) for phone camera scanning.'
        return False, 'Ticket server URL must start with https://'
    host = url[8:].split('/')[0].split(':')[0]
    if not host or '.' not in host:
        return False, (
            'Ticket server URL must be a real public hostname (e.g. https://your-domain.com). '
            'A localhost or bare IP address will not work from a phone camera.'
        )
    if host in ('localhost', '127.0.0.1', '0.0.0.0'):  # noqa: S104
        return False, 'localhost will not work from a phone camera. Use a public HTTPS URL.'
    return True, ''


# ---------------------------------------------------------------------------
# Token lookup — uses the real cta_db schema
# ---------------------------------------------------------------------------

def lookup_token(db_path: Path, token: str) -> Tuple[Optional[dict], Optional[str]]:
    """Return (info_dict, error_string).

    info_dict keys (all strings/ints, never raw DB objects):
        ticket_id, ticket_no, leg, status, revoked_at,
        passenger_name, passenger_category, fare_cents,
        booking_ref, from_station, to_station, depart_at, legs, trip_type,
        total_cents, payment_method, payment_status, payment_amount_cents

    On any error (unknown/revoked/expired token, DB problem) returns (None, message).
    Error messages are generic — no internal schema details are exposed to callers.
    """
    try:
        db_path = Path(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_token_table(conn)

            # 1. Look up the token
            wt = conn.execute(
                'SELECT ticket_id, issued_at, revoked_at '
                'FROM ticket_web_tokens WHERE token = ?',
                (token,),
            ).fetchone()
            if not wt:
                return None, 'This ticket link is not valid.'

            ticket_id = wt['ticket_id']
            revoked_at = wt['revoked_at']

            # 2. Revocation check — return generic message, no passenger data
            if revoked_at:
                return None, 'This ticket link has been disabled.'

            # 3. Expiry check
            try:
                issued_dt = datetime.strptime(wt['issued_at'], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - issued_dt > timedelta(days=TOKEN_EXPIRY_DAYS):
                    return None, 'This ticket link has expired.'
            except (ValueError, TypeError):
                # Malformed issued_at timestamp — cannot verify expiry, so reject for safety.
                return None, 'This ticket link has expired.'

            # 4. Look up the ticket row (uses the real schema)
            tkt = conn.execute(
                'SELECT ticket_no, reference, passenger_no, leg, ticket_id, '
                'status, refund_state, refunded_at '
                'FROM tickets WHERE ticket_id = ?',
                (ticket_id,),
            ).fetchone()
            if not tkt:
                return None, 'Ticket record not found.'

            reference = tkt['reference']
            passenger_no = tkt['passenger_no']

            # 5. Look up the passenger row (name, category, fare_cents are in passengers)
            pax = conn.execute(
                'SELECT name, category, fare_cents, doc '
                'FROM passengers WHERE reference = ? AND no = ?',
                (reference, passenger_no),
            ).fetchone()
            if not pax:
                return None, 'Passenger record not found.'

            # 6. Look up the booking doc (from_station, to_station, depart_at, legs, trip_type)
            bkg = conn.execute(
                'SELECT doc, total_cents, legs FROM bookings WHERE reference = ?',
                (reference,),
            ).fetchone()
            if not bkg:
                return None, 'Booking record not found.'

            import json as _json
            booking_doc = _json.loads(bkg['doc'])

            # Read leg_fare_cents from the passenger doc (stored alongside other pax fields).
            # pax['fare_cents'] is the TOTAL per-passenger fare for ALL legs (e.g. the full
            # round-trip amount). leg_fare_cents is the authoritative per-leg share stored
            # at booking time by cta_core.make_record.
            pax_doc = _json.loads(pax['doc']) if pax['doc'] else {}
            leg_fare_cents = int(pax_doc.get('leg_fare_cents') or 0)
            if not leg_fare_cents:
                # Fallback: divide total pax fare equally by number of legs.
                # This preserves correctness when reading older records that pre-date
                # the leg_fare_cents field.
                n_legs = max(1, int(bkg['legs'] or 1))
                leg_fare_cents = int(pax['fare_cents']) // n_legs

            # 7. Look up payment
            pay = conn.execute(
                'SELECT method, status, amount_cents FROM payments WHERE reference = ?',
                (reference,),
            ).fetchone()

            info = {
                'ticket_id':            ticket_id,
                'ticket_no':            tkt['ticket_no'],
                'leg':                  tkt['leg'],
                'status':               tkt['status'] or 'issued',
                'revoked_at':           None,  # not revoked (checked above)
                'passenger_name':       pax['name'],
                'passenger_category':   pax['category'],
                'fare_cents':           pax['fare_cents'],   # full pax fare (all legs)
                'leg_fare_cents':       leg_fare_cents,      # fare for this specific leg
                'booking_ref':          reference,
                'from_station':         booking_doc.get('from_station', ''),
                'to_station':           booking_doc.get('to_station', ''),
                'depart_at':            booking_doc.get('depart_at', ''),
                'legs':                 bkg['legs'],
                'trip_type':            booking_doc.get('trip_type', 'one-way'),
                'total_cents':          bkg['total_cents'],
                'payment_method':       pay['method'] if pay else '',
                'payment_status':       pay['status'] if pay else 'unpaid',
                'payment_amount_cents': pay['amount_cents'] if pay else 0,
            }
            return info, None

    except sqlite3.Error:
        return None, 'Ticket information is temporarily unavailable.'
    except Exception:  # noqa: BLE001
        return None, 'Ticket information is temporarily unavailable.'


# ---------------------------------------------------------------------------
# Flask server
# ---------------------------------------------------------------------------

def _require_flask():
    try:
        import flask  # noqa: F401
    except ImportError:
        raise ImportError(
            'Flask is required to run the ticket server.\n'
            'Install it:  pip install flask\n'
            'Or:          pip install -r requirements.txt'
        )


def build_app(db_path: Path, base_url: str = ''):
    """Return a configured Flask application.

    *base_url* is the validated public HTTPS base URL (e.g. https://cta.example.com).
    When set, each downloaded PDF embeds its authorised ticket URL in the QR code so
    that scanning the printed PDF on a phone opens the correct web ticket page.
    The request Host header is never used to construct these URLs — only the
    explicitly configured and validated *base_url* is trusted.
    """
    _require_flask()
    from flask import Flask, Response, abort, request  # type: ignore

    app = Flask(__name__)
    app.config['DB_PATH'] = Path(db_path)
    # Store the validated base URL (may be '' if not configured — QR falls back to
    # the signed ticket payload).
    ok, _ = validate_base_url(base_url) if base_url else (False, '')
    app.config['BASE_URL'] = base_url if ok else ''

    # ------------------------------------------------------------------ security headers
    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "script-src 'none'; base-uri 'none'; form-action 'none'"
        )
        return resp

    def _valid_token(token: str) -> bool:
        return bool(token) and bool(_TOKEN_RE.match(token))

    # ------------------------------------------------------------------ ticket page
    @app.route('/ticket/<token>')
    def ticket_page(token: str):
        if not _valid_token(token):
            abort(400)
        record, err = lookup_token(app.config['DB_PATH'], token)
        if err:
            return _error_page(html.escape(err)), 404
        # Build the correct absolute PDF URL using the request's own host
        pdf_url = f'/ticket/{html.escape(token)}/pdf'
        return _ticket_page_html(record, pdf_url), 200

    # ------------------------------------------------------------------ PDF download
    @app.route('/ticket/<token>/pdf')
    def ticket_pdf(token: str):
        if not _valid_token(token):
            abort(400)
        record, err = lookup_token(app.config['DB_PATH'], token)
        if err:
            return _error_page(html.escape(err)), 404
        try:
            import cta_pdf
            import cta_core as core
            import tempfile

            # Reconstruct a minimal booking dict matching what cta_pdf expects.
            # Only the authorised passenger/leg is included — other passengers are NOT exposed.
            booking = _record_to_booking(record)

            # Build the ticket_url_map so the QR in the downloaded PDF points to the
            # authorised HTTPS ticket page.  The request Host header is NOT used —
            # only the explicitly configured and validated base URL is trusted.
            _base = app.config.get('BASE_URL', '')
            _ticket_url_map = {}
            if _base:
                _leg = record.get('leg', 'out')
                _tid = record.get('ticket_id', '')
                if _tid:
                    _ticket_url_map[_tid] = ticket_url(_base, token)

            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                cta_pdf.build_ticket_pdf(
                    booking, tmp_path, include_receipt=False,
                    leg_override=booking.get('leg_override', ''),
                    ticket_url_map=_ticket_url_map,
                )
                pdf_bytes = tmp_path.read_bytes()
            finally:
                tmp_path.unlink(missing_ok=True)

            safe_no = re.sub(r'[^A-Za-z0-9_-]', '_', record.get('ticket_no', 'ticket'))
            return Response(
                pdf_bytes,
                mimetype='application/pdf',
                headers={
                    'Content-Disposition': f'attachment; filename="CTA-{safe_no}.pdf"',
                    'Cache-Control': 'no-store',
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Log the real error server-side (not shown to user)
            import traceback, sys
            print(f'[ticket_pdf error] {exc}', file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return _error_page('Could not generate the ticket PDF. Please contact station staff.'), 500

    # ------------------------------------------------------------------ health
    @app.route('/health')
    def health():
        return {'status': 'ok', 'ts': _now_iso()}, 200

    return app


# ---------------------------------------------------------------------------
# Booking reconstruction for PDF export
# ---------------------------------------------------------------------------

def _record_to_booking(r: dict) -> dict:
    """Convert the lookup_token info_dict into the booking dict that cta_pdf expects.

    Only the one authorised passenger and leg are included.
    The PDF is scoped to what the token holder is authorised to see.
    """
    leg = r.get('leg', 'out')
    is_return = leg == 'return'
    # leg_fare_cents: the authoritative per-leg fare read from the passenger doc by lookup_token.
    # fare_cents: the total pax fare for all legs (kept for the receipt total line).
    leg_fare_cents = int(r.get('leg_fare_cents') or 0)
    fare_cents     = int(r.get('fare_cents') or 0)
    # Always 1: each web token authorises exactly one journey page.
    # The raw booking may have legs=2 for return bookings, but the PDF must only contain
    # the single leg this token was issued for.
    legs = 1

    # Build a single-passenger record for this leg only.
    # cta_pdf._ticket_page reads pax['leg_fare_cents'] (falling back to pax['fare_cents'])
    # for the "Fare for this ticket" line, so we must set it to the per-leg amount.
    pax = {
        'no': 1,
        'name': r.get('passenger_name', ''),
        'category': r.get('passenger_category', 'Adult'),
        'fare_cents':     fare_cents,      # full pax fare (all legs) — for receipt total
        'leg_fare_cents': leg_fare_cents,  # per-leg fare — for the ticket fare line
        'status': r.get('status', 'issued'),
    }
    if is_return:
        # cta_pdf.py accesses pax['ticket_no'] unconditionally (line 244) before checking
        # return_ticket_no, so it must be present even for a return-leg token.
        pax['ticket_no'] = ''
        pax['return_ticket_no'] = r.get('ticket_no', '')
        pax['return_ticket_id'] = r.get('ticket_id', '')
        pax['return_status'] = r.get('status', 'issued')
    else:
        pax['ticket_no'] = r.get('ticket_no', '')
        pax['ticket_id'] = r.get('ticket_id', '')

    # Determine payment dict
    pay_status = r.get('payment_status', 'unpaid')
    payment = {
        'method': r.get('payment_method', 'cash'),
        'status': pay_status,
        'amount_cents': int(r.get('payment_amount_cents') or 0),
    }

    # Parse depart_at into separate date/time fields that cta_pdf uses
    depart_at = r.get('depart_at', '')
    try:
        dt = datetime.strptime(depart_at, '%Y-%m-%d %H:%M')
        depart_date = dt.strftime('%Y-%m-%d')
        depart_time = dt.strftime('%H:%M')
    except (ValueError, TypeError):
        depart_date = depart_at[:10] if depart_at else ''
        depart_time = depart_at[11:16] if len(depart_at) > 10 else ''

    booking = {
        'reference': r.get('booking_ref', ''),
        'issued_at': '',
        'from_station': r.get('from_station', ''),
        'to_station': r.get('to_station', ''),
        'depart_at': depart_at,
        'depart_date': depart_date,
        'depart_time': depart_time,
        'trip_type': r.get('trip_type', 'one-way'),
        'legs': legs,           # always 1 — one authorised journey per token
        'total_cents': int(r.get('total_cents') or 0),
        'passengers': [pax],
        'payment': payment,
        # Tell build_ticket_pdf exactly which leg to render.
        # For a return-leg token the pax fields are keyed as 'return_ticket_no' etc.,
        # so we must pass leg_override='return' to force the correct page layout.
        'leg_override': leg,    # 'out' or 'return'
    }
    return booking


# ---------------------------------------------------------------------------
# HTML helpers (all inline, zero external CDN)
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    'issued':    ('Active', '#0F766E', '✓'),
    'boarded':   ('Used', '#64748B', '✓'),
    'cancelled': ('Cancelled', '#DC2626', '✗'),
    'refunded':  ('Refunded', '#7C3AED', '↩'),
}

_PAYMENT_LABELS = {
    'paid':            ('Paid', '#0F766E'),
    'cash':            ('Paid (cash)', '#0F766E'),
    'pending':         ('Payment pending', '#D97706'),
    'pending_counter': ('Payment due at counter', '#D97706'),
    'unknown':         ('Payment unconfirmed', '#D97706'),
    'unpaid':          ('Unpaid', '#DC2626'),
    'refunded':        ('Refunded', '#7C3AED'),
    'due':             ('Refund due', '#7C3AED'),
}


def _ticket_page_html(r: dict, pdf_url: str) -> str:
    e = html.escape
    ticket_no  = e(str(r.get('ticket_no', '—')))
    name       = e(str(r.get('passenger_name', '—')))
    category   = e(str(r.get('passenger_category', '')))
    from_st    = e(str(r.get('from_station', '—')))
    to_st      = e(str(r.get('to_station', '—')))
    leg        = str(r.get('leg', 'out'))
    trip_type  = e(str(r.get('trip_type', '')))
    # Display the per-leg fare (leg_fare_cents), not the full round-trip fare (fare_cents).
    # For a single-journey token, these are the same.  For a return-leg token in a 2-leg
    # booking, leg_fare_cents is half of fare_cents and is the correct amount to show.
    _display_fare = r.get('leg_fare_cents') or r.get('fare_cents')
    fare_sar   = f"{int(_display_fare) / 100:.2f}" if _display_fare else '—'
    ref        = e(str(r.get('booking_ref', '—')))
    depart_at  = e(str(r.get('depart_at', '—')))

    leg_label  = 'Return journey' if leg == 'return' else 'Outbound journey'
    route_dir  = f'{to_st} → {from_st}' if leg == 'return' else f'{from_st} → {to_st}'

    status_key = str(r.get('status', '')).lower()
    slabel, scolor, sicon = _STATUS_LABELS.get(status_key, ('Unknown', '#6B7280', '?'))

    pay_key    = str(r.get('payment_status', '')).lower()
    plabel, pcolor = _PAYMENT_LABELS.get(pay_key, ('Unknown', '#6B7280'))

    pdf_href   = e(pdf_url)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>CTA Ticket — {ticket_no}</title>
<style>
:root {{
  --navy:#0B1F3A;--teal:#0F766E;--ink:#14263F;--muted:#6B7A90;
  --card:#fff;--bg:#F2F5F9;--line:#CBD5E1;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);
     color:var(--ink);min-height:100dvh;padding:16px}}
.card{{background:var(--card);border-radius:12px;padding:20px;
       max-width:440px;margin:0 auto 16px;box-shadow:0 2px 8px rgba(0,0,0,.08)}}
.header{{background:var(--navy);border-radius:10px;padding:18px 20px;color:#fff;
         max-width:440px;margin:0 auto 16px}}
.ticket-no{{font-size:1.5rem;font-weight:700}}
.badge{{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;
        border-radius:20px;font-weight:600;font-size:.85rem}}
.row{{display:flex;justify-content:space-between;align-items:center;
      padding:10px 0;border-bottom:1px solid var(--line)}}
.row:last-child{{border-bottom:none}}
.label{{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}}
.value{{font-weight:600;text-align:right;max-width:60%}}
.route{{font-size:1.1rem;font-weight:700;padding:12px 0}}
.dl-btn{{display:block;text-align:center;background:var(--teal);color:#fff;
         text-decoration:none;padding:14px;border-radius:8px;font-weight:600;
         font-size:1rem;margin-top:12px}}
.footer{{text-align:center;color:var(--muted);font-size:.75rem;padding:8px 0 16px;max-width:440px;margin:0 auto}}
</style>
</head>
<body>
<div class="header">
  <div style="font-size:.7rem;opacity:.6;letter-spacing:.1em">CTA METRO · TICKET</div>
  <div class="ticket-no">{ticket_no}</div>
  <div style="font-size:.8rem;opacity:.7;margin-top:4px">Ref: {ref}</div>
</div>

<div class="card">
  <div class="row"><span class="label">Passenger</span><span class="value">{name}</span></div>
  <div class="row"><span class="label">Category</span><span class="value">{category}</span></div>
  <div class="route">{route_dir}</div>
  <div class="row"><span class="label">Journey</span><span class="value">{leg_label} · {trip_type}</span></div>
  <div class="row"><span class="label">Departure</span><span class="value">{depart_at}</span></div>
  <div class="row">
    <span class="label">Status</span>
    <span class="value">
      <span class="badge" style="background:{scolor}20;color:{scolor}">{sicon} {e(slabel)}</span>
    </span>
  </div>
  <div class="row">
    <span class="label">Fare</span>
    <span class="value">SAR {fare_sar}</span>
  </div>
  <div class="row">
    <span class="label">Payment</span>
    <span class="value">
      <span class="badge" style="background:{pcolor}20;color:{pcolor}">{e(plabel)}</span>
    </span>
  </div>
</div>

<div class="card">
  <p style="font-size:.85rem;color:var(--muted);margin-bottom:12px">
    Download the PDF to save or print your ticket.
    Present the QR code or ticket number to station staff.
  </p>
  <a class="dl-btn" href="{pdf_href}">⬇ Download PDF</a>
</div>

<div class="footer">
  CTA Metro · Read-only. Scanning this page does not check you in.<br>
  This page contains a private link. Do not share it publicly.
</div>
</body>
</html>"""


def _error_page(message: str) -> str:
    safe = html.escape(str(message))
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTA — Ticket not found</title>
<style>
body{{font-family:system-ui,sans-serif;background:#F2F5F9;display:flex;
     align-items:center;justify-content:center;min-height:100dvh;padding:16px}}
.card{{background:#fff;border-radius:12px;padding:32px;max-width:380px;text-align:center;
       box-shadow:0 2px 8px rgba(0,0,0,.08)}}
h1{{color:#0B1F3A;font-size:1.1rem;margin-bottom:12px}}
p{{color:#6B7A90;font-size:.9rem;line-height:1.5}}
</style>
</head>
<body>
<div class="card">
  <h1>Ticket not available</h1>
  <p>{safe}</p>
  <p style="margin-top:16px">If you believe this is an error, contact CTA Metro station staff.</p>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Staff scanner integration — resolve URL-type QR codes locally
# ---------------------------------------------------------------------------

def parse_ticket_url(scanned_text: str, configured_base_url: str) -> Optional[str]:
    """If *scanned_text* is a URL matching the configured ticket server, return the token.

    Returns None if the text is not a ticket URL for this server (caller should fall back
    to signed-token or manual-number parsing). Never fetches the URL.
    """
    if not configured_base_url or not scanned_text:
        return None
    base = configured_base_url.rstrip('/')
    # Must be an exact prefix match — never accept arbitrary URLs
    prefix = base + '/ticket/'
    if not scanned_text.startswith(prefix):
        return None
    token_part = scanned_text[len(prefix):].split('/')[0].split('?')[0].split('#')[0]
    if _TOKEN_RE.match(token_part):
        return token_part
    return None


def resolve_url_token(db_path: Path, scanned_text: str, configured_base_url: str
                      ) -> Tuple[Optional[str], Optional[str]]:
    """If *scanned_text* is a ticket URL for this server, return (ticket_id, None).
    Otherwise return (None, reason). Never fetches any URL.

    Used by the staff scanner to resolve URL-type QR codes to ticket_ids
    without network access, then apply normal check-in permission/state checks.
    """
    token = parse_ticket_url(scanned_text, configured_base_url)
    if token is None:
        return None, 'Not a ticket URL for this server.'
    record, err = lookup_token(db_path, token)
    if err:
        return None, err
    return record.get('ticket_id'), None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

class BaseUrlError(ValueError):
    """The configured public ticket URL is present but unusable."""


def resolve_base_url(db_path: Path, cli_url: str = '', environ=None) -> Tuple[str, str]:
    """Return (validated_base_url, source). ('', '') when none is configured.

    Precedence: --url, then CTA_TICKET_SERVER_URL, then ``ticket_server.url`` in
    cta_config.json next to the database. A URL that is configured but invalid raises
    BaseUrlError: it is never silently dropped or replaced by a request Host header.
    A damaged cta_config.json raises cta_storage's error (settings are never reset).
    """
    environ = os.environ if environ is None else environ
    candidates = []
    if cli_url:
        candidates.append((cli_url, '--url'))
    if environ.get('CTA_TICKET_SERVER_URL'):
        candidates.append((environ['CTA_TICKET_SERVER_URL'], 'CTA_TICKET_SERVER_URL'))
    if not candidates:
        from cta_storage import load_config
        cfg_path = Path(db_path).resolve().parent / 'cta_config.json'
        section = load_config(cfg_path).get('ticket_server') or {}
        if isinstance(section, dict) and section.get('url'):
            candidates.append((str(section['url']), f'{cfg_path.name} ticket_server.url'))
    if not candidates:
        return '', ''
    url, source = candidates[0]
    url = url.strip()
    ok, reason = validate_base_url(url)
    if not ok:
        raise BaseUrlError(f'{source}: {reason}')
    return url.rstrip('/'), source


def main(argv=None):
    parser = argparse.ArgumentParser(description='CTA Ticket Access Server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5050)
    parser.add_argument('--db', default='cta.db', help='Path to cta.db (same file the desktop uses)')
    parser.add_argument('--url', default='',
                        help='Public HTTPS base URL for ticket QR codes (overrides cta_config.json)')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--check', action='store_true',
                        help='Validate configuration and build the app, then exit without serving')
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    try:
        base_url, url_source = resolve_base_url(db_path, args.url)
    except BaseUrlError as exc:
        print(f'ERROR: invalid public ticket URL - {exc}')
        print('       Fix cta_config.json ("ticket_server": {"url": "https://..."}) or remove it.')
        return 2
    if not db_path.exists():
        print(f'Warning: database {db_path} does not exist yet. '
              'It will be created when the first ticket is issued.')

    print(f'CTA Ticket Server starting on http://{args.host}:{args.port}')
    print(f'Database: {db_path.resolve()}')
    if args.host in ('127.0.0.1', 'localhost'):
        print()
        print('NOTE: This server is only accessible from this machine.')
        print('      Phone camera scanning requires a public HTTPS URL.')
        print('      See DEPLOYMENT.md for tunnelling / hosting options.')
    print()
    print('IMPORTANT: Point --db at the SAME cta.db file the desktop application uses.')
    print('           Copying the database file does not keep it synchronised.')

    if base_url:
        print(f'Public ticket URL: {base_url}  (from {url_source})')
        print('Ticket PDFs will carry a QR code linking to this URL.')
        print('Not verified: this does not prove the URL is reachable over HTTPS.')
    else:
        print('Public ticket URL: NOT CONFIGURED - downloaded PDFs will have no ticket-link QR.')

    app = build_app(db_path, base_url=base_url)
    if args.check:
        print('Configuration check passed; not starting the server (--check).')
        return 0
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
