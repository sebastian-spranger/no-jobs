#!/usr/bin/env python3
"""
Job-Monitor für Dr. Sevil Zafarmandi
====================================

A single-file job-finding monitor in the spirit of a lightweight flat-hunting
scraper: it polls many job sources on a schedule, judges *semantic fit* for one
very specific candidate using the Anthropic API (Claude), and pushes the good
matches to Telegram.

Why an LLM and not keyword matching? The candidate sits in a narrow
interdisciplinary niche (outdoor thermal comfort / urban microclimate /
sustainable building physics + AI). A keyword like "climate" or "architect"
alone is mostly noise, and relevant roles are scattered across academic boards,
climate job boards, general portals and employer career pages. So the core of
this tool is an LLM fit-score, not raw string matching.

Design goals (mirrors a free 24/7 GitHub Actions scraper):
  * one-shot execution, state committed back to the repo (seen.json / matches.json)
  * every source wrapped in try/except — a dead source is logged and skipped,
    never crashes the run
  * cheap hard filters first (free), then a cheap LLM prefilter (Haiku), then
    precise scoring (Opus) only for survivors — with a hard cap on LLM calls
  * precision over recall on the *push*: she should trust every Telegram ping.
    Near-misses (score 50-69) are still logged to matches.json as "maybe".

Run:
    python jobmonitor.py            # normal run
    python jobmonitor.py --test     # push one sample match end-to-end
    python jobmonitor.py --dry-run  # gather + score, but don't push or persist

Secrets (env / GitHub Actions secrets):
    ANTHROPIC_API_KEY   (required for scoring; --test works without scoring)
    TELEGRAM_TOKEN      (required to push)
    TELEGRAM_CHAT_ID    (required to push)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

# Third-party parsers are imported lazily / defensively so the script still
# starts (and --test still works) even if an optional dep is missing.
try:
    import feedparser  # type: ignore
except Exception:  # pragma: no cover
    feedparser = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None


# ──────────────────────────────────────────────────────────────────────────
# 0. SECRETS FILE  (.env) — one local file holds the keys + numbers
# ──────────────────────────────────────────────────────────────────────────
# Loads KEY=VALUE lines from a local .env into the environment so a plain
# `python jobmonitor.py` works without exporting anything. Real env vars and
# GitHub Actions secrets ALWAYS win (we never overwrite an already-set var).
# The .env file is gitignored — never commit it.

def _load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:  # don't clobber real env / CI secrets
                    os.environ[key] = val
    except Exception as exc:  # never let a malformed file crash startup
        logging.getLogger("jobmonitor").warning("Could not read %s: %s", path, exc)


_load_env_file()


# ──────────────────────────────────────────────────────────────────────────
# 1. CONFIG BLOCK  (env-overridable — edit here or set env vars in CI)
# ──────────────────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


MIN_FIT_SCORE   = _env_int("MIN_FIT_SCORE", 50)      # nur Treffer >= diesem Score pushen
MAYBE_MIN_SCORE = _env_int("MAYBE_MIN_SCORE", 40)    # 40-49 -> als "maybe" in matches.json loggen
PREFILTER_MIN   = _env_int("PREFILTER_MIN", 35)      # Haiku-Cutoff vor dem teuren Opus-Scoring

# Daily digest: even on days with no >= MIN_FIT_SCORE match, push ONE overview of
# the best available postings so she always sees what's out there. Sent only on
# the run whose UTC hour == DIGEST_HOUR (07 UTC = 9am CEST, the first daytime run).
DIGEST_ENABLED  = os.environ.get("DIGEST_ENABLED", "1").strip() not in ("0", "false", "")
DIGEST_HOUR_UTC = _env_int("DIGEST_HOUR_UTC", 7)     # 9am CEST
DIGEST_TOP_N    = _env_int("DIGEST_TOP_N", 5)

LOCATIONS = [
    "München", "Munich", "Bayern", "Bavaria",
    "remote", "EU remote", "remote Germany", "hybrid Munich",
]

MAX_LLM_CALLS   = _env_int("MAX_LLM_CALLS", 70)      # Kostendeckel pro Lauf (gesamt, beide Stufen)
SCORE_BATCH     = _env_int("SCORE_BATCH", 8)         # Postings pro LLM-Call (Batching senkt Kosten)

# Deadline enrichment: fetch the job page for each strong/maybe match and pull
# the application deadline (it is NOT in the RSS/HTML feed snippets). Own small
# LLM budget so it never starves scoring. Drives the priority label in the ping.
DEADLINE_SOON_DAYS  = _env_int("DEADLINE_SOON_DAYS", 7)    # <= -> 🔴 dringend
DEADLINE_WATCH_DAYS = _env_int("DEADLINE_WATCH_DAYS", 21)  # <= -> 🟠 bald bewerben
DEADLINE_MAX_CALLS  = _env_int("DEADLINE_MAX_CALLS", 6)    # eigener LLM-Deckel für Deadline-Extraktion
DEADLINE_PAGE_CHARS = _env_int("DEADLINE_PAGE_CHARS", 6000)  # wieviel Seitentext an die LLM geht

SCORING_MODEL   = os.environ.get("SCORING_MODEL", "claude-opus-4-8")
PREFILTER_MODEL = os.environ.get("PREFILTER_MODEL", "claude-haiku-4-5")

CHECK_CRON      = "0 */4 * * *"                       # alle 4 Stunden (siehe workflow yml)

# Files committed back to the repo (state, like a seen.json in a flat scraper).
SEEN_FILE       = os.environ.get("SEEN_FILE", "seen.json")
MATCHES_FILE    = os.environ.get("MATCHES_FILE", "matches.json")

# ── Second track: "No Easy Jobs for Rose" ─────────────────────────────────
# A parallel stream for jobs OUTSIDE her research niche that she could land
# quickly & easily and that pay decently — strictly Munich (on-site) OR fully
# remote. Same Telegram bot, a DIFFERENT chat: set TELEGRAM_EASY_CHAT_ID.
# The track is gated on that chat id (no chat -> not run on real runs) and on
# its own state files so it never collides with the niche track. It has its own
# rubric, sources, thresholds and LLM budget (see EASY_TRACK below).
EASY_ENABLED       = os.environ.get("EASY_ENABLED", "1").strip() not in ("0", "false", "")
EASY_MIN_SCORE     = _env_int("EASY_MIN_SCORE", 60)    # push threshold (easy stream)
EASY_MAYBE_MIN     = _env_int("EASY_MAYBE_MIN", 45)    # 45-59 -> "maybe" in matches_easy.json
EASY_PREFILTER_MIN = _env_int("EASY_PREFILTER_MIN", 35)
EASY_MAX_LLM_CALLS = _env_int("EASY_MAX_LLM_CALLS", MAX_LLM_CALLS)  # own cost ceiling
EASY_SEEN_FILE     = os.environ.get("EASY_SEEN_FILE", "seen_easy.json")
EASY_MATCHES_FILE  = os.environ.get("EASY_MATCHES_FILE", "matches_easy.json")

# Be a polite scraper.
USER_AGENT = (
    "JobMonitorBot/1.0 (+https://github.com/; academic job fit monitor; "
    "contact: repo owner)"
)
HTTP_TIMEOUT = _env_int("HTTP_TIMEOUT", 25)
MAX_ITEMS_PER_SOURCE = _env_int("MAX_ITEMS_PER_SOURCE", 300)


# ──────────────────────────────────────────────────────────────────────────
# 2. THE CANDIDATE  (condensed profile = the rubric the LLM scores against)
# ──────────────────────────────────────────────────────────────────────────

CANDIDATE_RUBRIC = """\
CANDIDATE: Dr. Sevil Zafarmandi — Humboldt Research Fellow / Postdoctoral Researcher.

Current: TU München, School of Engineering and Design (host: Dipl.-Ing. Thomas Auer,
founder of Transsolar). Also Chair of Environmental Meteorology, University of Freiburg
(Prof. Andreas Matzarakis). PhD in Architecture (Tarbiat Modares University, Tehran);
background in passive cooling, modern windcatchers, on-site microclimate field measurements.

CORE EXPERTISE / NICHE:
- Outdoor & semi-outdoor THERMAL COMFORT; thermal comfort indices (PET, UTCI, mPET),
  clothing insulation in urban settings.
- Urban biometeorology / environmental meteorology / urban microclimate.
- Urban heat island, heat resilience, climate-responsive urban design, climate adaptation,
  livability, public-health-oriented urban environments.
- Sustainable architecture / sustainable building, building physics (Bauphysik), green
  building (DGNB/LEED), facade & comfort engineering.
- AI / data-driven methods applied to the built environment & thermal comfort.
- Microclimate simulation tooling (ENVI-met, RayMan, SkyHelios), field measurement
  campaigns, statistical / data analysis.

LANGUAGES: English (research language); Persian (native); German likely limited — PREFER
English-language roles and FLAG German-required ones (do not auto-discard them).

LOCATION PREFERENCE: Munich / Greater Munich / Bayern, OR fully remote. Also OK: hybrid
roles reachable from Munich, and remote-from-Germany / remote-within-EU roles.

