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
