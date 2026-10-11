"""Validated public-market scanner for Boussole Crypto Funding Desk.

The module deliberately contains no order execution and no demo data. It only
normalizes public exchange metadata into a strict, inspectable feed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import math
import threading
import time
from typing import Any, Callable

import httpx


ALLOWED_INTERVALS = {1.0, 4.0, 8.0}
STALE_CACHE_SECONDS = 10 * 60
HTTP_TIMEOUT_SECONDS = 8.0
MAX_MARKET_DATA_AGE_MS = 3 * 60 * 1000
MAX_FUTURE_CLOCK_SKEW_MS = 30 * 1000
ENHANCED_VERIFICATION_THRESHOLD_PCT = 0.4
KRAKEN_INVERSE_HOURLY_CAP_PCT = 0.25


@dataclass
class SourceResult:
    exchange: str
    markets: list[dict[str, Any]]
    received_at: int
    market_data_at: int | None
    message: str | None = None
    rejections: list[dict[str, Any]] = field(default_factory=list)


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


def funding_rates_match(
    first: Any,
    second: Any,
    absolute_tolerance: float = 1e-10,
    relative_tolerance: float = 1e-7,
) -> bool:
    """Require two official fields to describe the same current funding rate."""
    left = finite(first)
    right = finite(second)
    return (
        left is not None
        and right is not None
        and math.isclose(
            left,
            right,
            rel_tol=relative_tolerance,
            abs_tol=absolute_tolerance,
        )
    )


def fresh_market_timestamp(
    timestamp: Any,
    reference_ms: int,
    max_age_ms: int = MAX_MARKET_DATA_AGE_MS,
) -> bool:
    value = timestamp_ms(timestamp)
    if value is None:
        return False
    return reference_ms - max_age_ms <= value <= reference_ms + MAX_FUTURE_CLOCK_SKEW_MS


def rejection(
    exchange: str,
    symbol: str,
    reason: str,
    funding_pct: float | None = None,
) -> dict[str, Any]:
    return {
        "exchange": exchange,
        "symbol": symbol,
        "reason": reason,
        "funding_pct": funding_pct,
    }


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


def kraken_open_interest_usd(
    symbol: str, open_interest: float | None, mark_price: float
) -> float | None:
    if open_interest is None or open_interest < 0 or mark_price <= 0:
        return None
    if symbol.startswith("PI_"):
        return open_interest
    return open_interest * mark_price



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


def _json_any(client: httpx.Client, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def _parallel_json(client: httpx.Client, urls: dict[str, str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_json, client, url): key for key, url in urls.items()}
        for future in as_completed(futures):
            output[futures[future]] = future.result()
    return output


def _post_json_snapshot(
    client: httpx.Client, url: str, body: dict[str, Any]
) -> tuple[Any, int]:
    """POST a public endpoint and retain its HTTP server clock as evidence."""
    response = client.post(url, json=body)
    response.raise_for_status()
    payload = response.json()
    try:
        server_ms = int(parsedate_to_datetime(response.headers["date"]).timestamp() * 1000)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Horloge HTTP officielle absente pour {url}") from error
    return payload, server_ms


def gate_open_interest_usd(
    total_size: float | None, quanto_multiplier: float, mark_price: float
) -> float | None:
    if total_size is None or min(quanto_multiplier, mark_price) <= 0:
        return None
    return total_size * quanto_multiplier * mark_price


def next_hour_boundary_ms(timestamp: int) -> int:
    return (timestamp // 3_600_000 + 1) * 3_600_000


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
    instrument_by_symbol = {
        row.get("symbol"): row
        for row in instrument_rows
        if row.get("status") == "online" and row.get("type") in (None, "", "perpetual")
    }
    received_at = now_ms()
    markets: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for row in funding_rows:
        symbol = str(row.get("symbol", ""))
        rate = finite(row.get("fundingRate"))
        interval = finite(row.get("fundingRateInterval"))
        deadline = timestamp_ms(row.get("nextUpdate"))
        ticker = ticker_by_symbol.get(symbol)
        instrument = instrument_by_symbol.get(symbol)
        if (
            not symbol
            or rate is None
            or interval not in ALLOWED_INTERVALS
            or deadline is None
            or deadline <= received_at
            or ticker is None
            or instrument is None
        ):
            continue
        funding_pct = rate * 100
        if not within_caps(
            rate,
            row.get("minFundingRate", row.get("fundingRateFloor")),
            row.get("maxFundingRate", row.get("fundingRateCap")),
        ):
            rejections.append(rejection("Bitget", symbol, "taux hors bornes officielles", funding_pct))
            continue
        if not funding_rates_match(rate, ticker.get("fundingRate")):
            rejections.append(
                rejection("Bitget", symbol, "désaccord funding ↔ ticker officiel", funding_pct)
            )
            continue
        if not funding_rates_match(interval, instrument.get("fundInterval"), absolute_tolerance=1e-8):
            rejections.append(
                rejection("Bitget", symbol, "désaccord de tranche funding ↔ contrat", funding_pct)
            )
            continue

        last = finite(ticker.get("lastPrice")) or 0.0
        ask = finite(ticker.get("ask1Price")) or last
        bid = finite(ticker.get("bid1Price")) or last
        if min(last, ask, bid) <= 0:
            continue
        mid = (ask + bid) / 2
        market_ts = timestamp_ms(ticker.get("ts"))
        if not fresh_market_timestamp(market_ts, received_at):
            rejections.append(
                rejection("Bitget", symbol, "ticker officiel absent ou périmé", funding_pct)
            )
            continue
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
        mark_price = finite(ticker.get("markPrice")) or last
        volume_24h_usd = finite(ticker.get("turnover24h"))
        open_interest = finite(ticker.get("openInterest"))
        open_interest_usd = None if open_interest is None else open_interest * mark_price
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
                "funding_verified": True,
                "funding_verification_source": "Bitget current-fund-rate + ticker + contrat",
                "verification_level": "cross-endpoint",
                "interval_hours": int(interval),
                "interval_source": "API Bitget · fundingRateInterval",
                "next_funding_at": deadline,
                "deadline_source": "exchange",
                "perp_price": last,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": mark_price,
                "contract_value": 1.0,
                "spread_bps": abs(ask - bid) / mid * 10_000,
                "margin_long_available": bool(margin_long),
                "margin_short_available": bool(margin_short),
                "max_margin_leverage": max_leverage,
                "depth_usd": volume_24h_usd or 0.0,
                "open_interest_usd": open_interest_usd,
                "open_interest_native": open_interest,
                "volume_24h_native": volume_24h_usd,
                "volume_24h_usd": volume_24h_usd,
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
    return SourceResult(
        "Bitget",
        markets,
        received_at,
        min(market_times) if market_times else None,
        rejections=rejections,
    )


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
    if not fresh_market_timestamp(server_time, received_at, max_age_ms=30_000):
        raise ValueError("Kraken Futures: horloge serveur périmée ou incohérente")
    markets: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for ticker in tickers:
        if ticker.get("tag") != "perpetual" or ticker.get("suspended") is True:
            continue
        symbol = str(ticker.get("symbol", ""))
        current_pct = kraken_published_relative_pct(ticker, "relativeFundingRate")
        if not symbol or current_pct is None:
            continue
        index_price = finite(ticker.get("indexPrice"))
        absolute_rate = finite(ticker.get("fundingRate"))
        if index_price is None or absolute_rate is None:
            rejections.append(
                rejection("Kraken", symbol, "preuve taux absolu/indice absente", current_pct)
            )
            continue
        reconstructed_pct = kraken_relative_funding_pct(symbol, absolute_rate, index_price)
        if not funding_rates_match(
            current_pct,
            reconstructed_pct,
            absolute_tolerance=0.002,
            relative_tolerance=0.02,
        ):
            rejections.append(
                rejection("Kraken", symbol, "désaccord taux relatif ↔ taux absolu/indice", current_pct)
            )
            continue
        if symbol.startswith("PI_") and abs(current_pct) > KRAKEN_INVERSE_HOURLY_CAP_PCT + 1e-9:
            rejections.append(
                rejection("Kraken", symbol, "taux inverse hors borne documentaire ±0,25 %/h", current_pct)
            )
            continue

        predicted_pct = kraken_published_relative_pct(ticker, "relativeFundingRatePrediction")
        predicted_absolute = finite(ticker.get("fundingRatePrediction"))
        if predicted_pct is not None and predicted_absolute is not None:
            reconstructed_prediction = kraken_relative_funding_pct(
                symbol, predicted_absolute, index_price
            )
            if not funding_rates_match(
                predicted_pct,
                reconstructed_prediction,
                absolute_tolerance=0.002,
                relative_tolerance=0.02,
            ):
                predicted_pct = None
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
        mark_price = finite(ticker.get("markPrice")) or last
        volume_24h_usd = finite(ticker.get("volumeQuote"))
        open_interest = finite(ticker.get("openInterest"))
        open_interest_usd = kraken_open_interest_usd(symbol, open_interest, mark_price)
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
                "funding_verified": True,
                "funding_verification_source": "Kraken relatif + absolu/index + spécification contrat",
                "verification_level": "cross-field+documented-cap",
                "interval_hours": 1,
                "interval_source": "Spécification Kraken Perpetual · auto-roll 1 h",
                "next_funding_at": next_hour,
                "deadline_source": "kraken-server-clock+official-hourly-boundary",
                "perp_price": last,
                "bid_price": bid or last,
                "ask_price": ask or last,
                "mark_price": mark_price,
                "contract_value": 1.0,
                "spread_bps": abs(ask - bid) / mid * 10_000 if ask and bid else 0.0,
                "margin_long_available": bool(margin["long"]),
                "margin_short_available": bool(margin["short"]),
                "max_margin_leverage": margin["leverage"],
                "depth_usd": volume_24h_usd or 0.0,
                "open_interest_usd": open_interest_usd,
                "open_interest_native": open_interest,
                "volume_24h_native": volume_24h_usd,
                "volume_24h_usd": volume_24h_usd,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": server_time,
                "market_timestamp_source": "exchange-server-time",
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
        server_time,
        "Catalogue public Kraken Futures ; éligibilité EU à confirmer par compte.",
        rejections=rejections,
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
                "open_interest": f"{base}/api/v1/market/open-interest",
            },
        )
    for key, label in (
        ("instruments", "BloFin instruments"),
        ("tickers", "BloFin tickers"),
        ("funding", "BloFin funding"),
        ("open_interest", "BloFin open interest"),
    ):
        assert_business_success(payloads[key], label, "0")

    instruments = require_list(payloads["instruments"].get("data"), "BloFin instruments")
    tickers = require_list(payloads["tickers"].get("data"), "BloFin tickers")
    funding_rows = require_list(payloads["funding"].get("data"), "BloFin funding")
    instrument_by_id = {row.get("instId"): row for row in instruments if row.get("state") == "live"}
    open_interest_rows = require_list(payloads["open_interest"].get("data"), "BloFin open interest")
    open_interest_by_id = {row.get("instId"): row for row in open_interest_rows}
    ticker_by_id = {row.get("instId"): row for row in tickers}

    enhanced_ids = {
        str(row.get("instId", ""))
        for row in funding_rows
        if (
            (rate := finite(row.get("fundingRate"))) is not None
            and abs(rate * 100) >= ENHANCED_VERIFICATION_THRESHOLD_PCT
        )
    }
    enhanced_confirmation: dict[str, float | None] = {}
    if enhanced_ids:
        def confirm_rate(instrument_id: str) -> tuple[str, float | None]:
            try:
                with _client() as confirmation_client:
                    payload = _json(
                        confirmation_client,
                        f"{base}/api/v1/market/funding-rate?instId={instrument_id}",
                    )
                assert_business_success(payload, f"BloFin confirmation {instrument_id}", "0")
                rows = require_list(payload.get("data"), f"BloFin confirmation {instrument_id}")
                exact = next(
                    (item for item in rows if item.get("instId") == instrument_id),
                    None,
                )
                return instrument_id, finite(exact.get("fundingRate")) if exact else None
            except Exception:
                return instrument_id, None

        with ThreadPoolExecutor(max_workers=min(4, len(enhanced_ids))) as pool:
            futures = [pool.submit(confirm_rate, instrument_id) for instrument_id in enhanced_ids]
            for future in as_completed(futures):
                instrument_id, confirmed_rate = future.result()
                enhanced_confirmation[instrument_id] = confirmed_rate

    received_at = now_ms()
    markets: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

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
        funding_pct = rate * 100
        if not within_caps(rate, row.get("fundingRateFloor"), row.get("fundingRateCap")):
            rejections.append(rejection("BloFin", instrument_id, "taux hors bornes officielles", funding_pct))
            continue
        if instrument_id in enhanced_ids and not funding_rates_match(
            rate, enhanced_confirmation.get(instrument_id)
        ):
            rejections.append(
                rejection("BloFin", instrument_id, "seconde lecture officielle non concordante", funding_pct)
            )
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
        if not fresh_market_timestamp(market_ts, received_at):
            rejections.append(
                rejection("BloFin", instrument_id, "ticker officiel absent ou périmé", funding_pct)
            )
            continue
        mark_price = finite(ticker.get("markPrice")) or last
        volume_24h_native = finite(ticker.get("volCurrency24h"))
        volume_24h_usd = (volume_24h_native or 0.0) * mark_price
        open_interest_row = open_interest_by_id.get(instrument_id) or {}
        open_interest_currency = finite(open_interest_row.get("openInterestCurrency"))
        open_interest_usd = None if open_interest_currency is None else open_interest_currency * mark_price
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
                "mark_price": mark_price,
                "contract_value": contract_value,
                "predicted_funding_pct": None,
                "prediction_source": "not-published-by-exchange",
                "funding_basis": "current",
                "funding_verified": True,
                "funding_verification_source": (
                    "BloFin funding-rate relu + contrat/ticker/cap"
                    if instrument_id in enhanced_ids
                    else "BloFin funding-rate + contrat/ticker/cap"
                ),
                "verification_level": (
                    "enhanced-double-read"
                    if instrument_id in enhanced_ids
                    else "official-endpoint+metadata"
                ),
                "interval_hours": int(interval),
                "interval_source": "API BloFin · fundingInterval + fundingIntervalUnit",
                "next_funding_at": deadline,
                "deadline_source": "exchange",
                "perp_price": last,
                "spread_bps": abs(ask - bid) / mid * 10_000,
                "margin_long_available": False,
                "margin_short_available": False,
                "max_margin_leverage": finite(instrument.get("maxLeverage")),
                "depth_usd": volume_24h_usd,
                "open_interest_usd": open_interest_usd,
                "open_interest_native": open_interest_currency,
                "volume_24h_native": volume_24h_native,
                "volume_24h_usd": volume_24h_usd,
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
    return SourceResult(
        "BloFin",
        markets,
        received_at,
        min(market_times) if market_times else None,
        rejections=rejections,
    )


def fetch_gateio() -> SourceResult:
    """Read Gate USDT perpetuals and its public spot-margin catalogue."""
    base = "https://api.gateio.ws/api/v4"
    urls = {
        "contracts": f"{base}/futures/usdt/contracts",
        "tickers": f"{base}/futures/usdt/tickers",
        "margin": f"{base}/margin/currency_pairs",
    }
    with _client() as client, ThreadPoolExecutor(max_workers=3) as pool:
        pending = {pool.submit(_json_any, client, url): key for key, url in urls.items()}
        payloads = {key: future.result() for future, key in pending.items()}
    contracts = require_list(payloads["contracts"], "Gate.io contrats")
    tickers = require_list(payloads["tickers"], "Gate.io tickers")
    margin_rows = require_list(payloads["margin"], "Gate.io margin")

    ticker_by_symbol = {str(row.get("contract", "")): row for row in tickers}
    margin_by_symbol = {
        str(row.get("id", "")): row
        for row in margin_rows
        if int(finite(row.get("status")) or 0) == 1
    }
    received_at = now_ms()
    markets: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for contract in contracts:
        symbol = str(contract.get("name", ""))
        ticker = ticker_by_symbol.get(symbol)
        rate = finite(contract.get("funding_rate"))
        interval_seconds = finite(contract.get("funding_interval"))
        next_apply_seconds = timestamp_ms(contract.get("funding_next_apply"))
        if (
            not symbol
            or ticker is None
            or rate is None
            or interval_seconds is None
            or next_apply_seconds is None
            or contract.get("status") != "trading"
            or contract.get("in_delisting") is True
            or contract.get("is_pre_market") is True
        ):
            continue
        interval = interval_seconds / 3_600
        deadline = next_apply_seconds * 1_000
        if interval not in ALLOWED_INTERVALS or deadline <= received_at:
            continue
        funding_pct = rate * 100
        ticker_rate = ticker.get("funding_rate")
        if not funding_rates_match(rate, ticker_rate):
            rejections.append(
                rejection("Gate.io", symbol, "désaccord contrat ↔ ticker officiel", funding_pct)
            )
            continue
        funding_limit = finite(contract.get("funding_rate_limit"))
        if funding_limit is not None and abs(rate) > funding_limit + 1e-12:
            rejections.append(
                rejection("Gate.io", symbol, "taux hors limite publiée", funding_pct)
            )
            continue

        last = finite(ticker.get("last")) or finite(contract.get("last_price")) or 0.0
        bid = finite(ticker.get("highest_bid"))
        ask = finite(ticker.get("lowest_ask"))
        mark_price = finite(ticker.get("mark_price")) or finite(contract.get("mark_price")) or 0.0
        multiplier = finite(contract.get("quanto_multiplier")) or 0.0
        if min(last, bid or 0.0, ask or 0.0, mark_price, multiplier) <= 0 or bid >= ask:
            continue
        margin = margin_by_symbol.get(symbol) or {}
        margin_listed = bool(margin)
        margin_leverage = finite(margin.get("leverage")) if margin_listed else None
        total_size = finite(ticker.get("total_size"))
        open_interest_usd = gate_open_interest_usd(total_size, multiplier, mark_price)
        volume_24h_usd = finite(ticker.get("volume_24h_quote"))
        volume_24h_native = finite(ticker.get("volume_24h_base"))
        predicted = finite(ticker.get("funding_rate_indicative"))
        taker_fee = finite(contract.get("taker_fee_rate"))
        mid = (bid + ask) / 2
        asset = normalize_asset(str(margin.get("base") or symbol.removesuffix("_USDT")))
        markets.append(
            {
                "id": f"gateio-{symbol.lower()}",
                "asset": asset,
                "symbol": symbol,
                "exchange": "Gate.io",
                "direction": "positive" if funding_pct >= 0 else "negative",
                "funding_pct": funding_pct,
                "predicted_funding_pct": predicted * 100 if predicted is not None else None,
                "prediction_source": "API Gate.io · funding_rate_indicative",
                "funding_basis": "current",
                "funding_verified": True,
                "funding_verification_source": "Gate.io contrat + ticker + limite du contrat",
                "verification_level": "cross-endpoint",
                "interval_hours": int(interval),
                "interval_source": "API Gate.io · funding_interval",
                "next_funding_at": deadline,
                "deadline_source": "exchange",
                "perp_price": last,
                "bid_price": bid,
                "ask_price": ask,
                "mark_price": mark_price,
                "contract_value": multiplier,
                "spread_bps": (ask - bid) / mid * 10_000,
                "taker_fee_pct": taker_fee * 100 if taker_fee is not None else None,
                "fee_source": "API Gate.io · taker_fee_rate du contrat",
                "margin_long_available": margin_listed,
                "margin_short_available": margin_listed,
                "max_margin_leverage": margin_leverage,
                "depth_usd": volume_24h_usd or 0.0,
                "open_interest_usd": open_interest_usd,
                "open_interest_native": (
                    total_size * multiplier if total_size is not None else None
                ),
                "volume_24h_native": volume_24h_native,
                "volume_24h_usd": volume_24h_usd,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": received_at,
                "market_timestamp_source": "official-http-receipt",
                "regional_eligibility": "unverified",
                "perp_long_available": True,
                "perp_short_available": True,
                "perp_availability_source": "Catalogue Gate.io Futures USDT · contrat trading",
                "margin_product_listed": margin_listed,
                "margin_long_source": (
                    "Catalogue Gate.io spot margin · paire active (emprunt quote à confirmer au compte)"
                    if margin_listed
                    else "Catalogue Gate.io spot margin · paire absente"
                ),
                "margin_short_source": (
                    "Catalogue Gate.io spot margin · paire active (emprunt base à confirmer au compte)"
                    if margin_listed
                    else "Catalogue Gate.io spot margin · paire absente"
                ),
            }
        )

    if not markets:
        raise ValueError("Gate.io: aucun PERP valide après contrôle")
    markets.sort(key=lambda item: abs(item["funding_pct"]), reverse=True)
    return SourceResult(
        "Gate.io",
        markets,
        received_at,
        received_at,
        "Disponibilité d'emprunt margin à confirmer au niveau du compte.",
        rejections=rejections,
    )


def fetch_hyperliquid() -> SourceResult:
    """Read the official HyperCore perpetual universe without inventing a BBO."""
    endpoint = "https://api.hyperliquid.xyz/info"
    with _client() as client:
        payload, server_time = _post_json_snapshot(
            client, endpoint, {"type": "metaAndAssetCtxs"}
        )
    received_at = now_ms()
    if not fresh_market_timestamp(server_time, received_at, max_age_ms=30_000):
        raise ValueError("Hyperliquid: horloge HTTP périmée ou incohérente")
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("Hyperliquid: réponse metaAndAssetCtxs invalide")
    meta, contexts = payload
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(contexts, list) or len(universe) != len(contexts):
        raise ValueError("Hyperliquid: univers ou contextes invalides")

    enhanced_symbols = {
        str(contract.get("name", ""))
        for contract, context in zip(universe, contexts)
        if isinstance(contract, dict)
        and isinstance(context, dict)
        and (rate := finite(context.get("funding"))) is not None
        and abs(rate * 100) >= ENHANCED_VERIFICATION_THRESHOLD_PCT
    }
    enhanced_confirmation: dict[str, float | None] = {}
    if enhanced_symbols:
        with _client() as client:
            confirmation_payload, confirmation_time = _post_json_snapshot(
                client, endpoint, {"type": "metaAndAssetCtxs"}
            )
        if not fresh_market_timestamp(confirmation_time, now_ms(), max_age_ms=30_000):
            raise ValueError("Hyperliquid: seconde horloge HTTP invalide")
        if isinstance(confirmation_payload, list) and len(confirmation_payload) == 2:
            confirmation_meta, confirmation_contexts = confirmation_payload
            confirmation_universe = (
                confirmation_meta.get("universe")
                if isinstance(confirmation_meta, dict)
                else None
            )
            if isinstance(confirmation_universe, list) and isinstance(confirmation_contexts, list):
                enhanced_confirmation = {
                    str(item.get("name", "")): finite(ctx.get("funding"))
                    for item, ctx in zip(confirmation_universe, confirmation_contexts)
                    if isinstance(item, dict) and isinstance(ctx, dict)
                }

    next_funding_at = next_hour_boundary_ms(server_time)
    markets: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for contract, context in zip(universe, contexts):
        if not isinstance(contract, dict) or not isinstance(context, dict):
            continue
        symbol = str(contract.get("name", ""))
        if not symbol or contract.get("isDelisted") is True:
            continue
        rate = finite(context.get("funding"))
        mark_price = finite(context.get("markPx"))
        oracle_price = finite(context.get("oraclePx"))
        mid_price = finite(context.get("midPx")) or mark_price
        if rate is None or min(mark_price or 0.0, oracle_price or 0.0, mid_price or 0.0) <= 0:
            continue
        funding_pct = rate * 100
        if abs(funding_pct) > 4.0 + 1e-9:
            rejections.append(
                rejection("Hyperliquid", symbol, "taux hors plafond officiel ±4 %/h", funding_pct)
            )
            continue
        if symbol in enhanced_symbols and not funding_rates_match(
            rate,
            enhanced_confirmation.get(symbol),
            absolute_tolerance=1e-5,
            relative_tolerance=1e-3,
        ):
            rejections.append(
                rejection(
                    "Hyperliquid",
                    symbol,
                    "seconde lecture officielle non concordante",
                    funding_pct,
                )
            )
            continue
        open_interest_native = finite(context.get("openInterest"))
        open_interest_usd = (
            open_interest_native * oracle_price
            if open_interest_native is not None
            else None
        )
        volume_24h_usd = finite(context.get("dayNtlVlm"))
        volume_24h_native = finite(context.get("dayBaseVlm"))
        markets.append(
            {
                "id": f"hyperliquid-{symbol.lower()}",
                "asset": normalize_asset(symbol),
                "symbol": symbol,
                "exchange": "Hyperliquid",
                "direction": "positive" if funding_pct >= 0 else "negative",
                "funding_pct": funding_pct,
                "predicted_funding_pct": None,
                "prediction_source": "not-published-by-exchange",
                "funding_basis": "current-hour",
                "funding_verified": True,
                "funding_verification_source": (
                    "Hyperliquid metaAndAssetCtxs relu + plafond protocolaire"
                    if symbol in enhanced_symbols
                    else "Hyperliquid metaAndAssetCtxs + plafond protocolaire"
                ),
                "verification_level": (
                    "enhanced-double-read"
                    if symbol in enhanced_symbols
                    else "official-snapshot+protocol-cap"
                ),
                "interval_hours": 1,
                "interval_source": "Protocole Hyperliquid · paiement horaire",
                "next_funding_at": next_funding_at,
                "deadline_source": "protocol-hour+http-server-clock",
                "perp_price": mid_price,
                "bid_price": None,
                "ask_price": None,
                "mark_price": mark_price,
                "oracle_price": oracle_price,
                "contract_value": 1.0,
                "spread_bps": None,
                "taker_fee_pct": 0.045,
                "fee_source": "Barème perp public de base ; taux exact dépend du compte",
                "margin_long_available": False,
                "margin_short_available": False,
                "max_margin_leverage": None,
                "depth_usd": volume_24h_usd or 0.0,
                "open_interest_usd": open_interest_usd,
                "open_interest_native": open_interest_native,
                "volume_24h_native": volume_24h_native,
                "volume_24h_usd": volume_24h_usd,
                "data_status": "live",
                "updated_at": received_at,
                "market_data_at": server_time,
                "market_timestamp_source": "official-http-server-clock",
                "regional_eligibility": "unverified",
                "perp_long_available": True,
                "perp_short_available": True,
                "perp_availability_source": "Catalogue Hyperliquid HyperCore · PERP actif",
                "margin_product_listed": False,
                "margin_long_source": "Aucun spot margin assimilé au PERP HyperCore",
                "margin_short_source": "Aucun spot margin assimilé au PERP HyperCore",
            }
        )

    if not markets:
        raise ValueError("Hyperliquid: aucun PERP valide après contrôle")
    markets.sort(key=lambda item: abs(item["funding_pct"]), reverse=True)
    return SourceResult(
        "Hyperliquid",
        markets,
        received_at,
        server_time,
        "HyperCore uniquement ; aucun spot simple n'est injecté dans le scanner.",
        rejections=rejections,
    )


def _source_health(result: SourceResult, fetched_at: int, status: str, message: str | None = None) -> dict[str, Any]:
    return {
        "exchange": result.exchange,
        "status": status,
        "markets": len(result.markets),
        "rejected": len(result.rejections),
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
        "Gate.io": fetch_gateio,
        "Hyperliquid": fetch_hyperliquid,
    }
    completed: dict[str, SourceResult | Exception] = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
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
    rejections: list[dict[str, Any]] = []
    live_sources = 0

    source_order = ("Bitget", "Kraken", "BloFin", "Gate.io", "Hyperliquid")
    for exchange in source_order:
        result = completed[exchange]
        if isinstance(result, SourceResult):
            live_sources += 1
            with _last_good_lock:
                _last_good[exchange] = (deepcopy(result), fetched_at)
            markets.extend(result.markets)
            rejections.extend(result.rejections)
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
                rejections.extend(cached.rejections)
                health = _source_health(cached, fetched_at, "stale", error_message)
                health["age_ms"] = cache_age
                sources.append(health)
                continue
        sources.append(
            {
                "exchange": exchange,
                "status": "error",
                "markets": 0,
                "rejected": 0,
                "source_updated_at": None,
                "age_ms": None,
                "market_data_age_ms": None,
                "message": error_message,
            }
        )

    markets = [
        market
        for market in markets
        if market.get("funding_verified") is True
        and float(market["interval_hours"]) in ALLOWED_INTERVALS
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
    mode = "live" if live_sources == len(fetchers) else "hybrid" if live_sources > 0 else "offline"
    return {
        "mode": mode,
        "markets": markets,
        "rejections": rejections,
        "fetched_at": fetched_at,
        "latency_ms": fetched_at - started_at,
        "sources": sources,
    }
