"""Regressions for bugs that once shipped: each block names the original defect.

These are cheap guards, not a specification: if one fails, a previously fixed
bug has come back.
"""

import sqlite3

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, check_eq, finish

# --- cache_get used to raise NameError on a bare `website` inside a list ------
conn = sqlite3.connect(":memory:")
conn.execute("""CREATE TABLE companies (
    company_name TEXT PRIMARY KEY, unmasked_tradename TEXT DEFAULT '',
    licence_type TEXT DEFAULT '', website TEXT DEFAULT '', phone TEXT DEFAULT '',
    email TEXT DEFAULT '', address TEXT DEFAULT '',
    google_rating REAL DEFAULT NULL, kill_reason TEXT DEFAULT '', updated_at TEXT NOT NULL)""")
conn.execute("INSERT INTO companies (company_name, unmasked_tradename, licence_type, website, phone, email, address, google_rating, kill_reason, updated_at) VALUES "
             "('ACME HOMES','','','https://acme.ca/','(403) 555-1234','info@acme.ca','1 St','4.8','','2026-01-01')")
conn.commit()
for _col, _ddl in P.CONTACT_COLUMN_DDL:  # production always runs init_db(), which adds these
    conn.execute(f"ALTER TABLE companies ADD COLUMN {_col} {_ddl}")
rec = P.cache_get(conn, "Acme Homes")
check_eq("cache_get returns a dict", isinstance(rec, dict), True)
check_eq("cache_get returns the stored website", (rec or {}).get("website"), "https://acme.ca/")

# --- valid_email used a substring test, so `ir@` rejected their@/repair@ ------
for kept in ("repair@acme.ca", "their@acme.ca", "chair@acme.ca", "flair@acme.ca",
             "info@acme.ca", "multimedia@acme.ca"):
    check_eq(f"valid_email keeps {kept}", P.valid_email(kept), True)
for dropped in ("ir@acme.ca", "investorrelations@acme.ca",
                "bondholders@mattamycorp.com", "Information.Texas@brookfieldrp.com"):
    check_eq(f"valid_email drops {dropped}", P.valid_email(dropped), False)


# --- the sanitizer was a no-op because it compared a name against itself ------
def sanitized_website(company, website):
    return P.sanitize_cached_record({
        "company_name": company, "unmasked_tradename": "", "licence_type": "",
        "website": website, "phone": "", "email": "", "address": "",
        "google_rating": None,
    })["website"]


check_eq("sanitizer keeps a related domain",
         sanitized_website("ACME HOMES", "https://acme.ca/"), "https://acme.ca/")
check_eq("sanitizer keeps a tenant subdomain",
         sanitized_website("HARTWELL HOMES", "https://hartwellhomes.managebuilding.com/x"),
         "https://hartwellhomes.managebuilding.com/x")
check_eq("sanitizer keeps a prefixed domain label",
         sanitized_website("ARBOR CONTRACTING", "https://www.arborcontracting.ca/"),
         "https://www.arborcontracting.ca/")
check_eq("sanitizer drops an unrelated domain",
         sanitized_website("PRAIRIE CONSTRUCTION", "https://totally-unrelated-domain.example/"), "")
check_eq("sanitizer drops a junk domain",
         sanitized_website("HAMILTON DESIGN STUDIO",
                           "https://amanda-the-adventurer.fandom.com/wiki/Amanda"), "")

# --- _domain_label used to return the subdomain (cloud, managebuilding) ------
check_eq("_domain_label on a tenant subdomain",
         P._domain_label("https://hartwellhomes.managebuilding.com/x"), "hartwellhomes")
check_eq("_domain_label on a www host",
         P._domain_label("https://www.arborcontracting.ca/"), "arborcontracting")

# --- dev permits were pitched as building permits -----------------------------
check_eq("pitch copy: subtrade on a development permit",
         P.pitch_for("SUBTRADE_PITCH", "DEVELOPMENT_PERMIT"),
         "Pre-construction subtrade opportunity — development permit filed, trades not yet awarded")
check_eq("pitch copy: subtrade on a building permit",
         P.pitch_for("SUBTRADE_PITCH", "BUILDING_PERMIT"),
         "Subtrade / supplier opportunity — active GC permit")

