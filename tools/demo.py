#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled disputes, zero credentials.

    make demo
    python3 tools/demo.py

Loads `load_settings(demo=True)`, which forces `mock` provider, `shadow`
mode and the `mock` adapter for every system regardless of config/hotel.yaml
(ARCHITECTURE.md section 1, "works in 5 minutes with zero credentials"), and
runs against its own database (data/demo/demo.db), never data/agent.db.
Running it twice always shows the same five bundled disputes.

Prints one line every check reads for the pass/fail signal:

    DEMO OK — 5 items processed, 5 drafted, 0 sent (shadow)
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store  # noqa: E402
from dispute_feed import load_disputes  # noqa: E402
from engine import fmt_money, process_dispute  # noqa: E402
from terms_store import load_terms_store  # noqa: E402


def main() -> int:
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()  # every `make demo` is a clean, repeatable run
    store = Store(settings, path=demo_db)

    pms = get_pms(settings)
    email_adapter = get_email(settings)
    terms_store = load_terms_store(settings)
    disputes = load_disputes(settings)
    if not disputes:
        print("no fixtures found in fixtures/inbound/ - nothing to demo", file=sys.stderr)
        return 1

    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0}
    print(f"Chargeback & Dispute AI demo - {len(disputes)} sample case(s) from "
         f"fixtures/inbound/\n")
    for raw in disputes:
        item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                                  terms_store=terms_store)
        draft = item.draft or {}
        stats["processed"] += 1
        stats["drafted"] += 1
        if item.review_status == "needs_human":
            stats["needs_human"] += 1
        print(f"  {raw['id']}: {raw.get('guest_name', '?')} "
             f"{fmt_money(raw.get('amount'), settings.hotel.currency)} -> "
             f"verdict={item.intent} strength={draft.get('evidence_strength', 0):.0f}/100 "
             f"days_left={draft.get('days_remaining', '?')} status={item.review_status}")

    print(f"\n{stats['needs_human']} of {stats['processed']} need a person to decide before "
         f"submitting - see docs/safety.md. Every case needs a human either way: this agent "
         f"never submits a representment on its own.")
    print("Nothing was filed: mode is shadow, and demo never calls finalize_dispute() at all.")
    print("Next: `make review ARGS=\"--demo\"` to see these packets (this demo always runs on\n"
         "its own sample data - your real config/agent.yaml applies to `make run`, not to\n"
         "`make demo`). Once you connect your real systems: `make run` builds packets from\n"
         "your own queue, then plain `make review` works that one.\n")

    print(f"DEMO OK — {summary_line(stats, settings.mode)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