CAREER INTERESTS: postdoc / research scientist roles AND industry roles (sustainability /
climate consultancies, building-climate engineering, urban climate analytics). Both
academia and industry are in scope. She already has a PhD — PhD-student / intern roles are
NOT a fit unless explicitly senior.
"""

SCORING_INSTRUCTIONS = """\
You score how well a single job posting fits the candidate above. Return a fit score 0-100.

Weighting:
- DOMAIN OVERLAP (highest weight): thermal comfort, urban climate/microclimate,
  biometeorology, climate-responsive/sustainable architecture, building physics, urban
  heat/resilience, climate adaptation. A posting deep in her niche -> 80-100.
- SENIORITY FIT: postdoc, research associate/scientist, senior consultant, specialist,
  lead -> good. Pure PhD-student / intern / Werkstudent -> score low (she has a PhD)
  unless explicitly senior.
- LOCATION FIT: Munich/Bayern, remote, or remote-from-EU -> boost; on-site elsewhere in
  Germany -> neutral; on-site outside reachable range with no remote option -> penalize.
- LANGUAGE: English-friendly -> boost; "fließend Deutsch / German native required" ->
  penalize, but still surface if domain fit is very high.

Be calibrated and strict: most general postings should score low. Reserve 70+ for genuine
domain matches. Optimize for precision — a high score should mean she'd genuinely want the ping.

For each posting return:
  score          : integer 0-100
  reason         : ONE short line (max ~18 words) saying why it fits or doesn't
  location_fit   : one of "munich", "remote", "germany", "eu", "elsewhere", "unknown"
  language_flag  : one of "english", "german_required", "unknown"
  work_mode      : one of "remote", "hybrid", "onsite", "unknown"
                   (remote = fully remote/home-office; hybrid = partly on-site;
                    onsite = fixed workplace). Infer from the text; "unknown" if unstated.
"""

# Coarse first-pass instructions for the Haiku prefilter (the niche track).
MAIN_PREFILTER_INSTRUCTIONS = (
    "Quickly rate each posting's RELEVANCE to the candidate's domain on 0-100 "
    "(domain overlap + seniority). This is a coarse first pass — be generous to "
    "anything plausibly in-domain, harsh on clearly off-topic roles. "
    "Return only id and score for each."
)


# ── "No Easy Jobs for Rose" — the easy / adjacent track rubric ─────────────
# Same person, different lens: instead of her research niche, find decently-paid
# jobs she could land QUICKLY and EASILY, strictly in Munich or fully remote.
EASY_RUBRIC = """\
CANDIDATE (same person, different lens): Dr. Sevil Zafarmandi — PhD, postdoctoral
researcher. Fluent ENGLISH (research language), native Persian, LIMITED German.
Transferable skills: research & analysis, statistics / data analysis, scientific &
technical writing/editing, simulation & modelling tools, spatial / GIS-style analysis,
sustainability & climate domain knowledge, teaching & presenting, project work.

GOAL OF THIS CHANNEL ("easy track"): surface jobs she could realistically land
QUICKLY and EASILY — roles where she is clearly qualified or OVERqualified, the barrier
to entry is low, hiring is fast, and the pay is DECENT (a solid professional salary, not
minimum wage, not unpaid). These need NOT be in her research niche. Think: a reliable,
well-paid paycheck she can get soon.

IN SCOPE (broad):
- Adjacent professional roles leveraging her PhD/English: sustainability/ESG/climate
  analyst or consultant, research assistant/associate (NOT PhD-student), data/GIS/
  quantitative analyst, technical or scientific writer/editor, environmental consulting,
  university/research-institute staff or coordinator, lab/project coordinator.
- Broad decently-paid English-friendly office roles she could plausibly get fast even
  outside her field: analysis, coordination, operations, program/project support,
  knowledge work, customer success (English).
- Flexible / quick-start work: English teaching/tutoring, part-time research, freelance
  analysis, proofreading/editing in English.

HARD CONSTRAINTS (this channel is strict):
- LOCATION: MUNICH / Greater Munich / Bavaria on-site, OR FULLY REMOTE only. Anything
  else (other German city on-site, EU on-site, hybrid tied to another city) is OUT.
- LANGUAGE: must be doable with English / limited German. Roles requiring fluent or
  native German score LOW.
- PAY & LEVEL: decently-paid professional work only. Internships, Werkstudent,
  Ausbildung, unpaid, or low-wage manual/service jobs are OUT.
- EASE: favour low-barrier, fast-hiring roles; penalise ones needing niche
  certifications/licences she lacks (e.g. German-bar lawyer, medical licence).
"""

EASY_SCORING_INSTRUCTIONS = """\
You score how well a single job posting fits the "easy, quick-to-land, decently-paid,
Munich-or-remote" goal above. Return a fit score 0-100.

Weighting:
- EASE OF LANDING (highest weight): is she clearly qualified or OVERqualified, with a low
  barrier and fast hiring? -> high. Needs skills/licences she lacks, or is highly
  competitive -> low.
- LOCATION (hard gate): Munich/Bavaria on-site OR fully remote -> ok; anything else -> LOW.
- LANGUAGE (hard gate): English-doable / limited-German ok -> ok; fluent/native German
  required -> LOW.
- PAY & LEVEL: decently-paid professional role -> ok; intern/Werkstudent/low-wage
  manual/service -> LOW.

Be calibrated: reserve 70+ for roles she could genuinely land quickly AND that pay
decently AND are in Munich or remote. Optimize for precision — a high score should mean
"she could realistically have this job soon."

For each posting return the SAME fields as the other rubric:
  score, reason (one short line), location_fit, language_flag, work_mode.
