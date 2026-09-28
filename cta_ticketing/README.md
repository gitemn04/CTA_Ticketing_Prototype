# CTA Ticket Voucher Prototype

A Python console prototype for the Centrala Transport Authority. It displays station information, calculates fares and produces on-screen travel vouchers.

## Requirements

- Python, tested with version 3.13.14.
- A terminal such as Windows PowerShell.
- Git for the clone method below; alternatively, download a ZIP.
- VS Code is optional.
- No third-party Python packages are required.

Testing has been performed on the developer's Windows PC. Other environments have not yet been verified.

## Download and installation

Open a terminal in the folder where you want to store a new copy:

```powershell
git clone https://github.com/gitemn04/CTA_Ticketing_Prototype.git
cd CTA_Ticketing_Prototype
```

Alternatively, download the repository as a ZIP from GitHub and extract it.

Run commands from the project root: the folder containing the cta_ticketing folder, not from inside cta_ticketing.

Check Python:

```powershell
python --version
```

## Start the application

```powershell
python -m cta_ticketing.main
```

1. Review the station information.
2. Choose the starting zone: 1 = Central, 2 = Midtown, 3 = Downtown.
3. Choose the destination zone.
4. Enter the adult, child, senior and student passenger counts.
5. Review the voucher and total fare.
6. Enter Y for another voucher or N to close.

Lowercase y and n are accepted.

The voucher includes the issue date and time, journey, chargeable zones, passenger breakdown, group size and total fare.

## Validation rules

- Zone choices must match the available menu options.
- Passenger counts must be non-negative whole numbers.
- Enter 0 when no passengers belong to a category.
- Blank, text, decimal and negative passenger entries are rejected.
- A group with zero passengers in every category is rejected.
- Invalid entries produce an error and another opportunity to enter data.
- The repeat prompt accepts only Y or N, ignoring case and surrounding spaces.

Requiring at least one traveller is a prototype validation rule that requires client acceptance.

## Fare rates

Rates per passenger per chargeable zone:

| Category | Rate |
| --- | ---: |
| Adult | 21.05 |
| Child | 14.10 |
| Senior | 10.25 |
| Student | 17.50 |

Rates and fare calculations use integer cents.

Category total = passenger count × rate per zone × chargeable zones.

The overall fare is the sum of all category totals.

## Provisional zone policy

The prototype uses:

```text
abs(start_position - destination_position) + 1
```

Under this assumption:

- Same-zone journeys cost one zone.
- Neighbouring-zone journeys cost two zones.
- Central to Downtown and the reverse cost three zones.

Menu identifiers and configured fare positions have separate roles, even though their current numeric values correspond.

This rule must be confirmed against CTA policy before operational acceptance. It does not calculate routes through a detailed transport network.

## Automated testing

Run from the project root:

```powershell
python -m unittest -v cta_ticketing.test_cta
```

The current suite contains 15 test methods covering station data, zone combinations, passenger fares and input validation.

A successful run ends with "Ran 15 tests" and "OK".

Passing the suite does not prove that every possible input or interaction is correct. Manual testing and genuine user testing are also needed.

## Manual verification examples

| Journey | Passengers | Expected zones | Expected total |
| --- | --- | ---: | ---: |
| Central to Downtown | 2 adults, 1 child, 1 senior, 1 student | 3 | 251.85 |
| Downtown to Central | 2 adults, 1 child, 1 senior, 1 student | 3 | 251.85 |
| Midtown to Midtown | 1 child | 1 | 14.10 |
| Central to Midtown | 1 adult | 2 | 42.10 |
| Central to Central | 1 student | 1 | 17.50 |
| Midtown to Downtown | 1 senior | 2 | 20.50 |

Also check:

- Invalid zone choices.
- Blank, text, decimal and negative passenger counts.
- An all-zero passenger group.
- Recovery after invalid input.
- Invalid repeat choices.
- Repeated transactions with different passenger counts.
- Normal exit using N.

Record actual results and retain screenshots. Do not mark an unexecuted test as passed.

