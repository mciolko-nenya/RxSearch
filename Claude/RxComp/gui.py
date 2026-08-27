"""
RxComp GUI — local browser-based front end over main.py's own pipeline.

Run: `python gui.py` — starts a local web server and opens
http://127.0.0.1:5057 in your default browser automatically.

Deliberately browser-based, not a native desktop window: an earlier
Tkinter version rendered nothing at all — confirmed live, `tkinter.
TkVersion` here reports 8.5.9, Apple's own long-deprecated system Tk
build (exactly what that "DEPRECATION WARNING: The system version of Tk
is deprecated" message was about), which is known to render blank on
modern macOS, and there's no Homebrew installed on this machine to get a
modern Tk instead. A browser-based UI sidesteps all of that — this
project already depends on a real browser for every scraper anyway, so
requiring one here too adds nothing new.

Launching the server itself still means running `python gui.py` from a
terminal (there's no bundled double-clickable app), but nothing *during*
actual use needs that terminal anymore — see the notifier/modal
paragraph below for why a challenge no longer means switching away from
the browser tab at all.

Reuses main.py's real functions (_load_scrapers, gather_results,
filter_out_insurance_required, normalize_quantities, sort_results)
rather than reimplementing any of that pipeline — this file is only
presentation, over HTTP instead of Tkinter widgets this time.

No more terminal input() at all, by request: GoodRx/SingleCare/Amazon's
interactive-challenge prompts (see driver_utils.ChallengeNotifier) go
through a GuiChallengeNotifier here instead, which the page polls for
and shows as an in-browser modal with a "Done" button — clicking it is
what actually unblocks the scraper thread, in place of pressing Enter in
a terminal. The scraper thread genuinely blocks (inside
wait_for_challenge_confirmation()) while that modal is up, so the Flask
server *must* be threaded (`app.run(threaded=True)` below) — otherwise
the polling/confirm requests the page needs to send while that one
request is still in flight could never be served. `_run_lock` still
serializes the actual searches/Amazon-setup themselves (so two can't
both try to drive Selenium at once) — that's a separate, narrower lock
than the threading this now needs for the modal to work at all.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from dataclasses import asdict

from flask import Flask, jsonify, render_template_string, request

import driver_utils
import main
from config import Config

app = Flask(__name__)
_scrapers_loaded = False


#: How long a challenge modal waits for "Done" before giving up on its
#: own. See GuiChallengeNotifier.wait()'s docstring for why this exists
#: at all — long enough that nobody actually solving a CAPTCHA gets cut
#: off mid-solve, short enough that an abandoned tab (closed browser,
#: sleeping laptop) doesn't wedge the server past a single work session.
CHALLENGE_TIMEOUT_SECONDS = 30 * 60


class GuiChallengeNotifier(driver_utils.ChallengeNotifier):
    """Browser-modal equivalent of driver_utils.TerminalChallengeNotifier.

    Reported live and confirmed, two related bugs in the original version
    of this class:

    1. `self._event.wait()` had no timeout. It's called from inside the
       same request that holds gui.py's `_run_lock` for its entire
       duration — so if a user started a search, a challenge modal
       appeared, and they then closed the tab (or their laptop slept, or
       they simply walked away) instead of clicking "Done," the request
       thread stayed parked here *forever*. `_run_lock` was never
       released, and every later `/api/search`/`/api/setup-amazon` call
       got a permanent 409 until the whole gui.py process was killed and
       restarted — a liveness bug, not just a slow recovery. Fixed with
       `CHALLENGE_TIMEOUT_SECONDS` above: `wait()` now gives up on its
       own after a bounded time, exactly like clicking "Done" without
       having solved anything (the existing "skip" semantics already
       used when a terminal user just presses Enter) — the caller
       re-checks the live page either way and reports a caveat/error if
       the challenge genuinely wasn't resolved.
    2. There was no per-challenge identifier — one shared `_message`/
       `_event` pair for the notifier's whole lifetime, on the theory
       that `INTERACTIVE_CHALLENGE_LOCK` already guarantees only one
       challenge is ever pending process-wide. That guarantee is real,
       but says nothing about which *browser tab* is aware of *which*
       challenge: with two tabs open (an ordinary thing to do — e.g. a
       leftover tab from an earlier session), a stale "Done" click in a
       tab that hasn't re-polled since challenge A was replaced by a
       later challenge B would still reach the server and unconditionally
       resolve whatever is pending *now* — silently marking B "solved"
       when the human never looked at it. Fixed by tagging every
       challenge with a monotonically increasing id; `/api/challenge`
       reports it, `confirm()` now requires the id it's confirming to
       still be the current one, and a stale/mismatched confirm is
       rejected instead of silently accepted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._message: str | None = None
        self._challenge_id = 0
        self._event = threading.Event()

    def wait(self, message: str) -> None:
        with self._lock:
            self._challenge_id += 1
            self._message = message
            self._event.clear()
        self._event.wait(timeout=CHALLENGE_TIMEOUT_SECONDS)
        with self._lock:
            self._message = None

    def pending(self) -> tuple[int, str] | None:
        """Returns (challenge_id, message) for whatever's currently
        pending, or None if nothing is."""
        with self._lock:
            if self._message is None:
                return None
            return self._challenge_id, self._message

    def confirm(self, challenge_id: int) -> bool:
        """Resolves the pending challenge only if `challenge_id` matches
        the one actually pending right now — a stale confirm for a
        challenge that's already moved on is rejected (returns False)
        rather than silently resolving whatever replaced it."""
        with self._lock:
            if self._message is None or challenge_id != self._challenge_id:
                return False
        self._event.set()
        return True


