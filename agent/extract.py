"""Turn a raw posting into structured records.

Two extractors share one output format:
  * ClaudeExtractor  - the real one. Sends the posting text to the Claude API
                       with a JSON schema, so the reply is always valid JSON.
  * KeywordExtractor - a fallback used when ANTHROPIC_API_KEY isn't set (handy
                       for local testing). It only fills fields it can match
                       with simple patterns and leaves the rest blank.

Data-quality guard rails (both extractors):
  * Every field is nullable; the prompt tells Claude to leave a field null
    unless the text states it. Nothing is guessed.
  * `ground()` double-checks the riskiest fields against the source text and
    blanks any value that doesn't appear there (pay, GPA, dates, links).
  * Results are cached by a hash of the posting text, so an unchanged posting
    is never paid for twice.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime

from .common import EXTRACTION_CACHE, RawPosting, load_json, save_json, sha

OPPORTUNITY_TYPES = ["Industry Internship", "Co-op", "REU", "National Lab", "Government Program",
                     "Fellowship", "Clinical/Hospital Research"]
SEASONS = ["Summer", "Fall", "Spring", "Year-round/Off-cycle"]
CLASS_YEARS = ["High school", "Freshman", "Sophomore", "Junior", "Senior", "Graduate student",
               "Recent graduate", "Community college"]
CITIZENSHIP = ["US citizen only", "US citizen or permanent resident", "International students eligible"]
REMOTE = ["On-site", "Remote", "Hybrid"]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def build_schema(major_names: list[str]) -> dict:
    """JSON schema the Claude response must follow (structured outputs)."""
    item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_student_opportunity": {"type": "boolean"},
            "organization": _nullable({"type": "string"}),
            "title": _nullable({"type": "string"}),
            "locations": {"type": "array", "items": {"type": "string"}},
            "states": {"type": "array", "items": {"type": "string"}},
            "remote": _nullable({"type": "string", "enum": REMOTE}),
            "opportunity_type": _nullable({"type": "string", "enum": OPPORTUNITY_TYPES}),
            "seasons": {"type": "array", "items": {"type": "string", "enum": SEASONS}},
            "majors": {"type": "array", "items": {"type": "string", "enum": major_names}},
            "class_years": {"type": "array", "items": {"type": "string", "enum": CLASS_YEARS}},
            "min_gpa": _nullable({"type": "number"}),
            "citizenship": _nullable({"type": "string", "enum": CITIZENSHIP}),
            "export_control": _nullable({"type": "boolean"}),
            "pay": _nullable({"type": "string"}),
            "paid": _nullable({"type": "boolean"}),
            "housing": _nullable({"type": "boolean"}),
            "letters_required": _nullable({"type": "integer"}),
            "open_date": _nullable({"type": "string"}),
            "deadline": _nullable({"type": "string"}),
            "rolling": _nullable({"type": "boolean"}),
            "apply_url": _nullable({"type": "string"}),
            "summary": _nullable({"type": "string"}),
        },
    }
    item["required"] = list(item["properties"].keys())
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"opportunities": {"type": "array", "items": item}},
        "required": ["opportunities"],
    }


def build_system_prompt(majors: dict[str, list[str]]) -> str:
    major_lines = "\n".join(f"- {name}: {', '.join(kws)}" for name, kws in majors.items())
    return f"""You extract structured data about internships and research opportunities for US undergraduate STEM students from job postings and program web pages.

Core rule: only record what the text actually states. If a field is not stated, use null (or an empty list). Never guess, infer from typical practice, or fill in a plausible value. A blank field is correct; an invented one is a serious error.

Return one entry in "opportunities" per distinct opportunity. A job posting is usually one entry. A program page may describe several sessions (for example separate Summer and Fall terms with different deadlines); give each its own entry. If the text is not a student opportunity at all (a full-time experienced role, a news article, an error page), return a single entry with is_student_opportunity = false and other fields null/empty.

