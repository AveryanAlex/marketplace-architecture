import asyncio
import io
import json
import struct
from pathlib import Path

import httpx
import structlog
from confluent_kafka import Producer
from fastavro import parse_schema, schemaless_writer
from fastavro.validation import validate

from app.common.config import Settings
from app.common.retry import retry
from app.producer.events import MovieEvent

log = structlog.get_logger()


class EventPublisher:
    def __init__(self, config: Settings):
        self.config = config
        self.schema = None
        self.schema_id = None
        self.producer = None
        self._poll_task = None

    async def start(self):
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{self.config.schema_registry_url}/subjects/"
                f"{self.config.schema_subject}/versions/{self.config.schema_version}"
            )
            response.raise_for_status()
            info = response.json()
        registry_schema = json.loads(info["schema"])
        local_schema = json.loads(
            await asyncio.to_thread(
                Path(f"schemas/movie-event-v{self.config.schema_version}.avsc").read_text
            )
        )
        if registry_schema != local_schema:
            raise RuntimeError("Registry schema does not match the pinned application schema")
        self.schema = parse_schema(registry_schema)
        self.schema_id = info["id"]
        self.producer = Producer(
            {
                "bootstrap.servers": self.config.kafka_bootstrap,
                "client.id": "movie-service",
                "enable.idempotence": True,
                "acks": "all",
                "max.in.flight.requests.per.connection": 5,
                "retries": 2147483647,
                "retry.backoff.ms": 200,
                "retry.backoff.max.ms": 2000,
                "delivery.timeout.ms": int(self.config.publish_timeout_seconds * 1000),
                "request.timeout.ms": 5000,
                "compression.type": "lz4",
                "allow.auto.create.topics": False,
            }
        )
        self._poll_task = asyncio.create_task(self._poll())

    async def _poll(self):
        while True:
            await asyncio.to_thread(self.producer.poll, 0.01)

    def encode(self, event: MovieEvent) -> bytes:
        record = event.avro_record()
        validate(record, self.schema)
        output = io.BytesIO()
        output.write(struct.pack(">bI", 0, self.schema_id))
        schemaless_writer(output, self.schema, record)
        return output.getvalue()

    async def publish(self, event: MovieEvent):
        payload = self.encode(event)
        loop = asyncio.get_running_loop()

        async def send():
            delivered = loop.create_future()

            def complete(error, message):
                def finish():
                    if delivered.done():
                        return
                    if error:
                        delivered.set_exception(RuntimeError(str(error)))
                    else:
                        delivered.set_result((message.partition(), message.offset()))

                loop.call_soon_threadsafe(finish)

            self.producer.produce(
                self.config.kafka_topic,
                key=event.user_id.encode(),
                value=payload,
                on_delivery=complete,
            )
            return await asyncio.wait_for(delivered, self.config.publish_timeout_seconds + 2)

        partition, offset = await retry(
            send, attempts=self.config.publish_attempts, name="publish_event"
        )
        log.info(
            "event_published",
            event_id=str(event.event_id),
            event_type=event.event_type.value,
            event_timestamp=event.timestamp.isoformat(),
            partition=partition,
            offset=offset,
            schema_version=self.config.schema_version,
        )
        return {"event_id": str(event.event_id), "partition": partition, "offset": offset}

    async def healthy(self):
        metadata = await asyncio.to_thread(self.producer.list_topics, self.config.kafka_topic, 5)
        topic = metadata.topics.get(self.config.kafka_topic)
        if (
            not topic
            or topic.error
            or len(topic.partitions) < 3
            or any(p.leader < 0 or len(p.isrs) < 2 for p in topic.partitions.values())
        ):
            raise RuntimeError("Kafka topic is unavailable")

    async def close(self):
        await asyncio.to_thread(self.producer.flush, self.config.publish_timeout_seconds + 1)
        if self._poll_task:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
