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

## Layout

| Path | Role |
|------|------|
| `jobmonitor.py` | Everything: config, candidate rubric, sources, fetchers, hard filters, LLM scoring, Telegram notifier, orchestration. |
| `.github/workflows/jobmonitor.yml` | Cron (every 4h) + manual dispatch; commits `seen.json`/`matches.json` back with rebase-retry. |
| `requirements.txt` | `anthropic`, `requests`, `feedparser`, `beautifulsoup4`. |
| `README.md` | User-facing setup (secrets, Telegram, sources, cost). |
| `seen.json` | Notified/evaluated posting ids (sha1 of canonical URL + title). State. |
| `matches.json` | Full audit backlog of everything scored (`push`/`maybe`/`low`). State. |
| `jobmonitor_PROMPT.md` | Original build spec. Reference only. |

## Architecture (pipeline per run)

```
gather() ─▶ dedup() ─▶ drop seen ─▶ hard_filter() ─▶ prefilter()[Haiku] ─▶ score()[Opus]
   ─▶ push score ≥ MIN_FIT_SCORE to Telegram ─▶ persist seen.json / matches.json
```

- **Models:** `SCORING_MODEL=claude-opus-4-8` (precise), `PREFILTER_MODEL=claude-haiku-4-5` (cheap first pass). Both use **structured JSON output** (`output_config.format`) and **batch** `SCORE_BATCH` postings per call. Rubric is sent as a **cached** system prompt. No thinking param (constrained JSON output).
- **Cost control:** free hard filters first; `MAX_LLM_CALLS` caps total API calls per run (shared across both stages); prefilter keeps most traffic off Opus.
- **Precision over recall on the push:** only `≥ MIN_FIT_SCORE` (70) pings; `50–69` logged to `matches.json` as `"maybe"`.
- **Fail-open / fail-safe:** every source wrapped in try/except (dead source logged, skipped); prefilter failures pass the batch through to scoring; scoring failures skip the batch. Telegram-push failure leaves the posting *unseen* so it retries next run.
- **Out-of-credits alert:** if the Anthropic API rejects calls for an exhausted balance (`NoCreditsError`, detected via `_is_credit_error`), `run()` pushes `NO_CREDITS_MESSAGE` to Telegram and exits code 3 — so a dead balance pings you to top up instead of failing silently.

## Key knobs (all env-overridable — see CONFIG block in `jobmonitor.py`)

`MIN_FIT_SCORE`=70 · `MAYBE_MIN_SCORE`=50 · `PREFILTER_MIN`=40 · `MAX_LLM_CALLS`=40 · `SCORE_BATCH`=8 · `SCORING_MODEL` · `PREFILTER_MODEL` · `SEEN_FILE` · `MATCHES_FILE`.

Secrets: `ANTHROPIC_API_KEY` (scoring), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID` (push — comma-separated for multiple recipients, e.g. `id1,id2`). Optional Tier C: `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` (+ `GOOGLE_CSE_PAGES`, `GOOGLE_CSE_DATERESTRICT`).

**Secrets live in a gitignored `.env`** at the repo root — one file, auto-loaded by `_load_env_file()` at startup so `python jobmonitor.py` works with nothing exported. Real env vars / CI secrets always override it (loader never clobbers an already-set var). NEVER commit `.env`; for the GitHub Actions run, put the same values in repo Secrets. Live bot: "No Jobs for Rose" (`@NoJobsforRoseBot`).

## Sources — current status

Defined in `SOURCES`; each logs a `verified`/`UNVERIFIED` tag + item count per run.

| Source | Tier | Type | Status |
|--------|------|------|--------|
| greenjobs.de | B | rss (Atom) | ✅ verified (~256 items, 14-day window) |
| EGU job board | B | rss | ✅ verified (~10 items, European Geosciences Union) |
| Transsolar careers | D | html | ✅ verified (job links) |
| Drees & Sommer | D | html | ✅ verified (~18 items, Munich sustainability consultancy) |
| Google Programmable Search | C | google_cse | ⚙️ gated (no-op without keys) |

`DISABLED_SOURCES` (documented, not run): **EURAXESS** (403s automated clients), **jobs.ac.uk** (bot-blocks fetchers / 404), **Fraunhofer IBP** (JS SuccessFactors portal). Each needs a dedicated fetcher or routing through Programmable Search.

## Run

```bash
python jobmonitor.py            # full run
python jobmonitor.py --dry-run  # gather+score, print top results, no push/persist
python jobmonitor.py --test     # push one sample match (no scrape/API)
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

- 2026-06-18 — Workflow cron changed to `0 7,11,15,19 * * *` — daytime only (9am/1pm/5pm/9pm CEST).
- 2026-06-18 — Multi-recipient Telegram: `TELEGRAM_CHAT_ID` now accepts comma-separated chat IDs (e.g. `7143335971,7647141150`) — both Rose and the monitor owner get every ping.
- 2026-06-18 — Added EGU RSS + Drees & Sommer sources; widened greenjobs.de to 14-day window (256 items); bumped `MAX_ITEMS_PER_SOURCE` to 300; fixed `fetch_html` to skip mailto/tel/anchor hrefs; expanded domain hard-filter regex with German synonyms; moved to own git repo (jobmonitor-rose).
- 2026-06-17 — Added `.env` auto-loader (`_load_env_file`, one gitignored secrets file) and out-of-credits Telegram alert (`NoCreditsError`/`_is_credit_error`/`NO_CREDITS_MESSAGE`, exit code 3). Went live on the "No Jobs for Rose" bot; test ping confirmed.
- 2026-06-17 — Added CLAUDE.md + a Stop hook that reminds Claude to keep it updated.
- 2026-06-17 — Source-hardening: greenjobs.de set to verified Atom feed; added gated Google CSE Tier C fetcher (`fetch_google_cse`); moved EURAXESS/jobs.ac.uk/Fraunhofer IBP to `DISABLED_SOURCES` (confirmed unscrapeable by generic fetcher); added per-source verified/UNVERIFIED log tag.
- 2026-06-17 — Initial build: `jobmonitor.py` (sources → hard filters → Haiku prefilter → Opus scoring → Telegram), CI workflow, requirements, README, seeded state files.
