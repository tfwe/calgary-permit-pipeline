"""Tests for tools/gemini_contacts.py and the contact columns in pipeline.py.

Run: .venv/bin/python -m pytest tests/test_gemini_contacts.py -q
No network: fetch_page, domain_has_mx and subprocess.run are monkeypatched.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
import pipeline as P  # noqa: E402
import gemini_contacts as G  # noqa: E402

NAMES = ["NORTHGATE HOMES", "KESTREL ARCHITECTURE", "FIELDSTONE CONTRACTING"]

# The three lines measured from a real Neon reply (see the design notes).
REAL_REPLY = (
    "NORTHGATE HOMES | Dana Whitfield (Owner) | dwhitfield@northgatedevs.ca | "
    "https://www.bbb.org/ca/ab/calgary/profile/general-contractor/northgate-homes-inc-0017-10001 ; "
    "https://unrelated-directory.ca/p/housing-developments\n"
    "KESTREL ARCHITECTURE | Mohammad Katanbaf, Tommy Nham (Partners) | info@kestrel-architecture.ca | "
    "https://www.kestrel-architecture.ca/about-us ; https://www.kestrel-architecture.ca/contact-us\n"
    "FIELDSTONE CONTRACTING | Montee Sutter (Owner) | UNKNOWN | "
    "https://www.bbb.org/ca/ab/calgary/profile/general-contractor/fieldstone-contracting-0017-10002\n"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway cache.db so the real one is never touched."""
    monkeypatch.setattr(P, "DB_PATH", tmp_path / "cache.db")
    conn = P.init_db()
    yield conn
    conn.close()


def _seed(conn, company, **kw):
    rec = {"company_name": company, "website": "", "phone": "", "email": "",
           "unmasked_tradename": "", "licence_type": "", "address": "",
           "google_rating": None, "kill_reason": "", **kw}
    P.cache_put(conn, rec)


# ---------------------------------------------------------------- parser
def test_parser_reads_three_real_reply_lines():
    got = G.parse_reply(REAL_REPLY, NAMES)
    assert set(got) == {0, 1, 2}

    m7 = got[0]
    assert m7["person"] == "Dana Whitfield"          # '(Owner)' annotation removed
    assert m7["email_raw"] == "dwhitfield@northgatedevs.ca"
    assert m7["urls"] == [
        "https://www.bbb.org/ca/ab/calgary/profile/general-contractor/northgate-homes-inc-0017-10001",
        "https://unrelated-directory.ca/p/housing-developments",
    ]  # ' ; ' join split into two URLs

    kn = got[1]
    assert kn["person"] == "Mohammad Katanbaf"  # first person only
    assert kn["person_all"] == "Mohammad Katanbaf, Tommy Nham"  # '(Partners)' removed, full roster kept
    assert kn["email_raw"] == "info@kestrel-architecture.ca"
    assert len(kn["urls"]) == 2

    fb = got[2]
    assert fb["person"] == "Montee Sutter"
    assert fb["email_raw"] == "UNKNOWN"          # literal token, not an address
    assert fb["urls"] == [
        "https://www.bbb.org/ca/ab/calgary/profile/general-contractor/fieldstone-contracting-0017-10002"]


def test_parser_does_not_fabricate_a_line_for_an_omitted_company():
    reply = "\n".join(REAL_REPLY.splitlines()[:1] + REAL_REPLY.splitlines()[2:])  # drop KN
    got = G.parse_reply(reply, NAMES)
    assert set(got) == {0, 2}
    assert 1 not in got
    assert G.decide({"company_name": "KESTREL ARCHITECTURE", "display": "KESTREL ARCHITECTURE"},
                    got.get(1), "h", 1, verify=lambda *a: (True, "x", ""))["status"] == "no_line"


def test_parser_skips_prose_and_unknown_names():
    reply = ("Here are the results I found for your list:\n"
             "ACME WIDGETS | Jane Doe | jane@acme.ca | https://acme.ca\n"
             + REAL_REPLY)
    got = G.parse_reply(reply, NAMES)
    assert set(got) == {0, 1, 2}  # the unknown ACME line is not attached to any batch company


def test_parser_tolerates_markdown_and_numbering():
    reply = "1. **NORTHGATE HOMES** | Dana Whitfield | dwhitfield@northgatedevs.ca | https://unrelated-directory.ca/p/x"
    got = G.parse_reply(reply, NAMES)
    assert got[0]["email_raw"] == "dwhitfield@northgatedevs.ca"


def test_strip_diagnostics_drops_driver_lines_only():
    text = "# bridge: switching model\n# typed 600 chars\nNORTHGATE HOMES | a | b | c\n"
    assert G.strip_diagnostics(text) == "NORTHGATE HOMES | a | b | c"


def test_clean_person_rejects_non_names():
    assert G.clean_person("UNKNOWN") == ""
    assert G.clean_person("someone@x.ca") == ""
    assert G.clean_person("Dana Whitfield (Owner)") == "Dana Whitfield"


# ---------------------------------------------------------------- verification
def _fake_pages(monkeypatch, pages, mx=True):
    """pages: {url: html or Exception}. Every other URL raises (a dead page)."""
    def fake_fetch(url, timeout=8):
        body = pages.get(url)
        if body is None or isinstance(body, Exception):
            raise OSError(f"no page for {url}")
        return body, url
    monkeypatch.setattr(P, "fetch_page", fake_fetch)
    monkeypatch.setattr(P, "domain_has_mx", lambda email: mx)


def test_email_absent_from_cited_page_is_rejected(monkeypatch):
    _fake_pages(monkeypatch, {"https://www.bbb.org/x": "<html><body>Dana Whitfield, Owner</body></html>"})
    ok, source, reason = G.verify_contact("dwhitfield@northgatedevs.ca", ["https://www.bbb.org/x"], "")
    assert (ok, source, reason) == (False, "", "not_on_cited_page")


def test_email_present_on_cited_page_is_accepted(monkeypatch):
    url = "https://www.kestrel-architecture.ca/contact-us"
    _fake_pages(monkeypatch, {url: '<html><a href="mailto:info@kestrel-architecture.ca">Email us</a></html>'})
    ok, source, reason = G.verify_contact("info@kestrel-architecture.ca", [url], "")
    assert (ok, source, reason) == (True, url, "")


def test_match_is_case_insensitive_and_entity_aware(monkeypatch):
    url = "https://www.kestrel-architecture.ca/contact-us"
    _fake_pages(monkeypatch, {url: "<p>INFO&#64;Kestrel-Architecture.ca</p>"})
    ok, _, _ = G.verify_contact("info@kestrel-architecture.ca", [url], "")
    assert ok is True


def test_substring_of_a_longer_address_is_not_a_match(monkeypatch):
    url = "https://www.kestrel-architecture.ca/contact-us"
    _fake_pages(monkeypatch, {url: "<p>xinfo@kestrel-architecture.ca</p>"})
    ok, _, reason = G.verify_contact("info@kestrel-architecture.ca", [url], "")
    assert ok is False and reason == "not_on_cited_page"


def test_own_site_fallback_when_cited_pages_fail(monkeypatch):
    _fake_pages(monkeypatch, {
        "https://fieldstone.example/contact": "<p>Call us. email: estimating@fieldstone.example</p>",
    })
    ok, source, _ = G.verify_contact("estimating@fieldstone.example",
                                     ["https://dead.example/x"], "https://fieldstone.example")
    assert ok is True and source == "own_site"


def test_no_mx_and_bad_syntax_are_rejected_before_any_fetch(monkeypatch):
    calls = []
    monkeypatch.setattr(P, "fetch_page", lambda url, timeout=8: calls.append(url) or ("", url))
    monkeypatch.setattr(P, "domain_has_mx", lambda email: False)
    assert G.verify_contact("a@dead.example", ["https://x.example"], "")[2] == "no_mx"
    assert G.verify_contact("not-an-email", ["https://x.example"], "")[2] == "invalid_syntax"
    assert calls == []


def test_no_cited_url_and_no_site_is_no_url(monkeypatch):
    _fake_pages(monkeypatch, {})
    assert G.verify_contact("a@acme.ca", [], "")[2] == "no_url"


def test_domain_mx_unreachable_is_rejected(monkeypatch):
    _fake_pages(monkeypatch, {}, mx=None)
    assert G.verify_contact("a@acme.ca", ["https://acme.ca"], "")[2] == "mx_unknown"


