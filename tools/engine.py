"""tools/engine.py - Chargeback & Dispute AI's evidence-packet pipeline.

No model call anywhere in here - see docs/how-it-works.md, "The central
design choice". Every function is a pure function over dicts and the
dataclasses in core/adapters/base.py, unit-tested directly in
tests/test_chargeback_dispute_engine.py. `process_dispute()` is the only
impure function: it fetches the reservation, the comms thread and the
terms-acceptance record, then hands everything to `assemble_packet()`.

Shared by tools/run.py (the real loop) and tools/demo.py (the
zero-credential walkthrough), so both exercise exactly the same code path.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any

from core.adapters import get_sheets
from core.adapters.base import AdapterError, EmailMessage, Reservation
from core.config import Settings, repo_root, sub_data_dir
from core.i18n import detect_language
from core.log import get_logger
from core.review import assert_write_allowed
from core.store import Item, Store, StoreError, utcnow

log = get_logger("engine")

DISPUTES_SHEET = "disputes"
DISPUTES_HEADER = ["filed_at", "item_id", "dispute_id", "guest_name", "amount", "currency",
                   "reason_code", "card_scheme", "deadline_date", "days_remaining_at_submit",
                   "evidence_strength", "verdict", "filed_path"]

_DEFAULT_REQUIRES = ["folio", "stay", "comms", "policy"]


# --------------------------------------------------------------------------
# small pure helpers - each one is a unit test
# --------------------------------------------------------------------------
def fmt_money(amount: Any, currency: str) -> str:
    """Money in the hotel's own currency, never a hard-coded EUR - see
    ARCHITECTURE.md and build-repo.md rule 'Money strings use hotel.currency'."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        value = 0.0
    return f"{currency} {value:,.2f}"


def trim_message(text: str | None, limit: int = 140) -> str:
    """Spec step 3: each excerpt trimmed to ``limit`` characters with an ellipsis."""
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def excerpt_messages(messages: list[EmailMessage], *, limit: int = 3,
                     trim_len: int = 140) -> list[dict]:
    """The first ``limit`` messages of a thread, oldest first, as
    ``{sender, when, body}`` - spec step 3."""
    out = []
    for m in messages[:limit]:
        out.append({
            "sender": m.from_name or m.from_email or "guest",
            "when": (m.received_at or "")[:10],
            "body": trim_message(m.body_text, trim_len),
        })
    return out


def parse_reason_code(reason_code: str, reason_code_map: dict) -> dict:
    """Split the free-text reason code into a display code + label, and look up
    which exhibits it requires. Longest-prefix match against
    ``config/agent.yaml: reason_code_map``; unmatched falls back to ``default``.
    See docs/how-it-works.md, design decision 4."""
    raw = (reason_code or "").strip()
    code = raw
    for sep in ("—", "–", " - ", ":"):
        if sep in raw:
            code = raw.split(sep, 1)[0].strip()
            break
    entry = None
    best_len = -1
    for key, value in (reason_code_map or {}).items():
        if key == "default":
            continue
        key_s = str(key)
        if code.startswith(key_s) and len(key_s) > best_len:
            entry, best_len = value, len(key_s)
    if entry is None:
        entry = (reason_code_map or {}).get("default") or {}
    label = str(entry.get("label") or "Reason not on file")
    requires = list(entry.get("requires") or _DEFAULT_REQUIRES)
    return {"code": code or "unknown", "raw": raw, "label": label, "requires": requires}


def guest_words_help(first_message_text: str, keywords: list[str]) -> bool:
    """Does the cardholder's own first message read as a self-initiated
    cancellation? Plain substring scan - see config/agent.yaml: guest_language."""
    text = (first_message_text or "").lower()
    return any(str(k).lower() in text for k in (keywords or []))


def guest_words_hurt(first_message_text: str, keywords: list[str]) -> bool:
    """Does the cardholder's own first message read as a service-failure claim -
    the kind of sentence that SUPPORTS the chargeback reason instead of
    rebutting it? See docs/how-it-works.md, design decision 3."""
    text = (first_message_text or "").lower()
    return any(str(k).lower() in text for k in (keywords or []))


