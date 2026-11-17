"""CTA Metro - modern journey planner and ticket booking app.

Run:  python cta_app.py
"""
from __future__ import annotations

import os
import socket
import sys
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Callable, Dict, List, Optional

import cta_accounts as acct
import cta_core as core
import cta_auth
import cta_errors
import cta_db
import cta_fares as fares
import cta_payments as payments
import cta_routing as rt
import cta_tickets
from cta_storage import ConcurrentUpdate, StorageError
from cta_routing import (ZONES, ZONE_NAMES, HEADWAY_MIN, DWELL_MIN, TRAIN_SPEED_KMH,
                         Route, RoutingHealth, load_routing, next_departures, round_minute,
                         stop_times)
from cta_admin import AdminPage
from cta_dialogs import AuthDialog, PassengerDialog, PaymentDialog, center_window, make_modal
from cta_sms import SmsUnavailable, load_sms_provider
from cta_zone_ui import ZoneBookingMixin
from cta_widgets import (ACCENT, ACCENT_DARK, ACCENT_SOFT, BG, CARD, DANGER, INK, LINE,
                         MUTED, NAVY, WARN_INK, WARN_SOFT, ZONE_COLOR, Card, F, NetworkMap,
                         RoundButton, ScrollFrame, Segmented, StationPicker, Stepper, Timeline, Tooltip, px)

ISSUE_TYPES = cta_db.SUPPORT_TOPICS                 # one list, shared with the database check (they cannot drift apart)


class _BlockedStorage:
    """Stands in for the database while it is unavailable: every save is refused with the reason."""

    def __init__(self, reason: str):
        self.reason = reason

    def load_accounts(self):
        return []

    def load_staff(self):
        return []

    def save_accounts(self, accounts):
        raise StorageError(self.reason)

    def save_staff(self, staff):
        raise StorageError(self.reason)


# 'zone' (zone-only booking, ATHE Unit 2 Appendix 2) is the home page; the station-to-station planner stays optional.
PAGES = (('zone', 'Zone ticket'), ('board', 'Station board'), ('plan', 'Plan trip'), ('map', 'Network map'),
         ('tickets', 'My ticket'), ('help', 'Help'))
NAV_WIDTH = {'board': 118, 'map': 112, 'zone': 104, 'plan': 90, 'tickets': 94, 'help': 70}   # six tabs fit 1366 px


