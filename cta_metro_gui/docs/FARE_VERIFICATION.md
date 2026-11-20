# Zone fare verification

Date: 20 November 2026
Baseline checked: ae55d20

## Commands
Run from cta_metro_gui:
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k ZoneRuleTests -v
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k AssignmentExampleTests -v

## Results
- ZoneRuleTests: 3 passed.
- AssignmentExampleTests: 4 passed.
- Total: 7 passed, no failures or skips.

Coverage:
- Chargeable zones for all nine start/destination zone pairs.
- Assignment passenger rates in cents.
- Same start and destination zone accepted.
- Central to Downtown mixed family example.
- Central to Midtown one-adult example.
- Midtown to Midtown one-child example.
- Equal fares in the reverse direction.

This run verified calculation logic through automated tests.
It did not test payment processing or PDF output.
