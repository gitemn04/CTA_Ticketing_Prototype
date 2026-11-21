# Input validation and recovery verification

Date: 21 November 2026
Baseline checked: bc7b521

## Commands
Run from cta_metro_gui:
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k ValidationTests -v
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k test_missing_zone_is_refused_without_a_voucher_then_corrected -v

## Results
ValidationTests: 5 passed.
GUI recovery test: 1 passed.
Total: 6 passed, no failures or skips.

Verified:
- Booking proceeds after invalid input is corrected.
- An all-zero passenger group is refused with a clear message.
- Invalid passenger counts are refused.
- Missing zones are refused.
- Spaces around a valid number are accepted.
- The GUI refuses a missing zone without issuing a voucher, then proceeds after correction.

These checks cover zone-ticket input validation and recovery.
They do not verify payment processing or message delivery.
