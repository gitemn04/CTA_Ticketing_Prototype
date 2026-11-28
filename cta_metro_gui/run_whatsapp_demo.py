"""Isolated classroom demonstration. Reusable template code, NOT secure OTP authentication."""
from __future__ import annotations
import getpass
import hmac
import os
from pathlib import Path


class WhatsAppTemplateDemo:
    name = 'whatsapp_template_demo'
    shows_code = False
    template_sid = ''

    def __init__(self, client, expected_code, sender, content_sid, recipient):
        if len(expected_code) != 6 or not expected_code.isascii() or not expected_code.isdigit():
            raise ValueError('Enter the six digits from the WhatsApp test message.')
        self.client = client
        self._expected_code = expected_code
        self.sender = sender
        self.content_sid = content_sid
        self.recipient = recipient
        self._requested = set()

    def start(self, phone, now, **kwargs):
        # Trial recipients must be verified AND linked to the WhatsApp testing environment.
        if phone != self.recipient:
            return False, 'This demo is configured for your linked WhatsApp tester only.'
        self._requested.discard(phone)
        try:
            message = self.client.messages.create(
                to='whatsapp:' + phone, from_='whatsapp:' + self.sender,
                content_sid=self.content_sid)
            if str(getattr(message, 'status', '')).lower() in ('failed', 'undelivered', 'canceled'):
                return False, 'WhatsApp delivery failed. Check the Twilio message log.'
            if not getattr(message, 'sid', ''):
                return False, 'Twilio did not return a message ID. Try again.'
        except Exception as exc:
            # Do not show provider exception bodies, credentials, or template codes.
            error = getattr(exc, 'code', None)
            suffix = f' (Twilio error {error})' if isinstance(error, int) else ''
            return False, 'Could not submit the WhatsApp message' + suffix + '. Check Twilio Logs and your tester connection.'
        self._requested.add(phone)
        return True, ''

    def check(self, phone, code, now):
        if phone not in self._requested:
            return False, 'Send the WhatsApp demo message first.'
        typed = ''.join((code or '').split())
        if not hmac.compare_digest(typed.encode('utf-8'), self._expected_code.encode('ascii')):
            return False, 'Enter the six-digit code from the WhatsApp test message.'
        # Intentionally reusable, without expiry/consumption, for this classroom demo only.
        return True, ''


def main():
    print('CTA WHATSAPP DEMO: reusable template code; not secure OTP verification.')
    print('Demo accounts and tickets are stored separately from your normal CTA data.')
    try:
        from twilio.rest import Client
        import tkinter as tk
        import cta_app
        from cta_dialogs import AuthDialog
    except ImportError as exc:
        print('Put this file beside cta_app.py and install the project dependencies plus twilio.')
        print('Missing module:', exc.name)
        return
    sid = os.environ.get('TWILIO_ACCOUNT_SID', '').strip() or input('Twilio Account SID (AC...): ').strip()
    token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip() or getpass.getpass('Twilio Auth Token (hidden): ').strip()
    if not sid.startswith('AC') or len(sid) != 34 or not token:
        print('Account SID or Auth Token is missing/invalid. Nothing was sent.')
        return
    recipient = input('Your linked tester phone (+9665..., no 0 after +966): ').strip()
    if not recipient.startswith('+') or not recipient[1:].isascii() or not recipient[1:].isdigit():
        print('Enter your tester phone in international format.')
        return
    code = getpass.getpass('Six-digit code from the existing WhatsApp TEST message (hidden): ').strip()
    try:
        from twilio.http.http_client import TwilioHttpClient
        client = Client(sid, token, http_client=TwilioHttpClient(timeout=15))
        provider = WhatsAppTemplateDemo(client, code,
            '+17372508034', 'HX25161c213d71bb75e073ead06f38fbbd', recipient)
    except ValueError as exc:
        print(str(exc))
        return

    class DemoAuthDialog(AuthDialog):
        def _title(self, body, title, sub=''):
            self.win.title('CTA WhatsApp demo — reusable code')
            sub = sub.replace('We sent a 6-digit code by text message to ',
                'WhatsApp demo message requested for ')
            sub += '\nDemo: reusable test code. Check WhatsApp; delivery may take a moment.'
            super()._title(body, title, sub)

    # Inject the demo provider/dialog only into this explicitly launched process.
    cta_app.AuthDialog = DemoAuthDialog
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    folder = Path.home() / 'Documents' / 'CTA_WhatsApp_Demo'
    root = tk.Tk()
    cta_app.CTAApp(root, folder=folder, sms=provider)
    root.title('CTA Metro — WhatsApp DEMO (reusable test code)')
    root.mainloop()


if __name__ == '__main__':
    main()
