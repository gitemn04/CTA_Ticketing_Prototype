# CTA Metro v3.3 — Appendix 2 (zone booking) test report

Date: 5 Nov 2026. Baseline: `CTA_Metro_App_v3_2.zip` (unchanged, kept as delivered). New version: v3.3.

The requirements were taken from the request text (Appendix 2, page 5, as summarised in the request). The brief's own
PDF/Word file was not part of the upload, so the wording of the brief itself was not checked line by line.

## Where the tests ran
| | |
|---|---|
| Automated tests and GUI tests | Linux (cloud sandbox), Python 3.12.3, Tk 8.6, reportlab 4.5.1, pypdf 6.21.4, on a virtual X display (`xvfb-run`) |
| Screen size for screenshots | 1366 x 768 virtual screen. The app window opens at 1280 x 698 there, because it sizes itself from the screen and does not maximise on Linux |
| Windows 10/11 | **Not run.** I had no Windows machine. See section 5 for the exact steps. |
| Real printer, Windows PDF viewer, Windows Save dialog | **Not run.** See section 5. |

The GUI tests below are real: they build the actual `CTAApp` window, press the real buttons in code, and read back the
labels shown on screen. They are not mocked screens. Only `print_pdf` and `open_pdf` were replaced by stubs, so no printer or
PDF viewer was launched. The PDF file itself was really built and read back.

## 1. Results table

