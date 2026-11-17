"""Payment badges (PNG pictures). Looks for assets/logos/<method id>.png; falls back to None so the caller can
show the method name instead. Replace the PNGs with the official logos any time (see tools/make_logos.py)."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Optional

from cta_widgets import ui_scale

LOGO_DIR = Path(__file__).resolve().parent / 'assets' / 'logos'


def load_logo(method_id: str, widget) -> Optional[tk.PhotoImage]:
    path = LOGO_DIR / f'{method_id}.png'
    if not path.is_file():
        return None
    try:
        img = tk.PhotoImage(file=str(path), master=widget)
        if ui_scale(widget) < 1.4:               # files are 300x100; show them at 150x50 on normal screens
            img = img.subsample(2, 2)
        return img
    except tk.TclError:                          # damaged or unsupported picture: the tile shows the name instead
        return None