# --- guard: only Calgary / southern Alberta phone numbers are accepted -------
# A (780) number on a Calgary permit almost always means the enrichment matched a
# Northern-Alberta business with a similar name.
check_eq("normalize_phone rejects 780", P.normalize_phone("(780) 555-0115"), "")
check_eq("normalize_phone keeps 403", P.normalize_phone("(403) 555-0116"), "(403) 555-0116")
check_eq("normalize_phone keeps 587", P.normalize_phone("587-555-0116"), "(587) 555-0116")
for code in ("403", "587", "825", "368"):
    check_eq(f"area code {code} is allowed", code in P.CA_PHONE_AREA_CODES, True)
check_eq("area code 780 is not allowed", "780" in P.CA_PHONE_AREA_CODES, False)

# --- guard: store-locator paths are junk ------------------------------------
check_eq("store locator path is junk",
         P.is_junk("https://eyeglassshop.example/en/stores/canada/"), True)
check_eq("dealer path is junk", P.is_junk("https://x.ca/dealers/list"), True)
check_eq("ordinary path is not junk", P.is_junk("https://x.ca/about"), False)

# --- guard: equipment/supply words are not brand tokens ---------------------
for word in ("EQUIPMENT", "SUPPLY", "SUPPLIES", "RENTALS", "SOLUTIONS"):
    check_eq(f"{word} is generic", word in P.GENERIC_TOKENS, True)

# --- guard: cached domains that no longer match are dropped -----------------
check_eq("sanitizer drops meadowprojects",
         sanitized_website("MEADOW DRAFTING AND CONSULTING", "https://www.meadowprojects.example/"), "")
check_eq("sanitizer drops eyeline",
         sanitized_website("EYEVA", "https://eyeglassshop.example/en/stores/canada/"), "")
check_eq("sanitizer drops ironcrestcrane",
         sanitized_website("FORGE CRANE AND EQUIPMENT", "http://www.ironcrestcrane.com/"), "")
check_eq("sanitizer drops 780 phone from a cached record",
         P.sanitize_cached_record({
             "company_name": "GRANITE RIDGE HOMES", "unmasked_tradename": "",
             "licence_type": "", "website": "", "phone": "(780) 555-0115",
             "email": "", "address": "", "google_rating": None})["phone"], "")
# Documented escape hatch: when the stored email sits on the SAME host as the
# stored website, the two corroborate each other and the site is kept. That is
# what allows a wrong-but-self-consistent pair (ironcrestcrane.com +
# jamier@ironcrestcrane.com) to survive sanitisation, and why those rows have to be
# deleted from cache.db rather than relying on the sanitizer.
# A self-consistent but structurally unrelated pair is now REJECTED. This used
# to survive because the email host matched the web host — but that is exactly
# the self-corroboration the structural guard forbids: a directory (or a wrong
# company) pairing its own profile URL with its own address vouched for itself
# into a sellable list. An unrelated host means the whole harvest is wrong.
_forge = P.sanitize_cached_record({
    "company_name": "FORGE CRANE AND EQUIPMENT", "unmasked_tradename": "",
    "licence_type": "", "website": "http://www.ironcrestcrane.com/",
    "phone": "", "email": "jamier@ironcrestcrane.com", "address": "",
    "google_rating": None})
check_eq("a self-consistent wrong pair is rejected", _forge["website"], "")
check_eq("its self-corroborating email goes with it", _forge["email"], "")


def _kept(company, website, email=""):
    return P.sanitize_cached_record({
        "company_name": company, "unmasked_tradename": "", "licence_type": "",
        "website": website, "phone": "(403) 555-0100", "email": email,
        "address": "", "google_rating": None,
    })["website"]


# --- legitimate Calgary roots must survive the structural guard --------------
# These were being wiped when the "email host == web host" escape hatch was
# removed wholesale. They are real companies whose domain shape differs from the
# registered name, so the matcher needs structural rules, not the escape hatch.
check_eq("kaizengroup.ca is kept for KAIZEN CONSTRUCTION",
         _kept("KAIZEN CONSTRUCTION", "http://kaizengroup.ca/", "info@kaizengroup.ca"),
         "http://kaizengroup.ca/")
check_eq("dcdevelopments.ca is kept for DC DEVELOPMENTS",
         _kept("DC DEVELOPMENTS", "https://www.dcdevelopments.ca/", "info@dcdevelopments.ca"),
         "https://www.dcdevelopments.ca/")
check_eq("mbcconstruction.ca is kept for MAPLESTONE BUILDING AND CONSTRUCTION",
         _kept("MAPLESTONE BUILDING AND CONSTRUCTION", "http://www.mbcconstruction.ca/",
               "office@mbcconstruction.ca"),
         "http://www.mbcconstruction.ca/")

