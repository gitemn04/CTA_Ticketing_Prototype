# Schematic routing setup and verification

Date: 25 November 2026
Baseline: 1560b3c

## Configuration
Set routing.provider to schematic in the local cta_config.json.
Preserved other existing settings.
The configuration loader returned:
status=ok, provider=schematic, live=False, explicit=True.

Local configuration contains installation-specific settings and is
excluded from version control.

## Windows GUI check
The application opened without the journey-planning-unavailable badge.
Selecting Centrala to Zord with one adult displayed:
- Route: Centrala -> Yaen -> Quthiel -> Zord.
- Three station-to-station hops.
- Three chargeable zones.
- Fare: 6,315 cents.
- Estimated distance: 4.71 km.
- A notice identifying distance and time as estimates, not live data.

## Scope
This verifies local schematic routing configuration and the displayed
route above. It does not verify live schedules, payments or messaging.
