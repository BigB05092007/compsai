# CompsAI design contract

This document is the single source of truth for how the modules fit together.
Every module is written against it, and the tests assert it. If you change a
convention here, change the code and the tests in the same commit.

Project root is the directory containing this `docs/` folder. The Python package
is `compsai/` inside it. Run every command from the project root.

## 1. Units (global, non-negotiable)

| Quantity | Unit | Example |
|---|---|---|
| Money aggregates: revenue, EBIT, D&A, EBITDA, net income, debt, cash, minority interest, preferred, equity, goodwill, intangibles, market cap, EV | **USD millions** (float) | Apple FY2023 revenue = `383285.0` |
| Share counts (shares outstanding, diluted shares) | **millions of shares** | `15550.0` |
| Per-share values (price, diluted EPS) | USD per share | `189.95`, `6.13` |
| Margins, growth rates | fractions | `0.15` means 15% |
| Multiples | plain floats | `12.3` means 12.3x |

`compsai.MILLION = 1_000_000` is the only divisor used to convert raw XBRL values.
Column names carry no unit suffix. Excel headers name the unit (`Revenue ($mm)`).

## 2. Module 1: `compsai/edgar.py`

### Public API

```python
get_cik(ticker: str, offline: bool = False) -> str          # 10-digit zero-padded, e.g. "0000320193"
get_company_name(ticker: str, offline: bool = False) -> str # "Apple Inc." (from company_tickers.json "title"; offline: facts["entityName"])
get_company_facts(cik: str, refresh: bool = False, offline: bool = False, ticker: str | None = None) -> dict
get_submissions(cik: str, refresh: bool = False, offline: bool = False, ticker: str | None = None) -> dict
extract_financials(facts: dict, n_years: int = 4) -> pd.DataFrame
```

`extract_financials` returns a DataFrame with **exactly these columns in this order**:

```
fiscal_year, period_type, period_end, revenue, ebit, da, ebitda, net_income, eps_diluted,
total_debt, cash, minority_interest, preferred, diluted_shares,
total_equity, goodwill, intangibles, tangible_book_value, currency
```

* `fiscal_year`: int. `period_type`: `"FY"` or `"TTM"`. `period_end`: ISO date string `YYYY-MM-DD`.
* Rows: up to `n_years` FY rows, **oldest first**, then exactly one TTM row **last**.
  `n_years` defaults to 4 because a 3-year CAGR needs four year-end points
  (FY_t and FY_t-3). The comps table shows 3 years; the fourth only feeds the CAGR.
* `ebitda = ebit + da` (EBITDA = operating income + depreciation & amortization).
* `tangible_book_value = total_equity - preferred - goodwill - intangibles` (tangible *common*
  equity, so P/TBV is the per-common-share ratio; preferred/goodwill/intangibles default 0 when absent).
* `minority_interest`, `preferred`, `goodwill`, `intangibles` default to `0.0` when no tag exists
  (absence means the company has none). Everything else is `NaN` when missing.
* `currency`: 3-letter code of the monetary unit used (normally `"USD"`).
* Diagnostics travel on the frame: `df.attrs["tags_used"]` = `{concept: {period_end: "us-gaap:Tag"}}`
  and `df.attrs["warnings"]` = list of human-readable strings (e.g. TTM fallbacks).
* The TTM row's `fiscal_year` equals the latest FY row's `fiscal_year`; its `period_end` is the latest quarter end.

### Tag fallback (per period, not per company)

For each concept, tags are tried **in order for each fiscal period separately**, and the
first tag with a value for that period wins. Per-period matters: Apple reports
`Revenues` only for old years and `RevenueFromContractWithCustomerExcludingAssessedTax`
for recent years; a company-level choice would produce NaNs.

A tag spec is either `"namespace:Tag"` or a list of such strings meaning "sum the
components that exist (at least one must exist)".

```python
CONCEPT_TAGS = {
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
    "eps_diluted":   ["us-gaap:EarningsPerShareDiluted", "us-gaap:EarningsPerShareBasicAndDiluted",
                      "ifrs-full:DilutedEarningsLossPerShare", "ifrs-full:BasicAndDilutedEarningsLossPerShare"],
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
```

**Total debt** has its own function `_total_debt(...)` because the components overlap.
Convention (decision D1/D2 in DECISIONS.md: operating leases excluded; finance leases
only if embedded in the debt tags):

