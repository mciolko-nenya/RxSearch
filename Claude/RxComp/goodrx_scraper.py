"""
GoodRx scraper.

Confirmed during planning research: a plain HTTP fetch of a GoodRx drug page
returns 403, consistent with GoodRx's known bot detection. So this site
needs a real Selenium browser, not just JS rendering — and even Selenium
gets challenged. Confirmed live: GoodRx serves an interactive PerimeterX
"px-captcha" widget (title "Access to this page has been denied"), not just
a JS-only Cloudflare-style check — no headless session can solve that, since
it needs an actual human looking at it. See _resolve_bot_challenge() below
for how this is handled. This is still the least reliable of the four
sites; treat failures here as expected "best-effort" territory, not a bug.

URL pattern: https://www.goodrx.com/{drug-slug} — dosage/quantity/zip are
chosen via on-page dropdowns/inputs on that single URL rather than being
encoded into separate per-dosage URLs. ZIP specifically is a button that
opens a location modal (see _try_enter_zip()), not a plain input — search()
re-checks the rendered ZIP afterward and flags a caveat if it didn't
actually take, rather than silently returning prices for the wrong location.

Drug name -> slug used to be built directly from drug_name (goodrx_slug())
rather than through GoodRx's own search. Switched to search-driven
resolution (_resolve_drug_url()) for the same reason singlecare_scraper.py
was: confirmed live and reported there, a directly-guessed slug can be
wrong for a drug whose real page uses a salt-form-qualified name (e.g.
SingleCare's "atorvastatin" vs. the real "atorvastatin-calcium") — there's
no algorithmic way to know the right form without asking the site. No
matching failure has actually been confirmed live for GoodRx specifically
(its slugs have matched plain drug names in everything tried so far), but
the same class of risk applies to any site whose URLs encode a specific
canonical drug name, so this closes it here too rather than waiting for a
confirmed failure first.

Not independently verified end-to-end the way SingleCare's equivalent was:
GoodRx's own bot detection blocked every attempt to inspect the real
autocomplete dropdown live while building this (a "Press & Hold" challenge
appeared from just typing into the search box, and a subsequent full-page
block on every later attempt, including plain direct navigation — likely a
session-level PerimeterX flag from the repeated automated-looking
attempts, not something specific to search). _resolve_drug_url() is built
defensively instead: it tries a short list of plausible suggestion
selectors, falls back to just submitting the search box directly if none
match, and either way validates whatever URL results against GoodRx's
*confirmed* real per-drug URL shape (a single path segment, e.g.
"/lisinopril" — extensively confirmed live elsewhere in this file) rather
than trusting the search flow blindly.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from config import Config
from driver_utils import (
    INTERACTIVE_CHALLENGE_LOCK,
    SeleniumScraperBase,
    find_first_present,
    robust_click,
    wait_for_challenge_confirmation,
)
from models import PriceResult
from utils import extract_price_after_label, extract_price_from_text, token_matches

SOURCE_NAME = "GoodRx"

# Confirmed live: the search input on GoodRx's homepage. Not confirmed
# live: the shape of its autocomplete suggestion dropdown — see module
# docstring for why (repeated bot-blocking during investigation). Tried
# in order, most-to-least specific; _resolve_drug_url() falls through to
# submitting the search box directly (Enter) if none of these match.
SEARCH_HOME_URL = "https://www.goodrx.com"
SEARCH_INPUT_SELECTOR = "#hero-drug-search-input"
# Confirmed live: the real, populated autocomplete dropdown lives in
# `#hero-drug-search-results` as `<li data-qa="search-result-N" role="link"
# aria-label="...">` items — not `role="option"`/`role="listbox"`, and not
# marked with `data-testid*=suggestion` either. That old selector list
# (kept below as SEARCH_SUGGESTION_SELECTORS_LEGACY) matched none of that;
# instead `[data-testid*=suggestion]` spuriously matched an unrelated,
# always-present "Popular searches" quick-links panel that's shown the
# moment the input is focused, before any typing. Its text stays stable
# across this method's two-reads-0.3s-apart settle check just as easily as
# a real answer would, so the old code clicked that decoy panel and landed
# wherever its markup happened to put the click point (observed live:
# GoodRx's generic /discount-card page) instead of the drug's own page.
SEARCH_RESULT_SELECTOR = '#hero-drug-search-results li[data-qa^="search-result-"]'
# The dropdown's first entry is frequently a sponsored ad for a *different*
# drug/program (confirmed live: `aria-label="Sponsored ad: Lisinopril is $0
# with Companion - ..."` for a lisinopril search) — must be excluded from
# matching, not just skipped-if-no-better-option, since the real result is
# reliably present alongside it in every request. seen live so far.
_SPONSORED_LABEL_PREFIX = "sponsored"
SEARCH_SUGGESTION_SELECTORS_LEGACY = [
    '[role="option"]',
    '[data-testid*=autocomplete] a',
    'ul[role="listbox"] li',
]

# Path segments confirmed to exist on goodrx.com that are NOT a drug page,
# despite otherwise matching the single-segment shape a real drug page
# has — used by _looks_like_drug_page_url() to avoid mistaking a nav
# link for one when scanning a search-results page.
_NON_DRUG_PATH_SEGMENTS = {
    "", "search", "drugs", "conditions", "care", "companion", "health",
    "pharmacy", "about", "careers", "prices", "coupons", "for-hcps",
    "login", "signup", "account", "cart",
}


def _looks_like_drug_page_url(path: str) -> bool:
    """GoodRx's real per-drug URL is confirmed live (extensively, across
    this whole file) to be exactly goodrx.com/{slug} — a single path
    segment, no further slashes. Used both to sanity-check wherever
    _resolve_drug_url()'s search flow ends up, and to filter candidate
    links on what might be a search-results listing page, without
    needing to know that page's exact markup (not confirmed live — see
    module docstring)."""
    path = path.strip("/")
    if not path or "/" in path:
        return False
    if path.lower() in _NON_DRUG_PATH_SEGMENTS:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", path.lower()))

# Confirmed live in the page title/HTML when GoodRx blocks a lookup instead
# of serving the drug page. Kept broad (PerimeterX AND Cloudflare-style
# markers) since which one shows up isn't guaranteed to stay consistent.
# "press & hold" / "confirm you are a human" are PerimeterX's *other*
# challenge presentation — confirmed live, triggered specifically by
# submitting a ZIP change (see _try_enter_zip()), distinct from the
# px-captcha widget shown on initial page load.
BOT_CHALLENGE_MARKERS = (
    "px-captcha",
    "Access to this page has been denied",
    "Just a moment",
    "captcha-delivery.com",
    "cf-challenge",
    "press & hold",
    "confirm you are a human",
)

# Tried in order; first selector that yields >=1 match wins.
# "button:has([data-qa=seller-name])" is confirmed live real markup — each
# pharmacy row is a <button> containing [data-qa=seller-name] (plain text,
# e.g. "Walgreens"), [data-qa=seller-price], and optionally
# [data-qa=special-offer-text] (e.g. "with Companion" — a paid-membership
# price, not the no-strings-attached one; see _extract_price_rows()). The
# rest are speculative fallbacks in case the site changes and this one
# stops matching.
PRICE_ROW_SELECTORS = [
    "button:has([data-qa=seller-name])",
    "[data-testid*=pharmacy]",
    "[data-testid*=price-row]",
    "[class*=PharmacyCard]",
    "[class*=priceRow]",
    "tr",
]

# Confirmed live: neither of these ever matched anything, on any run —
# there is no <select> anywhere on the page itself. Dosage/quantity are
# real native <select> elements (#configuration-editor-dropdown-dosage,
# #configuration-editor-dropdown-quantity — both confirmed live, with
# stable ids and aria-labels "Dosage"/"Quantity"), but they only exist
# inside the "Edit prescription" modal, opened via a button matched by
# text (there's no data-qa/aria-label on it) reading "Medication{drug}
# {dosage} ({quantity} tablets)Edit". See _try_edit_prescription() below,
# which replaces this file's entire previous dosage/quantity-selection
# approach — not just its selectors.
#
# Reported live: the modal shown was open and watched directly, yet
# dosage/quantity never changed — and confirmed live, repeatedly, by
# opening this same modal fresh several times in a row: the *entire*
# modal has (at least) two DOM variants GoodRx serves seemingly at
# random, not just the "Edit" button that _find_prescription_edit_button()
# already handles two variants of. The variant this file was built
# against ("Variant A" below) has a single "Confirm prescription" button
# and the ids above. A second, real variant ("Variant B") — confirmed
# live in roughly 2 of 3 fresh sessions in one run, so not a rare edge
# case — instead has separate `#dosage`/`#quantity`/`#form` <select>s
# (`#form` confirmed live to always exist but be genuinely disabled
# with a single "tablet" option for a tablet-only drug like
# atorvastatin — GoodRx already fixes the form itself here, this isn't
# a missed feature) and "Cancel"/"Update" buttons instead of one
# "Confirm prescription" button — matched by their own stable
# `data-qa` values, confirmed live
# ("prescription-editor-modal-cancel-button"/
# "prescription-editor-cta-button"). Both variants' dosage/quantity
# selects share the same aria-labels/option text shape, just different
# ids, so every selector below is now a *list*, tried in order — the
# same "confirmed selector(s), most-to-least specific" resilience
# pattern already used for PRICE_ROW_SELECTORS/SEARCH_RESULT_SELECTOR
# elsewhere in this file, just applied to a dosage/quantity lookup for
# the first time.
DOSAGE_SELECT_SELECTORS = ["#configuration-editor-dropdown-dosage", "#dosage"]
QUANTITY_SELECT_SELECTORS = ["#configuration-editor-dropdown-quantity", "#quantity"]
CONFIRM_BUTTON_TESTID = "prescription-editor-cta-button"  # Variant B only; Variant A has no stable data-qa, text-matched below instead

# Reported live: several rows clicked through in the same run never
# updated the Standard GoodRx Price panel at all, each costing the full
# SELENIUM_WAIT_TIMEOUT (15s default) before giving up — the window
# sitting idle for well over a minute on a single lookup. Deliberately
# shorter, dedicated to this one repeated operation rather than reusing
# the general element-wait timeout.
STANDARD_PRICE_WAIT_TIMEOUT = 6

# An earlier version capped how many Companion-priced rows got clicked
# through for a standard price, to the cheapest 3 (COMPANION_ROWS_LIMIT
# * STANDARD_PRICE_WAIT_TIMEOUT bounding the worst case regardless of how
# many the page had). Reported live: this silently skipped the
# click-through for CVS Pharmacy — confirmed from a debug dump of that
# exact run: Walgreens, Hy-Vee, and Walgreens Specialty Pharmacy were all
# tied at $0.00, so the "cheapest 3" cap took exactly those three and
# never attempted Target (CVS) or CVS Pharmacy at all (both $9.00) — not
# a failed click-through, no attempt was made. Removed: every Companion
# row now gets a click-through attempt. STANDARD_PRICE_WAIT_TIMEOUT still
# bounds the cost *per row*; with it this low, a realistic full pharmacy
# list (GoodRx pages seen so far top out around 9) costs under a minute
# even if every single row were a Companion row and every one timed out.

# Confirmed live: there's no plain zip <input> on the page at all — location
# is a button showing the current ZIP (aria-label="Set your location,
# Current location is 55419"), which opens a modal on click. Inside it: a
# plain text field (no autocomplete dropdown, unlike SingleCare's
# equivalent) and an explicit "Set location" submit button (see
# _try_enter_zip() — Enter alone may not submit).
#
# The modal input's *accessible name* reads "Enter a city or ZIP code" (via
# an associated <label>, confirmed live) — but its own attributes are
# empty: `placeholder=""`, no aria-label at all. An earlier version of
# ZIP_INPUT_SELECTOR matched against placeholder text that was never
# actually there, so the WebDriverWait below silently timed out on every
# attempt and nothing ever got typed, even though the modal opened
# correctly. Confirmed real selector: id="locationModalAddress".
ZIP_LOCATION_BUTTON_SELECTORS = ['[aria-label*="Set your location"]', "[data-qa=location-wrapper] button"]
ZIP_INPUT_SELECTOR = "#locationModalAddress"


def _detect_rendered_zip(driver) -> str:
    """Reads the live location button's aria-label directly (e.g. "Set
    your location, Current location is 55419") rather than
    driver.page_source — confirmed elsewhere in this project
    (costplusdrugs_scraper.py) that page_source can lag behind what's
    actually rendered; the same class of issue turned out to apply here
    too (see _wait_for_rendered_zip())."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, '[aria-label*="Set your location"]')
        label = el.get_attribute("aria-label") or ""
    except Exception:
        return ""
    m = re.search(r"Current location is (\d{5})", label)
    return m.group(1) if m else ""


