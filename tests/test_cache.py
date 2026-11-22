"""The enrichment cache has an age-based TTL.

Blank records (company found, no contact channel) must be retried soon; records
that already carry a channel can live for months. Getting this wrong either
re-queries Serper forever or leaves a company permanently blank.
"""

from datetime import datetime, timedelta, timezone

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, check_eq, finish


def iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


with_contact = {"company_name": "X", "phone": "4031234567", "updated_at": iso_days_ago(30)}
check("contact record inside max_age is fresh", P._cache_is_fresh(with_contact, 90, 7) is True)
check("contact record past max_age is stale", P._cache_is_fresh(with_contact, 20, 7) is False)

blank = {"company_name": "Y", "phone": "", "email": "", "website": "",
         "updated_at": iso_days_ago(10)}
check("blank record past empty_retry is stale", P._cache_is_fresh(blank, 90, 7) is False)

blank_recent = dict(blank, updated_at=iso_days_ago(3))
check("blank record inside empty_retry is fresh", P._cache_is_fresh(blank_recent, 90, 7) is True)

check("missing timestamp counts as stale", P._cache_is_fresh({"company_name": "Z"}, 90, 7) is False)
check("unparseable timestamp counts as stale",
      P._cache_is_fresh({"company_name": "Z", "updated_at": "not-a-date"}, 90, 7) is False)

age = P._cache_age_days(with_contact)
check_eq("age is parsed in days", 29 < (age or 0) < 31, True)
check_eq("age of an unparseable record is None",
         P._cache_age_days({"updated_at": "nope"}), None)

finish()