```
combined          := us-gaap:DebtLongtermAndShorttermCombinedAmount   # already the total
LongTermDebt      := us-gaap:LongTermDebt            # taxonomy definition INCLUDES the current portion
                     else us-gaap:LongTermDebtAndCapitalLeaseObligations (same meaning, incl. finance leases)
noncurrent        := us-gaap:LongTermDebtNoncurrent
current           := us-gaap:DebtCurrent   (already includes short-term borrowings)
                     else us-gaap:LongTermDebtCurrent
short_term        := us-gaap:ShortTermBorrowings, else us-gaap:CommercialPaper, else 0
                     -- NEVER both: CommercialPaper is one kind of ShortTermBorrowings
                     -- only added when `current` did NOT come from DebtCurrent (avoid double count)

if combined exists:          total = combined
elif LongTermDebt exists:    total = LongTermDebt + short_term
elif noncurrent exists:      total = noncurrent + (current or 0) + short_term_if_applicable
elif current exists:         total = current + short_term_if_applicable
elif ifrs-full:Borrowings:   total = Borrowings
elif ifrs LongtermBorrowings/ShorttermBorrowings exist: total = their sum
else:                        NaN -> then, if the period has a balance sheet (cash or equity
                             reported), total_debt = 0 "debt-free" with a warning naming the periods
```

### Fiscal-year selection algorithm

Facts live at `facts["facts"][namespace][Tag]["units"][unit]` as a list of
`{start?, end, val, accn, fy, fp, form, filed, frame?}`. Only non-dimensioned facts are in
the API, so no dimension handling is needed.

1. **Annual duration facts** (income statement / cash flow concepts): keep facts with
   `form in {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}`, both
   `start` and `end`, and duration 350–380 days. Group by `end`.
   * Label: take the fact with the **earliest** `filed` date for that `end` (the first annual
     report that showed the period). Its `fy` is that *filing's* fiscal year; the period is
     labelled `fy − k` where `k` is the number of years the period sits before the filing's own
     (latest) annual period. For an established filer `k = 0` (its own 10-K); for a young
     company whose first 10-K carries three years, the comparatives get `fy−1`, `fy−2`.
     Fallback when `fy` is missing: `end.year`.
   * Value: from the fact with the **latest** `filed` date for that `end` (most recent
     restatement). Ties: last one in the list.
   * The canonical list of fiscal periods = the `n_years` most recent period ends that
     have a **revenue** value. Other concepts are looked up by exact `end`, then ±7 days
     (52/53-week filers).
2. **Instant facts** (balance sheet: cash, debt, minority, preferred, equity, goodwill,
   intangibles): same forms, no `start`; match `end` to the fiscal period end (exact, then
   ±7 days); value from the latest `filed`.
3. **TTM** (trailing twelve months) for flow concepts (revenue, ebit, da, net_income, eps_diluted):
   * `E_fy` = latest FY period end. Quarterly forms = `{"10-Q", "10-Q/A"}`.
     (6-K carries no XBRL, so foreign filers get `TTM = FY` with a warning.)
   * `YTD_cur` = duration fact with `start` within ±7 days of `E_fy + 1 day` and `end > E_fy`;
     choose the one with the latest `end`. Let `d` = its duration in days.
   * `YTD_prior` = duration fact (any form) with `end` within ±10 days of `YTD_cur.end − 1 year`
     and duration within ±10 days of `d`.
   * `TTM = FY_latest + YTD_cur − YTD_prior`  (standard "roll-forward" TTM construction;
     for EPS this is an approximation that ignores share-count drift and is documented as such).
   * **Share-basis check**: if the diluted share count moved outside `[0.8, 1.25]×` between the
     latest 10-K and the latest 10-Q (a stock split: the 10-Q restates its comparatives
     post-split while the FY EPS is pre-split), TTM EPS = TTM net income ÷ latest diluted
     shares instead, with a warning.
   * No `YTD_cur` at all → TTM row = latest FY values, warning `"no 10-Q after latest 10-K; TTM = FY"`.
   * `YTD_cur` present but `YTD_prior` (or `YTD_cur`) missing for a concept → that concept's
     TTM falls back to its FY value and a warning names the concept.
   * TTM balance-sheet columns all come from **one balance sheet**: the latest date after
     `E_fy` (up to `YTD_cur.end`) at which cash or total equity is reported. Every instant
     concept and every debt component is read at that date (±7 days); a concept missing there
     falls back to its latest earlier value with a warning naming the concept.
   * TTM `diluted_shares` = the `YTD_cur`-matching fact if present, else the FY value.
   * TTM `period_end` = `YTD_cur.end`.
