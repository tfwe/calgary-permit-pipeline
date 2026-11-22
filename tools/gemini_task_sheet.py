"""Helpers for the --via-task enrichment mode of tools/gemini_contacts.py.

Pure functions plus thin subprocess wrappers. Nothing here runs at import time.
Each batch of gap companies becomes one Google Sheet (tab 'Leads'). The Gemini
Task fills Person, Email, Source URL and Status in that sheet; the caller reads
the cells back and verifies every email before anything is written.

External commands, each located through an environment variable (see config.py):
  CALGARY_LEADS_GWS_BIN        sheets create / append / get, drive delete
  CALGARY_LEADS_TOKEN_SCRIPT   prints a Google access token
  CALGARY_LEADS_JOB_SCRIPT     submit / await / abandon a Gemini Task job
  CALGARY_LEADS_TRIGGER_SCRIPT fires a submitted job
  CALGARY_LEADS_QUOTA_SCRIPT   check / record the daily submission budget
Unset variables leave a $CALGARY_LEADS_* placeholder; run() refuses to execute it.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import config

GWS = config.tool_or_placeholder(config.GWS_BIN, config.TOOL_ENV["GWS"])
JOB_PY = config.tool_or_placeholder(config.JOB_SCRIPT, config.TOOL_ENV["JOB_PY"])
TRIGGER_PY = config.tool_or_placeholder(config.TRIGGER_SCRIPT, config.TOOL_ENV["TRIGGER_PY"])
QUOTA_PY = config.tool_or_placeholder(config.QUOTA_SCRIPT, config.TOOL_ENV["QUOTA_PY"])
TOKEN_PY = config.tool_or_placeholder(config.TOKEN_SCRIPT, config.TOOL_ENV["TOKEN_PY"])

TAB = "Leads"
HEADERS = ["Company", "Address", "Community", "Est. Value", "Scope",
           "Person", "Email", "Source URL", "Status"]
COL_COMPANY, COL_ADDRESS, COL_COMMUNITY, COL_VALUE, COL_SCOPE = 0, 1, 2, 3, 4
COL_PERSON, COL_EMAIL, COL_SOURCE, COL_STATUS = 5, 6, 7, 8

# Lead CSVs that carry permit context, in priority order. Each entry maps the
# logical field to that file's header. Missing files are skipped, not errors.
REPO_ROOT = Path(__file__).resolve().parents[1]
_CLIENT_COLS = {"company": "Company / Applicant", "community": "Community",
                "value": "Est. Project Value", "scope": "Project Description"}
PERMIT_CSVS: tuple[tuple[str, dict[str, str]], ...] = (
    ("calgary_leads_demo.csv", {"company": "company_name", "community": "community",
                                "value": "permit_value", "scope": "description"}),
    ("calgary_needs_builder.csv", _CLIENT_COLS),
    ("calgary_subtrade_leads.csv", _CLIENT_COLS),
)
NONE_PUBLISHED = "none published"

DEFAULT_BATCH = 15
MAX_BATCH = 15
DEFAULT_TIMEOUT = 1500
CMD_TIMEOUT = 600
AWAIT_SLACK = 120  # subprocess headroom above the job's own --timeout

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_JOB_RE = re.compile(r"^[A-Za-z0-9T_-]{8,}$")


class TaskError(RuntimeError):
    """A task-mode step failed. The batch in progress writes nothing."""


class QuotaRefused(TaskError):
    """The quota helper's check exited non-zero: nothing may be submitted."""


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
def sheet_title(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return "leads-" + now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sheet_rows(companies: list[dict]) -> list[list[str]]:
    """Header row, then one row per company. Company and Address come from the cache;
    Community, Est. Value and Scope come from the lead CSVs (see permit_fields) and
    stay blank when no permit row matches."""
    rows: list[list[str]] = [list(HEADERS)]
    for c in companies:
        row = [""] * len(HEADERS)
        row[COL_COMPANY] = c["display"]
        row[COL_ADDRESS] = c.get("address", "") or ""
        row[COL_COMMUNITY] = c.get("community", "") or ""
        row[COL_VALUE] = c.get("est_value", "") or ""
        row[COL_SCOPE] = c.get("scope", "") or ""
        rows.append(row)
    return rows


def job_instructions(sheet_id: str, sheet_url: str, n_companies: int) -> str:
    last_row = n_companies + 1
    return (
        f"Fill in a Google Sheet of Calgary contractor companies: sheet ID {sheet_id} "
        f"({sheet_url}), tab named {TAB}. Row 1 is the header. Rows 2 to {last_row} each hold one "
        f"company in column A (Company). Edit the cells in that sheet itself; do not reply with a "
        f"table instead.\n"
        f"For EVERY company row:\n"
        f"- Column F (Person): the named owner, partner or manager you find on a real page for that "
        f"company. Leave it empty if none is published.\n"
        f"- Column G (Email): the email address exactly as published on a real web page for that "
        f"company. If no email is published, write exactly: {NONE_PUBLISHED}\n"
        f"- Column H (Source URL): the full https:// URL of the page where you found the email or "
        f"the person. Leave it empty if you found nothing.\n"
        f"- Column I (Status): exactly one of: found, {NONE_PUBLISHED}, not found.\n"
        f"Rules: never guess or construct an address from a pattern (no info@ or firstname@ guesses). "
        f"If you cannot see the address on a page, write {NONE_PUBLISHED}. Do not change columns A to E. "
        f"Do not add or delete rows. When the sheet is complete, create the result document exactly "
        f"as the job header says."
    )


def sheet_range_append() -> str:
    return f"{TAB}!A1"


def sheet_range_get(n_companies: int) -> str:
    return f"{TAB}!A1:I{n_companies + 1}"


# ---------------------------------------------------------------------------
# Command lines (argv lists, never shell strings)
# ---------------------------------------------------------------------------
def create_argv(title: str) -> list[str]:
    return [GWS, "sheets", "create", "--title", title, "--sheet-name", TAB]


def append_argv(sheet_id: str, rows: list[list[str]]) -> list[str]:
    return [GWS, "sheets", "append", sheet_id, sheet_range_append(),
            "--values", json.dumps(rows, ensure_ascii=False)]


def get_argv(sheet_id: str, n_companies: int) -> list[str]:
    return [GWS, "sheets", "get", sheet_id, sheet_range_get(n_companies)]


def submit_argv(text: str) -> list[str]:
    return [JOB_PY, "submit", text]


def trigger_argv(job_id: str) -> list[str]:
    return [TRIGGER_PY, job_id]


def await_argv(job_id: str, timeout: int) -> list[str]:
    return [JOB_PY, "await", job_id, "--timeout", str(timeout)]


def abandon_argv(job_id: str) -> list[str]:
    return [JOB_PY, "abandon", job_id]


def quota_argv(verb: str) -> list[str]:
    return [QUOTA_PY, verb]


def trash_argv(sheet_id: str) -> list[str]:
    return [GWS, "drive", "delete", sheet_id]


def gog_token_argv() -> list[str]:
    return ["python3", TOKEN_PY]


def gog_env(token: str) -> dict:
    return {**os.environ, "GOG_ACCESS_TOKEN": token}


def command_sequence(sheet_id: str = "<SHEET_ID>", job_id: str = "<JOB_ID>",
                     timeout: int = DEFAULT_TIMEOUT) -> list[str]:
    """Human-readable command sequence for --dry-run. Never executed."""
    return [
        "1. quota:  " + " ".join(quota_argv("check")) + "   (non-zero exit aborts the batch)",
        "2. token:  GOG_ACCESS_TOKEN=$(python3 " + TOKEN_PY + ") " + GWS
        + " sheets create --title <leads-UTC-timestamp> --sheet-name " + TAB,
        "3. rows:   " + GWS + " sheets append " + sheet_id + " '" + sheet_range_append()
        + "' --values '<json rows>'",
        "4. job:    " + " ".join([JOB_PY, "submit", "'<instructions>'"]) + "  -> <JOB_ID>",
        "5. quota:  " + " ".join(quota_argv("check")) + "   (again, immediately before the trigger)",
        "6. fire:   " + " ".join([TRIGGER_PY, job_id]) + "   (exit 0 = submitted)",
        "7. record: " + " ".join(quota_argv("record")),
        "8. await:  " + " ".join([JOB_PY, "await", job_id, "--timeout", str(timeout)]),
        "9. read:   " + GWS + " sheets get " + sheet_id + " '" + sheet_range_get(DEFAULT_BATCH) + "'",
        "10. verify each email, then write cache + audit (nothing is written before step 9 succeeds)",
    ]


# ---------------------------------------------------------------------------
# Subprocess wrappers
# ---------------------------------------------------------------------------
def run(argv: list[str], *, env: dict | None = None, timeout: int = CMD_TIMEOUT):
    """Return (returncode, stdout, stderr). Launch failures and timeouts become TaskError."""
    unset = config.unset_tool_error(argv)
    if unset:
        raise TaskError(unset)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise TaskError(f"timed out after {timeout}s: {argv[0]} {argv[1] if len(argv) > 1 else ''}")
    except OSError as exc:
        raise TaskError(f"could not start {argv[0]}: {exc}")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def call(argv: list[str], *, env: dict | None = None, timeout: int = CMD_TIMEOUT) -> str:
    rc, out, err = run(argv, env=env, timeout=timeout)
    if rc != 0:
        detail = (err.strip() or out.strip() or f"exit {rc}")[:400]
        raise TaskError(f"{' '.join(argv[:3])} failed (exit {rc}): {detail}")
    return out


def check_quota() -> None:
    """Consult the owner's rate governor. Raises QuotaRefused on any non-zero exit."""
    rc, out, err = run(quota_argv("check"), timeout=60)
    if rc != 0:
        detail = (out.strip() or err.strip() or f"exit {rc}")[:300]
        raise QuotaRefused(f"quota check refused (exit {rc}): {detail}")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_create(stdout: str, fallback_url: bool = True) -> tuple[str, str]:
    try:
        data = json.loads(stdout)
    except ValueError:
        raise TaskError("sheets create printed no JSON")
    sid = str(data.get("spreadsheetId") or "") if isinstance(data, dict) else ""
    if not _SID_RE.match(sid):
        raise TaskError("sheets create returned no spreadsheetId")
    url = str(data.get("spreadsheetUrl") or "") if isinstance(data, dict) else ""
    if not url and fallback_url:
        url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    return sid, url


def parse_job_id(stdout: str) -> str:
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines or not _JOB_RE.match(lines[-1]):
        raise TaskError("job submit printed no job id")
    return lines[-1]


def parse_values(stdout: str) -> list[list[str]]:
    """Accepts a bare JSON array of rows or {'values': [...]}."""
    try:
        data = json.loads(stdout) if stdout.strip() else []
    except ValueError:
        raise TaskError("sheets get printed no JSON")
    if isinstance(data, dict):
        data = data.get("values", [])
    if not isinstance(data, list):
        raise TaskError("sheets get returned an unexpected shape")
    return [[("" if c is None else str(c)) for c in row] if isinstance(row, list) else []
            for row in data]


def pad(row: list[str], width: int = len(HEADERS)) -> list[str]:
    return (list(row) + [""] * width)[:width]


def urls_in(cell: str) -> list[str]:
    """Real http(s) URLs in a Source URL cell. Anything else yields no URL, so the
    caller falls back to the company website pages."""
    out: list[str] = []
    for tok in re.split(r"\s+|;", cell or ""):
        tok = tok.strip(" <>()[]'\"")
        if re.match(r"^https?://[^\s/]+\.[^\s/]+", tok, re.I) and tok not in out:
            out.append(tok)
    return out


def row_hash(row: list[str]) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Permit context from the lead CSVs (Community, Est. Value, Scope)
# ---------------------------------------------------------------------------
def norm_company(name: str | None) -> str:
    """Join key: case and whitespace are ignored, nothing else."""
    return " ".join((name or "").split()).casefold()


def parse_value(raw: str | None) -> float | None:
    """'2500000.0', '$2,013,300' -> float. Anything without a number -> None."""
    s = (raw or "").strip()
    if not re.search(r"\d", s):
        return None
    try:
        return float(re.sub(r"[^0-9.\-]", "", s))
    except ValueError:
        return None


def _format_value(num: float | None, raw: str) -> str:
    if num is None:
        return (raw or "").strip()
    return f"${num:,.0f}"


def load_permit_index(sources=PERMIT_CSVS, repo: Path = REPO_ROOT) -> dict[str, dict]:
    """{normalised company: {community, est_value, scope}} from the lead CSVs.

    Several permits for one company: the highest estimated value wins. A permit with
    no value ranks below any with one. On a tie the earlier source wins, so
    calgary_leads_demo.csv is preferred. A missing or unreadable file is skipped.
    """
    index: dict[str, dict] = {}
    rank_of: dict[str, tuple] = {}
    order = 0
    for filename, cols in sources:
        path = Path(repo) / filename
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for rec in reader:
                    order += 1
                    key = norm_company(rec.get(cols["company"]))
                    if not key:
                        continue
                    num = parse_value(rec.get(cols["value"]))
                    rank = (num is not None, num or 0.0, -order)
                    if key in rank_of and rank_of[key] >= rank:
                        continue
                    rank_of[key] = rank
                    index[key] = {
                        "community": (rec.get(cols["community"]) or "").strip(),
                        "est_value": _format_value(num, rec.get(cols["value"]) or ""),
                        "scope": (rec.get(cols["scope"]) or "").strip(),
                    }
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return index


def permit_fields(index: dict[str, dict] | None, *names: str) -> dict[str, str]:
    """Community / est_value / scope for the first name that matches. Blank otherwise."""
    blank = {"community": "", "est_value": "", "scope": ""}
    if not index:
        return blank
    for name in names:
        key = norm_company(name)
        if key and key in index:
            return dict(index[key])
    return blank
