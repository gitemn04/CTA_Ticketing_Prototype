"""Tests for 2.5: admin filters, passenger refund requests, the staff inbox with replies, and QR codes that scan."""
import os
import sqlite3
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

from test_all import sign_in      # also sets the fast test settings
import cta_auth as auth
import cta_core as core
import cta_db
import cta_payments as pay
import cta_routing as rt
import cta_stats as stats
import cta_tickets as T

NOW = datetime(2026, 10, 25, 9, 0)
PHONE = '+447123456789'
OTHER = '+447000000000'


def audit_text(store) -> str:
    return '\n'.join(' '.join(str(v) for v in row) for row in store.conn.execute('SELECT * FROM audit'))


class SupportCase(unittest.TestCase):
    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.dir = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(self.store.close)
        self.admin = sign_in(self.store, 'admin')

    def issue(self, method='card', names=('Ann Lee',), legs=1, phone=PHONE):
        hist = self.store.load_history()
        route = rt.astar('Centrala', 'Zord')
        rec = core.make_record(route, {'Adult': len(names)}, 3, NOW, NOW, hist, names=list(names), legs=legs,
                               account_phone=phone)
        rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
        hist.append(rec)
        self.store.save_history(hist, NOW)
        return rec

    def booking(self, ref):
        return self.store.get_booking(ref)


