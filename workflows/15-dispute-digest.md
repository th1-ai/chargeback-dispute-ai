# Workflow: the dispute-queue digest

Objective: send whoever owns disputes a plain-text summary of what is open,
what is submitted, and what is inside the SLA alert window - the
deadline-first framing the roster's "detects... and drafts the response
within the deadline" depends on.

## Steps

1. **Build the digest.**
   ```bash
   python3 tools/digest.py
   ```
   Gathers every open and submitted dispute, flags anything inside
   `config/agent.yaml: sla.alert_days_before` (3 days by default) of its
   scheme deadline, and queues it as a `kind="digest"` item - the same
   review queue as a dispute, needing the same approval before it sends.
   Running this again the same day updates the queued draft instead of
   creating a second one.

2. **See it.**
   ```bash
   python3 tools/review.py list --kind digest
   python3 tools/review.py show <id>
   ```
   The body lists what is open (count and value), what is submitted and
   awaiting a ruling, every case inside the alert window by reference and
   days left, and, if `narrate.enabled: true`, a short cosmetic paragraph
   from `tools/narrate.py` - never a word that changes a score or a
   recommendation, see `docs/how-it-works.md`.

3. **Approve and send.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
   `send` is the same command `workflows/80-review.md` uses for disputes -
   it looks at each claimed item's `kind` and does the right thing
   (`email.send()` for a digest, `finalize_dispute()` for a dispute).
   Blocked in `mode: shadow`, like every other write.

4. **The staff alert.** When something is inside the alert window,
   `tools/digest.py` also tries `messaging.notify_staff()` - guarded like
   any other write, so in `mode: shadow` it is blocked and only logged; the
   digest email still carries the same information either way.

5. **Schedule it.**
   ```bash
   python3 tools/schedule.py --all
   ```
   `config/agent.yaml: digest.hour` sets when (08:00 by default) -
   `tools/schedule.py` always computes the dispute-digest cadence from that
   value, never from `schedule.dispute-digest.cadence` (a fallback only) -
   see `scheduler/` for the generated cron/launchd/systemd snippets.

## The optional case-manager note

`tools/narrate.py` is the only place besides the packet's own recommendation
text that ever calls a model, and only for a cosmetic paragraph appended to
the digest body - it never sees a packet's legal text, never changes a
score or a recommendation, and a failure there never blocks the digest
itself. Off by default (`config/agent.yaml: narrate.enabled: false`).

## Edge cases

- **Nothing to report.** The digest still queues, with all-zero counts and
  "Nothing is inside the SLA alert window." - useful as an "the agent is
  alive" signal even on a quiet day.
- **`narrate.enabled: true` and `llm.provider: interactive`.** Building the
  digest can itself pend, exit code 3, the same as any other `interactive`
  call - answer the prompt in `data/pending/` and run
  `python3 tools/digest.py` again.
- **An approved digest before you re-run `python3 tools/digest.py`.** The
  digest builder never overwrites a draft once you have approved, edited or
  sent it that day - see `tools/digest.py:build_digest`.
