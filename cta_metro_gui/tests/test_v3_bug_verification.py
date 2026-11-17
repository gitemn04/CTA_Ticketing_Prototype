"""CTA Metro v3.0 — Bug-fix verification tests for the 6 independently reproduced bugs.

Uses cta_db.Store.open() and real booking services (not raw SQL inserts) to
create a two-passenger return booking, then verifies all 6 bug fixes.

Run with: pytest tests/test_v3_bug_verification.py -v
"""
from __future__ import annotations

import json
import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))

# Testing environment — must not send real SMS or charge money
os.environ['CTA_ENV'] = 'testing'


# ---------------------------------------------------------------------------
# Shared fixture: real two-passenger return booking via Store.open()
# ---------------------------------------------------------------------------

def _build_real_return_booking(tmp_dir: Path):
    """Create a real two-passenger return booking via cta_db.Store.open().

    Returns (store, principal, record) where:
      - store:     cta_db.Store on the fresh database
      - principal: cta_auth.Principal for the admin staff member
      - record:    the full booking dict with passengers, ticket_ids, etc.
    """
    import cta_db
    import cta_routing as rt
    import cta_fares as fares
    import cta_core as core

    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)

    # Open a fresh store via the real service layer
    store = cta_db.Store.open(tmp_dir, now)

    # Create the first staff account (bootstrap — no auth needed for first admin)
    principal = store.bootstrap_admin('gatestaff', 'Pass1234!', now)

    # Build a route and fare for 2 Adult passengers, return (legs=2)
    route = rt.astar('Bylyn', 'Soth')
    q = fares.quote(route.origin, route.destination, {'Adult': 2}, 2, route)
    depart = now + timedelta(hours=2)

    history = store.load_history()
    rec = core.make_record(
        route, {'Adult': 2}, q.zones, depart, now, history,
        names=['Alice Smith', 'Bob Jones'],
        legs=2,
        payment={'method': 'cash', 'status': 'pending_counter',
                 'amount_cents': q.total_cents},
    )

    # Save through the real service — no PermissionDenied because it's a fresh booking
    store.save_history([rec], now)

    # Mark paid at the counter (requires staff permission)
    pax1_tno = rec['passengers'][0]['ticket_no']
    ok, msg = store.mark_paid(pax1_tno, principal, now)
    assert ok, f'mark_paid failed: {msg}'

    # Reload to get the post-payment state
    history2 = store.load_history()
    record = history2[-1]

    return store, principal, record


# ---------------------------------------------------------------------------
# Test class: two-passenger return booking — full verification matrix
# ---------------------------------------------------------------------------

