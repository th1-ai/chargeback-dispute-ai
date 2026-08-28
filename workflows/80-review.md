# Workflow: working the review queue

Objective: turn a dispute packet into a decision - approve, correct, or
reject - and file it; approve and send the dispute-queue digest. In
`mode: shadow`, approving records the decision only; nothing files or sends
until you switch to `mode: live` (`workflows/90-go-live.md`). Every dispute
comes through here, whatever its recommendation says - this agent never
skips the queue.

## Steps

1. **See what is waiting.**
   ```bash
   make review
   python3 tools/review.py list --kind dispute
   python3 tools/review.py list --status needs_human
   ```
   Each line shows the item id, status, kind, and a short summary (guest,
   amount, currency, recommended verdict, days left for a dispute; subject
   for a digest).

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   Prints the whole packet - folio, stay evidence, comms excerpts, the
   policy section, the evidence-strength score and what is missing, the
   recommendation, and the full event history. Read the recommendation to
   whoever owns the decision in plain language - "represent in full, every
   exhibit reason code 4853 needs is on file, six days left" is useful; the
   raw JSON is not.

3. **Decide, for a dispute.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py edit <id> --body-file recommendation.txt --verdict represent
   python3 tools/review.py reject <id> --reason "conceding this one - not worth the effort"
   ```
   `edit` replaces the recommendation text (write your own version to a
   file first) and can flip the verdict label without re-running the
   scoring - use it when a person's judgement should override the computed
   recommendation (you have context the packet does not, or you disagree
   with the evidence-strength call). `reject` discards the item; it does
   not file and is not retried - use it when the property is conceding the
   case rather than fighting it.

4. **Decide, for a held digest.** Same commands - a digest almost always
   just needs `approve`; `edit --body-file <path>` replaces the whole body
   if you want to add something by hand first.

5. **File or send what was approved.**
   ```bash
   python3 tools/review.py send
   ```
   Claims everything `approved`/`edited` and finishes it: a dispute gets
   filed (`tools/engine.py:finalize_dispute` - a Markdown packet plus the
   disputes-sheet row) and a digest gets emailed. In `mode: shadow` this is
   blocked outright, even for an item you just approved - shadow is a true
   kill switch (`docs/safety.md`). **Filing here writes a local file for a
   human to submit to the processor's own dispute portal - this repo never
   calls a card network.**

6. **A failed file/send.** `send` marks the item `failed` with the error
   attached.
   ```bash
   python3 tools/review.py retry <id>
   ```
   re-queues it for another attempt once the cause is fixed.

7. **Weeks later, record the real outcome.**
   ```bash
   python3 tools/review.py outcome <id> --result won
   python3 tools/review.py outcome <id> --result won_in_part --recovered 470.00
   python3 tools/review.py outcome <id> --result lost
   ```
   Only works on an already-`sent` item - see `docs/how-it-works.md`,
   design decisions 7-9. This is what `python3 tools/report.py`'s win rate
   is computed from.

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- Only `python3 tools/review.py send` writes `sending` / `sent`. There is
  no autonomous path in this agent at all - see `docs/how-it-works.md`,
  "Why every dispute needs a human, always."
- A dispute recommended "do not submit yet" never files itself, and a
  dispute recommended "represent" is not exempt from review either - there
  is nothing to "approve your way past"; you decide, either way.
- Confirm with whoever owns the decision before switching this agent to
  `mode: live` the first few times, even though every write is already
  gated by approval where it needs to be.
