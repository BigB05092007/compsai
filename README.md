# CompsAI — AI-assisted comparable company analysis

CompsAI turns a list of tickers into a banker-formatted trading-comps workbook. It pulls
each company's financials straight from SEC EDGAR's free XBRL API (no Bloomberg, no paid
data), builds trailing-twelve-month figures, computes enterprise value and the standard
multiples (EV/Revenue, EV/EBITDA, P/E — or P/E and P/TBV for banks), and writes an Excel
file where every number on the Comps sheet is a live formula tracing back to an Inputs
sheet, with `MEDIAN`/`QUARTILE` formulas for the peer statistics and a football-field chart
for the target. On top of the numbers, Claude reads each company's latest 10-K and drafts
the parts an analyst writes by hand — non-recurring items to normalise out of EBITDA and a
premium/discount rationale versus the peer median — with every claim tied to a short
verbatim quote that the code verifies against the filing. It exists so that the valuation
logic is open, reproducible and explainable line by line in an interview.

> Status: Modules 1–5 are built and tested offline (197 tests). Live EDGAR/Yahoo/Anthropic
> calls are implemented but were **not** exercised from the build sandbox (no outbound
> network); see [Limitations](#limitations). Finance conventions the brief said to confirm
> with Brett are listed in [DECISIONS.md](DECISIONS.md).

## Screenshots

| Excel comps sheet (`output/comps_*.xlsx`) | Streamlit app |
|---|---|
| ![Comps sheet](docs/comps_sheet.png) | ![App](docs/app.png) |

Both come from the offline demo (fixture companies). Regenerate them with:

```bash
pip install pypdfium2 playwright && playwright install chromium   # one-off
python scripts/make_screenshots.py     # LibreOffice renders the Comps sheet; Playwright drives the app
```

## Quick start

```bash
git clone https://github.com/BigB05092007/compsai.git && cd compsai
python -m venv .venv && source .venv/bin/activate      # or: uv venv && source .venv/bin/activate
pip install -r requirements.txt                        # or: uv pip install -r requirements.txt
cp .env.example .env                                   # then edit:
#   SEC_USER_AGENT="CompsAI you@example.com"           # required by SEC's fair-access policy
#   ANTHROPIC_API_KEY=sk-ant-...                       # only for the AI commentary
```

Run the pieces:

```bash
# Module 1: financials for a few tickers (4 fiscal years + TTM), with the XBRL tags used
python -m compsai.edgar AAPL MSFT GOOGL --debug

# Whole pipeline on a named peer set with a target -> output/comps_us_large_software_<date>.xlsx
python -m compsai.pipeline --peer-set us_large_software --target MSFT

# Custom peers, banks, no AI
python -m compsai.pipeline --tickers JPM,BAC,WFC,C --target GS --sector-type bank --no-ai --name banks

# Streamlit app (also deployable to Streamlit Community Cloud; see the docstring in the app)
streamlit run app/streamlit_app.py

# Tests (fully offline: bundled EDGAR fixtures + a fake Anthropic client)
pytest -q
```

**Offline demo.** Pass `--offline` (or tick "Offline demo" in the app) to run on the bundled
fixture companies `FIXA`, `FIXB`, `FIXC` without any network access:

```bash
python -m compsai.pipeline --tickers FIXA,FIXB,FIXC --target FIXA --offline
```

**Real fixtures.** The bundled fixtures are synthetic (see Limitations). To replace them with
real EDGAR documents once you have network access:

```bash
python -m compsai.edgar AAPL MSFT JPM --save-fixture tests/fixtures
```

## How it works

| Stage | Module | What happens |
|---|---|---|
| 1 | `compsai/edgar.py` | Ticker → CIK, then `companyfacts` JSON (cached in `data/cache/`, ≤ 9 requests/s). For each concept the XBRL tags are tried in order **per fiscal period**, the fiscal-year label comes from the first filing that reported the period and the value from the latest (restated) one, and TTM = FY + YTD current − YTD prior from the newest 10-Q. Diagnostics (`tags_used`, `warnings`) ride on `DataFrame.attrs`. |
| 1b | `compsai/market.py` | Last price and shares outstanding from yfinance, falling back to the 10-K cover-page share count (`dei:EntityCommonStockSharesOutstanding`); manual overrides supported. |
| 2 | `compsai/valuation.py` | Enterprise value, multiples, margins, growth; outlier bounds; peer statistics (target excluded); implied-valuation ranges for the football field. |
| 3 | `compsai/excel.py` | `Inputs` (blue hardcodes) → `Comps` (black formulas, `MEDIAN`/`QUARTILE` stats over stat-eligible helper columns) → `Football Field` (stacked bar chart) → `Commentary`. |
| 4 | `compsai/ai_commentary.py` | Latest 10-K from the submissions index → HTML stripped → Item 7 and Item 1A cut out by heading regex (skipping the table of contents) → two JSON-only prompts (`compsai/prompts/`) → quotes verified against the filing. |
| 5 | `app/streamlit_app.py` | Sidebar inputs, per-stage progress, formatted tables, Altair football field, commentary expanders, xlsx download. |

`compsai/pipeline.py` runs the stages in order and isolates failures: a ticker that cannot be
loaded is skipped with a warning; only an empty peer set aborts the run.

Units everywhere: money in **USD millions**, share counts in **millions**, per-share values in
USD, margins/growth as fractions, multiples as plain floats (12.3 = 12.3x). See
[docs/DESIGN.md](docs/DESIGN.md) for the full interface contract.



## Project structure

```
compsai/
├── CLAUDE.md                        # the original project brief (build order, acceptance criteria)
├── README.md, DECISIONS.md          # this file; finance-judgment questions for Brett
├── docs/DESIGN.md                   # interface contract: units, schemas, algorithms, Excel layout
├── requirements.txt, pyproject.toml, .env.example
├── config/peer_sets.yaml            # named peer groups with sector_type (industrial | bank)
├── compsai/
│   ├── edgar.py                     # Module 1: SEC EDGAR pull, fiscal years + TTM
│   ├── market.py                    # Module 1b: price and shares outstanding
│   ├── valuation.py                 # Module 2: EV, multiples, stats, football field
│   ├── excel.py                     # Module 3: openpyxl workbook with live formulas
│   ├── ai_commentary.py             # Module 4: 10-K download, section extraction, Claude
│   ├── prompts/normalize.md, premium_discount.md
│   ├── models.py                    # CompanyData / CommentaryResult / PipelineResult
│   └── pipeline.py                  # orchestrates 1 → 4 end to end (+ CLI)
├── app/streamlit_app.py             # Module 5
├── tests/                           # pytest, fully offline
│   ├── test_edgar.py, test_market.py, test_valuation.py, test_excel.py,
│   ├── test_ai_commentary.py, test_pipeline.py, excel_recalc.py
│   └── fixtures/                    # synthetic EDGAR JSON, a 10-K HTML, sample commentary
├── output/                          # generated xlsx (gitignored)
└── data/cache/                      # cached EDGAR / market responses (gitignored)
```

## Conventions and open decisions

The brief asked that finance-judgment calls (debt composition, leases, P/TBV, exclusion
bounds, TTM construction, …) be confirmed with Brett rather than chosen silently. Each one is
implemented with a documented default and written up as a question, with the code location,
in [DECISIONS.md](DECISIONS.md).

## Limitations

* **Synthetic test fixtures.** The build environment could not reach sec.gov, so the
  bundled `companyfacts_FIX*.json` files are hand-built, schema-exact EDGAR documents for
  fictional companies, chosen to exercise real-world quirks (Apple's revenue tag switch,
  52/53-week year ends, Microsoft's D&A tag, restated comparatives, a bank without EBITDA).
  Replace them with real data via `--save-fixture` and re-check the hand-calculated tests.
* **Live paths are untested.** yfinance, EDGAR and the Anthropic API are called through the
  documented, current interfaces, but the first live run may surface a tag or field the
  fixtures did not anticipate — the `--debug` flag on `compsai.edgar` prints exactly which
  XBRL tag fed each number.
* **Foreign filers.** 20-F/40-F filers with IFRS tags are best effort; 6-K filings carry no
  XBRL, so their TTM equals the last fiscal year. SEDAR+ is not supported.
* **Basic, not diluted, share counts** for market cap (no treasury-stock method).
* **yfinance** is an unofficial Yahoo Finance client; when it fails, pass prices with
  `price_overrides` (pipeline) or enter them in the app.

## Provenance

Built with Claude Code from the brief in [`CLAUDE.md`](CLAUDE.md).
## License

MIT — see [LICENSE](LICENSE).
