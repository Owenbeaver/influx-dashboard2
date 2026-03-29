#!/usr/bin/env python3
"""
Influx Lead Engine — ICP Filter

Takes raw_handles.csv (output of scraper.py) and filters down to only accounts
that match the Ideal Customer Profile:

  Pass 1 — Hard gates (no API calls, instant)
    - Skip obviously bad handles (brands, platforms, media accounts)

  Pass 2 — Apify Instagram scrape (follower count + bio + website)
    - Followers: 10K–100K
    - Disqualifier keywords in bio
    - Must have a link-in-bio

  Pass 3 — Claude scoring (only accounts that passed Pass 2)
    - Bio coaching signals: "I help", "DM me", "Apply", dollar figures
    - Niche match vs ICP approved list
    - Confidence score

  Pass 4 — Link-in-bio destination check (optional, Apify website crawl)
    - Checks if landing page contains "apply", "book a call", "join", etc.
    - Flags Amazon storefronts / physical product pages as disqualifiers

Output: qualified_leads.csv

Run:
  python filter.py                                  — uses default raw_handles.csv
  python filter.py --input raw_handles.csv          — explicit input
  python filter.py --input raw_handles.csv --skip-website  — skip Pass 4 (saves Apify credits)
"""

import csv
import json
import re
import sys
import os
import time
import argparse
import requests
import anthropic
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env", override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── API Keys ──────────────────────────────────────────────────────────────────
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")

# ── File paths ────────────────────────────────────────────────────────────────
BASE_DIR    = r"C:\Users\Owenb\Desktop\instagram-tool"
INPUT_CSV   = os.environ.get("FILTER_INPUT_CSV",  os.path.join(BASE_DIR, "raw_handles.csv"))
OUTPUT_CSV  = os.environ.get("FILTER_OUTPUT_CSV", os.path.join(BASE_DIR, "qualified_leads.csv"))

# ── Claude client ─────────────────────────────────────────────────────────────
claude = anthropic.Anthropic()


# ==============================================================================
# PASS 1 — Hard-skip list (no API needed)
# ==============================================================================

# Handles that are clearly brands, platforms, or media — not individual creators
_SKIP_HANDLES = {
    "instagram", "facebook", "tiktok", "youtube", "twitter", "linkedin",
    "amazon", "shopify", "skool", "kajabi", "clickfunnels", "gohighlevel",
    "forbes", "entrepreneur", "inc", "businessinsider", "cnbc", "huffpost",
    "nytimes", "techcrunch", "wired",
}

# Patterns that indicate a brand/media account, not a creator
_SKIP_PATTERNS = re.compile(
    r"^(official|team|brand|shop|store|news|media|tv|hq|corp|inc|llc|group)$",
    re.IGNORECASE,
)

def _pass1_ok(handle: str) -> bool:
    h = handle.lower().strip("._")
    if h in _SKIP_HANDLES:
        return False
    if _SKIP_PATTERNS.match(h):
        return False
    if len(h) < 3:
        return False
    return True


# ==============================================================================
# PASS 2 — Apify Instagram scrape + hard metric gates
# ==============================================================================

MIN_FOLLOWERS = 10_000
MAX_FOLLOWERS = 100_000

# Disqualifier keywords in bio
_DQ_RE = re.compile(
    r"\bcredit.?repair\b|\bcredit.?score\b|\btradeline\b"
    r"|\bforex\b|\bday.?trad|\bcrypto\b|\bstock.?market\b"
    r"|\brealtor\b|\breal.?estate.?agent\b|\bmortgage.?broker\b|\bloan.?officer\b"
    r"|\binsurance.?agent\b"
    r"|\bdropshipping.?store\b|\bamazon.?storefront\b"
    r"|\bbeauty.?influencer\b|\bfitness.?model\b",
    re.IGNORECASE,
)

