#!/usr/bin/env python3
"""
lead_store.py — Persistence adapter for the signal-led outbound engine.

Implements the 3-table data model from the brief:
  pipeline_leads / lead_artifacts / lead_events

TWO BACKENDS behind one LeadStore interface:
  - LocalJsonBackend  (default, LIVE)
  - SupabaseBackend   (thin implementation, flip via STORE_BACKEND=supabase or env)

SWAP: set env var STORE_BACKEND=supabase (or pass backend='supabase' to get_store()).
      That is the one-line config change — no rewrite needed.

CLI loader usage:
  python3 lead_store.py load-audit \
    /path/to/audit-hassite-batch200.json \
    /path/to/audit-full-612.json \
    [--master /path/to/leads-master.json] \
    [--hassite-source /path/to/leads-hassite-batch200.json] \
    [--backend local|supabase] \
    [--dry-run]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
import copy
import difflib
import argparse
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ENUMS (enforced as Python sets; Postgres enums live in the migration)
# ---------------------------------------------------------------------------

LEAD_STAGES = {
    "sourced", "qualified", "audited", "drafted", "edited",
    "sent", "replied", "booked", "qualified_opp", "won", "lost", "suppressed",
}

LEAD_WEDGES = {
    "L0_candidate",  # no-website lead
    "L2",            # has-site, RFQ/front-office gap
    "L3",            # has-site, margin/analytics gap
    "off_icp",       # confirmed not ICP
    None,            # fetch-error / unclassified
}

PAIN_TAGS = {
    "no_web_presence",
    "front_office_gap",
    "margin_blind",
    "none",
    None,
}

TEMPERATURES = {"cold", "warm", None}

CONFIDENCE_TIERS = {"confirmed", "suspected", "inferred", None}

ARTIFACT_TYPES = {
    "cold_email", "follow_up", "loom_script", "call_prep", "reply",
}

EVENT_KINDS = {
    "sent", "reply", "transcript", "bounce", "unsubscribe",
}

PROVIDERS = {"gmail", "instantly", None}

REPLY_CLASSES = {"positive", "negative", "ooo", "unsubscribe", None}


def _validate_enum(value, allowed: set, field: str) -> None:
    if value not in allowed:
        raise ValueError(f"Invalid {field}={value!r}. Allowed: {allowed}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DATA MODEL HELPERS
# ---------------------------------------------------------------------------

def _make_lead_row(
    place_id: str,
    name: str,
    *,
    website: Optional[str] = None,
    niche: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    addr: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    phone: Optional[str] = None,
    owner_name: Optional[str] = None,
    founded_year: Optional[int] = None,
    size_band: Optional[str] = None,
    web_presence: Optional[str] = None,
    confidence_tier: Optional[str] = None,
    evidence: Optional[dict] = None,
    pain_tag: Optional[str] = None,
    temperature: Optional[str] = None,
    wedge: Optional[str] = None,
    audit: Optional[dict] = None,
    stage: str = "audited",
    next_action: Optional[str] = None,
    suppressed: bool = False,
    suppression_reason: Optional[str] = None,
    reply_class: Optional[str] = None,
    booked: bool = False,
    qualified_opp: bool = False,
    closed_won: bool = False,
    current_artifacts: Optional[dict] = None,
    source_wave: Optional[str] = None,
    row_id: Optional[str] = None,
) -> dict:
    _validate_enum(stage, LEAD_STAGES, "stage")
    _validate_enum(wedge, LEAD_WEDGES, "wedge")
    _validate_enum(pain_tag, PAIN_TAGS, "pain_tag")
    _validate_enum(temperature, TEMPERATURES, "temperature")
    _validate_enum(confidence_tier, CONFIDENCE_TIERS, "confidence_tier")
    _validate_enum(reply_class, REPLY_CLASSES, "reply_class")

    now = _now_iso()
    return {
        "id": row_id or str(uuid.uuid4()),
        "place_id": place_id,
        "name": name,
        "niche": niche,
        "city": city,
        "region": region,
        "addr": addr,
        "lat": lat,
        "lng": lng,
        "phone": phone,
        "website": website,
        "owner_name": owner_name,
        "founded_year": founded_year,
        "size_band": size_band,
        "web_presence": web_presence,
        "confidence_tier": confidence_tier,
        "evidence": evidence or {},
        "pain_tag": pain_tag,
        "temperature": temperature,
        "wedge": wedge,
        "audit": audit or {},
        "stage": stage,
        "next_action": next_action,
        "suppressed": suppressed,
        "suppression_reason": suppression_reason,
        "reply_class": reply_class,
        "booked": booked,
        "qualified_opp": qualified_opp,
        "closed_won": closed_won,
        "current_artifacts": current_artifacts or {},
        "source_wave": source_wave,
        "created_at": now,
        "updated_at": now,
    }


def _make_artifact_row(
    lead_id: str,
    artifact_type: str,
    draft: str,
    *,
    final: Optional[str] = None,
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    param_stamp: Optional[dict] = None,
    artifact_id: Optional[str] = None,
) -> dict:
    _validate_enum(artifact_type, ARTIFACT_TYPES, "artifact_type")
    now = _now_iso()
    diff = None
    if final is not None:
        diff = "\n".join(
            difflib.unified_diff(
                draft.splitlines(),
                final.splitlines(),
                lineterm="",
                n=2,
            )
        )
    return {
        "id": artifact_id or str(uuid.uuid4()),
        "lead_id": lead_id,
        "artifact_type": artifact_type,
        "draft": draft,
        "final": final,
        "diff": diff,
        "model": model,
        "prompt_version": prompt_version,
        "param_stamp": param_stamp,
        "drafted_at": now,
        "edited_at": now if final else None,
        "promoted_to_learning": False,
    }


def _make_event_row(
    lead_id: str,
    kind: str,
    provider: Optional[str],
    provider_event_id: str,
    payload: Optional[dict] = None,
) -> dict:
    _validate_enum(kind, EVENT_KINDS, "kind")
    _validate_enum(provider, PROVIDERS, "provider")
    return {
        "id": str(uuid.uuid4()),
        "lead_id": lead_id,
        "kind": kind,
        "provider": provider,
        "provider_event_id": provider_event_id,
        "payload": payload or {},
        "received_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# ABSTRACT INTERFACE
# ---------------------------------------------------------------------------

class LeadStore(ABC):
    """
    Public API — both backends implement all methods below.

    Swap backends with a one-line config change:
        store = get_store(backend="supabase")   # or STORE_BACKEND=supabase env var
        store = get_store()                     # defaults to local
    """

    # --- pipeline_leads ---

    @abstractmethod
    def upsert_lead(self, record: dict) -> dict:
        """
        Insert or update a pipeline_leads row. Idempotent on place_id.
        Returns the stored row (with id / timestamps).
        record keys mirror _make_lead_row() kwargs — pass a dict built from it.
        """

    @abstractmethod
    def get_lead(self, *, id: Optional[str] = None, place_id: Optional[str] = None) -> Optional[dict]:
        """Fetch one lead by uuid id OR place_id. Returns None if not found."""

    @abstractmethod
    def list_leads(self, filters: Optional[dict] = None) -> list[dict]:
        """
        Return pipeline_leads rows. filters dict supports:
            stage, wedge, suppressed, pain_tag, temperature, confidence_tier
        All filters are ANDed. Omit or pass None for no filtering.
        """

    @abstractmethod
    def advance_stage(self, lead_id: str, new_stage: str) -> dict:
        """Advance a lead to new_stage. Enforces LEAD_STAGES enum. Returns updated row."""

    # --- lead_artifacts ---

    @abstractmethod
    def add_artifact(
        self,
        lead_id: str,
        artifact_type: str,
        draft: str,
        *,
        model: Optional[str] = None,
        prompt_version: Optional[str] = None,
        param_stamp: Optional[dict] = None,
    ) -> dict:
        """
        Append a new draft artifact for a lead.
        Updates lead's current_artifacts[artifact_type] cache.
        Returns the stored artifact row.

        param_stamp: optional dict of {channel, angle, frame, offer, ask, touch,
        voice_profile, prompt_version} written to the artifact for Phase 3
        diff-learning attribution. Stored in artifact.param_stamp (dedicated field,
        NOT serialized into prompt_version).
        """

    @abstractmethod
    def set_artifact_final(
        self, artifact_id: str, final: str, *, diff: Optional[str] = None
    ) -> dict:
        """
        Record the founder-edited final text on an artifact.
        Computes diff if not provided. Sets edited_at. Returns updated artifact row.
        """

    @abstractmethod
    def list_artifacts(self, lead_id: str) -> list[dict]:
        """Return all artifacts for a lead, oldest first."""

    # --- lead_events ---

    @abstractmethod
    def add_event(
        self,
        lead_id: str,
        kind: str,
        provider: Optional[str],
        provider_event_id: str,
        payload: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        Append an event. Idempotent on (provider, provider_event_id).
        Returns the stored row, or None if it was a duplicate (already ingested).
        """

    @abstractmethod
    def list_events(self, filters: Optional[dict] = None) -> list[dict]:
        """
        Return all lead_events rows as a flat list.
        filters dict supports: lead_id, kind, provider.
        All filters are ANDed. Omit or pass None for no filtering.
        This is the public replacement for the LocalJsonBackend._load_events() private reach.
        """

    @abstractmethod
    def update_lead_fields(self, lead_id: str, **fields) -> dict:
        """
        Atomically update a subset of fields on a pipeline_leads row identified by id.

        Validates 'stage' against LEAD_STAGES when present — raises ValueError on invalid.
        Validates 'reply_class' against REPLY_CLASSES when present.
        Validates 'temperature' against TEMPERATURES when present.
        Always sets updated_at to now.
        Returns the updated row. Raises KeyError if lead not found.

        Use this (NOT upsert_lead) for reply_class / temperature / suppressed /
        suppression_reason writes from the checkpoint runner so the stage enum guard
        is always enforced.
        """


