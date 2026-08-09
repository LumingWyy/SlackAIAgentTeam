"""Owner-scoped daily AI usage and quota governance."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from multi_core import TurnBudget
from state_store import StateStore


def _finite_tracker(
    tmp_path,
    limits,
    reservations,
    *,
    clock=None,
    db_name="quota.db",
):
    from multi_app import OwnerQuotaTracker

    return OwnerQuotaTracker(
        limits,
        reservations,
        store=StateStore(str(tmp_path / db_name)),
        clock=clock,
    )


def _agent(tmp_path, *, quota_tracker=None, owner="U01ALICE"):
    from multi_app import AgentConfig, NodeRuntimeLimiter, Roster, SlackAgent

    cfg = AgentConfig(
        name="alice",
        bot_token="local-bot",
        app_token="local-app",
        persona="local",
        workspace=str(tmp_path),
        allowed_tools=[],
        max_turns=1,
        owner=owner,
        node_id="alice-node",
        configured_user_id="UALICEBOT",
        configured_bot_id="BALICEBOT",
    )
    roster = Roster()
    roster.add(
        "alice",
        "UALICEBOT",
        "BALICEBOT",
        owner_user_id=owner,
        node_id="alice-node",
        local=True,
    )
    agent = SlackAgent(
        cfg,
        budget=TurnBudget(8),
        roster=roster,
        allowed_humans={owner},
        runtime_limiter=NodeRuntimeLimiter(1),
        quota_tracker=quota_tracker,
    )
    agent.user_id = "UALICEBOT"
    agent.bot_id = "BALICEBOT"
    agent.team_id = "TTEAM"
    return agent


def test_roster_parses_owner_daily_input_token_quotas(
    tmp_path, monkeypatch
):
    from multi_app import load_agents_config

    roster = tmp_path / "roster.yaml"
    local = tmp_path / "agents.alice.yaml"
    roster.write_text(
        """
version: 1
quotas:
  daily_total_tokens:
    U01ALICE: 1000
    U02BOB: 2500
  reservation_tokens:
    U01ALICE: 200
    U02BOB: 500
agents:
  - name: alice
    slack_user_id: UALICEBOT
    slack_bot_id: BALICEBOT
    owner: U01ALICE
    node_id: alice-node
  - name: bob
    slack_user_id: UBOBBOT
    slack_bot_id: BBOBBOT
    owner: U02BOB
    node_id: bob-node
""",
        encoding="utf-8",
    )
    local.write_text(
        """
roster: roster.yaml
node:
  id: alice-node
agents:
  - name: alice
    persona: local
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALICE_SLACK_BOT_TOKEN", "local-bot")
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "local-app")

    _configs, gcfg = load_agents_config(str(local))

    assert gcfg.owner_daily_total_token_limits == {
        "U01ALICE": 1000,
        "U02BOB": 2500,
    }
    assert gcfg.owner_quota_reservation_tokens == {
        "U01ALICE": 200,
        "U02BOB": 500,
    }


