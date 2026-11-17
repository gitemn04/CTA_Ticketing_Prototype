"""Screens for the zone-only booking (ATHE Unit 2, Appendix 2), mixed into ``cta_app.CTAApp``.

* 'zone'    - Zone ticket: choose a start zone and a destination zone, type the four passenger counts, Issue voucher.
* 'board'   - Station board: every station grouped by zone, A-Z within each zone.
* 'voucher' - the issued voucher, with Print / Save PDF / Open PDF and the two choices Issue another voucher | Exit.

All fare arithmetic lives in ``cta_zone_booking``; the PDF in ``cta_voucher_pdf``. This file only draws and wires.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Dict, Optional

import cta_core as core
import cta_zone_booking as zb
from cta_widgets import (ACCENT_DARK, ACCENT_SOFT, BG, CARD, DANGER, INK, LINE, MUTED, ZONE_COLOR, Card, F,
                         RoundButton, ScrollFrame, px)

ZONE_HINT = {'Central': 'zone 1', 'Midtown': 'zone 2', 'Downtown': 'zone 3'}


class ZoneChoice(tk.Frame):
    """Three zone buttons; nothing is chosen until the passenger presses one (``value`` is None)."""

    def __init__(self, master, on_change, parent_bg=CARD):
        super().__init__(master, bg=parent_bg)
        self.value: Optional[str] = None
        self.on_change = on_change
        self.buttons: Dict[str, RoundButton] = {}
        for z in zb.ZONE_ORDER:
            b = RoundButton(self, f'{z}  ({zb.ZONE_NUMBER[z]})', lambda zz=z: self.select(zz), width=150, height=40,
                            radius=10, kind='ghost', font=F(10, True), parent_bg=parent_bg)
            b.pack(side='left', padx=(0, 8))
            self.buttons[z] = b

    def select(self, zone: Optional[str]):
        self.value = zone
        for z, b in self.buttons.items():
            b.set_kind('primary' if z == zone else 'ghost')
        self.on_change()

    def clear(self):
        self.select(None)


class ZoneBookingMixin:
    """Methods added to CTAApp. Uses self.root, self.body, self.pages, self.clock, self.folder, self.show_page."""

    # ------------------------------------------------------------ zone ticket page
    def _build_zone(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['zone'] = page
        page.grid_columnconfigure(0, weight=3, uniform='zp')
        page.grid_columnconfigure(1, weight=2, uniform='zp')
        page.grid_rowconfigure(0, weight=1)
        self.voucher: Optional[zb.Voucher] = None
        self.voucher_note = ''

        form = Card(page, pad=8)
        form.grid(row=0, column=0, sticky='nsew', padx=(20, 8), pady=10)
        # the form scrolls when the window is short (1366 x 768 laptops), so Issue voucher is never cut off
        self.zone_scroll = ScrollFrame(form.body, width=px(560, self.root))
        self.zone_scroll.pack(fill='both', expand=True)
        b = tk.Frame(self.zone_scroll.body, bg=CARD)
        b.pack(fill='x', padx=(12, 12), pady=(8, 8))
        tk.Label(b, text='Buy a zone ticket', bg=CARD, fg=INK, font=F(18, True)).pack(anchor='w')
        tk.Label(b, text='Choose the zones you travel between. You do not need to pick a station.',
                 bg=CARD, fg=MUTED, font=F(10)).pack(anchor='w', pady=(0, 8))

        tk.Label(b, text='1   START ZONE', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(0, 4))
        self.zone_from = ZoneChoice(b, self._zone_changed)
        self.zone_from.pack(anchor='w')
        tk.Label(b, text='2   DESTINATION ZONE', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(8, 4))
        self.zone_to = ZoneChoice(b, self._zone_changed)
        self.zone_to.pack(anchor='w')
        tk.Label(b, text='The same zone for both is fine: a journey can stay inside one zone.', bg=CARD, fg=MUTED,
                 font=F(9)).pack(anchor='w', pady=(4, 0))

        tk.Label(b, text='3   HOW MANY PEOPLE ARE TRAVELLING?', bg=CARD, fg=MUTED, font=F(9, True)
                 ).pack(anchor='w', pady=(10, 4))
        grid = tk.Frame(b, bg=CARD)
        grid.pack(fill='x')
        self.zone_count_vars: Dict[str, tk.StringVar] = {}
        self.zone_count_entries: Dict[str, tk.Entry] = {}
        for i, cat in enumerate(zb.CATEGORIES):
            cell = tk.Frame(grid, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=10, pady=6)
            cell.grid(row=i // 2, column=i % 2, sticky='nsew', padx=(0, 8) if i % 2 == 0 else 0, pady=3)
            grid.grid_columnconfigure(i % 2, weight=1, uniform='zc')
            info = tk.Frame(cell, bg=CARD)
            info.pack(side='left', fill='x', expand=True)
            tk.Label(info, text=zb.CATEGORY_PLURAL[cat], bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w')
            tk.Label(info, text=core.CATEGORY_HINT[cat], bg=CARD, fg=MUTED, font=F(8)).pack(anchor='w')
            tk.Label(info, text=f'{zb.fares.FARE_RATES_CENTS[cat]:,} cents per zone', bg=CARD, fg=MUTED, font=F(8)
                     ).pack(anchor='w')
            var = tk.StringVar(value='0')
            ctl = tk.Frame(cell, bg=CARD)
            ctl.pack(side='right')
            RoundButton(ctl, '–', lambda c=cat: self._zone_step(c, -1), width=32, height=32, radius=16, kind='ghost',
                        font=F(13, True)).pack(side='left')
            box = tk.Frame(ctl, bg=LINE, padx=1, pady=1)
            box.pack(side='left', padx=4)
            e = tk.Entry(box, textvariable=var, width=3, justify='center', relief='flat', font=F(13, True),
                         bg='#F7F9FC', fg=INK, highlightthickness=0, insertbackground=INK)
            e.pack(ipady=3)
            e.bind('<Return>', lambda ev: self.issue_zone_voucher())
            RoundButton(ctl, '+', lambda c=cat: self._zone_step(c, 1), width=32, height=32, radius=16, kind='soft',
                        font=F(13, True)).pack(side='left')
            var.trace_add('write', lambda *_a: self._zone_changed())
            self.zone_count_vars[cat] = var
            self.zone_count_entries[cat] = e

        self.zone_preview = tk.Label(b, text='', bg=ACCENT_SOFT, fg=ACCENT_DARK, font=F(11, True), anchor='w',
                                     padx=12, pady=8)
        self.zone_preview.pack(fill='x', pady=(10, 0))
        self.zone_err = tk.Label(b, text='', bg=CARD, fg=DANGER, font=F(10, True), wraplength=px(560, self.root),
                                 justify='left')
        self.zone_err.pack(anchor='w', pady=(4, 0))
        row = tk.Frame(b, bg=CARD)
        row.pack(fill='x', pady=(6, 0))
        self.zone_issue_btn = RoundButton(row, 'Issue voucher', self.issue_zone_voucher, width=220, height=50,
                                          radius=14, font=F(13, True))
        self.zone_issue_btn.pack(side='left')
        RoundButton(row, 'Clear', self.reset_zone_form, width=100, height=50, radius=14, kind='ghost',
                    font=F(11, True)).pack(side='left', padx=10)

        # right: how the fare works + the optional station planner
        side = tk.Frame(page, bg=BG)
        side.grid(row=0, column=1, sticky='nsew', padx=(8, 20), pady=10)
        info = Card(side, pad=18)
        info.pack(fill='x')
        tk.Label(info.body, text='How zone fares work', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        tk.Label(info.body, text=('Central is zone 1, Midtown zone 2, Downtown zone 3.\n'
                                  'Chargeable zones = the difference between the zone numbers + 1.\n'
                                  'Fare = travellers x rate per zone x chargeable zones.'),
                 bg=CARD, fg=INK, font=F(10), justify='left', wraplength=px(400, self.root)).pack(anchor='w', pady=(4, 8))
        tbl = tk.Frame(info.body, bg=CARD)
        tbl.pack(anchor='w')
        tk.Label(tbl, text='from \\ to', bg=CARD, fg=MUTED, font=F(8, True)).grid(row=0, column=0, padx=4, pady=2)
        for j, z in enumerate(zb.ZONE_ORDER):
            tk.Label(tbl, text=z, bg=CARD, fg=MUTED, font=F(9, True)).grid(row=0, column=j + 1, padx=8)
            tk.Label(tbl, text=z, bg=CARD, fg=MUTED, font=F(9, True)).grid(row=j + 1, column=0, sticky='w', padx=4)
        for a, c_, n in zb.zone_matrix():
            tk.Label(tbl, text=f'{n} zone' + ('s' if n > 1 else ''), bg=ACCENT_SOFT if a == c_ else CARD, fg=INK,
                     font=F(9)).grid(row=zb.ZONE_ORDER.index(a) + 1, column=zb.ZONE_ORDER.index(c_) + 1, padx=6,
                                     pady=2, ipadx=4)
        tk.Label(info.body, text='Rates per passenger per zone: ' + ', '.join(
            f'{zb.CATEGORY_PLURAL[c]} {zb.fares.FARE_RATES_CENTS[c]:,}' for c in zb.CATEGORIES) + ' cents.',
                 bg=CARD, fg=MUTED, font=F(9), wraplength=px(380, self.root), justify='left').pack(anchor='w',
                                                                                                    pady=(8, 0))
        more = Card(side, pad=18)
        more.pack(fill='x', pady=(12, 0))
        tk.Label(more.body, text='Not sure which zone?', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w')
        tk.Label(more.body, text='The station board lists every station by zone.', bg=CARD, fg=MUTED,
                 font=F(9)).pack(anchor='w', pady=(0, 8))
        RoundButton(more.body, 'Open station board', lambda: self.show_page('board'), width=200, height=40,
                    radius=12, kind='soft', font=F(10, True)).pack(anchor='w')
        tk.Label(more.body, text='Optional: plan a trip between two stations (times, map, named tickets).',
                 bg=CARD, fg=MUTED, font=F(9), wraplength=px(380, self.root), justify='left'
                 ).pack(anchor='w', pady=(12, 8))
        RoundButton(more.body, 'Station-to-station planner', lambda: self.show_page('plan'), width=230, height=40,
                    radius=12, kind='ghost', font=F(10, True)).pack(anchor='w')
        self._zone_changed()

    def _zone_step(self, cat: str, d: int):
        var = self.zone_count_vars[cat]
        text = var.get().strip()
        n = int(text) if text.isascii() and text.isdigit() else 0
        var.set(str(max(0, min(zb.MAX_PER_CATEGORY, n + d))))

    def zone_counts_raw(self) -> Dict[str, str]:
        return {c: v.get() for c, v in self.zone_count_vars.items()}

    def _zone_changed(self):
        """Live summary under the form. The voucher itself is only made by Issue voucher."""
        if not hasattr(self, 'zone_preview'):
            return
        if hasattr(self, 'zone_err'):
            self.zone_err.config(text='')
        a, b = self.zone_from.value, self.zone_to.value
        try:
            zones = zb.chargeable_zones(a, b)
        except zb.ZoneBookingError:
            self.zone_preview.config(text='Choose a start zone and a destination zone.')
            return
        try:
            counts = zb.parse_counts(self.zone_counts_raw())
        except zb.ZoneBookingError:
            self.zone_preview.config(text=f'{a} to {b}: {zones} chargeable zone{"s" if zones > 1 else ""}. '
                                          'Now enter the travellers.')
            return
        total = sum(counts[c] * zb.fares.FARE_RATES_CENTS[c] * zones for c in zb.CATEGORIES)
        people = sum(counts.values())
        self.zone_preview.config(text=f'{a} to {b}  ·  {zones} chargeable zone{"s" if zones > 1 else ""}  ·  '
                                      f'{people} traveller{"s" if people > 1 else ""}  ·  {total:,} cents')

    def reset_zone_form(self):
        """Fresh form: no zones chosen, every count back to 0, no message."""
        for var in self.zone_count_vars.values():
            var.set('0')
        self.zone_from.clear()
        self.zone_to.clear()
        self.zone_err.config(text='')

    def issue_zone_voucher(self) -> Optional[zb.Voucher]:
        """Validate, then issue and show the voucher. Returns it, or None (with the reason on screen)."""
        if getattr(self, 'fare_problem', ''):
            self.zone_err.config(text='Bookings are paused: ' + self.fare_problem)
            return None
        try:
            v = zb.issue_voucher(self.zone_from.value, self.zone_to.value, self.zone_counts_raw(), self.clock())
        except zb.ZoneBookingError as exc:
            self.zone_err.config(text=str(exc))
            return None
        self.voucher = v
        self.voucher_note = ''
        try:                                             # a small log of issued vouchers: zones and counts only
            core.log_event(Path(self.folder) / 'zone_vouchers.jsonl', v.as_dict())
        except OSError as exc:
            self.voucher_note = f'Note: the voucher could not be added to the voucher log ({exc}).'
        self._paint_voucher()
        self.show_page('voucher')
        return v

    # ------------------------------------------------------------ issued voucher page
    def _build_voucher_page(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['voucher'] = page
        self.voucher_card = Card(page, pad=22)
        self.voucher_card.pack(pady=14)
        self.voucher_labels: Dict[str, tk.Label] = {}

    def _paint_voucher(self):
        v = self.voucher
        body = self.voucher_card.body
        for w in body.winfo_children():
            w.destroy()
        self.voucher_labels = {}
        if v is None:
            return
        lab = self.voucher_labels
        head = tk.Frame(body, bg=CARD)
        head.pack(fill='x')
        tk.Label(head, text='✓  Voucher issued', bg=CARD, fg=ACCENT_DARK, font=F(18, True)).pack(side='left')
        lab['number'] = tk.Label(head, text=v.number, bg=CARD, fg=INK, font=F(12, True))
        lab['number'].pack(side='right')

        top = tk.Frame(body, bg=CARD)
        top.pack(fill='x', pady=(12, 8))
        for i, (key, title, value) in enumerate((
                ('issued', 'ISSUED', v.issued_text),
                ('start', 'START ZONE', f'{v.start_zone} ({zb.ZONE_NUMBER[v.start_zone]})'),
                ('destination', 'DESTINATION ZONE', f'{v.destination_zone} ({zb.ZONE_NUMBER[v.destination_zone]})'),
                ('zones', 'CHARGEABLE ZONES', str(v.zones)))):
            top.grid_columnconfigure(i, weight=1, uniform='vt')
            cell = tk.Frame(top, bg='#F7F9FC', highlightthickness=1, highlightbackground=LINE, padx=10, pady=8)
            cell.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 8, 0))
            tk.Label(cell, text=title, bg='#F7F9FC', fg=MUTED, font=F(8, True)).pack(anchor='w')
            lab[key] = tk.Label(cell, text=value, bg='#F7F9FC', fg=INK, font=F(12, True))
            lab[key].pack(anchor='w')

        tbl = tk.Frame(body, bg=CARD)
        tbl.pack(fill='x', pady=(6, 0))
        heads = ('Passenger type', 'Count', 'Rate per zone', 'Chargeable zones', 'Subtotal')
        for j, h in enumerate(heads):
            tbl.grid_columnconfigure(j, weight=1 if j == 0 else 0, minsize=px(120 if j else 160, self.root))
            tk.Label(tbl, text=h.upper(), bg=CARD, fg=MUTED, font=F(8, True)).grid(
                row=0, column=j, sticky='w' if j == 0 else 'e', padx=6, pady=(0, 4))
        tk.Frame(tbl, bg=LINE, height=1).grid(row=1, column=0, columnspan=5, sticky='ew')
        for i, ln in enumerate(v.lines):
            r = 2 + i * 2
            colour = INK if ln.count else MUTED
            cells = (ln.label, str(ln.count), f'{ln.rate_cents:,} cents', str(ln.zones), f'{ln.subtotal_cents:,} cents')
            for j, text in enumerate(cells):
                w = tk.Label(tbl, text=text, bg=CARD, fg=colour, font=F(11, j in (0, 4) and ln.count > 0))
                w.grid(row=r, column=j, sticky='w' if j == 0 else 'e', padx=6, pady=5)
                lab[f'{ln.category}.{("label", "count", "rate", "zones", "subtotal")[j]}'] = w
            tk.Frame(tbl, bg=LINE, height=1).grid(row=r + 1, column=0, columnspan=5, sticky='ew')

        tot = tk.Frame(body, bg=ACCENT_SOFT, padx=14, pady=10)
        tot.pack(fill='x', pady=(12, 0))
        lab['travellers'] = tk.Label(tot, text=f'Total travellers: {v.travellers}', bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                     font=F(12, True))
        lab['travellers'].pack(side='left')
        lab['total'] = tk.Label(tot, text=f'Total fare: {v.total_cents:,} cents', bg=ACCENT_SOFT, fg=ACCENT_DARK,
                                font=F(18, True))
        lab['total'].pack(side='right')
        tk.Label(body, text='Each subtotal = count x rate per zone x chargeable zones.', bg=CARD, fg=MUTED,
                 font=F(9)).pack(anchor='w', pady=(6, 0))

        out = tk.Frame(body, bg=CARD)
        out.pack(fill='x', pady=(12, 0))
        for text, fn in (('Print voucher', self.print_voucher), ('Save PDF', self.save_voucher_pdf),
                         ('Open PDF', self.open_voucher_pdf)):
            RoundButton(out, text, fn, width=150, height=40, radius=12, kind='soft', font=F(10, True)
                        ).pack(side='left', padx=(0, 8))
        self.voucher_status = tk.Label(body, text=self.voucher_note, bg=CARD, fg=MUTED, font=F(9),
                                       wraplength=px(700, self.root), justify='left')
        self.voucher_status.pack(anchor='w', pady=(6, 0))

        tk.Frame(body, bg=LINE, height=1).pack(fill='x', pady=(12, 12))
        tk.Label(body, text='What would you like to do next?', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w')
        nxt = tk.Frame(body, bg=CARD)
        nxt.pack(fill='x', pady=(8, 0))
        self.issue_another_btn = RoundButton(nxt, 'Issue another voucher', self.issue_another, width=260, height=52,
                                             radius=14, font=F(13, True))
        self.issue_another_btn.pack(side='left')
        self.exit_btn = RoundButton(nxt, 'Exit', self.exit_app, width=140, height=52, radius=14, kind='danger',
                                    font=F(13, True))
        self.exit_btn.pack(side='left', padx=12)

    def voucher_pdf_path(self, v: zb.Voucher) -> Path:
        return Path(self.folder) / 'vouchers' / f'{v.number}.pdf'

    def _voucher_pdf(self, path: Optional[Path] = None) -> Optional[Path]:
        if self.voucher is None:
            return None
        import cta_voucher_pdf
        try:
            return cta_voucher_pdf.build_voucher_pdf(self.voucher, path or self.voucher_pdf_path(self.voucher))
        except ImportError:
            self.voucher_status.config(text='PDF export needs reportlab:  pip install -r requirements.txt', fg=DANGER)
        except OSError as exc:
            self.voucher_status.config(text=f'Could not save the PDF ({exc}).', fg=DANGER)
        return None

    def print_voucher(self):
        path = self._voucher_pdf()
        if path:
            ok, msg = core.print_pdf(path)
            self.voucher_status.config(text=msg, fg=ACCENT_DARK if ok else DANGER)

    def open_voucher_pdf(self):
        path = self._voucher_pdf()
        if path:
            ok, msg = core.open_pdf(path)
            self.voucher_status.config(text=msg, fg=ACCENT_DARK if ok else DANGER)

    def save_voucher_pdf(self):
        if self.voucher is None:
            return
        chosen = filedialog.asksaveasfilename(
            parent=self.root, title='Save your voucher', defaultextension='.pdf', filetypes=[('PDF file', '*.pdf')],
            initialdir=str(core.downloads_dir(Path(self.folder))), initialfile=f'{self.voucher.number}.pdf')
        if not chosen:
            return
        path = self._voucher_pdf(Path(chosen))
        if path:
            self.voucher_status.config(text=f'Saved: {path}', fg=ACCENT_DARK)

    def issue_another(self):
        """Back to a fresh zone booking: the previous zones and counts are cleared."""
        self.voucher = None
        self.reset_zone_form()
        self.show_page('zone')

    def exit_app(self):
        """Close the app normally (the same as closing the window)."""
        admin = getattr(self, 'admin', None)
        if admin is not None:
            admin.stop()
        self.root.destroy()

    # ------------------------------------------------------------ station board
    def _build_board(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['board'] = page
        card = Card(page, pad=18)
        card.pack(fill='both', expand=True, padx=20, pady=10)
        b = card.body
        n_st, n_links = zb.network_counts()
        tk.Label(b, text='Station board', bg=CARD, fg=INK, font=F(18, True)).pack(anchor='w')
        tk.Label(b, text=f'{n_st} stations in 3 zones, listed A–Z within each zone  ·  {n_links} track connections',
                 bg=CARD, fg=MUTED, font=F(10)).pack(anchor='w', pady=(0, 12))
        table = tk.Frame(b, bg=CARD)
        table.pack(fill='x')
        self.board_cells: Dict[str, list] = {}
        for j, (zone, names) in enumerate(zb.station_board()):
            table.grid_columnconfigure(j, weight=1, uniform='board')
            col = tk.Frame(table, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            col.grid(row=0, column=j, sticky='nsew', padx=(0 if j == 0 else 10, 0))
            head = tk.Frame(col, bg=ZONE_COLOR[zone])
            head.pack(fill='x')
            tk.Label(head, text=f'{zone}', bg=ZONE_COLOR[zone], fg='white', font=F(13, True)
                     ).pack(side='left', padx=10, pady=6)
            tk.Label(head, text=f'{ZONE_HINT[zone]}  ·  {len(names)} stations', bg=ZONE_COLOR[zone], fg='white',
                     font=F(9, True)).pack(side='right', padx=10)
            self.board_cells[zone] = []
            for i, name in enumerate(names):
                row = tk.Frame(col, bg=CARD if i % 2 == 0 else '#F7F9FC')
                row.pack(fill='x')
                tk.Label(row, text=f'{i + 1:>2}', bg=row['bg'], fg=MUTED, font=F(9), width=3).pack(side='left')
                w = tk.Label(row, text=name, bg=row['bg'], fg=INK, font=F(11))
                w.pack(side='left', pady=2)
                self.board_cells[zone].append(name)
        foot = tk.Frame(b, bg=CARD)
        foot.pack(fill='x', pady=(10, 0))
        RoundButton(foot, 'Buy a zone ticket', lambda: self.show_page('zone'), width=200, height=42, radius=12,
                    font=F(11, True)).pack(side='left')
        RoundButton(foot, 'Station-to-station planner', lambda: self.show_page('plan'), width=240, height=42,
                    radius=12, kind='ghost', font=F(10, True)).pack(side='left', padx=10)
        tk.Label(foot, text='Zone numbers: Central 1, Midtown 2, Downtown 3.', bg=CARD, fg=MUTED, font=F(9)
                 ).pack(side='right')
