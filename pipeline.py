#!/usr/bin/env python3
"""
Instagram Lead Research Pipeline

Steps:
  1. Read Instagram handles/URLs from CSV
  2. Scrape Instagram profile (name, bio, website) via Apify
  3. Scrape linked website via Apify Website Content Crawler
  4. Use Claude to identify full name + niche
  5. Use SerpAPI to find LinkedIn profile URL
  6. Use Claude to verify LinkedIn match
  7. Use Apollo.io to get email + phone
  8. Output results to CSV

Run in test mode (1 handle):  python pipeline.py
Run on full list:             python pipeline.py --full
"""

import csv
import json
import time
import re
import sys
import os
import requests
import anthropic
from serpapi import GoogleSearch
from urllib.parse import urlparse
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project directory (safe even if already loaded by app.py)
load_dotenv(Path(__file__).parent / ".env", override=False)

# Force UTF-8 output so emoji/accented chars in bios don't crash the terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# -- API Keys ------------------------------------------------------------------
APIFY_TOKEN  = os.environ.get("APIFY_TOKEN",  "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY",  "")
APOLLO_KEY   = os.environ.get("APOLLO_KEY",   "")
HUNTER_KEY   = os.environ.get("HUNTER_KEY",   "")

# Permanent webhook server URL (deployed on Railway).
# If set, the pipeline POSTs Apollo phone-reveal callbacks here and polls
# /result/<person_id> for the result — no per-request webhook.site tokens needed.
# Leave blank to fall back to webhook.site automatically.
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")

# -- File Paths (overridable via env so the dashboard can inject temp paths) ---
INPUT_CSV  = os.environ.get("PIPELINE_INPUT_CSV",  r"C:\Users\Owenb\Desktop\instagram-tool\Instagram Tool - Scraper Sheet 2 - Outreach.csv")
OUTPUT_CSV = os.environ.get("PIPELINE_OUTPUT_CSV", r"C:\Users\Owenb\Desktop\instagram-tool\output.csv")

# -- Claude client (uses ANTHROPIC_API_KEY env var) -----------------------------
claude = anthropic.Anthropic()


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def extract_username(handle: str) -> str:
    """Extract bare username from an Instagram URL or raw handle."""
    handle = handle.strip().rstrip("/")
    if "instagram.com/" in handle:
        return handle.split("instagram.com/")[-1].rstrip("/")
    return handle


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences from a JSON string."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ------------------------------------------------------------------------------
# Step 1 - Apify runner
# ------------------------------------------------------------------------------

