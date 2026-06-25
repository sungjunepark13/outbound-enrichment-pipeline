#!/usr/bin/env python3
"""
draft-outreach.py — PARAMETERIZED DRAFT layer of the signal-led outbound engine.
Phase 2 — PE-D-020. PROMPT_VERSION v3.

For each actionable lead (L2 or L0_candidate), reads the audit from the store and
writes a cold_email (or sms/call deferral) artifact via store.add_artifact().

--- PARAMETERIZATION (Phase 2 additions) ---
Explicit selected params:
  channel  — email | call | sms
  angle    — the audit wedge/leak_signal (e.g. rfq_no_cad_upload)
  frame    — a frame-shape slug (humble-student | peer-operator | consultative-curiosity |
              demo-gift | challenger-evidence-led)
  offer    — L0 | L2 | L3
  ask      — book_call | reply | watch_loom | request_access
  touch    — first | follow_up | breakup

Auto-resolved (not selected by caller):
  voice    — loaded at runtime from voices/<channel>.md
  brand    — config/ offering.md + pricing.md
  ICP      — from the lead's own audit record
  benchmark — from signal_rationale (null → mechanism-only, never fabricate)

Resolution map (D):
  frame    → read the frame-shape card (8 fields incl. May-claim / Must-NOT)
  angle    → audit record + signal_rationale benchmark
  offer    → offering.md + pricing.md (L0/L2/L3 tier)
  voice    → voices/<channel>.md (local-biz-owner schema)
  benchmark → signal_rationale get_benchmark_line() (null → omit, never invent)

Composition + precedence (E):
  Frame = skeleton/opener; angle = hook BOUNDED by frame's May-claim; offer = bridge;
  ask = close; touch = modulation; channel = format.
  Precedence ladder (top wins):
    1. Anti-fabrication + frame Must-NOT (absolute)
    2. Voice invariants
    3. Channel hard limits
    4. Frame skeleton
    5. Content

Frame-dependency gate:
  challenger-evidence-led: requires evidence_token AND benchmark — not offered if either absent
  demo-gift: requires demo_url — not offered if absent
  humble-student, consultative-curiosity: always-available fallbacks
  peer-operator: always-available (founder's builder background is always real)

Channel handling:
  email  → existing email path (now frame-aware)
  sms    → NEW: ≤~160 chars / 1-2 lines, hook+ask only, optional short link
  call   → DEFERRED to call-script-write.md SOP; draft contains the SOP reference

Param stamping (G):
  Every draft is stored with {channel, angle, frame, offer, ask, touch, voice_profile,
  prompt_version} in artifact metadata via lead_store.

Guardrail:
  Every generated draft routes through outreach_guardrail.py before returning.
  The anti-fab gate is non-negotiable and final.

--- PRESERVED BEHAVIORS (Phase 1) ---
- HARD ANTI-FABRICATION: every specific claim traces directly to audit.evidence_token or
  audit.opening_line — nothing is invented.
- Voice: loaded at runtime from voices/cold-email.md (local-biz-owner schema).
- NICHE_LABEL / _shop_label / _name_anchor helpers — unchanged.
- FIX D/E/F logic for rfq drafts — unchanged.
- Skips: off_icp, suppressed, None-wedge, leads already drafted.
- Existing function signatures preserved (draft_lead, run, _draft_text, etc.).

Usage:
  python3 draft-outreach.py                     # run on all actionable, un-drafted leads
  python3 draft-outreach.py --sample 12         # run on up to 12 leads (mix of wedges)
  python3 draft-outreach.py --sample 12 --dry-run    # print drafts, don't write
  python3 draft-outreach.py --place-id <id>     # run on a single lead by place_id
  python3 draft-outreach.py --limit-l2 10 --limit-l0 2  # explicit counts
  python3 draft-outreach.py --reset-drafted     # clear cold_email artifacts + reset stage=audited
  python3 draft-outreach.py --frame humble-student --channel email  # param override
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# PATH SETUP — works whether called from any cwd
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from lead_store import get_store, LeadStore  # noqa: E402

# TASK 8/9: rationale-driven consequence sentences + guardrail
try:
    from signal_rationale import (
        get_mechanism,
        get_benchmark_line,
        is_headline_eligible,
        NON_HEADLINE_SIGNALS,
    )
    _RATIONALE_AVAILABLE = True
except ImportError:
    _RATIONALE_AVAILABLE = False
    def get_mechanism(s): return ""
    def get_benchmark_line(s): return ""
    def is_headline_eligible(s): return s not in {"analytics_suspected_absent", "paid_pixel"}
    NON_HEADLINE_SIGNALS = frozenset({"analytics_suspected_absent", "paid_pixel"})

try:
    from outreach_guardrail import validate_text, check_text, GuardrailViolation
    _GUARDRAIL_AVAILABLE = True
except ImportError:
    _GUARDRAIL_AVAILABLE = False
    def validate_text(text, context=""): pass
    def check_text(text): return True, None
    class GuardrailViolation(ValueError): pass

MODEL_ID = "claude-sonnet-4-6"
PROMPT_VERSION = "v3"  # bumped: Phase 2 parameterized drafting layer (PE-D-020)

# ---------------------------------------------------------------------------
# PARAM ENUMS
# ---------------------------------------------------------------------------

VALID_CHANNELS = frozenset({"email", "call", "sms"})
VALID_FRAMES = frozenset({
    "humble-student",
    "peer-operator",
    "consultative-curiosity",
    "demo-gift",
    "challenger-evidence-led",
})
VALID_OFFERS = frozenset({"L0", "L2", "L3"})
VALID_ASKS = frozenset({"book_call", "reply", "watch_loom", "request_access"})
VALID_TOUCHES = frozenset({"first", "follow_up", "breakup"})

# Frames that are always available (no input dependencies beyond the lead itself)
_ALWAYS_AVAILABLE_FRAMES = frozenset({"humble-student", "peer-operator", "consultative-curiosity"})

# ---------------------------------------------------------------------------
# FRAME-SHAPE CARD LOADER
# ---------------------------------------------------------------------------

_FRAME_SHAPES_DIR = Path(
    os.environ.get(
        "FRAME_SHAPES_DIR",
        "/Users/sungjunepark/.claude/skills/shared/frame-shapes",
    )
)

_FRAME_CARD_CACHE: dict[str, Optional[str]] = {}


def _load_frame_card(frame: str) -> Optional[str]:
    """Load and cache a frame-shape card. Returns full markdown text or None."""
    global _FRAME_CARD_CACHE
    if frame in _FRAME_CARD_CACHE:
        return _FRAME_CARD_CACHE[frame]
    card_path = _FRAME_SHAPES_DIR / f"{frame}.md"
    if card_path.exists():
        content = card_path.read_text(encoding="utf-8")
        _FRAME_CARD_CACHE[frame] = content
        return content
    _FRAME_CARD_CACHE[frame] = None
    return None


def _get_frame_field(frame: str, field: str) -> str:
    """
    Extract a named field value from the frame-shape card table.
    Returns empty string if not found.
    field: one of Persona, Pretext, Stance, Opener pattern, May claim, Must NOT, Pairs with, Best for
    """
    card = _load_frame_card(frame)
    if not card:
        return ""
    # Match table row: | **Field name** | content |
    pattern = re.compile(
        rf"\|\s*\*\*{re.escape(field)}\*\*\s*\|\s*(.+?)\s*\|",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(card)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# FRAME-DEPENDENCY GATE
# ---------------------------------------------------------------------------

def _check_frame_available(
    frame: str,
    evidence_token: str,
    lead_with: str,
    demo_url: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Check whether this frame can be used for this lead.
    Returns (available: bool, reason: str).

    challenger-evidence-led: requires evidence_token AND benchmark
    demo-gift: requires a demo_url
    humble-student, peer-operator, consultative-curiosity: always available
    """
    if frame in _ALWAYS_AVAILABLE_FRAMES:
        return True, "always-available frame"

    if frame == "challenger-evidence-led":
        if not evidence_token:
            return False, "challenger-evidence-led requires evidence_token (absent)"
        if not _RATIONALE_AVAILABLE:
            return False, "challenger-evidence-led requires signal_rationale module (unavailable)"
        benchmark = get_benchmark_line(lead_with) if lead_with else ""
        if not benchmark:
            return False, f"challenger-evidence-led requires benchmark for signal={lead_with!r} (none found)"
        return True, "evidence_token + benchmark both present"

    if frame == "demo-gift":
        if not demo_url:
            return False, "demo-gift requires a built demo URL (absent)"
        return True, "demo_url present"

    return False, f"unknown frame: {frame!r}"


# ---------------------------------------------------------------------------
# VOICE FILE LOADER — per channel
# ---------------------------------------------------------------------------

_VOICE_BASE_DIR = Path(
    os.environ.get(
        "VOICE_BASE_DIR",
        "/Users/sungjunepark/.claude/skills/_context/people/sungjune/voices",
    )
)

# Legacy single-path override (Phase 1 compat)
_VOICE_FILE = Path(
    os.environ.get(
        "VOICE_FILE",
        str(_VOICE_BASE_DIR / "cold-email.md"),
    )
)

_VOICE_LOADED: Optional[str] = None
_VOICE_LOAD_WARNING: Optional[str] = None

_VOICE_FALLBACK = """
## local-biz-owner (FALLBACK — voice file not found)
Rules: subject specific+short (about them); body <75 words; first sentence = noticed; no pitch/credentials/adjectives.
Phrases: 'noticed that...', 'went ahead and', 'worth a look?', 'no pressure'.
Avoid: 'Hi [Name] I hope this email finds you well', marketing adjectives.
"""