"""

EASY_PREFILTER_INSTRUCTIONS = (
    "Quickly rate 0-100 how plausibly this is an EASY, decently-paid job she could land "
    "fast in MUNICH or REMOTE (see the goal above). Be generous to plausible office / "
    "adjacent / flexible roles in Munich or remote; be harsh on roles that are clearly the "
    "wrong location (other city on-site), require fluent German, are unpaid / intern / "
    "Werkstudent, or are low-wage manual/service work. Return only id and score."
)


# ──────────────────────────────────────────────────────────────────────────
# 3. SOURCES  (tiered, redundant; easy to extend — add a dict to SOURCES)
# ──────────────────────────────────────────────────────────────────────────
#
# Each source has: name, tier, type ("rss" | "html"), url, and for html a
# parser key. Prefer official RSS/JSON over fragile HTML. Every fetch is
# wrapped in try/except by gather() — a broken source logs a warning and is
# skipped, never crashes the run.
#
# RSS feeds are the most robust no-login option. The query-able general portals
# (Tier C) and employer career pages (Tier D) are best added as HTML sources or
# via a Programmable Search key — see README. A few representative HTML hooks
# are included and parsed defensively.

SOURCES: list[dict[str, Any]] = [
    # ---- Tier B: climate / sustainability boards (RSS) ----
    # greenjobs.de Atom feed — VERIFIED working (returns Atom XML with ~250
    # env-sector entries on a 14-day window). seen.json dedupes so the wide
    # window is harmless; only new postings are scored each run.
    {
        "name": "greenjobs.de",
        "tier": "B",
        "type": "rss",
        "url": "https://www.greenjobs.de/angebote/neueste.html?zeitraum=14&feed=atom",
        "verified": True,
    },
    # EGU (European Geosciences Union) job board RSS — VERIFIED working (~10
    # entries, all geosciences/meteorology). Directly relevant to urban climate /
    # biometeorology positions across European research institutions.
    {
        "name": "EGU job board",
        "tier": "B",
        "type": "rss",
        "url": "https://www.egu.eu/jobs/rss/",
        "verified": True,
    },
    # ---- Tier A: academic job board (her core target group) ----
    # academics.de — Germany's main academic board. Server-rendered search;
    # VERIFIED working (/jobs?q=<term> returns job-detail anchors in the HTML).
    # Covers the postdoc/professorship/research roles the feeds above miss.
    {
        "name": "academics.de",
        "tier": "A",
        "type": "academics",
        "queries": [
            "Stadtklima", "thermischer Komfort", "Bauphysik", "Klimaanpassung",
            "Mikroklima", "urban climate", "nachhaltiges Bauen", "Umweltmeteorologie",
        ],
        "verified": True,
    },
    # ---- Tier C: cross-board breadth via Serper.dev (Google SERP API) ----
    # The active breadth source: an OPEN free-tier Google search API (2,500/mo, no
    # card) reaching the big JS/bot-blocked boards via site:-restricted queries.
    # No-op unless SERPER_API_KEY is set — see README "Tier C setup".
    {
        "name": "Serper (Google web search)",
        "tier": "C",
        "type": "serper",
        # PLAIN keyword queries only — the free tier rejects site:/quotes/OR.
        # Each surfaces her niche across EURAXESS / Nature / university boards;
        # junk domains are filtered and the LLM scores the rest. (Verified live:
        # returns MSCA fellowships, urban-microclimate postdocs, urban-heat PhDs.)
        "queries": [
            "outdoor thermal comfort postdoc position",
            "urban microclimate researcher university vacancy",
            "urban heat climate adaptation postdoc",
            "urban climate modelling research position",
            "Stadtklima wissenschaftlicher Mitarbeiter Universität",
            "Bauphysik thermischer Komfort Postdoc Stelle",
        ],
        "verified": True,
    },
    # ---- Tier C (legacy): Google Programmable Search ----
    # Custom Search JSON API. No-op unless BOTH GOOGLE_API_KEY and GOOGLE_CSE_ID
    # are set. This is the ONLY automated route to the big JS/bot-blocked research
    # boards (EURAXESS, jobs.ac.uk, Nature Careers, academics.com, university
    # career pages): configure those domains in the Programmable Search Engine
    # control panel, and this query targets her niche + research-role keywords
    # across all of them via Google's index. See README "Tier C setup".
    {
        "name": "Google Programmable Search",
        "tier": "C",
        "type": "google_cse",
        "query": (
            '("thermal comfort" OR biometeorology OR "urban climate" OR '
            '"urban microclimate" OR microclimate OR "outdoor comfort" OR '
            '"climate adaptation" OR "urban heat" OR Stadtklima OR Bauphysik OR '
            '"building physics" OR "sustainable building" OR Klimaanpassung) '
            '(postdoc OR "post-doc" OR researcher OR "research associate" OR '
            'scientist OR "wissenschaftliche*r Mitarbeiter*in" OR professor OR '
            'Juniorprofessur OR fellowship)'
        ),
        "verified": False,
    },
    # ---- Tier D: employer career pages (HTML, generic parser) ----
    # Intentionally defensive: the generic HTML parser extracts anchor links
    # that look like job postings. Tune `link_must_match` per site.
    {
        "name": "Transsolar careers",
        "tier": "D",
        "type": "html",
        "url": "https://transsolar.com/jobs",
        "link_must_match": r"(job|stelle|career|vacan|position)",
        "verified": True,  # returns job links in testing; LLM filters out nav noise
    },
    # Drees & Sommer — Munich-based sustainability / engineering consultancy.
    # Full job listings page; VERIFIED 200 with ~18 current roles.
    {
        "name": "Drees & Sommer",
        "tier": "D",
        "type": "html",
        "url": "https://career.dreso.com/de/stellenangebote",
        "link_must_match": r"(stellenangebote/details|job|vacan|position)",
        "verified": True,
    },
]

# Known high-value targets that need a dedicated integration, NOT iterated by
# gather(). Each was confirmed unscrapeable by a generic fetcher (bot-blocking
# or JS-rendered portals). Wire one up by writing a small fetch_* function that
# returns Posting objects, then move its dict into SOURCES above.
DISABLED_SOURCES: list[dict[str, Any]] = [
    {
        "name": "EURAXESS",
        "tier": "A",
        # Returns HTTP 403 to automated clients; the old /jobs/search/rss feed is
        # gone. Use the EURAXESS search UI's underlying data endpoint or a
        # Programmable Search restricted to euraxess.ec.europa.eu.
        "url": "https://euraxess.ec.europa.eu/jobs/search",
    },
    {
        "name": "jobs.ac.uk",
        "tier": "A",
        # Has RSS by subject-area/location/role at https://www.jobs.ac.uk/feeds
        # but bot-blocks automated fetchers (404). Discover the real feed href
        # from a browser, or query it via Programmable Search.
        "url": "https://www.jobs.ac.uk/feeds",
    },
    {
        "name": "Fraunhofer IBP (Bauphysik, Holzkirchen)",
        "tier": "D",
        # Listings live on the central SAP SuccessFactors portal (JS-rendered),
        # filtered to "IBP - Bauphysik" — not anchor-scrapeable. Needs the
        # SuccessFactors search API or Programmable Search on jobs.fraunhofer.de.
        "url": "https://jobs.fraunhofer.de/search/?optionsFacetsDD_customfield4=IBP+-+Bauphysik",
    },
]

# Sources for the "No Easy Jobs for Rose" (easy) track. Broad Munich/remote
# breadth via Serper (Google SERP) with the LIGHTER junk filter
# ("junk_level": "light") so the big job boards (Indeed, StepStone, LinkedIn,
# Xing) survive — for general roles those boards are the target, not noise.
# Plain-keyword queries only (Serper free-tier constraint). No-op without
# SERPER_API_KEY, so the easy track needs that key set to find anything.
EASY_SOURCES: list[dict[str, Any]] = [
    {
        "name": "Serper Easy (Munich/remote)",
        "tier": "C",
        "type": "serper",
        "junk_level": "light",
        "queries": [
            "sustainability analyst Munich English",
            "ESG consultant Munich English",
            "research associate Munich English",
            "data analyst remote Germany English",
            "GIS analyst Germany remote English",
            "technical writer remote Germany English",
            "climate consultant Munich English",
            "project coordinator Munich English",
            "English speaking jobs Munich",
            "English teacher Munich",
        ],
        "verified": True,
    },
]


# ──────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jobmonitor")


# ──────────────────────────────────────────────────────────────────────────
# 4. Posting model + normalization
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Posting:
    title: str
    employer: str
    location: str
    url: str
    source: str
    snippet: str = ""
    date: str = ""
    # filled in by the scoring stage
    score: int = 0
    reason: str = ""
    location_fit: str = "unknown"
    language_flag: str = "unknown"
    work_mode: str = "unknown"   # remote | hybrid | onsite | unknown
    # filled in by the page-enrichment stage (fetches the full job page)
    deadline: str = ""        # ISO "YYYY-MM-DD", or "rolling", or "" if unknown
    priority: str = ""        # human label, set in run() from deadline + score

    @property
    def id(self) -> str:
        """Stable id = hash of canonical URL + title (like seen.json keys)."""
        canon = _canonical_url(self.url) + "::" + _norm(self.title)
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]

    @property
    def dedup_key(self) -> str:
        """Cross-source dedup: same role on several boards -> employer+title."""
        return _norm(self.employer) + "::" + _norm(self.title)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _canonical_url(url: str) -> str:
    url = (url or "").strip()
    # strip tracking params and trailing slash for stable hashing
    url = re.sub(r"[?#].*$", "", url)
    return url.rstrip("/")


def _clean_text(html: str) -> str:
    """Strip tags/entities to a short plain-text snippet."""
    if not html:
        return ""
    if BeautifulSoup is not None:
        try:
            text = BeautifulSoup(html, "html.parser").get_text(" ")
        except Exception:
            text = re.sub(r"<[^>]+>", " ", html)
    else:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


# ──────────────────────────────────────────────────────────────────────────
# 5. Source fetchers (defensive)
# ──────────────────────────────────────────────────────────────────────────

def _http_get(url: str) -> requests.Response:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=HTTP_TIMEOUT,
    )
    # Back off politely on rate limit / forbidden — caller treats as failure.
    if resp.status_code in (429, 403):
        retry_after = resp.headers.get("Retry-After")
        log.warning("  %s returned %s (Retry-After=%s); skipping this run",
                    url, resp.status_code, retry_after)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp


def fetch_rss(src: dict[str, Any]) -> list[Posting]:
    if feedparser is None:
        log.warning("  feedparser not installed; cannot parse RSS for %s", src["name"])
        return []
    resp = _http_get(src["url"])
    feed = feedparser.parse(resp.content)
    postings: list[Posting] = []
    for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = entry.get("summary", "") or entry.get("description", "")
        # author / source field often carries the employer
        employer = (
            entry.get("author")
            or entry.get("source", {}).get("title", "")  # type: ignore[union-attr]
            or ""
        )
        date = (
            entry.get("published")
            or entry.get("updated")
            or ""
        )
        postings.append(Posting(
            title=title,
            employer=employer.strip() or src["name"],
            location=_guess_location(title + " " + summary),
            url=link,
            source=src["name"],
            snippet=_clean_text(summary),
            date=str(date)[:40],
        ))
    return postings


def fetch_html(src: dict[str, Any]) -> list[Posting]:
    """Generic, defensive HTML scraper: extract anchors that look like jobs."""
    if BeautifulSoup is None:
        log.warning("  bs4 not installed; cannot parse HTML for %s", src["name"])
        return []
    resp = _http_get(src["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(src.get("link_must_match", r"."), re.I)
    base = src["url"]
    seen_links: set[str] = set()
    postings: list[Posting] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Skip non-HTTP links (mailto:, tel:, javascript:, anchors)
        if not href.startswith(("http", "/")) or href.startswith("#"):
            continue
        text = a.get_text(" ").strip()
        if not text or len(text) < 10:
            continue
        full = requests.compat.urljoin(base, href)  # type: ignore[attr-defined]
        if not full.startswith("http"):
            continue
        if not pattern.search(full) and not pattern.search(text):
            continue
        if full in seen_links:
            continue
        seen_links.add(full)
        postings.append(Posting(
            title=text[:200],
            employer=src["name"],
            location=_guess_location(text),
            url=full,
            source=src["name"],
            snippet="",
            date="",
        ))
        if len(postings) >= MAX_ITEMS_PER_SOURCE:
            break
    return postings


_ACADEMICS_HREF = re.compile(r"/jobs/[a-z0-9].*-\d{6,}", re.I)


def fetch_academics(src: dict[str, Any]) -> list[Posting]:
    """academics.de — Germany's main academic job board (postdocs, professorships,
    research staff). Server-rendered search: GET /jobs?q=<term> returns job-detail
    anchors right in the HTML. We run a few niche query terms and dedupe; the LLM
    scoring does the precision (the on-site search is fuzzy/OR-based)."""
    if BeautifulSoup is None:
        log.warning("  bs4 not installed; cannot parse academics.de")
        return []
    base = "https://www.academics.de"
    queries = src.get("queries", ["Stadtklima"])
    seen_links: set[str] = set()
    postings: list[Posting] = []
    for term in queries:
        try:
            resp = _http_get(f"{base}/jobs?q={requests.compat.quote(term)}")  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("  academics.de query %r failed: %s", term, exc)
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not _ACADEMICS_HREF.search(href):
                continue
            full = requests.compat.urljoin(base, href)  # type: ignore[attr-defined]
            if full in seen_links:
                continue
            text = re.sub(r"\s+", " ", a.get_text(" ")).strip()
            text = re.sub(r"^(Top Job|Premium|Anzeige)\s+", "", text, flags=re.I)
            if len(text) < 10:
                continue
            seen_links.add(full)
            postings.append(Posting(
                title=text[:180],
                employer="academics.de",
                location=_guess_location(text),
                url=full,
                source=src["name"],
                snippet=text[:400],   # title text carries institution + location
                date="",
            ))
            if len(postings) >= MAX_ITEMS_PER_SOURCE:
                break
        if len(postings) >= MAX_ITEMS_PER_SOURCE:
            break
    return postings


def fetch_google_cse(src: dict[str, Any]) -> list[Posting]:
    """Tier C: Google Programmable Search (Custom Search JSON API).

    No-op unless GOOGLE_API_KEY and GOOGLE_CSE_ID are both set. The API returns
    at most 10 results per call and is paginated via `start`; we pull up to
    GOOGLE_CSE_PAGES pages (default 2 -> 20 results) to bound quota use.
    """
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    cse_id = os.environ.get("GOOGLE_CSE_ID", "").strip()
    if not api_key or not cse_id:
        log.info("  Google CSE skipped (GOOGLE_API_KEY / GOOGLE_CSE_ID not set)")
        return []
    pages = _env_int("GOOGLE_CSE_PAGES", 2)
    postings: list[Posting] = []
    for page in range(pages):
        params = {
            "key": api_key,
            "cx": cse_id,
            "q": src["query"],
            "num": 10,
            "start": 1 + page * 10,
            "dateRestrict": os.environ.get("GOOGLE_CSE_DATERESTRICT", "d14"),  # last 14 days
        }
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params, timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            log.warning("  Google CSE HTTP %s: %s", resp.status_code, resp.text[:160])
            break
        items = resp.json().get("items", [])
        if not items:
            break
        for it in items:
            title = (it.get("title") or "").strip()
            link = (it.get("link") or "").strip()
            if not title or not link:
                continue
            display = it.get("displayLink", "")
            postings.append(Posting(
                title=title,
                employer=display or src["name"],
                location=_guess_location(title + " " + it.get("snippet", "")),
                url=link,
                source=src["name"],
                snippet=_clean_text(it.get("snippet", "")),
                date="",
            ))
        if len(items) < 10:
            break
    return postings


# Non-job domains that pollute web-search results (social media, paper repos,
# aggregators) — dropped before the LLM so they never burn prefilter calls.
_SERPER_JUNK_DOMAIN = re.compile(
    r"(facebook|instagram|linkedin|twitter|x\.com|youtube|tiktok|reddit|"
    r"redcircle|spotify|podcast|pinterest|amazon|wikipedia|researchgate|"
    r"biorxiv|medrxiv|arxiv\.org|ssrn|sciencedirect|springer|mdpi|"
    r"semanticscholar|google\.com|glassdoor|indeed)",
    re.I,
)

# Lighter blocklist for the EASY track: drop pure social / video / podcast /
# paper-repo noise but KEEP the big job boards (Indeed, StepStone, LinkedIn,
# Glassdoor, Xing) — for broad Munich/remote roles those boards ARE the target.
_SERPER_JUNK_LIGHT = re.compile(
    r"(facebook|instagram|twitter|x\.com|youtube|tiktok|reddit|pinterest|"
    r"redcircle|spotify|podcast|wikipedia|researchgate|biorxiv|medrxiv|"
    r"arxiv\.org|ssrn|sciencedirect|springer|mdpi|semanticscholar)",
    re.I,
)


def fetch_serper(src: dict[str, Any]) -> list[Posting]:
    """Tier C: cross-board breadth via Serper.dev (Google SERP API).

    The replacement for Google's Custom Search JSON API (closed to new customers
    since 2026). Serper has an open free tier (2,500 searches/month, no card).
    No-op unless SERPER_API_KEY is set.

    NOTE on the free tier: it rejects `site:` operators and complex boolean
    queries ("Query pattern not allowed for free accounts"). So `queries` must be
    plain keyword strings (no site:/quotes/OR). We run several niche queries,
    drop social-media/paper-repo junk domains, and let the LLM do the precision —
    EURAXESS, Nature, university boards still surface naturally in the results.
    """
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        log.info("  Serper skipped (SERPER_API_KEY not set)")
        return []
    tbs = os.environ.get("SERPER_TBS", "qdr:m")   # Google freshness: past month
    num = _env_int("SERPER_NUM", 20)
    seen_links: set[str] = set()
    postings: list[Posting] = []
    for q in src.get("queries", []):
        try:
            resp = requests.post(
                "https://google.serper.dev/search", timeout=HTTP_TIMEOUT,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": q, "num": num, "tbs": tbs, "gl": "de", "hl": "en"},
            )
            if resp.status_code != 200:
                log.warning("  Serper query %r HTTP %s: %s",
                            q[:40], resp.status_code, resp.text[:120])
                continue
            organic = resp.json().get("organic", [])
        except Exception as exc:
            log.warning("  Serper query failed (%s)", exc)
            continue
        for it in organic:
            link = (it.get("link") or "").strip()
            title = (it.get("title") or "").strip()
            if not link or not title or link in seen_links:
                continue
            domain = requests.compat.urlparse(link).netloc.replace("www.", "")  # type: ignore[attr-defined]
            junk = _SERPER_JUNK_LIGHT if src.get("junk_level") == "light" else _SERPER_JUNK_DOMAIN
            if junk.search(domain):
                continue  # social media / paper repos — not job postings
            seen_links.add(link)
            snippet = _clean_text(it.get("snippet", ""))
            postings.append(Posting(
                title=title[:200],
                employer=domain or src["name"],
                location=_guess_location(title + " " + snippet),
                url=link,
                source=src["name"],
                snippet=snippet,
                date=(it.get("date") or "").strip(),
            ))
        if len(postings) >= MAX_ITEMS_PER_SOURCE:
            break
    return postings


def _guess_location(text: str) -> str:
    t = (text or "").lower()
    for loc in ["münchen", "munich", "bayern", "bavaria", "freiburg",
                "berlin", "potsdam", "remote", "hybrid"]:
        if loc in t:
            return loc.title()
    return ""


def gather(sources: list[dict[str, Any]] | None = None) -> list[Posting]:
    """Run every source in `sources` (default SOURCES); failures logged & skipped."""
    sources = sources if sources is not None else SOURCES
    all_postings: list[Posting] = []
    for src in sources:
        try:
            if src["type"] == "rss":
                got = fetch_rss(src)
            elif src["type"] == "html":
                got = fetch_html(src)
            elif src["type"] == "academics":
                got = fetch_academics(src)
            elif src["type"] == "serper":
                got = fetch_serper(src)
            elif src["type"] == "google_cse":
                got = fetch_google_cse(src)
            else:
                log.warning("Unknown source type %r for %s", src["type"], src["name"])
                continue
            tag = "verified" if src.get("verified") else "UNVERIFIED"
            log.info("Source %-38s [tier %s · %-10s] -> %d items",
                     src["name"], src["tier"], tag, len(got))
            all_postings.extend(got)
        except Exception as exc:  # never let one source kill the run
            log.warning("Source %-38s FAILED: %s", src["name"], exc)
    return all_postings


# ──────────────────────────────────────────────────────────────────────────
# 6. Hard filters (cheap, no LLM — drop obvious noise before paying for tokens)
# ──────────────────────────────────────────────────────────────────────────

# Junior / non-research roles she shouldn't see (she has a PhD).
_JUNIOR = re.compile(
    r"\b(phd|ph\.d|doctoral|doktorand|promotion|werkstudent|working student|"
    r"praktik|internship|intern|ausbildung|apprentice|trainee|bachelor|master'?s? student"
    r"|studentische)\b",
    re.I,
)
_JUNIOR_SENIOR_OVERRIDE = re.compile(r"\b(senior|lead|principal|head|chief)\b", re.I)

# Clearly unrelated fields.
_UNRELATED = re.compile(
    r"\b(sales|vertrieb|accountant|accounting|nursing|nurse|pflege|"
    r"recruiter|marketing|frontend|backend|full[- ]?stack|devops|"
    r"sap consultant|salesforce|tax advisor|lawyer|attorney|"
    r"warehouse|logistics driver|barista|waiter|kassierer)\b",
    re.I,
)

# Positive domain signal — at least one of these should appear or we treat the
# posting as low-prior (still allowed through to the cheap prefilter, but this
# helps the prefilter and gives us a free skip for totally off-topic items).
_DOMAIN = re.compile(
    r"\b(thermal comfort|thermischer komfort|thermische behaglichkeit|"
    r"biometeorolog|microclimate|mikroklima|"
    r"urban climate|stadtklima|urban heat|hitze|heat island|wärmeinsel|"
    r"climate adaptation|klimaanpassung|klimaschutz|"
    r"climate.responsive|climate consultant|klimaberater|"
    r"bauphysik|building physics|gebäudephysik|"
    r"sustainable building|nachhalt|green building|dgnb|leed|"
    r"facade|fassade|urban design|stadtgestaltung|stadtplanung|"
    r"environmental meteorolog|umweltmeteorolog|"
    r"urban planning|stadtentwicklung|"
    r"pet|utci|envi-?met|rayman|skyhelios|"
    r"comfort engineering|livability|resilien|"
    r"outdoor comfort|wind comfort|solar radiation|"
    r"raumklima|innenraumklima|gebäudeklimatik)\b",
    re.I,
)

# Easy track: drop clearly-unsuitable manual / service / care / low-wage roles
# (a PhD-holder after decent office pay won't take these). Unlike the niche
# _UNRELATED list it deliberately KEEPS office/analyst/consultant/coordination
# roles so the easy-rubric LLM can judge them.
_EASY_UNSUITABLE = re.compile(
    r"\b(nurse|nursing|pflege|altenpflege|krankenpfleg|"
    r"warehouse|lagerist|lagerarbeiter|lagerhelfer|kommissionier|produktionshelfer|"
    r"driver|fahrer|lkw|kurier|paketbote|"
    r"barista|waiter|waitress|kellner|koch|gastro|küche|spülkraft|"
    r"cleaner|cleaning|reinigung|reinigungskraft|putzkraft|"
    r"security|wachmann|sicherheitsdienst|"
    r"bauarbeiter|maurer|dachdecker|installateur|schweißer|"
    r"cashier|kassierer|verkäufer|verkaeufer|retail associate|shop assistant|"
    r"friseur|hairdresser|kosmetik|"
    r"nanny|babysit|au.?pair|erzieher|kinderbetreuung|"
    r"aushilfe|minijob|450.?euro|520.?euro)\b",
    re.I,
)


def hard_filter(postings: list[Posting], *, require_domain: bool = True,
                unrelated_re: "re.Pattern[str]" = _UNRELATED) -> tuple[list[Posting], int]:
    """Return (kept, dropped_count). Drops obvious noise before LLM scoring.

    Parametrized per track: the niche track requires a domain signal on snippet
    postings (`require_domain=True`) and uses the niche `_UNRELATED` blocklist;
    the easy track sets `require_domain=False` and a broader `unrelated_re`.
    """
    kept: list[Posting] = []
    dropped = 0
    for p in postings:
        blob = f"{p.title} {p.snippet}"
        if not p.title or not p.url:
            dropped += 1
            continue
        if unrelated_re and unrelated_re.search(blob):
            dropped += 1
            continue
        if _JUNIOR.search(blob) and not _JUNIOR_SENIOR_OVERRIDE.search(blob):
            dropped += 1
            continue
        # Niche track only: keep anything with a domain signal; also keep
        # title-only postings (HTML sources) so the LLM can judge — but drop
        # clearly off-topic ones that have a snippet yet no domain signal at all.
        if require_domain and p.snippet and not _DOMAIN.search(blob):
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def dedup(postings: list[Posting]) -> list[Posting]:
    """Dedup by canonical URL id and by employer+title across sources."""
    by_id: dict[str, Posting] = {}
    by_key: dict[str, str] = {}
    for p in postings:
        if p.id in by_id:
            continue
        if p.dedup_key in by_key:
            continue
        by_id[p.id] = p
        by_key[p.dedup_key] = p.id
    return list(by_id.values())


# ──────────────────────────────────────────────────────────────────────────
# 7. LLM scoring (Anthropic / Claude) — structured output, batched, capped
# ──────────────────────────────────────────────────────────────────────────

class LLMBudget:
    """Tracks API calls so we never blow past MAX_LLM_CALLS in a run."""
    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0

    def allow(self) -> bool:
        return self.used < self.cap

    def spend(self) -> None:
        self.used += 1


class NoCreditsError(RuntimeError):
    """Raised when the Anthropic API rejects calls due to an exhausted balance."""


# Telegram alert pushed when scoring can't run because the API has no credits.
NO_CREDITS_MESSAGE = (
    "🚫 *No Jobs paused — Anthropic API out of credits*\n"
    "Job scoring couldn't run: the Anthropic API credit balance is used up and "
    "auto-reload is disabled.\n"
    "➡️ Top up at https://console.anthropic.com/settings/billing — the monitor "
    "resumes automatically on the next run once credits are added."
)


def _is_credit_error(exc: Exception) -> bool:
    """True if an exception looks like an out-of-credits / billing rejection."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    if any(s in msg for s in ("credit balance", "insufficient", "billing", "too low")):
        return True
    etype = str(getattr(exc, "type", "") or "").lower()
    return etype == "billing_error"


