# The business case

**Why.** Hotels lose winnable disputes purely by missing deadlines or weak
evidence.

**Output.** Lift dispute win-rate; recover a share of what's currently
written off.

**ROI.** +35% Dispute win-rate (revenue).

(Quoted verbatim from the roster - see `README.md` for the full promise.)

## The problem this solves

A chargeback lands with a clock already running - a handful of days to pull
together the booking, the guest's messages, the terms they agreed to, and
argue the case, or the money is written off automatically. Most properties
do this by hand, under time pressure, from memory: hunting through a PMS
and an inbox for the same three facts every time, often after the deadline
has already slipped past. The evidence usually exists - the booking is
real, the terms were shown at checkout, the guest often cancelled in
writing - it just was not assembled and submitted in time, or was submitted
thin because gathering it properly takes longer than the property had.
This agent does the part that is genuinely mechanical - pulling the
reservation, the thread, the acceptance record, and laying them out against
what the reason code actually requires - and leaves the part that needs
judgment and authority: deciding whether to submit, and clicking send.

## What to measure

`python3 tools/report.py` reads straight from `core.store` and shows:

- **Win rate**: the share of decided cases (`python3 tools/review.py
  outcome`) that came back won or won in part - the direct number against
  the roster's "+35% dispute win-rate" claim. It reads `None`, not a made-up
  percentage, until you have recorded at least one real outcome - see
  "Honest caveats" below.
- **Amount recovered versus amount disputed**: the euro (or your own
  currency) half of "recover a share of what's currently written off" -
  distinct fields, because a `won_in_part` outcome recovers less than the
  full disputed amount.
- **Open value at stake**: every dispute not yet decided, with the amount,
  so you can see what is still live.
- **Average days to submit**: from a case first landing to a human clicking
  send - the practical measure of "drafts the response within the
  deadline". A property whose average creeps toward the SLA alert window
  (`config/agent.yaml: sla.alert_days_before`) is the signal to work the
  review queue more often, not to change the agent.
- **Spend**: the optional case-manager note is the only thing that ever
  calls a model here (see `docs/how-it-works.md`); this line should stay
  at or near zero unless you turned `narrate.enabled` on.

`python3 tools/report.py --export` writes the same numbers to
`systems.sheets.adapter` so you can hand a controller a file instead of a
terminal.

## Honest caveats

- **"+35% dispute win-rate" is the source material's own estimate, not a
  guarantee for your property.** It depends on your actual reason-code mix,
  how complete your comms archive and terms-acceptance records already are,
  and how quickly your team works the review queue against the scheme
  deadline. `python3 tools/report.py` tells you your own number, once you
  have decided outcomes to measure it against.
- **The evidence-strength score is not a legal opinion.** It measures
  whether the exhibits a reason code typically needs are on file and
  whether the guest's own words help or hurt - it is not a prediction of
  what your bank or scheme will actually rule, and it never overrides a
  human's own judgment about whether to submit.
- **A thin comms or terms-acceptance archive means more "do not submit
  yet" recommendations, not weaker packets.** This agent will not paper
  over a missing acceptance record or an empty comms thread - see
  `docs/how-it-works.md`, design decisions 2 and 5. Connecting a real
  `terms_store` and a real guest-comms archive (`docs/integrations.md`) is
  what turns more cases into "represent in full".
- **"Detects" still needs a real feed connected.** As shipped, disputes
  come from `config/agent.yaml: dispute_feed.adapter` (mock fixtures, or a
  CSV export) - a live processor webhook is a genuine integration to add,
  not something this template can honestly claim already works. See
  `docs/integrations.md#implement-your-own`.
- **Submission itself is manual.** This agent never calls a card network -
  see `docs/how-it-works.md`, "Being honest about payments". The time this
  agent saves is in assembling the case, not in the last click.
