"""
Amazon Pharmacy scraper.

Confirmed during planning research: Amazon Pharmacy pricing (cash price,
Prime member price) is only rendered when signed in — there's no public,
no-login price lookup. So this scraper reuses a persistent Chrome profile
the user logs into manually once via `python main.py --setup-amazon`,
rather than storing any Amazon credentials.

Confirmed live from a saved copy of a real search (provided directly, not
scraped, since Amazon can't be tested live here without signing in — see
README.md's "Amazon Pharmacy: wrong search URL entirely" section): the
original SEARCH_URL (`https://www.amazon.com/primerx/search/?searchTerm=`)
never returned any results because it isn't Amazon Pharmacy's own drug
search at all — it's Amazon's "Prime Rx" discount card page (a marketing/
FAQ page for a *different* product: getting a discount at OTHER
pharmacies like CVS/Walgreens). The real flow is a plain Amazon.com
product search (`https://www.amazon.com/s?k=...`) — Amazon Pharmacy's
own prescription items show up as ordinary search results, distinguished
from unrelated products (books, supplements, other health services) by a
"Prescription Required" badge and a two-tier price block ("Average
insurance price" and "Without insurance").

Known, accepted risk: Amazon may detect headless automation and re-challenge
the session more aggressively than the other three sites even with valid
cookies. When that happens this module degrades gracefully (an error
PriceResult telling the user to re-run --setup-amazon) rather than crashing.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import Config
from driver_utils import SeleniumScraperBase, find_first_present, wait_for_challenge_confirmation
from models import PriceResult
from utils import extract_price_after_label, token_matches

SOURCE_NAME = "Amazon Pharmacy"
SIGNIN_URL = "https://pharmacy.amazon.com"
SEARCH_URL = "https://www.amazon.com/s?k={term}"

# Confirmed live real markup (see this module's docstring for how): every
# Amazon.com search result — pharmacy item or not — carries this attribute.
# It's plain product-search markup, not anything pharmacy-specific, but
# it's exactly what wraps each card we need to look inside.
RESULT_CARD_SELECTOR = 'div[data-component-type="s-search-result"]'

# Confirmed live: only genuine Amazon Pharmacy prescription items have this
# two-tier price block ("Average insurance price" / "Without insurance").
# Everything else in the same search (books, supplements, unrelated health
# services) lacks it entirely — this is what actually distinguishes a real
# Rx pricing card from the rest of a plain product search's results,
# rather than guessing from the title or a badge that might not always
# render the same way.
PRICE_RECIPE_SELECTOR = '[data-cy="price-recipe"]'

# Confirmed live: the first such row inside a card is the drug's form/
# strength/days-supply line (e.g. "Tablet · 20mg · 30 days supply") — a
# second, unrelated row with the same classes ("Prime member price, 30-day
# supply.") also exists further down in the same card, so this must stay
# the *first* match, not just any match.
STRENGTH_ROW_SELECTOR = "div.a-row.a-size-base.a-color-secondary"

SIGNIN_INDICATOR_SELECTORS = ["input[name=email]", "input[id=ap_email]", "input[type=password]"]


def profile_path() -> Path:
    p = Path(Config.AMAZON_CHROME_PROFILE_DIR)
    return p if p.is_absolute() else Path(__file__).parent / p


def is_set_up() -> bool:
    return profile_path().exists()


def setup_amazon_profile():
    """Entry point for `python main.py --setup-amazon`. Launches Chrome
    non-headless with a persistent profile and blocks until the user
    confirms they've finished logging in (including any 2FA/CAPTCHA)."""
    profile_dir = profile_path()
    profile_dir.mkdir(parents=True, exist_ok=True)

    print(f"Opening Chrome with profile at {profile_dir} ...")
    print("Log in to Amazon (and complete any 2FA/CAPTCHA), then come back here.")

    with SeleniumScraperBase(headless=False, user_data_dir=str(profile_dir)) as scraper:
        scraper.driver.get(SIGNIN_URL)
        wait_for_challenge_confirmation("Press Enter once you've finished logging in... ")

    print("Done. This session will be reused for future Amazon Pharmacy lookups.")


def _looks_like_signin_page(driver) -> bool:
    """Reviewed for quality: the selector-loop half of this used to be
    hand-rolled here (iterate SIGNIN_INDICATOR_SELECTORS, find_elements,
    swallow exceptions) — the same "try each selector, return on the
    first that exists" shape goodrx_scraper.py already had factored out
    as driver_utils.find_first_present(). Now shares that instead of
    being a second independent copy of the same resilience pattern."""
    if "/ap/signin" in driver.current_url:
        return True
    return find_first_present(driver, SIGNIN_INDICATOR_SELECTORS) is not None


