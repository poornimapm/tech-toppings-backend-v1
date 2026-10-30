"""What someone said or typed ("2 kg tomatoes 80 and milk 30 yesterday") as draft entries."""

from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.modules.expenses.parsing import numbers, when
from app.modules.expenses.parsing.lexicon import Lexicon, known, lookup
from app.modules.expenses.parsing.numbers import Item, Number, Word
from app.platform import normalise, western_digits

EXPENSE, INCOME = "expense", "income"
SEPARATOR = ","
MERCHANT_WORDS = 4
ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
QUOTES = (
    '"\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}'
    "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
)
# Spacing that makes "₹250", "250rs", "rs.250", "250/-" and "1,250" plain tokens.
SPACING = (
    (re.compile(r"(?<=\d),(?=\d)"), ""),
    (re.compile(r"/-"), " "),
    (re.compile(r"₹"), " ₹ "),
    (re.compile(r"\b(rs|inr)\.?(?=\d)", re.IGNORECASE), r"\1 "),
    (re.compile(r"(?<=\d)(?=(?:rs|rupees?)\b)", re.IGNORECASE), " "),
    (re.compile(r"[,;]"), f" {SEPARATOR} "),
    # Brackets, straight and curly quotes, and full stops that aren't decimal points.
    (re.compile(rf"[!?()\[\]{QUOTES}:]|\.(?!\d)"), " "),
)


@dataclass
class Draft:
    """One entry as understood, with how sure the parser is of each field (0 to 1)."""

    kind: str = EXPENSE
    title: str | None = None
    amount: Decimal | None = None
    category_id: str | None = None
    date: date | None = None
    merchant: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None  # a code of the module's unit table
    confidence: dict[str, float] = field(default_factory=dict)
    said: str = ""  # the words it was read from, without the day: what else they say (ious.py)


def tokenise(text: str, lexicon: Lexicon) -> list[Word]:
    raw = ORDINAL.sub("", western_digits(text))
    for pattern, replacement in SPACING:
        raw = pattern.sub(replacement, raw)
    for phrase in lexicon.phrases:  # "day before yesterday" becomes one token
        raw = re.sub(re.escape(phrase), phrase.replace(" ", "_"), raw, flags=re.IGNORECASE)
    return [Word(token, normalise(token).replace("_", " ")) for token in raw.split()]


def has_money_words(text: str, lexicon: Lexicon) -> bool:
    """Does the text talk about money (a currency or a word for spending or receiving)?"""
    signals = lexicon.currency | lexicon.spend | lexicon.income
    return any(known(word.text, signals) for word in tokenise(text, lexicon))


def _is(item: Item | None, words: Set[str]) -> bool:
    return isinstance(item, Word) and known(item.text, words)


def _split(items: list[Item], lexicon: Lexicon) -> list[list[Item]]:
    parts: list[list[Item]] = [[]]
    for item in items:
        if _is(item, lexicon.conjunctions) or _is(item, {SEPARATOR}):
            parts.append([])
        else:
            parts[-1].append(item)
    return [part for part in parts if part]


def _amount(items: list[Item], lexicon: Lexicon) -> tuple[int | None, float]:
    """Which item is the amount, and how sure: a number beside a currency word, else the only
    number that isn't a quantity, else the largest of them."""
    figures = [
        index
        for index, item in enumerate(items)
        if isinstance(item, Number)
        and not _is(items[index + 1] if index + 1 < len(items) else None, lexicon.units.keys())
    ]
    marked = [
        index
        for index in figures
        if _is(items[index - 1] if index else None, lexicon.currency)
        or _is(items[index + 1] if index + 1 < len(items) else None, lexicon.currency)
    ]
    sure = lexicon.confidence
    if marked:
        return marked[0], sure["amount_marked"]
    if len(figures) == 1:
        return figures[0], sure["amount_only"]
    if figures:
        largest = max(figures, key=lambda index: items[index].value)  # type: ignore[union-attr]
        return largest, sure["amount_guessed"]
    return None, 0.0


