#!/usr/bin/env python3
"""Re-run LinkedIn + Apollo for the 10 problem handles using the new logic.
Reuses cached full_name, niche, bio from output.csv — no Apify re-scraping.
"""
import csv, sys, time
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Import the updated pipeline functions
from pipeline import find_and_select_linkedin, get_apollo_contact

OUTPUT_CSV = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

TARGETS = {
    "symonebeez":        {"bio": "DMV | Ex SWE | I help you land $100k+ Tech jobs"},
    "adamrchap":         {"bio": "I help people get rich with home services"},
    "nuccirealestate":   {"bio": "$100M+ Closed From Cold Calling"},
    "jimmmyhill":        {"bio": "Founder: @yourfirstoffer turn what you know into a digital offer"},
    "skylarbmoon":       {"bio": "Flipping 50 Houses in 2026"},
    "austin.hancock1":   {"bio": "I help people build wealth through real estate investing"},
    "joshhighofficial":  {"bio": "I teach wholesalers/flippers to go from chasing deals to predictable pipeline"},
    "realquentinflores": {"bio": "Real Estate entrepreneur, want to close your first deal"},
    "thetroykearns":     {"bio": "Helping you achieve financial freedom through Real Estate"},
    "shrutipangtey":     {"bio": "Founder @digitalplrhub @digitalempiresofficial Turn knowledge into digital income using AI"},
}

def main():
    # Load CSV
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = ["handle", "full_name", "niche", "linkedin_url", "email", "phone", "error"]
    row_by_ig = {r["handle"].split("instagram.com/")[-1].rstrip("/"): r for r in rows}

    results = {}
    for i, (ig, extra) in enumerate(TARGETS.items(), 1):
        row = row_by_ig.get(ig)
        if not row:
            print(f"[{i}/10] {ig} -- not found in CSV, skipping")
            continue

        full_name = row["full_name"]
        niche     = row["niche"]
        bio       = extra["bio"]

        print(f"\n[{i}/10] @{ig}  ({full_name} / {niche})")
        print("=" * 64)

        linkedin_url = find_and_select_linkedin(ig, full_name, niche, bio)
        if not linkedin_url:
            print("    X No LinkedIn found")
            row["linkedin_url"] = ""
            row["error"]        = "LinkedIn URL not found (rerun)"
            results[ig] = {"linkedin": "", "email": "", "phone": "", "ok": False}
            continue

        row["linkedin_url"] = linkedin_url
        contact = get_apollo_contact(linkedin_url)
        row["email"] = contact["email"]
        row["phone"] = contact["phone"]
        row["error"] = ""
        results[ig] = {"linkedin": linkedin_url, "email": contact["email"], "phone": contact["phone"], "ok": True}
        time.sleep(1)

    # Save
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'='*64}")
    print("Summary:")
    ok = sum(1 for v in results.values() if v["ok"])
    print(f"  Resolved: {ok}/{len(TARGETS)}")
    for ig, v in results.items():
        status = f"email={v['email'] or '-'}  phone={v['phone'] or '-'}" if v["ok"] else "FAILED"
        print(f"  {ig}: {status}")

if __name__ == "__main__":
    main()
