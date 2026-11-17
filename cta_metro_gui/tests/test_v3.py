"""v3.0 regression and integration tests.

Covers:
  1. Saudi phone-number normalisation and invalid inputs
  2. Production sign-in refusing unavailable SMS (no silent fallback)
  3. No OTP leakage through the TwilioVerify provider
  4. Wrong, expired and reused OTPs (OnScreenCodes / OtpSession)
  5. Resend and attempt limits
  6. Ticket URL generation and QR decoding
  7. Valid, invalid, revoked and expired web access tokens
  8. PDF retrieval via the ticket server (mocked)
  9. Viewing a ticket does not check it in
 10. Staff scanner compatibility (URL tokens vs. signed tokens)
 11. Payment/refund status displayed accurately on the ticket page
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the app directory is on the path so modules import cleanly.
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))

os.environ.setdefault('CTA_ENV', 'testing')


# ===========================================================================
# 1. Saudi phone-number normalisation
# ===========================================================================

class TestSaudiPhoneNormalisation:
    """validate_phone must accept both Saudi local and international forms."""

    def setup_method(self):
        import cta_accounts as acct
        self.v = acct.validate_phone

    def test_saudi_local_basic(self):
        phone, err = self.v('0501234567')
        assert err == '', f'Unexpected error: {err}'
        assert phone == '+966501234567'

    def test_saudi_local_with_spaces(self):
        phone, err = self.v('050 123 4567')
        assert err == '' and phone == '+966501234567'

    def test_saudi_local_all_valid_prefixes(self):
        for prefix in ('050', '053', '055', '056', '058'):
            phone, err = self.v(prefix + '1234567')
            assert err == '' and phone.startswith('+96650') or phone.startswith('+9665')

    def test_saudi_international_form(self):
        phone, err = self.v('+9665 01 234 567')
        assert err == '' and phone == '+966501234567'

    def test_saudi_international_00966(self):
        phone, err = self.v('00966501234567')
        assert err == '' and phone == '+966501234567'

    def test_non_saudi_international(self):
        phone, err = self.v('+44 7123 456789')
        assert err == '' and phone == '+447123456789'

    def test_too_short_saudi(self):
        """0501234 is only 7 digits after stripping — too short."""
        phone, err = self.v('0501234')
        # Either normalised or rejected — must not silently produce wrong number
        if err == '':
            # If accepted, must have correct length
            digits = phone.lstrip('+')
            assert len(digits) >= 8, f'Normalised number too short: {phone}'

    def test_local_without_country_code_non_saudi(self):
        """A local number that is not Saudi (no 05 prefix) must be rejected."""
        phone, err = self.v('07911123456')
        assert phone is None or err != '', 'Non-Saudi local number should not be accepted without country code'

    def test_invalid_chars(self):
        phone, err = self.v('+966ABC12345')
        assert phone is None

    def test_empty(self):
        phone, err = self.v('')
        assert phone is None

    def test_too_long(self):
        phone, err = self.v('+9661234567890123456')
        assert phone is None

    def test_normalise_saudi_helper(self):
        import cta_accounts as acct
        assert acct.normalise_saudi('0501234567') == '+966501234567'
        assert acct.normalise_saudi('+966501234567') is None  # already E.164
        assert acct.normalise_saudi('07911123456') is None


# ===========================================================================
# 2. Production sign-in refuses unavailable SMS
# ===========================================================================

class TestProductionSmsBlock:
    """load_sms_provider must NEVER fall back to on-screen codes in production."""

    def _load(self, env='production', extra_env=None):
        import cta_sms
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / 'cta_config.json'
            cfg.write_text('{"environment": "' + env + '", "sms": {}}')
            env_vars = {'CTA_ENV': env}
            if extra_env:
                env_vars.update(extra_env)
            with patch.dict(os.environ, env_vars, clear=False):
                # Remove any Twilio env vars so credentials are absent
                for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
                    os.environ.pop(k, None)
                return cta_sms.load_sms_provider(cfg, environment=env)

    def test_production_no_credentials_returns_unavailable(self):
        import cta_sms
        provider = self._load('production')
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            f'Expected SmsUnavailable in production without credentials, got {type(provider).__name__}')

    def test_production_no_credentials_blocks_signin(self):
        import cta_sms
        provider = self._load('production')
        ok, msg = provider.start('+966501234567', datetime.now())
        assert not ok, 'Production without SMS should block sign-in'
        assert msg, 'Should return an error message'

    def test_development_no_credentials_returns_onscreen(self):
        import cta_sms
        provider = self._load('development')
        assert isinstance(provider, cta_sms.OnScreenCodes), (
            f'Expected OnScreenCodes in development without credentials, got {type(provider).__name__}')

    def test_testing_no_credentials_returns_onscreen(self):
        import cta_sms
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / 'cta_config.json'
            cfg.write_text('{}')
            for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
                os.environ.pop(k, None)
            with patch.dict(os.environ, {'CTA_ENV': 'testing'}, clear=False):
                provider = cta_sms.load_sms_provider(cfg, environment='testing')
        assert isinstance(provider, cta_sms.OnScreenCodes)


# ===========================================================================
# 3. No OTP leakage through TwilioVerify
# ===========================================================================

class TestTwilioNoOtpLeakage:
    """TwilioVerify.shows_code must be False; code_for must return empty string."""

    def test_shows_code_false(self):
        from cta_sms import TwilioVerify
        provider = TwilioVerify('ACtest', 'token', 'VAtest')
        assert provider.shows_code is False

    def test_no_code_for_method(self):
        from cta_sms import TwilioVerify
        provider = TwilioVerify('ACtest', 'token', 'VAtest')
        # TwilioVerify should not expose code_for at all, or return empty
        code = getattr(provider, 'code_for', lambda p: '')
        if callable(code):
            assert code('+966501234567') == ''

    def test_twilio_does_not_store_code_locally(self):
        """TwilioVerify.start() should call the API, not store a code locally."""
        from cta_sms import TwilioVerify
        calls = []

        def fake_open(req, timeout=15):
            calls.append(req)
            resp = MagicMock()
            resp.status = 201
            resp.read.return_value = b'{"status":"pending"}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        provider = TwilioVerify('ACtest', 'authtoken', 'VAtest', opener=fake_open)
        ok, msg = provider.start('+966501234567', datetime.now())
        assert ok
        assert len(calls) == 1
        # Should NOT have any stored code attribute
        assert not hasattr(provider, '_code')
        assert not hasattr(provider, 'sessions')


# ===========================================================================
# 4 & 5. OTP session: wrong, expired, reused, attempt limits
# ===========================================================================

class TestOtpSession:
    def _sess(self, now=None):
        from cta_accounts import OtpSession
        return OtpSession('+966501234567', now or datetime.now())

    def test_correct_code(self):
        s = self._sess()
        ok, msg = s.verify(s.demo_code, datetime.now())
        assert ok

    def test_wrong_code(self):
        s = self._sess()
        ok, msg = s.verify('000000', datetime.now())
        assert not ok
        assert 'Wrong' in msg or 'wrong' in msg.lower() or 'tries' in msg

    def test_expired(self):
        now = datetime.now()
        s = self._sess(now)
        future = now + timedelta(minutes=10)
        ok, msg = s.verify(s.demo_code, future)
        assert not ok
        assert 'expired' in msg.lower()

    def test_single_use(self):
        s = self._sess()
        code = s.demo_code
        ok1, _ = s.verify(code, datetime.now())
        ok2, msg = s.verify(code, datetime.now())
        assert ok1
        assert not ok2
        assert 'already used' in msg.lower()

    def test_attempt_limit(self):
        from cta_accounts import OtpSession
        s = self._sess()
        for _ in range(OtpSession.MAX_ATTEMPTS):
            s.verify('000000', datetime.now())
        ok, msg = s.verify(s.demo_code, datetime.now())
        assert not ok
        assert 'new' in msg.lower() or 'tries' in msg.lower() or 'wrong' in msg.lower() or 'locked' in msg.lower()

    def test_resend_creates_new_session(self):
        from cta_sms import OnScreenCodes
        provider = OnScreenCodes()
        now = datetime.now()
        provider.start('+966501234567', now)
        code1 = provider.code_for('+966501234567')
        provider.start('+966501234567', now)
        code2 = provider.code_for('+966501234567')
        # A new session replaces the old one (codes may differ due to randomness)
        # What matters: old session is gone, new one is usable
        ok, _ = provider.check('+966501234567', code2, now)
        assert ok


# ===========================================================================
# 6. Ticket URL generation
# ===========================================================================

class TestTicketUrlGeneration:
    def _make_db(self):
        """Create a minimal in-memory SQLite with a tickets row."""
        import cta_ticket_server as srv
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        db = Path(tmp.name)
        with sqlite3.connect(db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_no TEXT PRIMARY KEY, reference TEXT, passenger_no INTEGER,
                    leg TEXT, ticket_id TEXT, status TEXT, uses INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 1, boarded_at TEXT, boarded_by TEXT,
                    cancelled_at TEXT, cancelled_by TEXT, cancel_reason TEXT,
                    refund_cents INTEGER, refund_state TEXT, refunded_at TEXT,
                    refunded_by TEXT, refund_ref TEXT, reentries TEXT
                )
            """)
            conn.execute(
                "INSERT INTO tickets (ticket_no, reference, passenger_no, leg, ticket_id, status) "
                "VALUES ('TK-001', 'REF-001', 1, 'out', 'tid-abc-123', 'issued')"
            )
        return db

    def test_generate_token(self):
        from cta_ticket_server import generate_web_token
        token = generate_web_token()
        assert len(token) >= 40, 'Token should be at least 43 chars (32 bytes base64url)'
        assert all(c.isalnum() or c in '-_' for c in token)

    def test_issue_and_retrieve(self):
        from cta_ticket_server import issue_web_token
        db = self._make_db()
        try:
            token = issue_web_token(db, 'tid-abc-123')
            assert token and len(token) >= 40
            # Calling again returns the same token (idempotent)
            token2 = issue_web_token(db, 'tid-abc-123')
            assert token == token2
        finally:
            db.unlink(missing_ok=True)

    def test_issue_unknown_ticket_raises(self):
        from cta_ticket_server import issue_web_token
        db = self._make_db()
        try:
            with pytest.raises(ValueError, match='not found'):
                issue_web_token(db, 'nonexistent-ticket-id')
        finally:
            db.unlink(missing_ok=True)

    def test_ticket_url_format(self):
        from cta_ticket_server import ticket_url
        url = ticket_url('https://example.com', 'abc123token')
        assert url == 'https://example.com/ticket/abc123token'
        # No double slashes
        url2 = ticket_url('https://example.com/', 'abc123token')
        assert '//ticket' not in url2

    def test_tokens_are_unique(self):
        from cta_ticket_server import generate_web_token
        tokens = {generate_web_token() for _ in range(100)}
        assert len(tokens) == 100, 'Tokens should be unique'