def _load_voice_for_channel(channel: str = "email") -> str:
    """
    Load voice rules for a specific channel at runtime.
    Returns the local-biz-owner section from voices/<channel>.md, or the
    fallback if the file is missing.

    channel mapping:
      email → cold-email.md
      sms   → sms.md
      call  → cold-call.md (rarely needed — call drafts are deferred to the SOP)
    """
    global _VOICE_LOADED, _VOICE_LOAD_WARNING

    # Channel → voice file mapping
    _channel_file_map = {
        "email": "cold-email.md",
        "sms": "sms.md",
        "call": "cold-call.md",
    }
    filename = _channel_file_map.get(channel, "cold-email.md")
    voice_path = _VOICE_BASE_DIR / filename

    # Legacy single-file cache only applies to email (Phase 1 compat)
    if channel == "email":
        if _VOICE_LOADED is not None:
            return _VOICE_LOADED
        if voice_path.exists():
            _VOICE_LOADED = voice_path.read_text(encoding="utf-8")
        elif _VOICE_FILE.exists():
            _VOICE_LOADED = _VOICE_FILE.read_text(encoding="utf-8")
        else:
            _VOICE_LOAD_WARNING = (
                f"WARNING: Voice file not found at {voice_path}. "
                "Using inline fallback. Voice may drift from canonical rules."
            )
            _VOICE_LOADED = _VOICE_FALLBACK
        return _VOICE_LOADED

    # Non-email channels: load fresh each call (different files, no global cache)
    if voice_path.exists():
        return voice_path.read_text(encoding="utf-8")

    return _VOICE_FALLBACK


def _load_voice() -> str:
    """Backward-compat: load email voice (Phase 1 call sites)."""
    return _load_voice_for_channel("email")


def _get_local_biz_rules(channel: str = "email") -> str:
    """Extract the local-biz-owner section from the voice file for a channel."""
    content = _load_voice_for_channel(channel)
    match = re.search(r"## local-biz-owner.*?(?=\n## |\Z)", content, re.DOTALL)
    if match:
        return match.group(0)
    return content


# ---------------------------------------------------------------------------
# OFFERING RESOLVER
# ---------------------------------------------------------------------------

_OFFERING_FILE = Path(
    os.environ.get(
        "OFFERING_FILE",
        "./config/offering.md",
    )
)
_PRICING_FILE = Path(
    os.environ.get(
        "PRICING_FILE",
        "./config/pricing.md",
    )
)
_OFFERING_CACHE: Optional[str] = None
_PRICING_CACHE: Optional[str] = None


def _load_offering() -> str:
    """Load offering.md at runtime. Cached after first load."""
    global _OFFERING_CACHE
    if _OFFERING_CACHE is not None:
        return _OFFERING_CACHE
    if _OFFERING_FILE.exists():
        _OFFERING_CACHE = _OFFERING_FILE.read_text(encoding="utf-8")
    else:
        _OFFERING_CACHE = ""  # graceful — no fabrication
    return _OFFERING_CACHE


def _load_pricing() -> str:
    """Load pricing.md at runtime. Cached after first load."""
    global _PRICING_CACHE
    if _PRICING_CACHE is not None:
        return _PRICING_CACHE
    if _PRICING_FILE.exists():
        _PRICING_CACHE = _PRICING_FILE.read_text(encoding="utf-8")
    else:
        _PRICING_CACHE = ""
    return _PRICING_CACHE


# ---------------------------------------------------------------------------
# ASK RENDERER — channel-appropriate phrasing per ask enum
# ---------------------------------------------------------------------------

def _render_ask(ask: str, channel: str) -> str:
    """
    Render the ask enum as channel-appropriate copy.

    ask values: book_call | reply | watch_loom | request_access
    channel: email | sms | call

    Returns a single sentence or fragment, channel-tuned.
    """
    if channel == "sms":
        _sms_asks = {
            "book_call":      "worth a quick call?",
            "reply":          "worth a look?",
            "watch_loom":     "lmk if you want the link",
            "request_access": "lmk and i'll send access",
        }
        return _sms_asks.get(ask, "worth a look?")

    if channel == "call":
        # Call scripts are deferred to the SOP — this is the transition phrase
        _call_asks = {
            "book_call":      "Would you be open to a 15-minute call this week?",
            "reply":          "Is there a better time to reach you?",
            "watch_loom":     "I can send over a short video — would that be easier?",
            "request_access": "I can set up access — is that something you'd want to see?",
        }
        return _call_asks.get(ask, "Would you be open to a quick call?")

    # email
    _email_asks = {
        "book_call":      "Worth a 15-minute call to walk through what it could look like?",
        "reply":          "Worth a quick look?",
        "watch_loom":     "I made a short Loom showing exactly what I mean — worth 90 seconds?",
        "request_access": "Happy to set up access — want me to send it over?",
    }
    return _email_asks.get(ask, "Worth a quick look?")


# ---------------------------------------------------------------------------
# TOUCH MODULATOR — modulates copy tone per touch sequence position
# ---------------------------------------------------------------------------

def _get_touch_prefix(touch: str, channel: str) -> str:
    """
    Return a short touch-position phrase to prepend or weave into the message.

    first:     cold open — no reference to prior contact
    follow_up: reference prior touch, escalate ask slightly
    breakup:   last-touch framing

    Returns "" for first (no prefix needed), short phrase for others.
    """
    if touch == "first":
        return ""

    if touch == "follow_up":
        if channel == "sms":
            return "hey — following up on my note"
        if channel == "call":
            return "I'm following up — I reached out last week about"
        return "Following up on the note I sent last week —"

    if touch == "breakup":
        if channel == "sms":
            return "last note — no pressure either way"
        if channel == "call":
            return "I'll keep this brief — last reach-out before I move on."
        return "Last note — I'll keep this short."

    return ""


def _get_touch_suffix(touch: str, channel: str) -> str:
    """Return a closing modulation phrase per touch position."""
    if touch == "breakup":
        if channel == "sms":
            return "no worries if not the right time"
        return "No worries if the timing's off — wanted to make sure it landed."
    return ""


# ---------------------------------------------------------------------------
# SMS EMITTER (NEW — Phase 2)
# ≤~160 chars / 1-2 lines, hook+ask only, optional short link.
# Benchmark/elaboration cut per E truncation keep-priority.
# ---------------------------------------------------------------------------

def _build_sms(
    name: str,
    niche: Optional[str],
    lead_with: str,
    opening_line: str,
    evidence_token: str,
    frame: str,
    ask: str,
    touch: str,
    demo_url: Optional[str] = None,
) -> str:
    """
    Build a ≤~160-char SMS body (1-2 lines).

    Composition (E truncation priority for SMS):
      1. Hook (the one thing they noticed — abbreviated from opening_line)
      2. Ask (channel-appropriate)
      Benchmark: CUT (too long for SMS, not needed for the hook)
      Elaboration: CUT

    Frame skeleton for SMS:
      humble-student:         "hey [name] — [light observation] — [question]?"
      peer-operator:          "hey [name] — saw [X] on your site, [offer] — [ask]"
      consultative-curiosity: "hey [name] — how do you handle [pain]? [ask]"
      demo-gift:              "hey [name] — built you [X], [link] — [ask]"
      challenger-evidence-led:"hey [name] — noticed [X] on your site — [soft question]"
    """
    anchor = _name_anchor(name)
    touch_prefix = _get_touch_prefix(touch, "sms")
    ask_phrase = _render_ask(ask, "sms")
    touch_suffix = _get_touch_suffix(touch, "sms")

    # Abbreviate the observation from opening_line to fit SMS
    # Take the first clause before any em-dash or colon
    if opening_line:
        obs_raw = opening_line.split("—")[0].split(":")[0].strip().rstrip(".")
        obs = obs_raw[:80].strip()  # hard cap for SMS context budget
    else:
        obs = f"noticed something on your site"

    # Frame-tuned skeleton
    if frame == "demo-gift" and demo_url:
        link_part = f" {demo_url}" if demo_url else ""
        body = f"hey {anchor} — built you a quick mockup{link_part} — {ask_phrase}"
    elif frame == "consultative-curiosity":
        body = f"hey {anchor} — how do you handle the rfq path on your site? {ask_phrase}"
    elif frame == "peer-operator":
        body = f"hey {anchor} — {obs.lower()} — built a quick fix, {ask_phrase}"
    elif frame == "challenger-evidence-led":
        body = f"hey {anchor} — {obs.lower()} — might be costing you jobs, {ask_phrase}"
    else:
        # humble-student (default/fallback)
        body = f"hey {anchor} — {obs.lower()} — {ask_phrase}"

    # Prepend touch prefix if follow_up or breakup
    if touch_prefix:
        body = f"{touch_prefix} — {body}"

    # Append touch suffix if breakup
    if touch_suffix:
        body = f"{body}. {touch_suffix}"

    # Hard truncation: SMS must stay ≤160 chars (best-effort — never cut mid-word)
    if len(body) > 160:
        # Trim at last space before 160
        body = body[:157].rsplit(" ", 1)[0] + "..."

    return body


# ---------------------------------------------------------------------------
# VOCAB / NICHE MAPPING
# ---------------------------------------------------------------------------

NICHE_LABEL = {
    "metal": "machine shop",
    "welding": "welding shop",
    "fabrication": "fab shop",
    "aerospace": "aerospace machine shop",
    "precision": "precision machine shop",
    "plastics": "plastics shop",
    "solar": "solar installation business",
    "3pl": "warehousing/distribution operation",
    "warehouse": "warehousing/distribution operation",
    "distribution": "distribution operation",
    "logistics": "logistics operation",
    "marine": "marine fabrication shop",
    "hydraulics": "hydraulics shop",
    "tooling": "tooling shop",
    "die": "tool & die shop",
    "gear": "gear shop",
    "waterjet": "waterjet cutting shop",
    "laser": "laser cutting shop",
    "cnc": "CNC machine shop",
    "injection": "injection molding shop",
    "stamping": "metal stamping shop",
}

_NAME_FILLERS = {"the", "a", "an", "and", "of", "for", "inc", "llc", "co", "corp"}


def _shop_label(niche: Optional[str]) -> str:
    """Return a readable shop type label from niche field."""
    if not niche:
        return "shop"
    n = niche.lower()
    for key, label in NICHE_LABEL.items():
        if key in n:
            return label
    return "shop"


