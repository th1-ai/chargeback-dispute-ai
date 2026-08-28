# Workflow: shadow to live

Objective: decide, together with whoever owns disputes at this property,
whether Chargeback & Dispute AI is ready to file an approved representment
locally instead of only computing what it would build - and make the change
safely if so.

This is the property's decision, never the agent's. Do not suggest it until
the checklist below is genuinely true, and when you do raise it, say
plainly what changes - and what does not. **Nothing about going live lets
this agent submit anything without a human clicking send** - that is fixed
in `config/hotel.yaml: review.require_approval_for` and is not a supported
configuration to change.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it.
- [ ] `config/hotel.yaml` has the real property details.
      `knowledge/cancellation-policy.md` has your real terms, verbatim, not
      the shipped example - every packet quotes this file. `make doctor`
      checks this by content, not just that the file exists - a copy you
      never actually edited shows as a `FAIL`, same as a missing file.
- [ ] `config/agent.yaml` has your real `reason_code_map` (or the bundled
      defaults, if they match what you see) and a `guest_language.<lang>`
      entry - phrases that match how your own guests actually write - for
      every language you get disputes in, not just the bundled `en`.
- [ ] At least a few real disputes have gone through the review queue, not
      just the demo fixtures, and the packets and recommendations look
      right.
- [ ] You have connected a real `terms_store.adapter` and a real guest-
      comms archive (`docs/integrations.md`) - otherwise every packet says
      plainly that no acceptance record or no correspondence is on file,
      which is honest but weaker than it needs to be.
- [ ] You are comfortable with `evidence_strength_threshold` (70 by default)
      and `sla.alert_days_before` (3 days) - both are config, not something
      to hand-edit in code.

## Making the change

1. Clear the shadow-era queue so nothing from testing goes out by surprise:
   ```bash
   python3 tools/review.py stale
   ```
2. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
   `review.require_approval_for` already includes `dispute_submit` in
   `config/hotel.example.yaml` - leave it there. This is what keeps every
   dispute, however strong its evidence, waiting on a human click.
3. Run `make doctor` again to confirm.
4. Approve and send one real dispute by hand to see the whole path work:
   ```bash
   python3 tools/review.py list
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
   Check the file it wrote:
   ```bash
   ls data/exports/evidence-packets/
   cat data/exports/disputes.csv
   ```
5. Tell whoever owns the decision exactly what just changed: an approved
   dispute now really writes a packet file and a sheet row the moment you
   run `send` - nothing files itself, and nothing is ever submitted to a
   bank or processor by this repo; a person still opens the processor's own
   portal and submits it by hand (`docs/how-it-works.md`, "Being honest
   about payments").

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every write on the next pass, mid-schedule, with no other change.
