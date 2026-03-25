#!/usr/bin/env python3
"""
Fix 3 handles:
  1. nuccirealestate -- re-run with callsintolistings.com domain
  2. skylarbmoon     -- direct Apollo lookup on known-correct LinkedIn URL
  3. austin.hancock1 -- direct Apollo lookup on known-correct LinkedIn URL
"""
import sys, csv
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline import find_and_select_linkedin, get_apollo_contact, _normalize_li_url

OUTPUT_CSV = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

# Load CSV
with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
row_by_ig = {r["handle"].split("instagram.com/")[-1].rstrip("/"): r for r in rows}
fieldnames = ["handle", "full_name", "niche", "linkedin_url", "email", "phone", "error"]

# ── 1. nuccirealestate ─────────────────────────────────────────────────────────
print("\n" + "="*64)
print("  1. nuccirealestate  (callsintolistings.com)")
print("="*64)
row = row_by_ig["nuccirealestate"]
li = find_and_select_linkedin(
    "nuccirealestate",
    row["full_name"],
    row["niche"],
    "$100M+ Closed From Cold Calling",
    website="https://callsintolistings.com",
    ig_pic_url="",
)
if li:
    contact = get_apollo_contact(li)
    row["linkedin_url"] = li
    row["email"]        = contact["email"]
    row["phone"]        = contact["phone"]
    row["error"]        = ""
    print(f"\n  -> email={contact['email'] or '-'}  phone={contact['phone'] or '-'}")
else:
    print("\n  -> No LinkedIn found")

# ── 2. skylarbmoon -- direct Apollo hit ───────────────────────────────────────
print("\n" + "="*64)
print("  2. skylarbmoon  (direct: linkedin.com/in/skylar-moon-194b4a157)")
print("="*64)
li_skylar = _normalize_li_url("https://www.linkedin.com/in/skylar-moon-194b4a157")
contact_s = get_apollo_contact(li_skylar)
row_s = row_by_ig["skylarbmoon"]
row_s["linkedin_url"] = li_skylar
row_s["email"]        = contact_s["email"]
row_s["phone"]        = contact_s["phone"]
row_s["error"]        = ""
print(f"\n  -> email={contact_s['email'] or '-'}  phone={contact_s['phone'] or '-'}")

# ── 3. austin.hancock1 -- direct Apollo hit ───────────────────────────────────
print("\n" + "="*64)
print("  3. austin.hancock1  (direct: linkedin.com/in/austin-hancock-66b382129)")
print("="*64)
li_austin = _normalize_li_url("https://www.linkedin.com/in/austin-hancock-66b382129")
contact_a = get_apollo_contact(li_austin)
row_a = row_by_ig["austin.hancock1"]
row_a["linkedin_url"] = li_austin
row_a["email"]        = contact_a["email"]
row_a["phone"]        = contact_a["phone"]
row_a["error"]        = ""
print(f"\n  -> email={contact_a['email'] or '-'}  phone={contact_a['phone'] or '-'}")

# ── Save ───────────────────────────────────────────────────────────────────────
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)

print("\n" + "="*64)
print("output.csv saved.")
