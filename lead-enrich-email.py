#!/usr/bin/env python3
"""
lead-enrich-email.py — Email enrichment stage for the B2B outbound lead pipeline.

Finds + VERIFIES a contact email per worked lead using owned methods only:
  1. Lead's own site (mailto, contact/about pages, footer, JSON-LD)
  2. RDAP registrant email (low yield, often masked)
  3. Pattern inference (first.last@domain) — candidate only, never truth without SMTP

Every candidate is verified via MX lookup + SMTP RCPT probe with catch-all detection.

ENTRYPOINT:
  run_email_enrich(lead, *, store=None) -> dict

Writes back via lead_store.update_lead_fields:
  email, email_confidence (verified|risky|unverified|invalid),
  email_source (site_mailto|contact_page|jsonld|rdap|pattern_inferred),
  email_evidence (URL/substring proving the address)

ONLY verified|risky are send-eligible. No verified email → phone-first routing
(see draft-outreach.propose_param_set channel gate).

Self-test: python3 lead-enrich-email.py  (hermetic — zero live deps)
Batch:     python3 lead-enrich-email.py --batch [--limit N] [--live-smtp]
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

try:
    from lead_store import get_store, LeadStore
    _STORE_AVAILABLE = True
except ImportError:
    _STORE_AVAILABLE = False
    LeadStore = object  # type: ignore

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

EMAIL_CONFIDENCE = frozenset({"verified", "risky", "unverified", "invalid"})
EMAIL_SOURCES = frozenset({
    "site_mailto", "contact_page", "jsonld", "rdap", "pattern_inferred",
})
SEND_ELIGIBLE = frozenset({"verified", "risky"})

SITE_CACHE_DIR = Path(os.path.expanduser(
    os.environ.get(
        "SITE_CACHE_DIR",
        "./data/sites",
    )
))

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
JS_MARK = [
    "wix.com", "squarespace", "__next_data__", "enable javascript",
    "please enable js", "data-reactroot", "wixstatic",
]
CONTACT_SUBPATHS = [
    "contact", "about", "about-us", "capabilities", "services", "company",
    "shop", "work",
]
SITE_EVIDENCE_SOURCES = frozenset({"site_mailto", "contact_page", "jsonld"})

INSTANTLY_API_BASE = "https://api.instantly.ai/api/v2"
INSTANTLY_VERIFY_POLL_S = 2.0
INSTANTLY_VERIFY_MAX_POLLS = 8

EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9][a-zA-Z0-9._%+\-]*@[a-zA-Z0-9][a-zA-Z0-9.\-]*\.[a-zA-Z]{2,}\b"
)
MAILTO_RE = re.compile(r"mailto:([^\s\"'<>?#]+)", re.IGNORECASE)
JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

JUNK_LOCAL_PARTS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "abuse", "webmaster", "sentry", "wix", "example",
})
PLATFORM_DOMAINS = frozenset({
    "wixsite.com", "square.site", "wordpress.com", "godaddysites.com",
    "instagram.com", "facebook.com", "fb.com", "linkedin.com", "linktr.ee",
    "yelp.com", "google.com", "business.site", "wix.com", "sites.google.com",
})

# Stripped from lead/owner names before pattern inference (verifier finding #1)
CORPORATE_SUFFIXES = frozenset({
    "co", "company", "corp", "corporation", "inc", "llc", "ltd", "limited",
    "shop", "shops", "fabrication", "fab", "welding", "machine", "machining",
    "manufacturing", "mfg", "services", "service", "group", "enterprises",
    "industries", "industrial", "works", "steel", "metal", "metals", "tools",
    "tooling", "repair", "precision", "custom", "specialty", "specialties",
    "mobile", "plc", "contractors", "contractor", "systems", "system",
    "solutions", "international", "worldwide", "holdings", "partners",
})


def _is_platform_domain(domain: Optional[str]) -> bool:
    if not domain:
        return False
    d = domain.lower()
    return any(d == p or d.endswith(f".{p}") for p in PLATFORM_DOMAINS)

JUNK_DOMAINS = frozenset({
    "example.com", "email.com", "domain.com", "sentry.io", "wixpress.com",
    "godaddy.com", "squarespace.com", "schema.org",
}) | PLATFORM_DOMAINS

SMTP_TIMEOUT_S = 8
FETCH_TIMEOUT_S = 12
CACHE_TTL_DAYS = 30
MAX_VERIFY_CANDIDATES = 4  # cap SMTP probes per lead in batch mode


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class EmailCandidate:
    address: str
    source: str
    evidence: str
    priority: int  # lower = higher priority in waterfall


@dataclass
class SmtpVerifyResult:
    status: str  # verified | risky | unverified | invalid
    mx_host: Optional[str] = None
    smtp_code: Optional[int] = None
    catch_all: bool = False
    detail: str = ""


@dataclass
class MockSmtpBackend:
    """Hermetic SMTP/MX mock for self-test."""
    mx_by_domain: dict[str, list[str]] = field(default_factory=dict)
    accept: set[str] = field(default_factory=set)
    reject: set[str] = field(default_factory=set)
    catch_all_domains: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# SITE FETCH — delegates to lead-fetch-sites.py (shared cache + TLS fallback)
# ---------------------------------------------------------------------------

_SITE_FETCH_MOD: Any = None


def _site_fetch_module() -> Any:
    """Lazy-load lead-fetch-sites.py for fetch() + strip()."""
    global _SITE_FETCH_MOD
    if _SITE_FETCH_MOD is None:
        path = _SCRIPT_DIR / "lead-fetch-sites.py"
        spec = importlib.util.spec_from_file_location("lead_fetch_sites", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SITE_FETCH_MOD = mod
    return _SITE_FETCH_MOD


def _strip_html(html: str) -> str:
    return _site_fetch_module().strip(html)


def _fetch_url(url: str) -> tuple[Optional[int], str]:
    return _site_fetch_module().fetch(url)


def _read_site_cache(place_id: str) -> Optional[str]:
    path = SITE_CACHE_DIR / f"{place_id}.txt"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_cached_site_text(cached: str, website: str) -> list[dict]:
    """Split lead-fetch-sites cache format into page dicts for extraction."""
    if "=== SITE TEXT ===" not in cached:
        return [{"url": website, "html": "", "text": cached, "label": "cache"}]

    _, site_text = cached.split("=== SITE TEXT ===", 1)
    site_text = site_text.strip()
    if not site_text or site_text == "[empty]":
        return []

    pages: list[dict] = []
    chunks = re.split(r"\n\[PAGE (\w+)\]\n", site_text)
    home_text = chunks[0].strip()
    if home_text:
        pages.append({"url": website, "html": "", "text": home_text, "label": "home"})
    for i in range(1, len(chunks), 2):
        label = chunks[i]
        body = chunks[i + 1].strip() if i + 1 < len(chunks) else ""
        if body:
            sub_url = f"{website.rstrip('/')}/{label}"
            pages.append({"url": sub_url, "html": "", "text": body, "label": label})
    return pages or [{"url": website, "html": "", "text": site_text, "label": "cache"}]


def fetch_site_pages(
    website: str,
    *,
    place_id: str = "",
    use_cache: bool = True,
    live_fetch: bool = True,
    subpaths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Return {pages: [{url, html, text, label}], fetch_status, from_cache}.
    Reuses lead-fetch-sites.py cache when place_id matches a cached file.
    """
    if use_cache and place_id:
        cached = _read_site_cache(place_id)
        if cached and "=== SITE TEXT ===" in cached:
            return {
                "pages": _parse_cached_site_text(cached, website),
                "fetch_status": "ok:cache",
                "from_cache": True,
            }

    if not website or not live_fetch:
        return {"pages": [], "fetch_status": "no_website", "from_cache": False}

    base = website.rstrip("/")
    code, body = _fetch_url(base)
    pages: list[dict] = []

    if code == 200 and body and not body.startswith("ERR"):
        pages.append({"url": base, "html": body, "text": _strip_html(body), "label": "home"})
        raw_low = body.lower()
        paths = subpaths if subpaths is not None else CONTACT_SUBPATHS
        for sub in paths:
            sub_url = f"{base}/{sub}"
            c2, b2 = _fetch_url(sub_url)
            if c2 == 200 and b2 and not b2.startswith("ERR"):
                pages.append({
                    "url": sub_url,
                    "html": b2,
                    "text": _strip_html(b2),
                    "label": sub,
                })
        text_len = sum(len(p["text"]) for p in pages)
        render = "js_shell" if (
            text_len < 400 or (any(m in raw_low for m in JS_MARK) and text_len < 1200)
        ) else "static"
        status = f"ok:{render}"
    else:
        status = f"fetch_fail({code})"

    return {"pages": pages, "fetch_status": status, "from_cache": False}


