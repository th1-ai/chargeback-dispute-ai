#!/usr/bin/env python3
"""tools/report.py - what the agent did, and the win rate it produced.

    make report
    python3 tools/report.py
    python3 tools/report.py --export

The roster's promise is "+35% dispute win-rate; recover a share of what's
currently written off." This prints the numbers that let you check that
promise against what actually happened - but only once you have recorded
real outcomes with `python3 tools/review.py outcome`. Until then, win rate
is honestly `None`, not a made-up percentage - see docs/how-it-works.md,
design decision 8.

`--export` also writes the same row to `systems.sheets.adapter` (csv by
default: `data/exports/chargeback_dispute_report.csv`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_sheets  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402
from engine import fmt_money  # noqa: E402


def _draft(row) -> dict:
    try:
        return json.loads(row["draft_json"]) if row["draft_json"] else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def gather(store: Store) -> dict:
    rows = store.db.execute("SELECT * FROM items WHERE kind='dispute'").fetchall()
    submitted = [r for r in rows if r["review_status"] == "sent"]
    open_rows = [r for r in rows if r["review_status"] not in ("sent", "rejected")]
    decided = [r for r in submitted if _draft(r).get("outcome")]
    won = [r for r in decided if _draft(r).get("outcome") in ("won", "won_in_part")]
    lost = [r for r in decided if _draft(r).get("outcome") == "lost"]

    disputed_total = sum(float(_draft(r).get("amount") or 0) for r in decided)
    recovered_total = sum(float(_draft(r).get("recovered_amount") or 0) for r in decided)
    open_amount = sum(float(_draft(r).get("amount") or 0) for r in open_rows)
    win_rate = round(100 * len(won) / len(decided), 1) if decided else None

    ages = []
    for r in submitted:
        d = _draft(r)
        if not d.get("submitted_at"):
            continue
        try:
            created = datetime.fromisoformat(r["created_at"])
            sent_at = datetime.fromisoformat(d["submitted_at"])
            ages.append((sent_at - created).total_seconds() / 86400)
        except (TypeError, ValueError):
            continue

    cost_usd = 0.0
    for row in store.db.execute(
        "SELECT detail_json FROM events WHERE action='llm_call'").fetchall():
        try:
            cost_usd += float((json.loads(row["detail_json"]) or {}).get("cost_usd") or 0.0)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    return {
        "total": len(rows), "open": len(open_rows), "open_amount": round(open_amount, 2),
        "submitted": len(submitted), "decided": len(decided), "won": len(won), "lost": len(lost),
        "win_rate_pct": win_rate, "disputed_total": round(disputed_total, 2),
        "recovered_total": round(recovered_total, 2),
        "avg_days_to_submit": round(sum(ages) / len(ages), 1) if ages else None,
        "llm_cost_usd": round(cost_usd, 4), "by_status": store.counts(),
    }


def print_report(stats: dict, mode: str, currency: str) -> None:
    print("Chargeback & Dispute AI - report\n")
    print(f"  Disputes seen so far:     {stats['total']}")
    print(f"  Open (not yet decided):   {stats['open']} ({fmt_money(stats['open_amount'], currency)})")
    print(f"  Submitted:                {stats['submitted']}")
    if stats["decided"]:
        print(f"  Ruled on so far:          {stats['decided']} ({stats['won']} won, "
             f"{stats['lost']} lost)")
        print(f"  Win rate:                 {stats['win_rate_pct']}%")
        print(f"  Amount disputed (ruled):  {fmt_money(stats['disputed_total'], currency)}")
        print(f"  Amount recovered:         {fmt_money(stats['recovered_total'], currency)}")
    else:
        print("  Win rate:                 not measurable yet - no outcomes recorded. "
             "Run `python3 tools/review.py outcome <id> --result won|lost|won_in_part` once "
             "your bank rules on a case.")
    avg = stats["avg_days_to_submit"]
    print(f"  Average days to submit:  {avg}" if avg is not None
         else "  Average days to submit:  nothing submitted yet")
    print(f"  LLM spend so far:        ${stats['llm_cost_usd']} (the optional case-manager "
         f"note only - the packet itself never calls a model)")
    print("\n  By status: " + ", ".join(f"{k}={v}" for k, v in sorted(stats["by_status"].items())))
    print(f"\n  Mode: {mode}. Nothing is ever submitted without a human clicking send - see "
         f"docs/safety.md.")


def export_csv(settings, stats: dict) -> str:
    sheets = get_sheets(settings)
    header = ["generated_at", "total", "open", "open_amount", "submitted", "decided", "won",
             "lost", "win_rate_pct", "disputed_total", "recovered_total",
             "avg_days_to_submit", "llm_cost_usd"]
    row = [datetime.now(timezone.utc).isoformat(timespec="seconds"), stats["total"],
          stats["open"], stats["open_amount"], stats["submitted"], stats["decided"],
          stats["won"], stats["lost"], stats["win_rate_pct"] or "", stats["disputed_total"],
          stats["recovered_total"], stats["avg_days_to_submit"] or "", stats["llm_cost_usd"]]
    sheets.append("chargeback_dispute_report", [header, row])
    return "chargeback_dispute_report"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", action="store_true",
                        help="also write the numbers via systems.sheets.adapter")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    try:
        stats = gather(store)
        print_report(stats, settings.mode, settings.hotel.currency)
        if args.export:
            sheet = export_csv(settings, stats)
            print(f"\nExported to: {sheet} ({settings.systems.sheets.adapter} adapter)")
        return 0
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except WriteBlocked as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        print("--export writes via systems.sheets.adapter, which mode: shadow blocks like "
             "every other write. The numbers above are still accurate.", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