def _anthropic_client():
    import anthropic  # imported here so --test works without the dep installed
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


_PREFILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                },
                "required": ["id", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "reason": {"type": "string"},
                    "location_fit": {
                        "type": "string",
                        "enum": ["munich", "remote", "germany", "eu", "elsewhere", "unknown"],
                    },
                    "language_flag": {
                        "type": "string",
                        "enum": ["english", "german_required", "unknown"],
                    },
                    "work_mode": {
                        "type": "string",
                        "enum": ["remote", "hybrid", "onsite", "unknown"],
                    },
                },
                "required": ["id", "score", "reason", "location_fit", "language_flag", "work_mode"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _postings_block(postings: list[Posting]) -> str:
    lines = []
    for p in postings:
        lines.append(json.dumps({
            "id": p.id,
            "title": p.title,
            "employer": p.employer,
            "location": p.location,
            "source": p.source,
            "snippet": p.snippet[:400],
        }, ensure_ascii=False))
    return "\n".join(lines)


def _call_structured(client, model: str, system: str, user: str, schema: dict) -> dict:
    """One Messages API call constrained to a JSON schema. Returns parsed dict."""
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},  # rubric is stable -> cache it
            }],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as exc:
        # Surface out-of-credits distinctly so run() can alert via Telegram.
        if _is_credit_error(exc):
            raise NoCreditsError(str(getattr(exc, "message", "") or exc)) from exc
        raise
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def prefilter(client, postings: list[Posting], budget: LLMBudget, track: "Track") -> list[Posting]:
    """Cheap Haiku pass: coarse 0-100 relevance, keep >= track.prefilter_min."""
    if not postings:
        return []
    system = track.rubric + "\n\n" + track.prefilter_instructions
    survivors: list[Posting] = []
    for batch in _chunk(postings, SCORE_BATCH):
        if not budget.allow():
            log.warning("  LLM call cap reached during prefilter; "
                        "passing %d remaining postings straight to scoring",
                        len(postings) - postings.index(batch[0]))
            survivors.extend(batch)  # don't silently drop — let scoring judge
            continue
        user = "Postings (one JSON per line):\n" + _postings_block(batch)
        try:
            budget.spend()
            data = _call_structured(client, PREFILTER_MODEL, system, user, _PREFILTER_SCHEMA)
            scores = {r["id"]: int(r.get("score", 0)) for r in data.get("results", [])}
            for p in batch:
                if scores.get(p.id, 0) >= track.prefilter_min:
                    survivors.append(p)
        except NoCreditsError:
            raise  # bubble up so run() can send the Telegram alert
        except Exception as exc:
            log.warning("  prefilter batch failed (%s); passing batch to scoring", exc)
            survivors.extend(batch)  # fail open: precision is enforced at scoring
    log.info("Prefilter (%s): %d -> %d survivors", PREFILTER_MODEL,
             len(postings), len(survivors))
    return survivors


