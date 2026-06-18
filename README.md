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
  **70**) is pushed, so every Telegram ping is trustworthy. Near-misses
  (`50–69`) are logged to `matches.json` as `"maybe"` so nothing good is lost.
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
| `TELEGRAM_CHAT_ID`  | Chat/channel id to send matches to (see below). |

### 2. Telegram bot + chat id

1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_TOKEN`.
2. Send any message to your new bot (or add it to a group).
3. Get your chat id:
   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":...}` → that number is `TELEGRAM_CHAT_ID`.
   (For a group, the id is negative; add the bot to the group first.)

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
python jobmonitor.py             # full run: scrape → score → push → persist
python jobmonitor.py --dry-run   # scrape + score, print top results, no push/persist
python jobmonitor.py --test      # push one sample match (sanity-check Telegram)
```

Environment overrides (no code changes needed):

| Var | Default | Meaning |
|-----|---------|---------|
| `MIN_FIT_SCORE`   | `70` | push threshold |
| `MAYBE_MIN_SCORE` | `50` | log-as-"maybe" floor |
| `PREFILTER_MIN`   | `40` | Haiku cutoff before Opus scoring |
| `MAX_LLM_CALLS`   | `40` | hard cap on API calls per run (cost ceiling) |
| `SCORE_BATCH`     | `8`  | postings scored per LLM call |
| `SCORING_MODEL`   | `claude-opus-4-8` | precise scorer |
| `PREFILTER_MODEL` | `claude-haiku-4-5` | cheap first pass |

---

## Telegram message format

```
💼 Postdoctoral Researcher — Urban Microclimate & Thermal Comfort  ⭐⭐⭐⭐⭐92/100
🏢 TU München · School of Engineering and Design · EURAXESS
📍 München/Bayern   🗣 English
🎯 Deep niche match: outdoor thermal comfort + urban microclimate in Munich.
🗓 2026-06-17
🔗 https://…
```

Stars scale with the score. Messages are rate-limited (~0.7s apart) so a burst
of matches doesn't trip Telegram's limits.

---

## Sources

Sources live in the `SOURCES` list near the top of `jobmonitor.py`. Each run
logs a per-source line tagged `verified` / `UNVERIFIED` and its item count —
use `--dry-run` to see it.

**Active by default:**

| Source | Tier | Type | Status |
|--------|------|------|--------|
| academics.de | A (academic) | server-rendered search | ✅ verified (~80 items, German academic board) |
| greenjobs.de | B (climate/sustainability) | RSS (Atom) | ✅ verified working (~260 items) |
| EGU job board | B (geoscience) | RSS | ✅ verified (~10 items) |
| Transsolar careers · Drees & Sommer | D (employer) | HTML scrape | ✅ returns job links |
| Serper (Google SERP) | C (cross-board breadth) | JSON API | ⚙️ gated — no-op until you add `SERPER_API_KEY` |

**`DISABLED_SOURCES`** (documented but not run) holds high-value academic
targets — **EURAXESS, jobs.ac.uk, Fraunhofer IBP** — that a generic fetcher
can't reach (403/404 bot-blocking, or JS-rendered portals with no API, sitemap,
or RSS). The robust way to cover them is **Tier C** below (search-engine index),
not a direct fetcher.

### Enabling cross-board breadth — Serper.dev (Tier C)

The big international research boards (EURAXESS, jobs.ac.uk, Nature Careers,
academics.com) are JS apps with no scrapeable feed. The way to reach **all of
them at once** is a Google search API restricted to those domains. Google's own
Custom Search JSON API is **closed to new customers** (full shutdown Jan 1 2027),
so this project uses **[Serper.dev](https://serper.dev)** — an open Google-SERP
API with a free tier (**2,500 searches/month, no credit card**).

**Setup (~2 min):**
1. Sign up at [serper.dev](https://serper.dev) (Google login; no card).
2. Copy your API key from the dashboard.
3. Add it as `SERPER_API_KEY` — in the local `.env` and in GitHub repo Secrets.

That's it — the `serper` source activates automatically and starts pulling her
niche across EURAXESS et al. Tune the domain list / keywords in the source's
`queries` in `jobmonitor.py`.

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

Both are committed back by the workflow after each run.