# ---------------------------------------------------------------- bridge + retry
def test_bridge_nonzero_exit_is_a_failure_not_a_reply(monkeypatch):
    def fake_run(args, **kw):
        assert args[:3] == [G.BRIDGE, "ask", "--model"]
        assert args[3] == "hydrogen" and kw["timeout"] == 600 and kw["capture_output"] is True
        return subprocess.CompletedProcess(args, 1, stdout="# boom\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert G.ask_bridge("p") == (False, "", "exit 1")


def test_bridge_retries_once_then_merges_replies(monkeypatch):
    seq = [
        (False, "", "exit 1"),                                   # first call fails
        (True, "NORTHGATE HOMES | Dana Whitfield | dwhitfield@northgatedevs.ca | https://unrelated-directory.ca", ""),
    ]
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return seq.pop(0)
    parsed, hashes, successes, _ = G.collect_replies("p", NAMES, attempts=2, ask=ask)
    assert len(calls) == 2 and successes == 1
    assert set(parsed) == {0} and 0 in hashes


def test_short_reply_triggers_a_second_call_and_fills_the_gap():
    replies = [
        (True, "NORTHGATE HOMES | Dana Whitfield | UNKNOWN | https://unrelated-directory.ca", ""),
        (True, "KESTREL ARCHITECTURE | Tommy Nham | info@kestrel-architecture.ca | https://kestrel-architecture.ca", ""),
    ]
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return replies.pop(0)
    parsed, _, _, last = G.collect_replies("p", NAMES[:2], attempts=2, ask=ask)
    assert len(calls) == 2 and set(parsed) == {0, 1}


# ---------------------------------------------------------------- resume / no double spend
def test_rerun_does_not_spend_bridge_calls_on_done_companies(db, tmp_path, monkeypatch):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca")
    audit = tmp_path / "gemini_contacts.jsonl"
    _fake_pages(monkeypatch, {"https://unrelated-directory.ca/p/housing-developments": "dwhitfield@northgatedevs.ca here"},
                mx=True)

    asked = []

    def ask(prompt):
        asked.append(prompt)
        return True, REAL_REPLY, ""

    rows = G.select_gaps(db)
    assert {r["company_name"] for r in rows} == {"NORTHGATE HOMES", "KESTREL ARCHITECTURE"}
    G.run(db, rows, batch_size=4, bridge_retry=1, audit_path=audit, ask=ask,
          verify=G.verify_contact, out=lambda *a: None)
    assert len(asked) == 1  # one batch, one call

    # Second run: everything is already in the audit log, so nothing is asked.
    done = G.load_done(audit)
    assert G.select_gaps(db, done=done) == []
    asked.clear()
    G.run(db, G.select_gaps(db, done=done), batch_size=4, bridge_retry=1, audit_path=audit,
          ask=ask, verify=G.verify_contact, out=lambda *a: None)
    assert asked == []


def test_a_verified_contact_is_not_selected_again_even_without_audit(db):
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca", email="info@kestrel-architecture.ca")
    db.execute("UPDATE companies SET contact_name=?, contact_verified=1 WHERE company_name=?",
               ("Tommy Nham", "KESTREL ARCHITECTURE"))
    db.commit()
    assert G.select_gaps(db) == []


def test_bridge_failure_is_recorded_but_not_marked_done(db, tmp_path, monkeypatch):
    _seed(db, "FIELDSTONE CONTRACTING")
    audit = tmp_path / "a.jsonl"
    rows = G.select_gaps(db)
    G.run(db, rows, batch_size=4, bridge_retry=1, audit_path=audit,
          ask=lambda p: (False, "", "exit 1"), out=lambda *a: None)
    recs = [json.loads(x) for x in audit.read_text().splitlines()]
    assert recs[0]["status"] == "bridge_failed"
    assert G.load_done(audit) == set()  # retried on the next run
    assert len(G.select_gaps(db, done=G.load_done(audit))) == 1


def test_redo_ignores_the_audit_log(db, tmp_path):
    _seed(db, "FIELDSTONE CONTRACTING")
    audit = tmp_path / "a.jsonl"
    audit.write_text(json.dumps({"company": "FIELDSTONE CONTRACTING", "status": "unknown"}) + "\n")
    assert G.select_gaps(db, done=G.load_done(audit)) == []
    assert len(G.select_gaps(db, redo=True, done=G.load_done(audit))) == 1


def test_non_gap_companies_are_not_selected(db):
    _seed(db, "ACME DESIGN", website="https://acme.ca", email="sam.lee@acme.ca")
    _seed(db, "BETA BUILD", website="https://beta.ca", email="")
    assert [r["company_name"] for r in G.select_gaps(db)] == ["BETA BUILD"]


# ---------------------------------------------------------------- persistence
def test_accepted_contact_is_written_and_marks_the_row_fresh(db, tmp_path):
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca", email="")
    db.execute("UPDATE companies SET updated_at='2001-01-01T00:00:00+00:00' WHERE company_name='KESTREL ARCHITECTURE'")
    db.commit()
    row = G.select_gaps(db)[0]
    rec = G.decide(row, G.parse_reply(REAL_REPLY, [row["display"]])[0], "h1", 1,
                   verify=lambda e, u, w, *_: (True, "https://kestrel-architecture.ca/contact-us", ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "KESTREL ARCHITECTURE")
    assert got["email"] == "info@kestrel-architecture.ca"          # role email kept (no other email on file)
    assert got["contact_name"] == "Mohammad Katanbaf"
    assert got["contact_verified"] == 1
    assert got["updated_at"] > "2001-01-01"
    assert P.cache_get(db, "KESTREL ARCHITECTURE")["updated_at"][:4] != "2001"


def test_rejected_contact_never_touches_cache(db, tmp_path):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca", email="")
    row = G.select_gaps(db)[0]
    rec = G.decide(row, G.parse_reply(REAL_REPLY, [row["display"]])[0], "h1", 1,
                   verify=lambda e, u, w, *_: (False, "", "not_on_cited_page"))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "NORTHGATE HOMES")
    assert got["email"] == "" and got["contact_verified"] == 0
    audit = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert audit[0]["rejected_reason"] == "not_on_cited_page" and audit[0]["verified"] is False
    assert audit[0]["email"] == "dwhitfield@northgatedevs.ca" and audit[0]["person"] == "Dana Whitfield"


def test_phone_kept_only_for_allowed_area_and_empty_cache(db, tmp_path):
    _seed(db, "FIELDSTONE CONTRACTING", website="https://fieldstone.ca", email="")
    row = G.select_gaps(db)[0]
    reply = "FIELDSTONE CONTRACTING | Montee Sutter | montee@fieldstone.ca | https://fieldstone.ca/about 403-555-0199"
    rec = G.decide(row, G.parse_reply(reply, [row["display"]])[0], "h", 1,
                   verify=lambda e, u, w, *_: (True, "https://fieldstone.ca/about", ""))
    assert rec["phone_kept"] == "(403) 555-0199"

    reply780 = reply.replace("403-555-0199", "780-555-0199")
    rec2 = G.decide(row, G.parse_reply(reply780, [row["display"]])[0], "h", 1,
                    verify=lambda e, u, w, *_: (True, "x", ""))
    assert rec2["phone_kept"] == ""


def test_cache_put_without_contact_keys_preserves_verified_name(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DB_PATH", tmp_path / "c.db")
    conn = P.init_db()
    P.cache_put(conn, {"company_name": "KESTREL ARCHITECTURE", "email": "info@kestrel-architecture.ca",
                       "contact_name": "Tommy Nham", "contact_source": "https://x.ca", "contact_verified": 1})
    P.cache_put(conn, {"company_name": "KESTREL ARCHITECTURE", "email": "info@kestrel-architecture.ca"})
    got = P.cache_get(conn, "KESTREL ARCHITECTURE")
    assert got["contact_name"] == "Tommy Nham" and got["contact_verified"] == 1
    conn.close()


def test_migration_adds_contact_columns_to_an_old_cache_db(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE companies (company_name TEXT PRIMARY KEY, unmasked_tradename TEXT DEFAULT '', "
              "licence_type TEXT DEFAULT '', website TEXT DEFAULT '', phone TEXT DEFAULT '', "
              "email TEXT DEFAULT '', address TEXT DEFAULT '', updated_at TEXT NOT NULL)")
    c.execute("INSERT INTO companies (company_name, updated_at) VALUES ('OLD CO', '2026-01-01T00:00:00+00:00')")
    c.commit()
    c.close()
    monkeypatch.setattr(P, "DB_PATH", old)
    conn = P.init_db()
    conn = P.init_db()  # idempotent: second call must not fail
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    assert {"contact_name", "contact_source", "contact_verified"} <= cols
    assert P.cache_get(conn, "OLD CO")["contact_name"] == ""
    conn.close()


# ---------------------------------------------------------------- outputs
def test_client_csv_has_contact_name_after_company_and_never_none(tmp_path):
    import csv
    lead = P.Lead(permit_number="P1", permit_type="Building", lead_type="NEEDS_GC",
                  company_name="KESTREL ARCHITECTURE", contact_name="", phone="(403) 555-0199",
                  email="info@kestrel-architecture.ca", applied_date="2026-11-01", description="x")
    named = P.Lead(permit_number="P2", permit_type="Building", lead_type="NEEDS_GC",
                   company_name="NORTHGATE HOMES", contact_name="Dana Whitfield", email="dwhitfield@northgatedevs.ca",
                   applied_date="2026-11-01", description="y")
    out = tmp_path / "gc.csv"
    P.export_clean_client_csv([lead, named], out)
    rows = list(csv.reader(out.open(encoding="utf-8")))
    header = rows[0]
    assert header.index("Contact Name") == header.index("Company / Applicant") + 1
    i = header.index("Contact Name")
    assert rows[1][i] == "" and rows[2][i] == "Dana Whitfield"
    assert "None" not in [c for r in rows for c in r]


def test_master_csv_carries_contact_name(tmp_path):
    import csv
    lead = P.Lead(permit_number="P2", permit_type="Building", lead_type="NEEDS_GC",
                  company_name="NORTHGATE HOMES", contact_name="Dana Whitfield", applied_date="2026-11-01")
    out = tmp_path / "master.csv"
    P.export_csv([lead], out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["contact_name"] == "Dana Whitfield"


def test_html_sheet_has_a_contact_cell(tmp_path):
    lead = P.Lead(permit_number="P2", permit_type="Building", lead_type="NEEDS_GC",
                  company_name="NORTHGATE HOMES", contact_name="Dana Whitfield", email="dwhitfield@northgatedevs.ca",
                  applied_date="2026-11-01", description="y", permit_value=100000)
    out = tmp_path / "s.html"
    P.export_client_html([lead], out, "t")
    text = out.read_text(encoding="utf-8")
    assert '<span class="who">Dana Whitfield</span>' in text


# ---------------------------------------------------------------- domain + role rules (corrections)
def test_proof_page_on_a_foreign_domain_is_rejected(monkeypatch):
    """BRIGHTWATER HOMES: the literal sat on a broker's page (brokerlistings.example), not the builder's."""
    url = "https://brokerlistings.example/land/the-ranch/"
    _fake_pages(monkeypatch, {url: "<p>Contact sales@brightwaterhomes.ca for lots</p>"})
    ok, source, reason = G.verify_contact("sales@brightwaterhomes.ca", [url],
                                          "https://www.brightwaterhomes.ca/showhomes/x")
    assert (ok, source, reason) == (False, "", "cross_domain_source")


def test_proof_page_on_the_company_website_domain_is_accepted(monkeypatch):
    url = "https://www.brightwaterhomes.ca/contact"
    _fake_pages(monkeypatch, {url: "<p>sales@brightwaterhomes.ca</p>"})
    ok, source, _ = G.verify_contact("sales@brightwaterhomes.ca", [url],
                                     "https://www.brightwaterhomes.ca/showhomes/x")
    assert ok is True and source == url


def test_proof_page_on_the_email_domain_is_accepted(monkeypatch):
    url = "https://www.northgatedevs.ca/team"
    _fake_pages(monkeypatch, {url: "<p>dwhitfield@northgatedevs.ca</p>"})
    assert G.verify_contact("dwhitfield@northgatedevs.ca", [url], "https://northgatehomes.ca")[0] is True


def test_cross_domain_falls_back_to_own_site(monkeypatch):
    foreign = "https://brokerlistings.example/land/the-ranch/"
    _fake_pages(monkeypatch, {
        foreign: "<p>sales@brightwaterhomes.ca</p>",
        "https://www.brightwaterhomes.ca/contact": "<p>Email sales@brightwaterhomes.ca</p>",
    })
    ok, source, _ = G.verify_contact("sales@brightwaterhomes.ca", [foreign], "https://www.brightwaterhomes.ca/x")
    assert ok is True and source == "own_site"


def test_registrable_domain_ignores_www_and_subdomains():
    assert G.registrable("www.brightwaterhomes.ca") == "brightwaterhomes.ca"
    assert G.registrable("showhomes.brightwaterhomes.ca") == "brightwaterhomes.ca"
    assert G.registrable("brokerlistings.example") != G.registrable("brightwaterhomes.ca")
    assert G.registrable("www.calgary.co.uk") == "calgary.co.uk"


def test_role_email_with_a_person_is_named_person_only(db, tmp_path):
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca", email="")
    row = G.select_gaps(db)[0]
    fields = G.parse_reply(REAL_REPLY, [row["display"]])[0]
    rec = G.decide(row, fields, "h", 1, verify=lambda e, u, w, *_: (True, u[0] if u else "x", ""))
    assert rec["status"] == "named_person_only"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "KESTREL ARCHITECTURE")
    assert got["email"] == "info@kestrel-architecture.ca"       # role inbox kept
    assert got["contact_name"] == "Mohammad Katanbaf"
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert audit["status"] == "named_person_only"


def test_role_inbox_never_overwrites_a_non_empty_cached_email(db, tmp_path):
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca", email="hello@kestrel-architecture.ca")
    row = G.select_gaps(db)[0]
    rec = G.decide(row, G.parse_reply(REAL_REPLY, [row["display"]])[0], "h", 1,
                   verify=lambda e, u, w, *_: (True, "https://kestrel-architecture.ca/contact-us", ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "KESTREL ARCHITECTURE")
    assert got["email"] == "hello@kestrel-architecture.ca"       # untouched
    assert got["contact_name"] == ""                          # name not attached to a different address
    assert json.loads((tmp_path / "a.jsonl").read_text())["rejected_reason"] == "existing_email_kept"


def test_non_role_verified_email_replaces_a_role_inbox(db, tmp_path):
    _seed(db, "BRIGHTWATER HOMES", website="https://www.brightwaterhomes.ca/x", email="info@brightwaterhomes.ca")
    row = G.select_gaps(db)[0]
    reply = "BRIGHTWATER HOMES | Jordan Lowell | jordan@brightwaterhomes.ca | https://www.brightwaterhomes.ca/contact"
    rec = G.decide(row, G.parse_reply(reply, [row["display"]])[0], "h", 1,
                   verify=lambda e, u, w, *_: (True, u[0], ""))
    assert rec["status"] == "accepted"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "BRIGHTWATER HOMES")
    assert got["email"] == "jordan@brightwaterhomes.ca" and got["contact_name"] == "Jordan Lowell"


def test_report_counts_gap_rows_and_audit_statuses(db, tmp_path):
    _seed(db, "NOEMAIL CO", website="https://noemail.ca")
    _seed(db, "ROLE CO", website="https://role.ca", email="info@role.ca")
    _seed(db, "DONE CO", website="https://done.ca", email="sam@done.ca")  # not a gap
    audit = tmp_path / "a.jsonl"
    audit.write_text(json.dumps({"company": "ROLE CO", "status": "rejected",
                                 "rejected_reason": "not_on_cited_page"}) + "\n")
    r = G.report(db, audit)
    assert r["gap_rows"] == 2 and r["no_email"] == 1 and r["role_only"] == 1
    assert r["still_open"] == 2 and r["named_person_only_rows"] == 0
    assert r["rejected_reasons"] == {"not_on_cited_page": 1}


# ---------------------------------------------------------------- name persistence + revert (fix)
def test_name_written_by_tool_survives_cache_get_put_cycle(db, tmp_path):
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca", email="")
    row = G.select_gaps(db)[0]
    rec = G.decide(row, G.parse_reply(REAL_REPLY, [row["display"]])[0], "h", 1,
                   verify=lambda e, u, w, *_: (True, "https://kestrel-architecture.ca/contact-us", ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "KESTREL ARCHITECTURE")
    assert got["contact_name"] == "Mohammad Katanbaf"
    P.cache_put(db, got)                       # full round-trip, contact keys included
    again = P.cache_get(db, "KESTREL ARCHITECTURE")
    assert again["contact_name"] == "Mohammad Katanbaf"
    assert again["contact_source"] == "https://kestrel-architecture.ca/contact-us"
    assert again["contact_verified"] == 1


def test_empty_person_on_a_later_accept_does_not_blank_the_name(db, tmp_path):
    _seed(db, "AIM BUILDER", website="https://ascentbuilders.ca", email="")
    row = G.select_gaps(db)[0]
    first = G.decide(row, {"name_raw": "AIM", "person": "Inderpal Singh", "email_raw": "info@ascentbuilders.ca",
                           "urls": [], "phone": "", "line": ""}, "h1", 1,
                     verify=lambda e, u, w, *_: (True, "https://ascentbuilders.ca/blog", ""))
    G.write_decision(db, first, row, tmp_path / "a.jsonl")
    row2 = G.select_gaps(db, redo=True)[0]
    second = G.decide(row2, {"name_raw": "AIM", "person": "", "email_raw": "info@ascentbuilders.ca",
                             "urls": [], "phone": "", "line": ""}, "h2", 2,
                      verify=lambda e, u, w, *_: (True, "own_site", ""))
    G.write_decision(db, second, row2, tmp_path / "a.jsonl")
    got = P.cache_get(db, "AIM BUILDER")
    assert got["contact_name"] == "Inderpal Singh"
    assert got["email"] == "info@ascentbuilders.ca" and got["contact_verified"] == 1


def test_audit_records_the_pre_tool_email(db, tmp_path):
    _seed(db, "BRIGHTWATER HOMES", website="https://www.brightwaterhomes.ca/x", email="sales@brightwaterhomes.ca")
    row = G.select_gaps(db)[0]
    rec = G.decide(row, G.parse_reply("BRIGHTWATER HOMES | Jordan Lowell | jordan@brightwaterhomes.ca | https://www.brightwaterhomes.ca/c",
                                      [row["display"]])[0], "h", 1, verify=lambda e, u, w, *_: (True, u[0], ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert audit["prev_email"] == "sales@brightwaterhomes.ca" and audit["email_written"] is True


def test_rejection_reverts_an_email_the_tool_wrote_earlier(db, tmp_path):
    # A role inbox stays a gap, so a later --redo pass can reselect the company.
    _seed(db, "FIELDSTONE CONTRACTING", website="https://fieldstone.ca", email="")
    audit = tmp_path / "a.jsonl"
    row = G.select_gaps(db)[0]
    ok = G.decide(row, {"name_raw": "F", "person": "", "email_raw": "info@fieldstone.ca",
                        "urls": [], "phone": "", "line": ""}, "h", 1,
                  verify=lambda e, u, w, *_: (True, "https://fieldstone.ca/about", ""))
    G.write_decision(db, ok, row, audit)
    assert P.cache_get(db, "FIELDSTONE CONTRACTING")["email"] == "info@fieldstone.ca"
    row2 = G.select_gaps(db, redo=True)[0]
    bad = G.decide(row2, {"name_raw": "F", "person": "", "email_raw": "info@fieldstone.ca",
                          "urls": [], "phone": "", "line": ""}, "h2", 2,
                   verify=lambda e, u, w, *_: (False, "", "cross_domain_source"))
    G.write_decision(db, bad, row2, audit)
    got = P.cache_get(db, "FIELDSTONE CONTRACTING")
    assert got["email"] == "" and got["contact_verified"] == 0 and got["contact_name"] == ""
    assert json.loads(audit.read_text().splitlines()[-1])["reverted_email_to"] == ""


# ---------------------------------------------------------------- acceptance rule (company-owned domain)
def test_gmail_address_on_the_companys_own_domain_is_accepted(monkeypatch):
    url = "https://lakeviewdevelopmentsinfo.ca/contact"
    _fake_pages(monkeypatch, {url: "<p>Email us: lakeviewsyntheticbox@gmail.com</p>"})
    ok, source, reason = G.verify_contact("lakeviewsyntheticbox@gmail.com", [url], "", "LAKEVIEW DEVELOPMENTS")
    assert (ok, source, reason) == (True, url, "")


def test_the_same_gmail_page_is_still_rejected_without_the_company_name(monkeypatch):
    url = "https://lakeviewdevelopmentsinfo.ca/contact"
    _fake_pages(monkeypatch, {url: "<p>Email us: lakeviewsyntheticbox@gmail.com</p>"})
    ok, _, reason = G.verify_contact("lakeviewsyntheticbox@gmail.com", [url], "")
    assert ok is False and reason == "cross_domain_source"


@pytest.mark.parametrize("host", ["www.bbb.org", "houzz.com", "www.yelp.com", "www.facebook.com",
                                  "www.instagram.com", "www.linkedin.com"])
def test_broker_and_directory_pages_with_a_literal_match_stay_rejected(monkeypatch, host):
    # The company name appears in the slug on purpose: a directory listing is never the company's own site.
    url = f"https://{host}/ca/ab/calgary/profile/general-contractor/lakeview-developments-inc"
    _fake_pages(monkeypatch, {url: "<p>lakeviewsyntheticbox@gmail.com</p>"})
    ok, _, reason = G.verify_contact("lakeviewsyntheticbox@gmail.com", [url], "", "LAKEVIEW DEVELOPMENTS")
    assert ok is False and reason == "cross_domain_source"


def test_bbb_literal_match_is_rejected_for_northgate(monkeypatch):
    url = "https://www.bbb.org/ca/ab/calgary/profile/general-contractor/northgate-homes-inc-0017-10001"
    _fake_pages(monkeypatch, {url: "<p>dwhitfield@northgatedevs.ca</p>"})
    ok, _, reason = G.verify_contact("dwhitfield@northgatedevs.ca", [url], "", "NORTHGATE HOMES")
    assert (ok, reason) == (False, "cross_domain_source")


# ---------------------------------------------------------------- budget
class _Recorder:
    """Fake bridge: counts calls, asserts nothing, and answers from a per-call function."""

    def __init__(self, reply_fn=None):
        self.calls = []
        self.reply_fn = reply_fn or (lambda prompt: "\n".join(
            f"{n} | - | UNKNOWN | UNKNOWN" for n in re.findall(r"^\d+ (.+)$", prompt, re.M)))

    def __call__(self, prompt, model="hydrogen"):
        self.calls.append((model, prompt))
        return True, self.reply_fn(prompt), ""


def _calls(tmp_path, **limits):
    return G.CallBudget(tmp_path / "calls.json", {"hydrogen": 6, "neon": 2, "escalation": 1, **limits})


def test_budget_refusal_makes_no_bridge_call(db, tmp_path):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    rec = _Recorder()
    counts = G.run(db, G.select_gaps(db), batch_size=4, bridge_retry=1, audit_path=tmp_path / "a.jsonl",
                   ask=rec, out=lambda *a: None, budget=_calls(tmp_path, hydrogen=0), model="hydrogen")
    assert rec.calls == []
    assert counts["budget_stop"] == "hydrogen" and counts["selected"] == 1
    assert not (tmp_path / "a.jsonl").exists()          # nothing decided, nothing written


def test_each_bridge_attempt_is_counted_before_it_is_made(db, tmp_path):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    budget = _calls(tmp_path)
    seen_at_call = []

    def failing(prompt, model="hydrogen"):
        seen_at_call.append(json.loads((tmp_path / "calls.json").read_text())["hydrogen"])
        return False, "", "exit 1"
    G.run(db, G.select_gaps(db), batch_size=4, bridge_retry=1, audit_path=tmp_path / "a.jsonl",
          ask=lambda p: failing(p), out=lambda *a: None, budget=budget, model="hydrogen")
    assert seen_at_call == [1, 2]                       # counted before each of the two attempts
    state = json.loads((tmp_path / "calls.json").read_text())
    assert state["hydrogen"] == 2 and state["neon"] == 0 and state["companies"] == ["NORTHGATE HOMES"]


def test_mid_run_exhaustion_keeps_completed_records(db, tmp_path):
    for name in ("NORTHGATE HOMES", "KESTREL ARCHITECTURE", "FIELDSTONE CONTRACTING"):
        _seed(db, name)
    audit = tmp_path / "a.jsonl"
    rec = _Recorder()
    counts = G.run(db, G.select_gaps(db), batch_size=1, bridge_retry=0, audit_path=audit,
                   ask=rec, out=lambda *a: None, budget=_calls(tmp_path, hydrogen=2), model="hydrogen")
    assert len(rec.calls) == 2 and counts["budget_stop"] == "hydrogen"
    recs = [json.loads(x) for x in audit.read_text().splitlines()]
    assert [r["status"] for r in recs] == ["unknown", "unknown"]     # completed work kept, intact
    assert all(r["model"] == "hydrogen" for r in recs)
    # The third company was never asked, so it is still selectable tomorrow.
    assert len(G.select_gaps(db, done=G.load_done(audit))) == 1


def test_counters_reset_on_a_new_local_day(tmp_path):
    path = tmp_path / "calls.json"
    path.write_text(json.dumps({"date": "2000-01-01", "hydrogen": 6, "neon": 2, "companies": ["X"]}))
    budget = G.CallBudget(path, {"hydrogen": 6, "neon": 2, "escalation": 1}, today="2026-11-21")
    assert budget.reserve("hydrogen", ["Y"]) is None      # not refused: yesterday's count is gone
    state = json.loads(path.read_text())
    assert state["date"] == "2026-11-21" and state["hydrogen"] == 1 and state["companies"] == ["Y"]
    # Same-day exhaustion still refuses.
    budget2 = G.CallBudget(path, {"hydrogen": 1, "neon": 2, "escalation": 1}, today="2026-11-21")
    assert budget2.reserve("hydrogen", ["Z"]) == "hydrogen"


def test_today_local_uses_the_local_calendar_date():
    from datetime import datetime
    assert G.today_local() == datetime.now().astimezone().date().isoformat()


def test_calls_status_prints_pools_and_never_calls_the_bridge(tmp_path, monkeypatch, capsys):
    calls_file = tmp_path / "calls.json"
    G.CallBudget(calls_file, {"hydrogen": 6, "neon": 2, "escalation": 1}).reserve("hydrogen", ["A"])
    def no_bridge(*a, **k):
        raise AssertionError("bridge must not be called by --calls-status")
    monkeypatch.setattr(G, "ask_bridge", no_bridge)
    rc = G.main(["--calls-status", "--calls-file", str(calls_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"date={G.today_local()}" in out
    assert "hydrogen: used=1 remaining=5" in out and "neon: used=0 remaining=2" in out
    assert "escalation: used=0 remaining=1" in out


def test_main_refuses_cleanly_when_the_hydrogen_pool_is_zero(db, tmp_path, monkeypatch, capsys):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    rec = _Recorder()
    monkeypatch.setattr(G, "ask_bridge", rec)
    rc = G.main(["--quiet", "--audit-log", str(tmp_path / "a.jsonl"), "--calls-file", str(tmp_path / "c.json"),
                 "--max-calls-per-day-hydrogen", "0"])
    assert rc == 0 and rec.calls == []
    assert "hydrogen pool spent" in capsys.readouterr().out


# ---------------------------------------------------------------- escalation
def _bridge_by_model(monkeypatch, hydrogen_fn, neon_fn=None):
    rec = _Recorder(hydrogen_fn)
    neon = _Recorder(neon_fn or (lambda p: "\n".join(
        f"{n} | - | UNKNOWN | UNKNOWN" for n in re.findall(r"^\d+ (.+)$", p, re.M))))

    def fake(prompt, model="hydrogen"):
        return (rec if model == "hydrogen" else neon)(prompt, model=model)
    monkeypatch.setattr(G, "ask_bridge", fake)
    return rec, neon


def test_escalate_retries_only_hydrogen_unknown_companies_on_neon(db, tmp_path, monkeypatch):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    _seed(db, "KESTREL ARCHITECTURE", website="https://kestrel-architecture.ca")
    _seed(db, "FIELDSTONE CONTRACTING")
    _fake_pages(monkeypatch, {
        "https://northgatedevs.ca/team": "<p>dwhitfield@northgatedevs.ca</p>",
        "https://www.kestrel-architecture.ca/contact-us": "<p>info@kestrel-architecture.ca</p>",
    })

    def hydrogen(prompt):
        return ("NORTHGATE HOMES | Dana Whitfield | dwhitfield@northgatedevs.ca | https://northgatedevs.ca/team\n"
                "KESTREL ARCHITECTURE | - | UNKNOWN | UNKNOWN\n"
                "FIELDSTONE CONTRACTING | - | UNKNOWN | UNKNOWN")

    def neon(prompt):
        return ("KESTREL ARCHITECTURE | Tommy Nham | info@kestrel-architecture.ca | "
                "https://www.kestrel-architecture.ca/contact-us\n"
                "FIELDSTONE CONTRACTING | - | UNKNOWN | UNKNOWN")
    h, n = _bridge_by_model(monkeypatch, hydrogen, neon)
    audit, calls = tmp_path / "a.jsonl", tmp_path / "c.json"
    rc = G.main(["--escalate", "--quiet", "--audit-log", str(audit), "--calls-file", str(calls)])
    assert rc == 0
    assert [m for m, _ in h.calls] == ["hydrogen"] and [m for m, _ in n.calls] == ["neon"]
    neon_names = set(re.findall(r"^\d+ (.+)$", n.calls[0][1], re.M))
    assert neon_names == {"KESTREL ARCHITECTURE", "FIELDSTONE CONTRACTING"}      # first candidate accepted by the cheaper model: not sent
    recs = [json.loads(x) for x in audit.read_text().splitlines()]
    by = {(r["company"], r["model"], r["escalated"]) for r in recs}
    assert ("NORTHGATE HOMES", "hydrogen", False) in by
    assert ("KESTREL ARCHITECTURE", "hydrogen", False) in by and ("KESTREL ARCHITECTURE", "neon", True) in by
    assert all(r["model"] in ("hydrogen", "neon") for r in recs)
    assert P.cache_get(db, "KESTREL ARCHITECTURE")["email"] == "info@kestrel-architecture.ca"


def test_escalate_never_sends_a_hydrogen_accepted_company_to_neon(db, tmp_path, monkeypatch):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca")
    _fake_pages(monkeypatch, {"https://northgatedevs.ca/team": "<p>dwhitfield@northgatedevs.ca</p>"})
    h, n = _bridge_by_model(monkeypatch, lambda p: "NORTHGATE HOMES | Dana Whitfield | dwhitfield@northgatedevs.ca | https://northgatedevs.ca/team")
    rc = G.main(["--escalate", "--quiet", "--audit-log", str(tmp_path / "a.jsonl"),
                 "--calls-file", str(tmp_path / "c.json")])
    assert rc == 0 and n.calls == []                       # nothing unresolved, so Neon is never asked
    assert P.cache_get(db, "NORTHGATE HOMES")["email"] == "dwhitfield@northgatedevs.ca"


def test_escalation_uses_its_own_cap_and_refuses_cleanly(db, tmp_path, monkeypatch, capsys):
    _seed(db, "FIELDSTONE CONTRACTING")
    h, n = _bridge_by_model(monkeypatch, lambda p: "FIELDSTONE CONTRACTING | - | UNKNOWN | UNKNOWN")
    rc = G.main(["--escalate", "--audit-log", str(tmp_path / "a.jsonl"),
                 "--calls-file", str(tmp_path / "c.json"), "--escalate-calls-per-day", "0"])
    out = capsys.readouterr().out
    assert rc == 0 and [m for m, _ in h.calls] == ["hydrogen"] and n.calls == []
    assert "escalation pool spent" in out


def test_escalation_candidates_exclude_hydrogen_verified_and_resolved(tmp_path):
    audit = tmp_path / "a.jsonl"
    audit.write_text(json.dumps({"company": "OLD CO", "status": "accepted", "model": "hydrogen"}) + "\n")
    rows = {k: {"company_name": k} for k in ("OLD CO", "UNK CO", "REJ CO", "VER CO")}
    records = [
        {"company": "UNK CO", "status": "unknown", "model": "hydrogen", "verified": False},
        {"company": "REJ CO", "status": "rejected", "model": "hydrogen", "verified": False},
        {"company": "VER CO", "status": "rejected", "model": "hydrogen", "verified": True},
        {"company": "OLD CO", "status": "rejected", "model": "hydrogen", "verified": False},
        {"company": "UNK CO", "status": "unknown", "model": "neon", "verified": False},
    ]
    got = [r["company_name"] for r in G.escalation_candidates(records, rows, audit)]
    assert got == ["UNK CO", "REJ CO"]


def test_pipeline_pass_through_builds_the_budget_flags(monkeypatch, tmp_path):
    import pipeline as PL
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="GEMINI_CONTACTS selected=0\n", stderr="")
    monkeypatch.setattr(PL.subprocess, "run", fake_run)
    PL.run_gemini_contacts(["NORTHGATE HOMES"], model="hydrogen", max_calls_hydrogen=3,
                           max_calls_neon=1, max_calls_escalate=1)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "hydrogen"
    assert cmd[cmd.index("--max-calls-per-day-hydrogen") + 1] == "3"
    assert cmd[cmd.index("--max-calls-per-day-neon") + 1] == "1"
    assert cmd[cmd.index("--escalate-calls-per-day") + 1] == "1"


# ---------------------------------------------------------------- single-person contact_name
MERIDIAN_ROSTER = ("Mike Miller / Suzanne Facca / Danielle Callan / Michelle Ewing / "
                "Airyn Collier / Mitchell Martens / Cristi Louw")


@pytest.mark.parametrize("raw, first, full", [
    (MERIDIAN_ROSTER, "Mike Miller", MERIDIAN_ROSTER),                      # slash roster
    ("Tom Lee, Ann Lee", "Tom Lee", "Tom Lee, Ann Lee"),              # comma list
    ("Bob Ng and Al Ng", "Bob Ng", "Bob Ng and Al Ng"),               # ' and '
    ("Jo Park & Sam Park", "Jo Park", "Jo Park & Sam Park"),          # '&'
    ("Dana Whitfield", "Dana Whitfield", "Dana Whitfield"),                       # single name unchanged
    ("Dana Whitfield (Owner)", "Dana Whitfield", "Dana Whitfield"),               # Owner-annotated single name
])
def test_person_is_the_first_name_and_person_all_keeps_the_roster(raw, first, full):
    reply = f"NORTHGATE HOMES | {raw} | dwhitfield@northgatedevs.ca | https://northgatedevs.ca/team"
    got = G.parse_reply(reply, NAMES)[0]
    assert got["person"] == first
    assert got["person_all"] == full


def test_slash_roster_is_trimmed_in_cache_and_full_in_audit(db, tmp_path):
    _seed(db, "MERIDIAN ARCHITECTURE", website="https://meridian.ca", email="")
    row = G.select_gaps(db)[0]
    reply = f"MERIDIAN ARCHITECTURE | {MERIDIAN_ROSTER} | mike@meridian.ca | https://meridian.ca/team"
    fields = G.parse_reply(reply, [row["display"]])[0]
    rec = G.decide(row, fields, "h", 1, verify=lambda e, u, w, *_: (True, u[0], ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "MERIDIAN ARCHITECTURE")["contact_name"] == "Mike Miller"
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert audit["person"] == "Mike Miller"
    assert audit["person_all"] == MERIDIAN_ROSTER


def test_single_name_reply_is_stored_unchanged(db, tmp_path):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca", email="")
    row = G.select_gaps(db)[0]
    reply = "NORTHGATE HOMES | Dana Whitfield (Owner) | dwhitfield@northgatedevs.ca | https://northgatedevs.ca/team"
    fields = G.parse_reply(reply, [row["display"]])[0]
    rec = G.decide(row, fields, "h", 1, verify=lambda e, u, w, *_: (True, u[0], ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "NORTHGATE HOMES")["contact_name"] == "Dana Whitfield"
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert audit["person"] == "Dana Whitfield" and audit["person_all"] == "Dana Whitfield"


# ---------------------------------------------------------------- --via-task (offline)
import gemini_task_sheet as H  # noqa: E402

TASK_SID = "SIDabcdefghij1234"
TASK_JOB = "20261121T120000Z-abc123"
KN_PAGE = "https://www.kestrel-architecture.ca/contact-us"


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class _FakeSys:
    """Stands in for subprocess.run. Records every argv; answers by command."""

    def __init__(self, sheet_values=None, *, quota_rc=0, trigger_rc=0, await_rc=0, job=TASK_JOB):
        self.calls: list[list[str]] = []
        self.sheet_values = sheet_values
        self.quota_rc, self.trigger_rc, self.await_rc, self.job = quota_rc, trigger_rc, await_rc, job

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == H.QUOTA_PY:
            if argv[1] == "check":
                return _Proc(self.quota_rc, "limit reached; wait 3600s" if self.quota_rc else "", "")
            return _Proc(0)
        if argv[0] == "python3" and argv[1] == H.TOKEN_PY:
            return _Proc(0, "tok-123\n")
        if argv[0] == H.TRIGGER_PY:
            return _Proc(self.trigger_rc, "", "" if self.trigger_rc == 0 else "phone busy")
        if argv[0] == H.JOB_PY:
            if argv[1] == "submit":
                return _Proc(0, f"{self.job}\n")
            if argv[1] == "await":
                return _Proc(self.await_rc, "" if self.await_rc == 0 else "", "timed out" if self.await_rc == 6 else "")
            return _Proc(0)
        if argv[0] == H.GWS:
            sub = argv[1:3]
            if sub == ["sheets", "create"]:
                return _Proc(0, json.dumps({"spreadsheetId": TASK_SID}))
            if sub == ["sheets", "append"]:
                return _Proc(0, "{}")
            if sub == ["sheets", "get"]:
                return _Proc(0, json.dumps(self.sheet_values if self.sheet_values is not None else []))
            return _Proc(0, "{}")
        raise AssertionError(f"unexpected command {argv}")

    def invoked(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def _task_rows(filled):
    """Header + the three rows the model would have written. `filled` maps company -> (person, email, url, status)."""
    rows = [list(H.HEADERS)]
    for company, (person, email, url, status) in filled.items():
        r = [company, "", "", "", "", person, email, url, status]
        rows.append(r)
    return rows


def _three(db):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca", email="")
    _seed(db, "KESTREL ARCHITECTURE", website="https://www.kestrel-architecture.ca", email="")
    _seed(db, "FIELDSTONE CONTRACTING", website="https://fieldstone.ca", email="")


GOOD_FILL = {
    "KESTREL ARCHITECTURE": ("Mohammad Katanbaf", "info@kestrel-architecture.ca", KN_PAGE, "found"),
    "NORTHGATE HOMES": ("Dana Whitfield", "dwhitfield@northgatedevs.ca", "https://www.bbb.org/x", "found"),
    "FIELDSTONE CONTRACTING": ("", "none published", "", "none_published"),
}


def _audit(tmp_path):
    p = tmp_path / "a.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def _run_task(db, tmp_path, fake, *, budget=None, keep=True, timeout=1500, batch=15):
    rows = G.select_gaps(db, done=G.load_done(tmp_path / "a.jsonl"))
    budget = budget or _calls(tmp_path, task=2)
    return G.run_task(db, rows, batch_size=batch, task_timeout=timeout,
                      audit_path=tmp_path / "a.jsonl", budget=budget, keep_sheets=keep,
                      out=lambda *a, **k: None)


def test_task_round_trip_parses_into_accept_reject_and_unknown(db, tmp_path, monkeypatch):
    _three(db)
    _fake_pages(monkeypatch, {KN_PAGE: '<a href="mailto:info@kestrel-architecture.ca">mail</a>',
                              "https://www.bbb.org/x": "<html>Dana Whitfield, Owner</html>"})
    fake = _FakeSys(_task_rows(GOOD_FILL))
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake)
    assert counts["stopped"] == "none" and counts["batches"] == 1
    # info@ with a named person is the existing named_person_only class: still verified and written
    assert (counts["accepted"] + counts["named_person_only"], counts["rejected"], counts["unknown"]) == (1, 1, 1)
    assert P.cache_get(db, "KESTREL ARCHITECTURE")["email"] == "info@kestrel-architecture.ca"
    assert P.cache_get(db, "NORTHGATE HOMES")["email"] == ""          # not on its cited page
    assert P.cache_get(db, "FIELDSTONE CONTRACTING")["email"] == ""
    # the sheet was created with the proven command shape and the header + 3 rows were appended
    create = fake.invoked(H.GWS, "sheets", "create")[0]
    assert create[create.index("--title") + 1].startswith("leads-") and "--sheet-name" in create
    append = fake.invoked(H.GWS, "sheets", "append")[0]
    sent = json.loads(append[append.index("--values") + 1])
    assert sent[0] == H.HEADERS and len(sent) == 4
    assert sorted(r[0] for r in sent[1:]) == sorted(["KESTREL ARCHITECTURE", "NORTHGATE HOMES", "FIELDSTONE CONTRACTING"])
    audit = _audit(tmp_path)
    assert len(audit) == 3 and all(r["mode"] == "task" for r in audit)
    assert all(r["sheet_url"].endswith(f"{TASK_SID}/edit") and r["job_id"] == TASK_JOB for r in audit)
    assert all(len(r["row_hash"]) == 16 and r["model_reply_hash"] == "" for r in audit)
    assert len(fake.invoked(H.TRIGGER_PY)) == 1 and len(fake.invoked(H.JOB_PY, "await")) == 1


def test_none_published_gives_no_email_and_no_rejection(db, tmp_path, monkeypatch):
    _seed(db, "FIELDSTONE CONTRACTING", website="https://fieldstone.ca", email="")
    row = G.select_gaps(db)[0]
    values = _task_rows({"FIELDSTONE CONTRACTING": ("", "none published", "", "none_published")})
    out = G.task_decisions([row], values, batch_no=1, job_id=TASK_JOB, sheet_url="u",
                           verify=lambda *a: (_ for _ in ()).throw(AssertionError("must not verify")))
    _, rec = out[0]
    assert rec["status"] == "unknown" and rec["email"] == "" and rec["rejected_reason"] == "no_email"
    assert rec["status"] != "rejected"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "FIELDSTONE CONTRACTING")["email"] == ""
    assert [r["status"] for r in _audit(tmp_path)] == ["unknown"]


def test_returned_email_failing_page_verification_is_never_cached(db, tmp_path, monkeypatch):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca", email="")
    _fake_pages(monkeypatch, {"https://www.bbb.org/x": "<html>nothing here</html>"})
    fake = _FakeSys(_task_rows({"NORTHGATE HOMES": ("Dana Whitfield", "dwhitfield@northgatedevs.ca", "https://www.bbb.org/x", "found")}))
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake)
    assert counts["rejected"] == 1 and counts["accepted"] == 0
    assert P.cache_get(db, "NORTHGATE HOMES")["email"] == ""
    rec = _audit(tmp_path)[0]
    assert rec["status"] == "rejected" and rec["rejected_reason"] == "not_on_cited_page"


def test_junk_source_url_falls_back_to_company_site(db, tmp_path, monkeypatch):
    _seed(db, "KESTREL ARCHITECTURE", website="https://www.kestrel-architecture.ca", email="")
    row = G.select_gaps(db)[0]
    site_page = G.own_site_urls("https://www.kestrel-architecture.ca")[0]
    _fake_pages(monkeypatch, {site_page: '<a href="mailto:info@kestrel-architecture.ca">x</a>'})
    values = _task_rows({"KESTREL ARCHITECTURE": ("", "info@kestrel-architecture.ca", "see the website", "found")})
    _, rec = G.task_decisions([row], values, batch_no=1, job_id=TASK_JOB, sheet_url="u")[0]
    assert rec["verified"] is True and rec["status"] == "accepted"
    assert rec["email_source"] == "own_site"


def test_quota_refusal_aborts_before_any_submit(db, tmp_path, monkeypatch):
    _three(db)
    fake = _FakeSys(quota_rc=5)
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake)
    assert counts["stopped"] == "quota"
    assert [c[:2] for c in fake.calls] == [[H.QUOTA_PY, "check"]]     # nothing else ran
    assert not (tmp_path / "calls.json").exists()                        # no budget charge either
    assert _audit(tmp_path) == []


def test_task_budget_refusal_creates_no_sheet(db, tmp_path, monkeypatch):
    _three(db)
    fake = _FakeSys(_task_rows(GOOD_FILL))
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake, budget=_calls(tmp_path, task=0))
    assert counts["stopped"] == "budget:task"
    assert fake.invoked(H.GWS, "sheets", "create") == [] and fake.invoked(H.TRIGGER_PY) == []


def test_timeout_leaves_no_partial_writes(db, tmp_path, monkeypatch):
    _three(db)
    fake = _FakeSys(_task_rows(GOOD_FILL), await_rc=6)
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake, timeout=30)
    assert counts["stopped"].startswith("task_failed") and counts["batches"] == 0
    assert _audit(tmp_path) == []                                        # no audit line
    assert all(P.cache_get(db, n)["email"] == "" for n in ("NORTHGATE HOMES", "KESTREL ARCHITECTURE", "FIELDSTONE CONTRACTING"))
    assert fake.invoked(H.GWS, "sheets", "get") == []                   # never read back, never written
    await_argv = fake.invoked(H.JOB_PY, "await")[0]
    assert await_argv[await_argv.index("--timeout") + 1] == "30"


def test_resume_skips_companies_already_in_the_audit_log(db, tmp_path, monkeypatch):
    _three(db)
    audit = tmp_path / "a.jsonl"
    audit.write_text(json.dumps({"company": "KESTREL ARCHITECTURE", "status": "accepted"}) + "\n")
    fake = _FakeSys(_task_rows({k: GOOD_FILL[k] for k in ("NORTHGATE HOMES", "FIELDSTONE CONTRACTING")}))
    monkeypatch.setattr(H.subprocess, "run", fake)
    _fake_pages(monkeypatch, {"https://www.bbb.org/x": "<html>nothing</html>"})
    _run_task(db, tmp_path, fake)
    sent = json.loads(fake.invoked(H.GWS, "sheets", "append")[0][-1])
    assert sorted(r[0] for r in sent[1:]) == ["FIELDSTONE CONTRACTING", "NORTHGATE HOMES"]   # KN is not re-sent


def test_batches_of_fifteen_and_a_rerun_only_takes_what_is_left(db, tmp_path, monkeypatch):
    names = [f"ACME {i:02d} LTD" for i in range(16)]
    for n in names:
        _seed(db, n, website=f"https://{n.lower().replace(' ', '')}.ca", email="")
    fake = _FakeSys(_task_rows({}))
    monkeypatch.setattr(H.subprocess, "run", fake)
    counts = _run_task(db, tmp_path, fake, budget=_calls(tmp_path, task=5), batch=15)
    appends = fake.invoked(H.GWS, "sheets", "append")
    assert [len(json.loads(a[-1])) - 1 for a in appends] == [15, 1]
    assert counts["stopped"] == "none" and counts["batches"] == 2 and counts["selected"] == 16


def test_dry_run_makes_no_call_and_prints_the_plan(db, tmp_path, monkeypatch, capsys):
    for i in range(15):
        _seed(db, f"ACME {i:02d} LTD", website=f"https://acme{i}.ca", email="")
    def boom(*a, **k):
        raise AssertionError("dry run must not call anything")
    monkeypatch.setattr(H.subprocess, "run", boom)
    rc = G.main(["--via-task", "--no-current-only", "--dry-run", "--calls-file", str(tmp_path / "calls.json"),
                 "--audit-log", str(tmp_path / "a.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and "--via-task DRY RUN: 15 gap companies in 1 batch" in out
    assert json.dumps(H.HEADERS, ensure_ascii=False) in out
    assert "ACME 00 LTD" in out and "ACME 14 LTD" in out
    assert "Column G (Email)" in out and "none published" in out and "never guess" in out
    assert "gemini-task-trigger.py" in out and "gemini-quota.py check" in out
    assert not (tmp_path / "calls.json").exists()
    assert not (tmp_path / "a.jsonl").exists()


def test_task_flags_default_and_bounds(monkeypatch, tmp_path):
    seen = {}
    def fake_main_task(args, budget, audit_path, names):
        seen.update(batch=args.task_batch, timeout=args.task_timeout, keep=args.task_keep_sheets,
                    cap=args.max_calls_per_day_task)
        return 0
    monkeypatch.setattr(G, "_main_task", fake_main_task)
    G.main(["--via-task", "--calls-file", str(tmp_path / "c.json")])
    assert seen == {"batch": 15, "timeout": 1500, "keep": True, "cap": 2}
    G.main(["--via-task", "--no-task-keep-sheets", "--calls-file", str(tmp_path / "c.json")])
    assert seen["keep"] is False
    with pytest.raises(SystemExit):
        G.main(["--via-task", "--task-batch", "16"])
    with pytest.raises(SystemExit):
        G.main(["--via-task", "--escalate"])


def test_chat_path_is_still_the_default(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(G, "_main_task", lambda *a, **k: called.append(1) or 0)
    monkeypatch.setattr(G, "ask_bridge", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no bridge")))
    G.main(["--calls-status", "--calls-file", str(tmp_path / "c.json")])
    assert called == []


def test_task_pool_is_counted_in_the_calls_file(tmp_path):
    b = G.CallBudget(tmp_path / "c.json", {"task": 2})
    assert b.reserve("task", ["A"]) is None and b.reserve("task", ["B"]) is None
    assert b.reserve("task", ["C"]) == "task"
    st = json.loads((tmp_path / "c.json").read_text())
    assert st["task"] == 2 and st["hydrogen"] == 0 and st["companies"] == ["A", "B"]


def test_sheet_payload_and_urls():
    rows = H.sheet_rows([{"display": "KESTREL ARCHITECTURE", "address": "1 Main St"}])
    assert rows[0] == H.HEADERS and rows[1][:2] == ["KESTREL ARCHITECTURE", "1 Main St"]
    assert H.urls_in("see https://a.ca/x; junk") == ["https://a.ca/x"]
    assert H.urls_in("not a url") == [] and H.urls_in("") == []
    assert re.match(r"^leads-\d{8}T\d{6}Z$", H.sheet_title())
    assert H.parse_values('{"values": [["a"]]}') == [["a"]]
    with pytest.raises(H.TaskError):
        H.parse_create('{"nope": 1}')


# ---------------------------------------------------------------- roster ';' and unverified person
def test_semicolon_roster_reduces_to_the_first_person():
    assert G.first_person("Chris Hale ; Pat Ellison") == "Chris Hale"
    got = G.parse_reply("CEDARVALE HOMES | Chris Hale ; Pat Ellison | info@cedarvale.ca | https://cedarvale.ca/team",
                        ["CEDARVALE HOMES"])[0]
    assert got["person"] == "Chris Hale" and got["person_all"] == "Chris Hale ; Pat Ellison"


def test_semicolon_roster_is_stored_as_one_name(db, tmp_path):
    _seed(db, "CEDARVALE HOMES", website="https://cedarvale.ca", email="")
    row = G.select_gaps(db)[0]
    fields = G.parse_reply("CEDARVALE HOMES | Chris Hale ; Pat Ellison | info@cedarvale.ca | https://cedarvale.ca/team",
                           [row["display"]])[0]
    rec = G.decide(row, fields, "h", 1, verify=lambda e, u, w, *_: (True, u[0], ""))
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "CEDARVALE HOMES")["contact_name"] == "Chris Hale"


def test_rejected_email_row_still_stores_the_person_unverified(db, tmp_path, monkeypatch):
    # IRONWOOD HOMES: the cited page is third-party, so the email is rejected, but the named person is kept.
    _seed(db, "IRONWOOD HOMES", website="https://ironwoodhomes.ca", email="")
    row = G.select_gaps(db)[0]
    url = "https://www.bbb.org/ca/ab/calgary/profile/home-builder/ironwood-homes"
    _fake_pages(monkeypatch, {url: "<p>mreyes@ironwoodhomes.ca</p>"})
    fields = G.parse_reply(f"IRONWOOD HOMES | Morgan Reyes (President); Casey Dunn | mreyes@ironwoodhomes.ca | {url}",
                           [row["display"]])[0]
    rec = G.decide(row, fields, "replyhash1", 1)
    assert rec["status"] == "rejected" and rec["rejected_reason"] == "cross_domain_source"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "IRONWOOD HOMES")
    assert got["contact_name"] == "Morgan Reyes" and got["contact_verified"] == 0
    assert got["email"] == "" and got["contact_source"] == "reply:replyhash1"
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[-1])
    assert audit["person_stored"] is True and audit["rejected_reason"] == "cross_domain_source"
    assert audit["email_written"] is False


def test_unknown_email_row_still_stores_the_person_unverified(db, tmp_path):
    _seed(db, "FIELDSTONE CONTRACTING", website="https://fieldstone.ca", email="")
    row = G.select_gaps(db)[0]
    fields = G.parse_reply("FIELDSTONE CONTRACTING | Montee Sutter (Owner) | UNKNOWN | https://fieldstone.ca/about",
                           [row["display"]])[0]
    rec = G.decide(row, fields, "replyhash2", 1, verify=lambda *a: (_ for _ in ()).throw(AssertionError("no verify")))
    assert rec["status"] == "unknown" and rec["rejected_reason"] == "no_email"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "FIELDSTONE CONTRACTING")
    assert got["contact_name"] == "Montee Sutter" and got["contact_verified"] == 0
    assert got["email"] == "" and got["contact_source"] == "reply:replyhash2"
    audit = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[-1])
    assert audit["person_stored"] is True and audit["rejected_reason"] == "no_email"


def test_stored_name_is_not_blanked_by_a_later_row_without_a_person(db, tmp_path, monkeypatch):
    _seed(db, "IRONWOOD HOMES", website="https://ironwoodhomes.ca", email="")
    row = G.select_gaps(db)[0]
    first = G.decide(row, G.parse_reply("IRONWOOD HOMES | Morgan Reyes | UNKNOWN | https://ironwoodhomes.ca",
                                        [row["display"]])[0], "h1", 1)
    G.write_decision(db, first, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "IRONWOOD HOMES")["contact_name"] == "Morgan Reyes"
    later = G.decide(row, G.parse_reply("IRONWOOD HOMES | UNKNOWN | UNKNOWN | https://ironwoodhomes.ca",
                                        [row["display"]])[0], "h2", 2)
    assert later["person"] == ""
    G.write_decision(db, later, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "IRONWOOD HOMES")["contact_name"] == "Morgan Reyes"


def test_task_rejected_row_records_the_sheet_as_provenance(db, tmp_path, monkeypatch):
    _seed(db, "NORTHGATE HOMES", website="https://northgatehomes.ca", email="")
    _fake_pages(monkeypatch, {"https://www.bbb.org/x": "<html>nothing here</html>"})
    values = _task_rows({"NORTHGATE HOMES": ("Dana Whitfield ; Avery Quinn", "dwhitfield@northgatedevs.ca", "https://www.bbb.org/x", "found")})
    row = G.select_gaps(db)[0]
    _, rec = G.task_decisions([row], values, batch_no=1, job_id=TASK_JOB, sheet_url="https://docs/SID/edit")[0]
    assert rec["status"] == "rejected"
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "NORTHGATE HOMES")
    assert got["contact_name"] == "Dana Whitfield" and got["contact_verified"] == 0
    assert got["contact_source"] == "sheet:https://docs/SID/edit" and got["email"] == ""


# ---------------------------------------------------------------- priority order
import csv as _csv  # noqa: E402


def _permit_index(tmp_path, rows):
    """A real permit index built from a demo-format CSV under tmp_path."""
    head = ["permit_number", "company_name", "permit_value", "community", "description"]
    with (tmp_path / "calgary_leads_demo.csv").open("w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)
    return H.load_permit_index((("calgary_leads_demo.csv", {"company": "company_name",
                                                           "community": "community",
                                                           "value": "permit_value",
                                                           "scope": "description"}),),
                               repo=tmp_path)


def _seed_gaps(db, *names):
    for n in names:
        _seed(db, n, website=f"https://{n.lower().replace(' ', '')}.ca", email="")


def _ordered(rows):
    return [r["company_name"] for r in rows]


def test_select_gaps_orders_by_permit_value_then_unvalued_last(db, tmp_path):
    _seed_gaps(db, "ALPHA LTD", "BETA LTD", "DELTA LTD", "GAMMA LTD")
    index = _permit_index(tmp_path, [
        ["P1", "ALPHA LTD", "100000", "NW", "x"],
        ["P2", "BETA LTD", "2500000", "NE", "x"],
        ["P3", "GAMMA LTD", "1800000", "SE", "x"],
    ])                                               # DELTA LTD is not on any CSV
    rows = G.select_gaps(db, permits=index)
    assert _ordered(rows) == ["BETA LTD", "GAMMA LTD", "ALPHA LTD", "DELTA LTD"]
    assert [r["value_num"] for r in rows] == [2500000.0, 1800000.0, 100000.0, None]


def test_company_absent_from_index_sorts_after_valued_ones_even_when_alphabetically_first(db, tmp_path):
    _seed_gaps(db, "AAA CO", "ZZZ CO")
    index = _permit_index(tmp_path, [["P1", "ZZZ CO", "50000", "NW", "x"]])
    rows = G.select_gaps(db, permits=index)          # not --current-only: AAA stays, last
    assert _ordered(rows) == ["ZZZ CO", "AAA CO"]


def test_current_only_keeps_only_companies_on_this_weeks_csvs(db, tmp_path):
    _seed_gaps(db, "ALPHA LTD", "BETA LTD", "DELTA LTD", "GAMMA LTD")
    index = _permit_index(tmp_path, [
        ["P1", "ALPHA LTD", "100000", "NW", "x"],
        ["P2", "BETA LTD", "2500000", "NE", "x"],
        ["P3", "GAMMA LTD", "1800000", "SE", "x"],
    ])
    rows = G.select_gaps(db, permits=index, current_only=True)
    assert _ordered(rows) == ["BETA LTD", "GAMMA LTD", "ALPHA LTD"]      # DELTA dropped
    # The limit applies after the filter and the ordering, so the top 2 are the top-value current ones.
    assert _ordered(G.select_gaps(db, limit=2, permits=index, current_only=True)) == ["BETA LTD", "GAMMA LTD"]


def test_no_csv_falls_back_to_name_order_without_crashing(db, tmp_path):
    _seed_gaps(db, "GAMMA LTD", "ALPHA LTD", "BETA LTD")
    empty = H.load_permit_index((("calgary_leads_demo.csv", {"company": "company_name",
                                                           "community": "community",
                                                           "value": "permit_value",
                                                           "scope": "description"}),),
                                repo=tmp_path)       # no CSV in tmp_path
    assert empty == {}
    expected = ["ALPHA LTD", "BETA LTD", "GAMMA LTD"]
    assert _ordered(G.select_gaps(db, permits=empty)) == expected
    assert _ordered(G.select_gaps(db, permits=None)) == expected
    assert G.select_gaps(db, permits=empty, current_only=True) == []    # nothing is on a CSV
    assert all(r["value_num"] is None for r in G.select_gaps(db, permits=empty))


def test_ties_break_by_name_deterministically(db, tmp_path):
    _seed_gaps(db, "ZED CO", "ANT CO", "YAK CO", "BEE CO")
    index = _permit_index(tmp_path, [
        ["P1", "ZED CO", "1000000", "NW", "x"],
        ["P2", "ANT CO", "1000000", "NE", "x"],
    ])                                               # YAK and BEE have no value
    first = _ordered(G.select_gaps(db, permits=index))
    assert first == ["ANT CO", "ZED CO", "BEE CO", "YAK CO"]
    assert _ordered(G.select_gaps(db, permits=index)) == first


def test_via_task_defaults_current_only_on_and_prints_priority(db, tmp_path, monkeypatch, capsys):
    _seed_gaps(db, "ALPHA LTD", "BETA LTD", "DELTA LTD")
    index = _permit_index(tmp_path, [
        ["P1", "ALPHA LTD", "100000", "NW", "x"],
        ["P2", "BETA LTD", "2500000", "NE", "x"],
    ])
    monkeypatch.setattr(H, "load_permit_index", lambda *a, **k: index)
    seen = []
    real = G.select_gaps
    monkeypatch.setattr(G, "select_gaps", lambda *a, **k: (seen.append(k.get("current_only")), real(*a, **k))[1])
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run")))
    rc = G.main(["--via-task", "--dry-run", "--calls-file", str(tmp_path / "calls.json"),
                 "--audit-log", str(tmp_path / "a.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0 and seen == [True]
    assert "priority order: 2 company(ies)" in out
    assert " 1. BETA LTD" in out and "$2,500,000" in out and " 2. ALPHA LTD" in out and "$100,000" in out
    assert "DELTA" not in out                        # off this week's CSVs: not chosen


def test_chat_path_defaults_current_only_off(db, tmp_path, monkeypatch):
    _seed_gaps(db, "ALPHA LTD", "DELTA LTD")
    monkeypatch.setattr(H, "load_permit_index", lambda *a, **k: {"ALPHA LTD": {"est_value": "$1", "community": "", "scope": ""}})
    seen = []
    real = G.select_gaps
    monkeypatch.setattr(G, "select_gaps", lambda *a, **k: (seen.append(k.get("current_only")), real(*a, **k))[1])
    monkeypatch.setattr(G, "ask_bridge", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no bridge")))
    G.main(["--dry-run", "--calls-file", str(tmp_path / "calls.json"), "--audit-log", str(tmp_path / "a.jsonl")])
    assert seen == [False]


def test_no_current_only_flag_turns_the_filter_off_for_via_task(db, tmp_path, monkeypatch, capsys):
    _seed_gaps(db, "ALPHA LTD", "DELTA LTD")
    monkeypatch.setattr(H, "load_permit_index", lambda *a, **k: {"ALPHA LTD": {"est_value": "$1", "community": "", "scope": ""}})
    seen = []
    real = G.select_gaps
    monkeypatch.setattr(G, "select_gaps", lambda *a, **k: (seen.append(k.get("current_only")), real(*a, **k))[1])
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run")))
    G.main(["--via-task", "--no-current-only", "--dry-run", "--calls-file", str(tmp_path / "calls.json"),
            "--audit-log", str(tmp_path / "a.jsonl")])
    assert seen == [False]
    assert "DELTA LTD" in capsys.readouterr().out    # off-CSV company is now chosen, last


# ------------------------------------------------- opt-in own-domain role inference
def _adore_reply(email, url="https://harbourservices.ca/"):
    return f"HARBOUR BUILDING SERVICES | Joe Ferraro | {email} | {url}"


def _decide_with(db, company, reply, monkeypatch, *, flag, mx=True, pages=None):
    _fake_pages(monkeypatch, pages or {}, mx=mx)
    row = next(r for r in G.select_gaps(db) if r["company_name"] == P.cache_key(company))
    fields = G.parse_reply(reply, [row["display"]])[0]
    return row, G.decide(row, fields, "h", 1, accept_own_domain_role=flag)


def test_a_own_domain_role_inbox_with_mx_is_inferred_when_flag_on(db, tmp_path, monkeypatch):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply("info@harbourservices.ca"),
                            monkeypatch, flag=True)
    assert rec["status"] == G.ROLE_INFERRED
    assert rec["verified"] is False and rec["contact_verified"] == 0
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "HARBOUR BUILDING SERVICES")
    assert got["email"] == "info@harbourservices.ca"            # kept so the client can contact them
    assert got["contact_source"] == "own_domain_role_inferred"
    cur = db.execute("SELECT contact_verified FROM companies WHERE company_name=?",
                     ("HARBOUR BUILDING SERVICES",)).fetchone()
    assert cur[0] == 0


def test_b_same_address_is_rejected_when_flag_off(db, tmp_path, monkeypatch):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply("info@harbourservices.ca"),
                            monkeypatch, flag=False)
    assert rec["status"] == "rejected" and rec["rejected_reason"] == "not_on_cited_page"
    assert "inference" not in rec
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert (P.cache_get(db, "HARBOUR BUILDING SERVICES") or {}).get("email", "") == ""


@pytest.mark.parametrize("email", ["ryan@harbourservices.ca", "owen.marsh@harbourservices.ca"])
def test_c_non_role_local_part_is_never_inferred_even_on_the_company_domain(db, monkeypatch, email):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply(email), monkeypatch, flag=True)
    assert rec["status"] == "rejected"
    ok, detail = G.own_domain_role_inference(email, "HARBOUR BUILDING SERVICES", "https://harbourservices.ca/")
    assert ok is False and detail["refused"] == "non_role_local_part"


@pytest.mark.parametrize("email", ["info.synthetic@gmail.com", "info@reviewdirectory.example"])
def test_d_foreign_domain_is_never_inferred(db, monkeypatch, email):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply(email), monkeypatch, flag=True)
    assert rec["status"] == "rejected"
    ok, _detail = G.own_domain_role_inference(email, "HARBOUR BUILDING SERVICES", "https://harbourservices.ca/")
    assert ok is False


def test_e_audit_record_carries_the_inferred_reason_and_contact_verified_0(db, tmp_path, monkeypatch):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply("info@harbourservices.ca"),
                            monkeypatch, flag=True)
    audit = tmp_path / "a.jsonl"
    G.write_decision(db, rec, row, audit)
    line = json.loads(audit.read_text(encoding="utf-8").splitlines()[0])
    assert line["status"] == "own_domain_role_inferred"
    assert line["reason"] == "own_domain_role_inferred"
    assert line["contact_verified"] == 0
    assert line["inference"]["rule"] == "own_domain_role_inferred"
    assert line["inference"]["local_part"] == "info"
    assert line["inference"]["domain"] == "harbourservices.ca"
    assert line["inference"]["match"] == "website_domain"
    assert line["inference"]["mx"] is True
    assert line["inference"]["verify_reason"] == "not_on_cited_page"
    assert line["email_written"] is True
    # Terminal: a rerun does not select the company again (no bridge spend).
    assert "HARBOUR BUILDING SERVICES" in G.load_done(audit)


def test_e2_company_name_token_match_inferred_when_website_is_missing(db, monkeypatch):
    _seed(db, "ORIGIN BUILDERS GROUP", website="", email="")
    _fake_pages(monkeypatch, {}, mx=True)
    ok, detail = G.own_domain_role_inference("info@originbuilds.example", "ORIGIN BUILDERS GROUP", "")
    assert ok is True and detail["match"] == "company_name"


def test_no_mx_is_never_inferred_even_with_the_flag_on(db, monkeypatch):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    _row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply("info@harbourservices.ca"),
                             monkeypatch, flag=True, mx=False)
    assert rec["status"] == "rejected" and rec["rejected_reason"] == "no_mx"
    assert "inference" not in rec


def test_f_existing_verified_contact_is_never_downgraded(db, tmp_path, monkeypatch):
    _fake_pages(monkeypatch, {}, mx=True)  # MX stubbed: no live DNS
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/", email="")
    db.execute("UPDATE companies SET email=?, contact_verified=1, contact_name=? WHERE company_name=?",
               ("jordan@harbourservices.ca", "Jordan Lowell", "HARBOUR BUILDING SERVICES"))
    db.commit()
    row = {"company_name": "HARBOUR BUILDING SERVICES", "email": "jordan@harbourservices.ca",
           "website": "https://harbourservices.ca/", "display": "HARBOUR BUILDING SERVICES", "phone": ""}
    rec = G.decide(row, G.parse_reply(_adore_reply("info@harbourservices.ca"),
                                      ["HARBOUR BUILDING SERVICES"])[0], "h", 1,
                   verify=lambda *a, **k: (False, "", "not_on_cited_page"),
                   accept_own_domain_role=True)
    assert rec["status"] == G.ROLE_INFERRED
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    got = P.cache_get(db, "HARBOUR BUILDING SERVICES")
    assert got["email"] == "jordan@harbourservices.ca"
    assert db.execute("SELECT contact_verified, contact_name FROM companies WHERE company_name=?",
                      ("HARBOUR BUILDING SERVICES",)).fetchone() == (1, "Jordan Lowell")
    assert rec["reason"] == "existing_verified_kept" and rec.get("email_written") is not True


def test_inferred_role_never_overwrites_a_different_cached_email(db, tmp_path, monkeypatch):
    _seed(db, "HARBOUR BUILDING SERVICES", website="https://harbourservices.ca/",
          email="hello@harbourservices.ca")
    row, rec = _decide_with(db, "HARBOUR BUILDING SERVICES", _adore_reply("info@harbourservices.ca"),
                            monkeypatch, flag=True)
    G.write_decision(db, rec, row, tmp_path / "a.jsonl")
    assert P.cache_get(db, "HARBOUR BUILDING SERVICES")["email"] == "hello@harbourservices.ca"
    assert rec["reason"] == "existing_email_kept"


def test_cli_flag_defaults_off_and_is_passed_to_the_run(db, tmp_path, monkeypatch):
    seen = []

    def fake_run(*a, **k):
        seen.append(k.get("accept_own_domain_role"))
        return {"accepted": 0, "named_person_only": 0, "rejected": 0, "unknown": 0,
                "no_line": 0, "bridge_failed": 0, G.ROLE_INFERRED: 0, "budget_stop": "none"}

    monkeypatch.setattr(G, "run", fake_run)
    args = ["--calls-file", str(tmp_path / "calls.json"), "--audit-log", str(tmp_path / "a.jsonl")]
    G.main(args)
    G.main(args + ["--accept-own-domain-role"])
    assert seen == [False, True]


def test_task_bucket_keeps_inferred_separate_from_accepted():
    assert G._task_bucket({"status": G.ROLE_INFERRED}) == G.ROLE_INFERRED
    assert G.ROLE_INFERRED in G.TASK_STATUS_KEYS and G.ROLE_INFERRED in G.DONE_STATUSES
    assert G.ROLE_INFERRED not in G.RESOLVED_STATUSES



if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
