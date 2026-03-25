#!/usr/bin/env python3
"""
Test the 5-signal LinkedIn system on 4 specific handles.
Uses cached name/niche from output.csv + provided website/bio.
No Instagram re-scraping needed.
"""
import sys, csv
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline import find_and_select_linkedin, get_apollo_contact

OUTPUT_CSV = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

# Overrides: (ig_username, website, extra_bio_context)
OVERRIDES = {
    "nuccirealestate": {
        "website": "https://nuccirealestate.com",
        "extra_bio": "$100M+ Closed From Cold Calling",
    },
    "jimmmyhill": {
        "website": "https://yourfirstoffer.com",
        "extra_bio": "Founder: @yourfirstoffer turn what you know into a digital offer",
    },
    "austin.hancock1": {
        "website": "",
        "extra_bio": "I help people build wealth through real estate investing",
    },
    "skylarbmoon": {
        "website": "",
        "extra_bio": "Flipping 50 Houses in 2026",
    },
}

def main():
    # Load cached name/niche from output.csv
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row_by_ig = {r["handle"].split("instagram.com/")[-1].rstrip("/"): r for r in rows}

    for ig, ov in OVERRIDES.items():
        row = row_by_ig.get(ig)
        if not row:
            print(f"\n[!] {ig} not found in output.csv — skipping")
            continue

        full_name = row["full_name"]
        niche     = row["niche"]
        bio       = ov["extra_bio"]
        website   = ov["website"]

        print(f"\n{'='*64}")
        print(f"  @{ig}  ({full_name} / {niche})")
        print(f"  website={website!r}")
        print(f"  bio={bio!r}")
        print(f"{'='*64}")

        linkedin_url = find_and_select_linkedin(
            ig, full_name, niche, bio,
            website=website, ig_pic_url="",
        )

        if linkedin_url:
            contact = get_apollo_contact(linkedin_url)
            print(f"\n  -> email={contact['email'] or '-'}  phone={contact['phone'] or '-'}")
        else:
            print(f"\n  -> No LinkedIn found")

if __name__ == "__main__":
    main()
