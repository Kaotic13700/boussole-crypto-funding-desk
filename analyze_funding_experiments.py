"""Print the empirical profitability matrix collected from live markets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from funding_experiments import calculate_outcomes, connect_database, database_status, robust_summary


DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "funding_experiments.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=str(DEFAULT_DATABASE))
    args = parser.parse_args()
    with connect_database(args.database) as connection:
        payload = {
            "status": database_status(connection),
            "matrix": robust_summary(calculate_outcomes(connection)),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
