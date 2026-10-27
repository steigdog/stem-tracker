"""Process "Request a company" submissions.

The website's request form opens a pre-filled GitHub issue labelled
`company-request` (GitHub Pages is static, so it can't write files itself).
This step, run by GitHub Actions:
  1. copies new request issues into data/company_requests.json,
  2. tries to detect each company's ATS from its careers page,
  3. on success adds it to data/discovered_companies.json (collected from
     the next run on), otherwise marks it "needs_manual",
  4. comments on and closes the issue.
You can also add requests by hand to data/company_requests.json with
"status": "pending".
"""

from __future__ import annotations

import os
import re

import requests

from .ats import detect_ats
from .common import COMPANY_REQUESTS, DISCOVERED_COMPANIES, load_companies, load_json, load_settings, now_iso, save_json
from .run import RunLog, make_http

API = "https://api.github.com"


def _gh(method: str, path: str, **kw):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.request(method, f"{API}/repos/{repo}{path}", headers=headers, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.text else None


def _field(body: str, label: str) -> str | None:
    m = re.search(rf"^\s*{label}\s*:\s*(.+)$", body or "", re.I | re.M)
    return m.group(1).strip() if m and m.group(1).strip() not in ("", "-", "_No response_") else None


def process_requests() -> None:
    settings = load_settings()
    http = make_http(settings)
    log = RunLog("requests")
    queue = load_json(COMPANY_REQUESTS, [])
    seen_issues = {q.get("issue") for q in queue if q.get("issue")}

    # 1. Pull new issues labelled company-request.
    issues = _gh("GET", "/issues", params={"labels": "company-request", "state": "open", "per_page": 50}) or []
    for iss in issues:
        if iss["number"] in seen_issues:
            continue
        body = iss.get("body") or ""
        queue.append({
            "issue": iss["number"],
            "company": _field(body, "Company") or iss["title"].replace("Company request:", "").strip(),
            "careers_url": _field(body, "Careers page"),
            "sector": _field(body, "Sector") or "Other Industry",
            "note": _field(body, "Notes"),
            "status": "pending", "received_at": now_iso(),
        })

    # 2-3. Detect ATS and add the company.
    known = {c["name"].lower() for c in load_companies()}
    discovered = load_json(DISCOVERED_COMPANIES, [])
    for q in queue:
        if q["status"] != "pending":
            continue
        name = q["company"]
        src = log.source(name)
        if name.lower() in known:
            q.update(status="already_tracked", processed_at=now_iso())
            msg = f"{name} is already tracked."
        else:
            found = detect_ats(name, q.get("careers_url"), http)
            if found:
                discovered.append({"name": name, "enabled": True, "sector": q.get("sector", "Other Industry"),
                                   "careers_url": q.get("careers_url"), "ats": found[0], "ats_config": found[1],
                                   "origin": "request", "added_at": now_iso()})
                known.add(name.lower())
                q.update(status="added", ats=found[0], processed_at=now_iso())
                src["added"] = 1
                msg = f"Added {name} ({found[0]}). Its listings will appear after the next daily run."
            else:
                q.update(status="needs_manual", processed_at=now_iso())
                src["status"] = "needs manual setup"
                msg = (f"Couldn't detect a supported applicant tracking system for {name}. "
                       "The maintainer will need to add it to config/companies.yaml by hand.")
        log.entry["updated"].append(f"{name}: {q['status']}")
        # 4. Reply on the issue and close it.
        if q.get("issue"):
            try:
                _gh("POST", f"/issues/{q['issue']}/comments", json={"body": msg})
                _gh("PATCH", f"/issues/{q['issue']}", json={"state": "closed"})
            except Exception as e:
                log.fail(f"issue #{q['issue']}", repr(e))

    save_json(DISCOVERED_COMPANIES, discovered)
    save_json(COMPANY_REQUESTS, queue)
    log.save()