gui_notifier = GuiChallengeNotifier()
driver_utils.set_challenge_notifier(gui_notifier)
_run_lock = threading.Lock()

# Render (and most PaaS hosts) inject $PORT and expect the app to bind
# 0.0.0.0 to it; a plain local `python gui.py` has neither set, so this
# also doubles as the "are we deployed, not local" signal used below to
# skip the local-only browser auto-open.
_DEPLOYED = "PORT" in os.environ
PORT = int(os.environ.get("PORT", 5057))
HOST = os.environ.get("HOST", "0.0.0.0" if _DEPLOYED else "127.0.0.1")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>RxComp</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 1000px; margin: 24px auto; padding: 0 16px; color: #222;
  }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  .subtitle { color: #666; margin-top: 0; margin-bottom: 20px; font-size: 0.9rem; }
  form { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px 16px; align-items: end; }
  label { display: block; font-size: 0.82rem; color: #444; margin-bottom: 3px; }
  input[type=text] {
    width: 100%; padding: 6px 8px; font-size: 0.95rem;
    border: 1px solid #bbb; border-radius: 4px; box-sizing: border-box;
  }
  .sites { grid-column: 1 / -1; display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
  .sites label { display: flex; align-items: center; gap: 6px; font-size: 0.9rem; margin: 0; }
  .actions { grid-column: 1 / -1; display: flex; gap: 10px; align-items: center; margin-top: 4px; }
  button {
    padding: 8px 16px; font-size: 0.9rem; border-radius: 5px; border: 1px solid #444;
    background: #222; color: #fff; cursor: pointer;
  }
  button.secondary { background: #fff; color: #222; border-color: #999; }
  button:disabled { opacity: 0.5; cursor: default; }
  #status { grid-column: 1 / -1; font-size: 0.85rem; color: #555; min-height: 1.2em; }
  #status.error { color: #b00020; }
  table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.9rem; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #ddd; vertical-align: top; }
  th { color: #444; font-weight: 600; border-bottom: 2px solid #ccc; }
  tr.error td { color: #b00020; }
  tr:hover td { background: rgba(0,0,0,0.03); }
  #challenge-overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5);
    align-items: center; justify-content: center; z-index: 1000;
  }
  #challenge-modal {
    background: #fff; color: #222; border-radius: 8px; padding: 24px 28px;
    max-width: 420px; box-shadow: 0 8px 30px rgba(0,0,0,0.3);
  }
  #challenge-modal h2 { margin: 0 0 10px; font-size: 1.1rem; }
  #challenge-modal p { margin: 0 0 18px; font-size: 0.92rem; line-height: 1.4; color: #333; }
  #challenge-done-btn { width: 100%; }
</style>
</head>
<body>
<h1>RxComp</h1>
<p class="subtitle">Compare prescription drug prices across GoodRx, SingleCare, Amazon Pharmacy, and Cost Plus Drugs.</p>

<form id="search-form">
  <div>
    <label for="drug">Drug</label>
    <input type="text" id="drug" required>
  </div>
  <div>
    <label for="dosage">Dosage</label>
    <input type="text" id="dosage" required>
  </div>
  <div>
    <label for="formulation">Formulation</label>
    <input type="text" id="formulation" value="tablet">
  </div>
  <div>
    <label for="quantity">Quantity</label>
    <input type="text" id="quantity">
  </div>
  <div>
    <label for="zip">ZIP code</label>
    <input type="text" id="zip" value="{{ default_zip }}">
  </div>

  <div class="sites">
    {% for site in available_sites %}
    <label><input type="checkbox" class="site-checkbox" value="{{ site }}" checked> {{ site }}</label>
    {% endfor %}
    <label><input type="checkbox" id="debug"> Debug (visible browser)</label>
  </div>

  <div class="actions">
    <button type="submit" id="search-btn">Search</button>
    {% if 'amazon' in available_sites %}
    <button type="button" class="secondary" id="setup-amazon-btn">Set up Amazon login…</button>
    {% endif %}
    <span id="status"></span>
  </div>
</form>

<table id="results" style="display:none">
  <thead>
    <tr><th>Source</th><th>Price</th><th>Label</th><th>Pharmacy</th><th>Quantity</th><th>Notes</th></tr>
  </thead>
  <tbody></tbody>
</table>

<div id="challenge-overlay">
  <div id="challenge-modal">
    <h2>Action needed</h2>
    <p id="challenge-message"></p>
    <button id="challenge-done-btn">Done</button>
  </div>
</div>

<script>
const form = document.getElementById('search-form');
const statusEl = document.getElementById('status');
const searchBtn = document.getElementById('search-btn');
const setupBtn = document.getElementById('setup-amazon-btn');
const resultsTable = document.getElementById('results');
const resultsBody = resultsTable.querySelector('tbody');
const challengeOverlay = document.getElementById('challenge-overlay');
const challengeMessage = document.getElementById('challenge-message');
const challengeDoneBtn = document.getElementById('challenge-done-btn');

function setBusy(busy) {
  searchBtn.disabled = busy;
  // setupBtn is null when amazon isn't in this deployment's ENABLED_SITES
  // — the template omits the button entirely rather than just disabling it.
  if (setupBtn) setupBtn.disabled = busy;
}

// Polls for a pending challenge the whole time the page is open, not
// just during a search — cheap (see /api/challenge's docstring), and a
// search/Amazon-setup request is what's actually blocking server-side
// regardless of whether this happens to be mid-poll-cycle when it starts.
//
// Tracks the *id* of whichever challenge is currently displayed, not
// just a shown/hidden boolean — reported live and confirmed: the old
// boolean-only version only refreshed the modal's text on a
// not-pending-to-pending transition, so if challenge A was replaced by
// a different challenge B while this tab's modal was already showing
// (a real, expected sequence — see GuiChallengeNotifier's docstring),
// this tab would keep displaying A's stale text with its Done button
// still wired to confirm "whatever's pending now," i.e. B, even though
// the human never looked at B. Comparing ids (not just the boolean)
// catches exactly that pending-to-different-pending transition too, and
// the id is what's actually sent back on confirm — see below.
let shownChallengeId = null;
async function pollChallenge() {
  try {
    const resp = await fetch('/api/challenge');
    const data = await resp.json();
    if (data.pending && data.id !== shownChallengeId) {
      shownChallengeId = data.id;
      challengeMessage.textContent = data.message;
      challengeOverlay.style.display = 'flex';
    } else if (!data.pending && shownChallengeId !== null) {
      shownChallengeId = null;
      challengeOverlay.style.display = 'none';
    }
  } catch (err) {
    // A transient fetch failure here isn't worth surfacing — the next
    // poll a second later will just try again.
  }
}
setInterval(pollChallenge, 1000);

challengeDoneBtn.addEventListener('click', async () => {
  // Hide immediately rather than waiting for the next poll — the click
  // itself is what unblocks the waiting scraper thread server-side;
  // there's nothing left to show once that request has been sent.
  const confirmingId = shownChallengeId;
  challengeOverlay.style.display = 'none';
  shownChallengeId = null;
  try {
    // Sends the id of the challenge this tab actually displayed —
    // the server rejects it (silently, from this tab's perspective) if
    // it no longer matches what's currently pending, rather than
    // resolving whatever a later challenge happens to be.
    await fetch('/api/challenge/confirm', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: confirmingId}),
    });
  } catch (err) {
    setStatus('Could not confirm the challenge: ' + err, true);
  }
});

function setStatus(text, isError) {
  statusEl.textContent = text;
  statusEl.className = isError ? 'error' : '';
}

function renderResults(results) {
  resultsBody.innerHTML = '';
  for (const r of results) {
    const tr = document.createElement('tr');
    if (r.error) tr.className = 'error';
    const price = r.price !== null && r.price !== undefined ? '$' + r.price.toFixed(2) : 'N/A';
    for (const val of [r.source, price, r.price_label || '', r.pharmacy || '', r.quantity || '', r.error || '']) {
      const td = document.createElement('td');
      td.textContent = val;
      tr.appendChild(td);
    }
    resultsBody.appendChild(tr);
  }
  resultsTable.style.display = results.length ? '' : 'none';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const sites = Array.from(document.querySelectorAll('.site-checkbox:checked')).map(cb => cb.value);
  if (!sites.length) {
    setStatus('Select at least one site.', true);
    return;
  }
  const payload = {
    drug: document.getElementById('drug').value.trim(),
    dosage: document.getElementById('dosage').value.trim(),
    formulation: document.getElementById('formulation').value.trim(),
    quantity: document.getElementById('quantity').value.trim(),
    zip: document.getElementById('zip').value.trim(),
    sites: sites,
    debug: document.getElementById('debug').checked,
  };

  setBusy(true);
  setStatus('Searching… this can take a while. If a CAPTCHA appears, solve it in the ' +
             'Chrome window it opens, then click Done in the box that pops up here.');
  resultsTable.style.display = 'none';

  try {
    const resp = await fetch('/api/search', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      setStatus(data.error || 'Search failed.', true);
    } else {
      renderResults(data.results);
      const okCount = data.results.filter(r => !r.error).length;
      setStatus(`Done — ${data.results.length} row(s), ${okCount} with a price.`);
    }
  } catch (err) {
    setStatus('Search failed: ' + err, true);
  } finally {
    setBusy(false);
  }
});

