# Calgary Permit Leads Pipeline

A lead-generation pipeline over **public municipal open data**. It pulls recent
building and development permits from the City of Calgary's open-data API, keeps
the ones worth approaching a business about, enriches each company with public
contact details, caches the results in SQLite, and writes client-ready CSV and
HTML lists. Optionally it syncs those lists to Google Sheets and Drive.

The repository ships **no real data**. Everything under `fixtures/` and
`demo_output/` is synthetic, and `build_demo_data.py` regenerates it. The demo
runs fully offline and needs no API keys.

## Quick demo (no keys, no network)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build_demo_data.py
```

Expected output:

```
synthetic leads: 38  ->  needs_builder=18 subtrade=16 review=2 policy_excluded=2
...
master=38
needs_builder=18 (with phone/email: 18)
subtrade=16 (with phone/email: 16)
review=2

no problems found
```

This writes `demo_output/` (master CSV, two sellable CSVs, review CSV, two HTML
sheets) and refreshes `fixtures/sample_needs_builder.csv` and
`fixtures/sample_subtrade_leads.csv`. It then runs the QA gate
(`tools/audit_outputs.py`) over the output. The demo drives the same split,
review, export and audit functions the live pipeline uses, so it exercises the
output stage end to end. It does not run live ingest, web enrichment, the SQLite
cache, `build_leads` reclassification or `dedupe_leads`; those need the network
or the live pipeline path.

## Results

- Demo run (synthetic data, fully offline, no API keys): 38 leads total (master=38).
- Segments: needs_builder=18, subtrade=16, review=2, policy_excluded=2.
- Contact coverage: needs_builder 18 with phone/email 18; subtrade 16 with phone/email 16.
- QA gate: reported "no problems found".
- The live pipeline's SQLite cache holds around 300 companies.
- Tests: around 2,900 lines of tests pin recall behaviour with network access blocked, so changes to recall show up as test failures.
- Not covered by the demo: live Socrata ingest and web enrichment, which need network access.

## Architecture

```
 Socrata open data ──► fetch ──► classify ──► enrich ──► cache (SQLite) ──► split ──► export
  building permits      lead type   licence       Serper / keyless     TTL by      CSV, HTML,
  development permits   + pitch     lookup,       search, site scrape  record age  review pile
  business licences                 contacts      + MX check                        Google sync
```

| Stage | Code | What happens |
|---|---|---|
| Ingest | `cpp_fetch.py` `fetch_building_permits`, `fetch_dev_permits` | Public Socrata endpoints, no key. Value band `$50k–$3M` by default. |
| Classify | `build_leads` (`pipeline.py`), `pitch_for` (`cpp_export.py`) | Each permit gets a lead type: `NEEDS_GC`, `SUBTRADE_PITCH`, `EARLY_DESIGN` or `SUBCONTRACTOR` (a trade, excluded). Architect and design firms get a bid-list pitch instead of a GC pitch. One row per company. |
| Enrich | `enrich_company`, `scrape_contacts` (`pipeline.py`); `licence_lookup`, `search_web` (`cpp_fetch.py`) | Licence match, then Serper Places (keyed) or a keyless fallback, then page scraping. Candidate URLs are validated against the company name, so lookalike domains are rejected. |
| Verify | `domain_has_mx`, `valid_email` (`cpp_contacts.py`), `_reachable` (`cpp_export.py`) | Email is graded by DNS MX. A company is sellable only with a usable phone or a deliverable email. |
| Cache | `init_db` (`pipeline.py`), `cache_get`, `cache_put` (`cpp_cache.py`) | `cache.db`. Records with a contact live about 90 days. Blank ones retry after 7 days. A `kill_reason` column records why a company stayed blank. |
| Split | `filter_client_splits`, `select_review` (`cpp_export.py`) | Sellable GC list, sellable subtrade list, review pile. Trades and volume production builders are excluded by policy. |
| Export | `export_csv`, `export_clean_client_csv`, `export_client_html`, `export_review_csv` (`cpp_export.py`) | CSV and HTML outputs (see below). |
| Sync (optional) | `gog_sync`, `gog_sync_file` | Uploads to Google Sheets and Drive through the `gog` CLI. |
| Enrich, LLM (optional) | `tools/gemini_contacts.py`, `tools/gemini_task_sheet.py` | Backfill named contacts for gap rows through an external chat-model bridge or a Gemini Task sheet. Accepts an email only when the literal address appears on a page the tool fetched itself. Budgeted per day. |
| QA | `tools/audit_outputs.py` | Recomputes the invariants on the output CSVs: no duplicates, no junk URLs, no excluded trades, every sellable row reachable. |

Module layout:

```
pipeline.py                 orchestration: ingest-to-export flow, Sheets sync, entry point main();
                            also init_db, enrich_company, scrape_contacts, build_leads