4. **Units**: monetary → prefer `"USD"`, else the first key that is a 3-letter uppercase
   code; record it in `currency`. Shares → `"shares"`. EPS → `"USD/shares"` (or `"{ccy}/shares"`).
5. Divide monetary and share values by `MILLION`; leave EPS raw.

### Network, cache, rate limit

* `SEC_USER_AGENT` is read from the environment (after `dotenv.load_dotenv()`); if missing,
  raise `RuntimeError` with instructions. Never fall back to a fake value.
* Endpoints: `https://www.sec.gov/files/company_tickers.json`,
  `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`,
  `https://data.sec.gov/submissions/CIK{cik}.json`.
* One private `_get_json(url)` does every request: sets `User-Agent` and
  `Accept-Encoding: gzip, deflate`, sleeps so requests are ≥ 0.11 s apart (≤ 9/s; SEC cap is 10/s),
  raises `RuntimeError` with the status code on non-200 (403 usually means a bad User-Agent).
* Cache dir = `COMPSAI_CACHE_DIR` env or `compsai.DEFAULT_CACHE_DIR`. Files:
  `company_tickers.json` (TTL 7 d), `companyfacts_CIK{cik}.json` (TTL 1 d),
  `submissions_CIK{cik}.json` (TTL 1 d). Each cache file stores `{"fetched_at": iso, "data": ...}`.
  `refresh=True` bypasses the cache.
* **Offline mode** (`offline=True`, or env `COMPSAI_OFFLINE=1`): no network. Files are read from
  `COMPSAI_FIXTURE_DIR` (default `compsai.DEFAULT_FIXTURE_DIR`): `companyfacts_{TICKER}.json`,
  `submissions_{TICKER}.json`. `get_cik` offline returns the fixture's `cik` (zero-padded);
  `get_company_name` offline returns `entityName`.

### CLI

`python -m compsai.edgar AAPL MSFT GOOGL [--debug] [--offline] [--save-fixture DIR]`
prints one tidy DataFrame per ticker (all columns, no truncation). `--debug` also prints
`attrs["tags_used"]` and `attrs["warnings"]`. `--save-fixture DIR` writes the raw
companyfacts and submissions JSON to `DIR/companyfacts_{TICKER}.json` /
`DIR/submissions_{TICKER}.json` so real data can replace the synthetic fixtures.

## 3. Module 1b: `compsai/market.py`

```python
get_market_data(ticker: str, facts: dict | None = None, refresh: bool = False,
                offline: bool = False, overrides: dict | None = None) -> dict
```

Returns a dict with keys:

```
ticker, price (USD/share), shares_outstanding (millions), currency, market_cap (USD mm),
source ("yfinance" | "yfinance+xbrl" | "xbrl" | "override" | "fixture"), as_of (ISO date)
```

* yfinance: `yf.Ticker(t).fast_info` → `last_price`, `shares`, `currency`; fall back to
  `.info` (`currentPrice`/`regularMarketPrice`, `sharesOutstanding`). Every yfinance call is
  wrapped in `try/except Exception` and logged; never let yfinance crash the pipeline.
* Shares fallback: XBRL `dei:EntityCommonStockSharesOutstanding` from `facts`, latest `end`;
  if several facts share that same `end` and `filed` (multiple share classes), sum them.
  The cover-page total also **replaces** a Yahoo count that is below 90% of it: Yahoo reports
  only the quoted share class (GOOGL ≈ 5.8bn of ≈ 12.1bn), which would halve market cap and EV.
* `overrides` (`{"price": ..., "shares_outstanding": ...}`) win over everything; `source="override"`.
* Missing price → `price = NaN`, `market_cap = NaN` (never invent a price).
* Cache `market_{TICKER}.json`, TTL 12 h. Offline mode reads `market_{TICKER}.json` from the
  fixture dir (`source="fixture"`), and never imports/calls yfinance.

## 4. Module 2: `compsai/valuation.py`

```python
enterprise_value(market_cap, total_debt, cash, minority_interest=0.0, preferred=0.0) -> float
compute_multiples(financials: pd.DataFrame, market: dict, sector_type: str = "industrial") -> pd.Series
summary_stats(peer_df: pd.DataFrame, multiples: list[str] | None = None) -> pd.DataFrame
mask_non_meaningful(peer_df: pd.DataFrame) -> pd.DataFrame
is_meaningful(name: str, value: float) -> bool
exclusion_note(name: str, value: float) -> str
implied_valuation(financials: pd.DataFrame, market: dict, stats: pd.DataFrame,
                  sector_type: str = "industrial") -> pd.DataFrame
```

