# Workflow: first-run setup

Objective: get Chargeback & Dispute AI from a fresh clone to building real
evidence packets, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never
   overwrites your own copies). `make doctor` will show a `FAIL` on "hotel
   identity" right after setup - that is expected, it means the property
   name is still the shipped placeholder "Hotel Aurora".

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see 5 sample disputes from `fixtures/inbound/`, each scored
   and sorted into a verdict, and the line
   `DEMO OK — 5 items processed, 5 drafted, 0 sent (shadow)`. If you do not
   see that, stop and read `workflows/99-troubleshooting.md`.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address,
   contact, currency, and `contacts.escalation_email` for the digest). Then:
   ```bash
   cp knowledge/cancellation-policy.example.md knowledge/cancellation-policy.md
   ```
   Replace the example wording with your real deposit and cancellation
   terms, verbatim - this is the exact text every packet quotes in section
   4. Then edit `config/agent.yaml`:
   - `reason_code_map` - your real scheme reason codes, or leave the
     bundled ones (4853, 4855, 10.4, 13.1) if they match what you actually
     see.
   - `guest_language.<lang>.helps_case` / `.hurts_case` - one entry per
     language code, phrases your own guests actually use. A language with
     no entry is never scored - the case holds for a person to read
     instead of silently reading as neutral, so add an entry for every
     language you actually get disputes in, not just the bundled `en`.
   - `evidence_strength_threshold` and `sla.alert_days_before` - the
     bundled defaults (70, 3 days) are a reasonable place to start.

4. **Connect your real systems.** `systems.pms.adapter` and
   `systems.email.adapter` in `config/hotel.yaml`, plus
   `dispute_feed.adapter` and `terms_store.adapter` in `config/agent.yaml`,
   all start as `mock` - see `docs/integrations.md` for `csv` (works with
   any system, fastest to connect) and the built adapters.

5. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real and `knowledge/cancellation-policy.md`
   exists, those two lines turn green. Move on to
   `workflows/10-dispute-packets.md` to run the loop on your own disputes.