def _apify_run(actor_id: str, input_data: dict, max_wait: int = 180) -> list:
    """Run an Apify actor and return dataset items. Same pattern as pipeline.py."""
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    resp    = requests.post(run_url, params={"token": APIFY_TOKEN}, json=input_data, timeout=30)
    resp.raise_for_status()
    run_id  = resp.json()["data"]["id"]
    print(f"      -> Apify run: {run_id}")

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    status     = "RUNNING"
    for _ in range(max_wait // 5):
        time.sleep(5)
        st     = requests.get(status_url, params={"token": APIFY_TOKEN}, timeout=30)
        status = st.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        print(f"      ... {status}")

    if status != "SUCCEEDED":
        print(f"      X Run ended: {status}")
        return []

    items = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
        params={"token": APIFY_TOKEN}, timeout=30,
    )
    items.raise_for_status()
    return items.json()


def _first_url(profile: dict) -> str:
    urls = profile.get("externalUrls", [])
    if urls and isinstance(urls, list):
        return urls[0].get("url", "") if isinstance(urls[0], dict) else str(urls[0])
    return profile.get("externalUrl", "") or profile.get("website", "")


def _scrape_batch(handles: list[str]) -> dict:
    """
    Scrape a batch of Instagram handles via Apify.
    Returns dict: {username -> profile_dict}
    """
    urls = [f"https://www.instagram.com/{h}/" for h in handles]
    print(f"  [Apify] Scraping {len(urls)} profiles...")
    results = _apify_run(
        "apify~instagram-scraper",
        {"directUrls": urls, "resultsType": "details", "resultsLimit": len(urls)},
        max_wait=300,
    )
    out = {}
    for p in results:
        username = (p.get("username") or "").lower()
        if username:
            out[username] = {
                "username":     username,
                "display_name": p.get("fullName", ""),
                "bio":          p.get("biography", ""),
                "website":      _first_url(p),
                "followers":    p.get("followersCount", 0),
                "following":    p.get("followingCount", 0),
                "posts":        p.get("postsCount", 0),
            }
    return out


def _pass2_ok(profile: dict) -> tuple[bool, str]:
    """Returns (passes, reason)."""
    followers = profile.get("followers", 0)
    bio       = profile.get("bio", "")
    website   = profile.get("website", "")

    if followers < MIN_FOLLOWERS:
        return False, f"followers too low ({followers:,})"
    if followers > MAX_FOLLOWERS:
        return False, f"followers too high ({followers:,})"
    if _DQ_RE.search(bio):
        return False, "disqualifier keyword in bio"
    if not website:
        return False, "no link-in-bio"
    return True, "ok"


# ==============================================================================
# PASS 3 — Claude ICP scoring
# ==============================================================================

_APPROVED_NICHES = [
    "Business Acquisition", "Buying Businesses", "SMB M&A", "Search Fund",
    "Real Estate Wholesaling", "Land Flipping",
    "Section 8 Investing", "Rental Investing",
    "Amazon FBA", "E-commerce",
    "Tech Sales Coaching", "Career Transition",
    "SMMA", "Agency Building", "AI Lead Generation",
    "UGC Coaching", "TikTok Shop Affiliate",
    "Home Service Business Coaching",
]

def _claude_score(profile: dict) -> dict:
    """
    Ask Claude to evaluate the profile against the ICP.
    Returns structured scoring dict.
    """
    prompt = f"""You are evaluating an Instagram profile to see if it matches this Ideal Customer Profile (ICP):

ICP CRITERIA:
- Practitioner-turned-educator: person who achieved a result and now teaches others
- Sells a coaching program, online course, mentorship, or paid community
- Does NOT sell physical products or rely on brand sponsorships
- Bio uses "I help [audience] [achieve result]" framework (or similar)
- Bio contains social proof: dollar figure, client count, or specific result metric
- Bio has a CTA: "DM me", "Apply", "Free course", or similar
- Link-in-bio routes to landing page, application form, Skool, or Stan Store

APPROVED NICHES: {", ".join(_APPROVED_NICHES)}

STRICT DISQUALIFIERS:
- Credit repair, forex, crypto, stock trading
- Realtors, mortgage brokers, insurance agents
- Physical product sellers
- Lifestyle, beauty, fitness influencers with no coaching offer

PROFILE TO EVALUATE:
Username: @{profile.get("username", "")}
Display Name: {profile.get("display_name", "")}
Followers: {profile.get("followers", 0):,}
Bio: {profile.get("bio", "(empty)")}
Link-in-bio: {profile.get("website", "(none)")}

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "approved": true or false,
  "niche": "detected niche or null",
  "coaching_signals": ["list", "of", "signals", "found"],
  "disqualifier": "reason if rejected, else null",
  "confidence": "high|medium|low",
  "reason": "one sentence explanation"
}}"""

    msg  = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        return {"approved": False, "niche": None, "coaching_signals": [],
                "disqualifier": "Claude parse error", "confidence": "low", "reason": text[:200]}


