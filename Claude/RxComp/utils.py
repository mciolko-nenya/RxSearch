"""
Drug-name/slug normalization helpers shared by the drug-slug-based sites
(GoodRx, SingleCare, Cost Plus Drugs).

Flagged in the plan as needing live iteration: real drug/strength/form
combinations have edge cases (brand vs. generic names, "mcg" vs "mg",
liquid concentrations like "5mg/5mL", combination-drug ordering) that
can't be fully anticipated without testing against the live sites. Treat
these as a reasonable starting point, refined via the _save_debug_page
dumps when a lookup comes back empty.
"""

from __future__ import annotations

import re


def slugify_drug_name(name: str) -> str:
    """
    'Lisinopril' -> 'lisinopril'
    'Lisinopril/HCTZ' -> 'lisinoprilhctz' (combo drugs: no separator)
    'Vitamin B-12' -> 'vitamin-b-12'
    """
    s = name.lower().strip()
    s = re.sub(r"[/\s]+", "", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s


def _normalize_dose_token(dosage: str) -> str:
    """'2.5mg' -> '2_5mg', '20 mg' -> '20mg'."""
    d = dosage.lower().strip().replace(" ", "")
    d = d.replace(".", "_")
    return d


def costplusdrugs_slug(drug_name: str, dosage: str, formulation: str) -> str:
    """
    'Lisinopril', '2.5mg', 'tablet' -> 'lisinopril-2_5mg-tablet'

    Cost Plus Drugs URLs follow costplusdrugs.com/medications/{slug}/.
    """
    name = slugify_drug_name(drug_name)
    dose = _normalize_dose_token(dosage)
    form = formulation.lower().strip()
    return f"{name}-{dose}-{form}"


def goodrx_slug(drug_name: str) -> str:
    """GoodRx URLs are goodrx.com/{drug-slug} — dosage/form are page-level
    dropdowns rather than part of the URL."""
    return slugify_drug_name(drug_name)


def singlecare_slug(drug_name: str) -> str:
    """SingleCare URLs are singlecare.com/prescription/{drug-slug}."""
    return slugify_drug_name(drug_name)


_LEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)")


def token_matches(requested: str, rendered: str) -> bool:
    """Confirmed live to be a real bug: goodrx_scraper.py's/
    singlecare_scraper.py's `_loose_matches()` and
    costplusdrugs_scraper.py's `_find_and_click_variant_button()` each
    used to do their own normalize-and-check with a bidirectional plain
    substring test (`a in b or b in a`) to decide whether a requested
    dosage/quantity/form matched something scraped or a button's label.
    That's unanchored — "20mg" is a literal substring of "120mg", and
    "90" is a literal substring of "190" — so it could both (a) click
    the wrong strength/quantity button with no verification at all
    (costplusdrugs_scraper.py), and (b) decide a page already showed the
    requested dosage/quantity when it actually showed a different,
    merely-substring-colliding one, skipping the correction step and its
    caveat entirely (goodrx_scraper.py/singlecare_scraper.py).

    Fixed here, shared, so the fix can't drift between the three
    call sites that need it: normalizes both sides (strip anything but
    letters/digits, lowercase) same as before, but when either side
    starts with a number, the two leading numbers must be *equal*, not
    merely one containing the other — "20mg" no longer matches "120mg",
    while "90" still matches "90count" (the number is what has to match
    exactly; a trailing unit word like "mg"/"count"/"tablets" differing
    in wording, or one side carrying it and the other not, is still
    tolerated, matching this project's existing loose-match philosophy
    for anything that isn't the number itself). Falls back to plain
    normalized-string equality when neither side leads with a number
    (e.g. matching a form value like "tablet")."""
    norm = lambda s: re.sub(r"[^a-z0-9.]", "", (s or "").lower())
    req_norm, rend_norm = norm(requested), norm(rendered)
    req_num = _LEADING_NUMBER.match(req_norm)
    rend_num = _LEADING_NUMBER.match(rend_norm)
    if req_num and rend_num:
        return float(req_num.group(1)) == float(rend_num.group(1))
    # Neither side leads with a number (e.g. matching a form value like
    # "tablet") — no confirmed bug in this branch, so it keeps the
    # original bidirectional-substring behavior every caller already
    # relied on for non-numeric matches (e.g. "tablet" matching inside a
    # longer descriptive button label like "Oral Tablet"), rather than
    # tightening to exact equality and risking a regression nothing
    # showed was actually broken.
    req_plain, rend_plain = req_norm.replace(".", ""), rend_norm.replace(".", "")
    return bool(req_plain) and (req_plain in rend_plain or rend_plain in req_plain)


def extract_price_after_label(
    text: str, label: str, window: int = 100, raw_trim: int = 60
) -> tuple[float | None, str]:
    """Search rendered text for a label, read whatever $ amount follows
    it within `window` characters, and return that plus a short raw
    snippet for debugging.

    Reviewed for quality: goodrx_scraper.py's `_extract_standard_price()`
    and amazon_scraper.py's `_extract_price_after()` were independently-
    written copies of this exact recipe (goodrx's own docstring already
    said as much) — same regex-search-then-extract_price_from_text()
    shape, differing only in which label they search for and their
    window/raw-snippet sizes (goodrx: 100/60, amazon: 60/40). Shared here
    with both call sites passing their own existing sizes, so neither's
    behavior changed."""
    match = re.search(re.escape(label), text, re.IGNORECASE)
    if not match:
        return None, ""
    window_text = text[match.end() : match.end() + window]
    price = extract_price_from_text(window_text)
    if price is None:
        return None, ""
    return price, (match.group(0) + " " + window_text[:raw_trim]).strip()


def extract_price_from_text(text: str) -> float | None:
    """Best-effort fallback: pull the first $X.XX-looking price out of raw
    page text when structured selectors come up empty."""
    match = re.search(r"\$\s?(\d{1,4}(?:\.\d{2})?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


if __name__ == "__main__":
    print(slugify_drug_name("Lisinopril"))
    print(slugify_drug_name("Lisinopril/HCTZ"))
    print(costplusdrugs_slug("Lisinopril", "2.5mg", "tablet"))
    print(costplusdrugs_slug("Lisinopril", "20mg", "tablet"))
    print(goodrx_slug("Metformin"))
    print(singlecare_slug("Metformin"))
    print(extract_price_from_text("Cash price as low as $8.42 at Walgreens"))
