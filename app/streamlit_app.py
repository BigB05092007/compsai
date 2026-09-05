"""
CompsAI Streamlit app (Module 5): pick a peer set, run the pipeline, browse the results,
download the Excel comps sheet.

Deploying to Streamlit Community Cloud:
  * Main file path:      app/streamlit_app.py
  * Requirements file:   requirements.txt (repository root of the compsai project)
  * Secrets (App settings -> Secrets):
        ANTHROPIC_API_KEY = "sk-ant-..."
        SEC_USER_AGENT    = "CompsAI you@example.com"
    Locally the same values come from .env; st.secrets is only consulted when the
    environment variable is missing.

Run locally:  streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Make `import compsai` work when the app is launched from the project root or from app/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compsai.pipeline import load_peer_sets, run_pipeline  # noqa: E402
from compsai.valuation import MULTIPLE_LABELS, MULTIPLES_BY_SECTOR  # noqa: E402

STAGE_LABELS = {
    "edgar": "SEC EDGAR financials",
    "market": "Market data",
    "valuation": "Multiples & peer statistics",
    "commentary": "AI commentary",
    "excel": "Excel workbook",
    "done": "Done",
}


# ------------------------------------------------------------------------------------------
# Secrets / configuration
# ------------------------------------------------------------------------------------------

def _load_secrets() -> None:
    """Environment first (.env locally), then st.secrets (Community Cloud)."""
    load_dotenv()
    for key in ("ANTHROPIC_API_KEY", "SEC_USER_AGENT", "COMPSAI_MODEL"):
        if os.environ.get(key):
            continue
        try:
            value = st.secrets.get(key)  # raises when no secrets file exists
        except Exception:  # noqa: BLE001 - no secrets configured is normal locally
            value = None
        if value:
            os.environ[key] = str(value)


# ------------------------------------------------------------------------------------------
# Formatting helpers (display copies only; the raw floats stay in session_state)
# ------------------------------------------------------------------------------------------

def _fmt(value, kind: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if kind == "money":
        return f"${value:,.0f}"
    if kind == "price":
        return f"${value:,.2f}"
    if kind == "x":
        return f"{value:.1f}x"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    return str(value)


_COMPS_COLUMNS = [  # (column, header, kind)
    ("price", "Price", "price"),
    ("market_cap", "Market cap ($mm)", "money"),
    ("ev", "Enterprise value ($mm)", "money"),
    ("ev_revenue_ttm", "EV / Revenue", "x"),
    ("ev_ebitda_ttm", "EV / EBITDA", "x"),
    ("pe_ttm", "P / E", "x"),
    ("p_tbv", "P / TBV", "x"),
    ("ebitda_margin", "EBITDA margin", "pct"),
    ("net_margin", "Net margin", "pct"),
    ("revenue_growth_1y", "Revenue growth (1Y)", "pct"),
    ("revenue_growth_3y_cagr", "Revenue CAGR (3Y)", "pct"),
]


def _comps_display(comps: pd.DataFrame, sector_type: str) -> pd.DataFrame:
    """Formatted comps table; EV columns are dropped for banks (not meaningful)."""
    bank = sector_type == "bank"
    out = pd.DataFrame(index=comps.index)
    out["Company"] = [f"{n} (target)" if t else n for n, t in zip(comps["name"], comps["is_target"])]
    for col, header, kind in _COMPS_COLUMNS:
        if bank and col in ("ev", "ev_revenue_ttm", "ev_ebitda_ttm", "ebitda_margin"):
            continue
        if not bank and col == "p_tbv":
            continue
        out[header] = [_fmt(v, kind) for v in comps[col]]
    out["Notes"] = comps["note"]
    return out


def _stats_display(stats: pd.DataFrame) -> pd.DataFrame:
    out = stats.copy()
    out.index = ["Mean", "Median", "25th percentile", "75th percentile"]
    out.columns = [MULTIPLE_LABELS.get(c, c) for c in out.columns]
    return out.map(lambda v: _fmt(v, "x"))


def _football_chart(ff: pd.DataFrame, current_price: float | None, ticker: str) -> alt.Chart:
    data = ff[["method", "implied_price_low", "implied_price_high"]].dropna()
    bars = (
        alt.Chart(data)
        .mark_bar(size=28, color="#1F3864")
        .encode(
            x=alt.X("implied_price_low:Q", title="Implied share price ($)"),
            x2="implied_price_high:Q",
            y=alt.Y("method:N", sort=None, title=None),
            tooltip=[
                alt.Tooltip("method:N", title="Method"),
                alt.Tooltip("implied_price_low:Q", title="25th pctl", format="$.2f"),
                alt.Tooltip("implied_price_high:Q", title="75th pctl", format="$.2f"),
            ],
        )
        .properties(title=f"Football field — implied share price for {ticker}", height=60 + 40 * len(data))
    )
    if current_price is not None and not math.isnan(current_price):
        rule = (
            alt.Chart(pd.DataFrame({"price": [current_price], "label": [f"Current ${current_price:,.2f}"]}))
            .mark_rule(color="#C00000", strokeDash=[6, 4], size=2)
            .encode(x="price:Q", tooltip=["label:N"])
        )
        return bars + rule
    return bars


# ------------------------------------------------------------------------------------------
# Page
# ------------------------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="CompsAI", page_icon="📊", layout="wide")
    _load_secrets()
    st.title("CompsAI — comparable company analysis")
    st.caption("SEC EDGAR XBRL → trading multiples → banker-formatted Excel → Claude-drafted commentary")

    for key, default in (("result", None), ("xlsx_bytes", None), ("xlsx_name", None)):
        if key not in st.session_state:
            st.session_state[key] = default

    # ---- Sidebar: inputs ----------------------------------------------------------------
    peer_sets = load_peer_sets()

    def _sync_sector() -> None:
        # A keyed selectbox keeps its own state across reruns, so the sector default has to
        # be pushed into session state whenever the peer set changes (banks -> P/E, P/TBV).
        chosen = st.session_state.get("peer_set")
        st.session_state["sector_type"] = peer_sets[chosen].sector_type if chosen in peer_sets else "industrial"

    with st.sidebar:
        st.header("Inputs")
        choice = st.selectbox("Peer set", ["(custom tickers)"] + list(peer_sets), key="peer_set",
                              on_change=_sync_sector)
        custom = st.text_input("Peer tickers (comma-separated)", key="custom_tickers",
                               placeholder="MSFT, ORCL, CRM")
        target = st.text_input("Target ticker (gets the football field)", key="target", placeholder="MSFT")
        sector_type = st.selectbox("Sector type", list(MULTIPLES_BY_SECTOR), key="sector_type",
                                   help="industrial: EV/Revenue, EV/EBITDA, P/E · bank: P/E, P/TBV")
        with_ai = st.checkbox("Generate AI commentary (needs ANTHROPIC_API_KEY)", key="with_ai",
                              value=bool(os.environ.get("ANTHROPIC_API_KEY")))
        offline = st.checkbox("Offline demo (bundled fixtures: FIXA, FIXB, FIXC)", key="offline", value=False)
        run = st.button("Run", type="primary", key="run")

        st.divider()
        st.caption("Data: SEC EDGAR companyfacts (free), Yahoo Finance prices. "
                   "Set SEC_USER_AGENT and ANTHROPIC_API_KEY in .env or Streamlit secrets.")

    tickers = list(peer_sets[choice].tickers) if choice in peer_sets else []
    tickers += [t.strip().upper() for t in custom.split(",") if t.strip()]
    peer_set_name = choice if choice in peer_sets else "custom"

    # ---- Run ------------------------------------------------------------------------------
    if run:
        if not tickers and not target.strip():
            st.error("Pick a peer set or enter at least one ticker.")
        else:
            with st.status("Running the comps pipeline…", expanded=True) as status:
                done: set[str] = set()

                def progress(stage: str, message: str) -> None:
                    label = STAGE_LABELS.get(stage, stage)
                    if stage not in done:
                        done.add(stage)
                        st.write(f"**{label}**")
                    st.caption(f"{label}: {message}")

                try:
                    # A private output directory per run: the app may serve several users at
                    # once, and the default date-keyed file name would let them overwrite each
                    # other's workbook.
                    result = run_pipeline(
                        tickers, peer_set_name=peer_set_name, target=target.strip() or None,
                        sector_type=sector_type, with_commentary=with_ai, progress=progress,
                        offline=offline, out_dir=Path(tempfile.mkdtemp(prefix="compsai-app-")),
                    )
                except Exception as exc:  # noqa: BLE001 - show the reason instead of a traceback page
                    status.update(label="Run failed", state="error")
                    st.error(f"Pipeline failed: {exc}")
                    st.session_state["result"] = None
                    return
                status.update(label="Done", state="complete", expanded=False)
                st.session_state["result"] = result
                # Read the workbook once, now: the bytes stay with this session even if the
                # file is later cleaned up or another run reuses the name.
                if result.xlsx_path and Path(result.xlsx_path).exists():
                    st.session_state["xlsx_bytes"] = Path(result.xlsx_path).read_bytes()
                    st.session_state["xlsx_name"] = Path(result.xlsx_path).name
                else:
                    st.session_state["xlsx_bytes"] = None
                    st.session_state["xlsx_name"] = None

    result = st.session_state["result"]
    if result is None:
        st.info("Choose a peer set (or the offline demo) in the sidebar and press **Run**.")
        return

    # ---- Results --------------------------------------------------------------------------
    for warning in result.warnings:
        st.warning(warning)

    st.subheader(f"Comps — {result.peer_set} ({result.sector_type})")
    st.caption("$ in millions except per share · TTM multiples · target excluded from peer statistics")
    st.dataframe(_comps_display(result.comps, result.sector_type))

    st.subheader("Peer statistics")
    st.dataframe(_stats_display(result.stats))

    if result.football_field is not None and result.target:
        st.subheader("Football field")
        target_company = next(c for c in result.companies if c.is_target)
        chart = _football_chart(result.football_field, target_company.market.get("price"), result.target)
        st.altair_chart(chart)
        with st.expander("Implied valuation table"):
            st.dataframe(result.football_field.round(2))

    if result.commentary:
        st.subheader("AI commentary")
        for ticker, res in result.commentary.items():
            company = next((c for c in result.companies if c.ticker == ticker), None)
            name = company.name if company else ticker
            view = res.premium_discount.get("premium_or_discount", "n/a")
            with st.expander(f"{ticker} — {name} — {view}"):
                if res.errors:
                    for err in res.errors:
                        st.warning(err)
                pd_ = res.premium_discount
                if pd_:
                    st.markdown(f"**Growth outlook:** {pd_.get('growth_outlook', 'unknown')}")
                    st.markdown(f"**Margin trajectory:** {pd_.get('margin_trajectory', 'unknown')}")
                    risks = pd_.get("key_risks") or []
                    if risks:
                        st.markdown("**Key risks:**\n" + "\n".join(f"- {r}" for r in risks))
                    st.markdown(f"**Rationale:** {pd_.get('rationale', 'unknown')}")
                if res.normalization_items:
                    st.markdown("**Normalization items (non-recurring, adjust out of EBITDA):**")
                    items = pd.DataFrame(res.normalization_items)
                    items = items.rename(columns={
                        "description": "Description", "amount_usd_m": "Amount ($mm)",
                        "fiscal_year": "Fiscal year", "direction": "Direction",
                        "source_quote": "Source quote", "verified": "Verified in filing",
                    }).drop(columns=[c for c in ("quote_word_count",) if c in items.columns])
                    st.dataframe(items)
                if res.source_url:
                    st.caption(f"Source: {res.source_form} filed {res.filing_date} — {res.source_url}")

    if st.session_state.get("xlsx_bytes"):
        st.download_button(
            "Download Excel comps sheet",
            data=st.session_state["xlsx_bytes"],
            file_name=st.session_state["xlsx_name"] or "comps.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


main()
