"""Pure fare calculations for the CTA ticket voucher prototype."""

from collections.abc import Mapping


def calculate_chargeable_zones(start_position: int, destination_position: int) -> int:
    """Return inclusive fare-band zones under the documented assumption."""
    if start_position < 1 or destination_position < 1:
        raise ValueError("Zone positions must be positive.")
    return abs(start_position - destination_position) + 1


def calculate_category_total(
    passenger_count: int, rate_cents: int, zone_count: int
) -> int:
    """Return a category total in cents for validated inputs."""
    if passenger_count < 0 or rate_cents < 0 or zone_count < 1:
        raise ValueError("Fare inputs are outside the valid range.")
    return passenger_count * rate_cents * zone_count


def calculate_fare(
    passenger_counts: Mapping[str, int],
    fare_rates_cents: Mapping[str, int],
    zone_count: int,
) -> tuple[dict[str, int], int, int]:
    """Return category totals, group size and overall fare in cents."""
    if zone_count < 1:
        raise ValueError("At least one chargeable zone is required.")
    if set(passenger_counts) != set(fare_rates_cents):
        raise ValueError("Passenger categories and fare categories must match.")

    category_totals = {
        category: calculate_category_total(
            passenger_counts[category], fare_rates_cents[category], zone_count
        )
        for category in fare_rates_cents
    }
    group_size = sum(passenger_counts.values())
    overall_total = sum(category_totals.values())
    return category_totals, group_size, overall_total

