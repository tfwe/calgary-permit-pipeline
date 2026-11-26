"""What counts as sellable, how the sheet client CSV is shaped, and the exit codes.

The email grading matters commercially: an address on a domain with no MX record
bounces, so it must not count as a contact channel.
"""

import csv
from pathlib import Path

import html

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, check_eq, finish


def lead(permit="P1", phone="", email="", status="", website="", ltype="SUBTRADE_PITCH",
         address="", community=""):
    return P.Lead(permit_number=permit, permit_type="Building", lead_type=ltype,
                  company_name="EXAMPLE HOMES", phone=phone, email=email,
                  email_status=status, website=website, permit_value=100000.0,
                  address=address, community=community)


# --- reachability: a dead-domain email is not a contact channel ---------------
check("phone alone is reachable", P._reachable(lead(phone="4031234567")) is True)
check("mx_ok email is reachable", P._reachable(lead(email="a@x.ca", status="mx_ok")) is True)
check("role email is reachable", P._reachable(lead(email="info@x.ca", status="role")) is True)
check("no_mx-only lead is NOT reachable", P._reachable(lead(email="a@x.ca", status="no_mx")) is False)
check("no channel is not reachable", P._reachable(lead()) is False)
check("no_mx email plus a phone IS reachable",
      P._reachable(lead(phone="4031234567", email="a@x.ca", status="no_mx")) is True)

# --- email grading (offline-tolerant: DNS lookups fail open to 'unverified') --
check("gmail grades mx_ok or unverified",
      P.email_status("bob.synthetic@gmail.com") in {"mx_ok", "unverified"})
check("role local part is flagged",
      P.email_status("info.synthetic@gmail.com") in {"role", "role_unverified"})
check("dead domain grades no_mx or unverified",
      P.email_status("bob@no-such-domain-for-mx-test.invalid") in {"no_mx", "unverified"})
check_eq("empty email grades empty", P.email_status(""), "")

# --- client CSV shape --------------------------------------------------------
tmp = Path("/tmp/_test_delivery.csv")
P.export_clean_client_csv([lead(phone="4031234567")], tmp)
rows = list(csv.reader(tmp.open()))
check_eq("client CSV has 13 columns (Contact Name added)", len(rows[0]), 13)
check("client CSV carries Email Status", "Email Status" in rows[0])
check("client CSV rows match the header width", all(len(r) == 13 for r in rows[1:]))
tmp.unlink()

# --- console summary: print_summary must render, not raise NameError ----------
# print_summary builds a rich Table; cpp_export must import Table itself. No other
# test called print_summary, so a missing import went unnoticed until a real run.
import contextlib as _contextlib
import io as _io
_summary_out = _io.StringIO()
_summary_err = None
try:
    with _contextlib.redirect_stdout(_summary_out):
        P.print_summary([lead(phone="4031234567")])
except Exception as _e:  # noqa: BLE001
    _summary_err = _e
check("print_summary runs without raising", _summary_err is None)
check("print_summary prints the 'Leads:' line", "Leads:" in _summary_out.getvalue())

# --- exit codes are distinct so an unattended runner can alert by class ------
check_eq("exit codes are unique",
         len({P.EXIT_OK, P.EXIT_NO_PERMITS, P.EXIT_CONFIG, P.EXIT_LOW_YIELD, P.EXIT_UPLOAD}), 5)
check_eq("upload failure has its own code", P.EXIT_UPLOAD, 4)
check_eq("config failure has its own code", P.EXIT_CONFIG, 2)

# --- client-facing HTML sheet: tiered cards -----------------------------------
# A human pastes this into an email or prints it, so: tappable links, no internal
# jargon, no empty-field placeholders, and every lead appears exactly once.
import re as _re
import tempfile as _tempfile
from html.parser import HTMLParser as _HTMLParser

_OUT = Path(_tempfile.mkdtemp(prefix="client_sheet_"))


class _Texts(_HTMLParser):
    """Collects visible text nodes, so placeholder checks see what a reader sees."""

    def __init__(self):
        super().__init__()
        self.texts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(data.strip())


