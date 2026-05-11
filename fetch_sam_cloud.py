#!/usr/bin/env python3
"""
Denamita Daily SAM.gov Fetch — Cloud (GitHub Actions) variant.

Differences from fetch_sam_daily.py (local Windows variant):
  - Reads API key from environment variable SAM_GOV_API_KEY (set as a GitHub repo secret),
    not from .api_keys.txt
  - Writes output to ./output/SAM_Pull_YYYY-MM-DD.json relative to the repo root,
    not to a Windows path
  - No filesystem assumptions about a parent Fed Gov Contracts directory

Intended runtime: GitHub Actions ubuntu-latest runner on a cron schedule.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = REPO_ROOT / "output"

TARGET_NAICS = ["561210", "484121", "541513", "561720"]

ALLOWED_SET_ASIDES = {"", "NONE", "SBA", "Total Small Business"}
EXCLUDED_SET_ASIDES = {
    "8A", "8(A) SOLE SOURCE", "COMPETITIVE 8(A)", "8(A) COMPETED",
    "HZC", "HUBZONE", "HZS",
    "WOSB", "EDWOSB", "WOSBSS", "EDWOSBSS",
    "SDVOSBC", "SDVOSB", "SDVOSBS",
    "VSA", "VSS", "BICIV", "ISBEE", "IEE",
}

NOTICE_TYPES = ["o", "k", "p", "r", "s"]
API_BASE = "https://api.sam.gov/opportunities/v2/search"
LOOKBACK_DAYS = 30
PAGE_LIMIT = 100
MAX_PAGES_PER_NAICS = 10
REQUEST_TIMEOUT_SECONDS = 60
SLEEP_BETWEEN_REQUESTS = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg, log_lines):
    stamped = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}"
    print(stamped, flush=True)
    log_lines.append(stamped)


def read_api_key():
    """Read SAM_GOV_API_KEY from environment (set via GitHub Actions secret)."""
    key = os.environ.get("SAM_GOV_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "SAM_GOV_API_KEY environment variable is not set. "
            "Configure it as a GitHub repo secret: Settings → Secrets and variables → Actions → New repository secret."
        )
    return key


def fetch_naics_page(api_key, naics, posted_from, posted_to, notice_type, offset, log_lines):
    params = {
        "api_key": api_key,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "ncode": naics,
        "ptype": notice_type,
        "limit": PAGE_LIMIT,
        "offset": offset,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    # Mask the API key when logging URLs
    masked_url = url.replace(api_key, "***REDACTED***")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data, None
    except urllib.error.HTTPError as e:
        err = f"HTTPError {e.code} for NAICS {naics} ptype {notice_type} offset {offset}: {e.reason}"
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
            err += f" | body: {body}"
        except Exception:
            pass
        log(err, log_lines)
        return None, err
    except urllib.error.URLError as e:
        err = f"URLError for NAICS {naics} ptype {notice_type}: {e.reason}"
        log(err, log_lines)
        return None, err
    except Exception as e:
        err = f"Unexpected error for NAICS {naics} ptype {notice_type}: {e!r}"
        log(err, log_lines)
        return None, err


def passes_set_aside_filter(record):
    sa = (record.get("typeOfSetAside") or "").strip().upper()
    sa_desc = (record.get("typeOfSetAsideDescription") or "").strip().upper()
    if sa in EXCLUDED_SET_ASIDES or sa_desc in EXCLUDED_SET_ASIDES:
        return False
    if any(x in sa_desc for x in ["8(A)", "HUBZONE", "WOMAN-OWNED", "WOSB", "EDWOSB",
                                   "SERVICE-DISABLED", "SDVOSB", "VETERAN", "INDIAN ECONOMIC",
                                   "BUY INDIAN"]):
        return False
    if sa in ALLOWED_SET_ASIDES or sa_desc in ALLOWED_SET_ASIDES:
        return True
    if not sa and not sa_desc:
        return True
    if "TOTAL SMALL BUSINESS" in sa_desc:
        return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log_lines = []
    started_at = datetime.utcnow()
    log(f"Denamita SAM.gov daily fetch starting (cloud variant)", log_lines)
    log(f"Repo root: {REPO_ROOT}", log_lines)
    log(f"Output directory: {OUTPUT_DIR}", log_lines)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        api_key = read_api_key()
        log(f"API key loaded from environment (length: {len(api_key)})", log_lines)
    except Exception as e:
        log(f"FATAL: Could not read API key: {e}", log_lines)
        sys.exit(1)

    today = datetime.utcnow()
    posted_to = today.strftime("%m/%d/%Y")
    posted_from = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    log(f"Date window: {posted_from} to {posted_to} ({LOOKBACK_DAYS}-day lookback)", log_lines)

    all_records = []
    naics_counts = {n: 0 for n in TARGET_NAICS}
    errors = []

    for naics in TARGET_NAICS:
        log(f"--- Fetching NAICS {naics} ---", log_lines)
        for ptype in NOTICE_TYPES:
            offset = 0
            page = 0
            while page < MAX_PAGES_PER_NAICS:
                data, err = fetch_naics_page(api_key, naics, posted_from, posted_to,
                                             ptype, offset, log_lines)
                if err:
                    errors.append(err)
                    break
                if not data:
                    break
                records = data.get("opportunitiesData", []) or []
                if not records:
                    break

                kept = [r for r in records if passes_set_aside_filter(r)]
                for r in kept:
                    r["_pulled_naics"] = naics
                    r["_pulled_ptype"] = ptype
                    r["_pulled_at"] = today.isoformat()
                all_records.extend(kept)
                naics_counts[naics] += len(kept)

                log(f"  NAICS {naics} ptype {ptype} page {page+1}: "
                    f"{len(records)} fetched, {len(kept)} kept after set-aside filter",
                    log_lines)

                if len(records) < PAGE_LIMIT:
                    break
                offset += PAGE_LIMIT
                page += 1
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    seen = set()
    deduped = []
    for r in all_records:
        nid = r.get("noticeId") or r.get("solicitationNumber") or r.get("id")
        if nid and nid in seen:
            continue
        if nid:
            seen.add(nid)
        deduped.append(r)

    date_stamp = today.strftime("%Y-%m-%d")
    json_path = OUTPUT_DIR / f"SAM_Pull_{date_stamp}.json"
    log_path = OUTPUT_DIR / f"SAM_Pull_{date_stamp}_log.txt"

    output = {
        "metadata": {
            "run_date_utc": today.isoformat() + "Z",
            "posted_from": posted_from,
            "posted_to": posted_to,
            "target_naics": TARGET_NAICS,
            "notice_types": NOTICE_TYPES,
            "naics_counts": naics_counts,
            "total_records_after_dedup": len(deduped),
            "total_records_before_dedup": len(all_records),
            "error_count": len(errors),
            "source": "github_actions_cloud_run",
        },
        "records": deduped,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)
    log(f"Wrote {len(deduped)} records to {json_path}", log_lines)

    finished_at = datetime.utcnow()
    duration = (finished_at - started_at).total_seconds()
    log(f"Run complete. Duration: {duration:.1f}s. Errors: {len(errors)}.", log_lines)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
        if errors:
            f.write("\n\n=== Errors ===\n")
            f.write("\n".join(errors))

    print(f"\nDone. {len(deduped)} qualifying records saved.", flush=True)


if __name__ == "__main__":
    main()
