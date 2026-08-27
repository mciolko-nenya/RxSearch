"""
Shared data model for RxComp scrapers.

Every scraper's get_prices() returns a list[PriceResult] and never raises —
a failed lookup is represented as a PriceResult with price=None and `error`
set, rather than an exception. This keeps main.py's output logic to a single
code path regardless of which sites succeeded or failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PriceResult:
    drug_name: str
    formulation: str
    dosage: str
    source: str  # "GoodRx", "SingleCare", "Amazon Pharmacy", "Cost Plus Drugs"

    price: float | None = None
    price_label: str = ""  # e.g. "cash price", "coupon price", "Prime price"
    pharmacy: str = ""  # e.g. "Walgreens" — GoodRx/SingleCare list multiple pharmacies
    quantity: str = ""  # e.g. "30 tablets"
    url: str = ""
    raw_text: str = ""

    # True only for a price that needs actual insurance coverage to get
    # (currently just Amazon Pharmacy's "Average insurance price" row —
    # an estimate, not even a real quote, per amazon_scraper.py). A
    # structured flag rather than sniffing `price_label` for the word
    # "insurance": both Cost Plus Drugs' and Amazon's own *cash* price
    # labels already contain that word too ("cash price (no insurance)"),
    # which would make a naive substring check filter out exactly the
    # no-strings-attached prices this is meant to keep.
    requires_insurance: bool = False

    error: str = ""  # non-empty => this row represents a failed lookup, not a price
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def ok(self) -> bool:
        return not self.error and self.price is not None

    def __str__(self) -> str:
        if self.error:
            return f"{self.source}: no result — {self.error}"
        parts = [f"{self.source}: ${self.price:.2f}"]
        if self.pharmacy:
            parts.append(f"at {self.pharmacy}")
        if self.price_label:
            parts.append(f"({self.price_label})")
        if self.quantity:
            parts.append(f"— {self.quantity}")
        return " ".join(parts)


if __name__ == "__main__":
    sample = [
        PriceResult(
            drug_name="Lisinopril",
            formulation="tablet",
            dosage="20mg",
            source="SingleCare",
            price=8.42,
            price_label="coupon price",
            pharmacy="Walgreens",
            quantity="30 tablets",
            url="https://www.singlecare.com/prescription/lisinopril",
        ),
        PriceResult(
            drug_name="Lisinopril",
            formulation="tablet",
            dosage="20mg",
            source="GoodRx",
            error="no price table found (page structure may have changed)",
        ),
    ]
    for r in sample:
        print(r)
