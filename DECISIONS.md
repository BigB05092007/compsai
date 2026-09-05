# Finance-judgment decisions (for Brett to confirm)

The project brief says: *"Ask Brett before making a finance-judgment decision. Do not
silently pick a convention."* Brett was not available while the code was built, so every
such decision was made with a documented default and is listed here as a question. Each
entry names the current default, why it was chosen, and exactly where it lives so it can
be changed in one place. Nothing here is hidden in the code.

Legend: **Default** = what the code does today · **Where** = file and function.

---

## D1. What counts as "total debt" in the EV bridge?

**Default.** `DebtLongtermAndShorttermCombinedAmount` when a filer tags the total; else
`LongTermDebt` (the taxonomy element *includes* the current portion; or
`LongTermDebtAndCapitalLeaseObligations`) plus one short-term piece; if neither is tagged:
`LongTermDebtNoncurrent` + (`DebtCurrent`, else `LongTermDebtCurrent`) + the short-term piece —
added only when the current piece did *not* come from `DebtCurrent`, because `DebtCurrent`
already contains it. The short-term piece is `ShortTermBorrowings` if tagged, else
`CommercialPaper`, never both (in the taxonomy commercial paper is one kind of short-term
borrowing). IFRS fallbacks: `Borrowings`, else `LongtermBorrowings` + `ShorttermBorrowings`.
A period with a balance sheet but no debt element under any of these tags is treated as
**debt-free** (`total_debt = 0`) with a warning and a note in the comps table, so a genuinely
unlevered company still gets an EV; debt tagged under an element the code does not read would
be missed, which is why the note says "verify".
**Why.** The brief's tag list (`LongTermDebt`, `LongTermDebtNoncurrent + LongTermDebtCurrent`,
`DebtCurrent`) omits commercial paper, which is a large part of Apple's and Microsoft's debt;
excluding it would understate EV. The rule above never double-counts.
**Where.** `compsai/edgar.py::_total_debt` (the decision table is in its docstring).
**Question.** Keep this composition, or use the brief's three tags literally?

## D2. Leases

**Default.** Operating lease liabilities (`OperatingLeaseLiability*`) are **excluded** from
debt. Finance lease liabilities are excluded too unless the company folds them into one of
the debt tags above.
**Why.** Post-ASC 842 most trading-comps templates keep operating leases out of EV unless
every peer is treated on an EBITDAR basis; mixing conventions across a peer set is worse
than either choice. Retail, airline and restaurant peer sets are where this matters.
**Where.** `compsai/edgar.py::_total_debt` (no lease tags are read).
**Question.** Exclude (current), include finance leases only, or include both?

## D3. Cash = cash and cash equivalents only

**Default.** `CashAndCashEquivalentsAtCarryingValue` (fallbacks: the restricted-cash total,
IFRS `CashAndCashEquivalents`). Short-term marketable securities are **not** subtracted.
**Why.** This is what the brief specified. Apple and Microsoft hold most liquidity in
marketable securities, so their EV is higher here than on Bloomberg/CapIQ, which net them.
**Where.** `compsai/edgar.py::CONCEPT_TAGS["cash"]`.
**Question.** Add `ShortTermInvestments` / `MarketableSecuritiesCurrent` to cash?

## D4. Minority interest and preferred equity default to zero

**Default.** If a company tags neither `MinorityInterest` nor `PreferredStockValue`, the
value is 0 (absence means "none"), and EV is still computed. Missing debt or cash, by
contrast, makes EV NaN — never guessed.
**Where.** `compsai/edgar.py::ZERO_WHEN_ABSENT`; `compsai/valuation.py::enterprise_value`.

## D5. How TTM is built

**Default.** `TTM = latest fiscal year + year-to-date (current) − year-to-date (prior year)`
from the most recent 10-Q, for revenue, EBIT, D&A, net income and diluted EPS. Balance-sheet
items use the latest reported balance sheet. If no 10-Q follows the latest 10-K (or the
filer only files 6-Ks), TTM = FY and a warning is recorded.
**Caveat.** Applying the roll-forward to *EPS* ignores share-count drift between periods.
The alternative is TTM net income ÷ latest diluted share count — which the code switches to
automatically (with a warning) when the diluted share count moved outside 0.8–1.25× between the
latest 10-K and 10-Q, i.e. after a stock split, where the roll-forward would be wrong by the
split ratio (NVDA, AVGO, WMT, CMG in 2024).
All TTM balance-sheet items are read at one balance-sheet date (the latest reported); a
component missing there falls back to its latest earlier value with a warning.
**Where.** `compsai/edgar.py::_ytd_period`, `_ytd_facts`, `extract_financials`.
**Question.** Keep the EPS roll-forward (simple, common) or switch to NI ÷ diluted shares?