## Project modules

| File | Responsibility |
| --- | --- |
| config.py | Zone configuration, station lists and fare rates |
| fares.py | Fare-calculation functions |
| validation.py | Input-validation helpers |
| interface.py | Console prompts, station display and voucher output |
| main.py | Application entry point and repeat loop |
| test_cta.py | Automated unit tests |

## Station maintenance

Make changes on a separate Git branch and retain the previous working version.

1. Edit STATIONS_BY_ZONE in config.py.
2. Check station spelling, duplicates and zone assignments against an approved map.
3. The current baseline contains 36 stations: Central 10, Midtown 12 and Downtown 14.
4. Wicyt belongs to Midtown.
5. Update affected tests only when an approved requirement changes.
6. Run the complete test suite.
7. Check that the station display remains alphabetical.
8. Review and commit the changes.

Adding or removing a zone requires reviewing configuration, menu prompts, validation messages and fare logic. It is not necessarily a configuration-only change.

## Fare maintenance

1. Obtain approval for the revised rates or charging policy.
2. Edit FARE_RATES_CENTS in config.py using integer cents.
3. Independently calculate the new expected results.
4. Update affected tests and documentation.
5. Run automated tests and representative manual journeys.
6. Review and commit the changes.
7. Verify a fresh copy before handing over the revised version.

A change to the zone-charging policy also requires reviewing calculation logic and its use by the console interface.

## Troubleshooting

### Python is not recognised

Check that Python is installed and accessible from the terminal. Reopen the terminal after installation or configuration changes.

If the Windows Python launcher is available, try:

```powershell
py -m cta_ticketing.main
```

### No module named cta_ticketing

Check that the terminal is in the project root and the cta_ticketing folder is present.

### Attempted relative import with no known parent package

Launch the application as a module:

```powershell
python -m cta_ticketing.main
```

Do not launch main.py directly.

### The application keeps asking for input

Read the error message and enter an allowed value. At the repeat prompt, enter Y or N rather than a number.

### A test fails

Record the failing test, expected result and actual result. Investigate the cause before changing code or expected values. After correcting the cause, rerun the complete suite and relevant manual tests.

### The voucher date or time is incorrect

Check the computer's date, time and timezone settings. The voucher uses the local system clock.

## Support information

When reporting a problem, include:

- Operating system and Python version.
- Git commit identifier.
- Steps and inputs needed to reproduce the problem.
- Expected and actual results.
- Relevant error messages or screenshots.

Record the current version using:

```powershell
python --version
git rev-parse --short HEAD
git status
```

Support ownership and response arrangements must be agreed with the recipient before operational handover. No production support service is currently established.

## Fresh-copy verification

On 28 September 2026, a separate clone named CTA_Handover_Check was downloaded from GitHub on the developer's existing Windows PC.

All 15 automated tests passed in that copy. An interactive journey from Midtown to Downtown for one senior produced two chargeable zones and a total of 20.50. Entering n closed the application normally.

This demonstrates a fresh-copy check on the same PC. It is not evidence of independent user testing or compatibility with another computer.

## Handover acceptance checklist

Before operational acceptance:

- Confirm the CTA fare policy and validation assumptions.
- Provide the source code, README and test evidence.
- Record the delivered Git revision and Python environment.
- Verify installation and execution in the recipient's environment.
- Complete genuine user testing and record feedback.
- Resolve or explicitly accept outstanding defects and limitations.
- Agree maintenance and support responsibilities.
- Obtain acceptance from the intended recipient.

These are acceptance requirements, not a claim that acceptance has already occurred.

## Scope and integration limitations

This is a standalone console prototype, not a production ticketing service.

It does not:

- Process payments.
- Manage customer accounts.
- Save voucher records to a database.
- Integrate with live CTA systems.
- Provide a graphical or web interface.

Vouchers are displayed on screen.

Future integration would require agreed interfaces, security controls, failure handling and additional testing. The current prototype should not be presented as operationally approved.