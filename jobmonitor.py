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
from datetime import datetime, timezone
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


MIN_FIT_SCORE   = _env_int("MIN_FIT_SCORE", 70)      # nur Treffer >= diesem Score pushen
MAYBE_MIN_SCORE = _env_int("MAYBE_MIN_SCORE", 50)    # 50-69 -> als "maybe" in matches.json loggen
PREFILTER_MIN   = _env_int("PREFILTER_MIN", 40)      # Haiku-Cutoff vor dem teuren Opus-Scoring

LOCATIONS = [
    "München", "Munich", "Bayern", "Bavaria",
    "remote", "EU remote", "remote Germany", "hybrid Munich",
]

MAX_LLM_CALLS   = _env_int("MAX_LLM_CALLS", 40)      # Kostendeckel pro Lauf (gesamt, beide Stufen)
SCORE_BATCH     = _env_int("SCORE_BATCH", 8)         # Postings pro LLM-Call (Batching senkt Kosten)

SCORING_MODEL   = os.environ.get("SCORING_MODEL", "claude-opus-4-8")
PREFILTER_MODEL = os.environ.get("PREFILTER_MODEL", "claude-haiku-4-5")

CHECK_CRON      = "0 */4 * * *"                       # alle 4 Stunden (siehe workflow yml)

# Files committed back to the repo (state, like a seen.json in a flat scraper).
SEEN_FILE       = os.environ.get("SEEN_FILE", "seen.json")
MATCHES_FILE    = os.environ.get("MATCHES_FILE", "matches.json")

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
"""


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
    # ---- Tier C: general search, queried with her keyword combos ----
    # Google Programmable Search (Custom Search JSON API). No-op unless BOTH
    # GOOGLE_API_KEY and GOOGLE_CSE_ID are set — see README. This is where the
    # niche-but-industry roles (consultancies, climate analytics) surface.
    {
        "name": "Google Programmable Search",
        "tier": "C",
        "type": "google_cse",
        "query": (
            '("thermal comfort" OR biometeorology OR "urban climate" OR '
            '"urban microclimate" OR "climate adaptation" OR Bauphysik OR '
            '"sustainable architecture") (postdoc OR researcher OR scientist OR '
            'consultant OR engineer) (München OR Munich OR remote OR EU)'
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


def _guess_location(text: str) -> str:
    t = (text or "").lower()
    for loc in ["münchen", "munich", "bayern", "bavaria", "freiburg",
                "berlin", "potsdam", "remote", "hybrid"]:
        if loc in t:
            return loc.title()
    return ""


def gather() -> list[Posting]:
    """Run every source; failures are logged and skipped, never fatal."""
    all_postings: list[Posting] = []
    for src in SOURCES:
        try:
            if src["type"] == "rss":
                got = fetch_rss(src)
            elif src["type"] == "html":
                got = fetch_html(src)
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


def hard_filter(postings: list[Posting]) -> tuple[list[Posting], int]:
    """Return (kept, dropped_count). Drops obvious noise before LLM scoring."""
    kept: list[Posting] = []
    dropped = 0
    for p in postings:
        blob = f"{p.title} {p.snippet}"
        if not p.title or not p.url:
            dropped += 1
            continue
        if _UNRELATED.search(blob):
            dropped += 1
            continue
        if _JUNIOR.search(blob) and not _JUNIOR_SENIOR_OVERRIDE.search(blob):
            dropped += 1
            continue
        # Keep anything with a domain signal; also keep title-only postings
        # (HTML sources) so the LLM can judge — but drop clearly off-topic
        # ones that have a snippet yet no domain signal at all.
        if p.snippet and not _DOMAIN.search(blob):
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
                },
                "required": ["id", "score", "reason", "location_fit", "language_flag"],
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


def prefilter(client, postings: list[Posting], budget: LLMBudget) -> list[Posting]:
    """Cheap Haiku pass: coarse 0-100 relevance, keep >= PREFILTER_MIN."""
    if not postings:
        return []
    system = CANDIDATE_RUBRIC + "\n\n" + (
        "Quickly rate each posting's RELEVANCE to the candidate's domain on 0-100 "
        "(domain overlap + seniority). This is a coarse first pass — be generous to "
        "anything plausibly in-domain, harsh on clearly off-topic roles. "
        "Return only id and score for each."
    )
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
                if scores.get(p.id, 0) >= PREFILTER_MIN:
                    survivors.append(p)
        except NoCreditsError:
            raise  # bubble up so run() can send the Telegram alert
        except Exception as exc:
            log.warning("  prefilter batch failed (%s); passing batch to scoring", exc)
            survivors.extend(batch)  # fail open: precision is enforced at scoring
    log.info("Prefilter (%s): %d -> %d survivors", PREFILTER_MODEL,
             len(postings), len(survivors))
    return survivors


def score(client, postings: list[Posting], budget: LLMBudget) -> list[Posting]:
    """Precise Opus pass: fills score/reason/location_fit/language_flag in place."""
    if not postings:
        return []
    system = CANDIDATE_RUBRIC + "\n\n" + SCORING_INSTRUCTIONS
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


def _loc_label(p: Posting) -> str:
    label = {
        "munich": "München/Bayern",
        "remote": "Remote",
        "germany": "Deutschland",
        "eu": "EU",
        "elsewhere": "Andernorts",
    }.get(p.location_fit, p.location or "Ort unklar")
    return label


def format_message(p: Posting) -> str:
    """Telegram Markdown message in the spec's format."""
    lines = [
        f"💼 *{_md(p.title)}*  {_stars(p.score)}{p.score}/100",
        f"🏢 {_md(p.employer)} · _{_md(p.source)}_",
        f"📍 {_md(_loc_label(p))}   🗣 {_lang_label(p.language_flag)}",
    ]
    if p.reason:
        lines.append(f"🎯 {_md(p.reason)}")
    if p.date:
        lines.append(f"🗓 {_md(p.date)}")
    lines.append(f"🔗 {p.url}")
    return "\n".join(lines)