def first_message_text(messages: list[EmailMessage]) -> str:
    """The cardholder's own opening message, or '' if there is no thread on
    file. Shared by guest_words_help/hurt's caller and detect_language()."""
    return messages[0].body_text if messages else ""


def guest_phrase_set(guest_language: dict, lang: str) -> dict | None:
    """The helps_case/hurts_case phrases configured for one guest language
    (``config/agent.yaml: guest_language.<lang>``), or ``None`` when the
    hotel has not typed any phrases for this language at all - a silent
    "neutral" score is exactly the gap SIMULATION.md finding 4 flags. A
    language with no entry here must be read by a person, never auto-scored."""
    entry = (guest_language or {}).get(lang)
    if not isinstance(entry, dict):
        return None
    if not entry.get("helps_case") and not entry.get("hurts_case"):
        return None
    return entry


def cancellation_policy_is_placeholder() -> bool:
    """True when knowledge/cancellation-policy.md exists but is still a
    word-for-word copy of knowledge/cancellation-policy.example.md - see
    tools/doctor.py:check_cancellation_policy and SIMULATION.md finding 5.
    A packet must never quote the shipped placeholder terms to a bank as
    though they were this property's real policy. A fresh clone (no real
    file yet, only the .example.md) is a different, already-flagged state
    (`make doctor` WARNs "using the shipped example") and returns False here."""
    real = repo_root() / "knowledge" / "cancellation-policy.md"
    example = repo_root() / "knowledge" / "cancellation-policy.example.md"
    if not real.is_file() or not example.is_file():
        return False
    real_hash = hashlib.sha256(real.read_bytes()).hexdigest()
    example_hash = hashlib.sha256(example.read_bytes()).hexdigest()
    return real_hash == example_hash


def deadline_date(ingested_on: str, deadline_offset: int) -> str:
    """The fixed response deadline, computed ONCE at ingestion (see
    docs/how-it-works.md, design decision 6) - never recomputed, so it
    cannot drift on a later pass."""
    base = date.fromisoformat(ingested_on)
    return (base + timedelta(days=int(deadline_offset))).isoformat()


def days_remaining(deadline_iso: str, today: date | None = None) -> int:
    """Days left to respond, computed fresh on every read against ``today``."""
    today = today or date.today()
    try:
        return (date.fromisoformat(deadline_iso) - today).days
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# evidence sections - spec section 4
# --------------------------------------------------------------------------
def folio_rows(dispute: dict, reservation: Reservation | None, currency: str) -> list[list[str]]:
    """The two-column folio summary table. Degrades to the dispute's own
    fields when the reservation could not be found - spec step 2."""
    amount = dispute.get("amount", 0.0)
    if reservation is None:
        return [
            ["Booking reference", dispute.get("reservation_ref") or "(not on file)"],
            ["Guest", dispute.get("guest_name", "")],
            ["Amount disputed", fmt_money(amount, currency)],
            ["Card scheme", dispute.get("card_scheme", "")],
        ]
    pct = round(100 * amount / reservation.total) if reservation.total else None
    deposit_label = (f"{fmt_money(amount, currency)} ({pct}% of stay total)" if pct is not None
                     else fmt_money(amount, currency))
    return [
        ["Booking reference", reservation.external_ref or reservation.id],
        ["Guest", reservation.guest.full_name or dispute.get("guest_name", "")],
        ["Booking channel", reservation.source or "(not on file)"],
        ["Room type", reservation.room_type_name or reservation.room_type_id],
        ["Arrival", reservation.check_in],
        ["Departure", f"{reservation.check_out} ({reservation.nights} night(s))"],
        ["Guests", str(reservation.adults + reservation.children)],
        ["Booking status", reservation.status],
        ["Stay total", fmt_money(reservation.total, currency)],
        ["Deposit charged", deposit_label],
        ["Amount disputed", fmt_money(amount, currency)],
        ["Card scheme", dispute.get("card_scheme", "")],
    ]


