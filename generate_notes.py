#!/usr/bin/env python3
"""
generate_notes.py
=================
Generate a personalized LinkedIn connection-request note for every confirmed
profile in leads_with_profiles.csv, using the free Groq API (same key/model as
scrape_and_extract.py).

For each row with confirmed=yes AND an empty/missing `personalized_note`:
  - build a prompt from (name, title, clinic_name, city)
  - call Groq, strip markdown/quotes from the response
  - enforce LinkedIn's hard 300-char limit (clean word-boundary truncation)
  - store the result in the new `personalized_note` column

Rows that are not confirmed, or already have a note, are SKIPPED so re-runs
don't overwrite notes or burn API calls. On a Groq failure the row is marked
personalized_note=GENERATION_FAILED so you can spot and hand-write those few.

Output is written back to the input file in place (existing columns preserved,
with a .bak copy kept).

Usage:
  python generate_notes.py                 # all eligible rows
  python generate_notes.py --limit 5       # first 5 eligible rows only
  python generate_notes.py --debug         # print raw Groq responses

Requires GROQ_API_KEY (free tier) - see README for setup.
"""

import argparse
import os
import random
import re
import shutil
import sys
import time

import requests

from scrape_and_extract import (
    GROQ_MODEL,
    GROQ_API_URL,
    classify_api_error,
)
from linkedin_connector import (
    read_csv,
    write_csv,
)

# ============================================================================
# CONFIG
# ============================================================================

DEFAULT_FILE = "leads_with_profiles.csv"
GROQ_TIMEOUT = 30
HARD_CHAR_LIMIT = 300          # LinkedIn's hard limit on connection notes
TL = 250                       # ask the LLM to stay well under the hard limit
GEN_FAILED = "GENERATION_FAILED"
MAX_GROQ_ATTEMPTS = 2           # max total attempts per note (1 + 1 retry)
RETRY_FALLBACK_SECS = 5         # if retry-after header missing/0
BATCH_DELAY_SECS = 1.5          # fixed delay between consecutive Groq calls

# Structural "angles" picked at random per row so notes don't all read alike.
ANGLE_TEXT = {
    "curiosity": (
        "Lead with genuine curiosity - open with a question about GENERIC "
        "practice-life topics that hold for any dental practice, such as 'what got "
        "you into running your own practice?' or 'what's it like leading a team in "
        "the local dental scene?'. Only ask universally-true questions like these "
        "about ownership and city - NEVER claim a specific focus, program, or "
        "trait of this particular practice, because no such data exists."
    ),
    "networking": (
        "Frame it as simple networking and general interest in the dental "
        "industry as an outside observer - never state, imply, or hint at the "
        "sender's own profession or occupation (it is unknown). No shared-"
        "profession framing."
    ),
    "direct_brief": (
        "Lead direct and brief - a short plain greeting, one concrete line, then "
        "stop. No praise pile-on, no filler."
    ),
}


def build_prompt(name, title, clinic_name, city, angle=None):
    role = f"who is {title}" if title else "who works"
    location = f" in {city}" if city else ""
    if angle is None:
        angle = random.choice(tuple(ANGLE_TEXT))
    return (
        "Write a short, friendly LinkedIn connection request note to "
        f"{name}, {role} at {clinic_name}{location}. "
        "Tone: warm, professional, non-salesy, no pitch, just genuine interest in "
        "connecting. Do not mention any product or service. "
        f"Angle for this note: {ANGLE_TEXT[angle]} "
        "Vary your sentence structure and opening - don't always start with "
        "\"I'm impressed by\". Mix it up: sometimes lead with a question, sometimes "
        "with a specific observation, sometimes with a simple direct greeting. "
        "Keep it natural and human, like different people wrote each note, so no "
        "two notes look alike. "
        "You only know the person's name, title, clinic name, and city - nothing "
        "else. Do NOT invent or assume any specific programs, events, achievements, "
        "or details about them or their practice. Keep the personalization grounded "
        "only in the generic fact that they run/work at a dental practice in that "
        "city - use tone and structure variety instead of fake specifics to sound "
        "personal. "
        "Never claim anything about the sender: their name, profession, "
        "occupation, or circumstances are UNKNOWN. Do not state or imply you are "
        "a dentist, work in dentistry, or share their profession - frame the note "
        "purely as general interest and networking, never a shared profession. "
        "Never run two words together without a space - every word must have a "
        "space before it, and spell-check your output before answering. "
        f"Strictly under {TL} characters. Output ONLY the note text, nothing else - "
        "no quotes, no markdown, no labels."
    )


def _parse_int_header(headers, key, default):
    """Parse an integer HTTP header (e.g. retry-after) safely."""
    try:
        val = int(headers.get(key, ""))
        return val
    except (TypeError, ValueError):
        return default


