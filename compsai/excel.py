"""
Module 3: write the banker-formatted comps workbook (docs/DESIGN.md section 6).

What it does
------------
Takes the per-company records produced by edgar.py / market.py / valuation.py and
writes one .xlsx per run with four sheets:

* ``Inputs``          - the raw financials and market data, one 9-row block per company.
                        Every hardcoded number is blue; EBITDA and tangible book value are
                        black formulas so the two derived lines are visibly derived.
* ``Comps``           - the comparable-companies table. EVERY numeric cell is an Excel
                        formula that points back into ``Inputs`` (so a reviewer can click
                        any multiple and trace it to the filing values), followed by
                        Mean / Median / 25th / 75th percentile rows that are live
                        ``AVERAGE`` / ``MEDIAN`` / ``QUARTILE`` formulas over hidden-ish
                        "stat-eligible" helper columns. A target company, or a company
                        with no market price / share count, has ``""`` helper cells so it
                        never enters the peer statistics (a blank Inputs price reads as 0
                        in Excel and would otherwise produce a bogus, in-bounds multiple).
* ``Football Field``  - (only when a target is named) the implied valuation range for the
                        target from the 25th-75th percentile multiples, with a horizontal
                        floating-bar chart.
* ``Commentary``      - Claude-drafted normalization items and premium/discount notes
                        (Module 4), or a placeholder line per company.

Why formulas instead of pasted numbers
--------------------------------------
A banker's model must recalculate when an input changes (a price update, a restated
EBITDA). Pasting Python values would produce a dead table; writing formulas keeps the
workbook auditable and lets the stats rows move when a peer's input is edited.

Formatting conventions (industry standard "blue inputs, black formulas"):
Arial 10 everywhere; blue ``0000FF`` font for hardcoded inputs, black for formulas;
``$#,##0`` for $ millions, ``$#,##0.00`` per share, ``0.0"x"`` for multiples, ``0.0%``.
Only Excel-2007-era functions are used (AVERAGE, MEDIAN, QUARTILE, IF, AND, ISNUMBER,
IFERROR) so both Excel and LibreOffice evaluate every cell.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

import compsai
from compsai.models import CommentaryResult, CompanyData

# valuation.py owns the bounds; the fallback keeps excel.py importable on its own and
# MUST stay identical to DESIGN.md section 4.
try:  # pragma: no cover - which branch runs depends on the checkout
    from compsai.valuation import MULTIPLE_BOUNDS, MULTIPLES_BY_SECTOR
except ImportError:  # pragma: no cover
    MULTIPLE_BOUNDS: dict[str, tuple[float, float]] = {
        "ev_ebitda_ttm": (0, 100),
        "pe_ttm": (0, 200),
        "ev_revenue_ttm": (0, 100),
        "p_tbv": (0, 20),
    }
    MULTIPLES_BY_SECTOR: dict[str, list[str]] = {
        "industrial": ["ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm"],
        "bank": ["pe_ttm", "p_tbv"],
    }

log = logging.getLogger("compsai")

# --------------------------------------------------------------------------------------
# Styling constants
# --------------------------------------------------------------------------------------

FONT_NAME = "Arial"
FONT_SIZE = 10
BLUE = "0000FF"  # hardcoded inputs
BLACK = "000000"  # formulas and labels
GREY = "808080"  # helper columns / secondary text

FMT_DOLLARS = "$#,##0"  # $ in millions
FMT_PER_SHARE = "$#,##0.00"  # price, EPS
FMT_MULTIPLE = '0.0"x"'  # 12.3x
FMT_PERCENT = "0.0%"  # margins, growth
FMT_SHARES = "#,##0"  # millions of shares
FMT_INTEGER = "0"  # fiscal year

#: Number format for each column "kind" used in COLUMN_SPECS and the Inputs layout.
FORMAT_BY_KIND: dict[str, str | None] = {
    "text": None,
    "notes": None,
    "per_share": FMT_PER_SHARE,
    "dollars": FMT_DOLLARS,
    "multiple": FMT_MULTIPLE,
    "percent": FMT_PERCENT,
    "shares": FMT_SHARES,
    "integer": FMT_INTEGER,
}

SHEET_INPUTS = "Inputs"
SHEET_COMPS = "Comps"
SHEET_FOOTBALL = "Football Field"
SHEET_COMMENTARY = "Commentary"

# --------------------------------------------------------------------------------------
# Layout constants (DESIGN.md section 6)
# --------------------------------------------------------------------------------------

INPUTS_FIRST_BLOCK_ROW = 3
INPUTS_BLOCK_HEIGHT = 9
INPUTS_MAX_FY_ROWS = 4  # rows b+3..b+6; the newest FY is always at b+6

#: Inputs sheet column letter for each financials column (plus the label columns).
INPUTS_COLS: dict[str, str] = {
    "period_type": "A",
    "fiscal_year": "B",
    "period_end": "C",
    "revenue": "D",
    "ebit": "E",
    "da": "F",
    "ebitda": "G",
    "net_income": "H",
    "eps_diluted": "I",
    "total_debt": "J",
    "cash": "K",
    "minority_interest": "L",
    "preferred": "M",
    "diluted_shares": "N",
    "total_equity": "O",
    "goodwill": "P",
    "intangibles": "Q",
    "tangible_book_value": "R",
}

#: Header text and number kind for the Inputs financials table, in column order A..R.
INPUTS_HEADERS: list[tuple[str, str, str]] = [
    ("period_type", "Period", "text"),
    ("fiscal_year", "Fiscal year", "integer"),
    ("period_end", "Period end", "text"),
    ("revenue", "Revenue", "dollars"),
    ("ebit", "EBIT", "dollars"),
    ("da", "D&A", "dollars"),
    ("ebitda", "EBITDA", "dollars"),
    ("net_income", "Net income", "dollars"),
    ("eps_diluted", "Diluted EPS", "per_share"),
    ("total_debt", "Total debt", "dollars"),
    ("cash", "Cash", "dollars"),
    ("minority_interest", "Minority interest", "dollars"),
    ("preferred", "Preferred", "dollars"),
    ("diluted_shares", "Diluted shares (mm)", "shares"),
    ("total_equity", "Total equity", "dollars"),
    ("goodwill", "Goodwill", "dollars"),
    ("intangibles", "Intangibles", "dollars"),
    ("tangible_book_value", "Tangible book value", "dollars"),
]

#: Inputs columns that are blue hardcodes; G (EBITDA) and R (TBV) are black formulas.
INPUTS_HARDCODE_KEYS = frozenset(
    ["revenue", "ebit", "da", "net_income", "eps_diluted", "total_debt", "cash",
     "minority_interest", "preferred", "diluted_shares", "total_equity", "goodwill",
     "intangibles"]
)

COMPS_TITLE_ROW = 1
COMPS_SUBTITLE_ROW = 2
COMPS_HEADER_ROW = 4
COMPS_FIRST_COMPANY_ROW = 5

#: Comps columns. ``key`` matches the compute_multiples index (or name/ticker/note),
#: ``header`` is the visible header text, ``kind`` picks the number format, and
#: ``sectors`` says which sector types show the column. Order matters: it is the
#: left-to-right order of the sheet for every sector after filtering by ``sectors``.
COLUMN_SPECS: list[dict] = [
    {"key": "name", "header": "Company", "kind": "text", "sectors": ("industrial", "bank")},
    {"key": "ticker", "header": "Ticker", "kind": "text", "sectors": ("industrial", "bank")},
    {"key": "price", "header": "Price", "kind": "per_share", "sectors": ("industrial", "bank")},
    {"key": "market_cap", "header": "Market Cap", "kind": "dollars", "sectors": ("industrial", "bank")},
    {"key": "ev", "header": "Enterprise Value", "kind": "dollars", "sectors": ("industrial",)},
    {"key": "ev_revenue_ttm", "header": "EV / Revenue (TTM)", "kind": "multiple", "sectors": ("industrial",)},
    {"key": "ev_ebitda_ttm", "header": "EV / EBITDA (TTM)", "kind": "multiple", "sectors": ("industrial",)},
    {"key": "pe_ttm", "header": "P / E (TTM)", "kind": "multiple", "sectors": ("industrial", "bank")},
    {"key": "p_tbv", "header": "P / TBV", "kind": "multiple", "sectors": ("bank",)},
    {"key": "ebitda_margin", "header": "EBITDA Margin", "kind": "percent", "sectors": ("industrial",)},
    {"key": "net_margin", "header": "Net Margin", "kind": "percent", "sectors": ("industrial", "bank")},
    {"key": "revenue_growth_1y", "header": "Revenue Growth (1Y)", "kind": "percent", "sectors": ("industrial", "bank")},
    {"key": "revenue_growth_3y_cagr", "header": "Revenue CAGR (3Y)", "kind": "percent", "sectors": ("industrial", "bank")},
    {"key": "note", "header": "Notes", "kind": "notes", "sectors": ("industrial", "bank")},
]

#: Stats rows under the comps table: (label, Excel function, extra argument or None).
STAT_ROWS: list[tuple[str, str, int | None]] = [
    ("Mean", "AVERAGE", None),
    ("Median", "MEDIAN", None),
    ("25th Percentile", "QUARTILE", 1),
    ("75th Percentile", "QUARTILE", 3),
]

#: Which Inputs line a football-field method values, and how to describe it.
FOOTBALL_METRIC: dict[str, tuple[str, str]] = {
    "ev_revenue_ttm": ("Revenue (TTM)", "revenue"),
    "ev_ebitda_ttm": ("EBITDA (TTM)", "ebitda"),
    "pe_ttm": ("Diluted EPS (TTM)", "eps_diluted"),
    "p_tbv": ("Tangible book value", "tangible_book_value"),
}

TARGET_NOTE = "(target — excluded from peer stats)"
NO_PRICE_NOTE = "(no market price/share count — excluded from peer stats)"


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def column_specs(sector_type: str) -> list[dict]:
    """The Comps columns shown for ``sector_type``, left to right."""
    if sector_type not in MULTIPLES_BY_SECTOR:
        raise ValueError(
            f"unknown sector_type {sector_type!r}; expected one of {list(MULTIPLES_BY_SECTOR)}"
        )
    return [spec for spec in COLUMN_SPECS if sector_type in spec["sectors"]]


def inputs_block_start(i: int) -> int:
    """First row of company block ``i`` (0-based) on the Inputs sheet: b = 3 + 9*i."""
    return INPUTS_FIRST_BLOCK_ROW + INPUTS_BLOCK_HEIGHT * i


def header_for(key: str) -> str:
    """Visible Comps header for a multiples key (e.g. 'ev_ebitda_ttm' -> 'EV / EBITDA (TTM)')."""
    for spec in COLUMN_SPECS:
        if spec["key"] == key:
            return spec["header"]
    raise KeyError(key)


def _clean(value):
    """Convert pandas/numpy scalars to plain Python and NaN/None/'' to None (blank cell)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value != "" else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):  # numpy scalar -> python scalar
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _has_market_price(company: CompanyData) -> bool:
    """True when both price and shares outstanding are finite numbers.

    Without them every Comps multiple for the company is built on a blank Inputs cell,
    which Excel evaluates as 0 (price $0, EV = net debt), so the row must stay out of
    the peer statistics exactly as valuation.py keeps NaN multiples out of them.
    """
    market = company.market or {}
    for key in ("price", "shares_outstanding"):
        value = _clean(market.get(key))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
    return True


