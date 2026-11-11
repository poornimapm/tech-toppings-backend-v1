"""Money for an AI model: every ``_minor`` figure in the main unit, by the currency's decimals."""

from __future__ import annotations

from app.core.money import in_major


def test_minor_figures_become_main_units_all_the_way_down() -> None:
    figures = {
        "currency": "JPY",
        "budget_minor": 50000,
        "remaining_minor": None,
        "categories": [{"category": "food", "actual_minor": 1200}],
        "count": 3,
    }

    assert in_major(figures, "JPY") == {
        "currency": "JPY",
        "budget": 50000.0,
        "remaining": None,
        "categories": [{"category": "food", "actual": 1200.0}],
        "count": 3,
    }
    assert in_major({"spent_minor": 25050}, "INR") == {"spent": 250.5}
    assert in_major([{"owed_minor": 1235}], "KWD") == [{"owed": 1.235}]