def _visible(doc: str) -> list[str]:
    parser = _Texts()
    parser.feed(doc)
    return parser.texts


def _sheet(leads, title="Test Sheet") -> str:
    out = _OUT / "sheet.html"
    P.export_client_html(leads, out, title)
    return out.read_text(encoding="utf-8")


def _cards(doc: str) -> list[str]:
    return _re.findall(r"<article class=\"card\">.*?</article>", doc, _re.S)


def _rows(doc: str) -> list[str]:
    return _re.findall(r'<li><span class="rn">.*?</li>', doc, _re.S)


def _named(co, value, **kw):
    kw.setdefault("contact_name", "Jo Smith")
    kw.setdefault("email", "jo@" + co.split()[0].lower() + ".ca")
    kw.setdefault("phone", "(403) 555-0100")
    return P.Lead(permit_number=f"N-{co}", permit_type="Commercial / Multi Family Project",
                  lead_type="NEEDS_GC", company_name=co, permit_value=value, **kw)


# (a) role inbox + phone, no person -> compact MAINLINE row, never a full card
_ml = P.Lead(permit_number="ML1", permit_type="Single Construction Permit", lead_type="SUBTRADE_PITCH",
             company_name="MAINLINE ONLY LTD", permit_value=90000,
             email="info@mainline.ca", phone="(403) 555-0177")
check_eq("(a) role inbox + phone is tier mainline", P.client_tier(_ml), "mainline")
_ml_doc = _sheet([_ml])
check_eq("(a) role inbox + phone renders no full card", len(_cards(_ml_doc)), 0)
check_eq("(a) it renders as one compact row", len(_rows(_ml_doc)), 1)
check("(a) under the 'Contact by main line' heading",
      "Contact by main line <span>1</span>" in _ml_doc)
check("(a) the row keeps a real tel: link",
      'href="tel:+14035550177"' in _ml_doc)

# (b) a lead with a person is a full card that shows the name
_person = _named("PERSON CO", 300000, contact_name="Dana Whitfield", email="dana@personco.ca")
check_eq("(b) a named lead is tier named", P.client_tier(_person), "named")
_p_doc = _sheet([_person])
_p_cards = _cards(_p_doc)
check_eq("(b) a lead with a person is a full card", len(_p_cards), 1)
check("(b) the card shows the contact's name", "Dana Whitfield" in _p_cards[0])
check("(b) the card links the email with mailto:", 'href="mailto:dana@personco.ca"' in _p_cards[0])

# (c) no sheet shows a dash or 'None' in place of a missing value
_bare = [
    P.Lead(permit_number="B1", permit_type="Building", lead_type="NEEDS_GC", company_name="BARE ONE"),
    P.Lead(permit_number="B2", permit_type="Building", lead_type="NEEDS_GC", company_name="BARE TWO",
           email="sales@bare.ca"),
    P.Lead(permit_number="B3", permit_type="Building", lead_type="NEEDS_GC", company_name="BARE THREE",
           phone="(403) 555-0133"),
    _named("FULL CO", 120000, website="https://www.fullco.ca/", address="9 1 ST NE",
           community="CRESCENT HEIGHTS", description="Commercial / Multi Family Project — New build"),
    _ml,
]
_bare_doc = _sheet(_bare)
_texts = _visible(_bare_doc)
check_eq("(c) no visible text node is a dash placeholder",
         [t for t in _texts if t in {"-", "—", "–", "&mdash;", "&ndash;"}], [])
check_eq("(c) no visible text node is 'None'", [t for t in _texts if t == "None"], [])
check_eq("(c) no 'None' word anywhere in the visible text",
         _re.search(r"\bNone\b", " ".join(_texts)) is None, True)
check_eq("(c) no em dash is rendered as a placeholder", "&mdash;" in _bare_doc, False)
check_eq("(c) no empty-field placeholder class", 'class="none"' in _bare_doc, False)
check_eq("(c) the sheet carries no 'Unspecified' copy", "Unspecified" in _bare_doc, False)
check_eq("(c) every lead is rendered",
         len(_cards(_bare_doc)) + len(_rows(_bare_doc)), len(_bare))

