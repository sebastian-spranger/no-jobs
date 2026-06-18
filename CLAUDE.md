# CLAUDE.md — Job-Monitor für Dr. Sevil Zafarmandi

> **MAINTENANCE RULE (read first).** This file is the living memory of the
> project. Whenever you make a **noteworthy** change — new/removed source, model
> change, new config var, pipeline/scoring logic change, new CLI flag, schema or
> state-file format change, dependency change, or a fix that changes behavior —
> **update the relevant section below AND prepend a dated entry to the
> Changelog**. Skip purely cosmetic edits (typos, formatting). Keep entries one
> line. A `Stop` hook reminds you each turn; act on it with judgment.

## What this is

A single-file, free, 24/7 job-finding monitor. It polls job sources on a
schedule, judges **semantic fit** for one specific candidate using the Anthropic
API (Claude), and pushes strong matches to Telegram. Built like a lightweight
cloud scraper: one-shot per run, state committed back to the repo, runs on
GitHub Actions cron. The core value is the **LLM fit-score**, not keyword match —
the candidate sits in a narrow niche (outdoor thermal comfort / urban
microclimate / sustainable building physics + AI).

**Two parallel tracks** run each cron: the **niche** stream above, and
**"No Easy Jobs for Rose"** — broader, decently-paid, quick-to-land jobs OUTSIDE
her exact field but strictly in Munich or remote (its own rubric, sources, state
files and Telegram chat; same bot).

## Layout

| Path | Role |
|------|------|
| `jobmonitor.py` | Everything: config, candidate rubric, sources, fetchers, hard filters, LLM scoring, Telegram notifier, orchestration. |
| `.github/workflows/jobmonitor.yml` | Cron (every 4h) + manual dispatch; commits `seen.json`/`matches.json` back with rebase-retry. |
| `requirements.txt` | `anthropic`, `requests`, `feedparser`, `beautifulsoup4`. |
| `README.md` | User-facing setup (secrets, Telegram, sources, cost). |
| `seen.json` / `matches.json` | Niche-track state: seen ids + full audit backlog. |
| `seen_easy.json` / `matches_easy.json` | Easy-track ("No Easy Jobs for Rose") state. |
| `jobmonitor_PROMPT.md` | Original build spec. Reference only. |

## Architecture (pipeline per run)

```
per track: gather(sources) ─▶ dedup() ─▶ drop seen ─▶ hard_filter() ─▶ prefilter()[Haiku] ─▶ score()[Opus]
   ─▶ enrich_details()[Haiku, fetches job page → deadline + work_mode + language + location] ─▶ assign_priority()
   ─▶ (easy track) Munich/remote location gate ─▶ push score ≥ track.min_score to Telegram (urgency-sorted) ─▶ persist <track> state
```

