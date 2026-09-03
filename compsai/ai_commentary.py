"""
Module 4: Claude-drafted commentary grounded in the company's latest 10-K.

What it does, in order:
  1. Look up the newest annual report (10-K, else 20-F/40-F) in the company's EDGAR
     submissions index and download its primary HTML document.
  2. Strip the HTML down to plain text and cut out the two sections analysts read first:
     Item 7 (Management's Discussion & Analysis) and Item 1A (Risk Factors).
  3. Ask Claude two narrow, JSON-only questions:
       * normalize.md         -> non-recurring items to adjust out of EBITDA, each with a
                                 short verbatim quote so Brett can check it in the filing;
       * premium_discount.md  -> whether the stock deserves a premium/discount to the peer
                                 median, given its multiples and the filing's outlook/risks.
  4. Verify every quote against the filing text and bundle everything into a
     CommentaryResult that excel.py writes to the "Commentary" sheet.

Why it is built this way: the model is only ever asked to *extract* and *reason about* text
it was given, never to recall numbers from memory, and every claim carries a quote that is
mechanically checked.  A bad or missing API response is logged and recorded in
CommentaryResult.errors instead of crashing the comps run - commentary is a nice-to-have
layered on top of the numbers, not a dependency of them.  See docs/DESIGN.md section 7.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import string
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from compsai import DEFAULT_CACHE_DIR, DEFAULT_FIXTURE_DIR
from compsai.edgar import _throttle, _user_agent, get_cik, get_submissions, is_offline
from compsai.models import CommentaryResult, CompanyData

log = logging.getLogger("compsai")

# ------------------------------------------------------------------------------------------
# Constants (DESIGN.md section 7)
# ------------------------------------------------------------------------------------------

#: Short and strict on purpose: the schema lives in the user message, this only sets the role.
SYSTEM_PROMPT = (
    "You are a sell-side equity research associate. You are given a section of a 10-K.\n"
    "Return only valid JSON matching the schema below. No preamble, no markdown.\n"
    'If you cannot find relevant information, return an empty list or "unknown".\n'
    "Never invent numbers. Every item must include a short verbatim source_quote."
)

DEFAULT_MODEL = "claude-sonnet-4-6"  # override with env COMPSAI_MODEL
MAX_CHARS = 350_000  # ~85-90k tokens at ~4 chars/token: under the 100k-per-call target
MAX_CHUNKS = 2  # normalisation looks at no more than this many MAX_CHARS chunks
MAX_QUOTE_WORDS = 15  # a source_quote must be shorter than this to be checkable by eye

#: The 10-K text is placed after this line in every prompt so the model cannot confuse
#: instructions with filing content.
SECTION_DELIMITER = "===== 10-K SECTION TEXT BEGINS ====="

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
EDGAR_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
REQUEST_TIMEOUT = 60  # seconds; a full 10-K HTML can be several megabytes

#: Annual-report forms in order of preference. 10-K/A (amendments) are skipped because they
#: usually contain only the amended exhibit, not the full MD&A.
ANNUAL_FORMS = ("10-K", "20-F", "40-F")

VALID_DIRECTIONS = {"add_back", "deduct"}
VALID_VERDICTS = {"premium", "discount", "inline"}


class CommentaryError(RuntimeError):
    """Raised for problems specific to this module (API failures, no annual filing)."""


@dataclass
class FilingRef:
    """Where one annual report lives on EDGAR."""

    form: str  # "10-K", "20-F" or "40-F"
    accession: str  # accession number with dashes removed, e.g. "000032019324000123"
    primary_document: str  # e.g. "aapl-20240928.htm"
    filing_date: str  # ISO date the SEC accepted it
    report_date: str  # ISO fiscal period end
    url: str  # full https://www.sec.gov/Archives/... URL of the primary document


# ------------------------------------------------------------------------------------------
# Step 1: find and download the annual report
# ------------------------------------------------------------------------------------------

def get_latest_annual_filing(submissions: dict, cik: str) -> FilingRef:
    """Pick the newest 10-K (else 20-F, else 40-F) from an EDGAR submissions JSON.

    `submissions["filings"]["recent"]` holds parallel arrays sorted newest first, so the
    first index whose form matches is the latest filing of that form.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for wanted in ANNUAL_FORMS:
        for i, form in enumerate(forms):
            if form != wanted:  # exact match: "10-K/A" must not count as "10-K"
                continue
            accession = recent["accessionNumber"][i].replace("-", "")
            document = recent["primaryDocument"][i]
            return FilingRef(
                form=form,
                accession=accession,
                primary_document=document,
                filing_date=recent.get("filingDate", [""] * len(forms))[i],
                report_date=recent.get("reportDate", [""] * len(forms))[i],
                # EDGAR archive paths use the un-padded integer CIK.
                url=EDGAR_ARCHIVE_URL.format(cik=int(cik), accession=accession, document=document),
            )
    raise CommentaryError(f"no 10-K, 20-F or 40-F found in submissions for CIK {cik}")