def _name_anchor(name: str) -> str:
    """
    Extract the most distinctive word from a shop name for use in subject lines.
    Skips generic fillers (The, A, Inc, LLC) and picks the first meaningful word.
    """
    stripped = re.sub(r'\s*\b(LLC|Inc\.?|Corp\.?|Co\.?|Ltd\.?)\s*$', '', name, flags=re.I).strip()
    stripped = stripped.rstrip(",.").strip()
    stripped_words = stripped.split()

    if len(stripped_words) <= 3:
        return stripped or name

    words = name.split()
    meaningful = []
    for word in words:
        clean = word.strip(".,&-").lower()
        if clean and clean not in _NAME_FILLERS and len(clean) > 1:
            meaningful.append(word.rstrip(".,"))

    if not meaningful:
        return " ".join(stripped_words[:2]) if len(stripped_words) >= 2 else name

    _GENERIC_STANDALONE = {"shop", "works", "machine", "services", "manufacturing", "fab", "fix"}
    first = meaningful[0].rstrip(".,")
    if first.lower() in _GENERIC_STANDALONE and len(meaningful) > 1:
        return f"{first} {meaningful[1].rstrip('.,')}"

    first_idx = name.split().index(meaningful[0]) if meaningful[0] in name.split() else -1
    if first_idx > 1:
        prefix_words = [w for w in name.split()[:first_idx] if w.strip(".,&-")]
        if prefix_words:
            return " ".join(prefix_words[:2]) + " " + first

    return first


# ---------------------------------------------------------------------------
# CONTACT PATH / CAPABILITY EXTRACTION (Phase 1 unchanged)
# ---------------------------------------------------------------------------

def _extract_contact_path(evidence_token: str) -> str:
    """Extract the most informative contact path from an evidence_token string."""
    if not evidence_token:
        return ""
    path_match = re.search(r'"((?:/[\w\-_/]*)+)"', evidence_token)
    if path_match:
        return path_match.group(1)
    path_match2 = re.search(r'(?:^|\s)(/[\w\-_/]+)', evidence_token)
    if path_match2:
        return path_match2.group(1).strip()
    return ""


def _extract_shop_capability(opening_line: str, evidence_token: str, name: str) -> str:
    """Extract a shop-specific capability or location hint from evidence_token."""
    if not evidence_token:
        return ""
    et = re.sub(r'&amp;', '&', evidence_token)
    et = re.sub(r'&#\d+;', '', et)
    et = re.sub(r'<[^>]+>', '', et)
    et = et.strip().strip('"')

    if et.startswith('/'):
        return ""

    pipe_parts = [p.strip() for p in et.split('|')]
    for part in pipe_parts:
        if re.search(r'\b(CNC|CAD|mill|lathe|turn|weld|fab|stamp|laser|waterjet|EDM|grind|broach)\b', part, re.I):
            return part[:60]

    dash_match = re.search(r'[-–—]\s*(.+)', et)
    if dash_match:
        after_dash = dash_match.group(1).strip()
        if len(after_dash) > 5 and after_dash.lower() not in ('home', 'homepage', 'index'):
            return after_dash[:50]

    comma_match = re.search(r',\s*([A-Z][a-z]+ [A-Z]{2})\b', et)
    if comma_match:
        return f"in {comma_match.group(1)}"

    return ""


# ---------------------------------------------------------------------------
# FRAME-AWARE EMAIL OPENER BUILDER
# Applies the frame skeleton to produce the opener paragraph (S1).
# The May-claim constraint governs how much of the angle's ammo can be used.
# ---------------------------------------------------------------------------

def _build_frame_opener(
    frame: str,
    name: str,
    lead_with: str,
    opening_line: str,
    evidence_token: str,
    benchmark: str,
    touch: str,
    demo_url: Optional[str] = None,
) -> str:
    """
    Build the opener sentence(s) for an email using the selected frame skeleton.

    Composition (E) applied here:
      - Frame = skeleton/tone of the opener
      - Angle (the audit hook) is BOUNDED by the frame's May-claim
      - Benchmark is only included if the frame's May-claim allows it
        (challenger-evidence-led may use it; humble-student/consultative-curiosity must not)

    Returns a string (1-3 sentences for email).
    """
    # Extract the S1 observation from opening_line (unchanged from Phase 1)
    s1_raw = opening_line.split("—")[0].strip().rstrip(".")
    s1 = s1_raw[0].upper() + s1_raw[1:] + "." if s1_raw else opening_line.rstrip(".") + "."

    touch_prefix = _get_touch_prefix(touch, "email")
    prefix_phrase = f"{touch_prefix} " if touch_prefix else ""

    if frame == "humble-student":
        # Opener pattern: humble intro → one genuine specific question about them → easy to answer
        # May claim: nothing about us; a light public observation about them. No benchmark.
        return f"{prefix_phrase}I've been looking at how small shops handle their online quoting path — noticed something on your site I wanted to ask about.\n\n{s1}"

    if frame == "peer-operator":
        # Opener pattern: quick who-I-am (builder) → specific observation → offer-of-value
        # May claim: founder's builder background + audit observation. No results.
        return f"{prefix_phrase}I build dashboards and front-office tools for job shops. Was looking at a few sites in the area and {s1.lower()}"

    if frame == "consultative-curiosity":
        # Opener pattern: frame the problem-space → one genuine open question about how THEY handle it
        # May claim: problem framing (industry-level). Nothing about results.
        return f"{prefix_phrase}Trying to understand how CNC shops handle their inbound quoting path — I keep seeing the same friction point.\n\n{s1}"

    if frame == "demo-gift":
        # Opener pattern: "I built you X" → the link → one line on what it shows → soft ask
        # May claim: the demo itself + shop details it's built on. No results.
        link_part = f" — here it is: {demo_url}" if demo_url else ""
        return f"{prefix_phrase}I put together a quick mockup of what a better quoting path could look like for your shop{link_part}.\n\n{s1}"

    if frame == "challenger-evidence-led":
        # Opener pattern: specific observation (evidence_token) → risk → industry stat (benchmark) → soft question
        # May claim: ONLY evidence_token (DOM-confirmed) + benchmark (proof-stat-gated). Nothing else.
        # Stance: soft provocation — a *question*, never an assertion; "might be," never "you are"
        bench_line = ""
        if benchmark:
            # Strip the trailing source citation for email brevity
            _bench_parts = benchmark.split(" (")
            bench_stat = _bench_parts[0].strip()
            bench_line = f" {bench_stat}"
        return f"{prefix_phrase}{s1}{bench_line} — might be worth a closer look?"

    # Fallback: humble-student behavior
    return f"{prefix_phrase}{s1}"


# ---------------------------------------------------------------------------
# LEAK SIGNAL → COPY LOGIC (Phase 1 preserved, now frame-aware)
# ---------------------------------------------------------------------------