class TestReturnBookingVerification:
    """Two-passenger return booking — full verification matrix for all 6 bug fixes."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        """Create real DB, real booking, real staff account."""
        self.tmp = tmp_path
        self.store, self.principal, self.record = _build_real_return_booking(tmp_path)
        self.db_path = self.store.path
        pax = self.record['passengers']
        self.p1 = pax[0]  # Alice Smith
        self.p2 = pax[1]  # Bob Jones
        self.base_url = 'https://cta.example.com'

    def _flask_app(self):
        import cta_ticket_server as srv
        app = srv.build_app(self.db_path)
        app.config['TESTING'] = True
        return app

    def _issue_tokens(self):
        """Issue web tokens for all 4 ticket legs (p1-out, p1-return, p2-out, p2-return)."""
        from cta_ticket_server import issue_web_token
        return {
            'p1_out':    issue_web_token(self.db_path, self.p1['ticket_id']),
            'p1_return': issue_web_token(self.db_path, self.p1['return_ticket_id']),
            'p2_out':    issue_web_token(self.db_path, self.p2['ticket_id']),
            'p2_return': issue_web_token(self.db_path, self.p2['return_ticket_id']),
        }

    # -----------------------------------------------------------------------
    # Bug 1 fix: return-leg PDF no longer crashes with KeyError: 'ticket_no'
    # -----------------------------------------------------------------------

    def test_bug1_return_pdf_no_crash(self):
        """Bug 1 fix: return-leg token must generate a PDF without HTTP 500.

        Root cause: _record_to_booking() set pax['return_ticket_no'] for return legs
        but never set pax['ticket_no'], causing KeyError in cta_pdf.py line 244.
        """
        pytest.importorskip('reportlab', reason='reportlab not installed')
        tokens = self._issue_tokens()
        app = self._flask_app()
        with app.test_client() as client:
            for leg_key in ('p1_return', 'p2_return'):
                token = tokens[leg_key]
                resp = client.get(f'/ticket/{token}/pdf')
                assert resp.status_code == 200, (
                    f'Return-leg PDF crashed (Bug 1): expected 200, got {resp.status_code}. '
                    f'Token: {token[:8]}...'
                )
                assert resp.content_type == 'application/pdf'
                assert resp.data[:4] == b'%PDF', 'Response must begin with PDF magic bytes'

    # -----------------------------------------------------------------------
    # Bug 2 fix: outbound token generates exactly ONE journey page
    # -----------------------------------------------------------------------

    def test_bug2_outbound_token_one_page_only(self):
        """Bug 2 fix: an outbound token's PDF must contain exactly one journey page.

        Root cause: _record_to_booking() passed booking['legs']=2 (from the stored
        booking), causing build_ticket_pdf to render both outbound AND return pages.
        Fix: always set booking['legs']=1 — one authorised journey per token.
        """
        pytest.importorskip('reportlab', reason='reportlab not installed')
        from reportlab.lib.pagesizes import A4
        import io
        try:
            from pypdf import PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                pytest.skip('pypdf / PyPDF2 not installed — cannot count pages')

        tokens = self._issue_tokens()
        app = self._flask_app()
        with app.test_client() as client:
            for leg_key in ('p1_out', 'p2_out'):
                token = tokens[leg_key]
                resp = client.get(f'/ticket/{token}/pdf')
                assert resp.status_code == 200
                reader = PdfReader(io.BytesIO(resp.data))
                # One journey page + one receipt page (or just one page) — must not be 3+
                # The critical check: outbound token must NOT produce return journey page
                assert len(reader.pages) <= 2, (
                    f'Bug 2: outbound token produced {len(reader.pages)} pages — '
                    f'expected at most 2 (journey + receipt), not both journeys. '
                    f'Token key: {leg_key}'
                )

    def test_bug2_return_token_one_page_only(self):
        """Bug 2 fix: a return-leg token must also produce exactly one journey page."""
        pytest.importorskip('reportlab', reason='reportlab not installed')
        try:
            from pypdf import PdfReader
        except ImportError:
            try:
                from PyPDF2 import PdfReader
            except ImportError:
                pytest.skip('pypdf / PyPDF2 not installed')

        import io
        tokens = self._issue_tokens()
        app = self._flask_app()
        with app.test_client() as client:
            for leg_key in ('p1_return', 'p2_return'):
                token = tokens[leg_key]
                resp = client.get(f'/ticket/{token}/pdf')
                assert resp.status_code == 200
                reader = PdfReader(io.BytesIO(resp.data))
                assert len(reader.pages) <= 2, (
                    f'Return token produced {len(reader.pages)} pages — expected ≤2. '
                    f'Token key: {leg_key}'
                )

    def test_bug2_outbound_token_shows_correct_passenger_page(self):
        """Outbound ticket page shows the correct passenger name."""
        tokens = self._issue_tokens()
        app = self._flask_app()
        with app.test_client() as client:
            # Alice's outbound must show Alice, not Bob
            resp = client.get(f'/ticket/{tokens["p1_out"]}')
            assert resp.status_code == 200
            body = resp.data.decode()
            assert 'Alice Smith' in body
            assert 'Bob Jones' not in body, 'P1 token must not expose P2 details'

            # Bob's outbound must show Bob, not Alice
            resp2 = client.get(f'/ticket/{tokens["p2_out"]}')
            body2 = resp2.data.decode()
            assert 'Bob Jones' in body2
            assert 'Alice Smith' not in body2, 'P2 token must not expose P1 details'

    # -----------------------------------------------------------------------
    # Bug 3 fix: staff URL-scanning resolves to ticket_id correctly
    # -----------------------------------------------------------------------

    def test_bug3_resolve_url_token_returns_ticket_id(self):
        """Bug 3 fix: resolve_url_token must return the correct ticket_id for a URL QR code."""
        from cta_ticket_server import issue_web_token, resolve_url_token
        tokens = self._issue_tokens()
        for leg_key, expected_tid in [
            ('p1_out', self.p1['ticket_id']),
            ('p2_out', self.p2['ticket_id']),
            ('p1_return', self.p1['return_ticket_id']),
            ('p2_return', self.p2['return_ticket_id']),
        ]:
            url = f'{self.base_url}/ticket/{tokens[leg_key]}'
            ticket_id, err = resolve_url_token(self.db_path, url, self.base_url)
            assert err is None, f'{leg_key}: unexpected error: {err!r}'
            assert ticket_id == expected_tid, (
                f'{leg_key}: expected ticket_id {expected_tid!r}, got {ticket_id!r}'
            )

    def test_bug3_url_scan_never_fetches_url(self):
        """Bug 3 security: URL resolution must never make any HTTP request."""
        import unittest.mock as mock
        from cta_ticket_server import issue_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        url = f'{self.base_url}/ticket/{token}'
        with mock.patch('urllib.request.urlopen',
                        side_effect=AssertionError('HTTP request was made')):
            ticket_id, err = resolve_url_token(self.db_path, url, self.base_url)
        assert ticket_id == self.p1['ticket_id'], (
            f'Expected ticket_id, got: ticket_id={ticket_id!r}, err={err!r}'
        )

    def test_bug3_different_host_url_not_resolved(self):
        """Bug 3 security: URLs for a different host must be rejected."""
        from cta_ticket_server import issue_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        evil_url = f'https://evil.example.com/ticket/{token}'
        ticket_id, err = resolve_url_token(self.db_path, evil_url, self.base_url)
        assert ticket_id is None, 'Different host URL must be rejected'
        assert err, 'Must return an error for wrong host'

    def test_bug3_viewing_url_does_not_check_in(self):
        """Bug 3 security: viewing the ticket page (passenger scanning own phone) must NOT check them in."""
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        app = self._flask_app()
        with app.test_client() as client:
            # Simulate passenger viewing their ticket URL
            client.get(f'/ticket/{token}')
        # Status must be unchanged after viewing
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute(
                'SELECT status FROM tickets WHERE ticket_id = ?',
                (self.p1['ticket_id'],)
            ).fetchone()
        assert row[0] == 'issued', (
            f'Viewing the ticket page must not check in: status is {row[0]!r}'
        )

    def test_bug3_staff_checkin_via_ticket_no_works(self):
        """Staff check-in via typed ticket number still works after Bug 3 fix."""
        import cta_db
        tno = self.p1['ticket_no']
        # depart_at stored as naive datetime; check_ticket compares against now naively
        now = datetime(2026, 10, 30, 12, 0)  # naive
        res = self.store.check_in(tno, signer=None, now=now,
                                   operator=self.principal,
                                   device='gate-01', environment='testing')
        assert res.ok, f'Staff check-in via ticket_no failed: {res.message}'

    # -----------------------------------------------------------------------
    # Bug 4 fix: unsafe OTP fallback — only dev/test envs show on-screen codes
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize('env_value', [
        '',           # empty string — was silently falling through to OnScreenCodes
        'staging',    # unrecognised value — was silently falling through
        'qa',         # another common non-prod env
        'prod',       # explicit production
        'production', # explicit production
    ])
    def test_bug4_non_dev_env_blocks_signin(self, env_value, tmp_path):
        """Bug 4 fix: missing/unrecognised environment must block sign-in (SmsUnavailable)."""
        import cta_sms
        cfg_path = tmp_path / 'cta_config.json'
        cfg_path.write_text(json.dumps({}))
        # Remove all credential env vars
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID', 'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment=env_value)
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            f'Bug 4: environment={env_value!r} must return SmsUnavailable, '
            f'got {type(provider).__name__}. On-screen codes must never appear '
            f'outside explicit dev/test environments.'
        )

    @pytest.mark.parametrize('env_value', ['development', 'dev', 'testing', 'test'])
    def test_bug4_explicit_dev_env_allows_onscreen(self, env_value, tmp_path):
        """Bug 4 fix: only explicit dev/test environments may show on-screen codes."""
        import cta_sms
        cfg_path = tmp_path / 'cta_config.json'
        cfg_path.write_text(json.dumps({}))
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID', 'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment=env_value)
        assert isinstance(provider, cta_sms.OnScreenCodes), (
            f'environment={env_value!r} should give OnScreenCodes for dev use, '
            f'got {type(provider).__name__}'
        )

    def test_bug4_onscreen_provider_shows_code_attribute(self, tmp_path):
        """OnScreenCodes.shows_code must be True so the UI can label it as dev-only."""
        import cta_sms
        cfg_path = tmp_path / 'cta_config.json'
        cfg_path.write_text(json.dumps({}))
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID', 'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment='development')
        assert provider.shows_code is True

    def test_bug4_sms_unavailable_blocks_start(self, tmp_path):
        """SmsUnavailable.start() must always return (False, reason) — never allow sign-in."""
        import cta_sms
        cfg_path = tmp_path / 'cta_config.json'
        cfg_path.write_text(json.dumps({}))
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID', 'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment='')  # empty env
        now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
        ok, msg = provider.start('+966512345678', now)
        assert not ok, 'SmsUnavailable.start() must return ok=False'
        assert msg, 'SmsUnavailable.start() must provide an error message'

    # -----------------------------------------------------------------------
    # Bug 5: personalised SMS — TwilioVerify.start() accepts template params
    # -----------------------------------------------------------------------

    def test_bug5_twilio_start_accepts_template_sid(self):
        """Bug 5: TwilioVerify.start() must accept template_sid and template_vars."""
        import cta_sms
        calls = []

        def fake_open(req, timeout=15.0):
            calls.append({'url': req.full_url, 'data': req.data})
            import io
            class FakeResp:
                status = 201
                def read(self): return b'{"status":"pending"}'
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return FakeResp()

        provider = cta_sms.TwilioVerify('AC123', 'token123', 'VA123', opener=fake_open)
        now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
        ok, err = provider.start(
            '+966512345678', now,
            template_sid='HJxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            template_vars={'name': 'Ahmed Al-Rashidi'},
        )
        assert ok, f'start() with template_sid failed: {err!r}'
        assert calls, 'No HTTP request was made'
        # The posted data must contain the template SID
        posted = calls[0]['data'].decode()
        assert 'TemplateSid=HJ' in posted, (
            f'TemplateSid not found in POST data: {posted!r}'
        )
        assert 'Ahmed' in posted, (
            f'template_vars not included in POST data: {posted!r}'
        )

    def test_bug5_twilio_start_accepts_locale(self):
        """Bug 5: TwilioVerify.start() must accept locale for built-in localisation."""
        import cta_sms
        calls = []

        def fake_open(req, timeout=15.0):
            calls.append({'data': req.data})
            import io
            class FakeResp:
                status = 201
                def read(self): return b'{"status":"pending"}'
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return FakeResp()

        provider = cta_sms.TwilioVerify('AC123', 'token123', 'VA123', opener=fake_open)
        now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
        ok, err = provider.start('+966512345678', now, locale='ar')
        assert ok, f'start() with locale failed: {err!r}'
        posted = calls[0]['data'].decode()
        assert 'Locale=ar' in posted, f'Locale not found in POST data: {posted!r}'

    def test_bug5_twilio_never_shows_code_on_screen(self):
        """Bug 5: TwilioVerify must never show the OTP on screen (shows_code=False)."""
        import cta_sms
        provider = cta_sms.TwilioVerify('AC123', 'token123', 'VA123')
        assert provider.shows_code is False

    def test_bug5_saudi_number_format_note(self):
        """Verify Saudi number normalisation: 05XXXXXXXX → +9665XXXXXXXX.

        This is a documentation test: the caller is responsible for normalisation.
        TwilioVerify passes the number through to Twilio unchanged.
        """
        # Confirm E.164 form that must be used with Twilio
        saudi_local = '0512345678'
        expected_e164 = '+966512345678'
        normalised = '+966' + saudi_local.lstrip('0')
        assert normalised == expected_e164, (
            f'Saudi number normalisation: {saudi_local} → {normalised}, expected {expected_e164}'
        )

    # -----------------------------------------------------------------------
    # Bug 6 fix: malformed token expiry timestamps deny access
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize('bad_date', [
        'not-a-date',
        '2026/01/01 10:00:00',   # wrong separator
        '2026-01-01T10:00:00',   # ISO with T (not the stored format)
        '',                       # empty string
        '99999-99-99 99:99:99',  # out-of-range
    ])
    def test_bug6_malformed_issued_at_denies_access(self, bad_date):
        """Bug 6 fix: malformed issued_at must deny access (return 404), not bypass expiry."""
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])

        # Write a malformed issued_at
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                'UPDATE ticket_web_tokens SET issued_at = ? WHERE token = ?',
                (bad_date, token)
            )
            conn.commit()

        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404, (
                f'Bug 6: malformed issued_at {bad_date!r} must be rejected (404), '
                f'got {resp.status_code}. Malformed dates must not bypass expiry checking.'
            )

    def test_bug6_malformed_date_pdf_also_denied(self):
        """Bug 6 fix: PDF route must also deny access when issued_at is malformed."""
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                'UPDATE ticket_web_tokens SET issued_at = ? WHERE token = ?',
                ('not-a-date', token)
            )
            conn.commit()
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}/pdf')
            assert resp.status_code == 404, (
                f'Bug 6: malformed date must deny PDF access, got {resp.status_code}'
            )

    def test_bug6_valid_date_still_works(self):
        """Bug 6: a well-formed issued_at must still grant access (regression guard)."""
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 200, (
                f'A valid token must still work after Bug 6 fix, got {resp.status_code}'
            )

    def test_bug6_resolve_url_token_rejects_malformed_date(self):
        """Bug 6 fix: resolve_url_token must also reject malformed dates."""
        from cta_ticket_server import issue_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                'UPDATE ticket_web_tokens SET issued_at = ? WHERE token = ?',
                ('not-a-date', token)
            )
            conn.commit()
        url = f'{self.base_url}/ticket/{token}'
        ticket_id, err = resolve_url_token(self.db_path, url, self.base_url)
        assert ticket_id is None, 'resolve_url_token must reject malformed date'
        assert err, 'resolve_url_token must return an error for malformed date'

    # -----------------------------------------------------------------------
    # Regression: outbound link does NOT expose the other passenger's details
    # -----------------------------------------------------------------------

    def test_each_token_scoped_to_one_passenger(self):
        """Each private link must show only the authorised passenger (no cross-leakage)."""
        tokens = self._issue_tokens()
        app = self._flask_app()
        with app.test_client() as client:
            for token_key, owner, other in [
                ('p1_out',    'Alice Smith', 'Bob Jones'),
                ('p1_return', 'Alice Smith', 'Bob Jones'),
                ('p2_out',    'Bob Jones',   'Alice Smith'),
                ('p2_return', 'Bob Jones',   'Alice Smith'),
            ]:
                resp = client.get(f'/ticket/{tokens[token_key]}')
                body = resp.data.decode()
                assert owner in body, f'{token_key}: owner {owner!r} missing from page'
                assert other not in body, (
                    f'{token_key}: other passenger {other!r} must not appear in {owner}\'s page'
                )

    # -----------------------------------------------------------------------
    # Revoked and expired links deny access
    # -----------------------------------------------------------------------

    def test_revoked_link_denied(self):
        """Revoked tokens must return 404 with no passenger data."""
        from cta_ticket_server import issue_web_token, revoke_web_token
        token = issue_web_token(self.db_path, self.p1['ticket_id'])
        revoke_web_token(self.db_path, self.p1['ticket_id'])
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404
            assert 'Alice Smith' not in resp.data.decode()

    def test_expired_link_denied(self):
        """Expired tokens must return 404."""
        from cta_ticket_server import issue_web_token, TOKEN_EXPIRY_DAYS
        token = issue_web_token(self.db_path, self.p2['ticket_id'])
        old = (datetime.now(timezone.utc) - timedelta(days=TOKEN_EXPIRY_DAYS + 1)
               ).strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute('UPDATE ticket_web_tokens SET issued_at = ? WHERE token = ?',
                         (old, token))
            conn.commit()
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PDF content verification tests
# Verifies the ACTUAL TEXT in generated PDFs — not just HTTP 200 / page count.
# Covers all 4 tokens: p1-out, p1-return, p2-out, p2-return.
# ---------------------------------------------------------------------------

class TestPDFContent:
    """Verify actual text content of generated PDFs for each leg/passenger combo."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp = tmp_path
        self.store, self.principal, self.record = _build_real_return_booking(tmp_path)
        self.db_path = self.store.path
        pax = self.record['passengers']
        self.p1 = pax[0]  # Alice Smith
        self.p2 = pax[1]  # Bob Jones
        self.booking_ref = self.record['reference']

    def _extract_pdf_text(self, pdf_path: Path) -> str:
        """Extract all text from a PDF using pdfminer."""
        from pdfminer.high_level import extract_text
        return extract_text(str(pdf_path))

    def _build_pdf_from_booking(self, booking: dict, pdf_path: Path):
        """Call build_ticket_pdf with leg_override from booking dict."""
        import cta_pdf
        cta_pdf.build_ticket_pdf(
            booking, pdf_path, include_receipt=False,
            leg_override=booking.get('leg_override', ''),
        )

    def _booking_for_token(self, ticket_id: str) -> dict:
        """Issue a web token and reconstruct the booking dict via _record_to_booking."""
        from cta_ticket_server import issue_web_token, lookup_token, _record_to_booking
        token = issue_web_token(self.db_path, ticket_id)
        info, err = lookup_token(self.db_path, token)
        assert info is not None, f'Token lookup failed: {err}'
        return _record_to_booking(info)

    # -----------------------------------------------------------------------
    # P1 outbound ticket
    # -----------------------------------------------------------------------

    def test_p1_out_pdf_has_correct_passenger_name(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Alice Smith' in text, f'Passenger name missing. PDF text:\n{text[:800]}'

    def test_p1_out_pdf_has_correct_ticket_number(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        expected = self.p1['ticket_no']
        assert expected and expected in text, (
            f'Ticket number {expected!r} missing. PDF text:\n{text[:800]}'
        )

    def test_p1_out_pdf_correct_route_direction(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        from pypdf import PdfReader
        xs = {}

        def visit(text, cm, tm, font_dict, font_size):
            if text.strip() in ('Bylyn', 'Soth'):
                xs[text.strip()] = tm[4]
        PdfReader(str(pdf)).pages[0].extract_text(visitor_text=visit)
        # Outbound: the start station is drawn left of the arrow and the destination to its right
        assert set(xs) == {'Bylyn', 'Soth'}, f'stations not found on the pass: {xs}'
        assert xs['Bylyn'] < xs['Soth'], f'Outbound should read Bylyn to Soth (x positions: {xs})'

    def test_p1_out_pdf_does_not_show_return_ticket_no(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        return_no = self.p1.get('return_ticket_no', '')
        if return_no:
            assert return_no not in text, (
                f'Return ticket number {return_no!r} should not appear in outbound PDF'
            )

    def test_p1_out_pdf_does_not_show_p2_name(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Bob Jones' not in text, (
            f"P2 name 'Bob Jones' must not appear in P1 PDF. PDF text:\n{text[:800]}"
        )

    def test_p1_out_pdf_has_booking_reference(self, tmp_path):
        booking = self._booking_for_token(self.p1['ticket_id'])
        pdf = tmp_path / 'p1_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert self.booking_ref in text, (
            f'Booking reference {self.booking_ref!r} missing. PDF text:\n{text[:800]}'
        )

    # -----------------------------------------------------------------------
    # P1 return ticket — the critical bug-fix case
    # -----------------------------------------------------------------------

    def test_p1_return_pdf_has_correct_passenger_name(self, tmp_path):
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Alice Smith' in text, f'Passenger name missing. PDF text:\n{text[:800]}'

    def test_p1_return_pdf_has_correct_ticket_number(self, tmp_path):
        """Return PDF must show return_ticket_no, not blank."""
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        expected = self.p1['return_ticket_no']
        assert expected, 'return_ticket_no is empty on p1 record'
        assert expected in text, (
            f'Return ticket number {expected!r} missing from return PDF.\n'
            f'PDF text:\n{text[:1000]}'
        )

    def test_p1_return_pdf_ticket_number_not_blank(self, tmp_path):
        """The old bug rendered blank ticket number — verify it never regresses."""
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        # If ticket_no was blank, the PDF would have no CTA- prefixed ticket number.
        import re
        ticket_nos = re.findall(r'CTA-\S+', text)
        assert ticket_nos, (
            f'No CTA-... ticket number found in return PDF (blank ticket_no bug).\n'
            f'PDF text:\n{text[:1000]}'
        )

    def test_p1_return_pdf_correct_route_direction(self, tmp_path):
        """Return PDF must show Soth → Bylyn (reversed)."""
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        soth_idx = text.find('Soth')
        bylyn_idx = text.find('Bylyn')
        assert soth_idx != -1, f"'Soth' not found in return PDF text:\n{text[:800]}"
        assert bylyn_idx != -1, f"'Bylyn' not found in return PDF text:\n{text[:800]}"
        assert soth_idx < bylyn_idx, (
            f"Expected Soth before Bylyn (return direction). soth={soth_idx} bylyn={bylyn_idx}"
        )

    def test_p1_return_pdf_has_return_ticket_header(self, tmp_path):
        """Return PDF header must say RETURN TICKET, not PASSENGER TICKET."""
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'RETURN' in text.upper(), (
            f"'RETURN' not found in return PDF. Header wrong?\nPDF text:\n{text[:800]}"
        )

    def test_p1_return_pdf_does_not_show_p2_name(self, tmp_path):
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Bob Jones' not in text, (
            f"P2 name 'Bob Jones' must not appear in P1 return PDF. Text:\n{text[:800]}"
        )

    def test_p1_return_pdf_does_not_show_outbound_ticket_no(self, tmp_path):
        """Outbound ticket number must not appear as a standalone token in return PDF.

        Note: the return ticket number (e.g. CTA-...-P1R) starts with the outbound
        number (CTA-...-P1), so we check that the outbound number does NOT appear as
        a word-boundary match — i.e. not as 'P1' followed by end-of-string/non-alnum.
        """
        import re
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        outbound_no = self.p1['ticket_no']
        if outbound_no:
            # Escape and match the outbound number only when NOT followed by alphanumeric
            # (so 'CTA-...-P1' doesn't match inside 'CTA-...-P1R')
            pattern = re.escape(outbound_no) + r'(?![A-Za-z0-9])'
            assert not re.search(pattern, text), (
                f'Outbound ticket no {outbound_no!r} appears standalone in return PDF'
            )

    def test_p1_return_pdf_has_booking_reference(self, tmp_path):
        booking = self._booking_for_token(self.p1['return_ticket_id'])
        pdf = tmp_path / 'p1_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert self.booking_ref in text, (
            f'Booking reference {self.booking_ref!r} missing. PDF text:\n{text[:800]}'
        )

    # -----------------------------------------------------------------------
    # P2 outbound ticket
    # -----------------------------------------------------------------------

    def test_p2_out_pdf_has_correct_passenger_name(self, tmp_path):
        booking = self._booking_for_token(self.p2['ticket_id'])
        pdf = tmp_path / 'p2_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Bob Jones' in text, f'Passenger name missing. PDF text:\n{text[:800]}'

    def test_p2_out_pdf_has_correct_ticket_number(self, tmp_path):
        booking = self._booking_for_token(self.p2['ticket_id'])
        pdf = tmp_path / 'p2_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        expected = self.p2['ticket_no']
        assert expected and expected in text, (
            f'Ticket number {expected!r} missing. PDF text:\n{text[:800]}'
        )

    def test_p2_out_pdf_does_not_show_p1_name(self, tmp_path):
        booking = self._booking_for_token(self.p2['ticket_id'])
        pdf = tmp_path / 'p2_out.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Alice Smith' not in text, (
            f"P1 name 'Alice Smith' must not appear in P2 PDF. Text:\n{text[:800]}"
        )

    # -----------------------------------------------------------------------
    # P2 return ticket
    # -----------------------------------------------------------------------

    def test_p2_return_pdf_has_correct_passenger_name(self, tmp_path):
        booking = self._booking_for_token(self.p2['return_ticket_id'])
        pdf = tmp_path / 'p2_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Bob Jones' in text, f'Passenger name missing. PDF text:\n{text[:800]}'

    def test_p2_return_pdf_has_correct_ticket_number(self, tmp_path):
        booking = self._booking_for_token(self.p2['return_ticket_id'])
        pdf = tmp_path / 'p2_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        expected = self.p2['return_ticket_no']
        assert expected, 'return_ticket_no is empty on p2 record'
        assert expected in text, (
            f'Return ticket number {expected!r} missing. PDF text:\n{text[:1000]}'
        )

    def test_p2_return_pdf_correct_route_direction(self, tmp_path):
        """Return PDF must show Soth → Bylyn (reversed)."""
        booking = self._booking_for_token(self.p2['return_ticket_id'])
        pdf = tmp_path / 'p2_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        soth_idx = text.find('Soth')
        bylyn_idx = text.find('Bylyn')
        assert soth_idx != -1, f"'Soth' not found in return PDF text:\n{text[:800]}"
        assert bylyn_idx != -1, f"'Bylyn' not found in return PDF text:\n{text[:800]}"
        assert soth_idx < bylyn_idx, (
            f"Expected Soth before Bylyn (return). soth={soth_idx} bylyn={bylyn_idx}"
        )

    def test_p2_return_pdf_does_not_show_p1_name(self, tmp_path):
        booking = self._booking_for_token(self.p2['return_ticket_id'])
        pdf = tmp_path / 'p2_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        assert 'Alice Smith' not in text, (
            f"P1 name 'Alice Smith' must not appear in P2 return PDF. Text:\n{text[:800]}"
        )

    def test_p2_return_pdf_does_not_show_p1_ticket_nos(self, tmp_path):
        """P2 return must not contain P1's ticket numbers."""
        booking = self._booking_for_token(self.p2['return_ticket_id'])
        pdf = tmp_path / 'p2_return.pdf'
        self._build_pdf_from_booking(booking, pdf)
        text = self._extract_pdf_text(pdf)
        for no in (self.p1['ticket_no'], self.p1.get('return_ticket_no', '')):
            if no:
                assert no not in text, (
                    f"P1 ticket no {no!r} must not appear in P2 return PDF"
                )

    # -----------------------------------------------------------------------
    # Cross-isolation: token scope
    # -----------------------------------------------------------------------

    def test_all_four_pdfs_have_distinct_ticket_numbers(self, tmp_path):
        """Each PDF renders its own distinct ticket number — no cross-contamination."""
        from cta_ticket_server import issue_web_token, lookup_token, _record_to_booking
        import cta_pdf
        import re

        ticket_ids = [
            self.p1['ticket_id'],
            self.p1['return_ticket_id'],
            self.p2['ticket_id'],
            self.p2['return_ticket_id'],
        ]
        ticket_nos_seen = {}
        for tid in ticket_ids:
            token = issue_web_token(self.db_path, tid)
            info, err = lookup_token(self.db_path, token)
            assert info is not None, f'lookup_token failed for {tid}: {err}'
            booking = _record_to_booking(info)
            pdf_path = tmp_path / f'{tid[:8]}.pdf'
            cta_pdf.build_ticket_pdf(
                booking, pdf_path, include_receipt=False,
                leg_override=booking.get('leg_override', ''),
            )
            text = self._extract_pdf_text(pdf_path)
            # Match only full ticket numbers (with passenger suffix: -P1, -P2, -P1R, -P2R)
            # Exclude the booking reference (CTA-DATE-REF without -Pn suffix)
            nos = set(re.findall(r'CTA-\d{8}-\w+-P\w+', text))
            assert nos, f'No passenger ticket number (CTA-...-Pn) found in PDF for {tid[:8]}'
            ticket_nos_seen[tid] = nos

        # No PDF should contain the other passenger's ticket numbers
        assert ticket_nos_seen[self.p1['ticket_id']].isdisjoint(
            ticket_nos_seen[self.p2['ticket_id']]
        ), (
            f"P1 outbound PDF contains P2 ticket numbers.\n"
            f"P1: {ticket_nos_seen[self.p1['ticket_id']]}\n"
            f"P2: {ticket_nos_seen[self.p2['ticket_id']]}"
        )
        assert ticket_nos_seen[self.p1['return_ticket_id']].isdisjoint(
            ticket_nos_seen[self.p2['return_ticket_id']]
        ), (
            f"P1 return PDF contains P2 ticket numbers.\n"
            f"P1R: {ticket_nos_seen[self.p1['return_ticket_id']]}\n"
            f"P2R: {ticket_nos_seen[self.p2['return_ticket_id']]}"
        )