# ==============================================================================
# PASS 4 — Link-in-bio destination check
# ==============================================================================

# Landing page signals that confirm a coaching offer
_LP_APPROVE_RE = re.compile(
    r"\b(apply|book a call|book a free|schedule a call|join the community"
    r"|enroll|get started|free training|free course|claim your spot"
    r"|limited spots|work with me|join now|get access)\b",
    re.IGNORECASE,
)

# Destination disqualifiers
_LP_DQ_RE = re.compile(
    r"\b(amazon\.com/shop|shopify\.com|etsy\.com|merch|physical product"
    r"|buy now|add to cart|free shipping)\b",
    re.IGNORECASE,
)

def _check_landing_page(url: str) -> tuple[str, str]:
    """
    Fetch the link-in-bio URL and check for coaching signals.
    Returns (verdict, reason): verdict is "approved"|"rejected"|"unknown"
    """
    if not url:
        return "unknown", "no url"
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0"})
        text = resp.text[:8000]   # only need first 8KB

        if _LP_DQ_RE.search(text):
            return "rejected", "physical product / storefront page"

        m = _LP_APPROVE_RE.search(text)
        if m:
            return "approved", f"found '{m.group(0)}' on landing page"

        return "unknown", "no strong signals found"
    except Exception as e:
        return "unknown", str(e)[:100]


# ==============================================================================
# MAIN FILTER RUNNER
# ==============================================================================

BATCH_SIZE = 20   # Apify handles per run (keeps runs fast)