## D6. P/E definition

**Default.** `P/E = share price ÷ diluted EPS (TTM)`. If EPS is missing,
`market cap ÷ net income (TTM)` with a note in the table.
**Where.** `compsai/valuation.py::compute_multiples`.

## D7. Outlier exclusion bounds

**Default.** A multiple is excluded from the mean/median/percentiles (but kept in the
table with a note) unless `lower < value ≤ upper`:
EV/EBITDA (0, 100], P/E (0, 200], EV/Revenue (0, 100], P/TBV (0, 20].
**Why.** The first two come from the brief; EV/Revenue and P/TBV bounds were added so a
broken input cannot distort the statistics. 20x P/TBV is loose (banks rarely exceed ~4x).
**Where.** `compsai/valuation.py::MULTIPLE_BOUNDS`; mirrored in the Excel helper columns
by `compsai/excel.py::_helper_formula`.
**Question.** Tighten P/TBV (e.g. 10x)? Add bounds for margins?

## D8. Four fiscal years are pulled so the 3-year CAGR is real

**Default.** `extract_financials(n_years=4)`. The table shows the last three fiscal years;
the fourth only serves as the base of `(FY_t / FY_t-3)^(1/3) − 1`. With fewer than four
years the CAGR is NaN rather than silently becoming a 2-year CAGR.
**Where.** `compsai/edgar.py::extract_financials`; `compsai/valuation.py::_revenue_growth`.

## D9. Banks: P/TBV was implemented before sign-off

The brief said *"Ask Brett before implementing P/TBV."* It is implemented so that the
`us_banks` peer set produces a usable sheet; it is easy to remove.
**Default.** `P/TBV = market cap ÷ (total equity − preferred − goodwill − intangibles)`, i.e.
tangible **common** equity, because the numerator (price × common shares) belongs to common
holders only — every name in `us_banks` carries $10–30bn of preferred, so leaving it in the
denominator would understate the ratio by 5–10%. Bank peer sets show P/E and P/TBV; EV,
EV/Revenue, EV/EBITDA and EBITDA margin are blank with a note because debt is a bank's raw
material, not a financing choice.
**Data caveats for real banks.** Cash is usually tagged `CashAndDueFromBanks` (not in the
cash fallback list — harmless because EV is not used for banks), and several banks tag
revenue only as `InterestAndDividendIncomeOperating` + `NoninterestIncome` or
`RevenuesNetOfInterestExpense`, which are not in the revenue list yet; expect NaN revenue
for some of JPM/BAC/WFC/C/GS/MS until those tags are added.
**Where.** `compsai/valuation.py::compute_multiples` (bank branch), `MULTIPLES_BY_SECTOR`;
`compsai/edgar.py::CONCEPT_TAGS` (`total_equity`, `goodwill`, `intangibles`).
**Questions.** Keep P/TBV? Relabel the column "P / TCE"? Add the bank revenue/cash tags?

## D10. The target is excluded from its own peer statistics

**Default.** The target row is shown first in the table and on the football field, but it
is left out of mean/median/p25/p75 (in Python and in the Excel helper columns).
**Where.** `compsai/valuation.py::summary_stats`; `compsai/excel.py::_write_comps`.

## D11. Percentiles and the football-field range

**Default.** Percentiles use linear interpolation — pandas' default, identical to Excel's
`QUARTILE` / `PERCENTILE.INC` — so the Python and Excel numbers agree. The football field
uses the 25th–75th percentile multiples (the brief's choice); some groups use min–max.
**Where.** `compsai/valuation.py::summary_stats`, `implied_valuation`.

## D12. Market cap uses basic shares outstanding

**Default.** Price × shares outstanding from Yahoo Finance (`fast_info.shares`, else
`impliedSharesOutstanding`), falling back to the 10-K/10-Q cover-page count
(`dei:EntityCommonStockSharesOutstanding`, summing share classes reported on the same date).
Because Yahoo's count covers only the quoted share class, a Yahoo figure below 90% of the
cover-page total is replaced by that total (GOOGL: 5.8bn Class A vs 12.1bn all classes).
Fully diluted shares via the treasury-stock method are **not** computed, while P/E uses
*diluted* EPS. This is a documented simplification.
**Where.** `compsai/market.py::get_market_data`, `_shares_from_xbrl`.
**Question.** Accept basic shares, or add a treasury-stock-method dilution step?

## D13. Revenue tag order is applied per period

**Default.** For every fiscal period the tags are tried in the brief's order (`Revenues`,
then `RevenueFromContractWithCustomerExcludingAssessedTax`, then `SalesRevenueNet`, then
IFRS `Revenue`) and the first with a value wins. Applying the order *per period* (not per
company) is what makes Apple work: it used `Revenues` only for old years.
**Caveat.** If a company restated an old year only under the newer tag, the older
unrestated `Revenues` value wins.
**Where.** `compsai/edgar.py::_first_available`, `CONCEPT_TAGS["revenue"]`.