- **Two tracks (`Track` dataclass, `run_track()`):** `run()` loops `TRACKS` = [`MAIN_TRACK`, `EASY_TRACK`]. `MAIN_TRACK` = niche (`CANDIDATE_RUBRIC`, `SOURCES`, `seen.json`/`matches.json`, `TELEGRAM_CHAT_ID`, requires domain signal). `EASY_TRACK` = "No Easy Jobs for Rose" (`EASY_RUBRIC`, `EASY_SOURCES`, `seen_easy.json`/`matches_easy.json`, `TELEGRAM_EASY_CHAT_ID`, no domain requirement, broader `_EASY_UNSUITABLE` blocklist, hard **Munich-or-remote location gate** applied after enrichment via `_location_ok`). Same bot token, different chat. Easy track is gated on `EASY_ENABLED` + its chat id (skipped on real runs if the chat id is unset, so no wasted API calls). Each track has its own LLM budget.
- **Models:** `SCORING_MODEL=claude-opus-4-8` (precise), `PREFILTER_MODEL=claude-haiku-4-5` (cheap first pass). Both use **structured JSON output** (`output_config.format`) and **batch** `SCORE_BATCH` postings per call. Rubric is sent as a **cached** system prompt. No thinking param (constrained JSON output).
- **Cost control:** free hard filters first; `MAX_LLM_CALLS` (easy: `EASY_MAX_LLM_CALLS`) caps API calls per run **per track** (shared across both LLM stages within a track); prefilter keeps most traffic off Opus.
- **Precision over recall on the push:** only `≥ track.min_score` (niche 50 / easy 60) pings; the maybe band (niche 40–49 / easy 45–59) is logged to the track's matches file as `"maybe"`.
- **Daily digest:** on the first daytime run (`DIGEST_HOUR_UTC`=7 → 9am CEST) each track pushes ONE overview (`format_digest`) of its top `DIGEST_TOP_N` (5) *below-threshold* postings, so she always sees the best available even on a zero-match day. Digest items are marked `seen` (with `"digest": True`) so the overview never repeats. `DIGEST_FORCE=1` forces it on any run (for testing).
- **Page enrichment & priority:** deadline, work-mode (remote/hybrid/on-site), work language and exact location are **not** in feed snippets — `enrich_details()` fetches each actionable match's job page and ONE Haiku call extracts ALL of them (own `DEADLINE_MAX_CALLS` budget; **page values overwrite the snippet-based guesses from scoring**). New `Posting.work_mode` field + a 🏠/🔀/🏢 badge in the message. `assign_priority()` turns days-to-deadline into a label (🔴 DRINGEND ≤7d · 🟠 BALD ≤21d · 🟢 ZEIT/LAUFEND · ⚪ unbekannt · ⚫ abgelaufen); pushes sorted most-urgent-first; message shows a 🚦 priority line + ⏳ deadline line.
- **Fail-open / fail-safe:** every source wrapped in try/except (dead source logged, skipped); prefilter failures pass the batch through to scoring; scoring failures skip the batch. Telegram-push failure leaves the posting *unseen* so it retries next run.
- **Out-of-credits alert:** if the Anthropic API rejects calls for an exhausted balance (`NoCreditsError`, detected via `_is_credit_error`), `run()` pushes `NO_CREDITS_MESSAGE` to Telegram and exits code 3 — so a dead balance pings you to top up instead of failing silently.

## Key knobs (all env-overridable — see CONFIG block in `jobmonitor.py`)

`MIN_FIT_SCORE`=50 · `MAYBE_MIN_SCORE`=40 · `PREFILTER_MIN`=35 · `MAX_LLM_CALLS`=70 · `SCORE_BATCH`=8 · `SCORING_MODEL` · `PREFILTER_MODEL` · `SEEN_FILE` · `MATCHES_FILE` · `DEADLINE_SOON_DAYS`=7 · `DEADLINE_WATCH_DAYS`=21 · `DEADLINE_MAX_CALLS`=6 · `DEADLINE_PAGE_CHARS`=6000 · `DIGEST_ENABLED`=1 · `DIGEST_HOUR_UTC`=7 · `DIGEST_TOP_N`=5 (`DIGEST_FORCE`=1 to force).

Easy track: `EASY_ENABLED`=1 · `EASY_MIN_SCORE`=60 · `EASY_MAYBE_MIN`=45 · `EASY_PREFILTER_MIN`=35 · `EASY_MAX_LLM_CALLS`=`MAX_LLM_CALLS` · `EASY_SEEN_FILE`=seen_easy.json · `EASY_MATCHES_FILE`=matches_easy.json.

Secrets: `ANTHROPIC_API_KEY` (scoring), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (niche push — comma-separated for multiple recipients, e.g. `id1,id2`), `TELEGRAM_EASY_CHAT_ID` (easy track — same bot, different chat; track is skipped if unset). Tier C breadth (niche AND the entire easy track): `SERPER_API_KEY` (+ `SERPER_TBS`=qdr:m, `SERPER_NUM`=20). Legacy Tier C: `GOOGLE_API_KEY` + `GOOGLE_CSE_ID`.

**Secrets live in a gitignored `.env`** at the repo root — one file, auto-loaded by `_load_env_file()` at startup so `python jobmonitor.py` works with nothing exported. Real env vars / CI secrets always override it (loader never clobbers an already-set var). NEVER commit `.env`; for the GitHub Actions run, put the same values in repo Secrets. Live bot: "No Jobs for Rose" (`@NoJobsforRoseBot`).

## Sources — current status

Defined in `SOURCES`; each logs a `verified`/`UNVERIFIED` tag + item count per run.

| Source | Tier | Type | Status |
|--------|------|------|--------|
| academics.de | A | academics | ✅ verified (~49 items, German academic board; `/jobs?q=<term>` server-rendered, niche query terms) |
| greenjobs.de | B | rss (Atom) | ✅ verified (~256 items, 14-day window) |
| EGU job board | B | rss | ✅ verified (~10 items, European Geosciences Union) |
| Transsolar careers | D | html | ✅ verified (job links) |
| Drees & Sommer | D | html | ✅ verified (~18 items, Munich sustainability consultancy) |
| Serper (Google SERP) | C | serper | ⚙️ gated (no-op without `SERPER_API_KEY`) — cross-board breadth |
| Google Programmable Search | C | google_cse | ⚙️ legacy/gated (CSE API closed to new customers) |

