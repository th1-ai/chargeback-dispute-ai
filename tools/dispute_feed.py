"""tools/dispute_feed.py - where a chargeback case first shows up.

Not a core adapter (yet). Nothing in `core/adapters/base.py` models "a card
network opened a dispute against you" - the closest thing,
`core.adapters.get_stub("payments", settings)`, is a deliberate stub (see
docs/how-it-works.md, "Being honest about payments"). This module is the
same shape `tools/po_ledger.py` in `finance-filing-ai` uses for the same
reason: a small, honest reader with a mock mode (zero credentials) and a csv
mode (works with any processor's export), not a fabricated live webhook.

A real deployment wants Stripe's `charge.dispute.created` (or Adyen's
NOTIFICATION_OF_CHARGEBACK) landing here instead - see
docs/integrations.md#implement-your-own for the recipe. That is a genuine
integration to build, not something a template can honestly claim already
works.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from core.config import Settings, repo_root, sub_data_dir


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalise(raw: dict) -> dict | None:
    dispute_id = str(raw.get("id") or raw.get("dispute_ref") or "").strip()
    if not dispute_id:
        return None
    return {
        "id": dispute_id,
        "guest_name": str(raw.get("guest_name") or "").strip(),
        "reservation_ref": str(raw.get("reservation_ref") or "").strip(),
        "comms_conversation_id": str(raw.get("comms_conversation_id") or "").strip(),
        "amount": _float(raw.get("amount") or raw.get("amount_eur")),
        "reason_code": str(raw.get("reason_code") or "").strip(),
        "card_scheme": str(raw.get("card_scheme") or "").strip(),
        "deadline_offset": _int(raw.get("deadline_offset"), 7),
        "venue": str(raw.get("venue") or "hotel").strip().lower() or "hotel",
    }


def load_disputes(settings: Settings) -> list[dict]:
    """Read every case named by ``config/agent.yaml: dispute_feed.adapter``.

    ``mock`` reads one dispute per file from ``fixtures/inbound/dispute-*.json``
    - what `make demo` and the tests use, the same one-file-per-item shape
    every other agent in this family uses for its inbound fixtures. ``csv``
    reads ``data/imports/disputes.csv``, one row per case, headers matched
    loosely like every CSV reader in this family (`amount`/`amount_eur`,
    `dispute_ref`/`id`). A missing source returns an empty list rather than
    raising: no new disputes today is a normal, handled outcome.
    """
    name = str(settings.agent_get("dispute_feed.adapter", "mock") or "mock").lower()
    records: list[dict] = []
    if name == "csv":
        path = sub_data_dir("imports") / "disputes.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            records = [dict(row) for row in csv.DictReader(fh)]
    else:
        directory = repo_root() / "fixtures" / "inbound"
        for p in sorted(directory.glob("dispute-*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(raw, dict):
                records.append(raw)
    out = []
    for raw in records:
        norm = _normalise(raw)
        if norm is not None:
            out.append(norm)
    return out
