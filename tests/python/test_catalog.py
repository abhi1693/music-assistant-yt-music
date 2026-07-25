"""Tests for the durable PostgreSQL cache catalog."""

from __future__ import annotations

import asyncio
import logging

from ytmusic_free.catalog import CacheJob, PostgresCacheCatalog


class FakePool:
    """Record catalog queries without requiring a PostgreSQL server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.claim_rows: list[dict] = []
        self.closed = False

    async def execute(self, query, *args):
        self.calls.append((query, args))

    async def executemany(self, query, args):
        self.calls.append((query, tuple(args)))

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.claim_rows

    async def close(self):
        self.closed = True


def test_enqueue_is_instance_scoped_and_idempotent():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(catalog.enqueue(["first", "second"], priority=25))

    query, rows = pool.calls[0]
    assert "ON CONFLICT" in query
    assert rows == (
        ("ytmusic_free--home", "first", 25),
        ("ytmusic_free--home", "second", 25),
    )


def test_claim_uses_skip_locked_and_returns_attempt_count():
    pool = FakePool()
    pool.claim_rows = [{"track_id": "first", "attempt_count": 3}]
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    jobs = asyncio.run(catalog.claim(10))

    assert jobs == [CacheJob("first", 3)]
    query, args = pool.calls[0]
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "lease_until" in query
    assert args == ("ytmusic_free--home", 10)


def test_reconcile_imports_completed_files_as_cached():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(
        catalog.reconcile_cached(
            [("track", "/cache/track.webm", 1234, "webm")]
        )
    )

    query, rows = pool.calls[0]
    assert "'cached'" in query
    assert rows == (
        ("ytmusic_free--home", "track", "/cache/track.webm", 1234, "webm"),
    )


def test_retry_records_sanitized_error_and_bounded_delay():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(catalog.mark_retry("track", "ClientResponseError HTTP 403", 20))

    update_query, update_args = pool.calls[0]
    assert "attempt_count >= 10" in update_query
    assert update_args == (
        "ytmusic_free--home",
        "ClientResponseError HTTP 403",
        21600,
        "track",
    )
    assert len(pool.calls) == 2


def test_close_closes_pool():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(catalog.close())

    assert pool.closed is True