@pytest.mark.parametrize(
    ("quotas", "error"),
    [
        (
            {
                "daily_total_tokens": {"U99UNKNOWN": 100},
                "reservation_tokens": {"U99UNKNOWN": 10},
            },
            "unknown owner",
        ),
        (
            {
                "daily_total_tokens": {"U01ALICE": 0},
                "reservation_tokens": {"U01ALICE": 10},
            },
            "positive",
        ),
        (
            {
                "daily_total_tokens": {"U01ALICE": "many"},
                "reservation_tokens": {"U01ALICE": 10},
            },
            "integer",
        ),
        ({"daily_total_tokens": []}, "mapping"),
        (
            {"daily_total_tokens": {"U01ALICE": 100}},
            "reservation_tokens",
        ),
    ],
)
def test_owner_quota_config_fails_closed(tmp_path, monkeypatch, quotas, error):
    import yaml

    from multi_app import load_agents_config

    roster = tmp_path / "roster.yaml"
    local = tmp_path / "agents.alice.yaml"
    roster.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "quotas": quotas,
                "agents": [
                    {
                        "name": "alice",
                        "slack_user_id": "UALICEBOT",
                        "slack_bot_id": "BALICEBOT",
                        "owner": "U01ALICE",
                        "node_id": "alice-node",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    local.write_text(
        "node:\n  id: alice-node\nagents:\n  - name: alice\n    persona: local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALICE_SLACK_BOT_TOKEN", "local-bot")
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "local-app")

    with pytest.raises(RuntimeError, match=error):
        load_agents_config(str(local), roster_path=str(roster))


def test_owner_quota_tracker_aggregates_agents_and_rolls_over_utc_day(
    tmp_path,
):
    from multi_app import OwnerQuotaTracker

    now = [datetime(2026, 7, 28, 23, 59, tzinfo=timezone.utc)]
    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
        clock=lambda: now[0],
    )
    tracker.record(
        "U01ALICE",
        total_tokens=40,
        input_tokens=25,
        output_tokens=10,
        cache_tokens=5,
        agent_name="alice/dev",
    )
    tracker.record(
        "U01ALICE",
        total_tokens=35,
        input_tokens=20,
        output_tokens=10,
        cache_tokens=5,
        agent_name="alice/reviewer",
    )
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["owner"] == "U01ALICE"
    assert snapshot["utc_day"] == "2026-07-28"
    assert snapshot["total_tokens"] == 75
    assert snapshot["input_tokens"] == 45
    assert snapshot["output_tokens"] == 20
    assert snapshot["cache_tokens"] == 10
    assert snapshot["daily_total_token_limit"] == 100
    assert snapshot["remaining_total_tokens"] == 25
    assert snapshot["quota_exhausted"] is False

    now[0] = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)
    assert tracker.snapshot("U01ALICE")["total_tokens"] == 0
    assert tracker.allows("U01ALICE") is True


