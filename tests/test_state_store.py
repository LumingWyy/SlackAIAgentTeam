"""Unit tests for state_store.StateStore (thread-state persistence)."""

from __future__ import annotations

import sqlite3

import pytest

from state_store import (
    LEGACY_UNKNOWN_CONTINUATION_IDENTITY,
    LEGACY_UNKNOWN_EXECUTION_PATH,
    LEGACY_UNKNOWN_GITHUB_REPO,
    LEGACY_UNKNOWN_WORKSPACE_MODE,
    StateStore,
)


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.db"))


def _save(store, agent="a", thread="C1:1.0", **over):
    kwargs = dict(
        session_id="sess-1",
        runtime="claude",
        workspace="/ws/a",
        summary="",
        input_tokens=1200,
        num_turns=3,
        last_seen_ts="1.0",
    )
    kwargs.update(over)
    store.save_turn(agent, thread, **kwargs)


def test_save_and_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    _save(store, summary="did things")

    rows = store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert rows == {
        "C1:1.0": {
            "session_id": "sess-1",
            "summary": "did things",
            "input_tokens": 1200,
            "num_turns": 3,
            "last_seen_ts": "1.0",
        }
    }


def test_removed_worktree_rehome_is_status_and_path_cas(tmp_path):
    store = _store(tmp_path)
    digest = "a" * 64
    old_path = str(tmp_path / "old" / f"sat-t-{digest}")
    new_path = str(tmp_path / "new" / f"sat-t-{digest}")
    mapping = {
        "identity_digest": digest,
        "repo_digest": "b" * 64,
        "common_dir": str(tmp_path / "repo" / ".git"),
        "base_workspace": str(tmp_path / "repo"),
        "team_id": "T1",
        "channel_id": "C1",
        "root_thread_ts": "1.0",
        "owner": "U01ALICE",
        "path": old_path,
        "branch": f"slack-agent-wt/{digest}",
        "base_ref": "main",
        "base_oid": "c" * 40,
        "status": "ready",
    }
    assert store.save_worktree_mapping(**mapping)

    assert not store.rehome_removed_worktree_mapping(
        identity_digest=digest,
        old_path=old_path,
        new_path=new_path,
    )
    assert store.load_worktree_mapping(digest)["path"] == old_path

    mapping["status"] = "removed"
    assert store.save_worktree_mapping(**mapping)
    assert not store.rehome_removed_worktree_mapping(
        identity_digest=digest,
        old_path=old_path + "-stale",
        new_path=new_path,
    )
    assert store.rehome_removed_worktree_mapping(
        identity_digest=digest,
        old_path=old_path,
        new_path=new_path,
    )
    rehomed = store.load_worktree_mapping(digest)
    assert rehomed["path"] == new_path
    assert rehomed["status"] == "removed"
    assert not store.rehome_removed_worktree_mapping(
        identity_digest=digest,
        old_path=old_path,
        new_path=str(tmp_path / "third" / f"sat-t-{digest}"),
    )
    store.close()


def test_worktree_removing_and_removed_transitions_survive_reopen(tmp_path):
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))
    digest = "d" * 64
    path = str(tmp_path / "worktrees" / f"sat-t-{digest}")
    head = "e" * 40
    assert store.save_worktree_mapping(
        identity_digest=digest,
        repo_digest="f" * 64,
        common_dir=str(tmp_path / "repo" / ".git"),
        base_workspace=str(tmp_path / "repo"),
        team_id="T1",
        channel_id="C1",
        root_thread_ts="1.0",
        owner="U01ALICE",
        path=path,
        branch=f"slack-agent-wt/{digest}",
        base_ref="main",
        base_oid="a" * 40,
        status="ready",
    )
    assert not store.begin_worktree_removal(
        identity_digest=digest,
        expected_path=path + "-stale",
        expected_status="ready",
        retained_head_oid=head,
    )
    assert store.begin_worktree_removal(
        identity_digest=digest,
        expected_path=path,
        expected_status="ready",
        retained_head_oid=head,
    )
    store.close()

    reopened = StateStore(str(db_path))
    mapping = reopened.load_worktree_mapping(digest)
    assert mapping["status"] == "removing"
    assert mapping["retained_head_oid"] == head
    assert not reopened.mark_worktree_removed(
        identity_digest=digest,
        expected_path=path,
        expected_status="ready",
        retained_head_oid="b" * 40,
    )
    assert reopened.mark_worktree_removed(
        identity_digest=digest,
        expected_path=path,
        expected_status="removing",
        retained_head_oid=head,
    )
    reopened.close()

    finalized = StateStore(str(db_path))
    mapping = finalized.load_worktree_mapping(digest)
    assert mapping["status"] == "removed"
    assert mapping["retained_head_oid"] == head
    finalized.close()