def score(client, postings: list[Posting], budget: LLMBudget, track: "Track") -> list[Posting]:
    """Precise Opus pass: fills score/reason/location_fit/language_flag/work_mode in place."""
    if not postings:
        return []
    system = track.rubric + "\n\n" + track.scoring_instructions
    scored: list[Posting] = []
    for batch in _chunk(postings, SCORE_BATCH):
        if not budget.allow():
            log.warning("  LLM call cap reached during scoring; "
                        "%d postings left unscored this run", len(postings) - len(scored))
            break
        user = "Score each posting (one JSON per line):\n" + _postings_block(batch)
        try:
            budget.spend()
            data = _call_structured(client, SCORING_MODEL, system, user, _SCORE_SCHEMA)
            by_id = {r["id"]: r for r in data.get("results", [])}
            for p in batch:
                r = by_id.get(p.id)
                if not r:
                    continue
                p.score = int(r.get("score", 0))
                p.reason = (r.get("reason") or "").strip()
                p.location_fit = r.get("location_fit", "unknown")
                p.language_flag = r.get("language_flag", "unknown")
                p.work_mode = r.get("work_mode", "unknown")
                scored.append(p)
        except NoCreditsError:
            raise  # bubble up so run() can send the Telegram alert
        except Exception as exc:
            log.warning("  scoring batch failed (%s); skipping batch", exc)
    log.info("Scoring (%s): %d postings scored (LLM calls used: %d/%d)",
             SCORING_MODEL, len(scored), budget.used, budget.cap)
    return scored