def _font(color: str = BLACK, bold: bool = False, italic: bool = False) -> Font:
    return Font(name=FONT_NAME, size=FONT_SIZE, color=color, bold=bold, italic=italic)


def _put(
    ws: Worksheet,
    row: int,
    col: int,
    value,
    *,
    kind: str | None = None,
    hardcode: bool = False,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    wrap: bool = False,
    align: str | None = None,
):
    """Write one cell with the house style.

    ``hardcode=True`` paints a *number* blue (an input someone typed or fetched).
    Formulas (strings starting with '=') and labels are always black unless ``color``
    overrides. NaN/None become an empty cell, never the text 'nan'.
    """
    cell = ws.cell(row=row, column=col)
    value = _clean(value)
    cell.value = value
    is_formula = isinstance(value, str) and value.startswith("=")
    if color is None:
        color = BLUE if (hardcode and value is not None and not is_formula) else BLACK
    cell.font = _font(color=color, bold=bold, italic=italic)
    fmt = FORMAT_BY_KIND.get(kind) if kind else None
    if fmt:
        cell.number_format = fmt
    if wrap or align:
        cell.alignment = Alignment(wrap_text=wrap, horizontal=align, vertical="top" if wrap else None)
    return cell


def _print_setup(ws: Worksheet, print_area: str) -> None:
    """Landscape, fit to one page wide (any number of pages tall), fixed print area."""
    ws.print_area = print_area
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def _set_widths(ws: Worksheet, widths: dict[str, float]) -> None:
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width