def test_worktree_schema_adds_legacy_retained_head_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE worktree_mappings (
            identity_digest TEXT PRIMARY KEY,
            repo_digest TEXT NOT NULL,
            common_dir TEXT NOT NULL,
            base_workspace TEXT NOT NULL,
            team_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            root_thread_ts TEXT NOT NULL,
            owner TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL UNIQUE,
            base_ref TEXT NOT NULL,
            base_oid TEXT NOT NULL,
            status TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            last_used_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO worktree_mappings VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            "a" * 64,
            "b" * 64,
            str(tmp_path / "repo" / ".git"),
            str(tmp_path / "repo"),
            "T1",
            "C1",
            "1.0",
            "U01ALICE",
            str(tmp_path / "old-worktree"),
            "slack-agent-wt/" + ("a" * 64),
            "main",
            "c" * 40,
            "removed",
            "",
            1.0,
            1.0,
            1.0,
        ),
    )
    conn.commit()
    conn.close()

    store = StateStore(str(db_path))
    columns = {
        row["name"]
        for row in store._conn.execute(
            "PRAGMA table_info(worktree_mappings)"
        ).fetchall()
    }
    assert "retained_head_oid" in columns
    mapping = store.load_worktree_mapping("a" * 64)
    assert mapping["retained_head_oid"] == ""
    store.close()


def test_new_schema_explicit_no_repo_row_is_restorable(tmp_path):
    """Only a row written by the repo-aware schema may use the no-repo identity."""
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    _save(store, session_id="explicit-no-repo")
    stored_repo = store._conn.execute(
        "SELECT github_repo FROM thread_state"
    ).fetchone()[0]
    store.close()

    assert stored_repo == ""
    rows = StateStore(path).load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        github_repo="",
        ttl_seconds=3600,
    )
    assert rows["C1:1.0"]["session_id"] == "explicit-no-repo"


def test_load_survives_reopen(tmp_path):
    """A second StateStore on the same file sees the first one's rows (restart)."""
    path = str(tmp_path / "state.db")
    StateStore(path).save_turn(
        "a",
        "C1:1.0",
        session_id="sess-1",
        runtime="claude",
        workspace="/ws/a",
        summary="s",
        input_tokens=1,
        num_turns=1,
        last_seen_ts="1.0",
    )
    rows = StateStore(path).load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert rows["C1:1.0"]["session_id"] == "sess-1"


def test_same_state_db_is_exclusively_owned_until_close(tmp_path):
    from state_store import StateStoreLockError

    path = str(tmp_path / "state.db")
    first = StateStore(path)
    assert first.enabled

    with pytest.raises(StateStoreLockError, match="already in use"):
        StateStore(path)

    first.close()
    reopened = StateStore(path)
    assert reopened.enabled
    reopened.close()


def test_different_state_db_paths_can_run_together(tmp_path):
    alice = StateStore(str(tmp_path / "alice.db"))
    bob = StateStore(str(tmp_path / "bob.db"))
    assert alice.enabled and bob.enabled
    alice.close()
    bob.close()


