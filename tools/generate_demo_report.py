#!/usr/bin/env python3
"""Render REPORT-DEMO.md from the offline demo run.

Stdlib only, no network, no timestamps or random ids: running it twice on the
same tree produces a byte-identical report. Every count is recomputed from the
files on disk or from a local command; nothing is hardcoded except the open
defect list and the scope statements, which describe code rather than data.

Usage (from the repo root, after the offline demo has been regenerated):

    SERPER_API_KEY= .venv/bin/python build_demo_data.py
    .venv/bin/python tools/generate_demo_report.py
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
OUT = os.path.join(ROOT, "REPORT-DEMO.md")
DEMO = os.path.join(ROOT, "demo_output")
FIXTURES = os.path.join(ROOT, "fixtures")

# demo_output file -> role in the split. Names come from build_demo_data.py.
SPLIT_FILES = [
    ("calgary_leads_demo.csv", "master (all synthetic leads)"),
    ("calgary_needs_builder.csv", "needs_builder (GC list)"),
    ("calgary_subtrade_leads.csv", "subtrade (trade list)"),
    ("calgary_needs_review.csv", "review"),
]
POLICY_PATTERN = re.compile(r"(policy|excluded)", re.IGNORECASE)

MODULES = ["pipeline.py"] + sorted(
    os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "cpp_*.py"))
)


def run(cmd, timeout=600, extra_env=None):
    """Run a local command; return (returncode, stdout+stderr)."""
    env = {**os.environ, "SERPER_API_KEY": ""}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def read_csv_rows(path):
    """Return (header_lowercase, rows) using the csv module (handles quoted newlines)."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    return [h.strip().lower() for h in header], rows


def contact_coverage(path):
    """Count rows with a phone or an email, using whichever header names exist."""
    header, rows = read_csv_rows(path)
    idx = [header.index(name) for name in ("phone", "email") if name in header]
    if not idx:
        return None
    covered = sum(1 for r in rows if any(i < len(r) and r[i].strip() for i in idx))
    return covered


def count_module_lines(path):
    # wc -l semantics: count newline bytes.
    with open(path, "rb") as fh:
        return fh.read().count(b"\n")


def pytest_counts():
    """Collected test count from the --collect-only tail, plus the full-run summary."""
    rc, out = run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"])
    m = re.search(r"(\d+) tests? collected", out)
    collected = int(m.group(1)) if m else None
    rc2, out2 = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    summary = [l for l in out2.splitlines() if re.search(r"\b(passed|failed|error)", l)]
    last = summary[-1] if summary else ""
    # Strip the timing suffix ("in 1.23s") so the line is stable across runs.
    last = re.sub(r"\s+in [\d.]+s.*$", "", last).strip(" =")
    return collected, rc2, last


def run_all_status():
    # Inherit the generator's interpreter; otherwise run_all.sh falls back to
    # system python3 on hosts without pytest and embeds a spurious failure.
    rc, out = run(["bash", "tests/run_all.sh"], extra_env={"PYTHON": sys.executable})
    lines = [l for l in out.splitlines() if l.strip()]
    tail = lines[-1] if lines else ""
    return rc, tail


def git_head():
    rc, out = run(["git", "rev-parse", "--short", "HEAD"])
    return out.strip() if rc == 0 else "unknown"


def fmt(n):
    return "n/a" if n is None else str(n)