# ---------------------------------------------------------------------------
# LOCAL JSON BACKEND (LIVE)
# ---------------------------------------------------------------------------

LOCAL_STORE_DIR = Path(
    os.environ.get(
        "LOCAL_STORE_DIR",
        "./data/engine-store",
    )
)


class LocalJsonBackend(LeadStore):
    """
    Persists the 3-table model as JSON files under engine-store/.
    Thread-safety: not designed for concurrent writers — single-process only.
    """

    def __init__(self, store_dir: Path = LOCAL_STORE_DIR) -> None:
        self._dir = store_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._leads_path = self._dir / "pipeline_leads.json"
        self._artifacts_path = self._dir / "lead_artifacts.json"
        self._events_path = self._dir / "lead_events.json"
        # In-memory state — loaded lazily
        self._leads: Optional[dict[str, dict]] = None        # keyed by place_id
        self._artifacts: Optional[list[dict]] = None
        self._events: Optional[dict[tuple, dict]] = None     # keyed by (provider, provider_event_id)

    # --- private IO helpers ---

    def _load_leads(self) -> dict[str, dict]:
        if self._leads is None:
            if self._leads_path.exists():
                raw = json.loads(self._leads_path.read_text())
                self._leads = {r["place_id"]: r for r in raw}
            else:
                self._leads = {}
        return self._leads

    def _save_leads(self) -> None:
        rows = list(self._load_leads().values())
        self._leads_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    def _load_artifacts(self) -> list[dict]:
        if self._artifacts is None:
            if self._artifacts_path.exists():
                self._artifacts = json.loads(self._artifacts_path.read_text())
            else:
                self._artifacts = []
        return self._artifacts

    def _save_artifacts(self) -> None:
        self._artifacts_path.write_text(
            json.dumps(self._load_artifacts(), indent=2, ensure_ascii=False)
        )

    def _load_events(self) -> dict[tuple, dict]:
        if self._events is None:
            if self._events_path.exists():
                rows = json.loads(self._events_path.read_text())
                self._events = {(r["provider"], r["provider_event_id"]): r for r in rows}
            else:
                self._events = {}
        return self._events

    def _save_events(self) -> None:
        rows = list(self._load_events().values())
        self._events_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False))

    # --- pipeline_leads ---

    def upsert_lead(self, record: dict) -> dict:
        store = self._load_leads()
        place_id = record["place_id"]
        if place_id in store:
            existing = store[place_id]
            updated = copy.deepcopy(existing)
            for k, v in record.items():
                if k not in ("id", "created_at"):
                    updated[k] = v
            updated["updated_at"] = _now_iso()
            store[place_id] = updated
            self._save_leads()
            return updated
        else:
            row = copy.deepcopy(record)
            if "id" not in row or not row["id"]:
                row["id"] = str(uuid.uuid4())
            now = _now_iso()
            row.setdefault("created_at", now)
            row["updated_at"] = now
            store[place_id] = row
            self._save_leads()
            return row

    def get_lead(self, *, id: Optional[str] = None, place_id: Optional[str] = None) -> Optional[dict]:
        store = self._load_leads()
        if place_id:
            return store.get(place_id)
        if id:
            for row in store.values():
                if row["id"] == id:
                    return row
        return None

    def list_leads(self, filters: Optional[dict] = None) -> list[dict]:
        store = self._load_leads()
        rows = list(store.values())
        if not filters:
            return rows
        result = []
        for row in rows:
            match = True
            for k, v in filters.items():
                if row.get(k) != v:
                    match = False
                    break
            if match:
                result.append(row)
        return result

    def advance_stage(self, lead_id: str, new_stage: str) -> dict:
        _validate_enum(new_stage, LEAD_STAGES, "stage")
        lead = self.get_lead(id=lead_id)
        if not lead:
            raise KeyError(f"Lead id={lead_id!r} not found")
        lead["stage"] = new_stage
        lead["updated_at"] = _now_iso()
        store = self._load_leads()
        store[lead["place_id"]] = lead
        self._save_leads()
        return lead

    # --- lead_artifacts ---

    def add_artifact(
        self,
        lead_id: str,
        artifact_type: str,
        draft: str,
        *,
        model: Optional[str] = None,
        prompt_version: Optional[str] = None,
        param_stamp: Optional[dict] = None,
    ) -> dict:
        row = _make_artifact_row(
            lead_id, artifact_type, draft,
            model=model,
            prompt_version=prompt_version,
            param_stamp=param_stamp,
        )
        artifacts = self._load_artifacts()
        artifacts.append(row)
        self._save_artifacts()
        # Update current_artifacts cache on the lead
        lead = self.get_lead(id=lead_id)
        if lead:
            lead.setdefault("current_artifacts", {})[artifact_type] = {
                "artifact_id": row["id"],
                "draft": draft,
                "final": None,
                "drafted_at": row["drafted_at"],
            }
            lead["updated_at"] = _now_iso()
            store = self._load_leads()
            store[lead["place_id"]] = lead
            self._save_leads()
        return row

    def set_artifact_final(
        self, artifact_id: str, final: str, *, diff: Optional[str] = None
    ) -> dict:
        artifacts = self._load_artifacts()
        for i, row in enumerate(artifacts):
            if row["id"] == artifact_id:
                row = copy.deepcopy(row)
                row["final"] = final
                row["edited_at"] = _now_iso()
                if diff:
                    row["diff"] = diff
                else:
                    row["diff"] = "\n".join(
                        difflib.unified_diff(
                            (row["draft"] or "").splitlines(),
                            final.splitlines(),
                            lineterm="",
                            n=2,
                        )
                    )
                artifacts[i] = row
                self._save_artifacts()
                # Sync current_artifacts cache
                lead = self.get_lead(id=row["lead_id"])
                if lead:
                    cache = lead.setdefault("current_artifacts", {})
                    atype = row["artifact_type"]
                    if atype in cache and cache[atype].get("artifact_id") == artifact_id:
                        cache[atype]["final"] = final
                        cache[atype]["edited_at"] = row["edited_at"]
                    lead["updated_at"] = _now_iso()
                    store = self._load_leads()
                    store[lead["place_id"]] = lead
                    self._save_leads()
                return row
        raise KeyError(f"Artifact id={artifact_id!r} not found")

    def list_artifacts(self, lead_id: str) -> list[dict]:
        artifacts = self._load_artifacts()
        return [a for a in artifacts if a["lead_id"] == lead_id]

    # --- lead_events ---

    def add_event(
        self,
        lead_id: str,
        kind: str,
        provider: Optional[str],
        provider_event_id: str,
        payload: Optional[dict] = None,
    ) -> Optional[dict]:
        events = self._load_events()
        key = (provider, provider_event_id)
        if key in events:
            return None  # idempotent — already ingested
        row = _make_event_row(lead_id, kind, provider, provider_event_id, payload)
        events[key] = row
        self._save_events()
        return row

    def list_events(self, filters: Optional[dict] = None) -> list[dict]:
        rows = list(self._load_events().values())
        if not filters:
            return rows
        result = []
        for row in rows:
            match = True
            for k, v in filters.items():
                if row.get(k) != v:
                    match = False
                    break
            if match:
                result.append(row)
        return result

    def update_lead_fields(self, lead_id: str, **fields) -> dict:
        # Validate enums when the caller passes them
        if "stage" in fields:
            _validate_enum(fields["stage"], LEAD_STAGES, "stage")
        if "reply_class" in fields:
            _validate_enum(fields["reply_class"], REPLY_CLASSES, "reply_class")
        if "temperature" in fields:
            _validate_enum(fields["temperature"], TEMPERATURES, "temperature")

        lead = self.get_lead(id=lead_id)
        if not lead:
            raise KeyError(f"Lead id={lead_id!r} not found")
        for k, v in fields.items():
            lead[k] = v
        lead["updated_at"] = _now_iso()
        store = self._load_leads()
        store[lead["place_id"]] = lead
        self._save_leads()
        return lead


