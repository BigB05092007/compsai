"""
SEC EDGAR access and fiscal-year / TTM financials extraction (CompsAI Module 1).

What this module does, in plain English
---------------------------------------
1. Maps a ticker to the SEC's 10-digit Central Index Key (CIK) and company name.
2. Downloads (and caches) the free "companyfacts" JSON that holds every XBRL number a
   company has ever tagged in its filings, plus the "submissions" filing index.
3. Turns that pile of facts into one small table per company: the last `n_years` fiscal
   years plus a trailing-twelve-month (TTM) row, in the unit conventions of docs/DESIGN.md
   (money in USD millions, share counts in millions, per-share values raw).

Why it is fiddly (the EDGAR quirks handled here)
-------------------------------------------------
* Companies tag the same concept with different XBRL tags (Apple switched its revenue tag
  from `Revenues` to `RevenueFromContractWithCustomerExcludingAssessedTax`; Microsoft's
  D&A lives in `DepreciationAmortizationAndOther`). We therefore try tags in a fixed order
  FOR EACH FISCAL PERIOD SEPARATELY and record which tag won in `df.attrs["tags_used"]`.
* A 10-K restates the two prior years as comparatives. Those comparative facts carry the
  NEWER filing's `fy` and `filed`, so the fiscal-year label must come from the EARLIEST
  filing of a period while the value comes from the LATEST filing (most recent restatement).
* 52/53-week filers (Apple, retailers) end their year on a different date every year, so
  matching by exact date needs a +/-7 day tolerance.
* Nobody files a "TTM" number. It is rolled forward from the last 10-K and the year-to-date
  facts of the latest 10-Q:  TTM = FY + YTD(current) - YTD(prior year).

Everything is offline-testable: with `COMPSAI_OFFLINE=1` (or `offline=True`) the module
reads JSON fixtures from the fixture directory instead of the network.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from compsai import DEFAULT_CACHE_DIR, DEFAULT_FIXTURE_DIR, MILLION

log = logging.getLogger("compsai")

# ------------------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------------------

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

#: Forms that carry a full fiscal year of XBRL data (domestic + foreign annual reports).
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}
#: Forms that carry quarterly XBRL data. (6-K has no XBRL, so foreign filers get TTM = FY.)
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}

#: Cache time-to-live in seconds (DESIGN.md section 2).
TTL_TICKERS = 7 * 24 * 3600
TTL_FACTS = 24 * 3600
TTL_SUBMISSIONS = 24 * 3600

#: SEC fair-access cap is 10 requests/second; we stay under it at <= 9/s.
MIN_REQUEST_INTERVAL = 0.11
REQUEST_TIMEOUT = 30

#: Output columns of extract_financials, in contract order (DESIGN.md section 2).
COLUMNS = [
    "fiscal_year", "period_type", "period_end", "revenue", "ebit", "da", "ebitda",
    "net_income", "eps_diluted", "total_debt", "cash", "minority_interest", "preferred",
    "diluted_shares", "total_equity", "goodwill", "intangibles", "tangible_book_value",
    "currency",
]
NUMERIC_COLUMNS = [c for c in COLUMNS if c not in ("fiscal_year", "period_type", "period_end", "currency")]

#: Tag fallback table. A spec is "ns:Tag" or a list of such strings meaning "sum the
#: components that exist (at least one must)". Tried in order, per fiscal period.
CONCEPT_TAGS: dict[str, list] = {
    "revenue":       ["us-gaap:Revenues",
                      "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                      "us-gaap:SalesRevenueNet",
                      "ifrs-full:Revenue"],
    "ebit":          ["us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"],
    "da":            ["us-gaap:DepreciationDepletionAndAmortization",
                      "us-gaap:DepreciationAndAmortization",
                      "us-gaap:DepreciationAmortizationAndAccretionNet",
                      "us-gaap:DepreciationAmortizationAndOther",           # Microsoft's tag
                      ["us-gaap:Depreciation", "us-gaap:AmortizationOfIntangibleAssets"],
                      "ifrs-full:DepreciationAndAmortisationExpense"],
    "net_income":    ["us-gaap:NetIncomeLoss", "us-gaap:ProfitLoss",
                      "ifrs-full:ProfitLossAttributableToOwnersOfParent", "ifrs-full:ProfitLoss"],
    "eps_diluted":   ["us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted", "ifrs-full:DilutedEarningsLossPerShare",
                    "ifrs-full:BasicAndDilutedEarningsLossPerShare"],
    "cash":          ["us-gaap:CashAndCashEquivalentsAtCarryingValue",
                      "us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
                      "ifrs-full:CashAndCashEquivalents"],
    "minority_interest": ["us-gaap:MinorityInterest",
                          "us-gaap:RedeemableNoncontrollingInterestEquityCarryingAmount",
                          "ifrs-full:NoncontrollingInterests"],
    "preferred":     ["us-gaap:PreferredStockValue", "us-gaap:PreferredStockValueOutstanding"],
    "diluted_shares": ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                       "ifrs-full:AdjustedWeightedAverageShares"],
    "total_equity":  ["us-gaap:StockholdersEquity", "ifrs-full:EquityAttributableToOwnersOfParent"],
    "goodwill":      ["us-gaap:Goodwill", "ifrs-full:Goodwill"],
    "intangibles":   ["us-gaap:IntangibleAssetsNetExcludingGoodwill",
                      "us-gaap:FiniteLivedIntangibleAssetsNet",
                      "ifrs-full:IntangibleAssetsOtherThanGoodwill"],
}

#: Income-statement style concepts (have start AND end); rolled forward for TTM.
FLOW_CONCEPTS = ("revenue", "ebit", "da", "net_income", "eps_diluted", "diluted_shares")
#: Balance-sheet concepts (instant facts, end only). total_debt is handled by _total_debt().
INSTANT_CONCEPTS = ("cash", "minority_interest", "preferred", "total_equity", "goodwill", "intangibles")
#: Absence of these tags means the company has none of it -> 0.0, not NaN.
ZERO_WHEN_ABSENT = {"minority_interest", "preferred", "goodwill", "intangibles"}
#: Which XBRL unit each concept is measured in ("money" unless listed here).
UNIT_KIND = {"eps_diluted": "per_share", "diluted_shares": "shares"}

#: Date tolerances from DESIGN.md.
FY_END_TOLERANCE_DAYS = 7      # 52/53-week filers: other concepts vs the revenue period end
YTD_START_TOLERANCE_DAYS = 7   # YTD_cur must start the day after the fiscal year end
YTD_PRIOR_TOLERANCE_DAYS = 10  # YTD_prior end / duration vs YTD_cur
# Diluted share count ratio (latest 10-Q / latest 10-K) outside this band = a change of share
# basis (stock split, reverse split, major issuance); buybacks move it by a few percent a year.
SHARE_BASIS_MIN_RATIO = 0.8
SHARE_BASIS_MAX_RATIO = 1.25
ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 350, 380


# ------------------------------------------------------------------------------------------
# Network: one function does every request (User-Agent, throttling, error handling)
# ------------------------------------------------------------------------------------------

_last_request_at: float | None = None  # time.monotonic() of the previous request


def _user_agent() -> str:
    """SEC rejects anonymous clients; the header must name the app and a contact email."""
    load_dotenv()
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. SEC EDGAR requires a descriptive User-Agent with a "
            "contact email. Add a line like  SEC_USER_AGENT=CompsAI you@example.com  to .env "
            "(see .env.example) or export it in your shell."
        )
    return ua


def _throttle() -> None:
    """Sleep so consecutive requests are at least MIN_REQUEST_INTERVAL apart (<= 9/s)."""
    global _last_request_at
    now = time.monotonic()
    wait = 0.0
    if _last_request_at is not None:
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
    _last_request_at = now + max(wait, 0.0)


def _get_json(url: str) -> dict:
    """GET a JSON document from the SEC with the required headers and rate limit."""
    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}
    _throttle()
    log.debug("GET %s", url)
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        hint = " (403 usually means the SEC rejected the User-Agent header)" if resp.status_code == 403 else ""
        raise RuntimeError(f"SEC request failed: HTTP {resp.status_code} for {url}{hint}")
    return resp.json()


# ------------------------------------------------------------------------------------------
# Cache: data/cache/<name> = {"fetched_at": iso, "data": ...}
# ------------------------------------------------------------------------------------------

def cache_dir() -> Path:
    return Path(os.environ.get("COMPSAI_CACHE_DIR") or DEFAULT_CACHE_DIR)


def read_cache(name: str, ttl_seconds: float) -> dict | None:
    """Return cached `data` if the file exists and is younger than the TTL, else None."""
    path = cache_dir() / name
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        log.warning("ignoring unreadable cache file %s: %s", path, exc)
        return None
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > ttl_seconds:
        log.debug("cache expired for %s (age %.0fs > ttl %.0fs)", name, age, ttl_seconds)
        return None
    return payload["data"]


def write_cache(name: str, data) -> Path:
    path = cache_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fetch_cached(name: str, url: str, ttl_seconds: float, refresh: bool) -> dict:
    """Cache-through fetch: serve from cache unless expired or refresh=True."""
    if not refresh:
        cached = read_cache(name, ttl_seconds)
        if cached is not None:
            log.debug("cache hit for %s", name)
            return cached
    data = _get_json(url)
    write_cache(name, data)
    return data


# ------------------------------------------------------------------------------------------
# Offline mode: read fixtures instead of the network
# ------------------------------------------------------------------------------------------

def is_offline(offline: bool = False) -> bool:
    return offline or os.environ.get("COMPSAI_OFFLINE", "").strip().lower() in {"1", "true", "yes"}


def fixture_dir() -> Path:
    return Path(os.environ.get("COMPSAI_FIXTURE_DIR") or DEFAULT_FIXTURE_DIR)


def load_fixture(kind: str, ticker: str) -> dict:
    """Read `<fixture_dir>/<kind>_<TICKER>.json` (kind: companyfacts | submissions | market)."""
    path = fixture_dir() / f"{kind}_{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"offline mode: no fixture {path}. Create it with "
            f"`python -m compsai.edgar {ticker.upper()} --save-fixture {fixture_dir()}` "
            "or run tests/fixtures/make_fixtures.py."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_by_cik(kind: str, cik: str) -> dict:
    """Offline lookup when only a CIK is known: scan the fixture dir for a matching file."""
    for path in sorted(fixture_dir().glob(f"{kind}_*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if _pad_cik(doc.get("cik")) == _pad_cik(cik):
            return doc
    raise FileNotFoundError(f"offline mode: no {kind} fixture with CIK {cik} in {fixture_dir()}")


def _pad_cik(cik) -> str:
    return f"{int(str(cik).strip()):010d}"


# ------------------------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------------------------

def _ticker_table(refresh: bool = False) -> dict:
    """company_tickers.json: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}"""
    return _fetch_cached("company_tickers.json", TICKERS_URL, TTL_TICKERS, refresh)


def _lookup_ticker(ticker: str) -> dict:
    ticker = ticker.upper().strip()
    table = _ticker_table()
    # SEC writes share classes with a dash (BRK-B); Yahoo uses a dot (BRK.B). Accept both.
    wanted = {ticker, ticker.replace(".", "-")}
    for entry in table.values():
        if str(entry.get("ticker", "")).upper() in wanted:
            return entry
    raise KeyError(f"ticker {ticker!r} not found in SEC company_tickers.json")


def get_cik(ticker: str, offline: bool = False) -> str:
    """Ticker -> 10-digit zero-padded CIK (e.g. AAPL -> "0000320193")."""
    if is_offline(offline):
        return _pad_cik(load_fixture("companyfacts", ticker)["cik"])
    return _pad_cik(_lookup_ticker(ticker)["cik_str"])


def get_company_name(ticker: str, offline: bool = False) -> str:
    """Ticker -> registrant name ("Apple Inc."). Offline: the fixture's entityName."""
    if is_offline(offline):
        return str(load_fixture("companyfacts", ticker)["entityName"])
    return str(_lookup_ticker(ticker)["title"])