def stay_evidence_lines(reservation: Reservation | None, helps_case: bool) -> list[str]:
    """The five stay-evidence assertions, generated from the real
    reservation - spec step 4, section 2. Empty when the reservation is
    missing: there is nothing true to assert about a stay we cannot see."""
    if reservation is None:
        return []
    guest = reservation.guest.full_name or "the cardholder"
    lines = [
        f"The booking was created and confirmed in the property's system in {guest}'s own name.",
        f"The reservation was held for {reservation.check_in} to {reservation.check_out} "
        f"({reservation.nights} night(s)), {reservation.room_type_name or reservation.room_type_id}, "
        f"stay total {reservation.total:,.2f}.",
        "The room was blocked and held for the guest for the full period; it was not resold. "
        "The service was available and was not withdrawn by the property.",
    ]
    if helps_case:
        lines.append("The cancellation was initiated by the cardholder, not by the property - "
                     "see the guest's own message in the comms section.")
    else:
        lines.append("No written cancellation from the cardholder is on file for this booking.")
    lines.append("The disputed amount is the confirmation deposit under accepted terms, not a "
                 "charge for an unrendered service.")
    return lines


def read_knowledge_file(name: str) -> str:
    """Read a knowledge/ file, falling back to its .example.md twin - the same
    fallback core.templates.load_knowledge() uses, without the '### heading'
    wrapper, so the text can go straight into a packet section body."""
    path = repo_root() / "knowledge" / name
    if not path.exists():
        example = path.with_suffix(".example" + path.suffix) if path.suffix != ".md" \
            else path.with_name(path.stem + ".example.md")
        path = example
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    # Strip HTML comments (the "copy this and fill it in" instructions in the
    # shipped .example.md) - a legal document must never carry editor notes.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def policy_section_body(terms_record: dict | None) -> dict:
    """The cancellation-policy text plus the acceptance record, or an honest
    note when there is no record - spec section 4, design decision 2/10."""
    policy_text = read_knowledge_file("cancellation-policy.md")
    if terms_record:
        acceptance = (
            f"Accepted electronically on {terms_record.get('accepted_at', '(unknown)')} "
            f"from IP {terms_record.get('ip', '(unknown)')}, checkout page version "
            f"{terms_record.get('checkout_page_version', '(unknown)')}, policy version "
            f"{terms_record.get('policy_version', '(unknown)')}.")
    else:
        acceptance = ("No acceptance record is on file for this booking. The terms below are "
                      "shown for reference; this packet cannot show exactly when or how this "
                      "guest accepted them.")
    return {"policy_text": policy_text, "acceptance": acceptance, "has_record": bool(terms_record)}


COMMS_ARGUMENT_HELPS = (
    "The cardholder states in writing that the trip was cancelled by them and asks for a "
    "refund outside the free-cancellation window - a request, not a claim that the service "
    "was never provided. That single line contradicts the reason code the chargeback was "
    "filed under."
)
COMMS_ARGUMENT_HURTS = (
    "The cardholder's own message describes a problem with the service, which does not "
    "support this representment - see Recommendation below before submitting anything."
)
COMMS_ARGUMENT_NEUTRAL = (
    "The cardholder's own words are included above for the case file."
)


def comms_argument(helps_case: bool, hurts_case: bool) -> str:
    if hurts_case:
        return COMMS_ARGUMENT_HURTS
    if helps_case:
        return COMMS_ARGUMENT_HELPS
    return COMMS_ARGUMENT_NEUTRAL


# --------------------------------------------------------------------------
# scoring + recommendation - spec step 5, reworked per design decision 3
# --------------------------------------------------------------------------
def score_evidence(required: list[str], *, has_folio: bool, has_stay: bool,
                   has_comms: bool, has_policy: bool, hurts_case: bool) -> dict:
    """How much of what THIS reason code requires is actually on file.

    A flat percentage of the required exhibits present, minus a penalty when
    the guest's own words undercut the case. Unlike the source demo, this is
    never a single on/off rule - see docs/how-it-works.md, design decision 3.
    """
    exhibits = {"folio": has_folio, "stay": has_stay, "comms": has_comms, "policy": has_policy}
    required = required or _DEFAULT_REQUIRES
    weight = 100.0 / len(required)
    present = [name for name in required if exhibits.get(name)]
    missing = [name for name in required if not exhibits.get(name)]
    strength = round(weight * len(present), 1)
    if hurts_case:
        strength = max(0.0, strength - 30)
    return {"strength": min(100.0, strength), "present": present, "missing": missing}


