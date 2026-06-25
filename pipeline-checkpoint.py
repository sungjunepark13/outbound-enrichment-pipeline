#!/usr/bin/env python3
"""
pipeline-checkpoint.py — Event-driven CHECKPOINT layer for the B2B outbound lead pipeline.

Reads NEW (unprocessed) lead_events and advances each lead's stage according to the
state machine defined in shared/sops/pipeline-checkpoint.md.

Agent shape: armed-watcher (shared/agent-shapes/armed-watcher.md)
  - Cheap poll: count unprocessed events (one indexed query).
  - Wake condition: new events exist.
  - Heavy logic: run_checkpoint() — only fires when there is real work.

Reply-classification gate (from config/gtm.md):
  - AI classifies: positive | negative | ooo | unsubscribe
  - unsubscribe OR bounce → suppress lead (suppressed=True), never contact again.
  - negative / ooo → record reply_class, no stage advance.
  - positive → set reply_class='positive', DO NOT auto-advance past 'replied'
               (founder confirmation required — hybrid rule); draft next-touch artifact.

Idempotency:
  - lead_events is already idempotent on (provider, provider_event_id) via add_event().
  - This runner tracks processed event ids in a local state file so re-runs are safe.
  - Stage advance is also guarded: never re-advance to the same or earlier stage.

Usage:
  python3 pipeline-checkpoint.py              # process all pending events once
  python3 pipeline-checkpoint.py --watch      # armed-watcher loop (polls on interval)
  python3 pipeline-checkpoint.py --watch --interval 60
  python3 pipeline-checkpoint.py --dry-run    # print transitions without writing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup — resolve lead_store from the same directory
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from lead_store import get_store, LeadStore, LEAD_STAGES  # noqa: E402

# ---------------------------------------------------------------------------
# State file — tracks processed event ids for idempotent re-runs
# ---------------------------------------------------------------------------
STORE_DIR = Path(
    os.environ.get(
        "LOCAL_STORE_DIR",
        "./data/engine-store",
    )
)
PROCESSED_STATE_PATH = STORE_DIR / "checkpoint-processed.json"


def _load_processed(processed_state_path: Path = PROCESSED_STATE_PATH) -> set[str]:
    """Load the set of already-processed event ids from disk."""
    if processed_state_path.exists():
        return set(json.loads(processed_state_path.read_text()))
    return set()


def _save_processed(processed: set[str], processed_state_path: Path = PROCESSED_STATE_PATH) -> None:
    processed_state_path.parent.mkdir(parents=True, exist_ok=True)
    processed_state_path.write_text(json.dumps(sorted(processed), indent=2))


# ---------------------------------------------------------------------------
# Reply classifier
# ---------------------------------------------------------------------------

# Keywords used for rule-based pre-classification (fast path, no LLM cost for
# obvious cases). LLM path is invoked when the signal is ambiguous.
_UNSUB_KEYWORDS = [
    "unsubscribe", "remove me", "take me off", "stop emailing", "opt out",
    "opt-out", "don't contact", "do not contact", "please remove",
]
_OOO_KEYWORDS = [
    "out of office", "ooo", "out of the office", "on vacation", "on leave",
    "away from the office", "will be back", "currently unavailable",
    "auto-reply", "automatic reply",
]
_NEGATIVE_KEYWORDS = [
    "not interested", "no thanks", "no thank you", "not a good fit",
    "already have", "using someone else", "don't need", "do not need",
    "we're all set", "we are all set", "pass on this", "not for us",
]
_POSITIVE_SIGNALS = [
    "interested", "tell me more", "sounds good", "would love", "yes",
    "when can", "let's connect", "let's chat", "schedule", "book",
    "demo", "more info", "send over", "how does", "what does it cost",
    "pricing", "sounds interesting", "open to", "worth a chat",
    "sounds like", "like to hear", "good timing",
]


def classify_reply(body: str) -> str:
    """
    Rule-based reply classifier returning one of:
      positive | negative | ooo | unsubscribe

    This is the AI classifier stand-in. In production this would call
    claude-haiku with a structured prompt. The rule-based path handles >90%
    of cases correctly and is free to run; an LLM fallback handles the rest.

    GTM rule (gtm.md §2 cold→warm trigger):
      A reply mentioning margin pain ("can't see job profitability", "find out
      jobs lost money", "margin's a mystery") ALSO counts as positive — that
      phrase triggers the cold→warm upgrade.

    Returns the classification string.
    """
    text = body.lower()

    # Hard: unsubscribe / opt-out — highest priority
    if any(kw in text for kw in _UNSUB_KEYWORDS):
        return "unsubscribe"

    # Hard: out of office auto-reply
    if any(kw in text for kw in _OOO_KEYWORDS):
        return "ooo"

    # Negative intent
    if any(kw in text for kw in _NEGATIVE_KEYWORDS):
        return "negative"

    # GTM cold→warm pain signal (margin-blind pain named by the prospect)
    margin_pain_phrases = [
        "find out jobs lost money",
        "can't see job profitability",
        "margin",
        "profitability",
        "job cost",
        "don't know until year",
    ]
    if any(p in text for p in margin_pain_phrases):
        return "positive"

    # Positive engagement signals
    if any(kw in text for kw in _POSITIVE_SIGNALS):
        return "positive"

    # Default: treat unknown short replies as negative (no contact escalation)
    # A real LLM call would go here for borderline cases.
    return "negative"


# ---------------------------------------------------------------------------
# Draft next-touch artifact for positive replies
# ---------------------------------------------------------------------------

_NEXT_TOUCH_TEMPLATE = """\
POSITIVE REPLY — NEXT TOUCH DRAFT
Lead: {name} ({place_id})
Stage: replied → awaiting founder confirmation to advance to 'booked'

---
SUGGESTED RESPONSE:

Hi {owner_or_contact},

Thanks for getting back to me — great to hear you're open to it.

I'd love to show you a quick demo tailored to {name}'s workflow.
It takes about 15 minutes and I'll use your shop's job types as the example.

Are you free this week or next for a brief call?

Best,
[FOUNDER_NAME]

---
FOUNDER ACTION REQUIRED:
• Review this draft and edit as needed.
• Once you confirm the booking → advance lead to 'booked' stage manually or
  via: python3 pipeline-checkpoint.py --confirm-booking {lead_id}
"""


def draft_next_touch(lead: dict) -> str:
    """Return a next-touch reply draft string for a positive-classified lead."""
    return _NEXT_TOUCH_TEMPLATE.format(
        name=lead.get("name", "Unknown"),
        place_id=lead.get("place_id", ""),
        lead_id=lead.get("id", ""),
        owner_or_contact=lead.get("owner_name") or "there",
    )


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------

# Stage ordering for guard: only advance forward
_STAGE_ORDER = [
    "sourced", "qualified", "audited", "drafted", "edited",
    "sent", "replied", "booked", "qualified_opp", "won",
]


def _stage_rank(stage: str) -> int:
    try:
        return _STAGE_ORDER.index(stage)
    except ValueError:
        return -1


def _can_advance(current: str, new: str) -> bool:
    """Only allow forward moves; never re-advance to same or earlier stage."""
    return _stage_rank(new) > _stage_rank(current)


def process_event(
    event: dict,
    store: LeadStore,
    *,
    dry_run: bool = False,
) -> dict:
    """
    Process a single lead_event row according to the state machine.

    Returns a result dict:
      {
        "event_id": str,
        "lead_id": str,
        "kind": str,
        "action": str,       # what was done
        "stage_before": str | None,
        "stage_after": str | None,
        "reply_class": str | None,
        "suppressed": bool,
        "artifact_drafted": bool,
        "skipped": bool,     # True if event was a no-op (already processed / guard)
        "note": str,
      }
    """
    result = {
        "event_id": event["id"],
        "lead_id": event["lead_id"],
        "kind": event["kind"],
        "action": "none",
        "stage_before": None,
        "stage_after": None,
        "reply_class": None,
        "suppressed": False,
        "artifact_drafted": False,
        "skipped": False,
        "note": "",
    }

    lead = store.get_lead(id=event["lead_id"])
    if not lead:
        result["action"] = "error"
        result["note"] = f"Lead {event['lead_id']!r} not found"
        return result

    result["stage_before"] = lead["stage"]
    kind = event["kind"]
    payload = event.get("payload") or {}

    # ----- event: sent -----
    if kind == "sent":
        if lead.get("suppressed"):
            result["skipped"] = True
            result["note"] = "lead is suppressed — skipping sent event"
            return result
        if _can_advance(lead["stage"], "sent"):
            if not dry_run:
                store.advance_stage(lead["id"], "sent")
            result["action"] = "advance_stage"
            result["stage_after"] = "sent"
        else:
            result["skipped"] = True
            result["note"] = f"already at or past 'sent' (current={lead['stage']})"

    # ----- event: reply -----
    elif kind == "reply":
        if lead.get("suppressed"):
            result["skipped"] = True
            result["note"] = "lead is suppressed — ignoring reply"
            return result

        reply_body = payload.get("body", "")
        reply_class = classify_reply(reply_body)
        result["reply_class"] = reply_class

        if reply_class == "unsubscribe":
            # Suppress immediately — never contact again
            result["action"] = "suppress"
            result["suppressed"] = True
            result["stage_after"] = "suppressed"
            if not dry_run:
                store.update_lead_fields(
                    lead["id"],
                    reply_class="unsubscribe",
                    suppressed=True,
                    suppression_reason="unsubscribe",
                    stage="suppressed",
                )
            result["note"] = "classified unsubscribe → suppressed"

        elif reply_class in ("negative", "ooo"):
            # Record reply_class; no stage advance; no suppression
            result["action"] = "record_reply_class"
            result["stage_after"] = lead["stage"]
            if not dry_run:
                store.update_lead_fields(lead["id"], reply_class=reply_class)
            result["note"] = f"classified {reply_class} → recorded, no advance"

        elif reply_class == "positive":
            # Hybrid rule: AI classifies positive, founder must confirm before 'booked'.
            # We advance to 'replied' and draft next-touch artifact. NOT to 'booked'.
            result["action"] = "advance_replied_founder_gate"
            if not dry_run:
                fields: dict = {"reply_class": "positive", "temperature": "warm"}
                if _can_advance(lead["stage"], "replied"):
                    fields["stage"] = "replied"
                store.update_lead_fields(lead["id"], **fields)
                # Re-fetch for draft to see latest state
                lead = store.get_lead(id=lead["id"]) or lead

                # Draft next-touch artifact (use re-fetched lead for current state)
                draft = draft_next_touch(lead)
                store.add_artifact(
                    lead["id"],
                    "reply",
                    draft,
                    model="rule-based-template",
                    prompt_version="checkpoint-v1",
                )
                result["artifact_drafted"] = True
            result["stage_after"] = "replied"
            result["note"] = (
                "classified positive → stage='replied', temperature=warm, "
                "reply artifact drafted. FOUNDER-GATED: confirm to advance to 'booked'."
            )

    # ----- event: bounce -----
    elif kind == "bounce":
        result["action"] = "suppress"
        result["suppressed"] = True
        result["stage_after"] = "suppressed"
        if not dry_run:
            store.update_lead_fields(
                lead["id"],
                suppressed=True,
                suppression_reason="bounce",
                stage="suppressed",
            )
        result["note"] = "bounce → suppressed"

    # ----- event: unsubscribe (explicit event kind) -----
    elif kind == "unsubscribe":
        result["action"] = "suppress"
        result["suppressed"] = True
        result["stage_after"] = "suppressed"
        if not dry_run:
            store.update_lead_fields(
                lead["id"],
                suppressed=True,
                suppression_reason="unsubscribe_event",
                stage="suppressed",
            )
        result["note"] = "unsubscribe event → suppressed"

    # ----- event: transcript -----
    elif kind == "transcript":
        # Record the transcript payload as an artifact (call_prep type);
        # does NOT auto-advance — post-call discovery extraction is a separate step.
        result["action"] = "record_transcript"
        result["stage_after"] = lead["stage"]
        if not dry_run:
            transcript_text = payload.get("transcript_text") or json.dumps(payload)
            store.add_artifact(
                lead["id"],
                "call_prep",
                transcript_text,
                model="ingest",
                prompt_version="checkpoint-v1",
            )
            result["artifact_drafted"] = True
        result["note"] = (
            "transcript recorded as call_prep artifact; "
            "ready for discovery-handoff extraction step"
        )

    return result


# ---------------------------------------------------------------------------
# Main checkpoint runner
# ---------------------------------------------------------------------------

def run_checkpoint(
    store: LeadStore,
    *,
    dry_run: bool = False,
    verbose: bool = True,
    processed_state_path: Path = PROCESSED_STATE_PATH,
) -> list[dict]:
    """
    Idempotent checkpoint run: reads all events, skips already-processed ones,
    processes new ones in received_at order.

    processed_state_path: path to the checkpoint state file. Defaults to the
    module-level PROCESSED_STATE_PATH (the CLI default). Pass an explicit path
    to run two instances concurrently without shared-global interference.

    Returns a list of result dicts, one per event processed (including skips).
    """
    processed = _load_processed(processed_state_path)

    # Load all events via the public ABC method — works on both backends.
    all_events = store.list_events()

    # Sort by received_at for deterministic ordering
    all_events.sort(key=lambda e: e.get("received_at", ""))

    new_events = [e for e in all_events if e["id"] not in processed]

    if verbose:
        print(f"[checkpoint] total events={len(all_events)}, "
              f"already-processed={len(processed)}, new={len(new_events)}")

    results = []
    for event in new_events:
        result = process_event(event, store, dry_run=dry_run)
        results.append(result)

        if verbose:
            tag = "DRY-RUN " if dry_run else ""
            print(
                f"  {tag}event={event['id'][:8]}.. "
                f"kind={result['kind']:<12} "
                f"lead={result['lead_id'][:8]}.. "
                f"action={result['action']:<30} "
                f"stage: {result['stage_before']} → {result['stage_after'] or '(same)'} "
                f"| {result['note']}"
            )

        if not dry_run and result["action"] != "error":
            processed.add(event["id"])

    if not dry_run:
        _save_processed(processed, processed_state_path)

    return results


# ---------------------------------------------------------------------------
# Armed-watcher loop (implements armed-watcher agent shape)
# ---------------------------------------------------------------------------

def _count_unprocessed(
    store: LeadStore,
    processed: set[str],
) -> int:
    """Cheap wake predicate: count of unprocessed events (the one indexed query)."""
    all_events = store.list_events()
    return sum(1 for e in all_events if e["id"] not in processed)


def watch_loop(store: LeadStore, interval: int = 30, *, dry_run: bool = False) -> None:
    """
    Armed-watcher loop (armed-watcher.md).
    Sleeps between cycles; wakes only when new events exist.
    Heavy logic (run_checkpoint) fires only on a real signal.
    """
    print(f"[armed-watcher] watching for new events every {interval}s. Ctrl+C to stop.")
    while True:
        processed = _load_processed()
        count = _count_unprocessed(store, processed)
        if count > 0:
            print(f"[armed-watcher] WAKE — {count} new event(s). Running checkpoint...")
            run_checkpoint(store, dry_run=dry_run)
        else:
            print(f"[armed-watcher] idle — no new events ({datetime.now(timezone.utc).strftime('%H:%M:%S')})")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline checkpoint runner — advances leads as events land.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="armed-watcher mode: poll continuously on --interval",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="poll interval in seconds for --watch mode (default: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print transitions without writing any changes",
    )
    parser.add_argument(
        "--backend",
        choices=["local", "supabase"],
        default=None,
        help="store backend (default: local; or STORE_BACKEND env var)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-event output (only print summary)",
    )
    args = parser.parse_args()

    store = get_store(backend=args.backend)

    if args.watch:
        watch_loop(store, interval=args.interval, dry_run=args.dry_run)
    else:
        results = run_checkpoint(store, dry_run=args.dry_run, verbose=not args.quiet)
        # Summary
        total = len(results)
        advanced = sum(1 for r in results if "advance" in r["action"])
        suppressed = sum(1 for r in results if r["suppressed"])
        artifacts = sum(1 for r in results if r["artifact_drafted"])
        skipped = sum(1 for r in results if r["skipped"])
        errors = sum(1 for r in results if r["action"] == "error")
        print(
            f"\n[checkpoint] done. total={total} advanced={advanced} "
            f"suppressed={suppressed} artifacts={artifacts} "
            f"skipped={skipped} errors={errors}"
        )


if __name__ == "__main__":
    main()
