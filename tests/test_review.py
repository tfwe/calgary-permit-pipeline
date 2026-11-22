"""The review pile holds leads a human might still sell, and nothing else.

Policy exclusions (trades, volume tract builders) are deliberate, so they must
NOT appear in a pile meant for manual follow-up.
"""

import csv
from pathlib import Path

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, check_eq, finish


def lead(permit, company, phone="", email="", status="", website="", ltype="SUBTRADE_PITCH"):
    return P.Lead(permit_number=permit, permit_type="Building", lead_type=ltype,
                  company_name=company, phone=phone, email=email, email_status=status,
                  website=website, permit_value=100000.0)


sellable = lead("P1", "ALPHA HOMES", phone="4031234567")
dead_email = lead("P2", "BETA BUILDERS", email="x@dead.test", status="no_mx")
no_contact = lead("P3", "GAMMA DEVELOPMENTS")
website_only = lead("P4", "DELTA DESIGN", website="https://delta.ca/")
trade = lead("P5", "EPSILON ELECTRIC", phone="4039998888", ltype="SUBCONTRACTOR")
tract_builder = lead("P6", "TRICO HOMES", phone="4035550117",
                     email="a@trico.ca", status="mx_ok")

leads = [sellable, dead_email, no_contact, website_only, trade, tract_builder]
gc, sub = P.filter_client_splits(leads)
review, excluded = P.select_review(leads, gc, sub)
names = {x.company_name for x in review}

check("a phone lead is sellable", any(x.company_name == "ALPHA HOMES" for x in gc + sub))
check("no-contact lead goes to review", "GAMMA DEVELOPMENTS" in names)
check("dead-email lead goes to review", "BETA BUILDERS" in names)
check("website-only lead goes to review", "DELTA DESIGN" in names)
check("sold leads stay out of review", "ALPHA HOMES" not in names)
check("trades stay out of review", "EPSILON ELECTRIC" not in names)
check("tract builders stay out of review", "TRICO HOMES" not in names)
check_eq("review holds the three unsellable leads", len(review), 3)
check_eq("two leads were excluded by policy", excluded, 2)

review_in, excluded_in = P.select_review(leads, gc, sub, include_tract_builders=True)
check("opting in rescues the tract builder",
      any(x.company_name == "TRICO HOMES" for x in review_in))
check_eq("opting in leaves only the trade excluded", excluded_in, 1)

check_eq("reason: no channel", P.review_reason(no_contact), "no contact channel found")
check("reason: dead MX names the cause", "no MX" in P.review_reason(dead_email))
check("reason: website-only names the cause", "website only" in P.review_reason(website_only))
check("reason: never the vague 'not sellable'", P.review_reason(tract_builder) != "not sellable")

tmp = Path("/tmp/_test_review.csv")
P.export_review_csv(review, tmp)
rows = list(csv.reader(tmp.open()))
check_eq("review CSV has 13 columns", len(rows[0]), 13)
check_eq("review CSV ends with Why Flagged", rows[0][-1], "Why Flagged")
check("review CSV rows match the header width", all(len(r) == 13 for r in rows[1:]))
check_eq("review CSV holds the three rows", len(rows), 4)
tmp.unlink()

finish()
