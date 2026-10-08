import pytest

import paper_market_data


def test_fetch_order_books_preserves_both_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_book(exchange: str, symbol: str, contract_value: float) -> dict:
        timestamp = 10_000 if exchange == "Bitget" else 10_050
        return {
            "exchange": exchange,
            "symbol": symbol,
            "timestamp": timestamp,
            "bids": [(99.0, contract_value)],
            "asks": [(101.0, contract_value)],
        }

    monkeypatch.setattr(paper_market_data, "fetch_order_book", fake_book)
    books = paper_market_data.fetch_order_books(
        {
            "funding": ("Bitget", "ABCUSDT", 1.0),
            "hedge": ("BloFin", "ABC-USDT", 2.0),
        }
    )
    assert set(books) == {"funding", "hedge"}
    assert books["funding"]["symbol"] == "ABCUSDT"
    assert books["hedge"]["asks"][0][1] == 2.0


def test_fetch_order_books_rejects_excessive_snapshot_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_book(exchange: str, symbol: str, contract_value: float) -> dict:
        timestamp = 1_000 if exchange == "Bitget" else 1_000 + paper_market_data.MAX_BOOK_AGE_MS + 1
        return {
            "exchange": exchange,
            "symbol": symbol,
            "timestamp": timestamp,
            "bids": [(99.0, 1.0)],
            "asks": [(101.0, 1.0)],
        }

    monkeypatch.setattr(paper_market_data, "fetch_order_book", fake_book)
    with pytest.raises(ValueError, match="Décalage temporel excessif"):
        paper_market_data.fetch_order_books(
            {
                "funding": ("Bitget", "ABCUSDT", 1.0),
                "hedge": ("BloFin", "ABC-USDT", 1.0),
            }
        )
