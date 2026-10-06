#!/usr/bin/env python3
"""
Build a correct leads.csv from your two CLEAN source exports.

Sources (edit paths/tier below):
  1. Charlotte:  `Charlotte_filtered.xlsx`  (134 rows, clean, from AI receptionist/leads)
  2. NYC:        `nyc_dental_call_priority.csv`  Tier-A = 36 rows (33 NY + 3 NJ)

Produces leads.csv with columns: clinic_name, city, state.
Never touches the source files. Dry-runs by default.

Usage:
    python build_leads.py                 # dry run -> prints 15 samples + writes nothing
    python build_leads.py --dry-run       # same
    python build_leads.py --write         # writes leads.csv (backs up existing first)
"""

import argparse
import csv
import os
import re
import shutil

CHARLOTTE_XLSX = r"G:\CODE\AI receptionist\leads\Charlotte_filtered.xlsx"
NYC_CSV = r"G:\CODE\AI receptionist\leads\nyc_dental_call_priority.csv"
# Which tier(s) to take from the NYC file. "A - Call First" = 36 rows.
NYC_TIERS = ["A - Call First"]

OUT_FILE = "leads.csv"
BACKUP = "leads.csv.bak"

ADDR = re.compile(r",\s*([A-Za-z .\-]+?)\s*,\s*([A-Z]{2})\s+\d{5}(?:\d{4})?")


def addr_fields(s):
    m = ADDR.search(s or "")
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", "")


def build_rows():
    rows = []

    # --- Charlotte from xlsx ---
    if os.path.exists(CHARLOTTE_XLSX):
        try:
            import pandas as pd
            df = pd.read_excel(CHARLOTTE_XLSX)
        except Exception as e:
            print(f"  ! Could not read {CHARLOTTE_XLSX}: {e}")
            df = None
        if df is not None:
            for _, row in df.iterrows():
                name = str(row.get("name") or "").strip()
                addr = str(row.get("address") or "")
                city, state = addr_fields(addr)
                if name and city and state:
                    rows.append({
                        "clinic_name": name, "city": city, "state": state,
                        "_addr": addr.lower().strip(),
                    })
            from collections import Counter
            print(f"  Loaded Charlotte rows: {sum(1 for r in rows)}")
            print(f"    states: {dict(Counter(r['state'] for r in rows))}")
    else:
        print(f"  ! Charlotte source missing: {CHARLOTTE_XLSX}")

    char_rows = len(rows)

    # --- NYC from csv (Tier-A) ---
    if os.path.exists(NYC_CSV):
        with open(NYC_CSV, newline="", encoding="utf-8") as f:
            src = list(csv.DictReader(f))
        sel = [r for r in src if r.get("priority_tier") in NYC_TIERS]
        for r in sel:
            name = str(r.get("name") or "").strip()
            addr = str(r.get("address") or "")
            city, state = addr_fields(addr)
            if name and city and state:
                rows.append({
                    "clinic_name": name, "city": city, "state": state,
                    "_addr": addr.lower().strip(),
                })
        from collections import Counter
        nyc = [r for r in rows[char_rows:]]
        print(f"  Loaded NYC rows: {len(nyc)} (tiers {NYC_TIERS})")
        print(f"    states: {dict(Counter(r['state'] for r in nyc))}")
    else:
        print(f"  ! NYC source missing: {NYC_CSV}")

    # Dedupe by full identity: name+city+state+ADDRESS.
    # Same-name different-address branches are KEPT (distinct leads);
    # only an exact double-pasted row (identical address) is dropped.
    seen = set()
    unique = []
    for r in rows:
        key = (r["clinic_name"].lower(), r["city"].lower(), r["state"], r["_addr"])
        if key in seen:
            print(f"  ! Dropping exact duplicate: {r['clinic_name']} | {r['city']} | {r['state']}")
            continue
        seen.add(key)
        unique.append({"clinic_name": r["clinic_name"], "city": r["city"], "state": r["state"]})

    return unique


def main():
    parser = argparse.ArgumentParser(description="Build leads.csv from clean source exports")
    parser.add_argument("--write", action="store_true", help="Write the output file (default is dry-run)")
    parser.add_argument("--samples", type=int, default=15, help="Number of sample rows to print")
    parser.add_argument("--out", type=str, default="leads.csv",
                        help="Output filename (default: leads.csv). Use a review name to avoid overwriting.")
    args = parser.parse_args()

    print("=" * 70)
    print("BUILD LEADS.CSV FROM CLEAN SOURCES")
    print("=" * 70)

    rows = build_rows()

    print(f"\nTotal unique clinic/city/state rows: {len(rows)}")

    from collections import Counter
    by_state = Counter(r["state"] for r in rows)
    print("Composition: " + ", ".join(f"{s}={n}" for s, n in sorted(by_state.items())))

    # Print sample rows — spread across NY and NC
    print(f"\n--- {args.samples} SAMPLE ROWS (mixed) for manual verification ---")
    ny = [r for r in rows if r["state"] in ("NY", "NJ")]
    nc = [r for r in rows if r["state"] in ("NC", "SC")]
    samples = []
    half = max(1, args.samples // 2)
    samples += nc[:half]
    samples += ny[: args.samples - half]
    for r in samples:
        print(f"  {r['clinic_name']} | {r['city']} | {r['state']}")

    print("-" * 70)
    out_file = args.out
    if not args.write:
        print("DRY RUN — nothing written.")
        print("Review the samples above. If alignment looks correct, run:")
        print(f"  python build_leads.py --write --out {out_file}")
        return

    if os.path.exists(out_file):
        shutil.copy2(out_file, BACKUP)
        print(f"  Backed up existing {out_file} -> {BACKUP}")

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["clinic_name", "city", "state"])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["state"], r["city"], r["clinic_name"])))
    print(f"  Wrote {len(rows)} rows to {out_file}")


if __name__ == "__main__":
    main()