cpp_config.py               constants, paths, EXIT_* codes, the Lead model, regexes, logging setup
                            (moved here from pipeline.py); VOLUME_TRACT_BUILDERS_RE lives here
cpp_cache.py                pure SQLite cache helpers: cache_key, cache_get, cache_put, cache age
cpp_match.py                name and URL matching, contractor/subtrade heuristics, junk-URL filter
cpp_fetch.py                network layer: Socrata permits, licence lookup, keyless search,
                            Serper, fetch_page (fetch_building_permits / fetch_dev_permits live here)
cpp_contacts.py             phone and email normalisation, MX check, contact extraction from sites
cpp_export.py               split, review pile, CSV/HTML export, dedupe, pitch_for
config.py                   every external-tool path and secret location, from env vars
build_demo_data.py          synthetic demo: builds sample leads, runs real export + audit
run.sh                      unattended runner: PATH, lock, per-run log, exit codes
run-weekly.sh               scheduler entry point: 7-day window and yield floor
send-weekly-pdfs.sh         render the HTML sheets to PDF and email them (optional)
tools/audit_outputs.py      post-run CSV QA gate
tools/gemini_contacts.py    optional named-contact backfill (bridge or Gemini Task)
tools/gemini_task_sheet.py  helpers for the Gemini Task path
fixtures/                   committed synthetic sample CSVs
tests/                      pytest suite (fixtures are synthetic; network is blocked)
```

## Outputs

| File | Contents |
|---|---|
| `calgary_leads_demo.csv` | Master: every lead, including unsellable ones (internal use). |
| `calgary_needs_builder.csv` | Sellable GC and early-design leads. |
| `calgary_subtrade_leads.csv` | Sellable subtrade and supplier leads. |
| `calgary_needs_review.csv` | Not sellable as-is. `Why Flagged` gives the cause. |
| `calgary_needs_builder.html`, `calgary_subtrade_leads.html` | Client sheets: grouped by value tier, with tappable `tel:` and `mailto:` links. Print to PDF from a browser. |

All outputs are written to the repository root by `pipeline.py` and to
`demo_output/` by the demo. The `.gitignore` excludes live outputs and
`cache.db`, because they contain contact data.

## Configuration

Core use needs only the optional Serper key. Every other external tool is
located through an environment variable. A variable left unset disables only
the code path that needs it, and that path fails with a message naming the
variable.

| Variable | Used by | Purpose |
|---|---|---|
| `SERPER_API_KEY` | `pipeline.py` | Serper Places and organic search. Read from `~/.config/serper.env` if present. Without it, use `--allow-keyless` (much lower yield). |
| `CALGARY_LEADS_SHEETS_STATE` | `pipeline.py` | JSON file holding the IDs of the Sheets and Drive files the pipeline updates in place. Default `~/.config/calgary-permit-leads/sheets.json`. If it is lost, the next run creates a second set of sheets. |
| `CALGARY_LEADS_GWS_BIN` | `tools/gemini_task_sheet.py` | Google Workspace CLI, used to create, append and read the task sheet. |
| `CALGARY_LEADS_TOKEN_SCRIPT` | `tools/gemini_task_sheet.py` | Prints a short-lived Google access token on stdout. |
| `CALGARY_LEADS_JOB_SCRIPT` | `tools/gemini_task_sheet.py` | Submit, await and abandon a Gemini Task job. |
| `CALGARY_LEADS_TRIGGER_SCRIPT` | `tools/gemini_task_sheet.py` | Fires a submitted job. |
| `CALGARY_LEADS_QUOTA_SCRIPT` | `tools/gemini_task_sheet.py` | Daily budget: `check` exits non-zero to refuse, `record` counts a submission. |
| `CALGARY_LEADS_BRIDGE_SCRIPT` | `tools/gemini_contacts.py` | Chat-model phone bridge, called as `<bridge> ask --model <m> <prompt>`. |
| `WEEKLY_PDF_TO` | `send-weekly-pdfs.sh` | Recipient of the weekly PDF email. Empty means the script refuses to send. |

Setup for the optional Google leg:

- `gog` (Google Workspace CLI) must be on `PATH`, authorized for Drive and Sheets.
  Its keyring password is read from `~/.config/gogcli/exec-env.sh`.
- `send-weekly-pdfs.sh` needs `chromium` and `pdfinfo` on `PATH`.

Run `./run.sh --skip-gog` first. It exercises everything except Google, but it
still needs live Socrata, Serper and web access.

## Running

There are two paths, and they are not interchangeable:

- **Live: `pipeline.py`.** A network CLI. It has no `--offline` or `--demo`
  flag. `--skip-gog` only disables the Google Workspace upload. Socrata
  ingest, Serper and web enrichment, and DNS MX checks still run.
- **Offline (1): `build_demo_data.py`.** It feeds 38 synthetic leads through the
  post-enrichment output stage: `pitch_for`, `filter_client_splits`,
  `select_review`, CSV and HTML export, and `tools/audit_outputs.py`. It does
  **not** exercise live ingest, web enrichment, SQLite caching,
  `build_leads` reclassification, or `dedupe_leads`.
- **Offline (2): the test suite.** `pytest` and `tests/run_all.sh` block all
  sockets through `tests/_env.py`.

```bash
.venv/bin/python pipeline.py --skip-gog       # one run, no Google sync
.venv/bin/python pipeline.py --days 7         # 7-day window, with Google sync
./run.sh --skip-gog                           # unattended wrapper: lock, log, exit codes (needs live network)
./run-weekly.sh                               # 7-day window plus yield floor, for a scheduler
```

`run.sh` is the supported entry point for unattended use. It sets `PATH`,
loads the gog keyring env, takes a `flock`, writes a per-run log under
`logs/`, and maps failures to distinct exit codes:

| code | meaning |
|---|---|
| 0 | ok, sellable leads produced (and uploaded, unless `--skip-gog`) |
| 1 | no permits returned |
| 2 | config: `gog`, keyring or `SERPER_API_KEY` missing |
| 3 | low yield: fewer sellable leads than `--min-client-leads` |
| 4 | Google Workspace sync failed |
| 5 | another run is already in progress |
| 6 | PDF report email failed (leads were still produced) |

Useful flags: `--days N` (lookback window), `--refresh` (ignore cache),
`--min-cost` / `--max-cost` (value band), `--building-limit` / `--dev-limit`
(row caps), `--min-client-leads N` (yield floor), `--include-tract-builders`.
`--help` lists them all.

Scheduling is up to you. Run `run-weekly.sh` from cron or a systemd timer.
Have the wrapper record each run's exit code, because a run that never happens
looks the same as a quiet week.

## Tests

```bash
pip install -r requirements-dev.txt   # adds pytest
.venv/bin/python -m pytest -q          # the full suite
tests/run_all.sh                       # the same modules, run one by one
.venv/bin/python tools/audit_outputs.py --dir demo_output
```

The suite needs no network and no keys:

- `tests/conftest.py` and `tests/_env.py` set fake tool paths and block every
  socket call, so an unstubbed network request fails loudly.
- Test data is invented. Company names, people, phones and emails in the tests
  are placeholders.
- Tests that exercise the Gemini paths stub `subprocess.run`, so no tool is
  executed.

Test modules, grouped by concern:

- `test_matching.py`, `test_recall.py`: name and URL matching, false-positive cases
- `test_regression.py`: one block per defect that once shipped
- `test_cache.py`: cache TTL and migration
- `test_delivery.py`: what counts as sellable, CSV shape, HTML output
- `test_review.py`: review pile and policy exclusions
- `test_fetch_fallback.py`: user-agent fallback on fetch failures
- `test_gemini_contacts.py`, `test_gemini_task_permits.py`, `test_via_task_stage.py`: optional contact enrichment

No test is skipped.

## Notes and caveats

- **Serper credits.** Only new companies cost a credit. A cold cache costs one
  or two credits per company.
- **Socrata needs no key.** The three endpoints are public.
- **Phone area codes.** Only Calgary and southern Alberta codes are kept. A
  northern Alberta number on a Calgary permit usually means a wrong match.
- **Rejected websites take their contacts with them.** A phone or email found on
  a site that fails name validation is discarded too, so it cannot leak into a sellable list.
- **Public homebuilder names.** `VOLUME_TRACT_BUILDERS_RE` in `cpp_config.py` lists
  large public homebuilders, which the sellable subtrade list excludes by policy.
  Edit it for your market.
- **Cache file.** `cache.db` is local state and contains contact data. Don't publish it.

## Publishing checklist

Before pushing this repository anywhere public, confirm that `cache.db`,
`logs/`, `demo_output/`, live output CSVs and HTML, `outreach/`, `reports/`,
`.venv/` and any `*.env` or keyring file are absent. `.gitignore` already
excludes them. Use `grep -rIn` with your own email, home path and sheet IDs to
confirm no personal value is present.
