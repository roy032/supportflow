# SupportFlow

A small AI helpdesk for an online shop. Customer messages arrive from a Tally form as signed webhooks. An LLM classifies each one and drafts a reply from the shop's own policy documents. The team gets the ticket in Slack and in a web inbox, checks or edits the draft, and sends it to the customer by email in one click. Tickets that wait too long are escalated. Every step is stored, and every failure is recorded with the stage where it happened.

Messages can be in English, Bangla, or a mix of both.

## How it works

```mermaid
flowchart LR
    A[Tally form] -->|signed webhook| B[POST /webhooks/tally]
    B --> C{Signature valid?}
    C -->|no| X[401, nothing stored]
    C -->|yes| D{Seen this eventId?}
    D -->|yes| Y[200 duplicate, ignored]
    D -->|no| E[(events<br/>= job queue)]
    E -->|worker takes a lease| F[LLM classification<br/>JSON schema + Pydantic]
    F -->|invalid output| G[retry once with feedback]
    G -->|still invalid| H[(failures)]
    F -->|valid| I[(classifications)]
    I --> J[Slack incoming webhook<br/>retries with backoff]
    J -->|gave up| H
    H -.->|retry with backoff,<br/>dead after N runs| E
```

1. **Intake.** The webhook endpoint verifies Tally's HMAC-SHA256 signature over the raw body, rejects anything unsigned, and stores the event. It responds immediately with `202 Accepted`.
2. **Classification.** A worker picks up the queued event and sends the message to the LLM, which must return JSON matching a schema: category, urgency, sentiment, language, order ID and a one-line summary. The output is validated with Pydantic.
3. **Notification.** The result is posted to a Slack channel.
4. **Retries.** If either step fails, the event is retried automatically with exponential backoff. After `JOB_MAX_ATTEMPTS` runs it is marked `dead` for a person to look at.
5. **Monitoring.** `/events` shows recent events and their status; `/failures` shows what went wrong and where.

### The helpdesk

- **Tickets.** Every message is a ticket: `open` (needs a reply), `pending` (we replied, waiting on the customer) or `resolved`. Tickets can be assigned to a person. Spam is resolved automatically (`AUTO_RESOLVE_SPAM`).
- **Suggested replies.** After classification, the AI writes a draft using only the sections of `knowledge/*.md` that match the message, in the customer's language. It flags drafts a person must check first: no matching policy, an angry customer, or anything that needs an order looked up. Drafts are never sent on their own. Edit the Markdown files to teach it your policies (see [knowledge/README.md](knowledge/README.md)).
- **Email replies.** Replies go to the customer over SMTP from the dashboard, the API or Slack. A failed send is kept with its error and the ticket stays open.
- **Slack buttons.** With a Slack app's signing secret set, each Slack message shows the draft with **Send suggested reply** (asks for confirmation), **Resolve** and **Open ticket** buttons.
- **SLA escalation.** When a ticket has had no reply for longer than its urgency allows (`SLA_HIGH_MINUTES`, `SLA_MEDIUM_MINUTES`, `SLA_LOW_MINUTES`), the worker posts one reminder to Slack.
- **Dashboard.** `/dashboard` is a sign-in-protected inbox, urgent and oldest first, with filters, search, "assigned to me", and a ticket page showing the message, the classification, the draft and the reply history.

## Tech stack

Python, FastAPI, SQLAlchemy 2.0 with Alembic migrations (SQLite locally, Postgres-ready), Pydantic, httpx, Ollama (local LLM, `qwen2.5:3b` by default) or Claude through the Anthropic SDK, Prometheus metrics, Slack incoming webhooks, pytest, GitHub Actions.

## Design decisions

