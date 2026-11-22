"""Recall regressions from the November 2026 enrichment rework.

These cover the November 2026 enrichment rework. Each case is a regression that
once cost real leads: a company was wrongly dropped (or wrongly kept) because the
name-corroboration and fallback logic had a gap. Keep each case small and named
for the behaviour it pins.
"""

import sqlite3

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, check_eq, finish

# --- BLOCK A: title match from a Places business title alone -----------------
# A Places hit with no website is corroborated only by its title. Two distinctive
# tokens are needed, so a single shared word must not admit a different business.
check("title with extra words still matches the company",
      P._title_matches_company("Keystone Construction & Renovation", "KEYSTONE CONSTRUCTION") is True)
check("shared first word alone does not match (Keystones Restaurants)",
      P._title_matches_company("Keystones Restaurants", "KEYSTONE CONSTRUCTION") is False)
check("empty title never matches",
      P._title_matches_company("", "KEYSTONE CONSTRUCTION") is False)
check("one distinctive token in a longer title is not enough (Calgary Keystone Cafe)",
      P._title_matches_company("Calgary Keystone Cafe", "KEYSTONE CONSTRUCTION") is False)

# --- BLOCK B: fallback_name (Places title) corroborates a DBA domain ---------
# The permit name shares no usable token with the domain, so without the
# fallback the website is blanked. The Places title is a trading name whose
# tokens match the domain, so passing it as fallback_name must keep the site.
REC_B = {
    "company_name": "ZZ PARTNERS",
    "unmasked_tradename": "",
    "website": "https://summitroofing.ca",
    "phone": "(403) 555-1234",
    "email": "",
}
website_no_fallback = P.sanitize_cached_record(dict(REC_B))["website"]
website_with_fallback = P.sanitize_cached_record(dict(REC_B), "Summit Roofing")["website"]
print(f"observed: no fallback -> {website_no_fallback!r}; fallback 'Summit Roofing' -> {website_with_fallback!r}")
check_eq("without fallback_name the website is blanked", website_no_fallback, "")
check_eq("with fallback_name the website is kept", website_with_fallback, "https://summitroofing.ca")
check("the fallback pair discriminates (values differ)",
      website_no_fallback != website_with_fallback)

# --- BLOCK C: enrich_company walks the search_web URL list in order ----------
# Offline: every network-facing function is replaced, then restored in finally.
CANDIDATE_URLS = [
    "https://dirlist-one.example",
    "https://dirlist-two.example",
    "https://prospecthomes.ca",
]
CONTACT_PHONE = "(403) 555-1234"
CONTACT_EMAIL = "test@example.ca"

_saved = {name: getattr(P, name) for name in (
    "serper_places", "search_web", "licence_lookup", "cache_get", "cache_put", "scrape_contacts")}
scrape_calls: list[str] = []


def _fake_scrape(url, require_local_signal=False):
    scrape_calls.append(url)
    if url == CANDIDATE_URLS[2]:
        return (url, CONTACT_PHONE, CONTACT_EMAIL)
    return ("", "", "")


try:
    P.serper_places = lambda company: {}
    P.search_web = lambda query, company: list(CANDIDATE_URLS)
    P.licence_lookup = lambda company, address: {}
    P.cache_get = lambda conn, name: None
    P.cache_put = lambda conn, rec: None
    P.scrape_contacts = _fake_scrape

    conn = sqlite3.connect(":memory:")
    result = P.enrich_company(conn, "PROSPECT HOMES INC", "")
    conn.close()
finally:
    for name, fn in _saved.items():
        setattr(P, name, fn)

check_eq("scrape_contacts called once per candidate, in order",
         scrape_calls, CANDIDATE_URLS)
check("returned phone is the one from the third candidate",
      result.get("phone") == CONTACT_PHONE)

finish()
