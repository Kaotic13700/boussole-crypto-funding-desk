from datetime import datetime, timedelta, timezone

import pytest

from market_history import (
    apply_24h_changes,
    bucket_datetime,
    exact_change_pct,
    rows_to_changes,
    snapshot_rows,
)


def market(**overrides):
    base = {
        "exchange": "Bitget",
        "symbol": "BTCUSDT",
        "data_status": "live",
        "open_interest_native": 120.0,
        "open_interest_usd": 12_000.0,
        "volume_24h_native": 250.0,
        "volume_24h_usd": 25_000.0,
        "market_data_at": 1_700_000_000_000,
    }
    return {**base, **overrides}


def test_bucket_datetime_is_exact_five_minute_utc_bucket() -> None:
    timestamp = int(datetime(2026, 10, 11, 12, 7, 42, tzinfo=timezone.utc).timestamp() * 1000)
    assert bucket_datetime(timestamp) == datetime(2026, 10, 11, 12, 5, tzinfo=timezone.utc)


def test_snapshot_rows_excludes_stale_and_invalid_markets() -> None:
    rows = snapshot_rows(
        [market(), market(symbol="OLD", data_status="stale"), market(symbol="BAD", open_interest_native=-1, volume_24h_native=None)],
        int(datetime(2026, 10, 11, 12, 7, tzinfo=timezone.utc).timestamp() * 1000),
    )
    assert len(rows) == 1
    assert rows[0][1:3] == ("Bitget", "BTCUSDT")


def test_exact_change_pct_never_invents_missing_or_zero_history() -> None:
    assert exact_change_pct(110, 100) == pytest.approx(10.0)
    assert exact_change_pct(90, 100) == pytest.approx(-10.0)
    assert exact_change_pct(100, 0) is None
    assert exact_change_pct(None, 100) is None


def test_rows_to_changes_requires_fresh_exact_pair() -> None:
    now = datetime(2026, 10, 11, 12, 10, tzinfo=timezone.utc)
    fresh_bucket = now - timedelta(minutes=5)
    stale_bucket = now - timedelta(minutes=16)
    changes = rows_to_changes(
        [
            ("Bitget", "BTCUSDT", fresh_bucket, 120.0, 100.0, 80.0, 100.0),
            ("BloFin", "ETH-USDT", stale_bucket, 120.0, 100.0, 120.0, 100.0),
        ],
        now=now,
    )
    assert changes[("Bitget", "BTCUSDT")]["open_interest_change_pct"] == pytest.approx(20.0)
    assert changes[("Bitget", "BTCUSDT")]["volume_24h_change_pct"] == pytest.approx(-20.0)
    assert ("BloFin", "ETH-USDT") not in changes


def test_apply_24h_changes_uses_exchange_and_symbol_key() -> None:
    changes = {
        ("Bitget", "BTCUSDT"): {
            "open_interest_change_pct": 4.2,
            "volume_24h_change_pct": -3.1,
            "metric_change_bucket_ms": 123,
            "metric_change_source": "verified",
        }
    }
    enriched = apply_24h_changes([market(), market(exchange="BloFin")], changes)
    assert enriched[0]["open_interest_change_pct"] == 4.2
    assert enriched[1]["open_interest_change_pct"] is None

