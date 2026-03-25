#!/usr/bin/env python3
"""
Final retry for the 7 still-unresolved handles.
Strategy order:
  1. Instagram username  site:linkedin.com/in
  2. "Full Name"         site:linkedin.com/in
  3. Full Name           site:linkedin.com/in
  4. Full Name           linkedin  (broadest)
  5. Website about/contact page — grep for linkedin.com/in links
"""

import csv, json, re, sys, time
import requests
import anthropic
from serpapi import GoogleSearch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERPAPI_KEY = "c48a948bd6b662aea5c8a37211bb5a4dbdf31909c1ecc0b27f75590be7c63e9f"
APOLLO_KEY  = "QNzIvFjlqqLHkuwhmxbgIQ"
OUTPUT_CSV  = r"C:\Users\Owenb\Desktop\instagram-tool\output.csv"

claude = anthropic.Anthropic()

TARGETS = [
    # (handle_url, full_name, niche, website)
    ("https://www.instagram.com/watchmeamazon/",  "Larry Lubarsky", "Amazon FBA Wholesale",          "https://2dworkflow.com"),
    ("https://www.instagram.com/justinwoll/",     "Justin Woll",    "E-commerce coaching",           "https://bsfmarketing.com"),
    ("https://www.instagram.com/nuccirealestate/","Anthony Nucci",  "Real estate coaching",          "https://www.callsintolistings.com"),
    ("https://www.instagram.com/joeparys/",       "Joe Parys",      "Digital Products",              "https://joeparys.com"),
    ("https://www.instagram.com/austin.hancock1/","Austin Hancock", "Real estate investing",         ""),
    ("https://www.instagram.com/adamenfroy/",     "Adam Enfroy",    "Blogging coaching",             "https://adamenfroy.com"),
    ("https://www.instagram.com/jordanplatten/",  "Jordan Platten", "Social media marketing agency", "https://affluent.academy"),
]


def extract_username(handle_url):
    return handle_url.strip().rstrip("/").split("instagram.com/")[-1].rstrip("/")


def serp_first_linkedin(query):
    r = GoogleSearch({"q": query, "api_key": SERPAPI_KEY, "num": 5}).get_dict()
    for hit in r.get("organic_results", []):
        link = hit.get("link", "")
        if "linkedin.com/in/" in link:
            return link
    return ""


def find_linkedin_from_website(website_url):
    """Fetch homepage + /about + /contact and grep for linkedin.com/in/ links."""
    if not website_url:
        return ""
    pages = [website_url, website_url.rstrip("/") + "/about",
             website_url.rstrip("/") + "/contact"]
    seen = set()
    for url in pages:
        try:
            resp = requests.get(url, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
            links = re.findall(r'linkedin\.com/in/[a-zA-Z0-9_\-]+', resp.text)
            for l in links:
                full = "https://www." + l
                if full not in seen:
                    seen.add(full)
                    return full
        except Exception:
            pass
    return ""


def find_linkedin(ig_username, full_name):
    """Try 4 query strategies in order, return first linkedin.com/in/ URL."""
    queries = [
        f"{ig_username} site:linkedin.com/in",
        f'"{full_name}" site:linkedin.com/in',
        f"{full_name} site:linkedin.com/in",
        f"{full_name} linkedin",
    ]
    for q in queries:
        print(f"    [SerpAPI] {q}")
        url = serp_first_linkedin(q)
        if url:
            print(f"    -> {url}")
            return url
        time.sleep(0.4)
    return ""


def strip_json_fences(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def verify_linkedin(full_name, linkedin_url):
    slug = linkedin_url.split("linkedin.com/in/")[-1].rstrip("/")
    prompt = f"""Does this LinkedIn URL belong to someone named "{full_name}"?
LinkedIn URL: {linkedin_url}
Slug: "{slug}"

Answer TRUE if the slug contains the person's first name, last name, or clear abbreviation of either.
Answer FALSE only if the slug clearly belongs to a completely different person.
Respond ONLY with JSON: {{"match": true_or_false, "reason": "one sentence"}}"""
    msg = claude.messages.create(
        model="claude-sonnet-4-6", max_tokens=150,
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


def main():
    # Load full CSV
    with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = ["handle", "full_name", "niche", "linkedin_url", "email", "phone", "error"]

    # Build lookup by handle for easy update
    row_by_handle = {r["handle"]: r for r in rows}

    for i, (handle, full_name, niche, website) in enumerate(TARGETS, 1):
        ig_username = extract_username(handle)
        print(f"\n[{i}/7] {handle}  ({full_name})")
        print("=" * 64)

        # 1. SerpAPI username + name strategies
        linkedin_url = find_linkedin(ig_username, full_name)

        # 2. Fallback: scrape website for LinkedIn link
        if not linkedin_url and website:
            print(f"    [Website] Checking {website} for LinkedIn links...")
            linkedin_url = find_linkedin_from_website(website)
            if linkedin_url:
                print(f"    -> Found on website: {linkedin_url}")

        if not linkedin_url:
            print("    No LinkedIn found after all strategies")
            if handle in row_by_handle:
                row_by_handle[handle]["error"] = "LinkedIn URL not found (all strategies exhausted)"
            continue

        # 3. Verify
        match, reason = verify_linkedin(full_name, linkedin_url)
        print(f"    match={match}  reason={reason}")
        if not match:
            if handle in row_by_handle:
                row_by_handle[handle]["linkedin_url"] = linkedin_url + " [UNVERIFIED]"
                row_by_handle[handle]["error"] = "LinkedIn match unconfirmed (final retry)"
            continue

        # 4. Apollo
        contact = get_apollo_contact(linkedin_url)

        # 5. Update row
        if handle in row_by_handle:
            row_by_handle[handle]["linkedin_url"] = linkedin_url
            row_by_handle[handle]["email"]        = contact["email"]
            row_by_handle[handle]["phone"]        = contact["phone"]
            row_by_handle[handle]["error"]        = ""
        time.sleep(1)

    # Write updated CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'='*64}")
    print("Done. output.csv updated.")
    resolved = [h for h, _, _, _ in TARGETS if not row_by_handle.get(h, {}).get("error")]
    print(f"  Resolved {len(resolved)}/7")
    for h in [t[0] for t in TARGETS]:
        r = row_by_handle.get(h, {})
        status = "OK" if not r.get("error") else r.get("error")
        print(f"  {h.split('instagram.com/')[-1].rstrip('/')}  {status}")


if __name__ == "__main__":
    main()
