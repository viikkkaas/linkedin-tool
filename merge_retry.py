#!/usr/bin/env python3
"""
merge_retry.py
==============
Fold scrape-retry results + repair source-data corruption into leads_with_names.csv.

1) MERGE
   Rows in the patch file (retry_results.csv) with a real outcome update their
   matching row in the base file (leads_with_names.csv) in place:
     - scrape_status in {ok, ok_playwright_fallback} -> success, fold candidate fields
     - no_person_found / extraction_failed           -> adopt the new (more accurate)
       status so downstream truthful, matching the full-pipeline result
   Rows in the base file that were ALREADY a success are never touched/downgraded.

2) REPAIR
   Rows whose `website` column holds a non-URL (a state value like 'NC') were
   corrupted by an unquoted comma in the source clinic_name (columns shifted right
   by one). They are looked up in the raw export (leads.csv, optional):
     - if a real website exists there -> restored, columns fixed
     - otherwise website is cleared and scrape_status='needs_manual_website'
       (visible flag, never silently skipped)

Matching key for the merge is (clinic_name, city, state), identical in base and
patch because retry_results.csv was filtered out of leads_with_names.csv.

Usage:
  python merge_retry.py                  # does both steps, writes leads_with_names.csv
  python merge_retry.py --dry-run        # preview only, write nothing
  python merge_retry.py --no-repair      # skip the corruption fix
"""

import argparse
import csv
import os
import shutil

BASE_DEFAULT = "leads_with_names.csv"
PATCH_DEFAULT = "retry_results.csv"
RAW_DEFAULT = "leads.csv"

SUCCESS = {"ok", "ok_playwright_fallback"}
OUTPUT_FIELDNAMES = [
    "clinic_name", "city", "state", "website",
    "candidate_name", "candidate_title", "confidence", "scrape_status",
]


def read_rows(path, required):
    if not os.path.exists(path):
        return None
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if required and not required.issubset(set(reader.fieldnames or [])):
            return None
        return list(reader)


def write_rows(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def key(row):
    return (row.get("clinic_name", "").strip(),
            row.get("city", "").strip(),
            row.get("state", "").strip())


def is_junk_website(row):
    website = (row.get("website") or "").strip()
    return "." not in website


def lookup_raw(raw_rows, clinic, city):
    """Try to reconstruct the true clinic_name (original had an unquoted comma)
    and find the row in the raw export."""
    joined = f"{clinic}, {city}".strip()
    variants = {joined, joined.replace(", ", ","), joined.replace(" , ", ", ")}
    for r in raw_rows:
        name = (r.get("clinic_name") or "").strip()
        if name in variants or name.replace(", ", ",") in variants:
            return r
    for r in raw_rows:
        name = (r.get("clinic_name") or "").strip()
        if clinic and name.lower().startswith(clinic.lower()):
            return r
    return None


def repair_junk(row, raw_rows):
    """Fix a corrupted (shifted) row. Returns True if it was a known shift."""
    clinic, city = row.get("clinic_name", "").strip(), row.get("city", "").strip()
    rec = lookup_raw(raw_rows, clinic, city)
    if rec is not None:
        row["clinic_name"] = (rec.get("clinic_name") or "").strip()
        row["city"] = (rec.get("city") or "").strip()
        row["state"] = (rec.get("state") or "").strip()
        website = (rec.get("website") or "").strip()
        row["website"] = website
        if website:
            return True
        row["scrape_status"] = "needs_manual_website"
        return True
    row["website"] = ""
    row["scrape_status"] = "needs_manual_website"
    return True


def main():
    parser = argparse.ArgumentParser(description="Merge scrape retries + fix corrupted website rows")
    parser.add_argument("--base", default=BASE_DEFAULT, help=f"base file (default {BASE_DEFAULT})")
    parser.add_argument("--patch", default=PATCH_DEFAULT, help=f"retry results (default {PATCH_DEFAULT})")
    parser.add_argument("--raw", default=RAW_DEFAULT, help=f"raw export for repair lookup (default {RAW_DEFAULT})")
    parser.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    parser.add_argument("--no-repair", action="store_true", help="skip the website-corruption fix")
    args = parser.parse_args()

    base = read_rows(args.base, required={"clinic_name"})
    if base is None:
        print(f"ERROR: base file not found or missing 'clinic_name': {args.base}")
        return 1
    patch = read_rows(args.patch, required={"clinic_name"})
    if patch is None:
        print(f"ERROR: patch file not found or missing 'clinic_name': {args.patch}")
        return 1
    raw = read_rows(args.raw, required=set()) if not args.no_repair else []

    patch_index = {}
    for p in patch:
        patch_index.setdefault(key(p), []).append(p)

    merged_success, adopted, repaired, flagged_manual = 0, 0, 0, 0
    for b in base:
        patch_candidates = patch_index.get(key(b)) or []

        if b.get("scrape_status", "").strip() not in SUCCESS:
            for p in patch_candidates:
                patched = False
                if p.get("scrape_status", "").strip() in SUCCESS:
                    b["candidate_name"] = p.get("candidate_name", "")
                    b["candidate_title"] = p.get("candidate_title", "")
                    b["confidence"] = p.get("confidence", "")
                    b["scrape_status"] = p.get("scrape_status", "")
                    merged_success += 1
                    patched = True
                elif p.get("scrape_status", "").strip() in ("no_person_found", "extraction_failed"):
                    b["scrape_status"] = p.get("scrape_status", "")
                    adopted += 1
                    patched = True
                if patched:
                    break

        if not args.no_repair and is_junk_website(b):
            repair_junk(b, raw or [])
            if b.get("scrape_status", "") == "needs_manual_website":
                flagged_manual += 1
            else:
                repaired += 1

    merged_success_final = merged_success
    adopted_final = adopted

    if args.dry_run:
        print(f"[dry-run] merge: {merged_success_final} success, {adopted_final} adopted; "
              f"repair: {repaired} restored, {flagged_manual} flagged needs_manual_website")
        return 0

    shutil.copyfile(args.base, args.base + ".bak")
    write_rows(args.base, base)
    print(f"[merge]  {merged_success_final} success rows folded in")
    print(f"[merge]  {adopted_final} rows updated to no_person_found / extraction_failed")
    print(f"[repair] {repaired} website rows restored from {args.raw}")
    print(f"[repair] {flagged_manual} rows flagged needs_manual_website (no URL anywhere in source)")
    print(f"[done]   wrote {args.base} (backup kept at {args.base}.bak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())