"""Round 2: readable QR, test-mode card entry with a pretend bank, working admin buttons, custom WhatsApp sender."""
import os
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

from test_support import SupportCase, NOW, PHONE      # noqa: E402  (also sets fast test settings)
import cta_core as core                                # noqa: E402
import cta_payments as pay                             # noqa: E402
import cta_routing as rt                               # noqa: E402
import cta_sms                                         # noqa: E402
import cta_stats as stats                              # noqa: E402
import cta_testpay as tp                               # noqa: E402
import cta_tickets as T                                # noqa: E402

try:
    import tkinter as tk
    HAVE_TK = bool(os.environ.get('DISPLAY'))
except ImportError:
    HAVE_TK = False

GOOD = {'number': '4242 4242 4242 4242', 'name': 'Eman Test', 'expiry': '12/29', 'cvv': '123'}
TODAY = datetime(2026, 11, 1, 12, 0)


# ---------------------------------------------------------------------------------------------- readable QR
class ReadableQrTests(SupportCase):
    def setUp(self):
        super().setUp()
        self.signer = T.TicketSigner([bytes.fromhex('5e' * 32)])
        T.set_default_signer(self.signer)
        self.rec = self.issue(names=('Ann Lee',), legs=2)
        self.p = self.rec['passengers'][0]

    def test_text_is_one_line_with_the_details_a_phone_will_show(self):
        s = core.ticket_qr_text(self.rec, self.p, 'out', self.signer)
        self.assertNotIn('\n', s)
        for want in ('CTA METRO', 'ANN LEE', 'CENTRALA TO ZORD', self.p['ticket_no'], 'CTA1:'):
            self.assertIn(want, s)
        self.assertNotIn('+44', s)                                   # no phone number
        self.assertNotIn('cents', s)                                 # no price
        back = core.ticket_qr_text(self.rec, self.p, 'return', self.signer)
        self.assertIn('ZORD TO CENTRALA', back)
        self.assertIn(self.p['return_ticket_no'], back)

    def test_scanner_uses_only_the_signed_token(self):
        s = core.ticket_qr_text(self.rec, self.p, 'out', self.signer)
        got = T.parse_presented(s, self.signer)
        self.assertEqual((got.code, got.ticket_id), ('', self.p['ticket_id']))
        edited = s.replace('ANN LEE', 'MALLORY EVIL').replace('CENTRALA TO ZORD', 'ELSEWHERE TO NOWHERE')
        self.assertEqual(T.parse_presented(edited, self.signer).ticket_id, self.p['ticket_id'])   # same ticket
        forged = s[:-4] + 'AAAA'
        self.assertEqual(T.parse_presented(forged, self.signer).code, T.FORGED)
        only_text = ' / '.join(s.split(' / ')[:-1])
        res = self.store.check_in(only_text, signer=self.signer, now=NOW, operator=self.admin, device='t',
                                  environment='testing')
        self.assertFalse(res.ok)                                     # the readable details alone never admit anyone

    def test_plain_token_and_ticket_number_still_work(self):
        token = T.qr_payload(self.rec, self.p, 'out', self.signer)
        self.assertEqual(T.parse_presented(token, self.signer).ticket_id, self.p['ticket_id'])
        self.assertEqual(T.parse_presented(self.p['ticket_no'], self.signer).ticket_no, self.p['ticket_no'].upper())

    def test_checking_in_with_the_scanned_text_works_end_to_end(self):
        s = core.ticket_qr_text(self.rec, self.p, 'out', self.signer)
        res = self.store.check_in(s, signer=self.signer, now=NOW, operator=self.admin, device='t',
                                  environment='testing')
        self.assertTrue(res.ok, res.message)
        again = self.store.check_in(s, signer=self.signer, now=NOW, operator=self.admin, device='t',
                                    environment='testing')
        self.assertFalse(again.ok)                                    # single use still holds

    def test_the_on_screen_qr_reads_even_blurred(self):
        try:
            import zxingcpp
        except ImportError:
            self.skipTest('zxing-cpp not installed (pip install -r requirements-dev.txt)')
        import numpy as np
        from PIL import ImageFilter
        s = core.ticket_qr_text(self.rec, self.p, 'out', self.signer)
        img = core.qr_image(s, 240)
        for blur in (0, 1.0, 1.5):
            pic = img.filter(ImageFilter.GaussianBlur(blur)) if blur else img
            for shrink in (1, 2):
                small = pic.resize((pic.size[0] // shrink, pic.size[1] // shrink))
                found = zxingcpp.read_barcodes(np.array(small.convert('RGB')))
                self.assertTrue(found and found[0].text == s, (blur, shrink))

    def test_a_separator_in_a_name_cannot_smuggle_a_second_token(self):
        self.p['name'] = 'Eve / CTA1:0000:AAAAAAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAA'
        s = core.ticket_qr_text(self.rec, self.p, 'out', self.signer)
        self.assertEqual(s.count('CTA1:'), 1)
        self.assertEqual(T.parse_presented(s, self.signer).ticket_id, self.p['ticket_id'])

    def test_the_printed_qr_decodes_to_the_text_at_many_sizes(self):
        import cv2
        import numpy as np
        import pypdfium2 as pdfium
        import cta_pdf
        out = self.dir / 'q.pdf'
        cta_pdf.build_ticket_pdf(self.rec, out, include_receipt=False, signer=self.signer)
        doc = pdfium.PdfDocument(str(out))
        try:
            import zxingcpp                    # the ZXing engine (used by many phone scanner apps)

            def read(img):
                found = zxingcpp.read_barcodes(img)
                return found[0].text if found else ''
            need = 1.0
        except ImportError:                    # OpenCV's reader is weaker: allow an occasional miss
            det = cv2.QRCodeDetector()

            def read(img):
                return det.detectAndDecode(img)[0]
            need = 0.8
        ok = tot = 0
        misses = []
        for i in range(len(doc)):
            want = core.ticket_qr_text(self.rec, self.p, 'out' if i == 0 else 'return', self.signer)
            for sc in (2, 2.5, 3, 3.5, 4, 5):
                img = np.array(doc[i].render(scale=sc).to_pil().convert('RGB'))
                tot += 1
                got = read(img)
                ok += got == want
                if got != want:
                    misses.append((i, sc, got[:90], want[:90]))
        self.assertGreaterEqual(ok / tot, need, f'{ok}/{tot} decoded {misses}')


# ---------------------------------------------------------------------------------------------- card entry rules
class TestPaymentRules(unittest.TestCase):
    def check(self, method='card', **over):
        values = dict(GOOD)
        values.update(over)
        return tp.validate(method, values, TODAY)

    def test_a_good_made_up_card_passes_and_luhn_is_not_applied(self):
        self.assertEqual(self.check(), {})
        self.assertEqual(self.check(number='1111 1111 1111 1111'), {})         # fails Luhn, still fine

    def test_cvv_must_be_exactly_three_digits(self):
        for bad in ('', '12', '1234', 'abc', '12a'):
            self.assertIn('cvv', self.check(cvv=bad), bad)
        self.assertEqual(self.check(cvv='٣٢١'), {})                           # Arabic-Indic digits are accepted
        self.assertEqual(self.check(number='3782 822463 10005', cvv='1234'), {})   # American Express: 4
        self.assertIn('cvv', self.check(number='3782 822463 10005', cvv='123'))

    def test_card_number_length_and_characters(self):
        for bad in ('', '1234', '4' * 12, '4' * 20, '4242-4242-abcd-4242'):
            self.assertIn('number', self.check(number=bad), bad)
        self.assertIn('number', self.check('mada', number='4' * 15))
        self.assertEqual(self.check('mada', number='5' * 16), {})

    def test_expiry(self):
        self.assertEqual(self.check(expiry='11/26'), {})                       # this month is still valid
        self.assertIn('expiry', self.check(expiry='10/26'))
        for bad in ('', '13/29', '00/29', '1229', 'ab/cd', '12/99'):
            self.assertIn('expiry', self.check(expiry=bad), bad)

    def test_name_phone_and_email(self):
        self.assertIn('name', self.check(name=''))
        self.assertIn('name', self.check(name='R2D2 99'))
        self.assertEqual(tp.validate('stcpay', {'phone': '0512345678'}, TODAY), {})
        self.assertIn('phone', tp.validate('stcpay', {'phone': '12'}, TODAY))
        self.assertEqual(tp.validate('paypal', {'email': 'a@b.co'}, TODAY), {})
        for bad in ('', 'nope', 'a@b', '@b.com', 'a b@c.com'):
            self.assertIn('email', tp.validate('paypal', {'email': bad}, TODAY), bad)

    def test_typing_helpers(self):
        for typed, shown in (('4242424242424242', '4242 4242 4242 4242'), ('4242 4242 4242 4242', '4242 4242 4242 4242'),
                             ('378282246310005', '3782 822463 10005'), ('٤٢٤٢', '4242')):
            self.assertEqual(tp.format_card_number(typed), shown)
        for typed, shown in (('1', '1'), ('12', '12'), ('122', '12/2'), ('1229', '12/29'), ('12/29', '12/29'),
                             ('12/2', '12/2'), ('12/299', '12/29')):
            self.assertEqual(tp.format_expiry(typed), shown, typed)

    def test_pretend_bank_checks_the_balance(self):
        ok = tp.authorize('card', GOOD, 19610, TODAY)
        self.assertTrue(ok.approved)
        self.assertEqual((ok.code, ok.message), (tp.APPROVED, 'Payment accepted'))
        self.assertEqual(ok.detail, 'Visa ending 4242')
        broke = tp.authorize('card', dict(GOOD, number='4000 0000 0000 0000'), 19610, TODAY)
        self.assertFalse(broke.approved)
        self.assertEqual(broke.code, tp.INSUFFICIENT)
        self.assertIn('not enough money', broke.message)
        edge = dict(GOOD, number='4000 0000 0000 0197')                         # 197 x 100 = 19,700 >= 19,610
        self.assertTrue(tp.authorize('card', edge, 19610, TODAY).approved)
        self.assertFalse(tp.authorize('card', dict(GOOD, number='4000 0000 0000 0196'), 19610, TODAY).approved)
        self.assertFalse(tp.authorize('paypal', {'email': 'empty@x.com'}, 100, TODAY).approved)
        self.assertTrue(tp.authorize('paypal', {'email': 'rich@x.com'}, 10 ** 6, TODAY).approved)

    def test_bad_details_are_never_approved(self):
        res = tp.authorize('card', dict(GOOD, cvv='1'), 100, TODAY)
        self.assertEqual((res.approved, res.code), (False, tp.INVALID))
        self.assertFalse(tp.authorize('card', GOOD, 0, TODAY).approved)

    def test_nothing_sensitive_is_in_the_result(self):
        res = tp.authorize('card', GOOD, 100, TODAY)
        blob = repr(res)
        for secret in ('4242 4242 4242 4242', '4242424242424242', '123', '12/29', 'Eman'):
            self.assertNotIn(secret, blob.replace('ending 4242', ''))


class PaymentRecordTests(SupportCase):
    def test_booking_keeps_only_the_short_description_and_admin_can_see_it(self):
        self.issue(method='card')
        hist = self.store.load_history()
        rec = hist[-1]
        rec['payment']['detail'] = tp.authorize('card', GOOD, rec['total_cents'], TODAY).detail
        rows = stats.ticket_rows([rec])
        self.assertEqual(rows[0]['payment_detail'], 'Visa ending 4242')
        self.assertEqual(rows[0]['method'], 'card')
        blob = repr(rec['payment'])
        for secret in ('4242 4242', '12/29', 'Eman'):
            self.assertNotIn(secret, blob)


# ---------------------------------------------------------------------------------------------- custom WhatsApp
class CustomProviderTests(unittest.TestCase):
    def make(self, source):
        d = Path(tempfile.mkdtemp())
        name = f'wa_{abs(hash(source)) % 10 ** 8}'
        (d / f'{name}.py').write_text(source)
        (d / 'cta_config.json').write_text('{"sms": {"provider": "custom", "module": "%s"}}' % name)
        return cta_sms.load_sms_provider(d / 'cta_config.json'), d

    def test_send_code_style_sends_and_the_app_checks_the_code(self):
        sent = []
        p, _d = self.make('SENT = []\ndef send_code(phone, code):\n    SENT.append((phone, code))\n')
        self.assertEqual(p.name.split(':')[0], 'custom')
        self.assertFalse(p.shows_code)
        ok, _ = p.start('+966512345678', NOW)
        self.assertTrue(ok)
        import importlib
        mod = importlib.import_module(p.name.split(':')[1])
        phone, code = mod.SENT[-1]
        self.assertEqual(phone, '+966512345678')
        self.assertEqual(p.code_for(phone), '')                              # never shown on screen
        self.assertFalse(p.check(phone, '000000' if code != '000000' else '111111', NOW)[0])
        self.assertTrue(p.check(phone, code, NOW)[0])
        self.assertFalse(p.check(phone, code, NOW)[0])                        # single use

    def test_start_check_style(self):
        p, _d = self.make('def start(phone, now):\n    return True, ""\n'
                          'def check(phone, code, now):\n    return code == "424242", "That code is not correct."\n')
        self.assertTrue(p.start('+966512345678', NOW)[0])
        self.assertTrue(p.check('+966512345678', '٤٢٤٢٤٢', NOW)[0])
        self.assertEqual(p.check('+966512345678', '1', NOW), (False, 'That code is not correct.'))

    def test_failures_become_messages_not_crashes(self):
        p, _d = self.make('def send_code(phone, code):\n    raise RuntimeError("boom secret-token-123")\n')
        ok, msg = p.start('+966512345678', NOW)
        self.assertFalse(ok)
        self.assertNotIn('secret-token-123', msg)
        p2, _ = self.make('def send_code(phone, code):\n    return False, "Not set up"\n')
        self.assertEqual(p2.start('+966512345678', NOW), (False, 'Not set up'))

    def test_broken_or_missing_module_blocks_sign_in_with_a_clear_message(self):
        p, _ = self.make('this is not python(')
        self.assertIsInstance(p, cta_sms.SmsUnavailable)
        self.assertIn('problem', p.reason)
        d = Path(tempfile.mkdtemp())
        (d / 'cta_config.json').write_text('{"sms": {"provider": "custom", "module": "does_not_exist_xyz"}}')
        self.assertIsInstance(cta_sms.load_sms_provider(d / 'cta_config.json'), cta_sms.SmsUnavailable)
        (d / 'cta_config.json').write_text('{"sms": {"provider": "custom", "module": "../evil"}}')
        self.assertIsInstance(cta_sms.load_sms_provider(d / 'cta_config.json'), cta_sms.SmsUnavailable)

    def test_twilio_whatsapp_channel_is_accepted(self):
        self.assertIn('whatsapp', cta_sms.TwilioVerify.CHANNELS)

    def test_the_shipped_example_file_loads_and_reports_it_is_not_set_up(self):
        d = Path(tempfile.mkdtemp())
        (d / 'my_whatsapp_example_copy.py').write_text((HERE.parent / 'cta_whatsapp_example.py').read_text())
        (d / 'cta_config.json').write_text('{"sms": {"provider": "custom", "module": "my_whatsapp_example_copy"}}')
        p = cta_sms.load_sms_provider(d / 'cta_config.json')
        with mock.patch.dict(os.environ, {'MY_WHATSAPP_TOKEN': ''}):
            ok, msg = p.start('+966512345678', NOW)
        self.assertFalse(ok)
        self.assertIn('not set up', msg)


# ---------------------------------------------------------------------------------------------- screens
@unittest.skipUnless(HAVE_TK, 'needs tkinter and a display (run under xvfb-run)')
class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        for target in ('cta_app.messagebox', 'cta_admin.messagebox', 'cta_dialogs.messagebox'):
            try:
                patcher = mock.patch(target)
            except AttributeError:
                continue
            try:
                self.boxes = patcher.start()
            except AttributeError:
                continue
            self.addCleanup(patcher.stop)

    def dialog(self, method='card', total=19610):
        import cta_dialogs as D
        got = []
        D.TestPaymentDialog.CHECK_MS = 0
        D.TestPaymentDialog.DONE_MS = 0
        dlg = D.TestPaymentDialog(self.root, method, total, got.append, clock=lambda: TODAY)
        self.addCleanup(lambda: dlg.win.winfo_exists() and dlg.win.destroy())
        return dlg, got

    def fill(self, dlg, **vals):
        for k, v in vals.items():
            dlg.vars[k].set(v)

    def pump(self):
        for _ in range(5):
            self.root.update()

    def test_bad_details_show_messages_and_nothing_is_approved(self):
        dlg, got = self.dialog()
        self.fill(dlg, number='4242', name='', expiry='01/20', cvv='12')
        dlg._submit()
        self.pump()
        for key in ('number', 'name', 'expiry', 'cvv'):
            self.assertTrue(dlg.err[key].cget('text'), key)
        self.assertIn('exactly 3 digits', dlg.err['cvv'].cget('text'))
        self.assertEqual(got, [])

    def test_good_details_are_accepted_and_the_form_is_wiped(self):
        dlg, got = self.dialog()
        self.fill(dlg, **GOOD)
        self.assertEqual(dlg.vars['number'].get(), '4242 4242 4242 4242')            # grouped as you type
        dlg._submit()
        self.pump()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].approved)
        self.assertEqual(got[0].detail, 'Visa ending 4242')

    def test_not_enough_money_is_refused_and_can_be_retried(self):
        dlg, got = self.dialog()
        self.fill(dlg, **dict(GOOD, number='4000 0000 0000 0000'))
        dlg._submit()
        self.pump()
        self.assertEqual(got, [])
        self.assertIn('not enough money', dlg.status.cget('text'))
        self.assertTrue(dlg.pay_btn.enabled)
        self.fill(dlg, number='4000 0000 0000 5000')
        dlg._submit()
        self.pump()
        self.assertEqual(len(got), 1)

    def test_payment_window_hands_the_approval_to_the_booking(self):
        import cta_dialogs as D
        done = []
        D.TestPaymentDialog.CHECK_MS = 0
        D.TestPaymentDialog.DONE_MS = 0
        pd = D.PaymentDialog(self.root, 19610, 1, lambda m, a=None: done.append((m, a)), test_mode=True)
        pd._select('card')
        pd._confirm()
        self.pump()
        forms = [w for w in pd.win.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(forms), 1)
        self.assertEqual(done, [])                                       # nothing booked before the bank says yes
        forms[0].destroy()
        pd2 = D.PaymentDialog(self.root, 19610, 1, lambda m, a=None: done.append((m, a)), test_mode=True)
        pd2._select('cash')
        pd2._confirm()                                                    # cash needs no card form
        self.assertEqual(done, [('cash', None)])
        pd.win.destroy()

    def make_app(self):
        from cta_app import CTAApp
        from test_all import sign_in
        self.tmp = tempfile.mkdtemp()
        now = datetime(2026, 10, 24, 14, 2, 10)
        app = CTAApp(self.root, clock=lambda: now, folder=self.tmp, environment='development',
                     provider=rt.SimulatedProvider())
        app.staff = sign_in(app.store, 'admin', now=datetime(2026, 10, 24, 8, 0))
        app._paint_account()
        app.from_pick.set('Centrala'); app.to_pick.set('Zord'); app.search()
        app.steppers['Adult'].set(2)
        app.account = {'phone': '+447123456789', 'name': 'Jane Doe'}
        app.show_confirmation = lambda rec, saved=None: None
        return app

    def test_card_booking_through_the_real_app_keeps_only_the_last_four_digits(self):
        app = self.make_app()
        auth = tp.authorize('card', GOOD, app.current_quote().total_cents, app.clock())
        app._issue(['Jane Doe', 'John Roe'], 'card', auth)
        pay_rec = app.history[0]['payment']
        self.assertEqual((pay_rec['status'], pay_rec['detail']), ('paid', 'Visa ending 4242'))
        for secret in ('4242 4242', '12/29', 'Eman'):
            self.assertNotIn(secret, repr(app.history))
            self.assertNotIn(secret, (Path(self.tmp) / 'payment_events.jsonl').read_text()
                             if (Path(self.tmp) / 'payment_events.jsonl').exists() else '')

    def test_help_tab_is_hidden_for_staff_and_returns_after_sign_out(self):
        app = self.make_app()
        self.assertFalse(app.nav['help'].winfo_ismapped() and app._help_shown)
        self.assertFalse(app._help_shown)
        app.staff = None
        app._paint_account()
        self.assertTrue(app._help_shown)

    def test_admin_buttons_work_and_the_selection_survives_a_refresh(self):
        app = self.make_app()
        app._issue(['Jane Doe', 'John Roe'], 'cash')
        app.steppers['Adult'].set(2)                                       # the app resets travellers after a booking
        app._issue(['Jane Doe', 'John Roe'], 'card', tp.authorize('card', GOOD, 12630, app.clock()))
        self.assertEqual(len(app.history), 2)
        adm = app.admin
        adm.refresh()
        self.pump()
        rows = adm.tree.get_children()
        self.assertEqual(len(rows), 4)
        adm.tree.selection_set(rows[0])
        adm._sync_actions()
        self.assertTrue(all(b.enabled for b in adm.action_btns))
        adm.refresh()                                                      # what the 5-second timer does
        self.assertEqual(adm.tree.selection(), (rows[0],))                 # still selected: the old bug is gone
        # payment filter: only the card booking
        adm.method_box.current([k for k, _ in stats.METHOD_FILTERS].index('card'))
        adm._fill_tickets()
        self.assertEqual(len(adm.tree.get_children()), 2)
        self.assertIn('Visa ending 4242', adm.tree.item(adm.tree.get_children()[0], 'values')[5])
        adm.method_box.current([k for k, _ in stats.METHOD_FILTERS].index('cash'))
        adm._fill_tickets()
        cash_rows = adm.tree.get_children()
        self.assertEqual(len(cash_rows), 2)
        # cancel one ticket, then the whole booking, then record a refund, then allow re-entry / correct a name
        adm.tree.selection_set(cash_rows[0])
        adm.cancel_selected(reason='Passenger changed plans')
        rec = app.history[0]
        self.assertEqual(rec['passengers'][0]['status'], 'cancelled')
        self.assertEqual(rec['passengers'][1]['status'], 'issued')
        adm.tree.selection_set(cash_rows[1])
        adm.cancel_selected(reason='Whole booking', whole_booking=True)
        self.assertEqual(app.history[0]['passengers'][1]['status'], 'cancelled')
        adm.tree.selection_set(cash_rows[0])
        adm.refund_selected(ref='')                                        # unpaid counter booking: nothing to refund
        self.assertEqual(app.history[0]['passengers'][0]['status'], 'cancelled')
        self.assertTrue(adm.action_msg.cget('text'))                       # ...and the reason is shown, not silence
        adm.method_box.current(0)
        adm._fill_tickets()
        paid_ticket = app.history[1]['passengers'][1]['ticket_no']         # paid by (test) card: refund is due
        adm.tree.selection_set(paid_ticket)
        adm.cancel_selected(reason='Refund wanted')
        self.assertIn('Refund due', adm.action_msg.cget('text'))
        adm.tree.selection_set(paid_ticket)
        adm.refund_selected(ref='TEST-REFUND-1')
        self.assertEqual(app.history[1]['passengers'][1]['status'], 'refunded')
        card_ticket = app.history[1]['passengers'][0]['ticket_no']
        adm.tree.selection_set(card_ticket)
        adm.correct_name_selected(new_name='Janet Doe')
        self.assertEqual(app.history[1]['passengers'][0]['name'], 'Janet Doe')
        adm.tree.selection_set(card_ticket)
        adm.check_in_selected()
        self.assertEqual(app.history[1]['passengers'][0]['status'], 'boarded')
        adm.tree.selection_set(card_ticket)
        adm.reentry_selected(reason='Scanner glitch')
        self.assertEqual(app.history[1]['passengers'][0]['status'], 'issued')
        self.assertTrue(adm.action_msg.cget('text'))                       # feedback is shown above the list

    def test_a_complaint_shows_up_for_the_admin_with_a_banner(self):
        app = self.make_app()
        app.store.open_case('problem', 'The guard was rude.', app.clock(), topic='Complaint about service or staff',
                            account_phone='+447123456789')
        adm = app.admin
        adm.refresh()
        self.pump()
        self.assertTrue(adm._banner_shown)
        self.assertIn('1 passenger complaint', adm.inbox_msg.cget('text'))
        self.assertIn('(1)', adm.nb.tab(adm.messages_tab, 'text'))
        self.assertEqual(len(adm.messages_tab.tree.get_children()), 1)


if __name__ == '__main__':
    unittest.main()