def test_owner_daily_usage_persists_across_restart(tmp_path):
    from multi_app import OwnerQuotaTracker

    path = str(tmp_path / "state.db")
    first_store = StateStore(path)
    first = OwnerQuotaTracker(
        {"U01ALICE": 100},
        {"U01ALICE": 20},
        store=first_store,
        clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    first.record(
        "U01ALICE",
        total_tokens=60,
        input_tokens=40,
        output_tokens=15,
        cache_tokens=5,
        agent_name="alice/dev",
    )
    first_store.close()

    second_store = StateStore(path)
    second = OwnerQuotaTracker(
        {"U01ALICE": 100},
        {"U01ALICE": 20},
        store=second_store,
        clock=lambda: datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    assert second.snapshot("U01ALICE")["total_tokens"] == 60
    second.record(
        "U01ALICE",
        total_tokens=40,
        input_tokens=20,
        output_tokens=15,
        cache_tokens=5,
        agent_name="alice/reviewer",
    )
    assert second.allows("U01ALICE") is False
    second_store.close()


def test_quota_exhaustion_rejects_activation_before_queue_or_runtime(
    tmp_path,
):
    from multi_app import OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 50},
        {"U01ALICE": 10},
    )
    tracker.record(
        "U01ALICE",
        total_tokens=50,
        input_tokens=30,
        output_tokens=15,
        cache_tokens=5,
        agent_name="alice/dev",
    )
    agent = _agent(tmp_path, quota_tracker=tracker)
    messages = []

    async def say(**kwargs):
        messages.append(kwargs)

    async def scenario():
        await agent._on_message(
            {"event_id": "E1", "team_id": "TTEAM"},
            {
                "channel": "DOWNER",
                "channel_type": "im",
                "ts": "1.0",
                "user": "U01ALICE",
                "text": "please run",
            },
            object(),
            say,
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert agent.runtime_limiter.snapshot("alice")["node_admitted"] == 0
    assert agent._tasks == set()
    assert len(messages) == 1
    assert messages[0]["thread_ts"] == "1.0"
    assert "daily AI quota" in messages[0]["text"]
    assert "50 / 50" in messages[0]["text"]


def test_codex_turn_records_completed_input_usage(tmp_path, monkeypatch):
    from multi_app import OwnerQuotaTracker

    from multi_app import ProviderTokenUsage

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    agent = _agent(tmp_path, quota_tracker=tracker)
    agent.cfg.runtime = "codex"

    async def run_exec(*_args, **_kwargs):
        # Production marks the reservation immediately after the provider
        # subprocess has started. This fake bypasses that boundary, so mirror it.
        agent._mark_quota_runtime_started()
        return (
            "done",
            "thread-id",
            37,
            ProviderTokenUsage(
                input_tokens=20,
                output_tokens=5,
                cache_tokens=17,
                total_tokens=42,
                complete=True,
            ),
        )

    monkeypatch.setattr(agent, "_run_codex_exec", run_exec)
    thread_key = "C1:1.0"
    reservation = tracker.reserve("U01ALICE", agent_name="alice")
    assert reservation is not None

    async def scenario():
        from multi_app import _CURRENT_QUOTA_RESERVATION

        token = _CURRENT_QUOTA_RESERVATION.set(reservation)
        try:
            return await agent._run_codex(
                "prompt",
                thread_key,
                agent._turn_generation(thread_key),
            )
        finally:
            _CURRENT_QUOTA_RESERVATION.reset(token)

    result = asyncio.run(scenario())

    assert result == "done"
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 42
    assert snapshot["input_tokens"] == 20
    assert snapshot["output_tokens"] == 5
    assert snapshot["cache_tokens"] == 17


def test_admin_state_aggregates_usage_by_visible_owner(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from control_auth import ControlAuthenticator
    from multi_app import (
        GlobalConfig,
        OwnerQuotaTracker,
        build_admin_app,
    )

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100, "U02BOB": 200},
        {"U01ALICE": 20, "U02BOB": 40},
    )
    tracker.record("U01ALICE", total_tokens=30, agent_name="alice")
    tracker.record("U02BOB", total_tokens=70, agent_name="bob")
    alice = _agent(tmp_path / "alice", quota_tracker=tracker)
    bob = _agent(
        tmp_path / "bob",
        quota_tracker=tracker,
        owner="U02BOB",
    )
    bob.cfg.name = "bob"
    token = "alice-" + ("q" * 40)
    admin_token = "admin-" + ("q" * 40)
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE", "U02BOB"},
        admin_user_ids={"U00ADMIN"},
        required=True,
        environ={
            "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": token,
            "SLACK_AGENT_CONTROL_TOKEN_U02BOB": "bob-" + ("q" * 40),
            "SLACK_AGENT_CONTROL_TOKEN_U00ADMIN": admin_token,
        },
    )
    gcfg = GlobalConfig(
        max_agent_rounds=8,
        github_repo=None,
        patrol_channel=None,
        owner_daily_total_token_limits={
            "U01ALICE": 100,
            "U02BOB": 200,
        },
        owner_quota_reservation_tokens={
            "U01ALICE": 20,
            "U02BOB": 40,
        },
    )

    async def scenario():
        async with TestClient(
            TestServer(
                build_admin_app(
                    [alice, bob],
                    gcfg,
                    authenticator=auth,
                )
            )
        ) as client:
            response = await client.get(
                "/state",
                headers={"Authorization": f"Bearer {token}"},
            )
            admin_response = await client.get(
                "/state",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            return (
                response.status,
                await response.json(),
                admin_response.status,
                await admin_response.json(),
            )

    status, payload, admin_status, admin_payload = asyncio.run(scenario())
    assert status == 200
    assert set(payload["owner_usage"]) == {"U01ALICE"}
    assert payload["owner_usage"]["U01ALICE"]["total_tokens"] == 30
    assert payload["owner_usage"]["U01ALICE"]["agent_names"] == ["alice"]
    assert admin_status == 200
    assert set(admin_payload["owner_usage"]) == {
        "U01ALICE",
        "U02BOB",
    }
    assert {
        row["agent"]
        for row in admin_payload["owner_usage"]["U02BOB"]["breakdown"]
    } == {"bob"}

    def nested_keys(value):
        if isinstance(value, dict):
            return set(value).union(
                *(
                    nested_keys(item)
                    for item in value.values()
                ),
                set(),
            )
        if isinstance(value, list):
            return set().union(*(nested_keys(item) for item in value), set())
        return set()

    usage_keys = nested_keys(admin_payload["owner_usage"])
    assert "prompt" not in usage_keys
    assert "text" not in usage_keys


def test_sqlite_reservation_is_atomic_and_blocks_concurrent_oversubscription(
    tmp_path,
):
    from multi_app import OwnerQuotaTracker

    store = StateStore(str(tmp_path / "state.db"))
    statements = []
    store._conn.set_trace_callback(statements.append)
    tracker = OwnerQuotaTracker(
        {"U01ALICE": 100},
        {"U01ALICE": 60},
        store=store,
    )

    first = tracker.reserve("U01ALICE", agent_name="alice/dev")
    second = tracker.reserve("U01ALICE", agent_name="alice/reviewer")

    assert first is not None
    assert second is None
    assert tracker.snapshot("U01ALICE")["active_reserved_tokens"] == 60
    assert any(
        statement.strip().upper() == "BEGIN IMMEDIATE"
        for statement in statements
    )
    store.close()


def test_unstarted_reservation_release_restores_capacity(tmp_path):
    from multi_app import OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 50},
        {"U01ALICE": 50},
    )
    first = tracker.reserve("U01ALICE", agent_name="alice/dev")
    assert first is not None
    assert tracker.reserve("U01ALICE", agent_name="alice/reviewer") is None

    tracker.release(first)
    assert tracker.snapshot("U01ALICE")["active_reserved_tokens"] == 0
    assert tracker.reserve("U01ALICE", agent_name="alice/reviewer") is not None


