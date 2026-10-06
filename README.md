# LinkedIn Connection Tool

Semi-automated tool to find LinkedIn profiles for dental clinic owners/office managers, then send personalized connection requests. You click Send manually — the script fills the note for you.

## How It Works

```
leads.csv  →  Step 1 (profile finder)  →  leads_with_profiles.csv
                                               ↓ (you pick 1/2/3/s/q per clinic)
                                         leads_with_profiles.csv (confirmed=yes)
                                               ↓
                                          Step 2 (connect tool)  →  sent_log.csv
```

## Setup

### 1. Install Python (3.8+)

Download from [python.org](https://www.python.org/downloads/) — check "Add Python to PATH" during install.

```bash
python --version   # should show 3.8+
```

### 2. Install Playwright

```bash
cd linkedin-tool
pip install playwright
playwright install chromium
```

### 3. Prepare `leads.csv`

Your CSV needs at minimum these columns (extra columns are fine, they're ignored):

```csv
clinic_name,city,state
Bright Smile Dental,New York,NY
Charlotte Dental Works,Charlotte,NC
```

## First-Time Login

The first time you run either step, a Chromium browser window opens. Log into LinkedIn manually in that window. Your session is saved to `.linkedin_user_data/` — you only need to log in once.

**Don't delete `.linkedin_user_data/`** or you'll need to log in again.

## Step 1: Find Profiles

```bash
python linkedin_connector.py --step 1
```

For each clinic, the script searches LinkedIn with three fallback queries:
1. `"{clinic_name} {city} {state} owner"`
2. `"{clinic_name} {city} {state} office manager"`
3. `"{clinic_name} {city} {state} dentist"`

It shows you the top 3 results in the terminal:

```
  Top results for: Bright Smile Dental
  ------------------------------------------------------------
  1. Dr. Jane Smith
     Owner at Bright Smile Dental — Dentist
     https://www.linkedin.com/in/janesmith/
  2. Mark Johnson
     Office Manager at Bright Smile Dental
     https://www.linkedin.com/in/markjohnson/
  3. Sarah Williams
     Dentist at Bright Smile Dental
     https://www.linkedin.com/in/sarahwilliams/
  s. Skip this clinic
  q. Quit Step 1
  ------------------------------------------------------------
  Pick (1/2/3/s/q):
```

You pick a number, `s` to skip, or `q` to quit (progress is saved). Only picked profiles are saved to `leads_with_profiles.csv` with `confirmed=yes`.

Already-processed clinics are skipped on re-runs.

## Step 2: Send Connections

```bash
python linkedin_connector.py --step 2
```

For each confirmed profile:
1. Navigates to the profile
2. Clicks Connect → Add a note
3. Fills the personalized note
4. **Stops and waits** — you click Send in the browser
5. Press ENTER in terminal (or type `skip`)
6. Logs to `sent_log.csv`

### Safety features
- **Daily cap**: stops after 20 sends per day (configurable)
- **Random delays**: 5-15 sec between profile loads (not between your clicks)
- **Dupe prevention**: skips profiles already in `sent_log.csv`
- **Error handling**: timeouts and missing buttons logged, never crashes

## Interactive Mode

```bash
python linkedin_connector.py
```

Shows a menu to pick Step 1, Step 2, or check file status.

## Pre-Flight Check

Run before Step 1/Step 2 to catch issues early:

```bash
python preflight_check.py                # check default leads.csv
python preflight_check.py --file test_leads.csv
```

Checks:
- **Leads CSV** — row count, required columns (`clinic_name`, `city`, `state`), sanity warning if count looks off
- **Connector script** — syntax valid
- **Playwright** — installed, Chromium present
- **Internet** — linkedin.com reachable
- **Browser profile** — `.linkedin_user_data/` exists, not locked by another Chrome process
- **Sent log** — readable, today's daily-cap usage

Exits non-zero if any auto-checkable item fails. Also prints a manual checklist (2FA, close Chrome, set aside time).

## Build Leads CSV From Clean Sources (RECOMMENDED)

Your target list is ~170 clinics: **Charlotte NC (134)** + **NYC Tier-A (36)**. The `leads.csv` you may have is a corrupted merge of multiple exports (582 contaminated rows including NJ/SC/suppliers — not ground truth).

The clean sources live elsewhere (edit the paths/tier in `build_leads.py`):
- Charlotte: `AI receptionist\leads\Charlotte_filtered.xlsx` (134 rows)
- NYC: `AI receptionist\leads\nyc_dental_call_priority.csv` Tier-A (36 rows)

Build a correct, reviewable file (does NOT touch your existing `leads.csv`):

```bash
python build_leads.py                          # DRY RUN: prints 15 samples, writes nothing
python build_leads.py --write --out leads.csv  # write to leads.csv (backs up existing first)
python build_leads.py --write --out leads_review_170.csv   # write to a review file
```

Preserves same-name multi-branch clinics (each branch = distinct lead), dedupes only exact double-pasted rows. Produces exactly 170 rows (NC=133, SC=1, NY=33, NJ=3).

## Fix Mangled Leads CSV (fallback)

If you only have the corrupted `leads.csv` and no clean source exports, you can attempt recovery (will not recover the contamination):

```bash
python fix_leads_csv.py --dry-run   # writes cleaned_leads.csv, doesn't touch leads.csv
python fix_leads_csv.py --write     # backs up leads.csv -> leads.csv.bak, overwrites leads.csv
```

Prefer `build_leads.py` over this — recovery guesses from mangled rows, while `build_leads.py` uses your known-good source exports.

## Configuration

Edit these at the top of `linkedin_connector.py`:

```python
CONNECTION_NOTE_TEMPLATE = (
    "Hi {first_name}, came across {clinic_name} — would love to connect and share "
    "something that might help with patient calls/bookings."
)
DAILY_CAP = 20
DELAY_MIN = 5
DELAY_MAX = 15
```

## Sourcing Pipeline: Find REAL Owners (recommended flow)

Clinic-name keyword search has a low hit rate (small practices often have weak
LinkedIn presence for the owner specifically). Use this pipeline instead:

```
dental_leads_with_websites.csv      <-  clinic_name,city,state,website (170 rows)
        │
        ▼
scrape_and_extract.py               <-  scrape each clinic's website + Groq LLM extracts owner name
        │
        ▼
leads_with_names.csv                <-  clinic_name,website,candidate_name,candidate_title,confidence
        │
        ▼
linkedin_finder.py owner            <-  search LinkedIn BY OWNER NAME (high hit rate) -> you pick
        │
        ▼
leads_with_profiles.csv (confirmed=yes)
        │
        ▼
linkedin_connector.py --step 2      <-  send connections (you click Send)
```

### Step 0: website scrape + AI name extraction

```bash
python scrape_and_extract.py                                 # all 170
python scrape_and_extract.py --limit 5                       # test on 5 first
python scrape_and_extract.py --no-groq --limit 3             # scraping only, no LLM calls
```

Requires a free Groq API key (for the LLM name extraction).

1. Sign up free at https://console.groq.com/keys and create a key.
2. Set the env var in **Windows PowerShell** (persists for new terminals):

   ```powershell
   setx GROQ_API_KEY "your_key_here"
   ```

   Then open a NEW terminal window. Or set it for just the current window:

   ```powershell
   $env:GROQ_API_KEY = "your_key_here"
   ```

3. Install dependencies (already present in `requirements.txt`):

   ```bash
   pip install requests beautifulsoup4
   ```

Output `leads_with_names.csv` columns:
`clinic_name, city, state, website, candidate_name, candidate_title, confidence, scrape_status`

`scrape_status`: `ok` / `scrape_failed` / `extraction_failed` / `no_person_found`.
Rows with a `candidate_name` → feed into the `owner` lane below.

### Step 1: find the owner's LinkedIn profile

For clinics with a name from Step 0:

```bash
python linkedin_finder.py owner --input leads_with_names.csv   # prompts for names; use candidate_name
python linkedin_finder.py owner --file test_reals.csv          # or manual owner-name test
```

`linkedin_finder.py` has four lanes:

| Lane | Command | When to use |
|------|---------|-------------|
| `company` | `linkedin_finder.py company --file ...` | Clinic has a LinkedIn company page → People tab shows actual staff. Highest validity. (Sales Nav gives full roster.) |
| `owner` | `linkedin_finder.py owner --file ...` | You have an owner NAME (from Step 0 / Google Maps / About page). Search by name, not clinic. |
| `dork` | `linkedin_finder.py dork --file ...` | Manual Google `site:linkedin.com/in "<clinic>"` paste-URL flow. Zero ban risk. |
| `batch` | `linkedin_finder.py batch --file leads.csv` | Accept ~10-20% real matches from clinic-name auto search; skip the rest. |

All confirmed picks are appended to `leads_with_profiles.csv` (confirmed=yes) for Step 2.

### Fallback ladder (if nothing found)

1. No website / no team page → `linkedin_finder.py company` (People tab).
2. No company page → `linkedin_finder.py batch` (generic clinic-name search).
3. Still nothing → connect with *anyone plausibly related* (any dentist/staff from clinic-name search) — on-topic, lower precision.

### Safe send volume (ban avoidance)

- **Week 1-2 (warm-up):** 5-8 sends/day (LinkedIn watches new automation-style patterns closely the first 2 weeks).
- **Week 3+ (steady state):** 15-20 sends/day (matches `DAILY_CAP`.
- **Hard ceiling:** never exceed ~100/week regardless of daily math.
- Keep the note short and non-salesy (existing template) — salesy notes raise the report rate.

### Network compounding (no extra build)

Once you hold ~30-50 real dental-niche connections, LinkedIn's own
"People You May Know" starts surfacing more dental-industry people organically.
Just keep steady outreach — no custom code needed.

## Files

| File | Purpose |
|------|---------|
| `leads.csv` | Your input — clinic names + cities |
| `leads_with_profiles.csv` | Picked LinkedIn profiles (auto-generated) |
| `sent_log.csv` | Audit trail of all connection attempts |
| `.linkedin_user_data/` | Browser session (keep this!) |

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Playwright not installed" | `pip install playwright && playwright install chromium` |
| Browser doesn't open | Make sure `HEADLESS = False` at top of script |
| LinkedIn CAPTCHA | Stop, wait 24 hours, reduce `DAILY_CAP` |
| Connect button not found | Some profiles use Premium/Creator mode — logged as error, skipped |
| "Page Not Found" | Profile URL is stale — logged as error, skipped |
| Want to re-run Step 1 | Already-picked clinics are skipped automatically |
| Want to start fresh | Delete `leads_with_profiles.csv` and `sent_log.csv` |
#   l i n k e d i n - t o o l  
 