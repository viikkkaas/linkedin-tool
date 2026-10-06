#!/usr/bin/env python3
"""
Semi-automated LinkedIn Connection Tool
=======================================

Find LinkedIn profiles for dental clinic owners/managers, then semi-automate
sending connection requests (script fills note, you click Send manually).

Usage:
    python linkedin_connector.py --step 1    # Run profile finder
    python linkedin_connector.py --step 2    # Run connection tool
    python linkedin_connector.py             # Interactive menu
"""

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================================
# CONFIGURATION - Edit these variables at the top of the file
# ============================================================================

# File paths
LEADS_FILE = "leads.csv"
PROFILES_FILE = "leads_with_profiles.csv"
SENT_LOG_FILE = "sent_log.csv"

# LinkedIn search configuration
SEARCH_QUERIES = [
    "{clinic_name} owner",
    "{clinic_name} office manager",
    "{clinic_name} dentist",
]
TOP_RESULTS = 3

# Connection note template - EDIT THIS
CONNECTION_NOTE_TEMPLATE = (
    "Hi {first_name}, came across {clinic_name} — would love to connect and share "
    "something that might help with patient calls/bookings."
)

# Daily cap to avoid LinkedIn restrictions
DAILY_CAP = 20

# Random delay between profile loads (seconds) — NOT between your clicks
DELAY_MIN = 5
DELAY_MAX = 15

# Browser settings
USER_DATA_DIR = ".linkedin_user_data"
HEADLESS = False

# LinkedIn URLs
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/search/results/people/"
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"

# Required columns in leads.csv
REQUIRED_COLUMNS = {"clinic_name", "city", "state"}

# LinkedIn security checkpoint indicators (URL patterns + page content)
CHECKPOINT_URL_PATTERNS = ["checkpoint", "challenge", "captcha", "verify", "security"]
CHECKPOINT_SELECTORS = [
    "iframe[src*='captcha']",
    "#captcha-container",
    ".captcha-container",
    "[data-test-id='security-challenge']",
    "form[action*='challenge']",
]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_first_name(full_name):
    if not full_name:
        return ""
    parts = full_name.strip().split()
    return parts[0] if parts else ""


def format_search_query(query_template, clinic_name, city, state):
    query = query_template.format(clinic_name=clinic_name)
    if city and state:
        query += f" {city} {state}"
    elif city:
        query += f" {city}"
    return query.strip()


