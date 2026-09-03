"""
Tests for compsai.edgar against the synthetic EDGAR fixtures (tests/fixtures/make_fixtures.py).

Every expected number below is hand-derivable from the round figures in make_fixtures.py;
the arithmetic is written out in comments so a reviewer can check it without running code.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from compsai import MILLION, edgar
from compsai.edgar import (
    COLUMNS,
    extract_financials,
    get_cik,
    get_company_facts,
    get_company_name,
    get_submissions,
)


@pytest.fixture(scope="module")
def fixa() -> pd.DataFrame:
    return extract_financials(get_company_facts("1", offline=True, ticker="FIXA"))


@pytest.fixture(scope="module")
def fixb() -> pd.DataFrame:
    return extract_financials(get_company_facts("2", offline=True, ticker="FIXB"))


@pytest.fixture(scope="module")
def fixc() -> pd.DataFrame:
    return extract_financials(get_company_facts("3", offline=True, ticker="FIXC"))


def fy_row(df: pd.DataFrame, year: int) -> pd.Series:
    rows = df[(df["period_type"] == "FY") & (df["fiscal_year"] == year)]
    assert len(rows) == 1, f"expected one FY{year} row"
    return rows.iloc[0]


def ttm_row(df: pd.DataFrame) -> pd.Series:
    rows = df[df["period_type"] == "TTM"]
    assert len(rows) == 1
    return rows.iloc[0]


# ------------------------------------------------------------------------------------------
# Offline lookups
# ------------------------------------------------------------------------------------------

def test_get_cik_offline_is_zero_padded_10_digits():
    assert get_cik("FIXA", offline=True) == "0000000001"
    assert get_cik("fixb") == "0000000002"  # env COMPSAI_OFFLINE=1 from conftest, lowercase ok
    assert get_cik("FIXC", offline=True) == "0000000003"


def test_get_company_name_offline():
    assert get_company_name("FIXA", offline=True) == "Fixture Fruit Inc."
    assert get_company_name("FIXB", offline=True) == "Fixture Software Corp."
    assert get_company_name("FIXC", offline=True) == "Fixture Bancorp"


def test_get_company_facts_offline_by_ticker_and_by_cik():
    by_ticker = get_company_facts("0000000001", offline=True, ticker="FIXA")
    by_cik = get_company_facts("1", offline=True)  # no ticker: scans fixtures for the CIK
    assert by_ticker["entityName"] == "Fixture Fruit Inc."
    assert by_cik["cik"] == 1
    assert "us-gaap" in by_ticker["facts"] and "dei" in by_ticker["facts"]


def test_get_submissions_offline_has_required_latest_10k():
    subs = get_submissions("1", offline=True, ticker="FIXA")
    recent = subs["filings"]["recent"]
    idx = recent["form"].index("10-K")  # newest first, so the first 10-K is the latest
    assert recent["accessionNumber"][idx] == "0000000001-24-000010"
    assert recent["filingDate"][idx] == "2024-11-01"
    assert recent["reportDate"][idx] == "2024-09-28"
    assert recent["primaryDocument"][idx] == "fixa-20240928.htm"
    # a 10-K/A, 10-Qs and an 8-K are present, so a "latest 10-K" lookup must filter
    assert {"10-K/A", "10-Q", "8-K"} <= set(recent["form"])


def test_missing_fixture_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        get_company_facts("999", offline=True, ticker="NOPE")


# ------------------------------------------------------------------------------------------
# Table shape
# ------------------------------------------------------------------------------------------

def test_columns_in_contract_order_and_dtypes(fixa):
    assert list(fixa.columns) == COLUMNS
    assert COLUMNS[:3] == ["fiscal_year", "period_type", "period_end"]
    assert COLUMNS[-1] == "currency"
    assert fixa["fiscal_year"].dtype.kind == "i"
    for column in ("revenue", "ebit", "da", "ebitda", "net_income", "eps_diluted", "total_debt",
                   "cash", "minority_interest", "preferred", "diluted_shares", "total_equity",
                   "goodwill", "intangibles", "tangible_book_value"):
        assert fixa[column].dtype == "float64", column
    for column in ("period_type", "period_end", "currency"):
        assert pd.api.types.is_string_dtype(fixa[column]), column


def test_rows_oldest_first_then_single_ttm_last(fixa):
    assert list(fixa["period_type"]) == ["FY", "FY", "FY", "FY", "TTM"]
    assert list(fixa["fiscal_year"]) == [2021, 2022, 2023, 2024, 2024]  # TTM carries latest FY label
    assert list(fixa["period_end"]) == ["2021-09-25", "2022-09-24", "2023-09-30", "2024-09-28", "2025-06-28"]


def test_n_years_controls_number_of_fy_rows():
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    df = extract_financials(facts, n_years=2)
    assert list(df["period_type"]) == ["FY", "FY", "TTM"]
    assert list(df["fiscal_year"]) == [2023, 2024, 2024]


def test_currency_column_is_usd(fixa, fixb, fixc):
    for df in (fixa, fixb, fixc):
        assert set(df["currency"]) == {"USD"}


def test_no_nan_in_revenue_ebitda_net_income_for_last_three_fy(fixa, fixb):
    for df in (fixa, fixb):
        last3 = df[df["period_type"] == "FY"].tail(3)
        assert not last3[["revenue", "ebitda", "net_income"]].isna().any().any()


def test_no_revenue_at_all_raises_value_error():
    with pytest.raises(ValueError):
        extract_financials({"cik": 9, "entityName": "Empty Co", "facts": {}})


# ------------------------------------------------------------------------------------------
# Tag fallback, restatements, labels
# ------------------------------------------------------------------------------------------

def test_per_period_revenue_tag_fallback(fixa):
    """FY2021 exists only as `Revenues` in its own 10-K; later years use the ASC 606 tag."""
    used = fixa.attrs["tags_used"]["revenue"]
    assert used["2021-09-25"] == "us-gaap:Revenues"
    assert used["2022-09-24"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert used["2024-09-28"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert used["2025-06-28"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert list(fixa["revenue"]) == [365_000.0, 375_000.0, 383_000.0, 391_000.0, 406_000.0]


def test_latest_filed_restatement_wins(fixa):
    """FY2023 net income: 97,000 in the FY2023 10-K, restated to 97,500 in the FY2024 10-K."""
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    ni = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    reported = {f["filed"]: f["val"] for f in ni if f["end"] == "2023-09-30" and f["form"] == "10-K"}
    assert reported["2023-11-03"] == 97_000 * MILLION  # original
    assert reported["2024-11-01"] == 97_500 * MILLION  # restated comparative
    assert fy_row(fixa, 2023)["net_income"] == 97_500.0


def test_fiscal_year_label_comes_from_earliest_filing(fixa):
    """Period end 2021-09-25 appears in the FY2021, FY2022 and FY2023 10-Ks (fy 2021/2022/2023)."""
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    ni = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    fys = sorted({f["fy"] for f in ni if f["end"] == "2021-09-25"})
    assert fys == [2021, 2022, 2023]
    row = fixa[fixa["period_end"] == "2021-09-25"].iloc[0]
    assert row["fiscal_year"] == 2021


def test_10k_amendment_does_not_duplicate_rows(fixa):
    assert (fixa["period_end"] == "2022-09-24").sum() == 1


def test_53_week_year_matched_within_tolerance(fixa):
    """FY2023 is a 53-week year (2022-09-25 .. 2023-09-30, 370 days) and still counts as annual."""
    row = fy_row(fixa, 2023)
    assert row["period_end"] == "2023-09-30"
    assert row["revenue"] == 383_000.0


def test_fixb_da_via_microsoft_tag_and_ebitda_is_ebit_plus_da(fixb):
    assert fixb.attrs["tags_used"]["da"]["2025-06-30"] == "us-gaap:DepreciationAmortizationAndOther"
    row = fy_row(fixb, 2025)
    assert row["ebit"] == 96_000.0 and row["da"] == 21_000.0
    assert row["ebitda"] == 96_000.0 + 21_000.0  # EBITDA = EBIT + D&A = 117,000


# ------------------------------------------------------------------------------------------
# Total debt convention (DESIGN.md _total_debt)
# ------------------------------------------------------------------------------------------

def test_total_debt_fixa_noncurrent_plus_current_plus_commercial_paper(fixa):
    # FY2024: LongTermDebtNoncurrent 86,000 + LongTermDebtCurrent 11,000 + CommercialPaper 3,000
    assert fy_row(fixa, 2024)["total_debt"] == 100_000.0
    assert fy_row(fixa, 2021)["total_debt"] == 109_000.0 + 10_000.0 + 6_000.0  # 125,000
    assert fixa.attrs["tags_used"]["total_debt"]["2024-09-28"] == (
        "us-gaap:LongTermDebtNoncurrent+us-gaap:LongTermDebtCurrent+us-gaap:CommercialPaper")


def test_total_debt_fixb_long_term_debt_includes_current_portion(fixb):
    """LongTermDebt (42,000) already includes the current portion; LongTermDebtNoncurrent
    (39,000) and LongTermDebtCurrent (3,000) are also tagged and must NOT be added on top."""
    assert fy_row(fixb, 2025)["total_debt"] == 42_000.0
    assert fixb.attrs["tags_used"]["total_debt"]["2025-06-30"] == "us-gaap:LongTermDebt"


def test_total_debt_fixc_long_term_debt_plus_short_term_borrowings(fixc):
    # FY2024: LongTermDebt 400,000 + ShortTermBorrowings 45,000
    assert fy_row(fixc, 2024)["total_debt"] == 445_000.0
    assert fixc.attrs["tags_used"]["total_debt"]["2024-12-31"] == "us-gaap:LongTermDebt+us-gaap:ShortTermBorrowings"


def test_total_debt_helper_debt_current_not_double_counted():
    """DebtCurrent already contains short-term borrowings, so CP must not be added again."""
    values = {"us-gaap:LongTermDebtNoncurrent": 80.0, "us-gaap:DebtCurrent": 20.0,
              "us-gaap:CommercialPaper": 5.0}
    total, label = edgar._total_debt(values.get)
    assert total == 100.0
    assert label == "us-gaap:LongTermDebtNoncurrent+us-gaap:DebtCurrent"
    # with LongTermDebtCurrent instead, short-term items ARE added
    values = {"us-gaap:LongTermDebtNoncurrent": 80.0, "us-gaap:LongTermDebtCurrent": 20.0,
              "us-gaap:CommercialPaper": 5.0}
    assert edgar._total_debt(values.get)[0] == 105.0
    # IFRS fallbacks
    assert edgar._total_debt({"ifrs-full:Borrowings": 30.0}.get) == (30.0, "ifrs-full:Borrowings")
    assert edgar._total_debt({}.get) == (None, None)


# ------------------------------------------------------------------------------------------
# TTM roll-forward
# ------------------------------------------------------------------------------------------

def test_ttm_roll_forward_fixa(fixa):
    """TTM = FY2024 + YTD 9m FY2025 - YTD 9m FY2024 (roll-forward construction)."""
    ttm = ttm_row(fixa)
    assert ttm["fiscal_year"] == 2024 and ttm["period_end"] == "2025-06-28"
    # revenue        = 391,000 + 300,000 - 285,000 = 406,000
    assert ttm["revenue"] == pytest.approx(406_000.0)
    # ebit           = 123,000 + 100,000 -  93,000 = 130,000
    assert ttm["ebit"] == pytest.approx(130_000.0)
    # da             =  11,400 +   9,000 -   8,500 =  11,900
    assert ttm["da"] == pytest.approx(11_900.0)
    # ebitda         = 130,000 + 11,900 = 141,900
    assert ttm["ebitda"] == pytest.approx(141_900.0)
    # net income     =  94,000 +  84,000 -  79,000 =  99,000
    assert ttm["net_income"] == pytest.approx(99_000.0)
    # diluted EPS    =    6.10 +    5.60 -    5.10 =    6.60  (ignores share-count drift)
    assert ttm["eps_diluted"] == pytest.approx(6.60)
    # diluted shares = the 9m FY2025 weighted average itself (not a flow) = 15,100
    assert ttm["diluted_shares"] == pytest.approx(15_100.0)
    assert fixa.attrs["warnings"] == []


def test_ttm_uses_restated_prior_year_ytd(fixa):
    """9m FY2024 revenue was 284,000 in the original Q3 FY2024 10-Q and 285,000 in the
    FY2025 Q3 10-Q comparative; latest filed wins, so TTM is 406,000 not 407,000."""
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    rev = facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    ytd_prior = {f["filed"]: f["val"] for f in rev if f.get("start") == "2023-10-01" and f["end"] == "2024-06-29"}
    assert ytd_prior == {"2024-08-02": 284_000 * MILLION, "2025-08-01": 285_000 * MILLION}
    assert ttm_row(fixa)["revenue"] == pytest.approx(406_000.0)


def test_ttm_balance_sheet_is_latest_quarter_end(fixa):
    ttm = ttm_row(fixa)
    # Q3 FY2025 balance sheet (2025-06-28): debt 78,000 + 12,000 + 2,000 = 92,000
    assert ttm["total_debt"] == 92_000.0
    assert ttm["cash"] == 28_000.0
    assert ttm["total_equity"] == 66_000.0
    assert ttm["tangible_book_value"] == 66_000.0  # no goodwill / intangibles


def test_ttm_equals_fy_when_no_10q_after_latest_10k(fixb):
    ttm, fy = ttm_row(fixb), fy_row(fixb, 2025)
    assert "no 10-Q after latest 10-K; TTM = FY" in fixb.attrs["warnings"]
    assert ttm["period_end"] == "2025-06-30" and ttm["fiscal_year"] == 2025
    for column in ("revenue", "ebit", "da", "ebitda", "net_income", "eps_diluted", "diluted_shares",
                   "total_debt", "cash", "minority_interest", "total_equity", "goodwill",
                   "intangibles", "tangible_book_value"):
        assert ttm[column] == fy[column], column
    assert ttm["revenue"] == 212_000.0


def test_fixc_q1_ttm(fixc):
    ttm = ttm_row(fixc)
    assert ttm["period_end"] == "2025-03-31" and ttm["fiscal_year"] == 2024
    # revenue    = FY2024 160,000 + Q1 2025 45,000 - Q1 2024 40,000 = 165,000
    assert ttm["revenue"] == pytest.approx(165_000.0)
    # net income =  50,000 + 14,000 - 12,000 = 52,000
    assert ttm["net_income"] == pytest.approx(52_000.0)
    # EPS        =   17.50 +  5.00 -  4.20 = 18.30
    assert ttm["eps_diluted"] == pytest.approx(18.30)
    assert ttm["diluted_shares"] == 2_800.0
    # banks: no operating income / D&A tags -> EBIT, D&A and EBITDA are NaN
    assert math.isnan(ttm["ebit"]) and math.isnan(ttm["da"]) and math.isnan(ttm["ebitda"])
    # balance sheet at 2025-03-31: LongTermDebt 410,000 + ShortTermBorrowings 40,000
    assert ttm["total_debt"] == 450_000.0
    assert ttm["cash"] == 28_000.0
    assert ttm["preferred"] == 25_000.0
    # TBV = 350,000 - 52,000 - 3,000 = 295,000
    assert ttm["tangible_book_value"] == 295_000.0


def test_ttm_concept_falls_back_to_fy_with_warning_when_ytd_missing():
    """Drop FIXA's quarterly D&A facts: TTM D&A must equal FY D&A and the warning names it."""
    facts = json.loads(json.dumps(get_company_facts("1", offline=True, ticker="FIXA")))
    da = facts["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"]["units"]["USD"]
    facts["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"]["units"]["USD"] = [
        f for f in da if f["form"] != "10-Q"]
    df = extract_financials(facts)
    assert ttm_row(df)["da"] == fy_row(df, 2024)["da"] == 11_400.0
    assert ttm_row(df)["revenue"] == pytest.approx(406_000.0)  # other concepts still rolled
    assert any(w.startswith("da:") and "TTM = FY" in w for w in df.attrs["warnings"])


