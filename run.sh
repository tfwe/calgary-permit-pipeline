#!/usr/bin/env bash
# Unattended runner for the Calgary permit-leads pipeline.
#
#   ./run.sh                 normal run: cache is reused, only NEW companies cost a Serper credit
#   ./run.sh --refresh       re-enrich every company (one Serper credit each)
#   ./run.sh --skip-gog      everything except the Google Workspace sync
#
# Why this wrapper exists: unattended runners give you a minimal PATH and no
# interactive shell. gog does not live in /usr/bin on either host (linuxbrew on
# WSL, ~/.local/bin on Linux) and it reads its OAuth token from a file keyring
# that needs GOG_KEYRING_PASSWORD (no TTY to prompt with). Both are handled here,
# and both are checked BEFORE a run so a config problem can't burn credits.
#
# Exit codes: 0 ok | 1 no permits | 2 config (gog/SERPER_API_KEY/keyring missing)
#             3 low sellable yield | 4 Google Workspace sync failed | 5 already running
#             6 PDF report email failed (leads were still produced and uploaded)
set -uo pipefail
cd "$(dirname "$0")" || exit 1
mkdir -p logs

# --skip-gog runs the pipeline without the Google Workspace sync, so it must not
# demand the gog binary or the keyring password it does not need.
skip_gog=0
for a in "$@"; do [ "$a" = "--skip-gog" ] && skip_gog=1; done

# Minimal PATH differs by host: ~/.local/bin on Linux, linuxbrew on WSL.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if [ -d /home/linuxbrew/.linuxbrew/bin ]; then
  export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"
fi

# gogcli's own non-interactive env (exports GOG_KEYRING_PASSWORD, mode 600).
GOG_ENV="$HOME/.config/gogcli/exec-env.sh"
if [ -f "$GOG_ENV" ]; then
  # shellcheck disable=SC1090
  . "$GOG_ENV"
fi

for bin in $( [ "$skip_gog" -eq 1 ] && echo flock || echo gog flock ); do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "FAIL  config: $bin not found on PATH ($PATH)" >&2
    exit 2
  fi
done

if [ "$skip_gog" -eq 0 ] && [ -z "${GOG_KEYRING_PASSWORD:-}" ]; then
  echo "FAIL  config: GOG_KEYRING_PASSWORD unset (expected in $GOG_ENV)" >&2
  exit 2
fi

exec 9>"logs/.run.lock"
if ! flock -n 9; then
  echo "another run is already in progress" >&2
  exit 5
fi

# Keep only the 30 newest per-run logs so a recurring schedule doesn't accumulate forever.
ls -1t logs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

if [ ! -x .venv/bin/python ]; then
  echo "FAIL  config: .venv/bin/python missing (create the venv first, see README)" >&2
  exit 2
fi

ts=$(date +%Y%m%d-%H%M%S)
log="logs/run-$ts.log"
# Epoch before the run: send-weekly-pdfs.sh uses it to refuse stale HTML from a
# previous week, so a quiet week never re-emails last week's leads as fresh.
started=$(date +%s)
.venv/bin/python pipeline.py "$@" >"$log" 2>&1
code=$?

# The pipeline logs one greppable SUMMARY line; put its numbers in the alert so
# a weekly message says what was produced instead of just "ok".
summary=$(grep -h "SUMMARY" "$log" 2>/dev/null | tail -1 | sed 's/.*SUMMARY //')

case $code in
  0) echo "ok    ${summary:-sellable leads uploaded}   ($log)" ;;
  1) echo "FAIL  no permits returned                  ($log)" ;;
  2) echo "FAIL  config: gog, keyring, or key missing ($log)" ;;
  3) echo "FAIL  low yield: below --min-client-leads  ($log)" ;;
  4) echo "FAIL  Google Workspace sync failed         ($log)" ;;
  *) echo "FAIL  unexpected exit $code                ($log)" ;;
esac

# Email this week's client PDF sheets to the list owner. The recipient is fixed
# inside send-weekly-pdfs.sh (these PDFs hold business contact data and must not
# reach a third party). Skipped for --skip-gog test runs, which never touch gog.
# A report failure keeps its own exit code so the alert says "leads produced,
# email failed" instead of a generic failure.
if [ "$code" -eq 0 ] && [[ " $* " != *" --skip-gog "* ]]; then
  report_out=$(./send-weekly-pdfs.sh --since "$started" "${summary:-}" 2>&1)
  report_rc=$?
  printf '%s\n' "$report_out"
  if [ "$report_rc" -ne 0 ]; then
    echo "FAIL  report: PDF email failed (leads were still produced)  ($log)" >&2
    code=6
  fi
fi

# Weekly entry point pins the window and the yield floor: see run-weekly.sh
exit $code
