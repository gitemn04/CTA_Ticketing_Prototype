"""Modal dialogs (Tkinter, no third-party packages): passenger sign-in, staff sign-in, payment, passenger names.

All the rules (phone format, names, codes, passwords, payments) live in cta_accounts.py, cta_sms.py,
cta_payments.py and cta_core.py and are unit-tested there; these classes only collect input and show messages.
"""
from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import cta_accounts as acct
import cta_core as core
import cta_payments as payments
import cta_testpay as testpay
from cta_logos import load_logo
from cta_sms import OnScreenCodes
from cta_widgets import (ACCENT, ACCENT_DARK, ACCENT_SOFT, BG, CARD, DANGER, INK, LINE, MUTED, WARN_INK,
                         WARN_SOFT, Card, F, RoundButton, ScrollFrame, Tooltip, px)

RESEND_SECONDS = 20
STAFF_MAX_FAILS = 5
STAFF_LOCK_SECONDS = 30


def center_window(win: tk.Toplevel, root: tk.Misc, w: int, h: int) -> None:
    """Centre ``win`` over ``root``; sizes are in 96-dpi pixels and scaled for the screen, never bigger than it."""
    root.update_idletasks()
    w, h = px(w, win), px(h, win)
    w = min(w, max(360, win.winfo_screenwidth() - 60))
    h = min(h, max(360, win.winfo_screenheight() - 100))
    x = root.winfo_rootx() + (root.winfo_width() - w) // 2
    y = root.winfo_rooty() + (root.winfo_height() - h) // 2
    win.geometry(f'{w}x{h}+{max(0, x)}+{max(0, y)}')


def make_modal(win: tk.Toplevel) -> None:
    win.update_idletasks()
    try:
        win.grab_set()
    except tk.TclError:
        pass


