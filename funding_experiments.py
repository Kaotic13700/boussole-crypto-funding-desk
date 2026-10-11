"""Empirical funding-window recorder and profitability calculator.

This module never sends an order.  It records official live market metadata,
paired order books around a real funding deadline, and official settled rates.
All paper fills can therefore be recomputed later from the captured depth.
"""

from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time
from typing import Any, Iterable

from paper_engine import (
    DEFAULT_TAKER_FEE_PCT,
    build_perp_routes,
    funding_cashflow,
    vwap_for_notional,
    vwap_for_quantity,
)


ENTRY_OFFSETS_SECONDS = (-900, -600, -300, -120, -60, -30)
EXIT_OFFSETS_SECONDS = (5, 30, 60, 120, 300, 900)
ALL_OFFSETS_SECONDS = ENTRY_OFFSETS_SECONDS + EXIT_OFFSETS_SECONDS
TOTAL_NOTIONALS_USD = (1_000.0, 2_000.0, 4_000.0, 8_000.0)
LEVERAGES = (3, 5, 6, 10)
EXACT_PUBLIC_SETTLEMENT_EXCHANGES = {"Bitget", "BloFin", "Gate.io"}
DEFAULT_RESEARCH_THRESHOLD_PCT = 0.01
MIN_VALIDATED_SAMPLE_SIZE = 30


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    route_json TEXT NOT NULL,
    funding_market_json TEXT NOT NULL,
    hedge_market_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiments_deadline_idx ON experiments(deadline_ms);

