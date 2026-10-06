#!/usr/bin/env python3
"""
Pre-flight check for LinkedIn Connection Tool.
Run before step1/step2 to catch issues early.

Usage:
    python preflight_check.py              # Check default leads.csv
    python preflight_check.py --file test_leads.csv   # Check specific file

Exit code 0 = all clear, non-zero = something needs fixing.
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

# ============================================================================
# CONFIG — mirror linkedin_connector.py defaults
# ============================================================================

LEADS_FILE = "leads.csv"
PROFILES_FILE = "leads_with_profiles.csv"
SENT_LOG_FILE = "sent_log.csv"
USER_DATA_DIR = ".linkedin_user_data"
EXPECTED_ROW_COUNT = 170
REQUIRED_COLUMNS = {"clinic_name", "city", "state"}
DAILY_CAP = 20
CONNECTOR_SCRIPT = "linkedin_connector.py"

# Lock file to detect running Chrome/Playwright instances
LOCK_FILE = os.path.join(USER_DATA_DIR, ".process_lock")


# ============================================================================
# CHECKS
# ============================================================================

class CheckResult:
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"

    def __init__(self, name, status, message):
        self.name = name
        self.status = status
        self.message = message


def check_leads_csv(file_path):
    """Validate leads.csv: row count, required columns, sanity check."""
    if not os.path.exists(file_path):
        return CheckResult(
            "Leads CSV",
            CheckResult.FAIL,
            f"File not found: {file_path}\n"
            f"  Create it with columns: clinic_name, city, state"
        )

    try:
        with open(file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        return CheckResult("Leads CSV", CheckResult.FAIL, f"Cannot read: {e}")

    if not rows:
        return CheckResult("Leads CSV", CheckResult.FAIL, "File is empty (0 rows)")

    columns = set(rows[0].keys())
    missing = REQUIRED_COLUMNS - columns
    if missing:
        return CheckResult(
            "Leads CSV",
            CheckResult.FAIL,
            f"Missing required columns: {', '.join(sorted(missing))}\n"
            f"  Found: {', '.join(sorted(columns))}\n"
            f"  Required: {', '.join(sorted(REQUIRED_COLUMNS))}"
        )

    extra = columns - REQUIRED_COLUMNS
    parts = [f"File: {file_path}", f"Rows: {len(rows)}", f"Columns: {', '.join(sorted(columns))}"]
    if extra:
        parts.append(f"Extra columns (ignored): {', '.join(sorted(extra))}")

    status = CheckResult.PASS
    if len(rows) < 5:
        status = CheckResult.WARN
        parts.append(f"Only {len(rows)} rows — is this a test file? (expected ~{EXPECTED_ROW_COUNT} for full list)")
    elif len(rows) > EXPECTED_ROW_COUNT * 1.5:
        status = CheckResult.WARN
        parts.append(f"{len(rows)} rows — more than expected (~{EXPECTED_ROW_COUNT}). Double-check source.")

    return CheckResult("Leads CSV", status, "\n  ".join(parts))


def check_browser_profile_dir():
    """Check .linkedin_user_data/ exists and isn't locked by another process."""
    if not os.path.exists(USER_DATA_DIR):
        return CheckResult(
            "Browser Profile",
            CheckResult.PASS,
            f"{USER_DATA_DIR}/ not found — will be created on first run (you'll log in then)"
        )

    # Try to detect running Chrome/Playwright by checking for lock file
    # and by looking for chrome.exe processes
    warnings = []

    # Method 1: Check for our lock file
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                pid = f.read().strip()
            warnings.append(f"Lock file found (PID {pid}) — another instance may be running")
        except Exception:
            warnings.append("Lock file exists but unreadable — possible stale lock")

    # Method 2: Check for chrome.exe / chromium processes using this profile dir
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        chrome_lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        if chrome_lines:
            # Check if any chrome process has our user-data-dir in its command line
            # We can't easily check command line from tasklist, so just warn
            proc_result = subprocess.run(
                ["wmic", "process", "where", "name='chrome.exe'", "get", "CommandLine", "/FORMAT:LIST"],
                capture_output=True, text=True, timeout=5
            )
            if USER_DATA_DIR.replace("./", "").replace(".\\", "") in proc_result.stdout:
                warnings.append(
                    f"chrome.exe appears to be using this profile dir\n"
                    f"  Close all Chrome windows using this profile before running step1/step2"
                )
    except Exception:
        pass  # tasklist/wmic not available, skip

    # Method 3: Check profile dir isn't suspiciously small (could be corrupted)
    try:
        contents = list(Path(USER_DATA_DIR).iterdir())
        if len(contents) == 0:
            warnings.append(f"{USER_DATA_DIR}/ is empty — may be corrupted, delete and re-login")
    except Exception:
        pass

    if warnings:
        return CheckResult(
            "Browser Profile",
            CheckResult.WARN,
            f"{USER_DATA_DIR}/ exists\n  " + "\n  ".join(warnings)
        )

    return CheckResult(
        "Browser Profile",
        CheckResult.PASS,
        f"{USER_DATA_DIR}/ exists and appears available"
    )


