"""
RxComp — prescription drug price comparison CLI.

Usage:
    python main.py --drug lisinopril --dosage 20mg --formulation tablet --zip 10001
    python main.py --drug metformin --dosage 500mg --sites goodrx,costplusdrugs
    python main.py --drug lisinopril --dosage 20mg --json > prices.json
    python main.py --setup-amazon
    python main.py --update-costplusdrugs-shipping
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from config import Config
from models import PriceResult

# Confirmed against every scraper's actual PriceResult.quantity strings:
# GoodRx "30 tablets", SingleCare "90 count", Cost Plus Drugs "30 count",
# Amazon "Tablet · 20mg · 30 days supply". Anchored to a following unit
# word rather than just grabbing the first number in the string —
# Amazon's format has a dosage number ("20mg") *before* the actual
# quantity, which a bare first-number match would wrongly grab instead.
QUANTITY_COUNT_PATTERN = re.compile(r"(\d+)\s*(?:tablets?|capsules?|counts?|days?\s*supply)", re.IGNORECASE)

SCRAPERS = {}

# Populated alongside SCRAPERS by _load_scrapers(), from each module's own
# SOURCE_NAME — single source of truth, so this can't drift from what each
# scraper's own PriceResults already report. Used by run_site()'s error
# fallback below so a truly unexpected failure still reports the same
# display name ("GoodRx") every other row for that site uses, not the raw
# lowercase --sites/SCRAPERS key ("goodrx").
SOURCE_NAMES = {}


def _load_scrapers():
    """Deferred import so --setup-amazon and --help don't pay the cost of
    importing every scraper module (and their Selenium/requests deps)."""
    import amazon_scraper
    import costplusdrugs_scraper
    import goodrx_scraper
    import singlecare_scraper

    SCRAPERS.update(
        {
            "goodrx": goodrx_scraper.get_prices,
            "singlecare": singlecare_scraper.get_prices,
            "amazon": amazon_scraper.get_prices,
            "costplusdrugs": costplusdrugs_scraper.get_prices,
        }
    )
    SOURCE_NAMES.update(
        {
            "goodrx": goodrx_scraper.SOURCE_NAME,
            "singlecare": singlecare_scraper.SOURCE_NAME,
            "amazon": amazon_scraper.SOURCE_NAME,
            "costplusdrugs": costplusdrugs_scraper.SOURCE_NAME,
        }
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Compare prescription drug prices across pharmacy sites.")
    parser.add_argument("--drug", help="Drug name, e.g. 'lisinopril'")
    parser.add_argument("--dosage", help="Dosage/strength, e.g. '20mg'")
    parser.add_argument("--formulation", default="tablet", help="Formulation, e.g. tablet/capsule/liquid (default: tablet)")
    parser.add_argument(
        "--quantity", default=None,
        help=(
            "Requested pack size/quantity, e.g. '30'. GoodRx/SingleCare/Cost Plus Drugs try to select "
            "it directly; Amazon has no separate quantity field to select. Wherever selection isn't "
            "possible or doesn't match, that result is linearly rescaled to approximate this quantity "
            "instead and clearly labeled as an estimate, not a real quote"
        ),
    )
    parser.add_argument("--zip", dest="zip_code", default=None, help="ZIP code for pricing lookups (defaults to DEFAULT_ZIP_CODE from .env)")
    parser.add_argument("--sites", default=None, help=f"Comma-separated subset of: {', '.join(Config.ALL_SITES)}")
    parser.add_argument("--debug", action="store_true", help="Run browsers visibly, sequentially, and always dump debug HTML")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of a table")
    parser.add_argument("--setup-amazon", action="store_true", help="One-time interactive Amazon login into a persistent Chrome profile")
    parser.add_argument(
        "--update-costplusdrugs-shipping", action="store_true",
        help="Refresh the cached Cost Plus Drugs shipping fee (see costplusdrugs_shipping.py) and exit",
    )
    return parser.parse_args()


def run_site(site: str, drug_name: str, formulation: str, dosage: str, zip_code, quantity) -> list[PriceResult]:
    """Belt-and-suspenders wrapper: every scraper is supposed to self-handle
    its own errors and never raise, but this catches anything truly
    unexpected (e.g. ChromeDriver install failure) so one site's failure
    can never take down the whole run.

    Reviewed live: this fallback used to set source=site — the raw
    lowercase SCRAPERS/--sites key ("goodrx"), not the display name
    ("GoodRx") every other code path uses, including each scraper's own
    internal error paths and models.py's own documented value set for
    PriceResult.source. Exactly the case meant to be the most
    defensively handled ended up the one inconsistent row. Fixed by
    looking up the display name from SOURCE_NAMES, falling back to the
    raw key only if that lookup itself can't find one (e.g. SCRAPERS/
    SOURCE_NAMES were never populated at all — already a distinct,
    unlikely failure of its own)."""
    try:
        return SCRAPERS[site](drug_name, formulation, dosage, zip_code, quantity)
    except Exception as e:
        return [
            PriceResult(
                drug_name=drug_name,
                formulation=formulation,
                dosage=dosage,
                source=SOURCE_NAMES.get(site, site),
                error=f"unexpected error running scraper: {e}",
            )
        ]


def gather_results(sites, drug_name, formulation, dosage, zip_code, quantity, sequential: bool) -> list[PriceResult]:
    if sequential:
        results = [run_site(s, drug_name, formulation, dosage, zip_code, quantity) for s in sites]
    else:
        with ThreadPoolExecutor(max_workers=max(len(sites), 1)) as executor:
            futures = [
                executor.submit(run_site, s, drug_name, formulation, dosage, zip_code, quantity)
                for s in sites
            ]
            results = [f.result() for f in futures]
    return [r for group in results for r in group]


def filter_out_insurance_required(results: list[PriceResult]) -> list[PriceResult]:
    """Drops any result that needs actual insurance coverage to get —
    currently just Amazon Pharmacy's "Average insurance price" row (see
    `PriceResult.requires_insurance`'s docstring for why this checks that
    structured flag rather than sniffing `price_label` for the word
    "insurance": Cost Plus Drugs' and Amazon's own cash prices both say
    "no insurance" in their own label). Cash/coupon/discount-club prices
    (GoodRx Companion, SingleCare Member Bonus) aren't insurance and are
    left alone — those need signing up for something, not having
    insurance, and this project already surfaces both tiers for those
    honestly rather than hiding either one."""
    return [r for r in results if not r.requires_insurance]


def _parse_quantity_count(quantity_text: str) -> int | None:
    """Extracts the pack-size/day-supply integer from a PriceResult's
    free-text `quantity` field. Returns None if nothing matches — callers
    treat that as "can't tell, don't guess", the same tolerance used
    throughout the individual scrapers for soft-matching dosage/zip/
    quantity elsewhere in this project."""
    if not quantity_text:
        return None
    match = QUANTITY_COUNT_PATTERN.search(quantity_text)
    return int(match.group(1)) if match else None


def _parse_requested_quantity(quantity_arg: str | None) -> int | None:
    """--quantity is documented as a plain count (e.g. '30'), so this
    just grabs the first number rather than requiring a unit word like
    _parse_quantity_count above — there's no dosage number to
    accidentally collide with in a user-typed --quantity value."""
    if not quantity_arg:
        return None
    match = re.search(r"(\d+)", quantity_arg)
    return int(match.group(1)) if match else None


def normalize_quantities(results: list[PriceResult], requested_quantity: str | None) -> list[PriceResult]:
    """When --quantity is requested and a result's actual quantity doesn't
    match it — whether because that site has no quantity selector at all
    (Amazon, Cost Plus Drugs) or because selection was attempted but
    couldn't be confirmed (GoodRx/SingleCare, already flagged with their
    own caveat) — this rescales the price linearly: price × (requested /
    actual), e.g. a 30-day-supply price × 3 to approximate 90 days.

    This is explicitly a rough estimate, not a real quote, and is always
    labeled as one rather than presented as if a site had actually priced
    the requested quantity: real pharmacy pricing usually isn't perfectly
    linear (Cost Plus Drugs' own price breakdown, for example, includes a
    flat $5 pharmacy fee on top of the per-unit manufacturing cost — that
    fee doesn't triple just because the quantity does), so scaling this
    way can materially over- or under-state the real price at a different
    quantity. It's still a more useful starting point than silently
    comparing prices for different quantities as if they were equivalent,
    which is what happened before this existed.

    Only applied when both the requested and actual quantities can be
    confidently parsed as a plain count via the patterns above; results
    with an error, no price, or an unparseable quantity are left
    untouched rather than guessed at. Mutates and returns the same list
    (each PriceResult in place) rather than building a new one — nothing
    downstream depends on the pre-scaled values once this runs."""
    requested_count = _parse_requested_quantity(requested_quantity)
    if not requested_count:
        return results

    for r in results:
        if r.price is None or r.error:
            continue
        actual_count = _parse_quantity_count(r.quantity)
        if not actual_count or actual_count == requested_count:
            continue
        original_price = r.price
        r.price = round(original_price * requested_count / actual_count, 2)
        r.quantity = f"{requested_count} (est. from {actual_count})"
        estimate_note = (
            f"estimated for qty {requested_count}: ${original_price:.2f} for {actual_count} "
            f"× {requested_count}/{actual_count} — not an actual quote for this quantity"
        )
        r.price_label = f"{r.price_label} — {estimate_note}" if r.price_label else estimate_note
    return results


def sort_results(results: list[PriceResult]) -> list[PriceResult]:
    return sorted(results, key=lambda r: (r.price is None, r.price if r.price is not None else float("inf")))


def print_table(results: list[PriceResult]):
    headers = ["Source", "Price", "Label", "Pharmacy", "Quantity", "Notes"]
    rows = []
    for r in results:
        rows.append(
            [
                r.source,
                f"${r.price:.2f}" if r.price is not None else "N/A",
                r.price_label or "",
                r.pharmacy or "",
                r.quantity or "",
                r.error or "",
            ]
        )

    try:
        from tabulate import tabulate

        print(tabulate(rows, headers=headers, tablefmt="simple"))
        return
    except ImportError:
        pass

    # Plain-text fallback if tabulate isn't installed.
    widths = [max(len(str(h)), *(len(str(row[i])) for row in rows)) if rows else len(h) for i, h in enumerate(headers)]
    def fmt_row(row):
        return "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths))
    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for row in rows:
        print(fmt_row(row))


def print_json(results: list[PriceResult]):
    print(json.dumps([asdict(r) for r in results], default=str, indent=2))


def main():
    args = parse_args()

    if args.setup_amazon:
        import amazon_scraper

        amazon_scraper.setup_amazon_profile()
        return

    if args.update_costplusdrugs_shipping:
        import costplusdrugs_shipping

        record = costplusdrugs_shipping.update_shipping_fee(headless=not args.debug)
        print(
            f"Cost Plus Drugs standard shipping fee: ${record.fee:.2f} "
            f"(checked {record.checked_at} against {record.checked_url})"
        )
        print(f"Written to {Config.COSTPLUSDRUGS_SHIPPING_PATH}")
        return

    if not args.drug or not args.dosage:
        print(
            "Error: --drug and --dosage are required (unless using --setup-amazon or "
            "--update-costplusdrugs-shipping)",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        Config.validate()
    except ValueError as e:
        print(f"Configuration error:\n{e}", file=sys.stderr)
        sys.exit(1)

    sites = [s.strip() for s in args.sites.split(",")] if args.sites else list(Config.ENABLED_SITES)
    unknown = set(sites) - set(Config.ALL_SITES)
    if unknown:
        print(f"Unknown site(s): {', '.join(sorted(unknown))} (valid: {', '.join(Config.ALL_SITES)})", file=sys.stderr)
        sys.exit(2)

    # Reviewed live: this used to have no try/except at all, so an
    # import-time failure in any of the four scraper modules (a missing/
    # mismatched dependency, or a syntax error introduced while one of
    # them is mid-edit) crashed the whole CLI with a raw traceback —
    # inconsistent with gui.py's api_search(), which wraps the identical
    # call in a broad except and degrades to a clean error instead of
    # crashing. Matches this function's own existing style for a
    # startup-time failure (Config.validate() right above) rather than
    # gui.py's JSON-error style, since this is the CLI.
    try:
        _load_scrapers()
    except Exception as e:
        print(f"Error loading scraper modules:\n{e}", file=sys.stderr)
        sys.exit(1)

    zip_code = args.zip_code or Config.DEFAULT_ZIP_CODE or None

    if args.debug:
        # Force visible, sequential runs so terminal output and debug HTML
        # dumps stay attributable to one site at a time.
        original_default = Config.HEADLESS_DEFAULT
        Config.HEADLESS_DEFAULT = False
        try:
            results = gather_results(sites, args.drug, args.formulation, args.dosage, zip_code, args.quantity, sequential=True)
        finally:
            Config.HEADLESS_DEFAULT = original_default
    else:
        results = gather_results(sites, args.drug, args.formulation, args.dosage, zip_code, args.quantity, sequential=False)

    results = filter_out_insurance_required(results)
    results = normalize_quantities(results, args.quantity)
    results = sort_results(results)

    if args.json:
        print_json(results)
    else:
        print_table(results)


if __name__ == "__main__":
    main()