def read_csv(file_path):
    if not os.path.exists(file_path):
        return []
    with open(file_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def validate_leads_csv(leads, file_path):
    if not leads:
        print(f"ERROR: No data found in {file_path}")
        return False
    actual_columns = set(leads[0].keys())
    missing = REQUIRED_COLUMNS - actual_columns
    if missing:
        print(f"ERROR: {file_path} is missing required columns: {', '.join(sorted(missing))}")
        print(f"Found columns: {', '.join(sorted(actual_columns))}")
        print(f"Required columns: {', '.join(sorted(REQUIRED_COLUMNS))}")
        return False
    return True


def write_csv(file_path, data, fieldnames):
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


def append_csv(file_path, data, fieldnames):
    file_exists = os.path.exists(file_path)
    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(data)


def get_sent_log_entries():
    return read_csv(SENT_LOG_FILE)


def is_already_sent(profile_url, sent_log):
    for entry in sent_log:
        if entry.get("profile_url") == profile_url:
            return True
    return False


def get_clinic_key(clinic_name, city, state):
    return f"{clinic_name.lower()}|{city.lower()}|{state.lower()}"


def check_for_checkpoint(page, label=""):
    """Check if LinkedIn hit a CAPTCHA/security checkpoint. Pause if so."""
    url = page.url.lower()
    for pattern in CHECKPOINT_URL_PATTERNS:
        if pattern in url:
            _pause_for_checkpoint(page, label)
            return True

    for selector in CHECKPOINT_SELECTORS:
        try:
            if page.query_selector(selector):
                _pause_for_checkpoint(page, label)
                return True
        except Exception:
            pass

    title = page.title().lower()
    if any(w in title for w in ["security", "verification", "captcha", "unusual"]):
        _pause_for_checkpoint(page, label)
        return True

    return False


def _pause_for_checkpoint(page, label):
    print()
    print("!" * 70)
    print(f"  LinkedIn security checkpoint detected! {label}")
    print("  The browser is showing a CAPTCHA or verification page.")
    print()
    print("  >>> Solve it in the browser window now.")
    print("  >>> After solving, wait for the page to load normally.")
    print("  >>> Then come back here and press ENTER to continue.")
    print("!" * 70)
    input("\n  Press ENTER after checkpoint is cleared: ")
    time.sleep(2)


def safe_goto(page, url, timeout=60000, label=""):
    """Navigate to `url`, tolerating the 'navigation interrupted by another
    navigation to the same URL' race.

    LinkedIn auto-redirects (e.g. to /feed/ after login, or routes a search
    URL) at the same time we issue an explicit page.goto(), and the two
    navigation attempts can collide. When that happens Playwright raises
    'Navigation to "..." is interrupted by another navigation' — but the page
    is ALREADY navigating there, so we treat it as success and just settle.
    """
    try:
        page.goto(url, timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "interrupted by another navigation" in msg or "navigation is interrupted" in msg:
            print(f"  (nav raced on '{label or url}' — page is already navigating there, ignoring)")
            time.sleep(1)
        else:
            print(f"  [safe_goto] non-race navigation error on '{label or url}': {e}")
        return True

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")


def ensure_debug_dir():
    os.makedirs(DEBUG_DIR, exist_ok=True)


# Soft-block / wall indicators commonly shown after rapid automated browsing.
# NOTE: these are matched against VISIBLE page body text ONLY (document.body
# .innerText), never raw HTML. Raw HTML includes dormant/inert templates such
# as <meta name="como-err"> in the <head> (LinkedIn embeds an error template
# in every page) that are NOT displayed on screen and must be ignored.
SOFT_BLOCK_PATTERNS = [
    "sign in", "sign-in", "join now", "log in", "login",
    "something went wrong", "please try again",
    "rate limit", "too many requests", "temporarily blocked",
    "unusual activity", "this page isn\u2019t available",
]

# Actual on-screen error / block UI elements (visible DOM, not <head> markup).
# * auth wall      -> #authwall-join-form / authwall-join-form__title "Sign in"
# * CAPTCHA        -> iframes/#captcha-container (also caught by checkpoint fns)
# * CUL / technical-> artdeco-toast-message / [aria-label*="technical" i]
SOFT_BLOCK_UI_SELECTORS = [
    "#authwall-join-form",
    ".authwall-join-form",
    "input[name='session_key']",
    "#captcha-container",
    "iframe[src*='captcha']",
    "iframe[src*='challenge']",
    "[aria-label*='technical difficulties' i]",
    "button[aria-label*='commercial use' i]",
    "button[aria-label*='upgrade to sales' i]",
]


def _visible_body_text(page):
    """Return normalized VISIBLE text of the page body (never <head>/<meta>)."""
    try:
        return page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
        ) or ""
    except Exception:
        return ""


def detect_soft_block(page):
    """Detect sign-in walls / CAPTCHA / rate-limit using VISIBLE page content only.

    Takes the `page` object (not raw HTML) so <meta>/<head> content in the raw
    markup — e.g. the inert <meta name="como-err"> template LinkedIn puts in
    every page's <head> — is ignored. Only rendered body text and actual
    error/block UI elements are considered.
    """
    try:
        visible_text = _visible_body_text(page)
    except Exception:
        return "UNKNOWN (could not read page)"

    if not visible_text:
        return "UNKNOWN (no visible body text)"

    low = visible_text.lower()
    text_hits = [p for p in SOFT_BLOCK_PATTERNS if p in low]

    ui_hits = []
    for selector in SOFT_BLOCK_UI_SELECTORS:
        try:
            if page.query_selector(selector):
                ui_hits.append(selector)
        except Exception:
            pass

    if ui_hits:
        return "DETECTED (UI): " + ", ".join(ui_hits[:5])
    if text_hits:
        return "DETECTED (text): " + ", ".join(sorted(set(text_hits))[:5])
    return "none"


def dump_debug_info(page, label=""):
    """Screenshot + HTML dump + URL/title for debugging search/load failures.

    Saves to ./debug/ (see DEBUG_DIR). Returns a summary string printed to
    console so the failure is inspectable at a glance.
    """
    ensure_debug_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = (label or "unknown").replace(" ", "_")[:40]
    prefix = f"search_fail_{ts}_{safe_label}"

    url = ""
    title = ""
    try:
        url = page.url
    except Exception:
        pass
    try:
        title = page.title()
    except Exception:
        pass

    # 1) Screenshot
    shot_path = os.path.join(DEBUG_DIR, f"{prefix}.png")
    try:
        page.screenshot(path=shot_path, full_page=False)
        print(f"    [debug] screenshot -> {shot_path}")
    except Exception as e:
        print(f"    [debug] screenshot failed: {e}")

    # 2) URL + title
    print(f"    [debug] page URL:   {url}")
    print(f"    [debug] page title: {title}")

    # 3) HTML dump (first 2000 chars)
    html = ""
    try:
        html = page.content()
    except Exception as e:
        print(f"    [debug] content read failed: {e}")
    html_path = os.path.join(DEBUG_DIR, f"{prefix}.html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html[:2000])
        print(f"    [debug] html dump (first 2000 chars) -> {html_path}")
    except Exception as e:
        print(f"    [debug] html write failed: {e}")

    # 4) Soft-block / sign-in wall / rate-limit detection (visible content only)
    block = detect_soft_block(page)
    print(f"    [debug] soft-block/sign-in check: {block}")

    return {"url": url, "title": title, "block": block, "html_path": html_path}


# ============================================================================
# STEP 1: PROFILE FINDER (interactive picker)
# ============================================================================

# LinkedIn people-search is Server-Driven UI / React Server Components now.
# CSS class hashes rotate every deploy and display:contents flattens the DOM,
# so class-based per-card selectors are dead. This script instead uses the
# stable profile anchor `a[href*="/in/"]` for profile URLs + display names,
# and slices `main.innerText` between consecutive name lines to recover the
# headline (title). Returns { rows, candidate_count, resolved_count }.
SEARCH_EXTRACT_JS = """
(() => {
  if (!/search\\/results\\/people/.test(window.location.href)) {
    return { error: 'not on people search page', url: window.location.href };
  }
  const main = document.querySelector('main') || document.body;
  const normalize = (s) => String(s || '').replace(/[\\s\\u00a0\\u202f]+/g, ' ').trim();
  const skip = (l) => !l
    || /^Status is/.test(l)
    || /^(Message|Connect|Follow|View profile|Pending|Remove)$/i.test(l)
    || /^[•·]/.test(l)
    || l.includes('mutual connection')
    || l.includes('shared connection')
    || /^Summary:/i.test(l)
    || /^About this profile/i.test(l);

  const anchors = Array.from(main.querySelectorAll('a[href*="/in/"]'));
  const personEntries = [];
  const seenHandles = new Set();
  for (const a of anchors) {
    const m = (a.getAttribute('href') || '').match(/\\/in\\/([^/?#]+)/);
    if (!m || !m[1]) continue;
    const profileHandle = m[1];
    if (seenHandles.has(profileHandle)) continue;
    const aria = a.querySelector('span[aria-hidden="true"]');
    let name = normalize(aria ? aria.textContent : a.textContent);
    name = name.replace(/^Status is (online|offline)\\.?\\s*/i, '')
               .replace(/'?s profile$/i, '')
               .replace(/\\s*[•·].*$/, '').trim();
    if (!name) continue;
    seenHandles.add(profileHandle);
    personEntries.push({ profileHandle, displayName: name });
  }

  const lines = (main.innerText || '').split(/\\n+/).map(normalize).filter(Boolean);
  const nameToIndex = new Map();
  for (const { displayName } of personEntries) {
    if (nameToIndex.has(displayName)) continue;
    const match = lines.findIndex((l) =>
      !skip(l) && (l === displayName
        || l.startsWith(displayName + ' ')
        || l.startsWith(displayName + ',')
        || l.startsWith(displayName + "'"))
    );
    if (match >= 0) nameToIndex.set(displayName, match);
  }

  const resolved = personEntries.filter((p) => nameToIndex.has(p.displayName));
  const rows = [];
  for (let i = 0; i < resolved.length; i++) {
    const { profileHandle, displayName } = resolved[i];
    const startIdx = nameToIndex.get(displayName);
    let stopIdx = lines.length;
    for (let j = i + 1; j < resolved.length; j++) {
      const otherStart = nameToIndex.get(resolved[j].displayName);
      if (otherStart != null && otherStart > startIdx) { stopIdx = otherStart; break; }
    }
    const slice = lines.slice(startIdx + 1, stopIdx).filter((l) => l !== displayName && !skip(l));
    rows.push({
      name: displayName,
      headline: slice[0] || '',
      profile_url: 'https://www.linkedin.com/in/' + profileHandle + '/',
    });
  }
  return {
    rows,
    candidate_count: personEntries.length,
    resolved_count: resolved.length,
  };
})()
"""


def extract_search_results(page, top_n=TOP_RESULTS, label="search"):
    results = []
    try:
        # LinkedIn moved people-search to Server-Driven UI (SDUI/RSC). CSS
        # class hashes (`.entity-result__*`, `.reusable-search__result-container`)
        # rotate on every deploy and `display:contents` wrappers flatten the
        # DOM, so per-card class selectors no longer exist / are unreliable.
        # The STABLE, non-rotating signal is the profile anchor `a[href*="/in/"]`.
        page.wait_for_selector(
            "main a[href*='/in/'], "
            "[data-test-id='search-results-container'], "
            "ul.reusable-search__entity-result-list, "
            ".reusable-search__entity-result-list",
            timeout=15000,
        )
        time.sleep(1)

        raw = page.evaluate(SEARCH_EXTRACT_JS)
        if not raw or not isinstance(raw, dict):
            print("    ERROR: search extraction returned no payload")
            return []

        if raw.get("error"):
            print(f"    ERROR: {raw['error']}")
            dump_debug_info(page, label=label)
            return []

        candidate_count = raw.get("candidate_count") or 0
        resolved_count = raw.get("resolved_count") or 0
        rows = raw.get("rows") or []

        # Print RAW match counts before any filtering so partial matches are
        # visible even when nothing resolves to a usable row.
        print(f"    [debug] candidate /in/ anchors: {candidate_count}, "
              f"resolved rows: {resolved_count}, usable: {len(rows)}")

        for row in rows[:top_n]:
            name = (row.get("name") or "").strip()
            headline = (row.get("headline") or "").strip()
            profile_url = (row.get("profile_url") or "").strip()
            if name and profile_url:
                results.append({"name": name, "title": headline, "profile_url": profile_url})

    except PlaywrightTimeoutError:
        print("    Timeout waiting for search results")
        print("    >>> Dumping debug info for diagnosis...")
        dump_debug_info(page, label=label)
    except Exception as e:
        print(f"    Error extracting results: {e}")

    return results[:top_n]


def prompt_pick_result(clinic_name, results):
    """Show numbered results and prompt user to pick one."""
    print(f"\n  Top results for: {clinic_name}")
    print("  " + "-" * 60)
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r['name']}")
        print(f"     {r['title']}")
        print(f"     {r['profile_url']}")
    print(f"  s. Skip this clinic")
    print(f"  q. Quit Step 1")
    print("  " + "-" * 60)

    while True:
        choice = input("  Pick (1/2/3/s/q): ").strip().lower()
        if choice in ("s", "q"):
            return choice
        try:
            idx = int(choice)
            if 1 <= idx <= len(results):
                return str(idx)
        except ValueError:
            pass
        print("  Invalid choice. Enter 1, 2, 3, 's', or 'q'.")


def step1_find_profiles():
    print("\n" + "=" * 70)
    print("STEP 1: LinkedIn Profile Finder (interactive)")
    print("=" * 70)

    leads = read_csv(LEADS_FILE)
    if not validate_leads_csv(leads, LEADS_FILE):
        return

    print(f"Found {len(leads)} leads in {LEADS_FILE}")

    # Load already-processed clinics so we can skip them
    existing_profiles = read_csv(PROFILES_FILE)
    processed_keys = set()
    for p in existing_profiles:
        if p.get("confirmed", "").lower() == "yes" and p.get("clinic_name"):
            processed_keys.add(get_clinic_key(p["clinic_name"], p.get("city", ""), p.get("state", "")))

    results = []
    completed = False

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        # Check login
        print("\nChecking LinkedIn login status...")
        page.goto(LINKEDIN_LOGIN_URL, timeout=30000)
        time.sleep(3)
        check_for_checkpoint(page, "(login check)")

        if "feed" not in page.url and "mynetwork" not in page.url:
            print("\n>>> A browser window opened. Please log in to LinkedIn.")
            print(">>> After logging in, come back here and press ENTER.")
            input("\nPress ENTER when logged in: ")
            try:
                page.wait_for_url("**/feed/**", timeout=15000)
            except Exception:
                try:
                    page.goto("https://www.linkedin.com/feed/", timeout=30000)
                except Exception:
                    pass
            time.sleep(2)
            check_for_checkpoint(page, "(post-login)")

        print("LinkedIn session active.\n")

        # Initial settle delay before the first search — LinkedIn can soft-block
        # searches that fire too quickly right after login.
        settle = random.uniform(3, 5)
        print(f"  Settling {settle:.1f}s before first search to avoid soft-block...")
        time.sleep(settle)

        for idx, lead in enumerate(leads, 1):
            clinic_name = lead.get("clinic_name", "")
            city = lead.get("city", "")
            state = lead.get("state", "")
            clinic_key = get_clinic_key(clinic_name, city, state)

            if clinic_key in processed_keys:
                print(f"  Skipping {idx}/{len(leads)}: {clinic_name} (already picked)")
                continue

            print(f"\nProcessing {idx}/{len(leads)}: {clinic_name} @ {city}, {state}")

            all_results = []
            for query_template in SEARCH_QUERIES:
                query = format_search_query(query_template, clinic_name, city, state)
                print(f"  Searching: '{query}'")

                try:
                    search_url = f"{LINKEDIN_SEARCH_URL}?keywords={query.replace(' ', '%20')}"
                    safe_goto(page, search_url, timeout=60000, label=f"search: {query[:30]}")
                    time.sleep(random.uniform(2, 4))

                    if check_for_checkpoint(page, f"(search: {query[:40]})"):
                        print("  Retrying this search after checkpoint cleared...")
                        safe_goto(page, search_url, timeout=60000, label=f"search-retry: {query[:30]}")
                        time.sleep(random.uniform(2, 4))

                    found = extract_search_results(page, label=f"query_{query[:30]}")
                    all_results.extend(found)
                    print(f"  Found {len(found)} results")

                except Exception as e:
                    print(f"  Search error: {e}")
                    continue

                if len(all_results) >= TOP_RESULTS:
                    break

            # Deduplicate by URL
            unique = []
            seen_urls = set()
            for r in all_results:
                if r["profile_url"] not in seen_urls:
                    seen_urls.add(r["profile_url"])
                    unique.append(r)
            top_results = unique[:TOP_RESULTS]

            if not top_results:
                print(f"  No results found for {clinic_name}. Skipping.")
                continue

            # Interactive picker
            choice = prompt_pick_result(clinic_name, top_results)

            if choice == "q":
                print("\nQuitting Step 1. Progress so far will be saved.")
                completed = True
                break
            elif choice == "s":
                print(f"  Skipped {clinic_name}")
                continue
            else:
                picked = top_results[int(choice) - 1]
                results.append({
                    "clinic_name": clinic_name,
                    "city": city,
                    "state": state,
                    "name": picked["name"],
                    "title": picked["title"],
                    "profile_url": picked["profile_url"],
                    "confirmed": "yes",
                })
                print(f"  Picked: {picked['name']} — {picked['title']}")

            # Natural delay between clinics
            time.sleep(random.uniform(1, 2))

        context.close()

    # Save results — only picked (confirmed=yes) entries
    if results:
        fieldnames = ["clinic_name", "city", "state", "name", "title", "profile_url", "confirmed"]

        # Merge with any previously confirmed picks
        for p in existing_profiles:
            if p.get("confirmed", "").lower() == "yes" and p.get("profile_url"):
                already_in = any(r["profile_url"] == p["profile_url"] for r in results)
                if not already_in:
                    results.append(p)

        write_csv(PROFILES_FILE, results, fieldnames)
        print(f"\nSaved {len(results)} confirmed profile(s) to {PROFILES_FILE}")
    else:
        print("\nNo profiles picked.")

    if not completed:
        print(f"\nDone with all clinics. Run: python {__file__} --step 2")


# ============================================================================
# STEP 2: CONNECTION TOOL
# ============================================================================

def log_connection(profile_url, clinic, name, status, notes=""):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "name": name,
        "clinic": clinic,
        "profile_url": profile_url,
        "status": status,
        "notes": notes,
    }
    fieldnames = ["timestamp", "date", "name", "clinic", "profile_url", "status", "notes"]
    append_csv(SENT_LOG_FILE, entry, fieldnames)


