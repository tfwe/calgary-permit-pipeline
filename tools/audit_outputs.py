#!/usr/bin/env python3
"""Check the CSVs a run just produced against the invariants that matter.

This is a post-run QA gate, not a unit test: it reads the real output files and
fails loudly if a lead that should never be sold has leaked into a sellable list.
Run it after pipeline.py (or build_demo_data.py), from the repository root:

    python tools/audit_outputs.py                 # outputs in the repo root
    python tools/audit_outputs.py --dir demo_output

Blocklists come from pipeline.py rather than being copied here, so the checker
cannot drift away from the code it is checking.

A *low* number of sellable leads is deliberately not a problem here: a class can
legitimately be empty, and low yield is the pipeline's own job to catch (it exits
3 when the sellable total falls under --min-client-leads). This gate only fails
on things that must never be true of a list you would send someone.
"""

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pipeline as P  # noqa: E402

def load(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def get(row, *names):
    for n in names:
        if row.get(n):
            return row[n]
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QA gate over the pipeline CSV outputs.")
    parser.add_argument("--dir", default=str(P.BASE),
                        help="directory holding calgary_*.csv (default: repository root)")
    args = parser.parse_args(argv)
    base = Path(args.dir)
    MASTER = base / P.CSV_PATH.name
    SPLITS = {"needs_builder": base / P.GC_CSV_PATH.name, "subtrade": base / P.SUB_CSV_PATH.name}
    REVIEW = base / P.REVIEW_CSV_PATH.name

    master = load(MASTER)
    splits = {k: load(v) for k, v in SPLITS.items()}
    review = load(REVIEW)
    problems = []

    if not master:
        print(f"no master CSV at {MASTER} - run the pipeline first")
        return 1

    # 1. one row per company (dedupe must hold)
    names = [get(r, "company_name").upper() for r in master]
    repeated = {n for n in names if names.count(n) > 1}
    if repeated:
        problems.append(f"master repeats {len(repeated)} company name(s): {sorted(repeated)[:5]}")

    # 2. no junk domain may reach any output
    for label, rows in [("master", master), *splits.items(), ("review", review)]:
        for r in rows:
            url = get(r, "website", "Website")
            if url and P.is_junk(url):
                problems.append(f"{label}: junk URL for {get(r, 'company_name', 'Company / Applicant')}: {url}")

    # 3. volume tract builders must not be pitched subtrade work
    for r in splits["subtrade"]:
        who = get(r, "Company / Applicant")
        if P.is_volume_tract_builder(who):
            problems.append(f"subtrade: volume tract builder {who!r} should have been excluded")

    # 4. deliberate SUBCONTRACTOR rows must not be sold
    not_sold = {get(r, "company_name").upper()
                for r in master if get(r, "lead_type") == "SUBCONTRACTOR"}
    for label, rows in splits.items():
        for r in rows:
            who = get(r, "Company / Applicant").upper()
            if who in not_sold:
                problems.append(f"{label}: {who!r} is a trade and must not be pitched its own trade")

    # 5. every sellable row needs a genuinely usable channel
    for label, rows in splits.items():
        for r in rows:
            phone = get(r, "Phone")
            email = get(r, "Email")
            status = get(r, "Email Status")
            if not phone and not (email and status != "no_mx"):
                problems.append(f"{label}: {get(r, 'Company / Applicant')!r} has no usable contact channel")

    # 6. the review pile is for human follow-up only
    for r in review:
        who = get(r, "Company / Applicant")
        if not get(r, "Why Flagged"):
            problems.append(f"review: {who!r} has no reason recorded")
        if P.is_volume_tract_builder(who):
            problems.append(f"review: {who!r} is a policy exclusion, not a review case")
        if who.upper() in not_sold:
            problems.append(f"review: {who!r} is a policy exclusion, not a review case")

    # Summary, always printed
    print(f"master={len(master)}")
    for label, rows in splits.items():
        contact = sum(1 for r in rows if get(r, "Phone") or get(r, "Email"))
        print(f"{label}={len(rows)} (with phone/email: {contact})")
    print(f"review={len(review)}")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nno problems found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
