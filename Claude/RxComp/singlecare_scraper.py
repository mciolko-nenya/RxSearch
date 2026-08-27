"""
SingleCare scraper.

Originally, a plain HTTP fetch of a SingleCare drug page returned a
server-rendered pricing table without hitting bot detection — confirmed
during planning research. That's no longer true: every path on
singlecare.com, including the homepage, now returns an identical 403 to a
plain `requests` fetch — confirmed live, this isn't page-specific bot
detection, it's blocking the request itself regardless of what it asks
for. So Selenium (undetected-chromedriver, via SeleniumScraperBase) is the
only path now, not a fallback.

Drug name -> URL resolution goes through SingleCare's own real search box
(#searchbar on the homepage, a JS-driven autocomplete — see
resolve_drug_url()), not a guessed URL slug. An earlier version built the
URL directly from the drug name (singlecare_slug()) — reported live, and
confirmed: this breaks for any drug whose real SingleCare page uses a
salt-form-qualified name. "atorvastatin" isn't a real page; the real one
is "atorvastatin-calcium". There is no algorithmic way to know the right
suffix ("-calcium", "-hcl", "-besylate", "-succinate", ...) for an
arbitrary drug without asking the site itself, so this asks the site
itself: it drives the real search autocomplete and follows wherever the
top suggestion actually leads, the same page a real user would land on.

Selenium itself gets challenged too: confirmed live, DataDome serves an
iframed CAPTCHA widget (`geo.captcha-delivery.com`, title "DataDome
CAPTCHA") instead of the page. No headless session can solve that — it
needs an actual human — so _resolve_bot_challenge() below opens a visible
window and prompts at the terminal, the same pattern used in
goodrx_scraper.py and amazon_scraper.py's --setup-amazon.

The resolved page's *default* dosage/quantity may also not match what the
user asked for (that's chosen via a custom listbox widget, not a native
<select> — see DOSAGE_INDICATOR_SELECTORS's docstring). refine() attempts
to reselect it directly; if that interaction fails (selectors drift, as
expected per the "try a list of selectors" resilience pattern), the
original rows are still returned but flagged in price_label that they may
be for a different dosage/quantity than requested, rather than silently
misleading the user.

ZIP code works the same way: the resolved page comes back geo-IP-guessed
(confirmed live: it guessed the scraping machine's own location, not
anything requested) unless explicitly set via the real "Enter Location"
modal dialog (see _try_enter_zip()) — verified live to actually change the
displayed prices/pharmacies, not just guessed.

Every click in this file now goes through driver_utils.robust_click()
rather than a bare `.click()` — reviewed live: this file had no
defense at all against a click being intercepted by something drawn on
top of the real target (e.g. a cookie-consent backdrop), unlike
costplusdrugs_scraper.py, which discovered and fixed that exact failure
mode three separate times after live cookie-consent-overlay failures.
No such overlay has actually been observed intercepting a click on this
page — this is a preemptive, shared fix for a real, general risk (the
same class of third-party widget Cost Plus Drugs' page has), not a
response to a confirmed SingleCare-specific bug.
"""

from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup

from config import Config
from driver_utils import (
    INTERACTIVE_CHALLENGE_LOCK,
    SeleniumScraperBase,
    robust_click,
    wait_for_challenge_confirmation,
)
from models import PriceResult
from utils import extract_price_from_text, token_matches

SOURCE_NAME = "SingleCare"

# Confirmed live: a real, JS-driven autocomplete search — typing a drug
# name into #searchbar (aria-label "Search drugs") shows a dropdown of
# role="option" suggestions (e.g. "Atorvastatin Calcium (Lipitor)",
# "Ezetimibe-Atorvastatin", ranked with the single-ingredient match
# first for that query), and clicking one navigates to that drug's real
# canonical /prescription/{slug} URL — see resolve_drug_url().
SEARCH_HOME_URL = "https://www.singlecare.com"
SEARCH_INPUT_SELECTOR = "#searchbar"
SEARCH_OPTION_SELECTOR = '[role="option"]'

# Confirmed live in the page HTML when SingleCare blocks a lookup instead of
# serving the drug page: a DataDome challenge, delivered via an iframe
# titled "DataDome CAPTCHA" from a captcha-delivery.com/captcha/ URL.
#
# Deliberately NOT a bare "datadome" marker: confirmed live, a *legitimately
# loaded* drug page (with real prices) still contains the ambient template
# comment "<!-- Datadome script snippet -->" in its <head> — present
# whether or not a challenge is actually shown. A bare "datadome" match
# false-positived on that, permanently reporting "still blocked" on pages
# that had, in fact, loaded successfully. These two markers require the
# actual challenge-specific context (the iframe's title, and its specific
# /captcha/ URL path) instead of just the vendor's name appearing anywhere.
BOT_CHALLENGE_MARKERS = (
    "datadome captcha",
    "captcha-delivery.com/captcha/",
)

