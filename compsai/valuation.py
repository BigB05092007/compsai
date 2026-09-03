"""
Trading multiples, peer statistics and implied valuation (Module 2 of CompsAI).

This module turns one company's financials (the DataFrame produced by
``compsai.edgar.extract_financials``) plus its market data (the dict produced by
``compsai.market.get_market_data``) into the numbers that appear on a comps sheet.
Every formula here follows the standard investment-banking conventions taught in
Rosenbaum & Pearl, *Investment Banking*, and the Training the Street comps course, so
that each line can be defended in an interview. The binding interface is
docs/DESIGN.md section 4.

How the logic flows, in plain English
-------------------------------------

1. **Enterprise value (EV).** Market capitalisation is what the equity is worth.
   Enterprise value is what the *whole business* is worth to all capital providers:

       EV = market cap + total debt + minority interest + preferred equity - cash

   We add debt, minority interest and preferred because those claims would have to be
   settled by a buyer of the whole company; we subtract cash because a buyer gets it
   back. Minority interest and preferred are zero for most companies, so a missing value
   is treated as zero; a missing market cap, debt or cash figure makes EV unknowable
   (NaN) because we would be guessing at a large number.

2. **Multiples.** A multiple divides a value (EV or price) by a matching operating
   metric so that companies of different sizes can be compared:

   * ``EV / Revenue``  - value per dollar of sales; useful when earnings are thin.
   * ``EV / EBITDA``   - value per dollar of operating cash earnings before financing,
     taxes and non-cash charges; the workhorse industrial multiple because it is
     capital-structure neutral (EV is pre-debt, EBITDA is pre-interest).
   * ``P / E``         - price per dollar of diluted earnings per share; an equity
     multiple, so it *is* affected by leverage.
   * ``P / TBV``       - market cap per dollar of tangible book value; the bank multiple.

   Multiples use trailing-twelve-month (TTM) figures so every peer is measured over the
   same recent window regardless of fiscal-year end. Growth rates use full fiscal
   years, because a year-over-year comparison of two TTM windows is not what "revenue
   growth" means on a comps page.

3. **Banks are different.** A bank's debt (deposits, borrowings) is its raw material,
   not a financing choice, and interest is its revenue, not a cost below EBITDA. So EV
   and EBITDA are meaningless for a bank; bankers value them on P/E and price-to-
   tangible-book-value. ``sector_type="bank"`` therefore blanks the EV-based fields and
   adds P/TBV to the peer statistics.

4. **Outliers.** A negative EV/EBITDA (negative EBITDA), a negative P/E (a loss) or an
   absurdly high multiple (EV/EBITDA above 100x, P/E above 200x, usually a company
   whose earnings are near zero) is labelled "NM" (not meaningful) on a real comps page.
   Averaging such values would poison the peer mean and median. We therefore keep the
   raw number *in the table* - the reader should see it - annotate it in the ``note``
   column, and mask it to NaN only when computing the mean / median / quartiles.

5. **Implied valuation (the football field).** Applying the peer 25th and 75th
   percentile multiples to the target's own metric gives a low-high range of implied
   EV, then implied equity value (EV minus net debt and other claims), then implied
   share price (equity divided by shares outstanding). For P/E and P/TBV the range is
   computed at the equity level directly.

Units follow docs/DESIGN.md section 1: money in USD millions, shares in millions,
per-share values in USD, margins and growth as fractions, multiples as plain floats.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger("compsai")

# ---------------------------------------------------------------------------
# Constants (from docs/DESIGN.md section 4)
# ---------------------------------------------------------------------------

#: A multiple is "meaningful" only if it is finite and ``lower < value <= upper``.
#: The lower bound of 0 excludes negative multiples (negative EBITDA or a net loss);
#: the upper bounds catch near-zero denominators that produce silly numbers.
MULTIPLE_BOUNDS: dict[str, tuple[float, float]] = {
    "ev_ebitda_ttm": (0, 100),
    "pe_ttm": (0, 200),
    "ev_revenue_ttm": (0, 100),
    "p_tbv": (0, 20),
}

#: Which multiples belong on the comps page (and in the peer statistics) per sector.
MULTIPLES_BY_SECTOR: dict[str, list[str]] = {
    "industrial": ["ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm"],
    "bank": ["pe_ttm", "p_tbv"],
}

#: Output index of ``compute_multiples`` - exactly this order (DESIGN.md section 4).
MULTIPLES_INDEX: list[str] = [
    "market_cap",
    "ev",
    "ev_revenue_ttm",
    "ev_ebitda_ttm",
    "pe_ttm",
    "ebitda_margin",
    "net_margin",
    "revenue_growth_1y",
    "revenue_growth_3y_cagr",
    "p_tbv",
    "price",
    "note",
]

#: Row labels of ``summary_stats``.
STAT_ROWS: list[str] = ["mean", "median", "p25", "p75"]

#: Human-readable labels used in notes and in the football-field table.
MULTIPLE_LABELS: dict[str, str] = {
    "ev_revenue_ttm": "EV/Revenue",
    "ev_ebitda_ttm": "EV/EBITDA",
    "pe_ttm": "P/E",
    "p_tbv": "P/TBV",
}

#: Note text for the bank branch (also asserted in tests).
BANK_NOTE = "EV-based multiples (EV, EV/Revenue, EV/EBITDA, EBITDA margin) are not meaningful for banks"


# ---------------------------------------------------------------------------
# Small helpers - kept tiny so each one is obviously correct
# ---------------------------------------------------------------------------


def _num(value: Any) -> float:
    """Coerce any scalar (None, numpy number, string) to a float; missing -> NaN."""
    if value is None:
        return math.nan
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _is_nan(value: float) -> bool:
    """True for NaN (and None); False for any real number including 0."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _safe_divide(numerator: float, denominator: float) -> float:
    """numerator / denominator, or NaN when either is missing or the denominator is 0."""
    numerator = _num(numerator)
    denominator = _num(denominator)
    if _is_nan(numerator) or _is_nan(denominator) or denominator == 0:
        return math.nan
    return numerator / denominator