def get_company_facts(cik: str, refresh: bool = False, offline: bool = False,
                      ticker: str | None = None) -> dict:
    """Every XBRL fact a company has filed (cached 1 day). Offline: companyfacts_{TICKER}.json."""
    cik = _pad_cik(cik)
    if is_offline(offline):
        return load_fixture("companyfacts", ticker) if ticker else _fixture_by_cik("companyfacts", cik)
    return _fetch_cached(f"companyfacts_CIK{cik}.json", COMPANYFACTS_URL.format(cik=cik), TTL_FACTS, refresh)


def get_submissions(cik: str, refresh: bool = False, offline: bool = False,
                    ticker: str | None = None) -> dict:
    """Filing index (forms, dates, primary documents; cached 1 day). Offline: submissions_{TICKER}.json."""
    cik = _pad_cik(cik)
    if is_offline(offline):
        return load_fixture("submissions", ticker) if ticker else _fixture_by_cik("submissions", cik)
    return _fetch_cached(f"submissions_CIK{cik}.json", SUBMISSIONS_URL.format(cik=cik), TTL_SUBMISSIONS, refresh)


# ------------------------------------------------------------------------------------------
# Small helpers over the companyfacts JSON
# ------------------------------------------------------------------------------------------

def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _one_year_earlier(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28)


