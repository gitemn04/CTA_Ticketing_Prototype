"""PDF (and print-out) of an issued zone fare voucher. One small landscape page, 7.5 x 4 in - not A4.

The PDF is drawn from the same ``cta_zone_booking.Voucher`` object as the screen, so the two always show the same
zones, counts, rates, subtotals and total.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas

import cta_zone_booking as zb
from cta_pdf import ACCENT, ACCENT_DARK, ACCENT_SOFT, INK, LINE, MUTED, NAVY, PAPER, T, fit, fonts

VW, VH = 540.0, 288.0            # 7.5 x 4 in
VM = 16.0                        # inner margin


def _box(c: canvas.Canvas, x: float, y: float, w: float, h: float, label: str, value: str, big: bool = False) -> None:
    reg, bold, _ = fonts()
    c.setFillColor(PAPER)
    c.setStrokeColor(LINE)
    c.roundRect(x, y, w, h, 6, stroke=1, fill=1)
    c.setFillColor(MUTED)
    c.setFont(bold, 7)
    c.drawString(x + 8, y + h - 12, T(label.upper()))
    c.setFillColor(INK)
    text, size = fit(value, bold, 15 if big else 13, w - 16, 8)
    c.setFont(bold, size)
    c.drawString(x + 8, y + 9, text)


def build_voucher_pdf(voucher: zb.Voucher, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reg, bold, _ = fonts()
    c = canvas.Canvas(str(path), pagesize=(VW, VH))
    c.setTitle(f'CTA Metro zone fare voucher {voucher.number}')
    c.setAuthor('Centrala Transport Authority')
    c.setSubject(f'{voucher.start_zone} to {voucher.destination_zone}')

    # border + header band
    c.saveState()
    clip = c.beginPath()
    clip.roundRect(3, 3, VW - 6, VH - 6, 9)
    c.clipPath(clip, stroke=0, fill=0)
    hh = 42.0
    c.setFillColor(NAVY)
    c.rect(0, VH - hh - 3, VW, hh + 3, stroke=0, fill=1)
    c.setFillColor(ACCENT)
    c.circle(VM + 15, VH - hh / 2 - 3, 13, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont(bold, 14)
    c.drawCentredString(VM + 15, VH - hh / 2 - 8, 'C')
    c.setFont(bold, 15)
    c.drawString(VM + 37, VH - hh / 2 - 1, 'CTA Metro')
    c.setFont(reg, 7.5)
    c.setFillColor(HexColor('#9FB3CE'))
    c.drawString(VM + 37, VH - hh / 2 - 12, 'Centrala Transport Authority')
    c.setFont(bold, 7.5)
    c.drawRightString(VW - VM, VH - hh / 2 + 6, 'ZONE FARE VOUCHER')
    c.setFillColor(white)
    c.setFont(bold, 12.5)
    c.drawRightString(VW - VM, VH - hh / 2 - 10, T(voucher.number))
    y = VH - hh - 3
    c.setFillColor(ACCENT_SOFT)
    c.rect(0, y - 17, VW, 17, stroke=0, fill=1)
    c.setFillColor(ACCENT_DARK)
    c.setFont(bold, 8)
    c.drawCentredString(VW / 2, y - 12, T(f'Issued {voucher.issued_text}'))
    c.restoreState()
    c.setStrokeColor(LINE)
    c.setLineWidth(1.2)
    c.roundRect(3, 3, VW - 6, VH - 6, 9, stroke=1, fill=0)

    # zones
    top = VH - hh - 3 - 17 - 8
    bw = (VW - 2 * VM - 16) / 3
    bh = 36.0
    _box(c, VM, top - bh, bw, bh, 'Start zone', f'{voucher.start_zone} ({zb.ZONE_NUMBER[voucher.start_zone]})')
    _box(c, VM + bw + 8, top - bh, bw, bh, 'Destination zone',
         f'{voucher.destination_zone} ({zb.ZONE_NUMBER[voucher.destination_zone]})')
    _box(c, VM + 2 * (bw + 8), top - bh, bw, bh, 'Chargeable zones', str(voucher.zones))

    # fare table
    y = top - bh - 16
    cols = [('PASSENGER TYPE', VM + 6, 'l'), ('COUNT', VM + 190, 'r'), ('RATE PER ZONE', VM + 300, 'r'),
            ('ZONES', VM + 360, 'r'), ('SUBTOTAL', VW - VM - 6, 'r')]
    c.setFillColor(MUTED)
    c.setFont(bold, 7)
    for title, x, al in cols:
        (c.drawString if al == 'l' else c.drawRightString)(x, y, title)
    y -= 5
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.line(VM, y, VW - VM, y)
    for ln in voucher.lines:
        y -= 16
        c.setFillColor(INK if ln.count else MUTED)
        c.setFont(bold if ln.count else reg, 10)
        c.drawString(cols[0][1], y, T(ln.label))
        c.setFont(reg, 10)
        c.drawRightString(cols[1][1], y, str(ln.count))
        c.drawRightString(cols[2][1], y, f'{ln.rate_cents:,} cents')
        c.drawRightString(cols[3][1], y, str(ln.zones))
        c.setFont(bold if ln.count else reg, 10)
        c.drawRightString(cols[4][1], y, f'{ln.subtotal_cents:,} cents')
        c.setStrokeColor(LINE)
        c.line(VM, y - 5, VW - VM, y - 5)

    # totals
    y -= 30
    c.setFillColor(ACCENT_SOFT)
    c.roundRect(VM, y - 8, VW - 2 * VM, 30, 6, stroke=0, fill=1)
    c.setFillColor(ACCENT_DARK)
    c.setFont(bold, 10)
    c.drawString(VM + 10, y + 3, f'Total travellers: {voucher.travellers}')
    c.setFont(bold, 15)
    c.drawRightString(VW - VM - 10, y + 2, f'Total fare: {voucher.total_cents:,} cents')

    c.setFillColor(MUTED)
    c.setFont(reg, 7)
    c.drawString(VM, 22, T('Subtotal = count x rate per zone (cents) x chargeable zones.'))
    c.drawString(VM, 12, T(zb.RULE_TEXT + '.'))
    c.showPage()
    c.save()
    return path