def entry_field(parent, caption: str, var: tk.StringVar, show: str = '', font_size: int = 13) -> tk.Entry:
    tk.Label(parent, text=caption, bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w')
    box = tk.Frame(parent, bg=LINE, padx=1, pady=1)
    box.pack(fill='x', pady=(2, 4))
    e = tk.Entry(box, textvariable=var, show=show, relief='flat', font=F(font_size, True), bg='#F7F9FC', fg=INK,
                 highlightthickness=0, insertbackground=INK)
    e.pack(fill='x', ipady=8)
    return e


class AuthDialog:
    """Passengers: phone number -> (new number: name) -> verification code.
    Staff: 'Staff sign-in' button -> username + password (first time on a computer: create the admin account)."""

    def __init__(self, root: tk.Misc, accounts: List[dict], accounts_path: Path, clock: Callable[[], datetime],
                 on_success: Callable[[dict], None], intro: str = '', sms=None,
                 sms_template_sid: str = '',
                 staff: Optional[List[dict]] = None, staff_path: Optional[Path] = None,
                 on_staff: Optional[Callable[[dict], None]] = None, staff_auth=None, device: str = '',
                 audit: Optional[Callable[[str, bool, str], None]] = None):
        self.root, self.accounts, self.path, self.clock, self.on_success = root, accounts, accounts_path, clock, on_success
        self.sms = sms or OnScreenCodes()
        self.sms_template_sid = sms_template_sid  # Twilio Custom Verification Template SID ('' = use default)
        self.staff, self.staff_path, self.on_staff = staff, staff_path, on_staff
        self.staff_auth, self.device, self.audit = staff_auth, device, audit   # service-layer sign-in when given
        self.intro = intro
        self.phone = ''
        self.existing: Optional[dict] = None
        self.new_name = ''
        self._sent_once = False
        self._next_send = 0.0
        self._staff_fails, self._staff_locked_until = 0, 0.0
        self.win = tk.Toplevel(root)
        self.win.title('Sign in')
        self.win.configure(bg=BG)
        self.win.transient(root)
        self.card = Card(self.win, pad=22)
        self.card.pack(fill='both', expand=True, padx=16, pady=16)
        self.phone_var, self.name_var, self.code_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.user_var, self.pw_var, self.pw2_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        center_window(self.win, root, 520, 600)
        self._phone_step()
        make_modal(self.win)

    # ---------------------------------------------------------------- helpers
    def _reset(self) -> tk.Frame:
        for w in self.card.body.winfo_children():
            w.destroy()
        return self.card.body

    def _title(self, b, title: str, sub: str = ''):
        tk.Label(b, text=title, bg=CARD, fg=INK, font=F(16, True)).pack(anchor='w')
        if sub:
            tk.Label(b, text=sub, bg=CARD, fg=MUTED, font=F(10), wraplength=px(420, self.win), justify='left'
                     ).pack(anchor='w', pady=(4, 12))

    def _error_label(self, b) -> tk.Label:
        lab = tk.Label(b, text='', bg=CARD, fg=DANGER, font=F(10, True), wraplength=px(420, self.win), justify='left')
        lab.pack(anchor='w', pady=(4, 8))
        return lab

    # ---------------------------------------------------------------- passenger: phone
    def _phone_step(self):
        b = self._reset()
        self._title(b, 'Sign in or sign up',
                    self.intro or 'Your mobile number keeps your tickets together. We will send you a verification code.')
        e = entry_field(b, 'MOBILE NUMBER', self.phone_var)
        tk.Label(b, text=acct.PHONE_HINT, bg=CARD, fg=MUTED, font=F(9), wraplength=px(420, self.win),
                 justify='left').pack(anchor='w')
        self.err = self._error_label(b)
        RoundButton(b, 'Continue', self._phone_next, width=420, height=46).pack(anchor='w')
        if self.staff is not None and self.staff_path is not None and self.on_staff is not None:
            RoundButton(b, 'Staff / admin sign-in', self._staff_step, width=420, height=40, kind='ghost'
                        ).pack(anchor='w', pady=(14, 0))
        e.bind('<Return>', lambda ev: self._phone_next())
        e.focus_set()

    def _phone_next(self):
        phone, err = acct.validate_phone(self.phone_var.get())
        if err:
            self.err.config(text=err)
            return
        self.phone = phone or ''
        self.existing = acct.find_account(self.accounts, self.phone)
        if self.existing:
            self._send_code()
        else:
            self._name_step()

    # ---------------------------------------------------------------- passenger: name (new numbers only)
    def _name_step(self):
        b = self._reset()
        self._title(b, 'Create your account',
                    f'{acct.mask_phone(self.phone)} is new to CTA. What name should we put on your account? '
                    'You can still enter different names for the other passengers.')
        e = entry_field(b, 'YOUR FULL NAME', self.name_var)
        self.err = self._error_label(b)
        row = tk.Frame(b, bg=CARD)
        row.pack(anchor='w')
        RoundButton(row, 'Back', self._phone_step, width=100, height=46, kind='ghost').pack(side='left', padx=(0, 10))
        RoundButton(row, 'Send code', self._name_next, width=310, height=46).pack(side='left')
        e.bind('<Return>', lambda ev: self._name_next())
        e.focus_set()

    def _name_next(self):
        name, err = acct.validate_person_name(self.name_var.get(), 'your')
        if err:
            self.err.config(text=err)
            return
        self.new_name = name
        self._send_code()

    # ---------------------------------------------------------------- passenger: verification code
    def _send_code(self):
        wait = self._next_send - time.monotonic()
        if self._sent_once and wait > 0:
            self.err.config(text=f'Please wait {int(wait) + 1} s before asking for another code.')
            return
        # Build start() kwargs.
        # When a personalised Twilio template is configured, include the passenger's name
        # so the message reads "Hi {name}, your CTA verification code is {code}…".
        # The template must be pre-approved in the Twilio Console before use.
        # Saudi phones are already in E.164 by the time validate_phone() stores self.phone.
        passenger_name = (self.existing or {}).get('name') or self.new_name or ''
        start_kwargs: dict = {}
        if self.sms_template_sid and passenger_name:
            start_kwargs['template_sid'] = self.sms_template_sid
            start_kwargs['template_vars'] = {'name': passenger_name}
        ok, msg = self.sms.start(self.phone, self.clock(), **start_kwargs)
        if not ok:
            self.err.config(text=msg)
            return
        self._sent_once = True
        self._next_send = time.monotonic() + RESEND_SECONDS
        self._code_step()

    def _code_step(self):
        b = self._reset()
        who = f'Welcome back, {self.existing["name"]}. ' if self.existing else ''
        if self.sms.shows_code:
            sub = f'{who}Enter the {acct.OtpSession.LENGTH}-digit code shown below. It works for 5 minutes.'
        else:
            sub = (f'{who}We sent a {acct.OtpSession.LENGTH}-digit code by text message to '
                   f'{acct.mask_phone(self.phone)}.')
        self._title(b, 'Enter your code', sub)
        if self.sms.shows_code:
            box = tk.Frame(b, bg=WARN_SOFT)
            box.pack(fill='x', pady=(0, 10))
            tk.Label(box, text='Text messages are not set up on this computer, so the code is shown here.',
                     bg=WARN_SOFT, fg=WARN_INK, font=F(9, True), padx=10, pady=6, wraplength=px(400, self.win),
                     justify='left').pack(anchor='w')
            tk.Label(box, text=f'Your code:  {self.sms.code_for(self.phone)}', bg=WARN_SOFT, fg=WARN_INK,
                     font=F(16, True), padx=10).pack(anchor='w', pady=(0, 8))
        self.code_var.set('')
        e = entry_field(b, 'VERIFICATION CODE', self.code_var)
        self.err = self._error_label(b)
        row = tk.Frame(b, bg=CARD)
        row.pack(anchor='w')
        RoundButton(row, 'Back', self._phone_step, width=100, height=46, kind='ghost').pack(side='left', padx=(0, 10))
        RoundButton(row, 'Verify', self._verify, width=310, height=46).pack(side='left')
        RoundButton(b, 'Send a new code', self._send_code, width=420, height=38, kind='soft').pack(anchor='w', pady=(10, 0))
        e.bind('<Return>', lambda ev: self._verify())
        e.focus_set()

    def _verify(self):
        now = self.clock()
        ok, msg = self.sms.check(self.phone, self.code_var.get(), now)
        if self.audit:                                      # outcome only: never the code
            try:
                self.audit(self.phone, ok, '' if ok else msg[:60])
            except Exception as exc:  # noqa: BLE001 - an audit failure must not stop a sign-in; it is logged instead
                import cta_errors
                cta_errors.report(exc, 'auditing a passenger sign-in')
        if not ok:
            self.err.config(text=msg)
            return
        created = False
        account: Optional[dict] = None
        try:
            if self.existing:
                account = self.existing
                acct.touch_login(account, now)
            else:
                account = acct.create_account(self.accounts, self.phone, self.new_name, now)
                created = True
            acct.save_accounts(self.path, self.accounts)
        except (OSError, ValueError) as exc:
            if created and account in self.accounts:
                self.accounts.remove(account)
            self.err.config(text=f'Could not save your account: {exc}')
            return
        self.win.destroy()
        self.on_success(account)

    # ---------------------------------------------------------------- staff / admin
    def _staff_step(self):
        first_run = self.staff_auth.needs_bootstrap() if self.staff_auth else not self.staff
        b = self._reset()
        self._title(b, 'Create the admin account' if first_run else 'Staff sign-in',
                    'No admin account exists on this computer yet. Choose a username and a password; you will use '
                    'them to open the admin portal.' if first_run else
                    'For CTA staff only. Passengers sign in with their phone number.')
        self.user_var.set('')
        self.pw_var.set('')
        self.pw2_var.set('')
        u = entry_field(b, 'USERNAME', self.user_var)
        pw = entry_field(b, 'PASSWORD', self.pw_var, show='•')
        if first_run:
            pw2 = entry_field(b, 'REPEAT PASSWORD', self.pw2_var, show='•')
            tk.Label(b, text=f'At least {acct.PASSWORD_MIN} characters with a letter and a digit.', bg=CARD, fg=MUTED,
                     font=F(9)).pack(anchor='w')
            pw2.bind('<Return>', lambda ev: self._staff_next())
        pw.bind('<Return>', lambda ev: self._staff_next())
        self.err = self._error_label(b)
        row = tk.Frame(b, bg=CARD)
        row.pack(anchor='w')
        RoundButton(row, 'Back', self._phone_step, width=100, height=46, kind='ghost').pack(side='left', padx=(0, 10))
        RoundButton(row, 'Create admin' if first_run else 'Sign in', self._staff_next, width=310, height=46
                    ).pack(side='left')
        u.focus_set()

    def _staff_next(self):
        now = self.clock()
        if self.staff_auth is not None:                     # the database service decides (lockout shared by all windows)
            self._staff_next_service(now)
            return
        if self.staff is None or self.staff_path is None or self.on_staff is None:
            self.err.config(text='Staff sign-in is not available here.')    # an explicit check (asserts vanish under -O)
            return
        if not self.staff:                                   # first run: create the admin
            if self.pw_var.get() != self.pw2_var.get():
                self.err.config(text='The two passwords are not the same.')
                return
            try:
                rec = acct.create_staff(self.staff, self.user_var.get(), self.pw_var.get(), now)
                acct.save_staff(self.staff_path, self.staff)
            except ValueError as exc:
                self.err.config(text=str(exc))
                return
            except OSError as exc:
                self.staff.clear()
                self.err.config(text=f'Could not save the admin account: {exc}')
                return
            self.win.destroy()
            self.on_staff(rec)
            return
        wait = self._staff_locked_until - time.monotonic()
        if wait > 0:
            self.err.config(text=f'Too many wrong attempts. Wait {int(wait) + 1} s.')
            return
        rec = acct.authenticate_staff(self.staff, self.user_var.get(), self.pw_var.get())
        if rec is None:
            self._staff_fails += 1
            if self._staff_fails >= STAFF_MAX_FAILS:
                self._staff_fails = 0
                self._staff_locked_until = time.monotonic() + STAFF_LOCK_SECONDS
                self.err.config(text=f'Too many wrong attempts. Wait {STAFF_LOCK_SECONDS} s.')
            else:
                self.err.config(text='Wrong username or password.')
            return
        self.win.destroy()
        self.on_staff(rec)


    def _staff_next_service(self, now: datetime):
        from cta_auth import AuthError, PermissionDenied
        try:
            if self.staff_auth.needs_bootstrap():
                if self.pw_var.get() != self.pw2_var.get():
                    self.err.config(text='The two passwords are not the same.')
                    return
                principal = self.staff_auth.bootstrap_admin(self.user_var.get(), self.pw_var.get(), now, self.device)
            else:
                principal = self.staff_auth.login(self.user_var.get(), self.pw_var.get(), now, self.device)
        except (AuthError, PermissionDenied, ValueError) as exc:
            self.err.config(text=str(exc))
            self.pw_var.set('')
            return
        except OSError as exc:
            self.err.config(text=f'Could not sign in: {exc}')
            return
        self.pw_var.set('')
        self.pw2_var.set('')
        self.win.destroy()
        self.on_staff(principal)


class PaymentDialog:
    """Choose how to pay before any ticket is issued: big picture tiles, no reading needed."""

    def __init__(self, root: tk.Misc, total_cents: int, legs: int, on_done: Callable[..., None],
                 online_unavailable: str = '', test_mode: bool = False):
        self.root, self.total, self.on_done = root, total_cents, on_done
        self.test_mode = test_mode                      # True: online methods ask for made-up details (TestPaymentDialog)
        self.online_unavailable = online_unavailable   # non-empty: only cash can be chosen, and this says why
        self.selected: Optional[str] = None
        self._images: list = []                      # keep references so Tk does not drop the pictures
        self.win = tk.Toplevel(root)
        self.win.title('Payment')
        self.win.configure(bg=BG)
        self.win.transient(root)

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill='x', padx=22, pady=(18, 6))
        tk.Label(head, text='How would you like to pay?', bg=BG, fg=INK, font=F(16, True)).pack(anchor='w')
        tk.Label(head, text=f"Total  {core.format_cents(total_cents)}" + ('   ·   return ticket' if legs == 2 else ''),
                 bg=BG, fg=ACCENT_DARK, font=F(13, True)).pack(anchor='w', pady=(4, 0))
        if online_unavailable:
            tk.Label(head, text=online_unavailable, bg=WARN_SOFT, fg=WARN_INK, font=F(10, True), padx=10, pady=6,
                     wraplength=px(520, self.win), justify='left').pack(anchor='w', fill='x', pady=(8, 0))

        foot = tk.Frame(self.win, bg=BG)
        foot.pack(side='bottom', fill='x', padx=22, pady=(6, 18))
        tk.Label(foot, text='Card details are never typed into, or stored by, this app.', bg=BG, fg=MUTED,
                 font=F(9)).pack(anchor='w', pady=(0, 8))
        RoundButton(foot, 'Back', self._close, width=100, height=48, kind='ghost', parent_bg=BG).pack(side='left')
        self.confirm = RoundButton(foot, 'Tap a payment method', self._confirm, width=340, height=48, parent_bg=BG)
        self.confirm.pack(side='right')
        self.confirm.set_enabled(False)

        area = ScrollFrame(self.win, width=px(520, self.win), bg=BG)
        area.pack(fill='both', expand=True, padx=(22, 10), pady=6)
        grid = area.body
        grid.grid_columnconfigure((0, 1), weight=1, uniform='pay')
        self.tiles = {}
        for i, (mid, title, desc, kind) in enumerate(payments.methods()):
            wide = (kind == 'counter')
            tile = tk.Frame(grid, bg=CARD, highlightthickness=3, highlightbackground=LINE, cursor='hand2')
            tile.grid(row=i // 2 + (1 if wide and i % 2 else 0), column=0 if wide else i % 2,
                      columnspan=2 if wide else 1, sticky='nsew', padx=5, pady=5)
            parts = []
            logo = load_logo(mid, self.win)
            if logo is not None:
                self._images.append(logo)
                parts.append(tk.Label(tile, image=logo, bg=CARD))
            else:
                parts.append(tk.Label(tile, text=title, bg=CARD, fg=INK, font=F(14, True), pady=14))
            parts[0].pack(pady=(10, 4))
            parts.append(tk.Label(tile, text=title if logo is not None else desc, bg=CARD, fg=INK,
                                  font=F(10, True)))
            parts[1].pack()
            parts.append(tk.Label(tile, text=desc, bg=CARD, fg=MUTED, font=F(8)))
            if logo is not None:
                parts[2].pack(pady=(0, 10))
            if online_unavailable and kind != 'counter':
                for w in [tile] + parts:
                    w.config(cursor='arrow')
                for w in parts[1:]:
                    w.config(fg=MUTED)
                parts[-1].config(text='Not available')
                parts[-1].pack(pady=(0, 10))
            for w in [tile] + parts:
                w.bind('<Button-1>', lambda e, m=mid: self._select(m))
                w.bind('<Enter>', lambda e, m=mid: self._hover(m, True), add='+')
                w.bind('<Leave>', lambda e, m=mid: self._hover(m, False), add='+')
            if online_unavailable and kind != 'counter':
                Tooltip(tile, 'Online payment is not set up on this device yet. '
                              'Choose "Pay at the counter" to reserve this booking and pay at a CTA counter.')
                for w in parts:
                    Tooltip(w, 'Online payment is not set up on this device yet. '
                               'Choose "Pay at the counter" to reserve this booking and pay at a CTA counter.')
            self.tiles[mid] = (tile, parts)
        center_window(self.win, root, 600, 780)
        make_modal(self.win)

    def available(self, mid: str) -> bool:
        return not self.online_unavailable or payments.METHOD_KIND.get(mid) == 'counter'

    def _hover(self, mid: str, on: bool):
        """Highlight an available, unselected tile under the pointer."""
        if mid == self.selected or not self.available(mid):
            return
        tile, parts = self.tiles[mid]
        try:
            tile.config(highlightbackground=ACCENT if on else LINE, bg='#EEF6F5' if on else CARD)
            for w in parts:
                w.config(bg='#EEF6F5' if on else CARD)
        except tk.TclError:
            pass

    def _select(self, mid: str):
        if not self.available(mid):
            return
        self.selected = mid
        for m, (tile, parts) in self.tiles.items():
            on = (m == mid)
            tile.config(highlightbackground=ACCENT if on else LINE, bg=ACCENT_SOFT if on else CARD)
            for w in parts:
                w.config(bg=ACCENT_SOFT if on else CARD)
        counter = payments.METHOD_KIND[mid] == 'counter'
        self.confirm.set_text('Reserve  -  pay at the counter' if counter
                              else f'Pay {core.format_cents(self.total)} with {payments.METHOD_TITLE[mid]}')
        self.confirm.set_enabled(True)

    def _close(self):
        self.win.destroy()

    def _confirm(self):
        if not self.selected:
            return
        mid = self.selected
        if not self.available(mid):
            return
        if self.test_mode and payments.METHOD_KIND[mid] != 'counter':
            # Made-up details + a pretend bank. This window stays open (and the booking un-made) until accepted.
            TestPaymentDialog(self.win, mid, self.total, lambda auth: self._accepted(mid, auth))
            return
        self.win.destroy()
        self.on_done(mid)

    def _accepted(self, mid: str, auth) -> None:
        self.win.destroy()
        self.on_done(mid, auth)