def run_filter(input_path: str, output_path: str, skip_website: bool = False) -> None:
    # -- Load raw handles ------------------------------------------------------
    raw = []
    with open(input_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            h = row.get("handle","").strip().lower()
            if h:
                raw.append({"handle": h, "source": row.get("source",""), "niche": row.get("niche","")})

    print(f"\nLoaded {len(raw)} raw handles from {input_path}")

    # -- Pass 1: hard skip (no API) --------------------------------------------
    p1_pass = [r for r in raw if _pass1_ok(r["handle"])]
    p1_fail = len(raw) - len(p1_pass)
    print(f"\nPass 1 (hard skip): {len(p1_pass)} passed, {p1_fail} skipped")

    # -- Pass 2: Apify scrape + metric gates -----------------------------------
    print(f"\nPass 2 (Apify scrape + metrics): scraping {len(p1_pass)} profiles in batches of {BATCH_SIZE}...")
    all_profiles : dict = {}
    handles_list = [r["handle"] for r in p1_pass]

    for i in range(0, len(handles_list), BATCH_SIZE):
        batch = handles_list[i:i+BATCH_SIZE]
        print(f"  Batch {i//BATCH_SIZE + 1}: {batch[0]} ... {batch[-1]}")
        profiles = _scrape_batch(batch)
        all_profiles.update(profiles)
        time.sleep(2)

    p2_pass = []
    p2_fail = []
    for r in p1_pass:
        profile = all_profiles.get(r["handle"])
        if not profile:
            p2_fail.append({**r, "fail_reason": "profile not found / private"})
            continue
        ok, reason = _pass2_ok(profile)
        if ok:
            p2_pass.append({**r, **profile})
        else:
            p2_fail.append({**r, **profile, "fail_reason": reason})

    print(f"Pass 2: {len(p2_pass)} passed, {len(p2_fail)} failed")
    if p2_fail:
        reasons = {}
        for r in p2_fail:
            key = r.get("fail_reason","").split("(")[0].strip()
            reasons[key] = reasons.get(key, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {count:>4}x  {reason}")

    if not p2_pass:
        print("\nNo accounts passed Pass 2 — nothing to score.")
        return

    # -- Pass 3: Claude ICP scoring -------------------------------------------
    print(f"\nPass 3 (Claude scoring): evaluating {len(p2_pass)} profiles...")
    p3_pass = []
    p3_fail = []

    for i, r in enumerate(p2_pass, 1):
        print(f"  [{i}/{len(p2_pass)}] @{r['handle']} ({r.get('followers',0):,} followers)")
        score = _claude_score(r)
        r["claude_approved"]  = score.get("approved", False)
        r["claude_niche"]     = score.get("niche", "")
        r["claude_signals"]   = ", ".join(score.get("coaching_signals", []))
        r["claude_reject"]    = score.get("disqualifier", "")
        r["claude_confidence"]= score.get("confidence", "")
        r["claude_reason"]    = score.get("reason", "")

        status = "✓" if score.get("approved") else "✗"
        print(f"    {status} {score.get('confidence','?')} — {score.get('reason','')[:80]}")

        if score.get("approved"):
            p3_pass.append(r)
        else:
            p3_fail.append(r)
        time.sleep(0.3)   # gentle rate limiting

    print(f"Pass 3: {len(p3_pass)} approved, {len(p3_fail)} rejected by Claude")

    if not p3_pass:
        print("\nNo accounts passed Claude scoring.")
        return

    # -- Pass 4: Link-in-bio check (optional) ----------------------------------
    if not skip_website:
        print(f"\nPass 4 (landing page check): checking {len(p3_pass)} URLs...")
        for i, r in enumerate(p3_pass, 1):
            url = r.get("website","")
            print(f"  [{i}/{len(p3_pass)}] {url[:60]}")
            verdict, reason = _check_landing_page(url)
            r["lp_verdict"] = verdict
            r["lp_reason"]  = reason
            print(f"    → {verdict}: {reason}")
            time.sleep(0.5)
    else:
        print("\nPass 4 skipped (--skip-website)")
        for r in p3_pass:
            r["lp_verdict"] = "skipped"
            r["lp_reason"]  = ""

    # -- Save output -----------------------------------------------------------
    fieldnames = [
        "handle", "display_name", "followers", "bio", "website",
        "source", "niche", "claude_niche", "claude_signals",
        "claude_confidence", "claude_reason", "lp_verdict", "lp_reason",
    ]

    # Final list: approved by Claude + not rejected by landing page check
    final = [r for r in p3_pass if r.get("lp_verdict") != "rejected"]

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(final)

    print(f"\n{'='*64}")
    print(f"  Filter complete")
    print(f"  Input:     {len(raw)} raw handles")
    print(f"  Output:    {len(final)} qualified leads → {output_path}")
    print(f"  Pass rate: {len(final)/len(raw)*100:.1f}%")
    print(f"{'='*64}")
    print(f"\nNext step: run pipeline.py on {output_path} to get emails + phones")


# ==============================================================================
# CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Influx filter — apply ICP gates to raw handles")
    parser.add_argument("--input",        type=str, default=INPUT_CSV,  help="Input CSV (raw_handles.csv)")
    parser.add_argument("--output",       type=str, default=OUTPUT_CSV, help="Output CSV (qualified_leads.csv)")
    parser.add_argument("--skip-website", action="store_true",          help="Skip Pass 4 landing page check")
    args = parser.parse_args()

    run_filter(args.input, args.output, skip_website=args.skip_website)


if __name__ == "__main__":
    main()
