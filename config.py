"""Runtime configuration, read from environment variables.

Nothing in this project hard-codes a home directory, a host, a mailbox or a
Google Sheet. The core pipeline (Socrata ingest, Serper enrichment, CSV/HTML
export) needs none of the variables below. Each one enables a single optional
code path; leaving it unset disables only that path, with a clear error if it
is actually used. See README.md, section "Configuration".
"""
from __future__ import annotations

import os
from pathlib import Path


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Google Workspace CLI used by the Gemini Task sheet helpers (tools/gemini_task_sheet.py).
GWS_BIN = _get("CALGARY_LEADS_GWS_BIN")
# Helper that prints a short-lived Google access token on stdout.
TOKEN_SCRIPT = _get("CALGARY_LEADS_TOKEN_SCRIPT")
# Job submit / await / abandon helper for Gemini Task jobs.
JOB_SCRIPT = _get("CALGARY_LEADS_JOB_SCRIPT")
# Fires a submitted Gemini Task job.
TRIGGER_SCRIPT = _get("CALGARY_LEADS_TRIGGER_SCRIPT")
# Rate governor: `check` refuses (non-zero) when a submission is not allowed; `record` counts one.
QUOTA_SCRIPT = _get("CALGARY_LEADS_QUOTA_SCRIPT")
# Phone bridge used by tools/gemini_contacts.py: `<bridge> ask --model <m> <prompt>`.
BRIDGE_SCRIPT = _get("CALGARY_LEADS_BRIDGE_SCRIPT")

# Recipient of the weekly PDF email (send-weekly-pdfs.sh). Empty = refuse to send.
WEEKLY_PDF_TO = _get("WEEKLY_PDF_TO")

# JSON file remembering the Google Sheet / Drive IDs that pipeline.py updates in
# place. Losing it makes the next run create a second set of sheets.
SHEETS_STATE_PATH = Path(
    _get("CALGARY_LEADS_SHEETS_STATE")
    or Path.home() / ".config" / "calgary-permit-leads" / "sheets.json"
).expanduser()

# Variable name for each external-tool setting, used in error messages.
TOOL_ENV = {
    "GWS": "CALGARY_LEADS_GWS_BIN",
    "TOKEN_PY": "CALGARY_LEADS_TOKEN_SCRIPT",
    "JOB_PY": "CALGARY_LEADS_JOB_SCRIPT",
    "TRIGGER_PY": "CALGARY_LEADS_TRIGGER_SCRIPT",
    "QUOTA_PY": "CALGARY_LEADS_QUOTA_SCRIPT",
    "BRIDGE": "CALGARY_LEADS_BRIDGE_SCRIPT",
}


def tool_or_placeholder(value: str, env_name: str) -> str:
    """Return the configured path, or a visible `$ENV` placeholder.

    The placeholder keeps dry-run output readable without config. Anything that
    would execute a placeholder is refused by the caller (see unset_tool_error).
    """
    return value or f"${env_name}"


def unset_tool_error(argv: list[str]) -> str | None:
    """Name the unset variables if an argv still contains a placeholder, else None."""
    bad = [a for a in argv if isinstance(a, str) and a.startswith("$CALGARY_LEADS_")]
    if not bad:
        return None
    names = ", ".join(sorted({b[1:] for b in bad}))
    return (f"external helper not configured: set {names} to the executable path "
            f"(see README, Configuration)")