def _spec_tags(spec: str | list[str]) -> list[str]:
    return [spec] if isinstance(spec, str) else list(spec)


def _units_for_tag(facts: dict, tag: str) -> dict | None:
    """The {unit: [facts]} dict for "ns:Tag", or None when the company never used the tag."""
    ns, _, name = tag.partition(":")
    return facts.get("facts", {}).get(ns, {}).get(name, {}).get("units")


def _is_currency_code(key: str) -> bool:
    return len(key) == 3 and key.isalpha() and key.isupper()


def _unit_key(units: dict, kind: str, currency: str) -> str | None:
    """Pick the unit bucket to read: money -> currency/USD/any 3-letter code; shares; ccy/shares."""
    if kind == "shares":
        return "shares" if "shares" in units else None
    if kind == "per_share":
        for key in (f"{currency}/shares", "USD/shares"):
            if key in units:
                return key
        return next((k for k in units if k.endswith("/shares")), None)
    for key in (currency, "USD"):
        if key in units:
            return key
    return next((k for k in units if _is_currency_code(k)), None)


def _tag_facts(facts: dict, tag: str, kind: str, currency: str) -> list[dict]:
    """All facts for a tag in the wanted unit (empty list when absent). Skips null values."""
    units = _units_for_tag(facts, tag)
    if not units:
        return []
    key = _unit_key(units, kind, currency)
    if key is None:
        return []
    return [f for f in units.get(key, []) if f.get("val") is not None and f.get("end")]


def _detect_currency(facts: dict) -> str:
    """Currency of the first revenue tag present: USD if available, else its first 3-letter unit."""
    for spec in CONCEPT_TAGS["revenue"]:
        for tag in _spec_tags(spec):
            units = _units_for_tag(facts, tag)
            if units:
                if "USD" in units:
                    return "USD"
                code = next((k for k in units if _is_currency_code(k)), None)
                if code:
                    return code
    return "USD"


def _to_millions(value: float, kind: str) -> float:
    """Raw XBRL -> package units: money and shares / MILLION; per-share values unchanged."""
    return float(value) if kind == "per_share" else float(value) / MILLION


def _latest_filed(candidates: list[dict]) -> dict:
    """The most recently filed fact (latest restatement). Ties: the last one in the list."""
    best = candidates[0]
    for f in candidates[1:]:
        if f.get("filed", "") >= best.get("filed", ""):
            best = f
    return best


def _nearest_end(available: list[date], target: date, tolerance_days: int) -> date | None:
    """Exact match first; otherwise the closest date within +/-tolerance (later date on ties)."""
    if target in available:
        return target
    within = [d for d in available if abs((d - target).days) <= tolerance_days]
    if not within:
        return None
    return min(within, key=lambda d: (abs((d - target).days), -d.toordinal()))