| ID | Test | Expected | Actual | Status | Evidence |
|---|---|---|---|---|---|
| T1 | Station board: all stations, grouped, A–Z | 36 stations; Central 10, Midtown 12, Downtown 14; each zone sorted A–Z; same set as the network data | 36 stations; Central 10, Midtown 12, Downtown 14; every column A–Z; set equal to `cta_routing.STATIONS`; the GUI board shows the same lists | **Pass** | `StationBoardTests` (4), `ZoneGuiTests::test_station_board_page_shows_all_stations_grouped_and_sorted`; `evidence/appendix2/02_station_board.png` |
| T2 | Network data preserved, Wicyt in Midtown | 36 stations, 35 connections, no validation problems, Wicyt in Midtown | `network_counts() == (36, 35)`, `validate_network() == []`, Wicyt in Midtown | **Pass** | `test_network_keeps_36_stations_and_35_connections`, `test_wicyt_is_in_midtown` |
| T3 | Central → Downtown, 2 Adults, 1 Child, 1 Senior, 1 Student | 3 zones, 5 travellers, 25,185 cents; subtotals 12,630 / 4,230 / 3,075 / 5,250 | 3 zones, "Total travellers: 5", "Total fare: 25,185 cents", subtotals 12,630 / 4,230 / 3,075 / 5,250 cents (logic and screen) | **Pass** | `test_central_to_downtown_family`, GUI `test_issued_voucher_screen_shows_every_field_and_matches_the_pdf`; `06_voucher_central_to_downtown.png`, `evidence_log.txt` |
| T4 | Midtown → Midtown, 1 Child | 1 zone, 1,410 cents | 1 zone, 1,410 cents | **Pass** | `test_midtown_to_midtown_one_child`, GUI `test_all_zero_group_is_refused_then_corrected`; `09_voucher_midtown_to_midtown.png` |
| T5 | Central → Midtown, 1 Adult | 2 zones, 4,210 cents | 2 zones, 4,210 cents | **Pass** | `test_central_to_midtown_one_adult`, GUI `test_zero_categories_are_shown_on_the_voucher`; `10_voucher_central_to_midtown.png` |
| T6 | Missing zone refused, then corrected | No voucher, stays on the form, clear message; booking proceeds after correction | Returned `None`, page stayed `zone`, message "Choose a start zone.", voucher log empty; after choosing Central, voucher issued (25,185) and logged once | **Pass** | `test_missing_zones_are_refused`, GUI `test_missing_zone_is_refused_without_a_voucher_then_corrected`; `03_refused_missing_zone.png` |
| T7 | All-zero group refused, then corrected | No voucher, clear message; proceeds after correction | Message "Enter at least one traveller: all four counts are 0.", no voucher, nothing logged; after Child = 1, voucher 1 zone / 1,410 | **Pass** | `test_all_zero_group_is_refused_with_a_clear_message`, GUI `test_all_zero_group_is_refused_then_corrected`; `04_refused_all_zero.png` |
| T8 | Invalid counts | Blank, `-1`, `1.5`, `two`, `100`, an Arabic-Indic digit and `True` are refused | All refused, each with a message naming the passenger type (6 subtests + 1) | **Pass** | `test_bad_counts_are_refused` |
| T9 | Same start and destination zone | Allowed, 1 chargeable zone | Downtown → Downtown, 1 Adult = 1 zone, 2,105 cents | **Pass** | `test_same_start_and_destination_zone_is_allowed`, `test_chargeable_zones_for_all_nine_pairs` (all 9 pairs) |
| T10 | Voucher shows every required field | Issue date/time, start and destination zone, chargeable zones, all 4 types (zeros too) with count, rate and subtotal, total travellers, total fare | Each field read back from the on-screen labels; Children/Seniors/Students show 0 and "0 cents" when unused | **Pass** | GUI `test_issued_voucher_screen_shows_every_field_and_matches_the_pdf`, `test_zero_categories_are_shown_on_the_voucher`; `06_…png`, `10_…png` |
| T11 | PDF / print copy agrees with the screen | Same number, date, zones, rates, subtotals, travellers and total; not A4 | **Print voucher** built the PDF. Its text, extracted with pypdf, contains every on-screen value. Page size 540 x 288 pt (7.5 x 4 in) | **Pass** | `VoucherPdfTests`, GUI `…matches_the_pdf`; `07_voucher_central_to_downtown_PDF.png` |
| T12 | Issue another voucher resets | Fresh form: no zones, all counts 0, no message; a new voucher can be issued | Page `zone`, zones (None, None), counts all "0", message empty; a second voucher (1,410) issued | **Pass** | GUI `test_issue_another_voucher_resets_the_form`; `08_after_issue_another_fresh_form.png` |
| T13 | Exit closes the app | The window closes and `mainloop` ends normally | The Tk root was destroyed (`winfo_exists` raises `TclError`); evidence run printed "mainloop returned" | **Pass** (Linux) | GUI `test_exit_closes_the_app`; `evidence_log.txt` last line; `12_voucher_before_exit.png` |
| T14 | Station planner still works | Centrala → Quthiel, 1 Adult: 2 zones, 4,210 cents; reviewed fare matrix unchanged | Route ends at Quthiel, bottom bar "4,210 cents"; `check_against_matrix()` passes; Yaen → Zord family still 25,185 | **Pass** | `StationPlannerRegressionTests`, GUI `test_station_planner_still_works_in_the_app`; `11_station_planner_still_works.png` |
| T15 | Owner's edge case Zord → Marend (station planner only) | Kept separate from the assignment examples; current behaviour recorded | Route Zord–Quthiel–Yaen–Centrala–Bylyn–Agralle–Docia–Marend passes through Downtown, Midtown, Central, Midtown, Downtown. The planner charges 1 zone (2,105 cents, 1 Adult); the zones passed through count 3 | **Pass** (characterisation) | `test_own_edge_case_zord_to_marend_is_kept_separate` |
| T16 | Zone booking without `cta_config.json` | Works even though the planner reports "not configured" | Production mode, no config: `routing_problem` set, zone voucher 25,185 issued | **Pass** | `test_zone_booking_works_even_when_journey_planning_is_not_configured` |
| T17 | No regressions in the existing app | Same results as the v3.2 baseline | Baseline: 646 passed, 1 skipped. v3.3: the same 646 passed, the same 1 skipped (PyMuPDF not installed), 0 failed | **Pass** | `raw_baseline_tests_v3_2.txt`, `raw_final_tests_v3_3.txt` |
| T18 | Layout at 1366 x 768 | Issue voucher, Clear and the header fully visible | First version: **failed** (defect D1, D2). After the fix: everything is visible | **Pass after fix** | `DEFECT1_before_fix_buttons_cut_off_1366x768.png` vs `05_corrected_central_to_downtown.png` |
| W1–W6 | Windows checks | see section 5 | — | **Not run** | — |

All new automated tests: **37 passed, 6 subtests passed, 0 failed** (`tests/test_zone_booking.py`).

## 2. Defects found and fixed

