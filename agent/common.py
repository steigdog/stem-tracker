"""Shared helpers: file paths, config loading, small text utilities, and the
RawPosting type that every source adapter produces."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Paths. Everything is relative to the repository root so the agent works the
# same on your laptop and in GitHub Actions.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"            # agent state (not needed by the website)
PUBLIC_DIR = ROOT / "docs" / "data"  # what the website reads

LISTINGS_STATE = DATA_DIR / "listings_all.json"     # every listing ever seen, incl. removed
PUBLIC_LISTINGS = PUBLIC_DIR / "listings.json"      # active listings only
RUN_LOG = DATA_DIR / "run_log.json"
EXTRACTION_CACHE = DATA_DIR / "extraction_cache.json"
ATS_CACHE = DATA_DIR / "ats_cache.json"
SOURCE_HEALTH = DATA_DIR / "source_health.json"
DISCOVERED_SOURCES = DATA_DIR / "discovered_sources.json"
DISCOVERED_COMPANIES = DATA_DIR / "discovered_companies.json"
COMPANY_REQUESTS = DATA_DIR / "company_requests.json"


# ---------------------------------------------------------------------------
# JSON / YAML helpers
# ---------------------------------------------------------------------------
def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path, default):
    """Read a JSON file, or return `default` if it doesn't exist yet."""
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data, indent: int | None = 2) -> None:
    """Write JSON atomically (write to a temp file, then rename) so a crash
    mid-write can never leave a half-written file in the repo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def load_settings() -> dict:
    return load_yaml(CONFIG_DIR / "settings.yaml")


def load_majors() -> dict[str, list[str]]:
    return load_yaml(CONFIG_DIR / "majors.yaml")["majors"]


def load_companies() -> list[dict]:
    """Companies from config/companies.yaml plus any the agent added itself
    (from "Request a company" submissions)."""
    companies = load_yaml(CONFIG_DIR / "companies.yaml").get("companies", [])
    names = {c["name"].lower() for c in companies}
    for c in load_json(DISCOVERED_COMPANIES, []):
        if c["name"].lower() not in names:
            companies.append(c)
    return companies


def load_research_sources() -> list[dict]:
    """Sources from config/research_sources.yaml plus web-discovered ones."""
    sources = load_yaml(CONFIG_DIR / "research_sources.yaml").get("sources", [])
    urls = {s.get("url") for s in sources}
    for s in load_json(DISCOVERED_SOURCES, []):
        if s.get("url") not in urls:
            sources.append(s)
    return sources


# ---------------------------------------------------------------------------
# Time and text helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def days_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def html_to_text(html: str) -> str:
    """Turn an HTML page or fragment into readable plain text."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def norm(s: str | None) -> str:
    """Lowercase, strip punctuation and filler words. Used for duplicate matching."""
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(inc|llc|corp|corporation|company|co|the|ltd|plc|usa|us)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slug(s: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:n]


# ---------------------------------------------------------------------------
# RawPosting: what every adapter returns, before LLM extraction.
# ---------------------------------------------------------------------------
@dataclass
class RawPosting:
    source: str                 # source name, e.g. "Pfizer" or "Pathways to Science"
    source_id: str              # stable id within that source (req id, URL, ...)
    organization: str | None    # best guess before extraction (may be refined by LLM)
    title: str | None
    url: str                    # direct link to the posting / program page
    source_url: str             # where we found it (feed or directory URL)
    text: str | None = None     # full posting text; None = "still listed, unchanged, no need to re-read"
    location: str | None = None
    org_size: str = "Large employer"      # or "Smaller lab / organization"
    sector: str | None = None
    default_type: str | None = None       # fallback opportunity type
    multi: bool = False         # page may describe several opportunities
    check: dict = field(default_factory=dict)   # how the nightly verifier checks this posting

    @property
    def key(self) -> str:
        return f"{self.source}::{self.source_id}"