## D14. EBITDA is NaN when D&A is not tagged

**Default.** `EBITDA = operating income + D&A`; if no D&A tag exists for the period the
EBITDA is NaN and EV/EBITDA is excluded with a note (rather than silently using EBIT).
D&A fallbacks include Microsoft's `DepreciationAmortizationAndOther` and the sum of
`Depreciation` + `AmortizationOfIntangibleAssets`.
**Where.** `compsai/edgar.py::CONCEPT_TAGS["da"]`, `extract_financials`.

## D15. Foreign private issuers (20-F / 40-F, IFRS) are best effort

**Default.** Annual facts are accepted from 10-K, 10-K/A, 10-KT, 20-F, 40-F and their
amendments; IFRS tags are
last-resort fallbacks for each concept; 6-K filings carry no XBRL so TTM = FY. Values in a
non-USD currency are reported as-is with the currency recorded in the table — multiples
still work (they are ratios) but market cap uses the Yahoo price currency, so cross-currency
peer sets need care. The `canadian_cross_listed` peer set is shipped but was not verified
against live data.
**Where.** `compsai/edgar.py::ANNUAL_FORMS`, `_detect_currency`.

## D16. AI commentary model and prompt design

**Default.** `claude-sonnet-4-6` (the brief's choice; override with `COMPSAI_MODEL`),
JSON-only prompts with the schema in the user message and the filing text after a
delimiter, at most ~350k characters (≈85–90k tokens) per call and two chunks per section.
Every `source_quote` is mechanically checked against the filing; unverifiable quotes are
flagged, not dropped.
**Where.** `compsai/ai_commentary.py`, `compsai/prompts/*.md`.

## D17. Excel statistics formulas are bare `MEDIAN` / `QUARTILE`

**Default.** As the brief requires. If *every* peer is ineligible for a multiple, Excel
shows `#NUM!` for that statistic (the Python side shows NaN). Every root input on the Comps
sheet (price, shares, debt, cash, EPS) is `ISNUMBER`-guarded so a blank cell shows "n/a"
exactly where Python shows NaN — Excel would otherwise read a blank as 0 and quietly include
a debt-free or unpriced company in the statistics.
**Where.** `compsai/excel.py::_write_comps`.
**Question.** Wrap them in `IFERROR(..., "n/a")`?

## D18. Test fixtures are synthetic

The build sandbox had no access to sec.gov, so `tests/fixtures/companyfacts_FIX*.json` are
**synthetic but schema-exact** EDGAR documents for three fictional companies (an
Apple-like 52/53-week filer, a Microsoft-like June filer, a bank). Replace them with real
data at any time:

```bash
python -m compsai.edgar AAPL MSFT JPM --save-fixture tests/fixtures
```

and adjust the hand-checked numbers in `tests/test_edgar.py`.

## D19. Share price = last trade, not strictly "last close"

**Default.** `yfinance` `fast_info.last_price` (fallback `currentPrice` / `regularMarketPrice`).
Outside market hours this *is* the last close; during the session it is the latest trade, so
two runs on the same day can differ slightly. The `as_of` date is recorded on the Inputs sheet.
**Alternative.** `previous_close`, which is reproducible intraday but one day stale after the
close.
**Where.** `compsai/market.py::_fetch_yfinance`.

## D20. Ownership of `valuation.py` and the Streamlit app

The brief reserved `valuation.py` for Brett (Claude Code to review) and the Streamlit app
for Brett alone. Both were written here because the project was requested end to end and
Modules 3–4 cannot run without Module 2. They are written to be read line by line and can
be rewritten or replaced; the tests in `tests/test_valuation.py` (including the
hand-calculated case) are the contract any rewrite has to satisfy.
