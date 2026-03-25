#!/usr/bin/env python3
"""
Retry pass for handles that failed LinkedIn lookup.
- Reuses full_name + niche already extracted (no re-scraping)
- "URL not found": tries 3 progressively looser SerpAPI queries
- "Match unconfirmed": re-tests the [UNVERIFIED] URL with a looser prompt,
  then also tries fresh SerpAPI searches if still failing
- Updates output.csv in place
"""

import csv
import json
import re
import sys
import time
import requests
import anthropic
from serpapi import GoogleSearch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERPAPI_KEY = "c48a948bd6b662aea5c8a37211bb5a4dbdf31909c1ecc0b27f75590be7c63e9f"
APOLLO_KEY  = "QNzIvFjlqqLHkuwhmxbgIQ"
OUTPUT_CSV  = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

claude = anthropic.Anthropic()


# ── helpers ──────────────────────────────────────────────────────────────────

def strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def serp_first_linkedin(query):
    """Return first linkedin.com/in/ URL from a SerpAPI query, or ''."""
    results = GoogleSearch({"q": query, "api_key": SERPAPI_KEY, "num": 5}).get_dict()
    for r in results.get("organic_results", []):
        link = r.get("link", "")
        if "linkedin.com/in/" in link:
            return link
    return ""


def find_linkedin(full_name, niche):
    """
    Try 4 progressively looser searches and return the first LinkedIn URL found.
    """
    niche_short = niche.split("(")[0].split("/")[0].strip()  # e.g. "Real estate investing"
    first_name  = full_name.split()[0]
    last_name   = full_name.split()[-1] if len(full_name.split()) > 1 else ""

    queries = [
        f'"{full_name}" {niche_short} site:linkedin.com/in',      # quoted name + short niche
        f'{full_name} site:linkedin.com/in',                       # name only, no niche
        f'"{first_name} {last_name}" site:linkedin.com/in',        # quoted first+last only
        f'{full_name} linkedin',                                    # broadest fallback
    ]
    for q in queries:
        print(f"    [SerpAPI] {q}")
        url = serp_first_linkedin(q)
        if url:
            print(f"    -> {url}")
            return url
        time.sleep(0.5)
    return ""


