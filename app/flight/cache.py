import logging

from google.protobuf.message import DecodeError
from redis.exceptions import RedisError
from redis.sentinel import Sentinel

from app.common.config import positive_int

log = logging.getLogger(__name__)


class FlightCache:
    def __init__(self, client, ttl: int = 300):
        if not 300 <= ttl <= 600:
            raise ValueError("CACHE_TTL_SECONDS must be between 300 and 600")
        self.client = client
        self.ttl = ttl

    @classmethod
    def from_env(cls):
        import os

        nodes = [
            (host, int(port))
            for host, port in (
                item.rsplit(":", 1) for item in os.environ["REDIS_SENTINELS"].split(",")
            )
        ]
        sentinel = Sentinel(nodes, socket_timeout=0.25, socket_connect_timeout=0.25)
        client = sentinel.master_for(
            os.getenv("REDIS_MASTER_NAME", "flights"),
            socket_timeout=0.25,
            socket_connect_timeout=0.25,
        )
        return cls(client, positive_int("CACHE_TTL_SECONDS", 300))

    def get(self, key, message_type):
        try:
            raw = self.client.get(key)
            if raw is not None:
                result = message_type.FromString(raw)
                log.info("cache hit key=%s", key)
                return result
        except (RedisError, DecodeError):
            log.warning("cache unavailable or invalid; reading PostgreSQL", exc_info=True)
        log.info("cache miss key=%s", key)
        return None

    def put(self, key, message):
        try:
            self.client.set(key, message.SerializeToString(), ex=self.ttl)
        except RedisError:
            log.warning("cache write skipped; PostgreSQL remains authoritative", exc_info=True)


def invalidate(conn):
    row = conn.execute(
        "UPDATE cache_revision SET revision = revision + 1 WHERE id = 1 RETURNING revision"
    ).fetchone()
    log.info("cache invalidated revision=%s (transactional namespace)", row["revision"])
