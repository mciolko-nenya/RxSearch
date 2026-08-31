"""
Cost Plus Drugs' standard shipping fee — maintained as a small, periodically
refreshed local file, not looked up live on every price request.

Their public API (see costplusdrugs_scraper.py's docstring) doesn't return
a shipping fee at all — only the drug price itself. Confirmed live: the fee
is disclosed on the product page as its own line ("Standard Shipping
*Additional cost at checkout $5.25"), but that page sits behind a genuine
Cloudflare JS challenge ("Just a moment..." interstitial) — confirmed live
that even a plain HTTP request with a realistic browser User-Agent still
gets HTTP 403, so there is no lightweight, non-Selenium way to read it.
Getting this one number at all means driving a real browser, the same
class of infrastructure this project moved Cost Plus Drugs' own price
lookups *away* from when it switched to the API.

Rather than reintroduce that dependency into every price lookup, it's
scoped to exactly this one fee and run occasionally instead of per-request:

  - update_shipping_fee() drives a real Chrome session (reusing
    driver_utils' existing undetected-chromedriver setup — the same one
    GoodRx/SingleCare/Amazon still use) to read the fee directly off a
    live product page, and writes it to a small JSON file alongside an
    ISO-8601 UTC timestamp of when it was checked.
  - load_shipping_fee() is the cheap, non-Selenium read
    costplusdrugs_scraper.py's get_prices() calls on every lookup — it
    only ever reads the cached JSON file, never touches a browser.

Run this module directly, or `python main.py --update-costplusdrugs-
shipping`, to refresh the file:

    python costplusdrugs_shipping.py            # headless (default)
    python costplusdrugs_shipping.py --debug     # visible Chrome window

There's no built-in scheduler here — this project doesn't run a
background process — so "periodically" means either running that by hand
now and then, or pointing an external cron/launchd job at it. Cost Plus
Drugs' shipping fee changes rarely (confirmed live: unchanged $5.25 across
every check made during this project's history so far, across different
drugs and quantities), so checking every few months is almost certainly
more than enough — the file's own `checked_at` timestamp is what makes a
stale value visible to a caller rather than silently trusted forever (see
costplusdrugs_scraper.py's staleness caveat).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import re

from selenium.webdriver.common.by import By

from config import Config
from driver_utils import SeleniumScraperBase

# Bounded to 100 chars after the label — not a greedy/unbounded match — so
# this can't run past an unrelated later $ amount elsewhere on the page if
# its wording ever shifts. Same pattern this project already used for this
# exact extraction before Cost Plus Drugs switched to its API (see git
# history on costplusdrugs_scraper.py for the retired SHIPPING_PATTERN).
SHIPPING_PATTERN = re.compile(r"Standard Shipping[^$]{0,100}\$\s*(\d+(?:\.\d{2})?)", re.IGNORECASE)

# Any real, always-carried product page works: the fee is a flat,
# drug-independent shipping charge (confirmed live previously: unchanged
# across 30/60/90-count for the same drug), not something specific to this
# particular one — it's just a stable, known-good page to load.
DEFAULT_CHECK_URL = "https://www.costplusdrugs.com/medications/atorvastatin-40mg-tablet/"


@dataclass
class ShippingFeeRecord:
    fee: float
    checked_at: str  # ISO-8601, UTC
    checked_url: str


def load_shipping_fee(path: str | None = None) -> ShippingFeeRecord | None:
    """Cheap, non-Selenium read. Never raises — missing file, corrupt
    JSON, or an unexpected shape are all just "no cached fee yet" from
    the caller's point of view, same as this project's other
    never-raise-past-get_prices() scraper contracts."""
    path = path or Config.COSTPLUSDRUGS_SHIPPING_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return ShippingFeeRecord(
            fee=float(data["fee"]),
            checked_at=str(data["checked_at"]),
            checked_url=str(data.get("checked_url", "")),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def update_shipping_fee(
    path: str | None = None,
    url: str = DEFAULT_CHECK_URL,
    headless: bool = True,
) -> ShippingFeeRecord:
    """Drives a real Chrome session to read the live fee and writes it to
    `path`. Raises on failure (page structure changed, the shipping line
    never appeared within the timeout, driver/network error) rather than
    silently leaving a stale file in place — the caller (this module's
    __main__, or main.py's --update-costplusdrugs-shipping) is
    responsible for surfacing that."""
    path = path or Config.COSTPLUSDRUGS_SHIPPING_PATH

    with SeleniumScraperBase(headless=headless) as scraper:
        driver = scraper.driver
        driver.get(url)

        # Cloudflare's own JS challenge (confirmed live: it blocks even a
        # plain HTTP fetch with realistic headers) has historically
        # resolved on its own within a few seconds for a real browser —
        # same as it did throughout this project's original Selenium-based
        # Cost Plus Drugs scraper's history, with no interactive
        # CAPTCHA-solve path ever needed for this specific site. Polling
        # for the pattern to actually appear (rather than a fixed sleep)
        # covers both that brief challenge delay and the page's own
        # client-side render time.
        body_text = None
        deadline = time.monotonic() + Config.SELENIUM_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            candidate = driver.find_element(By.TAG_NAME, "body").text
            if SHIPPING_PATTERN.search(candidate):
                body_text = candidate
                break
            time.sleep(0.5)

        if body_text is None:
            scraper._save_debug_page("page_costplusdrugs_shipping_not_found.html")
            raise RuntimeError(
                f"Could not find a 'Standard Shipping ... $X.XX' line on {url} "
                f"within {Config.SELENIUM_WAIT_TIMEOUT}s — saved "
                "page_costplusdrugs_shipping_not_found.html for troubleshooting"
            )

    fee = float(SHIPPING_PATTERN.search(body_text).group(1))
    record = ShippingFeeRecord(
        fee=fee,
        checked_at=datetime.now(timezone.utc).isoformat(),
        checked_url=url,
    )
    with open(path, "w") as f:
        json.dump(asdict(record), f, indent=2)
        f.write("\n")
    return record


if __name__ == "__main__":
    import sys

    headless = "--debug" not in sys.argv
    record = update_shipping_fee(headless=headless)
    print(
        f"Cost Plus Drugs standard shipping fee: ${record.fee:.2f} "
        f"(checked {record.checked_at} against {record.checked_url})"
    )
    print(f"Written to {Config.COSTPLUSDRUGS_SHIPPING_PATH}")