def _call_groq(prompt, api_key):
    """One Groq call. Returns dict:
    ok:          bool
    content:     str ('' if unusable)
    error:       str ('' when ok)
    status_code: int (0 = network error)
    finish_reason: str ('' when unknown/not parsed)
    body:        str (raw response text, always kept for debugging)
    headers:     dict (a few relevant headers, for debugging)
    A 200 with empty/null content is reported as NOT ok so it is never
    mislabeled as a successful generation.
    """
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "reasoning_effort": "low",
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
                timeout=GROQ_TIMEOUT,
            )
        except Exception as e:
            return {"ok": False, "content": "", "error": f"network error: {e.__class__.__name__}: {e}",
                    "status_code": 0, "finish_reason": "", "body": "", "headers": {}}

        if resp.status_code != 429:
            last = None
            break
        # 429: rate-limited. Honor retry-after, then retry once more.
        header = {}
        try:
            header = {k: v for k, v in resp.headers.items()
                      if k.lower() in ("retry-after", "content-type", "content-length", "date")}
        except Exception:
            pass
        retry_after = _parse_int_header(resp.headers, "retry-after", RETRY_FALLBACK_SECS)
        delay = retry_after if retry_after > 0 else RETRY_FALLBACK_SECS
        print(f"  rate-limited (429), waiting {delay}s then retrying...")
        time.sleep(delay)
        last = {"status_code": 429, "headers": header, "body": resp.text}
        continue

    if last is not None:
        # both attempts returned 429
        return {"ok": False, "content": "", "error": classify_api_error(429, last["body"]),
                "status_code": 429, "finish_reason": "", "body": last["body"], "headers": last["headers"]}

    body = resp.text
    headers = {k: v for k, v in resp.headers.items()
               if k.lower() in ("content-type", "content-length", "date")}

    if resp.status_code != 200:
        return {"ok": False, "content": "", "error": classify_api_error(resp.status_code, body),
                "status_code": resp.status_code, "finish_reason": "", "body": body, "headers": headers}

    try:
        data = resp.json()
    except Exception as e:
        return {"ok": False, "content": "", "error": f"response not JSON: {e}",
                "status_code": 200, "finish_reason": "", "body": body, "headers": headers}

    try:
        choice = data["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason") or ""
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = ""
    except Exception as e:
        return {"ok": False, "content": "", "error": f"unexpected response shape: {e}",
                "status_code": 200, "finish_reason": "", "body": body, "headers": headers}

    if not content.strip():
        return {"ok": False, "content": "", "error": f"empty content (finish_reason={finish!r})",
                "status_code": 200, "finish_reason": finish, "body": body, "headers": headers}

    return {"ok": True, "content": content, "error": "",
            "status_code": 200, "finish_reason": finish, "body": body, "headers": headers}


def _show_full_response(res, clinic_name, name):
    """Dump the COMPLETE raw Groq response for one row (pre-parsing)."""
    print()
    print("  " + "=" * 70)
    print("  FULL RAW GROQ RESPONSE (first failing row)")
    print(f"    row           : {clinic_name} -> {name}")
    print(f"    status_code   : {res['status_code']}")
    print(f"    headers       : {res['headers']}")
    print(f"    finish_reason : {res['finish_reason']!r}")
    print("    body:")
    for line in (res["body"] or "").splitlines():
        print("      | " + line)
    print("  " + "=" * 70)


def clean_response(raw):
    """Strip markdown fences and surrounding quotes from the LLM output."""
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    for open_q, close_q in (('"', '"'), ("'", "'"),
                            ("\u201c", "\u201d"), ("\u2018", "\u2019")):
        if txt.startswith(open_q) and txt.endswith(close_q):
            txt = txt[1:-1].strip()
            break
    return txt