* `EV = market cap + total debt + minority interest + preferred equity − cash`
  (Rosenbaum & Pearl / Training the Street convention). NaN minority or preferred → 0;
  NaN market cap, debt or cash → NaN EV.
* `compute_multiples` returns a Series with **exactly this index, in this order**:

```
market_cap, ev, ev_revenue_ttm, ev_ebitda_ttm, pe_ttm, ebitda_margin, net_margin,
revenue_growth_1y, revenue_growth_3y_cagr, p_tbv, price, note
```

  * Multiples and margins use the **TTM** row (if there is no TTM row, the latest FY row, with a note).
  * `pe_ttm = price / eps_diluted_ttm`; if EPS is NaN, fall back to `market_cap / net_income_ttm` (note it).
  * `ebitda_margin = ebitda_ttm / revenue_ttm`; `net_margin = net_income_ttm / revenue_ttm`.
  * `revenue_growth_1y = FY[-1] / FY[-2] − 1`; `revenue_growth_3y_cagr = (FY[-1] / FY[-4]) ** (1/3) − 1`
    (NaN if fewer than 4 FY rows). Growth uses fiscal years, not TTM.
  * `p_tbv = market_cap / tangible_book_value` (TTM/latest balance sheet; TBV is tangible
    common equity, i.e. net of preferred, so the ratio is per common share).
  * **Bank branch** (`sector_type == "bank"`): `ev`, `ev_revenue_ttm`, `ev_ebitda_ttm`,
    `ebitda_margin` are NaN and the note says EV-based multiples are not meaningful for banks.
  * `note` is a `"; "`-joined string of every caveat (exclusions, fallbacks); `""` when clean.
* Bounds (a value is meaningful iff finite and `lower < value <= upper`):

```python
MULTIPLE_BOUNDS = {"ev_ebitda_ttm": (0, 100), "pe_ttm": (0, 200), "ev_revenue_ttm": (0, 100), "p_tbv": (0, 20)}
MULTIPLES_BY_SECTOR = {"industrial": ["ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm"], "bank": ["pe_ttm", "p_tbv"]}
```

  Non-meaningful values **stay in the table** with an `exclusion_note` in `note` and are
  masked to NaN only for statistics.
* `summary_stats` returns a DataFrame with index `["mean", "median", "p25", "p75"]` and one
  column per multiple (default: `MULTIPLE_BOUNDS` keys present in `peer_df`). Percentiles use
  pandas' default linear interpolation, which equals Excel `QUARTILE`/`PERCENTILE.INC`.
  If `peer_df` has an `is_target` boolean column, target rows are excluded from the stats.
* `implied_valuation` (football field) returns one row per multiple in
  `MULTIPLES_BY_SECTOR[sector_type]` with columns:

```
method, metric_name, metric_value, low_multiple, high_multiple,
implied_ev_low, implied_ev_high, implied_equity_low, implied_equity_high,
implied_price_low, implied_price_high
```

  using `p25`/`p75`. EV-based: `equity = EV − debt − minority − preferred + cash`,
  `price = equity / shares_outstanding`. P/E: `price = multiple × eps_ttm`, `equity = price × shares`.
  P/TBV: `equity = multiple × TBV`.

## 5. Shared records: `compsai/models.py`

```python
@dataclass
class CompanyData:
    ticker: str
    name: str
    financials: pd.DataFrame       # extract_financials output
    market: dict                   # get_market_data output
    multiples: pd.Series           # compute_multiples output
    is_target: bool = False

@dataclass
class CommentaryResult:
    ticker: str
    normalization_items: list[dict] = field(default_factory=list)
    # each: {description, amount_usd_m, fiscal_year, direction, source_quote, verified: bool, quote_word_count: int}
    premium_discount: dict = field(default_factory=dict)
    # {growth_outlook, margin_trajectory, key_risks: [...], premium_or_discount, rationale}
    source_form: str = ""
    source_url: str = ""
    filing_date: str = ""
    model: str = ""
    errors: list[str] = field(default_factory=list)

@dataclass
class PipelineResult:
    peer_set: str
    sector_type: str
    target: str | None
    companies: list[CompanyData]
    comps: pd.DataFrame            # index = ticker; columns = name, is_target, + compute_multiples index
    stats: pd.DataFrame            # summary_stats output
    football_field: pd.DataFrame | None
    commentary: dict[str, CommentaryResult]
    xlsx_path: Path | None
    warnings: list[str]
```

