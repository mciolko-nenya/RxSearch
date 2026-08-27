"""
Cost Plus Drugs price lookup.

Switched from Selenium screen-scraping to Cost Plus Drugs' own free,
public JSON API. Researched and confirmed live before making this
switch: of the four sources this project prices, Cost Plus Drugs is the
*only* one with a genuinely self-serve API — no signup, no API key,
documented at https://github.com/CostPlusDrugs/apidocs /
https://costplusdrugs.github.io/apidocs/. GoodRx and SingleCare each
have a real API too, but both are gated behind a business-partnership
application (a sales/review process, reportedly requiring GoodRx's
prior written consent for non-consumer-facing/non-commercial use) with
no self-serve path for a personal project — those two, and Amazon
Pharmacy (which has no pricing API at all — prescription drugs are
explicitly excluded from Amazon's own Product Advertising/Creators API
by its own policy), are still screen-scraped elsewhere in this project.

Verified live against the endpoint directly as part of that research:
querying `medication_name=lisinopril`, taking its 20mg NDC, then
`ndc=<that NDC>&quantity_units=30` returned `requested_quote: "$5.55"` —
the exact dollar figure the old Selenium scraper's own confirmed-live
comment recorded reading off the real page's "A 30 count supply of 20mg
Lisinopril will cost: ... $5.55" sentence. That confirms `requested_quote`
is the same pre-shipping "Your Drug Price With Us" figure the page
itself shows, not a different number. It's still not the full checkout
total, though: Cost Plus Drugs' site separately discloses a flat
"Standard Shipping" fee at checkout (confirmed $5.25 as of that same
live check, in the version of this file that scraped the page directly)
which the API doesn't return at all. The old scraper folded that fee
into `price` because it could re-read it live off the page every time;
this version can't confirm it's still $5.25 without going back to
scraping the very thing it's replacing, so it's called out as an
explicit, honest caveat in `price_label` instead of silently baking in a
number that could go stale.

Still uses the static Team Cuban Card formulary spreadsheet
(costplusdrugs_formulary.py) as an optional fast pre-filter when
COSTPLUSDRUGS_LOOKUP_MODE=file — unchanged by this switch; it answers a
different question (is this drug on our own formulary list at all) than
the live API call does, and the API call is cheap enough now that the
pre-filter is more a "confirm it's a formulary drug" step than a
meaningful performance optimization, but removing it wasn't asked for.
"""

from __future__ import annotations

import requests

from config import Config
from costplusdrugs_formulary import find_entry, load_formulary
from models import PriceResult
from utils import extract_price_from_text, token_matches

SOURCE_NAME = "Cost Plus Drugs"

API_BASE_URL = "https://us-central1-costplusdrugs-publicapi.cloudfunctions.net/main"

# Cost Plus Drugs' own page used to state its default pack size in plain
# sentence form ("A 30 count supply of ... will cost:"), varying per
# drug — that's what the old scraper reported when no --quantity was
# given. The API has no equivalent "what's this drug's own default pack
# size" field; it only quotes whatever quantity_units you actually ask
# for. Rather than guess at a per-drug default this project has no way
# to ask for, this falls back to a fixed, clearly-labeled assumption
# instead of silently presenting it as Cost Plus Drugs' own default.
DEFAULT_QUANTITY_UNITS = "30"


def _error_result(drug_name: str, formulation: str, dosage: str, error: str, url: str = "") -> list[PriceResult]:
    return [
        PriceResult(
            drug_name=drug_name, formulation=formulation, dosage=dosage,
            source=SOURCE_NAME, url=url, error=error,
        )
    ]


