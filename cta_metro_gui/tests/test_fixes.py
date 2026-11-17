"""Regression tests for the defects found in the 2.4.0 audit.

One test class per defect, each named after what used to go wrong, so a future change that reintroduces the bug
says so plainly. Nothing here depends on a display.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cta_accounts as acct            # noqa: E402
import cta_auth as auth                # noqa: E402
import cta_core as core                # noqa: E402
import cta_db                          # noqa: E402
import cta_payments as payments        # noqa: E402
import cta_routing as rt               # noqa: E402
import cta_sms as sms                  # noqa: E402
import cta_stats as stats              # noqa: E402

NOW = datetime(2026, 6, 1, 10, 0, 0)

# Digits a real passenger's keyboard may produce for "123456".
ARABIC_INDIC = '١٢٣٤٥٦'
EASTERN_ARABIC = '۱۲۳۴۵۶'
DEVANAGARI = '१२३४५६'
FULL_WIDTH = '１２３４５６'


# ====================================================================== 1. non-ASCII input crashed the comparisons
class TypedTextIsNeverFatalTests(unittest.TestCase):
    """hmac.compare_digest raises TypeError for a str holding non-ASCII characters. Anything a person types used to
    reach it directly, so one Arabic-Indic digit crashed the sign-in and the ticket lookup."""

    def test_secure_equal_accepts_any_text_and_still_compares(self):
        self.assertTrue(acct.secure_equal('123456', '123456'))
        self.assertTrue(acct.secure_equal('Zoë-Müller', 'Zoë-Müller'))
        self.assertFalse(acct.secure_equal('123456', '123457'))
        self.assertFalse(acct.secure_equal('رمز', 'code'))
        self.assertFalse(acct.secure_equal(None, 'x'))        # type: ignore[arg-type]
        self.assertFalse(acct.secure_equal('x', 12345))       # type: ignore[arg-type]

    def test_normalize_digits_maps_every_alphabet_to_ascii(self):
        for typed in (ARABIC_INDIC, EASTERN_ARABIC, DEVANAGARI, FULL_WIDTH):
            self.assertEqual(acct.normalize_digits(typed), '123456', typed)

    def test_normalize_digits_leaves_letters_and_non_decimal_digits_alone(self):
        self.assertEqual(acct.normalize_digits('Zoë Müller'), 'Zoë Müller')
        self.assertEqual(acct.normalize_digits('x²'), 'x²')   # a superscript is not a decimal digit
        self.assertEqual(acct.normalize_digits('CTA-20260601-001'), 'CTA-20260601-001')
        self.assertEqual(acct.normalize_digits(''), '')
        self.assertEqual(acct.normalize_digits(None), '')     # type: ignore[arg-type]

    def test_otp_rejects_non_ascii_without_crashing(self):
        session = acct.OtpSession('+447123456789', NOW)
        for typed in ('١٢٣٤٥٦', '12345é', 'رمز', FULL_WIDTH):
            ok, msg = session.verify(typed, NOW)              # used to raise TypeError
            self.assertIsInstance(ok, bool)
            self.assertIsInstance(msg, str)

    def test_otp_accepts_the_right_code_typed_in_arabic_indic_digits(self):
        session = acct.OtpSession('+447123456789', NOW)
        in_arabic = acct.normalize_digits  # the code is ASCII; map it the other way for the test
        table = {str(i): d for i, d in enumerate('٠١٢٣٤٥٦٧٨٩')}
        typed = ''.join(table[ch] for ch in session.demo_code)
        self.assertEqual(in_arabic(typed), session.demo_code)
        ok, msg = session.verify(typed, NOW)
        self.assertTrue(ok, msg)

    def test_otp_still_rejects_a_wrong_code_and_counts_the_try(self):
        session = acct.OtpSession('+447123456789', NOW)
        wrong = '000000' if session.demo_code != '000000' else '111111'
        before = session.attempts_left
        ok, _ = session.verify(wrong, NOW)
        self.assertFalse(ok)
        self.assertEqual(session.attempts_left, before - 1)

    def test_phone_number_may_be_typed_in_arabic_indic_digits(self):
        plain, err = acct.validate_phone('+44 7123 456789')
        self.assertEqual(err, '')
        arabic, err2 = acct.validate_phone('+٤٤ ٧١٢٣ ٤٥٦٧٨٩')
        self.assertEqual(err2, '')
        self.assertEqual(arabic, plain)

    def test_on_screen_sms_provider_does_not_crash_on_non_ascii(self):
        provider = sms.OnScreenCodes()
        provider.start('+447123456789', NOW)
        ok, msg = provider.check('+447123456789', 'رمز', NOW)
        self.assertFalse(ok)
        self.assertIsInstance(msg, str)


# ====================================================================== 2. the ticket lookup crashed and lost the try
class RecoveryCodeLookupTests(unittest.TestCase):
    def history(self):
        return [{'reference': 'CTA-20260601-001', 'recovery_code': 'A1B2C3D4E5F6A7B8',
                 'issued_at': '2026-06-01 09:00:00', 'total_cents': 4210}]

    def test_non_ascii_reference_or_code_is_simply_no_match(self):
        hist = self.history()
        for bad in ('CTA-é', 'ＣＴＡ', 'مرجع', 'A1B2C3D4E5F6A7Bé'):
            self.assertIsNone(core.find_ticket_with_code(hist, bad, 'A1B2C3D4E5F6A7B8'))   # used to raise TypeError
            self.assertIsNone(core.find_ticket_with_code(hist, 'CTA-20260601-001', bad))

    def test_the_right_reference_and_code_still_find_the_booking(self):
        hist = self.history()
        self.assertIs(core.find_ticket_with_code(hist, 'CTA-20260601-001', 'A1B2C3D4E5F6A7B8'), hist[0])
        self.assertIs(core.find_ticket_with_code(hist, ' cta-20260601-001 ', 'a1b2c3d4e5f6a7b8'), hist[0])

    def test_a_reference_typed_in_other_digits_still_finds_the_booking(self):
        hist = self.history()
        self.assertIs(core.find_ticket_with_code(hist, 'CTA-٢٠٢٦٠٦٠١-٠٠١', 'A1B2C3D4E5F6A7B8'), hist[0])

    def test_a_wrong_code_is_still_refused(self):
        hist = self.history()
        self.assertIsNone(core.find_ticket_with_code(hist, 'CTA-20260601-001', 'A1B2C3D4E5F6A7B9'))


# ====================================================================== 3. a provider bug could charge twice
class ChargeIsCalledExactlyOnceTests(unittest.TestCase):
    """``pay`` used to retry ``charge`` with fewer arguments whenever it raised TypeError. A TypeError from a bug
    INSIDE a live provider's adapter looks the same, so the passenger could be charged twice."""

    def _pay(self, provider):
        return payments.pay('card', 4210, 'CTA-20260601-001', '+447123456789', NOW,
                            provider=provider, environment=payments.DEVELOPMENT,
                            ledger=payments.PaymentLedger(None))

    def test_a_type_error_inside_the_provider_does_not_retry_the_charge(self):
        calls = []

        class BuggyAdapter(payments.PaymentProvider):
            name, live = 'buggy', True

            def charge(self, method, amount_cents, reference, phone, currency=payments.DEFAULT_CURRENCY,
                       idempotency_key=''):
                calls.append(reference)
                raise TypeError('bug inside the provider adapter')

            def verify(self, transaction_id):
                raise LookupError('never reached')

        record = self._pay(BuggyAdapter())
        self.assertEqual(len(calls), 1, 'charge() must be attempted exactly once')
        self.assertEqual(record['status'], payments.UNKNOWN)
        self.assertNotEqual(record['status'], payments.PAID)

    def test_an_old_four_argument_provider_is_still_supported(self):
        calls = []

        class OldProvider(payments.PaymentProvider):
            name, live = 'old', False

            def charge(self, method, amount_cents, reference, phone):
                calls.append(reference)
                return payments.PaymentResult(payments.PAID, 'TX-OLD-1', 'ok')

            def verify(self, transaction_id):
                return payments.VerifiedPayment(payments.PAID, 'TX-OLD-1', 4210,
                                                payments.DEFAULT_CURRENCY, 'CTA-20260601-001')

        record = self._pay(OldProvider())
        self.assertEqual(len(calls), 1)
        self.assertEqual(record['status'], payments.PAID)

    def test_a_provider_taking_kwargs_gets_the_currency(self):
        seen = {}

        class KwargsProvider(payments.PaymentProvider):
            name, live = 'kw', False

            def charge(self, method, amount_cents, reference, phone, **kw):
                seen.update(kw)
                return payments.PaymentResult(payments.PAID, 'TX-KW-1', 'ok')

            def verify(self, transaction_id):
                return payments.VerifiedPayment(payments.PAID, 'TX-KW-1', 4210, 'SAR', 'CTA-20260601-001')

        self._pay(KwargsProvider())
        self.assertEqual(seen.get('currency'), 'SAR')
        self.assertEqual(seen.get('idempotency_key'), 'CTA-20260601-001')

    def test_a_provider_returning_rubbish_is_never_treated_as_paid(self):
        class RubbishProvider(payments.PaymentProvider):
            name, live = 'rubbish', False

            def charge(self, *a, **k):
                return {'status': 'paid'}            # not a PaymentResult

            def verify(self, transaction_id):
                raise LookupError

        record = self._pay(RubbishProvider())
        self.assertEqual(record['status'], payments.UNKNOWN)


