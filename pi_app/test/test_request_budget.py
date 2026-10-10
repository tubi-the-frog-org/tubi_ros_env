import datetime

from pi_app.request_budget import CharacterBudget, RequestBudget


def test_unlimited_by_default():
    budget = RequestBudget()
    for _ in range(50):
        assert budget.try_consume() is True


def test_daily_limit_exhausts():
    budget = RequestBudget(daily_limit=3)
    assert [budget.try_consume() for _ in range(5)] == [True, True, True, False, False]
    assert budget.daily_count == 3


def test_total_limit_exhausts_even_across_days():
    day = [datetime.date(2026, 1, 1)]
    budget = RequestBudget(total_limit=2, today_fn=lambda: day[0])
    assert budget.try_consume() is True
    day[0] = datetime.date(2026, 1, 2)  # new day, daily counter would reset, but total shouldn't
    assert budget.try_consume() is True
    assert budget.try_consume() is False


def test_daily_limit_resets_on_new_day():
    day = [datetime.date(2026, 1, 1)]
    budget = RequestBudget(daily_limit=1, today_fn=lambda: day[0])
    assert budget.try_consume() is True
    assert budget.try_consume() is False
    day[0] = datetime.date(2026, 1, 2)
    assert budget.try_consume() is True


def test_failed_consume_does_not_increment_counts():
    budget = RequestBudget(daily_limit=1)
    budget.try_consume()
    budget.try_consume()  # fails
    assert budget.daily_count == 1
    assert budget.total_count == 1


# ---- CharacterBudget ----

def test_character_budget_unlimited_by_default():
    budget = CharacterBudget()
    assert budget.try_consume(10_000) is True


def test_character_budget_daily_limit_exhausts():
    budget = CharacterBudget(daily_limit=100)
    assert budget.try_consume(60) is True
    assert budget.try_consume(60) is False  # would exceed 100
    assert budget.try_consume(40) is True  # exactly fills to 100
    assert budget.daily_chars == 100


def test_character_budget_resets_on_new_day():
    day = [datetime.date(2026, 1, 1)]
    budget = CharacterBudget(daily_limit=50, today_fn=lambda: day[0])
    assert budget.try_consume(50) is True
    assert budget.try_consume(1) is False
    day[0] = datetime.date(2026, 1, 2)
    assert budget.try_consume(50) is True


def test_character_budget_total_limit_persists_across_days():
    day = [datetime.date(2026, 1, 1)]
    budget = CharacterBudget(total_limit=50, today_fn=lambda: day[0])
    assert budget.try_consume(30) is True
    day[0] = datetime.date(2026, 1, 2)
    assert budget.try_consume(30) is False  # would exceed the 50 total