def test_started_error_conservatively_settles_reservation(tmp_path):
    from multi_app import OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 40},
    )
    reservation = tracker.reserve("U01ALICE", agent_name="alice/dev")
    assert reservation is not None
    tracker.mark_started(reservation)
    tracker.settle_error(reservation)

    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 40
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["estimated_turns"] == 1
    assert snapshot["errors"] == 1


def test_startup_recovers_started_and_unstarted_stale_reservations(tmp_path):
    from multi_app import OwnerQuotaTracker

    path = str(tmp_path / "state.db")
    first_store = StateStore(path)
    first = OwnerQuotaTracker(
        {"U01ALICE": 200},
        {"U01ALICE": 50},
        store=first_store,
    )
    started = first.reserve("U01ALICE", agent_name="alice/dev")
    unstarted = first.reserve("U01ALICE", agent_name="alice/reviewer")
    assert started is not None and unstarted is not None
    first.mark_started(started)
    first_store.close()

    second_store = StateStore(path)
    second = OwnerQuotaTracker(
        {"U01ALICE": 200},
        {"U01ALICE": 50},
        store=second_store,
    )
    snapshot = second.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 50
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["estimated_turns"] == 1
    assert snapshot["errors"] == 1
    second_store.close()


def test_exact_usage_may_overshoot_one_reservation_then_denies_next_turn(
    tmp_path,
):
    from multi_app import OwnerQuotaTracker, ProviderTokenUsage

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 40},
    )
    reservation = tracker.reserve("U01ALICE", agent_name="alice/dev")
    assert reservation is not None
    tracker.mark_started(reservation)
    tracker.settle(
        reservation,
        ProviderTokenUsage(
            input_tokens=90,
            output_tokens=35,
            total_tokens=125,
            complete=True,
        ),
    )

    assert tracker.reserve(
        "U01ALICE", agent_name="alice/reviewer"
    ) is None
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 125
    assert snapshot["completed_turns"] == 1
    assert snapshot["estimated_turns"] == 0
    assert snapshot["denied"] == 1


def test_missing_output_usage_settles_at_least_reservation_as_estimated(
    tmp_path,
):
    from multi_app import OwnerQuotaTracker, ProviderTokenUsage

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    reservation = tracker.reserve("U01ALICE", agent_name="alice/dev")
    assert reservation is not None
    tracker.mark_started(reservation)
    tracker.settle(
        reservation,
        ProviderTokenUsage(
            input_tokens=7,
            total_tokens=7,
            complete=False,
        ),
    )

    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 20
    assert snapshot["input_tokens"] == 7
    assert snapshot["output_tokens"] == 0
    assert snapshot["estimated_turns"] == 1
    assert snapshot["completed_turns"] == 0


