# SupportFlow runbook

What to check when something goes wrong. Start with the two monitoring endpoints:

- `GET /failures` shows each failure with its **stage** (`parse`, `classify` or `notify`) and the error.
- `GET /events` shows each event's **status**: `received`, `classified`, `notified`, `failed` or `notify_failed`.

The server log (the terminal running `uvicorn`) has a line for every accepted, rejected, classified and notified event.

---

## Tally submissions don't show up at all

Nothing in `/events`, nothing in the log.

1. **Is the server running?** Open http://localhost:8000/health. It should return `{"status": "ok"}`.
2. **Is ngrok running, and is the URL current?** Free ngrok URLs change every time ngrok restarts. Compare the URL in the ngrok window with the one in Tally (Integrations > Webhooks). The path must end in `/webhooks/tally`.
3. **Did Tally try to deliver?** Tally's webhook settings show recent deliveries and their response codes. The ngrok inspector at http://127.0.0.1:4040 shows every request that reached your laptop.

## Webhooks are rejected with 401

The log says `rejected webhook with invalid signature`.

1. `TALLY_SIGNING_SECRET` in `.env` must exactly match the signing secret in Tally's webhook settings (no extra spaces or quotes).
2. Restart the server after editing `.env`; settings are read at startup.
3. To test the server on its own, run `python scripts/send_test_webhook.py`. It signs requests with your `.env` secret, so a `202` there means the server is fine and the mismatch is in Tally.

## Webhooks are rejected with 422

`/failures` shows stage `parse`: the form doesn't have a field the app recognises as the message. Rename the main text field in Tally so its label contains "Message".

## Events are stuck in `received`

Classification never finished. Usually the server restarted while the event was being processed, or the process crashed. Check the log for `unexpected error while processing event`. A job queue with automatic replay is on the roadmap.

## Events end in `failed` (stage `classify`)

The LLM step failed twice. The error in `/failures` says why:

| Error contains | Meaning | Fix |
|---|---|---|
| `Could not reach Ollama` | Ollama isn't running | Start the Ollama app, or run `ollama serve` |
| `HTTP 404` and `model` | The model isn't downloaded | `ollama pull qwen2.5:3b` (or match `OLLAMA_MODEL` in `.env`) |
| `timed out` | The model is too slow on this machine | Raise `LLM_TIMEOUT_SECONDS`, close other heavy apps, or use a smaller model |
| `not valid JSON` / `field '...'` | The model answered in the wrong format twice | Try the message with `python scripts/try_classifier.py "..."`; consider a larger model |

## Events end in `notify_failed` (stage `notify`)

Classification worked, Slack didn't.

| Error contains | Meaning | Fix |
|---|---|---|
| `rejected` + `HTTP 404` / `no_service` | The webhook URL is wrong or was revoked | Create a new incoming webhook in the Slack app settings and update `SLACK_WEBHOOK_URL` |
| `HTTP 403` / `invalid_token` | The Slack app was removed from the channel | Reinstall the app to the workspace |
| `after 3 attempts` + `429` / `5xx` / `network error` | Slack or the network was down | Usually temporary; check https://status.slack.com |

Run `python scripts/test_slack.py` to test the Slack connection on its own.

## Resetting local data

Stop the server and delete `supportflow.db`. It is recreated empty on the next start. The file is git-ignored.
