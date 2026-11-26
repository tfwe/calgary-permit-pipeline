#!/usr/bin/env python3
"""
Calgary building/development permits -> B2B lead pipeline.

Pulls public City of Calgary open data, enriches each company with contact
details, caches the result, writes client-ready CSVs, and keeps a set of
Google Sheets updated in place.

Sources (all public, unauthenticated Socrata):
  building permits    https://data.calgary.ca/resource/c2es-76ed.json
  development permits https://data.calgary.ca/resource/6933-unw5.json
  business licences   https://data.calgary.ca/resource/vdjc-pybd.json

Enrichment order: Serper Places (needs SERPER_API_KEY) -> keyless Bing/DDG
fallback -> regex extraction from the company's own site. Emails are graded
with a keyless DNS MX lookup, so an undeliverable address never counts as
contact.

Outputs (regenerated every run; see README):
  calgary_leads_demo.csv      every lead, including unsellable ones (internal)
  calgary_needs_builder.csv   sellable: GC / early-design leads
  calgary_subtrade_leads.csv  sellable: subtrade leads
  calgary_needs_review.csv    not sellable as-is, with the reason why
  cache.db                    SQLite enrichment cache (age-based TTL)

Secrets are read from ~/.config/serper.env and ~/.config/gogcli/exec-env.sh,
so the pipeline does not depend on the caller's shell or PATH. Run it through
run.sh (or run-weekly.sh) rather than directly.

Exit codes: EXIT_* below. Entry point: main().
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import logging
import logging.handlers
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import config

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from cpp_config import *
from cpp_cache import *
from cpp_match import *
from cpp_fetch import *

# ---------------------------------------------------------------------------
# Paths and exit codes
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# SQLite enrichment cache
# ---------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            company_name TEXT PRIMARY KEY,
            unmasked_tradename TEXT DEFAULT '',
            licence_type TEXT DEFAULT '',
            website TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            address TEXT DEFAULT '',
            google_rating REAL DEFAULT NULL,
            kill_reason TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            contact_name TEXT DEFAULT '',
            contact_source TEXT DEFAULT '',
            contact_verified INTEGER DEFAULT 0
        )
    """)
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN google_rating REAL DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN kill_reason TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Existing cache.db files predate the contact columns; add them once.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
    for column, ddl in CONTACT_COLUMN_DDL:
        if column not in existing:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {column} {ddl}")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Calgary business-licence lookup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Keyless search fallbacks (DuckDuckGo / Bing)
# ---------------------------------------------------------------------------








































# ---------------------------------------------------------------------------
# Enrichment: places lookup, phone/email normalisation
# ---------------------------------------------------------------------------






def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[:3] not in CA_PHONE_AREA_CODES:
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


# ---------------------------------------------------------------------------
# Logging, cache freshness, email grading
# ---------------------------------------------------------------------------
_mx_cache: dict[str, bool | None] = {}


def domain_has_mx(email: str) -> bool | None:
    """MX lookup via Google public DNS-over-HTTPS (keyless). None when DNS is unreachable."""
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]
    result: bool | None = None
    try:
        resp = httpx.get("https://dns.google/resolve",
                         params={"name": domain, "type": "MX"}, timeout=6.0)
        if resp.status_code == 200:
            data = resp.json()
            result = any(a.get("type") == 15 for a in (data.get("Answer") or []))
    except Exception:  # noqa: BLE001
        result = None
    if result is not None:
        _mx_cache[domain] = result
    return result


def email_status(email: str) -> str:
    """Grade an address: mx_ok | role | no_mx | unverified | role_unverified."""
    if not email or "@" not in email:
        return ""
    local = email.rsplit("@", 1)[0].lower()
    role = any(local == r or local.startswith(r + suf)
               for r in _ROLE_LOCALS for suf in (".", "-", "_"))
    mx = domain_has_mx(email)
    if mx is False:
        return "no_mx"
    if mx is None:
        return "role_unverified" if role else "unverified"
    return "role" if role else "mx_ok"


def _cache_is_fresh(rec: dict, max_age_days: float, empty_retry_days: float) -> bool:
    """Contact-bearing records live long; blank ones get retried soon."""
    age = _cache_age_days(rec)
    if age is None:
        return False
    clean = sanitize_cached_record(rec)
    has_any = bool(clean.get("phone") or clean.get("email") or clean.get("website"))
    return age < (max_age_days if has_any else empty_retry_days)


def valid_email(email: str) -> bool:
    e = email.lower().strip(".,;:()[]<>\"'")
    if not EMAIL_RE.fullmatch(e):
        return False
    # Noisy local parts must be prefix checks: substring tests wrongly reject
    # legitimate addresses such as repair@acme.ca or their@acme.ca.
    local = e.split("@", 1)[0]
    noisy = (
        "careers", "jobs", "hr", "privacy", "media", "press", "webmaster",
        "accessibility", "eventcentre", "bondholders", "legal", "compliance",
        "security", "abuse", "ir",
    )
    for stem in noisy:
        if local == stem or local.startswith((stem + ".", stem + "-", stem + "_")):
            return False
    if local.startswith("investor") or local.startswith("information.texas"):
        return False
    bad_bits = (
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "sentry", "wix",
        "example.com", "domain.com", "email.com", "yourdomain", "godaddy",
        "squarespace", "wordpress", "no-reply", "noreply", "@2x", ".texas@",
    )
    return not any(b in e for b in bad_bits)


def email_priority(email: str) -> int:
    """Rank estimating/bid inboxes first, then general, then departmental."""
    prefix = email.lower().split("@", 1)[0]
    if any(k in prefix for k in ("estimat", "bid", "tender", "project", "quote")):
        return 0
    if any(k in prefix for k in ("info", "contact", "office", "hello", "studio",
                                 "sales", "reception", "admin")):
        return 1
    return 2


