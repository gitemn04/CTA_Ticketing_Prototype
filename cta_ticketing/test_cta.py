import unittest

from .config import FARE_RATES_CENTS, PASSENGER_CATEGORIES, STATIONS_BY_ZONE
from .fares import calculate_chargeable_zones, calculate_category_total, calculate_fare
from .validation import (
    parse_non_negative_integer,
    parse_repeat_choice,
    parse_zone_choice,
    validate_passenger_group,
)


class CtaUnitTests(unittest.TestCase):
    def test_station_data_has_36_unique_stations(self):
        stations = [station for names in STATIONS_BY_ZONE.values() for station in names]
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
        totals, group_size, overall = calculate_fare(counts, FARE_RATES_CENTS, 3)
        self.assertEqual(totals, {"Adult": 12630, "Child": 4230, "Senior": 3075, "Student": 5250})
        self.assertEqual(group_size, 5)
        self.assertEqual(overall, 25185)

    def test_zero_category_is_valid(self):
        counts = {category: 0 for category in PASSENGER_CATEGORIES}
        counts["Student"] = 1
        validate_passenger_group(counts)
        self.assertEqual(calculate_fare(counts, FARE_RATES_CENTS, 1)[2], 1750)

    def test_empty_group_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_passenger_group({category: 0 for category in PASSENGER_CATEGORIES})

    def test_invalid_counts_are_rejected(self):
        for value in ("", "two", "2.5", "-1"):
            with self.assertRaises(ValueError):
                parse_non_negative_integer(value)

    def test_valid_count_and_zone_inputs_are_normalised(self):
        self.assertEqual(parse_non_negative_integer(" 2 "), 2)
        self.assertEqual(parse_zone_choice(" 2 ", {"1", "2", "3"}), "2")
        self.assertEqual(parse_repeat_choice(" y "), "Y")

    def test_invalid_zone_and_repeat_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_zone_choice("4", {"1", "2", "3"})
        with self.assertRaises(ValueError):
            parse_repeat_choice("maybe")


if __name__ == "__main__":
    unittest.main()

