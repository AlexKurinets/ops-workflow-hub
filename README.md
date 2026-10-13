# ops-workflow-hub

An integration hub that runs cross-application business workflows the way an operations team needs them to run: every inbound event is recorded once, every workflow is a sequence of checkpointed steps defined in YAML, every write to another application happens at most once, and when a run fails after its retries it parks in a dead-letter queue with a timeline a person can read, fix and replay.

This is a reference implementation of the architecture behind an integration platform I built for a regional staffing agency with about 150 internal staff, connecting its applicant tracking system, CRM, e-signature service, payroll, accounting, Slack and Google Sheets. The production build is under NDA, so a few integrations are abstracted behind adapters: the SaaS applications are replaced by sandbox adapters backed by Postgres, and Slack and email can run in sandbox, console or live mode. It runs on synthetic data, and every company, place, person, phone number and figure in it is made up.

[![CI](https://github.com/AlexKurinets/ops-workflow-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexKurinets/ops-workflow-hub/actions/workflows/ci.yml)

## Architecture

```mermaid
flowchart LR
    WH["Webhooks: ATS, e-signature, web forms, CRM"] -->|"POST /webhooks/{source}"| API
    SCH["Scheduler: pollers + cron"] -->|ingest| DB
    API["FastAPI ingress"] -->|"insert event ON CONFLICT DO NOTHING, queue runs"| DB[("Postgres: events, runs, step_runs, effects, entity_links")]
    DB -->|"FOR UPDATE SKIP LOCKED"| WK["Worker: checkpointed YAML steps"]
    WK --> AD["Adapters: rate limit, retry classes, verify by key"]
    AD --> APPS["ATS · CRM · e-signature · payroll · accounting · Sheets · Slack · email"]
    WK -->|"retries exhausted"| DLQ["Dead-letter: Slack alert + timeline"]
    DLQ -->|"hub replay"| DB
    OPS["Ops page /ops"] --> API
```

An event arrives by webhook, by a poller for what an application does not send as a webhook (timesheet approvals in the ATS here; payroll and accounting were polled in production), or from a cron trigger. It is inserted with a unique key of (source, event type, external id); a redelivery hits the conflict and does nothing. In the same transaction a run is queued for every workflow that listens for that event, so the event log is also the outbox. Workers claim due runs with `FOR UPDATE SKIP LOCKED` and execute the workflow's steps in order, committing after each one. A step's `with` block is rendered from the event and from earlier steps' outputs, its `when` condition can skip it, and its action is a named call on an adapter. Transient failures reschedule the run with exponential backoff; permanent failures and exhausted retries park it in the dead-letter queue, post to the ops channel with a link to the timeline, and wait for a person.

## Workflows

The five workflows in `workflows/` are the shape of the twelve that ran in production:

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `candidate_placed` | ATS webhook `placement.created` | Creates the payroll worker if none is linked, links the ids, sends the onboarding packet for signature, tells the recruiter, appends the placements sheet row |
| `onboarding_signed` | e-signature webhook `envelope.completed` | Marks the candidate onboarded in the ATS, upserts the CRM contact, logs the signing on their timeline |
| `timesheet_approved` | poll of the ATS every 60 s | Pushes hours to payroll and creates the invoice, or routes a timesheet that disagrees with the placement by more than four hours to the exceptions sheet and the billing channel |
| `client_lead_received` | web form webhook | Finds or creates the CRM company by email domain, upserts the contact, logs the enquiry, tells sales, sends an acknowledgement |
| `nightly_payroll_reconciliation` | cron `0 2 * * *` Chicago | Compares active placements with payroll workers and reports anyone missing before the pay run |

A step looks like this:

```yaml
  - id: submit_hours
    action: payroll.submit_hours
    when: not steps.check_hours.mismatch
    idempotency_key: "timesheet:{{ event.data.id }}:hours"
    retry:
      max_attempts: 6
      base_seconds: 5
    with:
      worker_id: "{{ steps.find_worker.target_id }}"
      period_start: "{{ steps.load_timesheet.period_start }}"
      period_end: "{{ steps.load_timesheet.period_end }}"
      hours: "{{ steps.load_timesheet.hours }}"
```

Workflow files are validated when a process starts: unknown actions, duplicate step ids and template syntax errors fail at load time, not at 2am. `hub validate` runs the same check from the command line.

## Design decisions

**Events are deduplicated by the database, not by the code.** Webhook delivery is at-least-once everywhere, and pollers overlap their windows on purpose. The `events` table has a unique constraint on (source, event type, external id) and ingestion is an `INSERT ... ON CONFLICT DO NOTHING`. Every path into the hub (webhook, poller, cron, the manual `hub trigger`) goes through the same function, so there is exactly one place where "have we seen this?" is answered, and it is answered by Postgres. See `src/hub/engine/ingest.py`.

**Side effects go through a ledger, committed before the call.** A step that writes to another application is given an idempotency key, either declared in the workflow (`timesheet:{{ event.data.id }}:invoice`) or defaulted to the run and step id. Before the adapter is called, a row for that key is committed to `effects` as `pending`; after the call it is marked `done` with the output. A retry that finds `done` reuses the output without calling again. A retry that finds `pending` knows an earlier attempt may have reached the application and the response was lost, which is the dangerous case: it asks the adapter to verify by key (the real APIs support this through idempotency headers or searchable external references, and the sandbox adapters store the key), repeats the call only if the adapter can say it never happened, and otherwise parks the run with a message saying what to check. Actions that tolerate a duplicate, such as a Slack message, declare `duplicate_tolerant` and are simply repeated. The same ledger makes keys work across runs: if the ATS sends `placement.created` twice with different event ids, the second run reuses the first run's envelope and sheet row. See `_run_effect` in `src/hub/engine/runner.py` and `tests/test_effects.py`.

**Checkpoint after every step, and keep the timeline.** A run's position is its `current_step` plus the outputs saved in its context, so a worker that dies mid-run leaves a row another worker resumes. Every attempt at every step is a row in `step_runs` with its output or error and duration. That table is the timeline the ops page shows and the first thing anyone looks at when a run is parked; in production it replaced most "what happened to this placement?" conversations.

**Errors are classified, and the classification decides the retry.** Adapters raise `TransientError` for rate limits, timeouts and 5xx responses and `PermanentError` for validation, authorisation and missing records. Transient failures back off exponentially with jitter up to the step's `max_attempts`; permanent failures dead-letter at once, because retrying a 422 five times only delays the person who has to fix the data. An unexpected exception is treated as permanent and parks the run with the traceback rather than crashing the worker. Bad template references are permanent too: a typo in a workflow file must never pass an empty value to a payroll system.

**Replay resumes where it failed, or earlier.** `hub replay <run>` puts a dead-lettered run back in the queue at the failed step. `--from-step` restarts at an earlier step and drops the checkpointed outputs from there on, for the case where a lookup has to be repeated after someone corrected the data; it is safe because any side effect on the way is reused through the ledger. `--skip-step` records that a person did the step by hand. `hub mark-effect-done <key>` resolves the parked "may have happened" case after someone checked the application.

**One crosswalk table maps identities across applications.** The same person is a candidate id in the ATS, a worker id in payroll and a contact id in the CRM. `entity_links` records each (entity type, system, external id) under a canonical id, and the `crosswalk.lookup` and `crosswalk.link` actions read and write it inside the run's transaction. Linking ids that already belong to two identities merges them. This was the hardest part of the production project, not technically but because the data was dirty, and keeping it in one table with one API is what made it tractable.

**Pollers and cron triggers are just another event source.** The ATS does not send a webhook when a timesheet is approved, so the scheduler polls with a persisted cursor and feeds the records through ingestion, where an overlap is a duplicate like any other. Cron triggers are evaluated against the last fire time, so a schedule registered at 10:00 does not fire for the 02:00 that already passed, and a scheduler that was down catches up one missed fire per tick.

**Rate limits live in the adapter, not in the workflow.** Each adapter owns a token bucket sized for its application and a retry class mapping. A workflow author never thinks about a 429; a step that would have to wait too long for a token raises a transient error and is rescheduled rather than holding a worker.

**Sandbox adapters are the real adapters' contract.** Each adapter registers its actions with `side_effect`, `verify` and `duplicate_tolerant` flags, and the sandbox implementations honour them the way the live ones do, including storing idempotency keys so verification works. Fault injection (`hub fault payroll.upsert_worker after 1`) makes the lost-response case reproducible from the command line, which is how the effects tests and the Compose demo exercise it.

## Running it

### With Docker

```bash
docker compose up --build
```

This starts Postgres, a one-shot `migrate` service that creates the schema and loads the synthetic data, the API on http://localhost:8000 (ops page at http://localhost:8000/ops), one worker and the scheduler. Then, from another terminal:

```bash
docker compose exec api hub simulate placement --api-url http://localhost:8000
docker compose exec api hub simulate duplicate --api-url http://localhost:8000   # second delivery is ignored
docker compose exec api hub simulate lead --api-url http://localhost:8000
docker compose exec api hub simulate timesheet --mismatch                        # picked up by the poller
docker compose exec api hub simulate timesheet --unlinked                        # dead-letters: no payroll worker
docker compose exec api hub trigger nightly_payroll_reconciliation
docker compose exec api hub runs
```

To watch a retry and the verify-by-key path:

```bash
docker compose exec api hub fault payroll.upsert_worker after 1
docker compose exec api hub simulate placement --api-url http://localhost:8000
docker compose exec api hub runs            # waiting_retry, then succeeded; one worker created, step reused
```

To fix the dead-lettered timesheet the way the ops team would:

```bash
docker compose exec api hub runs --status dead_lettered
docker compose exec api hub link person ats:cand_1006 payroll:wk_7006      # after creating the worker
docker compose exec api hub replay <run-id> --from-step find_worker
```

`scripts/smoke.sh` runs this sequence against the stack and checks the outcomes; CI runs it on every push.

### Without Docker

You need Python 3.12 and Postgres 16.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                      # set DATABASE_URL
hub seed
hub api                                   # terminal 1
hub worker                                # terminal 2
hub scheduler                             # terminal 3
hub simulate placement --api-url http://localhost:8000
```

Webhooks are verified with an HMAC-SHA256 signature of the raw body in the `X-Hub-Signature` header, using `WEBHOOK_SECRET` or a per-source `WEBHOOK_SECRET_<SOURCE>`. The simulator signs its requests the same way the applications would.

### API

| Route | Purpose |
| --- | --- |
| `POST /webhooks/{source}` | Signed inbound events; returns the event id, whether it was a duplicate, and the runs queued |
| `GET /runs`, `GET /runs/{id}` | Runs, filterable by status and workflow; a run's full timeline, context and event |
| `POST /runs/{id}/replay` | Replay a dead-lettered run, with `skip_step` or `from_step` |
| `GET /stats` | Counts by status and the unattended completion rate over a window |
| `GET /workflows`, `GET /actions` | The loaded catalog and the registered actions with their flags |
| `GET /ops`, `GET /ops/runs/{id}` | The operations page and the timeline view |
| `GET /sandbox/{system}/{collection}`, `POST /faults` | Inspect the sandbox applications; arm an injected failure |

## Tests

```bash
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/hub_test pytest
```

The suite runs against a real Postgres (CI uses the `postgres:16` service), because the behaviour that matters here is database behaviour: the unique-key dedupe, `SKIP LOCKED` claiming, the ledger commit ordering. Time is a frozen clock, so backoff and cron tests take milliseconds. The tests cover: definition validation; templating; signed webhook ingestion and dedupe; concurrent claiming; each shipped workflow end to end against the sandbox applications, including the mismatch path, the dead-letter and replay path, and the nightly reconciliation; backoff, exhaustion, permanent and crash handling; the effects ledger under lost responses, unverifiable effects, duplicate-tolerant effects and cross-run keys; pollers, cursors and cron catch-up; the token bucket; the crosswalk; the API and ops pages; and that the seed data is synthetic.

## Layout

```
src/hub/
  api/           FastAPI app, webhook signature check, ops templates
  adapters/      one module per application, sandbox store, fault injector, crosswalk, webhook envelopes
  engine/        definitions (YAML schema), templating, ingest, runner, scheduler, retry, rate limit
  db/            models, engine, SKIP LOCKED claim
  cli.py         hub command: seed, api, worker, scheduler, runs, show, replay, link, fault, simulate, trigger
  seed.py        synthetic data
  simulate.py    emits events the way the applications would
workflows/       the five workflow definitions
tests/
scripts/smoke.sh Compose stack smoke test used by CI
```

## What is deliberately left out

- The live HTTP adapters for the ATS, CRM, e-signature, payroll, accounting and Sheets. Each production adapter was a thin client behind the same `Adapter` interface with the same action flags; the sandbox versions here keep the contract, including idempotency-key verification, so the engine code is unchanged.
- The shared rate-limit budget across worker processes (a Postgres advisory counter in production). Here each worker process has its own bucket per adapter.
- Authentication on the ops page and the runs API, which sat behind the company's SSO proxy.
- The simple, low-volume flows that stayed in Make. They were never part of the hub.
- Metrics export. The `/stats` endpoint is what the production dashboard read; the dashboard itself is not included.

## License

MIT, Alex Kurinets.