# (d) an empty tier omits its heading
_only_named = _sheet([_person])
check_eq("(d) no mainline heading when that tier is empty",
         "Contact by main line" in _only_named, False)
check_eq("(d) no inbox heading when that tier is empty",
         "General inbox only" in _only_named, False)
check_eq("(d) no rest wrapper group when there are no compact rows",
         'class="grp"' in _only_named, False)
_only_ml = _sheet([_ml])
check_eq("(d) no 'General inbox only' heading when only mainline leads exist",
         "General inbox only" in _only_ml, False)

# (e) the 'Largest projects' strip is omitted when no lead qualifies
_inbox_only = P.Lead(permit_number="I1", permit_type="Building", lead_type="NEEDS_GC",
                     company_name="INBOX ONLY CO", permit_value=500000, email="office@inbox.ca")
check_eq("(e) a role-inbox-only lead is tier inbox", P.client_tier(_inbox_only), "inbox")
_no_strip = _sheet([_ml, _inbox_only])
check_eq("(e) strip omitted when no named or direct lead exists",
         "Top projects with contact details" in _no_strip, False)
check_eq("(e) the inbox lead still renders", "INBOX ONLY CO" in _no_strip, True)
check_eq("(e) strip is present once a named lead exists",
         "Top projects with contact details" in _sheet([_person, _ml]), True)

# --- tiering rules -------------------------------------------------------------
check_eq("tier: named when contact_name is set (even with a role inbox)",
         P.client_tier(_named("ROLE NAMED", 1, email="info@rolenamed.ca")), "named")
check_eq("tier: direct for a non-role email, no person",
         P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                              company_name="D CO", email="jane@dco.ca")), "direct")
check_eq("tier: role inbox matching is case-insensitive",
         P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                              company_name="D CO", email="INFO@dco.ca", phone="4035550100")),
         "mainline")
check_eq("tier: a role inbox with no phone is inbox",
         P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                              company_name="D CO", email="sales@dco.ca")), "inbox")
check_eq("tier: no email and a phone is mainline",
         P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                              company_name="D CO", phone="4035550100")), "mainline")
check_eq("tier: nothing at all is inbox",
         P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                              company_name="D CO")), "inbox")
for _role in sorted(P.CLIENT_ROLE_INBOXES):
    check_eq(f"role inbox {_role!r} is muted, not a direct contact",
             P.client_tier(P.Lead(permit_number="D", permit_type="x", lead_type="NEEDS_GC",
                                  company_name="D CO", email=f"{_role}@dco.ca")), "inbox")

# --- nothing is dropped; sort is by value; strip is top five -----------------
_mix = [
    _named("ALPHA NAMED", 900000),
    _named("BRAVO NAMED", 800000),
    P.Lead(permit_number="M9", permit_type="x", lead_type="NEEDS_GC", company_name="BIG MAINLINE",
           permit_value=950000, phone="(403) 555-0190"),
    _named("CHARLIE NAMED", 700000),
    _named("DELTA NAMED", 600000),
    _named("ECHO NAMED", 500000),
    _named("FOXTROT NAMED", 400000),
    P.Lead(permit_number="I9", permit_type="x", lead_type="NEEDS_GC", company_name="TINY INBOX",
           permit_value=1000),
]
_mix_doc = _sheet(_mix)
check_eq("every lead appears exactly once (cards + rows)",
         len(_cards(_mix_doc)) + len(_rows(_mix_doc)), len(_mix))
check_eq("the strip lists five leads",
         _re.search(r'<div class="start">.*?</div>', _mix_doc, _re.S).group(0).count("<li>"), 5)
check_eq("the strip leaves out a larger mainline lead",
         "BIG MAINLINE" in _re.search(r'<div class="start">.*?</div>', _mix_doc, _re.S).group(0), False)
check_eq("the strip leaves out the lowest-value named lead",
         "FOXTROT NAMED" in _re.search(r'<div class="start">.*?</div>', _mix_doc, _re.S).group(0), False)
