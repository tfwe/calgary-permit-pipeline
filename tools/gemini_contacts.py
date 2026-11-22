#!/usr/bin/env python3
"""Gemini contact enrichment: resumable backfill of named contacts.

Selects companies in cache.db whose email is missing or a role inbox
(info@, contact@, ...), batches them into prompts for the Gemini app via the
phone bridge (CALGARY_LEADS_BRIDGE_SCRIPT), and accepts a claimed email only
when it is proven on a page this tool fetched itself:

  (a) pipeline.valid_email, (b) pipeline.domain_has_mx is True,
  (c) the email literal appears in the HTML of a cited URL whose host is the
      email's domain, the company's cached website domain, or a domain that
      carries the company's own name (lakeviewdevelopmentsinfo.ca for LAKEVIEW DEVELOPMENTS);
      or else in the company's cached website / contact / about pages
      (source 'own_site').

Model policy: the bridge model defaults to Hydrogen (the workhorse). With
--escalate, companies Hydrogen left UNKNOWN or rejected are retried once on Neon,
under a separate escalation cap. A company Hydrogen resolved is never sent to Neon.

Budget: every bridge call is counted per model pool in logs/gemini_contacts.calls.json
(local date). The counter is incremented before the call, and a run stops cleanly
when a pool is spent. --calls-status prints the counters without calling the bridge.

Nothing the model says is written to cache.db unless (a)-(c) hold. Every
company decided is appended to logs/gemini_contacts.jsonl; a company with a
terminal audit record is skipped on the next run, so reruns never re-spend
bridge calls on work already done.

    .venv/bin/python tools/gemini_contacts.py --limit 3 --batch 3
    .venv/bin/python tools/gemini_contacts.py --escalate --limit 3
    .venv/bin/python tools/gemini_contacts.py --calls-status
    .venv/bin/python tools/gemini_contacts.py --dry-run --batch 3
    .venv/bin/python tools/gemini_contacts.py --dry-run --replay captured.json
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import subprocess
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import config  # noqa: E402
import pipeline as P  # noqa: E402
import gemini_task_sheet as H  # noqa: E402
from gemini_task_sheet import QuotaRefused, TaskError  # noqa: E402

BRIDGE = config.tool_or_placeholder(config.BRIDGE_SCRIPT, "CALGARY_LEADS_BRIDGE_SCRIPT")
BRIDGE_TIMEOUT = 600
AUDIT_LOG = REPO / "logs" / "gemini_contacts.jsonl"
CALLS_FILE = REPO / "logs" / "gemini_contacts.calls.json"

BRIDGE_MODELS = ("hydrogen", "neon", "auto")
DEFAULT_MODEL = "hydrogen"
# Per-day caps. "escalation" is a sub-cap on Neon calls made by --escalate.
# "task" counts --via-task Gemini Task submissions (one per batch of up to 15 companies).
DEFAULT_LIMITS = {"hydrogen": 6, "neon": 2, "escalation": 1, "task": 2}
POOLS = ("hydrogen", "neon", "escalation", "task")

ROLE_LOCALS = frozenset({
    "info", "contact", "admin", "office", "sales", "hello", "enquiries", "inquiries",
    "general", "reception", "mail", "service", "accounts", "support", "team",
    "marketing", "billing",
})
PROMPT_HEAD = (
    "Search the web. Calgary Alberta businesses. Reply ONE line each, format: "
    "NAME | named person | published email | source URL. Use UNKNOWN for anything "
    "not found on a real page. Never guess or construct an email pattern."
)
UNKNOWN_TOKENS = frozenset({"", "UNKNOWN", "N/A", "NONE", "-"})
# Statuses that close a company for resume purposes. Anything else (bridge
# failure, company missing from a reply) is retried on the next run.
# named_person_only: a role inbox was verified and a person was found; the role
# email stays and the name is recorded. Terminal, so it is not re-spent.
# own_domain_role_inferred: opt-in (--accept-own-domain-role). A role inbox on the company's
# own domain with a valid MX, not proven on any page. Stored with contact_verified=0. Terminal,
# so it is not re-spent. It is not a resolved (verified) status.
ROLE_INFERRED = "own_domain_role_inferred"
DONE_STATUSES = frozenset({"accepted", "named_person_only", "rejected", "unknown", ROLE_INFERRED})
RESOLVED_STATUSES = frozenset({"accepted", "named_person_only"})
ESCALATE_STATUSES = frozenset({"unknown", "rejected"})
_MULTI_LABEL_SLD = frozenset({"co", "com", "net", "org", "gc", "ab", "bc", "ac", "gov", "edu"})
# Third-party hosts never prove a company's own address, even when the page
# carries a literal match. A name in the label would not make them the company's site.
THIRD_PARTY_HOSTS = frozenset({
    "bbb.org", "houzz.com", "houzz.ca", "yelp.com", "yelp.ca", "facebook.com",
    "instagram.com", "linkedin.com", "anthemproperties.com",
})


class BudgetExhausted(Exception):
    """Raised inside the bridge guard when a pool has no calls left today."""

    def __init__(self, pool: str):
        super().__init__(pool)
        self.pool = pool


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def local_part(email: str) -> str:
    return email.rsplit("@", 1)[0].strip().lower() if "@" in email else ""


def is_gap(email: str) -> bool:
    """True when the company has no email, or only a role inbox."""
    email = (email or "").strip()
    if "@" not in email:
        return True
    return local_part(email) in ROLE_LOCALS


def is_role(email: str) -> bool:
    return local_part(email) in ROLE_LOCALS


def registrable(host: str) -> str:
    """Registrable domain, ignoring a leading www. Handles '.co.uk'-style suffixes
    well enough for the Canadian/US hosts in this list (e.g. 'x.ab.ca' -> 'x.ab.ca')."""
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _MULTI_LABEL_SLD:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def display_name(company: str, unmasked: str) -> str:
    """Name the model should search for: the unmasked trade name for numbered shells."""
    name = unmasked if (P.NUMBERED_RE.match(company) and unmasked) else company
    return re.sub(r"\s+", " ", name).strip()


def load_done(audit_path: Path) -> set[str]:
    done: set[str] = set()
    if not audit_path.exists():
        return done
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("status") in DONE_STATUSES:
            done.add(P.cache_key(rec.get("company", "")))
    return done


def resolved_companies(audit_path: Path) -> set[str]:
    """Companies with any audit record that holds a verified contact, on any model."""
    out: set[str] = set()
    if not audit_path.exists():
        return out
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("status") in RESOLVED_STATUSES:
            out.add(P.cache_key(rec.get("company", "")))
    return out


def permit_value(permits: dict[str, dict] | None, *names: str) -> float | None:
    """Estimated permit value for the first name found in the index, or None."""
    return H.parse_value(H.permit_fields(permits, *names).get("est_value"))


def in_permit_index(permits: dict[str, dict] | None, *names: str) -> bool:
    """True when one of the names is a company on this week's lead CSVs."""
    if not permits:
        return False
    return any(H.norm_company(n) in permits for n in names if H.norm_company(n))


