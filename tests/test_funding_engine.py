import pytest

from funding_engine import (
    SourceResult,
    fresh_market_timestamp,
    funding_rates_match,
    kraken_open_interest_usd,
    kraken_published_relative_pct,
    kraken_relative_funding_pct,
    market_state,
    normalize_asset,
    within_caps,
)


def test_kraken_linear_contract_formula() -> None:
    assert kraken_relative_funding_pct("PF_XBTUSD", 0.43, 86_000) == pytest.approx(0.0005)


def test_kraken_inverse_contract_formula() -> None:
    assert kraken_relative_funding_pct("PI_XRPUSD", -0.00331, 1.51) == pytest.approx(-0.49981)


def test_kraken_published_relative_rate_is_used_directly() -> None:
    ticker = {"relativeFundingRate": 0.00123, "fundingRate": 999}
    assert kraken_published_relative_pct(ticker, "relativeFundingRate") == pytest.approx(0.123)
    assert kraken_published_relative_pct(ticker, "relativeFundingRatePrediction") is None


def test_kraken_open_interest_normalization() -> None:
    assert kraken_open_interest_usd("PI_XBTUSD", 2_000_000, 80_000) == pytest.approx(2_000_000)
    assert kraken_open_interest_usd("PF_XBTUSD", 10, 80_000) == pytest.approx(800_000)
    assert kraken_open_interest_usd("PF_XBTUSD", None, 80_000) is None



def test_thresholds_do_not_depend_on_leverage() -> None:
    assert market_state(0.5) == "ALERTE"
    assert market_state(-0.4) == "SURVEILLER"
    assert market_state(0.399999) == "FLUX"
    assert market_state(2.0, "stale") == "PÉRIMÉ"


def test_asset_normalization() -> None:
    assert normalize_asset("XBT") == "BTC"
    assert normalize_asset("XDG") == "DOGE"


def test_exchange_caps() -> None:
    assert within_caps(0.003, "-0.003", "0.003")
    assert not within_caps(0.004, "-0.003", "0.003")


def test_cross_endpoint_funding_match_is_strict() -> None:
    assert funding_rates_match("0.005", 0.005)
    assert funding_rates_match("-0.01", "-0.01000000001")
    assert not funding_rates_match("0.005", "0.0049")
    assert not funding_rates_match(None, "0.005")


def test_market_timestamp_must_be_recent_and_not_in_the_future() -> None:
    reference = 1_000_000
    assert fresh_market_timestamp(reference - 179_999, reference)
    assert not fresh_market_timestamp(reference - 180_001, reference)
    assert not fresh_market_timestamp(reference + 30_001, reference)
    assert not fresh_market_timestamp(None, reference)


def test_source_result_tracks_rejections_without_mutable_default_leak() -> None:
    first = SourceResult("A", [], 1, 1)
    second = SourceResult("B", [], 1, 1)
    first.rejections.append({"symbol": "TEST"})
    assert second.rejections == []
