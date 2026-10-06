#!/usr/bin/env python3
"""
LinkedIn Sourcing Toolkit
=========================
Multi-lane tool for finding REAL owners of small dental clinics on LinkedIn,
built because keyword clinic-name search has a low hit rate (small practices
often have weak/absent LinkedIn presence for the OWNER specifically).

Lanes:
  A. company  - Search LinkedIn as a COMPANY, open the People tab, list the
                actual people linked to that company (uses LinkedIn's real
                company->employee graph, much higher validity than text match).
                Primary, recommended approach. Sales Navigator / Recruiter
                gives the full roster; plain LinkedIn shows a partial list
                (often enough to surface the owner).
  B. owner    - Manually enter owner name(s) sourced elsewhere (Google Maps
                listing, website About page). Search LinkedIn by NAME only;
                far higher hit rate than clinic-name search.
  C. dork     - Google dork: site:linkedin.com/in "<clinic name>" via Google
                (zero LinkedIn rate/ban risk), paste the profile URL manually.
                Fully manual, per-clinic.
  D. batch    - Run the original clinic-name auto-search across a file
                (default leads.csv = all 170), keeping ~10-20% real matches;
                rest need manual sourcing.

All lanes that yield a confirmed pick append a row to <PROFILES_FILE>
(leads_with_profiles.csv) with confirmed=yes, so Step 2 of the connector
sends only genuine picks. Nothing is sent from this tool.

Usage:
    python linkedin_finder.py company --file test_reals.csv
    python linkedin_finder.py owner --file leads_with_names.csv --limit 5
    python linkedin_finder.py dork
    python linkedin_finder.py batch --file leads.csv
    python linkedin_finder.py            # interactive menu

The owner lane (B) batch-reads the owner's name from the CSV's `candidate_name`
column when run with --file leads_with_names.csv (only rows where the scrape
happened AND the LLM found a person). Run it against leads_with_names.csv with
`python linkedin_finder.py owner --file leads_with_names.csv` — no typing needed;
it loops clinics, searches by candidate name, and asks you to pick/verify the match
(1/2/3/s/m/q) exactly as before. Rows where scrape failed or produced no name are
skipped automatically.
"""

import argparse
import os
import random
import re
import shutil
import sys
import time

from linkedin_connector import (
    LEADS_FILE,
    PROFILES_FILE,
    LINKEDIN_SEARCH_URL,
    USER_DATA_DIR,
    HEADLESS,
    SEARCH_QUERIES,
    TOP_RESULTS,
    check_for_checkpoint,
    get_clinic_key,
    read_csv,
    write_csv,
    validate_leads_csv,
    safe_goto,
    sync_playwright,
    PlaywrightTimeoutError,
)

# ============================================================================
# CONFIG
# ============================================================================

COMPANY_SEARCH_URL = "https://www.linkedin.com/search/results/companies/"
PEOPLE_SEARCH_URL = LINKEDIN_SEARCH_URL  # .../search/results/people/
PROFILES_FIELDNAMES = ["clinic_name", "city", "state", "name", "title", "profile_url", "confirmed", "personalized_note"]

# A row in the scrape output (leads_with_names.csv) is owner-lane eligible only
# when the LLM actually found a person. Every other status (no_person_found,
# extraction_failed, api_error, needs_manual_website, scrape_failed) has no name
# to search by, so those rows are skipped, not errored on.
OWNER_ELIGIBLE_STATUSES = {"ok", "ok_playwright_fallback"}

NAME_PREFIXES = ("dr.", "mr.", "mrs.", "ms.", "miss", "prof.", "prof")
DEGREE_SUFFIXES = ("d.d.s.", "dds", "d.m.d.", "dmd", "ph.d.", "m.d.", "f.a.g.d.", "fagd", "d.sc.")