# ====================================================================== 4. invented return ticket numbers
class AdminRowsOnlyShowRealTicketsTests(unittest.TestCase):
    def test_a_two_leg_booking_without_a_return_ticket_shows_only_its_real_ticket(self):
        rec = {'reference': 'CTA-20260601-002', 'issued_at': '2026-06-01 09:00:00', 'total_cents': 8420,
               'legs': 2, 'from_station': 'Centrala', 'to_station': 'Yaen', 'route': ['Centrala', 'Yaen'],
               'depart_at': '2026-06-01 10:00',
               'passengers': [{'no': 1, 'name': 'A Passenger', 'category': 'Adult', 'fare_cents': 8420,
                               'leg_fare_cents': 4210, 'ticket_no': 'CTA-20260601-002-P1', 'status': 'issued'}]}
        numbers = [r['ticket_no'] for r in stats.ticket_rows([rec])]
        self.assertEqual(numbers, ['CTA-20260601-002-P1'])
        self.assertNotIn('CTA-20260601-002-P1R', numbers)      # used to invent this

    def test_a_complete_return_booking_still_shows_both_legs(self):
        rec = {'reference': 'CTA-20260601-003', 'issued_at': '2026-06-01 09:00:00', 'total_cents': 8420,
               'legs': 2, 'from_station': 'Centrala', 'to_station': 'Yaen', 'route': ['Centrala', 'Yaen'],
               'depart_at': '2026-06-01 10:00',
               'passengers': [{'no': 1, 'name': 'A Passenger', 'category': 'Adult', 'fare_cents': 8420,
                               'leg_fare_cents': 4210, 'ticket_no': 'CTA-20260601-003-P1', 'status': 'issued',
                               'return_ticket_no': 'CTA-20260601-003-P1R', 'return_status': 'issued'}]}
        self.assertEqual([r['ticket_no'] for r in stats.ticket_rows([rec])],
                         ['CTA-20260601-003-P1', 'CTA-20260601-003-P1R'])


