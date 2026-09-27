"""Input-validation helpers for the CTA console interface."""


def parse_zone_choice(raw_value: str, valid_choices: set[str]) -> str:
    """Return a valid zone menu choice or raise ValueError."""
    value = raw_value.strip()
    if value not in valid_choices:
        raise ValueError("Please enter 1 for Central, 2 for Midtown or 3 for Downtown.")
    return value


def parse_non_negative_integer(raw_value: str) -> int:
    """Return a non-negative whole number or raise ValueError."""
    value = raw_value.strip()
    if not value:
        raise ValueError("Enter a passenger count. Use 0 if none are travelling.")
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("Please enter a whole number, such as 0, 1 or 2.") from exc
    if number < 0:
        raise ValueError("Passenger numbers cannot be negative. Enter 0 or more.")
    return number


def validate_passenger_group(passenger_counts: dict[str, int]) -> None:
    """Reject a transaction containing no travellers."""
    if sum(passenger_counts.values()) == 0:
        raise ValueError(
            "A voucher must include at least one traveller. "
            "Please enter the passenger numbers again."
        )


def parse_repeat_choice(raw_value: str) -> str:
    """Return normalised Y or N, rejecting any other answer."""
    value = raw_value.strip().upper()
    if value not in {"Y", "N"}:
        raise ValueError("Enter Y to issue another voucher or N to finish.")
    return value