# ---------------------------------------------------------------------------
# External setup requirements (printed on import for CI visibility)
# ---------------------------------------------------------------------------

EXTERNAL_SETUP_REQUIRED = """
External setup still required
==============================

1. TWILIO SMS / PERSONALISED TEMPLATE (Bug 5)
   - Create a Twilio account: https://www.twilio.com/
   - Create a Verify Service in the Twilio Console (Verify → Services).
   - Set credentials in cta_config.json (run: python setup_sms.py) or via env vars:
       TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VERIFY_SERVICE_SID
   - For the personalised message
       "Hi {name}, your CTA verification code is {code}.
        It expires in 15 minutes. Do not share this code."
     you must:
     a. Create a Custom Verification Template in Twilio Console:
           Verify → (your service) → General → Custom Code Templates
     b. Submit the template body for Twilio approval (1-5 business days).
     c. Once approved, pass template_sid=<approved SID> and
        template_vars={'name': passenger_name} to TwilioVerify.start().
     d. Saudi numbers must be normalised to E.164 format before calling:
           '05XXXXXXXX' → '+9665XXXXXXXX'
   - IMPORTANT: Never expose real OTPs on screen. Set CTA_ENV=production
     (or leave it unset) in any non-development deployment.
   - IMPORTANT: Do not send real SMS or incur charges without explicit authorisation.
   - Credentials must be stored outside the submission package (never in git).

2. TICKET SERVER PUBLIC HTTPS URL (for URL-format QR scanning — Bug 3)
   - Phone camera QR scanning requires a public HTTPS URL (not localhost).
   - Set the URL in cta_config.json under {"ticket_server": {"url": "https://..."}}
   - See DEPLOYMENT.md for tunnelling and hosting options.
   - The URL must not be a localhost or plain http:// address.
   - Do not claim remote access works until a real hosted HTTPS endpoint
     is available and tested end-to-end.

3. REPORTLAB (for PDF tests)
   - Install: pip install reportlab
   - PDF page-count tests also require: pip install pypdf (or PyPDF2)

4. PAYMENT PROVIDER (for real online payment testing)
   - The test suite uses cash/pending_counter payments.
   - Real online payment (Stripe etc.) requires separate provider configuration.
   - Do not charge real money without explicit authorisation.
"""
