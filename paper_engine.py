"""Pure helpers for the live-market paper portfolio.

The module never sends orders and never invents prices. A route is emitted only
when both legs have a live price in the exchange feed.
"""

from __future__ import annotations

from typing import Any
DEFAULT_TAKER_FEE_PCT = {"Bitget": 0.06, "Kraken": 0.05, "BloFin": 0.06}


def executable_top_price(market: dict[str, Any], side: str, action: str) -> float:
    if side not in {"long", "short"} or action not in {"open", "close"}:
        raise ValueError("Sens ou action invalide")
    buying = (action == "open" and side == "long") or (action == "close" and side == "short")
    field = "ask_price" if buying else "bid_price"
    price = float(market.get(field) or market.get("perp_price") or 0)
    if price <= 0:
        raise ValueError("Prix exécutable indisponible")
    return price


def funding_cashflow_pct(funding_pct: float, side: str) -> float:
    if side == "short":
        return float(funding_pct)
    if side == "long":
        return -float(funding_pct)
    raise ValueError("Sens de position invalide")


def vwap_for_notional(
    book: dict[str, Any], side: str, notional: float, action: str = "open"
) -> dict[str, float]:
    if notional <= 0:
        raise ValueError("Notionnel strictement positif requis")
    if side not in {"long", "short"} or action not in {"open", "close"}:
        raise ValueError("Sens ou action invalide")
    buying = (action == "open" and side == "long") or (action == "close" and side == "short")
    levels = book.get("asks" if buying else "bids") or []
    remaining = float(notional)
    quantity = 0.0
    executed = 0.0
    top_price = float(levels[0][0]) if levels else 0.0
    for raw_price, raw_quantity in levels:
        price = float(raw_price)
        available_quantity = float(raw_quantity)
        if min(price, available_quantity) <= 0:
            continue
        taken_quantity = min(available_quantity, remaining / price)
        quantity += taken_quantity
        level_notional = taken_quantity * price
        executed += level_notional
        remaining -= level_notional
        if remaining <= max(1e-8, notional * 1e-10):
            break
    if remaining > max(0.01, notional * 1e-6) or quantity <= 0 or top_price <= 0:
        raise ValueError("Profondeur insuffisante pour le notionnel demandé")
    vwap = executed / quantity
    adverse_move = vwap - top_price if buying else top_price - vwap
    return {
        "price": vwap,
        "quantity": quantity,
        "notional": executed,
        "slippage_bps": max(0.0, adverse_move / top_price * 10_000),
    }



def vwap_for_quantity(
    book: dict[str, Any], side: str, quantity: float, action: str = "close"
) -> dict[str, float]:
    if quantity <= 0:
        raise ValueError("Quantité strictement positive requise")
    if side not in {"long", "short"} or action not in {"open", "close"}:
        raise ValueError("Sens ou action invalide")
    buying = (action == "open" and side == "long") or (action == "close" and side == "short")
    levels = book.get("asks" if buying else "bids") or []
    remaining = float(quantity)
    executed_quantity = 0.0
    executed_notional = 0.0
    top_price = float(levels[0][0]) if levels else 0.0
    for raw_price, raw_quantity in levels:
        price = float(raw_price)
        available_quantity = float(raw_quantity)
        if min(price, available_quantity) <= 0:
            continue
        taken_quantity = min(available_quantity, remaining)
        executed_quantity += taken_quantity
        executed_notional += taken_quantity * price
        remaining -= taken_quantity
        if remaining <= max(1e-12, quantity * 1e-10):
            break
    if remaining > max(1e-10, quantity * 1e-6) or executed_quantity <= 0 or top_price <= 0:
        raise ValueError("Profondeur insuffisante pour clôturer la quantité")
    vwap = executed_notional / executed_quantity
    adverse_move = vwap - top_price if buying else top_price - vwap
    return {
        "price": vwap,
        "quantity": executed_quantity,
        "notional": executed_notional,
        "slippage_bps": max(0.0, adverse_move / top_price * 10_000),
    }