def recommend(score: dict, *, threshold: float, hurts_case: bool, amount: Any,
             currency: str, reason_label: str, reason_code: str, days_left: int,
             card_scheme: str) -> dict:
    """spec step 5: a plain-language recommendation, gated on real evidence
    quality rather than a single rule toggle - see score_evidence()."""
    money = fmt_money(amount, currency)
    represent = (not score["missing"]) and not hurts_case and score["strength"] >= threshold
    if represent:
        text = (f"Represent in full for {money}. Every exhibit reason code {reason_code} "
                f"({reason_label}) requires is on file. Submit before the {days_left}-day "
                f"deadline.")
        return {"verdict": "represent", "text": text}
    reasons = []
    if score["missing"]:
        reasons.append(f"missing: {', '.join(score['missing'])}")
    if hurts_case:
        reasons.append("the cardholder's own message works against this reason code")
    why = "; ".join(reasons) if reasons else "evidence strength is below the review threshold"
    text = (f"Do not submit yet ({why}). Gather the rest, or turn on the full evidence pack, "
           f"before {money} goes to {card_scheme} on reason code {reason_code} with "
           f"{days_left} day(s) left.")
    return {"verdict": "hold", "text": text}


# --------------------------------------------------------------------------
# packet assembly - pure function, everything above feeds into this
# --------------------------------------------------------------------------
def assemble_packet(dispute: dict, *, reservation: Reservation | None,
                    messages: list[EmailMessage], terms_record: dict | None,
                    reason_map: dict, rules: dict, guest_language: dict,
                    threshold: float, currency: str, ingested_on: str,
                    guest_lang: str = "en") -> dict:
    """Build the whole evidence packet. No I/O - every input is already fetched.

    ``guest_lang`` is the guest's own message language (``process_dispute()``
    detects it with ``core.i18n.detect_language()`` before calling this) -
    used only to pick which ``guest_language.<lang>`` phrase set screens the
    cardholder's own words, never to change the reply language (this agent
    never drafts a guest-facing reply)."""
    parsed = parse_reason_code(dispute.get("reason_code", ""), reason_map)
    degraded = reservation is None
    full_pack = bool(rules.get("full_evidence_pack", True))
    first_message = first_message_text(messages)
    guest_lang = str(guest_lang or "en")
    phrases = guest_phrase_set(guest_language, guest_lang)
    # A language with no configured phrase set is never silently scored
    # neutral (SIMULATION.md finding 4) - it is held for a person to read.
    no_phrase_set = bool(first_message) and phrases is None
    helps = guest_words_help(first_message, phrases.get("helps_case")) if phrases else False
    hurts = guest_words_hurt(first_message, phrases.get("hurts_case")) if phrases else False
    has_comms_available = bool(messages)
    has_comms = full_pack and has_comms_available
    has_policy = full_pack and bool(terms_record)
    # A cancellation-policy file that is still the shipped placeholder must
    # never be quoted to a bank as this property's real terms (SIMULATION.md
    # finding 5) - see cancellation_policy_is_placeholder().
    placeholder_policy = full_pack and cancellation_policy_is_placeholder()

    score = score_evidence(parsed["requires"], has_folio=True, has_stay=not degraded,
                           has_comms=has_comms, has_policy=has_policy, hurts_case=hurts)

    ddl = deadline_date(ingested_on, dispute.get("deadline_offset", 7))
    days_left = days_remaining(ddl)
    rec = recommend(score, threshold=threshold, hurts_case=hurts, amount=dispute.get("amount"),
                    currency=currency, reason_label=parsed["label"], reason_code=parsed["code"],
                    days_left=days_left, card_scheme=dispute.get("card_scheme", ""))

    warnings: list[str] = []
    block_reasons: list[str] = []
    if no_phrase_set:
        block_reasons.append(f"no {guest_lang} phrase set configured - review manually")
        warnings.append(
            f"No {guest_lang} phrase set is configured under config/agent.yaml: "
            f"guest_language - the cardholder's own message could not be screened for "
            f"language that would work against this case. Add guest_language.{guest_lang} "
            f"phrases, or read this message yourself before approving.")
    if placeholder_policy:
        block_reasons.append("cancellation policy is still the shipped example, word for word")
        warnings.append(
            "knowledge/cancellation-policy.md is still the shipped example, word for word - "
            "section 4 below quotes placeholder terms, not this property's real policy. "
            "Replace the file before filing anything built from this packet.")
    if block_reasons:
        rec = {"verdict": "hold",
              "text": f"Do not submit yet ({'; '.join(block_reasons)}). Fix the above and "
                      f"re-run to get a real recommendation for "
                      f"{fmt_money(dispute.get('amount'), currency)}."}
    if degraded:
        warnings.append(
            f"No reservation record was found for {dispute.get('reservation_ref') or '(none given)'}. "
            f"The folio below is rebuilt from the dispute's own deposit charge; no stay evidence "
            f"could be built from the property system.")
    if not full_pack:
        warnings.append(
            'The "Full evidence packets" rule is off. This packet ships without the guest '
            'correspondence and without the cancellation policy the cardholder accepted - two '
            'of the documents that actually rebut most chargeback reason codes.')
        warnings.append(f"On reason code {parsed['code']}, packets missing required exhibits are "
                        f"the ones that lose. {fmt_money(dispute.get('amount'), currency)} is at risk.")
    elif not has_comms_available and "comms" in parsed["requires"]:
        warnings.append("No guest correspondence is attached to this booking in the comms "
                        "record - reason code " + parsed["code"] + " normally needs it.")
    if full_pack and not terms_record and "policy" in parsed["requires"]:
        warnings.append("No acceptance record is on file for the cancellation terms - see the "
                        "Policy section.")
    if hurts:
        warnings.append("The cardholder's own first message describes a problem with the "
                        "service, which works against this representment.")

    sections = [{"key": "folio", "title": "1. Folio summary",
                "rows": folio_rows(dispute, reservation, currency)},
               {"key": "stay", "title": "2. Stay evidence",
                "lines": stay_evidence_lines(reservation, helps)}]
    if full_pack:
        sections.append({"key": "comms", "title": "3. Guest communications on file",
                         "excerpts": excerpt_messages(messages),
                         "argument": comms_argument(helps, hurts)})
        sections.append({"key": "policy", "title": "4. Cancellation policy as accepted",
                         **policy_section_body(terms_record)})
        section_keys = ["summary", "folio", "stay", "comms", "policy", "recommendation"]
        evidence_items, evidence_note = 4, "folio, stay, comms, policy"
    else:
        section_keys = ["summary", "folio", "stay", "warning", "recommendation"]
        evidence_items, evidence_note = 2, "folio, stay only"

    return {
        "dispute_id": dispute.get("id"), "title": "Chargeback representment",
        "guest_name": dispute.get("guest_name", ""), "amount": dispute.get("amount"),
        "currency": currency, "card_scheme": dispute.get("card_scheme", ""),
        "reason_code": parsed["code"], "reason_label": parsed["label"],
        "reason_code_raw": dispute.get("reason_code", ""), "venue": dispute.get("venue", "hotel"),
        "deadline_date": ddl, "days_remaining": days_left,
        "evidence_items": evidence_items, "evidence_items_note": evidence_note,
        "evidence_strength": score["strength"], "evidence_present": score["present"],
        "evidence_missing": score["missing"], "degraded": degraded, "helps_case": helps,
        "hurts_case": hurts, "comms_thread_total": len(messages), "section_keys": section_keys,
        "sections": sections, "warnings": warnings, "recommendation": rec,
        "guest_lang": guest_lang, "guest_lang_screened": phrases is not None,
    }