class TestPaymentDialog:
    """Test-mode payment form: type made-up details, they are checked for shape, then a pretend bank checks the
    balance. Nothing is charged and only a short description (\"Visa ending 4242\") is ever kept."""
    __test__ = False                                     # not a pytest test class

    CHECK_MS, DONE_MS = 700, 1000

    def __init__(self, parent: tk.Misc, method: str, total_cents: int, on_approved: Callable[[object], None],
                 clock: Callable[[], datetime] = datetime.now):
        self.method, self.total, self.on_approved, self.clock = method, total_cents, on_approved, clock
        self.busy = False
        self.vars: dict = {}
        self.err: dict = {}
        self.win = tk.Toplevel(parent)
        self.win.title(f'Test payment - {payments.METHOD_TITLE[method]}')
        self.win.configure(bg=BG)
        self.win.transient(parent)
        self.win.protocol('WM_DELETE_WINDOW', self._close)
        self.win.bind('<Escape>', lambda e: self._close())

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill='x', padx=22, pady=(16, 4))
        tk.Label(head, text=payments.METHOD_TITLE[method], bg=BG, fg=INK, font=F(16, True)).pack(anchor='w')
        tk.Label(head, text=f'Total  {core.format_cents(total_cents)}', bg=BG, fg=ACCENT_DARK,
                 font=F(13, True)).pack(anchor='w', pady=(2, 0))
        tk.Label(head, text='TEST MODE: type any made-up details. Nothing is charged, and the number, expiry and '
                            'CVV are never saved.', bg=WARN_SOFT, fg=WARN_INK, font=F(10, True), padx=10, pady=6,
                 wraplength=px(440, self.win), justify='left').pack(anchor='w', fill='x', pady=(8, 0))

        foot = tk.Frame(self.win, bg=BG)                 # packed first at the bottom: always visible
        foot.pack(side='bottom', fill='x', padx=22, pady=(6, 16))
        self.status = tk.Label(self.win, text='', bg=BG, fg=MUTED, font=F(11, True), wraplength=px(460, self.win),
                               justify='left')
        self.status.pack(side='bottom', anchor='w', padx=22)
        card = Card(self.win, pad=12)
        card.pack(fill='x', padx=22, pady=8)
        self.first_entry = None
        for fld in testpay.fields_for(method):
            var = tk.StringVar()
            self.vars[fld.key] = var
            tk.Label(card.body, text=fld.label.upper(), bg=CARD, fg=MUTED, font=F(9, True)).pack(anchor='w')
            box = tk.Frame(card.body, bg=LINE, padx=1, pady=1)
            box.pack(anchor='w', pady=(2, 0))
            e = tk.Entry(box, textvariable=var, width=fld.width, relief='flat', font=F(13, True), bg='#F7F9FC',
                         fg=INK, highlightthickness=0, insertbackground=INK, show='\u2022' if fld.secret else '')
            e.pack(ipady=6)
            e.bind('<Return>', lambda ev: self._submit())
            self.first_entry = self.first_entry or e
            hint = tk.Label(card.body, text=fld.hint, bg=CARD, fg=MUTED, font=F(8))
            hint.pack(anchor='w')
            self.err[fld.key] = tk.Label(card.body, text='', bg=CARD, fg=DANGER, font=F(9, True),
                                         wraplength=px(420, self.win), justify='left')
            self.err[fld.key].pack(anchor='w', pady=(0, 2))
            if fld.key == 'number':
                var.trace_add('write', lambda *_a, v=var: self._reformat(v, testpay.format_card_number))
            elif fld.key == 'expiry':
                var.trace_add('write', lambda *_a, v=var: self._reformat(v, testpay.format_expiry))
        tk.Label(card.body, text=testpay.balance_rule(method), bg=CARD, fg=MUTED, font=F(9),
                 wraplength=px(440, self.win), justify='left').pack(anchor='w', pady=(4, 0))

        self.back = RoundButton(foot, 'Back', self._close, width=100, height=48, kind='ghost', parent_bg=BG)
        self.back.pack(side='left')
        self.pay_btn = RoundButton(foot, f'Pay {core.format_cents(total_cents)}', self._submit, width=300, height=48,
                                   parent_bg=BG)
        self.pay_btn.pack(side='right')
        center_window(self.win, parent, 540, 760)
        make_modal(self.win)
        if self.first_entry is not None:
            self.first_entry.focus_set()

    @staticmethod
    def _reformat(var: tk.StringVar, fn) -> None:
        old = var.get()
        new = fn(old)
        if new != old:
            var.set(new)

    def values(self) -> dict:
        return {k: v.get() for k, v in self.vars.items()}

    def _submit(self, delay: Optional[int] = None) -> None:
        """Check the shape of the details, then ask the pretend bank (after a short 'checking' pause)."""
        if self.busy:
            return
        for lab in self.err.values():
            lab.config(text='')
        errors = testpay.validate(self.method, self.values(), self.clock())
        if errors:
            for key, why in errors.items():
                if key in self.err:
                    self.err[key].config(text=why)
            self.status.config(text='Please fix the highlighted details.', fg=DANGER)
            return
        self.busy = True
        self.pay_btn.set_enabled(False)
        self.back.set_enabled(False)
        self.status.config(text='Checking with the bank (test)...', fg=MUTED)
        self.win.after(self.CHECK_MS if delay is None else delay, self._finish)

    def _finish(self) -> None:
        try:
            auth = testpay.authorize(self.method, self.values(), self.total, self.clock())
        except Exception:  # noqa: BLE001 - a form bug must never look like an approved payment
            auth = testpay.TestAuth(False, testpay.INVALID, 'The test payment could not be checked.')
        self.busy = False
        if auth.approved:
            self.status.config(text='\u2713  Payment accepted', fg=ACCENT_DARK)
            for v in self.vars.values():
                v.set('')                                        # nothing sensitive lingers in the form
            self.win.after(self.DONE_MS, lambda: self._approved(auth))
            return
        self.status.config(text=auth.message, fg=DANGER)
        self.pay_btn.set_enabled(True)
        self.back.set_enabled(True)

    def _approved(self, auth) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.on_approved(auth)

    def _close(self) -> None:
        if self.busy:
            return
        for v in self.vars.values():
            v.set('')
        self.win.destroy()


