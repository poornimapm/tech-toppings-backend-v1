"""A day's attendance said or typed ("Ravi full day, Meena half day, the rest absent"), read into
a draft of marks for the owner to check (the mark-attendance skill's static mode). Nothing is
saved here: the page shows the draft, and the owner saves it.

The words are data (the skill's ``references/attendance.yaml``), in English, Tamil and Hindi.
Workers are found by name (whole, by first name, or close to it) or by number ("number 3",
"W003"). Each mark word goes with the names before it ("Ravi, Meena full day"), or, when the
words start with a mark, with the names after it ("full day: Ravi, Meena"). A name heard but
matching nobody, a match that is only close, and a name without a mark are handed over marked
uncertain, for the owner to settle."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.platform import normalise, western_digits, word_pattern

STATUSES = ("full", "half", "absent")
MOST_UNKNOWN_WORDS = 3  # a longer run of unknown words is talk, not a name
SHORTEST_NAME = 2  # a single letter matches too much to be a name
INDIC = "ऀ-ॿ஀-௿"  # Devanagari and Tamil letters
# Sentences are read one by one: each may start with its mark or end with it. Commas don't end
# one ("Ravi, Meena full day"); full stops, semicolons, new lines and the danda (।) do.
SENTENCE_END = re.compile(r"[.;\n।]+")


@dataclass(frozen=True)
class MarkWords:
    tool: str
    statuses: Mapping[str, re.Pattern[str]]
    rest: re.Pattern[str]
    everyone: re.Pattern[str]
    days: Mapping[int, re.Pattern[str]]
    number: re.Pattern[str]  # group 1: the worker's number
    fillers: frozenset[str]
    similar_at: float


@cache
def load_words(folder: Path) -> MarkWords:
    data: dict[str, Any] = yaml.safe_load((folder / "attendance.yaml").read_text(encoding="utf-8"))
    numbers = sorted((normalise(str(word)) for word in data["number"]), key=len, reverse=True)
    lead = "|".join(re.escape(word) for word in numbers)
    return MarkWords(
        tool=data["tool"],
        statuses={status: word_pattern(data["statuses"][status]) for status in STATUSES},
        rest=word_pattern(data["rest"]),
        everyone=word_pattern(data["everyone"]),
        days={int(back): word_pattern(words) for back, words in data["days"].items()},
        number=re.compile(rf"(?<![\w{INDIC}])(?:{lead})\.?\s*0*(\d{{1,4}})(?!\d)"),
        # "number 7" with no worker 7 is neither a name: its words are fillers too.
        fillers=frozenset(normalise(str(word)) for word in [*data["fillers"], *numbers]),
        similar_at=float(data["similar_at"]),
    )


@dataclass(frozen=True)
class RosterWorker:
    id: str
    name: str
    code: str


@dataclass
class DraftMark:
    worker_id: str | None
    name: str  # the worker's own name, or the words heard when no one matched
    status: str | None = None
    uncertain: list[str] = field(default_factory=list)  # "worker", "status"


@dataclass
class MarksDraft:
    marks: list[DraftMark] = field(default_factory=list)
    rest: str | None = None  # for every worker not named
    days_back: int | None = None  # said ("yesterday"); None: the page's day

    @property
    def empty(self) -> bool:
        return not self.marks and self.rest is None

    def arguments(self, project_id: str | None, day: date | None) -> dict[str, Any]:
        """The draft as the ``labor_attendance_draft`` client tool takes it."""
        return {
            "project_id": project_id,
            "day": day.isoformat() if day else None,
            "marks": [
                {
                    "worker_id": mark.worker_id,
                    "name": mark.name,
                    "status": mark.status,
                    "uncertain": mark.uncertain,
                }
                for mark in self.marks
            ],
            "rest": self.rest,
            "uncertain": [],
        }


@dataclass(frozen=True)
class _Found:
    start: int
    end: int
    kind: str  # a status, "rest", "everyone", "worker", "unknown", "day"
    value: Any = None


def _spans(pattern: re.Pattern[str], text: str, kind: str, value: Any = None) -> list[_Found]:
    return [_Found(m.start(), m.end(), kind, value) for m in pattern.finditer(text)]


def _longest(found: Iterable[_Found]) -> list[_Found]:
    """Overlapping finds: the longer one counts ("नहीं आया" over "आया")."""
    kept: list[_Found] = []
    for item in sorted(found, key=lambda f: (f.start, f.start - f.end)):
        if kept and item.start < kept[-1].end:
            if item.end - item.start > kept[-1].end - kept[-1].start:
                kept[-1] = item
            continue
        kept.append(item)
    return kept


def _plain(text: str) -> str:
    """Normalised, digits made western, punctuation spaced out (names keep their letters)."""
    text = western_digits(normalise(text))
    return "".join(
        letter if letter.isalnum() or letter.isspace() or _is_mark(letter) else " "
        for letter in text
    )


def _is_mark(letter: str) -> bool:
    """A combining sign: part of a Tamil or Hindi letter, never punctuation."""
    return unicodedata.category(letter).startswith("M") or letter in "'"


class _Roster:
    def __init__(self, workers: Sequence[RosterWorker], words: MarkWords) -> None:
        self.workers = workers
        self.words = words
        self.by_number = {
            number: worker for worker in workers if (number := _number(worker.code)) is not None
        }
        firsts = [normalise(worker.name).split(" ")[0] for worker in workers]
        self.shared = {first for first in firsts if firsts.count(first) > 1}

    def find(self, text: str) -> list[_Found]:
        """Workers named in ``text``: by number, whole name, or first name (uncertain when
        another worker has it too)."""
        found: list[_Found] = []
        for match in self.words.number.finditer(text):
            worker = self.by_number.get(int(match.group(1)))
            if worker:
                found.append(_Found(match.start(), match.end(), "worker", (worker, False)))
        for worker in self.workers:
            whole = normalise(worker.name)
            first = whole.split(" ")[0] if " " in whole else ""
            for name in (whole, first):
                if len(name) < SHORTEST_NAME:
                    continue
                unsure = name == first and first in self.shared
                spans = _spans(word_pattern([name]), text, "worker", (worker, unsure))
                found += spans
                if spans:
                    break
        return found

    def close(self, heard: str) -> RosterWorker | None:
        """The one worker whose name (or first name) is close to what was heard, if any."""
        best: tuple[float, RosterWorker | None] = (0.0, None)
        for worker in self.workers:
            names = {normalise(worker.name), normalise(worker.name).split(" ")[0]}
            score = max(SequenceMatcher(None, heard, name).ratio() for name in names)
            if score > best[0]:
                best = (score, worker)
        return best[1] if best[0] >= self.words.similar_at else None


def _number(code: str) -> int | None:
    digits = re.findall(r"\d+", code)
    return int(digits[-1]) if digits else None


def _unknown(text: str, taken: list[_Found], words: MarkWords) -> list[_Found]:
    """Runs of words between the finds that are neither fillers nor anything known: perhaps a
    name the roster doesn't have (or heard otherwise), for the owner to settle."""
    found: list[_Found] = []
    edges = [0, *[edge for item in taken for edge in (item.start, item.end)], len(text)]
    for start, end in zip(edges[::2], edges[1::2], strict=True):
        run: list[tuple[int, int]] = []
        for match in re.finditer(rf"[\w{INDIC}']+", text[start:end]):
            word = match.group(0)
            if word in words.fillers or word.isdigit():
                if run:
                    found.append(_run(text, run))
                run = []
                continue
            run.append((start + match.start(), start + match.end()))
        if run:
            found.append(_run(text, run))
    return [item for item in found if len(str(item.value).split()) <= MOST_UNKNOWN_WORDS]