class CTAApp(ZoneBookingMixin):
    def __init__(self, root: tk.Tk, provider=None, clock: Callable[[], datetime] = datetime.now,
                 folder: Optional[Path] = None, sms=None, pay_provider=None, environment: Optional[str] = None):
        self.root, self.clock = root, clock
        self.problems: List[str] = []               # shown at start-up and as a red badge in the header
        routing_env = environment if environment in payments.ENVIRONMENTS else payments.current_environment()
        if provider is not None:                     # supplied by the caller (tests, embedding): take it as it is
            self.provider = provider
            self.routing_health = (provider.health() if hasattr(provider, 'health') else RoutingHealth(
                'ok', getattr(provider, 'kind', 'schematic'), bool(getattr(provider, 'live', False)), True,
                'Routing provider supplied by the caller.'))
        else:                                        # never silently swapped: an error means no journey planning
            self.provider, self.routing_health = load_routing(environment=routing_env)
        self.routing_problem = self.routing_health.message if self.routing_health.status == 'error' else ''
        if self.routing_problem:
            self.problems.append(self.routing_problem)
        self.folder = Path(folder) if folder else core.data_dir()
        cta_errors.configure(self.folder)
        cta_errors.register_known_secrets()
        root.report_callback_exception = self._on_unexpected_error   # no button press ends in a silent traceback
        self.storage_problem: Optional[str] = None   # set => bookings, check-ins and payments are paused
        self.save_error = ''
        self.store: Optional[cta_db.Store] = None
        self.history: List[dict] = []
        self.accounts: List[dict] = []
        self.staff_accounts: List[dict] = []
        try:
            self.store = cta_db.Store.open(self.folder, clock())
            self.history = self.store.load_history()
            self.accounts = self.store.load_accounts()
            self.staff_accounts = self.store.load_staff()
            if self.store.read_only:
                self.storage_problem = ('The data folder is read-only, so bookings, check-ins and payments are '
                                        'paused. Existing tickets can still be viewed.')
            else:
                try:
                    self.store.auto_backup(clock())
                except StorageError as exc:
                    self.problems.append(f'Daily backup failed: {exc}')
                try:
                    self.store.prune_sessions(clock())   # housekeeping: old sign-in rows, never audit records
                except StorageError as exc:              # nothing is lost; recorded for whoever maintains the app
                    cta_errors.report(exc, 'tidying up old sign-in sessions', clock())
        except StorageError as exc:
            self.storage_problem = str(exc)
        if self.storage_problem:
            self.problems.insert(0, self.storage_problem)
        self.fare_problem = ''
        try:                                         # the code's fares must equal the reviewed fare matrix
            fares.check_against_matrix()
        except fares.FarePolicyError as exc:
            self.fare_problem = str(exc)
            self.problems.append(self.fare_problem)
        self._shown_total: Optional[int] = None
        self._last_names: Optional[List[str]] = None
        self.signer: Optional[cta_tickets.TicketSigner] = None   # signs / verifies the QR codes
        self.key_problem = ''
        self.device_id = (os.environ.get('CTA_DEVICE_ID') or socket.gethostname() or 'device')[:40]
        if self.store is not None:
            try:
                if self.store.read_only:             # can still verify and print existing tickets, never creates a key
                    self.signer = cta_tickets.load_signer_readonly(self.folder)
                else:
                    self.signer = cta_tickets.load_signer(self.folder, self.store, clock())
                cta_tickets.set_default_signer(self.signer)
            except (cta_tickets.KeyUnavailable, StorageError) as exc:
                self.key_problem = str(exc)
                self.problems.append(self.key_problem)
            self.reload_history(force=True)          # key set-up writes a note to the database: start from its latest state
        self.account: Optional[dict] = None          # the signed-in passenger (None until sign-in)
        self.staff: Optional[dict] = None            # the signed-in admin (None unless staff signed in)
        self.data_target = (self.store if self.store is not None and not self.store.read_only
                            else _BlockedStorage(self.storage_problem or 'Data storage is unavailable.'))
        try:
            if sms is not None:
                self.sms = sms
                self.sms_template_sid = getattr(sms, 'template_sid', '')
            else:
                self.sms = load_sms_provider()
                # load_sms_provider attaches .template_sid on every returned provider
                self.sms_template_sid = getattr(self.sms, 'template_sid', '')
        except StorageError as exc:
            self.sms = SmsUnavailable('Sign-in is unavailable because the settings file is damaged. '
                                      'Please ask staff for help.')
            self.sms.template_sid = ''
            self.sms_template_sid = ''
            self.problems.append(str(exc))
        for secret in (getattr(self.provider, 'api_key', None), getattr(self.sms, 'auth_token', None)):
            cta_errors.register_secret(secret)       # never written to the diagnostics log
        self.environment = environment if environment in payments.ENVIRONMENTS else payments.current_environment()
        self.pay_provider = (pay_provider if pay_provider is not None
                             else payments.load_payment_provider(environment=self.environment))
        self.online_ok, self.online_msg = payments.online_availability(self.pay_provider, self.environment)
        self.ledger = (cta_db.StoreLedger(self.store) if self.store is not None and not self.store.read_only
                       else payments.PaymentLedger(None))
        self.staff_auth = (cta_auth.StaffAuth(self.store) if self.store is not None and not self.store.read_only
                           and not self.storage_problem else None)
        if self.store is not None:
            self._record_configuration(clock())
            self.reload_history(force=True)
        self.current_page = 'plan'
        self.trip_type = 'One way'
        self.pick_step = 'from'
        self.route: Optional[Route] = None
        self.deps: List[datetime] = []
        self.dep_idx = 0
        self.anchor = clock()
        self.counts: Dict[str, int] = {c: 0 for c in core.CATEGORIES}
        self._fails, self._locked_until = 0, 0.0
        self._photos: list = []
        self.map_sel: Optional[str] = None
        self.mono = 'Consolas' if 'Consolas' in tkfont.families() else 'DejaVu Sans Mono'

        root.title('CTA Metro')
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f'{min(px(1280, root), sw - 20)}x{min(px(830, root), sh - 70)}+10+10')
        root.minsize(min(px(1120, root), sw - 20), min(px(780, root), sh - 70))
        root.configure(bg=BG)
        if sys.platform == 'win32':
            try:
                root.state('zoomed')                 # fill the screen so nothing is cut off
            except tk.TclError:
                pass
        self._header()
        self.bar = self._bottom_bar()
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill='both', expand=True)
        self.pages: Dict[str, tk.Frame] = {}
        self._build_plan()
        self._build_map()
        self._build_tickets()
        self._build_help()
        self._build_admin()
        self._build_zone()
        self._build_board()
        self._build_voucher_page()
        self.show_page('plan')
        self.from_pick.set('Centrala')
        self._derive_step()
        self.update_marks()
        self.update_bar()
        self.show_page('zone')                       # home: the zone ticket (no station needs to be chosen)

    # ------------------------------------------------------------ shell
    def _header(self):
        h = tk.Frame(self.root, bg=NAVY, height=66)
        h.pack(fill='x')
        h.pack_propagate(False)
        logo = tk.Canvas(h, width=44, height=44, bg=NAVY, highlightthickness=0)
        logo.pack(side='left', padx=(22, 10), pady=11)
        logo.create_oval(2, 2, 42, 42, fill=ACCENT, outline='')
        logo.create_text(22, 22, text='C', fill='white', font=F(18, True))
        t = tk.Frame(h, bg=NAVY)
        t.pack(side='left')
        tk.Label(t, text='CTA Metro', bg=NAVY, fg='white', font=F(15, True)).pack(anchor='w')
        tk.Label(t, text='Centrala Transport Authority', bg=NAVY, fg='#9FB3CE', font=F(9)).pack(anchor='w')
        nav = tk.Frame(h, bg=NAVY)
        nav.pack(side='left', padx=16)
        self.nav: Dict[str, RoundButton] = {}
        for key, label in PAGES:
            b = RoundButton(nav, label, lambda k=key: self.show_page(k), width=NAV_WIDTH.get(key, 100),
                            height=38, radius=19, kind='dark', font=F(10, True), parent_bg=NAVY)
            b.pack(side='left', padx=2)
            self.nav[key] = b
        self.nav['admin'] = RoundButton(nav, 'Admin', lambda: self.show_page('admin'), width=112, height=38, radius=19,
                                        kind='dark', font=F(10, True), parent_bg=NAVY)
        self._admin_shown = False                    # the Admin tab only appears after staff sign in
        self._help_shown = True                      # ...and the passenger Help tab disappears for staff
        self.acct_btn = RoundButton(h, 'Sign in', self.account_click, width=170, height=38, radius=19,
                                    font=F(10, True), parent_bg=NAVY)
        self.acct_btn.pack(side='right', padx=(0, 22))
        if self.problems:
            badge = tk.Label(h, text='DATA PROBLEM - see details' if self.storage_problem else
                             'JOURNEY PLANNING UNAVAILABLE' if self.routing_problem else 'SETTINGS PROBLEM',
                             bg=DANGER, fg='white', font=F(9, True), padx=10, pady=4, cursor='hand2')
            badge.pack(side='right', padx=(0, 12))
            badge.bind('<Button-1>', lambda e: self._show_problems())
            self._problems_after = self.root.after(300, self._show_problems)
        if self.environment != payments.PRODUCTION:     # never let a test build pass for the real thing
            tk.Label(h, text=f'{self.environment.upper()} MODE - no real money',
                     bg=WARN_SOFT, fg=WARN_INK, font=F(9, True), padx=10, pady=4).pack(side='right', padx=(0, 12))

    def show_page(self, key: str):
        if key == 'admin' and not (self.staff and self.store is not None
                                   and self.store.has_permission(self.staff, 'report.view', self.clock())):
            key = 'plan'
        if key == 'voucher' and getattr(self, 'voucher', None) is None:
            key = 'zone'                             # no voucher yet: go to the booking form
        self.current_page = key
        for k, f in self.pages.items():
            f.pack_forget()
        self.pages[key].pack(fill='both', expand=True)
        lit = 'zone' if key == 'voucher' else key
        for k, b in self.nav.items():
            b.set_kind('soft' if k == lit else 'dark')
        if key == 'plan':
            self.bar.pack(side='bottom', fill='x', before=self.body)
        else:
            self.bar.pack_forget()
        if key == 'map':
            self._sync_map()
        if key == 'tickets':
            self.refresh_my_tickets()
        if key == 'admin':
            self.admin.start()
        else:
            self.admin.stop()

    # ------------------------------------------------------------ account
    def _paint_account(self):
        if self.staff:
            name = self.staff['username']
            self.acct_btn.set_text(f"Admin · {name if len(name) <= 10 else name[:9] + '…'}")
            self.acct_btn.set_kind('soft')
        elif self.account:
            first = self.account['name'].split()[0]
            first = first if len(first) <= 10 else first[:9] + '…'
            self.acct_btn.set_text(f"{first}  ··{self.account['phone'][-4:]}")
            self.acct_btn.set_kind('soft')
        else:
            self.acct_btn.set_text('Sign in')
            self.acct_btn.set_kind('primary')
        self._sync_help()
        if self.staff and self._help_shown:                  # staff do not need the passenger Help page: their
            self.nav['help'].pack_forget()                   # complaints and messages are in the Admin page
            self._help_shown = False
            if self.current_page == 'help':
                self.show_page('plan')
        elif not self.staff and not self._help_shown:
            self.nav['help'].pack(side='left', padx=3)
            self._help_shown = True
        if self.staff and not self._admin_shown:
            self.nav['admin'].pack(side='left', padx=3)
            self._admin_shown = True
        elif not self.staff and self._admin_shown:
            self.nav['admin'].pack_forget()
            self._admin_shown = False
            if self.current_page == 'admin':
                self.show_page('plan')

    def account_click(self):
        if self.staff is None and self.account is None:
            self.open_auth()
            return
        if self.staff:
            who = f"admin {self.staff['username']}"
        else:
            who = f"{self.account['name']} ({acct.mask_phone(self.account['phone'])})"
        if messagebox.askyesno('Account', f'Signed in as {who}.\n\nSign out?', parent=self.root):
            self.sign_out()

    def sign_out(self):
        if self.staff is not None and self.staff_auth is not None:
            try:
                self.staff_auth.logout(self.staff, self.clock())    # ends the session for every check, at once
            except OSError as exc:
                incident = cta_errors.report(exc, 'staff sign-out', self.clock())
                messagebox.showwarning('Sign-out', 'You are signed out on this screen, but the sign-in could not be '
                                       'closed in the database. It ends by itself within '
                                       f'{cta_auth.SESSION_HOURS} hours; tell an administrator. (Reference {incident}.)',
                                       parent=self.root)
        self.account = None
        self.staff = None
        self._paint_account()
        self.refresh_my_tickets()

    def open_auth(self, on_success: Optional[Callable[[], None]] = None, intro: str = ''):
        def done(account: dict):
            self.staff = None
            self.account = account
            self._paint_account()
            self.refresh_my_tickets()
            if on_success:
                on_success()

        def done_staff(principal):
            self.account = None
            self.staff = principal                   # a cta_auth.Principal: the services re-check it every time
            self._paint_account()
            self.refresh_my_tickets()
            self.show_page('admin')

        AuthDialog(self.root, self.accounts, self.data_target, self.clock, done, intro, sms=self.sms,
                   sms_template_sid=getattr(self, 'sms_template_sid', ''),
                   staff=self.staff_accounts, staff_path=self.data_target, on_staff=done_staff,
                   staff_auth=self.staff_auth, device=self.device_id, audit=self._audit_passenger_sign_in)

    def _audit_passenger_sign_in(self, phone: str, ok: bool, reason: str):
        """Passenger sign-in attempts: outcome and a masked phone number only (never the code)."""
        if self.can_save():
            self.store.audit('auth.passenger_login', self.clock().strftime('%Y-%m-%d %H:%M:%S'),
                             actor=acct.mask_phone(phone, '*'), outcome='success' if ok else 'failure',
                             detail={'reason': reason} if reason else None)

    def _watch_routing_health(self):
        """A live provider that starts or stops failing is an operational event: record the change once."""
        if not hasattr(self.provider, 'health'):
            return
        new = self.provider.health()
        old, self.routing_health = self.routing_health, new
        if new.status != old.status and self.can_save():
            try:
                self.store.audit('routing.live_failed' if new.status == 'degraded' else 'routing.live_restored',
                                 self.clock().strftime('%Y-%m-%d %H:%M:%S'), actor='system', target=new.provider,
                                 outcome='failure' if new.status == 'degraded' else 'success',
                                 detail={'message': new.message})
            except OSError as exc:
                cta_errors.report(exc, 'recording a routing health change', self.clock())

    def _record_configuration(self, now):
        """Write an audit event whenever the fare policy, the signing keys or the settings file changed since the
        last start (fingerprints only, never values)."""
        if not self.can_save():
            return
        import hashlib
        def digest(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()[:16]
        try:
            matrix = fares.MATRIX_FILE.read_bytes()
        except OSError:
            matrix = b''
        cfg_path = rt.CONFIG_PATH
        try:
            cfg = cfg_path.read_bytes()
        except OSError:
            cfg = b''
        items = {'fare_policy': f'{fares.FARE_POLICY_VERSION}:{digest(matrix)}',
                 'signing_keys': ','.join(self.signer.kids) if self.signer else 'none',
                 'settings': digest(cfg) if cfg else 'none',
                 'environment': self.environment,
                 'routing': f"{self.routing_health.provider}:{'live' if self.routing_health.live else 'estimated'}:"
                            f"{'chosen' if self.routing_health.explicit else 'default'}:{self.routing_health.status}"}
        stamp = now.strftime('%Y-%m-%d %H:%M:%S')
        try:
            if self.routing_problem:                 # why journey planning is off (the message never holds the key)
                self.store.audit('routing.unavailable', stamp, actor='system', outcome='failure',
                                 detail={'message': self.routing_problem})
            for item, value in items.items():
                old = self.store.meta_get('config.' + item)
                if old != value:
                    self.store.audit('config.changed' if old else 'config.recorded', stamp, actor='system',
                                     target=item, detail={'from': old or '', 'to': value})
                    self.store.meta_set('config.' + item, value)
        except OSError as exc:
            cta_errors.report(exc, 'recording configuration changes', now)

    def _on_unexpected_error(self, exc_type, exc, tb):
        """Any error that escapes a button or timer: record it with a reference, tell the person plainly, keep what they
        typed on screen (nothing is cleared here)."""
        if exc is None:
            exc = exc_type()
        exc.__traceback__ = tb
        message = cta_errors.user_message(exc, 'screen action')
        try:
            messagebox.showerror('CTA Metro', message, parent=self.root)
        except tk.TclError:
            pass                                     # the window is closing; the error is already in the log

    # ------------------------------------------------------------ shared database
    def _show_problems(self):
        if self.problems:
            messagebox.showerror('CTA Metro - data problem' if self.storage_problem else 'CTA Metro - settings problem',
                                 '\n\n'.join(self.problems), parent=self.root)

    def can_save(self) -> bool:
        return self.store is not None and not self.store.read_only and not self.storage_problem

    def can_issue(self) -> bool:
        """New tickets need a working database AND a signing key: a ticket whose QR code cannot be verified is not issued."""
        return self.can_save() and self.signer is not None and not self.fare_problem and not self.routing_problem

    def reload_history(self, force: bool = False):
        """Pick up bookings saved by another window or computer using the same database."""
        if self.store is None:
            return
        try:
            if force or self.store.has_changes():
                self.history = self.store.load_history()
        except StorageError as exc:
            self.storage_problem = str(exc)          # keep what is on screen; stop writing until it is fixed

    def save_history(self) -> bool:
        """Write new and changed bookings. On any failure nothing is half-saved: the screen is reloaded from the
        database (so unsaved changes disappear) and ``self.save_error`` says why."""
        self.save_error = ''
        if not self.can_save():
            self.save_error = self.storage_problem or 'Data storage is unavailable.'
            return False
        try:
            self.store.save_history(self.history, self.clock(), self.staff)
        except ConcurrentUpdate as exc:
            self.save_error = str(exc)
            self.reload_history(force=True)
            return False
        except (StorageError, cta_auth.PermissionDenied) as exc:
            self.save_error = str(exc)
            self.reload_history(force=True)
            return False
        return True

    def _bottom_bar(self):
        bar = tk.Frame(self.root, bg=NAVY, height=84)
        bar.pack_propagate(False)
        left = tk.Frame(bar, bg=NAVY)
        left.pack(side='left', padx=26, pady=12)
        self.bar_people = tk.Label(left, text='', bg=NAVY, fg='white', font=F(12, True))
        self.bar_people.pack(anchor='w')
        self.bar_sub = tk.Label(left, text='', bg=NAVY, fg='#9FB3CE', font=F(10))
        self.bar_sub.pack(anchor='w')
        self.book_btn = RoundButton(bar, 'Book ticket', self.book, width=190, height=52, radius=14,
                                    font=F(13, True), parent_bg=NAVY)
        self.book_btn.pack(side='right', padx=26)
        self.bar_total = tk.Label(bar, text='', bg=NAVY, fg='white', font=F(20, True))
        self.bar_total.pack(side='right', padx=(0, 18))
        RoundButton(bar, 'Fare details', self.fare_details, width=120, height=38, radius=12, kind='dark',
                    font=F(10, True), parent_bg=NAVY, outline='#2D4A73').pack(side='right', padx=(0, 18))
        return bar

    # ------------------------------------------------------------ plan page
    def _build_plan(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['plan'] = page
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)

        # ---- left: the journey form (scrolls if the screen is short, so nothing is ever cut off)
        left = Card(page, pad=10)
        left.grid(row=0, column=0, sticky='ns', padx=(16, 8), pady=12)
        scroll = ScrollFrame(left.body, width=px(356, self.root))
        scroll.pack(fill='both', expand=True)
        b = tk.Frame(scroll.body, bg=CARD)
        b.pack(fill='x', padx=(6, 8), pady=(4, 8))
        tk.Label(b, text='Plan your journey', bg=CARD, fg=INK, font=F(16, True)).pack(anchor='w')
        tk.Label(b, text='Tap the map, or type a station name.', bg=CARD, fg=MUTED, font=F(9)
                 ).pack(anchor='w', pady=(0, 8))

        row = tk.Frame(b, bg=CARD)
        row.pack(fill='x')
        row.grid_columnconfigure(0, weight=1)
        self.from_pick = StationPicker(row, 'FROM', self.search)
        self.from_pick.grid(row=0, column=0, sticky='ew')
        self.to_pick = StationPicker(row, 'TO', self.search)
        self.to_pick.grid(row=1, column=0, sticky='ew', pady=(6, 0))
        RoundButton(row, '⇅', self.swap, width=44, height=44, radius=22, kind='soft', font=F(15, True)
                    ).grid(row=0, column=1, rowspan=2, padx=(10, 0))
        self.err = tk.Label(b, text='', bg=CARD, fg=DANGER, font=F(9, True), wraplength=px(330, self.root),
                            justify='left')
        self.err.pack(anchor='w')

        tk.Label(b, text='WHEN', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(4, 3))
        wr = tk.Frame(b, bg=CARD)
        wr.pack(fill='x')
        self.when = Segmented(wr, ('Leave now', 'Later'), self._when_changed)
        self.when.pack(side='left')
        self.time_var = tk.StringVar()
        self.time_box = tk.Frame(wr, bg=LINE, padx=1, pady=1)
        self.time_entry = tk.Entry(self.time_box, textvariable=self.time_var, width=6, relief='flat', justify='center',
                                   font=F(13, True), bg='#F7F9FC', fg=INK, highlightthickness=0, insertbackground=INK)
        self.time_entry.pack(ipady=5)
        self.time_entry.bind('<Return>', lambda e: self.search())
        self.time_entry.bind('<FocusOut>', lambda e: self.search())

        tk.Label(b, text='TRIP', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(10, 3))
        self.trip = Segmented(b, ('One way', 'Return'), self._trip_changed)
        self.trip.pack(anchor='w')

        tk.Label(b, text='TRAVELLERS', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(10, 3))
        grid = tk.Frame(b, bg=CARD)
        grid.pack(fill='x')
        grid.grid_columnconfigure((0, 1), weight=1, uniform='trav')
        self.steppers: Dict[str, Stepper] = {}
        for i, cat in enumerate(core.CATEGORIES):
            cell = tk.Frame(grid, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=8, pady=6)
            cell.grid(row=i // 2, column=i % 2, sticky='nsew', padx=(0, 6) if i % 2 == 0 else (0, 0), pady=3)
            tk.Label(cell, text=cat, bg=CARD, fg=INK, font=F(11, True)).pack(anchor='w')
            tk.Label(cell, text=core.CATEGORY_HINT[cat], bg=CARD, fg=MUTED, font=F(8)).pack(anchor='w')
            tk.Label(cell, text=f'{core.FARE_RATES_CENTS[cat]:,} cents / zone', bg=CARD, fg=MUTED, font=F(8)
                     ).pack(anchor='w')
            st = Stepper(cell, core.MAX_PER_CATEGORY, lambda c=cat: self._count_changed(c))
            st.pack(anchor='w', pady=(4, 0))
            self.steppers[cat] = st

        # ---- right: steps, result, map
        right = tk.Frame(page, bg=BG)
        right.grid(row=0, column=1, sticky='nsew', padx=(8, 16), pady=12)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)

        self.steps_card = Card(right, pad=10)
        self.steps_card.grid(row=0, column=0, sticky='ew')
        srow = tk.Frame(self.steps_card.body, bg=CARD)
        srow.pack(fill='x')
        self.step_labels: List[tk.Label] = []
        for i in range(3):
            lab = tk.Label(srow, text='', font=F(11, True), padx=14, pady=8, cursor='hand2')
            lab.pack(side='left', padx=(0, 8))
            self.step_labels.append(lab)
        self.step_labels[0].bind('<Button-1>', lambda e: self._set_step('from'))
        self.step_labels[1].bind('<Button-1>', lambda e: self._set_step('to'))
        self.step_hint = tk.Label(srow, text='', bg=CARD, fg=MUTED, font=F(10))
        self.step_hint.pack(side='left', padx=8)

        self.summary = Card(right, pad=16)
        self.summary.grid(row=1, column=0, sticky='ew', pady=(10, 0))
        self.deps_card = Card(right, pad=10)
        self.deps_card.grid(row=2, column=0, sticky='ew', pady=(10, 0))
        lower = tk.Frame(right, bg=BG)
        lower.grid(row=3, column=0, sticky='nsew', pady=(10, 0))
        lower.grid_columnconfigure(0, weight=1)
        lower.grid_rowconfigure(0, weight=1)
        mcard = Card(lower, pad=6)
        mcard.grid(row=0, column=0, sticky='nsew')
        self.mini = NetworkMap(mcard.body, labels='all', interactive=True, on_click=self._plan_map_click,
                               width=px(420, self.root), height=px(240, self.root))
        self.mini.pack(fill='both', expand=True)
        self.tcard = Card(lower, pad=12)
        self.tcard.grid(row=0, column=1, sticky='ns', padx=(10, 0))
        tk.Label(self.tcard.body, text='Your stops', bg=CARD, fg=INK, font=F(12, True)).pack(anchor='w', padx=4)
        tl = tk.Frame(self.tcard.body, bg=CARD)
        tl.pack(fill='both', expand=True, pady=(6, 0))
        sb = tk.Scrollbar(tl, orient='vertical')
        sb.pack(side='right', fill='y')
        self.timeline = Timeline(tl, width=px(300, self.root), height=px(260, self.root), yscrollcommand=sb.set)
        sb.config(command=self.timeline.yview)
        self.timeline.pack(side='left', fill='both', expand=True)
        self._render_summary()
        self._render_deps()

    # ------------------------------------------------------------ pick stations on the map
    def _derive_step(self):
        a, b = self.from_pick.get(), self.to_pick.get()
        self.pick_step = 'from' if not a else ('to' if not b else 'done')

    def _set_step(self, step: str):
        self.pick_step = step
        self._update_steps()

    def _plan_map_click(self, name: str):
        """Tap a station: first tap = start, second tap = destination, a tap after that starts a new journey."""
        a, b = self.from_pick.get(), self.to_pick.get()
        step = self.pick_step
        if step == 'from' or not a:
            self.from_pick.set(name)
            if b == name:
                self.to_pick.set(None)
        elif step == 'to':
            if name == a:
                self.err.config(text='That is your start. Tap a different station for your destination.')
                return
            self.to_pick.set(name)
        else:
            self.from_pick.set(name)
            self.to_pick.set(None)
        self.search()

    def _update_steps(self):
        labels = getattr(self, 'step_labels', None)
        if not labels:
            return
        a, b = self.from_pick.get(), self.to_pick.get()
        step = getattr(self, 'pick_step', 'from')
        people = sum(self.counts.values())
        styles = {'done': (ACCENT_SOFT, ACCENT_DARK), 'active': (ACCENT, 'white'), 'todo': ('#EEF2F7', MUTED)}

        def paint(i, state, text):
            bg, fg = styles[state]
            labels[i].config(text=text, bg=bg, fg=fg)

        if step == 'from':
            paint(0, 'active', '1   Tap your start')
            paint(1, 'todo', '2   Destination')
        elif step == 'to':
            paint(0, 'done', f'✓   From {a}')
            paint(1, 'active', '2   Tap your destination')
        else:
            paint(0, 'done', f'✓   From {a}')
            paint(1, 'done', f'✓   To {b}')
        if people:
            paint(2, 'done', f'✓   {core.people_label(self.counts)}')
        else:
            paint(2, 'active' if step == 'done' else 'todo', '3   Add travellers')
        self.step_hint.config(text={'from': 'Tap a station on the map',
                                    'to': 'Now tap where you are going',
                                    'done': 'Tap any station to plan a new trip'}[step])

    def _when_changed(self, v):
        if v == 'Later':
            t = round_minute(self.clock() + timedelta(minutes=10))
            self.time_var.set(t.strftime('%H:%M'))
            self.time_box.pack(side='left', padx=(6, 0))
            self.time_entry.focus_set()
        else:
            self.time_box.pack_forget()
        self.search()

    def _anchor_time(self) -> Optional[datetime]:
        now = self.clock()
        if self.when.value == 'Leave now':
            return now
        txt = self.time_var.get().strip()
        try:
            hh, mm = txt.split(':')
            t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except ValueError:
            self.err.config(text='Enter the time as HH:MM, for example 14:30.')
            return None
        return t if t >= now.replace(second=0, microsecond=0) else t + timedelta(days=1)

    def swap(self):
        a, b = self.from_pick.get(), self.to_pick.get()
        self.from_pick.set(b)
        self.to_pick.set(a)
        self.search()

    def search(self, *_):
        self.err.config(text='')
        a, b = self.from_pick.get(), self.to_pick.get()
        self._derive_step()
        self.update_marks()
        self._update_steps()
        if not a or not b:
            self._clear_route()
            return
        if a == b:
            self.err.config(text='Choose two different stations.')
            self._clear_route()
            return
        anchor = self._anchor_time()
        if anchor is None:
            return
        if self.provider is None:
            self.err.config(text='Journey planning is unavailable: ' + self.routing_problem)
            self._clear_route()
            return
        try:
            self.route = self.provider.plan(a, b)
        except ValueError as exc:
            self.err.config(text=str(exc))
            self._clear_route()
            return
        self._watch_routing_health()
        self.anchor = anchor
        self.deps = next_departures(anchor, 4)
        self.dep_idx = 0
        self.render()

    def _clear_route(self):
        self.route, self.deps = None, []
        self.render()

    def _trip_changed(self, value: str):
        self.trip_type = value
        self.update_bar()

    def _legs(self) -> int:
        return 2 if self.trip_type == 'Return' else 1

    def _count_changed(self, cat):
        self.counts[cat] = self.steppers[cat].value
        self.update_bar()

    # ------------------------------------------------------------ results
    def _chip(self, parent, text, bg=ACCENT_SOFT, fg=ACCENT_DARK):
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=F(10, True), padx=10, pady=4)

    def _render_summary(self):
        for w in self.summary.body.winfo_children():
            w.destroy()
        s = self.summary.body
        if not self.route:
            self.summary.grid_remove()
            return
        self.summary.grid()
        r = self.route
        dep = self.deps[self.dep_idx]
        wait = max(0.0, (dep - self.anchor).total_seconds() / 60)
        total = wait + r.ride_minutes
        arrive = round_minute(dep + timedelta(minutes=r.ride_minutes))
        top = tk.Frame(s, bg=CARD)
        top.pack(fill='x')
        big = tk.Frame(top, bg=CARD)
        big.pack(side='left')
        tk.Label(big, text=f'{total:.0f} min', bg=CARD, fg=INK, font=F(30, True)).pack(anchor='w')
        tk.Label(big, text=f'{r.ride_minutes:.0f} min ride  +  {wait:.0f} min wait', bg=CARD, fg=MUTED,
                 font=F(10)).pack(anchor='w')
        arr = tk.Frame(top, bg=CARD)
        arr.pack(side='right')
        tk.Label(arr, text=f'Arrive {arrive:%H:%M}', bg=CARD, fg=ACCENT_DARK, font=F(18, True)).pack(anchor='e')
        tk.Label(arr, text=f'Depart {round_minute(dep):%H:%M}  ·  {dep:%a %d %b}', bg=CARD, fg=MUTED,
                 font=F(10)).pack(anchor='e')
        tk.Label(s, text=f'{r.origin}  →  {r.destination}', bg=CARD, fg=INK, font=F(13, True)
                 ).pack(anchor='w', pady=(10, 6))
        chips = tk.Frame(s, bg=CARD)
        chips.pack(fill='x')
        self._chip(chips, f'{r.km:.2f} km').pack(side='left', padx=(0, 6))
        self._chip(chips, f'{r.stops} hop{"" if r.stops == 1 else "s"}').pack(side='left', padx=(0, 6))
        zones = core.chargeable_zones(r.origin, r.destination, r)
        self._chip(chips, f'{zones} zone{"" if zones == 1 else "s"} charged', bg='#EEF2F7', fg=INK).pack(side='left')
        zrow = tk.Frame(s, bg=CARD)
        zrow.pack(fill='x', pady=(6, 0))
        tk.Label(zrow, text='Passes through', bg=CARD, fg=MUTED, font=F(9)).pack(side='left', padx=(0, 8))
        for z in r.zones_entered:
            tk.Label(zrow, text=z, bg=ZONE_COLOR[z], fg='white', font=F(9, True), padx=8, pady=2
                     ).pack(side='left', padx=(0, 4))
        if core.route_zones(r) != core.band_zones(r.origin, r.destination):
            tk.Label(s, text=f'Heads-up: the track passes through {core.route_zones(r)} zones, but fares follow the '
                             f'zone-band rule ({core.band_zones(r.origin, r.destination)} charged).',
                     bg=WARN_SOFT, fg=WARN_INK, font=F(9, True), padx=10, pady=5, wraplength=760, justify='left'
                     ).pack(anchor='w', pady=(8, 0))
        if r.verified:
            tk.Label(s, text='Distance and time: live from Google Routes.', bg=CARD, fg=ACCENT_DARK,
                     font=F(9, True)).pack(anchor='w', pady=(8, 0))
        elif getattr(self.provider, 'live', False):          # a live provider was chosen but did not answer: say so
            tk.Label(s, text=r.note or 'Live routing unavailable: distance and time are ESTIMATED, not live.',
                     bg=WARN_SOFT, fg=WARN_INK, font=F(9, True), padx=10, pady=5, wraplength=760, justify='left'
                     ).pack(anchor='w', pady=(8, 0))
        else:
            tk.Label(s, text='Distance and time are estimates from the CTA network diagram (not live).', bg=CARD,
                     fg=MUTED, font=F(9)).pack(anchor='w', pady=(8, 0))

    def _render_deps(self):
        for w in self.deps_card.body.winfo_children():
            w.destroy()
        d = self.deps_card.body
        if not self.route:
            self.deps_card.grid_remove()
            return
        self.deps_card.grid()
        tk.Label(d, text='Next trains', bg=CARD, fg=INK, font=F(12, True)
                 ).pack(side='left', padx=(4, 12))
        for i, dep in enumerate(self.deps):
            arr = round_minute(dep + timedelta(minutes=self.route.ride_minutes))
            RoundButton(d, f'{round_minute(dep):%H:%M} → {arr:%H:%M}', lambda i=i: self.pick_departure(i),
                        width=124, height=40, radius=12, font=F(10, True),
                        kind='primary' if i == self.dep_idx else 'ghost').pack(side='left', padx=3)

    def pick_departure(self, i):
        self.dep_idx = i
        self.render()

    def render(self):
        self._render_summary()
        self._render_deps()
        if self.route:
            rows = stop_times(self.route, self.deps[self.dep_idx])
            rows = [(n, round_minute(t), km) for n, t, km in rows]
            self.timeline.show(self.route, rows)
        else:
            self.timeline.show(None, None)
        self.mini.set_route(self.route)
        if self.route:
            self.tcard.grid()
        else:
            self.tcard.grid_remove()
        self.update_bar()
        if 'map' in self.pages:
            self._sync_map()

    # ------------------------------------------------------------ bottom bar / fare
    def current_zones(self) -> int:
        return core.chargeable_zones(self.route.origin, self.route.destination, self.route) if self.route else 0

    def current_quote(self) -> Optional[fares.FareQuote]:
        """The fare for what is on screen right now, from the one fare function (None if it cannot be priced)."""
        if not self.route or self.fare_problem:
            return None
        try:
            return fares.quote(self.route.origin, self.route.destination, self.counts, self._legs(), self.route)
        except fares.FareError:
            return None

    def update_bar(self):
        counts = {k: v for k, v in self.counts.items()}
        people = sum(counts.values())
        self.bar_people.config(text=core.people_label(counts))
        self._update_steps()
        if not self.route:
            self.bar_sub.config(text='Choose a journey to see the fare')
            self.bar_total.config(text='')
            self.book_btn.set_enabled(False)
            return
        z = self.current_zones()
        legs = self._legs()
        q = self.current_quote() if people else None
        self.bar_sub.config(text=f'{self.route.origin} → {self.route.destination}  ·  {z} zone{"" if z == 1 else "s"}'
                                 + ('  ·  return' if legs == 2 else ''))
        self.bar_total.config(text=core.format_cents(q.total_cents) if q else '—')
        self.book_btn.set_enabled(q is not None)

    def fare_details(self):
        if not self.route:
            messagebox.showinfo('Fare details', 'Choose a journey first.', parent=self.root)
            return
        if self.fare_problem:
            messagebox.showerror('Fare details', self.fare_problem, parent=self.root)
            return
        q = self.current_quote()
        body = q.summary() if q else 'Add at least one traveller.'
        extra = '\nA return ticket is two single journeys; the return is valid on the day of travel.' if self._legs() == 2 else ''
        messagebox.showinfo('Fare details', f'{body}{extra}\n\nPolicy: {fares.FARE_POLICY_VERSION}', parent=self.root)

    # ------------------------------------------------------------ booking
    def book(self):
        """Book ticket: check travellers -> sign in (phone) -> passenger names -> issue named tickets."""
        if not self.route:
            return
        if not self.can_issue():
            messagebox.showerror('Bookings are paused', (self.storage_problem or self.key_problem or self.fare_problem
                                                         or self.routing_problem
                                                         or 'Data storage is unavailable.')
                                 + '\n\nNo ticket can be booked until this is fixed.', parent=self.root)
            return
        ok, why = core.validate_counts(self.counts)
        if not ok:
            messagebox.showwarning('Check travellers', why, parent=self.root)
            return
        ok, why = core.validate_booking_size(self.counts)
        if not ok:
            messagebox.showwarning('Too many travellers', why, parent=self.root)
            return
        if self.account is None:
            self.open_auth(on_success=self.book, intro='Sign in or sign up with your mobile number to book. Then you '
                                                       'will enter each passenger\u2019s name and choose how to pay.')
            return
        slots = core.passenger_slots(self.counts)
        prefill = self._last_names if self._last_names and len(self._last_names) == len(slots) else None
        PassengerDialog(self.root, slots, self.account['name'], self._names_done, prefill=prefill)

    def _names_done(self, names: List[str]):
        self._last_names = list(names)               # kept until the booking succeeds, so a failure costs no retyping
        q = self.current_quote()
        if q is None:
            messagebox.showerror('Could not book', self.fare_problem or 'This journey cannot be priced.', parent=self.root)
            return
        self._shown_total = q.total_cents                  # the amount the passenger is asked to pay
        test_mode = bool(self.online_ok and self.pay_provider is not None
                         and not getattr(self.pay_provider, 'live', False))
        PaymentDialog(self.root, q.total_cents, q.legs, lambda method, auth=None: self._issue(names, method, auth),
                      online_unavailable='' if self.online_ok else self.online_msg, test_mode=test_mode)

    def _issue(self, names: List[str], method: str, auth=None):
        """Names are in and a payment method is chosen: take payment (or reserve for the counter), then issue."""
        if not self.route or self.account is None:
            return
        if not self.can_issue():
            messagebox.showerror('Bookings are paused', self.storage_problem or self.key_problem or self.fare_problem
                                                         or self.routing_problem
                                 or 'Data storage is unavailable.', parent=self.root)
            return
        self.reload_history()
        now = self.clock()
        try:
            reference = self.store.reserve_reference(now)      # unique even if another window books right now
            record = core.make_record(self.route, self.counts, self.current_zones(), self.deps[self.dep_idx], now,
                                      self.history, names=names, account_phone=self.account['phone'],
                                      legs=self._legs(), reference=reference)
            shown, self._shown_total = self._shown_total, None    # belongs to this booking only
            if shown is not None and shown != record['total_cents']:
                raise ValueError(f"The fare changed from {core.format_cents(shown)} to "
                                 f"{core.format_cents(record['total_cents'])} since it was shown. Nothing was charged; "
                                 'please check the journey and book again.')
            problems = fares.fare_problems(record)
            if problems:
                raise ValueError('The booking does not match the fare policy (' + '; '.join(problems) + '). '
                                 'Nothing was charged.')
            payment = payments.pay(method, record['total_cents'], record['reference'], self.account['phone'], now,
                                   self.pay_provider, environment=self.environment, ledger=self.ledger)
            if payment.get('amount_cents') != record['total_cents']:     # the charge must equal the booking, always
                raise ValueError('Internal error: the payment amount differs from the booking total. No ticket was issued.')
            if auth is not None and getattr(auth, 'approved', False) and auth.detail:
                payment['detail'] = auth.detail[:40]                   # e.g. "Visa ending 4242"; nothing more is kept
        except payments.PaymentUnavailable as exc:
            messagebox.showerror('Online payment unavailable', f'{exc}\n\nNo ticket was issued and you were not '
                                 'charged.', parent=self.root)
            return
        except ValueError as exc:
            messagebox.showerror('Could not book', str(exc), parent=self.root)
            return
        except StorageError as exc:                             # e.g. the reference could not be reserved
            messagebox.showerror('Could not book', f'{exc}\n\nNothing was charged.', parent=self.root)
            return
        try:
            self._complete_booking(record, payment)
        except Exception as exc:  # noqa: BLE001 - after a payment attempt nothing may vanish without a trace
            incident = cta_errors.report(exc, 'completing a booking after payment', self.clock(),
                                         reference=record.get('reference'), payment_status=payment.get('status'),
                                         transaction=payment.get('transaction_id'))
            messagebox.showerror('Booking problem', 'Something went wrong while finishing your booking.\n\n'
                                 f"Booking reference: {record.get('reference')}\n"
                                 f"Payment: {payments.status_text(payment)}"
                                 + (f" (transaction {payment.get('transaction_id')})" if payment.get('transaction_id') else '')
                                 + f'\n\nPlease show this to CTA staff before paying again. (Reference {incident}.)',
                                 parent=self.root)

    def _complete_booking(self, record: dict, payment: dict):
        """Everything after the payment attempt: refuse, save, confirm. Any unexpected error here is reported with the
        booking and transaction references by the caller."""
        status = payment['status']
        if status in payments.REJECTED:
            title = 'Payment cancelled' if status == payments.CANCELLED else 'Payment failed'
            messagebox.showerror(title, (payment.get('message') or 'The payment was not completed.')
                                 + '\n\nNo ticket was issued.', parent=self.root)
            return
        record['payment'] = payment
        self.history.append(record)
        if not self.save_history():
            self.history = [r for r in self.history if r is not record]
            extra = ''
            if status in (payments.PAID,) + payments.UNCONFIRMED and not payment.get('simulated'):
                extra = (f"\n\nIf money was taken, show staff booking reference {record['reference']} and "
                         f"transaction {payment.get('transaction_id') or '(none)'}.")
            messagebox.showerror('Could not save booking', 'Your ticket was NOT issued because the booking could '
                                 f'not be saved.\n\n{self.save_error}' + extra, parent=self.root)
            return
        if status in payments.UNCONFIRMED:
            # Kept so the provider's confirmation can complete it later, but nothing valid for travel is issued.
            messagebox.showwarning('Payment not confirmed', f"{payment.get('message') or 'The payment is not confirmed yet.'}"
                                   f"\n\nBooking {record['reference']} is saved, but no ticket is valid until the payment "
                                   'is confirmed by the payment provider. Do not pay again.', parent=self.root)
            self._reset_travellers()
            return
        saved = self._export(record, self.folder / f"{record['reference']}.pdf", receipt=True)   # archive copy (keeps the receipt page)
        self.show_confirmation(record, saved)
        self._reset_travellers()

    def _reset_travellers(self):
        self._last_names = None                      # a finished booking: the typed names are no longer needed
        for st in self.steppers.values():
            st.set(0, notify=False)
        self.counts = {c: 0 for c in core.CATEGORIES}
        self.update_bar()

    def show_confirmation(self, record: dict, saved: Optional[Path] = None):
        dlg = tk.Toplevel(self.root)
        dlg.title('Ticket issued')
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        n = len(core.passengers_of(record))
        pay = core.payment_of(record)
        counter = pay.get('status') == payments.PENDING_COUNTER
        head = tk.Frame(dlg, bg=ACCENT)
        head.pack(side='top', fill='x')
        word = 'reserved' if counter else 'issued'
        tk.Label(head, text=f'✓  {"Ticket" if n == 1 else "Tickets"} {word}', bg=ACCENT, fg='white',
                 font=F(16, True), pady=10).pack()
        tk.Label(head, text=(f"Pay {core.format_cents(record.get('total_cents', 0))} at a CTA counter before you travel."
                             if counter else f"{payments.status_text(pay)}  ·  {pay.get('label', '')}"),
                 bg=ACCENT, fg='#D7F3EE',
                 font=F(10, True)).pack(pady=(0, 10))
        RoundButton(dlg, 'Done', dlg.destroy, width=200, height=44, parent_bg=BG).pack(side='bottom', pady=12)
        card = Card(dlg, pad=16)
        card.pack(side='top', fill='both', expand=True, padx=18, pady=(14, 4))
        self.ticket_view(card.body, record, saved).pack(fill='both', expand=True)
        center_window(dlg, self.root, 680, 780)
        make_modal(dlg)

    def _center(self, win, w, h):
        center_window(win, self.root, w, h)

    def ticket_view(self, parent, record: dict, saved: Optional[Path] = None,
                    allow_refund_request: bool = False) -> tk.Frame:
        f = tk.Frame(parent, bg=CARD)
        pax = core.passengers_of(record)
        top = tk.Frame(f, bg=CARD)
        top.pack(fill='x')
        qr_box = tk.Label(top, bg=CARD)
        qr_box.pack(side='left', padx=(0, 16))
        d = tk.Frame(top, bg=CARD)
        d.pack(side='left', fill='x', expand=True)
        tk.Label(d, text='BOOKING REFERENCE', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w')
        tk.Label(d, text=record.get('reference', ''), bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        tk.Label(d, text='PASSENGER', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(8, 0))
        name_lbl = tk.Label(d, text='', bg=CARD, fg=INK, font=F(15, True), wraplength=360, justify='left')
        name_lbl.pack(anchor='w')
        tk.Label(d, text='TICKET NUMBER', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w', pady=(8, 0))
        tno_lbl = tk.Label(d, text='', bg=ACCENT_SOFT, fg=ACCENT_DARK, font=(self.mono, 12, 'bold'), padx=8, pady=3)
        tno_lbl.pack(anchor='w')
        tk.Label(d, text='Cannot scan the QR code? Read this number to station staff.', bg=CARD, fg=MUTED,
                 font=F(9), wraplength=360, justify='left').pack(anchor='w', pady=(4, 0))
        status_lbl = tk.Label(d, text='', bg=CARD, fg=ACCENT_DARK, font=F(10, True), wraplength=360, justify='left')
        status_lbl.pack(anchor='w', pady=(6, 0))
        qr_mode_lbl = tk.Label(d, text='', bg=CARD, fg=MUTED, font=F(9), wraplength=360, justify='left')
        qr_mode_lbl.pack(anchor='w', pady=(4, 0))
        Tooltip(qr_box, lambda: qr_mode_lbl.cget('text'))

        entries = []
        for p in pax:
            entries.append((p, 'out'))
            if record.get('legs', 1) == 2 and not p.get('legacy'):
                entries.append((p, 'return'))

        def show_pax(i: int):
            p, leg = entries[i]
            name_lbl.config(text=p['name'])
            tno_lbl.config(text=p.get('return_ticket_no', p['ticket_no']) if leg == 'return' else p['ticket_no'])
            # Use a ticket URL QR when the server is configured (consistent with PDF export).
            # Fall back to signed token when no server URL is set.
            qr_payload = None
            mode_note = ''
            try:
                server_url = self._ticket_server_url()
                if not server_url:
                    mode_note = ('Scan with any phone camera to read this ticket\u2019s details. '
                                 'Station staff scan the same code to check you in.')
                if server_url and self.store is not None:
                    tid = p.get('return_ticket_id') if leg == 'return' else p.get('ticket_id')
                    if tid:
                        try:
                            from cta_ticket_server import issue_web_token, ticket_url as _ticket_url, validate_base_url
                            ok, reason = validate_base_url(server_url)
                            if ok:
                                token = issue_web_token(self.store.path, tid)
                                qr_payload = _ticket_url(server_url, token)
                            else:
                                qr_payload = None  # invalid URL; fall through to signed token
                                mode_note = (f'Ticket link not used ({reason}); the code shows the ticket details '
                                             'as text instead. Fix ticket_server.url in cta_config.json.')
                        except Exception:  # noqa: BLE001
                            qr_payload = None
                            mode_note = 'Ticket link unavailable; the code shows the ticket details as text instead.'
            except Exception:  # noqa: BLE001
                qr_payload = None
            if qr_payload is None:
                # Fall back to signed CTA1:... token (staff scanner only)
                try:
                    qr_payload = core.ticket_qr_text(record, p, leg, self.signer)
                except cta_tickets.KeyUnavailable:
                    qr_payload = None
            if qr_payload and qr_payload.startswith('http') and not mode_note:
                mode_note = 'Scan with a phone camera to open this ticket. Station staff can also scan it.'
            if not qr_payload:
                mode_note = 'No QR code could be made for this ticket. Read the ticket number to station staff.'
            qr_mode_lbl.config(text=mode_note)
            try:
                img = core.qr_image(qr_payload, 240) if qr_payload else None
            except Exception:  # noqa: BLE001
                img = None
            _tno, st, _uses, _max = cta_tickets.leg_fields(p, leg)
            shown = {'issued': ('Not used yet', ACCENT_DARK), 'boarded': ('USED - already checked in', WARN_INK),
                     'cancelled': ('CANCELLED - not valid for travel', DANGER),
                     'refunded': ('REFUNDED - not valid for travel', DANGER)}.get(st, ('', MUTED))
            status_lbl.config(text='' if p.get('legacy') else shown[0], fg=shown[1])
            photo = None
            if img is not None:
                try:
                    from PIL import ImageTk
                    photo = ImageTk.PhotoImage(img)
                except (ImportError, OSError, ValueError, RuntimeError, tk.TclError):
                    photo = None                     # preview only: the PDF still has the QR code
            if photo is not None:
                self._photos.append(photo)
                qr_box.config(image=photo, text='')
            else:
                qr_box.config(image='', text='QR preview unavailable.\nThe PDF has the QR code.', fg=MUTED,
                              font=F(9), justify='left')

        if len(entries) > 1:
            sel = tk.Frame(f, bg=CARD)
            sel.pack(fill='x', pady=(10, 0))
            tk.Label(sel, text='Show QR code for', bg=CARD, fg=MUTED, font=F(9, True)).pack(side='left', padx=(0, 8))
            two_way = record.get('legs', 1) == 2
            pick = ttk.Combobox(sel, state='readonly', width=40, values=[
                f"{p['no']}. {p['name']}" + ((' - return' if leg == 'return' else ' - outbound') if two_way else '')
                for p, leg in entries])
            pick.current(0)
            pick.pack(side='left')
            pick.bind('<<ComboboxSelected>>', lambda e: show_pax(pick.current()))
        show_pax(0)

        tk.Frame(f, bg=LINE, height=1).pack(fill='x', pady=12)
        arrow = '⇄' if record.get('legs', 1) == 2 else '→'
        tk.Label(f, text=f"{record.get('from_station')}  {arrow}  {record.get('to_station')}", bg=CARD, fg=INK,
                 font=F(15, True)).pack(anchor='w')
        bits = []
        if 'km' in record:
            bits.append(f"{record['km']:.2f} km")
        if 'ride_minutes' in record:
            bits.append(f"{record['ride_minutes']:.0f} min ride")
        if ('km' in record or 'ride_minutes' in record) and not record.get('distance_verified'):
            bits[-1] += ' (estimated)'
        bits.append(f"{record.get('zones', '')} zone(s)")
        if record.get('depart_at'):
            bits.append(f"departs {record['depart_at'][-5:]}")
        tk.Label(f, text='  ·  '.join(bits), bg=CARD, fg=MUTED, font=F(10)).pack(anchor='w', pady=(2, 0))
        shown = pax[:4]
        for p in shown:
            tk.Label(f, text=f"{p['no']}.  {p['name']}   ·   {p['category']}   ·   {p['fare_cents']:,} cents",
                     bg=CARD, fg=INK, font=F(10), wraplength=600, justify='left').pack(anchor='w', pady=(4, 0))
        if len(pax) > len(shown):
            tk.Label(f, text=f'+ {len(pax) - len(shown)} more passengers (all of them are in the PDF)', bg=CARD,
                     fg=MUTED, font=F(9)).pack(anchor='w', pady=(4, 0))
        if pax and pax[0].get('legacy'):
            tk.Label(f, text='Booked with version 2.0, before names were required.', bg=CARD, fg=WARN_INK,
                     font=F(9, True)).pack(anchor='w', pady=(4, 0))
        pay = core.payment_of(record)
        due = not payments.is_settled(pay, self.environment) or payments.is_simulated(pay)
        tk.Label(f, text=f"Payment: {pay.get('label', '')}  -  {payments.status_text(pay)}", bg=CARD,
                 fg=WARN_INK if due else ACCENT_DARK, font=F(10, True)).pack(anchor='w', pady=(8, 0))
        tk.Label(f, text=f"Total  {core.format_cents(record.get('total_cents', 0))}", bg=CARD, fg=ACCENT_DARK,
                 font=F(18, True)).pack(anchor='w', pady=(2, 8))

        status = tk.Label(f, text=f'A copy was saved to: {saved}' if saved else '', bg=CARD, fg=ACCENT_DARK,
                          font=F(9, True), wraplength=600, justify='left')
        btns = tk.Frame(f, bg=CARD)
        btns.pack(anchor='w')
        win = f.winfo_toplevel()
        RoundButton(btns, 'Download PDF', lambda: self.download_pdf(record, status, win), width=150, height=42,
                    radius=12, font=F(10, True)).pack(side='left', padx=(0, 8))
        RoundButton(btns, 'Print', lambda: self.print_ticket(record, status), width=100, height=42, kind='soft',
                    radius=12, font=F(10, True)).pack(side='left', padx=(0, 8))
        RoundButton(btns, 'Open PDF', lambda: self.open_ticket(record, status), width=110, height=42, kind='ghost',
                    radius=12, font=F(10, True)).pack(side='left', padx=(0, 8))
        if allow_refund_request and cta_tickets.unused_journeys(record)[0]:
            RoundButton(btns, 'Request refund', lambda: self.request_refund(record, status), width=150, height=42,
                        kind='ghost', radius=12, font=F(10, True)).pack(side='left')
        tk.Label(f, text='The PDF has one page per passenger and journey (name, QR code, ticket number) plus a receipt '
                         'page. Print prints the ticket pages only.', bg=CARD, fg=MUTED, font=F(9), wraplength=600,
                 justify='left').pack(anchor='w', pady=(8, 0))
        status.pack(anchor='w', pady=(4, 0))
        return f

    def _ticket_server_url(self) -> str:
        """Return the configured ticket server base URL, or '' if not set."""
        try:
            from cta_storage import load_config
            cfg = load_config(self.folder / 'cta_config.json')
            return (cfg.get('ticket_server', {}) or {}).get('url', '').rstrip('/')
        except Exception:  # noqa: BLE001
            return ''

    def _build_ticket_url_map(self, record: dict, base_url: str) -> dict:
        """Generate web tokens for each ticket in *record* and return {ticket_id: url}."""
        if not base_url or self.store is None:
            return {}
        url_map = {}
        try:
            from cta_ticket_server import issue_web_token, ticket_url as make_url, validate_base_url
            if not validate_base_url(base_url)[0]:
                return {}                                           # a bad URL never goes into a printed QR
            for pax in core.passengers_of(record):
                for key in ('ticket_id', 'return_ticket_id'):
                    tid = pax.get(key, '')
                    if tid:
                        try:
                            token = issue_web_token(self.store.path, tid)
                            url_map[tid] = make_url(base_url, token)
                        except Exception:  # noqa: BLE001
                            pass
        except ImportError:  # noqa: BLE001
            pass
        return url_map

    def _export(self, record: dict, path: Path, status: Optional[tk.Label] = None,
                receipt: bool = False) -> Optional[Path]:
        # receipt=False: ticket PDFs are boarding passes only (no A4 page), whether saved, opened or printed.
        """Write the PDF. Returns the path, or None (and shows why in ``status``) if it failed."""
        try:
            server_url = self._ticket_server_url()
            url_map = self._build_ticket_url_map(record, server_url) if server_url else {}
            core.export_ticket_pdf(record, path, include_receipt=receipt, signer=self.signer,
                                   ticket_url_map=url_map)           # one build; no URL = scanner-only QR
            return path
        except ImportError:  # noqa: BLE001
            msg = 'PDF export needs reportlab:  pip install -r requirements.txt'
        except OSError as exc:
            msg = f'Could not save the PDF ({cta_errors.user_message(exc, "saving a ticket PDF")}).'
        except Exception as exc:  # noqa: BLE001 - reportlab/font problems must not lose the booking; logged with a reference
            incident = cta_errors.report(exc, 'creating a ticket PDF', reference=record.get('reference'))
            msg = f'The ticket PDF could not be created. Your booking is saved. (Reference {incident}.)'
        if status is not None:
            status.config(text=msg, fg=DANGER)
        return None

    def download_pdf(self, record: dict, status: Optional[tk.Label] = None, parent=None) -> Optional[Path]:
        chosen = filedialog.asksaveasfilename(
            parent=parent or self.root, title='Save your ticket', defaultextension='.pdf',
            filetypes=[('PDF file', '*.pdf')], initialdir=str(core.downloads_dir(self.folder)),
            initialfile=f"{record['reference']}.pdf")
        if not chosen:
            return None
        path = self._export(record, Path(chosen), status)
        if path and status is not None:
            status.config(text=f'Saved: {path}', fg=ACCENT_DARK)
        return path

    def print_ticket(self, record: dict, status: Optional[tk.Label] = None):
        path = self._export(record, self.folder / f"{record['reference']}_print.pdf", status, receipt=False)
        if not path:
            return
        ok, msg = core.print_pdf(path)
        if status is not None:
            status.config(text=msg, fg=ACCENT_DARK if ok else DANGER)

    def open_ticket(self, record: dict, status: Optional[tk.Label] = None):
        path = self._export(record, self.folder / f"{record['reference']}.pdf", status)
        if not path:
            return
        ok, msg = core.open_pdf(path)
        if status is not None:
            status.config(text=msg, fg=ACCENT_DARK if ok else DANGER)

    # ------------------------------------------------------------ map page
    def _build_map(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['map'] = page
        card = Card(page, pad=10)
        card.pack(fill='both', expand=True, padx=20, pady=(16, 8))
        self.big_map = NetworkMap(card.body, labels='all', interactive=True, on_click=self._map_click)
        self.big_map.pack(fill='both', expand=True)
        strip = Card(page, pad=12)
        strip.pack(fill='x', padx=20, pady=(0, 16))
        s = strip.body
        self.map_label = tk.Label(s, text='Tap a station on the map', bg=CARD, fg=INK, font=F(12, True))
        self.map_label.pack(side='left')
        RoundButton(s, 'Plan this trip', lambda: self.show_page('plan'), width=140, height=40, radius=12,
                    font=F(10, True)).pack(side='right')
        self.map_to = RoundButton(s, 'Set as TO', lambda: self._map_set('to'), width=110, height=40, radius=12,
                                  kind='soft', font=F(10, True))
        self.map_to.pack(side='right', padx=8)
        self.map_from = RoundButton(s, 'Set as FROM', lambda: self._map_set('from'), width=120, height=40,
                                    radius=12, kind='soft', font=F(10, True))
        self.map_from.pack(side='right')
        leg = tk.Frame(s, bg=CARD)
        leg.pack(side='right', padx=24)
        for z, c in ZONE_COLOR.items():
            tk.Label(leg, text='●', fg=c, bg=CARD, font=F(13)).pack(side='left')
            tk.Label(leg, text=f'{z} ({len(ZONE_NAMES[z])})', fg=MUTED, bg=CARD, font=F(9, True)).pack(side='left', padx=(2, 10))
        self.map_from.set_enabled(False)
        self.map_to.set_enabled(False)

    def _map_click(self, name):
        self.map_sel = name
        self.map_label.config(text=f'{name}  ·  {ZONES[name]} zone')
        self.map_from.set_enabled(True)
        self.map_to.set_enabled(True)
        self.update_marks()

    def _map_set(self, which):
        if not self.map_sel:
            return
        (self.from_pick if which == 'from' else self.to_pick).set(self.map_sel, notify=True)

    def update_marks(self):
        marks = {}
        if self.map_sel:
            marks[self.map_sel] = 'selected'
        if hasattr(self, 'big_map'):
            self.big_map.set_marks(marks)
        if hasattr(self, 'mini'):
            a, b = self.from_pick.get(), self.to_pick.get()
            self.mini.set_marks({**({a: 'from'} if a else {}), **({b: 'to'} if b else {})})

    def _sync_map(self):
        self.big_map.set_route(self.route)

    # ------------------------------------------------------------ ticket lookup
    def _build_tickets(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['tickets'] = page
        card = Card(page, pad=24)
        card.pack(pady=24)
        b = card.body
        tk.Frame(b, width=560, height=1, bg=CARD).pack()
        tk.Label(b, text='My ticket', bg=CARD, fg=INK, font=F(18, True)).pack(anchor='w')
        tk.Label(b, text='Signed-in passengers see their bookings below. Otherwise, enter the booking reference and '
                         'the private recovery code from the receipt.',
                 bg=CARD, fg=MUTED, font=F(10), wraplength=540, justify='left').pack(anchor='w', pady=(2, 10))
        self.my_list = tk.Frame(b, bg=CARD)
        self.my_list.pack(fill='x')
        self.ref_var, self.code_var = tk.StringVar(), tk.StringVar()
        for cap, var, show in (('BOOKING REFERENCE', self.ref_var, ''), ('RECOVERY CODE', self.code_var, '•')):
            lab = tk.Label(b, text=cap, bg=CARD, fg=MUTED, font=F(9, True))
            lab.pack(anchor='w')
            if not hasattr(self, 'form_first'):
                self.form_first = lab
            box = tk.Frame(b, bg=LINE, padx=1, pady=1)
            box.pack(fill='x', pady=(2, 10))
            e = tk.Entry(box, textvariable=var, show=show, relief='flat', font=F(13, True), bg='#F7F9FC', fg=INK,
                         highlightthickness=0)
            e.pack(fill='x', ipady=8)
            e.bind('<Return>', lambda ev: self.lookup())
        RoundButton(b, 'Find ticket', self.lookup, width=200, height=44).pack(anchor='w')
        self.lookup_msg = tk.Label(b, text='', bg=CARD, fg=DANGER, font=F(10, True), wraplength=540, justify='left')
        self.lookup_msg.pack(anchor='w', pady=(8, 0))
        self.lookup_result = tk.Frame(b, bg=CARD)
        self.lookup_result.pack(fill='x')

    def lookup(self):
        for w in self.lookup_result.winfo_children():
            w.destroy()
        now = time.monotonic()
        if now < self._locked_until:
            self.lookup_msg.config(text=f'Too many wrong attempts. Try again in {int(self._locked_until - now)} s.')
            return
        rec = core.find_ticket_with_code(self.history, self.ref_var.get(), self.code_var.get())
        if rec is None:
            self._fails += 1
            if self._fails >= 5:
                self._locked_until, self._fails = now + 30, 0
            self.lookup_msg.config(text='No ticket matches that reference and code. Check both and try again.')
            return
        self._fails = 0
        self.lookup_msg.config(text='')
        self._verified_by_code = True                  # they typed this booking's reference AND its private code
        self.show_found(rec)

    def show_found(self, rec: dict):
        for w in self.lookup_result.winfo_children():
            w.destroy()
        self.my_list.pack_forget()
        tk.Frame(self.lookup_result, bg=LINE, height=1).pack(fill='x', pady=14)
        self.ticket_view(self.lookup_result, rec, allow_refund_request=True).pack(fill='x')

    def refresh_my_tickets(self):
        """Rebuild the "my bookings" list for the signed-in person and clear any ticket shown below it."""
        for w in self.lookup_result.winfo_children():
            w.destroy()
        for w in self.my_list.winfo_children():
            w.destroy()
        self.lookup_msg.config(text='')
        self._verified_by_code = False
        self.my_list.pack(fill='x', before=self.form_first)
        if self.account is None:
            tk.Label(self.my_list, text='Sign in to see your bookings here without typing a recovery code.',
                     bg=CARD, fg=MUTED, font=F(10), wraplength=540, justify='left').pack(anchor='w')
            RoundButton(self.my_list, 'Sign in', self.open_auth, width=140, height=38, kind='soft', radius=12,
                        font=F(10, True)).pack(anchor='w', pady=(6, 12))
            return
        mine = [r for r in self.history if r.get('account_phone') == self.account['phone']][-4:][::-1]
        tk.Label(self.my_list, text=f"YOUR BOOKINGS  ·  {self.account['name']}", bg=CARD, fg=MUTED,
                 font=F(9, True)).pack(anchor='w')
        if not mine:
            tk.Label(self.my_list, text='No bookings yet. Plan a trip and press Book ticket.', bg=CARD, fg=MUTED,
                     font=F(10)).pack(anchor='w', pady=(4, 12))
            self._paint_my_messages()                        # a problem report needs no booking, and neither does its reply
            return
        for r in mine:
            text = f"{r.get('reference', '')}    {r.get('from_station', '')} → {r.get('to_station', '')}    " \
                   f"{r.get('depart_at', '')[:16]}"
            RoundButton(self.my_list, text, lambda r=r: self.show_found(r), width=520, height=38, kind='ghost',
                        radius=10, font=F(10, True)).pack(anchor='w', pady=2)
        tk.Label(self.my_list, text='Latest four bookings. For older ones use the booking reference and recovery code.',
                 bg=CARD, fg=MUTED, font=F(9)).pack(anchor='w', pady=(4, 6))
        self._paint_my_messages()

    def _paint_my_messages(self):
        """Your problem reports and refund requests, with the answers from CTA staff."""
        if self.account is None or not self.can_save():
            return
        try:
            cases = self.store.cases_for_account(self.account['phone'])[:5]
        except StorageError:
            return
        if not cases:
            return
        tk.Frame(self.my_list, bg=LINE, height=1).pack(fill='x', pady=(2, 8))
        tk.Label(self.my_list, text='YOUR MESSAGES', bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w')
        done = {'open': 'Waiting for staff', 'answered': 'Answered', 'approved': 'Approved', 'declined': 'Declined'}
        for case in cases:
            what = (f"Refund request  ·  {case['reference']}" if case['kind'] == 'refund_request'
                    else f"Problem report  ·  {case['topic']}")
            tk.Label(self.my_list, text=f"{what}   -   {done.get(case['status'], case['status'])}", bg=CARD, fg=INK,
                     font=F(10, True), wraplength=540, justify='left').pack(anchor='w', pady=(8, 0))
            for m in case['messages']:
                who = 'CTA staff' if m['from_staff'] else 'You'
                tk.Label(self.my_list, text=f"{who} ({m['at'][:16]}):  {m['body']}", bg=CARD,
                         fg=ACCENT_DARK if m['from_staff'] else MUTED, font=F(9), wraplength=540,
                         justify='left').pack(anchor='w', padx=(12, 0))
        tk.Label(self.my_list, text='Open this page again to see new replies.', bg=CARD, fg=MUTED,
                 font=F(9)).pack(anchor='w', pady=(6, 12))

    # ------------------------------------------------------------ refund request (passenger)
    def request_refund(self, record: dict, status: Optional[tk.Label] = None):
        """Ask staff to cancel the unused journeys of this booking and refund them. The app promises nothing: staff
        decide each request (see docs/PERMISSIONS.md - the refund rules are the owner's to define)."""
        phone = record.get('account_phone', '')
        mine = bool(self.account and self.account.get('phone') == phone)
        if not (mine or getattr(self, '_verified_by_code', False)):
            msg = 'Sign in with the phone number used for this booking to ask for a refund.'
            if status is not None:
                status.config(text=msg, fg=DANGER)
            return
        count, cents = cta_tickets.unused_journeys(record)
        win = tk.Toplevel(self.root)
        win.title('Request a refund')
        win.configure(bg=CARD)
        win.transient(self.root)
        center_window(win, self.root, px(520, self.root), px(430, self.root))
        tk.Label(win, text='Request a refund', bg=CARD, fg=INK, font=F(16, True)).pack(anchor='w', padx=22, pady=(18, 4))
        tk.Label(win, text=(f"Booking {record.get('reference', '')}: {count} journey(s) not used yet"
                            + (f", {core.format_cents(cents)} paid for them." if cents else '.')
                            + "\n\nStaff will look at your request and reply here under My ticket. Nothing is "
                              'cancelled until they approve it. Tell them why you cannot travel.'),
                 bg=CARD, fg=MUTED, font=F(10), wraplength=px(470, self.root), justify='left').pack(anchor='w', padx=22)
        box = tk.Text(win, height=6, width=48, relief='flat', bg='#F7F9FC', font=F(10), highlightthickness=1,
                      highlightbackground=LINE)
        box.pack(fill='x', padx=22, pady=10)
        msg = tk.Label(win, text='', bg=CARD, fg=DANGER, font=F(9, True), wraplength=px(470, self.root), justify='left')
        msg.pack(anchor='w', padx=22)

        def send():
            ok, text = self.submit_refund_request(record, box.get('1.0', 'end'))
            if ok:
                win.destroy()
                if status is not None:
                    status.config(text=text, fg=ACCENT_DARK)
            else:
                msg.config(text=text)
        row = tk.Frame(win, bg=CARD)
        row.pack(anchor='e', padx=22, pady=10)
        RoundButton(row, 'Cancel', win.destroy, width=100, height=38, kind='ghost', radius=12,
                    font=F(10, True)).pack(side='left', padx=(0, 8))
        RoundButton(row, 'Send request', send, width=150, height=38, radius=12, font=F(10, True)).pack(side='left')
        make_modal(win)

    def submit_refund_request(self, record: dict, text: str):
        """(ok, message). The booking's own phone number is the contact; replies appear on My ticket after sign-in."""
        if not self.can_save():
            return False, self.storage_problem or 'Data storage is unavailable, so the request could not be saved.'
        try:
            case = self.store.open_case('refund_request', text, self.clock(), account_phone=record.get('account_phone', ''),
                                        reference=record.get('reference', ''))
        except ValueError as exc:
            return False, str(exc)
        except StorageError as exc:
            return False, f'Your request could not be saved. {exc}'
        return True, f'Your refund request (number {case}) was sent to staff. Sign in and open My ticket to read the reply.'

    # ------------------------------------------------------------ help page
    def _build_admin(self):
        self.admin = AdminPage(self.body, self)
        self.pages['admin'] = self.admin

    def _build_help(self):
        page = tk.Frame(self.body, bg=BG)
        self.pages['help'] = page
        page.grid_columnconfigure((0, 1), weight=1, uniform='h')
        a = Card(page, pad=18)
        a.grid(row=0, column=0, sticky='nsew', padx=(20, 8), pady=(16, 8))
        steps = ['Zone ticket (quickest, no station needed): open Zone ticket, choose the start zone and the',
                 'destination zone, type how many Adults, Children, Seniors and Students, then press Issue voucher.',
                 'Station-to-station trip (optional):',
                 '1  Pick FROM and TO (type a name, or tap a station on the Network map).',
                 '2  Choose when to leave and pick one of the next trains.',
                 '3  Add travellers with + and –. The fare shows at the bottom.',
                 '4  Choose One way or Return, then press Book ticket. Sign in with your mobile number.',
                 '5  Type the name of every passenger. Names are printed on the tickets.',
                 '6  Choose how to pay (card, PayPal, STC Pay, Tabby, Tamara, or cash at the counter).',
                 '7  Download the PDF or press Print. Show the QR code, or read the ticket number, to staff.']
        tk.Label(a.body, text='How to book', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        for t in steps:
            tk.Label(a.body, text=t, bg=CARD, fg=INK, font=F(10), wraplength=px(480, self.root), justify='left').pack(anchor='w', pady=3)

        b = Card(page, pad=18)
        b.grid(row=0, column=1, sticky='nsew', padx=(8, 20), pady=(16, 8))
        tk.Label(b.body, text='Trains, return tickets and paying', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        tk.Label(b.body, text=(
            f'Trains leave every {HEADWAY_MIN} minutes from 05:00 to midnight and run at about {TRAIN_SPEED_KMH:.0f} km/h, '
            f'stopping {DWELL_MIN * 60:.0f} s at each middle station. There is one track route between two stations, '
            'so the alternatives are the later trains.\n\n'
            'A return ticket costs two single journeys and the way back is valid on the day of travel on any train.\n\n'
            'You can pay by card, PayPal, STC Pay, Tabby or Tamara, or choose cash and pay at a CTA counter before '
            'you travel. A counter booking is not valid for boarding until staff have marked it paid.'),
            bg=CARD, fg=INK, font=F(10), wraplength=px(480, self.root), justify='left').pack(anchor='w', pady=(4, 0))

        c = Card(page, pad=18)
        c.grid(row=1, column=0, sticky='nsew', padx=(20, 8), pady=(8, 16))
        tk.Label(c.body, text='Fares per zone', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w', pady=(0, 6))
        for cat in core.CATEGORIES:
            r = tk.Frame(c.body, bg=CARD)
            r.pack(fill='x', pady=2)
            tk.Label(r, text=f'{cat}  ({core.CATEGORY_HINT[cat]})', bg=CARD, fg=INK, font=F(10)).pack(side='left')
            tk.Label(r, text=f'{core.FARE_RATES_CENTS[cat]:,} cents', bg=CARD, fg=INK, font=F(10, True)).pack(side='right')
        tk.Label(c.body, text=f'Policy {core.FARE_POLICY_VERSION}. Amounts are always shown in cents.', bg=CARD,
                 fg=MUTED, font=F(9)).pack(anchor='w', pady=(8, 0))

        d = Card(page, pad=18)
        d.grid(row=1, column=1, sticky='nsew', padx=(8, 20), pady=(8, 16))
        self.report_card = d
        self.staff_note_card = Card(page, pad=18)
        tk.Label(self.staff_note_card.body, text='Complaints and messages', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        tk.Label(self.staff_note_card.body, text=('You are signed in as staff, so the problem form is hidden here. '
                                                  'Passenger complaints, reports and refund requests arrive in the Admin page, in '
                                                  'the "Complaints and messages" and "Refund requests" tabs, where you can '
                                                  'reply or decide.'),
                 bg=CARD, fg=MUTED, font=F(10), wraplength=px(480, self.root), justify='left').pack(anchor='w', pady=(6, 0))
        tk.Label(d.body, text='Report a problem or make a complaint', bg=CARD, fg=INK, font=F(14, True)).pack(anchor='w')
        self.issue_type = ttk.Combobox(d.body, state='readonly', width=30, values=ISSUE_TYPES)
        self.issue_type.current(0)
        self.issue_type.pack(anchor='w', pady=6)
        self.issue_text = tk.Text(d.body, height=4, width=50, relief='flat', bg='#F7F9FC', font=F(10),
                                  highlightthickness=1, highlightbackground=LINE)
        self.issue_text.pack(fill='x')
        self.issue_msg = tk.Label(d.body, text='', bg=CARD, fg=ACCENT_DARK, font=F(9, True))
        RoundButton(d.body, 'Send report', self.send_issue, width=140, height=38, radius=12, kind='soft',
                    font=F(10, True)).pack(anchor='w', pady=8)
        self.issue_msg.pack(anchor='w')

    def _sync_help(self):
        """Staff answer problems in the Admin page; they do not file them, so the form is for passengers only."""
        if not hasattr(self, 'report_card'):
            return
        if self.staff:
            self.report_card.grid_remove()
            self.staff_note_card.grid(row=1, column=1, sticky='nsew', padx=(8, 20), pady=(8, 16))
        else:
            self.staff_note_card.grid_remove()
            self.report_card.grid(row=1, column=1, sticky='nsew', padx=(8, 20), pady=(8, 16))

    def send_issue(self):
        text = self.issue_text.get('1.0', 'end').strip()
        if not text:
            self.issue_msg.config(text='Please describe the problem first.', fg=DANGER)
            return
        if self.issue_type.get() not in ISSUE_TYPES:
            self.issue_msg.config(text='Choose what the problem is about.', fg=DANGER)
            return
        report = {'at': self.clock().isoformat(timespec='seconds'), 'type': self.issue_type.get(), 'text': text[:1000]}
        phone = self.account['phone'] if self.account else ''
        try:
            if self.can_save():
                self.store.open_case('problem', report['text'], self.clock(), topic=report['type'], account_phone=phone)
            else:                                    # append-only, so a damaged database cannot lose the report
                core.log_event(self.folder / 'problem_reports.jsonl', report)
        except ValueError as exc:
            self.issue_msg.config(text=str(exc), fg=DANGER)
            return
        except (OSError, StorageError) as exc:
            self.issue_msg.config(text=f'Could not save report: {exc}', fg=DANGER)
            return
        self.issue_text.delete('1.0', 'end')
        self.issue_msg.config(text='Thank you. Your report was saved for staff to review.' + (
            ' Staff replies appear under My ticket.' if phone else
            ' Sign in before sending next time if you would like a reply.'), fg=ACCENT_DARK)


def launch() -> None:
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):          # not Windows 8.1+: default scaling is fine
            pass
    root = tk.Tk()
    CTAApp(root)
    root.mainloop()


if __name__ == '__main__':
    launch()