# ---------------------------------------------------------------------------
# EMAIL EXTRACTION
# ---------------------------------------------------------------------------

def _normalize_email(raw: str) -> Optional[str]:
    addr = raw.strip().lower()
    addr = addr.split("?")[0]  # mailto query strip
    if not EMAIL_RE.fullmatch(addr):
        m = EMAIL_RE.search(addr)
        if not m:
            return None
        addr = m.group(0).lower()
    local, _, domain = addr.partition("@")
    if local in JUNK_LOCAL_PARTS or domain in JUNK_DOMAINS:
        return None
    if any(x in domain for x in (".png", ".jpg", ".gif", ".webp")):
        return None
    return addr


def _domain_from_website(website: str) -> Optional[str]:
    if not website:
        return None
    host = urlparse.urlparse(website).hostname or ""
    host = host.lower().removeprefix("www.")
    return host or None


def _email_matches_domain(email: str, domain: Optional[str]) -> bool:
    if not domain:
        return True
    return email.endswith(f"@{domain}") or email.endswith(f".{domain}")


def _walk_jsonld_for_email(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == "email" and isinstance(v, str):
                out.append(v)
            else:
                _walk_jsonld_for_email(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_jsonld_for_email(item, out)


def extract_candidates_from_site(
    pages: list[dict],
    domain: Optional[str],
) -> list[EmailCandidate]:
    """Waterfall tier 1 — site-sourced emails with evidence."""
    seen: set[str] = set()
    candidates: list[EmailCandidate] = []

    def add(addr: str, source: str, evidence: str, priority: int) -> None:
        norm = _normalize_email(addr)
        if not norm or norm in seen:
            return
        # Site-sourced emails: accept if evidenced on page (mailto/jsonld/text).
        # Pattern tier applies domain filter separately.
        if source == "pattern_inferred" and domain and not _email_matches_domain(norm, domain):
            return
        if source != "pattern_inferred" and domain and not _email_matches_domain(norm, domain):
            # Still allow if it's a custom domain on a platform page (e.g. info@shop.com on wix)
            if _is_platform_domain(domain):
                pass  # accept cross-domain emails found on platform-hosted site
            else:
                return
        seen.add(norm)
        candidates.append(EmailCandidate(norm, source, evidence, priority))

    for page in pages:
        url = page.get("url", "")
        html = page.get("html") or ""
        text = page.get("text") or ""
        label = page.get("label", "")

        for m in MAILTO_RE.findall(html):
            add(m, "site_mailto", f"mailto:{m} @ {url}", 1)
        for m in MAILTO_RE.findall(text):
            add(m, "site_mailto", f"mailto:{m} in text @ {url or label}", 1)

        for block in JSONLD_RE.findall(html):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            emails: list[str] = []
            _walk_jsonld_for_email(data, emails)
            for e in emails:
                add(e, "jsonld", f'jsonld:"{e}" @ {url}', 2)

        source_label = "contact_page" if label in CONTACT_SUBPATHS else "contact_page"
        priority = 3 if label in CONTACT_SUBPATHS else 4
        for m in EMAIL_RE.findall(text):
            add(m, source_label, f'"{m}" in page text @ {url or label}', priority)

    candidates.sort(key=lambda c: c.priority)
    return candidates


def fetch_rdap_email(domain: str, *, live: bool = True) -> Optional[EmailCandidate]:
    """Waterfall tier 2 — RDAP registrant email."""
    if not domain or not live:
        return None
    url = f"https://rdap.org/domain/{domain}"
    try:
        req = urlrequest.Request(url, headers={**UA, "Accept": "application/rdap+json"})
        with urlrequest.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    for entity in data.get("entities") or []:
        roles = {r.lower() for r in (entity.get("roles") or [])}
        if not roles & {"registrant", "administrative", "technical"}:
            continue
        vcard = entity.get("vcardArray")
        if not vcard or len(vcard) < 2:
            continue
        for row in vcard[1]:
            if len(row) >= 4 and str(row[0]).lower() == "email":
                val = row[3] if isinstance(row[3], str) else str(row[3])
                norm = _normalize_email(val)
                if norm:
                    return EmailCandidate(
                        norm, "rdap",
                        f'rdap.org/domain/{domain} vcard email:"{norm}"',
                        10,
                    )
    return None


def _parse_name_tokens(raw: str) -> list[str]:
    """Person/company tokens for pattern inference — strips corporate suffix noise."""
    cleaned = re.sub(r"[^a-zA-Z\s]", " ", raw or "")
    parts = [p.lower() for p in cleaned.split() if len(p) > 1]
    while parts and parts[-1] in CORPORATE_SUFFIXES:
        parts.pop()
    while parts and parts[0] in CORPORATE_SUFFIXES:
        parts.pop(0)
    return parts


def infer_pattern_candidates(
    owner_name: Optional[str],
    lead_name: str,
    domain: Optional[str],
) -> list[EmailCandidate]:
    """Waterfall tier 3 — pattern inference (candidate only)."""
    if not domain or _is_platform_domain(domain):
        return []

    tokens: list[str] = []
    if owner_name:
        tokens = _parse_name_tokens(owner_name)
    if len(tokens) < 2:
        lead_tokens = _parse_name_tokens(lead_name)
        if lead_tokens:
            tokens = lead_tokens

    patterns: list[str] = []
    if tokens:
        first = tokens[0]
        # Only use a last-name token if it looks like a person name, not industry text
        last = tokens[1] if len(tokens) > 1 and tokens[1] not in CORPORATE_SUFFIXES else None
        if last:
            patterns.extend([
                f"{first}.{last}@{domain}",
                f"{first}{last}@{domain}",
                f"{first[0]}{last}@{domain}",
                f"{first}@{domain}",
            ])
        else:
            patterns.append(f"{first}@{domain}")
    patterns.append(f"info@{domain}")
    patterns.append(f"sales@{domain}")

    out: list[EmailCandidate] = []
    seen: set[str] = set()
    for p in patterns:
        norm = _normalize_email(p)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(EmailCandidate(
                norm, "pattern_inferred",
                f"pattern:{p} (not evidenced — SMTP required)",
                20,
            ))
    return out


# ---------------------------------------------------------------------------
# MX + SMTP VERIFICATION
# ---------------------------------------------------------------------------

def lookup_mx(
    domain: str,
    *,
    mock: Optional[MockSmtpBackend] = None,
) -> list[str]:
    if mock and domain in mock.mx_by_domain:
        return mock.mx_by_domain[domain]
    try:
        proc = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=8,
        )
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception:
        return []

    scored: list[tuple[int, str]] = []
    for ln in lines:
        parts = ln.split()
        if len(parts) >= 2 and parts[1].endswith("."):
            try:
                prio = int(parts[0])
            except ValueError:
                prio = 10
            host = parts[1].rstrip(".")
            scored.append((prio, host))
    scored.sort(key=lambda x: x[0])
    return [h for _, h in scored]


def _smtp_rcpt_probe(
    email: str,
    mx_host: str,
    *,
    mock: Optional[MockSmtpBackend] = None,
) -> SmtpVerifyResult:
    domain = email.split("@", 1)[1]
    bogus = f"nonexist-{uuid.uuid4().hex[:12]}@{domain}"

    if mock is not None:
        if domain in mock.catch_all_domains:
            if email.lower() in {a.lower() for a in mock.accept}:
                return SmtpVerifyResult("risky", mx_host, 250, True, "mock:catch-all accepts")
            if email.lower() in {r.lower() for r in mock.reject}:
                return SmtpVerifyResult("invalid", mx_host, 550, True, "mock:rejected")
            return SmtpVerifyResult("risky", mx_host, 250, True, "mock:catch-all domain")
        if email.lower() in {a.lower() for a in mock.accept}:
            return SmtpVerifyResult("verified", mx_host, 250, False, "mock:accepted")
        if email.lower() in {r.lower() for r in mock.reject}:
            return SmtpVerifyResult("invalid", mx_host, 550, False, "mock:rejected")
        return SmtpVerifyResult("unverified", mx_host, None, False, "mock:inconclusive")

    try:
        sock = socket.create_connection((mx_host, 25), timeout=SMTP_TIMEOUT_S)
    except Exception as e:
        return SmtpVerifyResult("unverified", mx_host, None, False, f"connect:{type(e).__name__}")

    lines: list[str] = []

    def readline() -> str:
        sock.settimeout(SMTP_TIMEOUT_S)
        data = sock.recv(4096)
        line = data.decode("utf-8", errors="replace")
        lines.append(line)
        return line

    def send(cmd: str) -> tuple[int, str]:
        sock.sendall((cmd + "\r\n").encode("ascii", errors="ignore"))
        line = readline()
        try:
            code = int(line[:3])
        except (ValueError, IndexError):
            code = 0
        return code, line

    try:
        readline()  # banner
        send(f"EHLO mailverify.example")
        send("MAIL FROM:<verify@example.com>")
        code, _ = send(f"RCPT TO:<{email}>")
        catch_all = False
        if code in (250, 251):
            bogus_code, _ = send(f"RCPT TO:<{bogus}>")
            catch_all = bogus_code in (250, 251)
        send("QUIT")
    except Exception as e:
        try:
            sock.close()
        except Exception:
            pass
        return SmtpVerifyResult("unverified", mx_host, None, False, f"smtp:{type(e).__name__}")
    finally:
        try:
            sock.close()
        except Exception:
            pass

    if code in (550, 551, 553, 552):
        return SmtpVerifyResult("invalid", mx_host, code, catch_all, "rcpt-rejected")
    if code in (250, 251):
        if catch_all:
            return SmtpVerifyResult("risky", mx_host, code, True, "catch-all")
        return SmtpVerifyResult("verified", mx_host, code, False, "rcpt-accepted")
    if 400 <= code < 500:
        return SmtpVerifyResult("unverified", mx_host, code, catch_all, "temp-fail")
    return SmtpVerifyResult("unverified", mx_host, code, catch_all, f"smtp-code-{code}")


_smtp_port_open: Optional[bool] = None


def smtp_port_reachable(timeout: float = 4.0) -> bool:
    """One-time probe — many networks block outbound :25."""
    global _smtp_port_open
    if _smtp_port_open is not None:
        return _smtp_port_open
    try:
        sock = socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=timeout)
        sock.close()
        _smtp_port_open = True
    except Exception:
        _smtp_port_open = False
    return _smtp_port_open