def funding_cashflow(quantity: float, mark_price: float, funding_pct: float, side: str) -> float:
    if min(quantity, mark_price) <= 0:
        raise ValueError("Quantité et prix de marque strictement positifs requis")
    return quantity * mark_price * funding_cashflow_pct(funding_pct, side) / 100


def build_perp_routes(
    markets: list[dict[str, Any]], threshold_pct: float = 0.4
) -> list[dict[str, Any]]:
    live = [
        market
        for market in markets
        if market.get("data_status") == "live"
        and min(
            float(market.get("bid_price") or market.get("perp_price") or 0),
            float(market.get("ask_price") or market.get("perp_price") or 0),
        )
        > 0
        and market.get("perp_long_available")
        and market.get("perp_short_available")
    ]
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for market in live:
        by_asset.setdefault(market["asset"], []).append(market)

    routes: list[dict[str, Any]] = []
    for funding_leg in live:
        funding_rate = float(funding_leg["funding_pct"])
        if abs(funding_rate) < threshold_pct:
            continue
        funding_side = "short" if funding_rate > 0 else "long"
        hedge_side = "long" if funding_side == "short" else "short"
        candidate_routes: list[dict[str, Any]] = []
        for hedge_leg in by_asset.get(funding_leg["asset"], []):
            if hedge_leg["id"] == funding_leg["id"]:
                continue
            hedge_rate = float(hedge_leg["funding_pct"])
            hedge_in_capture_window = int(hedge_leg["next_funding_at"]) <= int(
                funding_leg["next_funding_at"]
            )
            hedge_cashflow = (
                funding_cashflow_pct(hedge_rate, hedge_side)
                if hedge_in_capture_window
                else 0.0
            )
            net_rate = funding_cashflow_pct(funding_rate, funding_side) + hedge_cashflow
            candidate_routes.append(
                {
                    "id": f"{funding_leg['id']}::{hedge_leg['id']}",
                    "asset": funding_leg["asset"],
                    "funding_market_id": funding_leg["id"],
                    "hedge_market_id": hedge_leg["id"],
                    "funding_exchange": funding_leg["exchange"],
                    "hedge_exchange": hedge_leg["exchange"],
                    "funding_symbol": funding_leg["symbol"],
                    "hedge_symbol": hedge_leg["symbol"],
                    "funding_contract_value": float(funding_leg.get("contract_value") or 1),
                    "hedge_contract_value": float(hedge_leg.get("contract_value") or 1),
                    "funding_side": funding_side,
                    "hedge_side": hedge_side,
                    "funding_pct": funding_rate,
                    "hedge_funding_pct": hedge_rate,
                    "hedge_funding_in_capture_window": hedge_in_capture_window,
                    "hedge_capture_cashflow_pct": hedge_cashflow,
                    "net_funding_pct": net_rate,
                    "interval_hours": int(funding_leg["interval_hours"]),
                    "hedge_interval_hours": int(hedge_leg["interval_hours"]),
                    "next_funding_at": int(funding_leg["next_funding_at"]),
                    "hedge_next_funding_at": int(hedge_leg["next_funding_at"]),
                    "funding_entry_price": executable_top_price(funding_leg, funding_side, "open"),
                    "hedge_entry_price": executable_top_price(hedge_leg, hedge_side, "open"),
                    "combined_spread_bps": float(funding_leg.get("spread_bps") or 0)
                    + float(hedge_leg.get("spread_bps") or 0),
                    "cross_exchange": funding_leg["exchange"] != hedge_leg["exchange"],
                }
            )
        if not candidate_routes:
            continue
        candidate_routes.sort(
            key=lambda route: (
                -route["net_funding_pct"],
                route["combined_spread_bps"],
                not route["cross_exchange"],
            )
        )
        routes.append(candidate_routes[0])
    routes.sort(key=lambda route: abs(route["funding_pct"]), reverse=True)
    return routes


def leg_pnl(entry_price: float, current_price: float, notional: float, side: str) -> float:
    if min(entry_price, current_price, notional) <= 0:
        raise ValueError("Prix et notionnel strictement positifs requis")
    if side not in {"long", "short"}:
        raise ValueError("Sens de position invalide")
    quantity = notional / entry_price
    direction = 1 if side == "long" else -1
    return direction * quantity * (current_price - entry_price)

