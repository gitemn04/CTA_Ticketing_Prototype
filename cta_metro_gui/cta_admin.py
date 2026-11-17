"""Admin portal page (Tkinter). All the numbers come from cta_stats.py / cta_core.py, which are unit-tested;
this file only lays them out. The page re-reads the booking file every few seconds while it is open, so
bookings made on another window or computer sharing the same folder appear without pressing anything.
"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Dict, List, Optional

import cta_core as core
import cta_tickets
from cta_auth import PermissionDenied
from cta_storage import StorageError, atomic_write_bytes
import cta_stats as stats
from cta_widgets import (ACCENT, ACCENT_DARK, ACCENT_SOFT, BG, CARD, DANGER, INK, LINE, MUTED, WARN_INK, WARN_SOFT,
                         Card, F, RoundButton, Segmented, px)

REFRESH_MS = 5000
CHART_DAYS = 14
TICKET_COLUMNS = (('ticket', 'Ticket', 180), ('passenger', 'Passenger', 140), ('phone', 'Phone', 125),
                  ('trip', 'Trip', 150), ('departs', 'Departs', 115), ('payment', 'Payment', 330),
                  ('status', 'Status', 170))
STATUS_LABELS = {'open': 'Waiting', 'answered': 'Answered', 'approved': 'Approved', 'declined': 'Declined'}


class InboxTab(tk.Frame):
    """One list of passenger cases with the conversation below it: problem reports (reply) or refund requests (approve
    or decline). All decisions go through the database service, which checks permission and writes the audit entry."""

    def __init__(self, parent, page: 'AdminPage', kind: str):
        super().__init__(parent, bg=CARD)
        self.page, self.kind = page, kind
        self._cases: Dict[str, dict] = {}
        self._shown = None
        refunds = kind == 'refund_request'
        cols = ([('case', 'Request', 70), ('ref', 'Booking', 150), ('phone', 'Passenger phone', 130),
                 ('unused', 'Unused journeys', 110), ('refund', 'Refund if approved', 130),
                 ('payment', 'Payment', 300), ('status', 'Status', 90)] if refunds else
                [('case', 'Case', 60), ('phone', 'From', 140), ('topic', 'About', 170), ('status', 'Status', 90),
                 ('updated', 'Last activity', 150), ('text', 'First message', 380)])
        self.note = tk.Label(self, text='', bg=CARD, fg=MUTED, font=F(10), wraplength=px(1000, self), justify='left')
        self.note.pack(anchor='w', padx=10, pady=(8, 2))
        wrap = tk.Frame(self, bg=CARD)
        wrap.pack(fill='x', padx=10)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show='headings', height=4, selectmode='browse')
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width, self), anchor='w')
        self.tree.pack(side='left', fill='x', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda ev: self._show())
        self.thread = tk.Text(self, height=5, relief='flat', bg='#F7F9FC', font=F(10), highlightthickness=1,
                              highlightbackground=LINE, wrap='word', state='disabled')
        self.thread.pack(fill='x', padx=10, pady=(6, 4))
        row = tk.Frame(self, bg=CARD)
        row.pack(fill='x', padx=10, pady=(0, 8))
        self.reply_var = tk.StringVar()
        box = tk.Frame(row, bg=LINE, padx=1, pady=1)
        box.pack(side='left', fill='x', expand=True)
        self.entry = tk.Entry(box, textvariable=self.reply_var, relief='flat', font=F(11), bg='#F7F9FC', fg=INK,
                              highlightthickness=0, insertbackground=INK)
        self.entry.pack(fill='x', ipady=5)
        self.entry.bind('<Return>', lambda ev: self.reply_selected())
        if refunds:
            RoundButton(row, 'Decline', lambda: self.decide_selected(False), width=100, height=36, radius=10,
                        kind='ghost', font=F(10, True)).pack(side='right', padx=(8, 0))
            RoundButton(row, 'Approve refund', lambda: self.decide_selected(True), width=150, height=36, radius=10,
                        font=F(10, True)).pack(side='right', padx=(8, 0))
        RoundButton(row, 'Send reply', self.reply_selected, width=120, height=36, radius=10, kind='soft',
                    font=F(10, True)).pack(side='right', padx=(8, 0))

    # -- painting
    def fill(self, cases: List[dict], history: List[dict]) -> int:
        """Show the cases of this kind; returns how many are waiting for staff."""
        mine = [c for c in cases if c['kind'] == self.kind]
        keep = (self.tree.selection() or (None,))[0]
        for iid in (self.tree.get_children() or ()):
            self.tree.delete(iid)
        self._cases = {}
        for c in mine:
            iid = f"c{c['id']}"
            self._cases[iid] = c
            first = c['messages'][0]['body'].replace('\n', ' ')[:70] if c['messages'] else ''
            status = STATUS_LABELS.get(c['status'], c['status'])
            if self.kind == 'refund_request':
                rec = next((r for r in history if r.get('reference', '').upper() == (c['reference'] or '').upper()), None)
                n, cents = cta_tickets.unused_journeys(rec) if rec else (0, 0)
                pay = core.payment_of(rec) if rec else {}
                paytxt = (f"{pay.get('label', '?')} · "
                          f"{core.payments.status_text(pay)}") if rec else '(booking not found)'
                values = (f"#{c['id']}", c['reference'], c['account_phone'] or '', n, core.format_cents(cents), paytxt, status)
            else:
                values = (f"#{c['id']}", c['account_phone'] or '(not signed in)', c['topic'], status,
                          c['updated_at'][:16], first)
            self.tree.insert('', 'end', iid=iid, values=values)
        if keep in self._cases:
            self.tree.selection_set(keep)
        waiting = sum(1 for c in mine if c['status'] == 'open')
        what = 'refund requests' if self.kind == 'refund_request' else 'complaints and messages'
        self.note.config(text=(f'{waiting} of {len(mine)} {what} waiting for staff. Select one to read it.' if mine
                               else f'No {what} yet.'))
        self._show()
        return waiting

    def _selected(self) -> Optional[dict]:
        sel = self.tree.selection()
        return self._cases.get(sel[0]) if sel else None

    def _show(self):
        c = self._selected()
        marker = (c['id'], len(c['messages']), c['status']) if c else None
        if marker == self._shown:
            return
        self._shown = marker
        self.thread.config(state='normal')
        self.thread.delete('1.0', 'end')
        if c:
            for m in c['messages']:
                who = f"Staff ({m['author']})" if m['from_staff'] else 'Passenger'
                self.thread.insert('end', f"{who}  {m['at'][:16]}\n{m['body']}\n\n")
        self.thread.config(state='disabled')

    # -- actions (also callable without dialogs, for tests)
    def reply_selected(self, text: Optional[str] = None):
        c = self._selected()
        if not c:
            self.page._say(False, 'Select a message in the list first.')
            return
        body = self.reply_var.get() if text is None else text
        app = self.page.app
        try:
            ok, msg = app.store.reply_case(c['id'], app.staff, body, app.clock())
        except ValueError as exc:
            ok, msg = False, str(exc)
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        if ok:
            self.reply_var.set('')
        self.page._say(ok, msg)
        self.page.refresh()

    def decide_selected(self, approve: bool, note: Optional[str] = None, confirm: bool = True):
        c = self._selected()
        if not c:
            self.page._say(False, 'Select a refund request in the list first.')
            return
        if c['status'] != 'open':
            self.page._say(False, 'This request has already been dealt with.')
            return
        app = self.page.app
        if approve and confirm and not messagebox.askyesno(
                'Approve refund', f"Cancel every unused journey of booking {c['reference']}? A cancelled ticket can "
                                  'never be used. You will then record the payout from Ticket actions.', parent=app.root):
            return
        if note is None:
            note = self.reply_var.get()
        try:
            ok, msg = app.store.decide_refund_request(c['id'], app.staff, approve, note, app.clock())
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        if ok:
            self.reply_var.set('')
        self.page._finish_action(ok, msg)


class AdminPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=BG)
        self.app = app
        self.period = 'This year'
        self.kpi: Dict[str, tk.Label] = {}
        self._rows: Dict[str, dict] = {}
        self._noshow_rows: Dict[str, dict] = {}
        self.search_var = tk.StringVar()
        self.checkin_var = tk.StringVar()
        self._after_id = None
        self.bind('<Destroy>', self._on_destroy)        # no timer may outlive the page (it would print a Tk error)
        self._build()

    # ------------------------------------------------------------ layout
    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill='x', padx=20, pady=(12, 6))
        tk.Label(top, text='Admin portal', bg=BG, fg=INK, font=F(18, True)).pack(side='left')
        self.who = tk.Label(top, text='', bg=BG, fg=MUTED, font=F(10))
        self.who.pack(side='left', padx=14)
        RoundButton(top, 'Export CSV', self.export_csv, width=120, height=38, kind='soft', radius=12,
                    font=F(10, True), parent_bg=BG).pack(side='right')
        RoundButton(top, 'Refresh', self.refresh, width=100, height=38, kind='ghost', radius=12,
                    font=F(10, True), parent_bg=BG).pack(side='right', padx=(0, 8))
        self.updated = tk.Label(top, text='', bg=BG, fg=MUTED, font=F(9))
        self.updated.pack(side='right', padx=12)
        self.seg = Segmented(top, stats.PERIODS, self._period_changed, value=self.period, parent_bg=BG)
        self.seg.pack(side='right', padx=12)

        # check-in strip
        strip = Card(self, pad=10)
        strip.pack(fill='x', padx=20, pady=(0, 8))
        self.strip = strip
        row = tk.Frame(strip.body, bg=CARD)
        row.pack(fill='x')
        tk.Label(row, text='Check in a ticket', bg=CARD, fg=INK, font=F(11, True)).pack(side='left', padx=(0, 10))
        box = tk.Frame(row, bg=LINE, padx=1, pady=1)
        box.pack(side='left')
        e = tk.Entry(box, textvariable=self.checkin_var, width=26, relief='flat', font=F(12, True), bg='#F7F9FC',
                     fg=INK, highlightthickness=0, insertbackground=INK)
        e.pack(ipady=5)
        e.bind('<Return>', lambda ev: self.check_in_typed())
        RoundButton(row, 'Check in', self.check_in_typed, width=110, height=38, radius=12, font=F(10, True)
                    ).pack(side='left', padx=8)
        self.result = tk.Label(row, text='Scan the QR code or type the ticket number printed under it.', bg=CARD,
                               fg=MUTED, font=F(10, True), wraplength=px(560, self), justify='left')
        self.result.pack(side='left', padx=8)

        # waiting messages banner: complaints and refund requests are one click away
        self.inbox_banner = tk.Frame(self, bg=WARN_SOFT)
        self.inbox_msg = tk.Label(self.inbox_banner, text='', bg=WARN_SOFT, fg=WARN_INK, font=F(11, True))
        self.inbox_msg.pack(side='left', padx=14, pady=8)
        RoundButton(self.inbox_banner, 'Open complaints', lambda: self._open_inbox(self.messages_tab), width=150,
                    height=32, radius=10, kind='soft', font=F(9, True), parent_bg=WARN_SOFT).pack(side='right', padx=(0, 8), pady=6)
        RoundButton(self.inbox_banner, 'Open refund requests', lambda: self._open_inbox(self.refunds_tab), width=170,
                    height=32, radius=10, kind='soft', font=F(9, True), parent_bg=WARN_SOFT).pack(side='right', padx=(0, 8), pady=6)
        self._banner_shown = False

        # KPI row
        kp = tk.Frame(self, bg=BG)
        kp.pack(fill='x', padx=20)
        for i, (key, title) in enumerate((('bookings', 'Bookings'), ('passengers', 'Passengers'),
                                          ('tickets', 'Tickets (journeys)'), ('revenue', 'Booked value'),
                                          ('collected', 'Paid'), ('to_collect', 'To collect at counter'),
                                          ('boarded', 'Checked in'))):
            kp.grid_columnconfigure(i, weight=1, uniform='kpi')
            c = Card(kp, pad=10)
            c.grid(row=0, column=i, sticky='ew', padx=(0 if i == 0 else 8, 0))
            tk.Label(c.body, text=title.upper(), bg=CARD, fg=MUTED, font=F(8, True)).pack(anchor='w')
            lab = tk.Label(c.body, text='0', bg=CARD, fg=INK, font=F(17, True))
            lab.pack(anchor='w')
            self.kpi[key] = lab

        # bottom tabs
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=20, pady=(4, 12))
        self.nb = nb
        t1, t2 = tk.Frame(nb, bg=CARD), tk.Frame(nb, bg=CARD)
        nb.add(t1, text='  All tickets  ')
        nb.add(t2, text='  Booked but not travelled - contact  ')
        self.messages_tab = InboxTab(nb, self, 'problem')
        self.refunds_tab = InboxTab(nb, self, 'refund_request')
        nb.add(self.messages_tab, text='  Complaints and messages  ')
        nb.add(self.refunds_tab, text='  Refund requests  ')
        charts_tab = tk.Frame(nb, bg=BG)               # charts get their own tab so the ticket list has room
        nb.add(charts_tab, text='  Charts and zones  ')

        # charts tab
        mid = tk.Frame(charts_tab, bg=BG)
        mid.pack(fill='both', expand=True, padx=8, pady=8)
        mid.grid_columnconfigure(0, weight=3, uniform='m')
        mid.grid_columnconfigure(1, weight=3, uniform='m')
        mid.grid_columnconfigure(2, weight=2, uniform='m')
        self.zone_card = Card(mid, pad=12)
        self.zone_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.chart_card = Card(mid, pad=12)
        self.chart_card.grid(row=0, column=1, sticky='nsew', padx=(0, 8))
        self.side_card = Card(mid, pad=12)
        self.side_card.grid(row=0, column=2, sticky='nsew')


        bar = tk.Frame(t1, bg=CARD)
        bar.pack(fill='x', padx=10, pady=(8, 4))
        tk.Label(bar, text='Search', bg=CARD, fg=MUTED, font=F(9, True)).pack(side='left')
        sbox = tk.Frame(bar, bg=LINE, padx=1, pady=1)
        sbox.pack(side='left', padx=8)
        se = tk.Entry(sbox, textvariable=self.search_var, width=28, relief='flat', font=F(11), bg='#F7F9FC', fg=INK,
                      highlightthickness=0, insertbackground=INK)
        se.pack(ipady=4)
        se.bind('<KeyRelease>', lambda ev: self._fill_tickets())
        fbar = tk.Frame(t1, bg=CARD)                          # filters on their own row so no button is squeezed
        fbar.pack(fill='x', padx=10, pady=(0, 4))
        tk.Label(fbar, text='Filter  -  status', bg=CARD, fg=MUTED, font=F(9, True)).pack(side='left', padx=(0, 4))
        self.status_box = ttk.Combobox(fbar, state='readonly', width=24, values=[lab for _k, lab in stats.STATUS_FILTERS])
        self.status_box.current(0)
        self.status_box.pack(side='left')
        tk.Label(fbar, text='paid with', bg=CARD, fg=MUTED, font=F(9, True)).pack(side='left', padx=(12, 4))
        self.method_box = ttk.Combobox(fbar, state='readonly', width=24, values=[lab for _k, lab in stats.METHOD_FILTERS])
        self.method_box.current(0)
        self.method_box.pack(side='left', padx=(0, 6))
        for box_ in (self.status_box, self.method_box):
            box_.bind('<<ComboboxSelected>>', lambda ev: self._fill_tickets())
        RoundButton(fbar, 'Clear', self.clear_filters, width=70, height=30, radius=8, kind='ghost',
                    font=F(9, True)).pack(side='left')
        self.count_lbl = tk.Label(fbar, text='', bg=CARD, fg=MUTED, font=F(9))
        self.count_lbl.pack(side='left', padx=10)
        RoundButton(bar, 'Mark paid (counter)', lambda: self.mark_selected_paid(popup=True), width=170, height=36, radius=10,
                    kind='soft', font=F(10, True)).pack(side='right')
        RoundButton(bar, 'Check in selected', self.check_in_selected, width=150, height=36, radius=10,
                    kind='soft', font=F(10, True)).pack(side='right', padx=8)
        abar = tk.Frame(t1, bg=CARD)                          # one visible button per action; enabled once a ticket is selected
        abar.pack(fill='x', padx=10, pady=(0, 4))
        tk.Label(abar, text='Selected ticket', bg=CARD, fg=MUTED, font=F(9, True)).pack(side='left', padx=(0, 8))
        self.action_btns = []
        for text, fn, w in (('Cancel ticket', self.cancel_selected, 120),
                            ('Cancel whole booking', lambda: self.cancel_selected(whole_booking=True), 170),
                            ('Record refund paid', self.refund_selected, 150),
                            ('Allow re-entry', self.reentry_selected, 125),
                            ('Correct name', self.correct_name_selected, 120)):
            b = RoundButton(abar, text, fn, width=w, height=32, radius=9, kind='ghost', font=F(9, True))
            b.pack(side='left', padx=(0, 6))
            self.action_btns.append(b)
        self.action_msg = tk.Label(t1, text='Select a ticket in the list to cancel, refund, re-admit or correct it.',
                                   bg=CARD, fg=MUTED, font=F(10, True), anchor='w', justify='left',
                                   wraplength=px(1000, self))
        self.action_msg.pack(fill='x', padx=12, pady=(0, 4))
        self.tree = self._make_tree(t1, [(c[0], c[1], c[2]) for c in TICKET_COLUMNS])
        self.tree.bind('<Double-1>', lambda ev: self.check_in_selected())
        self.tree.bind('<<TreeviewSelect>>', lambda ev: self._sync_actions())
        self._sync_actions()

        self.noshow_note = tk.Label(t2, text='', bg=CARD, fg=MUTED, font=F(10), wraplength=px(1000, self),
                                    justify='left')
        self.noshow_note.pack(anchor='w', padx=10, pady=(8, 4))
        self.noshow_tree = self._make_tree(t2, [('ticket', 'Ticket', 190), ('passenger', 'Passenger', 170),
                                                ('phone', 'Phone to call', 150), ('trip', 'Trip', 170),
                                                ('departs', 'Departed', 120), ('payment', 'Payment', 150)])

    def _make_tree(self, parent, cols):
        wrap = tk.Frame(parent, bg=CARD)
        wrap.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show='headings', height=12, selectmode='browse')
        for key, title, width in cols:
            tree.heading(key, text=title)
            tree.column(key, width=px(width, self), anchor='w')
        sb = tk.Scrollbar(wrap, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        return tree

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.refresh()
        self.stop()
        self._after_id = self.after(REFRESH_MS, self._tick)

    def _on_destroy(self, event):
        if event.widget is self:
            self.stop()

    def stop(self):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:                          # the timer already ran or the page is closing
                pass
            self._after_id = None

    def _tick(self):
        self._after_id = None
        if self.app.current_page == 'admin' and self.app.staff:
            self.refresh()
            self._after_id = self.after(REFRESH_MS, self._tick)

    def _period_changed(self, value: str):
        self.period = value
        self.refresh()

    # ------------------------------------------------------------ painting
    @staticmethod
    def _clear(tree):
        for iid in (tree.get_children() or ()):
            tree.delete(iid)

    def refresh(self):
        app = self.app
        app.reload_history()
        now = app.clock()
        recs = stats.filter_period(app.history, self.period, now)
        s = stats.summarize(recs, app.environment)
        self.who.config(text=f"Signed in as {app.staff['username']}" if app.staff else '')
        self.updated.config(text=f'Updated {now:%H:%M:%S}  ·  live')
        k = self.kpi
        k['bookings'].config(text=f"{s['bookings']:,}")
        k['passengers'].config(text=f"{s['passengers']:,}")
        k['tickets'].config(text=f"{s['tickets']:,}")
        k['revenue'].config(text=core.format_cents(s['revenue_cents']))
        k['collected'].config(text=core.format_cents(s['collected_cents']))
        k['to_collect'].config(text=core.format_cents(s['to_collect_cents']),
                               fg=DANGER if s['to_collect_cents'] else INK)
        k['boarded'].config(text=f"{s['boarded']:,} / {s['tickets']:,}")
        self._paint_zones(recs)
        self._paint_chart(app.history, now)
        self._paint_side(recs, s)
        self._fill_tickets(recs)
        self._fill_noshows(recs, now)
        self._fill_inboxes()

    def _fill_inboxes(self):
        app = self.app
        counts = {}
        try:
            cases = app.store.list_cases(app.staff, app.clock())
        except PermissionDenied:
            cases = []                                        # this role may not answer passengers: tabs stay empty
        except StorageError as exc:
            self._say(False, f'Passenger messages could not be read. {exc}')
            return
        for tab, label in ((self.messages_tab, 'Complaints and messages'), (self.refunds_tab, 'Refund requests')):
            waiting = tab.fill(cases, app.history)
            counts[tab] = waiting
            self.nb.tab(tab, text=f'  {label}' + (f' ({waiting})' if waiting else '') + '  ')
        self._paint_banner(counts.get(self.messages_tab, 0), counts.get(self.refunds_tab, 0))

    def _paint_banner(self, complaints: int, refunds: int):
        if not (complaints or refunds):
            if self._banner_shown:
                self.inbox_banner.pack_forget()
                self._banner_shown = False
            return
        bits = []
        if complaints:
            bits.append(f"{complaints} passenger complaint{'s' if complaints != 1 else ''} / message{'s' if complaints != 1 else ''} waiting")
        if refunds:
            bits.append(f"{refunds} refund request{'s' if refunds != 1 else ''} waiting")
        self.inbox_msg.config(text='  \u2022  '.join(bits))
        if not self._banner_shown:
            self.inbox_banner.pack(fill='x', padx=20, pady=(0, 8), after=self.strip)
            self._banner_shown = True

    def clear_filters(self):
        self.search_var.set('')
        self.status_box.current(0)
        self.method_box.current(0)
        self._fill_tickets()

    def _paint_zones(self, recs: List[dict]):
        b = self.zone_card.body
        for w in b.winfo_children():
            w.destroy()
        tk.Label(b, text='Zone to zone (tickets)', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w')
        tk.Label(b, text='Rows: where the journey starts. Columns: where it ends.', bg=CARD, fg=MUTED, font=F(9)
                 ).pack(anchor='w', pady=(0, 6))
        matrix = stats.zone_matrix(recs)
        grid = tk.Frame(b, bg=CARD)
        grid.pack(anchor='w')
        zones = stats.ZONE_ORDER
        tk.Label(grid, text='From \\ To', bg=CARD, fg=MUTED, font=F(9, True), width=10).grid(row=0, column=0)
        for j, z in enumerate(zones, start=1):
            tk.Label(grid, text=z, bg=CARD, fg=MUTED, font=F(9, True), width=10).grid(row=0, column=j)
        for i, a in enumerate(zones, start=1):
            tk.Label(grid, text=a, bg=CARD, fg=MUTED, font=F(9, True), width=10, anchor='e').grid(row=i, column=0)
            for j, z in enumerate(zones, start=1):
                n = matrix.get((a, z), 0)
                tk.Label(grid, text=str(n), bg=ACCENT_SOFT if n else '#F2F5F9', fg=ACCENT_DARK if n else MUTED,
                         font=F(12, True), width=8, pady=4).grid(row=i, column=j, padx=2, pady=2)
        cross = stats.zone_crossings(recs)
        tk.Label(b, text='Moving into the next zone', bg=CARD, fg=INK, font=F(10, True)).pack(anchor='w', pady=(8, 2))
        if not cross:
            tk.Label(b, text='No zone changes in this period.', bg=CARD, fg=MUTED, font=F(10)).pack(anchor='w')
        for (a, z), n in sorted(cross.items(), key=lambda kv: -kv[1]):
            tk.Label(b, text=f'{a}  →  {z}:   {n:,}', bg=CARD, fg=INK, font=F(10)).pack(anchor='w')

    def _paint_chart(self, history: List[dict], now: datetime):
        b = self.chart_card.body
        for w in b.winfo_children():
            w.destroy()
        tk.Label(b, text=f'Passengers booked per day (last {CHART_DAYS} days)', bg=CARD, fg=INK,
                 font=F(12, True)).pack(anchor='w')
        data = stats.daily_counts(history, CHART_DAYS, now)
        w, h = px(400, self), px(150, self)
        cv = tk.Canvas(b, width=w, height=h, bg=CARD, highlightthickness=0)
        cv.pack(anchor='w', pady=(8, 0))
        peak = max([n for _, n in data] + [1])
        bw = w / len(data)
        for i, (d, n) in enumerate(data):
            x0 = i * bw + bw * 0.15
            x1 = (i + 1) * bw - bw * 0.15
            top = h - 22 - (h - 44) * n / peak
            cv.create_rectangle(x0, top, x1, h - 22, fill=ACCENT if n else LINE, outline='')
            if n:
                cv.create_text((x0 + x1) / 2, top - 8, text=str(n), fill=INK, font=F(8, True))
            cv.create_text((x0 + x1) / 2, h - 10, text=f'{d:%d}', fill=MUTED, font=F(8))

    def _paint_side(self, recs: List[dict], s: dict):
        b = self.side_card.body
        for w in b.winfo_children():
            w.destroy()
        tk.Label(b, text='Busiest routes', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w')
        routes = stats.top_routes(recs)
        if not routes:
            tk.Label(b, text='No bookings yet.', bg=CARD, fg=MUTED, font=F(10)).pack(anchor='w')
        for a, z, n in routes:
            tk.Label(b, text=f'{a} → {z}:  {n:,}', bg=CARD, fg=INK, font=F(10)).pack(anchor='w')
        tk.Label(b, text='How people paid', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w', pady=(10, 0))
        for label, n in sorted(s['by_method'].items(), key=lambda kv: -kv[1]):
            cents = s['by_method_cents'].get(label, 0)
            tk.Label(b, text=f'{label}:  {n:,}  ({core.format_cents(cents)})', bg=CARD, fg=INK, font=F(9),
                     wraplength=px(260, self), justify='left').pack(anchor='w')
        tk.Label(b, text=f"Return bookings: {s['return_bookings']:,}", bg=CARD, fg=MUTED, font=F(9)
                 ).pack(anchor='w', pady=(8, 0))
        if s['cancelled_tickets']:
            tk.Label(b, text=f"Cancelled journeys: {s['cancelled_tickets']:,}", bg=CARD, fg=MUTED, font=F(9)
                     ).pack(anchor='w')
        if s['refund_due_cents']:
            tk.Label(b, text=f"Refunds to pay out: {core.format_cents(s['refund_due_cents'])}", bg=CARD, fg=DANGER,
                     font=F(9, True)).pack(anchor='w', pady=(2, 0))

    def _fill_tickets(self, recs: Optional[List[dict]] = None):
        if recs is None:
            recs = stats.filter_period(self.app.history, self.period, self.app.clock())
        rows = stats.ticket_rows(recs)
        status = stats.STATUS_FILTERS[max(0, self.status_box.current())][0]
        method = stats.METHOD_FILTERS[max(0, self.method_box.current())][0]
        shown = stats.filter_rows(rows, self.search_var.get(), status, method)
        keep = self.tree.selection()                          # the list is rebuilt every few seconds: keep the pick
        top = self.tree.yview()[0]
        self._clear(self.tree)
        self._rows = {}
        for r in shown:
            self._rows[r['ticket_no']] = r
            self.tree.insert('', 'end', iid=r['ticket_no'], values=(
                r['ticket_no'], r['passenger'], r['phone'], f"{r['from']} → {r['to']}", r['departs'],
                f"{r['payment_method']}" + (f" ({r['payment_detail']})" if r.get('payment_detail') else '') +
                f" · {core.payments.status_text({'status': r['payment_status'], 'gateway': r['gateway']})}",
                self._status_text(r)))
        self.count_lbl.config(text=f'Showing {len(shown)} of {len(rows)} tickets')
        still = [t for t in keep if self.tree.exists(t)]
        if still:
            self.tree.selection_set(still)
            self.tree.focus(still[0])
        self.tree.yview_moveto(top)
        self._sync_actions()

    @staticmethod
    def _status_text(row: dict) -> str:
        st = row['status']
        if st == 'boarded':
            return 'checked in'
        if st == 'cancelled':
            return 'cancelled - refund due' if row.get('refund_state') == 'due' else 'cancelled'
        if st == 'refunded':
            return 'refunded'
        return 'not yet'

    def _fill_noshows(self, recs: List[dict], now: datetime):
        self._clear(self.noshow_tree)
        rows = stats.no_shows(recs, now)
        self._noshow_rows = {}
        self.noshow_note.config(text=(
            f'{len(rows)} ticket(s) whose departure time has passed with nobody checked in. Call the phone number to '
            'find out whether the passenger still wants to travel, and offer a new booking or a return ticket.'
            if rows else 'Nobody is waiting: every ticket whose departure has passed was checked in.'))
        for r in rows:
            self._noshow_rows[r['ticket_no']] = r
            self.noshow_tree.insert('', 'end', iid=r['ticket_no'], values=(
                r['ticket_no'], r['passenger'], r['phone'], f"{r['from']} → {r['to']}", r['departs'],
                core.payments.status_text({'status': r['payment_status'], 'gateway': r['gateway']})))

    # ------------------------------------------------------------ actions
    def _say(self, ok: bool, text: str):
        self.result.config(text=text, fg=ACCENT_DARK if ok else DANGER)
        self.action_msg.config(text=text, fg=ACCENT_DARK if ok else DANGER)    # also right above the list

    def _sync_actions(self):
        on = bool(self.tree.selection())
        for b in self.action_btns:
            b.set_enabled(on)

    def _open_inbox(self, tab):
        self.nb.select(tab)

    def _selected_ticket(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def check_in_typed(self):
        self._check_in(self.checkin_var.get())
        self.checkin_var.set('')

    def check_in_selected(self):
        tno = self._selected_ticket()
        if not tno:
            self._say(False, 'Select a ticket in the list first.')
            return
        self._check_in(tno)

    def _check_in(self, ticket_no: str):
        """Typed number, scanned QR token, URL-format QR code or the selected row — all go through the
        one atomic database check-in.

        URL-format QR codes (produced when a ticket server URL is configured):
          The scanner may present a URL such as https://tickets.example.com/ticket/<token>.
          These are resolved locally against the database — no network request is made.
          The token is validated (revocation, expiry, format) and converted to a ticket_id,
          which is then passed into the normal check-in path with all authentication,
          permission, payment and ticket-state checks applied.

          SECURITY: the URL is never fetched.  Only the token portion is read.
          SECURITY: viewing the URL (passenger scanning their own phone) does NOT check them in;
                    check-in requires a signed-in staff member with the ticket.check_in permission.
        """
        app = self.app
        if not app.can_save():
            self._say(False, app.storage_problem or 'Data storage is unavailable, so nobody can be checked in.')
            return

        # --- URL-format QR resolution (Bug 3 fix) ---
        resolved = ticket_no.strip()
        base_url = app._ticket_server_url()
        if base_url and resolved.startswith(base_url.rstrip('/') + '/ticket/'):
            # Scanned text looks like a ticket URL for this server.
            # Resolve locally without fetching the URL.
            try:
                from cta_ticket_server import resolve_url_token
                ticket_id, err = resolve_url_token(app.store.path, resolved, base_url)
            except Exception as exc:  # noqa: BLE001
                self._say(False, f'Could not read the QR code. {exc}')
                return
            if err:
                self._say(False, err)
                return
            # Convert ticket_id → ticket_no for the normal check-in path.
            # find_ticket_by_id searches the in-memory history; the live path below
            # re-reads from the database inside a transaction, so this is only used
            # to translate the identifier — all state checks happen atomically below.
            found = core.find_ticket_by_id(app.history, ticket_id or '')
            if found:
                _rec, p, leg = found
                pre = 'return_' if leg == 'return' else ''
                resolved = p.get(pre + 'ticket_no') or ticket_id or resolved
            else:
                # Ticket exists in the token table but not in loaded history; pass
                # the ticket_id directly — store.check_in can resolve by ticket_id
                # via parse_presented if it matches the manual-number pattern, but
                # more robustly, look up the ticket_no from the database directly.
                try:
                    import sqlite3
                    row = sqlite3.connect(str(app.store.path)).execute(
                        'SELECT ticket_no FROM tickets WHERE ticket_id = ?', (ticket_id,)
                    ).fetchone()
                    if row:
                        resolved = row[0]
                except Exception:  # noqa: BLE001
                    pass  # fall through with whatever we have
        # --- end URL resolution ---

        try:
            res = app.store.check_in(resolved, signer=app.signer, now=app.clock(), operator=app.staff,
                                     device=app.device_id, environment=app.environment)
        except (StorageError, PermissionDenied) as exc:
            self._say(False, f'The check-in was NOT recorded. {exc}')
            return
        self._say(res.ok, res.message)
        self.refresh()

    def mark_selected_paid(self, popup: bool = False):
        tno = self._selected_ticket()
        if not tno:
            self._say(False, 'Select a ticket of the booking that was paid.')
            return
        app = self.app
        if not app.can_save():
            self._say(False, app.storage_problem or 'Data storage is unavailable.')
            return
        try:
            ok, msg = app.store.mark_paid(tno, app.staff, app.clock())     # authorised and audited in one transaction
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, f'The payment was NOT recorded. {exc}'
        self._finish_action(ok, msg, popup=popup)

    # ------------------------------------------------------------ cancel / refund / re-entry / correction
    def _ready_for_action(self) -> Optional[str]:
        tno = self._selected_ticket()
        if not tno:
            self._say(False, 'Select a ticket in the list first.')
            return None
        if not self.app.can_save():
            self._say(False, self.app.storage_problem or 'Data storage is unavailable.')
            return None
        return tno

    def _ask(self, title: str, prompt: str) -> Optional[str]:
        return simpledialog.askstring(title, prompt, parent=self.app.root)

    def _finish_action(self, ok: bool, msg: str, popup: bool = False):
        self._say(ok, msg)
        if popup:                                             # buttons pressed by a person: say clearly what happened
            (messagebox.showinfo if ok else messagebox.showwarning)('Done' if ok else 'Not done', msg,
                                                                    parent=self.app.root)
        self.app.reload_history(force=True)
        self.refresh()

    def _staff(self):
        return self.app.staff                                    # the signed-in Principal; checked by every service call

    def cancel_selected(self, reason: Optional[str] = None, whole_booking: bool = False):
        tno = self._ready_for_action()
        if not tno:
            return
        interactive = reason is None
        if reason is None:                                       # interactive: confirm, then ask why
            what = 'every unused journey of this booking' if whole_booking else f'ticket {tno}'
            if not messagebox.askyesno('Cancel', f'Cancel {what}? A cancelled ticket can never be used.',
                                       parent=self.app.root):
                return
            reason = self._ask('Reason', 'Short reason (do not enter passenger details):') or ''
        app = self.app
        try:
            if whole_booking:
                found = core.find_ticket(app.history, tno)
                ref = found[0]['reference'] if found else ''
                ok, msg = app.store.cancel_booking(ref, self._staff(), app.clock(), reason)
            else:
                ok, msg = app.store.cancel_ticket(tno, self._staff(), app.clock(), reason)
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        self._finish_action(ok, msg, popup=interactive)

    def refund_selected(self, ref: Optional[str] = None):
        tno = self._ready_for_action()
        if not tno:
            return
        interactive = ref is None
        if ref is None:
            ref = self._ask('Refund', 'Refund reference from the payment provider (leave empty for a cash refund):') or ''
        try:
            ok, msg = self.app.store.record_refund(tno, self._staff(), self.app.clock(), ref)
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        self._finish_action(ok, msg, popup=interactive)

    def reentry_selected(self, reason: Optional[str] = None):
        tno = self._ready_for_action()
        if not tno:
            return
        interactive = reason is None
        if reason is None:
            reason = self._ask('Allow re-entry', 'Why is this ticket being allowed through again?')
            if reason is None:
                return
        try:
            ok, msg = self.app.store.allow_reentry(tno, self._staff(), self.app.clock(), reason)
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        self._finish_action(ok, msg, popup=interactive)

    def correct_name_selected(self, new_name: Optional[str] = None):
        tno = self._ready_for_action()
        if not tno:
            return
        found = core.find_ticket(self.app.history, tno)
        if not found:
            self._say(False, 'That booking no longer exists.')
            return
        rec, p, _leg = found
        interactive = new_name is None
        if new_name is None:
            new_name = self._ask('Correct name', f"Correct spelling of the name on ticket {p['ticket_no']}:")
            if new_name is None:
                return
        try:
            ok, msg = self.app.store.correct_name(rec['reference'], p['no'], new_name, self._staff(), self.app.clock())
        except (StorageError, PermissionDenied) as exc:
            ok, msg = False, str(exc)
        self._finish_action(ok, msg, popup=interactive)

    def export_csv(self):
        now = self.app.clock()
        recs = stats.filter_period(self.app.history, self.period, now)
        text = stats.rows_to_csv(stats.ticket_rows(recs))
        path = filedialog.asksaveasfilename(
            parent=self.app.root, title='Export tickets', defaultextension='.csv', filetypes=[('CSV file', '*.csv')],
            initialdir=str(core.downloads_dir(self.app.folder)),
            initialfile=f"cta_tickets_{self.period.lower().replace(' ', '_')}_{now:%Y%m%d}.csv")
        if not path:
            return
        self._export_to(Path(path), text, now)

    def _export_to(self, path: Path, text: str, now):
        """The export contains phone numbers: it needs 'report.export', and every export is audited (row count only)."""
        app = self.app
        try:
            who = app.store.authorize(app.staff, 'report.export', now, target=path.name)
            atomic_write_bytes(path, text.encode('utf-8-sig'))        # utf-8-sig so Excel shows names correctly
            app.store.audit('report.exported', now.strftime('%Y-%m-%d %H:%M:%S'), actor=who, target=path.name,
                            detail={'rows': max(0, len(text.splitlines()) - 1), 'period': self.period})
        except PermissionDenied as exc:
            self._say(False, str(exc))
            return
        except OSError as exc:
            messagebox.showerror('Export', f'Could not save the file: {exc}', parent=self.app.root)
            return
        self._say(True, f'Exported {len(text.splitlines()) - 1} tickets to {path}')
        return path