def test_queue_full_and_task_creation_failure_release_unstarted_quota(
    tmp_path, monkeypatch
):
    import multi_app
    from multi_app import NodeRuntimeLimiter, OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    agent = _agent(tmp_path, quota_tracker=tracker)
    agent.runtime_limiter = NodeRuntimeLimiter(1, 0)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_inner(*_args):
        started.set()
        await gate.wait()

    async def say(**_kwargs):
        return None

    agent._activate_inner = fake_inner

    async def send(event_id, ts):
        await agent._on_message(
            {"event_id": event_id, "team_id": "TTEAM"},
            {
                "channel": "DOWNER",
                "channel_type": "im",
                "ts": ts,
                "user": "U01ALICE",
                "text": "run",
            },
            object(),
            say,
        )

    async def queue_scenario():
        await send("E-queue-1", "1.0")
        await started.wait()
        assert tracker.snapshot("U01ALICE")[
            "active_reserved_tokens"
        ] == 20
        await send("E-queue-2", "2.0")
        # The rejected second activation briefly reserved quota, then released
        # it because no node/queue slot existed.
        assert tracker.snapshot("U01ALICE")[
            "active_reserved_tokens"
        ] == 20
        task = next(iter(agent._tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(queue_scenario())
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["total_tokens"] == 0

    def fail_create_task(coroutine):
        raise RuntimeError("task creation failed")

    monkeypatch.setattr(multi_app.asyncio, "create_task", fail_create_task)

    async def creation_scenario():
        with pytest.raises(RuntimeError, match="task creation failed"):
            await send("E-create-fail", "3.0")

    asyncio.run(creation_scenario())
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["total_tokens"] == 0


def test_queued_cancellation_before_provider_start_releases_quota(tmp_path):
    from multi_app import NodeRuntimeLimiter, OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    agent = _agent(tmp_path, quota_tracker=tracker)
    agent.runtime_limiter = NodeRuntimeLimiter(1, 1)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_inner(*_args):
        started.set()
        await gate.wait()

    async def say(**_kwargs):
        return None

    agent._activate_inner = fake_inner

    async def send(event_id, ts):
        before = set(agent._tasks)
        await agent._on_message(
            {"event_id": event_id, "team_id": "TTEAM"},
            {
                "channel": "DOWNER",
                "channel_type": "im",
                "ts": ts,
                "user": "U01ALICE",
                "text": "run",
            },
            object(),
            say,
        )
        created = set(agent._tasks) - before
        assert len(created) == 1
        return created.pop()

    async def scenario():
        first = await send("E-cancel-1", "1.0")
        await started.wait()
        second = await send("E-cancel-2", "2.0")
        assert tracker.snapshot("U01ALICE")[
            "active_reserved_tokens"
        ] == 40
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        await asyncio.sleep(0)
        assert tracker.snapshot("U01ALICE")[
            "active_reserved_tokens"
        ] == 20
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["total_tokens"] == 0
    assert snapshot["errors"] == 0


def test_started_activation_error_is_conservatively_finalized(tmp_path):
    from multi_app import OwnerQuotaTracker

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    agent = _agent(tmp_path, quota_tracker=tracker)

    async def fake_inner(*_args):
        agent._mark_quota_runtime_started()
        raise RuntimeError("provider failed without usage")

    async def say(**_kwargs):
        return None

    agent._activate_inner = fake_inner

    async def scenario():
        await agent._on_message(
            {"event_id": "E-started-error", "team_id": "TTEAM"},
            {
                "channel": "DOWNER",
                "channel_type": "im",
                "ts": "1.0",
                "user": "U01ALICE",
                "text": "run",
            },
            object(),
            say,
        )
        task = next(iter(agent._tasks))
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], RuntimeError)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 20
    assert snapshot["active_reserved_tokens"] == 0
    assert snapshot["estimated_turns"] == 1
    assert snapshot["errors"] == 1


def test_openai_usage_counts_output_and_tracks_cached_input_without_double_count(
    tmp_path,
):
    from types import SimpleNamespace

    from multi_app import (
        OwnerQuotaTracker,
        _CURRENT_QUOTA_RESERVATION,
    )

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 100},
        {"U01ALICE": 20},
    )
    agent = _agent(tmp_path, quota_tracker=tracker)
    agent.cfg.runtime = "openai"

    class FakeResponses:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                id="response-1",
                output_text="done",
                usage={
                    "input_tokens": 30,
                    "output_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 11},
                },
            )

    agent._openai_client = SimpleNamespace(responses=FakeResponses())
    reservation = tracker.reserve(
        "U01ALICE", agent_name="alice", runtime="openai"
    )
    assert reservation is not None

    async def scenario():
        token = _CURRENT_QUOTA_RESERVATION.set(reservation)
        try:
            return await agent._run_openai_response(
                "prompt",
                system_prompt="system",
                previous_response_id=None,
            )
        finally:
            _CURRENT_QUOTA_RESERVATION.reset(token)

    assert asyncio.run(scenario()) == ("done", "response-1", 30)
    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 37
    assert snapshot["input_tokens"] == 30
    assert snapshot["output_tokens"] == 7
    assert snapshot["cache_tokens"] == 11