def step2_send_connections():
    print("\n" + "=" * 70)
    print("STEP 2: Semi-Automated Connection Tool")
    print("=" * 70)

    profiles = read_csv(PROFILES_FILE)
    confirmed = [p for p in profiles if p.get("confirmed", "").lower() == "yes"]

    if not confirmed:
        print(f"No confirmed profiles in {PROFILES_FILE}")
        print("Run Step 1 first, or add 'yes' to the 'confirmed' column manually.")
        return

    sent_log = get_sent_log_entries()
    to_process = [p for p in confirmed if not is_already_sent(p.get("profile_url", ""), sent_log)]

    if not to_process:
        print("All confirmed profiles have already been processed.")
        return

    # Count how many were sent today (for daily cap)
    today_str = datetime.now().strftime("%Y-%m-%d")
    sent_today = sum(1 for e in sent_log if e.get("date") == today_str and e.get("status") == "sent")

    print(f"Confirmed profiles: {len(confirmed)}")
    print(f"To process (not yet sent): {len(to_process)}")
    print(f"Already sent today: {sent_today}")
    print(f"Daily cap: {DAILY_CAP}")
    print()

    sent_count = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        print("Checking LinkedIn login status...")
        page.goto(LINKEDIN_LOGIN_URL, timeout=30000)
        time.sleep(3)
        check_for_checkpoint(page, "(login check)")

        if "feed" not in page.url and "mynetwork" not in page.url:
            print("\n>>> A browser window opened. Please log in to LinkedIn.")
            input("\nPress ENTER when logged in: ")
            # Wait for LinkedIn's own post-login redirect to /feed/ instead of
            # racing it with an explicit goto (which gets interrupted). If the
            # redirect never happens, fall back to a defensive goto.
            try:
                page.wait_for_url("**/feed/**", timeout=15000)
            except Exception:
                try:
                    page.goto("https://www.linkedin.com/feed/", timeout=30000)
                except Exception:
                    # e.g. "Navigation interrupted by another navigation" to same URL
                    pass
            time.sleep(2)
            check_for_checkpoint(page, "(post-login)")

        print("LinkedIn session active.\n")

        for idx, profile in enumerate(to_process, 1):
            clinic_name = profile.get("clinic_name", "")
            name = profile.get("name", "")
            profile_url = profile.get("profile_url", "")

            # Check daily cap
            if sent_today + sent_count >= DAILY_CAP:
                print("\n" + "=" * 70)
                print("DAILY CAP REACHED")
                print(f"Sent {sent_today + sent_count} connections today.")
                print("Come back tomorrow to continue.")
                print("=" * 70)
                break

            print(f"Processing {idx}/{len(to_process)}: {name} @ {clinic_name}")

            try:
                # Random delay between profile loads (not between user clicks)
                if idx > 1:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    print(f"  Natural delay: {delay:.1f}s ...")
                    time.sleep(delay)

                print(f"  Loading profile...")
                safe_goto(page, profile_url, timeout=60000, label=f"profile: {name}")
                time.sleep(2)

                if check_for_checkpoint(page, f"(profile: {name})"):
                    print("  Retrying profile load after checkpoint cleared...")
                    safe_goto(page, profile_url, timeout=60000, label=f"profile-retry: {name}")
                    time.sleep(2)

                # Check for 404
                page_title = page.title()
                if "Page Not Found" in page_title or "404" in page_title:
                    print("  ERROR: Profile page not found (404)")
                    log_connection(profile_url, clinic_name, name, "error", "Page not found (404)")
                    continue

                # Find Connect button — robust fallback list (current 2026 DOM)
                connect_button = None
                for selector in [
                    "a[href*='/preload/custom-invite/']",  # structural anchor, locale-independent
                    "div.pvs-profile-actions button:has-text('Connect')",
                    "button:has-text('Connect')",
                    "button[aria-label*='Invite'][aria-label*='to connect']",
                    "button.pv-s-profile-actions--connect",
                    "button.pvs-profile-actions__action:has-text('Connect')",
                ]:
                    connect_button = page.query_selector(selector)
                    if connect_button:
                        break

                if not connect_button:
                    # Check if already connected / pending / follow-only
                    if page.query_selector("button:has-text('Pending')"):
                        print("  Already pending/connected. Skipping.")
                        log_connection(profile_url, clinic_name, name, "skipped", "Already pending")
                        continue
                    if page.query_selector("a[href*='/messaging/compose']") or \
                       page.query_selector("button:has-text('Message')"):
                        print("  Already connected (Message present). Skipping.")
                        log_connection(profile_url, clinic_name, name, "skipped", "Already connected")
                        continue
                    if page.query_selector("button:has-text('Follow')") or \
                       page.query_selector("button:has-text('Following')"):
                        print("  Premium/Creator mode — no Connect button. Skipping.")
                        log_connection(profile_url, clinic_name, name, "error", "Connect button not found")
                        continue

                    print("  ERROR: Connect button not found")
                    log_connection(profile_url, clinic_name, name, "error", "Connect button not found")
                    continue

                # Click Connect (either the direct link or the button)
                print("  Clicking Connect...")
                if connect_button.get_attribute("href"):
                    connect_button.click(timeout=8000)
                else:
                    connect_button.click(timeout=5000)
                time.sleep(1.5)

                # Handle both dialog layouts.
                #  (A) Newer gating dialog: "Add a note to your invitation?"
                #      -> two buttons [Add a note] [Send without a note]
                #  (B) Legacy inline dialog: note textarea already exposed.
                # Click "Add a note" if present (gating or inline).
                add_note_btn = None
                for selector in [
                    "button[aria-label='Add a note']",
                    "button:has-text('Add a note')",
                    "div[role='button']:has-text('Add a note')",
                    "[data-test-id='connect-note-button']",
                ]:
                    add_note_btn = page.query_selector(selector)
                    if add_note_btn:
                        break

                if add_note_btn:
                    print("  Clicking 'Add a note'...")
                    add_note_btn.click(timeout=5000)
                    time.sleep(1)

                # Find the note textarea (scoped broadly to current DOM)
                note_textarea = None
                for selector in [
                    "textarea[name='message']",
                    "textarea#custom-message",
                    "textarea[aria-label*='note']",
                    "textarea[placeholder*='note']",
                    "textarea[placeholder*='message']",
                    "[role='dialog'] textarea",
                    "dialog textarea",
                ]:
                    note_textarea = page.query_selector(selector)
                    if note_textarea:
                        break

                if not note_textarea:
                    print("  ERROR: Could not find note textarea")
                    log_connection(profile_url, clinic_name, name, "error", "Note textarea not found")
                    continue

                # Fill the note: prefer the generated personalized_note column
                # (generate_notes.py); fall back to the static template when it
                # is missing/empty. Hard-truncate at 300 chars defensively.
                first_name = get_first_name(name)
                personalized_note = (profile.get("personalized_note") or "").strip()
                if personalized_note:
                    if len(personalized_note) > 300:
                        cut = personalized_note[:300]
                        sp = cut.rfind(" ")
                        if sp > 0:
                            cut = cut[:sp]
                        personalized_note = cut.strip()
                    print(f"  Using personalized_note from CSV ({len(personalized_note)} chars, for {first_name})")
                else:
                    personalized_note = CONNECTION_NOTE_TEMPLATE.format(
                        first_name=first_name,
                        clinic_name=clinic_name,
                    )

                note_textarea.fill(personalized_note)
                time.sleep(0.5)

                # STOP — wait for user
                print()
                print("=" * 70)
                print(f"  NOTE FILLED for {name} @ {clinic_name}")
                print(f"  Profile: {profile_url}")
                print(f"  Note:    {personalized_note}")
                print("=" * 70)
                print()
                print("  >>> Click 'Send' in the browser window, then come back here.")
                print("  >>> Or type 'skip' to skip this one.")
                print()

                user_input = input("  Press ENTER after sending, or type 'skip': ").strip().lower()

                if user_input == "skip":
                    print(f"  Skipped {name}")
                    log_connection(profile_url, clinic_name, name, "skipped", "User skipped")
                    continue

                # Log as sent (user confirmed by pressing ENTER)
                log_connection(profile_url, clinic_name, name, "sent", "User confirmed send")
                sent_count += 1
                print(f"  Logged as sent. ({sent_today + sent_count}/{DAILY_CAP} today)")

            except PlaywrightTimeoutError as e:
                print(f"  ERROR: Timeout — {e}")
                log_connection(profile_url, clinic_name, name, "error", f"Timeout: {e}")
            except Exception as e:
                print(f"  ERROR: {e}")
                log_connection(profile_url, clinic_name, name, "error", str(e))

            print()

        context.close()

    print()
    print("=" * 70)
    print(f"SESSION COMPLETE")
    print(f"Sent this run: {sent_count}")
    print(f"Total sent today: {sent_today + sent_count}")
    print(f"Log: {SENT_LOG_FILE}")
    print("=" * 70)