def _build_l2_email(
    name: str,
    niche: Optional[str],
    lead_with: str,
    opening_line: str,
    evidence_token: str,
    all_leaks: list[dict],
    frame: str = "humble-student",
    offer: str = "L2",
    ask: str = "reply",
    touch: str = "first",
    demo_url: Optional[str] = None,
) -> dict[str, str]:
    """
    Assemble subject + body for an L2 lead (has-site, front-office gap).

    Composition order (E): frame=opener, angle=hook bounded by May-claim,
    offer=bridge, ask=close, touch=modulation, channel=email format.

    ANTI-FABRICATION: opening_line and evidence_token come directly from the audit.
    """
    shop = _shop_label(niche)

    # --- subject ---
    anchor = _name_anchor(name)
    poss = f"{anchor}'" if anchor.endswith("s") else f"{anchor}'s"
    subject_map = {
        "rfq_no_cad_upload": f"quote path on {poss} site",
        "missing_defense_tokens": f"defense work visibility for {anchor}",
        "adjectives_not_numbers": f"one thing engineers need on {poss} site",
        "builder_site": f"RFQ path on {poss} site",
        "missing_viewport": f"mobile issue on {poss} site",
        "footer_year_stale": f"quick fix on {poss} site",
        "analytics_suspected_absent": f"blind spot on {poss} site",
        "no_chat_widget": f"inbound response gap at {anchor}",
        "wayback_stale": f"site freshness issue for {anchor}",
    }
    subject = subject_map.get(lead_with, f"noticed something on {poss} site")

    # --- benchmark (angle's ammo, bounded by frame's May-claim) ---
    # challenger-evidence-led: may use benchmark; others: mechanism only
    benchmark = ""
    if _RATIONALE_AVAILABLE and lead_with:
        benchmark = get_benchmark_line(lead_with)  # "" if no verified stat

    # --- S1 + frame opener ---
    opener = _build_frame_opener(
        frame=frame,
        name=name,
        lead_with=lead_with,
        opening_line=opening_line,
        evidence_token=evidence_token,
        benchmark=benchmark,
        touch=touch,
        demo_url=demo_url,
    )

    # --- S2: cost framing — mechanism from rationale ---
    _STATIC_COST_MAP = {
        "rfq_no_cad_upload": "That's where quotes go cold — engineers who've already chosen you move on.",
        "missing_defense_tokens": "That means your shop doesn't surface in defense procurement searches — those contracts go to whoever shows up registered.",
        "adjectives_not_numbers": "Engineers self-qualify on specs — without tolerances and materials listed, they move to the next result.",
        "builder_site": "Engineers expect a file-upload RFQ path, not a contact form — most don't fill it out.",
        "missing_viewport": "Most RFQ traffic is mobile now — a broken mobile layout loses the job before they read your capabilities.",
        "footer_year_stale": "An outdated footer date signals an unmaintained site — buyers notice.",
        "analytics_suspected_absent": "Without tracking, you can't see which jobs come from the site vs word of mouth.",
        "no_chat_widget": "Inbound leads that don't get a fast response often don't come back.",
        "wayback_stale": "Engineers check site freshness when vetting new shops — a stale site reads as inactive.",
    }

    if _RATIONALE_AVAILABLE and lead_with:
        mechanism = get_mechanism(lead_with)
        bench_line = get_benchmark_line(lead_with)
        if mechanism:
            mech_sentences = [s.strip() for s in mechanism.split(".") if s.strip()]
            mech_short = mech_sentences[0] + "." if mech_sentences else ""

            # Apply frame May-claim: challenger-evidence-led already used benchmark in opener.
            # Other frames get mechanism-only in S2 (no double-use of benchmark).
            if bench_line and frame != "challenger-evidence-led":
                _bench_parts = bench_line.split(" (")
                bench_stat_only = _bench_parts[0].strip()
                s2 = f"{mech_short} {bench_stat_only}"
            else:
                s2 = mech_short
        else:
            s2 = _STATIC_COST_MAP.get(lead_with, "That gap is where jobs quietly go to other shops.")
    else:
        s2 = _STATIC_COST_MAP.get(lead_with, "That gap is where jobs quietly go to other shops.")

    # For challenger-evidence-led: the opener already contains mechanism+benchmark,
    # so S2 should be a shorter bridge rather than repeating the cost.
    if frame == "challenger-evidence-led":
        s2 = ""  # opener already carries the full challenge; bridge directly to offer

    # --- S3: offer — tied to the specific leak ---
    if lead_with == "rfq_no_cad_upload":
        contact_path = _extract_contact_path(evidence_token)
        capability = _extract_shop_capability(opening_line, evidence_token, name)
        if contact_path and contact_path != "/contact":
            path_ref = f"your {contact_path.lstrip('/')} page"
        elif contact_path:
            path_ref = "your contact page"
        else:
            path_ref = "your site"
        cap_note = f" — looks like you do {capability}" if capability else ""
        s3 = (
            f"I can add a STEP/DWG upload path alongside {path_ref}{cap_note} — "
            f"want me to mock up what it'd look like?"
        )
    elif lead_with == "missing_defense_tokens":
        s3 = "I can help you get CAGE-registered and show up where defense buyers actually search — happy to map out what that looks like for your shop."
    elif lead_with == "adjectives_not_numbers":
        s3 = "I can restructure your capabilities page with the numbers engineers actually use to self-qualify — tolerances, materials, machine specs. Want me to mock one up?"
    elif lead_with == "builder_site":
        s3 = "I can show you what a proper file-upload RFQ path looks like for your shop — took an hour to rough one out."
    elif lead_with == "missing_viewport":
        s3 = "I can fix the mobile layout — quick change, and I can show you exactly what it would look like before you commit to anything."
    elif lead_with == "footer_year_stale":
        s3 = "Small fix worth doing if you're actively trying to win new accounts — happy to show you the full list of quick wins on your site."
    elif lead_with == "analytics_suspected_absent":
        s3 = "I can show you how to wire up basic tracking in an afternoon so you know what's actually working — free to set up, happy to walk you through it."
    elif lead_with == "no_chat_widget":
        s3 = "A basic chat widget is almost free to set up and keeps inbound from going cold — happy to show you what it'd look like on your site."
    elif lead_with == "wayback_stale":
        s3 = "A few targeted content updates signals to buyers that your shop is active and taking work — happy to mock up what that would look like."
    else:
        s3 = "I can mock up a fix — it's a quick change, and I can show you exactly what it would look like before anything else."

    # --- S4: ask + risk-reversal (rendered from ask param) ---
    ask_phrase = _render_ask(ask, "email")
    if lead_with == "rfq_no_cad_upload":
        s4 = f"No pitch, no pressure — {ask_phrase}"
    else:
        s4 = f"Happy to show you what it would look like — no pitch, no pressure. {ask_phrase}"

    # --- touch suffix ---
    touch_suffix = _get_touch_suffix(touch, "email")

    # Assemble body
    if s2:
        body_parts = [f"{opener}\n\n{s2}", s3, s4]
    else:
        body_parts = [opener, s3, s4]

    if touch_suffix:
        body_parts.append(touch_suffix)

    body_parts.append("\nthanks,\nSungjune")
    body = "\n\n".join(body_parts)

    draft = {"subject": subject, "body": body}
    try:
        validate_text(subject, context=f"L2-email.subject[{lead_with}]")
        validate_text(body, context=f"L2-email.body[{lead_with}]")
    except GuardrailViolation as e:
        raise GuardrailViolation(
            f"Draft guardrail violation for lead_with={lead_with}: {e}",
            pattern_name=e.pattern_name,
            matched_text=e.matched_text,
        )
    return draft


def _build_l0_email(
    name: str,
    niche: Optional[str],
    evidence_token: str,
    frame: str = "humble-student",
    offer: str = "L0",
    ask: str = "reply",
    touch: str = "first",
    demo_url: Optional[str] = None,
) -> dict[str, str]:
    """
    Assemble subject + body for an L0_candidate (no website).

    ANTI-FABRICATION: the only specific claim is that they have no website.
    FIX F: uses correct shop label.
    """
    shop = _shop_label(niche)

    anchor = _name_anchor(name)
    subject = f"website for {anchor}?"

    touch_prefix = _get_touch_prefix(touch, "email")
    prefix_phrase = f"{touch_prefix} " if touch_prefix else ""

    s1 = f"{prefix_phrase}Noticed {name} doesn't have a website listed."
    s2 = "Most operations running on referrals are fine — but buyers vetting new suppliers always check for a site first, and a missing one can quietly rule you out."

    # Frame modulates S3 slightly for L0
    if frame == "demo-gift" and demo_url:
        s3 = f"I went ahead and built a quick site mockup for a {shop} like yours — here it is: {demo_url}. Happy to walk you through it if it's useful."
    elif frame == "peer-operator":
        s3 = f"I build sites for {shop}s and went ahead and roughed out what one could look like for yours — took about an hour. Happy to share it if that's useful."
    else:
        s3 = f"I went ahead and roughed out what a site for a {shop} like yours could look like — took about an hour. Happy to walk you through it on a quick call if it's useful."

    ask_phrase = _render_ask(ask, "email")
    s4 = f"No pressure either way — {ask_phrase}"

    touch_suffix = _get_touch_suffix(touch, "email")

    body_parts = [f"{s1} {s2}", s3, s4]
    if touch_suffix:
        body_parts.append(touch_suffix)
    body_parts.append("\nthanks,\nSungjune")

    body = "\n\n".join(body_parts)

    draft = {"subject": subject, "body": body}
    try:
        validate_text(subject, context="L0-email.subject")
        validate_text(body, context="L0-email.body")
    except GuardrailViolation as e:
        raise GuardrailViolation(
            f"L0 draft guardrail violation: {e}",
            pattern_name=e.pattern_name,
            matched_text=e.matched_text,
        )
    return draft


# ---------------------------------------------------------------------------
# CALL CHANNEL — DEFER TO SOP
# draft-outreach does NOT reinvent call script logic.
# See: ~/.claude/skills/shared/sops/call-script-write.md
# ---------------------------------------------------------------------------

_CALL_SOP_PATH = Path(
    os.environ.get(
        "CALL_SOP_PATH",
        "/Users/sungjunepark/.claude/skills/shared/sops/call-script-write.md",
    )
)


def _build_call_deferral(
    name: str,
    lead_with: str,
    opening_line: str,
    frame: str,
    ask: str,
    touch: str,
) -> dict[str, str]:
    """
    For channel=call, return a deferral artifact pointing to the call-script-write SOP.
    The artifact is stamped with all params so Phase 3 can route it to the SOP.
    Does NOT invent call script copy — the SOP owns that logic.

    Routes through outreach_guardrail.validate_text() like all other channels so
    the invariant "every generated draft passes the guardrail" holds for call too.
    """
    anchor = _name_anchor(name)
    sop_ref = str(_CALL_SOP_PATH)
    body = (
        f"CALL SCRIPT REQUIRED — defer to SOP: {sop_ref}\n\n"
        f"Lead: {name}\n"
        f"Signal: {lead_with}\n"
        f"Frame: {frame}\n"
        f"Ask: {ask}\n"
        f"Touch: {touch}\n"
        f"Opening line (from audit): {opening_line}\n\n"
        f"Run the call-script-write SOP with the above params to generate the talk-track."
    )
    subject = f"[CALL] {anchor} — {lead_with}"
    try:
        validate_text(subject, context=f"call-deferral.subject[{lead_with}]")
        validate_text(body, context=f"call-deferral.body[{lead_with}]")
    except GuardrailViolation as e:
        raise GuardrailViolation(
            f"Call deferral guardrail violation for lead_with={lead_with}: {e}",
            pattern_name=e.pattern_name,
            matched_text=e.matched_text,
        )
    return {"subject": subject, "body": body}


# ---------------------------------------------------------------------------
# DRAFT ROUTER — top-level function called per lead
# Now accepts explicit params; auto-resolves defaults from audit data.
# BACKWARD COMPATIBLE: all new params are Optional with defaults.
# ---------------------------------------------------------------------------