def strip_name_prefix(name):
    """Strip title prefixes (Dr./Mr./Mrs./Ms./Prof.) from a scraped candidate
    name — LinkedIn profiles rarely carry 'Dr.' in the name field, and leading
    titles reduce search precision. 'Dr. Ishita Seth' -> 'Ishita Seth'."""
    n = (name or "").strip()
    lower = n.lower()
    for p in NAME_PREFIXES:
        if lower == p or lower.startswith(p + " "):
            n = n[len(p):].strip()
            break
    return n


def strip_degree_suffix(name):
    """Strip trailing degree qualifiers (', DDS', ' DMD', ', D.D.S.' etc.)
    e.g. 'Mengchi Steven Yang, DDS' -> 'Mengchi Steven Yang'."""
    n = (name or "").rstrip()
    lower = n.lower()
    for d in DEGREE_SUFFIXES:
        if lower.endswith(d):
            n = n[:len(n) - len(d)].rstrip(",; ")
            break
    return n


def prepare_search_name(name):
    """Name-only query for LinkedIn people search: strip title prefixes and
    trailing degree qualifiers, collapse whitespace."""
    n = strip_degree_suffix(strip_name_prefix(name))
    n = re.sub(r"\s+", " ", n)
    return n.strip()


def dedup_name(name):
    """Collapse doubled name text from extraction bugs: "Nina I. Nina I." -> "Nina I.",
    "Todd X ☆☆☆☆☆ Todd X" -> "Todd X", "Dr. Navid Rahmani Dr. Navid Rahmani" ->
    "Dr. Navid Rahmani". Handles star separators, repeated full names, and the
    LinkedIn 'is open to work' badge text leaking into name fields."""
    n = (name or "").strip()
    if not n:
        return n
    # strip LinkedIn badge fragments: "Jennifer L. Cox is open to workJennifer L. Cox"
    badge = "is open to work"
    lo = n.lower()
    idx = lo.find(badge)
    if idx >= 0:
        n = n[:idx].strip()
    # strip star separators: "Todd X ☆☆☆☆☆ Todd X"
    stripped = re.sub(r"[☆★]+", " ", n).strip()
    stripped = re.sub(r"\s+", " ", stripped)
    # check if the string is exactly X+X (doubled)
    half = len(stripped) // 2
    if half > 2:
        first = stripped[:half].strip()
        second = stripped[half:].strip()
        if first and first == second:
            return first
    return n


def filter_owner_eligible(leads):
    """Keep only rows whose scrape produced a person (status ok / ok_playwright_fallback
    AND a non-empty candidate_name). Returns the filtered list."""
    eligible, skipped = [], 0
    for lead in leads:
        status = (lead.get("scrape_status") or "").strip()
        name = (lead.get("candidate_name") or "").strip()
        if status in OWNER_ELIGIBLE_STATUSES and name:
            eligible.append(lead)
        else:
            skipped += 1
    if skipped:
        reason = (f"skipped {skipped} rows (not {sorted(OWNER_ELIGIBLE_STATUSES)} status "
                  "or missing candidate_name)")
        print(f"  [{reason}]")
    return eligible


# ============================================================================
# CSV helpers (append a confirmed pick, preserving existing rows)
# ============================================================================

def load_profiles():
    return read_csv(PROFILES_FILE)


def add_pick(clinic_name, city, state, name, title, profile_url):
    """Append a confirmed=yes pick to leads_with_profiles.csv.
    Returns True if the profile_url was newly added, False if already present.
    """
    existing = load_profiles()
    for p in existing:
        if p.get("profile_url") == profile_url:
            print(f"  Already in {PROFILES_FILE} (url match). Skipping duplicate.")
            return False

    name = dedup_name(name)
    entry = {
        "clinic_name": clinic_name,
        "city": city,
        "state": state,
        "name": name,
        "title": title,
        "profile_url": profile_url,
        "confirmed": "yes",
    }
    existing.append(entry)
    shutil.copyfile(PROFILES_FILE, PROFILES_FILE + ".bak")
    write_csv(PROFILES_FILE, existing, PROFILES_FIELDNAMES)
    print(f"  Saved pick -> {PROFILES_FILE}: {name} <{profile_url}> [confirmed=yes]")
    return True