def verify_linkedin_loose(display_name, bio, full_name, linkedin_url):
    """
    Looser Claude verification — accept partial/abbreviation matches.
    Returns (match: bool, reason: str)
    """
    slug = linkedin_url.split("linkedin.com/in/")[-1].rstrip("/")
    first = full_name.split()[0].lower()
    last  = full_name.split()[-1].lower() if len(full_name.split()) > 1 else ""

    prompt = f"""Does this LinkedIn profile URL likely belong to the same person as this Instagram account?

Instagram Display Name: {display_name}
Instagram Bio: {bio[:400]}
Identified Full Name: {full_name}
LinkedIn URL: {linkedin_url}
LinkedIn slug: "{slug}"

Matching rules — answer TRUE if ANY of these hold:
- The slug contains both the first name and last name (in any form/order)
- The slug contains the first name AND a recognizable abbreviation of the last name
- The slug contains the last name AND a recognizable abbreviation of the first name
- The slug is a well-known username variation of the name (e.g. initials + last name)
- The bio mentions a handle/username that matches the slug

Only answer FALSE if the slug clearly belongs to a completely different person.
Respond ONLY with valid JSON: {{"match": true_or_false, "reason": "one sentence"}}"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(strip_json_fences(msg.content[0].text))
    return bool(data.get("match", False)), data.get("reason", "")


def get_apollo_contact(linkedin_url):
    resp = requests.post(
        "https://api.apollo.io/v1/people/match",
        headers={"Content-Type": "application/json", "x-api-key": APOLLO_KEY},
        json={"linkedin_url": linkedin_url, "reveal_personal_emails": True},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        print(f"    Apollo {resp.status_code}: {resp.text[:200]}")
        return {"email": "", "phone": ""}
    person  = resp.json().get("person") or {}
    email   = person.get("email", "")
    contact = person.get("contact") or {}
    phones  = contact.get("phone_numbers", [])
    mobile  = next((p for p in phones if p.get("type") == "mobile"), None)
    phone   = (mobile or (phones[0] if phones else {})).get("sanitized_number", "")
    print(f"    email={email or '-'}  phone={phone or '-'}")
    return {"email": email, "phone": phone}


# ── main retry logic ──────────────────────────────────────────────────────────

def retry_row(row):
    handle     = row["handle"]
    full_name  = row["full_name"]
    niche      = row["niche"]
    error      = row["error"]
    # Strip [UNVERIFIED] tag if present from a previous run
    prev_li    = row["linkedin_url"].replace(" [UNVERIFIED]", "").strip()

    print(f"\n{'='*64}")
    print(f"  {handle}  ({full_name} / {niche})")
    print(f"  error was: {error}")
    print('='*64)

    # We need the bio for verification — fetch it from Apify only if verifying
    # For "URL not found" we just need to find + verify, bio from row not stored.
    # We'll do verification without bio if unavailable (it's already in Claude's training).
    bio          = ""   # not stored in CSV; verification uses name matching primarily
    display_name = full_name

    result = dict(row)  # start with existing data

    # ── CASE 1: had a URL but Claude rejected it ───────────────────────────
    if error == "LinkedIn match unconfirmed" and prev_li:
        print(f"  Re-verifying (loose): {prev_li}")
        match, reason = verify_linkedin_loose(display_name, bio, full_name, prev_li)
        print(f"    match={match}  reason={reason}")
        if match:
            result["linkedin_url"] = prev_li
            result["error"]        = ""
            contact = get_apollo_contact(prev_li)
            result["email"] = contact["email"]
            result["phone"] = contact["phone"]
            return result
        # Still no match — fall through to fresh SerpAPI search
        print("  Still unconfirmed — trying fresh searches...")

    # ── CASE 2: no URL found, or re-verify failed — search fresh ──────────
    linkedin_url = find_linkedin(full_name, niche)
    if not linkedin_url:
        print("  No LinkedIn URL found after all attempts")
        result["error"] = "LinkedIn URL not found (retried)"
        return result

    match, reason = verify_linkedin_loose(display_name, bio, full_name, linkedin_url)
    print(f"    match={match}  reason={reason}")
    if not match:
        result["linkedin_url"] = linkedin_url + " [UNVERIFIED]"
        result["error"]        = "LinkedIn match unconfirmed (retried)"
        return result

    result["linkedin_url"] = linkedin_url
    result["error"]        = ""
    contact = get_apollo_contact(linkedin_url)
    result["email"] = contact["email"]
    result["phone"] = contact["phone"]
    return result


def main():
    # Read full CSV
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    fieldnames = ["handle", "full_name", "niche", "linkedin_url", "email", "phone", "error"]

    # Identify failed rows — deduplicate by handle+name
    seen    = set()
    failed  = []
    ok_rows = []
    for r in rows:
        key = (r["handle"], r["full_name"])
        if r["error"]:
            if key not in seen:
                seen.add(key)
                failed.append(r)
            # skip duplicate failed rows entirely
        else:
            ok_rows.append(r)

    print(f"Retrying {len(failed)} failed handles (deduped from {sum(1 for r in rows if r['error'])} failed rows)\n")

    retried = []
    for i, row in enumerate(failed, 1):
        print(f"\n[{i}/{len(failed)}]")
        retried.append(retry_row(row))
        time.sleep(1)

    # Merge: ok_rows + retried results
    all_rows = ok_rows + retried

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n{'='*64}")
    print(f"Done. output.csv updated with {len(retried)} retried rows.")

    resolved = [r for r in retried if not r["error"]]
    still_failed = [r for r in retried if r["error"]]
    print(f"  Resolved:     {len(resolved)}")
    print(f"  Still failed: {len(still_failed)}")
    for r in still_failed:
        print(f"    {r['handle']}  {r['error']}")


if __name__ == "__main__":
    main()
