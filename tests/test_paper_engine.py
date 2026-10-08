import pytest

from paper_engine import (
    apply_confirmed_settlements,
    build_perp_routes,
    close_paper_position,
    funding_cashflow,
    funding_cashflow_pct,
    leg_pnl,
    mark_paper_position,
    open_paper_position,
    vwap_for_notional,
)


def market(identifier: str, exchange: str, rate: float, price: float = 100.0) -> dict:
    return {
        "id": identifier,
        "asset": "ABC",
        "symbol": f"ABC-{exchange}",
        "exchange": exchange,
        "funding_pct": rate,
        "interval_hours": 4,
        "next_funding_at": 10_000,
        "perp_price": price,
        "bid_price": price - 0.1,
        "ask_price": price + 0.1,
        "mark_price": price,
        "contract_value": 1.0,
        "spread_bps": 2,
        "depth_usd": 1_000_000,
        "data_status": "live",
        "perp_long_available": True,
        "perp_short_available": True,
    }


def flat_book(bid: float, ask: float, quantity: float = 100.0) -> dict:
    return {"bids": [(bid, quantity)], "asks": [(ask, quantity)]}


def route_by_funding_id(routes: list[dict], identifier: str) -> dict:
    return next(route for route in routes if route["funding_market_id"] == identifier)


def test_positive_route_receives_on_short_leg_and_accounts_for_hedge_funding() -> None:
    routes = build_perp_routes([market("a", "Bitget", 0.6), market("b", "BloFin", 0.1)])
    route = route_by_funding_id(routes, "a")
    assert route["funding_side"] == "short"
    assert route["hedge_side"] == "long"
    assert route["cross_exchange"] is True
    assert route["net_funding_pct"] == pytest.approx(0.5)


def test_negative_route_receives_on_long_leg() -> None:
    routes = build_perp_routes([market("a", "Bitget", -0.6), market("b", "BloFin", 0.1)])
    route = route_by_funding_id(routes, "a")
    assert route["funding_side"] == "long"
    assert route["hedge_side"] == "short"
    assert route["net_funding_pct"] == pytest.approx(0.7)


def test_route_selects_best_live_hedge_funding_not_only_smallest_spread() -> None:
    funding = market("a", "Bitget", 0.6)
    costly_hedge = market("b", "BloFin", 0.1)
    favorable_hedge = market("c", "Kraken", -0.2)
    favorable_hedge["spread_bps"] = 5
    route = route_by_funding_id(
        build_perp_routes([funding, costly_hedge, favorable_hedge]), "a"
    )
    assert route["hedge_market_id"] == "c"
    assert route["net_funding_pct"] == pytest.approx(0.8)



def test_hedge_funding_after_signal_deadline_is_not_counted_in_capture_window() -> None:
    funding = market("a", "Bitget", 0.6)
    hedge = market("b", "BloFin", -0.2)
    funding["next_funding_at"] = 10_000
    hedge["next_funding_at"] = 20_000
    route = route_by_funding_id(build_perp_routes([funding, hedge]), "a")
    assert route["hedge_funding_in_capture_window"] is False
    assert route["net_funding_pct"] == pytest.approx(0.6)


def test_watch_threshold_does_not_create_routes_below_absolute_point_four() -> None:
    routes = build_perp_routes([market("a", "Bitget", 0.3999), market("b", "BloFin", 0.0)])
    assert routes == []


def test_vwap_consumes_real_depth_and_rejects_insufficient_depth() -> None:
    book = {"bids": [(99.0, 20.0)], "asks": [(100.0, 10.0), (101.0, 10.0)]}
    fill = vwap_for_notional(book, "long", 1_500)
    assert fill["notional"] == pytest.approx(1_500)
    assert fill["quantity"] == pytest.approx(10 + 500 / 101)
    assert fill["price"] == pytest.approx(1_500 / (10 + 500 / 101))
    assert fill["slippage_bps"] > 0
    with pytest.raises(ValueError, match="Profondeur insuffisante"):
        vwap_for_notional(book, "long", 3_000)


