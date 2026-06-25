#!/usr/bin/env python3
"""Export redacted enrichment sample + batch stats from a local lead store.

Usage (paths default to ./data/ — copy your engine-store locally, gitignored):
  python3 scripts/export-redacted-sample.py \\
    --store ./data/pipeline_leads.json \\
    --shards ./data/email_enrich_shards

Outputs:
  samples/enriched-leads-redacted.csv
  samples/batch-stats.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

NAMES = [
    "Peachtree Precision Machining", "Bayou Metal Works", "Summit CNC LLC",
    "Lakeside Fabrication", "Northline Tool & Die", "Heritage Machine Co.",
    "Atlas Job Shop", "Riverbend Metals", "Cornerstone Machining", "Pioneer Precision",
]
CONTACTS = [
    "Jordan Ellis", "Maria Chen", "", "Tom Reed", "Pat Okafor", "Dana Wu",
    "", "Chris Holt", "Leah Park", "",
]


def slug(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:6]


def best_candidate(row: dict) -> dict:
    ee = (row.get("audit") or {}).get("email_enrich") or {}
    log = ee.get("verification_log") or []
    order = {"site_mailto": 0, "contact_page": 1, "jsonld": 2, "rdap": 3, "pattern_inferred": 9}
    cands = sorted(log, key=lambda c: order.get(c.get("source"), 5))
    for c in cands:
        if c.get("address"):
            return c
    return cands[0] if cands else {}


def merged_lead(lead: dict, shard: dict) -> dict:
    cand = best_candidate(shard) if shard else {}
    out = dict(lead)
    if cand:
        out["_cand_email"] = cand.get("address")
        out["_cand_source"] = cand.get("source") or lead.get("email_source")
        out["_cand_evidence"] = cand.get("evidence") or ""
    out["email_confidence"] = lead.get("email_confidence") or "unverified"
    out["email_source"] = lead.get("email_source") or out.get("_cand_source") or ""
    return out


def channel(lead: dict) -> str:
    return "email" if lead.get("email_confidence") in ("verified", "risky") else "call"


def lead_with(lead: dict) -> str:
    return (lead.get("audit") or {}).get("lead_with") or ""


def redact_email(email: str | None, co: str) -> str:
    if not email:
        return ""
    local = email.split("@")[0]
    show = (local[0] + "***" + local[-1]) if len(local) > 2 else local[0] + "***"
    return f"{show}@{co}.example"


def redact_evidence(lead: dict, co: str, cand_ev: str) -> str:
    if cand_ev and str(cand_ev).startswith("http"):
        return f"https://{co}.example/contact"
    if lead.get("website"):
        return f"https://{co}.example/"
    if cand_ev and "pattern:" in str(cand_ev):
        return f"pattern on {co}.example"
    if cand_ev and "mailto:" in str(cand_ev):
        return f"https://{co}.example/ (mailto)"
    return f"https://{co}.example/contact" if lead.get("website") else ""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--shards", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, default=Path("samples"))
    args = p.parse_args()

    leads = json.loads(args.store.read_text())
    shard_enrich: dict = {}
    for shard in sorted(args.shards.glob("shard_*.json")):
        shard_enrich.update(json.loads(shard.read_text()))

    enriched = [merged_lead(l, shard_enrich.get(l.get("place_id"), {}))
                for l in leads if l.get("place_id") in shard_enrich]

    seen: set[tuple[str, str]] = set()
    selected: list[dict] = []
    for l in sorted(enriched, key=lambda z: (z.get("email_source") or "", lead_with(z))):
        key = (l.get("email_source") or "none", lead_with(l))
        if key in seen:
            continue
        seen.add(key)
        selected.append(l)
    for l in enriched:
        if len(selected) >= 10:
            break
        if l not in selected:
            selected.append(l)
    selected = selected[:10]

    rows = []
    for i, l in enumerate(selected):
        co = f"shop{slug(l.get('id', ''))}"
        rows.append({
            "company": NAMES[i],
            "contact_name": CONTACTS[i],
            "email": redact_email(l.get("_cand_email"), co),
            "email_confidence": l.get("email_confidence") or "unverified",
            "email_source": l.get("email_source") or "",
            "evidence_url": redact_evidence(l, co, l.get("_cand_evidence") or ""),
            "lead_with": lead_with(l),
            "channel": channel(l),
            "stage": l.get("stage", "audited"),
        })

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "enriched-leads-redacted.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_all, n_enr = len(leads), len(enriched)
    n_aud = sum(1 for l in leads if (l.get("audit") or {}).get("lead_with"))
    src_c = Counter(l.get("email_source") for l in enriched if l.get("email_source"))
    with_email = sum(1 for l in enriched if l.get("_cand_email"))

    stats = f"""# Batch enrichment stats (from live store, redacted)

**Cohort:** {n_all}-lead manufacturing batch (Northeast GA job shops). **Pre-send:** enrichment + audit complete; no first touch sent (founder-gated).

| Metric | Value |
|--------|-------|
| Leads in store | {n_all} |
| Audited (pass 1+2) | {n_aud} |
| Email enrichment run (has-site cohort) | {n_enr} |
| Candidates found (any source) | {with_email} ({round(100*with_email/n_enr,1) if n_enr else 0}%) |
| Send-eligible (verified + risky) | 0 — SMTP pass not run on this batch |
| Routed to call at current tier | {n_enr} (100% until live verify) |

## Note on verification tier

Batch was run with **SMTP verification disabled** (`smtp-disabled`). The waterfall still found candidate addresses (mailto, contact page, JSON-LD, pattern). A live `--live-smtp` pass upgrades `unverified` → `verified`/`risky`/`invalid` before send.

## Email source breakdown ({n_enr} enriched)

"""
    for k, v in src_c.most_common():
        stats += f"- `{k}`: {v} ({round(100*v/n_enr,1)}%)\n"
    (args.out_dir / "batch-stats.md").write_text(stats)
    print(f"Wrote {csv_path} and {args.out_dir / 'batch-stats.md'}")


if __name__ == "__main__":
    main()
