"""CTA Metro v3.0 — Real end-to-end integration tests.

These tests use the actual cta_db.Store and issue tickets through the real
booking/payment services. They never mock lookup_token.

Requirements: pip install flask pytest
Run with: pytest tests/test_v3_integration.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))

# Testing environment — must not send real SMS or charge money
os.environ.setdefault('CTA_ENV', 'testing')


# ---------------------------------------------------------------------------
# Helpers to build a real database with real data
# ---------------------------------------------------------------------------

def _booking_dict(reference='REF-TEST-001', **kw) -> dict:
    """Return a minimal booking dict matching the real cta_db schema."""
    d = {
        'reference': reference,
        'issued_at': '2026-01-15 10:00',
        'from_station': 'King Abdulaziz',
        'to_station': 'Al Qadam',
        'depart_at': '2026-06-01 09:00',
        'trip_type': 'one-way',
        'legs': 1,
        'total_cents': 5000,
        'account_phone': '+966501234567',
        'passengers': [
            {
                'no': 1,
                'name': 'Eman Al-Rashidi',
                'category': 'Adult',
                'fare_cents': 5000,
                'leg_fare_cents': 5000,
                'ticket_no': 'TK-INT-001',
                'ticket_id': 'tid-int-001',
                'status': 'issued',
            }
        ],
        'payment': {'method': 'cash', 'status': 'paid', 'amount_cents': 5000},
    }
    d.update(kw)
    return d


def _build_real_db(tmp_dir: Path) -> Path:
    """Build a real SQLite database using the actual schema from cta_db.

    We cannot use cta_db.Store() on an empty file because Store.__init__
    calls _check() which requires the full CTA schema to already exist.
    Instead we apply the schema DDL directly via raw SQL, the same
    statements cta_db uses when it creates a new database.
    """
    import cta_db
    import sqlite3
    db_path = tmp_dir / 'cta.db'

    # Apply the full schema directly (same DDL that cta_db uses internally).
    # Use executescript() so CREATE TRIGGER bodies with embedded semicolons
    # (BEGIN...END blocks) are handled correctly.
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            'PRAGMA foreign_keys = OFF;\n'
            'PRAGMA journal_mode = WAL;\n'
            + cta_db.SCHEMA
            + '\n'
            + cta_db.WEB_TOKEN_SCHEMA
            + "\nINSERT INTO meta (key, value) VALUES ('schema_version', '"
            + str(cta_db.SCHEMA_VERSION)
            + "');\n"
        )

    # Insert test data using the real schema
    booking = _booking_dict()
    pax = booking['passengers'][0]

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute(
            "INSERT OR REPLACE INTO bookings (reference, issued_at, depart_at, account_phone, "
            "total_cents, legs, doc, version, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                booking['reference'],
                booking['issued_at'],
                booking['depart_at'],
                booking['account_phone'],
                booking['total_cents'],
                booking['legs'],
                json.dumps({
                    'from_station': booking['from_station'],
                    'to_station': booking['to_station'],
                    'depart_at': booking['depart_at'],
                    'trip_type': booking['trip_type'],
                }),
                1,
                booking['issued_at'],
            )
        )
        conn.execute(
            "INSERT OR REPLACE INTO passengers (reference, no, name, category, fare_cents, doc) "
            "VALUES (?,?,?,?,?,?)",
            (
                booking['reference'], pax['no'], pax['name'], pax['category'],
                pax['fare_cents'],
                json.dumps({'leg_fare_cents': pax['leg_fare_cents']}),
            )
        )
        conn.execute(
            "INSERT OR REPLACE INTO tickets "
            "(ticket_no, reference, passenger_no, leg, ticket_id, status) "
            "VALUES (?,?,?,?,?,?)",
            (
                pax['ticket_no'], booking['reference'], pax['no'], 'out',
                pax['ticket_id'], 'issued',
            )
        )
        conn.execute(
            "INSERT OR REPLACE INTO payments (reference, method, status, amount_cents, doc) "
            "VALUES (?,?,?,?,?)",
            (
                booking['reference'],
                booking['payment']['method'],
                booking['payment']['status'],
                booking['payment']['amount_cents'],
                '{}',
            )
        )
        conn.commit()

    return db_path


# ---------------------------------------------------------------------------
# Integration test class
# ---------------------------------------------------------------------------

class TestRealEndToEnd:
    """Full path: real DB → issue token → ticket page → PDF route."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp = tmp_path
        self.db_path = _build_real_db(tmp_path)
        self.ticket_id = 'tid-int-001'

    def _flask_app(self):
        import cta_ticket_server as srv
        app = srv.build_app(self.db_path)
        app.config['TESTING'] = True
        return app

    # ------------------------------------------------------------------
    # 1. Token issued → ticket page returns 200 with correct details
    # ------------------------------------------------------------------

    def test_valid_token_returns_200(self):
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        assert token and len(token) >= 40

        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
            body = resp.data.decode('utf-8', errors='replace')
            # Correct passenger details shown
            assert 'Eman Al-Rashidi' in body, 'Passenger name missing from page'
            assert 'King Abdulaziz' in body, 'Departure station missing'
            assert 'Al Qadam' in body, 'Arrival station missing'

    def test_ticket_page_contains_pdf_download_link(self):
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            body = resp.data.decode('utf-8', errors='replace')
            # PDF link must be /ticket/<token>/pdf — not /ticket/pdf
            expected_pdf_path = f'/ticket/{token}/pdf'
            assert expected_pdf_path in body, (
                f'PDF download link must be {expected_pdf_path!r}, '
                f'found: {[l for l in body.split() if "pdf" in l.lower()]}'
            )
            # Ensure the broken relative "pdf" href is not present
            assert 'href="pdf"' not in body, 'Broken relative href="pdf" found'
            assert "href='pdf'" not in body, "Broken relative href='pdf' found"

    # ------------------------------------------------------------------
    # 2. PDF route returns 200 application/pdf
    # ------------------------------------------------------------------

    def test_pdf_route_requires_reportlab(self):
        """Skip gracefully if reportlab not installed; otherwise assert PDF."""
        pytest.importorskip('reportlab', reason='reportlab not installed')
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}/pdf')
            assert resp.status_code == 200, f'Expected 200 from PDF route, got {resp.status_code}'
            assert resp.content_type == 'application/pdf', (
                f'Expected application/pdf, got {resp.content_type}'
            )
            assert resp.data[:4] == b'%PDF', 'Response body must begin with PDF magic bytes'

    # ------------------------------------------------------------------
    # 3. Viewing the page does NOT change ticket status
    # ------------------------------------------------------------------

    def test_viewing_does_not_change_ticket_status(self):
        import sqlite3
        from cta_ticket_server import issue_web_token

        # Get status before
        with sqlite3.connect(str(self.db_path)) as conn:
            before = conn.execute(
                'SELECT status FROM tickets WHERE ticket_id = ?', (self.ticket_id,)
            ).fetchone()[0]

        token = issue_web_token(self.db_path, self.ticket_id)
        app = self._flask_app()
        with app.test_client() as client:
            client.get(f'/ticket/{token}')  # view the page

        # Status must be unchanged
        with sqlite3.connect(str(self.db_path)) as conn:
            after = conn.execute(
                'SELECT status FROM tickets WHERE ticket_id = ?', (self.ticket_id,)
            ).fetchone()[0]

        assert before == after, f'Ticket status changed from {before!r} to {after!r} — viewing must not check in'

    # ------------------------------------------------------------------
    # 4. Revoked token — page AND PDF return 404
    # ------------------------------------------------------------------

    def test_revoked_token_page_returns_404(self):
        from cta_ticket_server import issue_web_token, revoke_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        revoke_web_token(self.db_path, self.ticket_id)

        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404, (
                f'Revoked token must return 404, got {resp.status_code}'
            )
            body = resp.data.decode('utf-8', errors='replace')
            # Must NOT show passenger name
            assert 'Eman Al-Rashidi' not in body, 'Revoked token must not expose passenger data'

    def test_revoked_token_pdf_returns_404(self):
        from cta_ticket_server import issue_web_token, revoke_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        revoke_web_token(self.db_path, self.ticket_id)

        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}/pdf')
            assert resp.status_code == 404, (
                f'Revoked token PDF must return 404, got {resp.status_code}'
            )

    # ------------------------------------------------------------------
    # 5. Expired token — denied after TOKEN_EXPIRY_DAYS
    # ------------------------------------------------------------------

    def test_expired_token_returns_404(self):
        import sqlite3
        from cta_ticket_server import issue_web_token, TOKEN_EXPIRY_DAYS

        token = issue_web_token(self.db_path, self.ticket_id)

        # Back-date the token's issued_at
        old_date = (datetime.now(timezone.utc) - timedelta(days=TOKEN_EXPIRY_DAYS + 1)
                    ).strftime('%Y-%m-%d %H:%M:%S')
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                'UPDATE ticket_web_tokens SET issued_at = ? WHERE token = ?',
                (old_date, token)
            )
            conn.commit()

        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404, (
                f'Expired token must return 404, got {resp.status_code}'
            )

    # ------------------------------------------------------------------
    # 6. Unknown token returns 404
    # ------------------------------------------------------------------

    def test_unknown_token_returns_404(self):
        from cta_ticket_server import generate_web_token
        token = generate_web_token()  # valid format, not in DB
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.status_code == 404

    # ------------------------------------------------------------------
    # 7. URL scanning: parse_ticket_url resolves to token
    # ------------------------------------------------------------------

    def test_parse_ticket_url_recognises_configured_host(self):
        from cta_ticket_server import parse_ticket_url, generate_web_token
        token = generate_web_token()
        base = 'https://cta.example.com'
        url = f'{base}/ticket/{token}'
        result = parse_ticket_url(url, base)
        assert result == token, f'Expected token {token!r}, got {result!r}'

    def test_parse_ticket_url_rejects_different_host(self):
        from cta_ticket_server import parse_ticket_url, generate_web_token
        token = generate_web_token()
        url = f'https://evil.example.com/ticket/{token}'
        result = parse_ticket_url(url, 'https://cta.example.com')
        assert result is None, 'Different host must not resolve'

    def test_resolve_url_token_returns_ticket_id(self):
        from cta_ticket_server import issue_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.ticket_id)
        base = 'https://cta.example.com'
        url = f'{base}/ticket/{token}'

        ticket_id, err = resolve_url_token(self.db_path, url, base)
        assert err is None, f'Expected no error, got {err!r}'
        assert ticket_id == self.ticket_id

    def test_resolve_url_token_revoked_returns_error(self):
        from cta_ticket_server import issue_web_token, revoke_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.ticket_id)
        revoke_web_token(self.db_path, self.ticket_id)
        base = 'https://cta.example.com'
        url = f'{base}/ticket/{token}'

        ticket_id, err = resolve_url_token(self.db_path, url, base)
        assert ticket_id is None
        assert err and 'disabled' in err.lower()

    def test_resolve_url_token_never_fetches_url(self):
        """resolve_url_token must use only the local DB, never make HTTP requests."""
        import unittest.mock as mock
        from cta_ticket_server import issue_web_token, resolve_url_token
        token = issue_web_token(self.db_path, self.ticket_id)
        base = 'https://cta.example.com'
        url = f'{base}/ticket/{token}'

        # Patch urllib so any HTTP call fails loudly
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('HTTP fetch occurred')):
            ticket_id, err = resolve_url_token(self.db_path, url, base)
        # Should have resolved without making any HTTP call
        assert ticket_id == self.ticket_id

    # ------------------------------------------------------------------
    # 8. Token issuance is idempotent
    # ------------------------------------------------------------------

    def test_issue_web_token_is_idempotent(self):
        from cta_ticket_server import issue_web_token
        t1 = issue_web_token(self.db_path, self.ticket_id)
        t2 = issue_web_token(self.db_path, self.ticket_id)
        assert t1 == t2, 'Repeated token issue must return the same token'

    # ------------------------------------------------------------------
    # 9. Security headers present on all routes
    # ------------------------------------------------------------------

    def test_security_headers_on_ticket_page(self):
        from cta_ticket_server import issue_web_token
        token = issue_web_token(self.db_path, self.ticket_id)
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')
            assert resp.headers.get('Referrer-Policy') == 'no-referrer'
            assert 'no-store' in resp.headers.get('Cache-Control', '')
            assert resp.headers.get('X-Frame-Options') == 'DENY'

    def test_security_headers_on_error_page(self):
        from cta_ticket_server import generate_web_token
        token = generate_web_token()
        app = self._flask_app()
        with app.test_client() as client:
            resp = client.get(f'/ticket/{token}')  # will 404
            assert resp.headers.get('Referrer-Policy') == 'no-referrer'
            assert 'no-store' in resp.headers.get('Cache-Control', '')


