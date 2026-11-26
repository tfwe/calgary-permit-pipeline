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
from cpp_contacts import *
from cpp_export import *

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


# ---------------------------------------------------------------------------
# Logging, cache freshness, email grading
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Contact extraction from a company's own site
# ---------------------------------------------------------------------------


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
# Google Sheets sync (gog), stable URLs
# ---------------------------------------------------------------------------
def _gog(args: list[str], timeout: int = 180) -> str:
    console.print(f"[cyan]gog[/cyan] {' '.join(args)}")
    proc = subprocess.run(["gog", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gog {' '.join(args[:3])} failed ({proc.returncode}): "
                           f"{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


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