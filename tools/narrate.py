#!/usr/bin/env python3
"""tools/narrate.py - an optional, cosmetic case-manager note for the digest.

    python3 tools/narrate.py

The ONLY place this repo calls a model at all (see docs/how-it-works.md,
"The central design choice") - one short paragraph for a person, appended
to the daily digest by tools/digest.py when config/agent.yaml:
narrate.enabled is true (off by default). It never sees a packet's legal
text, never changes a score or a recommendation, and is read only after the
fact.

`build_narrative()` is what tools/digest.py calls; running this file
directly checks the prompt on its own, against the current queue. Exit
codes: 0 ok, 3 waiting on an `interactive` answer, 1 a real error. A schema
or provider error here never blocks the digest itself - tools/digest.py
catches any exception from `build_narrative()` and just sends the digest
without a note.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive, complete  # noqa: E402
from core.store import Store  # noqa: E402
from core.templates import build_prompt  # noqa: E402

SCHEMA = json.loads((REPO_ROOT / "prompts" / "schemas" / "narrate.json").read_text())


def build_narrative(settings, store: Store, stats: dict) -> str | None:
    """Return the case-manager note, or ``None`` if narrate is off."""
    if not settings.agent_get("narrate.enabled", False):
        return None
    prompt = build_prompt("narrate", settings=settings, item=stats,
                          knowledge=settings.agent_get("narrate.knowledge"),
                          fixture_id="controller-note")
    result = complete("narrate", prompt, SCHEMA, settings=settings, store=store,
                      fixture_id="controller-note")
    return result.data["narrative"]


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if not settings.agent_get("narrate.enabled", False):
        print("narrate.enabled is false in config/agent.yaml - nothing to do. "
             "This is optional and off by default; see docs/how-it-works.md.")
        return 0

    store = Store(settings)
    try:
        from digest import gather_open_disputes  # local import: avoids a circular import
        stats = gather_open_disputes(store, settings)
        try:
            narrative = build_narrative(settings, store, stats)
        except LLMPendingInteractive as exc:
            print(str(exc))
            return 3
        print(narrative)
        return 0
    except LLMError as exc:
        print(f"note skipped: {exc}", file=sys.stderr)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
