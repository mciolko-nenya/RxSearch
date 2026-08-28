"""
Configuration for RxComp, loaded from environment variables (.env).

Unlike Schedule Checker's config.py, nothing here is hardcoded — every
setting flows through os.getenv() so credentials/settings never end up
committed to source. There are deliberately no Amazon username/password
fields: Amazon login happens interactively into a persistent Chrome
profile (see amazon_scraper.py), not via stored credentials.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # Optional default ZIP code used for pharmacy pricing lookups.
    DEFAULT_ZIP_CODE = os.getenv("DEFAULT_ZIP_CODE", "")

    # Run Selenium browsers headless by default. --debug overrides to False.
    HEADLESS_DEFAULT = os.getenv("HEADLESS_DEFAULT", "true").lower() == "true"

    # Timeouts.
    REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
    SELENIUM_PAGE_LOAD_TIMEOUT = int(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT", "30"))
    SELENIUM_WAIT_TIMEOUT = int(os.getenv("SELENIUM_WAIT_TIMEOUT", "15"))

    # Directory (relative to repo root, or absolute) holding the persistent
    # Chrome profile used to reuse a logged-in Amazon session. Created by
    # `python main.py --setup-amazon`. Not a credential store.
    AMAZON_CHROME_PROFILE_DIR = os.getenv(
        "AMAZON_CHROME_PROFILE_DIR", ".chrome-profile-amazon"
    )

    # Spoofed desktop Chrome UA, matching Schedule Checker's scraper.py.
    SCRAPER_USER_AGENT = os.getenv(
        "SCRAPER_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )

    # Comma-separated list of sites to query by default; overridable per-run
    # with --sites.
    ENABLED_SITES = [
        s.strip()
        for s in os.getenv(
            "ENABLED_SITES", "goodrx,singlecare,amazon,costplusdrugs"
        ).split(",")
        if s.strip()
    ]

    ALL_SITES = ("goodrx", "singlecare", "amazon", "costplusdrugs")

    # Cost Plus Drugs can be looked up two ways:
    #   "live" — always scrape the live site (original behavior).
    #   "file" — check the static formulary spreadsheet first (see
    #            costplusdrugs_formulary.py); if the drug/dosage/formulation
    #            isn't listed there at all, skip the live site entirely
    #            (it's not carried). If it IS listed, still scrape live —
    #            the spreadsheet has no price column, only what's carried.
    COSTPLUSDRUGS_LOOKUP_MODE = os.getenv("COSTPLUSDRUGS_LOOKUP_MODE", "live").strip().lower()
    COSTPLUSDRUGS_LOOKUP_MODES = ("live", "file")

    # Path (relative to repo root, or absolute) to the Team Cuban Card
    # downloadable medication list (.xlsx) used when COSTPLUSDRUGS_LOOKUP_MODE
    # is "file". Only read when that mode is active.
    COSTPLUSDRUGS_FORMULARY_PATH = os.getenv(
        "COSTPLUSDRUGS_FORMULARY_PATH",
        "TeamCubanCard_DownloadableMedicationList_08.06.2026.xlsx",
    )

    # Path (relative to repo root, or absolute) to the cached record of
    # Cost Plus Drugs' standard shipping fee — not returned by their API
    # (see costplusdrugs_scraper.py's docstring) and only readable by
    # driving a real browser (see costplusdrugs_shipping.py's docstring
    # for why). Written by `python costplusdrugs_shipping.py` /
    # `python main.py --update-costplusdrugs-shipping`, read by
    # costplusdrugs_scraper.py's get_prices() on every lookup. Missing or
    # unreadable is treated as "no cached fee yet", not an error.
    COSTPLUSDRUGS_SHIPPING_PATH = os.getenv(
        "COSTPLUSDRUGS_SHIPPING_PATH", "costplusdrugs_shipping.json"
    )

    # GoodRx frequently shows an interactive bot-check (a PerimeterX
    # px-captcha widget, sometimes a Cloudflare-style challenge instead) —
    # something no headless session can solve, since no human is looking at
    # it. When enabled (default), goodrx_scraper.py opens a *visible* Chrome
    # window and waits for you to solve it at the terminal before
    # continuing. Set to false for unattended/automated runs, where there's
    # no one to solve it anyway — GoodRx will just report the block as an
    # error like any other failure.
    GOODRX_INTERACTIVE_CAPTCHA = os.getenv("GOODRX_INTERACTIVE_CAPTCHA", "true").lower() == "true"

    # Same idea, for SingleCare's DataDome challenge (an iframed CAPTCHA
    # widget served from geo.captcha-delivery.com) — see
    # singlecare_scraper.py's _resolve_bot_challenge().
    SINGLECARE_INTERACTIVE_CAPTCHA = os.getenv("SINGLECARE_INTERACTIVE_CAPTCHA", "true").lower() == "true"

    @classmethod
    def validate(cls):
        errors = []

        if not cls.AMAZON_CHROME_PROFILE_DIR:
            errors.append("AMAZON_CHROME_PROFILE_DIR is required")

        if cls.DEFAULT_ZIP_CODE and (
            not cls.DEFAULT_ZIP_CODE.isdigit() or len(cls.DEFAULT_ZIP_CODE) != 5
        ):
            errors.append("DEFAULT_ZIP_CODE must be a 5-digit ZIP code if set")

        if cls.REQUEST_TIMEOUT_SECONDS <= 0:
            errors.append("REQUEST_TIMEOUT_SECONDS must be positive")

        if cls.SELENIUM_PAGE_LOAD_TIMEOUT <= 0:
            errors.append("SELENIUM_PAGE_LOAD_TIMEOUT must be positive")

        if cls.SELENIUM_WAIT_TIMEOUT <= 0:
            errors.append("SELENIUM_WAIT_TIMEOUT must be positive")

        if not cls.ENABLED_SITES:
            errors.append("ENABLED_SITES must list at least one site")

        unknown = set(cls.ENABLED_SITES) - set(cls.ALL_SITES)
        if unknown:
            errors.append(
                f"ENABLED_SITES has unknown site(s): {', '.join(sorted(unknown))} "
                f"(valid: {', '.join(cls.ALL_SITES)})"
            )

        if cls.COSTPLUSDRUGS_LOOKUP_MODE not in cls.COSTPLUSDRUGS_LOOKUP_MODES:
            errors.append(
                f"COSTPLUSDRUGS_LOOKUP_MODE must be one of: {', '.join(cls.COSTPLUSDRUGS_LOOKUP_MODES)}"
            )

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))


if __name__ == "__main__":
    # Self-test, matching the sibling project's convention of each module
    # being independently runnable.
    try:
        Config.validate()
        print("Config OK:")
        for name in (
            "DEFAULT_ZIP_CODE",
            "HEADLESS_DEFAULT",
            "REQUEST_TIMEOUT_SECONDS",
            "SELENIUM_PAGE_LOAD_TIMEOUT",
            "SELENIUM_WAIT_TIMEOUT",
            "AMAZON_CHROME_PROFILE_DIR",
            "ENABLED_SITES",
            "COSTPLUSDRUGS_LOOKUP_MODE",
            "COSTPLUSDRUGS_FORMULARY_PATH",
            "COSTPLUSDRUGS_SHIPPING_PATH",
            "GOODRX_INTERACTIVE_CAPTCHA",
            "SINGLECARE_INTERACTIVE_CAPTCHA",
        ):
            print(f"  {name} = {getattr(Config, name)!r}")
    except ValueError as e:
        print(f"Config INVALID:\n{e}")
