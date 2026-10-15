"""
Cost text -> structured cents. Deterministic, no LLM, no venue priors — the
engine stores only PARSED FACT (priors like "library events are free" are
persona-layer inference and live in thirdplace). Called by ingest at upsert.
"""
from __future__ import annotations

import re

FREE_WORDS = re.compile(r"\b(free|no\s+charge|no\s+cover)\b", re.I)
DONATION = re.compile(r"\b(donation|pay[\s-]+what[\s-]+you[\s-]+(can|wish)|sliding\s+scale|suggested)\b", re.I)
# '$16' / '$16.17' (sign required) OR decimal-formatted '0.00'/'15.00' (how
# adapters emit numeric costs). A bare integer is NOT money — "doors at 8" is
# a time (caught by tests.py::test_parse_cost).
MONEY = re.compile(r"\$\s*(\d{1,4}(?:\.\d{2})?)|\b(\d{1,4}\.\d{2})\b")


def parse_cost(text: str | None) -> tuple[int | None, int | None, bool | None]:
    """-> (min_cents, max_cents, is_free); all None = no signal in the text.
    Donation / pay-what-you-can counts as free (min 0)."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    if FREE_WORDS.search(text) or DONATION.search(text):
        return 0, 0, True
    amounts = [round(float(a or b) * 100) for a, b in MONEY.findall(text)]
    if amounts:
        lo, hi = min(amounts), max(amounts)
        return lo, hi, lo == 0
    return None, None, None