def _chunk(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ──────────────────────────────────────────────────────────────────────────
# 7b. Page enrichment — the feed snippet rarely states the application deadline,
#     the work mode (remote/hybrid/on-site), the work language or the exact
#     location. Those facts live in the full job page. So for each actionable
#     match we fetch the page once and let the cheap model pull ALL of them in a
#     single call (no extra cost over the old deadline-only pass). Page values
#     are authoritative: they overwrite the snippet-based guesses from scoring.
# ──────────────────────────────────────────────────────────────────────────

_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    # ISO date, or "rolling" for ongoing/no fixed date, or
                    # "unknown" if the page states no deadline.
                    "deadline": {"type": "string"},
                    "work_mode": {
                        "type": "string",
                        "enum": ["remote", "hybrid", "onsite", "unknown"],
                    },
                    "language": {
                        "type": "string",
                        "enum": ["english", "german_required", "unknown"],
                    },
                    "location_fit": {
                        "type": "string",
                        "enum": ["munich", "remote", "germany", "eu", "elsewhere", "unknown"],
                    },
                },
                "required": ["id", "deadline", "work_mode", "language", "location_fit"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fetch_page_text(url: str) -> str:
    """Best-effort plain text of a job page (for deadline extraction). Fail-open."""
    try:
        resp = _http_get(url)
    except Exception as exc:
        log.warning("  deadline: could not fetch %s (%s)", url, exc)
        return ""
    text = _clean_text(resp.text)
    # _clean_text caps at 600; re-extract with a wider window for deadlines.
    if BeautifulSoup is not None:
        try:
            text = re.sub(r"\s+", " ",
                          BeautifulSoup(resp.text, "html.parser").get_text(" ")).strip()
        except Exception:
            pass
    return text[:DEADLINE_PAGE_CHARS]


def enrich_details(client, postings: list[Posting], budget: LLMBudget) -> None:
    """Fetch each posting's full page and fill in deadline + work_mode + language
    + location_fit (the snippet rarely has them). Page values overwrite the
    snippet-based guesses from scoring; fail-open and budget-capped."""
    if not postings:
        return
    today = _today_iso()
    system = (
        "You read a job posting's full page text and extract structured facts. "
        f"Today is {today}. The candidate is based in MUNICH, Germany and can work "
        "in Munich/Bavaria on-site OR fully remote. For each id return:\n"
        '  deadline: an ISO date "YYYY-MM-DD" if a clear application deadline / '
        '"Bewerbungsfrist" / "Bewerbungsschluss" / "apply by" / "closing date" is stated '
        "(resolve relative phrases like 'within 3 weeks' against today); "
        '"rolling" if ongoing / "laufend" / "bis zur Besetzung" / until filled / no fixed date; '
        '"unknown" if no deadline info is present.\n'
        '  work_mode: "remote" if fully remote / home-office / ortsunabhängig; '
        '"hybrid" if partly on-site and partly remote; "onsite" if it must be done at a fixed '
        'workplace; "unknown" if unclear.\n'
        '  language: "english" if the work language is English or the posting says English '
        'is sufficient; "german_required" if fluent/native German is required; "unknown" if unclear.\n'
        '  location_fit: relative to the Munich candidate — "munich" if the workplace is '
        'Munich/Greater Munich/Bavaria; "remote" if fully remote (workable from Munich); '
        '"germany" if elsewhere in Germany on-site; "eu" if elsewhere in the EU on-site; '
        '"elsewhere" if outside the EU on-site; "unknown" if unclear.\n'
        "Do not guess — use \"unknown\" when the page does not say. Output only these fields."
    )
    pages: list[dict[str, str]] = []
    for p in postings:
        txt = fetch_page_text(p.url)
        if txt:
            pages.append({"id": p.id, "title": p.title, "page_text": txt})
    if not pages:
        return
    by_id = {p.id: p for p in postings}
    for batch in _chunk(pages, SCORE_BATCH):
        if not budget.allow():
            log.warning("  enrich: LLM budget reached; %d postings left un-enriched",
                        len(pages) - pages.index(batch[0]))
            break
        user = "Extract the facts for each posting:\n" + "\n".join(
            json.dumps(b, ensure_ascii=False) for b in batch)
        try:
            budget.spend()
            data = _call_structured(client, PREFILTER_MODEL, system, user, _ENRICH_SCHEMA)
            for r in data.get("results", []):
                p = by_id.get(r.get("id", ""))
                if not p:
                    continue
                d = (r.get("deadline") or "").strip().lower()
                if d == "rolling":
                    p.deadline = "rolling"
                elif _ISO_DATE_RE.match(d):
                    p.deadline = d
                # "unknown" / anything else -> leave deadline as-is (unknown)
                wm = (r.get("work_mode") or "").strip().lower()
                if wm in ("remote", "hybrid", "onsite"):
                    p.work_mode = wm
                lang = (r.get("language") or "").strip().lower()
                if lang in ("english", "german_required"):
                    p.language_flag = lang   # page is fuller than the snippet -> trust it
                lf = (r.get("location_fit") or "").strip().lower()
                if lf in ("munich", "remote", "germany", "eu", "elsewhere"):
                    p.location_fit = lf
        except NoCreditsError:
            raise
        except Exception as exc:
            log.warning("  enrich batch failed (%s); leaving those unknown", exc)
    got_dl = sum(1 for p in postings if p.deadline)
    got_wm = sum(1 for p in postings if p.work_mode != "unknown")
    log.info("Page enrichment: %d pages read; deadline %d/%d, work-mode %d/%d (LLM calls: %d/%d)",
             len(pages), got_dl, len(postings), got_wm, len(postings), budget.used, budget.cap)


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _in_days(n: int) -> str:
    """ISO date n days from today (used for the --test sample)."""
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


def _days_until(iso_date: str) -> int | None:
    """Whole days from today (UTC) to an ISO date; None if unparseable."""
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - datetime.now(timezone.utc).date()).days


def assign_priority(p: Posting) -> None:
    """Set p.priority from deadline urgency (primary) and score (tie-break)."""
    if p.deadline == "rolling":
        p.priority = "🟢 LAUFEND"
        return
    days = _days_until(p.deadline) if p.deadline else None
    if days is None:
        p.priority = "⚪ FRIST UNBEKANNT"
    elif days < 0:
        p.priority = "⚫ ABGELAUFEN"
    elif days <= DEADLINE_SOON_DAYS:
        p.priority = f"🔴 DRINGEND · {days} Tage"
    elif days <= DEADLINE_WATCH_DAYS:
        p.priority = f"🟠 BALD · {days} Tage"
    else:
        p.priority = f"🟢 ZEIT · {days} Tage"


def _priority_rank(p: Posting) -> tuple[int, int]:
    """Sort key: more urgent first, then higher score. Lower tuple = higher up."""
    days = _days_until(p.deadline) if p.deadline and p.deadline != "rolling" else None
    if days is not None and days >= 0 and days <= DEADLINE_SOON_DAYS:
        bucket = 0
    elif days is not None and days >= 0 and days <= DEADLINE_WATCH_DAYS:
        bucket = 1
    elif p.deadline == "rolling" or (days is not None and days > DEADLINE_WATCH_DAYS):
        bucket = 2
    elif days is not None and days < 0:
        bucket = 4          # expired -> bottom
    else:
        bucket = 3          # unknown deadline
    return (bucket, -p.score)


def _deadline_label(p: Posting) -> str:
    """Human deadline line for the Telegram message."""
    if p.deadline == "rolling":
        return "⏳ Bewerbung laufend / bis zur Besetzung"
    if not p.deadline:
        return "⏳ Bewerbungsfrist unbekannt — auf der Seite prüfen"
    days = _days_until(p.deadline)
    if days is None:
        return f"⏳ Bewerbungsfrist: {p.deadline}"
    if days < 0:
        return f"⏳ Frist abgelaufen ({p.deadline})"
    if days == 0:
        return f"⏳ Bewerbungsfrist: HEUTE ({p.deadline})"
    return f"⏳ Bewerbungsfrist: {p.deadline} (in {days} Tagen)"