def add_manual_pick_with_session(clinic_name, city, state):
    """Prompt manually for a person's name/title/profile_url and append."""
    print(f"\n  Manual add for: {clinic_name}")
    name = input("    Person name: ").strip()
    if not name:
        return
    title = input("    Title (optional): ").strip()
    url = input("    LinkedIn profile URL: ").strip()
    if not url:
        return
    add_pick(clinic_name, city, state, name, title, url)


def prompt_root(choices, prompt):
    while True:
        inp = input(prompt).strip().lower()
        if inp in choices:
            return inp
        print("  Invalid choice.")


# ============================================================================
# Browser session (persistent login, shared across lanes)
# ============================================================================

def open_session():
    """Launch the persistent-context browser, ensure logged in, return page."""
    p = sync_playwright().start()
    context = p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        headless=HEADLESS,
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()
    print("\nChecking LinkedIn login status...")
    safe_goto(page, "https://www.linkedin.com/login", timeout=30000, label="login")
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
                safe_goto(page, "https://www.linkedin.com/feed/", timeout=30000, label="post-login")
            except Exception:
                pass
        time.sleep(2)
        check_for_checkpoint(page, "(post-login)")

    print("LinkedIn session active.\n")
    return p, context, page


# ============================================================================
# SDUI-safe extraction scripts (class hashes rotate per deploy; use stable
# anchors `a[href*=...]` + innerText slicing).
# ============================================================================

COMPANY_EXTRACT_JS = """
(() => {
  const main = document.querySelector('main') || document.body;
  const anchors = Array.from(main.querySelectorAll('a[href*="/company/"]'));
  const rows = [];
  const seen = new Set();
  for (const a of anchors) {
    const href = (a.getAttribute('href') || '').replace(/\\?.*$/, '').split('#')[0];
    const m = href.match(/\\/company\\/([^/]+)/);
    if (!m || !m[1]) continue;
    const slug = m[1];
    if (seen.has(slug)) continue;
    const text = (a.innerText || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
    if (!text && !a.getAttribute('aria-label')) continue;
    seen.add(slug);
    rows.push({
      name: text || a.getAttribute('aria-label'),
      company_url: href.startsWith('http') ? href : 'https://www.linkedin.com' + href,
      slug: slug,
    });
  }
  return rows;
})()
"""

# Generic people extraction: enumerate stable /in/ anchors in <main> and, where
# possible, a nearby headline line. Works for both people-search results and
# company People tabs (which also render /in/ anchors with name + role).
PEOPLE_EXTRACT_JS = """
(() => {
  const main = document.querySelector('main') || document.body;
  const anchors = Array.from(main.querySelectorAll('a[href*="/in/"]'));
  const rows = [];
  const seen = new Set();
  for (const a of anchors) {
    const m = (a.getAttribute('href') || '').match(/\\/in\\/([^/?#]+)/);
    if (!m || !m[1]) continue;
    const handle = m[1];
    if (seen.has(handle)) continue;
    const aria = a.querySelector('span[aria-hidden="true"]');
    let name = (aria ? aria.textContent : a.textContent) || '';
    name = name.replace(/[\\s\\u00a0]+/g, ' ').replace(/^Status is (online|offline)\\.?\\s*/i, '')
               .replace(/'?s profile$/i, '').replace(/\\s*[•·].*$/, '').trim();
    if (!name) name = a.getAttribute('aria-label') || a.innerText || '';
    if (!name.trim()) continue;
    /* collapse doubled text: "Nina I. Nina I." or "Todd X ☆☆☆☆☆ Todd X ☆☆☆☆☆" */
    const stripped = name.replace(/[☆★]+/g, '').trim();
    const half = Math.floor(stripped.length / 2);
    if (half > 2 && stripped.substring(0, half).trim() === stripped.substring(half).trim()) {
      name = stripped.substring(0, half).trim();
    }
    seen.add(handle);
    rows.push({
      handle,
      name: name.trim(),
      profile_url: 'https://www.linkedin.com/in/' + handle + '/',
    });
  }
  return rows;
})()
"""