## 6. Module 3: `compsai/excel.py`

```python
write_comps_workbook(peer_set: str, companies: list[CompanyData], sector_type: str = "industrial",
                     target: str | None = None, commentary: dict[str, CommentaryResult] | None = None,
                     out_dir: Path | None = None, as_of: date | None = None) -> Path
```

Writes `output/comps_{peer_set}_{YYYY-MM-DD}.xlsx` (out_dir defaults to `compsai.OUTPUT_DIR`).
Font Arial 10 everywhere. **Blue font (`0000FF`) for every hardcoded input, black for every formula.**
Number formats: `$#,##0` for $mm, `$#,##0.00` for per-share, `0.0"x"` for multiples, `0.0%` for percentages.

### Sheet "Inputs" (every number on Comps traces here)

Row 1 title. Company block `i` (0-based) starts at `b = 3 + 9*i`:

```
b   : A=ticker (bold)  B=company name
b+1 : A="Price ($)"  B=price[blue]  C="Shares out (mm)"  D=shares[blue]  E="Currency" F=ccy  G="Price as of" H=as_of
b+2 : headers: A Period | B Fiscal year | C Period end | D Revenue | E EBIT | F D&A | G EBITDA | H Net income |
      I Diluted EPS | J Total debt | K Cash | L Minority interest | M Preferred | N Diluted shares (mm) |
      O Total equity | P Goodwill | Q Intangibles | R Tangible book value
b+3..b+6 : FY rows, oldest at b+3 ... NEWEST ALWAYS at b+6 (bottom-aligned; missing years left blank)
b+7 : TTM row
b+8 : blank
```

Hardcodes (blue): D,E,F,H,I,J,K,L,M,N,O,P,Q. Formulas (black): `G = IF(AND(ISNUMBER(E),ISNUMBER(F)),E+F,"")`
(EBITDA, blank when a component is missing — mirrors NaN in Python), `R = IF(ISNUMBER(O),O−M−P−Q,"")`
(tangible common equity).

### Sheet "Comps"

Row 1 title `Comparable Companies Analysis — {peer_set}`; row 2 `As of {date} · $ in millions except per share · Source: SEC EDGAR XBRL, Yahoo Finance`.
Row 4 headers; company rows start at row 5 (one per company, target first if given, marked `(target)` in the Notes).
Columns depend on sector type (look columns up by header text in tests; excel.py exports `COLUMN_SPECS`):

* industrial: Company | Ticker | Price | Market Cap | Enterprise Value | EV / Revenue (TTM) | EV / EBITDA (TTM) | P / E (TTM) | EBITDA Margin | Net Margin | Revenue Growth (1Y) | Revenue CAGR (3Y) | Notes
* bank: Company | Ticker | Price | Market Cap | P / E (TTM) | P / TBV | Net Margin | Revenue Growth (1Y) | Revenue CAGR (3Y) | Notes

Every numeric cell is a **formula into Inputs** (company block start `b`, TTM row `t = b+7`, newest FY `y = b+6`, prior FY `b+5`, oldest FY `b+3`):

Root inputs are ISNUMBER-guarded so a blank cell yields "n/a" (Excel would otherwise read it as
0), exactly where valuation.py yields NaN:

```
Price          = IF(ISNUMBER(B),B,"n/a")                                   with B = Inputs!$B${b+1}
Market Cap     = IF(AND(ISNUMBER(B),ISNUMBER(D)),B*D,"n/a")                  D = Inputs!$D${b+1}
EV             = IF(AND(ISNUMBER(B),ISNUMBER(D),ISNUMBER(J),ISNUMBER(K)),
                    B*D + J + Inputs!$L$t + Inputs!$M$t - K, "n/a")           J/K = Inputs!$J$t / $K$t
EV/Revenue     = IFERROR({EV}/Inputs!$D$t,"n/a")
EV/EBITDA      = IFERROR({EV}/Inputs!$G$t,"n/a")
P/E            = IFERROR(IF(ISNUMBER(Inputs!$I$t),{Price}/Inputs!$I$t,{MarketCap}/Inputs!$H$t),"n/a")
                 (no diluted EPS -> market cap / net income, the valuation.py fallback)
P/TBV          = IFERROR({MarketCap}/Inputs!$R$t,"n/a")
EBITDA margin  = IFERROR(Inputs!$G$t/Inputs!$D$t,"n/a")
Net margin     = IFERROR(Inputs!$H$t/Inputs!$D$t,"n/a")
Growth 1Y      = IFERROR(Inputs!$D$y/Inputs!$D${b+5}-1,"n/a")
CAGR 3Y        = IFERROR((Inputs!$D$y/Inputs!$D${b+3})^(1/3)-1,"n/a")
Notes          = plain text from multiples["note"] (+ "(target — excluded from peer stats)")
```