`SERPER` is the active breadth path: an OPEN free Google-SERP API (serper.dev, 2,500/mo, no card) reaching the JS/bot-blocked boards via **plain keyword** niche queries (free tier blocks `site:`/quotes/`OR`), with junk-domain filtering. Replaces Google CSE, which is **closed to new customers** (verified 2026-06-18; full shutdown 2027-01-01) — `fetch_google_cse` kept only for existing-key holders.

`DISABLED_SOURCES` (documented, not run): **EURAXESS** (JS SPA — no API, no jobs sitemap, no RSS; reach via Serper instead), **jobs.ac.uk** (JS app / bot-blocks; reach via Serper), **Fraunhofer IBP** (JS SuccessFactors portal). academics.com / Nature Careers / jobvector / FindAPostDoc all likewise SPA-or-403 — Serper is the route to all of them.

`EASY_SOURCES` (easy track only): one Serper source with `junk_level:"light"` (keeps Indeed/StepStone/LinkedIn/Xing job boards, drops only social/video/paper noise) and ~10 plain-keyword Munich/remote queries (sustainability/ESG/data/GIS analyst, research associate, technical writer, project coordinator, English-speaking jobs, English teacher). **The easy track depends on `SERPER_API_KEY`** — it has no non-Serper source, so without the key it finds nothing.

## Run

```bash
python jobmonitor.py                 # full run, both tracks
python jobmonitor.py --track easy     # only the "No Easy Jobs for Rose" track (main|easy|both)
python jobmonitor.py --dry-run        # gather+score both tracks, print top results, no push/persist
python jobmonitor.py --test           # push one sample match (no scrape/API)
```

## Conventions

- Edit the **rubric** (`CANDIDATE_RUBRIC`) and **weights** (`SCORING_INSTRUCTIONS`) to retune fit — don't hardcode keyword logic.
- Adding a source = a dict in `SOURCES` + (if a new type) a `fetch_*` function returning `Posting` objects + a branch in `gather()`.
- Posting id = sha1(canonical URL + normalized title); cross-source dedup = normalized employer+title.
- Telegram uses legacy Markdown — free text is escaped via `_md()`.

## Known gaps / next steps

- Academic breadth (EURAXESS especially) depends on a dedicated fetcher or a Google CSE key. Google's Custom Search JSON API is **closed to new customers** (migrate by 2027-01-01).
- Live RSS/HTML endpoints can change; a `--dry-run` after any source edit confirms per-source counts.

## Changelog

> Newest first. One line each: `YYYY-MM-DD — what changed (why)`.

