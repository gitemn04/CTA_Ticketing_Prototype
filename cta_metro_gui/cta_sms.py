"""Phone verification providers (v3.0).

* ``TwilioVerify``   - real text messages through the Twilio Verify API (needs your own Twilio account).
* ``OnScreenCodes``  - DEVELOPMENT / TESTING ONLY. The code is shown on screen. Never use in production.
  Production mode refuses sign-in when no real SMS provider is configured (see ``load_sms_provider``).
* ``CustomProvider`` - YOUR OWN WhatsApp (or any other) sender, loaded from a small Python file next to the app:
      "sms": {"provider": "custom", "module": "my_whatsapp"}      ->  my_whatsapp.py in the app folder
  The file may offer EITHER ``send_code(phone, code)`` (the app makes and checks the 6-digit code: 5 minutes,
  3 tries, single use - you only deliver it) OR ``start(phone, now)`` and ``check(phone, code, now)`` (your service
  makes and checks the code itself). See ``cta_whatsapp_example.py``. Twilio Verify can also send WhatsApp:
  set "channel": "whatsapp" in the same section.
* ``SmsUnavailable`` - used when the settings file is damaged.

Configuration (either way works; environment variables win):
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VERIFY_SERVICE_SID
    or cta_config.json:  {"sms": {"provider": "twilio_verify", "account_sid": "...", "auth_token": "...",
                                  "service_sid": "VA...", "channel": "sms"}}      (channel: sms | whatsapp | call)
    Easiest: run  python setup_sms.py  which asks for the three values, sends you a test code and saves the file.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from cta_accounts import OtpSession, normalize_digits

VERIFY_BASE = 'https://verify.twilio.com/v2/Services'


class OnScreenCodes:
    """Fallback: generates the code locally and shows it on screen (nothing is texted)."""
    name = 'on-screen'
    shows_code = True

    def __init__(self):
        self.sessions: Dict[str, OtpSession] = {}

    def start(self, phone: str, now: datetime) -> Tuple[bool, str]:
        self.sessions[phone] = OtpSession(phone, now)
        return True, ''

    def code_for(self, phone: str) -> str:
        s = self.sessions.get(phone)
        return s.demo_code if s else ''

    def check(self, phone: str, code: str, now: datetime) -> Tuple[bool, str]:
        s = self.sessions.get(phone)
        if s is None:
            return False, 'Ask for a code first.'
        return s.verify(code, now)


class TwilioVerify:
    """Twilio Verify v2: POST /Verifications sends the SMS, POST /VerificationCheck checks the code.
    Twilio generates the code, enforces expiry and attempt limits, and never shows it to this app."""
    name = 'twilio_verify'
    shows_code = False

    CHANNELS = ('sms', 'whatsapp', 'call')

    def __init__(self, account_sid: str, auth_token: str, service_sid: str,
                 opener: Optional[Callable] = None, timeout: float = 15.0, channel: str = 'sms'):
        self.account_sid, self.auth_token, self.service_sid = account_sid, auth_token, service_sid
        self.channel = channel if channel in self.CHANNELS else 'sms'
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def _post(self, endpoint: str, fields: Dict[str, str]) -> Tuple[int, dict]:
        url = f'{VERIFY_BASE}/{self.service_sid}/{endpoint}'
        token = base64.b64encode(f'{self.account_sid}:{self.auth_token}'.encode()).decode()
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method='POST',
                                     headers={'Authorization': f'Basic {token}',
                                              'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode('utf-8') or '{}')
            except ValueError:
                body = {}
            return exc.code, body

    def start(self, phone: str, now: datetime, *, locale: str = '',
              template_sid: str = '', template_vars: Optional[dict] = None) -> Tuple[bool, str]:
        """Send a verification code to *phone*.

        Personalised message support (Bug 5):
        ------------------------------------
        To send  "Hi {name}, your CTA verification code is {code}. It expires in 15 minutes.
        Do not share this code."  you must:

        1. Create a Custom Verification Template in the Twilio Console:
               Verify → (your service) → General → Custom Code Templates
           Enter the body above exactly, using Twilio's substitution placeholders
           (e.g. {name} as a custom variable).  Twilio must approve the template
           before it can be used (approval typically takes 1-5 business days).

        2. Pass the approved template's SID as ``template_sid`` and supply
           ``template_vars={'name': passenger_name}`` when calling this method.
           Example:
               provider.start(phone, now,
                               template_sid='HJxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                               template_vars={'name': 'Ahmed Al-Rashidi'})

        3. Saudi phone numbers must be normalised before calling this method:
               '0512345678'  →  '+966512345678'
           The caller is responsible for normalisation (e.g. via cta_accounts).

        Until an approved template SID is supplied, Twilio sends its standard
        default message.  The code is never shown on screen regardless.

        NOTE: Do NOT send real SMS or incur charges without explicit authorisation.
              Do NOT pass a template SID that has not been approved by Twilio.
        """
        fields: Dict[str, str] = {'To': phone, 'Channel': self.channel}
        if template_sid:
            fields['TemplateSid'] = template_sid
            if template_vars:
                # Twilio accepts template substitutions as a JSON-encoded string
                # under the key 'TemplateCustomSubstitutions'
                fields['TemplateCustomSubstitutions'] = json.dumps(template_vars)
        elif locale:
            # Locale-based localisation (no custom template required)
            fields['Locale'] = locale
        try:
            status, body = self._post('Verifications', fields)
        except (OSError, ValueError) as exc:
            return False, f'Could not reach the SMS service ({exc}). Check your internet connection.'
        if status in (200, 201):
            return True, ''
        if status == 429:
            return False, 'Too many codes requested. Please wait a few minutes and try again.'
        return False, body.get('message') or f'The SMS service refused the request (HTTP {status}).'

    def check(self, phone: str, code: str, now: datetime) -> Tuple[bool, str]:
        typed = ''.join(normalize_digits(code or '').split())     # accept Arabic-Indic / full-width digits
        if not typed:
            return False, 'Enter the code from the text message.'
        try:
            status, body = self._post('VerificationCheck', {'To': phone, 'Code': typed})
        except (OSError, ValueError) as exc:
            return False, f'Could not reach the SMS service ({exc}).'
        if status in (200, 201) and body.get('status') == 'approved':
            return True, ''
        if status == 404:
            return False, 'This code has expired or was already used. Ask for a new one.'
        if status == 429:
            return False, 'Too many wrong tries. Ask for a new code.'
        return False, 'That code is not correct.'


class SmsUnavailable:
    """Used when the settings file is damaged: sign-in codes are neither texted nor shown on screen."""
    name = 'unavailable'
    shows_code = False

    def __init__(self, reason: str):
        self.reason = reason

    def start(self, phone: str, now: datetime) -> Tuple[bool, str]:
        return False, self.reason

    def code_for(self, phone: str) -> str:
        return ''

    def check(self, phone: str, code: str, now: datetime) -> Tuple[bool, str]:
        return False, self.reason


class CustomProvider:
    """Adapter around the user's own sender module (see the top of this file). Never raises to the caller and never
    shows the code on screen."""
    name = 'custom'
    shows_code = False
    template_sid = ''

    def __init__(self, module, label: str = 'custom'):
        self._m = module
        self.name = f'custom:{label}'
        self._sessions: Dict[str, OtpSession] = {}
        self._own_codes = callable(getattr(module, 'start', None)) and callable(getattr(module, 'check', None))
        if not self._own_codes and not callable(getattr(module, 'send_code', None)):
            raise ValueError(f'{label}.py must define send_code(phone, code), or start(phone, now) and '
                             'check(phone, code, now).')

    @staticmethod
    def _result(res, default_ok_message: str = '') -> Tuple[bool, str]:
        if isinstance(res, tuple) and len(res) == 2:
            return bool(res[0]), str(res[1] or '')
        if res is None or res is True:
            return True, default_ok_message
        if res is False:
            return False, 'The verification message could not be sent.'
        return True, default_ok_message

    def code_for(self, phone: str) -> str:
        return ''

    def start(self, phone: str, now: datetime, **_ignored) -> Tuple[bool, str]:
        try:
            if self._own_codes:
                return self._result(self._m.start(phone, now))
            session = OtpSession(phone, now)
            ok, msg = self._result(self._m.send_code(phone, session.demo_code))
            if ok:
                self._sessions[phone] = session
            return ok, msg
        except Exception as exc:  # noqa: BLE001 - the sender is the user's code; its failure must not crash sign-in
            import cta_errors
            incident = cta_errors.report(exc, 'sending a verification code with the custom provider')
            return False, f'The verification message could not be sent. (Reference {incident}.)'

    def check(self, phone: str, code: str, now: datetime) -> Tuple[bool, str]:
        try:
            if self._own_codes:
                return self._result(self._m.check(phone, normalize_digits(code or '').strip(), now))
            session = self._sessions.get(phone)
            if session is None:
                return False, 'Ask for a code first.'
            return session.verify(code, now)
        except Exception as exc:  # noqa: BLE001
            import cta_errors
            incident = cta_errors.report(exc, 'checking a verification code with the custom provider')
            return False, f'The code could not be checked. (Reference {incident}.)'


def load_custom_provider(cfg: dict, folder: Path):
    """Import the module named in ``sms.module`` from the app folder. Returns a CustomProvider or SmsUnavailable."""
    import importlib
    import re
    import sys
    name = str(cfg.get('module') or '').strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,40}', name):
        return SmsUnavailable('The custom verification module name in cta_config.json is not valid. Use the file name '
                              'without .py, for example "my_whatsapp".')
    added = str(folder) not in sys.path
    if added:
        sys.path.insert(0, str(folder))
    try:
        module = importlib.import_module(name)
        return CustomProvider(module, name)
    except ImportError as exc:
        return SmsUnavailable(f'The custom verification file {name}.py could not be loaded ({exc}). '
                              'Put it in the same folder as cta_app.py.')
    except Exception as exc:  # noqa: BLE001 - a typo in the user's file must show a message, not crash the app
        return SmsUnavailable(f'The custom verification file {name}.py has a problem: {exc}')
    finally:
        if added and str(folder) in sys.path:
            sys.path.remove(str(folder))


CONFIG_PATH = Path(__file__).with_name('cta_config.json')


def load_sms_provider(config_path: Optional[Path] = None, environment: str = ''):
    """Return the configured SMS provider.

    The returned provider is one of TwilioVerify, OnScreenCodes, or SmsUnavailable.
    The Twilio Custom Verification Template SID (``sms.template_sid``) is attached
    as ``provider.template_sid`` so callers can pass it to provider.start() along
    with ``template_vars={'name': passenger_name}`` to send the personalised
    "Hi {name}, your CTA code is {code}…" message without a second config read.

    Production environment:
      * Full Twilio credentials present → TwilioVerify (real SMS).
      * Credentials missing              → SmsUnavailable (blocks sign-in with a clear message).
      * On-screen provider is NEVER used in production. There is no silent fallback.

    Development / testing environment (environment='development' or CTA_ENV env var):
      * Full Twilio credentials present  → TwilioVerify.
      * Credentials missing              → OnScreenCodes (code shown on screen, labelled as dev-only).

    A damaged cta_config.json raises cta_storage.StorageError (never silently falls back).
    """
    from cta_storage import load_config
    path = config_path or CONFIG_PATH
    full_cfg = load_config(path)
    cfg = full_cfg.get('sms') or {}
    if not isinstance(cfg, dict):
        cfg = {}
    if str(cfg.get('provider', '')).strip().lower() == 'custom':        # your own WhatsApp file (see top of this file)
        p = load_custom_provider(cfg, path.parent)
        p.template_sid = ''
        return p
    sid   = os.environ.get('TWILIO_ACCOUNT_SID')          or cfg.get('account_sid', '')
    token = os.environ.get('TWILIO_AUTH_TOKEN')            or cfg.get('auth_token', '')
    service = os.environ.get('TWILIO_VERIFY_SERVICE_SID') or cfg.get('service_sid', '')
    channel = os.environ.get('TWILIO_VERIFY_CHANNEL')     or cfg.get('channel') or 'sms'
    template_sid = os.environ.get('TWILIO_VERIFY_TEMPLATE_SID') or cfg.get('template_sid', '')
    if channel not in TwilioVerify.CHANNELS:
        from cta_storage import DataInvalid
        raise DataInvalid(f'The text-message channel in the settings must be one of: {", ".join(TwilioVerify.CHANNELS)}.')
    if sid and token and service:
        p = TwilioVerify(sid, token, service, channel=channel)
        p.template_sid = template_sid
        return p
    # Decide how to handle missing credentials based on the environment.
    # Resolution order: explicit argument → CTA_ENV env var → top-level config key → sms sub-dict key.
    # 'environment' is documented as a TOP-LEVEL config key; the sms sub-dict fallback is kept for
    # backward compatibility only (it was the original bug: the sub-dict was checked exclusively).
    env = (
        environment
        or os.environ.get('CTA_ENV', '')
        or full_cfg.get('environment', '')          # top-level key (authoritative)
        or cfg.get('environment', '')               # sms sub-dict (legacy fallback)
    ).lower()
    # Production-safe default: only an explicit development/testing environment
    # enables the on-screen fallback.  Every other value — including empty, missing,
    # 'staging', 'qa', or anything unrecognised — blocks sign-in to prevent silent
    # exposure of verification codes in a non-development context.
    _DEV_ENVS = ('development', 'dev', 'testing', 'test')
    if env in _DEV_ENVS:
        # Development / testing: on-screen fallback (explicitly labelled)
        p = OnScreenCodes()
        p.template_sid = ''
        return p
    p = SmsUnavailable(
        'SMS sign-in is not available: Twilio credentials are not configured. '
        'Run python setup_sms.py to set up real SMS, or set CTA_ENV=development for on-screen codes '
        '(development use only).'
    )
    p.template_sid = ''
    return p


def write_sms_config(path: Path, account_sid: str, auth_token: str, service_sid: str,
                    channel: str = 'sms', template_sid: str = '') -> None:
    """Save the Twilio details into cta_config.json, keeping every other setting already in the file.

    *template_sid* is the Twilio Custom Verification Template SID for personalised messages.
    Leave empty to use Twilio's default message.  The template must be approved by Twilio before use.
    Refuses to touch a damaged settings file, keeps the old one as cta_config.json.bak, writes crash-safely.
    """
    from cta_storage import atomic_write_bytes, load_config
    path = Path(path)
    cfg = load_config(path)                                     # raises if the existing file is damaged
    if path.exists():
        atomic_write_bytes(path.with_name(path.name + '.bak'), path.read_bytes())
    sms_cfg: Dict[str, str] = {
        'provider': 'twilio_verify',
        'account_sid': account_sid.strip(),
        'auth_token': auth_token.strip(),
        'service_sid': service_sid.strip(),
        'channel': channel if channel in TwilioVerify.CHANNELS else 'sms',
    }
    if template_sid.strip():
        sms_cfg['template_sid'] = template_sid.strip()
    cfg['sms'] = sms_cfg
    atomic_write_bytes(path, json.dumps(cfg, indent=2).encode('utf-8'))
