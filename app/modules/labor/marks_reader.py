"""A day's attendance said or typed ("Ravi full day, Meena half day, the rest absent"), read into
a draft of marks for the owner to check (the mark-attendance skill's static mode). Nothing is
saved here: the page shows the draft, and the owner saves it.

The words are data (the skill's ``references/attendance.yaml``), in English, Tamil and Hindi.
Workers are found by name (whole, by first name, or close to it) or by number ("number 3",
"W003"). Each mark word goes with the names before it ("Ravi, Meena full day"); a sentence that
starts with a mark gives it to the names after it, each comma part going on from the last mark
("full day: Ravi, Meena, Lakshmi absent"). A general word ("came") gives way to a mark said
right after it ("Ravi came, half day"). Two workers with the same name or number, a name only
close to a worker's, one matching nobody, and a name without a mark are handed over marked
uncertain, for the owner to settle.

Every worker's name is matched by one pattern built once for the list, and the words read are
bounded (``reading.max_chars``, ``most_unknown``): a long text costs a few scans, not one per
worker."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
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
PRESENT = "present"  # a general word for "came": a full day, unless a mark follows it
REST = "rest"  # what "the rest" was given to
MOST_UNKNOWN_WORDS = 3  # a longer run of unknown words is talk, not a name
NAME_MAX = 80  # as the draft's names (DraftMarkIn.name)
SHORTEST_NAME = 2  # a single letter matches too much to be a name
INDIC = "ऀ-ॿ஀-௿"  # Devanagari and Tamil letters
# The curly apostrophes phones type (U+2019, U+2018, U+02BC) are read as the plain one.
APOSTROPHES = str.maketrans({chr(0x2019): "'", chr(0x2018): "'", chr(0x02BC): "'"})
# Sentences are read one by one: each may start with its mark or end with it. Commas don't end
# one ("Ravi, Meena full day"); full stops (not an initial's: "K. Murugan"), semicolons, new
# lines and the danda (।) do.
SENTENCE_END = re.compile(r"(?<!\b\w)\.+|[;\n।]+")


@dataclass(frozen=True)
class MarkWords:
    tool: str
    statuses: Mapping[str, re.Pattern[str]]
    present: re.Pattern[str]
    rest: re.Pattern[str]  # "the rest", and "everyone"
    days: Mapping[int, re.Pattern[str]]
    number: re.Pattern[str]  # group 1: the worker's number
    fillers: frozenset[str]
    filler: re.Pattern[str]
    similar_at: float
    most_unknown: int


@cache
def load_words(folder: Path) -> MarkWords:
    data: dict[str, Any] = yaml.safe_load((folder / "attendance.yaml").read_text(encoding="utf-8"))
    numbers = sorted((normalise(str(word)) for word in data["number"]), key=len, reverse=True)
    lead = "|".join(re.escape(word) for word in numbers)
    # "number 7" with no worker 7 is no name either: its words are fillers too.
    fillers = [normalise(str(word)) for word in [*data["fillers"], *numbers]]
    return MarkWords(
        tool=data["tool"],
        statuses={status: word_pattern(data["statuses"][status]) for status in STATUSES},
        present=word_pattern(data["present"]),
        rest=word_pattern([*data["rest"], *data["everyone"]]),
        days={int(back): word_pattern(words) for back, words in data["days"].items()},
        number=re.compile(rf"(?<![\w{INDIC}])(?:{lead})\.?\s*0*(\d{{1,4}})(?!\d)"),
        fillers=frozenset(fillers),
        filler=word_pattern(fillers),
        similar_at=float(data["similar_at"]),
        most_unknown=int(data["most_unknown"]),
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

    @property
    def sure(self) -> bool:
        """Something read for certain: a worker matched without doubt, or "the rest"."""
        sure = (m.worker_id and "worker" not in m.uncertain and m.status for m in self.marks)
        return self.rest is not None or any(sure)

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
    kind: str  # a status, PRESENT, "rest", "worker", "unknown", "day", "filler"
    value: Any = None

    @property
    def status(self) -> str | None:
        """The mark this find says, if it says one (a general word is a full day)."""
        if self.kind == PRESENT:
            return "full"
        return self.kind if self.kind in STATUSES else None


def _spans(pattern: re.Pattern[str], text: str, kind: str, value: Any = None) -> list[_Found]:
    return [_Found(m.start(), m.end(), kind, value) for m in pattern.finditer(text)]


def _longest(found: Iterable[_Found]) -> list[_Found]:
    """Overlapping finds: the longer one counts ("नहीं आया" over "आया"); of two as long, the one
    found first (a worker's name over a filler)."""
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
    text = western_digits(normalise(text.translate(APOSTROPHES)))
    return "".join(
        letter if letter.isalnum() or letter.isspace() or _is_mark(letter) else " "
        for letter in text
    )


def _name(text: str) -> str:
    """A name as the text is read: plain, its spaces single ("K. Murugan" is "k murugan")."""
    return " ".join(_plain(text).split())


def _is_mark(letter: str) -> bool:
    """A combining sign: part of a Tamil or Hindi letter, never punctuation."""
    return unicodedata.category(letter).startswith("M") or letter in "'"


def _number(code: str) -> int | None:
    digits = re.findall(r"\d+", code)
    return int(digits[-1]) if digits else None


class _Roster:
    """The workers, matched by one pattern of every name and first name, and by number."""

    def __init__(self, workers: Sequence[RosterWorker], words: MarkWords) -> None:
        self.workers = workers
        self.words = words
        self.by_name: dict[str, list[tuple[RosterWorker, bool]]] = defaultdict(list)
        self.by_number: dict[int, list[RosterWorker]] = defaultdict(list)
        self.names: list[tuple[RosterWorker, set[str]]] = []
        for worker in workers:
            whole = _name(worker.name)
            first = whole.split(" ")[0] if " " in whole else ""
            for name, partly in ((whole, False), (first, True)):
                if len(name) >= SHORTEST_NAME:
                    self.by_name[name].append((worker, partly))
            self.names.append((worker, {whole, first} - {""}))
            if (number := _number(worker.code)) is not None:
                self.by_number[number].append(worker)
        self.pattern = word_pattern(self.by_name)
        self.close_ones: dict[str, RosterWorker | None] = {}

    def find(self, text: str) -> list[_Found]:
        """Workers named in ``text``, by name or number: uncertain when the name or number is
        more than one worker's."""
        found: list[_Found] = []
        for match in self.words.number.finditer(text):
            same = self.by_number.get(int(match.group(1)), [])
            if same:
                found.append(_Found(match.start(), match.end(), "worker", (same[0], len(same) > 1)))
        for match in self.pattern.finditer(text):
            candidates = self.by_name.get(match.group(0), [])
            if not candidates:
                continue
            # A whole name before a first name; any other worker it could be makes it unsure.
            best = min(candidates, key=lambda candidate: candidate[1])[0]
            unsure = len({worker.id for worker, _ in candidates}) > 1
            found.append(_Found(match.start(), match.end(), "worker", (best, unsure)))
        return found

    def close(self, heard: str) -> RosterWorker | None:
        """The one worker whose name (or first name) is close to what was heard, if any."""
        if heard not in self.close_ones:
            best: tuple[float, RosterWorker | None] = (0.0, None)
            for worker, names in self.names:
                score = max(
                    (SequenceMatcher(None, heard, name).ratio() for name in names), default=0
                )
                if score > best[0]:
                    best = (score, worker)
            self.close_ones[heard] = best[1] if best[0] >= self.words.similar_at else None
        return self.close_ones[heard]


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
    return [
        item
        for item in found
        if len(str(item.value).split()) <= MOST_UNKNOWN_WORDS and len(str(item.value)) <= NAME_MAX
    ]


def _run(text: str, run: list[tuple[int, int]]) -> _Found:
    return _Found(run[0][0], run[-1][1], "unknown", text[run[0][0] : run[-1][1]])


def _finds(part: str, roster: _Roster, words: MarkWords) -> list[_Found]:
    """What one comma part of a sentence says, in order: marks, names, numbers, "the rest",
    days, and words that may be names. Fillers count only to keep a word they hold from being
    taken for another ("हाजिरी", attendance, is no "हाजिर", present)."""
    plain = _name(part)
    known = [*_spans(words.rest, plain, "rest"), *_spans(words.present, plain, PRESENT)]
    for status in STATUSES:
        known += _spans(words.statuses[status], plain, status)
    for back, pattern in words.days.items():
        known += _spans(pattern, plain, "day", back)
    known += roster.find(plain)
    known += _spans(words.filler, plain, "filler")
    taken = _longest(known)
    every = _longest([*taken, *_unknown(plain, taken, words)])
    return [item for item in every if item.kind != "filler"]


@dataclass
class _Reading:
    roster: _Roster
    draft: MarksDraft = field(default_factory=MarksDraft)
    by_worker: dict[str, DraftMark] = field(default_factory=dict)
    unknown: int = 0

    def give(self, item: _Found, status: str | None) -> DraftMark | str | None:
        """``item`` (a name, a number, "the rest") gets ``status``: the mark made, or REST, or
        None for an unknown word past the most read."""
        if item.kind == "rest":
            self.draft.rest = status
            return REST
        if item.kind == "unknown":
            self.unknown += 1
            if self.unknown > self.roster.words.most_unknown:
                return None
        mark = _mark(item, self.roster)
        mark.status = status
        if status is None:
            mark.uncertain.append("status")
        if mark.worker_id is None:
            self.draft.marks.append(mark)
        else:
            self.by_worker[mark.worker_id] = mark
        return mark

    def sentence(self, parts: list[list[_Found]]) -> None:
        """One sentence's comma parts. Starting with a mark, each part with marks of its own
        reads them, and a part without goes on from the last mark said ("full day: Meena,
        Lakshmi, Ravi absent"); otherwise the sentence is read whole, each mark to the names
        before it ("Ravi, Meena full day")."""
        every = [item for part in parts for item in part]
        if not every:
            return
        if every[0].status is None:
            self.assign(every)
            return
        current: str | None = None
        for part in parts:
            if any(item.status for item in part):
                current = self.assign(part)
            else:
                for item in part:
                    self.give(item, current)

    def assign(self, items: list[_Found]) -> str | None:
        """Each mark word to its names: those before it, or those after it when ``items`` start
        with one. A general word ("came") followed straight by a mark gives way to it. Returns
        the last mark said."""
        status_first = bool(items) and items[0].status is not None
        current: str | None = None
        waiting: list[_Found] = []
        given: list[DraftMark | str | None] = []  # what the last mark word went to
        previous: _Found | None = None
        for item in items:
            status = item.status
            if status is None:
                if status_first:
                    given = [self.give(item, current)]
                else:
                    waiting.append(item)
            elif previous is not None and previous.kind == PRESENT and not waiting:
                current = status  # "came, half day": the mark said, for whom "came" went to
                for mark in given:
                    self._change(mark, status)
            else:
                current = status
                if not status_first:
                    given = [self.give(each, current) for each in waiting]
                    waiting = []
            previous = item
        for each in waiting:  # names after the last mark word, with none of their own
            self.give(each, None)
        return current

    def _change(self, mark: DraftMark | str | None, status: str) -> None:
        if mark == REST:  # "the rest came, half day"
            self.draft.rest = status
            return
        if not isinstance(mark, DraftMark):
            return
        mark.status = status
        if "status" not in mark.uncertain:
            mark.uncertain.append("status")


def _mark(item: _Found, roster: _Roster) -> DraftMark:
    if item.kind == "worker":
        worker, unsure = item.value
        return DraftMark(worker.id, worker.name, uncertain=["worker"] if unsure else [])
    close = roster.close(str(item.value))
    if close is not None:
        return DraftMark(close.id, close.name, uncertain=["worker"])
    return DraftMark(None, str(item.value), uncertain=["worker"])


def read_marks(text: str, workers: Sequence[RosterWorker], words: MarkWords) -> MarksDraft:
    """The marks said in ``text`` for these workers (the project's, for the day). Without a
    single mark word nothing is read: a name alone ("Ravi paid 500") isn't attendance."""
    reading = _Reading(_Roster(workers, words))
    sentences = [
        [_finds(part, reading.roster, words) for part in sentence.split(",")]
        for sentence in SENTENCE_END.split(text)
    ]
    finds = [item for parts in sentences for part in parts for item in part]
    days = [item.value for item in finds if item.kind == "day"]
    reading.draft.days_back = days[0] if days else None
    if not any(item.status for item in finds):
        return reading.draft
    for parts in sentences:
        reading.sentence([[item for item in part if item.kind != "day"] for part in parts])
    reading.draft.marks = [*reading.by_worker.values(), *reading.draft.marks]
    return reading.draft


def speaks_of_marks(text: str, words: MarkWords) -> bool:
    """Whether ``text`` says a mark at all: asked without the page ("Ravi paid 500" is the
    expenses skill's), the reader takes only words that do."""
    plain = _name(text)
    patterns = [words.present, *(words.statuses[status] for status in STATUSES)]
    return any(pattern.search(plain) for pattern in patterns)


def roster_of(rows: Iterable[Mapping[str, Any]]) -> list[RosterWorker]:
    """The workers as the ``labor_day`` tool lists them."""
    return [RosterWorker(str(row["id"]), str(row["name"]), str(row["code"])) for row in rows]


def day_said(draft: MarksDraft, today: date, given: date | None) -> date:
    """The day the marks are for: the one said, else the page's, else today."""
    if draft.days_back is not None:
        return today - timedelta(days=draft.days_back)
    return given or today
