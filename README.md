# STEM Opportunity Board

An automatically updated tracker of internships, co-ops, REUs and research programs for US undergraduates in STEM fields outside software: biology, biochemistry, chemistry, chemical and biomedical engineering, mechanical and aerospace engineering, materials science, environmental science, physics and public health.

It has three parts:

1. **Collection agent** (`agent/`): a Python script that GitHub Actions runs on a schedule. Each day it pulls new listings from employer job systems and research-program sites. Each night it checks every application link.
2. **Opportunity board** (`docs/`): a static website that reads `docs/data/listings.json`. It's free to host on GitHub Pages, and nobody needs an account to browse it.
3. **My Applications** (also in `docs/`): a personal tracker with pipeline stages, checklists, recommenders and notes. It's stored in your browser, and you can export it as JSON.

```
config/            <- what to collect (edit these, no code changes needed)
  companies.yaml       large employers + their applicant tracking system
  research_sources.yaml  research programs / government programs
  majors.yaml          the fixed list of majors + synonyms
  settings.yaml        model, cost caps, rate limits, title filters
agent/             <- the collection agent (Python)
  run.py               entry point: collect / verify / requests
  ats.py               Workday, Greenhouse, Lever, iCIMS adapters + ATS detection
  sources/             Pathways to Science, single program pages, USAJobs
  extract.py           Claude API extraction (+ keyword fallback) and guard rails
  store.py             merging, de-duplication, removal, publishing
  http.py              polite fetching: robots.txt, rate limits, login detection
  discover.py          weekly web search for new programs
  requests_queue.py    "Request a company" processing
data/              <- agent state (full history, run log, caches)
docs/              <- the website (GitHub Pages serves this folder)
  data/listings.json   active listings (written by the agent)
.github/workflows/ <- the schedules
```

---

## Setup