# ------------------------------------------------------------------------------------------
# Fiscal-year (annual) values
# ------------------------------------------------------------------------------------------

def _annual_duration_facts(fact_list: list[dict]) -> dict[date, dict]:
    """
    Group a tag's annual-report duration facts by period end.

    Returns {end: {"fiscal_year": int, "value": raw, "filed": str}} where the label comes
    from the EARLIEST filing that reported the period (the company's own 10-K for that
    year; later 10-Ks restate it as a comparative under a newer `fy`) and the value from
    the LATEST filing (most recent restatement).
    """
    groups: dict[date, list[dict]] = {}
    filing_own_end: dict[str, date] = {}  # accession -> the latest annual period in that filing
    for f in fact_list:
        if f.get("form") not in ANNUAL_FORMS or not f.get("start"):
            continue
        start, end = _parse_date(f["start"]), _parse_date(f["end"])
        if not (ANNUAL_MIN_DAYS <= (end - start).days <= ANNUAL_MAX_DAYS):
            continue  # not a full fiscal year (quarters, 6-month stubs, etc.)
        groups.setdefault(end, []).append(f)
        accn = f.get("accn")
        if accn:
            filing_own_end[accn] = max(filing_own_end.get(accn, end), end)
    out: dict[date, dict] = {}
    for end, group in groups.items():
        earliest = min(group, key=lambda f: f.get("filed", ""))
        latest = _latest_filed(group)
        fy = earliest.get("fy")
        if fy:
            # A filing's `fy` is the filing's OWN fiscal year. The comparative periods it
            # carries (a young company's first 10-K shows three years, all stamped with the
            # same fy) sit k fiscal years earlier, so label them fy - k.
            own_end = filing_own_end.get(earliest.get("accn"), end)
            years_back = round((own_end - end).days / 365.25)
            label = int(fy) - years_back
        else:
            label = end.year
        out[end] = {
            "fiscal_year": label,
            "value": latest["val"],
            "filed": latest.get("filed", ""),
        }
    return out


def _instant_facts_by_end(fact_list: list[dict], forms: set[str] | None = None) -> dict[date, list[dict]]:
    """Balance-sheet (instant) facts grouped by their date, optionally limited to some forms."""
    groups: dict[date, list[dict]] = {}
    for f in fact_list:
        if f.get("start"):
            continue  # duration fact
        if forms is not None and f.get("form") not in forms:
            continue
        groups.setdefault(_parse_date(f["end"]), []).append(f)
    return groups


def _instant_value_at(fact_list: list[dict], target: date, tolerance_days: int,
                      forms: set[str] | None = ANNUAL_FORMS) -> float | None:
    """Raw instant value at `target` (exact, then +/-tolerance), latest filing wins."""
    groups = _instant_facts_by_end(fact_list, forms)
    end = _nearest_end(list(groups), target, tolerance_days)
    if end is None:
        return None
    return _latest_filed(groups[end])["val"]


def _latest_instant_between(fact_list: list[dict], lo: date, hi: date) -> float | None:
    """Raw value of the most recent instant fact (any form) dated within [lo, hi]."""
    groups = _instant_facts_by_end(fact_list)
    ends = [d for d in groups if lo <= d <= hi]
    if not ends:
        return None
    return _latest_filed(groups[max(ends)])["val"]


# ------------------------------------------------------------------------------------------
# TTM (trailing twelve months) building blocks
# ------------------------------------------------------------------------------------------

def _ytd_period(facts: dict, currency: str, e_fy: date) -> tuple[date, date] | None:
    """
    Find the year-to-date window of the latest 10-Q after the last 10-K, from revenue facts.

    YTD_cur = a quarterly-form duration fact starting within +/-7 days of E_fy + 1 day and
    ending after E_fy; among candidates take the latest end (the newest 10-Q). Returns
    (start, end) or None when no 10-Q has been filed since the last annual report.
    """
    day_after_fy = e_fy + timedelta(days=1)
    best: dict | None = None
    for spec in CONCEPT_TAGS["revenue"]:
        for tag in _spec_tags(spec):
            for f in _tag_facts(facts, tag, "money", currency):
                if f.get("form") not in QUARTERLY_FORMS or not f.get("start"):
                    continue
                start, end = _parse_date(f["start"]), _parse_date(f["end"])
                if abs((start - day_after_fy).days) > YTD_START_TOLERANCE_DAYS or end <= e_fy:
                    continue
                if best is None or (end, f.get("filed", "")) > (_parse_date(best["end"]), best.get("filed", "")):
                    best = f
    if best is None:
        return None
    return _parse_date(best["start"]), _parse_date(best["end"])


def _ytd_facts(fact_list: list[dict], ytd_start: date, ytd_end: date) -> tuple[dict | None, dict | None]:
    """
    For one tag, the (YTD_cur, YTD_prior) facts matching the TTM window.

    YTD_cur: quarterly form, start within +/-7 days of ytd_start, end within +/-7 days of ytd_end.
    YTD_prior: any form, end within +/-10 days of ytd_end minus one year, duration within
    +/-10 days of YTD_cur's. Both prefer the latest filing (a restated comparative in the
    newest 10-Q beats the number originally reported a year earlier).
    """
    duration = (ytd_end - ytd_start).days
    prior_end_target = _one_year_earlier(ytd_end)
    cur_candidates: list[dict] = []
    prior_candidates: list[dict] = []
    for f in fact_list:
        if not f.get("start"):
            continue
        start, end = _parse_date(f["start"]), _parse_date(f["end"])
        if (f.get("form") in QUARTERLY_FORMS
                and abs((start - ytd_start).days) <= YTD_START_TOLERANCE_DAYS
                and abs((end - ytd_end).days) <= FY_END_TOLERANCE_DAYS):
            cur_candidates.append(f)
        if (abs((end - prior_end_target).days) <= YTD_PRIOR_TOLERANCE_DAYS
                and abs((end - start).days - duration) <= YTD_PRIOR_TOLERANCE_DAYS):
            prior_candidates.append(f)
    cur = _latest_filed(cur_candidates) if cur_candidates else None
    prior = _latest_filed(prior_candidates) if prior_candidates else None
    return cur, prior