**D1 — Issue voucher and Clear cut off on a 1366 x 768 screen (real defect in my first version)**
- Original failure: the evidence run at 1366 x 768 (window 1280 x 698) showed the zone form taller than the window. The
  **Issue voucher** and **Clear** buttons were cut in half at the bottom edge
  (`DEFECT1_before_fix_buttons_cut_off_1366x768.png`). A passenger on a small laptop might not find the button.
  The automated tests still passed, because they press the button in code. Only the screenshot showed the problem.
- Change: the form now sits in a scrolling panel (`ScrollFrame`, the same widget the station planner already uses),
  and the vertical spacing is tighter (`cta_zone_ui.py`).
- Retest: the evidence run was repeated. Both buttons are fully visible (`05_corrected_central_to_downtown.png`), and all
  37 zone tests plus the full suite pass.

**D2 — Header label clipped once six menu tabs were added**
- Original failure: the yellow "TESTING MODE - no real money" label showed only "ODE - no" (same screenshot as D1).
  It appears only in development or testing mode.
- Change: narrower menu tabs (`NAV_WIDTH` in `cta_app.py`), smaller gaps, and a Sign-in button 20 px narrower.
- Retest: the label is shown in full (`02_station_board.png`, `06_…png`).

Not a defect, but worth knowing: in production mode without `cta_config.json`, the **station planner** says
"Journey planning is not configured". This is unchanged v3.2 behaviour. My first GUI planner test hit it, and I
corrected the test to run in testing mode, like the other suites. The zone booking does not need routing (T16).

## 3. Commands and real output

Run from the project folder (on Linux these were run under `xvfb-run -a`, with `CTA_DATA_DIR` and `HOME` pointed at
empty temporary folders):

```
python -m compileall -q .
compileall: OK

python -m pytest tests/test_zone_booking.py -v
==================== 37 passed, 6 subtests passed in 8.69s =====================

python -m unittest discover -s tests -p "test_zone_booking.py" -v
----------------------------------------------------------------------
Ran 37 tests in 8.779s

OK

python -m pip check
No broken requirements found.

python -m ruff check .
Found 36 errors.        <- identical 36 findings in the v3.2 baseline; none in the new or changed files
```

Full suite, one test file per run (`tests/test_*.py`), baseline vs. new:

| File | v3.2 baseline | v3.3 |
|---|---|---|
| test_all.py | 151 passed | 151 passed |
| test_auth.py | 33 passed | 33 passed |
| test_complaints_and_ui.py | 9 passed | 9 passed |
| test_fares.py | 25 passed | 25 passed |
| test_fixes.py | 27 passed | 27 passed |
| test_integration.py | 13 passed | 13 passed |
| test_pdf_and_sms_verification.py | 50 passed | 50 passed |
| test_round2.py | 31 passed | 31 passed |
| test_routing.py | 22 passed | 22 passed |
| test_startup_path.py | 12 passed | 12 passed |
| test_support.py | 39 passed, 1 skipped | 39 passed, 1 skipped |
| test_tickets.py | 77 passed | 77 passed |
| test_v3.py | 46 passed | 46 passed |
| test_v3_bug_verification.py | 58 passed | 58 passed |
| test_v3_integration.py | 30 passed | 30 passed |
| test_validation.py | 23 passed | 23 passed |
| **test_zone_booking.py (new)** | — | **37 passed** |
| **Total** | **646 passed, 1 skipped** | **683 passed, 1 skipped, 0 failed** |

The skip is `tests/test_support.py:392: PyMuPDF not installed to render the PDF page`. It is the same in both runs.
The raw outputs are in `docs/evidence/appendix2/raw_*.txt`.

The GUI evidence run: `xvfb-run -a -s "-screen 0 1366x768x24" python tools/appendix2_evidence.py docs/evidence/appendix2`.
Its log (`evidence_log.txt`):

```
home page = zone; menu = ['Zone ticket', 'Station board', 'Plan trip', 'Network map', 'My ticket', 'Help']
board counts = {'Central': 10, 'Midtown': 12, 'Downtown': 14}
missing start zone -> voucher=None, page=zone, message="Choose a start zone."
all-zero group -> voucher=None, page=zone, message="Enter at least one traveller: all four counts are 0."
Central->Downtown 2/1/1/1 -> zones=3, Total travellers: 5, Total fare: 25,185 cents, subtotals=['12,630 cents', '4,230 cents', '3,075 cents', '5,250 cents']
print -> Print copy created: ZV-20261105-135210-E80F.pdf
Issue another -> page=zone, zones=(None, None), counts={'Adult': '0', 'Child': '0', 'Senior': '0', 'Student': '0'}
Midtown->Midtown 1 child -> zones=1, Total fare: 1,410 cents
Central->Midtown 1 adult -> zones=2, Total fare: 4,210 cents
Station planner Centrala->Quthiel 1 adult -> route ends Quthiel, bar "4,210 cents"
Exit -> window closed, mainloop ended normally
```

