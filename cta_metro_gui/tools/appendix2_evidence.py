"""Drive the real app through the Appendix 2 flow and save screenshots + evidence_log.txt.

Usage (from the project folder):  python tools/appendix2_evidence.py docs/evidence/appendix2
Linux without a screen: xvfb-run -a -s "-screen 0 1366x768x24" python tools/appendix2_evidence.py OUT_DIR
Needs the scrot screenshot tool (Linux). Print/Open PDF are replaced by stubs so no printer or viewer is used.
"""
import os, sys, subprocess, time, tempfile, tkinter as tk
sys.path.insert(0, os.getcwd())
os.environ['CTA_ENV'] = 'testing'
os.environ.setdefault('CTA_TICKET_KEY', '5e' * 32)
from datetime import datetime
from pathlib import Path
import cta_app, cta_zone_ui
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
LOG = []
def log(s): LOG.append(s); print(s, flush=True)
cta_zone_ui.core.print_pdf = lambda p: (True, f'Print copy created: {Path(p).name}')
cta_zone_ui.core.open_pdf = lambda p: (True, f'Opened {Path(p).name}')
root = tk.Tk(); root.geometry('1366x768+0+0')
folder = tempfile.mkdtemp()
app = cta_app.CTAApp(root, clock=lambda: datetime(2026, 11, 5, 13, 52, 10), folder=folder, environment='testing')
N = [0]
def shot(name):
    for _ in range(6): root.update(); time.sleep(0.05)
    N[0] += 1
    fn = f'{N[0]:02d}_{name}.png'
    subprocess.run(['scrot', '-o', str(OUT / fn)], check=True)
    log(f'  screenshot: {fn}')
def fill(a, b, *n):
    app.zone_from.select(a); app.zone_to.select(b)
    for c, v in zip(('Adult', 'Child', 'Senior', 'Student'), n): app.zone_count_vars[c].set(str(v))
def labels(): return {k: w.cget('text') for k, w in app.voucher_labels.items()}
def go():
    for t in [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]: t.destroy()
    log(f'home page = {app.current_page}; menu = {[b.text for b in app.nav.values()][:6]}')
    shot('zone_home_empty')
    app.show_page('board'); log(f'board counts = { {z: len(v) for z, v in app.board_cells.items()} }'); shot('station_board')
    app.show_page('zone')
    fill(None, 'Downtown', 2, 1, 1, 1); r = app.issue_zone_voucher()
    log(f'missing start zone -> voucher={r}, page={app.current_page}, message="{app.zone_err.cget("text")}"'); shot('refused_missing_zone')
    fill('Central', 'Downtown', 0, 0, 0, 0); r = app.issue_zone_voucher()
    log(f'all-zero group -> voucher={r}, page={app.current_page}, message="{app.zone_err.cget("text")}"'); shot('refused_all_zero')
    fill('Central', 'Downtown', 2, 1, 1, 1); shot('corrected_central_to_downtown')
    v = app.issue_zone_voucher(); L = labels()
    log(f'Central->Downtown 2/1/1/1 -> zones={L["zones"]}, {L["travellers"]}, {L["total"]}, subtotals='
        f'{[L[c + ".subtotal"] for c in ("Adult", "Child", "Senior", "Student")]}')
    shot('voucher_central_to_downtown')
    app.print_voucher(); log(f'print -> {app.voucher_status.cget("text")}')
    pdf = app.voucher_pdf_path(v)
    import pypdfium2 as pdfium
    pdfium.PdfDocument(str(pdf))[0].render(scale=2.5).to_pil().save(OUT / f'{N[0] + 1:02d}_voucher_central_to_downtown_PDF.png'); N[0] += 1
    log(f'  screenshot: {N[0]:02d}_voucher_central_to_downtown_PDF.png  (page {pdfium.PdfDocument(str(pdf))[0].get_size()} pt)')
    app.issue_another_btn._click()
    log(f'Issue another -> page={app.current_page}, zones=({app.zone_from.value}, {app.zone_to.value}), counts={app.zone_counts_raw()}')
    shot('after_issue_another_fresh_form')
    fill('Midtown', 'Midtown', 0, 1, 0, 0); app.issue_zone_voucher(); L = labels()
    log(f'Midtown->Midtown 1 child -> zones={L["zones"]}, {L["total"]}'); shot('voucher_midtown_to_midtown')
    app.issue_another(); fill('Central', 'Midtown', 1, 0, 0, 0); app.issue_zone_voucher(); L = labels()
    log(f'Central->Midtown 1 adult -> zones={L["zones"]}, {L["total"]}'); shot('voucher_central_to_midtown')
    app.show_page('plan'); app.from_pick.set('Centrala'); app.to_pick.set('Quthiel'); app.search(); app.steppers['Adult'].set(1)
    log(f'Station planner Centrala->Quthiel 1 adult -> route ends {app.route.stations[-1]}, bar "{app.bar_total.cget("text")}"'); shot('station_planner_still_works')
    app.show_page('voucher'); shot('voucher_before_exit')
    app.exit_btn._click()
    try:
        root.winfo_exists(); log('Exit -> window still open (FAIL)')
    except tk.TclError:
        log('Exit -> window closed, mainloop ended normally')
    (OUT / 'evidence_log.txt').write_text('\n'.join(LOG) + '\n', encoding='utf-8')
root.after(1500, go)
root.mainloop()
print('mainloop returned')
