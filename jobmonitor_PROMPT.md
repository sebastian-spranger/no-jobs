# One-Shot Prompt for Claude Opus — "Job-Monitor für Dr. Sevil Zafarmandi"

> Paste everything below the line into a fresh Claude Opus (Claude Code) session in an
> empty repo. It is written so Opus can build the whole thing in one shot.

---

You are building a **job-finding monitor** — same spirit as a flat-hunting scraper that
checks sources on a schedule and pushes matches to Telegram, but for **academic + industry
job openings** that fit one very specific person. This is harder than apartment hunting:
relevant jobs are **not confined to a few portals**, the candidate sits in a **narrow
interdisciplinary niche**, and a keyword like "climate" or "architect" alone produces
mostly noise. So the core of this tool is **semantic fit-scoring with an LLM**, not raw
keyword matching.

Build it to run **for free, 24/7, in GitHub Actions** (cron), single-run per invocation,
state committed back to the repo — exactly like a lightweight cloud scraper. No paid
infra is required, but the script must call the **Anthropic API** (Claude) to judge fit;
assume an `ANTHROPIC_API_KEY` secret is available.

## 1. The candidate (build the matching profile around this)

**Dr. Sevil Zafarmandi** — Humboldt Research Fellow / Postdoctoral Researcher.

- **Location preference:** Munich (Greater Munich / Bayern) **OR fully remote**. Also
  consider hybrid roles reachable from Munich and remote-from-Germany roles across the EU.
- **Current affiliation:** TU München, School of Engineering and Design (host: Dipl.-Ing.
  Thomas Auer, founder of **Transsolar**); previously/also Chair of Environmental
  Meteorology, University of Freiburg (Prof. Andreas Matzarakis).
- **Education:** PhD in Architecture (Tarbiat Modares University, Tehran). Background in
  passive cooling, modern windcatchers, on-site microclimate field measurements.
- **Core expertise / niche:**
  - Outdoor & semi-outdoor **thermal comfort**; thermal comfort indices (**PET, UTCI,
    mPET**), **clothing insulation** in urban settings
  - **Urban biometeorology / environmental meteorology / urban microclimate**
  - **Urban heat island**, heat resilience, **climate-responsive urban design**, climate
    adaptation, livability, public-health–oriented urban environments
  - **Sustainable architecture / sustainable building**, building physics (Bauphysik),
    green building (DGNB/LEED), facade/comfort engineering
  - **AI / data-driven methods** applied to the built environment & thermal comfort
  - Microclimate simulation tooling (e.g. **ENVI-met, RayMan, SkyHelios**), field
    measurement campaigns, statistical/data analysis
- **Languages:** English (research language); Persian (native); German likely limited —
  so **prefer English-language roles** and **flag German-required ones** rather than
  discarding them.
- **Career interests:** postdoc / research scientist roles AND industry roles
  (sustainability/climate consultancies, building-climate engineering, urban climate
  analytics). Both academia and industry are in scope.

Embed a condensed version of this profile in the script as the **rubric** the LLM uses to
score each posting.

## 2. What counts as a good match (fit rubric)

Have the LLM return a **fit score 0–100** plus a one-line reason, scoring on:
- **Domain overlap** (highest weight): thermal comfort, urban climate/microclimate,
  biometeorology, climate-responsive/sustainable architecture, building physics, urban
  heat/resilience, climate adaptation. A posting deep in her niche → 80–100.
- **Seniority fit:** postdoc, research associate/scientist, senior consultant, specialist,
  lead. Exclude pure PhD-student/intern roles (she already has a PhD) unless explicitly
  senior — score those low.
- **Location fit:** Munich/Bayern, remote, or remote-from-EU → boost; on-site elsewhere in
  Germany → neutral; on-site outside reachable range and non-remote → penalize.
- **Language:** English-friendly → boost; "fließend Deutsch / German native required" →
  penalize but still surface if domain fit is very high.

**Hard filters (drop before scoring, to save tokens):** clearly unrelated fields (pure
software dev, sales, nursing, finance, etc.), student/Werkstudent/Praktikum/Ausbildung,
listings with no title or no link, non-EU on-site roles with no remote option.

Only push matches with **score ≥ a configurable threshold (default 70)**.

## 3. Where to look ("jobs can appear anywhere")

Use a **tiered, redundant** approach — don't rely on one source. Implement each source
defensively (failure in one must never kill the run).

**Tier A — researcher/academic boards (RSS or HTML, no login):**
- EURAXESS (EU researcher jobs — has search/RSS), academics.de, academic-positions.com,
  jobvector, jobs.ac.uk, AcademicTransfer, Nature Careers, ResearchGate jobs, DAAD,
  university job portals (TUM, LMU, Uni Freiburg, ETH, EPFL, TU Delft, Lund, …).

