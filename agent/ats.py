"""Adapters for applicant tracking systems (ATS), plus ATS auto-detection.

Large employers publish jobs through a handful of ATS vendors that expose
structured JSON. Reading that JSON is far more reliable than scraping page
layouts, and one adapter covers every company on the same vendor.

Each `fetch_*` function takes a company config dict and returns RawPostings.
For postings we already know (and that don't need refreshing) the adapter
skips the detail request and returns a RawPosting with text=None, which just
means "still listed".
"""

from __future__ import annotations

import html
import re
import threading
from typing import Callable, Iterable

from .common import RawPosting, html_to_text, now_iso, load_json, save_json, ATS_CACHE, days_since
from .http import PoliteSession

# A callable the pipeline passes in: should_read(posting_key) -> bool.
# False means we already have an up-to-date copy of this posting.
ShouldRead = Callable[[str], bool]


def title_matches(title: str, include: list[re.Pattern], exclude: list[re.Pattern]) -> bool:
    """Internship / co-op / early-career filter, applied to job titles."""
    t = title or ""
    return any(p.search(t) for p in include) and not any(p.search(t) for p in exclude)


def _base(company: dict, raw_kw: dict) -> dict:
    return dict(organization=company["name"], org_size=company.get("org_size", "Large employer"),
                sector=company.get("sector"), default_type="Industry Internship", **raw_kw)


