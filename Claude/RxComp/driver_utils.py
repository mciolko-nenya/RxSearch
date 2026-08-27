"""
Shared Selenium driver setup, factored out so goodrx_scraper.py,
amazon_scraper.py, and the Selenium-fallback paths in
costplusdrugs_scraper.py/singlecare_scraper.py don't each re-implement the
same driver-options/context-manager/debug-dump boilerplate.

Uses undetected-chromedriver rather than plain Selenium. Confirmed live
against Cost Plus Drugs: even with navigator.webdriver spoofed and the
automation-controlled Chrome flags stripped, its Cloudflare Bot Management
still 403'd the price API for a plain Selenium session — undetected-
chromedriver (which patches the chromedriver binary itself, not just
Chrome's launch flags) is the level this actually requires. Same
--user-data-dir/--profile-directory support as before, for Amazon's
persistent logged-in profile.
"""

from __future__ import annotations

import subprocess
import threading

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from undetected_chromedriver.patcher import Patcher
from webdriver_manager.chrome import ChromeDriverManager

from config import Config

# main.py runs sites concurrently by default (see gather_results()), and
# GoodRx and SingleCare can both need a human to solve an interactive
# CAPTCHA (see their _resolve_bot_challenge() methods). Without
# coordination, two threads could print/input() at the same time —
# interleaved terminal output and two Chrome windows competing for the same
# stdin prompt. This lock serializes that: whichever scraper hits its
# challenge first holds it until the user resolves (or skips) it, and any
# other scraper's challenge-resolution blocks here until it's free, so only
# one is ever presented to the user at a time.
#
# An RLock, not a plain Lock: reported live, a second (GoodRx) challenge
# window was popping up while a first (SingleCare) one was still
# unresolved — this lock existed and was correctly held the whole time,
# but goodrx_scraper.py's ZIP/dosage-quantity actions preemptively switch
# to a visible window *before* even checking for a challenge (see
# _try_enter_zip()'s docstring — that switch reliably needs to happen
# ahead of time, not reactively), and that switch-and-check block wasn't
# wrapped in this lock at all, only the actual _resolve_bot_challenge()
# call nested inside it was. Confirmed live: with a plain Lock, wrapping
# the outer block too would deadlock the moment that same thread's own
# nested _resolve_bot_challenge() tried to acquire it a second time — a
# plain Lock isn't reentrant. An RLock lets the same thread re-acquire it
# (a no-op re-lock) while still correctly blocking every other thread
# until the whole nested sequence releases it once, at the end.
INTERACTIVE_CHALLENGE_LOCK = threading.RLock()


class ChallengeNotifier:
    """How a scraper asks a human to resolve something (a CAPTCHA, an
    Amazon login) and waits for them to say they're done. The default
    (TerminalChallengeNotifier) is a blocking `input()` — the original,
    only behavior before this existed, and still what `main.py`'s CLI
    uses. `gui.py` installs a different one (see its own module) that
    shows a browser modal with a "Done" button instead of relying on a
    terminal at all, so the scrapers themselves don't need to know or
    care which front end is actually running them — they just call
    `wait_for_challenge_confirmation(message)` and block until *some*
    notifier says the human is done, whatever form that took."""

    def wait(self, message: str) -> None:
        raise NotImplementedError


class TerminalChallengeNotifier(ChallengeNotifier):
    def wait(self, message: str) -> None:
        input(message)


_challenge_notifier: ChallengeNotifier = TerminalChallengeNotifier()
_challenge_notifier_lock = threading.Lock()


def set_challenge_notifier(notifier: ChallengeNotifier) -> None:
    """Swaps which ChallengeNotifier wait_for_challenge_confirmation()
    below delegates to. Call once at startup — gui.py does this for its
    own browser-modal notifier; main.py's CLI never calls this, so it
    keeps the terminal-input default."""
    global _challenge_notifier
    with _challenge_notifier_lock:
        _challenge_notifier = notifier


