# SupportFlow runbook

What to check when something goes wrong. Start with the two monitoring endpoints:

- `GET /failures` shows each failure with its **stage** (`parse`, `classify` or `notify`) and the error.
- `GET /events` shows each event's **status**: `received`, `classified`, `notified`, `failed` or `notify_failed`.

The server log (the terminal running `uvicorn`) has a line for every accepted, rejected, classified and notified event.

Most failures can be retried once the cause is fixed, without re-sending anything from Tally:

```bash
python scripts/replay_events.py --dry-run   # what would be replayed
python scripts/replay_events.py             # replay every unfinished event
```

Classified events are not sent to the LLM again, and notified events are never touched. Events updated in the last 5 minutes are skipped, so the script is safe to run while the server is up.

---

## Tally submissions don't show up at all

Nothing in `/events`, nothing in the log.

1. **Is the server running?** Open http://localhost:8000/health. It should return `{"status": "ok", "database": "ok"}`.
2. **Is ngrok running, and is the URL current?** Free ngrok URLs change every time ngrok restarts. Compare the URL in the ngrok window with the one in Tally (Integrations > Webhooks). The path must end in `/webhooks/tally`.
3. **Did Tally try to deliver?** Tally's webhook settings show recent deliveries and their response codes. The ngrok inspector at http://127.0.0.1:4040 shows every request that reached your laptop.

## Webhooks are rejected with 401

The log says `rejected webhook with invalid signature`.

1. If the startup log says `TALLY_SIGNING_SECRET is not set`, the secret is empty or still the `change-me` placeholder. Every webhook is rejected until it is set.
2. `TALLY_SIGNING_SECRET` in `.env` must exactly match the signing secret in Tally's webhook settings (no quotes; surrounding spaces are ignored).
3. Restart the server after editing `.env`; settings are read at startup.
4. To test the server on its own, run `python scripts/send_test_webhook.py`. It signs requests with your `.env` secret, so a `202` there means the server is fine and the mismatch is in Tally.

## Webhooks are rejected with 400 or 422

- `400`: the body is not JSON, or `eventId` is missing or longer than 128 characters. The log line starting `rejected webhook:` says which. This should not happen with real Tally deliveries.
- `422`: `/failures` shows stage `parse`: the form doesn't have a field the app recognises as the message. Rename the main text field in Tally so its label contains "Message".

## Events are stuck in `received`

Classification never finished. Usually the server restarted while the event was being processed, or the process crashed. Check the log for `unexpected error while processing event`, then run `python scripts/replay_events.py --status received`.

## Events end in `failed` (stage `classify`)

The LLM step failed twice. The error in `/failures` says why:

| Error contains | Meaning | Fix |
|---|---|---|
| `Could not reach Ollama` | Ollama isn't running | Start the Ollama app, or run `ollama serve` |
| `HTTP 404` and `model` | The model isn't downloaded | `ollama pull qwen2.5:3b` (or match `OLLAMA_MODEL` in `.env`) |
| `timed out` | The model is too slow on this machine | Raise `LLM_TIMEOUT_SECONDS`, close other heavy apps, or use a smaller model |
| `not valid JSON` / `field '...'` | The model answered in the wrong format twice | Try the message with `python scripts/try_classifier.py "..."`; consider a larger model |
| `Unexpected response shape` | Ollama answered, but not in the chat API format | Check `OLLAMA_URL` points at Ollama itself, not a proxy or another service |

After fixing the cause, run `python scripts/replay_events.py --status failed`.

## Events end in `notify_failed` (stage `notify`)

Classification worked, Slack didn't.

| Error contains | Meaning | Fix |
|---|---|---|
| `rejected` + `HTTP 404` / `no_service` | The webhook URL is wrong or was revoked | Create a new incoming webhook in the Slack app settings and update `SLACK_WEBHOOK_URL` |
| `invalid SLACK_WEBHOOK_URL` | The URL is malformed (for example, missing `https://`) | Copy the full URL from the Slack app settings |
| `HTTP 403` / `invalid_token` | The Slack app was removed from the channel | Reinstall the app to the workspace |
| `after 3 attempts` + `429` / `5xx` / `network error` | Slack or the network was down | Usually temporary; check https://status.slack.com |

Run `python scripts/test_slack.py` to test the Slack connection on its own. Once it works (restart the server if you changed `.env`), resend the missed notifications with `python scripts/replay_events.py --status notify_failed`.

## Resetting local data

Stop the server and delete `supportflow.db`. It is recreated empty on the next start. The file is git-ignored.
