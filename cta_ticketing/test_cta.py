import unittest

from .config import (
    FARE_RATES_CENTS,
    PASSENGER_CATEGORIES,
    STATIONS_BY_ZONE,
)
from .fares import (
    calculate_chargeable_zones,
    calculate_category_total,
    calculate_fare,
)
from .validation import (
    parse_non_negative_integer,
    parse_repeat_choice,
    parse_zone_choice,
    validate_passenger_group,
)


class CtaUnitTests(unittest.TestCase):
    def test_station_data_has_36_unique_stations(self):
        stations = [
            station
            for names in STATIONS_BY_ZONE.values()
            for station in names
        ]
        self.assertEqual(len(stations), 36)
        self.assertEqual(len(stations), len(set(stations)))

    def test_chargeable_zone_boundaries_and_reverse_direction(self):
        self.assertEqual(calculate_chargeable_zones(1, 1), 1)
        self.assertEqual(calculate_chargeable_zones(1, 2), 2)
        self.assertEqual(calculate_chargeable_zones(1, 3), 3)
        self.assertEqual(calculate_chargeable_zones(3, 1), 3)

    def test_category_total(self):
        self.assertEqual(calculate_category_total(2, 2105, 3), 12630)

    def test_mixed_group_total(self):
        counts = {"Adult": 2, "Child": 1, "Senior": 1, "Student": 1}
        totals, group_size, overall = calculate_fare(
            counts, FARE_RATES_CENTS, 3
        )
        self.assertEqual(
            totals,
            {
                "Adult": 12630,
                "Child": 4230,
                "Senior": 3075,
                "Student": 5250,
            },
        )
        self.assertEqual(group_size, 5)
        self.assertEqual(overall, 25185)

    def test_zero_category_is_valid(self):
        counts = {category: 0 for category in PASSENGER_CATEGORIES}
        counts["Student"] = 1
        validate_passenger_group(counts)
        self.assertEqual(
            calculate_fare(counts, FARE_RATES_CENTS, 1)[2],
            1750,
        )

    def test_empty_group_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_passenger_group(
                {category: 0 for category in PASSENGER_CATEGORIES}
            )

    def test_invalid_counts_are_rejected(self):
        for value in ("", "two", "2.5", "-1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_non_negative_integer(value)

    def test_valid_count_and_zone_inputs_are_normalised(self):
        self.assertEqual(parse_non_negative_integer(" 2 "), 2)
        self.assertEqual(
            parse_zone_choice(" 2 ", {"1", "2", "3"}),
            "2",
        )
        self.assertEqual(parse_repeat_choice(" y "), "Y")

    def test_invalid_zone_and_repeat_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_zone_choice("4", {"1", "2", "3"})
        with self.assertRaises(ValueError):
            parse_repeat_choice("maybe")

    def test_station_distribution_and_wicyt_location(self):
        expected_counts = {
            "Central": 10,
            "Midtown": 12,
            "Downtown": 14,
        }
        actual_counts = {
            zone: len(stations)
            for zone, stations in STATIONS_BY_ZONE.items()
        }
        self.assertEqual(actual_counts, expected_counts)
        self.assertIn("Wicyt", STATIONS_BY_ZONE["Midtown"])

    def test_all_nine_zone_combinations(self):
        # Expected results follow the provisional inclusive-zone rule.
        cases = (
            (1, 1, 1),
            (1, 2, 2),
            (1, 3, 3),
            (2, 1, 2),
            (2, 2, 1),
            (2, 3, 2),
            (3, 1, 3),
            (3, 2, 2),
            (3, 3, 1),
        )
        for start, destination, expected in cases:
            with self.subTest(start=start, destination=destination):
                self.assertEqual(
                    calculate_chargeable_zones(start, destination),
                    expected,
                )

    def test_each_passenger_fare_for_one_two_and_three_zones(self):
        expected_fares = {
            "Adult": (2105, 4210, 6315),
            "Child": (1410, 2820, 4230),
            "Senior": (1025, 2050, 3075),
            "Student": (1750, 3500, 5250),
        }
        for category, expected_values in expected_fares.items():
            for zones, expected in enumerate(expected_values, start=1):
                with self.subTest(category=category, zones=zones):
                    counts = {
                        name: 0 for name in PASSENGER_CATEGORIES
                    }
                    counts[category] = 1
                    totals, group_size, overall = calculate_fare(
                        counts, FARE_RATES_CENTS, zones
                    )
                    self.assertEqual(group_size, 1)
                    self.assertEqual(overall, expected)
                    self.assertEqual(totals[category], expected)
                    self.assertEqual(sum(totals.values()), overall)

    def test_zero_count_is_accepted_and_costs_nothing(self):
        self.assertEqual(parse_non_negative_integer("0"), 0)
        self.assertEqual(parse_non_negative_integer(" 0 "), 0)
        self.assertEqual(calculate_category_total(0, 2105, 3), 0)

    def test_repeat_choices_accept_case_and_surrounding_spaces(self):
        cases = (
            ("Y", "Y"),
            ("y", "Y"),
            (" Y ", "Y"),
            ("N", "N"),
            ("n", "N"),
            (" n ", "N"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(parse_repeat_choice(value), expected)

    def test_additional_invalid_zone_and_repeat_choices(self):
        for value in ("", " ", "0", "-1", "4", "1.5", "Central"):
            with self.subTest(field="zone", value=value):
                with self.assertRaises(ValueError):
                    parse_zone_choice(value, {"1", "2", "3"})

        for value in ("", " ", "0", "1", "2", "yes", "no"):
            with self.subTest(field="repeat", value=value):
                with self.assertRaises(ValueError):
                    parse_repeat_choice(value)


if __name__ == "__main__":
    unittest.main()