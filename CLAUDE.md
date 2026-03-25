# Influx Lead Engine — Claude Code Quick-Start

## What This Project Does

Automated lead research pipeline: takes a CSV of Instagram handles → scrapes profiles → finds LinkedIn → looks up email + phone via Apollo. Results displayed in a real-time Streamlit dashboard. Used to build outreach lists for the Influx agency.

---

## Tech Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| Dashboard | Streamlit | Real-time UI, file upload, results table |
| Scraping | Apify | Instagram profiles + website content crawling |
| Search | SerpAPI | Google search to find LinkedIn URLs |
| AI | Anthropic Claude | Name/niche extraction, LinkedIn match verification |
| Contacts | Apollo.io | Email + phone lookup via webhook reveal |
| Hosting | Railway | Webhook server (always-on) + Streamlit dashboard |
| Language | Python 3.11 | Everything |

---

## File Structure

```
instagram-tool/
├── app.py                  — Streamlit dashboard (login gate, pipeline runner, results, admin)
├── pipeline.py             — Core lead research pipeline (all 7 steps)
├── .env                    — API keys and passwords (never commit this)
├── requirements.txt        — Python dependencies for Railway build
├── railway.json            — Railway config for Streamlit dashboard deployment
├── CLAUDE.md               — This file
├── LESSONS.md              — Hard-won lessons from building this project
├── access_log.csv          — Login attempt log (auto-created on first login)
├── influx_logo.png         — Logo used in dashboard header and login screen
├── .streamlit/
│   └── config.toml         — Streamlit theme + headless=true for Railway
└── webhook-server/
    ├── webhook_server.py   — Flask app: receives Apollo callbacks, serves phone results
    ├── railway.json        — Railway config for webhook service
    ├── requirements.txt    — Flask + waitress
    ├── Procfile            — Start command: waitress-serve ...
    └── runtime.txt         — python-3.11
```

---

## API Keys — `.env` File

```
APIFY_TOKEN         — Apify platform token (scraping Instagram + websites)
SERPAPI_KEY         — SerpAPI key (Google search for LinkedIn discovery)
APOLLO_KEY          — Apollo.io API key (email + phone lookup)
ANTHROPIC_API_KEY   — Claude API key (name/niche extraction, LinkedIn verification)
ADMIN_PASSWORD      — Unlocks API key view in the dashboard Admin Settings panel
DASHBOARD_PASSWORD  — Login password shown to VA for remote dashboard access
WEBHOOK_URL         — https://influx-webhook-production.up.railway.app
```

Keys are loaded via `python-dotenv` with `override=True` so `.env` always wins over system env vars.

---

## How to Launch the Dashboard

```bash
cd C:\Users\Owenb\Desktop\instagram-tool
streamlit run app.py
```

Opens at `http://localhost:8501`. The Railway-hosted version is at the Railway dashboard URL — share that with the VA.

---

## How to Run the Pipeline

**Test mode (1 handle):**
```bash
python pipeline.py
```

**Full list:**
```bash
python pipeline.py --full
```

Input CSV must have a `handle` column with Instagram URLs. The dashboard handles this automatically via file upload. Always test on 1 account before running the full list.

---

## Railway Webhook

**URL:** `https://influx-webhook-production.up.railway.app`

**Check if live:**
```bash
curl https://influx-webhook-production.up.railway.app/health
```
Should return `{"status": "ok", ...}`. If it returns nothing or errors, redeploy from the Railway dashboard.

**Endpoints:**
- `POST /webhook` — Apollo posts phone reveal results here
- `GET /result/<person_id>` — Pipeline polls this to retrieve phone numbers
- `GET /health` — Liveness check

---

## Deploying Changes to Railway

Both services auto-deploy when `master` is pushed to GitHub (`Owenbeaver/influx-dashboard2`).

```bash
git add app.py          # or whatever changed
git commit -m "..."
git push
```

Railway picks up the push and redeploys within ~2 minutes. First deploy after connecting GitHub needs a manual trigger in the Railway UI.

**Dashboard service:** deploys from repo root, uses `railway.json` + `requirements.txt`
**Webhook service:** deploys from `webhook-server/` subfolder, uses its own `railway.json`
