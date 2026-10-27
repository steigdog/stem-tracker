"""A single web page that describes one program (or a few sessions of it),
such as DOE SULI, NASA internships, or a university summer research program.

The page text goes to the LLM, which may return several records (e.g.
separate Summer / Fall / Spring terms). Web-discovered sources use this type.
"""

from __future__ import annotations

from typing import Iterable

from ..common import RawPosting, html_to_text
from ..http import PoliteSession


def fetch(source: dict, http: PoliteSession, should_read) -> Iterable[RawPosting]:
    url = source["url"]
    raw = RawPosting(
        source=source["name"], source_id=url, organization=source.get("organization"),
        title=source.get("title") or source["name"], url=url, source_url=url,
        org_size=source.get("org_size", "Smaller lab / organization"), sector=source.get("sector"),
        default_type=source.get("opportunity_type"), multi=True,
        check={"kind": "url", "url": url},
    )
    if should_read(raw.key):
        r = http.get(url)
        r.raise_for_status()
        raw.text = f"Source page: {url}\nOrganization: {source.get('organization') or 'unknown'}\n\n" + html_to_text(r.text)
    yield raw