# --------------------------------------------------------------------------
# the pipeline - the only impure function in this module
# --------------------------------------------------------------------------
def process_dispute(settings: Settings, store: Store, raw: dict, *, pms: Any,
                    email_adapter: Any, terms_store: Any) -> tuple[Item, bool]:
    """Fetch the evidence for one dispute and build its packet.

    Idempotent: an item that already has a ``draft`` was fully packeted by an
    earlier pass and is left untouched (``(item, False)``). ``settings.dry_run``
    computes and returns the exact decision but writes nothing at all - no
    ``items`` row, no state change - see docs/how-it-works.md, "Idempotency".
    """
    dispute_id = raw["id"]
    if settings.dry_run:
        existing = store.get_by_external("disputes", dispute_id)
        if existing is not None and existing.draft is not None:
            return existing, False
        item = existing if existing is not None else Item(
            id=f"dry-run-{dispute_id}", kind="dispute", source="disputes",
            external_id=dispute_id, payload=raw, review_status="new")
        item.payload = raw
    else:
        item = store.upsert_item("disputes", dispute_id, kind="dispute", payload=raw)
        if item.draft is not None:
            return item, False

    reservation: Reservation | None = None
    if raw.get("reservation_ref"):
        try:
            reservation = pms.get_reservation(raw["reservation_ref"])
        except AdapterError as exc:
            log.warn("pms lookup failed, degrading", dispute_id=dispute_id, error=str(exc))

    messages: list[EmailMessage] = []
    if raw.get("comms_conversation_id"):
        try:
            messages = sorted(email_adapter.fetch_thread(raw["comms_conversation_id"]),
                              key=lambda m: m.received_at or "")
        except AdapterError as exc:
            log.warn("comms lookup failed", dispute_id=dispute_id, error=str(exc))

    terms_record = terms_store.lookup(raw.get("reservation_ref"))
    guest_lang = detect_language(first_message_text(messages), settings=settings).lang
    packet = assemble_packet(
        raw, reservation=reservation, messages=messages, terms_record=terms_record,
        reason_map=settings.agent_get("reason_code_map", {}) or {},
        rules=settings.agent_get("rules", {}) or {},
        guest_language=settings.agent_get("guest_language", {}) or {},
        threshold=float(settings.agent_get("evidence_strength_threshold", 70)),
        currency=settings.hotel.currency, ingested_on=date.today().isoformat(),
        guest_lang=guest_lang)
    status = "pending_review" if packet["recommendation"]["verdict"] == "represent" else "needs_human"

    if settings.dry_run:
        item.intent = packet["recommendation"]["verdict"]
        item.confidence = packet["evidence_strength"] / 100.0
        item.draft, item.review_status = packet, status
        log.info("computed (--dry-run, nothing written)", dispute_id=dispute_id, would_be=status)
        return item, True

    store.set_fields(item.id, intent=packet["recommendation"]["verdict"],
                     confidence=packet["evidence_strength"] / 100.0, draft=packet)
    updated = store.transition(item.id, status, actor="agent",
                               detail={"verdict": packet["recommendation"]["verdict"],
                                      "evidence_strength": packet["evidence_strength"]})
    log.info("packet built", dispute_id=dispute_id, verdict=packet["recommendation"]["verdict"],
             strength=packet["evidence_strength"], status=updated.review_status)
    return updated, True


