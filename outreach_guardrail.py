#!/usr/bin/env python3
"""
outreach_guardrail.py  —  Hard no-fabrication validator for the outbound engine.

Used by BOTH lead-audit.py and draft-outreach.py BEFORE any text is emitted.

BLOCKS:
  (a) Per-shop quantified claims: patterns like "you're losing N", "$<num>",
      "<num> quotes/jobs/leads a month" attributed to the prospect.
  (b) Results / case-study claims: "shops I've fixed", "shops I have fixed",
      "we have helped N shops", "our clients saw", "proven results", etc.
      Both contracted (I've / we've) and uncontracted (I have / we have) forms
      are blocked. Both first-person-singular (I/my) and first-person-plural
      (we/our) are blocked.

ALLOWS:
  - Industry benchmarks phrased generally: "69% of buyers...", "53% of mobile visits..."
  - Mechanism descriptions that don't attribute a specific number to the prospect.

Usage:
  from outreach_guardrail import validate_text, GuardrailViolation

  validate_text(text, context="email body")   # raises GuardrailViolation on violation
  # or
  ok, reason = check_text(text)               # returns (True, None) or (False, reason_str)
"""

import re
from typing import Optional


class GuardrailViolation(ValueError):
    """Raised when text contains a blocked pattern."""
    def __init__(self, message: str, pattern_name: str, matched_text: str):
        super().__init__(message)
        self.pattern_name = pattern_name
        self.matched_text = matched_text


# ---------------------------------------------------------------------------
# BLOCKED PATTERN REGISTRY
# Each entry: (name, compiled_regex, description)
# ---------------------------------------------------------------------------

_BLOCKED_PATTERNS = [

    # (a) Per-shop quantified claims — "you're losing N", "$<num>", "<num> jobs/quotes/month"
    (
        "per_shop_losing_N",
        re.compile(
            r"\byou'?re?\s+losing\s+\$?[\d,]+",
            re.IGNORECASE,
        ),
        "Per-shop dollar/count loss claim ('you're losing N')",
    ),
    (
        "per_shop_dollar_amount",
        re.compile(
            # Matches "$1,200" or "$5k" or "$50,000" in a context that could be shop-specific.
            # We block any dollar amount that is specific (not a percentage or benchmark).
            # Allowlist note: "69% of buyers" etc. won't match this pattern.
            r"\$\s*[\d,]+(?:k|K|M)?(?:\s*(?:per\s+month|a\s+month|\/month|a\s+year|\/year|in\s+revenue|in\s+jobs|in\s+quotes))?\b",
            re.IGNORECASE,
        ),
        "Per-shop dollar amount",
    ),
    (
        "per_shop_N_quotes_month",
        re.compile(
            r"\b\d+\s*(?:quotes?|jobs?|leads?|inquiries?|rfqs?)\s*(?:a\s+month|per\s+month|\/month|each\s+month)",
            re.IGNORECASE,
        ),
        "Per-shop N quotes/jobs/leads a month claim",
    ),
    (
        "per_shop_N_per_year",
        re.compile(
            r"\b\d+\s*(?:quotes?|jobs?|leads?|inquiries?|rfqs?|contracts?)\s*(?:a\s+year|per\s+year|\/year)",
            re.IGNORECASE,
        ),
        "Per-shop N quotes/jobs/year claim",
    ),
    (
        "per_shop_losing_jobs",
        re.compile(
            r"\b(?:losing|miss(?:ing)?|leaking)\s+\d+\s*(?:quotes?|jobs?|leads?|inquiries?|rfqs?|customers?)",
            re.IGNORECASE,
        ),
        "Per-shop losing-N-jobs/quotes claim",
    ),

    # (b) Results / case-study claims
    # Pattern covers both contracted (I've / we've) and uncontracted (I have / we have)
    # forms, and both I/my (singular) and we/our (plural) subjects.
    (
        "shops_ive_or_have_fixed",
        re.compile(
            # Matches:
            #   "shops I've fixed"   "shops I have fixed"
            #   "shops I've worked with"  "shops I have worked on"
            #   "shops I've helped"  "shops I have helped"
            #   "shops I've done this for"  "shops I have done this for"
            # Singular subject only; plural (we) covered by we_got_X and clients_saw below.
            r"\bshops?\s+i(?:\s+have|'?ve)?\s+(?:fixed|worked\s+(?:with|on)|helped|done\s+this\s+for)",
            re.IGNORECASE,
        ),
        "Results claim: 'shops I've/I have fixed/worked with/helped'",
    ),
    (
        "we_got_X",
        re.compile(
            # Blocks "we got/achieved/generated..." AND "we have got/achieved..."
            # AND "we've got/achieved..."
            r"\bwe(?:\s+have|'?ve)?\s+(?:got|achieved|generated|produced|drove|increased|grew|helped|fixed)\b",
            re.IGNORECASE,
        ),
        "Results claim: 'we got/achieved/helped X' (contracted or uncontracted)",
    ),
    (
        "clients_saw",
        re.compile(
            # Blocks "clients saw", "shops saw", "customers got", etc.
            # Also: "our clients saw", "our shops saw"
            r"\b(?:our\s+)?(?:clients?|shops?|customers?)\s+(?:saw|experienced|reported|achieved|got|received)\b",
            re.IGNORECASE,
        ),
        "Results claim: 'clients/shops/customers saw/experienced/got X'",
    ),
    (
        "case_study",
        re.compile(
            r"\b(?:case\s+stud(?:y|ies)|case-stud(?:y|ies)|results\s+for|proof\s+of\s+results|track\s+record)\b",
            re.IGNORECASE,
        ),
        "Case study / results-for claim",
    ),
    (
        "we_increased",
        re.compile(
            r"\b(?:we(?:\s+have|'?ve)?\s+)?increased\s+(?:their|your|the)\s+(?:revenue|jobs?|quotes?|leads?|bookings?|traffic|conversions?)",
            re.IGNORECASE,
        ),
        "Results claim: 'increased their/your revenue/jobs' (contracted or uncontracted)",
    ),
    (
        "results_claim_percent",
        re.compile(
            # Block "X% more jobs/quotes/leads" attributed to a shop's result
            # but ALLOW benchmark phrasing like "69% of buyers..."
            r"\b\d+\s*%\s*more\s+(?:jobs?|quotes?|leads?|inquiries?|rfqs?|revenue)",
            re.IGNORECASE,
        ),
        "Per-result percentage improvement claim",
    ),
    (
        "proven_results",
        re.compile(
            # Blocks: "proven to increase quotes", "proven results", "proven track record"
            # "proven to get", "proven to help", "proven to grow"
            r"\bproven\s+(?:to\s+(?:increase|get|help|grow|generate|drive|boost)|results|track\s+record)",
            re.IGNORECASE,
        ),
        "Results claim: 'proven to increase/get/help/grow...' or 'proven results'",
    ),
    (
        "we_have_helped_N",
        re.compile(
            # Catches: "we have helped N shops", "we've helped N shops",
            #          "I have helped N shops", "I've helped N shops"
            # The word 'helped' + digit combination is the tell.
            r"\b(?:i|we)(?:\s+have|'?ve)?\s+helped\s+\d+",
            re.IGNORECASE,
        ),
        "Results claim: 'I/we have/I've/we've helped N [shops/clients/...]'",
    ),

]


