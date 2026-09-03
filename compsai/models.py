"""
Shared record types passed between CompsAI modules.

Why this file exists: edgar.py produces a financials table, market.py a price dict,
valuation.py a Series of multiples, ai_commentary.py a commentary record, and excel.py
needs all of them for every company. Bundling them in small dataclasses keeps function
signatures short and makes it obvious what each stage adds. See docs/DESIGN.md section 5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


@dataclass
class CompanyData:
    """Everything the comps sheet knows about one company."""

    ticker: str
    name: str
    financials: pd.DataFrame  # extract_financials() output (FY rows oldest-first, then TTM)
    market: dict  # get_market_data() output (price, shares_outstanding in mm, market_cap in $mm)
    multiples: pd.Series  # compute_multiples() output
    is_target: bool = False  # target rows are shown but excluded from peer statistics


@dataclass
class CommentaryResult:
    """Claude-drafted commentary for one company, with verification metadata."""

    ticker: str
    # Each item: {description, amount_usd_m, fiscal_year, direction ("add_back"|"deduct"),
    #             source_quote, verified (bool), quote_word_count (int)}
    normalization_items: list[dict] = field(default_factory=list)
    # {growth_outlook, margin_trajectory, key_risks: [...],
    #  premium_or_discount ("premium"|"discount"|"inline"|"unknown"), rationale}
    premium_discount: dict = field(default_factory=dict)
    source_form: str = ""  # "10-K", "20-F", "40-F"
    source_url: str = ""
    filing_date: str = ""
    model: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Plain-dict form (JSON-serialisable) for fixtures and the Streamlit app."""
        return {
            "ticker": self.ticker,
            "normalization_items": list(self.normalization_items),
            "premium_discount": dict(self.premium_discount),
            "source_form": self.source_form,
            "source_url": self.source_url,
            "filing_date": self.filing_date,
            "model": self.model,
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CommentaryResult":
        return cls(
            ticker=data.get("ticker", ""),
            normalization_items=list(data.get("normalization_items", [])),
            premium_discount=dict(data.get("premium_discount", {})),
            source_form=data.get("source_form", ""),
            source_url=data.get("source_url", ""),
            filing_date=data.get("filing_date", ""),
            model=data.get("model", ""),
            errors=list(data.get("errors", [])),
        )


@dataclass
class PipelineResult:
    """Output of one end-to-end run (see pipeline.run_pipeline)."""

    peer_set: str
    sector_type: str
    target: str | None
    companies: list[CompanyData]
    comps: pd.DataFrame  # index = ticker; columns = name, is_target, + compute_multiples index
    stats: pd.DataFrame  # summary_stats() output
    football_field: pd.DataFrame | None
    commentary: dict[str, CommentaryResult]
    xlsx_path: Path | None
    warnings: list[str] = field(default_factory=list)