# ---------------------------------------------------------------------------
# Contact extraction from a company's own site
# ---------------------------------------------------------------------------
def extract_contacts(html: str, base_url: str) -> tuple[str, str]:
    """Return (phone, email) extracted from a page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ")
    # mailto links are the strongest email signal
    emails = []
    for a in soup.select('a[href^="mailto:"]'):
        href = a.get("href", "")
        candidate = href.split(":", 1)[1].split("?", 1)[0]
        emails.append(candidate)
    emails.extend(EMAIL_RE.findall(text))
    valid_emails = [clean(x) for x in emails if valid_email(clean(x))]
    valid_emails.sort(key=email_priority)
    email = valid_emails[0] if valid_emails else ""

    phones = []
    for a in soup.select('a[href^="tel:"]'):
        phones.append(a.get("href", "").split(":", 1)[1])
    raw_phones = phones + [m.group(0) for m in PHONE_RE.finditer(text)]
    phone = next((p for p in (normalize_phone(x) for x in raw_phones) if p), "")
    return phone, email




def _has_calgary_signal(html: str, url: str, phone: str, email: str) -> bool:
    """Whether a fetched page evidences a Calgary/Alberta presence.

    Guards the keyless-search fallback, where the failure mode is a foreign
    company with the same words in its name (GOLF CAVE in Calgary matching a US
    golfshop.com and scraping clark@golfshop.com).
    """
    if phone:                                   # already normalised: 403/587/825/368 only
        return True
    if _host(url).endswith(".ca"):
        return True
    if email.lower().endswith(".ca"):
        return True
    # Deliberately does NOT accept a bare "alberta": a Spruce Grove or Edmonton site
    # says Alberta too. Calgary/YYC, or a southern-Alberta postal code (T1-T3, which
    # excludes the Edmonton region's T4-T8), is what actually indicates Calgary.
    return bool(re.search(r"\b(calgary|yyc|t[123][a-z]\s*\d[a-z]\d)\b", html, re.I))


def scrape_contacts(url: str, require_local_signal: bool = False) -> tuple[str, str, str]:
    """Fetch homepage + contact page; return (website, phone, email).

    With require_local_signal=True the site is discarded unless some fetched page
    shows a Calgary/Alberta signal. Used for keyless-search fallbacks only; a
    Serper-verified place is already locality-checked.
    """
    website = strip_url_tracking(url)
    phone = ""
    email = ""
    local_signal = False
    try:
        html, final = fetch_page(url)
        website = strip_url_tracking(final)
        p, e = extract_contacts(html, final)
        phone, email = phone or p, email or e
        local_signal = _has_calgary_signal(html, final, phone, email)
        # Try contact pages only when we still miss a contact channel.
        if not (phone and email):
            base = urlparse(final)
            for path in ("/contact", "/contact-us", "/about", "/about-us"):
                try:
                    c_html, c_final = fetch_page(urljoin(f"{base.scheme}://{base.netloc}", path), timeout=6)
                    p2, e2 = extract_contacts(c_html, c_final)
                    phone, email = phone or p2, email or e2
                    local_signal = local_signal or _has_calgary_signal(c_html, c_final, phone, email)
                    if phone and email:
                        break
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]site fetch failed ({url[:60]}): {exc}[/yellow]")
    if require_local_signal and not local_signal:
        logger.info("discarded non-local fallback site: %s", url)
        return "", "", ""
    return website, phone, email


# ---------------------------------------------------------------------------
# Per-company enrichment pipeline
# ---------------------------------------------------------------------------
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.ca", "hotmail.com", "hotmail.ca",
    "outlook.com", "live.com", "live.ca", "icloud.com", "me.com",
    "shaw.ca", "telus.net", "telusplanet.net", "sasktel.net", "bell.net", "rogers.com",
}


def sanitize_cached_record(rec: dict, fallback_name: str = "") -> dict:
    """Clean a cached record: junk/unrelated URLs, non-local phones, poisoned contacts.

    A website that fails the name check is discarded AND so are the phone and
    email harvested alongside it, because they came from the same wrong company:
    MEADOW DRAFTING AND CONSULTING kept a Calgary number and a meadowmiddleeast.com
    address that had both been scraped from the rejected meadowprojects.com, and
    both were reaching a sellable list.

    The Google Places business title is passed in as fallback_name so a DBA domain
    can corroborate a company whose legal permit name differs from the brand.
    """
    company = rec.get("company_name", "")
    unmasked = rec.get("unmasked_tradename", "")
    fallback = clean(fallback_name)

    raw_phone = rec.get("phone", "")
    phone = normalize_phone(raw_phone) if raw_phone else ""
    raw_email = clean(rec.get("email", ""))
    email = raw_email if (raw_email and valid_email(raw_email)) else ""

    website = strip_url_tracking(rec.get("website", ""))
    website_rejected = False
    if (website and _url_path_depth(website) >= 2
            and not (_domain_related(website, company)
                     or (unmasked and _domain_related(website, unmasked))
                     or (fallback and _domain_related(website, fallback)))):
        # Deliberately before the email-host escape: a directory profile pairs
        # with an address on the directory's own domain
        # (builderhubs.com/calgary/builder/... with hello@builderhubs.com) and
        # would otherwise corroborate itself into a sellable list.
        website = ""
        website_rejected = True
    elif website and is_junk(website):
        website = ""
        website_rejected = True
    elif website and not (_domain_related(website, company)
                          or (unmasked and _domain_related(website, unmasked))
                          or (fallback and _domain_related(website, fallback))):
        # No email-host escape hatch here. A third-party directory pairs its own
        # profile URL with its own address (calgarynorthrent.com with
        # sales@calgarynorthrent.com, builderhubs.com with hello@builderhubs.com),
        # so "the email host matches the web host" is exactly how a wrong company
        # vouches for itself. An unrelated host means the whole harvest is wrong.
        website = ""
        website_rejected = True

    if website_rejected:
        phone = ""
        email = ""
    elif email:
        # With no website and no phone, nothing corroborates a company-specific
        # address, so it must at least relate to the name. Free/ISP mailboxes are
        # exempt: a small outfit legitimately runs on gmail or shaw.ca.
        host = email.split("@", 1)[1].lower()
        if host not in FREE_EMAIL_DOMAINS and not website and not phone and not (
            _domain_related(f"https://{host}", company)
            or (unmasked and _domain_related(f"https://{host}", unmasked))
            or (fallback and _domain_related(f"https://{host}", fallback))
        ):
            email = ""

    out = dict(rec)
    out["website"] = website
    out["phone"] = phone
    out["email"] = email
    if any(k in rec for k in CONTACT_COLUMNS):
        # A verified name belongs to the verified email; once that email is
        # purged the name has nothing left to be attached to.
        out["contact_name"] = clean(rec.get("contact_name") or "")
        out["contact_source"] = clean(rec.get("contact_source") or "")
        out["contact_verified"] = int(rec.get("contact_verified") or 0)
        if not email:
            out["contact_name"], out["contact_source"], out["contact_verified"] = "", "", 0
    return out


def enrich_company(conn: sqlite3.Connection, company: str, address: str, refresh: bool = False,
                   max_age_days: float = 90.0, empty_retry_days: float = 7.0) -> dict:
    cached = cache_get(conn, company)
    if cached and not refresh:
        if _cache_is_fresh(cached, max_age_days, empty_retry_days):
            clean_rec = sanitize_cached_record(cached)
            if any(clean_rec.get(k) != cached.get(k) for k in ("website", "phone", "email")):
                # Write the purge back so rejected contacts stop resurfacing, while
                # preserving updated_at to leave the TTL honest.
                logger.info("purged stale contact data from cache: %s", company)
                conn.execute(
                    "UPDATE companies SET website=?, phone=?, email=?, contact_name=?, "
                    "contact_source=?, contact_verified=? WHERE company_name=?",
                    (clean_rec.get("website", ""), clean_rec.get("phone", ""),
                     clean_rec.get("email", ""), clean_rec.get("contact_name", ""),
                     clean_rec.get("contact_source", ""),
                     int(clean_rec.get("contact_verified") or 0), cache_key(company)),
                )
                conn.commit()
                clean_rec["updated_at"] = cached.get("updated_at", "")
            return clean_rec
        logger.info("stale cache, re-enriching: %s (age=%s d)",
                    company, _cache_age_days(cached))

    numbered = bool(NUMBERED_RE.match(company))
    lic = licence_lookup(company, address)
    unmasked = lic.get("unmasked_tradename", "")
    # Avoid "unmasking" to a generic building name that merely shares the address.
    if numbered and unmasked and unmasked.upper() in {"APARTMENT BUILDING", "COMMERCIAL BUILDING"}:
        unmasked = ""

    search_name = unmasked if (numbered and unmasked) else company
    website = phone = email = ""
    google_rating = None
    place_title = ""

    if not numbered or unmasked:
        # 1) Serper Google Places is the highest-quality source for phone/website/rating.
        sp = serper_places(search_name)
        if sp:
            place_title = sp.get("title", "") or ""
            website = strip_url_tracking(sp.get("website", "") or "")
            raw_phone = sp.get("phone", "") or ""
            phone = normalize_phone(raw_phone)
            google_rating = sp.get("rating")
            if website:
                _, site_phone, site_email = scrape_contacts(website)
                phone = phone or site_phone
                email = email or site_email

        # 2) If Serper found no site, fall back to keyless Bing/DDG scraping.
        if not website:
            urls = search_web(f"{search_name} Calgary AB", search_name)
            if not urls:
                urls = search_web(f'"{search_name}" Calgary Alberta', search_name)
            if urls:
                site_phone = site_email = ""
                first_site = ""
                for i, candidate in enumerate(urls[:3]):
                    cand_site, cand_phone, cand_email = scrape_contacts(
                        candidate, require_local_signal=True)
                    if i == 0:
                        first_site = cand_site
                    if cand_phone or cand_email:
                        website, site_phone, site_email = (cand_site or candidate), cand_phone, cand_email
                        break
                else:
                    # No candidate yielded a contact: keep the first scrape's site as before.
                    website = first_site
                phone = phone or site_phone
                email = email or site_email
        time.sleep(0.4)
    else:
        console.print(f"[dim]skip web/Serper search (numbered shell, no unmask): {company}[/dim]")

    # Reason the record ends up blank; computed from pre-sanitize values.
    if numbered and not unmasked:
        kill_reason = "numbered shell, no licence unmask"
    elif not phone and not email and not website:
        kill_reason = "no places match and no validated search result"
    elif not phone and not email:
        kill_reason = "site found, no phone or email on it"
    else:
        kill_reason = ""

    rec = sanitize_cached_record({
        "company_name": company,
        "unmasked_tradename": unmasked,
        "licence_type": lic.get("licence_type", ""),
        "website": website,
        "phone": phone,
        "email": email,
        "address": address,
        "google_rating": google_rating,
        "kill_reason": kill_reason,
    }, place_title)
    if (not rec.get("phone") and not rec.get("email") and not rec.get("website")
            and not rec.get("kill_reason")):
        # A website was found and then blanked by the name-safety rules, so the
        # reason set above no longer describes the record. Say so instead of
        # leaving a blank with no cause.
        rec["kill_reason"] = "site rejected by name-safety rules"
    cache_put(conn, rec)
    rec["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["licence_active"] = lic.get("active", False)
    rec["licence_matched_by"] = lic.get("matched_by", "")
    return rec


# ---------------------------------------------------------------------------
# Classification and lead assembly
# ---------------------------------------------------------------------------
def pitch_for(lead_type: str, permit_stage: str = "", company_name: str = "") -> str:
    if lead_type == "SUBTRADE_PITCH" and permit_stage == "DEVELOPMENT_PERMIT":
        return "Pre-construction subtrade opportunity — development permit filed, trades not yet awarded"
    # An architect/design firm with no contractor of record runs the tender itself;
    # offering it full-build GC services misses. Ask for the bid list instead.
    # Guard against design-build contractors, whose names carry both signals.
    if (lead_type in {"NEEDS_GC", "EARLY_DESIGN"} and company_name
            and DESIGN_FIRM_RE.search(company_name)
            and not CONTRACTOR_NAME_RE.search(company_name)
            # A design-build firm names both roles and buys GC services itself.
            and not re.search(r"\bDESIGN[\s\-]*BUILD\b", company_name, re.I)):
        return "Architect / designer of record (no GC listed) — request invite to tender / bid list"
    return {
        "NEEDS_GC": "Full-build GC services — applicant has no contractor of record",
        "SUBTRADE_PITCH": "Subtrade / supplier opportunity — active GC permit",
        "EARLY_DESIGN": "Pre-construction outreach — development permit at design stage",
        "SUBCONTRACTOR": "Trade already active — not a buyer of subtrade services",
    }.get(lead_type, "Permit-based B2B outreach")


def build_leads(conn: sqlite3.Connection, permits: list[dict], refresh: bool = False,
                max_age_days: float = 90.0, empty_retry_days: float = 7.0) -> list[Lead]:
    # Enrich each unique target company once.
    enrichments: dict[str, dict] = {}
    failures = 0
    targets = 0
    for p in permits:
        key = cache_key(p["target_company"])
        if key in enrichments:
            continue
        targets += 1
        console.print(f"[cyan]enriching[/cyan] {p['target_company']}")
        try:
            enrichments[key] = enrich_company(
                conn, p["target_company"], p.get("address", ""), refresh=refresh,
                max_age_days=max_age_days, empty_retry_days=empty_retry_days,
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            console.print(f"[red]enrichment error for {p['target_company']}: {exc}[/red]")
            enrichments[key] = {"company_name": p["target_company"], "unmasked_tradename": "",
                                "licence_type": "", "website": "", "phone": "", "email": "",
                                "address": p.get("address", ""), "google_rating": None}

    if targets >= 5 and failures == targets:
        raise RuntimeError(
            f"enrichment failed for all {targets} companies — refusing to emit an empty lead list"
        )

    leads = []
    for p in permits:
        e = enrichments.get(cache_key(p["target_company"]), {})
        licence_type = e.get("licence_type", "")
        unmasked = e.get("unmasked_tradename", "")

        # If a NEEDS_GC or EARLY_DESIGN applicant turns out to be a licensed contractor/builder,
        # reclassify to SUBTRADE_PITCH so we never pitch GC services to an actual builder.
        lead_type = p["lead_type"]
        if lead_type in {"NEEDS_GC", "EARLY_DESIGN"} and (
            looks_like_contractor(p["target_company"], licence_type)
            or (unmasked and looks_like_contractor(unmasked, licence_type))
        ):
            lead_type = "SUBTRADE_PITCH"
        if lead_type == "SUBTRADE_PITCH" and (
            SUBCONTRACTOR_RE.search(p["target_company"])
            or (unmasked and SUBCONTRACTOR_RE.search(unmasked))
        ):
            lead_type = "SUBCONTRACTOR"

        leads.append(Lead(
            permit_number=p["permit_number"],
            permit_type=p["permit_type"],
            permit_stage=p.get("permit_stage", ""),
            lead_type=lead_type,
            status=p.get("status", ""),
            permit_value=p["permit_value"],
            company_name=p["target_company"],
            contact_name=e.get("contact_name", "") or "",
            contact_source=e.get("contact_source", "") or "",
            contact_verified=int(e.get("contact_verified") or 0),
            unmasked_tradename=unmasked,
            licence_type=licence_type,
            applicant_name=p["applicant_name"],
            contractor_name=p["contractor_name"],
            address=p["address"],
            community=p.get("community", ""),
            applied_date=p["applied_date"],
            issued_date=p.get("issued_date", ""),
            description=p["description"],
            website=e.get("website", ""),
            phone=e.get("phone", ""),
            email=e.get("email", ""),
            email_status=(email_status(e.get("email", "")) if e.get("email") else ""),
            google_rating=e.get("google_rating"),
            kill_reason=e.get("kill_reason", ""),
            pitch_angle=pitch_for(lead_type, p.get("permit_stage", ""),
                                  p.get("target_company", "")),
        ))
    return leads


# ---------------------------------------------------------------------------
# Output: CSVs and terminal summary
# ---------------------------------------------------------------------------
def export_csv(leads: list[Lead], path: Path) -> None:
    # Fields declared with exclude=True (extra_permits) are HTML-only detail;
    # including them would add an empty column and break the CSV's column count.
    _all = getattr(Lead, "model_fields", getattr(Lead, "__fields__", {}))
    fields = [name for name, info in _all.items()
              if not getattr(info, "exclude", False)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            row = lead.model_dump() if hasattr(lead, "model_dump") else lead.dict()
            writer.writerow(row)


def export_clean_client_csv(leads: list[Lead], path: Path) -> None:
    """11-column, zero-jargon CSV ready to send straight to Calgary contractors."""
    headers = [
        "Applied Date", "Permit #", "Address", "Community",
        "Est. Project Value", "Project Description",
        "Company / Applicant", "Contact Name", "Phone", "Email", "Email Status", "Website", "Lead Angle",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for x in leads:
            display_name = (x.unmasked_tradename
                            if NUMBERED_RE.match(x.company_name) and x.unmasked_tradename
                            else x.company_name)
            val_str = f"${x.permit_value:,.0f}" if x.permit_value > 0 else "TBD (Dev Permit)"
            writer.writerow([
                x.applied_date, x.permit_number, x.address, x.community, val_str,
                x.description, display_name, x.contact_name or "", x.phone, x.email, x.email_status,
                x.website, x.pitch_angle,
            ])


def review_reason(x: Lead) -> str:
    """Why a lead is not sellable as-is, for the manual review pile."""
    if x.email and x.email_status == "no_mx":
        return "email domain has no MX (undeliverable)"
    if not (x.phone or x.email or x.website):
        if x.kill_reason:
            return f"no contact channel found ({x.kill_reason})"
        return "no contact channel found"
    if not (x.phone or x.email):
        return "website only - no phone or email"
    return "unclassified - check manually"


def select_review(leads: list[Lead], gc_leads: list[Lead], sub_leads: list[Lead],
                  include_tract_builders: bool = False) -> tuple[list[Lead], int]:
    """Leads dropped from both sellable lists, minus deliberate policy exclusions.

    Two groups are excluded on purpose rather than pending judgement, so they must
    NOT land in a pile meant for human follow-up:
      * SUBCONTRACTOR - we would be pitching a trade its own trade
      * volume tract builders - locked-in annual vendor contracts
    Returns (review_leads, policy_excluded_count).
    """
    sold = {(x.permit_number, x.company_name) for x in gc_leads}
    sold |= {(x.permit_number, x.company_name) for x in sub_leads}
    review: list[Lead] = []
    excluded = 0
    for x in leads:
        if (x.permit_number, x.company_name) in sold:
            continue
        if x.lead_type == "SUBCONTRACTOR":
            excluded += 1
            continue
        if not include_tract_builders and is_volume_tract_builder(x.company_name, x.unmasked_tradename):
            excluded += 1
            continue
        review.append(x)
    return review, excluded


# Required attribution for Calgary's open data. Shown on every client sheet so a
# reader can trace the source and its terms.
OGL_FOOTER = (
    "Weekly Calgary Construction Permit &amp; Market Intelligence Digest. "
    "Contains information licensed under the "
    '<a href="https://data.calgary.ca/stories/s/Open-Calgary-Terms-of-Use/'
    'u45n-7awa/" target="_blank" rel="noopener">Open Government Licence '
    "&ndash; City of Calgary</a>. Public business contact details are provided "
    "for B2B project estimation and bid inquiries relevant to the "
    "recipient&rsquo;s business role."
)

COMMERCIAL_HINTS = ("commercial", "multi", "industrial", "institutional",
                    "non-residential", "office", "retail", "restaurant")


def _is_commercial_lead(x: Lead) -> bool:
    """Whether a lead belongs in the commercial / high-value section.

    Derived from the permit's own words (the Socrata `permittype` and the
    humanised scope) rather than a hand-maintained company list.
    """
    if x.permit_stage == "DEVELOPMENT_PERMIT":
        return True
    text = f"{x.permit_type} {x.description}".lower()
    return any(h in text for h in COMMERCIAL_HINTS) or (x.permit_value or 0) >= 400000


def _tel_href(phone: str) -> str:
    """tel: URI with digits only, so the number is tappable on a phone or PDF."""
    digits = re.sub(r"\D", "", phone or "")
    return f"tel:+1{digits}" if digits else ""


def _maps_href(address: str) -> str:
    query = quote_plus(f"{address}, Calgary, AB")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


# Client sheet: tiered cards (prototype proto_cards5.py). Role inboxes and tier
# rules live here so tests can exercise them directly.
CLIENT_ROLE_INBOXES = frozenset({
    "info", "contact", "admin", "office", "sales", "hello", "enquiries", "inquiries",
    "general", "reception", "mail", "service", "accounts", "support", "team",
})

# The five real pitch_for() strings. Any other angle gets no chip.
CLIENT_PITCH_CHIPS = {
    "Architect / designer of record (no GC listed) — request invite to tender / bid list": "GC not in permit record",
    "Full-build GC services — applicant has no contractor of record": "No contractor on record",
    "Pre-construction outreach — development permit at design stage": "Pre-construction",
    "Subtrade / supplier opportunity — active GC permit": "Active GC",
    "Pre-construction subtrade opportunity — development permit filed, trades not yet awarded": "Pre-construction, trades open",
}

CLIENT_SHEET_CSS = """
  :root { --ink:#14161a; --body:#333944; --muted:#687080; --faint:#98a0ad; --line:#e3e6eb; --accent:#1d4ed8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:var(--ink); font-size:11px; line-height:1.4; }
  header { padding:13px 16px 0; }
  .kicker { font-size:8.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); }
  h1 { font-size:19px; margin:1px 0 8px; letter-spacing:-.015em; }
  .stats { display:flex; gap:26px; margin-bottom:11px; }
  .stats span { font-size:9.5px; color:var(--muted); }
  .stats b { display:block; color:var(--ink); font-size:12.5px; font-weight:700; }
  .start { border:1px solid var(--line); border-radius:5px; padding:8px 11px 9px; background:#fbfcfd; }
  .start h2 { font-size:9px; letter-spacing:.13em; text-transform:uppercase; color:var(--muted); margin:0 0 5px; font-weight:600; }
  .start ol { margin:0; padding:0; list-style:none; display:flex; flex-direction:column; gap:2px; }
  .start li { display:flex; gap:10px; align-items:baseline; font-size:10.5px; }
  .s-val { width:76px; font-weight:700; }
  .s-co { flex:1; }
  .s-why { color:var(--muted); font-size:9.5px; }
  .rule { height:1px; background:#c9ced6; margin:12px 16px 0; }
  main, .rest { padding:10px 16px 12px; }
  main.grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding-top:12px; }
  .card { border:1px solid var(--line); border-radius:5px; padding:7px 9px 6px; break-inside:avoid; }
  .row1 { display:flex; align-items:center; gap:6px; }
  .num { font-size:9px; color:var(--faint); font-variant-numeric:tabular-nums; width:15px; }
  .chip { font-size:8.5px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; color:#8a4b00; background:#fdf3e3; border:1px solid #f3ddba; border-radius:3px; padding:1px 5px; white-space:nowrap; }
  .val { margin-left:auto; font-size:13px; font-weight:700; letter-spacing:-.02em; }
  .val.tbd { font-weight:400; color:var(--faint); }
  h3 { margin:3px 0 0; font-size:12px; font-weight:650; }
  h3 a { color:inherit; text-decoration:none; }
  .host { font-weight:400; font-size:9px; color:var(--faint); margin-left:5px; }
  .where { margin:1px 0 0; font-size:9.5px; color:var(--muted); }
  .where a { color:var(--muted); text-decoration:none; }
  .comm { margin-left:5px; text-transform:uppercase; letter-spacing:.06em; font-size:8.5px; color:var(--faint); }
  .scope { margin:4px 0 0; font-size:10.5px; color:var(--body); }
  .reach { margin:6px 0 0; padding-top:5px; border-top:1px solid var(--line); font-size:10.5px; line-height:1.5; }
  .who { font-weight:650; }
  .dot { color:var(--faint); margin:0 6px; }
  .lbl { font-size:8px; text-transform:uppercase; letter-spacing:.08em; color:var(--faint); margin-right:2px; }
  a.tel { color:var(--accent); text-decoration:none; font-weight:600; }
  a.mail { color:var(--accent); text-decoration:none; }
  .inbox { color:var(--muted); }
  .meta { margin:5px 0 0; font-size:8.5px; color:var(--muted); }
  .grp { margin:6px 0 14px; }
  .grp h2 { font-size:9px; letter-spacing:.13em; text-transform:uppercase; color:var(--muted); font-weight:600; margin:0 0 4px; border-bottom:1px solid var(--line); padding-bottom:3px; break-after:avoid; }
  .grp h2 span { color:var(--faint); font-weight:400; letter-spacing:0; }
  .grp ul { margin:0; padding:0; list-style:none; }
  .grp li { display:flex; gap:9px; align-items:baseline; padding:3.5px 0; border-bottom:1px solid #f1f3f6; font-size:10.5px; break-inside:avoid; }
  .rn { font-size:8.5px; color:var(--faint); width:14px; flex:none; font-variant-numeric:tabular-nums; }
  .rval { font-weight:700; width:74px; flex:none; }
  .rco { flex:1.4; font-weight:600; }
  .rco a { color:inherit; text-decoration:none; }
  .chip-s { font-size:8px; text-transform:uppercase; letter-spacing:.04em; color:#8a4b00; margin-left:6px; white-space:nowrap; }
  .rmeta { flex:2; color:var(--muted); }
  .rreach { flex:1.2; text-align:right; white-space:nowrap; }
  .none-msg { color:var(--faint); padding:12px 0; }
  footer { padding:7px 16px 11px; font-size:8.5px; color:var(--faint); border-top:1px solid var(--line); margin:0 16px; }
  footer p { margin:0 0 3px; }
  footer a { color:var(--muted); }
  @page { size:A4 landscape; margin:9mm; }
  @media print { .card, .grp li { break-inside:avoid; } .grp h2 { break-after:avoid; } }
"""


def _client_esc(text) -> str:
    return html.escape(str(text or "").strip())


def _client_is_role(email: str) -> bool:
    return bool(email) and email.split("@")[0].strip().lower() in CLIENT_ROLE_INBOXES


def client_tier(x: Lead) -> str:
    """Which part of the sheet a lead lands in: named | direct | mainline | inbox.

    named    : a person is known (contact_name set) -> full card with their name.
    direct   : no person, but an email whose local part is not a role inbox -> full card.
    mainline : no person, email empty or role inbox, but a phone -> compact row.
    inbox    : everything else (role inbox without phone, or nothing at all) -> compact row.
    """
    if (x.contact_name or "").strip():
        return "named"
    email = (x.email or "").strip()
    if email and not _client_is_role(email):
        return "direct"
    if (x.phone or "").strip():
        return "mainline"
    return "inbox"


def _client_money(value) -> str:
    v = value or 0
    return f"${v:,.0f}" if v > 0 else "TBD"


def _client_scope(x: Lead) -> str:
    s = re.sub(r"\s*\[\+\d+ more permit\(s\) this window[^\]]*\]", "", x.description or "")
    s = re.sub(r"^(Commercial / Multi Family Project|Residential Improvement Project)\s*[—-]\s*",
               "", s.strip())
    s = s.strip()
    return "" if s.lower() == "unspecified" else s


def _client_reach(x: Lead, compact: bool = False) -> str:
    """Name, phone and email as real links (role inboxes are muted, not linked)."""
    bits = []
    who = _client_esc(x.contact_name)
    if who:
        bits.append(f'<span class="who">{who}</span>')
    phone = _client_esc(x.phone)
    if phone:
        bits.append(f'<a class="tel" href="{_tel_href(phone)}">{phone}</a>')
    email = _client_esc(x.email)
    if email:
        if _client_is_role(email):
            label = "" if compact else '<span class="lbl">inbox</span>'
            bits.append(f'<span class="inbox">{label}{email}</span>')
        else:
            bits.append(f'<a class="mail" href="mailto:{email}">{email}</a>')
    return '<span class="dot">·</span>'.join(bits)


def _client_chip(x: Lead, css_class: str = "chip") -> str:
    label = CLIENT_PITCH_CHIPS.get((x.pitch_angle or "").strip(), "")
    return f'<span class="{css_class}">{label}</span>' if label else ""


def _client_extra_lines(x: Lead) -> list[str]:
    """'Also filed' lines for the other permits this company filed in the window.

    The CSV keeps one row per company, so the sheet is the only place a GC sees
    every jobsite. Used by both the full card and the compact row.
    """
    lines = []
    for extra in (x.extra_permits or []):
        e_addr = _client_esc(extra.get("address"))
        if extra.get("community"):
            e_addr += f" ({_client_esc(extra['community'])})"
        e_scope = _client_esc(extra.get("description"))
        e_val = f"${extra['permit_value']:,.0f}" if extra.get("permit_value") else "TBD"
        line = " · ".join(p for p in (e_addr, e_scope, e_val) if p)
        e_meta = " · ".join(_client_esc(str(v)) for v in
                            (extra.get("permit_number"), extra.get("applied_date")) if v)
        lines.append(f"Also filed: {line}" + (f" ({e_meta})" if e_meta else ""))
    return lines


def _client_card(x: Lead, n: int) -> str:
    display = (x.unmasked_tradename
               if NUMBERED_RE.match(x.company_name or "") and x.unmasked_tradename
               else x.company_name)
    company = _client_esc(display)
    site = (x.website or "").strip()
    host = _host(site) if site else ""
    name = (f'<a href="{html.escape(site)}" target="_blank" rel="noopener">{company}</a>'
            if site else company)
    if host:
        name += f'<span class="host">{_client_esc(host)}</span>'

    addr = _client_esc(x.address)
    comm = _client_esc(x.community)
    where = ""
    if addr:
        where = (f'<a href="{html.escape(_maps_href(x.address.strip()))}" target="_blank" rel="noopener">{addr}</a>')
    if comm:
        where += f'<span class="comm">{html.escape(comm.title())}</span>'

    scope = _client_esc(_client_scope(x))
    reach = _client_reach(x)
    money = _client_money(x.permit_value)
    val_class = "val tbd" if money == "TBD" else "val"

    parts = [
        f'    <article class="card">',
        f'      <div class="row1"><span class="num">{n:02d}</span>{_client_chip(x)}'
        f'<span class="{val_class}">{money}</span></div>',
        f"      <h3>{name}</h3>",
    ]
    if where:
        parts.append(f'      <p class="where">{where}</p>')
    if scope:
        parts.append(f'      <p class="scope">{scope}</p>')
    if reach:
        parts.append(f'      <div class="reach">{reach}</div>')
    meta = " · ".join(_client_esc(v) for v in (x.permit_number, x.applied_date) if v)
    if meta:
        parts.append(f'      <p class="meta">{meta}</p>')
    for line in _client_extra_lines(x):
        parts.append(f'      <p class="meta">{line}</p>')
    parts.append("    </article>")
    return "\n".join(parts)


def _client_row(x: Lead, n: int) -> str:
    company = _client_esc(x.company_name)
    site = (x.website or "").strip()
    name = (f'<a href="{html.escape(site)}" target="_blank" rel="noopener">{company}</a>'
            if site else company)
    scope = _client_scope(x)
    if len(scope) > 62:
        scope = scope[:59].rstrip() + "…"
    comm = _client_esc(x.community).title()
    meta = " · ".join(p for p in (comm, _client_esc(scope)) if p)
    extras = _client_extra_lines(x)
    if extras:
        meta = " · ".join(p for p in (meta, " · ".join(extras)) if p)
    return (f'      <li><span class="rn">{n:02d}</span>'
            f'<span class="rval">{_client_money(x.permit_value)}</span>'
            f'<span class="rco">{name}{_client_chip(x, "chip-s")}</span>'
            f'<span class="rmeta">{meta}</span>'
            f'<span class="rreach">{_client_reach(x, compact=True)}</span></li>')


CLIENT_GROUP_HEADINGS = {
    "mainline": "Contact by main line",
    "inbox": "General inbox only",
}


def export_client_html(leads: list[Lead], path: Path, title: str) -> None:
    """Self-contained HTML lead sheet, printed landscape A4 or pasted into an email.

    Leads are tiered (see client_tier): named and direct leads get full cards,
    the rest get compact one-line rows under their own headings. Every lead is
    rendered; empty fields are omitted rather than shown as a placeholder.
    """
    tiers: dict[str, list[Lead]] = {"named": [], "direct": [], "mainline": [], "inbox": []}
    for x in leads:
        tiers[client_tier(x)].append(x)
    # Each group is sorted on its own, highest value first. TBD (no value) sorts
    # last because it keys as 0; the sort is stable, so ties keep input order.
    for group in tiers.values():
        group.sort(key=lambda x: x.permit_value or 0, reverse=True)
    # Named and direct are concatenated, so the full-card list needs its own
    # sort: otherwise direct leads land after every named lead regardless of value.
    full = sorted(tiers["named"] + tiers["direct"], key=lambda x: x.permit_value or 0, reverse=True)
    total = sum(x.permit_value or 0 for x in leads)

    strip = ""
    if full:
        items = "".join(
            f'<li><span class="s-val">{_client_money(x.permit_value)}</span>'
            f'<span class="s-co">{_client_esc(x.company_name)}</span>'
            f'<span class="s-why">{_client_reach(x, compact=True)}</span></li>'
            for x in full[:5])
        strip = (f'  <div class="start"><h2>Top projects with contact details</h2>'
                 f'<ol>{items}</ol></div>\n')

    cards = "\n".join(_client_card(x, i + 1) for i, x in enumerate(full))
    rest_html = ""
    n = len(full)
    for key in ("mainline", "inbox"):
        group = tiers[key]
        if not group:
            continue
        rows = []
        for x in group:
            n += 1
            rows.append(_client_row(x, n))
        rest_html += (f'    <section class="grp"><h2>{CLIENT_GROUP_HEADINGS[key]} '
                      f'<span>{len(group)}</span></h2><ul>\n' + "\n".join(rows)
                      + "\n    </ul></section>\n")

    if not leads:
        cards = '    <p class="none-msg">No leads in this list.</p>'

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{CLIENT_SHEET_CSS}</style></head><body>
<header>
  <div class="kicker">Calgary permit leads</div>
  <h1>{html.escape(title)}</h1>
  <div class="stats">
    <span><b>{len(leads)} leads</b>this run</span>
    <span><b>CAD ${total:,.0f}</b>total estimated value</span>
    <span><b>{len(full)}</b>with a named contact or direct address</span>
    <span><b>{datetime.now().date().isoformat()}</b>prepared</span>
  </div>
{strip}</header>
<div class="rule"></div>
<main class="grid">
{cards}
</main>
<div class="rest">
{rest_html}</div>
<footer>
  <p>All figures in Canadian dollars. Estimated values are the applicant's declared project cost, not contract value. Sorted by estimated value, highest first.</p>
  <p>Contacts were checked against the company's own pages on {datetime.now().date().isoformat()}; an address no page could confirm is shown as unverified, not asserted.</p>
  <p>{OGL_FOOTER}</p>
</footer>
</body></html>
"""
    path.write_text(document, encoding="utf-8")


def export_review_csv(leads: list[Lead], path: Path) -> None:
    """Same readable shape as the client CSV, plus the reason it was flagged."""
    headers = [
        "Applied Date", "Permit #", "Address", "Community",
        "Est. Project Value", "Project Description",
        "Company / Applicant", "Phone", "Email", "Email Status", "Website",
        "Lead Angle", "Why Flagged",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for x in leads:
            display_name = (x.unmasked_tradename
                            if NUMBERED_RE.match(x.company_name) and x.unmasked_tradename
                            else x.company_name)
            val_str = f"${x.permit_value:,.0f}" if x.permit_value > 0 else "TBD (Dev Permit)"
            writer.writerow([
                x.applied_date, x.permit_number, x.address, x.community, val_str,
                x.description, display_name, x.phone, x.email, x.email_status,
                x.website, x.pitch_angle, review_reason(x),
            ])


def print_summary(leads: list[Lead]) -> None:
    ranked = sorted(leads, key=lambda x: x.permit_value, reverse=True)[:10]
    table = Table(title="Calgary Permit Leads — Top 10 by Permit Value", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Permit", style="cyan")
    table.add_column("Lead Type", style="magenta")
    table.add_column("Value", justify="right", style="green")
    table.add_column("Company", style="bold")
    table.add_column("Website", style="blue")
    table.add_column("Phone")
    table.add_column("Email")
    for i, lead in enumerate(ranked, 1):
        table.add_row(
            str(i), lead.permit_number, lead.lead_type,
            f"${lead.permit_value:,.0f}" if lead.permit_value else "—",
            lead.company_name, lead.website or "—", lead.phone or "—", lead.email or "—",
        )
    console.print(table)

    enriched = sum(1 for x in leads if x.phone or x.email or x.website)
    console.print(f"\n[bold]Leads:[/bold] {len(leads)} | [bold]with contact data:[/bold] {enriched} | "
                  f"[bold]emails:[/bold] {sum(1 for x in leads if x.email)} | "
                  f"[bold]phones:[/bold] {sum(1 for x in leads if x.phone)} | "
                  f"[bold]websites:[/bold] {sum(1 for x in leads if x.website)}")
    by_type: dict[str, int] = {}
    for x in leads:
        by_type[x.lead_type] = by_type.get(x.lead_type, 0) + 1
    console.print("[bold]By lead type:[/bold] " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


# ---------------------------------------------------------------------------
# Google Sheets sync (gog), stable URLs
# ---------------------------------------------------------------------------
SHEETS_STATE_PATH = config.SHEETS_STATE_PATH  # set CALGARY_LEADS_SHEETS_STATE to override


def _load_sheet_state() -> dict:
    try:
        return json.loads(SHEETS_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_sheet_state(state: dict) -> None:
    SHEETS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHEETS_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _gog(args: list[str], timeout: int = 180) -> str:
    console.print(f"[cyan]gog[/cyan] {' '.join(args)}")
    proc = subprocess.run(["gog", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gog {' '.join(args[:3])} failed ({proc.returncode}): "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _csv_values(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.reader(fh)]


def gog_sync_file(path: Path, key: str, display_name: str) -> dict:
    """Upload a plain (non-Workspace) file to Drive, keeping the link stable.

    The sibling of gog_sync for artifacts that must NOT become a Google file: an
    uploaded .html lands as text/html, so `gog drive upload --replace` is accepted
    here (it is refused for native Google types) and later runs overwrite the same
    file id instead of stacking a new copy in Drive every week.
    """
    state = _load_sheet_state()
    entry = state.get(key)
    if entry and entry.get("file_id"):
        out = _gog(["drive", "upload", str(path), "--replace", entry["file_id"], "--json"])
    else:
        out = _gog(["drive", "upload", str(path), "--name", display_name, "--json"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse gog output: {out[:200]}") from exc
    file = data.get("file", {}) if isinstance(data, dict) else {}
    fid = file.get("id") or (entry or {}).get("file_id")
    if not fid:
        raise RuntimeError(f"could not read file id from gog output: {out[:200]}")
    entry = {
        "file_id": fid,
        "kind": "file",
        "name": file.get("name") or display_name,
        "url": file.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state[key] = entry
    _save_sheet_state(state)
    return entry


def gog_sync(path: Path, key: str, title: str) -> dict:
    """Create the sheet once, then rewrite its values in place so the URL never changes.

    `gog drive upload --replace` refuses native Google files, so the update path is
    sheets clear + values update; the tab is renamed to `key` to keep ranges unambiguous.
    """
    state = _load_sheet_state()
    entry = state.get(key)
    values = _csv_values(path)

    if entry and entry.get("file_id") and entry.get("tab"):
        fid, tab = entry["file_id"], entry["tab"]
        try:
            _gog(["sheets", "clear", fid, f"{tab}!A1:ZZ100000"])
            tmp = path.parent / f".gog_{key}.json"
            tmp.write_text(json.dumps(values), encoding="utf-8")
            try:
                _gog(["sheets", "update", fid, f"{tab}!A1",
                      f"--values-json=@{tmp}", "--input", "RAW", "--json"])
            finally:
                tmp.unlink(missing_ok=True)
            back = _gog(["sheets", "get", fid, f"{tab}!A1", "--json"])
            if values and values[0] and values[0][0] and values[0][0] not in back:
                raise RuntimeError(f"readback mismatch for {key}: expected {values[0][0]!r}")
            entry = {**entry, "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            state[key] = entry
            _save_sheet_state(state)
            return entry
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]in-place update failed for {key} ({exc}); recreating[/yellow]")
            logger.warning("in-place update failed for %s: %s", key, exc)

    out = _gog(["drive", "upload", str(path), "--convert-to", "sheet",
                "--name", title, "--json"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"could not parse gog output: {out[:200]}") from exc
    file = data.get("file", {}) if isinstance(data, dict) else {}
    fid = file.get("id")
    if not fid:
        raise RuntimeError(f"could not read file id from gog output: {out[:200]}")
    tab = title
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", key).strip("_") or "leads"
    try:
        _gog(["sheets", "rename-tab", fid, tab, safe])
        tab = safe
    except Exception as exc:  # noqa: BLE001
        logger.warning("rename-tab failed for %s: %s", key, exc)
    entry = {
        "file_id": fid,
        "tab": tab,
        "title": title,
        "url": file.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{fid}",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state[key] = entry
    _save_sheet_state(state)
    return entry


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    """One row per company: keep the highest-value permit so outreach never duplicates.

    A company that filed several permits in the window would otherwise look like a
    single small job. The kept row carries the roll-up (count and combined value)
    in its description so the reader knows the row represents more than one job.
    """
    groups: dict[str, list[Lead]] = {}
    for x in leads:
        groups.setdefault(cache_key(x.company_name), []).append(x)

    out: list[Lead] = []
    for key, group in groups.items():
        lead = max(group, key=lambda x: x.permit_value or 0)
        total = sum(x.permit_value or 0 for x in group)
        extras = [x for x in group if x is not lead]
        if extras:
            extra_val = total - (lead.permit_value or 0)
            suffix = (f" [+{len(extras)} more permit(s) this window, "
                      f"${extra_val:,.0f} combined]")
            desc = (lead.description or "").strip()
            new_desc = f"{desc}{suffix}" if desc else suffix.strip()
            extra_permits = [
                {
                    "permit_number": x.permit_number,
                    "permit_type": x.permit_type,
                    "applied_date": x.applied_date,
                    "address": x.address,
                    "community": x.community,
                    "permit_value": x.permit_value,
                    "description": x.description,
                    "status": x.status,
                }
                for x in sorted(extras, key=lambda x: -(x.permit_value or 0))
            ]
            if hasattr(lead, "model_copy"):
                lead = lead.model_copy(update={"description": new_desc,
                                               "extra_permits": extra_permits})
            else:
                lead = lead.copy(update={"description": new_desc,
                                         "extra_permits": extra_permits})
        out.append(lead)
    return sorted(out, key=lambda x: -(x.permit_value or 0))


def _reachable(x: Lead) -> bool:
    """Sellable only with a phone, or an email whose domain actually accepts mail."""
    return bool(x.phone) or bool(x.email and x.email_status != "no_mx")


def filter_client_splits(leads: list[Lead], include_tract_builders: bool = False) -> tuple[list[Lead], list[Lead]]:
    """Split into (gc_leads, sub_leads) with contactability and tract-builder filtering."""
    contactable = [x for x in leads if _reachable(x)]
    gc_leads = [
        x for x in contactable
        if x.lead_type in {"NEEDS_GC", "EARLY_DESIGN"}
        and not is_volume_tract_builder(x.company_name, x.unmasked_tradename)
    ]
    sub_leads = [
        x for x in contactable
        if x.lead_type == "SUBTRADE_PITCH"
        and (include_tract_builders or not is_volume_tract_builder(x.company_name, x.unmasked_tradename))
    ]
    # SUBCONTRACTOR rows are excluded from both client lists by design.
    return gc_leads, sub_leads


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
GEMINI_CONTACTS_TOOL = BASE / "tools" / "gemini_contacts.py"
GEMINI_STAGE_TIMEOUT = 7200  # seconds; the stage is resumable, so a timeout only defers work


def run_gemini_contacts(names: list[str], *, model: str = "hydrogen", max_calls_hydrogen: int = 6,
                        max_calls_neon: int = 2, max_calls_escalate: int = 1) -> int:
    """Run tools/gemini_contacts.py for this run's companies. Never raises."""
    names = sorted({n for n in names if n})
    if not names:
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write("\n".join(names) + "\n")
        names_path = tmp.name
    cmd = [sys.executable, str(GEMINI_CONTACTS_TOOL), "--names-file", names_path, "--quiet",
           "--model", model, "--max-calls-per-day-hydrogen", str(max_calls_hydrogen),
           "--max-calls-per-day-neon", str(max_calls_neon),
           "--escalate-calls-per-day", str(max_calls_escalate)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GEMINI_STAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        console.print("[yellow]gemini contacts stage timed out; progress is saved, rerun to resume[/yellow]")
        logger.warning("gemini contacts stage timed out")
        return -1
    except OSError as exc:
        console.print(f"[yellow]gemini contacts stage could not start: {exc}[/yellow]")
        logger.error("gemini contacts stage failed to start: %s", exc)
        return -1
    finally:
        try:
            os.unlink(names_path)
        except OSError:
            pass
    summary = [ln for ln in (proc.stdout or "").splitlines() if ln.startswith("GEMINI_CONTACTS ")]
    console.print(summary[-1] if summary else f"[yellow]gemini contacts stage exit {proc.returncode}[/yellow]")
    if proc.returncode != 0:
        logger.warning("gemini contacts stage exit=%s stderr=%s", proc.returncode,
                       (proc.stderr or "")[-500:])
    logger.info("gemini contacts stage exit=%s %s", proc.returncode, summary[-1] if summary else "")
    return proc.returncode


# Task-mode enrichment (tools/gemini_contacts.py --via-task). Off unless requested by
# --gemini-contacts-task or CALGARY_VIA_TASK=1. The tool enforces its own daily Task
# quota (--max-calls-per-day-task), so this stage only passes --limit through.
VIA_TASK_ENV = "CALGARY_VIA_TASK"
VIA_TASK_LIMIT_ENV = "CALGARY_VIA_TASK_LIMIT"
VIA_TASK_DEFAULT_LIMIT = 15          # one Task call (the tool's default batch size)
COVERAGE_PATH = BASE / "logs" / "coverage-latest.json"
_ROLE_STATUSES = ("role", "role_unverified")


def via_task_requested(flag: bool, env=None) -> bool:
    env = os.environ if env is None else env
    return bool(flag) or env.get(VIA_TASK_ENV, "").strip() == "1"


def via_task_limit(env=None) -> int:
    env = os.environ if env is None else env
    raw = env.get(VIA_TASK_LIMIT_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else VIA_TASK_DEFAULT_LIMIT
    except ValueError:
        return VIA_TASK_DEFAULT_LIMIT


def run_gemini_contacts_task(names: list[str], limit: int) -> tuple[int, dict]:
    """Run gemini_contacts.py --via-task for this run's gap companies. Never raises.
    Returns (exit code, counts parsed from the GEMINI_CONTACTS line)."""
    names = sorted({n for n in names if n})
    if not names:
        return 0, {"selected": 0}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
        tmp.write("\n".join(names) + "\n")
        names_path = tmp.name
    cmd = [sys.executable, str(GEMINI_CONTACTS_TOOL), "--via-task", "--limit", str(limit),
           "--names-file", names_path, "--accept-own-domain-role", "--quiet"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GEMINI_STAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        console.print("[yellow]task enrichment timed out; progress is saved, rerun to resume[/yellow]")
        logger.warning("task enrichment stage timed out")
        return -1, {}
    except OSError as exc:
        console.print(f"[yellow]task enrichment could not start: {exc}[/yellow]")
        logger.error("task enrichment failed to start: %s", exc)
        return -1, {}
    finally:
        try:
            os.unlink(names_path)
        except OSError:
            pass
    line = next((ln for ln in reversed((proc.stdout or "").splitlines())
                 if ln.startswith("GEMINI_CONTACTS mode=task")), "")
    counts: dict = {}
    for tok in line.split()[2:]:
        k, _, v = tok.partition("=")
        counts[k] = int(v) if v.isdigit() else v
    console.print(line or f"[yellow]task enrichment exit {proc.returncode}[/yellow]")
    if proc.returncode != 0:
        logger.warning("task enrichment exit=%s stderr=%s", proc.returncode, (proc.stderr or "")[-500:])
    logger.info("task enrichment exit=%s %s", proc.returncode, line)
    return proc.returncode, counts


def run_via_task_stage(conn: sqlite3.Connection, leads: list, *, flag: bool, env=None) -> dict | None:
    """Inert (returns None, touches nothing) unless enrichment was requested."""
    if not via_task_requested(flag, env):
        return None
    limit = via_task_limit(env)
    rc, counts = run_gemini_contacts_task([x.company_name for x in leads], limit)
    changed = refresh_leads_contacts(conn, leads)
    console.print(f"Task enrichment: {changed} lead email(s) updated from cache")
    return {"rc": rc, "limit": limit, "task": counts}


def coverage_counts(leads: list) -> dict:
    """Coverage of the (deduped) lead list, for week-over-week trend lines.
    named = a person is attached; verified = a non-role email; role_only = a
    role-inbox email (info@, sales@...); blanks = no email at all."""
    emails = [x for x in leads if x.email]
    return {
        "companies": len(leads),
        "named_contacts": sum(1 for x in leads if x.contact_name),
        "verified_emails": sum(1 for x in emails if x.email_status not in _ROLE_STATUSES),
        "role_only_emails": sum(1 for x in emails if x.email_status in _ROLE_STATUSES),
        "blanks": sum(1 for x in leads if not x.email),
    }


def write_coverage(leads: list, stage: dict | None, path: Path = COVERAGE_PATH) -> dict | None:
    """Write the coverage record ONLY when the enrichment stage ran (stage is not None)."""
    if stage is None:
        return None
    rec = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
           **coverage_counts(leads), "task_rc": stage["rc"], "task_limit": stage["limit"],
           "task": stage["task"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, sort_keys=True) + "\n", encoding="utf-8")
    return rec


def refresh_leads_contacts(conn: sqlite3.Connection, leads: list[Lead]) -> int:
    """Copy verified contact fields from cache.db onto the in-memory leads."""
    changed = 0
    for x in leads:
        rec = cache_get(conn, x.company_name)
        if not rec:
            continue
        rec = sanitize_cached_record(rec)
        new_email = rec.get("email", "") or ""
        if new_email != x.email:
            x.email = new_email
            x.email_status = email_status(new_email) if new_email else ""
            changed += 1
        if rec.get("phone") and not x.phone:
            x.phone = rec["phone"]
        x.contact_name = rec.get("contact_name", "") or ""
        x.contact_source = rec.get("contact_source", "") or ""
        x.contact_verified = int(rec.get("contact_verified") or 0)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Ignore cached enrichments")
    parser.add_argument("--skip-gog", action="store_true", help="Do not upload to Google Workspace")
    parser.add_argument("--building-limit", type=int, default=300)
    parser.add_argument("--dev-limit", type=int, default=100)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--min-cost", type=int, default=50000)
    parser.add_argument("--max-cost", type=int, default=3000000)
    parser.add_argument("--no-dedupe", action="store_true", help="Keep one row per permit instead of per company")
    parser.add_argument("--tract-min-permits", type=int, default=15,
                        help="Single-family permits/year that mark a production tract builder")
    parser.add_argument("--max-age-days", type=float, default=90.0,
                        help="Re-enrich a contact-bearing cached company after this many days")
    parser.add_argument("--empty-retry-days", type=float, default=7.0,
                        help="Retry companies cached with no contact channel after this many days")
    parser.add_argument("--min-client-leads", type=int, default=5,
                        help="Refuse to upload (exit 3) if the sellable split yields fewer than this")
    parser.add_argument("--include-tract-builders", action="store_true",
                        help="Keep volume production homebuilders in calgary_subtrade_leads.csv")
    parser.add_argument("--allow-keyless", action="store_true",
                        help="Continue without SERPER_API_KEY (keyless scraping only)")
    parser.add_argument("--gemini-contacts", action="store_true",
                        help="After enrichment, ask the Gemini-Neon phone bridge for named contacts "
                             "on this run's gap companies (role inbox or no email); default off")
    parser.add_argument("--gemini-contacts-task", action="store_true",
                        help="Run gemini_contacts.py --via-task (Gemini Task, quota-capped by the tool) "
                             "after permits and before export; also enabled by CALGARY_VIA_TASK=1; "
                             "default off. Writes logs/coverage-latest.json when it runs")
    args = parser.parse_args()

    # Self-load the key so fish users never need to source a bash-style env file.
    load_env_file(Path.home() / ".config" / "serper.env")
    # gog keeps its OAuth token in a file keyring that needs GOG_KEYRING_PASSWORD.
    # cron has no shell to prompt, so load gogcli's own env file here as well.
    load_env_file(Path.home() / ".config" / "gogcli" / "exec-env.sh")
    load_env_file(BASE / ".env")

    logger.info("run start days=%s building_limit=%s dev_limit=%s min_cost=%s refresh=%s",
                args.days, args.building_limit, args.dev_limit, args.min_cost, args.refresh)

    if not os.getenv("SERPER_API_KEY", "").strip() and not args.allow_keyless:
        console.print(
            "[bold red]SERPER_API_KEY is not set.[/bold red] Put it in "
            "~/.config/serper.env as SERPER_API_KEY=..., or pass --allow-keyless "
            "to run keyless-only (much lower contact yield)."
        )
        logger.error("SERPER_API_KEY missing")
        return EXIT_CONFIG

    console.rule("[bold]1. Fetching Calgary SMB permit data (Socrata, zero key)")
    building = fetch_building_permits(
        days=args.days, limit=args.building_limit,
        min_cost=args.min_cost, max_cost=args.max_cost,
    )
    dev = fetch_dev_permits(days=args.days, limit=args.dev_limit)
    console.print(f"Building permits: [green]{len(building)}[/green] | Development permits: [green]{len(dev)}[/green]")
    permits = building + dev
    logger.info("fetched building=%d dev=%d", len(building), len(dev))
    if not permits:
        console.print("[red]No permits returned — aborting.[/red]")
        logger.error("no permits returned")
        return EXIT_NO_PERMITS

    console.rule("[bold]2. Licence checks + keyless web enrichment (SQLite cache)")
    conn = init_db()
    try:
        n_tract = load_dynamic_tract_builders(conn, min_permits=args.tract_min_permits)
        console.print(f"Volume tract builders (>= {args.tract_min_permits} single-family "
                      f"permits/yr): [green]{n_tract}[/green]")
        leads = build_leads(conn, permits, refresh=args.refresh,
                            max_age_days=args.max_age_days,
                            empty_retry_days=args.empty_retry_days)
        if args.gemini_contacts:
            console.rule("[bold]2b. Gemini-Neon named-contact backfill (gap rows only)")
            run_gemini_contacts([x.company_name for x in leads])
            changed = refresh_leads_contacts(conn, leads)
            console.print(f"Gemini contacts: {changed} lead email(s) updated from cache")
            logger.info("gemini contacts: %d lead emails refreshed", changed)
        # Read the env here (after load_env_file) so BASE/.env can set CALGARY_VIA_TASK.
        task_stage = None
        if via_task_requested(args.gemini_contacts_task):
            console.rule("[bold]2c. Gemini Task named-contact enrichment (gap rows only)")
            task_stage = run_via_task_stage(conn, leads, flag=args.gemini_contacts_task)
            logger.info("task enrichment stage %s", task_stage)
    finally:
        conn.close()

    if not args.no_dedupe:
        before = len(leads)
        leads = dedupe_leads(leads)
        console.print(f"Dedupe: {before} permit rows -> {len(leads)} unique companies")

    if task_stage is not None:
        cov = write_coverage(leads, task_stage)
        console.print(f"Coverage: {cov}")

    contactable = sum(1 for x in leads if x.phone or x.email or x.website)
    ratio = contactable / len(leads) if leads else 0.0
    console.print(f"[bold]Contact yield (any channel):[/bold] {contactable}/{len(leads)} ({ratio:.0%})")
    logger.info("deduped=%d any_channel=%d ratio=%.3f", len(leads), contactable, ratio)

    console.rule("[bold]3. Exporting master + client-ready split CSVs")
    export_csv(leads, CSV_PATH)

    gc_leads, sub_leads = filter_client_splits(leads, include_tract_builders=args.include_tract_builders)
    client_total = len(gc_leads) + len(sub_leads)
    console.print(f"[bold]Sellable leads (usable phone or email):[/bold] {client_total}")
    logger.info("sellable=%d gc=%d sub=%d", client_total, len(gc_leads), len(sub_leads))

    export_clean_client_csv(gc_leads, GC_CSV_PATH)
    export_clean_client_csv(sub_leads, SUB_CSV_PATH)
    export_client_html(gc_leads, GC_HTML_PATH, "Calgary Permit Leads - Needs Builder / GC")
    export_client_html(sub_leads, SUB_HTML_PATH, "Calgary Permit Leads - Subtrade & Supplier")

    review, policy_excluded = select_review(
        leads, gc_leads, sub_leads,
        include_tract_builders=args.include_tract_builders,
    )
    export_review_csv(review, REVIEW_CSV_PATH)
    logger.info("review=%d policy_excluded=%d", len(review), policy_excluded)

    console.print(f"Master CSV       : [green]{CSV_PATH}[/green] ({len(leads)} rows)")
    console.print(f"Needs Builder CSV: [green]{GC_CSV_PATH}[/green] ({len(gc_leads)} contactable rows)")
    console.print(f"Subtrade CSV     : [green]{SUB_CSV_PATH}[/green] ({len(sub_leads)} contactable rows)")
    console.print(f"Needs Builder HTML: [green]{GC_HTML_PATH}[/green] (paste into an email or Print-to-PDF)")
    console.print(f"Subtrade HTML    : [green]{SUB_HTML_PATH}[/green] (paste into an email or Print-to-PDF)")
    console.print(f"Review CSV       : [yellow]{REVIEW_CSV_PATH}[/yellow] ({len(review)} need manual follow-up)")
    console.print(f"Excluded by policy (trades, tract builders): {policy_excluded}")
    print_summary(leads)

    # The guard belongs on the sellable output, not on the master list.
    if client_total < args.min_client_leads:
        console.print(
            f"[bold red]Refusing to upload: only {client_total} sellable leads "
            f"(floor {args.min_client_leads}). CSVs written for inspection.[/bold red]"
        )
        logger.warning("low sellable yield: %d < %d", client_total, args.min_client_leads)
        return EXIT_LOW_YIELD

    if not args.skip_gog:
        console.rule("[bold]4. Google Workspace sync via gog")
        try:
            uploads = [(CSV_PATH, "master", "Calgary Permit Leads - Master")]
            if gc_leads:
                uploads.append((GC_CSV_PATH, "needs_builder", "Calgary Permit Leads - Needs Builder"))
            if sub_leads:
                uploads.append((SUB_CSV_PATH, "subtrade", "Calgary Permit Leads - Subtrade"))
            if review:
                uploads.append((REVIEW_CSV_PATH, "needs_review", "Calgary Permit Leads - Needs Review"))
            for path, key, title in uploads:
                entry = gog_sync(path, key, title)
                console.print(json.dumps(entry, indent=2))
                console.print(f"[bold green]Google Sheet ({key}):[/bold green] {entry.get('url', '')}")
                logger.info("sheet %s id=%s url=%s", key, entry.get("file_id"), entry.get("url"))

            # The HTML sheets go to Drive as plain files, so a human can download
            # and open/print them.
            files = []
            if gc_leads:
                files.append((GC_HTML_PATH, "needs_builder_html",
                              "Calgary Permit Leads - Needs Builder.html"))
            if sub_leads:
                files.append((SUB_HTML_PATH, "subtrade_html",
                              "Calgary Permit Leads - Subtrade.html"))
            for path, key, name in files:
                entry = gog_sync_file(path, key, name)
                console.print(f"[bold green]Google Drive file ({key}):[/bold green] {entry.get('url', '')}")
                logger.info("drive file %s id=%s url=%s", key, entry.get("file_id"), entry.get("url"))
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]gog sync failed: {exc}[/red]")
            logger.exception("gog sync failed")
            return EXIT_UPLOAD

    summary = (f"sellable={client_total} gc={len(gc_leads)} sub={len(sub_leads)} "
               f"review={len(review)} excluded={policy_excluded} master={len(leads)} "
               f"uploaded={not args.skip_gog}")
    console.print(f"SUMMARY {summary}")
    logger.info("SUMMARY %s", summary)
    logger.info("run complete: sellable=%d uploaded=%s", client_total, not args.skip_gog)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())