def _wait_for_rendered_zip(driver, requested_zip: str | None, timeout: float) -> str:
    """Poll _detect_rendered_zip() rather than checking once immediately
    after _try_enter_zip() returns. Confirmed live: a single check right
    after submitting a ZIP (and solving the Press & Hold, if it appeared)
    can still see the *old* location — the button's label evidently
    doesn't always finish updating within that immediate instant. Same
    "checked before it settled" issue already fixed elsewhere
    (costplusdrugs_scraper.py's price wait, singlecare_scraper.py's
    challenge re-check). Returns whatever was last read, whether or not
    it ends up matching — the caller decides what that means."""
    deadline = time.monotonic() + timeout
    rendered = _detect_rendered_zip(driver)
    while time.monotonic() < deadline:
        if _zip_matches(rendered, requested_zip):
            return rendered
        time.sleep(0.5)
        rendered = _detect_rendered_zip(driver)
    return rendered


def _zip_matches(rendered: str, requested: str | None) -> bool:
    if not requested or not rendered:
        # No --zip requested, or couldn't read the page's current one —
        # nothing to enforce (same "can't tell, assume matching" rule used
        # for dosage/zip matching elsewhere in this project).
        return True
    return rendered.strip() == requested.strip()


def _loose_matches(rendered: str, requested: str | None) -> bool:
    """Shared match used for both dosage and quantity — tolerates
    "20mg" vs "20 mg", "30" vs "30 tablets", etc. without needing an
    exact match. Same rule as _zip_matches: nothing requested, or
    nothing readable, means there's nothing to enforce.

    Reviewed live: this used to do its own normalize-and-substring
    check (`norm(requested) in norm(rendered) or ...`), which is
    unanchored — e.g. levothyroxine's real strengths include both
    25mcg and 125mcg, and normalized "25mcg" is a literal substring of
    normalized "125mcg". If GoodRx defaulted to showing 125mcg and the
    user requested 25mcg, this returned True (a false "already
    matches"), so search() never even attempted the correction and
    never produced a dosage_caveat — the 125mcg price would be reported
    as if it were the confirmed 25mcg one. Fixed by delegating to
    utils.token_matches(), which requires the two sides' leading
    numbers to be numerically equal instead of merely one containing
    the other, while still tolerating a differing trailing unit word —
    see that function's docstring for the full reasoning, shared with
    the identical fix in singlecare_scraper.py's own _loose_matches()
    and costplusdrugs_scraper.py's _find_and_click_variant_button()."""
    if not requested or not rendered:
        return True
    return token_matches(requested, rendered)


def _parse_medication_summary(text: str) -> tuple[str, str, str]:
    """Parses the "Edit" button's visible text — confirmed live, reads
    like "MedicationLisinopril 20mg (30 tablets)Edit" — into (dosage,
    quantity, form), e.g. ("20mg", "30", "tablets"). Returns ("", "", "")
    if the text doesn't match this shape (page structure changed, or
    nothing found).

    Reported live: GoodRx accepted a --formulation value but never
    verified it against anything on the page, unlike dosage/quantity —
    silently echoing the requested formulation back as if confirmed even
    when the page actually carried a different form. The dosage/
    quantity capture here was already confirmed live for the tablet
    case; this widens the previously tablet-only, hardcoded `tablets?`
    unit match to capture whatever word actually follows the count
    instead, so search() below can compare it against the requested
    formulation the same way it already compares dosage/quantity — a
    strict generalization of an already-confirmed regex, not a new
    selector. Not independently confirmed live for any non-tablet drug
    (none has been tested against this page), but it changes nothing
    about the already-confirmed tablet case: any drug whose summary
    still reads "(N tablets)" parses exactly as before."""
    match = re.search(r"([\d.]+\s*mg)\s*\((\d+)\s*([a-z]+)\)", text, re.IGNORECASE)
    if not match:
        return "", "", ""
    return match.group(1).replace(" ", ""), match.group(2), match.group(3)


