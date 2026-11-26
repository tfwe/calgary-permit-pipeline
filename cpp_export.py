"""Export/formatting layer and lead post-processing for the Calgary pipeline.

Verbatim move out of pipeline.py (behaviour-preserving split). pipeline.py
star-imports this module, so every name below stays reachable as P.<name>.
"""
from __future__ import annotations

import csv
import html
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from rich.table import Table

from cpp_config import *
from cpp_cache import *
from cpp_match import *

__all__ = [
    "pitch_for",
    "export_csv",
    "export_clean_client_csv",
    "review_reason",
    "select_review",
    "OGL_FOOTER",
    "COMMERCIAL_HINTS",
    "_is_commercial_lead",
    "_tel_href",
    "_maps_href",
    "CLIENT_ROLE_INBOXES",
    "CLIENT_PITCH_CHIPS",
    "CLIENT_SHEET_CSS",
    "CLIENT_GROUP_HEADINGS",
    "_client_esc",
    "_client_is_role",
    "client_tier",
    "_client_money",
    "_client_scope",
    "_client_reach",
    "_client_chip",
    "_client_extra_lines",
    "_client_card",
    "_client_row",
    "export_client_html",
    "export_review_csv",
    "print_summary",
    "_csv_values",
    "dedupe_leads",
    "_reachable",
    "filter_client_splits",
]

def pitch_for(lead_type: str, permit_stage: str = "", company_name: str = "") -> str:
    if lead_type == "SUBTRADE_PITCH" and permit_stage == "DEVELOPMENT_PERMIT":
        return "Pre-construction subtrade opportunity — development permit filed, trades not yet awarded"
    # An architect/design firm with no contractor of record runs the tender itself;
    # offering it full-build GC services misses. Ask for the bid list instead.
    # Guard against design-build contractors, whose names carry both signals.
    if (lead_type in {"NEEDS_GC", "EARLY_DESIGN"} and company_name
            and DESIGN_FIRM_RE.search(company_name)
            and not CONTRACTOR_NAME_RE.search(company_name)
            # A design-build firm names both roles and buys GC services itself.
            and not re.search(r"\bDESIGN[\s\-]*BUILD\b", company_name, re.I)):
        return "Architect / designer of record (no GC listed) — request invite to tender / bid list"
    return {
        "NEEDS_GC": "Full-build GC services — applicant has no contractor of record",
        "SUBTRADE_PITCH": "Subtrade / supplier opportunity — active GC permit",
        "EARLY_DESIGN": "Pre-construction outreach — development permit at design stage",
        "SUBCONTRACTOR": "Trade already active — not a buyer of subtrade services",
    }.get(lead_type, "Permit-based B2B outreach")