def test_state_db_path_is_canonical_and_symlink_alias_conflicts(tmp_path):
    import os

    real = tmp_path / "state.db"
    alias = tmp_path / "state-link.db"
    first = StateStore(str(real))
    alias.symlink_to(real)

    assert first.path == os.path.realpath(real)
    with pytest.raises(
        __import__("state_store").StateStoreLockError,
        match="already in use",
    ):
        StateStore(str(alias))

    first.close()
    reopened = StateStore(str(alias))
    assert reopened.path == os.path.realpath(real)
    reopened.close()


def test_state_db_relative_parent_alias_canonicalizes_to_same_path(
    tmp_path, monkeypatch
):
    import os

    subdir = tmp_path / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(tmp_path)
    first = StateStore("state.db")

    with pytest.raises(
        __import__("state_store").StateStoreLockError,
        match="already in use",
    ):
        StateStore("subdir/../state.db")
    assert first.path == os.path.realpath(tmp_path / "state.db")
    first.close()


def test_state_db_hardlink_alias_conflicts_on_actual_database_inode(tmp_path):
    import os

    real = tmp_path / "state.db"
    alias = tmp_path / "state-hardlink.db"
    first = StateStore(str(real))
    os.link(real, alias)

    with pytest.raises(
        __import__("state_store").StateStoreLockError,
        match="already in use",
    ):
        StateStore(str(alias))

    first.close()
    reopened = StateStore(str(alias))
    assert reopened.enabled
    reopened.close()


def test_database_inode_lock_is_held_before_sqlite_connect(
    tmp_path, monkeypatch
):
    import os
    import sqlite3
    import state_store as state_store_module

    path = str(tmp_path / "state.db")
    real_connect = sqlite3.connect
    checked = []

    def connect_after_lock(candidate, *args, **kwargs):
        stat_result = os.stat(candidate)
        inode_key = (int(stat_result.st_dev), int(stat_result.st_ino))
        assert inode_key in state_store_module._LOCKED_DB_INODES
        checked.append(os.path.realpath(candidate))
        return real_connect(candidate, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_after_lock)
    store = StateStore(path)

    assert store.enabled
    assert checked == [os.path.realpath(path)]
    store.close()


def test_missing_ofd_lock_support_disables_store_before_sqlite_connect(
    tmp_path, monkeypatch
):
    import fcntl
    import sqlite3

    connect_calls = []
    monkeypatch.delattr(fcntl, "F_OFD_SETLK")
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: connect_calls.append(True),
    )

    store = StateStore(str(tmp_path / "state.db"))

    assert store.enabled is False
    assert connect_calls == []


def test_hardlink_inode_lock_blocks_fresh_subprocess(tmp_path):
    import os
    import subprocess
    import sys

    real = tmp_path / "state.db"
    alias = tmp_path / "state-hardlink.db"
    first = StateStore(str(real))
    os.link(real, alias)
    script = """
import sys
from state_store import StateStore, StateStoreLockError
try:
    StateStore(sys.argv[1])
except StateStoreLockError:
    raise SystemExit(0)
raise SystemExit(7)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(alias)],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    first.close()


@pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"),
    reason="requires os.fork",
)
def test_hardlink_inode_lock_blocks_forked_child(tmp_path):
    import os

    real = tmp_path / "state.db"
    alias = tmp_path / "state-hardlink.db"
    first = StateStore(str(real))
    os.link(real, alias)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions happen in parent
        os.close(read_fd)
        try:
            StateStore(str(alias))
        except __import__("state_store").StateStoreLockError:
            os.write(write_fd, b"locked")
        except BaseException:
            os.write(write_fd, b"error")
        else:
            os.write(write_fd, b"opened")
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        outcome = os.read(read_fd, 16)
        _pid, status = os.waitpid(child, 0)
    finally:
        os.close(read_fd)
        first.close()
    assert os.waitstatus_to_exitcode(status) == 0
    assert outcome == b"locked"


@pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"),
    reason="requires os.fork",
)
@pytest.mark.parametrize(
    "child_action",
    ["close", "gc", "context_manager", "constructor_failure"],
)
def test_fork_child_lifecycle_cannot_release_parent_inode_lock(
    tmp_path, child_action
):
    import gc
    import os
    import subprocess
    import sys

    real = tmp_path / "state.db"
    alias = tmp_path / "state-hardlink.db"
    first = StateStore(str(real))
    os.link(real, alias)
    status_read, status_write = os.pipe()
    release_read, release_write = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions happen in parent
        os.close(status_read)
        os.close(release_write)
        outcome = b"done"
        try:
            if child_action == "close":
                first.close()
            elif child_action == "gc":
                del first
                gc.collect()
            elif child_action == "context_manager":
                with first:
                    pass
            else:
                with pytest.raises(
                    __import__("state_store").StateStoreLockError
                ):
                    StateStore(str(alias))
        except BaseException:
            outcome = b"error"
        os.write(status_write, outcome)
        os.read(release_read, 1)
        os.close(status_write)
        os.close(release_read)
        os._exit(0)

    os.close(status_write)
    os.close(release_read)
    script = """
