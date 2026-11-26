#!/usr/bin/env python3
"""Recorded-HTTP offline demo: ingest, enrichment, SQLite caching and dedup.

Two modes:

  tools/demo_recorded.py --record   run the real stages against the live,
                                    keyless public HTTP surfaces and save every
                                    response into fixtures/http/cassettes.json
  tools/demo_recorded.py --replay   run the same stages fully offline, serving
                                    every HTTP request from the committed
                                    cassettes and failing loudly on a request
                                    with no cassette

Recorded surfaces (all public): Socrata open data (building and development
permits, tract-builder aggregates, business licences), company contact pages,
and DNS-over-HTTPS MX lookups. Record mode additionally uses the configured
Serper key (~/.config/serper.env, Serper Places + Organic): keyless Bing and
DuckDuckGo are soft-blocked from this host and return junk, so the keyless
fallback path would record a demo where every company is killed. The cassettes
store status + URL + response body only (never request headers), and record
scans the bodies afterwards to prove the key value appears nowhere in the
committed file. Replay and CI never need the real Serper key and run fully
offline; a constant placeholder value passes the client-side gate only.

Replay hard-verifies, and exits non-zero if any check fails: every HTTP request
served from a cassette, zero real connections, a cache-only second pass, and
byte-identical evidence versus the committed run.

Known fidelity limits, accepted by design (a record/replay pair must be
self-consistent; both sides behave identically):
- fetch_page's User-Agent fallback chain is not part of the cassette key, so a
  URL retried across UAs replays the last recorded attempt's body for every
  attempt. Terminal outcomes are unchanged because the stored body is the last
  attempt's.
- Transport errors recorded as HTTP 599 abort fetch_page on the first UA
  instead of walking the chain; production's _FETCH_RETRYABLE path and a 599
  abort end with the same no-body outcome.
- Redirects are followed inside the record transport, so cpp_fetch._fetch_once
  sees the pre-redirect URL in r.url while receiving the final body. Record and
  replay see the same URL/body pair.

  * every HTTP request was served from a cassette (a miss raises), and a socket
    guard turns any real connection attempt into an error, so "offline" is
    enforced rather than assumed;
  * the second enrichment pass performs zero HTTP requests -- everything came
    from the SQLite cache, which is what proves caching;
  * dedupe_leads leaves one row per company, and companies that filed several
    permits carry the "[+N more permit(s)]" roll-up;
  * the recomputed evidence matches the committed recorded_demo/run-output.json
    byte for byte (dict equality), so the committed numbers cannot rot.

Time freezing: Socrata URLs embed a cutoff date (days_ago_iso) and licence
expiry checks compare against now(), so replay pins both to the values captured
at record time (stored in the cassette manifest). Without that the URLs would
not match the cassettes and recorded licences would slowly expire.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from cpp_config import HEADERS, days_ago_iso  # noqa: E402
import cpp_cache as CC  # noqa: E402
import cpp_contacts as C  # noqa: E402
import pipeline as P  # noqa: E402

cache_key = CC.cache_key  # hot alias used throughout

CASSETTES = REPO / "fixtures" / "http" / "cassettes.json"
OUTDIR = REPO / "recorded_demo"


# ---------------------------------------------------------------------------
# Cassette store
# ---------------------------------------------------------------------------
def _request_key(request: httpx.Request) -> str:
    """method + final URL (+ body hash for POSTs) as httpx itself renders it."""
    body = request.read()
    tag = hashlib.sha256(body).hexdigest()[:16] if body else ""
    return f"{request.method} {request.url}" + (f" #{tag}" if tag else "")


class CassetteStore:
    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.meta: dict = {}

    def add(self, key: str, status: int, content_type: str, body: bytes) -> None:
        self.entries[key] = {
            "status": status,
            "content_type": content_type or "application/octet-stream",
            "body_gz": base64.b64encode(gzip.compress(body)).decode("ascii"),
        }

    @classmethod
    def load(cls, path: Path) -> "CassetteStore":
        store = cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        store.meta = {k: v for k, v in payload.items() if k != "requests"}
        store.entries = payload["requests"]
        return store

    def save(self, path: Path, frozen: dict, frozen_now: str, record_args: dict) -> None:
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frozen_days_ago_iso": frozen,
            "frozen_now": frozen_now,
            "record_args": record_args,
            "requests": {k: self.entries[k] for k in sorted(self.entries)},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------
_RealClient = httpx.Client  # captured before the global patch below


class OfflineViolation(BaseException):
    """Raised when the demo tries the network while it must be offline.

    BaseException, deliberately: pipeline code has broad `except Exception`
    fallbacks (cpp_contacts.domain_has_mx, cpp_fetch.ddg_search,
    pipeline.scrape_contacts) that would swallow a plain RuntimeError and
    silently degrade the evidence. An OfflineViolation propagates through
    them and kills the run.
    """


BODY_CAP = 120_000  # per recorded response, spliced head+tail (see below)


def cap_body(body: bytes, content_type: str = "") -> bytes:
    """Cap a recorded response body at BODY_CAP bytes.

    Real company pages bury contact blocks under megabytes of inline base64
    images; storing them raw would make the cassettes enormous. When a body
    exceeds the cap, splice head and tail around a marker and use the spliced
    bytes everywhere: the record run parses exactly these bytes, so replay sees
    precisely what the record run parsed. Contact details normally live in the
    page header or footer, which the splice keeps.

    HTML only: splicing a JSON document would corrupt json(), and no Socrata
    JSON response in practice exceeds the cap. Sliced on decoded character
    boundaries so a multi-byte UTF-8 sequence is never cut.
    """
    if len(body) <= BODY_CAP:
        return body
    if "html" not in content_type.lower():
        return body
    text = body.decode("utf-8", errors="replace")
    half = BODY_CAP // 2
    return (text[:half]
            + "\n<!-- recorded-demo: body cap splice, middle trimmed -->\n"
            + text[-half:]).encode("utf-8")


class RecordTransport(httpx.BaseTransport):
    """Passes requests through to the real network and records every response.

    A transport-level failure (connect error, timeout) is recorded as a 599
    response, so replay reproduces the same failure the live run saw.
    """

    def __init__(self, store: CassetteStore) -> None:
        self.store = store
        self.served = 0
        self.misses: list[str] = []  # record transport never misses; kept for symmetry

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            real = httpx.Request(method=request.method, url=request.url,
                                 headers=request.headers, content=request.read())
            with _RealClient(timeout=40, follow_redirects=True) as client:
                r = client.send(real, follow_redirects=True)
            status, ctype, body = r.status_code, r.headers.get("content-type", ""), \
                cap_body(r.content, r.headers.get("content-type", ""))
        except httpx.HTTPError as exc:
            status, ctype, body = 599, "text/plain", f"transport error: {exc}".encode()
        self.served += 1
        self.store.add(_request_key(request), status, ctype, body)
        return httpx.Response(status, headers={"content-type": ctype}, content=body)


class ReplayTransport(httpx.BaseTransport):
    """Serves every request from the cassettes; a miss is a hard error."""

    def __init__(self, store: CassetteStore) -> None:
        self.store = store
        self.served = 0
        self.misses: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = _request_key(request)
        entry = self.store.entries.get(key)
        if entry is None:
            self.misses.append(key)
            # OfflineViolation, not RuntimeError: pipeline fallbacks catch
            # Exception broadly (cpp_fetch.socrata even retries RuntimeError),
            # and a swallowed miss would silently degrade the evidence.
            raise OfflineViolation(
                f"cassette miss ({len(self.misses)} so far): {key}\n"
                f"cassette file holds {len(self.store.entries)} entries")
        self.served += 1
        body = gzip.decompress(base64.b64decode(entry["body_gz"]))
        return httpx.Response(entry["status"],
                              headers={"content-type": entry["content_type"]},
                              content=body)


def install_transport(store: CassetteStore, mode: str) -> "RecordTransport | ReplayTransport":
    transport = RecordTransport(store) if mode == "record" else ReplayTransport(store)

    class _PatchedClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    # Patch both bindings: cpp_contacts.domain_has_mx uses httpx.get, whose
    # top-level helper resolves httpx._api.Client (its own import-time binding),
    # not httpx.Client. Missing the second binding would silently let the DoH
    # MX lookups bypass the transport -- live queries during record (never
    # cassetted) and socket-guard crashes during replay.
    httpx.Client = _PatchedClient
    if getattr(httpx, "_api", None) is not None:
        httpx._api.Client = _PatchedClient  # type: ignore[attr-defined]
    return transport


# ---------------------------------------------------------------------------
# Time freezing
# ---------------------------------------------------------------------------
def freeze_time(cassette: CassetteStore):
    """Pin days_ago_iso (Socrata cutoffs) and the datetime objects (licence
    expiry, cache stamps) to the values captured at record time. Returns a
    restore() callable that puts the original module attributes back."""
    frozen = cassette.meta["frozen_days_ago_iso"]

    def _frozen_days_ago(days: int) -> str:
        key = str(int(days))
        if key not in frozen:
            raise RuntimeError(f"no recorded cutoff for days={days}; cassettes froze {sorted(frozen)}")
        return frozen[key]

    frozen_now = datetime.fromisoformat(cassette.meta["frozen_now"])

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            dt = frozen_now if frozen_now.tzinfo else frozen_now.replace(tzinfo=timezone.utc)
            return dt.astimezone(tz) if tz else dt.replace(tzinfo=None)

    saved: dict = {}
    for name in ("cpp_config", "cpp_cache", "cpp_match", "cpp_fetch", "cpp_contacts",
                 "cpp_export", "pipeline"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if hasattr(mod, "days_ago_iso"):
            saved[(name, "days_ago_iso")] = mod.days_ago_iso
            setattr(mod, "days_ago_iso", _frozen_days_ago)
        if name in ("cpp_fetch", "cpp_cache", "pipeline") and hasattr(mod, "datetime"):
            # cpp_cache stamps updated_at / ages cache rows with datetime; pipeline
            # logs and stamps event records with it. Freeze all three so cache
            # freshness (cpp_cache._cache_age_days via pipeline._cache_is_fresh) is
            # identical no matter when the replay runs.
            saved[(name, "datetime")] = mod.datetime
            setattr(mod, "datetime", _FrozenDatetime)

    def restore() -> None:
        for (name, attr), value in saved.items():
            setattr(sys.modules[name], attr, value)

    return restore


def no_network_guard():
    """Context manager making any real connection attempt a loud error.

    Every blocked call is recorded in self.blocked, and the replay asserts the
    list is empty -- a violation swallowed somewhere upstream still fails the
    run at the end.
    """
    def _blocked(*_a, **_k):
        raise OfflineViolation("the recorded demo is offline; the pipeline tried the network")

    class _Guard:
        def __init__(self):
            self.blocked: list[str] = []

        def _tracked(self, label):
            def _b(*_a, **_k):
                self.blocked.append(label)
                raise OfflineViolation(f"the recorded demo is offline; the pipeline called {label}")
            return _b

        def __enter__(self):
            self._saves = {}
            for name in ("connect", "connect_ex", "sendto"):
                self._saves[name] = getattr(socket.socket, name)
                setattr(socket.socket, name, self._tracked(f"socket.{name}"))

            self._saves["getaddrinfo"] = socket.getaddrinfo
            socket.getaddrinfo = self._tracked("socket.getaddrinfo")

            self._saves["create_connection"] = socket.create_connection
            socket.create_connection = self._tracked("socket.create_connection")
            return self

        def __exit__(self, *_a):
            setattr(socket.socket, "connect", self._saves["connect"])
            setattr(socket.socket, "connect_ex", self._saves["connect_ex"])
            setattr(socket.socket, "sendto", self._saves["sendto"])
            socket.getaddrinfo = self._saves["getaddrinfo"]
            socket.create_connection = self._saves["create_connection"]
            return False

    return _Guard()


# ---------------------------------------------------------------------------
# The demo run (identical in both modes; only the transport differs)
# ---------------------------------------------------------------------------
def choose_targets(permits: list[dict], cap: int) -> list[str]:
    """Deterministic pick: sellable leads first (highest value), then the rest.

    The company with the most permits in the window is always kept (its slot is
    the last), so the dedupe stage always has a genuine multi-permit roll-up to
    prove, whatever the permit mix looks like.
    """
    if not permits:
        raise ValueError("choose_targets: the recorded window produced no permits")
    counts: dict[str, int] = {}
    for p in permits:
        counts[cache_key(p["target_company"])] = counts.get(cache_key(p["target_company"]), 0) + 1
    multi_key, multi_count = max(counts.items(), key=lambda kv: kv[1])
    seen: dict[str, dict] = {}
    for p in permits:
        key = cache_key(p["target_company"])
        if key not in seen:
            seen[key] = p
    sellable = [p for p in seen.values() if p["lead_type"] in {"NEEDS_GC", "EARLY_DESIGN"}]
    others = [p for p in seen.values()
              if cache_key(p["target_company"])
              not in {cache_key(s["target_company"]) for s in sellable}]
    sellable.sort(key=lambda p: -(p["permit_value"] or 0))
    ordered = sellable + others
    chosen = [p["target_company"] for p in ordered[:cap]]
    keys = [cache_key(c) for c in chosen]
    if multi_count > 1 and multi_key not in keys:
        if chosen:
            chosen[-1] = next(p["target_company"] for p in permits
                              if cache_key(p["target_company"]) == multi_key)
        else:
            chosen = [next(p["target_company"] for p in permits
                           if cache_key(p["target_company"]) == multi_key)]
    return chosen


def run_demo(store: CassetteStore, transport, args) -> tuple[dict, dict]:
    """Run the three stages. Returns (evidence, runtime); evidence is committed
    and must be identical between record and replay, runtime only feeds the
    replay-side checks."""
    with tempfile.TemporaryDirectory() as td:
        P.DB_PATH = Path(td) / "cache.db"
        # Keep pipeline log records out of the repo: importing cpp_config leaves
        # LOG_DIR pointing at REPO/logs, and the run emits log records that a
        # lazy handler would materialise there (CI included).
        C.LOG_DIR = Path(td) / "logs"
        # MX verdicts are memoised on the module; a stale entry from an earlier
        # run in this process would skip a recorded DoH request.
        C._mx_cache.clear()
        conn = P.init_db()
        try:
            n_tract = P.load_dynamic_tract_builders(conn, days=365, min_permits=15)
            building = P.fetch_building_permits(days=args.days, limit=args.building_limit)
            dev = P.fetch_dev_permits(days=args.days, limit=args.dev_limit)
            permits = building + dev
            targets = choose_targets(permits, args.enrich)
            wanted = {cache_key(t) for t in targets}
            subset = [p for p in permits if cache_key(p["target_company"]) in wanted]

            leads_pass1 = P.build_leads(conn, subset)
            served_pass1 = transport.served
            leads_pass2 = P.build_leads(conn, subset)
            served_pass2 = transport.served

            cache_rows = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            kill_reasons = dict(conn.execute(
                "SELECT kill_reason, COUNT(*) FROM companies GROUP BY kill_reason"))
        finally:
            conn.close()

    deduped = P.dedupe_leads(leads_pass2)
    groups: dict[str, list] = {}
    for x in deduped:
        groups.setdefault(cache_key(x.company_name), []).append(x)
    rollups, extra_permits_total = [], 0
    for key, group in groups.items():
        lead = group[0]
        extras = lead.extra_permits or []
        if extras:
            extra_permits_total += len(extras)
            combined = (lead.permit_value or 0) + sum((e.get("permit_value") or 0) for e in extras)
            rollups.append({
                "company": lead.company_name,
                "permits": 1 + len(extras),
                "combined_value": round(combined, 2),
            })
    # The roll-up must be visible in the committed row itself: dedupe_leads
    # appends a "[+N more permit(s) this window, $X combined]" suffix and carries
    # the folded permits in lead.extra_permits. Counted on the DEDUPED leads so a
    # broken dedupe_leads cannot satisfy the check.
    suffix_ok = all(
        (not (l.extra_permits or []))
        or (f"[+{len(l.extra_permits)} more permit(s) this window" in (l.description or ""))
        for l in deduped)
    gc, sub = P.filter_client_splits(deduped)
    review, excluded = P.select_review(deduped, gc, sub)
    contactable = sum(1 for x in deduped if x.phone or x.email or x.website)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    P.export_csv(deduped, OUTDIR / "recorded_master.csv")
    P.export_clean_client_csv(gc, OUTDIR / "recorded_needs_builder.csv")
    P.export_clean_client_csv(sub, OUTDIR / "recorded_subtrade_leads.csv")
    P.export_review_csv(review, OUTDIR / "recorded_review.csv")

    evidence = {
        "cassette_file": str(CASSETTES.relative_to(REPO)),
        "cassettes": len(store.entries),
        "ingest": {
            "building_permits": len(building),
            "dev_permits": len(dev),
            "total_permits": len(permits),
            "companies_enriched": len(targets),
            "dynamic_tract_builders": n_tract,
        },
        "caching": {
            "cache_rows": cache_rows,
            "kill_reasons": kill_reasons,
        },
        "dedupe": {
            "leads_before": len(leads_pass2),
            "leads_after": len(deduped),
            "rollup_companies": len(rollups),
            "rollup_extra_permits": extra_permits_total,
            "rollup_suffix_visible": suffix_ok,
            "rollup_example": rollups[:3],
        },
        "coverage": P.coverage_counts(deduped),
        "splits": {
            "needs_builder": len(gc),
            "subtrade": len(sub),
            "review": len(review),
            "policy_excluded": excluded,
            "contactable": contactable,
        },
    }
    runtime = {
        "served_pass1": served_pass1,
        "served_pass2_delta": served_pass2 - served_pass1,
        "served_total": served_pass2,
        "pass2_identical": leads_pass1 == leads_pass2,
        "misses": getattr(transport, "misses", []),
    }
    return evidence, runtime


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="record live HTTP into cassettes")
    mode.add_argument("--replay", action="store_true", help="replay cassettes, fully offline")
    parser.add_argument("--days", type=int, default=14, help="permit window (default 14)")
    parser.add_argument("--building-limit", type=int, default=40)
    parser.add_argument("--dev-limit", type=int, default=20)
    parser.add_argument("--enrich", type=int, default=10, help="companies to enrich (default 10)")
    parser.add_argument("--refresh", action="store_true",
                        help="replay only: rewrite committed run-output.json even if it "
                             "differs (evidence-schema migrations; never for CI)")
    args = parser.parse_args(argv)
    args.mode = "record" if args.record else "replay"

    serper_key = ""
    if args.mode == "record":
        # Record uses the real enrichment path; the key never enters the
        # cassettes (bodies only, scanned below) and replay/CI stay keyless.
        P.load_env_file(Path.home() / ".config" / "serper.env")
        serper_key = os.environ.get("SERPER_API_KEY", "").strip()
        if not serper_key:
            print("record: SERPER_API_KEY not set; recording keyless fallback only",
                  file=sys.stderr)
    else:
        # A placeholder, never transmitted: the transport serves the recorded
        # Serper responses before any request could leave, but the client-side
        # gate in cpp_fetch skips Serper entirely when the var is empty, which
        # would change the code path the cassettes recorded. CI passes this
        # constant; the real key is never needed offline.
        os.environ["SERPER_API_KEY"] = "recorded-cassette-replay"

    if args.mode == "record":
        store = CassetteStore()
        transport = install_transport(store, "record")
        real_days_ago = days_ago_iso  # the real one, captured before any freezing
        frozen = {str(args.days): real_days_ago(args.days), "365": real_days_ago(365)}
        frozen_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Freeze BEFORE the run with exactly the values the cassettes will store:
        # the Socrata cutoff URLs and the cache stamps are then identical no
        # matter where midnight falls during a long record.
        store.meta = {"frozen_days_ago_iso": frozen, "frozen_now": frozen_now}
        restore = freeze_time(store)
        try:
            evidence, runtime = run_demo(store, transport, args)
        finally:
            restore()
            httpx.Client = _RealClient
            if getattr(httpx, "_api", None) is not None:
                httpx._api.Client = _RealClient
        record_args = {"days": args.days, "building_limit": args.building_limit,
                       "dev_limit": args.dev_limit, "enrich": args.enrich}
        if serper_key:
            leaked = [k for k, e in store.entries.items()
                      if serper_key.encode() in base64.b64decode(e["body_gz"])]
            if leaked:
                print("ABORT: SERPER_API_KEY value found in cassette bodies", file=sys.stderr)
                return 1
        store.save(CASSETTES, frozen, frozen_now, record_args=record_args)
        (OUTDIR / "run-output.json").write_text(json.dumps(evidence, indent=1), encoding="utf-8")
        print(json.dumps(evidence, indent=1))
        print(f"record run: {runtime['served_total']} HTTP requests recorded "
              f"({len(store.entries)} distinct cassettes)")
        print(f"cassette manifest -> {CASSETTES.relative_to(REPO)}")
        return 0

    store = CassetteStore.load(CASSETTES)
    transport = install_transport(store, "replay")
    recorded = store.meta.get("record_args", {})
    for field in ("days", "building_limit", "dev_limit", "enrich"):
        if field in recorded:
            setattr(args, field, recorded[field])  # replay uses the recorded window
    freeze = freeze_time(store)
    guard = no_network_guard()
    try:
        with guard:
            evidence, runtime = run_demo(store, transport, args)
    except (RuntimeError, OfflineViolation) as exc:
        print(f"REPLAY FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        freeze()
        httpx.Client = _RealClient
        if getattr(httpx, "_api", None) is not None:
            httpx._api.Client = _RealClient

    master_rows = _csv_rows(OUTDIR / "recorded_master.csv")
    checks = [
        ("every HTTP request was served from a cassette", not transport.misses),
        ("no blocked network call anywhere in the replay", not guard.blocked),
        ("second enrichment pass made zero HTTP requests (all from SQLite cache)",
         runtime["served_pass2_delta"] == 0),
        ("cache read-back reproduced the first pass exactly", runtime["pass2_identical"]),
        ("dedupe left one row per company",
         evidence["dedupe"]["leads_after"]
         == len({cache_key(x["company_name"]) for x in master_rows})),
        ("master CSV has exactly the deduped leads", len(master_rows)
         == evidence["dedupe"]["leads_after"]),
        ("dedupe shrank the lead list", evidence["dedupe"]["leads_after"]
         < evidence["dedupe"]["leads_before"]),
        ("the recorded window contains at least one multi-permit company",
         evidence["dedupe"]["rollup_companies"] >= 1),
        ("folded permits are visible in the deduped rows",
         evidence["dedupe"]["rollup_extra_permits"] >= 1
         and evidence["dedupe"]["rollup_suffix_visible"]),
    ]
    committed = json.loads((OUTDIR / "run-output.json").read_text(encoding="utf-8"))
    checks.append(("recomputed evidence matches the committed run-output.json",
                   args.refresh or evidence == committed))
    failures = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if failures:
        if evidence != committed:
            for key in evidence:
                if evidence[key] != committed.get(key):
                    print(f"  {key}: replay={json.dumps(evidence[key])} "
                          f"committed={json.dumps(committed.get(key))}", file=sys.stderr)
        return 1
    (OUTDIR / "run-output.json").write_text(json.dumps(evidence, indent=1), encoding="utf-8")
    print(f"\nrecorded demo replayed offline: {runtime['served_total']} HTTP requests served "
          f"from {len(store.entries)} cassettes, 0 from the network")
    return 0


def _csv_rows(path: Path) -> list[dict]:
    import csv as _csv
    with path.open(newline="", encoding="utf-8") as fh:
        return list(_csv.DictReader(fh))


if __name__ == "__main__":
    raise SystemExit(main())