CREATE TABLE IF NOT EXISTS book_snapshots (
    experiment_id TEXT NOT NULL,
    offset_seconds INTEGER NOT NULL,
    target_at_ms INTEGER NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    funding_book_json TEXT NOT NULL,
    hedge_book_json TEXT NOT NULL,
    PRIMARY KEY (experiment_id, offset_seconds),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS settlements (
    experiment_id TEXT NOT NULL,
    leg TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    settled_at_ms INTEGER NOT NULL,
    funding_pct REAL NOT NULL,
    mark_price REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (experiment_id, leg, settlement_id),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);

CREATE TABLE IF NOT EXISTS collector_runs (
    observed_at_ms INTEGER PRIMARY KEY,
    live_markets INTEGER NOT NULL,
    active_experiments INTEGER NOT NULL,
    captured_snapshots INTEGER NOT NULL,
    stored_settlements INTEGER NOT NULL,
    error TEXT
);
"""


def now_ms() -> int:
    return int(time.time() * 1_000)


def connect_database(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _from_json(value: str) -> Any:
    return json.loads(value)


def _market_features(market: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "asset", "exchange", "symbol", "funding_pct", "interval_hours",
        "next_funding_at", "mark_price", "perp_price", "bid_price", "ask_price",
        "spread_bps", "open_interest_usd", "volume_24h_usd", "market_data_at",
        "contract_value", "funding_source", "price_source", "metrics_source",
    )
    return {key: market.get(key) for key in keys}


def candidate_routes(
    markets: list[dict[str, Any]],
    observed_at_ms: int,
    threshold_pct: float = DEFAULT_RESEARCH_THRESHOLD_PCT,
    horizon_seconds: int = 16 * 60,
    limit: int = 12,
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Return exact-settlement routes approaching their next deadline.

    A same-window hedge on Kraken or Hyperliquid is rejected because its real
    account funding payment cannot be proven from the public endpoint alone.
    """
    live = [market for market in markets if market.get("data_status") == "live"]
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for market in live:
        by_asset[str(market.get("asset"))].append(market)

    candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for funding_market in live:
        deadline = int(funding_market.get("next_funding_at") or 0)
        seconds_to_deadline = (deadline - observed_at_ms) / 1_000
        if (
            funding_market.get("exchange") not in EXACT_PUBLIC_SETTLEMENT_EXCHANGES
            or abs(float(funding_market.get("funding_pct") or 0)) < threshold_pct
            or seconds_to_deadline < -20
            or seconds_to_deadline > horizon_seconds
        ):
            continue
        alternatives: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for hedge_market in by_asset.get(str(funding_market.get("asset")), []):
            if hedge_market.get("id") == funding_market.get("id"):
                continue
            hedge_deadline = int(hedge_market.get("next_funding_at") or 0)
            if (
                hedge_deadline <= deadline
                and hedge_market.get("exchange") not in EXACT_PUBLIC_SETTLEMENT_EXCHANGES
            ):
                continue
            routes = build_perp_routes([funding_market, hedge_market], threshold_pct)
            route = next(
                (
                    item for item in routes
                    if item.get("funding_market_id") == funding_market.get("id")
                ),
                None,
            )
            if route is not None:
                if float(route["net_funding_pct"]) > 0:
                    alternatives.append((route, hedge_market))
        if not alternatives:
            continue
        alternatives.sort(
            key=lambda item: (
                -float(item[0]["net_funding_pct"]),
                float(item[0]["combined_spread_bps"]),
                not bool(item[0]["cross_exchange"]),
            )
        )
        route, hedge_market = alternatives[0]
        candidates.append((route, funding_market, hedge_market))
    candidates.sort(
        key=lambda item: (
            int(item[0]["next_funding_at"]),
            -float(item[0]["net_funding_pct"]),
            float(item[0]["combined_spread_bps"]),
        )
    )
    return candidates[: max(0, int(limit))]


def register_experiments(
    connection: sqlite3.Connection,
    candidates: Iterable[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    created_at_ms: int,
) -> int:
    inserted = 0
    for route, funding_market, hedge_market in candidates:
        experiment_id = f"{route['id']}::{int(route['next_funding_at'])}"
        cursor = connection.execute(
            """INSERT OR IGNORE INTO experiments
               (experiment_id, created_at_ms, deadline_ms, route_json,
                funding_market_json, hedge_market_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                experiment_id,
                int(created_at_ms),
                int(route["next_funding_at"]),
                _json(route),
                _json(_market_features(funding_market)),
                _json(_market_features(hedge_market)),
            ),
        )
        inserted += max(0, cursor.rowcount)
    connection.commit()
    return inserted


def due_snapshots(
    connection: sqlite3.Connection,
    observed_at_ms: int,
    tolerance_seconds: int = 18,
) -> list[tuple[sqlite3.Row, int]]:
    lower = observed_at_ms - tolerance_seconds * 1_000
    upper = observed_at_ms + tolerance_seconds * 1_000
    rows = connection.execute(
        "SELECT * FROM experiments WHERE deadline_ms BETWEEN ? AND ?",
        (
            lower - max(ALL_OFFSETS_SECONDS) * 1_000,
            upper - min(ALL_OFFSETS_SECONDS) * 1_000,
        ),
    ).fetchall()
    output: list[tuple[sqlite3.Row, int]] = []
    for row in rows:
        existing = {
            int(item[0])
            for item in connection.execute(
                "SELECT offset_seconds FROM book_snapshots WHERE experiment_id = ?",
                (row["experiment_id"],),
            )
        }
        for offset in ALL_OFFSETS_SECONDS:
            target = int(row["deadline_ms"]) + offset * 1_000
            if offset not in existing and lower <= target <= upper:
                output.append((row, offset))
    return output


def store_book_snapshot(
    connection: sqlite3.Connection,
    experiment_id: str,
    deadline_ms: int,
    offset_seconds: int,
    observed_at_ms: int,
    books: dict[str, dict[str, Any]],
) -> bool:
    cursor = connection.execute(
        """INSERT OR IGNORE INTO book_snapshots
           (experiment_id, offset_seconds, target_at_ms, observed_at_ms,
            funding_book_json, hedge_book_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            experiment_id,
            int(offset_seconds),
            int(deadline_ms) + int(offset_seconds) * 1_000,
            int(observed_at_ms),
            _json(books["funding"]),
            _json(books["hedge"]),
        ),
    )
    connection.commit()
    return cursor.rowcount > 0


def experiments_needing_settlements(
    connection: sqlite3.Connection, observed_at_ms: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT * FROM experiments
           WHERE deadline_ms BETWEEN ? AND ?""",
        (observed_at_ms - 24 * 60 * 60 * 1_000, observed_at_ms - 45 * 1_000),
    ).fetchall()


def store_settlements(
    connection: sqlite3.Connection,
    experiment_id: str,
    leg: str,
    events: Iterable[dict[str, Any]],
) -> int:
    inserted = 0
    for event in events:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO settlements
               (experiment_id, leg, settlement_id, settled_at_ms, funding_pct,
                mark_price, source) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                experiment_id,
                leg,
                str(event["id"]),
                int(event["timestamp"]),
                float(event["funding_pct"]),
                float(event["mark_price"]),
                str(event["source"]),
            ),
        )
        inserted += max(0, cursor.rowcount)
    connection.commit()
    return inserted


def record_collector_run(
    connection: sqlite3.Connection,
    observed_at_ms: int,
    live_markets: int,
    active_experiments: int,
    captured_snapshots: int,
    stored_settlements_count: int,
    error: str | None = None,
) -> None:
    connection.execute(
        """INSERT OR REPLACE INTO collector_runs
           (observed_at_ms, live_markets, active_experiments, captured_snapshots,
            stored_settlements, error) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            int(observed_at_ms), int(live_markets), int(active_experiments),
            int(captured_snapshots), int(stored_settlements_count), error,
        ),
    )
    connection.commit()


def _expected_settlement_times(route: dict[str, Any], entry_ms: int, exit_ms: int) -> dict[str, int]:
    expected: dict[str, int] = {}
    funding_time = int(route["next_funding_at"])
    if entry_ms < funding_time <= exit_ms:
        expected["funding"] = funding_time
    hedge_time = int(route["hedge_next_funding_at"])
    if entry_ms < hedge_time <= exit_ms:
        expected["hedge"] = hedge_time
    return expected


def calculate_outcomes(
    connection: sqlite3.Connection,
    notionals: Iterable[float] = TOTAL_NOTIONALS_USD,
    fee_rates_pct: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Reprice every complete entry/exit combination from captured depth."""
    fees = {**DEFAULT_TAKER_FEE_PCT, **(fee_rates_pct or {})}
    outcomes: list[dict[str, Any]] = []
    experiments = connection.execute("SELECT * FROM experiments").fetchall()
    for experiment in experiments:
        route = _from_json(experiment["route_json"])
        funding_market = _from_json(experiment["funding_market_json"])
        hedge_market = _from_json(experiment["hedge_market_json"])
        snapshots = {
            int(row["offset_seconds"]): row
            for row in connection.execute(
                "SELECT * FROM book_snapshots WHERE experiment_id = ?",
                (experiment["experiment_id"],),
            )
        }
        settlements = connection.execute(
            "SELECT * FROM settlements WHERE experiment_id = ?",
            (experiment["experiment_id"],),
        ).fetchall()
        for entry_offset in ENTRY_OFFSETS_SECONDS:
            entry = snapshots.get(entry_offset)
            if entry is None:
                continue
            entry_ms = int(entry["observed_at_ms"])
            entry_books = {
                "funding": _from_json(entry["funding_book_json"]),
                "hedge": _from_json(entry["hedge_book_json"]),
            }
            for exit_offset in EXIT_OFFSETS_SECONDS:
                exit_snapshot = snapshots.get(exit_offset)
                if exit_snapshot is None:
                    continue
                exit_ms = int(exit_snapshot["observed_at_ms"])
                expected = _expected_settlement_times(route, entry_ms, exit_ms)
                matched: dict[str, sqlite3.Row] = {}
                for leg, target_ms in expected.items():
                    candidates = [
                        row for row in settlements
                        if row["leg"] == leg and abs(int(row["settled_at_ms"]) - target_ms) <= 60_000
                    ]
                    if candidates:
                        matched[leg] = min(
                            candidates,
                            key=lambda row: abs(int(row["settled_at_ms"]) - target_ms),
                        )
                if set(matched) != set(expected):
                    continue
                exit_books = {
                    "funding": _from_json(exit_snapshot["funding_book_json"]),
                    "hedge": _from_json(exit_snapshot["hedge_book_json"]),
                }
                for total_notional in notionals:
                    try:
                        leg_notional = float(total_notional) / 2
                        funding_open = vwap_for_notional(
                            entry_books["funding"], route["funding_side"], leg_notional, "open"
                        )
                        hedge_open = vwap_for_notional(
                            entry_books["hedge"], route["hedge_side"], leg_notional, "open"
                        )
                        funding_close = vwap_for_quantity(
                            exit_books["funding"], route["funding_side"],
                            funding_open["quantity"], "close",
                        )
                        hedge_close = vwap_for_quantity(
                            exit_books["hedge"], route["hedge_side"],
                            hedge_open["quantity"], "close",
                        )
                    except ValueError:
                        continue
                    funding_direction = 1 if route["funding_side"] == "long" else -1
                    hedge_direction = 1 if route["hedge_side"] == "long" else -1
                    market_pnl = (
                        funding_direction * funding_open["quantity"]
                        * (funding_close["price"] - funding_open["price"])
                        + hedge_direction * hedge_open["quantity"]
                        * (hedge_close["price"] - hedge_open["price"])
                    )
                    funding_income = 0.0
                    for leg, event in matched.items():
                        fill = funding_open if leg == "funding" else hedge_open
                        side = route[f"{leg}_side"]
                        funding_income += funding_cashflow(
                            fill["quantity"], float(event["mark_price"]),
                            float(event["funding_pct"]), side,
                        )
                    funding_fee = float(fees[route["funding_exchange"]])
                    hedge_fee = float(fees[route["hedge_exchange"]])
                    trading_fees = (
                        (funding_open["notional"] + funding_close["notional"]) * funding_fee
                        + (hedge_open["notional"] + hedge_close["notional"]) * hedge_fee
                    ) / 100
                    net = market_pnl + funding_income - trading_fees
                    realized_net_funding_pct = funding_income / leg_notional * 100
                    break_even_funding_pct = (trading_fees - market_pnl) / leg_notional * 100
                    oi_values = [
                        float(value) for value in (
                            funding_market.get("open_interest_usd"),
                            hedge_market.get("open_interest_usd"),
                        ) if value is not None and float(value) > 0
                    ]
                    volume_values = [
                        float(value) for value in (
                            funding_market.get("volume_24h_usd"),
                            hedge_market.get("volume_24h_usd"),
                        ) if value is not None and float(value) > 0
                    ]
                    outcomes.append(
                        {
                            "experiment_id": experiment["experiment_id"],
                            "asset": route["asset"],
                            "venue_pair": f"{route['funding_exchange']}→{route['hedge_exchange']}",
                            "interval_hours": int(route["interval_hours"]),
                            "entry_offset_seconds": entry_offset,
                            "exit_offset_seconds": exit_offset,
                            "total_notional": float(total_notional),
                            "signal_funding_pct": float(route["funding_pct"]),
                            "expected_net_funding_pct": float(route["net_funding_pct"]),
                            "settled_funding_income": funding_income,
                            "realized_net_funding_pct": realized_net_funding_pct,
                            "break_even_funding_pct": break_even_funding_pct,
                            "market_pnl": market_pnl,
                            "trading_fees": trading_fees,
                            "net_result": net,
                            "return_on_notional_pct": net / float(total_notional) * 100,
                            "combined_entry_slippage_bps": (
                                funding_open["slippage_bps"] + hedge_open["slippage_bps"]
                            ),
                            "combined_exit_slippage_bps": (
                                funding_close["slippage_bps"] + hedge_close["slippage_bps"]
                            ),
                            "size_to_min_oi_pct": (
                                leg_notional / min(oi_values) * 100 if oi_values else None
                            ),
                            "size_to_min_volume_pct": (
                                leg_notional / min(volume_values) * 100 if volume_values else None
                            ),
                            **{
                                f"roe_{leverage}x_pct": net / (float(total_notional) / leverage) * 100
                                for leverage in LEVERAGES
                            },
                        }
                    )
    return outcomes


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def robust_summary(outcomes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        key = (
            outcome["venue_pair"], outcome["interval_hours"],
            outcome["entry_offset_seconds"], outcome["exit_offset_seconds"],
            outcome["total_notional"],
        )
        grouped[key].append(outcome)
    summary: list[dict[str, Any]] = []
    for key, rows in grouped.items():
        nets = [float(row["net_result"]) for row in rows]
        break_evens = [float(row["break_even_funding_pct"]) for row in rows]
        gains = sum(value for value in nets if value > 0)
        losses = -sum(value for value in nets if value < 0)
        n = len(nets)
        median_net = statistics.median(nets)
        p10_net = _percentile(nets, 0.10)
        summary.append(
            {
                "venue_pair": key[0],
                "interval_hours": key[1],
                "entry_offset_seconds": key[2],
                "exit_offset_seconds": key[3],
                "total_notional": key[4],
                "sample_size": n,
                "median_net_usd": median_net,
                "p10_net_usd": p10_net,
                "median_break_even_funding_pct": statistics.median(break_evens),
                "p90_break_even_funding_pct": _percentile(break_evens, 0.90),
                "p95_break_even_funding_pct": _percentile(break_evens, 0.95),
                "win_rate_pct": sum(value > 0 for value in nets) / n * 100,
                "profit_factor": gains / losses if losses > 0 else (None if gains > 0 else 0.0),
                "status": (
                    "VALIDÉ" if n >= MIN_VALIDATED_SAMPLE_SIZE and p10_net > 0
                    else "REJETÉ" if n >= MIN_VALIDATED_SAMPLE_SIZE
                    else "COLLECTE"
                ),
            }
        )
    summary.sort(
        key=lambda row: (
            row["status"] != "VALIDÉ", -row["sample_size"], -row["p10_net_usd"]
        )
    )
    return summary


def database_status(connection: sqlite3.Connection) -> dict[str, Any]:
    last_run = connection.execute(
        "SELECT * FROM collector_runs ORDER BY observed_at_ms DESC LIMIT 1"
    ).fetchone()
    counts = {
        "experiments": connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0],
        "snapshots": connection.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0],
        "settlements": connection.execute("SELECT COUNT(*) FROM settlements").fetchone()[0],
    }
    return {**counts, "last_run": dict(last_run) if last_run else None}