# ---------------------------------------------------------------------------
# Workday  (most large pharma / aerospace employers)
# Public endpoints used by every Workday career site:
#   POST https://HOST/wday/cxs/TENANT/SITE/jobs         -> search results
#   GET  https://HOST/wday/cxs/TENANT/SITE/job/...      -> one posting
# ---------------------------------------------------------------------------
def fetch_workday(company, http: PoliteSession, should_read: ShouldRead, filt, settings) -> Iterable[RawPosting]:
    cfg = company["ats_config"]
    base = f"https://{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['site']}"
    prefix = (company.get("search_prefix") or "").strip()
    terms = settings["collection"]["workday_search_terms"]
    max_pages = settings["collection"]["workday_max_pages"]

    seen_paths: dict[str, dict] = {}
    for term in terms:
        search = f"{prefix} {term}".strip()
        for page in range(max_pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": search}
            r = http.post(base + "/jobs", json=body, headers={"Accept": "application/json"})
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            for p in postings:
                if p.get("externalPath") and title_matches(p.get("title", ""), *filt):
                    seen_paths.setdefault(p["externalPath"], p)
            if len(postings) < 20:
                break

    for path, summary in seen_paths.items():
        check = {"kind": "workday", "api": base + path}
        public_url = f"https://{cfg['host']}/en-US/{cfg['site']}{path}"
        raw = RawPosting(source=company["name"], source_id=path, title=summary.get("title"),
                         url=public_url, source_url=base + "/jobs",
                         location=summary.get("locationsText"), check=check, **_base(company, {}))
        if not should_read(raw.key):
            yield raw
            continue
        d = http.get(base + path, headers={"Accept": "application/json"})
        if d.status_code != 200:
            continue
        info = d.json().get("jobPostingInfo", {})
        # Some employers put department names in additionalLocations; keep
        # only entries that look like places ("City, State" / "City - Country").
        extra = [l for l in (info.get("additionalLocations") or []) if "," in l or " - " in l]
        locs = [info.get("location")] + extra
        raw.location = "; ".join(l for l in locs if l) or raw.location
        raw.url = info.get("externalUrl") or public_url
        raw.text = "\n".join(filter(None, [
            f"Title: {info.get('title')}",
            f"Company: {company['name']}",
            f"Location(s): {raw.location}",
            f"Time type: {info.get('timeType')}",
            f"Posted: {info.get('postedOn')}  Start date: {info.get('startDate')}",
            f"Requisition: {info.get('jobReqId')}",
            html_to_text(info.get("jobDescription", "")),
        ]))
        yield raw


# ---------------------------------------------------------------------------
# Greenhouse  (SpaceX and many startups)
#   GET https://boards-api.greenhouse.io/v1/boards/BOARD/jobs
#   GET https://boards-api.greenhouse.io/v1/boards/BOARD/jobs/ID
# ---------------------------------------------------------------------------
def fetch_greenhouse(company, http: PoliteSession, should_read: ShouldRead, filt, settings) -> Iterable[RawPosting]:
    board = company["ats_config"]["board"]
    api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    r = http.get(api)
    r.raise_for_status()
    for job in r.json().get("jobs", []):
        if not title_matches(job.get("title", ""), *filt):
            continue
        job_api = f"{api}/{job['id']}"
        raw = RawPosting(source=company["name"], source_id=str(job["id"]), title=job.get("title"),
                         url=job.get("absolute_url"), source_url=api,
                         location=(job.get("location") or {}).get("name"),
                         check={"kind": "api404", "api": job_api}, **_base(company, {}))
        if should_read(raw.key):
            d = http.get(job_api)
            if d.status_code != 200:
                continue
            content = html.unescape(d.json().get("content", ""))
            raw.text = f"Title: {job.get('title')}\nCompany: {company['name']}\nLocation: {raw.location}\n" \
                       f"Updated: {job.get('updated_at')}\n\n{html_to_text(content)}"
        yield raw


# ---------------------------------------------------------------------------
# Lever  (startups, some biotech)
#   GET https://api.lever.co/v0/postings/COMPANY?mode=json   (full text included)
# ---------------------------------------------------------------------------
def fetch_lever(company, http: PoliteSession, should_read: ShouldRead, filt, settings) -> Iterable[RawPosting]:
    slug = company["ats_config"]["company"]
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = http.get(api)
    r.raise_for_status()
    for job in r.json():
        if not title_matches(job.get("text", ""), *filt):
            continue
        cats = job.get("categories") or {}
        raw = RawPosting(source=company["name"], source_id=job["id"], title=job.get("text"),
                         url=job.get("hostedUrl"), source_url=api, location=cats.get("location"),
                         check={"kind": "url", "url": job.get("hostedUrl")}, **_base(company, {}))
        if should_read(raw.key):
            lists = "\n".join(f"{l.get('text')}:\n{html_to_text(l.get('content', ''))}" for l in job.get("lists", []))
            raw.text = f"Title: {job.get('text')}\nCompany: {company['name']}\nLocation: {raw.location}\n" \
                       f"Commitment: {cats.get('commitment')}\n\n{job.get('descriptionPlain', '')}\n{lists}\n" \
                       f"{job.get('additionalPlain', '')}"
        yield raw


# ---------------------------------------------------------------------------
# iCIMS  (EXPERIMENTAL - not yet tested against a live portal)
# iCIMS has no public JSON API, so this reads the iframe search page.
# ---------------------------------------------------------------------------
def fetch_icims(company, http: PoliteSession, should_read: ShouldRead, filt, settings) -> Iterable[RawPosting]:
    portal = company["ats_config"]["portal"]
    base = f"https://{portal}.icims.com"
    search = f"{base}/jobs/search?ss=1&searchKeyword=intern&in_iframe=1"
    r = http.get(search)
    r.raise_for_status()
    link_re = r'href="(https://[^"]+/jobs/(\d+)/[^"]+/job)[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)'
    found = {job_id: (url, title.strip()) for url, job_id, title in re.findall(link_re, r.text)}
    for job_id, (url, title) in found.items():
        if not title_matches(title, *filt):
            continue
        raw = RawPosting(source=company["name"], source_id=job_id, title=title, url=url, source_url=search,
                         check={"kind": "url", "url": url}, **_base(company, {}))
        if should_read(raw.key):
            d = http.get(url + "?in_iframe=1")
            if d.status_code == 200:
                raw.text = f"Company: {company['name']}\n" + html_to_text(d.text)
        yield raw


ADAPTERS = {"workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever, "icims": fetch_icims}


# ---------------------------------------------------------------------------
# ATS auto-detection
# ---------------------------------------------------------------------------
_PATTERNS = [
    ("workday", re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/)?(?!embed)([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)")),
    ("icims", re.compile(r"https?://([\w-]+)\.icims\.com")),
]


def _config_from_match(ats: str, m: re.Match) -> dict:
    if ats == "workday":
        tenant, wd, site = m.group(1), m.group(2), m.group(3)
        return {"host": f"{tenant}.{wd}.myworkdayjobs.com", "tenant": tenant, "site": site}
    if ats == "greenhouse":
        return {"board": m.group(1)}
    if ats == "lever":
        return {"company": m.group(1)}
    return {"portal": m.group(1)}


def _probe(ats: str, cfg: dict, http: PoliteSession) -> bool:
    """Confirm a detected ATS config really serves jobs."""
    try:
        if ats == "workday":
            url = f"https://{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs"
            r = http.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
            return r.status_code == 200 and "jobPostings" in r.json()
        if ats == "greenhouse":
            return http.get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['board']}/jobs").status_code == 200
        if ats == "lever":
            r = http.get(f"https://api.lever.co/v0/postings/{cfg['company']}?mode=json&limit=1")
            return r.status_code == 200 and isinstance(r.json(), list)
        if ats == "icims":
            return http.get(f"https://{cfg['portal']}.icims.com/jobs/search?ss=1&in_iframe=1").status_code == 200
    except Exception:
        return False
    return False


def detect_ats(name: str, careers_url: str | None, http: PoliteSession) -> tuple[str, dict] | None:
    """Work out which ATS a company uses.

    1. Fetch the careers page and look for links to a known ATS domain
       (also one level deeper, following "search jobs"-style links).
    2. Otherwise guess Greenhouse / Lever board names from the company name.
    Every candidate is confirmed with a live API call before it is accepted.
    """
    pages = []
    if careers_url:
        try:
            r = http.get(careers_url)
            pages.append(r.text)
            # Follow up to 3 links that look like a job-search entry point.
            for href in re.findall(r'href="(https?://[^"]+)"[^>]*>[^<]*(?:search|view|open|find)[^<]*(?:jobs|roles|positions|openings)',
                                   r.text, re.I)[:3]:
                try:
                    pages.append(http.get(href).text)
                except Exception:
                    pass
        except Exception:
            pass
    for text in pages:
        for ats, pat in _PATTERNS:
            for m in pat.finditer(text):
                cfg = _config_from_match(ats, m)
                if _probe(ats, cfg, http):
                    return ats, cfg
    # Guess board names from the company name: "Blue Origin" -> blueorigin, blue-origin
    words = re.sub(r"[^a-z0-9 ]", "", name.lower()).split()
    for guess in {"".join(words), "-".join(words)}:
        for ats, cfg in (("greenhouse", {"board": guess}), ("lever", {"company": guess})):
            if _probe(ats, cfg, http):
                return ats, cfg
    return None


def resolve_ats(company: dict, http: PoliteSession) -> tuple[str, dict] | None:
    """Return (ats, ats_config) for a company, using the cache for 'auto' entries.
    Failed detections are retried after 7 days."""
    if company.get("ats", "auto") != "auto":
        return company["ats"], company.get("ats_config", {})
    with _CACHE_LOCK:
        hit = load_json(ATS_CACHE, {}).get(company["name"])
    if hit and hit.get("ats"):
        return hit["ats"], hit["ats_config"]
    if hit and days_since(hit.get("checked_at")) < 7:
        return None
    found = detect_ats(company["name"], company.get("careers_url"), http)
    with _CACHE_LOCK:   # several sources may finish detection at the same time
        cache = load_json(ATS_CACHE, {})
        cache[company["name"]] = ({"ats": found[0], "ats_config": found[1], "checked_at": now_iso()} if found
                                  else {"ats": None, "checked_at": now_iso()})
        save_json(ATS_CACHE, cache)
    return found


_CACHE_LOCK = threading.Lock()
