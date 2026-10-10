"""Step 1-2 of the brain pipeline: normalize text and score it against
each snack's aliases/tags/label. Pure Python, no ROS dependency.
"""

from __future__ import annotations

import re

from animatronic_common.snacks import Snack

_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    text = text.lower()
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def score_snacks(normalized_text: str, snacks: list[Snack]) -> dict[str, int]:
    """Score every snack by how many of its aliases/tags/label appear in
    the normalized text. Multi-word keywords match as a substring;
    single-word keywords match as a whole word."""
    words = set(normalized_text.split())
    scores: dict[str, int] = {}
    for snack in snacks:
        keywords = [*snack.aliases, *snack.tags, snack.label]
        score = 0
        for kw in keywords:
            kw_norm = normalize_text(kw)
            if not kw_norm:
                continue
            if " " in kw_norm:
                if kw_norm in normalized_text:
                    score += 1
            elif kw_norm in words:
                score += 1
        scores[snack.id] = score
    return scores


def decide_keyword(
    normalized_text: str,
    snacks: list[Snack],
    threshold: int,
    margin: int,
) -> str | None:
    """Return a snack id if the top score clears `threshold` with at
    least `margin` over the second-best score; otherwise None (meaning:
    not confident enough, fall through to the LLM)."""
    scores = score_snacks(normalized_text, snacks)
    if not scores:
        return None

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_id, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    if best_score >= threshold and (best_score - second_score) >= margin:
        return best_id
    return None