def select_gaps(conn: sqlite3.Connection, *, limit: int | None = None, redo: bool = False,
                done: frozenset[str] | set[str] = frozenset(),
                names: set[str] | None = None,
                permits: dict[str, dict] | None = None,
                current_only: bool = False) -> list[dict]:
    """Gap companies to enrich, in priority order.

    permits is the index from gemini_task_sheet.load_permit_index(). With a non-empty
    index the candidates are ordered by estimated permit value, highest first, then by
    name; companies with no value (or absent from the index) come last. With no index
    the order is the original name order, so nothing changes without the CSVs.

    current_only keeps only companies present in the lead CSVs (this week's sheets).
    Without an index nothing is current, so the result is empty.

    The gap definition and the done/verified/redo skips are unchanged.
    """
    rows = conn.execute(
        "SELECT company_name, unmasked_tradename, website, phone, email, "
        "COALESCE(contact_verified, 0) FROM companies "
        "ORDER BY (COALESCE(website, '') = ''), company_name"
    ).fetchall()
    out: list[dict] = []
    for company, unmasked, website, phone, email, verified in rows:
        key = P.cache_key(company)
        if names is not None and key not in names:
            continue
        if not redo and key in done:
            continue
        if not redo and verified:
            # Already verified by an earlier run whose audit record may be lost:
            # treat as done rather than spend another bridge call on it.
            continue
        if not is_gap(email or ""):
            continue
        if P.NUMBERED_RE.match(company) and not unmasked:
            continue  # numbered shell with no licence unmask: nothing to search for
        display = display_name(key, unmasked or "")
        if current_only and not in_permit_index(permits, display, key):
            continue
        out.append({"company_name": key, "unmasked_tradename": unmasked or "",
                    "display": display,
                    "website": website or "", "phone": phone or "", "email": email or "",
                    "value_num": permit_value(permits, display, key)})
    if permits:
        out.sort(key=lambda r: (r["value_num"] is None, -(r["value_num"] or 0.0), r["company_name"]))
    if limit is not None:
        out = out[:limit]
    return out


def print_priority(rows: list[dict], out=print) -> None:
    """The chosen companies in the order they will be enriched, with their values."""
    out(f"priority order: {len(rows)} company(ies), highest estimated permit value first")
    for i, r in enumerate(rows, 1):
        v = r.get("value_num")
        val = f"${v:,.0f}" if v is not None else "no permit value"
        out(f"  {i:>2}. {r['display']:<40} {val:>16}")


# ---------------------------------------------------------------------------
# Daily call budget, per model pool
# ---------------------------------------------------------------------------
def today_local() -> str:
    return datetime.now().astimezone().date().isoformat()


def _charges(model: str, escalation: bool) -> tuple[str, ...]:
    """Which pools one call draws on. 'auto' may land on Neon, so it is charged to
    both pools. An escalation call is a Neon call that also draws on the escalation cap."""
    if model == "hydrogen":
        return ("hydrogen",)
    if model == "neon":
        return ("neon", "escalation") if escalation else ("neon",)
    if model == "auto":
        return ("hydrogen", "neon")
    if model == "task":
        return ("task",)
    raise ValueError(f"unknown model {model!r}")


class CallBudget:
    """Persistent per-day call counters in the calls file. The file is rewritten
    before every bridge call, so a kill between count and call cannot return a free call."""

    def __init__(self, path: Path, limits: dict[str, int], today: str | None = None):
        self.path = Path(path)
        self.limits = {p: int(limits.get(p, DEFAULT_LIMITS[p])) for p in POOLS}
        self.today = today or today_local()

    def load(self) -> dict:
        fresh = {"date": self.today, **{p: 0 for p in POOLS}, "companies": []}
        if not self.path.exists():
            return fresh
        # An unreadable file raises: resetting the counters would silently re-open the quota.
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("date") != self.today:
            return fresh  # new local day: counters reset
        for p in POOLS:
            data[p] = int(data.get(p, 0) or 0)
        data["companies"] = list(data.get("companies") or [])
        return data

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def reserve(self, model: str, companies: list[str], escalation: bool = False) -> str | None:
        """Charge one call before it is made. Returns the spent pool name, or None
        when the call is allowed (and already counted)."""
        state = self.load()
        charges = _charges(model, escalation)
        for pool in charges:
            if state[pool] >= self.limits[pool]:
                return pool
        for pool in charges:
            state[pool] += 1
        seen = list(state["companies"])
        for name in companies:
            if name not in seen:
                seen.append(name)
        state["companies"] = seen
        state["date"] = self.today
        self._write(state)
        return None

    def status(self) -> dict:
        st = self.load()
        return {"date": self.today,
                **{p: {"used": st[p], "limit": self.limits[p],
                       "remaining": max(0, self.limits[p] - st[p])} for p in POOLS}}


def escalation_candidates(records: list[dict], rows_by_key: dict[str, dict],
                          audit_path: Path) -> list[dict]:
    """Rows Hydrogen left UNKNOWN or rejected in this run. Never a company that
    already holds a verified contact, and never one Hydrogen verified (so a
    company Hydrogen resolved cannot reach Neon)."""
    resolved = resolved_companies(audit_path)
    out: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        key = rec.get("company", "")
        if rec.get("model") != "hydrogen" or rec.get("escalated"):
            continue
        if rec.get("status") not in ESCALATE_STATUSES or rec.get("verified"):
            continue
        if key in resolved or key in seen or key not in rows_by_key:
            continue
        seen.add(key)
        out.append(rows_by_key[key])
    return out


# ---------------------------------------------------------------------------
# Prompting and the bridge
# ---------------------------------------------------------------------------
def build_prompt(names: list[str]) -> str:
    return PROMPT_HEAD + "\n" + "\n".join(f"{i} {n}" for i, n in enumerate(names, 1))


def strip_diagnostics(text: str) -> str:
    """Drop the driver's '# ...' diagnostic lines, keep the reply."""
    keep = [ln for ln in text.splitlines() if not re.match(r"\s*#( |$)", ln)]
    return "\n".join(keep).strip()


def reply_hash(reply: str) -> str:
    return hashlib.sha256(reply.encode("utf-8")).hexdigest()[:16]