Field guide:
- organization: the host employer, lab, or institution.
- title: the role or program name as written.
- locations: places as written ("Cambridge, MA"). states: two-letter US state codes for those places only.
- remote: only if the text says on-site, remote, or hybrid.
- opportunity_type: Industry Internship (company internship), Co-op (multi-term/co-op), REU (NSF REU or university summer undergraduate research), National Lab (DOE or other national laboratory program), Government Program (federal agency program such as NASA, NIH, USAJobs), Fellowship, Clinical/Hospital Research.
- seasons: the term(s) the work happens in. "Off-cycle" or year-round roles use Year-round/Off-cycle.
- majors: choose only from this fixed list, mapping the fields the posting names (e.g. "cell biology" and "molecular biology" both map to Biology; "chemical engineering" maps to Engineering-Chemical). Leave empty if no field of study is named or implied by the role's discipline. Do not add majors for generic phrases like "STEM" unless the posting lists them.
{major_lines}
- class_years: years eligible as stated (e.g. "rising juniors" -> Junior; "graduating seniors" -> Senior; "currently enrolled in a PhD program" -> Graduate student).
- min_gpa: a number only if a minimum GPA is stated.
- citizenship: "US citizen only", "US citizen or permanent resident" (also for "U.S. persons"/green card holders), or "International students eligible" only if the text explicitly says non-citizens may apply. null if not mentioned.
- export_control: true if the posting mentions ITAR, EAR, export control, "U.S. person" requirements for export reasons, or a security clearance. null if not mentioned.
- pay: the pay or stipend as written (e.g. "$25-$32/hour", "$6,000 stipend"). paid: true if any pay/stipend is stated, false only if the text says unpaid.
- housing: true if housing is provided or paid for; false only if the text says it is not.
- letters_required: number of recommendation letters if stated.
- open_date, deadline: ISO format YYYY-MM-DD, only for explicit dates. If the year is not written, use null. rolling: true if the text says applications are reviewed on a rolling basis.
- apply_url: a link to apply only if it appears in the text.
- summary: one plain sentence (max 30 words) describing the work, based only on the text."""


# ---------------------------------------------------------------------------
# Grounding: blank out risky fields the source text doesn't support.
# ---------------------------------------------------------------------------
def _valid_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def ground(rec: dict, raw: RawPosting) -> dict:
    text = raw.text or ""
    low = text.lower()
    # Pay must contain at least one number that appears in the text.
    if rec.get("pay"):
        nums = re.findall(r"\d[\d,.]*", rec["pay"])
        if not nums or not any(n.rstrip(".,") in text for n in nums):
            rec["pay"] = None
    # GPA must appear in the text (as "3.0" or "3").
    if rec.get("min_gpa") is not None:
        g = rec["min_gpa"]
        if not (f"{g:.1f}" in text or f"{g:g}" in text or f"{g:.2f}" in text):
            rec["min_gpa"] = None
    # Dates must be valid, and their day number and year must appear in the text.
    for f in ("deadline", "open_date"):
        d = _valid_date(rec.get(f))
        if d:
            y, m, dd = d.split("-")
            if y not in text or not re.search(rf"\b0?{int(dd)}\b", text):
                d = None
        rec[f] = d
    # Apply URL must appear in the text; otherwise use the posting link itself.
    if rec.get("apply_url") and rec["apply_url"] not in text:
        rec["apply_url"] = None
    rec["apply_url"] = rec.get("apply_url") or raw.url
    # Housing / letters: require the topic to be mentioned at all.
    if rec.get("housing") is not None and "hous" not in low and "lodging" not in low:
        rec["housing"] = None
    if rec.get("letters_required") is not None and "recommend" not in low and "reference" not in low:
        rec["letters_required"] = None
    rec["states"] = [s.upper() for s in rec.get("states", []) if re.fullmatch(r"[A-Za-z]{2}", s or "")]
    return rec


# ---------------------------------------------------------------------------
# Cache (shared by both extractors)
# ---------------------------------------------------------------------------
class ExtractionCache:
    def __init__(self):
        self.data = load_json(EXTRACTION_CACHE, {})
        self.lock = threading.Lock()

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        with self.lock:
            self.data[key] = {"at": datetime.utcnow().date().isoformat(), "result": value}

    def save(self, keep: int = 20000):
        # Keep the newest entries only, so the file can't grow forever.
        items = sorted(self.data.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:keep]
        save_json(EXTRACTION_CACHE, dict(items), indent=None)


# ---------------------------------------------------------------------------
# Claude extractor
# ---------------------------------------------------------------------------
class ExtractionError(Exception):
    pass


class ClaudeExtractor:
    name = "claude"

    def __init__(self, settings: dict, majors: dict[str, list[str]]):
        import anthropic  # imported here so keyword-only runs don't need the package

        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from the environment
        self.model = settings["llm"]["model"]
        self.effort = settings["llm"].get("effort", "low")
        self.max_chars = settings["llm"].get("max_input_chars", 12000)
        self.schema = build_schema(list(majors.keys()))
        self.system = build_system_prompt(majors)
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}

    def extract(self, raw: RawPosting) -> list[dict]:
        text = (raw.text or "")[: self.max_chars]
        hint = f"Source: {raw.source}\nPosting link: {raw.url}\nOrganization hint: {raw.organization or 'unknown'}\n\n"
        kwargs = dict(
            model=self.model,
            max_tokens=4000,
            # The system prompt is identical on every call, so mark it cacheable;
            # repeat calls then read it at a fraction of the normal input price.
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": hint + "<posting>\n" + text + "\n</posting>"}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": self.schema}},
        )
        if self.model.startswith(("claude-opus", "claude-sonnet", "claude-fable")):
            # If a safety classifier declines a request (rare for job postings),
            # let the API retry it on a fallback model instead of failing.
            resp = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = self.client.messages.create(**kwargs)

        u = resp.usage
        self.usage["calls"] += 1
        self.usage["input_tokens"] += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["output_tokens"] += u.output_tokens or 0

        if resp.stop_reason == "refusal":
            raise ExtractionError("model declined to process this posting")
        if resp.stop_reason == "max_tokens":
            raise ExtractionError("response was cut off (max_tokens)")
        body = next((b.text for b in resp.content if b.type == "text"), None)
        if not body:
            raise ExtractionError("empty response")
        return json.loads(body)["opportunities"]


# ---------------------------------------------------------------------------
# Keyword fallback extractor (no API key needed)
# ---------------------------------------------------------------------------
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}
_CODES = set(US_STATES.values())
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"


class KeywordExtractor:
    name = "keywords"

    def __init__(self, settings: dict, majors: dict[str, list[str]]):
        self.majors = majors
        self.usage = {"calls": 0}

    def _states(self, loc: str) -> list[str]:
        found = []
        for m in re.finditer(r"\b(?:,\s*|US[-\s])([A-Z]{2})\b", loc or ""):
            if m.group(1) in _CODES:
                found.append(m.group(1))
        low = (loc or "").lower()
        found += [code for name, code in US_STATES.items() if re.search(rf"\b{name}\b", low)]
        return sorted(set(found))

    def extract(self, raw: RawPosting) -> list[dict]:
        self.usage["calls"] += 1
        text = raw.text or ""
        low = text.lower()
        title = raw.title or ""
        tl = title.lower()

        majors = [m for m, kws in self.majors.items()
                  if any(re.search(rf"\b{re.escape(k)}\b", low) for k in kws)]
        seasons = [s for s in ("Summer", "Fall", "Spring") if re.search(rf"\b{s.lower()}\b", tl or low[:600])]
        if re.search(r"off[- ]cycle|year[- ]round", low):
            seasons.append("Year-round/Off-cycle")

        otype = raw.default_type
        if re.search(r"\bco-?op\b", tl):
            otype = "Co-op"
        elif "fellowship" in tl:
            otype = "Fellowship"
        elif re.search(r"\breu\b|research experiences? for undergraduates", low):
            otype = "REU"

        citizenship = None
        if re.search(r"u\.?s\.? citizens?(hip)?\b.{0,40}(or|and/or)\s+(lawful\s+)?permanent resident", low):
            citizenship = "US citizen or permanent resident"
        elif re.search(r"\b(must be|requires?|only)\b.{0,30}u\.?s\.? citizen", low):
            citizenship = "US citizen only"
        elif re.search(r"international students (are )?(eligible|welcome|may apply)", low):
            citizenship = "International students eligible"

        pay = None
        m = re.search(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[-–]\s?\$?\s?\d[\d,]*(?:\.\d+)?)?\s*(?:/|per\s)?\s*(?:hour|hr|week|month|year|annually|stipend)?", text, re.I)
        if m and re.search(r"pay|salary|stipend|compensation|hour|wage", text[max(0, m.start() - 80):m.end() + 40], re.I):
            pay = m.group(0).strip()

        gpa = None
        g = re.search(r"(?:minimum|min\.?|at least)[^.]{0,30}?\b([234]\.\d{1,2})\b[^.]{0,15}gpa|gpa[^.]{0,30}?\b([234]\.\d{1,2})\b", low)
        if g:
            gpa = float(g.group(1) or g.group(2))

        deadline = None
        d = re.search(rf"deadline[^.\n]{{0,40}}?\b({MONTHS})\s+(\d{{1,2}}),?\s+(20\d\d)", low)
        if d:
            deadline = datetime.strptime(f"{d.group(1)} {d.group(2)} {d.group(3)}", "%B %d %Y").date().isoformat()

        letters = None
        lw = re.search(r"\b(one|two|three|1|2|3)\s+(?:letters?|recommendations?)\s+(?:of\s+)?(?:recommendation|reference)?", low)
        if lw:
            letters = {"one": 1, "two": 2, "three": 3}.get(lw.group(1), None) or int(lw.group(1))

        rec = {
            "is_student_opportunity": True,
            "organization": raw.organization,
            "title": raw.title,
            "locations": [l.strip() for l in (raw.location or "").split(";") if l.strip()],
            "states": self._states(raw.location or ""),
            "remote": "Remote" if re.search(r"\bremote\b", tl) else ("Hybrid" if "hybrid" in tl else None),
            "opportunity_type": otype,
            "seasons": seasons,
            "majors": majors,
            "class_years": [y for y in ("Freshman", "Sophomore", "Junior", "Senior") if y.lower() in low],
            "min_gpa": gpa,
            "citizenship": citizenship,
            "export_control": True if re.search(r"\bitar\b|export control|security clearance|\bear\b", low) else None,
            "pay": pay,
            "paid": True if pay else None,
            "housing": True if re.search(r"(free|provided|includes?|on-campus)\s+housing|housing (is )?(provided|included)", low) else None,
            "letters_required": letters,
            "open_date": None,
            "deadline": deadline,
            "rolling": True if "rolling basis" in low or "rolling admission" in low else None,
            "apply_url": raw.url,
            "summary": None,
        }
        return [rec]


def make_extractor(settings: dict, majors: dict[str, list[str]]):
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeExtractor(settings, majors)
    print("NOTE: ANTHROPIC_API_KEY not set - using the keyword fallback extractor (fewer fields filled).")
    return KeywordExtractor(settings, majors)