# ====================================================================== filters in the admin ticket list
class TicketFilterTests(unittest.TestCase):
    def setUp(self):
        T.set_default_signer(None)
        self.addCleanup(T.set_default_signer, None)
        self.dir = Path(tempfile.mkdtemp())
        self.store = cta_db.Store.open(self.dir, NOW)
        self.addCleanup(self.store.close)
        self.admin = sign_in(self.store, 'admin')
        self.signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
        hist = self.store.load_history()
        self.refs = {}
        for name, method in (('Card Cara', 'card'), ('Mada Mo', 'mada'), ('Tamara Tia', 'tamara'),
                             ('Cash Cal', 'cash'), ('Cancel Cy', 'card'), ('Refund Ray', 'card')):
            rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, hist, names=[name],
                                   account_phone=PHONE)
            rec['payment'] = pay.pay(method, rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
            hist.append(rec)
            self.refs[name] = rec['reference']
        self.store.save_history(hist, NOW)
        self.tno = {n: self.store.get_booking(r)['passengers'][0]['ticket_no'] for n, r in self.refs.items()}
        self.store.check_in(self.tno['Card Cara'], signer=self.signer, now=NOW, operator=self.admin, environment='testing')
        self.store.cancel_ticket(self.tno['Cancel Cy'], self.admin, NOW, 'x')
        self.store.cancel_ticket(self.tno['Refund Ray'], self.admin, NOW, 'x')
        self.store.record_refund(self.tno['Refund Ray'], self.admin, NOW, 'REF-1')
        self.rows = stats.ticket_rows(self.store.load_history())

    def names(self, **kw):
        return {r['passenger'] for r in stats.filter_rows(self.rows, **kw)}

    def test_every_status_filter_shows_exactly_the_right_tickets(self):
        self.assertEqual(self.names(status='any'), set(self.tno))
        self.assertEqual(self.names(status='checked_in'), {'Card Cara'})
        self.assertEqual(self.names(status='not_used'), {'Mada Mo', 'Tamara Tia', 'Cash Cal'})
        self.assertEqual(self.names(status='unpaid'), {'Cash Cal'})
        self.assertEqual(self.names(status='refund_due'), {'Cancel Cy'})
        self.assertEqual(self.names(status='refunded'), {'Refund Ray'})
        self.assertEqual(self.names(status='cancelled'), {'Cancel Cy', 'Refund Ray'})
        self.assertEqual(self.names(status='paid'), set(self.tno) - {'Cash Cal'})

    def test_every_payment_method_filter(self):
        self.assertEqual(self.names(method='tamara'), {'Tamara Tia'})
        self.assertEqual(self.names(method='mada'), {'Mada Mo'})
        self.assertEqual(self.names(method='cash'), {'Cash Cal'})
        self.assertEqual(self.names(method='card'), {'Card Cara', 'Cancel Cy', 'Refund Ray'})
        self.assertEqual(self.names(method='paypal'), set())

    def test_filters_combine(self):
        self.assertEqual(self.names(status='refund_due', method='card'), {'Cancel Cy'})
        self.assertEqual(self.names(status='refund_due', method='tamara'), set())
        self.assertEqual(self.names(text='cara', status='checked_in'), {'Card Cara'})

    def test_typing_a_payment_method_or_a_state_into_search_now_finds_tickets(self):
        self.assertEqual(self.names(text='tamara'), {'Tamara Tia'})          # 2.4.x only searched names and numbers
        self.assertEqual(self.names(text='cash'), {'Cash Cal'})
        self.assertEqual(self.names(text='refunded'), {'Refund Ray'})
        self.assertEqual(self.names(text='refund due'), {'Cancel Cy'})
        self.assertEqual(self.names(text='mada paid'), {'Mada Mo'})
        self.assertEqual(self.names(text=self.tno['Mada Mo'].lower()), {'Mada Mo'})
        self.assertEqual(self.names(text='no such thing'), set())

    def test_unknown_filter_is_refused_not_ignored(self):
        with self.assertRaises(ValueError):
            stats.filter_rows(self.rows, status='everything')
        with self.assertRaises(ValueError):
            stats.filter_rows(self.rows, method='bitcoin')

    def test_the_filter_lists_cover_every_status_and_method_the_app_has(self):
        self.assertEqual({k for k, _ in stats.METHOD_FILTERS} - {'any'}, {m[0] for m in pay.METHODS})
        reachable = set().union(*(stats.ticket_state(r) for r in self.rows))
        self.assertTrue({'checked_in', 'not_used', 'paid', 'unpaid', 'cancelled', 'refund_due', 'refunded'} <= reachable)

    def test_the_csv_export_is_unchanged_by_the_extra_row_field(self):
        header = stats.rows_to_csv(self.rows).splitlines()[0]
        self.assertNotIn('method,', header.replace('payment_method', ''))


# ====================================================================== passenger messages
class MessageTests(SupportCase):
    def test_a_problem_report_is_stored_and_the_passenger_sees_the_reply(self):
        case = self.store.open_case('problem', 'The receipt printer jammed.', NOW, topic='Receipt or printer',
                                    account_phone=PHONE)
        inbox = self.store.list_cases(self.admin, NOW)
        self.assertEqual([c['id'] for c in inbox], [case])
        self.assertEqual(inbox[0]['status'], 'open')
        ok, _ = self.store.reply_case(case, self.admin, 'Sorry - we have sent an engineer.', NOW)
        self.assertTrue(ok)
        mine = self.store.cases_for_account(PHONE)
        self.assertEqual(mine[0]['status'], 'answered')
        self.assertEqual([(m['from_staff'], m['body']) for m in mine[0]['messages']],
                         [(False, 'The receipt printer jammed.'), (True, 'Sorry - we have sent an engineer.')])
        self.assertEqual(self.store.cases_for_account(OTHER), [])                    # nobody else sees it

    def test_the_audit_log_records_who_answered_but_never_the_message_text(self):
        case = self.store.open_case('problem', 'My secret complaint text', NOW, topic='Other', account_phone=PHONE)
        self.store.reply_case(case, self.admin, 'Our secret reply text', NOW)
        log = audit_text(self.store)
        self.assertIn('support.opened', log)
        self.assertIn('support.replied', log)
        self.assertNotIn('secret complaint', log)
        self.assertNotIn('secret reply', log)
        self.assertEqual(self.store.verify(), [])                                    # the audit chain is intact

    def test_bad_input_is_refused_before_anything_is_saved(self):
        for kw, text in (({'topic': 'Something invented'}, 'x'), ({'topic': 'Other'}, '   '),
                         ({'topic': 'Other'}, 'x' * 1001)):
            with self.subTest(text=text[:10], kw=kw), self.assertRaises(ValueError):
                self.store.open_case('problem', text, NOW, **kw)
        with self.assertRaises(ValueError):
            self.store.open_case('complaint', 'x', NOW, topic='Other')
        self.assertEqual(self.store.list_cases(self.admin, NOW), [])
        with self.assertRaises(ValueError):
            self.store.reply_case(1, self.admin, '', NOW)

    def test_only_signed_in_staff_with_the_permission_can_read_or_answer(self):
        case = self.store.open_case('problem', 'Help', NOW, topic='Other', account_phone=PHONE)
        with self.assertRaises(auth.PermissionDenied):
            self.store.list_cases(None, NOW)
        with self.assertRaises(auth.PermissionDenied):
            self.store.reply_case(case, None, 'hi', NOW)
        self.store.staff_logout(self.admin, NOW)
        with self.assertRaises(auth.PermissionDenied):                              # a kept, signed-out session
            self.store.reply_case(case, self.admin, 'hi', NOW)
        self.assertEqual(len(self.store.cases_for_account(PHONE)[0]['messages']), 1)

    def test_proposed_roles_other_than_admin_cannot_answer_passengers_yet(self):
        for role in ('supervisor', 'cashier', 'gate'):
            self.assertFalse(auth.role_allows(role, 'support.reply'))
        self.assertTrue(auth.role_allows('admin', 'support.reply'))

    def test_messages_can_never_be_edited_or_deleted(self):
        self.store.open_case('problem', 'Help', NOW, topic='Other', account_phone=PHONE)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute("UPDATE support_messages SET body = 'changed'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute('DELETE FROM support_messages')

    def test_an_anonymous_report_is_kept_for_staff_but_has_nowhere_to_send_a_reply(self):
        case = self.store.open_case('problem', 'Anonymous', NOW, topic='Other')
        self.assertEqual(self.store.list_cases(self.admin, NOW)[0]['account_phone'], None)
        self.assertTrue(self.store.reply_case(case, self.admin, 'Noted.', NOW)[0])


# ====================================================================== refund requests
class RefundRequestTests(SupportCase):
    def test_approving_cancels_the_unused_journeys_and_tells_the_passenger(self):
        rec = self.issue('card', ('Ann Lee', 'Bob Lee'))
        case = self.store.open_case('refund_request', 'Our plans changed.', NOW, account_phone=PHONE,
                                    reference=rec['reference'])
        ok, msg = self.store.decide_refund_request(case, self.admin, True, 'Sorry to see you go.', NOW)
        self.assertTrue(ok, msg)
        booking = self.booking(rec['reference'])
        self.assertEqual({p['status'] for p in booking['passengers']}, {'cancelled'})
        self.assertEqual({p['refund_state'] for p in booking['passengers']}, {'due'})
        self.assertEqual(sum(p['refund_cents'] for p in booking['passengers']), rec['total_cents'])
        seen = self.store.cases_for_account(PHONE)[0]
        self.assertEqual(seen['status'], 'approved')
        self.assertIn('approved', seen['messages'][-1]['body'])
        self.assertIn('Sorry to see you go.', seen['messages'][-1]['body'])
        self.assertIn('Refund due', seen['messages'][-1]['body'])
        rows = stats.filter_rows(stats.ticket_rows([booking]), status='refund_due')
        self.assertEqual(len(rows), 2)                                              # now visible under the filter
        self.assertTrue(self.store.record_refund(booking['passengers'][0]['ticket_no'], self.admin, NOW)[0])

    def test_declining_changes_no_ticket(self):
        rec = self.issue()
        case = self.store.open_case('refund_request', 'Please', NOW, account_phone=PHONE, reference=rec['reference'])
        ok, _ = self.store.decide_refund_request(case, self.admin, False, 'Too close to departure.', NOW)
        self.assertTrue(ok)
        self.assertEqual(self.booking(rec['reference'])['passengers'][0]['status'], 'issued')
        seen = self.store.cases_for_account(PHONE)[0]
        self.assertEqual(seen['status'], 'declined')
        self.assertIn('Too close to departure.', seen['messages'][-1]['body'])

    def test_a_request_is_decided_once(self):
        rec = self.issue()
        case = self.store.open_case('refund_request', 'Please', NOW, account_phone=PHONE, reference=rec['reference'])
        self.assertTrue(self.store.decide_refund_request(case, self.admin, True, '', NOW)[0])
        ok, msg = self.store.decide_refund_request(case, self.admin, False, '', NOW)
        self.assertFalse(ok)
        self.assertIn('already', msg)
        self.assertEqual(self.store.cases_for_account(PHONE)[0]['status'], 'approved')

    def test_a_passenger_cannot_ask_for_somebody_elses_booking_or_a_booking_with_nothing_to_refund(self):
        rec = self.issue()
        with self.assertRaises(ValueError):
            self.store.open_case('refund_request', 'Mine now', NOW, account_phone=OTHER, reference=rec['reference'])
        with self.assertRaises(ValueError):
            self.store.open_case('refund_request', 'No booking', NOW, account_phone=PHONE, reference='CTA-20260101-999')
        with self.assertRaises(ValueError):
            self.store.open_case('refund_request', 'Not signed in', NOW, reference=rec['reference'])
        self.store.cancel_booking(rec['reference'], self.admin, NOW, 'x')
        with self.assertRaises(ValueError) as cm:
            self.store.open_case('refund_request', 'Again', NOW, account_phone=PHONE, reference=rec['reference'])
        self.assertIn('already used or cancelled', str(cm.exception))
        self.assertEqual(self.store.list_cases(self.admin, NOW), [])

    def test_a_used_journey_stays_used_and_is_not_refunded(self):
        rec = self.issue('card', ('Ann Lee',), legs=2)
        signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
        self.store.check_in(rec['passengers'][0]['ticket_no'], signer=signer, now=NOW, operator=self.admin,
                            environment='testing')
        case = self.store.open_case('refund_request', 'Return not needed', NOW, account_phone=PHONE,
                                    reference=rec['reference'])
        self.assertTrue(self.store.decide_refund_request(case, self.admin, True, '', NOW)[0])
        p = self.booking(rec['reference'])['passengers'][0]
        self.assertEqual((p['status'], p['return_status']), ('boarded', 'cancelled'))
        self.assertEqual(p['return_refund_cents'], p['leg_fare_cents'])

    def test_only_one_open_request_per_booking_until_it_is_decided(self):
        rec = self.issue()
        case = self.store.open_case('refund_request', 'First', NOW, account_phone=PHONE, reference=rec['reference'])
        with self.assertRaises(ValueError) as cm:
            self.store.open_case('refund_request', 'Second', NOW, account_phone=PHONE, reference=rec['reference'])
        self.assertIn('already waiting', str(cm.exception))
        self.store.decide_refund_request(case, self.admin, False, '', NOW)
        self.assertTrue(self.store.open_case('refund_request', 'Third', NOW, account_phone=PHONE,
                                             reference=rec['reference']))

    def test_an_unpaid_counter_booking_is_cancelled_without_any_refund(self):
        rec = self.issue('cash')
        self.assertEqual(T.unused_journeys(rec), (1, 0))
        case = self.store.open_case('refund_request', 'Cancel it', NOW, account_phone=PHONE, reference=rec['reference'])
        self.assertTrue(self.store.decide_refund_request(case, self.admin, True, '', NOW)[0])
        p = self.booking(rec['reference'])['passengers'][0]
        self.assertEqual(p['status'], 'cancelled')
        self.assertFalse(p.get('refund_state'))

    def test_a_payment_the_provider_has_not_confirmed_cannot_be_refunded_yet(self):
        rec = self.issue()
        hist = self.store.load_history()
        hist[-1]['payment']['status'] = pay.PENDING
        self.store.save_history(hist, NOW, actor=self.admin)
        with self.assertRaises(ValueError):
            self.store.open_case('refund_request', 'x', NOW, account_phone=PHONE, reference=rec['reference'])

    def test_deciding_needs_both_the_inbox_permission_and_the_cancel_permission(self):
        rec = self.issue()
        case = self.store.open_case('refund_request', 'Please', NOW, account_phone=PHONE, reference=rec['reference'])
        with self.assertRaises(auth.PermissionDenied):
            self.store.decide_refund_request(case, None, True, '', NOW)
        self.assertEqual(self.booking(rec['reference'])['passengers'][0]['status'], 'issued')
        self.assertEqual(self.store.cases_for_account(PHONE)[0]['status'], 'open')
        with mock.patch.object(auth, 'ROLE_PERMISSIONS', {**auth.ROLE_PERMISSIONS, 'admin': frozenset(
                set(auth.PERMISSIONS) - {'ticket.cancel'})}):
            with self.assertRaises(auth.PermissionDenied):
                self.store.decide_refund_request(case, self.admin, True, '', NOW)
        self.assertEqual(self.booking(rec['reference'])['passengers'][0]['status'], 'issued')
        self.assertEqual(self.store.cases_for_account(PHONE)[0]['status'], 'open')    # all or nothing

    def test_two_windows_approving_at_once_cancel_once(self):
        rec = self.issue()
        case = self.store.open_case('refund_request', 'Please', NOW, account_phone=PHONE, reference=rec['reference'])
        other = cta_db.Store(self.dir / cta_db.DB_NAME)
        self.addCleanup(other.close)
        admin2 = other.staff_login('admin', __import__('test_all').TEST_PASSWORD, NOW)
        first = self.store.decide_refund_request(case, self.admin, True, '', NOW)
        second = other.decide_refund_request(case, admin2, True, '', NOW)
        self.assertEqual((first[0], second[0]), (True, False))
        p = self.booking(rec['reference'])['passengers'][0]
        self.assertEqual(p['refund_cents'], rec['total_cents'])                       # refunded once, not twice


# ====================================================================== older databases
class OlderDatabaseTests(SupportCase):
    def test_a_database_from_2_4_without_the_message_tables_is_upgraded_silently_and_keeps_everything(self):
        rec = self.issue()
        self.store.close()
        raw = sqlite3.connect(str(self.dir / cta_db.DB_NAME))
        raw.executescript('DROP TRIGGER support_messages_no_update; DROP TRIGGER support_messages_no_delete; '
                          'DROP TABLE support_messages; DROP TABLE support_cases;')
        raw.close()
        reopened = cta_db.Store(self.dir / cta_db.DB_NAME)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, 3)
        self.assertEqual(reopened.get_booking(rec['reference'])['reference'], rec['reference'])
        self.assertEqual(reopened.cases_for_account(PHONE), [])
        self.assertTrue(reopened.open_case('problem', 'Works', NOW, topic='Other', account_phone=PHONE))
        self.assertEqual(reopened.verify(), [])

    def test_a_read_only_old_database_just_shows_no_messages(self):
        self.store.close()
        raw = sqlite3.connect(str(self.dir / cta_db.DB_NAME))
        raw.executescript('DROP TRIGGER support_messages_no_update; DROP TRIGGER support_messages_no_delete; '
                          'DROP TABLE support_messages; DROP TABLE support_cases;')
        raw.close()
        ro = cta_db.Store(self.dir / cta_db.DB_NAME, read_only=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.cases_for_account(PHONE), [])

    def test_backups_carry_the_messages(self):
        self.store.open_case('problem', 'Keep me', NOW, topic='Other', account_phone=PHONE)
        backup = self.store.backup('test', now=NOW)
        copy = cta_db.Store(backup, read_only=True, allow_old=True)
        self.addCleanup(copy.close)
        self.assertEqual(copy.cases_for_account(PHONE)[0]['messages'][0]['body'], 'Keep me')


# ====================================================================== QR codes
class QrCodeTests(unittest.TestCase):
    def setUp(self):
        try:
            import cv2
            import numpy
        except ImportError:
            self.skipTest('opencv / numpy not installed')
        self.cv2, self.np = cv2, numpy
        self.token = T.TicketSigner([b'k' * 32]).sign('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

    def decode(self, img):
        value, _pts, _ = self.cv2.QRCodeDetector().detectAndDecode(self.np.array(img.convert('L')))
        return value

    def test_the_qr_is_drawn_with_whole_pixels_per_square_and_is_big_enough(self):
        import qrcode
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(self.token)
        q.make(fit=True)
        squares = q.modules_count + 8
        for wanted in (150, 240, 300):
            img = core.qr_image(self.token, wanted)
            self.assertEqual(img.size[0] % squares, 0, 'a fractional scale smears the squares')
            self.assertGreaterEqual(img.size[0] // squares, 5)          # whole pixels, at least 5 per square
            self.assertGreaterEqual(img.size[0], wanted)
            self.assertEqual(img.size[0], img.size[1])

    def test_the_qr_decodes_exactly_and_survives_blur_and_shrinking(self):
        from PIL import ImageFilter
        img = core.qr_image(self.token, 240)
        self.assertEqual(self.decode(img), self.token)
        self.assertEqual(self.decode(img.filter(ImageFilter.GaussianBlur(1.5))), self.token)     # 2.4.1 failed here
        half = img.resize((img.size[0] // 2, img.size[1] // 2))
        self.assertEqual(self.decode(half), self.token)

    def test_the_qr_in_the_pdf_decodes_too(self):
        try:
            import fitz
        except ImportError:
            self.skipTest('PyMuPDF not installed to render the PDF page')
        signer = T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])])
        store = cta_db.Store.open(Path(tempfile.mkdtemp()), NOW)
        self.addCleanup(store.close)
        hist = store.load_history()
        rec = core.make_record(rt.astar('Centrala', 'Zord'), {'Adult': 1}, 3, NOW, NOW, hist, names=['Ann Lee'],
                               account_phone=PHONE)
        rec['payment'] = pay.pay('card', rec['total_cents'], rec['reference'], 'x', NOW, environment='testing')
        hist.append(rec)
        store.save_history(hist, NOW)
        rec = store.get_booking(rec['reference'])
        path = Path(tempfile.mkdtemp()) / 't.pdf'
        core.export_ticket_pdf(rec, path, include_receipt=False, signer=signer)
        page = fitz.open(str(path))[0]
        pix = page.get_pixmap(dpi=200)
        arr = self.np.frombuffer(pix.samples, dtype=self.np.uint8).reshape(pix.height, pix.width, pix.n)
        got, _pts, _ = self.cv2.QRCodeDetector().detectAndDecode(arr)
        self.assertEqual(got, T.qr_payload(rec, rec['passengers'][0], 'out', signer))


# ====================================================================== printing the voucher
class PrintTests(SupportCase):
    """The printer itself cannot be tested here: these prove what the app sends and what happens when Windows says no."""

    def make_pdf(self, receipt):
        rec = self.issue('card', ('Ann Lee', 'Bob Lee'), legs=2)
        path = self.dir / f'{rec["reference"]}_{"r" if receipt else "p"}.pdf'
        core.export_ticket_pdf(self.booking(rec['reference']), path, include_receipt=receipt,
                               signer=T.TicketSigner([bytes.fromhex(os.environ['CTA_TICKET_KEY'])]))
        return path

    def pages(self, path):
        try:
            import pypdf
        except ImportError:
            self.skipTest('pypdf not installed')
        return len(pypdf.PdfReader(str(path)).pages)

    def test_the_print_copy_has_one_page_per_passenger_and_journey_and_no_receipt(self):
        self.assertEqual(self.pages(self.make_pdf(False)), 4)          # 2 passengers x outbound + return
        self.assertEqual(self.pages(self.make_pdf(True)), 5)           # the saved PDF adds the receipt page

    def test_windows_sends_the_pdf_to_the_default_printer(self):
        path = self.make_pdf(False)
        with mock.patch('cta_core.sys.platform', 'win32'), \
             mock.patch('cta_core.os.startfile', create=True) as start:
            ok, msg = core.print_pdf(path)
        start.assert_called_once_with(str(path), 'print')
        self.assertTrue(ok)
        self.assertIn('default printer', msg)

    def test_when_windows_has_no_print_handler_the_pdf_opens_so_ctrl_p_works(self):
        path = self.make_pdf(False)
        opened = []

        def startfile(target, verb=None):
            if verb == 'print':
                raise OSError('no application is associated with the specified file for this operation')
            opened.append(target)
        with mock.patch('cta_core.sys.platform', 'win32'), mock.patch('cta_core.os.startfile', startfile, create=True):
            ok, msg = core.print_pdf(path)
        self.assertTrue(ok)
        self.assertEqual(opened, [str(path)])
        self.assertIn('Ctrl+P', msg)

    def test_when_nothing_can_open_it_the_message_says_where_the_file_is(self):
        path = self.make_pdf(False)
        with mock.patch('cta_core.sys.platform', 'win32'), \
             mock.patch('cta_core.os.startfile', side_effect=OSError('nothing'), create=True):
            ok, msg = core.print_pdf(path)
        self.assertFalse(ok)
        self.assertIn(str(path), msg)


# ====================================================================== the screens
class ScreenTests(unittest.TestCase):
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
        self.box = p.start()
        self.addCleanup(p.stop)
        self.box.askyesno.return_value = True
        q = mock.patch('cta_admin.messagebox', self.box)             # the admin page has its own import of messagebox
        q.start()
        self.addCleanup(q.stop)

    def app(self):
        from cta_app import CTAApp
        app = CTAApp(self.root, clock=lambda: NOW, folder=self.tmp)
        if getattr(app, '_problems_after', None):
            self.root.after_cancel(app._problems_after)
        return app

    def book(self, app, method='card', names=('Ann Lee',)):
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(len(names))
        app.account = {'phone': PHONE, 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        app._issue(list(names), method)
        return app.history[-1]

    def test_the_problem_form_is_for_passengers_only(self):
        app = self.app()
        self.root.update()
        self.assertTrue(app.report_card.winfo_manager())
        app.staff = sign_in(app.store, 'admin', now=NOW)
        app._paint_account()
        self.root.update()
        self.assertFalse(app.report_card.winfo_manager())                         # hidden for staff
        self.assertTrue(app.staff_note_card.winfo_manager())
        app.sign_out()
        self.root.update()
        self.assertTrue(app.report_card.winfo_manager())                          # back for passengers

    def test_a_signed_in_passengers_report_reaches_the_inbox_and_the_reply_reaches_them(self):
        app = self.app()
        app.account = {'phone': PHONE, 'name': 'Jane Doe'}
        app.issue_text.insert('1.0', 'The gate did not open.')
        app.issue_type.current(2)
        app.send_issue()
        self.assertIn('Staff replies appear', app.issue_msg.cget('text'))
        app.staff = sign_in(app.store, 'admin', now=NOW)
        app._paint_account()
        app.show_page('admin')
        page = app.admin
        page.refresh()
        self.assertEqual(len(page.messages_tab.tree.get_children()), 1)
        self.assertIn('(1)', page.nb.tab(page.messages_tab, 'text'))              # the tab shows how many are waiting
        page.messages_tab.tree.selection_set(page.messages_tab.tree.get_children()[0])
        page.messages_tab.reply_selected('Thanks - the gate has been repaired.')
        app.sign_out()
        app.account = {'phone': PHONE, 'name': 'Jane Doe'}
        app.refresh_my_tickets()
        shown = ' '.join(w.cget('text') for w in app.my_list.winfo_children() if w.winfo_class() == 'Label')
        self.assertIn('YOUR MESSAGES', shown)
        self.assertIn('Thanks - the gate has been repaired.', shown)
        self.assertIn('Answered', shown)

    def test_an_anonymous_report_says_a_reply_needs_sign_in(self):
        app = self.app()
        app.issue_text.insert('1.0', 'Fare looks wrong.')
        app.send_issue()
        self.assertIn('Sign in before sending', app.issue_msg.cget('text'))

    def test_the_passenger_button_and_the_admin_decision_end_to_end(self):
        app = self.app()
        rec = self.book(app, 'tamara', ('Ann Lee', 'Bob Lee'))
        ok, text = app.submit_refund_request(rec, 'I am ill.')
        self.assertTrue(ok, text)
        self.assertIn('number', text)
        ok, text = app.submit_refund_request(rec, 'again')
        self.assertFalse(ok)
        self.assertIn('already waiting', text)
        app.staff = sign_in(app.store, 'admin', now=NOW)
        app._paint_account()
        app.show_page('admin')
        page = app.admin
        page.refresh()
        tab = page.refunds_tab
        iid = tab.tree.get_children()[0]
        tab.tree.selection_set(iid)
        values = tab.tree.item(iid, 'values')
        self.assertEqual(values[1], rec['reference'])
        self.assertEqual(values[3], '2')                                         # unused journeys
        self.assertEqual(values[4], core.format_cents(rec['total_cents']))        # refund if approved
        tab.decide_selected(True, 'We are sorry.')
        # the admin filter now finds the two refunds waiting to be paid out
        page.status_box.set('Refund to pay out')
        page._fill_tickets()
        self.assertEqual(len(page.tree.get_children()), 2)
        page.method_box.set('Tamara')
        page._fill_tickets()
        self.assertEqual(len(page.tree.get_children()), 2)
        page.method_box.set('mada')
        page._fill_tickets()
        self.assertEqual(len(page.tree.get_children()), 0)
        self.assertIn('Showing 0 of 2', page.count_lbl.cget('text'))
        page.clear_filters()
        self.assertIn('Showing 2 of 2', page.count_lbl.cget('text'))

    def test_the_refund_button_appears_only_when_it_can_be_used(self):
        app = self.app()
        rec = self.book(app)
        def buttons(record, allow):
            frame = app.ticket_view(self.root, record, allow_refund_request=allow)
            self.root.update_idletasks()
            texts = []
            def walk(w):
                for c in w.winfo_children():
                    texts.append(getattr(c, 'text', None) or (c.cget('text') if c.winfo_class() == 'Label' else ''))
                    walk(c)
            walk(frame)
            frame.destroy()
            return texts
        self.assertIn('Request refund', buttons(rec, True))
        self.assertNotIn('Request refund', buttons(rec, False))                    # not on the booking confirmation
        cancelled = dict(rec)
        cancelled['passengers'] = [dict(p, status='cancelled') for p in rec['passengers']]
        self.assertNotIn('Request refund', buttons(cancelled, True))               # nothing left to refund

    def test_a_signed_out_visitor_cannot_request_a_refund_without_the_recovery_code(self):
        app = self.app()
        rec = self.book(app)
        app.account = None
        app._verified_by_code = False
        label = mock.Mock()
        app.request_refund(rec, label)
        self.assertIn('Sign in', label.config.call_args[1]['text'])
        self.assertEqual(app.store.list_cases(sign_in(app.store, 'admin', now=NOW), NOW), [])


if __name__ == '__main__':
    unittest.main()