def truncate_to_limit(note, limit=HARD_CHAR_LIMIT):
    """Hard-truncate at the last full word that fits under `limit`."""
    if len(note) <= limit:
        return note
    cut = note[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.strip()


CAMEL_MERGE_RE = re.compile(r"[a-z][A-Z]")          # "ourPractices"
LONG_RUN_RE = re.compile(r"[A-Za-z]{12,}")          # unbroken run: "ourpractices"


RETRY_SUFFIX = (
    " Fix the typos in your draft: there must be a space between every two "
    "words (never run them together), and spell-check the whole text."
)


def typo_flag(text):
    """Return a reason string when text shows likely merged/missing-space typos."""
    if CAMEL_MERGE_RE.search(text):
        return "camelCase merge (missing space between words)"
    m = LONG_RUN_RE.search(text)
    if m:
        return f"unbroken word run: {m.group(0)!r}"
    return ""


def find_eligible(rows, debug=False, force=False):
    """Yield (index, row) for rows needing a note: confirmed=yes and empty note.
    With force=True, re-generate even when a note already exists."""
    for i, row in enumerate(rows):
        confirmed = (row.get("confirmed") or "").strip().lower() == "yes"
        note = (row.get("personalized_note") or "").strip()
        if not confirmed:
            if debug:
                print(f"  skip [{i}]: not confirmed")
            continue
        if note and note != GEN_FAILED and not force:
            if debug:
                print(f"  skip [{i}]: note already present")
            continue
        yield i, row


def main():
    parser = argparse.ArgumentParser(description="Generate personalized LinkedIn notes via Groq")
    parser.add_argument("--file", type=str, default=DEFAULT_FILE,
                        help=f"Profiles CSV (default {DEFAULT_FILE})")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process the first N eligible rows (0 = all).")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw Groq responses + skip reasons.")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate notes even if a note already exists.")
    args = parser.parse_args()

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        print("=" * 70)
        print("  GROQ_API_KEY is not set.")
        print("  Get a free key at https://console.groq.com/keys, then:")
        print("    setx GROQ_API_KEY \"your_key_here\"   (then open a NEW terminal)")
        print("  or for just this session:")
        print("    $env:GROQ_API_KEY = \"your_key_here\"")
        print("=" * 70)
        sys.exit(1)

    rows = read_csv(args.file)
    if not rows:
        print(f"ERROR: no data in {args.file}")
        sys.exit(1)

    base_fieldnames = list(rows[0].keys())
    if "personalized_note" not in rows[0]:
        for r in rows:
            r["personalized_note"] = ""

    eligible = list(find_eligible(rows, debug=args.debug, force=args.force))
    if args.limit and args.limit > 0:
        eligible = eligible[: args.limit]

    total = len(rows)
    print(f"Loaded {len(rows)} rows from {args.file}; {len(eligible)} need a note"
          + (f" (processing first {len(eligible)})" if args.limit and args.limit < len(eligible) else "")
          + "\n")

    processed = 0
    failures = 0
    full_dumped = False

    for idx, row in eligible:
        clinic_name = row.get("clinic_name", "")
        name = row.get("name", "")
        title = row.get("title", "")
        city = row.get("city", "")

        angle = random.choice(tuple(ANGLE_TEXT))
        prompt = build_prompt(name, title, clinic_name, city, angle=angle)
        if args.debug:
            print(f"  [angle: {angle}]")
        res = _call_groq(prompt, api_key)

        if args.debug and res["ok"]:
            print(f"  [GROQ content]: {res['content'][:300]!r}")

        if idx != eligible[-1][0]:
            time.sleep(BATCH_DELAY_SECS)

        if res["ok"]:
            note = truncate_to_limit(clean_response(res["content"]))
            reason = typo_flag(note)
            if reason:
                print(f"  ... typo check: {reason} -> retrying once")
                res = _call_groq(prompt + RETRY_SUFFIX, api_key)
                if res["ok"]:
                    retry_note = truncate_to_limit(clean_response(res["content"]))
                    if typo_flag(retry_note):
                        print(f"  ... still flagged on retry; keeping draft with TYPO WARNING")
                    else:
                        note = retry_note
            if not note:
                # content was whitespace-only after scrubbing (unlikely: ok implies
                # non-empty, but guard anyway)
                note = GEN_FAILED
                failures += 1
                print(f"  {idx + 1}/{total}: {clinic_name} -> {name}: scrubbed to empty")
                if args.debug and not full_dumped:
                    _show_full_response(res, clinic_name, name)
                    full_dumped = True
            else:
                print(f"  {idx + 1}/{total}: {clinic_name} -> {name}: note OK ({len(note)} chars)")
        else:
            note = GEN_FAILED
            failures += 1
            print(f"  {idx + 1}/{total}: {clinic_name} -> {name}: GROQ FAILED ({res['error']})")
            if args.debug and not full_dumped:
                _show_full_response(res, clinic_name, name)
                full_dumped = True

        row["personalized_note"] = note
        processed += 1

    fieldnames = base_fieldnames
    if "personalized_note" not in fieldnames:
        fieldnames = fieldnames + ["personalized_note"]

    shutil.copyfile(args.file, args.file + ".bak")
    write_csv(args.file, rows, fieldnames)
    print()
    print("=" * 70)
    print(f"Wrote {len(rows)} rows to {args.file} (backup: {args.file}.bak)")
    print(f"  processed:                {processed}")
    print(f"  skipped (not confirmed):  {sum(1 for r in rows if (r.get('confirmed') or '').strip().lower() != 'yes')}")
    print(f"  skipped (note present):   {sum(1 for r in rows if (r.get('confirmed') or '').strip().lower() == 'yes' and (r.get('personalized_note') or '').strip() and r.get('personalized_note') != GEN_FAILED)}")
    print(f"  GROQ failures (marked {GEN_FAILED}): {failures}")
    print("=" * 70)
    if failures:
        print("  Hand-write notes for the GENERATION_FAILED rows before sending.")


if __name__ == "__main__":
    main()