def open_paper_position(
    route: dict[str, Any],
    total_notional: float,
    leverage: int,
    fee_rates_pct: dict[str, float],
    books: dict[str, dict[str, Any]],
    opened_at: int,
    position_id: str,
) -> dict[str, Any]:
    if total_notional <= 0 or leverage <= 0:
        raise ValueError("Notionnel et levier strictement positifs requis")
    leg_notional = float(total_notional) / 2
    funding_fill = vwap_for_notional(
        books["funding"], route["funding_side"], leg_notional, "open"
    )
    hedge_fill = vwap_for_notional(
        books["hedge"], route["hedge_side"], leg_notional, "open"
    )
    funding_fee_rate = float(fee_rates_pct[route["funding_exchange"]])
    hedge_fee_rate = float(fee_rates_pct[route["hedge_exchange"]])
    entry_fee = (
        funding_fill["notional"] * funding_fee_rate
        + hedge_fill["notional"] * hedge_fee_rate
    ) / 100
    funding_book_at = int(books["funding"].get("timestamp") or opened_at)
    hedge_book_at = int(books["hedge"].get("timestamp") or opened_at)
    return {
        **route,
        "position_id": position_id,
        "opened_at": int(opened_at),
        "funding_book_at": funding_book_at,
        "hedge_book_at": hedge_book_at,
        "execution_skew_ms": abs(funding_book_at - hedge_book_at),
        "leg_notional": leg_notional,
        "total_notional": float(total_notional),
        "leverage": int(leverage),
        "margin_required": float(total_notional) / leverage,
        "funding_entry_price": funding_fill["price"],
        "hedge_entry_price": hedge_fill["price"],
        "funding_quantity": funding_fill["quantity"],
        "hedge_quantity": hedge_fill["quantity"],
        "funding_entry_slippage_bps": funding_fill["slippage_bps"],
        "hedge_entry_slippage_bps": hedge_fill["slippage_bps"],
        "funding_fee_rate_pct": funding_fee_rate,
        "hedge_fee_rate_pct": hedge_fee_rate,
        "entry_fee": entry_fee,
        "confirmed_funding_events": [],
    }



def mark_paper_position(
    position: dict[str, Any], markets_by_id: dict[str, dict[str, Any]]
) -> dict[str, float] | None:
    funding_market = markets_by_id.get(position["funding_market_id"])
    hedge_market = markets_by_id.get(position["hedge_market_id"])
    if not funding_market or not hedge_market:
        return None
    if funding_market.get("data_status") != "live" or hedge_market.get("data_status") != "live":
        return None

    leg_notional = float(position["leg_notional"])
    funding_entry = float(position["funding_entry_price"])
    hedge_entry = float(position["hedge_entry_price"])
    funding_quantity = float(position.get("funding_quantity") or leg_notional / funding_entry)
    hedge_quantity = float(position.get("hedge_quantity") or leg_notional / hedge_entry)
    funding_exit = executable_top_price(funding_market, position["funding_side"], "close")
    hedge_exit = executable_top_price(hedge_market, position["hedge_side"], "close")
    funding_direction = 1 if position["funding_side"] == "long" else -1
    hedge_direction = 1 if position["hedge_side"] == "long" else -1
    funding_pnl = funding_direction * funding_quantity * (funding_exit - funding_entry)
    hedge_pnl = hedge_direction * hedge_quantity * (hedge_exit - hedge_entry)
    market_pnl = funding_pnl + hedge_pnl

    funding_mark = float(funding_market.get("mark_price") or funding_market["perp_price"])
    hedge_mark = float(hedge_market.get("mark_price") or hedge_market["perp_price"])
    projected_funding_leg = funding_cashflow(
        funding_quantity,
        funding_mark,
        float(funding_market["funding_pct"]),
        position["funding_side"],
    )
    projected_hedge_leg = funding_cashflow(
        hedge_quantity,
        hedge_mark,
        float(hedge_market["funding_pct"]),
        position["hedge_side"],
    )
    confirmed_funding = sum(
        float(event["cashflow"]) for event in position.get("confirmed_funding_events", [])
    )
    entry_fee = float(position.get("entry_fee") or 0)
    exit_fee = (
        funding_quantity
        * funding_exit
        * float(position.get("funding_fee_rate_pct") or 0)
        + hedge_quantity
        * hedge_exit
        * float(position.get("hedge_fee_rate_pct") or 0)
    ) / 100
    return {
        "funding_leg_pnl": funding_pnl,
        "hedge_leg_pnl": hedge_pnl,
        "market_pnl": market_pnl,
        "funding_exit_price": funding_exit,
        "hedge_exit_price": hedge_exit,
        "projected_next_funding": projected_funding_leg,
        "projected_hedge_funding": projected_hedge_leg,
        "projected_net_funding": projected_funding_leg + projected_hedge_leg,
        "confirmed_funding": confirmed_funding,
        "entry_fee": entry_fee,
        "estimated_exit_fee": exit_fee,
        "net_if_closed": market_pnl + confirmed_funding - entry_fee - exit_fee,
    }


