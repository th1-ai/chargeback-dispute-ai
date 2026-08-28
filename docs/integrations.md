# Connecting your systems

Every connector in this repo is one of three things, and the table says which.
We will not tell you an integration exists when it does not.

| Badge | Means |
|---|---|
| **built** | Written against the real API and tested against it. |
| **universal** | Works with any system through a common protocol: IMAP/SMTP, CSV, a webhook, a fixture reader. |
| **stub** | Interface only. Calling it raises a clear error with a recipe for adding it. |

Check what is actually working on your machine at any time:

```bash
make doctor
```

This agent uses four systems plus two small readers of its own: **PMS**
(read-only, the reservation behind a dispute), **email** (read-only, the
guest correspondence thread), **messaging** (the SLA-alert digest, optional),
**sheets** (the disputes log and the report export), and the **dispute feed**
and **terms store** - not core adapters yet, see "Core requests" below.

## PMS - `systems.pms.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/reservations.json`. What `make demo` uses. |
| `csv` | universal | a reservations export | Reads `data/imports/reservations.csv`. **Start here for a real property.** |
| `cloudbeds` | built | OAuth app + refresh token | Live reads. |
| `cli` | universal | a JSON-speaking CLI | Advanced. |

This agent only ever calls `get_reservation(reservation_ref)` - it never
lists availability, writes a note, or touches a rate. Any of the four
adapters above will do; `csv` is the fastest way to get real bookings behind
real packets without a live API. When the reservation cannot be found (a
typo in `reservation_ref`, or the booking predates your export), the packet
degrades rather than failing - see docs/how-it-works.md.

## Email - `systems.email.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/comms/*.json` (this agent points `fixtures_dir` away from the shared `fixtures/inbound`, which holds the dispute records themselves - see `config/hotel.example.yaml`). |
| `imap` | universal | mailbox + app password | Any provider. **Start here for a real property.** |
| `gmail` | built | Google OAuth desktop client | Adds labels/threads. |

This agent only ever calls `fetch_thread(comms_conversation_id)` - it never
polls an inbox for unread mail. Point it at the same mailbox your guests
actually write to (reservations, support, whatever address a booking
confirmation replies land in). In `.env`:

```
EMAIL_ADDRESS=reservations@example.com
EMAIL_PASSWORD=            # an APP password, never your login password
IMAP_HOST=imap.example.com
```

If your guest comms live in a system that is not email (a PMS's own message
log, a WhatsApp-only conversation), see "Implement your own" below - the
interface only needs one method, `fetch_thread`.

## Messaging - `systems.messaging.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Logs to `data/exports/sent_messages.jsonl`. |
| `unipile` | built | your own UniPile account | WhatsApp on your own number. |
| `webhook` | universal | any URL | POST to Zapier, Make, n8n, or your own endpoint. |

Used for exactly one thing: `tools/digest.py` calls `notify_staff()` when a
dispute is inside `config/agent.yaml: sla.alert_days_before` of its scheme
deadline. This is optional - the daily digest email carries the same
information either way, and `mode: shadow` blocks the alert (logged, not
sent) like any other write.

## Sheets - `systems.sheets.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `csv` | universal | nothing | Writes `data/exports/disputes.csv` and `data/exports/chargeback_dispute_report.csv`. |
| `google` | built | service account JSON | A live shared spreadsheet. |

`tools/engine.py:finalize_dispute` appends one row per submitted dispute
(`filed_at, item_id, dispute_id, guest_name, amount, currency, reason_code,
card_scheme, deadline_date, days_remaining_at_submit, evidence_strength,
verdict, filed_path`). `python3 tools/report.py --export` writes the
win-rate numbers to a second sheet.

## Dispute feed - `config/agent.yaml: dispute_feed.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/dispute-*.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/disputes.csv`. |

Not a core adapter yet - see "Core requests" below. This is where a real
deployment wants a processor webhook (Stripe's `charge.dispute.created`,
Adyen's `NOTIFICATION_OF_CHARGEBACK`) instead of a CSV export; see
"Implement your own". Columns: `id, guest_name, reservation_ref,
comms_conversation_id, amount, reason_code, card_scheme, deadline_offset,
venue`.

## Terms store - `config/agent.yaml: terms_store.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/terms-acceptance.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/terms_acceptance.csv`. |

Columns: `reservation_ref, accepted_at, ip, checkout_page_version,
policy_version`. Export this from your booking engine's own acceptance log.
A reservation with no row here still gets a packet - section 4 says plainly
that no acceptance record is on file, rather than asserting one - see
docs/how-it-works.md, design decision 2.

## Being honest about payments

There is no `systems.payments.adapter`. `core.adapters.get_stub("payments",
settings)` is a deliberate stub - this agent never calls a card network.
See docs/how-it-works.md, "Being honest about payments", for what actually
happens instead (a local packet file and a sheet row, both written by a
human's own "send" click).

## Implement your own

<a id="implement-your-own"></a>

**A real dispute webhook** (rather than a CSV export). Open `claude` in this
folder and paste:

> Read `tools/dispute_feed.py` and `docs/integrations.md#implement-your-own`.
> I want disputes to arrive from <Stripe / Adyen / our acquirer's own API -
> say which>, not from a CSV export. Write a small HTTP endpoint or poller
> that normalises their payload into the same shape `_normalise()` returns
> in `tools/dispute_feed.py`, and switch `config/agent.yaml:
> dispute_feed.adapter` to a name you register there. Keep it read-only -
> this agent never submits anything without a human clicking send.

**A general adapter** (mailbox, sheet, PMS, or anything in
`core/adapters/base.py`). The five-step recipe every repo in this family
uses:

1. Copy the closest existing adapter (`core/adapters/pms_csv.py`,
   `core/adapters/email_imap.py`).
2. Implement `ping()` and `capabilities()` first - `make doctor` reads both.
3. Implement the reads, mapping onto the dataclasses in `core/adapters/base.py`.
4. Implement the writes, each with `@guarded_write("<action>")` - not
   optional, or your adapter can write while the agent is in shadow mode.
5. Register it in `core/adapters/__init__.py`'s `REGISTRY`, set the adapter
   name in `config/hotel.yaml`, and run `make doctor`.

### Rules that matter

- **`ping()` never raises.** Return `HealthCheck(ok=False, ...)` with a hint.
- **Every write is decorated**, or the write guard cannot see it.
- **Never log a credential.** `core/log.py` masks anything whose key looks
  like a secret, but do not rely on it.
- **Redact on ingestion.** Any inbound text goes through `core.redact.redact()`
  before it is stored, logged or put into a prompt - the email adapters do
  this for you already.
- **Write a test.** Copy `tests/test_chargeback_dispute_loop.py`'s pattern:
  build `Settings` from a tmp copy of the shipped `.example.yaml` files,
  feed your reader a fixture, check the dict that comes out.

### Core requests

`tools/dispute_feed.py` and `tools/terms_store.py` live in `tools/` rather
than `core/adapters/` because `core/` is vendored byte-for-byte into all 28
repos in this family from a single factory source, and no other agent in
the family needs "a dispute" or "an acceptance record" shape yet. A "Core
request" to add these two families (fixture + CSV, the way `pms_csv.py`
already works for reservations) to `core/adapters/__init__.py`'s registry is
noted in this repo's build report, for whenever a second agent needs the
same shape.
