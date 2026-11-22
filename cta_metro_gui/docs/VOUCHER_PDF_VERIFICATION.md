# Voucher and PDF verification

Date: 22 November 2026
Baseline checked: a7e89d8

## Commands
Run from cta_metro_gui:
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k VoucherTests -v
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k VoucherPdfTests -v

## Results
VoucherTests: 5 passed.
VoucherPdfTests: 1 passed after installing pypdf 7.1.1.
Final result: 6 passed, no failures or skips.

The initial PDF check was skipped because a PDF reader dependency was missing.
Installed it using: python -m pip install pypdf
Reran the PDF check successfully.

Verified:
- Log entry contains zones and passenger counts.
- Subtotals equal passenger count times rate times chargeable zones.
- Voucher lists all four passenger categories, including zero counts.
- Voucher number is unique and dated.
- Voucher text contains the required fields checked by the test.
- Generated PDF agrees with the voucher and uses a non-A4 page size.

These automated checks do not establish visual layout quality or physical printing quality.
