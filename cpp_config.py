"""Shared constants, the Lead model, and small pure helpers for the Calgary pipeline.

Split out of pipeline.py. Names are re-exported there via
`from cpp_config import *`, so `pipeline.<name>` keeps working.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field
from rich.console import Console

__all__ = [
    "BASE",
    "DB_PATH",
    "CSV_PATH",
    "GC_CSV_PATH",
    "SUB_CSV_PATH",
    "REVIEW_CSV_PATH",
    "GC_HTML_PATH",
    "SUB_HTML_PATH",
    "EXIT_OK",
    "EXIT_NO_PERMITS",
    "EXIT_CONFIG",
    "EXIT_LOW_YIELD",
    "EXIT_UPLOAD",
    "UA",
    "HEADERS",
    "BUILDING_URL",
    "DEV_URL",
    "LICENCE_URL",
    "JUNK_DOMAINS",
    "VOLUME_TRACT_BUILDERS_RE",
    "EMAIL_RE",
    "CA_PHONE_AREA_CODES",
    "PHONE_RE",
    "NUMBERED_RE",
    "CONTRACTOR_NAME_RE",
    "DESIGN_FIRM_RE",
    "SUBCONTRACTOR_RE",
    "DEV_CATEGORIES",
    "console",
    "Lead",
    "clean",
    "_DYNAMIC_TRACT_BUILDERS",
    "strip_url_tracking",
    "load_env_file",
    "days_ago_iso",
    "SCOPE_SHORTHAND",
    "EMPTY_SCOPE_VALUES",
    "_humanize_scope_tokens",
    "_strip_code_prefix",
    "format_building_scope",
    "LOG_DIR",
    "setup_logging",
    "logger",
    "_ROLE_LOCALS",
]


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "cache.db"
CSV_PATH = BASE / "calgary_leads_demo.csv"
GC_CSV_PATH = BASE / "calgary_needs_builder.csv"
SUB_CSV_PATH = BASE / "calgary_subtrade_leads.csv"
REVIEW_CSV_PATH = BASE / "calgary_needs_review.csv"
GC_HTML_PATH = BASE / "calgary_needs_builder.html"
SUB_HTML_PATH = BASE / "calgary_subtrade_leads.html"

# Distinct exits so an unattended runner can tell config rot from a Google outage.
EXIT_OK = 0
EXIT_NO_PERMITS = 1
EXIT_CONFIG = 2
EXIT_LOW_YIELD = 3
EXIT_UPLOAD = 4


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Constants: endpoints, blocklists, patterns
# ---------------------------------------------------------------------------
BUILDING_URL = "https://data.calgary.ca/resource/c2es-76ed.json"
DEV_URL = "https://data.calgary.ca/resource/6933-unw5.json"
LICENCE_URL = "https://data.calgary.ca/resource/vdjc-pybd.json"


JUNK_DOMAINS = {
    "yelp.ca", "yelp.com", "yellowpages.ca", "yellowpages.com", "facebook.com",
    "linkedin.com", "mapquest.com", "buildzoom.com", "bbb.org", "instagram.com",
    "twitter.com", "x.com", "tiktok.com", "google.com", "duckduckgo.com",
    "kijiji.ca", "indeed.com", "glassdoor.ca", "pinterest.com", "youtube.com",
    "wikipedia.org", "canada.ca", "calgary.ca", "cityofcalgary.com",
    "homeadvisor.com", "houzz.com", "angi.com", "thumbtack.com", "manta.com",
    "zoominfo.com", "dnb.com", "opencorporates.com", "alberta.ca",
    "cra-arc.gc.ca", "chamberofcommerce.com", "canadianbusinessdirectory.ca",
    "cylex.ca", "provenexpert.com", "bark.com", "findglocal.com",
    "albertacorporations.com", "bizapedia.com", "canadacompanyregistry.com",
    "companydatabase.org", "opengovca.com", "govcb.com",
    "canadiancompanyregistry.com", "companies.ca", "bizapedia.ca",
    "foursquare.com", "tripadvisor.ca", "tripadvisor.com",
    "yellowpages.cylex.ca", "fandom.com", "microsoft.com", "microsoft",
    "cloud.microsoft", "office.com", "live.com", "apple.com", "amazon.com",
    "msn.com", "reddit.com", "medium.com", "wordpress.com", "adobe.com",
    "portfolio.adobe.com", "behance.net", "linktr.ee", "wixsite.com",
    "squarespace.com", "godaddy.com", "formed.org", "base.org",
}

# High-volume production/tract home builders use locked-in annual subtrade tenders
# and do not buy from cold subtrade outreach. Exclude from client-facing subtrade CSVs
# unless --include-tract-builders is passed.
VOLUME_TRACT_BUILDERS_RE = re.compile(
    r"\b(MATTAMY|BROOKFIELD\s+RESIDENTIAL|JAYMAN|MORRISON\s+HOMES|SHANE\s+HOMES|"
    r"STERLING\s+HOMES|TRICO\s+HOMES|EXCEL\s+HOMES|HOMES\s+BY\s+(?:AVI|US|DREAM)|"
    r"CARDEL\s+HOMES|CEDARGLEN\s+(?:HOMES|GROUP|LIVING)|HOPEWELL|"
    r"BROADVIEW\s+HOMES|PACESETTER\s+HOMES|GENESIS\s+BUILDERS|TRUMAN\s+HOMES|"
    r"ROHIT\s+COMMUNITIES|STREETSIDE\s+DEVELOPMENTS|LOGEL\s+HOMES|"
    r"ANTHEM\s+(?:PROPERTIES|CORNERSTONE|CD\s+HOMES)|QUALICO|BAYWEST\s+HOMES|"
    r"CALBRIDGE\s+HOMES|STEPPER\s+HOMES|NU\s*VISTA|CANTIRO|AVALON\s+MASTER\s+BUILDER|"
    r"BEDROCK\s+HOMES|DAYTONA\s+HOMES|PROMINENT\s+HOMES|MCKEE\s+HOMES|"
    r"AKASH\s+HOMES|STEALTH\s+HOMES|CRYSTAL\s+CREEK\s+HOMES|WEST\s+CREEK\s+HOMES|"
    r"DOUGLAS\s+HOMES|WILLIAM\s+BLAKE\s+HOMES)\b",
    re.I,
)

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,24}\b")
# Calgary / southern Alberta area codes only: a 780 (Edmonton) number on a Calgary
# permit almost always means enrichment matched the wrong business.
CA_PHONE_AREA_CODES = ("403", "587", "825", "368")
PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?(403|587|825|368)\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
NUMBERED_RE = re.compile(r"^\s*\d{6,8}\s+ALBERTA\b", re.I)
CONTRACTOR_NAME_RE = re.compile(
    r"\b(CONSTRUCTION|CONTRACTING|CONTRACTOR|CONTRACTORS|BUILDERS|BUILDER|"
    r"BUILDING|HOMES|HOMEBUILDER|DEVELOPMENTS|DEVELOPERS|RENOVATIONS|"
    r"RESTORATION|EXCAVATING|MECHANICAL|ELECTRICAL|ROOFING|MASONRY|"
    r"PATIOS|DECKS|EXTERIORS|INTERIORS|MILLWORK|DRYWALL|FRAMING|PLUMBING|HVAC)\b",
    re.I,
)

# Architect / design firms applying for a permit with no contractor of record are
# not GC prospects — they are the design professional who runs the tender. Pitch
# them an invite to the bid list, not full-build GC services.
DESIGN_FIRM_RE = re.compile(
    r"\b(ARCHITECTS?|ARCHITECTURE|DESIGNERS?|DESIGN(?:S)?|PLANNERS?|DRAFTING)\b",
    re.I,
)

# Pure trades are buyers' suppliers, not buyers of subtrade services.
SUBCONTRACTOR_RE = re.compile(
    r"\b(ELECTRIC|ELECTRICAL|PLUMBING|HVAC|HEATING|SHEET\s*METAL|ROOFING|"
    r"DRYWALL|FRAMING|MASONRY|CONCRETE|LANDSCAP|PAINTING|FLOORING|INSULATION|"
    r"SIDING|WINDOWS|GLAZING|EXCAVAT|WELDING|SURVEY)\b",
    re.I,
)

DEV_CATEGORIES = [
    "Commercial - Other Areas",
    "Residential - Multi-Family",
    "Mixed Use - Downtown",
    "Renovations - Non-Residential",
    "Change of Use - Permitted Use",
    "Change of Use - Discretionary Use or Relaxations Required",
]

console = Console()


class Lead(BaseModel):
    permit_number: str
    permit_type: str
    permit_stage: str = ""
    lead_type: str
    status: str = ""
    permit_value: float = 0.0
    company_name: str
    # Named person from a verified Gemini-Neon contact lookup (tools/gemini_contacts.py).
    # Empty unless the accepted email was found on a page we fetched ourselves.
    contact_name: str = ""
    contact_source: str = ""
    contact_verified: int = 0
    unmasked_tradename: str = ""
    licence_type: str = ""
    applicant_name: str = ""
    contractor_name: str = ""
    address: str = ""
    community: str = ""
    applied_date: str = ""
    issued_date: str = ""
    description: str = ""
    website: str = ""
    phone: str = ""
    email: str = ""
    email_status: str = ""
    google_rating: float | None = None
    pitch_angle: str = ""
    # Other permits the same company filed in this window. Deliberately excluded
    # from model_dump() so the CSV keeps its 1-row-per-company invariant; only
    # the HTML sheet expands them into sub-rows.
    extra_permits: list[dict] = Field(default_factory=list, exclude=True)
    # Carried for the review pile's reason text; deliberately kept out of model_dump().
    kill_reason: str = Field(default="", exclude=True)


def clean(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


_DYNAMIC_TRACT_BUILDERS: set[str] = set()


def strip_url_tracking(url: str) -> str:
    """Remove noisy UTM/GBP query parameters from enriched URLs."""
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
        if not p.scheme or not p.netloc:
            return url.strip()
        if not p.query:
            return url.strip()
        qs = parse_qs(p.query, keep_blank_values=True)
        clean_qs = {
            k: v for k, v in qs.items()
            if not k.lower().startswith(("utm_", "gbp", "fbclid", "gclid"))
            and k not in {"1"}
        }
        if not clean_qs:
            return f"{p.scheme}://{p.netloc}{p.path}"
        from urllib.parse import urlencode
        return f"{p.scheme}://{p.netloc}{p.path}?{urlencode(clean_qs, doseq=True)}"
    except Exception:  # noqa: BLE001
        return url.strip()


def load_env_file(path: Path) -> None:
    """Parse KEY=VALUE lines into os.environ (never overwrite existing vars)."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def days_ago_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT00:00:00.000")


