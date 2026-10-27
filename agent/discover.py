"""Weekly web search for new STEM internship / research programs.

Claude runs the searches in settings.yaml (via its built-in web search tool)
and returns candidate program pages. Each candidate is checked (reachable,
allowed by robots.txt, not already known) and then appended to
data/discovered_sources.json as a `program_page` source, so it is collected
automatically from the next run on.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlparse

from .common import DISCOVERED_SOURCES, load_json, load_research_sources, now_iso, save_json

PROMPT = """Find official web pages for US-based summer research programs, internships, or co-ops for undergraduate students in non-software STEM fields (biology, chemistry, biochemistry, engineering, materials science, physics, environmental science, public health).

Run these searches:
{queries}

Only include pages that are the program's own official page (a university, lab, agency, company, or society site), describe one specific program, and are open to undergraduates. Skip aggregator lists, news articles, and job boards.

Known pages to skip:
{known}

Reply with only a JSON array, at most {limit} items, each:
{{"name": "...", "organization": "...", "url": "https://...", "opportunity_type": "REU|National Lab|Government Program|Industry Internship|Fellowship|Clinical/Hospital Research", "sector": "Academic Research|Government / National Lab|Biotech/Pharma|Aerospace & Defense|Other Industry"}}"""


def _norm_url(u: str) -> str:
    p = urlparse(u)
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def discover(settings: dict, http, log) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warn("discovery skipped: ANTHROPIC_API_KEY not set")
        return
    import anthropic

    dcfg = settings["discovery"]
    known_sources = load_research_sources()
    known = {_norm_url(s["url"]) for s in known_sources if s.get("url")}
    client = anthropic.Anthropic()
    prompt = PROMPT.format(
        queries="\n".join(f"- {q}" for q in dcfg["queries"]),
        known="\n".join(sorted(known))[:4000] or "(none)",
        limit=dcfg["max_new_sources_per_run"],
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": dcfg["max_searches_per_run"]}]
    model = settings["llm"]["model"]
    if model.startswith("claude-haiku"):
        tools[0]["type"] = "web_search_20250305"   # Haiku uses the basic web search tool

    text = ""
    for _ in range(4):   # web search may pause a long turn; resume it up to 3 times
        resp = client.messages.create(model=model, max_tokens=8000, messages=messages, tools=tools,
                                      output_config={"effort": "medium"})
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "refusal":
            log.warn("discovery: model declined the request")
            return
        text = "".join(b.text for b in resp.content if b.type == "text")
        break

    m = re.search(r"\[.*\]", text, re.S)
    try:
        candidates = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        log.fail("discovery", "could not parse the candidate list")
        return

    discovered = load_json(DISCOVERED_SOURCES, [])
    added = 0
    for c in candidates:
        url = (c.get("url") or "").strip()
        if not url.startswith("http") or _norm_url(url) in known:
            continue
        try:
            if not http.allowed(url) or http.get(url).status_code != 200:
                continue
        except Exception:
            continue
        discovered.append({
            "name": (c.get("name") or url)[:120], "enabled": True, "type": "program_page", "url": url,
            "organization": c.get("organization"), "opportunity_type": c.get("opportunity_type"),
            "sector": c.get("sector") or "Academic Research", "refresh_days": 7,
            "origin": "discovered", "discovered_at": now_iso(),
        })
        known.add(_norm_url(url))
        added += 1
        if added >= dcfg["max_new_sources_per_run"]:
            break
    save_json(DISCOVERED_SOURCES, discovered)
    log.source("web discovery").update(found=len(candidates), added=added,
                                       note=f"{added} new sources added to discovered_sources.json")