def _api_get(params: dict) -> list[dict]:
    """Raises on network/HTTP failure — every caller catches broadly,
    matching every other scraper's "never raise past get_prices()"
    contract."""
    # Config.REQUEST_TIMEOUT_SECONDS was kept validated (unused) after
    # SingleCare/Cost Plus Drugs' original plain-fetch paths were both
    # replaced by Selenium, for exactly this situation: "in case a plain-
    # fetch path is reintroduced later." It has been now.
    response = requests.get(API_BASE_URL, params=params, timeout=Config.REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json().get("results", [])


def get_prices(
    drug_name: str,
    formulation: str,
    dosage: str,
    # Accepted for signature parity with the other three sites'
    # get_prices() — never used here, same as before this switch: Cost
    # Plus Drugs is a single flat-price mail-order pharmacy with no
    # ZIP-based price variation to look up.
    zip_code: str | None = None,
    quantity: str | None = None,
) -> list[PriceResult]:
    """Never raises — any unexpected failure is returned as an error PriceResult
    so main.py can treat every site the same way."""
    try:
        if Config.COSTPLUSDRUGS_LOOKUP_MODE == "file":
            try:
                entries = load_formulary(Config.COSTPLUSDRUGS_FORMULARY_PATH)
            except FileNotFoundError:
                return _error_result(
                    drug_name, formulation, dosage,
                    "COSTPLUSDRUGS_LOOKUP_MODE=file but formulary file not found: "
                    f"{Config.COSTPLUSDRUGS_FORMULARY_PATH}",
                )
            if find_entry(entries, drug_name, dosage, formulation) is None:
                return _error_result(
                    drug_name, formulation, dosage,
                    "not found in Team Cuban Card formulary list — "
                    "not carried by Cost Plus Drugs (skipped live lookup)",
                )
            # Found in the formulary — it has no price column, so the live
            # API is still needed for the actual $ number. Fall through.

        try:
            rows = _api_get({"medication_name": drug_name})
        except Exception as e:
            return _error_result(drug_name, formulation, dosage, f"Cost Plus Drugs API request failed: {e}")

        if not rows:
            return _error_result(
                drug_name, formulation, dosage,
                f"no Cost Plus Drugs catalog entry for '{drug_name}'",
            )

        # Form first, then strength — same priority order the old
        # Selenium flow used (the page's own top-to-bottom "Select Form"
        # above "Select Strength"), applied here as filters instead of
        # button clicks. Hard-excludes on mismatch rather than falling
        # back to "everything", matching every other site's drug-
        # identity/formulation/dosage filters in this project: showing a
        # wrong strength or form is worse than showing nothing.
        form_matching = [r for r in rows if token_matches(formulation, r.get("form", ""))] if formulation else rows
        if formulation and not form_matching:
            available = ", ".join(sorted({r.get("form", "") for r in rows if r.get("form")})) or "none listed"
            return _error_result(
                drug_name, formulation, dosage,
                f"no result for formulation '{formulation}' — available: {available}",
            )

        dosage_matching = (
            [r for r in form_matching if token_matches(dosage, r.get("strength", ""))]
            if dosage else form_matching
        )
        if dosage and not dosage_matching:
            available = (
                ", ".join(sorted({r.get("strength", "") for r in form_matching if r.get("strength")}))
                or "none listed"
            )
            return _error_result(
                drug_name, formulation, dosage,
                f"no result for dosage '{dosage}' — available: {available}",
            )

        row = dosage_matching[0]
        ndc = row.get("ndc", "")
        url = row.get("url", "")

        quantity_units = quantity or DEFAULT_QUANTITY_UNITS
        # "count" suffix matches this project's established quantity-string
        # convention (main.py's QUANTITY_COUNT_PATTERN parses it back out
        # to decide whether normalize_quantities() needs to rescale a
        # result at all) — a bare number would still behave correctly
        # there (it just fails to parse, which normalize_quantities()
        # already treats as "leave it alone"), but this makes the actual
        # requested quantity legible in the CLI/GUI output the same way
        # every other site's quantity column already is.
        quantity_label = (
            f"{quantity_units} count" if quantity
            else f"{quantity_units} count (assumed default quantity, not requested)"
        )

        try:
            quote_rows = _api_get({"ndc": ndc, "quantity_units": quantity_units})
        except Exception as e:
            return _error_result(drug_name, formulation, dosage, f"Cost Plus Drugs API request failed: {e}", url)

        if not quote_rows:
            return _error_result(
                drug_name, formulation, dosage,
                f"Cost Plus Drugs' API returned no quote for NDC {ndc} at quantity {quantity_units}",
                url,
            )

        price = extract_price_from_text(quote_rows[0].get("requested_quote", ""))
        if price is None:
            return _error_result(drug_name, formulation, dosage, "no price returned by Cost Plus Drugs' API", url)

        label = (
            "cash price (no insurance) — excludes Cost Plus Drugs' flat "
            "standard shipping fee, charged separately at checkout and not "
            "returned by this API"
        )

        return [
            PriceResult(
                drug_name=drug_name,
                formulation=formulation,
                dosage=dosage,
                source=SOURCE_NAME,
                price=price,
                price_label=label,
                quantity=quantity_label,
                url=url,
                raw_text=str(quote_rows[0]),
            )
        ]
    except Exception as e:
        return _error_result(drug_name, formulation, dosage, f"unexpected error: {e}")


if __name__ == "__main__":
    results = get_prices("Lisinopril", "tablet", "20mg")
    for r in results:
        print(r)
