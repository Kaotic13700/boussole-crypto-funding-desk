"""Persistent 24-hour market-metric history.

The scanner itself stays stateless and fast.  A separate collector stores one
official snapshot per five-minute UTC bucket.  A 24-hour change is published
only when both snapshots exist for the exact same bucket, 24 hours apart.
There is deliberately no interpolation and no refresh-to-refresh fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Iterable


BUCKET_SECONDS = 300
RETENTION_HOURS = 72
MAX_CURRENT_BUCKET_AGE_SECONDS = 15 * 60


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_metric_snapshots (
    observed_bucket TIMESTAMPTZ NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open_interest_native DOUBLE PRECISION,
    open_interest_usd DOUBLE PRECISION,
    volume_24h_native DOUBLE PRECISION,
    volume_24h_usd DOUBLE PRECISION,
    source_market_ts BIGINT,
    PRIMARY KEY (exchange, symbol, observed_bucket)
);

CREATE INDEX IF NOT EXISTS market_metric_snapshots_bucket_idx
    ON market_metric_snapshots (observed_bucket DESC);
"""


UPSERT_SQL = """
INSERT INTO market_metric_snapshots (
    observed_bucket,
    exchange,
    symbol,
    open_interest_native,
    open_interest_usd,
    volume_24h_native,
    volume_24h_usd,
    source_market_ts
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (exchange, symbol, observed_bucket) DO UPDATE SET
    open_interest_native = EXCLUDED.open_interest_native,
    open_interest_usd = EXCLUDED.open_interest_usd,
    volume_24h_native = EXCLUDED.volume_24h_native,
    volume_24h_usd = EXCLUDED.volume_24h_usd,
    source_market_ts = EXCLUDED.source_market_ts
"""


EXACT_24H_SQL = """
WITH latest_bucket AS (
    SELECT MAX(observed_bucket) AS value
    FROM market_metric_snapshots
), paired AS (
    SELECT
        current.exchange,
        current.symbol,
        current.observed_bucket,
        current.open_interest_native AS current_oi,
        history.open_interest_native AS history_oi,
        current.volume_24h_native AS current_volume,
        history.volume_24h_native AS history_volume
    FROM market_metric_snapshots AS current
    JOIN latest_bucket ON current.observed_bucket = latest_bucket.value
    JOIN market_metric_snapshots AS history
      ON history.exchange = current.exchange
     AND history.symbol = current.symbol
     AND history.observed_bucket = current.observed_bucket - INTERVAL '24 hours'
)
SELECT
    exchange,
    symbol,
    observed_bucket,
    current_oi,
    history_oi,
    current_volume,
    history_volume
FROM paired
"""


def bucket_datetime(timestamp_ms: int) -> datetime:
    """Return the exact five-minute UTC bucket for a millisecond timestamp."""

    seconds = max(0, int(timestamp_ms) // 1000)
    bucket = seconds - seconds % BUCKET_SECONDS
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def exact_change_pct(current: Any, history: Any) -> float | None:
    """Return a percentage only for two valid, positive official readings."""

    current_value = finite_number(current)
    history_value = finite_number(history)
    if current_value is None or history_value is None or history_value <= 0:
        return None
    return (current_value - history_value) / history_value * 100


def snapshot_rows(
    markets: Iterable[dict[str, Any]], timestamp_ms: int
) -> list[tuple[Any, ...]]:
    observed_bucket = bucket_datetime(timestamp_ms)
    rows: list[tuple[Any, ...]] = []
    for market in markets:
        if market.get("data_status") != "live":
            continue
        open_interest_native = finite_number(market.get("open_interest_native"))
        volume_24h_native = finite_number(market.get("volume_24h_native"))
        if open_interest_native is None and volume_24h_native is None:
            continue
        rows.append(
            (
                observed_bucket,
                str(market["exchange"]),
                str(market["symbol"]),
                open_interest_native,
                finite_number(market.get("open_interest_usd")),
                volume_24h_native,
                finite_number(market.get("volume_24h_usd")),
                market.get("market_data_at"),
            )
        )
    return rows


def initialize_database(database_url: str) -> None:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)


def store_snapshot(
    database_url: str,
    markets: Iterable[dict[str, Any]],
    timestamp_ms: int,
) -> int:
    import psycopg

    rows = snapshot_rows(markets, timestamp_ms)
    if not rows:
        return 0
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
            cursor.executemany(UPSERT_SQL, rows)
            cursor.execute(
                "DELETE FROM market_metric_snapshots "
                "WHERE observed_bucket < NOW() - (%s * INTERVAL '1 hour')",
                (RETENTION_HOURS,),
            )
    return len(rows)


def rows_to_changes(
    rows: Iterable[tuple[Any, ...]], now: datetime | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    current_time = now or datetime.now(tz=timezone.utc)
    changes: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        exchange, symbol, observed_bucket, current_oi, history_oi, current_volume, history_volume = row
        if observed_bucket.tzinfo is None:
            observed_bucket = observed_bucket.replace(tzinfo=timezone.utc)
        age_seconds = (current_time - observed_bucket).total_seconds()
        if age_seconds < 0 or age_seconds > MAX_CURRENT_BUCKET_AGE_SECONDS:
            continue
        changes[(str(exchange), str(symbol))] = {
            "open_interest_change_pct": exact_change_pct(current_oi, history_oi),
            "volume_24h_change_pct": exact_change_pct(current_volume, history_volume),
            "metric_change_bucket_ms": int(observed_bucket.timestamp() * 1000),
            "metric_change_source": "Snapshots officiels espacés exactement de 24 h",
        }
    return changes


def load_24h_changes(
    database_url: str, now: datetime | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    import psycopg

    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(EXACT_24H_SQL)
            rows = cursor.fetchall()
    return rows_to_changes(rows, now=now)


def apply_24h_changes(
    markets: Iterable[dict[str, Any]],
    changes: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for market in markets:
        verified_change = changes.get((str(market["exchange"]), str(market["symbol"])), {})
        enriched.append(
            {
                **market,
                "open_interest_change_pct": verified_change.get("open_interest_change_pct"),
                "volume_24h_change_pct": verified_change.get("volume_24h_change_pct"),
                "metric_change_bucket_ms": verified_change.get("metric_change_bucket_ms"),
                "metric_change_source": verified_change.get("metric_change_source"),
            }
        )
    return enriched

