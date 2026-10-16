"""Words the whole module shares."""

from __future__ import annotations

from enum import StrEnum

MODULE = "expenses"


class EntryKind(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class PaymentMode(StrEnum):
    CASH = "cash"
    UPI = "upi"
    CARD = "card"
    BANK = "bank"
    WALLET = "wallet"
    OTHER = "other"


class EntrySource(StrEnum):
    """How an entry, or a change to it, was made."""

    MANUAL = "manual"
    ASSISTANT = "assistant"
    RECURRING = "recurring"  # added by a repeating rule on its day


class RuleMode(StrEnum):
    """What a repeating rule does on each due date."""

    AUTO_CREATE = "auto_create"  # adds the entry by itself
    REMIND = "remind"  # a bill: reminds the user, who marks it paid


class CadenceUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