# ------------------------------------------------------------------------------------------
# Concept resolution: tag specs -> values, per period
# ------------------------------------------------------------------------------------------

Lookup = Callable[[str], float | None]  # "ns:Tag" -> value in package units, or None


def _resolve(spec: str | list[str], lookup: Lookup) -> tuple[float | None, str | None]:
    """A single tag, or the sum of the list's components that exist (None if none exist)."""
    found = [(tag, lookup(tag)) for tag in _spec_tags(spec)]
    found = [(tag, value) for tag, value in found if value is not None]
    if not found:
        return None, None
    return sum(value for _, value in found), "+".join(tag for tag, _ in found)


def _first_available(specs: list, lookup: Lookup) -> tuple[float | None, str | None]:
    """Per-period tag fallback: the first spec (in order) with a value for this period wins."""
    for spec in specs:
        value, label = _resolve(spec, lookup)
        if value is not None:
            return value, label
    return None, None


def _total_debt(lookup: Lookup) -> tuple[float | None, str | None]:
    """
    Total debt in $mm following DESIGN.md (operating leases excluded; finance leases only
    if the company embeds them in these debt tags):

        DebtLongtermAndShorttermCombinedAmount                       (already the total)
        else LongTermDebt (taxonomy definition INCLUDES the current portion)
             [or LongTermDebtAndCapitalLeaseObligations, same meaning incl. finance leases]
          + short-term piece
        else LongTermDebtNoncurrent + (DebtCurrent or LongTermDebtCurrent)
          + short-term piece only when the current piece is NOT DebtCurrent
            (DebtCurrent already contains short-term borrowings -> avoid double counting)
        else current piece alone (+ short-term piece, same rule)
        else IFRS Borrowings, else IFRS long-term + short-term borrowings, else NaN.

    Short-term piece = ShortTermBorrowings if tagged, else CommercialPaper, else 0. In the
    us-gaap taxonomy CommercialPaper is one KIND of short-term borrowing, so the two are
    never added together (that would count the paper twice).
    """
    combined = lookup("us-gaap:DebtLongtermAndShorttermCombinedAmount")
    if combined is not None:
        return combined, "us-gaap:DebtLongtermAndShorttermCombinedAmount"

    noncurrent = lookup("us-gaap:LongTermDebtNoncurrent")
    debt_current = lookup("us-gaap:DebtCurrent")
    ltd_current = lookup("us-gaap:LongTermDebtCurrent")
    short_term_borrowings = lookup("us-gaap:ShortTermBorrowings")
    commercial_paper = lookup("us-gaap:CommercialPaper")

    if short_term_borrowings is not None:
        short_term, short_term_label = short_term_borrowings, ["us-gaap:ShortTermBorrowings"]
    elif commercial_paper is not None:
        short_term, short_term_label = commercial_paper, ["us-gaap:CommercialPaper"]
    else:
        short_term, short_term_label = 0.0, []

    for total_tag in ("us-gaap:LongTermDebt", "us-gaap:LongTermDebtAndCapitalLeaseObligations"):
        long_term_debt = lookup(total_tag)
        if long_term_debt is not None:
            return long_term_debt + short_term, "+".join([total_tag] + short_term_label)

    # Current piece: DebtCurrent already includes short-term borrowings/CP.
    if debt_current is not None:
        current, current_label, add_short_term = debt_current, "us-gaap:DebtCurrent", False
    elif ltd_current is not None:
        current, current_label, add_short_term = ltd_current, "us-gaap:LongTermDebtCurrent", True
    else:
        current, current_label, add_short_term = None, None, True

    if noncurrent is not None:
        total, parts = noncurrent, ["us-gaap:LongTermDebtNoncurrent"]
        if current is not None:
            total, parts = total + current, parts + [current_label]
        if add_short_term:
            total, parts = total + short_term, parts + short_term_label
        return total, "+".join(parts)
    if current is not None:
        total, parts = current, [current_label]
        if add_short_term:
            total, parts = total + short_term, parts + short_term_label
        return total, "+".join(parts)

    borrowings = lookup("ifrs-full:Borrowings")
    if borrowings is not None:
        return borrowings, "ifrs-full:Borrowings"
    ifrs_parts = [(t, lookup(t)) for t in ("ifrs-full:LongtermBorrowings", "ifrs-full:ShorttermBorrowings")]
    ifrs_parts = [(t, v) for t, v in ifrs_parts if v is not None]
    if ifrs_parts:
        return sum(v for _, v in ifrs_parts), "+".join(t for t, _ in ifrs_parts)
    return None, None


# --- lookup factories: each returns a function "ns:Tag" -> value for ONE period ------------

