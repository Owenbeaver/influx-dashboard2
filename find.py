#!/usr/bin/env python3
"""
Influx Lead Engine — Find

One command to find qualified Instagram accounts across multiple niches.

Usage:
  python find.py --niches "amazon fba, real estate wholesaling, SMMA" --target 50
  python find.py --niches "amazon fba" --target 20
  python find.py --list-niches

Output:
  found_leads.csv  — clean list of qualified handles ready for your VA
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Import our two modules ────────────────────────────────────────────────────
from scraper import discover, _NICHE_TERMS
from filter  import _pass1_ok, _scrape_batch, _pass2_ok, _claude_score

BASE_DIR   = r"C:\Users\Owenb\Desktop\instagram-tool"
OUTPUT_CSV = os.path.join(BASE_DIR, "found_leads.csv")

FIELDNAMES = ["handle", "display_name", "followers", "bio", "website",
              "niche", "claude_niche", "claude_signals", "claude_confidence", "claude_reason"]


def _print_header(niches, target):
    print(f"\n{'='*64}")
    print(f"  INFLUX FIND")
    print(f"  Target:  {target} qualified accounts")
    print(f"  Niches:  {', '.join(niches)}")
    print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*64}\n")


def find(niches: list[str], target: int, output_path: str) -> None:
    _print_header(niches, target)

    all_qualified : list[dict] = []
    seen_handles  : set        = set()

    for niche in niches:
        if len(all_qualified) >= target:
            break

        still_need = target - len(all_qualified)
        print(f"\n[{niche.upper()}]  Need {still_need} more accounts\n")

        # -- Step 1: Discover raw handles for this niche ----------------------
        raw = discover(niche, target=still_need * 6, max_passes=3)
        # We ask for 6x the target because most won't pass the filter

        # -- Step 2: Deduplicate against already-found handles ----------------
        fresh = [r for r in raw if r["handle"] not in seen_handles]
        for r in fresh:
            seen_handles.add(r["handle"])

        print(f"\n  {len(fresh)} fresh handles to filter for {niche!r}")

        # -- Step 3: Pass 1 — hard skip (instant, no API) ---------------------
        p1 = [r for r in fresh if _pass1_ok(r["handle"])]
        print(f"  Pass 1: {len(p1)} passed hard-skip filter")

        if not p1:
            print("  No handles passed — moving to next niche.")
            continue

        # -- Step 4: Pass 2 — Apify scrape + metric gates ---------------------
        BATCH = 20
        profiles: dict = {}
        handles_list = [r["handle"] for r in p1]

        for i in range(0, len(handles_list), BATCH):
            batch = handles_list[i:i+BATCH]
            print(f"  [Apify] Batch {i//BATCH+1}: scraping {len(batch)} profiles...")
            profiles.update(_scrape_batch(batch))
            time.sleep(2)

        p2 = []
        for r in p1:
            profile = profiles.get(r["handle"])
            if not profile:
                continue
            ok, reason = _pass2_ok(profile)
            if ok:
                p2.append({**r, **profile})

        print(f"  Pass 2: {len(p2)} passed follower/bio/link check")

        if not p2:
            print("  No handles passed — moving to next niche.")
            continue

        # -- Step 5: Pass 3 — Claude ICP scoring ------------------------------
        print(f"  Pass 3: Claude scoring {len(p2)} profiles...")
        for i, r in enumerate(p2, 1):
            # Stop early if we've hit the target
            if len(all_qualified) >= target:
                break

            print(f"    [{i}/{len(p2)}] @{r['handle']} ({r.get('followers',0):,} followers)")
            score = _claude_score(r)

            approved = score.get("approved", False)
            status   = "✓" if approved else "✗"
            print(f"      {status} {score.get('confidence','?')} — {score.get('reason','')[:80]}")

            if approved:
                r["claude_niche"]      = score.get("niche", "")
                r["claude_signals"]    = ", ".join(score.get("coaching_signals", []))
                r["claude_confidence"] = score.get("confidence", "")
                r["claude_reason"]     = score.get("reason", "")
                all_qualified.append(r)
                print(f"      ✅ QUALIFIED  ({len(all_qualified)}/{target})")

            time.sleep(0.3)

        print(f"\n  [{niche}] Done.  Qualified so far: {len(all_qualified)}/{target}")

    # -- Save output -----------------------------------------------------------
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_qualified[:target])

    print(f"\n{'='*64}")
    print(f"  DONE")
    print(f"  Found:  {min(len(all_qualified), target)} qualified accounts")
    print(f"  Saved:  {output_path}")
    print(f"  Time:   {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*64}\n")
    print("Hand found_leads.csv to your VA — they're ready to go.")


def main():
    parser = argparse.ArgumentParser(description="Find qualified Instagram accounts by niche")
    parser.add_argument("--niches",      type=str, default="",  help='Comma-separated niches e.g. "amazon fba, SMMA"')
    parser.add_argument("--target",      type=int, default=50,  help="How many qualified accounts you want")
    parser.add_argument("--output",      type=str, default=OUTPUT_CSV, help="Output CSV path")
    parser.add_argument("--list-niches", action="store_true",   help="Show all available niches and exit")
    args = parser.parse_args()

    if args.list_niches:
        print("\nAvailable niches:")
        for n in _NICHE_TERMS:
            print(f"  • {n}")
        print("\nYou can also type any custom niche not on this list.")
        return

    niches_raw = args.niches.strip()
    if not niches_raw:
        print("Available niches:")
        for n in _NICHE_TERMS:
            print(f"  • {n}")
        print()
        niches_raw = input("Enter niches (comma-separated): ").strip()
        if not niches_raw:
            print("No niches entered — exiting.")
            return

    niches = [n.strip() for n in niches_raw.split(",") if n.strip()]
    find(niches, target=args.target, output_path=args.output)


if __name__ == "__main__":
    main()
