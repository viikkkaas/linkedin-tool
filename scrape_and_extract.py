#!/usr/bin/env python3
"""
scrape_and_extract.py
=====================
Pre-processing stage for the LinkedIn sourcing pipeline: for each dental clinic,
scrape its website for an owner / decision-maker (founder / lead dentist), then
use a FREE Groq LLM call to extract the person's name + title.

Input : .csv  (clinic_name,city,state,website)
Output: leads_with_names.csv
        (clinic_name,city,state,website,candidate_name,candidate_title,
         confidence,scrape_status)

scrape_status values:
  ok                      - scraped text, LLM returned a person
  ok_playwright_fallback  - text found via Playwright (JS-rendered site), LLM returned a person
  no_person_found         - scraped text, LLM said no clear identifiable person
  extraction_failed       - scraped text, but LLM JSON could not be parsed (after 1 retry)
  api_error               - Groq API call failed (bad key, rate limit, retired model, etc.)
  scrape_failed           - website fetch failed (no text available)

Requires GROQ_API_KEY (free tier). See README for setup.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ============================================================================
# CONFIG
# ============================================================================

DEFAULT_INPUT = "dental_leads_with_websites.csv"
DEFAULT_OUTPUT = "leads_with_names.csv"

OUTPUT_FIELDNAMES = [
    "clinic_name", "city", "state", "website",
    "candidate_name", "candidate_title", "confidence", "scrape_status",
]

REQUEST_TIMEOUT = 10          # seconds per fetch
MIN_DELAY = 1.0               # polite-scrape delay between sites
MAX_DELAY = 2.0
MAX_TEXT_CHARS = 3000         # truncate combined homepage+team text
MAX_EXTRA_PAGES = 1           # at most one extra page beyond home page

# Requests-first scraping, with Playwright fallback for JS-rendered sites (React/Wix/Squarespace)
REQUESTS_MIN_TEXT_CHARS = 200    # if requests yields less text than this, retry with Playwright
PW_NAV_TIMEOUT_MS = 30000        # page load timeout for the headless browser
PW_NETWORK_IDLE_MS = 3000        # how long to wait for network idle before grabbing the DOM

# Keywords that mark an "About / Team / Staff" page link
TEAM_LINK_KEYWORDS = ["about", "team", "staff", "meet", "doctors", "our-team", "who-we-are"]
# Keywords rejected when they appear inside the link href/text (nav/contact noise)
TEAM_LINK_EXCLUDE = ["facebook", "instagram", "youtube", "twitter", "tiktok", "linkedin", "mailto", "tel:"]

# Groq config
# llama-3.3-70b-versatile was shut down Aug 16 2026 (HTTP 404 on every call).
# Documented migration: https://console.groq.com/docs/deprecations
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_GROQ_ATTEMPTS = 2           # max total attempts per call (1 + 1 retry on 429)
RETRY_FALLBACK_SECS = 5         # if retry-after header missing/0

# Prompt sent to the LLM (keep it strict and cheap)
GROQ_PROMPT = (
    "You are extracting the owner / decision-maker of a dental practice from its "
    "website text. People to look for: owner, founder, practice owner, lead "
    "dentist, clinical director. Doctors (Dr. X) are strong candidates.\n\n"
    "Extract the single most likely decision-maker.\n"
    'Reply ONLY with JSON: {"name": "...", "title": "...", "confidence": '
    '"high|medium|low"}\n'
    'If no clear person is identifiable, reply: {"name": null, "title": null, '
    '"confidence": "none"}\n\n'
    "Website text:\n{text}"
)


# ============================================================================
# CSV IO
# ============================================================================

def read_rows(path):
    if not os.path.exists(path):
        print(f"ERROR: input file not found: {path}")
        sys.exit(1)
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"clinic_name", "website"}
        if not required.issubset(set(reader.fieldnames or [])):
            print(f"ERROR: {path} must have at least columns: clinic_name, website")
            sys.exit(1)
        return list(reader)


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


# ============================================================================
# STEP A: WEBSITE SCRAPING
# ============================================================================

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


def fetch(url):
    """Fetch a URL, return (status, html). status in {ok, error}."""
    try:
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return "ok", resp.text
    except Exception as e:
        return "error", f"{e.__class__.__name__}: {e}"


def extract_visible_text(html):
    """Strip script/style/nav/footer and return normalized visible body text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    nav = soup.find_all(["nav", "header", "footer"])
    for tag in nav:
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def find_team_page(home_html, home_url):
    """Find the most likely About/Team/Staff page URL on the homepage.
    Returns a URL string, or None. Scans href + link text, prefers
    'our-team'/'meet'/'team'/'staff' over generic 'about'."""
    soup = BeautifulSoup(home_html, "html.parser")
    home_base = urlparse(home_url)

    candidates = []  # (priority, url)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low_href = href.lower()
        text = (a.get_text(" ", strip=True) or "").lower()

        if any(noise in low_href for noise in TEAM_LINK_EXCLUDE):
            continue

        joined = urljoin(home_url, href)
        joined_low = joined.lower()

        # only same-site links
        j = urlparse(joined)
        if j.netloc and j.netloc != home_base.netloc:
            continue

        # combined haystack to match keywords against (text + href path)
        haystack = text + " " + low_href + " " + urlparse(joined_low).path

        # priority scoring
        prio = None
        if not prio and ("our-team" in haystack or "meet-the-team" in haystack or "meet our team" in haystack
                         or "meet the team" in haystack):
            prio = 0
        if not prio and ("team" in haystack or "staff" in haystack or "doctors" in haystack):
            prio = 1
        if not prio and ("about" in haystack):
            prio = 2
        if prio is None:
            continue

        candidates.append((prio, joined))

    if not candidates:
        return None
    # lowest priority number wins
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def scrape_clinic(home_url):
    """Scrape clinic website text (home + at most one team/about page).
    Returns (status, combined_text).
      status: 'ok' or 'scrape_failed'
    """
    combined_chunks = []

    status1, html1 = fetch(home_url)
    if status1 != "ok":
        return "scrape_failed", ""

    text1 = extract_visible_text(html1)
    combined_chunks.append(text1)

    # find + follow one about/team page (max 1 extra fetch)
    team_url = find_team_page(html1, home_url)
    if team_url and team_url.rstrip("/") != home_url.rstrip("/"):
        status2, html2 = fetch(team_url)
        if status2 == "ok":
            text2 = extract_visible_text(html2)
            # avoid emitting the same text twice (rough dup: identical normalized text)
            if text2 and text2 != text1:
                combined_chunks.append(text2)

    combined = "\n\n".join(chunk for chunk in combined_chunks if chunk)
    combined = combined[:MAX_TEXT_CHARS]
    if not combined.strip():
        return "scrape_failed", ""
    return "ok", combined