def _filing_cache_path(ref: FilingRef) -> Path:
    cache_root = Path(os.environ.get("COMPSAI_CACHE_DIR") or DEFAULT_CACHE_DIR)
    return cache_root / "filings" / f"{ref.accession}.txt"


def download_filing_text(ref: FilingRef, refresh: bool = False) -> str:
    """Download the primary document and return its plain text (cached forever by accession).

    A filed document never changes, so the cache has no TTL; `refresh=True` re-downloads.
    """
    path = _filing_cache_path(ref)
    if path.exists() and not refresh:
        log.debug("filing cache hit %s", path)
        return path.read_text(encoding="utf-8")

    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}
    _throttle()  # share edgar.py's SEC rate limiter (<= 9 requests/second)
    log.info("downloading %s", ref.url)
    resp = requests.get(ref.url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise CommentaryError(f"SEC request failed: HTTP {resp.status_code} for {ref.url}")

    text = html_to_text(resp.text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


# ------------------------------------------------------------------------------------------
# Step 2: HTML -> text -> Item 7 / Item 1A
# ------------------------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    """Plain text of a 10-K: no scripts/styles, no hidden inline-XBRL header, tidy whitespace."""
    soup = BeautifulSoup(html, "html.parser")
    # ix:header carries hidden XBRL facts (dei:*) that are not visible in the filing.
    for tag in soup.find_all(["script", "style", "ix:header"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")  # &nbsp;
    text = re.sub(r"[ \t]+", " ", text)  # collapse runs of spaces
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse 3+ newlines to a paragraph break
    return text.strip()


# Heading regexes. `(?im)` = case-insensitive + multi-line so `^` anchors at a line start:
# real headings sit on their own line, whereas cross-references ("see Item 7 of this
# report") sit mid-sentence and are ignored. `[\s.:\-–—]*` tolerates
# "Item 7.", "ITEM 7 -", "Item 7:", "Item 7 —" and line breaks between the number and the
# title (BeautifulSoup emits one line per inline <span>). `[’'`]?` tolerates the curly
# apostrophe in "Management’s".
_SECTION_PATTERNS: dict[str, dict[str, str]] = {
    "1A": {
        "start": r"(?im)^[ \t]*item[\s.:\-–—]*1a[\s.:\-–—]*risk[\s]+factors",
        "end": r"(?im)^[ \t]*item[\s.:\-–—]*1b\b",
        "fallback_end": r"(?im)^[ \t]*item[\s.:\-–—]*2\b",
    },
    "7": {
        "start": r"(?im)^[ \t]*item[\s.:\-–—]*7[\s.:\-–—]*management[’'`]?s[\s]+discussion",
        "end": r"(?im)^[ \t]*item[\s.:\-–—]*7a\b",
        "fallback_end": r"(?im)^[ \t]*item[\s.:\-–—]*8\b",
    },
}


def extract_section(text: str, item: str) -> str:
    """Return the text of Item 1A or Item 7, or "" when the heading cannot be found.

    Every 10-K also lists the item in its table of contents, so there are usually two or
    more matches for the start heading.  The real section is the one with the longest body
    (from its heading to the next end heading); the TOC entry is one line long.
    """
    patterns = _SECTION_PATTERNS.get(item.upper())
    if patterns is None:
        log.warning("extract_section: unsupported item %r (expected '1A' or '7')", item)
        return ""

    best = ""
    for start in re.finditer(patterns["start"], text):
        end_match = re.search(patterns["end"], text[start.end():])
        if end_match is None:
            end_match = re.search(patterns["fallback_end"], text[start.end():])
        end = start.end() + end_match.start() if end_match else len(text)
        body = text[start.start():end].strip()
        if len(body) > len(best):
            best = body
    if not best:
        log.warning("extract_section: Item %s heading not found", item)
    return best


def chunk_text(text: str, max_chars: int | None = None) -> list[str]:
    """Split text into pieces of at most `max_chars`, breaking at a line end when possible.

    `max_chars` defaults to the module-level MAX_CHARS at call time (not import time), so
    tests and callers can lower the limit by setting the constant.
    """
    if max_chars is None:
        max_chars = MAX_CHARS
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:  # no convenient line break in the back half: hard cut
            cut = max_chars
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return chunks


# ------------------------------------------------------------------------------------------
# Step 3: prompts and the Claude call
# ------------------------------------------------------------------------------------------

def load_prompt(name: str) -> string.Template:
    """Load compsai/prompts/{name}.md as a string.Template ($placeholders, $$ = literal $)."""
    path = PROMPT_DIR / f"{name}.md"
    return string.Template(path.read_text(encoding="utf-8"))


def _model_name(model: str | None = None) -> str:
    load_dotenv()
    return model or os.environ.get("COMPSAI_MODEL", "").strip() or DEFAULT_MODEL


def call_claude(user_prompt: str, *, system: str = SYSTEM_PROMPT, model: str | None = None,
                max_tokens: int = 4096, client=None) -> str:
    """Send one user message to Claude and return the concatenated text of the reply.

    `client` is any object with `.messages.create(...)` (the real anthropic.Anthropic or a
    test fake).  SDK 1.x notes: no `temperature`, no assistant prefill, model id without a
    date suffix.
    """
    if client is None:
        load_dotenv()  # anthropic.Anthropic() reads ANTHROPIC_API_KEY from the environment
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            raise CommentaryError(f"could not create Anthropic client: {exc}") from exc

    model_name = _model_name(model)
    try:
        response = client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
    # Most specific first so the log line says what actually happened.
    except anthropic.RateLimitError as exc:
        log.error("Anthropic rate limit hit (model %s): %s", model_name, exc)
        raise CommentaryError(f"Anthropic API rate limit (HTTP 429): {exc}") from exc
    except anthropic.APIStatusError as exc:
        log.error("Anthropic API error HTTP %s (model %s): %s", exc.status_code, model_name, exc)
        raise CommentaryError(f"Anthropic API error HTTP {exc.status_code}: {exc}") from exc
    except anthropic.APIConnectionError as exc:
        log.error("could not reach the Anthropic API: %s", exc)
        raise CommentaryError(f"Anthropic API connection error: {exc}") from exc

    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


def parse_json_response(text: str) -> dict | None:
    """Best-effort JSON extraction from a model reply; returns None (never raises) on failure."""
    if not text or not text.strip():
        log.warning("parse_json_response: empty response")
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)  # tolerate ```json fences despite the prompt
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        log.warning("parse_json_response: no JSON object in response: %.120r", text)
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except (ValueError, TypeError) as exc:
        log.warning("parse_json_response: invalid JSON (%s): %.120r", exc, text)
        return None
    if not isinstance(data, dict):
        log.warning("parse_json_response: expected a JSON object, got %s", type(data).__name__)
        return None
    return data


# ------------------------------------------------------------------------------------------
# Step 3a: normalisation items (non-recurring adjustments to EBITDA)
# ------------------------------------------------------------------------------------------

def _to_number(value) -> float | None:
    """Coerce a JSON value to float; strings like "$1,250" are tolerated; anything else -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def _to_year(value) -> int | None:
    number = _to_number(value)
    if number is None or number != int(number) or not 1900 <= number <= 2100:
        return None
    return int(number)


def _clean_item(raw) -> dict | None:
    """Validate one normalisation item from the model; None means 'drop it'."""
    if not isinstance(raw, dict):
        return None
    description = str(raw.get("description") or "").strip()
    quote = str(raw.get("source_quote") or "").strip()
    direction = str(raw.get("direction") or "").strip().lower()
    if not description or not quote or direction not in VALID_DIRECTIONS:
        log.warning("dropping malformed normalization item: %r", raw)
        return None
    return {
        "description": description,
        "amount_usd_m": _to_number(raw.get("amount_usd_m")),
        "fiscal_year": _to_year(raw.get("fiscal_year")),
        "direction": direction,
        "source_quote": quote,
        "verified": False,  # set by the caller once the quote is checked against the filing
        "quote_word_count": len(quote.split()),
    }


def normalize_items(section_text: str, ticker: str, fiscal_year: int | None, client=None) -> list[dict]:
    """Ask Claude for non-recurring items in the MD&A; returns validated, de-duplicated items.

    Long sections are chunked; only the first MAX_CHUNKS chunks are sent (the rest is
    dropped with a warning rather than failing the run).
    """
    chunks = chunk_text(section_text)
    if len(chunks) > MAX_CHUNKS:
        log.warning("%s: section is %d chunks of %d chars; only the first %d are analysed",
                    ticker, len(chunks), MAX_CHARS, MAX_CHUNKS)
        chunks = chunks[:MAX_CHUNKS]

    template = load_prompt("normalize")
    items: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks:
        prompt = template.substitute(
            ticker=ticker,
            fiscal_year=fiscal_year if fiscal_year is not None else "unknown",
            section_text=chunk,
        )
        reply = call_claude(prompt, client=client)
        data = parse_json_response(reply)
        raw_items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw_items, list):
            log.warning("%s: normalization reply had no 'items' list", ticker)
            continue
        for raw in raw_items:
            item = _clean_item(raw)
            if item is None:
                continue
            key = item["description"][:60].lower()  # de-duplicate across chunks
            if key in seen:
                continue
            seen.add(key)
            item["verified"] = verify_quote(item["source_quote"], section_text)
            items.append(item)
    return items


# ------------------------------------------------------------------------------------------
# Step 4: quote verification
# ------------------------------------------------------------------------------------------

_QUOTE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # curly single quotes
    "“": '"', "”": '"', "„": '"',  # curly double quotes
    "–": "-", "—": "-", "−": "-", "‐": "-",  # dashes / minus
    "\xa0": " ",
})
_SURROUNDING_PUNCTUATION = " \t\n.,;:!?\"'()[]-"


def _normalise_for_match(text: str) -> str:
    text = text.lower().translate(_QUOTE_TRANSLATION)
    text = re.sub(r"\s+", " ", text)
    # Inline XBRL splits "$410" into "$" and "410" on separate lines; close that gap.
    text = re.sub(r"\$ (?=\d)", "$", text)
    return text.strip(_SURROUNDING_PUNCTUATION)


def verify_quote(quote: str, source_text: str) -> bool:
    """True when `quote` appears verbatim in `source_text` (ignoring case, quote style, spacing)."""
    needle = _normalise_for_match(quote or "")
    if not needle:
        return False
    return needle in _normalise_for_match(source_text or "")


# ------------------------------------------------------------------------------------------
# Step 3b: premium / discount view
# ------------------------------------------------------------------------------------------

#: (Series label, display label, format) rows of the multiples table sent to Claude.
_TABLE_ROWS_INDUSTRIAL = [
    ("ev_revenue_ttm", "EV / Revenue (TTM)", "multiple"),
    ("ev_ebitda_ttm", "EV / EBITDA (TTM)", "multiple"),
    ("pe_ttm", "P / E (TTM)", "multiple"),
    ("ebitda_margin", "EBITDA margin", "pct"),
    ("net_margin", "Net margin", "pct"),
    ("revenue_growth_1y", "Revenue growth (1Y)", "pct"),
    ("revenue_growth_3y_cagr", "Revenue CAGR (3Y)", "pct"),
]
_TABLE_ROWS_BANK = [
    ("pe_ttm", "P / E (TTM)", "multiple"),
    ("p_tbv", "P / TBV", "multiple"),
    ("net_margin", "Net margin", "pct"),
    ("revenue_growth_1y", "Revenue growth (1Y)", "pct"),
    ("revenue_growth_3y_cagr", "Revenue CAGR (3Y)", "pct"),
]


def _fmt(value, kind: str) -> str:
    """12.3 -> '12.3x' for multiples, 0.153 -> '15.3%' for fractions, NaN/None -> 'n/a'."""
    number = _to_number(value)
    if number is None or pd.isna(number):
        return "n/a"
    return f"{number:.1f}x" if kind == "multiple" else f"{number * 100:.1f}%"


def _multiples_table(ticker: str, multiples: pd.Series, peer_median: pd.Series, sector_type: str) -> str:
    """Plain-text table `Metric | TICKER | Peer median` for the premium/discount prompt."""
    rows = _TABLE_ROWS_BANK if sector_type == "bank" else _TABLE_ROWS_INDUSTRIAL
    header = f"{'Metric':<24}| {ticker:<12}| Peer median"
    lines = [header, "-" * len(header)]
    for key, label, kind in rows:
        own = _fmt(multiples.get(key), kind) if multiples is not None else "n/a"
        peer = _fmt(peer_median.get(key), kind) if peer_median is not None else "n/a"
        lines.append(f"{label:<24}| {own:<12}| {peer}")
    return "\n".join(lines)


def _to_str(value) -> str:
    text = str(value).strip() if value is not None else ""
    return text or "unknown"


def _clean_premium_discount(data: dict | None) -> dict:
    """Coerce the model's reply into the fixed premium/discount shape; missing -> unknown/[]."""
    data = data if isinstance(data, dict) else {}
    risks = data.get("key_risks")
    if isinstance(risks, str):
        risks = [risks]
    if not isinstance(risks, list):
        risks = []
    verdict = str(data.get("premium_or_discount") or "").strip().lower()
    return {
        "growth_outlook": _to_str(data.get("growth_outlook")),
        "margin_trajectory": _to_str(data.get("margin_trajectory")),
        "key_risks": [str(r).strip() for r in risks if str(r).strip()],
        "premium_or_discount": verdict if verdict in VALID_VERDICTS else "unknown",
        "rationale": _to_str(data.get("rationale")),
    }


def premium_discount(section_text: str, ticker: str, multiples: pd.Series, peer_median: pd.Series,
                     sector_type: str, client=None) -> dict:
    """Ask Claude whether the company deserves a premium/discount to the peer median."""
    if len(section_text) > MAX_CHARS:
        log.warning("%s: premium/discount text truncated from %d to %d chars",
                    ticker, len(section_text), MAX_CHARS)
        section_text = section_text[:MAX_CHARS]
    prompt = load_prompt("premium_discount").substitute(
        ticker=ticker,
        sector_type=sector_type,
        multiples_table=_multiples_table(ticker, multiples, peer_median, sector_type),
        section_text=section_text,
    )
    reply = call_claude(prompt, client=client)
    return _clean_premium_discount(parse_json_response(reply))


# ------------------------------------------------------------------------------------------
# Step 5: one company end to end
# ------------------------------------------------------------------------------------------

def _fixture_dir() -> Path:
    return Path(os.environ.get("COMPSAI_FIXTURE_DIR") or DEFAULT_FIXTURE_DIR)


def _latest_fiscal_year(company: CompanyData) -> int | None:
    """Newest FY label in the financials table (the fiscal year the 10-K covers)."""
    df = company.financials
    try:
        fy_rows = df[df["period_type"] == "FY"]
        if fy_rows.empty:
            return None
        return int(fy_rows["fiscal_year"].max())
    except (KeyError, TypeError, ValueError):
        return None


def _load_filing(company: CompanyData, result: CommentaryResult, refresh: bool, offline: bool) -> str | None:
    """Fill result.source_* and return the filing text, or None (with result.errors set)."""
    ticker = company.ticker.upper()
    if is_offline(offline):
        path = _fixture_dir() / f"filing_{ticker}.html"
        if not path.exists():
            result.errors.append(f"offline mode: no filing fixture {path}")
            return None
        text = html_to_text(path.read_text(encoding="utf-8"))
        # Metadata is best-effort offline: the fixture index may be absent.
        try:
            submissions = get_submissions("0", offline=True, ticker=ticker)
            ref = get_latest_annual_filing(submissions, submissions.get("cik", "0"))
            result.source_form, result.source_url, result.filing_date = ref.form, ref.url, ref.filing_date
        except (FileNotFoundError, CommentaryError, KeyError, ValueError) as exc:
            log.warning("%s: no filing metadata offline (%s)", ticker, exc)
        return text

    cik = get_cik(ticker)
    submissions = get_submissions(cik, refresh=refresh)
    ref = get_latest_annual_filing(submissions, cik)
    result.source_form, result.source_url, result.filing_date = ref.form, ref.url, ref.filing_date
    return download_filing_text(ref, refresh=refresh)


def generate_commentary(company: CompanyData, peer_median: pd.Series, sector_type: str,
                        client=None, refresh: bool = False, offline: bool = False) -> CommentaryResult:
    """Produce the CommentaryResult for one company.  Never raises: problems go to .errors."""
    ticker = company.ticker.upper()
    result = CommentaryResult(ticker=ticker, model=_model_name())

    # 1. Filing text (network or fixture).
    try:
        text = _load_filing(company, result, refresh=refresh, offline=offline)
    except Exception as exc:  # noqa: BLE001 - any failure here must not stop the comps run
        log.error("%s: could not load the annual filing: %s", ticker, exc)
        result.errors.append(f"could not load annual filing: {exc}")
        return result
    if text is None:
        return result

    # 2. Sections.  MD&A is where one-time items and guidance live; Risk Factors adds the
    #    downside case for the premium/discount view.
    mdna = extract_section(text, "7")
    risks = extract_section(text, "1A")
    if not mdna:
        log.warning("%s: Item 7 not found; normalising on the first %d chars of the filing", ticker, MAX_CHARS)
        norm_text = text[:MAX_CHARS]
    else:
        norm_text = mdna
    pd_text = "\n\n".join(part for part in (mdna, risks) if part) or text
    pd_text = pd_text[:MAX_CHARS]

    # 3. Client.  Without a key there is nothing to call - say so and stop.
    if client is None:
        load_dotenv()
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            result.errors.append("ANTHROPIC_API_KEY not set; commentary skipped")
            return result
        try:
            client = anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            result.errors.append(f"could not create Anthropic client: {exc}")
            return result

    fiscal_year = _latest_fiscal_year(company)
    if fiscal_year is None and result.filing_date:
        fiscal_year = int(result.filing_date[:4])

    # 4. Non-recurring items, each re-verified against the whole filing (a quote may sit
    #    just outside the extracted section, e.g. in a note the model saw in the fallback text).
    try:
        items = normalize_items(norm_text, ticker, fiscal_year, client=client)
        for item in items:
            item["verified"] = item["verified"] or verify_quote(item["source_quote"], text)
        result.normalization_items = items
    except CommentaryError as exc:
        result.errors.append(f"normalization failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("%s: unexpected error in normalize_items", ticker)
        result.errors.append(f"normalization failed unexpectedly: {exc}")

    # 5. Premium / discount view.
    try:
        result.premium_discount = premium_discount(
            pd_text, ticker, company.multiples, peer_median, sector_type, client=client)
    except CommentaryError as exc:
        result.errors.append(f"premium/discount failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("%s: unexpected error in premium_discount", ticker)
        result.errors.append(f"premium/discount failed unexpectedly: {exc}")

    return result


# ------------------------------------------------------------------------------------------
# CLI: inspect the raw commentary for one company before running the whole peer set
# ------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft 10-K commentary for one ticker and print it as JSON.")
    parser.add_argument("ticker")
    parser.add_argument("--offline", action="store_true", help="read filing_{TICKER}.html from the fixture dir")
    parser.add_argument("--sections-only", action="store_true",
                        help="print the extracted Item 7 / Item 1A lengths and exit (no API call)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(levelname)s %(message)s")

    # Local imports keep the module import light for the tests.
    from compsai.edgar import extract_financials, get_company_facts, get_company_name
    from compsai.market import get_market_data
    from compsai.valuation import compute_multiples

    ticker = args.ticker.upper()
    offline = is_offline(args.offline)
    cik = get_cik(ticker, offline=offline)
    facts = get_company_facts(cik, offline=offline, ticker=ticker)
    financials = extract_financials(facts)
    market = get_market_data(ticker, facts=facts, offline=offline)
    multiples = compute_multiples(financials, market)
    company = CompanyData(ticker=ticker, name=get_company_name(ticker, offline=offline),
                          financials=financials, market=market, multiples=multiples)

    if args.sections_only:
        result = CommentaryResult(ticker=ticker)
        text = _load_filing(company, result, refresh=False, offline=offline) or ""
        print(json.dumps({"item_7_chars": len(extract_section(text, "7")),
                          "item_1a_chars": len(extract_section(text, "1A")),
                          "errors": result.errors, "source_url": result.source_url}, indent=2))
        return 0

    peer_median = multiples.copy()  # a single company is its own "peer median" for a smoke test
    result = generate_commentary(company, peer_median, sector_type="industrial", offline=offline)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