# ------------------------------------------------------------------------------------------
# Defaults, derived columns, units
# ------------------------------------------------------------------------------------------

def test_minority_preferred_goodwill_intangibles_default_to_zero(fixa, fixc):
    for column in ("minority_interest", "preferred", "goodwill", "intangibles"):
        assert (fixa[column] == 0.0).all(), column
    assert (fixc["minority_interest"] == 0.0).all()
    assert fy_row(fixc, 2024)["preferred"] == 25_000.0
    assert fy_row(fixc, 2024)["goodwill"] == 52_000.0


def test_bank_tangible_book_value(fixc, fixb):
    # FIXC FY2024: equity 345,000 - goodwill 52,000 - intangibles 3,000 = 290,000
    assert fy_row(fixc, 2024)["tangible_book_value"] == 290_000.0
    # FIXB FY2025: 340,000 - 120,000 - 25,000 = 195,000
    assert fy_row(fixb, 2025)["tangible_book_value"] == 195_000.0
    assert fy_row(fixb, 2025)["minority_interest"] == 500.0


def test_units_are_millions_except_per_share(fixa):
    facts = get_company_facts("1", offline=True, ticker="FIXA")
    raw = facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    fy2024 = [f for f in raw if f["end"] == "2024-09-28" and f["form"] == "10-K"][-1]["val"]
    assert fy2024 == 391_000_000_000
    assert fy_row(fixa, 2024)["revenue"] == fy2024 / MILLION == 391_000.0
    raw_shares = facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"]["units"]["shares"]
    assert [f for f in raw_shares if f["end"] == "2024-09-28"][-1]["val"] == 15_400_000_000
    assert fy_row(fixa, 2024)["diluted_shares"] == 15_400.0
    assert fy_row(fixa, 2024)["eps_diluted"] == 6.10  # per-share values are left raw


