#!/usr/bin/env python3
"""
Recover a clean clinic_name, city, state CSV from the mangled leads.csv export.

The current leads.csv is a paste of two different exports:
  - 'good' rows:   name = clinic name,  address = full address
  - 'shifted' rows: name = junk,         clinic name in category, address in reviews

Creates a backup then writes leads.csv in the canonical format the tool expects.

Usage:
    python fix_leads_csv.py                 # recover leads.csv -> leads.csv
    python fix_leads_csv.py --dry-run       # write cleaned_leads.csv instead (no overwrite)
"""

import argparse
import csv
import os
import re
import shutil
from collections import Counter

SRC_FILE = "leads.csv"
OUT_FILE = "leads.csv"
DRY_RUN_OUT = "cleaned_leads.csv"
BACKUP = "leads.csv.bak"

STATE_ZIP = re.compile(r"^([A-Z]{2})\s+\d{5}(?:\d{4})?$")


def addr_fields(s):
    """Parse 'city', 'state' out of an address string.

    Handles '2201 South Blvd #210, Charlotte, NC 28203',
    'New York, NY 10012', and '200 Main St #1, Setauket- East Setauket, NY 11733'.
    """
    parts = [p.strip() for p in (s or "").split(",")]
    for i, part in enumerate(parts):
        m = STATE_ZIP.match(part)
        if m and i >= 1:
            return parts[i - 1], m.group(1)
    return "", ""


def is_shifted(name):
    return bool(re.match(r"^[a-z ]+, \d{5}, ", name or ""))


def parse_row(r, warnings):
    name = (r.get("name") or "").strip()
    if is_shifted(name):
        clinic = (r.get("category") or "").strip()
        city, state = addr_fields((r.get("reviews") or "").strip())
    elif name:
        clinic = name
        city, state = addr_fields((r.get("address") or "").strip())
    else:
        warnings.append("Row with no name column value")
        return None

    if not clinic:
        warnings.append(f"Row with empty clinic name (name={name!r})")
        return None
    if clinic.lower() == "query":
        warnings.append("Row with junk clinic name 'query'")
        return None
    if not city or not state:
        warnings.append(f"Row with unparseable address (name={name!r}, clinic={clinic!r})")
        return None
    return clinic, city, state


def main():
    parser = argparse.ArgumentParser(description="Recover clean leads.csv from mangled export")
    parser.add_argument("--dry-run", action="store_true", help="Write cleaned_leads.csv instead of overwriting")
    args = parser.parse_args()

    if not os.path.exists(SRC_FILE):
        print(f"ERROR: {SRC_FILE} not found")
        raise SystemExit(1)

    with open(SRC_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Read {len(rows)} rows from {SRC_FILE}")

    warnings = []
    recovered = []
    for r in rows:
        parsed = parse_row(r, warnings)
        if parsed:
            recovered.append(parsed)

    # Dedupe by clinic_name|city|state
    unique = {}
    for clinic, city, state in recovered:
        unique.setdefault(f"{clinic.lower()}|{city.lower()}|{state}", (clinic, city, state))

    clean_rows = sorted(
        [{"clinic_name": c, "city": city, "state": st} for c, city, st in unique.values()],
        key=lambda r: (r["state"], r["city"], r["clinic_name"]),
    )

    print(f"Recovered: {len(recovered)} rows -> {len(unique)} unique clinic/city/state combos")
    by_state = Counter(r["state"] for r in clean_rows)
    print("Composition: " + ", ".join(f"{s}={n}" for s, n in sorted(by_state.items())))
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings[:20]:
            print(f"  ! {w}")

    out = OUT_FILE if not args.dry_run else DRY_RUN_OUT
    if not args.dry_run and os.path.exists(OUT_FILE):
        shutil.copy2(OUT_FILE, BACKUP)
        print(f"\nBacked up original to {BACKUP}")

    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["clinic_name", "city", "state"])
        writer.writeheader()
        writer.writerows(clean_rows)

    print(f"Wrote {len(clean_rows)} rows to {out}")
    print("\nPreview:")
    for r in clean_rows[:5]:
        print(f"  {r['clinic_name']} | {r['city']} | {r['state']}")


if __name__ == "__main__":
    main()