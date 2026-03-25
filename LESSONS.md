# Influx Lead Engine — Lessons Learned

Hard-won knowledge from building this project. Read before touching anything.

---

## Railway Deployment

**Use waitress, not gunicorn.**
Python 3.14 dropped the `cgi` module that gunicorn depends on. waitress is the correct WSGI server for this stack. Start command: `waitress-serve --host=0.0.0.0 --port=$PORT webhook_server:app`

**Always read PORT from the environment.**
Railway injects `$PORT` dynamically — it is not always 8080. Never hardcode the port. Read it with `int(os.environ.get("PORT", 8080))`.

**Set PORT=8080 as a Railway environment variable.**
Without this set explicitly in Railway's env vars, the service may not bind correctly on first boot.

**Point the domain to port 8080.**
When configuring a custom domain or Railway-generated domain, make sure it routes to port 8080. Railway's UI has a separate "Domain" and "Port" setting — they must match.

**Auto-deploy works once GitHub is connected, but the first deploy needs a manual trigger.**
After connecting the GitHub repo in Railway, the first build does not fire automatically. Hit "Deploy" manually once, then all future pushes to `master` trigger auto-deploys.

**Health check endpoint must return JSON at `/health`.**
Railway's health check hits `/health` on an interval. Return `{"status": "ok"}` with HTTP 200. If the endpoint is missing or returns non-200, Railway will restart the service in a loop.

---

## Apollo Phones

**`reveal_phone_number` requires a real HTTPS webhook URL — local servers don't work.**
Apollo's phone reveal is asynchronous. Apollo POSTs the result to your webhook URL. `localhost` or `127.0.0.1` URLs will silently fail because Apollo cannot reach your machine. The Railway webhook server is the production solution.

**webhook.site works for testing but rate-limits at scale.**
Good for debugging a single reveal. Not suitable for batch runs — hits rate limits fast and URLs expire.

**Each phone reveal costs 8 mobile credits.**
Budget accordingly. Check remaining credits at apollo.io before large batch runs.

**Timeout should be 240 seconds, not 90.**
Apollo's phone reveal can take up to 3 minutes (180s). Use a 240s timeout with polling every 5–10 seconds to give it room to breathe. 90s causes false "no phone found" failures.

**Always do a retry pass for empty phones after the main run finishes.**
Some reveals arrive after the pipeline has already moved on. After the full list completes, re-run Apollo on every row that still has an empty phone field.

**Phone numbers live at `person.contact.phone_numbers`, not `person.phone_numbers`.**
Apollo's response schema nests contact data under `person.contact`. Going directly to `person.phone_numbers` returns nothing.

---

## LinkedIn Matching

**Never use the `site:linkedin.com` search operator.**
It's too restrictive — Google's index of LinkedIn is incomplete and throttled. Search without it and filter results by URL pattern instead.

**Always search: `[name] [niche keyword] linkedin` — exactly how a human would.**
This is the most reliable form. Keyword narrows to the right person when names are common. Example: `"Sarah Johnson" fitness coach linkedin`

**`MIN_SCORE` should be 10, not 20.**
A score of 20 is too strict and misses genuine matches. 10 catches them while still filtering out junk. Tune this if false positives increase.

**Relax name matching — first OR last name in the LinkedIn slug is enough.**
Full-name slug matches are rare. If either the first or last name appears in the slug, treat it as a signal, not a requirement.

**Blacklist platform domains or they match platform employees, not creators.**
Domains like `stan.store`, `linktr.ee`, `skool.com`, `beacons.ai`, `bio.link` appear on creator profiles but are platform companies. Without a blacklist, LinkedIn searches find LinkedIn employees at those platforms, not the creator.

**When scores tie, surface both LinkedIn URLs and set `needs_review=TRUE` instead of guessing.**
A tie means genuine ambiguity. Flag it for human review rather than picking one arbitrarily and poisoning the outreach list.

**Photo comparison via Claude Vision is blocked by Instagram's `robots.txt` — don't implement it.**
Instagram returns 403 for most direct image fetches. Spent time on this approach; it does not work reliably. Abandon it.

**Username-as-LinkedIn-slug (signal S5) is one of the strongest match signals — always try it.**
Creators who have a personal brand often register LinkedIn with the same handle as their Instagram username. This is a high-confidence match when it hits.

---

## Apify

**Instagram scraper actor ID:** `apify~instagram-scraper`
**Website crawler actor ID:** `apify~website-content-crawler`

**Use `resultsType: details`, not `profiles`.**
`profiles` returns a shallow summary. `details` returns the full bio, external URL, and post data needed for name/niche extraction.

**Poll every 5 seconds with a 180-second max wait.**
Apify runs are async. Polling faster than 5s burns API quota. 180s is enough for most runs; longer runs are usually stalled and should be retried.

**HTTP 402 means out of Apify credits — top up at apify.com/billing.**
The error is not a code bug. Log it clearly so it's obvious what happened rather than appearing as a data failure.

---

## Streamlit Dashboard

**Always use `override=True` when calling `load_dotenv`.**
Without it, system-level environment variables silently win over `.env` values on Windows. This causes confusion where changing `.env` has no effect.

```python
load_dotenv(_ENV_PATH, override=True)
```

**API keys must live in `.env`, never in the code.**
Hardcoded keys in source files get committed to GitHub. `.env` is gitignored. When deploying to Railway, set keys as Railway environment variables — they override `.env` automatically.

**Use a password-protected Admin Settings expander to hide keys from VAs.**
The dashboard is shared with the VA. The Admin Settings section (behind `ADMIN_PASSWORD`) shows API key status. The `DASHBOARD_PASSWORD` is the VA-facing login — these are two separate passwords on purpose.

**The Stop button must use `psutil` to kill the entire process tree.**
`pipeline.py` spawns child processes (Apify polls, Apollo calls). Setting a flag or calling `proc.terminate()` only kills the parent. The children keep running. `psutil.Process(pid).children(recursive=True)` gets them all.

**Logo images over ~100KB cause a `MemoryError` in base64 encoding within Streamlit.**
Resize and compress the logo to under 20KB before embedding. Use `PIL` or an online compressor. The `influx_logo.png` in this repo is already optimised.

**Column names are case-sensitive — handle both `Handle` and `handle`.**
CSVs exported from different tools use different capitalisation. Always do a case-insensitive column lookup when reading input CSVs, or normalise headers to lowercase on load.

---

## General Python / Windows

**Always use `python-dotenv` with `override=True` on Windows.**
Windows system environment variables can shadow `.env` values. `override=True` ensures `.env` is authoritative.

**Unicode characters in `print()` cause `cp1252` encoding errors on Windows.**
Instagram bios contain emoji, accents, and non-Latin scripts. Fix at the top of any script that prints to stdout:
```python
sys.stdout.reconfigure(encoding="utf-8")
```
Or set `PYTHONIOENCODING=utf-8` in the environment.

**Always test on 1 account before running the full list.**
The pipeline makes paid API calls at every step. A misconfiguration on a 200-row run wastes credits and time. Run `python pipeline.py` (test mode, 1 handle) first, confirm the output looks right, then run `--full`.

**CSV column names may have unexpected capitalisation — always do case-insensitive lookup.**
`row["handle"]` crashes if the column is `"Handle"`. Use `{k.lower(): v for k, v in row.items()}` to normalise, or check both variants explicitly.
