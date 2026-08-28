# Workflow: the dispute-packet loop

Objective: run one pass over the dispute feed and see what Chargeback &
Dispute AI built. This agent never files anything on its own, in either
mode - see `workflows/80-review.md` for that step.

## Inputs

- A configured `config/agent.yaml: dispute_feed.adapter` (`mock` by default
  - see `workflows/00-setup.md` step 4 to connect a real feed).
- A configured `systems.pms.adapter` and `systems.email.adapter` (for the
  reservation and the comms thread) and `terms_store.adapter` (for the
  acceptance record) - all `mock` until you connect them.
- `config/agent.yaml: reason_code_map`, `guest_language`,
  `evidence_strength_threshold` - the defaults match the behaviour this
  agent was built from; your real numbers belong here.

## Steps

1. **Run one pass.**
   ```bash
   make run
   make run ARGS="--limit 10"      # just the first ten disputes
   make run ARGS="--dry-run"       # compute everything, write nothing at all
   ```
   Every new dispute goes through `tools/engine.py:process_dispute`: pull
   the reservation, pull the comms thread, pull the acceptance record, and
   assemble the packet - no model call anywhere in this path, see
   `docs/how-it-works.md`. There is no `interactive`-provider pause to
   answer here; a pass either finishes or fails with a real error.

2. **See what happened.**
   ```bash
   make review
   ```
   A dispute with every exhibit its reason code requires on file, and
   nothing in the guest's own words working against it, is
   `pending_review` with a "represent" recommendation. Anything missing a
   required exhibit, or with a guest message that reads as a service
   complaint rather than a self-initiated cancellation, is `needs_human`
   with a "do not submit yet" recommendation and the reason spelled out.
   **Both still need a human to approve and send** - the verdict only
   changes what the recommendation says, never whether review happens.

3. **Work the queue.** `workflows/80-review.md` covers approve / edit /
   reject / send in full.

4. **Try a config change.** Turn `rules.full_evidence_pack` off and run
   `make run` again on a fresh copy of a dispute - the packet now ships
   without the comms and policy sections, and the warning names exactly
   what is missing. Add a phrase to `guest_language.<lang>.hurts_case`
   (under the guest's own language) that matches something your own guests
   actually write, and a case that used to read "represent" may now
   correctly hold.

5. **Keep it running.**
   ```bash
   make watch                        # loop on the configured interval
   python3 tools/schedule.py --all   # cron/launchd/systemd snippets for every job
   ```
   `config/agent.yaml: schedule.dispute-packets` sets the cadence (`hourly`
   by default); `scheduler/` has ready-made cron, launchd and systemd files.

## Edge cases

- **No new disputes.** `make run` prints `0 items processed, 0 drafted, 0 sent`
  and exits 0.
- **A re-run sees the same dispute again.** `(source, external_id)` is
  unique on `items` - see `core.store.Store.upsert_item`. Nothing is
  rebuilt; `item.draft is not None` is the resume check in
  `tools/engine.py:process_dispute`.
- **The reservation cannot be found.** The folio degrades to the dispute's
  own fields rather than failing; "stay" evidence cannot be built, and the
  packet holds by default - see `fixtures/inbound/dispute-04.json` for an
  example (`make demo` -> DP-5501).
- **No comms thread on file.** The packet says so plainly in section 3
  rather than inventing a cancellation - see `fixtures/inbound/dispute-02.json`
  (`make demo` -> DP-3312).
