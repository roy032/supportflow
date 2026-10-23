"""Re-run unfinished events through the pipeline (classification and Slack).

Failed events are retried automatically (see app/jobs.py). Use this for events the
worker gave up on (`dead`), after fixing a configuration problem, or for events from
before the job queue existed. Each replayed event gets a fresh retry budget. Events that
are already classified are not sent to the LLM again, and notified events are never
touched.

Usage:
    python scripts/replay_events.py                          # all unfinished events
    python scripts/replay_events.py --status dead            # events the worker gave up on
    python scripts/replay_events.py --status notify_failed   # only Slack failures
    python scripts/replay_events.py --event-id 12 --event-id 15
    python scripts/replay_events.py --dry-run                # list, don't process

By default only events last updated at least 5 minutes ago are picked, so work the
running server is still doing is left alone. Explicit --event-id values skip that check.
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import init_db, make_engine, make_session_factory  # noqa: E402
from app.jobs import JobRunner, RetryPolicy  # noqa: E402
from app.models import EventStatus  # noqa: E402
from app.pipeline import build_processor  # noqa: E402
from app.replay import UNFINISHED_STATUSES, find_unfinished, replay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--status", action="append", choices=UNFINISHED_STATUSES,
        help="only replay events with this status (repeatable; default: all unfinished)",
    )
    parser.add_argument(
        "--event-id", action="append", type=int, help="replay this event (repeatable)"
    )
    parser.add_argument(
        "--min-age-minutes", type=float, default=5.0,
        help="skip events updated more recently than this (default: 5)",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    if args.event_id:
        event_ids = args.event_id
    else:
        with session_factory() as session:
            event_ids = find_unfinished(
                session,
                statuses=args.status or UNFINISHED_STATUSES,
                min_age=timedelta(minutes=args.min_age_minutes),
                limit=args.limit,
            )

    if not event_ids:
        print("Nothing to replay.")
        return 0
    if args.dry_run:
        print("Would replay events:", ", ".join(str(i) for i in event_ids))
        return 0

    if not settings.slack_webhook_url:
        print("Note: SLACK_WEBHOOK_URL is not set, so events will stop at `classified`.")

    runner = JobRunner(
        session_factory, build_processor(settings, session_factory),
        RetryPolicy.from_settings(settings),
    )
    runner.requeue(event_ids)
    # A worker could claim a requeued event first; runner.run then skips it, and the
    # status printed below is whatever that worker has reached.
    results = replay(event_ids, runner.run, session_factory)
    still_failing = 0
    for event_id, (before, after) in results.items():
        if before is None:
            print(f"event {event_id}: not found")
            continue
        print(f"event {event_id}: {before} -> {after}")
        still_failing += after in (
            EventStatus.RECEIVED, EventStatus.FAILED, EventStatus.NOTIFY_FAILED, EventStatus.DEAD
        )

    print("\nDetails of any new failures: GET /failures")
    return 1 if still_failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