// setupBtn is null when amazon isn't in this deployment's ENABLED_SITES —
// the template omits the whole button, so there's nothing to wire up.
if (setupBtn) {
  setupBtn.addEventListener('click', async () => {
    setBusy(true);
    setStatus('Opening Chrome for Amazon login — log in there, then click Done in the box ' +
               'that pops up here.');
    try {
      const resp = await fetch('/api/setup-amazon', {method: 'POST'});
      const data = await resp.json();
      if (!resp.ok) {
        setStatus(data.error || 'Amazon setup failed.', true);
      } else {
        setStatus('Amazon login saved. You can now include amazon in searches.');
      }
    } catch (err) {
      setStatus('Amazon setup failed: ' + err, true);
    } finally {
      setBusy(false);
    }
  });
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    # Deliberately Config.ENABLED_SITES here, not Config.ALL_SITES: a
    # site this deployment has disabled (e.g. a Cost-Plus-Drugs-only
    # Render deployment with ENABLED_SITES=costplusdrugs, which has no
    # Chrome/display to run the other three sites' Selenium flows at
    # all) shouldn't even be offered as a checkbox — see api_search()'s
    # matching restriction below for why offering it would be worse than
    # just confusing.
    return render_template_string(
        PAGE, available_sites=Config.ENABLED_SITES, enabled_sites=Config.ENABLED_SITES,
        default_zip=Config.DEFAULT_ZIP_CODE or "",
    )


@app.route("/api/search", methods=["POST"])
def api_search():
    global _scrapers_loaded
    if not _run_lock.acquire(blocking=False):
        return jsonify({"error": "A search or Amazon setup is already running — wait for it to finish."}), 409

    try:
        payload = request.get_json(force=True) or {}
        drug_name = (payload.get("drug") or "").strip()
        dosage = (payload.get("dosage") or "").strip()
        formulation = (payload.get("formulation") or "").strip() or "tablet"
        quantity = (payload.get("quantity") or "").strip() or None
        zip_code = (payload.get("zip") or "").strip() or None
        # Config.ENABLED_SITES, not Config.ALL_SITES: this is the actual
        # enforcement point. Without it, a client could still POST
        # {"sites": ["goodrx"]} on a deployment that deliberately
        # disabled every Selenium-based site (e.g. no Chrome installed
        # at all) regardless of what the rendered checkboxes offer —
        # the template restriction above is only the UI half of this.
        sites = [s for s in (payload.get("sites") or []) if s in Config.ENABLED_SITES]

        if not drug_name or not dosage:
            return jsonify({"error": "Drug and Dosage are both required."}), 400
        if not sites:
            return jsonify({"error": "Select at least one site."}), 400

        try:
            Config.validate()
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        if not _scrapers_loaded:
            main._load_scrapers()
            _scrapers_loaded = True

        debug = bool(payload.get("debug"))
        # Requested directly: with sites run concurrently (main.py's own
        # non---debug default), a challenge on one site's thread and a
        # challenge on another's can both land at roughly the same time —
        # INTERACTIVE_CHALLENGE_LOCK already guarantees only one modal is
        # ever shown at once, so nothing breaks, but from the person
        # answering them it reads as a confusing, unpredictable queue:
        # which site's window is this challenge even for, and how many
        # more are coming? Always running sequential=True here (not just
        # when the Debug checkbox is checked, since a challenge can force
        # a preemptive visible-window switch regardless of headless mode
        # — see goodrx_scraper.py's ZIP/dosage-edit docstrings) makes that
        # queue predictable instead: one site finishes completely —
        # challenges and all — before the next one's own challenges can
        # even occur. Debug still only controls headless vs. visible; it
        # no longer needs to also control sequencing, since sequencing is
        # unconditional now.
        original_default = Config.HEADLESS_DEFAULT
        try:
            if debug:
                Config.HEADLESS_DEFAULT = False
            results = main.gather_results(
                sites, drug_name, formulation, dosage, zip_code, quantity, sequential=True
            )
        finally:
            Config.HEADLESS_DEFAULT = original_default

        results = main.filter_out_insurance_required(results)
        results = main.normalize_quantities(results, quantity)
        results = main.sort_results(results)

        return jsonify({"results": [asdict(r) for r in results]})
    except Exception as e:
        return jsonify({"error": f"unexpected error: {e}"}), 500
    finally:
        _run_lock.release()


@app.route("/api/setup-amazon", methods=["POST"])
def api_setup_amazon():
    # Same restriction as api_search()'s site filter, for the same
    # reason: a deployment that disabled amazon (no Chrome/display to
    # actually run its interactive login flow on) shouldn't let this
    # route try anyway just because it was hit directly.
    if "amazon" not in Config.ENABLED_SITES:
        return jsonify({"error": "Amazon is not enabled on this deployment (amazon not in ENABLED_SITES)."}), 403
    if not _run_lock.acquire(blocking=False):
        return jsonify({"error": "A search or Amazon setup is already running — wait for it to finish."}), 409
    try:
        import amazon_scraper

        amazon_scraper.setup_amazon_profile()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        _run_lock.release()


@app.route("/api/challenge")
def api_challenge():
    """Polled by the page every second or so while a search/Amazon-setup
    is running, to know whether to show the "Done" modal right now — see
    GuiChallengeNotifier above. Cheap and side-effect-free by design, so
    polling it has no real cost. Reports the challenge's own id
    alongside its message, not just pending/not-pending — the page needs
    that id to confirm the *right* challenge later, not just whichever
    one happens to be pending by the time the click lands (see
    GuiChallengeNotifier's docstring)."""
    pending = gui_notifier.pending()
    if pending is None:
        return jsonify({"pending": False, "message": "", "id": None})
    challenge_id, message = pending
    return jsonify({"pending": True, "message": message, "id": challenge_id})


@app.route("/api/challenge/confirm", methods=["POST"])
def api_challenge_confirm():
    """What the modal's "Done" button actually calls — this is the
    browser-side equivalent of pressing Enter in a terminal: it unblocks
    whichever scraper thread is currently parked inside
    wait_for_challenge_confirmation(), which then re-checks the live page
    itself to decide whether the challenge is actually gone, exactly as
    it always did for the terminal-input version of this.

    Requires the same challenge id the page most recently saw from
    /api/challenge, in the JSON body as {"id": ...} — a stale click for
    a challenge that's already been superseded by a new one gets
    rejected (200 with resolved: false) rather than silently resolving
    whatever replaced it. The JS never surfaces that as an error to the
    user; a stale click is expected to occasionally happen (a browser
    tab that hasn't repolled yet), not a real failure."""
    payload = request.get_json(silent=True) or {}
    challenge_id = payload.get("id")
    resolved = gui_notifier.confirm(challenge_id) if isinstance(challenge_id, int) else False
    return jsonify({"ok": True, "resolved": resolved})


def main_gui():
    if _DEPLOYED:
        print(f"RxComp GUI running on {HOST}:{PORT}")
    else:
        url = f"http://127.0.0.1:{PORT}"
        print(f"RxComp GUI running at {url} — opening your browser...")
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # threaded=True is required now, not just nice-to-have: a search or
    # Amazon-setup request can sit blocked inside
    # wait_for_challenge_confirmation() for as long as a challenge modal
    # is up, and the page still needs to poll /api/challenge and hit
    # /api/challenge/confirm *while that's happening* — an unthreaded dev
    # server could never serve those, and the modal's "Done" button would
    # have no way to actually reach the waiting thread.
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main_gui()
