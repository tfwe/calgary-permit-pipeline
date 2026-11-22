#!/usr/bin/env bash
# Render the client HTML lead sheets to PDF and email them to the list owner.
#
# Called by run.sh after a successful pipeline run. Kept separate from run.sh so
# a PDF/email failure has its own exit code and can't be confused with a pipeline
# failure (the leads were still produced and uploaded even if the email bounced).
#
#   ./send-weekly-pdfs.sh --since <epoch> [summary text]
#
# --since <epoch>  only send HTML files modified at/after this epoch. The
#                  pipeline only writes an HTML file when that split has leads,
#                  so a missing/empty split leaves the PREVIOUS week's file on
#                  disk; without this check a quiet week would re-email stale
#                  leads as if they were new.
#
# Recipient comes from WEEKLY_PDF_TO and should be the list owner: these PDFs
# contain business contact data and must never be mailed to a third party by accident.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

PDF_TO="${WEEKLY_PDF_TO:-}"
if [ -z "$PDF_TO" ]; then
  echo "FAIL  report: WEEKLY_PDF_TO is not set; refusing to send" >&2
  exit 6
fi

SINCE=0
SUMMARY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    *) SUMMARY="$1"; shift ;;
  esac
done

# gogcli's non-interactive env (exports GOG_KEYRING_PASSWORD).
if [ -f "$HOME/.config/gogcli/exec-env.sh" ]; then
  # shellcheck disable=SC1090
  . "$HOME/.config/gogcli/exec-env.sh"
fi

if ! command -v gog >/dev/null 2>&1; then
  echo "FAIL  report: gog not found on PATH" >&2
  exit 6
fi
if ! command -v chromium >/dev/null 2>&1; then
  echo "FAIL  report: chromium not found on PATH" >&2
  exit 6
fi
if ! command -v pdfinfo >/dev/null 2>&1; then
  echo "FAIL  report: pdfinfo not found on PATH (poppler-utils)" >&2
  exit 6
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

# Render one HTML file. Echoes the PDF path on success, nothing on failure.
render() {
  local html="$1" base="$2"
  local pdf="$tmpdir/$base.pdf"
  [ -f "$html" ] || return 1
  # Freshness gate: skip a file the pipeline did not rewrite this run.
  local mtime
  mtime=$(stat -c %Y "$html" 2>/dev/null || echo 0)
  if [ "$mtime" -lt "$SINCE" ]; then
    echo "SKIP  report: $html is stale (not rewritten this run)" >&2
    return 1
  fi
  rm -f "$pdf"
  timeout 180 chromium \
    --headless=new --disable-gpu --no-sandbox \
    --user-data-dir="$tmpdir/chrome" \
    --no-pdf-header-footer \
    --print-to-pdf="$pdf" "file://$PWD/$html" >/dev/null 2>&1
  if [ ! -s "$pdf" ]; then
    echo "FAIL  report: chromium produced no PDF for $html" >&2
    return 1
  fi
  local pages
  pages=$(pdfinfo "$pdf" 2>/dev/null | awk '/^Pages:/{print $2}')
  if [ -z "$pages" ] || [ "$pages" -lt 1 ]; then
    echo "FAIL  report: $pdf has no pages (print clipped)" >&2
    return 1
  fi
  echo "ok    report: $base.pdf ($pages pages)" >&2
  printf '%s' "$pdf"
}

gc_pdf=$(render calgary_needs_builder.html Needs_Builder) || gc_pdf=""
sub_pdf=$(render calgary_subtrade_leads.html Subtrade_Leads) || sub_pdf=""

if [ -z "$gc_pdf" ] && [ -z "$sub_pdf" ]; then
  echo "FAIL  report: no fresh HTML sheets to send" >&2
  exit 6
fi

# CSVs go alongside the PDFs: same freshness gate, same two splits, so the
# recipient can sort, filter or import what the PDF only summarises. The
# internal master CSV is deliberately NOT attached (it carries exclusion
# reasons and internal grading the client sheet is meant to hide).
csv_fresh() {
  local csv="$1"
  [ -f "$csv" ] || return 1
  local mtime
  mtime=$(stat -c %Y "$csv" 2>/dev/null || echo 0)
  [ "$mtime" -ge "$SINCE" ] || { echo "SKIP  report: $csv is stale" >&2; return 1; }
  echo "ok    report: $(basename "$csv") ($(wc -l < "$csv" | tr -d ' ') lines)" >&2
  printf '%s' "$csv"
}
gc_csv=$(csv_fresh calgary_needs_builder.csv) || gc_csv=""
sub_csv=$(csv_fresh calgary_subtrade_leads.csv) || sub_csv=""

attachments=""
count_pdf=0
count_csv=0
for p in "$gc_pdf" "$sub_pdf"; do
  [ -n "$p" ] || continue
  attachments="${attachments:+$attachments,}$p"
  count_pdf=$((count_pdf + 1))
done
for p in "$gc_csv" "$sub_csv"; do
  [ -n "$p" ] || continue
  attachments="${attachments:+$attachments,}$(cd "$(dirname "$p")" && pwd)/$(basename "$p")"
  count_csv=$((count_csv + 1))
done

body="$tmpdir/body.txt"
{
  echo "This week's Calgary permit lead sheets are attached as PDFs, with the same two splits as CSV so you can sort, filter or import them."
  echo
  if [ -n "$SUMMARY" ]; then
    echo "Run stats: $SUMMARY"
    echo
  fi
  echo "Attached: $count_pdf PDF(s) and $count_csv CSV(s). Tappable phone and email links work in the PDF, and it prints cleanly on A4 landscape. The CSV carries the same columns plus the Email Status column."
  echo
  echo "Generated $(date '+%Y-%m-%d %H:%M %Z') by the weekly pipeline."
} > "$body"

subject="Calgary Permit Leads - week of $(date +%Y-%m-%d)"
out=$(gog gmail send --to "$PDF_TO" --subject "$subject" \
      --body-file "$body" --attach "$attachments" --no-input -p 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  echo "FAIL  report: gog send failed: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')" >&2
  exit 6
fi

msg_id=$(printf '%s' "$out" | awk -F'\t' '$1=="message_id"{print $2}')
echo "ok    report: emailed $count_pdf PDF(s) and $count_csv CSV(s) to $PDF_TO (message_id ${msg_id:-unknown})"
exit 0