def _md(s: str) -> str:
    """Escape Telegram *legacy* Markdown special chars in free text."""
    return re.sub(r"([_*\[\]`])", r"\\\1", s or "")


def push_telegram(text: str) -> bool:
    """Send to all chat IDs in TELEGRAM_CHAT_ID (comma-separated for multiple recipients)."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    raw_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not raw_ids:
        log.error("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set — cannot push.")
        return False
    chat_ids = [c.strip() for c in raw_ids.split(",") if c.strip()]
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

def load_seen() -> dict[str, Any]:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Could not read %s (%s); starting fresh", SEEN_FILE, exc)
    return {}


def save_seen(seen: dict[str, Any]) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)


def append_matches(records: list[dict[str, Any]]) -> None:
    backlog: list[dict[str, Any]] = []
    if os.path.exists(MATCHES_FILE):
        try:
            with open(MATCHES_FILE, encoding="utf-8") as f:
                backlog = json.load(f)
        except Exception:
            backlog = []
    backlog.extend(records)
    with open(MATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(backlog, f, ensure_ascii=False, indent=2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────────────────
# 10. Orchestration
# ──────────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> int:
    log.info("=== Job-Monitor run @ %s ===", _now())

    seen = load_seen()
    log.info("Loaded %d seen ids", len(seen))

    # gather -> dedup -> drop seen -> hard filter
    raw = gather()
    log.info("Gathered %d raw postings from %d sources", len(raw), len(SOURCES))

    unique = dedup(raw)
    fresh = [p for p in unique if p.id not in seen]
    log.info("After dedup: %d ; after dropping seen: %d", len(unique), len(fresh))

    kept, dropped = hard_filter(fresh)
    log.info("Hard filters: kept %d, dropped %d", len(kept), dropped)

    if not kept:
        log.info("Nothing to score. Done.")
        return 0

    # LLM: prefilter (Haiku) -> score (Opus), under a shared call budget
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        log.error("ANTHROPIC_API_KEY not set — cannot score. Aborting (no state change).")
        return 2

    budget = LLMBudget(MAX_LLM_CALLS)
    try:
        client = _anthropic_client()
    except Exception as exc:
        log.error("Could not init Anthropic client: %s", exc)
        return 2

    try:
        survivors = prefilter(client, kept, budget)
        scored = score(client, survivors, budget)
    except NoCreditsError as exc:
        log.error("Anthropic API out of credits (%s) — alerting via Telegram", exc)
        push_telegram(NO_CREDITS_MESSAGE)
        return 3
    scored.sort(key=lambda p: p.score, reverse=True)

    pushes = [p for p in scored if p.score >= MIN_FIT_SCORE]
    maybes = [p for p in scored if MAYBE_MIN_SCORE <= p.score < MIN_FIT_SCORE]
    log.info("Results: %d push (>=%d), %d maybe (%d-%d)",
             len(pushes), MIN_FIT_SCORE, len(maybes), MAYBE_MIN_SCORE, MIN_FIT_SCORE - 1)

    if dry_run:
        log.info("--dry-run: not pushing or persisting. Top results:")
        for p in (pushes + maybes)[:15]:
            log.info("  %3d  %-14s  %s", p.score, p.location_fit, p.title[:70])
        return 0

    # Push the strong matches (precision over recall on the ping).
    pushed_ok = 0
    for p in pushes:
        if push_telegram(format_message(p)):
            pushed_ok += 1
            seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(), "pushed": True}
            time.sleep(0.7)  # gentle rate-limit between messages
        else:
            log.warning("  push failed for %s — leaving unseen to retry next run", p.title[:60])

    # Mark maybes as seen too (so we don't re-evaluate them), but log them as
    # "maybe" to matches.json so nothing good is silently lost.
    for p in maybes:
        seen[p.id] = {"title": p.title, "score": p.score, "ts": _now(), "pushed": False}

    # Append everything scored to the audit backlog.
    records = [
        {**asdict(p), "id": p.id, "kind": "push" if p.score >= MIN_FIT_SCORE
         else ("maybe" if p.score >= MAYBE_MIN_SCORE else "low"), "ts": _now()}
        for p in scored
    ]
    append_matches(records)
    save_seen(seen)

    log.info("Done. Pushed %d/%d strong matches; %d maybes logged; seen=%d; LLM calls=%d/%d",
             pushed_ok, len(pushes), len(maybes), len(seen), budget.used, budget.cap)
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
    )
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
    args = parser.parse_args(argv)

    if args.test:
        return run_test()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
