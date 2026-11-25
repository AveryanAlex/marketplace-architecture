import asyncio
import random
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog

from app.producer.events import MovieEvent

log = structlog.get_logger()


def session_events(user_id: str, start: datetime, *, seed: int | None = None, stable=False):
    """One chronological session; equal pause/resume progress is intentional."""
    rng = random.Random(seed)
    session_id = (
        str(uuid5(NAMESPACE_URL, f"cinema-demo:{user_id}:{start.isoformat()}"))
        if stable
        else str(uuid4())
    )
    movie_id = f"movie-{rng.randint(1, 12):02d}"
    device = rng.choice(["MOBILE", "DESKTOP", "TV", "TABLET"])
    duration = rng.randint(1800, 7200)
    paused = duration // 3
    sequence = [
        ("SEARCHED", 0, None),
        ("VIEW_STARTED", 2, 0),
        ("VIEW_PAUSED", paused + 2, paused),
        ("VIEW_RESUMED", paused + 62, paused),
    ]
    if rng.random() < 0.8:
        sequence.extend(
            [
                ("VIEW_FINISHED", duration + 62, duration),
                ("LIKED", duration + 65, None),
            ]
        )
    return [
        MovieEvent(
            event_id=uuid5(NAMESPACE_URL, f"{session_id}:{kind}") if stable else uuid4(),
            user_id=user_id,
            movie_id=movie_id,
            event_type=kind,
            timestamp=start + timedelta(seconds=offset),
            device_type=device,
            session_id=session_id,
            progress_seconds=progress,
            app_version="2.0-demo",
        )
        for kind, offset, progress in sequence
    ]


def demo_events(today=None):
    """Stable identities on restart: 10 historical cohorts with D0..D7 returns."""
    today = today or datetime.now(UTC).date()
    for cohort_age in range(1, 11):
        cohort = today - timedelta(days=cohort_age)
        for person in range(8):
            user = f"demo:{cohort.isoformat()}:{person}"
            for day in range(min(7, cohort_age - 1) + 1):
                if day and person >= max(1, 7 - day):
                    continue
                start = datetime.combine(cohort + timedelta(days=day), datetime.min.time(), UTC)
                start += timedelta(hours=10, minutes=person * 3)
                yield from session_events(user, start, seed=person + day, stable=True)


async def run_generator(publisher, config):
    if config.demo_seed_enabled:
        for event in demo_events():
            await publisher.publish(event)
        log.info("demo_history_seeded")
    user = 0
    while True:
        # Synthetic timestamps are in the past; publish in chronological session order.
        # user IDs are bounded to produce both new and returning users.
        start = datetime.now(UTC) - timedelta(hours=3)
        events = session_events(f"live:{user % 100}", start)
        try:
            for event in events:
                await publisher.publish(event)
                await asyncio.sleep(config.generator_interval_seconds)
        except Exception as exc:
            log.error("generator_publish_failed", error=type(exc).__name__)
            await asyncio.sleep(config.generator_interval_seconds)
        user += 1
