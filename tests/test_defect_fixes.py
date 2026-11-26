"""The six defects REPORT-DEMO.md listed at pass-6 are closed, and stay closed.

Run: .venv/bin/python -m pytest tests/test_defect_fixes.py -q
No network: everything runs offline against temp dirs and the static blocklists.

Each test is tagged d1..d6 so a regression maps straight back to the report's
defect-status table:

  d1  role-inbox stems drifted across three sites -> one canonical set
  d2  audit gate ignored persisted (dynamically discovered) tract builders
  d3  coverage_counts counted no_mx addresses as verified emails
  d4  --include-tract-builders only reached select_review, not the GC split
  d5  importing any cpp_* module created logs/ as a side effect
  d6  client HTML rows and the top-5 strip leaked raw numbered-shell names
"""
from __future__ import annotations

import csv
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
import pipeline as P  # noqa: E402
import pytest  # noqa: E402
import audit_outputs as A  # noqa: E402
import gemini_contacts as G  # noqa: E402
import cpp_config  # noqa: E402


def lead(permit, company, phone="", email="", status="", website="",
         ltype="SUBTRADE_PITCH", unmasked=""):
    return P.Lead(permit_number=permit, permit_type="Building", lead_type=ltype,
                  company_name=company, phone=phone, email=email, email_status=status,
                  website=website, permit_value=100000.0, unmasked_tradename=unmasked)


# --- d1: one canonical role-inbox stem set ------------------------------------
def test_d1_one_canonical_role_inbox_set():
    assert P._ROLE_LOCALS is cpp_config._ROLE_LOCALS
    assert P.CLIENT_ROLE_INBOXES is cpp_config._ROLE_LOCALS
    assert G.ROLE_LOCALS is cpp_config._ROLE_LOCALS
    assert len(cpp_config._ROLE_LOCALS) == 17
    assert P.email_status("mail@no-such-domain-for-mx-test.invalid") in {"role", "role_unverified"}


# --- d2: audit gate loads persisted tract builders ----------------------------
def write_minimal_outputs(base: Path, builder: str) -> None:
    with (base / "calgary_leads_demo.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company_name", "lead_type"])
        w.writerow([builder, "SUBTRADE_PITCH"])
    with (base / "calgary_subtrade_leads.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Company / Applicant", "Phone", "Email"])
        w.writerow([builder, "4035550100", ""])
    (base / "calgary_needs_builder.csv").write_text("Company / Applicant\n", encoding="utf-8")


def cache_db_with_builder(base: Path, name: str) -> None:
    conn = sqlite3.connect(base / "cache.db")
    conn.execute("CREATE TABLE tract_builders (name TEXT PRIMARY KEY, permits INTEGER, fetched_at TEXT)")
    conn.execute("INSERT INTO tract_builders VALUES (?, 40, '')", (name,))
    conn.commit()
    conn.close()


def test_d2_audit_gate_loads_persisted_tract_builders(capsys):
    builder = "DYNAMIC BUILDS LTD"      # not in the static regex
    assert not P.is_volume_tract_builder(builder)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        write_minimal_outputs(base, builder)
        # No cache.db: no activation, no false failure.
        assert A.main(["--dir", str(base)]) == 0
        cache_db_with_builder(base, builder)
        # With cache.db, main() itself activates the persisted knowledge and
        # fails loudly because the builder sits in a sellable CSV.
        assert A.main(["--dir", str(base)]) == 1
    assert P.is_volume_tract_builder(builder)
    # Once base/ is gone the loader is silent: no cache.db, no activation.
    assert A.activate_persisted_tract_builders(base / "cache.db", 15) == 0
    P._DYNAMIC_TRACT_BUILDERS.clear()   # leave the shared set as the suite found it


def test_d2_missing_cache_db_is_silent():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        write_minimal_outputs(base, "PLAIN BUILD CO")
        assert A.main(["--dir", str(base)]) == 0


# --- d3: no_mx is never a verified email --------------------------------------
def test_d3_no_mx_is_not_a_verified_email():
    counts = P.coverage_counts([lead("P1", "A CO", email="a@b.invalid", status="no_mx")])
    assert counts["verified_emails"] == 0
    assert counts["role_only_emails"] == 0
    counts = P.coverage_counts([lead("P2", "B CO", email="b@c.invalid", status="mx_ok")])
    assert counts["verified_emails"] == 1


# --- d4: --include-tract-builders reads the GC split too ----------------------
def test_d4_include_flag_reaches_the_gc_split():
    tract_builder = lead("P6", "TRICO HOMES", phone="4035550117", ltype="NEEDS_GC")
    gc, _sub = P.filter_client_splits([tract_builder], include_tract_builders=True)
    assert [x.company_name for x in gc] == ["TRICO HOMES"]
    gc0, _ = P.filter_client_splits([tract_builder], include_tract_builders=False)
    assert gc0 == []
    review_in, _excluded = P.select_review([tract_builder], [], [], include_tract_builders=True)
    assert any(x.company_name == "TRICO HOMES" for x in review_in)


# --- d5: importing cpp_config has no filesystem side effect --------------------
def test_d5_import_has_no_filesystem_side_effect():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for src in REPO.glob("*.py"):
            shutil.copy(src, tmp / src.name)
        r = subprocess.run([sys.executable, "-c", "import cpp_config"], cwd=tmp,
                           capture_output=True, text=True)
        assert r.returncode == 0
        assert not (tmp / "logs").exists()
        # The lazy handler opens (and creates logs/) on the first record only.
        r = subprocess.run(
            [sys.executable, "-c",
             "import cpp_config, logging; logging.getLogger('calgary_leads').info('hello')"],
            cwd=tmp, capture_output=True, text=True)
        assert r.returncode == 0
        assert (tmp / "logs" / "pipeline.log").exists()


# --- d6: client HTML unmasks numbered-shell names everywhere -------------------
def test_d6_client_html_unmasks_numbered_shells():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        shell = lead("P9", "123456 ALBERTA LTD", phone="4035550119", ltype="NEEDS_GC",
                     unmasked="Acme Custom Homes")
        out = base / "gc.html"
        P.export_client_html([shell], out, "needs_builder")
        text = out.read_text(encoding="utf-8")
        assert "Acme Custom Homes" in text
        assert "123456 ALBERTA" not in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