# Tried in order; first selector that yields >=1 match wins.
# "#pharmacyItemContainer" is confirmed live real markup — yes, an id
# shared by every per-pharmacy row (invalid HTML — ids should be unique —
# but that's what the live site actually does, and browsers/BeautifulSoup
# both tolerate it fine). The rest are speculative fallbacks in case the
# site changes and this one stops matching.
PHARMACY_ROW_SELECTORS = [
    "#pharmacyItemContainer",
    "[data-testid*=pharmacy]",
    "[class*=PharmacyRow]",
    "[class*=pharmacy-row]",
    "[class*=PriceCard]",
    "[class*=price-card]",
]

# Confirmed live: SingleCare has no native <select> elements anywhere on
# the page at all — Form/Dosage/Quantity/Brand are all custom
# `div[role="listbox"]` widgets (aria-labeled "Select form"/"Select
# dosage"/"Select quantity"/"Select brand"), each with `[role="option"]`
# children. An earlier version of this file's dosage-selection code
# searched for `select[name*=dosage]`/`select[name*=strength]`/`select` —
# since none of those ever existed, that lookup silently found nothing on
# every single run, not just occasionally; dosage mismatches were never
# actually correctable, only detectable. `.custom-select__trigger`'s
# first <span> child holds the currently-displayed value (e.g. "10mg",
# "90 count") — confirmed live to update immediately (with prices
# re-rendering) after clicking a matching `[role="option"]`, no separate
# "confirm" step needed, unlike GoodRx's modal.
DOSAGE_INDICATOR_SELECTORS = [
    '[aria-label="Select dosage"] .custom-select__trigger span',
    "[data-testid*=dosage]",
    "[data-testid*=strength]",
    "[class*=Dosage]",
    "[class*=Strength]",
]

# Reported live: --formulation was accepted as a parameter throughout
# this file but never actually used, unlike dosage/quantity — unlike
# GoodRx (confirmed to have no on-page formulation control at all) and
# Amazon, SingleCare's own "Select form" listbox is the exact same
# custom-listbox widget already confirmed live for dosage/quantity above
# (see this section's own docstring), so formulation can be selected for
# real here, the same way, rather than only ever flagged as unverified.
FORM_INDICATOR_SELECTORS = [
    '[aria-label="Select form"] .custom-select__trigger span',
    "[data-testid*=form]",
    "[class*=Form]",
]

# Reported live: a Quantity dropdown exists too (same custom-listbox
# widget as dosage) and does update listed prices — confirmed live,
# selecting "30" instead of the default "90" changed Acme's price from
# $10.91 to $7.64. `quantity` was accepted as a parameter throughout this
# file but never actually used anywhere in it until now.
QUANTITY_INDICATOR_SELECTORS = [
    '[aria-label="Select quantity"] .custom-select__trigger span',
]

# Confirmed live real markup: a hidden input carries the numeric ZIP the
# page is currently priced for (e.g. <input id="zip-code" name="zip Code"
# type="hidden" value="23666">) — easier and more reliable to read than
# parsing the visible "23666 - Hampton, VA" location-search input's value.
ZIP_INDICATOR_SELECTORS = ["#zip-code", "#zipcode", "input[name*=zip]"]


def _extract_pharmacy_name(el) -> str:
    """Confirmed live: the pharmacy name isn't in any text node within a
    row — only in an <img data-name="kroger pharmacy"> or
    <img alt="Lisinopril coupon at Kroger Pharmacy">. get_text() alone
    can't recover it; it just silently omits image alt text."""
    img = el.select_one("img[data-name]")
    if img and img.get("data-name"):
        return img["data-name"].title()
    img = el.select_one("img[alt]")
    if img and img.get("alt"):
        # "Lisinopril coupon at Kroger Pharmacy" -> "Kroger Pharmacy"
        return re.sub(r"^.*?\bat\s+", "", img["alt"]).strip()
    return ""