def ask_bridge(prompt: str, model: str = DEFAULT_MODEL) -> tuple[bool, str, str]:
    """Return (ok, reply, error). Never raises."""
    if BRIDGE.startswith("$"):
        return False, "", "CALGARY_LEADS_BRIDGE_SCRIPT is not set"
    try:
        proc = subprocess.run([BRIDGE, "ask", "--model", model, prompt],
                              capture_output=True, text=True, timeout=BRIDGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except OSError as exc:
        return False, "", f"os error: {exc}"
    if proc.returncode != 0:
        return False, "", f"exit {proc.returncode}"
    reply = strip_diagnostics(proc.stdout or "")
    if not reply:
        return False, "", "empty reply"
    return True, reply, ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_index(name_raw: str, names: list[str]) -> int | None:
    """Map a reply's NAME column to a batch index. Exact (normalised) first, then a
    unique close match. Unknown names return None so nothing is fabricated."""
    target = _norm(name_raw)
    if not target:
        return None
    norms = [_norm(n) for n in names]
    exact = [i for i, n in enumerate(norms) if n == target]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    close = difflib.get_close_matches(target, norms, n=2, cutoff=0.9)
    if len(close) == 1:
        return norms.index(close[0])
    return None


def clean_person(raw: str) -> str:
    if raw.strip().upper() in UNKNOWN_TOKENS:
        return ""
    s = re.sub(r"\([^)]*\)", "", raw)  # drop '(Owner)' / '(Partners)' annotations
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    if not re.search(r"[A-Za-z]", s) or "@" in s or "http" in s.lower() or len(s) > 120:
        return ""
    return s


_PERSON_SPLIT_RE = re.compile(r"\s*(?:/|,|&|;|\n)\s*|\s+and\s+", re.IGNORECASE)


def first_person(names: str) -> str:
    """The first person in a roster such as 'A / B', 'A, B', 'A and B', 'A & B', 'A ; B'
    (';' is the Task surface's list separator).

    contact_name is a single person for a cold email, so the rest of the roster is
    kept only in the audit record (person_all)."""
    for part in _PERSON_SPLIT_RE.split(names or ""):
        part = part.strip(" ,;")
        if part:
            return part
    return ""


def _urls_from(text: str) -> list[str]:
    urls: list[str] = []
    for tok in re.split(r"\s*;\s*|\s+", text):
        tok = tok.strip(" <>()[]'\"")
        if tok.lower().startswith(("http://", "https://")) and tok not in urls:
            urls.append(tok)
    return urls


def parse_reply(reply: str, names: list[str]) -> dict[int, dict]:
    """Parse 'NAME | person | email | url ; url' lines into {batch_index: fields}.

    Lines that are not pipe-separated, name no company in the batch, or repeat an
    already-parsed company are skipped. A missing company yields no entry.
    """
    out: dict[int, dict] = {}
    for raw in reply.splitlines():
        line = raw.strip().strip("*").strip()
        line = re.sub(r"^(?:\d+[.)]\s+|[-*\u2022]\s+)", "", line).strip()
        if line.count("|") < 3:
            continue
        parts = [p.strip() for p in line.split("|")]
        idx = match_index(parts[0].strip("* "), names)
        if idx is None or idx in out:
            continue
        person_raw, email_raw = parts[1], parts[2]
        rest = "|".join(parts[3:])
        phone = ""
        for m in P.PHONE_RE.finditer(re.sub(r"https?://\S+", " ", " ".join(parts[1:]))):
            phone = P.normalize_phone(m.group(0))
            if phone:
                break
        person_all = clean_person(person_raw)
        out[idx] = {
            "name_raw": parts[0],
            "person": first_person(person_all),
            "person_all": person_all,
            "email_raw": email_raw.strip(),
            "urls": _urls_from(rest),
            "phone": phone,
            "line": line,
        }
    return out


# ---------------------------------------------------------------------------
# Verification: an email is accepted only if its literal is on a page we fetched
# ---------------------------------------------------------------------------
def page_contains(page_html: str, email: str) -> bool:
    """Case-insensitive literal test on the entity-decoded page. Whitespace is allowed
    only inside the address (between its characters), never stripped from the page,
    so 'sales@acme.ca</p>Trades' does not glue into a longer token. The lookarounds
    stop 'xinfo@acme.ca' from counting as 'info@acme.ca'."""
    haystack = html.unescape(page_html).lower()
    needle = email.strip().lower()
    body = r"\s*".join(re.escape(ch) for ch in needle)
    pattern = r"(?<![a-z0-9._%+\-])" + body + r"(?![a-z0-9\-]|\.[a-z0-9])"
    return re.search(pattern, haystack) is not None


def own_site_urls(website: str) -> list[str]:
    base = website if website.startswith(("http://", "https://")) else f"https://{website}"
    return [base, urljoin(base, "/contact"), urljoin(base, "/about")]


def company_domain_proves(url: str, company: str) -> bool:
    """True when the page's host carries the company's own name, e.g. lakeviewdevelopmentsinfo.ca
    for LAKEVIEW DEVELOPMENTS. Generic suffixes (homes, builders, developments, ...) are
    handled inside pipeline._domain_related. Known third-party hosts never count."""
    if not company:
        return False
    host = (urlparse(url).hostname or "").lower()
    reg = registrable(host)
    if not reg or reg in THIRD_PARTY_HOSTS:
        return False
    return P._domain_related(f"https://{reg}", company)


def verify_contact(email: str, cited_urls: list[str], website: str,
                   company: str = "") -> tuple[bool, str, str]:
    """Return (accepted, email_source, reason).

    Reasons: invalid_syntax, no_mx, mx_unknown, no_url, not_on_cited_page,
    cross_domain_source. A cited page proves the address when it is served from the
    email's own registrable domain, the company's cached website domain, or a domain
    carrying the company's own name. The literal alone is not enough: a broker's
    listing can carry a builder's address, so third-party pages are rejected.
    """
    if not P.valid_email(email):
        return False, "", "invalid_syntax"
    mx = P.domain_has_mx(email)
    if mx is False:
        return False, "", "no_mx"
    if mx is None:
        return False, "", "mx_unknown"
    allowed = {registrable(email.rsplit("@", 1)[1])}
    if website:
        allowed.add(registrable(urlparse(website if "://" in website else "https://" + website).hostname or ""))
    cross_domain = False
    for url in cited_urls:
        try:
            page, final = P.fetch_page(url)
        except Exception:  # noqa: BLE001 - a dead cited page just means try the next
            continue
        if not page_contains(page, email):
            continue
        served = final or url
        host = urlparse(served).hostname or ""
        if registrable(host) in allowed or company_domain_proves(served, company):
            return True, url, ""
        cross_domain = True
    if website:
        for url in own_site_urls(website):
            try:
                page, _final = P.fetch_page(url)
            except Exception:  # noqa: BLE001
                continue
            if page_contains(page, email):
                return True, "own_site", ""
    if cross_domain:
        return False, "", "cross_domain_source"
    if not cited_urls:
        return False, "", "no_url"
    return False, "", "not_on_cited_page"


def own_domain_role_inference(email: str, company: str, website: str,
                              mx_ok=None) -> tuple[bool, dict]:
    """Opt-in rule for a verification failure. Accept the address as an INFERRED,
    unverified role inbox only when ALL hold:
      * the local part is a role inbox (ROLE_LOCALS);
      * the email's registrable domain is the company's cached website registrable
        domain, or a domain that carries the company's own name (company_domain_proves);
      * the domain has a valid MX (domain_has_mx is True).
    A non-role local part, a free-mail domain, a directory/third-party host or a
    foreign domain is never accepted. Returns (accepted, audit_detail)."""
    detail = {"rule": ROLE_INFERRED, "local_part": local_part(email),
              "domain": "", "website_domain": "", "match": "", "mx": None}
    if not P.valid_email(email):
        return False, {**detail, "refused": "invalid_syntax"}
    local, _, domain = email.strip().lower().rpartition("@")
    reg = registrable(domain)
    detail.update(domain=reg)
    if local not in ROLE_LOCALS:
        return False, {**detail, "refused": "non_role_local_part"}
    if reg in P.FREE_EMAIL_DOMAINS or reg in THIRD_PARTY_HOSTS:
        return False, {**detail, "refused": "not_company_domain"}
    site_host = urlparse(website if "://" in website else "https://" + website).hostname or "" \
        if website else ""
    site_reg = registrable(site_host) if site_host else ""
    detail["website_domain"] = site_reg
    if site_reg and reg == site_reg:
        detail["match"] = "website_domain"
    elif company_domain_proves(f"https://{reg}", company):
        detail["match"] = "company_name"
    else:
        return False, {**detail, "refused": "foreign_domain"}
    mx = P.domain_has_mx(email) if mx_ok is None else mx_ok
    detail["mx"] = mx
    if mx is not True:
        return False, {**detail, "refused": "no_mx"}
    return True, detail


# ---------------------------------------------------------------------------
# Decisions, persistence, the run loop
# ---------------------------------------------------------------------------
def collect_replies(prompt: str, names: list[str], attempts: int, ask=ask_bridge
                    ) -> tuple[dict[int, dict], dict[int, str], int, str]:
    """Call the bridge up to `attempts` times, merging parsed lines across replies.
    Stops early once every company has a line. Returns (parsed, hashes, successes, last_err).
    A budget refusal stops the loop with last_err 'budget:<pool>' and keeps replies already received."""
    parsed: dict[int, dict] = {}
    hashes: dict[int, str] = {}
    successes = 0
    last_err = ""
    for _ in range(max(1, attempts)):
        try:
            ok, reply, err = ask(prompt)
        except BudgetExhausted as exc:
            return parsed, hashes, successes, f"budget:{exc.pool}"
        if not ok:
            last_err = err
            continue
        successes += 1
        h = reply_hash(reply)
        for idx, fields in parse_reply(reply, names).items():
            if idx not in parsed:
                parsed[idx] = fields
                hashes[idx] = h
        if len(parsed) >= len(names):
            break
        last_err = f"short reply {len(parsed)}/{len(names)}"
    return parsed, hashes, successes, last_err


def decide(row: dict, fields: dict | None, model_hash: str, batch_no: int,
           verify=verify_contact, accept_own_domain_role: bool = False) -> dict:
    rec = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "company": row["company_name"], "person": "", "email": "", "email_source": "",
        "verified": False, "rejected_reason": "", "model_reply_hash": model_hash,
        "status": "", "batch": batch_no, "phone_kept": "", "name_prompted": row["display"],
        "prev_email": row.get("email", "") or "", "email_written": False,
    }
    if fields is None:
        rec["status"] = "no_line"
        rec["rejected_reason"] = "missing_from_reply"
        return rec
    rec["person"] = fields["person"]
    rec["person_all"] = fields.get("person_all", fields["person"])
    email_raw = fields["email_raw"]
    if email_raw.strip().upper() in UNKNOWN_TOKENS:
        rec["status"] = "unknown"
        rec["rejected_reason"] = "no_email"
        return rec
    m = P.EMAIL_RE.search(email_raw)
    email = m.group(0) if m else ""
    if not email:
        rec["status"] = "rejected"
        rec["rejected_reason"] = "invalid_syntax"
        return rec
    rec["email"] = email
    ok, source, reason = verify(email, fields["urls"], row.get("website", ""), row["company_name"])
    if not ok:
        rec["status"] = "rejected"
        rec["rejected_reason"] = reason
        # Opt-in only. Invalid syntax and MX failures are never inferred past.
        if accept_own_domain_role and reason not in ("invalid_syntax", "no_mx", "mx_unknown"):
            inferred, detail = own_domain_role_inference(
                email, row["company_name"], row.get("website", ""))
            detail["verify_reason"] = reason
            if inferred:
                rec["status"] = ROLE_INFERRED
                rec["reason"] = ROLE_INFERRED
                rec["rejected_reason"] = ""
                rec["verified"] = False
                rec["contact_verified"] = 0
                rec["inference"] = detail
            else:
                rec["inference"] = {**detail, "not_inferred": True}
        return rec
    rec["verified"] = True
    rec["email_source"] = source
    if is_role(email) and fields["person"]:
        rec["status"] = "named_person_only"
    else:
        rec["status"] = "accepted"
    if fields["phone"] and not row.get("phone"):
        rec["phone_kept"] = fields["phone"]
    return rec