def _fy_flow_lookup(facts: dict, currency: str, period_end: date, kind: str) -> Lookup:
    """Annual duration value whose end matches period_end (exact, then +/-7 days)."""
    def lookup(tag: str) -> float | None:
        index = _annual_duration_facts(_tag_facts(facts, tag, kind, currency))
        end = _nearest_end(list(index), period_end, FY_END_TOLERANCE_DAYS)
        return None if end is None else _to_millions(index[end]["value"], kind)
    return lookup


def _fy_instant_lookup(facts: dict, currency: str, period_end: date, kind: str = "money") -> Lookup:
    """Balance-sheet value at the fiscal year end from annual reports (exact, then +/-7 days)."""
    def lookup(tag: str) -> float | None:
        raw = _instant_value_at(_tag_facts(facts, tag, kind, currency), period_end, FY_END_TOLERANCE_DAYS)
        return None if raw is None else _to_millions(raw, kind)
    return lookup


def _ttm_delta_lookup(facts: dict, currency: str, ytd_start: date, ytd_end: date, kind: str) -> Lookup:
    """YTD_cur - YTD_prior for a tag (None unless BOTH exist); TTM = FY + this delta."""
    def lookup(tag: str) -> float | None:
        cur, prior = _ytd_facts(_tag_facts(facts, tag, kind, currency), ytd_start, ytd_end)
        if cur is None or prior is None:
            return None
        return _to_millions(cur["val"], kind) - _to_millions(prior["val"], kind)
    return lookup


def _ttm_cur_lookup(facts: dict, currency: str, ytd_start: date, ytd_end: date, kind: str) -> Lookup:
    """The YTD_cur value itself (used for diluted shares: a weighted average, not a flow)."""
    def lookup(tag: str) -> float | None:
        cur, _ = _ytd_facts(_tag_facts(facts, tag, kind, currency), ytd_start, ytd_end)
        return None if cur is None else _to_millions(cur["val"], kind)
    return lookup


def _ttm_balance_sheet_date(facts: dict, currency: str, e_fy: date, ytd_end: date) -> date | None:
    """
    The date of the latest balance sheet filed after the fiscal year end (up to the latest
    quarter end), taken from the core balance-sheet tags (cash, then total equity).

    Every TTM balance-sheet item is then read at THIS one date, so debt, cash and minority
    interest always come from the same balance sheet - never fiscal-year-end debt added to
    quarter-end commercial paper.
    """
    candidates: list[date] = []
    for concept in ("cash", "total_equity"):
        for spec in CONCEPT_TAGS[concept]:
            for tag in _spec_tags(spec):
                groups = _instant_facts_by_end(_tag_facts(facts, tag, "money", currency))
                candidates.extend(d for d in groups if e_fy < d <= ytd_end)
    return max(candidates) if candidates else None


def _ttm_instant_lookup(facts: dict, currency: str, bs_date: date, kind: str = "money") -> Lookup:
    """Balance-sheet value (any form) at `bs_date` (exact, then +/-7 days); None if not reported there."""
    def lookup(tag: str) -> float | None:
        raw = _instant_value_at(_tag_facts(facts, tag, kind, currency), bs_date, FY_END_TOLERANCE_DAYS, forms=None)
        return None if raw is None else _to_millions(raw, kind)
    return lookup


def _ttm_latest_lookup(facts: dict, currency: str, lo: date, hi: date, kind: str = "money") -> Lookup:
    """Latest balance-sheet value (any form) dated within [lo, hi] - the fallback when a
    concept is not reported at the anchored balance-sheet date."""
    def lookup(tag: str) -> float | None:
        raw = _latest_instant_between(_tag_facts(facts, tag, kind, currency), lo, hi)
        return None if raw is None else _to_millions(raw, kind)
    return lookup


def _is_num(value) -> bool:
    return value is not None and not pd.isna(value)


# ------------------------------------------------------------------------------------------
# Row assembly
# ------------------------------------------------------------------------------------------

def _canonical_periods(facts: dict, currency: str, n_years: int) -> list[tuple[date, int]]:
    """
    The fiscal periods the table will show: the `n_years` most recent period ends that have a
    revenue value under ANY revenue tag, oldest first, each with its fiscal-year label.
    The label is taken across all revenue tags so it always comes from the earliest filing.
    """
    all_revenue_facts: list[dict] = []
    for spec in CONCEPT_TAGS["revenue"]:
        for tag in _spec_tags(spec):
            all_revenue_facts.extend(_tag_facts(facts, tag, "money", currency))
    index = _annual_duration_facts(all_revenue_facts)
    chosen: list[date] = []
    for end in sorted(index, reverse=True):
        # 52/53-week ends never sit within a week of each other; anything that close is a duplicate.
        if any(abs((end - c).days) <= FY_END_TOLERANCE_DAYS for c in chosen):
            continue
        chosen.append(end)
        if len(chosen) == n_years:
            break
    return [(end, index[end]["fiscal_year"]) for end in sorted(chosen)]


def _blank_row() -> dict:
    row = {c: np.nan for c in COLUMNS}
    row["currency"] = "USD"
    return row


def _finish_row(row: dict) -> dict:
    """Apply the zero defaults and the two derived columns."""
    for concept in ZERO_WHEN_ABSENT:
        if pd.isna(row[concept]):
            row[concept] = 0.0
    # EBITDA = operating income (EBIT) + depreciation & amortization (standard definition).
    row["ebitda"] = row["ebit"] + row["da"]
    # Tangible book value = shareholders' equity - preferred stock - goodwill - other intangibles:
    # tangible COMMON equity, so P/TBV (common market cap over it) is the per-common-share
    # ratio bank comps quote. Preferred/goodwill/intangibles default to 0 when untagged.
    row["tangible_book_value"] = (row["total_equity"] - row["preferred"]
                                  - row["goodwill"] - row["intangibles"])
    return row