def test_tags_used_shape(fixa):
    used = fixa.attrs["tags_used"]
    assert isinstance(used, dict)
    for concept, per_period in used.items():
        assert isinstance(concept, str)
        for period_end, tag in per_period.items():
            assert len(period_end) == 10 and ":" in tag


# ------------------------------------------------------------------------------------------
# Network plumbing: cache, User-Agent, rate limiter (all with a fake _get_json / requests)
# ------------------------------------------------------------------------------------------

TICKER_TABLE = {"0": {"cik_str": 1, "ticker": "FIXA", "title": "Fixture Fruit Inc."},
                "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


@pytest.fixture
def online(monkeypatch):
    """Turn off the conftest offline switch for tests that exercise the network layer."""
    monkeypatch.delenv("COMPSAI_OFFLINE", raising=False)


def test_cache_write_read_ttl_and_refresh(online, tmp_cache_dir, monkeypatch):
    calls: list[str] = []

    def fake_get_json(url):
        calls.append(url)
        return TICKER_TABLE

    monkeypatch.setattr(edgar, "_get_json", fake_get_json)

    assert get_cik("AAPL") == "0000320193"           # 1st call: network + write cache
    assert get_company_name("aapl") == "Apple Inc."  # 2nd call: served from cache
    assert calls == [edgar.TICKERS_URL]
    cache_file = tmp_cache_dir / "company_tickers.json"
    payload = json.loads(cache_file.read_text())
    assert set(payload) == {"fetched_at", "data"} and payload["data"] == TICKER_TABLE

    # Expire the file (TTL for company_tickers.json is 7 days) -> refetched.
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    cache_file.write_text(json.dumps(payload))
    assert get_cik("FIXA") == "0000000001"
    assert len(calls) == 2

    # refresh=True bypasses a fresh cache.
    edgar._ticker_table(refresh=True)
    assert len(calls) == 3


def test_company_facts_cache_file_name(online, tmp_cache_dir, monkeypatch):
    fixture = get_company_facts("1", offline=True, ticker="FIXA")
    monkeypatch.setattr(edgar, "_get_json", lambda url: fixture)
    facts = get_company_facts("1")
    assert facts["entityName"] == "Fixture Fruit Inc."
    assert (tmp_cache_dir / "companyfacts_CIK0000000001.json").exists()
    get_submissions("1")
    assert (tmp_cache_dir / "submissions_CIK0000000001.json").exists()


def test_missing_user_agent_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(edgar, "load_dotenv", lambda *a, **k: False)  # ignore a developer's .env
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        edgar._get_json(edgar.TICKERS_URL)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_get_json_sets_headers_and_raises_on_non_200(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return _FakeResponse(403)

    monkeypatch.setattr(edgar.requests, "get", fake_get)
    monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="403"):
        edgar._get_json("https://data.sec.gov/x.json")
    assert seen["headers"]["User-Agent"] == "CompsAI test@example.com"
    assert seen["headers"]["Accept-Encoding"] == "gzip, deflate"


def test_rate_limiter_spaces_requests(monkeypatch):
    clock = iter([100.00, 100.02, 100.40])  # 2nd request 20ms after the 1st, 3rd well after
    sleeps: list[float] = []
    monkeypatch.setattr(edgar.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(edgar.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(edgar.requests, "get", lambda url, headers=None, timeout=None: _FakeResponse(200, {"ok": 1}))
    monkeypatch.setattr(edgar, "_last_request_at", None)

    edgar._get_json("https://data.sec.gov/a.json")
    edgar._get_json("https://data.sec.gov/b.json")
    edgar._get_json("https://data.sec.gov/c.json")
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(edgar.MIN_REQUEST_INTERVAL - 0.02)  # 0.09 s
    assert edgar.MIN_REQUEST_INTERVAL >= 0.1  # <= 10 requests/second (SEC cap)


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------

def test_cli_offline_prints_tables(capsys):
    assert edgar.main(["FIXA", "FIXB", "FIXC", "--offline"]) == 0
    out = capsys.readouterr().out
    for header in ("=== FIXA — Fixture Fruit Inc.", "=== FIXB — Fixture Software Corp.", "=== FIXC — Fixture Bancorp"):
        assert header in out
    assert "406,000.00" in out  # FIXA TTM revenue
    assert "..." not in out     # no pandas truncation


def test_cli_debug_prints_tags_and_warnings(capsys):
    assert edgar.main(["FIXB", "--offline", "--debug"]) == 0
    out = capsys.readouterr().out
    assert "tags_used:" in out and "us-gaap:DepreciationAmortizationAndOther" in out
    assert "no 10-Q after latest 10-K; TTM = FY" in out


def test_cli_save_fixture_writes_raw_json(tmp_path, capsys):
    assert edgar.main(["FIXC", "--offline", "--save-fixture", str(tmp_path)]) == 0
    facts = json.loads((tmp_path / "companyfacts_FIXC.json").read_text())
    subs = json.loads((tmp_path / "submissions_FIXC.json").read_text())
    assert facts["entityName"] == "Fixture Bancorp" and subs["cik"] == "0000000003"


def test_cli_unknown_ticker_returns_nonzero(capsys):
    assert edgar.main(["NOPE", "--offline"]) == 1