# ====================================================================== 5. unreachable stations were over-reported
class NetworkValidationTests(unittest.TestCase):
    def test_the_real_network_is_valid(self):
        self.assertEqual(rt.validate_network(), [])

    def test_a_station_with_a_bad_position_is_reported_once_not_twice(self):
        stations = {'A': (0, 0), 'B': (100, 0), 'C': ('nonsense', 0)}
        problems = rt.validate_network(stations, (('A', 'B'), ('B', 'C')))
        self.assertTrue(any('no valid position' in p for p in problems))
        self.assertFalse(any('cannot be reached' in p for p in problems),
                         'C is linked to B, so it must not also be called unreachable')

    def test_a_genuinely_disconnected_station_is_still_reported(self):
        problems = rt.validate_network({'A': (0, 0), 'B': (100, 0), 'C': (200, 0)}, (('A', 'B'),))
        self.assertTrue(any('cannot be reached' in p for p in problems))


# ====================================================================== 6. sign-in sessions grew for ever
class SessionHousekeepingTests(unittest.TestCase):
    def setUp(self):
        # Another test module widens auth.SESSION_HOURS globally; pin the real value so this test is independent.
        self._hours = auth.SESSION_HOURS
        auth.SESSION_HOURS = 12
        self.folder = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.folder, NOW)
        self.admin = self.store.bootstrap_admin('boss', 'Secret123', NOW)
        self.much_later = NOW + timedelta(days=90)

    def tearDown(self):
        self.store.close()
        auth.SESSION_HOURS = self._hours

    def _session_count(self) -> int:
        return self.store.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]

    def test_an_old_expired_session_is_removed(self):
        self.assertEqual(self._session_count(), 1)
        self.assertEqual(self.store.prune_sessions(self.much_later), 1)
        self.assertEqual(self._session_count(), 0)

    def test_a_live_session_is_never_removed(self):
        self.assertEqual(self.store.prune_sessions(NOW + timedelta(hours=1)), 0)
        self.assertEqual(self._session_count(), 1)
        self.assertTrue(self.store.has_permission(self.admin, 'staff.manage', NOW))

    def test_pruning_keeps_the_audit_trail_of_who_signed_in(self):
        self.store.prune_sessions(self.much_later)
        kinds = [row['kind'] for row in self.store.read_audit(
            self.store.staff_login('boss', 'Secret123', self.much_later), self.much_later)]
        self.assertIn('auth.login', kinds)

    def test_a_read_only_store_prunes_nothing(self):
        self.store.close()
        read_only = cta_db.Store(self.folder / cta_db.DB_NAME, read_only=True)
        try:
            self.assertEqual(read_only.prune_sessions(self.much_later), 0)
        finally:
            read_only.close()
        self.store = cta_db.Store.open(self.folder, NOW)

    def test_the_new_indexes_exist(self):
        names = {r[0] for r in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        for index in ('scans_by_ticket_id', 'bookings_by_phone', 'sessions_by_user'):
            self.assertIn(index, names)


# ====================================================================== 7. the CLI left its session open on a refusal
class StaffCliSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        store = cta_db.Store.open(self.folder, NOW)
        store.staff_logout(store.bootstrap_admin('boss', 'Secret123', NOW), NOW)   # start with no open session
        store.close()

    def test_a_refused_command_still_closes_the_sign_in(self):
        import cta_staff
        lines = []
        rc = cta_staff.main(['role', 'boss', 'supervisor', '--folder', str(self.folder), '--as', 'boss'],
                            ask=lambda _p: 'Secret123', out=lines.append)
        self.assertEqual(rc, 1)                                    # 'supervisor' is not assignable yet
        self.assertTrue(any('REFUSED' in line for line in lines), lines)
        store = cta_db.Store.open(self.folder, NOW)
        try:
            open_sessions = store.conn.execute(
                'SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL').fetchone()[0]
        finally:
            store.close()
        self.assertEqual(open_sessions, 0, 'a refused CLI command must not leave a usable session behind')


if __name__ == '__main__':
    unittest.main()
