#!/usr/bin/env python3
"""
Influx Lead Engine — Discovery / Scraper (v3)

Two sources:
  Source 1 — Google SerpAPI (site:instagram.com keyword search)
    Searches Google for indexed Instagram profiles matching niche phrases
    like "amazon fba course site:instagram.com". Finds established accounts
    whose bios/profiles contain coaching keywords — can't be done via
    Instagram's native search (which only matches usernames, not bio content).

  Source 2 — Similar accounts expansion
    Pulls relatedProfiles from every account that passes hard filters.
    Starts from hardcoded seeds + Source 1 passers. Runs up to 3 rounds,
    feeding each round's passers as seeds for the next.

Hard filters (applied to every account from both sources):
  • Followers 10K–100K
  • Last 8 posts averaging >=1,500 views  (video views; falls back to likes for photo posts)
  • Posted within last 45 days
  • Link in bio required

Output: raw_handles.csv
  Columns: handle, source, niche, followers, avg_views, last_post_date

Run:
  python scraper.py --niche "amazon fba" --target 10
  python scraper.py                                   # interactive mode
"""

import csv
import re
import sys
import os
import time
import argparse
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from serpapi import GoogleSearch

# ── .env must load before anything that touches os.environ ───────────────────
load_dotenv(Path(__file__).parent / ".env", override=True)

# Unicode safety on Windows cp1252 terminals (LESSONS.md)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── API Keys ──────────────────────────────────────────────────────────────────
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY", "")

# ── Output path ───────────────────────────────────────────────────────────────
OUTPUT_CSV = os.environ.get(
    "SCRAPER_OUTPUT_CSV",
    r"C:\Users\Owenb\Desktop\instagram-tool\raw_handles.csv",
)


# ═════════════════════════════════════════════════════════════════════════════
# NICHE CONFIG
# ═════════════════════════════════════════════════════════════════════════════

# Niche → Google site:instagram.com search phrases.
# These are phrases a coach would use in their bio or profile.
# Google can match against bio text; Instagram's own search cannot.
NICHE_QUERIES: dict[str, list[str]] = {
    "amazon fba": [
        "amazon fba coach",
        "amazon fba course",
        "amazon fba mentor",
        "amazon seller course",
        "amazon fba program",
    ],
    "real estate wholesaling": [
        "real estate wholesaling course",
        "wholesale real estate coach",
        "real estate wholesaling mentor",
        "how to wholesale real estate",
        "real estate wholesaling program",
    ],
    "smma": [
        "smma coach",
        "social media marketing agency course",
        "smma mentor",
        "agency owner coaching",
        "lead gen agency mentor",
    ],
    "tech sales": [
        "tech sales coach",
        "sdr coaching program",
        "break into tech sales",
        "tech sales mentor",
        "saas sales coaching",
    ],
    "home service business": [
        "cleaning business coach",
        "home service business mentor",
        "pressure washing business coaching",
        "lawn care business course",
        "home service entrepreneur coach",
    ],
}

# Regex to pull Instagram handles from URLs and text
_IG_HANDLE_RE = re.compile(
    r"(?:instagram\.com/|@)([A-Za-z0-9_.]{3,30})"
    r"(?!/(?:p|reel|stories|explore|tv|reels)/)"
)
_IG_SKIP_SEGMENTS = {
    "instagram", "reels", "explore", "accounts", "p",
    "reel", "stories", "tv", "highlights",
}

# Known good accounts to seed the similar-accounts expansion.
# Add any niche as a new key — empty list means expansion starts purely from Source 1 passers.
NICHE_SEEDS: dict[str, list[str]] = {
    "amazon fba": [
        "tabare_sotomayor", "travismarziani", "amazonfbalikeaboss",
        "camronjamesfba", "theultimatefba", "mike.j.elliott",
        "briannoonanofficial", "abuv_thepar", "fbaboys",
        "_patland", "privatelabelmasters", "fbajayden",
    ],
    "real estate wholesaling": [],
    "smma":                    [],
    "tech sales":              [],
    "home service business":   [],
}