def _run(text: str, run: list[tuple[int, int]]) -> _Found:
    return _Found(run[0][0], run[-1][1], "unknown", text[run[0][0] : run[-1][1]])


def _finds(sentence: str, roster: _Roster, words: MarkWords) -> list[_Found]:
    """What one sentence says, in order: marks, names, numbers, "the rest", days, and words
    that may be names."""
    known = [*_spans(words.rest, sentence, "rest"), *_spans(words.everyone, sentence, "everyone")]
    for status in STATUSES:
        known += _spans(words.statuses[status], sentence, status)
    for back, pattern in words.days.items():
        known += _spans(pattern, sentence, "day", back)
    known += roster.find(sentence)
    taken = _longest(known)
    return _longest([*taken, *_unknown(sentence, taken, words)])


def read_marks(text: str, workers: Sequence[RosterWorker], words: MarkWords) -> MarksDraft:
    """The marks said in ``text`` for these workers (the project's, for the day). Without a
    single mark word nothing is read: a name alone ("Ravi paid 500") isn't attendance."""
    roster = _Roster(workers, words)
    sentences = [_finds(_plain(part), roster, words) for part in SENTENCE_END.split(text)]
    draft = MarksDraft()
    days = [item.value for items in sentences for item in items if item.kind == "day"]
    draft.days_back = days[0] if days else None
    if not any(item.kind in STATUSES for items in sentences for item in items):
        return draft
    named = any(item.kind in {"worker", "unknown"} for items in sentences for item in items)
    by_worker: dict[str, DraftMark] = {}
    for items in sentences:
        _assign([item for item in items if item.kind != "day"], draft, roster, named, by_worker)
    draft.marks = [*by_worker.values(), *draft.marks]
    return draft