import sys
from state_store import StateStore, StateStoreLockError
try:
    StateStore(sys.argv[1])
except StateStoreLockError:
    raise SystemExit(0)
raise SystemExit(7)
"""
    try:
        try:
            outcome = os.read(status_read, 5)
            assert outcome == b"done"
            probe = subprocess.run(
                [sys.executable, "-c", script, str(alias)],
                cwd=str(
                    __import__("pathlib").Path(__file__).resolve().parents[1]
                ),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert probe.returncode == 0, probe.stderr
        finally:
            os.write(release_write, b"x")
            os.close(release_write)
            os.close(status_read)
            _pid, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0

        # The child's final os._exit closes every inherited descriptor. The
        # parent's independent descriptors must still retain both locks.
        after_exit = subprocess.run(
            [sys.executable, "-c", script, str(alias)],
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert after_exit.returncode == 0, after_exit.stderr
    finally:
        first.close()

    reopened = StateStore(str(alias))
    assert reopened.enabled
    reopened.close()


@pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"),
    reason="requires os.fork",
)
def test_fork_child_starts_with_empty_inode_registry(tmp_path):
    import os
    import state_store as state_store_module

    first = StateStore(str(tmp_path / "state.db"))
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions happen in parent
        os.close(read_fd)
        clean = not state_store_module._LOCKED_DB_INODES
        os.write(write_fd, b"clean" if clean else b"dirty")
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        outcome = os.read(read_fd, 5)
        _pid, status = os.waitpid(child, 0)
    finally:
        os.close(read_fd)
        first.close()
    assert os.waitstatus_to_exitcode(status) == 0
    assert outcome == b"clean"


def test_state_lock_file_is_private(tmp_path):
    import os

    path = str(tmp_path / "state.db")
    store = StateStore(path)
    assert (os.stat(f"{path}.lock").st_mode & 0o777) == 0o600
    store.close()


def test_state_open_failure_releases_lifetime_lock(tmp_path, monkeypatch):
    import sqlite3

    path = str(tmp_path / "state.db")
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("injected open failure")
        ),
    )
    disabled = StateStore(path)
    assert disabled.enabled is False

    monkeypatch.setattr(sqlite3, "connect", real_connect)
    reopened = StateStore(path)
    assert reopened.enabled
    reopened.close()


def test_workspace_scope_prevents_cross_team_restore(tmp_path):
    store = _store(tmp_path)
    store.save_turn(
        "a",
        "C1:1.0",
        session_id="team-one-session",
        runtime="claude",
        workspace="/ws/a",
        summary="private team context",
        input_tokens=1,
        num_turns=1,
        last_seen_ts="1.0",
        scope="T-ONE",
    )

    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        ttl_seconds=3600,
        scope="T-TWO",
    ) == {}
    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        ttl_seconds=3600,
        scope="T-ONE",
    )["C1:1.0"]["session_id"] == "team-one-session"


def test_github_repo_isolates_same_agent_thread(tmp_path):
    """The same local agent/thread must never resume another repo's state."""
    store = _store(tmp_path)
    _save(
        store,
        session_id="repo-a-session",
        summary="repo a private summary",
        github_repo="acme/repo-a",
    )
    _save(
        store,
        session_id="repo-b-session",
        summary="repo b private summary",
        github_repo="acme/repo-b",
    )

    repo_a = store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        github_repo="acme/repo-a",
        ttl_seconds=3600,
    )
    repo_b = store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        github_repo="acme/repo-b",
        ttl_seconds=3600,
    )

    assert repo_a["C1:1.0"]["session_id"] == "repo-a-session"
    assert repo_a["C1:1.0"]["summary"] == "repo a private summary"
    assert repo_b["C1:1.0"]["session_id"] == "repo-b-session"
    assert repo_b["C1:1.0"]["summary"] == "repo b private summary"


