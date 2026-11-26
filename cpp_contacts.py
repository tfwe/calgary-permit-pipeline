"""Pure contacts utilities: phone/email normalisation, MX check, contact extraction,
and cached-record sanitising (extracted from pipeline.py).

Patch-sensitive callers (email_status, scrape_contacts, enrich_company) stay in
pipeline.py; this module only holds leaf helpers they resolve via the star-import.
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from cpp_config import *  # noqa: F401,F403
from cpp_cache import *  # noqa: F401,F403
from cpp_match import *  # noqa: F401,F403

__all__ = [
    "FREE_EMAIL_DOMAINS",
    "normalize_phone",
    "_mx_cache",
    "domain_has_mx",
    "valid_email",
    "email_priority",
    "extract_contacts",
    "_has_calgary_signal",
    "sanitize_cached_record",
]


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[:3] not in CA_PHONE_AREA_CODES:
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

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
