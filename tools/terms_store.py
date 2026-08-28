"""tools/terms_store.py - the record that a guest accepted the cancellation
terms at booking, and which version of the terms they saw.

Not a core adapter (yet) - see docs/how-it-works.md, design decision 2 and
`tools/dispute_feed.py`'s own docstring for why a small reader lives here
instead. A real deployment reads this from your booking engine's own
acceptance log (timestamp, IP, checkout page version) - see
docs/integrations.md#implement-your-own.

The policy TEXT itself lives in `knowledge/cancellation-policy.md`, not
here. This module only answers "did THIS reservation accept it, and when" -
the acceptance record is the actual evidentiary gap the source spec flagged
("signatures are in the promise and absent everywhere"), not the wording of
the policy, which is always available.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Settings, repo_root, sub_data_dir


@dataclass
class TermsStore:
    """A lookup table of acceptance records, keyed by reservation reference."""

    rows: dict[str, dict] = field(default_factory=dict)

    def lookup(self, reservation_ref: str | None) -> dict | None:
        if not reservation_ref:
            return None
        return self.rows.get(str(reservation_ref).strip().upper())

    def __len__(self) -> int:
        return len(self.rows)


def _index(records: list[dict]) -> TermsStore:
    rows: dict[str, dict] = {}
    for r in records:
        ref = str(r.get("reservation_ref") or "").strip().upper()
        if not ref:
            continue
        rows[ref] = {
            "reservation_ref": ref,
            "accepted_at": str(r.get("accepted_at", "")),
            "ip": str(r.get("ip", "")),
            "checkout_page_version": str(r.get("checkout_page_version", "")),
            "policy_version": str(r.get("policy_version", "")),
        }
    return TermsStore(rows=rows)


def load_terms_store(settings: Settings) -> TermsStore:
    """Read the acceptance ledger named by ``config/agent.yaml: terms_store.adapter``.

    ``mock`` reads ``fixtures/hotel/terms-acceptance.json`` - what `make demo`
    and the tests use. ``csv`` reads ``data/imports/terms_acceptance.csv`` -
    an export from your booking engine. Either way, a missing file returns an
    empty store: a reservation with no acceptance record on file is a normal,
    handled outcome (the packet says so plainly, see `tools/engine.py`), not
    a crash.
    """
    name = str(settings.agent_get("terms_store.adapter", "mock") or "mock").lower()
    if name == "csv":
        path = sub_data_dir("imports") / "terms_acceptance.csv"
        if not path.exists():
            return TermsStore()
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return _index([dict(row) for row in csv.DictReader(fh)])

    path = repo_root() / "fixtures" / "hotel" / "terms-acceptance.json"
    if not path.exists():
        return TermsStore()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TermsStore()
    return _index(records if isinstance(records, list) else [])
