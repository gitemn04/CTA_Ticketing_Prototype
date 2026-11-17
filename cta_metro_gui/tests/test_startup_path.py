"""Normal startup path: `python cta_ticket_server.py` (what launch_ticket_server.bat runs).

These tests start the real entry point as a subprocess against a real cta_db.Store
database and a real cta_config.json, fetch real PDFs over HTTP, render them and decode
the QR with OpenCV. No direct build_app() calls, no SMS, no network beyond 127.0.0.1.

The fake domain below is a test value only; it is not a real or deployed endpoint.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Talk to 127.0.0.1 directly; never through an environment HTTP proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from pathlib import Path

import pytest

APP_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from test_pdf_and_sms_verification import _make_real_booking  # noqa: E402

BASE = 'https://tickets.example.test'   # reserved .test TLD, never resolvable
SERVER = APP_DIR / 'cta_ticket_server.py'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _env(**extra):
    import os
    env = {k: v for k, v in os.environ.items() if k != 'CTA_TICKET_SERVER_URL'}
    env.update(extra)
    return env


def _run_check(db_path, *args, **env_extra):
    return subprocess.run(
        [sys.executable, str(SERVER), '--db', str(db_path), '--check', *args],
        capture_output=True, text=True, timeout=60, env=_env(**env_extra), cwd=str(APP_DIR))


class _Server:
    def __init__(self, db_path, *args, **env_extra):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), '--db', str(db_path),
             '--port', str(self.port), *args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_env(**env_extra), cwd=str(APP_DIR))
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                _OPENER.open(f'http://127.0.0.1:{self.port}/ticket/x', timeout=1)
            except urllib.error.HTTPError:
                return
            except Exception:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError('server did not start')

    def get(self, path, headers=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', headers=headers or {})
        try:
            with _OPENER.open(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _decode_qr(pdf_bytes: bytes, tmp: Path) -> str:
    import cv2
    import numpy as np
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_bytes)
    assert len(pdf) == 1
    img = np.array(pdf[0].render(scale=4).to_pil().convert('RGB'))
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return text


@pytest.fixture
def booking(tmp_path):
    store, principal, record, db_path = _make_real_booking(tmp_path)
    return tmp_path, db_path, record


def _write_cfg(tmp, url):
    (tmp / 'cta_config.json').write_text(json.dumps({'ticket_server': {'url': url}}))


def test_config_url_is_loaded_by_entry_point(booking):
    tmp, db, _ = booking
    _write_cfg(tmp, BASE)
    r = _run_check(db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert BASE in r.stdout and 'cta_config.json' in r.stdout


def test_no_config_starts_and_says_no_qr(booking):
    tmp, db, _ = booking
    r = _run_check(db)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'NOT CONFIGURED' in r.stdout


@pytest.mark.parametrize('bad', ['http://tickets.example.test', 'https://localhost',
                                 'https://127.0.0.1', 'tickets.example.test', 'https://nodot'])
def test_invalid_configured_url_refuses_to_start(booking, bad):
    tmp, db, _ = booking
    _write_cfg(tmp, bad)
    r = _run_check(db)
    assert r.returncode == 2, r.stdout + r.stderr
    assert 'invalid public ticket URL' in r.stdout


def test_damaged_config_refuses_to_start(booking):
    tmp, db, _ = booking
    (tmp / 'cta_config.json').write_text('{not json')
    r = _run_check(db)
    assert r.returncode != 0


def test_cli_and_env_override_precedence(booking):
    tmp, db, _ = booking
    _write_cfg(tmp, BASE)
    r = _run_check(db, '--url', 'https://cli.example.test')
    assert 'https://cli.example.test' in r.stdout and '--url' in r.stdout
    r = _run_check(db, CTA_TICKET_SERVER_URL='https://env.example.test')
    assert 'https://env.example.test' in r.stdout


def test_served_pdf_qr_decodes_to_exact_url_for_all_four_links(booking):
    """Real server process, real config, all four links: PDF QR == exact HTTPS URL."""
    from cta_ticket_server import issue_web_token
    tmp, db, record = booking
    _write_cfg(tmp, BASE)
    srv = _Server(db)
    try:
        for pax in record['passengers']:
            for tid in (pax['ticket_id'], pax['return_ticket_id']):
                token = issue_web_token(str(db), tid)
                code, page = srv.get(f'/ticket/{token}')
                assert code == 200
                code, pdf = srv.get(f'/ticket/{token}/pdf',
                                    headers={'Host': 'evil.attacker.test'})
                assert code == 200 and pdf[:4] == b'%PDF'
                assert _decode_qr(pdf, tmp) == f'{BASE}/ticket/{token}', tid
    finally:
        srv.stop()


def test_without_config_served_pdf_has_no_https_qr(booking):
    from cta_ticket_server import issue_web_token
    tmp, db, record = booking
    srv = _Server(db)
    try:
        token = issue_web_token(str(db), record['passengers'][0]['ticket_id'])
        code, pdf = srv.get(f'/ticket/{token}/pdf', headers={'Host': 'evil.attacker.test'})
        assert code == 200
        decoded = _decode_qr(pdf, tmp)
        assert 'evil.attacker' not in decoded and not decoded.startswith('http')
    finally:
        srv.stop()


def test_launcher_forwards_args_and_documents_config():
    bat = (APP_DIR / 'launch_ticket_server.bat').read_text()
    assert 'cta_ticket_server.py' in bat and '%*' in bat
    assert 'ticket_server' in bat and 'CTA_TICKET_SERVER_URL' in bat
    assert 'ERRORLEVEL' in bat