# Bio must contain at least one of these keywords for an account to be used as
# an expansion seed. Prevents off-niche accounts (nurses, doctors, etc.) that
# pass hard filters from poisoning the relatedProfiles pool in later rounds.
# Leave empty list to skip keyword gating for that niche.
NICHE_EXPANSION_KEYWORDS: dict[str, list[str]] = {
    "amazon fba":             ["fba", "amazon", "seller", "ecommerce", "e-commerce", "wholesale", "resell"],
    "real estate wholesaling": ["wholesale", "real estate", "rei", "property", "flip"],
    "smma":                    ["smma", "agency", "marketing", "lead gen", "ads"],
    "tech sales":              ["tech sales", "sdr", "saas", "sales rep", "bdr"],
    "home service business":   ["cleaning", "lawn", "pressure wash", "home service", "landscaping"],
}

# ── Hard filter constants ─────────────────────────────────────────────────────
MIN_FOLLOWERS     = 10_000
MAX_FOLLOWERS     = 100_000
MIN_AVG_VIEWS     = 1_500
MAX_POST_AGE_DAYS = 45
POSTS_TO_CHECK    = 8       # last N posts for the views average
SCRAPE_BATCH_SIZE = 20      # profiles per Apify run
MAX_EXPANSION_ROUNDS = 3    # similar-account expansion rounds

# Platform / brand handles to always skip
_SKIP_HANDLES = {
    "instagram", "facebook", "tiktok", "youtube", "twitter", "linkedin",
    "amazon", "shopify", "skool", "kajabi", "clickfunnels", "gohighlevel",
    "forbes", "entrepreneur",
}


# ═════════════════════════════════════════════════════════════════════════════
# APIFY HELPER  (poll every 5 s, same pattern as pipeline.py — LESSONS.md)
# ═════════════════════════════════════════════════════════════════════════════

