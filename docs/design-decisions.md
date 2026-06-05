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

## Decision 4 — n8n (or any workflow orchestrator): considered and rejected

n8n fits the *shape* of this job (schedule → fetch → approve → send), and an
importable workflow was briefly built to explore it. It was then **removed**: the
publisher does not run n8n, and a workflow orchestrator would be a **second runtime**
(Node + its own DB + a webhook) that owns **none** of the correctness guarantees —
those live entirely in the tested Python core. "More n8n" is the wrong direction for
one small publisher; rebuilding the policy / idempotency / audit as untested Function
nodes would throw away exactly what makes this safe for five-figure invoices. The
deployment is plain **cron + the CLI** (Decision 6 / 7): one runtime, one SQLite
file. The only artifact kept from that exploration is the general `--json` output
mode (useful for scripting and parsing the `cron-run` summary).

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

- **Deployment is cron + the CLI, not n8n.** For one small publisher, n8n is a
  second runtime that owns no correctness guarantee; "more n8n" is the wrong
  direction (the exploratory n8n workflow was subsequently removed — see Decision 4).
  The Python core is unconditionally primary and self-sufficient.
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

## Decision 7 — fully automated under cron: auto the routine, hard-gate the rest

Dan's goal is to eliminate the manual dunning work entirely (no n8n, plain cron). A
second adversarial review (three failure-mode lenses → spec → red-team) pressure-
tested unattended sending before any code. The honest verdict: **you cannot safely
fully-automate the irreversible cases off a human-exported flat CSV.** The export can
be *confidently wrong* (a paid invoice still marked open; mtime is falsifiable by an
Excel re-save or a Drive sync; the real Magazine Manager export has no report-date
column), and the human approval keystroke was exactly the "wait, they paid last week"
catch being removed. The literal "$100K FINAL auto-sent" is preventable; friendly/firm
dunning of a paying advertiser is not, off a flat file.

So "fully automated" was implemented as: **auto-send the routine lane, divert the
irreversible slice to a human.**

- **Code-enforced gate (not config):** `reminders.automation` holds every `final`
  notice, any amount over a hard `$2,500` ceiling, and every first-ever contact to a
  new advertiser — for human `approve`. Config can only make the gate *more*
  conservative; no YAML edit can widen it. This replaces the old structural
  two-seatbelt gate with a gate that still can't be config-disabled.
- **Guards are the safety now** (every one fails *closed* — refuse the whole run +
  alert, never a partial blast): kill switch (`enabled` + `REMINDERS_ALLOW_SEND` +
  a `HOLD` file), CSV freshness, required cap, per-run cap, volume floor, strict
  ingest (unknown status quarantined not coerced to open; required status/DNC columns;
  amount band + email validation), single summary email after every run.
- **Ships dark, rolled out via canary:** `cron-run --dry-run` (all guards, sends
  nothing) → `enabled` with `max_send_per_run: 1`, friendly-only → ramp. FINAL,
  high-value, and first-contact stay human-gated permanently — that is the floor,
  not a ramp step.

Deferred hardening for before heavier live use (the canary phase surfaces these):
two-phase reserve-before-send for wire-idempotency, a durable suppression list that
survives a dropped DNC column, an export-schema fingerprint, an independent alert
channel (today the summary rides the same SMTP as the sends), and a dead-man's-switch
on "no successful run in N hours."
