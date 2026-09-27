"""Entry point for the CTA ticket voucher prototype."""

from .interface import display_station_board, prompt_repeat, run_transaction


def main() -> None:
    print("Centrala Transport Authority Ticket Voucher System")
    display_station_board()
    while True:
        run_transaction()
        if prompt_repeat() == "N":
            print("Thank you. Application closed.")
            return


if __name__ == "__main__":
    main()