Below the companies: one blank row, then stats rows labelled `Mean`, `Median`, `25th Percentile`,
`75th Percentile`, each an Excel formula (`AVERAGE`, `MEDIAN`, `QUARTILE(range,1)`, `QUARTILE(range,3)`)
over **helper columns** placed to the right of Notes (header `<multiple> (stat-eligible)`, grey italic,
narrow). Helper cell per multiple mirrors `MULTIPLE_BOUNDS`, e.g. for EV/EBITDA in column G row r:
`=IF(AND(ISNUMBER(G5),G5>0,G5<=100),G5,"")`. A target company's helper cells are `""`.
Stats are computed only for the sector's multiples (`MULTIPLES_BY_SECTOR`). Freeze panes below the header.

### Sheet "Football Field" (only when `target` is given)

Table (formulas, one row per multiple in `MULTIPLES_BY_SECTOR[sector]`):
`Method | Target metric | Metric value | Low multiple (25th) | High multiple (75th) | Implied EV low | Implied EV high | Implied equity low | Implied equity high | Implied price low | Implied price high`,
where low/high multiples link to the Comps stats cells and metric values link to Inputs
(ISNUMBER-guarded; the P/E row switches to net income when diluted EPS is blank, like
`implied_valuation`). Also `Current price` linked to Inputs. Chart: horizontal stacked `BarChart`
(`type="bar"`, `grouping="stacked"`, `overlap=100`): series 1 = a helper `MAX(low,0)` with
`graphicalProperties.noFill = True`, series 2 = a helper `MAX(high,0) − MAX(low,0)` (a stacked bar
cannot start below the axis, so a negative implied price draws nothing), categories = method names,
title `Implied share price ({ticker})`.

### Sheet "Commentary"

One block per company: header row `{ticker} — {name} — {premium_or_discount}`, then label/value rows
`Growth outlook`, `Margin trajectory`, `Key risks` (one per line), `Rationale`, then a table
`Description | Amount ($mm) | Fiscal year | Direction | Source quote | Verified in filing`,
then `Source: {form} filed {filing_date} — {url}`. If a company has no commentary, write one line:
`Commentary not generated: {reason}`. Wrap text; column widths set.

## 7. Module 4: `compsai/ai_commentary.py`

```python
SYSTEM_PROMPT: str                      # exactly the four lines below (from the brief's prompt-design notes)
DEFAULT_MODEL = "claude-sonnet-4-6"     # override with env COMPSAI_MODEL
MAX_CHARS = 350_000                     # ≈ 85–90k tokens at ~4 chars/token, under the 100k/call target
MAX_CHUNKS = 2

get_latest_annual_filing(submissions: dict, cik: str, page_loader=None) -> FilingRef   # 10-K first, then 20-F/40-F;
                                        # pages into filings.files[] when `recent` (~1,000 filings) holds none
download_filing_text(ref: FilingRef, refresh: bool = False) -> str    # cached data/cache/filings/{accession}.txt
html_to_text(html: str) -> str
extract_section(text: str, item: str) -> str                          # item in {"1A", "7"}; "" if not found
chunk_text(text: str, max_chars: int = MAX_CHARS) -> list[str]
load_prompt(name: str) -> string.Template                             # compsai/prompts/{name}.md, $placeholders
call_claude(user_prompt: str, *, system: str = SYSTEM_PROMPT, model: str | None = None,
            max_tokens: int = 4096, client=None) -> str
parse_json_response(text: str) -> dict | None
normalize_items(section_text: str, ticker: str, fiscal_year: int, client=None) -> list[dict]
verify_quote(quote: str, source_text: str) -> bool
premium_discount(section_text: str, ticker: str, multiples: pd.Series, peer_median: pd.Series,
                 sector_type: str, client=None) -> dict
generate_commentary(company: CompanyData, peer_median: pd.Series, sector_type: str,
                    client=None, refresh: bool = False, offline: bool = False) -> CommentaryResult
```