# ---------------------------------------------------------------------------
# SUPABASE BACKEND (ready to flip — import-safe, no migration required)
# ---------------------------------------------------------------------------

class SupabaseBackend(LeadStore):
    """
    Thin Supabase implementation wired to the same 3-table schema.
    Requires:
        pip install supabase
        SUPABASE_URL and SUPABASE_SERVICE_KEY env vars (or supabase-py config)

    This class is import-safe even without supabase-py installed — it raises
    ImportError only when instantiated, not at module import time.

    The migration (pipeline_leads, lead_artifacts, lead_events tables + indexes)
    must be applied via /hq-architect before activating this backend.
    """

    def __init__(self) -> None:
        try:
            from supabase import create_client, Client  # type: ignore
        except ImportError as e:
            raise ImportError(
                "supabase-py not installed. Run: pip install supabase"
            ) from e

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set for SupabaseBackend"
            )
        self._client = create_client(url, key)

    def _table(self, name: str):
        return self._client.table(name)

    # --- pipeline_leads ---

    def upsert_lead(self, record: dict) -> dict:
        """Upsert on place_id (unique constraint). Postgres handles the merge."""
        row = copy.deepcopy(record)
        row.setdefault("id", str(uuid.uuid4()))
        now = _now_iso()
        row.setdefault("created_at", now)
        row["updated_at"] = now
        result = (
            self._table("pipeline_leads")
            .upsert(row, on_conflict="place_id")
            .execute()
        )
        return result.data[0]

    def get_lead(self, *, id: Optional[str] = None, place_id: Optional[str] = None) -> Optional[dict]:
        if place_id:
            result = self._table("pipeline_leads").select("*").eq("place_id", place_id).execute()
        elif id:
            result = self._table("pipeline_leads").select("*").eq("id", id).execute()
        else:
            return None
        return result.data[0] if result.data else None

    def list_leads(self, filters: Optional[dict] = None) -> list[dict]:
        q = self._table("pipeline_leads").select("*")
        if filters:
            for k, v in filters.items():
                q = q.eq(k, v)
        return q.execute().data

    def advance_stage(self, lead_id: str, new_stage: str) -> dict:
        _validate_enum(new_stage, LEAD_STAGES, "stage")
        result = (
            self._table("pipeline_leads")
            .update({"stage": new_stage, "updated_at": _now_iso()})
            .eq("id", lead_id)
            .execute()
        )
        if not result.data:
            raise KeyError(f"Lead id={lead_id!r} not found")
        return result.data[0]

    # --- lead_artifacts ---

    def add_artifact(
        self,
        lead_id: str,
        artifact_type: str,
        draft: str,
        *,
        model: Optional[str] = None,
        prompt_version: Optional[str] = None,
        param_stamp: Optional[dict] = None,
    ) -> dict:
        row = _make_artifact_row(
            lead_id, artifact_type, draft,
            model=model,
            prompt_version=prompt_version,
            param_stamp=param_stamp,
        )
        result = self._table("lead_artifacts").insert(row).execute()
        stored = result.data[0]
        # Update current_artifacts cache on the lead
        lead = self.get_lead(id=lead_id)
        if lead:
            cache = lead.get("current_artifacts") or {}
            cache[artifact_type] = {
                "artifact_id": stored["id"],
                "draft": draft,
                "final": None,
                "drafted_at": stored["drafted_at"],
            }
            self._table("pipeline_leads").update({
                "current_artifacts": cache,
                "updated_at": _now_iso(),
            }).eq("id", lead_id).execute()
        return stored

    def set_artifact_final(
        self, artifact_id: str, final: str, *, diff: Optional[str] = None
    ) -> dict:
        artifact = self._table("lead_artifacts").select("*").eq("id", artifact_id).execute()
        if not artifact.data:
            raise KeyError(f"Artifact id={artifact_id!r} not found")
        row = artifact.data[0]
        computed_diff = diff or "\n".join(
            difflib.unified_diff(
                (row["draft"] or "").splitlines(),
                final.splitlines(),
                lineterm="",
                n=2,
            )
        )
        updated = (
            self._table("lead_artifacts")
            .update({"final": final, "diff": computed_diff, "edited_at": _now_iso()})
            .eq("id", artifact_id)
            .execute()
        )
        return updated.data[0]

    def list_artifacts(self, lead_id: str) -> list[dict]:
        return (
            self._table("lead_artifacts")
            .select("*")
            .eq("lead_id", lead_id)
            .order("drafted_at")
            .execute()
            .data
        )

    # --- lead_events ---

    def add_event(
        self,
        lead_id: str,
        kind: str,
        provider: Optional[str],
        provider_event_id: str,
        payload: Optional[dict] = None,
    ) -> Optional[dict]:
        _validate_enum(kind, EVENT_KINDS, "kind")
        _validate_enum(provider, PROVIDERS, "provider")
        row = _make_event_row(lead_id, kind, provider, provider_event_id, payload)
        try:
            result = self._table("lead_events").insert(row).execute()
            return result.data[0]
        except Exception as e:
            err_str = str(e)
            code = getattr(e, "code", None)
            if code == "23505" or "lead_events_provider_event_uniq" in err_str:
                log.info(
                    "duplicate event skipped: provider=%r provider_event_id=%r",
                    provider,
                    provider_event_id,
                )
                return None
            raise

    def list_events(self, filters: Optional[dict] = None) -> list[dict]:
        q = self._table("lead_events").select("*")
        if filters:
            for k, v in filters.items():
                q = q.eq(k, v)
        return q.execute().data

    def update_lead_fields(self, lead_id: str, **fields) -> dict:
        # Validate enums when the caller passes them
        if "stage" in fields:
            _validate_enum(fields["stage"], LEAD_STAGES, "stage")
        if "reply_class" in fields:
            _validate_enum(fields["reply_class"], REPLY_CLASSES, "reply_class")
        if "temperature" in fields:
            _validate_enum(fields["temperature"], TEMPERATURES, "temperature")

        fields["updated_at"] = _now_iso()
        result = (
            self._table("pipeline_leads")
            .update(fields)
            .eq("id", lead_id)
            .execute()
        )
        if not result.data:
            raise KeyError(f"Lead id={lead_id!r} not found")
        return result.data[0]


