"""Money is stored as whole minor units (paise, cents) with an ISO-4217 currency (ADR-0004),
so sums are exact. This is the one place that converts to and from decimal amounts."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ISO-4217 currencies whose minor unit is not 1/100 of the major one.
_EXPONENTS: dict[str, int] = {
    **dict.fromkeys(("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG"), 0),
    **dict.fromkeys(("RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"), 0),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}


def minor_exponent(currency: str) -> int:
    """Decimal places of the currency: 2 for INR, 0 for JPY, 3 for KWD."""
    return _EXPONENTS.get(currency, 2)


def to_minor(amount: Decimal | float | str, currency: str) -> int:
    """``250.5`` INR -> ``25050``. Halves round away from zero, as people expect of money."""
    scaled = Decimal(str(amount)).scaleb(minor_exponent(currency))
    return int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_major(amount_minor: int, currency: str) -> float:
    """``25050`` INR -> ``250.5``, for figures the client formats (tiles, charts)."""
    return float(Decimal(amount_minor).scaleb(-minor_exponent(currency)))
