"""The accuracy gate on the 20 sample receipts (tests/modules/expenses/receipts): the receipt
reader on the text as printed, and on the text a phone read from each photo (committed, so the
gate needs no OCR). The floors sit at what was measured (``poe eval-receipts``), so a change
that reads receipts worse fails here; raise them when it reads them better."""

from __future__ import annotations

import pytest

from app.modules.expenses.parsing.scoring import score_receipts
from scripts.eval_receipts import ocr_text, read_by_rules, samples

CASES, TODAY = samples()

# Measured 2026-10-29 (docs/modules/expenses.md): language -> field -> floor. `drafts` is the
# share of drafts that are right: phantom drafts lower it even when the total still balances.
PAPER = {
    "en": {"total": 1.0, "sum": 1.0, "items": 0.95, "drafts": 0.95, "shop": 1.0, "date": 1.0},
    "ta": {"total": 1.0, "sum": 1.0, "items": 0.94, "drafts": 0.94, "shop": 1.0, "date": 1.0},
    "hi": {"total": 1.0, "sum": 1.0, "items": 1.0, "drafts": 1.0, "shop": 1.0, "date": 1.0},
}
OCR = {
    "en": {"total": 1.0, "sum": 0.91, "items": 0.68, "drafts": 0.81, "shop": 1.0, "date": 1.0},
    "ta": {"total": 1.0, "sum": 1.0, "items": 0.94, "drafts": 0.94, "shop": 1.0, "date": 1.0},
    "hi": {"total": 0.5, "sum": 0.5, "items": 0.4, "drafts": 0.5, "shop": 1.0, "date": 0.0},
}


def test_there_are_twenty_labelled_receipts_in_three_languages() -> None:
    assert len(CASES) == 20
    assert {case["lang"] for case in CASES} == {"en", "ta", "hi"}
    for case in CASES:
        items = sum(amount for _, amount in case["truth"]["items"])
        # What the items don't cover is a charge (a total above them) or a discount (below).
        assert items > 0, case["id"]
        assert case["truth"]["total"] > 0, case["id"]


@pytest.mark.parametrize(("source", "floors"), [("paper", PAPER), ("ocr", OCR)])
def test_the_reader_keeps_its_measured_accuracy(
    source: str, floors: dict[str, dict[str, float]]
) -> None:
    reads = [
        (
            case,
            read_by_rules(
                "\n".join(case["paper"]) if source == "paper" else ocr_text(case),
                case["lang"],
                TODAY,
            ),
        )
        for case in CASES
    ]
    scores, misses = score_receipts(reads)

    below = [
        f"{language} {field}: {scores[language][field]} < {floor:.0%}"
        for language, fields in floors.items()
        for field, floor in fields.items()
        if scores[language][field].rate < floor - 1e-9
    ]
    assert not below, "\n".join(below + misses)
