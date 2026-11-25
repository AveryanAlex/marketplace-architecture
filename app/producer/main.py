import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException

from app.common.config import settings
from app.common.logging import configure_logging
from app.common.retry import retry
from app.producer.events import MovieEvent
from app.producer.generator import run_generator
from app.producer.kafka import EventPublisher

configure_logging()
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app):
    publisher = EventPublisher(settings())
    await retry(publisher.start, attempts=8, name="producer_start")
    app.state.publisher = publisher
    generator = None
    if settings().generator_enabled:

        async def supervise():
            while True:
                try:
                    await run_generator(publisher, settings())
                except Exception as exc:
                    log.error("generator_failed", error=type(exc).__name__)
                    await asyncio.sleep(5)

        generator = asyncio.create_task(supervise())
    yield
    if generator:
        generator.cancel()
        await asyncio.gather(generator, return_exceptions=True)
    await publisher.close()


app = FastAPI(title="Movie Service", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    try:
        await app.state.publisher.healthy()
    except Exception as exc:
        raise HTTPException(503, "Kafka unavailable") from exc
    return {"status": "ok", "schema_version": settings().schema_version}


@app.post("/events", status_code=201)
async def publish(event: MovieEvent):
    try:
        return await app.state.publisher.publish(event)
    except Exception as exc:
        log.error("publication_failed", event_id=str(event.event_id), error=type(exc).__name__)
        raise HTTPException(
            503,
            {
                "message": "Publication unconfirmed; retry with the same event_id",
                "event_id": str(event.event_id),
            },
        ) from exc
