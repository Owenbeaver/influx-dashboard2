#!/usr/bin/env python3
"""One-off fix for Larry Lubarsky — try all LinkedIn candidates per query."""
import sys, json, re, requests, csv, time
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from serpapi import GoogleSearch
import anthropic

SERPAPI_KEY = "c48a948bd6b662aea5c8a37211bb5a4dbdf31909c1ecc0b27f75590be7c63e9f"
APOLLO_KEY  = "QNzIvFjlqqLHkuwhmxbgIQ"
OUTPUT_CSV  = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"
claude = anthropic.Anthropic()


def serp_all_linkedin(query):
    r = GoogleSearch({"q": query, "api_key": SERPAPI_KEY, "num": 5}).get_dict()
    return [h["link"] for h in r.get("organic_results", []) if "linkedin.com/in/" in h.get("link", "")]


def verify(full_name, url):
    slug = url.split("linkedin.com/in/")[-1].rstrip("/")
    prompt = f'Does the LinkedIn slug "{slug}" belong to someone named "{full_name}"? Reply with only valid JSON on one line: {{"match": true}} or {{"match": false}}'
    msg = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip().strip("`").strip()
    # strip any leading label like "json"
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text).get("match", False)


full_name = "Larry Lubarsky"
queries = [
    "watchmeamazon site:linkedin.com/in",
    '"Larry Lubarsky" site:linkedin.com/in',
    "Larry Lubarsky site:linkedin.com/in",
    "Larry Lubarsky amazon linkedin",
]

found = ""
for q in queries:
    print(f"\nQuery: {q}")
    urls = serp_all_linkedin(q)
    print(f"  Candidates: {urls}")
    for url in urls:
        ok = verify(full_name, url)
        print(f"  {url}  match={ok}")
        if ok:
            found = url
            break
    if found:
        break
    time.sleep(0.4)

if not found:
    print("\nNot found — no matching LinkedIn profile.")
    sys.exit(0)

print(f"\nVerified: {found}")
resp = requests.post(
    "https://api.apollo.io/v1/people/match",
    headers={"Content-Type": "application/json", "x-api-key": APOLLO_KEY},
    json={"linkedin_url": found, "reveal_personal_emails": True},
    timeout=30,
)
person  = resp.json().get("person") or {}
email   = person.get("email", "")
contact = person.get("contact") or {}
phones  = contact.get("phone_numbers", [])
mobile  = next((p for p in phones if p.get("type") == "mobile"), None)
phone   = (mobile or (phones[0] if phones else {})).get("sanitized_number", "")
print(f"email={email or '-'}  phone={phone or '-'}")

with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
fieldnames = ["handle", "full_name", "niche", "linkedin_url", "email", "phone", "error"]
for r in rows:
    if "watchmeamazon" in r["handle"]:
        r["linkedin_url"] = found
        r["email"]        = email
        r["phone"]        = phone
        r["error"]        = ""
        print(f"Updated: {r['handle']}")
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
print("CSV saved.")
