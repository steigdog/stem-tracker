"""Pathways to Science (pathwaystoscience.org) program directory.

The search results are split into A-Z pages (one per first letter of the host
institution). Each result links to a "programhub" detail page with the full
description, eligibility, dates and an external "Learn More and Apply" link.

To keep runs short and polite, only `max_new_per_run` unseen detail pages are
read per run; known pages are re-read every `refresh_days`.
"""

from __future__ import annotations

import re
import string
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..common import RawPosting, html_to_text
from ..http import PoliteSession

SITE = "https://www.pathwaystoscience.org/"

# The "sort" key prefix on programhub links tells us roughly what kind of
# program it is.
TYPE_BY_PREFIX = {"SUM": "REU", "FEL": "Fellowship", "OPP": "REU", "INT": "Industry Internship"}


def _listing_pages(base_url: str) -> list[str]:
    return [f"{base_url}&alpha={letter}&pos={i}" for i, letter in enumerate(string.ascii_uppercase)]


def _parse_listing(html: str) -> dict[str, dict]:
    """Return {programhub_url: {"title", "organization", "location"}} for one A-Z page."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    # The page lists "Institution", "(City, ST)", then one or more programs
    # for that institution. Walk the text in order and remember the current
    # institution so every program under it gets the right one.
    org = loc = None
    prev = None
    for node in soup.find_all(string=True):
        s = node.strip()
        if not s:
            continue
        a = node.find_parent("a", href=True)
        if a and a["href"].lower().startswith("programhub.aspx"):
            if "read more" not in s.lower():
                out.setdefault(urljoin(SITE, a["href"]), {"title": s, "organization": org, "location": loc})
        elif re.fullmatch(r"\([^()]{2,80}\)", s) and prev:
            org, loc = prev, s.strip("()")
        prev = s
    return out


def _detail_text(html: str) -> tuple[str, str | None]:
    """Main text of a programhub page, plus its external apply link."""
    soup = BeautifulSoup(html, "html.parser")
    apply = None
    for a in soup.find_all("a", href=True):
        if "learn more" in a.get_text(" ", strip=True).lower() and a["href"].startswith("http"):
            apply = a["href"]
            break
    text = html_to_text(html)
    # Drop the site navigation that precedes the program content.
    marker = text.find("Click Here to Learn More")
    if marker > 0:
        text = text[: text.rfind("\n", 0, marker)].splitlines()[-1] + "\n" + text[marker:]
    # Drop the generic footer.
    for footer in ("Pathways to Science is a project", "Copyright"):
        cut = text.find(footer)
        if cut > 0:
            text = text[:cut]
    return text.strip(), apply


def fetch(source: dict, http: PoliteSession, should_read) -> Iterable[RawPosting]:
    programs: dict[str, dict] = {}
    for page_url in _listing_pages(source["url"]):
        r = http.get(page_url)
        r.raise_for_status()
        programs.update(_parse_listing(r.text))

    budget = int(source.get("max_new_per_run", 40))
    for hub_url, info in programs.items():
        sort_key = hub_url.split("sort=")[-1]
        prefix = sort_key.split("-")[0].upper()
        raw = RawPosting(
            source=source["name"], source_id=sort_key, organization=info["organization"],
            title=info["title"], url=hub_url, source_url=source["url"], location=info["location"],
            org_size=source.get("org_size", "Smaller lab / organization"), sector=source.get("sector"),
            default_type=TYPE_BY_PREFIX.get(prefix, source.get("opportunity_type")),
            # Pathways returns HTTP 200 with a near-empty page for removed
            # programs, so the verifier looks for the program title instead.
            check={"kind": "page_contains", "url": hub_url, "must_contain": info["title"][:40]},
        )
        if budget > 0 and should_read(raw.key):
            d = http.get(hub_url)
            if d.status_code == 200:
                text, apply = _detail_text(d.text)
                raw.text = (f"Program: {info['title']}\nInstitution: {info['organization']}\n"
                            f"Location: {info['location']}\nApply link: {apply or hub_url}\n\n{text}")
                if apply:
                    raw.url = apply
                budget -= 1
        yield raw