* `SYSTEM_PROMPT` is exactly:
  `You are a sell-side equity research associate. You are given a section of a 10-K.` /
  `Return only valid JSON matching the schema below. No preamble, no markdown.` /
  `If you cannot find relevant information, return an empty list or "unknown".` /
  `Never invent numbers. Every item must include a short verbatim source_quote.`
* `FilingRef` dataclass: `form, accession (no dashes), primary_document, filing_date, report_date, url`
  with `url = https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_document}`.
* `download_filing_text` decodes the response **bytes** through BeautifulSoup (EDGAR sends no
  charset header; `requests.text` would default to latin-1 and mangle curly quotes).
* `html_to_text`: BeautifulSoup `html.parser`; drop `script`, `style`, and inline-XBRL `ix:header`;
  `get_text("\n")`; normalise `\xa0` → space; collapse runs of spaces; collapse 3+ newlines to 2.
* `extract_section`: case-insensitive regexes tolerant of `Item 7.`, `ITEM 7 –`, `Item 7:`, smart quotes,
  and the combined `Item 7 and 7A.` heading. End headings are tried in order — titled first
  (`Item 1B ... Unresolved`, `Item 2 ... Properties`; `Item 7A ... Quantitative`, `Item 8 ... Financial
  Statements`), then the bare `Item 1B`/`Item 2`/`Item 7A`/`Item 8` — so a hyperlinked
  cross-reference ("see Item 7A" on its own line) cannot end the section early.
  Among all start matches, choose the one whose body (to the next end match) is **longest** —
  this skips the table-of-contents entry.
* Claude calls go through `call_claude` only: `client.messages.create(model=..., max_tokens=...,
  system=system, messages=[{"role": "user", "content": user_prompt}])`; concatenate `block.text`
  for blocks with `type == "text"`. No `temperature` (removed in SDK 1.x), no assistant prefill.
  `client=None` → `anthropic.Anthropic()` (reads `ANTHROPIC_API_KEY` after `load_dotenv()`).
  Catch `anthropic.APIStatusError` / `anthropic.APIConnectionError` → log → raise `CommentaryError`.
* `parse_json_response`: strip ``` fences, take the substring from the first `{` to the last `}`,
  `json.loads`; on any failure log a warning and return `None`. Never raise.
* Prompt files are `string.Template` markdown with `$ticker`, `$fiscal_year`, `$section_text`,
  `$multiples_table`, `$sector_type` placeholders; the schema is in the user message; the 10-K
  text follows the delimiter line `===== 10-K SECTION TEXT BEGINS =====`.
* `normalize_items`: run on up to `MAX_CHUNKS` chunks of the section (log when truncating);
  validate every item (`description` str, `amount_usd_m` number or None, `fiscal_year` int or None,
  `direction` in `{"add_back", "deduct"}`, `source_quote` str); add `quote_word_count` (quotes of
  15+ words are kept but counted into `CommentaryResult.errors`); merge chunks; de-duplicate on
  (first 60 lowercase characters of `description`, `fiscal_year`).
* `verify_quote`: normalise both sides (lowercase, unify curly quotes/dashes, drop soft hyphens,
  remove **all** whitespace — inline XBRL puts every tagged number on its own line — strip
  surrounding punctuation) and test substring containment.
* `premium_discount`: build a small text table of the company's multiples vs. the peer median;
  return dict with keys `growth_outlook, margin_trajectory, key_risks (list), premium_or_discount
  (premium|discount|inline|unknown), rationale`; missing fields → `"unknown"` / `[]`.
* `generate_commentary` never raises: it collects failures into `CommentaryResult.errors`.
  Normalization runs on Item 7 (fallback: first `MAX_CHARS` of the whole filing); premium/discount runs on
  Item 7 followed by Item 1A within `MAX_CHARS` — when they do not fit, MD&A keeps at most 70% of the
  budget so Risk Factors is never dropped, and the truncation is recorded in `errors`. Offline mode reads `filing_{TICKER}.html` from
  the fixture dir instead of downloading.

## 8. `compsai/pipeline.py`

```python
@dataclass
class PeerSet: name: str; tickers: list[str]; sector_type: str = "industrial"

load_peer_sets(path: Path | None = None) -> dict[str, PeerSet]
run_pipeline(tickers: list[str], peer_set_name: str = "custom", target: str | None = None,
             sector_type: str = "industrial", with_commentary: bool = True,
             progress: Callable[[str, str], None] | None = None, offline: bool = False,
             price_overrides: dict[str, dict] | None = None, out_dir: Path | None = None,
             write_excel: bool = True) -> PipelineResult
