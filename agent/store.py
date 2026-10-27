"""The listing store: merging, de-duplication, removal and publishing.

Data model
----------
A *listing* is one opportunity as shown on the board (e.g. "Moderna - Intern,
Biometrics - Summer 2027"). It has one or more *postings*: the concrete places
it was found (a Workday requisition, a Pathways to Science page, ...). The
same role found through two sources becomes one listing with two postings.

A listing stays active while at least one of its postings is live and its
deadline hasn't passed. Removed listings stay in data/listings_all.json (so
history is never lost) but are left out of docs/data/listings.json.
"""

from __future__ import annotations

import re
from datetime import date

from .common import (LISTINGS_STATE, PUBLIC_LISTINGS, RawPosting, load_json, norm, now_iso, save_json, sha,
                     today_iso)

# Fields copied from extraction results onto the listing.
FIELDS = ["organization", "title", "locations", "states", "remote", "opportunity_type", "seasons", "majors",
          "class_years", "min_gpa", "citizenship", "export_control", "pay", "paid", "housing",
          "letters_required", "open_date", "deadline", "rolling", "apply_url", "summary"]


def _title_tokens(title: str) -> set[str]:
    stop = {"intern", "internship", "the", "and", "of", "for", "in", "a", "program", "summer", "2025", "2026",
            "2027", "2028"}
    return {t for t in norm(title).split() if t not in stop}


def _year(title: str) -> str:
    m = re.search(r"\b(20\d\d)\b", title or "")
    return m.group(1) if m else ""


def dedupe_key(rec: dict) -> str:
    """Same organization + same title (+ same year/season) = same opportunity."""
    seasons = ",".join(sorted(rec.get("seasons") or []))
    return f"{norm(rec.get('organization'))}|{norm(rec.get('title'))}|{_year(rec.get('title'))}|{seasons}"