def _quantity(items: list[Item], lexicon: Lexicon) -> tuple[set[int], Decimal | None, str | None]:
    """How much was bought: the items saying it ("2 kg", "half kilo", "dozen"), the quantity
    and its unit. A unit with no number before it is one of it."""
    for index, item in enumerate(items):
        unit = lookup(item.text, lexicon.units) if isinstance(item, Word) else None
        if unit is None:
            continue
        before = items[index - 1] if index else None
        if isinstance(before, Number):
            return {index - 1, index}, before.value, unit
        return {index}, Decimal(1), unit
    return set(), None, None


def category(
    text: str,
    kind: str,
    lexicon: Lexicon,
    habits: Mapping[str, str],
    usable: Set[str],
    otherwise: tuple[str, float] | None = None,
) -> tuple[str | None, float]:
    """The user's own habit for this title first, then a keyword, then ``otherwise`` (a guess
    from elsewhere, with how sure it is: a receipt's kind of shop), then the kind's "other"."""
    sure = lexicon.confidence
    if (usual := habits.get(text)) in usable:
        return usual, sure["category_history"]
    words = text.split()
    for found, keywords in lexicon.keywords.items():
        phrase = any(" " in keyword and keyword in text for keyword in keywords)
        if found in usable and (phrase or any(known(word, keywords) for word in words)):
            return found, sure["category_keyword"]
    if otherwise and otherwise[0] in usable:
        return otherwise
    fallback = lexicon.defaults.get(kind)
    return (fallback if fallback in usable else None), sure["category_default"]


def _draft(
    items: list[Item],
    kind: str,
    day: date | None,
    lexicon: Lexicon,
    habits: Mapping[str, str],
    usable: Set[str],
) -> Draft:
    sure = lexicon.confidence
    amount_at, amount_sure = _amount(items, lexicon)
    measure_at, quantity, unit = _quantity(items, lexicon)
    place_at = next((i for i, item in enumerate(items) if _is(item, lexicon.places)), None)
    merchant: list[str] = []
    title: list[str] = []
    naming_place = False
    for index, item in enumerate(items):
        plain = isinstance(item, Word) and not _is(item, lexicon.currency | lexicon.fillers)
        if index == place_at:
            naming_place = True  # the plain words right after "at" are where it was paid
        elif naming_place and plain and len(merchant) < MERCHANT_WORDS:
            merchant.append(item.raw)
        else:
            naming_place = False
            if (
                index != amount_at
                and index not in measure_at
                and (plain or isinstance(item, Number))
            ):
                title.append(item.raw)
    name = " ".join(title).strip(" -") or None
    filed, category_sure = category(
        normalise(" ".join([*title, *merchant])), kind, lexicon, habits, usable
    )
    amount = items[amount_at].value if amount_at is not None else None  # type: ignore[union-attr]
    return Draft(
        kind=kind,
        title=name[:1].upper() + name[1:] if name else None,
        amount=amount,
        category_id=filed,
        date=day,
        merchant=" ".join(merchant) or None,
        quantity=quantity,
        unit=unit,
        confidence={
            "amount": amount_sure,
            "title": sure["title"] if name else 0.0,
            "category_id": category_sure if filed else 0.0,
            "date": sure["date_said"] if day else sure["date_assumed"],
        },
        said=" ".join(item.text if isinstance(item, Word) else item.raw for item in items),
    )


def parse(
    text: str,
    today: date,
    lexicon: Lexicon,
    habits: Mapping[str, str],
    categories: Mapping[str, Set[str]],
) -> list[Draft]:
    """Drafts for a sentence: one per thing bought when each has its own amount, else one."""
    items = numbers.read(tokenise(text, lexicon), lexicon)
    kind = INCOME if any(_is(item, lexicon.income) for item in items) else EXPENSE
    day, items = when.extract(items, today, lexicon)
    parts = _split(items, lexicon)
    if not all(_amount(part, lexicon)[0] is not None for part in parts) or not parts:
        parts = [[item for item in items if not _is(item, {SEPARATOR})]]
    usable = categories.get(kind, frozenset())
    return [_draft(part, kind, day, lexicon, habits, usable) for part in parts if part]
