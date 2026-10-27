"""Run the job queue worker as its own process: `python -m app.worker`.

In production, run one or more of these next to the web server (with
RUN_WORKER_IN_PROCESS=false there). Several workers are safe: each event is leased
to one worker at a time.
"""

import signal

from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.jobs import JobRunner, RetryPolicy, Worker
from app.observability import configure_logging
from app.pipeline import build_processor


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    runner = JobRunner(
        session_factory, build_processor(settings, session_factory), RetryPolicy.from_settings(settings)
    )
    worker = Worker(runner, poll_seconds=settings.worker_poll_seconds)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop())
    worker.run_forever()


if __name__ == "__main__":
    main()
