"""
Cost Plus Drugs scraper.

Drug name -> URL used to be built directly (utils.costplusdrugs_slug():
{name}-{strength}-{form}) rather than through Cost Plus Drugs' own search.
Switched to search-driven resolution (resolve_and_select() below) for the
same reason singlecare_scraper.py's equivalent was: a directly-guessed
slug can be wrong for a drug whose real page uses a different base name
than expected (e.g. a salt-form qualifier) — there's no algorithmic way to
know without asking the site. No matching failure has actually been
confirmed live for Cost Plus Drugs specifically (its slug matched the
plain drug name correctly for atorvastatin, the case that motivated this),
but the same class of risk applies here as anywhere URLs encode a
canonical drug name, so this closes it here too.

Also confirmed live while building this: Cost Plus Drugs' own plain-fetch
path is blocked outright now (matching what's already true for
singlecare_scraper.py) — so this is Selenium-only, not a fallback.

Confirmed live end-to-end: the search result link always lands on a
drug's *default* dosage page (e.g. "/medications/atorvastatin-10mg-
tablet/" regardless of what dosage was searched for) — it only confirms
the correct base drug name, not dosage. Reaching a specific dosage/
quantity uses that page's own selector buttons (confirmed live: plain
<button data-testid="strength-selection-{dose}">/"quantity-selection-
{count}">, not a dropdown — clicking either live-updates the price, and
for strength, the URL, with no separate confirm step).

When Config.COSTPLUSDRUGS_LOOKUP_MODE is "file" (default: "live"), a static
formulary spreadsheet (see costplusdrugs_formulary.py) is checked first as a
fast pre-filter: if the drug/dosage/formulation isn't carried at all, this
skips the live site entirely. It's never a replacement for the live scrape
above — the spreadsheet has no pricing, only what's carried — just a way to
avoid pointless live lookups for drugs Cost Plus Drugs doesn't stock.
"""

from __future__ import annotations

import re
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import Config
from costplusdrugs_formulary import find_entry, load_formulary
from driver_utils import SeleniumScraperBase, robust_click
from models import PriceResult
from utils import extract_price_from_text, token_matches

SOURCE_NAME = "Cost Plus Drugs"

# Confirmed live: the real search flow. A fake-looking "search bar" on the
# homepage is actually a plain button (data-testid confirmed) that opens
# a dialog containing the real input. Typing a drug name renders a
# "Medication Results" section with real <a href="/medications/{slug}/">
# links inside that dialog.
SEARCH_HOME_URL = "https://www.costplusdrugs.com"
SEARCH_TRIGGER_SELECTOR = '[data-testid="medications-search-trigger-button"]'
SEARCH_DIALOG_SELECTOR = "#search-overlay-dialog"
SEARCH_INPUT_SELECTOR = f"{SEARCH_DIALOG_SELECTOR} #search"

# Reported live: search resolution failed for *every* drug, always — not a
# wrong-drug pick, a total failure. Root cause, confirmed from a debug
# dump of the exact run: an earlier version distinguished a real result
# link from the bare "/medications/" link (e.g. "View All Health
# Conditions") by counting path segments, assuming
# `element.get_attribute("href")` returns a fully-resolved absolute URL
# (which would have 5 segments after splitting on "/" — scheme, empty,
# host, "medications", slug). It doesn't, at least not reliably here — it
# returned the raw relative attribute (3 segments: empty, "medications",
# slug), so *every* real result link failed that segment-count check on
# *every* run, not just some. Separately, and confirmed from the same
# dump: the homepage also has an unrelated "popular products" pill list
# elsewhere on the page (`data-testid="products-pill-{slug}"`) whose links
# match the exact same `/medications/{slug}/` shape as a real search
# result — a plain shape-based match without also excluding these would
# risk clicking an unrelated drug instead of what was actually searched
# for. `_is_real_drug_link()` fixes both: matches by regex on whatever
# form the href actually is (relative or absolute, side-stepping the
# get_attribute ambiguity above) rather than counting segments, and
# explicitly excludes both the bare "/medications/" link and the
# "products-pill-*" pattern. The element search itself is also now scoped
# to `#search-overlay-dialog` specifically, rather than the whole page,
# so it can't match that unrelated pill list — or anything else outside
# the dialog — in the first place.
DRUG_LINK_PATTERN = re.compile(r"/medications/[a-z0-9][a-z0-9_-]*/?$", re.IGNORECASE)


