"""
CompsAI: AI-assisted comparable company analysis.

Pulls public-company financials from SEC EDGAR (free XBRL API), computes trading
multiples for a peer set, writes a banker-formatted Excel comps sheet with live
formulas, and uses Claude to draft valuation commentary grounded in the 10-K.

Unit conventions used everywhere in this package (see docs/DESIGN.md):
  * money aggregates (revenue, EBITDA, debt, market cap, EV) -> USD millions
  * share counts                                            -> millions of shares
  * per-share values (price, EPS)                           -> USD per share
  * margins and growth                                      -> fractions (0.15 = 15%)
  * multiples                                               -> plain floats (12.3 = 12.3x)
"""

from pathlib import Path

__version__ = "0.1.0"

#: Divide raw XBRL dollar/share values by this to get the package's working unit.
MILLION = 1_000_000

#: Project root = the directory that contains the `compsai` package, `config/`, `tests/`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
