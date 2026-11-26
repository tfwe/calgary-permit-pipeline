"""Matching and classification heuristics (extracted from pipeline.py).

Pure functions over company names, URLs and page titles: junk filtering,
contractor/tract-builder classification, and strict name-to-domain matching.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from cpp_config import *  # noqa: F401,F403
from cpp_cache import *  # noqa: F401,F403


__all__ = [
    "looks_like_contractor",
    "is_volume_tract_builder",
    "LEGAL_SUFFIXES",
    "is_junk",
    "GENERIC_TOKENS",
    "NON_CONTEXT_WORDS",
    "company_tokens",
    "_norm_text",
    "_compact_text",
    "_host",
    "_domain_label",
    "_tokens",
    "_url_path_depth",
    "_suffix_corroborates",
    "_label_matches_token",
    "_name_match",
    "result_matches_company",
    "_title_matches_company",
    "place_matches_company",
    "PLACE_WORDS",
    "_company_initials",
    "_domain_related",
    "NON_CALGARY_CITIES",
    "_place_is_local",
]


def looks_like_contractor(name: str, licence_type: str = "") -> bool:
    """True when the name or licence says they already build/contract (do not pitch GC to them)."""
    if "CONTRACTOR" in (licence_type or "").upper():
        return True
    return bool(CONTRACTOR_NAME_RE.search(name or ""))


def is_volume_tract_builder(name: str, tradename: str = "") -> bool:
    """True when the company is a large volume/tract residential builder.

    Two sources: the static fallback (offline, and what the unit tests exercise)
    and the data-driven set from permit frequency, once loaded.
    """
    if VOLUME_TRACT_BUILDERS_RE.search(f"{name or ''} {tradename or ''}"):
        return True
    return any(cache_key(c) in _DYNAMIC_TRACT_BUILDERS for c in (name, tradename) if c)


LEGAL_SUFFIXES = (" LTD", " LTD.", " LIMITED", " INC", " INC.", " INCORPORATED",
                  " CORP", " CORP.", " CORPORATION", " LP", " L.P.", " LLP", " ULC",
                  " CO", " HOLDINGS")


def is_junk(url: str) -> bool:
    if not url:
        return True
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        path = parsed.path.lower()
    except ValueError:
        return True
    if not host:
        return True
    if any(host == d or host.endswith("." + d) for d in JUNK_DOMAINS):
        return True
    if any(seg in path for seg in ("/missing", "/contractor/", "/login",
                                   "/stores/", "/store-locator", "/locations/",
                                   "/dealers/")):
        return True
    return False


# ---------------------------------------------------------------------------
# Company-name to domain matching (false-positive control)
# ---------------------------------------------------------------------------
GENERIC_TOKENS = {
    "CONSTRUCTION", "HOMES", "HOME", "MANAGEMENT", "DEVELOPMENT", "DEVELOPERS",
    "DESIGN", "ARCHITECTURE", "CALGARY", "ALBERTA", "LTD", "LIMITED", "INC",
    "CORP", "CORPORATION", "THE", "AND", "BUILDERS", "BUILDING", "CONTRACTING",
    "CONTRACTORS", "PROPERTIES", "GROUP", "SERVICES", "SYSTEMS", "MARKET",
    "STUDIO", "HOUSE", "RENOVATIONS", "ENTERPRISES", "INDUSTRIES",
    "PARTNERSHIP", "HOLDINGS", "VENTURES", "LAB", "LABS", "INTERIORS",
    "EXTERIORS", "ARCHITECTS", "ENGINEERS", "PLANNERS", "CONSULTING",
    "DRAFTING", "COMMERCIAL", "RESIDENTIAL",
    "EQUIPMENT", "SUPPLY", "SUPPLIES", "RENTALS", "SOLUTIONS",
}


# Words that name a place or a legal wrapper rather than the business itself.
# They must never corroborate a name match: "CALGARY VERTEX HOMES" against
# calgarynorthrent.com passes today because "calgary" counts as a company word and
# the rental company's title happens to contain "technology".
NON_CONTEXT_WORDS = {
    "CALGARY", "ALBERTA", "CANADA", "CANADIAN", "THE", "AND", "OF",
    "LTD", "LIMITED", "INC", "CORP", "CORPORATION", "LP", "LLP", "ULC",
    "CO", "COMPANY", "ENTERPRISE", "ENTERPRISES",
}


def company_tokens(name: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9]{2,}", name.upper())
    return [t for t in toks if t not in GENERIC_TOKENS]


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _compact_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def _domain_label(url: str) -> str:
    """Leftmost host label. For tenant sites such as
    hartwellhomes.managebuilding.com this is the company label, not the SaaS brand."""
    parts = [x for x in _host(url).split(".") if x]
    return parts[0] if parts else ""


def _tokens(name: str) -> list[str]:
    return [t.lower() for t in company_tokens(name)]


def _url_path_depth(url: str) -> int:
    """Count non-empty path segments: a homepage is 0, a profile page is 2+."""
    try:
        return len([seg for seg in urlparse(url).path.split("/") if seg])
    except ValueError:
        return 0


def _suffix_corroborates(suffix: str, word: str) -> bool:
    """Whether a domain suffix corroborates a word from the company name.

    Two legitimate shapes:
      containment  - arborcontracting.ca  -> "contracting" is in the suffix
      abbreviation - spherearc.ca        -> "arc" starts "architecture"
    A short suffix is ignored so single letters cannot match by accident.
    """
    if len(suffix) < 2 or not word:
        return False
    return word[:4] in suffix or word.startswith(suffix)


def _label_matches_token(label: str, token: str) -> bool:
    """Exact label, or a label that starts with the token on a non-digit boundary."""
    if label == token:
        return True
    if label.startswith(token):
        nxt = label[len(token):len(token) + 1]
        return bool(nxt) and not nxt.isdigit() and nxt not in ".-_"
    return False


def _name_match(title: str, website: str, company: str) -> bool:
    """Strict company match: reject one-token coincidences and mega-brand pages.

    Accept when:
      - the full company name appears in the page title, or
      - the title is a prefix of the company name (or vice versa), or
      - the compact company name appears in the domain label, or
      - at least two distinctive tokens appear in title/domain label, or
      - for single-token names, the domain label includes both the token and a
        business/industry qualifier (or the full company name is in the title).
    """
    if is_junk(website):
        return False
    # A profile page on somebody else's site: deep path AND unrelated host. A
    # directory puts the builder's name in <title>, so title evidence alone cannot
    # reject it (builderhubs.com/calgary/builder/birchglen-group), while a
    # company's own site is a homepage or a tenant subdomain that passes the
    # domain check.
    if _url_path_depth(website) >= 2 and not _domain_related(website, company):
        return False
    nt = _norm_text(title)
    nc = _norm_text(company)
    if not nc:
        return False

    toks = _tokens(company)
    label = _domain_label(website)
    host_compact = _compact_text(_host(website))
    compact = _compact_text(company)

    # Single short token (AI, etc.): only trust when the full compact name is in the domain label.
    if len(toks) == 1 and len(toks[0]) <= 3:
        return bool(compact and compact in host_compact)

    if nc in nt:
        return True
    # "Excel Homes" vs "EXCEL HOMES LIMITED PARTNERSHIP" -> accept the real site.
    if len(nt) >= 6 and len(nc) >= 6 and (nt.startswith(nc) or nc.startswith(nt)):
        return True

    if compact and len(compact) >= 5 and compact in host_compact:
        return True

    if not toks:
        return False
    title_words = set(re.findall(r"[a-z0-9]+", nt))
    label_words = set(re.findall(r"[a-z0-9]+", label))

    # If the company only has 1 non-generic token (e.g. "BASE CONSTRUCTION" -> ["base"]),
    # do NOT match bare generic domain labels like "base.org". Require the domain label
    # to be longer than just the single token (e.g., "cana.ca" has "Cana Construction" in title;
    # or domain has "baseconstruction"), OR require another word from the company name in the title/domain.
    if len(toks) == 1:
        t = toks[0]
        compact_label = _compact_text(label)
        all_company_words = (
            set(re.findall(r"[a-z0-9]{3,}", nc)) - {t}
            - {w.lower() for w in NON_CONTEXT_WORDS}
        )
        if not all_company_words:
            # A one-word company name (EYEVA): only an exact token/label counts, never a
            # longer brand that merely starts with it (eyeline.com).
            return (t in title_words and label == t) or (
                bool(compact) and len(compact) >= 5 and label == compact
            )
        has_context_word = any(
            w in title_words or w in label or w[:4] in compact_label
            for w in all_company_words
        )
        # A domain starting with the token corroborates only when its suffix contains
        # another word from the company name: arborcontracting.ca yes,
        # meadowprojects.com for "MEADOW DRAFTING AND CONSULTING" no.
        domain_has_compound = any(
            _label_matches_token(lab, t)
            and any(_suffix_corroborates(lab[len(t):], w) for w in all_company_words)
            for lab in _host(website).split(".") if lab
        )
        return (t in title_words or t in label_words or domain_has_compound) and (
            has_context_word or domain_has_compound
        )

    # Multi-token names: the FIRST distinctive token is the brand and must match, or a
    # shared generic token alone is enough (FORGE CRANE AND EQUIPMENT matched
    # "N-Force Crane & Equipment" on crane+equipment while ignoring "forge" entirely).
    primary = toks[0]
    if not (primary in title_words or primary in label_words
            or _label_matches_token(label, primary)):
        return False
    matched = {t for t in toks
               if t in title_words or t in label_words
               or _label_matches_token(label, t)
               or (t != primary and len(t) >= 4 and t in label)}
    return len(matched) >= 2


def result_matches_company(url: str, title: str, company: str) -> bool:
    return _name_match(title, url, company)


def _title_matches_company(title: str, company: str) -> bool:
    """Corroborate a company from a business title alone (a place with no website).

    Used only for Google Places hits that _place_is_local() has already placed in
    Calgary, so sharing tokens cannot admit an out-of-town business.
    """
    co = clean(company).lower()
    ti = clean(title).lower()
    if not co or not ti:
        return False
    if co in ti:
        return True
    co_toks = [t for t in re.split(r"[^a-z0-9]+", co) if len(t) > 2 and t.upper() not in NON_CONTEXT_WORDS]
    ti_toks = set(re.split(r"[^a-z0-9]+", ti))
    if not co_toks:
        return False
    hits = sum(1 for t in set(co_toks) if t in ti_toks)
    return hits >= 2 and hits * 2 >= len(set(co_toks))


def place_matches_company(title: str, website: str, company: str) -> bool:
    return _name_match(title, website, company)


# Place/regional words that make a domain a lookalike rather than the company's
# own site. Kept separate from NON_CONTEXT_WORDS because those also include legal
# endings (LTD, INC) that legitimately appear inside real domains (mbcconstruction.ca).
PLACE_WORDS = {"calgary", "alberta", "canada", "canadian", "yyc"}


def _company_initials(company: str, drop_industry: bool = False) -> str:
    """Initials of the significant words in a company name.

    "MAPLESTONE BUILDING AND CONSTRUCTION" -> "mbc"   (legal/place words dropped)
    "DC DEVELOPMENTS"                     -> "dc"    (industry word dropped too)
    Two schemes are tried because "Developments" is an industry word for one
    company but part of the brand for another; neither alone covers both.
    """
    noise = {w.lower() for w in NON_CONTEXT_WORDS}
    words = [w for w in re.findall(r"[a-z0-9]+", _norm_text(company)) if w not in noise]
    if drop_industry:
        # Plural forms count as industry too ("DEVELOPMENTS" -> "DEVELOPMENT" is
        # in GENERIC_TOKENS) so DC DEVELOPMENTS reduces to "dc", not "dd".
        words = [w for w in words
                 if w.upper() not in GENERIC_TOKENS
                 and w.upper().rstrip("S") not in GENERIC_TOKENS]
    if len(words) == 1:
        # A lone short brand token is already an abbreviation ("DC" -> "dc"),
        # so keep the token itself rather than reducing it to one letter.
        return words[0][:4]
    return "".join(w[0] for w in words)


def _domain_related(url: str, company: str) -> bool:
    """Domain-only check for cached records, where no page title was stored.

    Weaker than _name_match by nature, so it stays conservative: the *first*
    distinctive token has to be present in a domain label, and a compound label
    only counts when its suffix corroborates with another word from the name.
    Without that, any later token matched on its own ("crane" in ironcrestcrane.com).

    Two further shapes are accepted, both anchored on the leading brand token so
    a lookalike cannot slip through:
      suffix   - kaizengroup.ca for KAIZEN CONSTRUCTION ("group" is a generic
                 industry ending, NOT "projects" which is a separate brand)
      initials - mbcconstruction.ca for MAPLESTONE BUILDING AND CONSTRUCTION, dcdevelopments.ca
                 for DC DEVELOPMENTS. ironcrestcrane.com stays rejected because it
                 starts with neither "forge" nor "fce".
    """
    if not url or is_junk(url):
        return False
    host = _compact_text(_host(url))
    nc = _norm_text(company)
    nc_words = re.findall(r"[a-z0-9]{3,}", nc)
    compact = _compact_text(company)
    if compact and len(compact) >= 5 and len(nc_words) >= 2 and compact in host:
        return True
    toks = _tokens(company)
    if not toks:
        return False
    primary = toks[0]
    other_words = set(nc_words) - {primary} - {w.lower() for w in NON_CONTEXT_WORDS}
    labels = [x for x in _host(url).split(".") if x]
    for lab in labels:
        if lab == primary:
            return True
        if _label_matches_token(lab, primary) and other_words:
            suffix = lab[len(primary):]
            if any(_suffix_corroborates(suffix, w) for w in other_words):
                return True
        # Brand token plus a generic industry ending: kaizengroup.ca. The suffix
        # must be a real ending from GENERIC_TOKENS (or a place word), so
        # meadowprojects.com stays rejected while sterlingcalgary.com is kept.
        if len(primary) >= 5 and _label_matches_token(lab, primary):
            suffix = lab[len(primary):]
            if suffix and (suffix.upper() in GENERIC_TOKENS or suffix.lower() in PLACE_WORDS):
                return True
    # Company initials: mbcconstruction.ca, dcdevelopments.ca. Bounded to 2-5 letters so a single
    # letter cannot match by accident, and the remainder must be a generic ending
    # or short so a longer brand that merely starts with the initials is not
    # accepted (a short prefix must not match meadowprojects.com).
    for drop_industry in (False, True):
        ini = _company_initials(company, drop_industry=drop_industry)
        if 2 <= len(ini) <= 5:
            for lab in labels:
                if _label_matches_token(lab, ini):
                    rest = lab[len(ini):]
                    if not rest or len(rest) <= 4 or rest.upper() in GENERIC_TOKENS \
                            or rest.lower() in PLACE_WORDS:
                        return True
    return False


NON_CALGARY_CITIES = (
    "edmonton", "spruce grove", "st. albert", "st albert", "leduc", "red deer",
    "lethbridge", "medicine hat", "fort mcmurray", "grande prairie", "vancouver",
    "toronto", "usa", "united states",
)


def _place_is_local(place: dict) -> bool:
    """Reject a Google place that is evidently not a Calgary business.

    Serper is queried as "<company> Calgary AB", but it still returns a nearby or
    out-of-province business when no Calgary listing exists, which is how a
    Spruce Grove number or an Edmonton address can end up on a Calgary lead.
    """
    address = clean(place.get("address")).lower()
    if address and "calgary" not in address and any(c in address for c in NON_CALGARY_CITIES):
        return False
    digits = re.sub(r"\D", "", clean(place.get("phoneNumber")))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10 and digits[:3] not in CA_PHONE_AREA_CODES:
        return False
    return True
