"""Small reusable, modern-looking Tkinter widgets (no third-party packages)."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from cta_routing import LINKS, STATIONS, ZONES

# ---- palette ---------------------------------------------------------------
NAVY = '#0B1F3A'
INK = '#14263F'
MUTED = '#6B7A90'
BG = '#F2F5F9'
CARD = '#FFFFFF'
LINE = '#E3E9F1'
ACCENT = '#0F766E'
ACCENT_DARK = '#115E59'
ACCENT_SOFT = '#D9F2EE'
DANGER = '#B42318'
DANGER_SOFT = '#FDECEA'
WARN_SOFT = '#FFF4DB'
WARN_INK = '#7A5200'
ZONE_COLOR = {'Central': '#E5546C', 'Midtown': '#2F80ED', 'Downtown': '#F2A900'}

_FAMILY: Optional[str] = None


_scale_cache: Optional[float] = None


def ui_scale(widget=None) -> float:
    """How much bigger than 96-dpi this screen draws text (1.0 normal, 1.25 for 125% Windows scaling, ...).
    Pixel sizes are multiplied by this so text no longer gets clipped on scaled screens."""
    global _scale_cache
    if _scale_cache is None:
        try:
            raw = float(widget.tk.call('tk', 'scaling')) / (96 / 72)
        except (tk.TclError, ValueError, TypeError):     # unusual display: fall back to normal size
            raw = 1.0
        _scale_cache = min(2.5, max(0.8, raw))
    return _scale_cache


def px(n: float, widget=None) -> int:
    return int(round(n * ui_scale(widget)))


def family() -> str:
    global _FAMILY
    if _FAMILY is None:
        have = set(tkfont.families())
        _FAMILY = next((f for f in ('Segoe UI', 'SF Pro Text', 'Helvetica Neue', 'Inter',
                                    'DejaVu Sans', 'Arial') if f in have), 'TkDefaultFont')
    return _FAMILY


def F(size: int = 10, bold: bool = False) -> Tuple[str, int, str]:
    return (family(), size, 'bold' if bold else 'normal')


def rrect(c: tk.Canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Card(tk.Frame):
    def __init__(self, master, pad=16, **kw):
        super().__init__(master, bg=CARD, highlightthickness=1, highlightbackground=LINE, **kw)
        self.body = tk.Frame(self, bg=CARD)
        self.body.pack(fill='both', expand=True, padx=pad, pady=pad)


class RoundButton(tk.Canvas):
    """Rounded button. kind: primary | ghost | soft | dark | danger."""
    KINDS = {
        'primary': (ACCENT, 'white', ACCENT_DARK),
        'soft': (ACCENT_SOFT, ACCENT_DARK, '#BFE7E1'),
        'ghost': (CARD, INK, BG),
        'dark': (NAVY, 'white', '#1B3558'),
        'danger': (DANGER_SOFT, DANGER, '#F9D3CE'),
    }

    def __init__(self, master, text, command: Callable = None, width=160, height=44,
                 kind='primary', radius=12, font=None, parent_bg=CARD, outline=None):
        width, height, radius = px(width, master), px(height, master), px(radius, master)
        super().__init__(master, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor='hand2')
        self.text, self.command, self.kind, self.radius = text, command, kind, radius
        self.font = font or F(11, True)
        self.outline = outline
        self.enabled = True
        self._hover = False
        self.bind('<Configure>', lambda e: self._draw())
        self.bind('<Enter>', lambda e: self._set_hover(True))
        self.bind('<Leave>', lambda e: self._set_hover(False))
        self.bind('<ButtonRelease-1>', self._click)
        self.bind('<Return>', self._click)
        self.bind('<space>', self._click)
        self.configure(takefocus=1)

    def _set_hover(self, v):
        self._hover = v
        self._draw()

    def _click(self, e=None):
        if self.enabled and self.command:
            self.command()

    def set_text(self, text):
        self.text = text
        self._draw()

    def set_enabled(self, on: bool):
        self.enabled = on
        self.configure(cursor='hand2' if on else 'arrow')
        self._draw()

    def set_kind(self, kind):
        self.kind = kind
        self._draw()

    def _draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self['width']), int(self['height'])
        bg, fg, hov = self.KINDS[self.kind]
        if not self.enabled:
            bg, fg, hov = '#E8EDF3', '#9AA7B8', '#E8EDF3'
        rrect(self, 1, 1, w - 1, h - 1, self.radius, fill=hov if self._hover and self.enabled else bg,
              outline=self.outline or ('' if self.kind != 'ghost' else LINE))
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class ScrollFrame(tk.Frame):
    """A frame whose content scrolls when it is taller than the space it is given (mouse wheel or scroll bar).
    Put your widgets in ``.body``."""

    def __init__(self, master, width: int = 300, bg: str = CARD):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, width=width, height=10, bg=bg, highlightthickness=0)
        self.bar = tk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        for w in (self.canvas, self.body):
            w.bind('<Enter>', self._grab_wheel)
            w.bind('<Leave>', self._release_wheel)

    def _grab_wheel(self, _e=None):
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.canvas.bind_all(seq, self._wheel)

    def _release_wheel(self, _e=None):
        for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            try:
                self.canvas.unbind_all(seq)
            except tk.TclError:
                pass

    def _wheel(self, e):
        up = getattr(e, 'num', 0) == 4 or getattr(e, 'delta', 0) > 0
        self.canvas.yview_scroll(-2 if up else 2, 'units')


class Segmented(tk.Frame):
    def __init__(self, master, options: Sequence[str], command: Callable[[str], None], value=None, parent_bg=CARD):
        super().__init__(master, bg=parent_bg)
        self.command, self.buttons, self.value = command, {}, value or options[0]
        for opt in options:
            b = RoundButton(self, opt, lambda o=opt: self.select(o), width=96, height=36, radius=10,
                            font=F(10, True), parent_bg=parent_bg)
            b.pack(side='left', padx=(0, 6))
            self.buttons[opt] = b
        self._paint()

    def _paint(self):
        for o, b in self.buttons.items():
            b.set_kind('primary' if o == self.value else 'ghost')

    def select(self, opt, notify=True):
        self.value = opt
        self._paint()
        if notify:
            self.command(opt)


class Stepper(tk.Frame):
    def __init__(self, master, maximum: int, command: Callable[[], None], parent_bg=CARD):
        super().__init__(master, bg=parent_bg)
        self.value, self.maximum, self.command = 0, maximum, command
        self.minus = RoundButton(self, '–', lambda: self.add(-1), width=36, height=36, radius=18,
                                 kind='ghost', font=F(14, True), parent_bg=parent_bg)
        self.label = tk.Label(self, text='0', width=3, bg=parent_bg, fg=INK, font=F(14, True))
        self.plus = RoundButton(self, '+', lambda: self.add(1), width=36, height=36, radius=18,
                                kind='soft', font=F(14, True), parent_bg=parent_bg)
        self.minus.pack(side='left')
        self.label.pack(side='left', padx=4)
        self.plus.pack(side='left')
        self.set(0, notify=False)

    def add(self, d):
        self.set(self.value + d)

    def set(self, v, notify=True):
        self.value = max(0, min(self.maximum, v))
        self.label.config(text=str(self.value))
        self.minus.set_enabled(self.value > 0)
        self.plus.set_enabled(self.value < self.maximum)
        if notify:
            self.command()


class StationPicker(tk.Frame):
    """Type-ahead station field with a floating suggestion list."""

    def __init__(self, master, caption: str, on_change: Callable[[], None], parent_bg=CARD):
        super().__init__(master, bg=parent_bg)
        self.on_change = on_change
        self.names = sorted(STATIONS)
        self.value: Optional[str] = None
        self._items: List[str] = []
        tk.Label(self, text=caption, bg=parent_bg, fg=MUTED, font=F(9, True)).pack(anchor='w')
        self.var = tk.StringVar()
        self.box = tk.Frame(self, bg=LINE, padx=1, pady=1)
        self.box.pack(fill='x', pady=(2, 0))
        self.entry = tk.Entry(self.box, textvariable=self.var, relief='flat', font=F(13, True),
                              bg='#F7F9FC', fg=INK, insertbackground=INK, highlightthickness=0)
        self.entry.pack(fill='x', ipady=8, padx=0)
        self.dot = tk.Label(self, text='', bg=parent_bg, fg=MUTED, font=F(9))
        self.dot.pack(anchor='w', pady=(2, 0))
        self.list = tk.Listbox(self.winfo_toplevel(), font=F(11), activestyle='none', relief='flat',
                               highlightthickness=1, highlightbackground=LINE, exportselection=False,
                               selectbackground=ACCENT_SOFT, selectforeground=INK, bg='white', fg=INK)
        self.entry.bind('<FocusIn>', lambda e: self._open())
        self.entry.bind('<KeyRelease>', self._typed)
        self.entry.bind('<Down>', lambda e: self._move(1))
        self.entry.bind('<Up>', lambda e: self._move(-1))
        self.entry.bind('<Return>', self._enter)
        self.entry.bind('<Escape>', lambda e: self._close())
        self.entry.bind('<FocusOut>', lambda e: self.after(160, self._focus_lost))
        self.list.bind('<ButtonRelease-1>', self._pick)

    # -- public
    def set(self, name: Optional[str], notify=False):
        self.value = name if name in STATIONS else None
        self.var.set(self.value or '')
        self._refresh_hint()
        if notify:
            self.on_change()

    def get(self) -> Optional[str]:
        return self.value

    # -- internals
    def _refresh_hint(self):
        if self.value:
            z = ZONES[self.value]
            self.dot.config(text=f'● {z} zone', fg=ZONE_COLOR[z])
            self.box.config(bg=LINE)
        else:
            txt = self.var.get().strip()
            self.dot.config(text='Not a CTA station' if txt else '', fg=DANGER)
            self.box.config(bg=DANGER if txt else LINE)

    def _filtered(self) -> List[str]:
        t = self.var.get().strip().lower()
        if not t:
            return list(self.names)
        hits = [n for n in self.names if t in n.lower()]
        return sorted(hits, key=lambda n: (not n.lower().startswith(t), n))

    def _open(self):
        self.entry.select_range(0, 'end')
        self._fill()

    def _fill(self):
        self._items = self._filtered()
        self.list.delete(0, 'end')
        for n in self._items:
            self.list.insert('end', f'{n}   ·   {ZONES[n]}')
        if not self._items:
            self._close()
            return
        top = self.winfo_toplevel()
        self.update_idletasks()
        x = self.box.winfo_rootx() - top.winfo_rootx()
        y = self.box.winfo_rooty() - top.winfo_rooty() + self.box.winfo_height() + 2
        rows = min(7, len(self._items))
        self.list.config(height=rows)
        self.list.place(x=x, y=y, width=self.box.winfo_width())
        self.list.lift()
        self.list.selection_clear(0, 'end')
        self.list.selection_set(0)

    def _close(self):
        self.list.place_forget()

    def _focus_lost(self):
        if self.focus_get() is not self.list:
            self._commit()
            self._close()

    def _typed(self, e):
        if e.keysym in ('Up', 'Down', 'Return', 'Escape', 'Tab'):
            return
        self._fill()
        exact = next((n for n in STATIONS if n.lower() == self.var.get().strip().lower()), None)
        old = self.value
        self.value = exact
        self._refresh_hint()
        if exact != old:
            self.on_change()

    def _move(self, d):
        if not self.list.winfo_ismapped():
            self._fill()
        cur = self.list.curselection()
        i = (cur[0] if cur else -1) + d
        i = max(0, min(len(self._items) - 1, i))
        self.list.selection_clear(0, 'end')
        self.list.selection_set(i)
        self.list.see(i)
        return 'break'

    def _enter(self, e=None):
        cur = self.list.curselection()
        if self.list.winfo_ismapped() and cur:
            self._choose(self._items[cur[0]])
        else:
            self._commit()
        return 'break'

    def _pick(self, e):
        i = self.list.nearest(e.y)
        if 0 <= i < len(self._items):
            self._choose(self._items[i])

    def _choose(self, name):
        old = self.value
        self.set(name)
        self._close()
        if name != old:
            self.on_change()

    def _commit(self):
        txt = self.var.get().strip().lower()
        match = next((n for n in STATIONS if n.lower() == txt), None)
        if match is None and txt:
            cand = [n for n in STATIONS if n.lower().startswith(txt)]
            match = cand[0] if len(cand) == 1 else None
        old = self.value
        self.set(match)
        if match != old:
            self.on_change()


class NetworkMap(tk.Canvas):
    """Schematic metro map. Highlights a route; optionally clickable."""

    def __init__(self, master, labels='route', interactive=False, on_click=None, **kw):
        super().__init__(master, bg=kw.pop('bg', CARD), highlightthickness=0, **kw)
        self.labels, self.interactive, self.on_click = labels, interactive, on_click
        self.route = None
        self.marks: Dict[str, str] = {}      # station -> 'from' | 'to' | 'selected'
        self._pos: Dict[str, Tuple[float, float]] = {}
        self.bind('<Configure>', lambda e: self.redraw())
        if interactive:
            self.bind('<Button-1>', self._click)
            self.bind('<Motion>', self._motion)

    def set_route(self, route):
        self.route = route
        self.redraw()

    def set_marks(self, marks: Dict[str, str]):
        self.marks = marks
        self.redraw()

    def _layout(self, w, h, pad_x, pad_top, pad_bottom):
        xs = [p[0] for p in STATIONS.values()]
        ys = [p[1] for p in STATIONS.values()]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        s = min((w - 2 * pad_x) / (x1 - x0), (h - pad_top - pad_bottom) / (y1 - y0))
        ox = (w - (x1 - x0) * s) / 2
        self._pos = {n: (ox + (x - x0) * s, pad_top + (y - y0) * s) for n, (x, y) in STATIONS.items()}
        return s

    def redraw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 60:
            return
        big = self.labels == 'all'
        self._layout(w, h, 40 if big else 26, 34 if big else 22, 26 if big else 14)   # sets self._pos
        chosen = set()
        if self.route:
            chosen = {frozenset((l.a, l.b)) for l in self.route.legs}
        for a, b in LINKS:
            (x1, y1), (x2, y2) = self._pos[a], self._pos[b]
            if frozenset((a, b)) in chosen:
                continue
            self.create_line(x1, y1, x2, y2, fill='#CBD5E1', width=4 if big else 3, capstyle='round')
        for a, b in LINKS:
            if frozenset((a, b)) in chosen:
                (x1, y1), (x2, y2) = self._pos[a], self._pos[b]
                self.create_line(x1, y1, x2, y2, fill='white', width=11 if big else 9, capstyle='round')
                self.create_line(x1, y1, x2, y2, fill=ACCENT, width=6 if big else 5, capstyle='round')
        on_route = set(self.route.stations) if self.route else set()
        for n, (x, y) in self._pos.items():
            mark = self.marks.get(n)
            ends = self.route and n in (self.route.origin, self.route.destination)
            r = 9 if (ends or mark) else (7 if n in on_route else (6 if big else 5))
            ring = INK if (ends or mark) else 'white'
            self.create_oval(x - r - 2, y - r - 2, x + r + 2, y + r + 2, fill='white', outline='')
            self.create_oval(x - r, y - r, x + r, y + r, fill=ZONE_COLOR[ZONES[n]],
                             outline=ring, width=3 if (ends or mark) else 2)
            show = (self.labels == 'all' or n in on_route or n == 'Centrala' or mark)
            if mark in ('from', 'to'):
                tag, col = ('START', ACCENT) if mark == 'from' else ('END', DANGER)
                tw = 8 * len(tag) + 14
                ty = y - r - 34
                rrect(self, x - tw / 2, ty - 10, x + tw / 2, ty + 10, 10, fill=col, outline='')
                self.create_text(x, ty, text=tag, fill='white', font=F(8, True))
            if show:
                bold = ends or mark or n in on_route
                fnt = F(9 if big else 8, bool(bold))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                    self.create_text(x + dx, y - r - 9 + dy, text=n, fill=BG if self.cget('bg') == BG else 'white', font=fnt)
                self.create_text(x, y - r - 9, text=n, fill=INK if bold else MUTED, font=fnt)

    def _nearest(self, x, y, limit=16):
        best, bd = None, limit
        for n, (px, py) in self._pos.items():
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d < bd:
                best, bd = n, d
        return best

    def _motion(self, e):
        self.config(cursor='hand2' if self._nearest(e.x, e.y) else 'arrow')

    def _click(self, e):
        n = self._nearest(e.x, e.y)
        if n and self.on_click:
            self.on_click(n)


class Timeline(tk.Canvas):
    """Vertical stop list with times, distances and zone colours."""
    ROW = 48

    def __init__(self, master, **kw):
        super().__init__(master, bg=CARD, highlightthickness=0, **kw)
        self.rows = None
        self.route = None
        self.bind('<Configure>', lambda e: self.redraw())
        self.bind('<MouseWheel>', lambda e: self.yview_scroll(int(-e.delta / 120), 'units'))
        self.bind('<Button-4>', lambda e: self.yview_scroll(-1, 'units'))
        self.bind('<Button-5>', lambda e: self.yview_scroll(1, 'units'))

    def show(self, route, rows):
        self.route, self.rows = route, rows
        self.redraw()

    def redraw(self):
        self.delete('all')
        if not self.rows:
            self.create_text(max(self.winfo_width(), 260) / 2, 60, text='Your stops and arrival times\nwill appear here',
                             fill=MUTED, font=F(10), justify='center')
            return
        w = max(self.winfo_width(), 260)
        n = len(self.rows)
        x = 26
        for i in range(n - 1):
            self.create_line(x, 30 + i * self.ROW, x, 30 + (i + 1) * self.ROW, fill=ACCENT, width=5, capstyle='round')
        for i, (name, when, km) in enumerate(self.rows):
            y = 30 + i * self.ROW
            end = i in (0, n - 1)
            z = ZONES[name]
            r = 10 if end else 7
            self.create_oval(x - r - 3, y - r - 3, x + r + 3, y + r + 3, fill='white', outline='')
            self.create_oval(x - r, y - r, x + r, y + r, fill=ZONE_COLOR[z], outline=INK if end else 'white', width=3 if end else 2)
            self.create_text(54, y - 8, text=name, anchor='w', fill=INK, font=F(12 if end else 11, end))
            tag = 'Board here' if i == 0 else ('Get off here' if i == n - 1 else f'{z}')
            sub = tag if end else f'{z}  ·  {km:.2f} km from start'
            if end and i == n - 1:
                sub = f'Get off here  ·  {km:.2f} km total'
            self.create_text(54, y + 10, text=sub, anchor='w', fill=MUTED, font=F(9))
            self.create_text(w - 14, y, text=when.strftime('%H:%M'), anchor='e', fill=INK, font=F(12, True))
        self.configure(scrollregion=(0, 0, w, 30 + n * self.ROW))


class Tooltip:
    """Small hover note. Shows after a short delay, hides on leave/click; never raises."""
    def __init__(self, widget, text_or_fn, delay=350):
        self.widget, self.src, self.delay = widget, text_or_fn, delay
        self.tip = None
        self._job = None
        widget.bind('<Enter>', self._enter, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _text(self):
        return self.src() if callable(self.src) else self.src

    def _enter(self, _e=None):
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        self._job = None
        text = self._text()
        if not text or self.tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f'+{x}+{y}')
            tk.Label(self.tip, text=text, bg='#1F2937', fg='#FFFFFF', justify='left', wraplength=300,
                     padx=10, pady=6, font=('Segoe UI', 9)).pack()
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None