def _fy_rows(financials: pd.DataFrame) -> pd.DataFrame:
    """Fiscal-year rows only, oldest first (sorted by fiscal_year to be safe)."""
    fy = financials.loc[financials["period_type"] == "FY"]
    return fy.sort_values("fiscal_year", kind="stable").reset_index(drop=True)


def _current_row(financials: pd.DataFrame) -> tuple[pd.Series, str]:
    """
    The row that multiples are computed on: the TTM row, else the latest FY row.

    Returns (row, note). The note is "" when a TTM row exists and explains the
    fallback otherwise.
    """
    ttm = financials.loc[financials["period_type"] == "TTM"]
    if len(ttm) > 0:
        return ttm.iloc[-1], ""
    fy = _fy_rows(financials)
    if len(fy) == 0:
        raise ValueError("financials has neither a TTM row nor any FY rows")
    row = fy.iloc[-1]
    note = f"no TTM row; multiples use FY{int(row['fiscal_year'])}"
    log.warning("compute_multiples: %s", note)
    return row, note


# ---------------------------------------------------------------------------
# Enterprise value
# ---------------------------------------------------------------------------


def enterprise_value(
    market_cap: float,
    total_debt: float,
    cash: float,
    minority_interest: float = 0.0,
    preferred: float = 0.0,
) -> float:
    """
    Enterprise value in USD millions.

    Convention (Rosenbaum & Pearl / Training the Street):
        EV = market cap + total debt + minority interest + preferred equity - cash

    NaN minority interest or preferred equity is treated as zero (most companies have
    none, and EDGAR simply has no tag). NaN market cap, debt or cash returns NaN
    because EV cannot be honestly estimated without them.
    """
    market_cap = _num(market_cap)
    total_debt = _num(total_debt)
    cash = _num(cash)
    minority_interest = _num(minority_interest)
    preferred = _num(preferred)

    if _is_nan(market_cap) or _is_nan(total_debt) or _is_nan(cash):
        return math.nan
    # Absent minority interest / preferred means the company has none -> 0.
    if _is_nan(minority_interest):
        minority_interest = 0.0
    if _is_nan(preferred):
        preferred = 0.0

    # EV = market cap + total debt + minority interest + preferred equity - cash
    return market_cap + total_debt + minority_interest + preferred - cash


