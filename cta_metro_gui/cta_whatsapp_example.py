"""Example of YOUR OWN WhatsApp verification sender. Copy this file, rename it (for example my_whatsapp.py), put your
sending code in ``send_code`` and point the app at it in cta_config.json:

    "sms": { "provider": "custom", "module": "my_whatsapp" }

The app makes the 6-digit code itself (valid 5 minutes, 3 wrong tries, single use) and checks what the passenger
types. This file only has to DELIVER the text. The code is never shown on screen and never written to the logs.

``phone`` arrives in international form, for example +9665XXXXXXXX.
Return None / True on success, False on failure, or (ok, "message to show the passenger").
Keep keys and tokens out of this file: read them from environment variables.
"""
import os


def send_code(phone: str, code: str):
    message = f"Your CTA Metro verification code is {code}. It expires in 5 minutes. Do not share it."
    # ---- replace this block with your WhatsApp sending code, for example a call to your provider's API ----
    token = os.environ.get('MY_WHATSAPP_TOKEN', '')
    if not token:
        return False, 'WhatsApp is not set up yet (MY_WHATSAPP_TOKEN is missing).'
    raise NotImplementedError('Add your WhatsApp API call here, sending `message` to `phone`.')


# Alternative: if your service makes and checks the codes itself, delete send_code and define instead:
#
# def start(phone, now):            # send a code; return (True, "") or (False, "reason")
#     ...
# def check(phone, code, now):      # return (True, "") or (False, "That code is not correct.")
#     ...