def _record(tags_used: dict, concept: str, period_end: date, label: str | None) -> None:
    if label:
        tags_used.setdefault(concept, {})[period_end.isoformat()] = label


def _fy_row(facts: dict, currency: str, period_end: date, fiscal_year: int, tags_used: dict) -> dict:
    row = _blank_row()
    row.update(fiscal_year=fiscal_year, period_type="FY", period_end=period_end.isoformat(), currency=currency)
    for concept in FLOW_CONCEPTS:
        kind = UNIT_KIND.get(concept, "money")
        value, label = _first_available(CONCEPT_TAGS[concept], _fy_flow_lookup(facts, currency, period_end, kind))
        if value is not None:
            row[concept] = value
        _record(tags_used, concept, period_end, label)
    instant = _fy_instant_lookup(facts, currency, period_end)
    for concept in INSTANT_CONCEPTS:
        value, label = _first_available(CONCEPT_TAGS[concept], instant)
        if value is not None:
            row[concept] = value
        _record(tags_used, concept, period_end, label)
    debt, label = _total_debt(instant)
    if debt is not None:
        row["total_debt"] = debt
    _record(tags_used, "total_debt", period_end, label)
    return _finish_row(row)


def _ttm_row(facts: dict, currency: str, latest_fy: dict, tags_used: dict, warnings: list[str]) -> dict:
    """
    TTM = FY_latest + YTD_cur - YTD_prior ("roll-forward" TTM construction). For EPS this is
    an approximation: it ignores the drift in the diluted share count between the periods.
    Balance-sheet columns are simply the latest reported balance sheet.
    """
    e_fy = _parse_date(latest_fy["period_end"])
    row = dict(latest_fy)  # start from the FY values; every fallback below keeps them
    row["period_type"] = "TTM"

    window = _ytd_period(facts, currency, e_fy)
    if window is None:
        warnings.append("no 10-Q after latest 10-K; TTM = FY")
        return _finish_row(row)
    ytd_start, ytd_end = window
    row["period_end"] = ytd_end.isoformat()

    for concept in FLOW_CONCEPTS:
        kind = UNIT_KIND.get(concept, "money")
        if concept == "diluted_shares":
            # A weighted-average share count is not a flow: use the YTD_cur figure, else FY.
            value, label = _first_available(CONCEPT_TAGS[concept],
                                            _ttm_cur_lookup(facts, currency, ytd_start, ytd_end, kind))
            if value is not None:
                row[concept] = value
                _record(tags_used, concept, ytd_end, label)
            continue
        delta, label = _first_available(CONCEPT_TAGS[concept],
                                        _ttm_delta_lookup(facts, currency, ytd_start, ytd_end, kind))
        if delta is None:
            # Concepts the company never reports (a bank's EBIT) are flagged once by
            # extract_financials; only warn here when there IS an FY value to fall back to.
            if not pd.isna(latest_fy[concept]):
                warnings.append(f"{concept}: no quarterly year-to-date facts to roll forward; TTM = FY")
            continue
        if pd.isna(latest_fy[concept]):
            warnings.append(f"{concept}: latest fiscal-year value missing; TTM is NaN")
            continue
        row[concept] = latest_fy[concept] + delta
        _record(tags_used, concept, ytd_end, label)

    # Share-basis check. A stock split between the latest 10-K and the latest 10-Q leaves
    # the FY EPS in pre-split dollars while the 10-Q deltas are post-split (the 10-Q restates
    # its comparatives), so the roll-forward would be wrong by the split ratio. When the
    # diluted share count moved outside the +/-20-25% band, rebuild TTM EPS on the current
    # share basis instead: TTM net income / latest diluted share count.
    fy_shares, ttm_shares = latest_fy.get("diluted_shares"), row.get("diluted_shares")
    if _is_num(fy_shares) and _is_num(ttm_shares) and fy_shares > 0 and ttm_shares > 0:
        ratio = ttm_shares / fy_shares
        if not (SHARE_BASIS_MIN_RATIO <= ratio <= SHARE_BASIS_MAX_RATIO):
            if _is_num(row.get("net_income")):
                row["eps_diluted"] = row["net_income"] / ttm_shares  # $mm / mm shares = $/share
                _record(tags_used, "eps_diluted", ytd_end, "net_income/diluted_shares")
            else:
                row["eps_diluted"] = np.nan
            warnings.append(
                f"eps_diluted: diluted share count changed {ratio:.2f}x between the latest 10-K and "
                "10-Q (stock split?); TTM EPS = TTM net income / latest diluted shares"
            )

    # Balance sheet: every item at the same (latest) balance-sheet date.
    bs_date = _ttm_balance_sheet_date(facts, currency, e_fy, ytd_end)
    if bs_date is None:
        warnings.append(f"balance sheet: no balance sheet reported after {e_fy}; TTM balance-sheet items = FY")
        return _finish_row(row)
    instant = _ttm_instant_lookup(facts, currency, bs_date)
    earlier = _ttm_latest_lookup(facts, currency, e_fy, bs_date)  # fallback: latest value before the anchor
    for concept in INSTANT_CONCEPTS:
        value, label = _first_available(CONCEPT_TAGS[concept], instant)
        if value is None:
            value, label = _first_available(CONCEPT_TAGS[concept], earlier)
            if value is not None and value != 0:
                warnings.append(f"{concept}: not reported at the {bs_date} balance sheet; using the latest earlier value")
        if value is not None:
            row[concept] = value
            _record(tags_used, concept, ytd_end, label)
    debt, label = _total_debt(instant)
    if debt is None:
        debt, label = _total_debt(earlier)
        if debt is not None:
            warnings.append(f"total_debt: no debt facts at the {bs_date} balance sheet; using the latest earlier value")
    if debt is not None:
        row["total_debt"] = debt
        _record(tags_used, "total_debt", ytd_end, label)
    return _finish_row(row)


