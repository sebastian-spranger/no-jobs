# Job-Monitor für Dr. Sevil Zafarmandi

A free, 24/7 job-finding monitor that polls many job sources on a schedule,
judges **semantic fit** for one very specific candidate using the **Anthropic
API (Claude)**, and pushes the good matches to **Telegram**.

It's built like a lightweight cloud scraper: single-file, one-shot per run, with
state committed back to the repo — and it runs for free on **GitHub Actions**.

> Why an LLM instead of keyword matching? The candidate sits in a narrow
> interdisciplinary niche (outdoor **thermal comfort** / **urban microclimate** /
> **sustainable building physics** + AI). A keyword like "climate" or "architect"
> alone is mostly noise, and relevant roles are scattered across academic boards,
> climate boards, general portals and employer career pages. The core of this
> tool is the **LLM fit-score**, not raw string matching.

---

## How it works (pipeline per run)

```
gather all sources  ─▶  normalize to Posting  ─▶  dedup (url + employer/title)
   ─▶  drop already-seen  ─▶  cheap hard filters (free, no LLM)
   ─▶  Haiku prefilter (coarse relevance)  ─▶  Opus scoring (precise 0-100)
   ─▶  push score ≥ threshold to Telegram  ─▶  persist seen.json / matches.json
```

- **Two-stage LLM**, capped by `MAX_LLM_CALLS`: a cheap [`claude-haiku-4-5`]
  prefilter thins the herd, then [`claude-opus-4-8`] scores survivors precisely.
  Both stages use **structured JSON output** and **batch** several postings per
  call to keep cost low. The candidate rubric is sent as a cached system prompt.
- **Precision over recall on the push**: only `score ≥ MIN_FIT_SCORE` (default
  **50**) is pushed, so every Telegram ping is trustworthy. Near-misses
  (`40–49`) are logged to `matches.json` as `"maybe"` so nothing good is lost.
- **Two tracks**: a **niche** stream (her research field) and
  **"No Easy Jobs for Rose"** — broader, decently-paid, quick-to-land jobs
  strictly in **Munich or remote**. Same bot, different chat
  (`TELEGRAM_EASY_CHAT_ID`); own rubric, sources and state
  (`seen_easy.json` / `matches_easy.json`). Choose with `--track main|easy|both`.
- **Defensive sourcing**: every source is wrapped in `try/except`; a dead source
  is logged and skipped, never crashes the run.

---

## Setup

### 1. Secrets

Add these as **GitHub Actions secrets** (`Settings → Secrets and variables →
Actions → New repository secret`), or export them locally:

