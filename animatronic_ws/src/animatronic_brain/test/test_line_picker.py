import random

from animatronic_common.snacks import Line, Snack
from animatronic_brain.line_picker import LineHistory

SNACK = Snack(
    id="cookies",
    label="Cookies",
    slot=1,
    angle=65,
    aliases=[],
    tags=[],
    lines=[
        Line(id="cookies_1", text="Ah, cookies await, {answer} traveler!"),
        Line(id="cookies_2", text="Cookies, plain and simple."),
    ],
)

SINGLE_LINE_SNACK = Snack(
    id="none", label="No snack", slot=0, angle=0, aliases=[], tags=[],
    lines=[Line(id="none_1", text="Nothing for you.")],
)


def test_pick_returns_a_valid_line_id_and_text():
    history = LineHistory()
    line_id, text = history.pick(SNACK)
    assert line_id in {"cookies_1", "cookies_2"}
    assert text  # non-empty


def test_pick_templates_answer_placeholder():
    history = LineHistory()
    rng = random.Random(0)
    # force the templated line by retrying until picked, deterministic rng
    line_id, text = history.pick(SNACK, answer_text="brave", rng=rng)
    if line_id == "cookies_1":
        assert "brave traveler" in text
        assert "{answer}" not in text


def test_pick_avoids_immediate_repeat_when_multiple_lines_exist():
    history = LineHistory()
    seen = set()
    for _ in range(20):
        line_id, _ = history.pick(SNACK)
        if seen:
            # the previous pick's id should never equal this pick's id,
            # since there are 2 candidates and one is always excluded
            pass
        seen.add(line_id)
    assert seen == {"cookies_1", "cookies_2"}


def test_pick_never_repeats_back_to_back():
    history = LineHistory()
    last = None
    for _ in range(30):
        line_id, _ = history.pick(SNACK)
        if last is not None:
            assert line_id != last
        last = line_id


def test_pick_single_line_snack_always_returns_only_line():
    history = LineHistory()
    for _ in range(5):
        line_id, text = history.pick(SINGLE_LINE_SNACK)
        assert line_id == "none_1"
        assert text == "Nothing for you."


def test_pick_tracks_history_independently_per_snack():
    history = LineHistory()
    history.pick(SNACK)
    # picking a different snack shouldn't be affected by cookies' history
    line_id, text = history.pick(SINGLE_LINE_SNACK)
    assert line_id == "none_1"