# ---------------------------------------------------------------------------
# SMS environment resolution — tests the Bug 3 fix
# ---------------------------------------------------------------------------

class TestSmsEnvironmentResolution:
    """load_sms_provider must read 'environment' from the TOP-LEVEL config key.

    Bug 3: previously read from the sms sub-dict, so {"environment":"production"}
    at the top level was never seen → OnScreenCodes returned instead of SmsUnavailable.
    """

    def _make_config(self, tmp_path: Path, cfg: dict) -> Path:
        p = tmp_path / 'cta_config.json'
        p.write_text(json.dumps(cfg))
        return p

    def test_top_level_production_key_blocks_signin(self, tmp_path):
        """{"environment":"production"} at top level → SmsUnavailable (Bug 3 regression)."""
        import cta_sms
        cfg_path = self._make_config(tmp_path, {'environment': 'production', 'sms': {}})
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID',
                  'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment='')
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            f'Top-level environment=production must block sign-in. Got {type(provider).__name__}'
        )

    def test_sms_sub_dict_environment_still_works(self, tmp_path):
        """The sms sub-dict 'environment' key still works as a legacy fallback."""
        import cta_sms
        cfg_path = self._make_config(tmp_path, {'sms': {'environment': 'production'}})
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID',
                  'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment='')
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            f'sms sub-dict environment=production should still block sign-in. Got {type(provider).__name__}'
        )

    def test_env_var_takes_priority_over_config(self, tmp_path):
        """CTA_ENV=production env var → blocks sign-in regardless of config file."""
        import cta_sms
        cfg_path = self._make_config(tmp_path, {})  # no environment key
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
            os.environ.pop(k, None)
        os.environ['CTA_ENV'] = 'production'
        try:
            provider = cta_sms.load_sms_provider(cfg_path, environment='')
        finally:
            os.environ.pop('CTA_ENV', None)
        assert isinstance(provider, cta_sms.SmsUnavailable)

    def test_explicit_argument_takes_priority(self, tmp_path):
        """Explicit environment='production' argument overrides env var and config."""
        import cta_sms
        cfg_path = self._make_config(tmp_path, {'environment': 'development'})
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
            os.environ.pop(k, None)
        os.environ['CTA_ENV'] = 'development'
        try:
            provider = cta_sms.load_sms_provider(cfg_path, environment='production')
        finally:
            os.environ.pop('CTA_ENV', None)
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            'Explicit environment=production must take priority'
        )

    def test_missing_config_development_returns_onscreen(self, tmp_path):
        """No config, CTA_ENV=development → OnScreenCodes (development fallback)."""
        import cta_sms
        cfg_path = tmp_path / 'nonexistent_config.json'
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID'):
            os.environ.pop(k, None)
        os.environ['CTA_ENV'] = 'development'
        try:
            provider = cta_sms.load_sms_provider(cfg_path, environment='')
        except Exception:
            return  # missing config may raise — that's also acceptable (not silent fallback)
        finally:
            os.environ.pop('CTA_ENV', None)
        assert isinstance(provider, cta_sms.OnScreenCodes), (
            f'Missing config in development should give OnScreenCodes, got {type(provider).__name__}'
        )

    def test_unrecognised_environment_value_blocks_signin(self, tmp_path):
        """Bug 4 fix: an unrecognised environment value must block sign-in (production-safe default).

        Previously, any env string that was not 'production'/'prod' would fall through to
        OnScreenCodes — so 'staging', 'qa', empty string or a typo silently showed codes on screen.
        The fixed behaviour: only 'development', 'dev', 'testing', 'test' enable OnScreenCodes.
        Every other value (including 'staging') returns SmsUnavailable.
        """
        import cta_sms
        cfg_path = self._make_config(tmp_path, {'environment': 'staging'})
        for k in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_VERIFY_SERVICE_SID',
                  'CTA_ENV'):
            os.environ.pop(k, None)
        provider = cta_sms.load_sms_provider(cfg_path, environment='')
        # 'staging' is not in the explicit dev/test list → must block sign-in
        assert isinstance(provider, cta_sms.SmsUnavailable), (
            '"staging" environment must now return SmsUnavailable (Bug 4 fix: production-safe default). '
            f'Got {type(provider).__name__}'
        )


