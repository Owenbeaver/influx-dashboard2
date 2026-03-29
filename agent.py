#!/usr/bin/env python3
"""
Influx Creator Sourcing Agent
Usage:  python agent.py "find 50 wholesale real estate coaches"
        python agent.py "find 50 wholesale real estate coaches" --test   (10 accounts only)
"""

import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import anthropic
from serpapi import GoogleSearch
from dotenv import load_dotenv

# ── Setup — identical pattern to pipeline.py ───────────────────────────────────
load_dotenv(Path(__file__).parent / ".env", override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
claude      = anthropic.Anthropic()

if not APIFY_TOKEN:
    sys.exit("ERROR: APIFY_TOKEN not set in .env")
if not SERPAPI_KEY:
    sys.exit("ERROR: SERPAPI_KEY not set in .env")

PROJECT_DIR = Path(__file__).parent

# ── ICP constants ──────────────────────────────────────────────────────────────
MIN_FOLLOWERS   = 8_000
MIN_AVG_VIEWS   = 1_500
MAX_FOLLOWERS   = 200_000
SCRAPE_BATCH    = 20    # usernames per Apify run

REJECT_BIO_WORDS = {
    "forex", "crypto", "trading", "stocks", "realtor",
    "mortgage", "insurance", "tradeline",
}
REJECT_BIO_PHRASES = {
    "real estate agent", "credit repair", "credit score", "physical products",
}
REJECT_LINK_DOMAINS = {"amazon.com", "etsy.com"}   # .myshopify.com handled separately

# Instagram URL slugs that are not usernames
_IG_RESERVED = {"p", "reel", "reels", "stories", "explore", "accounts", "tv",
                "about", "privacy", "legal", "help", "ar", "shop"}


# ── Tiny helpers ───────────────────────────────────────────────────────────────

def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_ig_username(url: str) -> str:
    """Extract bare username from an instagram.com URL. Returns '' for posts/reels."""
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?", url)
    if not m:
        return ""
    slug = m.group(1).lower()
    return "" if slug in _IG_RESERVED else slug


def _link_domain(url: str) -> str:
    """Return bare domain (no www) from a URL."""
    if not url:
        return ""
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", url)
    return m.group(1).lower() if m else ""


def _fetch(url: str, timeout: int = 12) -> str:
    """GET a URL, return response text. Empty string on any error."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""


def _make_account(username: str, source: str, skool_url: str = "") -> dict:
    return {
        "username":  username.lower().lstrip("@"),
        "sources":   {source},
        "skool_url": skool_url,
    }


# ── Apify runner — identical to pipeline.py ────────────────────────────────────

def apify_run_and_wait(actor_id: str, input_data: dict, max_wait: int = 180) -> list:
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    try:
        resp = requests.post(
            run_url,
            params={"token": APIFY_TOKEN},
            json=input_data,
            timeout=30,
        )
        if resp.status_code == 402:
            print("  [ERROR] Apify 402 — out of credits. Top up at apify.com/billing")
            return []
        resp.raise_for_status()
    except Exception as e:
        print(f"  [ERROR] Apify start failed: {e}")
        return []

    run_id = resp.json()["data"]["id"]
    print(f"    -> Apify run {run_id[:10]}...")

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    status = "RUNNING"
    for _ in range(max_wait // 5):
        time.sleep(5)
        try:
            st = requests.get(status_url, params={"token": APIFY_TOKEN}, timeout=30)
            st.raise_for_status()
            status = st.json()["data"]["status"]
        except Exception:
            continue
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        print(f"    ... {status}")

    if status != "SUCCEEDED":
        print(f"    X Run ended: {status}")
        return []

    items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items"
    try:
        items = requests.get(items_url, params={"token": APIFY_TOKEN}, timeout=60)
        items.raise_for_status()
        return items.json()
    except Exception as e:
        print(f"  [ERROR] Apify items fetch failed: {e}")
        return []


# ── Step 1a — Parse natural-language prompt ────────────────────────────────────

def parse_prompt(prompt: str) -> dict:
    """Use Claude Haiku to extract niche, search keywords, hashtags, target count."""
    print(f'\n[Step 1] Parsing: "{prompt}"')
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": f"""Extract structured info from this creator sourcing request.

Request: "{prompt}"

Generate exactly 8 keyword phrases with varied angles: base term, how-to, program/course name, technique name.
Example for "wholesale real estate": ["wholesale real estate coaching", "how to wholesale real estate",
"wholesale real estate program", "wholesale houses course", "flip houses coaching",
"wholesaling houses training", "real estate wholesaling mentor", "wholesale deals course"]

Return ONLY valid JSON — no extra text:
{{
  "niche": "short niche label, e.g. wholesale real estate",
  "keywords": ["phrase1", "phrase2", "phrase3", "phrase4", "phrase5", "phrase6", "phrase7", "phrase8"],
  "hashtags": ["2-3 Instagram hashtags WITHOUT the # symbol, e.g. wholesalerealestate"],
  "target_count": 50
}}"""}],
    )
    parsed = json.loads(_strip_json_fences(msg.content[0].text))
    print(f"  Niche      : {parsed['niche']}")
    print(f"  Keywords   : {parsed['keywords']}")
    print(f"  Hashtags   : {parsed['hashtags']}")
    print(f"  Target     : {parsed['target_count']}")
    return parsed


# ── Step 1b — Four discovery sources ──────────────────────────────────────────

_COACH_MODIFIERS = ["coaching", "mentor", "course", "training", "program"]


def discover_via_google(keywords: list, niche: str) -> list:
    """
    SerpAPI Google search — 10 queries.
    Template A (5×): short niche term + modifier on site:instagram.com
      — Instagram bios are short; only 2-3 word phrases match reliably here.
    Template B (5×): full keyword phrase on the open web (finds directories,
      articles, etc. that link to or name specific Instagram accounts).
    """
    print("  [Google] Searching (10 queries)...")
    found = []
    seen  = set()

    # Template A: niche + 5 coaching modifiers — finds bio content on Instagram
    for mod in _COACH_MODIFIERS:
        q = f'{niche} {mod} site:instagram.com'
        try:
            r = GoogleSearch({"q": q, "api_key": SERPAPI_KEY, "num": 10}).get_dict()
            for hit in r.get("organic_results", []):
                link = hit.get("link", "")
                if "instagram.com" not in link:
                    continue
                un = _extract_ig_username(link)
                if un and un not in seen:
                    seen.add(un)
                    found.append(_make_account(un, "google"))
            time.sleep(0.4)
        except Exception as e:
            print(f"    [WARN] Google query failed: {e}")

    # Template B: full varied phrases on open web — finds articles naming IG accounts
    for kw in keywords[:5]:
        q = f'"{kw}" instagram.com -site:instagram.com/p/ -site:instagram.com/reel/'
        try:
            r = GoogleSearch({"q": q, "api_key": SERPAPI_KEY, "num": 10}).get_dict()
            for hit in r.get("organic_results", []):
                link = hit.get("link", "")
                if "instagram.com" not in link:
                    continue
                un = _extract_ig_username(link)
                if un and un not in seen:
                    seen.add(un)
                    found.append(_make_account(un, "google"))
            time.sleep(0.4)
        except Exception as e:
            print(f"    [WARN] Google query failed: {e}")

    print(f"  [Google] {len(found)} raw handles")
    return found


def _get_seed_accounts(niche: str, keywords: list) -> list:
    """Ask Claude Haiku to suggest 2-3 well-known Instagram accounts in this niche."""
    kw = keywords[0] if keywords else niche
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"""Name 3 well-known Instagram accounts (coaches or educators) in the "{niche}" space.
Return ONLY a JSON array of bare Instagram usernames (no @ symbol, no URLs):
["username1", "username2", "username3"]
Use real, currently active accounts. If unsure, return your best guesses."""}],
    )
    try:
        seeds = json.loads(_strip_json_fences(msg.content[0].text))
        return [s.lower().strip("@/ ") for s in seeds if isinstance(s, str)][:3]
    except Exception:
        return []


def discover_via_apify(niche: str, keywords: list) -> list:
    """
    Mention mining: scrape seed accounts and extract everyone they mention in posts.
    Instagram's hashtag/user search API is blocked — directUrls still works.
    Validated approach: test_sourcing.py showed mentions in latestPosts are
    the most reliable signal for same-niche accounts.
    """
    print("  [Apify] Mention mining from seed accounts...")
    seeds = _get_seed_accounts(niche, keywords)
    if not seeds:
        print("    No seed accounts suggested — skipping")
        return []
    print(f"    Seeds: {seeds}")

    results = apify_run_and_wait(
        "apify~instagram-scraper",
        {
            "directUrls":   [f"https://www.instagram.com/{s}/" for s in seeds],
            "resultsType":  "details",
            "resultsLimit": len(seeds),
        },
        max_wait=180,
    )

    found = []
    seen  = set()
    for profile in results:
        if profile.get("error"):
            continue
        for post in profile.get("latestPosts", []):
            for mention in post.get("mentions", []):
                un = mention.lower().strip("@, ")
                if un and un not in seen and un not in _IG_RESERVED:
                    seen.add(un)
                    found.append(_make_account(un, "apify"))

    print(f"  [Apify] {len(found)} handles from {len(results)} seed accounts")
    return found


def discover_via_instagram_profiles(niche: str, keywords: list) -> list:
    """
    SerpAPI search for Instagram profile pages (not posts).
    Query: '[niche] instagram.com -site:instagram.com/p/' excludes post URLs
    so results are almost exclusively profile pages.
    """
    print("  [Instagram Profiles] Searching profile pages...")
    found = []
    queries = [f'{niche} instagram.com -site:instagram.com/p/']
    for kw in keywords[:4]:
        queries.append(f'{kw} instagram.com -site:instagram.com/p/ -site:instagram.com/reel/')

    seen = set()
    for q in queries:
        try:
            r = GoogleSearch({"q": q, "api_key": SERPAPI_KEY, "num": 10}).get_dict()
            for hit in r.get("organic_results", []):
                link = hit.get("link", "")
                if "instagram.com" not in link:
                    continue
                un = _extract_ig_username(link)
                if un and un not in seen:
                    seen.add(un)
                    found.append(_make_account(un, "instagram_profiles"))
            time.sleep(0.5)
        except Exception as e:
            print(f"    [WARN] Instagram profiles query failed: {e}")

    print(f"  [Instagram Profiles] {len(found)} handles")
    return found


_SKOOL_RESERVED = {"discover", "login", "signup", "about", "pricing", "help",
                   "blog", "terms", "privacy", "community", "courses"}


def _scrape_skool_community(skool_url: str) -> dict:
    """Fetch one Skool community page. Return {username, skool_url} or {username:'', skool_url}."""
    html = _fetch(skool_url)
    ig_hits = [
        m.lower() for m in re.findall(r'instagram\.com/([A-Za-z0-9_.]{2,30})', html)
        if m.lower() not in _IG_RESERVED
    ]
    if ig_hits:
        return {"username": ig_hits[0], "sources": {"skool"}, "skool_url": skool_url}
    return {"username": "", "sources": {"skool"}, "skool_url": skool_url}


def discover_via_skool(keywords: list) -> list:
    """
    Two-pronged Skool discovery:
    1. SerpAPI keyword search on skool.com (3 keyword variations)
    2. Scrape skool.com/discover browse page and filter by niche keywords
    """
    print("  [Skool] Searching skool.com + browse page...")
    community_urls = set()

    # Prong 1: SerpAPI keyword search — 3 keywords
    for kw in keywords[:3]:
        try:
            r = GoogleSearch({
                "q": f'"{kw}" site:skool.com',
                "api_key": SERPAPI_KEY,
                "num": 10,
            }).get_dict()
            for hit in r.get("organic_results", []):
                link = hit.get("link", "")
                if "skool.com" in link:
                    community_urls.add(link)
            time.sleep(0.4)
        except Exception as e:
            print(f"    [WARN] Skool search failed: {e}")

    # Prong 2: Skool /discover browse page
    try:
        discover_html = _fetch("https://www.skool.com/discover")
        # Extract community slugs from the page (pattern: /slug-with-possible-id-1234)
        slugs = re.findall(r'href="/([a-z0-9][a-z0-9-]{2,48})"', discover_html)
        niche_kws = set(re.findall(r'\b[a-z]{4,}\b', " ".join(keywords).lower()))
        for slug in slugs:
            if slug in _SKOOL_RESERVED:
                continue
            # Only scrape if the slug or surrounding context hints at the niche
            slug_words = set(slug.replace("-", " ").split())
            if niche_kws & slug_words:
                community_urls.add(f"https://www.skool.com/{slug}")
        # Also grab any full skool.com URLs embedded in the discover page
        for link in re.findall(r'https://www\.skool\.com/([a-z0-9][a-z0-9-]{2,48})', discover_html):
            if link not in _SKOOL_RESERVED:
                community_urls.add(f"https://www.skool.com/{link}")
    except Exception as e:
        print(f"    [WARN] Skool discover page failed: {e}")

    # Scrape each community page
    found = []
    for url in list(community_urls)[:15]:
        result = _scrape_skool_community(url)
        found.append(result)
        time.sleep(0.3)

    ig_count = sum(1 for f in found if f["username"])
    print(f"  [Skool] {len(found)} communities ({ig_count} with Instagram link)")
    return found


def _youtube_channel_about_url(video_html: str) -> str:
    """
    Extract the channel About page URL from a YouTube video page's embedded JSON.
    Instagram handles are almost always in the About/Links section, not the description.
    """
    # @handle form (most common for modern channels)
    m = re.search(r'"ownerUrls":\["(https://www\.youtube\.com/@[^"]+)"', video_html)
    if m:
        return m.group(1).rstrip("/") + "/about"
    m = re.search(r'youtube\.com/(@[A-Za-z0-9_.-]{2,50})"', video_html)
    if m:
        return f"https://www.youtube.com/{m.group(1)}/about"
    # Legacy channel ID form
    m = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{22})"', video_html)
    if m:
        return f"https://www.youtube.com/channel/{m.group(1)}/about"
    return ""


def _ig_from_html(html: str) -> str:
    """Return first Instagram username found in HTML, or ''."""
    hits = [
        m.lower() for m in re.findall(r'instagram\.com/([A-Za-z0-9_.]{2,30})', html)
        if m.lower() not in _IG_RESERVED
    ]
    return hits[0] if hits else ""


def discover_via_youtube(keywords: list) -> list:
    """
    SerpAPI YouTube search across 4 keywords.
    For each video: check video page first, then fall back to channel About page.
    Channel About pages reliably contain Instagram links even when descriptions don't.
    """
    print("  [YouTube] Searching YouTube videos...")
    found = []
    seen  = set()

    for kw in keywords[:4]:
        try:
            r = GoogleSearch({
                "q": f'"{kw}" site:youtube.com',
                "api_key": SERPAPI_KEY,
                "num": 5,
            }).get_dict()
            for hit in r.get("organic_results", []):
                link = hit.get("link", "")
                if "youtube.com/watch" not in link and "youtu.be/" not in link:
                    continue
                video_html = _fetch(link, timeout=15)
                if not video_html:
                    continue

                # First try: Instagram link anywhere in the video page source
                un = _ig_from_html(video_html)

                # Second try: fetch channel About page (links section)
                if not un:
                    about_url = _youtube_channel_about_url(video_html)
                    if about_url:
                        about_html = _fetch(about_url, timeout=15)
                        un = _ig_from_html(about_html)

                if un and un not in seen:
                    seen.add(un)
                    found.append(_make_account(un, "youtube"))
            time.sleep(0.4)
        except Exception as e:
            print(f"    [WARN] YouTube query failed: {e}")

    print(f"  [YouTube] {len(found)} raw handles")
    return found


# ── Step 1c — Merge all sources ────────────────────────────────────────────────

def discover_all(parsed: dict):
    """Run all five discovery sources in parallel, deduplicate, return results."""
    print("\n[Step 1] Running 5 discovery sources in parallel...")
    keywords = parsed["keywords"]
    niche    = parsed["niche"]
    source_fns = {
        "google":              lambda: discover_via_google(keywords, niche),
        "apify":               lambda: discover_via_apify(niche, keywords),
        "skool":               lambda: discover_via_skool(keywords),
        "youtube":             lambda: discover_via_youtube(keywords),
        "instagram_profiles":  lambda: discover_via_instagram_profiles(niche, keywords),
    }

    merged      = {}   # username -> account dict
    skool_manual = []  # Skool results where no Instagram handle was found

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fn): name for name, fn in source_fns.items()}
        for future in as_completed(futures):
            source_name = futures[future]
            try:
                results = future.result()
            except Exception as e:
                print(f"  [WARN] {source_name} crashed: {e}")
                continue
            for item in results:
                un = item["username"]
                if not un:
                    # Skool-only entry (no IG found) — VA fills manually
                    skool_manual.append(item)
                    continue
                if un in merged:
                    merged[un]["sources"] |= item["sources"]
                    if item.get("skool_url"):
                        merged[un]["skool_url"] = item["skool_url"]
                else:
                    merged[un] = dict(item)   # copy so we own it

    accounts = list(merged.values())
    n_raw = len(accounts) + len(skool_manual)
    print(f"\n  Raw total    : {n_raw}")
    print(f"  Unique users : {len(accounts)}")
    print(f"  Skool-manual : {len(skool_manual)} (no IG link found — VA fills)")
    return accounts, skool_manual


# ── Step 2 — Scrape profiles ───────────────────────────────────────────────────

def _avg_views(posts: list) -> str:
    """Average video view count across posts that have it."""
    views = []
    for p in posts:
        v = (p.get("videoViewCount") or p.get("videoPlayCount") or p.get("playCount"))
        if isinstance(v, (int, float)) and v > 0:
            views.append(v)
    return str(int(sum(views) / len(views))) if views else ""


def scrape_profiles(accounts: list) -> dict:
    """
    Batch-scrape all Instagram usernames via Apify (SCRAPE_BATCH per run).
    Returns {username: profile_dict}.
    """
    usernames = [a["username"] for a in accounts if a["username"]]
    if not usernames:
        return {}

    print(f"\n[Step 2] Scraping {len(usernames)} profiles in batches of {SCRAPE_BATCH}...")
    profiles = {}

    for batch_i, start in enumerate(range(0, len(usernames), SCRAPE_BATCH)):
        batch = usernames[start:start + SCRAPE_BATCH]
        print(f"  Batch {batch_i + 1} ({len(batch)} accounts)...")
        results = apify_run_and_wait(
            "apify~instagram-scraper",
            {
                "directUrls":   [f"https://www.instagram.com/{u}/" for u in batch],
                "resultsType":  "details",
                "resultsLimit": len(batch),
            },
            max_wait=300,
        )
        for p in results:
            un = (p.get("username") or "").lower()
            if not un:
                continue
            ext_urls = p.get("externalUrls") or []
            external = (ext_urls[0].get("url", "") if ext_urls and isinstance(ext_urls[0], dict)
                        else p.get("externalUrl", "") or "")
            profiles[un] = {
                "username":     un,
                "full_name":    p.get("fullName", ""),
                "bio":          p.get("biography", ""),
                "followers":    int(p.get("followersCount", 0) or 0),
                "private":      bool(p.get("private", False)),
                "external_url": external,
                "avg_views":    _avg_views(p.get("latestPosts", [])),
                "verified":     bool(p.get("verified", False)),
            }

    print(f"  Scraped {len(profiles)}/{len(usernames)} profiles")
    return profiles


# ── Step 3a — Stage 1: Hard rules (free, instant) ─────────────────────────────

def stage1_filter(accounts: list, profiles: dict):
    """Apply hard follower/bio/link rules. Returns (passed, rejected)."""
    print(f"\n[Step 3a] Stage 1 hard filter — {len(accounts)} accounts...")
    passed, rejected = [], []

    for acc in accounts:
        un = acc["username"]
        p  = profiles.get(un)

        if not p:
            rejected.append({**acc, "filter_reason": "scrape_failed"})
            continue

        followers  = p["followers"]
        bio_lower  = (p["bio"] or "").lower()
        domain     = _link_domain(p["external_url"])

        # Private account
        if p["private"]:
            rejected.append({**acc, **p, "filter_reason": "private_account"})
            continue

        # Follower band
        if followers < MIN_FOLLOWERS:
            rejected.append({**acc, **p, "filter_reason": f"too_few_followers ({followers:,})"})
            continue
        if followers > MAX_FOLLOWERS:
            rejected.append({**acc, **p, "filter_reason": f"too_many_followers ({followers:,})"})
            continue

        # Reject bio multi-word phrases first
        phrase_hit = next((ph for ph in REJECT_BIO_PHRASES if ph in bio_lower), None)
        if phrase_hit:
            rejected.append({**acc, **p, "filter_reason": f"bio: {phrase_hit}"})
            continue

        # Reject bio single words
        bio_words  = set(re.findall(r"\b\w+\b", bio_lower))
        word_hit   = next((w for w in REJECT_BIO_WORDS if w in bio_words), None)
        if word_hit:
            rejected.append({**acc, **p, "filter_reason": f"bio: {word_hit}"})
            continue

        # Reject link domains
        if any(d in domain for d in REJECT_LINK_DOMAINS):
            rejected.append({**acc, **p, "filter_reason": f"link: {domain}"})
            continue
        if domain.endswith(".myshopify.com"):
            rejected.append({**acc, **p, "filter_reason": f"shopify_store: {domain}"})
            continue

        # Average views filter
        avg_v = p.get("avg_views", "")
        if avg_v == "":
            # Apify returned no video posts — pass to Stage 2 for human review
            passed.append({**acc, **p, "filter_reason": "views_unknown"})
        elif int(avg_v) < MIN_AVG_VIEWS:
            rejected.append({**acc, **p, "filter_reason": f"low_avg_views ({int(avg_v):,})"})
        else:
            passed.append({**acc, **p})

    print(f"  Passed: {len(passed)}  |  Rejected: {len(rejected)}")
    return passed, rejected


# ── Step 3b — Stage 2: Claude Haiku bio filter ────────────────────────────────

def stage2_filter(accounts: list):
    """Batch Claude Haiku filter, 10 bios per API call. Returns (passed, rejected)."""
    if not accounts:
        return [], []

    print(f"\n[Step 3b] Stage 2 Claude Haiku filter — {len(accounts)} accounts...")
    passed, rejected = [], []
    BATCH = 10

    for batch_i, start in enumerate(range(0, len(accounts), BATCH)):
        batch = accounts[start:start + BATCH]

        numbered = ""
        for j, acc in enumerate(batch):
            numbered += (
                f"\n[{j+1}] @{acc['username']} "
                f"({acc.get('followers', 0):,} followers)\n"
                f"Bio: {(acc.get('bio') or '')[:300]}\n"
                f"Link: {acc.get('external_url', '')}\n"
            )

        prompt = f"""Filter these Instagram accounts for a B2B coaching lead list.

KEEP if the person:
1. Sells a coaching program, online course, mentorship, or paid community
2. Is a practitioner-turned-educator teaching others how to get a result
3. Fits one of these approved niches:
   business acquisition, wholesale real estate, land flipping, Section 8 rental investing,
   Amazon FBA, tech sales coaching, SMMA/agency building, UGC/TikTok affiliate coaching,
   home service business coaching

REJECT if they only sell physical products, are a service provider (not educator), or don't fit any approved niche.

Accounts:
{numbered}
Respond ONLY with a JSON array — one object per account in index order:
[{{"index": 1, "passes": true, "niche": "wholesale real estate", "reason": "teaches house wholesaling system"}}]"""

        try:
            msg = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            decisions = json.loads(_strip_json_fences(msg.content[0].text))
            if not isinstance(decisions, list):
                raise ValueError("Expected JSON array")
        except Exception as e:
            print(f"  [WARN] Claude batch {batch_i+1} failed ({e}) — flagging for review")
            for acc in batch:
                passed.append({**acc, "niche": "review_needed",
                                "filter_reason": f"claude_error: {e}"})
            continue

        n_pass = 0
        for j, acc in enumerate(batch):
            dec = next((d for d in decisions if d.get("index") == j + 1), None)
            if dec is None:
                # Claude skipped this account — pass it for manual review
                passed.append({**acc, "niche": "", "filter_reason": "claude_no_decision"})
                n_pass += 1
            elif dec.get("passes"):
                passed.append({**acc, "niche": dec.get("niche", ""),
                                "filter_reason": dec.get("reason", "")})
                n_pass += 1
            else:
                rejected.append({**acc, "niche": dec.get("niche", ""),
                                  "filter_reason": dec.get("reason", "")})

        print(f"  Batch {batch_i+1}: {n_pass}/{len(batch)} passed")

    print(f"  Stage 2 total — Passed: {len(passed)}  |  Rejected: {len(rejected)}")
    return passed, rejected


# ── Step 4 — Output CSV ────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "username", "instagram_url", "skool_url", "follower_count",
    "avg_views", "bio", "link_in_bio", "niche", "source", "filter_reason",
]


def _build_row(acc: dict) -> dict:
    un = acc.get("username", "")
    return {
        "username":      un,
        "instagram_url": f"https://www.instagram.com/{un}/" if un else "",
        "skool_url":     acc.get("skool_url", ""),
        "follower_count": acc.get("followers", ""),
        "avg_views":     acc.get("avg_views", ""),
        "bio":           (acc.get("bio") or "").replace("\n", " "),
        "link_in_bio":   acc.get("external_url", ""),
        "niche":         acc.get("niche", ""),
        "source":        ", ".join(sorted(acc.get("sources", set()))),
        "filter_reason": acc.get("filter_reason", "passed"),
    }


def write_csv(passed: list, skool_manual: list) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    path = PROJECT_DIR / f"output_sourcing_{ts}.csv"

    rows = [_build_row(a) for a in passed]
    for s in skool_manual:
        rows.append(_build_row({**s, "niche": "", "filter_reason": "skool_manual_fill"}))

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return str(path)


# ── Keyword expansion for second pass ─────────────────────────────────────────

def _generate_more_keywords(niche: str, used_keywords: list) -> list:
    """Ask Claude Haiku for 8 fresh keyword phrases not already tried."""
    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": f"""Generate 8 NEW search keyword phrases for finding "{niche}" Instagram coaches.
Do NOT repeat any of these already-used phrases: {used_keywords}
Use different angles: specific techniques, program names, income claims, audience types.
Return ONLY a JSON array of 8 strings: ["phrase1", "phrase2", ...]"""}],
    )
    try:
        kws = json.loads(_strip_json_fences(msg.content[0].text))
        return [k for k in kws if isinstance(k, str) and k not in used_keywords][:8]
    except Exception:
        return []


# ── Main ───────────────────────────────────────────────────────────────────────

MAX_PASSES = 3   # maximum discovery passes before giving up on target


def main():
    if len(sys.argv) < 2:
        print('Usage: python agent.py "find 50 wholesale real estate coaches"')
        print('       python agent.py "find 50 wholesale real estate coaches" --test')
        sys.exit(1)

    prompt    = sys.argv[1]
    test_mode = "--test" in sys.argv

    parsed       = parse_prompt(prompt)
    target_count = parsed["target_count"]

    # Accumulators across all passes
    all_discovered   = set()   # usernames seen in any discovery pass
    all_scraped      = {}      # username -> profile dict
    all_s2_passed    = []      # final ICP-approved accounts
    all_skool_manual = []      # Skool communities with no IG handle
    used_keywords    = list(parsed["keywords"])

    for pass_num in range(1, MAX_PASSES + 1):
        print(f"\n{'='*55}\n  PASS {pass_num}/{MAX_PASSES}"
              f"  (target: {target_count}, found so far: {len(all_s2_passed)})\n{'='*55}")

        # ── Step 1: Discover ───────────────────────────────────────────────────
        accounts, skool_manual = discover_all(parsed)
        all_skool_manual.extend(skool_manual)

        # Only process usernames not seen in a previous pass
        new_accounts = [a for a in accounts if a["username"] not in all_discovered]
        all_discovered.update(a["username"] for a in accounts)

        if not new_accounts:
            print("  No new accounts found — stopping early.")
            break

        if test_mode and pass_num == 1:
            new_accounts = new_accounts[:10]
            print(f"  [TEST MODE] Capped at 10 accounts")

        # ── Step 2: Scrape ─────────────────────────────────────────────────────
        new_profiles = scrape_profiles(new_accounts)
        all_scraped.update(new_profiles)

        # ── Step 3: Filter ─────────────────────────────────────────────────────
        s1_passed, _ = stage1_filter(new_accounts, new_profiles)
        s2_passed, _ = stage2_filter(s1_passed)
        all_s2_passed.extend(s2_passed)

        print(f"\n  Pass {pass_num} result: +{len(s2_passed)} accounts"
              f"  |  Running total: {len(all_s2_passed)}/{target_count}")

        if len(all_s2_passed) >= target_count:
            print("  Target reached!")
            break

        if pass_num < MAX_PASSES:
            new_kws = _generate_more_keywords(parsed["niche"], used_keywords)
            if not new_kws:
                print("  No new keywords available — stopping.")
                break
            used_keywords.extend(new_kws)
            parsed = {**parsed, "keywords": new_kws}
            print(f"  New keywords for pass {pass_num + 1}: {new_kws}")

    # ── Step 4: Output ─────────────────────────────────────────────────────────
    filename = write_csv(all_s2_passed, all_skool_manual if not test_mode else [])

    print(f"""
{'='*55}
  SOURCING COMPLETE
{'='*55}
  Passes run:     {pass_num}
  Unique scraped: {len(all_scraped)}
  ICP passed:     {len(all_s2_passed)}/{target_count}
  Skool manual:   {len(all_skool_manual)} (no IG found — VA fills manually)
  Output: {filename}
{'='*55}
""")


if __name__ == "__main__":
    main()