def _matches_dosage(strength_text: str, dosage: str | None) -> bool:
    """Reviewed live: this used to do its own unanchored substring check
    (`target in strength_text...`) — e.g. requested dosage "20mg" is a
    literal substring of a strength_text containing "120mg", so a search
    mixing multiple strengths together (confirmed live: it does, per
    this function's caller) could match and keep the wrong one.

    First fixed by delegating straight to utils.token_matches() on the
    whole strength_text — caught and corrected here, before shipping,
    by a test against the exact "20mg" vs "120mg" collision this
    docstring describes: token_matches("20mg", "Tablet · 120mg · 30
    days supply") still returned True, because token_matches's own
    number-equality check only applies when its *second* argument
    starts with a number, and here the number is the *middle* of three
    "·"-separated segments, not the start — so it fell through to that
    function's substring fallback, which is exactly as vulnerable to
    this collision as the code being fixed. The actual fix extracts
    just the dosage segment first (index 1 of the confirmed-live "Form ·
    Dose · Days supply" shape — see STRENGTH_ROW_SELECTOR's docstring)
    and compares only that via token_matches, the same way
    _matches_formulation() below already correctly extracts its own
    (leading, index 0) segment rather than matching against the whole
    compound string."""
    if not dosage:
        # No specific dosage requested — same "can't tell, don't enforce"
        # tolerance used for soft matches elsewhere in this project.
        return True
    segments = strength_text.split("·")
    dose_segment = segments[1] if len(segments) > 1 else strength_text
    return token_matches(dosage, dose_segment)


def _matches_formulation(strength_text: str, formulation: str | None) -> bool:
    """Reported live: formulation was accepted as a parameter but never
    checked against anything scraped, unlike dosage/drug-name — a search
    mixing listings for multiple forms of the same drug together (the
    strength_text row already confirmed live to read like "Tablet ·
    20mg · 30 days supply" — its own leading token, before the first
    "·", is the form) could keep a wrong-form row with nothing to catch
    it, echoing the requested formulation back as if it had been
    verified. Uses already-scraped data, not a new selector — the same
    field _matches_dosage() above already reads from, just its leading
    token instead of a later one."""
    if not formulation:
        return True
    leading_token = strength_text.split("·", 1)[0]
    return token_matches(formulation, leading_token)


def _matches_drug_name(title: str, drug_name: str) -> bool:
    """Reported live: searching for a single drug (e.g. "atorvastatin")
    returned combination-drug products too (e.g. "Amlodipine -
    Atorvastatin") — a genuinely different drug that merely contains the
    searched name as a substring within a longer one. Nothing here
    previously checked drug identity at all, only dosage, so a plain
    Amazon.com product search returning both kinds of listing side by
    side (confirmed live from a real search: single-ingredient and
    combination listings for the same searched ingredient interleaved in
    the same result set) meant every combination product silently passed
    straight through.

    Confirmed live, real title text for both cases: a single-ingredient
    listing's title is *just* the drug's own name, optionally followed by
    a "(Generic for ...)"/similar suffix — e.g. "Atorvastatin (Generic
    for Lipitor)". A combination listing instead joins multiple active
    ingredients with " - " before any such suffix — e.g. "Amlodipine -
    Atorvastatin" / "Amlodipine - Atorvastatin (Generic for Caduet)".
    Strips off any "(...)" suffix, then requires drug_name to loosely
    match (same normalize-and-compare tolerance as _matches_dosage())
    the *entire* remaining name, not just appear somewhere within it — a
    combination's multi-ingredient name is never equal to a single
    ingredient's name outright, so this rejects those while still
    accepting the real match regardless of its own parenthetical
    suffix."""
    if not drug_name:
        return True
    name_only = title.split("(")[0].strip()
    target = drug_name.lower().replace(" ", "")
    actual = name_only.lower().replace(" ", "")
    return target == actual


def _safe_text(card, selector: str, default: str = "") -> str:
    """Reviewed for quality: search()'s per-card extraction had four
    near-identical `try: card.find_element(...).text/attr ... except
    Exception: default` blocks, differing only in selector/attribute/
    default. Collapsed to this and _safe_attr() below."""
    try:
        return card.find_element(By.CSS_SELECTOR, selector).text.strip()
    except Exception:
        return default


def _safe_attr(card, selector: str, attr: str, default: str = "") -> str:
    try:
        return card.find_element(By.CSS_SELECTOR, selector).get_attribute(attr) or default
    except Exception:
        return default


def _error_result(
    drug_name: str, formulation: str, dosage: str, error: str, url: str = ""
) -> list[PriceResult]:
    """Every early-return failure branch in search()/get_prices() built
    the identical PriceResult(drug_name=..., formulation=..., dosage=...,
    source=SOURCE_NAME, url=..., error=...) by hand — reviewed for
    quality: ~8 near-duplicate copies of the same four request-identity
    fields, differing only in `error`/`url`. Collapsed to one call each,
    so a future field that should always be echoed on error only needs
    adding here once."""
    return [
        PriceResult(
            drug_name=drug_name, formulation=formulation, dosage=dosage,
            source=SOURCE_NAME, url=url, error=error,
        )
    ]