def _apify_run(actor_id: str, input_data: dict, max_wait: int = 180) -> list:
    """Start an Apify actor run, poll until done, return dataset items."""
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    try:
        resp = requests.post(
            run_url, params={"token": APIFY_TOKEN}, json=input_data, timeout=30,
        )
    except requests.RequestException as e:
        print(f"  [Apify] Request error: {e}")
        return []

    if resp.status_code == 402:
        print("  [Apify] HTTP 402 — out of Apify credits. Top up at apify.com/billing.")
        return []
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        print(f"  [Apify] HTTP {resp.status_code}: {e}")
        return []

    run_id = resp.json()["data"]["id"]
    print(f"    -> run {run_id}")

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    status = "RUNNING"
    for _ in range(max_wait // 5):
        time.sleep(5)
        try:
            st = requests.get(status_url, params={"token": APIFY_TOKEN}, timeout=30)
            status = st.json()["data"]["status"]
        except Exception:
            continue
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        print(f"    ... {status}")

    if status != "SUCCEEDED":
        print(f"    X Run ended: {status}")
        return []

    items = requests.get(
        f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
        params={"token": APIFY_TOKEN}, timeout=60,
    )
    items.raise_for_status()
    return items.json()


# ═════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — INSTAGRAM SEARCH SCRAPER
# ═════════════════════════════════════════════════════════════════════════════

def _google_search_handles(queries: list[str], results_per_query: int = 10) -> list[str]:
    """
    Source 1: Google SerpAPI site:instagram.com searches.

    Instagram's native search only matches usernames — not bio content.
    Google indexes full Instagram profile pages, so a query like
    "amazon fba course site:instagram.com" surfaces profiles whose bio,
    username, or linked pages contain those words.

    Returns a deduplicated list of handles extracted from result URLs + snippets.
    """
    if not SERPAPI_KEY:
        print("  [!] SERPAPI_KEY not set — skipping Source 1")
        return []

    handles: list[str] = []
    seen: set[str] = set()

    for phrase in queries:
        query = f'{phrase} site:instagram.com'
        print(f"  [Google] {query!r}")
        try:
            results = GoogleSearch({"q": query, "num": results_per_query, "api_key": SERPAPI_KEY}).get_dict()
        except Exception as e:
            print(f"    [!] SerpAPI error: {e}")
            time.sleep(1)
            continue

        new_this = 0
        for hit in results.get("organic_results", []):
            # Extract handles from the result URL and surrounding text
            text = hit.get("link", "") + " " + hit.get("snippet", "") + " " + hit.get("title", "")
            for m in _IG_HANDLE_RE.finditer(text):
                h = m.group(1).lower().strip("._")
                if h and len(h) >= 3 and h not in _IG_SKIP_SEGMENTS \
                        and h not in _SKIP_HANDLES and h not in seen:
                    seen.add(h)
                    handles.append(h)
                    new_this += 1

        print(f"    -> {len(results.get('organic_results', []))} hits, {new_this} new handles")
        time.sleep(0.5)

    return handles


# ═════════════════════════════════════════════════════════════════════════════
# PROFILE SCRAPER  (used by both sources)
# ═════════════════════════════════════════════════════════════════════════════

def _scrape_profiles(handles: list[str]) -> dict:
    """
    Batch-scrape Instagram profiles with apify~instagram-scraper.
    resultsType=details returns biography, followersCount, latestPosts,
    and relatedProfiles in one call (LESSONS.md).
    Returns {username: raw_profile_dict}.
    """
    all_profiles: dict = {}
    total_batches = max(1, (len(handles) + SCRAPE_BATCH_SIZE - 1) // SCRAPE_BATCH_SIZE)

    for i in range(0, len(handles), SCRAPE_BATCH_SIZE):
        batch = handles[i : i + SCRAPE_BATCH_SIZE]
        batch_num = i // SCRAPE_BATCH_SIZE + 1
        print(f"  [Scrape] Batch {batch_num}/{total_batches} ({len(batch)} profiles)...")
        urls = [f"https://www.instagram.com/{h}/" for h in batch]
        results = _apify_run(
            "apify~instagram-scraper",
            {"directUrls": urls, "resultsType": "details", "resultsLimit": len(urls)},
            max_wait=300,
        )
        for p in results:
            username = (p.get("username") or "").lower()
            if username:
                all_profiles[username] = p
        print(f"    -> {len(results)} profiles returned")
        time.sleep(2)

    return all_profiles


# ═════════════════════════════════════════════════════════════════════════════
# HARD FILTERS
# ═════════════════════════════════════════════════════════════════════════════

def _calc_avg_views(profile: dict) -> float:
    """
    Average engagement across the last POSTS_TO_CHECK posts.
    For video/reel posts: uses videoViewCount.
    For photo/carousel posts: falls back to likesCount (same threshold still applies —
    1,500 likes on a photo is equivalent engagement signal to 1,500 reel views).
    """
    posts = (profile.get("latestPosts") or [])[:POSTS_TO_CHECK]
    if not posts:
        return 0.0
    total = sum(
        int(
            p.get("videoViewCount")
            or p.get("videoPlayCount")
            or p.get("playCount")
            or p.get("likesCount")   # photo/carousel fallback
            or 0
        )
        for p in posts
    )
    return total / len(posts)


def _get_last_post_date(profile: dict) -> str:
    posts = profile.get("latestPosts") or []
    return posts[0].get("timestamp", "") if posts else ""


def _posted_within(profile: dict) -> bool:
    ts_str = _get_last_post_date(profile)
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt > datetime.now(timezone.utc) - timedelta(days=MAX_POST_AGE_DAYS)
    except Exception:
        return True  # unparseable → let through


def _get_website(profile: dict) -> str:
    urls = profile.get("externalUrls") or []
    if urls and isinstance(urls, list):
        first = urls[0]
        return first.get("url", "") if isinstance(first, dict) else str(first)
    return profile.get("externalUrl", "") or profile.get("website", "") or ""


def _apply_hard_filters(profiles: dict, source: str, niche: str) -> list[dict]:
    """
    Apply the four hard gates to a batch of raw profile dicts.
    Returns enriched dicts for accounts that pass. Prints a drop breakdown.
    """
    passing: list[dict] = []
    drops: dict[str, int] = {
        "followers too low":  0,
        "followers too high": 0,
        "views too low":      0,
        "no link in bio":     0,
        "stale (>45 days)":   0,
    }

    for username, p in profiles.items():
        followers = int(p.get("followersCount") or 0)
        website   = _get_website(p)
        avg_views = _calc_avg_views(p)

        if followers < MIN_FOLLOWERS:
            drops["followers too low"] += 1
            continue
        if followers > MAX_FOLLOWERS:
            drops["followers too high"] += 1
            continue
        if avg_views < MIN_AVG_VIEWS:
            drops["views too low"] += 1
            continue
        if not website:
            drops["no link in bio"] += 1
            continue
        if not _posted_within(p):
            drops["stale (>45 days)"] += 1
            continue

        passing.append({
            "handle":         username,
            "source":         source,
            "niche":          niche,
            "followers":      followers,
            "avg_views":      round(avg_views, 1),
            "last_post_date": _get_last_post_date(p),
            "bio":            p.get("biography") or "",
            "website":        website,
            "display_name":   p.get("fullName") or "",
            # keep raw profile for similar-account extraction
            "_raw":           p,
        })

    total_dropped = sum(drops.values())
    print(f"  Filters: {len(profiles)} in → {len(passing)} passed, {total_dropped} dropped")
    for reason, count in drops.items():
        if count:
            print(f"    {count:>4}x  {reason}")

    return passing


# ═════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — SIMILAR ACCOUNTS EXPANSION  (3-step cascade per account)
# ═════════════════════════════════════════════════════════════════════════════

def _parse_username(entry) -> str:
    """Extract a clean lowercase username from a dict, list-node, or bare string."""
    if isinstance(entry, dict):
        raw = (
            entry.get("username")
            or (entry.get("node") or {}).get("username")
            or ""
        )
    elif isinstance(entry, str):
        raw = entry
    else:
        return ""
    return raw.lower().strip("._")


def _valid_new(h: str, seen: set[str]) -> bool:
    return bool(h) and len(h) >= 3 and h not in _SKIP_HANDLES and h not in seen


def _get_expansion_handles(
    accounts: list[dict], seen: set[str], bio_keywords: list[str] | None = None
) -> list[str]:
    """
    For each account, find new similar handles using a 3-step cascade:

    Step 1 — relatedProfiles from the existing Apify scrape (free, already fetched).
    Step 2 — apify~instagram-profile-scraper, which may return richer suggested
              accounts data than the generic scraper.
    Step 3 — Followers list fallback: scrape up to 300 followers, keep those with
              10K–100K followers — these are likely peer educators in the same niche.

    If `bio_keywords` is provided, an account's bio must contain at least one keyword
    before it is used as an expansion seed. This prevents off-niche accounts that pass
    hard filters (e.g. nurses, doctors) from poisoning later expansion rounds.

    Returns new handles not already in `seen`.
    """
    all_new: list[str] = []

    for acct in accounts:
        # ── Bio keyword gate (stops off-niche drift across rounds) ────────────
        if bio_keywords:
            bio = (acct.get("bio") or "").lower()
            if not any(kw in bio for kw in bio_keywords):
                continue
        handle = acct["handle"]
        raw    = acct.get("_raw", {})
        found  = 0

        # ── Step 1: relatedProfiles from existing scrape ──────────────────────
        related = (
            raw.get("relatedProfiles")
            or raw.get("suggestedProfiles")
            or raw.get("edge_related_profiles", {}).get("edges", [])
            or []
        )
        for entry in related:
            h = _parse_username(entry)
            if _valid_new(h, seen):
                seen.add(h)
                all_new.append(h)
                found += 1

        if found:
            print(f"    @{handle}: {found} from relatedProfiles")
            continue

        # ── Step 2: apify~instagram-profile-scraper ───────────────────────────
        print(f"    @{handle}: relatedProfiles empty — trying profile scraper...")
        ps_items = _apify_run(
            "apify~instagram-profile-scraper",
            {"usernames": [handle]},
            max_wait=120,
        )
        for p in ps_items:
            for field in ("relatedProfiles", "suggestedProfiles", "related", "suggested"):
                for entry in (p.get(field) or []):
                    h = _parse_username(entry)
                    if _valid_new(h, seen):
                        seen.add(h)
                        all_new.append(h)
                        found += 1

        if found:
            print(f"    @{handle}: {found} from profile scraper")
            continue

        # ── Step 3: Followers list fallback ───────────────────────────────────
        # Scrape followers list; the next expansion round's _apply_hard_filters
        # will cull by follower count, views, etc.
        # Actor: scraping_solutions~instagram-scraper-followers-following-no-cookies
        # Items return 'username' field (no follower count at this stage).
        print(f"    @{handle}: no suggestions — scraping followers list...")
        follower_items = _apify_run(
            "scraping_solutions~instagram-scraper-followers-following-no-cookies",
            {
                "Account":    [handle],
                "scrapeType": "followers",
                "maxItems":   300,
            },
            max_wait=240,
        )
        for f in follower_items:
            h = (f.get("username") or "").lower().strip("._")
            if _valid_new(h, seen):
                seen.add(h)
                all_new.append(h)
                found += 1

        print(f"    @{handle}: {found} peer handles from {len(follower_items)} followers scraped")

    return all_new


# ═════════════════════════════════════════════════════════════════════════════
# MAIN DISCOVERY RUNNER
# ═════════════════════════════════════════════════════════════════════════════

def discover(niche: str, target: int = 50) -> list[dict]:
    """
    Run full discovery for one niche. Returns all accounts that passed hard filters.
    """
    niche_key = niche.lower().strip()

    # Resolve niche config — exact match first, then substring
    queries = NICHE_QUERIES.get(niche_key)
    if not queries:
        for k, v in NICHE_QUERIES.items():
            if niche_key in k or k in niche_key:
                queries = v
                niche_key = k
                break
    if not queries:
        queries = [
            f"{niche} coach",
            f"{niche} mentor",
            f"{niche} course",
        ]

    seeds = [h.lstrip("@").lower() for h in NICHE_SEEDS.get(niche_key, [])]
    expansion_keywords = NICHE_EXPANSION_KEYWORDS.get(niche_key, [])

    print(f"\n{'='*64}")
    print(f"  Scraper v3 — niche: {niche!r}")
    print(f"  S1 queries: {', '.join(repr(q) for q in queries)}")
    print(f"  Seeds:    {', '.join('@' + s for s in seeds) or '(none)'}")
    print(f"  Target:   {target}")
    print(f"{'='*64}")

    seen: set[str] = set(seeds)
    all_passing: list[dict] = []

    # ── Source 1: Google site:instagram.com keyword search ───────────────────
    print(f"\n[Source 1/2] Google site:instagram.com search...")
    s1_handles = _google_search_handles(queries, results_per_query=10)
    seen.update(s1_handles)
    s1_passing: list[dict] = []
    if s1_handles:
        print(f"  Scraping {len(s1_handles)} Source 1 handles...")
        s1_profiles = _scrape_profiles(s1_handles)
        s1_passing = _apply_hard_filters(s1_profiles, source="google_search", niche=niche)
        all_passing.extend(s1_passing)
        print(f"  Source 1: {len(s1_passing)}/{len(s1_handles)} accounts passed hard filters")
    else:
        print("  Source 1: no handles found")

    # ── Source 2: Iterative similar-account expansion ─────────────────────────
    print(f"\n[Source 2/2] Similar accounts expansion (up to {MAX_EXPANSION_ROUNDS} rounds)...")

    # Scrape hardcoded seeds (always, even if Source 1 found nothing)
    if seeds:
        print(f"\n  Scraping {len(seeds)} hardcoded seeds...")
        seed_raw = _scrape_profiles(seeds)
        seed_passing = _apply_hard_filters(seed_raw, source="seed", niche=niche)
        all_passing.extend(seed_passing)
        print(f"  Seeds: {len(seed_passing)}/{len(seeds)} passed hard filters")
        # Build wrapped list for similar-account extraction — includes ALL seeds,
        # not just those that passed filters, because even a "stale" seed still
        # carries useful relatedProfiles pointing to active accounts in the niche.
        seeds_for_expansion = [{"handle": u, "_raw": p} for u, p in seed_raw.items()]
    else:
        seed_passing = []
        seeds_for_expansion = []

    # Round 1 source: all scraped seeds (pass or fail) + any S1 passers
    # This is the critical fix — seeds feed relatedProfiles even if they failed filters.
    # Seeds are manually curated — bypass keyword gate so they always contribute
    # their relatedProfiles regardless of bio content.
    initial_handles = _get_expansion_handles(seeds_for_expansion + s1_passing, seen, bio_keywords=None)
    print(f"\n  Initial similar-account pool: {len(initial_handles)} handles")

    current_handles = initial_handles
    for round_num in range(1, MAX_EXPANSION_ROUNDS + 1):
        if not current_handles:
            print(f"  [Round {round_num}] No similar handles to process — stopping.")
            break

        print(f"\n  [Round {round_num}/{MAX_EXPANSION_ROUNDS}] Scraping {len(current_handles)} similar accounts...")
        round_raw = _scrape_profiles(current_handles)
        round_passing = _apply_hard_filters(
            round_raw, source=f"similar_r{round_num}", niche=niche
        )
        all_passing.extend(round_passing)
        print(f"  [Round {round_num}] {len(round_passing)} accounts passed filters")

        if len(all_passing) >= target:
            print(f"  Target of {target} reached — stopping expansion.")
            break

        # Feed this round's passers as seeds for the next round
        current_handles = _get_expansion_handles(round_passing, seen, expansion_keywords)
        print(f"  [Round {round_num}] Extracted {len(current_handles)} new handles for next round")

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen_h: set[str] = set()
    deduped: list[dict] = []
    for r in all_passing:
        h = r["handle"]
        if h not in seen_h:
            seen_h.add(h)
            deduped.append(r)

    return deduped


# ═════════════════════════════════════════════════════════════════════════════
# OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def _print_table(rows: list[dict]) -> None:
    """Print a formatted table of all accounts that passed hard filters."""
    if not rows:
        print("\n  No accounts passed hard filters.")
        return

    # Sort by followers descending
    rows_sorted = sorted(rows, key=lambda r: r["followers"], reverse=True)

    col_handle   = max(len(r["handle"]) for r in rows_sorted)
    col_handle   = max(col_handle, 6)   # min width = len("handle")

    header = (
        f"  {'handle':<{col_handle}}  {'followers':>10}  {'avg_views':>10}"
        f"  {'source':<16}  last_post_date"
    )
    divider = "  " + "-" * (len(header) - 2)

    print(f"\n{'='*64}")
    print(f"  {len(rows_sorted)} accounts passed hard filters")
    print(f"{'='*64}")
    print(header)
    print(divider)
    for r in rows_sorted:
        date_str = (r.get("last_post_date") or "")[:10]
        print(
            f"  {r['handle']:<{col_handle}}"
            f"  {r['followers']:>10,}"
            f"  {r['avg_views']:>10,.0f}"
            f"  {r['source']:<16}"
            f"  {date_str}"
        )
    print(divider)


def save_handles(rows: list[dict], path: str, niche: str) -> None:
    """Write accounts that passed hard filters to CSV. Deduplicates by handle."""
    fieldnames = ["handle", "source", "niche", "followers", "avg_views", "last_post_date"]
    seen_h: set[str] = set()
    unique: list[dict] = []
    for r in rows:
        h = r.get("handle", "").lower().strip()
        if h and h not in seen_h:
            seen_h.add(h)
            unique.append({
                "handle":         h,
                "source":         r.get("source", ""),
                "niche":          r.get("niche") or niche,
                "followers":      r.get("followers", ""),
                "avg_views":      r.get("avg_views", ""),
                "last_post_date": r.get("last_post_date", ""),
            })

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    print(f"\n  Saved {len(unique)} handles → {path}")


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Influx scraper v3 — Instagram search + similar accounts discovery"
    )
    parser.add_argument("--niche",  type=str, default="",         help="Niche to search (e.g. 'amazon fba')")
    parser.add_argument("--target", type=int, default=50,         help="Target number of passing accounts")
    parser.add_argument("--output", type=str, default=OUTPUT_CSV, help="Output CSV path")
    args = parser.parse_args()

    niche = args.niche.strip()
    if not niche:
        print("Available niches:")
        for n in NICHE_QUERIES:
            print(f"  • {n}")
        print()
        niche = input("Enter niche (or type your own): ").strip()
        if not niche:
            print("No niche entered — exiting.")
            return

    results = discover(niche, target=args.target)

    _print_table(results)

    if results:
        save_handles(results, args.output, niche)
        print(f"\nDone. Next step: python filter.py --input {args.output}")
    else:
        print("\nNo accounts passed hard filters.")


if __name__ == "__main__":
    main()
