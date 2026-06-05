# n8n integration

An importable n8n workflow that runs this tool's dunning loop on a schedule with
**human approval before any send** — n8n is the *orchestrator*, the tested Python
core makes every money-touching decision.

`invoice-reminders.workflow.json` — import via n8n → **Workflows → Import from File**.

> **Before you reach for this:** for a single small publisher, plain `cron` + the
> CLI (see the main README, "Running on a schedule") is simpler and has fewer moving
> parts. Use this workflow only if you already run n8n. Two hard constraints:
> it is **self-hosted only** (the Execute Command node is disabled on n8n Cloud), and
> you **must wire the approval-notification node** (step 4 below) — out of the box
> the approve link only appears in the execution log.

## What it does

```
Schedule (daily)  ─┐
                   ├─►  Stage batch ──►  Parse & gate ──►  Build approval ──►  Await human ──►  Approve & send ──►  Record
Run on demand  ────┘   (run --send)     (stop if none)     request            approval          (approve)           outcome
```

1. **Schedule / Run on demand** — a daily cron, or a manual click to test.
2. **Stage batch** (`Execute Command`) — runs `reminders run --send --json`, which
   *stages* a batch into the approval queue and **sends nothing**. Prints a batch id.
3. **Parse & gate** (`Code`) — parses the JSON; if zero reminders are due, stops
   the run here.
4. **Build approval request** (`Code`) — assembles a summary + the resume link
   (`$execution.resumeUrl`).
5. **Await human approval** (`Wait`, resume-on-webhook) — the run pauses here.
   Nothing is sent until a human opens the resume link.
6. **Approve & send** (`Execute Command`) — runs `reminders approve <batch-id> --json`,
   the **only** step that actually emails, then records the audit trail.
7. **Record outcome** (`Code`) — surfaces the result in the execution log.

## Why this shape (n8n orchestrates; it does not replace the core)

n8n is great at the *glue*: scheduling, routing an approval, and invoking a command.
But the money-critical logic stays in the tested Python service, not in Function nodes:

- **Who's overdue / which stage** → `DunningPolicy` (deterministic, unit-tested),
  not a Code node.
- **"Never send the same reminder twice"** → a `UNIQUE(invoice_id, stage)` SQLite
  constraint — a hard guarantee even if the workflow re-runs or crashes mid-batch.
- **Audit trail** (who/what/when + message hash) → the state store.
- **The two seatbelts** map directly: `REMINDERS_ALLOW_SEND=1` is set on the
  Execute Command nodes, and the **Wait** node is the human-approval gate.

So you get n8n's convenience *and* the correctness guarantees you want for a
workflow touching five-figure invoices.

## Prerequisites

1. Install the tool on the **same host as n8n** so the CLI is on `PATH`:
   `pip install -e /opt/invoice-reminder` (adjust the path).
2. Create `/opt/invoice-reminder/config.yaml` — set `source.kind` (`csv` or a
   `quickbooks_*` adapter) and SMTP creds in `.env`. For a real backlog, set
   `dunning.first_contact_stage_cap: "friendly"`.
3. Edit the path in the two **Execute Command** nodes if your install differs.
4. **Wire the approval notification:** insert your Slack / Email / Teams node
   between *Build approval request* and *Await human approval*, sending the
   `approvalMessage` field (it includes the resume link) to whoever approves AR.
   Out of the box the link is only visible in the execution log.

> The Execute Command nodes shell out to the local CLI (n8n and the tool share a
> host). If you'd rather decouple them, the same `--json` commands can sit behind a
> thin HTTP wrapper and be called with HTTP Request nodes instead — no workflow
> logic changes.

## Note on `--json`

Every command accepts `--json` and prints a single JSON object on stdout, e.g.:

```json
{"batch_id": "B-46b17be609e8", "as_of": "2026-06-05", "count": 3, "reminders": [ ... ]}
```

That's the contract the Code nodes parse. `run --send --json` returns the
`batch_id`; `approve <id> --json` returns `{"batch_id": ..., "sent": N, "results": [...]}`.