def check_text(text: str) -> tuple[bool, Optional[str]]:
    """
    Check text against all blocked patterns.
    Returns (True, None) if clean, (False, reason_string) if blocked.
    Does NOT raise — use validate_text() if you want an exception.
    """
    if not text:
        return True, None

    for name, pattern, description in _BLOCKED_PATTERNS:
        m = pattern.search(text)
        if m:
            matched = m.group(0)
            reason = (
                f"GUARDRAIL VIOLATION [{name}]: {description}. "
                f"Matched: '{matched}' "
                f"(full context: '...{text[max(0,m.start()-30):m.end()+30]}...')"
            )
            return False, reason

    return True, None


def validate_text(text: str, context: str = "text") -> None:
    """
    Validate text against all blocked patterns.
    Raises GuardrailViolation (subclass of ValueError) if a violation is found.
    context: a label for error messages (e.g. "email body", "one_line_frame").
    """
    ok, reason = check_text(text)
    if not ok:
        # Extract the pattern name and matched text from the reason string
        name_match = re.search(r'\[([^\]]+)\]', reason)
        pattern_name = name_match.group(1) if name_match else "unknown"
        match_match = re.search(r"Matched: '([^']+)'", reason)
        matched_text = match_match.group(1) if match_match else ""
        raise GuardrailViolation(
            f"[{context}] {reason}",
            pattern_name=pattern_name,
            matched_text=matched_text,
        )


# Send-eligible email confidence tiers (lead-enrich-email.py)
_SEND_ELIGIBLE_EMAIL = frozenset({"verified", "risky"})


def check_email_sendable(lead: dict) -> tuple[bool, Optional[str]]:
    """
    Gate cold-email send on verified email enrichment.
    Returns (True, None) if the lead has a send-eligible email, else (False, reason).
    """
    conf = lead.get("email_confidence")
    email = lead.get("email")
    if not email:
        return False, "No email on lead record — run lead-enrich-email or route phone-first"
    if conf not in _SEND_ELIGIBLE_EMAIL:
        return False, (
            f"Email {email!r} is not send-eligible (confidence={conf!r}). "
            "Only verified|risky may be sent; guessed/unverified must be dropped."
        )
    if lead.get("email_source") == "pattern_inferred" and conf != "verified":
        return False, (
            f"Pattern-inferred email {email!r} without SMTP verification — dropped."
        )
    return True, None