_HEADER_FILL = PatternFill("solid", fgColor="1F3864")  # navy header band
_HEADER_FONT_COLOR = "FFFFFF"
_THIN = Side(style="thin", color="808080")


def _header_cell(ws: Worksheet, row: int, col: int, text: str, *, wrap: bool = True):
    """Navy header band with white bold text (the standard banker header)."""
    cell = _put(ws, row, col, text, bold=True, color=_HEADER_FONT_COLOR, wrap=wrap, align="center")
    cell.fill = _HEADER_FILL
    cell.border = Border(bottom=_THIN)
    return cell


def _fy_rows(financials: pd.DataFrame) -> pd.DataFrame:
    """The (up to) four most recent fiscal-year rows, oldest first."""
    fy = financials[financials["period_type"] == "FY"]
    return fy.tail(INPUTS_MAX_FY_ROWS)


def _ttm_row(financials: pd.DataFrame) -> pd.Series | None:
    ttm = financials[financials["period_type"] == "TTM"]
    if ttm.empty:
        return None
    return ttm.iloc[-1]


def _slug(text: str) -> str:
    """File-name-safe version of the peer-set name."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip()) or "custom"


# --------------------------------------------------------------------------------------
# Sheet "Inputs"
# --------------------------------------------------------------------------------------


def _write_inputs(ws: Worksheet, companies: list[CompanyData], as_of: date) -> None:
    """One 9-row block per company; see DESIGN.md section 6 for the exact layout."""
    _put(ws, 1, 1, "Inputs — raw financials ($ in millions except per share) and market data",
         bold=True)

    for i, company in enumerate(companies):
        b = inputs_block_start(i)
        fin = company.financials
        market = company.market or {}

        # b: ticker / name
        _put(ws, b, 1, company.ticker, bold=True)
        _put(ws, b, 2, company.name, bold=True)

        # b+1: market data. Price and shares are blue hardcodes fetched from Yahoo/XBRL.
        _put(ws, b + 1, 1, "Price ($)")
        _put(ws, b + 1, 2, market.get("price"), kind="per_share", hardcode=True)
        _put(ws, b + 1, 3, "Shares out (mm)")
        _put(ws, b + 1, 4, market.get("shares_outstanding"), kind="shares", hardcode=True)
        _put(ws, b + 1, 5, "Currency")
        _put(ws, b + 1, 6, market.get("currency") or _currency_from(fin))
        _put(ws, b + 1, 7, "Price as of")
        _put(ws, b + 1, 8, market.get("as_of") or as_of.isoformat())

        # b+2: table headers
        for col, (_key, header, _kind) in enumerate(INPUTS_HEADERS, start=1):
            _header_cell(ws, b + 2, col, header)

        # b+3..b+6: fiscal years, bottom-aligned so the newest is always at b+6
        fy = _fy_rows(fin)
        first_fy_row = b + 3 + (INPUTS_MAX_FY_ROWS - len(fy))
        for j, (_idx, row) in enumerate(fy.iterrows()):
            _write_financial_row(ws, first_fy_row + j, row)

        # b+7: TTM. If the extractor found no TTM row, the multiples were computed on the
        # latest FY (DESIGN section 4), so link the TTM slot to the newest FY row with
        # formulas rather than duplicating hardcodes.
        ttm = _ttm_row(fin)
        if ttm is not None:
            _write_financial_row(ws, b + 7, ttm)
        elif len(fy) > 0:
            _write_ttm_as_fy_link(ws, b + 7, b + 6)
        # b+8 stays blank

    _set_widths(ws, {"A": 14, "B": 16, "C": 12, **{get_column_letter(c): 13 for c in range(4, 19)}})
    ws.freeze_panes = "A3"


def _currency_from(fin: pd.DataFrame) -> str | None:
    if "currency" in fin.columns and len(fin) > 0:
        return _clean(fin["currency"].iloc[-1])
    return None


def _ebitda_formula(r: int) -> str:
    """Inputs!G = EBIT + D&A, or "" when either input is blank (mirrors NaN in valuation.py)."""
    return f'=IF(AND(ISNUMBER(E{r}),ISNUMBER(F{r})),E{r}+F{r},"")'


def _tbv_formula(r: int) -> str:
    """Inputs!R = total equity - preferred - goodwill - intangibles (tangible common equity), or "" when equity is blank."""
    return f'=IF(ISNUMBER(O{r}),O{r}-M{r}-P{r}-Q{r},"")'


def _write_financial_row(ws: Worksheet, r: int, row: pd.Series) -> None:
    """Write one FY/TTM line: blue hardcodes plus the two derived black formulas."""
    for col, (key, _header, kind) in enumerate(INPUTS_HEADERS, start=1):
        if key == "ebitda":
            # EBITDA = EBIT + D&A (operating income plus depreciation & amortization).
            # Blank when either component is missing so it matches valuation.py (NaN) instead
            # of silently collapsing to EBIT or 0; downstream IFERROR()s then show "n/a".
            _put(ws, r, col, _ebitda_formula(r), kind=kind)
        elif key == "tangible_book_value":
            # TBV = total equity - goodwill - intangibles (book equity net of intangibles);
            # blank when equity is missing (goodwill/intangibles default to 0 like edgar.py).
            _put(ws, r, col, _tbv_formula(r), kind=kind)
        else:
            value = row.get(key)
            if key == "fiscal_year" and _clean(value) is not None:
                value = int(value)
            _put(ws, r, col, value, kind=kind, hardcode=key in INPUTS_HARDCODE_KEYS)


def _write_ttm_as_fy_link(ws: Worksheet, r: int, fy_row: int) -> None:
    """TTM slot when no TTM exists: every value is a formula to the newest FY row."""
    for col, (key, _header, kind) in enumerate(INPUTS_HEADERS, start=1):
        letter = get_column_letter(col)
        if key == "period_type":
            _put(ws, r, col, "TTM (=FY)")
        elif key == "ebitda":
            _put(ws, r, col, _ebitda_formula(r), kind=kind)  # EBITDA = EBIT + D&A
        elif key == "tangible_book_value":
            _put(ws, r, col, _tbv_formula(r), kind=kind)  # TBV = equity - preferred - goodwill - intangibles
        else:
            _put(ws, r, col, f"={letter}{fy_row}", kind=kind)


# --------------------------------------------------------------------------------------
# Sheet "Comps"
# --------------------------------------------------------------------------------------


def _comps_formulas(b: int) -> dict[str, str]:
    """Excel formulas for one company whose Inputs block starts at row ``b``.

    Every formula points at Inputs (DESIGN.md section 6). Rows: t = b+7 (TTM),
    y = b+6 (newest FY), b+5 (prior FY), b+3 (oldest FY, three years before y).
    """
    t, y, prior, oldest = b + 7, b + 6, b + 5, b + 3
    inp = "Inputs!$"
    price_cell, shares_cell = f"{inp}B${b + 1}", f"{inp}D${b + 1}"
    debt_cell, cash_cell = f"{inp}J${t}", f"{inp}K${t}"
    # Every root input is guarded with ISNUMBER so a blank cell yields "n/a" instead of the 0
    # Excel would otherwise read - the same rule valuation.py applies (NaN in, NaN out).
    price = f'IF(ISNUMBER({price_cell}),{price_cell},"n/a")'
    # Market cap = price x shares outstanding
    market_cap = f'IF(AND(ISNUMBER({price_cell}),ISNUMBER({shares_cell})),{price_cell}*{shares_cell},"n/a")'
    # EV = market cap + total debt + minority interest + preferred - cash
    # (Rosenbaum & Pearl / Training the Street enterprise-value bridge); minority interest and
    # preferred default to 0 when untagged, but a missing debt or cash figure makes EV n/a.
    ev = (f'IF(AND(ISNUMBER({price_cell}),ISNUMBER({shares_cell}),ISNUMBER({debt_cell}),ISNUMBER({cash_cell})),'
          f'{price_cell}*{shares_cell}+{debt_cell}+{inp}L${t}+{inp}M${t}-{cash_cell},"n/a")')
    return {
        "price": f"={price}",
        "market_cap": f"={market_cap}",
        "ev": f"={ev}",
        # EV / Revenue (TTM)
        "ev_revenue_ttm": f'=IFERROR(({ev})/{inp}D${t},"n/a")',
        # EV / EBITDA (TTM)
        "ev_ebitda_ttm": f'=IFERROR(({ev})/{inp}G${t},"n/a")',
        # P / E = price / diluted EPS (TTM); when no EPS is reported, market cap / net income
        # (the same fallback valuation.py uses, so both sides stay in step)
        "pe_ttm": f'=IFERROR(IF(ISNUMBER({inp}I${t}),({price})/{inp}I${t},({market_cap})/{inp}H${t}),"n/a")',
        # P / TBV = market cap / tangible book value
        "p_tbv": f'=IFERROR(({market_cap})/{inp}R${t},"n/a")',
        # EBITDA margin = EBITDA / revenue (TTM)
        "ebitda_margin": f'=IFERROR({inp}G${t}/{inp}D${t},"n/a")',
        # Net margin = net income / revenue (TTM)
        "net_margin": f'=IFERROR({inp}H${t}/{inp}D${t},"n/a")',
        # 1-year revenue growth = FY[-1] / FY[-2] - 1 (fiscal years, not TTM)
        "revenue_growth_1y": f'=IFERROR({inp}D${y}/{inp}D${prior}-1,"n/a")',
        # 3-year revenue CAGR = (FY[-1] / FY[-4]) ^ (1/3) - 1
        "revenue_growth_3y_cagr": f'=IFERROR(({inp}D${y}/{inp}D${oldest})^(1/3)-1,"n/a")',
    }


def _helper_formula(cell_ref: str, key: str) -> str:
    """Stat-eligibility test mirroring MULTIPLE_BOUNDS: finite and lower < x <= upper."""
    lower, upper = MULTIPLE_BOUNDS[key]
    return f'=IF(AND(ISNUMBER({cell_ref}),{cell_ref}>{lower:g},{cell_ref}<={upper:g}),{cell_ref},"")'


def _write_comps(
    ws: Worksheet,
    companies: list[CompanyData],
    flags: list[bool],
    sector_type: str,
    peer_set: str,
    as_of: date,
) -> dict:
    """Write the comps table and return the row/column map other sheets link to."""
    specs = column_specs(sector_type)
    stat_keys = [k for k in MULTIPLES_BY_SECTOR[sector_type] if k in MULTIPLE_BOUNDS]
    col_of = {spec["key"]: idx for idx, spec in enumerate(specs, start=1)}
    n_visible = len(specs)
    helper_col_of = {key: n_visible + 1 + j for j, key in enumerate(stat_keys)}

    _put(ws, COMPS_TITLE_ROW, 1, f"Comparable Companies Analysis — {peer_set}", bold=True)
    _put(ws, COMPS_SUBTITLE_ROW, 1,
         f"As of {as_of.isoformat()} · $ in millions except per share · "
         "Source: SEC EDGAR XBRL, Yahoo Finance", italic=True, color=GREY)

    for spec in specs:
        _header_cell(ws, COMPS_HEADER_ROW, col_of[spec["key"]], spec["header"])
    for key, col in helper_col_of.items():
        _put(ws, COMPS_HEADER_ROW, col, f"{header_for(key)} (stat-eligible)",
             italic=True, color=GREY, wrap=True, align="center")

    first_row = COMPS_FIRST_COMPANY_ROW
    last_row = first_row + len(companies) - 1
    for i, (company, is_target) in enumerate(zip(companies, flags)):
        r = first_row + i
        formulas = _comps_formulas(inputs_block_start(i))
        no_price = not _has_market_price(company)
        if no_price:
            log.warning("%s has no market price/share count; excluded from peer stats", company.ticker)
        for spec in specs:
            key, col, kind = spec["key"], col_of[spec["key"]], spec["kind"]
            if key == "name":
                _put(ws, r, col, company.name)
            elif key == "ticker":
                _put(ws, r, col, company.ticker)
            elif key == "note":
                _put(ws, r, col, _note_text(company, is_target, no_price), wrap=False)
            else:
                _put(ws, r, col, formulas[key], kind=kind, align="right")
        for key, hcol in helper_col_of.items():
            ref = f"{get_column_letter(col_of[key])}{r}"
            # Target companies (and companies with no price) are shown but never feed
            # the peer statistics: "" is ignored by AVERAGE / MEDIAN / QUARTILE.
            excluded = is_target or no_price
            _put(ws, r, hcol, '=""' if excluded else _helper_formula(ref, key),
                 kind="multiple", color=GREY)

    # Stats rows: blank line, then Mean / Median / 25th / 75th as live formulas over the
    # helper columns (text "" cells are ignored by AVERAGE, MEDIAN and QUARTILE).
    stats_rows: dict[str, int] = {}
    stat_ids = ["mean", "median", "p25", "p75"]
    for j, (label, func, arg) in enumerate(STAT_ROWS):
        r = last_row + 2 + j
        stats_rows[stat_ids[j]] = r
        _put(ws, r, 1, label, bold=True)
        for key in stat_keys:
            hl = get_column_letter(helper_col_of[key])
            rng = f"{hl}{first_row}:{hl}{last_row}"
            formula = f"={func}({rng},{arg})" if arg is not None else f"={func}({rng})"
            _put(ws, r, col_of[key], formula, kind="multiple", bold=True, align="right")
        for col in range(1, n_visible + 1):
            ws.cell(row=r, column=col).border = Border(top=_THIN if j == 0 else None)

    widths = {"A": 30, "B": 9}
    for spec in specs[2:]:
        letter = get_column_letter(col_of[spec["key"]])
        widths[letter] = 44 if spec["key"] == "note" else 15
    for col in helper_col_of.values():
        widths[get_column_letter(col)] = 6
    _set_widths(ws, widths)
    ws.row_dimensions[COMPS_HEADER_ROW].height = 30
    ws.freeze_panes = ws.cell(row=COMPS_FIRST_COMPANY_ROW, column=1).coordinate
    # Print setup: one landscape page wide, helper columns left off the printed page.
    _print_setup(ws, f"A1:{get_column_letter(n_visible)}{max(stats_rows.values())}")

    return {
        "first_row": first_row,
        "last_row": last_row,
        "stats_rows": stats_rows,
        "col_letter": {key: get_column_letter(col) for key, col in col_of.items()},
        "helper_col_letter": {key: get_column_letter(col) for key, col in helper_col_of.items()},
    }


def _note_text(company: CompanyData, is_target: bool, no_price: bool = False) -> str | None:
    note = ""
    if company.multiples is not None and "note" in company.multiples.index:
        note = _clean(company.multiples["note"]) or ""
    if is_target:
        note = f"{note} {TARGET_NOTE}".strip()
    elif no_price:
        note = f"{note} {NO_PRICE_NOTE}".strip()
    return note or None


# --------------------------------------------------------------------------------------
# Sheet "Football Field"
# --------------------------------------------------------------------------------------

FF_HEADERS = [
    "Method", "Target metric", "Metric value", "Low multiple (25th)", "High multiple (75th)",
    "Implied EV low", "Implied EV high", "Implied equity low", "Implied equity high",
    "Implied price low", "Implied price high",
]
FF_HEADER_ROW = 4
FF_FIRST_ROW = 5
FF_RANGE_COL = len(FF_HEADERS) + 1  # helper: high - low (floored at 0), the visible bar
FF_BASE_COL = len(FF_HEADERS) + 2  # helper: low floored at 0, the invisible bar base


def _write_football_field(
    ws: Worksheet,
    target: CompanyData,
    target_index: int,
    sector_type: str,
    comps_map: dict,
) -> None:
    """Implied valuation range for the target from the peers' 25th-75th percentile multiples."""
    b = inputs_block_start(target_index)
    t = b + 7
    inp = "Inputs!$"
    price_ref = f"{inp}B${b + 1}"
    shares_ref = f"{inp}D${b + 1}"
    debt_ref, cash_ref = f"{inp}J${t}", f"{inp}K${t}"
    eps_ref, ni_ref = f"{inp}I${t}", f"{inp}H${t}"
    # Net-debt items of the EV bridge (debt + minority + preferred - cash) at TTM; "n/a" when
    # debt or cash is blank so a missing input never silently counts as 0.
    bridge = (f'IF(AND(ISNUMBER({debt_ref}),ISNUMBER({cash_ref})),'
              f'{debt_ref}+{inp}L${t}+{inp}M${t}-{cash_ref},"n/a")')

    _put(ws, 1, 1, f"Football Field — {target.ticker} ({target.name})", bold=True)
    _put(ws, 2, 1, "Current price")
    _put(ws, 2, 2, f'=IF(ISNUMBER({price_ref}),{price_ref},"n/a")', kind="per_share")
    _put(ws, 2, 3, "Shares out (mm)")
    _put(ws, 2, 4, f'=IF(ISNUMBER({shares_ref}),{shares_ref},"n/a")', kind="shares")

    for col, header in enumerate(FF_HEADERS, start=1):
        _header_cell(ws, FF_HEADER_ROW, col, header)
    _put(ws, FF_HEADER_ROW, FF_RANGE_COL, "Bar span (high − low)", italic=True, color=GREY,
         wrap=True, align="center")
    _put(ws, FF_HEADER_ROW, FF_BASE_COL, "Bar base (low, floored at 0)", italic=True, color=GREY,
         wrap=True, align="center")

    p25_row = comps_map["stats_rows"]["p25"]
    p75_row = comps_map["stats_rows"]["p75"]
    methods = [k for k in MULTIPLES_BY_SECTOR[sector_type] if k in FOOTBALL_METRIC]
    for j, key in enumerate(methods):
        r = FF_FIRST_ROW + j
        metric_label, metric_key = FOOTBALL_METRIC[key]
        metric_ref = f"{inp}{INPUTS_COLS[metric_key]}${t}"
        comps_letter = comps_map["col_letter"][key]
        metric_kind = "per_share" if metric_key == "eps_diluted" else "dollars"

        _put(ws, r, 1, header_for(key))
        if key == "pe_ttm":
            # Diluted EPS when reported; otherwise net income (valuation.py makes the same switch).
            _put(ws, r, 2, f'=IF(ISNUMBER({eps_ref}),"{metric_label}","Net income (TTM)")')
            _put(ws, r, 3, f'=IF(ISNUMBER({eps_ref}),{eps_ref},IF(ISNUMBER({ni_ref}),{ni_ref},"n/a"))',
                 kind=metric_kind)
        else:
            _put(ws, r, 2, metric_label)
            _put(ws, r, 3, f'=IF(ISNUMBER({metric_ref}),{metric_ref},"n/a")', kind=metric_kind)
        _put(ws, r, 4, f"='{SHEET_COMPS}'!${comps_letter}${p25_row}", kind="multiple")
        _put(ws, r, 5, f"='{SHEET_COMPS}'!${comps_letter}${p75_row}", kind="multiple")

        if key in ("ev_revenue_ttm", "ev_ebitda_ttm"):
            # EV-based: implied EV = multiple x metric; equity = EV - debt - minority
            # - preferred + cash (EV bridge in reverse); price = equity / shares
            for lo_hi, mult_col in (("low", "D"), ("high", "E")):
                ev_col, eq_col, px_col = _ff_cols(lo_hi)
                _put(ws, r, ev_col, f'=IFERROR({mult_col}{r}*C{r},"n/a")', kind="dollars")
                ev_ref = f"{get_column_letter(ev_col)}{r}"
                _put(ws, r, eq_col, f'=IFERROR({ev_ref}-({bridge}),"n/a")', kind="dollars")
                eq_ref = f"{get_column_letter(eq_col)}{r}"
                _put(ws, r, px_col, f'=IFERROR({eq_ref}/{shares_ref},"n/a")', kind="per_share")
        else:
            # Equity-based (P/E, P/TBV): P/E -> price = multiple x EPS, equity = price x
            # shares; P/TBV -> equity = multiple x TBV, price = equity / shares.
            for lo_hi, mult_col in (("low", "D"), ("high", "E")):
                ev_col, eq_col, px_col = _ff_cols(lo_hi)
                eq_ref = f"{get_column_letter(eq_col)}{r}"
                px_ref = f"{get_column_letter(px_col)}{r}"
                if key == "pe_ttm":
                    # EPS path: price = multiple x EPS, equity = price x shares.
                    # Net-income path (no EPS): equity = multiple x net income, price = equity / shares.
                    _put(ws, r, px_col,
                         f'=IFERROR(IF(ISNUMBER({eps_ref}),{mult_col}{r}*C{r},{mult_col}{r}*C{r}/{shares_ref}),"n/a")',
                         kind="per_share")
                    _put(ws, r, eq_col,
                         f'=IFERROR(IF(ISNUMBER({eps_ref}),{px_ref}*{shares_ref},{mult_col}{r}*C{r}),"n/a")',
                         kind="dollars")
                else:  # p_tbv
                    _put(ws, r, eq_col, f'=IFERROR({mult_col}{r}*C{r},"n/a")', kind="dollars")
                    _put(ws, r, px_col, f'=IFERROR({eq_ref}/{shares_ref},"n/a")', kind="per_share")
                if sector_type == "bank":
                    # EV is not a meaningful concept for banks (debt is their raw material).
                    _put(ws, r, ev_col, "n/a", align="right")
                else:
                    # Implied EV = implied equity + net debt items (EV bridge forward)
                    _put(ws, r, ev_col, f'=IFERROR({eq_ref}+{bridge},"n/a")', kind="dollars")

        # Chart helpers. A stacked bar cannot start below the axis, so the invisible base is
        # the low price floored at 0 and the visible span runs from there to the high price;
        # a fully negative range (net debt above implied EV) draws nothing.
        _put(ws, r, FF_BASE_COL, f"=IFERROR(MAX(J{r},0),0)", kind="per_share", color=GREY)
        _put(ws, r, FF_RANGE_COL, f"=IFERROR(MAX(K{r},0)-MAX(J{r},0),0)", kind="per_share", color=GREY)

    last_row = FF_FIRST_ROW + len(methods) - 1
    _add_football_chart(ws, target.ticker, last_row)

    _set_widths(ws, {"A": 22, "B": 22, "C": 14, "D": 14, "E": 14, "F": 15, "G": 15, "H": 16,
                     "I": 16, "J": 15, "K": 15, get_column_letter(FF_RANGE_COL): 12,
                     get_column_letter(FF_BASE_COL): 12})
    ws.row_dimensions[FF_HEADER_ROW].height = 30
    ws.freeze_panes = f"A{FF_FIRST_ROW}"