def verify_email(
    email: str,
    *,
    mock: Optional[MockSmtpBackend] = None,
    live_smtp: bool = True,
) -> SmtpVerifyResult:
    domain = email.split("@", 1)[1]
    mx_hosts = lookup_mx(domain, mock=mock)
    if not mx_hosts:
        return SmtpVerifyResult("unverified", None, None, False, "no-mx")

    if not live_smtp and mock is None:
        return SmtpVerifyResult("unverified", mx_hosts[0], None, False, "smtp-disabled")

    if mock is None and not smtp_port_reachable():
        return SmtpVerifyResult(
            "unverified", mx_hosts[0], None, False, "smtp-port-25-blocked",
        )

    last = SmtpVerifyResult("unverified", None, None, False, "no-mx-response")
    for mx in mx_hosts[:3]:
        result = _smtp_rcpt_probe(email, mx, mock=mock)
        if result.status in ("verified", "risky", "invalid"):
            return result
        last = result
    return last


# ---------------------------------------------------------------------------
# WATERFALL ORCHESTRATION
# ---------------------------------------------------------------------------

def collect_candidates(
    lead: dict,
    *,
    site_bundle: Optional[dict] = None,
    live_rdap: bool = True,
    include_patterns: bool = True,
) -> list[EmailCandidate]:
    website = lead.get("website") or ""
    domain = _domain_from_website(website)
    pages = (site_bundle or {}).get("pages") or []

    candidates = extract_candidates_from_site(pages, domain)

    rdap = fetch_rdap_email(domain, live=live_rdap) if domain else None
    if rdap:
        candidates.append(rdap)

    if include_patterns:
        candidates.extend(infer_pattern_candidates(
            lead.get("owner_name"),
            lead.get("name") or "",
            domain,
        ))

    # Dedupe by address, keep highest-priority (lowest number) source
    by_addr: dict[str, EmailCandidate] = {}
    for c in candidates:
        prev = by_addr.get(c.address)
        if prev is None or c.priority < prev.priority:
            by_addr[c.address] = c
    return sorted(by_addr.values(), key=lambda c: c.priority)