**Tier B — climate/sustainability-specific boards (great for remote):**
- climatebase.org, greenjobs.de, terra.do / climate job boards, ReNewable/Greenpeace-style
  boards, Stellenwerk.

**Tier C — general job search, queried with her keyword combos:**
- Google Jobs / a Programmable Search, Indeed.de, StepStone, LinkedIn Jobs, Xing.
- Query matrices like: `("thermal comfort" OR biometeorology OR "urban climate" OR
  "urban microclimate" OR "climate adaptation" OR Bauphysik OR "sustainable architecture")
  (postdoc OR researcher OR scientist OR consultant OR engineer) (München OR Munich OR
  remote)`.

**Tier D — targeted employer career pages** (poll directly; this is where niche roles hide):
- Consultancies: **Transsolar**, Drees & Sommer, Arup, Ramboll, Buro Happold, Werner Sobek,
  EGS-plan, ee concept, Sweco, ENGIE, Ramboll.
- Research orgs: **Fraunhofer IBP (Bauphysik, Holzkirchen near Munich)**, DLR, Helmholtz
  Munich (HMGU), PIK Potsdam, Deutscher Wetterdienst (DWD), Climate Service Center (GERICS),
  UFZ, Wuppertal Institut.
- Public sector: **Stadt München / Referat für Klima- und Umweltschutz** (urban heat /
  climate-adaptation roles), other city climate offices.

Make the source list a **config block** at the top so it's easy to extend. Prefer official
RSS/JSON/API endpoints over fragile HTML scraping where they exist; for HTML use stable
selectors and fall back to full-text parsing.

## 4. Architecture & behaviour (mirror a lightweight cloud scraper)

- **Single Python file** (e.g. `jobmonitor.py`), one-shot execution.
- **Schedule:** GitHub Actions cron — default **every 3–6 hours** (job postings don't change
  by the minute; keep API costs low). `workflow_dispatch` for manual runs.
- **State files committed back to repo** (like a `seen.json`):
  - `seen.json` — IDs of already-notified postings (hash of canonical URL + title).
  - `matches.json` — full backlog of pushed matches (audit/backup).
- **Pipeline per run:** gather candidates from all sources → normalize to a common
  `Posting` dataclass (title, employer, location, url, source, snippet, date) →
  drop already-seen → apply cheap hard filters → **batch-score the rest with Claude** →
  keep score ≥ threshold → sort by score → push to Telegram → persist state.
- **LLM scoring:** one Anthropic API call per posting (or batched), model
  `claude-opus-4-8` or a cheaper `claude-haiku-4-5` for the first-pass filter and Opus for
  borderline cases. Return strict JSON `{score, reason, location_fit, language_flag}`. Cap
  the number of LLM calls per run (config) to bound cost; log when capped.
- **Dedup across sources:** same role often appears on several boards — dedupe by
  normalized (employer + title) and by URL.
- **Notifier:** Telegram bot (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` secrets), Markdown
  message. Optional: also write a daily digest.

## 5. Telegram message format

```
💼 <Job title>  ⭐<score>/100
🏢 <Employer> · <source>
📍 <Location | Remote>   🗣 <English | Deutsch erforderlich>
🎯 <one-line why it fits — from the LLM>
🗓 <posted/closing date if known>
🔗 <link>
```

Stars/emoji scaled by score. Group or rate-limit if many matches land at once.

## 6. Config block (top of file, env-overridable)

```python
MIN_FIT_SCORE   = 70          # nur Treffer >= dieser LLM-Fit-Score pushen
LOCATIONS       = ["München", "Munich", "Bayern", "remote", "EU remote"]
MAX_LLM_CALLS   = 40          # Kostendeckel pro Lauf
SCORING_MODEL   = "claude-opus-4-8"
PREFILTER_MODEL = "claude-haiku-4-5"
CHECK_CRON      = "0 */4 * * *"   # alle 4 Stunden
# Secrets: ANTHROPIC_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
```

## 7. Deliverables

1. `jobmonitor.py` — the scraper + LLM scorer + notifier (runs standalone & in CI).
2. `.github/workflows/jobmonitor.yml` — cron + manual, installs deps, runs, commits
   `seen.json`/`matches.json` back (with rebase-retry on push races).
3. `requirements.txt`.
4. `README.md` — setup (secrets, Telegram chat-id, how to add sources, cost notes).
5. A `--test` mode that pushes one sample match end-to-end.

## 8. Constraints & quality bar

- Every source wrapped in try/except; a dead source logs and is skipped, never crashes the run.
- Be honest in logs about what each source returned (count) and how many LLM calls were used.
- Respect robots/rate limits; set a real User-Agent; back off on 429/403.
- Keep it readable and configurable; no secrets in code.
- Optimize for **precision over recall on the push** (she should trust every Telegram ping),
  but log near-misses (score 50–69) to `matches.json` as "maybe" so nothing good is silently lost.

Build it now, end to end.
