#!/usr/bin/env python3
"""tools/review.py - work the review queue: list / show / approve / edit / reject / send.

    python3 tools/review.py list [--status needs_human]
    python3 tools/review.py show <id>
    python3 tools/review.py approve <id> [--note "..."]
    python3 tools/review.py edit <id> --body-file recommendation.txt [--verdict represent] [--note "..."]
    python3 tools/review.py reject <id> --reason "conceding this one"
    python3 tools/review.py retry <id>          # re-queue a failed submission
    python3 tools/review.py send                # file everything approved/edited
    python3 tools/review.py outcome <id> --result won|lost|won_in_part [--recovered 940.00]
    python3 tools/review.py stale                # go-live step: see below

Add `--demo` to `list` / `show` to read `data/demo/demo.db` (built by
`make demo`) instead of your real queue in `data/agent.db` - `make demo`
always runs on the shipped example scenario, in its own isolated database,
so this is the only way to inspect one of those packets; see README.md,
"Quick start". `approve` / `edit` / `reject` / `send` do not take `--demo`:
the demo queue is read-only, on purpose - work the real queue instead.

Only this tool writes `approved` / `edited` / `rejected` (core/review.py).
Only `send` writes `sending` / `sent`. Nothing here bypasses `mode: shadow` -
see docs/safety.md. Every dispute needs this: the roster promise is
explicit that this agent "won't submit without human review".

`stale` is a `workflows/90-go-live.md` step: it moves every item still
waiting (held, approved or edited) to `stale` so nothing built up while you
were only testing in shadow goes out by surprise the moment you flip to
`live`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import get_logger  # noqa: E402
from core.review import (WriteBlocked, approve, edit, list_queue, reject,  # noqa: E402
                         retry, show, stale_backlog)
from core.store import Store, StoreError, utcnow  # noqa: E402
from engine import finalize_dispute, record_outcome  # noqa: E402

log = get_logger("review")


def _print_item_line(item) -> None:
    draft = item.draft or {}
    if item.kind == "digest":
        label = draft.get("subject", "dispute-queue digest")
    else:
        payload = item.payload or {}
        label = (f"{draft.get('guest_name', payload.get('guest_name', '?'))} "
                f"{draft.get('amount', '?')} {draft.get('currency', '')} "
                f"verdict={draft.get('recommendation', {}).get('verdict', '-')} "
                f"days_left={draft.get('days_remaining', '?')}")
    # Show the dispute/digest's own reference (e.g. GM-20138, or a digest's
    # own date) - the id a person actually recognises, not the internal
    # store id. `show`/`approve`/etc. below accept either - see _resolve_id.
    shown_id = item.external_id or item.id
    marker = " [SAMPLE DATA]" if item.is_sample else ""
    print(f"  {shown_id}  {item.review_status:<14} {item.kind:<8} {label[:60]}{marker}")


def _resolve_id(store, raw_id: str) -> str:
    """Accept either an item's own internal id, or its business reference -
    the dispute id `make demo`/`make run` print (e.g. GM-20138) or a
    digest's own date - the ids `list` now shows and a person actually
    types. See SIMULATION.md finding 2's reproduction: a stranger reusing
    the id `make demo` just showed them must not hit "no item <id>"."""
    if store.get_item(raw_id) is not None:
        return raw_id
    for source in ("disputes", "digest"):
        found = store.get_by_external(source, raw_id)
        if found is not None:
            return found.id
    return raw_id


def cmd_list(store, args) -> int:
    items = list_queue(store, status=args.status, kind=args.kind, limit=args.limit)
    if not items:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(items)} item(s) waiting:\n")
    for item in items:
        _print_item_line(item)
    if any(item.is_sample for item in items):
        print("\n[SAMPLE DATA] One or more items above were built from the shipped "
             "sample fixtures, not your property - the system they were read from is "
             "still on the `mock` adapter. Connect your real systems "
             "(docs/integrations.md) before approving them.")
    print("\nRun `python3 tools/review.py show <id>` for the full packet.")
    return 0


def cmd_show(store, args) -> int:
    try:
        detail = show(store, args.id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = (detail.get("item") or {}).get("payload") or {}
    if payload.get("_sample"):
        print("[SAMPLE DATA] This packet was built from the shipped sample fixtures, not "
             "your property - the system it was read from is still on the `mock` adapter. "
             "Connect your real systems (docs/integrations.md) before submitting it to "
             "the bank.\n")
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_approve(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    if not item.draft:
        print(f"error: {args.id} has no packet built yet", file=sys.stderr)
        return 1
    approve(store, args.id, note=args.note or "")
    log.info("approved", item_id=args.id, actor="human", note=args.note or None)
    print(f"approved {args.id} - now in the send queue")
    return 0


def cmd_edit(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    new_draft = dict(item.draft or {})
    rec = dict(new_draft.get("recommendation") or {})
    if args.body_file:
        text = Path(args.body_file).read_text(encoding="utf-8").strip()
        rec["text"] = text
        new_draft["body"] = text  # lets core.review.edit() diff before/after
    if args.verdict:
        rec["verdict"] = args.verdict
    new_draft["recommendation"] = rec
    edit(store, args.id, new_draft, note=args.note or "")
    log.info("edited", item_id=args.id, actor="human", note=args.note or None,
             verdict=args.verdict)
    print(f"edited {args.id} - now in the send queue")
    return 0


def cmd_reject(store, args) -> int:
    reject(store, args.id, reason=args.reason or "")
    log.info("rejected", item_id=args.id, actor="human", reason=args.reason or None)
    print(f"rejected {args.id}")
    return 0


def cmd_retry(store, args) -> int:
    retry(store, args.id)
    log.info("retry_queued", item_id=args.id, actor="human")
    print(f"queued {args.id} for another submission attempt")
    return 0


def cmd_stale(store, args) -> int:
    moved = stale_backlog(store)
    log.info("stale_backlog", actor="human", moved=len(moved))
    print(f"marked {len(moved)} item(s) stale. Nothing from before go-live will submit.")
    return 0


def cmd_outcome(store, args) -> int:
    try:
        item = record_outcome(store, args.id, args.result, args.recovered)
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    draft = item.draft or {}
    log.info("outcome_recorded", item_id=args.id, actor="human", result=args.result,
             recovered=draft.get("recovered_amount"))
    print(f"recorded {args.id}: {args.result}, recovered "
         f"{draft.get('recovered_amount')} {draft.get('currency', '')}")
    return 0


def cmd_send(store, settings, args) -> int:
    claimed = store.claim_for_send(limit=args.limit)
    if not claimed:
        print("Nothing approved or edited is waiting to send.")
        return 0
    sent, blocked, failed = 0, 0, 0
    email = None
    for item in claimed:
        try:
            if item.kind == "digest":
                if email is None:
                    email = get_email(settings)
                draft = item.draft or {}
                payload = item.payload or {}
                result = email.send(payload.get("to") or [], draft.get("subject", ""),
                                    draft.get("body", ""), item=item)
                store.mark_sent(item.id, result.get("message_id"))
                log.info("sent", item_id=item.id, actor="human",
                        message_id=result.get("message_id"))
                print(f"sent {item.id}")
                sent += 1
                continue
            result = finalize_dispute(settings, item)
        except WriteBlocked as exc:
            # Not a failure: the mode blocked it. The approval stands for go-live.
            store.transition(item.id, "approved", "agent", {"blocked": str(exc)[:200]})
            log.warn("blocked_send", item_id=item.id, actor="human", reason=str(exc))
            print(f"blocked {item.id} (approval kept): {exc}")
            blocked += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record and move on, never crash the batch
            store.mark_send_failed(item.id, str(exc))
            log.error("send_failed", item_id=item.id, actor="human", error=str(exc))
            print(f"failed {item.id}: {exc}")
            failed += 1
            continue
        new_draft = {**(item.draft or {}), "submitted_at": utcnow(),
                    "filed_path": result["filed_path"]}
        store.set_fields(item.id, draft=new_draft)
        store.mark_sent(item.id, result["filed_path"])
        log.info("submitted", item_id=item.id, actor="human", filed=result["filed_path"])
        print(f"submitted {item.id} -> {result['filed_path']}")
        sent += 1
    print(f"\n{sent} sent/submitted, {blocked} blocked (approval kept), {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    demo_parent = argparse.ArgumentParser(add_help=False)
    demo_parent.add_argument(
        "--demo", action="store_true",
        help="read data/demo/demo.db (built by `make demo`) instead of your real queue")

    p_list = sub.add_parser("list", parents=[demo_parent], help="what is waiting for a human")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None, help="dispute | digest")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", parents=[demo_parent], help="full detail for one item")
    p_show.add_argument("id")

    p_approve = sub.add_parser("approve", help="approve the packet unchanged")
    p_approve.add_argument("id")
    p_approve.add_argument("--note", default="")

    p_edit = sub.add_parser("edit", help="rewrite the recommendation text, then queue it")
    p_edit.add_argument("id")
    p_edit.add_argument("--body-file", default=None, help="replacement recommendation text")
    p_edit.add_argument("--verdict", default=None, choices=["represent", "hold"])
    p_edit.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="discard the packet - conceding this dispute")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default="")

    p_retry = sub.add_parser("retry", help="re-queue a failed submission")
    p_retry.add_argument("id")

    p_send = sub.add_parser("send", help="file everything approved or edited")
    p_send.add_argument("--limit", type=int, default=20)

    p_outcome = sub.add_parser("outcome", help="record the real bank ruling on a submitted dispute")
    p_outcome.add_argument("id")
    p_outcome.add_argument("--result", required=True, choices=["won", "lost", "won_in_part"])
    p_outcome.add_argument("--recovered", type=float, default=None,
                           help="amount actually recovered (default: the full amount if won, "
                                "0 if lost)")

    sub.add_parser("stale", help="go-live step: mark every waiting item stale")

    args = parser.parse_args(argv)
    use_demo = bool(getattr(args, "demo", False))

    try:
        settings = load_settings(demo=use_demo)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    if use_demo:
        demo_db = sub_data_dir("demo") / "demo.db"
        if not demo_db.exists():
            print("no demo data yet - run `make demo` first", file=sys.stderr)
            return 1
        store = Store(settings, path=demo_db)
    else:
        store = Store(settings)
    if hasattr(args, "id"):
        args.id = _resolve_id(store, args.id)
    try:
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "approve":
            return cmd_approve(store, args)
        if args.command == "edit":
            return cmd_edit(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "retry":
            return cmd_retry(store, args)
        if args.command == "send":
            return cmd_send(store, settings, args)
        if args.command == "outcome":
            return cmd_outcome(store, args)
        if args.command == "stale":
            return cmd_stale(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        log.warn("blocked", actor="human", command=args.command, reason=str(exc))
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