# ---------------------------------------------------------------------------
# Output: CSVs and terminal summary
# ---------------------------------------------------------------------------
def export_csv(leads: list[Lead], path: Path) -> None:
    # Fields declared with exclude=True (extra_permits) are HTML-only detail;
    # including them would add an empty column and break the CSV's column count.
    _all = getattr(Lead, "model_fields", getattr(Lead, "__fields__", {}))
    fields = [name for name, info in _all.items()
              if not getattr(info, "exclude", False)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            row = lead.model_dump() if hasattr(lead, "model_dump") else lead.dict()
            writer.writerow(row)


def export_clean_client_csv(leads: list[Lead], path: Path) -> None:
    """11-column, zero-jargon CSV ready to send straight to Calgary contractors."""
    headers = [
        "Applied Date", "Permit #", "Address", "Community",
        "Est. Project Value", "Project Description",
        "Company / Applicant", "Contact Name", "Phone", "Email", "Email Status", "Website", "Lead Angle",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for x in leads:
            display_name = (x.unmasked_tradename
                            if NUMBERED_RE.match(x.company_name) and x.unmasked_tradename
                            else x.company_name)
            val_str = f"${x.permit_value:,.0f}" if x.permit_value > 0 else "TBD (Dev Permit)"
            writer.writerow([
                x.applied_date, x.permit_number, x.address, x.community, val_str,
                x.description, display_name, x.contact_name or "", x.phone, x.email, x.email_status,
                x.website, x.pitch_angle,
            ])


def review_reason(x: Lead) -> str:
    """Why a lead is not sellable as-is, for the manual review pile."""
    if x.email and x.email_status == "no_mx":
        return "email domain has no MX (undeliverable)"
    if not (x.phone or x.email or x.website):
        if x.kill_reason:
            return f"no contact channel found ({x.kill_reason})"
        return "no contact channel found"
    if not (x.phone or x.email):
        return "website only - no phone or email"
    return "unclassified - check manually"


def select_review(leads: list[Lead], gc_leads: list[Lead], sub_leads: list[Lead],
                  include_tract_builders: bool = False) -> tuple[list[Lead], int]:
    """Leads dropped from both sellable lists, minus deliberate policy exclusions.

    Two groups are excluded on purpose rather than pending judgement, so they must
    NOT land in a pile meant for human follow-up:
      * SUBCONTRACTOR - we would be pitching a trade its own trade
      * volume tract builders - locked-in annual vendor contracts
    Returns (review_leads, policy_excluded_count).
    """
    sold = {(x.permit_number, x.company_name) for x in gc_leads}
    sold |= {(x.permit_number, x.company_name) for x in sub_leads}
    review: list[Lead] = []
    excluded = 0
    for x in leads:
        if (x.permit_number, x.company_name) in sold:
            continue
        if x.lead_type == "SUBCONTRACTOR":
            excluded += 1
            continue
        if not include_tract_builders and is_volume_tract_builder(x.company_name, x.unmasked_tradename):
            excluded += 1
            continue
        review.append(x)
    return review, excluded


# Required attribution for Calgary's open data. Shown on every client sheet so a
# reader can trace the source and its terms.
OGL_FOOTER = (
    "Weekly Calgary Construction Permit &amp; Market Intelligence Digest. "
    "Contains information licensed under the "
    '<a href="https://data.calgary.ca/stories/s/Open-Calgary-Terms-of-Use/'
    'u45n-7awa/" target="_blank" rel="noopener">Open Government Licence '
    "&ndash; City of Calgary</a>. Public business contact details are provided "
    "for B2B project estimation and bid inquiries relevant to the "
    "recipient&rsquo;s business role."
)

COMMERCIAL_HINTS = ("commercial", "multi", "industrial", "institutional",
                    "non-residential", "office", "retail", "restaurant")


def _is_commercial_lead(x: Lead) -> bool:
    """Whether a lead belongs in the commercial / high-value section.

    Derived from the permit's own words (the Socrata `permittype` and the
    humanised scope) rather than a hand-maintained company list.
    """
    if x.permit_stage == "DEVELOPMENT_PERMIT":
        return True
    text = f"{x.permit_type} {x.description}".lower()
    return any(h in text for h in COMMERCIAL_HINTS) or (x.permit_value or 0) >= 400000


def _tel_href(phone: str) -> str:
    """tel: URI with digits only, so the number is tappable on a phone or PDF."""
    digits = re.sub(r"\D", "", phone or "")
    return f"tel:+1{digits}" if digits else ""


def _maps_href(address: str) -> str:
    query = quote_plus(f"{address}, Calgary, AB")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


# Client sheet: tiered cards. Role inboxes and tier
# rules live here so tests can exercise them directly.
CLIENT_ROLE_INBOXES = frozenset({
    "info", "contact", "admin", "office", "sales", "hello", "enquiries", "inquiries",
    "general", "reception", "mail", "service", "accounts", "support", "team",
})

# The five real pitch_for() strings. Any other angle gets no chip.
CLIENT_PITCH_CHIPS = {
    "Architect / designer of record (no GC listed) — request invite to tender / bid list": "GC not in permit record",
    "Full-build GC services — applicant has no contractor of record": "No contractor on record",
    "Pre-construction outreach — development permit at design stage": "Pre-construction",
    "Subtrade / supplier opportunity — active GC permit": "Active GC",
    "Pre-construction subtrade opportunity — development permit filed, trades not yet awarded": "Pre-construction, trades open",
}

CLIENT_SHEET_CSS = """
  :root { --ink:#14161a; --body:#333944; --muted:#687080; --faint:#98a0ad; --line:#e3e6eb; --accent:#1d4ed8; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:var(--ink); font-size:11px; line-height:1.4; }
  header { padding:13px 16px 0; }
  .kicker { font-size:8.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); }
  h1 { font-size:19px; margin:1px 0 8px; letter-spacing:-.015em; }
  .stats { display:flex; gap:26px; margin-bottom:11px; }
  .stats span { font-size:9.5px; color:var(--muted); }
  .stats b { display:block; color:var(--ink); font-size:12.5px; font-weight:700; }
  .start { border:1px solid var(--line); border-radius:5px; padding:8px 11px 9px; background:#fbfcfd; }
  .start h2 { font-size:9px; letter-spacing:.13em; text-transform:uppercase; color:var(--muted); margin:0 0 5px; font-weight:600; }
  .start ol { margin:0; padding:0; list-style:none; display:flex; flex-direction:column; gap:2px; }
  .start li { display:flex; gap:10px; align-items:baseline; font-size:10.5px; }
  .s-val { width:76px; font-weight:700; }
  .s-co { flex:1; }
  .s-why { color:var(--muted); font-size:9.5px; }
  .rule { height:1px; background:#c9ced6; margin:12px 16px 0; }
  main, .rest { padding:10px 16px 12px; }
  main.grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding-top:12px; }
  .card { border:1px solid var(--line); border-radius:5px; padding:7px 9px 6px; break-inside:avoid; }
  .row1 { display:flex; align-items:center; gap:6px; }
  .num { font-size:9px; color:var(--faint); font-variant-numeric:tabular-nums; width:15px; }
  .chip { font-size:8.5px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; color:#8a4b00; background:#fdf3e3; border:1px solid #f3ddba; border-radius:3px; padding:1px 5px; white-space:nowrap; }
  .val { margin-left:auto; font-size:13px; font-weight:700; letter-spacing:-.02em; }
  .val.tbd { font-weight:400; color:var(--faint); }
  h3 { margin:3px 0 0; font-size:12px; font-weight:650; }
  h3 a { color:inherit; text-decoration:none; }
  .host { font-weight:400; font-size:9px; color:var(--faint); margin-left:5px; }
  .where { margin:1px 0 0; font-size:9.5px; color:var(--muted); }
  .where a { color:var(--muted); text-decoration:none; }
  .comm { margin-left:5px; text-transform:uppercase; letter-spacing:.06em; font-size:8.5px; color:var(--faint); }
  .scope { margin:4px 0 0; font-size:10.5px; color:var(--body); }
  .reach { margin:6px 0 0; padding-top:5px; border-top:1px solid var(--line); font-size:10.5px; line-height:1.5; }
  .who { font-weight:650; }
  .dot { color:var(--faint); margin:0 6px; }
  .lbl { font-size:8px; text-transform:uppercase; letter-spacing:.08em; color:var(--faint); margin-right:2px; }
  a.tel { color:var(--accent); text-decoration:none; font-weight:600; }
  a.mail { color:var(--accent); text-decoration:none; }
  .inbox { color:var(--muted); }
  .meta { margin:5px 0 0; font-size:8.5px; color:var(--muted); }
  .grp { margin:6px 0 14px; }
  .grp h2 { font-size:9px; letter-spacing:.13em; text-transform:uppercase; color:var(--muted); font-weight:600; margin:0 0 4px; border-bottom:1px solid var(--line); padding-bottom:3px; break-after:avoid; }
  .grp h2 span { color:var(--faint); font-weight:400; letter-spacing:0; }
  .grp ul { margin:0; padding:0; list-style:none; }
  .grp li { display:flex; gap:9px; align-items:baseline; padding:3.5px 0; border-bottom:1px solid #f1f3f6; font-size:10.5px; break-inside:avoid; }
  .rn { font-size:8.5px; color:var(--faint); width:14px; flex:none; font-variant-numeric:tabular-nums; }
  .rval { font-weight:700; width:74px; flex:none; }
  .rco { flex:1.4; font-weight:600; }
  .rco a { color:inherit; text-decoration:none; }
  .chip-s { font-size:8px; text-transform:uppercase; letter-spacing:.04em; color:#8a4b00; margin-left:6px; white-space:nowrap; }
  .rmeta { flex:2; color:var(--muted); }
  .rreach { flex:1.2; text-align:right; white-space:nowrap; }
  .none-msg { color:var(--faint); padding:12px 0; }
  footer { padding:7px 16px 11px; font-size:8.5px; color:var(--faint); border-top:1px solid var(--line); margin:0 16px; }
  footer p { margin:0 0 3px; }
  footer a { color:var(--muted); }
  @page { size:A4 landscape; margin:9mm; }
  @media print { .card, .grp li { break-inside:avoid; } .grp h2 { break-after:avoid; } }
"""


def _client_esc(text) -> str:
    return html.escape(str(text or "").strip())


def _client_is_role(email: str) -> bool:
    return bool(email) and email.split("@")[0].strip().lower() in CLIENT_ROLE_INBOXES


def client_tier(x: Lead) -> str:
    """Which part of the sheet a lead lands in: named | direct | mainline | inbox.

    named    : a person is known (contact_name set) -> full card with their name.
    direct   : no person, but an email whose local part is not a role inbox -> full card.
    mainline : no person, email empty or role inbox, but a phone -> compact row.
    inbox    : everything else (role inbox without phone, or nothing at all) -> compact row.
    """
    if (x.contact_name or "").strip():
        return "named"
    email = (x.email or "").strip()
    if email and not _client_is_role(email):
        return "direct"
    if (x.phone or "").strip():
        return "mainline"
    return "inbox"


def _client_money(value) -> str:
    v = value or 0
    return f"${v:,.0f}" if v > 0 else "TBD"


def _client_scope(x: Lead) -> str:
    s = re.sub(r"\s*\[\+\d+ more permit\(s\) this window[^\]]*\]", "", x.description or "")
    s = re.sub(r"^(Commercial / Multi Family Project|Residential Improvement Project)\s*[—-]\s*",
               "", s.strip())
    s = s.strip()
    return "" if s.lower() == "unspecified" else s


def _client_reach(x: Lead, compact: bool = False) -> str:
    """Name, phone and email as real links (role inboxes are muted, not linked)."""
    bits = []
    who = _client_esc(x.contact_name)
    if who:
        bits.append(f'<span class="who">{who}</span>')
    phone = _client_esc(x.phone)
    if phone:
        bits.append(f'<a class="tel" href="{_tel_href(phone)}">{phone}</a>')
    email = _client_esc(x.email)
    if email:
        if _client_is_role(email):
            label = "" if compact else '<span class="lbl">inbox</span>'
            bits.append(f'<span class="inbox">{label}{email}</span>')
        else:
            bits.append(f'<a class="mail" href="mailto:{email}">{email}</a>')
    return '<span class="dot">·</span>'.join(bits)


def _client_chip(x: Lead, css_class: str = "chip") -> str:
    label = CLIENT_PITCH_CHIPS.get((x.pitch_angle or "").strip(), "")
    return f'<span class="{css_class}">{label}</span>' if label else ""


def _client_extra_lines(x: Lead) -> list[str]:
    """'Also filed' lines for the other permits this company filed in the window.

    The CSV keeps one row per company, so the sheet is the only place a GC sees
    every jobsite. Used by both the full card and the compact row.
    """
    lines = []
    for extra in (x.extra_permits or []):
        e_addr = _client_esc(extra.get("address"))
        if extra.get("community"):
            e_addr += f" ({_client_esc(extra['community'])})"
        e_scope = _client_esc(extra.get("description"))
        e_val = f"${extra['permit_value']:,.0f}" if extra.get("permit_value") else "TBD"
        line = " · ".join(p for p in (e_addr, e_scope, e_val) if p)
        e_meta = " · ".join(_client_esc(str(v)) for v in
                            (extra.get("permit_number"), extra.get("applied_date")) if v)
        lines.append(f"Also filed: {line}" + (f" ({e_meta})" if e_meta else ""))
    return lines


def _client_card(x: Lead, n: int) -> str:
    display = (x.unmasked_tradename
               if NUMBERED_RE.match(x.company_name or "") and x.unmasked_tradename
               else x.company_name)
    company = _client_esc(display)
    site = (x.website or "").strip()
    host = _host(site) if site else ""
    name = (f'<a href="{html.escape(site)}" target="_blank" rel="noopener">{company}</a>'
            if site else company)
    if host:
        name += f'<span class="host">{_client_esc(host)}</span>'

    addr = _client_esc(x.address)
    comm = _client_esc(x.community)
    where = ""
    if addr:
        where = (f'<a href="{html.escape(_maps_href(x.address.strip()))}" target="_blank" rel="noopener">{addr}</a>')
    if comm:
        where += f'<span class="comm">{html.escape(comm.title())}</span>'

    scope = _client_esc(_client_scope(x))
    reach = _client_reach(x)
    money = _client_money(x.permit_value)
    val_class = "val tbd" if money == "TBD" else "val"

    parts = [
        f'    <article class="card">',
        f'      <div class="row1"><span class="num">{n:02d}</span>{_client_chip(x)}'
        f'<span class="{val_class}">{money}</span></div>',
        f"      <h3>{name}</h3>",
    ]
    if where:
        parts.append(f'      <p class="where">{where}</p>')
    if scope:
        parts.append(f'      <p class="scope">{scope}</p>')
    if reach:
        parts.append(f'      <div class="reach">{reach}</div>')
    meta = " · ".join(_client_esc(v) for v in (x.permit_number, x.applied_date) if v)
    if meta:
        parts.append(f'      <p class="meta">{meta}</p>')
    for line in _client_extra_lines(x):
        parts.append(f'      <p class="meta">{line}</p>')
    parts.append("    </article>")
    return "\n".join(parts)


def _client_row(x: Lead, n: int) -> str:
    company = _client_esc(x.company_name)
    site = (x.website or "").strip()
    name = (f'<a href="{html.escape(site)}" target="_blank" rel="noopener">{company}</a>'
            if site else company)
    scope = _client_scope(x)
    if len(scope) > 62:
        scope = scope[:59].rstrip() + "…"
    comm = _client_esc(x.community).title()
    meta = " · ".join(p for p in (comm, _client_esc(scope)) if p)
    extras = _client_extra_lines(x)
    if extras:
        meta = " · ".join(p for p in (meta, " · ".join(extras)) if p)
    return (f'      <li><span class="rn">{n:02d}</span>'
            f'<span class="rval">{_client_money(x.permit_value)}</span>'
            f'<span class="rco">{name}{_client_chip(x, "chip-s")}</span>'
            f'<span class="rmeta">{meta}</span>'
            f'<span class="rreach">{_client_reach(x, compact=True)}</span></li>')


CLIENT_GROUP_HEADINGS = {
    "mainline": "Contact by main line",
    "inbox": "General inbox only",
}


def export_client_html(leads: list[Lead], path: Path, title: str) -> None:
    """Self-contained HTML lead sheet, printed landscape A4 or pasted into an email.

    Leads are tiered (see client_tier): named and direct leads get full cards,
    the rest get compact one-line rows under their own headings. Every lead is
    rendered; empty fields are omitted rather than shown as a placeholder.
    """
    tiers: dict[str, list[Lead]] = {"named": [], "direct": [], "mainline": [], "inbox": []}
    for x in leads:
        tiers[client_tier(x)].append(x)
    # Each group is sorted on its own, highest value first. TBD (no value) sorts
    # last because it keys as 0; the sort is stable, so ties keep input order.
    for group in tiers.values():
        group.sort(key=lambda x: x.permit_value or 0, reverse=True)
    # Named and direct are concatenated, so the full-card list needs its own
    # sort: otherwise direct leads land after every named lead regardless of value.
    full = sorted(tiers["named"] + tiers["direct"], key=lambda x: x.permit_value or 0, reverse=True)
    total = sum(x.permit_value or 0 for x in leads)

    strip = ""
    if full:
        items = "".join(
            f'<li><span class="s-val">{_client_money(x.permit_value)}</span>'
            f'<span class="s-co">{_client_esc(x.company_name)}</span>'
            f'<span class="s-why">{_client_reach(x, compact=True)}</span></li>'
            for x in full[:5])
        strip = (f'  <div class="start"><h2>Top projects with contact details</h2>'
                 f'<ol>{items}</ol></div>\n')

    cards = "\n".join(_client_card(x, i + 1) for i, x in enumerate(full))
    rest_html = ""
    n = len(full)
    for key in ("mainline", "inbox"):
        group = tiers[key]
        if not group:
            continue
        rows = []
        for x in group:
            n += 1
            rows.append(_client_row(x, n))
        rest_html += (f'    <section class="grp"><h2>{CLIENT_GROUP_HEADINGS[key]} '
                      f'<span>{len(group)}</span></h2><ul>\n' + "\n".join(rows)
                      + "\n    </ul></section>\n")

    if not leads:
        cards = '    <p class="none-msg">No leads in this list.</p>'

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>{CLIENT_SHEET_CSS}</style></head><body>
<header>
  <div class="kicker">Calgary permit leads</div>
  <h1>{html.escape(title)}</h1>
  <div class="stats">
    <span><b>{len(leads)} leads</b>this run</span>
    <span><b>CAD ${total:,.0f}</b>total estimated value</span>
    <span><b>{len(full)}</b>with a named contact or direct address</span>
    <span><b>{datetime.now().date().isoformat()}</b>prepared</span>
  </div>
{strip}</header>
<div class="rule"></div>
<main class="grid">
{cards}
</main>
<div class="rest">
{rest_html}</div>
<footer>
  <p>All figures in Canadian dollars. Estimated values are the applicant's declared project cost, not contract value. Sorted by estimated value, highest first.</p>
  <p>Contacts were checked against the company's own pages on {datetime.now().date().isoformat()}; an address no page could confirm is shown as unverified, not asserted.</p>
  <p>{OGL_FOOTER}</p>
</footer>
</body></html>
"""
    path.write_text(document, encoding="utf-8")


def export_review_csv(leads: list[Lead], path: Path) -> None:
    """Same readable shape as the client CSV, plus the reason it was flagged."""
    headers = [
        "Applied Date", "Permit #", "Address", "Community",
        "Est. Project Value", "Project Description",
        "Company / Applicant", "Phone", "Email", "Email Status", "Website",
        "Lead Angle", "Why Flagged",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for x in leads:
            display_name = (x.unmasked_tradename
                            if NUMBERED_RE.match(x.company_name) and x.unmasked_tradename
                            else x.company_name)
            val_str = f"${x.permit_value:,.0f}" if x.permit_value > 0 else "TBD (Dev Permit)"
            writer.writerow([
                x.applied_date, x.permit_number, x.address, x.community, val_str,
                x.description, display_name, x.phone, x.email, x.email_status,
                x.website, x.pitch_angle, review_reason(x),
            ])


def print_summary(leads: list[Lead]) -> None:
    ranked = sorted(leads, key=lambda x: x.permit_value, reverse=True)[:10]
    table = Table(title="Calgary Permit Leads — Top 10 by Permit Value", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Permit", style="cyan")
    table.add_column("Lead Type", style="magenta")
    table.add_column("Value", justify="right", style="green")
    table.add_column("Company", style="bold")
    table.add_column("Website", style="blue")
    table.add_column("Phone")
    table.add_column("Email")
    for i, lead in enumerate(ranked, 1):
        table.add_row(
            str(i), lead.permit_number, lead.lead_type,
            f"${lead.permit_value:,.0f}" if lead.permit_value else "—",
            lead.company_name, lead.website or "—", lead.phone or "—", lead.email or "—",
        )
    console.print(table)

    enriched = sum(1 for x in leads if x.phone or x.email or x.website)
    console.print(f"\n[bold]Leads:[/bold] {len(leads)} | [bold]with contact data:[/bold] {enriched} | "
                  f"[bold]emails:[/bold] {sum(1 for x in leads if x.email)} | "
                  f"[bold]phones:[/bold] {sum(1 for x in leads if x.phone)} | "
                  f"[bold]websites:[/bold] {sum(1 for x in leads if x.website)}")
    by_type: dict[str, int] = {}
    for x in leads:
        by_type[x.lead_type] = by_type.get(x.lead_type, 0) + 1
    console.print("[bold]By lead type:[/bold] " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


def _csv_values(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [row for row in csv.reader(fh)]


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    """One row per company: keep the highest-value permit so outreach never duplicates.

    A company that filed several permits in the window would otherwise look like a
    single small job. The kept row carries the roll-up (count and combined value)
    in its description so the reader knows the row represents more than one job.
    """
    groups: dict[str, list[Lead]] = {}
    for x in leads:
        groups.setdefault(cache_key(x.company_name), []).append(x)

    out: list[Lead] = []
    for key, group in groups.items():
        lead = max(group, key=lambda x: x.permit_value or 0)
        total = sum(x.permit_value or 0 for x in group)
        extras = [x for x in group if x is not lead]
        if extras:
            extra_val = total - (lead.permit_value or 0)
            suffix = (f" [+{len(extras)} more permit(s) this window, "
                      f"${extra_val:,.0f} combined]")
            desc = (lead.description or "").strip()
            new_desc = f"{desc}{suffix}" if desc else suffix.strip()
            extra_permits = [
                {
                    "permit_number": x.permit_number,
                    "permit_type": x.permit_type,
                    "applied_date": x.applied_date,
                    "address": x.address,
                    "community": x.community,
                    "permit_value": x.permit_value,
                    "description": x.description,
                    "status": x.status,
                }
                for x in sorted(extras, key=lambda x: -(x.permit_value or 0))
            ]
            if hasattr(lead, "model_copy"):
                lead = lead.model_copy(update={"description": new_desc,
                                               "extra_permits": extra_permits})
            else:
                lead = lead.copy(update={"description": new_desc,
                                         "extra_permits": extra_permits})
        out.append(lead)
    return sorted(out, key=lambda x: -(x.permit_value or 0))


def _reachable(x: Lead) -> bool:
    """Sellable only with a phone, or an email whose domain actually accepts mail."""
    return bool(x.phone) or bool(x.email and x.email_status != "no_mx")


def filter_client_splits(leads: list[Lead], include_tract_builders: bool = False) -> tuple[list[Lead], list[Lead]]:
    """Split into (gc_leads, sub_leads) with contactability and tract-builder filtering."""
    contactable = [x for x in leads if _reachable(x)]
    gc_leads = [
        x for x in contactable
        if x.lead_type in {"NEEDS_GC", "EARLY_DESIGN"}
        and not is_volume_tract_builder(x.company_name, x.unmasked_tradename)
    ]
    sub_leads = [
        x for x in contactable
        if x.lead_type == "SUBTRADE_PITCH"
        and (include_tract_builders or not is_volume_tract_builder(x.company_name, x.unmasked_tradename))
    ]
    # SUBCONTRACTOR rows are excluded from both client lists by design.
    return gc_leads, sub_leads