# ---------------------------------------------------------------------------
# Meaningfulness of a multiple
# ---------------------------------------------------------------------------


def is_meaningful(name: str, value: float) -> bool:
    """
    True if ``value`` should enter the peer statistics for multiple ``name``.

    A multiple is meaningful iff it is finite and ``lower < value <= upper`` per
    ``MULTIPLE_BOUNDS``. Names without bounds (margins, growth) only need to be finite.
    """
    value = _num(value)
    if not math.isfinite(value):
        return False
    if name not in MULTIPLE_BOUNDS:
        return True
    lower, upper = MULTIPLE_BOUNDS[name]
    return lower < value <= upper


def exclusion_note(name: str, value: float) -> str:
    """
    Plain-English reason a multiple was excluded from the peer statistics.

    The raw number stays in the comps table; this text goes in the ``note`` column so a
    reader sees *why* it is labelled NM (not meaningful).
    """
    label = MULTIPLE_LABELS.get(name, name)
    value = _num(value)
    if not math.isfinite(value):
        return f"{label} n/a (missing input); excluded from peer stats"
    lower, upper = MULTIPLE_BOUNDS.get(name, (-math.inf, math.inf))
    if value <= lower:
        # A non-positive multiple means a negative denominator (loss / negative EBITDA).
        return f"{label} {value:.1f}x is negative (NM); excluded from peer stats"
    if value > upper:
        return f"{label} {value:.1f}x exceeds {upper:g}x (NM); excluded from peer stats"
    return ""


# ---------------------------------------------------------------------------
# Multiples for one company
# ---------------------------------------------------------------------------


def _market_cap(market: dict) -> float:
    """Market cap ($mm) from the market dict; recomputed from price x shares if absent."""
    market_cap = _num(market.get("market_cap"))
    if not _is_nan(market_cap):
        return market_cap
    # Market cap = share price x shares outstanding (shares in millions -> $mm).
    return _safe_multiply(_num(market.get("price")), _num(market.get("shares_outstanding")))


def _safe_multiply(a: float, b: float) -> float:
    """a * b, or NaN when either is missing."""
    if _is_nan(a) or _is_nan(b):
        return math.nan
    return a * b


def _revenue_growth(fy: pd.DataFrame) -> tuple[float, float]:
    """
    (1-year growth, 3-year CAGR) from fiscal-year revenues, oldest first.

    Convention:
        growth_1y   = FY[-1] / FY[-2] - 1
        cagr_3y     = (FY[-1] / FY[-4]) ** (1/3) - 1      (needs four year-end points)
    """
    revenues = [_num(v) for v in fy["revenue"].tolist()]

    growth_1y = math.nan
    if len(revenues) >= 2:
        # Revenue growth (1Y) = latest FY revenue / prior FY revenue - 1
        ratio = _safe_divide(revenues[-1], revenues[-2])
        growth_1y = ratio - 1 if not _is_nan(ratio) else math.nan

    cagr_3y = math.nan
    if len(revenues) >= 4:
        # Revenue CAGR (3Y) = (latest FY revenue / revenue three years earlier) ^ (1/3) - 1
        ratio = _safe_divide(revenues[-1], revenues[-4])
        # A negative or zero ratio has no real cube-root growth rate -> NaN.
        if not _is_nan(ratio) and ratio > 0:
            cagr_3y = ratio ** (1 / 3) - 1

    return growth_1y, cagr_3y


