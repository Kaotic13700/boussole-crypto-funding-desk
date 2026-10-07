import pytest

from paper_engine import build_perp_routes, leg_pnl, mark_paper_position


def market(identifier: str, exchange: str, rate: float, price: float = 100.0) -> dict:
    return {
        "id": identifier,
        "asset": "ABC",
        "exchange": exchange,
        "funding_pct": rate,
        "interval_hours": 4,
        "next_funding_at": 10_000,
        "perp_price": price,
        "spread_bps": 2,
        "depth_usd": 1_000_000,
        "data_status": "live",
        "perp_long_available": True,
        "perp_short_available": True,
    }


def test_positive_route_receives_on_short_leg() -> None:
    routes = build_perp_routes([market("a", "Bitget", 0.6), market("b", "BloFin", 0.1)])
    assert routes[0]["funding_side"] == "short"
    assert routes[0]["hedge_side"] == "long"
    assert routes[0]["cross_exchange"] is True


def test_negative_route_receives_on_long_leg() -> None:
    routes = build_perp_routes([market("a", "Bitget", -0.6), market("b", "BloFin", 0.1)])
    assert routes[0]["funding_side"] == "long"
    assert routes[0]["hedge_side"] == "short"


def test_mark_to_market_uses_live_prices_only() -> None:
    position = {
        "funding_market_id": "a",
        "hedge_market_id": "b",
        "funding_entry_price": 100,
        "hedge_entry_price": 100,
        "funding_side": "short",
        "hedge_side": "long",
        "leg_notional": 2_000,
    }
    markets = {"a": market("a", "Bitget", 0.5, 99), "b": market("b", "BloFin", 0.1, 101)}
    marked = mark_paper_position(position, markets)
    assert marked is not None
    assert marked["funding_leg_pnl"] == pytest.approx(20)
    assert marked["hedge_leg_pnl"] == pytest.approx(20)
    assert marked["projected_next_funding"] == pytest.approx(10)


def test_invalid_side_is_rejected() -> None:
    with pytest.raises(ValueError):
        leg_pnl(100, 101, 2_000, "flat")
