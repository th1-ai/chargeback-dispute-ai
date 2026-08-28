"""A packet built from the shipped fixtures must never look like a real one.

Every `config/*.example.yaml` ships with `systems.*.adapter: mock`, so a hotel
that runs the real loop on a fresh clone - before connecting a PMS or a
mailbox - gets invented evidence. `core.store.Store.upsert_item` tags those
items `_sample: True` (via `core.adapters.is_sample_source`) and
`item.is_sample` reads it back; this repo does not re-implement the tagging,
it only has to SHOW it. These tests pin that `make review` does:

  * `list` - a `[SAMPLE DATA]` marker on the item's own line, plus one
    footer line explaining why,
  * `show` - a `[SAMPLE DATA]` banner above the packet JSON, before anyone
    approves it for submission to a bank.

`tests/conftest.py`'s autouse fixture points AGENT_CONFIG_DIR/AGENT_REPO_ROOT
at temp copies of the shipped examples, so `load_settings()` below is the
real (non-demo) path a fresh clone would take.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.config import load_settings  # noqa: E402
from core.store import Store  # noqa: E402
from review import cmd_list, cmd_show  # noqa: E402


def _waiting_sample_item(tmp_path):
    """One dispute packet whose evidence came from a `mock` adapter."""
    settings = load_settings()
    assert settings.systems.pms.adapter == "mock", "fresh clone default changed"
    store = Store(settings, path=tmp_path / "test.db")
    item = store.upsert_item(
        "pms", "GM-20138", kind="dispute",
        payload={"id": "GM-20138", "guest_name": "Sample Guest", "amount": 940.00,
                 "currency": "EUR", "reason_code": "13.1",
                 "reservation_ref": "RES-4471", "days_remaining": 6})
    store.set_fields(item.id, draft={"guest_name": "Sample Guest", "amount": 940.00,
                                     "currency": "EUR", "days_remaining": 6,
                                     "recommendation": {"verdict": "represent"}})
    store.transition(item.id, "pending_review", actor="agent")
    return store, store.get_item(item.id)


def test_mock_read_item_is_tagged_sample(tmp_path):
    store, item = _waiting_sample_item(tmp_path)
    try:
        assert item.is_sample is True
    finally:
        store.close()


def test_list_marks_the_sample_packet(tmp_path, capsys):
    store, item = _waiting_sample_item(tmp_path)
    try:
        args = SimpleNamespace(status=None, kind=None, limit=50)
        assert cmd_list(store, args) == 0
    finally:
        store.close()
    out = capsys.readouterr().out
    assert "[SAMPLE DATA]" in out
    # ...on the item's own line, not only in the footer.
    line = next(ln for ln in out.splitlines() if "GM-20138" in ln)
    assert "[SAMPLE DATA]" in line
    assert "docs/integrations.md" in out


def test_show_warns_before_the_packet_json(tmp_path, capsys):
    store, item = _waiting_sample_item(tmp_path)
    try:
        assert cmd_show(store, SimpleNamespace(id=item.id)) == 0
    finally:
        store.close()
    out = capsys.readouterr().out
    assert "[SAMPLE DATA]" in out
    assert out.index("[SAMPLE DATA]") < out.index("{"), "banner must precede the JSON"