def _last_written(audit_path: Path, company: str) -> dict | None:
    """Most recent audit record in which this tool wrote an email for the company."""
    last = None
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("company") == company and rec.get("email_written"):
                last = rec
    return last


def write_decision(conn: sqlite3.Connection, rec: dict, row: dict, audit_path: Path) -> None:
    """Persist one decision, then append it to the audit log (DB first, so a crash
    cannot cause a re-spend; the verified guard in select_gaps also covers it).

    Rules:
      * a role inbox never overwrites a non-empty cached email;
      * a verified name is written only when the cached email ends up equal to the
        verified one, and an empty person never blanks a name already stored;
      * when a later decision rejects a company whose verified email this tool wrote
        earlier, that email is reverted to the pre-tool value (prev_email from the audit).
    """
    key = row["company_name"]
    rec.setdefault("prev_email", (row.get("email") or ""))
    if rec["status"] in ("accepted", "named_person_only"):
        existing = (row.get("email") or "").strip()
        new_email = rec["email"]
        if is_role(new_email) and "@" in existing:
            final_email = existing
        else:
            final_email = new_email
        if final_email.lower() == new_email.lower():
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            phone = rec.get("phone_kept", "")
            person = rec.get("person", "") or ""
            conn.execute(
                "UPDATE companies SET email=?, "
                "contact_name=CASE WHEN ?<>'' THEN ? ELSE contact_name END, "
                "contact_source=?, contact_verified=1, "
                "phone=CASE WHEN ?<>'' AND (phone IS NULL OR phone='') THEN ? ELSE phone END, "
                "updated_at=? WHERE company_name=?",
                (final_email, person, person, rec["email_source"], phone, phone, now, key),
            )
            conn.commit()
            rec["email_written"] = True
        else:
            # Verified role inbox, but the cache already holds a different non-empty email.
            rec["rejected_reason"] = "existing_email_kept"
            if rec["status"] == "accepted":
                rec["status"] = "unknown"
    elif rec["status"] == ROLE_INFERRED:
        # Never downgrades a verified contact. Never overwrites a different non-empty
        # cached email (same rule as a verified role inbox).
        cur = conn.execute("SELECT email, COALESCE(contact_verified, 0) FROM companies "
                           "WHERE company_name=?", (key,)).fetchone()
        existing = (cur[0] if cur else "") or ""
        new_email = rec["email"]
        if cur and cur[1] == 1:
            rec["reason"] = "existing_verified_kept"
        elif existing.strip() and "@" in existing and existing.strip().lower() != new_email.lower():
            rec["reason"] = "existing_email_kept"
        else:
            now = datetime.now(timezone.utc).isoformat(timespec='seconds')
            person = (rec.get("person") or "").strip()
            conn.execute(
                "UPDATE companies SET email=?, "
                "contact_name=CASE WHEN ?<>'' THEN ? ELSE contact_name END, "
                "contact_source=?, contact_verified=0, updated_at=? WHERE company_name=?",
                (new_email, person, person, ROLE_INFERRED, now, key))
            conn.commit()
            rec["email_written"] = True
    else:
        prior = _last_written(audit_path, key)
        if prior:
            cur = conn.execute("SELECT email, contact_verified FROM companies WHERE company_name=?",
                               (key,)).fetchone()
            if cur and (cur[0] or "") == prior.get("email", "") and cur[1] == 1:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE companies SET email=?, contact_name='', contact_source='', "
                    "contact_verified=0, updated_at=? WHERE company_name=?",
                    (prior.get("prev_email", ""), now, key))
                conn.commit()
                rec["reverted_email_to"] = prior.get("prev_email", "")
    if rec["status"] in ("rejected", "unknown") and not rec.get("email_written"):
        _store_unverified_person(conn, rec, key)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _provenance(rec: dict) -> str:
    """Where the person came from: the Task sheet, or the chat reply by its hash."""
    if rec.get("sheet_url"):
        return f"sheet:{rec['sheet_url']}"
    if rec.get("model_reply_hash"):
        return f"reply:{rec['model_reply_hash']}"
    return ""


