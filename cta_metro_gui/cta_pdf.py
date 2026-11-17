"""Ticket PDF builder (reportlab).

Layout:
  * one BOARDING-PASS page per passenger and journey (landscape 7.5 x 3.5 in, 190 x 89 mm; a return booking gives
    each passenger an outbound and a return pass): header band, payment / status strip, passenger, route, times,
    fare, and a perforated stub with a vector QR code and the ticket number in large type (for people who cannot
    or do not want to scan);
  * one RECEIPT PAGE (optional, A4): booking summary, every passenger and fare, the private recovery code.
    The Print button leaves the receipt out, so only the passes are sent to the printer.

The QR is drawn as vector shapes, so it stays sharp when printed. With a configured public ticket URL it holds that
HTTPS link; otherwise a signed token for the staff scanner. It never holds the private recovery code.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

import cta_core as core
import cta_tickets as tickets
from cta_accounts import mask_phone
from cta_routing import ZONES

W, H = A4
M = 36                                  # page margin
NAVY, INK, MUTED = HexColor('#0B1F3A'), HexColor('#14263F'), HexColor('#6B7A90')
ACCENT, ACCENT_DARK, ACCENT_SOFT = HexColor('#0F766E'), HexColor('#115E59'), HexColor('#D9F2EE')
LINE, PAPER = HexColor('#CBD5E1'), HexColor('#F2F5F9')
WARN_SOFT, WARN_INK = HexColor('#FFF4DB'), HexColor('#7A5200')

# Unicode-capable fonts let names such as "Nguyễn Thị" or "Айдар" print correctly. Without one we fall back
# to the built-in Helvetica (Western European letters only; anything else prints as "?").
_FONT_CANDIDATES = (
    ('C:/Windows/Fonts/segoeui.ttf', 'C:/Windows/Fonts/segoeuib.ttf'),
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    ('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
    ('/Library/Fonts/Arial.ttf', '/Library/Fonts/Arial Bold.ttf'),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
     '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
)
_fonts_cache: Optional[Tuple[str, str, bool]] = None


def fonts() -> Tuple[str, str, bool]:
    """(regular, bold, is_unicode)."""
    global _fonts_cache
    if _fonts_cache is None:
        _fonts_cache = ('Helvetica', 'Helvetica-Bold', False)
        for reg, bold in _FONT_CANDIDATES:
            if Path(reg).is_file() and Path(bold).is_file():
                try:
                    pdfmetrics.registerFont(TTFont('CTA-Sans', reg))
                    pdfmetrics.registerFont(TTFont('CTA-Sans-Bold', bold))
                    _fonts_cache = ('CTA-Sans', 'CTA-Sans-Bold', True)
                    break
                except Exception:  # noqa: BLE001 - probing optional system fonts: an unreadable one is skipped  # optional font probing: an unreadable font is skipped  # nosec B112
                    continue
    return _fonts_cache


def T(text) -> str:
    """Make text safe for the active font (Helvetica cannot draw characters outside Western European)."""
    s = str(text)
    if fonts()[2]:
        return s
    return s.encode('cp1252', 'replace').decode('cp1252')


def name_lines(name: str, font: str, max_w: float) -> List[Tuple[str, float, float]]:
    """Lay the passenger name out as [(text, font size, y offset below the card top)].

    One line at up to 26 pt if it fits (shrinking to 16 pt); otherwise two lines split at the space that
    balances them best, shrunk as needed. The name is never cut off.
    """
    text = T(name)
    size = 26.0
    while size > 16 and pdfmetrics.stringWidth(text, font, size) > max_w:
        size -= 0.5
    if pdfmetrics.stringWidth(text, font, size) <= max_w:
        return [(text, size, 54)]
    words = text.split(' ')
    best = None
    for i in range(1, len(words)):
        a, b = ' '.join(words[:i]), ' '.join(words[i:])
        worst = max(pdfmetrics.stringWidth(a, font, 16), pdfmetrics.stringWidth(b, font, 16))
        if best is None or worst < best[0]:
            best = (worst, a, b)
    lines = [best[1], best[2]] if best else [text[:len(text) // 2], text[len(text) // 2:]]
    size = 18.0
    while size > 9 and max(pdfmetrics.stringWidth(ln, font, size) for ln in lines) > max_w:
        size -= 0.5
    return [(lines[0], size, 42), (lines[1], size, 42 + size + 3)]


def fit(text: str, font: str, size: float, max_w: float, min_size: float = 9) -> Tuple[str, float]:
    """Shrink the font (down to min_size), then truncate with '...' so the text never leaves its box."""
    text = T(text)
    while size > min_size and pdfmetrics.stringWidth(text, font, size) > max_w:
        size -= 0.5
    if pdfmetrics.stringWidth(text, font, size) > max_w:
        while len(text) > 1 and pdfmetrics.stringWidth(text + '...', font, size) > max_w:
            text = text[:-1]
        text += '...'
    return text, size


# ------------------------------------------------------------------ small drawing helpers
def _qr(c: canvas.Canvas, payload: str, x: float, y: float, size: float) -> None:
    """Draw the QR with the same encoder as the screen (``qrcode``: picks the densest mode, so uppercase text gives a
    smaller code with bigger squares). 2 white squares of margin inside *size*, plus the white frame around it."""
    try:
        import qrcode
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
        q.add_data(payload)
        q.make(fit=True)
        matrix = q.get_matrix()
    except Exception:  # noqa: BLE001 - qrcode missing or failing: reportlab's own encoder below
        matrix = None
    if matrix:
        n = len(matrix)
        m = size / n
        c.saveState()
        c.setFillColor(white)
        c.rect(x, y, size, size, stroke=0, fill=1)
        c.setFillColor(black)
        path = c.beginPath()                                 # ONE filled shape: no hairline seams between squares
        for r, row in enumerate(matrix):
            col = 0
            while col < n:                                   # one rectangle per run of dark squares
                if row[col]:
                    start = col
                    while col < n and row[col]:
                        col += 1
                    path.rect(x + start * m, y + size - (r + 1) * m, (col - start) * m, m * 1.002)
                else:
                    col += 1
        c.drawPath(path, stroke=0, fill=1, fillMode=1)       # non-zero winding: touching squares merge
        c.restoreState()
        return
    widget = QrCodeWidget(payload, barLevel='M', barBorder=2)   # plus the white frame around it = quiet zone
    x1, y1, x2, y2 = widget.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x, y)


def _label(c, text, x, y, align='left'):
    c.setFont(fonts()[1], 7.5)
    c.setFillColor(MUTED)
    {'left': c.drawString, 'right': c.drawRightString, 'center': c.drawCentredString}[align](x, y, T(text.upper()))


def _value(c, text, x, y, max_w, size=11.5):
    s, sz = fit(text, fonts()[1], size, max_w, 8)
    c.setFont(fonts()[1], sz)
    c.setFillColor(INK)
    c.drawString(x, y, s)


def _header(c, right_top: str, right_big: str, strip: str = '', strip_kind: str = 'ok') -> None:
    reg, bold, _ = fonts()
    c.setFillColor(NAVY)
    c.rect(0, H - 90, W, 90, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.circle(M + 20, H - 45, 20, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont(bold, 20)
    c.drawCentredString(M + 20, H - 52, 'C')
    c.setFont(bold, 22)
    c.drawString(M + 52, H - 42, 'CTA Metro')
    c.setFont(reg, 9.5)
    c.setFillColor(HexColor('#9FB3CE'))
    c.drawString(M + 52, H - 60, 'Centrala Transport Authority')
    c.setFont(bold, 8)
    c.setFillColor(HexColor('#9FB3CE'))
    c.drawRightString(W - M, H - 34, T(right_top.upper()))
    c.setFont(bold, 22)
    c.setFillColor(white)
    c.drawRightString(W - M, H - 60, T(right_big))
    if strip:
        c.setFillColor(WARN_SOFT if strip_kind == 'warn' else ACCENT_SOFT)
        c.rect(0, H - 114, W, 24, stroke=0, fill=1)
        c.setFillColor(WARN_INK if strip_kind == 'warn' else ACCENT_DARK)
        c.setFont(bold, 9)
        c.drawCentredString(W / 2, H - 106, T(strip))


def _payment_strip(record: dict) -> Tuple[str, str]:
    pay = core.payment_of(record)
    if pay.get('status') == 'pending_counter':
        return f"PAY AT THE COUNTER BEFORE YOU TRAVEL  -  {pay.get('amount_cents', 0):,} cents due", 'warn'
    if pay.get('status') == 'paid' and pay.get('method') != 'legacy':
        if core.payments.is_simulated(pay):
            return f"PAID (TEST MODE - NO MONEY TAKEN)  -  {pay.get('label', '')}", 'warn'
        return (f"PAID  -  {pay['label']}" if pay.get('label') else 'PAID'), 'ok'
    if pay.get('status') in core.payments.UNCONFIRMED:
        return 'PAYMENT NOT CONFIRMED  -  NOT VALID FOR TRAVEL UNTIL CONFIRMED', 'warn'
    return '', 'ok'


def _footer(c, page_no: int, pages: int) -> None:
    reg = fonts()[0]
    c.setStrokeColor(LINE)
    c.setLineWidth(0.6)
    c.line(M, 54, W - M, 54)
    c.setFont(reg, 7.5)
    c.setFillColor(MUTED)
    c.drawString(M, 40, 'Centrala Transport Authority  |  Keep this ticket for the whole journey.')
    c.drawRightString(W - M, 40, f'Page {page_no} of {pages}')


def _para(c, html: str, x: float, y_top: float, width: float, size=9.5, leading=13.5, color=INK, bold=False) -> float:
    """Draw wrapped text with its top edge at y_top; return the height used."""
    style = ParagraphStyle('p', fontName=fonts()[1 if bold else 0], fontSize=size, leading=leading, textColor=color)
    p = Paragraph(html, style)
    _, h = p.wrap(width, 10_000)
    p.drawOn(c, x, y_top - h)
    return h


def _esc(text) -> str:
    return T(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _zone_path(stations: List[str]) -> List[str]:
    seq: List[str] = []
    for st in stations:
        z = ZONES.get(st)
        if z and (not seq or seq[-1] != z):
            seq.append(z)
    return seq


def _times(record: dict) -> Tuple[str, str]:
    """(departure text, arrival text) from the saved record; 'n/a' if the record has no times."""
    try:
        dep = datetime.strptime(record['depart_at'], '%Y-%m-%d %H:%M')
    except (KeyError, ValueError):
        return 'n/a', 'n/a'
    dep_txt = f'{dep:%a %d %b %Y}   {dep:%H:%M}'
    if 'ride_minutes' not in record:
        return dep_txt, 'n/a'
    arr = dep + timedelta(minutes=float(record['ride_minutes']), seconds=30)
    arr_txt = f'{arr:%H:%M}' + ('  (next day)' if arr.date() != dep.date() else '')
    return dep_txt, arr_txt


# ------------------------------------------------------------------ ticket page
def _leg_strip(record: dict, pax: dict, leg: str) -> Tuple[str, str]:
    """Banner for one journey: a cancelled or refunded journey says so; otherwise the payment banner."""
    status = tickets.leg_fields(pax, leg)[1]
    if status == 'cancelled':
        return 'CANCELLED  -  NOT VALID FOR TRAVEL', 'warn'
    if status == 'refunded':
        return 'REFUNDED  -  NOT VALID FOR TRAVEL', 'warn'
    return _payment_strip(record)


# Boarding-pass ticket page: 7.5 x 3.5 in (190.5 x 88.9 mm), landscape. The receipt page stays A4.
BW, BH = 540.0, 252.0
BM = 14.0                                  # margin inside the pass
STUB_W = 140.0                             # width of the QR stub on the right


def _bp_name_lines(name: str, font: str, max_w: float) -> List[Tuple[str, float]]:
    """The passenger name as [(text, font size)]: one line up to 19 pt, otherwise two balanced lines. Never cut off."""
    text = T(name)
    size = 19.0
    while size > 12 and pdfmetrics.stringWidth(text, font, size) > max_w:
        size -= 0.5
    if pdfmetrics.stringWidth(text, font, size) <= max_w:
        return [(text, size)]
    words = text.split(' ')
    best = None
    for i in range(1, len(words)):
        a, b = ' '.join(words[:i]), ' '.join(words[i:])
        worst = max(pdfmetrics.stringWidth(a, font, 13), pdfmetrics.stringWidth(b, font, 13))
        if best is None or worst < best[0]:
            best = (worst, a, b)
    lines = [best[1], best[2]] if best else [text[:len(text) // 2], text[len(text) // 2:]]
    size = 13.0
    while size > 8 and max(pdfmetrics.stringWidth(ln, font, size) for ln in lines) > max_w:
        size -= 0.5
    return [(lines[0], size), (lines[1], size)]


def _bp_frame(c: canvas.Canvas, right_top: str, right_big: str, strip: str, strip_kind: str) -> float:
    """Border, header band and status strip of a boarding pass. Returns the y where the body starts."""
    reg, bold, _ = fonts()
    c.saveState()
    clip = c.beginPath()
    clip.roundRect(3, 3, BW - 6, BH - 6, 9)
    c.clipPath(clip, stroke=0, fill=0)
    hh = 40.0
    c.setFillColor(NAVY)
    c.rect(0, BH - hh - 3, BW, hh + 3, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.circle(BM + 15, BH - hh / 2 - 3, 13, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont(bold, 14)
    c.drawCentredString(BM + 15, BH - hh / 2 - 8, 'C')
    c.setFont(bold, 15)
    c.drawString(BM + 37, BH - hh / 2 - 1, 'CTA Metro')
    c.setFont(reg, 7.5)
    c.setFillColor(HexColor('#9FB3CE'))
    c.drawString(BM + 37, BH - hh / 2 - 12, 'Centrala Transport Authority')
    c.setFont(bold, 7)
    c.drawRightString(BW - BM, BH - hh / 2 + 6, T(right_top.upper()))
    c.setFont(bold, 15)
    c.setFillColor(white)
    c.drawRightString(BW - BM, BH - hh / 2 - 10, T(right_big))
    y = BH - hh - 3
    if strip:
        c.setFillColor(WARN_SOFT if strip_kind == 'warn' else ACCENT_SOFT)
        c.rect(0, y - 16, BW, 16, stroke=0, fill=1)
        c.setFillColor(WARN_INK if strip_kind == 'warn' else ACCENT_DARK)
        text, size = fit(strip, bold, 7.5, BW - 2 * BM, 6)
        c.setFont(bold, size)
        c.drawCentredString(BW / 2, y - 11, text)
        y -= 16
    c.restoreState()
    c.setStrokeColor(LINE)
    c.setLineWidth(1.2)
    c.roundRect(3, 3, BW - 6, BH - 6, 9, stroke=1, fill=0)
    return y


def _ticket_page(c: canvas.Canvas, record: dict, pax: dict, n: int, count: int, page_no: int, pages: int,
                 leg: str = 'out', signer=None, ticket_url: str = '') -> None:
    """One passenger, one journey, on a boarding-pass sized page (see the module docstring)."""
    reg, bold, _ = fonts()
    back = leg == 'return'
    strip, kind = _leg_strip(record, pax, leg)
    yt = _bp_frame(c, 'Return boarding pass' if back else 'Boarding pass', f'{n} of {count}', strip, kind)
    ticket_no = pax.get('return_ticket_no', pax['ticket_no']) if back else pax['ticket_no']
    leg_fare = pax.get('leg_fare_cents', pax['fare_cents'])
    start_name, end_name = ((record.get('to_station', ''), record.get('from_station', '')) if back
                            else (record.get('from_station', ''), record.get('to_station', '')))

    split = BW - BM - STUB_W                  # x of the dashed perforation between details and QR stub
    x0 = BM + 6
    lw = split - x0 - 12                      # width of the details column
    c.setDash(3, 3)
    c.setStrokeColor(LINE)
    c.setLineWidth(1)
    c.line(split, yt - 8, split, 12)
    c.setDash()

    # passenger (the most important item on the pass) and ticket type
    _label(c, 'Passenger', x0, yt - 14)
    cat, csz = fit(pax['category'], bold, 8.5, lw * 0.4, 6.5)
    c.setFont(bold, csz)
    c.setFillColor(ACCENT_DARK)
    c.drawRightString(split - 12, yt - 14, cat)
    c.setFillColor(INK)
    ny = yt - 18
    for text, size in _bp_name_lines(pax['name'], bold, lw):
        ny -= size + 2
        c.setFont(bold, size)
        c.drawString(x0, ny, text)

    # route with a drawn arrow (Helvetica has no arrow glyph)
    a, b = T(start_name), T(end_name)
    size = 17.0
    arrow = 30
    while size > 10 and pdfmetrics.stringWidth(a, bold, size) + pdfmetrics.stringWidth(b, bold, size) + arrow + 16 > lw:
        size -= 0.5
    ay = yt - 70
    c.setFont(bold, size)
    c.setFillColor(INK)
    c.drawString(x0, ay, a)
    ax = x0 + pdfmetrics.stringWidth(a, bold, size) + 8
    c.setStrokeColor(ACCENT)
    c.setFillColor(ACCENT)
    c.setLineWidth(1.6)
    c.line(ax, ay + size * 0.33, ax + arrow - 6, ay + size * 0.33)
    arrow_head = c.beginPath()
    arrow_head.moveTo(ax + arrow, ay + size * 0.33)
    arrow_head.lineTo(ax + arrow - 7, ay + size * 0.33 + 4)
    arrow_head.lineTo(ax + arrow - 7, ay + size * 0.33 - 4)
    arrow_head.close()
    c.drawPath(arrow_head, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont(bold, size)
    c.drawString(ax + arrow + 8, ay, b)

    # detail grid: 3 columns x 2 rows
    dep_txt, arr_txt = _times(record)
    km = f"{record['km']:.2f} km" if 'km' in record else 'n/a'
    if 'km' in record and not record.get('distance_verified'):
        km += ' (estimate)'
    first = ([('Valid on', dep_txt.split('   ')[0]), ('Travel', 'Any train that day')] if back
             else [('Departs', dep_txt), ('Arrives (estimate)', arr_txt)])
    cells = first + [
        ('Distance', km), ('Zones charged', str(record.get('zones', ''))),
        ('Fare for this ticket', f"{leg_fare:,} cents"), ('Booking reference', record.get('reference', '')),
    ]
    colw = lw / 3
    for i, (lab, val) in enumerate(cells):
        cx0 = x0 + (i % 3) * colw
        cy = yt - 92 - (i // 3) * 32
        _label(c, lab, cx0, cy)
        _value(c, val, cx0, cy - 14, colw - 8, 10.5)

    note = ('Show the QR code to station staff, or read out the ticket number. '
            + ('Valid for the journey back on the day of travel, on any train.' if back
               else 'Keep this pass for the whole journey.'))
    _para(c, _esc(note), x0, yt - 148, lw, size=7.5, leading=9.5, color=MUTED)
    c.setFont(reg, 6.5)
    c.setFillColor(MUTED)
    phone = record.get('account_phone')
    booked = f"Issued {record.get('issued_at', '')}" + (f"   |   Booked by {mask_phone(phone, '*')}" if phone else '')
    c.drawString(x0, 13, T(booked))

    # QR stub
    cx = (split + BW - BM) / 2
    _label(c, 'Scan or show this code', cx, yt - 14, 'center')
    qs = 108
    qy = yt - 22 - qs
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.roundRect(cx - qs / 2 - 5, qy - 5, qs + 10, qs + 10, 5, stroke=1, fill=1)
    is_link = False
    try:
        # v3.0: if a ticket URL is provided, embed it in the QR so a phone camera opens the ticket page.
        # Fall back to the signed token (for staff scanner compatibility) if no URL is configured.
        if ticket_url:
            qr_content, is_link = ticket_url, True
        else:
            qr_content = core.ticket_qr_text(record, pax, leg, signer)
        _qr(c, qr_content, cx - qs / 2, qy, qs)
    except tickets.KeyUnavailable:                  # never print a QR that cannot be verified
        c.setFont(reg, 7.5)
        c.setFillColor(MUTED)
        c.drawCentredString(cx, qy + qs / 2 + 4, 'QR unavailable')
        c.drawCentredString(cx, qy + qs / 2 - 7, 'Use the number below')
    tn_y = qy - 22
    _label(c, 'Ticket number', cx, tn_y, 'center')
    tn_size = 10.0
    while tn_size > 7 and pdfmetrics.stringWidth(ticket_no, 'Courier-Bold', tn_size) > STUB_W - 10:
        tn_size -= 0.5
    c.setFont('Courier-Bold', tn_size)
    c.setFillColor(INK)
    c.drawCentredString(cx, tn_y - 13, ticket_no)
    c.setFont(reg, 6.5)
    c.setFillColor(MUTED)
    c.drawCentredString(cx, tn_y - 25, 'Opens this ticket on a phone' if is_link else 'Scan with a phone to read this ticket')


# ------------------------------------------------------------------ receipt page
def _receipt_page(c: canvas.Canvas, record: dict, pax_list: List[dict], page_no: int, pages: int) -> None:
    reg, bold, _ = fonts()
    strip, kind = _payment_strip(record)
    _header(c, 'Booking receipt', record.get('reference', ''), strip, kind)
    y = H - 146
    c.setFont(bold, 18)
    c.setFillColor(INK)
    s, sz = fit(f"{record.get('from_station', '')}  to  {record.get('to_station', '')}", bold, 18, W - 2 * M, 12)
    c.setFont(bold, sz)
    c.drawString(M, y, s)
    dep_txt, arr_txt = _times(record)
    info = [
        ('Departs', dep_txt), ('Arrives (estimate)', arr_txt),
        ('Distance', f"{record['km']:.2f} km" if 'km' in record else 'n/a'),
        ('Zones charged', str(record.get('zones', ''))),
        ('Issued', record.get('issued_at', '')),
        ('Booked by', mask_phone(record['account_phone'], '*') if record.get('account_phone') else 'n/a'),
    ]
    colw = (W - 2 * M) / 3
    for i, (lab, val) in enumerate(info):
        cx = M + (i % 3) * colw
        cy = y - 30 - (i // 3) * 38
        _label(c, lab, cx, cy)
        _value(c, val, cx, cy - 16, colw - 8, 11)
    y -= 30 + 38 + 40

    _label(c, 'Passengers and fares', M, y)
    cell = ParagraphStyle('cell', fontName=reg, fontSize=9, leading=11, textColor=INK)
    cellb = ParagraphStyle('cellb', parent=cell, fontName=bold)
    head = ParagraphStyle('head', parent=cell, fontName=bold, textColor=white)
    rows = [[Paragraph(h, head) for h in ('No.', 'Passenger', 'Type', 'Ticket number', 'Fare (cents)')]]
    for p in pax_list:
        rows.append([Paragraph(str(p['no']), cell), Paragraph(_esc(p['name']), cellb), Paragraph(_esc(p['category']), cell),
                     Paragraph(f'<font name="Courier-Bold">{_esc(p["ticket_no"])}</font>'
                               + (f'<br/><font name="Courier-Bold">{_esc(p["return_ticket_no"])}</font>'
                                  if p.get('return_ticket_no') else ''), cell),
                     Paragraph(f"{p['fare_cents']:,}", cell)])
    rows.append([Paragraph('', cell), Paragraph('<b>Total</b>', cell), Paragraph('', cell), Paragraph('', cell),
                 Paragraph(f"<b>{record.get('total_cents', 0):,} cents</b>", cell)])
    table = Table(rows, colWidths=[30, 180, 80, 150, 83], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('ROWBACKGROUNDS', (0, 1), (-1, -2), [white, PAPER]),
        ('BACKGROUND', (0, -1), (-1, -1), ACCENT_SOFT),
        ('LINEBELOW', (0, 0), (-1, -1), 0.4, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
    ]))
    _, th = table.wrap(W - 2 * M, 10_000)
    table.drawOn(c, M, y - 10 - th)
    y = y - 10 - th - 26

    c.setFillColor(ACCENT_SOFT)
    c.roundRect(M, y - 78, W - 2 * M, 78, 8, stroke=0, fill=1)
    _label(c, 'Private recovery code', M + 16, y - 20)
    c.setFont('Courier-Bold', 20)
    c.setFillColor(ACCENT_DARK)
    c.drawString(M + 16, y - 46, record.get('recovery_code', ''))
    _para(c, 'Keep this page private. With the booking reference, this code is the only way to get the tickets back. '
             'Print the ticket pages for the passengers, not this page.', M + 16, y - 54, W - 2 * M - 32,
          size=8.5, leading=11, color=INK)
    y -= 78 + 22
    pay = core.payment_of(record)
    trip = 'Return (2 journeys per passenger)' if record.get('legs', 1) == 2 else 'One way'
    notes = (f"Trip: {trip}.   Payment: {pay.get('label', '')} - {core.payments.status_text(pay)}"
             + (f" (ref {pay['transaction_id']})" if pay.get('transaction_id') else '')
             + f".   Fare policy {record.get('fare_policy_version', core.FARE_POLICY_VERSION)}.")
    _para(c, _esc(notes), M, y, W - 2 * M, size=8.5, color=MUTED)
    _footer(c, page_no, pages)


# ------------------------------------------------------------------ public entry point
def build_ticket_pdf(record: dict, path: Path, include_receipt: bool = True, signer=None,
                    ticket_url_map: dict = None, leg_override: str = '') -> None:
    """Build a ticket PDF. *ticket_url_map* maps ticket_id -> https URL for URL-based QR codes.

    *leg_override*: when set to 'out' or 'return', renders only that journey leg.
    Use this when a web token authorises exactly one leg of a multi-leg booking.
    """
    if ticket_url_map is None:
        ticket_url_map = {}
    path.parent.mkdir(parents=True, exist_ok=True)
    pax_list = core.passengers_of(record)
    if leg_override in ('out', 'return'):
        legs = [(leg_override, None)]
    else:
        legs = [('out', None)] + ([('return', None)] if record.get('legs', 1) == 2 and not pax_list[0].get('legacy') else [])
    count_tickets = len(pax_list) * len(legs)
    pages = count_tickets + (1 if include_receipt else 0)
    c = canvas.Canvas(str(path), pagesize=(BW, BH))          # ticket pages are boarding-pass sized
    c.setTitle(f"CTA Metro ticket {record.get('reference', '')}")
    c.setAuthor('Centrala Transport Authority')
    c.setSubject(f"{record.get('from_station', '')} to {record.get('to_station', '')}")
    k = 0
    for pax in pax_list:
        for leg, _ in legs:
            k += 1
            # Look up a URL QR for this specific ticket leg
            if leg == 'return':
                _tid = pax.get('return_ticket_id', '')
            else:
                _tid = pax.get('ticket_id', '')
            _url = ticket_url_map.get(_tid, '') if _tid else ''
            _ticket_page(c, record, pax, k, count_tickets, k, pages, leg, signer, ticket_url=_url)
            c.showPage()
    if include_receipt:
        c.setPageSize(A4)                                       # the receipt is a normal A4 page
        _receipt_page(c, record, pax_list, pages, pages)
        c.showPage()
    c.save()
