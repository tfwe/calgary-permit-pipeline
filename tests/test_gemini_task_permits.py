"""Tests for the Community / Est. Value / Scope join in tools/gemini_task_sheet.py.

Run: .venv/bin/python -m pytest tests/test_gemini_task_permits.py -q
Offline: every CSV is written under tmp_path; nothing reads the real lead files
or calls the bridge, Sheets or Gemini.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
import gemini_task_sheet as H  # noqa: E402
import gemini_contacts as G  # noqa: E402

DEMO = ("calgary_leads_demo.csv", {"company": "company_name", "community": "community",
                                   "value": "permit_value", "scope": "description"})
CLIENT = ("calgary_needs_builder.csv", {"company": "Company / Applicant", "community": "Community",
                                        "value": "Est. Project Value", "scope": "Project Description"})
CLIENT_SUB = ("calgary_subtrade_leads.csv", CLIENT[1])

DEMO_HEAD = ["permit_number", "company_name", "permit_value", "community", "description"]
CLIENT_HEAD = ["Permit #", "Company / Applicant", "Est. Project Value", "Community", "Project Description"]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def demo_csv(tmp: Path, rows: list[list[str]]) -> None:
    write_csv(tmp / DEMO[0], DEMO_HEAD, rows)


def client_csv(tmp: Path, rows: list[list[str]], name: str = CLIENT[0]) -> None:
    write_csv(tmp / name, CLIENT_HEAD, rows)


def gap(display: str, company_name: str | None = None) -> dict:
    return {"display": display, "company_name": company_name or display.upper()}


# --- matched row fills all three columns -----------------------------------------

def test_matched_row_fills_community_value_scope(tmp_path):
    demo_csv(tmp_path, [["P1", "CEDARVALE HOMES", "440357.86", "SADDLE RIDGE", "New single family house"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    rows = H.sheet_rows([{**gap("CEDARVALE HOMES"), "address": "117 SPRUCE GLEN MR SW",
                          **H.permit_fields(index, "CEDARVALE HOMES")}])
    assert rows[1][H.COL_COMMUNITY] == "SADDLE RIDGE"
    assert rows[1][H.COL_VALUE] == "$440,358"
    assert rows[1][H.COL_SCOPE] == "New single family house"
    assert rows[1][H.COL_ADDRESS] == "117 SPRUCE GLEN MR SW"


def test_client_csv_headers_fill_columns(tmp_path):
    client_csv(tmp_path, [["P9", "FIELDSTONE CONTRACTING", "$50,000", "BRIDLEWOOD", "Basement dev"]])
    index = H.load_permit_index((CLIENT,), repo=tmp_path)
    assert index["fieldstone contracting"] == {
        "community": "BRIDLEWOOD", "est_value": "$50,000", "scope": "Basement dev"}


def test_unmatched_company_leaves_columns_blank(tmp_path):
    demo_csv(tmp_path, [["P1", "CEDARVALE HOMES", "1", "X", "Y"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    rows = H.sheet_rows([{**gap("NOBODY LTD"), "address": "1 MAIN ST",
                          **H.permit_fields(index, "NOBODY LTD")}])
    assert rows[1][H.COL_COMMUNITY] == rows[1][H.COL_VALUE] == rows[1][H.COL_SCOPE] == ""


# --- highest-value pick among duplicates -----------------------------------------

def test_highest_value_wins_within_one_file(tmp_path):
    demo_csv(tmp_path, [
        ["P1", "GREYSTONE BUILDING DEVELOPERS", "64624.2", "HOMESTEAD", "small"],
        ["P2", "GREYSTONE BUILDING DEVELOPERS", "900000", "MAHOGANY", "big"],
        ["P3", "GREYSTONE BUILDING DEVELOPERS", "120000", "HOMESTEAD", "mid"],
    ])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    assert index["greystone building developers"]["est_value"] == "$900,000"
    assert index["greystone building developers"]["scope"] == "big"


def test_highest_value_wins_across_files(tmp_path):
    demo_csv(tmp_path, [["P1", "SAME CO", "100", "A", "demo scope"]])
    client_csv(tmp_path, [["P2", "Same Co", "$5,000,000", "B", "client scope"]])
    index = H.load_permit_index((DEMO, CLIENT), repo=tmp_path)
    assert index["same co"] == {"community": "B", "est_value": "$5,000,000", "scope": "client scope"}


def test_priced_permit_beats_unpriced_one(tmp_path):
    demo_csv(tmp_path, [["P1", "NOPRICE INC", "", "A", "no value"],
                        ["P2", "NOPRICE INC", "10", "B", "priced"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    assert index["noprice inc"]["scope"] == "priced"


def test_equal_value_tie_prefers_earlier_source(tmp_path):
    demo_csv(tmp_path, [["P1", "TIE LTD", "500", "DEMO AREA", "from demo"]])
    client_csv(tmp_path, [["P2", "TIE LTD", "500", "CLIENT AREA", "from client"]])
    index = H.load_permit_index((DEMO, CLIENT), repo=tmp_path)
    assert index["tie ltd"]["community"] == "DEMO AREA"


# --- missing CSV degrades to blanks ----------------------------------------------

def test_missing_csv_is_skipped_not_an_error(tmp_path):
    client_csv(tmp_path, [["P9", "ONLY CLIENT", "1", "C", "d"]])        # demo CSV absent
    index = H.load_permit_index((DEMO, CLIENT), repo=tmp_path)
    assert "only client" in index


def test_no_csvs_at_all_gives_blank_columns(tmp_path):
    index = H.load_permit_index(repo=tmp_path)                           # empty dir
    assert index == {}
    rows = H.sheet_rows([{**gap("CEDARVALE HOMES"), "address": "", **H.permit_fields(index, "CEDARVALE HOMES")}])
    assert rows[1][H.COL_COMMUNITY:H.COL_SCOPE + 1] == ["", "", ""]


def test_permit_fields_tolerates_none_index_and_blank_names():
    blank = {"community": "", "est_value": "", "scope": ""}
    assert H.permit_fields(None, "CEDARVALE HOMES") == blank
    assert H.permit_fields({}, "", None) == blank


def test_unreadable_csv_is_skipped(tmp_path):
    (tmp_path / DEMO[0]).write_bytes(b"\xff\xfe\x00garbage\x00")          # not valid utf-8
    client_csv(tmp_path, [["P9", "STILL HERE", "1", "C", "d"]])
    index = H.load_permit_index((DEMO, CLIENT), repo=tmp_path)
    assert "still here" in index


def test_rows_without_a_company_name_are_ignored(tmp_path):
    demo_csv(tmp_path, [["P1", "   ", "999", "X", "no name"], ["P2", "", "999", "X", "no name"]])
    assert H.load_permit_index((DEMO,), repo=tmp_path) == {}


# --- case and whitespace insensitivity -------------------------------------------

@pytest.mark.parametrize("csv_name", [
    "Cedarvale Homes",
    "  CEDARVALE   HOMES ",
    "cedarvale\thomes",
    "CEDARVALE HOMES\n",
])
def test_matching_ignores_case_and_whitespace(tmp_path, csv_name):
    demo_csv(tmp_path, [["P1", csv_name, "440357.86", "SADDLE RIDGE", "scope"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    fields = H.permit_fields(index, "CEDARVALE HOMES")
    assert fields["community"] == "SADDLE RIDGE"
    assert fields["est_value"] == "$440,358"


def test_lookup_uses_display_name_then_cache_name(tmp_path):
    demo_csv(tmp_path, [["P1", "UNMASKED TRADE CO", "10", "NW", "s"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    # display (unmasked) misses; the cache company_name hits
    assert H.permit_fields(index, "SHELL 123", "unmasked  trade co")["community"] == "NW"


def test_norm_company_only_folds_case_and_whitespace():
    assert H.norm_company("  Foo   Bar ") == "foo bar"
    assert H.norm_company("Foo, Bar") != H.norm_company("Foo Bar")          # punctuation is significant


def test_parse_value_handles_formats():
    assert H.parse_value("$2,013,300") == 2013300.0
    assert H.parse_value("2500000.0") == 2500000.0
    assert H.parse_value("") is None
    assert H.parse_value("n/a") is None


# --- the dry-run payload carries the join ----------------------------------------

def test_task_plan_joins_permits_into_sheet_rows(tmp_path):
    demo_csv(tmp_path, [["P1", "CEDARVALE HOMES", "440357.86", "SADDLE RIDGE", "New house"]])
    index = H.load_permit_index((DEMO,), repo=tmp_path)
    rows = [gap("CEDARVALE HOMES"), gap("NOBODY LTD")]
    plan = G.task_plan(rows, 15, {"CEDARVALE HOMES": "117 SPRUCE GLEN MR SW"}, index)
    sheet = H.sheet_rows(plan[0]["companies"])
    assert sheet[1][:5] == ["CEDARVALE HOMES", "117 SPRUCE GLEN MR SW", "SADDLE RIDGE", "$440,358", "New house"]
    assert sheet[2][H.COL_COMMUNITY:H.COL_SCOPE + 1] == ["", "", ""]


def test_task_plan_without_permits_keeps_columns_blank():
    plan = G.task_plan([gap("CEDARVALE HOMES")], 15, {})
    assert H.sheet_rows(plan[0]["companies"])[1][H.COL_COMMUNITY:H.COL_SCOPE + 1] == ["", "", ""]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
