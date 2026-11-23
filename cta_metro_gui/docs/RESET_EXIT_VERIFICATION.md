# Reset and exit verification

Date: 23 November 2026
Baseline checked: 03e8ce3

## Automated checks
Run from cta_metro_gui:
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k test_issue_another_voucher_resets_the_form -v
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k test_exit_closes_the_app -v

Result: 2 tests passed; no failures or skips.

Verified:
- Issuing another voucher resets the form.
- Exit closes the app.

## Open check
Manual window resizing and display scaling were not completed.
No pass is claimed for those checks.