def _extract_row_price(el, whole_text: str, selector: str) -> tuple[float | None, float | None, str]:
    """Returns (price, no_bonus_price, no_bonus_raw).

    Confirmed live: SingleCare's "Member Bonus" is a free, opt-in signup
    discount (not a paid subscription like GoodRx's Companion) — a flat
    $3.00 off some pharmacies' prices. Its real displayed price lives in
    `.pharmacy-item__price.bonusPrice .pharmacy-item__price`; the
    no-signup price appears separately, struck through, in
    `.cutPriceNew` — present (non-empty) only on rows that actually have
    the bonus; empty on flat-price rows.

    This matters for more than just an extra data point: `.cutPriceNew`'s
    $ amount appears *earlier* in the row's DOM/text order than the real
    displayed price. The previous version extracted price by searching
    the whole row's flattened text for the first "$X.XX" — confirmed
    live, that silently returned the *no-signup* (higher) price for
    every Member Bonus row, not the actual displayed one. E.g. one row
    read "$13.91" here while the page's own headline price for that same
    pharmacy was "$10.91" — a materially wrong number reported with no
    indication anything was off, not merely a missing second price.
    `.pharmacy-item__price.bonusPrice .pharmacy-item__price` also matches
    correctly on flat-price rows (`bonusPrice` turned out to be a generic
    styling class, not an actual "has a bonus" signal — confirmed live
    against a row with no bonus at all), so it replaces the whole-text
    search entirely for the confirmed selector, with the old whole-text
    guess kept only as a fallback for the speculative selectors below it
    in PHARMACY_ROW_SELECTORS, which have no specific structure to target."""
    if selector == "#pharmacyItemContainer":
        price_el = el.select_one(".pharmacy-item__price.bonusPrice .pharmacy-item__price")
        price = extract_price_from_text(price_el.get_text(" ", strip=True)) if price_el else extract_price_from_text(whole_text)
        no_bonus_price, no_bonus_raw = None, ""
        cut_el = el.select_one(".cutPriceNew")
        if cut_el:
            cut_text = cut_el.get_text(" ", strip=True)
            if cut_text:
                no_bonus_price = extract_price_from_text(cut_text)
                no_bonus_raw = cut_text
        return price, no_bonus_price, no_bonus_raw
    return extract_price_from_text(whole_text), None, ""


def _extract_pharmacy_rows(soup: BeautifulSoup) -> list[dict]:
    rows = []
    for selector in PHARMACY_ROW_SELECTORS:
        elements = soup.select(selector)
        if not elements:
            continue
        for el in elements:
            text = el.get_text(" ", strip=True)
            price, no_bonus_price, no_bonus_raw = _extract_row_price(el, text, selector)
            if price is None:
                continue
            name = _extract_pharmacy_name(el)
            if not name:
                if selector == "#pharmacyItemContainer":
                    # Confirmed live: a couple of these rows have no
                    # identifiable pharmacy image at all (empty alt, no
                    # data-name) — hidden/duplicate template variants, not
                    # real listings. Skip rather than mislabel with
                    # unrelated button/tooltip text scraped from the same
                    # row (e.g. "How is this calculated?" is not a
                    # pharmacy name).
                    continue
                # Fallback for the other, speculative selectors, in case
                # they carry the name as plain text instead.
                name = re.sub(r"\$\s?\d{1,4}(?:\.\d{2})?", "", text).strip(" -—|")
                if not name:
                    continue
            rows.append({
                "pharmacy": name[:40], "price": price, "raw": text[:200],
                "no_bonus_price": no_bonus_price, "no_bonus_raw": no_bonus_raw,
            })
        if rows:
            break
    return rows


def _detect_rendered_dosage(soup: BeautifulSoup) -> str:
    for selector in DOSAGE_INDICATOR_SELECTORS:
        el = soup.select_one(selector)
        if el:
            return el.get_text(strip=True)
    return ""


def _detect_rendered_quantity(soup: BeautifulSoup) -> str:
    for selector in QUANTITY_INDICATOR_SELECTORS:
        el = soup.select_one(selector)
        if el:
            return el.get_text(strip=True)
    return ""


def _detect_rendered_form(soup: BeautifulSoup) -> str:
    for selector in FORM_INDICATOR_SELECTORS:
        el = soup.select_one(selector)
        if el:
            return el.get_text(strip=True)
    return ""


def _loose_matches(rendered: str, requested: str) -> bool:
    """Shared by dosage and quantity — tolerates "90" vs "90 count",
    "20mg" vs "20 mg", etc. without needing an exact match.

    Reviewed live: this used to do its own normalize-and-substring
    check, unanchored — e.g. a requested quantity "30" is a literal
    substring of a rendered "130", or requested dosage "25mcg" a
    literal substring of rendered "125mcg". A page showing the wrong
    (but substring-colliding) value would be treated as already
    matching, skipping the Selenium correction and its caveat entirely.
    Fixed by delegating to utils.token_matches(), which requires the
    two sides' leading numbers to be numerically equal rather than
    merely one containing the other — see that function's docstring,
    shared with the identical fix in goodrx_scraper.py's own
    _loose_matches() and costplusdrugs_scraper.py's
    _find_and_click_variant_button()."""
    if not rendered:
        # Can't tell either way — don't trigger the Selenium fallback on
        # a guess; treat as matching.
        return True
    return token_matches(requested, rendered)