# --------------------------------------------------------------------------
# rendering + the one guarded write this agent makes
# --------------------------------------------------------------------------
def render_packet_markdown(packet: dict) -> str:
    """The human-readable artifact a person uploads to their processor's own
    dispute portal - see docs/how-it-works.md, 'Being honest about payments'."""
    lines = [f"# {packet['title']} - {packet['dispute_id']}", "",
            f"Prepared for: {packet.get('guest_name', '')}  |  Amount: "
            f"{fmt_money(packet['amount'], packet['currency'])}  |  Scheme: {packet['card_scheme']}",
            f"Reason code: {packet['reason_code']} - {packet['reason_label']}  |  "
            f"Response deadline: {packet['deadline_date']} ({packet['days_remaining']} day(s) left)",
            "", f"Evidence items: {packet['evidence_items']} ({packet['evidence_items_note']})  |  "
            f"Evidence strength: {packet['evidence_strength']:.0f}/100"]
    for w in packet.get("warnings") or []:
        lines += ["", f"> WARNING: {w}"]
    for section in packet["sections"]:
        lines += ["", f"## {section['title']}"]
        for label, value in section.get("rows", []):
            lines.append(f"- **{label}:** {value}")
        for i, line in enumerate(section.get("lines", []), 1):
            lines.append(f"{i}. {line}")
        if "excerpts" in section:
            if not section["excerpts"]:
                lines.append("No guest correspondence is attached to this booking in the "
                             "comms record.")
            for ex in section["excerpts"]:
                lines.append(f"- **{ex['when']} - {ex['sender']}:** {ex['body']}")
            lines += ["", section.get("argument", "")]
        if "policy_text" in section:
            lines += [section["policy_text"], "", section.get("acceptance", "")]
    verdict = packet["recommendation"]["verdict"].upper()
    lines += ["", "## Recommendation", "", f"**{verdict}.** {packet['recommendation']['text']}"]
    return "\n".join(lines) + "\n"