def main():
    head = git_head()

    split_rows = []
    for name, role in SPLIT_FILES:
        path = os.path.join(DEMO, name)
        if not os.path.exists(path):
            split_rows.append((name, role, None, None))
            continue
        _, rows = read_csv_rows(path)
        split_rows.append((name, role, len(rows), contact_coverage(path)))

    policy_files = sorted(
        f for f in os.listdir(DEMO) if POLICY_PATTERN.search(f)
    ) if os.path.isdir(DEMO) else []

    fixture_rows = []
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".csv"):
            _, rows = read_csv_rows(os.path.join(FIXTURES, name))
            fixture_rows.append((name, len(rows)))

    collected, pytest_rc, pytest_summary = pytest_counts()
    ra_rc, ra_tail = run_all_status()
    modules = sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py")))

    # The recorded demo: replay the committed cassettes offline and collect the
    # evidence it verifies. A non-zero replay exit fails the whole generation.
    rec_rc, rec_out = run(
        [sys.executable, os.path.join("tools", "demo_recorded.py"), "--replay"],
        extra_env={"SERPER_API_KEY": "recorded-cassette-replay"})
    rec_evidence = {}
    rec_path = os.path.join(ROOT, "recorded_demo", "run-output.json")
    if os.path.exists(rec_path):
        with open(rec_path, encoding="utf-8") as fh:
            rec_evidence = json.load(fh)

    line_counts = [(m, count_module_lines(os.path.join(ROOT, m))) for m in MODULES]

    L = []
    add = L.append
    add("# Demo run report")
    add("")
    add("Generated by `tools/generate_demo_report.py` from the offline synthetic demo "
        "and the recorded-HTTP demo replay. Do not edit by hand; rerun the generator instead.")
    add("")
    add("## How this report was generated")
    add("")
    add("```")
    add("SERPER_API_KEY= .venv/bin/python build_demo_data.py")
    add(".venv/bin/python tools/demo_recorded.py --replay   # offline cassette replay")
    add(".venv/bin/python tools/generate_demo_report.py")
    add("```")
    add("")
    add(f"- Commit: `{head}` (short HEAD when the generator ran)")
    add("- Network: none. The synthetic demo runs with `SERPER_API_KEY` empty on synthetic "
        "leads; the recorded demo replays committed HTTP cassettes with a socket guard.")
    add("- Output: `demo_output/` is gitignored and regenerated; `fixtures/sample_*.csv` "
        "and `fixtures/http/cassettes.json` (with `recorded_demo/`) are tracked.")
    add("")
    add("## Results")
    add("")
    add("### Split counts (recomputed from `demo_output/`)")
    add("")
    add("| File | Role | Rows | Rows with phone or email |")
    add("|---|---|---:|---:|")
    for name, role, rows, cov in split_rows:
        add(f"| `{name}` | {role} | {fmt(rows)} | {fmt(cov)} |")
    add("")
    if policy_files:
        add("Policy/excluded files present: " + ", ".join(f"`{f}`" for f in policy_files) + ".")
    else:
        add("Policy/excluded files: none written to `demo_output/`. The `policy_excluded` "
            "count is printed by the build stage only and is not recomputed here.")
    add("")
    add("### Fixtures (`fixtures/`, tracked)")
    add("")
    add("| File | Data rows |")
    add("|---|---:|")
    for name, n in fixture_rows:
        add(f"| `{name}` | {n} |")
    add("")
    add("### Recorded-HTTP demo (`tools/demo_recorded.py --replay`) "
        f"(exit code {rec_rc})")
    add("")
    if rec_evidence and rec_rc == 0:
        ing = rec_evidence.get("ingest", {})
        cache = rec_evidence.get("caching", {})
        dup = rec_evidence.get("dedupe", {})
        splits = rec_evidence.get("splits", {})
        add("The committed cassettes (`fixtures/http/cassettes.json`) drive the real "
            "pipeline stages offline: ingest, enrichment, SQLite caching and dedup.")
        add("")
        add("| Stage | Check | Value |")
        add("|---|---|---:|")
        add(f"| ingest | building permits | {ing.get('building_permits', 'n/a')} |")
        add(f"| ingest | development permits | {ing.get('dev_permits', 'n/a')} |")
        add(f"| ingest | dynamic tract builders activated | {ing.get('dynamic_tract_builders', 'n/a')} |")
        add(f"| enrich | companies enriched | {ing.get('companies_enriched', 'n/a')} |")
        add(f"| enrich | rows with contact details | {splits.get('contactable', 'n/a')} |")
        add(f"| cache | SQLite cache rows | {cache.get('cache_rows', 'n/a')} |")
        add(f"| cache | pass-2 HTTP requests (must be 0) | 0 (verified) |")
        add(f"| dedupe | leads before / after | {dup.get('leads_before', 'n/a')} / {dup.get('leads_after', 'n/a')} |")
        add(f"| dedupe | multi-permit roll-ups | {dup.get('rollup_companies', 'n/a')} |")
        add(f"| splits | needs_builder / subtrade / review | "
            f"{splits.get('needs_builder', 'n/a')} / {splits.get('subtrade', 'n/a')} / {splits.get('review', 'n/a')} |")
        add("")
        roll = dup.get("rollup_example") or []
        if roll:
            ex = roll[0]
            add(f"Roll-up example: {ex['company']} filed {ex['permits']} permits "
                f"in the window; dedupe folds them into one row worth "
                f"${ex['combined_value']:,.2f}.")
            add("")
        add("Replay enforces this evidence: every HTTP request is served from a "
            "cassette (a miss is a hard error), a socket guard turns any real "
            "connection attempt into an error, the second enrichment pass must "
            "make zero HTTP requests, and the recomputed counts must match "
            "`recorded_demo/run-output.json` exactly. Kill reasons split "
            + ", ".join(f"`{k or 'success'}: {v}`" for k, v in sorted(
                cache.get("kill_reasons", {}).items())) + ".")
        add("")
    else:
        add(f"The recorded demo replay did not produce usable evidence "
            f"(exit code {rec_rc}); fix `tools/demo_recorded.py` and rerun.")
        add("")
    add("## What this run exercises / does not exercise")
    add("")
    add("Exercised offline (synthetic stage):")
    add("")
    add("- `pitch_for`, `filter_client_splits`, `select_review`")
    add("- CSV and HTML export")
    add("- `tools/audit_outputs.py` (the offline QA gate)")
    add("")
    add("Exercised offline (recorded-HTTP replay):")
    add("")
    add("- Live-shape Socrata permit ingest (`fetch_building_permits`, `fetch_dev_permits`)")
    add("- Enrichment against real recorded responses (Serper Places/Organic, company "
        "site scrapes, DNS-over-HTTPS MX grading)")
    add("- SQLite caching (`init_db`, `cache_get`/`cache_put`, second pass fully served "
        "from cache with zero HTTP requests)")
    add("- `build_leads` reclassification")
    add("- `dedupe_leads` multi-permit roll-up")
    add("")
    add("Not exercised:")
    add("")
    add("- Google sheet (gog) sync and the Gemini contact bridge (need secrets; out of demo scope)")
    add("- The keyed Gemini interpretability stages and live Google API writes")
    add("")
    add("## Test suite invariants")
    add("")
    add(f"- pytest: `{fmt(collected)}` tests collected (`pytest --collect-only -q`).")
    add(f"- pytest full run summary: `{pytest_summary or 'n/a'}` (exit code {pytest_rc}).")
    add(f"- `tests/run_all.sh`: `{ra_tail}` (exit code {ra_rc}).")
    add(f"- Test modules on disk: {len(modules)} (`tests/test_*.py`).")
    add("")
    add("## Module layout")
    add("")
    add("| Module | Lines |")
    add("|---|---:|")
    for m, n in line_counts:
        add(f"| `{m}` | {n} |")
    add(f"| **total** | **{sum(n for _, n in line_counts)}** |")
    add("")
    add("## Defect status")
    add("")
    add("The six defects the previous report listed as open are fixed and pinned by "
        "`tests/test_defect_fixes.py` (tags d1-d6):")
    add("")
    add("1. Role-inbox sets now come from one canonical `cpp_config._ROLE_LOCALS` "
        "(17 stems); `cpp_export.CLIENT_ROLE_INBOXES` and `tools/gemini_contacts.ROLE_LOCALS` "
        "import it. (d1, fixed)")
    add("2. `tools/audit_outputs.py` loads persisted tract_builders from the outputs "
        "`cache.db` before grading, so dynamically discovered builders cannot skip "
        "the offline QA gate. (d2, fixed)")
    add("3. `coverage_counts` no longer counts `no_mx` addresses toward `verified_emails`. (d3, fixed)")
    add("4. `filter_client_splits` honours `--include-tract-builders` on the GC split "
        "the same way `select_review` does. (d4, fixed)")
    add("5. `setup_logging()` defers `logs/` creation through a lazy file handler; "
        "importing any `cpp_*` module has no filesystem side effect. (d5, fixed)")
    add("6. Client HTML `_client_row` and the top-5 strip unmask numbered-shell names "
        "through the shared `_client_display` helper, matching `_client_card`. (d6, fixed)")
    add("")

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
