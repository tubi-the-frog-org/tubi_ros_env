from animatronic_common.snacks import Line, Snack
from animatronic_brain.keyword_scorer import decide_keyword, normalize_text, score_snacks

COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=["biscuit"], tags=["sweet", "soft", "blue", "rain"],
                 lines=[Line(id="cookies_1", text="cookies!")])
CANDY = Snack(id="candy", label="Candy", slot=2, angle=130,
              aliases=["sweets"], tags=["sweet", "colorful"],
              lines=[Line(id="candy_1", text="candy!")])
CHIPS = Snack(id="chips", label="Chips", slot=3, angle=195,
              aliases=["crisps"], tags=["salty", "crunchy"],
              lines=[Line(id="chips_1", text="chips!")])
NONE = Snack(id="none", label="No snack", slot=0, angle=0,
             aliases=[], tags=[], lines=[Line(id="none_1", text="nothing.")])

SNACKS = [COOKIES, CANDY, CHIPS, NONE]


def test_normalize_lowercases_and_strips_punctuation():
    assert normalize_text("Crunchy, Salty!!") == "crunchy salty"


def test_normalize_collapses_whitespace():
    assert normalize_text("  a   b\tc  ") == "a b c"


def test_score_snacks_counts_tag_and_alias_matches():
    scores = score_snacks(normalize_text("something crunchy and salty"), SNACKS)
    assert scores["chips"] == 2  # salty + crunchy tags
    assert scores["cookies"] == 0
    assert scores["candy"] == 0
    assert scores["none"] == 0


def test_score_snacks_matches_multiword_alias_as_substring():
    scores = score_snacks(normalize_text("I want some sweets please"), SNACKS)
    assert scores["candy"] >= 1


def test_score_snacks_whole_word_only_no_partial_match():
    # "biscuity" should not match the "biscuit" alias as a substring
    scores = score_snacks(normalize_text("biscuity texture"), SNACKS)
    assert scores["cookies"] == 0


def test_decide_keyword_clears_threshold_and_margin():
    normalized = normalize_text("something crunchy and salty")
    result = decide_keyword(normalized, SNACKS, threshold=1, margin=1)
    assert result == "chips"


def test_decide_keyword_returns_none_below_threshold():
    normalized = normalize_text("just talking about nothing relevant")
    result = decide_keyword(normalized, SNACKS, threshold=1, margin=1)
    assert result is None


def test_decide_keyword_returns_none_when_margin_not_cleared():
    # "sweet" matches both cookies and candy tags equally -> no margin
    normalized = normalize_text("sweet")
    result = decide_keyword(normalized, SNACKS, threshold=1, margin=1)
    assert result is None


def test_decide_keyword_empty_snack_list_returns_none():
    assert decide_keyword("anything", [], threshold=1, margin=1) is None