def apify_run_and_wait(actor_id: str, input_data: dict, max_wait: int = 180) -> list:
    """
    Start an Apify actor run, poll until finished, return dataset items.
    max_wait: seconds before giving up polling.
    """
    run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    resp = requests.post(
        run_url,
        params={"token": APIFY_TOKEN},
        json=input_data,
        timeout=30,
    )
    resp.raise_for_status()
    run_id = resp.json()["data"]["id"]
    print(f"    -> Apify run started: {run_id}")

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    for _ in range(max_wait // 5):
        time.sleep(5)
        st = requests.get(status_url, params={"token": APIFY_TOKEN}, timeout=30)
        st.raise_for_status()
        status = st.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        print(f"    ... {status}")

    if status != "SUCCEEDED":
        print(f"    X Run ended with status: {status}")
        return []

    items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items"
    items = requests.get(items_url, params={"token": APIFY_TOKEN}, timeout=30)
    items.raise_for_status()
    return items.json()


# ------------------------------------------------------------------------------
# Step 2 - Instagram profile scrape
# ------------------------------------------------------------------------------

def _first_url(profile: dict) -> str:
    """Extract the first external URL from an Apify Instagram profile result."""
    # externalUrls is a list of dicts with 'url' key; externalUrl is a plain string
    urls = profile.get("externalUrls", [])
    if urls and isinstance(urls, list):
        return urls[0].get("url", "") if isinstance(urls[0], dict) else str(urls[0])
    return profile.get("externalUrl", "") or profile.get("website", "")


def scrape_instagram_profile(handle_url: str) -> dict:
    """Return display_name, bio, website, username from Apify Instagram scraper."""
    username = extract_username(handle_url)
    profile_url = f"https://www.instagram.com/{username}/"
    print(f"  [Instagram] Scraping: {profile_url}")

    results = apify_run_and_wait(
        "apify~instagram-scraper",
        {
            "directUrls": [profile_url],
            "resultsType": "details",
            "resultsLimit": 1,
        },
    )

    if not results:
        return {}

    p = results[0]
    return {
        "username":     p.get("username", username),
        "display_name": p.get("fullName", ""),
        "bio":          p.get("biography", ""),
        "website":      _first_url(p),
        "profile_pic_url": p.get("profilePicUrl", ""),
    }


# ------------------------------------------------------------------------------
# Step 3 - Website content crawl
# ------------------------------------------------------------------------------

def scrape_website(url: str) -> str:
    """Return concatenated text content from the website (up to 5 pages)."""
    print(f"  [Website] Crawling: {url}")
    results = apify_run_and_wait(
        "apify~website-content-crawler",
        {
            "startUrls": [{"url": url}],
            "maxCrawlPages": 5,
            "maxCrawlDepth": 1,
            "crawlerType": "cheerio",
        },
        max_wait=240,
    )

    if not results:
        return ""

    chunks = []
    for page in results[:5]:
        text = page.get("text", "") or page.get("markdown", "") or ""
        if text:
            chunks.append(text[:2000])
    return "\n\n---\n\n".join(chunks)


# ------------------------------------------------------------------------------
# Step 3b - Website email scrape (contact/about pages, zero API credits)
# ------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
_EMAIL_SKIP_TERMS = ("noreply", "no-reply", "@example", "test@", "@sentry", "@sampleemail")


def scrape_domain_for_email(domain: str) -> str:
    """
    Crawl /contact, /about, /contact-us on the domain in one Apify run.
    Returns the first real email address found, or ''.
    """
    if not domain:
        return ""
    print(f"  [WebEmail] Scraping contact/about pages for {domain}...")
    results = apify_run_and_wait(
        "apify~website-content-crawler",
        {
            "startUrls": [
                {"url": f"https://{domain}/contact"},
                {"url": f"https://{domain}/about"},
                {"url": f"https://{domain}/contact-us"},
            ],
            "maxCrawlPages": 3,
            "maxCrawlDepth": 0,
            "crawlerType": "cheerio",
        },
        max_wait=120,
    )
    for page in results:
        text = page.get("text", "") or page.get("markdown", "") or ""
        for email in _EMAIL_RE.findall(text):
            if not any(skip in email.lower() for skip in _EMAIL_SKIP_TERMS):
                print(f"    [WebEmail] Found: {email} on {page.get('url', '?')}")
                return email
    print("    [WebEmail] No email found on contact/about pages")
    return ""


# ------------------------------------------------------------------------------
# Step 4 - Claude: identify niche + full name
# ------------------------------------------------------------------------------

def identify_niche_and_name(display_name: str, bio: str, website_content: str) -> dict:
    """Return {full_name, niche, confidence} using Claude."""
    print("  [Claude] Identifying niche and full name...")

    prompt = f"""Analyze this Instagram profile and (optionally) website content to identify:
1. The person's full name (first and last name)
2. Their specific business niche (e.g. "Amazon FBA", "Real estate investing", "Lead gen agency",
   "E-commerce dropshipping", "Social media marketing", "Crypto trading", "Coaching", etc.)

Instagram Display Name: {display_name}
Instagram Bio: {bio}
Website Content (may be empty):
{website_content[:3000] if website_content else "(none)"}

Respond ONLY with valid JSON -- no extra text:
{{"full_name": "...", "niche": "...", "confidence": "high|medium|low"}}"""

    msg = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    text = strip_json_fences(msg.content[0].text)
    return json.loads(text)


# ------------------------------------------------------------------------------
# Step 5 - LinkedIn: 5-signal scoring system
#
# Signals (in order of reliability):
#   S1 Domain match  (50 pts) -- website domain found via LinkedIn search
#   S2 Brand match   (30 pts) -- brand/company keyword in LinkedIn title
#   S3 Bio keywords  (0-15)   -- niche/bio keyword overlap in LinkedIn title
#   S5 Username slug (20 pts) -- Instagram username matches LinkedIn slug
#   S6 Niche/bio     (15 pts) -- niche keyword in slug or bio keyword in headline
#   S_name           (0-10)   -- first or last name in slug (baseline)
#
# Accept if top score >= MIN_SCORE.
# ------------------------------------------------------------------------------

# ── weights ────────────────────────────────────────────────────────────────────
W_DOMAIN   = 50
W_BRAND    = 30
W_KEYWORDS = 15   # max; 3 pts per matching keyword
W_USERNAME = 20
W_NAME     = 10
MIN_SCORE  = 10   # reject below this

# ── generic platforms that don't reveal a person's brand ──────────────────────
_LINK_AGGREGATORS = {
    "linktr.ee","linktree.com","beacons.ai","taap.it","direct.me","linkpop.com","lnk.to",
    "bio.fm","campsite.bio","tap.bio","solo.to","linktw.in","skool.com",
    "youtube.com","youtu.be","m.youtube.com","webinarjam.com","samcart.com",
    "clickfunnels.com","kajabi.com","teachable.com","gohighlevel.com",
    "kartra.com","thinkific.com","mysamcart.com",
    # link aggregators / hosted profile pages
    "stan.store","urlgenius.com","urlgeni.us","affiliateautomated.com",
    "taplink.cc","later.com","bio.site","koji.to","carrd.co",
}

# ── first-name nickname expansions ────────────────────────────────────────────
_NICKNAMES = {
    "anthony":{"tony","ant"}, "robert":{"rob","bob"},
    "james":{"jim","jimmy","jake"}, "william":{"will","bill"},
    "michael":{"mike","mick"}, "richard":{"rick","rich"},
    "joseph":{"joe"}, "thomas":{"tom","tommy"},
    "christopher":{"chris"}, "matthew":{"matt"},
    "jacob":{"jake"}, "nicholas":{"nick"},
    "andrew":{"andy","drew"}, "daniel":{"dan","danny"},
    "benjamin":{"ben"}, "samuel":{"sam"},
    "alexander":{"alex"}, "joshua":{"josh"},
    "jonathan":{"jon"}, "timothy":{"tim"},
    "stephen":{"steve"}, "patricia":{"pat"},
    "katherine":{"kate","kathy"}, "elizabeth":{"liz","beth"},
}

_STOPWORDS = {
    "that","this","with","from","have","been","will","your","their","help",
    "make","more","also","just","into","over","than","then","when","what",
    "each","know","some","they","them","about","there","would","could",
    "people","learn","want","need","time","life","work","show","take",
    "made","only","like","very","even","most","such","down","using","built",
    "using","start","free","online","money","business","coach","coaching",
}


def _normalize_li_url(url: str) -> str:
    """Canonicalise LinkedIn URL: strip country subdomain and trailing /xx path."""
    url = re.sub(r"https?://[a-z]{2}\.linkedin\.com/", "https://www.linkedin.com/", url)
    url = re.sub(r"/[a-z]{2}$", "", url.rstrip("/"))
    return url


def _extract_root_domain(url: str) -> str:
    """Return root domain (e.g. 'yourfirstoffer.com'), '' for aggregators."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.netloc.lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        parts = host.split(".")
        # Handle country second-level domains (co.uk, com.au etc.)
        if len(parts) >= 3 and parts[-2] in ("co","com","net","org","gov"):
            root = ".".join(parts[-3:])
        elif len(parts) >= 2:
            root = ".".join(parts[-2:])
        else:
            root = host
        return "" if root in _LINK_AGGREGATORS else root
    except Exception:
        return ""


def _name_variants(full_name: str) -> set:
    """Return all recognisable slug fragments for a full name."""
    parts = full_name.lower().split()
    variants = set(parts)
    if parts:
        first = parts[0]
        variants.add(first[:4])
        for formal, nicks in _NICKNAMES.items():
            if first == formal or first in nicks:
                variants |= nicks | {formal}
    return variants


def _serp_candidates(query: str, n: int = 5) -> list:
    """Return list of (normalized_url, title) for LinkedIn /in/ hits."""
    r = GoogleSearch({"q": query, "api_key": SERPAPI_KEY, "num": n}).get_dict()
    out = []
    for hit in r.get("organic_results", []):
        link = hit.get("link", "")
        if "linkedin.com/in/" in link:
            out.append((_normalize_li_url(link), hit.get("title", "")))
    return out


def _score_candidate(url, title, domain_hit,
                     ig_username, full_name, niche, bio, domain, brand_kws):
    """Score one candidate. Returns (total_score, breakdown_dict)."""
    slug       = url.split("linkedin.com/in/")[-1].rstrip("/").lower()
    title_low  = title.lower()
    bd         = {}
    score      = 0

    # S_name -- first + last name in slug (computed early; used by S1 gate)
    variants   = _name_variants(full_name)
    name_parts = full_name.lower().split()
    has_first  = any(v in slug for v in variants if len(v) >= 3)
    has_last   = len(name_parts) > 1 and (name_parts[-1] in slug or name_parts[-1][:4] in slug)
    if has_first or has_last:
        sn = W_NAME
    else:
        sn = 0
    bd["S_name"] = sn;  score += sn

    # S1 -- Domain match (only awarded if candidate passes basic name check)
    # Prevents a random co-worker from winning purely on domain hit.
    s1 = W_DOMAIN if (domain_hit and (has_first or has_last)) else 0
    bd["S1_domain"] = s1;  score += s1

    # S5 -- Username slug match
    ig_clean   = re.sub(r"[._\-]", "", ig_username.lower())
    slug_clean = re.sub(r"[._\-]", "", slug)
    slug_nonum = re.sub(r"\d+$", "", slug_clean)
    if ig_clean in (slug_clean, slug_nonum):
        s5 = W_USERNAME
    elif len(ig_clean) >= 4 and (ig_clean in slug_clean or slug_clean in ig_clean):
        s5 = W_USERNAME // 2
    else:
        s5 = 0
    bd["S5_username"] = s5;  score += s5

    # S2 -- Brand/company name in LinkedIn title
    s2 = 0
    title_alphanum = re.sub(r"[^a-z0-9]", "", title_low)
    for kw in brand_kws:
        kw_clean = re.sub(r"[^a-z0-9]", "", kw)
        if len(kw_clean) >= 4 and kw_clean in title_alphanum:
            s2 = W_BRAND
            break
    if not s2 and domain:
        d_slug = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        if len(d_slug) >= 4 and d_slug in title_alphanum:
            s2 = W_BRAND
    bd["S2_brand"] = s2;  score += s2

    # S3 -- Bio/niche keyword overlap in title (3 pts per match, max 15)
    kw_pool = (set(re.findall(r"\b[a-z]{4,}\b", niche.lower())) |
               set(re.findall(r"\b[a-z]{4,}\b", bio.lower()))) - _STOPWORDS
    matches = sum(1 for kw in kw_pool if kw in title_low)
    s3 = min(matches * 3, W_KEYWORDS)
    bd["S3_keywords(x{})".format(matches)] = s3;  score += s3

    # S6 -- Niche keyword appears in LinkedIn slug, OR bio keyword in headline
    niche_words = set(re.findall(r"\b[a-z]{4,}\b", niche.lower())) - _STOPWORDS
    bio_words   = set(re.findall(r"\b[a-z]{4,}\b", bio.lower())) - _STOPWORDS
    s6 = 15 if (any(w in slug for w in niche_words) or
                any(w in title_low for w in bio_words)) else 0
    bd["S6_niche"] = s6;  score += s6

    # Industry mismatch penalty (-30): clear sector collision
    niche_low = niche.lower()
    _SECTOR_CONFLICTS = [
        ({"real estate","wholesale","flipping","rental"},
         {"boeing","aerospace","pharma","pharmaceutical","espresso","coffee shop",
          "nightclub","energy utility","plumbing","dental","nursing"}),
        ({"amazon fba","ecommerce","dropshipping"},
         {"teacher","nurse","hospital","nonprofit","government","municipality"}),
        ({"agency","smma","marketing agency"},
         {"pharma","aerospace","nursing","plumbing"}),
    ]
    penalty = 0
    for niche_tags, bad_terms in _SECTOR_CONFLICTS:
        if any(tag in niche_low for tag in niche_tags):
            if any(bad in title_low for bad in bad_terms):
                penalty = -30
                break

    # Auto-reject: entrepreneurial niche + corporate employee title with no
    # entrepreneurial signals in the headline.
    _ENTREPRENEUR_NICHES = {
        "real estate","entrepreneur","coach","coaching","agency","ecommerce",
        "amazon fba","dropshipping","flipping","wholesale","investing","investor",
        "smma","digital products","online business",
    }
    _CORPORATE_TITLES = {
        "engineer","analyst","specialist","coordinator","administrator",
        "associate","technician","developer","programmer","scientist",
        "accountant","auditor","compliance","paralegal","intern","resident",
        "manager","director",   # only counts as corporate when paired with no-signal
    }
    _ENTREPRENEUR_SIGNALS = {
        "founder","owner","ceo","entrepreneur","investor","coach","partner",
        "president","principal","self-employed","freelance","consultant",
    }
    if any(tag in niche_low for tag in _ENTREPRENEUR_NICHES):
        has_corp_title = any(ct in title_low for ct in _CORPORATE_TITLES)
        has_entre_signal = any(es in title_low for es in _ENTREPRENEUR_SIGNALS)
        if has_corp_title and not has_entre_signal:
            penalty = min(penalty, -30)   # take the worse of the two penalties

    bd["S_mismatch_penalty"] = penalty;  score += penalty

    return score, bd


def _follow_redirect(url: str) -> str:
    """Follow HTTP redirects and return the final URL (best-effort)."""
    try:
        r = requests.get(url, allow_redirects=True, timeout=10, stream=True)
        r.close()
        return r.url
    except Exception:
        return url


def find_and_select_linkedin(ig_username: str, full_name: str, niche: str,
                             bio: str, website: str = "") -> str:
    """
    5-signal LinkedIn matching with transparent per-candidate scoring.
    Returns a normalised LinkedIn URL, or '' if nothing meets MIN_SCORE.
    """
    print(f"  [LinkedIn] Searching for {full_name!r} (@{ig_username}) ...")

    domain     = _extract_root_domain(website)

    # If the website is a link aggregator (domain empty), try following the redirect
    # to discover the person's real website domain.
    if not domain and website:
        real_url = _follow_redirect(website)
        if real_url != website:
            real_domain = _extract_root_domain(real_url)
            if real_domain:
                print(f"    [Website] Followed redirect: {website} → {real_url}  (domain: {real_domain})")
                domain = real_domain

    # If the domain doesn't contain the IG username, it's a hosted platform page —
    # skip domain signals so we don't match platform employees instead of the creator.
    if domain:
        ig_slug     = re.sub(r"[._\-]", "", ig_username.lower())
        domain_slug = re.sub(r"[^a-z0-9]", "", domain.lower())
        if ig_slug not in domain_slug and domain_slug not in ig_slug:
            print(f"    [Website] {domain!r} doesn't contain @{ig_username} — treating as hosted platform, skipping domain signals")
            domain = ""

    brand_kws  = set(re.findall(r"@(\w+)", bio))          # @mentions in bio
    if domain:
        brand_kws.add(domain.split(".")[0].lower())         # domain slug
        brand_kws.add(re.sub(r"[^a-z0-9]","",domain.split(".")[0].lower()))

    # ── collect candidates ─────────────────────────────────────────────────────
    # url -> {title, domain_hit}
    candidates: dict = {}

    def _add(hits, domain_hit=False):
        for url, title in hits:
            if url not in candidates:
                candidates[url] = {"title": title, "domain_hit": domain_hit}
            elif domain_hit:
                candidates[url]["domain_hit"] = True   # upgrade

    def _search(query, n=5, domain_hit=False):
        hits = _serp_candidates(query, n)
        print(f"    [Search] {query!r} → {len(hits)} hit(s)")
        _add(hits, domain_hit=domain_hit)

    # S5 source: username search (with and without niche)
    _search(f"{ig_username} {niche} linkedin", 3)
    _search(f"{ig_username} linkedin", 3)
    # Name + niche searches
    _search(f'"{full_name}" {niche} linkedin', 5)
    _search(f"{full_name} {niche} linkedin", 3)
    # Name + role keywords (catches founders/coaches with non-obvious niches)
    name_parts_q = full_name.split()
    if len(name_parts_q) >= 2:
        _search(f'"{full_name}" founder OR coach OR entrepreneur linkedin', 5)

    # S1 source: domain-specific searches (Signal 1)
    if domain:
        brand_slug = domain.split(".")[0]
        _search(f"{domain} {niche} linkedin", 5, domain_hit=True)
        _search(f'"{brand_slug}" {niche} linkedin', 3, domain_hit=True)

    if not candidates:
        print("    No candidates found")
        return "", []

    # ── score every candidate ──────────────────────────────────────────────────
    scored = {}
    for url, meta in candidates.items():
        score, bd = _score_candidate(
            url, meta["title"], meta["domain_hit"],
            ig_username, full_name, niche, bio, domain, brand_kws,
        )
        scored[url] = {"score": score, "breakdown": bd, "title": meta["title"]}

    # ── print scoreboard ───────────────────────────────────────────────────────
    ranked = sorted(scored.items(), key=lambda x: -x[1]["score"])
    print(f"\n    {'SCORE':>5}  CANDIDATE")
    print(f"    {'-----':>5}  ---------")
    for url, data in ranked:
        bd_str = "  ".join(f"{k}={v}" for k, v in data["breakdown"].items())
        print(f"    {data['score']:>5}  {url}")
        print(f"           {data['title']}")
        print(f"           {bd_str}")

    best_url, best_data = ranked[0]
    best_score = best_data["score"]

    if best_score < MIN_SCORE:
        print(f"\n    REJECTED — top score {best_score} < MIN_SCORE {MIN_SCORE}")
        # ── Retry: broaden queries before giving up ────────────────────────────
        name_parts = full_name.split()
        retry_qs   = []
        if len(name_parts) >= 2:
            retry_qs.append(f'"{name_parts[0]} {name_parts[-1]}" {niche} linkedin')
        retry_qs.append(f"{full_name} {niche} linkedin")
        print("    [Retry] Broadening search...")
        for rq in retry_qs:
            for rurl, rtitle in _serp_candidates(rq, 5):
                if rurl in scored:
                    continue   # already evaluated above
                rscore, rbd = _score_candidate(
                    rurl, rtitle, False,
                    ig_username, full_name, niche, bio, domain, brand_kws,
                )
                rbd_str = "  ".join(f"{k}={v}" for k, v in rbd.items())
                print(f"    [Retry] {rscore:>4}  {rurl}")
                print(f"            {rtitle}")
                print(f"            {rbd_str}")
                if rscore >= MIN_SCORE:
                    print(f"    [Retry] Accepted: {rurl}")
                    return rurl, []
            time.sleep(0.3)
        return "", []

    # ── Detect ties ────────────────────────────────────────────────────────────
    tied_urls = [url for url, data in ranked if data["score"] == best_score]
    if len(tied_urls) > 1:
        print(f"\n    TIE — {len(tied_urls)} candidates share top score {best_score}:")
        for t in tied_urls:
            print(f"      {t}")
        print(f"    Will attempt Apollo on first; flagging for manual review.")
    else:
        tied_urls = []   # no tie — leave empty so caller knows

    print(f"\n    SELECTED ({best_score} pts): {best_url}")
    return best_url, tied_urls


# ------------------------------------------------------------------------------
# Step 5b - Unverified LinkedIn search (low-confidence / single-name accounts)
# ------------------------------------------------------------------------------

def find_linkedin_unverified(ig_username: str, first_name: str, niche: str,
                              bio: str, website: str = "") -> tuple[str, list]:
    """
    Lightweight LinkedIn search for accounts where Claude couldn't extract a
    reliable full name.  Runs up to 4 targeted queries in order and takes the
    first result, flagging it as [UNVERIFIED].

    Returns ("[UNVERIFIED] url", deduplicated_candidate_list) or ("", []).
    """
    print(f"  [LinkedIn-Unverified] Searching for @{ig_username} ({first_name!r})...")

    domain = _extract_root_domain(website)
    if not domain and website:
        real_url = _follow_redirect(website)
        if real_url != website:
            domain = _extract_root_domain(real_url)

    bio_mentions = re.findall(r"@(\w+)", bio)  # brand @mentions in bio

    seen: set = set()
    all_hits: list = []  # (url, title) ordered by discovery

    def _try(query: str):
        hits = _serp_candidates(query, 3)
        print(f"    [Search] {query!r} → {len(hits)} hit(s)")
        for url, title in hits:
            if url not in seen:
                seen.add(url)
                all_hits.append((url, title))

    # 1. Username + niche
    _try(f"{ig_username} {niche} linkedin")
    # 2. First name + niche
    _try(f"{first_name} {niche} linkedin")
    # 3. Brand @mentions from bio
    for mention in bio_mentions[:2]:
        if mention.lower() != ig_username.lower():
            _try(f"{mention} {niche} linkedin")
            time.sleep(0.3)
    # 4. Website domain
    if domain:
        _try(f"{domain} linkedin")

    if not all_hits:
        print("    [Unverified] No candidates found")
        return "", []

    best_url = all_hits[0][0]
    all_candidate_urls = [u for u, _ in all_hits]
    print(f"    [Unverified] Taking first result: {best_url}")
    return f"[UNVERIFIED] {best_url}", all_candidate_urls


# ------------------------------------------------------------------------------
# Step 6b - Hunter.io: email from website domain
# ------------------------------------------------------------------------------

def get_hunter_email(domain: str) -> str:
    """Search Hunter.io Domain Search for the best email on the given domain."""
    if not domain or not HUNTER_KEY:
        return ""
    print(f"  [Hunter] Searching domain: {domain}")
    try:
        resp = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"    [Hunter] {resp.status_code}: {resp.text[:200]}")
            return ""
        emails = resp.json().get("data", {}).get("emails", [])
        if not emails:
            print("    [Hunter] No emails found")
            return ""
        best = max(emails, key=lambda e: e.get("confidence", 0))
        email = best.get("value", "")
        print(f"    [Hunter] Found: {email} (confidence={best.get('confidence')})")
        return email
    except Exception as e:
        print(f"    [Hunter] Error: {e}")
        return ""


# ------------------------------------------------------------------------------
# Step 7 - Apollo.io: get email + phone
# ------------------------------------------------------------------------------

def _best_phone(phone_list: list) -> str:
    """Pick the best sanitized phone number from Apollo's phone_numbers array."""
    if not phone_list:
        return ""
    mobile = next((p for p in phone_list
                   if p.get("type_cd") == "mobile" and p.get("status_cd") == "valid_number"), None)
    valid  = next((p for p in phone_list if p.get("status_cd") == "valid_number"), None)
    best   = mobile or valid or phone_list[0]
    return best.get("sanitized_number", "")


# ── Webhook backend selection ──────────────────────────────────────────────────
#
# Two modes, chosen automatically:
#
#   PERMANENT (Railway)  — WEBHOOK_URL is set in .env
#     • Sends Apollo callbacks to  {WEBHOOK_URL}/webhook
#     • Polls               {WEBHOOK_URL}/result/<person_id>
#     • No per-request token creation needed; server is always-on
#
#   FALLBACK (webhook.site) — WEBHOOK_URL not set
#     • Creates a fresh per-request webhook.site token
#     • Polls the webhook.site token request log
#     • Works out of the box with no infrastructure
# ──────────────────────────────────────────────────────────────────────────────

def _webhook_site_create() -> tuple[str, str]:
    """Create a fresh webhook.site token. Returns (post_url, poll_url)."""
    r = requests.post(
        "https://webhook.site/token",
        headers={"Content-Type": "application/json"},
        json={"default_status": 200, "default_content": "OK",
              "default_content_type": "text/plain"},
        timeout=15,
    )
    r.raise_for_status()
    uuid = r.json()["uuid"]
    return f"https://webhook.site/{uuid}", f"https://webhook.site/token/{uuid}/requests"


def _poll_permanent(person_id: str, timeout: int = 240) -> str:
    """Poll our Railway server for a phone result keyed by Apollo person_id."""
    url      = f"{WEBHOOK_BASE_URL}/result/{person_id}"
    deadline = time.time() + timeout
    interval = 6
    while time.time() < deadline:
        time.sleep(interval)
        try:
            r = requests.get(url, timeout=10)
            d = r.json()
            if d.get("found"):
                return d.get("phone", "")
        except Exception:
            pass
        interval = min(interval + 4, 15)
    return ""


def _poll_webhook_site(check_url: str, timeout: int = 240) -> str:
    """Poll webhook.site token log until Apollo posts a result."""
    deadline = time.time() + timeout
    interval = 8
    while time.time() < deadline:
        time.sleep(interval)
        try:
            r = requests.get(check_url, headers={"Accept": "application/json"}, timeout=10)
            for item in r.json().get("data", []):
                try:
                    payload = json.loads(item.get("content", "{}"))
                    for person in payload.get("people", []):
                        phone = _best_phone(person.get("phone_numbers", []))
                        if phone:
                            return phone
                        if person.get("phone_numbers") is not None:
                            return ""   # Apollo responded — no number available
                except Exception:
                    pass
        except Exception:
            pass
        interval = min(interval + 4, 20)
    return ""


def get_apollo_contact(linkedin_url: str, full_name: str = "", phone_only: bool = False) -> dict:
    """
    Return {email, phone} from Apollo.io.

    Step 1 — email:  POST /v1/people/match   (synchronous) — skipped if phone_only=True
    Step 2 — phone:  POST /v1/people/enrich  (async webhook reveal)
      • If WEBHOOK_URL is set → use permanent Railway server
      • Otherwise             → use a per-request webhook.site token
    """
    linkedin_url = _normalize_li_url(linkedin_url)
    print(f"  [Apollo] Looking up: {linkedin_url}{' (phone only)' if phone_only else ''}")

    h = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "x-api-key": APOLLO_KEY,
    }

    # ── Step 1: email ─────────────────────────────────────────────────────────
    email     = ""
    person_id = ""
    if not phone_only:
        resp = requests.post(
            "https://api.apollo.io/v1/people/match",
            headers=h,
            json={"linkedin_url": linkedin_url, "reveal_personal_emails": True},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            person    = resp.json().get("person") or {}
            person_id = person.get("id", "")
            email     = person.get("email", "")
            if not email:
                personal = person.get("personal_emails") or []
                email = personal[0] if personal else ""
        else:
            print(f"    X Apollo match {resp.status_code}: {resp.text[:200]}")

        print(f"    [Apollo] email={email or '-'}")

    # ── Step 2: phone via enrich + webhook reveal ─────────────────────────────
    phone = ""
    try:
        if WEBHOOK_BASE_URL:
            # ── Permanent Railway server ──────────────────────────────────────
            webhook_post_url = f"{WEBHOOK_BASE_URL}/webhook"
            mode = f"permanent ({WEBHOOK_BASE_URL})"
        else:
            # ── Fallback: create a per-request webhook.site token ─────────────
            webhook_post_url, _wh_check_url = _webhook_site_create()
            mode = "webhook.site"

        print(f"    [Apollo] Triggering phone reveal ({mode})...")

        enrich = requests.post(
            "https://api.apollo.io/v1/people/enrich",
            headers=h,
            json={
                "linkedin_url": linkedin_url,
                "reveal_personal_emails": False,
                "reveal_phone_number": True,
                "webhook_url": webhook_post_url,
            },
            timeout=30,
        )

        if enrich.status_code in (200, 201):
            # Capture person_id from enrich response if we didn't get it from match
            if not person_id:
                person_id = (enrich.json().get("person") or {}).get("id", "")

            print(f"    [Apollo] Polling for phone (up to 240 s, person_id={person_id})...")

            if WEBHOOK_BASE_URL and person_id:
                phone = _poll_permanent(person_id, timeout=240)
            else:
                phone = _poll_webhook_site(_wh_check_url, timeout=240)

            if phone:
                print(f"    [Apollo] Phone received!")
            else:
                print("    [Apollo] No phone returned within timeout")
        else:
            print(f"    [Apollo] Enrich {enrich.status_code}: {enrich.text[:200]}")

    except Exception as e:
        print(f"    [Apollo] Phone reveal error: {e}")

    print(f"    email={email or '-'}  phone={phone or '-'}")
    return {"email": email, "phone": phone}


# ------------------------------------------------------------------------------
# Full pipeline for one handle
# ------------------------------------------------------------------------------

def process_handle(handle: str) -> dict:
    result = {
        "handle":               handle,
        "full_name":            "",
        "niche":                "",
        "linkedin_url":         "",
        "linkedin_candidates":  "",
        "needs_review":         "",
        "email":                "",
        "email_source":         "",
        "phone":                "",
        "error":                "",
    }

    print(f"\n{'='*64}")
    print(f"  Handle: {handle}")
    print(f"{'='*64}")

    try:
        # -- 1. Instagram ----------------------------------------------
        profile = scrape_instagram_profile(handle)
        if not profile:
            result["error"] = "Failed to scrape Instagram profile"
            return result

        display_name = profile["display_name"]
        bio          = profile["bio"]
        website      = profile["website"]
        print(f"    name={display_name!r}  website={website!r}")
        print(f"    bio={bio[:120]!r}")

        # -- 2. Website ------------------------------------------------
        website_content = ""
        if website:
            website_content = scrape_website(website)
            print(f"    website content: {len(website_content)} chars")
        else:
            print("    (no linked website)")

        # -- 3. Claude: niche + name -----------------------------------
        id_data    = identify_niche_and_name(display_name, bio, website_content)
        full_name  = id_data.get("full_name", display_name)
        niche      = id_data.get("niche", "Unknown")
        confidence = id_data.get("confidence", "?")
        print(f"    -> full_name={full_name!r}  niche={niche!r}  confidence={confidence}")

        result["full_name"] = full_name
        result["niche"]     = niche

        # -- Confidence flag: single-word name or low confidence → search harder --
        name_parts_check = full_name.strip().split()
        is_uncertain     = confidence == "low" or len(name_parts_check) < 2

        # -- 4+5. LinkedIn -----------------------------------------------
        ig_username = profile["username"]
        all_linkedin_candidates: list = []

        if is_uncertain:
            reason = "single-word name" if len(name_parts_check) < 2 else "low confidence"
            print(f"    ~ Uncertain name ({reason}) — using unverified LinkedIn search")
            first_name = name_parts_check[0] if name_parts_check else full_name
            linkedin_url, all_linkedin_candidates = find_linkedin_unverified(
                ig_username, first_name, niche, bio, website=website,
            )
            if not linkedin_url:
                print("    X No LinkedIn candidates found via unverified search")
                result["error"]        = "LinkedIn URL not found"
                result["needs_review"] = "TRUE"
                result["email_source"] = "manual_needed"
                result["phone"]        = "Manual lookup needed | No LinkedIn candidates found"
                return result
            result["linkedin_url"]        = linkedin_url
            result["linkedin_candidates"] = " | ".join(all_linkedin_candidates[:5])
            result["needs_review"]        = "TRUE"
        else:
            linkedin_url, tied_urls = find_and_select_linkedin(
                ig_username, full_name, niche, bio, website=website,
            )
            if not linkedin_url:
                print("    X No confirmed LinkedIn URL found")
                result["error"] = "LinkedIn URL not found"
                return result
            result["linkedin_url"] = linkedin_url
            if tied_urls:
                result["linkedin_candidates"] = " | ".join(tied_urls)
                result["needs_review"]        = "TRUE"
                all_linkedin_candidates       = tied_urls

        # Strip [UNVERIFIED] prefix before passing to Apollo
        apollo_url = linkedin_url[len("[UNVERIFIED] "):] if linkedin_url.startswith("[UNVERIFIED] ") else linkedin_url

        # -- 6. Email: website scrape → Hunter → Apollo ------------------
        email        = ""
        email_source = ""
        ig_domain    = _extract_root_domain(website)

        if ig_domain:
            email = scrape_domain_for_email(ig_domain)
            if email:
                email_source = "website_scrape"

        if not email and ig_domain:
            email = get_hunter_email(ig_domain)
            if email:
                email_source = "hunter"

        # -- 7. Apollo: phone (+ email if not already found) ------------
        contact = get_apollo_contact(apollo_url, full_name=full_name,
                                     phone_only=bool(email))
        if not email and contact["email"]:
            email        = contact["email"]
            email_source = "apollo"

        result["email"]        = email
        result["email_source"] = email_source or "manual_needed"
        result["phone"]        = contact["phone"]

        # -- Change 3: helpful phone message when manual review needed ---
        if result["needs_review"] == "TRUE" and not result["phone"]:
            candidates_str = result["linkedin_candidates"] or result["linkedin_url"]
            result["phone"] = f"Manual lookup needed | Candidates: {candidates_str}"

    except Exception as exc:
        result["error"] = str(exc)
        print(f"    X ERROR: {exc}")

    return result


# ------------------------------------------------------------------------------
# Phone reveal retry pass
# ------------------------------------------------------------------------------

def _retry_phone_reveals(results: list) -> bool:
    """
    After the main loop, retry phone reveals for any row that has an email
    but no phone.  Returns True if any phones were recovered.
    """
    fieldnames = ["handle", "full_name", "niche", "linkedin_url", "linkedin_candidates", "needs_review", "email", "email_source", "phone", "error"]
    need_retry = [
        r for r in results
        if r.get("email") and not r.get("phone") and r.get("linkedin_url")
    ]
    if not need_retry:
        print("\n[Phone retry] All phones resolved — nothing to retry.")
        return False

    print(f"\n{'='*64}")
    print(f"[Phone retry] {len(need_retry)} row(s) have email but no phone — retrying (240 s each)...")
    print(f"{'='*64}")

    changed = 0
    for r in need_retry:
        print(f"\n  Retrying: {r['handle']} | {r['linkedin_url']}")
        contact = get_apollo_contact(r["linkedin_url"], full_name=r.get("full_name", ""))
        if contact["phone"]:
            r["phone"] = contact["phone"]
            changed += 1

    if changed:
        print(f"\n[Phone retry] Recovered {changed} phone(s). Rewriting {OUTPUT_CSV}...")
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print("[Phone retry] Done.")
    else:
        print("\n[Phone retry] No additional phones recovered.")
    return bool(changed)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

def _test_apollo_phones():
    """Quick test: hit Apollo for known LinkedIn URLs, including phone webhook reveal."""
    mode = f"permanent webhook ({WEBHOOK_BASE_URL})" if WEBHOOK_BASE_URL else "webhook.site fallback"
    print(f"Webhook mode: {mode}\n")
    test_urls = [
        ("https://www.linkedin.com/in/robuilt",              "Rob Abasolo"),
        ("https://www.linkedin.com/in/theo-clarke-2a7128200","Theo Clarke"),
    ]
    for url, name in test_urls:
        print(f"\n{'='*60}")
        print(f"  Testing Apollo for: {url}")
        print(f"{'='*60}")
        result = get_apollo_contact(url, full_name=name)
        print(f"  => email={result['email'] or '-'}  phone={result['phone'] or '-'}")


def main():
    if "--test-apollo" in sys.argv:
        _test_apollo_phones()
        return

    # Retest mode: run only specific handles
    if "--retest" in sys.argv:
        handles = [
            "philgrahamofficial",
            "chasecommerce",
            "the_alex_db",
            "jeroagency",
        ]
        delay_between = True
        print(f"RETEST MODE -- processing {len(handles)} specific handles\n")
    else:
        test_mode = "--full" not in sys.argv

        # Load handles
        handles = []
        with open(INPUT_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                h = row.get("handle", "").strip()
                if h:
                    handles.append(h)

        print(f"Loaded {len(handles)} handles from CSV")

        if test_mode:
            handles = handles[:1]
            delay_between = False
            print(f"TEST MODE -- processing 1 handle: {handles[0]}\n")
        else:
            delay_between = True
            print(f"FULL MODE -- processing all {len(handles)} handles\n")

    fieldnames = ["handle", "full_name", "niche", "linkedin_url", "linkedin_candidates", "needs_review", "email", "email_source", "phone", "error"]
    results = []

    # Open CSV at start and flush each row immediately so the dashboard can
    # display live results without waiting for the full run to complete.
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()
        out_f.flush()

        for i, handle in enumerate(handles, 1):
            print(f"\n[{i}/{len(handles)}]")
            result = process_handle(handle)
            results.append(result)
            writer.writerow(result)
            out_f.flush()
            if delay_between and i < len(handles):
                time.sleep(2)   # polite delay between handles

    print(f"\n{'='*64}")
    print(f"Done!  Results -> {OUTPUT_CSV}")
    print(f"Processed {len(results)} handle(s)")
    for r in results:
        status = "OK" if not r["error"] else f"X {r['error']}"
        print(f"  {r['handle'][:50]}  {status}")

    _retry_phone_reveals(results)


if __name__ == "__main__":
    main()