You need a GitHub account (free) and an Anthropic API key (paid, usually a few dollars a month; see [API cost](#api-cost)). The agent also runs without a key, but then it uses a much weaker keyword-based extractor.

### 1. Run it on your computer first (optional, about 5 minutes)

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python -m agent.run collect --only Moderna
python -m agent.run verify
```

To view the site, serve the `docs` folder (opening the file directly won't work, because browsers block `fetch` from `file://`):

```bash
python -m http.server 8000 --directory docs
```

Then open http://localhost:8000.

To try the real Claude extractor locally, set the key in your terminal session first. Don't put it in any file.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # macOS/Linux
$env:ANTHROPIC_API_KEY="sk-ant-..."      # Windows PowerShell
```

### 2. Create the GitHub repository

1. On GitHub, create a new repository (for example `stem-tracker`). It can be public or private, but GitHub Pages on a private repository needs a paid plan.
2. Upload this folder: on the new repo's page, click **uploading an existing file** and drag everything in. Alternatively, use git:
   ```bash
   git init && git add . && git commit -m "Initial version"
   git branch -M main
   git remote add origin https://github.com/YOUR-USER/stem-tracker.git
   git push -u origin main
   ```
3. Edit `docs/config.js` and set `githubRepo: "YOUR-USER/stem-tracker"`.
4. Edit `config/settings.yaml` and put your repository URL in `user_agent`. This lets site owners see who is crawling.

### 3. Add your API key as a secret (never in code)

1. Get a key at https://console.anthropic.com (**Settings → API keys**). Add a few dollars of credit, and set a monthly spend limit under **Limits** as a safety net.
2. In your GitHub repository, go to **Settings → Secrets and variables → Actions → New repository secret**.
3. Name: `ANTHROPIC_API_KEY`. Value: your key.

Optional, for federal student jobs:

- Request a free key at https://developer.usajobs.gov/apirequest/.
- Add the secrets `USAJOBS_API_KEY` and `USAJOBS_EMAIL` (the email you registered with).
- Set `enabled: true` on the USAJobs entry in `config/research_sources.yaml`.

### 4. Turn on the website

**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main`, folder `/docs` → Save.**

After a minute your site is live at `https://YOUR-USER.github.io/stem-tracker/`.

### 5. Turn on the schedule

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **Collect listings → Run workflow** to do a first run right away.
3. Create an issue label named `company-request` (**Issues → Labels → New label**). The request form uses it.

From then on, both jobs run on their own:

| Workflow | When | What it does | API cost |
|---|---|---|---|
| `collect.yml` | daily, 11:00 UTC | processes company requests, pulls new listings, runs the LLM extraction on new or changed postings, and on Sundays also searches the web for new programs | yes |
| `verify.yml` | nightly, 07:00 UTC | checks every active posting link, then removes listings that are offline or past their deadline | none |

Each run commits the updated JSON files, so the site updates automatically. You can read a run's summary table on its page in the Actions tab, and the full history is in `data/run_log.json`.

---

## How the agent works

**Large employers** (`config/companies.yaml`). Most big companies post jobs through an applicant tracking system (ATS), and each ATS serves structured JSON. The agent reads that JSON directly instead of scraping page layouts:

| ATS | Endpoint used | Example employers |
|---|---|---|
| Workday | `POST https://HOST/wday/cxs/TENANT/SITE/jobs` | Pfizer, Moderna, J&J, Boeing, Merck, Amgen, Northrop, RTX… |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/BOARD/jobs` | SpaceX |
| Lever | `api.lever.co/v0/postings/COMPANY` | many startups |
| iCIMS | the portal's search page (experimental) | — |

When a company is set to `ats: auto`, the agent fetches its careers page and looks for links to a known ATS. If it finds none, it tries likely Greenhouse and Lever board names. It confirms every candidate with a live API call, then caches the answer in `data/ats_cache.json`.

Job titles are filtered to internships, co-ops and early-career roles using the patterns in `settings.yaml`.

**Research programs** (`config/research_sources.yaml`):

- **Pathways to Science**: a directory of over 500 summer programs, including many NSF REU sites. The agent walks its A–Z result pages and reads up to 40 new program pages per run, so the full directory fills in over about two weeks.
- **Single program pages** such as DOE SULI and NASA internships. Claude reads the page and can return several records, for example separate Summer and Fall terms.
- **Discovered sources**: each week Claude runs web searches for new programs. The agent checks that each candidate page loads and that robots.txt allows it, then adds it to `data/discovered_sources.json`. From then on it is collected like any other source.

**Extraction** (`agent/extract.py`). Each new or changed posting goes to Claude with a fixed JSON schema, so the reply is always well-formed. Majors and other categories must come from fixed lists. Guard rails against made-up data:

- The prompt says to leave any unstated field blank.
- `ground()` re-checks the riskiest fields against the source text and blanks anything it can't find there: pay figures, GPA, dates and apply links.
- Results are cached by a hash of the posting text, so the agent never pays twice for the same text.

**De-duplication** (`agent/store.py`). One listing can have several postings. The same role found through two sources, or posted in several cities, becomes one row on the board. Two postings count as the same role when the organization and title match (exactly or nearly), with the same season and year.

**Removal.** The nightly check removes a listing when every one of its postings returns 404/410 or shows a "no longer available" page, or when its deadline has passed. Network errors never cause a removal. Removed listings stay in `data/listings_all.json` for history but disappear from the board. Listings you saved in **My Applications** keep their own copy, so they stay in your tracker.

**Politeness.** The agent:

- reads robots.txt before every fetch,
- waits at least 1.5 seconds between requests to the same host (longer if robots.txt asks),
- retries with backoff,
- skips anything that redirects to a login page.

Sites that block automated clients are left alone. As of October 2026 these are the NSF REU directory and the NIH SIP page; see "Next steps".

**Health checks.** If a source returns zero results but normally returns some, the run log flags it as `possibly broken`, and the Actions run summary shows a WARNING.

---

## Common changes

- **Add a company:** copy a block in `config/companies.yaml`. If you know the Workday, Greenhouse or Lever details, fill in `ats_config`. If not, set `ats: auto` and the agent will try to detect them.
- **Enable more of the starter list:** set `enabled: true`. Fifteen of the 20 requested companies already have verified endpoints.
- **Add a program page:** add a `program_page` entry to `config/research_sources.yaml` with its URL.
- **Add a major or synonym:** edit `config/majors.yaml`.
- **Add a preset page on the site:** add an entry to `VIEWS` near the top of `docs/app.js`.
- **Change schedules:** edit the `cron:` lines in `.github/workflows/`. Times are in UTC.

---

## API cost

What drives the cost: the agent calls Claude only for **new or changed** postings. Unchanged postings, and postings it has already read, cost nothing. The nightly link check makes no API calls at all. `max_extractions_per_run` in `settings.yaml` (default 150) is a hard cap per run.

Estimates assume about 3,000 input tokens and 600 output tokens per posting:

| | Claude Opus 5.5 (default) | Claude Haiku 5.5 |
|---|---|---|
| Price (input / output per million tokens) | $4 / $20 | $0.10 / $0.50 |
| Per posting | ~$0.024 | ~$0.0006 |
| One-time backfill, starter sources (~850 postings over ~6 days) | ~$20 | ~$0.50 |
| **Monthly, starter sources** (~20–30 new or changed postings a day, plus weekly discovery) | **~$15–20** | **~$0.50** |
| Monthly, all 20 companies plus more sources | ~$50–60 | ~$1.50 |

To switch models, change `llm.model` in `config/settings.yaml` to `claude-haiku-5-5`. Haiku handles this kind of field extraction well, but it is a little less careful with ambiguous postings. A good approach is to start on Opus, spot-check the results, then try Haiku and compare.

Each run logs its actual token usage in `data/run_log.json` (look for `llm`). Check it after the first week and adjust.

GitHub Actions is free for public repositories. Private repositories get 2,000 free minutes a month, and these jobs use roughly 600–900.

---

## Next steps (roadmap)

1. **Enable the remaining 15 verified employers.** Turn them on 3–5 at a time and watch the run log for warnings.
2. **Write adapters for the 4 unresolved employers.** Lockheed Martin uses its own careers site, which needs a custom adapter or its sitemap. Eli Lilly, AbbVie and Dow need ATS detection run against their careers pages.
3. **Find another route for NSF REU and NIH SIP.** Both block automated clients. Options: the NSF ETAP opportunity listings, NIH's OITE data feeds if any are published, or manual seed entries refreshed by the program-page adapter.
4. **University summer programs.** Seed `research_sources.yaml` with 30–50 well-known programs (Amgen Scholars sites, HHMI EXROP, SURF programs at Caltech and NIST, Mayo SURF, etc.).
5. **More sources:** the other DOE lab programs (Zintellect/ORISE catalog), the NOAA Hollings and EPA programs, and more national labs.
6. **Use the Batch API for extraction** to halve LLM cost. This suits the daily job because it isn't time-sensitive.
7. **Accounts and sync for My Applications.** Swap localStorage for a small backend (for example Supabase) when you want the tracker on several devices.
8. **Email or RSS alerts** for new listings that match saved filters.
9. **An evaluation set:** 50 hand-labeled postings, to measure extraction accuracy before and after changing models or prompts.