def _ff_cols(lo_hi: str) -> tuple[int, int, int]:
    """(EV col, equity col, price col) for the low or high side of the football table."""
    if lo_hi == "low":
        return 6, 8, 10  # F, H, J
    return 7, 9, 11  # G, I, K


def _add_football_chart(ws: Worksheet, ticker: str, last_row: int) -> None:
    """Horizontal floating bars: an invisible 'low' series stacked under a 'high - low' series."""
    chart = BarChart()
    chart.type = "bar"  # horizontal bars
    chart.grouping = "stacked"
    chart.overlap = 100
    chart.title = f"Implied share price ({ticker})"
    chart.y_axis.title = "Implied share price ($)"
    chart.y_axis.number_format = FMT_PER_SHARE
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    chart.legend = None
    chart.height = 7.5
    chart.width = 18

    low = Reference(ws, min_col=FF_BASE_COL, min_row=FF_HEADER_ROW, max_row=last_row)
    span = Reference(ws, min_col=FF_RANGE_COL, min_row=FF_HEADER_ROW, max_row=last_row)
    chart.add_data(low, titles_from_data=True)
    chart.add_data(span, titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=FF_FIRST_ROW, max_row=last_row))
    chart.series[0].graphicalProperties.noFill = True  # hide the offset bar
    chart.series[1].graphicalProperties.solidFill = "1F3864"
    ws.add_chart(chart, f"A{last_row + 3}")