def extract_companies(page):
    """Return list of {name, company_url, slug} from a company-search page."""
    for sel in ["main a[href*='/company/']", "li.entity-result"]:
        try:
            page.wait_for_selector(sel, timeout=15000)
            break
        except PlaywrightTimeoutError:
            continue
    time.sleep(1)
    try:
        return page.evaluate(COMPANY_EXTRACT_JS) or []
    except Exception as e:
        print(f"  company extract error: {e}")
        return []


def extract_people(page):
    """Enumerate people /in/ refs visible on the current page (people-search
    OR company People tab). Returns list of {handle, name, profile_url}."""
    try:
        page.wait_for_selector("main a[href*='/in/']", timeout=15000)
    except PlaywrightTimeoutError:
        return []
    time.sleep(1)
    try:
        return page.evaluate(PEOPLE_EXTRACT_JS) or []
    except Exception as e:
        print(f"  people extract error: {e}")
        return []


# ============================================================================
# Lane A: COMPANY search -> People tab
# ============================================================================

def lane_company(leads):
    p, context, page = open_session()
    try:
        for idx, lead in enumerate(leads, 1):
            clinic_name = lead.get("clinic_name", "")
            city = lead.get("city", "")
            state = lead.get("state", "")

            print(f"\n=== {idx}/{len(leads)}: {clinic_name} @ {city}, {state} ===")

            # 1) Company search
            q = f"{clinic_name} {city}".strip()
            url = f"{COMPANY_SEARCH_URL}?keywords={q.replace(' ', '%20')}"
            safe_goto(page, url, timeout=60000, label=f"company-search: {q[:30]}")
            time.sleep(random.uniform(2, 4))
            if check_for_checkpoint(page, f"(company-search: {q[:30]})"):
                input("  Press ENTER after clearing checkpoint: ")

            companies = extract_companies(page)
            if not companies:
                print("  No company pages found for this clinic name.")
                add_manual_pick_with_session(clinic_name, city, state)
                continue

            # Dedup / show top few
            shown = companies[:5]
            print(f"  Company results ({len(companies)} found, showing {len(shown)}):")
            for i, c in enumerate(shown, 1):
                print(f"    {i}. {c['name']}")
                print(f"       {c['company_url']}")
            print(f"    s. Skip clinic")
            print(f"    m. No valid company - manually add owner URL")
            pick = prompt_root(
                [str(i) for i in range(1, len(shown) + 1)] + ["s", "m", "q"],
                "    Pick company (1/2/...), s=skip, m=manual, q=quit: ",
            )
            if pick == "q":
                break
            if pick == "s":
                continue
            if pick == "m":
                add_manual_pick_with_session(clinic_name, city, state)
                continue

            comp = shown[int(pick) - 1]

            # 2) People tab
            people_url = comp["company_url"].rstrip("/") + "/people/"
            safe_goto(page, people_url, timeout=60000, label=f"people: {comp['slug']}")
            time.sleep(random.uniform(2, 4))
            if check_for_checkpoint(page, f"(people: {comp['slug']})"):
                input("  Press ENTER after clearing checkpoint: ")

            people = extract_people(page)
            if not people:
                print("  No people listed on this company's People tab "
                      "(plain LinkedIn shows a partial list; consider Sales Navigator).")
                add_manual_pick_with_session(clinic_name, city, state)
                continue

            # Filter to likely-owner titles if present, but show everything
            shown_people = people[:8]
            print(f"  People at {comp['name']} ({len(people)} found, showing {len(shown_people)}):")
            for i, per in enumerate(shown_people, 1):
                print(f"    {i}. {per['name']}")
                print(f"       {per['profile_url']}")
            print(f"    s. Skip clinic")
            print(f"    o. Owner not listed - manually add URL")
            pick2 = prompt_root(
                [str(i) for i in range(1, len(shown_people) + 1)] + ["s", "o", "q"],
                "    Pick owner (1/2/...), s=skip, o=owner-not-listed, q=quit: ",
            )
            if pick2 == "q":
                break
            if pick2 == "s":
                continue
            if pick2 == "o":
                add_manual_pick_with_session(clinic_name, city, state)
                continue

            per = shown_people[int(pick2) - 1]
            add_pick(clinic_name, city, state, per["name"], "at " + comp["name"], per["profile_url"])

            # Gentle delay between clinics
            time.sleep(random.uniform(1, 2))
    finally:
        context.close()
        p.stop()


