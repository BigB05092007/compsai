"""
Market data for CompsAI (Module 1b): share price and shares outstanding per ticker.

What this module does
---------------------
`get_market_data(ticker)` returns the two market inputs a comps table needs — the last
close price and the number of shares outstanding — plus the market cap they imply:

    market cap = share price x shares outstanding      (basic, not diluted)

Where the numbers come from, in priority order
----------------------------------------------
1. `overrides` passed by the caller (a banker typing in a price) always win.
2. Offline mode: `market_{TICKER}.json` from the fixture directory (tests / demo).
3. yfinance (Yahoo Finance): `fast_info` first, `.info` as a fallback. Every yfinance call
   is wrapped in try/except because Yahoo changes its API and rate-limits without warning;
   a broken price feed must never crash the pipeline. Results are cached for 12 hours.
4. Shares fallback: the cover page of the latest 10-K/10-Q tags shares outstanding as
   `dei:EntityCommonStockSharesOutstanding` (one fact per share class), so when yfinance has
   no share count we sum the classes reported on the latest date.

A missing price is returned as NaN (and market cap NaN). We never invent a price.
Units follow docs/DESIGN.md: price in USD/share, shares in millions, market cap in $mm.
yfinance is imported lazily inside `_fetch_yfinance` so offline runs never import it.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date

from compsai import MILLION
from compsai.edgar import is_offline, load_fixture, read_cache, write_cache

log = logging.getLogger("compsai")

#: Prices move; a half-day cache keeps a comps run consistent without going stale.
TTL_MARKET = 12 * 3600


# ------------------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------------------

def _as_number(value) -> float | None:
    """Coerce yfinance/JSON values to float; None for missing, NaN, or garbage."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _fast_info_get(fast_info, key: str):
    """yfinance's FastInfo is dict-like in recent versions, attribute-like in older ones."""
    try:
        return fast_info[key]
    except Exception:
        return getattr(fast_info, key, None)


# ------------------------------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------------------------------

def _fetch_yfinance(ticker: str) -> dict:
    """
    Price / shares / currency from Yahoo Finance via yfinance. Never raises: any failure is
    logged and the field stays None. Shares are converted to millions here.
    """
    out = {"price": None, "shares_outstanding": None, "currency": None,
           "as_of": date.today().isoformat()}
    try:
        import yfinance as yf  # lazy: offline/test runs must never import it
    except Exception as exc:  # ImportError or anything odd in yfinance's import chain
        log.warning("%s: yfinance unavailable (%s); price will be NaN", ticker, exc)
        return out

    try:
        stock = yf.Ticker(ticker)
    except Exception as exc:
        log.warning("%s: yfinance Ticker() failed: %s", ticker, exc)
        return out

    raw_shares = None
    try:
        fast_info = stock.fast_info
        out["price"] = _as_number(_fast_info_get(fast_info, "last_price")
                                  or _fast_info_get(fast_info, "lastPrice"))
        raw_shares = _as_number(_fast_info_get(fast_info, "shares"))
        currency = _fast_info_get(fast_info, "currency")
        out["currency"] = str(currency) if currency else None
    except Exception as exc:
        log.warning("%s: yfinance fast_info failed: %s", ticker, exc)

    if out["price"] is None or raw_shares is None:
        try:
            info = stock.info or {}
            if out["price"] is None:
                out["price"] = _as_number(info.get("currentPrice") or info.get("regularMarketPrice"))
            if raw_shares is None:
                raw_shares = _as_number(info.get("sharesOutstanding"))
            if not out["currency"] and info.get("currency"):
                out["currency"] = str(info["currency"])
        except Exception as exc:
            log.warning("%s: yfinance .info failed: %s", ticker, exc)

    if raw_shares is not None:
        out["shares_outstanding"] = raw_shares / MILLION  # Yahoo reports a raw share count
    if out["price"] is None:
        log.warning("%s: no price from yfinance", ticker)
    return out


