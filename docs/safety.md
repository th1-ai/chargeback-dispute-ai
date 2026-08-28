# Guardrails and safety

This agent handles money-adjacent evidence and touches your systems, but it
never talks to a guest and never calls a card network. Everything below is
built in, not optional, and this page explains what it does and what is left
for you to decide.

## The two modes

| Mode | What happens |
|---|---|
| `shadow` (default) | The agent reads, builds packets and queues them. It **never** files a packet and **never** writes to your PMS. Approving, editing or rejecting a packet records your decision but files nothing. At go-live, `python3 tools/review.py stale` clears that shadow-era queue so nothing old goes out by surprise. |
| `live` | Disputes you approved are really filed - a Markdown packet written locally and a disputes-sheet row. Everything else still waits. |

`mode` lives in `config/hotel.yaml`. It is a global kill switch: flipping it back
to `shadow` stops every outbound action immediately, mid-schedule, with no other
change. `config/agent.yaml` can be stricter than `hotel.yaml`, never looser.

Two more brakes:

- `make run ARGS="--dry-run"` computes every packet and writes nothing, even
  in live mode. Use it to preview a scoring change before it touches the
  real queue.
- `review.require_approval_for` in `config/hotel.yaml` lists the actions
  that need a human even in live mode. `config/hotel.example.yaml` adds
  `dispute_submit` to the family defaults (`send_email`, `send_message`,
  `pms_write`, `payment`, `publish`) - filing a representment always needs
  a person, so removing it from this list is not a supported configuration.

Every outbound action in the codebase goes through one function,
`core/review.py:assert_write_allowed`. There is no second path.

## The review queue

Nothing is submitted without passing through the queue, and every dispute
passes through it, whatever the evidence score says.

```bash
make review                                              # what is waiting
python3 tools/review.py show <id>                         # the full packet and how it got there
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --body-file recommendation.txt
python3 tools/review.py reject <id> --reason "conceding this one"
```

A dispute moves `new -> pending_review` (recommended: represent) or
`new -> needs_human` (recommended: do not submit yet), then waits. Only
`tools/review.py` can write `approved`, `edited` or `rejected`; only
`python3 tools/review.py send` can write `sending`/`sent`. A crash between
"about to file" and "filed" is picked up on the next pass and shown to you
as failed rather than silently retried.

**Your edits teach nothing here on their own.** Editing a packet's
recommendation text is recorded (`core/store.py`'s `learnings` table, the
same mechanism every repo in this family uses), but this agent has no coach
layer that turns edits into rules - see `docs/how-it-works.md`, "Sub-agents
and the coach layer".

## What the agent will not do

- **Submit a representment while `mode: shadow`, or without a human's
  approval, ever.** There is no confidence level or evidence score high
  enough to skip the review queue - unlike some of its siblings in this
  family, this agent has no autonomous path at all. See
  `docs/how-it-works.md`, "Why every dispute needs a human, always."
- **Call a card network.** `core.adapters.get_stub("payments", ...)` is a
  deliberate stub. Filing a packet writes a local Markdown file and a sheet
  row; a human still opens their processor's own dispute portal and submits
  it. See `docs/how-it-works.md`, "Being honest about payments".
- **Invent evidence.** No message on file reads as "No guest correspondence
  is attached to this booking in the comms record" (`tools/engine.py`), not
  a paraphrase. No acceptance record reads as "No acceptance record is on
  file for this booking", not an assumed "accepted electronically". A
  missing reservation degrades the folio rather than guessing dates, a room
  type, or a stay total that were never on file.
- **Read a hostile guest message as a strong case.** When the cardholder's
  own first message describes a service problem rather than a self-
  initiated cancellation, the recommendation is always "do not submit yet",
  whatever the rest of the paperwork looks like - see
  `docs/how-it-works.md`, design decision 3.
- **Take a payment, issue a refund, or move money.** This agent's one write
  is filing a packet locally; nothing here schedules or releases funds.

## Data handling