# ============================================================================
# Lane B: OWNER-name search
# ============================================================================

def lane_owner(leads):
    p, context, page = open_session()
    try:
        for idx, lead in enumerate(leads, 1):
            clinic_name = lead.get("clinic_name", "")
            city = lead.get("city", "")
            state = lead.get("state", "")
            candidate = (lead.get("candidate_name") or "").strip()
            candidate_title = (lead.get("candidate_title") or "").strip()
            conf = (lead.get("confidence") or "").strip()

            print(f"\n=== {idx}/{len(leads)}: {clinic_name} @ {city}, {state} ===")
            if candidate:
                print(f"  Scrape candidate: {candidate}"
                      + (f" ({conf} confidence)" if conf else "")
                      + (f" — {candidate_title}" if candidate_title else ""))
                owner = prepare_search_name(candidate)
                if owner != candidate.strip():
                    print(f"  Search name (prefix/degree stripped): {owner}")
            else:
                print("  No candidate_name in this row. Source the owner name from")
                print("  Google Maps / the clinic's About page.")
                raw_owner = input("  Owner full name (ENTER to skip): ").strip()
                if not raw_owner:
                    continue
                owner = prepare_search_name(raw_owner)

            q = f"{owner} {city}".strip()
            url = f"{PEOPLE_SEARCH_URL}?keywords={q.replace(' ', '%20')}"
            safe_goto(page, url, timeout=60000, label=f"owner-search: {owner[:30]}")
            time.sleep(random.uniform(2, 4))
            if check_for_checkpoint(page, f"(owner-search: {owner[:30]})"):
                input("  Press ENTER after clearing checkpoint: ")

            people = extract_people(page)
            if not people:
                print("  No people found for that name.")
                continue

            shown = people[:5]
            print(f"  People results ({len(people)} found, showing {len(shown)}):")
            for i, per in enumerate(shown, 1):
                print(f"    {i}. {per['name']}")
                print(f"       {per['profile_url']}")
            print(f"    s. Skip this owner")
            print(f"    m. Manually add a URL")
            pick = prompt_root(
                [str(i) for i in range(1, len(shown) + 1)] + ["s", "m", "q"],
                "    Pick match (1/2/...), s=skip, m=manual, q=quit: ",
            )
            if pick == "q":
                break
            if pick == "s":
                continue
            if pick == "m":
                title = input("    Title (optional): ").strip()
                url2 = input("    Profile URL: ").strip()
                add_pick(clinic_name, city, state, owner, title, url2)
                continue

            per = shown[int(pick) - 1]
            add_pick(clinic_name, city, state, per["name"], "", per["profile_url"])
            time.sleep(random.uniform(1, 2))
    finally:
        context.close()
        p.stop()


# ============================================================================
# Lane C: GOOGLE DORK (manual, no browser needed)
# ============================================================================