| Secret              | What it is |
|---------------------|------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude). Required for scoring. |
| `TELEGRAM_TOKEN`    | Telegram bot token from [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_CHAT_ID`  | Chat/channel id for the **niche** track (see below). |
| `TELEGRAM_EASY_CHAT_ID` | Chat id for the **"No Easy Jobs for Rose"** track — same bot, different chat. Unset → that track is skipped. |
| `SERPER_API_KEY`    | [serper.dev](https://serper.dev) key (free). Powers cross-board breadth **and the entire easy track** — no key → the easy track finds nothing. |

### 2. Telegram bot + chat id

1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_TOKEN`.
2. Send any message to your new bot (or add it to a group).
3. Get your chat id:
   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":...}` → that number is `TELEGRAM_CHAT_ID`.
   (For a group, the id is negative; add the bot to the group first.)

### 2b. Second channel — "No Easy Jobs for Rose"

The easy track uses the **same bot** but posts to a **different chat**, so the two
streams stay cleanly separated. Easiest setup:

1. In Telegram, create a **channel** (or group) called *No Easy Jobs for Rose* and
   have Rose join it.
2. Add your bot to it **as an admin** (channels require admin to post).
3. Post any message there, then read the chat id:
   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates"
   ```
   The channel/group id is negative (e.g. `-1001234567890`) → set it as
   `TELEGRAM_EASY_CHAT_ID`.

If `TELEGRAM_EASY_CHAT_ID` is unset the easy track is simply skipped (the niche
track runs as before). The easy track also needs `SERPER_API_KEY` to find anything.

### 3. Verify end-to-end

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export TELEGRAM_CHAT_ID=...
python jobmonitor.py --test      # pushes one sample match — no scrape, no API key needed
```

### 4. Turn on the schedule

The workflow in `.github/workflows/jobmonitor.yml` runs **every 4 hours**
(`cron: "0 */4 * * *"`) and on manual **Run workflow** (`workflow_dispatch`),
and commits `seen.json` / `matches.json` back to the repo with a rebase-retry
loop to survive push races. Just push this repo to GitHub with the secrets set.

---

## Running locally

```bash
python jobmonitor.py              # full run, both tracks: scrape → score → push → persist
python jobmonitor.py --track easy # only the "No Easy Jobs for Rose" track (main|easy|both)
python jobmonitor.py --dry-run    # scrape + score, print top results, no push/persist
python jobmonitor.py --test       # push one sample match (sanity-check Telegram)
```

Environment overrides (no code changes needed):

*(Score/digest/age defaults are in temporary **recall mode** while the candidate is job-hunting — wider nets, more options. Raise them back later.)*

| Var | Default | Meaning |
|-----|---------|---------|
| `MIN_FIT_SCORE`   | `45` | niche push threshold |
| `MAYBE_MIN_SCORE` | `30` | log-as-"maybe" floor |
| `PREFILTER_MIN`   | `25` | Haiku cutoff before Opus scoring |
| `MAX_LLM_CALLS`   | `120` | hard cap on API calls per run, **per track** (cost ceiling) |
| `MAX_AGE_DAYS`    | `21` | drop postings older than this (when the post date is known) |
| `SCORE_BATCH`     | `8`  | postings scored per LLM call |
| `SCORING_MODEL` / `PREFILTER_MODEL` | `claude-opus-4-8` / `claude-haiku-4-5` | precise scorer / cheap first pass |
| `EASY_MIN_SCORE` / `EASY_MAYBE_MIN` | `50` / `35` | easy-track push / maybe thresholds |
| `EASY_ENABLED`    | `1`  | set `0` to disable the easy track entirely |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | – | optional free [Adzuna](https://developer.adzuna.com/signup) key (extra easy-track volume); no-op if unset |

---

## Telegram message format

```
💼 Postdoctoral Researcher — Urban Microclimate & Thermal Comfort  ⭐⭐⭐⭐⭐92/100
🚦 🔴 DRINGEND · 5 Tage
🏢 TU München · School of Engineering and Design · EURAXESS
📍 München/Bayern · 🔀 Hybrid   🗣 English
⏳ Bewerbungsfrist: 2026-06-23 (in 5 Tagen)
🎯 Deep niche match: outdoor thermal comfort + urban microclimate in Munich.
🗓 2026-06-17
🔗 https://…
```

Stars scale with the score; the 🚦 line is deadline urgency, 📍 carries the
work mode (🏠 Remote · 🔀 Hybrid · 🏢 Vor Ort) and 🗣 the language. Easy-track
messages add a 🟦 header so the second stream is unmistakable. Messages are
rate-limited (~0.7s apart) so a burst of matches doesn't trip Telegram's limits.

---

## Sources

Sources live in the `SOURCES` list near the top of `jobmonitor.py`. Each run
logs a per-source line tagged `verified` / `UNVERIFIED` and its item count —
use `--dry-run` to see it.

Most sources need **no API key**. A live-verified sweep (2026-06-19) added several
real job-board APIs/feeds, which give true posting dates (so stale listings get
dropped) and curated, scam-free data.

**Niche track (`SOURCES`) — active by default:**

| Source | Key? | What it adds |
|--------|------|--------------|
| **Arbeitsagentur** (German federal jobs API) | none | huge German coverage (~120/run); German occupation terms |
| **EURAXESS** | none | EU research jobs (now server-rendered, GET facet filters) |
| **Nature Careers** (RSS) · **jobs.ac.uk** | none | international academic/research roles |
| academics.de · greenjobs.de · EGU | none | German academic + climate boards |
| Transsolar · Drees & Sommer | none | Munich employer pages |
| Serper (Google SERP) | `SERPER_API_KEY` | cross-board breadth |

**Easy track (`EASY_SOURCES`):** Serper Easy · **Arbeitsagentur** (Munich) ·
**Himalayas** · **Arbeitnow** · **Jobicy** (free remote-jobs APIs, no key) ·
**Adzuna** (gated on `ADZUNA_APP_ID`/`KEY` — free, best freshness).

**Quality guards (all sources):** `is_scam_or_junk()` drops fake free-host
aggregators (e.g. `*.liveblog365.com`), board *search* pages, and lead-gen scams
**before** the LLM; a freshness gate drops anything older than `MAX_AGE_DAYS`.
`DISABLED_SOURCES` now holds only **Fraunhofer IBP** (JS SuccessFactors portal).

### Enabling cross-board breadth — Serper.dev (Tier C)

The big international research boards (EURAXESS, jobs.ac.uk, Nature Careers,
academics.com) are JS apps with no scrapeable feed. The way to reach **all of
them at once** is a Google web-search API. Google's own Custom Search JSON API is
**closed to new customers** (full shutdown Jan 1 2027), so this project uses
**[Serper.dev](https://serper.dev)** — an open Google-SERP API with a free tier
(**2,500 searches/month, no credit card**).

**Setup (~2 min):**
1. Sign up at [serper.dev](https://serper.dev) (Google login; no card).
2. Copy your API key from the dashboard.
3. Add it as `SERPER_API_KEY` — in the local `.env` and in GitHub repo Secrets.

That's it — the `serper` source activates automatically and pulls her niche
across EURAXESS / Nature / university boards. Tune the keywords in the source's
`queries` in `jobmonitor.py`. **Note:** the free tier only accepts *plain
keyword* queries — `site:` filters, quotes and boolean `OR` are rejected
("Query pattern not allowed for free accounts"), so each query is a simple phrase
and a junk-domain filter (`_SERPER_JUNK_DOMAIN`) strips social-media/paper-repo
noise before scoring.

| Var | What it is |
|-----|------------|
| `SERPER_API_KEY` | [serper.dev](https://serper.dev) API key (free tier) |
| `SERPER_TBS` | optional, Google freshness, default `qdr:m` (past month) |
| `SERPER_NUM` | optional, results per query, default `20` |

> The legacy `fetch_google_cse` source (vars `GOOGLE_API_KEY` / `GOOGLE_CSE_ID`)
> is kept for anyone who already has a Custom Search key, but it's a no-op
> otherwise and can't be newly provisioned.

### Adding any source

```python
{
  "name": "greenjobs.de",
  "tier": "B",            # A academic · B climate boards · C general · D employers
  "type": "rss",          # "rss" (preferred) · "html" · "google_cse"
  "url":  "https://…/feed",
  "verified": True,       # shown in the per-source log line
}
```

Prefer RSS/JSON over HTML — far more stable. HTML sources use a generic,
defensive scraper that extracts job-looking anchor links; tune `link_must_match`
(regex) per site.

The candidate profile (the LLM **rubric**) is `CANDIDATE_RUBRIC` in the same
file; the scoring weights are `SCORING_INSTRUCTIONS`. Edit those to retune fit.

---

## Cost notes

- The rubric is sent as a **cached** system prompt; postings are **batched**
  (`SCORE_BATCH` per call); the Haiku prefilter keeps most traffic off Opus.
- `MAX_LLM_CALLS` is a hard per-run ceiling — if hit, the run logs it and stops
  scoring rather than spending more. With a 4-hour cron and modest source
  volume, typical cost is a few cents per day.
- Hard filters (junior/unrelated/no-domain-signal) run **before** any LLM call,
  so obvious noise never costs a token.

---

## State files

- `seen.json` — ids (sha1 of canonical URL + title) of already-notified/evaluated
  postings, so we never ping twice.
- `matches.json` — full audit backlog of everything scored, tagged
  `push` / `maybe` / `low`. Your safety net: review it for near-misses.
- `seen_easy.json` / `matches_easy.json` — the same two files for the
  **"No Easy Jobs for Rose"** track.

All are committed back by the workflow after each run.