# ============================================================================
# STEP A.5: PLAYWRIGHT FALLBACK (for JS-rendered sites)
# ============================================================================

# One shared headless browser, started lazily and reused across fallback sites.
_pw_state = {"playwright": None, "browser": None, "context": None, "unavailable": None}


def _pw_ensure_context():
    """Start the shared headless Chromium if needed. Returns context or None."""
    if _pw_state["context"] is not None:
        return _pw_state["context"]
    if _pw_state["unavailable"]:
        return None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        # Use Playwright's own bundled Chromium UA: the requests Chrome/124 spoof
        # gets HTTP 403 from several clinic-site WAFs, while the default UA passes.
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            ignore_https_errors=True,  # several clinic sites have expired/invalid certs
        )
        _pw_state.update(playwright=pw, browser=browser, context=context)
        return context
    except Exception as e:
        _pw_state["unavailable"] = f"{e.__class__.__name__}: {e}"
        print(f"    [playwright] fallback disabled ({_pw_state['unavailable']})"
              " - install with: python -m playwright install chromium")
        return None


def _pw_fetch(url):
    """Load a URL in the shared headless browser, return (status, rendered_html)."""
    ctx = _pw_ensure_context()
    if ctx is None:
        return "error", ""
    page = ctx.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PW_NAV_TIMEOUT_MS)
        if response is not None and not response.ok:
            return "error", f"HTTP {response.status}"
        try:
            page.wait_for_load_state("networkidle", timeout=PW_NETWORK_IDLE_MS)
        except Exception:
            pass  # ads/analytics keep the network busy — grab the DOM anyway
        return "ok", page.content()
    except Exception as e:
        return "error", f"{e.__class__.__name__}: {e}"
    finally:
        page.close()