def test_v2_schema_migrates_repo_identity_without_losing_rows(tmp_path):
    """All legacy scopes survive, but their unknown repo can never be restored."""
    import sqlite3

    path = str(tmp_path / "v2.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE thread_state (
            scope TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL,
            thread_key TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
            runtime TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            num_turns INTEGER NOT NULL DEFAULT 0,
            last_seen_ts TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
            PRIMARY KEY (scope, agent, thread_key)
        )
        """
    )
    rows = [
        ("T-ONE", "a", "C1:1", "sess-a", "claude", "/ws/a", "sum-a", 1, 1, "1", 9999999999),
        ("T-TWO", "b", "C2:2", "sess-b", "codex", "/ws/b", "sum-b", 2, 2, "2", 9999999999),
    ]
    conn.executemany(
        "INSERT INTO thread_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    store = StateStore(path)
    columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(thread_state)").fetchall()
    }
    assert "github_repo" in columns
    migrated = store._conn.execute(
        "SELECT scope, agent, session_id, summary, github_repo "
        "FROM thread_state ORDER BY scope"
    ).fetchall()
    assert [
        tuple(row) for row in migrated
    ] == [
        (
            "T-ONE",
            "a",
            "sess-a",
            "sum-a",
            LEGACY_UNKNOWN_GITHUB_REPO,
        ),
        (
            "T-TWO",
            "b",
            "sess-b",
            "sum-b",
            LEGACY_UNKNOWN_GITHUB_REPO,
        ),
    ]
    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        github_repo="",
        ttl_seconds=3600,
        scope="T-ONE",
    ) == {}
    assert store.load_agent(
        "b",
        runtime="codex",
        workspace="/ws/b",
        github_repo="",
        ttl_seconds=3600,
        scope="T-TWO",
    ) == {}
    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        github_repo="acme/new-repo",
        ttl_seconds=3600,
        scope="T-ONE",
    ) == {}
    store._conn.execute("UPDATE thread_state SET updated_at=0")
    store._conn.commit()
    assert store.sweep(3600) == 2
    assert store._conn.execute(
        "SELECT COUNT(*) FROM thread_state"
    ).fetchone()[0] == 0


def test_legacy_schema_migrates_without_claiming_a_team_scope(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE thread_state (
            agent TEXT NOT NULL, thread_key TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '', runtime TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            num_turns INTEGER NOT NULL DEFAULT 0,
            last_seen_ts TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
            PRIMARY KEY (agent, thread_key)
        )
        """
    )
    conn.execute(
        "INSERT INTO thread_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("a", "C:1", "legacy", "claude", "/ws/a", "summary", 1, 1, "1", 9999999999),
    )
    conn.commit()
    conn.close()

    store = StateStore(path)
    columns = {
        row["name"]
        for row in store._conn.execute("PRAGMA table_info(thread_state)").fetchall()
    }
    assert "scope" in columns
    migrated = store._conn.execute(
        "SELECT scope, session_id, summary, github_repo FROM thread_state"
    ).fetchone()
    assert tuple(migrated) == (
        "",
        "legacy",
        "summary",
        LEGACY_UNKNOWN_GITHUB_REPO,
    )
    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        ttl_seconds=3600,
        scope="T-NEW",
    ) == {}
    assert store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        ttl_seconds=3600,
    ) == {}