def close_paper_position(
    position: dict[str, Any], books: dict[str, dict[str, Any]], closed_at: int
) -> dict[str, Any]:
    funding_fill = vwap_for_quantity(
        books["funding"],
        position["funding_side"],
        float(position["funding_quantity"]),
        "close",
    )
    hedge_fill = vwap_for_quantity(
        books["hedge"],
        position["hedge_side"],
        float(position["hedge_quantity"]),
        "close",
    )
    funding_direction = 1 if position["funding_side"] == "long" else -1
    hedge_direction = 1 if position["hedge_side"] == "long" else -1
    funding_pnl = funding_direction * float(position["funding_quantity"]) * (
        funding_fill["price"] - float(position["funding_entry_price"])
    )
    hedge_pnl = hedge_direction * float(position["hedge_quantity"]) * (
        hedge_fill["price"] - float(position["hedge_entry_price"])
    )
    exit_fee = (
        funding_fill["notional"] * float(position["funding_fee_rate_pct"])
        + hedge_fill["notional"] * float(position["hedge_fee_rate_pct"])
    ) / 100
    confirmed_funding = sum(
        float(event["cashflow"]) for event in position.get("confirmed_funding_events", [])
    )
    market_pnl = funding_pnl + hedge_pnl
    net_result = (
        market_pnl
        + confirmed_funding
        - float(position.get("entry_fee") or 0)
        - exit_fee
    )
    return {
        **position,
        "closed_at": int(closed_at),
        "funding_exit_price": funding_fill["price"],
        "hedge_exit_price": hedge_fill["price"],
        "funding_exit_slippage_bps": funding_fill["slippage_bps"],
        "hedge_exit_slippage_bps": hedge_fill["slippage_bps"],
        "funding_leg_pnl": funding_pnl,
        "hedge_leg_pnl": hedge_pnl,
        "market_pnl": market_pnl,
        "confirmed_funding": confirmed_funding,
        "exit_fee": exit_fee,
        "net_result": net_result,
    }
def apply_confirmed_settlements(
    position: dict[str, Any], leg: str, events: list[dict[str, Any]]
) -> int:
    if leg not in {"funding", "hedge"}:
        raise ValueError("Jambe invalide")
    stored = position.setdefault("confirmed_funding_events", [])
    known = {str(event["id"]) for event in stored}
    side = position[f"{leg}_side"]
    quantity = float(position[f"{leg}_quantity"])
    added = 0
    for event in events:
        event_id = f"{leg}:{event['id']}"
        if event_id in known:
            continue
        stored.append(
            {
                **event,
                "id": event_id,
                "leg": leg,
                "side": side,
                "cashflow": funding_cashflow(
                    quantity,
                    float(event["mark_price"]),
                    float(event["funding_pct"]),
                    side,
                ),
            }
        )
        known.add(event_id)
        added += 1
    stored.sort(key=lambda event: int(event["timestamp"]))
    return added
