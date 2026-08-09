"""Execution-plan scheduling tests using real temporary Git worktrees."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

import multi_app
from multi_app import (
    AgentConfig,
    NodeRuntimeLimiter,
    OwnerQuotaTracker,
    Roster,
    SlackAgent,
    load_agents_config,
    reload_config,
)
from multi_core import TurnBudget
from state_store import StateStore
from worktree_manager import WorktreeManager, discover_repo_spec


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", cwd=repo)
    _git("config", "user.name", "Execution Test", cwd=repo)
    _git("config", "user.email", "execution@example.invalid", cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _agent(
    name: str,
    *,
    repo: Path,
    root: Path,
    store: StateStore,
    manager: WorktreeManager,
    limiter: NodeRuntimeLimiter,
    runtime: str = "claude",
    owner: str = "",
    quota_tracker: OwnerQuotaTracker | None = None,
) -> SlackAgent:
    config = AgentConfig(
        name=name,
        bot_token=f"xoxb-{name}",
        app_token=f"xapp-{name}",
        persona=f"{name} worker",
        workspace=str(repo),
        allowed_tools=[],
        max_turns=2,
        runtime=runtime,
        owner=owner,
        workspace_mode="thread_worktree",
        worktree_root=str(root),
        worktree_base_ref="main",
        worktree_max_per_repo=8,
    )
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(root),
        base_ref="main",
        max_per_repo=8,
    )
    roster = Roster()
    roster.add(name, f"U{name.upper()}", f"B{name.upper()}", local=True)
    agent = SlackAgent(
        config,
        budget=TurnBudget(8),
        roster=roster,
        allowed_humans=set(),
        store=store,
        runtime_limiter=limiter,
        quota_tracker=quota_tracker,
        worktree_manager=manager,
        repo_spec=spec,
    )
    agent.user_id = f"U{name.upper()}"
    agent.bot_id = f"B{name.upper()}"
    agent.team_id = "T-EXECUTION"

    async def empty(*_args, **_kwargs):
        return ""

    async def names(*_args, **_kwargs):
        return frozenset({name})

    agent._channel_agent_names = names
    agent._set_reaction = empty
    agent._fetch_context = empty
    agent._fetch_channel_guidance = empty
    agent._ingest_files = empty
    agent._post_result = empty
    agent._maybe_rollover = empty
    return agent


def _event(thread_ts: str) -> dict[str, str]:
    return {
        "channel": "C-EXECUTION",
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "text": "work",
        "user": "U-HUMAN",
    }


async def _say(**_kwargs) -> None:
    return None


def test_execution_plan_is_immutable_and_shared_across_agents(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    limiter = NodeRuntimeLimiter(2, 4)
    first = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=limiter,
    )
    second = _agent(
        "b",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=limiter,
    )

    first_plan = first.build_execution_plan(_event("1.0"))
    second_plan = second.build_execution_plan(_event("1.0"))
    first.cfg.runtime = "codex"
    first.cfg.workspace = str(tmp_path / "mutated")
    first.cfg.allowed_tools.append("Bash")

    assert first_plan.execution_path == second_plan.execution_path
    assert first_plan.worktree_plan == second_plan.worktree_plan
    assert first_plan.config.runtime == "claude"
    assert first_plan.config.workspace == str(repo)
    assert first_plan.config.allowed_tools == ()
    assert Path(first_plan.execution_path).name.startswith("sat-t-")
    store.close()


def test_codex_new_and_resume_use_execution_worktree_cwd(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
        runtime="codex",
    )
    plan = agent.build_execution_plan(_event("codex"))
    calls: list[tuple[tuple[str, ...], dict]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(
        multi_app.asyncio, "create_subprocess_exec", fake_create
    )

    async def scenario():
        await agent._run_codex_exec(
            "new",
            None,
            config=plan.config,
            execution_workspace=plan.execution_path,
        )
        await agent._run_codex_exec(
            "resume",
            "thread-id",
            config=plan.config,
            execution_workspace=plan.execution_path,
        )

    asyncio.run(scenario())

    new_args, new_kwargs = calls[0]
    resume_args, resume_kwargs = calls[1]
    assert new_kwargs["cwd"] == plan.execution_path
    assert resume_kwargs["cwd"] == plan.execution_path
    assert new_args[new_args.index("-C") + 1] == plan.execution_path
    assert "resume" in resume_args
    assert "-C" not in resume_args
    store.close()


def test_openai_holds_same_plan_lease_without_claiming_filesystem(
    tmp_path
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
        runtime="openai",
    )
    requests: list[dict] = []

    class Response:
        id = "response-1"
        output_text = "done"
        usage = {"input_tokens": 3, "output_tokens": 2}

    class Responses:
        async def create(self, **request):
            requests.append(request)
            assert manager.active_lease_count() == 1
            return Response()

    class Client:
        responses = Responses()

    agent._openai_client = Client()
    asyncio.run(agent._activate(_event("openai"), object(), _say))

    assert len(requests) == 1
    instructions = requests[0]["instructions"]
    assert "scheduler-only lease" in instructions
    assert "without access to this machine's filesystem" in instructions
    assert "git worktree" in instructions
    assert manager.active_lease_count() == 0
    store.close()


def test_claude_rollover_stays_in_same_execution_worktree(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    agent.cfg.context_rollover_tokens = 1
    del agent.__dict__["_maybe_rollover"]
    seen_cwds: list[str] = []
    calls = 0

    class System:
        subtype = "init"
        data = {"session_id": "session-1"}

    class Result:
        num_turns = 1

        def __init__(self, result):
            self.result = result
            self.usage = {"input_tokens": 10, "output_tokens": 1}

    async def fake_query(*, prompt, options):
        nonlocal calls
        calls += 1
        seen_cwds.append(options.cwd)
        if calls == 1:
            yield System()
            yield Result("turn done")
        else:
            yield Result("rollover summary")

    monkeypatch.setattr(multi_app, "query", fake_query)
    monkeypatch.setattr(multi_app, "SystemMessage", System)
    monkeypatch.setattr(multi_app, "ResultMessage", Result)

    asyncio.run(agent._activate(_event("rollover"), object(), _say))

    assert calls == 2
    assert len(set(seen_cwds)) == 1
    assert str(root) in seen_cwds[0]
    assert "C-EXECUTION:rollover" not in agent.sessions
    assert (
        agent.thread_summaries["C-EXECUTION:rollover"]
        == "rollover summary"
    )
    store.close()


def test_persisted_session_identity_includes_exact_worktree_cwd(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )

    class System:
        subtype = "init"
        data = {"session_id": "session-base-identity"}

    class Result:
        result = "done"
        usage = {"input_tokens": 2, "output_tokens": 1}
        num_turns = 1

    async def fake_query(*, prompt, options):
        yield System()
        yield Result()

    monkeypatch.setattr(multi_app, "query", fake_query)
    monkeypatch.setattr(multi_app, "SystemMessage", System)
    monkeypatch.setattr(multi_app, "ResultMessage", Result)
    asyncio.run(agent._activate(_event("persist"), object(), _say))

    base_rows = store.load_agent(
        "a",
        runtime="claude",
        workspace=str(repo),
        ttl_seconds=3600,
        scope="T-EXECUTION",
    )
    mappings = store.list_worktree_mappings()
    assert base_rows["C-EXECUTION:persist"]["session_id"] == (
        "session-base-identity"
    )
    assert mappings
    assert store._conn is not None
    persisted_workspaces = {
        str(row[0])
        for row in store._conn.execute(
            "SELECT DISTINCT workspace FROM thread_state WHERE agent='a'"
        ).fetchall()
    }
    assert persisted_workspaces == {str(repo)}
    persisted_identity = store._conn.execute(
        "SELECT workspace_mode, execution_path "
        "FROM thread_state WHERE agent='a'"
    ).fetchone()
    assert persisted_identity["workspace_mode"] == "thread_worktree"
    assert persisted_identity["execution_path"] == mappings[0]["path"]
    store.close()


def test_cold_restore_same_exact_worktree_cwd_resumes(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    old = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = old.build_execution_plan(_event("same-cwd"))
    old.sessions[plan.thread_key] = "cwd-bound-session"
    old.thread_summaries[plan.thread_key] = "safe handoff"
    old.thread_stats[plan.thread_key] = {
        "input_tokens": 7,
        "num_turns": 2,
    }
    old.persist_thread(plan.thread_key, "1.0", plan)

    fresh = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    assert multi_app.restore_agent_state_from_store(fresh, store) == 1
    assert fresh.sessions == {
        plan.thread_key: "cwd-bound-session"
    }
    assert fresh.thread_summaries == {
        plan.thread_key: "safe handoff"
    }
    assert fresh.thread_stats[plan.thread_key]["num_turns"] == 2
    store.close()


@pytest.mark.parametrize(
    ("old_mode", "new_mode"),
    [
        ("serial", "thread_worktree"),
        ("thread_worktree", "serial"),
    ],
)
def test_cold_restore_workspace_mode_change_drops_session_keeps_summary(
    tmp_path, old_mode, new_mode
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    old = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    old.cfg.workspace_mode = old_mode
    plan = old.build_execution_plan(_event("mode-change"))
    old.sessions[plan.thread_key] = "must-not-resume"
    old.thread_summaries[plan.thread_key] = "safe handoff"
    old.thread_stats[plan.thread_key] = {
        "input_tokens": 7,
        "num_turns": 2,
    }
    old.persist_thread(plan.thread_key, "1.0", plan)

    fresh = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    fresh.cfg.workspace_mode = new_mode
    assert multi_app.restore_agent_state_from_store(fresh, store) == 1
    assert fresh.sessions == {}
    assert fresh.thread_stats == {}
    assert fresh.thread_summaries == {
        plan.thread_key: "safe handoff"
    }
    store.close()


def test_cold_restore_worktree_root_rehome_drops_cwd_bound_session(
    tmp_path,
):
    repo = _repo(tmp_path)
    old_root = tmp_path / "worktrees-old"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    old = _agent(
        "a",
        repo=repo,
        root=old_root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = old.build_execution_plan(_event("root-rehome"))
    old.sessions[plan.thread_key] = "old-root-session"
    old.thread_summaries[plan.thread_key] = "safe handoff"
    old.thread_stats[plan.thread_key] = {
        "input_tokens": 7,
        "num_turns": 2,
    }
    old.persist_thread(plan.thread_key, "1.0", plan)

    fresh = _agent(
        "a",
        repo=repo,
        root=tmp_path / "worktrees-new",
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    assert multi_app.restore_agent_state_from_store(fresh, store) == 1
    assert fresh.sessions == {}
    assert fresh.thread_stats == {}
    assert fresh.thread_summaries == {
        plan.thread_key: "safe handoff"
    }
    store.close()


def test_busy_worktree_root_reload_is_restart_only_and_keeps_live_spec(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    first_root = tmp_path / "worktrees-one"
    second_root = tmp_path / "worktrees-two"
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        (
            "worktrees:\n"
            f"  root: {first_root}\n"
            "  base_ref: main\n"
            "  max_per_repo: 8\n"
            "agents:\n"
            "  - name: a\n"
            "    persona: old\n"
            f"    workspace: {repo}\n"
            "    workspace_mode: thread_worktree\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("A_SLACK_BOT_TOKEN", "xoxb-a")
    monkeypatch.setenv("A_SLACK_APP_TOKEN", "xapp-a")
    configs, global_config = load_agents_config(str(config_path))
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(first_root),
        base_ref="main",
        max_per_repo=8,
    )
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
        store=store,
        runtime_limiter=NodeRuntimeLimiter(2, 4),
        worktree_manager=manager,
        repo_spec=spec,
    )
    agent.team_id = "T-EXECUTION"
    agent.user_id = "UA"
    agent.sessions["C-EXECUTION:keep"] = "session-must-survive"
    gate = asyncio.Event()
    started = asyncio.Event()
    seen_cwds: list[str] = []

    async def empty(*_args, **_kwargs):
        return ""

    async def names(*_args, **_kwargs):
        return frozenset({"a"})

    async def fake_query(*, prompt, options):
        seen_cwds.append(options.cwd)
        started.set()
        await gate.wait()
        if False:
            yield None

    agent._channel_agent_names = names
    agent._set_reaction = empty
    agent._fetch_context = empty
    agent._fetch_channel_guidance = empty
    agent._ingest_files = empty
    agent._post_result = empty
    agent._maybe_rollover = empty
    monkeypatch.setattr(multi_app, "query", fake_query)

    async def scenario():
        turn = asyncio.create_task(
            agent._activate(_event("reload"), object(), _say)
        )
        await started.wait()
        config_path.write_text(
            (
                "worktrees:\n"
                f"  root: {second_root}\n"
                "  base_ref: main\n"
                "  max_per_repo: 8\n"
                "agents:\n"
                "  - name: a\n"
                "    persona: new\n"
                f"    workspace: {repo}\n"
                "    workspace_mode: thread_worktree\n"
            ),
            encoding="utf-8",
        )
        report = reload_config(str(config_path), [agent], global_config)
        assert report["deferred"] == {}
        assert report["restart_required"]["a"] == ["worktree_root"]
        assert "worktree_root" in report["global_restart_required"]
        assert agent.cfg.worktree_root == str(first_root)
        assert agent.cfg.persona == "new"
        assert agent.sessions["C-EXECUTION:keep"] == "session-must-survive"
        gate.set()
        await turn

    asyncio.run(scenario())

    assert len(seen_cwds) == 1
    assert str(first_root) in seen_cwds[0]
    assert agent.cfg.worktree_root == str(first_root)
    assert global_config.worktree_root == str(first_root)
    assert agent.cfg.persona == "new"
    assert agent._repo_spec is not None
    assert agent._repo_spec.worktree_root == str(first_root)
    assert agent.sessions["C-EXECUTION:keep"] == "session-must-survive"
    store.close()


def test_ensure_failure_and_cancel_release_lease_limiter_and_quota(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    quota = OwnerQuotaTracker(
        {"U01ALICE": 1000},
        {"U01ALICE": 100},
        store=store,
    )
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(1, 2),
        owner="U01ALICE",
        quota_tracker=quota,
    )
    provider_calls = 0
    notices: list[dict] = []

    async def should_not_run(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return "unexpected"

    async def record_say(**kwargs):
        notices.append(kwargs)

    async def fail_ensure(*_args, **_kwargs):
        snapshot = agent.runtime_limiter.snapshot("a")
        assert snapshot["node_running"] == 1
        raise multi_app.WorktreeError("create failed")

    agent._run_turn = should_not_run
    monkeypatch.setattr(manager, "ensure", fail_ensure)
    reservation = quota.reserve(
        "U01ALICE", agent_name="a", runtime="claude"
    )
    assert reservation is not None

    async def failure_scenario():
        token = multi_app._CURRENT_QUOTA_RESERVATION.set(reservation)
        try:
            await agent._activate(_event("failure"), object(), record_say)
        finally:
            multi_app._CURRENT_QUOTA_RESERVATION.reset(token)

    asyncio.run(failure_scenario())
    assert provider_calls == 0
    assert len(notices) == 1
    assert quota.snapshot("U01ALICE")["active_reserved_tokens"] == 0
    assert quota.snapshot("U01ALICE")["total_tokens"] == 0
    assert manager.active_lease_count() == 0
    assert agent.runtime_limiter.snapshot("a")["node_admitted"] == 0

    entered = asyncio.Event()

    async def blocked_ensure(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "ensure", blocked_ensure)

    async def cancel_scenario():
        task = asyncio.create_task(
            agent._activate(_event("cancel"), object(), record_say)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_scenario())
    assert manager.active_lease_count() == 0
    assert agent.runtime_limiter.snapshot("a")["node_admitted"] == 0
    assert len(notices) == 1  # cancellation posts no second error
    store.close()


def test_on_message_admits_planned_path_before_activation_task_runs(
    tmp_path
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    limiter = NodeRuntimeLimiter(1, 2)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=limiter,
    )
    agent.allowed_humans = {"U-HUMAN"}
    entered = asyncio.Event()
    release = asyncio.Event()
    captured: list[tuple] = []

    async def fake_activate(_event, _client, _say):
        captured.append(
            (
                multi_app._CURRENT_EXECUTION_PLAN.get(),
                multi_app._CURRENT_RUNTIME_ADMISSION.get(),
            )
        )
        entered.set()
        await release.wait()

    agent._activate = fake_activate

    async def scenario():
        event = {
            **_event("planned"),
            "text": "<@UA> work",
            "user": "U-HUMAN",
        }
        await agent._on_message(
            {"event_id": "E-planned"}, event, object(), _say
        )
        await entered.wait()
        plan, admission = captured[0]
        assert plan is not None
        assert admission is not None
        assert admission.workspace == str(
            Path(plan.execution_path).resolve()
        )
        assert limiter.snapshot("a")["running"] == 1
        assert agent.is_busy()
        release.set()
        await asyncio.gather(*tuple(agent._tasks))
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert limiter.snapshot("a")["node_admitted"] == 0
    store.close()


def test_attachment_directory_uses_execution_worktree_not_base(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = agent.build_execution_plan(_event("attachment"))
    assert plan.worktree_plan is not None
    asyncio.run(manager.ensure(plan.worktree_plan, owner=""))
    token = multi_app._CURRENT_EXECUTION_PLAN.set(plan)
    try:
        destination = Path(plan.execution_path) / ".slack-files"
        assert agent._prepare_slack_files_dir(str(destination))
    finally:
        multi_app._CURRENT_EXECUTION_PLAN.reset(token)

    assert (destination / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert not (repo / ".slack-files").exists()
    store.close()


def test_sweep_cleans_only_expired_owned_canonical_worktree_attachments(
    tmp_path,
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
        owner="U01ALICE",
    )

    def create_attachment(
        plan, *, owner: str, filename: str, expired: bool
    ) -> Path:
        assert plan.worktree_plan is not None
        asyncio.run(
            manager.ensure(plan.worktree_plan, owner=owner)
        )
        destination = Path(plan.execution_path) / ".slack-files"
        token = multi_app._CURRENT_EXECUTION_PLAN.set(plan)
        try:
            assert agent._prepare_slack_files_dir(str(destination))
        finally:
            multi_app._CURRENT_EXECUTION_PLAN.reset(token)
        attachment = destination / filename
        attachment.write_text("temporary\n", encoding="utf-8")
        if expired:
            old = time.time() - (
                multi_app.THREAD_STATE_TTL_SECONDS * 2
            )
            os.utime(attachment, (old, old))
        return attachment

    expired_plan = agent.build_execution_plan(_event("expired"))
    fresh_plan = agent.build_execution_plan(_event("fresh"))
    expired = create_attachment(
        expired_plan,
        owner="U01ALICE",
        filename="expired.txt",
        expired=True,
    )
    fresh = create_attachment(
        fresh_plan,
        owner="U01ALICE",
        filename="fresh.txt",
        expired=False,
    )

    bob_plan = agent.build_execution_plan(
        {
            **_event("bob"),
            "channel": "C-BOB",
        }
    )
    bob_file = create_attachment(
        bob_plan,
        owner="U02BOB",
        filename="bob-expired.txt",
        expired=True,
    )

    symlink_plan = agent.build_execution_plan(
        {
            **_event("symlink"),
            "channel": "C-SYMLINK",
        }
    )
    assert symlink_plan.worktree_plan is not None
    asyncio.run(
        manager.ensure(
            symlink_plan.worktree_plan, owner="U01ALICE"
        )
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "must-stay.txt"
    outside_file.write_text("do not delete\n", encoding="utf-8")
    old = time.time() - (multi_app.THREAD_STATE_TTL_SECONDS * 2)
    os.utime(outside_file, (old, old))
    (
        Path(symlink_plan.execution_path) / ".slack-files"
    ).symlink_to(outside, target_is_directory=True)

    tampered_root = tmp_path / "tampered-root"
    tampered_root.mkdir()
    (tampered_root / ".git").write_text("fake\n", encoding="utf-8")
    tampered_files = tampered_root / ".slack-files"
    tampered_files.mkdir()
    tampered_file = tampered_files / "must-stay.txt"
    tampered_file.write_text("not managed\n", encoding="utf-8")
    os.utime(tampered_file, (old, old))
    assert store.save_worktree_mapping(
        identity_digest="f" * 64,
        repo_digest=expired_plan.repo_spec.repo_digest,
        common_dir=expired_plan.repo_spec.common_dir,
        base_workspace=expired_plan.repo_spec.base_workspace,
        team_id=expired_plan.team_id,
        channel_id="C-TAMPER",
        root_thread_ts="tamper",
        owner="U01ALICE",
        path=str(tampered_root),
        branch="slack-agent-wt/" + ("f" * 64),
        base_ref=expired_plan.repo_spec.base_ref,
        base_oid=expired_plan.repo_spec.base_oid,
        status="ready",
    )

    agent._last_sweep = -multi_app.SWEEP_INTERVAL_SECONDS * 2
    agent._sweep_thread_state()

    assert not expired.exists()
    assert fresh.read_text(encoding="utf-8") == "temporary\n"
    assert bob_file.read_text(encoding="utf-8") == "temporary\n"
    assert outside_file.read_text(encoding="utf-8") == "do not delete\n"
    assert tampered_file.read_text(encoding="utf-8") == "not managed\n"
    store.close()


def test_ingest_rejects_dangling_gitignore_symlink_without_network(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = agent.build_execution_plan(_event("dangling"))
    assert plan.worktree_plan is not None
    asyncio.run(manager.ensure(plan.worktree_plan, owner=""))
    destination = Path(plan.execution_path) / ".slack-files"
    destination.mkdir(mode=0o700)
    outside = tmp_path / "outside" / "created-by-symlink"
    outside.parent.mkdir()
    (destination / ".gitignore").symlink_to(outside)

    class NetworkMustNotStart:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("network started after unsafe directory")

    monkeypatch.setattr(
        multi_app.aiohttp, "ClientSession", NetworkMustNotStart
    )
    token = multi_app._CURRENT_EXECUTION_PLAN.set(plan)
    try:
        note = asyncio.run(
            SlackAgent._ingest_files(
                agent,
                {
                    **_event("dangling"),
                    "files": [
                        {
                            "name": "payload.txt",
                            "size": 7,
                            "url_private": (
                                "https://files.slack.com/files-pri/payload"
                            ),
                        }
                    ],
                },
            )
        )
    finally:
        multi_app._CURRENT_EXECUTION_PLAN.reset(token)

    assert "取得に失敗" in note
    assert not outside.exists()
    store.close()


def test_ingest_parent_swap_during_network_wait_never_writes_outside(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = agent.build_execution_plan(_event("swap"))
    assert plan.worktree_plan is not None
    asyncio.run(manager.ensure(plan.worktree_plan, owner=""))
    destination = Path(plan.execution_path) / ".slack-files"
    moved_destination = Path(plan.execution_path) / ".slack-files-moved"
    outside = tmp_path / "outside"
    outside.mkdir()

    class Content:
        async def iter_chunked(self, _size):
            yield b"secret Slack payload"

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            destination.rename(moved_destination)
            destination.symlink_to(outside, target_is_directory=True)
            return self

        async def __aexit__(self, *_exc_info):
            return None

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(multi_app.aiohttp, "ClientSession", Session)
    token = multi_app._CURRENT_EXECUTION_PLAN.set(plan)
    try:
        note = asyncio.run(
            SlackAgent._ingest_files(
                agent,
                {
                    **_event("swap"),
                    "files": [
                        {
                            "name": "payload.txt",
                            "size": 20,
                            "url_private": (
                                "https://files.slack.com/files-pri/payload"
                            ),
                        }
                    ],
                },
            )
        )
    finally:
        multi_app._CURRENT_EXECUTION_PLAN.reset(token)

    assert "取得に失敗" in note
    assert list(outside.iterdir()) == []
    assert not (
        moved_destination / "swap-0-payload.txt"
    ).exists()
    store.close()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_prepare_rejects_existing_nonregular_gitignore(
    tmp_path, kind
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = agent.build_execution_plan(_event(f"bad-{kind}"))
    assert plan.worktree_plan is not None
    asyncio.run(manager.ensure(plan.worktree_plan, owner=""))
    destination = Path(plan.execution_path) / ".slack-files"
    destination.mkdir(mode=0o700)
    gitignore = destination / ".gitignore"
    outside = tmp_path / "external-gitignore"
    outside.write_text("preserve\n", encoding="utf-8")
    if kind == "symlink":
        gitignore.symlink_to(outside)
    else:
        gitignore.mkdir()

    assert not agent._prepare_slack_files_dir(
        str(destination), plan.execution_path
    )
    assert outside.read_text(encoding="utf-8") == "preserve\n"
    store.close()


def test_prepare_slack_files_dir_is_concurrent_and_private(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    plan = agent.build_execution_plan(_event("concurrent"))
    assert plan.worktree_plan is not None
    asyncio.run(manager.ensure(plan.worktree_plan, owner=""))
    destination = Path(plan.execution_path) / ".slack-files"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _index: agent._prepare_slack_files_dir(
                    str(destination), plan.execution_path
                ),
                range(16),
            )
        )

    assert all(results)
    gitignore = destination / ".gitignore"
    assert gitignore.read_text(encoding="utf-8") == "*\n"
    assert os.stat(destination).st_mode & 0o777 == 0o700
    assert os.stat(gitignore).st_mode & 0o777 == 0o600
    store.close()


def test_base_ref_reload_rebuilds_repo_spec_and_clears_sessions(tmp_path):
    repo = _repo(tmp_path)
    _git("branch", "release", "main", cwd=repo)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    agent.sessions["C:1"] = "old-session"
    agent.thread_stats["C:1"] = {"input_tokens": 2, "num_turns": 1}
    agent.persist_thread("C:1", "1.0")
    generation = agent._config_gen
    fields = asdict(agent.cfg)
    fields["worktree_base_ref"] = "release"

    changed, restart = agent.apply_config(fields)

    assert changed == ["worktree_base_ref"]
    assert restart == []
    assert agent.sessions == {}
    assert agent.thread_stats == {}
    assert agent._config_gen == generation + 1
    assert agent._repo_spec is not None
    assert agent._repo_spec.base_ref == "release"
    rows = store.load_agent(
        "a",
        runtime="claude",
        workspace=str(repo),
        ttl_seconds=3600,
        scope="T-EXECUTION",
    )
    assert rows["C:1"]["session_id"] == ""
    store.close()


def test_max_per_repo_reload_rebuilds_spec_without_clearing_sessions(
    tmp_path,
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    agent.sessions["C:1"] = "session-kept"
    agent.thread_stats["C:1"] = {"input_tokens": 2, "num_turns": 1}
    generation = agent._config_gen
    fields = asdict(agent.cfg)
    fields["worktree_max_per_repo"] = 3

    changed, restart = agent.apply_config(fields)

    assert changed == ["worktree_max_per_repo"]
    assert restart == []
    assert agent.sessions == {"C:1": "session-kept"}
    assert agent.thread_stats["C:1"]["num_turns"] == 1
    assert agent._config_gen == generation
    assert agent._repo_spec is not None
    assert agent._repo_spec.worktree_root == str(root)
    assert agent._repo_spec.max_per_repo == 3
    store.close()


def test_equivalent_canonical_worktree_root_reload_needs_no_restart(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    spelling_dir = tmp_path / "spelling"
    spelling_dir.mkdir()
    config_path = tmp_path / "agents.yaml"

    def write_config(root_text: str) -> None:
        config_path.write_text(
            (
                "worktrees:\n"
                f"  root: {root_text}\n"
                "  base_ref: main\n"
                "  max_per_repo: 8\n"
                "agents:\n"
                "  - name: a\n"
                "    persona: worker\n"
                f"    workspace: {repo}\n"
                "    workspace_mode: thread_worktree\n"
            ),
            encoding="utf-8",
        )

    write_config(str(root))
    monkeypatch.setenv("A_SLACK_BOT_TOKEN", "xoxb-a")
    monkeypatch.setenv("A_SLACK_APP_TOKEN", "xapp-a")
    configs, global_config = load_agents_config(str(config_path))
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(root),
        base_ref="main",
        max_per_repo=8,
    )
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
        store=store,
        runtime_limiter=NodeRuntimeLimiter(2, 4),
        worktree_manager=manager,
        repo_spec=spec,
    )
    equivalent = spelling_dir / ".." / root.name
    write_config(str(equivalent))

    report = reload_config(str(config_path), [agent], global_config)

    assert "worktree_root" not in report["global_restart_required"]
    assert "worktree_root" not in report["restart_required"].get("a", [])
    assert agent.cfg.worktree_root == str(root)
    assert global_config.worktree_root == str(root)
    assert agent._repo_spec is not None
    assert agent._repo_spec.worktree_root == str(root)
    store.close()


def test_two_threads_same_agent_runtime_actually_overlap(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    agent = _agent(
        "a",
        repo=repo,
        root=root,
        store=store,
        manager=manager,
        limiter=NodeRuntimeLimiter(2, 4),
    )
    active = 0
    max_active = 0
    both_started = asyncio.Event()
    seen_cwds: list[str] = []

    async def fake_query(*, prompt, options):
        nonlocal active, max_active
        seen_cwds.append(options.cwd)
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            both_started.set()
        try:
            await both_started.wait()
            if False:
                yield None
        finally:
            active -= 1

    monkeypatch.setattr(multi_app, "query", fake_query)

    async def scenario():
        tasks = [
            asyncio.create_task(agent._activate(_event(thread), object(), _say))
            for thread in ("1.0", "2.0")
        ]
        try:
            await asyncio.wait_for(both_started.wait(), timeout=5)
        finally:
            if not both_started.is_set():
                for task in tasks:
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())

    assert max_active == 2
    assert len(set(seen_cwds)) == 2
    assert all(str(root) in cwd for cwd in seen_cwds)
    assert manager.active_lease_count() == 0
    store.close()


def test_same_thread_two_agents_share_path_and_serialize(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    limiter = NodeRuntimeLimiter(2, 4)
    agents = [
        _agent(
            name,
            repo=repo,
            root=root,
            store=store,
            manager=manager,
            limiter=limiter,
        )
        for name in ("a", "b")
    ]
    started = 0
    active = 0
    max_active = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    seen_cwds: list[str] = []

    async def fake_query(*, prompt, options):
        nonlocal started, active, max_active
        started += 1
        active += 1
        max_active = max(max_active, active)
        seen_cwds.append(options.cwd)
        if started == 1:
            first_started.set()
            await release_first.wait()
        active -= 1
        if False:
            yield None

    monkeypatch.setattr(multi_app, "query", fake_query)

    async def scenario():
        tasks = [
            asyncio.create_task(agent._activate(_event("1.0"), object(), _say))
            for agent in agents
        ]
        await asyncio.wait_for(first_started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        assert started == 1
        release_first.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())

    assert started == 2
    assert max_active == 1
    assert len(set(seen_cwds)) == 1
    assert str(root) in seen_cwds[0]
    assert manager.active_lease_count() == 0
    store.close()


def test_worktree_lease_releases_after_body_error(tmp_path):
    repo = _repo(tmp_path)
    root = tmp_path / "worktrees"
    store = StateStore(str(tmp_path / "state.db"))
    manager = WorktreeManager(store)
    spec = discover_repo_spec(
        workspace=str(repo),
        worktree_root=str(root),
        base_ref="main",
        max_per_repo=2,
    )
    plan = manager.plan(
        spec,
        team_id="T",
        channel_id="C",
        root_thread_ts="1.0",
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="body failed"):
            async with manager.lease(plan, owner=""):
                assert manager.active_lease_count(plan.identity_digest) == 1
                raise RuntimeError("body failed")

    asyncio.run(scenario())
    assert manager.active_lease_count() == 0
    store.close()
