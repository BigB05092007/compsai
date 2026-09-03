"""
Pipeline: run the whole comps analysis end to end (Modules 1 → 2 → 4 → 3).

What it does, stage by stage (each stage is reported through an optional progress callback
so the Streamlit app can show a checklist):

  edgar       pull every company's XBRL facts from SEC EDGAR and build the financials table
  market      fetch price and shares outstanding (yfinance, XBRL fallback, or overrides)
  valuation   compute multiples per company, peer statistics, and the target's football field
  commentary  ask Claude for normalization items and a premium/discount view (optional)
  excel       write the banker-formatted workbook
  done

Why it is built this way:
  * One company failing (a missing tag, a network error) must not sink the whole peer set,
    so per-company problems are logged, appended to ``PipelineResult.warnings`` and the
    company is skipped. Only when *no* company survives does the run raise.
  * The target company is shown in the table but never inside its own benchmark: it is
    excluded from the peer statistics (in Python and in the Excel helper columns).
  * Offline mode (``offline=True`` or ``COMPSAI_OFFLINE=1``) reads the bundled fixtures
    instead of the network so the pipeline can be demoed and tested anywhere.

CLI:
    python -m compsai.pipeline --peer-set us_large_software --target MSFT
    python -m compsai.pipeline --tickers FIXA,FIXB,FIXC --target FIXA --offline
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml
from dotenv import load_dotenv

from compsai import CONFIG_DIR, OUTPUT_DIR
from compsai.edgar import (
    extract_financials,
    fixture_dir,
    get_cik,
    get_company_facts,
    get_company_name,
    is_offline,
)
from compsai.market import get_market_data
from compsai.models import CommentaryResult, CompanyData, PipelineResult
from compsai.valuation import (
    MULTIPLES_BY_SECTOR,
    MULTIPLES_INDEX,
    compute_multiples,
    implied_valuation,
    mask_non_meaningful,
    summary_stats,
)

log = logging.getLogger("compsai")

#: Stage names in the order they are reported to the progress callback.
STAGES: tuple[str, ...] = ("edgar", "market", "valuation", "commentary", "excel", "done")

#: ``progress(stage, message)`` - called at least once per stage.
ProgressCallback = Callable[[str, str], None]

DEFAULT_PEER_SETS_PATH = CONFIG_DIR / "peer_sets.yaml"


# ------------------------------------------------------------------------------------------
# Peer-set configuration
# ------------------------------------------------------------------------------------------

@dataclass
class PeerSet:
    """A named list of tickers plus the sector branch the multiples should follow."""

    name: str
    tickers: list[str]
    sector_type: str = "industrial"  # "industrial" | "bank" (see valuation.MULTIPLES_BY_SECTOR)


def load_peer_sets(path: Path | str | None = None) -> dict[str, PeerSet]:
    """
    Read ``config/peer_sets.yaml``. Two shapes are accepted per entry:

        us_banks:                       # mapping form (preferred)
          sector_type: bank
          tickers: [JPM, BAC]
        consumer_staples: [PG, KO]      # bare list = industrial
    """
    path = Path(path) if path is not None else DEFAULT_PEER_SETS_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping of peer-set names to tickers")

    peer_sets: dict[str, PeerSet] = {}
    for name, spec in raw.items():
        if isinstance(spec, list):
            tickers, sector_type = spec, "industrial"
        elif isinstance(spec, dict):
            tickers = spec.get("tickers") or []
            sector_type = str(spec.get("sector_type") or "industrial")
        else:
            raise ValueError(f"{path}: peer set {name!r} must be a list or a mapping")
        if sector_type not in MULTIPLES_BY_SECTOR:
            raise ValueError(
                f"{path}: peer set {name!r} has sector_type {sector_type!r}; "
                f"expected one of {list(MULTIPLES_BY_SECTOR)}"
            )
        clean = [str(t).upper().strip() for t in tickers if str(t).strip()]
        peer_sets[str(name)] = PeerSet(name=str(name), tickers=clean, sector_type=sector_type)
    return peer_sets


# ------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------

def _report(progress: ProgressCallback | None, stage: str, message: str) -> None:
    log.info("[%s] %s", stage, message)
    if progress is not None:
        progress(stage, message)


def _normalise_tickers(tickers: list[str], target: str | None) -> tuple[list[str], str | None]:
    """Upper-case, de-duplicate (keeping order) and put the target first."""
    target = target.upper().strip() if target else None
    ordered: list[str] = []
    for t in ([target] if target else []) + list(tickers):
        t = str(t).upper().strip()
        if t and t not in ordered:
            ordered.append(t)
    return ordered, target


def load_company_financials(ticker: str, offline: bool = False, refresh: bool = False) -> tuple[dict, pd.DataFrame, str]:
    """EDGAR stage for one ticker: (facts JSON, financials table, company name)."""
    cik = get_cik(ticker, offline=offline)
    facts = get_company_facts(cik, refresh=refresh, offline=offline, ticker=ticker)
    financials = extract_financials(facts)
    name = get_company_name(ticker, offline=offline)
    return facts, financials, name


def _peer_median(comps: pd.DataFrame) -> pd.Series:
    """Median of every numeric column across the peers (target excluded, NM multiples masked).

    Used as the benchmark shown to Claude in the premium/discount prompt. Unlike
    ``summary_stats`` it also covers margins and growth, which the prompt lists.
    """
    peers = comps
    if "is_target" in comps.columns:
        peers = comps[~comps["is_target"].fillna(False).astype(bool)]
    if peers.empty:
        return pd.Series(dtype="float64")
    masked = mask_non_meaningful(peers)
    numeric = masked.select_dtypes(include="number")
    return numeric.median(numeric_only=True)


def _load_commentary_fixture(ticker: str) -> CommentaryResult | None:
    """Offline mode: ``commentary_{TICKER}.json`` from the fixture dir, if present."""
    path = fixture_dir() / f"commentary_{ticker.upper()}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CommentaryResult.from_dict(data)


# ------------------------------------------------------------------------------------------
# The pipeline
# ------------------------------------------------------------------------------------------

def run_pipeline(
    tickers: list[str],
    peer_set_name: str = "custom",
    target: str | None = None,
    sector_type: str = "industrial",
    with_commentary: bool = True,
    progress: ProgressCallback | None = None,
    offline: bool = False,
    price_overrides: dict[str, dict] | None = None,
    out_dir: Path | None = None,
    write_excel: bool = True,
    refresh: bool = False,
    commentary_client=None,
) -> PipelineResult:
    """
    Run the comps analysis for ``tickers`` (plus ``target``) and return a PipelineResult.

    * ``sector_type``: "industrial" (EV/Revenue, EV/EBITDA, P/E) or "bank" (P/E, P/TBV).
    * ``progress(stage, message)``: optional callback, called for every stage in STAGES order.
    * ``offline``: read the bundled fixtures instead of EDGAR / Yahoo / Anthropic.
    * ``price_overrides``: ``{"MSFT": {"price": 410.0, "shares_outstanding": 7430.0}}``
      (shares in millions) - wins over yfinance and XBRL.
    * ``commentary_client``: an object with ``.messages.create`` (tests inject a fake);
      when None the real Anthropic client is used if ``ANTHROPIC_API_KEY`` is set.
    """
    if sector_type not in MULTIPLES_BY_SECTOR:
        raise ValueError(f"unknown sector_type {sector_type!r}; expected one of {list(MULTIPLES_BY_SECTOR)}")
    offline = is_offline(offline)
    price_overrides = {k.upper(): v for k, v in (price_overrides or {}).items()}
    tickers, target = _normalise_tickers(tickers, target)
    if not tickers:
        raise ValueError("run_pipeline needs at least one ticker")

    warnings: list[str] = []

    # ---- Stage 1: EDGAR ------------------------------------------------------------------
    _report(progress, "edgar", f"pulling SEC EDGAR financials for {len(tickers)} companies"
                               + (" (offline fixtures)" if offline else ""))
    loaded: list[dict] = []  # {ticker, name, facts, financials}
    for ticker in tickers:
        try:
            facts, financials, name = load_company_financials(ticker, offline=offline, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the peer set
            log.warning("%s: skipped (EDGAR stage failed: %s)", ticker, exc)
            warnings.append(f"{ticker}: skipped - {exc}")
            continue
        for note in financials.attrs.get("warnings", []):
            warnings.append(f"{ticker}: {note}")
        loaded.append({"ticker": ticker, "name": name, "facts": facts, "financials": financials})
        _report(progress, "edgar", f"{ticker}: {len(financials)} periods ({name})")
    if not loaded:
        raise RuntimeError("no company could be loaded from EDGAR: " + "; ".join(warnings))

    # ---- Stage 2: market data ------------------------------------------------------------
    _report(progress, "market", "fetching share prices and shares outstanding")
    for entry in loaded:
        ticker = entry["ticker"]
        market = get_market_data(
            ticker, facts=entry["facts"], refresh=refresh, offline=offline,
            overrides=price_overrides.get(ticker),
        )
        entry["market"] = market
        if market.get("price") is None or (isinstance(market.get("price"), float) and math.isnan(market["price"])):
            warnings.append(f"{ticker}: no market price available (source={market.get('source') or 'none'}); "
                            "market cap and EV are blank")
        _report(progress, "market", f"{ticker}: price {market.get('price')} ({market.get('source')})")

    # ---- Stage 3: valuation --------------------------------------------------------------
    _report(progress, "valuation", "computing multiples and peer statistics")
    companies: list[CompanyData] = []
    for entry in loaded:
        ticker = entry["ticker"]
        try:
            multiples = compute_multiples(entry["financials"], entry["market"], sector_type)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: skipped (valuation failed: %s)", ticker, exc)
            warnings.append(f"{ticker}: skipped - valuation failed: {exc}")
            continue
        companies.append(CompanyData(
            ticker=ticker, name=entry["name"], financials=entry["financials"],
            market=entry["market"], multiples=multiples, is_target=(ticker == target),
        ))
    if not companies:
        raise RuntimeError("no company survived the valuation stage: " + "; ".join(warnings))
    if target and not any(c.is_target for c in companies):
        warnings.append(f"{target}: target could not be loaded; no football field")
        target = None

    comps = build_comps_table(companies)
    stats = summary_stats(comps, MULTIPLES_BY_SECTOR[sector_type])
    football_field: pd.DataFrame | None = None
    if target:
        target_company = next(c for c in companies if c.is_target)
        football_field = implied_valuation(target_company.financials, target_company.market, stats, sector_type)
    _report(progress, "valuation", f"{len(companies)} companies valued; peer median EV/EBITDA "
                                   f"{_fmt_stat(stats, 'median', 'ev_ebitda_ttm')}, P/E "
                                   f"{_fmt_stat(stats, 'median', 'pe_ttm')}")

    # ---- Stage 4: commentary (optional) --------------------------------------------------
    commentary: dict[str, CommentaryResult] = {}
    if not with_commentary:
        _report(progress, "commentary", "skipped (disabled)")
    else:
        commentary = _run_commentary(companies, comps, sector_type, offline, refresh,
                                     commentary_client, warnings, progress)

    # ---- Stage 5: Excel ------------------------------------------------------------------
    xlsx_path: Path | None = None
    if write_excel:
        from compsai.excel import write_comps_workbook  # local import: openpyxl is only needed here

        _report(progress, "excel", "writing the comps workbook")
        xlsx_path = write_comps_workbook(
            peer_set_name, companies, sector_type=sector_type, target=target,
            commentary=commentary or None, out_dir=out_dir or OUTPUT_DIR,
        )
        _report(progress, "excel", f"wrote {xlsx_path}")
    else:
        _report(progress, "excel", "skipped (write_excel=False)")

    _report(progress, "done", "complete" + (f" with {len(warnings)} warning(s)" if warnings else ""))
    return PipelineResult(
        peer_set=peer_set_name, sector_type=sector_type, target=target, companies=companies,
        comps=comps, stats=stats, football_field=football_field, commentary=commentary,
        xlsx_path=xlsx_path, warnings=warnings,
    )


def _run_commentary(companies, comps, sector_type, offline, refresh, client, warnings, progress) -> dict[str, CommentaryResult]:
    """Commentary stage: fixtures offline, Claude online (only when a key/client exists)."""
    load_dotenv()
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    results: dict[str, CommentaryResult] = {}

    if client is None and offline:
        _report(progress, "commentary", "offline: loading commentary fixtures")
        for company in companies:
            fixture = _load_commentary_fixture(company.ticker)
            if fixture is None:
                warnings.append(f"{company.ticker}: no commentary fixture offline; commentary skipped")
                continue
            results[company.ticker] = fixture
        return results

    if client is None and not have_key:
        message = "ANTHROPIC_API_KEY not set; commentary skipped"
        warnings.append(message)
        _report(progress, "commentary", message)
        return results

    from compsai.ai_commentary import generate_commentary  # local import: keeps anthropic optional

    peer_median = _peer_median(comps)
    _report(progress, "commentary", f"drafting 10-K commentary for {len(companies)} companies")
    for company in companies:
        result = generate_commentary(company, peer_median, sector_type, client=client,
                                     refresh=refresh, offline=offline)
        results[company.ticker] = result
        for err in result.errors:
            warnings.append(f"{company.ticker}: commentary - {err}")
        _report(progress, "commentary",
                f"{company.ticker}: {len(result.normalization_items)} normalization item(s), "
                f"view = {result.premium_discount.get('premium_or_discount', 'n/a')}")
    return results


def build_comps_table(companies: list[CompanyData]) -> pd.DataFrame:
    """One row per company: index = ticker; columns = name, is_target, then MULTIPLES_INDEX."""
    rows = []
    for company in companies:
        row = {"name": company.name, "is_target": bool(company.is_target)}
        for key in MULTIPLES_INDEX:
            row[key] = company.multiples.get(key)
        rows.append(row)
    comps = pd.DataFrame(rows, index=pd.Index([c.ticker for c in companies], name="ticker"))
    for key in MULTIPLES_INDEX:
        if key != "note":
            comps[key] = pd.to_numeric(comps[key], errors="coerce").astype("float64")
    comps["note"] = comps["note"].fillna("").astype(str)
    return comps


def _fmt_stat(stats: pd.DataFrame, row: str, column: str) -> str:
    try:
        value = float(stats.loc[row, column])
    except (KeyError, TypeError, ValueError):
        return "n/a"
    return "n/a" if math.isnan(value) else f"{value:.1f}x"


# ------------------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------------------

def _format_for_print(comps: pd.DataFrame) -> pd.DataFrame:
    """Banker-style strings for the terminal (raw floats stay in PipelineResult.comps)."""
    out = pd.DataFrame(index=comps.index)
    out["name"] = comps["name"]
    money = ["market_cap", "ev"]
    multiples = ["ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm", "p_tbv"]
    pcts = ["ebitda_margin", "net_margin", "revenue_growth_1y", "revenue_growth_3y_cagr"]

    def fmt(value, kind):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "n/a"
        if kind == "money":
            return f"${value:,.0f}"
        if kind == "x":
            return f"{value:.1f}x"
        if kind == "pct":
            return f"{value * 100:.1f}%"
        return f"{value:,.2f}"

    out["price"] = [fmt(v, "price") for v in comps["price"]]
    for col in money:
        out[col] = [fmt(v, "money") for v in comps[col]]
    for col in multiples:
        out[col] = [fmt(v, "x") for v in comps[col]]
    for col in pcts:
        out[col] = [fmt(v, "pct") for v in comps[col]]
    out["note"] = comps["note"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the CompsAI comparable-company pipeline.")
    parser.add_argument("--peer-set", help="name from config/peer_sets.yaml")
    parser.add_argument("--tickers", help="comma-separated tickers (overrides / extends the peer set)")
    parser.add_argument("--target", help="target company ticker (gets the football field)")
    parser.add_argument("--sector-type", choices=sorted(MULTIPLES_BY_SECTOR), help="industrial | bank")
    parser.add_argument("--no-ai", action="store_true", help="skip the Claude commentary stage")
    parser.add_argument("--offline", action="store_true", help="use bundled fixtures instead of the network")
    parser.add_argument("--refresh", action="store_true", help="ignore cached EDGAR / market responses")
    parser.add_argument("--out", help="output directory for the workbook (default: output/)")
    parser.add_argument("--no-excel", action="store_true", help="do not write the workbook")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(message)s")

    tickers: list[str] = []
    sector_type = args.sector_type
    peer_set_name = "custom"
    if args.peer_set:
        peer_sets = load_peer_sets()
        if args.peer_set not in peer_sets:
            parser.error(f"unknown peer set {args.peer_set!r}; available: {', '.join(peer_sets)}")
        chosen = peer_sets[args.peer_set]
        tickers = list(chosen.tickers)
        sector_type = sector_type or chosen.sector_type
        peer_set_name = args.peer_set
    if args.tickers:
        tickers += [t for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        parser.error("give --peer-set and/or --tickers")

    result = run_pipeline(
        tickers, peer_set_name=peer_set_name, target=args.target, sector_type=sector_type or "industrial",
        with_commentary=not args.no_ai, offline=args.offline, refresh=args.refresh,
        out_dir=Path(args.out) if args.out else None, write_excel=not args.no_excel,
    )

    with pd.option_context("display.width", 250, "display.max_columns", 50, "display.max_colwidth", 60):
        print(f"\n=== Comps: {result.peer_set} ({result.sector_type}) ===")
        print(_format_for_print(result.comps).to_string())
        print("\n=== Peer statistics (target excluded, NM multiples masked) ===")
        print(result.stats.round(2).to_string())
        if result.football_field is not None:
            print(f"\n=== Football field: {result.target} ===")
            print(result.football_field.round(2).to_string(index=False))
        if result.commentary:
            print("\n=== Commentary ===")
            for ticker, res in result.commentary.items():
                view = res.premium_discount.get("premium_or_discount", "n/a")
                print(f"{ticker}: {len(res.normalization_items)} normalization item(s); view = {view}"
                      + (f"; errors: {res.errors}" if res.errors else ""))
        if result.warnings:
            print("\n=== Warnings ===")
            for w in result.warnings:
                print(f"- {w}")
        if result.xlsx_path:
            print(f"\nWorkbook: {result.xlsx_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
