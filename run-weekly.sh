#!/usr/bin/env bash
# Weekly entry point for an external scheduler (cron, systemd timer, ...).
#
# --days 7    : a 7-day window so consecutive weekly runs don't overlap and
#               re-pitch the same permit (the default 14-day window would list
#               every permit twice across two consecutive weekly runs).
# --min-client-leads 7 : PROVISIONAL. First real 7-day run produced 25 sellable
#               (13 GC + 12 subtrade), so 7 is well under a quarter of normal and
#               will only catch a near-total enrichment failure. Raise it toward
#               ~12 (half of typical) once a few more weekly runs confirm the
#               spread; every run logs sellable=/review=/excluded= to calibrate from.
set -uo pipefail
exec "$(dirname "$0")/run.sh" --days 7 --min-client-leads 7 "$@"