SCOPE_SHORTHAND = {
    "SFD": "Single-Family Detached",
    "SEMI-D": "Semi-Detached",
    "BSMT-DEV": "Basement Development",
    "SSUITE": "Secondary Suite",
    "GARAGE": "Garage",
    "FIREPLACE": "Fireplace",
    "PORCH": "Porch",
    "DECK": "Deck",
}

# Values that carry no information for a contractor.
EMPTY_SCOPE_VALUES = {"", "unspecified", "online", "none", "n/a"}


def _humanize_scope_tokens(description: str) -> str:
    """Turn `SFD,BSMT-DEV,PORCH,` into readable labels, dropping empty values."""
    parts = [p.strip() for p in clean(description).split(",") if p.strip()]
    labels = [SCOPE_SHORTHAND.get(p.upper(), p) for p in parts
              if p.lower() not in EMPTY_SCOPE_VALUES]
    return ", ".join(dict.fromkeys(labels))          # de-duplicate, keep order


def _strip_code_prefix(value: str) -> str:
    """`1106 - Single Family House` -> `Single Family House`; `9999  Online` -> `Online`."""
    return re.sub(r"^\d+\s*[-\u2013]?\s*", "", clean(value)).strip()


def format_building_scope(r: dict) -> str:
    """A one-line, jargon-free scope built from the Socrata scope columns.

    Calgary's `permittypemapped` is always the generic "Building", while
    `permittype` carries the real distinction ("Commercial / Multi Family
    Project"), and description/permitclass/workclass carry the detail.
    """
    headline = clean(r.get("permittype") or r.get("permittypemapped")) or "Building Permit"
    bits = []
    for value in (clean(r.get("workclass") or r.get("workclassgroup")),
                  _strip_code_prefix(r.get("permitclass"))):
        if value.lower() not in EMPTY_SCOPE_VALUES and value.lower() != headline.lower():
            bits.append(value)
    scope = headline
    if bits:
        scope += " \u2014 " + ", ".join(dict.fromkeys(bits))
    detail = _humanize_scope_tokens(r.get("description"))
    if detail:
        scope += f" \u00b7 {detail}"
    try:
        sqft = float(r.get("totalsqft") or 0)
    except (TypeError, ValueError):
        sqft = 0.0
    if sqft > 0:
        scope += f" \u00b7 {int(sqft):,} sq ft"
    try:
        units = int(float(r.get("housingunits") or 0))
    except (TypeError, ValueError):
        units = 0
    if units > 1:
        scope += f" \u00b7 {units} units"
    return scope