def enrich_email_for_lead(
    lead: dict,
    *,
    live_fetch: bool = True,
    live_rdap: bool = True,
    live_smtp: bool = True,
    mock_smtp: Optional[MockSmtpBackend] = None,
    site_bundle: Optional[dict] = None,
    max_verify_candidates: int = MAX_VERIFY_CANDIDATES,
    fetch_subpaths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Run source → verify waterfall for one lead.
    Returns enrichment result dict (does not mutate lead).
    """
    website = lead.get("website") or ""
    place_id = lead.get("place_id") or ""

    if site_bundle is None:
        site_bundle = fetch_site_pages(
            website, place_id=place_id, live_fetch=live_fetch,
            subpaths=fetch_subpaths,
        )

    candidates = collect_candidates(
        lead, site_bundle=site_bundle, live_rdap=live_rdap,
    )
    if max_verify_candidates > 0:
        candidates = candidates[:max_verify_candidates]

    verification_log: list[dict] = []
    chosen: Optional[dict] = None

    for cand in candidates:
        # Pattern candidates require SMTP confirmation — never truth without it
        smtp = verify_email(cand.address, mock=mock_smtp, live_smtp=live_smtp)
        entry = {
            "address": cand.address,
            "source": cand.source,
            "evidence": cand.evidence,
            "smtp_status": smtp.status,
            "smtp_detail": smtp.detail,
            "catch_all": smtp.catch_all,
            "mx_host": smtp.mx_host,
        }
        verification_log.append(entry)

        if smtp.status in SEND_ELIGIBLE:
            chosen = {
                "email": cand.address,
                "email_confidence": smtp.status,
                "email_source": cand.source,
                "email_evidence": cand.evidence,
            }
            break

        # Evidenced site/rdap with invalid SMTP — record but keep trying
        if smtp.status == "invalid" and cand.source != "pattern_inferred":
            if chosen is None:
                chosen = {
                    "email": None,
                    "email_confidence": "invalid",
                    "email_source": cand.source,
                    "email_evidence": f"{cand.evidence} (SMTP rejected)",
                }

    if chosen is None:
        # FLOOR — honest absence
        if candidates:
            best = candidates[0]
            last = verification_log[-1] if verification_log else {}
            chosen = {
                "email": None,
                "email_confidence": last.get("smtp_status", "unverified"),
                "email_source": best.source,
                "email_evidence": (
                    f"No send-eligible email. Best candidate {best.address}: "
                    f"{last.get('smtp_detail', 'unverified')}"
                ),
            }
        else:
            chosen = {
                "email": None,
                "email_confidence": None,
                "email_source": None,
                "email_evidence": "No email candidates found on site, RDAP, or patterns",
            }

    return {
        "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "website": website,
        "fetch_status": site_bundle.get("fetch_status"),
        "candidates_found": len(candidates),
        "verification_log": verification_log,
        **chosen,
        "send_eligible": chosen.get("email_confidence") in SEND_ELIGIBLE,
    }


def _is_cache_valid(lead: dict, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    enrich = (lead.get("audit") or {}).get("email_enrich") or {}
    ran_at = enrich.get("ran_at")
    conf = lead.get("email_confidence")
    if not ran_at or conf not in SEND_ELIGIBLE:
        return False
    try:
        ts = datetime.datetime.fromisoformat(ran_at.replace("Z", "+00:00"))
        age = datetime.datetime.now(datetime.timezone.utc) - ts
        return age.days < ttl_days
    except Exception:
        return False


def run_email_enrich(
    lead: dict,
    *,
    store: Optional[Any] = None,
    config: Optional[dict] = None,
    _mock_site_pages: Optional[list[dict]] = None,
    _mock_smtp: Optional[MockSmtpBackend] = None,
) -> dict:
    """
    Lazy per-lead email enrichment. Writes email fields + audit.email_enrich.
    """
    cfg = {
        "live_fetch": True,
        "live_rdap": True,
        "live_smtp": True,
        "force": False,
        "fetch_subpaths": None,
        "max_verify_candidates": MAX_VERIFY_CANDIDATES,
    }
    if config:
        cfg.update(config)

    if not cfg.get("force") and _is_cache_valid(lead):
        return lead

    website = lead.get("website") or ""
    if not website:
        result = {
            "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "website": "",
            "fetch_status": "no_website",
            "email": None,
            "email_confidence": None,
            "email_source": None,
            "email_evidence": "No website — phone-first",
            "send_eligible": False,
            "candidates_found": 0,
            "verification_log": [],
        }
    else:
        site_bundle = None
        if _mock_site_pages is not None:
            site_bundle = {"pages": _mock_site_pages, "fetch_status": "mocked", "from_cache": False}
        result = enrich_email_for_lead(
            lead,
            live_fetch=cfg["live_fetch"] and _mock_site_pages is None,
            live_rdap=cfg["live_rdap"] and _mock_smtp is None,
            live_smtp=cfg["live_smtp"],
            mock_smtp=_mock_smtp,
            site_bundle=site_bundle,
            max_verify_candidates=cfg.get("max_verify_candidates", MAX_VERIFY_CANDIDATES),
            fetch_subpaths=cfg.get("fetch_subpaths"),
        )

    lead = dict(lead)
    audit = dict(lead.get("audit") or {})
    audit["email_enrich"] = result
    lead["audit"] = audit

    for field in ("email", "email_confidence", "email_source", "email_evidence"):
        lead[field] = result.get(field)

    if not result.get("send_eligible") and lead.get("phone"):
        lead["next_action"] = "phone_first"

    if store is not None and _STORE_AVAILABLE:
        lead_id = lead.get("id")
        if lead_id:
            try:
                store.update_lead_fields(
                    lead_id,
                    email=lead.get("email"),
                    email_confidence=lead.get("email_confidence"),
                    email_source=lead.get("email_source"),
                    email_evidence=lead.get("email_evidence"),
                    next_action=lead.get("next_action"),
                    audit=audit,
                )
            except Exception as e:
                result["store_write_error"] = str(e)[:120]

    return lead


# ---------------------------------------------------------------------------
# BATCH CLI
# ---------------------------------------------------------------------------

def run_batch(
    store: Optional[LeadStore],
    *,
    limit: Optional[int] = None,
    offset: int = 0,
    wedge_filter: Optional[str] = None,
    live_smtp: bool = True,
    force: bool = False,
    shard_path: Optional[Path] = None,
) -> dict:
    if store is None and shard_path is None:
        raise ValueError("run_batch requires store or shard_path")

    leads = store.list_leads() if store is not None else _load_cohort_from_disk()
    # has-site cohort: website present, not suppressed — stable sort for sharding
    cohort = sorted(
        [l for l in leads if l.get("website") and not l.get("suppressed")],
        key=lambda l: l.get("place_id") or "",
    )
    if wedge_filter:
        cohort = [l for l in cohort if l.get("wedge") == wedge_filter]
    if offset:
        cohort = cohort[offset:]
    if limit is not None:
        cohort = cohort[:limit]

    counts = {"verified": 0, "risky": 0, "unverified": 0, "invalid": 0, "none": 0}
    shard_rows: dict[str, dict] = {}
    write_store = store if shard_path is None else None

    for i, lead in enumerate(cohort, 1):
        print(f"  [{i}/{len(cohort)}] {lead.get('name', '')[:40]}", flush=True)
        updated = run_email_enrich(
            lead, store=write_store,
            config={
                "live_smtp": live_smtp,
                "force": force,
                "live_rdap": False,
                "fetch_subpaths": None,  # CONTACT_SUBPATHS default
            },
        )
        conf = updated.get("email_confidence")
        if conf in counts:
            counts[conf] += 1
        else:
            counts["none"] += 1

        if shard_path is not None:
            pid = updated.get("place_id") or lead.get("place_id")
            if pid:
                shard_rows[pid] = {
                    "place_id": pid,
                    "id": updated.get("id") or lead.get("id"),
                    "email": updated.get("email"),
                    "email_confidence": updated.get("email_confidence"),
                    "email_source": updated.get("email_source"),
                    "email_evidence": updated.get("email_evidence"),
                    "next_action": updated.get("next_action"),
                    "audit": updated.get("audit"),
                }

    if shard_path is not None:
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_text(json.dumps(shard_rows, indent=2, ensure_ascii=False))
        print(f"Wrote shard: {shard_path} ({len(shard_rows)} leads)")

    return {"processed": len(cohort), "counts": counts, "shard_path": str(shard_path) if shard_path else None}


def _load_cohort_from_disk() -> list[dict]:
    path = Path(os.path.expanduser(
        os.environ.get(
            "LOCAL_STORE_DIR",
            "./data/engine-store",
        )
    )) / "pipeline_leads.json"
    return json.loads(path.read_text(encoding="utf-8"))


def merge_shards(shard_dir: Path, store: LeadStore) -> dict:
    """Apply parallel batch shard files into pipeline_leads (single-writer merge)."""
    merged = 0
    for shard_file in sorted(shard_dir.glob("shard_*.json")):
        rows = json.loads(shard_file.read_text(encoding="utf-8"))
        for pid, patch in rows.items():
            lead_id = patch.get("id")
            if not lead_id:
                lead = store.get_lead(place_id=pid)
                lead_id = lead.get("id") if lead else None
            if not lead_id:
                print(f"  skip {pid}: no lead id", flush=True)
                continue
            store.update_lead_fields(
                lead_id,
                email=patch.get("email"),
                email_confidence=patch.get("email_confidence"),
                email_source=patch.get("email_source"),
                email_evidence=patch.get("email_evidence"),
                next_action=patch.get("next_action"),
                audit=patch.get("audit"),
            )
            merged += 1
    return {"merged": merged, "shard_dir": str(shard_dir)}


# ---------------------------------------------------------------------------
# INSTANTLY VERIFY (founder-gated fallback — verify-only, not SuperSearch)
# ---------------------------------------------------------------------------

def _instantly_api_key() -> str:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if key:
        return key
    # Optional: ~/.claude/gtm/.env (same path as push_to_instantly.py)
    env_path = Path.home() / ".claude" / "gtm" / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("INSTANTLY_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return ""


def map_instantly_result(result: dict) -> tuple[str, bool]:
    """Map Instantly verification_status + catch_all → our confidence tier."""
    status = (result.get("verification_status") or "").lower()
    catch_raw = result.get("catch_all")
    catch_all = catch_raw is True or str(catch_raw).lower() == "true"
    if status == "verified":
        return ("risky" if catch_all else "verified"), catch_all
    if status == "invalid":
        return "invalid", catch_all
    return "unverified", catch_all


def _instantly_request(
    method: str,
    path: str,
    api_key: str,
    payload: Optional[dict] = None,
) -> dict:
    """HTTP to Instantly v2 — curl avoids Cloudflare 1010 blocks on urllib."""
    url = f"{INSTANTLY_API_BASE}{path}"
    cmd = [
        "curl", "-sS", "-X", method, url,
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-w", "\n__HTTP__%{http_code}",
    ]
    if payload is not None:
        cmd.extend(["-d", json.dumps(payload)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except Exception as e:
        raise RuntimeError(f"Instantly request failed: {e}") from e

    raw = proc.stdout or ""
    if "\n__HTTP__" in raw:
        body, _, code_str = raw.rpartition("\n__HTTP__")
        http_code = int(code_str.strip() or "0")
    else:
        body, http_code = raw, 0

    if proc.returncode != 0:
        raise RuntimeError(f"curl failed ({proc.returncode}): {(proc.stderr or body)[:200]}")

    try:
        detail = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        detail = {"message": body[:200]}

    if http_code >= 400:
        raise RuntimeError(f"Instantly HTTP {http_code}: {detail}")
    return detail


def instantly_verify_email(email: str, api_key: str) -> dict:
    """POST verify, poll GET if pending. Returns Instantly EmailVerification object."""
    result = _instantly_request("POST", "/email-verification", api_key, {"email": email})
    status = (result.get("verification_status") or "").lower()
    if status != "pending":
        return result

    encoded = urlparse.quote(email, safe="")
    for _ in range(INSTANTLY_VERIFY_MAX_POLLS):
        time.sleep(INSTANTLY_VERIFY_POLL_S)
        polled = _instantly_request("GET", f"/email-verification/{encoded}", api_key)
        if (polled.get("verification_status") or "").lower() != "pending":
            return polled
    return result


def collect_site_sourced_candidates(leads: list[dict]) -> list[dict]:
    """Best site-sourced email per has-site lead (for Instantly verify queue)."""
    out: list[dict] = []
    for lead in leads:
        if not lead.get("website") or lead.get("suppressed"):
            continue
        log = ((lead.get("audit") or {}).get("email_enrich") or {}).get("verification_log", [])
        for entry in log:
            if entry.get("source") in SITE_EVIDENCE_SOURCES:
                out.append({
                    "place_id": lead.get("place_id"),
                    "id": lead.get("id"),
                    "name": lead.get("name"),
                    "email": entry.get("address"),
                    "source": entry.get("source"),
                    "evidence": entry.get("evidence"),
                })
                break
    return out


def apply_instantly_verify_to_lead(lead: dict, candidate: dict, instantly: dict) -> dict:
    """Merge Instantly result into lead; promote email only on verified|risky."""
    conf, catch_all = map_instantly_result(instantly)
    email = candidate["email"]
    lead = dict(lead)
    audit = dict(lead.get("audit") or {})
    enrich = dict(audit.get("email_enrich") or {})

    instantly_block = {
        "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "email": email,
        "verification_status": instantly.get("verification_status"),
        "catch_all": instantly.get("catch_all"),
        "credits_used": instantly.get("credits_used"),
        "credits_remaining": instantly.get("credits"),
        "mapped_confidence": conf,
        "provider": "instantly",
    }
    enrich["instantly_verify"] = instantly_block

    log = list(enrich.get("verification_log") or [])
    for entry in log:
        if entry.get("address") == email:
            entry["smtp_status"] = conf
            entry["smtp_detail"] = f"instantly:{instantly.get('verification_status')}"
            entry["catch_all"] = catch_all
            entry["verify_provider"] = "instantly"
            break

    enrich["verification_log"] = log
    send_eligible = conf in SEND_ELIGIBLE
    enrich["send_eligible"] = send_eligible

    audit["email_enrich"] = enrich
    lead["audit"] = audit

    if send_eligible:
        lead["email"] = email
        lead["email_confidence"] = conf
        lead["email_source"] = candidate.get("source")
        lead["email_evidence"] = (
            f"{candidate.get('evidence')} (Instantly {instantly.get('verification_status')}"
            f"{', catch-all' if catch_all else ''})"
        )
    elif conf == "invalid":
        lead["email"] = None
        lead["email_confidence"] = "invalid"
        lead["email_source"] = candidate.get("source")
        lead["email_evidence"] = f"{candidate.get('evidence')} (Instantly invalid)"
    else:
        lead["email"] = None
        lead["email_confidence"] = "unverified"
        lead["email_source"] = candidate.get("source")
        lead["email_evidence"] = (
            f"No send-eligible email. Site candidate {email}: "
            f"instantly:{instantly.get('verification_status')}"
        )

    if not send_eligible and lead.get("phone"):
        lead["next_action"] = "phone_first"

    return lead


def run_instantly_verify_batch(
    store: LeadStore,
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
    sleep_s: float = 0.35,
) -> dict:
    """
    Verify site-sourced email candidates via Instantly API (verify-only fallback).
    Founder-gated: requires INSTANTLY_API_KEY or 1Password credential.
    """
    api_key = _instantly_api_key()
    if not api_key and not dry_run:
        raise RuntimeError(
            "INSTANTLY_API_KEY not set. Add to env or your secrets manager or .env"
        )

    leads = store.list_leads()
    candidates = collect_site_sourced_candidates(leads)
    if limit is not None:
        candidates = candidates[:limit]

    counts = {"verified": 0, "risky": 0, "unverified": 0, "invalid": 0, "error": 0}
    credits_used = 0

    if dry_run:
        print(f"DRY RUN — would verify {len(candidates)} site-sourced emails via Instantly")
        for i, c in enumerate(candidates[:5], 1):
            print(f"  [{i}] {c.get('name', '')[:35]:35} {c['email']}")
        if len(candidates) > 5:
            print(f"  ... +{len(candidates) - 5} more")
        return {"processed": 0, "queued": len(candidates), "counts": counts, "dry_run": True}

    lead_by_pid = {l["place_id"]: l for l in leads if l.get("place_id")}

    for i, cand in enumerate(candidates, 1):
        email = cand["email"]
        print(f"  [{i}/{len(candidates)}] {cand.get('name', '')[:35]:35} {email}", flush=True)
        try:
            result = instantly_verify_email(email, api_key)
        except Exception as e:
            counts["error"] += 1
            print(f"    ERROR: {e}", flush=True)
            continue

        conf, _ = map_instantly_result(result)
        if conf in counts:
            counts[conf] += 1
        used = result.get("credits_used")
        if isinstance(used, (int, float)):
            credits_used += int(used)

        pid = cand.get("place_id")
        lead = lead_by_pid.get(pid)
        if not lead or not lead.get("id"):
            continue
        updated = apply_instantly_verify_to_lead(lead, cand, result)
        store.update_lead_fields(
            lead["id"],
            email=updated.get("email"),
            email_confidence=updated.get("email_confidence"),
            email_source=updated.get("email_source"),
            email_evidence=updated.get("email_evidence"),
            next_action=updated.get("next_action"),
            audit=updated.get("audit"),
        )
        lead_by_pid[pid] = updated
        print(f"    → {conf} (instantly:{result.get('verification_status')})", flush=True)
        if sleep_s > 0:
            time.sleep(sleep_s)

    return {
        "processed": len(candidates) - counts["error"],
        "queued": len(candidates),
        "counts": counts,
        "credits_used": credits_used,
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# SELF-TEST (hermetic)
# ---------------------------------------------------------------------------

def _run_self_test() -> None:
    PASS, FAIL = "PASS", "FAIL"
    failures: list[str] = []

    domain = "testshop.example"
    mock = MockSmtpBackend(
        mx_by_domain={domain: ["mx.testshop.example"]},
        accept={f"info@{domain}", f"jane.doe@{domain}"},
        reject={f"bad@{domain}"},
        catch_all_domains={"catchall.example"},
    )
    mock.mx_by_domain["catchall.example"] = ["mx.catchall.example"]
    mock.accept.add("anything@catchall.example")

    html = f'''
    <html><body>
    <a href="mailto:info@{domain}">Email us</a>
    <script type="application/ld+json">
    {{"@type":"Organization","email":"info@{domain}"}}
    </script>
    <footer>Reach us at sales@{domain} for quotes.</footer>
    </body></html>
    '''

    # T1: site mailto → verified
    print("\n[T1] site mailto → verified (mock SMTP)")
    lead = {"place_id": "t1", "name": "Test Shop", "website": f"https://{domain}"}
    pages = [{"url": f"https://{domain}", "html": html, "text": _strip_html(html), "label": "home"}]
    r = run_email_enrich(lead, _mock_site_pages=pages, _mock_smtp=mock)
    t1_ok = r.get("email") == f"info@{domain}" and r.get("email_confidence") == "verified"
    print(f"  email={r.get('email')} confidence={r.get('email_confidence')}")
    print(f"  {PASS if t1_ok else FAIL} T1")
    if not t1_ok:
        failures.append("T1")

    # T2: pattern only — must not verify without SMTP accept
    print("\n[T2] pattern candidate rejected → no send-eligible email")
    mock2 = MockSmtpBackend(
        mx_by_domain={domain: ["mx.testshop.example"]},
        reject={f"jane.doe@{domain}"},
    )
    lead2 = {"place_id": "t2", "name": "Jane Doe Fab", "website": f"https://{domain}", "owner_name": "Jane Doe"}
    empty_pages: list[dict] = []
    r2 = run_email_enrich(lead2, _mock_site_pages=empty_pages, _mock_smtp=mock2)
    t2_ok = r2.get("email_confidence") not in SEND_ELIGIBLE or r2.get("email") is None
    print(f"  confidence={r2.get('email_confidence')} email={r2.get('email')}")
    print(f"  {PASS if t2_ok else FAIL} T2")
    if not t2_ok:
        failures.append("T2")

    # T3: catch-all → risky not verified
    print("\n[T3] catch-all domain → risky (not verified)")
    ca_domain = "catchall.example"
    html3 = f'<a href="mailto:owner@{ca_domain}">contact</a>'
    lead3 = {"place_id": "t3", "name": "Catchall Shop", "website": f"https://{ca_domain}"}
    pages3 = [{"url": f"https://{ca_domain}", "html": html3, "text": html3, "label": "home"}]
    r3 = run_email_enrich(lead3, _mock_site_pages=pages3, _mock_smtp=mock)
    t3_ok = r3.get("email_confidence") == "risky" and r3.get("email_confidence") != "verified"
    print(f"  confidence={r3.get('email_confidence')}")
    print(f"  {PASS if t3_ok else FAIL} T3")
    if not t3_ok:
        failures.append("T3")

    # T4: no website → honest floor (phone-first)
    print("\n[T4] no website → honest null (no fabrication)")
    lead4 = {"place_id": "t4", "name": "No Email Shop", "website": "", "phone": "555-0100"}
    r4 = run_email_enrich(lead4, _mock_site_pages=[], _mock_smtp=mock)
    t4_ok = r4.get("email") is None and r4.get("email_confidence") is None
    print(f"  email={r4.get('email')} evidence={r4.get('email_evidence', '')[:60]}")
    print(f"  {PASS if t4_ok else FAIL} T4")
    if not t4_ok:
        failures.append("T4")

    # T5: guessed pattern must not survive as verified without evidence+SMTP
    print("\n[T5] pattern_inferred never labeled verified without SMTP accept")
    mock5 = MockSmtpBackend(
        mx_by_domain={domain: ["mx.testshop.example"]},
        accept={f"jane.doe@{domain}"},
    )
    r5 = enrich_email_for_lead(
        {"name": "Jane Doe", "website": f"https://{domain}", "owner_name": "Jane Doe"},
        site_bundle={"pages": [], "fetch_status": "mock"},
        mock_smtp=mock5,
        live_smtp=True,
    )
    # pattern can become verified ONLY via SMTP — that's allowed per open-the-source gate
    # but adversarial check: without SMTP it must NOT be verified
    mock5b = MockSmtpBackend(mx_by_domain={domain: ["mx.testshop.example"]}, reject=set())
    r5b = enrich_email_for_lead(
        {"name": "Jane Doe", "website": f"https://{domain}", "owner_name": "Jane Doe"},
        site_bundle={"pages": [], "fetch_status": "mock"},
        mock_smtp=mock5b,
        live_smtp=True,
    )
    t5_ok = r5b.get("email_confidence") != "verified"
    print(f"  without SMTP accept: confidence={r5b.get('email_confidence')}")
    print(f"  {PASS if t5_ok else FAIL} T5")
    if not t5_ok:
        failures.append("T5")

    # T6: corporate suffix strip — no silvasons.co@ from "Silvasons Machine Co"
    print("\n[T6] corporate suffix strip in pattern inference")
    pats = infer_pattern_candidates(None, "Silvasons Machine Co", "silvasons.com")
    pat_addrs = {c.address for c in pats}
    t6_ok = "silvasons.co@silvasons.com" not in pat_addrs and "silvasons.machine@silvasons.com" not in pat_addrs
    t6_ok = t6_ok and "silvasons@silvasons.com" in pat_addrs
    print(f"  patterns={sorted(pat_addrs)}")
    print(f"  {PASS if t6_ok else FAIL} T6")
    if not t6_ok:
        failures.append("T6")

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    print("ALL SELF-TESTS PASSED")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Email enrichment stage")
    parser.add_argument("--batch", action="store_true", help="Run on has-site cohort")
    parser.add_argument("--place-id", help="Enrich one lead by place_id")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0, help="Skip first N has-site leads (sharding)")
    parser.add_argument(
        "--shard-out",
        metavar="PATH",
        help="Write results to shard JSON instead of store (parallel-safe)",
    )
    parser.add_argument(
        "--merge-shards",
        metavar="DIR",
        help="Merge shard_*.json from DIR into pipeline_leads",
    )
    parser.add_argument("--no-smtp", action="store_true", help="Skip live SMTP (MX only)")
    parser.add_argument("--force", action="store_true", help="Ignore cache")
    parser.add_argument("--self-test", action="store_true", help="Hermetic self-test")
    parser.add_argument(
        "--instantly-verify",
        action="store_true",
        help="Verify site-sourced candidates via Instantly API (founder-gated)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --instantly-verify: list queue only, no API calls",
    )
    args = parser.parse_args()

    if args.merge_shards:
        store = get_store() if _STORE_AVAILABLE else None
        if store is None:
            print("lead_store not available", file=sys.stderr)
            sys.exit(1)
        summary = merge_shards(Path(args.merge_shards), store)
        print("=== Shard Merge Summary ===")
        print(f"Merged: {summary['merged']} leads from {summary['shard_dir']}")
        return

    if args.self_test or (
        not args.batch and not args.place_id and not args.instantly_verify
    ):
        _run_self_test()
        return

    store = get_store() if _STORE_AVAILABLE else None
    if store is None:
        print("lead_store not available", file=sys.stderr)
        sys.exit(1)

    if args.instantly_verify:
        summary = run_instantly_verify_batch(
            store, dry_run=args.dry_run, limit=args.limit,
        )
        print("=== Instantly Verify Summary ===")
        print(f"Queued: {summary.get('queued', 0)}")
        print(f"Processed: {summary.get('processed', 0)}")
        if summary.get("credits_used"):
            print(f"Credits used: {summary['credits_used']}")
        for k, v in summary.get("counts", {}).items():
            if v:
                print(f"  {k}: {v}")
        return

    # Skip RDAP in batch — low yield, adds latency
    cfg = {"live_smtp": not args.no_smtp, "force": args.force, "live_rdap": False}

    if args.batch:
        if not args.no_smtp and not smtp_port_reachable():
            print("WARNING: outbound SMTP port 25 appears blocked on this network.")
            print("  Site emails will be sourced but confidence stays unverified until SMTP can run.")
            print("  Re-run batch from a VPS that allows :25 to obtain verified|risky tiers.\n")
        shard_path = Path(args.shard_out) if args.shard_out else None
        summary = run_batch(
            store,
            limit=args.limit,
            offset=args.offset,
            live_smtp=not args.no_smtp,
            force=args.force,
            shard_path=shard_path,
        )
        print("=== Email Enrichment Batch Summary ===")
        print(f"Processed: {summary['processed']}")
        for k, v in summary["counts"].items():
            print(f"  {k}: {v}")
        return

    if args.place_id:
        lead = store.get_lead(place_id=args.place_id)
        if not lead:
            print(f"Lead not found: {args.place_id}", file=sys.stderr)
            sys.exit(1)
        updated = run_email_enrich(lead, store=store, config=cfg)
        print(json.dumps({
            k: updated.get(k)
            for k in ("place_id", "name", "email", "email_confidence", "email_source", "email_evidence")
        }, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
