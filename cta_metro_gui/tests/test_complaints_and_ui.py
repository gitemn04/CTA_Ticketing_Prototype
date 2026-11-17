"""Complaints reach the admin inbox; payment tiles hover/explain; QR status is stated; boarding-pass print path."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from test_support import SupportCase, NOW, PHONE, OTHER   # noqa: E402
import cta_db  # noqa: E402
import cta_core as core  # noqa: E402
from cta_auth import PermissionDenied  # noqa: E402

try:
    import tkinter as tk
    HAVE_TK = bool(os.environ.get('DISPLAY'))
except ImportError:
    HAVE_TK = False


class ComplaintTests(SupportCase):
    def test_complaint_topics_exist_and_form_uses_same_list(self):
        import cta_app
        self.assertIs(cta_app.ISSUE_TYPES, cta_db.SUPPORT_TOPICS)
        self.assertTrue([t for t in cta_db.SUPPORT_TOPICS if t.startswith('Complaint')])

    def test_each_complaint_topic_reaches_admin_and_can_be_answered(self):
        for t in [t for t in cta_db.SUPPORT_TOPICS if t.startswith('Complaint')]:
            cid = self.store.open_case('problem', f'Complaint text for {t}', NOW, topic=t, account_phone=PHONE)
            row = [c for c in self.store.list_cases(self.admin, NOW) if c['id'] == cid][0]
            self.assertEqual(row['topic'], t)
            self.assertEqual(row['status'], 'open')
            ok, _ = self.store.reply_case(cid, self.admin, 'Thank you, we are looking into it.', NOW)
            self.assertTrue(ok)

    def test_unknown_topic_rejected(self):
        with self.assertRaises(ValueError):
            self.store.open_case('problem', 'x', NOW, topic='Made up topic')

    def test_non_staff_cannot_read_complaints(self):
        self.store.open_case('problem', 'Rude staff', NOW, topic='Complaint about service or staff', account_phone=PHONE)
        with self.assertRaises(PermissionDenied):
            self.store.list_cases(None, NOW)


class BoardingPassPrintTests(SupportCase):
    def test_pdf_is_boarding_pass_size_and_print_has_no_a4_receipt(self):
        import pypdf
        rec = self.issue(names=('Ann Lee', 'Bo Chan'))
        out = self.dir / 'bp.pdf'
        core.export_ticket_pdf(rec, out, include_receipt=False, signer=None)
        r = pypdf.PdfReader(str(out))
        self.assertEqual(len(r.pages), 2)
        for p in r.pages:
            self.assertAlmostEqual(float(p.mediabox.width), 540, delta=1)
            self.assertAlmostEqual(float(p.mediabox.height), 252, delta=1)
        full = self.dir / 'full.pdf'
        core.export_ticket_pdf(rec, full, include_receipt=True, signer=None)
        self.assertEqual(len(pypdf.PdfReader(str(full)).pages), 3)

    def test_url_map_goes_into_the_qr_and_unmapped_tickets_keep_the_scanner_code(self):
        import cv2, numpy as np, pypdfium2 as pdfium
        import cta_tickets as T
        T.set_default_signer(T.TicketSigner([bytes.fromhex('5e' * 32)]))
        rec = self.issue(names=('Ann Lee', 'Bo Chan'))
        first, second = rec['passengers'][0]['ticket_id'], rec['passengers'][1]['ticket_id']
        url = 'https://tickets.example.test/ticket/ABC123'
        out = self.dir / 'm.pdf'
        core.export_ticket_pdf(rec, out, include_receipt=False, ticket_url_map={first: url})
        doc = pdfium.PdfDocument(str(out))
        got = []
        for i in range(len(doc)):
            img = np.array(doc[i].render(scale=4).to_pil().convert('RGB'))
            got.append(cv2.QRCodeDetector().detectAndDecode(img)[0])
        self.assertEqual(got[0], url)
        self.assertIn('/ CTA1:', got[1])

    def test_print_pdf_reports_failure_instead_of_raising(self):
        rec = self.issue()
        out = self.dir / 'p.pdf'
        core.export_ticket_pdf(rec, out, include_receipt=False, signer=None)
        with mock.patch('cta_core.os.startfile', create=True, side_effect=OSError('no printer')), \
                mock.patch('cta_core.shutil.which', return_value=None), \
                mock.patch('cta_core.open_pdf', return_value=(False, 'No PDF viewer found.')):
            ok, msg = core.print_pdf(out)                # every route fails: must answer, never raise
        self.assertFalse(ok)
        self.assertTrue(msg)
        with mock.patch('cta_core.shutil.which', return_value='/usr/bin/lp'), \
                mock.patch('cta_core.subprocess.run', return_value=mock.Mock(returncode=0)):
            ok, msg = core.print_pdf(out)
        self.assertTrue(ok)


@unittest.skipUnless(HAVE_TK, 'needs tkinter and a display (run under xvfb-run)')
class UiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry('600x400+0+0')
        self.addCleanup(self.root.destroy)

    def test_tooltip_shows_and_hides(self):
        from cta_widgets import Tooltip
        lbl = tk.Label(self.root, text='hover me')
        lbl.pack(padx=40, pady=40)
        self.root.update()
        t = Tooltip(lbl, 'hello', delay=1)
        lbl.event_generate('<Enter>')
        self.root.after(50, self.root.quit)
        self.root.mainloop()
        self.assertIsNotNone(t.tip)
        lbl.event_generate('<Leave>')
        self.root.update()
        self.assertIsNone(t.tip)

    def test_tooltip_survives_bad_text_fn(self):
        from cta_widgets import Tooltip
        lbl = tk.Label(self.root, text='x')
        lbl.pack()
        t = Tooltip(lbl, lambda: '', delay=1)
        t._show()
        self.assertIsNone(t.tip)


if __name__ == '__main__':
    unittest.main()