def compute_multiples(
    financials: pd.DataFrame,
    market: dict,
    sector_type: str = "industrial",
) -> pd.Series:
    """
    Trading multiples and margins for one company.

    Returns a Series indexed exactly as ``MULTIPLES_INDEX`` (DESIGN.md section 4):
    market_cap, ev, ev_revenue_ttm, ev_ebitda_ttm, pe_ttm, ebitda_margin, net_margin,
    revenue_growth_1y, revenue_growth_3y_cagr, p_tbv, price, note.

    Money in $mm, per-share in USD, margins/growth as fractions, multiples as floats.
    ``note`` is a "; "-joined list of caveats (fallbacks and NM exclusions), "" if clean.
    """
    if sector_type not in MULTIPLES_BY_SECTOR:
        raise ValueError(f"unknown sector_type {sector_type!r}; expected one of {list(MULTIPLES_BY_SECTOR)}")

    notes: list[str] = []
    row, row_note = _current_row(financials)  # TTM row, or latest FY with a note
    if row_note:
        notes.append(row_note)

    # --- inputs from the current (TTM) row and the market dict ------------------
    revenue = _num(row["revenue"])
    ebitda = _num(row["ebitda"])
    net_income = _num(row["net_income"])
    eps = _num(row["eps_diluted"])
    tbv = _num(row["tangible_book_value"])
    price = _num(market.get("price"))
    market_cap = _market_cap(market)
    is_bank = sector_type == "bank"

    # --- enterprise value and EV multiples (blank for banks) ----------------------
    if is_bank:
        ev = math.nan
        ev_revenue = math.nan
        ev_ebitda = math.nan
        ebitda_margin = math.nan
        notes.append(BANK_NOTE)
    else:
        # EV = market cap + total debt + minority interest + preferred equity - cash
        ev = enterprise_value(
            market_cap,
            _num(row["total_debt"]),
            _num(row["cash"]),
            _num(row["minority_interest"]),
            _num(row["preferred"]),
        )
        # EV / Revenue (TTM) = enterprise value / trailing-twelve-month revenue
        ev_revenue = _safe_divide(ev, revenue)
        # EV / EBITDA (TTM) = enterprise value / trailing-twelve-month EBITDA
        ev_ebitda = _safe_divide(ev, ebitda)
        # EBITDA margin = EBITDA / revenue
        ebitda_margin = _safe_divide(ebitda, revenue)

    # --- P/E: price / diluted EPS, falling back to market cap / net income ---------
    if not _is_nan(eps):
        # P / E (TTM) = share price / diluted EPS (TTM)
        pe = _safe_divide(price, eps)
    else:
        # P / E (TTM) = market cap / net income (TTM) - same ratio at the company level,
        # used when the filer reports no diluted EPS.
        pe = _safe_divide(market_cap, net_income)
        notes.append("EPS unavailable; P/E = market cap / net income")

    # --- margins, growth, P/TBV ----------------------------------------------------
    # Net margin = net income / revenue
    net_margin = _safe_divide(net_income, revenue)
    growth_1y, cagr_3y = _revenue_growth(_fy_rows(financials))
    # P / TBV = market cap / tangible book value (equity - goodwill - intangibles)
    p_tbv = _safe_divide(market_cap, tbv)

    values: dict[str, Any] = {
        "market_cap": market_cap,
        "ev": ev,
        "ev_revenue_ttm": ev_revenue,
        "ev_ebitda_ttm": ev_ebitda,
        "pe_ttm": pe,
        "ebitda_margin": ebitda_margin,
        "net_margin": net_margin,
        "revenue_growth_1y": growth_1y,
        "revenue_growth_3y_cagr": cagr_3y,
        "p_tbv": p_tbv,
        "price": price,
    }

    # --- flag NM multiples (kept in the table, masked only in the stats) ----------
    for name in MULTIPLES_BY_SECTOR[sector_type]:
        if not is_meaningful(name, values[name]):
            notes.append(exclusion_note(name, values[name]))

    values["note"] = "; ".join(n for n in notes if n)
    return pd.Series([values[k] for k in MULTIPLES_INDEX], index=MULTIPLES_INDEX, dtype=object)


# ---------------------------------------------------------------------------
# Peer statistics
# ---------------------------------------------------------------------------


def mask_non_meaningful(peer_df: pd.DataFrame) -> pd.DataFrame:
    """
    Copy of ``peer_df`` where every non-meaningful multiple (per ``MULTIPLE_BOUNDS``)
    is replaced by NaN. Only the bounded multiple columns are touched. Used by
    ``summary_stats``; the comps table itself keeps the raw values.
    """
    masked = peer_df.copy()
    for name in MULTIPLE_BOUNDS:
        if name not in masked.columns:
            continue
        numeric = pd.to_numeric(masked[name], errors="coerce").astype(float)
        keep = numeric.map(lambda v, n=name: is_meaningful(n, v))
        masked[name] = numeric.where(keep, np.nan)
    return masked


