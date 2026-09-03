Company ticker: $ticker
Most recent fiscal year covered by this filing: FY$fiscal_year

TASK
From the 10-K section below, list every non-recurring or one-time item that a sell-side
analyst would adjust out of EBITDA when normalising earnings for a comparable-company
analysis. Typical examples: restructuring and severance charges, litigation settlements,
asset or goodwill impairments, gains or losses on the sale of a business, acquisition-
related costs, insurance recoveries, and other items the company itself describes as
unusual, non-recurring, one-time or not expected to recur. Ignore ordinary items such as
depreciation, interest, income taxes and stock-based compensation.

OUTPUT FORMAT
Return ONLY a JSON object with exactly this schema. No prose before or after it, no
markdown, no code fences.

{"items": [
  {"description": "short plain-English label for the item",
   "amount_usd_m": 123.4,
   "fiscal_year": 2024,
   "direction": "add_back",
   "source_quote": "verbatim words copied from the text below"}
]}

FIELD RULES
- description: what the item is, in a few words (e.g. "Restructuring charge - European distribution").
- amount_usd_m: the pre-tax amount in USD MILLIONS as a plain number (write 1250 for
  $$1.25 billion and 0.4 for $$400 thousand). Use null when the filing does not disclose an amount.
- fiscal_year: the fiscal year in which the item was recognised, as an integer (e.g. 2024).
  Use null if the year is unclear.
- direction: "add_back" for charges, losses and expenses that reduced reported EBITDA
  (adding them back raises normalised EBITDA); "deduct" for gains, recoveries and benefits
  that inflated reported EBITDA (removing them lowers normalised EBITDA).
- source_quote: a VERBATIM quote copied character-for-character from the text below,
  UNDER 15 WORDS, that names the item and ideally its amount. It is used to verify the item
  against the filing, so do not paraphrase, do not fix typos, do not add words.

HARD RULES
- Never invent numbers, years or items. Every item must be supported by its source_quote.
- If the section contains no such items, return {"items": []}.
- Amounts must be expressed in USD millions, never in thousands, billions or raw dollars.

===== 10-K SECTION TEXT BEGINS =====
$section_text