# --------------------------------------------------------------------------------------
# Sheet "Commentary"
# --------------------------------------------------------------------------------------

COMMENTARY_TABLE_HEADERS = [
    "Description", "Amount ($mm)", "Fiscal year", "Direction", "Source quote", "Verified in filing",
]


def _write_commentary(
    ws: Worksheet,
    companies: list[CompanyData],
    commentary: dict[str, CommentaryResult] | None,
    peer_set: str,
) -> None:
    """One block per company: header, premium/discount notes, normalization table, source."""
    _put(ws, 1, 1, f"AI Commentary — {peer_set}", bold=True)
    _put(ws, 2, 1, "Drafted by Claude from the latest annual filing; quotes marked 'Yes' were "
         "verified verbatim against the filing text. Review before use.", italic=True, color=GREY)
    r = 4
    for company in companies:
        result = _commentary_for(company.ticker, commentary)
        reason = _missing_reason(result, commentary)
        if reason is not None:
            _put(ws, r, 1, f"{company.ticker} — {company.name}", bold=True)
            _put(ws, r + 1, 1, f"Commentary not generated: {reason}", italic=True, color=GREY)
            r += 3
            continue
        r = _write_commentary_block(ws, r, company, result)

    _set_widths(ws, {"A": 44, "B": 16, "C": 12, "D": 12, "E": 70, "F": 18})


