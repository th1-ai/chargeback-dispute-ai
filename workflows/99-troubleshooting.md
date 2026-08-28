# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`cancellation policy`: no cancellation-policy file at all.** Restore
  `knowledge/cancellation-policy.example.md` from git, or it was never
  copied - `cp knowledge/cancellation-policy.example.md
  knowledge/cancellation-policy.md`.
- **`cancellation policy`: still the shipped example, word for word.** The
  file exists but was only ever copied, never edited - `make doctor`
  compares its content to `knowledge/cancellation-policy.example.md`, the
  same way `hotel identity` catches an unedited placeholder name. Replace
  the wording with your real terms; `tools/engine.py` also refuses to
  represent a packet while this is true.
- **`reason code map`: missing its 'default' entry.** Copy
  `config/agent.example.yaml` to `config/agent.yaml`.
- **`pms adapter` / `email adapter` show something other than `ok`.** Check
  `systems.pms.adapter` / `systems.email.adapter` in `config/hotel.yaml`
  and the matching variables in `.env` - see `docs/integrations.md`.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` calls `load_settings(demo=True)`, which forces the mock
  provider, shadow mode and the mock adapter for every system - if you
  deleted or renamed `fixtures/inbound/dispute-*.json` or
  `fixtures/comms/*.json`, restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow
  errors on purpose, so a fixture problem shows up immediately.

## A packet looks wrong

- **"No guest correspondence is attached to this booking" but you know
  there is a thread.** Check `comms_conversation_id` on the dispute record
  against the thread's own id in your comms system - see
  `docs/integrations.md`.
- **"No acceptance record is on file" for a booking you know accepted the
  terms.** Check `config/agent.yaml: terms_store.adapter` is connected, and
  that `reservation_ref` matches exactly between the dispute and the
  acceptance record.
- **A dispute holds even though every exhibit looks present.** The
  guest's own first message may match a phrase in
  `config/agent.yaml: guest_language.<lang>.hurts_case`; that language may
  have no `guest_language` entry at all yet ("no `<lang>` phrase set
  configured"); or `knowledge/cancellation-policy.md` may still be the
  shipped example - read the packet's warnings, they name the exact
  reason.
- **A dispute you expected to hold instead represents.** Check
  `config/agent.yaml: reason_code_map` for that reason code's `requires`
  list - a code that does not require `comms` will represent without a
  message on file, by design (see `docs/how-it-works.md`, decision 4).
- **The wrong required exhibits for a reason code.** Check
  `config/agent.yaml: reason_code_map` - this is config, not something the
  agent infers per dispute.

## `python3 tools/review.py send` says "blocked"

Expected while `mode: shadow` - shadow blocks every write, approved or not.
Handle it yourself for now, or go live (`workflows/90-go-live.md`). Once
live, `send` writes a local Markdown packet and a sheet row - it still does
not call a card network; a person submits the file to the processor's own
portal by hand.

## `python3 tools/review.py outcome` says "not 'sent'"

Only a dispute that has already been through `approve` then `send`
(`review_status = sent`) can have an outcome recorded - approve and send it
first, then come back once the bank has actually ruled.

## An item is stuck at `sending`

A process died between claiming an item and finishing it.
`python3 tools/run.py` calls `core.store.Store.reap_stuck_sending()` on
every real pass, which moves anything stuck for more than 30 minutes to
`failed` so you see it in the queue instead of it vanishing. Use
`python3 tools/review.py retry <id>` once the cause is fixed.

## `python3 tools/*.py` says `ModuleNotFoundError: No module named 'core'`

You ran it with a Python that is not the repo's virtualenv, or from outside
the repo root. Use `make run` / `make doctor` / etc. (they call
`.venv/bin/python` for you), or run `.venv/bin/python tools/run.py` directly
from the repo root.

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one
item. If neither explains it, that is a real bug - describe exactly what
you ran and what you expected, and ask.
