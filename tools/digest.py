#!/usr/bin/env python3
"""tools/digest.py - build (and queue) the daily dispute-queue summary email.

    python3 tools/digest.py
    python3 tools/digest.py --date 2026-08-27

Gathers every open dispute, flags anything inside
`config/agent.yaml: sla.alert_days_before` (default 3 days) of its scheme
deadline, and queues a plain-text summary as a `kind="digest"` item - the
same review FSM as a dispute, so it needs the same `approve` + `send`
before it goes out (`review.require_approval_for: send_email` by default).
One digest per calendar day: re-running the same day updates the queued
draft instead of creating a second one.

When something is inside the alert window, this also tries
`messaging.notify_staff()` - guarded like any other write, so in
`mode: shadow` it is blocked and just logged, never sent.

The optional cosmetic case-manager note (`tools/narrate.py`,
`narrate.enabled` in config/agent.yaml, off by default) is appended if it is
switched on; a failure there never blocks the digest itself.

Exit codes: 0 ok (queued), 3 waiting on an `interactive` answer, 1 a real error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_messaging  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402
from engine import fmt_money  # noqa: E402

log = get_logger("digest")


def gather_open_disputes(store: Store, settings) -> dict:
    import json as _json

    def draft_of(row) -> dict:
        try:
            return _json.loads(row["draft_json"]) if row["draft_json"] else {}
        except (TypeError, ValueError):
            return {}

    rows = store.db.execute("SELECT * FROM items WHERE kind='dispute'").fetchall()
    open_rows = [r for r in rows if r["review_status"] not in ("sent", "rejected")]
    submitted_rows = [r for r in rows if r["review_status"] == "sent"]
    open_amount = sum(float(draft_of(r).get("amount") or 0) for r in open_rows)
    alert_days = int(settings.agent_get("sla.alert_days_before", 3))

    urgent = []
    for r in open_rows:
        d = draft_of(r)
        days_left = d.get("days_remaining")
        if days_left is not None and days_left <= alert_days:
            urgent.append({"id": d.get("dispute_id"), "amount": d.get("amount"),
                           "currency": d.get("currency"), "days_remaining": days_left})
    urgent.sort(key=lambda u: u["days_remaining"])

    return {"open": len(open_rows), "open_amount": round(open_amount, 2),
            "submitted": len(submitted_rows), "urgent": urgent,
            "currency": settings.hotel.currency}


def build_body(hotel_name: str, stats: dict, narrative: str | None) -> str:
    lines = [
        f"Chargeback & Dispute AI - dispute queue for {hotel_name}", "",
        f"Open disputes: {stats['open']} ({fmt_money(stats['open_amount'], stats['currency'])} "
        f"at stake).",
        f"Submitted, awaiting a ruling: {stats['submitted']}.",
    ]
    if stats["urgent"]:
        lines += ["", "Inside the SLA alert window:"]
        for u in stats["urgent"]:
            lines.append(f"  - {u['id']}: {fmt_money(u['amount'], u['currency'])}, "
                         f"{u['days_remaining']} day(s) left")
    else:
        lines += ["", "Nothing is inside the SLA alert window."]
    if narrative:
        lines += ["", narrative]
    lines += ["", "Run `python3 tools/review.py list` for the full queue."]
    return "\n".join(lines)


def build_digest(settings, store: Store, *, day: str | None = None) -> dict:
    day = day or date.today().isoformat()
    with Run("dispute-digest", settings, store) as run:
        stats = gather_open_disputes(store, settings)
        narrative = None
        if settings.agent_get("narrate.enabled", False):
            from narrate import build_narrative  # local import: optional dependency
            try:
                narrative = build_narrative(settings, store, stats)
            except LLMPendingInteractive:
                # Deliberately NOT caught below: a pending interactive prompt
                # must reach the user, never be swallowed as "no note this
                # time" - see core/llm.py's LLMPendingInteractive docstring.
                raise
            except Exception:  # noqa: BLE001 - any OTHER failure must never block the digest
                log.warn("case note skipped")
                narrative = None

        to = [settings.contacts.escalation_email] if settings.contacts.escalation_email else []
        body = build_body(settings.hotel.name, stats, narrative)
        payload = {"date": day, "to": to, "stats": stats}
        draft = {"subject": f"{settings.hotel.name} - dispute queue, {day}", "body": body}
        item, created = store.upsert_unique("digest", day, payload=payload, source="digest")
        if item.review_status in ("new", "pending_review"):
            # Only overwrite the draft while a human has not yet acted on it.
            store.set_fields(item.id, draft=draft, intent="digest")
        if item.review_status == "new":
            item = store.transition(item.id, "pending_review", actor="agent",
                                    detail={"open": stats["open"], "urgent": len(stats["urgent"])})

        if stats["urgent"]:
            try:
                messaging = get_messaging(settings)
                names = ", ".join(u["id"] for u in stats["urgent"])
                messaging.notify_staff(f"{len(stats['urgent'])} dispute(s) inside the SLA "
                                       f"alert window: {names}. See `make review`.")
            except (WriteBlocked, AdapterError) as exc:
                # A blocked or unconfigured staff channel never blocks the
                # digest itself - the email queued above still carries the
                # same information.
                log.info("staff alert not sent", reason=str(exc)[:200])

        run.stats = {"item_id": item.id, "created": created, **stats}
        log.info("digest built", item_id=item.id, open=stats["open"], urgent=len(stats["urgent"]))
    return {"item_id": item.id, "created": created, "stats": stats}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default today")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        try:
            result = build_digest(settings, store, day=args.date)
        except LLMPendingInteractive as exc:
            print(str(exc))
            return 3
        stats = result["stats"]
        print(f"digest {result['item_id']} queued: {stats['open']} open "
             f"({fmt_money(stats['open_amount'], stats['currency'])}), "
             f"{len(stats['urgent'])} inside the SLA alert window.")
        print("Approve it with `python3 tools/review.py approve <id>`, then "
             "`python3 tools/review.py send`.")
        return 0
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