def _store_unverified_person(conn: sqlite3.Connection, rec: dict, key: str) -> None:
    """Keep a named person from a rejected or unknown row as 'who to ask for'.

    Stored with contact_verified=0 and no email. Never touches a row whose contact
    is verified, and an empty person never blanks a name already stored."""
    person = (rec.get("person") or "").strip()
    if not person:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE companies SET contact_name=?, contact_source=?, updated_at=? "
        "WHERE company_name=? AND COALESCE(contact_verified, 0)=0",
        (person, _provenance(rec), now, key),
    )
    conn.commit()
    if cur.rowcount:
        rec["person_stored"] = True
        rec["contact_source"] = _provenance(rec)


def report(conn: sqlite3.Connection, audit_path: Path) -> dict:
    """Counts for the gap rows: what is verified, what is only a named person, and
    what is still open. Read-only."""
    out = {"gap_rows": 0, "no_email": 0, "role_only": 0, "verified_email": 0,
           "named_person_only_rows": 0, "still_open": 0}
    rows = conn.execute("SELECT company_name, unmasked_tradename, email, "
                        "COALESCE(contact_name,''), COALESCE(contact_verified,0) FROM companies").fetchall()
    for company, unmasked, email, name, verified in rows:
        if not is_gap(email or "") or (P.NUMBERED_RE.match(company) and not unmasked):
            continue
        out["gap_rows"] += 1
        if not (email or "").strip():
            out["no_email"] += 1
        else:
            out["role_only"] += 1
        if verified and is_role(email or "") and name:
            out["named_person_only_rows"] += 1
        elif verified:
            out["verified_email"] += 1
        else:
            out["still_open"] += 1
    audit: dict[str, int] = {}
    reasons: dict[str, int] = {}
    if audit_path.exists():
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            audit[rec.get("status", "")] = audit.get(rec.get("status", ""), 0) + 1
            if rec.get("status") == "rejected":
                reasons[rec.get("rejected_reason", "")] = reasons.get(rec.get("rejected_reason", ""), 0) + 1
    out["audit_status"] = audit
    out["rejected_reasons"] = reasons
    return out


def run(conn: sqlite3.Connection, rows: list[dict], *, batch_size: int, bridge_retry: int,
        audit_path: Path, dry_run: bool = False, replay: list[str] | None = None,
        ask=ask_bridge, verify=verify_contact, out=print, quiet: bool = False,
        model: str = DEFAULT_MODEL, budget: CallBudget | None = None,
        escalation: bool = False, collect: list[dict] | None = None,
        accept_own_domain_role: bool = False) -> dict:
    """Run the batches. Each bridge attempt is charged to `budget` before it is made.
    A spent pool stops the run cleanly; records already written are kept.
    `collect`, when given, receives every decision record (used by --escalate)."""
    counts = {"selected": len(rows), "accepted": 0, "named_person_only": 0, "rejected": 0,
              "unknown": 0, "no_line": 0, "bridge_failed": 0, ROLE_INFERRED: 0}
    replay_iter = iter(replay or [])
    attempts = 1 + max(0, bridge_retry)
    stopped = ""
    for batch_no, start in enumerate(range(0, len(rows), batch_size), 1):
        batch = rows[start:start + batch_size]
        names = [r["display"] for r in batch]
        prompt = build_prompt(names)
        if dry_run:
            out(f"--- batch {batch_no} prompt ({len(prompt)} chars) ---\n{prompt}")
            if replay is None:
                continue
            try:
                reply = strip_diagnostics(next(replay_iter))
            except StopIteration:
                out("replay exhausted")
                break
            parsed = parse_reply(reply, names)
            for i, row in enumerate(batch):
                f = parsed.get(i)
                out(f"  {row['display']}: " + (f["line"] if f else "NO LINE (not fabricated)"))
            continue

        def guarded(prompt: str, _names: list[str] = names) -> tuple[bool, str, str]:
            if budget is not None:
                pool = budget.reserve(model, _names, escalation=escalation)
                if pool is not None:
                    raise BudgetExhausted(pool)
            return ask(prompt)

        parsed, hashes, successes, last_err = collect_replies(prompt, names, attempts, ask=guarded)
        exhausted = last_err.startswith("budget:")
        if exhausted:
            stopped = last_err.split(":", 1)[1]
        if successes == 0:
            if exhausted:
                out(_budget_line(budget, stopped, f"stopping before batch {batch_no}"))
                break
            for row in batch:
                rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "company": row["company_name"], "person": "", "email": "",
                       "email_source": "", "verified": False,
                       "rejected_reason": f"bridge_failed: {last_err}", "model_reply_hash": "",
                       "status": "bridge_failed", "batch": batch_no, "phone_kept": "",
                       "name_prompted": row["display"], "model": model, "escalated": escalation}
                write_decision(conn, rec, row, audit_path)
                counts["bridge_failed"] += 1
                if collect is not None:
                    collect.append(rec)
            out(f"batch {batch_no}: bridge failed after {attempts} attempt(s): {last_err}")
            continue
        for i, row in enumerate(batch):
            fields = parsed.get(i)
            rec = decide(row, fields, hashes.get(i, ""), batch_no, verify=verify,
                         accept_own_domain_role=accept_own_domain_role)
            rec["model"] = model
            rec["escalated"] = escalation
            write_decision(conn, rec, row, audit_path)
            if collect is not None:
                collect.append(rec)
            if rec["status"] == ROLE_INFERRED:
                counts[ROLE_INFERRED] += 1
                if not quiet and rec.get("email_written"):
                    out(f"  INFERRED {row['display']}: {rec['email']} own-domain role inbox, "
                        f"contact_verified=0 [{model}]")
            elif rec["status"] in ("accepted", "named_person_only"):
                counts[rec["status"]] += 1
                if not quiet:
                    out(f"  {rec['status'].upper()} {row['display']}: {rec['person'] or '-'} "
                        f"<{rec['email']}> via {rec['email_source']} [{model}]")
            elif rec["status"] == "rejected":
                counts["rejected"] += 1
                out(f"  REJECTED {row['display']}: {rec['email']} reason={rec['rejected_reason']} [{model}]")
            elif rec["status"] == "unknown":
                counts["unknown"] += 1
            else:
                counts["no_line"] += 1
                if not quiet:
                    out(f"  NO LINE {row['display']} (retry next run)")
        if exhausted:
            out(_budget_line(budget, stopped, f"stopped after batch {batch_no}; its partial reply was kept"))
            break
    counts["budget_stop"] = stopped or "none"
    return counts


