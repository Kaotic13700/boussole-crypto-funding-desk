import json

import pytest

from funding_experiments import (
    calculate_outcomes,
    candidate_routes,
    connect_database,
    register_experiments,
    robust_summary,
    store_book_snapshot,
    store_settlements,
)


def market(identifier, exchange, rate, deadline=1_000_000):
    return {
        "id": identifier,
        "asset": "ABC",
        "symbol": f"ABC_{exchange}",
        "exchange": exchange,
        "funding_pct": rate,
        "interval_hours": 4,
        "next_funding_at": deadline,
        "perp_price": 100.0,
        "bid_price": 99.9,
        "ask_price": 100.1,
        "mark_price": 100.0,
        "contract_value": 1.0,
        "spread_bps": 20.0,
        "open_interest_usd": 10_000_000,
        "volume_24h_usd": 20_000_000,
        "market_data_at": deadline - 60_000,
        "data_status": "live",
        "perp_long_available": True,
        "perp_short_available": True,
    }


def book(bid, ask):
    return {
        "timestamp": 1,
        "bids": [(bid, 1_000)],
        "asks": [(ask, 1_000)],
    }


def test_candidate_routes_reject_unverifiable_same_window_hedge():
    observed = 100_000
    deadline = observed + 600_000
    signal = market("signal", "Gate.io", 0.5, deadline)
    unverified = market("hedge-hl", "Hyperliquid", -0.4, deadline)
    verified = market("hedge-bg", "Bitget", -0.1, deadline)
    candidates = candidate_routes([signal, unverified, verified], observed)
    signal_route = next(
        item for item in candidates if item[0]["funding_market_id"] == "signal"
    )
    assert signal_route[0]["hedge_exchange"] == "Bitget"


def test_outcome_uses_real_books_settlement_fees_and_leverage():
    database = ":memory:"
    deadline = 1_000_000
    signal = market("signal", "Gate.io", 0.5, deadline)
    hedge = market("hedge", "Bitget", 0.0, deadline + 4 * 60 * 60 * 1_000)
    with connect_database(database) as connection:
        route, funding_market, hedge_market = candidate_routes(
            [signal, hedge], deadline - 600_000
        )[0]
        register_experiments(
            connection, [(route, funding_market, hedge_market)], deadline - 600_000
        )
        experiment_id = connection.execute(
            "SELECT experiment_id FROM experiments"
        ).fetchone()[0]
        store_book_snapshot(
            connection, experiment_id, deadline, -60, deadline - 60_000,
            {"funding": book(100, 101), "hedge": book(99, 100)},
        )
        store_book_snapshot(
            connection, experiment_id, deadline, 60, deadline + 60_000,
            {"funding": book(100, 101), "hedge": book(99, 100)},
        )
        store_settlements(
            connection, experiment_id, "funding",
            [{
                "id": "Gate.io:ABC:1000000",
                "timestamp": deadline,
                "funding_pct": 0.5,
                "mark_price": 100,
                "source": "official",
            }],
        )
        outcomes = calculate_outcomes(
            connection, notionals=[4_000],
            fee_rates_pct={"Gate.io": 0.05, "Bitget": 0.05},
        )
    assert len(outcomes) == 1
    result = outcomes[0]
    assert result["settled_funding_income"] == pytest.approx(10)
    assert result["market_pnl"] == pytest.approx(-40)
    assert result["trading_fees"] == pytest.approx(4)
    assert result["net_result"] == pytest.approx(-34)
    assert result["realized_net_funding_pct"] == pytest.approx(0.5)
    assert result["break_even_funding_pct"] == pytest.approx(2.2)
    assert result["roe_10x_pct"] == pytest.approx(-8.5)


def test_robust_summary_requires_thirty_positive_lower_tail_samples():
    rows = []
    for index in range(30):
        rows.append(
            {
                "venue_pair": "Gate.io→Bitget",
                "interval_hours": 4,
                "entry_offset_seconds": -60,
                "exit_offset_seconds": 60,
                "total_notional": 4_000.0,
                "net_result": 5.0 + index / 100,
                "break_even_funding_pct": 0.2 + index / 1_000,
            }
        )
    summary = robust_summary(rows)
    assert summary[0]["status"] == "VALIDÉ"
    assert summary[0]["sample_size"] == 30
    assert summary[0]["p10_net_usd"] > 0
