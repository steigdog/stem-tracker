"""USAJobs (federal jobs) via the official search API, student hiring path.

Needs two GitHub Actions secrets (free; see README):
  USAJOBS_API_KEY   from https://developer.usajobs.gov/apirequest/
  USAJOBS_EMAIL     the email address you registered with
Without them this source is skipped with a note in the run log.
"""

from __future__ import annotations

import os
from typing import Iterable

from ..common import RawPosting, html_to_text
from ..http import PoliteSession

API = "https://data.usajobs.gov/api/search"


class MissingKey(Exception):
    pass


def fetch(source: dict, http: PoliteSession, should_read) -> Iterable[RawPosting]:
    key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key or not email:
        raise MissingKey("USAJOBS_API_KEY / USAJOBS_EMAIL secrets not set; skipping USAJobs")
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    page = 1
    while page <= 10:
        params = {"Keyword": source.get("keyword", "intern"), "HiringPath": "student",
                  "ResultsPerPage": 250, "Page": page}
        r = http.get(API, params=params, headers=headers)
        r.raise_for_status()
        result = r.json().get("SearchResult", {})
        items = result.get("SearchResultItems", [])
        for item in items:
            d = item.get("MatchedObjectDescriptor", {})
            details = (d.get("UserArea") or {}).get("Details") or {}
            pay = d.get("PositionRemuneration") or [{}]
            raw = RawPosting(
                source=source["name"], source_id=str(d.get("PositionID") or item.get("MatchedObjectId")),
                organization=d.get("OrganizationName") or d.get("DepartmentName"),
                title=d.get("PositionTitle"), url=(d.get("ApplyURI") or [d.get("PositionURI")])[0],
                source_url=API, location=d.get("PositionLocationDisplay"),
                org_size="Smaller lab / organization", sector=source.get("sector"),
                default_type=source.get("opportunity_type"),
                check={"kind": "url", "url": d.get("PositionURI")},
            )
            if should_read(raw.key):
                raw.text = "\n".join(filter(None, [
                    f"Title: {d.get('PositionTitle')}",
                    f"Agency: {d.get('OrganizationName')} ({d.get('DepartmentName')})",
                    f"Location: {d.get('PositionLocationDisplay')}",
                    f"Pay: {pay[0].get('MinimumRange')} - {pay[0].get('MaximumRange')} {pay[0].get('RateIntervalCode') or ''}",
                    f"Open: {d.get('PublicationStartDate')}  Closes: {d.get('ApplicationCloseDate')}",
                    f"Who may apply: {(details.get('WhoMayApply') or {}).get('Name')}",
                    f"Summary: {details.get('JobSummary')}",
                    f"Qualifications: {d.get('QualificationSummary')}",
                    f"Education: {details.get('Education')}",
                    f"Requirements: {html_to_text(details.get('Requirements') or '')}",
                ]))
            yield raw
        if page >= int(result.get("UserArea", {}).get("NumberOfPages", 1) or 1):
            break
        page += 1
