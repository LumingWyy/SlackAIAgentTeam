"""SQLite persistence for per-thread agent state (sessions / summaries / stats).

Single writer: multi_app's event loop (webui never touches this DB). An OS-level
lifetime lock prevents two nodes from sharing one STATE_DB. Lock contention is a
fatal configuration error; other sqlite/open errors remain best-effort and make
the store degrade to no-ops.

Not persisted on purpose: TurnBudget (observation-based, resets whenever a human
speaks) and per-thread locks / dedup caches (meaningless across processes).

Security: the DB holds session resume ids, handoff summaries, token stats and
last-seen timestamps. The DB/WAL/SHM files are always 0600 so other OS users
cannot read thread content; a parent directory is chmod 0700 only when this
class creates it, since STATE_DB may legitimately point into a shared directory
(the repo root, $HOME) whose permissions are not ours to narrow.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("state_store")

# SQLite's conventional byte-range locks live around 0x40000000. An exclusive
# one-byte POSIX record lock far outside that range protects the actual database
# inode (including hardlink aliases) without colliding with SQLite's own locks.
_DB_INODE_LOCK_OFFSET = 1 << 62
_LOCKED_DB_INODES: set[tuple[int, int]] = set()
_LOCKED_DB_INODES_GUARD = threading.Lock()
_LOCKED_DB_INODES_PID = os.getpid()


def _reset_db_inode_registry_after_fork() -> None:
    """Drop parent-only lock bookkeeping and replace a possibly held guard."""
    global _LOCKED_DB_INODES_GUARD, _LOCKED_DB_INODES_PID
    _LOCKED_DB_INODES.clear()
    _LOCKED_DB_INODES_GUARD = threading.Lock()
    _LOCKED_DB_INODES_PID = os.getpid()


def _ensure_db_inode_registry_process() -> None:
    """Lazy fork guard for runtimes that bypass ``register_at_fork``."""
    if _LOCKED_DB_INODES_PID != os.getpid():
        _reset_db_inode_registry_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_db_inode_registry_after_fork)

# Pre-v3 rows have no repository provenance. This value is deliberately not a
# canonical ``owner/repo`` and therefore can never equal a configured repo or
# the explicit no-repo identity (empty string) used by the current schema.
LEGACY_UNKNOWN_GITHUB_REPO = "<legacy-unknown-repo>"
LEGACY_UNKNOWN_WORKSPACE_MODE = "<legacy-unknown-workspace-mode>"
LEGACY_UNKNOWN_EXECUTION_PATH = "<legacy-unknown-execution-path>"
LEGACY_UNKNOWN_CONTINUATION_IDENTITY = (
    "<legacy-unknown-continuation-identity>"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_state (
    scope        TEXT NOT NULL DEFAULT '',
    agent        TEXT NOT NULL,
    thread_key   TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    runtime      TEXT NOT NULL DEFAULT '',
    workspace    TEXT NOT NULL DEFAULT '',
    workspace_mode TEXT NOT NULL DEFAULT '',
    execution_path TEXT NOT NULL DEFAULT '',
    continuation_identity TEXT NOT NULL DEFAULT '',
    github_repo  TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    num_turns    INTEGER NOT NULL DEFAULT 0,
    last_seen_ts TEXT NOT NULL DEFAULT '',
    updated_at   REAL NOT NULL,
    PRIMARY KEY (
        scope, agent, thread_key, runtime, workspace, workspace_mode,
        execution_path, continuation_identity, github_repo
    )
)
"""

_TRANSCRIPT_THREADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_threads (
    team_id              TEXT NOT NULL,
    channel_id           TEXT NOT NULL,
    thread_ts            TEXT NOT NULL,
    complete             INTEGER NOT NULL DEFAULT 0,
    from_root            INTEGER NOT NULL DEFAULT 0,
    truncated_before_ts  TEXT NOT NULL DEFAULT '',
    last_message_ts      TEXT NOT NULL DEFAULT '',
    hydrated_at          REAL NOT NULL DEFAULT 0,
    retry_after          REAL NOT NULL DEFAULT 0,
    failure_count        INTEGER NOT NULL DEFAULT 0,
    validated_boot_id    TEXT NOT NULL DEFAULT '',
    authoritative        INTEGER NOT NULL DEFAULT 0,
    verified_through_ts  TEXT NOT NULL DEFAULT '',
    authority_truncated  INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (team_id, channel_id, thread_ts)
)
"""

_TRANSCRIPT_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_messages (
    team_id           TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    thread_ts         TEXT NOT NULL,
    message_ts        TEXT NOT NULL,
    user_id           TEXT NOT NULL DEFAULT '',
    bot_id            TEXT NOT NULL DEFAULT '',
    subtype           TEXT NOT NULL DEFAULT '',
    text              TEXT NOT NULL DEFAULT '',
    blocks_json       TEXT NOT NULL DEFAULT '[]',
    attachments_json  TEXT NOT NULL DEFAULT '[]',
    edited_ts         TEXT NOT NULL DEFAULT '',
    revision_ts       TEXT NOT NULL DEFAULT '',
    tombstone         INTEGER NOT NULL DEFAULT 0,
    source_rank       INTEGER NOT NULL DEFAULT 0,
    payload_bytes     INTEGER NOT NULL DEFAULT 0,
    content_truncated INTEGER NOT NULL DEFAULT 0,
    updated_at        REAL NOT NULL,
    PRIMARY KEY (team_id, channel_id, thread_ts, message_ts)
)
"""

_OWNER_DAILY_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_daily_usage (
    utc_day          TEXT NOT NULL,
    owner            TEXT NOT NULL,
    agent            TEXT NOT NULL,
    runtime          TEXT NOT NULL,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_tokens     INTEGER NOT NULL DEFAULT 0,
    turns            INTEGER NOT NULL DEFAULT 0,
    completed_turns  INTEGER NOT NULL DEFAULT 0,
    estimated_turns  INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    denied           INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (utc_day, owner, agent, runtime)
)
"""

_OWNER_QUOTA_RESERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_quota_reservations (
    reservation_id  TEXT PRIMARY KEY,
    utc_day         TEXT NOT NULL,
    owner           TEXT NOT NULL,
    agent           TEXT NOT NULL,
    runtime         TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    runtime_started INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
)
"""

_WORKTREE_MAPPINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS worktree_mappings (
    identity_digest TEXT PRIMARY KEY,
    repo_digest     TEXT NOT NULL,
    common_dir      TEXT NOT NULL,
    base_workspace  TEXT NOT NULL,
    team_id         TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    root_thread_ts  TEXT NOT NULL,
    owner           TEXT NOT NULL,
    path            TEXT NOT NULL UNIQUE,
    branch          TEXT NOT NULL UNIQUE,
    base_ref        TEXT NOT NULL,
    base_oid        TEXT NOT NULL,
    retained_head_oid TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    last_used_at    REAL NOT NULL,
    updated_at      REAL NOT NULL
)
"""

# `codex exec resume` reports usage accumulated over the whole session; the
# last cumulative counters per codex thread let a restart still bill only
# the new turn's delta.
_CODEX_USAGE_BASELINES_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_usage_baselines (
    session_id    TEXT PRIMARY KEY,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
)
"""
CODEX_USAGE_BASELINE_TTL_SECONDS = 30 * 24 * 3600