def test_patrol_and_rollover_each_settle_provider_usage(tmp_path, monkeypatch):
    import multi_app
    from multi_app import OwnerQuotaTracker, ProviderTokenUsage

    tracker = _finite_tracker(
        tmp_path,
        {"U01ALICE": 200},
        {"U01ALICE": 20},
    )
    patrol = _agent(tmp_path / "patrol", quota_tracker=tracker)
    patrol.cfg.runtime = "codex"
    patrol.github_repo = "acme/widgets"

    async def fake_exec(*_args, **_kwargs):
        patrol._mark_quota_runtime_started()
        return (
            "PATROL_IDLE",
            None,
            10,
            ProviderTokenUsage(
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
                complete=True,
            ),
        )

    patrol._run_codex_exec = fake_exec
    asyncio.run(patrol._run_patrol_once("patrol", "CPATROL"))

    rollover = _agent(tmp_path / "rollover", quota_tracker=tracker)
    rollover.cfg.runtime = "claude"
    rollover.cfg.context_rollover_tokens = 10
    thread_key = "C1:1.0"
    rollover.sessions[thread_key] = "session-1"
    rollover.thread_stats[thread_key] = {
        "input_tokens": 11,
        "num_turns": 1,
    }

    class FakeResult:
        result = "handoff summary"
        usage = {
            "input_tokens": 8,
            "cache_read_input_tokens": 3,
            "output_tokens": 5,
        }

    async def fake_query(*_args, **_kwargs):
        yield FakeResult()

    async def say(**_kwargs):
        return None

    monkeypatch.setattr(multi_app, "ResultMessage", FakeResult)
    monkeypatch.setattr(multi_app, "query", fake_query)
    asyncio.run(rollover._maybe_rollover(thread_key, "1.0", say))

    snapshot = tracker.snapshot("U01ALICE")
    assert snapshot["total_tokens"] == 30
    assert snapshot["input_tokens"] == 21
    assert snapshot["output_tokens"] == 9
    assert snapshot["cache_tokens"] == 3
    assert snapshot["completed_turns"] == 2
    assert snapshot["errors"] == 0


@pytest.mark.parametrize("store_kind", ["none", "disabled"])
def test_finite_owner_quota_refuses_missing_or_disabled_store(
    tmp_path, store_kind
):
    from multi_app import OwnerQuotaTracker

    store = None
    if store_kind == "disabled":
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("blocker", encoding="utf-8")
        store = StateStore(str(blocker / "state.db"))
        assert store.enabled is False

    with pytest.raises(
        RuntimeError, match="finite owner quotas require.*StateStore"
    ):
        OwnerQuotaTracker(
            {"U01ALICE": 100},
            {"U01ALICE": 20},
            store=store,
        )


def test_unlimited_legacy_tracker_still_works_without_state_db():
    from multi_app import OwnerQuotaTracker

    tracker = OwnerQuotaTracker({}, {})
    reservation = tracker.reserve("U01ALICE", agent_name="alice")
    assert reservation is not None
    tracker.release(reservation)
    assert tracker.snapshot("U01ALICE")["daily_total_token_limit"] is None


def test_reserve_defensively_rejects_finite_quota_without_store():
    from multi_app import OwnerQuotaTracker

    tracker = OwnerQuotaTracker({}, {})
    # Defense in depth for restart/reload/programmatic callers that mutate a
    # tracker outside the validated set_limits path.
    tracker._daily_limits = {"U01ALICE": 100}
    tracker._reservation_tokens = {"U01ALICE": 20}

    with pytest.raises(
        RuntimeError, match="finite owner quotas require.*StateStore"
    ):
        tracker.reserve("U01ALICE", agent_name="alice")


