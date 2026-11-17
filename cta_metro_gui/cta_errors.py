"""One way to handle errors in every layer (GUI, storage, payments, routing, tickets).

* EXPECTED errors carry a message written for people: ``StorageError``, ``FareError``, ``FarePolicyError``,
  ``PermissionDenied``, ``AuthError``, ``PaymentUnavailable``, ``RoutingConfigError``, ``KeyUnavailable`` and the
  ``ValueError``s raised by the validators. ``user_message`` shows that message as it is.
* UNEXPECTED errors (anything else) are bugs or environment failures. The person sees a short, plain message with a
  reference such as ``ERR-20261025-7F3A2C``; the full details (type, message, traceback) go to
  ``<data folder>/logs/diagnostics.log`` for whoever maintains the app. Nothing is swallowed silently.
* Secrets never reach the log: every value registered with ``register_secret`` (API keys, tokens, signing keys) is
  replaced by ``***`` before writing, and so is anything that looks like ``key=...`` / ``token: ...``.

``report`` never raises: diagnostics must not turn one failure into two.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

LOG_NAME = 'diagnostics.log'
MAX_LOG_BYTES = 1_000_000                     # one file of about 1 MB plus one previous file
_SECRETS: Set[str] = set()
_STATE: Dict[str, Optional[Path]] = {'folder': None}
_LOCK = threading.Lock()
_SECRET_FIELDS = re.compile(r'(?i)\b(api[_-]?key|auth[_-]?token|token|secret|password|passwd|pwd|otp|code)'
                            r'(["\']?\s*[:=]\s*["\']?)([^\s"\',;&]+)')

GENERIC = 'Something went wrong and this action may not have finished. Please check before trying again.'


def configure(folder: Optional[Path]) -> None:
    """Where diagnostics are written (the app's data folder). Without one, they go to the user's home folder."""
    _STATE['folder'] = Path(folder) if folder else None


def register_secret(value) -> None:
    """Never write this value to the diagnostics log. Short values are ignored (they would mask ordinary text)."""
    if isinstance(value, (bytes, bytearray)):
        value = value.hex()
    if isinstance(value, str) and len(value.strip()) >= 6:
        _SECRETS.add(value.strip())


def register_known_secrets() -> None:
    for name in ('CTA_TICKET_KEY', 'CTA_TICKET_KEY_PREVIOUS', 'CTA_GOOGLE_API_KEY', 'TWILIO_AUTH_TOKEN',
                 'TWILIO_ACCOUNT_SID', 'TWILIO_VERIFY_SERVICE_SID'):
        register_secret(os.environ.get(name, ''))


def scrub(text: str) -> str:
    text = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(s, '***')
    return _SECRET_FIELDS.sub(lambda m: m.group(1) + m.group(2) + '***', text)


def new_incident_id(now: Optional[datetime] = None) -> str:
    return f"ERR-{(now or datetime.now()):%Y%m%d}-{secrets.token_hex(3).upper()}"


def log_path() -> Path:
    folder = _STATE['folder'] or Path.home()
    return Path(folder) / 'logs' / LOG_NAME


def report(exc: BaseException, context: str, now: Optional[datetime] = None, **facts) -> str:
    """Write one diagnostics entry and return its reference id. Never raises."""
    incident = new_incident_id(now)
    try:
        entry = {
            'id': incident,
            'at': (now or datetime.now()).isoformat(timespec='seconds'),
            'context': scrub(context)[:200],
            'type': f'{type(exc).__module__}.{type(exc).__name__}',
            'message': scrub(str(exc))[:500],
            'facts': {k: scrub(str(v))[:200] for k, v in facts.items()},
            'traceback': scrub(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-8000:],
        }
        path = log_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
                os.replace(path, path.with_suffix('.log.1'))          # keep the previous file, drop older ones
            with path.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:  # noqa: BLE001 - the diagnostics writer itself must never fail the caller  # the diagnostics writer must never raise into the caller  # nosec B110
        pass
    return incident


def note(context: str, message: str, now: Optional[datetime] = None, **facts) -> str:
    """Record a handled problem that is not an exception (for example an audit row that could not be written)."""
    return report(RuntimeError(message), context, now, **facts)


def is_expected(exc: BaseException) -> bool:
    """True for errors whose message was written for people (see module docstring)."""
    names = {c.__name__ for c in type(exc).__mro__}
    expected = {'StorageError', 'FareError', 'FarePolicyError', 'PermissionDenied', 'AuthError', 'PaymentUnavailable',
                'RoutingConfigError', 'KeyUnavailable', 'ValueError'}
    return bool(names & expected) and not isinstance(exc, (UnicodeError,))


def user_message(exc: BaseException, context: str = '', now: Optional[datetime] = None) -> str:
    """What to show a person. Expected errors: their own message. Anything else: a plain message plus a reference,
    with the details recorded for maintenance. Never a traceback, a path or a secret."""
    if is_expected(exc):
        return scrub(str(exc)) or GENERIC
    incident = report(exc, context or 'unexpected error', now)
    return f'{GENERIC} (Reference {incident}.)'
