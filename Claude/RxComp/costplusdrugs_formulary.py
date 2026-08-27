"""
Static formulary lookup for Cost Plus Drugs, backed by the "Team Cuban
Card" downloadable medication list — an .xlsx export of what Cost Plus
Drugs carries (drug name / strength / form). It has no pricing at all,
just eligibility, so this can never replace the live scrape (see
costplusdrugs_scraper.py) — it's a fast pre-check ahead of it. If a
drug/dosage/formulation combo isn't in this list, it's not carried and
there's no point hitting the live site. If it IS in the list, the live
site is still needed for the actual $ number.

Only used when Config.COSTPLUSDRUGS_LOOKUP_MODE == "file"; in "live" mode
(the default) this module isn't touched at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook

from utils import slugify_drug_name

SHEET_NAME = "Team Cuban Card - Medications"


@dataclass(frozen=True)
class FormularyEntry:
    name: str
    strength: str
    form: str


def formulary_path(configured_path: str) -> Path:
    p = Path(configured_path)
    return p if p.is_absolute() else Path(__file__).parent / p


def _normalize_strength(s: str) -> str:
    """'20 MG' -> '20mg', matching the plain '20mg'-style dosage strings
    used everywhere else in this project (no unit-spacing normalization
    needed beyond stripping whitespace, since the file's units already
    read the same as ours once spaces are gone)."""
    return "".join(s.split()).lower() if s else ""


@lru_cache(maxsize=4)
def load_formulary(path_str: str) -> tuple[FormularyEntry, ...]:
    """Parses the workbook once per distinct path per process — cached
    since a single run may look Cost Plus Drugs up more than once (e.g.
    the module's own self-test, or repeated CLI invocations in a shell
    session that reuses the interpreter). Raises FileNotFoundError with
    its normal message if the configured path doesn't exist; the caller
    (get_prices()) turns that into a clean PriceResult error rather than
    letting it propagate."""
    path = formulary_path(path_str)
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[SHEET_NAME]

    entries = []
    for name, strength, form in ws.iter_rows(values_only=True):
        if not (isinstance(name, str) and isinstance(strength, str) and isinstance(form, str)):
            continue  # skips the leading notice/blank rows, which aren't 3 real strings
        entries.append(FormularyEntry(name=name.strip(), strength=strength.strip(), form=form.strip()))
    return tuple(entries)


def find_entry(
    entries: tuple[FormularyEntry, ...], drug_name: str, dosage: str, formulation: str
) -> FormularyEntry | None:
    """Best-effort match against the formulary:
      - drug name: via the same slugify_drug_name() used for URL slugs
        elsewhere — handles the source file's own inconsistent combo-drug
        formatting (e.g. 'Abacavir / Lamivudine' vs 'Lisinopril/Hctz').
      - strength: whitespace/case-insensitive comparison.
      - form: prefix match, case-insensitive — the file's forms are things
        like 'Tablet Delayed Release', so a 'tablet' request should match
        any Tablet* variant, preferring an exact match if one exists.
    Returns None if nothing matches (i.e. not carried)."""
    target_name = slugify_drug_name(drug_name)
    target_strength = _normalize_strength(dosage)
    target_form = formulation.strip().lower()

    by_name = [e for e in entries if slugify_drug_name(e.name) == target_name]
    by_strength = [e for e in by_name if _normalize_strength(e.strength) == target_strength]
    if not by_strength:
        return None

    for e in by_strength:
        if e.form.lower() == target_form:
            return e
    for e in by_strength:
        if e.form.lower().startswith(target_form):
            return e
    return None


if __name__ == "__main__":
    from config import Config

    entries = load_formulary(Config.COSTPLUSDRUGS_FORMULARY_PATH)
    print(f"Loaded {len(entries)} formulary entries from {Config.COSTPLUSDRUGS_FORMULARY_PATH}")

    for drug, dosage, form in [
        ("Lisinopril", "20mg", "tablet"),
        ("Lisinopril", "999mg", "tablet"),  # shouldn't exist
        ("Lisinopril/HCTZ", "20-12.5mg", "tablet"),
    ]:
        entry = find_entry(entries, drug, dosage, form)
        print(f"{drug} {dosage} {form} -> {entry}")