def _dosage_matches(rendered: str, requested: str) -> bool:
    return _loose_matches(rendered, requested)


def _quantity_matches(rendered: str, requested: str | None) -> bool:
    if not requested:
        return True
    return _loose_matches(rendered, requested)


def _form_matches(rendered: str, requested: str | None) -> bool:
    if not requested:
        return True
    return _loose_matches(rendered, requested)


def _detect_rendered_zip(soup: BeautifulSoup) -> str:
    for selector in ZIP_INDICATOR_SELECTORS:
        el = soup.select_one(selector)
        if el and el.get("value"):
            return el["value"].strip()
    return ""


def _zip_matches(rendered: str, requested: str | None) -> bool:
    if not requested or not rendered:
        # No --zip requested, or couldn't read the page's current one —
        # nothing to enforce; don't trigger the Selenium fallback on a
        # guess (same "can't tell, assume matching" rule as dosage above).
        return True
    return rendered.strip() == requested.strip()


def _looks_like_bot_challenge(driver) -> bool:
    try:
        haystack = (driver.title or "") + " " + driver.page_source
    except Exception:
        return False  # can't tell — treat as not-a-challenge, let normal extraction report "no rows"
    haystack_lower = haystack.lower()
    return any(marker in haystack_lower for marker in BOT_CHALLENGE_MARKERS)