- **Verify before parsing.** The signature is checked on the raw request bytes before the body is parsed or stored, and the comparison is constant-time. If no secret is configured (or the `.env.example` placeholder is left in place), every request is rejected and a warning is logged at startup, so a misconfigured server fails closed.
- **Customer data is behind a key.** `/events` and `/failures` return names, emails and messages, so they require `Authorization: Bearer <ADMIN_API_KEY>`, compared in constant time. Like the webhook, they fail closed when no key is set. The webhook also refuses bodies over `MAX_WEBHOOK_BODY_BYTES` before reading them into memory.
- **Customer text can't format Slack.** The message, name and summary are escaped before they go into Slack mrkdwn, so a form submission can't ping `@channel` or disguise a phishing link.
- **Idempotent intake.** Webhook senders retry, so the same event can arrive more than once. Tally's `eventId` has a unique index; a repeat delivery returns `200 duplicate` and is not processed again, even if two deliveries race.
- **Respond fast, work later.** The endpoint only verifies and stores. Classification and Slack run after the response is sent, so a slow model never causes Tally to time out and retry.
- **No event is lost.** The `events` table doubles as a job queue. A worker claims an event with one conditional `UPDATE` that takes a lease, so two workers never run the same event, and a worker that dies mid-run just lets its lease expire for another to take over. Failed runs are retried with capped, jittered exponential backoff and end in `dead` after `JOB_MAX_ATTEMPTS`. This needs no Redis or broker and behaves the same on SQLite and Postgres. The lease is validated to outlast a worst-case run, so a slow model can't cause a double Slack post.
- **Observable by default.** Every response carries an `X-Request-ID` (taken from the caller when it is safe, otherwise generated), and every log line written while handling a request or running an event carries its `request_id` / `event_id`. `LOG_FORMAT=json` makes the logs one JSON object per line. `/metrics` serves Prometheus metrics. Queue depth, queue lag, events by status, failures in the last hour and LLM latency are read from the database at scrape time, so the numbers are right however many web servers and workers run.
- **Schema changes are migrations.** Alembic migrations run on startup, and a database created before migrations existed is detected and upgraded in place.
- **Structured, validated LLM output.** The model is constrained to a JSON schema, and the result is validated again with Pydantic. Invalid output gets one retry that tells the model what was wrong; if it still fails, the failure is recorded instead of crashing the worker.
- **Provider interface.** The pipeline talks to an `LLMProvider` interface. Ollama keeps the project free and offline; Claude (`LLM_PROVIDER=anthropic`) is one setting away. Claude's structured output is constrained to the same JSON schema, and its server-side fallback re-runs a request that a safety classifier declined. `LLM_FALLBACK_PROVIDER` puts a second provider behind the first, for example Claude behind a local model that is down. Token counts are stored with every classification, so cost per message is known.
- **Model choices are measured, not guessed.** `scripts/run_eval.py` scores any provider and model against a labelled set of English, Bangla and mixed messages (including spam and a prompt-injection attempt) and reports accuracy per field, latency, tokens and cost.
- **Safe to re-run.** An event that is already classified is never sent to the LLM again; if only the Slack step failed, a retry resends the notification without paying for a second classification.
- **Retry only what can succeed.** Slack calls retry network errors, `429` (honouring `Retry-After`, capped at 30 seconds) and `5xx` with exponential backoff. Other `4xx` errors and a malformed webhook URL fail immediately.
- **Bounded waits.** Slack calls time out after 10 seconds. Ollama gets `LLM_TIMEOUT_SECONDS` to generate, but only 10 seconds to connect, so an unreachable host fails fast.
- **Drafts are grounded and never auto-sent.** The drafting prompt only contains policy sections retrieved for that message, and the customer's text is passed as data with an instruction to ignore embedded instructions. If nothing in the knowledge base matched, the draft is always flagged for a person. A draft that fails is recorded as a `draft` failure and never blocks classification or Slack.
- **Each button click is checked.** Slack requests are verified with Slack's v0 HMAC signature and rejected when older than 5 minutes, so a captured request can't be replayed. The dashboard uses signed, HttpOnly, SameSite cookies, a CSRF token on every form, and a sign-in throttle. Escalations are claimed with a conditional `UPDATE` first, so two workers never post the same reminder.
- **Forms can change.** Tally fields are matched by label keywords, so the form can be edited without code changes.

## Getting started

