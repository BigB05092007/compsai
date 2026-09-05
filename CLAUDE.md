# CompsAI: AI-Assisted Comparable Company Analysis

Project instruction file for Claude Code. Read this fully before writing any code.

> This is the original project brief the repository was built against, kept verbatim.
> Two companion documents refine it: `docs/DESIGN.md` (the interface contract every module
> and test follows) and `DECISIONS.md` (the finance-judgment defaults the brief says to
> confirm with Brett). Where this brief and DESIGN.md differ, DESIGN.md is the more specific
> statement of what was built.

## 1. Objective

Build an open-source Python tool that:

1. Pulls public company financials from SEC EDGAR (free XBRL API).
2. Computes trading multiples and operating metrics for a peer set.
3. Writes a banker-formatted Excel comps sheet with live formulas.
4. Uses the Anthropic API to read each company's 10-K and draft valuation commentary (non-recurring items, guidance, premium/discount rationale).
5. Exposes everything through a small Streamlit app.

Owner: Brett (second-year Commerce, Queen's University). Purpose: a portfolio project for investment banking and finance-AI recruiting. Brett must be able to explain every line of the valuation logic in an interview, so keep code readable and commented over clever.

## 2. Ground rules for Claude Code

- Language: Python 3.11+. Use `uv` or `pip` with a `requirements.txt`.
- No paid data sources. SEC EDGAR and yfinance only.
- No scraping HTML unless the XBRL API does not cover the field. Prefer structured APIs.
- Every module gets a docstring at the top explaining what it does and why in plain English.
- Every finance formula gets a comment citing the convention (e.g. "EV = market cap + debt + minority interest + preferred - cash, per Training the Street").
- Write tests for the valuation engine. Use pytest.
- Do not hardcode API keys. Read `ANTHROPIC_API_KEY` from `.env`.
- Commit after each completed module with a clear message.
- Ask Brett before making a finance-judgment decision (e.g. how to treat operating leases). Do not silently pick a convention.
- Build in the order given in Section 5. Do not skip ahead to the Streamlit app.

## 3. Repository structure

```
compsai/
├── CLAUDE.md                  # this file
├── README.md                  # user-facing docs, written last
├── requirements.txt
├── .env.example
├── .gitignore
├── config/
│   └── peer_sets.yaml         # named peer groups, e.g. "us_banks", "canadian_software"
├── compsai/
│   ├── __init__.py
│   ├── edgar.py               # Module 1: SEC data pull
│   ├── market.py              # Module 1b: share price and shares outstanding
│   ├── valuation.py           # Module 2: EV, multiples, margins, growth
│   ├── excel.py               # Module 3: openpyxl comps sheet
│   ├── ai_commentary.py       # Module 4: Claude-powered 10-K analysis
│   ├── prompts/
│   │   ├── normalize.md       # prompt: identify non-recurring items
│   │   └── premium_discount.md# prompt: why this company trades where it does
│   └── pipeline.py            # orchestrates 1 to 4 end to end
├── app/
│   └── streamlit_app.py       # Module 5
├── tests/
│   ├── test_valuation.py
│   └── fixtures/              # saved EDGAR JSON so tests run offline
├── output/                    # generated xlsx files, gitignored
└── data/cache/                # cached EDGAR responses, gitignored
```

## 4. Data sources

### SEC EDGAR
- Ticker to CIK map: `https://www.sec.gov/files/company_tickers.json`
- Company facts (all XBRL): `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`
- Filing index (to find the 10-K document): `https://data.sec.gov/submissions/CIK##########.json`
- SEC requires a `User-Agent` header of the form `"CompsAI brett@example.com"`. Read it from `.env` as `SEC_USER_AGENT`.
- Rate limit: max 10 requests per second. Add a simple sleep and a local JSON cache in `data/cache/`.

### Market data
- Use `yfinance` for last close price and shares outstanding. Fall back to XBRL `dei:EntityCommonStockSharesOutstanding` for shares if yfinance fails.

### XBRL tags to pull (US GAAP)
Handle the fact that companies use different tags for the same concept. Try tags in order and take the first that exists.

| Concept | Tags to try, in order |
|---|---|
| Revenue | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `SalesRevenueNet` |
| Operating income | `OperatingIncomeLoss` |
| D&A | `DepreciationDepletionAndAmortization`, `DepreciationAndAmortization` |
| Net income | `NetIncomeLoss` |
| Diluted EPS | `EarningsPerShareDiluted` |
| Total debt | `LongTermDebt`, `LongTermDebtNoncurrent` + `LongTermDebtCurrent`, `DebtCurrent` |
| Cash | `CashAndCashEquivalentsAtCarryingValue` |
| Minority interest | `MinorityInterest` |
| Preferred equity | `PreferredStockValue` |
| Diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` |

Pull the last 3 fiscal years (10-K, `form == "10-K"`) plus trailing twelve months built from the last 4 quarters (10-Q + 10-K). Store both.

## 5. Build order and acceptance criteria

### Module 1: `edgar.py` and `market.py`
Functions:
- `get_cik(ticker: str) -> str`
- `get_company_facts(cik: str) -> dict` (cached)
- `extract_financials(facts: dict) -> pd.DataFrame` with columns: `fiscal_year, period_type (FY|TTM), revenue, ebit, da, ebitda, net_income, eps_diluted, total_debt, cash, minority_interest, preferred, diluted_shares`
- `get_market_data(ticker) -> dict` with `price, shares_outstanding, currency`

Done when: `python -m compsai.edgar AAPL MSFT GOOGL` prints a tidy DataFrame for each with no NaN in revenue, EBITDA, net income for the last 3 fiscal years.

### Module 2: `valuation.py` (Brett writes this, Claude Code reviews)
Functions:
- `enterprise_value(market_cap, total_debt, cash, minority_interest, preferred) -> float`
- `compute_multiples(financials: pd.DataFrame, market: dict) -> pd.Series` returning: `market_cap, ev, ev_revenue_ttm, ev_ebitda_ttm, pe_ttm, ebitda_margin, net_margin, revenue_growth_1y, revenue_growth_3y_cagr`
- `summary_stats(peer_df) -> pd.DataFrame` with mean, median, 25th, 75th percentile per multiple
- Flag and exclude negative or absurd multiples (EV/EBITDA < 0 or > 100, P/E < 0 or > 200) from the stats, but keep them in the table with a note.

Done when: `pytest tests/test_valuation.py` passes, including a hand-calculated test case Brett writes for one company.

Claude Code: when Brett asks you to review this module, check the finance logic against standard IB conventions and point out anything wrong. Do not rewrite it without being asked.

### Module 3: `excel.py`
- One workbook per run: `output/comps_{peer_set}_{date}.xlsx`
- Sheet "Comps": company rows, multiple columns, then mean/median/quartile rows. Blue font for hardcoded inputs, black for formulas. The stats rows must be Excel formulas (`=MEDIAN(...)`), not pasted values.
- Sheet "Inputs": raw financials per company, so every number on Comps traces back.
- Sheet "Football Field": bar chart of implied valuation range for a target company using 25th to 75th percentile EV/EBITDA and P/E.
- Sheet "Commentary": filled by Module 4.
- Number formats: `$#,##0` for dollars in millions, `0.0x` for multiples, `0.0%` for percentages.

Done when: the file opens in Excel with no errors and the median row recalculates if an input is changed.

### Module 4: `ai_commentary.py`
- Find the latest 10-K primary document URL from the submissions API. Download it, strip HTML, and extract the MD&A (Item 7) and Risk Factors (Item 1A) sections by heading regex.
- Send each section to the Anthropic API. Model: `claude-sonnet-4-6`. Two prompts, stored as markdown files in `compsai/prompts/`:
  - `normalize.md`: return JSON `{items: [{description, amount_usd_m, fiscal_year, direction: "add_back"|"deduct", source_quote}]}` listing non-recurring or one-time items an analyst would adjust out of EBITDA. Require a `source_quote` under 15 words for every item so Brett can verify.
  - `premium_discount.md`: return JSON `{growth_outlook, margin_trajectory, key_risks: [...], premium_or_discount: "premium"|"discount"|"inline", rationale}` given the company's multiples versus the peer median.
- Ask for JSON only, no prose, no code fences. Parse with try/except and log failures rather than crashing the pipeline.
- Chunk long sections to stay under ~100k tokens per call. Truncate rather than fail.
- Write results to the "Commentary" sheet: one block per company.

Done when: running on a 5-company peer set produces a Commentary sheet with at least one normalization item and a rationale for each company, and every `source_quote` can be found in the source 10-K text.

### Module 5: `app/streamlit_app.py` (Brett builds solo, Claude Code answers questions only)
- Input: target ticker, peer tickers (or pick a named set from `peer_sets.yaml`).
- Button: Run. Shows progress per stage.
- Output: comps table rendered in the browser, football field chart, commentary expanders, download button for the xlsx.
- Deployable to Streamlit Community Cloud.

### Final: `README.md`
- One paragraph on what it does and why.
- Screenshot of the Excel output and the app.
- Install and run instructions.
- A "Methodology" section explaining EV and each multiple in plain English. This section is Brett's interview prep.

## 6. Prompt design notes for Module 4

Keep the system prompt short and strict:

```
You are a sell-side equity research associate. You are given a section of a 10-K.
Return only valid JSON matching the schema below. No preamble, no markdown.
If you cannot find relevant information, return an empty list or "unknown".
Never invent numbers. Every item must include a short verbatim source_quote.
```

Put the schema in the user message. Put the 10-K text after a clear delimiter. Test prompts on one company first, inspect the JSON by hand, then run the full set.

## 7. Testing

- `tests/fixtures/` holds saved `companyfacts` JSON for 3 companies so tests run without network.
- `test_valuation.py` must include at least one case where Brett computed EV and EV/EBITDA by hand and the code matches to two decimals.
- Add a test that negative EBITDA companies are excluded from median calculations.

## 8. Peer sets to ship with

`config/peer_sets.yaml` should include at least:

```yaml
us_large_software: [MSFT, ORCL, CRM, ADBE, NOW, INTU]
us_banks: [JPM, BAC, WFC, C, GS, MS]
canadian_cross_listed: [SHOP, CP, CNQ, SU, BNS, RY]
consumer_staples: [PG, KO, PEP, CL, KMB, GIS]
```

Note for banks: EV/EBITDA is meaningless for financials. Use P/E and P/TBV instead. Add a `sector_type: bank | industrial` field per peer set and branch the multiples accordingly. Ask Brett before implementing P/TBV.

## 9. Stretch goals (after everything above works)

- DCF tab where Claude drafts revenue growth and margin assumptions from MD&A guidance, and Brett overrides them in blue cells.
- SEDAR+ support for Canadian filers (harder, no clean XBRL API).
- Weekly cron that refreshes prices and re-emails the comps sheet.

## 10. What good looks like

A recruiter opens the GitHub repo and sees: a clear README, a screenshot of a professional comps sheet, tests passing, and a commentary sample where every AI claim links to a quote in the filing. Brett can walk through `valuation.py` line by line without notes.
