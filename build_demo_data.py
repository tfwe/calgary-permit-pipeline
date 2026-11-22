#!/usr/bin/env python3
"""Build a fully synthetic demo of the pipeline's output stage. No network, no secrets.

The company names, people, phone numbers, addresses and emails below are invented.
Phones use the reserved 555-01xx range; emails and websites use example.com.

What it does:
  1. Builds a list of synthetic Lead records (the same model pipeline.py uses).
  2. Runs the REAL split, review and export functions from pipeline.py on them:
       demo_output/calgary_leads_demo.csv        master, every lead
       demo_output/calgary_needs_builder.csv     sellable GC / early-design leads
       demo_output/calgary_subtrade_leads.csv    sellable subtrade leads
       demo_output/calgary_needs_review.csv      not sellable; each row says why
       demo_output/calgary_*.html                the two client sheets
  3. Copies the two sellable client CSVs into fixtures/ as sample_*.csv, the
     committed sample data.
  4. Runs tools/audit_outputs.py against demo_output/ as the QA gate.

Usage:  python build_demo_data.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pipeline as P  # noqa: E402

OUT = ROOT / "demo_output"
FIX = ROOT / "fixtures"

# (lead_type, permit_stage, company, permit_type, value, community, description,
#  phone_suffix or "", email_local or "", website_slug or "", email_status)
# Permit numbers, addresses and dates are generated below from the row index.
ROWS = [
    # --- GC leads: applicant with no contractor of record (NEEDS_GC) -------------------
    ("NEEDS_GC", "", "Northgate Builders Ltd.", "Single Construction Permit", 540000, "Evanston",
     "New Construction, Single Family House · Single-Family Detached · 2,870 sq ft", "01", "info", "northgatebuilders", "mx_ok"),
    ("NEEDS_GC", "", "Ridgeline Homes Inc.", "Single Construction Permit", 615000, "Cranston",
     "New Construction, Single Family House · Single-Family Detached · 3,120 sq ft", "02", "hello", "ridgelinehomes", "mx_ok"),
    ("NEEDS_GC", "", "Maple Hollow Renovations Ltd.", "Single Construction Permit", 128000, "Mahogany",
     "Basement Development, Secondary Suite · 980 sq ft", "03", "office", "maplehollow", "mx_ok"),
    ("NEEDS_GC", "", "Blackfoot Custom Homes", "Single Construction Permit", 890000, "Lake Bonavista",
     "New Construction, Single Family House · Single-Family Detached · 4,050 sq ft", "04", "sales", "blackfootcustom", "mx_ok"),
    ("NEEDS_GC", "", "Westmere Design-Build", "Single Construction Permit", 472000, "Westgate",
     "Addition, Single Family House · Two-storey addition · 640 sq ft", "05", "info", "westmeredb", "unverified"),
    ("NEEDS_GC", "", "Cedar Bluff Builders", "Single Construction Permit", 256000, "Panorama Hills",
     "Porch, Deck and Garage · 720 sq ft", "06", "contact", "cedarbluff", "mx_ok"),
    ("NEEDS_GC", "", "Summit Ridge Developments Ltd.", "Single Construction Permit", 3100000, "Seton",
     "Multi-Family Townhouse Block · 12 units", "07", "info", "summitridgedev", "mx_ok"),
    ("NEEDS_GC", "", "Chinook Ridge Contracting", "Single Construction Permit", 205000, "Silver Springs",
     "Basement Development, Bedroom and Bath · 900 sq ft", "08", "hello", "chinookridge", "mx_ok"),
    ("NEEDS_GC", "", "Juniper Lane Homes", "Single Construction Permit", 349000, "Auburn Bay",
     "New Construction, Semi-Detached · 1,640 sq ft", "09", "info", "juniperlane", "role"),
    ("NEEDS_GC", "", "Redtail Commercial Renovations Ltd.", "Commercial Renovation Permit", 880000, "Ogden",
     "Tenant Improvement, Office · 6,200 sq ft", "10", "info", "redtailcomm", "mx_ok"),
    ("NEEDS_GC", "", "Harrier Modular Homes", "Single Construction Permit", 412000, "Cornerstone",
     "New Construction, Single Family House · 2,260 sq ft", "11", "hello", "harriermodular", "mx_ok"),
    ("NEEDS_GC", "", "Copperfield Custom Builds", "Single Construction Permit", 960000, "Copperfield",
     "New Construction, Single Family House · 4,400 sq ft", "12", "info", "copperfieldbuilds", "mx_ok"),
    ("NEEDS_GC", "", "Kingsmere Homes Corp.", "Single Construction Permit", 330000, "Kincora",
     "Addition, Garage and Mudroom · 520 sq ft", "13", "info", "kingsmerehomes", "mx_ok"),
    ("NEEDS_GC", "", "Prairie Crest Fine Homes", "Single Construction Permit", 705000, "Prairie Winds",
     "New Construction, Single Family House · 3,300 sq ft", "14", "sales", "prairiecrest", "mx_ok"),
    # --- architect / design firms: pitched the bid list, not GC services ----------------
    ("NEEDS_GC", "", "Lantern Architecture Studio", "Single Construction Permit", 0, "Bridgeland",
     "Architectural drawings for a new home", "15", "info", "lanternarch", "mx_ok"),
    ("NEEDS_GC", "", "Halcyon Design Works", "Single Construction Permit", 0, "Hillhurst",
     "Interior and exterior design for a renovation", "16", "info", "halcyondesign", "mx_ok"),
    # --- early design: development permit still at design stage -------------------------
    ("EARLY_DESIGN", "DEVELOPMENT_PERMIT", "Stonebridge Multi-Residential Inc.", "Development Permit", 0,
     "Currie Barracks", "Multi-Family, Mixed-Use, 48 units", "17", "info", "stonebridgemulti", "mx_ok"),
    ("EARLY_DESIGN", "DEVELOPMENT_PERMIT", "Aspen Lane Properties", "Development Permit", 0,
     "Crossfield", "Commercial, Office and Retail, 2 storeys", "18", "hello", "aspenlaneprops", "mx_ok"),
    # --- subtrade pitches: a contractor is already engaged (SUBTRADE_PITCH) -------------
    ("SUBTRADE_PITCH", "", "Bowfront Framing Co.", "Single Construction Permit", 1150000, "Seton",
     "New Construction, Single Family House · 3,900 sq ft", "19", "info", "bowfrontframing", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Aurora Concrete Supply Ltd.", "Single Construction Permit", 294000, "Redstone",
     "New Construction, Single Family House · 2,100 sq ft", "20", "sales", "auroraconcrete", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Foothills Insulation Inc.", "Single Construction Permit", 365000, "Sage Hill",
     "Basement Development, Living Space · 1,020 sq ft", "21", "info", "foothillsinsul", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Lakeview Drywall & Finishing", "Single Construction Permit", 189000, "Lake Bonavista",
     "Basement Development · 880 sq ft", "22", "info", "lakeviewdrywall", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Kestrel Glazing Ltd.", "Commercial Renovation Permit", 720000, "Beltline",
     "Storefront Renovation · 2,400 sq ft", "23", "sales", "kestrelglazing", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Millrace Millwork Co.", "Single Construction Permit", 240000, "Evergreen",
     "Interior Renovation, Kitchen and Built-ins · 600 sq ft", "24", "hello", "millracemillwork", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Sandstone Steel Erectors", "Commercial Renovation Permit", 1480000, "Glenmore",
     "Warehouse Expansion, Steel Frame · 9,000 sq ft", "25", "info", "sandstonesteel", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Riverbend Flooring Supply", "Single Construction Permit", 176000, "Riverbend",
     "Main Floor Renovation · 1,100 sq ft", "26", "info", "riverbendfloor", "mx_ok"),
    ("SUBTRADE_PITCH", "DEVELOPMENT_PERMIT", "Silverpine Civil Works", "Development Permit", 0, "Silverado",
     "Site Servicing for a 24-unit block", "27", "info", "silverpinecivil", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Juniper Roofing Supply", "Single Construction Permit", 268000, "Tuscany",
     "New Construction, Single Family House · 2,500 sq ft", "28", "contact", "juniperroofing", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Glacier Excavation Ltd.", "Single Construction Permit", 415000, "Nolan Hill",
     "New Construction, Single Family House · Site Grading", "29", "info", "glacierexcav", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Cobalt Landscape Supply", "Single Construction Permit", 332000, "Cranston",
     "New Construction, Single Family House · 2,900 sq ft", "30", "sales", "cobaltlandscape", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Trillium Cabinet Works", "Single Construction Permit", 98000, "Tuxedo Park",
     "Kitchen Renovation · 250 sq ft", "31", "info", "trilliumcabinets", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Aspenview Tile & Stone", "Single Construction Permit", 142000, "Hamptons",
     "Bathroom and Basement Finish · 500 sq ft", "32", "sales", "aspenviewtile", "mx_ok"),
    ("SUBTRADE_PITCH", "", "Granite Peak Heating Supply", "Single Construction Permit", 221000, "Mahogany",
     "Basement Development · 1,150 sq ft", "33", "info", "granitepeakheat", "unverified"),
    ("SUBTRADE_PITCH", "", "Eastgate Scaffolding Rentals", "Single Construction Permit", 510000, "Taradale",
     "New Construction, Single Family House · 3,050 sq ft", "34", "hello", "eastgatescaffold", "mx_ok"),
    # --- excluded by policy: a pure trade is never pitched its own trade -----------------
    ("SUBCONTRACTOR", "", "Northwind Electric Ltd.", "Single Construction Permit", 64000, "Franklin",
     "Electrical service upgrade", "35", "info", "northwindelectric", "mx_ok"),
    ("SUBCONTRACTOR", "", "Clearwater Plumbing & Gas", "Single Construction Permit", 88000, "Marlborough",
     "Plumbing rough-in, basement", "36", "info", "clearwaterplumb", "mx_ok"),
    # --- review pile: sellable-looking but no usable channel -----------------------------
    ("NEEDS_GC", "", "Beacon Hill Developments", "Single Construction Permit", 430000, "Beacon Hill",
     "New Construction, Single Family House · 2,700 sq ft", "", "", "", ""),
    ("SUBTRADE_PITCH", "", "Marigold Paving Co.", "Single Construction Permit", 152000, "Dover",
     "Driveway and Walkway Replacement", "", "", "marigoldpaving", "no_mx"),
]

# Website slugs: pure labels, so every URL is example.com and the domain check passes.
STREETS = ["Spruce Glen Rd SW", "Maple Ridge Cres NW", "Aspen Hollow Way NE", "Cedar Meadow Dr SE",
           "Juniper Ridge Blvd NW", "Birch Hill Path SW", "Larch Common NE", "Willow Crest Rd SE"]


def build_leads() -> list[P.Lead]:
    leads: list[P.Lead] = []
    start = date(2026, 11, 9)
    for i, (ltype, stage, company, ptype, value, community, desc, phone_sfx, local, slug, estatus) in enumerate(ROWS):
        email = f"{local}@{slug}.example.com" if local and slug else ""
        website = f"https://www.{slug}.example.com/" if slug else ""
        phone = f"(403) 555-01{phone_sfx}" if phone_sfx else ""
        pitch = P.pitch_for(ltype, stage, company)
        leads.append(P.Lead(
            permit_number=f"DEMO-{2026}-{i + 1:05d}",
            permit_type=ptype,
            permit_stage=stage,
            lead_type=ltype,
            status="Under Review" if i % 3 else "Approved",
            permit_value=float(value),
            company_name=company,
            address=f"{100 + i * 7} {STREETS[i % len(STREETS)]}",
            community=community,
            applied_date=(start + timedelta(days=i % 6)).isoformat(),
            description=desc,
            website=website,
            phone=phone,
            email=email,
            email_status=estatus if email else "",
            pitch_angle=pitch,
            kill_reason="" if (phone or email) else "no contact channel found",
        ))
    return leads


def main() -> int:
    OUT.mkdir(exist_ok=True)
    FIX.mkdir(exist_ok=True)
    leads = build_leads()
    gc, sub = P.filter_client_splits(leads)
    review, excluded = P.select_review(leads, gc, sub)

    P.export_csv(leads, OUT / "calgary_leads_demo.csv")
    P.export_clean_client_csv(gc, OUT / "calgary_needs_builder.csv")
    P.export_clean_client_csv(sub, OUT / "calgary_subtrade_leads.csv")
    P.export_review_csv(review, OUT / "calgary_needs_review.csv")
    P.export_client_html(gc, OUT / "calgary_needs_builder.html", "Calgary Permit Leads (SYNTHETIC DEMO) - Needs Builder / GC")
    P.export_client_html(sub, OUT / "calgary_subtrade_leads.html", "Calgary Permit Leads (SYNTHETIC DEMO) - Subtrade & Supplier")

    shutil.copyfile(OUT / "calgary_needs_builder.csv", FIX / "sample_needs_builder.csv")
    shutil.copyfile(OUT / "calgary_subtrade_leads.csv", FIX / "sample_subtrade_leads.csv")

    print(f"synthetic leads: {len(leads)}  ->  needs_builder={len(gc)} subtrade={len(sub)} "
          f"review={len(review)} policy_excluded={excluded}")
    print(f"wrote {OUT}/ and {FIX}/sample_needs_builder.csv, {FIX}/sample_subtrade_leads.csv")
    audit = subprocess.run([sys.executable, str(ROOT / "tools" / "audit_outputs.py"), "--dir", str(OUT)],
                           capture_output=True, text=True)
    print(audit.stdout.rstrip())
    if audit.returncode:
        print(audit.stderr.rstrip(), file=sys.stderr)
    return audit.returncode


if __name__ == "__main__":
    raise SystemExit(main())
