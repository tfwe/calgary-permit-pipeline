"""Network, search and licence layer: Socrata, licence lookup, keyless search,
Serper, and the page fetcher."""
from __future__ import annotations

import base64
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from cpp_config import *
from cpp_cache import *
from cpp_match import *

__all__ = [
    "fetch_volume_tract_builders",
    "load_dynamic_tract_builders",
    "socrata",
    "fetch_building_permits",
    "fetch_dev_permits",
    "licence_query",
    "is_active_licence",
    "licence_lookup",
    "decode_ddg_href",
    "ddg_search",
    "decode_bing_href",
    "bing_search",
    "search_web",
    "serper_places",
    "serper_organic",
    "_fetch_once",
    "fetch_page",
    "FETCH_UA_CHAIN",
    "_FETCH_RETRYABLE",
    "_FETCH_RETRY_STATUSES",
]


def fetch_volume_tract_builders(days: int = 365, min_permits: int = 15) -> dict[str, int]:
    """Names filing >= min_permits single-family permits in the last `days`.

    Permit volume is the honest signal for "production tract builder": it comes
    from the city's own records rather than a hand-maintained name list, so a new
    high-volume builder (or a numbered limited partnership) is caught on its first
    busy year without editing a regex. Fails open to {} when Socrata is
    unreachable, leaving the static list in charge.
    """
    counts: dict[str, int] = {}
    cutoff = days_ago_iso(days)
    for field in ("contractorname", "applicantname"):
        try:
            rows = socrata(BUILDING_URL, {
                "$select": f"upper({field}) as name, count(*) as cnt",
                "$where": (f"permittype = 'Single Construction Permit' "
                           f"AND applieddate >= '{cutoff}' AND {field} IS NOT NULL"),
                "$group": f"upper({field})",
                "$limit": 5000,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("tract-builder frequency probe failed (%s); static list only", exc)
            return {}
        for r in rows:
            key = cache_key(clean(r.get("name")))
            try:
                cnt = int(float(r.get("cnt") or 0))
            except (TypeError, ValueError):
                continue
            if key and cnt >= min_permits:
                counts[key] = max(counts.get(key, 0), cnt)
    return counts


def load_dynamic_tract_builders(conn: sqlite3.Connection, days: int = 365,
                                min_permits: int = 15) -> int:
    """Refresh the volume-builder set into cache.db and activate it.

    Persisted rather than only held in memory so tools/audit_outputs.py can apply
    the same rule offline, long after the run that fetched it.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS tract_builders ("
                 "name TEXT PRIMARY KEY, permits INTEGER NOT NULL, fetched_at TEXT NOT NULL)")
    counts = fetch_volume_tract_builders(days=days, min_permits=min_permits)
    if counts:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.executemany(
            "INSERT INTO tract_builders(name, permits, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET permits=excluded.permits, "
            "fetched_at=excluded.fetched_at",
            [(n, c, now) for n, c in counts.items()],
        )
        conn.commit()
    _DYNAMIC_TRACT_BUILDERS.clear()
    _DYNAMIC_TRACT_BUILDERS.update(
        row[0] for row in
        conn.execute("SELECT name FROM tract_builders WHERE permits >= ?", (min_permits,))
    )
    return len(_DYNAMIC_TRACT_BUILDERS)


# ---------------------------------------------------------------------------
# Socrata ingestion
# ---------------------------------------------------------------------------
def socrata(url: str, params: dict, retries: int = 3) -> list:
    """GET a Socrata dataset with retries and return a list of rows."""
    last_err = None
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=40, follow_redirects=True, headers=HEADERS) as client:
                r = client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    return data
                # Some Socrata error payloads are dicts
                last_err = RuntimeError(f"Unexpected payload: {str(data)[:300]}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Socrata request failed for {url}: {last_err}")


def fetch_building_permits(days: int = 14, limit: int = 300,
                           min_cost: int = 50000, max_cost: int = 3000000) -> list[dict]:
    where = (
        f"estprojectcost >= {min_cost} AND estprojectcost <= {max_cost} "
        f"AND applieddate >= '{days_ago_iso(days)}' "
        "AND (applicantname IS NOT NULL OR contractorname IS NOT NULL)"
    )
    rows = socrata(BUILDING_URL, {"$where": where, "$order": "applieddate DESC", "$limit": limit})
    out = []
    for r in rows:
        applicant = clean(r.get("applicantname"))
        contractor = clean(r.get("contractorname"))
        if not applicant and not contractor:
            continue
        # NEEDS_GC only when the applicant is not itself a builder/GC.
        if applicant and not contractor and not looks_like_contractor(applicant):
            lead_type = "NEEDS_GC"
            target = applicant
        else:
            lead_type = "SUBTRADE_PITCH"
            target = contractor or applicant
        try:
            value = float(r.get("estprojectcost") or 0)
        except (TypeError, ValueError):
            value = 0.0
        out.append({
            "permit_number": clean(r.get("permitnum")),
            # permittype first: permittypemapped is always the generic "Building".
            "permit_type": clean(r.get("permittype") or r.get("permittypemapped")
                                 or "Building Permit"),
            "permit_stage": "BUILDING_PERMIT",
            "lead_type": lead_type,
            "status": clean(r.get("statuscurrent")),
            "permit_value": value,
            "target_company": target,
            "applicant_name": applicant,
            "contractor_name": contractor,
            "address": clean(r.get("originaladdress") or r.get("locationaddresses")),
            "community": clean(r.get("communityname")),
            "applied_date": clean(r.get("applieddate"))[:10],
            "issued_date": clean(r.get("issueddate"))[:10],
            "description": format_building_scope(r),
        })
    return out


def fetch_dev_permits(days: int = 14, limit: int = 100) -> list[dict]:
    cats = ",".join(f"'{c}'" for c in DEV_CATEGORIES)
    where = (
        f"applieddate >= '{days_ago_iso(days)}' AND applicant IS NOT NULL "
        f"AND category in ({cats})"
    )
    rows = socrata(DEV_URL, {"$where": where, "$order": "applieddate DESC", "$limit": limit})
    out = []
    for r in rows:
        applicant = clean(r.get("applicant"))
        if not applicant:
            continue
        lead_type = "SUBTRADE_PITCH" if looks_like_contractor(applicant) else "EARLY_DESIGN"
        out.append({
            "permit_number": clean(r.get("permitnum")),
            "permit_type": "Development Permit",
            "permit_stage": "DEVELOPMENT_PERMIT",
            "lead_type": lead_type,
            "status": clean(r.get("statuscurrent")),
            "permit_value": 0.0,
            "target_company": applicant,
            "applicant_name": applicant,
            "contractor_name": "",
            "address": clean(r.get("address")),
            "community": clean(r.get("communityname")),
            "applied_date": clean(r.get("applieddate"))[:10],
            "issued_date": "",
            "description": clean(f"{r.get('category','')}: {r.get('proposedusedescription','')}"),
        })
    return out


def licence_query(params: dict) -> list[dict]:
    try:
        return socrata(LICENCE_URL, params)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]licence query failed: {exc}[/yellow]")
        return []


def is_active_licence(row: dict) -> bool:
    status = clean(row.get("jobstatusdesc")).lower()
    if status == "licensed":
        return True
    exp = clean(row.get("exp_dt"))
    if exp:
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            return exp_dt >= datetime.now(timezone.utc) - timedelta(days=1)
        except ValueError:
            pass
    return False


def licence_lookup(company: str, address: str) -> dict:
    """Return {unmasked_tradename, licence_type, active, matched_by} or empty dict."""
    company_u = clean(company).upper().replace("'", "''")
    if not company_u:
        return {}

    numbered = bool(NUMBERED_RE.match(company))
    rows: list[dict] = []
    matched_by = ""

    # 1) Exact-ish trade name match (named companies only).
    if not numbered:
        stripped = company_u
        while True:
            for suffix in LEGAL_SUFFIXES:
                if stripped.endswith(suffix):
                    stripped = stripped[:-len(suffix)].rstrip()
                    break
            else:
                break
        if stripped and stripped != company_u:
            rows = licence_query({"$where": f"upper(tradename) LIKE '%{stripped}%'", "$limit": 5})
            if not rows:
                console.print(f"[dim]licence: no match on stripped name, retried full name: {company}[/dim]")
        if not rows:
            rows = licence_query({"$where": f"upper(tradename) LIKE '%{company_u}%'", "$limit": 5})
        matched_by = "tradename"

        # 2) Retry with the first two meaningful, non-generic words.
        if not rows:
            words = [w for w in re.split(r"[^A-Z0-9]+", company_u) if len(w) > 2]
            if len(words) >= 2 and not all(w in GENERIC_TOKENS for w in words[:2]):
                short = " ".join(words[:2]).replace("'", "''")
                rows = licence_query({"$where": f"upper(tradename) LIKE '%{short}%'", "$limit": 5})
                matched_by = "tradename_prefix"

    # 3) Address fallback ONLY for numbered shells; it poisons named companies with old tenants.
    if numbered and address:
        addr = clean(address).upper().replace("'", "''")
        addr = re.sub(r"^#?\s*[A-Z0-9\-]+\s+", "", addr, count=1) if addr.startswith("#") else addr
        m = re.search(r"(\d{2,6})\s+([A-Z0-9]{2,})", addr)
        if m:
            frag = f"{m.group(1)} {m.group(2)}".replace("'", "''")
            rows = licence_query({"$where": f"upper(address) LIKE '%{frag}%'", "$limit": 5})
            matched_by = "address"

    if not rows:
        return {}

    active_rows = [r for r in rows if is_active_licence(r)] or rows
    best = active_rows[0]
    tradename = clean(best.get("tradename"))
    if tradename.upper() in {"APARTMENT BUILDING", "COMMERCIAL BUILDING"}:
        return {}
    return {
        "unmasked_tradename": tradename,
        "licence_type": clean(best.get("licencetypes")),
        "active": is_active_licence(best),
        "matched_by": matched_by,
        "licence_address": clean(best.get("address")),
    }


def decode_ddg_href(href: str) -> str | None:
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return qs["uddg"][0]
        return None
    return href


# deprecated: keyless engines are blocked from datacenter IPs; kept only as a last resort.
def ddg_search(query: str, max_results: int = 6) -> list[tuple[str, str]]:
    """Keyless DuckDuckGo HTML search; returns (url, title) pairs."""
    hits: list[tuple[str, str]] = []
    try:
        with httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS) as client:
            r = client.post("https://html.duckduckgo.com/html/", data={"q": query})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.result__a"):
                u = decode_ddg_href(a.get("href", ""))
                title = a.get_text(" ", strip=True)
                if u and not is_junk(u) and all(u != h[0] for h in hits):
                    hits.append((u, title))
                if len(hits) >= max_results:
                    break
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]DDG search failed ({query[:40]}...): {exc}[/yellow]")
    return hits


def decode_bing_href(href: str) -> str | None:
    """Bing wraps organic links in /ck/a?...&u=a1<base64url>."""
    if not href:
        return None
    if "bing.com/ck/a" in href:
        qs = parse_qs(urlparse(href).query)
        u = qs.get("u", [""])[0]
        if u.startswith("a1"):
            b = u[2:]
            pad = "=" * (-len(b) % 4)
            try:
                return base64.urlsafe_b64decode(b + pad).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                return None
        return None
    return href


# deprecated: keyless engines are blocked from datacenter IPs; kept only as a last resort.
def bing_search(query: str, max_results: int = 6) -> list[tuple[str, str]]:
    """Keyless Bing HTML search; returns (url, title) pairs."""
    hits: list[tuple[str, str]] = []
    try:
        with httpx.Client(timeout=25, follow_redirects=True, headers=HEADERS) as client:
            r = client.get("https://www.bing.com/search",
                           params={"q": query, "setlang": "en-CA", "cc": "CA"})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("li.b_algo h2 a"):
                u = decode_bing_href(a.get("href", ""))
                title = a.get_text(" ", strip=True)
                if u and not is_junk(u) and all(u != h[0] for h in hits):
                    hits.append((u, title))
                if len(hits) >= max_results:
                    break
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Bing search failed ({query[:40]}...): {exc}[/yellow]")
    return hits


def search_web(query: str, company: str) -> list[str]:
    """Serper organic is primary; keyless Bing then DuckDuckGo are last-resort fallbacks (no SERPER_API_KEY only); title+domain validated."""
    candidates = serper_organic(query)
    if not candidates and not os.getenv("SERPER_API_KEY", "").strip():
        candidates = bing_search(query)
        if not candidates:
            candidates = ddg_search(query)
    valid = []
    for u, title in candidates:
        if not _name_match(title, u, company):
            console.print(f"[dim]reject search result (name mismatch): {u[:80]}[/dim]")
            continue
        valid.append(u)
    return valid


def serper_places(company: str) -> dict:
    """Google Places via Serper.dev. Returns phone/website/rating/title/address."""
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        console.print("[yellow]SERPER_API_KEY not set — skipping Serper Places[/yellow]")
        return {}
    try:
        with httpx.Client(timeout=15, headers={"X-API-KEY": key, "Content-Type": "application/json"}) as client:
            r = client.post("https://google.serper.dev/places",
                            json={"q": f"{company} Calgary AB", "gl": "ca"})
        if r.status_code != 200:
            console.print(f"[yellow]Serper HTTP {r.status_code}: {r.text[:120]}[/yellow]")
            return {}
        places = r.json().get("places", [])
        for p in places[:3]:
            if not _place_is_local(p):
                continue
            title = clean(p.get("title"))
            website = strip_url_tracking(clean(p.get("website")))
            if (place_matches_company(title, website, company)
                    or _title_matches_company(title, company)):
                return {
                    "phone": clean(p.get("phoneNumber")),
                    "website": website,
                    "rating": p.get("rating"),
                    "title": title,
                    "address": clean(p.get("address")),
                }
        return {}
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Serper lookup failed ({company[:40]}): {exc}[/yellow]")
        return {}


def serper_organic(query: str, num: int = 10) -> list[tuple[str, str]]:
    """Google organic results via Serper.dev (1 credit). Returns (url, title) pairs."""
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        console.print("[yellow]SERPER_API_KEY not set - skipping Serper Organic[/yellow]")
        return []
    try:
        with httpx.Client(timeout=15, headers={"X-API-KEY": key, "Content-Type": "application/json"}) as client:
            r = client.post("https://google.serper.dev/search",
                            json={"q": query, "gl": "ca", "num": num})
        if r.status_code != 200:
            console.print(f"[yellow]Serper HTTP {r.status_code}: {r.text[:120]}[/yellow]")
            return []
        out: list[tuple[str, str]] = []
        for item in r.json().get("organic", []):
            url = clean(item.get("link"))
            if not url:
                continue
            out.append((url, clean(item.get("title"))))
        return out
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Serper organic failed ({query[:40]}): {exc}[/yellow]")
        return []


# Some builder sites reset or drop the connection when they see the Chrome UA from
# httpx but answer the same request fine for a plain client UA (measured: rsarchitecture.ca,
# bravehomes.ca, amandahamiltondesign.com). The chain is tried in order, in-process.
# None = the normal HEADERS. Only transport-level failures advance the chain; an HTTP
# status (including 404) is an answer and raises immediately.
FETCH_UA_CHAIN: tuple[str | None, ...] = (None, "curl/8.5.0", "python-httpx/0.28")
_FETCH_RETRYABLE = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
                    httpx.ReadError, httpx.ConnectTimeout, httpx.WriteError)
# 403/429 mean "we do not like this client", not "this page is gone": a different UA often
# gets the same page. A 404 is the page genuinely not existing, so it never retries.
_FETCH_RETRY_STATUSES = (403, 429)


def _fetch_once(url: str, timeout: float, user_agent: str | None) -> tuple[str, str]:
    headers = HEADERS if user_agent is None else {**HEADERS, "User-Agent": user_agent}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"non-HTML content-type: {ctype}")
        return r.text, str(r.url)


def fetch_page(url: str, timeout: int = 8) -> tuple[str, str]:
    last: Exception | None = None
    for user_agent in FETCH_UA_CHAIN:
        try:
            return _fetch_once(url, timeout, user_agent)
        except _FETCH_RETRYABLE as exc:
            last = exc
            logger.info("fetch %s failed with UA %s: %s; trying next UA",
                        url, user_agent or "chrome", type(exc).__name__)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _FETCH_RETRY_STATUSES:
                raise
            last = exc
            logger.info("fetch %s got HTTP %s with UA %s; trying next UA",
                        url, status, user_agent or "chrome")
    raise last  # type: ignore[misc]