# ──────────────────────────────────────────────────────────────────────────
# 8. Notifier (Telegram)
# ──────────────────────────────────────────────────────────────────────────

def _stars(score: int) -> str:
    filled = max(1, min(5, round(score / 20)))
    return "⭐" * filled


def _lang_label(flag: str) -> str:
    return {
        "english": "English",
        "german_required": "Deutsch erforderlich",
    }.get(flag, "Sprache unklar")


def _work_mode_label(mode: str) -> str:
    """Short badge for the work mode; '' (hidden) when unknown."""
    return {
        "remote": "🏠 Remote",
        "hybrid": "🔀 Hybrid",
        "onsite": "🏢 Vor Ort",
    }.get(mode, "")


def _loc_label(p: Posting) -> str:
    label = {
        "munich": "München/Bayern",
        "remote": "Remote",
        "germany": "Deutschland",
        "eu": "EU",
        "elsewhere": "Andernorts",
    }.get(p.location_fit, p.location or "Ort unklar")
    return label


def format_message(p: Posting, tag: str = "") -> str:
    """Telegram Markdown message in the spec's format. `tag` prepends a header
    line (used by the easy-jobs track so the second stream is unmistakable)."""
    lines: list[str] = []
    if tag:
        lines.append(f"🟦 *{_md(tag)}*")
    lines.append(f"💼 *{_md(p.title)}*  {_stars(p.score)}{p.score}/100")
    if p.priority:
        lines.append(f"🚦 *{_md(p.priority)}*")
    locline = f"📍 {_md(_loc_label(p))}"
    wm = _work_mode_label(p.work_mode)
    if wm:
        locline += f" · {wm}"
    locline += f"   🗣 {_lang_label(p.language_flag)}"
    lines += [
        f"🏢 {_md(p.employer)} · _{_md(p.source)}_",
        locline,
        _md(_deadline_label(p)),
    ]
    if p.reason:
        lines.append(f"🎯 {_md(p.reason)}")
    if p.date:
        lines.append(f"🗓 {_md(p.date)}")
    lines.append(f"🔗 {p.url}")
    return "\n".join(lines)


def format_digest(postings: list[Posting], title: str, subtitle: str) -> str:
    """One compact overview message of the best available postings (digest run)."""
    lines = [f"*{_md(title)}*", f"_{_md(subtitle)}_", ""]
    for i, p in enumerate(postings, 1):
        prio = f"  {p.priority}" if p.priority else ""
        lines.append(f"{i}. *{p.score}/100*{_md(prio)}")
        lines.append(f"   {_md(p.title[:90])}")
        locbits = _loc_label(p)
        wm = _work_mode_label(p.work_mode)
        if wm:
            locbits += f" · {wm}"
        lines.append(f"   📍 {_md(locbits)} · _{_md(p.source)}_")
        lines.append(f"   🔗 {p.url}")
    return "\n".join(lines)


def _md(s: str) -> str:
    """Escape Telegram *legacy* Markdown special chars in free text."""
    return re.sub(r"([_*\[\]`])", r"\\\1", s or "")