# --- ...while the lookalikes and directories stay rejected --------------------
# A directory profile: deep path (depth 3) on an unrelated host. The directory's
# own email must not vouch for it.
check_eq("builderhubs.example profile is rejected",
         _kept("SOLANO CONSTRUCTION",
               "https://builderhubs.example/calgary/builder/solano-construction",
               "hello@builderhubs.example"), "")
# A rental company whose domain starts with the city name the applicant shares.
# "calgary" is a place word, not a brand token, so the self-consistent email
# cannot keep it.
check_eq("calgarynorthrent.example is rejected for CALGARY VERTEX HOMES",
         _kept("CALGARY VERTEX HOMES", "https://calgarynorthrent.example/",
               "sales@calgarynorthrent.example"), "")

# --- guard: Google Places results must be local -----------------------------
check_eq("Calgary address accepted",
         P._place_is_local({"address": "1623 6 St NW, Calgary, AB T2M 3E9"}), True)
check_eq("Edmonton address rejected",
         P._place_is_local({"address": "123 Main St, Edmonton, AB"}), False)
check_eq("Spruce Grove address rejected",
         P._place_is_local({"address": "5 Industrial Rd, Spruce Grove, AB"}), False)
check_eq("US address rejected",
         P._place_is_local({"address": "100 Main St, Dallas, United States"}), False)
check_eq("780 phone rejected",
         P._place_is_local({"address": "Calgary, AB", "phoneNumber": "(780) 555-0115"}), False)
check_eq("403 phone accepted",
         P._place_is_local({"address": "Calgary, AB", "phoneNumber": "(403) 555-0111"}), True)
check_eq("empty place accepted (nothing to reject on)", P._place_is_local({}), True)

# --- guard: keyless fallback sites need a Calgary signal --------------------
check_eq("host ending .ca is a signal",
         P._has_calgary_signal("<html></html>", "https://arborcontracting.ca/", "", ""), True)
check_eq("a Calgary phone is a signal",
         P._has_calgary_signal("<html></html>", "http://example.com/", "(403) 555-0112", ""), True)
check_eq("a .ca email is a signal",
         P._has_calgary_signal("<html></html>", "http://example.com/", "", "info@x.ca"), True)
check_eq("naming Calgary is a signal",
         P._has_calgary_signal("<p>Servicing Calgary since 1999</p>", "http://example.com/", "", ""), True)
check_eq("'Alberta' alone is NOT a signal (Edmonton sites say it too)",
         P._has_calgary_signal("<p>Serving Alberta</p>", "http://example.com/", "", ""), False)
check_eq("no signal at all is rejected",
         P._has_calgary_signal("<p>Dallas golf memberships</p>", "https://golfshop.example/", "", ""), False)

# --- guard: a rejected website takes its phone and email with it ------------
# Both were scraped from the same wrong company, and both were reaching a
# SELLABLE list while the website alone was being discarded.
def _rec(company, website="", phone="", email="", unmasked=""):
    return {"company_name": company, "unmasked_tradename": unmasked, "licence_type": "",
            "website": website, "phone": phone, "email": email, "address": "",
            "google_rating": None}


_vista = P.sanitize_cached_record(_rec("MEADOW DRAFTING AND CONSULTING",
                                       "https://www.meadowprojects.example/",
                                       "(403) 555-0111", "bd@meadowmiddleeast.example"))
check_eq("poisoned website dropped", _vista["website"], "")
check_eq("its phone dropped too", _vista["phone"], "")
check_eq("its email dropped too", _vista["email"], "")

_eyeva = P.sanitize_cached_record(_rec("EYEVA", "https://eyeglassshop.example/en/stores/canada/",
                                       "(403) 555-0113"))
check_eq("an eyewear brand's phone is dropped", _eyeva["phone"], "")

# ...while legitimate contacts must survive. A blanket "email domain must match
# the company name" rule would have destroyed all three of these.
_mattamy = P.sanitize_cached_record(_rec("MATTAMY HOMES CALGARY", "https://www.mattamyhomes.com/",
                                         "(403) 555-0114", "Trades.Canada@mattamycorp.com"))
check_eq("a corporate trades inbox survives", _mattamy["email"], "Trades.Canada@mattamycorp.com")
_briarwood = P.sanitize_cached_record(_rec("BRIARWOOD HOMES", "https://briarwoodhomes.ca/",
                                            "(403) 555-0101", "jamie@skylinegroup.ca"))