class PassengerDialog:
    """One name box per traveller. Names are checked here and again when the ticket is built."""

    def __init__(self, root: tk.Misc, slots: Sequence[Tuple[int, str]], first_name: str,
                 on_done: Callable[[List[str]], None], prefill: Optional[Sequence[str]] = None):
        self.root, self.slots, self.on_done = root, list(slots), on_done
        self.prefill = list(prefill) if prefill and len(prefill) == len(self.slots) else None
        self.win = tk.Toplevel(root)
        self.win.title('Passenger names')
        self.win.configure(bg=BG)
        self.win.transient(root)
        self.win.protocol('WM_DELETE_WINDOW', self._close)

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill='x', padx=20, pady=(16, 6))
        tk.Label(head, text='Who is travelling?', bg=BG, fg=INK, font=F(16, True)).pack(anchor='w')
        tk.Label(head, text='Type each passenger\u2019s name exactly as it should be printed on their ticket. '
                            'A name cannot be changed after the ticket is issued.',
                 bg=BG, fg=MUTED, font=F(10), wraplength=px(480, self.win), justify='left').pack(anchor='w', pady=(4, 0))

        foot = tk.Frame(self.win, bg=BG)
        foot.pack(side='bottom', fill='x', padx=20, pady=(6, 16))
        self.msg = tk.Label(foot, text='', bg=BG, fg=DANGER, font=F(10, True))
        self.msg.pack(anchor='w', pady=(0, 6))
        RoundButton(foot, 'Back', self._close, width=100, height=46, kind='ghost', parent_bg=BG).pack(side='left')
        RoundButton(foot, 'Continue to payment', self._submit, width=280, height=46, parent_bg=BG).pack(side='right')

        wrap = tk.Frame(self.win, bg=BG)
        wrap.pack(fill='both', expand=True, padx=(20, 12), pady=6)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        inner = tk.Frame(self.canvas, bg=BG)
        win_id = self.canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(win_id, width=e.width))
        if len(self.slots) > 4:
            sb = tk.Scrollbar(wrap, orient='vertical', command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)

        self.vars: List[tk.StringVar] = []
        self.entries: List[tk.Entry] = []
        self.err_labels: List[tk.Label] = []
        for i, (no, cat) in enumerate(self.slots):
            card = Card(inner, pad=12)
            card.pack(fill='x', pady=5, padx=(0, 6))
            r = card.body
            tk.Label(r, text=f'Passenger {no}', bg=CARD, fg=INK, font=F(11, True)).pack(side='top', anchor='w')
            tk.Label(r, text=f'{cat}  \u00b7  {core.CATEGORY_HINT[cat]}', bg=CARD, fg=MUTED, font=F(9)
                     ).pack(side='top', anchor='w', pady=(0, 4))
            var = tk.StringVar(value=self.prefill[i] if self.prefill else (first_name if i == 0 else ''))
            box = tk.Frame(r, bg=LINE, padx=1, pady=1)
            box.pack(fill='x')
            e = tk.Entry(box, textvariable=var, relief='flat', font=F(13, True), bg='#F7F9FC', fg=INK,
                         highlightthickness=0, insertbackground=INK)
            e.pack(fill='x', ipady=7)
            e.bind('<Return>', lambda ev, k=i: self._next(k))
            err = tk.Label(r, text='', bg=CARD, fg=DANGER, font=F(9, True), wraplength=px(440, self.win), justify='left')
            err.pack(anchor='w')
            self.vars.append(var)
            self.entries.append(e)
            self.err_labels.append(err)

        center_window(self.win, root, 560, 260 + 112 * min(len(self.slots), 4))
        self.win.bind_all('<MouseWheel>', self._wheel)
        self.win.bind_all('<Button-4>', self._wheel)
        self.win.bind_all('<Button-5>', self._wheel)
        make_modal(self.win)
        self.entries[0].focus_set()
        self.entries[0].select_range(0, 'end')

    def _wheel(self, e):
        up = getattr(e, 'num', 0) == 4 or getattr(e, 'delta', 0) > 0
        self.canvas.yview_scroll(-1 if up else 1, 'units')

    def _next(self, i: int):
        if i + 1 < len(self.entries):
            self.entries[i + 1].focus_set()
        else:
            self._submit()

    def _close(self):
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            try:
                self.win.unbind_all(seq)
            except tk.TclError:
                pass
        self.win.destroy()

    def _submit(self):
        names, errors = core.validate_passenger_names(self.slots, [v.get() for v in self.vars])
        for i, (no, _cat) in enumerate(self.slots):
            self.err_labels[i].config(text=errors.get(no, ''))
        if errors:
            bad = [i for i, (no, _c) in enumerate(self.slots) if no in errors]
            if bad:
                self.entries[bad[0]].focus_set()
            self.msg.config(text=f'Please check {len(bad)} name{"" if len(bad) == 1 else "s"} above.')
            return
        self._close()
        self.on_done(names)
