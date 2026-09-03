#!/usr/bin/env python
"""
Deterministic generator for CompsAI's offline test fixtures.

Why this exists: the sandbox that authored CompsAI had no network access, and the unit
tests must never touch sec.gov or Yahoo Finance. So we synthesise three fictional filers
whose JSON mirrors the real EDGAR shapes EXACTLY (companyfacts, submissions) plus a small
market-data JSON per ticker. Numbers are ROUND on purpose so a human can check every
derived value (TTM roll-forward, total debt, tangible book value) by hand.

Run from the project root to (re)generate every fixture:

    python tests/fixtures/make_fixtures.py

To swap in real data later:  python -m compsai.edgar AAPL --save-fixture tests/fixtures

The three fixtures and the EDGAR quirks each one exercises
-----------------------------------------------------------
FIXA "Fixture Fruit Inc." (CIK 1, FYE = last Saturday of September, Apple-like)
  * 52/53-week fiscal years: FY2023 has 53 weeks (2022-09-25 .. 2023-09-30).
  * Revenue tag switch: 10-Ks for FY<=2021 report `Revenues`; FY>=2022 report
    `RevenueFromContractWithCustomerExcludingAssessedTax`. FY2021 therefore exists under
    BOTH tags (the FY2022/FY2023 10-Ks restate it as a comparative under the new tag), so a
    company-level "first tag that exists" choice would give NaN for FY2022+ (per-period
    fallback is required).
  * Every 10-K restates two prior years of income statement and one prior year of balance
    sheet with the NEWER filing's `fy`/`filed`. FY2023 net income was 97,000 in the FY2023
    10-K and restated to 97,500 in the FY2024 10-K ("latest filed wins").
  * One 10-K/A (FY2022) duplicating the FY2022 10-K facts.
  * FY2024 and FY2025 10-Qs (Q1-Q3) with 3-month AND year-to-date facts plus prior-year
    comparatives. The 9-month FY2024 revenue was 284,000 in the original FY2024 Q3 10-Q and
    restated to 285,000 in the FY2025 Q3 10-Q comparative (latest filed wins for YTD_prior).
  * Debt = LongTermDebtNoncurrent + LongTermDebtCurrent + CommercialPaper.
  * No goodwill / intangibles / minority / preferred tags (-> default 0).

FIXB "Fixture Software Corp." (CIK 2, FYE June 30, Microsoft-like)
  * D&A via `DepreciationAmortizationAndOther`.
  * Debt via `LongTermDebt` (which by taxonomy definition INCLUDES the current portion);
    `LongTermDebtNoncurrent` and `LongTermDebtCurrent` are ALSO reported and must not be
    added on top.
  * `MinorityInterest`, `Goodwill`, `IntangibleAssetsNetExcludingGoodwill` present.
  * Latest filing is the FY2025 10-K; the FY2025 10-Qs all predate it -> TTM = FY.
  * Two share classes on the cover page (dei facts with identical end/filed) that must be
    summed: 5,400 + 2,000 = 7,400 mm.

FIXC "Fixture Bancorp" (CIK 3, FYE Dec 31, bank)
  * `Revenues`; NO OperatingIncomeLoss and NO D&A tags (EBITDA is NaN by design).
  * `StockholdersEquity`, `Goodwill`, `IntangibleAssetsNetExcludingGoodwill`,
    `PreferredStockValue` all > 0 (tangible book value = equity - goodwill - intangibles).
  * Debt = LongTermDebt + ShortTermBorrowings.
  * One 10-Q (Q1 2025) after the FY2024 10-K -> TTM = FY2024 + Q1 2025 - Q1 2024.

Hand-check values (all $mm; the tests assert these with the arithmetic written out)
------------------------------------------------------------------------------------
FIXA TTM (period end 2025-06-28):
  revenue        = FY2024 391,000 + YTD 9m FY2025 300,000 - YTD 9m FY2024 285,000 = 406,000
  ebit           = 123,000 + 100,000 - 93,000 = 130,000
  da             =  11,400 +   9,000 -  8,500 =  11,900      -> ebitda = 141,900
  net income     =  94,000 +  84,000 - 79,000 =  99,000
  diluted EPS    =    6.10 +    5.60 -   5.10 =    6.60
  diluted shares = 9m FY2025 weighted average = 15,100
  balance sheet at 2025-06-28: debt 78,000 + 12,000 + 2,000 = 92,000; cash 28,000; equity 66,000
FIXB TTM = FY2025 (no 10-Q after the 10-K).
FIXC TTM (period end 2025-03-31):
  revenue = 160,000 + 45,000 - 40,000 = 165,000;  net income = 50,000 + 14,000 - 12,000 = 52,000
  diluted EPS = 17.50 + 5.00 - 4.20 = 18.30; debt 410,000 + 40,000 = 450,000
  TBV = 350,000 - 52,000 - 3,000 = 295,000 (FY2024: 345,000 - 52,000 - 3,000 = 290,000)
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
MILLION = 1_000_000

# Sentinel concept key resolved to a real tag per filing (Apple's revenue tag switch).
REVENUE = "revenue"
EPS_TAG = "EarningsPerShareDiluted"
SHARES_TAG = "WeightedAverageNumberOfDilutedSharesOutstanding"
DEI_SHARES_TAG = "EntityCommonStockSharesOutstanding"

# Flow concepts that are summed across quarters to build year-to-date values.
# (EPS is summed too — a simplification; diluted shares use the average of the quarters.)
FLOW_TAGS_FOR_YTD_SUM = {
    REVENUE,
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "OperatingIncomeLoss",
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndOther",
    "NetIncomeLoss",
    EPS_TAG,
}

LABELS = {
    "Revenues": "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax":
        "Revenue from Contract with Customer, Excluding Assessed Tax",
    "OperatingIncomeLoss": "Operating Income (Loss)",
    "DepreciationDepletionAndAmortization": "Depreciation, Depletion and Amortization",
    "DepreciationAmortizationAndOther": "Depreciation, Amortization and Other",
    "NetIncomeLoss": "Net Income (Loss) Attributable to Parent",
    EPS_TAG: "Earnings Per Share, Diluted",
    SHARES_TAG: "Weighted Average Number of Shares Outstanding, Diluted",
    "LongTermDebt": "Long-Term Debt",
    "LongTermDebtNoncurrent": "Long-Term Debt, Excluding Current Maturities",
    "LongTermDebtCurrent": "Long-Term Debt, Current Maturities",
    "CommercialPaper": "Commercial Paper",
    "ShortTermBorrowings": "Short-Term Borrowings",
    "CashAndCashEquivalentsAtCarryingValue": "Cash and Cash Equivalents, at Carrying Value",
    "MinorityInterest": "Stockholders' Equity Attributable to Noncontrolling Interest",
    "PreferredStockValue": "Preferred Stock, Value, Issued",
    "StockholdersEquity": "Stockholders' Equity Attributable to Parent",
    "Goodwill": "Goodwill",
    "IntangibleAssetsNetExcludingGoodwill": "Intangible Assets, Net (Excluding Goodwill)",
    DEI_SHARES_TAG: "Entity Common Stock, Shares Outstanding",
}


def D(s: str) -> date:
    return date.fromisoformat(s)


def unit_for(tag: str) -> str:
    if tag == EPS_TAG:
        return "USD/shares"
    if tag in (SHARES_TAG, DEI_SHARES_TAG):
        return "shares"
    return "USD"


def raw_value(tag: str, val: float) -> int | float:
    """Fixture numbers are written in $mm / mm shares; EDGAR stores raw units."""
    if tag == EPS_TAG:
        return round(float(val), 2)
    return int(round(val * MILLION))


def frame_for(start: date | None, end: date) -> str:
    """Informational SEC `frame` string (the algorithm never reads it)."""
    if start is None:
        return f"CY{end.year}Q{(end.month - 1) // 3 + 1}I"
    mid = start + (end - start) / 2
    days = (end - start).days
    if days > 300:
        return f"CY{mid.year}"
    return f"CY{mid.year}Q{(mid.month - 1) // 3 + 1}"


class FactsBuilder:
    """Accumulates facts in the exact companyfacts JSON layout."""

    def __init__(self, cik: int, name: str, overrides: dict | None = None):
        self.doc = {"cik": cik, "entityName": name, "facts": {}}
        self.overrides = overrides or {}

    def add(self, ns: str, tag: str, *, val: float, end: date, accn: str, fy: int, fp: str,
            form: str, filed: str, start: date | None = None, frame: str | None = None) -> None:
        # Restatements: an override keyed by (accession, tag, start, end) replaces the value
        # reported in THAT filing only, so the same period carries different values in
        # different filings (exactly what happens on EDGAR).
        key = (accn, tag, start.isoformat() if start else None, end.isoformat())
        val = self.overrides.get(key, val)
        entry = self.doc["facts"].setdefault(ns, {}).setdefault(
            tag,
            {"label": LABELS.get(tag, tag), "description": f"Synthetic fixture fact for {tag}.",
             "units": {}},
        )
        facts = entry["units"].setdefault(unit_for(tag), [])
        fact: dict = {}
        if start is not None:
            fact["start"] = start.isoformat()
        fact["end"] = end.isoformat()
        fact["val"] = raw_value(tag, val)
        fact["accn"] = accn
        fact["fy"] = fy
        fact["fp"] = fp
        fact["form"] = form
        fact["filed"] = filed
        if frame:
            fact["frame"] = frame
        facts.append(fact)

    def finish(self) -> dict:
        for ns in self.doc["facts"].values():
            for entry in ns.values():
                for facts in entry["units"].values():
                    facts.sort(key=lambda f: (f["end"], f["filed"], f.get("start", "")))
        return self.doc


def quarter_bounds(spec: dict, fy: int, q: int) -> tuple[date, date]:
    """Quarter q of fiscal year fy: Q1 starts with the fiscal year, Qn starts after Q(n-1)."""
    end = D(spec["quarters"][(fy, q)]["end"])
    start = D(spec["fy_periods"][fy][0]) if q == 1 else D(spec["quarters"][(fy, q - 1)]["end"]) + timedelta(days=1)
    return start, end


def ytd_income(spec: dict, fy: int, q: int) -> dict:
    """Year-to-date income statement = sum of quarters 1..q (shares = average)."""
    quarters = [spec["quarters"][(fy, i)]["income"] for i in range(1, q + 1)]
    out: dict = {}
    for tag in quarters[0]:
        vals = [qi[tag] for qi in quarters]
        if tag == SHARES_TAG:
            out[tag] = round(sum(vals) / len(vals))
        else:
            out[tag] = round(sum(vals), 2)
    return out


def resolve_revenue_tag(spec: dict, filing_fy: int) -> str:
    rt = spec["revenue_tag"]
    return rt(filing_fy) if callable(rt) else rt


def build_facts(spec: dict) -> dict:
    b = FactsBuilder(spec["cik"], spec["name"], spec.get("overrides"))

    def add_income(values: dict, start: date, end: date, *, accn, fy, fp, form, filed, original):
        for tag, val in values.items():
            real_tag = resolve_revenue_tag(spec, fy) if tag == REVENUE else tag
            b.add("us-gaap", real_tag, val=val, start=start, end=end, accn=accn, fy=fy, fp=fp,
                  form=form, filed=filed, frame=frame_for(start, end) if original else None)

    def add_balance(values: dict, end: date, *, accn, fy, fp, form, filed, original):
        for tag, val in values.items():
            b.add("us-gaap", tag, val=val, end=end, accn=accn, fy=fy, fp=fp, form=form,
                  filed=filed, frame=frame_for(None, end) if original else None)

    def add_dei(accn, fy, fp, form, filed):
        cover_date, classes = spec["dei"][accn]
        for shares in classes:  # several entries with identical end/filed = share classes
            b.add("dei", DEI_SHARES_TAG, val=shares, end=D(cover_date), accn=accn, fy=fy, fp=fp,
                  form=form, filed=filed)

    # --- Annual reports: current year + 2 prior years of P&L, + 1 prior year of balance sheet.
    for fy, accn, filed, form in spec["annual_filings"]:
        for k in range(3):
            y = fy - k
            if y in spec["income"]:
                start, end = (D(x) for x in spec["fy_periods"][y])
                add_income(spec["income"][y], start, end, accn=accn, fy=fy, fp="FY", form=form,
                           filed=filed, original=(k == 0 and form == "10-K"))
        for k in range(2):
            y = fy - k
            if y in spec["balance"]:
                end = D(spec["fy_periods"][y][1])
                add_balance(spec["balance"][y], end, accn=accn, fy=fy, fp="FY", form=form,
                            filed=filed, original=(k == 0 and form == "10-K"))
        add_dei(accn, fy, "FY", form, filed)

    # --- Quarterly reports: 3-month facts (+ YTD for Q2/Q3), prior-year comparatives,
    #     quarter-end balance sheet and the prior fiscal-year-end comparative balance sheet.
    for fy, q, accn, filed in spec["quarterly_filings"]:
        fp = f"Q{q}"
        for year, original in ((fy, True), (fy - 1, False)):
            start, end = quarter_bounds(spec, year, q)
            add_income(spec["quarters"][(year, q)]["income"], start, end, accn=accn, fy=fy, fp=fp,
                       form="10-Q", filed=filed, original=original)
            if q >= 2:
                ytd_start = D(spec["fy_periods"][year][0])
                add_income(ytd_income(spec, year, q), ytd_start, end, accn=accn, fy=fy, fp=fp,
                           form="10-Q", filed=filed, original=False)
        start, end = quarter_bounds(spec, fy, q)
        add_balance(spec["quarters"][(fy, q)]["balance"], end, accn=accn, fy=fy, fp=fp,
                    form="10-Q", filed=filed, original=True)
        prior_fy_end = D(spec["fy_periods"][fy - 1][1])
        add_balance(spec["balance"][fy - 1], prior_fy_end, accn=accn, fy=fy, fp=fp, form="10-Q",
                    filed=filed, original=False)
        add_dei(accn, fy, fp, "10-Q", filed)

    return b.finish()


def build_submissions(spec: dict) -> dict:
    """Parallel-array filing index, newest first, including non-XBRL 8-Ks."""
    ticker = spec["ticker"].lower()
    filings: list[dict] = []
    for fy, accn, filed, form in spec["annual_filings"]:
        report = spec["fy_periods"][fy][1]
        filings.append({"accessionNumber": accn, "filingDate": filed, "reportDate": report,
                        "form": form, "primaryDocument": f"{ticker}-{report.replace('-', '')}.htm",
                        "primaryDocDescription": form})
    for fy, q, accn, filed in spec["quarterly_filings"]:
        report = spec["quarters"][(fy, q)]["end"]
        filings.append({"accessionNumber": accn, "filingDate": filed, "reportDate": report,
                        "form": "10-Q", "primaryDocument": f"{ticker}-{report.replace('-', '')}.htm",
                        "primaryDocDescription": "10-Q"})
    for form, accn, filed, report in spec["other_filings"]:
        filings.append({"accessionNumber": accn, "filingDate": filed, "reportDate": report,
                        "form": form, "primaryDocument": f"{ticker}-8k_{report.replace('-', '')}.htm",
                        "primaryDocDescription": form})
    filings.sort(key=lambda f: (f["filingDate"], f["accessionNumber"]), reverse=True)
    recent = {key: [f[key] for f in filings] for key in
              ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument",
               "primaryDocDescription")}
    return {
        "cik": f"{spec['cik']:010d}",
        "entityType": "operating",
        "sic": spec["sic"],
        "sicDescription": spec["sic_description"],
        "name": spec["name"].upper(),
        "tickers": [spec["ticker"]],
        "exchanges": ["Nasdaq"],
        "fiscalYearEnd": spec["fye"],
        "stateOfIncorporation": "DE",
        "filings": {"recent": recent, "files": []},
    }


def build_market(spec: dict) -> dict:
    m = spec["market"]
    return {"ticker": spec["ticker"], "price": m["price"],
            "shares_outstanding": m["shares_outstanding"], "currency": m["currency"],
            "as_of": m["as_of"]}


# =====================================================================================
# FIXA — Fixture Fruit Inc.  (all money in $mm, EPS in $/share, shares in mm)
# =====================================================================================
def _fixa_income(rev, ebit, da, ni, eps, shares):
    return {REVENUE: rev, "OperatingIncomeLoss": ebit, "DepreciationDepletionAndAmortization": da,
            "NetIncomeLoss": ni, EPS_TAG: eps, SHARES_TAG: shares}


def _fixa_balance(ltd_noncurrent, ltd_current, cp, cash, equity):
    return {"LongTermDebtNoncurrent": ltd_noncurrent, "LongTermDebtCurrent": ltd_current,
            "CommercialPaper": cp, "CashAndCashEquivalentsAtCarryingValue": cash,
            "StockholdersEquity": equity}


FIXA = {
    "cik": 1, "ticker": "FIXA", "name": "Fixture Fruit Inc.", "fye": "0928",
    "sic": "3571", "sic_description": "Electronic Computers",
    # 52/53-week years ending the last Saturday of September (FY2023 = 53 weeks).
    "fy_periods": {
        2018: ("2017-10-01", "2018-09-29"),
        2019: ("2018-09-30", "2019-09-28"),
        2020: ("2019-09-29", "2020-09-26"),
        2021: ("2020-09-27", "2021-09-25"),
        2022: ("2021-09-26", "2022-09-24"),
        2023: ("2022-09-25", "2023-09-30"),
        2024: ("2023-10-01", "2024-09-28"),
        2025: ("2024-09-29", "2025-09-27"),  # no 10-K yet; only 10-Qs
    },
    # `Revenues` for filings up to FY2021, the ASC 606 tag from FY2022 on.
    "revenue_tag": lambda filing_fy: ("Revenues" if filing_fy <= 2021
                                      else "RevenueFromContractWithCustomerExcludingAssessedTax"),
    #                rev      ebit     da      ni      eps    shares
    "income": {
        2018: _fixa_income(255_000, 71_000, 10_900, 59_000, 3.00, 20_000),
        2019: _fixa_income(260_000, 64_000, 12_500, 55_000, 2.97, 18_600),
        2020: _fixa_income(275_000, 66_000, 11_000, 57_000, 3.28, 17_500),
        2021: _fixa_income(365_000, 108_000, 11_000, 94_000, 5.60, 16_800),
        2022: _fixa_income(375_000, 119_000, 11_000, 99_000, 6.10, 16_300),
        2023: _fixa_income(383_000, 114_000, 11_500, 97_500, 6.15, 15_800),  # NI restated (orig 97,000)
        2024: _fixa_income(391_000, 123_000, 11_400, 94_000, 6.10, 15_400),
    },
    #                 LTD noncurrent  LTD current  CP      cash    equity
    "balance": {
        2019: _fixa_balance(92_000, 10_000, 6_000, 49_000, 90_000),
        2020: _fixa_balance(99_000, 9_000, 5_000, 38_000, 65_000),
        2021: _fixa_balance(109_000, 10_000, 6_000, 35_000, 63_000),   # total debt 125,000
        2022: _fixa_balance(99_000, 11_000, 10_000, 24_000, 51_000),   # total debt 120,000
        2023: _fixa_balance(95_000, 10_000, 6_000, 30_000, 62_000),    # total debt 111,000
        2024: _fixa_balance(86_000, 11_000, 3_000, 30_000, 57_000),    # total debt 100,000
    },
    "annual_filings": [
        (2020, "0000000001-20-000010", "2020-10-30", "10-K"),
        (2021, "0000000001-21-000010", "2021-10-29", "10-K"),
        (2022, "0000000001-22-000010", "2022-10-28", "10-K"),
        (2022, "0000000001-22-000012", "2022-12-15", "10-K/A"),   # amendment, same numbers
        (2023, "0000000001-23-000010", "2023-11-03", "10-K"),
        (2024, "0000000001-24-000010", "2024-11-01", "10-K"),     # referenced by filing_FIXA.html
    ],
    # Quarterly income (3-month) and quarter-end balance sheet.
    #   FY2024 9m: rev 285,000 (orig 284,000), ebit 93,000, da 8,500, ni 79,000, eps 5.10, sh 15,500
    #   FY2025 9m: rev 300,000,                ebit 100,000, da 9,000, ni 84,000, eps 5.60, sh 15,100
    "quarters": {
        (2023, 1): {"end": "2022-12-31", "income": _fixa_income(117_000, 36_000, 2_900, 30_000, 1.88, 16_000),
                    "balance": _fixa_balance(99_000, 11_000, 2_000, 20_000, 57_000)},
        (2023, 2): {"end": "2023-04-01", "income": _fixa_income(95_000, 28_000, 2_900, 24_000, 1.52, 15_900),
                    "balance": _fixa_balance(97_000, 11_000, 2_000, 25_000, 62_000)},
        (2023, 3): {"end": "2023-07-01", "income": _fixa_income(82_000, 23_000, 2_900, 20_000, 1.26, 15_800),
                    "balance": _fixa_balance(98_000, 7_000, 4_000, 28_000, 60_000)},
        (2024, 1): {"end": "2023-12-30", "income": _fixa_income(119_000, 40_000, 2_900, 34_000, 2.20, 15_600),
                    "balance": _fixa_balance(95_000, 10_000, 2_000, 40_000, 74_000)},
        (2024, 2): {"end": "2024-03-30", "income": _fixa_income(90_000, 28_000, 2_800, 24_000, 1.55, 15_500),
                    "balance": _fixa_balance(92_000, 12_000, 2_000, 32_000, 74_000)},
        (2024, 3): {"end": "2024-06-29", "income": _fixa_income(76_000, 25_000, 2_800, 21_000, 1.35, 15_400),
                    "balance": _fixa_balance(86_000, 12_000, 3_000, 25_000, 67_000)},
        (2025, 1): {"end": "2024-12-28", "income": _fixa_income(124_000, 42_000, 3_100, 36_000, 2.40, 15_200),
                    "balance": _fixa_balance(84_000, 11_000, 3_000, 30_000, 67_000)},
        (2025, 2): {"end": "2025-03-29", "income": _fixa_income(95_000, 30_000, 3_000, 25_000, 1.65, 15_100),
                    "balance": _fixa_balance(80_000, 12_000, 2_000, 29_000, 67_000)},
        (2025, 3): {"end": "2025-06-28", "income": _fixa_income(81_000, 28_000, 2_900, 23_000, 1.55, 15_000),
                    "balance": _fixa_balance(78_000, 12_000, 2_000, 28_000, 66_000)},  # TTM debt 92,000
    },
    "quarterly_filings": [
        (2024, 1, "0000000001-24-000003", "2024-02-02"),
        (2024, 2, "0000000001-24-000005", "2024-05-03"),
        (2024, 3, "0000000001-24-000007", "2024-08-02"),
        (2025, 1, "0000000001-25-000003", "2025-01-31"),
        (2025, 2, "0000000001-25-000006", "2025-05-02"),
        (2025, 3, "0000000001-25-000009", "2025-08-01"),
    ],
    "other_filings": [("8-K", "0000000001-25-000011", "2025-08-15", "2025-08-14")],
    # Cover-page shares outstanding (dei), as of a date shortly after each period end.
    "dei": {
        "0000000001-20-000010": ("2020-10-16", [17_000]),
        "0000000001-21-000010": ("2021-10-15", [16_400]),
        "0000000001-22-000010": ("2022-10-14", [15_900]),
        "0000000001-22-000012": ("2022-10-14", [15_900]),
        "0000000001-23-000010": ("2023-10-20", [15_550]),
        "0000000001-24-000003": ("2024-01-19", [15_450]),
        "0000000001-24-000005": ("2024-04-19", [15_350]),
        "0000000001-24-000007": ("2024-07-19", [15_250]),
        "0000000001-24-000010": ("2024-10-18", [15_150]),
        "0000000001-25-000003": ("2025-01-17", [15_100]),
        "0000000001-25-000006": ("2025-04-18", [15_050]),
        "0000000001-25-000009": ("2025-07-18", [15_000]),
    },
    # Restatements: (accession, tag, start, end) -> value reported in THAT filing.
    "overrides": {
        # FY2023 net income as originally reported in the FY2023 10-K (restated to 97,500 later).
        ("0000000001-23-000010", "NetIncomeLoss", "2022-09-25", "2023-09-30"): 97_000,
        # Q3 FY2024 revenue as originally reported (3-month 75,000 / 9-month 284,000);
        # the FY2025 Q3 10-Q comparative restates them to 76,000 / 285,000.
        ("0000000001-24-000007", "RevenueFromContractWithCustomerExcludingAssessedTax",
         "2024-03-31", "2024-06-29"): 75_000,
        ("0000000001-24-000007", "RevenueFromContractWithCustomerExcludingAssessedTax",
         "2023-10-01", "2024-06-29"): 284_000,
    },
    "market": {"price": 200.0, "shares_outstanding": 15_000.0, "currency": "USD", "as_of": "2025-08-29"},
}


# =====================================================================================
# FIXB — Fixture Software Corp.
# =====================================================================================
def _fixb_income(rev, ebit, da, ni, eps, shares):
    return {"RevenueFromContractWithCustomerExcludingAssessedTax": rev, "OperatingIncomeLoss": ebit,
            "DepreciationAmortizationAndOther": da, "NetIncomeLoss": ni, EPS_TAG: eps,
            SHARES_TAG: shares}


def _fixb_balance(ltd, ltd_noncurrent, ltd_current, cash, mi, equity, goodwill, intangibles):
    return {"LongTermDebt": ltd, "LongTermDebtNoncurrent": ltd_noncurrent,
            "LongTermDebtCurrent": ltd_current, "CashAndCashEquivalentsAtCarryingValue": cash,
            "MinorityInterest": mi, "StockholdersEquity": equity, "Goodwill": goodwill,
            "IntangibleAssetsNetExcludingGoodwill": intangibles}


FIXB = {
    "cik": 2, "ticker": "FIXB", "name": "Fixture Software Corp.", "fye": "0630",
    "sic": "7372", "sic_description": "Services-Prepackaged Software",
    "fy_periods": {y: (f"{y - 1}-07-01", f"{y}-06-30") for y in range(2019, 2026)},
    "revenue_tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
    #                rev      ebit    da      ni      eps    shares
    "income": {
        2019: _fixb_income(120_000, 40_000, 11_000, 38_000, 4.90, 7_750),
        2020: _fixb_income(140_000, 50_000, 12_000, 43_000, 5.60, 7_680),
        2021: _fixb_income(160_000, 65_000, 11_000, 56_000, 7.40, 7_600),
        2022: _fixb_income(172_000, 72_000, 12_000, 62_000, 8.30, 7_500),
        2023: _fixb_income(184_000, 78_000, 13_000, 66_000, 8.90, 7_450),
        2024: _fixb_income(196_000, 86_000, 17_000, 74_000, 10.00, 7_420),
        2025: _fixb_income(212_000, 96_000, 21_000, 82_000, 11.10, 7_400),
    },
    #                 LTD     LTDnc   LTDc   cash    MI   equity   GW      intang
    "balance": {
        2020: _fixb_balance(60_000, 56_000, 4_000, 14_000, 300, 118_000, 43_000, 7_000),
        2021: _fixb_balance(55_000, 51_000, 4_000, 14_000, 400, 142_000, 50_000, 8_000),
        2022: _fixb_balance(50_000, 47_000, 3_000, 14_000, 400, 166_000, 67_000, 11_000),
        2023: _fixb_balance(47_000, 42_000, 5_000, 34_000, 400, 206_000, 68_000, 9_000),
        2024: _fixb_balance(45_000, 43_000, 2_000, 18_000, 500, 268_000, 119_000, 28_000),
        2025: _fixb_balance(42_000, 39_000, 3_000, 30_000, 500, 340_000, 120_000, 25_000),  # TBV 195,000
    },
    "annual_filings": [
        (2021, "0000000002-21-000020", "2021-07-29", "10-K"),
        (2022, "0000000002-22-000020", "2022-07-28", "10-K"),
        (2023, "0000000002-23-000020", "2023-07-27", "10-K"),
        (2024, "0000000002-24-000020", "2024-07-30", "10-K"),
        (2025, "0000000002-25-000020", "2025-07-30", "10-K"),   # latest filing of all
    ],
    "quarters": {
        (2024, 1): {"end": "2023-09-30", "income": _fixb_income(48_000, 22_000, 4_000, 19_000, 2.55, 7_430),
                    "balance": _fixb_balance(46_000, 44_000, 2_000, 22_000, 400, 220_000, 68_000, 9_000)},
        (2024, 2): {"end": "2023-12-31", "income": _fixb_income(50_000, 24_000, 4_000, 20_000, 2.70, 7_425),
                    "balance": _fixb_balance(46_000, 44_000, 2_000, 17_000, 400, 238_000, 119_000, 28_000)},
        (2024, 3): {"end": "2024-03-31", "income": _fixb_income(51_000, 25_000, 4_500, 21_000, 2.80, 7_420),
                    "balance": _fixb_balance(46_000, 44_000, 2_000, 19_000, 400, 253_000, 119_000, 28_000)},
        (2025, 1): {"end": "2024-09-30", "income": _fixb_income(52_000, 24_000, 5_000, 21_000, 2.85, 7_410),
                    "balance": _fixb_balance(44_000, 41_000, 3_000, 20_000, 500, 290_000, 119_000, 27_000)},
        (2025, 2): {"end": "2024-12-31", "income": _fixb_income(55_000, 26_000, 5_000, 22_000, 2.95, 7_405),
                    "balance": _fixb_balance(43_000, 40_000, 3_000, 17_000, 500, 300_000, 119_000, 26_000)},
        (2025, 3): {"end": "2025-03-31", "income": _fixb_income(56_000, 27_000, 5_500, 23_000, 3.10, 7_400),
                    "balance": _fixb_balance(43_000, 40_000, 3_000, 28_000, 500, 320_000, 119_000, 26_000)},
    },
    # All 10-Qs are BEFORE the FY2025 10-K, so there is nothing to roll forward.
    "quarterly_filings": [
        (2025, 1, "0000000002-24-000030", "2024-10-30"),
        (2025, 2, "0000000002-25-000005", "2025-01-29"),
        (2025, 3, "0000000002-25-000010", "2025-04-30"),
    ],
    "other_filings": [("8-K", "0000000002-25-000022", "2025-08-05", "2025-08-05")],
    # Two share classes (Class A, Class B) reported as two dei facts with identical end/filed.
    "dei": {
        "0000000002-21-000020": ("2021-07-26", [5_500, 2_000]),
        "0000000002-22-000020": ("2022-07-25", [5_480, 2_000]),
        "0000000002-23-000020": ("2023-07-24", [5_450, 2_000]),
        "0000000002-24-000020": ("2024-07-25", [5_430, 2_000]),
        "0000000002-24-000030": ("2024-10-25", [5_420, 2_000]),
        "0000000002-25-000005": ("2025-01-24", [5_410, 2_000]),
        "0000000002-25-000010": ("2025-04-24", [5_405, 2_000]),
        "0000000002-25-000020": ("2025-07-25", [5_400, 2_000]),   # 7,400 mm total
    },
    "overrides": {},
    "market": {"price": 400.0, "shares_outstanding": 7_400.0, "currency": "USD", "as_of": "2025-08-29"},
}


# =====================================================================================
# FIXC — Fixture Bancorp
# =====================================================================================
def _fixc_income(rev, ni, eps, shares):
    # Banks report no operating income and no D&A line -> EBIT/EBITDA are NaN downstream.
    return {"Revenues": rev, "NetIncomeLoss": ni, EPS_TAG: eps, SHARES_TAG: shares}


def _fixc_balance(ltd, stb, cash, equity, goodwill, intangibles, preferred):
    return {"LongTermDebt": ltd, "ShortTermBorrowings": stb,
            "CashAndCashEquivalentsAtCarryingValue": cash, "StockholdersEquity": equity,
            "Goodwill": goodwill, "IntangibleAssetsNetExcludingGoodwill": intangibles,
            "PreferredStockValue": preferred}


FIXC = {
    "cik": 3, "ticker": "FIXC", "name": "Fixture Bancorp", "fye": "1231",
    "sic": "6021", "sic_description": "National Commercial Banks",
    "fy_periods": {y: (f"{y}-01-01", f"{y}-12-31") for y in range(2018, 2026)},
    "revenue_tag": "Revenues",
    #                rev      ni      eps    shares
    "income": {
        2018: _fixc_income(110_000, 32_000, 9.60, 3_350),
        2019: _fixc_income(115_000, 36_000, 10.90, 3_300),
        2020: _fixc_income(120_000, 29_000, 8.90, 3_250),
        2021: _fixc_income(125_000, 40_000, 13.10, 3_050),
        2022: _fixc_income(130_000, 38_000, 12.90, 2_950),
        2023: _fixc_income(145_000, 45_000, 15.50, 2_900),
        2024: _fixc_income(160_000, 50_000, 17.50, 2_850),
    },
    #                 LTD      STB     cash    equity   GW      intang  pref
    "balance": {
        2019: _fixc_balance(290_000, 40_000, 22_000, 260_000, 48_000, 6_000, 27_000),
        2020: _fixc_balance(280_000, 45_000, 25_000, 280_000, 49_000, 6_000, 30_000),
        2021: _fixc_balance(300_000, 50_000, 26_000, 290_000, 50_000, 5_000, 30_000),
        2022: _fixc_balance(295_000, 45_000, 27_000, 292_000, 51_000, 4_000, 28_000),
        2023: _fixc_balance(390_000, 40_000, 29_000, 328_000, 52_000, 3_000, 27_000),
        2024: _fixc_balance(400_000, 45_000, 25_000, 345_000, 52_000, 3_000, 25_000),  # debt 445,000; TBV 290,000
    },
    "annual_filings": [
        (2020, "0000000003-21-000005", "2021-02-25", "10-K"),
        (2021, "0000000003-22-000005", "2022-02-24", "10-K"),
        (2022, "0000000003-23-000005", "2023-02-23", "10-K"),
        (2023, "0000000003-24-000005", "2024-02-22", "10-K"),
        (2024, "0000000003-25-000005", "2025-02-20", "10-K"),
    ],
    "quarters": {
        (2024, 1): {"end": "2024-03-31", "income": _fixc_income(40_000, 12_000, 4.20, 2_870),
                    "balance": _fixc_balance(395_000, 42_000, 27_000, 332_000, 52_000, 3_000, 27_000)},
        (2025, 1): {"end": "2025-03-31", "income": _fixc_income(45_000, 14_000, 5.00, 2_800),
                    "balance": _fixc_balance(410_000, 40_000, 28_000, 350_000, 52_000, 3_000, 25_000)},
    },
    "quarterly_filings": [(2025, 1, "0000000003-25-000020", "2025-05-01")],
    "other_filings": [("8-K", "0000000003-25-000025", "2025-07-15", "2025-07-15")],
    "dei": {
        "0000000003-21-000005": ("2021-02-12", [3_200]),
        "0000000003-22-000005": ("2022-02-11", [3_000]),
        "0000000003-23-000005": ("2023-02-10", [2_930]),
        "0000000003-24-000005": ("2024-02-09", [2_880]),
        "0000000003-25-000005": ("2025-02-07", [2_830]),
        "0000000003-25-000020": ("2025-04-25", [2_800]),
    },
    "overrides": {},
    "market": {"price": 250.0, "shares_outstanding": 2_800.0, "currency": "USD", "as_of": "2025-08-29"},
}

COMPANIES = [FIXA, FIXB, FIXC]


def write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    for spec in COMPANIES:
        t = spec["ticker"]
        facts = build_facts(spec)
        write_json(HERE / f"companyfacts_{t}.json", facts)
        write_json(HERE / f"submissions_{t}.json", build_submissions(spec))
        write_json(HERE / f"market_{t}.json", build_market(spec))
        n_facts = sum(len(fl) for ns in facts["facts"].values() for e in ns.values()
                      for fl in e["units"].values())
        print(f"wrote fixtures for {t} ({spec['name']}): {n_facts} facts")


if __name__ == "__main__":
    main()