LOG_DIR = BASE / "logs"


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("calgary_leads")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    # The file handler is lazy: importing cpp_config (directly or through any
    # cpp_* module) must have no filesystem side effect, so logs/ is created
    # and opened only when the first record is actually logged.
    logger.addHandler(_LazyFileHandler())
    return logger


class _LazyFileHandler(logging.Handler):
    """Defer LOG_DIR creation and the log file open until the first record."""

    def __init__(self) -> None:
        super().__init__()
        self._real: logging.Handler | None = None

    def _ensure(self) -> logging.Handler:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / "pipeline.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        return handler

    def emit(self, record: logging.LogRecord) -> None:
        if self._real is None:
            self._real = self._ensure()
        self._real.handle(record)


logger = setup_logging()

# Canonical role-inbox stems, shared by every consumer so the three sites that
# grade role inboxes (pipeline.email_status, the client-sheet tiers in
# cpp_export, and tools/gemini_contacts.py) cannot drift apart again. Stems
# cover exact matches (info@) and suffixed forms (info., info-, info_).
_ROLE_LOCALS = frozenset({"info", "sales", "contact", "admin", "office", "hello", "team",
                          "support", "reception", "enquiries", "inquiries", "accounts",
                          "marketing", "billing", "general", "mail", "service"})