def _wait_for_updated_medication_summary(
    driver, requested_dosage: str | None, requested_quantity: str | None, timeout: float
) -> tuple[str, str]:
    """Poll the prescription-edit button's own summary text rather than
    checking it exactly once immediately after _try_edit_prescription()
    returns — the same "checked before it settled" issue already fixed
    for ZIP (_wait_for_rendered_zip()) and price rows
    (_wait_for_stable_price_rows()) elsewhere in this file.

    Reported live: a user who genuinely solved the inline Press & Hold
    challenge that confirming a dosage/quantity change can trigger still
    got a "could not confirm ... was selected" caveat. Root cause
    unconfirmed — no debug artifact existed for that specific run, since
    nothing on that path saved one before now (see
    _try_edit_prescription()'s challenge branch, which does now) — but
    the single-immediate-read pattern this replaces is the same shape
    that's already caused several *confirmed* bugs elsewhere in this
    project, so this closes it defensively either way: keeps re-reading
    until the summary matches both requested values (loosely, same
    tolerance as _loose_matches() everywhere else) or the timeout runs
    out, rather than trusting whatever it happened to say the instant
    this was called. Returns whatever was last read either way — the
    caller decides what that means, same contract as
    _wait_for_rendered_zip()."""
    deadline = time.monotonic() + timeout
    current_dosage, current_quantity = "", ""
    while time.monotonic() < deadline:
        try:
            button = _find_prescription_edit_button(driver)
            current_dosage, current_quantity, _current_form = _parse_medication_summary(
                button.text.strip() if button else ""
            )
        except Exception:
            pass
        dosage_ok = not requested_dosage or _loose_matches(current_dosage, requested_dosage)
        quantity_ok = not requested_quantity or _loose_matches(current_quantity, requested_quantity)
        if dosage_ok and quantity_ok:
            break
        time.sleep(0.5)
    return current_dosage, current_quantity


def _wait_for_visible_element(driver, selector: str, timeout: float):
    """Reported live: search resolution failed with "element not
    interactable" trying to type into GoodRx's own confirmed search
    input selector. Confirmed live, and a genuinely different bug from
    every other "checked before it settled" race fixed elsewhere in this
    project: `#hero-drug-search-input` isn't unique on the page — two
    elements share that id (invalid HTML, but real; likely one markup
    variant per responsive breakpoint, both left in the DOM). One is a
    zero-size, `is_displayed() == False` element; the other is the real,
    visible 1037×25 input. `driver.find_element()` (and
    `EC.presence_of_element_located`, which uses it internally) always
    returns the *first* DOM match regardless of visibility — which
    happened to be the hidden one, every time, not intermittently. No
    amount of waiting longer fixes a wrong pick, since it's deterministic
    — this instead searches all matches and returns the first one that's
    actually `is_displayed()`, polling in case visibility itself is still
    settling. Returns None if none become visible within `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed():
                    return el
        except Exception:
            pass
        time.sleep(0.3)
    return None


def _pick_search_result(driver, drug_name: str, timeout: float = 3.0):
    """Waits for GoodRx's real autocomplete dropdown
    (`SEARCH_RESULT_SELECTOR`) to settle, then returns the `<li>` best
    matching `drug_name` — or None if nothing usable showed up.

    Settling is checked the same way _resolve_drug_url() previously
    checked a single candidate's text (two reads ~0.3s apart must agree),
    but applied to the whole result list's aria-labels, since results are
    swapped in as a batch per keystroke/debounce rather than growing one
    at a time.

    Confirmed live: the first result is frequently a sponsored ad for an
    unrelated program (aria-label starting with "Sponsored ad: ...") —
    always excluded, never used as a fallback, since a real match is
    reliably present alongside it. Among the rest, prefers one whose
    aria-label loosely matches drug_name (same normalize-and-substring
    approach already used a few lines down in _resolve_drug_url() for
    matching search-results-listing links); falls back to the first
    non-sponsored result only when there's exactly one, so an ambiguous
    multi-result page doesn't silently guess the wrong drug."""
    target_norm = re.sub(r"[^a-z0-9]", "", drug_name.lower())
    deadline = time.monotonic() + timeout
    last_labels = None
    items: list = []
    while time.monotonic() < deadline:
        items = driver.find_elements(By.CSS_SELECTOR, SEARCH_RESULT_SELECTOR)
        labels = tuple((it.get_attribute("aria-label") or "") for it in items)
        if labels and labels == last_labels:
            break
        last_labels = labels
        time.sleep(0.3)
    if not items:
        return None

    candidates = [
        it for it in items
        if not (it.get_attribute("aria-label") or "").strip().lower().startswith(_SPONSORED_LABEL_PREFIX)
    ]
    if not candidates:
        return None

    for it in candidates:
        label_norm = re.sub(r"[^a-z0-9]", "", (it.get_attribute("aria-label") or "").lower())
        if target_norm and (target_norm in label_norm or label_norm in target_norm):
            return it
    return candidates[0] if len(candidates) == 1 else None


def _find_confirm_button(driver):
    """Finds whichever of the two confirmed "Edit prescription" modal
    variants' confirm button is actually present — Variant B's own
    stable `data-qa` first, falling back to Variant A's text match
    (which has no data-qa of its own) — see
    DOSAGE_SELECT_SELECTORS/CONFIRM_BUTTON_TESTID's shared docstring
    above for how both variants were confirmed live."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, f'[data-qa="{CONFIRM_BUTTON_TESTID}"]')
        if el:
            return el
    except Exception:
        pass
    try:
        return next(
            (
                b for b in driver.find_elements(By.TAG_NAME, "button")
                if b.text.strip() == "Confirm prescription"
            ),
            None,
        )
    except Exception:
        return None


def _find_prescription_edit_button(driver):
    """Finds the button that opens the "Edit prescription" modal and
    shows the current dosage/quantity summary (e.g. "Lisinopril 20mg (30
    tablets)"). Reported live: results stopped showing a quantity at
    all — confirmed from a fresh debug dump that GoodRx's DOM for this
    button had changed since it was first verified live, just hours
    earlier in the same investigation, most likely an A/B test bucket
    rather than a one-time site change (nothing else about the page
    looked different). Two shapes seen so far:

      1. Confirmed live just now: `[data-qa="rx-editor-button"]` — a
         stable attribute. The summary text lives in its own <span>
         inside the button; "Edit" is represented only by an SVG icon
         (`data-qa="icon-goodrx-edit-filled"`), no text at all — which is
         exactly why the text-based match below stopped finding anything
         on this variant, returning "" silently rather than erroring.
      2. Confirmed live earlier the same session: no `data-qa` on the
         button at all, only matchable by its visible text, which does
         include a literal "Edit": "Medication{drug} {dosage} ({quantity}
         tablets)Edit".

    Tries the confirmed selector first, falls back to the text match for
    the other variant, returns None if neither matches — same
    resilience pattern as this file's selector lists elsewhere
    (PRICE_ROW_SELECTORS, etc.), just expressed as a function since the
    two candidates need different matching logic (CSS selector vs. text
    content), not just different CSS selectors."""
    try:
        return driver.find_element(By.CSS_SELECTOR, '[data-qa="rx-editor-button"]')
    except Exception:
        pass
    try:
        return next(
            (
                b for b in driver.find_elements(By.TAG_NAME, "button")
                if "Medication" in b.text and b.text.strip().endswith("Edit")
            ),
            None,
        )
    except Exception:
        return None


def _wait_for_stable_price_rows(driver, timeout: float) -> None:
    """Polls for the pharmacy row count to stabilize — two reads ~0.5s
    apart returning the same non-zero count — rather than just checking
    that at least one row is present.

    Reported live: "very few" pharmacy options came back, with no error
    at all — confirmed by inspecting a debug dump that GoodRx's real
    default list has 9 rows, un-truncated, right after page load. A
    presence-only wait (what a previous version of
    _try_edit_prescription() used, added when fixing a *different* bug —
    zero rows on a still-loading page) can catch this list right after
    the first row or two has streamed in but before the rest have,
    since both a ZIP change and a confirmed dosage/quantity change
    replace the whole list asynchronously. That satisfies a
    presence-only check immediately, and extraction right afterward then
    silently gets a much shorter list than the page was about to show —
    not an error, so nothing else here would have caught it. Best-effort:
    if the count never stabilizes within the timeout, this just returns
    whatever was last seen; _extract_price_rows() re-reads the page
    independently afterward regardless, and will report its own "no
    rows" error if even that comes back empty."""
    deadline = time.monotonic() + timeout
    last_count = -1
    while time.monotonic() < deadline:
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, PRICE_ROW_SELECTORS[0]))
        except Exception:
            count = 0
        if count > 0 and count == last_count:
            return
        last_count = count
        time.sleep(0.5)


def _looks_like_bot_challenge(driver) -> bool:
    try:
        haystack = (driver.title or "") + " " + driver.page_source
    except Exception:
        return False  # can't tell — treat as not-a-challenge, let normal extraction report "no rows"
    haystack_lower = haystack.lower()
    return any(marker.lower() in haystack_lower for marker in BOT_CHALLENGE_MARKERS)