def finalize_dispute(settings: Settings, item: Item) -> dict:
    """File the packet locally and log a row to the disputes sheet - the one
    write path this agent has. Guarded like any other write in this family:
    `mode: shadow` and `--dry-run` both block it before either write
    happens. This never calls a card network."""
    assert_write_allowed(settings, "dispute_submit", item)
    packet = item.draft or {}
    target_dir = sub_data_dir("exports") / "evidence-packets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{packet.get('dispute_id', item.id)}.md"
    target.write_text(render_packet_markdown(packet), encoding="utf-8")

    sheets = get_sheets(settings)
    existing = []
    try:
        existing = sheets.read(DISPUTES_SHEET)
    except Exception:  # noqa: BLE001 - a broken read must not block filing
        log.warn("could not read disputes sheet before append")
    filed_path = str(target.relative_to(sub_data_dir("exports")))
    rows = [] if existing else [DISPUTES_HEADER]
    rows.append([utcnow(), item.id, packet.get("dispute_id"), packet.get("guest_name"),
                packet.get("amount"), packet.get("currency"), packet.get("reason_code"),
                packet.get("card_scheme"), packet.get("deadline_date"),
                packet.get("days_remaining"), packet.get("evidence_strength"),
                packet.get("recommendation", {}).get("verdict"), filed_path])
    sheet_result = sheets.append(DISPUTES_SHEET, rows, item=item)
    return {"filed_path": filed_path, "sheet": sheet_result}


def record_outcome(store: Store, item_id: str, result: str,
                   recovered_amount: float | None = None) -> Item:
    """A human records the real bank ruling, once it is in - see
    docs/how-it-works.md, design decisions 7-9. Not a guarded write: this is
    bookkeeping on a decision a person already made outside this repo, not
    an action this agent takes on its own."""
    if result not in ("won", "lost", "won_in_part"):
        raise StoreError("result must be one of: won, lost, won_in_part")
    item = store.get_item(item_id)
    if item is None:
        raise StoreError(f"no item {item_id}")
    if item.review_status != "sent":
        raise StoreError(
            f"item {item_id} is '{item.review_status}', not 'sent' - only a submitted dispute "
            f"can have an outcome recorded. Approve it and run `python3 tools/review.py send` "
            f"first.")
    draft = dict(item.draft or {})
    if recovered_amount is None:
        recovered_amount = draft.get("amount") if result == "won" else 0.0
    draft["outcome"] = result
    draft["recovered_amount"] = float(recovered_amount)
    draft["decided_at"] = utcnow()
    updated = store.set_fields(item_id, draft=draft)
    assert updated is not None
    return updated