class AmazonPharmacyScraper(SeleniumScraperBase):
    def search(
        self, drug_name: str, formulation: str, dosage: str,
        zip_code: str | None = None, quantity: str | None = None,
    ) -> list[PriceResult]:
        url = SEARCH_URL.format(term=quote_plus(drug_name))
        try:
            self.driver.get(url)
        except Exception as e:
            return _error_result(drug_name, formulation, dosage, f"navigation failed: {e}", url)

        if _looks_like_signin_page(self.driver):
            self._save_debug_page("page_amazon_signin_redirect.html")
            return _error_result(
                drug_name, formulation, dosage,
                "session expired or challenged — re-run: python main.py --setup-amazon", url,
            )

        # Reviewed for quality: this used to save a debug page here
        # unconditionally, on every call — including the success path,
        # where a full driver.page_source round-trip + disk write was
        # paid for a snapshot nothing ever reads. Every failure branch
        # below (no rows, no drug-name match, no formulation match, no
        # dosage match) already saves its own debug page at the point it
        # actually fails, so nothing lost its diagnostic value by
        # removing this — it was only ever redundant with one of those,
        # or wasted on a run that succeeded.
        try:
            WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, RESULT_CARD_SELECTOR))
            )
            cards = self.driver.find_elements(By.CSS_SELECTOR, RESULT_CARD_SELECTOR)
        except Exception:
            cards = []

        rows: list[dict] = []
        for card in cards:
            # A plain Amazon.com search for a drug name returns a mix of
            # actual Amazon Pharmacy Rx items and unrelated products
            # (books, supplements, other health services) — confirmed
            # live, 12 of 18 results in one real search were exactly this
            # kind of noise. Only cards with the two-tier Rx price block
            # are genuine pharmacy pricing; everything else is skipped
            # rather than misread as a price.
            try:
                price_recipe = card.find_element(By.CSS_SELECTOR, PRICE_RECIPE_SELECTOR)
            except Exception:
                continue
            price_text = price_recipe.text
            if "without insurance" not in price_text.lower():
                continue

            # Confirmed live: both "Average insurance price" and "Without
            # insurance" render as their own line followed shortly by a $
            # amount, rather than depending on the price block's exact
            # nested-span markup (data-a-color, a-offscreen, etc.), which
            # could change without the labels themselves changing.
            # extract_price_after_label() is shared with
            # goodrx_scraper.py's identical _extract_standard_price()
            # recipe — this keeps the same 60/40 window/raw-snippet sizes
            # already confirmed live for this page.
            cash_price, cash_raw = extract_price_after_label(price_text, "without insurance", window=60, raw_trim=40)
            insurance_price, insurance_raw = extract_price_after_label(
                price_text, "average insurance price", window=60, raw_trim=40
            )
            if cash_price is None and insurance_price is None:
                continue

            title = _safe_text(card, "h2")
            strength_text = _safe_text(card, STRENGTH_ROW_SELECTOR)
            product_url = _safe_attr(card, "h2 a", "href", url)

            rows.append({
                "title": title, "strength_text": strength_text, "url": product_url,
                "cash_price": cash_price, "cash_raw": cash_raw,
                "insurance_price": insurance_price, "insurance_raw": insurance_raw,
            })

        if not rows:
            self._save_debug_page("page_amazon_no_results.html")
            return _error_result(
                drug_name, formulation, dosage,
                "no Amazon Pharmacy results found (blocked, or page structure changed — "
                "see page_amazon_no_results.html)",
                url,
            )

        # Reported live: results included combination-drug products whose
        # title merely contains the searched name (e.g. "Amlodipine -
        # Atorvastatin" showing up for a search for "atorvastatin") — a
        # genuinely different drug, not a substitute or variant of the one
        # actually requested. Unlike the dosage filter below, this is a
        # hard exclusion with no "fall back to everything" case: showing a
        # different drug entirely is worse than showing nothing, whereas a
        # dosage mismatch is still the *same* drug, just not the requested
        # strength — see _matches_drug_name()'s docstring for the real
        # title text distinguishing the two live.
        name_matching = [r for r in rows if _matches_drug_name(r["title"], drug_name)]
        if not name_matching:
            # Reported live: this used to assert *why* nothing matched
            # ("only combination products ... were found") without
            # actually checking that — it's just what's true of the one
            # case this filter was written for. A plain typo in drug_name
            # (e.g. "amlopidine" vs. the real "Amlodipine") hits this same
            # branch, and _matches_drug_name()'s exact-match check
            # deliberately won't paper over that gap — two genuinely
            # different real drugs can be a letter apart (a fuzzy match
            # would risk the exact wrong-drug mix-up this filter exists to
            # prevent). So rather than guess a reason, this lists whatever
            # titles *were* actually found, the same "available: ..." shape
            # already used for the formulation/dosage mismatches just
            # below — a typo like that one is immediately obvious once the
            # real title is right there next to it.
            available = ", ".join(sorted({r["title"] for r in rows if r["title"]})) or "none parsed"
            self._save_debug_page("page_amazon_no_drug_name_match.html")
            return _error_result(
                drug_name, formulation, dosage,
                f"no Amazon Pharmacy result's name matched '{drug_name}' exactly — "
                f"found: {available} (check for a typo, or a different drug whose name "
                "happens to contain it — see page_amazon_no_drug_name_match.html)",
                url,
            )
        rows = name_matching

        # Reported live: formulation was accepted but never actually
        # verified — see _matches_formulation()'s docstring. Filtered the
        # same way dosage is below: only rows whose form matches are
        # kept, and if a formulation was requested and nothing matches,
        # that's reported as an error listing the forms that *were*
        # found, rather than silently keeping a wrong-form row.
        formulation_matching = [r for r in rows if _matches_formulation(r["strength_text"], formulation)]
        if formulation and not formulation_matching:
            available_forms = ", ".join(
                sorted({r["strength_text"].split("·", 1)[0].strip() for r in rows if r["strength_text"]})
            ) or "none parsed"
            self._save_debug_page("page_amazon_no_formulation_match.html")
            return _error_result(
                drug_name, formulation, dosage,
                f"no result for formulation '{formulation}' — available: {available_forms}", url,
            )
        rows = formulation_matching

        # Confirmed live: a single drug-name search returns one Rx card
        # per strength (e.g. lisinopril's 2.5mg/5mg/10mg/20mg/30mg/40mg all
        # showed up side by side) — mixing them together and taking the
        # cheapest few, the way a couple of the other scrapers used to,
        # would silently compare the wrong strength's price. Filtered
        # strictly instead: only rows whose strength line contains the
        # requested dosage are returned; if a dosage was requested and
        # nothing matches, that's reported as an error rather than
        # silently substituting a different strength's price.
        matching = [r for r in rows if _matches_dosage(r["strength_text"], dosage)]
        if dosage and not matching:
            available = ", ".join(sorted({r["strength_text"] for r in rows if r["strength_text"]})) or "none parsed"
            self._save_debug_page("page_amazon_no_dosage_match.html")
            return _error_result(
                drug_name, formulation, dosage,
                f"no result for dosage '{dosage}' — available: {available}", url,
            )

        # Reviewed for quality: the cash-price and insurance-price rows
        # below used to be two separate, near-identical PriceResult(...)
        # constructions, differing only in price/label/raw/
        # requires_insurance — every other field (drug_name, formulation,
        # dosage, source, pharmacy, quantity, url) was repeated twice per
        # row. Looping over the two (price, label, raw, requires_insurance)
        # tuples keeps that shared boilerplate in one place.
        results = []
        for row in matching:
            quantity_label = row["strength_text"] or quantity or ""
            for price, label, raw, requires_insurance in (
                (row["cash_price"], "cash price (no insurance)", row["cash_raw"], False),
                (
                    row["insurance_price"],
                    "average price with insurance (estimate — exact price set when you transfer the prescription)",
                    row["insurance_raw"], True,
                ),
            ):
                if price is not None:
                    results.append(
                        PriceResult(
                            drug_name=drug_name, formulation=formulation, dosage=dosage,
                            source=SOURCE_NAME, price=price, price_label=label,
                            pharmacy=SOURCE_NAME, quantity=quantity_label, url=row["url"],
                            raw_text=raw, requires_insurance=requires_insurance,
                        )
                    )
        return results


def get_prices(
    drug_name: str,
    formulation: str,
    dosage: str,
    zip_code: str | None = None,
    quantity: str | None = None,
) -> list[PriceResult]:
    """Never raises — any unexpected failure is returned as an error PriceResult."""
    if not is_set_up():
        return _error_result(drug_name, formulation, dosage, "not set up — run: python main.py --setup-amazon")

    try:
        with AmazonPharmacyScraper(
            headless=Config.HEADLESS_DEFAULT, user_data_dir=str(profile_path())
        ) as scraper:
            return scraper.search(drug_name, formulation, dosage, zip_code, quantity)
    except Exception as e:
        return _error_result(drug_name, formulation, dosage, f"unexpected error: {e}")


if __name__ == "__main__":
    if not is_set_up():
        print("Amazon profile not set up. Run: python main.py --setup-amazon")
    else:
        results = get_prices("Lisinopril", "tablet", "20mg")
        for r in results:
            print(r)
