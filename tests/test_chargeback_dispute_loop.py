"""Tests for the full dispute-packet loop (tools/engine.py:process_dispute)
against the bundled fixtures, with provider=mock. No network, no credentials.

``_settings()`` never reads this repo's own `config/agent.yaml` or
`config/hotel.yaml` - it points `AGENT_CONFIG_DIR` at a tmp copy of the
shipped `.example.yaml` files instead, so a hotel's own edits never turn
`make test` red (factory/workflows/build-repo.md §5).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.adapters import get_email, get_pms  # noqa: E402
from core.config import load_settings, sub_data_dir  # noqa: E402
from core.review import WriteBlocked  # noqa: E402
from core.store import Store, StoreError  # noqa: E402
from dispute_feed import load_disputes  # noqa: E402
from engine import finalize_dispute, process_dispute, record_outcome  # noqa: E402
from terms_store import load_terms_store  # noqa: E402

EXPECTED_VERDICTS = {
    "GM-20138": "represent",  # full evidence, guest cancelled in writing
    "DP-3312": "hold",        # no comms thread on file
    "DP-4470": "hold",        # guest's own words describe a service problem
    "DP-5501": "hold",        # reservation missing from the PMS - degraded
    "DP-6100": "represent",   # restaurant event deposit, full evidence
}


def _settings(monkeypatch, tmp_path, mode: str = "shadow", provider: str = "mock"):
    cfg_dir = tmp_path / "example_config"
    cfg_dir.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "config" / "hotel.example.yaml", cfg_dir / "hotel.yaml")
    shutil.copy(REPO_ROOT / "config" / "agent.example.yaml", cfg_dir / "agent.yaml")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg_dir))
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    return load_settings(mode=mode, provider=provider)


def _readers(settings):
    return get_pms(settings), get_email(settings), load_terms_store(settings)


def test_five_fixtures_are_present(monkeypatch, tmp_path):
    disputes = load_disputes(_settings(monkeypatch, tmp_path))
    assert len(disputes) == 5
    assert {d["id"] for d in disputes} == set(EXPECTED_VERDICTS)


def test_every_fixture_gets_the_expected_verdict(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "loop.db")
    for raw in load_disputes(settings):
        item, did_work = process_dispute(settings, store, raw, pms=pms,
                                         email_adapter=email_adapter, terms_store=terms)
        assert did_work is True
        assert item.draft is not None
        assert item.draft["recommendation"]["verdict"] == EXPECTED_VERDICTS[raw["id"]]
    store.close()


def test_represent_verdicts_land_in_pending_review_hold_in_needs_human(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "loop2.db")
    statuses = {}
    for raw in load_disputes(settings):
        item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                                  terms_store=terms)
        statuses[raw["id"]] = item.review_status
    assert statuses["GM-20138"] == "pending_review"
    assert statuses["DP-6100"] == "pending_review"
    assert statuses["DP-3312"] == "needs_human"
    assert statuses["DP-4470"] == "needs_human"
    assert statuses["DP-5501"] == "needs_human"
    store.close()


def test_rerun_is_idempotent_and_does_not_reprocess(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "loop3.db")
    disputes = load_disputes(settings)
    for raw in disputes:
        process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                        terms_store=terms)
    for raw in disputes:
        item, did_work = process_dispute(settings, store, raw, pms=pms,
                                         email_adapter=email_adapter, terms_store=terms)
        assert did_work is False  # already packeted by the first pass
    assert len(store.list_items()) == 5
    store.close()


def test_shadow_mode_never_files_anything(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "loop4.db")
    item = None
    for raw in load_disputes(settings):
        item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                                  terms_store=terms)
        if item.review_status == "pending_review":
            break
    store.transition(item.id, "approved", "human")
    claimed = store.claim_for_send(limit=1)
    assert claimed and claimed[0].id == item.id
    try:
        finalize_dispute(settings, claimed[0])
        raised = False
    except WriteBlocked:
        raised = True
    assert raised is True
    counts = store.counts()
    assert counts.get("sent", 0) == 0
    store.close()


def test_dry_run_writes_no_db_rows_and_is_safe_to_repeat(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    settings.dry_run = True
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "dryrun.db")
    for raw in load_disputes(settings):
        item, did_work = process_dispute(settings, store, raw, pms=pms,
                                         email_adapter=email_adapter, terms_store=terms)
        assert did_work is True
        assert item.draft is not None
    assert store.counts() == {}  # nothing written, twice in a row is safe too
    for raw in load_disputes(settings):
        process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                        terms_store=terms)
    assert store.counts() == {}
    store.close()


def test_finalize_dispute_writes_the_packet_and_a_sheet_row_when_live_and_approved(
        monkeypatch, tmp_path):
    """The counterpart to test_shadow_mode_never_files_anything: in
    `mode: live`, an approved dispute really files a packet - the one write
    path this agent has - and logs a disputes-sheet row."""
    settings = _settings(monkeypatch, tmp_path, mode="live")
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "live.db")
    item = None
    for raw in load_disputes(settings):
        item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                                  terms_store=terms)
        if item.review_status == "pending_review":
            break
    store.transition(item.id, "approved", "human")
    claimed = store.claim_for_send(limit=1)
    result = finalize_dispute(settings, claimed[0])
    assert Path(result["filed_path"]).name.endswith(".md")
    # sub_data_dir() honours AGENT_REPO_ROOT - the autouse _isolated_repo
    # fixture in conftest.py points that at a tmp sandbox, never this repo's
    # own data/ - resolve the same way finalize_dispute() did, not against
    # the module-level REPO_ROOT (this repo's real, un-sandboxed root).
    filed = sub_data_dir("exports") / result["filed_path"]
    assert filed.exists()
    assert "Recommendation" in filed.read_text(encoding="utf-8")
    store.close()


def test_record_outcome_requires_the_item_to_already_be_sent(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "outcome.db")
    raw = next(d for d in load_disputes(settings) if d["id"] == "GM-20138")
    item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                              terms_store=terms)
    assert item.review_status == "pending_review"
    try:
        record_outcome(store, item.id, "won")
        raised = False
    except StoreError:
        raised = True
    assert raised is True
    store.close()


def test_record_outcome_records_a_partial_win_with_its_own_recovered_amount(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, mode="live")
    pms, email_adapter, terms = _readers(settings)
    store = Store(settings, path=tmp_path / "outcome2.db")
    raw = next(d for d in load_disputes(settings) if d["id"] == "GM-20138")
    item, _ = process_dispute(settings, store, raw, pms=pms, email_adapter=email_adapter,
                              terms_store=terms)
    store.transition(item.id, "approved", "human")
    claimed = store.claim_for_send(limit=1)
    finalize_dispute(settings, claimed[0])
    store.mark_sent(claimed[0].id, "evidence-packets/GM-20138.md")

    updated = record_outcome(store, item.id, "won_in_part", recovered_amount=470.0)
    assert updated.draft["outcome"] == "won_in_part"
    assert updated.draft["recovered_amount"] == 470.0
    assert updated.draft["amount"] == 940.0  # the ORIGINAL disputed amount is untouched
    store.close()
