"""Validated public-market scanner for Boussole Crypto Funding Desk.

The module deliberately contains no order execution and no demo data. It only
normalizes public exchange metadata into a strict, inspectable feed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
import threading
import time
from typing import Any, Callable

import httpx


ALLOWED_INTERVALS = {1.0, 4.0, 8.0}
STALE_CACHE_SECONDS = 10 * 60
HTTP_TIMEOUT_SECONDS = 8.0


@dataclass
class SourceResult:
    exchange: str
    markets: list[dict[str, Any]]
    received_at: int
    market_data_at: int | None
    message: str | None = None


_last_good: dict[str, tuple[SourceResult, int]] = {}
_last_good_lock = threading.Lock()


def now_ms() -> int:
    return int(time.time() * 1000)


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def timestamp_ms(value: Any) -> int | None:
    number = finite(value)
    return int(number) if number is not None and number > 0 else None


def require_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label}: réponse vide ou invalide")
    return value


def assert_business_success(payload: dict[str, Any], label: str, success_code: str) -> None:
    if str(payload.get("code", "")) != success_code:
        detail = payload.get("msg") or f"code {payload.get('code', 'absent')}"
        raise ValueError(f"{label}: {detail}")


def within_caps(rate: float, floor_value: Any, cap_value: Any) -> bool:
    floor = finite(floor_value)
    cap = finite(cap_value)
    if floor is not None and rate < floor - 1e-12:
        return False
    if cap is not None and rate > cap + 1e-12:
        return False
    return True


def normalize_asset(value: str) -> str:
    clean = "".join(character for character in value.upper() if character.isalnum())
    if clean == "XBT":
        return "BTC"
    if clean == "XDG":
        return "DOGE"
    return clean


def market_state(funding_pct: float, data_status: str = "live") -> str:
    if data_status == "stale":
        return "PÉRIMÉ"
    magnitude = abs(funding_pct)
    if magnitude >= 0.5:
        return "ALERTE"
    if magnitude >= 0.4:
        return "SURVEILLER"
    return "FLUX"


def kraken_relative_funding_pct(symbol: str, absolute_rate: float, index_price: float) -> float:
    """Convert Kraken's absolute hourly funding to the UI relative percentage.

    PI contracts are inverse; PF contracts are linear. Applying one formula to
    both families silently corrupts the threshold signal.
    """
    if index_price <= 0:
        raise ValueError("Kraken: index invalide")
    if symbol.startswith("PI_"):
        return absolute_rate * index_price * 100
    return absolute_rate / index_price * 100


def kraken_published_relative_pct(ticker: dict[str, Any], field: str) -> float | None:
    """Read Kraken's published relative rate without reconstructing it."""
    rate = finite(ticker.get(field))
    if rate is None:
        return None
    return rate * 100


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "BoussoleFundingDesk/1.0"},
    )


