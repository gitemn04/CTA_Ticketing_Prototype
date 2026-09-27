"""Central configuration for the CTA ticket voucher prototype."""

ZONES = {
    "1": {"name": "Central", "position": 1},
    "2": {"name": "Midtown", "position": 2},
    "3": {"name": "Downtown", "position": 3},
}

STATIONS_BY_ZONE = {
    "Central": [
        "Bylyn", "Centrala", "Frestin", "Jaund", "Lomil",
        "Ninia", "Rede", "Soth", "Tallan", "Yaen",
    ],
    "Midtown": [
        "Agralle", "Docia", "Garion", "Obelyn", "Oloadus",
        "Quthiel", "Ralith", "Riclya", "Riladia", "Stonyam",
        "Sylas", "Wicyt",
    ],
    "Downtown": [
        "Adohad", "Brunad", "Ederif", "Elyot", "Erean", "Holmer",
        "Keivia", "Marend", "Perinad", "Pryn", "Ruril", "Ryall",
        "Vertwall", "Zord",
    ],
}

FARE_RATES_CENTS = {
    "Adult": 2105,
    "Child": 1410,
    "Senior": 1025,
    "Student": 1750,
}

PASSENGER_CATEGORIES = tuple(FARE_RATES_CENTS)

