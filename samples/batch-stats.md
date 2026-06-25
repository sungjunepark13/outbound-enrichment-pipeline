# Batch enrichment stats (from live store, redacted)

**Cohort:** 812-lead manufacturing batch (Northeast GA job shops). **Pre-send:** enrichment + audit complete; no first touch sent (founder-gated).

| Metric | Value |
|--------|-------|
| Leads in store | 812 |
| Audited (pass 1+2) | 775 |
| Email enrichment run (has-site cohort) | 201 |
| Candidates found (any source) | 195 (97.0%) |
| Send-eligible (verified + risky) | 0 — SMTP pass not run on this batch |
| Routed to call at current tier | 201 (100% until live verify) |

## Note on verification tier

Batch was run with **SMTP verification disabled** (`smtp-disabled`). The waterfall still found candidate addresses (mailto, contact page, JSON-LD, pattern). A live `--live-smtp` pass upgrades `unverified` → `verified`/`risky`/`invalid` before send.

## Email source breakdown (201 enriched)

- `pattern_inferred`: 107 (53.2%)
- `site_mailto`: 65 (32.3%)
- `contact_page`: 19 (9.5%)
- `jsonld`: 4 (2.0%)