def test_runtime_or_workspace_mismatch_drops_session_keeps_summary(tmp_path):
    store = _store(tmp_path)
    _save(store, summary="handoff")

    rows = store.load_agent(
        "a", runtime="codex", workspace="/ws/a", ttl_seconds=3600
    )
    row = rows["C1:1.0"]
    assert row["session_id"] == ""  # engine changed -> resume id unusable
    assert row["input_tokens"] == 0 and row["num_turns"] == 0
    assert row["summary"] == "handoff"  # summary still hands over

    # the drop is persisted: a matching reload no longer sees the session either
    rows2 = store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert rows2["C1:1.0"]["session_id"] == ""

    store.close()
    store2 = _store(tmp_path)
    _save(store2, thread="C2:2.0")
    rows3 = store2.load_agent(
        "a", runtime="claude", workspace="/ws/other", ttl_seconds=3600
    )
    assert rows3["C2:2.0"]["session_id"] == ""  # cwd changed -> same treatment


def test_exact_execution_and_continuation_identity_isolated_by_agent_base_repo(
    tmp_path,
):
    store = _store(tmp_path)
    store.save_turn(
        "a",
        "C1:1.0",
        session_id="exact",
        runtime="codex",
        workspace="/base/a",
        workspace_mode="thread_worktree",
        execution_path="/managed/thread-a",
        continuation_identity="codex-v1:read-only",
        github_repo="acme/repo-a",
        summary="safe handoff",
        input_tokens=4,
        num_turns=1,
        last_seen_ts="1.0",
    )

    exact = store.load_agent(
        "a",
        runtime="codex",
        workspace="/base/a",
        workspace_mode="thread_worktree",
        execution_path="/managed/thread-a",
        continuation_identity="codex-v1:read-only",
        github_repo="acme/repo-a",
        ttl_seconds=3600,
    )
    assert exact["C1:1.0"]["session_id"] == "exact"
    assert store.load_agent(
        "b",
        runtime="codex",
        workspace="/base/a",
        workspace_mode="thread_worktree",
        execution_path="/managed/thread-a",
        continuation_identity="codex-v1:read-only",
        github_repo="acme/repo-a",
        ttl_seconds=3600,
    ) == {}
    assert store.load_agent(
        "a",
        runtime="codex",
        workspace="/base/b",
        workspace_mode="thread_worktree",
        execution_path="/managed/thread-a",
        continuation_identity="codex-v1:read-only",
        github_repo="acme/repo-a",
        ttl_seconds=3600,
    )["C1:1.0"]["session_id"] == ""
    assert store.load_agent(
        "a",
        runtime="codex",
        workspace="/base/a",
        workspace_mode="thread_worktree",
        execution_path="/managed/thread-a",
        continuation_identity="codex-v1:read-only",
        github_repo="acme/repo-b",
        ttl_seconds=3600,
    ) == {}


