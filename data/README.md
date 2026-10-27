# data/

Agent state, written automatically by the GitHub Actions runs. Don't edit by hand
except `company_requests.json` (to queue a request manually).

- listings_all.json      every listing ever seen, including removed ones
- run_log.json           what each run added, updated, removed or failed
- extraction_cache.json  LLM results keyed by posting-text hash (saves API cost)
- ats_cache.json         detected applicant tracking systems for "auto" companies
- source_health.json     result counts per source, for "possibly broken" warnings
- discovered_sources.json / discovered_companies.json   sources the agent added itself
