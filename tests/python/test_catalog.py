"""Tests for the durable PostgreSQL cache catalog."""

from __future__ import annotations

import asyncio
import logging

from ytmusic_free.catalog import CacheJob, CachedEntry, PostgresCacheCatalog


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


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class TransactionalFakePool(FakePool):
    """Support the transaction surface used by account snapshots."""

    def acquire(self):
        return _AsyncContext(self)

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return True


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
    pool.claim_rows = [
        {
            "track_id": "first",
            "attempt_count": 3,
            "upgrade_requested": True,
            "bitrate": 128,
            "cache_path": "/cache/first.webm",
        }
    ]
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    jobs = asyncio.run(catalog.claim(10))

    assert jobs == [
        CacheJob(
            "first",
            3,
            quality_upgrade=True,
            cached_bitrate=128,
            cache_path="/cache/first.webm",
        )
    ]
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
    assert "upgrade_requested" in query


def test_list_cached_returns_rows_that_must_have_files():
    pool = FakePool()
    pool.claim_rows = [
        {
            "track_id": "track",
            "cache_path": "/cache/track.webm",
            "bitrate": 138,
        }
    ]
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    rows = asyncio.run(catalog.list_cached())

    assert rows == [CachedEntry("track", "/cache/track.webm", 138)]
    query, args = pool.calls[0]
    assert "status = 'cached' OR upgrade_requested" in query
    assert args == ("ytmusic_free--home",)


def test_missing_cache_files_are_requeued_and_metadata_is_cleared():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(catalog.requeue_missing(["first", "second"]))

    query, args = pool.calls[0]
    assert "status = 'pending'" in query
    assert "cache_path = NULL" in query
    assert "upgrade_requested = false" in query
    assert args == ("ytmusic_free--home", ["first", "second"])


def test_quality_upgrade_scheduler_is_bounded_and_cooled_down():
    pool = FakePool()
    pool.claim_rows = [{"track_id": "first"}]
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    count = asyncio.run(catalog.schedule_quality_upgrades(256, 100, 30))

    assert count == 1
    query, args = pool.calls[0]
    assert "bitrate IS NULL OR bitrate < $2" in query
    assert "quality_checked_at" in query
    assert "upgrade_requested = true" in query
    assert "FOR UPDATE SKIP LOCKED" in query
    assert args == ("ytmusic_free--home", 256, 100, 30)


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


def test_account_snapshot_is_instance_scoped_and_soft_removes_absences():
    pool = TransactionalFakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(
        catalog.replace_account_collection(
            "liked_tracks",
            "",
            [
                {
                    "object_type": "track",
                    "object_id": "video-id",
                    "relation_key": "video-id",
                    "position": 0,
                    "payload": {"videoId": "video-id", "title": "Song"},
                }
            ],
        )
    )

    statements = "\n".join(query for query, _args in pool.calls)
    assert "ytmusic_account_objects" in statements
    assert "ytmusic_account_relations" in statements
    assert "removed_at = now()" in statements
    assert "ytmusic_account_sync_runs" in statements
    assert "ytmusic_free--home" in repr(pool.calls)


def test_prefetch_cooldown_is_durable_and_scoped_to_provider():
    pool = FakePool()
    catalog = PostgresCacheCatalog(pool, "ytmusic_free--home", logging.getLogger())

    asyncio.run(catalog.set_cooldown(21600, "YouTube bot challenge"))

    query, args = pool.calls[0]
    assert "ytmusic_cache_controls" in query
    assert "ON CONFLICT" in query
    assert args == (
        "ytmusic_free--home",
        21600,
        "YouTube bot challenge",
    )