class Store:
    def __init__(self):
        state = load_json(LISTINGS_STATE, {"listings": {}})
        self.listings: dict[str, dict] = state["listings"]
        self.meta: dict = state.get("meta", {})
        # Postings we read but didn't keep (not a student/STEM role), so we
        # don't fetch them again every run: {posting_key: {"at": iso, "why": str}}
        self.skipped: dict[str, dict] = state.get("skipped", {})
        self._reindex()

    def last_read(self, key: str) -> str | None:
        """When we last read this posting's full text (kept or skipped)."""
        times = [p.get("last_read") or p.get("first_seen") for p in self.postings_for(key)]
        if key in self.skipped:
            times.append(self.skipped[key]["at"])
        times = [t for t in times if t]
        return max(times) if times else None

    def _reindex(self):
        self.by_posting: dict[str, str] = {}
        self.by_sub: dict[str, str] = {}
        self.by_key: dict[str, str] = {}
        for lid, l in self.listings.items():
            self.by_key[dedupe_key(l)] = lid
            for p in l["postings"]:
                self.by_posting.setdefault(p["key"], lid)
                self.by_sub[p.get("sub_id", p["key"])] = lid

    # ----- lookups used by the collector -----------------------------------
    def posting(self, key: str) -> dict | None:
        lid = self.by_posting.get(key)
        if not lid:
            return None
        return next((p for p in self.listings[lid]["postings"] if p["key"] == key), None)

    def postings_for(self, key: str) -> list[dict]:
        """All postings with this key (a multi-opportunity page has one per listing)."""
        lids = {lid for sub, lid in self.by_sub.items() if sub == key or sub.startswith(key + "#")}
        return [p for lid in lids for p in self.listings[lid]["postings"] if p["key"] == key]

    def _find_duplicate(self, rec: dict) -> str | None:
        lid = self.by_key.get(dedupe_key(rec))
        if lid:
            return lid
        # Fuzzy match: same organization, nearly identical title, compatible seasons.
        org, toks = norm(rec.get("organization")), _title_tokens(rec.get("title") or "")
        if not org or len(toks) < 2:
            return None
        for lid, l in self.listings.items():
            lorg = norm(l.get("organization"))
            if not (lorg == org or (len(org) > 6 and (org in lorg or lorg in org))):
                continue
            ltoks = _title_tokens(l.get("title") or "")
            if not ltoks:
                continue
            jacc = len(toks & ltoks) / len(toks | ltoks)
            same_term = (not rec.get("seasons") or not l.get("seasons") or set(rec["seasons"]) & set(l["seasons"]))
            if jacc >= 0.85 and same_term and _year(rec.get("title")) == _year(l.get("title")):
                return lid
        return None

    # ----- writes -----------------------------------------------------------
    def touch_seen(self, key: str) -> None:
        """A posting is still listed in its feed (no content change)."""
        for p in self.postings_for(key):
            p["last_seen"] = now_iso()

    def upsert(self, rec: dict, raw: RawPosting, text_hash: str) -> str:
        """Add or update a listing from one extracted record.
        Returns "added", "updated" or "unchanged"."""
        now = now_iso()
        posting_key = raw.key
        sub_id = posting_key if not raw.multi else f"{posting_key}#{norm(rec.get('title'))}|{','.join(rec.get('seasons') or [])}"

        lid = self.by_sub.get(sub_id)
        if lid is None:
            lid = self._find_duplicate(rec)

        posting = {
            "key": posting_key, "sub_id": sub_id, "source": raw.source, "url": rec.get("apply_url") or raw.url,
            "source_url": raw.source_url, "text_hash": text_hash, "check": raw.check, "last_seen": now,
            "first_seen": now, "last_read": now, "last_verified": now, "gone_count": 0, "status": "live",
        }

        if lid is None:
            lid = sha(dedupe_key(rec) + posting_key, 12)
            listing = {"id": lid, **{f: rec.get(f) for f in FIELDS}, "org_size": raw.org_size,
                       "sector": raw.sector, "first_seen": now, "last_updated": now, "last_verified": now,
                       "status": "active", "removed_at": None, "removed_reason": None, "postings": [posting]}
            self.listings[lid] = listing
            self._reindex()
            return "added"

        listing = self.listings[lid]
        existing = next((p for p in listing["postings"] if p.get("sub_id", p["key"]) == sub_id), None)
        changed = False
        if existing:
            if existing.get("text_hash") != text_hash:
                changed = True
            existing.update({k: posting[k] for k in ("url", "text_hash", "check", "last_seen", "last_read", "status")})
            existing["gone_count"] = 0
        else:
            listing["postings"].append(posting)   # same role found via another source
            changed = True

        same_source_update = existing is not None
        for f in FIELDS:
            new = rec.get(f)
            if new in (None, [], ""):
                continue
            if listing.get(f) in (None, [], "") or (same_source_update and listing.get(f) != new):
                listing[f] = new
                changed = True
        if listing["status"] != "active":
            listing.update(status="active", removed_at=None, removed_reason=None)
            changed = True
        if changed:
            listing["last_updated"] = now
        self._reindex()
        return "updated" if changed else "unchanged"

    def remove(self, lid: str, reason: str) -> None:
        l = self.listings[lid]
        if l["status"] == "active":
            l.update(status="removed", removed_at=now_iso(), removed_reason=reason)

    def expire_deadlines(self) -> list[str]:
        """Remove active listings whose deadline is in the past."""
        today = today_iso()
        gone = []
        for lid, l in self.listings.items():
            if l["status"] == "active" and l.get("deadline") and not l.get("rolling") and l["deadline"] < today:
                self.remove(lid, f"deadline passed ({l['deadline']})")
                gone.append(lid)
        return gone

    # ----- persistence ------------------------------------------------------
    def save(self, sources_monitored: int | None = None) -> None:
        if sources_monitored is not None:
            self.meta["sources_monitored"] = sources_monitored
        save_json(LISTINGS_STATE, {"meta": self.meta, "listings": self.listings, "skipped": self.skipped})
        self.publish()

    def publish(self) -> None:
        """Write the active listings to docs/data/listings.json for the website."""
        active = []
        for l in self.listings.values():
            if l["status"] != "active":
                continue
            pub = {k: v for k, v in l.items() if k not in ("postings", "removed_at", "removed_reason", "status")}
            pub["sources"] = sorted({p["source"] for p in l["postings"]})
            pub["links"] = [{"source": p["source"], "url": p["url"]} for p in l["postings"] if p["status"] != "gone"]
            pub["apply_url"] = l.get("apply_url") or (pub["links"][0]["url"] if pub["links"] else None)
            active.append(pub)
        active.sort(key=lambda x: (x.get("deadline") or "9999", x.get("organization") or ""))
        save_json(PUBLIC_LISTINGS, {
            "generated_at": now_iso(),
            "last_collect": self.meta.get("last_collect"),
            "last_verify": self.meta.get("last_verify"),
            "sources_monitored": self.meta.get("sources_monitored"),
            "count": len(active),
            "listings": active,
        })