def summary_stats(peer_df: pd.DataFrame, multiples: list[str] | None = None) -> pd.DataFrame:
    """
    Mean, median, 25th and 75th percentile of each multiple across the peers.

    * Index: ``["mean", "median", "p25", "p75"]``; one column per multiple.
    * Default columns: the ``MULTIPLE_BOUNDS`` keys present in ``peer_df``.
    * Rows with ``is_target == True`` are excluded - the target is what we are valuing,
      so it must not influence its own benchmark.
    * Non-meaningful values are masked to NaN first (see ``mask_non_meaningful``).
    * Percentiles use pandas' default linear interpolation, which is the same
      algorithm as Excel's ``QUARTILE`` / ``PERCENTILE.INC`` (so p25 of [1,2,3,4] is 1.75).
    """
    if multiples is None:
        multiples = [name for name in MULTIPLE_BOUNDS if name in peer_df.columns]

    peers = peer_df
    if "is_target" in peers.columns:
        # Keep only the peers: the target must not sit inside its own benchmark.
        is_target = peers["is_target"].fillna(False).astype(bool)
        peers = peers.loc[~is_target]

    masked = mask_non_meaningful(peers)

    stats = pd.DataFrame(index=STAT_ROWS, columns=multiples, dtype=float)
    for name in multiples:
        if name not in masked.columns:
            log.warning("summary_stats: multiple %r not in peer_df; stats are NaN", name)
            continue
        values = pd.to_numeric(masked[name], errors="coerce").astype(float).dropna()
        if values.empty:
            continue  # leave NaN: no meaningful peer values
        stats.loc["mean", name] = values.mean()
        stats.loc["median", name] = values.median()
        stats.loc["p25", name] = values.quantile(0.25)  # linear == Excel QUARTILE.INC
        stats.loc["p75", name] = values.quantile(0.75)
    return stats


# ---------------------------------------------------------------------------
# Implied valuation (football field)
# ---------------------------------------------------------------------------

#: Output columns of ``implied_valuation`` (DESIGN.md section 4).
IMPLIED_COLUMNS: list[str] = [
    "method",
    "metric_name",
    "metric_value",
    "low_multiple",
    "high_multiple",
    "implied_ev_low",
    "implied_ev_high",
    "implied_equity_low",
    "implied_equity_high",
    "implied_price_low",
    "implied_price_high",
]

#: Display names for the football-field rows (match the Excel column headers).
METHOD_LABELS: dict[str, str] = {
    "ev_revenue_ttm": "EV / Revenue (TTM)",
    "ev_ebitda_ttm": "EV / EBITDA (TTM)",
    "pe_ttm": "P / E (TTM)",
    "p_tbv": "P / TBV",
}


def _stat(stats: pd.DataFrame, row: str, name: str) -> float:
    """stats.loc[row, name] as float, NaN if the column is absent."""
    if name not in stats.columns or row not in stats.index:
        return math.nan
    return _num(stats.loc[row, name])


def _ev_to_equity(ev: float, row: pd.Series) -> float:
    """
    Equity value from enterprise value (the EV bridge run backwards):
        equity = EV - total debt - minority interest - preferred equity + cash
    """
    minority = _num(row["minority_interest"])
    preferred = _num(row["preferred"])
    minority = 0.0 if _is_nan(minority) else minority
    preferred = 0.0 if _is_nan(preferred) else preferred
    debt = _num(row["total_debt"])
    cash = _num(row["cash"])
    if _is_nan(ev) or _is_nan(debt) or _is_nan(cash):
        return math.nan
    return ev - debt - minority - preferred + cash


def _equity_to_ev(equity: float, row: pd.Series) -> float:
    """
    Enterprise value from equity value (the EV bridge run forwards):
        EV = equity + total debt + minority interest + preferred equity - cash
    """
    return enterprise_value(
        equity, _num(row["total_debt"]), _num(row["cash"]),
        _num(row["minority_interest"]), _num(row["preferred"]),
    )


