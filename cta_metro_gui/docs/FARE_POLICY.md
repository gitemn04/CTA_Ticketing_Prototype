# CTA Metro fare policy (CTA-FARES-2)

**Status: UNCONFIRMED.** This describes the rule the app has always used. It has not been confirmed as the official CTA
fare policy. Nothing here was changed by Module 4; the code was only centralised and locked to this rule. Section 6 lists
what the project owner needs to confirm.

## 1. The rule in use

| Item | Current rule |
|---|---|
| Zones | Central = band 1 (10 stations), Midtown = band 2 (12), Downtown = band 3 (14). Membership: `cta_routing.ZONE_NAMES`. |
| Chargeable zones | `|band(start) - band(destination)| + 1`, so 1, 2 or 3. The track the train actually uses is **not** counted. |
| One-way fare | category rate x chargeable zones, per passenger |
| Rates (cents per zone) | Adult 2,105 · Child 1,410 · Senior 1,025 · Student 1,750 |
| Return | two one-way journeys (x 2). No return discount. The way back has the same zones (the rule is symmetric). |
| Group | the sum of every passenger's fare. No group discount, no cap. |
| Rounding | none needed: whole cents multiplied by whole numbers. Rates that are not whole positive cents are rejected. |
| Minimum / maximum | none beyond what the rule gives: one way 1,025 (Senior, 1 zone) to 6,315 (Adult, 3 zones). |
| Not priced | same start and destination, unknown station, station without a zone, pairs with no track. |
| Direction | every link runs both ways; A->B and B->A cost the same. |
| Where it comes from | the original kiosk brief (ATHE Unit 2, Appendix 2), per the comment in the original code. The brief itself is not in the project. |

Every ordered station pair and its one-way fare per passenger type is listed in **`fare_matrix.csv`** (1,260 journeys).

## 2. The two rules disagree on 584 of 1,260 journeys

The network is a tree centred on Centrala, so a trip between two different branches always passes through Central.
The code also contained a second rule, "zones the train passes through" (`route`). It was never used for charging.

* Same branch, or one end in Central: both rules agree.
* Different branches: the route rule charges more in all 584 cases: 1 -> 3 zones (166), 2 -> 3 (290), 1 -> 2 (128).
* Example: Adohad -> Brunad (both Downtown) is **2,105** cents for an adult today; the route rule would make it **6,315**.

`python tools/fare_matrix.py --compare` writes every affected journey, with both fares, to `fare_rule_comparison.csv`.
The app already shows passengers a "heads-up" on these routes saying fares follow the zone-band rule.

## 3. Where fares are calculated and shown

One function calculates every fare: `cta_fares.quote(start, destination, travellers, journeys, route)`.

| Place | What it uses |
|---|---|
| Plan screen: fare bar, "Fare details", zones chip | `quote` / the same zone function |
| Payment screen total | the `quote` total, remembered as the amount shown |
| Booking record (`total_cents`, each passenger's `fare_cents` / `leg_fare_cents`, `zones`) | `make_record`, which prices through `quote` and refuses a different zone count |
| Amount sent to payment | the booking total; issuing stops if it differs from the amount shown or the payment comes back with another amount |
| Ticket screen, PDF tickets and receipt, admin, CSV, statistics, refunds | the values stored on the booking (never recalculated) |

Old helper names (`core.trip_total`, `core.chargeable_zones`, ...) still exist and use the same rule.

## 4. How the rule is protected

* `fare_matrix.csv` is the reviewed artefact. At start-up (and in the tests) the code's fare for every journey must equal
  the matrix. If it does not, or the matrix is missing or damaged, **new bookings are paused** with a clear message;
  viewing tickets and check-in keep working.
* Invalid rates, zones or rule names stop sales the same way (`FarePolicyError`).
* `make_record` refuses a booking whose zones, total or passenger fares differ from the policy.
* The amount charged must equal the booking total. The only allowed change afterwards is a staff cancellation, which
  keeps the original amount (`original_amount_cents`) and is written to the audit log.
* `python cta_recover.py verify` reports any stored booking whose fare does not add up. It never changes the booking.
* Fares are **not** configurable from a settings file. They change only in code, with approval.

## 5. Changing the fare policy (only after written approval)

1. Change the rule or rates in `cta_fares.py` and bump `FARE_POLICY_VERSION` (for example `CTA-FARES-3`).
2. If you were given an official matrix, put it in `fare_matrix.csv`. Otherwise run `python tools/fare_matrix.py --write`
   and have the owner review the file.
3. Set `FARE_POLICY_STATUS = 'approved'`, run the tests, and record who approved it and when.

Bookings already made keep the fare and policy version they were sold under.

## 6. Questions for the project owner

1. Which rule is official: zone-band difference (current), zones the train passes through, or something else? Or can
   you supply the official station-to-station fare matrix?
2. Are the rates (Adult 2,105 · Child 1,410 · Senior 1,025 · Student 1,750 cents per zone) and category ages correct?
3. Is a return exactly two one-way fares, or is there a return discount?
4. Is there a minimum fare, a daily or group cap, or any discount that would need a rounding rule?
5. Is the currency really "cents" of an unnamed unit, or a real currency (payments default to SAR)?
6. Is the zone list in `cta_routing.ZONE_NAMES` correct for every station?
