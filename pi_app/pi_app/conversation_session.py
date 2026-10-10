"""One guest's multi-turn conversation with Tubi: intro -> guest's own
intro -> open-ended questions -> reveal with percentages, once Tubi is
confident (no "are you ready?" gate -- he decides on his own, like a
friend reading someone's vibe rather than a quiz show waiting to be
told to give the answer). The LLM itself drives the flow (what to ask,
when it's confident enough, which snack(s) fit) via
DecisionClient.converse() -- this class just owns the growing chat
history and turns the final answer into a JSON-able QuizResult. A
guest can match more than one snack -- the reveal can name anywhere
from one up to all of them.

Percentages are the model's own holistic read of the whole
conversation, not a mechanical point tally -- in the spirit of the
IFT/EmoMap research this was asked to follow (open-ended elicitation
rather than forcing answers onto a fixed predetermined scale), though
it's worth being honest that an LLM's holistic judgment call is not
the same rigor as a validated psychometric instrument. Each snack's
percentage is independent (not a split of 100 across all snacks), since
a guest can genuinely score high on more than one at once.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from pi_app.decision_client import DecisionClient, DecisionError, build_system_prompt
from pi_app.snacks import SnacksConfig

# Varied and in-character (Tubi's whole bit is a forgetful wizard) rather than
# one fixed line -- repeating the exact same sentence on consecutive failures
# reads as broken, not charming (this happened in a real test run: the same
# apology three times in a row looked like a glitch, not a bit).
_GOOFY_RETRY_REPLIES = [
    "Oh dear, my enchanted earwax clogged up again -- what was that, traveler?",
    "Hmm, a pond toad nibbled your words right out of the air! Say it once more?",
    "My memory crystal just fogged over -- mind repeating that for an old frog?",
    "A passing cloud swallowed your sentence whole! One more time, please?",
    "My ears are full of lily pads today -- could you say that again, traveler?",
]
_FORCE_FINALIZE_REPLY_TEMPLATE = "My crystal ball flickers and settles... {label} it shall be, traveler!"
# Question topics for the either/or phase. Left to itself the model reuses
# the same two or three questions every session, so each session gets its own
# shuffled pool and is told to work through it in order.
_QUESTION_BANK = [
    "early bird or night owl",
    "cats or dogs",
    "beach or mountains",
    "plan everything or wing it",
    "loud party or cozy night in",
    "sweet tooth or salty soul",
    "book or movie",
    "city or countryside",
    "summer or winter",
    "talk first or listen first",
    "adventure or comfort",
    "dance floor or karaoke mic",
    "tidy desk or creative chaos",
    "sunrise or sunset",
    "coffee or tea",
    "thrill ride or lazy river",
]

_START_PROMPT = (
    "(The guest has just arrived and hasn't said anything yet. Begin with your INTRO step: one short sentence "
    "introducing yourself with a froggy sound, a quick hint of what you'll do together, and ask their name. "
    "Do not mention snacks.)"
)

# A prompt instruction alone ("ask 2-4 questions") isn't reliably enough --
# measured a real run asking 10+ questions in a row, never reaching done.
# This is a demo: a bored judge matters more than one extra clever question.
# So the question count is tracked and enforced in code, with two rungs:
# a nudge first (model still has a say), then a structurally-forced reveal
# (it doesn't) if that didn't work.
_NUDGE_AFTER_TURNS = 6
_FORCE_AFTER_TURNS = 8
_WRAP_UP_NUDGE = (
    "(Internal note, not from the guest -- you've asked enough questions now. You have enough information to "
    "decide. Do not ask any more questions -- set done=true this turn, choose whichever snack(s) genuinely "
    "fit, and reveal.)"
)


@dataclass(frozen=True)
class ChosenSnack:
    id: str
    slot: int
    label: str


@dataclass(frozen=True)
class QuizResult:
    session_id: str
    chosen_snacks: list[ChosenSnack]  # one or more, up to all configured snacks
    percentages: dict[str, float]  # independent 0-100 confidence per snack id, not a split of 100
    transcript: list[dict]  # [{"role": "user"|"assistant", "content": ...}, ...], no system prompt


class ConversationSession:
    def __init__(
        self,
        session_id: str,
        client: DecisionClient,
        snacks: SnacksConfig,
        logger: logging.Logger | None = None,
    ):
        self.session_id = session_id
        self._client = client
        self._snacks = snacks
        self._logger = logger or logging.getLogger(__name__)

        # The default/fallback snack (e.g. "none") is reserved for the
        # orchestration layer's own fallback (budget exhausted, DecisionError)
        # -- it's never a genuine quiz outcome. Excluding it from both the
        # prompt's listing and the schema's enum makes that a hard guarantee
        # (the model structurally can't pick it) rather than a soft
        # instruction it might ignore; it also sidesteps an awkward mention
        # check, since "No snack" doesn't naturally fit into a reveal line.
        self._choosable_snacks = [s for s in snacks.snacks if s.id != snacks.default_snack]
        if not self._choosable_snacks:
            self._logger.warning(f"[{session_id}] no non-default snacks configured; falling back to the full list")
            self._choosable_snacks = snacks.snacks

        pool = random.sample(_QUESTION_BANK, 6)
        system_prompt = build_system_prompt(self._choosable_snacks) + (
            "\n\nQUESTION POOL for this guest, in order (phrase them your own goofy way): " + "; ".join(pool) + "."
        )
        self._messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.done = False
        self.result: QuizResult | None = None
        self._turn_count = 0  # counts turns after the intro; the intro/name-exchange itself doesn't count

    def start(self) -> str:
        """Call once per session, before any guest speech, to get Tubi's opening line."""
        return self._step(_START_PROMPT, is_intro=True)

    def respond(self, guest_text: str) -> str:
        if self.done:
            raise RuntimeError(f"session {self.session_id} already finished")
        return self._step(guest_text)

    def _step(self, user_text: str, is_intro: bool = False) -> str:
        self._messages.append({"role": "user", "content": user_text})

        force_done = self._turn_count >= _FORCE_AFTER_TURNS
        send_messages = self._messages
        if not is_intro and self._turn_count >= _NUDGE_AFTER_TURNS:
            send_messages = [*self._messages, {"role": "system", "content": _WRAP_UP_NUDGE}]

        try:
            turn = self._client.converse(send_messages, self._choosable_snacks, force_done=force_done)
        except DecisionError as e:
            self._messages.pop()  # don't poison history with a turn that never happened
            if force_done:
                # We've already given the model every chance (a nudge, then a
                # structurally-forced done=true) and it still didn't commit to
                # a real snack (e.g. it can still say snack="undecided" even
                # with done locked true -- that field's enum isn't locked).
                # A failed call never advances _turn_count, so simply retrying
                # here could loop forever. A demo must never hang on an LLM
                # that won't cooperate, so finish the session locally instead.
                self._logger.warning(f"[{self.session_id}] forced reveal still failed ({e}); finalizing locally")
                return self._finalize_locally()
            self._logger.warning(f"[{self.session_id}] conversation call failed: {e}")
            return random.choice(_GOOFY_RETRY_REPLIES)

        self._messages.append({"role": "assistant", "content": turn.reply_text})
        if not is_intro:
            self._turn_count += 1

        if turn.done:
            self.done = True
            chosen = [self._snacks.get(sid) for sid in turn.snack_ids]
            self.result = QuizResult(
                session_id=self.session_id,
                chosen_snacks=[ChosenSnack(id=s.id, slot=s.slot, label=s.label) for s in chosen],
                percentages=turn.percentages or {},
                transcript=[m for m in self._messages if m["role"] != "system"],
            )

        return turn.reply_text

    def _finalize_locally(self) -> str:
        """Last-resort termination, no further LLM call: pick one snack
        ourselves (random, since we have no real signal the model failed
        to give us) and finish the session. Guarantees a demo never hangs
        on an uncooperative model. Deliberately picks just one here (not
        the full multi-snack feature) -- this is the degenerate error path,
        not worth the extra complexity."""
        snack = random.choice(self._choosable_snacks)
        self.done = True
        even_share = round(100.0 / len(self._choosable_snacks), 1)
        self.result = QuizResult(
            session_id=self.session_id,
            chosen_snacks=[ChosenSnack(id=snack.id, slot=snack.slot, label=snack.label)],
            percentages={s.id: even_share for s in self._choosable_snacks},
            transcript=[m for m in self._messages if m["role"] != "system"],
        )
        return _FORCE_FINALIZE_REPLY_TEMPLATE.format(label=snack.label)
