"""Shared bounded local-first Slack thread transcript.

One instance is shared by every local SlackAgent in a process. Raw Slack
credentials are never accepted or persisted; only the message fields required
by sender classification, context formatting, and feed flattening are retained.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from state_store import StateStore

logger = logging.getLogger("transcript_store")

TRANSCRIPT_MAX_THREADS = 512
TRANSCRIPT_MAX_MESSAGES_PER_THREAD = 50
TRANSCRIPT_DB_MAX_THREADS = 2048
TRANSCRIPT_MAX_RECORD_BYTES = 64 * 1024
TRANSCRIPT_MEMORY_MAX_BYTES = 32 * 1024 * 1024
TRANSCRIPT_DB_MAX_BYTES = 256 * 1024 * 1024
TRANSCRIPT_MIN_RECORD_BYTES = 256
TRANSCRIPT_HARD_MAX_RECORD_BYTES = 1024 * 1024
TRANSCRIPT_HARD_MAX_MEMORY_BYTES = 512 * 1024 * 1024
TRANSCRIPT_HARD_MAX_DB_BYTES = 4 * 1024 * 1024 * 1024
TRANSCRIPT_TTL_SECONDS = 7 * 24 * 3600
TRANSCRIPT_RETRY_COOLDOWN_SECONDS = 60.0
TRANSCRIPT_SWEEP_INTERVAL_SECONDS = 600.0
SLACK_TRANSCRIPT_PAGE_LIMIT = 15

_BACKFILL_SOURCE_RANK = 1
_LIVE_SOURCE_RANK = 2
_DELETE_SOURCE_RANK = 3

TranscriptKey = tuple[str, str, str]


def _timestamp_key(value: str) -> tuple[int, Decimal | str]:
    text = str(value or "")
    try:
        return (0, Decimal(text))
    except (InvalidOperation, ValueError):
        return (1, text)


def _later_timestamp(first: str, second: str) -> str:
    return first if _timestamp_key(first) >= _timestamp_key(second) else second


@dataclass
class TranscriptSnapshot:
    """Reader-facing thread state; messages contain no internal metadata."""

    messages: list[dict[str, Any]]
    complete: bool
    persisted_complete: bool
    needs_revalidate: bool
    from_root: bool
    truncated_before_ts: str
    authoritative: bool
    verified_through_ts: str
    authority_truncated: bool


@dataclass
class _ThreadState:
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    complete: bool = False
    persisted_complete: bool = False
    needs_revalidate: bool = False
    from_root: bool = False
    truncated_before_ts: str = ""
    last_message_ts: str = ""
    hydrated_at: float = 0.0
    retry_after: float = 0.0
    failure_count: int = 0
    touched_at: float = 0.0
    loaded_from_db: bool = False
    live_generation: int = 0
    validated_boot_id: str = ""
    authoritative: bool = False
    verified_through_ts: str = ""
    authority_truncated: bool = False
    persistence_blocked: bool = False


@dataclass
class _HydrationState:
    task: asyncio.Task[None] | None = None
    waiters: int = 0


class TranscriptStore:
    """Bounded process-shared transcript with optional SQLite persistence."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        *,
        max_threads: int = TRANSCRIPT_MAX_THREADS,
        max_messages_per_thread: int = TRANSCRIPT_MAX_MESSAGES_PER_THREAD,
        db_max_threads: int = TRANSCRIPT_DB_MAX_THREADS,
        max_record_bytes: int = TRANSCRIPT_MAX_RECORD_BYTES,
        max_memory_bytes: int = TRANSCRIPT_MEMORY_MAX_BYTES,
        max_db_bytes: int = TRANSCRIPT_DB_MAX_BYTES,
        ttl_seconds: float = TRANSCRIPT_TTL_SECONDS,
        retry_cooldown_seconds: float = TRANSCRIPT_RETRY_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        boot_id: str | None = None,
    ) -> None:
        self._state_store = state_store
        self.max_threads = max(1, int(max_threads))
        self.max_messages_per_thread = max(
            1, int(max_messages_per_thread)
        )
        self.db_max_threads = max(1, int(db_max_threads))
        self.max_record_bytes = self._bounded_capacity(
            "max_record_bytes",
            max_record_bytes,
            TRANSCRIPT_HARD_MAX_RECORD_BYTES,
            minimum=TRANSCRIPT_MIN_RECORD_BYTES,
        )
        self.max_memory_bytes = self._bounded_capacity(
            "max_memory_bytes",
            max_memory_bytes,
            TRANSCRIPT_HARD_MAX_MEMORY_BYTES,
        )
        self.max_db_bytes = self._bounded_capacity(
            "max_db_bytes",
            max_db_bytes,
            TRANSCRIPT_HARD_MAX_DB_BYTES,
        )
        if self.max_memory_bytes < self.max_record_bytes:
            raise ValueError(
                "max_memory_bytes must be greater than or equal to "
                "max_record_bytes"
            )
        if self.max_db_bytes < self.max_record_bytes:
            raise ValueError(
                "max_db_bytes must be greater than or equal to "
                "max_record_bytes"
            )
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.retry_cooldown_seconds = max(
            1.0, float(retry_cooldown_seconds)
        )
        self._clock = clock
        self.boot_id = str(boot_id or uuid.uuid4().hex)
        self._threads: OrderedDict[TranscriptKey, _ThreadState] = (
            OrderedDict()
        )
        self._hydrations: dict[TranscriptKey, _HydrationState] = {}
        self._last_sweep = 0.0
        self._counters = {
            "reads": 0,
            "warm_reads": 0,
            "partial_reads": 0,
            "backfill_attempts": 0,
            "backfill_calls": 0,
            "backfill_failures": 0,
            "singleflight_waits": 0,
            "evicted_threads": 0,
            "byte_evicted_threads": 0,
            "ingested": 0,
            "duplicates": 0,
        }
        if self._state_store is not None:
            self._state_store.sweep_transcripts(
                self.ttl_seconds,
                max_threads=self.db_max_threads,
                max_bytes=self.max_db_bytes,
            )

    @staticmethod
    def _bounded_capacity(
        name: str,
        value: int,
        hard_max: int,
        *,
        minimum: int = 1,
    ) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive integer") from exc
        if parsed < minimum:
            raise ValueError(
                f"{name} must be an integer greater than or equal to "
                f"{minimum}"
            )
        if parsed > hard_max:
            raise ValueError(f"{name} exceeds hard maximum {hard_max}")
        return parsed

    @classmethod
    def from_environment(
        cls,
        state_store: StateStore | None,
        environ: Mapping[str, str],
    ) -> "TranscriptStore":
        """Build a store from positive, non-secret capacity settings."""

        def positive_int(
            name: str, default: int, hard_max: int | None = None
        ) -> int:
            raw = environ.get(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                value = int(str(raw).strip())
            except ValueError as exc:
                raise ValueError(
                    f"{name} must be a positive integer"
                ) from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            if hard_max is not None and value > hard_max:
                raise ValueError(
                    f"{name} exceeds hard maximum {hard_max}"
                )
            return value

        def positive_float(name: str, default: float) -> float:
            raw = environ.get(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                value = float(str(raw).strip())
            except ValueError as exc:
                raise ValueError(
                    f"{name} must be a positive number"
                ) from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive number")
            return value

        max_record_bytes = positive_int(
            "TRANSCRIPT_MAX_RECORD_BYTES",
            TRANSCRIPT_MAX_RECORD_BYTES,
            TRANSCRIPT_HARD_MAX_RECORD_BYTES,
        )
        if max_record_bytes < TRANSCRIPT_MIN_RECORD_BYTES:
            raise ValueError(
                "TRANSCRIPT_MAX_RECORD_BYTES must be greater than or "
                f"equal to {TRANSCRIPT_MIN_RECORD_BYTES}"
            )
        max_memory_bytes = positive_int(
            "TRANSCRIPT_MEMORY_MAX_BYTES",
            TRANSCRIPT_MEMORY_MAX_BYTES,
            TRANSCRIPT_HARD_MAX_MEMORY_BYTES,
        )
        max_db_bytes = positive_int(
            "TRANSCRIPT_DB_MAX_BYTES",
            TRANSCRIPT_DB_MAX_BYTES,
            TRANSCRIPT_HARD_MAX_DB_BYTES,
        )
        if max_memory_bytes < max_record_bytes:
            raise ValueError(
                "TRANSCRIPT_MEMORY_MAX_BYTES must be greater than or "
                "equal to TRANSCRIPT_MAX_RECORD_BYTES"
            )
        if max_db_bytes < max_record_bytes:
            raise ValueError(
                "TRANSCRIPT_DB_MAX_BYTES must be greater than or equal "
                "to TRANSCRIPT_MAX_RECORD_BYTES"
            )
        return cls(
            state_store,
            max_threads=positive_int(
                "TRANSCRIPT_MAX_THREADS", TRANSCRIPT_MAX_THREADS
            ),
            max_messages_per_thread=positive_int(
                "TRANSCRIPT_MAX_MESSAGES_PER_THREAD",
                TRANSCRIPT_MAX_MESSAGES_PER_THREAD,
            ),
            db_max_threads=positive_int(
                "TRANSCRIPT_DB_MAX_THREADS",
                TRANSCRIPT_DB_MAX_THREADS,
            ),
            max_record_bytes=max_record_bytes,
            max_memory_bytes=max_memory_bytes,
            max_db_bytes=max_db_bytes,
            ttl_seconds=positive_float(
                "TRANSCRIPT_TTL_SECONDS", TRANSCRIPT_TTL_SECONDS
            ),
            retry_cooldown_seconds=positive_float(
                "TRANSCRIPT_RETRY_COOLDOWN_SECONDS",
                TRANSCRIPT_RETRY_COOLDOWN_SECONDS,
            ),
        )

    def _key(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> TranscriptKey:
        return (str(team_id), str(channel_id), str(thread_ts))

    def _meta(self, state: _ThreadState) -> dict[str, Any]:
        persist_authority = not state.persistence_blocked
        return {
            "complete": (
                state.persisted_complete and persist_authority
            ),
            "from_root": state.from_root and persist_authority,
            "truncated_before_ts": state.truncated_before_ts,
            "last_message_ts": state.last_message_ts,
            "hydrated_at": state.hydrated_at,
            "retry_after": 0,
            "failure_count": state.failure_count,
            "validated_boot_id": (
                state.validated_boot_id if persist_authority else ""
            ),
            "authoritative": (
                state.authoritative if persist_authority else False
            ),
            "verified_through_ts": (
                state.verified_through_ts if persist_authority else ""
            ),
            "authority_truncated": state.authority_truncated,
        }

    def _load_thread(self, key: TranscriptKey) -> _ThreadState:
        existing = self._threads.get(key)
        if existing is not None:
            existing.touched_at = self._clock()
            self._threads.move_to_end(key)
            return existing

        state = _ThreadState(touched_at=self._clock())
        if self._state_store is not None:
            persisted = self._state_store.load_transcript_thread(
                *key,
                limit=self.max_messages_per_thread,
                max_record_bytes=self.max_record_bytes,
            )
            if persisted is not None:
                state.loaded_from_db = True
                state.persisted_complete = bool(
                    persisted.get("complete")
                )
                persisted_boot_id = str(
                    persisted.get("validated_boot_id") or ""
                )
                same_boot = bool(
                    persisted_boot_id
                    and persisted_boot_id == self.boot_id
                )
                state.complete = (
                    state.persisted_complete if same_boot else False
                )
                state.needs_revalidate = (
                    False if same_boot else state.persisted_complete
                )
                state.from_root = bool(
                    persisted.get("from_root")
                ) if same_boot else False
                state.validated_boot_id = (
                    persisted_boot_id if same_boot else ""
                )
                state.authoritative = bool(
                    persisted.get("authoritative")
                ) if same_boot else False
                state.verified_through_ts = (
                    str(persisted.get("verified_through_ts") or "")
                    if same_boot
                    else ""
                )
                state.authority_truncated = bool(
                    persisted.get("authority_truncated")
                ) if same_boot else False
                state.truncated_before_ts = str(
                    persisted.get("truncated_before_ts") or ""
                )
                state.last_message_ts = str(
                    persisted.get("last_message_ts") or ""
                )
                state.hydrated_at = float(
                    persisted.get("hydrated_at") or 0
                )
                state.failure_count = int(
                    persisted.get("failure_count") or 0
                )
                repaired_records: list[dict[str, Any]] = []
                repair_epoch = (
                    self._state_store.transcript_eviction_epoch(*key)
                )
                for stored_record in persisted.get("messages") or []:
                    raw_record = dict(stored_record)
                    storage_needs_rewrite = bool(
                        raw_record.pop(
                            "_storage_needs_rewrite", False
                        )
                    )
                    bounded_record = self._bound_record(raw_record)
                    needs_rewrite = storage_needs_rewrite or any(
                        raw_record.get(field_name)
                        != bounded_record.get(field_name)
                        for field_name in (
                            "ts",
                            "user",
                            "bot_id",
                            "subtype",
                            "text",
                            "blocks",
                            "attachments",
                            "edited_ts",
                            "revision_ts",
                            "tombstone",
                            "source_rank",
                            "payload_bytes",
                            "content_truncated",
                        )
                    )
                    message_ts = str(
                        bounded_record.get("ts") or ""
                    )
                    if message_ts:
                        state.records[message_ts] = bounded_record
                        if needs_rewrite:
                            repaired_records.append(bounded_record)
                        state.last_message_ts = (
                            message_ts
                            if not state.last_message_ts
                            else _later_timestamp(
                                message_ts, state.last_message_ts
                            )
                        )
                self._refresh_authority_truncation(state)
                for repaired in repaired_records:
                    self._state_store.rewrite_transcript_record(
                        *key,
                        record=repaired,
                        max_threads=self.db_max_threads,
                        max_bytes=self.max_db_bytes,
                    )
                if repaired_records:
                    if (
                        self._state_store.transcript_eviction_epoch(
                            *key
                        )
                        != repair_epoch
                    ):
                        state.persistence_blocked = True
                        state.persisted_complete = False
                    else:
                        self._persist_meta(key, state)

        self._threads[key] = state
        self._threads.move_to_end(key)
        self._evict_memory()
        return state

    @staticmethod
    def _record_bytes(record: Mapping[str, Any]) -> int:
        """Return the deterministic logical size persisted for one record."""
        payload = {
            "ts": str(record.get("ts") or ""),
            "user": str(record.get("user") or ""),
            "bot_id": str(record.get("bot_id") or ""),
            "subtype": str(record.get("subtype") or ""),
            "text": str(record.get("text") or ""),
            "blocks": (
                record.get("blocks")
                if isinstance(record.get("blocks"), list)
                else []
            ),
            "attachments": (
                record.get("attachments")
                if isinstance(record.get("attachments"), list)
                else []
            ),
            "edited_ts": str(record.get("edited_ts") or ""),
            "revision_ts": str(record.get("revision_ts") or ""),
            "tombstone": bool(record.get("tombstone")),
            "source_rank": int(record.get("source_rank") or 0),
            "content_truncated": bool(
                record.get("content_truncated")
            ),
        }
        return len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )

    @staticmethod
    def _truncate_utf8(value: str, byte_limit: int) -> str:
        if byte_limit <= 0:
            return ""
        encoded = value.encode("utf-8")
        if len(encoded) <= byte_limit:
            return value
        return encoded[:byte_limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _bounded_structured_list(
        value: Any, byte_budget: int
    ) -> tuple[list[Any], bool]:
        """Reject cyclic/deep/high-node JSON before serialization."""
        if not isinstance(value, list):
            return [], bool(value)
        stack: list[tuple[Any, int]] = [(value, 0)]
        seen_containers: set[int] = set()
        nodes = 0
        approximate_bytes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > 1024 or depth > 16:
                return [], True
            if isinstance(item, (list, dict)):
                identity = id(item)
                if identity in seen_containers:
                    return [], True
                seen_containers.add(identity)
            if isinstance(item, list):
                if len(item) > 256:
                    return [], True
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, dict):
                if len(item) > 128:
                    return [], True
                for key, child in item.items():
                    if not isinstance(key, str):
                        return [], True
                    stack.append((key, depth + 1))
                    stack.append((child, depth + 1))
            elif isinstance(item, str):
                if len(item) > byte_budget:
                    return [], True
                approximate_bytes += len(item.encode("utf-8"))
            elif isinstance(item, int):
                if item.bit_length() > 128:
                    return [], True
                approximate_bytes += 24
            elif isinstance(item, (float, bool)) or item is None:
                approximate_bytes += 24
            else:
                return [], True
            if approximate_bytes > byte_budget:
                return [], True
        return list(value), False

    def _bound_record(
        self, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Normalize one untrusted Slack record to a strict byte budget."""
        raw_ts = str(record.get("ts") or "")
        raw_user = str(record.get("user") or "")
        raw_bot_id = str(record.get("bot_id") or "")
        raw_subtype = str(record.get("subtype") or "")
        raw_edited_ts = str(record.get("edited_ts") or "")
        raw_revision_ts = str(record.get("revision_ts") or "")
        bounded_ts = self._truncate_utf8(raw_ts, 64)
        bounded_user = self._truncate_utf8(raw_user, 128)
        bounded_bot_id = self._truncate_utf8(raw_bot_id, 128)
        bounded_subtype = self._truncate_utf8(raw_subtype, 64)
        bounded_edited_ts = self._truncate_utf8(raw_edited_ts, 64)
        bounded_revision_ts = self._truncate_utf8(
            raw_revision_ts, 64
        )
        blocks, blocks_truncated = self._bounded_structured_list(
            record.get("blocks"), self.max_record_bytes
        )
        attachments, attachments_truncated = (
            self._bounded_structured_list(
                record.get("attachments"), self.max_record_bytes
            )
        )
        scalar_truncated = any(
            bounded != raw
            for bounded, raw in (
                (bounded_ts, raw_ts),
                (bounded_user, raw_user),
                (bounded_bot_id, raw_bot_id),
                (bounded_subtype, raw_subtype),
                (bounded_edited_ts, raw_edited_ts),
                (bounded_revision_ts, raw_revision_ts),
            )
        ) or blocks_truncated or attachments_truncated
        normalized: dict[str, Any] = {
            "ts": bounded_ts,
            "user": bounded_user,
            "bot_id": bounded_bot_id,
            "subtype": bounded_subtype,
            "text": str(record.get("text") or ""),
            "blocks": blocks,
            "attachments": attachments,
            "edited_ts": bounded_edited_ts,
            "revision_ts": bounded_revision_ts,
            "tombstone": bool(record.get("tombstone")),
            "source_rank": int(record.get("source_rank") or 0),
            "content_truncated": bool(
                record.get("content_truncated")
            )
            or scalar_truncated,
        }
        try:
            payload_bytes = self._record_bytes(normalized)
        except (TypeError, ValueError, OverflowError, RecursionError):
            normalized["blocks"] = []
            normalized["attachments"] = []
            normalized["content_truncated"] = True
            payload_bytes = self._record_bytes(normalized)

        if payload_bytes > self.max_record_bytes:
            normalized["blocks"] = []
            normalized["attachments"] = []
            normalized["content_truncated"] = True
            original_text = str(normalized["text"])
            normalized["text"] = ""
            baseline = self._record_bytes(normalized)
            available = max(0, self.max_record_bytes - baseline)
            normalized["text"] = self._truncate_utf8(
                original_text, available
            )
            # JSON quoting can consume extra bytes for control characters.
            while (
                normalized["text"]
                and self._record_bytes(normalized)
                > self.max_record_bytes
            ):
                encoded = normalized["text"].encode("utf-8")
                overflow = (
                    self._record_bytes(normalized)
                    - self.max_record_bytes
                )
                normalized["text"] = self._truncate_utf8(
                    normalized["text"],
                    max(0, len(encoded) - max(1, overflow)),
                )
            payload_bytes = self._record_bytes(normalized)

        # Slack identity fields are normally tiny. A pathologically small
        # configured record budget may not fit even the fixed metadata; fail
        # closed by marking it truncated while retaining revision identity.
        normalized["payload_bytes"] = payload_bytes
        return normalized

    @staticmethod
    def _state_bytes(state: _ThreadState) -> int:
        return sum(
            max(0, int(record.get("payload_bytes") or 0))
            for record in state.records.values()
        )

    def _memory_bytes(self) -> int:
        return sum(
            self._state_bytes(state)
            for state in self._threads.values()
        )

    def _evict_memory(self) -> None:
        while (
            len(self._threads) > self.max_threads
            or self._memory_bytes() > self.max_memory_bytes
        ):
            over_bytes = self._memory_bytes() > self.max_memory_bytes
            victim = next(
                (
                    key
                    for key in self._threads
                    if not (
                        key in self._hydrations
                        and self._hydrations[key].task is not None
                        and not self._hydrations[key].task.done()
                    )
                ),
                None,
            )
            if victim is None:
                # Active hydrations are short-lived and are evicted when the
                # last waiter exits; never split one singleflight state.
                return
            self._threads.pop(victim, None)
            self._counters["evicted_threads"] += 1
            if over_bytes:
                self._counters["byte_evicted_threads"] += 1

    def _sweep_if_due(self) -> None:
        now = self._clock()
        if now - self._last_sweep < TRANSCRIPT_SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        stale = [
            key
            for key, state in self._threads.items()
            if now - state.touched_at > self.ttl_seconds
            and not (
                key in self._hydrations
                and self._hydrations[key].task is not None
                and not self._hydrations[key].task.done()
            )
        ]
        for key in stale:
            self._threads.pop(key, None)
            self._counters["evicted_threads"] += 1
        if self._state_store is not None:
            self._state_store.sweep_transcripts(
                self.ttl_seconds,
                max_threads=self.db_max_threads,
                max_bytes=self.max_db_bytes,
            )

    @staticmethod
    def _record_from_message(
        message: dict[str, Any],
        *,
        revision_ts: str,
        source_rank: int,
        tombstone: bool = False,
    ) -> dict[str, Any] | None:
        message_ts = str(message.get("ts") or "")
        if not message_ts:
            return None
        edited = message.get("edited")
        edited_ts = (
            str(edited.get("ts") or "")
            if isinstance(edited, dict)
            else ""
        )
        effective_revision = (
            revision_ts or edited_ts or message_ts
        )
        subtype = message.get("subtype")
        return {
            "ts": message_ts,
            "user": str(message.get("user") or ""),
            "bot_id": str(message.get("bot_id") or ""),
            "subtype": str(subtype or ""),
            "text": "" if tombstone else str(message.get("text") or ""),
            "blocks": (
                list(message.get("blocks") or [])
                if isinstance(message.get("blocks"), list)
                else []
            ),
            "attachments": (
                list(message.get("attachments") or [])
                if isinstance(message.get("attachments"), list)
                else []
            ),
            "edited_ts": edited_ts,
            "revision_ts": effective_revision,
            "tombstone": bool(tombstone),
            "source_rank": int(source_rank),
        }

    def _normalize_live_event(
        self, team_id: str, event: dict[str, Any]
    ) -> tuple[TranscriptKey, dict[str, Any], bool] | None:
        channel = str(event.get("channel") or "")
        if not team_id or not channel:
            return None
        subtype = event.get("subtype")
        event_ts = str(event.get("event_ts") or "")
        normal_root_observed = False
        if subtype == "message_changed":
            inner = event.get("message")
            if not isinstance(inner, dict):
                return None
            message = dict(inner)
            previous = event.get("previous_message")
            if isinstance(previous, dict):
                # Slack normally sends a complete inner message, but preserve
                # stable thread/sender/classification fields when an envelope
                # is sparse. The edited body remains authoritative.
                for field_name in (
                    "ts",
                    "thread_ts",
                    "user",
                    "bot_id",
                    "subtype",
                    "blocks",
                    "attachments",
                ):
                    if (
                        field_name not in message
                        and field_name in previous
                    ):
                        message[field_name] = previous[field_name]
            message["channel"] = channel
            edited = message.get("edited")
            revision_ts = (
                str(edited.get("ts") or "")
                if isinstance(edited, dict)
                else ""
            ) or event_ts
            record = self._record_from_message(
                message,
                revision_ts=revision_ts,
                source_rank=_LIVE_SOURCE_RANK,
            )
        elif subtype == "message_deleted":
            previous = event.get("previous_message")
            message = dict(previous) if isinstance(previous, dict) else {}
            message["channel"] = channel
            message["ts"] = str(
                event.get("deleted_ts") or message.get("ts") or ""
            )
            record = self._record_from_message(
                message,
                revision_ts=event_ts or message.get("ts", ""),
                source_rank=_DELETE_SOURCE_RANK,
                tombstone=True,
            )
        else:
            message = dict(event)
            record = self._record_from_message(
                message,
                revision_ts=event_ts or str(message.get("ts") or ""),
                source_rank=_LIVE_SOURCE_RANK,
            )
            if record is not None:
                normal_root_observed = not message.get("thread_ts")
        if record is None:
            return None
        message_ts = str(record["ts"])
        thread_ts = str(message.get("thread_ts") or message_ts)
        return (
            self._key(team_id, channel, thread_ts),
            record,
            normal_root_observed and message_ts == thread_ts,
        )

    @staticmethod
    def _should_replace(
        current: dict[str, Any] | None,
        candidate: dict[str, Any],
    ) -> bool:
        if current is None:
            return True
        current_revision = _timestamp_key(
            str(current.get("revision_ts") or current.get("ts") or "")
        )
        candidate_revision = _timestamp_key(
            str(
                candidate.get("revision_ts")
                or candidate.get("ts")
                or ""
            )
        )
        if candidate_revision != current_revision:
            return candidate_revision > current_revision
        current_rank = int(current.get("source_rank") or 0)
        candidate_rank = int(candidate.get("source_rank") or 0)
        if candidate_rank != current_rank:
            return candidate_rank > current_rank
        # Same revision and source authority is an idempotent redelivery.
        # Keeping the existing value avoids arrival-order-dependent content.
        return False

    @staticmethod
    def _record_fingerprint(record: dict[str, Any]) -> tuple[Any, ...]:
        """Per-message compare-and-swap identity for one hydration run."""
        return (
            str(record.get("revision_ts") or ""),
            int(record.get("source_rank") or 0),
            bool(record.get("tombstone")),
            str(record.get("user") or ""),
            str(record.get("bot_id") or ""),
            str(record.get("subtype") or ""),
            str(record.get("text") or ""),
            str(record.get("edited_ts") or ""),
            repr(record.get("blocks") or []),
            repr(record.get("attachments") or []),
            bool(record.get("content_truncated")),
        )

    @staticmethod
    def _potentially_canonical(record: Mapping[str, Any]) -> bool:
        return not bool(record.get("tombstone"))

    def _refresh_authority_truncation(
        self, state: _ThreadState
    ) -> None:
        if not state.verified_through_ts:
            state.authority_truncated = False
            return
        state.authority_truncated = any(
            bool(record.get("content_truncated"))
            and self._potentially_canonical(record)
            and _timestamp_key(str(record.get("ts") or ""))
            <= _timestamp_key(state.verified_through_ts)
            for record in state.records.values()
        )
        if state.authority_truncated:
            state.authoritative = False
        elif state.complete and state.from_root:
            state.authoritative = True

    def _trim_memory(self, state: _ThreadState) -> None:
        extra = len(state.records) - self.max_messages_per_thread
        if extra <= 0:
            return
        ordered = sorted(state.records, key=_timestamp_key)
        removed = ordered[:extra]
        for message_ts in removed:
            state.records.pop(message_ts, None)
        boundary = removed[-1]
        state.truncated_before_ts = (
            boundary
            if not state.truncated_before_ts
            else _later_timestamp(
                boundary, state.truncated_before_ts
            )
        )

    def _persist_record(
        self,
        key: TranscriptKey,
        state: _ThreadState,
        record: dict[str, Any],
    ) -> None:
        if self._state_store is None:
            return
        boundary = self._state_store.save_transcript_record(
            *key,
            record=record,
            meta=self._meta(state),
            max_messages=self.max_messages_per_thread,
            max_threads=self.db_max_threads,
            max_bytes=self.max_db_bytes,
        )
        if boundary:
            state.truncated_before_ts = (
                boundary
                if not state.truncated_before_ts
                else _later_timestamp(
                    boundary, state.truncated_before_ts
                )
            )

    def _persist_meta(
        self, key: TranscriptKey, state: _ThreadState
    ) -> None:
        if self._state_store is not None:
            self._state_store.save_transcript_meta(
                *key,
                meta=self._meta(state),
                max_threads=self.db_max_threads,
                max_bytes=self.max_db_bytes,
            )

    def _upsert_record(
        self,
        key: TranscriptKey,
        record: dict[str, Any],
        *,
        root_observed: bool,
        live: bool,
        force: bool = False,
    ) -> bool:
        record = self._bound_record(record)
        state = self._load_thread(key)
        message_ts = str(record["ts"])
        current = state.records.get(message_ts)
        changed = force or self._should_replace(current, record)
        if changed:
            candidate = dict(record)
            if live:
                state.live_generation += 1
                candidate["_live_generation"] = state.live_generation
            else:
                candidate["_live_generation"] = int(
                    (current or {}).get("_live_generation") or 0
                )
            state.records[message_ts] = candidate
            state.last_message_ts = (
                message_ts
                if not state.last_message_ts
                else _later_timestamp(
                    message_ts, state.last_message_ts
                )
            )
        if (
            live
            and root_observed
            and not state.loaded_from_db
            and len(state.records) <= 1
        ):
            # Seeing a newly-created root before any reply means this process
            # can account for the thread from its beginning.
            state.from_root = True
            state.complete = True
            state.persisted_complete = True
            state.needs_revalidate = False
            state.validated_boot_id = self.boot_id
            state.authoritative = False
            state.verified_through_ts = ""
            state.authority_truncated = False
        state.touched_at = self._clock()
        self._trim_memory(state)
        if changed:
            self._refresh_authority_truncation(state)
        if changed:
            self._persist_record(
                key, state, state.records[message_ts]
            )
        self._evict_memory()
        return changed

    def ingest_event(
        self,
        team_id: str,
        event: dict[str, Any],
        *,
        event_id: str = "",
    ) -> bool:
        """Incrementally ingest one Slack message event before any routing."""
        del event_id  # stable message identity/revision is the dedup authority
        self._sweep_if_due()
        normalized = self._normalize_live_event(str(team_id), event)
        if normalized is None:
            return False
        key, record, root_observed = normalized
        changed = self._upsert_record(
            key,
            record,
            root_observed=root_observed,
            live=True,
        )
        if changed:
            self._counters["ingested"] += 1
        else:
            self._counters["duplicates"] += 1
        return changed

    @staticmethod
    def _public_message(
        thread_ts: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "ts": str(record.get("ts") or ""),
            "thread_ts": thread_ts,
            "text": str(record.get("text") or ""),
        }
        for key in ("user", "bot_id", "subtype"):
            value = str(record.get(key) or "")
            if value:
                message[key] = value
        blocks = record.get("blocks")
        if isinstance(blocks, list) and blocks:
            message["blocks"] = list(blocks)
        attachments = record.get("attachments")
        if isinstance(attachments, list) and attachments:
            message["attachments"] = list(attachments)
        edited_ts = str(record.get("edited_ts") or "")
        if edited_ts:
            message["edited"] = {"ts": edited_ts}
        if bool(record.get("content_truncated")):
            message["content_truncated"] = True
        return message

    def _snapshot(
        self, key: TranscriptKey, state: _ThreadState
    ) -> TranscriptSnapshot:
        ordered = sorted(state.records.values(), key=lambda item: _timestamp_key(
            str(item.get("ts") or "")
        ))
        return TranscriptSnapshot(
            messages=[
                self._public_message(key[2], record)
                for record in ordered
                if not bool(record.get("tombstone"))
            ],
            complete=state.complete and not state.needs_revalidate,
            persisted_complete=state.persisted_complete,
            needs_revalidate=state.needs_revalidate,
            from_root=state.from_root,
            truncated_before_ts=state.truncated_before_ts,
            authoritative=(
                state.authoritative
                and state.complete
                and not state.needs_revalidate
                and not state.authority_truncated
            ),
            verified_through_ts=state.verified_through_ts,
            authority_truncated=state.authority_truncated,
        )

    def read_thread(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> TranscriptSnapshot:
        """Return ordered local messages without applying caller permissions."""
        self._counters["reads"] += 1
        key = self._key(team_id, channel_id, thread_ts)
        state = self._load_thread(key)
        return self._snapshot(key, state)

    @staticmethod
    def _retry_after(exc: BaseException, fallback: float) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        value: Any = None
        if headers is not None:
            try:
                value = headers.get("Retry-After")
            except (AttributeError, TypeError):
                value = None
        try:
            return max(fallback, float(value))
        except (TypeError, ValueError):
            return fallback

    def _ingest_backfill_message(
        self,
        key: TranscriptKey,
        message: dict[str, Any],
        *,
        state: _ThreadState,
        start_fingerprints: dict[str, tuple[Any, ...]],
        start_live_generation: int,
    ) -> tuple[str | None, bool]:
        edited = message.get("edited")
        edited_ts = (
            str(edited.get("ts") or "")
            if isinstance(edited, dict)
            else ""
        )
        record = self._record_from_message(
            message,
            revision_ts=edited_ts or str(message.get("ts") or ""),
            source_rank=_BACKFILL_SOURCE_RANK,
        )
        if record is None:
            return None, False
        record = self._bound_record(record)
        record_truncated = bool(record.get("content_truncated"))
        message_ts = str(record["ts"])
        current = state.records.get(message_ts)
        if current is not None:
            start_fingerprint = start_fingerprints.get(message_ts)
            if start_fingerprint is not None:
                if self._record_fingerprint(current) != start_fingerprint:
                    # This exact start key changed after hydration began.
                    return message_ts, record_truncated
            elif int(current.get("_live_generation") or 0) > (
                start_live_generation
            ):
                # A live message created after the start snapshot wins.
                return message_ts, record_truncated
        self._upsert_record(
            key,
            record,
            root_observed=False,
            live=False,
        )
        return message_ts, record_truncated

    async def _backfill(
        self,
        client: Any,
        key: TranscriptKey,
        state: _ThreadState,
    ) -> None:
        self._counters["backfill_attempts"] += 1
        cursor = ""
        seen_cursors: set[str] = set()
        persistence_epoch = (
            self._state_store.transcript_eviction_epoch(*key)
            if self._state_store is not None
            else 0
        )
        state.persistence_blocked = False
        # Invalidate any previous-process marker before the first remote await.
        # A crash or partial-page commit can therefore never leave a stale
        # "complete" marker behind.
        state.complete = False
        state.persisted_complete = False
        state.needs_revalidate = False
        state.from_root = False
        state.validated_boot_id = ""
        state.authoritative = False
        state.verified_through_ts = ""
        state.authority_truncated = False
        self._persist_meta(key, state)
        start_live_generation = state.live_generation
        start_fingerprints = {
            message_ts: self._record_fingerprint(record)
            for message_ts, record in state.records.items()
        }
        remote_seen: set[str] = set()
        remote_authority_truncated = False
        root_seen = False
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "channel": key[1],
                    "ts": key[2],
                    "limit": SLACK_TRANSCRIPT_PAGE_LIMIT,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                self._counters["backfill_calls"] += 1
                response = await client.conversations_replies(**kwargs)
                if bool(response.get("is_limited")):
                    raise RuntimeError(
                        "Slack thread history coverage is limited"
                    )
                batch = response.get("messages")
                if not isinstance(batch, list):
                    raise RuntimeError(
                        "Slack thread history omitted messages"
                    )
                for message in batch:
                    if isinstance(message, dict):
                        if bool(message.get("is_limited")):
                            raise RuntimeError(
                                "Slack thread message coverage is limited"
                            )
                        message_ts = str(message.get("ts") or "")
                        declared_thread_ts = str(
                            message.get("thread_ts") or ""
                        )
                        if (
                            not message_ts
                            or (
                                declared_thread_ts
                                and declared_thread_ts != key[2]
                            )
                        ):
                            raise RuntimeError(
                                "Slack thread history mixed thread identity"
                            )
                        remote_seen.add(message_ts)
                        if message_ts == key[2]:
                            root_seen = True
                        _, record_truncated = self._ingest_backfill_message(
                            key,
                            message,
                            state=state,
                            start_fingerprints=start_fingerprints,
                            start_live_generation=start_live_generation,
                        )
                        remote_authority_truncated = (
                            remote_authority_truncated
                            or record_truncated
                        )

                metadata = response.get("response_metadata") or {}
                next_cursor = (
                    str(metadata.get("next_cursor") or "")
                    if isinstance(metadata, dict)
                    else ""
                )
                if bool(response.get("has_more")) and not next_cursor:
                    raise RuntimeError(
                        "Slack thread history pagination is incomplete"
                    )
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise RuntimeError(
                        "Slack thread history cursor repeated"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            if not root_seen:
                raise RuntimeError(
                    "Slack thread history did not prove root coverage"
                )

            # A successful terminal root-based snapshot is authoritative for
            # start-time records. Missing records are remote deletions only
            # when their exact per-message CAS fingerprint is unchanged.
            for message_ts, start_fingerprint in (
                start_fingerprints.items()
            ):
                if message_ts in remote_seen:
                    continue
                current = state.records.get(message_ts)
                if (
                    current is None
                    or self._record_fingerprint(current)
                    != start_fingerprint
                ):
                    continue
                tombstone = {
                    **current,
                    "text": "",
                    "blocks": [],
                    "attachments": [],
                    "tombstone": True,
                    "source_rank": _DELETE_SOURCE_RANK,
                }
                tombstone.pop("_live_generation", None)
                self._upsert_record(
                    key,
                    tombstone,
                    root_observed=False,
                    live=False,
                    force=True,
                )

            state.complete = True
            state.persisted_complete = True
            state.needs_revalidate = False
            state.from_root = True
            state.validated_boot_id = self.boot_id
            state.authoritative = True
            state.verified_through_ts = max(
                remote_seen, key=_timestamp_key
            )
            self._refresh_authority_truncation(state)
            if remote_authority_truncated:
                state.authority_truncated = True
                state.authoritative = False
            state.hydrated_at = time.time()
            state.retry_after = (
                self._clock() + self.retry_cooldown_seconds
                if state.authority_truncated
                else 0.0
            )
            state.failure_count = 0
            if self._state_store is not None:
                persisted = (
                    self._state_store.finalize_transcript_hydration(
                        *key,
                        meta=self._meta(state),
                        required_records=list(state.records.values()),
                        root_ts=key[2],
                        expected_eviction_epoch=persistence_epoch,
                    )
                )
                if not persisted:
                    state.persistence_blocked = True
                    state.persisted_complete = False
        except Exception as exc:
            state.complete = False
            state.persisted_complete = False
            state.needs_revalidate = False
            state.from_root = False
            state.validated_boot_id = ""
            state.authoritative = False
            state.verified_through_ts = ""
            state.authority_truncated = False
            state.failure_count += 1
            cooldown = self._retry_after(
                exc, self.retry_cooldown_seconds
            )
            state.retry_after = self._clock() + cooldown
            self._counters["backfill_failures"] += 1
            self._persist_meta(key, state)
            logger.warning(
                "transcript backfill failed team=%s channel=%s "
                "thread=%s; local partial context retained, retry in %.0fs",
                key[0],
                key[1],
                key[2],
                cooldown,
            )

    @staticmethod
    def _has_authoritative_coverage(
        state: _ThreadState, current_ts: str
    ) -> bool:
        if not (
            state.complete
            and state.from_root
            and state.authoritative
            and not state.authority_truncated
            and state.verified_through_ts
        ):
            return False
        return _timestamp_key(current_ts) <= _timestamp_key(
            state.verified_through_ts
        )

    def _mark_cancelled_hydration(
        self, key: TranscriptKey, state: _ThreadState
    ) -> None:
        state.complete = False
        state.persisted_complete = False
        state.needs_revalidate = False
        state.from_root = False
        state.validated_boot_id = ""
        state.authoritative = False
        state.verified_through_ts = ""
        state.authority_truncated = False
        state.failure_count += 1
        state.retry_after = self._clock() + self.retry_cooldown_seconds
        self._counters["backfill_failures"] += 1
        self._persist_meta(key, state)
        logger.warning(
            "transcript shared backfill cancelled team=%s channel=%s "
            "thread=%s; retry in %.0fs",
            key[0],
            key[1],
            key[2],
            self.retry_cooldown_seconds,
        )

    async def _run_shared_backfill(
        self,
        client: Any,
        key: TranscriptKey,
        state: _ThreadState,
        hydration: _HydrationState,
    ) -> None:
        try:
            await self._backfill(client, key, state)
        except asyncio.CancelledError:
            self._mark_cancelled_hydration(key, state)
            raise
        finally:
            current = asyncio.current_task()
            if (
                self._hydrations.get(key) is hydration
                and hydration.task is current
            ):
                self._hydrations.pop(key, None)
            self._evict_memory()

    async def _ensure(
        self,
        client: Any,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        authoritative_ts: str = "",
    ) -> TranscriptSnapshot:
        self._counters["reads"] += 1
        key = self._key(team_id, channel_id, thread_ts)
        state = self._load_thread(key)
        needs_authority = bool(authoritative_ts)
        ready = (
            self._has_authoritative_coverage(
                state, authoritative_ts
            )
            if needs_authority
            else state.complete and not state.needs_revalidate
        )
        if ready:
            self._counters["warm_reads"] += 1
            return self._snapshot(key, state)
        if self._clock() < state.retry_after:
            self._counters["partial_reads"] += 1
            return self._snapshot(key, state)

        hydration = self._hydrations.get(key)
        if (
            hydration is not None
            and hydration.task is not None
            and not hydration.task.done()
        ):
            self._counters["singleflight_waits"] += 1
        else:
            hydration = _HydrationState()
            self._hydrations[key] = hydration
            try:
                hydration.task = asyncio.create_task(
                    self._run_shared_backfill(
                        client, key, state, hydration
                    )
                )
            except Exception:
                self._hydrations.pop(key, None)
                self._mark_cancelled_hydration(key, state)
                self._counters["partial_reads"] += 1
                return self._snapshot(key, state)

        hydration.waiters += 1
        try:
            assert hydration.task is not None
            await asyncio.shield(hydration.task)
        finally:
            hydration.waiters = max(0, hydration.waiters - 1)

        state = self._load_thread(key)
        ready = (
            self._has_authoritative_coverage(
                state, authoritative_ts
            )
            if needs_authority
            else state.complete and not state.needs_revalidate
        )
        if ready:
            self._counters["warm_reads"] += 1
        else:
            self._counters["partial_reads"] += 1
        return self._snapshot(key, state)

    async def ensure_thread(
        self,
        client: Any,
        team_id: str,
        channel_id: str,
        thread_ts: str,
    ) -> TranscriptSnapshot:
        """Return context-local data, hydrating only cold/incomplete state."""
        return await self._ensure(
            client, team_id, channel_id, thread_ts
        )

    async def ensure_authoritative(
        self,
        client: Any,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        current_ts: str,
    ) -> TranscriptSnapshot:
        """Ensure canonical ordering coverage through ``current_ts``."""
        return await self._ensure(
            client,
            team_id,
            channel_id,
            thread_ts,
            authoritative_ts=current_ts,
        )

    def status_snapshot(self) -> dict[str, Any]:
        """Sanitized counters only: never channel IDs, users, or message text."""
        threads = len(self._threads)
        messages = sum(
            len(state.records) for state in self._threads.values()
        )
        memory_bytes = self._memory_bytes()
        truncated_records = sum(
            bool(record.get("content_truncated"))
            for state in self._threads.values()
            for record in state.records.values()
        )
        warm_threads = sum(
            state.complete and not state.needs_revalidate
            for state in self._threads.values()
        )
        persisted = (
            self._state_store.transcript_counts()
            if self._state_store is not None
            else {
                "threads": 0,
                "messages": 0,
                "bytes": 0,
                "evicted_threads": 0,
                "byte_evicted_threads": 0,
            }
        )
        counters = {
            key: int(value) for key, value in self._counters.items()
        }
        counters["memory_evicted_threads"] = counters[
            "evicted_threads"
        ]
        counters["persisted_evicted_threads"] = int(
            persisted.get("evicted_threads", 0)
        )
        counters["evicted_threads"] += counters[
            "persisted_evicted_threads"
        ]
        counters["memory_byte_evicted_threads"] = counters[
            "byte_evicted_threads"
        ]
        counters["persisted_byte_evicted_threads"] = int(
            persisted.get("byte_evicted_threads", 0)
        )
        counters["byte_evicted_threads"] += counters[
            "persisted_byte_evicted_threads"
        ]
        return {
            "threads": threads,
            "messages": messages,
            "memory_bytes": memory_bytes,
            "truncated_records": truncated_records,
            "warm_threads": warm_threads,
            "partial_threads": threads - warm_threads,
            "persisted_threads": int(persisted["threads"]),
            "persisted_messages": int(persisted["messages"]),
            "persisted_bytes": int(persisted.get("bytes", 0)),
            "capacity": {
                "memory_threads": self.max_threads,
                "messages_per_thread": self.max_messages_per_thread,
                "persisted_threads": self.db_max_threads,
                "record_bytes": self.max_record_bytes,
                "memory_bytes": self.max_memory_bytes,
                "persisted_bytes": self.max_db_bytes,
                "ttl_seconds": self.ttl_seconds,
                "retry_cooldown_seconds": self.retry_cooldown_seconds,
            },
            **counters,
        }