_order = [m for m in _re.findall(r"<h3>([A-Z ]+)", _mix_doc)]
check_eq("cards are sorted by value, highest first",
         _order, ["ALPHA NAMED", "BRAVO NAMED", "CHARLIE NAMED", "DELTA NAMED", "ECHO NAMED", "FOXTROT NAMED"])
check_eq("stats count the full cards", '<b>6</b>with a named contact' in _mix_doc, True)

# --- pitch chips: five real strings map; anything else gets no chip ----------
_chip_doc = _sheet([
    _named("CHIP ACTIVE", 1, pitch_angle="Subtrade / supplier opportunity — active GC permit"),
    _named("CHIP NONE", 2, pitch_angle="Full-build GC services — applicant has no contractor of record"),
    _named("CHIP FREE", 3, pitch_angle="Some invented angle"),
])
check_eq("a real angle renders its chip", ">Active GC</span>" in _chip_doc, True)
check_eq("a real angle renders its chip (GC)", ">No contractor on record</span>" in _chip_doc, True)
check_eq("an unknown angle renders no chip", "Some invented angle" in _chip_doc, False)
check_eq("only two chips render for three leads", _chip_doc.count('class="chip"'), 2)

# --- values, scope, links -------------------------------------------------
check_eq("a zero value renders as TBD, faint", 'class="val tbd"' in _sheet(
    [P.Lead(permit_number="Z", permit_type="x", lead_type="NEEDS_GC", company_name="ZERO CO",
            email="jane@zero.ca")]), True)
check_eq("a real value renders in full", "$750,000" in _sheet(
    [_named("VALUE CO", 750000)]), True)
_link_doc = _sheet([_named("LINK CO", 1000, website="https://www.linkco.ca/", address="12 Main St SW",
                           community="BELTLINE", email="jo@linkco.ca", phone="(403) 555-0100")])
check_eq("company links to the website", 'href="https://www.linkco.ca/"' in _link_doc, True)
check_eq("address links to Google Maps", "google.com/maps/search" in _link_doc, True)
check_eq("phone is a tel: link", 'href="tel:+14035550100"' in _link_doc, True)
check_eq("email is a mailto: link", 'href="mailto:jo@linkco.ca"' in _link_doc, True)
check_eq("the host is shown beside the company", '<span class="host">linkco.ca</span>' in _link_doc, True)
check_eq("the sheet is A4 landscape for print", "A4 landscape" in _link_doc, True)
check_eq("cards and compact rows must not break across pages",
         "break-inside:avoid" in _link_doc, True)

# --- scope copy --------------------------------------------------------------
_scope_doc = _sheet([P.Lead(permit_number="S", permit_type="Commercial / Multi Family Project",
                            lead_type="NEEDS_GC", company_name="SCOPE CO", permit_value=1,
                            email="jane@scope.ca",
                            description="Commercial / Multi Family Project — Alteration "
                                        "[+2 more permit(s) this window, $5]")])
check_eq("the category prefix is stripped from the scope", "Commercial / Multi Family Project —" in _scope_doc, False)
check_eq("the roll-up counter is not printed", "more permit(s) this window" in _scope_doc, False)
check_eq("the scope reads 'Alteration'", ">Alteration</p>" in _scope_doc, True)

# --- OGL footer, escaping, empty list -------------------------------------
_ogl = _sheet([_named("LICENCE CO", 1)])
check("the licence footer is present", "Contains information licensed under the" in _ogl)
check("the licence names the Open Government Licence", "Open Government Licence" in _ogl)
check("the licence names the City of Calgary", "City of Calgary" in _ogl)
check("the licence links the Open Calgary terms page",
      "data.calgary.ca/stories/s/Open-Calgary-Terms-of-Use" in _ogl)

check_eq("an empty list still renders a usable document",
         "No leads in this list." in _sheet([]), True)

_sneaky = _sheet([P.Lead(permit_number="P10", permit_type="Building", lead_type="SUBTRADE_PITCH",
                         company_name="<script>alert(1)</script>", permit_value=1.0,
                         contact_name="<b>x</b>")])
check_eq("markup in a company name is escaped, not injected", "<script>" in _sneaky, False)
check_eq("markup in a contact name is escaped", "<b>x</b>" in _sneaky, False)

