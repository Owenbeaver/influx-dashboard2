#!/usr/bin/env python3
"""
End-to-end verification test on 3 known-correct handles.
Uses cached name/niche from output.csv — no Apify re-scraping.
Shows per-candidate scores and confirms correct LinkedIn is selected.
"""
import sys, csv
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline import find_and_select_linkedin, get_apollo_contact

OUTPUT_CSV = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

TESTS = [
    {
        "ig":       "lattes.and.leases",
        "bio":      "Helping women invest in real estate | Short term rental investor | Realtor",
        "website":  "https://terrapalmsprings.com",
        "correct":  "https://www.linkedin.com/in/soli-cayetano-101ab3134",
    },
    {
        "ig":       "jimmmyhill",
        "bio":      "Founder: @yourfirstoffer turn what you know into a digital offer",
        "website":  "https://yourfirstoffer.com",
        "correct":  "https://www.linkedin.com/in/jimmyhillofficial",
    },
    {
        "ig":       "nuccirealestate",
        "bio":      "$100M+ Closed From Cold Calling",
        "website":  "https://callsintolistings.com",
        "correct":  "https://www.linkedin.com/in/anthony-nucci-3153b3376",
    },
]

def main():
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row_by_ig = {r["handle"].split("instagram.com/")[-1].rstrip("/"): r for r in rows}

    passed = 0
    for t in TESTS:
        ig      = t["ig"]
        row     = row_by_ig.get(ig, {})
        full_name = row.get("full_name", "")
        niche     = row.get("niche", "")

        print(f"\n{'='*64}")
        print(f"  TEST: @{ig}")
        print(f"  Expected: {t['correct']}")
        print(f"  full_name={full_name!r}  niche={niche!r}")
        print(f"{'='*64}")

        selected = find_and_select_linkedin(
            ig, full_name, niche, t["bio"],
            website=t["website"], ig_pic_url="",
        )

        ok = selected == t["correct"]
        print(f"\n  RESULT: {'PASS' if ok else 'FAIL'}")
        print(f"  Selected : {selected or '(none)'}")
        print(f"  Expected : {t['correct']}")

        if selected:
            contact = get_apollo_contact(selected)
            print(f"  email={contact['email'] or '-'}  phone={contact['phone'] or '-'}")

        if ok:
            passed += 1

    print(f"\n{'='*64}")
    print(f"  TOTAL: {passed}/{len(TESTS)} passed")
    print(f"{'='*64}")

if __name__ == "__main__":
    main()