def test_reload_cannot_enable_finite_quota_without_store_or_mutate_state(
    tmp_path,
):
    from multi_app import GlobalConfig, OwnerQuotaTracker, reload_config

    tracker = OwnerQuotaTracker({}, {})
    agent = _agent(tmp_path, quota_tracker=tracker)
    gcfg = GlobalConfig(
        max_agent_rounds=8,
        github_repo=None,
        patrol_channel=None,
    )
    config = tmp_path / "agents.yaml"
    config.write_text(
        f"""
quotas:
  daily_total_tokens:
    U01ALICE: 100
  reservation_tokens:
    U01ALICE: 20
agents:
  - name: alice
    owner: U01ALICE
    node_id: alice-node
    slack_user_id: UALICEBOT
    slack_bot_id: BALICEBOT
    persona: must-not-apply
    workspace: {tmp_path}
""",
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError, match="finite owner quotas require.*StateStore"
    ):
        reload_config(str(config), [agent], gcfg)

    assert agent.cfg.persona == "local"
    assert agent.status_snapshot()["config_reload"]["pending"] is None
    assert gcfg.owner_daily_total_token_limits == {}
    assert gcfg.owner_quota_reservation_tokens == {}
    assert tracker.snapshot("U01ALICE")["daily_total_token_limit"] is None