def _commentary_for(ticker: str, commentary: dict | None) -> CommentaryResult | None:
    if not commentary:
        return None
    result = commentary.get(ticker)
    if result is None:  # tolerate lower-case keys
        result = commentary.get(ticker.upper()) or commentary.get(ticker.lower())
    return result


def _missing_reason(result: CommentaryResult | None, commentary: dict | None) -> str | None:
    """Why there is nothing to show for this company, or None when there is content."""
    if commentary is None:
        return "AI commentary was not run for this analysis"
    if result is None:
        return "no commentary result for this company"
    has_content = bool(result.premium_discount) or bool(result.normalization_items)
    if not has_content:
        return "; ".join(result.errors) if result.errors else "the model returned no content"
    return None


def _write_commentary_block(ws: Worksheet, r: int, company: CompanyData, res: CommentaryResult) -> int:
    pd_ = res.premium_discount or {}
    verdict = pd_.get("premium_or_discount") or "unknown"
    _put(ws, r, 1, f"{company.ticker} — {company.name} — {verdict}", bold=True)
    r += 1

    def label_value(label: str, value: str | None, row: int) -> int:
        _put(ws, row, 1, label, bold=True, wrap=True)
        _put(ws, row, 2, value or "unknown", wrap=True)
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
        return row + 1

    r = label_value("Growth outlook", pd_.get("growth_outlook"), r)
    r = label_value("Margin trajectory", pd_.get("margin_trajectory"), r)
    risks = pd_.get("key_risks") or []
    if isinstance(risks, str):  # a bare string would otherwise iterate per character
        risks = [risks]
    if not risks:
        r = label_value("Key risks", "unknown", r)
    for k, risk in enumerate(risks):
        r = label_value("Key risks" if k == 0 else "", str(risk), r)
    r = label_value("Rationale", pd_.get("rationale"), r)
    r += 1

    for col, header in enumerate(COMMENTARY_TABLE_HEADERS, start=1):
        _header_cell(ws, r, col, header)
    r += 1
    if not res.normalization_items:
        _put(ws, r, 1, "No normalization items identified", italic=True, color=GREY)
        r += 1
    for item in res.normalization_items:
        _put(ws, r, 1, item.get("description"), wrap=True)
        # Amounts come from the model (hardcoded inputs), hence blue.
        _put(ws, r, 2, item.get("amount_usd_m"), kind="dollars", hardcode=True)
        _put(ws, r, 3, item.get("fiscal_year"), kind="integer", hardcode=True)
        _put(ws, r, 4, item.get("direction"))
        _put(ws, r, 5, item.get("source_quote"), wrap=True)
        _put(ws, r, 6, "Yes" if item.get("verified") else "No", align="center")
        r += 1

    source = f"Source: {res.source_form or 'filing'} filed {res.filing_date or 'n/a'} — {res.source_url or 'n/a'}"
    _put(ws, r, 1, source, italic=True, color=GREY)
    if res.errors:
        r += 1
        _put(ws, r, 1, "Warnings: " + "; ".join(res.errors), italic=True, color=GREY)
    return r + 2


