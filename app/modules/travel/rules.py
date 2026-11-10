"""Travel's tunables (data/travel.yaml)."""

from __future__ import annotations

from enum import StrEnum
from functools import cache
from math import ceil
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DATA = Path(__file__).parent / "data" / "travel.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackingBag(StrEnum):
    CHECK_IN = "check_in"
    CABIN = "cabin"
    HANDBAG = "handbag"
    DAY_BAG = "day_bag"
    TOILETRY = "toiletry"
    ELECTRONICS = "electronics"


class NextAction(StrEnum):
    """What a trip's overview can say to do next (overview.py: when each applies)."""

    TRIP_ENDED = "trip_ended"
    ADD_ACTIVITIES = "add_activities"
    PREPARE_TODAY = "prepare_today"
    PREPARE_TOMORROW = "prepare_tomorrow"
    CONFIRM_BOOKINGS = "confirm_bookings"
    FINISH_PACKING = "finish_packing"
    START_PACKING = "start_packing"
    FINISH_CHECKLIST = "finish_checklist"
    ADD_TASKS = "add_tasks"
    ALL_SET = "all_set"


class TripRules(_Strict):
    max_days: int = Field(ge=1)
    max_per_user: int = Field(ge=1)
    max_destinations: int = Field(ge=1)
    max_travellers: int = Field(ge=1)
    max_interests: int = Field(ge=1)


class ActivityRules(_Strict):
    kinds: tuple[str, ...] = Field(min_length=1)
    transport_kinds: tuple[str, ...]
    max_per_trip: int = Field(ge=1)
    max_duration_minutes: int = Field(ge=1)


class ChecklistRules(_Strict):
    max_per_trip: int = Field(ge=1)
    max_quantity: int = Field(ge=1)


class BookingRules(_Strict):
    max_per_trip: int = Field(ge=1)


class BudgetRules(_Strict):
    categories: tuple[str, ...] = Field(min_length=1)
    max_expenses_per_trip: int = Field(ge=1)
    fx_rest_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def names_each_category_once(self) -> Self:
        if len(set(self.categories)) != len(self.categories):
            raise ValueError(f"a category comes twice in {self.categories}")
        return self


class OverviewRules(_Strict):
    next_activities: int = Field(ge=1)
    next_actions: tuple[NextAction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def always_has_an_answer(self) -> Self:
        if len(set(self.next_actions)) != len(self.next_actions):
            raise ValueError("a next action comes twice")
        if self.next_actions[-1] is not NextAction.ALL_SET:
            raise ValueError("all_set comes last: it always applies")
        return self


class SuggestedItem(_Strict):
    """A thing worth suggesting: one of it, or one for every ``per_days`` days of the trip,
    ``up_to`` a limit."""

    key: str = Field(pattern=r"^[a-z_]+$")
    title: str = Field(min_length=1, max_length=120)
    bag: PackingBag | None = None
    per_days: int | None = Field(default=None, ge=1)
    up_to: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def scales_up_to_a_limit(self) -> Self:
        if (self.per_days is None) != (self.up_to is None):
            raise ValueError(f"{self.key}: per_days and up_to come together")
        return self

    def quantity_for(self, days: int) -> int:
        if self.per_days is None or self.up_to is None:
            return 1
        return min(ceil(days / self.per_days), self.up_to)


def _once_each(items: tuple[SuggestedItem, ...]) -> None:
    keys = [item.key for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError(f"a key comes twice in {keys}")


class SuggestionRules(_Strict):
    max_per_run: int = Field(ge=1)
    activities: dict[str, tuple[SuggestedItem, ...]]
    packing: tuple[SuggestedItem, ...]

    @model_validator(mode="after")
    def suggests_each_thing_once(self) -> Self:
        for items in (self.packing, *self.activities.values()):
            _once_each(items)
        return self

    def words_of(self, key: str | None) -> set[str]:
        """How the rule a suggestion's key ends in words it ("activity:ab12:water_bottle")."""
        rule = (key or "").rsplit(":", 1)[-1]
        every = (item for items in (self.packing, *self.activities.values()) for item in items)
        return {item.title for item in every if item.key == rule}


class Rules(_Strict):
    trips: TripRules
    activities: ActivityRules
    checklist: ChecklistRules
    bookings: BookingRules
    budget: BudgetRules
    overview: OverviewRules
    suggestions: SuggestionRules

    @model_validator(mode="after")
    def names_only_known_kinds(self) -> Self:
        named = {*self.suggestions.activities, *self.activities.transport_kinds}
        unknown = named - set(self.activities.kinds)
        if unknown:
            raise ValueError(f"not kinds of activity: {sorted(unknown)}")
        return self


@cache
def rules() -> Rules:
    return Rules.model_validate(yaml.safe_load(DATA.read_text(encoding="utf-8")))
