"""One-shot collector intended for a five-minute external cron job."""

from __future__ import annotations

import os
import sys

from funding_engine import collect_feed
from market_history import store_snapshot


def main() -> int:
    database_url = os.environ.get("HISTORY_DATABASE_URL", "").strip()
    if not database_url:
        print("HISTORY_DATABASE_URL is required", file=sys.stderr)
        return 2

    feed = collect_feed()
    live_markets = [
        market for market in feed["markets"] if market.get("data_status") == "live"
    ]
    if not live_markets:
        print("No live official market data; snapshot not written", file=sys.stderr)
        return 1

    stored = store_snapshot(database_url, live_markets, int(feed["fetched_at"]))
    print(f"Stored {stored} official market snapshots for bucket {feed['fetched_at']}")
    return 0 if stored else 1


if __name__ == "__main__":
    raise SystemExit(main())