# Admitted Slack activations that have not finished. A row left behind at
# startup means the previous process stopped mid-activation.
_ACTIVATION_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS activation_ledger (
    agent       TEXT NOT NULL,
    team_id     TEXT NOT NULL DEFAULT '',
    channel_id  TEXT NOT NULL,
    trigger_ts  TEXT NOT NULL,
    thread_ts   TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    PRIMARY KEY (agent, channel_id, trigger_ts)
)
"""


class StateStoreLockError(RuntimeError):
    """Raised when another live StateStore already owns the same DB path."""


class StateStore:
    """One row per agent/thread/runtime/workspace/repo; survives restarts.

    session_id is engine- and cwd-bound (claude resume / codex thread), so it is
    restored only for its exact identity. A handoff summary may bridge a runtime
    or workspace change within one repo, but repo boundaries never share state.
    """

    def __init__(self, path: str) -> None:
        # Resolve every textual alias before deriving the sidecar or opening
        # SQLite. realpath handles symlink and relative/.. aliases; a separate
        # lifetime flock on the actual DB inode below also handles hardlinks.
        self.path = os.path.realpath(
            os.path.abspath(os.path.expanduser(path))
        )
        self._conn: sqlite3.Connection | None = None
        # Diagnostic/path lock and actual-inode lock respectively.
        self._lock_fd: int | None = None
        self._db_lock_fd: int | None = None
        self._db_inode_key: tuple[int, int] | None = None
        self._lock_owner_pid = os.getpid()
        self._transcript_evicted_threads = 0
        self._transcript_byte_evicted_threads = 0
        self._transcript_eviction_epochs: dict[
            tuple[str, str, str], int
        ] = {}
        try:
            parent = Path(self.path).resolve().parent
            # Harden only a directory we create. chmod-ing a pre-existing one would
            # silently narrow permissions on whatever STATE_DB happens to sit in —
            # the repo root for the default STATE_DB=state.db, or $HOME. The 0600 on
            # the DB files below is what actually protects the contents.
            created = not parent.exists()
            parent.mkdir(parents=True, exist_ok=True)
            if created:
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    logger.debug(
                        "could not chmod 0700 state dir %s", parent, exc_info=True
                    )

            self._acquire_lock()

            # Create the DB with umask so a new file is not world-readable.
            old_umask = os.umask(0o077)
            try:
                conn = sqlite3.connect(self.path)
            finally:
                os.umask(old_umask)

            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute(_SCHEMA)
            self._migrate_legacy_schema(conn)
            conn.execute(_TRANSCRIPT_THREADS_SCHEMA)
            conn.execute(_TRANSCRIPT_MESSAGES_SCHEMA)
            self._migrate_transcript_schema(conn)
            conn.execute(_OWNER_DAILY_USAGE_SCHEMA)
            conn.execute(_OWNER_QUOTA_RESERVATIONS_SCHEMA)
            conn.execute(_WORKTREE_MAPPINGS_SCHEMA)
            self._migrate_worktree_schema(conn)
            conn.execute(_CODEX_USAGE_BASELINES_SCHEMA)
            conn.execute(_ACTIVATION_LEDGER_SCHEMA)
            conn.commit()
            self._conn = conn
            self._harden_file_perms()
        except StateStoreLockError:
            raise
        except (sqlite3.Error, OSError):
            logger.warning(
                "state store disabled: cannot open %s", self.path, exc_info=True
            )
            try:
                conn.close()
            except (NameError, sqlite3.Error):
                pass
            self._conn = None
            self._release_lock()

    def _acquire_lock(self) -> None:
        lock_path = f"{self.path}.lock"
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        old_umask = os.umask(0o077)
        try:
            sidecar_fd = os.open(lock_path, open_flags, 0o600)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(
                sidecar_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError as exc:
            os.close(sidecar_fd)
            raise StateStoreLockError(
                f"state DB already in use: {self.path}; each owner/node "
                "must use an independent STATE_DB"
            ) from exc
        except OSError:
            os.close(sidecar_fd)
            raise

        # Create/open and lock the real database file before sqlite3.connect or
        # any schema/PRAGMA work. A hardlink has a different sidecar pathname
        # but the same inode, so this is the authoritative single-writer guard.
        old_umask = os.umask(0o077)
        try:
            db_fd = os.open(self.path, open_flags, 0o600)
        except OSError:
            self._unlock_close_fd(sidecar_fd)
            raise
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.path, 0o600)
            stat_result = os.fstat(db_fd)
            inode_key = (int(stat_result.st_dev), int(stat_result.st_ino))
            _ensure_db_inode_registry_process()
            with _LOCKED_DB_INODES_GUARD:
                if inode_key in _LOCKED_DB_INODES:
                    raise StateStoreLockError(
                        f"state DB already in use: {self.path}; database "
                        "inode is already locked in this process"
                    )
                try:
                    self._set_inode_lock(db_fd, fcntl.F_WRLCK)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    raise StateStoreLockError(
                        f"state DB already in use: {self.path}; database "
                        "inode is already locked by another node"
                    ) from exc
                _LOCKED_DB_INODES.add(inode_key)
        except StateStoreLockError:
            os.close(db_fd)
            self._unlock_close_fd(sidecar_fd)
            raise
        except OSError:
            os.close(db_fd)
            self._unlock_close_fd(sidecar_fd)
            raise
        self._lock_fd = sidecar_fd
        self._db_lock_fd = db_fd
        self._db_inode_key = inode_key

    @staticmethod
    def _set_inode_lock(fd: int, lock_type: int) -> None:
        """Set an OFD record lock without interfering with SQLite byte locks.

        Unlike traditional POSIX process locks, an open-file-description lock
        is not discarded when SQLite closes one of its own descriptors for the
        same inode during setup. Darwin and Linux expose different ``flock``
        struct layouts, so pack the native layout explicitly.
        """
        command = getattr(fcntl, "F_OFD_SETLK", None)
        if command is None:
            raise OSError(
                errno.ENOTSUP,
                "open-file-description locks are required for STATE_DB",
            )
        if sys.platform == "darwin":
            lock_data = struct.pack(
                "@qqihh",
                _DB_INODE_LOCK_OFFSET,
                1,
                0,
                lock_type,
                os.SEEK_SET,
            )
        else:
            lock_data = struct.pack(
                "@hhqqi",
                lock_type,
                os.SEEK_SET,
                _DB_INODE_LOCK_OFFSET,
                1,
                0,
            )
        fcntl.fcntl(fd, command, lock_data)

    @staticmethod
    def _unlock_close_fd(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _release_lock(self) -> None:
        owns_lock = self._lock_owner_pid == os.getpid()
        db_fd = self._db_lock_fd
        self._db_lock_fd = None
        if db_fd is not None:
            inode_key = self._db_inode_key
            self._db_inode_key = None
            if owns_lock:
                try:
                    self._set_inode_lock(db_fd, fcntl.F_UNLCK)
                except OSError:
                    pass
            if inode_key is not None:
                _ensure_db_inode_registry_process()
                with _LOCKED_DB_INODES_GUARD:
                    _LOCKED_DB_INODES.discard(inode_key)
            try:
                os.close(db_fd)
            except OSError:
                pass
        sidecar_fd = self._lock_fd
        self._lock_fd = None
        if sidecar_fd is not None:
            if owns_lock:
                self._unlock_close_fd(sidecar_fd)
            else:
                try:
                    os.close(sidecar_fd)
                except OSError:
                    pass

    @staticmethod
    def _quota_row_values(
        *,
        total_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
        turns: int = 0,
        completed_turns: int = 0,
        estimated_turns: int = 0,
        errors: int = 0,
        denied: int = 0,
    ) -> tuple[int, ...]:
        values = (
            total_tokens,
            input_tokens,
            output_tokens,
            cache_tokens,
            turns,
            completed_turns,
            estimated_turns,
            errors,
            denied,
        )
        if any(isinstance(value, bool) or int(value) < 0 for value in values):
            raise ValueError("quota counters must be non-negative integers")
        return tuple(int(value) for value in values)

    @staticmethod
    def _upsert_owner_usage(
        conn: sqlite3.Connection,
        *,
        utc_day: str,
        owner: str,
        agent: str,
        runtime: str,
        total_tokens: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
        turns: int = 0,
        completed_turns: int = 0,
        estimated_turns: int = 0,
        errors: int = 0,
        denied: int = 0,
    ) -> None:
        counters = StateStore._quota_row_values(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_tokens,
            turns=turns,
            completed_turns=completed_turns,
            estimated_turns=estimated_turns,
            errors=errors,
            denied=denied,
        )
        conn.execute(
            """
            INSERT INTO owner_daily_usage (
                utc_day, owner, agent, runtime, total_tokens, input_tokens,
                output_tokens, cache_tokens, turns, completed_turns,
                estimated_turns, errors, denied, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(utc_day, owner, agent, runtime) DO UPDATE SET
                total_tokens = total_tokens + excluded.total_tokens,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                cache_tokens = cache_tokens + excluded.cache_tokens,
                turns = turns + excluded.turns,
                completed_turns = completed_turns + excluded.completed_turns,
                estimated_turns = estimated_turns + excluded.estimated_turns,
                errors = errors + excluded.errors,
                denied = denied + excluded.denied,
                updated_at = excluded.updated_at
            """,
            (
                utc_day,
                owner,
                agent,
                runtime,
                *counters,
                time.time(),
            ),
        )

    def reserve_owner_quota(
        self,
        *,
        reservation_id: str,
        utc_day: str,
        owner: str,
        agent: str,
        runtime: str,
        daily_limit: int,
        reservation_tokens: int,
    ) -> bool:
        """Atomically reserve one owner's daily capacity or record a denial."""
        if self._conn is None:
            raise RuntimeError("state store is unavailable for quota reservation")
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(total_tokens), 0)
                    FROM owner_daily_usage
                    WHERE utc_day = ? AND owner = ?
                    """,
                    (utc_day, owner),
                ).fetchone()[0]
            )
            active = int(
                conn.execute(
                    """
                    SELECT COALESCE(SUM(reserved_tokens), 0)
                    FROM owner_quota_reservations
                    WHERE utc_day = ? AND owner = ?
                    """,
                    (utc_day, owner),
                ).fetchone()[0]
            )
            if used + active + reservation_tokens > daily_limit:
                self._upsert_owner_usage(
                    conn,
                    utc_day=utc_day,
                    owner=owner,
                    agent=agent,
                    runtime=runtime,
                    denied=1,
                )
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO owner_quota_reservations (
                    reservation_id, utc_day, owner, agent, runtime,
                    reserved_tokens, runtime_started, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    reservation_id,
                    utc_day,
                    owner,
                    agent,
                    runtime,
                    reservation_tokens,
                    time.time(),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError("owner quota reservation failed") from exc

    def mark_owner_quota_started(self, reservation_id: str) -> bool:
        if self._conn is None:
            raise RuntimeError("state store is unavailable for quota reservation")
        try:
            cursor = self._conn.execute(
                """
                UPDATE owner_quota_reservations
                SET runtime_started = 1
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            self._conn.commit()
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError("owner quota start marker failed") from exc

    def release_owner_quota(self, reservation_id: str) -> bool:
        """Release only a reservation whose provider runtime never started."""
        if self._conn is None:
            raise RuntimeError("state store is unavailable for quota reservation")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """
                SELECT runtime_started FROM owner_quota_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return False
            if int(row[0]):
                self._conn.rollback()
                raise RuntimeError(
                    "started quota reservation must be settled, not released"
                )
            self._conn.execute(
                """
                DELETE FROM owner_quota_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError("owner quota release failed") from exc

    def settle_owner_quota(
        self,
        reservation_id: str,
        *,
        total_tokens: int,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int,
        estimated: bool,
        error: bool,
    ) -> bool:
        if self._conn is None:
            raise RuntimeError("state store is unavailable for quota settlement")
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT utc_day, owner, agent, runtime, runtime_started
                FROM owner_quota_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return False
            if not int(row["runtime_started"]):
                conn.rollback()
                raise RuntimeError(
                    "cannot settle quota before provider runtime starts"
                )
            self._upsert_owner_usage(
                conn,
                utc_day=str(row["utc_day"]),
                owner=str(row["owner"]),
                agent=str(row["agent"]),
                runtime=str(row["runtime"]),
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                turns=1,
                completed_turns=0 if estimated else 1,
                estimated_turns=1 if estimated else 0,
                errors=1 if error else 0,
            )
            conn.execute(
                """
                DELETE FROM owner_quota_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            conn.commit()
            return True
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError("owner quota settlement failed") from exc

    def record_owner_usage(
        self,
        *,
        utc_day: str,
        owner: str,
        agent: str,
        runtime: str,
        total_tokens: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
        estimated: bool = False,
        error: bool = False,
    ) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._upsert_owner_usage(
                self._conn,
                utc_day=utc_day,
                owner=owner,
                agent=agent,
                runtime=runtime,
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_tokens=cache_tokens,
                turns=1,
                completed_turns=0 if estimated else 1,
                estimated_turns=1 if estimated else 0,
                errors=1 if error else 0,
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError("owner usage write failed") from exc

    def recover_owner_quota_reservations(self) -> dict[str, int]:
        """Recover crash-stale reservations under the process lifetime DB lock."""
        if self._conn is None:
            return {"settled": 0, "released": 0}
        conn = self._conn
        settled = 0
        released = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT reservation_id, utc_day, owner, agent, runtime,
                       reserved_tokens, runtime_started
                FROM owner_quota_reservations
                """
            ).fetchall()
            for row in rows:
                if int(row["runtime_started"]):
                    self._upsert_owner_usage(
                        conn,
                        utc_day=str(row["utc_day"]),
                        owner=str(row["owner"]),
                        agent=str(row["agent"]),
                        runtime=str(row["runtime"]),
                        total_tokens=int(row["reserved_tokens"]),
                        turns=1,
                        estimated_turns=1,
                        errors=1,
                    )
                    settled += 1
                else:
                    released += 1
            conn.execute("DELETE FROM owner_quota_reservations")
            conn.commit()
            return {"settled": settled, "released": released}
        except sqlite3.Error as exc:
            conn.rollback()
            raise RuntimeError("owner quota stale recovery failed") from exc

    def save_worktree_mapping(
        self,
        *,
        identity_digest: str,
        repo_digest: str,
        common_dir: str,
        base_workspace: str,
        team_id: str,
        channel_id: str,
        root_thread_ts: str,
        owner: str,
        path: str,
        branch: str,
        base_ref: str,
        base_oid: str,
        status: str,
        retained_head_oid: str = "",
        last_error: str = "",
        created_at: float | None = None,
        last_used_at: float | None = None,
    ) -> bool:
        """Persist one deterministic worktree mapping.

        Callers validate paths, refs and identities before reaching SQLite.
        The unique path/ref constraints are a final collision guard.
        """
        if self._conn is None:
            return False
        now = time.time()
        created = now if created_at is None else float(created_at)
        last_used = now if last_used_at is None else float(last_used_at)
        try:
            self._conn.execute(
                """
                INSERT INTO worktree_mappings (
                    identity_digest, repo_digest, common_dir, base_workspace,
                    team_id, channel_id, root_thread_ts, owner, path, branch,
                    base_ref, base_oid, retained_head_oid, status, last_error,
                    created_at, last_used_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_digest) DO UPDATE SET
                    repo_digest=excluded.repo_digest,
                    common_dir=excluded.common_dir,
                    base_workspace=excluded.base_workspace,
                    team_id=excluded.team_id,
                    channel_id=excluded.channel_id,
                    root_thread_ts=excluded.root_thread_ts,
                    owner=excluded.owner,
                    path=excluded.path,
                    branch=excluded.branch,
                    base_ref=excluded.base_ref,
                    base_oid=excluded.base_oid,
                    retained_head_oid=excluded.retained_head_oid,
                    status=excluded.status,
                    last_error=excluded.last_error,
                    last_used_at=excluded.last_used_at,
                    updated_at=excluded.updated_at
                """,
                (
                    identity_digest,
                    repo_digest,
                    common_dir,
                    base_workspace,
                    team_id,
                    channel_id,
                    root_thread_ts,
                    owner,
                    path,
                    branch,
                    base_ref,
                    base_oid,
                    retained_head_oid,
                    status,
                    last_error,
                    created,
                    last_used,
                    now,
                ),
            )
            self._conn.commit()
            self._harden_file_perms()
            return True
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning(
                "state store worktree mapping write failed",
                exc_info=True,
            )
            return False

    def begin_worktree_removal(
        self,
        *,
        identity_digest: str,
        expected_path: str,
        expected_status: str,
        retained_head_oid: str,
    ) -> bool:
        """CAS a live mapping to durable pre-Git ``removing`` state."""
        if self._conn is None:
            return False
        now = time.time()
        try:
            cursor = self._conn.execute(
                """
                UPDATE worktree_mappings
                SET status='removing', retained_head_oid=?, last_error='',
                    last_used_at=?, updated_at=?
                WHERE identity_digest=? AND path=? AND status=?
                """,
                (
                    retained_head_oid,
                    now,
                    now,
                    identity_digest,
                    expected_path,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            self._harden_file_perms()
            return True
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning(
                "state store worktree removing transition failed",
                exc_info=True,
            )
            return False

    def mark_worktree_removed(
        self,
        *,
        identity_digest: str,
        expected_path: str,
        expected_status: str,
        retained_head_oid: str,
    ) -> bool:
        """CAS one unchanged live mapping to removed with its exact HEAD."""
        if self._conn is None:
            return False
        now = time.time()
        try:
            cursor = self._conn.execute(
                """
                UPDATE worktree_mappings
                SET status='removed', retained_head_oid=?, last_error='',
                    last_used_at=?, updated_at=?
                WHERE identity_digest=? AND path=? AND status=?
                    AND retained_head_oid=?
                """,
                (
                    retained_head_oid,
                    now,
                    now,
                    identity_digest,
                    expected_path,
                    expected_status,
                    retained_head_oid,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            self._harden_file_perms()
            return True
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning(
                "state store worktree removed transition failed",
                exc_info=True,
            )
            return False

    def load_worktree_mapping(
        self, identity_digest: str
    ) -> dict[str, Any] | None:
        """Load one worktree registry row without following its path."""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM worktree_mappings WHERE identity_digest=?",
                (identity_digest,),
            ).fetchone()
        except sqlite3.Error:
            logger.warning(
                "state store worktree mapping load failed",
                exc_info=True,
            )
            return None
        return dict(row) if row is not None else None

    def rehome_removed_worktree_mapping(
        self,
        *,
        identity_digest: str,
        old_path: str,
        new_path: str,
    ) -> bool:
        """Atomically move only an unchanged removed mapping to a new root."""
        if self._conn is None:
            return False
        try:
            cursor = self._conn.execute(
                """
                UPDATE worktree_mappings
                SET path=?, updated_at=?
                WHERE identity_digest=? AND path=? AND status='removed'
                """,
                (
                    new_path,
                    time.time(),
                    identity_digest,
                    old_path,
                ),
            )
            if cursor.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.commit()
            self._harden_file_perms()
            return True
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning(
                "state store removed worktree rehome failed",
                exc_info=True,
            )
            return False

    def list_worktree_mappings(
        self,
        *,
        common_dir: str | None = None,
        owner: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return registry rows for status/capacity inspection."""
        if self._conn is None:
            return []
        row_limit = (
            min(10_000, max(0, int(limit)))
            if limit is not None
            else None
        )
        if row_limit == 0:
            return []
        try:
            conditions: list[str] = []
            params: tuple[Any, ...] = ()
            if common_dir is not None:
                conditions.append("common_dir=?")
                params += (common_dir,)
            if owner is not None:
                conditions.append("owner=?")
                params += (owner,)
            sql = "SELECT * FROM worktree_mappings"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY last_used_at DESC, identity_digest"
            if row_limit is not None:
                sql += " LIMIT ?"
                params += (row_limit,)
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            logger.warning(
                "state store worktree mapping list failed",
                exc_info=True,
            )
            return []
        return [dict(row) for row in rows]

    def owner_usage_snapshot(
        self, owner: str, utc_day: str
    ) -> dict[str, Any]:
        if self._conn is None:
            return {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_tokens": 0,
                "turns": 0,
                "completed_turns": 0,
                "estimated_turns": 0,
                "errors": 0,
                "denied": 0,
                "active_reserved_tokens": 0,
                "breakdown": [],
            }
        rows = self._conn.execute(
            """
            SELECT agent, runtime, total_tokens, input_tokens, output_tokens,
                   cache_tokens, turns, completed_turns, estimated_turns,
                   errors, denied
            FROM owner_daily_usage
            WHERE utc_day = ? AND owner = ?
            ORDER BY agent, runtime
            """,
            (utc_day, owner),
        ).fetchall()
        fields = (
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cache_tokens",
            "turns",
            "completed_turns",
            "estimated_turns",
            "errors",
            "denied",
        )
        result: dict[str, Any] = {
            field: sum(int(row[field]) for row in rows) for field in fields
        }
        result["active_reserved_tokens"] = int(
            self._conn.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0)
                FROM owner_quota_reservations
                WHERE utc_day = ? AND owner = ?
                """,
                (utc_day, owner),
            ).fetchone()[0]
        )
        result["breakdown"] = [
            {
                "agent": str(row["agent"]),
                "runtime": str(row["runtime"]),
                **{field: int(row[field]) for field in fields},
            }
            for row in rows
        ]
        return result

    @staticmethod
    def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
        """Migrate pre-scope and pre-repo databases to the current identity.

        SQLite cannot change a primary key in place, so migration uses a
        transactionally replaced table. Pre-v2 rows receive the empty Slack
        scope; pre-v3 rows receive an impossible repo sentinel. Those explicit
        unknown identities preserve every legacy row without claiming a current
        team or repository. In particular, legacy state must never become the
        current schema's explicit no-repo identity.
        """
        table_info = conn.execute(
            "PRAGMA table_info(thread_state)"
        ).fetchall()
        columns = {str(row["name"]) for row in table_info}
        primary_key = [
            str(row["name"])
            for row in sorted(
                (row for row in table_info if int(row["pk"]) > 0),
                key=lambda row: int(row["pk"]),
            )
        ]
        target_primary_key = [
            "scope",
            "agent",
            "thread_key",
            "runtime",
            "workspace",
            "workspace_mode",
            "execution_path",
            "continuation_identity",
            "github_repo",
        ]
        if (
            {
                "github_repo",
                "workspace_mode",
                "execution_path",
                "continuation_identity",
            }
            <= columns
            and primary_key == target_primary_key
        ):
            return
        scope_expr = "scope" if "scope" in columns else "''"
        has_repo_identity = "github_repo" in columns
        repo_expr = "github_repo" if has_repo_identity else "?"
        mode_expr = (
            "workspace_mode"
            if "workspace_mode" in columns
            else "?"
        )
        path_expr = (
            "execution_path"
            if "execution_path" in columns
            else "?"
        )
        continuation_expr = (
            "continuation_identity"
            if "continuation_identity" in columns
            else "?"
        )
        migration_params: tuple[str, ...] = ()
        if "workspace_mode" not in columns:
            migration_params += (LEGACY_UNKNOWN_WORKSPACE_MODE,)
        if "execution_path" not in columns:
            migration_params += (LEGACY_UNKNOWN_EXECUTION_PATH,)
        if "continuation_identity" not in columns:
            migration_params += (
                LEGACY_UNKNOWN_CONTINUATION_IDENTITY,
            )
        if not has_repo_identity:
            migration_params += (LEGACY_UNKNOWN_GITHUB_REPO,)
        conn.execute("SAVEPOINT migrate_thread_state_identity")
        try:
            conn.execute(
                "ALTER TABLE thread_state RENAME TO thread_state_legacy"
            )
            conn.execute(_SCHEMA)
            conn.execute(
                """
                INSERT INTO thread_state (
                    scope, agent, thread_key, session_id, runtime, workspace,
                    workspace_mode, execution_path, continuation_identity,
                    github_repo, summary, input_tokens, num_turns,
                    last_seen_ts, updated_at
                )
                SELECT
                    {scope}, agent, thread_key, session_id, runtime, workspace,
                    {mode}, {path}, {continuation}, {repo}, summary,
                    input_tokens, num_turns, last_seen_ts, updated_at
                FROM thread_state_legacy
                """.format(
                    scope=scope_expr,
                    mode=mode_expr,
                    path=path_expr,
                    continuation=continuation_expr,
                    repo=repo_expr,
                ),
                migration_params,
            )
            conn.execute("DROP TABLE thread_state_legacy")
            conn.execute("RELEASE migrate_thread_state_identity")
        except sqlite3.Error:
            conn.execute("ROLLBACK TO migrate_thread_state_identity")
            conn.execute("RELEASE migrate_thread_state_identity")
            raise

    @staticmethod
    def _migrate_transcript_schema(conn: sqlite3.Connection) -> None:
        """Add transcript metadata columns without replacing existing rows."""
        thread_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(transcript_threads)"
            ).fetchall()
        }
        for name, ddl in (
            ("validated_boot_id", "TEXT NOT NULL DEFAULT ''"),
            ("authoritative", "INTEGER NOT NULL DEFAULT 0"),
            ("verified_through_ts", "TEXT NOT NULL DEFAULT ''"),
            ("authority_truncated", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in thread_columns:
                conn.execute(
                    f"ALTER TABLE transcript_threads "
                    f"ADD COLUMN {name} {ddl}"
                )

        message_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(transcript_messages)"
            ).fetchall()
        }
        for name, ddl in (
            ("payload_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("content_truncated", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in message_columns:
                conn.execute(
                    f"ALTER TABLE transcript_messages "
                    f"ADD COLUMN {name} {ddl}"
                )
        # Legacy rows have unknown boot authority. Estimate their logical
        # payload so byte pruning is bounded immediately after migration.
        conn.execute(
            "UPDATE transcript_messages SET payload_bytes="
            "length(CAST(text AS BLOB))"
            "+length(CAST(blocks_json AS BLOB))"
            "+length(CAST(attachments_json AS BLOB))+256 "
            "WHERE payload_bytes<=0"
        )

    @staticmethod
    def _migrate_worktree_schema(conn: sqlite3.Connection) -> None:
        """Add removal-integrity metadata without rewriting legacy rows."""
        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(worktree_mappings)"
            ).fetchall()
        }
        if "retained_head_oid" not in columns:
            conn.execute(
                "ALTER TABLE worktree_mappings ADD COLUMN "
                "retained_head_oid TEXT NOT NULL DEFAULT ''"
            )

    def _harden_file_perms(self) -> None:
        """Ensure DB + SQLite sidecars are owner-read/write only (0600)."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = f"{self.path}{suffix}"
            try:
                if os.path.exists(p):
                    os.chmod(p, 0o600)
            except OSError:
                logger.debug("could not chmod 0600 %s", p, exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def _note_transcript_eviction(
        self, key: tuple[str, str, str], *, byte_eviction: bool = False
    ) -> None:
        self._transcript_eviction_epochs[key] = (
            self._transcript_eviction_epochs.get(key, 0) + 1
        )
        self._transcript_evicted_threads += 1
        if byte_eviction:
            self._transcript_byte_evicted_threads += 1

    def transcript_eviction_epoch(
        self, team_id: str, channel_id: str, thread_ts: str
    ) -> int:
        """Process-local generation bumped whenever this DB key is pruned."""
        return self._transcript_eviction_epochs.get(
            (str(team_id), str(channel_id), str(thread_ts)), 0
        )

    def _exec(self, sql: str, params: tuple = ()) -> int:
        """Execute + commit; affected rowcount, or -1 when disabled / on error."""
        if self._conn is None:
            return -1
        try:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount
        except sqlite3.Error:
            logger.warning(
                "state store write failed (%s)", sql.split()[0], exc_info=True
            )
            return -1

    def record_activation(
        self,
        agent: str,
        *,
        team_id: str,
        channel_id: str,
        trigger_ts: str,
        thread_ts: str,
        now: float | None = None,
    ) -> None:
        self._exec(
            """
            INSERT OR REPLACE INTO activation_ledger (
                agent, team_id, channel_id, trigger_ts, thread_ts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                agent,
                team_id,
                channel_id,
                trigger_ts,
                thread_ts,
                time.time() if now is None else now,
            ),
        )

    def clear_activation(
        self, agent: str, *, channel_id: str, trigger_ts: str
    ) -> None:
        self._exec(
            """
            DELETE FROM activation_ledger
            WHERE agent = ? AND channel_id = ? AND trigger_ts = ?
            """,
            (agent, channel_id, trigger_ts),
        )

    def take_interrupted_activations(
        self,
        agent: str,
        *,
        team_id: str,
        max_age_seconds: float,
        limit: int,
        now: float | None = None,
    ) -> list[dict[str, str]]:
        """Remove this agent's leftover rows; return the recent ones, newest first.

        Call once at startup, before any new activation is admitted: every
        row still present belonged to the previous process.
        """
        if self._conn is None:
            return []
        now = time.time() if now is None else now
        try:
            rows = self._conn.execute(
                """
                SELECT channel_id, trigger_ts, thread_ts, created_at
                FROM activation_ledger
                WHERE agent = ? AND team_id = ?
                ORDER BY created_at DESC
                """,
                (agent, team_id),
            ).fetchall()
            self._conn.execute(
                "DELETE FROM activation_ledger WHERE agent = ? AND team_id = ?",
                (agent, team_id),
            )
            self._conn.commit()
        except sqlite3.Error:
            logger.warning("activation ledger read failed", exc_info=True)
            return []
        recent = [
            {
                "channel_id": str(row[0]),
                "trigger_ts": str(row[1]),
                "thread_ts": str(row[2]),
            }
            for row in rows
            if now - float(row[3]) <= max_age_seconds
        ]
        return recent[:limit]

    def load_codex_usage_baseline(
        self, session_id: str
    ) -> tuple[int, int, int] | None:
        """Last cumulative ``(input, cache, output)`` of one codex thread."""
        if self._conn is None or not session_id:
            return None
        try:
            row = self._conn.execute(
                """
                SELECT input_tokens, cache_tokens, output_tokens
                FROM codex_usage_baselines WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            logger.warning("codex usage baseline read failed", exc_info=True)
            return None
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2])

    def save_codex_usage_baseline(
        self,
        session_id: str,
        *,
        input_tokens: int,
        cache_tokens: int,
        output_tokens: int,
        now: float | None = None,
    ) -> None:
        if not session_id:
            return
        now = time.time() if now is None else now
        self._exec(
            """
            INSERT INTO codex_usage_baselines (
                session_id, input_tokens, cache_tokens, output_tokens,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                input_tokens = excluded.input_tokens,
                cache_tokens = excluded.cache_tokens,
                output_tokens = excluded.output_tokens,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                int(input_tokens),
                int(cache_tokens),
                int(output_tokens),
                now,
            ),
        )
        self._exec(
            "DELETE FROM codex_usage_baselines WHERE updated_at < ?",
            (now - CODEX_USAGE_BASELINE_TTL_SECONDS,),
        )

    def save_turn(
        self,
        agent: str,
        thread_key: str,
        *,
        session_id: str,
        runtime: str,
        workspace: str,
        workspace_mode: str = "serial",
        execution_path: str | None = None,
        continuation_identity: str = "",
        summary: str,
        input_tokens: int,
        num_turns: int,
        last_seen_ts: str,
        scope: str = "",
        github_repo: str = "",
    ) -> None:
        """Upsert one thread's full state after a completed turn."""
        canonical_execution_path = os.path.realpath(
            os.path.abspath(
                os.path.expanduser(execution_path or workspace)
            )
        )
        self._exec(
            """
            INSERT INTO thread_state (
                scope, agent, thread_key, session_id, runtime, workspace,
                workspace_mode, execution_path, continuation_identity,
                github_repo, summary, input_tokens, num_turns, last_seen_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                scope, agent, thread_key, runtime, workspace, workspace_mode,
                execution_path, continuation_identity, github_repo
            ) DO UPDATE SET
                session_id=excluded.session_id, summary=excluded.summary,
                input_tokens=excluded.input_tokens, num_turns=excluded.num_turns,
                last_seen_ts=excluded.last_seen_ts, updated_at=excluded.updated_at
            """,
            (
                scope,
                agent,
                thread_key,
                session_id,
                runtime,
                workspace,
                workspace_mode,
                canonical_execution_path,
                continuation_identity,
                github_repo,
                summary,
                int(input_tokens),
                int(num_turns),
                last_seen_ts,
                time.time(),
            ),
        )
        # WAL/SHM may appear after the first write
        self._harden_file_perms()

    def set_summary(
        self,
        agent: str,
        thread_key: str,
        summary: str,
        *,
        scope: str = "",
        runtime: str | None = None,
        workspace: str | None = None,
        workspace_mode: str = "serial",
        execution_path: str | None = None,
        continuation_identity: str = "",
        github_repo: str = "",
    ) -> None:
        """Context rollover: keep the summary, drop the session id and stats."""
        if runtime is None or workspace is None:
            updated = self._exec(
                "UPDATE thread_state SET summary=?, session_id='',"
                " input_tokens=0, num_turns=0, updated_at=?"
                " WHERE scope=? AND agent=? AND thread_key=? AND github_repo=?",
                (
                    summary,
                    time.time(),
                    scope,
                    agent,
                    thread_key,
                    github_repo,
                ),
            )
            if updated != 0:
                return
            runtime = runtime or ""
            workspace = workspace or ""
        canonical_execution_path = os.path.realpath(
            os.path.abspath(
                os.path.expanduser(execution_path or workspace)
            )
        )
        self._exec(
            """
            INSERT INTO thread_state (
                scope, agent, thread_key, runtime, workspace, workspace_mode,
                execution_path, continuation_identity, github_repo, summary,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                scope, agent, thread_key, runtime, workspace, workspace_mode,
                execution_path, continuation_identity, github_repo
            ) DO UPDATE SET
                summary=excluded.summary, session_id='', input_tokens=0,
                num_turns=0, updated_at=excluded.updated_at
            """,
            (
                scope,
                agent,
                thread_key,
                runtime,
                workspace,
                workspace_mode,
                canonical_execution_path,
                continuation_identity,
                github_repo,
                summary,
                time.time(),
            ),
        )

    def delete_thread(
        self, agent: str, thread_key: str, *, scope: str = ""
    ) -> None:
        """!reset: forget the thread entirely (session, summary, stats)."""
        self._exec(
            "DELETE FROM thread_state WHERE scope=? AND agent=? AND thread_key=?",
            (scope, agent, thread_key),
        )

    def clear_sessions(
        self,
        agent: str,
        *,
        scope: str = "",
        github_repo: str | None = None,
    ) -> None:
        """Session restart / runtime or workspace switch: drop resume ids and stats,
        keep summaries and last_seen (same semantics as SlackAgent.restart_sessions)."""
        repo_clause = ""
        params: tuple[Any, ...] = (time.time(), scope, agent)
        if github_repo is not None:
            repo_clause = " AND github_repo=?"
            params += (github_repo,)
        self._exec(
            "UPDATE thread_state SET session_id='', input_tokens=0, num_turns=0,"
            " updated_at=? WHERE scope=? AND agent=?" + repo_clause,
            params,
        )

    def sweep(self, ttl_seconds: float) -> int:
        """Delete rows (all agents) idle past ttl; returns count removed (0 on error)."""
        removed = self._exec(
            "DELETE FROM thread_state WHERE updated_at < ?",
            (time.time() - ttl_seconds,),
        )
        return max(removed, 0)

    def load_transcript_thread(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        limit: int,
        max_record_bytes: int,
    ) -> dict[str, Any] | None:
        """Load one bounded persisted transcript, including tombstones."""
        if self._conn is None:
            return None
        try:
            meta = self._conn.execute(
                "SELECT * FROM transcript_threads "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (team_id, channel_id, thread_ts),
            ).fetchone()
            if meta is None:
                return None
            record_limit = max(1, int(max_record_bytes))
            rows = self._conn.execute(
                "SELECT team_id, channel_id, thread_ts, message_ts, "
                "user_id, bot_id, subtype, "
                "substr(text, 1, ?) AS text, "
                "CASE WHEN length(CAST(blocks_json AS BLOB)) > ? "
                "THEN '[]' ELSE blocks_json END AS blocks_json, "
                "CASE WHEN length(CAST(attachments_json AS BLOB)) > ? "
                "THEN '[]' ELSE attachments_json END AS attachments_json, "
                "edited_ts, revision_ts, tombstone, source_rank, "
                "payload_bytes, content_truncated, updated_at, "
                "length(CAST(text AS BLOB)) AS text_bytes, "
                "length(CAST(blocks_json AS BLOB)) AS blocks_bytes, "
                "length(CAST(attachments_json AS BLOB)) "
                "AS attachments_bytes "
                "FROM transcript_messages "
                "WHERE team_id=? AND channel_id=? AND thread_ts=? "
                "ORDER BY CAST(message_ts AS REAL) DESC, message_ts DESC "
                "LIMIT ?",
                (
                    record_limit,
                    record_limit,
                    record_limit,
                    team_id,
                    channel_id,
                    thread_ts,
                    max(1, int(limit)),
                ),
            ).fetchall()
        except sqlite3.Error:
            logger.warning(
                "state store transcript load failed team=%s channel=%s",
                team_id,
                channel_id,
                exc_info=True,
            )
            return None

        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            storage_truncated = any(
                int(row[name] or 0) > record_limit
                for name in (
                    "text_bytes",
                    "blocks_bytes",
                    "attachments_bytes",
                )
            )
            try:
                blocks = json.loads(str(row["blocks_json"]) or "[]")
            except (TypeError, ValueError):
                blocks = []
                storage_truncated = True
            try:
                attachments = json.loads(
                    str(row["attachments_json"]) or "[]"
                )
            except (TypeError, ValueError):
                attachments = []
                storage_truncated = True
            messages.append(
                {
                    "ts": str(row["message_ts"]),
                    "thread_ts": thread_ts,
                    "user": str(row["user_id"]),
                    "bot_id": str(row["bot_id"]),
                    "subtype": str(row["subtype"]),
                    "text": str(row["text"]),
                    "blocks": blocks if isinstance(blocks, list) else [],
                    "attachments": (
                        attachments
                        if isinstance(attachments, list)
                        else []
                    ),
                    "edited_ts": str(row["edited_ts"]),
                    "revision_ts": str(row["revision_ts"]),
                    "tombstone": bool(row["tombstone"]),
                    "source_rank": int(row["source_rank"]),
                    "payload_bytes": int(row["payload_bytes"]),
                    "content_truncated": bool(
                        row["content_truncated"]
                    )
                    or storage_truncated,
                    "_storage_needs_rewrite": storage_truncated,
                }
            )
        return {
            "complete": bool(meta["complete"]),
            "from_root": bool(meta["from_root"]),
            "truncated_before_ts": str(
                meta["truncated_before_ts"]
            ),
            "last_message_ts": str(meta["last_message_ts"]),
            "hydrated_at": float(meta["hydrated_at"]),
            "retry_after": float(meta["retry_after"]),
            "failure_count": int(meta["failure_count"]),
            "validated_boot_id": str(meta["validated_boot_id"]),
            "authoritative": bool(meta["authoritative"]),
            "verified_through_ts": str(
                meta["verified_through_ts"]
            ),
            "authority_truncated": bool(
                meta["authority_truncated"]
            ),
            "updated_at": float(meta["updated_at"]),
            "messages": messages,
        }

    def save_transcript_record(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        record: dict[str, Any],
        meta: dict[str, Any],
        max_messages: int,
        max_threads: int,
        max_bytes: int | None = None,
    ) -> str:
        """Persist one revision plus metadata; return the DB trim boundary."""
        if self._conn is None:
            return str(meta.get("truncated_before_ts") or "")
        now = time.time()
        try:
            self._conn.execute(
                """
                INSERT INTO transcript_threads (
                    team_id, channel_id, thread_ts, complete, from_root,
                    truncated_before_ts, last_message_ts, hydrated_at,
                    retry_after, failure_count, validated_boot_id,
                    authoritative, verified_through_ts,
                    authority_truncated, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
                    complete=excluded.complete,
                    from_root=excluded.from_root,
                    truncated_before_ts=excluded.truncated_before_ts,
                    last_message_ts=excluded.last_message_ts,
                    hydrated_at=excluded.hydrated_at,
                    retry_after=excluded.retry_after,
                    failure_count=excluded.failure_count,
                    validated_boot_id=excluded.validated_boot_id,
                    authoritative=excluded.authoritative,
                    verified_through_ts=excluded.verified_through_ts,
                    authority_truncated=excluded.authority_truncated,
                    updated_at=excluded.updated_at
                """,
                (
                    team_id,
                    channel_id,
                    thread_ts,
                    int(bool(meta.get("complete"))),
                    int(bool(meta.get("from_root"))),
                    str(meta.get("truncated_before_ts") or ""),
                    str(meta.get("last_message_ts") or ""),
                    float(meta.get("hydrated_at") or 0),
                    float(meta.get("retry_after") or 0),
                    int(meta.get("failure_count") or 0),
                    str(meta.get("validated_boot_id") or ""),
                    int(bool(meta.get("authoritative"))),
                    str(meta.get("verified_through_ts") or ""),
                    int(bool(meta.get("authority_truncated"))),
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO transcript_messages (
                    team_id, channel_id, thread_ts, message_ts, user_id,
                    bot_id, subtype, text, blocks_json, attachments_json,
                    edited_ts, revision_ts, tombstone, source_rank, updated_at
                    , payload_bytes, content_truncated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    team_id, channel_id, thread_ts, message_ts
                ) DO UPDATE SET
                    user_id=excluded.user_id,
                    bot_id=excluded.bot_id,
                    subtype=excluded.subtype,
                    text=excluded.text,
                    blocks_json=excluded.blocks_json,
                    attachments_json=excluded.attachments_json,
                    edited_ts=excluded.edited_ts,
                    revision_ts=excluded.revision_ts,
                    tombstone=excluded.tombstone,
                    source_rank=excluded.source_rank,
                    payload_bytes=excluded.payload_bytes,
                    content_truncated=excluded.content_truncated,
                    updated_at=excluded.updated_at
                WHERE
                    CAST(excluded.revision_ts AS REAL) >
                    CAST(transcript_messages.revision_ts AS REAL)
                    OR (
                        excluded.revision_ts =
                        transcript_messages.revision_ts
                        AND excluded.source_rank >=
                            transcript_messages.source_rank
                    )
                """,
                (
                    team_id,
                    channel_id,
                    thread_ts,
                    str(record.get("ts") or ""),
                    str(record.get("user") or ""),
                    str(record.get("bot_id") or ""),
                    str(record.get("subtype") or ""),
                    str(record.get("text") or ""),
                    json.dumps(
                        record.get("blocks")
                        if isinstance(record.get("blocks"), list)
                        else [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        record.get("attachments")
                        if isinstance(record.get("attachments"), list)
                        else [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(record.get("edited_ts") or ""),
                    str(record.get("revision_ts") or ""),
                    int(bool(record.get("tombstone"))),
                    int(record.get("source_rank") or 0),
                    now,
                    int(record.get("payload_bytes") or 0),
                    int(bool(record.get("content_truncated"))),
                ),
            )
            trim_count = self._conn.execute(
                "SELECT MAX(COUNT(*) - ?, 0) FROM transcript_messages "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                (
                    max(1, int(max_messages)),
                    team_id,
                    channel_id,
                    thread_ts,
                ),
            ).fetchone()[0]
            trimmed_before = str(
                meta.get("truncated_before_ts") or ""
            )
            if int(trim_count or 0) > 0:
                trimmed = self._conn.execute(
                    "SELECT message_ts FROM transcript_messages "
                    "WHERE team_id=? AND channel_id=? AND thread_ts=? "
                    "ORDER BY CAST(message_ts AS REAL), message_ts LIMIT ?",
                    (
                        team_id,
                        channel_id,
                        thread_ts,
                        int(trim_count),
                    ),
                ).fetchall()
                if trimmed:
                    trimmed_before = str(trimmed[-1]["message_ts"])
                    self._conn.executemany(
                        "DELETE FROM transcript_messages "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=? "
                        "AND message_ts=?",
                        [
                            (
                                team_id,
                                channel_id,
                                thread_ts,
                                str(row["message_ts"]),
                            )
                            for row in trimmed
                        ],
                    )
                    self._conn.execute(
                        "UPDATE transcript_threads "
                        "SET truncated_before_ts=? "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                        (
                            trimmed_before,
                            team_id,
                            channel_id,
                            thread_ts,
                        ),
                    )
            self._prune_transcript_threads_locked(
                max_threads, max_bytes=max_bytes
            )
            self._conn.commit()
            self._harden_file_perms()
            return trimmed_before
        except (sqlite3.Error, TypeError, ValueError):
            self._conn.rollback()
            logger.warning(
                "state store transcript write failed team=%s channel=%s",
                team_id,
                channel_id,
                exc_info=True,
            )
            return str(meta.get("truncated_before_ts") or "")

    def save_transcript_meta(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        meta: dict[str, Any],
        max_threads: int,
        max_bytes: int | None = None,
    ) -> None:
        """Persist hydration/completeness metadata without a message revision."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """
                INSERT INTO transcript_threads (
                    team_id, channel_id, thread_ts, complete, from_root,
                    truncated_before_ts, last_message_ts, hydrated_at,
                    retry_after, failure_count, validated_boot_id,
                    authoritative, verified_through_ts,
                    authority_truncated, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, channel_id, thread_ts) DO UPDATE SET
                    complete=excluded.complete,
                    from_root=excluded.from_root,
                    truncated_before_ts=excluded.truncated_before_ts,
                    last_message_ts=excluded.last_message_ts,
                    hydrated_at=excluded.hydrated_at,
                    retry_after=excluded.retry_after,
                    failure_count=excluded.failure_count,
                    validated_boot_id=excluded.validated_boot_id,
                    authoritative=excluded.authoritative,
                    verified_through_ts=excluded.verified_through_ts,
                    authority_truncated=excluded.authority_truncated,
                    updated_at=excluded.updated_at
                """,
                (
                    team_id,
                    channel_id,
                    thread_ts,
                    int(bool(meta.get("complete"))),
                    int(bool(meta.get("from_root"))),
                    str(meta.get("truncated_before_ts") or ""),
                    str(meta.get("last_message_ts") or ""),
                    float(meta.get("hydrated_at") or 0),
                    float(meta.get("retry_after") or 0),
                    int(meta.get("failure_count") or 0),
                    str(meta.get("validated_boot_id") or ""),
                    int(bool(meta.get("authoritative"))),
                    str(meta.get("verified_through_ts") or ""),
                    int(bool(meta.get("authority_truncated"))),
                    time.time(),
                ),
            )
            if bool(meta.get("complete")):
                message_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM transcript_messages "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                        (team_id, channel_id, thread_ts),
                    ).fetchone()[0]
                )
                if message_count == 0:
                    # Byte-LRU may have evicted an oversized thread during
                    # its own backfill. Never recreate an empty row carrying
                    # a same-boot authoritative marker.
                    self._conn.execute(
                        "DELETE FROM transcript_threads "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                        (team_id, channel_id, thread_ts),
                    )
                    self._conn.commit()
                    return
            self._prune_transcript_threads_locked(
                max_threads, max_bytes=max_bytes
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning(
                "state store transcript metadata write failed",
                exc_info=True,
            )

    def rewrite_transcript_record(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        record: dict[str, Any],
        max_threads: int,
        max_bytes: int,
    ) -> None:
        """Force one loaded legacy/untrusted row to its bounded form."""
        if self._conn is None:
            return
        try:
            self._conn.execute(
                """
                UPDATE transcript_messages SET
                    user_id=?, bot_id=?, subtype=?, text=?,
                    blocks_json=?, attachments_json=?, edited_ts=?,
                    revision_ts=?, tombstone=?, source_rank=?,
                    payload_bytes=?, content_truncated=?, updated_at=?
                WHERE team_id=? AND channel_id=? AND thread_ts=?
                    AND message_ts=?
                """,
                (
                    str(record.get("user") or ""),
                    str(record.get("bot_id") or ""),
                    str(record.get("subtype") or ""),
                    str(record.get("text") or ""),
                    json.dumps(
                        record.get("blocks")
                        if isinstance(record.get("blocks"), list)
                        else [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        record.get("attachments")
                        if isinstance(record.get("attachments"), list)
                        else [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(record.get("edited_ts") or ""),
                    str(record.get("revision_ts") or ""),
                    int(bool(record.get("tombstone"))),
                    int(record.get("source_rank") or 0),
                    int(record.get("payload_bytes") or 0),
                    int(bool(record.get("content_truncated"))),
                    time.time(),
                    team_id,
                    channel_id,
                    thread_ts,
                    str(record.get("ts") or ""),
                ),
            )
            self._prune_transcript_threads_locked(
                max_threads, max_bytes=max_bytes
            )
            self._conn.commit()
            self._harden_file_perms()
        except (sqlite3.Error, TypeError, ValueError):
            self._conn.rollback()
            logger.warning(
                "state store transcript repair failed",
                exc_info=True,
            )

    def finalize_transcript_hydration(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        *,
        meta: dict[str, Any],
        required_records: list[dict[str, Any]],
        root_ts: str,
        expected_eviction_epoch: int,
    ) -> bool:
        """Atomically publish authority only for intact persisted coverage."""
        if self._conn is None:
            return True
        key = (str(team_id), str(channel_id), str(thread_ts))

        def cleanup() -> None:
            assert self._conn is not None
            self._conn.execute(
                "DELETE FROM transcript_messages "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            )
            self._conn.execute(
                "DELETE FROM transcript_threads "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            )
            self._note_transcript_eviction(key)

        try:
            self._conn.execute(
                "SAVEPOINT finalize_transcript_hydration"
            )
            if self.transcript_eviction_epoch(*key) != int(
                expected_eviction_epoch
            ):
                cleanup()
                self._conn.execute(
                    "RELEASE finalize_transcript_hydration"
                )
                self._conn.commit()
                return False
            thread_exists = self._conn.execute(
                "SELECT 1 FROM transcript_threads "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            ).fetchone()
            rows = self._conn.execute(
                "SELECT message_ts, text, blocks_json, attachments_json, "
                "revision_ts, source_rank, tombstone, payload_bytes, "
                "content_truncated FROM transcript_messages "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            ).fetchall()
            stored = {
                str(row["message_ts"]): row for row in rows
            }
            coverage_ok = bool(
                thread_exists is not None and str(root_ts) in stored
            )
            for record in required_records:
                message_ts = str(record.get("ts") or "")
                row = stored.get(message_ts)
                if row is None:
                    coverage_ok = False
                    break
                expected_blocks = json.dumps(
                    record.get("blocks")
                    if isinstance(record.get("blocks"), list)
                    else [],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                expected_attachments = json.dumps(
                    record.get("attachments")
                    if isinstance(record.get("attachments"), list)
                    else [],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if (
                    str(row["revision_ts"])
                    != str(record.get("revision_ts") or "")
                    or int(row["source_rank"])
                    != int(record.get("source_rank") or 0)
                    or bool(row["tombstone"])
                    != bool(record.get("tombstone"))
                    or str(row["text"])
                    != str(record.get("text") or "")
                    or str(row["blocks_json"]) != expected_blocks
                    or str(row["attachments_json"])
                    != expected_attachments
                    or int(row["payload_bytes"])
                    != int(record.get("payload_bytes") or 0)
                    or bool(row["content_truncated"])
                    != bool(record.get("content_truncated"))
                ):
                    coverage_ok = False
                    break
            if not coverage_ok:
                cleanup()
                self._conn.execute(
                    "RELEASE finalize_transcript_hydration"
                )
                self._conn.commit()
                return False
            cursor = self._conn.execute(
                """
                UPDATE transcript_threads SET
                    complete=?, from_root=?, truncated_before_ts=?,
                    last_message_ts=?, hydrated_at=?, retry_after=?,
                    failure_count=?, validated_boot_id=?,
                    authoritative=?, verified_through_ts=?,
                    authority_truncated=?, updated_at=?
                WHERE team_id=? AND channel_id=? AND thread_ts=?
                """,
                (
                    int(bool(meta.get("complete"))),
                    int(bool(meta.get("from_root"))),
                    str(meta.get("truncated_before_ts") or ""),
                    str(meta.get("last_message_ts") or ""),
                    float(meta.get("hydrated_at") or 0),
                    float(meta.get("retry_after") or 0),
                    int(meta.get("failure_count") or 0),
                    str(meta.get("validated_boot_id") or ""),
                    int(bool(meta.get("authoritative"))),
                    str(meta.get("verified_through_ts") or ""),
                    int(bool(meta.get("authority_truncated"))),
                    time.time(),
                    *key,
                ),
            )
            if cursor.rowcount != 1:
                cleanup()
                self._conn.execute(
                    "RELEASE finalize_transcript_hydration"
                )
                self._conn.commit()
                return False
            self._conn.execute("RELEASE finalize_transcript_hydration")
            self._conn.commit()
            self._harden_file_perms()
            return True
        except (sqlite3.Error, TypeError, ValueError):
            try:
                self._conn.execute(
                    "ROLLBACK TO finalize_transcript_hydration"
                )
                self._conn.execute(
                    "RELEASE finalize_transcript_hydration"
                )
            except sqlite3.Error:
                self._conn.rollback()
            logger.warning(
                "state store transcript finalize failed",
                exc_info=True,
            )
            return False

    def _prune_transcript_threads_locked(
        self, max_threads: int, *, max_bytes: int | None = None
    ) -> int:
        """Prune transcript tables by LRU count and logical payload bytes."""
        if self._conn is None:
            return 0
        limit = max(1, int(max_threads))
        stale = self._conn.execute(
            "SELECT team_id, channel_id, thread_ts "
            "FROM transcript_threads ORDER BY updated_at DESC "
            "LIMIT -1 OFFSET ?",
            (limit,),
        ).fetchall()
        for row in stale:
            key = (
                str(row["team_id"]),
                str(row["channel_id"]),
                str(row["thread_ts"]),
            )
            self._conn.execute(
                "DELETE FROM transcript_messages "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            )
            self._conn.execute(
                "DELETE FROM transcript_threads "
                "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                key,
            )
            self._note_transcript_eviction(key)
        removed = len(stale)

        if max_bytes is not None:
            byte_limit = max(1, int(max_bytes))
            logical_bytes = int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(payload_bytes), 0) "
                    "FROM transcript_messages"
                ).fetchone()[0]
                or 0
            )
            if logical_bytes > byte_limit:
                oldest = self._conn.execute(
                    "SELECT t.team_id, t.channel_id, t.thread_ts, "
                    "COALESCE(SUM(m.payload_bytes), 0) AS thread_bytes "
                    "FROM transcript_threads AS t "
                    "LEFT JOIN transcript_messages AS m "
                    "ON m.team_id=t.team_id "
                    "AND m.channel_id=t.channel_id "
                    "AND m.thread_ts=t.thread_ts "
                    "GROUP BY t.team_id, t.channel_id, t.thread_ts, "
                    "t.updated_at ORDER BY t.updated_at ASC"
                ).fetchall()
                for row in oldest:
                    if logical_bytes <= byte_limit:
                        break
                    key = (
                        str(row["team_id"]),
                        str(row["channel_id"]),
                        str(row["thread_ts"]),
                    )
                    self._conn.execute(
                        "DELETE FROM transcript_messages "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                        key,
                    )
                    self._conn.execute(
                        "DELETE FROM transcript_threads "
                        "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                        key,
                    )
                    logical_bytes -= max(
                        0, int(row["thread_bytes"] or 0)
                    )
                    removed += 1
                    self._note_transcript_eviction(
                        key, byte_eviction=True
                    )
        return removed

    def sweep_transcripts(
        self,
        ttl_seconds: float,
        *,
        max_threads: int,
        max_bytes: int | None = None,
        now: float | None = None,
    ) -> dict[str, int]:
        """Age/size-bound transcript tables without touching agent state."""
        if self._conn is None:
            return {"threads": 0, "messages": 0, "bytes": 0}
        cutoff = (time.time() if now is None else float(now)) - ttl_seconds
        try:
            stale = self._conn.execute(
                "SELECT team_id, channel_id, thread_ts "
                "FROM transcript_threads WHERE updated_at < ?",
                (cutoff,),
            ).fetchall()
            removed_messages = 0
            for row in stale:
                key = (
                    str(row["team_id"]),
                    str(row["channel_id"]),
                    str(row["thread_ts"]),
                )
                cursor = self._conn.execute(
                    "DELETE FROM transcript_messages "
                    "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                    key,
                )
                removed_messages += max(cursor.rowcount, 0)
                self._conn.execute(
                    "DELETE FROM transcript_threads "
                    "WHERE team_id=? AND channel_id=? AND thread_ts=?",
                    key,
                )
                self._note_transcript_eviction(key)
            size_removed = self._prune_transcript_threads_locked(
                max_threads, max_bytes=max_bytes
            )
            self._conn.commit()
            return {
                "threads": len(stale) + size_removed,
                "messages": removed_messages,
                "bytes": self.transcript_counts()["bytes"],
            }
        except sqlite3.Error:
            self._conn.rollback()
            logger.warning("state store transcript sweep failed", exc_info=True)
            return {"threads": 0, "messages": 0, "bytes": 0}

    def transcript_counts(self) -> dict[str, int]:
        """Sanitized persistence counts for monitoring/tests."""
        if self._conn is None:
            return {
                "threads": 0,
                "messages": 0,
                "bytes": 0,
                "evicted_threads": self._transcript_evicted_threads,
                "byte_evicted_threads": (
                    self._transcript_byte_evicted_threads
                ),
            }
        try:
            threads = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM transcript_threads"
                ).fetchone()[0]
            )
            messages = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM transcript_messages"
                ).fetchone()[0]
            )
            payload_bytes = int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(payload_bytes), 0) "
                    "FROM transcript_messages"
                ).fetchone()[0]
                or 0
            )
            return {
                "threads": threads,
                "messages": messages,
                "bytes": payload_bytes,
                "evicted_threads": self._transcript_evicted_threads,
                "byte_evicted_threads": (
                    self._transcript_byte_evicted_threads
                ),
            }
        except sqlite3.Error:
            return {
                "threads": 0,
                "messages": 0,
                "bytes": 0,
                "evicted_threads": self._transcript_evicted_threads,
                "byte_evicted_threads": (
                    self._transcript_byte_evicted_threads
                ),
            }

    def load_agent(
        self,
        agent: str,
        *,
        runtime: str,
        workspace: str,
        workspace_mode: str | None = None,
        execution_path: str | None = None,
        continuation_identity: str | None = None,
        identity_for_thread: (
            Callable[[str], tuple[str, str, str]] | None
        ) = None,
        github_repo: str = "",
        ttl_seconds: float,
        scope: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Restorable rows for one agent, TTL-filtered (expired rows are deleted).

        Continuations require an exact runtime, configured workspace, workspace
        mode, canonical execution cwd and runtime-specific identity. A caller
        may supply a per-thread resolver for deterministic worktree paths.
        Mismatches retain summary/last-seen metadata but clear session and stats.

        Restored rows have their updated_at refreshed: SlackAgent.restore_state
        gives them a full idle TTL in memory, and without this the DB sweep would
        keep counting from the old timestamp and delete a row restored just under
        the TTL while the process still considers that thread live.
        """
        if self._conn is None:
            return {}
        now = time.time()
        cutoff = now - ttl_seconds
        try:
            rows = self._conn.execute(
                "SELECT * FROM thread_state "
                "WHERE scope=? AND agent=? AND github_repo=?"
                " AND updated_at >= ? "
                "ORDER BY thread_key, updated_at DESC",
                (
                    scope,
                    agent,
                    github_repo,
                    cutoff,
                ),
            ).fetchall()
        except sqlite3.Error:
            logger.warning(
                "state store load failed for %s", agent, exc_info=True
            )
            return {}
        self._exec(
            "DELETE FROM thread_state "
            "WHERE scope=? AND agent=? AND github_repo=? AND updated_at < ?",
            (scope, agent, github_repo, cutoff),
        )
        # Keep the DB TTL clock in step with the in-memory one (see docstring).
        # Runs after the delete above, so it only touches the rows just returned.
        self._exec(
            "UPDATE thread_state SET updated_at=?"
            " WHERE scope=? AND agent=? AND github_repo=?",
            (now, scope, agent, github_repo),
        )

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["thread_key"]), []).append(row)

        default_execution_path = (
            os.path.realpath(
                os.path.abspath(os.path.expanduser(execution_path))
            )
            if execution_path is not None
            else None
        )
        result: dict[str, dict[str, Any]] = {}
        stale_keys: list[str] = []
        for thread_key, candidates in grouped.items():
            try:
                expected_mode, expected_path, expected_continuation = (
                    identity_for_thread(thread_key)
                    if identity_for_thread is not None
                    else (
                        workspace_mode,
                        default_execution_path,
                        continuation_identity,
                    )
                )
                if expected_path is not None:
                    expected_path = os.path.realpath(
                        os.path.abspath(os.path.expanduser(expected_path))
                    )
            except Exception:
                expected_mode = "<invalid-workspace-mode>"
                expected_path = "<invalid-execution-path>"
                expected_continuation = (
                    "<invalid-continuation-identity>"
                )
            exact = [
                row
                for row in candidates
                if (
                    str(row["runtime"]) == runtime
                    and str(row["workspace"]) == workspace
                    and (
                        expected_mode is None
                        or str(row["workspace_mode"]) == expected_mode
                    )
                    and (
                        expected_path is None
                        or str(row["execution_path"]) == expected_path
                    )
                    and (
                        expected_continuation is None
                        or str(row["continuation_identity"])
                        == expected_continuation
                    )
                )
            ]
            row = exact[0] if exact else candidates[0]
            session_id = row["session_id"]
            input_tokens = int(row["input_tokens"])
            num_turns = int(row["num_turns"])
            if not exact:
                stale_keys.append(thread_key)
                session_id = ""
                input_tokens = 0
                num_turns = 0
            result[thread_key] = {
                "session_id": session_id,
                "summary": row["summary"],
                "input_tokens": input_tokens,
                "num_turns": num_turns,
                "last_seen_ts": row["last_seen_ts"],
            }
        for key in stale_keys:
            self._exec(
                "UPDATE thread_state SET session_id='', input_tokens=0,"
                " num_turns=0 WHERE scope=? AND agent=? AND thread_key=?"
                " AND github_repo=?",
                (scope, agent, key, github_repo),
            )
        return result

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self._release_lock()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