# ---------------------------------------------------------------------------
# FACTORY — the one-line swap point
# ---------------------------------------------------------------------------

def get_store(backend: Optional[str] = None) -> LeadStore:
    """
    Return a LeadStore instance.

    Backend selection (in order of precedence):
      1. backend arg passed directly
      2. STORE_BACKEND env var ("local" or "supabase")
      3. Default: "local"

    One-line swap: set STORE_BACKEND=supabase in the environment.
    """
    chosen = backend or os.environ.get("STORE_BACKEND", "local")
    if chosen == "supabase":
        return SupabaseBackend()
    return LocalJsonBackend()


# ---------------------------------------------------------------------------
# AUDIT LOADER — maps audit JSON records → pipeline_leads via LeadStore
# ---------------------------------------------------------------------------

# Wedge → pain_tag mapping (deterministic table from the brief)
WEDGE_TO_PAIN_TAG: dict[Optional[str], Optional[str]] = {
    "L0_candidate": "no_web_presence",
    "L2": "front_office_gap",
    "L3": "margin_blind",
    "off_icp": None,
    None: None,
}

# Fetch statuses that indicate the site couldn't be reached (not actionable)
FETCH_ERROR_PREFIXES = {"error", "dns_fail"}

# Wedges to skip loading as active leads
SKIP_WEDGES = {"off_icp"}