def check_playwright():
    """Verify Playwright is importable and Chromium browser is installed."""
    try:
        from importlib.metadata import PackageNotFoundError, version as pkg_version
        pw_version = pkg_version("playwright")
    except PackageNotFoundError:
        return CheckResult(
            "Playwright",
            CheckResult.FAIL,
            "playwright not installed. Run: pip install playwright"
        )

    # Check if Chromium browser is installed by looking for the install manifest
    try:
        from playwright._impl._driver import compute_driver_executable
        # Check common install locations
        home = Path.home()
        ms_playwright = home / "AppData" / "Local" / "ms-playwright"
        if not ms_playwright.exists():
            # Check alternative locations
            for alt in [
                home / ".cache" / "ms-playwright",
                Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
            ]:
                if alt.exists():
                    ms_playwright = alt
                    break

        if ms_playwright.exists():
            chromium_dirs = [d for d in ms_playwright.iterdir() if "chromium" in d.name.lower()]
            if chromium_dirs:
                latest = max(chromium_dirs, key=lambda d: d.stat().st_mtime)
                return CheckResult(
                    "Playwright",
                    CheckResult.PASS,
                    f"v{pw_version} installed, Chromium found at {latest.name}"
                )
            else:
                return CheckResult(
                    "Playwright",
                    CheckResult.FAIL,
                    f"v{pw_version} installed but Chromium not found in {ms_playwright}\n"
                    f"  Run: playwright install chromium"
                )
        else:
            return CheckResult(
                "Playwright",
                CheckResult.WARN,
                f"v{pw_version} installed, but cannot locate browser install directory\n"
                f"  If you've run 'playwright install chromium' before, this should be fine"
            )
    except Exception as e:
        return CheckResult(
            "Playwright",
            CheckResult.WARN,
            f"v{pw_version} installed, could not verify browser: {e}"
        )


