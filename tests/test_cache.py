from unittest.mock import Mock

from redis.exceptions import ConnectionError

from app.flight.cache import FlightCache
from app.generated import flight_pb2 as pb


def test_cache_ttl_hit_miss_and_redis_outage(caplog):
    client = Mock()
    cache = FlightCache(client)
    flight = pb.Flight(id="flight", price_minor=10)
    cache.put("flight:key", flight)
    client.set.assert_called_once_with("flight:key", flight.SerializeToString(), ex=300)
    client.get.return_value = flight.SerializeToString()
    assert cache.get("flight:key", pb.Flight) == flight
    client.get.return_value = None
    assert cache.get("flight:key", pb.Flight) is None
    client.get.side_effect = ConnectionError("redis down")
    client.set.side_effect = ConnectionError("redis down")
    assert cache.get("flight:key", pb.Flight) is None
    cache.put("flight:key", flight)
    assert "PostgreSQL" in caplog.text


def test_corrupt_cache_entry_is_a_miss():
    client = Mock()
    client.get.return_value = b"\x80"
    assert FlightCache(client).get("key", pb.Flight) is None
