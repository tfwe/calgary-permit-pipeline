"""Pure SQLite storage helpers for the enrichment cache (extracted from pipeline.py)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from cpp_config import *  # noqa: F401,F403

__all__ = [
    "CONTACT_COLUMNS",
    "CONTACT_COLUMN_DDL",
    "cache_key",
    "cache_get",
    "cache_put",
    "_cache_age_days",
]


CONTACT_COLUMNS = ("contact_name", "contact_source", "contact_verified")
CONTACT_COLUMN_DDL = (
    ("contact_name", "TEXT DEFAULT ''"),
    ("contact_source", "TEXT DEFAULT ''"),
    ("contact_verified", "INTEGER DEFAULT 0"),
)


def cache_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().upper()


def cache_get(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT company_name, unmasked_tradename, licence_type, website, phone, email, address, google_rating, kill_reason, updated_at, "
        "contact_name, contact_source, contact_verified "
        "FROM companies WHERE company_name = ?",
        (cache_key(name),),
    ).fetchone()
    if not row:
        return None
    keys = ["company_name", "unmasked_tradename", "licence_type", "website", "phone", "email", "address", "google_rating", "kill_reason", "updated_at",
            "contact_name", "contact_source", "contact_verified"]
    return dict(zip(keys, row))


def cache_put(conn: sqlite3.Connection, rec: dict) -> None:
    # A record without contact keys (the normal enrichment path) must not wipe a
    # contact the Gemini stage already verified, so the contact columns are only
    # overwritten when the caller supplies them.
    keep_contact = not any(k in rec for k in CONTACT_COLUMNS)
    conn.execute(
        """INSERT INTO companies
           (company_name, unmasked_tradename, licence_type, website, phone, email, address, google_rating, kill_reason, updated_at,
            contact_name, contact_source, contact_verified)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company_name) DO UPDATE SET
             unmasked_tradename=excluded.unmasked_tradename,
             licence_type=excluded.licence_type,
             website=excluded.website,
             phone=excluded.phone,
             email=excluded.email,
             address=excluded.address,
             google_rating=excluded.google_rating,
             kill_reason=excluded.kill_reason,
             updated_at=excluded.updated_at,
             contact_name=CASE WHEN ? THEN companies.contact_name ELSE excluded.contact_name END,
             contact_source=CASE WHEN ? THEN companies.contact_source ELSE excluded.contact_source END,
             contact_verified=CASE WHEN ? THEN companies.contact_verified ELSE excluded.contact_verified END""",
        (
            cache_key(rec["company_name"]),
            rec.get("unmasked_tradename", ""),
            rec.get("licence_type", ""),
            rec.get("website", ""),
            rec.get("phone", ""),
            rec.get("email", ""),
            rec.get("address", ""),
            rec.get("google_rating"),
            rec.get("kill_reason", ""),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            rec.get("contact_name", "") or "",
            rec.get("contact_source", "") or "",
            int(rec.get("contact_verified") or 0),
            keep_contact, keep_contact, keep_contact,
        ),
    )
    conn.commit()


def _cache_age_days(rec: dict) -> float | None:
    try:
        dt = datetime.fromisoformat(rec.get("updated_at") or "")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return None
