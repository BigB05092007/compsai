"""
Tests for Module 4 (compsai/ai_commentary.py).

Everything runs offline: the Anthropic client is replaced by FakeClient, which records the
kwargs of every `messages.create` call and returns canned text, and the 10-K comes from
tests/fixtures/filing_FIXA.html.  The tests assert the DESIGN.md section 7 contract.
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import anthropic
import httpx2
import numpy as np
import pandas as pd
import pytest

from compsai import ai_commentary as ac
from compsai.models import CommentaryResult, CompanyData

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ------------------------------------------------------------------------------------------
# Helpers and fakes
# ------------------------------------------------------------------------------------------

class FakeClient:
    """Stands in for anthropic.Anthropic: records calls, returns canned replies, or raises."""

    def __init__(self, replies: list[str] | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.replies = list(replies or ["{}"])
        self.error = error
        self.messages = self  # so `client.messages.create(...)` resolves to self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])


NORMALIZE_REPLY = json.dumps({
    "items": [
        {"description": "Orchard Networks litigation settlement", "amount_usd_m": 410,
         "fiscal_year": 2024, "direction": "add_back",
         "source_quote": "one-time charge of $410 million to settle the Orchard Networks patent litigation"},
        {"description": "Gain on sale of Grove accessories business", "amount_usd_m": 260.0,
         "fiscal_year": "2024", "direction": "deduct",
         "source_quote": "recognized a pre-tax gain of $260 million"},
        {"description": "Made-up item", "amount_usd_m": 999, "fiscal_year": 2024,
         "direction": "add_back", "source_quote": "this sentence is not in the filing"},
    ]
})

PREMIUM_REPLY = json.dumps({
    "growth_outlook": "Low-to-mid single digit growth guided for fiscal 2025.",
    "margin_trajectory": "Gross margin expanding on Services mix.",
    "key_risks": ["China weakness", "Search deal regulation", "Antitrust"],
    "premium_or_discount": "premium",
    "rationale": "24.5x EV/EBITDA vs 18.0x peer median is justified by margins.",
})

MULTIPLES_INDEX = ["market_cap", "ev", "ev_revenue_ttm", "ev_ebitda_ttm", "pe_ttm", "ebitda_margin",
                   "net_margin", "revenue_growth_1y", "revenue_growth_3y_cagr", "p_tbv", "price", "note"]


def make_multiples(**overrides) -> pd.Series:
    values = {"market_cap": 3_500_000.0, "ev": 3_560_000.0, "ev_revenue_ttm": 9.1, "ev_ebitda_ttm": 24.5,
              "pe_ttm": 30.0, "ebitda_margin": 0.37, "net_margin": 0.27, "revenue_growth_1y": 0.02,
              "revenue_growth_3y_cagr": 0.045, "p_tbv": 61.0, "price": 230.0, "note": ""}
    values.update(overrides)
    return pd.Series([values[k] for k in MULTIPLES_INDEX], index=MULTIPLES_INDEX)


def make_company(ticker: str = "FIXA") -> CompanyData:
    financials = pd.DataFrame({
        "fiscal_year": [2022, 2023, 2024, 2024],
        "period_type": ["FY", "FY", "FY", "TTM"],
        "period_end": ["2022-09-24", "2023-09-30", "2024-09-28", "2025-06-28"],
        "revenue": [394328.0, 383285.0, 391035.0, 400000.0],
    })
    return CompanyData(ticker=ticker, name="Fixture Fruit Inc.", financials=financials,
                       market={"price": 230.0, "shares_outstanding": 15116.0}, multiples=make_multiples())


def peer_median() -> pd.Series:
    return make_multiples(ev_revenue_ttm=6.0, ev_ebitda_ttm=18.0, pe_ttm=24.0, ebitda_margin=0.30,
                          net_margin=0.20, revenue_growth_1y=0.05, revenue_growth_3y_cagr=0.06, p_tbv=12.0)


@pytest.fixture
def filing_html() -> str:
    return (FIXTURES / "filing_FIXA.html").read_text(encoding="utf-8")


@pytest.fixture
def filing_text(filing_html) -> str:
    return ac.html_to_text(filing_html)


@pytest.fixture
def offline_fixture_dir(tmp_path, monkeypatch) -> Path:
    """A private fixture dir with the filing and a self-contained submissions index."""
    d = tmp_path / "fixtures"
    d.mkdir()
    shutil.copy(FIXTURES / "filing_FIXA.html", d / "filing_FIXA.html")
    (d / "submissions_FIXA.json").write_text(json.dumps(sample_submissions()), encoding="utf-8")
    monkeypatch.setenv("COMPSAI_FIXTURE_DIR", str(d))
    return d


def sample_submissions() -> dict:
    """Newest-first EDGAR submissions shape with 8-K, 10-Q and 10-K/A rows to be skipped."""
    rows = [
        ("0000000001-25-000011", "2025-08-15", "2025-08-14", "8-K", "fixa-8k_20250814.htm"),
        ("0000000001-25-000009", "2025-08-01", "2025-06-28", "10-Q", "fixa-20250628.htm"),
        ("0000000001-24-000012", "2024-12-20", "2024-09-28", "10-K/A", "fixa-20240928a.htm"),
        ("0000000001-24-000010", "2024-11-01", "2024-09-28", "10-K", "fixa-20240928.htm"),
        ("0000000001-23-000010", "2023-11-03", "2023-09-30", "10-K", "fixa-20230930.htm"),
    ]
    return {
        "cik": "0000000001",
        "filings": {"recent": {
            "accessionNumber": [r[0] for r in rows],
            "filingDate": [r[1] for r in rows],
            "reportDate": [r[2] for r in rows],
            "form": [r[3] for r in rows],
            "primaryDocument": [r[4] for r in rows],
            "primaryDocDescription": [r[3] for r in rows],
        }},
    }


# ------------------------------------------------------------------------------------------
# get_latest_annual_filing
# ------------------------------------------------------------------------------------------

def test_latest_annual_filing_picks_newest_10k_and_ignores_amendments():
    ref = ac.get_latest_annual_filing(sample_submissions(), "0000000001")
    assert ref.form == "10-K"
    assert ref.accession == "000000000124000010"  # dashes removed
    assert ref.primary_document == "fixa-20240928.htm"
    assert ref.filing_date == "2024-11-01"
    assert ref.report_date == "2024-09-28"
    assert ref.url == "https://www.sec.gov/Archives/edgar/data/1/000000000124000010/fixa-20240928.htm"


def test_latest_annual_filing_falls_back_to_20f_then_raises():
    subs = sample_submissions()
    recent = subs["filings"]["recent"]
    recent["form"] = ["8-K", "6-K", "20-F", "20-F", "40-F"]
    ref = ac.get_latest_annual_filing(subs, "1")
    assert ref.form == "20-F" and ref.accession == "000000000124000012"

    recent["form"] = ["8-K", "10-Q", "10-K/A", "6-K", "10-Q"]
    with pytest.raises(ac.CommentaryError):
        ac.get_latest_annual_filing(subs, "1")


# ------------------------------------------------------------------------------------------
# html_to_text / extract_section / chunk_text
# ------------------------------------------------------------------------------------------

def test_html_to_text_strips_scripts_styles_and_ix_header(filing_text):
    assert "SCRIPTCONTENTSHOULDNOTAPPEAR" not in filing_text
    assert "STYLECONTENTSHOULDNOTAPPEAR" not in filing_text
    assert "HIDDENFACT" not in filing_text  # ix:header hidden facts are invisible in the filing
    assert "\xa0" not in filing_text
    assert "  " not in filing_text  # runs of spaces collapsed
    assert "\n\n\n" not in filing_text  # 3+ newlines collapsed
    assert "Fixture Fruit Inc." in filing_text
    assert "Orchard Networks patent litigation" in filing_text


def test_extract_item7_skips_table_of_contents(filing_text):
    section = ac.extract_section(filing_text, "7")
    assert section.upper().startswith("ITEM 7.")
    assert "MANAGEMENT’S DISCUSSION" in section.upper()
    assert "one-time charge of $" in section
    assert "Items Affecting Comparability" in section
    assert "Interest Rate Risk" not in section  # that is Item 7A
    assert "CONSOLIDATED STATEMENTS OF OPERATIONS" not in section  # Item 8
    assert len(section) > 3000  # the TOC entry is one line long


def test_extract_item1a_ends_at_item_1b(filing_text):
    section = ac.extract_section(filing_text, "1A")
    assert section.lower().startswith("item 1a.")
    assert "Macroeconomic and Industry Risks" in section
    assert "subject to volatility" in section
    assert "Unresolved Staff Comments" not in section.split("\n", 1)[1]  # Item 1B body excluded
    assert "Cybersecurity" not in section
    # Cross-references mid-sentence ("as discussed in Item 7 of this report") do not start a
    # section: Item 7 begins at its real heading and contains no Item 1A text. (The phrase
    # "Items Affecting Comparability" legitimately appears twice inside Item 7 - once in prose,
    # once as a sub-heading - so counting it is not a valid proxy.)
    section7 = ac.extract_section(filing_text, "7")
    assert section7.upper().count("ITEM 7.") == 1
    assert "Macroeconomic and Industry Risks" not in section7
    assert "Items Affecting Comparability" in section7


def test_extract_section_handles_missing_and_unknown_items():
    assert ac.extract_section("no headings here at all", "7") == ""
    assert ac.extract_section("Item 7. Management's Discussion and Analysis\nbody", "9") == ""
    # Tolerant heading styles: dash separator, colon, plain apostrophe, no end heading.
    text = "ITEM 7 – MANAGEMENT'S DISCUSSION AND ANALYSIS\nSome body text.\nItem 8: Financial Statements\n..."
    assert ac.extract_section(text, "7") == "ITEM 7 – MANAGEMENT'S DISCUSSION AND ANALYSIS\nSome body text."
    text2 = "Item 1A: Risk Factors\nrisk body\nItem 2. Properties\nprops"  # no Item 1B -> fallback Item 2
    assert ac.extract_section(text2, "1A") == "Item 1A: Risk Factors\nrisk body"


def test_chunk_text_splits_on_line_breaks_and_preserves_content():
    text = "\n".join(f"line {i:03d} " + "x" * 20 for i in range(100))
    chunks = ac.chunk_text(text, max_chars=500)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) > 1
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
    assert ac.chunk_text("", max_chars=10) == []
    assert ac.chunk_text("short", max_chars=10) == ["short"]
    assert ac.chunk_text("a" * 25, max_chars=10) == ["a" * 10, "a" * 10, "a" * 5]  # no line breaks: hard cut


# ------------------------------------------------------------------------------------------
# Prompts and the Claude call
# ------------------------------------------------------------------------------------------

def test_prompt_templates_contain_delimiter_schema_and_rules():
    normalize = ac.load_prompt("normalize").substitute(ticker="FIXA", fiscal_year=2024, section_text="BODY")
    assert ac.SECTION_DELIMITER in normalize
    assert normalize.rstrip().endswith("BODY")
    assert '"items"' in normalize and '"source_quote"' in normalize and '"amount_usd_m"' in normalize
    assert "FIXA" in normalize and "2024" in normalize
    assert "15" in normalize and "verbatim" in normalize.lower()
    assert "code fences" in normalize.lower() and "never invent" in normalize.lower()
    assert "$1.25 billion" in normalize  # $$ escaping in the template works
    assert "$$" not in normalize

    premium = ac.load_prompt("premium_discount").substitute(
        ticker="FIXA", sector_type="industrial", multiples_table="TABLE", section_text="BODY")
    assert ac.SECTION_DELIMITER in premium
    assert "TABLE" in premium and "industrial" in premium
    for key in ("growth_outlook", "margin_trajectory", "key_risks", "premium_or_discount", "rationale"):
        assert f'"{key}"' in premium
    assert '"inline"' in premium and "unknown" in premium


def test_system_prompt_is_the_design_text():
    assert ac.SYSTEM_PROMPT.startswith("You are a sell-side equity research associate.")
    assert "Never invent numbers" in ac.SYSTEM_PROMPT
    assert "source_quote" in ac.SYSTEM_PROMPT


def test_call_claude_passes_system_model_and_concatenates_text_blocks(monkeypatch):
    monkeypatch.delenv("COMPSAI_MODEL", raising=False)
    fake = FakeClient(replies=["hello"])
    out = ac.call_claude("prompt text", client=fake)
    assert out == "hello"
    kwargs = fake.calls[0]
    assert kwargs["model"] == ac.DEFAULT_MODEL == "claude-sonnet-4-6"
    assert kwargs["system"] == ac.SYSTEM_PROMPT
    assert kwargs["max_tokens"] == 4096
    assert kwargs["messages"] == [{"role": "user", "content": "prompt text"}]
    assert "temperature" not in kwargs and "top_p" not in kwargs

    # Only text blocks are concatenated; a thinking/tool block is ignored.
    class MultiBlockClient(FakeClient):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(content=[
                types.SimpleNamespace(type="thinking", thinking="..."),
                types.SimpleNamespace(type="text", text="{\"a\": "),
                types.SimpleNamespace(type="text", text="1}"),
            ])

    assert ac.call_claude("p", client=MultiBlockClient()) == '{"a": 1}'


def test_call_claude_model_override_from_env(monkeypatch):
    monkeypatch.setenv("COMPSAI_MODEL", "claude-opus-5")
    fake = FakeClient(replies=["x"])
    ac.call_claude("p", client=fake)
    assert fake.calls[0]["model"] == "claude-opus-5"
    ac.call_claude("p", model="explicit-model", client=fake)
    assert fake.calls[1]["model"] == "explicit-model"


def _status_error(cls, status: int, message: str):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize("error, expected", [
    (_status_error(anthropic.RateLimitError, 429, "slow down"), "rate limit"),
    (_status_error(anthropic.InternalServerError, 500, "boom"), "HTTP 500"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
     "connection"),
])
def test_call_claude_wraps_sdk_errors(error, expected):
    with pytest.raises(ac.CommentaryError) as excinfo:
        ac.call_claude("p", client=FakeClient(error=error))
    assert expected.lower() in str(excinfo.value).lower()


# ------------------------------------------------------------------------------------------
# parse_json_response
# ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"items": [{"description": "x"}]}',
    '```json\n{"items": [{"description": "x"}]}\n```',
    'Sure, here is the JSON you asked for:\n\n{"items": [{"description": "x"}]}\n\nLet me know!',
])
def test_parse_json_response_tolerates_fences_and_preamble(text):
    assert ac.parse_json_response(text) == {"items": [{"description": "x"}]}


@pytest.mark.parametrize("text", ["", "   ", "not json at all", '{"items": [unclosed', "[1, 2, 3]", '"a string"'])
def test_parse_json_response_returns_none_never_raises(text):
    assert ac.parse_json_response(text) is None


# ------------------------------------------------------------------------------------------
# normalize_items
# ------------------------------------------------------------------------------------------

def test_normalize_items_validates_and_verifies(filing_text):
    section = ac.extract_section(filing_text, "7")
    fake = FakeClient(replies=[NORMALIZE_REPLY])
    items = ac.normalize_items(section, "FIXA", 2024, client=fake)

    assert len(fake.calls) == 1
    prompt = fake.calls[0]["messages"][0]["content"]
    assert ac.SECTION_DELIMITER in prompt and '"items"' in prompt and "FIXA" in prompt
    assert prompt.split(ac.SECTION_DELIMITER, 1)[1].strip() == section  # 10-K text after the delimiter

    assert [i["description"] for i in items] == [
        "Orchard Networks litigation settlement", "Gain on sale of Grove accessories business", "Made-up item"]
    first, second, fake_item = items
    assert first["amount_usd_m"] == 410.0 and first["fiscal_year"] == 2024 and first["direction"] == "add_back"
    assert first["quote_word_count"] == 12 and first["verified"] is True
    assert second["fiscal_year"] == 2024  # "2024" string coerced to int
    assert second["direction"] == "deduct" and second["verified"] is True
    assert fake_item["verified"] is False  # quote not in the filing
    assert fake_item["quote_word_count"] == 7
    for item in items:
        assert set(item) == {"description", "amount_usd_m", "fiscal_year", "direction",
                             "source_quote", "verified", "quote_word_count"}


def test_normalize_items_drops_malformed_and_dedupes():
    reply = json.dumps({"items": [
        {"description": "Restructuring charge", "amount_usd_m": None, "fiscal_year": None,
         "direction": "add_back", "source_quote": "restructuring charges of $185 million"},
        {"description": "restructuring CHARGE", "amount_usd_m": 185, "fiscal_year": None,
         "direction": "add_back", "source_quote": "restructuring charges"},  # duplicate description + year
        {"description": "Bad direction", "amount_usd_m": 1, "fiscal_year": 2024,
         "direction": "sideways", "source_quote": "x"},
        {"description": "", "amount_usd_m": 1, "fiscal_year": 2024, "direction": "deduct", "source_quote": "x"},
        {"description": "No quote", "amount_usd_m": 1, "fiscal_year": 2024, "direction": "deduct", "source_quote": ""},
        "not a dict",
        {"description": "Odd values", "amount_usd_m": "$1,250", "fiscal_year": 2024.0,
         "direction": "DEDUCT", "source_quote": "odd values quote"},
    ]})
    items = ac.normalize_items("Restructuring charges of $185 million. Odd values quote.", "T", 2024,
                               client=FakeClient(replies=[reply]))
    assert [i["description"] for i in items] == ["Restructuring charge", "Odd values"]
    assert items[0]["amount_usd_m"] is None and items[0]["fiscal_year"] is None and items[0]["verified"]
    assert items[1]["amount_usd_m"] == 1250.0 and items[1]["fiscal_year"] == 2024 and items[1]["direction"] == "deduct"


def test_normalize_items_handles_garbage_replies():
    assert ac.normalize_items("text", "T", 2024, client=FakeClient(replies=["not json"])) == []
    assert ac.normalize_items("text", "T", 2024, client=FakeClient(replies=['{"items": "nope"}'])) == []
    assert ac.normalize_items("text", "T", None, client=FakeClient(replies=['{"items": []}'])) == []


def test_normalize_items_chunks_and_truncates_long_sections(monkeypatch, caplog):
    monkeypatch.setattr(ac, "MAX_CHARS", 200)
    section = "\n".join(f"paragraph {i} " + "words " * 8 for i in range(40))  # far more than 2 chunks
    assert len(ac.chunk_text(section)) > ac.MAX_CHUNKS
    reply_a = json.dumps({"items": [{"description": "Item A", "amount_usd_m": 1, "fiscal_year": 2024,
                                     "direction": "add_back", "source_quote": "paragraph 0"}]})
    reply_b = json.dumps({"items": [{"description": "Item B", "amount_usd_m": 2, "fiscal_year": 2024,
                                     "direction": "deduct", "source_quote": "paragraph 3"},
                                    {"description": "item a", "amount_usd_m": 1, "fiscal_year": 2024,
                                     "direction": "add_back", "source_quote": "paragraph 0"}]})
    fake = FakeClient(replies=[reply_a, reply_b])
    with caplog.at_level("WARNING", logger="compsai"):
        items = ac.normalize_items(section, "T", 2024, client=fake)
    assert len(fake.calls) == ac.MAX_CHUNKS == 2
    assert "only the first 2" in caplog.text
    for call in fake.calls:
        body = call["messages"][0]["content"].split(ac.SECTION_DELIMITER, 1)[1]
        assert len(body.strip()) <= 200
    assert [i["description"] for i in items] == ["Item A", "Item B"]  # merged, de-duplicated across chunks


# ------------------------------------------------------------------------------------------
# verify_quote
# ------------------------------------------------------------------------------------------

def test_verify_quote_normalises_quotes_dashes_case_and_whitespace():
    source = "The Company’s one-time charge of $\n410\n million — “settled” in 2024.\n"
    assert ac.verify_quote("the company's ONE-TIME charge of $410 million", source)
    assert ac.verify_quote('"settled" in 2024', source)
    assert ac.verify_quote("charge of $410 million - \"settled\"", source)
    assert ac.verify_quote("  charge of $410 million.  ", source)
    assert not ac.verify_quote("charge of $420 million", source)
    assert not ac.verify_quote("", source)
    assert not ac.verify_quote("anything", "")


def test_fixture_commentary_quotes_verify_against_fixture_filing(filing_text):
    data = json.loads((FIXTURES / "commentary_FIXA.json").read_text(encoding="utf-8"))
    result = CommentaryResult.from_dict(data)
    assert result.ticker == "FIXA" and result.model == "fixture" and result.errors == []
    assert result.source_form == "10-K" and result.filing_date == "2024-11-01"
    assert result.source_url == "https://www.sec.gov/Archives/edgar/data/1/000000000124000010/fixa-20240928.htm"
    assert len(result.normalization_items) >= 3
    for item in result.normalization_items:
        assert ac.verify_quote(item["source_quote"], filing_text), item["source_quote"]
        assert item["verified"] is True
        assert item["quote_word_count"] == len(item["source_quote"].split()) < ac.MAX_QUOTE_WORDS
        assert item["direction"] in {"add_back", "deduct"}
        assert isinstance(item["amount_usd_m"], float) and isinstance(item["fiscal_year"], int)
    pdx = result.premium_discount
    assert pdx["premium_or_discount"] == "premium"
    assert 3 <= len(pdx["key_risks"]) <= 5
    assert "peer" in pdx["rationale"].lower() and "x" in pdx["rationale"]
    assert result.to_dict() == data


# ------------------------------------------------------------------------------------------
# premium_discount
# ------------------------------------------------------------------------------------------

def test_premium_discount_builds_table_and_parses_reply():
    fake = FakeClient(replies=[PREMIUM_REPLY])
    out = ac.premium_discount("MD&A text", "FIXA", make_multiples(), peer_median(), "industrial", client=fake)
    prompt = fake.calls[0]["messages"][0]["content"]
    assert ac.SECTION_DELIMITER in prompt and prompt.rstrip().endswith("MD&A text")
    assert "24.5x" in prompt and "18.0x" in prompt  # own EV/EBITDA vs peer median
    assert "37.0%" in prompt and "30.0%" in prompt  # EBITDA margin as percentages
    assert "P / TBV" not in prompt  # industrial table has no bank multiples
    assert out == {
        "growth_outlook": "Low-to-mid single digit growth guided for fiscal 2025.",
        "margin_trajectory": "Gross margin expanding on Services mix.",
        "key_risks": ["China weakness", "Search deal regulation", "Antitrust"],
        "premium_or_discount": "premium",
        "rationale": "24.5x EV/EBITDA vs 18.0x peer median is justified by margins.",
    }


def test_premium_discount_bank_table_and_nan_handling():
    fake = FakeClient(replies=[PREMIUM_REPLY])
    own = make_multiples(ev=np.nan, ev_revenue_ttm=np.nan, ev_ebitda_ttm=np.nan, ebitda_margin=np.nan, p_tbv=1.4)
    ac.premium_discount("text", "FIXC", own, peer_median(), "bank", client=fake)
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "P / TBV" in prompt and "1.4x" in prompt and "12.0x" in prompt
    assert "EV / EBITDA" not in prompt
    # Missing values render as n/a rather than crashing.
    fake2 = FakeClient(replies=[PREMIUM_REPLY])
    ac.premium_discount("text", "X", make_multiples(pe_ttm=np.nan), make_multiples(pe_ttm=None), "industrial",
                        client=fake2)
    assert "n/a" in fake2.calls[0]["messages"][0]["content"]


def test_premium_discount_fills_unknowns_on_bad_reply():
    unknown = {"growth_outlook": "unknown", "margin_trajectory": "unknown", "key_risks": [],
               "premium_or_discount": "unknown", "rationale": "unknown"}
    assert ac.premium_discount("t", "X", make_multiples(), peer_median(), "industrial",
                               client=FakeClient(replies=["garbage"])) == unknown
    partial = json.dumps({"growth_outlook": "ok", "key_risks": "one risk", "premium_or_discount": "Massive premium"})
    out = ac.premium_discount("t", "X", make_multiples(), peer_median(), "industrial",
                              client=FakeClient(replies=[partial]))
    assert out["growth_outlook"] == "ok" and out["key_risks"] == ["one risk"]
    assert out["premium_or_discount"] == "unknown" and out["rationale"] == "unknown"


def test_premium_discount_truncates_to_max_chars(monkeypatch):
    monkeypatch.setattr(ac, "MAX_CHARS", 50)
    fake = FakeClient(replies=[PREMIUM_REPLY])
    ac.premium_discount("y" * 500, "X", make_multiples(), peer_median(), "industrial", client=fake)
    body = fake.calls[0]["messages"][0]["content"].split(ac.SECTION_DELIMITER, 1)[1].strip()
    assert body == "y" * 50


# ------------------------------------------------------------------------------------------
# generate_commentary (offline, end to end)
# ------------------------------------------------------------------------------------------

def test_generate_commentary_offline_end_to_end(offline_fixture_dir, monkeypatch):
    monkeypatch.delenv("COMPSAI_MODEL", raising=False)
    fake = FakeClient(replies=[NORMALIZE_REPLY, PREMIUM_REPLY])
    result = ac.generate_commentary(make_company(), peer_median(), "industrial", client=fake, offline=True)

    assert isinstance(result, CommentaryResult)
    assert result.errors == []
    assert result.ticker == "FIXA" and result.model == "claude-sonnet-4-6"
    assert result.source_form == "10-K" and result.filing_date == "2024-11-01"
    assert result.source_url == "https://www.sec.gov/Archives/edgar/data/1/000000000124000010/fixa-20240928.htm"
    assert len(fake.calls) == 2  # one normalisation chunk + one premium/discount call
    assert all(call["system"] == ac.SYSTEM_PROMPT for call in fake.calls)

    verified = [i for i in result.normalization_items if i["verified"]]
    assert len(verified) == 2 and len(result.normalization_items) == 3
    assert result.premium_discount["premium_or_discount"] == "premium"
    assert "peer median" in result.premium_discount["rationale"]

    # The normalisation prompt carried Item 7 and the latest fiscal year from the financials.
    norm_prompt = fake.calls[0]["messages"][0]["content"]
    assert "FY2024" in norm_prompt and "Items Affecting Comparability" in norm_prompt
    assert "Interest Rate Risk" not in norm_prompt
    # The premium/discount prompt carried Item 7 followed by Item 1A.
    pd_prompt = fake.calls[1]["messages"][0]["content"]
    assert pd_prompt.index("Items Affecting Comparability") < pd_prompt.index("Macroeconomic and Industry Risks")
    # The result round-trips through the JSON form excel.py / the app consume.
    assert CommentaryResult.from_dict(json.loads(json.dumps(result.to_dict()))).to_dict() == result.to_dict()


def test_generate_commentary_skips_api_without_key(offline_fixture_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    created = []
    monkeypatch.setattr(ac.anthropic, "Anthropic", lambda *a, **k: created.append(1))
    result = ac.generate_commentary(make_company(), peer_median(), "industrial", offline=True)
    assert created == []
    assert result.normalization_items == [] and result.premium_discount == {}
    assert any("ANTHROPIC_API_KEY" in e for e in result.errors)
    assert result.source_form == "10-K"  # the filing itself was still located


def test_generate_commentary_records_api_errors_without_raising(offline_fixture_dir):
    error = _status_error(anthropic.RateLimitError, 429, "slow down")
    result = ac.generate_commentary(make_company(), peer_median(), "industrial",
                                    client=FakeClient(error=error), offline=True)
    assert result.normalization_items == [] and result.premium_discount == {}
    assert len(result.errors) == 2
    assert result.errors[0].startswith("normalization failed:")
    assert result.errors[1].startswith("premium/discount failed:")
    assert "429" in result.errors[0]


def test_generate_commentary_missing_fixture_is_an_error_not_an_exception(offline_fixture_dir):
    fake = FakeClient()
    result = ac.generate_commentary(make_company("NOPE"), peer_median(), "industrial", client=fake, offline=True)
    assert fake.calls == []
    assert len(result.errors) == 1 and "filing_NOPE.html" in result.errors[0]


def test_generate_commentary_without_submissions_fixture_still_runs(offline_fixture_dir):
    (offline_fixture_dir / "submissions_FIXA.json").unlink()
    fake = FakeClient(replies=[NORMALIZE_REPLY, PREMIUM_REPLY])
    result = ac.generate_commentary(make_company(), peer_median(), "industrial", client=fake, offline=True)
    assert result.errors == []
    assert result.source_url == "" and result.source_form == ""
    assert len(result.normalization_items) == 3


def test_generate_commentary_survives_unexpected_exceptions(offline_fixture_dir, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr(ac, "normalize_items", explode)
    fake = FakeClient(replies=[PREMIUM_REPLY])
    result = ac.generate_commentary(make_company(), peer_median(), "industrial", client=fake, offline=True)
    assert any("kaboom" in e for e in result.errors)
    assert result.premium_discount["premium_or_discount"] == "premium"  # the other half still ran


# ------------------------------------------------------------------------------------------
# download_filing_text (network replaced by a fake requests.get)
# ------------------------------------------------------------------------------------------

def test_download_filing_text_caches_by_accession(tmp_cache_dir, monkeypatch, filing_html):
    ref = ac.get_latest_annual_filing(sample_submissions(), "1")
    hits = []

    def fake_get(url, headers=None, timeout=None):
        hits.append(url)
        assert url == ref.url
        assert "CompsAI" in headers["User-Agent"]
        # Real responses expose bytes; the code must decode them itself (EDGAR sends no charset).
        return types.SimpleNamespace(status_code=200, content=filing_html.encode("utf-8"))

    monkeypatch.setattr(ac.requests, "get", fake_get)
    text = ac.download_filing_text(ref)
    assert "Orchard Networks patent litigation" in text and "HIDDENFACT" not in text
    assert (tmp_cache_dir / "filings" / f"{ref.accession}.txt").exists()

    assert ac.download_filing_text(ref) == text  # served from cache
    assert len(hits) == 1
    ac.download_filing_text(ref, refresh=True)
    assert len(hits) == 2


def test_download_filing_text_raises_on_http_error(tmp_cache_dir, monkeypatch):
    ref = ac.get_latest_annual_filing(sample_submissions(), "1")
    monkeypatch.setattr(ac.requests, "get",
                        lambda url, headers=None, timeout=None: types.SimpleNamespace(status_code=403, text=""))
    with pytest.raises(ac.CommentaryError, match="403"):
        ac.download_filing_text(ref)


# ------------------------------------------------------------------------------------------
# Review fixes: paging, decoding, headings, de-duplication, quote matching, JSON tails
# ------------------------------------------------------------------------------------------

def test_latest_annual_filing_pages_into_older_submission_files():
    """Heavy filers overflow filings.recent (only 424B2s there); the 10-K sits in a paged file."""
    recent = {"accessionNumber": ["0000000009-25-000{0:03d}".format(i) for i in range(5)],
              "filingDate": ["2025-08-0%d" % (i + 1) for i in range(5)], "reportDate": [""] * 5,
              "form": ["424B2"] * 5, "primaryDocument": ["p%d.htm" % i for i in range(5)],
              "primaryDocDescription": ["PROSPECTUS"] * 5}
    submissions = {"cik": "0000000009", "filings": {"recent": recent, "files": [
        {"name": "CIK0000000009-submissions-001.json", "filingCount": 3},
        {"name": "CIK0000000009-submissions-002.json", "filingCount": 3},
    ]}}
    pages = {
        "CIK0000000009-submissions-001.json": {  # newer page: still only prospectuses
            "accessionNumber": ["0000000009-25-000100"], "filingDate": ["2025-03-01"], "reportDate": [""],
            "form": ["424B2"], "primaryDocument": ["p.htm"]},
        "CIK0000000009-submissions-002.json": {  # older page: the 10-K
            "accessionNumber": ["0000000009-25-000050", "0000000009-24-000900"], "filingDate": ["2025-02-20", "2024-12-01"],
            "reportDate": ["2024-12-31", ""], "form": ["10-K", "8-K"], "primaryDocument": ["bank-20241231.htm", "x.htm"]},
    }
    loaded = []

    def loader(name):
        loaded.append(name)
        return pages[name]

    ref = ac.get_latest_annual_filing(submissions, "9", page_loader=loader)
    assert loaded == list(pages)  # newest page first, stops once found
    assert ref.form == "10-K" and ref.accession == "000000000925000050"
    assert ref.url == "https://www.sec.gov/Archives/edgar/data/9/000000000925000050/bank-20241231.htm"
    # Offline without an injected loader: no network paging, clear error.
    with pytest.raises(ac.CommentaryError):
        ac.get_latest_annual_filing(submissions, "9")


def test_html_to_text_decodes_utf8_bytes_without_charset_header():
    html = "<html><body><p>Item 7. Management’s Discussion — “one-time” charge of $410 million</p></body></html>"
    text = ac.html_to_text(html.encode("utf-8"))
    assert "Management’s Discussion — “one-time” charge" in text
    # what requests' latin-1 default would have produced must NOT appear
    assert "â€™" not in text and "Ã¢" not in text


def test_extract_item7_survives_hyperlinked_cross_reference_to_item_7a():
    html = """<html><body>
    <p>Item 7. Management&#8217;s Discussion and Analysis</p><p>Item 7A. Quantitative and Qualitative Disclosures</p>
    <p id="i7"><b>Item 7. Management&#8217;s Discussion and Analysis of Financial Condition</b></p>
    <p>Body text about results.</p>
    <p>Refer to <a href="#i7a">Item 7A</a> of this Form 10-K for interest-rate exposure.</p>
    <p>We recorded a one-time charge of $410 million in fiscal 2024.</p>
    <p id="i7a"><b>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</b></p>
    <p>Interest Rate Risk paragraph.</p>
    <p><b>Item 8. Financial Statements and Supplementary Data</b></p>
    </body></html>"""
    section = ac.extract_section(ac.html_to_text(html), "7")
    assert "one-time charge of $410 million" in section  # not cut at the cross-reference line
    assert "Interest Rate Risk" not in section


def test_extract_item7_accepts_combined_item_7_and_7a_heading():
    text = ("Item 7 and 7A. Management's Discussion and Analysis and Quantitative and Qualitative "
            "Disclosures\nbody of the combined section\nItem 8. Financial Statements\nstatements")
    assert ac.extract_section(text, "7").endswith("body of the combined section")


def test_normalize_items_keeps_same_item_in_two_fiscal_years():
    reply = json.dumps({"items": [
        {"description": "Restructuring charge", "amount_usd_m": 185, "fiscal_year": 2024,
         "direction": "add_back", "source_quote": "Restructuring charges of $185 million in fiscal 2024"},
        {"description": "Restructuring charge", "amount_usd_m": 120, "fiscal_year": 2023,
         "direction": "add_back", "source_quote": "restructuring charges of $120 million in fiscal 2023"},
        {"description": "Restructuring charge", "amount_usd_m": 185, "fiscal_year": 2024,
         "direction": "add_back", "source_quote": "Restructuring charges of $185 million"},  # true duplicate
    ]})
    items = ac.normalize_items("Restructuring charges of $185 million in fiscal 2024 and restructuring "
                               "charges of $120 million in fiscal 2023.", "T", 2024,
                               client=FakeClient(replies=[reply]))
    assert [(i["fiscal_year"], i["amount_usd_m"]) for i in items] == [(2024, 185.0), (2023, 120.0)]


def test_verify_quote_ignores_inline_xbrl_line_breaks_and_odd_hyphens():
    source = ac.html_to_text("<p>Greater China net sales decreased <ix:nonFraction>8</ix:nonFraction>% "
                             "to $<ix:nonFraction>1.2</ix:nonFraction> billion, and a loss of "
                             "$(<ix:nonFraction>410</ix:nonFraction>) million; a non\u2011recurring charge "
                             "and a non\xadrecurring gain.</p>")
    assert ac.verify_quote("net sales decreased 8%", source)
    assert ac.verify_quote("loss of $(410) million", source)
    assert ac.verify_quote("a non-recurring charge", source)
    assert ac.verify_quote("nonrecurring gain", source)
    assert not ac.verify_quote("net sales increased 8%", source)


def test_parse_json_response_ignores_trailing_prose_with_braces():
    data = ac.parse_json_response('{"items": [{"a": 1}]}\nNote: {fiscal_year} is null where unclear.')
    assert data == {"items": [{"a": 1}]}


def test_long_source_quotes_are_flagged(offline_fixture_dir):
    long_quote = " ".join(["word"] * 20)
    reply = json.dumps({"items": [{"description": "Long", "amount_usd_m": 1, "fiscal_year": 2024,
                                   "direction": "add_back", "source_quote": long_quote}]})
    company = make_company()
    res = ac.generate_commentary(company, peer_median(), "industrial",
                                 client=FakeClient(replies=[reply, json.dumps({"premium_or_discount": "inline"})]),
                                 offline=True)
    assert res.normalization_items[0]["quote_word_count"] == 20
    assert any("15 words or longer" in e for e in res.errors)