def test_shared_owner_cannot_split_agents_across_two_nodes(tmp_path):
    from multi_app import load_credential_free_config

    roster = tmp_path / "roster.yaml"
    local = tmp_path / "agents.alice.yaml"
    roster.write_text(
        """
version: 1
agents:
  - name: alice/dev
    owner: U01ALICE
    node_id: alice-node
    slack_user_id: UALICEDEV
    slack_bot_id: BALICEDEV
  - name: alice/reviewer
    owner: U01ALICE
    node_id: other-node
    slack_user_id: UALICEREVIEW
    slack_bot_id: BALICEREVIEW
""",
        encoding="utf-8",
    )
    local.write_text(
        "node:\n  id: alice-node\nagents:\n  - name: alice/dev\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="owner U01ALICE.*multiple node"):
        load_credential_free_config(str(local), roster_path=str(roster))


def test_multiple_agents_for_owner_on_one_node_are_valid(tmp_path):
    from multi_app import load_credential_free_config

    roster = tmp_path / "roster.yaml"
    local = tmp_path / "agents.alice.yaml"
    roster.write_text(
        """
version: 1
agents:
  - name: alice/dev
    owner: U01ALICE
    node_id: alice-node
    slack_user_id: UALICEDEV
    slack_bot_id: BALICEDEV
  - name: alice/reviewer
    owner: U01ALICE
    node_id: alice-node
    slack_user_id: UALICEREVIEW
    slack_bot_id: BALICEREVIEW
""",
        encoding="utf-8",
    )
    local.write_text(
        """
node:
  id: alice-node
agents:
  - name: alice/dev
  - name: alice/reviewer
""",
        encoding="utf-8",
    )
    normalized = load_credential_free_config(
        str(local), roster_path=str(roster)
    )
    assert {
        item.node_id for item in normalized.global_config.logical_agents
    } == {"alice-node"}


def test_distinct_alice_and_bob_owners_may_use_distinct_nodes(tmp_path):
    from multi_app import load_credential_free_config

    roster = tmp_path / "roster.yaml"
    local = tmp_path / "agents.alice.yaml"
    roster.write_text(
        """
version: 1
agents:
  - name: alice/dev
    owner: U01ALICE
    node_id: alice-node
    slack_user_id: UALICEDEV
    slack_bot_id: BALICEDEV
  - name: bob/dev
    owner: U02BOB
    node_id: bob-node
    slack_user_id: UBOBDEV
    slack_bot_id: BBOBDEV
""",
        encoding="utf-8",
    )
    local.write_text(
        "node:\n  id: alice-node\nagents:\n  - name: alice/dev\n",
        encoding="utf-8",
    )
    normalized = load_credential_free_config(
        str(local), roster_path=str(roster)
    )
    assert {
        (item.owner_user_id, item.node_id)
        for item in normalized.global_config.logical_agents
    } == {
        ("U01ALICE", "alice-node"),
        ("U02BOB", "bob-node"),
    }


def test_local_owner_change_keeps_live_quota_and_auth_until_restart(
    tmp_path, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer

    from control_auth import ControlAuthenticator
    from multi_app import (
        OwnerQuotaTracker,
        Roster,
        SlackAgent,
        build_admin_app,
        load_agents_config,
        reload_config,
    )

    roster_path = tmp_path / "roster.yaml"
    local_path = tmp_path / "agents.alice.yaml"
    roster_path.write_text(
        """
version: 1
quotas:
  daily_total_tokens:
    U01ALICE: 100
  reservation_tokens:
    U01ALICE: 20
agents:
  - name: local/dev
    slack_user_id: ULOCALDEV
    slack_bot_id: BLOCALDEV
    owner: U01ALICE
    node_id: local-node
    card: old card
""",
        encoding="utf-8",
    )
    local_path.write_text(
        """
roster: roster.yaml
node:
  id: local-node
agents:
  - name: local/dev
    persona: old persona
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_DEV_SLACK_BOT_TOKEN", "xoxb-local")
    monkeypatch.setenv("LOCAL_DEV_SLACK_APP_TOKEN", "xapp-local")
    configs, gcfg = load_agents_config(str(local_path))
    store = StateStore(str(tmp_path / "live.db"))
    tracker = OwnerQuotaTracker(
        gcfg.owner_daily_total_token_limits,
        gcfg.owner_quota_reservation_tokens,
        store=store,
    )
    reservation = tracker.reserve(
        "U01ALICE", agent_name="local/dev", runtime="codex"
    )
    assert reservation is not None
    roster = Roster()
    roster.replace(gcfg.logical_agents)
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=roster,
        allowed_humans={"U01ALICE"},
        quota_tracker=tracker,
    )
    owner_token = "alice-" + ("x" * 40)
    bob_token = "bob-" + ("x" * 40)
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE"},
        admin_user_ids=set(),
        required=True,
        environ={"SLACK_AGENT_CONTROL_TOKEN_U01ALICE": owner_token},
    )

    roster_path.write_text(
        """
version: 1
quotas:
  daily_total_tokens:
    U02BOB: 200
  reservation_tokens:
    U02BOB: 40
agents:
  - name: local/dev
    slack_user_id: ULOCALDEV
    slack_bot_id: BLOCALDEV
    owner: U02BOB
    node_id: local-node
    card: new card
""",
        encoding="utf-8",
    )
    local_path.write_text(
        """
roster: roster.yaml
node:
  id: local-node
agents:
  - name: local/dev
    persona: new persona
""",
        encoding="utf-8",
    )

    report = reload_config(str(local_path), [agent], gcfg)

    assert agent.cfg.owner == "U01ALICE"
    assert agent.cfg.persona == "new persona"
    assert roster.owner_of("local/dev") == "U01ALICE"
    alice_usage = tracker.snapshot("U01ALICE")
    assert alice_usage["daily_total_token_limit"] == 100
    assert alice_usage["reservation_tokens"] == 20
    assert alice_usage["active_reserved_tokens"] == 20
    assert gcfg.owner_daily_total_token_limits == {"U01ALICE": 100}
    assert gcfg.owner_quota_reservation_tokens == {"U01ALICE": 20}
    assert "owner_quotas" not in report["global_changed"]
    assert set(report["global_restart_required"]) >= {
        "roster.local_identity",
        "control_auth",
        "owner_quotas",
    }

    async def state_for(token):
        async with TestClient(
            TestServer(build_admin_app([agent], gcfg, authenticator=auth))
        ) as client:
            response = await client.get(
                "/state",
                headers={"Authorization": f"Bearer {token}"},
            )
            return response.status, await response.text()

    alice_status, alice_body = asyncio.run(state_for(owner_token))
    bob_status, _bob_body = asyncio.run(state_for(bob_token))
    assert alice_status == 200
    assert "U01ALICE" in alice_body
    assert "U02BOB" not in alice_body
    assert bob_status == 401

    # A real restart parses the candidate owner and constructs a fresh durable
    # tracker with Bob's map; only then does the security/quota identity change.
    restarted_configs, restarted_gcfg = load_agents_config(str(local_path))
    restarted_tracker = OwnerQuotaTracker(
        restarted_gcfg.owner_daily_total_token_limits,
        restarted_gcfg.owner_quota_reservation_tokens,
        store=StateStore(str(tmp_path / "restart.db")),
    )
    assert restarted_configs[0].owner == "U02BOB"
    assert restarted_tracker.snapshot("U02BOB")[
        "daily_total_token_limit"
    ] == 200
    store.close()