def _classify_stage_from_audit(record: dict) -> str:
    """Records with a wedge and leaks are 'audited'. Others are 'sourced'."""
    wedge = record.get("wedge")
    leaks = record.get("all_leaks") or []
    if wedge and wedge not in SKIP_WEDGES and leaks:
        return "audited"
    return "sourced"


def _should_suppress(record: dict) -> tuple[bool, Optional[str]]:
    """Determine if a record should be suppressed rather than loaded as active."""
    fetch_status = record.get("fetch_status", "")
    wedge = record.get("wedge")
    off_icp = record.get("off_icp_reason")

    if off_icp or wedge == "off_icp":
        return True, f"off_icp: {off_icp or 'wedge=off_icp'}"

    prefix = fetch_status.split(":")[0] if fetch_status else ""
    if prefix in FETCH_ERROR_PREFIXES:
        return True, f"fetch_error: {fetch_status}"

    return False, None


def _build_lead_record_from_audit(
    audit_rec: dict,
    identity_map: dict[str, dict],
    source_wave: str,
) -> dict:
    """
    Merge an audit record + optional identity fields from a source file
    into a pipeline_leads dict ready for upsert_lead().
    """
    place_id = audit_rec["place_id"]
    identity = identity_map.get(place_id, {})

    wedge = audit_rec.get("wedge")
    fetch_status = audit_rec.get("fetch_status", "")
    suppressed, suppression_reason = _should_suppress(audit_rec)

    # Build the audit jsonb blob
    all_leaks = audit_rec.get("all_leaks") or []
    audit_blob = {
        "ran_at": _now_iso(),
        "fetch_status": fetch_status,
        "tech_stack": audit_rec.get("tech_stack"),
        "wayback_date": audit_rec.get("wayback_date"),
        "leaks": [
            {
                "type": lk.get("signal"),
                "evidence": lk.get("evidence_token"),
                "leverage": lk.get("frame"),
                "detectable": "E",  # static-HTML-detectable = Edge; PASS-2 needed = Browser
                "chosen": lk is all_leaks[0] if all_leaks else False,
                "confidence": lk.get("confidence"),
                "scan_note": lk.get("scan_note"),
            }
            for lk in all_leaks
        ],
        "lead_with": audit_rec.get("leak_signal"),
        "opening_line": audit_rec.get("one_line_frame"),
        "evidence_token": audit_rec.get("evidence_token"),
        "unchosen_leaks": [
            lk.get("signal") for lk in all_leaks[1:]
        ],
    }

    # Identity fields — prefer audit record values, fall back to identity_map
    name = audit_rec.get("name") or identity.get("name", "")
    website = audit_rec.get("website") or identity.get("website")
    niche = identity.get("niche") or identity.get("L2_btype")

    # L2 enrichment fields (only in leads-master / leads-verified records)
    owner_name = identity.get("L2_owner")
    founded_year = identity.get("L2_founded")
    if isinstance(founded_year, str):
        try:
            founded_year = int(founded_year)
        except (ValueError, TypeError):
            founded_year = None

    stage = "suppressed" if suppressed else _classify_stage_from_audit(audit_rec)
    pain_tag = WEDGE_TO_PAIN_TAG.get(wedge) if not suppressed else None

    return _make_lead_row(
        place_id=place_id,
        name=name,
        website=website,
        niche=niche,
        city=identity.get("city"),
        region=identity.get("region"),
        addr=identity.get("addr"),
        lat=identity.get("lat"),
        lng=identity.get("lng"),
        phone=identity.get("phone"),
        owner_name=owner_name,
        founded_year=founded_year,
        web_presence=identity.get("web_presence"),
        confidence_tier=audit_rec.get("confidence"),
        evidence={"call_only_seeds": audit_rec.get("call_only_seeds", [])},
        pain_tag=pain_tag,
        temperature="cold",  # all outbound cold to start
        wedge=wedge,
        audit=audit_blob,
        stage=stage,
        suppressed=suppressed,
        suppression_reason=suppression_reason,
        source_wave=source_wave,
    )