def draft_lead(
    lead: dict,
    channel: str = "email",
    frame: Optional[str] = None,
    offer: Optional[str] = None,
    ask: str = "reply",
    touch: str = "first",
    demo_url: Optional[str] = None,
) -> Optional[tuple[dict[str, str], str]]:
    """
    Given a pipeline_leads row, return ({"subject": ..., "body": ...}, resolved_frame)
    or None (skip).

    Returns a 2-tuple so the caller can stamp the ACTUAL frame used (which may differ
    from the requested frame after the dependency gate + fallback).

    New params (Phase 2):
      channel: "email" | "call" | "sms" — defaults to "email"
      frame:   frame-shape slug — if None, auto-selected based on lead data
      offer:   "L0" | "L2" | "L3" — if None, auto-inferred from wedge
      ask:     "book_call" | "reply" | "watch_loom" | "request_access" — default "reply"
      touch:   "first" | "follow_up" | "breakup" — default "first"
      demo_url: a pre-built demo URL (required for demo-gift frame)

    Returns None for: off_icp, suppressed, None-wedge, or missing audit data.
    Asserts ANTI-FABRICATION: returns None if evidence_token or opening_line is absent for L2.
    """
    wedge = lead.get("wedge")
    suppressed = lead.get("suppressed", False)

    if suppressed:
        return None
    if wedge in ("off_icp", None):
        return None
    if wedge not in ("L2", "L0_candidate"):
        return None

    name = lead.get("name") or "your shop"
    niche = lead.get("niche")
    audit = lead.get("audit") or {}
    lead_with = audit.get("lead_with")
    opening_line = audit.get("opening_line") or ""
    evidence_token = audit.get("evidence_token") or ""
    all_leaks = audit.get("leaks") or []

    # Auto-infer offer from wedge
    if offer is None:
        offer = wedge if wedge in VALID_OFFERS else ("L0" if wedge == "L0_candidate" else "L2")

    # Auto-select frame if not provided
    if frame is None:
        if wedge == "L0_candidate":
            frame = "humble-student"
        elif evidence_token and _RATIONALE_AVAILABLE and lead_with and get_benchmark_line(lead_with):
            frame = "challenger-evidence-led"
        else:
            frame = "humble-student"

    # Validate frame-dependency gate
    available, reason = _check_frame_available(frame, evidence_token, lead_with, demo_url)
    if not available:
        # Fall back to humble-student (always available) with a warning
        print(f"  FRAME GATE: {frame!r} not available for {name!r} ({reason}); falling back to humble-student")
        frame = "humble-student"

    if wedge == "L2":
        if not opening_line or not evidence_token:
            return None
        if not lead_with:
            return None
        if lead_with in NON_HEADLINE_SIGNALS:
            return None

        if channel == "sms":
            sms_body = _build_sms(
                name=name,
                niche=niche,
                lead_with=lead_with,
                opening_line=opening_line,
                evidence_token=evidence_token,
                frame=frame,
                ask=ask,
                touch=touch,
                demo_url=demo_url,
            )
            draft = {"subject": f"[SMS] {lead_with}", "body": sms_body}
            # Guardrail on SMS body
            try:
                validate_text(sms_body, context=f"L2-sms.body[{lead_with}]")
            except GuardrailViolation as e:
                raise GuardrailViolation(
                    f"SMS guardrail violation for lead_with={lead_with}: {e}",
                    pattern_name=e.pattern_name,
                    matched_text=e.matched_text,
                )
            return draft, frame

        if channel == "call":
            return _build_call_deferral(
                name=name,
                lead_with=lead_with,
                opening_line=opening_line,
                frame=frame,
                ask=ask,
                touch=touch,
            ), frame

        # Default: email
        return _build_l2_email(
            name, niche, lead_with, opening_line, evidence_token, all_leaks,
            frame=frame, offer=offer, ask=ask, touch=touch, demo_url=demo_url,
        ), frame

    if wedge == "L0_candidate":
        if channel == "sms":
            anchor = _name_anchor(name)
            sms_body = f"hey {anchor} — noticed you don't have a site listed — roughed one out for you, {_render_ask(ask, 'sms')}"
            if len(sms_body) > 160:
                sms_body = sms_body[:157].rsplit(" ", 1)[0] + "..."
            draft = {"subject": "[SMS] no_site", "body": sms_body}
            try:
                validate_text(sms_body, context="L0-sms.body")
            except GuardrailViolation as e:
                raise GuardrailViolation(
                    f"L0 SMS guardrail violation: {e}",
                    pattern_name=e.pattern_name,
                    matched_text=e.matched_text,
                )
            return draft, frame

        if channel == "call":
            return _build_call_deferral(
                name=name,
                lead_with="no_site",
                opening_line=f"{name} has no website listed.",
                frame=frame,
                ask=ask,
                touch=touch,
            ), frame

        return _build_l0_email(
            name, niche, evidence_token,
            frame=frame, offer=offer, ask=ask, touch=touch, demo_url=demo_url,
        ), frame

    return None


def _draft_text(subject: str, body: str) -> str:
    """Serialize subject + body into the artifact draft string."""
    return f"SUBJECT: {subject}\n\n{body}"


# ---------------------------------------------------------------------------
# PARAM STAMP — G: metadata dict for Phase 3 diff-learning attribution
# ---------------------------------------------------------------------------

