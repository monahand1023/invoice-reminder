# Design decisions

A short record of the choices behind this proof-of-concept, so the reasoning
survives the code. This is a **demo / POC** ("here's how you'd build it"), not a
production deployment.

## Context

A magazine publisher bills advertisers ($500–$100K) through **Magazine Manager**,
a web SaaS tool. Chasing overdue invoices is manual today: a staffer pulls the late
payers and emails reminders. The question we're answering: *can one AI tool just
sign in and do all of this, or is it a custom stack?*

**Answer: a custom stack, with the AI confined to the edges and a human on the
trigger.** A single autonomous "AI logs in and sends payment emails" agent is the
wrong tool for money: it can misread amounts, breaks when the SaaS UI changes, and
gives no idempotency/audit guarantees. So the selection of who/how-much/which-stage
is deterministic, tested code; a human approves every send; and AI is used only to
(a) build the tool and (b) optionally soften email *tone*.

## Decision 1 — first-contact cap (cold-start safety)

**Problem:** pointed at a real billing system, we inherit a backlog of invoices
already 30/60/90+ days overdue that have never been contacted by this tool. The
naive "highest-bucket-wins" rule would open with a **FINAL NOTICE**.

**Decision:** add `dunning.first_contact_stage_cap` (a stage name, e.g. `friendly`).
When an invoice has no prior reminder on record, its first send is held down to the
cap; it escalates to its true age-bucket afterward. Implemented in `DunningPolicy`
(stays pure — the cap is config, and the policy already receives the set of stages
already sent). Can only lower the first touch, never raise it.

**Default:** OFF in `config.example.yaml` — the CLI falls back to that file, and the
mock demo / README examples / `test_policy.py` rely on seeing all three stages
against a fresh DB. Recommended ON (`friendly`) for any real deployment.

## Decision 2 — LLM tone-rewrite seam (built, flagged OFF)

**Decision:** implement the `ToneRewriter` seam now, but keep it OFF by default.
`NoOpToneRewriter` (identity) is the default, so the system stays deterministic and
every existing test is unaffected. `ClaudeToneRewriter` (Anthropic SDK, lazy import,
injectable client) is used only when `tone_rewrite.enabled: true`.

**Why these constraints:** an LLM rewrite is non-deterministic, which would break two
guarantees the tests enforce (byte-identical dry-runs; stable message-hash
idempotency). So the rewrite is applied **only at send-time** (dry-run shows the
deterministic copy), **cached per `(invoice, stage)`** keyed on the source-body hash
(retries are byte-identical; the audit records the hash actually sent), and gated by
a **fact-preservation guard** that falls back to the deterministic body if the model
drops the invoice id or amount. Net: the LLM can never affect a billing fact.

See `src/reminders/tone.py`, `tests/test_tone.py`.

## Decision 3 — data source strategy (the real open question)

Magazine Manager has **no public API**, so the dominant unknown is how to get the
overdue list out. We don't yet know whether the publisher syncs MM → QuickBooks.
Three paths, built/planned best-to-last-resort:

1. **QuickBooks sync** — if MM pushes AR into QuickBooks, integrate with QB's real
   API. We build **both** `QuickBooksOnlineSource` (OAuth2/REST) and
   `QuickBooksDesktopSource` (qbXML/Web Connector) because we don't know which.
2. **CSV ingest (MVP)** — a human exports MM's AR/aging report; `CsvInvoiceSource`
   reads it. No API, no scraping, no AI-signs-in risk. **Built first** because it's
   the one path that's demo-able end-to-end with zero credentials and works whether
   or not a QB sync exists.
3. **Browser automation** — `MagazineManagerSource`, supervised Playwright against
   the logged-in UI. Last resort (brittle); stays a documented stub. We do not
   invent endpoints.

All adapters share one principle: **pure mapping/parsing split from injectable
transport, stdlib-only, import-safe, fully tested offline** — credentials and the
network are never required to run the tests.

## Decision 4 — n8n orchestrates the tested core (built)

n8n is an excellent fit for the *shape* of this job (schedule → fetch → approve →
send), so we built an importable workflow in `integrations/n8n/` plus a `--json`
output mode on the CLI for it to call. The deliberate boundary: n8n **orchestrates
around** the tested Python core (Schedule Trigger, a human-approval Wait node,
invoking the CLI) and never **replaces** it. Rebuilding the policy / idempotency /
audit as untested Function nodes would throw away exactly what makes this safe for
five-figure invoices, so the workflow shells out to `reminders run --send --json`
and `reminders approve <id> --json` instead. Both seatbelts survive the port.

Honest caveat: for a strict MVP, a leaner n8n-native build (logic in Function nodes)
would be less to own — defensible at low stakes. We kept the tested core because the
correctness guarantees (deterministic stage selection, a `UNIQUE(invoice, stage)`
idempotency constraint, the audit trail) are the part worth owning regardless of
which orchestrator wraps it.

## Decision 5 — diagrams

README diagrams are **Mermaid** (a `flowchart` for architecture, a `sequenceDiagram`
for the two-seatbelt approval flow). GitHub renders Mermaid inline; PlantUML needs a
proxy/action, so Mermaid is the right choice for "renders cleanly on the repo page."

## Decision 6 — architecture review: keep the core, simplify the edges

An adversarial multi-lens review (simplicity prosecutor, architecture defender,
n8n-native architect, operability realist → synthesis → red-team) reached a clear
verdict: the **deterministic core is right-sized and must not change** — the stage
policy, the `UNIQUE(invoice, stage)` idempotency, the audit trail, and the two
seatbelts are exactly where money-correctness must live, and rebuilding them as
untested low-code nodes would be the real over-engineering mistake. What was
over-built is *breadth*, not the core: two mutually-exclusive QuickBooks adapters
built before the source was confirmed, and n8n positioned as a co-equal layer.

Resulting decisions:

- **Recommended deployment is cron + the CLI, not n8n.** For one small publisher,
  n8n is a second runtime that owns no correctness guarantee; "more n8n" is the
  wrong direction. n8n stays in the repo as a clearly-labeled *optional* integration
  for shops already running it (self-hosted only; approval-notification node is a
  stub to wire). The Python core is unconditionally primary and self-sufficient.
- **Cold-start safety is now enforced in code,** not a config footnote: the first
  real `run --send` with an empty history and no `first_contact_stage_cap` is
  refused (override with `--allow-cold-start`). This was the single highest
  money-safety-per-line gap.
- **Idempotency claim corrected to be honest:** exactly-once on the *audit record*
  (the UNIQUE constraint); at-least-once on the *wire* by deliberate choice (a crash
  after SMTP-accept but before record may re-send — the safe direction for debt
  collection). The red-team's proposed "sent-intent row" fix was rejected because it
  would invert this to at-most-once (a silently-unsent reminder), which is worse.
- **The QuickBooks adapters are labeled as blueprints,** not "ready to switch on";
  QuickBooks Desktop additionally needs a SOAP host that isn't in this repo. Build
  exactly one, against a live tenant, after fact-finding.
- **Batch visibility added** (`batches` / `batches --cancel`) so unapproved batches
  don't accumulate silently.

Still open (Dan's call): whether the day-to-day operator approves from the terminal
or needs a clickable approval — and if the latter, it must be a two-step POST page
with a single-use, expiring token, **never** a bare GET link (mail-scanner link
prefetch would auto-fire it and release a send with no human intent). This is the
real fork between cron+CLI and n8n for a non-technical operator.