def _json(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Réponse JSON invalide pour {url}")
    return payload


def _parallel_json(client: httpx.Client, urls: dict[str, str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_json, client, url): key for key, url in urls.items()}
        for future in as_completed(futures):
            output[futures[future]] = future.result()
    return output


def fetch_bitget() -> SourceResult:
    base = "https://api.bitget.com"
    with _client() as client:
        payloads = _parallel_json(
            client,
            {
                "funding": f"{base}/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES",
                "margin": f"{base}/api/v3/market/instruments?category=MARGIN",
                "tickers": f"{base}/api/v3/market/tickers?category=USDT-FUTURES",
                "instruments": f"{base}/api/v3/market/instruments?category=USDT-FUTURES",
            },
        )

    for key, label in (
        ("funding", "Bitget funding"),
        ("margin", "Bitget margin"),
        ("tickers", "Bitget tickers"),
        ("instruments", "Bitget instruments"),
    ):
        assert_business_success(payloads[key], label, "00000")

    funding_rows = require_list(payloads["funding"].get("data"), "Bitget funding")
    margin_rows = require_list(payloads["margin"].get("data"), "Bitget margin")
    ticker_rows = require_list(payloads["tickers"].get("data"), "Bitget tickers")
    instrument_rows = require_list(payloads["instruments"].get("data"), "Bitget instruments")

    margin_by_symbol = {row.get("symbol"): row for row in margin_rows}
    ticker_by_symbol = {row.get("symbol"): row for row in ticker_rows}
    live_perps = {
        row.get("symbol")
        for row in instrument_rows
        if row.get("status") == "online" and row.get("type") in (None, "", "perpetual")
    }
    received_at = now_ms()
    markets: list[dict[str, Any]] = []

    for row in funding_rows:
        symbol = str(row.get("symbol", ""))
        rate = finite(row.get("fundingRate"))
        interval = finite(row.get("fundingRateInterval"))
        deadline = timestamp_ms(row.get("nextUpdate"))
        ticker = ticker_by_symbol.get(symbol)
        if (
            not symbol
            or rate is None
            or interval not in ALLOWED_INTERVALS
            or deadline is None
            or deadline <= received_at
            or ticker is None
            or symbol not in live_perps
        ):
            continue
        if not within_caps(
            rate,
            row.get("minFundingRate", row.get("fundingRateFloor")),
            row.get("maxFundingRate", row.get("fundingRateCap")),
        ):
            continue

        last = finite(ticker.get("lastPrice")) or 0.0
        ask = finite(ticker.get("ask1Price")) or last
        bid = finite(ticker.get("bid1Price")) or last
        if min(last, ask, bid) <= 0:
            continue
        mid = (ask + bid) / 2
        market_ts = timestamp_ms(ticker.get("ts"))
        margin = margin_by_symbol.get(symbol) or {}
        margin_online = margin.get("status") == "online"
        margin_long = margin_online and margin.get("isIsolatedQuotedBorrowable") == "YES"
        margin_short = margin_online and margin.get("isIsolatedBaseBorrowable") == "YES"
        max_leverage = next(
            (
                value
                for value in (
                    finite(margin.get("maxLeverage")),
                    finite(margin.get("maxIsolatedLeverage")),
                    finite(margin.get("maxCrossedLeverage")),
                )
                if value is not None
            ),
            None,
        )
        funding_pct = rate * 100
        markets.append(
            {
                "id": f"bitget-{symbol.lower()}",
                "asset": symbol.removesuffix("USDT"),
                "symbol": symbol,
                "exchange": "Bitget",
                "direction": "positive" if funding_pct >= 0 else "negative",
                "funding_pct": funding_pct,
                "predicted_funding_pct": None,
                "prediction_source": "not-published-by-exchange",
                "funding_basis": "current",
                "interval_hours": int(interval),
                "interval_source": "API Bitget · fundingRateInterval",
                "next_funding_at": deadline,
                "deadline_source": "exchange",
                "perp_price": last,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": finite(ticker.get("markPrice")) or last,
                "contract_value": 1.0,
                "spread_bps": abs(ask - bid) / mid * 10_000,
                "margin_long_available": bool(margin_long),
                "margin_short_available": bool(margin_short),
                "max_margin_leverage": max_leverage,
                "depth_usd": finite(ticker.get("turnover24h")) or 0.0,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": market_ts,
                "market_timestamp_source": "exchange-ticker" if market_ts else "unavailable",
                "regional_eligibility": "not-applicable",
                "perp_long_available": True,
                "perp_short_available": True,
                "perp_availability_source": "Catalogue Bitget USDT-FUTURES · instrument online",
                "margin_product_listed": bool(margin_online),
                "margin_long_source": (
                    "Catalogue Bitget MARGIN · quote empruntable"
                    if margin_long
                    else "Catalogue Bitget MARGIN · quote non empruntable ou paire absente"
                ),
                "margin_short_source": (
                    "Catalogue Bitget MARGIN · base empruntable"
                    if margin_short
                    else "Catalogue Bitget MARGIN · base non empruntable ou paire absente"
                ),
            }
        )

    if not markets:
        raise ValueError("Bitget: aucun PERP valide après contrôle")
    markets.sort(key=lambda item: abs(item["funding_pct"]), reverse=True)
    market_times = [item["market_data_at"] for item in markets if item["market_data_at"]]
    return SourceResult("Bitget", markets, received_at, min(market_times) if market_times else None)


def fetch_kraken() -> SourceResult:
    with _client() as client:
        payloads = _parallel_json(
            client,
            {
                "futures": "https://futures.kraken.com/derivatives/api/v3/tickers",
                "pairs": "https://api.kraken.com/0/public/AssetPairs",
            },
        )
    futures = payloads["futures"]
    pairs_payload = payloads["pairs"]
    if futures.get("result") != "success":
        raise ValueError(f"Kraken Futures: statut {futures.get('result', 'absent')}")
    errors = pairs_payload.get("error") or []
    if errors:
        raise ValueError(f"Kraken Spot: {', '.join(map(str, errors))}")
    tickers = require_list(futures.get("tickers"), "Kraken Futures")
    pairs = pairs_payload.get("result")
    if not isinstance(pairs, dict) or not pairs:
        raise ValueError("Kraken Spot: catalogue vide ou invalide")

    margin_by_asset: dict[str, dict[str, Any]] = {}
    for pair in pairs.values():
        wsname = str(pair.get("wsname", ""))
        if not wsname.endswith("/USD"):
            continue
        asset = normalize_asset(wsname.split("/")[0])
        buy_levels = [number for value in pair.get("leverage_buy", []) if (number := finite(value)) is not None]
        sell_levels = [number for value in pair.get("leverage_sell", []) if (number := finite(value)) is not None]
        existing = margin_by_asset.get(
            asset,
            {"long": False, "short": False, "leverage": None, "long_pairs": [], "short_pairs": []},
        )
        all_levels = buy_levels + sell_levels
        leverage_candidates = all_levels + ([existing["leverage"]] if existing["leverage"] is not None else [])
        margin_by_asset[asset] = {
            "long": bool(existing["long"] or buy_levels),
            "short": bool(existing["short"] or sell_levels),
            "leverage": max(leverage_candidates) if leverage_candidates else None,
            "long_pairs": sorted(set(existing["long_pairs"] + ([wsname] if buy_levels else []))),
            "short_pairs": sorted(set(existing["short_pairs"] + ([wsname] if sell_levels else []))),
        }

    try:
        server_time = int(datetime.fromisoformat(str(futures.get("serverTime", "")).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError as error:
        raise ValueError("Kraken Futures: horloge serveur invalide") from error
    next_hour = (server_time // 3_600_000 + 1) * 3_600_000
    received_at = now_ms()
    markets: list[dict[str, Any]] = []

    for ticker in tickers:
        if ticker.get("tag") != "perpetual" or ticker.get("suspended") is True:
            continue
        symbol = str(ticker.get("symbol", ""))
        current_pct = kraken_published_relative_pct(ticker, "relativeFundingRate")
        if not symbol or current_pct is None:
            continue
        predicted_pct = kraken_published_relative_pct(ticker, "relativeFundingRatePrediction")
        pair_name = str(ticker.get("pair") or symbol).split(":")[0]
        asset = normalize_asset(pair_name.removeprefix("PF_").removeprefix("PI_").removesuffix("USD"))
        margin = margin_by_asset.get(
            asset,
            {"long": False, "short": False, "leverage": None, "long_pairs": [], "short_pairs": []},
        )
        last = finite(ticker.get("last")) or finite(ticker.get("markPrice")) or 0.0
        ask = finite(ticker.get("ask"))
        bid = finite(ticker.get("bid"))
        if last <= 0:
            continue
        mid = (ask + bid) / 2 if ask and bid else last
        markets.append(
            {
                "id": f"kraken-{symbol.lower()}",
                "asset": asset,
                "symbol": symbol,
                "exchange": "Kraken",
                "direction": "positive" if current_pct >= 0 else "negative",
                "funding_pct": current_pct,
                "predicted_funding_pct": predicted_pct,
                "prediction_source": "kraken-relativeFundingRatePrediction" if predicted_pct is not None else "not-published-by-exchange",
                "funding_basis": "published-relative-rate",
                "interval_hours": 1,
                "interval_source": "Spécification Kraken Perpetual · auto-roll 1 h",
                "next_funding_at": next_hour,
                "deadline_source": "kraken-server-clock+official-hourly-boundary",
                "perp_price": last,
                "bid_price": bid or last,
                "ask_price": ask or last,
                "mark_price": finite(ticker.get("markPrice")) or last,
                "contract_value": 1.0,
                "spread_bps": abs(ask - bid) / mid * 10_000 if ask and bid else 0.0,
                "margin_long_available": bool(margin["long"]),
                "margin_short_available": bool(margin["short"]),
                "max_margin_leverage": margin["leverage"],
                "depth_usd": finite(ticker.get("volumeQuote")) or 0.0,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": None,
                "market_timestamp_source": "unavailable",
                "regional_eligibility": "unverified",
                "perp_long_available": True,
                "perp_short_available": True,
                "perp_availability_source": "Catalogue Kraken Futures · perpetual actif",
                "margin_product_listed": bool(margin["long"] or margin["short"]),
                "margin_long_source": (
                    f"Kraken AssetPairs leverage_buy · {', '.join(margin['long_pairs'])}"
                    if margin["long"]
                    else "Kraken AssetPairs · aucun leverage_buy USD"
                ),
                "margin_short_source": (
                    f"Kraken AssetPairs leverage_sell · {', '.join(margin['short_pairs'])}"
                    if margin["short"]
                    else "Kraken AssetPairs · aucun leverage_sell USD"
                ),
            }
        )

    if not markets:
        raise ValueError("Kraken Futures: aucun PERP valide après contrôle")
    markets.sort(key=lambda item: abs(item["funding_pct"]), reverse=True)
    return SourceResult(
        "Kraken",
        markets,
        received_at,
        None,
        "Catalogue public Kraken Futures ; éligibilité EU à confirmer par compte.",
    )


def fetch_blofin() -> SourceResult:
    base = "https://openapi.blofin.com"
    with _client() as client:
        payloads = _parallel_json(
            client,
            {
                "instruments": f"{base}/api/v1/market/instruments",
                "tickers": f"{base}/api/v1/market/tickers",
                "funding": f"{base}/api/v1/market/funding-rate",
            },
        )
    for key, label in (
        ("instruments", "BloFin instruments"),
        ("tickers", "BloFin tickers"),
        ("funding", "BloFin funding"),
    ):
        assert_business_success(payloads[key], label, "0")

    instruments = require_list(payloads["instruments"].get("data"), "BloFin instruments")
    tickers = require_list(payloads["tickers"].get("data"), "BloFin tickers")
    funding_rows = require_list(payloads["funding"].get("data"), "BloFin funding")
    instrument_by_id = {row.get("instId"): row for row in instruments if row.get("state") == "live"}
    ticker_by_id = {row.get("instId"): row for row in tickers}
    received_at = now_ms()
    markets: list[dict[str, Any]] = []

    for row in funding_rows:
        instrument_id = str(row.get("instId", ""))
        instrument = instrument_by_id.get(instrument_id)
        ticker = ticker_by_id.get(instrument_id)
        rate = finite(row.get("fundingRate"))
        deadline = timestamp_ms(row.get("fundingTime"))
        raw_interval = finite(row.get("fundingInterval"))
        if (
            not instrument_id
            or instrument is None
            or ticker is None
            or rate is None
            or deadline is None
            or deadline <= received_at
            or raw_interval is None
        ):
            continue
        if not within_caps(rate, row.get("fundingRateFloor"), row.get("fundingRateCap")):
            continue
        interval = raw_interval / 60 if row.get("fundingIntervalUnit") == "minute" else raw_interval
        if interval not in ALLOWED_INTERVALS:
            continue
        last = finite(ticker.get("last")) or finite(ticker.get("markPrice")) or 0.0
        contract_value = finite(instrument.get("contractValue"))
        if contract_value is None or contract_value <= 0:
            continue

        ask = finite(ticker.get("askPrice")) or last
        bid = finite(ticker.get("bidPrice")) or last
        if min(last, ask, bid) <= 0:
            continue
        mid = (ask + bid) / 2
        market_ts = timestamp_ms(ticker.get("ts"))
        funding_pct = rate * 100
        markets.append(
            {
                "id": f"blofin-{instrument_id.lower()}",
                "asset": str(instrument.get("baseCurrency", "")),
                "symbol": instrument_id,
                "exchange": "BloFin",
                "direction": "positive" if funding_pct >= 0 else "negative",
                "funding_pct": funding_pct,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": finite(ticker.get("markPrice")) or last,
                "contract_value": contract_value,
                "predicted_funding_pct": None,
                "prediction_source": "not-published-by-exchange",
                "funding_basis": "current",
                "interval_hours": int(interval),
                "interval_source": "API BloFin · fundingInterval + fundingIntervalUnit",
                "next_funding_at": deadline,
                "deadline_source": "exchange",
                "perp_price": last,
                "spread_bps": abs(ask - bid) / mid * 10_000,
                "margin_long_available": False,
                "margin_short_available": False,
                "max_margin_leverage": finite(instrument.get("maxLeverage")),
                "depth_usd": (finite(ticker.get("volCurrency24h")) or 0.0) * last,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": market_ts,
                "market_timestamp_source": "exchange-ticker" if market_ts else "unavailable",
                "regional_eligibility": "not-applicable",
                "perp_long_available": True,
                "perp_short_available": True,
                "perp_availability_source": "Catalogue BloFin Futures · instrument live",
                "margin_product_listed": False,
                "margin_long_source": "Aucun produit MARGIN vérifiable dans le catalogue public BloFin",
                "margin_short_source": "Aucun produit MARGIN vérifiable dans le catalogue public BloFin",
            }
        )

    if not markets:
        raise ValueError("BloFin: aucun PERP valide après contrôle")
    markets.sort(key=lambda item: abs(item["funding_pct"]), reverse=True)
    market_times = [item["market_data_at"] for item in markets if item["market_data_at"]]
    return SourceResult("BloFin", markets, received_at, min(market_times) if market_times else None)


def _source_health(result: SourceResult, fetched_at: int, status: str, message: str | None = None) -> dict[str, Any]:
    return {
        "exchange": result.exchange,
        "status": status,
        "markets": len(result.markets),
        "source_updated_at": result.received_at,
        "age_ms": max(0, fetched_at - result.received_at),
        "market_data_age_ms": (
            max(0, fetched_at - result.market_data_at) if result.market_data_at is not None else None
        ),
        "message": message if message is not None else result.message,
    }


def collect_feed() -> dict[str, Any]:
    started_at = now_ms()
    fetchers: dict[str, Callable[[], SourceResult]] = {
        "Bitget": fetch_bitget,
        "Kraken": fetch_kraken,
        "BloFin": fetch_blofin,
    }
    completed: dict[str, SourceResult | Exception] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetcher): exchange for exchange, fetcher in fetchers.items()}
        for future in as_completed(futures):
            exchange = futures[future]
            try:
                completed[exchange] = future.result()
            except Exception as error:  # one exchange must never erase the other two
                completed[exchange] = error

    fetched_at = now_ms()
    markets: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    live_sources = 0

    for exchange in ("Bitget", "Kraken", "BloFin"):
        result = completed[exchange]
        if isinstance(result, SourceResult):
            live_sources += 1
            with _last_good_lock:
                _last_good[exchange] = (deepcopy(result), fetched_at)
            markets.extend(result.markets)
            sources.append(_source_health(result, fetched_at, "live"))
            continue

        error_message = str(result)
        with _last_good_lock:
            cached_entry = deepcopy(_last_good.get(exchange))
        if cached_entry is not None:
            cached, cached_at = cached_entry
            cache_age = fetched_at - cached_at
            if cache_age <= STALE_CACHE_SECONDS * 1000:
                cached.markets = [
                    {**market, "data_status": "stale"}
                    for market in cached.markets
                    if market["next_funding_at"] > fetched_at
                ]
                markets.extend(cached.markets)
                health = _source_health(cached, fetched_at, "stale", error_message)
                health["age_ms"] = cache_age
                sources.append(health)
                continue
        sources.append(
            {
                "exchange": exchange,
                "status": "error",
                "markets": 0,
                "source_updated_at": None,
                "age_ms": None,
                "market_data_age_ms": None,
                "message": error_message,
            }
        )

    markets = [
        market
        for market in markets
        if float(market["interval_hours"]) in ALLOWED_INTERVALS
        and math.isfinite(float(market["funding_pct"]))
        and market["next_funding_at"] > fetched_at
    ]
    markets.sort(
        key=lambda item: (
            0 if item["data_status"] == "live" else 1,
            -abs(item["funding_pct"]),
            item["exchange"],
        )
    )
    mode = "live" if live_sources == 3 else "hybrid" if live_sources > 0 else "offline"
    return {
        "mode": mode,
        "markets": markets,
        "fetched_at": fetched_at,
        "latency_ms": fetched_at - started_at,
        "sources": sources,
    }
