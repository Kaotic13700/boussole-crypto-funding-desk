"""Pure helpers for the live-market paper portfolio.

The module never sends orders and never invents prices. A route is emitted only
when both legs have a live price in the exchange feed.
"""

from __future__ import annotations

from typing import Any


def build_perp_routes(
    markets: list[dict[str, Any]], threshold_pct: float = 0.4
) -> list[dict[str, Any]]:
    live = [
        market
        for market in markets
        if market.get("data_status") == "live"
        and market.get("perp_price", 0) > 0
        and market.get("perp_long_available")
        and market.get("perp_short_available")
    ]
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for market in live:
        by_asset.setdefault(market["asset"], []).append(market)

    routes: list[dict[str, Any]] = []
    for funding_leg in live:
        if abs(float(funding_leg["funding_pct"])) < threshold_pct:
            continue
        candidates = [
            market
            for market in by_asset.get(funding_leg["asset"], [])
            if market["id"] != funding_leg["id"]
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda market: (
                market["exchange"] == funding_leg["exchange"],
                float(market.get("spread_bps") or 0),
                -float(market.get("depth_usd") or 0),
            )
        )
        hedge_leg = candidates[0]
        funding_side = "short" if funding_leg["funding_pct"] > 0 else "long"
        hedge_side = "long" if funding_side == "short" else "short"
        routes.append(
            {
                "id": f"{funding_leg['id']}::{hedge_leg['id']}",
                "asset": funding_leg["asset"],
                "funding_market_id": funding_leg["id"],
                "hedge_market_id": hedge_leg["id"],
                "funding_exchange": funding_leg["exchange"],
                "hedge_exchange": hedge_leg["exchange"],
                "funding_side": funding_side,
                "hedge_side": hedge_side,
                "funding_pct": float(funding_leg["funding_pct"]),
                "interval_hours": int(funding_leg["interval_hours"]),
                "next_funding_at": int(funding_leg["next_funding_at"]),
                "funding_entry_price": float(funding_leg["perp_price"]),
                "hedge_entry_price": float(hedge_leg["perp_price"]),
                "cross_exchange": funding_leg["exchange"] != hedge_leg["exchange"],
            }
        )
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
    funding_pnl = leg_pnl(
        float(position["funding_entry_price"]),
        float(funding_market["perp_price"]),
        leg_notional,
        position["funding_side"],
    )
    hedge_pnl = leg_pnl(
        float(position["hedge_entry_price"]),
        float(hedge_market["perp_price"]),
        leg_notional,
        position["hedge_side"],
    )
    projected_funding = leg_notional * abs(float(funding_market["funding_pct"])) / 100
    return {
        "funding_leg_pnl": funding_pnl,
        "hedge_leg_pnl": hedge_pnl,
        "market_pnl": funding_pnl + hedge_pnl,
        "projected_next_funding": projected_funding,
    }