# --- multi-permit companies: one card, every jobsite listed ------------------
def _mp(co, value, desc, pn, addr, community, **kw):
    return P.Lead(permit_number=pn, permit_type="Commercial / Multi Family Project",
                  lead_type="NEEDS_GC", company_name=co, permit_value=value,
                  description=desc, address=addr, community=community,
                  applied_date="2026-10-20", status="In Review",
                  pitch_angle="Full-build GC services — applicant has no contractor of record",
                  **kw)


_mp_leads = [
    _mp("KAIZEN CONSTRUCTION", 2000000, "High-rise concrete structure", "BP1",
        "123 4 AV SW", "DOWNTOWN"),
    _mp("KAIZEN CONSTRUCTION", 451586, "Parkade slab", "BP2", "456 7 ST SW", "BELTLINE"),
    _mp("KAIZEN CONSTRUCTION", 300000, "Retaining wall", "BP3", "789 8 AV SW", "MISSION"),
]
_mp_deduped = [x.model_copy(update={"phone": "(403) 555-0150"})
               for x in P.dedupe_leads(_mp_leads)]
check_eq("dedupe keeps exactly one row for the company", len(_mp_deduped), 1)
_kept = _mp_deduped[0]
check_eq("the kept row is the highest-value permit", _kept.permit_value, 2000000)
check("the summary roll-up stays in the description",
      "+2 more permit(s)" in _kept.description)
check_eq("both other permits are retained on the lead", len(_kept.extra_permits), 2)

_dump = _kept.model_dump() if hasattr(_kept, "model_dump") else _kept.dict()
check("extra permits are excluded from model_dump", "extra_permits" not in _dump)
_master_path = _OUT / "multi.csv"
P.export_csv(_mp_deduped, _master_path)
import csv as _csv
with _master_path.open(newline="", encoding="utf-8") as _f:
    _rows_csv = list(_csv.reader(_f))
check_eq("the master CSV has 25 columns (3 contact fields added)", len(_rows_csv[0]), 25)
check("the extra-permit column never appears in the header", "extra_permits" not in _rows_csv[0])
check_eq("the master CSV keeps one row per company", len(_rows_csv) - 1, 1)

_mp_doc = _sheet(_mp_deduped)
check_eq("one row for the multi-permit company (no contact, phone only)", len(_rows(_mp_doc)), 1)
check_eq("the company name appears once", _mp_doc.count("KAIZEN CONSTRUCTION"), 1)
check("extra permits render as 'Also filed' on the row", "Also filed:" in _mp_doc)
check("the second jobsite address is listed", "456 7 ST SW" in _mp_doc)
check("the third jobsite address is listed", "789 8 AV SW" in _mp_doc)
check("the extra permit number is listed", "BP2" in _mp_doc)
check("the extra permit value is listed", "$451,586" in _mp_doc)
check_eq("the sub-lines do not double-escape the middle dot", "&amp;middot;" in _mp_doc, False)
check_eq("the roll-up counter is not repeated on the row",
         _mp_doc.count("more permit(s) this window"), 0)
_mp_card_lead = P.dedupe_leads(_mp_leads)[0].model_copy(
    update={"contact_name": "Ana Lee", "email": "ana@kaizen.ca"})
_mp_card_doc = _sheet([_mp_card_lead])
_mp_card = _cards(_mp_card_doc)
check_eq("with a contact, the multi-permit company is a full card", len(_mp_card), 1)
check("the card lists the other jobsites as 'Also filed'", "Also filed:" in _mp_card[0] and "456 7 ST SW" in _mp_card[0])



# --- ordering: cards, strip and compact rows run highest value first ----------
# Named and direct leads are concatenated into the card list; before the fix every
# direct lead landed after every named lead regardless of value.
def _money_to_int(text: str) -> int:
    text = text.strip()
    return 0 if text == "TBD" else int(text.lstrip("$").replace(",", ""))


def _card_values(doc: str) -> list[int]:
    return [_money_to_int(_re.search(r'<span class="val[^"]*">([^<]*)</span>', c).group(1))
            for c in _cards(doc)]