def implied_valuation(
    financials: pd.DataFrame,
    market: dict,
    stats: pd.DataFrame,
    sector_type: str = "industrial",
) -> pd.DataFrame:
    """
    Football-field table: one row per multiple in ``MULTIPLES_BY_SECTOR[sector_type]``,
    applying the peers' 25th (low) and 75th (high) percentile multiples to the target's
    own TTM metric.

    * EV-based methods: implied EV = multiple x metric;
      equity = EV - debt - minority - preferred + cash; price = equity / shares.
    * P/E: price = multiple x diluted EPS (TTM); equity = price x shares.
      (If EPS is missing, equity = multiple x net income and price = equity / shares.)
    * P/TBV: equity = multiple x tangible book value; price = equity / shares.
    * For equity-based methods on industrials, implied EV is bridged back
      (equity + debt + minority + preferred - cash); for banks it is NaN.

    Columns: ``IMPLIED_COLUMNS``. Units: $mm except implied prices (USD/share).
    """
    if sector_type not in MULTIPLES_BY_SECTOR:
        raise ValueError(f"unknown sector_type {sector_type!r}; expected one of {list(MULTIPLES_BY_SECTOR)}")

    row, _ = _current_row(financials)
    shares = _num(market.get("shares_outstanding"))
    is_bank = sector_type == "bank"

    records: list[dict[str, Any]] = []
    for name in MULTIPLES_BY_SECTOR[sector_type]:
        low = _stat(stats, "p25", name)
        high = _stat(stats, "p75", name)
        rec: dict[str, Any] = {
            "method": METHOD_LABELS[name],
            "low_multiple": low,
            "high_multiple": high,
        }

        if name in ("ev_revenue_ttm", "ev_ebitda_ttm"):
            metric_name = "Revenue (TTM)" if name == "ev_revenue_ttm" else "EBITDA (TTM)"
            metric = _num(row["revenue"] if name == "ev_revenue_ttm" else row["ebitda"])
            # Implied EV = peer multiple x target metric
            ev_low = _safe_multiply(low, metric)
            ev_high = _safe_multiply(high, metric)
            # Implied equity = EV - debt - minority - preferred + cash
            eq_low = _ev_to_equity(ev_low, row)
            eq_high = _ev_to_equity(ev_high, row)

        elif name == "pe_ttm":
            eps = _num(row["eps_diluted"])
            if not _is_nan(eps):
                metric_name, metric = "Diluted EPS (TTM)", eps
                # Implied price = peer P/E x target EPS; equity = price x shares
                eq_low = _safe_multiply(_safe_multiply(low, eps), shares)
                eq_high = _safe_multiply(_safe_multiply(high, eps), shares)
            else:
                metric_name, metric = "Net income (TTM)", _num(row["net_income"])
                # Implied equity = peer P/E x target net income (EPS unavailable)
                eq_low = _safe_multiply(low, metric)
                eq_high = _safe_multiply(high, metric)
            ev_low = math.nan if is_bank else _equity_to_ev(eq_low, row)
            ev_high = math.nan if is_bank else _equity_to_ev(eq_high, row)

        else:  # p_tbv
            metric_name, metric = "Tangible book value", _num(row["tangible_book_value"])
            # Implied equity = peer P/TBV x target tangible book value
            eq_low = _safe_multiply(low, metric)
            eq_high = _safe_multiply(high, metric)
            ev_low = math.nan if is_bank else _equity_to_ev(eq_low, row)
            ev_high = math.nan if is_bank else _equity_to_ev(eq_high, row)

        rec.update(
            metric_name=metric_name,
            metric_value=metric,
            implied_ev_low=ev_low,
            implied_ev_high=ev_high,
            implied_equity_low=eq_low,
            implied_equity_high=eq_high,
            # Implied share price = implied equity value / shares outstanding
            implied_price_low=_safe_divide(eq_low, shares),
            implied_price_high=_safe_divide(eq_high, shares),
        )
        records.append(rec)

    return pd.DataFrame.from_records(records, columns=IMPLIED_COLUMNS)