def _shares_from_xbrl(facts: dict | None) -> float | None:
    """
    Shares outstanding (millions) from the filing cover page: `dei:EntityCommonStockSharesOutstanding`.

    Take the latest `end` date; if several facts share that end AND the same `filed` date,
    they are separate share classes (e.g. Class A + Class B) and are summed.
    """
    if not facts:
        return None
    units = (facts.get("facts", {}).get("dei", {})
             .get("EntityCommonStockSharesOutstanding", {}).get("units", {}))
    share_facts = [f for f in units.get("shares", []) if f.get("val") is not None and f.get("end")]
    if not share_facts:
        return None
    latest_end = max(f["end"] for f in share_facts)
    at_latest_end = [f for f in share_facts if f["end"] == latest_end]
    latest_filed = max(f.get("filed", "") for f in at_latest_end)
    share_classes = [f for f in at_latest_end if f.get("filed", "") == latest_filed]
    total = sum(float(f["val"]) for f in share_classes)
    log.debug("dei shares outstanding as of %s: %d class(es) summing to %.0f", latest_end,
              len(share_classes), total)
    return total / MILLION


def _load_market_fixture(ticker: str) -> dict | None:
    try:
        return load_fixture("market", ticker)
    except FileNotFoundError as exc:
        log.warning("%s", exc)
        return None


# ------------------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------------------

def get_market_data(ticker: str, facts: dict | None = None, refresh: bool = False,
                    offline: bool = False, overrides: dict | None = None) -> dict:
    """
    Price, shares outstanding and market cap for one ticker (see module docstring).

    Returns {ticker, price (USD/share), shares_outstanding (mm), currency, market_cap ($mm),
             source ("yfinance" | "yfinance+xbrl" | "xbrl" | "override" | "fixture"), as_of}.
    """
    ticker = ticker.upper().strip()
    result = {
        "ticker": ticker,
        "price": math.nan,
        "shares_outstanding": math.nan,
        "currency": "USD",
        "market_cap": math.nan,
        "source": "",
        "as_of": date.today().isoformat(),
    }

    if is_offline(offline):
        result["source"] = "fixture"
        data = _load_market_fixture(ticker) or {}
    else:
        result["source"] = "yfinance"
        cache_name = f"market_{ticker}.json"
        data = None if refresh else read_cache(cache_name, TTL_MARKET)
        if data is None:
            data = _fetch_yfinance(ticker)
            if data.get("price") is not None:  # never cache a failed lookup for 12 hours
                write_cache(cache_name, data)

    price = _as_number(data.get("price"))
    shares = _as_number(data.get("shares_outstanding"))
    if price is not None:
        result["price"] = price
    if shares is not None:
        result["shares_outstanding"] = shares
    if data.get("currency"):
        result["currency"] = str(data["currency"])
    if data.get("as_of"):
        result["as_of"] = str(data["as_of"])

    # Shares fallback from the XBRL cover page.
    if shares is None:
        xbrl_shares = _shares_from_xbrl(facts)
        if xbrl_shares is not None:
            result["shares_outstanding"] = xbrl_shares
            result["source"] = "yfinance+xbrl" if (price is not None and result["source"] == "yfinance") else "xbrl"
            log.info("%s: shares outstanding from dei:EntityCommonStockSharesOutstanding (%.1f mm)",
                     ticker, xbrl_shares)

    # Caller overrides beat every source.
    for key in ("price", "shares_outstanding"):
        override = _as_number((overrides or {}).get(key))
        if override is not None:
            result[key] = override
            result["source"] = "override"

    # Market cap = share price x shares outstanding (both already in $ and millions).
    if not math.isnan(result["price"]) and not math.isnan(result["shares_outstanding"]):
        result["market_cap"] = result["price"] * result["shares_outstanding"]
    else:
        log.warning("%s: price or shares missing; market cap is NaN (source=%s)", ticker, result["source"])
    return result


# ------------------------------------------------------------------------------------------
# CLI:  python -m compsai.market AAPL MSFT [--offline]
# ------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m compsai.market",
                                     description="Print price / shares / market cap per ticker.")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--offline", action="store_true", help="read market_{TICKER}.json fixtures")
    parser.add_argument("--refresh", action="store_true", help="bypass the 12-hour cache")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for ticker in args.tickers:
        print(json.dumps(get_market_data(ticker, refresh=args.refresh, offline=args.offline), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
