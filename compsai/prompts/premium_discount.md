Company ticker: $ticker
Sector type: $sector_type (industrial = EV/Revenue, EV/EBITDA and P/E are the primary
multiples; bank = P/E and P/TBV are the primary multiples and EV-based multiples are not meaningful)

TRADING MULTIPLES VERSUS PEER MEDIAN
Multiples are plain numbers (12.3 means 12.3x); margins and growth are percentages;
"n/a" means not meaningful or not available.

$multiples_table

TASK
Using ONLY the multiples table above and the 10-K section text below (Management's
Discussion and Analysis followed by Risk Factors), assess whether the company deserves to
trade at a premium to, a discount to, or in line with the peer median, and explain why.
Ground every statement in the filing: growth commentary, guidance, margin trends,
competitive position and disclosed risks.

OUTPUT FORMAT
Return ONLY a JSON object with exactly this schema. No prose before or after it, no
markdown, no code fences.

{"growth_outlook": "1-3 sentences on revenue growth prospects, citing guidance or trends from the filing",
 "margin_trajectory": "1-3 sentences on where margins are heading and why",
 "key_risks": ["risk 1", "risk 2", "risk 3"],
 "premium_or_discount": "premium",
 "rationale": "2-4 sentences tying the multiples versus the peer median to the growth, margin and risk picture"}

FIELD RULES
- growth_outlook, margin_trajectory, rationale: plain strings. Use "unknown" if the text
  gives no basis for a view.
- key_risks: a list of 3 to 5 short strings, each a distinct risk from the filing. Use an
  empty list [] if none can be identified.
- premium_or_discount: exactly one of "premium", "discount" or "inline". Use "unknown" only
  if the multiples table is entirely n/a.
- rationale must reference the company's multiples versus the peer median (e.g. "trades at
  14.2x EV/EBITDA versus a 11.0x peer median") and say whether that gap is justified.

HARD RULES
- Never invent numbers. Quote figures only from the multiples table or the filing text.
- Do not speculate about information that is not in the material provided.

===== 10-K SECTION TEXT BEGINS =====
$section_text