```

* Stages, reported through `progress(stage, message)`: `"edgar"`, `"market"`, `"valuation"`,
  `"commentary"`, `"excel"`, `"done"`.
* If `target` is not in `tickers` it is prepended. The target row is in the comps table with
  `is_target=True` and is excluded from `summary_stats` and from the Excel helper columns.
* A single company failing (network, missing data) is logged, added to `warnings`, and skipped;
  if no company succeeds, raise `RuntimeError`.
* Commentary runs only when `with_commentary` and `ANTHROPIC_API_KEY` is set (otherwise a warning);
  in offline mode, commentary comes from `commentary_{TICKER}.json` fixtures when present. Companies
  the stage could not cover get an error-only `CommentaryResult` naming the reason, so the
  Commentary sheet states it. A "no debt tag found; treated as debt-free" caveat from edgar.py is
  appended to the company's `note`.
* CLI: `python -m compsai.pipeline --peer-set us_large_software --target MSFT
  [--tickers A,B,C] [--sector-type bank] [--no-ai] [--offline] [--out DIR]`.

## 9. `app/streamlit_app.py`

Sidebar: peer set (from `config/peer_sets.yaml`) or custom tickers; target ticker; sector type;
checkbox "Generate AI commentary"; checkbox "Offline demo (bundled fixtures)". Run button →
per-stage status via `st.status`. Output: comps table (`st.dataframe`, formatted), stats table,
football-field range chart (Altair `mark_bar` with `x`/`x2`), commentary expanders per company,
`st.download_button` for the xlsx bytes. Reads `.env` via `dotenv` and falls back to
`st.secrets` for `ANTHROPIC_API_KEY` / `SEC_USER_AGENT` (Streamlit Community Cloud).

## 10. Test fixtures (offline, deterministic)

`tests/fixtures/make_fixtures.py` generates every fixture deterministically (re-run to regenerate).
The companyfacts fixtures are **synthetic but schema-exact** copies of the EDGAR
`companyfacts` shape (they were authored without network access; `python -m compsai.edgar
AAPL --save-fixture tests/fixtures` swaps in real data). Fictional tickers make that explicit:

* `FIXA` "Fixture Fruit Inc." (CIK 1): FYE last Saturday of September (52/53-week: 2021-09-25,
  2022-09-24, 2023-09-30, 2024-09-28); `Revenues` tag only for FY2021, then
  `RevenueFromContractWithCustomerExcludingAssessedTax`; debt = `LongTermDebtNoncurrent` +
  `LongTermDebtCurrent` + `CommercialPaper`; each 10-K restates prior years (3 years of income
  statement, 2 of balance sheet); FY2025 10-Qs for Q1–Q3 with 3-month **and** YTD facts plus
  prior-year comparatives; one `10-K/A`; `dei:EntityCommonStockSharesOutstanding`.
* `FIXB` "Fixture Software Corp." (CIK 2): FYE June 30; D&A via `DepreciationAmortizationAndOther`;
  debt via `LongTermDebt`; `MinorityInterest` present; latest filing is the 10-K (no later 10-Q) → TTM = FY.
* `FIXC` "Fixture Bancorp" (CIK 3): FYE Dec 31; `Revenues`; no operating income / D&A (EBITDA NaN);
  `StockholdersEquity`, `Goodwill`, `IntangibleAssetsNetExcludingGoodwill`, `PreferredStockValue` > 0;
  `LongTermDebt` + `ShortTermBorrowings`; one 10-Q (Q1) after the 10-K.
* `market_FIXA.json` etc.: `{"ticker","price","shares_outstanding","currency","as_of"}`.
* `submissions_FIXA.json`, `filing_FIXA.html` (a small synthetic 10-K with a table of contents,
  Items 1A, 1B, 7, 7A, 8), `commentary_FIXA.json` (a `CommentaryResult` as JSON).

`tests/conftest.py` sets `COMPSAI_OFFLINE=1`, `COMPSAI_FIXTURE_DIR`, a temporary
`COMPSAI_CACHE_DIR`, and `SEC_USER_AGENT=CompsAI test@example.com` for every test.

## 11. Coding standards

* Python 3.11, type hints, module docstrings that say what and why in plain English.
* Every finance formula carries a comment naming the convention it follows.
* `logging.getLogger("compsai")`; no `print` outside CLIs.
* Readable over clever: an interviewer should be able to follow `valuation.py` line by line.
