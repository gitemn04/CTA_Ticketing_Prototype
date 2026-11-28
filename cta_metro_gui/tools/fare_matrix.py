"""Fare matrix tool.

    python tools/fare_matrix.py --check      does the code still give exactly the fares in fare_matrix.csv?
    python tools/fare_matrix.py --compare    every journey where the 'band' and 'route' zone rules give different fares
                                             (writes fare_rule_comparison.csv for the project owner to review)
    python tools/fare_matrix.py --write      regenerate fare_matrix.csv from the code. ONLY after the project owner has
                                             approved a fare change, and together with a new FARE_POLICY_VERSION.
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cta_fares as fares  # noqa: E402
import cta_routing as rt  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', action='store_true')
    g.add_argument('--compare', action='store_true')
    g.add_argument('--write', action='store_true')
    ap.add_argument('--out', default=str(ROOT / 'fare_rule_comparison.csv'))
    args = ap.parse_args(argv)
    if args.check:
        try:
            fares.check_against_matrix()
        except fares.FarePolicyError as exc:
            print(f'MISMATCH: {exc}')
            return 1
        print(f'OK: the code matches {fares.MATRIX_FILE.name} for all {len(fares.matrix_rows())} journeys '
              f'(policy {fares.FARE_POLICY_VERSION}, rule {fares.ACTIVE_RULE}, status {fares.FARE_POLICY_STATUS}).')
        return 0
    if args.compare:
        diff = fares.compare_rules()
        with open(args.out, 'w', newline='', encoding='utf-8') as fh:
            w = csv.writer(fh)
            w.writerow(['from', 'to', 'from_zone', 'to_zone', f'zones_{fares.ACTIVE_RULE}_rule_now',
                        'zones_other_rule', 'adult_now_cents', 'adult_other_cents'])
            rate = fares.FARE_RATES_CENTS['Adult']
            for a, b, x, y in diff:
                w.writerow([a, b, rt.ZONES[a], rt.ZONES[b], x, y, rate * x, rate * y])
        total = len(rt.STATIONS) * (len(rt.STATIONS) - 1)
        print(f'{len(diff)} of {total} journeys would change fare under the other rule. Details: {args.out}')
        return 0
    n = fares.write_matrix()
    print(f'Wrote {n} journeys to {fares.MATRIX_FILE.name}. Bump FARE_POLICY_VERSION if any fare changed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
