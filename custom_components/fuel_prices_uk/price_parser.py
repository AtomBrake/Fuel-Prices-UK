"""Utility helpers for normalising retailer price payloads."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PRICE_KEYS_PRIMARY: tuple[str, ...] = (
    "price",
    "value",
    "amount",
    "amount_ppl",
    "amountPpl",
    "amountPencePerLitre",
    "amount_pence_per_litre",
    "cash_price",
    "cashPrice",
    "pence_per_litre",
    "ppl",
)


# Bounds (GBP per litre) for a price to be believable. UK pump prices have sat
# well inside this range; anything outside it is a feed error, not a bargain.
MIN_PLAUSIBLE_PRICE = 0.50
MAX_PLAUSIBLE_PRICE = 5.00


def is_plausible_price(price: float) -> bool:
    """Return True if a GBP-per-litre price is within the believable range."""
    return MIN_PLAUSIBLE_PRICE <= price <= MAX_PLAUSIBLE_PRICE


def _iter_candidates(entry: Any) -> Iterable[Any]:
    if isinstance(entry, dict):
        for key in PRICE_KEYS_PRIMARY:
            if key in entry:
                yield entry[key]
        yield from entry.values()
    elif isinstance(entry, (list, tuple, set)):
        for item in entry:
            yield from _iter_candidates(item)
    else:
        yield entry


def coerce_price(value: Any) -> float | None:
    """Return a GBP float regardless of whether the feed uses pence, strings, or nested dicts."""

    for candidate in _iter_candidates(value):
        if candidate in (None, ""):
            continue
        try:
            price = float(candidate)
        except (TypeError, ValueError):
            continue
        if price >= 50:
            divisor = 1000 if price >= 1000 else 100
            price = price / divisor
        return round(price, 3)
    return None
