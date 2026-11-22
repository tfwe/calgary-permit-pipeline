"""Tests for the opt-in Task enrichment stage and its coverage record in pipeline.py.

Run: .venv/bin/python -m pytest tests/test_via_task_stage.py -q
No network, no Gemini: subprocess.run and refresh_leads_contacts are replaced.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
import pipeline as P  # noqa: E402


def lead(name, email="", status="", contact_name=""):
    return SimpleNamespace(company_name=name, email=email, email_status=status, contact_name=contact_name)


LEADS = [
    lead("A HOMES", "info@a.ca", "mx_ok", "Avery Quill"),       # named, personal-ish address
    lead("B BUILD", "sales@b.ca", "role", ""),               # role-only
    lead("C CUSTOM", "hi@c.ca", "role_unverified", ""),      # role-only
    lead("D DESIGN", "dan@d.ca", "unverified", "Dan Dee"),   # named, verified
    lead("E NONE", "", "", ""),                              # blank
]


@pytest.fixture
def no_db(monkeypatch):
    monkeypatch.setattr(P, "refresh_leads_contacts", lambda conn, leads: 0)
    return sqlite3.connect(":memory:")


def fake_run(stdout: str, rc: int = 0, calls: list | None = None):
    def _run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
    return _run


# --- (d) absent flag and env: behaviour unchanged -------------------------------------

def test_not_requested_by_default():
    assert P.via_task_requested(False, env={}) is False


@pytest.mark.parametrize("env", [{}, {"CALGARY_VIA_TASK": ""}, {"CALGARY_VIA_TASK": "0"},
                                 {"CALGARY_VIA_TASK": "true"}])
def test_env_other_than_1_stays_off(env):
    assert P.via_task_requested(False, env=env) is False


def test_absent_stage_runs_nothing_and_writes_no_coverage(tmp_path, monkeypatch, no_db):
    calls: list = []
    monkeypatch.setattr(P.subprocess, "run", fake_run("", calls=calls))
    path = tmp_path / "coverage-latest.json"
    stage = P.run_via_task_stage(no_db, LEADS, flag=False, env={})
    assert stage is None
    assert P.write_coverage(LEADS, stage, path) is None
    assert calls == [] and not path.exists()


# --- (e) coverage written only when the enrichment runs -------------------------------

def test_enabled_by_env_runs_tool_with_limit_and_writes_coverage(tmp_path, monkeypatch, no_db):
    calls: list = []
    line = ("GEMINI_CONTACTS mode=task selected=4 batches=1 accepted=2 named_person_only=1 "
            "rejected=0 unknown=1 no_line=0 stopped=none")
    monkeypatch.setattr(P.subprocess, "run", fake_run("progress\n" + line + "\n", calls=calls))
    path = tmp_path / "coverage-latest.json"

    stage = P.run_via_task_stage(no_db, LEADS, flag=False, env={"CALGARY_VIA_TASK": "1",
                                                                  "CALGARY_VIA_TASK_LIMIT": "30"})
    assert stage is not None and stage["limit"] == 30 and stage["rc"] == 0
    cmd = calls[0]
    assert cmd[1].endswith("tools/gemini_contacts.py")
    assert "--via-task" in cmd and cmd[cmd.index("--limit") + 1] == "30"
    assert "--max-calls-per-day-task" not in cmd          # the tool's own quota stays in charge

    rec = P.write_coverage(LEADS, stage, path)
    on_disk = json.loads(path.read_text())
    assert on_disk == rec
    assert on_disk["companies"] == 5
    assert on_disk["named_contacts"] == 2
    assert on_disk["verified_emails"] == 2     # info@a.ca (mx_ok), dan@d.ca (unverified)
    assert on_disk["role_only_emails"] == 2    # sales@, hi@
    assert on_disk["blanks"] == 1
    assert on_disk["task"]["accepted"] == 2 and on_disk["task_rc"] == 0


def test_flag_alone_enables_and_default_limit_is_fifteen(monkeypatch, no_db):
    calls: list = []
    monkeypatch.setattr(P.subprocess, "run", fake_run("GEMINI_CONTACTS mode=task selected=0\n", calls=calls))
    stage = P.run_via_task_stage(no_db, LEADS, flag=True, env={})
    assert stage["limit"] == 15
    assert calls[0][calls[0].index("--limit") + 1] == "15"


def test_bad_limit_falls_back_to_default():
    assert P.via_task_limit({"CALGARY_VIA_TASK_LIMIT": "abc"}) == 15
    assert P.via_task_limit({"CALGARY_VIA_TASK_LIMIT": "0"}) == 1


def test_tool_timeout_is_reported_not_raised(monkeypatch, no_db):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(P.subprocess, "run", boom)
    stage = P.run_via_task_stage(no_db, LEADS, flag=True, env={})
    assert stage["rc"] == -1 and stage["task"] == {}


def test_empty_gap_list_skips_subprocess(monkeypatch, no_db):
    calls: list = []
    monkeypatch.setattr(P.subprocess, "run", fake_run("", calls=calls))
    stage = P.run_via_task_stage(no_db, [], flag=True, env={})
    assert calls == [] and stage["task"] == {"selected": 0} and stage["rc"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