def _assign(
    items: list[_Found],
    draft: MarksDraft,
    roster: _Roster,
    named: bool,
    by_worker: dict[str, DraftMark],
) -> None:
    """Each mark word to its names: those before it, or after it when the sentence starts with
    one. A name said twice keeps its last mark."""
    status_first = bool(items) and items[0].kind in STATUSES
    current: str | None = None
    waiting: list[_Found] = []

    def give(item: _Found, status: str | None) -> None:
        if item.kind in {"rest", "everyone"}:
            if item.kind == "rest" or not named:
                draft.rest = status
            return
        mark = _mark(item, roster)
        mark.status = status
        if status is None:
            mark.uncertain.append("status")
        if mark.worker_id is None:
            draft.marks.append(mark)
            return
        by_worker[mark.worker_id] = mark

    for item in items:
        if item.kind in STATUSES:
            current = item.kind
            if not status_first:
                for each in waiting:
                    give(each, current)
                waiting = []
            continue
        if status_first:
            give(item, current)
        else:
            waiting.append(item)
    for each in waiting:  # names after the sentence's last mark word, with none of their own
        give(each, None)


def _mark(item: _Found, roster: _Roster) -> DraftMark:
    if item.kind == "worker":
        worker, partly = item.value
        return DraftMark(worker.id, worker.name, uncertain=["worker"] if partly else [])
    close = roster.close(str(item.value))
    if close is not None:
        return DraftMark(close.id, close.name, uncertain=["worker"])
    return DraftMark(None, str(item.value), uncertain=["worker"])


def speaks_of_marks(text: str, words: MarkWords) -> bool:
    """Whether ``text`` says a mark at all: asked without the page ("Ravi paid 500" is the
    expenses skill's), the reader takes only words that do."""
    plain = _plain(text)
    return any(words.statuses[status].search(plain) for status in STATUSES)


def roster_of(rows: Iterable[Mapping[str, Any]]) -> list[RosterWorker]:
    """The workers as the ``labor_day`` tool lists them."""
    return [RosterWorker(str(row["id"]), str(row["name"]), str(row["code"])) for row in rows]


def day_said(draft: MarksDraft, today: date, given: date | None) -> date:
    """The day the marks are for: the one said, else the page's, else today."""
    if draft.days_back is not None:
        return today - timedelta(days=draft.days_back)
    return given or today