def test_pre_identity_schema_migration_never_restores_legacy_session(
    tmp_path,
):
    path = str(tmp_path / "pre-identity.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE thread_state (
            scope TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL,
            thread_key TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            runtime TEXT NOT NULL DEFAULT '',
            workspace TEXT NOT NULL DEFAULT '',
            github_repo TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            num_turns INTEGER NOT NULL DEFAULT 0,
            last_seen_ts TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            PRIMARY KEY (
                scope, agent, thread_key, runtime, workspace, github_repo
            )
        )
        """
    )
    conn.execute(
        "INSERT INTO thread_state VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "T1",
            "a",
            "C1:1.0",
            "legacy-session",
            "claude",
            "/ws/a",
            "acme/repo",
            "safe handoff",
            5,
            1,
            "1.0",
            9999999999.0,
        ),
    )
    conn.commit()
    conn.close()

    store = StateStore(path)
    migrated = store._conn.execute(
        "SELECT workspace_mode, execution_path, "
        "continuation_identity FROM thread_state"
    ).fetchone()
    assert tuple(migrated) == (
        LEGACY_UNKNOWN_WORKSPACE_MODE,
        LEGACY_UNKNOWN_EXECUTION_PATH,
        LEGACY_UNKNOWN_CONTINUATION_IDENTITY,
    )
    restored = store.load_agent(
        "a",
        runtime="claude",
        workspace="/ws/a",
        workspace_mode="serial",
        execution_path="/ws/a",
        continuation_identity="claude-v1",
        github_repo="acme/repo",
        ttl_seconds=3600,
        scope="T1",
    )
    assert restored["C1:1.0"]["session_id"] == ""
    assert restored["C1:1.0"]["input_tokens"] == 0
    assert restored["C1:1.0"]["summary"] == "safe handoff"
    store.close()


def test_mismatch_clears_stats_even_without_session_id(tmp_path):
    """Empty session_id must not preserve stale token stats across runtime change."""
    store = _store(tmp_path)
    _save(
        store,
        session_id="",
        runtime="claude",
        input_tokens=99,
        num_turns=3,
        summary="keep",
    )
    rows = store.load_agent(
        "a", runtime="codex", workspace="/ws/a", ttl_seconds=3600
    )
    row = rows["C1:1.0"]
    assert row["session_id"] == ""
    assert row["input_tokens"] == 0 and row["num_turns"] == 0
    assert row["summary"] == "keep"


def test_state_db_file_permissions(tmp_path):
    """Parent dir 0700; DB (and WAL if present) 0600."""
    import os
    from pathlib import Path

    db_dir = tmp_path / "data"
    store = StateStore(str(db_dir / "state.db"))
    assert store.enabled
    assert (os.stat(db_dir).st_mode & 0o777) == 0o700
    assert (os.stat(db_dir / "state.db").st_mode & 0o777) == 0o600
    # WAL may or may not exist yet; if it does, it must be private
    for name in ("state.db-wal", "state.db-shm"):
        p = db_dir / name
        if p.exists():
            assert (os.stat(p).st_mode & 0o777) == 0o600


def test_ttl_expiry_removes_rows(tmp_path):
    store = _store(tmp_path)
    _save(store)
    # age the row directly
    store._conn.execute("UPDATE thread_state SET updated_at = updated_at - 100000")
    store._conn.commit()

    rows = store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert rows == {}
    # expired row was deleted, not just filtered
    n = store._conn.execute("SELECT COUNT(*) FROM thread_state").fetchone()[0]
    assert n == 0


def test_set_summary_clears_session_keeps_summary(tmp_path):
    store = _store(tmp_path)
    _save(store)
    store.set_summary("a", "C1:1.0", "rolled over")

    rows = store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    row = rows["C1:1.0"]
    assert row["session_id"] == ""
    assert row["summary"] == "rolled over"
    assert row["input_tokens"] == 0 and row["num_turns"] == 0


def test_clear_sessions_keeps_summaries(tmp_path):
    store = _store(tmp_path)
    _save(store, thread="C1:1.0", summary="s1")
    _save(store, thread="C2:2.0", summary="s2")
    _save(store, agent="b", thread="C3:3.0", summary="other-agent")

    store.clear_sessions("a")

    rows = store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert all(r["session_id"] == "" for r in rows.values())
    assert {r["summary"] for r in rows.values()} == {"s1", "s2"}
    other = store.load_agent(
        "b", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    assert other["C3:3.0"]["session_id"] == "sess-1"  # untouched


def test_delete_thread(tmp_path):
    store = _store(tmp_path)
    _save(store)
    store.delete_thread("a", "C1:1.0")
    assert (
        store.load_agent("a", runtime="claude", workspace="/ws/a", ttl_seconds=3600)
        == {}
    )


def test_sweep_deletes_stale_rows_across_agents(tmp_path):
    store = _store(tmp_path)
    _save(store, agent="a", thread="C1:1.0")
    _save(store, agent="b", thread="C2:2.0")
    store._conn.execute(
        "UPDATE thread_state SET updated_at = updated_at - 100000 WHERE agent='a'"
    )
    store._conn.commit()

    assert store.sweep(3600) == 1
    assert (
        store.load_agent("a", runtime="claude", workspace="/ws/a", ttl_seconds=3600)
        == {}
    )
    assert store.load_agent(
        "b", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )


def test_disabled_store_degrades_to_noops(tmp_path):
    """Unopenable path -> enabled False; every method is a safe no-op."""
    # File where a directory is required: mkdir fails → store disabled
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    store = StateStore(str(blocker / "state.db"))
    assert store.enabled is False
    _save(store)  # must not raise
    store.set_summary("a", "C1:1.0", "s")
    store.clear_sessions("a")
    store.delete_thread("a", "C1:1.0")
    assert store.sweep(1) == 0
    assert (
        store.load_agent("a", runtime="claude", workspace="/ws/a", ttl_seconds=1)
        == {}
    )
    store.close()


def test_existing_parent_dir_permissions_are_left_alone(tmp_path):
    """STATE_DB in a shared dir (repo root, $HOME) must not narrow that dir's mode."""
    import os

    db_dir = tmp_path / "existing"
    db_dir.mkdir()
    os.chmod(db_dir, 0o755)

    store = StateStore(str(db_dir / "state.db"))
    assert store.enabled
    assert (os.stat(db_dir).st_mode & 0o777) == 0o755  # untouched
    # the DB itself is still private, which is what actually protects the content
    assert (os.stat(db_dir / "state.db").st_mode & 0o777) == 0o600
    store.close()


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    """~ in STATE_DB opens under $HOME instead of failing on a literal '~'."""
    import os

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(home), 1))

    store = StateStore("~/state.db")
    assert store.enabled
    assert (home / "state.db").exists()
    assert not (tmp_path / "~").exists()  # no literal '~' directory anywhere
    _save(store)
    assert store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    store.close()