check_eq("a parent-company inbox survives", _briarwood["email"], "jamie@skylinegroup.ca")
_free = P.sanitize_cached_record(_rec("ACME HOMES", "", "", "acmesyntheticmail@gmail.com"))
check_eq("a free mailbox needs no domain match", _free["email"], "acmesyntheticmail@gmail.com")
_own = P.sanitize_cached_record(_rec("ACME HOMES", "", "", "info@acme.ca"))
check_eq("a matching own-domain email survives", _own["email"], "info@acme.ca")

# --- structural guard: a directory profile is not a company website ----------
# Shipped defect: builderhub.example/calgary/builder/<slug> passed the name check
# because the directory puts the builder's name in <title>, and its own
# hello@builderhub.example address then corroborated the site, so the row was sold
# with the directory's contact details.
_dir = P.sanitize_cached_record(_rec(
    "SOLANO CONSTRUCTION",
    "https://builderhub.example/calgary/builder/solano-construction",
    "(403) 555-0142", "hello@builderhub.example"))
check_eq("a deep directory profile is rejected", _dir["website"], "")
check_eq("its phone goes with it", _dir["phone"], "")
check_eq("its self-corroborating email goes with it", _dir["email"], "")

# A deep path on the company's OWN domain is a service page, not a directory.
_own_path = P.sanitize_cached_record(_rec(
    "ACME HOMES", "https://acme.ca/services/residential/renovations", "", ""))
check_eq("a deep path on the company's own domain survives",
         _own_path["website"], "https://acme.ca/services/residential/renovations")

# A tenant subdomain is the builder's own storefront, however deep the path.
_tenant = P.sanitize_cached_record(_rec(
    "HARTWELL HOMES", "https://hartwellhomes.managebuilding.com/Resident/public/home",
    "", ""))
check_eq("a tenant subdomain survives the path guard",
         _tenant["website"], "https://hartwellhomes.managebuilding.com/Resident/public/home")

# --- a multi-permit company was shown as a single small job ------------------
# dedupe_leads keeps the highest-value permit per company; without a roll-up the
# reader sees one row and never learns the same company filed several jobs.
def _mk(co, value, desc, pn):
    return P.Lead(company_name=co, target_company=co, permit_number=pn,
                  permit_value=value, description=desc, address="1 St",
                  permit_type="BP", applied_date="2026-10-20", status="Applied",
                  lead_type="NEEDS_GC")

_dd = P.dedupe_leads([
    _mk("ACME BUILDERS", 500000, "Office renovation", "BP1"),
    _mk("ACME BUILDERS", 250000, "Warehouse fit-out", "BP2"),
    _mk("SOLO CO", 900000, "Single job", "BP3"),
])
_by_name = {x.company_name: x for x in _dd}
check_eq("dedupe keeps one row per company", len(_dd), 2)
check_eq("dedupe keeps the highest-value permit",
         _by_name["ACME BUILDERS"].permit_value, 500000)
check("multi-permit row carries the roll-up",
      "+1 more permit(s)" in _by_name["ACME BUILDERS"].description)
check("roll-up reports the combined extra value",
      "$250,000 combined" in _by_name["ACME BUILDERS"].description)
check("single-permit row gets no roll-up suffix",
      "more permit" not in _by_name["SOLO CO"].description)

# --- an architect/design firm with no GC was pitched full-build GC services --
# It runs the tender itself; the useful ask is an invite to its bid list.
check_eq("design firm gets the architect pitch",
         P.pitch_for("NEEDS_GC", "", "FORMA DESIGN LAB"),
         "Architect / designer of record (no GC listed) — request invite to tender / bid list")
check_eq("design firm on an early-design permit gets it too",
         P.pitch_for("EARLY_DESIGN", "", "ARCHWAY DESIGN"),
         "Architect / designer of record (no GC listed) — request invite to tender / bid list")
# A design-build firm is a GC and must keep the GC pitch.
check_eq("design-build firm keeps the GC pitch",
         P.pitch_for("NEEDS_GC", "", "DESIGN BUILD GROUP LTD"),
         "Full-build GC services — applicant has no contractor of record")
check_eq("ordinary GC applicant is unaffected",
         P.pitch_for("NEEDS_GC", "", "SOLANO DEVELOPMENTS"),
         "Full-build GC services — applicant has no contractor of record")

# --- long email/phone broke mid-token in the HTML and print layout -----------
import inspect
_src = inspect.getsource(P.export_client_html)
check("the table layout is gone from the client sheet", "<table" not in _src)
check("the reach line stays on one line (no mid-token break)",
      "white-space:nowrap" in P.CLIENT_SHEET_CSS and ".rreach" in P.CLIENT_SHEET_CSS)

finish()