def push_telegram(text: str, chat_ids: list[str] | None = None) -> bool:
    """Send `text` to each chat id. Defaults to TELEGRAM_CHAT_ID (the niche track
    and the out-of-credits alert); the easy track passes its own chat ids. The
    bot TOKEN is shared across both tracks."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if chat_ids is None:
        raw_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]
    if not token or not chat_ids:
        log.error("TELEGRAM_TOKEN / chat id not set — cannot push.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    all_ok = True
    for chat_id in chat_ids:
        try:
            resp = requests.post(url, timeout=HTTP_TIMEOUT, data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "false",
            })
            # Telegram rejects malformed legacy-Markdown (e.g. an unbalanced _ in a
            # URL) with 400. Rather than lose the message, resend as plain text so
            # the content always gets through (just without bold/italic).
            if resp.status_code == 400:
                log.warning("Telegram 400 for %s (%s) — retrying as plain text",
                            chat_id, resp.text[:120])
                resp = requests.post(url, timeout=HTTP_TIMEOUT, data={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": "false",
                })
            if resp.status_code != 200:
                log.error("Telegram push failed for %s — %s: %s",
                          chat_id, resp.status_code, resp.text[:200])
                all_ok = False
        except Exception as exc:
            log.error("Telegram push error for %s: %s", chat_id, exc)
            all_ok = False
    return all_ok


# ──────────────────────────────────────────────────────────────────────────
# 9. State (seen.json / matches.json) — committed back by CI
# ──────────────────────────────────────────────────────────────────────────

def load_seen(path: str | None = None) -> dict[str, Any]:
    path = path or SEEN_FILE
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Could not read %s (%s); starting fresh", path, exc)
    return {}


def save_seen(seen: dict[str, Any], path: str | None = None) -> None:
    with open(path or SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)


def append_matches(records: list[dict[str, Any]], path: str | None = None) -> None:
    path = path or MATCHES_FILE
    backlog: list[dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                backlog = json.load(f)
        except Exception:
            backlog = []
    backlog.extend(records)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backlog, f, ensure_ascii=False, indent=2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────────────────
# 9b. Tracks — the niche stream and the "No Easy Jobs for Rose" easy stream
# ──────────────────────────────────────────────────────────────────────────
# A Track bundles everything that differs between the two parallel streams:
# rubric/instructions, sources, hard-filter behaviour, thresholds, LLM budget,
# state files, the Telegram chat env var, and the message/digest presentation.

@dataclass
class Track:
    key: str                       # "main" | "easy"
    label: str                     # human name for logs
    rubric: str
    scoring_instructions: str
    prefilter_instructions: str
    sources: list[dict[str, Any]]
    seen_file: str
    matches_file: str
    chat_env: str                  # env var holding this track's chat id(s)
    min_score: int
    maybe_min: int
    prefilter_min: int
    max_llm_calls: int
    require_domain: bool           # hard filter: niche -> True, easy -> False
    unrelated_re: "re.Pattern[str]"
    location_whitelist: set[str] | None   # easy -> {"munich","remote"}; niche -> None
    tag: str                       # per-message header (easy only; "" hides it)
    digest_title: str              # "{n}" placeholder for the count
    digest_subtitle: str

    def chat_ids(self) -> list[str]:
        raw = os.environ.get(self.chat_env, "").strip()
        return [c.strip() for c in raw.split(",") if c.strip()]


MAIN_TRACK = Track(
    key="main",
    label="Niche fit · No Jobs for Rose",
    rubric=CANDIDATE_RUBRIC,
    scoring_instructions=SCORING_INSTRUCTIONS,
    prefilter_instructions=MAIN_PREFILTER_INSTRUCTIONS,
    sources=SOURCES,
    seen_file=SEEN_FILE,
    matches_file=MATCHES_FILE,
    chat_env="TELEGRAM_CHAT_ID",
    min_score=MIN_FIT_SCORE,
    maybe_min=MAYBE_MIN_SCORE,
    prefilter_min=PREFILTER_MIN,
    max_llm_calls=MAX_LLM_CALLS,
    require_domain=True,
    unrelated_re=_UNRELATED,
    location_whitelist=None,
    tag="",
    digest_title="🗓 Tages-Übersicht — Top {n} aktuelle Stellen",
    digest_subtitle="Das Beste, was die Quellen gerade hergeben (auch unter der Ping-Schwelle):",
)

EASY_TRACK = Track(
    key="easy",
    label="Easy / adjacent · No Easy Jobs for Rose",
    rubric=EASY_RUBRIC,
    scoring_instructions=EASY_SCORING_INSTRUCTIONS,
    prefilter_instructions=EASY_PREFILTER_INSTRUCTIONS,
    sources=EASY_SOURCES,
    seen_file=EASY_SEEN_FILE,
    matches_file=EASY_MATCHES_FILE,
    chat_env="TELEGRAM_EASY_CHAT_ID",
    min_score=EASY_MIN_SCORE,
    maybe_min=EASY_MAYBE_MIN,
    prefilter_min=EASY_PREFILTER_MIN,
    max_llm_calls=EASY_MAX_LLM_CALLS,
    require_domain=False,
    unrelated_re=_EASY_UNSUITABLE,
    location_whitelist={"munich", "remote"},
    tag="Easy-Job · schnell & solide bezahlt · München/Remote",
    digest_title="🟦 Easy-Jobs — Top {n} schnelle, solide Stellen (München/Remote)",
    digest_subtitle="Schnell erreichbare, anständig bezahlte Jobs in München oder remote:",
)

# Tracks in run order. The easy track is gated at runtime on EASY_ENABLED and on
# its chat id being set (see run()).
TRACKS: list[Track] = [MAIN_TRACK, EASY_TRACK]


def _location_ok(p: Posting, whitelist: set[str]) -> bool:
    """Easy track gate: keep only Munich-area / remote roles. A fully-remote
    work_mode qualifies regardless of where the employer sits (she works from Munich)."""
    if p.work_mode == "remote":
        return True
    return p.location_fit in whitelist


# ──────────────────────────────────────────────────────────────────────────
# 10. Orchestration
# ──────────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False, only_track: str | None = None) -> int:
    """Run all enabled tracks. The Anthropic client and the once-a-day digest
    check are shared; each track keeps its own sources, LLM budget and state.
    Returns 0 on success, 2 if the API key/client is missing, 3 if out of credits."""
    log.info("=== Job-Monitor run @ %s ===", _now())

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        log.error("ANTHROPIC_API_KEY not set — cannot score. Aborting (no state change).")
        return 2
    try:
        client = _anthropic_client()
    except Exception as exc:
        log.error("Could not init Anthropic client: %s", exc)
        return 2

    # Is this the once-a-day digest run? (first daytime cron, or forced for tests)
    digest_run = DIGEST_ENABLED and (
        os.environ.get("DIGEST_FORCE", "").strip() in ("1", "true")
        or datetime.now(timezone.utc).hour == DIGEST_HOUR_UTC
    )

    rc = 0
    for track in TRACKS:
        if only_track and track.key != only_track:
            continue
        if track.key == "easy" and not EASY_ENABLED:
            log.info("Track 'easy' disabled (EASY_ENABLED=0) — skipping.")
            continue
        # The easy track needs its own chat id; without it (and not a dry-run)
        # there's nowhere to push, so don't spend API calls scoring it.
        if track.key != "main" and not dry_run and not track.chat_ids():
            log.info("Track '%s' skipped: %s not set (no chat to push to).",
                     track.key, track.chat_env)
            continue
        try:
            rc = run_track(client, track, dry_run=dry_run, digest_run=digest_run) or rc
        except NoCreditsError as exc:
            log.error("Anthropic API out of credits (%s) — alerting via Telegram", exc)
            push_telegram(NO_CREDITS_MESSAGE)  # always to the main chat
            return 3
    return rc


def run_track(client, track: Track, dry_run: bool = False, digest_run: bool = False) -> int:
    """One track end-to-end: gather -> filter -> prefilter -> score -> enrich ->
    push -> persist, using the track's own rubric, sources, thresholds and state."""
    log.info("--- Track '%s': %s ---", track.key, track.label)

    seen = load_seen(track.seen_file)
    log.info("Loaded %d seen ids (%s)", len(seen), track.seen_file)

    # gather -> dedup -> drop seen -> hard filter
    raw = gather(track.sources)
    log.info("Gathered %d raw postings from %d sources", len(raw), len(track.sources))

    unique = dedup(raw)
    fresh = [p for p in unique if p.id not in seen]
    log.info("After dedup: %d ; after dropping seen: %d", len(unique), len(fresh))

    kept, dropped = hard_filter(fresh, require_domain=track.require_domain,
                                unrelated_re=track.unrelated_re)
    log.info("Hard filters: kept %d, dropped %d", len(kept), dropped)

    if not kept:
        log.info("Nothing to score for track '%s'. Done.", track.key)
        return 0

    # LLM: prefilter (Haiku) -> score (Opus), under this track's own budget.
    budget = LLMBudget(track.max_llm_calls)
    survivors = prefilter(client, kept, budget, track)
    scored = score(client, survivors, budget, track)
    scored.sort(key=lambda p: p.score, reverse=True)

    pushes = [p for p in scored if p.score >= track.min_score]
    maybes = [p for p in scored if track.maybe_min <= p.score < track.min_score]
    log.info("Results: %d push (>=%d), %d maybe (%d-%d)",
             len(pushes), track.min_score, len(maybes), track.maybe_min, track.min_score - 1)

    # Digest = the best below-threshold postings she'd otherwise never see.
    digest_extra = [p for p in scored if p.score < track.min_score][:DIGEST_TOP_N] \
        if digest_run else []

    # Enrich the actionable matches (+ digest entries) with deadline + work_mode
    # + language + location (fetched from each job page) and an urgency label.
    actionable = pushes + maybes + [p for p in digest_extra if p not in maybes]
    if actionable:
        enrich_details(client, actionable, LLMBudget(DEADLINE_MAX_CALLS))
        for p in actionable:
            assign_priority(p)

    # Easy track is strict on location: only Munich-area or remote may ping or
    # appear in the digest. Applied AFTER enrichment so we use the accurate
    # work_mode / location_fit from the page, not the snippet guess.
    gate_dropped: list[Posting] = []
    if track.location_whitelist is not None:
        ok = [p for p in pushes if _location_ok(p, track.location_whitelist)]
        gate_dropped = [p for p in pushes if p not in ok]
        pushes = ok
        digest_extra = [p for p in digest_extra if _location_ok(p, track.location_whitelist)]
        if gate_dropped:
            log.info("Location gate (Munich/remote): dropped %d non-local push(es)",
                     len(gate_dropped))

    # Most urgent first within the push (deadline buckets, then score).
    pushes.sort(key=_priority_rank)

    if dry_run:
        log.info("--dry-run [%s]: not pushing or persisting. Top results:", track.key)
        for p in (pushes + maybes)[:15]:
            log.info("  %3d  %-9s  %-7s  %-20s  %s",
                     p.score, p.location_fit, p.work_mode, p.priority or "—", p.title[:55])
        if digest_run:
            log.info("--dry-run [%s]: would send daily digest of %d postings",
                     track.key, len(digest_extra))
        return 0

    chat_ids = track.chat_ids()

    # Push the strong matches (precision over recall on the ping).
    pushed_ok = 0
    for p in pushes:
        if push_telegram(format_message(p, tag=track.tag), chat_ids):
            pushed_ok += 1
            seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(), "pushed": True}
            time.sleep(0.7)  # gentle rate-limit between messages
        else:
            log.warning("  push failed for %s — leaving unseen to retry next run", p.title[:60])

    # Daily digest: one overview of the best below-threshold postings so she
    # always sees what's out there. Mark them seen so the digest never repeats.
    if digest_run and digest_extra:
        title = track.digest_title.format(n=len(digest_extra))
        if push_telegram(format_digest(digest_extra, title, track.digest_subtitle), chat_ids):
            for p in digest_extra:
                seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(),
                              "pushed": False, "digest": True}
            log.info("Sent daily digest of %d postings", len(digest_extra))
        else:
            log.warning("  digest push failed — leaving those unseen to retry")

    # Mark maybes as seen too (so we don't re-evaluate them), logged as "maybe".
    for p in maybes:
        seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(), "pushed": False}
    # Strong matches the location gate rejected: mark seen so we don't re-fetch
    # and re-enrich them every run (they can never ping on this track).
    for p in gate_dropped:
        seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(),
                      "pushed": False, "gated": "location"}

    # Append everything scored to the audit backlog.
    records = [
        {**asdict(p), "id": p.id, "kind": "push" if p.score >= track.min_score
         else ("maybe" if p.score >= track.maybe_min else "low"), "ts": _now()}
        for p in scored
    ]
    append_matches(records, track.matches_file)
    save_seen(seen, track.seen_file)

    log.info("Track '%s' done. Pushed %d/%d; %d maybes; seen=%d; LLM calls=%d/%d",
             track.key, pushed_ok, len(pushes), len(maybes), len(seen),
             budget.used, budget.cap)
    return 0


def run_test() -> int:
    """Push one sample match end-to-end (no scraping, no LLM)."""
    log.info("--test: pushing one sample match to Telegram")
    sample = Posting(
        title="Postdoctoral Researcher — Urban Microclimate & Thermal Comfort",
        employer="TU München · School of Engineering and Design",
        location="München",
        url="https://example.com/jobs/postdoc-urban-microclimate",
        source="EURAXESS (sample)",
        snippet="Research on outdoor thermal comfort (PET/UTCI), ENVI-met simulation, "
                "urban heat resilience. English-speaking team.",
        date="2026-06-17",
        score=92,
        reason="Deep niche match: outdoor thermal comfort + urban microclimate in Munich.",
        location_fit="munich",
        language_flag="english",
        work_mode="hybrid",    # sample: shows the new 🔀 Hybrid badge
        deadline=_in_days(5),  # sample: deadline 5 days out -> 🔴 DRINGEND
    )
    assign_priority(sample)
    ok = push_telegram(format_message(sample))
    if ok:
        log.info("Sample match pushed successfully.")
        return 0
    log.error("Sample push failed — check TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job-Monitor für Dr. Sevil Zafarmandi")
    parser.add_argument("--test", action="store_true",
                        help="push one sample match end-to-end (no scrape/LLM)")
    parser.add_argument("--dry-run", action="store_true",
                        help="gather + score but do not push or persist state")
    parser.add_argument("--track", choices=["main", "easy", "both"], default="both",
                        help="which track(s) to run: niche 'main', 'easy', or 'both' (default)")
    args = parser.parse_args(argv)

    if args.test:
        return run_test()
    only = None if args.track == "both" else args.track
    return run(dry_run=args.dry_run, only_track=only)


if __name__ == "__main__":
    sys.exit(main())