def check_internet():
    """Quick connectivity check to linkedin.com."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://www.linkedin.com",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        code = resp.getcode()
        return CheckResult(
            "Internet",
            CheckResult.PASS,
            f"linkedin.com reachable (HTTP {code})"
        )
    except Exception as e:
        return CheckResult(
            "Internet",
            CheckResult.FAIL,
            f"Cannot reach linkedin.com — {type(e).__name__}: {e}\n"
            f"  Check your internet connection and DNS"
        )


def check_sent_log():
    """Check sent_log.csv and report daily progress."""
    if not os.path.exists(SENT_LOG_FILE):
        return CheckResult(
            "Sent Log",
            CheckResult.PASS,
            f"{SENT_LOG_FILE} does not exist yet — will be created on first send"
        )

    try:
        with open(SENT_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except Exception as e:
        return CheckResult("Sent Log", CheckResult.FAIL, f"Cannot read {SENT_LOG_FILE}: {e}")

    today = datetime.now().strftime("%Y-%m-%d")
    today_sent = sum(1 for r in rows if r.get("date") == today and r.get("status") == "sent")
    today_skipped = sum(1 for r in rows if r.get("date") == today and r.get("status") == "skipped")
    today_errors = sum(1 for r in rows if r.get("date") == today and r.get("status") == "error")
    total_sent = sum(1 for r in rows if r.get("status") == "sent")

    remaining = DAILY_CAP - today_sent
    status = CheckResult.PASS
    msg = (
        f"Total entries: {len(rows)} | Lifetime sent: {total_sent}\n"
        f"  Today ({today}): {today_sent} sent, {today_skipped} skipped, {today_errors} errors\n"
        f"  Daily cap: {today_sent}/{DAILY_CAP} used, {max(remaining, 0)} remaining"
    )

    if today_sent >= DAILY_CAP:
        status = CheckResult.WARN
        msg += "\n  !!! Daily cap already reached — step2 will stop immediately"

    return CheckResult("Sent Log", status, msg)


def check_connector_script():
    """Verify linkedin_connector.py exists and is importable."""
    if not os.path.exists(CONNECTOR_SCRIPT):
        return CheckResult(
            "Connector Script",
            CheckResult.FAIL,
            f"{CONNECTOR_SCRIPT} not found in current directory"
        )

    try:
        import py_compile
        py_compile.compile(CONNECTOR_SCRIPT, doraise=True)
        return CheckResult(
            "Connector Script",
            CheckResult.PASS,
            f"{CONNECTOR_SCRIPT} — syntax OK"
        )
    except py_compile.PyCompileError as e:
        return CheckResult(
            "Connector Script",
            CheckResult.FAIL,
            f"{CONNECTOR_SCRIPT} — syntax error: {e}"
        )


# ============================================================================
# MAIN
# ============================================================================

def print_header():
    print("=" * 70)
    print("LINKEDIN TOOL — PRE-FLIGHT CHECK")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print()


def print_result(result):
    icons = {CheckResult.PASS: "[OK]", CheckResult.FAIL: "[!!]", CheckResult.WARN: "[~~]"}
    icon = icons[result.status]
    print(f"  {icon} {result.name}")
    for line in result.message.split("\n"):
        print(f"      {line}")
    print()


def print_manual_checklist():
    print("=" * 70)
    print("  MANUAL CHECKLIST (can't be auto-checked)")
    print("=" * 70)
    print()
    print("  [ ] Have 2FA device ready if this is your first login")
    print("  [ ] Close ALL Chrome windows using the LinkedIn profile")
    print("      (other Chrome windows using same user-data-dir will conflict)")
    print("  [ ] Set aside ~15-20min uninterrupted for the test run")
    print("  [ ] Know your LinkedIn password handy (first login only)")
    print("  [ ] If running full list: plan batches of 20-30, use 'q' to quit between")
    print()
    print("=" * 70)


def main():
    global LEADS_FILE

    parser = argparse.ArgumentParser(description="Pre-flight check for LinkedIn Connection Tool")
    parser.add_argument("--file", type=str, help="Input CSV to validate (default: leads.csv)")
    args = parser.parse_args()

    if args.file:
        LEADS_FILE = args.file

    print_header()

    checks = [
        check_leads_csv(LEADS_FILE),
        check_connector_script(),
        check_playwright(),
        check_internet(),
        check_browser_profile_dir(),
        check_sent_log(),
    ]

    for result in checks:
        print_result(result)

    # Summary
    fails = [c for c in checks if c.status == CheckResult.FAIL]
    warns = [c for c in checks if c.status == CheckResult.WARN]

    print("-" * 70)
    if fails:
        print(f"  RESULT: {len(fails)} FAIL — fix these before running step1/step2")
        for f in fails:
            print(f"    - {f.name}")
        print()
        print_manual_checklist()
        sys.exit(1)
    elif warns:
        print(f"  RESULT: All checks passed with {len(warns)} warning(s) — review above")
        print()
        print_manual_checklist()
        sys.exit(0)
    else:
        print("  RESULT: ALL CLEAR — ready to run step1/step2")
        print()
        print_manual_checklist()
        sys.exit(0)


if __name__ == "__main__":
    main()