def _pw_close():
    """Shut down the shared browser (call at end of run)."""
    try:
        if _pw_state["context"] is not None:
            _pw_state["context"].close()
        if _pw_state["browser"] is not None:
            _pw_state["browser"].close()
        if _pw_state["playwright"] is not None:
            _pw_state["playwright"].stop()
    except Exception:
        pass
    _pw_state.update(playwright=None, browser=None, context=None)


def scrape_clinic_playwright(home_url):
    """Scrape a clinic website with a real headless browser (JS-rendered sites).
    Same extraction path as scrape_clinic: home + at most one team page, visible
    text stripped of script/style/nav/footer.
    Returns (status, combined_text); status in {'ok','scrape_failed'}.
    """
    combined_chunks = []

    status1, html1 = _pw_fetch(home_url)
    if status1 != "ok":
        return "scrape_failed", ""

    text1 = extract_visible_text(html1)
    combined_chunks.append(text1)

    team_url = find_team_page(html1, home_url)
    if team_url and team_url.rstrip("/") != home_url.rstrip("/"):
        status2, html2 = _pw_fetch(team_url)
        if status2 == "ok":
            text2 = extract_visible_text(html2)
            if text2 and text2 != text1:
                combined_chunks.append(text2)

    combined = "\n\n".join(chunk for chunk in combined_chunks if chunk)
    combined = combined[:MAX_TEXT_CHARS]
    if not combined.strip():
        return "scrape_failed", ""
    return "ok", combined


# ============================================================================
# STEP B: GROQ LLM EXTRACTION
# ============================================================================

def classify_api_error(status_code, body):
    """Classify a non-200 Groq response into a human-readable detail string."""
    body_lower = body.lower()
    if status_code == 401:
        return "Invalid Groq API key (401 Unauthorized). Check GROQ_API_KEY is correct."
    if status_code == 403:
        return "Groq access denied (403 Forbidden). Your key may lack access to this model."
    if status_code == 404:
        return (f"Model not found (404). The model may have been retired or the name is wrong. "
                "Check https://console.groq.com/docs/models for valid model IDs. "
                f"Raw: {body[:300]}")
    if status_code == 429:
        return "Rate limit / quota exceeded (429). Wait and retry, or check Groq dashboard for usage."
    if status_code >= 500:
        return f"Groq server error (HTTP {status_code}). Retry shortly."
    return f"Unexpected Groq API response (HTTP {status_code}). Raw: {body[:300]}"


def _parse_int_header(headers, key, default):
    """Parse an integer HTTP header (e.g. retry-after) safely."""
    try:
        val = int(headers.get(key, ""))
        return val
    except (TypeError, ValueError):
        return default


def _call_groq(text, api_key):
    """One Groq chat completion call. Returns dict:
    ok:         bool
    status_code: int (0 = network error)
    raw:        str (LLM content on success; error body on failure)
    error:      str (human-readable detail; empty on success)
    429 responses are retried once after honoring retry-after (fallback 5s).
    """
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "user", "content": GROQ_PROMPT.replace("{text}", text)},
        ],
        "temperature": 0.0,
        "max_completion_tokens": 1024,
    }
    last = None
    for attempt in range(MAX_GROQ_ATTEMPTS):
        try:
            resp = requests.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
        except Exception as e:
            return {"ok": False, "status_code": 0, "raw": "",
                    "error": f"Network error: {e.__class__.__name__}: {e}"}

        if resp.status_code != 429:
            last = None
            break
        retry_after = _parse_int_header(resp.headers, "retry-after", RETRY_FALLBACK_SECS)
        delay = retry_after if retry_after > 0 else RETRY_FALLBACK_SECS
        print(f"  rate-limited (429), waiting {delay}s then retrying...")
        time.sleep(delay)
        last = {"status_code": 429, "body": resp.text}
        continue

    if last is not None:
        return {"ok": False, "status_code": 429, "raw": last["body"][:2000],
                "error": classify_api_error(429, last["body"])}

    body = resp.text
    if resp.status_code != 200:
        return {"ok": False, "status_code": resp.status_code, "raw": body[:2000],
                "error": classify_api_error(resp.status_code, body)}

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return {"ok": True, "status_code": 200, "raw": content, "error": ""}
    except Exception as e:
        return {"ok": False, "status_code": 200, "raw": body[:2000],
                "error": f"Malformed Groq response body: {e}"}