class GoodRxScraper(SeleniumScraperBase):
    def _resolve_bot_challenge(self, url: str) -> bool:
        """GoodRx sometimes serves an interactive CAPTCHA instead of the
        drug page — a headless session can't solve that, since nothing is
        looking at it. Switch to (or reuse, if already visible) a real
        Chrome window and let the person running this tool solve it
        directly, same interactive pattern as amazon_scraper.py's
        --setup-amazon. Returns True once the page looks clear, False if
        the user skips it (blank Enter) or it's still blocked afterward.

        Guarded by INTERACTIVE_CHALLENGE_LOCK: main.py runs sites
        concurrently by default, and SingleCare can hit this same situation
        (see singlecare_scraper.py) — the lock makes sure only one
        challenge is ever presented to the user at a time, rather than two
        threads racing to print/input() simultaneously."""
        if not Config.GOODRX_INTERACTIVE_CAPTCHA:
            return False

        with INTERACTIVE_CHALLENGE_LOCK:
            print("\nGoodRx is showing a bot-check (CAPTCHA) page for this lookup.")
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
            # Confirmed live on SingleCare's equivalent check (see
            # singlecare_scraper.py): even with a valid solved-challenge
            # cookie already set, a reload can briefly re-show an
            # interstitial/verifying state before redirecting to the real
            # page a moment later. Checking once, immediately, risks
            # catching that transient state. Not independently confirmed
            # for GoodRx specifically, but it's the same reload-then-check
            # shape, so applying the same fix defensively rather than
            # waiting to hit it live.
            #
            # Confirmed live separately: absence of a bot-challenge marker
            # isn't the same as "back on the page we asked for". Right
            # after solving GoodRx's CAPTCHA, the reload above landed on
            # goodrx.com/discount-card — GoodRx's generic "sign up for a
            # free savings card" promo page — instead of the actual
            # homepage/drug page requested, with no CAPTCHA markers on it
            # at all (it's a real, ordinary page). That's a softer
            # anti-bot response than the CAPTCHA itself: rather than
            # re-block outright, redirect anywhere but the real content.
            # A plain "no more CAPTCHA" check can't tell that apart from
            # success, so this also checks the URL actually landed on
            # matches the one requested (by path, ignoring query/hash —
            # small canonicalization differences aren't the concern here).
            # One retry reload clears the one case observed live; if a
            # second landing still doesn't match, this reports still-
            # blocked rather than silently proceeding on the wrong page.
            target_path = urlsplit(url).path.rstrip("/")
            deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
            retried_redirect = False
            while time.monotonic() < deadline:
                if not _looks_like_bot_challenge(self.driver):
                    current_path = urlsplit(self.driver.current_url).path.rstrip("/")
                    if current_path == target_path:
                        return True
                    if retried_redirect:
                        return False
                    retried_redirect = True
                    try:
                        self.driver.get(url)
                    except Exception:
                        pass
                    continue
                time.sleep(0.5)
            return False

    def _read_medication_summary(self) -> str:
        """Reads the prescription-edit button's visible text without
        opening the modal — e.g. "Lisinopril 20mg (30 tablets)", possibly
        with a "Prescription" label alongside it depending on which DOM
        variant is showing (see _find_prescription_edit_button()). Used
        both to report the actual dosage/quantity a price is for, and to
        decide *whether* an edit is even needed before opening the
        modal — see _try_edit_prescription()'s docstring for why that
        matters."""
        try:
            button = _find_prescription_edit_button(self.driver)
            return button.text.strip() if button else ""
        except Exception:
            return ""

    def _click_through_challenge(self, find_fn, description: str):
        """Reported live: a user who solved the *initial* bot-check
        prompt for this lookup still ended up with dosage/quantity not
        applied, no disclaimer about why beyond "could not confirm ...
        was selected". Root cause, confirmed live by directly reproducing
        it: clicking the "Edit" button inside this very method can
        itself trigger GoodRx's PerimeterX challenge — a full-page
        overlay `<iframe id="px-captcha-modal" ...>` that appears
        reactively, mid-interaction, not just on page load. Confirmed via
        the actual exception it raises: "element click intercepted ...
        Other element would receive the click: <iframe
        id='px-captcha-modal' ...>". Nothing before this fix ever checked
        for or surfaced *this* occurrence of the challenge — the only
        challenge checks in this method are for the initial page-load
        state (before) and after clicking "Confirm prescription" (after)
        — so a challenge triggered specifically by the Edit click itself
        fell through a gap between those two checks, silently failing the
        click via the bare `except Exception: return False` around it,
        with no prompt at all. That's exactly consistent with a user
        having solved *a* challenge for this lookup (the earlier one)
        while never being shown *this* one.

        Retries the click once through `_resolve_inline_bot_challenge()`
        — deliberately not `_resolve_bot_challenge()`, for the same
        reason `_resolve_inline_bot_challenge()`'s own docstring gives:
        this is an overlay on an in-progress interaction, not a
        navigation, and reloading would just as destructively discard
        that interaction here as it would there. Raises whatever
        exception the click ultimately failed with — including the
        original one if this isn't actually that challenge, or if
        resolving it didn't stick — so callers keep their existing
        try/except-and-return-False handling with no other changes
        needed."""
        element = find_fn()
        if element is None:
            raise RuntimeError(f"{description} not found")
        try:
            element.click()
            return element
        except Exception:
            if not _looks_like_bot_challenge(self.driver):
                raise
            if not self._resolve_inline_bot_challenge():
                raise
            element = find_fn()
            if element is None:
                raise RuntimeError(f"{description} not found after resolving challenge")
            element.click()
            return element

    def _try_edit_prescription(self, dosage: str | None, quantity: str | None) -> bool:
        """Opens the "Edit prescription" modal and sets whichever of
        dosage/quantity was passed (None means "leave this one alone")
        via its real <select> — DOSAGE_SELECT_SELECTORS/
        QUANTITY_SELECT_SELECTORS, confirmed live; see that constant's
        docstring for why the previous version's selectors never matched
        anything, and for why each is now a list rather than one
        selector. Then clicks whichever variant's confirm button is
        present (_find_confirm_button()).

        Confirmed live: confirming a change here reliably triggers the
        same "Press & Hold" PerimeterX challenge as submitting a ZIP
        change (see _try_enter_zip()) — not observed intermittently, the
        one attempt made here hit it too. So this switches to a visible
        window first, exactly like _try_enter_zip(), if not already
        visible. Only called when search() has already determined an
        edit is actually needed (the requested dosage/quantity doesn't
        already match the page's default) — unconditionally opening this
        modal on every lookup, the way the old (non-functional) dosage
        selection ran unconditionally, would mean triggering that
        challenge far more often than necessary, since GoodRx's default
        dosage frequently won't match what was requested.

        Same lock-scoping fix as _try_enter_zip() — see
        INTERACTIVE_CHALLENGE_LOCK's docstring (driver_utils.py) for the
        "second challenge window opened while a first was still
        unresolved" bug this addresses."""
        with INTERACTIVE_CHALLENGE_LOCK:
            if self.headless and Config.GOODRX_INTERACTIVE_CAPTCHA:
                current_url = self.driver.current_url
                self.close()
                self.headless = False
                self._create_driver()
                try:
                    self.driver.get(current_url)
                except Exception:
                    return False
                if _looks_like_bot_challenge(self.driver):
                    if not self._resolve_bot_challenge(current_url):
                        return False

        try:
            self._click_through_challenge(
                lambda: _find_prescription_edit_button(self.driver), "prescription-edit button"
            )
        except Exception:
            self._save_debug_page("page_goodrx_edit_click_intercepted.html")
            return False

        # Confirmed live: what looked at first like a plain rendering/
        # timing gap (a debug snapshot with zero dosage/quantity <select>
        # elements, no challenge markers) turned out, on further live
        # reproduction, to actually be this — see
        # DOSAGE_SELECT_SELECTORS' shared docstring above: opening this
        # same modal fresh, repeatedly, landed on Variant B's different
        # ids in roughly 2 of 3 attempts, and Variant A's own selectors
        # (which this timeout-then-retry check originally probed with)
        # will *never* appear on a Variant B page no matter how long or
        # how many times this retries. Kept the retry (a genuine
        # rendering lag is still possible independently of which variant
        # loaded), but the real fix is trying every known variant's
        # selector rather than just one.
        needed_selectors = DOSAGE_SELECT_SELECTORS if dosage else QUANTITY_SELECT_SELECTORS
        deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
        modal_open = False
        while time.monotonic() < deadline:
            if find_first_present(self.driver, needed_selectors) is not None:
                modal_open = True
                break
            time.sleep(0.3)
        if not modal_open:
            try:
                self._click_through_challenge(
                    lambda: _find_prescription_edit_button(self.driver),
                    "prescription-edit button (retry)",
                )
                deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
                while time.monotonic() < deadline:
                    if find_first_present(self.driver, needed_selectors) is not None:
                        modal_open = True
                        break
                    time.sleep(0.3)
            except Exception:
                pass
            if not modal_open:
                self._save_debug_page("page_goodrx_edit_modal_not_open.html")
                return False

        try:
            for value, selectors in (
                (dosage, DOSAGE_SELECT_SELECTORS),
                (quantity, QUANTITY_SELECT_SELECTORS),
            ):
                if not value:
                    continue
                select_el = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                    lambda d, sels=selectors: find_first_present(d, sels)
                )
                select = Select(select_el)
                target = value.lower().replace(" ", "")
                matched_option = next(
                    (o for o in select.options if target in o.text.lower().replace(" ", "")), None
                )
                if matched_option is None:
                    # Reported live: the modal was open long enough to
                    # watch, and the dropdowns never changed from the
                    # page's default at all. No debug artifact existed
                    # for that run to confirm why — this whole block had
                    # no failure-path save at all, the same gap already
                    # closed for GoodRx's search and Cost Plus Drugs'
                    # search elsewhere in this project. Closed here too,
                    # and reporting specifically which value had no
                    # matching option rather than one generic
                    # "return False" that could mean several different
                    # things.
                    self._save_debug_page("page_goodrx_no_matching_option.html")
                    return False
                select.select_by_visible_text(matched_option.text)
                # Confirmed pattern for this class of symptom (not
                # independently confirmed as *this* run's specific cause
                # — no debug artifact existed to inspect): WebDriver's
                # native option-click can update a <select>'s own
                # value/selectedIndex (which is why Select.
                # first_selected_option can read back "correct" right
                # after) without the browser firing a real change/input
                # event in every case — and a React-controlled component
                # (which is what actually drives this custom-styled
                # dropdown's *visible* label, confirmed live elsewhere in
                # this file to often report is_displayed() == False on
                # the underlying native <select> itself, i.e. it's not
                # the thing actually rendered on screen) only updates
                # that visible label in response to that event. Dispatch
                # both explicitly, rather than trusting the native click
                # alone — harmless if it already fired correctly, and the
                # fix if it didn't.
                self.driver.execute_script(
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                    select_el,
                )
        except Exception:
            self._save_debug_page("page_goodrx_select_failed.html")
            return False

        try:
            self._click_through_challenge(
                lambda: _find_confirm_button(self.driver), "confirm/update prescription button"
            )
        except Exception:
            self._save_debug_page("page_goodrx_confirm_click_intercepted.html")
            return False

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_inline_bot_challenge():
                self._save_debug_page("page_goodrx_edit_challenge_unresolved.html")
                return False
            # This handles the challenge appearing *after* the Confirm
            # click already went through — a distinct moment from the one
            # _click_through_challenge() above now confirms it can also
            # appear at (during the click itself, intercepting it
            # outright). Still not independently confirmed for this exact
            # later moment specifically, but the same PerimeterX
            # interstitial could plausibly intercept a request that's
            # already in flight the same way it intercepted the click
            # here — defends against that by re-clicking Confirm if it's
            # still present, a no-op if the first click's request already
            # went through and the modal closed, since then this simply
            # won't find the button.
            retry_confirm = _find_confirm_button(self.driver)
            if retry_confirm:
                try:
                    retry_confirm.click()
                except Exception:
                    pass

        # Confirming a change here re-renders the whole pharmacy list
        # asynchronously — see _wait_for_stable_price_rows()'s docstring
        # for the two distinct bugs a too-simple wait here has already
        # caused: zero rows (a presence check run before *any* row had
        # streamed in) and, separately, far fewer rows than the page
        # actually has (a presence check satisfied by the first row or
        # two, before the rest arrived).
        _wait_for_stable_price_rows(self.driver, Config.SELENIUM_WAIT_TIMEOUT)
        return True

    def _resolve_inline_bot_challenge(self) -> bool:
        """Handles the Press & Hold challenge from _try_enter_zip() below —
        deliberately NOT reusing _resolve_bot_challenge(), even though it
        looks like the same situation. That method is built for a full-page
        challenge reached by navigating to a URL: solving it, it
        close()s/recreates the driver (if headless) and reloads that URL.
        Confirmed live, that's actively wrong here: this challenge is an
        *overlay* on top of the in-progress ZIP submission, not a
        navigation. close()-ing the driver destroys that overlay (and the
        whole in-progress action) before a human ever sees it — the fresh
        reload afterward lands on a normal, unchallenged page with the
        ZIP back to default, and _resolve_bot_challenge() reports that as
        "resolved" even though nothing was actually solved. This method
        never closes the driver or navigates anywhere; it only prompts in
        place and re-checks. If the session is headless, there is nothing
        that can be shown to a human without doing that same destructive
        thing, so this honestly reports failure instead."""
        if not Config.GOODRX_INTERACTIVE_CAPTCHA:
            return False
        if self.headless:
            return False

        with INTERACTIVE_CHALLENGE_LOCK:
            print("\nGoodRx is showing a bot-check (Press & Hold) after that action.")
            wait_for_challenge_confirmation(
                "Solve it in the Chrome window, then press Enter here to continue "
                "(or just press Enter to skip)... "
            )
            return not _looks_like_bot_challenge(self.driver)

    def _try_enter_zip(self, zip_code: str) -> bool:
        """Confirmed live end-to-end via direct browser interaction
        (outside the CAPTCHA-gated Selenium path, to see the real flow
        without guessing): clicking the location button
        (ZIP_LOCATION_BUTTON_SELECTORS) opens a "Set your location" modal
        with a plain `input[placeholder="Enter a city or ZIP code"]` (no
        autocomplete dropdown) and an explicit "Set location" submit
        button. An earlier version of this method never clicked that
        submit button at all — Enter alone may not confirm it.

        Also confirmed live, reliably (every attempt tried, not just
        occasionally): submitting a ZIP triggers a *separate* PerimeterX
        "Press & Hold to confirm you are a human" challenge — distinct
        from _resolve_bot_challenge()'s page-load one. Since it's
        essentially guaranteed to show up, this switches to a visible
        window *before* attempting anything below if the session is
        currently headless — discovering that need reactively, after an
        already-doomed headless attempt, would mean a human never gets a
        real chance to solve it (see _resolve_inline_bot_challenge()'s
        docstring for exactly how that failed before this fix). Gated on
        GOODRX_INTERACTIVE_CAPTCHA like every other interactive step here —
        with it disabled (unattended runs), skip straight to the plain
        headless attempt instead of forcing a visible window open for
        no one to use.

        Reported live: a second (GoodRx) challenge window opened, showing
        its own captcha, while a first (SingleCare) one was still
        unresolved — see INTERACTIVE_CHALLENGE_LOCK's docstring
        (driver_utils.py) for the root cause. The whole switch-to-visible
        and reload sequence below is now inside that lock, not just the
        nested _resolve_bot_challenge() call — so if another site is
        mid-challenge, this blocks *before* opening a window at all,
        rather than opening one (and potentially rendering a second,
        unrelated challenge on screen) while queued behind the lock for
        only the terminal prompt.

        Reported live again: after solving that Press & Hold challenge,
        the browser had silently drifted to goodrx.com/discount-card —
        GoodRx's generic "sign up for a free savings card" page — same
        symptom as the full-page-CAPTCHA soft-redirect confirmed and
        fixed in _resolve_bot_challenge(), but through a path that fix
        doesn't cover: _resolve_inline_bot_challenge() only checks that
        no challenge marker remains, exactly as _resolve_bot_challenge()
        used to, and this method returns that result directly with
        nothing re-checking *where* it left us. Fixed the same way: the
        page's path is captured before any of this starts, and checked
        again once the challenge appears resolved — a mismatch counts as
        still-blocked, not success, same reasoning as before."""
        page_path_before = urlsplit(self.driver.current_url).path.rstrip("/")
        with INTERACTIVE_CHALLENGE_LOCK:
            if self.headless and Config.GOODRX_INTERACTIVE_CAPTCHA:
                current_url = self.driver.current_url
                self.close()
                self.headless = False
                self._create_driver()
                try:
                    self.driver.get(current_url)
                except Exception:
                    return False
                if _looks_like_bot_challenge(self.driver):
                    if not self._resolve_bot_challenge(current_url):
                        return False

        try:
            button = None
            for selector in ZIP_LOCATION_BUTTON_SELECTORS:
                try:
                    button = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                    break
                except Exception:
                    continue
            if not button:
                return False
            button.click()
        except Exception:
            return False

        try:
            zip_input = WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ZIP_INPUT_SELECTOR))
            )
            zip_input.click()
            try:
                zip_input.clear()
            except Exception:
                pass
            zip_input.send_keys(zip_code)

            submit_button = next(
                (b for b in self.driver.find_elements(By.TAG_NAME, "button") if b.text.strip() == "Set location"),
                None,
            )
            if submit_button:
                submit_button.click()
            else:
                from selenium.webdriver.common.keys import Keys

                zip_input.send_keys(Keys.RETURN)
        except Exception:
            return False

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_inline_bot_challenge():
                return False
            if urlsplit(self.driver.current_url).path.rstrip("/") != page_path_before:
                return False
        return True

    def _extract_standard_price(self) -> tuple[float | None, str]:
        """Reported from live testing: clicking a pharmacy row (see
        _get_standard_price_for_row() below) updates a "Standard GoodRx
        Price" panel elsewhere on the page — the plain coupon price, no
        Companion membership needed. The exact panel markup isn't
        independently selector-confirmed here (blocked by CAPTCHA while
        trying), so this searches the rendered body text for the label and
        reads whatever $ amount immediately follows it, rather than
        depending on a specific CSS structure that could be wrong.

        Reviewed for quality: the "search for label, read $ in a window
        after it" part below used to be reimplemented here — now shared
        via utils.extract_price_after_label(), also used by
        amazon_scraper.py's identical _extract_price_after(); this call
        keeps the same 100/60 window/raw-snippet sizes already confirmed
        live for this page, so nothing about this method's own behavior
        changed."""
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return None, ""
        return extract_price_after_label(body_text, "standard goodrx price", window=100, raw_trim=60)

    def _get_standard_price_for_row(
        self, selector: str, index: int, previous_price: float | None
    ) -> tuple[float | None, str]:
        """Click the row at (selector, index) to select that pharmacy,
        then read the resulting Standard GoodRx Price. Re-finds the
        element fresh right before clicking — the element reference from
        the original extraction pass may be stale by the time this runs
        (a prior row's click can re-render the list).

        Polls for the price rather than a flat sleep, and requires it to
        be stable across two checks ~0.5s apart before accepting it — the
        same pattern already needed for costplusdrugs_scraper.py's price
        extraction and singlecare_scraper.py's challenge re-check.
        Confirmed live this was actually needed here too, not just
        theoretically: a first version using a flat 1s sleep only
        successfully got a standard price for 1 of 5 rows clicked through
        in the same run — the panel evidently doesn't always finish
        updating (or settle on the newly-clicked pharmacy specifically)
        within a fixed delay.

        `previous_price` guards against a subtler bug that "stable"
        checking alone doesn't catch: if the panel simply hasn't updated
        *at all* yet, two reads 0.5s apart read the same (stale, leftover
        from whichever pharmacy was selected before) value and look
        "stable" despite not actually being this row's price yet. Require
        the value to differ from whatever the last row's confirmed price
        was before accepting it as stable — the caller tracks and passes
        this in across rows; None for the very first row, since there's
        nothing to distinguish it from yet.

        Bounded by STANDARD_PRICE_WAIT_TIMEOUT (deliberately shorter than
        the general SELENIUM_WAIT_TIMEOUT): this runs once per Companion
        row on the page — at the general timeout's default of 15s each, a
        run where several rows never update at all could leave the window
        sitting idle for well over a minute, reported live as exactly
        that."""
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            elements[index].click()
        except Exception:
            return None, ""

        deadline = time.monotonic() + STANDARD_PRICE_WAIT_TIMEOUT
        price, raw = None, ""
        while time.monotonic() < deadline:
            price, raw = self._extract_standard_price()
            if price is not None and price != previous_price:
                time.sleep(0.5)
                price_again, raw_again = self._extract_standard_price()
                if price_again == price:
                    return price, raw_again
                price, raw = price_again, raw_again
            time.sleep(0.5)
        return price, raw

    def _expand_pharmacy_list(self) -> None:
        """Reported from live testing: the default pharmacy list can be
        truncated — an expand control reveals the rest, including several
        no-strings-attached options (Costco, Sam's Club, Walmart, Capsule
        Pharmacy) that otherwise never appear at all. This explains the
        inconsistent pharmacy counts seen across different runs: whether
        the fuller list showed up depended on whether this had already
        been expanded, not just on what GoodRx happened to return.

        Not independently confirmed here: my own attempt to reproduce the
        truncated state landed on a page that already showed the full
        list, so the control's exact wording couldn't be directly seen.
        This is broadened to a few plausible variants rather than the one
        originally guessed ("See all pharmacies", which never matched
        anything) — and deliberately scoped to the pharmacy list's own
        container rather than the whole page, skipping anything with a
        real navigation `href`: the page has an unrelated "View All" link
        (in a footer "Browse medications" section, pointing to /drugs) —
        a page-wide search risked clicking that instead and navigating
        away from the drug page entirely."""
        candidates = ("see all pharmacies", "view all pharmacies", "see all", "view all", "show all", "more pharmacies")
        try:
            rows = self.driver.find_elements(By.CSS_SELECTOR, PRICE_ROW_SELECTORS[0])
            if not rows:
                return
            container = rows[0]
            for _ in range(4):
                container = container.find_element(By.XPATH, "..")
            for el in container.find_elements(By.CSS_SELECTOR, "button, a"):
                text = (el.text or "").strip().lower()
                if not any(c in text for c in candidates):
                    continue
                href = el.get_attribute("href")
                if href and not href.rstrip("/").endswith("#") and "javascript:" not in href:
                    # A real navigation target — likely one of the page's
                    # unrelated "View All" links, not an in-place
                    # expander. Skip rather than risk navigating away.
                    continue
                el.click()
                time.sleep(1)
                return
        except Exception:
            pass

    def _extract_price_rows(self) -> tuple[list[dict], str]:
        html = self.driver.page_source
        rows: list[dict] = []
        for selector in PRICE_ROW_SELECTORS:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            if not elements:
                continue
            for index, el in enumerate(elements):
                # Confirmed live real structure (for the primary selector):
                # [data-qa=seller-name] (plain text, e.g. "Walgreens") and
                # [data-qa=seller-price] as direct children of the row.
                # Fall back to the old whole-text-minus-price guess for the
                # speculative selectors further down PRICE_ROW_SELECTORS.
                try:
                    name = el.find_element(By.CSS_SELECTOR, "[data-qa=seller-name]").text.strip()
                except Exception:
                    name = ""
                try:
                    price_text = el.find_element(By.CSS_SELECTOR, "[data-qa=seller-price]").text.strip()
                except Exception:
                    try:
                        price_text = el.text.strip()
                    except Exception:
                        continue
                if not price_text:
                    continue
                price = extract_price_from_text(price_text)
                if price is None:
                    continue
                if not name:
                    name = re.sub(r"\$\s?\d{1,4}(?:\.\d{2})?", "", el.text.strip()).strip(" \n-—|")
                    name = name.splitlines()[0] if name else ""
                # [data-qa=special-offer-text] (e.g. "with Companion") means
                # this price needs a paid GoodRx membership signup — not a
                # no-strings-attached coupon price. Flag it rather than
                # silently presenting it as one.
                try:
                    offer_note = el.find_element(By.CSS_SELECTOR, "[data-qa=special-offer-text]").text.strip()
                except Exception:
                    offer_note = ""
                try:
                    raw = el.text.strip()[:150]
                except Exception:
                    raw = price_text
                rows.append({
                    "pharmacy": name[:40], "price": price, "raw": raw, "offer_note": offer_note,
                    "_selector": selector, "_index": index,
                })
            if rows:
                break
        return rows, html

    def _resolve_drug_url(self, drug_name: str) -> tuple[str, str]:
        """Returns (resolved_url, error). Drives GoodRx's real homepage
        search rather than guessing a URL slug from drug_name — see this
        module's docstring for why, and for what's and isn't confirmed
        live about this specific method.

        Reactive challenge-handling only (checks after navigating/typing,
        then calls the existing _resolve_bot_challenge() if needed) — no
        preemptive headless-to-visible switch here, unlike
        _try_enter_zip()/_try_edit_prescription(). Those switch ahead of
        time because their specific action is *confirmed* to reliably
        trigger a challenge; that hasn't been confirmed for search
        (the one challenge seen here during investigation may have been
        this file's already-documented intermittent page-load risk, not
        specific to searching), so this doesn't add a preemptive switch
        it can't justify yet."""
        try:
            self.driver.get(SEARCH_HOME_URL)
        except Exception as e:
            return "", f"navigation to GoodRx homepage failed: {e}"

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_bot_challenge(SEARCH_HOME_URL):
                self._save_debug_page("page_goodrx_search_challenge_unresolved.html")
                return "", (
                    "bot check (CAPTCHA) shown on GoodRx's search and not resolved — see "
                    "page_goodrx_search_challenge_unresolved.html, or set "
                    "GOODRX_INTERACTIVE_CAPTCHA=false to skip this prompt"
                )

        try:
            search_input = _wait_for_visible_element(
                self.driver, SEARCH_INPUT_SELECTOR, Config.SELENIUM_WAIT_TIMEOUT
            )
            if search_input is None:
                raise TimeoutException(f"no visible element matched {SEARCH_INPUT_SELECTOR!r}")
            search_input.click()
            search_input.send_keys(drug_name)
        except Exception as e:
            # Reported live: GoodRx produced no results at all, with no
            # debug artifact left behind to show why — this and the
            # "could not submit" branch below were the only failure paths
            # in this method that never called _save_debug_page(), the
            # same gap already found and fixed for singlecare_scraper.py's
            # equivalent. Fixed here the same way, so a repeat of this
            # failure leaves something to diagnose from.
            self._save_debug_page("page_goodrx_search_no_input.html")
            return "", f"could not type into GoodRx's search box: {e} — see page_goodrx_search_no_input.html"

        # Reported live for SingleCare's identical send_keys()-then-poll
        # pattern: a plain presence check can catch this dropdown mid
        # keystroke-by-keystroke update (send_keys types one character at
        # a time) rather than settled on the completed drug name, and
        # click/submit against a stale partial-query state. Not
        # independently confirmed as the cause here specifically (see
        # this method's docstring for what is and isn't confirmed live
        # about GoodRx's search), but defended against anyway on the same
        # reasoning: two reads of the candidate suggestion's own text,
        # ~0.3s apart, must agree before it's trusted — see
        # _pick_search_result()'s docstring for the real dropdown's
        # settle check.
        clicked_suggestion = False
        try:
            option = _pick_search_result(self.driver, drug_name)
            if option is not None:
                robust_click(self.driver, option)
                clicked_suggestion = True
        except Exception:
            pass

        # Legacy fallback: kept in case a future page variant reintroduces
        # a real role="option"/role="listbox" dropdown. Not used as the
        # primary path any more — confirmed live, it used to match an
        # unrelated always-present "Popular searches" panel instead of any
        # actual per-query result (see SEARCH_RESULT_SELECTOR's docstring).
        if not clicked_suggestion:
            for selector in SEARCH_SUGGESTION_SELECTORS_LEGACY:
                try:
                    deadline = time.monotonic() + 3
                    last_text = None
                    option = None
                    while time.monotonic() < deadline:
                        candidate = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        text = candidate[0].text.strip() if candidate else ""
                        if text and text == last_text:
                            option = candidate[0]
                            break
                        last_text = text
                        time.sleep(0.3)
                    if option is None:
                        continue
                    robust_click(self.driver, option)
                    clicked_suggestion = True
                    break
                except Exception:
                    continue

        if not clicked_suggestion:
            try:
                from selenium.webdriver.common.keys import Keys

                search_input.send_keys(Keys.RETURN)
            except Exception as e:
                self._save_debug_page("page_goodrx_search_no_submit.html")
                return "", f"could not submit GoodRx's search box: {e} — see page_goodrx_search_no_submit.html"

        try:
            WebDriverWait(self.driver, Config.SELENIUM_WAIT_TIMEOUT).until(
                lambda d: d.current_url.rstrip("/") != SEARCH_HOME_URL.rstrip("/")
            )
        except Exception:
            pass

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_bot_challenge(self.driver.current_url):
                self._save_debug_page("page_goodrx_search_challenge_unresolved.html")
                return "", "bot check (CAPTCHA) shown after searching and not resolved"

        resolved = self.driver.current_url.split("?")[0].rstrip("/")
        if _looks_like_drug_page_url(resolved.split("goodrx.com")[-1]):
            return resolved, ""

        # Landed somewhere other than a direct drug page — most likely a
        # search-results listing, given clicked_suggestion is False in
        # that case. Look for a link to one, matched loosely against the
        # requested drug name, rather than assuming the top link is right.
        try:
            target_norm = re.sub(r"[^a-z0-9]", "", drug_name.lower())
            for el in self.driver.find_elements(By.CSS_SELECTOR, "a[href]"):
                href = el.get_attribute("href") or ""
                path = href.split("?")[0].rstrip("/").split("goodrx.com")[-1]
                if not _looks_like_drug_page_url(path):
                    continue
                text_norm = re.sub(r"[^a-z0-9]", "", (el.text or "").lower())
                if target_norm and (target_norm in text_norm or text_norm in target_norm):
                    return href, ""
        except Exception:
            pass

        self._save_debug_page("page_goodrx_search_no_match.html")
        return "", (
            f"search for '{drug_name}' did not land on a recognizable drug page "
            f"(ended up at {resolved}) — see page_goodrx_search_no_match.html"
        )

    def search(
        self, drug_name: str, formulation: str, dosage: str,
        zip_code: str | None = None, quantity: str | None = None,
    ) -> list[PriceResult]:
        resolved_url, resolve_error = self._resolve_drug_url(drug_name)
        if not resolved_url:
            return [
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME,
                    error=resolve_error or f"could not resolve a GoodRx page for '{drug_name}'",
                )
            ]
        url = resolved_url
        try:
            self.driver.get(url)
        except Exception as e:
            return [
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, url=url, error=f"navigation failed: {e}",
                )
            ]

        self._save_debug_page("page_goodrx_initial.html")

        if _looks_like_bot_challenge(self.driver):
            if not self._resolve_bot_challenge(url):
                self._save_debug_page("page_goodrx_challenge_unresolved.html")
                return [
                    PriceResult(
                        drug_name=drug_name, formulation=formulation, dosage=dosage,
                        source=SOURCE_NAME, url=url,
                        error=(
                            "bot check (CAPTCHA) shown and not resolved — see "
                            "page_goodrx_challenge_unresolved.html, or set "
                            "GOODRX_INTERACTIVE_CAPTCHA=false to skip this prompt"
                        ),
                    )
                ]
            self._save_debug_page("page_goodrx_after_challenge.html")

        # ZIP first, *then* dosage/quantity — deliberately, not just
        # incidentally. _try_enter_zip() can close and recreate the driver
        # entirely (see its docstring: switching to a visible window
        # before attempting a ZIP change, since that page action reliably
        # triggers its own bot-check). Confirmed live: doing dosage/
        # quantity selection first meant that recreation silently
        # discarded it — the fresh page after a ZIP-triggered relaunch
        # comes up with default dosage/quantity again, with nothing
        # having gone wrong loudly enough to notice. Selecting them after
        # instead means they apply to whichever driver is actually active
        # by the time it matters.
        zip_caveat = ""
        if zip_code:
            self._try_enter_zip(zip_code)
            # Reported live: even with _try_enter_zip() itself now
            # detecting a bot-challenge-triggered drift off this page
            # (see its docstring), this call site never looked at its
            # return value at all — the pipeline plowed straight into
            # reading dosage/quantity/prices off whatever page the drift
            # actually left us on (confirmed live: GoodRx's generic
            # "sign up for a free savings card" page), which predictably
            # has none of that, silently producing "no price rows found"
            # instead of an explanation. Checked directly here — by path,
            # not by trusting _try_enter_zip()'s own boolean, so this
            # also catches drift from a cause neither of us has seen yet
            # — rather than continuing on a page that plainly isn't the
            # one this method is supposed to be reading.
            if urlsplit(self.driver.current_url).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                self._save_debug_page("page_goodrx_zip_drift.html")
                return [
                    PriceResult(
                        drug_name=drug_name, formulation=formulation, dosage=dosage,
                        source=SOURCE_NAME, url=url,
                        error=(
                            f"page drifted away from {url} while setting ZIP {zip_code} "
                            "(likely a bot-check redirect) — see page_goodrx_zip_drift.html"
                        ),
                    )
                ]
            # Confirmed live: entering a zip that's never actually applied
            # (e.g. the modal-click/type attempt above silently found
            # nothing, same failure mode this replaced) would otherwise
            # return prices for GoodRx's default geo-IP-guessed location
            # with no indication they're not for the requested one — the
            # same silent-wrong-data problem singlecare_scraper.py had.
            # Poll rather than trust a single immediate check — see
            # _wait_for_rendered_zip()'s docstring for why that mattered
            # here too, not just in theory.
            rendered_zip = _wait_for_rendered_zip(self.driver, zip_code, Config.SELENIUM_WAIT_TIMEOUT)
            if not _zip_matches(rendered_zip, zip_code):
                zip_caveat = f"could not confirm ZIP {zip_code} was applied (page showed '{rendered_zip}')"
            # _wait_for_rendered_zip() above only confirms the location
            # button's own text updated — it says nothing about whether
            # the pharmacy list (which a ZIP change also reloads
            # asynchronously) has finished re-rendering yet. Same
            # "very few options" risk _try_edit_prescription() below
            # already has its own version of — see
            # _wait_for_stable_price_rows()'s docstring.
            _wait_for_stable_price_rows(self.driver, Config.SELENIUM_WAIT_TIMEOUT)

        # Read the current default (e.g. "20mg", "30") cheaply — no modal
        # needed — before deciding whether an edit is even necessary.
        # Reported live: a Quantity dropdown exists and does change listed
        # prices, and quantity was never actually reported anywhere in
        # this file's output despite being accepted as a parameter the
        # whole time. Fixed alongside dosage, which turned out to have a
        # deeper, pre-existing bug: see DOSAGE_SELECT_SELECTORS' docstring
        # — its old select-based lookup could never have matched anything
        # on this page, so a requested dosage was never actually
        # correctable here either, only ever silently ignored.
        current_dosage, current_quantity, current_form = _parse_medication_summary(
            self._read_medication_summary()
        )
        needs_dosage_change = bool(dosage) and not _loose_matches(current_dosage, dosage)
        needs_quantity_change = bool(quantity) and not _loose_matches(current_quantity, quantity)

        # Reported live: formulation was accepted as a parameter but
        # never checked against anything on the page — unlike dosage/
        # quantity, which already get a real caveat on mismatch. There's
        # no on-page control to *select* a different form here (GoodRx's
        # modal has no formulation field at all, confirmed live
        # elsewhere in this file/README), so this can only flag a
        # mismatch, not correct one — same asymmetry the project already
        # accepts for e.g. Amazon's quantity ("no separate field to
        # select, but still worth telling the user"). Only fires when a
        # form word was actually parsed; an unparseable summary already
        # falls back to "can't tell, don't guess" everywhere else in
        # this function, and formulation gets the same tolerance.
        formulation_caveat = ""
        if formulation and current_form and not token_matches(formulation, current_form):
            formulation_caveat = (
                f"requested formulation {formulation} may not match this page "
                f"(page showed '{current_form}', not independently selectable here)"
            )

        dosage_caveat = ""
        quantity_caveat = ""
        if needs_dosage_change or needs_quantity_change:
            self._try_edit_prescription(
                dosage if needs_dosage_change else None,
                quantity if needs_quantity_change else None,
            )
            # Re-check afterward, the same "don't just trust the attempt"
            # rule as ZIP above — an edit can fail silently (challenge
            # skipped, selector drift) while price extraction below still
            # succeeds against the unchanged, now-stale page. Polled
            # rather than read once immediately — see
            # _wait_for_updated_medication_summary()'s docstring for why
            # that mattered here too, not just in theory (a user who
            # genuinely solved the inline challenge still hit this
            # caveat).
            current_dosage, current_quantity = _wait_for_updated_medication_summary(
                self.driver,
                dosage if needs_dosage_change else None,
                quantity if needs_quantity_change else None,
                Config.SELENIUM_WAIT_TIMEOUT,
            )
            if needs_dosage_change and not _loose_matches(current_dosage, dosage):
                dosage_caveat = f"could not confirm dosage {dosage} was selected (page showed '{current_dosage}')"
            if needs_quantity_change and not _loose_matches(current_quantity, quantity):
                quantity_caveat = f"could not confirm quantity {quantity} was selected (page showed '{current_quantity}')"

        # See _expand_pharmacy_list()'s docstring: without this, the
        # pharmacy list can be truncated, and which pharmacies happen to
        # be in that truncated set (vs. only reachable after expanding)
        # isn't something the plain-row fix above can help with — this
        # has to run before extraction, not after.
        self._expand_pharmacy_list()

        try:
            rows, _html = self._extract_price_rows()
        except Exception as e:
            self._save_debug_page("page_goodrx_error.html")
            return [
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, url=url, error=f"price extraction failed: {e}",
                )
            ]

        if not rows:
            self._save_debug_page("page_goodrx_no_rows.html")
            return [
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, url=url,
                    error="no price rows found (blocked, CAPTCHA'd, or page structure changed — see page_goodrx_no_rows.html)",
                )
            ]

        # Sorting *all* rows by raw displayed price and taking the top 5
        # was wrong: Companion-priced rows show an artificially low price
        # ($0.00, $9.00 — a paid-membership discount), so they crowded out
        # every genuinely no-strings-attached row entirely. Reported live:
        # Costco, Sam's Club, Walmart, and Capsule Pharmacy — all plain
        # rows with no Companion offer — never appeared in the output at
        # all, even though they were the actually-accessible options.
        # Fixed: always include every plain row (no click-through needed —
        # its displayed price already is the standard one), plus every
        # Companion row too. An earlier version of this fix capped the
        # output list itself at GOODRX_COMPANION_ROWS_LIMIT (the cheapest
        # few Companion rows only) — confirmed live, via a saved copy of
        # the actual page (a real 9-row list: 4 plain, 5 Companion), that
        # this dropped 2 of the 5 Companion rows entirely, matching a
        # follow-up report that CVS and Target had gone missing after the
        # first fix.
        plain_rows = sorted((r for r in rows if not r.get("offer_note")), key=lambda r: r["price"])
        companion_rows = sorted((r for r in rows if r.get("offer_note")), key=lambda r: r["price"])
        rows_sorted = plain_rows + companion_rows

        # The plain, no-strings-attached "Standard GoodRx Price" for a
        # Companion row is only visible after clicking that pharmacy's row
        # (reported from live testing; see _extract_standard_price()'s
        # docstring for what is and isn't independently confirmed here).
        # A prior version of this loop also capped itself to the cheapest
        # GOODRX_COMPANION_ROWS_LIMIT Companion rows — confirmed live, via
        # a debug dump of an actual run, that this silently skipped CVS
        # Pharmacy: three other Companion rows tied for cheapest at $0.00
        # filled the cap entirely, so CVS ($9.00) and Target ($9.00) never
        # got a click-through attempt at all. Removed: every Companion row
        # is attempted now; STANDARD_PRICE_WAIT_TIMEOUT alone bounds the
        # cost, per row.
        # last_standard_price feeds _get_standard_price_for_row()'s
        # stale-value guard — see its docstring.
        last_standard_price = None
        for row in companion_rows:
            try:
                standard_price, standard_raw = self._get_standard_price_for_row(
                    row["_selector"], row["_index"], last_standard_price
                )
            except Exception:
                standard_price, standard_raw = None, ""
            if standard_price is not None:
                row["standard_price"] = standard_price
                row["standard_raw"] = standard_raw
                last_standard_price = standard_price

        all_caveats = "; ".join(
            c for c in (zip_caveat, dosage_caveat, quantity_caveat, formulation_caveat) if c
        )

        def _label_for(row: dict, *, standard: bool = False) -> str:
            label = "cash/coupon price"
            if standard:
                label += " (standard, no Companion membership)"
            elif row.get("offer_note"):
                # e.g. "with Companion" — a paid GoodRx membership price,
                # not a no-strings-attached one. Per-row, since not every
                # pharmacy necessarily has this offer.
                label += f" ({row['offer_note']})"
            if all_caveats:
                label += f" — prices may be inaccurate: {all_caveats}"
            return label

        # Whatever quantity the page actually ended up pricing — not
        # necessarily what was requested, if quantity_caveat above already
        # flagged that it couldn't be confirmed. Reported live: this was
        # never surfaced anywhere before, for any result, on this site.
        quantity_label = f"{current_quantity} tablets" if current_quantity else ""

        results = []
        for row in rows_sorted:
            results.append(
                PriceResult(
                    drug_name=drug_name, formulation=formulation, dosage=dosage,
                    source=SOURCE_NAME, price=row["price"], price_label=_label_for(row),
                    pharmacy=row["pharmacy"], quantity=quantity_label, url=url, raw_text=row["raw"],
                )
            )
            if row.get("standard_price") is not None:
                results.append(
                    PriceResult(
                        drug_name=drug_name, formulation=formulation, dosage=dosage,
                        source=SOURCE_NAME, price=row["standard_price"], price_label=_label_for(row, standard=True),
                        pharmacy=row["pharmacy"], quantity=quantity_label, url=url, raw_text=row["standard_raw"],
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
    try:
        with GoodRxScraper(headless=Config.HEADLESS_DEFAULT) as scraper:
            return scraper.search(drug_name, formulation, dosage, zip_code, quantity)
    except Exception as e:
        return [
            PriceResult(
                drug_name=drug_name,
                formulation=formulation,
                dosage=dosage,
                source=SOURCE_NAME,
                error=f"unexpected error: {e}",
            )
        ]


if __name__ == "__main__":
    results = get_prices("Lisinopril", "tablet", "20mg")
    for r in results:
        print(r)
