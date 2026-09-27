# CTA Ticket Voucher Prototype

This is a Python console prototype for the Centrala Transport Authority.

Run from the project root:

```bash
python -m cta_ticketing.main
```

Run automated tests:

```bash
python -m unittest -v cta_ticketing.test_cta
```

Validation accepts non-negative whole-number passenger counts, rejects blank,
text, decimal and negative entries, and requires at least one traveller in a
transaction.

The fare-band calculation uses the documented provisional rule:
`abs(start_position - destination_position) + 1`. This must be confirmed against the final CTA policy before operational acceptance.