def _parse_json_loose(content):
    """Parse a possibly markdown-fenced JSON string."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fence
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        # try to find {...} blob
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def extract_decision_maker_groq(text, api_key, debug=False):
    """Extract name+title via Groq.
    Returns (name, title, confidence, status, detail).
      status:  'ok' | 'no_person_found' | 'extraction_failed' | 'api_error'
      detail:  empty on ok; else human-readable reason (may include raw response excerpt)
    """

    def _try_parse(raw_content):
        """Attempt to parse raw LLM response. Returns (name, title, conf, status, detail).
        status=None means 'not parseable as valid JSON' — signal to caller to retry."""
        parsed = _parse_json_loose(raw_content)
        if parsed and isinstance(parsed, dict):
            name = (parsed.get("name") or "").strip()
            conf = (parsed.get("confidence") or "").strip().lower()
            title = (parsed.get("title") or "").strip()
            if not name or conf == "none":
                return None, None, "none", "no_person_found", ""
            if conf not in ("high", "medium", "low"):
                conf = ""
            return name, title, conf, "ok", ""
        return None, None, "", None, ""  # status=None sentinel

    # --- Attempt 1 ---
    r = _call_groq(text, api_key)
    if not r["ok"]:
        return None, None, "", "api_error", r["error"]

    raw = r["raw"]
    if debug:
        print(f"    [GROQ RAW (attempt 1)]: {raw[:800]}")

    name, title, conf, status, detail = _try_parse(raw)
    if status is not None:
        return name, title, conf, status, detail

    # --- Attempt 2 (explicit no-fences reminder) ---
    retry_text = text + (
        "\n\nCRITICAL: Do NOT wrap JSON in markdown code fences. "
        "Return ONLY the raw JSON object with no formatting."
    )
    r2 = _call_groq(retry_text, api_key)
    if not r2["ok"]:
        return None, None, "", "api_error", r2["error"]

    raw2 = r2["raw"]
    if debug:
        print(f"    [GROQ RAW (attempt 2)]: {raw2[:800]}")

    name2, title2, conf2, status2, detail2 = _try_parse(raw2)
    if status2 is not None:
        return name2, title2, conf2, status2, detail2

    # --- Still broken: report with first raw for diagnostics ---
    return (None, None, "", "extraction_failed",
            f"LLM returned non-JSON after 2 attempts. First response: {raw[:500]}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Scrape clinic websites + extract decision-maker via Groq")
    parser.add_argument("--input", type=str, default=DEFAULT_INPUT,
                        help=f"Input CSV with (clinic_name,...,website). Default: {DEFAULT_INPUT}")
    parser.add_argument("--output", type=str, default=DEFAULT_OUTPUT,
                        help=f"Output CSV. Default: {DEFAULT_OUTPUT}")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N rows (0 = all). Useful for testing.")
    parser.add_argument("--no-groq", action="store_true",
                        help="Skip LLM calls. Useful for testing scraping only (candidate fields left empty).")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw Groq response text for every LLM call.")
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not args.no_groq and not api_key:
        print("=" * 70)
        print("  GROQ_API_KEY is not set.")
        print()
        print("  Step B (name extraction) needs a free Groq API key.")
        print("  1. Sign up free at https://console.groq.com/keys")
        print("  2. Create an API key")
        print("  3. Set the env var, e.g. (Windows PowerShell):")
        print("       setx GROQ_API_KEY \"your_key_here\"")
        print("     then open a NEW terminal window.")
        print("  Or set it for just this run (current window only):")
        print("       $env:GROQ_API_KEY = \"your_key_here\"")
        print("=" * 70)
        sys.exit(1)

    rows = read_rows(args.input)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    print(f"Loaded {len(rows)} clinics from {args.input}\n")

    results = []
    total = len(rows)

    for idx, row in enumerate(rows, 1):
        clinic_name = row.get("clinic_name", "").strip()
        website = row.get("website", "").strip()
        city = row.get("city", "").strip()
        state = row.get("state", "").strip()

        out = {
            "clinic_name": clinic_name,
            "city": city,
            "state": state,
            "website": website,
            "candidate_name": "",
            "candidate_title": "",
            "confidence": "",
            "scrape_status": "scrape_failed",
        }

        # --- STEP A: scrape (requests first, Playwright fallback for JS-rendered sites) ---
        status, text = scrape_clinic(website)
        if status == "scrape_failed" or len(text.strip()) < REQUESTS_MIN_TEXT_CHARS:
            pw_status, pw_text = scrape_clinic_playwright(website)
            if pw_status == "ok" and len(pw_text.strip()) >= REQUESTS_MIN_TEXT_CHARS:
                status, text = "ok_playwright_fallback", pw_text
            else:
                out["scrape_status"] = "scrape_failed"
                results.append(out)
                print(f"  {idx}/{total}: {clinic_name} -> scrape failed (no website text)"
                      + (f"  [requests='{status}', playwright='{pw_status}']" if args.debug else ""))
                _polite_delay()
                continue

        # --- STEP B: extract ---
        if args.no_groq:
            out["scrape_status"] = status
            results.append(out)
            print(f"  {idx}/{total}: {clinic_name} -> scraped {status}, LLM skipped (--no-groq)")
            _polite_delay()
            continue

        name, title, conf, llm_status, detail = extract_decision_maker_groq(
            text, api_key, debug=args.debug
        )
        out["scrape_status"] = llm_status
        if llm_status == "ok" and status == "ok_playwright_fallback":
            out["scrape_status"] = "ok_playwright_fallback"
        out["candidate_name"] = name or ""
        out["candidate_title"] = title or ""
        out["confidence"] = conf

        if llm_status == "ok":
            print(f"  {idx}/{total}: {clinic_name} -> found: {name} ({conf} confidence)"
                  + (f" — {title}" if title else ""))
        elif llm_status == "no_person_found":
            print(f"  {idx}/{total}: {clinic_name} -> no person found, will need fallback lane")
        elif llm_status == "api_error":
            print(f"  {idx}/{total}: {clinic_name} -> API ERROR: {detail}")
        else:  # extraction_failed
            print(f"  {idx}/{total}: {clinic_name} -> extraction failed: {detail}")

        results.append(out)
        _polite_delay()

    _pw_close()

    # --- write output ---
    write_rows(args.output, results)
    print()
    print("=" * 70)
    print(f"Wrote {len(results)} rows to {args.output}")
    _print_summary(results)
    print("=" * 70)


def _polite_delay():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _print_summary(results):
    """Print counts by scrape_status and by confidence."""
    from collections import Counter
    by_status = Counter(r.get("scrape_status", "") for r in results)
    by_conf = Counter(r.get("confidence", "") for r in results)

    print("  By scrape_status:")
    for k, v in sorted(by_status.items()):
        print(f"    {k or 'unknown'}: {v}")
    print("  By confidence:")
    for k in ("high", "medium", "low", "none"):
        print(f"    {k}: {by_conf.get(k, 0)}")
    ok = by_status.get("ok", 0) + by_status.get("ok_playwright_fallback", 0)
    total = len(results)
    pct = (ok / total * 100) if total else 0
    print(f"  -> {ok}/{total} ({pct:.0f}%) rows with a decision-maker name to feed into the owner lane.")


if __name__ == "__main__":
    main()