def validate_email_sendable(lead: dict, context: str = "lead") -> None:
    """Raise GuardrailViolation if lead email is not safe to send."""
    ok, reason = check_email_sendable(lead)
    if not ok:
        raise GuardrailViolation(
            f"[{context}] {reason}",
            pattern_name="unverified_email",
            matched_text=lead.get("email") or "",
        )


def validate_outreach_dict(draft: dict, context: str = "draft") -> None:
    """
    Validate all text fields in a draft dict (subject, body, one_line_frame, etc.)
    Raises GuardrailViolation on first violation found.
    """
    for field, value in draft.items():
        if isinstance(value, str) and value:
            validate_text(value, context=f"{context}.{field}")


# ---------------------------------------------------------------------------
# SELF-TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== outreach_guardrail.py self-test ===")
    print()

    # These should PASS (industry benchmarks, general language)
    pass_cases = [
        "69% of B2B buyers abandon long forms.",
        "Industry data: ~90% of buyers research online first.",
        "53% of mobile visits are abandoned after 3 seconds.",
        "Engineers who've already chosen you move on.",
        "That's where quotes go cold.",
        "I can add a STEP/DWG upload path alongside your contact page.",
        "No pitch, no pressure — worth a quick look?",
        "An engineer who's chosen you has to call to send a drawing.",
        # Uncontracted benchmark phrases — must pass
        "69% of B2B buyers abandon forms that are too long.",
        "Approximately 90 percent of buyers research online before making contact.",
    ]

    print("--- PASS cases (should not raise) ---")
    all_passed_correctly = True
    for text in pass_cases:
        ok, reason = check_text(text)
        if ok:
            print(f"  [PASS] {text[:70]}")
        else:
            print(f"  [WRONGLY BLOCKED]: {text[:70]}")
            print(f"    reason: {reason[:100]}")
            all_passed_correctly = False

    print()

    # These should FAIL (fabricated/per-shop claims)
    # Includes both contracted and uncontracted forms, I and we variants.
    fail_cases = [
        # Original contracted forms
        ("you're losing $2,400 a month",                   "per_shop_dollar_amount"),
        ("you're losing 3 quotes a month",                  "per_shop_losing_N"),
        ("shops I've fixed see an immediate improvement",   "shops_ive_or_have_fixed"),
        ("we got 40% more RFQs for our client",             "we_got_X"),
        ("clients saw a 30% increase in leads",             "clients_saw"),
        ("here's a case study from a similar shop",         "case_study"),
        ("losing 5 jobs per month to this",                 "per_shop_losing_jobs"),
        ("results for a machine shop in Ohio",              "case_study"),
        ("we increased their revenue by 22%",               "we_increased"),
        ("40% more quotes after the fix",                   "results_claim_percent"),
        # NEW — uncontracted / alternate forms that were previously missed
        ("shops I have fixed see an immediate improvement", "shops_ive_or_have_fixed"),
        ("we have helped 12 shops just like yours",         "we_have_helped_N"),
        ("our clients saw a 30% lift in quote volume",      "clients_saw"),
        ("proven to increase quotes by 40%",                "proven_results"),
        # Additional uncontracted / plural forms
        ("we have helped shops close more jobs",            "we_got_X"),
        ("shops saw a big improvement after we rebuilt",    "clients_saw"),
        ("customers got more RFQs within 30 days",          "clients_saw"),
    ]

    print("--- FAIL cases (should be blocked) ---")
    all_failed_correctly = True
    for text, expected_pattern in fail_cases:
        ok, reason = check_text(text)
        if ok:
            print(f"  MISSED (should have blocked): {text[:70]}")
            all_failed_correctly = False
        else:
            print(f"  BLOCKED correctly: '{text[:60]}' → [{reason.split('[')[1].split(']')[0] if '[' in reason else '?'}]")

    print()
    if all_passed_correctly and all_failed_correctly:
        print("ALL SELF-TESTS PASSED")
    else:
        if not all_passed_correctly:
            print("SOME PASS CASES WERE WRONGLY BLOCKED — review patterns")
        if not all_failed_correctly:
            print("SOME FAIL CASES WERE NOT BLOCKED — review pattern coverage")

    # Email send-eligibility gate
    print()
    print("--- Email send-eligibility gate ---")
    ok, _ = check_email_sendable({"email": "a@b.com", "email_confidence": "verified"})
    assert ok, "verified should pass"
    ok, _ = check_email_sendable({"email": "a@b.com", "email_confidence": "risky"})
    assert ok, "risky should pass"
    ok, reason = check_email_sendable({"email": "guess@b.com", "email_confidence": "unverified"})
    assert not ok, "unverified should block"
    print("  email gate: verified/risky pass, unverified blocked — OK")
