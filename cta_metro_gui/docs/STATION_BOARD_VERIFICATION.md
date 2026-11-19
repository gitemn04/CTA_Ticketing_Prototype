# Station Board verification

Date: 19 November 2026
Baseline checked: commit 22743ce

## Automated checks

Command run from cta_metro_gui:
python -B -m unittest discover -s tests -p "test_zone_booking.py" -k StationBoardTests -v

Result: 4 tests passed; no failures or skips.

## Windows interface check

Opened CTA Metro and selected Station board.
Confirmed:
- Central: 10 stations.
- Midtown: 12 stations, including Wicyt.
- Downtown: 14 stations.
- All 36 stations visible and listed alphabetically within each zone.
- Header reports 35 track connections.

The optional journey planner remained unconfigured.
This did not prevent the Station Board from opening.
