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
per track: gather(sources)[drop scam/junk + stale > MAX_AGE_DAYS] ─▶ dedup() ─▶ drop seen ─▶ hard_filter()
   ─▶ prefilter()[Haiku] ─▶ score()[Opus] ─▶ enrich_details()[Haiku page fetch → deadline+work_mode+language+location]
   ─▶ assign_priority() ─▶ (easy) Munich/remote gate ─▶ push score ≥ track.min_score to Telegram (urgency-sorted) ─▶ persist state
```

- **Two tracks (`Track` dataclass, `run_track()`):** `run()` loops `TRACKS` = [`MAIN_TRACK`, `EASY_TRACK`]. `MAIN_TRACK` = niche (`CANDIDATE_RUBRIC`, `SOURCES`, `seen.json`/`matches.json`, `TELEGRAM_CHAT_ID`, requires domain signal). `EASY_TRACK` = "No Easy Jobs for Rose" (`EASY_RUBRIC`, `EASY_SOURCES`, `seen_easy.json`/`matches_easy.json`, `TELEGRAM_EASY_CHAT_ID`, no domain requirement, broader `_EASY_UNSUITABLE` blocklist, hard **Munich-or-remote location gate** applied after enrichment via `_location_ok`). Same bot token, different chat. Easy track is gated on `EASY_ENABLED` + its chat id (skipped on real runs if the chat id is unset, so no wasted API calls). Each track has its own LLM budget.
- **Models (scoring is PER TRACK):** niche `SCORING_MODEL=claude-opus-4-8` (precision on her real field), easy `EASY_SCORING_MODEL=claude-sonnet-4-6` (the high-volume track — bounded rubric fit-score, ~40% cheaper, negligible quality loss); `PREFILTER_MODEL=claude-haiku-4-5` (cheap first pass, both tracks). All use **structured JSON output** (`output_config.format`) and **batch** `SCORE_BATCH` per call. Rubric is sent as a **cached** system prompt. No thinking param (constrained JSON output). Steady-state cost ≈ **$0.30/day (~$9/mo)**; the dominant cost is Opus/Sonnet scoring (Haiku prefilter keeps most volume off it), and `seen.json` means each run only scores postings new since the last run.
- **Cost control:** free hard filters first; `MAX_LLM_CALLS` (easy: `EASY_MAX_LLM_CALLS`) caps API calls per run **per track** (shared across both LLM stages within a track); prefilter keeps most traffic off Opus.
- **Recall mode (2026-06-19, temporary — candidate job-hunting under deadline):** thresholds lowered (`MIN_FIT_SCORE` 50→45, `EASY_MIN_SCORE` 60→50, maybe/prefilter floors lowered too) to favor recall; precision held by the anti-scam/realism rubric + the scam/freshness filters. Pushes ≥ `track.min_score`; the maybe band is logged to the track's matches file. **Raise back after she lands a role.**
- **Many sources + freshness + anti-scam:** `gather()` now spans real job-board APIs/feeds (Bundesagentur, EURAXESS, Nature, jobs.ac.uk, Himalayas, Arbeitnow, Jobicy, Adzuna) — most with real post-dates. `is_scam_or_junk()` drops fake free-host aggregators (e.g. `*.liveblog365.com`/careersprint) + board search/listing pages + lead-gen red flags before the LLM; `_too_old()` drops postings older than `MAX_AGE_DAYS` (21) when the date is known (fail-open otherwise). `dedup_key` normalizes titles (strips `(m/w/d)` + trailing location) to collapse the same role across boards.
- **Daily digest:** twice daily in recall mode (`DIGEST_HOUR_UTC`=7 and `DIGEST_HOUR_UTC_2`=15 → 9am & 5pm CEST) each track pushes ONE overview (`format_digest`) of its top `DIGEST_TOP_N` (8) *below-threshold* postings, so she always sees the best available even on a zero-match run. Digest items are marked `seen` (`"digest": True`) so it never repeats. `DIGEST_FORCE=1` forces it.
- **Page enrichment & priority:** deadline, work-mode (remote/hybrid/on-site), work language and exact location are **not** in feed snippets — `enrich_details()` fetches each actionable match's job page and ONE Haiku call extracts ALL of them (own `DEADLINE_MAX_CALLS` budget; **page values overwrite the snippet-based guesses from scoring**). New `Posting.work_mode` field + a 🏠/🔀/🏢 badge in the message. `assign_priority()` turns days-to-deadline into a label (🔴 DRINGEND ≤7d · 🟠 BALD ≤21d · 🟢 ZEIT/LAUFEND · ⚪ unbekannt · ⚫ abgelaufen); pushes sorted most-urgent-first; message shows a 🚦 priority line + ⏳ deadline line.
- **Fail-open / fail-safe:** every source wrapped in try/except (dead source logged, skipped); prefilter failures pass the batch through to scoring; scoring failures skip the batch. Telegram-push failure leaves the posting *unseen* so it retries next run.
- **Out-of-credits alert:** if the Anthropic API rejects calls for an exhausted balance (`NoCreditsError`, detected via `_is_credit_error`), `run()` pushes `NO_CREDITS_MESSAGE` to Telegram and exits code 3 — so a dead balance pings you to top up instead of failing silently.

## Key knobs (all env-overridable — see CONFIG block in `jobmonitor.py`)

`MIN_FIT_SCORE`=45 · `MAYBE_MIN_SCORE`=30 · `PREFILTER_MIN`=25 · `MAX_LLM_CALLS`=120 (per track) · `MAX_AGE_DAYS`=21 · `MAX_PUSHES_PER_RUN`=15 (overflow rolls to next run) · `SCORE_BATCH`=8 · `SCORING_MODEL`=claude-opus-4-8 (niche) · `EASY_SCORING_MODEL`=claude-sonnet-4-6 (easy) · `PREFILTER_MODEL` · `SEEN_FILE` · `MATCHES_FILE` · `DEADLINE_SOON_DAYS`=7 · `DEADLINE_WATCH_DAYS`=21 · `DEADLINE_MAX_CALLS`=12 · `DEADLINE_PAGE_CHARS`=6000 · `DIGEST_ENABLED`=1 · `DIGEST_HOUR_UTC`=7 · `DIGEST_HOUR_UTC_2`=15 · `DIGEST_TOP_N`=8 (`DIGEST_FORCE`=1 to force). *(Score/digest/age knobs are in temporary recall mode — see Changelog.)*

Easy track: `EASY_ENABLED`=1 · `EASY_MIN_SCORE`=50 · `EASY_MAYBE_MIN`=35 · `EASY_PREFILTER_MIN`=25 · `EASY_MAX_LLM_CALLS`=`MAX_LLM_CALLS` · `EASY_SEEN_FILE`=seen_easy.json · `EASY_MATCHES_FILE`=matches_easy.json.

Secrets: `ANTHROPIC_API_KEY` (scoring), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (niche push — comma-separated for multiple recipients), `TELEGRAM_EASY_CHAT_ID` (easy track — same bot, different chat; track skipped if unset). Breadth: `SERPER_API_KEY` (both tracks). Optional easy-track volume: `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` (free, no card — gated no-op if unset). Legacy: `GOOGLE_API_KEY` + `GOOGLE_CSE_ID`. **Most new sources (Bundesagentur, EURAXESS, Nature, jobs.ac.uk, Himalayas, Arbeitnow, Jobicy) need NO key.**

**Secrets live in a gitignored `.env`** at the repo root — one file, auto-loaded by `_load_env_file()` at startup so `python jobmonitor.py` works with nothing exported. Real env vars / CI secrets always override it (loader never clobbers an already-set var). NEVER commit `.env`; for the GitHub Actions run, put the same values in repo Secrets. Live bot: "No Jobs for Rose" (`@NoJobsforRoseBot`).

## Sources — current status

Defined in `SOURCES`; each logs a `verified`/`UNVERIFIED` tag + item count per run.

**Niche track (`SOURCES`):**

| Source | Tier | Type | Key? | Status |
|--------|------|------|------|--------|
| Arbeitsagentur (niche) | A | bundesagentur | none | ✅ German federal jobs API (static `X-API-Key`); German occupation terms (~120 items) |
| EURAXESS | A | euraxess | none | ✅ now server-rendered (GET facet filters 195/219/345); browser UA; ~27 niche items |
| Nature Careers | A | nature | none | ✅ RSS per keyword (RFC822 pubDate; stale dropped by freshness gate) |
| jobs.ac.uk | A | jobs_ac_uk | none | ✅ HTML search scrape (RSS gone); browser UA (~28 items) |
| academics.de | A | academics | none | ✅ server-rendered `/jobs?q=<term>` |
| greenjobs.de · EGU | B | rss | none | ✅ Atom/RSS |
| Serper (Google SERP) | C | serper | `SERPER_API_KEY` | ⚙️ plain-keyword breadth (free tier blocks `site:`/quotes/`OR`) |
| Transsolar · Drees & Sommer | D | html | none | ✅ employer pages |
| Google Programmable Search | C | google_cse | legacy | ⚙️ no-op (CSE closed to new customers) |

**Easy track (`EASY_SOURCES`):** Serper Easy (`junk_level:"light"` keeps Indeed/StepStone/LinkedIn job boards) · Arbeitsagentur (easy, Munich, no key) · Himalayas (remote, no key, gives `expiryDate`) · Arbeitnow (DE/remote firehose, no key, Cloudflare-throttled → 1-2 pages) · Jobicy (remote, no key) · **Adzuna (DE, gated on `ADZUNA_APP_ID`/`KEY`** — best freshness via server-side `max_days_old`). The easy track no longer depends solely on Serper.

Every API fetcher is fail-open (`_http_get_json` returns `None` on non-200/non-JSON) and sends a real browser UA (`_BROWSER_UA`) since EURAXESS/jobs.ac.uk 403 a "Bot" UA. `DISABLED_SOURCES` now holds only **Fraunhofer IBP** (JS SuccessFactors). Paid Serper was evaluated and **declined** (no €10 tier — $50/50k floor; the new sources reach the boards it was working around).

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

- 2026-06-19 — **Cost: per-track scoring model** (`Track.scoring_model`): niche keeps Opus (`SCORING_MODEL`=claude-opus-4-8, precision on her real field), easy switches to Sonnet (`EASY_SCORING_MODEL`=claude-sonnet-4-6) — the easy track is the volume hog, and a bounded rubric fit-score is well within Sonnet's range, so this cuts the dominant cost ~40% with negligible quality loss. `score()` now uses `track.scoring_model`. Steady-state ≈ $0.30/day (~$9/mo); the scary ~$5/24h during the build was one-time (the research workflow + re-scoring the whole backlog 3–4× in verification), not recurring.
- 2026-06-19 — **Big source expansion + recall mode** (candidate has ~5 weeks to land a role — see memory `rose-job-urgency`): a live-verified source sweep added 8 real job-board sources — `fetch_bundesagentur` (German federal Jobsuche API, static public key, BOTH tracks), `fetch_euraxess` (now server-rendered Drupal, GET facet filters — **reactivated** from `DISABLED_SOURCES`), `fetch_nature` (RSS per keyword), `fetch_jobs_ac_uk` (HTML search — **reactivated**), `fetch_himalayas`/`fetch_arbeitnow`/`fetch_jobicy` (free remote-jobs APIs, no key), `fetch_adzuna` (gated on `ADZUNA_APP_ID`/`KEY`). Niche `gather()` went ~412→**606 raw** items. Added `is_scam_or_junk()` (drops `*.liveblog365.com`/careersprint free-host SCAM aggregators + board search/listing pages + lead-gen red-flag phrases — applied to EVERY posting before the LLM) and `_too_old()` freshness gate (`MAX_AGE_DAYS`=21; drops stale only when the date is known, fail-open). `dedup_key` now normalizes titles (strip `(m/w/d)` + trailing location) to collapse cross-board duplicates. Anti-scam/realism rubric block appended to both tracks; qualification+language-realism block to the easy track. Recall-mode knobs: `MIN_FIT_SCORE` 50→45, `EASY_MIN_SCORE` 60→50 (+maybe/prefilter floors), `MAX_LLM_CALLS` 70→120/track, `DEADLINE_MAX_CALLS` 6→12, `DIGEST_TOP_N` 5→8, twice-daily digest (`DIGEST_HOUR_UTC_2`=15), cron 4×→**6×/day**. New fetchers send a real browser UA (`_BROWSER_UA`) since EURAXESS/jobs.ac.uk 403 a "Bot" UA. Paid Serper evaluated and **declined** (no €10 tier; new sources reach the boards it was working around). All sources live-verified via a `--dry-run`.
- 2026-06-19 — **CI silently dead — fixed:** the GitHub `ANTHROPIC_API_KEY` secret was invalid (401 on every Claude call) so BOTH tracks scored 0 and pushed nothing while the run still reported "success" (fail-open). Re-set all 5 secrets (`ANTHROPIC_API_KEY`, `TELEGRAM_TOKEN`/`_CHAT_ID`/`_EASY_CHAT_ID`, `SERPER_API_KEY`) from the working local `.env` via the GitHub API; a re-triggered run then scored and the easy track pushed 2/2. The workflow had also never passed `SERPER_API_KEY` — fixed (+ `TELEGRAM_EASY_CHAT_ID`, `ADZUNA_*`).
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