Requirements: Python 3.11+, [Ollama](https://ollama.com), and optionally a Slack workspace, a [Tally](https://tally.so) form and [ngrok](https://ngrok.com).

```bash
git clone https://github.com/roy032/supportflow.git
cd supportflow
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements-dev.txt

cp .env.example .env              # Windows: copy .env.example .env
# then edit .env: TALLY_SIGNING_SECRET, ADMIN_API_KEY, SLACK_WEBHOOK_URL

ollama pull qwen2.5:3b
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive API docs.

### Try it without Tally

With the server running, in a second terminal:

```bash
python scripts/try_classifier.py "Where is my order ORD-12?"   # check the model on its own
python scripts/test_slack.py                                    # check the Slack connection
python scripts/send_test_webhook.py                             # send English, Bangla and spam samples
```

Then look at how each one was classified:

```bash
curl -H "Authorization: Bearer $ADMIN_API_KEY" http://localhost:8000/events
```

Or open http://localhost:8000/docs, click **Authorize**, paste the key, and call `/events` from there.

### Run with Docker (Postgres, web server and worker)

```bash
cp .env.example .env               # set TALLY_SIGNING_SECRET, ADMIN_API_KEY, SLACK_WEBHOOK_URL
docker compose up --build          # uses Ollama running on your machine
```

This starts Postgres, the web server on port 8000 and a separate worker process. Migrations run on startup; an advisory lock makes sure only one process applies them. To run Ollama in a container as well: `DOCKER_OLLAMA_URL=http://ollama:11434 docker compose --profile ollama up`, then `docker compose exec ollama ollama pull qwen2.5:3b`.

The image runs as a non-root user and has a health check against `/health`.

### Run the worker separately (production)

By default a worker thread runs inside the web server, which is all you need locally. In production, set `RUN_WORKER_IN_PROCESS=false` for the web server and run one or more workers next to it:

```bash
python -m app.worker
```

Several workers (and several web servers) are safe at once; each event is leased to one worker at a time.

### Replay dead events

Failures are retried automatically. Events the worker gave up on (`dead`), and events from before the job queue existed, can be queued again with a fresh retry budget once the cause is fixed:

```bash
python scripts/replay_events.py --dry-run               # list unfinished events
python scripts/replay_events.py --status dead           # events the worker gave up on
python scripts/replay_events.py --status notify_failed  # only Slack failures
python scripts/replay_events.py --event-id 12           # one event
```

Only events last updated at least 5 minutes ago are picked, so work the server is still doing is left alone. Notified events are never touched.

### Monitoring with Prometheus

Point Prometheus at `/metrics` with the admin key:

```yaml
scrape_configs:
  - job_name: supportflow
    authorization:
      credentials: <ADMIN_API_KEY>
    static_configs:
      - targets: ["supportflow:8000"]
```

Good first alerts: `supportflow_queue_lag_seconds > 600` (workers are down or falling behind), `increase(supportflow_events{status="dead"}[1h]) > 0`, and a rising `supportflow_failures_last_hour`.

### Compare models

```bash
python scripts/run_eval.py --provider ollama --model qwen2.5:3b
python scripts/run_eval.py --provider anthropic --model claude-opus-5-5 --json-out opus.json
python scripts/run_eval.py --min-category-accuracy 0.9    # exits 1 below 90%, for CI or release checks
```

The labelled messages live in `evals/classification.jsonl`. Add a line whenever the model gets a real message wrong, so the set grows with what the shop actually receives. Cloud runs send every message to the provider and cost a few cents for the bundled set.

### Database migrations

Migrations run automatically when the server or worker starts. After changing `app/models.py`, generate a migration and review it before committing:

```bash
alembic revision --autogenerate -m "describe the change"
```

### Set up the helpdesk

1. **Policies.** Replace the sample files in `knowledge/` with your shop's real refund, shipping and payment rules. Each `##` heading is one searchable section.
2. **Email.** Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_FROM` for any SMTP provider. Until then, replies can be drafted but not sent.
3. **Dashboard.** Set `DASHBOARD_PASSWORD` (12+ characters) and open `http://localhost:8000/dashboard`. Sign in with your name and the shared password; your name is recorded on the replies you send.
4. **Slack buttons (optional).** Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps), turn on **Incoming Webhooks** and use that webhook as `SLACK_WEBHOOK_URL`. Turn on **Interactivity** with the request URL `<PUBLIC_URL>/slack/interactions`, and put the app's **Signing Secret** in `SLACK_SIGNING_SECRET`. Set `PUBLIC_URL` so the **Open ticket** button links to the dashboard.

### Connect a real Tally form