def test_load_refreshes_updated_at(tmp_path):
    """Restored rows get a fresh TTL clock, matching SlackAgent.restore_state."""
    import time

    store = _store(tmp_path)
    _save(store)
    # age the row to just inside the TTL
    store._conn.execute("UPDATE thread_state SET updated_at = updated_at - 3500")
    store._conn.commit()

    assert store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )
    age = time.time() - store._conn.execute(
        "SELECT updated_at FROM thread_state"
    ).fetchone()[0]
    assert age < 5  # refreshed, not still 3500s old

    # so a sweep right after a restore no longer drops what memory still holds
    assert store.sweep(3600) == 0
    assert store.load_agent(
        "a", runtime="claude", workspace="/ws/a", ttl_seconds=3600
    )


def test_load_does_not_refresh_other_agents(tmp_path):
    """Refresh is scoped to the agent being restored."""
    store = _store(tmp_path)
    _save(store, agent="a")
    _save(store, agent="b", thread="C2:2.0")
    store._conn.execute("UPDATE thread_state SET updated_at = updated_at - 3500")
    store._conn.commit()

    store.load_agent("a", runtime="claude", workspace="/ws/a", ttl_seconds=3600)
    stale = store._conn.execute(
        "SELECT COUNT(*) FROM thread_state WHERE agent='b' AND updated_at < ?",
        (__import__("time").time() - 3000,),
    ).fetchone()[0]
    assert stale == 1
