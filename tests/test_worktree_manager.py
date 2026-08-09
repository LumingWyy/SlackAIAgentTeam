"""Real-Git tests for deterministic per-thread worktrees."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
import subprocess
from pathlib import Path

import pytest

from state_store import StateStore
from worktree_manager import (
    WorktreeError,
    WorktreeManager,
    WorktreePlan,
    discover_repo_spec,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.name", "Worktree Test", cwd=repo)
    _git("config", "user.email", "worktree@example.invalid", cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _spec(
    tmp_path: Path,
    repo: Path,
    *,
    max_per_repo: int = 4,
):
    return discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(tmp_path / "worktrees"),
        base_ref="main",
        max_per_repo=max_per_repo,
    )


def _plan(
    manager: WorktreeManager,
    spec,
    *,
    thread_ts: str = "1710000000.000001",
) -> WorktreePlan:
    return manager.plan(
        spec,
        team_id="T0001",
        channel_id="C0001",
        root_thread_ts=thread_ts,
    )


def _process_ensure(
    repo: str,
    root: str,
    db_path: str,
    start_event,
    result_queue,
) -> None:
    """Spawn-safe worker for the cross-process creation test."""
    try:
        spec = discover_repo_spec(
            workspace=repo,
            worktree_root=root,
            base_ref="main",
            max_per_repo=4,
        )
        store = StateStore(db_path)
        manager = WorktreeManager(store)
        plan = manager.plan(
            spec,
            team_id="T0001",
            channel_id="C0001",
            root_thread_ts="process-race",
        )
        start_event.wait(10)
        record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
        result_queue.put(("ok", record.path, record.branch))
        store.close()
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _process_ensure_at_root(
    repo: str,
    root: str,
    db_path: str,
    thread_ts: str,
    start_event,
    result_queue,
) -> None:
    """Spawn-safe worker for cross-root common-dir lock/cap tests."""
    try:
        spec = discover_repo_spec(
            workspace=repo,
            worktree_root=root,
            base_ref="main",
            max_per_repo=1,
        )
        store = StateStore(db_path)
        manager = WorktreeManager(store)
        plan = manager.plan(
            spec,
            team_id="T0001",
            channel_id="C0001",
            root_thread_ts=thread_ts,
        )
        start_event.wait(10)
        record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
        result_queue.put(("ok", record.path))
        store.close()
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _process_hold_lease(
    repo: str,
    root: str,
    db_path: str,
    start_method: str,
    ready_event,
    release_event,
    result_queue,
) -> None:
    """Hold the shared runtime lease so another process must reject removal."""
    try:
        spec = discover_repo_spec(
            workspace=repo,
            worktree_root=root,
            base_ref="main",
            max_per_repo=4,
        )
        store = StateStore(db_path)
        manager = WorktreeManager(store)
        plan = manager.plan(
            spec,
            team_id="T0001",
            channel_id="C0001",
            root_thread_ts=f"lease-{start_method}",
        )

        async def hold() -> None:
            async with manager.lease(plan, owner="U01ALICE"):
                ready_event.set()
                await asyncio.to_thread(release_event.wait, 15)

        asyncio.run(hold())
        result_queue.put(("ok",))
        store.close()
    except BaseException as exc:  # pragma: no cover - surfaced in parent
        result_queue.put(("error", type(exc).__name__, str(exc)))


def test_discover_repo_spec_rejects_non_git_and_nested_root(tmp_path):
    non_git = tmp_path / "not-git"
    non_git.mkdir()

    with pytest.raises(WorktreeError, match="Git repository"):
        discover_repo_spec(
            workspace=str(non_git),
            worktree_root=str(tmp_path / "worktrees"),
            base_ref="main",
            max_per_repo=2,
        )

    repo = _repo(tmp_path)
    with pytest.raises(WorktreeError, match="disjoint"):
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(repo / ".agent-worktrees"),
            base_ref="main",
            max_per_repo=2,
        )


def test_discover_repo_spec_rejects_symlink_root_component(tmp_path):
    repo = _repo(tmp_path)
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    alias = tmp_path / "root-alias"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(WorktreeError, match="symlink"):
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(alias / "children"),
            base_ref="main",
            max_per_repo=2,
        )


@pytest.mark.parametrize(
    ("base_ref", "maximum", "message"),
    [
        ("-main", 2, "base_ref"),
        ("main\nother", 2, "base_ref"),
        ("missing", 2, "resolve"),
        ("main", 0, "max_per_repo"),
        ("main", True, "max_per_repo"),
        ("main", 257, "max_per_repo"),
    ],
)
def test_discover_repo_spec_strict_base_ref_and_cap(
    tmp_path, base_ref, maximum, message
):
    repo = _repo(tmp_path)
    with pytest.raises(WorktreeError, match=message):
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees"),
            base_ref=base_ref,
            max_per_repo=maximum,
        )


def test_plan_is_stable_shared_and_contains_no_slack_text(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = _spec(tmp_path, repo)
    hostile = "../../pwn $(touch nope); スレッド / user@example.com"

    first = manager.plan(
        spec,
        team_id="T/../../alice",
        channel_id="C;rm -rf",
        root_thread_ts=hostile,
    )
    second = manager.plan(
        spec,
        team_id="T/../../alice",
        channel_id="C;rm -rf",
        root_thread_ts=hostile,
    )
    other = manager.plan(
        spec,
        team_id="T/../../alice",
        channel_id="C;rm -rf",
        root_thread_ts=hostile + "-other",
    )

    assert first == second
    assert first.path != other.path
    assert first.branch != other.branch
    assert hostile not in first.path
    assert "alice" not in first.path
    assert re.fullmatch(
        r"slack-agent-wt/[0-9a-f]{64}", first.branch
    )
    assert re.fullmatch(r"sat-t-[0-9a-f]{64}", Path(first.path).name)
    store.close()


def test_create_uses_committed_base_and_persists_mapping(tmp_path):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty main\n", encoding="utf-8")
    (repo / "main-only.txt").write_text("untracked\n", encoding="utf-8")
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))

    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    worktree = Path(record.path)
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == "committed\n"
    assert not (worktree / "main-only.txt").exists()
    assert _git("branch", "--show-current", cwd=worktree) == plan.branch
    assert record.status == "ready"
    persisted = store.load_worktree_mapping(plan.identity_digest)
    assert persisted is not None
    assert persisted["path"] == plan.path
    assert persisted["branch"] == plan.branch
    assert persisted["owner"] == "U01ALICE"
    assert persisted["last_used_at"] > 0
    store.close()


def test_creation_is_pinned_to_preflight_base_oid(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = _spec(tmp_path, repo)
    original_oid = spec.base_oid
    (repo / "after-preflight.txt").write_text("later\n", encoding="utf-8")
    _git("add", "after-preflight.txt", cwd=repo)
    _git("commit", "-m", "advance main", cwd=repo)
    assert _git("rev-parse", "main", cwd=repo) != original_oid
    plan = _plan(manager, spec)

    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert _git("rev-parse", "HEAD", cwd=Path(record.path)) == original_oid
    assert not (Path(record.path) / "after-preflight.txt").exists()
    store.close()


def test_reuses_same_worktree_and_registry_after_restart(tmp_path):
    repo = _repo(tmp_path)
    db = tmp_path / "state.db"
    spec = _spec(tmp_path, repo)
    first_store = StateStore(str(db))
    first_manager = WorktreeManager(first_store)
    first_plan = _plan(first_manager, spec)
    first = asyncio.run(first_manager.ensure(first_plan, owner="U01ALICE"))
    marker = Path(first.path) / "kept-untracked.txt"
    marker.write_text("keep me\n", encoding="utf-8")
    first_store.close()

    second_store = StateStore(str(db))
    second_manager = WorktreeManager(second_store)
    second_spec = _spec(tmp_path, repo)
    second_plan = _plan(second_manager, second_spec)
    second = asyncio.run(
        second_manager.ensure(second_plan, owner="U01ALICE")
    )

    assert second.path == first.path
    assert marker.read_text(encoding="utf-8") == "keep me\n"
    worktree_list = _git("worktree", "list", "--porcelain", cwd=repo)
    assert len(worktree_list.split("worktree ")) == 3
    second_store.close()


def test_reuses_retained_branch_after_clean_path_removed(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    first = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    _git("worktree", "remove", first.path, cwd=repo)

    restored = asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert restored.path == first.path
    assert Path(restored.path).is_dir()
    assert _git("branch", "--show-current", cwd=Path(restored.path)) == plan.branch
    store.close()


def test_remove_clean_worktree_retains_branch_and_ensure_restores_it(
    tmp_path,
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(record.path))

    removed = asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert removed.status == "removed"
    assert removed.retained_head_oid == original_head
    assert not Path(record.path).exists()
    assert _git("show-ref", "--hash", f"refs/heads/{plan.branch}", cwd=repo) == (
        original_head
    )
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert mapping is not None
    assert mapping["status"] == "removed"
    assert mapping["retained_head_oid"] == original_head

    restored = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    assert restored.status == "ready"
    assert Path(restored.path).is_dir()
    assert _git("rev-parse", "HEAD", cwd=Path(restored.path)) == original_head
    store.close()


def test_restart_rehomes_removed_mapping_to_new_root_and_restores_branch(
    tmp_path,
):
    repo = _repo(tmp_path)
    db = tmp_path / "state.db"
    first_root = tmp_path / "worktrees-one"
    second_root = tmp_path / "worktrees-two"
    first_store = StateStore(str(db))
    first_manager = WorktreeManager(first_store)
    first_spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(first_root),
        base_ref="main",
        max_per_repo=4,
    )
    first_plan = _plan(first_manager, first_spec)
    first = asyncio.run(first_manager.ensure(first_plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(first.path))
    asyncio.run(first_manager.remove(first_plan, owner="U01ALICE"))
    first_store.close()

    second_store = StateStore(str(db))
    second_manager = WorktreeManager(second_store)
    second_spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(second_root),
        base_ref="main",
        max_per_repo=4,
    )
    second_plan = _plan(second_manager, second_spec)
    restored = asyncio.run(
        second_manager.ensure(second_plan, owner="U01ALICE")
    )

    assert first_plan.identity_digest == second_plan.identity_digest
    assert restored.path == second_plan.path
    assert Path(restored.path).is_dir()
    assert not Path(first.path).exists()
    assert _git("rev-parse", "HEAD", cwd=Path(restored.path)) == original_head
    mapping = second_store.load_worktree_mapping(second_plan.identity_digest)
    assert mapping is not None
    assert mapping["path"] == second_plan.path
    assert mapping["status"] == "ready"
    assert mapping["retained_head_oid"] == ""
    second_store.close()


@pytest.mark.parametrize("rehome", [False, True])
def test_removed_restore_rejects_branch_moved_from_retained_head(
    tmp_path, rehome
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    first_plan = _plan(
        manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-one"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    record = asyncio.run(manager.ensure(first_plan, owner="U01ALICE"))
    retained_head = _git("rev-parse", "HEAD", cwd=Path(record.path))
    asyncio.run(manager.remove(first_plan, owner="U01ALICE"))

    (repo / "alternate.txt").write_text("different\n", encoding="utf-8")
    _git("add", "alternate.txt", cwd=repo)
    _git("commit", "-m", "different retained head", cwd=repo)
    alternate_head = _git("rev-parse", "HEAD", cwd=repo)
    assert alternate_head != retained_head
    _git("branch", "-f", first_plan.branch, alternate_head, cwd=repo)

    target_plan = first_plan
    if rehome:
        target_plan = _plan(
            manager,
            discover_repo_spec(
                workspace=str(repo),
                worktree_root=str(tmp_path / "worktrees-two"),
                base_ref="main",
                max_per_repo=4,
            ),
        )
    with pytest.raises(WorktreeError, match="retained|HEAD|changed"):
        asyncio.run(manager.ensure(target_plan, owner="U01ALICE"))

    mapping = store.load_worktree_mapping(first_plan.identity_digest)
    assert mapping["status"] == "removed"
    assert mapping["path"] == first_plan.path
    assert mapping["retained_head_oid"] == retained_head
    assert not Path(target_plan.path).exists()
    store.close()


def test_legacy_removed_mapping_without_retained_head_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    _git("worktree", "remove", record.path, cwd=repo)
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert store.save_worktree_mapping(
        identity_digest=plan.identity_digest,
        repo_digest=plan.repo.repo_digest,
        common_dir=plan.repo.common_dir,
        base_workspace=plan.repo.base_workspace,
        team_id=plan.team_id,
        channel_id=plan.channel_id,
        root_thread_ts=plan.root_thread_ts,
        owner="U01ALICE",
        path=plan.path,
        branch=plan.branch,
        base_ref=plan.repo.base_ref,
        base_oid=mapping["base_oid"],
        status="removed",
        created_at=mapping["created_at"],
        last_used_at=mapping["last_used_at"],
    )

    with pytest.raises(WorktreeError, match="retained|legacy"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert not Path(plan.path).exists()
    assert store.load_worktree_mapping(plan.identity_digest)["status"] == (
        "removed"
    )
    store.close()


def test_removed_restore_detects_branch_move_between_compare_and_add(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    retained_head = _git("rev-parse", "HEAD", cwd=Path(record.path))
    asyncio.run(manager.remove(plan, owner="U01ALICE"))

    (repo / "race.txt").write_text("race\n", encoding="utf-8")
    _git("add", "race.txt", cwd=repo)
    _git("commit", "-m", "racing branch head", cwd=repo)
    racing_head = _git("rev-parse", "HEAD", cwd=repo)
    original_run_git = manager._run_git
    moved = False

    async def racing_run_git(
        *args: str,
        cwd: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        nonlocal moved
        if (
            not moved
            and len(args) >= 2
            and args[:2] == ("worktree", "add")
        ):
            moved = True
            _git("branch", "-f", plan.branch, racing_head, cwd=repo)
        return await original_run_git(
            *args,
            cwd=cwd,
            allowed_returncodes=allowed_returncodes,
        )

    monkeypatch.setattr(manager, "_run_git", racing_run_git)

    with pytest.raises(WorktreeError, match="retained|HEAD|changed"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert moved
    assert not Path(plan.path).exists()
    assert plan.path not in _git("worktree", "list", "--porcelain", cwd=repo)
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert mapping["status"] in {"removed", "error"}
    assert mapping["retained_head_oid"] == retained_head
    assert _git(
        "show-ref", "--hash", f"refs/heads/{plan.branch}", cwd=repo
    ) == racing_head
    store.close()


@pytest.mark.parametrize("status", ["ready", "creating"])
def test_restart_rejects_live_old_root_mapping_until_clean_remove(
    tmp_path, status
):
    repo = _repo(tmp_path)
    db = tmp_path / "state.db"
    first_store = StateStore(str(db))
    first_manager = WorktreeManager(first_store)
    first_spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(tmp_path / "worktrees-one"),
        base_ref="main",
        max_per_repo=4,
    )
    first_plan = _plan(first_manager, first_spec)
    if status == "ready":
        asyncio.run(first_manager.ensure(first_plan, owner="U01ALICE"))
    else:
        assert first_store.save_worktree_mapping(
            identity_digest=first_plan.identity_digest,
            repo_digest=first_plan.repo.repo_digest,
            common_dir=first_plan.repo.common_dir,
            base_workspace=first_plan.repo.base_workspace,
            team_id=first_plan.team_id,
            channel_id=first_plan.channel_id,
            root_thread_ts=first_plan.root_thread_ts,
            owner="U01ALICE",
            path=first_plan.path,
            branch=first_plan.branch,
            base_ref=first_plan.repo.base_ref,
            base_oid=first_plan.repo.base_oid,
            status="creating",
        )
    first_store.close()

    second_store = StateStore(str(db))
    second_manager = WorktreeManager(second_store)
    second_plan = _plan(
        second_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-two"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    with pytest.raises(WorktreeError, match="old worktree root|clean remove"):
        asyncio.run(second_manager.ensure(second_plan, owner="U01ALICE"))

    mapping = second_store.load_worktree_mapping(second_plan.identity_digest)
    assert mapping is not None
    assert mapping["path"] == first_plan.path
    assert mapping["status"] == status
    assert not Path(second_plan.path).exists()
    second_store.close()


def test_restart_does_not_rehome_removed_mapping_with_tampered_old_path(
    tmp_path,
):
    repo = _repo(tmp_path)
    db = tmp_path / "state.db"
    first_store = StateStore(str(db))
    first_manager = WorktreeManager(first_store)
    first_plan = _plan(
        first_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-one"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    record = asyncio.run(first_manager.ensure(first_plan, owner="U01ALICE"))
    asyncio.run(first_manager.remove(first_plan, owner="U01ALICE"))
    first_store.close()
    Path(record.path).symlink_to(tmp_path, target_is_directory=True)

    second_store = StateStore(str(db))
    second_manager = WorktreeManager(second_store)
    second_plan = _plan(
        second_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-two"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    with pytest.raises(WorktreeError, match="symlink|tamper"):
        asyncio.run(second_manager.ensure(second_plan, owner="U01ALICE"))

    mapping = second_store.load_worktree_mapping(second_plan.identity_digest)
    assert mapping is not None
    assert mapping["path"] == first_plan.path
    assert not Path(second_plan.path).exists()
    second_store.close()


@pytest.mark.parametrize("change_kind", ["tracked", "untracked"])
def test_remove_rejects_dirty_or_untracked_worktree(tmp_path, change_kind):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    worktree = Path(record.path)
    if change_kind == "tracked":
        (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    else:
        (worktree / "untracked.txt").write_text(
            "must survive\n", encoding="utf-8"
        )

    with pytest.raises(WorktreeError, match="dirty|untracked"):
        asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert worktree.is_dir()
    assert store.load_worktree_mapping(plan.identity_digest)["status"] == "ready"
    store.close()


def test_remove_rejects_clean_unpushed_commit(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    worktree = Path(record.path)
    (worktree / "committed.txt").write_text("local only\n", encoding="utf-8")
    _git("add", "committed.txt", cwd=worktree)
    _git("commit", "-m", "local only", cwd=worktree)

    with pytest.raises(WorktreeError, match="unpushed"):
        asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert worktree.is_dir()
    assert (worktree / "committed.txt").read_text(encoding="utf-8") == (
        "local only\n"
    )
    store.close()


def test_remove_accepts_pushed_commit_and_retains_branch(tmp_path):
    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    worktree = Path(record.path)
    (worktree / "pushed.txt").write_text("published\n", encoding="utf-8")
    _git("add", "pushed.txt", cwd=worktree)
    _git("commit", "-m", "published", cwd=worktree)
    pushed_head = _git("rev-parse", "HEAD", cwd=worktree)
    _git("push", "--set-upstream", "origin", plan.branch, cwd=worktree)

    removed = asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert not worktree.exists()
    assert removed.retained_head_oid == pushed_head
    assert _git("show-ref", "--hash", f"refs/heads/{plan.branch}", cwd=repo) == (
        pushed_head
    )
    restored = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    assert _git("rev-parse", "HEAD", cwd=Path(restored.path)) == pushed_head
    store.close()


def test_remove_postcheck_branch_move_persists_original_head_and_blocks_restore(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(record.path))

    (repo / "post-remove-race.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "post-remove-race.txt", cwd=repo)
    _git("commit", "-m", "post-remove branch target", cwd=repo)
    moved_head = _git("rev-parse", "HEAD", cwd=repo)
    assert moved_head != original_head
    original_run_git = manager._run_git
    raced = False

    async def move_branch_after_remove(
        *args: str,
        cwd: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        nonlocal raced
        result = await original_run_git(
            *args,
            cwd=cwd,
            allowed_returncodes=allowed_returncodes,
        )
        if (
            not raced
            and len(args) >= 3
            and args[:3] == ("worktree", "remove", plan.path)
        ):
            raced = True
            _git("branch", "-f", plan.branch, moved_head, cwd=repo)
        return result

    monkeypatch.setattr(manager, "_run_git", move_branch_after_remove)

    with pytest.raises(WorktreeError, match="changed during removal"):
        asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert raced
    assert not Path(plan.path).exists()
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert mapping["status"] == "removed"
    assert mapping["retained_head_oid"] == original_head
    assert _git(
        "show-ref", "--hash", f"refs/heads/{plan.branch}", cwd=repo
    ) == moved_head
    with pytest.raises(WorktreeError, match="retained|HEAD|changed"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    assert not Path(plan.path).exists()
    store.close()


def test_remove_postcheck_branch_move_reports_removed_cas_failure(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(record.path))

    (repo / "cas-race.txt").write_text("moved\n", encoding="utf-8")
    _git("add", "cas-race.txt", cwd=repo)
    _git("commit", "-m", "cas failure branch target", cwd=repo)
    moved_head = _git("rev-parse", "HEAD", cwd=repo)
    original_run_git = manager._run_git

    async def move_branch_after_remove(
        *args: str,
        cwd: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        result = await original_run_git(
            *args,
            cwd=cwd,
            allowed_returncodes=allowed_returncodes,
        )
        if (
            len(args) >= 3
            and args[:3] == ("worktree", "remove", plan.path)
        ):
            _git("branch", "-f", plan.branch, moved_head, cwd=repo)
        return result

    cas_calls: list[dict[str, str]] = []

    def fail_removed_cas(**kwargs: str) -> bool:
        cas_calls.append(dict(kwargs))
        return False

    monkeypatch.setattr(manager, "_run_git", move_branch_after_remove)
    monkeypatch.setattr(
        store, "mark_worktree_removed", fail_removed_cas
    )

    with pytest.raises(
        WorktreeError,
        match="cannot atomically persist.*after.*changed",
    ):
        asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert cas_calls == [
        {
            "identity_digest": plan.identity_digest,
            "expected_path": plan.path,
            "expected_status": "removing",
            "retained_head_oid": original_head,
        }
    ]
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert mapping["status"] == "removing"
    assert mapping["retained_head_oid"] == original_head
    assert not Path(plan.path).exists()
    assert _git(
        "show-ref", "--hash", f"refs/heads/{plan.branch}", cwd=repo
    ) == moved_head
    store.close()

    reopened = StateStore(str(tmp_path / "state.db"))
    restarted_manager = WorktreeManager(reopened)
    with pytest.raises(WorktreeError, match="retained|HEAD|changed"):
        asyncio.run(
            restarted_manager.ensure(plan, owner="U01ALICE")
        )
    restarted_mapping = reopened.load_worktree_mapping(
        plan.identity_digest
    )
    assert restarted_mapping["status"] == "removing"
    assert restarted_mapping["retained_head_oid"] == original_head
    assert not Path(plan.path).exists()
    reopened.close()


def test_remove_transition_cas_failure_does_not_call_git_remove(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(record.path))
    monkeypatch.setattr(
        store,
        "begin_worktree_removal",
        lambda **_kwargs: False,
        raising=False,
    )

    with pytest.raises(WorktreeError, match="transition|removing"):
        asyncio.run(manager.remove(plan, owner="U01ALICE"))

    assert Path(plan.path).is_dir()
    assert _git("rev-parse", "HEAD", cwd=Path(plan.path)) == original_head
    mapping = store.load_worktree_mapping(plan.identity_digest)
    assert mapping["status"] == "ready"
    assert mapping["retained_head_oid"] == ""
    store.close()


@pytest.mark.parametrize("git_removed", [False, True])
def test_removing_state_recovers_safely_after_process_reopen(
    tmp_path, git_removed
):
    repo = _repo(tmp_path)
    db_path = tmp_path / "state.db"
    store = StateStore(str(db_path))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    original_head = _git("rev-parse", "HEAD", cwd=Path(record.path))
    assert store.begin_worktree_removal(
        identity_digest=plan.identity_digest,
        expected_path=plan.path,
        expected_status="ready",
        retained_head_oid=original_head,
    )
    if git_removed:
        _git("worktree", "remove", plan.path, cwd=repo)
    store.close()

    reopened = StateStore(str(db_path))
    restarted_manager = WorktreeManager(reopened)
    restored = asyncio.run(
        restarted_manager.ensure(plan, owner="U01ALICE")
    )

    assert restored.status == "ready"
    assert Path(restored.path).is_dir()
    assert _git("rev-parse", "HEAD", cwd=Path(restored.path)) == (
        original_head
    )
    mapping = reopened.load_worktree_mapping(plan.identity_digest)
    assert mapping["status"] == "ready"
    assert mapping["retained_head_oid"] == ""
    reopened.close()


def test_remove_rejects_active_lease_and_tampered_branch(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = _spec(tmp_path, repo)
    active_plan = _plan(manager, spec, thread_ts="active")

    async def active_scenario() -> None:
        async with manager.lease(active_plan, owner="U01ALICE"):
            with pytest.raises(WorktreeError, match="active"):
                await manager.remove(active_plan, owner="U01ALICE")

    asyncio.run(active_scenario())
    assert Path(active_plan.path).is_dir()

    tampered_plan = _plan(manager, spec, thread_ts="tampered")
    record = asyncio.run(manager.ensure(tampered_plan, owner="U01ALICE"))
    _git("switch", "-c", "unexpected-remove-branch", cwd=Path(record.path))
    with pytest.raises(WorktreeError, match="branch|tamper"):
        asyncio.run(manager.remove(tampered_plan, owner="U01ALICE"))
    assert Path(record.path).is_dir()
    store.close()


@pytest.mark.parametrize(
    "start_method",
    [
        method
        for method in ("spawn", "fork")
        if method in multiprocessing.get_all_start_methods()
    ],
)
def test_remove_rejects_cross_process_active_lease(tmp_path, start_method):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "parent.db"))
    manager = WorktreeManager(store)
    plan = _plan(
        manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(root),
            base_ref="main",
            max_per_repo=4,
        ),
        thread_ts=f"lease-{start_method}",
    )
    asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    context = multiprocessing.get_context(start_method)
    ready_event = context.Event()
    release_event = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_process_hold_lease,
        args=(
            str(repo),
            str(root),
            str(tmp_path / f"child-{start_method}.db"),
            start_method,
            ready_event,
            release_event,
            result_queue,
        ),
    )
    process.start()
    try:
        assert ready_event.wait(20)
        with pytest.raises(WorktreeError, match="active"):
            asyncio.run(manager.remove(plan, owner="U01ALICE"))
        assert Path(plan.path).is_dir()
    finally:
        release_event.set()
        result = result_queue.get(timeout=20)
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert result == ("ok",)
    assert process.exitcode == 0
    store.close()


def test_same_process_concurrent_ensure_is_singleflight(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    original = manager._run_git
    add_calls = 0

    async def counted(*args, **kwargs):
        nonlocal add_calls
        if "worktree" in args and "add" in args:
            add_calls += 1
            await asyncio.sleep(0.02)
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "_run_git", counted)

    async def run_both():
        return await asyncio.gather(
            manager.ensure(plan, owner="U01ALICE"),
            manager.ensure(plan, owner="U01ALICE"),
        )

    records = asyncio.run(run_both())
    assert records[0].path == records[1].path
    assert add_calls == 1
    store.close()


def test_two_processes_create_one_deterministic_worktree(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_ensure,
            args=(
                str(repo),
                str(root),
                str(tmp_path / f"state-{index}.db"),
                start_event,
                result_queue,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert [result[0] for result in results] == ["ok", "ok"]
    assert len({result[1] for result in results}) == 1
    assert len({result[2] for result in results}) == 1
    worktree_paths = [
        line.removeprefix("worktree ")
        for line in _git(
            "worktree", "list", "--porcelain", cwd=repo
        ).splitlines()
        if line.startswith("worktree ")
    ]
    assert len(worktree_paths) == 2  # base + one managed worktree


def test_cap_fails_closed_without_creating_second_worktree(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = _spec(tmp_path, repo, max_per_repo=1)
    first = _plan(manager, spec, thread_ts="1.0")
    second = _plan(manager, spec, thread_ts="2.0")
    asyncio.run(manager.ensure(first, owner="U01ALICE"))

    with pytest.raises(WorktreeError, match="capacity"):
        asyncio.run(manager.ensure(second, owner="U01ALICE"))

    assert not Path(second.path).exists()
    missing_branch = subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            f"refs/heads/{second.branch}",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    assert missing_branch.returncode != 0
    store.close()


def test_common_dir_lock_and_cap_span_different_roots_and_state_dbs(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    first_store = StateStore(str(tmp_path / "first.db"))
    second_store = StateStore(str(tmp_path / "second.db"))
    first_manager = WorktreeManager(first_store)
    second_manager = WorktreeManager(second_store)
    first_plan = _plan(
        first_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-one"),
            base_ref="main",
            max_per_repo=1,
        ),
        thread_ts="root-one",
    )
    second_plan = _plan(
        second_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-two"),
            base_ref="main",
            max_per_repo=1,
        ),
        thread_ts="root-two",
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = first_manager._ensure_locked

    async def gated(plan, *, owner):
        entered.set()
        await release.wait()
        return await original(plan, owner=owner)

    monkeypatch.setattr(first_manager, "_ensure_locked", gated)

    async def scenario():
        first_task = asyncio.create_task(
            first_manager.ensure(first_plan, owner="U01ALICE")
        )
        await entered.wait()
        second_task = asyncio.create_task(
            second_manager.ensure(second_plan, owner="U02BOB")
        )
        await asyncio.sleep(0.08)
        assert not second_task.done()
        release.set()
        first = await first_task
        with pytest.raises(WorktreeError, match="capacity"):
            await second_task
        return first

    first = asyncio.run(scenario())
    assert Path(first.path).is_dir()
    assert not Path(second_plan.path).exists()
    managed = [
        line
        for line in _git(
            "worktree", "list", "--porcelain", cwd=repo
        ).splitlines()
        if line.startswith("branch refs/heads/slack-agent-wt/")
    ]
    assert len(managed) == 1
    first_store.close()
    second_store.close()


def test_spawn_processes_share_common_dir_cap_across_different_roots(
    tmp_path,
):
    repo = _repo(tmp_path)
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_ensure_at_root,
            args=(
                str(repo),
                str(tmp_path / f"worktrees-{index}"),
                str(tmp_path / f"state-{index}.db"),
                f"thread-{index}",
                start_event,
                result_queue,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    assert sorted(result[0] for result in results) == ["error", "ok"]
    error = next(result for result in results if result[0] == "error")
    assert error[1] == "WorktreeError"
    assert "capacity" in error[2]
    managed = [
        line
        for line in _git(
            "worktree", "list", "--porcelain", cwd=repo
        ).splitlines()
        if line.startswith("branch refs/heads/slack-agent-wt/")
    ]
    assert len(managed) == 1


def test_capacity_ignores_external_branch_but_rejects_tampered_prefix(
    tmp_path,
):
    repo = _repo(tmp_path)
    external = tmp_path / "external"
    _git("worktree", "add", "-b", "external-branch", str(external), cwd=repo)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(tmp_path / "worktrees"),
        base_ref="main",
        max_per_repo=1,
    )
    first_plan = _plan(manager, spec, thread_ts="managed-one")
    asyncio.run(manager.ensure(first_plan, owner="U01ALICE"))
    assert Path(first_plan.path).is_dir()

    _git("worktree", "remove", first_plan.path, cwd=repo)
    malformed = tmp_path / "malformed"
    _git(
        "worktree",
        "add",
        "-b",
        "slack-agent-wt/not-a-managed-digest",
        str(malformed),
        cwd=repo,
    )
    second_plan = _plan(manager, spec, thread_ts="managed-two")
    with pytest.raises(WorktreeError, match="managed branch|tamper"):
        asyncio.run(manager.ensure(second_plan, owner="U01ALICE"))
    assert not Path(second_plan.path).exists()
    store.close()


def test_cross_root_identity_lease_blocks_other_lease_and_remove(tmp_path):
    repo = _repo(tmp_path)
    first_store = StateStore(str(tmp_path / "first.db"))
    second_store = StateStore(str(tmp_path / "second.db"))
    first_manager = WorktreeManager(first_store)
    second_manager = WorktreeManager(second_store)
    first_plan = _plan(
        first_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-one"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    second_plan = _plan(
        second_manager,
        discover_repo_spec(
            workspace=str(repo),
            worktree_root=str(tmp_path / "worktrees-two"),
            base_ref="main",
            max_per_repo=4,
        ),
    )
    assert first_plan.identity_digest == second_plan.identity_digest

    async def scenario():
        async with first_manager.lease(first_plan, owner="U01ALICE"):
            second_lease = asyncio.create_task(
                second_manager.lease(
                    second_plan, owner="U01ALICE"
                ).__aenter__()
            )
            await asyncio.sleep(0.08)
            assert not second_lease.done()
            with pytest.raises(WorktreeError, match="active"):
                await second_manager.remove(
                    second_plan, owner="U01ALICE"
                )
            second_lease.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_lease

    asyncio.run(scenario())
    first_store.close()
    second_store.close()


def test_common_dir_control_directory_rejects_symlink_and_mode_tamper(
    tmp_path,
):
    repo = _repo(tmp_path)
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(tmp_path / "worktrees"),
        base_ref="main",
        max_per_repo=4,
    )
    control_dir = Path(spec.common_dir) / "slack-agent-team-worktree-control"
    control_dir.symlink_to(tmp_path, target_is_directory=True)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, spec)
    with pytest.raises(WorktreeError, match="symlink|control"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    control_dir.unlink()
    control_dir.mkdir(mode=0o700)
    control_dir.chmod(0o755)
    with pytest.raises(WorktreeError, match="mode|permission|tamper"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    assert not Path(plan.path).exists()
    store.close()


def test_existing_path_symlink_and_branch_tamper_fail_closed(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = _spec(tmp_path, repo)
    symlink_plan = _plan(manager, spec, thread_ts="symlink")
    outside = tmp_path / "outside"
    outside.mkdir()
    Path(symlink_plan.path).parent.mkdir(parents=True)
    os.symlink(outside, symlink_plan.path)

    with pytest.raises(WorktreeError, match="symlink|tamper"):
        asyncio.run(manager.ensure(symlink_plan, owner="U01ALICE"))

    Path(symlink_plan.path).unlink()
    branch_plan = _plan(manager, spec, thread_ts="branch")
    record = asyncio.run(manager.ensure(branch_plan, owner="U01ALICE"))
    _git("switch", "-c", "unexpected-branch", cwd=Path(record.path))

    with pytest.raises(WorktreeError, match="branch|tamper"):
        asyncio.run(manager.ensure(branch_plan, owner="U01ALICE"))
    store.close()


def test_branch_checked_out_at_another_path_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    _git("branch", plan.branch, "main", cwd=repo)
    elsewhere = tmp_path / "elsewhere"
    _git("worktree", "add", str(elsewhere), plan.branch, cwd=repo)

    with pytest.raises(WorktreeError, match="checked out"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    assert not Path(plan.path).exists()
    store.close()


def test_registry_path_collision_fails_before_git_add(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    assert store.save_worktree_mapping(
        identity_digest="f" * 64,
        repo_digest=plan.repo.repo_digest,
        common_dir=plan.repo.common_dir,
        base_workspace=plan.repo.base_workspace,
        team_id="T-other",
        channel_id="C-other",
        root_thread_ts="other",
        owner="U01ALICE",
        path=plan.path,
        branch="slack-agent-wt/" + "f" * 64,
        base_ref=plan.repo.base_ref,
        base_oid=plan.repo.base_oid,
        status="ready",
    )

    with pytest.raises(WorktreeError, match="persist"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert not Path(plan.path).exists()
    store.close()


def test_repo_moved_after_discovery_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    moved = tmp_path / "repo-moved"
    repo.rename(moved)

    with pytest.raises(WorktreeError, match="Git command failed"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))

    assert not Path(plan.path).exists()
    store.close()


def test_existing_git_file_symlink_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    plan = _plan(manager, _spec(tmp_path, repo))
    record = asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    git_file = Path(record.path) / ".git"
    original = git_file.read_text(encoding="utf-8")
    git_file.unlink()
    target = tmp_path / "fake-git-file"
    target.write_text(original, encoding="utf-8")
    git_file.symlink_to(target)

    with pytest.raises(WorktreeError, match=r"\.git|symlink|tamper"):
        asyncio.run(manager.ensure(plan, owner="U01ALICE"))
    store.close()