- 2026-06-18 — **Second track "No Easy Jobs for Rose"** (`Track` dataclass, `EASY_TRACK`, `run_track()`): a parallel stream for broad, decently-paid, quick-to-land jobs OUTSIDE her niche, strictly Munich-or-remote. Own `EASY_RUBRIC`/`EASY_SCORING_INSTRUCTIONS`/`EASY_PREFILTER_INSTRUCTIONS`, `EASY_SOURCES` (Serper with `junk_level:"light"` keeping Indeed/StepStone/LinkedIn job boards), `_EASY_UNSUITABLE` hard filter (drops manual/service/low-wage, keeps office/analyst, no domain requirement), thresholds (`EASY_MIN_SCORE`=60/`EASY_MAYBE_MIN`=45/`EASY_PREFILTER_MIN`=35), budget (`EASY_MAX_LLM_CALLS`), state (`seen_easy.json`/`matches_easy.json`). Same bot, new chat `TELEGRAM_EASY_CHAT_ID` (track skipped if unset). Hard Munich/remote location gate (`_location_ok`) applied AFTER enrichment; location-gated would-be-pushes marked seen so they don't reprocess. New `--track {main,easy,both}` CLI flag. Workflow now also passes `SERPER_API_KEY` (was MISSING from CI — easy track + niche breadth both need it) and `TELEGRAM_EASY_CHAT_ID`, and commits the easy state files (untracked-safe staging).
- 2026-06-18 — **Recognition fix** (language / deadline / location / work-type came out mostly "unknown" because the scorer only saw the short feed snippet): generalized deadline-only `enrich_deadlines()` → `enrich_details()`, which from the already-fetched job page now ALSO extracts work-mode, work language and candidate-relative location in the SAME Haiku call (no extra cost) and **overwrites the snippet guesses**. New `Posting.work_mode` (remote/hybrid/onsite) field + `_SCORE_SCHEMA`/`SCORING_INSTRUCTIONS` field + a 🏠/🔀/🏢 message badge (also in the digest).
- 2026-06-18 — Cross-board breadth via **Serper.dev** (`fetch_serper`, `type:"serper"`, `SERPER_API_KEY`/`SERPER_TBS`/`SERPER_NUM`): open free Google-SERP API (2,500/mo, no card). **Free-tier constraint (verified live): `site:`, quotes and boolean `OR` are rejected** ("Query pattern not allowed for free accounts") — so queries are PLAIN keyword strings only; `_SERPER_JUNK_DOMAIN` drops social-media/paper-repo noise and the LLM scores the rest. EURAXESS/Nature/university boards still surface naturally (verified end-to-end: a live run pushed 5/5 strong matches incl. MSCA fellowships, urban-microclimate postdocs, urban-heat PhDs — up from 0–2 before). Chosen after verifying Google's Custom Search JSON API is **closed to new customers** (EURAXESS confirmed unscrapeable directly: SPA, no jobs sitemap/RSS/API; Brave API dropped its free tier). Also: Telegram push now **falls back to plain text on a 400** (malformed-Markdown safety net — fixes digest push failures); academics.de queries broadened 4→8 terms (~80 items).
- 2026-06-18 — Volume fixes (Rose got 0 pings: best score was 48, threshold 70): (1) added **academics.de** source (`fetch_academics`, Tier A, German academic board, server-rendered `/jobs?q=<term>` with niche query terms — 49 items, hits her postdoc/professorship target group); (2) lowered thresholds `MIN_FIT_SCORE` 70→50, `MAYBE_MIN_SCORE` 50→40, `PREFILTER_MIN` 40→35; (3) added **daily digest** (`format_digest`, `DIGEST_*` config) — one overview of the top-5 below-threshold postings on the 9am-CEST run so she always sees the best available.
- 2026-06-18 — Deadline enrichment + priority: `enrich_deadlines()` fetches each actionable match's job page and extracts an application deadline (Haiku, own `DEADLINE_MAX_CALLS` budget); `assign_priority()` adds a 🚦 urgency label (🔴/🟠/🟢/⚪/⚫) from days-to-deadline; pushes sorted most-urgent-first; Telegram message gained 🚦 priority + ⏳ deadline lines; new `deadline`/`priority` Posting fields. Deadlines confirmed absent from all feed snippets (0/291).
- 2026-06-18 — Workflow cron changed to `0 7,11,15,19 * * *` — daytime only (9am/1pm/5pm/9pm CEST).
- 2026-06-18 — Multi-recipient Telegram: `TELEGRAM_CHAT_ID` now accepts comma-separated chat IDs (e.g. `7143335971,7647141150`) — both Rose and the monitor owner get every ping.
- 2026-06-18 — Added EGU RSS + Drees & Sommer sources; widened greenjobs.de to 14-day window (256 items); bumped `MAX_ITEMS_PER_SOURCE` to 300; fixed `fetch_html` to skip mailto/tel/anchor hrefs; expanded domain hard-filter regex with German synonyms; moved to own git repo (jobmonitor-rose).
- 2026-06-17 — Added `.env` auto-loader (`_load_env_file`, one gitignored secrets file) and out-of-credits Telegram alert (`NoCreditsError`/`_is_credit_error`/`NO_CREDITS_MESSAGE`, exit code 3). Went live on the "No Jobs for Rose" bot; test ping confirmed.
- 2026-06-17 — Added CLAUDE.md + a Stop hook that reminds Claude to keep it updated.
- 2026-06-17 — Source-hardening: greenjobs.de set to verified Atom feed; added gated Google CSE Tier C fetcher (`fetch_google_cse`); moved EURAXESS/jobs.ac.uk/Fraunhofer IBP to `DISABLED_SOURCES` (confirmed unscrapeable by generic fetcher); added per-source verified/UNVERIFIED log tag.
- 2026-06-17 — Initial build: `jobmonitor.py` (sources → hard filters → Haiku prefilter → Opus scoring → Telegram), CI workflow, requirements, README, seeded state files.