# ============================================================================
# INTERACTIVE MENU
# ============================================================================

def show_menu():
    print("\n" + "=" * 70)
    print("LinkedIn Connection Tool")
    print("=" * 70)
    print("  1. Find LinkedIn profiles (Step 1)")
    print("  2. Send connection requests (Step 2)")
    print("  3. Check data files")
    print("  4. Exit")
    print("=" * 70)
    return input("  Select (1-4): ").strip()


def check_existing_files():
    print("\n" + "=" * 70)
    print("Data Files Status")
    print("=" * 70)

    for f in [LEADS_FILE, PROFILES_FILE, SENT_LOG_FILE]:
        if os.path.exists(f):
            rows = read_csv(f)
            print(f"  {f} — {len(rows)} rows")
            if f == PROFILES_FILE:
                confirmed = sum(1 for r in rows if r.get("confirmed", "").lower() == "yes")
                print(f"    Confirmed: {confirmed}")
            elif f == SENT_LOG_FILE:
                sent = sum(1 for r in rows if r.get("status") == "sent")
                print(f"    Sent: {sent}")
        else:
            print(f"  {f} — not found")

    print("=" * 70)


def interactive_mode():
    while True:
        choice = show_menu()
        if choice == "1":
            step1_find_profiles()
        elif choice == "2":
            step2_send_connections()
        elif choice == "3":
            check_existing_files()
        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("Invalid choice.")
        input("\nPress ENTER to continue...")


# ============================================================================
# MAIN
# ============================================================================

def main():
    global LEADS_FILE
    parser = argparse.ArgumentParser(description="Semi-automated LinkedIn Connection Tool")
    parser.add_argument("--step", type=int, choices=[1, 2], help="Run step 1 or 2 directly")
    parser.add_argument("--file", type=str, help="Input CSV file (default: leads.csv)")
    args = parser.parse_args()

    if args.file:
        LEADS_FILE = args.file

    if args.step == 1:
        step1_find_profiles()
    elif args.step == 2:
        step2_send_connections()
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
