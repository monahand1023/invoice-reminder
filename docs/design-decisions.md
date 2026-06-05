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

## Decision 4 — n8n is an orchestrator, not a replacement (deferred)

The README's "maps to n8n" section is a *target mapping*, not built — and whether
n8n is the deployment target is itself open. The stance: if/when n8n is used, it
**orchestrates around** the tested Python core (Schedule Trigger, human-approval
routing) and never **replaces** it (rebuilding the policy/idempotency/audit as
untested nodes would throw away exactly what makes this safe). Nothing n8n is built
in this pass; the Python service owns the QuickBooks fetch so the adapters are the
right investment with or without n8n.

## Decision 5 — diagrams

README diagrams are **Mermaid** (a `flowchart` for architecture, a `sequenceDiagram`
for the two-seatbelt approval flow). GitHub renders Mermaid inline; PlantUML needs a
proxy/action, so Mermaid is the right choice for "renders cleanly on the repo page."