## 4. Changed files
| File | Change |
|---|---|
| `cta_zone_booking.py` | **New.** Station board, the zone rule, count validation, and the voucher (no GUI code) |
| `cta_zone_ui.py` | **New.** The Zone ticket, Station board and Issued voucher screens; Issue another voucher; Exit |
| `cta_voucher_pdf.py` | **New.** Voucher PDF (7.5 x 4 in), used by Print, Save PDF and Open PDF |
| `tests/test_zone_booking.py` | **New.** 37 tests (logic, PDF, GUI, planner regression, edge case) |
| `tools/appendix2_evidence.py` | **New.** Script that drives the app and takes the evidence screenshots |
| `docs/APPENDIX2_TEST_REPORT.md`, `docs/evidence/appendix2/` | **New.** This report, the screenshots and the raw outputs |
| `cta_app.py` | Changed: new menu (Zone ticket first), home page = Zone ticket, voucher page guard, narrower tabs, Help text |
| `README.md` | Changed: Appendix 2 section, design assumption, worked examples, Zord → Marend note, Windows run steps; corrected the outdated "A4 page" ticket line |
| `CHANGELOG.md` | Changed: v3.3 entry |

No other file changed. The station data (`cta_routing.py`), the fares (`cta_fares.py`, `fare_matrix.csv`) and every
existing test are untouched.

## 5. Not run — exact steps to capture on Windows

Before you start: unzip to `Downloads\CTA_Metro_App_v3_3`, open PowerShell in `CTA_Metro_App_v3_3\CTA_Metro_App_v3`, and run
`python -m pip install -r requirements.txt -r requirements-dev.txt`. Take each screenshot with **Win + Shift + S** →
*Window* snip, and paste it into your evidence document.

| ID | What | Steps | Expected / capture |
|---|---|---|---|
| W1 | Tests on Windows | `python -m pytest tests\test_zone_booking.py -v` | The last line shows `37 passed`. Capture the terminal |
| W2 | App starts on Zone ticket | `python cta_app.py` (or double-click `launch.bat`) | The window opens maximised on **Zone ticket**, and **Issue voucher** is visible without scrolling. Capture it |
| W3 | Full flow on Windows | Press Issue voucher with no zone (capture the red message) → Central / Downtown with all counts 0 → Issue (capture) → Adults 2, Children 1, Seniors 1, Students 1 → Issue | The voucher shows 3 zones, 5 travellers and 25,185 cents. Capture it |
| W4 | Save PDF dialog | On the voucher press **Save PDF**, then save to Downloads | The Windows Save dialog opens with the name `ZV-….pdf`, and the voucher shows "Saved: …". Open the file: same totals as the screen. Capture both side by side |
| W5 | Print to a real printer | Press **Print voucher** | Windows sends the PDF to your default printer. Photograph the printed voucher next to the screen. If nothing prints, press **Open PDF** and then Ctrl+P |
| W6 | Open PDF | Press **Open PDF** | The voucher opens in your default PDF viewer. Capture it |
| W7 | Issue another / Exit | Press **Issue another voucher** (capture the empty form), issue the Midtown → Midtown / 1 Child voucher (1,410), then press **Exit** | The form is reset, then the window closes and the PowerShell prompt returns with no error text. Capture the terminal after Exit |
| W8 | Display scaling | Settings → Display → Scale 125 % and 150 %, then repeat W2 | Nothing is cut off. If the form is taller than the window, a scroll bar appears and the mouse wheel scrolls it |

## 6. Known limits
- The zone voucher does not take payment or sign the passenger in. It issues a fare voucher, as in the brief. The
  station planner keeps its existing sign-in, payment and named-ticket flow.
- Vouchers are recorded in `zone_vouchers.jsonl` (zones, counts and totals only). They are not stored in `cta.db` and are
  not shown in the admin portal.
- Zone-only input does not identify a route, so the zone rule is a documented design assumption (README).
