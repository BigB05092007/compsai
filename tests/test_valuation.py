"""
Tests for compsai/valuation.py (Module 2).

Every input frame is built inline with ``make_financials`` so these tests do not depend
on the EDGAR fixture files (written by another module). The first test is a full
hand-calculated case: the arithmetic is written out in comments exactly as an analyst
would do it on paper, and the code must match to two decimals.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from compsai.valuation import (
    IMPLIED_COLUMNS,
    MULTIPLE_BOUNDS,
    MULTIPLES_BY_SECTOR,
    MULTIPLES_INDEX,
    BANK_NOTE,
    compute_multiples,
    enterprise_value,
    exclusion_note,
    implied_valuation,
    is_meaningful,
    mask_non_meaningful,
    summary_stats,
)

# Column order of extract_financials output (docs/DESIGN.md section 2).
FIN_COLUMNS = [
    "fiscal_year", "period_type", "period_end", "revenue", "ebit", "da", "ebitda",
    "net_income", "eps_diluted", "total_debt", "cash", "minority_interest", "preferred",
    "diluted_shares", "total_equity", "goodwill", "intangibles", "tangible_book_value",
    "currency",
]

# Balance-sheet / income-statement defaults for every row, all in $mm (EPS in USD).
ROW_DEFAULTS = dict(
    ebit=100_000.0, da=30_000.0, net_income=100_000.0, eps_diluted=6.50,
    total_debt=100_000.0, cash=30_000.0, minority_interest=0.0, preferred=0.0,
    diluted_shares=15_000.0, total_equity=500_000.0, goodwill=50_000.0,
    intangibles=20_000.0, currency="USD",
)


def make_financials(
    fy_revenues: list[float],
    ttm: dict | None = None,
    first_year: int = 2021,
    include_ttm: bool = True,
    **overrides,
) -> pd.DataFrame:
    """
    Build an extract_financials-shaped frame: FY rows oldest-first, then one TTM row.

    ``overrides`` apply to every row; ``ttm`` applies to the TTM row only. ``ebitda`` and
    ``tangible_book_value`` are derived unless explicitly given, mirroring edgar.py.
    """
    rows = []
    for i, revenue in enumerate(fy_revenues):
        year = first_year + i
        row = dict(ROW_DEFAULTS, **overrides)
        row.update(fiscal_year=year, period_type="FY", period_end=f"{year}-12-31", revenue=revenue)
        rows.append(row)
    if include_ttm:
        last_year = first_year + len(fy_revenues) - 1
        row = dict(ROW_DEFAULTS, **overrides)
        row.update(fiscal_year=last_year, period_type="TTM", period_end=f"{last_year + 1}-06-30",
                   revenue=fy_revenues[-1])
        row.update(ttm or {})
        rows.append(row)

    for row in rows:
        # EBITDA = EBIT + D&A ; TBV = total equity - goodwill - intangibles (DESIGN.md section 2)
        row.setdefault("ebitda", row["ebit"] + row["da"])
        row.setdefault("tangible_book_value", row["total_equity"] - row["goodwill"] - row["intangibles"])
    return pd.DataFrame(rows, columns=FIN_COLUMNS)


def make_market(price: float = 200.0, shares: float = 15_000.0, **extra) -> dict:
    """get_market_data-shaped dict; market_cap = price x shares (shares in mm -> $mm)."""
    market = dict(ticker="TEST", price=price, shares_outstanding=shares,
                  market_cap=price * shares, currency="USD", source="fixture", as_of="2025-06-30")
    market.update(extra)
    return market


# ---------------------------------------------------------------------------
# (1) Hand-calculated case
# ---------------------------------------------------------------------------


def test_hand_calculated_company():
    """
    Worked on paper, $ in millions except per share:

      Price                       200.00
      Shares outstanding (mm)     15,000
      Market cap = 200 x 15,000 = 3,000,000

      Total debt                  100,000
      Cash                         30,000
      Minority interest                 0
      Preferred equity                  0
      EV = 3,000,000 + 100,000 + 0 + 0 - 30,000 = 3,070,000

      Revenue TTM                 400,000  -> EV/Revenue = 3,070,000 / 400,000 = 7.675x  (7.68x)
      EBITDA TTM                  130,000  -> EV/EBITDA  = 3,070,000 / 130,000 = 23.615x (23.62x)
      Diluted EPS TTM                6.50  -> P/E        = 200 / 6.50          = 30.769x (30.77x)
      Net income TTM              100,000  -> net margin = 100,000 / 400,000    = 25.0%
                                              EBITDA margin = 130,000 / 400,000 = 32.5%
      TBV = 500,000 - 50,000 - 20,000 = 430,000 -> P/TBV = 3,000,000 / 430,000 = 6.977x (6.98x)

      FY revenues 2021..2024: 300,000 -> 340,000 -> 370,000 -> 391,000
        1y growth = 391,000 / 370,000 - 1          = 5.68%
        3y CAGR   = (391,000 / 300,000)^(1/3) - 1  = 9.23%
    """
    fin = make_financials(
        [300_000, 340_000, 370_000, 391_000],
        ttm=dict(revenue=400_000.0, ebit=100_000.0, da=30_000.0),  # EBITDA = 130,000
    )
    m = compute_multiples(fin, make_market(price=200.0, shares=15_000.0))

    assert list(m.index) == MULTIPLES_INDEX
    assert m["market_cap"] == pytest.approx(3_000_000.0)
    assert m["ev"] == pytest.approx(3_070_000.0)
    assert m["ev_ebitda_ttm"] == pytest.approx(23.62, abs=0.005)
    assert m["ev_revenue_ttm"] == pytest.approx(7.68, abs=0.005)
    assert m["pe_ttm"] == pytest.approx(30.77, abs=0.005)
    assert m["ebitda_margin"] == pytest.approx(0.325, abs=0.0005)
    assert m["net_margin"] == pytest.approx(0.25, abs=0.0005)
    assert m["revenue_growth_1y"] == pytest.approx(0.0568, abs=0.00005)
    assert m["revenue_growth_3y_cagr"] == pytest.approx(0.0923, abs=0.00005)
    assert m["p_tbv"] == pytest.approx(6.98, abs=0.005)
    assert m["price"] == pytest.approx(200.0)
    assert m["note"] == ""  # a clean company carries no caveats


def test_enterprise_value_by_hand():
    # EV = 3,000,000 + 100,000 + 5,000 + 2,000 - 30,000 = 3,077,000
    assert enterprise_value(3_000_000, 100_000, 30_000, 5_000, 2_000) == pytest.approx(3_077_000.0)
    # Defaults: minority and preferred are zero.
    assert enterprise_value(3_000_000, 100_000, 30_000) == pytest.approx(3_070_000.0)


# ---------------------------------------------------------------------------
# (5) NaN handling in enterprise_value
# ---------------------------------------------------------------------------


def test_enterprise_value_nan_minority_and_preferred_treated_as_zero():
    assert enterprise_value(3_000_000, 100_000, 30_000, np.nan, np.nan) == pytest.approx(3_070_000.0)
    assert enterprise_value(3_000_000, 100_000, 30_000, None, None) == pytest.approx(3_070_000.0)


def test_enterprise_value_nan_core_inputs_give_nan():
    assert math.isnan(enterprise_value(np.nan, 100_000, 30_000))
    assert math.isnan(enterprise_value(3_000_000, np.nan, 30_000))
    assert math.isnan(enterprise_value(3_000_000, 100_000, np.nan))


def test_compute_multiples_nan_cash_gives_nan_ev_and_ev_multiples():
    fin = make_financials([300_000, 340_000, 370_000, 391_000], ttm=dict(cash=np.nan))
    m = compute_multiples(fin, make_market())
    assert math.isnan(m["ev"])
    assert math.isnan(m["ev_revenue_ttm"])
    assert math.isnan(m["ev_ebitda_ttm"])
    # P/E does not need EV, so it is still computed.
    assert m["pe_ttm"] == pytest.approx(200 / 6.5)
    assert "EV/EBITDA n/a" in m["note"]


def test_compute_multiples_nan_minority_preferred_treated_as_zero():
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ttm=dict(minority_interest=np.nan, preferred=np.nan))
    m = compute_multiples(fin, make_market())
    assert m["ev"] == pytest.approx(3_070_000.0)


# ---------------------------------------------------------------------------
# (6) Growth uses FY rows; CAGR needs four of them
# ---------------------------------------------------------------------------


def test_growth_uses_fiscal_years_not_ttm():
    # TTM revenue is deliberately huge; growth must ignore it.
    fin = make_financials([300_000, 340_000, 370_000, 391_000], ttm=dict(revenue=9_999_999.0))
    m = compute_multiples(fin, make_market())
    assert m["revenue_growth_1y"] == pytest.approx(391 / 370 - 1)
    assert m["revenue_growth_3y_cagr"] == pytest.approx((391 / 300) ** (1 / 3) - 1)


def test_cagr_nan_with_fewer_than_four_fy_rows():
    fin = make_financials([340_000, 370_000, 391_000])
    m = compute_multiples(fin, make_market())
    assert m["revenue_growth_1y"] == pytest.approx(391 / 370 - 1)
    assert math.isnan(m["revenue_growth_3y_cagr"])


def test_growth_nan_with_single_fy_row():
    fin = make_financials([391_000])
    m = compute_multiples(fin, make_market())
    assert math.isnan(m["revenue_growth_1y"])
    assert math.isnan(m["revenue_growth_3y_cagr"])


# ---------------------------------------------------------------------------
# (7) No TTM row -> latest FY with a note
# ---------------------------------------------------------------------------


def test_no_ttm_row_falls_back_to_latest_fy_with_note():
    fin = make_financials([300_000, 340_000, 370_000, 391_000], include_ttm=False)
    m = compute_multiples(fin, make_market())
    # Latest FY revenue is 391,000 -> EV/Revenue = 3,070,000 / 391,000 = 7.85x
    assert m["ev_revenue_ttm"] == pytest.approx(3_070_000 / 391_000)
    assert "no TTM row" in m["note"]
    assert "FY2024" in m["note"]


# ---------------------------------------------------------------------------
# (8) EPS NaN -> P/E = market cap / net income
# ---------------------------------------------------------------------------


def test_pe_falls_back_to_market_cap_over_net_income_when_eps_missing():
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ttm=dict(eps_diluted=np.nan, net_income=120_000.0))
    m = compute_multiples(fin, make_market())
    # P/E = 3,000,000 / 120,000 = 25.0x
    assert m["pe_ttm"] == pytest.approx(25.0)
    assert "EPS unavailable" in m["note"]


# ---------------------------------------------------------------------------
# (4) Bank branch
# ---------------------------------------------------------------------------


def test_bank_branch_blanks_ev_multiples_and_computes_p_tbv():
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ebit=np.nan, da=np.nan, ebitda=np.nan)  # banks report no EBITDA
    m = compute_multiples(fin, make_market(), sector_type="bank")
    assert list(m.index) == MULTIPLES_INDEX
    for name in ("ev", "ev_revenue_ttm", "ev_ebitda_ttm", "ebitda_margin"):
        assert math.isnan(m[name]), name
    # P/TBV = 3,000,000 / 430,000 = 6.98x ; P/E = 200 / 6.50 = 30.77x
    assert m["p_tbv"] == pytest.approx(6.98, abs=0.005)
    assert m["pe_ttm"] == pytest.approx(30.77, abs=0.005)
    assert m["net_margin"] == pytest.approx(100_000 / 391_000)
    assert BANK_NOTE in m["note"]
    assert "not meaningful for banks" in m["note"]


def test_unknown_sector_type_raises():
    fin = make_financials([300_000, 340_000, 370_000, 391_000])
    with pytest.raises(ValueError):
        compute_multiples(fin, make_market(), sector_type="crypto")


# ---------------------------------------------------------------------------
# Bounds, is_meaningful, exclusion_note
# ---------------------------------------------------------------------------


def test_bounds_and_sector_lists_match_design():
    assert MULTIPLE_BOUNDS == {"ev_ebitda_ttm": (0, 100), "pe_ttm": (0, 200),
                               "ev_revenue_ttm": (0, 100), "p_tbv": (0, 20)}
    assert MULTIPLES_BY_SECTOR == {"industrial": ["ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm"],
                                   "bank": ["pe_ttm", "p_tbv"]}


def test_is_meaningful_boundaries():
    # lower < value <= upper
    assert is_meaningful("ev_ebitda_ttm", 12.3)
    assert is_meaningful("ev_ebitda_ttm", 100.0)       # upper bound is inclusive
    assert not is_meaningful("ev_ebitda_ttm", 100.01)
    assert not is_meaningful("ev_ebitda_ttm", 0.0)     # lower bound is exclusive
    assert not is_meaningful("ev_ebitda_ttm", -5.0)
    assert not is_meaningful("pe_ttm", 200.5)
    assert is_meaningful("pe_ttm", 200.0)
    assert not is_meaningful("pe_ttm", -10.0)
    assert not is_meaningful("pe_ttm", np.nan)
    assert not is_meaningful("pe_ttm", math.inf)
    # Unbounded names only need to be finite.
    assert is_meaningful("net_margin", -0.4)
    assert not is_meaningful("net_margin", np.nan)


def test_exclusion_note_text():
    assert exclusion_note("ev_ebitda_ttm", -12.3) == "EV/EBITDA -12.3x is negative (NM); excluded from peer stats"
    assert exclusion_note("pe_ttm", 250.0) == "P/E 250.0x exceeds 200x (NM); excluded from peer stats"
    assert exclusion_note("p_tbv", np.nan) == "P/TBV n/a (missing input); excluded from peer stats"
    assert exclusion_note("ev_ebitda_ttm", 12.0) == ""  # meaningful -> no note


# ---------------------------------------------------------------------------
# (2)(3) Outliers stay in the table, leave the stats
# ---------------------------------------------------------------------------


def peer_table(rows: list[dict]) -> pd.DataFrame:
    """A comps-shaped frame (index = ticker) from a list of multiple dicts."""
    df = pd.DataFrame(rows).set_index("ticker")
    return df


def test_negative_ebitda_company_excluded_from_stats_but_kept_in_table():
    # Peer D has negative EBITDA -> negative EV/EBITDA and a net loss -> negative P/E.
    fin_d = make_financials([300_000, 340_000, 370_000, 391_000],
                            ttm=dict(ebit=-50_000.0, da=10_000.0,           # EBITDA = -40,000
                                     net_income=-20_000.0, eps_diluted=-1.30))
    m_d = compute_multiples(fin_d, make_market())
    # EV/EBITDA = 3,070,000 / -40,000 = -76.75x ; P/E = 200 / -1.30 = -153.8x
    assert m_d["ev_ebitda_ttm"] == pytest.approx(-76.75)
    assert m_d["pe_ttm"] == pytest.approx(200 / -1.30)
    assert "EV/EBITDA -76.8x is negative (NM)" in m_d["note"]
    assert "P/E -153.8x is negative (NM)" in m_d["note"]

    peers = peer_table([
        dict(ticker="A", ev_revenue_ttm=5.0, ev_ebitda_ttm=10.0, pe_ttm=20.0),
        dict(ticker="B", ev_revenue_ttm=6.0, ev_ebitda_ttm=12.0, pe_ttm=24.0),
        dict(ticker="C", ev_revenue_ttm=7.0, ev_ebitda_ttm=14.0, pe_ttm=28.0),
        dict(ticker="D", ev_revenue_ttm=m_d["ev_revenue_ttm"], ev_ebitda_ttm=m_d["ev_ebitda_ttm"],
             pe_ttm=m_d["pe_ttm"], note=m_d["note"]),
    ])
    stats = summary_stats(peers)

    # D is still in the peer table with its raw negative multiple and a note ...
    assert "D" in peers.index
    assert peers.loc["D", "ev_ebitda_ttm"] < 0
    assert "excluded from peer stats" in peers.loc["D", "note"]
    # ... but the stats are over A, B, C only: median of [10, 12, 14] = 12, mean = 12.
    assert stats.loc["median", "ev_ebitda_ttm"] == pytest.approx(12.0)
    assert stats.loc["mean", "ev_ebitda_ttm"] == pytest.approx(12.0)
    assert stats.loc["median", "pe_ttm"] == pytest.approx(24.0)
    # EV/Revenue of D (7.675x) is meaningful and stays in: median of [5, 6, 7, 7.675] = 6.5
    assert stats.loc["median", "ev_revenue_ttm"] == pytest.approx(6.5)


def test_absurd_multiples_excluded_from_stats():
    peers = peer_table([
        dict(ticker="A", ev_ebitda_ttm=10.0, pe_ttm=20.0),
        dict(ticker="B", ev_ebitda_ttm=12.0, pe_ttm=24.0),
        dict(ticker="C", ev_ebitda_ttm=14.0, pe_ttm=28.0),
        dict(ticker="HIGH_EV", ev_ebitda_ttm=150.0, pe_ttm=25.0),   # EV/EBITDA > 100 -> out
        dict(ticker="HIGH_PE", ev_ebitda_ttm=11.0, pe_ttm=350.0),   # P/E > 200 -> out
        dict(ticker="LOSS", ev_ebitda_ttm=13.0, pe_ttm=-15.0),      # P/E < 0 -> out
    ])
    stats = summary_stats(peers)
    # EV/EBITDA eligible: [10, 12, 14, 11, 13] -> mean 12, median 12
    assert stats.loc["mean", "ev_ebitda_ttm"] == pytest.approx(12.0)
    assert stats.loc["median", "ev_ebitda_ttm"] == pytest.approx(12.0)
    # P/E eligible: [20, 24, 28, 25] -> mean 24.25, median 24.5
    assert stats.loc["mean", "pe_ttm"] == pytest.approx(24.25)
    assert stats.loc["median", "pe_ttm"] == pytest.approx(24.5)
    # The raw values are untouched in the input table.
    assert peers.loc["HIGH_EV", "ev_ebitda_ttm"] == 150.0
    assert peers.loc["LOSS", "pe_ttm"] == -15.0


def test_compute_multiples_flags_high_ev_ebitda_and_pe_in_note():
    # EBITDA of 20,000 -> EV/EBITDA = 3,070,000 / 20,000 = 153.5x ; EPS 0.50 -> P/E 400x
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ttm=dict(ebit=10_000.0, da=10_000.0, eps_diluted=0.50))
    m = compute_multiples(fin, make_market())
    assert m["ev_ebitda_ttm"] == pytest.approx(153.5)
    assert m["pe_ttm"] == pytest.approx(400.0)
    assert "EV/EBITDA 153.5x exceeds 100x" in m["note"]
    assert "P/E 400.0x exceeds 200x" in m["note"]


def test_mask_non_meaningful_only_touches_bounded_columns():
    peers = peer_table([
        dict(ticker="A", ev_ebitda_ttm=10.0, pe_ttm=-5.0, net_margin=-0.3, note="x"),
        dict(ticker="B", ev_ebitda_ttm=500.0, pe_ttm=15.0, net_margin=0.2, note=""),
    ])
    masked = mask_non_meaningful(peers)
    assert masked.loc["A", "ev_ebitda_ttm"] == 10.0
    assert math.isnan(masked.loc["A", "pe_ttm"])
    assert math.isnan(masked.loc["B", "ev_ebitda_ttm"])
    assert masked.loc["B", "pe_ttm"] == 15.0
    assert masked.loc["A", "net_margin"] == -0.3       # unbounded -> untouched
    assert masked.loc["A", "note"] == "x"              # text untouched
    assert peers.loc["B", "ev_ebitda_ttm"] == 500.0    # original not mutated


# ---------------------------------------------------------------------------
# summary_stats shape, percentiles, target exclusion
# ---------------------------------------------------------------------------


def test_summary_stats_shape_and_default_columns():
    peers = peer_table([
        dict(ticker="A", ev_revenue_ttm=1.0, ev_ebitda_ttm=1.0, pe_ttm=1.0, p_tbv=1.0, net_margin=0.1),
        dict(ticker="B", ev_revenue_ttm=2.0, ev_ebitda_ttm=2.0, pe_ttm=2.0, p_tbv=2.0, net_margin=0.2),
    ])
    stats = summary_stats(peers)
    assert list(stats.index) == ["mean", "median", "p25", "p75"]
    assert list(stats.columns) == list(MULTIPLE_BOUNDS)   # only bounded multiples, not margins
    # Explicit column list is honoured.
    assert list(summary_stats(peers, ["pe_ttm"]).columns) == ["pe_ttm"]


def test_percentiles_match_excel_quartile_inc():
    # Excel QUARTILE.INC({1,2,3,4},1) = 1.75 and QUARTILE.INC({1,2,3,4},3) = 3.25
    peers = peer_table([dict(ticker=t, ev_ebitda_ttm=v) for t, v in zip("ABCD", [1.0, 2.0, 3.0, 4.0])])
    stats = summary_stats(peers)
    assert stats.loc["p25", "ev_ebitda_ttm"] == pytest.approx(1.75)
    assert stats.loc["p75", "ev_ebitda_ttm"] == pytest.approx(3.25)
    assert stats.loc["median", "ev_ebitda_ttm"] == pytest.approx(2.5)
    assert stats.loc["mean", "ev_ebitda_ttm"] == pytest.approx(2.5)


def test_summary_stats_excludes_target_rows():
    peers = peer_table([
        dict(ticker="TGT", is_target=True, ev_ebitda_ttm=50.0, pe_ttm=90.0),
        dict(ticker="A", is_target=False, ev_ebitda_ttm=10.0, pe_ttm=20.0),
        dict(ticker="B", is_target=False, ev_ebitda_ttm=12.0, pe_ttm=24.0),
        dict(ticker="C", is_target=False, ev_ebitda_ttm=14.0, pe_ttm=28.0),
    ])
    stats = summary_stats(peers)
    assert stats.loc["mean", "ev_ebitda_ttm"] == pytest.approx(12.0)
    assert stats.loc["median", "pe_ttm"] == pytest.approx(24.0)


def test_summary_stats_all_excluded_gives_nan():
    peers = peer_table([dict(ticker="A", pe_ttm=-1.0), dict(ticker="B", pe_ttm=np.nan)])
    stats = summary_stats(peers)
    assert stats["pe_ttm"].isna().all()


# ---------------------------------------------------------------------------
# (10) implied_valuation, hand-checked
# ---------------------------------------------------------------------------


def test_implied_valuation_industrial_by_hand():
    """
    Target: revenue 400,000; EBITDA 130,000; EPS 6.50; debt 100,000; cash 30,000;
    minority 0; preferred 0; shares 15,000 (all $mm / mm except EPS).

    EV/EBITDA at 10.0x - 14.0x:
      EV low  = 10 x 130,000 = 1,300,000 ; equity = 1,300,000 - 100,000 - 0 - 0 + 30,000 = 1,230,000
                                            price  = 1,230,000 / 15,000 = 82.00
      EV high = 14 x 130,000 = 1,820,000 ; equity = 1,820,000 - 70,000 = 1,750,000
                                            price  = 1,750,000 / 15,000 = 116.67
    EV/Revenue at 2.0x - 4.0x:
      EV low  = 800,000  ; equity = 730,000   ; price = 48.67
      EV high = 1,600,000; equity = 1,530,000 ; price = 102.00
    P/E at 20.0x - 30.0x:
      price low  = 20 x 6.50 = 130.00 ; equity = 130 x 15,000 = 1,950,000
      price high = 30 x 6.50 = 195.00 ; equity = 195 x 15,000 = 2,925,000
    """
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ttm=dict(revenue=400_000.0, ebit=100_000.0, da=30_000.0))
    stats = pd.DataFrame(
        {"ev_revenue_ttm": [3.0, 3.0, 2.0, 4.0],
         "ev_ebitda_ttm": [12.0, 12.0, 10.0, 14.0],
         "pe_ttm": [25.0, 25.0, 20.0, 30.0]},
        index=["mean", "median", "p25", "p75"],
    )
    ff = implied_valuation(fin, make_market(), stats, sector_type="industrial")

    assert list(ff.columns) == IMPLIED_COLUMNS
    assert ff["method"].tolist() == ["EV / Revenue (TTM)", "EV / EBITDA (TTM)", "P / E (TTM)"]

    ebitda = ff.set_index("method").loc["EV / EBITDA (TTM)"]
    assert ebitda["metric_name"] == "EBITDA (TTM)"
    assert ebitda["metric_value"] == pytest.approx(130_000.0)
    assert ebitda["low_multiple"] == pytest.approx(10.0)
    assert ebitda["high_multiple"] == pytest.approx(14.0)
    assert ebitda["implied_ev_low"] == pytest.approx(1_300_000.0)
    assert ebitda["implied_ev_high"] == pytest.approx(1_820_000.0)
    assert ebitda["implied_equity_low"] == pytest.approx(1_230_000.0)
    assert ebitda["implied_equity_high"] == pytest.approx(1_750_000.0)
    assert ebitda["implied_price_low"] == pytest.approx(82.00, abs=0.005)
    assert ebitda["implied_price_high"] == pytest.approx(116.67, abs=0.005)

    rev = ff.set_index("method").loc["EV / Revenue (TTM)"]
    assert rev["metric_value"] == pytest.approx(400_000.0)
    assert rev["implied_equity_low"] == pytest.approx(730_000.0)
    assert rev["implied_price_high"] == pytest.approx(102.00, abs=0.005)

    pe = ff.set_index("method").loc["P / E (TTM)"]
    assert pe["metric_name"] == "Diluted EPS (TTM)"
    assert pe["metric_value"] == pytest.approx(6.50)
    assert pe["implied_price_low"] == pytest.approx(130.00, abs=0.005)
    assert pe["implied_price_high"] == pytest.approx(195.00, abs=0.005)
    assert pe["implied_equity_low"] == pytest.approx(1_950_000.0)
    assert pe["implied_equity_high"] == pytest.approx(2_925_000.0)
    # Industrial P/E row: EV bridged back = equity + debt - cash = 1,950,000 + 70,000
    assert pe["implied_ev_low"] == pytest.approx(2_020_000.0)


def test_implied_valuation_bank_by_hand():
    """
    Bank target: TBV 430,000; EPS 6.50; shares 15,000.
    P/TBV at 1.0x - 2.0x: equity 430,000 - 860,000 ; price 28.67 - 57.33
    P/E   at 10.0x - 12.0x: price 65.00 - 78.00 ; equity 975,000 - 1,170,000
    """
    fin = make_financials([300_000, 340_000, 370_000, 391_000], ebit=np.nan, da=np.nan, ebitda=np.nan)
    stats = pd.DataFrame({"pe_ttm": [11.0, 11.0, 10.0, 12.0], "p_tbv": [1.5, 1.5, 1.0, 2.0]},
                         index=["mean", "median", "p25", "p75"])
    ff = implied_valuation(fin, make_market(), stats, sector_type="bank").set_index("method")

    assert list(ff.index) == ["P / E (TTM)", "P / TBV"]
    tbv = ff.loc["P / TBV"]
    assert tbv["metric_name"] == "Tangible book value"
    assert tbv["metric_value"] == pytest.approx(430_000.0)
    assert tbv["implied_equity_low"] == pytest.approx(430_000.0)
    assert tbv["implied_equity_high"] == pytest.approx(860_000.0)
    assert tbv["implied_price_low"] == pytest.approx(28.67, abs=0.005)
    assert tbv["implied_price_high"] == pytest.approx(57.33, abs=0.005)
    assert math.isnan(tbv["implied_ev_low"])   # EV is not meaningful for banks

    pe = ff.loc["P / E (TTM)"]
    assert pe["implied_price_low"] == pytest.approx(65.0)
    assert pe["implied_equity_high"] == pytest.approx(1_170_000.0)
    assert math.isnan(pe["implied_ev_high"])


def test_implied_valuation_missing_stat_column_gives_nan_row():
    fin = make_financials([300_000, 340_000, 370_000, 391_000])
    stats = pd.DataFrame({"ev_ebitda_ttm": [12.0, 12.0, 10.0, 14.0]}, index=["mean", "median", "p25", "p75"])
    ff = implied_valuation(fin, make_market(), stats).set_index("method")
    assert math.isnan(ff.loc["P / E (TTM)", "low_multiple"])
    assert math.isnan(ff.loc["P / E (TTM)", "implied_price_low"])
    assert ff.loc["EV / EBITDA (TTM)", "implied_price_low"] == pytest.approx(82.0)


def test_implied_valuation_pe_falls_back_to_net_income_when_eps_missing():
    # P/E 20x on net income 120,000 -> equity 2,400,000 -> price 160.00
    fin = make_financials([300_000, 340_000, 370_000, 391_000],
                          ttm=dict(eps_diluted=np.nan, net_income=120_000.0))
    stats = pd.DataFrame({"pe_ttm": [25.0, 25.0, 20.0, 30.0]}, index=["mean", "median", "p25", "p75"])
    ff = implied_valuation(fin, make_market(), stats).set_index("method")
    pe = ff.loc["P / E (TTM)"]
    assert pe["metric_name"] == "Net income (TTM)"
    assert pe["implied_equity_low"] == pytest.approx(2_400_000.0)
    assert pe["implied_price_low"] == pytest.approx(160.0)
