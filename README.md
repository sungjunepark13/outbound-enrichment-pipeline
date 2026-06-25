# Outbound Enrichment Pipeline

Python pipeline for B2B outbound: multi-source email enrichment with SMTP verification, audit-driven routing, anti-fabrication guardrails, and reply ingest hooks.

Built and dogfooded on an 812-lead manufacturing batch. This repo is a **sanitized public extract** for portfolio review. No client data, API keys, or live campaign credentials.

## What it does

1. **Enrich** — waterfall: site mailto → contact pages → JSON-LD → RDAP → pattern guess; each candidate SMTP-verified with catch-all detection; first passing source wins; evidence URL stored.
2. **Audit** — two-pass site review sets wedge, angle, and outreach frame.
3. **Route** — conditional logic picks channel (email vs call), frame, and offer from audit signals; frames drop if evidence or demo URL missing.
4. **Draft** — personalized outreach gated on audit tokens only (`outreach_guardrail.py`).
5. **Ingest** — Gmail / Instantly reply classification (positive / negative / OOO / unsub).

Founder approves every send before it leaves the system.

## Architecture

```
Source → enrich (SMTP verify) → audit → propose_param_set → guardrail → [approve] → send → reply ingest
```

- **Business-facing diagram:** [docs/pipeline-flow.html](docs/pipeline-flow.html) (open in browser)
- **Engine logic (built vs planned):** [docs/conditional-logic-graph.md](docs/conditional-logic-graph.md)

## Core modules

| File | Role |
|------|------|
| `lead-enrich-email.py` | Multi-source enrichment + SMTP verification |
| `draft-outreach.py` | Frame/angle composer + draft generation |
| `outreach_guardrail.py` | Anti-fabrication validator (claims ⊆ audit tokens) |
| `lead_store.py` | Lead persistence (local JSON or Supabase backend) |
| `pipeline-checkpoint.py` | Reply bucket routing + next-touch proposals |
| `lead-gate.py` | Intake qualification gate |

## Sample output

See [samples/enriched-leads-redacted.csv](samples/enriched-leads-redacted.csv) (10 fictional companies) and [samples/draft-examples.md](samples/draft-examples.md).

| Lead | Email | Confidence | Source |
|------|-------|------------|--------|
| Peachtree Precision | j.ellis@…example | verified | contact_page |
| Summit CNC | — | invalid | — |
| Northline Tool | pokafor@…example | verified | site_mailto |

## Stack

Python 3.11+, optional Supabase/Postgres, Instantly API, Claude API for drafts.

Buyer-facing summary: tested code, approval gates, logging. Not a no-code-only handoff.

## Self-test

```bash
python3 lead-enrich-email.py          # hermetic self-test
python3 outreach_guardrail.py         # guardrail unit checks
python3 draft-outreach.py --help      # draft CLI
```

Batch enrichment requires your own lead store under `./data/` (gitignored).

## Hire

[Upwork — Business Command Center + automation](https://www.upwork.com/freelancers/~01eeb422ba265368f1)

Related public repo: [ops-command-center](https://github.com/sungjunepark13/ops-command-center) (dashboard layer).

## License

MIT — demo/portfolio use. Do not use sample company names for live outreach.
