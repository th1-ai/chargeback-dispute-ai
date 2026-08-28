"""Pure-function tests for tools/engine.py - no store, no LLM, no I/O.

Every rule in docs/how-it-works.md is checked here directly, over plain
dicts and dataclasses, so a change to a threshold or a wording is a
one-line diff to spot.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters.base import Guest, Reservation  # noqa: E402
from engine import (assemble_packet, deadline_date, days_remaining,  # noqa: E402
                    fmt_money, guest_phrase_set, guest_words_help, guest_words_hurt,
                    parse_reason_code, recommend, score_evidence, trim_message)

REASON_MAP = {
    "4853": {"label": "Services not received", "requires": ["folio", "stay", "comms", "policy"]},
    "10.4": {"label": "Fraud - card not present", "requires": ["folio", "stay", "policy"]},
    "default": {"label": "Reason not on file", "requires": ["folio", "stay", "comms", "policy"]},
}
GUEST_LANGUAGE_PHRASES = {
    "helps_case": ["cancel", "won't be able to make it"],
    "hurts_case": ["never worked", "never received"],
}
# guest_language is keyed by language code - see tools/engine.py:guest_phrase_set.
GUEST_LANGUAGE = {"en": GUEST_LANGUAGE_PHRASES}


def _reservation(**overrides) -> Reservation:
    base = dict(id="RES-1", external_ref="RES-1", status="confirmed", check_in="2026-09-14",
               check_out="2026-09-17", room_type_name="Deluxe Sea View", adults=2, children=0,
               source="Direct", total=3760.0, currency="EUR",
               guest=Guest(first_name="Marcus", last_name="Webb"))
    base.update(overrides)
    return Reservation(**base)


# --------------------------------------------------------------------------
# money, reason codes, dates, keywords
# --------------------------------------------------------------------------
def test_fmt_money_uses_the_currency_passed_in_not_a_hardcoded_eur():
    assert fmt_money(940, "EUR") == "EUR 940.00"
    assert fmt_money(940, "GBP") == "GBP 940.00"
    assert fmt_money(1234.5, "NOK") == "NOK 1,234.50"


def test_parse_reason_code_matches_a_known_prefix():
    parsed = parse_reason_code("4853 - Services not received", REASON_MAP)
    assert parsed["code"] == "4853"
    assert parsed["label"] == "Services not received"
    assert parsed["requires"] == ["folio", "stay", "comms", "policy"]


def test_parse_reason_code_falls_back_to_default_for_an_unknown_code():
    parsed = parse_reason_code("9999 - Something new", REASON_MAP)
    assert parsed["code"] == "9999"
    assert parsed["label"] == "Reason not on file"
    assert parsed["requires"] == ["folio", "stay", "comms", "policy"]


def test_trim_message_adds_an_ellipsis_past_the_limit():
    assert trim_message("short", 140) == "short"
    long_text = "x" * 200
    trimmed = trim_message(long_text, 140)
    assert trimmed.endswith("...")
    assert len(trimmed) == 143


def test_deadline_date_is_fixed_at_ingestion_and_days_remaining_counts_down():
    ddl = deadline_date("2026-08-21", 6)
    assert ddl == "2026-08-27"
    from datetime import date
    assert days_remaining(ddl, today=date(2026, 8, 21)) == 6
    assert days_remaining(ddl, today=date(2026, 8, 25)) == 2
    assert days_remaining(ddl, today=date(2026, 8, 30)) == -3


def test_guest_words_help_and_hurt_are_independent_checks():
    assert guest_words_help("We need to cancel our stay",
                            GUEST_LANGUAGE_PHRASES["helps_case"]) is True
    assert guest_words_hurt("We need to cancel our stay",
                            GUEST_LANGUAGE_PHRASES["hurts_case"]) is False
    assert guest_words_hurt("The air con never worked",
                            GUEST_LANGUAGE_PHRASES["hurts_case"]) is True
    assert guest_words_help("The air con never worked",
                            GUEST_LANGUAGE_PHRASES["helps_case"]) is False


# --------------------------------------------------------------------------
# scoring + recommendation - docs/how-it-works.md, design decision 3
# --------------------------------------------------------------------------
def test_score_evidence_full_strength_when_every_required_exhibit_is_present():
    score = score_evidence(["folio", "stay", "comms", "policy"], has_folio=True, has_stay=True,
                           has_comms=True, has_policy=True, hurts_case=False)
    assert score["strength"] == 100.0
    assert score["missing"] == []


def test_score_evidence_flags_the_missing_exhibit():
    score = score_evidence(["folio", "stay", "comms", "policy"], has_folio=True, has_stay=True,
                           has_comms=False, has_policy=True, hurts_case=False)
    assert score["missing"] == ["comms"]
    assert score["strength"] == 75.0


def test_recommend_holds_when_a_required_exhibit_is_missing():
    score = score_evidence(["folio", "stay", "comms", "policy"], has_folio=True, has_stay=True,
                           has_comms=False, has_policy=True, hurts_case=False)
    rec = recommend(score, threshold=70, hurts_case=False, amount=300, currency="EUR",
                    reason_label="Services not received", reason_code="4853", days_left=10,
                    card_scheme="Visa")
    assert rec["verdict"] == "hold"
    assert "comms" in rec["text"]


def test_recommend_holds_even_at_full_strength_when_the_guest_words_hurt_the_case():
    """A hostile guest message must never read as a strong representment,
    however complete the paperwork is - docs/how-it-works.md decision 3."""
    score = score_evidence(["folio", "stay", "comms", "policy"], has_folio=True, has_stay=True,
                           has_comms=True, has_policy=True, hurts_case=True)
    assert score["missing"] == []  # every exhibit is technically on file
    rec = recommend(score, threshold=70, hurts_case=True, amount=160, currency="EUR",
                    reason_label="Services not received", reason_code="4853", days_left=2,
                    card_scheme="Visa")
    assert rec["verdict"] == "hold"


def test_recommend_represents_when_everything_required_is_on_file():
    score = score_evidence(["folio", "stay", "comms", "policy"], has_folio=True, has_stay=True,
                           has_comms=True, has_policy=True, hurts_case=False)
    rec = recommend(score, threshold=70, hurts_case=False, amount=940, currency="EUR",
                    reason_label="Services not received", reason_code="4853", days_left=6,
                    card_scheme="Mastercard")
    assert rec["verdict"] == "represent"
    assert "EUR 940.00" in rec["text"]


# --------------------------------------------------------------------------
# packet assembly
# --------------------------------------------------------------------------
def test_assemble_packet_degrades_gracefully_when_the_reservation_is_missing():
    dispute = {"id": "DP-1", "guest_name": "Aiko Tanaka", "reservation_ref": "RES-9999",
              "amount": 520.0, "reason_code": "10.4 - Fraud", "card_scheme": "Mastercard",
              "deadline_offset": 8}
    terms_record = {"accepted_at": "2026-07-28", "ip": "192.0.2.7",
                    "checkout_page_version": "v2", "policy_version": "2025.4"}
    packet = assemble_packet(dispute, reservation=None, messages=[], terms_record=terms_record,
                             reason_map=REASON_MAP, rules={"full_evidence_pack": True},
                             guest_language=GUEST_LANGUAGE, threshold=70, currency="EUR",
                             ingested_on="2026-08-21")
    assert packet["degraded"] is True
    # reason code 10.4 only requires folio/stay/policy (docs/how-it-works.md
    # decision 4) - with the acceptance record present, "stay" is the only gap.
    assert packet["evidence_missing"] == ["stay"]
    assert packet["recommendation"]["verdict"] == "hold"
    assert any("no reservation record" in w.lower() for w in packet["warnings"])
    # the folio still degrades to the dispute's own fields rather than crashing
    assert packet["sections"][0]["rows"][0] == ["Booking reference", "RES-9999"]


def test_assemble_packet_full_evidence_pack_off_uses_the_warning_section():
    reservation = _reservation()
    dispute = {"id": "DP-2", "guest_name": "Marcus Webb", "reservation_ref": "RES-1",
              "amount": 940.0, "reason_code": "4853 - Services not received",
              "card_scheme": "Mastercard", "deadline_offset": 6}
    packet = assemble_packet(dispute, reservation=reservation, messages=[], terms_record=None,
                             reason_map=REASON_MAP, rules={"full_evidence_pack": False},
                             guest_language=GUEST_LANGUAGE, threshold=70, currency="EUR",
                             ingested_on="2026-08-21")
    assert packet["section_keys"] == ["summary", "folio", "stay", "warning", "recommendation"]
    assert packet["evidence_items"] == 2
    assert packet["recommendation"]["verdict"] == "hold"
    assert any("full evidence packets" in w.lower() for w in packet["warnings"])


def test_assemble_packet_represents_on_a_strong_full_pack_case():
    reservation = _reservation()
    dispute = {"id": "GM-20138", "guest_name": "Marcus Webb", "reservation_ref": "RES-1",
              "amount": 940.0, "reason_code": "4853 - Services not received",
              "card_scheme": "Mastercard", "deadline_offset": 6}

    class Msg:
        def __init__(self, body):
            self.body_text, self.from_name, self.from_email = body, "Marcus Webb", "m@example.com"
            self.received_at = "2026-08-05T10:00:00+00:00"

    messages = [Msg("We need to cancel our stay, won't be able to make it.")]
    terms_record = {"accepted_at": "2026-08-02", "ip": "203.0.113.1",
                    "checkout_page_version": "v3", "policy_version": "2026.1"}
    packet = assemble_packet(dispute, reservation=reservation, messages=messages,
                             terms_record=terms_record, reason_map=REASON_MAP,
                             rules={"full_evidence_pack": True}, guest_language=GUEST_LANGUAGE,
                             threshold=70, currency="EUR", ingested_on="2026-08-21")
    assert packet["recommendation"]["verdict"] == "represent"
    assert packet["evidence_strength"] == 100.0
    assert packet["helps_case"] is True


# --------------------------------------------------------------------------
# guest_language guardrail - SIMULATION.md finding 4 (2026-08-27)
# --------------------------------------------------------------------------
def test_guest_phrase_set_is_none_for_an_unconfigured_language():
    assert guest_phrase_set(GUEST_LANGUAGE, "en") == GUEST_LANGUAGE_PHRASES
    assert guest_phrase_set(GUEST_LANGUAGE, "de") is None
    assert guest_phrase_set({}, "en") is None
    assert guest_phrase_set({"de": {}}, "de") is None  # entry present but empty - still None


def test_assemble_packet_holds_for_a_guest_language_with_no_phrase_set():
    """A guest message in a language `guest_language` has no entry for must
    never default to "represent" - regression for the German fixture built
    during the 2026-08-27 onboarding simulation against
    `hotel.languages: [es, en]` (SIMULATION.md finding 4)."""
    reservation = _reservation()
    dispute = {"id": "DP-9001", "guest_name": "Lena Fischer", "reservation_ref": "RES-1",
              "amount": 300.0, "reason_code": "4853 - Services not received",
              "card_scheme": "Mastercard", "deadline_offset": 6}

    class Msg:
        def __init__(self, body):
            self.body_text, self.from_name, self.from_email = body, "Lena Fischer", "l@example.de"
            self.received_at = "2026-08-05T10:00:00+00:00"

    # German: "we unfortunately have to cancel, our flight was cancelled" -
    # would read as a self-cancellation if it were screened, but no `de`
    # phrase set is configured, so it must hold instead of representing.
    messages = [Msg("Guten Tag, wir müssen leider stornieren, da unser Flug gestrichen wurde. "
                    "Bitte teilen Sie uns mit, wie es mit der Anzahlung weitergeht. Mit "
                    "freundlichen Grüßen, Lena Fischer")]
    terms_record = {"accepted_at": "2026-08-02", "ip": "203.0.113.9",
                    "checkout_page_version": "v3", "policy_version": "2026.1"}
    packet = assemble_packet(dispute, reservation=reservation, messages=messages,
                             terms_record=terms_record, reason_map=REASON_MAP,
                             rules={"full_evidence_pack": True}, guest_language=GUEST_LANGUAGE,
                             guest_lang="de", threshold=70, currency="EUR",
                             ingested_on="2026-08-21")
    assert packet["recommendation"]["verdict"] == "hold"
    assert "no de phrase set configured" in packet["recommendation"]["text"]
    assert any("no de phrase set" in w.lower() for w in packet["warnings"])
    assert packet["helps_case"] is False
    assert packet["hurts_case"] is False
    assert packet["guest_lang"] == "de"
    assert packet["guest_lang_screened"] is False


def test_assemble_packet_screens_normally_once_a_language_has_phrases():
    """Same shape as the German case above, but for a configured language -
    the guardrail must not hold every case, only the unscreened ones."""
    reservation = _reservation()
    dispute = {"id": "DP-9002", "guest_name": "Marcus Webb", "reservation_ref": "RES-1",
              "amount": 940.0, "reason_code": "4853 - Services not received",
              "card_scheme": "Mastercard", "deadline_offset": 6}

    class Msg:
        def __init__(self, body):
            self.body_text, self.from_name, self.from_email = body, "Marcus Webb", "m@example.com"
            self.received_at = "2026-08-05T10:00:00+00:00"

    messages = [Msg("We need to cancel our stay, won't be able to make it.")]
    terms_record = {"accepted_at": "2026-08-02", "ip": "203.0.113.1",
                    "checkout_page_version": "v3", "policy_version": "2026.1"}
    packet = assemble_packet(dispute, reservation=reservation, messages=messages,
                             terms_record=terms_record, reason_map=REASON_MAP,
                             rules={"full_evidence_pack": True}, guest_language=GUEST_LANGUAGE,
                             guest_lang="en", threshold=70, currency="EUR",
                             ingested_on="2026-08-21")
    assert packet["recommendation"]["verdict"] == "represent"
    assert packet["guest_lang_screened"] is True


# --------------------------------------------------------------------------
# cancellation-policy placeholder guardrail - SIMULATION.md finding 5
# --------------------------------------------------------------------------
def test_assemble_packet_holds_when_the_cancellation_policy_is_still_the_placeholder(
        monkeypatch):
    """`assemble_packet()` must refuse to represent while
    knowledge/cancellation-policy.md is a word-for-word copy of the shipped
    example - see tools/engine.py:cancellation_policy_is_placeholder and
    tools/doctor.py:check_cancellation_policy (SIMULATION.md finding 5)."""
    import engine as engine_module
    monkeypatch.setattr(engine_module, "cancellation_policy_is_placeholder", lambda: True)

    reservation = _reservation()
    dispute = {"id": "GM-20138", "guest_name": "Marcus Webb", "reservation_ref": "RES-1",
              "amount": 940.0, "reason_code": "4853 - Services not received",
              "card_scheme": "Mastercard", "deadline_offset": 6}

    class Msg:
        def __init__(self, body):
            self.body_text, self.from_name, self.from_email = body, "Marcus Webb", "m@example.com"
            self.received_at = "2026-08-05T10:00:00+00:00"

    messages = [Msg("We need to cancel our stay, won't be able to make it.")]
    terms_record = {"accepted_at": "2026-08-02", "ip": "203.0.113.1",
                    "checkout_page_version": "v3", "policy_version": "2026.1"}
    packet = assemble_packet(dispute, reservation=reservation, messages=messages,
                             terms_record=terms_record, reason_map=REASON_MAP,
                             rules={"full_evidence_pack": True}, guest_language=GUEST_LANGUAGE,
                             threshold=70, currency="EUR", ingested_on="2026-08-21")
    assert packet["recommendation"]["verdict"] == "hold"
    assert "cancellation policy is still the shipped example" in packet["recommendation"]["text"]
    assert any("cancellation-policy.md is still the shipped example" in w
              for w in packet["warnings"])