def _is_real_drug_link(el) -> bool:
    href = el.get_attribute("href") or ""
    path = href.split("?")[0]
    idx = path.find("/medications/")
    if idx == -1:
        return False
    path = path[idx:]
    if path.rstrip("/") == "/medications" or "/categories/" in path:
        return False
    if (el.get_attribute("data-testid") or "").startswith("products-pill-"):
        return False
    return bool(DRUG_LINK_PATTERN.search(path))

# Confirmed live: plain buttons, not a dropdown — e.g.
# <button data-testid="strength-selection-20mg" aria-label="Select
# Strength: 20mg">20mg</button>. Matched by visible text (via
# _find_and_click_variant_button()) rather than constructing the
# data-testid value directly, since this project's dosage/quantity
# strings aren't guaranteed to exactly match Cost Plus Drugs' own token
# formatting (e.g. "2.5mg").
STRENGTH_BUTTON_PREFIX = "strength-selection-"
QUANTITY_BUTTON_PREFIX = "quantity-selection-"

# Confirmed live: the price calculator panel has a third selector above
# strength/quantity — "Select Form" — for a plain
# <button data-testid="form-selection-{Value}"> (e.g. "form-selection-
# Tablet"), the same shape as strength/quantity. "Select Form"/"Select
# Strength"/"Select Quantity" are themselves just section-heading text,
# not clickable triggers — confirmed live by checking their tag/testid:
# plain <div>s, not buttons.
FORM_BUTTON_PREFIX = "form-selection-"

# Tried in order; first one that yields a parseable price wins. Cost Plus
# Drugs' front-end is a React app that may render prices under varying
# class names — keep this list easy to extend once real markup is observed.
PRICE_SELECTORS = [
    "[data-testid*=price]",
    ".price",
    "[class*=Price]",
    "[class*=price]",
]

# Confirmed live: the page states its price assumption in plain sentence
# form — "A 30 count supply of 20mg Lisinopril will cost: ... $5.55" —
# right above the price. That's the pack size the displayed price is
# actually for; nothing about the URL/slug alone reveals it, and it
# varies per drug (not assumed to always be 30).
QUANTITY_ASSUMPTION_PATTERN = re.compile(r"a\s+(\d+)\s+count supply", re.IGNORECASE)


def _extract_quantity_assumption(text: str) -> str:
    match = QUANTITY_ASSUMPTION_PATTERN.search(text)
    return f"{match.group(1)} count" if match else ""


# Requested directly: surface the shipping cost the page itself discloses.
# Confirmed live: the price breakdown section states it as its own line —
# "Standard Shipping *Additional cost at checkout" followed by a dollar
# amount ($5.25, confirmed unchanged across 30/60/90-count for the same
# drug — a flat fee, not scaled by quantity, at least in what's been
# observed) — separate from and *not* included in "Your Drug Price With
# Us" above it (confirmed live: Manufacturing + Markup + Pharmacy Labor
# alone already sum to that headline price). Bounded to 100 chars after
# the label rather than an unbounded/greedy match, so this can't run past
# an unrelated later $ amount if the page's wording ever shifts.
SHIPPING_PATTERN = re.compile(r"Standard Shipping[^$]{0,100}\$\s*(\d+(?:\.\d{2})?)", re.IGNORECASE)


def _extract_shipping_cost(text: str) -> float | None:
    match = SHIPPING_PATTERN.search(text)
    return float(match.group(1)) if match else None