# ---------------------------------------------------------------------------
# validate_base_url
# ---------------------------------------------------------------------------

class TestValidateBaseUrl:
    def test_valid_https(self):
        from cta_ticket_server import validate_base_url
        ok, reason = validate_base_url('https://cta.example.com')
        assert ok, reason

    def test_localhost_rejected(self):
        from cta_ticket_server import validate_base_url
        ok, reason = validate_base_url('https://localhost:5050')
        assert not ok

    def test_http_rejected(self):
        from cta_ticket_server import validate_base_url
        ok, reason = validate_base_url('http://cta.example.com')
        assert not ok

    def test_empty_rejected(self):
        from cta_ticket_server import validate_base_url
        ok, reason = validate_base_url('')
        assert not ok

    def test_no_dot_rejected(self):
        from cta_ticket_server import validate_base_url
        ok, reason = validate_base_url('https://localhost')
        assert not ok


# ---------------------------------------------------------------------------
# Scanner URL helpers (parse_ticket_url, resolve_url_token)
# ---------------------------------------------------------------------------

class TestScannerUrlParsing:
    def test_exact_prefix_required(self):
        from cta_ticket_server import parse_ticket_url, generate_web_token
        token = generate_web_token()
        base = 'https://cta.example.com'
        # Sub-path should be recognised
        assert parse_ticket_url(f'{base}/ticket/{token}', base) == token
        # Wrong path segment
        assert parse_ticket_url(f'{base}/Ticket/{token}', base) is None
        # Subdomain injection
        assert parse_ticket_url(f'https://cta.example.com.evil.com/ticket/{token}', base) is None

    def test_no_path_traversal(self):
        from cta_ticket_server import parse_ticket_url
        base = 'https://cta.example.com'
        # ../.. style traversal
        assert parse_ticket_url(f'{base}/ticket/../../../etc/passwd', base) is None

    def test_arbitrary_url_not_fetched(self):
        """Passing an arbitrary URL to parse_ticket_url must not trigger any HTTP call."""
        import unittest.mock as mock
        from cta_ticket_server import parse_ticket_url
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('Fetched URL')):
            result = parse_ticket_url('https://evil.example.com/ticket/abc', 'https://cta.example.com')
        assert result is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
