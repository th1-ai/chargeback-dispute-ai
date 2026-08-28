#!/usr/bin/env python3
"""tools/doctor.py - is Chargeback & Dispute AI configured and reachable now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus
this agent's own: the reason-code map, the cancellation-policy knowledge
file, and the dispute feed / terms store readers. Exits 0 when everything
passed, 1 when a FAIL line needs fixing. Never a traceback.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402
from dispute_feed import load_disputes  # noqa: E402
from terms_store import load_terms_store  # noqa: E402


def check_reason_code_map(settings: Settings) -> Check:
    m = settings.agent_get("reason_code_map", {})
    if not m or "default" not in m:
        return Check("reason code map", FAIL,
                     "config/agent.yaml: reason_code_map is missing its 'default' entry",
                     "Copy config/agent.example.yaml to config/agent.yaml - it ships with "
                     "4853, 4855, 10.4, 13.1 and a default fallback.")
    codes = ", ".join(k for k in m if k != "default")
    return Check("reason code map", PASS, f"{len(m) - 1} code(s): {codes}, plus default")


def check_cancellation_policy() -> Check:
    real = REPO_ROOT / "knowledge" / "cancellation-policy.md"
    example = REPO_ROOT / "knowledge" / "cancellation-policy.example.md"
    if real.is_file():
        # A file that exists but was never actually edited is worse than one
        # missing outright: every packet quotes it, verbatim, to a bank - see
        # SIMULATION.md finding 5. Compare content hashes, the same way
        # check_hotel_identity() catches an unedited placeholder name.
        if example.is_file() and (hashlib.sha256(real.read_bytes()).hexdigest()
                                  == hashlib.sha256(example.read_bytes()).hexdigest()):
            return Check("cancellation policy", FAIL,
                         "knowledge/cancellation-policy.md is still the shipped example, "
                         "word for word",
                         "Replace the wording with your real deposit and cancellation terms, "
                         "verbatim - every packet quotes this text to a bank. "
                         "tools/engine.py also refuses to represent while this is true.")
        return Check("cancellation policy", PASS, "knowledge/cancellation-policy.md")
    if example.is_file():
        return Check("cancellation policy", WARN, "using the shipped example",
                     "Copy knowledge/cancellation-policy.example.md to "
                     "knowledge/cancellation-policy.md and put your real terms in it.")
    return Check("cancellation policy", FAIL, "no cancellation-policy file at all",
                 "This ships with the repo - restore it from git.")


def check_dispute_feed(settings: Settings) -> Check:
    try:
        disputes = load_disputes(settings)
    except Exception as exc:  # noqa: BLE001 - doctor must always print a table
        return Check("dispute feed", FAIL, f"could not read: {exc}"[:160],
                     "Check config/agent.yaml: dispute_feed.adapter and data/imports/disputes.csv.")
    adapter = str(settings.agent_get("dispute_feed.adapter", "mock"))
    if not disputes:
        return Check("dispute feed", WARN, f"{adapter} adapter, 0 cases on file",
                     "Fine before you have connected a real feed. See docs/integrations.md.")
    return Check("dispute feed", PASS, f"{adapter} adapter, {len(disputes)} case(s) on file")


def check_terms_store(settings: Settings) -> Check:
    try:
        store = load_terms_store(settings)
    except Exception as exc:  # noqa: BLE001
        return Check("terms store", FAIL, f"could not read: {exc}"[:160], "")
    adapter = str(settings.agent_get("terms_store.adapter", "mock"))
    if len(store) == 0:
        return Check("terms store", WARN, f"{adapter} adapter, 0 acceptance records",
                     "Packets will say 'no acceptance record is on file' for every booking "
                     "until this is connected - see docs/integrations.md.")
    return Check("terms store", PASS, f"{adapter} adapter, {len(store)} acceptance record(s)")


def check_prompts() -> Check:
    missing = [p for p in ("prompts/narrate.md", "prompts/schemas/narrate.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "narrate.md + schema present (the one optional LLM call)")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="Chargeback & Dispute AI - doctor")

    checks = run_checks(settings, extra=[check_reason_code_map, check_dispute_feed,
                                         check_terms_store])
    checks.append(check_cancellation_policy())
    checks.append(check_prompts())
    return print_table(checks, title="Chargeback & Dispute AI - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