def _strip_block(doc: str) -> str:
    return _re.search(r'<div class="start">.*?</div>', doc, _re.S).group(0)


def _strip_names(doc: str) -> list[str]:
    return _re.findall(r'<span class="s-co">([^<]*)</span>', _strip_block(doc))


def _group_values(doc: str, heading: str) -> list[int]:
    sections = _re.findall(r'<section class="grp">.*?</section>', doc, _re.S)
    sec = [s for s in sections if heading in s]
    assert len(sec) == 1, heading
    return [_money_to_int(v) for v in _re.findall(r'<span class="rval">([^<]*)</span>', sec[0])]


def _direct(co, value, **kw):
    kw.setdefault("email", "jo@" + co.split()[0].lower() + ".ca")
    return P.Lead(permit_number=f"D-{co}", permit_type="Commercial / Multi Family Project",
                  lead_type="NEEDS_GC", company_name=co, permit_value=value, **kw)


_ord_leads = [
    _named("N-200K", 200000),
    _direct("D-2M", 2013300),
    _named("N-300K", 300000),
    _direct("D-80K", 80527),
    _named("N-TBD", 0),
    _direct("D-79K", 79558),
    _direct("D-TBD", 0),
    _direct("D-54K", 54492),
    _named("N-1.2M", 1200000),
    _direct("D-900K", 900000),
    _direct("D-600K", 600000),
    _direct("D-400K", 400000),
    _direct("D-350K", 350000),
    _direct("D-100K", 100000),
    _named("N-BIG-MAIN", 5000000, contact_name="", email="info@bigmain.ca", phone="(403) 555-0199"),
    P.Lead(permit_number="M-1", permit_type="x", lead_type="NEEDS_GC", company_name="MAIN-700K",
           permit_value=700000, phone="(403) 555-0188"),
    P.Lead(permit_number="M-TBD", permit_type="x", lead_type="NEEDS_GC", company_name="MAIN-TBD",
           permit_value=0, phone="(403) 555-0177"),
    P.Lead(permit_number="M-10K", permit_type="x", lead_type="NEEDS_GC", company_name="MAIN-10K",
           permit_value=10000, phone="(403) 555-0166"),
    P.Lead(permit_number="I-50K", permit_type="x", lead_type="NEEDS_GC", company_name="INBOX-50K",
           permit_value=50000, email="info@inbox50.ca"),
]
_ord_doc = _sheet(_ord_leads)
_ord_cards = _card_values(_ord_doc)

check_eq("ordering fixture renders every lead once",
         len(_cards(_ord_doc)) + len(_rows(_ord_doc)), len(_ord_leads))
check("card values are monotonically non-increasing",
      all(a >= b for a, b in zip(_ord_cards, _ord_cards[1:])))
check_eq("card values are the named+direct tier, highest first, TBD last",
         _ord_cards, [2013300, 1200000, 900000, 600000, 400000, 350000, 300000,
                      200000, 100000, 80527, 79558, 54492, 0, 0])

_strip = _strip_block(_ord_doc)
check_eq("the strip lists the five highest named+direct leads",
         _strip_names(_ord_doc), ["D-2M", "N-1.2M", "D-900K", "D-600K", "D-400K"])
check_eq("the strip omits a lower named+direct lead (D-350K)", "D-350K" in _strip, False)
check_eq("the strip omits the larger mainline lead", "N-BIG-MAIN" in _strip, False)

check_eq("mainline rows are highest first, TBD last",
         _group_values(_ord_doc, "Contact by main line"), [5000000, 700000, 10000, 0])
check_eq("inbox rows are highest first",
         _group_values(_ord_doc, "General inbox only"), [50000])
check("compact rows are monotonically non-increasing within each group",
      all(all(a >= b for a, b in zip(v, v[1:]))
          for v in (_group_values(_ord_doc, "Contact by main line"),
                    _group_values(_ord_doc, "General inbox only"))))

# Same leads in reverse input order must render identically (order comes from value, not input).
check_eq("input order does not change the card order",
         _card_values(_sheet(list(reversed(_ord_leads)))), _ord_cards)

finish()