# --------------------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------------------


def _order_companies(
    companies: list[CompanyData], target: str | None
) -> tuple[list[CompanyData], list[bool], int | None]:
    """Target first (if given); returns (ordered companies, is_target flags, target index)."""
    target_upper = target.upper() if target else None
    flagged = [
        (c, bool(c.is_target) or (target_upper is not None and c.ticker.upper() == target_upper))
        for c in companies
    ]
    target_index: int | None = None
    ordered: list[tuple[CompanyData, bool]] = []
    if target_upper is not None:
        for c, flag in flagged:
            if c.ticker.upper() == target_upper:
                ordered.append((c, flag))
                target_index = 0
                break
        else:
            log.warning("target %s is not among the companies; Football Field sheet skipped", target)
        ordered.extend((c, flag) for c, flag in flagged if c.ticker.upper() != target_upper)
    else:
        ordered = flagged
    return [c for c, _ in ordered], [f for _, f in ordered], target_index


def write_comps_workbook(
    peer_set: str,
    companies: list[CompanyData],
    sector_type: str = "industrial",
    target: str | None = None,
    commentary: dict[str, CommentaryResult] | None = None,
    out_dir: Path | None = None,
    as_of: date | None = None,
) -> Path:
    """Write ``output/comps_{peer_set}_{YYYY-MM-DD}.xlsx`` and return its path.

    ``companies`` come from the pipeline (one CompanyData each). ``target`` (a ticker)
    goes first in the table, is excluded from the peer statistics, and gets the Football
    Field sheet. ``commentary`` maps ticker -> CommentaryResult (None = not run).
    """
    if not companies:
        raise ValueError("write_comps_workbook needs at least one company")
    column_specs(sector_type)  # validates sector_type early
    as_of = as_of or date.today()
    out_dir = Path(out_dir) if out_dir is not None else compsai.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"comps_{_slug(peer_set)}_{as_of.isoformat()}.xlsx"

    ordered, flags, target_index = _order_companies(companies, target)

    wb = Workbook()
    ws_comps = wb.active
    ws_comps.title = SHEET_COMPS
    ws_inputs = wb.create_sheet(SHEET_INPUTS)

    _write_inputs(ws_inputs, ordered, as_of)
    comps_map = _write_comps(ws_comps, ordered, flags, sector_type, peer_set, as_of)
    if target is not None and target_index is not None:
        ws_ff = wb.create_sheet(SHEET_FOOTBALL)
        _write_football_field(ws_ff, ordered[target_index], target_index, sector_type, comps_map)
    _write_commentary(wb.create_sheet(SHEET_COMMENTARY), ordered, commentary, peer_set)

    wb.save(path)
    log.info("wrote comps workbook %s (%d companies, sector=%s)", path, len(ordered), sector_type)
    return path
