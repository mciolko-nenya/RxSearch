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

Confirmed live, separately: this API does exact-string matching only —
no fuzzy, substring, or typo tolerance, and it's case-insensitive but
whitespace-sensitive (a stray leading/trailing space is a miss). More
importantly, `medication_name` and `brand_name` are two disjoint fields:
`medication_name=Lipitor` returns nothing, `brand_name=Lipitor` returns
all 4 strengths; `medication_name=atorvastatin` is the reverse. A drug
typed by its brand name would otherwise falsely report "not carried"
even though Cost Plus Drugs stocks it — get_prices() now retries against
brand_name whenever the medication_name lookup comes back empty, before
reporting a real miss.

A third, separate gap: "Atorvastatin Calcium" (salt name included) still
missed both of the above, since the catalog's own medication_name for it
is the bare "Atorvastatin" — confirmed live that catalog naming is
inconsistent about this (252 of 872 unique medication_names *do* include
a salt word, e.g. "Acebutolol HCl"; most don't). Fixed via
_salt_stripped_lookup(): confirmed live that calling this API with *no*
filter params returns its entire catalog (2,373 rows), which is fetched
once, cached, and matched against after normalizing away pure salt
words. Deliberately conservative: never strips release-timing words
(ER/XR/DR/SR/CR — confirmed live those mark real, non-interchangeable
products, e.g. "Metoprolol Tartrate" vs. "Metoprolol Extended Release
(ER)"), and even pure-salt normalization can still collapse two
genuinely distinct products onto the same key (confirmed live:
"Diclofenac Potassium" vs. "Diclofenac Sodium", "Levalbuterol HCl" vs.
"Levalbuterol Tartrate") — so it only auto-accepts when exactly one
catalog name matches, and reports the specific candidates instead of
guessing when there's more than one. Deliberately does NOT add
true edit-distance/typo fuzzy matching on top of this — real drug names
are frequently one or two characters apart from a *different* real drug
(the same reasoning that kept amazon_scraper.py's drug-name matching
exact rather than fuzzy).

Separately, confirmed live in a real browser: `url` points to the right
drug page but the wrong quantity on load — e.g. requesting 90-count
returns a URL that always displays 30-count pricing when opened. Root
cause confirmed on Cost Plus Drugs' own site: its product page has no
URL that encodes a specific quantity at all — clicking its own "90
Count" button updates the on-page price (to the same $ figure this API's
`quantity_units=90` returns) but never changes the URL, query string, or
hash. So `url` here is already the single most specific link that exists
for a given drug+strength+form — there's no more-specific one to switch
to. `get_prices()` now appends a caveat to `price_label` whenever the
requested quantity isn't the page's own default (confirmed live, always
30) so the mismatch is surfaced instead of left silently misleading.

The shipping fee itself is now surfaced too, when known: see
costplusdrugs_shipping.py for how it's kept as a small, separately
and periodically refreshed local file (its own module docstring explains
why this couldn't just be one more field this API returns — the fee
lives behind a real browser-only Cloudflare challenge). `get_prices()`
reads that cache via `load_shipping_fee()` — a plain file read, no
Selenium — and states the last-known fee and how long ago it was
checked directly in `price_label`, rather than leaving the caveat as a
vague "not returned by this API" with no number attached. A cache older
than STALE_SHIPPING_FEE_DAYS gets an explicit staleness warning instead
of being presented as current.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

from config import Config
from costplusdrugs_formulary import find_entry, load_formulary
from costplusdrugs_shipping import load_shipping_fee
from models import PriceResult
from utils import extract_price_from_text, token_matches

SOURCE_NAME = "Cost Plus Drugs"

API_BASE_URL = "https://us-central1-costplusdrugs-publicapi.cloudfunctions.net/main"

# Confirmed live: Cost Plus Drugs' shipping fee has been unchanged ($5.25)
# across every check made during this project's history so far, across
# different drugs and quantities — a flat, rarely-changing fee, not
# something that needs re-checking constantly. This is a "the caller
# should know this might be out of date" threshold, not a hard cutoff:
# crossing it downgrades the price_label caveat's wording, it never
# blocks or invalidates the result.
STALE_SHIPPING_FEE_DAYS = 180

# Deliberately only true salt/counterion words — never release-timing
# modifiers ("ER"/"XR"/"XL"/"SR"/"CR"/"DR"/"extended release"/"delayed
# release"). Confirmed live against the full catalog (see
# _get_full_catalog()) that those mark genuinely different products —
# e.g. "Metoprolol Tartrate" (immediate-release) vs. "Metoprolol
# Extended Release (ER)" are not interchangeable, so stripping them
# could silently collapse two different real drugs into the same
# lookup. Pure salt words are safer but still not risk-free — see
# _salt_stripped_lookup()'s ambiguous-match handling.
_SALT_WORDS = {
    "calcium", "sodium", "potassium", "magnesium", "hcl", "hydrochloride",
    "succinate", "tartrate", "maleate", "besylate", "mesylate", "fumarate",
    "citrate", "sulfate", "sulphate", "phosphate", "acetate", "bitartrate",
    "dihydrate", "monohydrate", "trihydrate", "oxalate", "gluconate",
    "chloride", "bromide", "iodide", "carbonate", "nitrate", "stearate",
}

_FULL_CATALOG_CACHE: list[dict] | None = None


def _normalize_for_salt_match(name: str) -> str:
    """Lowercase, drop punctuation/separators, strip pure salt words —
    but never strip *every* word: some drugs' whole name is a salt
    (e.g. "Potassium Chloride", "Calcium Acetate" are themselves the
    active ingredient, not a salt-of-something-else), so stripping
    everything there would incorrectly treat two unrelated drugs as
    the same lookup key."""
    words = [w for w in re.split(r"[\s/-]+", name.lower()) if w]
    kept = [w for w in words if w not in _SALT_WORDS] or words
    return "".join(re.sub(r"[^a-z0-9]", "", w) for w in kept)


def _get_full_catalog() -> list[dict]:
    """Confirmed live: calling the API with no filter params at all
    returns its *entire* catalog (2,373 rows / 872 unique
    medication_name values as of this check, ~1.2MB) rather than an
    error or nothing — that's what makes client-side salt-normalized
    matching possible below. Cached for the life of the process: this
    catalog doesn't change minute-to-minute, and re-fetching ~1.2MB on
    every fallback lookup would be wasteful."""
    global _FULL_CATALOG_CACHE
    if _FULL_CATALOG_CACHE is None:
        _FULL_CATALOG_CACHE = _api_get({})
    return _FULL_CATALOG_CACHE


def _salt_stripped_lookup(drug_name: str) -> tuple[list[dict], str | None, list[str]]:
    """Last-resort lookup after exact medication_name and brand_name
    both miss (e.g. "Atorvastatin Calcium" when the catalog only has
    "Atorvastatin"). Matches the salt-normalized query against every
    catalog medication_name/brand_name, but only *auto-accepts* when
    exactly one distinct real catalog name maps to that normalized key.

    Confirmed live this matters: normalizing away salt words alone
    still collapses some genuinely different, clinically distinct
    catalog entries onto the same key — e.g. "Diclofenac Potassium"
    (immediate-release) vs. "Diclofenac Sodium" (enteric-coated), or
    "Levalbuterol HCl" vs. "Levalbuterol Tartrate". Silently picking
    one would repeat the exact mistake this project's own
    identity/dosage/formulation filters elsewhere are built to avoid
    (showing the wrong drug is worse than showing nothing) — so this
    returns those as `ambiguous_candidates` instead of guessing, and
    the caller reports them for the user to disambiguate by retyping
    the exact one they meant.

    Returns (rows, resolved_name, ambiguous_candidates): `rows` is
    populated only alongside a non-None `resolved_name` (the single
    safe match); `ambiguous_candidates` lists 2+ real catalog names
    when the key wasn't unique.
    """
    target = _normalize_for_salt_match(drug_name)
    if not target:
        return [], None, []

    catalog = _get_full_catalog()
    matching_names: dict[str, str] = {}  # catalog name -> field it matched on
    for row in catalog:
        for field in ("medication_name", "brand_name"):
            value = row.get(field, "")
            if value and value not in matching_names and _normalize_for_salt_match(value) == target:
                matching_names[value] = field

    candidates = sorted(matching_names)
    if len(candidates) != 1:
        return [], None, candidates

    resolved_name = candidates[0]
    field_used = matching_names[resolved_name]
    rows = [r for r in catalog if r.get(field_used) == resolved_name]
    return rows, resolved_name, []


def _shipping_fee_caveat() -> str:
    """Builds the shipping half of every price_label. Reads
    costplusdrugs_shipping.py's cached fee (a plain file read — never
    touches a browser itself) and states the concrete last-known number
    and how long ago it was checked, rather than the old vague "not
    returned by this API" with no figure attached. Falls back to that
    vague wording, plus a pointer to the updater, only when no cache
    file exists yet."""
    record = load_shipping_fee()
    if record is None:
        return (
            "excludes Cost Plus Drugs' flat standard shipping fee, charged "
            "separately at checkout and not returned by this API (run "
            "costplusdrugs_shipping.py to cache the current fee)"
        )

    try:
        checked_at = datetime.fromisoformat(record.checked_at)
        age_days = (datetime.now(timezone.utc) - checked_at).days
    except ValueError:
        age_days = None

    caveat = (
        f"excludes Cost Plus Drugs' flat standard shipping fee "
        f"(${record.fee:.2f} as of last check"
    )
    if age_days is not None:
        caveat += f", {age_days} day{'s' if age_days != 1 else ''} ago"
    caveat += "; not returned by this API), charged separately at checkout"
    if age_days is not None and age_days > STALE_SHIPPING_FEE_DAYS:
        caveat += (
            f" — ⚠ that check is over {STALE_SHIPPING_FEE_DAYS} days old, "
            "the fee may have changed since; run costplusdrugs_shipping.py "
            "to refresh it"
        )
    return caveat


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
            # Confirmed live: this API does exact-string matching only, no
            # fuzzy/substring/typo tolerance, and medication_name/brand_name
            # are two disjoint fields — "Lipitor" has zero medication_name
            # rows but four brand_name rows (the reverse of "Atorvastatin").
            # A drug typed by its brand name would otherwise report a false
            # "not carried" even though Cost Plus Drugs stocks it. Retry
            # against brand_name before giving up.
            try:
                rows = _api_get({"brand_name": drug_name})
            except Exception as e:
                return _error_result(drug_name, formulation, dosage, f"Cost Plus Drugs API request failed: {e}")

        resolved_via_salt_strip: str | None = None
        if not rows:
            try:
                rows, resolved_via_salt_strip, ambiguous = _salt_stripped_lookup(drug_name)
            except Exception as e:
                return _error_result(drug_name, formulation, dosage, f"Cost Plus Drugs API request failed: {e}")
            if ambiguous:
                return _error_result(
                    drug_name, formulation, dosage,
                    f"'{drug_name}' matches more than one distinct Cost Plus Drugs catalog "
                    f"entry once salt names are normalized away — refusing to guess which one "
                    f"you meant: {', '.join(ambiguous)}. Retype the exact one you want.",
                )

        if not rows:
            return _error_result(
                drug_name, formulation, dosage,
                f"no Cost Plus Drugs catalog entry for '{drug_name}' (checked generic name, "
                "brand name, and a salt-normalized match against the full catalog — the API "
                "itself requires an exact match, no fuzzy/typo tolerance)",
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

        label = f"cash price (no insurance) — {_shipping_fee_caveat()}"
        if quantity_units != DEFAULT_QUANTITY_UNITS:
            # Confirmed live in the browser: Cost Plus Drugs' own product
            # page has no URL that encodes a specific quantity at all —
            # clicking its "90 Count" button updates the on-page price (to
            # the same $ figure this API's quantity_units=90 returns) but
            # never changes the URL, query string, or hash. So `url` here
            # is already the most specific link that exists for this drug,
            # but it will always land on the page's own default view
            # (confirmed always 30, matching DEFAULT_QUANTITY_UNITS above)
            # regardless of what quantity was actually requested/quoted —
            # surfaced here rather than leaving the mismatch silent.
            label += (
                f"; note: the linked page defaults to showing "
                f"{DEFAULT_QUANTITY_UNITS}-count pricing — there is no "
                f"quantity-specific URL on Cost Plus Drugs' site, so "
                f"you'll need to reselect '{quantity_units} Count' there "
                "yourself to see this quote reflected on the page"
            )
        if resolved_via_salt_strip:
            # Never substitute silently — same "wrong drug is worse than no
            # drug" philosophy as the hard-exclude formulation/dosage
            # filters above, just surfaced as a caveat instead of an error
            # since this path only ever auto-accepts an unambiguous match.
            label = (
                f"interpreted '{drug_name}' as Cost Plus Drugs' catalog entry "
                f"'{resolved_via_salt_strip}' (salt name normalized away); " + label
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
