"""One-time helper: connect the app to Twilio Verify and prove it works by texting YOUR phone.

    python setup_sms.py

You need (from console.twilio.com): Account SID, Auth Token, and a Verify Service SID (Verify -> Services).
The details are saved in cta_config.json next to the app (plain text on this computer: do not share that file).
"""
from datetime import datetime
from getpass import getpass
from pathlib import Path

import cta_accounts as acct
from cta_sms import TwilioVerify, write_sms_config

CONFIG = Path(__file__).with_name('cta_config.json')


def main() -> None:
    print('CTA Metro - connect text-message verification (Twilio Verify)\n')
    sid = input('Account SID (starts with AC): ').strip()
    token = getpass('Auth Token (typing is hidden): ').strip()
    service = input('Verify Service SID (starts with VA): ').strip()
    channel = (input('How should the code arrive? sms / whatsapp / call [sms]: ').strip().lower() or 'sms')
    if channel not in TwilioVerify.CHANNELS:
        print('Unknown choice, using sms.')
        channel = 'sms'
    provider = TwilioVerify(sid, token, service, channel=channel)

    while True:
        raw = input('\nYour phone number with country code, e.g. +9665XXXXXXXX (Enter to skip the test): ').strip()
        if not raw:
            break
        phone, err = acct.validate_phone(raw)
        if err:
            print(err)
            continue
        ok, msg = provider.start(phone, datetime.now())
        if not ok:
            print('Could not send the code:', msg)
            if input('Try another number? (y/n): ').lower() != 'y':
                break
            continue
        print(f'Code sent by {channel}. Check your phone.')
        code = input('Type the code you received: ').strip()
        ok, msg = provider.check(phone, code, datetime.now())
        print('Verified - it works!' if ok else f'Not verified: {msg}')
        break

    print()
    print('Personalised message template (optional):')
    print('  Create a Custom Verification Template in Twilio Console:')
    print('  Verify > (your service) > General > Custom Code Templates')
    print('  Body: "Hi {name}, your CTA verification code is {code}.')
    print('          It expires in 15 minutes. Do not share this code."')
    print('  Twilio must approve the template (1-5 business days).')
    template_sid = input('Approved Template SID (HJxxx… — leave blank to use default message): ').strip()

    if input('\nSave these settings to cta_config.json? (y/n): ').strip().lower() == 'y':
        write_sms_config(CONFIG, sid, token, service, channel, template_sid=template_sid)
        if template_sid:
            print(f'Personalised template SID saved: {template_sid}')
            print('Passengers will be greeted by name when they request a code.')
        else:
            print('No template SID saved — Twilio default message will be used.')
        print(f'Settings saved to {CONFIG}. Start the app again: python cta_app.py')


if __name__ == '__main__':
    main()
