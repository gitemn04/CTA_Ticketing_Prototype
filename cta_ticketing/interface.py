"""Display and prompt functions for the CTA console interface."""

from datetime import datetime

from .config import FARE_RATES_CENTS, PASSENGER_CATEGORIES, STATIONS_BY_ZONE, ZONES
from .fares import calculate_fare
from .validation import (
    parse_non_negative_integer,
    parse_repeat_choice,
    parse_zone_choice,
    validate_passenger_group,
)


def display_station_board() -> None:
    print("\nCTA STATION INFORMATION")
    for zone_name in ("Central", "Midtown", "Downtown"):
        print(f"\n{zone_name}")
        for station in sorted(STATIONS_BY_ZONE[zone_name]):
            print(f"  - {station}")


def prompt_zone(prompt: str) -> str:
    while True:
        print("\n1. Central\n2. Midtown\n3. Downtown")
        try:
            choice = parse_zone_choice(input(prompt), set(ZONES))
            return ZONES[choice]["name"]
        except ValueError as error:
            print(f"Error: {error}")


def prompt_passengers() -> dict[str, int]:
    while True:
        counts = {}
        for category in PASSENGER_CATEGORIES:
            while True:
                try:
                    counts[category] = parse_non_negative_integer(
                        input(f"Number of {category.lower()} passengers: ")
                    )
                    break
                except ValueError as error:
                    print(f"Error: {error}")
        try:
            validate_passenger_group(counts)
            return counts
        except ValueError as error:
            print(f"Error: {error}")


def format_money(cents: int) -> str:
    """Display integer cents with a unit label and thousands separators."""
    return f"{cents:,} cents"


def display_voucher(
    start_zone: str,
    destination_zone: str,
    zone_count: int,
    passenger_counts: dict[str, int],
    category_totals: dict[str, int],
    group_size: int,
    overall_total: int,
    issued_at: datetime,
) -> None:
    print("\n" + "=" * 48)
    print("CTA TRAVEL VOUCHER")
    print("=" * 48)
    print(f"Issued: {issued_at.strftime('%d/%m/%Y %H:%M:%S')}")
    print(f"Journey: {start_zone} to {destination_zone}")
    print(f"Chargeable zones: {zone_count}")
    print("\nPassenger breakdown")
    for category in PASSENGER_CATEGORIES:
        rate = FARE_RATES_CENTS[category]
        total = category_totals[category]
        print(
            f"{category:8} {passenger_counts[category]:>4} passenger(s) "
            f"at {format_money(rate)} per zone = {format_money(total)}"
        )
    print(f"\nGroup size: {group_size}")
    print(f"Total fare: {format_money(overall_total)}")
    print("Payment processing is outside this prototype.")
    print("=" * 48)


def run_transaction() -> None:
    start_zone = prompt_zone("Starting zone (1-3): ")
    destination_zone = prompt_zone("Destination zone (1-3): ")
    start_choice = next(
        key for key, value in ZONES.items() if value["name"] == start_zone
    )
    destination_choice = next(
        key for key, value in ZONES.items() if value["name"] == destination_zone
    )
    start_position = ZONES[start_choice]["position"]
    destination_position = ZONES[destination_choice]["position"]
    zone_count = abs(start_position - destination_position) + 1
    passenger_counts = prompt_passengers()
    category_totals, group_size, overall_total = calculate_fare(
        passenger_counts, FARE_RATES_CENTS, zone_count
    )
    display_voucher(
        ZONES[start_choice]["name"],
        ZONES[destination_choice]["name"],
        zone_count,
        passenger_counts,
        category_totals,
        group_size,
        overall_total,
        datetime.now(),
    )


def prompt_repeat() -> str:
    while True:
        try:
            return parse_repeat_choice(input("Issue another voucher? (Y/N): "))
        except ValueError as error:
            print(f"Error: {error}")
