"""Step 5 of the brain pipeline: pick a pre-written line for a decided
snack, avoiding an immediate repeat, with {answer} templating.
"""

from __future__ import annotations

import random

from animatronic_common.snacks import Snack


class LineHistory:
    """Tracks the last line id served per snack id so repeats can be
    avoided across consecutive requests for the same snack."""

    def __init__(self):
        self._last_line_id: dict[str, str] = {}

    def pick(self, snack: Snack, answer_text: str = "", rng: random.Random | None = None) -> tuple[str, str]:
        rng = rng or random
        candidates = snack.lines

        last_id = self._last_line_id.get(snack.id)
        if last_id is not None and len(candidates) > 1:
            filtered = [line for line in candidates if line.id != last_id]
            if filtered:
                candidates = filtered

        chosen = rng.choice(candidates)
        self._last_line_id[snack.id] = chosen.id
        text = chosen.text.replace("{answer}", answer_text)
        return chosen.id, text