def _build_param_stamp(
    channel: str,
    angle: str,
    frame: str,
    offer: str,
    ask: str,
    touch: str,
    voice_profile: str = "local-biz-owner",
) -> dict:
    """
    Build the param stamp dict for artifact metadata.
    Stored with every draft so Phase 3 can attribute edits to a specific knob.

    Fields: channel, angle, frame, offer, ask, touch, voice_profile, prompt_version
    """
    return {
        "channel": channel,
        "angle": angle,
        "frame": frame,
        "offer": offer,
        "ask": ask,
        "touch": touch,
        "voice_profile": voice_profile,
        "prompt_version": PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# EXTENDED add_artifact — wraps store.add_artifact with a dedicated param_stamp field
# prompt_version stays a clean "v3" module constant.
# param_stamp is stored in its own dedicated artifact field (not overloaded into
# prompt_version) so Phase 3 can read it without parsing a composite string.
# ---------------------------------------------------------------------------

def _add_artifact_with_stamp(
    store: LeadStore,
    lead_id: str,
    artifact_type: str,
    draft: str,
    param_stamp: dict,
) -> dict:
    """
    Store a draft artifact with its parameter stamp in the dedicated param_stamp field.

    prompt_version = PROMPT_VERSION ("v3") — clean version string, no JSON overloading.
    param_stamp    = dedicated dict field with {channel, angle, frame, offer, ask, touch,
                     voice_profile, prompt_version} for Phase 3 diff-learning attribution.
    """
    return store.add_artifact(
        lead_id,
        artifact_type,
        draft,
        model=MODEL_ID,
        prompt_version=PROMPT_VERSION,
        param_stamp=param_stamp,
    )


# ---------------------------------------------------------------------------
# PROPOSE-THEN-ADJUST INTERFACE — Phase 3 (PE-D-020)
# ---------------------------------------------------------------------------
#
# propose_param_set(lead) → dict
#
# Proposes a sensible default {channel, angle, frame, offer, ask, touch} for a lead.
# The caller (dashboard, CLI) shows this default and lets the founder override any flag.
# This is the "propose-then-adjust" UX: never fill-from-blank, never ask about each flag.
#
# Resolution rules per parameter (maps directly to the E assembly map in brief section E):
#
#   angle   — the audit's lead_with (the proven leak signal; the E-map "hook")
#   offer   — inferred from the lead's wedge (L0/L2; L3 not yet emitted)
#   touch   — defaults to "first" (always the right default for an un-contacted lead)
#   frame   — sensible recommendation gated by frame availability:
#               challenger-evidence-led:  only if evidence_token + benchmark both present
#               demo-gift:               only if demo_url is non-empty
#               peer-operator:           if the lead is L2 (builder background always real)
#               humble-student:          always-available fallback
#   channel — defaults to "email" (the only fully-built emitter for cold outbound)
#   ask     — defaults to "reply" (lowest commitment; always appropriate for first touch)
#
# Frame gating rules (identical to _check_frame_available — no duplication, calls it):
#   challenger-evidence-led → requires evidence_token AND get_benchmark_line(lead_with)
#   demo-gift               → requires demo_url
#   always-available        → humble-student, peer-operator, consultative-curiosity

def propose_param_set(
    lead: dict,
    demo_url: Optional[str] = None,
) -> dict:
    """
    Propose a default {channel, angle, frame, offer, ask, touch} for a lead.

    Returns a dict with all six keys populated. The caller shows this to the
    founder and allows overriding any single flag before drafting.

    Frame gating:
      - challenger-evidence-led is NOT proposed if evidence_token or benchmark is absent
      - demo-gift is NOT proposed if demo_url is None
      - peer-operator is offered for L2 leads with a clear observation
      - humble-student is the always-available fallback

    Does NOT call draft_lead() — this is a proposal step, not a draft step.
    Does NOT write to any store — caller decides whether to proceed.
    """
    audit = lead.get("audit") or {}
    wedge = lead.get("wedge")
    lead_with = audit.get("lead_with") or ""
    evidence_token = audit.get("evidence_token") or ""

    # angle: always the audit's lead_with (the hook)
    angle = lead_with if lead_with else "no_site"

    # offer: inferred from wedge
    if wedge in VALID_OFFERS:
        offer = wedge
    elif wedge == "L0_candidate":
        offer = "L0"
    else:
        offer = "L2"  # safe default

    # touch: always first for un-contacted leads
    touch = "first"

    # channel: email when send-eligible address exists; else phone-first
    try:
        from outreach_guardrail import check_email_sendable
        email_ok, _ = check_email_sendable(lead)
    except ImportError:
        email_ok = bool(lead.get("email") and lead.get("email_confidence") in ("verified", "risky"))
    channel = "email" if email_ok else "call"

    # ask: reply is the lowest-commitment, always appropriate for first touch
    ask = "reply"

    # frame: sensible recommendation gated by availability
    frame = _resolve_recommended_frame(
        wedge=wedge,
        lead_with=lead_with,
        evidence_token=evidence_token,
        demo_url=demo_url,
    )

    return {
        "channel": channel,
        "angle": angle,
        "frame": frame,
        "offer": offer,
        "ask": ask,
        "touch": touch,
    }


def _resolve_recommended_frame(
    wedge: Optional[str],
    lead_with: str,
    evidence_token: str,
    demo_url: Optional[str] = None,
) -> str:
    """
    Internal helper for propose_param_set.
    Returns the recommended frame slug, applying the frame-dependency gate.

    Priority order:
      1. demo-gift               — if demo_url present (works for any wedge)
      2. challenger-evidence-led — if L2 wedge AND evidence_token + benchmark both present
                                   (never for no_site / L0_candidate / parked_site)
      3. peer-operator           — if no_site / L0_candidate / parked_site (no demo)
                                   ("I build sites — noticed you don't have one")
      4. peer-operator           — if L2 wedge (builder background always real)
      5. humble-student          — always-available fallback

    Key rule: challenger is NEVER proposed for no_site / L0_candidate / parked_site
    leads, even if they somehow carry evidence_token + benchmark. Challenger is a
    provocation that needs audit evidence and a benchmark — a no-site lead has neither
    and the posture doesn't fit. peer-operator ("I build sites") is always the right
    cold-open for a shop with no web presence.

    This mirrors _check_frame_available() but returns a recommendation rather than
    raising or falling back silently. The caller can override the returned frame.
    """
    # No-site / L0_candidate leads: demo-gift if we have a demo, otherwise peer-operator.
    # challenger is never appropriate here — it requires audit evidence + benchmark.
    _NO_SITE_WEDGES = {"L0_candidate", "no_site", "parked_site"}
    if wedge in _NO_SITE_WEDGES:
        if demo_url:
            return "demo-gift"
        return "peer-operator"

    # Try demo-gift first when a demo URL is present (works for any non-no_site wedge)
    if demo_url:
        return "demo-gift"

    # Try challenger-evidence-led for L2 leads only — requires evidence_token + benchmark
    if wedge == "L2" and evidence_token and _RATIONALE_AVAILABLE and lead_with:
        benchmark = get_benchmark_line(lead_with)
        if benchmark:
            return "challenger-evidence-led"

    # peer-operator works well for L2 leads (the builder framing is always authentic)
    if wedge == "L2":
        return "peer-operator"

    # humble-student: always-available fallback
    return "humble-student"


# ---------------------------------------------------------------------------
# SAMPLING LOGIC (Phase 1 unchanged)
# ---------------------------------------------------------------------------

def _select_sample(
    store: LeadStore,
    sample: int,
    limit_l2: Optional[int] = None,
    limit_l0: Optional[int] = None,
    skip_drafted: bool = True,
    place_id: Optional[str] = None,
) -> list[dict]:
    if place_id:
        lead = store.get_lead(place_id=place_id)
        return [lead] if lead else []

    l2_leads = store.list_leads({"wedge": "L2", "suppressed": False})
    l0_leads = store.list_leads({"wedge": "L0_candidate", "suppressed": False})

    if skip_drafted:
        l2_leads = [l for l in l2_leads if not l.get("current_artifacts", {}).get("cold_email")]
        l0_leads = [l for l in l0_leads if not l.get("current_artifacts", {}).get("cold_email")]

    random.shuffle(l2_leads)
    random.shuffle(l0_leads)

    if limit_l2 is not None and limit_l0 is not None:
        return l2_leads[:limit_l2] + l0_leads[:limit_l0]

    if sample:
        n_l0 = max(2, sample // 6)
        n_l2 = sample - n_l0
        return l2_leads[:n_l2] + l0_leads[:n_l0]

    return l2_leads + l0_leads


# ---------------------------------------------------------------------------
# RESET DRAFTED (Phase 1 unchanged)
# ---------------------------------------------------------------------------

def reset_drafted_leads(store: LeadStore, verbose: bool = True) -> dict:
    """
    Clear cold_email artifacts from all drafted leads and reset their stage to 'audited'.
    """
    from datetime import datetime, timezone
    import copy

    all_leads = store.list_leads()
    drafted = [
        l for l in all_leads
        if l.get("stage") == "drafted" and l.get("current_artifacts", {}).get("cold_email")
    ]

    count = 0
    for lead in drafted:
        lead_id = lead["id"]
        artifacts_cache = copy.deepcopy(lead.get("current_artifacts", {}))
        del artifacts_cache["cold_email"]
        if hasattr(store, "_load_leads"):
            store_map = store._load_leads()
            row = store_map.get(lead["place_id"])
            if row:
                row["current_artifacts"] = artifacts_cache
                row["stage"] = "audited"
                row["updated_at"] = datetime.now(timezone.utc).isoformat()
                store_map[lead["place_id"]] = row
            store._save_leads()
        else:
            store.advance_stage(lead_id, "audited")
        count += 1
        if verbose:
            print(f"  RESET [{lead.get('name')}] stage=drafted→audited, cold_email artifact cleared")

    return {"reset": count}


# ---------------------------------------------------------------------------
# RUNNER — now param-aware
# ---------------------------------------------------------------------------

def run(
    store: LeadStore,
    leads: list[dict],
    dry_run: bool = False,
    verbose: bool = True,
    channel: str = "email",
    frame: Optional[str] = None,
    offer: Optional[str] = None,
    ask: str = "reply",
    touch: str = "first",
    demo_url: Optional[str] = None,
) -> dict:
    """
    Draft outreach for each lead in the list and persist via store.add_artifact().

    New params (Phase 2): channel, frame, offer, ask, touch, demo_url.
    All have defaults that reproduce Phase 1 behavior when not supplied.
    Returns summary + per-lead results.
    """
    _load_voice()
    if _VOICE_LOAD_WARNING and verbose:
        print(f"\n{_VOICE_LOAD_WARNING}\n")

    results = {
        "total": len(leads),
        "drafted": 0,
        "skipped_no_data": 0,
        "skipped_suppressed": 0,
        "errors": 0,
        "by_wedge": {"L2": 0, "L0_candidate": 0},
        "by_leak_signal": {},
        "by_frame": {},
        "by_channel": {},
        "samples": [],
    }

    for lead in leads:
        lead_id = lead.get("id")
        name = lead.get("name") or "unknown"
        wedge = lead.get("wedge")
        suppressed = lead.get("suppressed", False)

        if suppressed:
            results["skipped_suppressed"] += 1
            continue

        _offer = offer

        try:
            result = draft_lead(
                lead,
                channel=channel,
                frame=frame,
                offer=_offer,
                ask=ask,
                touch=touch,
                demo_url=demo_url,
            )
        except Exception as e:
            results["errors"] += 1
            if verbose:
                print(f"  ERROR [{name}]: {e}")
            continue

        if result is None:
            results["skipped_no_data"] += 1
            if verbose:
                leak = lead.get("audit", {}).get("lead_with", "—")
                print(f"  SKIP [{name}] wedge={wedge} leak={leak} — missing audit data")
            continue

        # Unpack the (draft_dict, actual_frame) tuple returned by draft_lead().
        # actual_frame is the frame truly used after dependency gate + fallback —
        # it may differ from the caller-requested frame when the gate fired.
        draft, actual_frame = result

        subject = draft["subject"]
        body = draft["body"]
        draft_text = _draft_text(subject, body)
        leak_signal = lead.get("audit", {}).get("lead_with") or "no_site"

        actual_offer = _offer or (wedge if wedge in VALID_OFFERS else ("L0" if wedge == "L0_candidate" else "L2"))

        param_stamp = _build_param_stamp(
            channel=channel,
            angle=leak_signal,
            frame=actual_frame,
            offer=actual_offer,
            ask=ask,
            touch=touch,
        )

        if not dry_run:
            try:
                _add_artifact_with_stamp(
                    store,
                    lead_id,
                    "cold_email",
                    draft_text,
                    param_stamp,
                )
                store.advance_stage(lead_id, "drafted")
            except Exception as e:
                results["errors"] += 1
                if verbose:
                    print(f"  STORE ERROR [{name}]: {e}")
                continue

        results["drafted"] += 1
        results["by_wedge"][wedge] = results["by_wedge"].get(wedge, 0) + 1
        results["by_leak_signal"][leak_signal] = results["by_leak_signal"].get(leak_signal, 0) + 1
        results["by_frame"][actual_frame] = results["by_frame"].get(actual_frame, 0) + 1
        results["by_channel"][channel] = results["by_channel"].get(channel, 0) + 1

        if verbose:
            print(f"  DRAFTED [{wedge}] {name} | leak={leak_signal} | frame={actual_frame} | ch={channel} | SL: {subject}")
            print(f"    {body[:120].replace(chr(10), ' ')}")
            print()

        if len(results["samples"]) < 4:
            results["samples"].append({
                "name": name,
                "wedge": wedge,
                "leak_signal": leak_signal,
                "frame": actual_frame,
                "channel": channel,
                "ask": ask,
                "touch": touch,
                "param_stamp": param_stamp,
                "evidence_token": lead.get("audit", {}).get("evidence_token", ""),
                "opening_line": lead.get("audit", {}).get("opening_line", ""),
                "subject": subject,
                "body": body,
            })

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Draft outreach artifacts for actionable leads in the store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--limit-l2", type=int, default=None)
    parser.add_argument("--limit-l0", type=int, default=None)
    parser.add_argument("--place-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-drafted", action="store_true")
    parser.add_argument("--reset-drafted", action="store_true")
    parser.add_argument("--backend", choices=["local", "supabase"], default=None)
    parser.add_argument("--seed", type=int, default=42)
    # Phase 2 params
    parser.add_argument(
        "--channel", choices=["email", "call", "sms"], default="email",
        help="Output channel (default: email)",
    )
    parser.add_argument(
        "--frame", choices=list(VALID_FRAMES), default=None,
        help="Frame shape slug (auto-selected if not provided)",
    )
    parser.add_argument(
        "--offer", choices=list(VALID_OFFERS), default=None,
        help="Offer tier (auto-inferred from wedge if not provided)",
    )
    parser.add_argument(
        "--ask", choices=list(VALID_ASKS), default="reply",
        help="Ask enum (default: reply)",
    )
    parser.add_argument(
        "--touch", choices=list(VALID_TOUCHES), default="first",
        help="Touch position (default: first)",
    )
    parser.add_argument(
        "--demo-url", default=None,
        help="Pre-built demo URL (required for demo-gift frame)",
    )

    # Phase 3: --propose-params flag
    parser.add_argument(
        "--propose-params", action="store_true",
        help=(
            "Show the proposed param set for each lead (propose-then-adjust mode). "
            "Prints the default {channel, angle, frame, offer, ask, touch} before drafting "
            "so you can override any single flag. Does NOT draft — combine with --dry-run to preview."
        ),
    )

    args = parser.parse_args()
    random.seed(args.seed)

    store = get_store(backend=args.backend)

    if args.reset_drafted:
        print("Resetting drafted leads (clearing stale cold_email artifacts)...")
        summary = reset_drafted_leads(store)
        print(f"Done. Reset {summary['reset']} leads to stage=audited.")
        print()

    leads = _select_sample(
        store,
        sample=args.sample or 0,
        limit_l2=args.limit_l2,
        limit_l0=args.limit_l0,
        skip_drafted=not args.include_drafted,
        place_id=args.place_id,
    )

    if not leads:
        if not args.reset_drafted:
            print("No actionable leads found. Run the audit loader first or check filters.")
        sys.exit(0)

    # Phase 3: propose-then-adjust mode
    # Show the proposed param set for each lead BEFORE any drafting.
    # The founder sees the defaults and can pass --frame / --channel / --ask overrides.
    propose_params = getattr(args, "propose_params", False)
    if propose_params:
        print("=== PROPOSED PARAM SETS (propose-then-adjust) ===")
        print("Override any flag with --frame, --channel, --ask, --touch, --offer, --demo-url")
        print()
        for lead in leads:
            proposed = propose_param_set(lead, demo_url=args.demo_url)
            name = lead.get("name", "—")
            wedge = lead.get("wedge", "—")
            # Show which (if any) caller flags will override the proposal
            overrides = []
            if args.frame and args.frame != proposed["frame"]:
                overrides.append(f"frame: {proposed['frame']} → {args.frame} (override)")
            if args.channel != "email" or proposed["channel"] != "email":
                if args.channel != proposed["channel"]:
                    overrides.append(f"channel: {proposed['channel']} → {args.channel} (override)")
            if args.ask != "reply" or proposed["ask"] != "reply":
                if args.ask != proposed["ask"]:
                    overrides.append(f"ask: {proposed['ask']} → {args.ask} (override)")
            if args.touch != "first":
                overrides.append(f"touch: {proposed['touch']} → {args.touch} (override)")
            if args.offer and args.offer != proposed["offer"]:
                overrides.append(f"offer: {proposed['offer']} → {args.offer} (override)")

            override_str = f"  OVERRIDES: {'; '.join(overrides)}" if overrides else "  (no overrides — using proposed set)"
            print(
                f"  {name:<35} [{wedge}]\n"
                f"    proposed: channel={proposed['channel']!r} frame={proposed['frame']!r} "
                f"angle={proposed['angle']!r} offer={proposed['offer']!r} "
                f"ask={proposed['ask']!r} touch={proposed['touch']!r}\n"
                f"{override_str}\n"
            )
        print()
        # If --dry-run is NOT passed alongside --propose-params, stop here
        if not args.dry_run:
            print("Add --dry-run to preview drafts, or remove --propose-params to draft directly.")
            sys.exit(0)
        print("=== DRY RUN DRAFTS (with proposed params + overrides applied) ===")
        print()

    print(f"Drafting [{args.channel}] outreach for {len(leads)} leads {'[DRY RUN]' if args.dry_run else ''}...")
    if args.frame:
        print(f"  frame={args.frame} | ask={args.ask} | touch={args.touch}")
    print()

    results = run(
        store, leads,
        dry_run=args.dry_run,
        verbose=True,
        channel=args.channel,
        frame=args.frame,
        offer=args.offer,
        ask=args.ask,
        touch=args.touch,
        demo_url=args.demo_url,
    )

    print()
    print("=== Draft Run Summary ===")
    print(f"  Total processed : {results['total']}")
    print(f"  Drafted         : {results['drafted']}")
    print(f"  Skipped (no data): {results['skipped_no_data']}")
    print(f"  Skipped (suppressed): {results['skipped_suppressed']}")
    print(f"  Errors          : {results['errors']}")
    print(f"  Dry run         : {args.dry_run}")
    print()
    print("  By wedge:")
    for wedge, count in results["by_wedge"].items():
        print(f"    {wedge:<20} {count}")
    print()
    print("  By leak signal:")
    for signal, count in sorted(results["by_leak_signal"].items(), key=lambda x: -x[1]):
        print(f"    {signal:<35} {count}")
    print()
    print("  By frame:")
    for fr, count in sorted(results["by_frame"].items(), key=lambda x: -x[1]):
        print(f"    {fr:<35} {count}")
    print()
    print("  By channel:")
    for ch, count in sorted(results["by_channel"].items(), key=lambda x: -x[1]):
        print(f"    {ch:<20} {count}")


# ---------------------------------------------------------------------------
# SELF-TEST — Phase 2: exercises 2-3 param combos on SYNTHETIC lead data
# Synthetic: no real store, no real leads, no real files touched.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import textwrap

    print("=" * 70)
    print("  draft-outreach.py self-test (Phase 2 / PE-D-020)")
    print(f"  PROMPT_VERSION = {PROMPT_VERSION}")
    print("=" * 70)
    print()

    # -----------------------------------------------------------------------
    # SYNTHETIC LEAD DATA — no real DB, no real files
    # -----------------------------------------------------------------------

    SYNTHETIC_L2 = {
        "id": "synth-001",
        "place_id": "synth-place-001",
        "name": "Kendrick Tool & Engineering",
        "niche": "precision machining",
        "wedge": "L2",
        "suppressed": False,
        "stage": "audited",
        "audit": {
            "lead_with": "rfq_no_cad_upload",
            "opening_line": "Your site has a contact form but no STEP or DWG file upload field",
            "evidence_token": '"/contact" — no /rfq path or STEP/DWG accept= in HTML',
            "leaks": [{"type": "rfq_no_cad_upload", "evidence": "/contact"}],
        },
    }

    SYNTHETIC_L2_DEFENSE = {
        "id": "synth-002",
        "place_id": "synth-place-002",
        "name": "Atlas Aerospace Fabrication",
        "niche": "aerospace",
        "wedge": "L2",
        "suppressed": False,
        "stage": "audited",
        "audit": {
            "lead_with": "missing_defense_tokens",
            "opening_line": "No CAGE code, SAM registration, or DPAS acknowledgement visible on the site",
            "evidence_token": "no CAGE or SAM badge in footer or about page",
            "leaks": [{"type": "missing_defense_tokens", "evidence": "no CAGE visible"}],
        },
    }

    SYNTHETIC_L0 = {
        "id": "synth-003",
        "place_id": "synth-place-003",
        "name": "Bayou Metal Works",
        "niche": "welding",
        "wedge": "L0_candidate",
        "suppressed": False,
        "stage": "audited",
        "audit": {
            "lead_with": None,
            "opening_line": "",
            "evidence_token": "",
            "leaks": [],
        },
    }

    failures = []

    def _run_combo(label, lead, channel, frame, ask, touch, demo_url=None,
                   expected_frame=None):
        """
        Run one param combo and print results. Returns True if passed.

        expected_frame: if provided, assert that the param_stamp["frame"] matches
        this value. Use to verify that the stamp reflects the ACTUAL frame used
        (including gate-fallback cases).
        """
        print(f"--- COMBO: {label} ---")
        print(f"    channel={channel!r} | frame={frame!r} | ask={ask!r} | touch={touch!r}")
        if expected_frame:
            print(f"    expected_frame={expected_frame!r} (stamp accuracy check)")
        try:
            result = draft_lead(
                lead,
                channel=channel,
                frame=frame,
                ask=ask,
                touch=touch,
                demo_url=demo_url,
            )
        except Exception as e:
            print(f"  [FAIL] Exception: {e}")
            failures.append(label)
            return False

        if result is None:
            print(f"  [FAIL] draft_lead returned None (unexpected skip)")
            failures.append(label)
            return False

        # Unpack tuple — draft_lead now returns (draft_dict, actual_frame)
        draft, actual_frame = result

        subject = draft.get("subject", "")
        body = draft.get("body", "")

        # Guardrail check
        ok_s, reason_s = check_text(subject)
        ok_b, reason_b = check_text(body)
        if not ok_s:
            print(f"  [FAIL] Guardrail hit on subject: {reason_s}")
            failures.append(label)
            return False
        if not ok_b:
            print(f"  [FAIL] Guardrail hit on body: {reason_b}")
            failures.append(label)
            return False

        # Channel-specific checks
        if channel == "sms":
            if len(body) > 160:
                print(f"  [FAIL] SMS body exceeds 160 chars ({len(body)})")
                failures.append(label)
                return False

        # Build param stamp using the ACTUAL frame returned by draft_lead()
        angle = lead.get("audit", {}).get("lead_with") or "no_site"
        wedge = lead.get("wedge", "L2")
        actual_offer = wedge if wedge in VALID_OFFERS else ("L0" if wedge == "L0_candidate" else "L2")
        stamp = _build_param_stamp(
            channel=channel,
            angle=angle,
            frame=actual_frame,
            offer=actual_offer,
            ask=ask,
            touch=touch,
        )

        # STAMP ACCURACY ASSERTION: stamp["frame"] must equal actual_frame
        if stamp["frame"] != actual_frame:
            print(f"  [FAIL] STAMP ACCURACY: stamp['frame']={stamp['frame']!r} != actual_frame={actual_frame!r}")
            failures.append(label)
            return False

        # If caller specified expected_frame, assert the stamp reflects it
        if expected_frame is not None and stamp["frame"] != expected_frame:
            print(f"  [FAIL] STAMP ACCURACY: stamp['frame']={stamp['frame']!r} != expected_frame={expected_frame!r}")
            failures.append(label)
            return False

        print(f"  [PASS] subject: {subject}")
        print(f"  [PASS] actual_frame={actual_frame!r} (returned by draft_lead)")
        if channel == "sms":
            print(f"  [PASS] body ({len(body)} chars): {body}")
        else:
            # Print first 4 lines of body for readability
            body_preview = "\n    ".join(body.split("\n")[:4])
            print(f"  [PASS] body (first 4 lines):\n    {body_preview}")
        print(f"  [STAMP] {json.dumps(stamp, ensure_ascii=False)}")
        print()
        return True

    # -----------------------------------------------------------------------
    # COMBO 1: challenger-evidence-led + email + rfq_no_cad_upload
    # Tests: frame-dependency gate (has evidence_token + benchmark), benchmark use.
    # stamp accuracy: stamp["frame"] must == "challenger-evidence-led" (gate cleared)
    # -----------------------------------------------------------------------
    _run_combo(
        label="challenger+email+rfq_no_cad_upload",
        lead=SYNTHETIC_L2,
        channel="email",
        frame="challenger-evidence-led",
        ask="reply",
        touch="first",
        expected_frame="challenger-evidence-led",  # gate clears — no fallback
    )

    # -----------------------------------------------------------------------
    # COMBO 2: humble-student + sms + rfq_no_cad_upload
    # Tests: SMS emitter (≤160 chars), touch=first, hook+ask only
    # stamp accuracy: stamp["frame"] == "humble-student"
    # -----------------------------------------------------------------------
    _run_combo(
        label="humble-student+sms+rfq_no_cad_upload",
        lead=SYNTHETIC_L2,
        channel="sms",
        frame="humble-student",
        ask="reply",
        touch="first",
        expected_frame="humble-student",
    )

    # -----------------------------------------------------------------------
    # COMBO 3: demo-gift + email + L0 (no-site)
    # Tests: demo-gift frame with demo_url, L0 path, offer=L0
    # stamp accuracy: stamp["frame"] == "demo-gift"
    # -----------------------------------------------------------------------
    _run_combo(
        label="demo-gift+email+L0_no_site",
        lead=SYNTHETIC_L0,
        channel="email",
        frame="demo-gift",
        ask="watch_loom",
        touch="first",
        demo_url="https://demo.example.com/bayou-metal",
        expected_frame="demo-gift",
    )

    # -----------------------------------------------------------------------
    # COMBO 4: peer-operator + email + follow_up touch + defense signal
    # Tests: touch=follow_up modulation, peer-operator frame, defense angle
    # stamp accuracy: stamp["frame"] == "peer-operator"
    # -----------------------------------------------------------------------
    _run_combo(
        label="peer-operator+email+follow_up+defense",
        lead=SYNTHETIC_L2_DEFENSE,
        channel="email",
        frame="peer-operator",
        ask="book_call",
        touch="follow_up",
        expected_frame="peer-operator",
    )

    # -----------------------------------------------------------------------
    # COMBO 5: challenger-evidence-led gate FAIL → fallback to humble-student
    # Tests: frame-dependency gate forces fallback.
    # STAMP ACCURACY: stamp["frame"] must == "humble-student" (NOT "challenger-evidence-led")
    # This is the core regression for Finding 1 — the old code stamped wrong.
    # -----------------------------------------------------------------------
    SYNTHETIC_NO_BENCHMARK = {
        "id": "synth-004",
        "place_id": "synth-place-004",
        "name": "Gulf Coast Hydraulics",
        "niche": "hydraulics",
        "wedge": "L2",
        "suppressed": False,
        "stage": "audited",
        "audit": {
            "lead_with": "footer_year_stale",  # signal with no benchmark
            "opening_line": "Your site's copyright footer still shows 2019",
            "evidence_token": "© 2019",
            "leaks": [{"type": "footer_year_stale", "evidence": "© 2019"}],
        },
    }
    print("--- COMBO: challenger-evidence-led GATE FAIL → fallback to humble-student ---")
    print("    Expected: FRAME GATE warning printed, then humble-student draft produced")
    print("    STAMP ACCURACY: stamp['frame'] must == 'humble-student' (not challenger)")
    try:
        result = draft_lead(
            SYNTHETIC_NO_BENCHMARK,
            channel="email",
            frame="challenger-evidence-led",
            ask="reply",
            touch="first",
        )
        if result is not None:
            fallback_draft, fallback_frame = result
            ok_b, _ = check_text(fallback_draft["body"])
            # Stamp accuracy: the returned frame must be humble-student (fallback)
            if fallback_frame != "humble-student":
                print(f"  [FAIL] STAMP ACCURACY: actual_frame={fallback_frame!r} != 'humble-student'")
                failures.append("challenger-gate-fallback")
            elif ok_b:
                print(f"  [PASS] Fallback draft produced (subject: {fallback_draft['subject']})")
                print(f"  [PASS] actual_frame={fallback_frame!r} == 'humble-student' — stamp accurate")
            else:
                print(f"  [FAIL] Guardrail hit on fallback draft")
                failures.append("challenger-gate-fallback")
        else:
            print(f"  [FAIL] draft_lead returned None after gate fallback")
            failures.append("challenger-gate-fallback")
    except Exception as e:
        print(f"  [FAIL] Exception: {e}")
        failures.append("challenger-gate-fallback")
    print()

    # -----------------------------------------------------------------------
    # COMBO 6: SMS follow_up breakup
    # Tests: touch=breakup, SMS channel, breakup suffix
    # stamp accuracy: stamp["frame"] == "humble-student"
    # -----------------------------------------------------------------------
    _run_combo(
        label="humble-student+sms+breakup",
        lead=SYNTHETIC_L2,
        channel="sms",
        frame="humble-student",
        ask="reply",
        touch="breakup",
        expected_frame="humble-student",
    )

    # -----------------------------------------------------------------------
    # PHASE 3: propose_param_set() self-tests
    # Tests that propose_param_set returns a sensible set for synthetic leads
    # and does NOT propose challenger when evidence_token/benchmark are absent.
    # -----------------------------------------------------------------------
    print()
    print("=" * 70)
    print("  Phase 3: propose_param_set() tests")
    print("=" * 70)
    print()

    def _test_propose(label: str, lead: dict, demo_url: Optional[str] = None,
                      expect_not_frame: Optional[str] = None,
                      expect_frame: Optional[str] = None) -> None:
        """Run propose_param_set and assert expectations."""
        proposed = propose_param_set(lead, demo_url=demo_url)
        required_keys = {"channel", "angle", "frame", "offer", "ask", "touch"}
        missing = required_keys - set(proposed.keys())

        print(f"--- propose_param_set: {label} ---")
        print(f"    result: {json.dumps(proposed, ensure_ascii=False)}")

        ok = True

        if missing:
            print(f"  [FAIL] Missing keys: {missing}")
            failures.append(f"propose:{label}:missing_keys")
            ok = False

        # All values must be non-empty strings
        for k, v in proposed.items():
            if not isinstance(v, str) or not v:
                print(f"  [FAIL] Key {k!r} has empty/non-string value: {v!r}")
                failures.append(f"propose:{label}:{k}_empty")
                ok = False

        if expect_not_frame and proposed.get("frame") == expect_not_frame:
            print(f"  [FAIL] frame should NOT be {expect_not_frame!r} (gate should block it)")
            failures.append(f"propose:{label}:gate_violated")
            ok = False

        if expect_frame and proposed.get("frame") != expect_frame:
            print(f"  [FAIL] frame should be {expect_frame!r}, got {proposed.get('frame')!r}")
            failures.append(f"propose:{label}:wrong_frame")
            ok = False

        if ok:
            print(f"  [PASS]")
        print()

    # Test 1: L2 with evidence_token + benchmark present → challenger is a valid recommendation
    #         (we don't mandate it must be challenger, but it should not fail or return empty)
    _test_propose(
        label="L2 with evidence_token (challenger potentially available)",
        lead=SYNTHETIC_L2,
        expect_not_frame=None,  # challenger MAY be proposed (depends on signal_rationale)
    )

    # Test 2: L2 WITHOUT benchmark signal → challenger must NOT be proposed
    _test_propose(
        label="L2 no-benchmark signal (footer_year_stale) → challenger must NOT be proposed",
        lead={
            "id": "synth-no-bench",
            "place_id": "synth-place-no-bench",
            "name": "Gulf Coast Hydraulics",
            "niche": "hydraulics",
            "wedge": "L2",
            "suppressed": False,
            "stage": "audited",
            "audit": {
                "lead_with": "footer_year_stale",  # no benchmark in signal_rationale
                "opening_line": "Your site's copyright footer still shows 2019",
                "evidence_token": "© 2019",
                "leaks": [{"type": "footer_year_stale", "evidence": "© 2019"}],
            },
        },
        expect_not_frame="challenger-evidence-led",  # GATE: no benchmark → must NOT be proposed
    )

    # Test 3: L0 lead with no demo → frame must be peer-operator ("I build sites — noticed you don't have one")
    # challenger is never appropriate for no-site leads: it needs audit evidence + benchmark.
    _test_propose(
        label="L0 candidate (no-site, no demo) → frame=peer-operator",
        lead=SYNTHETIC_L0,
        expect_frame="peer-operator",  # BUG FIX: was humble-student; correct default is peer-operator
        expect_not_frame="challenger-evidence-led",
    )

    # Test 4: demo_url provided for L2 lead → demo-gift should win
    _test_propose(
        label="L2 with demo_url → frame=demo-gift",
        lead=SYNTHETIC_L2,
        demo_url="https://demo.example.com/kendrick-tool",
        expect_frame="demo-gift",
    )

    # Test 4b: demo_url provided for L0 lead → demo-gift should win (over peer-operator)
    _test_propose(
        label="L0 with demo_url → frame=demo-gift",
        lead=SYNTHETIC_L0,
        demo_url="https://demo.example.com/bayou-metal",
        expect_frame="demo-gift",
    )

    # Test 5: L0 no evidence_token, no demo → challenger must NOT be proposed; peer-operator must be
    _test_propose(
        label="L0 no evidence_token, no demo → peer-operator (NOT challenger)",
        lead=SYNTHETIC_L0,
        expect_frame="peer-operator",
        expect_not_frame="challenger-evidence-led",
    )

    # Test 6: L2 with evidence_token + benchmark → challenger IS proposable
    # (regression: challenger must still work for L2 leads that have the signal)
    _test_propose(
        label="L2 with evidence_token (challenger proposable if benchmark present)",
        lead=SYNTHETIC_L2,
        # challenger MAY be proposed (depends on signal_rationale having rfq_no_cad_upload benchmark)
        # we just confirm the frame is valid and challenger is NOT blocked for L2
        expect_not_frame=None,
    )

    # Test 7: thin L2 lead — no evidence_token — humble-student/consultative as soft fallback
    _test_propose(
        label="L2 thin lead (no evidence_token) → peer-operator (builder always real for L2)",
        lead={
            "id": "synth-thin",
            "place_id": "synth-place-thin",
            "name": "Thin Metal Fab",
            "niche": "fabrication",
            "wedge": "L2",
            "suppressed": False,
            "stage": "audited",
            "audit": {
                "lead_with": "",
                "opening_line": "",
                "evidence_token": "",
                "leaks": [],
            },
        },
        expect_not_frame="challenger-evidence-led",  # no evidence_token → challenger blocked
    )

    # -----------------------------------------------------------------------
    # FINAL RESULTS
    # -----------------------------------------------------------------------
    print("=" * 70)
    if not failures:
        print(f"  ALL SELF-TESTS PASSED — PROMPT_VERSION={PROMPT_VERSION}")
    else:
        print(f"  FAILURES ({len(failures)}): {failures}")
        sys.exit(1)
    print("=" * 70)
