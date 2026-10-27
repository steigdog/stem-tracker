"""Command-line entry point for the collection agent.

    python -m agent.run collect              # pull new listings from all enabled sources
    python -m agent.run collect --discover   # ...and also search the web for new programs
    python -m agent.run collect --only Pfizer
    python -m agent.run verify               # nightly: check every application link
    python -m agent.run requests             # process "Request a company" submissions

Each command updates data/listings_all.json, docs/data/listings.json and
appends an entry to data/run_log.json.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import ats
from .common import (RUN_LOG, SOURCE_HEALTH, days_since, load_companies, load_json, load_majors,
                     load_research_sources, load_settings, now_iso, save_json, sha)
from .extract import ExtractionCache, ExtractionError, ground, make_extractor
from .http import Blocked, PoliteSession
from .sources import ADAPTERS as SOURCE_ADAPTERS
from .sources.usajobs import MissingKey
from .store import Store


def make_http(settings) -> PoliteSession:
    h = settings["http"]
    return PoliteSession(h["user_agent"], h["min_delay_seconds"], h["timeout_seconds"], h["retries"])


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------
class RunLog:
    def __init__(self, kind: str):
        self.entry = {"kind": kind, "started": now_iso(), "finished": None, "sources": {},
                      "added": [], "updated": [], "removed": [], "failed": [], "warnings": [], "llm": {}}

    def source(self, name: str) -> dict:
        return self.entry["sources"].setdefault(
            name, {"found": 0, "read": 0, "added": 0, "updated": 0, "skipped": 0, "status": "ok", "note": ""})

    def fail(self, where: str, why: str) -> None:
        self.entry["failed"].append({"where": where, "error": why[:400]})

    def warn(self, msg: str) -> None:
        self.entry["warnings"].append(msg)

    def save(self) -> None:
        self.entry["finished"] = now_iso()
        log = load_json(RUN_LOG, [])
        log.append(self.entry)
        save_json(RUN_LOG, log[-120:])   # keep the most recent 120 runs
        self.print_summary()

    def print_summary(self) -> None:
        e = self.entry
        lines = [f"## {e['kind']} run {e['started']}",
                 f"added {len(e['added'])} | updated {len(e['updated'])} | removed {len(e['removed'])} | "
                 f"failed {len(e['failed'])} | warnings {len(e['warnings'])}", ""]
        if e["sources"]:
            lines.append("| source | found | read | added | updated | skipped | status | note |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for name, s in e["sources"].items():
                lines.append(f"| {name} | {s['found']} | {s['read']} | {s['added']} | {s['updated']} | "
                             f"{s['skipped']} | {s['status']} | {s['note']} |")
        for w in e["warnings"]:
            lines.append(f"- WARNING: {w}")
        for f in e["failed"][:30]:
            lines.append(f"- FAILED {f['where']}: {f['error']}")
        if e["llm"]:
            lines.append(f"- LLM usage: {e['llm']}")
        text = "\n".join(lines)
        print(text)
        # Shows the summary on the GitHub Actions run page.
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(text + "\n")


def check_health(log: RunLog, counts: dict[str, int], history_runs: int) -> None:
    """Flag sources that returned nothing when they usually return something."""
    health = load_json(SOURCE_HEALTH, {})
    for name, n in counts.items():
        past = health.get(name, [])[-history_runs:]
        if n == 0 and past and statistics.median(past) > 0:
            log.warn(f"'{name}' returned 0 results (usually ~{statistics.median(past):.0f}) - possibly broken")
            log.source(name)["status"] = "possibly broken"
        health[name] = (health.get(name, []) + [n])[-30:]
    save_json(SOURCE_HEALTH, health)


# Locations that clearly name a country other than the US.
_NON_US = re.compile(
    r"\b(uk|united kingdom|gbr|england|scotland|wales|ireland|irl|canada|can|mexico|mex|india|ind|china|chn|"
    r"japan|jpn|korea|singapore|sgp|australia|aus|germany|deu|france|fra|netherlands|nld|belgium|bel|"
    r"switzerland|che|spain|esp|italy|ita|poland|pol|sweden|denmark|austria|israel|brazil|bra|"
    r"costa rica|philippines|malaysia|taiwan|argentina|colombia|chile|hungary|czech)\b", re.I)


def is_us(rec: dict) -> bool:
    """False only when every stated location clearly names a non-US country.
    Remote roles, unstated locations and vague ones ("Any site") are kept."""
    locs = rec.get("locations") or []
    if rec.get("states") or rec.get("remote") == "Remote" or not locs:
        return True
    return not all(_NON_US.search(l) for l in locs)


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def collect(only: str | None = None, discover_new: bool = False) -> None:
    settings, majors = load_settings(), load_majors()
    http = make_http(settings)
    store = Store()
    log = RunLog("collect")
    col = settings["collection"]
    filt = ([re.compile(p, re.I) for p in col["include_title_patterns"]],
            [re.compile(p, re.I) for p in col["exclude_title_patterns"]])

    if discover_new and settings.get("discovery", {}).get("enabled"):
        from .discover import discover
        try:
            discover(settings, http, log)
        except Exception as e:  # discovery is optional; never let it stop collection
            log.fail("discovery", repr(e))

    companies = [c for c in load_companies() if c.get("enabled")]
    research = [s for s in load_research_sources() if s.get("enabled")]
    if only:
        companies = [c for c in companies if c["name"].lower() == only.lower()]
        research = [s for s in research if s["name"].lower() == only.lower()]

    def make_should_read(refresh_days: float):
        return lambda key: days_since(store.last_read(key)) >= refresh_days

    # ----- 1. Fetch raw postings from every source (in parallel) -----------
    def run_company(c):
        found = ats.resolve_ats(c, http)
        if not found:
            raise RuntimeError("could not detect the ATS for this company; add ats/ats_config by hand")
        kind, cfg = found
        if kind not in ats.ADAPTERS:
            raise RuntimeError(f"no adapter for ATS '{kind}'")
        c = {**c, "ats": kind, "ats_config": cfg}
        should = make_should_read(col.get("ats_refresh_days", 7))
        return list(ats.ADAPTERS[kind](c, http, should, filt, settings))

    def run_research(s):
        adapter = SOURCE_ADAPTERS.get(s["type"])
        if not adapter:
            raise RuntimeError(f"unknown source type '{s['type']}'")
        return list(adapter(s, http, make_should_read(s.get("refresh_days", 7))))

    jobs = [(c["name"], run_company, c) for c in companies] + [(s["name"], run_research, s) for s in research]
    raw_by_source: dict[str, list] = {}
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=col.get("parallel_sources", 4)) as pool:
        futures = {pool.submit(fn, cfg): name for name, fn, cfg in jobs}
        for fut in as_completed(futures):
            name = futures[fut]
            src = log.source(name)
            try:
                raws = fut.result()
                raw_by_source[name] = raws
                counts[name] = len(raws)
                src["found"] = len(raws)
            except MissingKey as e:
                src.update(status="skipped", note=str(e))
            except Blocked as e:
                src.update(status="blocked", note=str(e)[:120])
                log.fail(name, str(e))
            except Exception as e:
                src.update(status="failed", note=type(e).__name__)
                log.fail(name, f"{e!r}\n{traceback.format_exc(limit=2)}")
                counts[name] = 0

    # ----- 2. Extract structured records (new or changed postings only) ----
    extractor = make_extractor(settings, majors)
    cache = ExtractionCache()
    budget = settings["llm"]["max_extractions_per_run"]
    deferred = 0
    model_tag = getattr(extractor, "model", extractor.name)

    for name, raws in raw_by_source.items():
        src = log.source(name)
        for raw in raws:
            if raw.text is None:
                store.touch_seen(raw.key)
                continue
            text_hash = sha(raw.text)
            known = store.postings_for(raw.key)
            if known and all(p.get("text_hash") == text_hash for p in known):
                for p in known:
                    p["last_read"] = p["last_seen"] = now_iso()
                continue
            cache_key = f"{extractor.name}:{model_tag}:{text_hash}"
            hit = cache.get(cache_key)
            if hit:
                records = hit["result"]
            else:
                if extractor.name == "claude" and budget <= 0:
                    deferred += 1
                    continue
                try:
                    records = extractor.extract(raw)
                    budget -= 1
                    cache.put(cache_key, records)
                except (ExtractionError, ValueError, KeyError) as e:
                    log.fail(f"{name}: {raw.title}", f"extraction failed: {e}")
                    continue
                except Exception as e:   # API outage etc: stop extracting, keep what we have
                    log.fail("extraction", f"{e!r} - stopping extraction for this run")
                    budget = 0
                    continue
            src["read"] += 1
            kept = 0
            for rec in records:
                rec = ground(dict(rec), raw)
                rec["organization"] = rec.get("organization") or raw.organization
                rec["title"] = rec.get("title") or raw.title
                rec["opportunity_type"] = rec.get("opportunity_type") or raw.default_type
                if not rec.get("is_student_opportunity", True):
                    continue
                if col.get("require_relevant_major") and not rec.get("majors"):
                    continue
                if col.get("us_only") and not is_us(rec):
                    continue
                result = store.upsert(rec, raw, text_hash)
                kept += 1
                if result == "added":
                    src["added"] += 1
                    log.entry["added"].append(f"{rec['organization']} - {rec['title']}")
                elif result == "updated":
                    src["updated"] += 1
                    log.entry["updated"].append(f"{rec['organization']} - {rec['title']}")
            if kept == 0:
                src["skipped"] += 1
                store.skipped[raw.key] = {"at": now_iso(), "why": "not a STEM student opportunity"}
    if deferred:
        log.warn(f"{deferred} postings waiting for the next run (max_extractions_per_run reached)")
    log.entry["llm"] = {"extractor": extractor.name, "model": model_tag, **extractor.usage}

    # ----- 3. Deadlines, health, save --------------------------------------
    for lid in store.expire_deadlines():
        l = store.listings[lid]
        log.entry["removed"].append(f"{l['organization']} - {l['title']} ({l['removed_reason']})")
    check_health(log, counts, settings["health"]["history_runs"])
    cache.save()
    store.meta["last_collect"] = now_iso()
    store.save(sources_monitored=None if only else len(companies) + len(research))
    log.save()


# ---------------------------------------------------------------------------
# verify (nightly link check)
# ---------------------------------------------------------------------------
def check_posting(p: dict, http: PoliteSession, dead_phrases: list[str]) -> str:
    """Return "live", "gone" or "error" for one posting."""
    chk = p.get("check") or {"kind": "url", "url": p["url"]}
    url = chk.get("api") or chk.get("url") or p["url"]
    try:
        r = http.get(url, headers={"Accept": "application/json"} if chk["kind"] in ("workday", "api404") else None)
    except Exception:
        return "error"
    if r.status_code in (404, 410):
        return "gone"
    if r.status_code != 200:
        return "error"
    if chk["kind"] == "workday":
        try:
            return "live" if r.json().get("jobPostingInfo") else "gone"
        except ValueError:
            return "error"
    if chk["kind"] == "api404":
        return "live"
    low = r.text.lower()
    if chk["kind"] == "page_contains":
        return "live" if chk["must_contain"].lower() in low else "gone"
    if any(ph in low for ph in dead_phrases) and len(low) < 200_000:
        return "gone"
    return "live"


def verify() -> None:
    settings = load_settings()
    http = make_http(settings)
    store = Store()
    log = RunLog("verify")
    vcfg = settings["verification"]
    phrases = [p.lower() for p in vcfg["dead_page_phrases"]]

    todo = [(lid, p) for lid, l in store.listings.items() if l["status"] == "active"
            for p in l["postings"] if p["status"] != "gone"]
    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(check_posting, p, http, phrases): i for i, (_, p) in enumerate(todo)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()

    tally = {"live": 0, "gone": 0, "error": 0}
    for i, (lid, p) in enumerate(todo):
        status = results[i]
        tally[status] += 1
        src = log.source(p["source"])
        src["found"] += 1
        if status == "live":
            p.update(gone_count=0, status="live", last_verified=now_iso())
        elif status == "gone":
            p["gone_count"] = p.get("gone_count", 0) + 1
            if p["gone_count"] >= vcfg["gone_checks_before_removal"]:
                p["status"] = "gone"
        else:
            src["skipped"] += 1   # could not check (network error); try again tomorrow

    for lid, l in store.listings.items():
        if l["status"] != "active":
            continue
        live = [p for p in l["postings"] if p["status"] != "gone"]
        if live:
            l["last_verified"] = max(p.get("last_verified") or "" for p in live) or l.get("last_verified")
        else:
            store.remove(lid, "posting offline")
            log.entry["removed"].append(f"{l['organization']} - {l['title']} (posting offline)")
    for lid in store.expire_deadlines():
        l = store.listings[lid]
        log.entry["removed"].append(f"{l['organization']} - {l['title']} ({l['removed_reason']})")

    if tally["error"] > max(5, 0.3 * len(todo)):
        log.warn(f"{tally['error']} of {len(todo)} link checks failed with network errors")
    log.entry["llm"] = {}
    log.entry["checks"] = tally
    store.meta["last_verify"] = now_iso()
    store.save()
    log.save()


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="STEM opportunity collection agent")
    ap.add_argument("command", choices=["collect", "verify", "requests"])
    ap.add_argument("--only", help="run a single source by name (collect only)")
    ap.add_argument("--discover", action="store_true", help="also search the web for new program sources")
    args = ap.parse_args(argv)
    if args.command == "collect":
        collect(only=args.only, discover_new=args.discover)
    elif args.command == "verify":
        verify()
    else:
        from .requests_queue import process_requests
        process_requests()
    return 0


if __name__ == "__main__":
    sys.exit(main())