def _find_and_click_variant_button(driver, prefix: str, target_text: str, timeout: float | None = None) -> bool:
    """Confirmed live: Cost Plus Drugs' strength/quantity selectors are
    plain `<button data-testid="{prefix}{value}">` elements (e.g.
    "strength-selection-20mg", "quantity-selection-90"), not a dropdown —
    clicking one live-updates the displayed price (and, for strength,
    the URL) with no reload needed. Matched by visible button text
    using the same loose substring-normalize check used elsewhere in
    this project, rather than constructing the data-testid value
    directly — safer against a token format mismatch (e.g. "2.5mg").

    Reported live: a real, visible, correctly-labeled strength button
    (e.g. "strength-selection-40mg", confirmed present and matching by
    live DOM inspection) was still reported as "no matching strength
    button found" — and separately, quantity selection silently failed
    the same way. Root cause, confirmed by reproducing the exact flow
    directly: the Silktide cookie-consent backdrop div responsible for
    the "element click intercepted" bug already fixed for the search
    result link (see the resolve_and_select() writeup) isn't scoped to
    the search dialog at all — it's a site-wide overlay that persists
    across the client-side route change onto the drug page itself, and
    was still present and still intercepting clicks here. This
    function's `b.click()` is a native click, and the backdrop
    intercepted it every single time — silently, since the broad
    `except Exception: pass` here swallowed that error the same way it
    would swallow a real "not found," so it just polled until timeout
    and reported a false negative for a button that was genuinely
    present, correctly matched, and simply never successfully clicked.
    Fixed by applying the same native-click-then-JS-fallback pattern
    already used for the search result link: only decide "not found"
    once a poll (for up to `timeout` seconds, defaulting to
    Config.SELENIUM_WAIT_TIMEOUT — this part of the earlier fix, for the
    genuine "button row hasn't rendered yet" race, is still needed and
    correct on its own) truly finds no matching button at all, not
    whenever clicking one happens to fail.

    Reviewed live: the "loose substring-normalize check" this docstring
    used to describe was a plain bidirectional substring test — unanchored,
    so requesting "5mg" against a page whose only strength button was
    "25mg" would match and click that wrong button, with nothing here or
    in any caller ever re-reading the page afterward to confirm which
    strength actually got selected. Fixed by matching through
    utils.token_matches() instead, which requires the two sides' leading
    numbers to be numerically equal (not just one containing the other)
    while still tolerating a differing trailing unit word ("mg" vs
    nothing, "count" vs "tablets") and still substring-matching
    non-numeric values like a form name — see that function's own
    docstring for the full reasoning, shared with the identical fix in
    goodrx_scraper.py's/singlecare_scraper.py's _loose_matches()."""
    if timeout is None:
        timeout = Config.SELENIUM_WAIT_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, f'[data-testid^="{prefix}"]')
            for b in buttons:
                if token_matches(target_text, b.text or ""):
                    robust_click(driver, b)
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _wait_for_stable_price(driver, timeout: float) -> str | None:
    """Wait for a $ amount to appear in the rendered page — AND still be
    there ~1s later, not just momentarily. Returns the confirmed-stable body
    text (so the caller extracts from the exact text that was verified,
    rather than re-querying and risking yet another race), or None if
    nothing ever stabilized within the timeout.

    This is a client-rendered Next.js page with no data-testid/class hook to
    wait on (it uses plain Tailwind utility classes), so this polls the
    first-dollar-amount text pattern itself rather than a selector. The
    "still there a beat later" re-check is deliberate: confirmed live, the
    price can flash into the DOM and then disappear again a moment
    later — consistent with the site's bot-scoring intermittently killing
    an in-flight retry of its own price-data fetch mid-render.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = driver.find_element(By.TAG_NAME, "body").text
        if extract_price_from_text(text) is not None:
            time.sleep(1)
            text_again = driver.find_element(By.TAG_NAME, "body").text
            if extract_price_from_text(text_again) is not None:
                return text_again
            # It flashed and vanished — keep polling rather than giving up.
        time.sleep(0.5)
    return None


class _CostPlusDrugsSeleniumFallback(SeleniumScraperBase):
    def resolve_and_select(
        self, drug_name: str, formulation: str, dosage: str, quantity: str | None
    ) -> tuple[str, str, str]:
        """Returns (resolved_url, selection_caveat, error). See module
        docstring for the search flow and why it replaces slug-guessing.
        selection_caveat is separate from error: not finding a matching
        form/strength button isn't a total failure — the page still has
        *some* price on it (the search result's default form/dosage) —
        but silently returning that as if it were the requested one would
        repeat the same "silently wrong data" mistake already fixed
        elsewhere in this project, so it's flagged instead. Quantity gets
        no such caveat — see the quantity block below for why."""
        try:
            self.driver.get(SEARCH_HOME_URL)
        except Exception as e:
            return "", "", f"navigation to Cost Plus Drugs homepage failed: {e}"

        try:
            trigger = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, SEARCH_TRIGGER_SELECTOR))
            )
            # Reported live: "element click intercepted ... Other element
            # would receive the click: <div id='silktide-backdrop' ...>"
            # — the exact same cookie-consent overlay already confirmed
            # and fixed for the strength/quantity/form buttons and the
            # search-result-link click elsewhere in this file, just at
            # this click site (the very first one, opening the search
            # dialog) instead — that fallback was never applied here.
            # Same fix: fall back to a JS-dispatched click, which invokes
            # the button's own click handler directly regardless of
            # what's visually drawn on top of it.
            robust_click(self.driver, trigger)
            search_input = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_INPUT_SELECTOR))
            )
            search_input.send_keys(drug_name)
        except Exception as e:
            return "", "", f"could not open/type into Cost Plus Drugs' search: {e}"

        # Reported live (twice): search resolution failed for '{drug_name}'
        # despite the debug snapshot taken right after the failure showing
        # the correct result links present in the DOM all along. Root
        # cause, confirmed by replaying the exact snapshot's markup
        # through _is_real_drug_link()/DRUG_LINK_PATTERN above: the
        # matching logic itself is fine. The bug was in how it was waited
        # for — WebDriverWait(...).until(lambda d: ...) iterates live
        # elements and calls .get_attribute() on each, but Selenium's
        # WebDriverWait only auto-ignores NoSuchElementException by
        # default, not StaleElementReferenceException. This dialog
        # re-renders its suggestion list on every keystroke (typed via
        # send_keys, i.e. character by character), so it's easy for an
        # element handed out by one find_elements() call to go stale
        # before _is_real_drug_link() finishes reading its attributes —
        # when that happened, the exception propagated straight out of
        # .until() and killed the wait immediately, well before its real
        # timeout and well before the DOM had actually settled. Replaced
        # with an explicit poll loop (same "read twice, must match"
        # stability pattern used in goodrx_scraper.py/
        # singlecare_scraper.py's suggestion-click fixes) that catches
        # per-iteration exceptions instead of letting them abort the wait,
        # and only clicks once the set of matching hrefs reads the same
        # on two consecutive passes — guarding against clicking a
        # suggestion that only reflects a not-yet-finished keystroke too.
        link = None
        deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
        last_hrefs = None
        while time.monotonic() < deadline:
            try:
                candidates = [
                    a for a in self.driver.find_elements(
                        By.CSS_SELECTOR, f'{SEARCH_DIALOG_SELECTOR} a[href*="/medications/"]'
                    )
                    if _is_real_drug_link(a)
                ]
                hrefs = tuple(a.get_attribute("href") for a in candidates)
            except Exception:
                # Likely a stale element mid-re-render — the DOM is still
                # changing, so treat this as "not settled yet" and retry
                # rather than giving up.
                time.sleep(0.3)
                continue
            if hrefs and hrefs == last_hrefs:
                link = candidates[0]
                break
            last_hrefs = hrefs
            time.sleep(0.3)

        if link is None:
            self._save_debug_page("page_costplusdrugs_no_search_results.html")
            return "", "", (
                f"no search results appeared for '{drug_name}' — see "
                "page_costplusdrugs_no_search_results.html"
            )

        # Reported live again: even after the stability check above passed,
        # link.click() itself could still raise (StaleElementReferenceException
        # if one more re-render slipped in between the check and the click —
        # a narrow but real window). The first fix for this re-found the
        # element via a CSS selector built from the href string captured
        # during the stability check — but that's exactly the same
        # get_attribute("href") relative-vs-absolute ambiguity documented
        # above, biting a different piece of code: confirmed live, that
        # selector attribute value is matched against the *literal* HTML
        # attribute, while get_attribute("href") had returned the
        # browser-resolved absolute form here — so the selector could never
        # match anything, "no such element" every time, regardless of
        # whether the link was actually there. Fixed by not trying to
        # relocate the element by its href string at all: just re-run the
        # same find-and-filter query fresh and click immediately, a few
        # times, since a fresh query always returns live (non-stale)
        # elements as of that instant.
        # Reported live yet again, this time with the actual error attached:
        # "element click intercepted ... Other element would receive the
        # click: <div id="silktide-backdrop" ...>". A genuinely different
        # bug from the two stale-element races above — a cookie-consent
        # widget's (Silktide) backdrop div was sitting on top of the whole
        # page. Selenium's native .click() refuses to click anything that
        # isn't the actual topmost element at that pixel, so plain retries
        # of the same native click would fail identically every time the
        # backdrop is present, no matter how many attempts. Rather than
        # deciding cookie-consent semantics (accept/decline) to dismiss it,
        # this instead dispatches the click via JS
        # (`execute_script("arguments[0].click()", ...)`), which invokes
        # the element's own click handler directly and is unaffected by
        # whatever else is visually drawn on top of it — this project
        # never needs to interact with that widget at all, just get past
        # it. Tried after a plain click fails rather than unconditionally,
        # since a native click is the more faithful simulation of a real
        # user when nothing is actually blocking it.
        clicked = False
        last_error = None
        for _ in range(3):
            try:
                fresh = [
                    a for a in self.driver.find_elements(
                        By.CSS_SELECTOR, f'{SEARCH_DIALOG_SELECTOR} a[href*="/medications/"]'
                    )
                    if _is_real_drug_link(a)
                ]
                if not fresh:
                    time.sleep(0.3)
                    continue
                target = fresh[0]
                robust_click(self.driver, target)
                clicked = True
                break
            except Exception as e:
                last_error = e
                time.sleep(0.3)

        if not clicked:
            self._save_debug_page("page_costplusdrugs_no_search_results.html")
            return "", "", f"found a search result for '{drug_name}' but could not click it: {last_error}"

        try:
            WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                lambda d: "/medications/" in d.current_url
                and d.current_url.rstrip("/") != f"{SEARCH_HOME_URL}/medications"
            )
        except Exception:
            pass

        # Confirmed live: the search result lands on the drug's *default*
        # form/dosage page regardless of what was searched for — these
        # on-page buttons are what actually reach the requested ones.
        # Form is selected first, matching the page's own top-to-bottom
        # order ("Select Form" above "Select Strength" above "Select
        # Quantity") — unconfirmed whether choosing form after strength
        # would ever matter (e.g. resetting strength), but there's no
        # reason to risk it when matching the page's own order is free.
        caveats = []
        if formulation:
            if _find_and_click_variant_button(self.driver, FORM_BUTTON_PREFIX, formulation):
                time.sleep(1)  # let the client-side route/price update settle
            else:
                caveats.append(f"could not select form {formulation} (no matching form button found)")

        if dosage:
            if _find_and_click_variant_button(self.driver, STRENGTH_BUTTON_PREFIX, dosage):
                time.sleep(1)
            else:
                caveats.append(f"could not select dosage {dosage} (no matching strength button found)")

        if quantity:
            if _find_and_click_variant_button(self.driver, QUANTITY_BUTTON_PREFIX, quantity):
                time.sleep(1)

        resolved = self.driver.current_url.split("?")[0].rstrip("/")
        return resolved, "; ".join(caveats), ""

    def extract_price(self) -> tuple[float | None, str, str, float | None]:
        stable_text = _wait_for_stable_price(self.driver, Config.SELENIUM_WAIT_TIMEOUT)
        if stable_text is None:
            self._save_debug_page("page_costplusdrugs_no_price.html")
            return None, "", "", None

        # Extract from the confirmed-stable live text directly, not
        # page_source — confirmed live that driver.page_source can lag
        # behind what .text already reliably shows for this page's
        # client-rendered price (a separate issue from the flash above:
        # this is page_source itself trailing the live DOM, not the price
        # disappearing). No selector-based extraction is possible here
        # either way — this page has no data-testid/class hook on the price.
        price = extract_price_from_text(stable_text)
        match = re.search(r".{0,30}\$\s?\d{1,4}(?:\.\d{2})?.{0,10}", stable_text)
        raw = match.group(0) if match else ""
        quantity = _extract_quantity_assumption(stable_text)
        shipping_cost = _extract_shipping_cost(stable_text)
        return price, raw, quantity, shipping_cost


def get_prices(
    drug_name: str,
    formulation: str,
    dosage: str,
    zip_code: str | None = None,
    quantity: str | None = None,
) -> list[PriceResult]:
    """Never raises — any unexpected failure is returned as an error PriceResult
    so main.py can treat every site the same way."""
    url = ""
    try:
        if Config.COSTPLUSDRUGS_LOOKUP_MODE == "file":
            try:
                entries = load_formulary(Config.COSTPLUSDRUGS_FORMULARY_PATH)
            except FileNotFoundError:
                return [
                    PriceResult(
                        drug_name=drug_name,
                        formulation=formulation,
                        dosage=dosage,
                        source=SOURCE_NAME,
                        error=(
                            f"COSTPLUSDRUGS_LOOKUP_MODE=file but formulary file not found: "
                            f"{Config.COSTPLUSDRUGS_FORMULARY_PATH}"
                        ),
                    )
                ]
            if find_entry(entries, drug_name, dosage, formulation) is None:
                return [
                    PriceResult(
                        drug_name=drug_name,
                        formulation=formulation,
                        dosage=dosage,
                        source=SOURCE_NAME,
                        error=(
                            "not found in Team Cuban Card formulary list — "
                            "not carried by Cost Plus Drugs (skipped live lookup)"
                        ),
                    )
                ]
            # Found in the formulary — it has no price column, so the live
            # site is still needed for the actual $ number. Fall through.

        try:
            with _CostPlusDrugsSeleniumFallback(headless=Config.HEADLESS_DEFAULT) as scraper:
                resolved_url, selection_caveat, resolve_error = scraper.resolve_and_select(
                    drug_name, formulation, dosage, quantity
                )
                if not resolved_url:
                    return [
                        PriceResult(
                            drug_name=drug_name, formulation=formulation, dosage=dosage,
                            source=SOURCE_NAME,
                            error=resolve_error or f"could not resolve a Cost Plus Drugs page for '{drug_name}'",
                        )
                    ]
                url = resolved_url
                price, raw, quantity_shown, shipping_cost = scraper.extract_price()
        except Exception as e:
            return [
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, url=url, error=f"Selenium failed: {e}",
                )
            ]

        if price is None:
            return [
                PriceResult(
                    drug_name=drug_name,
                    formulation=formulation,
                    dosage=dosage,
                    source=SOURCE_NAME,
                    url=url,
                    error="no price found",
                )
            ]

        label = "cash price (no insurance)"
        # Requested directly: fold the page's own disclosed shipping fee
        # into `price` rather than tracking it as a separate field, with
        # a note so the total isn't silently different from what the
        # page's own headline "Your Drug Price With Us" number shows —
        # confirmed live that fee is stated separately there (see
        # _extract_shipping_cost()'s docstring), not already included.
        if shipping_cost is not None:
            price = round(price + shipping_cost, 2)
            label += f" — includes ${shipping_cost:.2f} shipping"
        if selection_caveat:
            label += f" — prices may be inaccurate: {selection_caveat}"

        return [
            PriceResult(
                drug_name=drug_name,
                formulation=formulation,
                dosage=dosage,
                source=SOURCE_NAME,
                price=price,
                price_label=label,
                quantity=quantity_shown,
                url=url,
                raw_text=raw,
            )
        ]
    except Exception as e:
        return [
            PriceResult(
                drug_name=drug_name,
                formulation=formulation,
                dosage=dosage,
                source=SOURCE_NAME,
                url=url,
                error=f"unexpected error: {e}",
            )
        ]


if __name__ == "__main__":
    results = get_prices("Lisinopril", "tablet", "20mg")
    for r in results:
        print(r)
