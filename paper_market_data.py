"""Official public data used only by the live paper portfolio.

The scanner remains independent. This module reads order books for executable
VWAPs and confirmed funding history for theoretical paper settlements.
"""

from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any

import httpx


TIMEOUT_SECONDS = 8.0
MAX_BOOK_AGE_MS = 20_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "BoussoleFundingDesk/2.0"},
    )


def _get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Réponse de marché invalide")
    return payload


def _levels(raw: Any, multiplier: float) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return parsed
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = _finite(row[0])
        quantity = _finite(row[1])
        if price is None or quantity is None or price <= 0 or quantity <= 0:
            continue
        parsed.append((price, quantity * multiplier))
    return parsed


def fetch_order_book(exchange: str, symbol: str, contract_value: float = 1.0) -> dict[str, Any]:
    """Return a normalized, fresh public book with quantities in base asset."""
    with _client() as client:
        if exchange == "Bitget":
            payload = _get_json(
                client,
                "https://api.bitget.com/api/v2/mix/market/merge-depth"
                f"?symbol={symbol}&productType=USDT-FUTURES&precision=scale0&limit=50",
            )
            if str(payload.get("code")) != "00000" or not isinstance(payload.get("data"), dict):
                raise ValueError("Carnet Bitget indisponible")
            data = payload["data"]
            timestamp = int(float(data.get("ts", 0)))
            multiplier = 1.0
        elif exchange == "Kraken":
            payload = _get_json(
                client,
                f"https://futures.kraken.com/derivatives/api/v3/orderbook?symbol={symbol}",
            )
            if payload.get("result") != "success" or not isinstance(payload.get("orderBook"), dict):
                raise ValueError("Carnet Kraken indisponible")
            data = payload["orderBook"]
            try:
                timestamp = int(
                    datetime.fromisoformat(str(payload.get("serverTime", "")).replace("Z", "+00:00")).timestamp()
                    * 1000
                )
            except ValueError as error:
                raise ValueError("Horloge du carnet Kraken invalide") from error
            multiplier = 1.0
        elif exchange == "BloFin":
            payload = _get_json(
                client,
                f"https://openapi.blofin.com/api/v1/market/books?instId={symbol}&size=100",
            )
            rows = payload.get("data")
            if str(payload.get("code")) != "0" or not isinstance(rows, list) or not rows:
                raise ValueError("Carnet BloFin indisponible")
            data = rows[0]
            timestamp = int(float(data.get("ts", 0)))
            multiplier = float(contract_value)
        else:
            raise ValueError(f"Plateforme non prise en charge: {exchange}")

    age_ms = _now_ms() - timestamp
    if timestamp <= 0 or age_ms < -5_000 or age_ms > MAX_BOOK_AGE_MS:
        raise ValueError(f"Carnet {exchange} périmé")
    asks = sorted(_levels(data.get("asks"), multiplier), key=lambda level: level[0])
    bids = sorted(_levels(data.get("bids"), multiplier), key=lambda level: level[0], reverse=True)
    if not asks or not bids or bids[0][0] >= asks[0][0]:
        raise ValueError(f"Carnet {exchange} vide ou croisé")
    return {"exchange": exchange, "symbol": symbol, "timestamp": timestamp, "asks": asks, "bids": bids}


def fetch_order_books(
    requests: dict[str, tuple[str, str, float]]
) -> dict[str, dict[str, Any]]:
    """Fetch both execution legs concurrently to minimize snapshot skew."""
    with ThreadPoolExecutor(max_workers=max(1, len(requests))) as pool:
        pending = {
            name: pool.submit(fetch_order_book, exchange, symbol, contract_value)
            for name, (exchange, symbol, contract_value) in requests.items()
        }
        books = {name: future.result() for name, future in pending.items()}
    timestamps = [int(book["timestamp"]) for book in books.values()]
    if timestamps and max(timestamps) - min(timestamps) > MAX_BOOK_AGE_MS:
        raise ValueError("Décalage temporel excessif entre les deux carnets")
    return books


def _mark_at_bitget(client: httpx.Client, symbol: str, timestamp: int) -> float | None:
    payload = _get_json(
        client,
        "https://api.bitget.com/api/v2/mix/market/history-mark-candles"
        f"?symbol={symbol}&productType=USDT-FUTURES&granularity=1m"
        f"&startTime={timestamp - 60_000}&endTime={timestamp + 60_000}&limit=3",
    )
    if str(payload.get("code")) != "00000":
        return None
    for row in payload.get("data") or []:
        if isinstance(row, list) and len(row) >= 2 and int(float(row[0])) == timestamp:
            return _finite(row[1])
    return None


def _mark_at_blofin(client: httpx.Client, symbol: str, timestamp: int) -> float | None:
    payload = _get_json(
        client,
        "https://openapi.blofin.com/api/v1/market/mark-price-candles"
        f"?instId={symbol}&bar=1m&after={timestamp + 60_000}&before={timestamp - 60_000}&limit=3",
    )
    if str(payload.get("code")) != "0":
        return None
    for row in payload.get("data") or []:
        if isinstance(row, list) and len(row) >= 2 and int(float(row[0])) == timestamp:
            return _finite(row[1])
    return None


def fetch_confirmed_settlements(
    exchange: str, symbol: str, since_ms: int, until_ms: int
) -> list[dict[str, Any]]:
    """Return settled rates with an official mark-price reference.

    Kraken is deliberately excluded: its public analytics do not expose the
    exact account-level continuous funding realization. That requires a later
    read-only authenticated connector.
    """
    if until_ms <= since_ms or exchange == "Kraken":
        return []
    with _client() as client:
        if exchange == "Bitget":
            payload = _get_json(
                client,
                "https://api.bitget.com/api/v2/mix/market/history-fund-rate"
                f"?symbol={symbol}&productType=USDT-FUTURES&pageSize=100&pageNo=1",
            )
            if str(payload.get("code")) != "00000":
                raise ValueError("Historique funding Bitget indisponible")
            rows = payload.get("data") or []
            mark_reader = _mark_at_bitget
            source = "API Bitget · historique funding + mark 1m"
        elif exchange == "BloFin":
            payload = _get_json(
                client,
                f"https://openapi.blofin.com/api/v1/market/funding-rate-history?instId={symbol}&limit=100",
            )
            if str(payload.get("code")) != "0":
                raise ValueError("Historique funding BloFin indisponible")
            rows = payload.get("data") or []
            mark_reader = _mark_at_blofin
            source = "API BloFin · historique funding + mark 1m"
        else:
            raise ValueError(f"Plateforme non prise en charge: {exchange}")

        output: list[dict[str, Any]] = []
        for row in rows:
            timestamp = int(float(row.get("fundingTime", 0)))
            rate = _finite(row.get("fundingRate"))
            if rate is None or not (since_ms < timestamp <= until_ms):
                continue
            mark_price = mark_reader(client, symbol, timestamp)
            if mark_price is None or mark_price <= 0:
                continue
            output.append(
                {
                    "id": f"{exchange}:{symbol}:{timestamp}",
                    "exchange": exchange,
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "funding_pct": rate * 100,
                    "mark_price": mark_price,
                    "source": source,
                }
            )
    return sorted(output, key=lambda event: int(event["timestamp"]))