def load_audit_files(
    audit_paths: list[str],
    store: LeadStore,
    *,
    master_path: Optional[str] = None,
    hassite_source_path: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Ingest one or more audit JSON files into pipeline_leads via the given store.

    identity_map is built from:
      - master_path (leads-master.json) → for leads already in master (full-612)
      - hassite_source_path (leads-hassite-batch200.json) → for newer wave leads

    Returns a summary dict with loaded/dedup/skipped counts and per-wedge breakdown.
    """
    from collections import Counter

    # Build identity lookup map
    identity_map: dict[str, dict] = {}

    if master_path and Path(master_path).exists():
        with open(master_path) as f:
            for row in json.load(f):
                identity_map[row["place_id"]] = row

    if hassite_source_path and Path(hassite_source_path).exists():
        with open(hassite_source_path) as f:
            for row in json.load(f):
                identity_map[row["place_id"]] = row

    total_input = 0
    loaded = 0
    dedup = 0
    suppressed_count = 0
    wedge_counts: Counter = Counter()
    errors: list[str] = []

    for audit_path in audit_paths:
        wave = Path(audit_path).stem  # e.g. "audit-hassite-batch200"
        with open(audit_path) as f:
            records = json.load(f)

        for rec in records:
            total_input += 1
            place_id = rec.get("place_id")
            if not place_id:
                errors.append(f"Missing place_id in {audit_path}: {rec.get('name')}")
                continue

            try:
                lead_row = _build_lead_record_from_audit(rec, identity_map, source_wave=wave)
            except Exception as e:
                errors.append(f"Build error for {place_id}: {e}")
                continue

            wedge_counts[lead_row["wedge"]] += 1

            if lead_row["suppressed"]:
                suppressed_count += 1

            if dry_run:
                loaded += 1
                continue

            # Check if already exists (idempotency)
            existing = store.get_lead(place_id=place_id)
            if existing:
                dedup += 1
                # Still upsert to update audit data if needed
                store.upsert_lead(lead_row)
            else:
                store.upsert_lead(lead_row)
                loaded += 1

    return {
        "total_input": total_input,
        "loaded": loaded,
        "dedup_updated": dedup,
        "suppressed": suppressed_count,
        "errors": len(errors),
        "error_details": errors[:10],  # cap at 10 for readability
        "wedge_breakdown": dict(wedge_counts),
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

DEFAULT_MASTER = "./data/leads-master.json"
DEFAULT_HASSITE = "./data/leads-hassite.json"
DEFAULT_AUDIT_FULL = "./data/audit-full.json"
DEFAULT_AUDIT_BATCH = "./data/audit-batch.json"


def main():
    parser = argparse.ArgumentParser(
        description="Lead pipeline persistence store CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # load-audit subcommand
    load_p = sub.add_parser("load-audit", help="Load audit JSON files into the store")
    load_p.add_argument("audit_files", nargs="*", help="Audit JSON files to load")
    load_p.add_argument("--master", default=DEFAULT_MASTER, help="leads-master.json path")
    load_p.add_argument("--hassite-source", default=DEFAULT_HASSITE, help="leads-hassite-batch200.json path")
    load_p.add_argument("--backend", choices=["local", "supabase"], default=None)
    load_p.add_argument("--dry-run", action="store_true")

    # list subcommand
    list_p = sub.add_parser("list", help="List leads with optional filters")
    list_p.add_argument("--stage", help="Filter by stage")
    list_p.add_argument("--wedge", help="Filter by wedge")
    list_p.add_argument("--backend", choices=["local", "supabase"], default=None)
    list_p.add_argument("--limit", type=int, default=10)

    # sample subcommand
    sample_p = sub.add_parser("sample", help="Print N sample records from the store")
    sample_p.add_argument("--n", type=int, default=2)
    sample_p.add_argument("--wedge", help="Filter by wedge")
    sample_p.add_argument("--backend", choices=["local", "supabase"], default=None)

    args = parser.parse_args()

    if args.command == "load-audit":
        audit_files = args.audit_files or [DEFAULT_AUDIT_FULL, DEFAULT_AUDIT_BATCH]
        store = get_store(backend=args.backend)
        summary = load_audit_files(
            audit_files,
            store,
            master_path=args.master,
            hassite_source_path=args.hassite_source,
            dry_run=args.dry_run,
        )
        print("=== Loader Summary ===")
        print(f"Total input records : {summary['total_input']}")
        print(f"New rows loaded     : {summary['loaded']}")
        print(f"Dedup (upserted)    : {summary['dedup_updated']}")
        print(f"Suppressed          : {summary['suppressed']}")
        print(f"Errors              : {summary['errors']}")
        print(f"Dry run             : {summary['dry_run']}")
        print()
        print("Wedge breakdown:")
        for wedge, count in sorted(summary["wedge_breakdown"].items(), key=lambda x: -(x[1])):
            print(f"  {wedge or 'None (fetch-error)':<20} {count}")
        if summary["error_details"]:
            print()
            print("Error details (first 10):")
            for e in summary["error_details"]:
                print(f"  - {e}")

    elif args.command == "list":
        store = get_store(backend=args.backend)
        filters = {}
        if args.stage:
            filters["stage"] = args.stage
        if args.wedge:
            filters["wedge"] = args.wedge
        leads = store.list_leads(filters or None)
        print(f"Found {len(leads)} leads (showing {min(args.limit, len(leads))})")
        for row in leads[: args.limit]:
            print(f"  {row['place_id']} | {row['name']:<30} | wedge={row['wedge']} | stage={row['stage']}")

    elif args.command == "sample":
        store = get_store(backend=args.backend)
        filters = {}
        if args.wedge:
            filters["wedge"] = args.wedge
        leads = store.list_leads(filters or None)
        sample = leads[: args.n]
        for row in sample:
            print(json.dumps(row, indent=2, ensure_ascii=False))
            print("---")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
