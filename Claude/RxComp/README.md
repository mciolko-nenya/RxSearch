# RxComp

Personal, on-demand CLI tool that compares prescription drug prices across
GoodRx, SingleCare, Amazon Pharmacy, and Mark Cuban's Cost Plus Drugs.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional: override defaults, see Configuration below

# One-time: log in to Amazon so pricing lookups can reuse the session.
# Opens a real (visible) Chrome window — log in manually, including any
# 2FA/CAPTCHA, then return to the terminal and press Enter.
python main.py --setup-amazon
```

The venv step isn't optional busywork on every machine: a Python install
managed by `uv` (or another PEP 668 "externally managed" distribution)
refuses a bare `pip install` outright rather than risk the system
interpreter, so `python -m venv`/`uv venv` is the actual fix, not just
tidiness. The activation half matters just as much as the creation half —
`.venv` only affects `python`/`pip` in a shell where you've run
`source .venv/bin/activate`; running plain `python main.py` from a fresh
terminal (without activating first) silently falls back to the system
interpreter, which has none of these packages installed, and fails with
`ModuleNotFoundError` on the first dependency it imports (`dotenv`,
`selenium`, whichever loads first). Activate once per terminal session
before running anything below.

Requires a real Chrome browser installed on the system — `webdriver-manager`
resolves the matching `chromedriver` binary automatically, but it doesn't
install Chrome itself.

**Python 3.12+ (confirmed working on 3.14.7):** `undetected-chromedriver`
still imports `distutils.version.LooseVersion` internally, but `distutils`
was removed from the standard library in Python 3.12 — a bare `pip install`
into an environment with no `setuptools` (e.g. a fresh `venv`, which stopped
bundling `setuptools` by default in 3.12) fails with `ModuleNotFoundError:
No module named 'distutils'` the moment any scraper module is imported.
`setuptools` ships its own vendored `distutils` and installs a shim that
satisfies that import, so it's pinned directly in `requirements.txt` rather
than left as an unstated transitive assumption. Confirmed live: a clean
`venv` on 3.14.7 reproduces the crash without `setuptools` installed, and
all 11 project modules import cleanly (plus `main.py --help` runs) once it
is.

Selenium sessions run through `undetected-chromedriver` rather than plain
Selenium (see [How it looks up prices](#how-it-looks-up-prices)) — several
sites now block a stock Selenium session outright (via `navigator.webdriver`
and similar automation tells), regardless of headless/visible mode.

**Apple Silicon Macs:** `undetected-chromedriver`'s stealth patching rewrites
bytes in the `chromedriver` binary, which invalidates its arm64 code
signature — without a fix, macOS's kernel kills the driver on launch
(`Status code was: -9`). `driver_utils.py` re-signs the binary ad-hoc after
patching to work around this; it's automatic and only runs once per
downloaded `chromedriver` version, but if you ever see that `-9` error again
(e.g. after a Chrome version bump), it means this step didn't run — check
that `codesign` is on `PATH` (it ships with Xcode Command Line Tools).

## Usage

```bash
# Basic lookup across all four sites
python main.py --drug lisinopril --dosage 20mg --formulation tablet --zip 10001

# Limit to specific sites
python main.py --drug metformin --dosage 500mg --sites goodrx,costplusdrugs

# Hint a specific pack size/quantity (best-effort — not all sites honor it)
python main.py --drug lisinopril --dosage 20mg --quantity 30

# Machine-readable output
python main.py --drug lisinopril --dosage 20mg --json > prices.json

# Visible browser + verbose debug HTML dumps, one site at a time
python main.py --drug lisinopril --dosage 20mg --debug
```

| Flag | Required | Description |
|---|---|---|
| `--drug` | yes | Drug name, e.g. `lisinopril` |
| `--dosage` | yes | Dosage/strength, e.g. `20mg` |
| `--formulation` | no | Formulation, e.g. tablet/capsule/liquid (default: `tablet`) |
| `--quantity` | no | Requested pack size/quantity, e.g. `30` — a best-effort hint used to select a matching control on GoodRx, SingleCare, and Cost Plus Drugs (see [What quantity is this pricing for?](#what-quantity-is-this-pricing-for)); Amazon has no separate quantity field to select. Wherever selection isn't possible or doesn't match, the price is rescaled to approximate the requested quantity instead (see [Rescaling to an unavailable quantity](#rescaling-to-an-unavailable-quantity)) |
| `--zip` | no | ZIP code for pricing lookups (defaults to `DEFAULT_ZIP_CODE` from `.env`) |
| `--sites` | no | Comma-separated subset of `goodrx,singlecare,amazon,costplusdrugs` (default: `ENABLED_SITES` from `.env`) |
| `--debug` | no | Runs all requested sites' browsers visibly and sequentially (instead of headless/parallel) |
| `--json` | no | Prints raw JSON instead of a table |
| `--setup-amazon` | no | One-time interactive Amazon login into a persistent Chrome profile; ignores all other flags |

(`--setup-amazon` is the only way to do the Amazon login — there's no
equivalent flag on `amazon_scraper.py` itself.)

Each site's lookup runs independently — if one site fails (blocked, page
changed, session expired, etc.) it shows up as a labeled row with a reason
in the `Notes` column instead of stopping the whole comparison. Re-run
`--setup-amazon` if the Amazon row reports a session/login problem.

Every scraper module can also be run standalone for troubleshooting (each
runs a sample `Lisinopril 20mg tablet` lookup):

```bash
python goodrx_scraper.py
python singlecare_scraper.py
python costplusdrugs_scraper.py
python amazon_scraper.py       # prints a setup reminder instead if --setup-amazon hasn't been run yet
```

`config.py`, `models.py`, `utils.py`, and `costplusdrugs_formulary.py` are
also independently runnable and each print a small self-test when run
directly.

Failed lookups write `page_*.html` snapshots to the project root for
debugging — these are gitignored. GoodRx and Amazon Pharmacy additionally
save one snapshot on *every* run (success or failure), since their bot
detection makes it useful to see exactly what page was loaded even when a
lookup appears to work.

### Error handling: consistent source names, no raw crashes on a broken import

Two small robustness gaps found in a code review, both in `main.py`'s
`run_site()`/`main()`:

- `run_site()`'s belt-and-suspenders fallback — for a truly unexpected
  exception a scraper's own error handling doesn't catch (a ChromeDriver
  install failure, say) — built its `PriceResult` with `source=site`, the
  raw lowercase `--sites`/`SCRAPERS` key ("goodrx"), not the display name
  ("GoodRx") every other row for that site, every scraper's own error
  paths, and `models.py`'s own documented value set for
  `PriceResult.source` all use. Precisely the case meant to be the most
  defensively handled turned out to be the one inconsistent row. Fixed
  with a `SOURCE_NAMES` mapping populated from each scraper's own
  `SOURCE_NAME` alongside `SCRAPERS` — single source of truth, so it
  can't drift from what every other row already reports.
- `main()` called `_load_scrapers()` with no surrounding `try`/`except`
  at all, so an import-time failure in any of the four scraper modules
  (a missing/mismatched dependency, or a syntax error introduced while
  one of them is mid-edit) crashed the whole CLI with a raw traceback —
  inconsistent with `gui.py`'s `api_search()`, which already wraps the
  identical call in a broad `except` and degrades to a clean error
  instead. Fixed to match: a clean `Error loading scraper modules: ...`
  message and `sys.exit(1)`, the same style already used for a
  `Config.validate()` failure right above it.

Both verified live: simulated an unexpected scraper exception and
confirmed the fallback row now reports `"GoodRx"`; simulated a broken
import and confirmed `main()` now exits cleanly with a readable message
instead of an unhandled traceback.

## GUI

```bash
python gui.py
```

Starts a small local web server and opens `http://127.0.0.1:5057` in your
default browser automatically. The page has the same fields as the CLI
flags (Drug, Dosage, Formulation, Quantity, ZIP, which sites, Debug), a
"Search" button, and a results table
(Source/Price/Label/Pharmacy/Quantity/Notes — the same columns the CLI
prints, with error rows highlighted in red). A "Set up Amazon login…"
button runs the same flow as `--setup-amazon` without needing the CLI at
all.