def lane_dork(leads):
    print("\n" + "=" * 70)
    print("LANE C: Google Dork - manual, zero LinkedIn ban risk")
    print("=" * 70)
    print("For each clinic, do this Google search (in your browser):")
    print("    site:linkedin.com/in \"<clinic name>\"")
    print("If you find the owner's profile URL, paste it here.\n")

    for idx, lead in enumerate(leads, 1):
        clinic_name = lead.get("clinic_name", "")
        city = lead.get("city", "")
        state = lead.get("state", "")
        print(f"\n=== {idx}/{len(leads)}: {clinic_name} ===")
        print(f"    Google query:  site:linkedin.com/in \"{clinic_name}\"")
        print("    Paste the owner's LinkedIn profile URL (with linkedin.com),")
        print("    or type 'n' to skip / 'q' to quit.")
        ans = input("    > ").strip()
        if ans.lower() == "q":
            break
        if ans.lower() == "n":
            continue
        if "linkedin.com" in ans:
            name = input("    Person name: ").strip()
            title = input("    Title (optional): ").strip()
            add_pick(clinic_name, city, state, name, title, ans)
        else:
            print("    Not recognized as a URL; skipping. (Paste a linkedin.com/in/... link.)")

    print("\nLane C complete.")


# ============================================================================
# Lane D: BATCH - delegate to connector's Step 1 (clinic-name auto-search)
# ============================================================================

def lane_batch(file_path):
    print("\n" + "=" * 70)
    print("LANE D: Batch auto-search (accept low hit rate)")
    print("=" * 70)
    print("Running the original clinic-name search across the file. Expect")
    print("only ~10-20% real matches; skip the rest.\n")
    # Reuse the connector's Step 1, which loads LEADS_FILE.
    import linkedin_connector as connector
    connector.LEADS_FILE = file_path
    connector.step1_find_profiles()


# ============================================================================
# MENU / MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Sourcing Toolkit (multi-lane)")
    parser.add_argument("lane", type=str, nargs="?", choices=["company", "owner", "dork", "batch"],
                        help="Lane to run")
    parser.add_argument("--file", type=str, default=LEADS_FILE,
                        help="Input CSV (clinic_name,city,state). Default: leads.csv")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N rows (0 = all). Test slice.")
    args = parser.parse_args()

    leads = read_csv(args.file)
    if not validate_leads_csv(leads, args.file):
        sys.exit(1)
    print(f"Loaded {len(leads)} leads from {args.file}")

    def slice_leads(rows):
        if args.limit and args.limit > 0:
            rows = rows[: args.limit]
        return rows

    if args.lane == "company":
        lane_company(slice_leads(leads))
    elif args.lane == "owner":
        eligible = filter_owner_eligible(leads)
        print(f"Owner-lane eligible rows: {len(eligible)}"
              + (f" (showing first {len(slice_leads(eligible))})" if args.limit else ""))
        lane_owner(slice_leads(eligible))
    elif args.lane == "dork":
        lane_dork(slice_leads(leads))
    elif args.lane == "batch":
        lane_batch(args.file)
    else:
        print("Choose a lane:")
        print("  company - company People-tab sourcing (recommended)")
        print("  owner   - search by owner name (batch candidate_name from --file)")
        print("  dork    - Google dork manual flow")
        print("  batch   - batch clinic-name auto-search")
        lane = prompt_root(["company", "owner", "dork", "batch"], "  Lane: ")
        if lane == "company":
            lane_company(slice_leads(leads))
        elif lane == "owner":
            eligible = filter_owner_eligible(leads)
            print(f"Owner-lane eligible rows: {len(eligible)}"
                  + (f" (showing first {len(slice_leads(eligible))})" if args.limit else ""))
            lane_owner(slice_leads(eligible))
        elif lane == "dork":
            lane_dork(slice_leads(leads))
        elif lane == "batch":
            lane_batch(args.file)


if __name__ == "__main__":
    main()