1. Run `ngrok http 8000` and copy the `https://...` forwarding URL.
2. In Tally, open the form, go to **Integrations > Webhooks**, and add `https://<your-ngrok-url>/webhooks/tally`.
3. Set a signing secret there and put the same value in `TALLY_SIGNING_SECRET` in `.env`, then restart the server.
4. The form needs a field whose label contains "Message". Fields labelled with "Name", "Email" and "Order" are picked up too.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhooks/tally` | Receive a signed Tally webhook (`202` accepted, `200` duplicate, `401` bad signature, `400`/`422` bad payload, `413` too large) |
| `GET` | `/events?limit=20` | Recent events with status and classification (admin key) |
| `GET` | `/failures?limit=20` | Recent failures with pipeline stage and error (admin key) |
| `GET` | `/metrics` | Prometheus metrics (admin key) |
| `GET` | `/tickets?status=open` | Ticket inbox; `status` is `open`, `pending`, `resolved` or `all`, with `category`, `urgency`, `assignee`, `q`, `limit`, `offset` filters (admin key) |
| `GET` | `/tickets/{id}` | One ticket with its classification, draft and replies (admin key) |
| `PATCH` | `/tickets/{id}` | Change `ticket_status` or `assignee` (admin key) |
| `POST` | `/tickets/{id}/reply` | Email a reply: `{"body", "author", "resolve"}`; `409` if it can't be sent (admin key) |
| `POST` | `/slack/interactions` | Slack button clicks, verified with `SLACK_SIGNING_SECRET` |
| `GET` | `/dashboard` | Agent dashboard (password sign-in) |
| `GET` | `/health` | Liveness and database check |

Event status moves through `received`, `classified`, `notified`. `failed` (classification) and `notify_failed` (Slack) are retried automatically; after `JOB_MAX_ATTEMPTS` runs the event becomes `dead`. Each event in `/events` also shows `attempts` and `next_attempt_at`.

## Tests

```bash
pytest -q
ruff check .
```

The suite covers signature verification, the helpdesk (knowledge search, drafts, sending replies, the inbox, SLA escalation, Slack buttons and the dashboard's sign-in, CSRF and escaping), idempotency, payload parsing (including Bangla text), settings validation, classifier retries and validation, Slack retry behaviour, replay of unfinished events, the job queue (leases, backoff, dead-lettering), migrations, and the full webhook-to-Slack flow. External services are mocked, so tests need no network, model or Slack workspace.

Tests use in-memory SQLite. To run the same suite against Postgres:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/supportflow_test pytest -q
```

GitHub Actions runs ruff, the tests on Python 3.11 to 3.13, the tests against Postgres 16, and a Docker build that must start and pass `/health`, on every push.

## Project layout

```
app/
  main.py         FastAPI app: webhook intake, /events, /failures, /health
  security.py     Tally signature verification
  tally.py        Tally payload -> SupportMessage
  classifier.py   Prompt, JSON schema, validation and retry logic
  llm/            LLMProvider interface, fallback wrapper, Ollama and Claude providers
  evaluation.py   Scores a provider against evals/classification.jsonl
  pipeline.py     Classify -> store -> notify, with failure recording
  jobs.py         Job queue on the events table: leases, retries with backoff, dead-lettering
  worker.py       Standalone worker process: python -m app.worker
  observability.py  JSON logs, request ids, Prometheus metrics
  migrations/     Alembic migrations, applied on startup
  replay.py       Find unfinished events and run them through the pipeline again
  slack.py        Slack message formatting and retrying sender
  slack_actions.py  Slack button handler with signature verification
  knowledge.py    Loads knowledge/*.md and finds the sections that match a message
  drafting.py     AI reply drafts grounded in those sections
  helpdesk.py     Ticket inbox, status, assignment and sending replies
  mailer.py       SMTP email
  escalation.py   SLA reminders, run by the worker
  api_tickets.py  /tickets API
  dashboard.py    Agent dashboard (templates/ holds its pages)
  web_sessions.py Signed session cookies and CSRF tokens
  models.py       events (tickets), classifications, replies, failures tables
knowledge/        Shop policies the AI drafts replies from (samples; replace with yours)
scripts/          Test webhook sender, Slack test, classifier playground, event replay
tests/            pytest suite
RUNBOOK.md        What to check when something breaks
DEVELOPMENT.md    Conventions and commands for working on the code
```

## Known limitations

- A small local model is fast and free but less accurate than larger models, especially on mixed-language messages.
- Only Tally is supported as a source so far, and customers reply by email outside the app: a customer's answer doesn't reopen the ticket yet.
- The dashboard has one shared password; there are no per-person accounts or roles.
- Knowledge search is keyword matching, which works well for a few dozen policy sections but isn't semantic search.

## Roadmap

- Receive customer email replies (inbound email webhook) and reopen the ticket
- More sources: Facebook Messenger and WhatsApp, where most Bangladeshi shops talk to customers
- Per-person accounts, and a daily summary of volume, response times and top issues

