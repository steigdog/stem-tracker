"""Adapters for research programs and other non-ATS sources.

Each module exposes `fetch(source_cfg, http, should_read) -> Iterable[RawPosting]`.
To add a new source type, write a module with that function and register it
in ADAPTERS below.
"""

from . import pathways, program_page, usajobs

ADAPTERS = {
    "pathways_directory": pathways.fetch,
    "program_page": program_page.fetch,
    "usajobs": usajobs.fetch,
}