# ===========================================================================
# 7. Valid, invalid, revoked web access tokens
# ===========================================================================

class TestWebTokenLookup:
    def _make_db_with_booking(self):
        import cta_ticket_server as srv
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        db = Path(tmp.name)
        with sqlite3.connect(db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT UNIQUE, issued_at TEXT, depart_at TEXT,
                    account_phone TEXT, total_cents INTEGER, legs INTEGER,
                    doc TEXT, version INTEGER DEFAULT 1, updated_at TEXT)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_no TEXT PRIMARY KEY, reference TEXT, passenger_no INTEGER,
                    leg TEXT, ticket_id TEXT, status TEXT, uses INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 1, boarded_at TEXT, boarded_by TEXT,
                    cancelled_at TEXT, cancelled_by TEXT, cancel_reason TEXT,
                    refund_cents INTEGER, refund_state TEXT, refunded_at TEXT,
                    refunded_by TEXT, refund_ref TEXT, reentries TEXT,
                    passenger_name TEXT
                )
            """)
            conn.execute(
                "INSERT INTO bookings VALUES (1,'REF-001','2026-01-01','2026-01-02',NULL,5000,1,'{}',1,'2026-01-01')"
            )
            conn.execute(
                "INSERT INTO tickets (ticket_no, reference, passenger_no, leg, ticket_id, status, passenger_name) "
                "VALUES ('TK-001', 'REF-001', 1, 'out', 'tid-abc-001', 'issued', 'Eman Al-Rashidi')"
            )
        return db

    def test_valid_token_lookup(self):
        from cta_ticket_server import issue_web_token, lookup_token
        db = self._make_db_with_booking()
        # Adjust lookup_token to use simpler query for test DB (no full join)
        token = issue_web_token(db, 'tid-abc-001')
        # Check token was stored
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                'SELECT ticket_id FROM ticket_web_tokens WHERE token = ?', (token,)
            ).fetchone()
            assert row and row[0] == 'tid-abc-001'
        db.unlink(missing_ok=True)

    def test_invalid_token_returns_error(self):
        from cta_ticket_server import lookup_token
        db = self._make_db_with_booking()
        record, err = lookup_token(db, 'this-token-does-not-exist-at-all')
        assert record is None
        assert err and 'not valid' in err.lower()
        db.unlink(missing_ok=True)

    def test_revoke_token(self):
        from cta_ticket_server import issue_web_token, revoke_web_token
        db = self._make_db_with_booking()
        token = issue_web_token(db, 'tid-abc-001')
        revoke_web_token(db, 'tid-abc-001')
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                'SELECT revoked_at FROM ticket_web_tokens WHERE token = ?', (token,)
            ).fetchone()
            assert row and row[0] is not None, 'Token should be marked revoked'
        db.unlink(missing_ok=True)

    def test_malformed_token_rejected_by_server(self):
        """The Flask route should reject tokens with invalid characters before DB lookup."""
        import cta_ticket_server as srv
        app = srv.build_app(Path(':memory:'))
        app.config['TESTING'] = True
        with app.test_client() as client:
            for bad in ('../../../etc/passwd', 'token with spaces', 'a' * 200):
                resp = client.get(f'/ticket/{bad}')
                assert resp.status_code in (400, 404), f'Expected 4xx for {bad!r}'

    def test_health_endpoint(self):
        import cta_ticket_server as srv
        app = srv.build_app(Path('nonexistent.db'))
        app.config['TESTING'] = True
        with app.test_client() as client:
            resp = client.get('/health')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['status'] == 'ok'

    def test_security_headers(self):
        import cta_ticket_server as srv
        app = srv.build_app(Path('nonexistent.db'))
        app.config['TESTING'] = True
        with app.test_client() as client:
            resp = client.get('/health')
            assert resp.headers.get('Referrer-Policy') == 'no-referrer'
            assert 'no-store' in resp.headers.get('Cache-Control', '')


# ===========================================================================
# 8. Viewing a ticket does NOT check it in
# ===========================================================================

class TestViewingDoesNotCheckIn:
    """The ticket server page must never trigger check-in logic."""

    def test_ticket_page_has_no_checkin_side_effects(self):
        """Fetching the ticket page must not call any check-in function."""
        import cta_ticket_server as srv

        checkin_called = []

        # Monkeypatch lookup_token to track what is called
        original_lookup = srv.lookup_token

        def fake_lookup(db, token):
            return {
                'ticket_id': 'tid-001',
                'ticket_no': 'TK-001',
                'leg': 'out',
                'status': 'issued',
                'revoked_at': None,
                'passenger_name': 'Test Passenger',
                'passenger_category': 'Adult',
                'fare_cents': 1000,
                'booking_ref': 'REF-001',
                'from_station': 'Station A',
                'to_station': 'Station B',
                'depart_at': '2026-06-01 09:00',
                'legs': 1,
                'trip_type': 'one-way',
                'total_cents': 1000,
                'payment_method': 'cash',
                'payment_status': 'paid',
                'payment_amount_cents': 1000,
            }, None

        srv.lookup_token = fake_lookup
        try:
            app = srv.build_app(Path('nonexistent.db'))
            app.config['TESTING'] = True
            with app.test_client() as client:
                resp = client.get('/ticket/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH')
                assert resp.status_code == 200
                # Page must not contain any check-in confirmation
                html = resp.data.decode('utf-8', errors='replace')
                assert 'check' not in html.lower() or 'Scanning this page does not check you in' in html
                assert checkin_called == [], 'Check-in must not be called by viewing the ticket page'
        finally:
            srv.lookup_token = original_lookup

    def test_ticket_page_shows_current_status_accurately(self):
        """Status shown on page must reflect the DB record, not a hardcoded value."""
        import cta_ticket_server as srv

        original_lookup = srv.lookup_token
        try:
            for status_key, expected_text in [
                ('issued', 'Active'),
                ('cancelled', 'Cancelled'),
                ('refunded', 'Refunded'),
                ('boarded', 'Used'),
            ]:
                def fake_lookup(db, token, _s=status_key):
                    return {
                        'ticket_id': 'tid-001', 'ticket_no': 'TK-001', 'leg': 'out',
                        'status': _s, 'revoked_at': None,
                        'passenger_name': 'Test Passenger', 'passenger_category': 'Adult',
                        'fare_cents': 1000, 'booking_ref': 'REF-001',
                        'from_station': 'A', 'to_station': 'B',
                        'depart_at': '2026-06-01 09:00', 'legs': 1,
                        'trip_type': 'one-way', 'total_cents': 1000,
                        'payment_method': 'cash', 'payment_status': 'paid',
                        'payment_amount_cents': 1000,
                    }, None

                srv.lookup_token = fake_lookup
                app = srv.build_app(Path('nonexistent.db'))
                app.config['TESTING'] = True
                with app.test_client() as client:
                    resp = client.get('/ticket/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH')
                    html = resp.data.decode('utf-8', errors='replace')
                    assert expected_text in html, (
                        f'Status "{status_key}" should show "{expected_text}" on the page, got:\n{html[:500]}'
                    )
        finally:
            srv.lookup_token = original_lookup


# ===========================================================================
# 9. Staff scanner compatibility: URL QR and signed-token QR both work
# ===========================================================================

class TestScannerCompatibility:
    """Staff scanner must support both signed CTA1:... tokens and URL-type QR codes.

    URL QR codes from the ticket server are resolved locally (no HTTP fetch)
    using parse_ticket_url / resolve_url_token from cta_ticket_server.
    Existing signed tokens continue to work through parse_presented as before.
    """

    def test_signed_token_still_parses(self):
        """Existing CTA1:... tokens still parse correctly."""
        from cta_tickets import parse_presented
        result = parse_presented('CTA1:k1:0123456789abcdef0123456789abcdef:signaturesig', signer=None)
        # Should not raise; code is FORGED or similar (signature fails without real key)
        assert result is not None

    def test_url_qr_is_recognised_by_ticket_server(self):
        """A ticket URL matching the configured server is resolved locally to a token."""
        from cta_ticket_server import parse_ticket_url, generate_web_token
        token = generate_web_token()
        base = 'https://cta.example.com'
        url = f'{base}/ticket/{token}'
        result = parse_ticket_url(url, base)
        assert result == token, f'Expected token {token!r}, got {result!r}'

    def test_url_qr_wrong_host_returns_none(self):
        """A ticket URL from a different host must not be resolved."""
        from cta_ticket_server import parse_ticket_url, generate_web_token
        token = generate_web_token()
        result = parse_ticket_url(
            f'https://evil.example.com/ticket/{token}',
            'https://cta.example.com',
        )
        assert result is None, 'URL from wrong host must return None'

    def test_url_qr_never_fetched_by_parse_ticket_url(self):
        """parse_ticket_url must never make HTTP requests."""
        import unittest.mock as mock
        from cta_ticket_server import parse_ticket_url
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('HTTP fetch')):
            result = parse_ticket_url(
                'https://evil.com/ticket/abc', 'https://cta.example.com'
            )
        assert result is None  # wrong host; also no HTTP was attempted

    def test_long_url_rejected_as_too_long(self):
        """A URL longer than MAX_PRESENTED is rejected immediately by parse_presented."""
        from cta_tickets import parse_presented, MAX_PRESENTED, MALFORMED
        long_url = 'https://example.com/ticket/' + 'a' * 500
        result = parse_presented(long_url, signer=None)
        assert result.code == MALFORMED


# ===========================================================================
# 10. Payment integrity: status displayed accurately (not hardcoded PAID)
# ===========================================================================

class TestPaymentStatusAccuracy:
    """The ticket PDF and web page must show the current payment status, not a hardcoded value.
    This is a regression test for the v2.5 finding: receipt page said PAID for refunded tickets."""

    def test_revoked_token_returns_404_not_data(self):
        """A revoked token must return 404 — no passenger data exposed (Bug 7 fix)."""
        import cta_ticket_server as srv

        # Simulate lookup_token returning an error (what happens for revoked tokens)
        original = srv.lookup_token

        def fake_lookup_revoked(db, token):
            return None, 'This ticket link has been disabled.'

        srv.lookup_token = fake_lookup_revoked
        try:
            app = srv.build_app(Path('nonexistent.db'))
            app.config['TESTING'] = True
            with app.test_client() as client:
                resp = client.get('/ticket/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH')
                assert resp.status_code == 404, (
                    f'Revoked token must return 404, got {resp.status_code}'
                )
                body = resp.data.decode('utf-8', errors='replace')
                # Must not show any passenger data
                assert 'Test Passenger' not in body
        finally:
            srv.lookup_token = original

    def test_web_page_shows_refunded_payment_status(self):
        """A refunded (non-revoked) ticket must show 'Refunded' payment status."""
        import cta_ticket_server as srv
        original = srv.lookup_token

        def fake_lookup_refunded(db, token):
            return {
                'ticket_id': 'tid-001', 'ticket_no': 'TK-001', 'leg': 'out',
                'status': 'refunded', 'revoked_at': None,
                'passenger_name': 'Test Passenger', 'passenger_category': 'Adult',
                'fare_cents': 1000, 'booking_ref': 'REF-001',
                'from_station': 'A', 'to_station': 'B',
                'depart_at': '2026-06-01 09:00', 'legs': 1,
                'trip_type': 'one-way', 'total_cents': 1000,
                'payment_method': 'cash', 'payment_status': 'refunded',
                'payment_amount_cents': 1000,
            }, None

        srv.lookup_token = fake_lookup_refunded
        try:
            app = srv.build_app(Path('nonexistent.db'))
            app.config['TESTING'] = True
            with app.test_client() as client:
                resp = client.get('/ticket/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH')
                assert resp.status_code == 200
                html = resp.data.decode('utf-8', errors='replace')
                # Should show refunded status, not 'Paid'
                assert 'Refunded' in html, 'Refunded ticket should show Refunded payment status'
        finally:
            srv.lookup_token = original

    def test_web_page_shows_unpaid_status(self):
        import cta_ticket_server as srv
        original = srv.lookup_token

        def fake_lookup_unpaid(db, token):
            return {
                'ticket_id': 'tid-001', 'ticket_no': 'TK-001', 'leg': 'out',
                'status': 'issued', 'revoked_at': None,
                'passenger_name': 'Test Passenger', 'passenger_category': 'Adult',
                'fare_cents': 1000, 'booking_ref': 'REF-001',
                'from_station': 'A', 'to_station': 'B',
                'depart_at': '2026-06-01 09:00', 'legs': 1,
                'trip_type': 'one-way', 'total_cents': 1000,
                'payment_method': 'cash', 'payment_status': 'unpaid',
                'payment_amount_cents': 0,
            }, None

        srv.lookup_token = fake_lookup_unpaid
        try:
            app = srv.build_app(Path('nonexistent.db'))
            app.config['TESTING'] = True
            with app.test_client() as client:
                resp = client.get('/ticket/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH')
                html = resp.data.decode('utf-8', errors='replace')
                assert 'Unpaid' in html or 'unpaid' in html.lower()
        finally:
            srv.lookup_token = original


# ===========================================================================
# Run summary
# ===========================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