def wait_for_challenge_confirmation(message: str) -> None:
    """Blocks until whichever ChallengeNotifier is currently installed
    reports the human is done — same call site every scraper already
    used for a direct `input(message)`, just indirected through
    whichever notifier is installed. Doesn't distinguish "solved" from
    "skipped": neither the original input()-based flow nor this one
    needs to — every caller already re-checks the live page afterward
    to see whether the challenge is actually gone, treating "still
    there" as a skip and "gone" as solved regardless of which button/key
    the human actually used."""
    with _challenge_notifier_lock:
        notifier = _challenge_notifier
    notifier.wait(message)


def _ensure_patched_and_signed(driver_path: str) -> str:
    """Patch the chromedriver binary for undetected-chromedriver's stealth
    (if not already patched — idempotent across repeated calls/runs), then
    re-sign it.

    On Apple Silicon, patching invalidates the binary's ad-hoc code
    signature — the content changed after it was signed — and macOS's
    kernel (AMFI) refuses to execute a binary whose signature no longer
    matches its content, killing it outright with SIGKILL. Confirmed live:
    without this, every driver launch failed with "unexpectedly exited.
    Status code was: -9". Re-signing ad-hoc restores a valid signature for
    the patched content. `codesign` doesn't exist off macOS, and unpatched
    binaries need no re-signing either way — both are harmless no-ops there.
    """
    patcher = Patcher(executable_path=driver_path, force=True)
    if not patcher.is_binary_patched():
        patcher.patch_exe()
        try:
            subprocess.run(
                ["codesign", "--force", "-s", "-", driver_path],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pass
    return driver_path


_driver_path_lock = threading.Lock()
_cached_driver_path: str | None = None


def _resolve_driver_path() -> str:
    """Resolves, patches, and signs the chromedriver binary — cached and
    locked so this only happens once per process, not once per
    create_chrome_driver() call.

    Reported live and confirmed: this used to run with no lock at all,
    while main.py's default (non-`--debug`) mode creates a driver per
    site concurrently via a ThreadPoolExecutor, and
    _ensure_patched_and_signed()'s own check-then-act
    (`is_binary_patched()` / `patch_exe()` / `codesign`) is not atomic —
    two threads could race to patch/sign the same on-disk binary
    (`ChromeDriverManager().install()` resolves to the same cached path
    for every caller), or one thread's `is_binary_patched()` check could
    observe the file as already patched by *another* thread's
    in-progress `patch_exe()` before that thread's `codesign` step has
    actually run, and hand that patched-but-not-yet-signed binary
    straight to `uc.Chrome()` — exactly the state this module's own
    docstring already documented as getting SIGKILLed (-9) by macOS AMFI
    on Apple Silicon.

    Caching after the first resolution isn't just an optimization here —
    it's what keeps the lock cheap to hold on every later call (a
    lock-protected variable read, not a re-run of the actual
    subprocess-spawning patch/sign work), so fixing the race doesn't
    reintroduce serialized contention on what was otherwise a genuinely
    concurrent path. The resolved/patched/signed path can't meaningfully
    change within one process's lifetime anyway (same Chrome install,
    same driver version), so caching it is also strictly correct, not
    just convenient."""
    global _cached_driver_path
    with _driver_path_lock:
        if _cached_driver_path is None:
            _cached_driver_path = _ensure_patched_and_signed(ChromeDriverManager().install())
        return _cached_driver_path


def robust_click(driver, element) -> None:
    """Reviewed live: costplusdrugs_scraper.py independently discovered
    and pasted in the same "try a native click, fall back to a
    JS-dispatched one" fix at three separate call sites, after live
    failures where a site-wide cookie-consent backdrop div (Silktide)
    sat on top of the page and intercepted native clicks — Selenium's
    native `.click()` refuses to click anything that isn't the actual
    topmost element at that screen pixel, so a plain retry of the same
    native click can never succeed while that backdrop is present, no
    matter how many attempts. singlecare_scraper.py has no such
    fallback anywhere — every click there is a bare native `.click()`
    — so the exact same failure mode would silently break it too if its
    page ever grew a similar overlay (a real, shared risk: third-party
    consent widgets are common to all of these sites, not particular to
    Cost Plus Drugs). Centralized here, a plain function rather than a
    SeleniumScraperBase method, since several call sites needing it
    (e.g. costplusdrugs_scraper.py's module-level
    _find_and_click_variant_button()) take a bare `driver`, not `self`
    — so a future site doesn't have to rediscover and hand-write this a
    fourth time; call this instead of `element.click()` directly
    wherever a click might be intercepted by something drawn on top of
    the real target."""
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def find_first_present(driver, selectors: list[str]):
    """Tries each CSS selector in order, returns the first element that
    exists at all — the "list of confirmed candidates, most-to-least
    specific" resilience pattern used throughout this project's scraper
    files for elements that can appear under different selectors
    depending on which DOM variant a site happens to serve. Originally
    written in goodrx_scraper.py (for its "Edit prescription" modal's two
    known variants) and independently re-implemented in
    amazon_scraper.py's `_looks_like_signin_page()` (over
    `SIGNIN_INDICATOR_SELECTORS`, differing only in using
    `find_elements` instead of `find_element`, functionally the same
    existence check) — moved here, shared, so a third site doesn't
    rewrite it a third time. Callers that need to *wait* for one to
    appear poll this themselves (see goodrx_scraper.py's modal-open
    check) rather than this helper doing its own waiting, since how
    long to wait and what to do if none ever appear differs by caller."""
    for selector in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            if el:
                return el
        except Exception:
            continue
    return None


def build_chrome_options(
    headless: bool, user_data_dir: str | None = None, profile_directory: str = "Default"
) -> uc.ChromeOptions:
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={Config.SCRAPER_USER_AGENT}")
    if user_data_dir:
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument(f"--profile-directory={profile_directory}")
    return options


def create_chrome_driver(
    headless: bool, user_data_dir: str | None = None
) -> uc.Chrome:
    options = build_chrome_options(headless, user_data_dir)

    # undetected-chromedriver's own chromedriver download has been observed
    # fetching the wrong CPU architecture on Apple Silicon (mac-x64 instead
    # of mac-arm64), which fails outright without Rosetta installed.
    # webdriver-manager's platform detection is reliable, so use it to
    # resolve the correct binary, patch/sign it ourselves, and hand the
    # already-patched path to uc (which skips re-patching a binary that's
    # already patched). Resolved once per process and cached/locked — see
    # _resolve_driver_path()'s own docstring for why that's required, not
    # just faster, when multiple sites create a driver concurrently.
    driver_path = _resolve_driver_path()

    # use_subprocess=True is undetected-chromedriver's documented setting
    # for headless/scripted use — it's what makes .quit() reliably clean up
    # the subprocess instead of leaking a Chrome process per run.
    driver = uc.Chrome(
        options=options, driver_executable_path=driver_path, headless=headless, use_subprocess=True
    )
    driver.set_page_load_timeout(Config.SELENIUM_PAGE_LOAD_TIMEOUT)
    return driver


class SeleniumScraperBase:
    """
    Shared context-manager + debug-dump behavior for Selenium-based scrapers,
    mirroring Schedule Checker's MindbodyScraper class (__enter__/__exit__/
    close(), _save_debug_page()).
    """

    def __init__(self, headless: bool = True, user_data_dir: str | None = None):
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.driver: uc.Chrome | None = None

    def _create_driver(self):
        self.driver = create_chrome_driver(self.headless, self.user_data_dir)
        return self.driver

    def _save_debug_page(self, filename: str):
        if not self.driver:
            return
        try:
            with open(filename, "w") as f:
                f.write(self.driver.page_source)
            print(f"Saved debug page to {filename}")
        except OSError as e:
            print(f"Could not save debug page {filename}: {e}")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def __enter__(self):
        self._create_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