**What leaves your machine.** The evidence packet itself never calls a
model at all - see `docs/how-it-works.md`, "The central design choice". The
one optional exception, `tools/narrate.py` (off by default), sends only
queue-level counts (how many disputes, how much money, which references are
urgent) to whichever `llm.provider` you configure - never a packet's own
legal text, never the guest's own words. With `llm.provider: mock` or
`interactive`, nothing leaves the machine at all.

**What is stored, and where.** Everything lives in `data/` inside this folder:
`agent.db` (SQLite), `logs/*.jsonl`, `exports/`. `data/` is gitignored. There is
no cloud service behind this repo and no telemetry.

**Card numbers are redacted on the way in.** Every inbound message passes through
`core/redact.py` before it is stored, logged or put into a prompt. A payment card
number is replaced with `[CARD REDACTED ****1234]`, and labelled CVC and expiry
values in the same message go with it. Detection requires a real card prefix and
a valid Luhn checksum, so booking references and door codes survive. IBANs are
masked the same way. Nothing you can do in config turns this off.

**Retention.** `privacy.retention_days` (default 365) is how long processed items
stay in the database. Deleting `data/agent.db` deletes everything the agent knows.

## GDPR, in practice

If you are in the EU or handle EU guests' data, the short version:

- **You are the controller.** This software runs on your machine, under your
  control, on your data. TH1 does not receive it.
- **Your model provider is a processor.** If you use the `anthropic` or
  `claude-code` provider, Anthropic processes guest data on your behalf. Check
  their data processing terms and record them in your processing register.
- **Purpose and minimisation.** The agent sees the message and the property facts
  it needs. Do not put staff phone numbers, card data or full guest histories in
  `knowledge/`.
- **Right to erasure.** A guest asking to be deleted means removing their rows
  from `data/agent.db` and any exported CSVs. Ask your Claude session:
  *"Delete every item in data/agent.db whose payload mentions this email address,
  and tell me how many rows you removed."*
- **Retention.** Set `privacy.retention_days` to what your own policy says, not
  to the default.

This is a practical summary, not legal advice.

## Telling guests they are talking to AI

There is no guest-facing text in this repo. A representment packet is
written for your bank or processor, never for the cardholder, and the one
message this agent sends - the dispute-queue digest - goes to your own
finance contact (`contacts.escalation_email`), not a third party. The EU AI
Act Article 50 guest-disclosure line that every other repo in this family
carries in `knowledge/signature.md` does not apply here for that reason.

If your processor's dispute portal asks whether the response was prepared
with AI assistance, answer honestly: the evidence was assembled by
deterministic code from your own records (see `docs/how-it-works.md`, "The
central design choice"), and a person reviewed and submitted it.

## Subscription or API: an honest note

Two ways to pay for the reasoning:

**Your Claude Code subscription** (`llm.provider: claude-code` or `interactive`).
Flat monthly cost, no per-message billing. This is genuinely the cheapest way to
run a small hotel's agent.

The caveat, plainly: a personal Pro or Max subscription is intended for
interactive use, and Anthropic's usage policy and rate limits apply to automated
use of it. A handful of scheduled runs a day is a normal way to work. Pointing
a busy inbox at it around the clock is not, and you will hit rate limits at the
worst moment. Read the terms and decide for yourself.

**The Anthropic API** (`llm.provider: anthropic`). Pay per token, no ambiguity
about automated use, proper rate limits, and usage you can attribute. This is
the right answer for production volume. `make report` shows what you are
spending.

Start on the subscription while you are learning what the agent does. Move to the
API when it becomes part of how the hotel runs.

## If something goes wrong

1. `mode: shadow` in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env`. Every
   outbound action stops on the next pass.
2. Remove the schedule (`crontab -e`, `launchctl unload`, or
   `systemctl disable --now <slug>.timer`).
3. `make doctor` to see what the agent thinks its state is.
4. `data/logs/*.jsonl` has every decision, with the run id, in order.