class _SingleCareSeleniumFallback(SeleniumScraperBase):
    """Used both when the plain fetch is blocked outright (bot detection —
    increasingly the common case, see get_prices()) and when its default
    dosage doesn't match the request: attempts to select the right
    dosage/quantity dropdown option, then re-extract the price rows.
    Best-effort: if the dropdown selectors don't match, this falls through
    and just re-parses whatever loaded rather than erroring outright."""

    def _resolve_bot_challenge(self, url: str) -> bool:
        """SingleCare sometimes serves an interactive DataDome CAPTCHA
        instead of the drug page — a headless session can't solve that,
        since nothing is looking at it. Switch to (or reuse, if already
        visible) a real Chrome window and let the person running this tool
        solve it directly, same interactive pattern as
        goodrx_scraper.py/amazon_scraper.py's --setup-amazon. Returns True
        once the page looks clear, False if the user skips it (blank Enter)
        or it's still blocked afterward.

        Guarded by INTERACTIVE_CHALLENGE_LOCK: main.py runs sites
        concurrently by default, and GoodRx can hit this same situation
        (see goodrx_scraper.py) — the lock makes sure only one challenge is
        ever presented to the user at a time, rather than two threads
        racing to print/input() simultaneously."""
        if not Config.SINGLECARE_INTERACTIVE_CAPTCHA:
            return False

        with INTERACTIVE_CHALLENGE_LOCK:
            print("\nSingleCare is showing a bot-check (CAPTCHA) page for this lookup.")
            if self.headless:
                print("Opening a visible Chrome window so you can solve it...")
                self.close()
                self.headless = False
                self._create_driver()
                try:
                    self.driver.get(url)
                except Exception:
                    pass
            wait_for_challenge_confirmation(
                "Solve it in the Chrome window, then press Enter here to continue "
                "(or just press Enter to skip this site)... "
            )
            # The user may have solved it, refreshed, or navigated within
            # the same window — reload the target URL fresh before
            # re-checking, in case they ended up somewhere else.
            try:
                self.driver.get(url)
            except Exception:
                pass
            # Confirmed live: even with a valid solved-challenge cookie
            # already set, DataDome can briefly re-show an
            # interstitial/verifying state on this reload before redirecting
            # to the real page a moment later. Checking once, immediately,
            # can catch that transient state and wrongly report "still
            # blocked" even though the real page (with real prices) loads
            # a beat afterward. Poll instead of judging from one snapshot.
            deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
            while time.monotonic() < deadline:
                if not _looks_like_bot_challenge(self.driver):
                    return True
                time.sleep(0.5)
            return False

    def _try_enter_zip(self, zip_code: str) -> bool:
        """Confirmed live end-to-end via direct browser interaction
        (outside the CAPTCHA-gated Selenium path, to see the real flow
        without guessing): the visible "23666 - Hampton, VA" text isn't a
        field you type into at all — #zipValue (searched by an earlier
        version of this method) is a display-only decoy (note its
        "nocursor" class; it's never actually editable). The real
        location control is a `div[role=button][aria-label="Enter
        Location"]` that opens a genuine modal dialog containing its own
        `input[aria-label="Enter zip code"]` (no autocomplete dropdown —
        typing a valid ZIP just resolves to a "City, ST" label beneath it)
        and a "Done" button to confirm. Verified this exact sequence
        actually changes the displayed prices and pharmacies (Hampton, VA
        -> New York, NY) and updates the hidden #zip-code/#zipcode fields
        _detect_rendered_zip() reads."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            trigger = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '[aria-label="Enter Location"]'))
            )
            robust_click(self.driver, trigger)
        except Exception:
            return False

        try:
            zip_input = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[aria-label="Enter zip code"]'))
            )
            robust_click(self.driver, zip_input)
            try:
                zip_input.clear()
            except Exception:
                pass
            zip_input.send_keys(zip_code)
            time.sleep(1)  # let the "City, ST" label resolve and Done enable

            done_button = next(
                (b for b in self.driver.find_elements(By.TAG_NAME, "button") if b.text.strip() == "Done"),
                None,
            )
            if done_button:
                robust_click(self.driver, done_button)
            else:
                from selenium.webdriver.common.keys import Keys

                zip_input.send_keys(Keys.RETURN)
            return True
        except Exception:
            return False

    def _try_select_listbox(self, aria_label: str, target_text: str) -> bool:
        """Confirmed live end-to-end: SingleCare's Form/Dosage/Quantity
        controls are all the same custom `div[role="listbox"]` widget, not
        native <select> elements (see DOSAGE_INDICATOR_SELECTORS's
        docstring — a previous version's `select[name*=dosage]`-style
        lookup could never have matched anything here). Clicking the
        `.custom-select__trigger` child specifically opens it — clicking
        the outer `[role="listbox"]` wrapper itself was tried first and
        did *not* open it, confirmed live via aria-expanded staying
        "false". Once open, `[role="option"]` children are matched
        loosely (same tolerant substring check as _loose_matches) and
        clicked directly; verified live that this closes the dropdown and
        immediately re-renders prices with no separate "confirm" step,
        unlike GoodRx's modal-based equivalent."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            wrapper = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, f'[aria-label="{aria_label}"]'))
            )
            trigger = wrapper.find_element(By.CSS_SELECTOR, ".custom-select__trigger")
            robust_click(self.driver, trigger)
            options = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                lambda d: wrapper.find_elements(By.CSS_SELECTOR, '[role="option"]') or None
            )
            for option in options:
                if _loose_matches(option.text.strip(), target_text):
                    robust_click(self.driver, option)
                    return True
            # Nothing matched — close the dropdown rather than leave it
            # open covering the price list beneath it.
            robust_click(self.driver, trigger)
            return False
        except Exception:
            return False

    def resolve_drug_url(self, drug_name: str) -> tuple[str, str]:
        """Returns (resolved_url, error). Drives SingleCare's real
        homepage search — see SEARCH_HOME_URL's docstring — rather than
        guessing a URL slug from drug_name: reported live, and confirmed,
        guessing breaks for any drug whose real page uses a salt-form
        suffix (e.g. "atorvastatin-calcium", not "atorvastatin"), and
        there's no way to know which suffix an arbitrary drug needs
        without asking the site. This asks the site: types drug_name
        into the search box and clicks the top-ranked suggestion,
        following wherever it actually navigates.

        Confirmed live end-to-end for "atorvastatin": lands on
        `https://www.singlecare.com/prescription/atorvastatin-calcium`
        (with a `?q=...&isHomeSearch=true` query string stripped off
        here — harmless, but not part of the real canonical URL).
        Best-effort on picking *which* suggestion: always the first
        (top-ranked) one, since that was confirmed live to be the
        correct single-ingredient match for this query, with
        combination-drug variants ranked after it — not a guarantee
        SingleCare's ranking puts the right match first for every
        possible drug name, just the best signal available without
        per-drug disambiguation logic this project has no way to
        maintain correctly."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            self.driver.get(SEARCH_HOME_URL)
        except Exception as e:
            return "", f"navigation to SingleCare homepage failed: {e}"

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_bot_challenge(SEARCH_HOME_URL):
                self._save_debug_page("page_singlecare_search_challenge_unresolved.html")
                return "", (
                    "bot check (CAPTCHA) shown on SingleCare's search and not resolved — "
                    "see page_singlecare_search_challenge_unresolved.html, or set "
                    "SINGLECARE_INTERACTIVE_CAPTCHA=false to skip this prompt"
                )

        try:
            search_input = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_INPUT_SELECTOR))
            )
            robust_click(self.driver, search_input)
            search_input.send_keys(drug_name)
        except Exception as e:
            return "", f"could not type into SingleCare's search box: {e}"

        # Reported live: this landed on a completely unrelated,
        # "nonsensical" drug relative to what was searched for — not just
        # occasionally slow, genuinely the wrong result. Root cause:
        # send_keys() below types character by character, and this
        # dropdown updates live on every keystroke — a plain presence
        # check can catch it still showing suggestions for an early
        # partial keystroke (e.g. just "a") rather than the completed
        # drug name, and click whatever top suggestion that partial
        # query happens to rank first. Fixed the same "checked before it
        # settled" way as everywhere else in this project: poll for the
        # option list's own text content to read the same (non-empty)
        # set twice in a row, ~0.3s apart, rather than trusting the very
        # first non-empty read.
        options = []
        deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
        last_texts = None
        while time.monotonic() < deadline:
            candidates = self.driver.find_elements(By.CSS_SELECTOR, SEARCH_OPTION_SELECTOR)
            texts = tuple((o.text or "").strip() for o in candidates)
            if texts and all(texts) and texts == last_texts:
                options = candidates
                break
            last_texts = texts
            time.sleep(0.3)

        if not options:
            self._save_debug_page("page_singlecare_no_search_suggestions.html")
            return "", (
                f"no search suggestions appeared for '{drug_name}' — see "
                "page_singlecare_no_search_suggestions.html"
            )
        try:
            robust_click(self.driver, options[0])
        except Exception as e:
            return "", f"could not click a search suggestion for '{drug_name}': {e}"

        try:
            WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                lambda d: "/prescription/" in d.current_url
            )
        except Exception:
            pass

        resolved = self.driver.current_url.split("?")[0]
        if "/prescription/" not in resolved:
            self._save_debug_page("page_singlecare_search_no_match.html")
            return "", (
                f"search for '{drug_name}' did not land on a drug page "
                f"(ended up at {resolved}) — see page_singlecare_search_no_match.html"
            )
        return resolved, ""

    def refine(
        self, url: str, formulation: str, dosage: str, quantity: str | None, zip_code: str | None = None
    ) -> tuple[list[dict], str, str, str, str, str, str]:
        """Returns (rows, error, zip_caveat, dosage_caveat, quantity_caveat,
        formulation_caveat, rendered_quantity). The caveats are separate from error/rows
        because a mismatch shouldn't be conflated with a total failure: an
        attempt can fail silently while the rest of dosage/quantity
        selection and price extraction still succeed against the now-stale
        page — confirmed live for ZIP, that combination previously
        produced clean-looking prices for the wrong location with zero
        indication anything was off, since the only zip check that existed
        ran *before* this method, purely to decide whether to call it at
        all. Applying the same after-the-fact re-check to dosage and
        quantity for the same reason — dosage's old pre-check-only version
        had the identical gap (and additionally could never actually
        correct a mismatch at all; see DOSAGE_INDICATOR_SELECTORS's
        docstring), even though this exact silent-failure combination
        hasn't independently been observed for quantity, which is new
        here. rendered_quantity is returned on its own, separately from
        quantity_caveat, so the caller can report what quantity a price is
        actually for even when none was explicitly requested — there's no
        caveat to build in that case, but the page is still pricing
        *something*, and the caller has no other way to know what.

        `url` is normally whatever resolve_drug_url() just navigated to
        via the real search flow — this re-navigates to it directly
        rather than reusing that in-progress page state. Slightly
        redundant (one extra page load), but simpler and more robust
        than threading "already there" state between the two methods,
        and this method is also called on its own with a
        previously-known URL in principle, not only right after a fresh
        search."""
        try:
            self.driver.get(url)
        except Exception as e:
            self._save_debug_page("page_singlecare_error.html")
            return [], f"Selenium navigation failed: {e}", "", "", "", "", ""

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_bot_challenge(url):
                self._save_debug_page("page_singlecare_challenge_unresolved.html")
                return (
                    [],
                    (
                        "bot check (CAPTCHA) shown and not resolved — see "
                        "page_singlecare_challenge_unresolved.html, or set "
                        "SINGLECARE_INTERACTIVE_CAPTCHA=false to skip this prompt"
                    ),
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            self._save_debug_page("page_singlecare_after_challenge.html")

        # Reported live: --formulation was accepted throughout this file
        # but never actually used, unlike dosage/quantity. This page's
        # "Select form" listbox was already confirmed live, in an
        # earlier investigation, to be the same custom-listbox widget as
        # dosage/quantity (see FORM_INDICATOR_SELECTORS's docstring) — so
        # this wires formulation selection to it the same way dosage/
        # quantity already work, rather than only flagging a mismatch.
        # Not independently re-confirmed live end-to-end for this
        # specific formulation-selection path — SingleCare was bot-
        # blocking every live attempt to verify it when this was
        # written — but it's the same mechanism, applied identically,
        # not a new guess about page structure.
        # Runs *first*, before dosage/quantity/ZIP: a form change is the
        # most "upstream" of the four (most plausibly resets which
        # dosages/quantities are even available, the same direction of
        # risk already documented below for dosage/quantity vs. ZIP), so
        # it's the one whose own post-check needs to run last among the
        # things it could invalidate — i.e. it needs to happen first.
        rendered_form = _detect_rendered_form(BeautifulSoup(self.driver.page_source, "lxml"))
        formulation_caveat = ""
        if formulation and not _form_matches(rendered_form, formulation):
            try:
                self._try_select_listbox("Select form", formulation)
            except Exception:
                pass
            rendered_form = _detect_rendered_form(BeautifulSoup(self.driver.page_source, "lxml"))
            if not _form_matches(rendered_form, formulation):
                formulation_caveat = (
                    f"could not confirm form {formulation} was selected (page showed '{rendered_form}')"
                )

        # Dosage/quantity *before* ZIP, deliberately — not just
        # incidentally, and not the order this originally shipped in.
        # Reported live as a regression right after the first version:
        # SingleCare came back with the wrong ZIP even though
        # _try_enter_zip() had already succeeded earlier in this method.
        # Root cause, confirmed from a debug dump of the exact run: the
        # old ZIP-then-dosage/quantity order meant a *later* listbox
        # interaction could reset ZIP state that had just been set —
        # re-clicking a listbox to reselect the value it's already
        # showing turned out not to be a no-op here, the same class of
        # "one action invalidates another" issue goodrx_scraper.py's ZIP
        # docstring already describes for a different reason (there, ZIP
        # entry can relaunch the driver and wipe dosage/quantity — here,
        # it's dosage/quantity wiping ZIP instead, so the fix is the same
        # shape in reverse: whichever action can invalidate another must
        # run first, so the *last* action's own post-check is the one
        # that's actually reliable). Also gated on an actual mismatch
        # now, not attempted unconditionally like the old dosage code —
        # nothing to fix if the page already shows the requested value,
        # and *that* alone would already have prevented the regression
        # above in the common case where only ZIP needed changing.
        rendered_dosage = _detect_rendered_dosage(BeautifulSoup(self.driver.page_source, "lxml"))
        dosage_caveat = ""
        if not _dosage_matches(rendered_dosage, dosage):
            try:
                self._try_select_listbox("Select dosage", dosage)
            except Exception:
                pass
            rendered_dosage = _detect_rendered_dosage(BeautifulSoup(self.driver.page_source, "lxml"))
            if not _dosage_matches(rendered_dosage, dosage):
                dosage_caveat = f"could not confirm {dosage} was selected (page showed '{rendered_dosage}')"

        # Detected unconditionally, unlike dosage above — even with no
        # --quantity requested, the page is still pricing *some*
        # quantity, and the caller needs to know what it was to report
        # it. Selection is still gated on an actual mismatch, same as
        # dosage: nothing to select (or risk resetting ZIP for) otherwise.
        rendered_quantity = _detect_rendered_quantity(BeautifulSoup(self.driver.page_source, "lxml"))
        quantity_caveat = ""
        if quantity and not _quantity_matches(rendered_quantity, quantity):
            try:
                self._try_select_listbox("Select quantity", quantity)
            except Exception:
                pass
            rendered_quantity = _detect_rendered_quantity(BeautifulSoup(self.driver.page_source, "lxml"))
            if not _quantity_matches(rendered_quantity, quantity):
                quantity_caveat = f"could not confirm quantity {quantity} was selected (page showed '{rendered_quantity}')"

        zip_caveat = ""
        if zip_code:
            # Runs last, after any dosage/quantity change above that
            # might have reset it — see this block's opening comment.
            try:
                self._try_enter_zip(zip_code)
            except Exception:
                pass
            # Re-check whether it actually took, right after attempting —
            # see this method's docstring for why this can't just be
            # inferred from whether rows come back.
            rendered_zip = _detect_rendered_zip(BeautifulSoup(self.driver.page_source, "lxml"))
            if not _zip_matches(rendered_zip, zip_code):
                zip_caveat = f"could not confirm ZIP {zip_code} was applied (page showed '{rendered_zip}')"

        # Reported live: SingleCare came back with no rows at all right
        # after ZIP/dosage/quantity refinement was added. Confirmed from a
        # debug dump of the exact run: the page wasn't blocked or
        # erroring — it was still showing its loading skeleton
        # (`sc-loader`, `pharmacy-item--loading` placeholders with empty
        # names/prices) at the moment it was read. Root cause: the old
        # wait here only checked once for *any* "$" in the page's body
        # text — a single check, the same "checked before it settled"
        # bug class already fixed elsewhere in this project (Cost Plus
        # Drugs' price, GoodRx's dosage/quantity confirm, this file's own
        # challenge re-check). A real, stale price left over from
        # *before* the ZIP/dosage/quantity change finished re-rendering
        # can satisfy a single check like that, immediately, moments
        # before the page clears it and shows the loading skeleton for
        # the refreshed state — exactly the failure mode already
        # described in _wait_for_stable_price()'s docstring
        # (costplusdrugs_scraper.py) for a different site. Fixed the same
        # way: poll for _extract_pharmacy_rows() itself, not just a bare
        # "$", and require two reads ~0.5s apart to agree before trusting
        # either one.
        rows = []
        deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            soup = BeautifulSoup(self.driver.page_source, "lxml")
            candidate_rows = _extract_pharmacy_rows(soup)
            if candidate_rows:
                time.sleep(0.5)
                soup_again = BeautifulSoup(self.driver.page_source, "lxml")
                rows_again = _extract_pharmacy_rows(soup_again)
                if len(rows_again) == len(candidate_rows):
                    rows = rows_again
                    break
                # Still changing — keep polling rather than trusting the
                # first read.
            time.sleep(0.5)

        if not rows:
            self._save_debug_page("page_singlecare_no_rows.html")
            return (
                [], "Selenium load found no pharmacy price rows",
                zip_caveat, dosage_caveat, quantity_caveat, formulation_caveat, rendered_quantity,
            )
        return rows, "", zip_caveat, dosage_caveat, quantity_caveat, formulation_caveat, rendered_quantity


def get_prices(
    drug_name: str,
    formulation: str,
    dosage: str,
    zip_code: str | None = None,
    quantity: str | None = None,
) -> list[PriceResult]:
    """Never raises — any unexpected failure is returned as an error PriceResult."""
    url = ""
    try:
        with _SingleCareSeleniumFallback(headless=Config.HEADLESS_DEFAULT) as scraper:
            resolved_url, resolve_error = scraper.resolve_drug_url(drug_name)
            if not resolved_url:
                return [
                    PriceResult(
                        drug_name=drug_name, formulation=formulation, dosage=dosage,
                        source=SOURCE_NAME,
                        error=resolve_error or f"could not resolve a SingleCare page for '{drug_name}'",
                    )
                ]
            url = resolved_url
            (
                rows, error, zip_caveat, dosage_caveat, quantity_caveat, formulation_caveat,
                rendered_quantity,
            ) = scraper.refine(url, formulation, dosage, quantity, zip_code)

        caveats = [c for c in (zip_caveat, dosage_caveat, quantity_caveat, formulation_caveat) if c]

        if not rows:
            return [
                PriceResult(
                    drug_name=drug_name,
                    formulation=formulation,
                    dosage=dosage,
                    source=SOURCE_NAME,
                    url=url,
                    error=error or "no pharmacy price rows found",
                )
            ]

        rows_sorted = sorted(rows, key=lambda r: r["price"])[:5]
        caveat_suffix = f" — prices may be inaccurate: {'; '.join(caveats)}" if caveats else ""
        # Whatever quantity the page actually ended up showing prices for —
        # not necessarily what was requested, if a mismatch caveat above
        # already flagged that it couldn't be confirmed. Reported live:
        # this dropdown does change listed prices, so leaving it unreported
        # (as every result did before) meant there was no way to tell which
        # pack size a given price was actually for.
        quantity_label = rendered_quantity or quantity or ""

        results = []
        for row in rows_sorted:
            # The Member Bonus is free (unlike GoodRx's paid Companion
            # membership), so it's not misleading to still sort/select by
            # this price — everyone can actually get it. But per feedback,
            # not everyone wants to sign up for anything at all, so the
            # no-signup price is shown too wherever available, labeled
            # distinctly rather than silently omitted — same "show both,
            # name them honestly" approach used for GoodRx's Companion/
            # standard prices.
            label = "cash/coupon price"
            if row.get("no_bonus_price") is not None:
                label += " (with free Member Bonus signup)"
            results.append(
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, price=row["price"], price_label=label + caveat_suffix,
                    pharmacy=row["pharmacy"], quantity=quantity_label, url=url, raw_text=row["raw"],
                )
            )
            if row.get("no_bonus_price") is not None:
                results.append(
                    PriceResult(
                        drug_name=drug_name, formulation=formulation, dosage=dosage,
                        source=SOURCE_NAME, price=row["no_bonus_price"],
                        price_label="cash/coupon price (standard, no Member Bonus signup)" + caveat_suffix,
                        pharmacy=row["pharmacy"], quantity=quantity_label, url=url, raw_text=row["no_bonus_raw"],
                    )
                )
        return results
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