def test_open_position_uses_book_vwap_and_taker_fees() -> None:
    route = route_by_funding_id(
        build_perp_routes([market("a", "Bitget", 0.6), market("b", "BloFin", 0.1)]),
        "a",
    )
    position = open_paper_position(
        route,
        4_000,
        5,
        {"Bitget": 0.06, "BloFin": 0.05},
        {
            "funding": {"bids": [(100, 10), (99, 20)], "asks": [(101, 50)]},
            "hedge": {"bids": [(99, 50)], "asks": [(100, 10), (101, 20)]},
        },
        1_000,
        "position-1",
    )
    assert position["total_notional"] == 4_000
    assert position["margin_required"] == 800
    assert position["funding_entry_price"] < 100
    assert position["hedge_entry_price"] > 100
    assert position["entry_fee"] == pytest.approx(2.2)


def test_mark_to_market_uses_executable_prices_and_both_projected_fundings() -> None:
    position = {
        "funding_market_id": "a",
        "hedge_market_id": "b",
        "funding_entry_price": 100,
        "hedge_entry_price": 100,
        "funding_side": "short",
        "hedge_side": "long",
        "leg_notional": 2_000,
        "funding_quantity": 20,
        "hedge_quantity": 20,
        "funding_fee_rate_pct": 0,
        "hedge_fee_rate_pct": 0,
        "entry_fee": 0,
        "confirmed_funding_events": [],
    }
    markets = {"a": market("a", "Bitget", 0.5, 99), "b": market("b", "BloFin", 0.1, 101)}
    marked = mark_paper_position(position, markets)
    assert marked is not None
    assert marked["funding_leg_pnl"] == pytest.approx(18)
    assert marked["hedge_leg_pnl"] == pytest.approx(18)
    assert marked["projected_next_funding"] == pytest.approx(9.9)
    assert marked["projected_hedge_funding"] == pytest.approx(-2.02)
    assert marked["projected_net_funding"] == pytest.approx(7.88)


def test_close_position_uses_exact_quantity_and_all_fees() -> None:
    route = route_by_funding_id(
        build_perp_routes([market("a", "Bitget", 0.6), market("b", "BloFin", 0.1)]),
        "a",
    )
    position = open_paper_position(
        route,
        4_000,
        5,
        {"Bitget": 0.06, "BloFin": 0.05},
        {"funding": flat_book(100, 101), "hedge": flat_book(99, 100)},
        1_000,
        "position-1",
    )
    closed = close_paper_position(
        position,
        {"funding": flat_book(98, 99), "hedge": flat_book(101, 102)},
        2_000,
    )
    assert closed["market_pnl"] == pytest.approx(40)
    assert closed["entry_fee"] == pytest.approx(2.2)
    assert closed["exit_fee"] == pytest.approx(2.198)
    assert closed["net_result"] == pytest.approx(35.602)


def test_confirmed_settlement_is_signed_and_deduplicated() -> None:
    position = {
        "funding_side": "short",
        "funding_quantity": 20,
        "hedge_side": "long",
        "hedge_quantity": 20,
        "confirmed_funding_events": [],
    }
    event = {
        "id": "Bitget:ABC:10000",
        "timestamp": 10_000,
        "funding_pct": 0.5,
        "mark_price": 100,
    }
    assert apply_confirmed_settlements(position, "funding", [event]) == 1
    assert apply_confirmed_settlements(position, "funding", [event]) == 0
    assert position["confirmed_funding_events"][0]["cashflow"] == pytest.approx(10)


def test_funding_signs_and_invalid_sides() -> None:
    assert funding_cashflow_pct(0.5, "short") == pytest.approx(0.5)
    assert funding_cashflow_pct(-0.5, "long") == pytest.approx(0.5)
    assert funding_cashflow(20, 100, 0.5, "short") == pytest.approx(10)
    with pytest.raises(ValueError):
        leg_pnl(100, 101, 2_000, "flat")
