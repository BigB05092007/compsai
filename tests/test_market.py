"""
Tests for compsai.market: fixture path, overrides, XBRL share-count fallback, missing price,
caching. yfinance is never imported here: the network path is exercised by monkeypatching
`_fetch_yfinance`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from compsai import market
from compsai.edgar import get_company_facts
from compsai.market import get_market_data


@pytest.fixture
def online(monkeypatch):
    monkeypatch.delenv("COMPSAI_OFFLINE", raising=False)


@pytest.fixture
def no_yfinance(monkeypatch):
    """Simulate Yahoo returning nothing (or being unreachable)."""
    calls: list[str] = []

    def fake_fetch(ticker):
        calls.append(ticker)
        return {"price": None, "shares_outstanding": None, "currency": None, "as_of": "2025-08-29"}

    monkeypatch.setattr(market, "_fetch_yfinance", fake_fetch)
    return calls


# ------------------------------------------------------------------------------------------
# Offline fixture path
# ------------------------------------------------------------------------------------------

def test_fixture_path_returns_contract_keys_and_market_cap():
    data = get_market_data("FIXA", offline=True)
    assert list(data) == ["ticker", "price", "shares_outstanding", "currency", "market_cap", "source", "as_of"]
    assert data["ticker"] == "FIXA"
    assert data["price"] == 200.0
    assert data["shares_outstanding"] == 15_000.0
    assert data["currency"] == "USD"
    # market cap = price x shares outstanding = 200 x 15,000 mm = 3,000,000 $mm
    assert data["market_cap"] == 3_000_000.0
    assert data["source"] == "fixture"
    assert data["as_of"] == "2025-08-29"


def test_fixture_path_via_env_and_other_tickers():
    fixb = get_market_data("fixb")  # conftest sets COMPSAI_OFFLINE=1; lowercase ticker ok
    assert (fixb["price"], fixb["shares_outstanding"], fixb["market_cap"]) == (400.0, 7_400.0, 2_960_000.0)
    fixc = get_market_data("FIXC")
    assert (fixc["price"], fixc["shares_outstanding"], fixc["market_cap"]) == (250.0, 2_800.0, 700_000.0)


def test_offline_never_calls_yfinance(monkeypatch):
    def boom(ticker):
        raise AssertionError("yfinance path must not run offline")

    monkeypatch.setattr(market, "_fetch_yfinance", boom)
    data = get_market_data("FIXA", offline=True)
    assert data["source"] == "fixture" and data["price"] == 200.0


def test_offline_missing_fixture_gives_nan_price_but_xbrl_shares():
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    data = get_market_data("NOPE", facts=facts, offline=True)
    assert math.isnan(data["price"]) and math.isnan(data["market_cap"])
    assert data["shares_outstanding"] == 15_000.0  # latest dei cover-page count
    assert data["source"] == "xbrl"


# ------------------------------------------------------------------------------------------
# Overrides
# ------------------------------------------------------------------------------------------

def test_overrides_win_over_fixture():
    data = get_market_data("FIXA", offline=True,
                           overrides={"price": 210.0, "shares_outstanding": 14_000.0})
    assert data["price"] == 210.0 and data["shares_outstanding"] == 14_000.0
    assert data["market_cap"] == 210.0 * 14_000.0  # 2,940,000
    assert data["source"] == "override"


def test_partial_override_keeps_other_field():
    data = get_market_data("FIXA", offline=True, overrides={"price": 250.0})
    assert data["price"] == 250.0 and data["shares_outstanding"] == 15_000.0
    assert data["market_cap"] == 250.0 * 15_000.0
    assert data["source"] == "override"


def test_override_none_values_are_ignored():
    data = get_market_data("FIXA", offline=True, overrides={"price": None, "shares_outstanding": None})
    assert data["price"] == 200.0 and data["source"] == "fixture"


# ------------------------------------------------------------------------------------------
# XBRL shares fallback (dei:EntityCommonStockSharesOutstanding)
# ------------------------------------------------------------------------------------------

def _dei_facts(entries):
    return {"cik": 42, "entityName": "X", "facts": {"dei": {"EntityCommonStockSharesOutstanding": {
        "label": "Entity Common Stock, Shares Outstanding", "description": "",
        "units": {"shares": entries}}}}}


def test_dei_fallback_sums_share_classes_with_same_end_and_filed():
    facts = _dei_facts([
        {"end": "2025-01-15", "val": 900_000_000, "accn": "a", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
        {"end": "2025-04-15", "val": 1_000_000_000, "accn": "b", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"end": "2025-04-15", "val": 500_000_000, "accn": "b", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
    ])
    # latest end = 2025-04-15; two classes filed the same day: 1,000 + 500 = 1,500 mm
    assert market._shares_from_xbrl(facts) == 1_500.0


def test_dei_fallback_uses_latest_end_only_and_latest_filing():
    facts = _dei_facts([
        {"end": "2025-04-15", "val": 1_000_000_000, "accn": "b", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"end": "2025-04-15", "val": 1_010_000_000, "accn": "c", "fy": 2025, "fp": "Q1", "form": "10-Q/A", "filed": "2025-06-01"},
        {"end": "2024-10-15", "val": 2_000_000_000, "accn": "a", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
    ])
    assert market._shares_from_xbrl(facts) == 1_010.0  # amended figure, older date ignored
    assert market._shares_from_xbrl(None) is None
    assert market._shares_from_xbrl({"facts": {}}) is None


def test_dei_fallback_from_fixb_fixture_two_classes(online, no_yfinance):
    facts = get_company_facts("2", offline=True, ticker="FIXB")
    data = get_market_data("FIXB", facts=facts)
    # FY2025 10-K cover page (2025-07-25): Class A 5,400 mm + Class B 2,000 mm
    assert data["shares_outstanding"] == 7_400.0
    assert math.isnan(data["price"]) and math.isnan(data["market_cap"])
    assert data["source"] == "xbrl"
    assert no_yfinance == ["FIXB"]


def test_yfinance_price_with_xbrl_shares_is_mixed_source(online, tmp_cache_dir, monkeypatch):
    monkeypatch.setattr(market, "_fetch_yfinance", lambda t: {
        "price": 123.45, "shares_outstanding": None, "currency": "USD", "as_of": "2025-08-29"})
    facts = get_company_facts("3", offline=True, ticker="FIXC")
    data = get_market_data("FIXC", facts=facts)
    assert data["price"] == 123.45 and data["shares_outstanding"] == 2_800.0
    assert data["market_cap"] == pytest.approx(123.45 * 2_800.0)
    assert data["source"] == "yfinance+xbrl"


# ------------------------------------------------------------------------------------------
# Missing price, caching
# ------------------------------------------------------------------------------------------

def test_missing_price_gives_nan_price_and_nan_market_cap(online, no_yfinance, tmp_cache_dir):
    data = get_market_data("FIXA")  # no facts -> no share fallback either
    assert math.isnan(data["price"]) and math.isnan(data["shares_outstanding"]) and math.isnan(data["market_cap"])
    assert data["source"] == "yfinance"
    assert not (tmp_cache_dir / "market_FIXA.json").exists()  # failed lookups are not cached


def test_market_cache_write_read_ttl_refresh(online, tmp_cache_dir, monkeypatch):
    calls: list[str] = []

    def fake_fetch(ticker):
        calls.append(ticker)
        return {"price": 100.0, "shares_outstanding": 1_000.0, "currency": "USD", "as_of": "2025-08-29"}

    monkeypatch.setattr(market, "_fetch_yfinance", fake_fetch)
    first = get_market_data("FIXA")
    second = get_market_data("FIXA")
    assert first["source"] == second["source"] == "yfinance"
    assert first["market_cap"] == 100_000.0
    assert calls == ["FIXA"]  # second call served from cache

    cache_file = tmp_cache_dir / "market_FIXA.json"
    payload = json.loads(cache_file.read_text())
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()  # TTL 12 h
    cache_file.write_text(json.dumps(payload))
    get_market_data("FIXA")
    assert calls == ["FIXA", "FIXA"]

    get_market_data("FIXA", refresh=True)
    assert calls == ["FIXA", "FIXA", "FIXA"]


def test_fetch_yfinance_survives_import_failure(monkeypatch):
    """If `import yfinance` blows up, the fetch logs and returns empty fields (never raises)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    out = market._fetch_yfinance("FIXA")
    assert out["price"] is None and out["shares_outstanding"] is None


def test_fetch_yfinance_uses_fast_info_then_info(monkeypatch):
    """Drive _fetch_yfinance with a fake yfinance module: fast_info lacks shares, .info has them."""
    import sys
    import types

    class FakeFastInfo(dict):
        pass

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol
            self.fast_info = FakeFastInfo(last_price=150.0, shares=None, currency="USD")
            self.info = {"sharesOutstanding": 2_000_000_000, "regularMarketPrice": 149.0}

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    out = market._fetch_yfinance("ZZZ")
    assert out["price"] == 150.0                # fast_info price wins
    assert out["shares_outstanding"] == 2_000.0  # .info shares, converted to millions
    assert out["currency"] == "USD"


def test_market_cli_offline(capsys):
    assert market.main(["FIXA", "--offline"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["market_cap"] == 3_000_000.0