**First built as a native Tkinter window instead — reported live: nothing
rendered at all**, alongside `DEPRECATION WARNING: The system version of
Tk is deprecated`. Confirmed live: `tkinter.TkVersion` on this machine
reports `8.5.9` — Apple's own long-deprecated system Tk build, exactly
what that warning was about, and known to render blank on modern macOS.
There's also no Homebrew installed here to get a modern Tk instead, which
would otherwise be the standard fix. Rather than depend on installing new
system software just to get a working window, this became a local
browser-based UI instead — sidesteps the broken system Tk entirely, and
this project already depends on a real browser for every scraper anyway,
so requiring one here adds nothing new. Verified this actually works,
not just compiles: started the server, fetched the rendered page,
exercised the API's validation paths (missing drug/dosage, no sites
selected), and ran one real end-to-end search through it — Cost Plus
Drugs' lisinopril lookup correctly returned `$10.80` ($5.55 + the $5.25
shipping fee folded in per [Cost Plus Drugs: search, then
strength/quantity selection](#cost-plus-drugs-search-then-strengthquantity-selection)),
labeled correctly, through the actual HTTP layer rather than by calling
the underlying Python directly.

**Requested directly: get rid of the need to switch to a terminal and
press Enter to confirm a CAPTCHA was solved.** Every scraper's
interactive-challenge handling (GoodRx's/SingleCare's bot-checks,
Amazon's login) used to call a direct, blocking `input()` — the only
option when the only front end was a terminal. That's now indirected
through a new `driver_utils.ChallengeNotifier` abstraction: scrapers call
`wait_for_challenge_confirmation(message)` instead of `input(message)`
directly, and *which* notifier actually handles that call is swappable.
`main.py`'s CLI never swaps it, so terminal `input()` is still exactly
what happens there — nothing about the CLI changed. `gui.py` installs a
`GuiChallengeNotifier` instead: the page polls `/api/challenge` roughly
once a second, and shows an in-page modal with the challenge's message
and a "Done" button whenever one is pending — clicking it calls
`/api/challenge/confirm`, which is what actually unblocks the waiting
scraper thread server-side, in place of pressing Enter in a terminal.
Neither this nor the original `input()` needs to distinguish "solved"
from "skipped" — both just unblock a wait, and every caller already
re-checks the live page afterward to see whether the challenge is
actually gone.

The still-separate, *visible* Chrome window a challenge opens (so there's
something to actually solve) is unaffected by any of this — only the
"tell it you're done" step moved into the browser tab this GUI itself
runs in. Verified end-to-end within a single process (matching how the
real deployment actually behaves — Flask's threaded dev server spawns a
worker thread per request in the *same* process, and that's exactly what
a real search request blocked inside `wait_for_challenge_confirmation()`
looks like too): started the server, spawned a thread that calls
`wait_for_challenge_confirmation()` directly (standing in for a
scraper), confirmed `/api/challenge` correctly reports it pending with
the right message while that thread is blocked, called
`/api/challenge/confirm`, and confirmed the waiting thread actually
unblocks and `/api/challenge` goes back to not-pending afterward.

This is also *why* the Flask dev server has to run threaded
(`app.run(threaded=True)`) now, not just optionally: a search request can
sit blocked inside `wait_for_challenge_confirmation()` for as long as a
challenge modal is up, and the page still needs to poll `/api/challenge`
and hit `/api/challenge/confirm` *while that's happening* — an
unthreaded dev server could never serve those, and the modal's "Done"
button would have no way to actually reach the waiting thread.
`_run_lock` still separately serializes the searches/Amazon-setup
themselves (so two can't both try to drive Selenium at once) — a
narrower guarantee than the request-level threading this now needs, not
a replacement for it.

**Requested directly: the modal above still didn't fully solve the
underlying UX problem — with sites run concurrently (`main.py`'s own
non-`--debug` default), a challenge on one site's thread and a challenge
on another's can both land at roughly the same time.**
`INTERACTIVE_CHALLENGE_LOCK` already guarantees only one modal is ever
shown at once, so nothing was actually broken, but from the person
answering them it reads as a confusing, unpredictable queue — which
site's window is this challenge even for, and how many more are coming?
Fixed by always passing `sequential=True` to `gather_results()` from the
GUI now, regardless of the Debug checkbox — one site finishes completely
(challenges and all) before the next one's own challenges can even
occur, so the queue becomes predictable: at most one site's worth of
challenges pending at any moment, in the same order the sites are
listed. Deliberately decoupled from Debug, which previously controlled
both visibility *and* sequencing together (mirroring `--debug`'s combined
CLI meaning): a challenge can force a preemptive switch to a visible
window regardless of headless mode (see GoodRx's ZIP/dosage-edit
sections below), so sequencing needed to be unconditional, not tied to
whether the browser itself is visible. Debug's checkbox label was
updated to drop "one site at a time," since that's no longer something
it toggles. Verified live: instrumented two real scrapers
(`costplusdrugs`/`amazon`) to record their own start/end timestamps and
confirmed the first one's `end` timestamp was ≤ the second one's `start`
— no overlap, not just no visible symptom of one.

**Found in a later code review: two real bugs in `GuiChallengeNotifier`
itself, both fixed together.**

1. `self._event.wait()` had no timeout — and it's called from inside the
   same request holding `_run_lock` for its entire duration. So if a
   user started a search, a challenge modal appeared, and they then
   closed the tab (or their laptop slept, or they just walked away)
   instead of clicking "Done," the request thread stayed parked there
   *forever*: `_run_lock` was never released, and every later
   `/api/search`/`/api/setup-amazon` call got a permanent 409 until the
   whole `gui.py` process was killed and restarted — a liveness bug, not
   just a slow recovery. Fixed with a bounded `CHALLENGE_TIMEOUT_SECONDS`
   (30 minutes): `wait()` now gives up on its own after that long,
   exactly like clicking "Done" without having solved anything — the
   existing "skip" semantics a terminal user already gets from pressing
   Enter without solving. The caller re-checks the live page either way
   and reports a caveat/error if the challenge genuinely wasn't
   resolved.
2. There was no per-challenge identifier — one shared message/event pair
   for the notifier's whole lifetime, on the theory that
   `INTERACTIVE_CHALLENGE_LOCK` already guarantees only one challenge is
   ever pending process-wide. That's true, but says nothing about which
   *browser tab* is aware of *which* challenge: with two tabs open (an
   ordinary thing to do — e.g. a leftover tab from an earlier session), a
   stale "Done" click in a tab that hadn't re-polled since challenge A
   was replaced by a later challenge B could still reach the server and
   unconditionally resolve whatever's pending *now* — silently marking B
   "solved" when a human never looked at it. Fixed by tagging every
   challenge with a monotonically increasing id, returned by
   `/api/challenge` and required by `/api/challenge/confirm`; a
   stale/mismatched confirm is now rejected instead of silently
   accepted. The page's own polling JS was also fixed to compare that id
   (not just a pending/not-pending boolean), so it refreshes the
   displayed message on a pending-to-*different*-pending transition too,
   not only on a not-pending-to-pending one.

Both verified live, in a single process (so the notifier's shared state
is genuinely shared, matching real deployment): confirmed a correct-id
confirm unblocks the waiting thread, confirmed a stale/wrong-id confirm
is rejected while the real pending challenge keeps waiting, and confirmed
an abandoned wait times out on its own instead of hanging.

Presentation only, same as before: the `/api/search` route calls
`main.py`'s own `gather_results()` / `filter_out_insurance_required()` /
`normalize_quantities()` / `sort_results()` directly rather than
reimplementing any of that logic, so the GUI and the CLI always behave
identically.

## API

`gui.py` also serves a plain JSON API, separate from the browser page and
the page's own internal `/api/search` (a POST-with-JSON-body endpoint
that's this GUI's own implementation detail — its exact shape is free to
change alongside the page, since nothing outside it depends on that
contract). The API below is the stable, documented one meant for
external callers — same underlying pipeline either way, since both just
call `main.py`'s own functions.

```bash
python gui.py   # same server, same command, as before
```

**`GET /api/v1/prices`** — query parameters:

| Param | Required | Meaning |
|---|---|---|
| `drug` | yes | Drug name, e.g. `lisinopril` |
| `dosage` | yes | Dosage/strength, e.g. `20mg` |
| `formulation` | no (default `tablet`) | e.g. `tablet`, `capsule`, `liquid` |
| `quantity` | no | Requested pack size, e.g. `30` — same rescaling/exact-quote behavior as `--quantity` on the CLI (see [What quantity is this pricing for?](#what-quantity-is-this-pricing-for)) |
| `zip` | no | ZIP code — ignored by Cost Plus Drugs, used by GoodRx/SingleCare |
| `sites` | no (default: this deployment's `ENABLED_SITES`) | Comma-separated subset, e.g. `sites=costplusdrugs` or `sites=goodrx,amazon`. Validated against `ENABLED_SITES`, not the full four — a site this deployment has disabled (e.g. a Cost-Plus-Drugs-only Render deployment with no Chrome installed at all) is rejected here even if requested explicitly, not just hidden from the GUI's checkboxes |

```bash
curl "http://localhost:5057/api/v1/prices?drug=lisinopril&dosage=20mg&quantity=30&sites=costplusdrugs"
```

Returns `{"results": [...]}`, one entry per `PriceResult` (same fields
the CLI's `--json` output and `/api/search` already use — `source`,
`price`, `price_label`, `quantity`, `url`, `error`, etc.). A request-level
problem (missing `drug`/`dosage`, an unknown/disabled site) is a `400`
with an `{"error": ...}` body; a per-site failure (blocked, no match,
bot check) is a normal `200` with that site's own `PriceResult.error`
set instead — the same "never raise, report a failed row" contract every
scraper's `get_prices()` already follows, extended to the API's own
request-level validation too.

**`GET /api/v1/sites`** — returns `{"sites": [...]}`, this deployment's
actual `ENABLED_SITES`. Lets a caller discover what's queryable without
hard-coding site names or guessing from a Render deployment's config.

**CAPTCHAs are force-disabled for API calls, not just left to time out.**
GoodRx and SingleCare can show an interactive CAPTCHA that the GUI's own
`/api/search` handles by blocking the request and showing a human a
browser modal to solve it in (see above) — an API caller has neither a
modal nor a human watching. Left alone, a challenged site would just
block an `/api/v1/prices` request for the full 30-minute challenge
timeout before finally reporting failure. Instead, `_captcha_disabled_for_api()`
temporarily forces `GOODRX_INTERACTIVE_CAPTCHA`/`SINGLECARE_INTERACTIVE_CAPTCHA`
off for the duration of the request (restored afterward — these are
shared `Config` class attributes, not per-request state, so a
concurrent GUI search must get its real interactive behavior back
unaffected) — a challenged site fails fast with a normal error result
instead of hanging. Verified directly: the flags flip off inside the
context manager and are restored afterward, including when the
lookup itself raises.

Only the three Selenium-based sites (`goodrx`/`singlecare`/`amazon`)
take the same `_run_lock` the GUI's searches already use — it exists to
stop two browser sessions launching at once, so a `sites=costplusdrugs`
request skips it entirely and never queues behind an unrelated
GoodRx/SingleCare/Amazon lookup. Verified live: `sites=costplusdrugs`
returns immediately with a real price; `sites=notarealsite` and a
request missing `drug`/`dosage` both return the expected `400`.

## Configuration

All settings live in `.env` (see `.env.example`); nothing is hardcoded and
there are no stored Amazon credentials.

| Variable | Default | Purpose |
|---|---|---|
| `DEFAULT_ZIP_CODE` | *(empty)* | Used when `--zip` isn't passed; must be 5 digits if set |
| `HEADLESS_DEFAULT` | `true` | Run Selenium browsers headless; `--debug` overrides this to `false` |
| `REQUEST_TIMEOUT_SECONDS` | `15` | Timeout for `costplusdrugs_scraper.py`'s plain HTTP calls to Cost Plus Drugs' own public API (see [switched to their own public API](#cost-plus-drugs-switched-to-their-own-public-api) below). Was unused for a while after SingleCare's and Cost Plus Drugs' original plain-fetch paths were both replaced by Selenium — kept validated "in case a plain-fetch path is reintroduced later," which is exactly what happened for Cost Plus Drugs. SingleCare is still Selenium-only |
| `SELENIUM_PAGE_LOAD_TIMEOUT` | `30` | Selenium page-load timeout |
| `SELENIUM_WAIT_TIMEOUT` | `15` | Selenium explicit-wait timeout for locating elements |
| `AMAZON_CHROME_PROFILE_DIR` | `.chrome-profile-amazon` | Where the persistent, logged-in Amazon Chrome profile is stored |
| `ENABLED_SITES` | `goodrx,singlecare,amazon,costplusdrugs` | Default site list when `--sites` isn't passed |
| `COSTPLUSDRUGS_LOOKUP_MODE` | `live` | `live` always queries Cost Plus Drugs' public API; `file` checks the static formulary spreadsheet first (see below) and only queries the API if the drug is actually carried |
| `COSTPLUSDRUGS_FORMULARY_PATH` | `TeamCubanCard_DownloadableMedicationList_08.06.2026.xlsx` | Path to the formulary spreadsheet; only read when `COSTPLUSDRUGS_LOOKUP_MODE=file` |
| `GOODRX_INTERACTIVE_CAPTCHA` | `true` | When GoodRx shows a bot-check, opens a visible Chrome window and waits at the terminal for you to solve it (see below). Set `false` for unattended runs — GoodRx will just report the block as an error instead of prompting |
| `SINGLECARE_INTERACTIVE_CAPTCHA` | `true` | Same idea, for SingleCare's DataDome challenge |

Run `python config.py` to validate your current `.env` and print the
resolved values.

## Project structure

```
main.py                  CLI entry point: arg parsing, dispatch, table/JSON output
gui.py                     Local browser-based GUI front end (Flask) over main.py's own pipeline (see GUI, above)
config.py                .env-backed configuration + validation
models.py                PriceResult dataclass shared by every scraper
driver_utils.py            Shared undetected-chromedriver setup + SeleniumScraperBase
utils.py                   Drug-name/slug normalization, price-from-text extraction
goodrx_scraper.py          GoodRx — Selenium only, resolves the drug page via GoodRx's own search, prompts interactively to solve a CAPTCHA when challenged
singlecare_scraper.py      SingleCare — Selenium only, resolves the drug page via SingleCare's own search, prompts interactively to solve a CAPTCHA when challenged
costplusdrugs_scraper.py   Cost Plus Drugs — calls their own public API directly (no browser at all; see below), or static-formulary pre-check first when COSTPLUSDRUGS_LOOKUP_MODE=file
costplusdrugs_formulary.py Static Team Cuban Card formulary loader/matcher, used only in COSTPLUSDRUGS_LOOKUP_MODE=file
amazon_scraper.py          Amazon Pharmacy — Selenium only, reuses a persistent logged-in profile
requirements.txt           Python dependencies
.env.example               Template for local .env
TeamCubanCard_Downloadable...xlsx  Static Cost Plus Drugs formulary export (drug/strength/form — no pricing)
```

## How it looks up prices

| Site | Approach |
|---|---|
| Cost Plus Drugs | Direct HTTP calls to Cost Plus Drugs' own free public API — no browser at all (see [switched to their own public API](#cost-plus-drugs-switched-to-their-own-public-api) below) |
| SingleCare | Selenium only — drives the site's own real search autocomplete to resolve the correct drug page (see [drug name → URL, via search](#singlecare-drug-name--url-via-search) below), then its dosage/quantity/ZIP controls; interactively prompts to solve a CAPTCHA when challenged (see below) |
| GoodRx | Selenium only — plain fetches are blocked by bot detection; also drives the site's own search to resolve the drug page (see [drug name → URL, via search](#goodrx-drug-name--url-via-search) below); interactively prompts to solve a CAPTCHA when challenged (see below) |
| Amazon Pharmacy | Selenium only, reusing the persistent logged-in Chrome profile from `--setup-amazon` — pricing isn't visible without an account. A plain `amazon.com` product search, not a dedicated pharmacy endpoint (see below) |

GoodRx, SingleCare, and Amazon Pharmacy remain Selenium-only: none of the
three has a self-serve API for a personal project (see [switched to
their own public API](#cost-plus-drugs-switched-to-their-own-public-api)
for what was actually checked), so their pricing has no path but the
rendered page. Cost Plus Drugs and SingleCare both originally had a
plain-fetch fast path for that page (their pricing was confirmed
publicly readable during planning research, no login or JS needed), but
both sites added bot protection (Cloudflare Bot Management and DataDome
respectively) that blocks a plain `requests` fetch of the *rendered
page* outright — confirmed live, every path on either site, including
each one's own homepage, now returns the same block regardless of what
it's asked for. This is unrelated to Cost Plus Drugs' now-in-use public
API, a separate, deliberately-open endpoint with no bot protection on
it at all — the blocked plain-fetch path refers to their normal
consumer-facing website, back when this project still scraped it, and
still describes SingleCare's site today. Selenium runs through
`undetected-chromedriver` everywhere it's still used, not plain
Selenium: without the stealth layer, a stock Selenium session gets
blocked just as easily as a plain fetch. Confirmed live at the time:
even with `navigator.webdriver` spoofed and the obvious automation-
controlled Chrome flags stripped, Cost Plus Drugs' Cloudflare still
blocked a plain Selenium session's request to *its own internal,
undocumented* frontend price-data endpoint (not the public API used
today) — only `undetected-chromedriver`'s deeper patching (of the
`chromedriver` binary itself) got through.

None of this is guaranteed to keep working — these are live anti-bot systems
that can (and did, mid-development) change their detection posture at any
time, independent of anything in this code. Selector lists in each scraper
are also best-effort and may need tuning if a site's markup changes. Treat
both as expected maintenance, not bugs.

### Chromedriver setup: a concurrency race across sites

Found in a code review, not a live report: `driver_utils.create_chrome_driver()`
resolves and patches the chromedriver binary
(`ChromeDriverManager().install()` + patch/codesign for undetected-
chromedriver's stealth — see above) on every single call, with no lock at
all. The patch step itself is a check-then-act
(`is_binary_patched()` → `patch_exe()` → `codesign`), and
`ChromeDriverManager().install()` resolves to the *same* on-disk binary
path for every caller on one machine. `main.py`'s default (non-`--debug`)
mode runs every requested site concurrently via a `ThreadPoolExecutor`,
each creating its own driver through this same unsynchronized path — so
on a cold cache (first run, or right after a chromedriver update), two
threads could race to patch/sign the same file, or one thread's
`is_binary_patched()` check could observe the binary as already patched
by *another* thread's in-progress patch, before that thread's `codesign`
has actually run — handing a patched-but-unsigned binary straight to
`uc.Chrome()`. That's exactly the state this module's own docstring
already said gets SIGKILLed (`-9`) by macOS AMFI on Apple Silicon — an
intermittent, misleading per-site crash that looks unrelated to anything
about that site's actual page.

Fixed by resolving the path once per process instead of once per driver:
a module-level lock plus a cached path, so the first call does the real
patch/sign work and every later call (including the mid-lookup
headless-to-visible driver recreations GoodRx's ZIP/dosage-edit flows do
— see below) just reads the cached result. Verified live, not just
reasoned through: fired 6 concurrent threads at the resolver with the
underlying patch/sign step instrumented to count its own invocations —
confirmed it ran exactly once, and all 6 threads got back the same path.

### Clicking through overlays: a shared helper

Also found in the same review: `costplusdrugs_scraper.py` had
independently discovered and pasted in the same "try a native click,
fall back to a JS-dispatched one" fix at three separate call sites — see
[Cost Plus Drugs: search, then strength/quantity
selection](#cost-plus-drugs-search-then-strengthquantity-selection) for
why that fix exists at all (a site-wide cookie-consent backdrop that
intercepts native clicks). `singlecare_scraper.py` had no such fallback
anywhere — every click there was a bare native `.click()` — which would
silently break the same way if its page ever grows a similar overlay (a
real, shared risk: third-party consent widgets aren't particular to Cost
Plus Drugs). Centralized into one `driver_utils.robust_click(driver,
element)`, now used by both files (all of SingleCare's click sites, and
all of Cost Plus Drugs' in place of its three separate copies) — a future
site doesn't have to rediscover and hand-write this a fourth time. Cost
Plus Drugs re-verified live end-to-end after the refactor (unchanged
result); SingleCare's own click sites are confirmed correct by
compile/import only, not a live run — SingleCare's bot detection was
blocking every live attempt when this was written.

### Two more shared helpers, found in a quality review of `amazon_scraper.py`

Not bug fixes — a `/simplify`-style pass over `amazon_scraper.py` alone
(reuse, simplification, efficiency, altitude, no correctness hunting)
turned up two more functions duplicated between it and
`goodrx_scraper.py`, on top of the click-fallback promotion above:

- **`find_first_present(driver, selectors)`** — "try each CSS selector
  in order, return the first that exists" existed as `goodrx_scraper.py`'s
  own `_find_first_present()` (for its "Edit prescription" modal's known
  DOM variants) and was independently re-written in
  `amazon_scraper.py`'s `_looks_like_signin_page()` (over
  `SIGNIN_INDICATOR_SELECTORS`, functionally the same existence check).
  Promoted to `driver_utils.py`; both files call the same function now.
- **`extract_price_after_label(text, label, window, raw_trim)`** —
  "search rendered text for a label, read whatever `$` amount follows it
  within a window" was independently written twice: `goodrx_scraper.py`'s
  `_extract_standard_price()` (its own docstring already said as much)
  and `amazon_scraper.py`'s `_extract_price_after()`, differing only in
  which label they search for and their window/snippet sizes (100/60 vs.
  60/40). Promoted to `utils.py`, parametrized so each call site keeps
  its own existing sizes — neither's behavior changed.

Also cleaned up within `amazon_scraper.py` itself: a dead
`if not matching: matching = rows` fallback that could never actually
execute (the branch above it already handles the only case that would
reach it), an unconditional full-page debug dump that ran on every
successful lookup for no reason (every failure path already saves its
own), and the repeated `PriceResult(...)`/per-card-field-extraction
boilerplate collapsed into small local helpers
(`_error_result()`/`_safe_text()`/`_safe_attr()`). All of this verified
by re-running the same unit-level regression checks used throughout this
project (the exact substring-collision/formulation test cases from
earlier), plus one live Cost Plus Drugs lookup as a broader sanity check
— all unchanged.

## Drug name → URL: via each site's own search, not a guessed slug

Reported live, with a concrete failing case: `--drug atorvastatin` against
SingleCare's old URL-building (`singlecare.com/prescription/atorvastatin`)
returned nothing, because that page doesn't exist — the real one is
`singlecare.com/prescription/atorvastatin-calcium`. There's no
algorithmic way to know that an arbitrary drug's real page needs a
salt-form suffix (or any other qualifier) without asking the site itself,
so all three sites that used to build a URL directly from the drug name
now drive that site's own real search box instead, and follow wherever it
actually leads — the same page a real user would land on:

| Site | Confirmed live? | Notes |
|---|---|---|
| SingleCare | Yes, end-to-end | See [SingleCare: drug name → URL, via search](#singlecare-drug-name--url-via-search) |
| GoodRx | Partially — see below | See [GoodRx: drug name → URL, via search](#goodrx-drug-name--url-via-search) |
| Cost Plus Drugs | Yes, end-to-end | See [Cost Plus Drugs: search, then strength/quantity selection](#cost-plus-drugs-search-then-strengthquantity-selection) |
| Amazon Pharmacy | Already was | Already a real `amazon.com` product search, not a per-drug URL slug — see [Amazon Pharmacy: wrong search URL entirely](#amazon-pharmacy-wrong-search-url-entirely) |

GoodRx's is explicitly flagged "partially confirmed": its own bot
detection blocked every attempt to inspect the real autocomplete dropdown
live while building this (a "Press & Hold" challenge from just typing
into the search box, then a full-page block on every later attempt,
including plain direct navigation — most likely a session-level
PerimeterX flag from the repeated automated-looking attempts, not
something specific to searching). Built defensively instead of guessed
blindly: it tries a short list of plausible suggestion selectors, falls
back to submitting the search box directly if none match, and either way
validates whatever URL results against GoodRx's own *confirmed* real
per-drug URL shape (a single path segment — extensively confirmed live
elsewhere in this document) before trusting it.

## What quantity is this pricing for?

There is no single, uniform quantity across sites — each one shows
whatever pack size it defaults to, which varies by drug and by site, and
until this was asked about, three of the four sites never reported it
anywhere in the output at all. Current behavior:

| Site | Without `--quantity` | With `--quantity N` |
|---|---|---|
| GoodRx | Reports whatever quantity the page defaults to (e.g. "30 tablets") in the `Quantity` column | Actually selects it — see below; if that can't be confirmed, the price is rescaled to approximate it instead (see [Rescaling to an unavailable quantity](#rescaling-to-an-unavailable-quantity)) |
| SingleCare | Reports the page's default (e.g. "90 count") | Actually selects it — confirmed live, changes listed prices; same rescaling fallback if selection can't be confirmed |
| Cost Plus Drugs | Reports the quantity its displayed price is explicitly stated for (e.g. "30 count") — parsed from the page's own "A 30 count supply of ... will cost:" sentence | Actually selects it — confirmed live, its own on-page "quantity-selection-{count}" button live-updates the price; same rescaling fallback if no matching button exists (see [Cost Plus Drugs: search, then strength/quantity selection](#cost-plus-drugs-search-then-strengthquantity-selection)) |
| Amazon Pharmacy | Reports whatever the matched search result states (e.g. "Tablet · 20mg · 30 days supply") | Not applicable to select — quantity isn't a separate field there, each strength is its own listing — but its price is rescaled the same way as the others' when it doesn't match |

**GoodRx and SingleCare** previously accepted `--quantity` as a parameter
but, on closer investigation, neither one actually used it anywhere in
their code — confirmed live for both. Worse, on GoodRx specifically,
dosage selection had the identical bug: the code searched for `<select>`
elements that never existed anywhere on the page at all (dosage/quantity
only exist inside an "Edit prescription" modal, never opened by the old
code), so a requested dosage was silently never applied either, only ever
matched by coincidence against the page's own default. Both are fixed now
— GoodRx opens that modal and uses its real
`#configuration-editor-dropdown-dosage`/`-quantity` `<select>` elements;
SingleCare clicks its real custom `div[role="listbox"]` widgets (also not
native `<select>` elements, which is why an even earlier version of
SingleCare's own dosage-selection code could never have worked either —
see [SingleCare: real row extraction and ZIP code](#singlecare-real-row-extraction-and-zip-code)
for the pattern this repeats). Both now re-check afterward and report a
caveat if the requested dosage/quantity couldn't be confirmed, rather than
silently returning a price for whatever the page happened to default to.

**Cost Plus Drugs** originally only *reported* the quantity its price
already assumed (parsed from the page's own explanatory sentence, since
it isn't fixed at 30 for every drug), with no way to actually request a
different one. It now also *selects* dosage and quantity directly, via
the same real on-page buttons used to resolve the correct drug page in
the first place — see [Cost Plus Drugs: search, then strength/quantity
selection](#cost-plus-drugs-search-then-strengthquantity-selection). If
no button matches the requested dosage, that's flagged as a caveat rather
than silently substituting the page's default; quantity falls back to
the [rescaling](#rescaling-to-an-unavailable-quantity) estimate in the
same case.

**Amazon Pharmacy** has no separate quantity selector to act on — see
[Amazon Pharmacy: wrong search URL entirely](#amazon-pharmacy-wrong-search-url-entirely):
each strength shows up as its own distinct search result, already
carrying its own day-supply/quantity in its listing text.

### Rescaling to an unavailable quantity

Requested via feedback, with a concrete example: run with `--quantity 90`
against a site that only has a 30-day-supply price, and instead of just
comparing that 30-day price against every other site's (possibly
different-quantity) price as if they meant the same thing, it should be
recalculated to approximate 90 — multiplying by (requested ÷ actual), 3
in that example. `main.py`'s `normalize_quantities()` does exactly this,
once, centrally, after all four sites' results come back — not
duplicated per-scraper, since it applies the same way regardless of
*why* a given result's quantity doesn't match (no selector for it at all,
like Amazon; or a selector that exists but couldn't be confirmed, like a
GoodRx/SingleCare/Cost Plus Drugs selection that hit its own caveat).

This is explicitly labeled as an estimate, not presented as if the site
had actually quoted the requested quantity — both in the `Quantity`
column (`"90 (est. from 30)"`) and appended to `price_label`
(`"... — estimated for qty 90: $5.90 for 30 × 90/30 — not an actual quote
for this quantity"`). Real pharmacy pricing usually isn't perfectly
linear: Cost Plus Drugs' own price breakdown (see
[Cost Plus Drugs: stable-price extraction](#cost-plus-drugs-stable-price-extraction))
includes a flat $5 pharmacy fee on top of per-unit manufacturing cost —
that fee doesn't triple just because the quantity does, so a 3×
rescaling of a 30-count price will *overstate* the real 90-count price
by roughly that fixed fee's difference. Still a more useful starting
point than silently comparing prices for different quantities as if they
were equivalent, which is what happened before this existed — but not a
substitute for an actual site-quoted price at that quantity, and not
presented as one.

Only triggers when both the requested quantity and the result's actual
quantity can be confidently parsed as a plain count (via a regex
anchored to a unit word — "tablets"/"count"/"days supply" — so a dosage
number elsewhere in the same string, e.g. Amazon's "20mg", is never
mistaken for the quantity). Left untouched otherwise, and left untouched
entirely for any result that already has an `error` set or already
matches the requested quantity exactly.

### Results that require insurance are filtered out

Requested: any result that needs actual insurance coverage to get — not
a discount club or free signup, real insurance — is dropped from the
output entirely. Right now that's exactly one row: Amazon Pharmacy's
"Average insurance price" (see [Amazon Pharmacy: wrong search URL
entirely](#amazon-pharmacy-wrong-search-url-entirely)) — itself already
labeled as an estimate, not even a real quote, since Amazon states the
exact price is only set once you transfer the prescription.
`main.py`'s `filter_out_insurance_required()` drops it via a structured
`PriceResult.requires_insurance` flag, set at the point each scraper
builds that specific result, rather than by checking `price_label` for
the word "insurance" — both Cost Plus Drugs' and Amazon's own *cash*
price labels already contain that word too ("cash price (no
insurance)"), so a naive text search would have filtered out exactly the
no-strings-attached prices this is meant to keep, confirmed by testing
against exactly that case before considering this done.

GoodRx's Companion price and SingleCare's Member Bonus price are left
alone — neither is insurance. Both are opt-in discount programs (one
paid, one free) that anyone can sign up for regardless of whether they
have insurance at all, which is exactly why this project shows both the
with- and without-membership price for each rather than treating them
like insurance and hiding one.

### Matching dosage, quantity, and form: a substring-collision bug shared across four files

Found in a code review: every "does this requested value match what's on
the page/button" check in this project (`goodrx_scraper.py`'s/
`singlecare_scraper.py`'s `_loose_matches()`, `costplusdrugs_scraper.py`'s
`_find_and_click_variant_button()`, `amazon_scraper.py`'s
`_matches_dosage()`) independently did the same normalize-and-check —
strip everything but letters/digits, lowercase, then a **bidirectional
plain substring test**: `a in b or b in a`. That's unanchored. Requested
dosage "20mg" is a literal substring of a page/button showing "120mg";
requested quantity "90" is a literal substring of "190". Concretely,
real strengths for the same drug can collide exactly this way —
levothyroxine carries both 25mcg and 125mcg. If a page defaulted to
125mcg and the user requested 25mcg, the old check returned a false
"already matches," so the correction step never even ran and no caveat
was produced; on Cost Plus Drugs' button-matching version specifically,
it could click the *wrong* button outright, with nothing downstream
re-checking which one actually got selected.

Fixed with one shared `utils.token_matches()`: when both sides' leading
token is a number, it requires the numbers to be *equal*, not merely one
containing the other, while still tolerating a differing trailing unit
word ("mg" vs none, "count" vs "tablets") and still substring-matching
non-numeric values like a form name (no confirmed bug there, so no
reason to tighten that case and risk a new regression). Verified against
the exact 25mcg/125mcg pair, plus the equivalent quantity collision
("30" vs "130"), and against every previously-passing case (exact
matches, unit-word differences, form matching) to confirm nothing broke.

Worth being honest about: this fix needed a second pass before shipping.
Amazon's `_matches_dosage()` passes a whole compound string
("Tablet · 120mg · 30 days supply") into `token_matches()`, not a bare
"120mg" — and the dosage number there is the *middle* of three
"·"-separated segments, not the leading token `token_matches()` checks
for. A test built specifically to catch the original bug (`"20mg"` vs
`"Tablet · 120mg · 30 days supply"`) caught this immediately: it still
returned a false match, for the same reason as before, just one layer
removed. The actual fix for Amazon extracts just the dosage segment
first, then compares only that — the same way `_matches_formulation()`
(see below) already correctly extracts its own leading segment rather
than matching against the whole compound string.

### Formulation: is this actually the requested form?

Found in the same review: `--formulation` was accepted as a parameter by
every site, but until now only Cost Plus Drugs actually verified or
selected it against the live page (see [Cost Plus Drugs: search, then
strength/quantity
selection](#cost-plus-drugs-search-then-strengthquantity-selection)) —
GoodRx, SingleCare, and Amazon all echoed the requested value straight
into `PriceResult.formulation` as if it had been confirmed, the same
"silently wrong data" gap already fixed for dosage/drug-name/ZIP
elsewhere in this project, just never applied to formulation.

- **SingleCare** gets real selection, not just a caveat: its page
  genuinely has a "Select form" custom-listbox widget, the exact same
  kind already confirmed live for dosage/quantity (see [SingleCare:
  dosage/quantity were never real `<select>` elements
  either](#singlecare-dosagequantity-were-never-real-select-elements-either))
  — so formulation is now selected the same way, run *first*, before
  dosage/quantity/ZIP, on the theory that a form change is the most
  "upstream" of the four and most plausibly resets which dosages/
  quantities are even available (the same direction of risk already
  documented there for dosage/quantity vs. ZIP). Not independently
  re-confirmed live end-to-end — SingleCare's bot detection was blocking
  every live attempt when this was written — but it's the same
  click-a-listbox-option mechanism already verified for dosage/quantity,
  not a new guess about page structure.
- **Amazon** gets a real filter: its already-scraped `strength_text`
  (e.g. "Tablet · 20mg · 30 days supply") already carries the form as its
  own leading segment — no new selector needed. Rows whose form doesn't
  match are excluded the same way dosage mismatches are, and if a
  formulation was requested and nothing matches, that's a clear error
  listing the forms that *were* found rather than silently keeping a
  wrong-form row.
- **GoodRx** gets a caveat, not a correction — confirmed live, its "Edit
  prescription" modal has no formulation control at all, so there's
  nothing to select. The regex that already parses dosage/quantity from
  the modal's summary text was widened to also capture whatever unit word
  follows the count (previously hardcoded to `tablets?`), and that
  captured word is compared against the requested formulation — a
  mismatch is flagged in `price_label`, same as an unconfirmed dosage.
  Not independently confirmed live for a non-tablet drug (none has been
  tested against this page), but it's a strict generalization of an
  already-confirmed regex: any drug whose summary still reads "(N
  tablets)" parses exactly as before.

### GoodRx: drug name → URL, via search

Previously built directly from the drug name (`goodrx_slug()`) — see
[Drug name → URL](#drug-name--url-via-each-sites-own-search-not-a-guessed-slug)
above for why this was replaced everywhere it was used, even though no
actual failure was confirmed for GoodRx specifically (unlike SingleCare).
`_resolve_drug_url()` navigates to GoodRx's homepage, types into the
confirmed real search input (`#hero-drug-search-input`), and either
clicks a suggestion or submits the search directly if no suggestion
selector matched — see this document's opening section for exactly what
is and isn't confirmed live about this method. Either way, whatever URL
results is validated against GoodRx's own confirmed real per-drug URL
shape (`_looks_like_drug_page_url()`: a single path segment, e.g.
"/lisinopril", excluding a short list of known non-drug paths like
"/search" or "/drugs") before being trusted — if that doesn't match, it
falls back to scanning the current page for a link whose text loosely
matches the requested drug name, on the theory that landing anywhere else
most likely means a search-results listing rather than a direct hit.

**Reported live: GoodRx produced no results at all**, with no debug HTML
left behind to show why. Root cause: two of this method's own failure
paths — search input not found, and the final Enter-key submit failing —
never called `_save_debug_page()` at all, the identical gap already found
and fixed for `singlecare_scraper.py`'s equivalent method (see below).
Fixed the same way, so a repeat of this leaves
`page_goodrx_search_no_input.html`/`page_goodrx_search_no_submit.html` to
diagnose from instead of nothing. The actual root cause of *this*
specific run's failure is still unconfirmed — there was nothing to
inspect it with — but the suggestion-click loop was also hardened
defensively at the same time: it now requires a candidate suggestion's
own text to read the same twice, ~0.3s apart, before trusting it, the
same fix applied to a *confirmed* version of this exact failure mode on
SingleCare's identical `send_keys()`-then-poll pattern, immediately
below.

**Reported live again, this time with the actual error attached: `could
not type into GoodRx's search box: ... element not interactable`.**
Confirmed live, and a genuinely different bug from every other "checked
before it settled" timing race fixed elsewhere in this project —
`#hero-drug-search-input` isn't actually unique on the page. Two
elements share that id (invalid HTML, but real — confirmed by querying
for all matches, not just the first): one is zero-size and
`is_displayed() == False`, the other is the real, visible input.
`driver.find_element()` — and `EC.presence_of_element_located`, which
uses it internally — always returns the *first* DOM match regardless of
visibility, which happened to be the hidden one. This isn't a race that
more waiting would fix — the wrong element was picked deterministically,
every time, not intermittently. Fixed with a new
`_wait_for_visible_element()` helper that searches *all* matches for the
selector and returns the first one that's actually `is_displayed()`
(still polling, since visibility itself could still be settling) —
generic enough to reuse anywhere else a selector might turn out to be
non-unique like this one did. Verified live end-to-end: resolves
straight to `https://www.goodrx.com/atorvastatin` with no error.

**Reported live again: GoodRx searches started landing on
`https://www.goodrx.com/discount-card`** — GoodRx's generic "get a
discount card" marketing page, not a drug page. Confirmed live by
inspecting the DOM step by step: GoodRx's autocomplete dropdown now
renders under `#hero-drug-search-results` as `<li
data-qa="search-result-N" role="link" aria-label="...">` items (e.g.
`aria-label="Lisinopril"`) — not `role="option"` or `role="listbox"`,
and not tagged `data-testid*=suggestion` either, which is what the old
`SEARCH_SUGGESTION_SELECTORS` list was matching against. That old list's
`[data-testid*=suggestion]` entry turned out to spuriously match a
completely unrelated, always-present "Popular searches" quick-links
panel that appears the instant the search input is focused, before any
text is even typed — its content is static, so it trivially passed the
"read the same text twice, ~0.3s apart" settle check described above,
and the code clicked it believing it was the real answer. That panel
contains its own bundle of links (popular drugs, promos including the
discount-card page); the browser's native click lands on whatever's at
the container's actual click point rather than on drug_name specifically,
which is how a lisinopril search ended up on `/discount-card` with no
error at all — a silently wrong result, not a crash, so it took stepping
through the live DOM to catch.

Fixed by replacing the matching logic outright rather than patching the
selector list: a new `_pick_search_result()` polls
`#hero-drug-search-results li[data-qa^="search-result-"]` (the real
result list, confirmed live) until its aria-labels stop changing, drops
any entry whose `aria-label` starts with "Sponsored ad" — confirmed live
that GoodRx often serves one for an unrelated paid program (e.g.
`"Sponsored ad: Lisinopril is $0 with Companion - ..."`) ahead of the
genuine result — and picks whichever remaining entry's `aria-label`
loosely matches the requested drug name, falling back to a lone
survivor only when there's exactly one (so an ambiguous multi-result
page isn't guessed at). The old selector list is kept as
`SEARCH_SUGGESTION_SELECTORS_LEGACY`, tried only if the new lookup finds
nothing at all, in case some future page variant reintroduces a real
`role="option"` dropdown; it's demoted from primary because it was
demonstrated live to actively pick the *wrong* thing, not merely fail to
find anything. All clicks in this method now go through
[`robust_click()`](#clicking-through-overlays-a-shared-helper) as well,
on the same reasoning as everywhere else it was already adopted.

**Verified live after the fix, minus the CAPTCHA path**: a follow-up
run with no challenge involved confirmed `_pick_search_result()` finds
the real "Lisinopril" result, skips the sponsored entry ahead of it, and
clicks through to `https://www.goodrx.com/lisinopril` exactly as
intended.

**Reported live again, right after that: solving the CAPTCHA itself led
to the same `/discount-card` page as before** — this time via a
completely different mechanism. `_resolve_bot_challenge()` used to treat
"the reload no longer shows a CAPTCHA marker" as proof the page was
clear and safe to keep going from. Confirmed live: right after solving
GoodRx's `px-captcha`, this project's own follow-up reload of the
homepage landed on `/discount-card` — GoodRx's generic "sign up for a
free savings card" page — which is a perfectly ordinary page with no
CAPTCHA markers on it at all, so the old check declared success while
sitting on the wrong page entirely. This reads as a softer, second-tier
anti-bot response: rather than re-challenge outright once a CAPTCHA has
already been solved once in a session, redirect to a generic page
instead of the one actually requested. `_looks_like_bot_challenge()`
alone can't distinguish that from a real, clean recovery.

Fixed by checking *where* the reload actually landed, not just whether
a CAPTCHA is showing: `_resolve_bot_challenge()` now compares the
post-reload URL's path against the path it was asked to reach, and only
declares success when they match. A mismatch triggers exactly one more
direct reload of the same URL (the case observed live cleared on a
second try); if that also lands somewhere else, it's reported as still
blocked rather than silently accepted. Verified against the exact
observed sequence (redirected once, recovers on retry; redirected
persistently; lands correctly first try) with a scripted fake driver
standing in for the real one, since deliberately reproducing this again
against the live site would mean provoking another CAPTCHA — not fully
re-verified against GoodRx itself end-to-end for that reason, but the
file compiles/imports cleanly and the logic is exercised directly.

**Reported live a third time, same page, after solving the challenge
this fix's checks were actually built for** — and this time the tool's
own output showed no bot-check error at all, just an ordinary-looking
"no price rows found". That ruled out `_resolve_bot_challenge()` (fixed
above) as the culprit for this specific report: it always reports a
mismatch loudly, never silently. Traced to a second, independent
instance of the exact same silent-drift shape, one level deeper.
Submitting a ZIP code reliably triggers its *own* separate "Press &
Hold" challenge, handled by a different method,
`_resolve_inline_bot_challenge()` — deliberately not
`_resolve_bot_challenge()`, since this one is an in-place overlay on an
in-progress action, not a navigation (see its docstring). It had the
identical gap `_resolve_bot_challenge()` had before today's earlier
fix: "no challenge marker left" was trusted as "resolved", with nothing
checking *where* solving it left the browser. Confirmed live: it can
leave it on `/discount-card` too. Worse, `_try_enter_zip()` — the only
caller that matters here — threw its return value away entirely
(`self._try_enter_zip(zip_code)`, not `if not self._try_enter_zip(...)`),
so even a correctly-detected failure changed nothing: `search()` read
straight on into dosage/quantity/price extraction on whatever page the
drift actually left it on, which predictably has none of that.

Fixed at both points, not just one, since either alone leaves a gap:
`_try_enter_zip()` now captures the page's path before doing anything
and checks it again once its challenge appears resolved, the same
path-match check `_resolve_bot_challenge()` got, so it can honestly
return "not actually resolved" instead of a false "yes". And separately
— because trusting any one boolean here has now failed live twice —
`search()` no longer just calls `_try_enter_zip()` and moves on; it
independently re-checks the browser's URL against the drug page it
started from immediately afterward, regardless of what `_try_enter_zip()`
itself reported, and returns a clear "page drifted away ... (likely a
bot-check redirect)" error rather than silently reading a wrong page.

**Not independently re-verified live**: by this point, testing this
session had made enough automated requests to GoodRx in a short window
that it was triggering a challenge on nearly every attempt, including
ones that used to succeed cleanly earlier the same session (e.g. a
plain `_resolve_drug_url()` call with no ZIP involved at all) — a sign
this session's own traffic pattern is now the thing being flagged, not
any one code path. Continuing to retry live at that point risks digging
that hole deeper for no real signal, so testing stopped here rather
than pushing further. The file compiles/imports cleanly and the fix
follows the same shape already verified for `_resolve_bot_challenge()`
above, but treat GoodRx specifically as unverified until a run happens
after this session's automated traffic has had time to cool down.

### GoodRx: dosage/quantity — the "Edit prescription" modal

Confirmed live: dosage and quantity are real native `<select>` elements
(`#configuration-editor-dropdown-dosage`, `#configuration-editor-dropdown-quantity`
— both with stable ids, and aria-labels "Dosage"/"Quantity") — but they
only exist inside an "Edit prescription" modal dialog, opened by clicking
a button matched by its text (no `data-qa`/`aria-label` of its own):
"Medication{drug} {dosage} ({quantity} tablets)Edit". The page itself has
*zero* `<select>` or `role="listbox"`/`role="combobox"` elements outside
that modal — confirmed live via direct DOM inspection — so the previous
`select[name*=dosage]`-style lookup could never have matched anything,
on any run, the same silent-no-op pattern already found and fixed for
ZIP and pharmacy-list expansion earlier in this document.

Confirmed live, and this is the important part: clicking "Confirm
prescription" to apply a dosage/quantity change reliably triggers the
same "Press & Hold" PerimeterX challenge that submitting a ZIP change
does (see [GoodRx: ZIP code](#goodrx-zip-code)) — not observed
intermittently, the one attempt made here to verify this hit it too.
Because of that, this modal is now only opened when actually necessary:
the page's current default (read cheaply from the "Edit" button's own
text, no modal needed) is compared against the requested dosage/quantity
first, and the modal only opens on a real mismatch — unconditionally
opening it on every lookup, the way the old (non-functional) dosage
selection ran unconditionally, would mean forcing this challenge far more
often than necessary, since GoodRx's default dosage frequently won't
match what was requested. If both ZIP and dosage/quantity need changing
in the same lookup, expect **two** separate Press & Hold prompts, not
one — they're genuinely separate site-triggered challenges.

Not independently verified end-to-end here: solving that Press & Hold
myself to confirm the full round-trip is out of bounds — this project
never completes CAPTCHAs programmatically, the same boundary respected
everywhere else in this file.

**Reported live, right after this shipped: the CAPTCHA got solved
successfully, but the run still failed afterward anyway.** Confirmed from
a debug dump of the exact run (`page_goodrx_no_rows.html`, saved when
`_extract_price_rows()` found zero rows): the page hadn't failed or been
re-blocked at all — no CAPTCHA markers anywhere in it — but its pharmacy
list section was still showing its loading skeleton
(`data-qa="pharmacy-selector-container"`, `aria-busy="true"`,
`"Loading..."`). Root cause: confirming a dosage/quantity change
re-renders that whole section, and `_try_edit_prescription()` returned
immediately after handling the Press & Hold without waiting for it — the
same "checked before it settled" issue already fixed elsewhere in this
project (ZIP earlier in this same file, Cost Plus Drugs' price,
SingleCare's challenge re-check), just not yet applied to this specific,
newer action. Fixed: it now polls for a real pharmacy row
(`PRICE_ROW_SELECTORS[0]`) to actually appear before returning, the same
poll-don't-guess pattern used throughout.

**Reported live: GoodRx results stopped showing a quantity at all.**
Confirmed from a fresh debug dump: the "Edit" button's DOM had changed
since it was first verified live — just hours earlier, in the same
investigation — most likely an A/B test bucket rather than a one-time
site change (nothing else about the page looked different). The button
no longer has the literal text "Medication...Edit" this file's matching
was built against at all: "Edit" is now an SVG icon
(`data-qa="icon-goodrx-edit-filled"`) with no text, and "Medication" as a
label is gone too — the summary text ("Lisinopril 20mg (30 tablets)") now
sits in its own `<span>` with no surrounding text to match against. A
text-based lookup built for one specific wording will always be this
fragile against a site that can silently serve a different DOM shape to
different sessions. Confirmed live on this new variant: the button now
carries a stable `data-qa="rx-editor-button"` attribute. Fixed:
`_find_prescription_edit_button()` tries that confirmed selector first,
falling back to the original text-based match for whichever variant
shows up — rather than assuming either one is the only shape this page
can take, which is exactly the assumption that broke here.

**Reported live, separately: very few GoodRx options came back — not an
error, just a much shorter pharmacy list than expected.** Confirmed from
a debug dump of the same run's initial page load: GoodRx's real default
list has 9 rows, un-truncated, right from the start — so this wasn't a
truncated-list case ([GoodRx: pharmacy list
truncation](#goodrx-pharmacy-list-truncation) already covers that
separately). Root cause: the wait added when fixing the *zero*-rows
regression above only checked that *at least one* pharmacy row was
present before moving on — both a ZIP change and a confirmed
dosage/quantity change replace the whole pharmacy list asynchronously,
and a presence-only check can be satisfied by the first row or two to
stream in, well before the rest arrive. Extraction right after that
silently got a much shorter list than the page was about to show — not
an error, so nothing else in this file would have caught it either.
Fixed: `_wait_for_stable_price_rows()` now polls for the row *count* to
stabilize (two reads ~0.5s apart returning the same non-zero number),
the same stability-check pattern already proven for the zero-rows case
above and for SingleCare's equivalent bug. Applied in both places that
reload the list — after ZIP entry (which had no row-count wait at all
before this, only a wait for the location button's own text to update)
and after a confirmed dosage/quantity edit (upgrading the presence-only
wait from the fix above).

**Reported live: dosage/quantity still weren't being applied, showing
the "could not confirm ... was selected" caveat — and this time, unlike
the report above, the person running it confirmed a CAPTCHA prompt *did*
appear and *was* solved.** That ruled out the obvious explanation (a
skipped/declined prompt) and pointed at something else entirely. Directly
reproduced by driving the exact same steps live, repeatedly, until it
failed: clicking the "Edit" button itself — not just clicking "Confirm
prescription", the only click this file's existing challenge-handling
already covered — can independently trigger GoodRx's PerimeterX
challenge, confirmed via the literal exception it raises: `element click
intercepted: ... Other element would receive the click: <iframe
id="px-captcha-modal" ...>` — a full-page overlay iframe, appearing
reactively mid-interaction, not just on initial page load (the "before"
check right after navigating and the "after" check right after
confirming don't cover a challenge that shows up *during* a click
in between them). That's exactly consistent with what was reported: a
user can solve *a* challenge for this lookup (the initial page-load one)
while never being shown *this* one, since nothing before this fix ever
checked for it — the click was simply, silently intercepted, caught by
the bare `except Exception: return False` already wrapping it, with no
prompt and no debug artifact.

Fixed with a new `_click_through_challenge()` helper, wrapping both the
"Edit" button click and the "Confirm prescription" click: on an
intercepted click, it checks whether GoodRx's challenge is actually the
cause, and if so prompts to solve it via `_resolve_inline_bot_challenge()`
— deliberately not `_resolve_bot_challenge()`, for the same reason
`_resolve_inline_bot_challenge()` already existed: this is an overlay on
an in-progress interaction, not a navigation, and reloading would
destroy that interaction here exactly as it would there — then retries
the click once. Both click sites now also save a debug snapshot on
failure (`page_goodrx_edit_click_intercepted.html`/
`page_goodrx_confirm_click_intercepted.html`), closing the same
"no artifact to diagnose from" gap already closed for search earlier in
this document. Separately, the re-check after a confirmed edit was
upgraded from a single immediate read to a poll
(`_wait_for_updated_medication_summary()`) — the same "checked before it
settled" class of fix as everywhere else in this project — in case the
page needs a beat to reflect the change even once a click genuinely goes
through.

Confirmed live: the click-interception mechanism itself, via direct
reproduction (the exact exception above). **Not** independently
confirmed end-to-end: that solving the prompt this fix now surfaces
actually results in the retried click succeeding and the value sticking
— verifying that would mean completing a CAPTCHA myself, which this
project never does. Also not re-verified after this specific fix:
repeated live testing while diagnosing this ended up triggering GoodRx's
*next* escalation tier — a full "Access to this page has been denied"
block on the search step itself, likely from the sheer volume of
automated requests this investigation made in a short window, separate
from anything this fix changes. That block should ease on its own after
a quieter period; it isn't a reason to doubt the fix, but it did mean
stopping further live verification here rather than pushing through it.

**Reported live, right after the fix above shipped: the modal itself was
now confirmed visibly open, watched directly (not just inferred) — and
the dosage/quantity dropdowns simply never changed away from the page's
default the whole time it was open.** This ruled out both prior
explanations (a declined prompt, or the click never landing at all) and
pointed at the select interaction itself. Two distinct, real causes
turned up:

1. Confirmed via a fresh debug artifact caught while reproducing this
   (closing the same "no failure-path save at all" gap this whole
   select-setting block had, exactly like the two click sites above):
   one snapshot had *zero* dosage/quantity `<select>` elements present,
   with no challenge markers either — the modal can fail to actually
   finish opening within the wait window even when the Edit click itself
   raised no exception, a plain rendering/timing gap unrelated to any
   challenge. Fixed with one retry of the same click if the expected
   `<select>` doesn't show up in time, before giving up.
2. **Not independently confirmed for this specific report** — no debug
   artifact captured the *exact* frozen-dropdown moment described, since
   nothing looked wrong from the DOM's perspective by the time a
   snapshot could be taken — but defended against anyway, because it's a
   well-known Selenium/React pitfall that matches the symptom precisely:
   `Select.select_by_visible_text()` performs a native option click,
   which can update a `<select>`'s own value/selectedIndex (so reading
   it back immediately after can look "correct") without the browser
   necessarily firing a real `change`/`input` event in every case — and
   a React-controlled component (which is what actually drives this
   dropdown's *visible*, custom-styled label; the underlying native
   `<select>` itself was already confirmed elsewhere in this file to
   often report `is_displayed() == False`, i.e. it isn't the thing
   actually rendered on screen) only updates that visible label in
   response to that event. Now dispatches both explicitly right after
   selecting — harmless if the native click already fired them
   correctly, the fix if it didn't.

GoodRx's search-level block from the previous investigation was still in
effect when this was written, so this couldn't be re-verified live
end-to-end yet either — both fixes above are reasoned from real evidence
(the empty-select snapshot is a confirmed cause; the event-dispatch gap
is a well-documented pattern matching the exact symptom described, not a
blind guess), but neither has a live "before vs. after" run to point to
yet.

**Reported live again, still failing — with a lead this time: "there is
a banner that expands from the bottom, and has to be closed before the
dropdowns are operational."** That specific theory didn't hold up under
live reproduction — screenshotting the open modal, waiting several
seconds, and inspecting every fixed/sticky element on the page found no
bottom banner blocking anything. But reopening the same modal fresh,
repeatedly, to look for one turned up something bigger: **the entire
"Edit prescription" modal has (at least) two DOM variants GoodRx serves
seemingly at random — not just the "Edit" button that
`_find_prescription_edit_button()` already handles two variants of.**
Confirmed live in 2 of 3 fresh sessions in one run, so not a rare edge
case:

- **Variant A** — what this file was built against: `#configuration-
  editor-dropdown-dosage`/`#configuration-editor-dropdown-quantity`
  `<select>`s, single "Confirm prescription" button, no stable
  `data-qa` on it.
- **Variant B** — confirmed live for the first time here: separate
  `#dosage`/`#quantity`/`#form` `<select>`s (different ids entirely —
  this fully explains the "zero `<select>` elements" debug snapshot from
  the previous report above: it wasn't a rendering-timing gap at all,
  it was Variant A's selectors genuinely never existing on a Variant B
  page, no matter how long or how many times that retry waited), and
  "Cancel"/"Update" buttons instead of one "Confirm prescription" button
  — matched by their own stable `data-qa` values, confirmed live:
  `prescription-editor-modal-cancel-button`/
  `prescription-editor-cta-button`.

(`#form` is confirmed live to exist in Variant B but be genuinely
disabled, with a single "tablet" option, for a tablet-only drug like
atorvastatin — GoodRx already fixes the form itself here; this isn't a
missed formulation-selection feature, consistent with what was already
found and reported for Variant A: no formulation control at all.)

Fixed: `DOSAGE_SELECT_SELECTOR`/`QUANTITY_SELECT_SELECTOR` are now
`DOSAGE_SELECT_SELECTORS`/`QUANTITY_SELECT_SELECTORS` — lists, tried in
order via a new `_find_first_present()` helper, the same "confirmed
selectors, most-to-least specific" resilience pattern already used for
`PRICE_ROW_SELECTORS`/`SEARCH_SUGGESTION_SELECTORS` elsewhere in this
file, just applied to a dosage/quantity lookup for the first time. The
confirm-button click now goes through a new `_find_confirm_button()`
that tries Variant B's `data-qa` first, falling back to Variant A's text
match — the same selector-then-text-fallback shape as
`_find_prescription_edit_button()` itself. Verified live end-to-end
across multiple fresh sessions afterward: dosage and quantity both
applied correctly with no caveat, landing on both variants across
different runs (not by design — which variant loads isn't something
this file controls or can force, only handle either way).

This also means the two defensive fixes from the report just above this
one were addressing a real but different, narrower risk than what was
actually causing the reported symptom in practice — a genuine rendering
lag independent of variant (the retry-on-modal-not-open logic) and the
React-controlled-select event-sync gap (the dispatched `input`/`change`
events) are both still real, still kept, and still worth having; they
just weren't the dominant cause here. The variant mismatch was.

**Found in a later code review: `_loose_matches()` here had the same
substring-collision bug documented at [Matching dosage, quantity, and
form](#matching-dosage-quantity-and-form-a-substring-collision-bug-shared-across-four-files)**
— now fixed via the same shared `utils.token_matches()`. Formulation
also gets a caveat now, not silence — see [Formulation: is this
actually the requested form?](#formulation-is-this-actually-the-requested-form)
for why GoodRx's version can only flag a mismatch rather than correct
one (confirmed live: this modal has no formulation control at all).

### GoodRx: interactive bot-check

GoodRx frequently serves an interactive CAPTCHA instead of the drug page —
confirmed live, a PerimeterX `px-captcha` widget (page title "Access to
this page has been denied"), not just a JS-only Cloudflare-style check. No
headless session can solve that; it needs an actual human looking at the
widget. So when `goodrx_scraper.py` detects one (via title/HTML markers —
see `BOT_CHALLENGE_MARKERS`), it doesn't just fail:

1. If running headless (the default), it closes that session and opens a
   **visible** Chrome window on the same URL.
2. It prints a message and blocks at the terminal with `input()`, the same
   interactive pattern `--setup-amazon` already uses for Amazon login.
3. Solve the CAPTCHA in the Chrome window, then press Enter in the
   terminal to continue — the reload picks up the site's now-passed
   session and the scrape proceeds normally from the same point.
4. Pressing Enter without solving it, or `GOODRX_INTERACTIVE_CAPTCHA=false`,
   skips straight to a clean error result — never hangs indefinitely
   waiting on a check that will never be solved.

The "is it resolved now?" re-check after the reload polls for a few seconds
rather than judging from one immediate snapshot — confirmed live on
SingleCare's equivalent check (below), a reload can briefly re-show an
interstitial/verifying state even with a valid solved-challenge cookie
already set, before redirecting to the real page a moment later. Checking
once immediately risked reporting "still blocked" on a page that had
already loaded successfully.

### GoodRx: ZIP code

Same class of bug as SingleCare's (below): `_try_enter_zip()` searched for
a plain `input[name*=zip]`-style field that never existed on the real
page. Confirmed live (from real, fully-loaded page captures): location is
a **button** (`aria-label="Set your location, Current location is
55419"`) that opens a modal/dropdown on click — the actual input only
exists inside that modal, invisible in any static capture until clicked.
So every previous attempt silently found nothing and did nothing, while
still returning prices as if for the requested ZIP — the same
silently-wrong-data problem as SingleCare's original bug.

`search()` re-checks the rendered ZIP afterward via `_detect_rendered_zip()`
(parses `"Current location is 55419"` from the page) and adds a caveat to
`price_label` if it still doesn't match — so an unconfirmed ZIP never
silently produces prices that claim to be for the wrong location, no
matter how the attempt below goes.

`_try_enter_zip()` itself was fixed the same way as SingleCare's equivalent
bug: by driving a real, non-CAPTCHA-gated browser session directly against
the live site instead of guessing through the blocked Selenium path.
Confirmed live: clicking the location button opens a modal with a plain
text input (no autocomplete, unlike SingleCare's) and an explicit **"Set
location" submit button** — the original version of this method never
clicked that button at all, so Enter alone may never have confirmed
anything. Fixed to click it directly.

That still didn't work: the modal correctly opened, but nothing got typed
into it. Root cause, found by inspecting the input's actual DOM attributes
rather than its accessible name: `ZIP_INPUT_SELECTOR` matched against
`placeholder*="ZIP code"`, but the real input's `placeholder` is empty
(`""`) and it has no `aria-label` either — "Enter a city or ZIP code" is
the accessible name produced by an associated `<label>` element, not
anything on the input itself. `WebDriverWait` on that selector silently
timed out every single time, so `_try_enter_zip()` returned before typing
anything — while the modal, opened by a separate, correct selector, stayed
visibly open the whole time. Fixed to the confirmed real selector,
`#locationModalAddress`.

Once typing itself worked, the ZIP still reported as unconfirmed even
after actually applying it on screen. Same class of bug as everywhere
else this session: `search()` checked `_detect_rendered_zip()` exactly
once, immediately after `_try_enter_zip()` returned — no wait, no poll.
Confirmed live: the location button's label doesn't always finish
updating within that immediate instant, particularly right after solving
a Press & Hold (there's presumably an API round-trip to actually apply
the new location server-side after the challenge clears). Fixed:
`_wait_for_rendered_zip()` now polls for up to `SELENIUM_WAIT_TIMEOUT`
instead of checking once, and reads the live aria-label attribute
directly (`driver.find_element(...).get_attribute("aria-label")`) rather
than `driver.page_source` — the same page_source-lags-behind-the-live-DOM
issue already confirmed on Cost Plus Drugs applies here too.

Reported from live testing: after the page-load bot-check, the window
would sit idle for a long time, only enter the ZIP at the very end, then
close immediately — with no pharmacy names or prices in the result.
Root cause: `search()` selected dosage/quantity *before* attempting the
ZIP, but `_try_enter_zip()` can close and recreate the driver entirely
(switching to a visible window before a ZIP change, since that action
reliably triggers its own bot-check — see below). That recreation
silently discarded the dosage/quantity selection that had just been made
on the now-closed driver — the fresh page after a ZIP-triggered relaunch
comes back with default dosage/quantity, nothing having failed loudly
enough to notice. Fixed: ZIP is now attempted *first* (relaunch and all),
dosage/quantity selection happens after, against whichever driver is
actually active by then.

Also confirmed live — **reliably, on every attempt tried, not just
occasionally** — submitting a ZIP triggers a *separate* PerimeterX "Press
& Hold to confirm you are a human" challenge, distinct from the page-load
`px-captcha` widget `_resolve_bot_challenge()` already handles.

Getting this right took two passes:

1. The first version detected this challenge and delegated straight to
   `_resolve_bot_challenge()` — which turned out to be actively wrong
   here, not just incomplete. That method is built for a full-page
   challenge reached by navigating to a URL: solving it, it `close()`s and
   recreates the driver (if headless), then reloads that URL. Confirmed
   live: this Press & Hold is an *overlay* on top of the in-progress ZIP
   submission, not a navigation — closing the driver destroys that overlay
   (and the whole in-progress action) before a human ever sees it. The
   fresh reload afterward lands on a normal, unchallenged page with the
   ZIP back to default, and the old code reported that as "resolved" even
   though nothing was actually solved.
2. Fixed two ways: `_resolve_inline_bot_challenge()` is a dedicated
   handler that never closes the driver or navigates anywhere — it only
   prompts in place and re-checks, preserving whatever state triggered it.
   And since this challenge is essentially guaranteed to appear (not
   intermittent), `_try_enter_zip()` now switches to a visible window
   *before* attempting anything, if headless — reactively discovering
   that need after an already-doomed headless attempt would mean a human
   never gets a real chance to solve it, the same failure mode as (1).
   Both are gated on `GOODRX_INTERACTIVE_CAPTCHA` like everything else
   here — disabled, `--zip` skips straight to the (fated-to-fail) plain
   attempt rather than forcing a visible window open for no one to use.

Verified end-to-end short of the actual press-and-hold (which needs a real
human — deliberately the one thing this tool should never do for you):
with the interactive prompt skipped, the pipeline runs cleanly all the way
through with real pharmacy names and prices, correctly reporting a "could
not confirm ZIP was applied" caveat rather than silently returning the
wrong location's prices. Whether solving the actual challenge then
successfully applies the ZIP still needs a real solve to fully confirm.

### GoodRx: pharmacy names and prices

Separately, `PRICE_ROW_SELECTORS` never matched GoodRx's real markup at
all — a pure guess (`[data-testid*=pharmacy]`, `[class*=PharmacyCard]`,
`tr`, etc.) made before any real page could be seen past the CAPTCHA, none
of which exist on the real site. Found the same way as the ZIP fix above:
each pharmacy row is a `<button>` containing `[data-qa=seller-name]`
(plain text, e.g. "Walgreens"), `[data-qa=seller-price]`, and optionally
`[data-qa=special-offer-text]`. Verified live against 9 real rows —
`button:has([data-qa=seller-name])` is now the primary selector, with the
old guesses kept as speculative fallbacks.

That `special-offer-text` turned out to matter: rows with it (e.g.
"Walgreens $0.00 with Companion") are a **paid GoodRx membership** price
("Companion", $9.99+/mo), not a walk-in coupon price — confirmed live,
several other rows (Costco, Walmart, Sam's Club, Capsule Pharmacy in the
same real lookup) have no such note and are genuine no-strings-attached
prices. `price_label` now includes the offer note per-row (e.g. `cash/
coupon price (with Companion)`) rather than presenting a subscription
price as if it were the plain one — the same "loyalty bonus" complication
SingleCare's rows had, handled the same way: surfaced, not hidden.

Both prices are shown for those rows, not just the Companion one. Reported
from live testing: clicking a pharmacy row updates a "Standard GoodRx
Price" panel elsewhere on the page with that pharmacy's plain,
no-membership-needed price. `_get_standard_price_for_row()` clicks through
each of the final top-5 rows that has an offer note (not every row on the
page — the extra clicks are bounded to what's actually returned) and emits
a second result labeled `cash/coupon price (standard, no Companion
membership)` right after that pharmacy's Companion-price row.

Getting this reliable needed the same fix as everywhere else this session:
a flat delay after clicking wasn't enough — a first version using a 1s
sleep only successfully got the standard price for 1 of 5 rows clicked
through in the same run. `_extract_standard_price()`'s panel-reading is a
best-effort body-text search rather than independently confirmed
markup — the exact selector for that panel couldn't be reached live
(blocked by CAPTCHA while trying) — but `_get_standard_price_for_row()`
now polls for it and requires it to be stable across two checks ~0.5s
apart, the same pattern already used in `costplusdrugs_scraper.py` and
`singlecare_scraper.py`. Verified live after that fix: 3 of 3 Companion
rows in one run correctly got a standard price, and one of them
(Walgreens, $15.57) independently matches the exact price seen via direct
browser inspection earlier, before any row had been clicked — the
page's own default-selected pharmacy.

Three more issues turned up from actually using this, all fixed together:

- **Plain (no-Companion) pharmacies stopped appearing at all.** Reported
  live: Costco, Sam's Club, Walmart, and Capsule Pharmacy — all genuinely
  no-strings-attached rows — never showed up in the output. Root cause:
  rows were sorted by *raw displayed price* and the top 5 taken — but
  Companion prices are artificially low ($0.00, $9.00), so they crowded
  every plain row out of the top 5 entirely, even though those are the
  actually-accessible options. Fixed: plain rows are now always included
  (their displayed price already is the standard one — no click-through
  needed), with only the cheapest few Companion rows
  (`GOODRX_COMPANION_ROWS_LIMIT`, currently 3) added on top.
- **The window sat open and idle for a long time.** Directly caused by the
  same top-5-by-raw-price selection: up to 5 Companion rows could each
  cost the full `SELENIUM_WAIT_TIMEOUT` (15s) in the click-through loop —
  worst case, well over a minute of visibly doing nothing. Fixed two ways:
  capping Companion rows processed (above) bounds *how many* rows pay that
  cost, and a new, dedicated `STANDARD_PRICE_WAIT_TIMEOUT` (6s, shorter
  than the general-purpose element-wait timeout) bounds *how long* each
  one can take.
- **A correctness gap in the stability check itself**, found while fixing
  the above: two identical reads 0.5s apart look "stable" whether or not
  the panel actually updated for *this* row — it could just be an
  unchanged leftover from the previous row's click. Fixed:
  `_get_standard_price_for_row()` now also requires the price to differ
  from the previous row's confirmed value before accepting it, with the
  caller (`search()`) tracking that value across rows.
- **That same fix then dropped CVS and Target instead.** Reported live as
  a regression right after the fix above shipped: CVS and Target had gone
  missing. Root cause: `GOODRX_COMPANION_ROWS_LIMIT` was being applied to
  the *output list itself* (`companion_rows[:GOODRX_COMPANION_ROWS_LIMIT]`
  fed straight into what got returned), not just to the click-through
  loop — so with more than 3 Companion rows on the page, the extras were
  silently dropped from the results entirely, not just left without a
  standard price. Confirmed live from a saved copy of the actual page
  (a real 9-row list: Walgreens, Hy-Vee, Target (CVS), CVS Pharmacy, and
  Walgreens Specialty Pharmacy all had Companion offers — 5 total, 2 more
  than the cap): those exact two extra rows are the ones that would be
  cut. Fixed: the output list now always includes every row, plain or
  Companion; `GOODRX_COMPANION_ROWS_LIMIT` only bounds how many Companion
  rows get the extra click-through for a standard price — the rest still
  appear in the output with their Companion price and offer note, just
  without a second standard-price row alongside them.
- **CVS then quietly lost just its standard-price row.** Reported live as
  a follow-up: CVS (and, per that same report, Family Fresh Pharmacy) had
  their Companion price but no `(standard, no Companion membership)` row
  alongside it. Root cause was the *other* half of the same
  `GOODRX_COMPANION_ROWS_LIMIT` cap — it still bounded the click-through
  loop to the cheapest 3 Companion rows. Confirmed live from a debug dump
  of the exact run in question: Walgreens, Hy-Vee, and Walgreens Specialty
  Pharmacy were all tied for cheapest at $0.00, so the cap's "cheapest 3"
  filled up on those three alone — CVS Pharmacy and Target (CVS), both
  $9.00, never got a click-through attempt at all, not a failed one.
  Since completeness has clearly mattered more than speed here (this is
  the second time a size-based cap silently dropped real pharmacies),
  `GOODRX_COMPANION_ROWS_LIMIT` is removed entirely — every Companion row
  now gets a standard-price click-through attempt.
  `STANDARD_PRICE_WAIT_TIMEOUT` (6s) alone bounds the cost per row; GoodRx
  pharmacy lists seen so far top out around 9, so even a worst case where
  every row is a Companion row that times out stays under a minute.

### GoodRx: pharmacy list truncation

Reported from live testing: the default pharmacy list can be truncated,
with an expand control revealing the rest — including the plain,
no-Companion pharmacies above. `_expand_pharmacy_list()` clicks it before
extraction if present. Not independently confirmed here: reproducing this
live landed on a page that already showed the full list (all 9 pharmacies,
confirmed via direct DOM inspection), so the control's exact wording
couldn't be directly seen. Matched against several plausible variants
("see all pharmacies", "view all", "show all", etc.) rather than one exact
guess, and deliberately scoped to the pharmacy list's own container rather
than the whole page: the page has an unrelated "View All" link elsewhere
(a footer "Browse medications" section, pointing to `/drugs`) — a
page-wide search risked clicking that instead and navigating away from the
drug page entirely. Anything matching text but carrying a real navigation
`href` is skipped for the same reason.

Confirmed from a saved copy of the real page (provided directly, not
scraped): GoodRx does ship this exact control — the translation strings
`seeAllPharmacies`/`"See all pharmacies"` and `seeLessPharmacies`/`"See
less pharmacies"` are both present in the page's data — and the pharmacy
list container does carry a `data-qa="expandable"` marker, matching the
scoping logic above. In that particular saved copy the control wasn't
actually engaged (`style="max-height:none"` — all 9 rows were already
present, un-truncated), so it still hasn't been possible to see the
control actually fire and confirm the click itself works, only that the
scoping heuristic is aimed at the right element. Also confirmed from that
same saved copy that a follow-up report of missing pharmacies (CVS,
Target) was a *different* bug, not this one — see the
`GOODRX_COMPANION_ROWS_LIMIT` fix above.

### SingleCare: drug name → URL, via search

Reported live, with a concrete failing case: `--drug atorvastatin` used
to build `singlecare.com/prescription/atorvastatin` (`singlecare_slug()`)
directly from the drug name — that page doesn't exist. SingleCare's real
page is `singlecare.com/prescription/atorvastatin-calcium`; there's no
way to know an arbitrary drug needs that salt-form suffix (or any other
qualifier) without asking the site. Confirmed live end-to-end:
SingleCare's homepage has a real, JS-driven autocomplete search
(`#searchbar`, aria-label "Search drugs") — typing a drug name shows a
dropdown of `role="option"` suggestions (e.g. "Atorvastatin Calcium
(Lipitor)", "Ezetimibe-Atorvastatin", ranked with the single-ingredient
match first for this query), and clicking one navigates to that drug's
real canonical URL. `resolve_drug_url()` now drives that flow directly —
opens the homepage, types the drug name, clicks the top-ranked
suggestion, and follows wherever it actually leads — replacing
`singlecare_slug()`-based guessing entirely rather than only patching the
one failing case. This also simplified `get_prices()`: the old
plain-fetch-first fast path was already confirmed dead in practice
(*every* path on singlecare.com, including the homepage, now returns an
identical 403 to a plain `requests` fetch — not page-specific bot
detection, the request itself is blocked regardless of what it asks
for), so Selenium is now the only path, not a fallback with a pretense of
a faster one.

Best-effort, not a guarantee: this always picks the top-ranked suggestion
(confirmed correct for "atorvastatin" — the single-ingredient match
ranked above the combination-drug variants), with no per-drug
disambiguation beyond trusting SingleCare's own ranking.

**Reported live: this produced "nonsensical" results — not slow or
missing, actually pulling up a completely unrelated drug.** Root cause:
`search_input.send_keys(drug_name)` types character by character, and
this dropdown updates live on every keystroke it sees. The original code
right after it was a plain presence check — `WebDriverWait(...).until
(EC.presence_of_element_located(...))` — which returns as soon as *any*
option exists, including one still showing suggestions for an early
partial keystroke (e.g. just "a") rather than the completed drug name.
Clicking that clicks whatever unrelated drug happens to rank first for
that partial query — a real, wrong result, not a failure that would have
been visible as an error. Fixed the same "checked before it settled" way
as everywhere else in this project: it now polls for the option list's
own text content to read the same non-empty set twice in a row, ~0.3s
apart, before trusting any of it.

### SingleCare: interactive bot-check

Same problem, different vendor: SingleCare's Selenium session (the only
path now — see [How it looks up prices](#how-it-looks-up-prices)) can get
served an interactive DataDome CAPTCHA instead of the drug page. Confirmed
live: an iframe from `geo.captcha-delivery.com`
titled "DataDome CAPTCHA", not just a JS-only redirect. `singlecare_scraper.py`
handles it identically to GoodRx above — detects it (via
`BOT_CHALLENGE_MARKERS`), opens a visible Chrome window if currently
headless, and blocks at the terminal with `input()` until you solve it or
press Enter to skip. Controlled by `SINGLECARE_INTERACTIVE_CAPTCHA`
(default `true`).

Getting the "is this actually resolved?" check right took two rounds of
fixes, both confirmed against a real solved page:

1. **A single immediate re-check after reloading could catch a transient
   state.** Even with a valid solved-challenge cookie already set,
   reloading can briefly re-show an interstitial/verifying state before
   the real page loads a moment later. Fix: the re-check now polls for up
   to `SELENIUM_WAIT_TIMEOUT` instead of judging from one snapshot.
2. **The original markers false-positived on legitimately loaded pages.**
   A bare `"datadome"` substring matched an ambient
   `<!-- Datadome script snippet -->` template comment present in every
   page's `<head>` — block or not. That made the polling in (1) pointless:
   every check, no matter how many times it polled, saw "still blocked."
   Fix: tightened to `"datadome captcha"` (from the challenge iframe's own
   `title="DataDome CAPTCHA"`) and `"captcha-delivery.com/captcha/"` (the
   iframe's specific serving path) — verified against both a real block
   page (matches) and a real, fully-loaded page with real prices
   (doesn't match).

### SingleCare: real row extraction and ZIP code

Once (1) and (2) above were fixed, solving the CAPTCHA revealed the actual
row-extraction and ZIP handling had never been confirmed against real
markup at all — both were guesses from before any real page could be seen
past the CAPTCHA. Fixed, both verified against a real solved page with 8
real pharmacy listings:

- **`PHARMACY_ROW_SELECTORS` didn't match anything** — real rows are
  `id="pharmacyItemContainer"` divs (a duplicated, non-unique id across
  every row — that's genuinely how the site is built, not a bug here).
  Added as the primary selector, with the original guesses kept as
  speculative fallbacks.
- **The pharmacy name isn't in any text node** — only in an
  `<img data-name="kroger pharmacy">` or
  `<img alt="Lisinopril coupon at Kroger Pharmacy">`. `get_text()` alone
  silently omits image `alt` text, so name extraction now reads the image
  attributes directly. A couple of rows per page turned out to have no
  identifiable pharmacy image at all (a hidden/duplicate template
  variant) — those are now skipped rather than mislabeled with unrelated
  button/tooltip text scraped from the same row.
- **The ZIP code was silently ignored entirely.** `zip_code` was accepted
  as a parameter but never actually used anywhere in the file — the plain
  fetch has no way to request one at all, and even the Selenium fallback
  never attempted to set one, nor was it ever *triggered* by a ZIP
  mismatch (only by blocking or a dosage mismatch). Confirmed live: a
  lookup with no explicit `--zip` came back priced for `23666` (Hampton,
  VA) — SingleCare's own geo-IP guess of the scraping machine's location,
  not anything requested. Fixed: any explicit `--zip` now forces the
  Selenium fallback (there's no way to verify a specific ZIP any other
  way), which attempts to set it via `_try_enter_zip()`.

That first attempt at `_try_enter_zip()` guessed wrong and failed silently
on a real solve: prices still came back clean, with no caveat at all, for
the (still wrong) default ZIP. Two separate fixes came out of tracking that
down:

1. **No verification existed after the attempt, only before it.**
   `refine()` only checked "is the requested ZIP already correct?" *before*
   calling `_try_enter_zip()`, purely to decide whether to bother — nothing
   re-checked afterward whether the attempt actually worked. So a silent
   failure there could coexist with otherwise-successful dosage selection
   and price extraction against the now-stale page, producing
   clean-looking prices for the wrong location with zero indication
   anything was off. Fixed: `refine()` now re-checks the rendered ZIP
   immediately after attempting to set it and returns that as a separate
   `zip_caveat`, which `get_prices()` surfaces regardless of whether rows
   came back.
2. **The interaction itself was wrong.** The visible "23666 - Hampton, VA"
   text was never actually typeable — `#zipValue` (what the first attempt
   targeted) is a display-only decoy (note its `nocursor` class). Found by
   driving a real, non-CAPTCHA-gated browser session directly against the
   live site rather than guessing further: the real control is a
   `div[role=button][aria-label="Enter Location"]` that opens a genuine
   modal dialog with its own `input[aria-label="Enter zip code"]` (no
   autocomplete — typing a valid ZIP just resolves to a "City, ST" label)
   and a "Done" button. **Verified end-to-end this way** — this exact
   click → type → click-Done sequence changed the displayed prices and
   pharmacies (Hampton, VA → New York, NY) and updated the hidden
   `#zip-code`/`#zipcode` fields `_detect_rendered_zip()` reads.
   `_try_enter_zip()` now performs that real sequence directly, replacing
   the old speculative selector list.

### SingleCare: dosage/quantity were never real `<select>` elements either

Reported live: a Quantity dropdown exists and does change listed prices —
confirmed, clicking "30" instead of the page's default "90" changed one
pharmacy's price from $10.91 to $7.64. Investigating this turned up a
second, older bug in the same area: SingleCare has **zero** native
`<select>` elements anywhere on the page — confirmed live via direct DOM
inspection (`document.querySelectorAll('select').length === 0`). Form,
Dosage, and Quantity are all the same custom `div[role="listbox"]` widget
(`aria-label="Select dosage"` / `"Select quantity"`, etc., each with
`[role="option"]` children). The dosage-selection code already in this
file before quantity was ever added used `Select(dropdown)` against
`select[name*=dosage]`/`select[name*=strength]`/`select` — since none of
those could ever have matched anything, dosage selection had silently
never worked at all, on any run; a requested dosage mismatch could be
*detected* but never actually *corrected*, contrary to what the
surrounding code's structure implied.

Fixed for both together: clicking the widget's `.custom-select__trigger`
child specifically (not the outer `[role="listbox"]` wrapper — confirmed
live, clicking the wrapper itself left `aria-expanded="false"`; only the
inner trigger opens it) reveals `[role="option"]` children, one of which
is clicked directly. Verified live end-to-end: this closes the dropdown
and immediately re-renders prices, no separate "confirm" step needed
(unlike GoodRx's modal-based equivalent — see
[What quantity is this pricing for?](#what-quantity-is-this-pricing-for)).
Both dosage and quantity now get the same after-the-fact re-check ZIP
already had — a caveat is added if the requested value couldn't be
confirmed afterward, rather than trusting the click blindly — and
whatever quantity actually ends up displayed is now reported in every
result's `Quantity` column, which no SingleCare result populated at all
before this.

**Reported live, right after this shipped: results came back with the
wrong ZIP even though ZIP entry had just succeeded moments earlier in the
same run.** Confirmed from a debug dump of the exact run: the ZIP-entry
code itself was unchanged and had already worked — the regression was
the dosage-selection call immediately *after* it. That call ran
unconditionally, on every single `refine()` invocation, even when the
*only* actual reason `refine()` had been invoked was a ZIP mismatch with
dosage already matching the page's default. Re-clicking a listbox to
reselect the value it's already showing turned out not to be a
no-op — it resets other page state, including the ZIP just set moments
before. Fixed two ways: dosage/quantity selection is now gated on an
actual mismatch (nothing to reselect if it already matches — which alone
would have prevented this specific regression, since dosage matched
already), and, more fundamentally, the order was flipped so ZIP is
applied *last*, after any dosage/quantity change — the same "whichever
action can invalidate another must run first" principle
`goodrx_scraper.py`'s ZIP-before-dosage/quantity ordering already
follows, for the mirror-image reason (there, ZIP entry can relaunch the
driver and wipe dosage/quantity; here, dosage/quantity selection wipes
ZIP instead). This way ZIP's own post-check is always the true final
state, regardless of whether dosage/quantity also needed changing in the
same run.

**Reported live, right after *that* fix shipped: SingleCare returned no
rows at all.** Confirmed from a debug dump of the exact run
(`page_singlecare_no_rows.html`): the page wasn't blocked, challenged, or
erroring — it was still showing its loading skeleton (`sc-loader`,
`pharmacy-item--loading` placeholders with empty names/prices) at the
moment it was read. A separate debug dump from earlier in the *same* run
(`page_singlecare_after_challenge.html`, saved before ZIP/dosage/quantity
refinement) confirms real rows genuinely were showing at that point — 6
of them — so this wasn't a "never loaded" failure, it was read at the
wrong moment. Root cause: the wait before extraction only checked once
for *any* `"$"` anywhere in the page's body text, not for the actual
pharmacy rows — the exact "checked before it settled" bug class already
fixed elsewhere in this project (Cost Plus Drugs' price, GoodRx's
dosage/quantity confirm, this file's own challenge re-check), just not
yet applied here. A real, stale price left over from *before* the
ZIP/dosage/quantity change finished re-rendering satisfied that single
check immediately, a moment before the page cleared it to show the
loading skeleton for the refreshed state. Fixed the same way as Cost
Plus Drugs' equivalent: poll for `_extract_pharmacy_rows()` itself, not
just a bare `"$"`, and require two reads ~0.5s apart to return the same
row count before trusting either one.

**Found in a later code review: `_loose_matches()` here had the same
substring-collision bug as GoodRx's identical helper** — see [Matching
dosage, quantity, and form](#matching-dosage-quantity-and-form-a-substring-collision-bug-shared-across-four-files)
— now fixed via the same shared `utils.token_matches()`. **Also added in
the same pass: real `--formulation` selection**, not just a caveat — this
page's "Select form" listbox is the exact same custom-listbox widget
already confirmed live for dosage/quantity above, so formulation is now
selected the same way, run before dosage/quantity/ZIP — see
[Formulation: is this actually the requested
form?](#formulation-is-this-actually-the-requested-form) for the reasoning
and what's and isn't independently confirmed live about it. Every click
in this file also now goes through the shared `driver_utils.robust_click()`
described at [Clicking through overlays](#clicking-through-overlays-a-shared-helper),
closing a latent gap (this file previously had no defense at all against
a click being intercepted by something drawn on top of it) rather than
responding to a confirmed SingleCare-specific failure.

### SingleCare: Member Bonus price was silently wrong, not just incomplete

Reported: some SingleCare pharmacies show a "Member Bonus" tag — a free,
opt-in signup discount (unlike GoodRx's paid Companion membership) — and
the price without it, visible in a hover tooltip, wasn't being shown at
all. Investigating turned up a worse, pre-existing bug in the same code
path: confirmed live against the real page, the price *already being
returned* for every Member Bonus pharmacy was actually the wrong one —
the higher, no-signup price, not the lower one the page actually displays
as that pharmacy's price. Not a rounding or formatting issue: for one row
that reads "$10.91" on the page, this code was returning "$13.91".

Root cause: price extraction searched the row's whole flattened text for
the first "$X.XX" it could find. The no-signup price lives in
`.cutPriceNew`, a struck-through note that — confirmed live via the raw
DOM — appears *earlier* in the row's markup than the actual displayed
price (`.pharmacy-item__price.bonusPrice .pharmacy-item__price`), even
though it's not the number the page leads with. A first-match search over
the concatenated text had no way to tell them apart and always took the
first (wrong) one, on every Member Bonus row.

Fixed, both verified end-to-end against real HTML pulled directly from a
live lookup (decoded and run through the actual extraction function, not
just inspected): price now comes from that specific bonus-price element,
falling back to the old whole-text search only for rows with no
Member-Bonus markup at all (where there's nothing else in the row to be
confused with a price) or when the speculative fallback selectors further
down `PHARMACY_ROW_SELECTORS` are in use. When `.cutPriceNew` is present
and non-empty, its price is now returned as a second result, labeled
`cash/coupon price (standard, no Member Bonus signup)` — same "show both,
name them honestly" approach used for GoodRx's Companion/standard prices,
though here the sorting/selection of which pharmacies make the top-5 list
is left alone: since the Member Bonus is free to everyone, showing it as
the primary price isn't the same "artificially inaccessible" problem
GoodRx's paid Companion price was.

### Amazon Pharmacy: wrong search URL entirely

Reported live: Amazon Pharmacy returned nothing at all, every time —
unlike every other bug in this README, not a wrong selector on an
otherwise-correct page, but the wrong page from the start. `SEARCH_URL`
(`https://www.amazon.com/primerx/search/?searchTerm=...`) was never
Amazon Pharmacy's own drug search. Confirmed from a saved copy of the
actual result page (provided directly, not scraped — Amazon can't be
tested live here without signing in, and that's off-limits): the session
was correctly signed in ("Hello, Marek", "Deliver to Marek Minneapolis
55406"), but the page itself was Amazon's **"Prime Rx"** — a discount
card for filling prescriptions at *other* pharmacies (CVS, Walgreens,
Rite Aid, etc.), administered by Inside Rx. It's a real, working page —
just a completely different product. The requested drug name appeared
zero times anywhere in its actual rendered content, only inside an
unrelated sign-in-tooltip URL — there was never a product card or price
to extract, regardless of how `RESULT_CARD_SELECTORS` was tuned.

Confirmed from a second saved copy, this time of a real search performed
by hand on `www.amazon.com` directly (same method): Amazon Pharmacy's
actual prescription items show up as ordinary results in a **plain
Amazon.com product search** (`https://www.amazon.com/s?k=...`), mixed in
among unrelated products — in one real search for "lisinopril", 6 of 18
results were genuine Rx pricing cards (one per strength: 2.5mg, 5mg,
10mg, 20mg, 30mg, 40mg) and the other 12 were books, supplements, an
"Alexa device" listing, and unrelated health services. What distinguishes
a real one, confirmed live: a two-tier price block reading "Average
insurance price" (a rough, insurance-dependent estimate) followed by
"Without insurance" (the plain cash price) — nothing else in the same
search results has this. `RESULT_CARD_SELECTOR` now targets
`div[data-component-type="s-search-result"]` (confirmed real markup for
any Amazon.com search, not pharmacy-specific), and each card is only
treated as a genuine price row if it contains that specific price block;
everything else is skipped rather than misread as a price.

This surfaced a second, related problem the old top-N-by-price selection
would have had here too (the same class of bug fixed for GoodRx above):
a single drug-name search returns one card *per strength* side by side —
so without filtering, results could easily mix up e.g. a 5mg price with
a 40mg request. Fixed: each card's form/strength/supply line (e.g.
"Tablet · 20mg · 30 days supply" — confirmed live, its own row inside the
card) is checked against the requested `--dosage`, and only matching rows
are returned. If a dosage was requested and nothing matches, that's
reported as an error listing the strengths that *were* found, rather than
silently substituting a different one's price. Both the cash price and
the insurance-estimate price were originally returned per matching row —
labeled distinctly, the same "show both, name them honestly" pattern
already used for GoodRx's Companion/standard prices, rather than only the
cheaper of the two. The insurance-estimate row is now filtered out of
the final output by default — see [Results that require insurance are
filtered out](#results-that-require-insurance-are-filtered-out) — but
this scraper still produces it internally; the filtering happens
centrally in `main.py`, not here.

**Reported live: searching for a single drug (e.g. "atorvastatin")
returned combination-drug products too** — e.g. "Amlodipine -
Atorvastatin", a genuinely different drug that merely contains the
searched name as a substring within a longer one. Confirmed from a fresh
debug dump of the exact search: nothing in this method had ever checked
drug *identity*, only dosage — a plain Amazon.com product search
genuinely interleaves single-ingredient and combination listings for the
same searched ingredient in the same result set (9 real Rx-price cards in
one search: 4 for plain atorvastatin at different strengths, 5 for
various Amlodipine-Atorvastatin combinations), so every combination
product silently passed straight through the existing (dosage-only)
filter. Confirmed live, the real title text distinguishing the two: a
single-ingredient listing's title is just the drug's own name, optionally
followed by a "(Generic for ...)" suffix — "Atorvastatin (Generic for
Lipitor)" — while a combination listing joins multiple active ingredients
with " - " before any such suffix — "Amlodipine - Atorvastatin" /
"Amlodipine - Atorvastatin (Generic for Caduet)". Fixed with
`_matches_drug_name()`: strips off any "(...)" suffix, then requires the
requested drug name to loosely match the *entire* remaining name, not
just appear somewhere within it. Applied as a hard exclusion with no
"fall back to everything if nothing matches" case (unlike the dosage
filter right after it) — showing a different drug entirely is worse than
showing nothing, whereas a dosage mismatch is still the *same* drug, just
not the requested strength. Verified directly against the real debug dump
from the reported run: correctly keeps all 4 genuine atorvastatin
listings and excludes all 5 combination-drug ones.

**Reported live: searching for "amlopidine" (a typo — missing letter
transposition of the real "Amlodipine") reported "only combination
products containing it were found", even though the debug page it
pointed at plainly showed a real, single-ingredient "Amlodipine
(Generic for Norvasc)" listing.** `_matches_drug_name()`'s exact-match
check itself did exactly what it's supposed to here — "amlopidine" and
"amlodipine" are genuinely different strings, and loosening this to a
fuzzy/typo-tolerant match would reopen the exact wrong-drug risk this
filter exists to close (real drug names really can be a letter or two
apart — that's what motivated the exact match to begin with). The bug
was the *message*: it named "combination products" as the reason
nothing matched unconditionally, because that's what was true of the
one case this filter was originally written for, without actually
checking that it was true this time too. Fixed by reporting what was
*actually* found instead of asserting why, the same "available: ..."
shape already used for the formulation/dosage mismatches right below
it — the message now lists every title seen (e.g. the real "Amlodipine
(Generic for Norvasc)" would show up right there), so a typo like this
one is obvious at a glance instead of being misattributed to the wrong
cause. Verified directly: `_matches_drug_name("Amlodipine (Generic for
Norvasc)", "amlopidine")` correctly returns `False` (this is not a bug
to "fix" into matching), while the same call with the correctly-spelled
"amlodipine" returns `True` — the identity check was always right; only
the explanation shown when it (correctly) rejects something needed
fixing.

**Found in a later code review: `_matches_dosage()` had the same
substring-collision bug documented at [Matching dosage, quantity, and
form](#matching-dosage-quantity-and-form-a-substring-collision-bug-shared-across-four-files)
— a requested "20mg" matched a strength_text containing "120mg".**
Fixing it here specifically needed one extra step the other three
sites didn't: `strength_text` is a whole compound string ("Tablet ·
20mg · 30 days supply"), and the dosage is its *middle* "·"-separated
segment, not the leading token `utils.token_matches()` checks for a
number in — throwing the whole string at that function still let the
collision through on the first attempt, caught by a test written
specifically to check for it. Fixed by extracting just the dosage
segment before comparing. **Also added, in the same pass: a real
`--formulation` filter,** something this file never had before —
`strength_text`'s own *leading* segment already states the form (e.g.
"Tablet"), so no new selector was needed, just reading a field this
file already scrapes. Rows whose form doesn't match are excluded, with
a clear error listing the forms that were found if none match at all —
see [Formulation: is this actually the requested
form?](#formulation-is-this-actually-the-requested-form) for how this
compares to the other three sites.

Not addressed here, and pre-existing rather than newly introduced: `--zip`
is accepted but not enforced for Amazon Pharmacy — delivery location comes
from whatever address is already on the signed-in account, not from a
per-lookup override, and nothing currently re-checks or flags that the way
it does for GoodRx/SingleCare's ZIP entry.

### Only one CAPTCHA prompt at a time

`main.py` runs sites concurrently by default (non-`--debug` mode), so
GoodRx and SingleCare could in principle both need solving around the same
time — two threads printing/`input()`-ing at once, which would interleave
into an unreadable mess and possibly pop two Chrome windows competing for
the same terminal prompt. `driver_utils.INTERACTIVE_CHALLENGE_LOCK`
prevents that: both scrapers' `_resolve_bot_challenge()` acquire it before
printing/prompting, so only one challenge's *terminal prompt* is ever
presented at a time. Whichever scraper hits its challenge first holds the
lock until you resolve (or skip) it; the other's challenge-handling simply
waits its turn — whichever of the two happens to hit its own challenge
first, in a given run (both go straight to Selenium now — see [How it
looks up prices](#how-it-looks-up-prices)).

**Reported live: a second (GoodRx) challenge window opened, showing its
own captcha, while a first (SingleCare) one was still unresolved** — the
terminal prompt for GoodRx hadn't actually jumped the queue (that part
was, and still is, correctly serialized), but a real, visible Chrome
window rendering GoodRx's own captcha popped up regardless, which looks
exactly like the script moving on without waiting. Root cause:
`goodrx_scraper.py`'s ZIP entry and dosage/quantity-edit actions both
preemptively switch from headless to a visible window *before* even
checking whether a challenge is showing — confirmed live, ZIP submission
and a confirmed dosage/quantity change both reliably trigger their own
"Press & Hold" challenge, so this switch happens ahead of time rather
than reactively (see [GoodRx: ZIP code](#goodrx-zip-code)). That
switch-and-check block was never wrapped in
`INTERACTIVE_CHALLENGE_LOCK` at all — only the nested
`_resolve_bot_challenge()` call inside it was — so opening the window
(and whatever it immediately renders) wasn't serialized against another
site's in-progress challenge, only the terminal prompt after it was.

Fixed by wrapping the entire switch-and-check block in the lock too, not
just the nested call — which meant switching `INTERACTIVE_CHALLENGE_LOCK`
from a plain `threading.Lock` to a `threading.RLock`: a plain `Lock`
isn't reentrant, so the same thread wrapping that whole block *and* then
calling `_resolve_bot_challenge()` (which independently acquires the same
lock) would deadlock itself the moment a challenge actually needed
solving. An `RLock` lets the same thread re-acquire it as a no-op while
still correctly blocking every *other* thread until the whole nested
sequence finishes — verified with a small threading test simulating
exactly that nested-acquire-by-one-thread-while-another-waits shape
before considering this fixed, not just reasoned through. This is
automatic; there's nothing to configure.

Still true under the GUI (see [GUI](#gui) above), just with a different
front end for the actual prompt: `INTERACTIVE_CHALLENGE_LOCK` guarantees
only one challenge is ever pending at a time regardless of which
`ChallengeNotifier` is installed, which is exactly why `gui.py`'s browser
modal can get away with tracking a single shared pending-message/event
pair instead of something keyed per-challenge.

### Cost Plus Drugs: switched to their own public API

`costplusdrugs_scraper.py` no longer drives a browser at all. Researched
directly (all four sources, in parallel) when asked to replace scraping
with real API calls wherever possible:

| Site | Public API? | Why it could/couldn't switch |
|---|---|---|
| **Cost Plus Drugs** | Yes — free, no signup, no key | Documented at [github.com/CostPlusDrugs/apidocs](https://github.com/CostPlusDrugs/apidocs) / [costplusdrugs.github.io/apidocs](https://costplusdrugs.github.io/apidocs/); verified live before switching (see below) |
| GoodRx | Real API exists (`/v2/price/compare`, `/v2/coupon`) | Gated behind a "API Partnership Program" application — GoodRx review, likely a sales call, a distributorship agreement; reported terms require consumer-facing product integration and GoodRx's prior written consent for non-commercial use. No self-serve key. Still scraped (see below) |
| SingleCare | Real API exists (RxSense Consumer API) | Docs and portal are login-gated; access is provisioned by an RxSense account manager, no public signup or pricing. Still scraped (see below) |
| Amazon Pharmacy | None | Prescription drugs/Amazon Pharmacy items are explicitly listed as **excluded** from Amazon's own Product Advertising/Creators API policy. The only Pharmacy API integration that exists is a closed enterprise partnership (payers, manufacturers, digital-health platforms), no individual-developer path. Still scraped (see below) |

So Cost Plus Drugs is the only one of the four where this was actually
possible — the sections below through "Cost Plus Drugs: stable-price
extraction" describe the **retired Selenium-based implementation**,
kept for historical record; none of the selectors/buttons/functions
they describe (`resolve_and_select()`, `STRENGTH_BUTTON_PREFIX`,
`_wait_for_stable_price()`, etc.) exist in the current
`costplusdrugs_scraper.py` any more.

**The new implementation**, verified live against the real endpoint:
a plain `GET` to `https://us-central1-costplusdrugs-publicapi.cloudfunctions.net/main`
with `medication_name=<drug>` returns every NDC/strength/form Cost Plus
Drugs carries for that name; `formulation`/`dosage` are matched against
the response's `form`/`strength` fields with the same `token_matches()`
used everywhere else in this project (hard-excluding on a mismatch and
listing what's actually available, same as every other site's
identity/dosage/formulation filters — showing the wrong strength is
worse than showing nothing). A second request,
`ndc=<matched NDC>&quantity_units=<N>`, returns `requested_quote` — an
exact price for that exact pack size, not a rescaled estimate. Confirmed
live: `ndc` for lisinopril 20mg with `quantity_units=30` returned
`"$5.55"` — the *exact* dollar figure the old Selenium scraper's own
confirmed-live comment recorded reading directly off the real page's "A
30 count supply of 20mg Lisinopril will cost: ... $5.55" sentence, and
the API accepts arbitrary quantities (`quantity_units=45` returned a
real `"$5.83"`, not just the page's own preset 30/60/90 buttons) — a
genuine capability improvement over clicking through fixed on-page
buttons.

Two honest caveats built into every result rather than glossed over:
- **No `--quantity` given** → defaults to a fixed, clearly-labeled `30`
  (`"30 count (assumed default quantity, not requested)"`), since the
  API has no "what's this drug's own default pack size" field the way
  the old scraper could read straight off the page's own sentence (that
  default varies by drug, confirmed live, not always 30) — rather than
  silently presenting an assumption as if it were Cost Plus Drugs' own
  stated default.
- **Shipping is never included in `price`.** The old scraper folded in
  a flat "Standard Shipping" fee it re-read live off the page every time
  (confirmed $5.25 as of that version). This API doesn't expose that fee
  at all, and hardcoding the old confirmed number here would silently go
  stale the moment Cost Plus Drugs changes it — so instead, `price_label`
  says outright that shipping is excluded and charged separately at
  checkout, rather than quietly reproducing a number this version has no
  way to keep in sync.

`zip_code` remains accepted-but-unused in `get_prices()`'s signature,
same as before this switch — for call-signature parity with the other
three sites' `get_prices()`, not because Cost Plus Drugs' flat mail-order
pricing varies by location.

### Cost Plus Drugs: search, then strength/quantity selection *(retired — historical)*

Previously built directly from drug name + dosage + form
(`costplusdrugs_slug()`: `{name}-{strength}-{form}`) — no failure was
actually confirmed for Cost Plus Drugs specifically (its slug matched the
plain drug name correctly for atorvastatin, the case that motivated this
across all three sites), but the same class of risk applies to any site
whose URLs encode a specific canonical drug name, so this closes it here
too rather than waiting for a confirmed failure first — see [Drug name →
URL](#drug-name--url-via-each-sites-own-search-not-a-guessed-slug) above.

Confirmed live end-to-end: the homepage's "search bar" is actually a
plain `<button data-testid="medications-search-trigger-button">` that
opens a dialog containing the real input (`#search-overlay-dialog
#search`) — typing a drug name renders a "Medication Results" section
with real `<a href="/medications/{slug}/">` links. `resolve_and_select()`
clicks the first one, scoped to that dialog and matched by
`_is_real_drug_link()` (see below) rather than relying on that heading's
exact DOM position, which wasn't confirmed reliably live — the same
"trust the top-ranked result" approach used for SingleCare/GoodRx.

**Found while investigating a separate report, not reported directly:
this resolution failed for *every* drug, on every run — a total,
systemic failure, not an occasional wrong pick.** Confirmed from a fresh
debug dump (saved by this method's own no-match branch): a real
"Atorvastatin (Generic for Lipitor)" result link was genuinely present in
the dialog, but never got matched. Root cause: the original version
distinguished a real result link from the bare "/medications/" link (and
from an unrelated "popular products" pill list elsewhere on the page,
also confirmed present in that same dump —
`data-testid="products-pill-{slug}"`, matching the identical
`/medications/{slug}/` shape) by counting URL path segments, assuming
`element.get_attribute("href")` always returns a fully-resolved absolute
URL (5 segments: scheme, empty, host, "medications", slug). It didn't —
it returned the raw relative attribute instead (3 segments), so *every*
real result link failed that segment-count check, always, regardless of
which drug was searched for. Fixed: `_is_real_drug_link()` matches by
regex on whatever form the href actually is (side-stepping the
get_attribute ambiguity entirely, rather than depending on which one it
turns out to be), explicitly excludes both the bare link and the
"products-pill-*" pattern, and the element search itself is now scoped
to the dialog specifically so it can't match that unrelated pill list —
or anything else outside the dialog — in the first place.

**Reported live again shortly after that fix shipped ("costplusdrugs is
not working") — same symptom, different root cause.** Confirmed from
another fresh debug dump: the correct atorvastatin result link was, once
again, genuinely present in the dialog by the time the failure branch
saved its snapshot — replaying that exact markup through
`_is_real_drug_link()`/`DRUG_LINK_PATTERN` directly confirmed the matcher
itself picks it out correctly. So the matching logic wasn't the bug this
time; the wait around it was. The fix above used
`WebDriverWait(...).until(lambda d: next(a for a in d.find_elements(...)
if _is_real_drug_link(a)))` — but Selenium's `WebDriverWait` only
auto-ignores `NoSuchElementException` by default, not
`StaleElementReferenceException`. This search dialog re-renders its
suggestion list on every keystroke (the drug name is typed via
`send_keys()`, i.e. character by character), so it's easy for an element
handed out by one `find_elements()` call to go stale before
`_is_real_drug_link()` finishes reading its attributes on it — when that
happened, the exception propagated straight out of `.until()` and killed
the whole wait immediately, well short of its real timeout and well
before the DOM had actually finished settling. Fixed by replacing the
`WebDriverWait`/lambda with an explicit poll loop (the same "read twice,
must match" stability pattern already used for the suggestion-click
fixes in `goodrx_scraper.py`/`singlecare_scraper.py`) that treats any
per-iteration exception as "not settled yet, keep polling" instead of
letting it abort the wait, and only clicks once the set of matching
hrefs reads identically on two consecutive passes.

**Reported live a third time, with an exact error message this time:**
`found a search result for 'atorvastatin' but could not click it: ...
no such element: ... {"method":"css selector","selector":"a[href="
https://www.costplusdrugs.com/medications/atorvastatin-10mg-tablet/"]"}`.
This was the stability-check fix above working correctly, then hitting a
second, narrower race right after: `link.click()` itself raised (most
likely `StaleElementReferenceException` from one more re-render slipping
in between the stability check passing and the click happening), so
control fell into a fallback that tried to re-find the same element via
a CSS selector built from the href string captured a moment earlier. The
error message gives the root cause directly: that selector's value was
the *absolute* URL (from `get_attribute("href")`), but a CSS attribute
selector matches the literal HTML attribute — which was apparently
relative here — so the selector could never match anything, "no such
element" every time, regardless of whether the link was actually there.
This is the exact same `get_attribute("href")` relative-vs-absolute
ambiguity already documented above for the original segment-counting
bug, just biting a second, different piece of code (this fallback,
rather than the matcher). Fixed by not trying to relocate the element by
its href string at all — instead, on a click failure, the code re-runs
the same find-and-filter query fresh (which always returns live,
non-stale elements as of that instant) and clicks immediately, up to a
few times.

**Reported live a fourth time, with the actual error attached this
time — and a genuinely different bug, not another stale-element race:**
`element click intercepted: ... Other element would receive the click:
<div id="silktide-backdrop" ...>`. A cookie-consent widget's (Silktide)
backdrop `<div>` was sitting on top of the whole page. Selenium's native
`.click()` refuses to click anything that isn't the actual topmost
element at that screen pixel — so plain retries of the same native click
(the previous fix) would fail identically every single time the backdrop
is present, no matter how many attempts; retrying was never going to
help here. Fixed by falling back to a JS-dispatched click
(`execute_script("arguments[0].click()", element)`) when the native
click fails — that invokes the element's own click handler directly and
is unaffected by whatever else is visually drawn on top of it, so
there's no need to make any decision about the cookie-consent widget
itself (accept/decline/dismiss) at all; this project never needs to
interact with it, just get past it for what's otherwise a plain,
already-intended click on a real search result.

That link only confirms the correct base drug name, though — confirmed
live, it always lands on the drug's *default* dosage page (e.g.
"atorvastatin-10mg-tablet" regardless of what dosage was searched for),
not the specific one requested. Reaching that uses a second confirmed-live
mechanism: plain `<button data-testid="strength-selection-{dose}">` /
`"quantity-selection-{count}">` elements on the landed page — not a
dropdown — which live-update the price (and, for strength, the URL
itself) immediately on click, no separate confirm step. Matched by
visible button text rather than constructing the `data-testid` value
directly, since this project's dosage strings aren't guaranteed to
exactly match Cost Plus Drugs' own token formatting (e.g. "2.5mg"). If no
strength button matches the requested dosage, that's flagged in
`price_label` as a caveat rather than silently returning the default
dosage's price as if it were the one requested — quantity has no such
caveat, since unlike dosage there's always a sensible default price to
fall back to reporting, matching the tolerance already used for optional
quantity elsewhere in this project.

**Reported live a fifth time: a "could not select dosage 40mg (no
matching strength button found)" caveat for a drug page that visibly had
a "40mg" strength button right on it.** Confirmed by inspecting the live
page directly: a `strength-selection-40mg` button really was present,
correctly labeled "40mg", and fully visible/clickable — so, again, the
matching logic itself was fine. Root cause, the same "checked before it
settled" bug class recurring in a new spot: the code only waited for the
URL to change to a `/medications/...` page after clicking the search
result — it never waited for this button row itself to actually finish
rendering, and React's client-side render of that row can lag a beat
behind the URL update. Calling `_find_and_click_variant_button()` once,
immediately, could find zero `[data-testid^="strength-selection-"]`
elements yet and report a false "not found" for a button that was
genuinely about to appear a moment later. Fixed by having that function
poll for up to `Config.SELENIUM_WAIT_TIMEOUT` seconds instead of
checking exactly once — same treatment as every other "trust the first
read" bug fixed elsewhere in this project.

**That poll fix wasn't the whole story — reported live again ("still
shows 'no matching strength button found'... also shows 'estimated for
qty 90' even though there is a Select Quantity button"), and this time
reproduced directly** by driving the real flow end-to-end (search →
click result → attempt strength/quantity selection) with the project's
own Chrome setup and inspecting state at each step. The strength buttons
were, again, genuinely present, visible, and correctly matched by text —
confirmed by counting them via the same selector this function uses,
immediately before the click. The actual cause: the Silktide
cookie-consent backdrop responsible for the "element click intercepted"
bug already fixed for the search-result-link click (see above) isn't
scoped to the search dialog at all — it's a site-wide overlay that
persists across the client-side route change onto the drug page itself,
confirmed still present (`document.querySelector('#silktide-backdrop')`
truthy) at the exact moment this function tries to click a strength
button. `_find_and_click_variant_button()`'s `b.click()` is a native
click, which that backdrop intercepted every time — silently, because
its broad `except Exception: pass` swallowed the interception error
the same way it would swallow a genuine "not found," so it just polled
until timeout and reported a false negative for a button that was
right there the whole time and simply never successfully clicked. The
same mechanism silently broke quantity selection too, which is why
`--quantity 90` fell through to `main.py`'s rescale-and-label-as-estimate
path instead of the site actually pricing 90 directly — not a second,
unrelated bug, just the same one showing up twice in one run. Fixed by
applying the same native-click-then-JS-fallback already used for the
search result link here too, and reproduced the exact previously-failing
call afterward to confirm: resolved straight to
`atorvastatin-40mg-tablet` with no dosage caveat, `$7.17` for "90 count"
directly from the site, no rescaling needed.

**Requested directly, not a bug report: the price calculator panel also
has a "Select Form" selector above "Select Strength"/"Select
Quantity", for a plain `<button data-testid="form-selection-{Value}">`
(e.g. "form-selection-Tablet") — the same shape as strength/quantity —
and `--formulation` wasn't wired to it.** `resolve_and_select()` now
selects it too, via the same `_find_and_click_variant_button()` used for
strength/quantity (so it already gets the same stability-poll and
click-interception handling above for free), clicked first — matching
the page's own top-to-bottom order (form, then strength, then
quantity) — since it's unconfirmed whether picking form after strength
could ever reset strength, and there's no reason to risk that when
matching the page's own order costs nothing. Not finding a matching
form button gets the same "flag, don't silently substitute" treatment
as dosage: both are collected into one combined caveat string (e.g.
"could not select form capsule (no matching form button found); could
not select dosage 999mg (...)") rather than each getting its own
separate field, since they're the same category of issue. Quantity
still gets no such caveat, for the same reason as before — there's
always a sensible default price to fall back to reporting. Verified
live end-to-end for both the matching case (formulation "tablet" reaches
"atorvastatin-40mg-tablet" with no caveat, same result as before this
was added) and the mismatched case (formulation "capsule" against a
drug that's Tablet-only correctly produces the form caveat, combined
correctly alongside a simultaneous dosage-mismatch caveat).

**Requested directly: also capture the shipping cost the page itself
discloses.** Confirmed live: the price breakdown section has its own
"Standard Shipping *Additional cost at checkout" line followed by a
dollar amount ($5.25, confirmed unchanged across 30/60/90-count for the
same drug — a flat fee, not scaled by quantity, at least in what's been
observed) — and confirmed separate from, not included in, "Your Drug
Price With Us" above it (its Manufacturing + Markup + Pharmacy Labor
breakdown already sums to that headline number on its own, without
shipping). `_extract_shipping_cost()` pulls that fee out of the same
stable page text `extract_price()` already reads (bounded to 100 chars
after the label rather than an unbounded/greedy match, so it can't run
past an unrelated later `$` if the page's wording ever shifts), and
`get_prices()` adds it directly into the reported `price` — per explicit
direction, not tracked as a separate field — with `price_label` gaining
an "includes $X.XX shipping" note so the total is never silently
different from what the page's own headline number shows without
explaining why. Verified live end-to-end: 90-count atorvastatin's
`$7.17` drug price plus its disclosed `$5.25` shipping correctly reports
as `$12.42`, labeled "cash price (no insurance) — includes $5.25
shipping".

Also confirmed live while building this: Cost Plus Drugs' own plain-fetch
path is now blocked outright too (matching what was already true for
SingleCare) — so, same as there, this whole flow is Selenium-only now,
not a fallback behind a plain-fetch fast path that no longer succeeds in
practice.

**Reported live once more: `could not open/type into Cost Plus Drugs'
search: ... element click intercepted ... Other element would receive
the click: <div id="silktide-backdrop" ...>`.** The exact same
cookie-consent overlay already confirmed and fixed for the strength/
quantity/form buttons and the search-result-link click earlier in this
section — just at a click site those fixes never touched: the very
first click of the whole flow, opening the search dialog itself
(`SEARCH_TRIGGER_SELECTOR`). The native-click-then-JS-fallback pattern
had only been applied to the clicks that had actually been reported
failing at the time; this one hadn't been, so it was still a bare
`trigger.click()` with nothing to fall back on. Same fix applied here
too. Verified live end-to-end afterward: a plain lisinopril lookup
resolves and prices correctly (`$10.80`, "30 count", shipping included
per the note above) with no error.

**Found in a later code review, two more issues in this same area:**
the strength/quantity/form button-matching logic (`_find_and_click_variant_button()`)
had the same substring-collision bug already documented for
dosage/quantity matching generally — see [Matching dosage, quantity, and
form: a substring-collision bug shared across four
files](#matching-dosage-quantity-and-form-a-substring-collision-bug-shared-across-four-files)
— now fixed via the same shared `utils.token_matches()`. And the three
independently-added native-click-then-JS-fallback copies in this file
(the button click above, the search-trigger click, and the search-
result-link click) are now one shared `driver_utils.robust_click()`
instead — see [Clicking through overlays: a shared
helper](#clicking-through-overlays-a-shared-helper). Both re-verified
live end-to-end after the refactor: same `$10.80`/`$12.42` results as
before, unchanged.

### Cost Plus Drugs: stable-price extraction *(retired — historical)*

Cost Plus Drugs' medication page is client-rendered (Next.js) with no
data-testid/class hook on the price itself — just plain Tailwind utility
classes — so there's no selector to wait on, only the appearance of a `$`
amount in the rendered text. Getting this reliable took fixing two distinct
issues, both confirmed live in `costplusdrugs_scraper.py`:

1. **The price can flash into the DOM and disappear again a moment
   later**, consistent with the site's bot-scoring intermittently killing
   an in-flight retry of its own price-data fetch mid-render. Accepting the
   very first appearance was catching that flash rather than a genuinely
   loaded price. Fix: `_wait_for_stable_price()` requires the price to
   still be present on a second check ~1s after the first before accepting
   it, and keeps polling (up to `SELENIUM_WAIT_TIMEOUT`) if it vanishes.
2. **`driver.page_source`, read right after confirming the price via
   `.text`, doesn't always reflect it** — a lag between the live rendered
   DOM and the serialized snapshot Selenium hands back, specific to this
   page. Selenium's `.text` was reliably right; `page_source` parsed via
   BeautifulSoup afterward sometimes wasn't. Fix: extract the price
   directly from the same `.text` that was just confirmed stable, instead
   of re-parsing a separate `page_source` snapshot that can trail behind it.

### Cost Plus Drugs: static-formulary pre-check (`COSTPLUSDRUGS_LOOKUP_MODE=file`)

Cost Plus Drugs publishes a downloadable "Team Cuban Card" medication list —
an .xlsx of every drug/strength/form it carries. **It has no pricing at
all**, so it can never replace the live API call above; what it's good for
is a near-instant pre-check that skips the live call entirely for drugs
that aren't carried in the first place. This was originally a meaningful
performance win (skipping a many-second Selenium page load); now that the
live lookup is a cheap JSON request, it's more "confirm it's on our own
formulary list" than a real speed optimization, but nothing about the
switch to the API changed this feature, so it's unchanged here.

With `COSTPLUSDRUGS_LOOKUP_MODE=file`:
1. `costplusdrugs_formulary.py` loads the spreadsheet (`COSTPLUSDRUGS_FORMULARY_PATH`)
   and checks whether your `--drug`/`--dosage`/`--formulation` combination is
   listed at all (drug name matched via the same slug logic used for URLs,
   strength via whitespace/case-insensitive comparison, form via prefix —
   e.g. `tablet` matches the file's `Tablet Delayed Release`).
2. **Not listed** → returns immediately with "not carried by Cost Plus
   Drugs", no network activity at all.
3. **Listed** → falls through to the exact same live API call described
   above, since the spreadsheet can't tell you the price — only that it's
   worth asking.

The default (`COSTPLUSDRUGS_LOOKUP_MODE=live`) skips this file check
entirely and always queries the API, matching the original behavior. The
spreadsheet is dated (`Team Cuban Card` "Last Updated" note inside the file
itself) — Cost Plus Drugs' actual catalog can drift from it over time, so
treat a "not carried" result from file mode as best-effort too, same as
everything else in this project.

## Deploying to Render.com (Cost Plus Drugs only)

Asked directly whether/how to deploy this on Render — the honest answer
depends entirely on which sites are in play. GoodRx, SingleCare, and
Amazon Pharmacy are Selenium-based and their bot-checks sometimes need
an actual human to solve a CAPTCHA in a real, visible Chrome window on
the same machine (see [GUI](#gui) above) — confirmed directly:
`gui.py`'s "Done" button just tells the scraper a human solved it
somewhere; it never streams or shows the browser itself. On a headless
Render instance there's no display for that window to render on and no
way for you to see it through the page, so those three genuinely can't
work unattended on a host like this without a real rework (streaming
the browser to the page, e.g. embedding noVNC — not attempted here).

**Cost Plus Drugs has none of that problem** — it's a plain, free,
public HTTP API call (see [switched to their own public
API](#cost-plus-drugs-switched-to-their-own-public-api) above), so a
Cost-Plus-Drugs-only deployment is genuinely straightforward. Two things
had to change in `gui.py` to make restricting it to just that site
actually safe, not just cosmetic:

- The site checkboxes used to always render and accept all four sites
  (`Config.ALL_SITES`) regardless of `ENABLED_SITES` — a client could
  still `POST /api/search` for `goodrx` even if the page only showed
  `ENABLED_SITES=costplusdrugs`'s checkbox. Both the rendered checkboxes
  and the server-side site filter now use `Config.ENABLED_SITES`
  instead, so a disabled site is rejected server-side, not just hidden
  from the UI.
- `/api/setup-amazon` always ran Amazon's interactive Selenium login
  flow regardless of `ENABLED_SITES` — now returns a plain 403 unless
  `amazon` is actually enabled, and the "Set up Amazon login…" button is
  omitted from the page entirely in that case.
- `gui.py` used to hard-code `host="127.0.0.1"` and `port=5057`, and
  always tried to auto-open a local browser tab — none of which works
  on a cloud host. It now binds `0.0.0.0` to Render's own `$PORT` (env
  var Render sets automatically) and skips the local-only browser-open,
  both keyed off whether `$PORT` is present at all — a plain local
  `python gui.py` is completely unaffected, since that env var is never
  set locally.

### Steps

1. Push this repo to GitHub (or GitLab) — Render deploys from a
   connected repo, not a local directory.
2. In Render: **New → Blueprint**, point it at the repo — it picks up
   [`render.yaml`](render.yaml) automatically (Python runtime, build
   command `pip install -r requirements.txt`, start command
   `python gui.py`, `ENABLED_SITES=costplusdrugs` already set). Or set
   the same thing up by hand with **New → Web Service** if you'd rather
   not use a Blueprint — same three settings, plus that one env var.
3. Deploy. Render assigns a public URL; open it and you should see the
   GUI with only a "costplusdrugs" checkbox and no Amazon-setup button.
   The [documented JSON API](#api) works the same way once deployed —
   `curl "https://your-app.onrender.com/api/v1/prices?drug=lisinopril&dosage=20mg&sites=costplusdrugs"` —
   and rejects any other site the same way the GUI's checkboxes do,
   since both enforce `ENABLED_SITES` server-side.

### Worth knowing before you do

- **This makes the tool a public web page with no login in front of
  it.** Render's free/starter tier gives anyone with the URL access to
  run lookups (which just relay to Cost Plus Drugs' own free API — no
  API key or credentials of yours are exposed by this). Fine for a
  personal tool nobody else knows the URL to; add real auth in front of
  it (Render supports basic auth / access control on paid plans, or put
  your own check in `gui.py`) if that's not an acceptable risk for you.
- **The other three sites' packages still get installed** even though
  they're never used — `main.py`'s `_load_scrapers()` imports all four
  scraper modules unconditionally regardless of `ENABLED_SITES`, so
  `selenium`/`undetected-chromedriver`/`webdriver-manager`/`beautifulsoup4`/
  `lxml` are still in `requirements.txt` and still get built. This is
  harmless — none of them touch an actual Chrome binary at import time,
  only when a scraper is actually invoked (confirmed: `driver_utils.py`'s
  Chrome-resolving code lives entirely inside functions, never at module
  level) — just a bigger, slower build than a Cost-Plus-only app
  strictly needs. Making `_load_scrapers()` import only the sites in
  `ENABLED_SITES` would trim this, but wasn't done here since it wasn't
  asked for and wasn't needed to make the deployment work.
- **Flask's built-in dev server** (what `python gui.py` runs) logs its
  own warning about not being meant for production. For a personal,
  low-traffic lookup tool this is fine as-is — `gui.py` already runs it
  `threaded=True` for the challenge-modal polling GoodRx/SingleCare/
  Amazon need, which happens to also just work as ordinary concurrent
  request handling here. Swap in `gunicorn` (`pip install gunicorn`,
  start command `gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT gui:app`) if
  you want something more production-grade later; not necessary to get
  this running.

## Disclaimer

This tool queries GoodRx, SingleCare, and Amazon by scraping their
pharmacy pricing websites, and queries Cost Plus Drugs through their own
free public API (see [switched to their own public
API](#cost-plus-drugs-switched-to-their-own-public-api)) — all for
personal, non-commercial use. It is not affiliated with GoodRx,
SingleCare, Amazon, or Cost Plus Drugs. Automated scraping — including
the automated-browser detection countermeasures this tool uses to get
past some sites' bot protection — may violate those three sites' Terms
of Service, and either their page structure or their bot-detection
posture may change and break this tool at any time without warning.
Treat all output as best-effort, not authoritative pricing — always
confirm the actual price at checkout/pickup before relying on it.