def _budget_line(budget: CallBudget | None, pool: str, where: str) -> str:
    if budget is None or pool not in POOLS:
        return f"budget: {pool} pool spent; {where}"
    st = budget.status()[pool]
    return (f"budget: {pool} pool spent for {budget.today} "
            f"({st['used']}/{st['limit']} used); {where}")



# ---------------------------------------------------------------------------
# --via-task: Gemini Task batches through a Google Sheet (see gemini_task_sheet.py)
# ---------------------------------------------------------------------------
TASK_STATUS_KEYS = ("accepted", "named_person_only", "rejected", "unknown", "no_line", ROLE_INFERRED)


def task_plan(rows: list[dict], batch_size: int, addresses: dict[str, str],
               permits: dict[str, dict] | None = None) -> list[dict]:
    """The batches a run would make, without touching anything. Used by --dry-run.
    Permit context (Community, Est. Value, Scope) is joined by display name, then by
    cache company name; with permits=None those columns stay blank."""
    plan = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        for r in batch:
            r["address"] = addresses.get(r["company_name"], "")
            r.update(H.permit_fields(permits, r["display"], r["company_name"]))
        plan.append({"batch": start // batch_size + 1, "companies": batch})
    return plan


def print_task_plan(rows: list[dict], batch_size: int, addresses: dict[str, str],
                    out=print, timeout: int = H.DEFAULT_TIMEOUT,
                    permits: dict[str, dict] | None = None) -> int:
    plan = task_plan(rows, batch_size, addresses, permits)
    out(f"--via-task DRY RUN: {len(rows)} gap companies in {len(plan)} batch(es) of up to "
        f"{batch_size}; nothing is called, no sheet is created, no quota is spent")
    for b in plan:
        batch = b["companies"]
        sid, url = "<SHEET_ID>", "https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit"
        out(f"\n=== batch {b['batch']}: {len(batch)} company(ies)")
        out(f"sheet title: {H.sheet_title()}  (tab {H.TAB}; the timestamp is taken at run time)")
        out("sheet rows (append to A1):")
        for row in H.sheet_rows(batch):
            out("  " + json.dumps(row, ensure_ascii=False))
        out("job instructions:")
        for line in H.job_instructions(sid, url, len(batch)).splitlines():
            out("  " + line)
        out("command sequence:")
        for line in H.command_sequence(sid, "<JOB_ID>", timeout):
            out("  " + line)
    return 0


def _task_fields(cells: list[str]) -> dict | None:
    """Map one returned sheet row onto the fields decide() expects. None means the
    row was not filled at all (Email cell empty)."""
    email_cell = cells[H.COL_EMAIL].strip()
    if not email_cell:
        return None
    if email_cell.lower() == H.NONE_PUBLISHED:
        email_raw = "UNKNOWN"        # decide() treats UNKNOWN as "no email published"
    else:
        email_raw = email_cell
    person_all = clean_person(cells[H.COL_PERSON])
    return {
        "person": first_person(person_all),
        "person_all": person_all,
        "email_raw": email_raw,
        "urls": H.urls_in(cells[H.COL_SOURCE]),   # empty -> verify_contact falls back to the company site
        "phone": "",
        "line": " | ".join(cells),
    }


def task_decisions(batch: list[dict], values: list[list[str]], *, batch_no: int, job_id: str,
                   sheet_url: str, verify=verify_contact,
                   accept_own_domain_role: bool = False) -> list[tuple[dict, dict]]:
    """Verify every returned row. Nothing is written here; the caller writes the whole batch."""
    names = [r["display"] for r in batch]
    by_row: dict[int, list[str]] = {}
    for raw in values[1:]:                       # values[0] is the header row
        cells = H.pad(raw)
        idx = match_index(cells[H.COL_COMPANY], names)
        if idx is not None and idx not in by_row:
            by_row[idx] = cells
    out: list[tuple[dict, dict]] = []
    for i, row in enumerate(batch):
        cells = by_row.get(i)
        fields = _task_fields(cells) if cells is not None else None
        rhash = H.row_hash(cells) if cells is not None else ""
        rec = decide(row, fields, "", batch_no, verify=verify,
                     accept_own_domain_role=accept_own_domain_role)
        if cells is not None and fields is None:
            rec["rejected_reason"] = "empty_email_cell"
        rec.update({
            "mode": "task",
            "sheet_url": sheet_url,
            "job_id": job_id,
            "row_hash": rhash,
            "sheet_status": cells[H.COL_STATUS].strip() if cells is not None else "",
            "model": "task",
            "model_reply_hash": "",
            "escalated": False,
        })
        out.append((row, rec))
    return out


def run_task(conn: sqlite3.Connection, rows: list[dict], *, batch_size: int, task_timeout: int,
             audit_path: Path, budget: CallBudget, keep_sheets: bool, out=print,
             verify=verify_contact, permits: dict[str, dict] | None = None,
             accept_own_domain_role: bool = False) -> dict:
    """Run the --via-task pipeline. Stops at the first failed batch; a batch writes
    nothing until its sheet has been read back and every row verified."""
    counts = {"selected": len(rows), "batches": 0, **{k: 0 for k in TASK_STATUS_KEYS},
              "stopped": "none"}
    addresses = {r[0]: r[1] or "" for r in conn.execute("SELECT company_name, address FROM companies")}
    plan = task_plan(rows, batch_size, addresses, permits)
    for b in plan:
        batch = b["companies"]
        names = [r["display"] for r in batch]
        sid = ""
        job_id = ""
        sheet_url = ""
        try:
            H.check_quota()
            spent = budget.reserve("task", names)
            if spent is not None:
                out(_budget_line(budget, spent, "no sheet created, no task submitted"))
                counts["stopped"] = f"budget:{spent}"
                break

            title = H.sheet_title()
            token = H.call(H.gog_token_argv()).strip()
            sid, sheet_url = H.parse_create(H.call(H.create_argv(title), env=H.gog_env(token)))
            out(f"batch {b['batch']}: sheet {title} {sheet_url}")
            H.call(H.append_argv(sid, H.sheet_rows(batch)))

            job_id = H.parse_job_id(H.call(H.submit_argv(H.job_instructions(sid, sheet_url, len(batch)))))
            H.check_quota()                       # again, immediately before the Gemini call
            rc, _o, err = H.run(H.trigger_argv(job_id), timeout=H.CMD_TIMEOUT)
            if rc != 0:
                try:
                    H.call(H.abandon_argv(job_id))
                except TaskError:
                    pass
                raise TaskError(f"gemini-task-trigger exit {rc} for job {job_id}: {err.strip()[:200]}")
            try:
                H.call(H.quota_argv("record"))
            except TaskError as exc:
                out(f"warning: quota record failed after a successful trigger: {exc}")
            out(f"batch {b['batch']}: job {job_id} triggered; awaiting result (timeout {task_timeout}s)")

            rc, _o, err = H.run(H.await_argv(job_id, task_timeout), timeout=task_timeout + H.AWAIT_SLACK)
            if rc != 0:
                raise TaskError(f"await for job {job_id} ended with exit {rc}: {err.strip()[:200]}")

            values = H.parse_values(H.call(H.get_argv(sid, len(batch))))
        except QuotaRefused as exc:
            out(f"task not submitted: {exc}")
            counts["stopped"] = "quota"
            break
        except TaskError as exc:
            # Nothing has been written for this batch. Report the pieces needed to resume by hand.
            where = f" sheet={sheet_url or '-'} job={job_id or '-'}" if (sid or job_id) else ""
            out(f"batch {b['batch']} stopped, nothing written for it: {exc}.{where}")
            counts["stopped"] = f"task_failed: {exc}"
            break

        counts["batches"] += 1
        decided = task_decisions(batch, values, batch_no=b["batch"], job_id=job_id,
                                 sheet_url=sheet_url, verify=verify,
                                 accept_own_domain_role=accept_own_domain_role)
        for row, rec in decided:
            write_decision(conn, rec, row, audit_path)
            counts[_task_bucket(rec)] += 1
        out(f"batch {b['batch']}: {len(decided)} row(s) written to cache and audit")
        if not keep_sheets and sid:
            try:
                H.call(H.trash_argv(sid))
            except TaskError as exc:
                out(f"warning: could not trash sheet {sid}: {exc}")
    return counts


def _task_bucket(rec: dict) -> str:
    if rec["status"] == "no_line":
        return "no_line"
    if rec["status"] == "unknown":
        return "unknown"
    if rec["status"] == "rejected":
        return "rejected"
    if rec["status"] == "named_person_only":
        return "named_person_only"
    if rec["status"] == ROLE_INFERRED:
        return ROLE_INFERRED
    return "accepted"


def _addresses(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] or "" for r in conn.execute("SELECT company_name, address FROM companies")}


def _current_only(args) -> bool:
    """--current-only defaults ON for --via-task and OFF for the chat path."""
    return args.via_task if args.current_only is None else args.current_only


def _main_task(args, budget: CallBudget, audit_path: Path, names: set[str] | None) -> int:
    if args.dry_run:
        # Read-only: a dry run must not migrate or write the cache.
        conn = sqlite3.connect(f"file:{P.DB_PATH}?mode=ro", uri=True)
    else:
        conn = P.init_db()
    permits = H.load_permit_index()          # missing CSVs are skipped; columns stay blank
    current_only = _current_only(args)
    try:
        done = frozenset() if args.redo else frozenset(load_done(audit_path))
        rows = select_gaps(conn, limit=args.limit, redo=args.redo, done=done, names=names,
                           permits=permits, current_only=current_only)
        if args.dry_run:
            print_priority(rows)
            print_task_plan(rows, args.task_batch, _addresses(conn), timeout=args.task_timeout,
                            permits=permits)
            try:
                st = budget.status()["task"]
                print(f"budget: task used={st['used']} remaining={st['remaining']} limit={st['limit']} "
                      f"(read only; no quota or budget charge)")
            except (ValueError, OSError) as exc:
                print(f"budget file unreadable: {exc}")
            return 0
        if not args.quiet:
            print(f"gap companies to process: {len(rows)} (mode=task, batch={args.task_batch})")
        if not rows:
            print("GEMINI_CONTACTS mode=task selected=0")
            return 0
        try:
            counts = run_task(conn, rows, batch_size=args.task_batch, task_timeout=args.task_timeout,
                              audit_path=audit_path, budget=budget, keep_sheets=args.task_keep_sheets,
                              out=(lambda *_a, **_k: None) if args.quiet else print,
                              permits=permits,
                              accept_own_domain_role=args.accept_own_domain_role)
        except (ValueError, OSError) as exc:
            print(f"budget file unreadable, no tasks submitted: {exc}")
            return 2
    finally:
        conn.close()
    print("GEMINI_CONTACTS mode=task " + " ".join(f"{k}={v}" for k, v in counts.items()))
    P.logger.info("gemini contacts task %s", counts)
    if counts["stopped"] == "quota":
        return 3
    if counts["stopped"].startswith("task_failed"):
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="max companies to process")
    ap.add_argument("--batch", type=int, default=4, help="companies per prompt (default 4)")
    ap.add_argument("--model", choices=BRIDGE_MODELS, default=DEFAULT_MODEL,
                    help="bridge model passed through to the bridge script (default hydrogen)")
    ap.add_argument("--escalate", action="store_true",
                    help="after the Hydrogen pass, retry only UNKNOWN/rejected companies once on Neon "
                         "(needs --model hydrogen; spends the neon and escalation pools)")
    ap.add_argument("--max-calls-per-day-hydrogen", type=int, default=DEFAULT_LIMITS["hydrogen"],
                    help="daily cap on Hydrogen calls (default 6)")
    ap.add_argument("--max-calls-per-day-neon", type=int, default=DEFAULT_LIMITS["neon"],
                    help="daily cap on Neon calls, escalation included (default 2)")
    ap.add_argument("--escalate-calls-per-day", type=int, default=DEFAULT_LIMITS["escalation"],
                    help="daily cap on --escalate Neon calls (default 1)")
    ap.add_argument("--via-task", action="store_true",
                    help="enrich via Gemini Task batches: each batch is a Google Sheet the Task fills in; "
                         "every returned email is verified before it is written (default: the chat path)")
    ap.add_argument("--task-batch", type=int, default=H.DEFAULT_BATCH,
                    help=f"companies per Task call, 1-{H.MAX_BATCH} (default {H.DEFAULT_BATCH}; --via-task only)")
    ap.add_argument("--task-timeout", type=int, default=H.DEFAULT_TIMEOUT,
                    help=f"seconds to wait for each Task result (default {H.DEFAULT_TIMEOUT}; --via-task only)")
    ap.add_argument("--task-keep-sheets", action=argparse.BooleanOptionalAction, default=True,
                    help="keep each batch's Google Sheet in Drive (default on); --no-task-keep-sheets trashes it "
                         "after the batch is written")
    ap.add_argument("--max-calls-per-day-task", type=int, default=DEFAULT_LIMITS["task"],
                    help="daily cap on --via-task Task submissions (default 2)")
    ap.add_argument("--calls-status", action="store_true",
                    help="print today's date and each pool's used/remaining; no bridge call")
    ap.add_argument("--dry-run", action="store_true", help="print prompts; no bridge call, no writes")
    ap.add_argument("--replay", metavar="FILE", help="JSON list of captured replies (dry-run only)")
    ap.add_argument("--redo", action="store_true", help="ignore the audit log and re-run gap companies")
    ap.add_argument("--bridge-retry", type=int, default=1,
                    help="extra bridge calls per batch after a failure or a short reply (default 1); each counts")
    ap.add_argument("--quiet", action="store_true", help="only the summary line")
    ap.add_argument("--accept-own-domain-role", action="store_true",
                    help="opt-in (default OFF): when verification fails, keep a role inbox "
                         "(info@, sales@, ...) on the company's own domain with a valid MX as an "
                         "INFERRED contact: contact_verified=0, status own_domain_role_inferred")
    ap.add_argument("--names-file", metavar="FILE",
                    help="restrict to the company names listed one per line (used by pipeline.py)")
    ap.add_argument("--current-only", action=argparse.BooleanOptionalAction, default=None,
                    help="only companies present in this week's lead CSVs (default ON for --via-task, "
                         "OFF for the chat path). Gap companies are always taken highest permit value first.")
    ap.add_argument("--report", action="store_true",
                    help="print gap-row and audit counts; no bridge call, no writes")
    ap.add_argument("--audit-log", default=str(AUDIT_LOG), help=argparse.SUPPRESS)
    ap.add_argument("--calls-file", default=str(CALLS_FILE), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.replay and not args.dry_run:
        ap.error("--replay requires --dry-run")
    if args.batch < 1:
        ap.error("--batch must be >= 1")
    for flag in ("max_calls_per_day_hydrogen", "max_calls_per_day_neon", "escalate_calls_per_day"):
        if getattr(args, flag) < 0:
            ap.error(f"--{flag.replace('_', '-')} must be >= 0")
    if args.escalate and args.model != "hydrogen":
        ap.error("--escalate needs --model hydrogen (it escalates Hydrogen's misses to Neon)")
    if args.via_task:
        if not 1 <= args.task_batch <= H.MAX_BATCH:
            ap.error(f"--task-batch must be between 1 and {H.MAX_BATCH}")
        if args.task_timeout < 1:
            ap.error("--task-timeout must be >= 1")
        if args.max_calls_per_day_task < 0:
            ap.error("--max-calls-per-day-task must be >= 0")
        if args.escalate or args.replay or args.model != DEFAULT_MODEL:
            ap.error("--via-task does not combine with --escalate, --replay or --model "
                     "(the Task surface picks its own model)")

    limits = {"hydrogen": args.max_calls_per_day_hydrogen, "neon": args.max_calls_per_day_neon,
              "escalation": args.escalate_calls_per_day, "task": args.max_calls_per_day_task}
    calls_path = Path(args.calls_file)
    budget = CallBudget(calls_path, limits)

    if args.calls_status:
        st = budget.status()
        print(f"calls status date={st['date']} file={calls_path}")
        for pool in POOLS:
            p = st[pool]
            print(f"  {pool}: used={p['used']} remaining={p['remaining']} limit={p['limit']}")
        return 0

    replay = None
    if args.replay:
        replay = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        if not isinstance(replay, list):
            ap.error("--replay FILE must hold a JSON list of reply strings")

    names = None
    if args.names_file:
        names = {P.cache_key(ln) for ln in Path(args.names_file).read_text(encoding="utf-8").splitlines()
                 if ln.strip()}

    audit_path = Path(args.audit_log)
    if args.report:
        conn = P.init_db()
        try:
            r = report(conn, audit_path)
        finally:
            conn.close()
        print("GEMINI_REPORT " + json.dumps(r, sort_keys=True))
        return 0
    if args.via_task:
        return _main_task(args, budget, audit_path, names)
    conn = P.init_db()
    permits = H.load_permit_index()
    try:
        done = frozenset() if args.redo else frozenset(load_done(audit_path))
        rows = select_gaps(conn, limit=args.limit, redo=args.redo, done=done, names=names,
                           permits=permits, current_only=_current_only(args))
        if args.dry_run:
            print_priority(rows)
        if not args.quiet:
            print(f"gap companies to process: {len(rows)} (batch={args.batch}, model={args.model})")
        ask = lambda prompt: ask_bridge(prompt, model=args.model)  # noqa: E731
        records: list[dict] = []
        try:
            counts = run(conn, rows, batch_size=args.batch, bridge_retry=args.bridge_retry,
                         audit_path=audit_path, dry_run=args.dry_run, replay=replay,
                         quiet=args.quiet, ask=ask, model=args.model, budget=budget,
                         collect=records, accept_own_domain_role=args.accept_own_domain_role)
            counts["escalated"] = 0
            counts["escalation_resolved"] = 0
            if args.escalate and not args.dry_run:
                rows_by_key = {r["company_name"]: r for r in rows}
                esc_rows = escalation_candidates(records, rows_by_key, audit_path)
                counts["escalated"] = len(esc_rows)
                if esc_rows:
                    if not args.quiet:
                        print(f"escalating {len(esc_rows)} Hydrogen-unresolved company(ies) to neon")
                    esc_ask = lambda prompt: ask_bridge(prompt, model="neon")  # noqa: E731
                    ec = run(conn, esc_rows, batch_size=args.batch, bridge_retry=args.bridge_retry,
                             audit_path=audit_path, ask=esc_ask, model="neon", budget=budget,
                             escalation=True, quiet=args.quiet,
                             accept_own_domain_role=args.accept_own_domain_role)
                    counts["escalation_resolved"] = ec["accepted"] + ec["named_person_only"]
                    if ec["budget_stop"] != "none":
                        counts["budget_stop"] = ec["budget_stop"]
        except (ValueError, OSError) as exc:
            print(f"budget file unreadable, no calls made: {exc}")
            return 2
    finally:
        conn.close()
    print("GEMINI_CONTACTS " + " ".join(f"{k}={v}" for k, v in counts.items()))
    P.logger.info("gemini contacts %s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
