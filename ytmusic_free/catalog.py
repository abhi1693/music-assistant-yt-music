"""PostgreSQL-backed durable cache catalog and prefetch queue."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CacheJob:
    """One durably claimed cache job."""

    track_id: str
    attempt_count: int


class PostgresCacheCatalog:
    """Coordinate cache state without becoming a playback dependency."""

    def __init__(self, pool: Any, provider_instance_id: str, logger: logging.Logger) -> None:
        self._pool = pool
        self._provider_instance_id = provider_instance_id
        self._logger = logger

    @classmethod
    async def connect(
        cls,
        dsn: str,
        provider_instance_id: str,
        logger: logging.Logger,
    ) -> "PostgresCacheCatalog":
        """Connect with a deliberately small application-side pool."""

        asyncpg = importlib.import_module("asyncpg")
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=2,
            command_timeout=15,
            server_settings={"application_name": "music-assistant-ytmusic-cache"},
        )
        catalog = cls(pool, provider_instance_id, logger)
        await catalog.migrate()
        return catalog

    async def migrate(self) -> None:
        """Create the additive v1 schema."""

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ytmusic_cache_schema (
                        version integer PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ytmusic_cache_entries (
                        provider_instance_id text NOT NULL,
                        track_id text NOT NULL,
                        cache_path text,
                        audio_format text,
                        bitrate integer,
                        size_bytes bigint,
                        status text NOT NULL DEFAULT 'pending'
                            CHECK (status IN (
                                'pending', 'downloading', 'cached', 'retry', 'failed'
                            )),
                        priority integer NOT NULL DEFAULT 100,
                        attempt_count integer NOT NULL DEFAULT 0,
                        next_attempt_at timestamptz NOT NULL DEFAULT now(),
                        lease_until timestamptz,
                        last_error text,
                        cached_at timestamptz,
                        last_accessed_at timestamptz,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (provider_instance_id, track_id)
                    )
                    """
                )
                await connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS ytmusic_cache_entries_claim_idx
                    ON ytmusic_cache_entries (
                        provider_instance_id,
                        status,
                        next_attempt_at,
                        priority,
                        created_at
                    )
                    """
                )
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ytmusic_cache_attempts (
                        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                        provider_instance_id text NOT NULL,
                        track_id text NOT NULL,
                        attempt_number integer NOT NULL,
                        outcome text NOT NULL,
                        error text,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await connection.execute(
                    """
                    INSERT INTO ytmusic_cache_schema (version)
                    VALUES (1)
                    ON CONFLICT (version) DO NOTHING
                    """
                )

    async def enqueue(self, track_ids: list[str], priority: int = 100) -> None:
        """Insert new candidates while preserving existing retry/cache state."""

        if not track_ids:
            return
        await self._pool.executemany(
            """
            INSERT INTO ytmusic_cache_entries (
                provider_instance_id, track_id, priority
            )
            VALUES ($1, $2, $3)
            ON CONFLICT (provider_instance_id, track_id) DO UPDATE
            SET priority = LEAST(ytmusic_cache_entries.priority, EXCLUDED.priority),
                updated_at = now()
            """,
            [(self._provider_instance_id, track_id, priority) for track_id in track_ids],
        )

    async def reconcile_cached(
        self, entries: list[tuple[str, str, int, str]]
    ) -> None:
        """Import completed filesystem entries without changing attempt history."""

        if not entries:
            return
        await self._pool.executemany(
            """
            INSERT INTO ytmusic_cache_entries (
                provider_instance_id, track_id, cache_path, size_bytes,
                audio_format, status, cached_at
            )
            VALUES ($1, $2, $3, $4, $5, 'cached', now())
            ON CONFLICT (provider_instance_id, track_id) DO UPDATE
            SET cache_path = EXCLUDED.cache_path,
                size_bytes = EXCLUDED.size_bytes,
                audio_format = EXCLUDED.audio_format,
                status = 'cached',
                lease_until = NULL,
                last_error = NULL,
                cached_at = COALESCE(ytmusic_cache_entries.cached_at, now()),
                updated_at = now()
            """,
            [
                (self._provider_instance_id, track_id, path, size, audio_format)
                for track_id, path, size, audio_format in entries
            ],
        )

    async def claim(self, limit: int) -> list[CacheJob]:
        """Atomically lease eligible jobs for this provider instance."""

        rows = await self._pool.fetch(
            """
            WITH candidates AS (
                SELECT provider_instance_id, track_id
                FROM ytmusic_cache_entries
                WHERE provider_instance_id = $1
                  AND (
                    (status IN ('pending', 'retry') AND next_attempt_at <= now())
                    OR (status = 'downloading' AND lease_until < now())
                  )
                ORDER BY priority, next_attempt_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT $2
            )
            UPDATE ytmusic_cache_entries AS entry
            SET status = 'downloading',
                attempt_count = entry.attempt_count + 1,
                lease_until = now() + interval '15 minutes',
                updated_at = now()
            FROM candidates
            WHERE entry.provider_instance_id = candidates.provider_instance_id
              AND entry.track_id = candidates.track_id
            RETURNING entry.track_id, entry.attempt_count
            """,
            self._provider_instance_id,
            limit,
        )
        return [CacheJob(str(row["track_id"]), int(row["attempt_count"])) for row in rows]

    async def mark_cached(
        self,
        track_id: str,
        cache_path: str,
        size_bytes: int,
        audio_format: str | None = None,
        bitrate: int | None = None,
    ) -> None:
        """Record a completed atomic cache publication."""

        await self._pool.execute(
            """
            INSERT INTO ytmusic_cache_entries (
                provider_instance_id, track_id, cache_path, audio_format,
                bitrate, size_bytes, status, cached_at, last_accessed_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, 'cached', now(), now())
            ON CONFLICT (provider_instance_id, track_id) DO UPDATE
            SET cache_path = EXCLUDED.cache_path,
                audio_format = EXCLUDED.audio_format,
                bitrate = EXCLUDED.bitrate,
                size_bytes = EXCLUDED.size_bytes,
                status = 'cached',
                lease_until = NULL,
                last_error = NULL,
                cached_at = now(),
                last_accessed_at = now(),
                updated_at = now()
            """,
            self._provider_instance_id,
            track_id,
            cache_path,
            audio_format,
            bitrate,
            size_bytes,
        )
        await self._record_attempt(track_id, "cached", None)

    async def mark_retry(self, track_id: str, error: str, attempt_count: int) -> None:
        """Release a failed job with bounded exponential backoff."""

        delay_seconds = min(6 * 60 * 60, 60 * (2 ** max(attempt_count - 1, 0)))
        await self._pool.execute(
            """
            UPDATE ytmusic_cache_entries
            SET status = CASE WHEN attempt_count >= 10 THEN 'failed' ELSE 'retry' END,
                next_attempt_at = now() + ($3 * interval '1 second'),
                lease_until = NULL,
                last_error = $2,
                updated_at = now()
            WHERE provider_instance_id = $1 AND track_id = $4
            """,
            self._provider_instance_id,
            error,
            delay_seconds,
            track_id,
        )
        await self._record_attempt(track_id, "retry", error)

    async def release(self, track_id: str) -> None:
        """Return a paused or size-limited claim to pending state."""

        await self._pool.execute(
            """
            UPDATE ytmusic_cache_entries
            SET status = 'pending', lease_until = NULL, updated_at = now()
            WHERE provider_instance_id = $1 AND track_id = $2
              AND status = 'downloading'
            """,
            self._provider_instance_id,
            track_id,
        )

    async def _record_attempt(
        self, track_id: str, outcome: str, error: str | None
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO ytmusic_cache_attempts (
                provider_instance_id, track_id, attempt_number, outcome, error
            )
            SELECT provider_instance_id, track_id, attempt_count, $3, $4
            FROM ytmusic_cache_entries
            WHERE provider_instance_id = $1 AND track_id = $2
            """,
            self._provider_instance_id,
            track_id,
            outcome,
            error,
        )

    async def close(self) -> None:
        """Close the application-side pool."""

        await self._pool.close()
