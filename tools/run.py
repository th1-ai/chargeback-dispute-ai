#!/usr/bin/env python3
"""tools/run.py - Chargeback & Dispute AI's main loop: fetch -> build packet -> queue.

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --limit 5

One pass: read new cases from the dispute feed, skip anything already
packeted, build an evidence packet for each new one and queue it for a
human. This agent never sends anything on its own - see
docs/safety.md and workflows/80-review.md. There is no LLM call in this
loop at all (see docs/how-it-works.md), so unlike its siblings this command
never exits 3 waiting on an `interactive` answer.

Exit codes: 0 ok, 1 a real error.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email, get_pms  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402
from dispute_feed import load_disputes  # noqa: E402
from engine import process_dispute  # noqa: E402
from terms_store import load_terms_store  # noqa: E402

log = get_logger("run")


def one_pass(settings, store: Store, *, limit: int) -> tuple[int, dict]:
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "skipped": 0}
    with Run("dispute-packets", settings, store) as run:
        pms = get_pms(settings)
        email_adapter = get_email(settings)
        terms_store = load_terms_store(settings)
        disputes = load_disputes(settings)[:limit]
        seen = store.already_processed("disputes", [d["id"] for d in disputes])
        for raw in disputes:
            if raw["id"] in seen:
                stats["skipped"] += 1
                continue
            item, did_work = process_dispute(settings, store, raw, pms=pms,
                                             email_adapter=email_adapter,
                                             terms_store=terms_store)
            if not did_work:
                stats["skipped"] += 1
                continue
            stats["processed"] += 1
            stats["drafted"] += 1
            if item.review_status == "needs_human":
                stats["needs_human"] += 1
            log.info("queued", item_id=item.id, verdict=item.intent, status=item.review_status)
        reaped = store.reap_stuck_sending()
        if reaped:
            log.warn("reaped stuck sends", count=len(reaped))
        run.stats = dict(stats)
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=50, help="max disputes per pass")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 3600)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 3600))
            while True:
                code, stats = one_pass(settings, store, limit=args.limit)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, limit=args.limit)
        print(summary_line(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
