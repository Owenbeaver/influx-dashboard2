#!/usr/bin/env python3
"""
Test script: scrape @douggrindstaff via Apify and inspect what fields come back,
specifically checking whether similar / related account data is returned.
"""

import json
import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
if not APIFY_TOKEN:
    sys.exit("ERROR: APIFY_TOKEN not set in .env")


# ── Apify runner (same pattern as pipeline.py) ─────────────────────────────────

def apify_run_and_wait(actor_id: str, input_data: dict, max_wait: int = 180) -> list:
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    resp = requests.post(
        run_url,
        params={"token": APIFY_TOKEN},
        json=input_data,
        timeout=30,
    )
    resp.raise_for_status()
    run_id = resp.json()["data"]["id"]
    print(f"  -> Run started: {run_id}")

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    status = "RUNNING"
    for _ in range(max_wait // 5):
        time.sleep(5)
        st = requests.get(status_url, params={"token": APIFY_TOKEN}, timeout=30)
        st.raise_for_status()
        status = st.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        print(f"  ... {status}")

    if status != "SUCCEEDED":
        print(f"  X Run ended with status: {status}")
        return []

    items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items"
    items = requests.get(items_url, params={"token": APIFY_TOKEN}, timeout=30)
    items.raise_for_status()
    return items.json()


# ── Scrape @douggrindstaff ─────────────────────────────────────────────────────

TARGET = "douggrindstaff"
print(f"\n{'='*60}")
print(f"  Scraping @{TARGET} via apify~instagram-scraper")
print(f"{'='*60}\n")

results = apify_run_and_wait(
    "apify~instagram-scraper",
    {
        "directUrls": [f"https://www.instagram.com/{TARGET}/"],
        "resultsType": "details",
        "resultsLimit": 1,
    },
)

if not results:
    sys.exit("No results returned from Apify.")

profile = results[0]


# ── Core profile fields ────────────────────────────────────────────────────────

print("\n── CORE PROFILE ──────────────────────────────────────────────")
for key in ("username", "fullName", "biography", "followersCount",
            "followsCount", "postsCount", "externalUrl", "externalUrls",
            "isVerified", "isBusinessAccount", "businessCategoryName",
            "profilePicUrl"):
    val = profile.get(key)
    if val is not None:
        print(f"  {key}: {val}")


# ── All top-level keys (so we know exactly what Apify returns) ─────────────────

print("\n── ALL KEYS RETURNED ─────────────────────────────────────────")
for k, v in sorted(profile.items()):
    if isinstance(v, list):
        print(f"  {k}: [list, {len(v)} items]")
    elif isinstance(v, dict):
        print(f"  {k}: {{dict, keys: {list(v.keys())[:6]}}}")
    else:
        snippet = str(v)[:120]
        print(f"  {k}: {snippet}")


# ── Similar / related account fields ──────────────────────────────────────────

SIMILAR_KEYS = (
    "relatedProfiles", "similarAccounts", "suggestedUsers",
    "edge_related_profiles", "related_profiles",
    "coauthorProducers", "pinnedChannels",
)

print("\n── SIMILAR / RELATED ACCOUNT DATA ───────────────────────────")
found_any = False
for key in SIMILAR_KEYS:
    val = profile.get(key)
    if val:
        found_any = True
        print(f"\n  [{key}]  ({len(val) if isinstance(val, list) else type(val).__name__})")
        items_to_show = val[:5] if isinstance(val, list) else [val]
        for item in items_to_show:
            if isinstance(item, dict):
                print(f"    username : {item.get('username', item.get('node', {}).get('username', '?'))}")
                print(f"    full_name: {item.get('full_name', item.get('fullName', ''))}")
                print(f"    followers: {item.get('followersCount', item.get('edge_followed_by', {}).get('count', '?'))}")
                print()
            else:
                print(f"    {item}")

if not found_any:
    print("  No similar/related account fields found in this response.")
    print("  The Instagram scraper does not expose 'suggested users' by default.")
    print("  Alternative sourcing approach: hashtag/keyword search actor.")


# ── Raw JSON dump (truncated) ──────────────────────────────────────────────────

print("\n── RAW JSON (first 3000 chars) ───────────────────────────────")
print(json.dumps(profile, indent=2, ensure_ascii=False)[:3000])
print("\n[...truncated — full response saved to test_sourcing_raw.json]\n")

with open(Path(__file__).parent / "test_sourcing_raw.json", "w", encoding="utf-8") as f:
    json.dump(profile, f, indent=2, ensure_ascii=False)

print("Done. Full raw response -> test_sourcing_raw.json")