def extract_financials(facts: dict, n_years: int = 4) -> pd.DataFrame:
    """
    companyfacts JSON -> DataFrame of the last `n_years` fiscal years (oldest first) plus one
    TTM row (last). Columns and units per docs/DESIGN.md section 2. Diagnostics are attached
    as df.attrs["tags_used"] and df.attrs["warnings"].

    Raises ValueError when no annual revenue facts exist (nothing to build a table from).
    """
    currency = _detect_currency(facts)
    tags_used: dict[str, dict[str, str]] = {}
    warnings: list[str] = []

    periods = _canonical_periods(facts, currency, n_years)
    if not periods:
        raise ValueError(
            f"no annual revenue facts found for {facts.get('entityName', 'company')!r} "
            f"(tried {[t for s in CONCEPT_TAGS['revenue'] for t in _spec_tags(s)]})"
        )

    rows = [_fy_row(facts, currency, end, fy, tags_used) for end, fy in periods]
    rows.append(_ttm_row(facts, currency, rows[-1], tags_used, warnings))

    # A period with a balance sheet (cash or equity reported) but no debt element under any
    # tag is treated as debt-free: total_debt = 0 so EV can be computed, with a warning so
    # the reader verifies it (debt tagged under an element we do not read would be missed).
    debt_free_periods = []
    for r in rows:
        if pd.isna(r["total_debt"]) and (_is_num(r["cash"]) or _is_num(r["total_equity"])):
            r["total_debt"] = 0.0
            debt_free_periods.append(r["period_end"])
            tags_used.setdefault("total_debt", {})[r["period_end"]] = "none tagged (treated as debt-free)"
    if debt_free_periods:
        warnings.append(
            "total_debt: no debt tag found (LongTermDebt, DebtCurrent, ShortTermBorrowings, "
            f"CommercialPaper all absent) for {', '.join(debt_free_periods)}; treated as debt-free - verify"
        )

    for concept in FLOW_CONCEPTS + INSTANT_CONCEPTS + ("total_debt",):
        if concept not in ZERO_WHEN_ABSENT and all(pd.isna(r[concept]) for r in rows):
            warnings.append(f"{concept}: no facts found for any period; value is NaN")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["fiscal_year"] = df["fiscal_year"].astype("int64")
    for column in NUMERIC_COLUMNS:
        df[column] = df[column].astype("float64")
    for column in ("period_type", "period_end", "currency"):
        df[column] = df[column].astype("str")
    df.attrs["tags_used"] = tags_used
    df.attrs["warnings"] = warnings
    for message in warnings:
        log.info("%s: %s", facts.get("entityName", "company"), message)
    return df


# ------------------------------------------------------------------------------------------
# CLI:  python -m compsai.edgar AAPL MSFT GOOGL [--debug] [--offline] [--save-fixture DIR]
# ------------------------------------------------------------------------------------------

def _save_fixture(directory: Path, ticker: str, facts: dict, submissions: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"companyfacts_{ticker}.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")
    (directory / f"submissions_{ticker}.json").write_text(json.dumps(submissions, indent=1), encoding="utf-8")
    log.info("saved raw EDGAR JSON for %s to %s", ticker, directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m compsai.edgar",
        description="Print the fiscal-year + TTM financials table CompsAI extracts from SEC EDGAR.",
    )
    parser.add_argument("tickers", nargs="+", help="tickers, e.g. AAPL MSFT GOOGL")
    parser.add_argument("--debug", action="store_true", help="debug logging + print tags_used/warnings")
    parser.add_argument("--offline", action="store_true", help="read fixtures instead of the network")
    parser.add_argument("--save-fixture", metavar="DIR", help="write raw companyfacts/submissions JSON here")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

    failures = 0
    for raw_ticker in args.tickers:
        ticker = raw_ticker.upper()
        try:
            cik = get_cik(ticker, offline=args.offline)
            name = get_company_name(ticker, offline=args.offline)
            facts = get_company_facts(cik, offline=args.offline, ticker=ticker)
            if args.save_fixture:
                submissions = get_submissions(cik, offline=args.offline, ticker=ticker)
                _save_fixture(Path(args.save_fixture), ticker, facts, submissions)
            df = extract_financials(facts)
        except Exception as exc:  # one bad ticker must not stop the others
            log.error("%s: %s", ticker, exc, exc_info=args.debug)
            failures += 1
            continue
        print(f"\n=== {ticker} — {name} (CIK {cik}) — $ in millions except per share ===")
        print(df.to_string(index=False))
        if args.debug:
            print("tags_used:")
            print(json.dumps(df.attrs["tags_used"], indent=2))
            print("warnings:", df.attrs["warnings"] or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
