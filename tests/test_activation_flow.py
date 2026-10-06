"""Activation-level bookkeeping: context cursor, posting, reactions."""

import asyncio

import pytest

SELF_USER = "U_SELF"
SELF_BOT = "B_SELF"
HUMAN = "U_HUMAN"


def _build_agent(tmp_path, monkeypatch):
    from multi_app import Roster, SlackAgent, load_agents_config
    from multi_core import TurnBudget

    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text(
        "agents:\n  - name: dev\n    persona: x\n", encoding="utf-8"
    )
    monkeypatch.setenv("DEV_SLACK_BOT_TOKEN", "xoxb-dev")
    monkeypatch.setenv("DEV_SLACK_APP_TOKEN", "xapp-dev")
    monkeypatch.delenv("CLAUDE_WORKSPACE", raising=False)
    monkeypatch.setenv("FRESHNESS_RECHECK", "0")
    configs, _ = load_agents_config(str(yaml_path))
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
    )
    agent.user_id = SELF_USER
    agent.bot_id = SELF_BOT
    agent.team_id = "T_TEST"
    return agent


class Recorder:
    def __init__(self):
        self.turns: list[str] = []
        self.posts: list[str] = []
        self.reactions: list[dict] = []


def _wire(agent, outputs):
    """Stub the side effects of _activate_inner around a scripted runtime."""
    rec = Recorder()

    async def fake_fetch_context(*_args, **_kwargs):
        return ""

    async def no_guidance(*_args, **_kwargs):
        return ""

    async def no_files(_event):
        return ""

    async def fake_run_turn(prompt, thread_key, gen, **kwargs):
        rec.turns.append(prompt)
        response = outputs[min(len(rec.turns) - 1, len(outputs) - 1)]
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return response

    async def fake_post(_channel, _thread_ts, result):
        rec.posts.append(result)

    async def fake_reaction(_client, _channel, _ts, add=None, remove=None):
        rec.reactions.append({"add": add, "remove": remove})

    async def members(*_args, **_kwargs):
        return {"dev"}

    async def no_rollover(*_args, **_kwargs):
        return None

    agent._fetch_context = fake_fetch_context
    agent._fetch_channel_guidance = no_guidance
    agent._ingest_files = no_files
    agent._run_turn = fake_run_turn
    agent._post_result = fake_post
    agent._set_reaction = fake_reaction
    agent._channel_agent_names = members
    agent._maybe_rollover = no_rollover
    return rec


def _event(ts="101.0", thread_ts="100.0", text="please do it"):
    return {
        "channel": "C1",
        "ts": ts,
        "thread_ts": thread_ts,
        "text": f"<@{SELF_USER}> {text}",
        "user": HUMAN,
    }


async def _say(**_kwargs):
    return None


def _activate(agent, event):
    asyncio.run(agent._activate_inner(event, object(), _say))


# ---------------------------------------------------------------------------
# Context cursor (last_seen)
# ---------------------------------------------------------------------------


def test_successful_turn_advances_context_cursor(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _wire(agent, ["done"])
    _activate(agent, _event(ts="101.0"))
    assert agent.last_seen["C1:100.0"] == "101.0"


@pytest.mark.parametrize(
    "failure", [RuntimeError("boom"), TimeoutError()], ids=["error", "timeout"]
)
def test_failed_turn_keeps_context_cursor(tmp_path, monkeypatch, failure):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.last_seen["C1:100.0"] = "100.0"
    rec = _wire(agent, [failure])
    _activate(agent, _event(ts="101.0"))
    # The failed question stays visible to the next activation's context.
    assert agent.last_seen["C1:100.0"] == "100.0"
    assert len(rec.posts) == 1


def test_failed_turn_on_new_thread_sets_no_cursor(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _wire(agent, [RuntimeError("boom")])
    _activate(agent, _event(ts="101.0"))
    assert "C1:100.0" not in agent.last_seen


def test_mid_turn_reset_leaves_cursor_cleared(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    generations = iter([(0, 0)])
    monkeypatch.setattr(
        agent, "_turn_generation", lambda _key: next(generations, (1, 1))
    )
    _wire(agent, ["done"])
    _activate(agent, _event(ts="101.0"))
    assert "C1:100.0" not in agent.last_seen


# ---------------------------------------------------------------------------
# _post_result: fall back only when Slack refused the post
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, first_error):
        self.first_error = first_error
        self.calls: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1 and self.first_error is not None:
            raise self.first_error
        return {"message": {"text": kwargs.get("markdown_text") or kwargs.get("text")}}


def _post_with(agent, client):
    from types import SimpleNamespace

    agent.app = SimpleNamespace(client=client)
    return asyncio.run(agent._post_result("C1", "100.0", "short reply"))


def test_post_falls_back_to_text_when_slack_refuses_markdown(
    tmp_path, monkeypatch
):
    from slack_sdk.errors import SlackApiError

    agent = _build_agent(tmp_path, monkeypatch)
    client = _FakeClient(
        SlackApiError("invalid_arguments", {"ok": False, "error": "invalid_arguments"})
    )
    _post_with(agent, client)
    assert len(client.calls) == 2
    assert client.calls[1]["text"] == "short reply"


def test_post_does_not_repost_when_delivery_is_unknown(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    client = _FakeClient(asyncio.TimeoutError())
    with pytest.raises(asyncio.TimeoutError):
        _post_with(agent, client)
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Socket delivery gaps invalidate warm transcripts
# ---------------------------------------------------------------------------


def test_live_coverage_monitor_flags_gap_once():
    from multi_core import LiveCoverageMonitor

    monitor = LiveCoverageMonitor(120.0)
    assert monitor.observe(0.0, True) is False
    assert monitor.observe(10.0, True) is False
    assert monitor.observe(20.0, False) is False
    assert monitor.observe(100.0, False) is False
    # Up again 125s after it was last seen up: a gap.
    assert monitor.observe(135.0, True) is True
    assert monitor.observe(145.0, True) is False
    # Host sleep: no samples at all for a long time.
    assert monitor.observe(1000.0, True) is True


def test_mark_coverage_gap_forces_revalidation(tmp_path):
    from transcript_store import TranscriptStore

    store = TranscriptStore(None)
    key = ("T1", "C1", "100.0")
    state = store._load_thread(key)
    state.complete = True
    old_boot = store.boot_id
    assert store.mark_coverage_gap() == 1
    assert store.boot_id != old_boot
    assert state.needs_revalidate is True
    assert store.read_thread(*key).complete is False
    assert store.status_snapshot()["coverage_gaps"] == 1


def test_watch_live_coverage_marks_gap_after_reconnect():
    from types import SimpleNamespace

    from multi_app import watch_live_coverage

    states = iter([True, False, False, True])
    times = iter([0.0, 50.0, 100.0, 200.0])
    marks: list[int] = []

    class FakeClient:
        async def is_connected(self):
            return next(states)

    store = SimpleNamespace(mark_coverage_gap=lambda: marks.append(1) or 0)

    async def scenario():
        task = asyncio.create_task(
            watch_live_coverage(
                [SimpleNamespace(client=FakeClient())],
                store,
                threshold_seconds=120.0,
                sample_seconds=0,
                clock=lambda: next(times),
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if marks:
                break
        task.cancel()
        with pytest.raises((asyncio.CancelledError, StopIteration, RuntimeError)):
            await task

    asyncio.run(scenario())
    assert marks == [1]


def test_socket_gap_threshold_env(monkeypatch):
    from multi_app import socket_gap_threshold_from_env

    monkeypatch.delenv("SOCKET_GAP_REVALIDATE_SECONDS", raising=False)
    assert socket_gap_threshold_from_env() == 120.0
    monkeypatch.setenv("SOCKET_GAP_REVALIDATE_SECONDS", "300")
    assert socket_gap_threshold_from_env() == 300.0
    for bad in ("abc", "5"):
        monkeypatch.setenv("SOCKET_GAP_REVALIDATE_SECONDS", bad)
        with pytest.raises(RuntimeError):
            socket_gap_threshold_from_env()


# ---------------------------------------------------------------------------
# Thread root pinning in truncated context
# ---------------------------------------------------------------------------


def _context_messages():
    root = {"ts": "1.0", "text": "TASK: migrate the billing table"}
    replies = [{"ts": f"{i}.0", "text": f"reply {i} " * 12} for i in range(2, 60)]
    return [root, *replies]


def test_truncated_context_keeps_thread_root():
    from multi_core import THREAD_CONTEXT_MIDDLE_OMITTED, format_thread_context

    block = format_thread_context(
        _context_messages(), lambda _m: "h", "", max_chars=800, pin_root_ts="1.0"
    )
    assert block.startswith("[h] TASK: migrate the billing table")
    assert THREAD_CONTEXT_MIDDLE_OMITTED in block
    assert "reply 59" in block
    assert len(block) <= 800


def test_truncation_without_pinned_root_keeps_legacy_shape():
    from multi_core import THREAD_CONTEXT_OMITTED, format_thread_context

    # The root was already consumed by the session (filtered out).
    block = format_thread_context(
        _context_messages()[5:], lambda _m: "h", "", max_chars=800, pin_root_ts="1.0"
    )
    assert block.startswith(THREAD_CONTEXT_OMITTED)
    assert "TASK" not in block


def test_oversized_root_is_clipped():
    from multi_core import format_thread_context

    messages = [{"ts": "1.0", "text": "R" * 5000}, {"ts": "2.0", "text": "latest"}]
    block = format_thread_context(
        messages, lambda _m: "h", "", max_chars=900, pin_root_ts="1.0"
    )
    assert block.endswith("[h] latest")
    assert len(block) <= 900


def test_short_context_is_unchanged():
    from multi_core import format_thread_context

    messages = [{"ts": "1.0", "text": "root"}, {"ts": "2.0", "text": "reply"}]
    assert (
        format_thread_context(messages, lambda _m: "h", "", pin_root_ts="1.0")
        == "[h] root\n[h] reply"
    )


# ---------------------------------------------------------------------------
# Plain-text @agent mentions
# ---------------------------------------------------------------------------


def test_plain_agent_mentions_detection():
    from multi_core import plain_agent_mentions

    ids = {"reviewer": "UREV01", "dev": "UDEV01", "grok-reviewer": "UGROK01"}
    assert plain_agent_mentions("please @reviewer check", ids) == ["reviewer"]
    assert plain_agent_mentions("@reviewer、お願いします", ids) == ["reviewer"]
    assert plain_agent_mentions("ping @grok-reviewer.", ids) == ["grok-reviewer"]
    # Real mentions, code, and e-mail addresses are not plain mentions.
    assert plain_agent_mentions("<@UREV01> and @reviewer", ids) == []
    assert plain_agent_mentions("run `@dev deploy`", ids) == []
    assert plain_agent_mentions("mail ops@reviewer.example", ids) == []
    assert plain_agent_mentions("@someone-else", ids) == []


def test_post_result_flags_plain_agent_mention(tmp_path, monkeypatch):
    from types import SimpleNamespace

    agent = _build_agent(tmp_path, monkeypatch)
    agent.roster.add("reviewer", "U_REV", "B_REV")
    client = _FakeClient(None)
    agent.app = SimpleNamespace(client=client)

    async def members(*_args, **_kwargs):
        return {"dev", "reviewer"}

    agent._channel_agent_names = members
    asyncio.run(agent._post_result("C1", "100.0", "done, @reviewer please look"))
    posted = client.calls[0]["markdown_text"]
    assert "`@reviewer`" in posted
    assert "通知されていません" in posted


# ---------------------------------------------------------------------------
# Runtime failure classification + patrol fence
# ---------------------------------------------------------------------------


def test_classify_runtime_failure():
    from multi_core import classify_runtime_failure

    assert classify_runtime_failure("Claude error result: Prompt is too long") == (
        "context_too_long"
    )
    assert classify_runtime_failure("Invalid API key · Please run /login") == "auth"
    assert classify_runtime_failure("Credit balance is too low") == "billing"
    assert classify_runtime_failure("boom") == "other"


def test_failed_turn_posts_actionable_notice(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, [RuntimeError("Claude error result: Prompt is too long")])
    _activate(agent, _event())
    assert "!reset" in rec.posts[0]
    assert agent._last_failure["category"] == "context_too_long"


def test_failure_fence_opens_after_threshold_and_probes():
    from multi_core import FailureFence

    fence = FailureFence(threshold=3, probe_every=3)
    assert fence.note_failure("auth") is False
    assert fence.note_failure("other") is False  # category change resets
    assert fence.note_failure("auth") is False
    assert fence.note_failure("auth") is False
    assert fence.note_failure("auth") is True  # third auth in a row
    assert fence.note_failure("auth") is False  # announced only once
    assert [fence.should_skip() for _ in range(3)] == [True, True, False]
    fence.note_success()
    assert fence.should_skip() is False
    assert fence.snapshot()["open"] is False


def test_patrol_pauses_after_repeated_auth_failures(tmp_path, monkeypatch):
    from types import SimpleNamespace

    agent = _build_agent(tmp_path, monkeypatch)
    client = _FakeClient(None)
    agent.app = SimpleNamespace(client=client)
    runs: list[int] = []

    async def failing_inner(*_args, **_kwargs):
        runs.append(1)
        raise RuntimeError("Not logged in · Please run /login")

    agent._run_patrol_once_inner = failing_inner
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(agent._run_patrol_once("p", "C-PATROL"))
    assert len(runs) == 3
    assert len(client.calls) == 1
    assert "巡回を一時停止" in client.calls[0]["text"]
    # The next round is skipped without touching the provider.
    asyncio.run(agent._run_patrol_once("p", "C-PATROL"))
    assert len(runs) == 3
    assert agent.status_snapshot()["patrol_fence"]["open"] is True


# ---------------------------------------------------------------------------
# Queue / cooldown visibility
# ---------------------------------------------------------------------------


def test_waiting_trigger_shows_inbox_then_hourglass(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["done"])
    event = _event()
    plan = agent.build_execution_plan(event)

    async def scenario():
        lock = agent.locks.setdefault(plan.thread_key, asyncio.Lock())
        await lock.acquire()  # another turn holds the thread
        await agent._mark_queued(event, object(), plan, None)
        lock.release()
        await agent._activate_inner(event, object(), _say)

    asyncio.run(scenario())
    assert rec.reactions[0] == {"add": "inbox_tray", "remove": None}
    assert rec.reactions[1] == {
        "add": "hourglass_flowing_sand",
        "remove": "inbox_tray",
    }


def test_idle_trigger_skips_inbox_reaction(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["done"])
    event = _event()
    plan = agent.build_execution_plan(event)
    asyncio.run(agent._mark_queued(event, object(), plan, None))
    assert rec.reactions == []


def test_long_cooldown_wait_is_announced_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import multi_app

    agent = _build_agent(tmp_path, monkeypatch)
    client = _FakeClient(None)
    agent.app = SimpleNamespace(client=client)
    plan = agent.build_execution_plan(_event())
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(multi_app.asyncio, "sleep", fake_sleep)

    async def scenario():
        token = multi_app._CURRENT_EXECUTION_PLAN.set(plan)
        try:
            await agent._wait_out_cooldown(45.0, plan.thread_key)
            await agent._wait_out_cooldown(5.0, plan.thread_key)
        finally:
            multi_app._CURRENT_EXECUTION_PLAN.reset(token)

    asyncio.run(scenario())
    assert slept == [45.0, 5.0]
    assert len(client.calls) == 1
    assert "約 45 秒後" in client.calls[0]["text"]
    assert client.calls[0]["thread_ts"] == plan.root_thread_ts


# ---------------------------------------------------------------------------
# Batching triggers that queued in one thread
# ---------------------------------------------------------------------------


def test_claim_trigger_batch_absorbs_queued_triggers(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    first, second, third = (
        _event(ts="101.0"),
        _event(ts="102.0"),
        _event(ts="103.0"),
    )
    for event in (first, second, third):
        agent._register_pending_trigger("C1:100.0", event)
    batch = agent._claim_trigger_batch("C1:100.0", second)
    assert [e["ts"] for e in batch] == ["101.0", "102.0", "103.0"]
    assert agent._claim_trigger_batch("C1:100.0", first) == []
    assert agent._claim_trigger_batch("C1:100.0", third) == []
    assert agent._pending_triggers == {}
    assert agent._absorbed_triggers == set()


def test_unregistered_trigger_is_its_own_batch(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    event = _event(ts="101.0")
    assert agent._claim_trigger_batch("C1:100.0", event) == [event]


def test_forget_trigger_drops_stale_bookkeeping(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    event = _event(ts="101.0")
    agent._register_pending_trigger("C1:100.0", event)
    agent._forget_trigger("C1:100.0", event)
    assert agent._pending_triggers == {}


def test_queued_triggers_are_answered_in_one_turn(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["one combined answer"])
    first = _event(ts="101.0", text="add the export button")
    second = _event(ts="102.0", text="also make it CSV")
    agent._register_pending_trigger("C1:100.0", first)
    agent._register_pending_trigger("C1:100.0", second)
    excluded: list[frozenset] = []

    async def fake_fetch_context(*_args, exclude_ts, **_kwargs):
        excluded.append(exclude_ts)
        return ""

    agent._fetch_context = fake_fetch_context
    _activate(agent, first)
    _activate(agent, second)  # already answered: no second turn
    assert len(rec.turns) == 1
    assert "add the export button" in rec.turns[0]
    assert "also make it CSV" in rec.turns[0]
    assert excluded == [frozenset({"101.0", "102.0"})]
    assert rec.posts == ["one combined answer"]
    done = [r for r in rec.reactions if r["add"] == "white_check_mark"]
    assert len(done) == 2
    # The cursor moves to the newest answered trigger.
    assert agent.last_seen["C1:100.0"] == "102.0"


def test_absorbed_trigger_does_not_get_inbox_reaction(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["done"])
    first, second = _event(ts="101.0"), _event(ts="102.0")
    agent._register_pending_trigger("C1:100.0", first)
    agent._register_pending_trigger("C1:100.0", second)
    agent._claim_trigger_batch("C1:100.0", first)
    plan = agent.build_execution_plan(second)

    async def scenario():
        lock = agent.locks.setdefault(plan.thread_key, asyncio.Lock())
        await lock.acquire()
        try:
            await agent._mark_queued(second, object(), plan, None)
        finally:
            lock.release()

    asyncio.run(scenario())
    assert rec.reactions == []


# ---------------------------------------------------------------------------
# Activation ledger: restart reports cut-off work instead of hiding it
# ---------------------------------------------------------------------------


def test_activation_ledger_take_is_scoped_and_bounded(tmp_path):
    from state_store import StateStore

    store = StateStore(str(tmp_path / "ledger.db"))
    for index in range(5):
        store.record_activation(
            "dev",
            team_id="T1",
            channel_id="C1",
            trigger_ts=f"10{index}.0",
            thread_ts="100.0",
            now=1000.0 + index,
        )
    store.record_activation(
        "dev", team_id="T1", channel_id="C1", trigger_ts="1.0", thread_ts="",
        now=1.0,
    )
    store.record_activation(
        "dev", team_id="T2", channel_id="C9", trigger_ts="9.0", thread_ts="",
        now=1000.0,
    )
    store.record_activation(
        "qa", team_id="T1", channel_id="C1", trigger_ts="8.0", thread_ts="",
        now=1000.0,
    )
    store.clear_activation("dev", channel_id="C1", trigger_ts="104.0")
    rows = store.take_interrupted_activations(
        "dev", team_id="T1", max_age_seconds=3600, limit=3, now=1010.0
    )
    assert [row["trigger_ts"] for row in rows] == ["103.0", "102.0", "101.0"]
    # Everything for (dev, T1) is consumed, including stale/excess rows.
    assert store.take_interrupted_activations(
        "dev", team_id="T1", max_age_seconds=3600, limit=10, now=1010.0
    ) == []
    assert len(
        store.take_interrupted_activations(
            "dev", team_id="T2", max_age_seconds=3600, limit=10, now=1010.0
        )
    ) == 1
    store.close()


def test_restart_reconciliation_reports_interrupted_activation(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from state_store import StateStore

    agent = _build_agent(tmp_path, monkeypatch)
    store = StateStore(str(tmp_path / "ledger.db"))
    agent._store = store
    event = _event(ts="101.0")
    plan = agent.build_execution_plan(event)
    agent._ledger_record(event, plan)

    reactions: list[dict] = []

    async def fake_reaction(_client, channel, ts, add=None, remove=None):
        reactions.append({"ts": ts, "add": add, "remove": remove})

    agent._set_reaction = fake_reaction
    client = _FakeClient(None)
    agent.app = SimpleNamespace(client=client)
    assert asyncio.run(agent.reconcile_interrupted_activations()) == 1
    assert {"ts": "101.0", "add": "warning", "remove": "inbox_tray"} in reactions
    assert client.calls[0]["thread_ts"] == "100.0"
    assert "中断されました" in client.calls[0]["text"]
    # Reported once only.
    assert asyncio.run(agent.reconcile_interrupted_activations()) == 0
    store.close()


def test_finished_activation_leaves_no_ledger_row(tmp_path, monkeypatch):
    from state_store import StateStore

    agent = _build_agent(tmp_path, monkeypatch)
    store = StateStore(str(tmp_path / "ledger.db"))
    agent._store = store
    event = _event(ts="101.0")
    agent._ledger_record(event, agent.build_execution_plan(event))
    agent._ledger_clear(event)
    assert store.take_interrupted_activations(
        "dev", team_id="T_TEST", max_age_seconds=3600, limit=10
    ) == []
    store.close()


def test_batched_turn_clears_ledger_rows_of_absorbed_triggers(
    tmp_path, monkeypatch
):
    from state_store import StateStore

    agent = _build_agent(tmp_path, monkeypatch)
    store = StateStore(str(tmp_path / "ledger.db"))
    agent._store = store
    _wire(agent, ["answer"])
    first, second = _event(ts="101.0"), _event(ts="102.0")
    for event in (first, second):
        agent._register_pending_trigger("C1:100.0", event)
        agent._ledger_record(event, agent.build_execution_plan(event))
    _activate(agent, first)
    rows = store.take_interrupted_activations(
        "dev", team_id="T_TEST", max_age_seconds=3600, limit=10
    )
    # Only the holder's own row remains until its task callback clears it.
    assert [row["trigger_ts"] for row in rows] == ["101.0"]
    store.close()


# ---------------------------------------------------------------------------
# Operator stop (console)
# ---------------------------------------------------------------------------

def test_stop_cancels_running_work_and_tells_each_thread(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    started = asyncio.Event()

    async def endless():
        started.set()
        await asyncio.sleep(3600)

    rec = _wire(agent, [endless])

    async def fake_run_turn(prompt, thread_key, gen, **kwargs):
        rec.turns.append(prompt)
        await endless()

    agent._run_turn = fake_run_turn
    notices = []

    async def say(**kwargs):
        notices.append(kwargs)

    async def scenario():
        events = [_event(ts="101.0"), _event(ts="102.0")]  # one thread, two triggers
        for event in events:
            task = asyncio.create_task(agent._activate_inner(event, object(), say))
            agent._tasks.add(task)
            agent._task_triggers[task] = (event, object(), say)
        await started.wait()
        result = await agent.stop()
        return result, events

    result, _events = asyncio.run(scenario())
    assert result == {"cancelled": 2, "patrol_cancelled": 0}
    assert agent.paused is True
    assert rec.posts == []  # nothing half-finished was posted
    assert len(notices) == 1 and notices[0]["thread_ts"] == "100.0"
    assert "中断" in notices[0]["text"]
    assert {"add": "black_square_for_stop", "remove": None} in rec.reactions
    assert {"add": None, "remove": "hourglass_flowing_sand"} in rec.reactions
    assert not any(r["add"] == "x" for r in rec.reactions)  # stopped, not failed


def test_stop_survives_a_restart_until_resumed(tmp_path, monkeypatch):
    from multi_app import Roster, SlackAgent, load_agents_config
    from multi_core import TurnBudget
    from state_store import StateStore

    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text("agents:\n  - name: dev\n    persona: x\n", encoding="utf-8")
    monkeypatch.setenv("DEV_SLACK_BOT_TOKEN", "xoxb-dev")
    monkeypatch.setenv("DEV_SLACK_APP_TOKEN", "xapp-dev")
    configs, _ = load_agents_config(str(yaml_path))

    def boot(store):
        return SlackAgent(
            configs[0], budget=TurnBudget(8), roster=Roster(),
            allowed_humans=set(), store=store,
        )

    store = StateStore(str(tmp_path / "state.db"))
    boot(store).set_paused(True)
    store.close()
    store = StateStore(str(tmp_path / "state.db"))
    agent = boot(store)
    assert agent.paused is True
    agent.resume()
    store.close()
    store = StateStore(str(tmp_path / "state.db"))
    assert boot(store).paused is False
    store.close()


def test_stopped_patrol_round_hands_the_issue_back(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls, posts = [], []

    async def claim_tool(action, *, repo, issue, config, stale_only=False):
        calls.append((action, issue))
        return {"status": "released"}

    async def fake_post(_channel, _thread_ts, text):
        posts.append(text)

    agent._run_claim_tool = claim_tool
    agent._post_result = fake_post
    agent.paused = True
    asyncio.run(agent._release_stopped_claim("acme/widgets", 7, agent.cfg, "C-PATROL"))
    assert calls == [("release", 7)]
    assert "#7" in posts[0] and "todo" in posts[0]


def _stream_then_hang(monkeypatch, session_id, started):
    import multi_app
    from claude_agent_sdk import SystemMessage

    async def fake_query(*, prompt, options):
        yield SystemMessage(subtype="init", data={"session_id": session_id})
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(multi_app, "query", fake_query)


@pytest.mark.parametrize("operator_stop", [True, False])
def test_interrupted_first_turn_keeps_its_session_only_on_operator_stop(
    tmp_path, monkeypatch, operator_stop
):
    """A stop keeps the new session for the next turn; other cancels do not."""
    agent = _build_agent(tmp_path, monkeypatch)
    started = asyncio.Event()
    _stream_then_hang(monkeypatch, "sess-first", started)
    thread_key = "C1:100.0"

    async def scenario():
        turn = asyncio.create_task(
            agent._run_claude("p", thread_key, agent._turn_generation(thread_key))
        )
        await started.wait()
        if operator_stop:
            agent.set_paused(True)
            agent._operator_cancelled.add(turn)
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)

    asyncio.run(scenario())
    assert agent.sessions.get(thread_key) == ("sess-first" if operator_stop else None)


def test_turn_after_a_stop_is_told_not_to_pick_the_work_back_up(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    started = asyncio.Event()
    replies = iter(["hang", "fail", "ok", "ok"])

    rec = _wire(agent, ["unused"])

    async def fake_run_turn(prompt, thread_key, gen, **kwargs):
        rec.turns.append(prompt)
        reply = next(replies)
        if reply == "hang":
            started.set()
            await asyncio.sleep(3600)
        if reply == "fail":
            raise RuntimeError("provider blew up")
        return "done"

    agent._run_turn = fake_run_turn

    async def scenario():
        event = _event(ts="101.0")
        task = asyncio.create_task(agent._activate_inner(event, object(), _say))
        agent._tasks.add(task)
        agent._task_triggers[task] = (event, object(), _say)
        await started.wait()
        await agent.stop()
        agent.resume()
        for ts in ("103.0", "105.0", "107.0"):
            await agent._activate_inner(_event(ts=ts), object(), _say)

    asyncio.run(scenario())
    note = "操作者が途中で停止しました"
    assert note not in rec.turns[0]
    assert note in rec.turns[1]  # the first turn after the stop (it fails)
    assert note in rec.turns[2]  # so the retry still carries it
    assert note not in rec.turns[3]  # cleared once a turn succeeded


# ---------------------------------------------------------------------------
# Slack identity (what Slack shows for the bot)
# ---------------------------------------------------------------------------

def test_slack_identity_reads_the_shown_name_not_the_app_name():
    from multi_core import slack_identity

    user = {"name": "ai_agent_luming", "profile": {"real_name": "dev", "display_name": ""}}
    bot = {"name": "Agent (developer)", "app_id": "A0BJRUTHFV5"}
    assert slack_identity(user, bot) == {
        "display_name": "dev",
        "username": "ai_agent_luming",
        "app_name": "Agent (developer)",
        "app_id": "A0BJRUTHFV5",
        "app_home_url": "https://api.slack.com/apps/A0BJRUTHFV5/app-home",
    }
    renamed = {"name": "x", "profile": {"real_name": "dev", "display_name": "developer"}}
    assert slack_identity(renamed, bot)["display_name"] == "developer"
    odd = slack_identity(user, {"app_id": "javascript:alert(1)"})
    assert odd["app_id"] == "" and odd["app_home_url"] == ""


def test_sync_rereads_the_bot_profile_from_slack(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    names = iter(["dev", "developer"])

    class Client:
        async def users_info(self, user):
            assert user == SELF_USER
            return {"user": {"name": "dev", "profile": {"real_name": next(names)}}}

        async def bots_info(self, bot):
            assert bot == SELF_BOT
            return {"bot": {"name": "Agent (dev)", "app_id": "A0TESTAPP1"}}

    class App:
        client = Client()

    agent.app = App()
    assert asyncio.run(agent.refresh_slack_identity())["display_name"] == "dev"
    assert asyncio.run(agent.refresh_slack_identity())["display_name"] == "developer"
    assert agent.status_snapshot()["slack"]["display_name"] == "developer"


def test_status_reports_stopped_apart_from_cooldown_paused_admissions(
    tmp_path, monkeypatch
):
    """The limiter's "paused" (cooldown waits) must not mask or fake a stop."""
    agent = _build_agent(tmp_path, monkeypatch)
    snapshot = agent.status_snapshot()
    assert snapshot["stopped"] is False and "paused" in snapshot
    agent.set_paused(True)
    snapshot = agent.status_snapshot()
    assert snapshot["stopped"] is True
    assert snapshot["paused"] == 0  # still the limiter's count


def test_resume_before_a_cancelled_task_unwinds_still_counts_as_a_stop(
    tmp_path, monkeypatch
):
    """Cleanup asks whether its own task was stopped, not the live flag."""
    agent = _build_agent(tmp_path, monkeypatch)
    seen = []
    started = asyncio.Event()

    async def work():
        try:
            started.set()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # Resume lands while this unwinds
            seen.append(agent._stopped_by_operator())
            raise

    async def scenario():
        task = asyncio.create_task(work())
        agent._tasks.add(task)
        await started.wait()
        stopping = asyncio.create_task(agent.stop())
        await asyncio.sleep(0)
        agent.resume()
        await stopping

    asyncio.run(scenario())
    assert seen == [True]
    assert agent.paused is False


def test_stop_replies_within_its_bound_when_work_is_slow_to_unwind(
    tmp_path, monkeypatch
):
    import time as _time

    import multi_app

    monkeypatch.setattr(multi_app, "STOP_WAIT_SECONDS", 0.05)
    agent = _build_agent(tmp_path, monkeypatch)
    started = asyncio.Event()

    async def stubborn():
        try:
            started.set()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)
            raise

    async def scenario():
        task = asyncio.create_task(stubborn())
        agent._tasks.add(task)
        await started.wait()
        begin = _time.monotonic()
        result = await agent.stop()
        elapsed = _time.monotonic() - begin
        await asyncio.gather(task, return_exceptions=True)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result["cancelled"] == 1
    assert elapsed < 0.8


def test_stop_note_survives_a_restart(tmp_path, monkeypatch):
    from multi_app import Roster, SlackAgent, load_agents_config
    from multi_core import TurnBudget
    from state_store import StateStore

    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text("agents:\n  - name: dev\n    persona: x\n", encoding="utf-8")
    monkeypatch.setenv("DEV_SLACK_BOT_TOKEN", "xoxb-dev")
    monkeypatch.setenv("DEV_SLACK_APP_TOKEN", "xapp-dev")
    configs, _ = load_agents_config(str(yaml_path))

    def boot(store):
        return SlackAgent(
            configs[0], budget=TurnBudget(8), roster=Roster(),
            allowed_humans=set(), store=store,
        )

    store = StateStore(str(tmp_path / "state.db"))
    boot(store)._mark_thread_stopped("C1:100.0")
    store.close()
    store = StateStore(str(tmp_path / "state.db"))
    agent = boot(store)
    assert "C1:100.0" in agent._stopped_threads
    agent._clear_thread_stopped("C1:100.0")
    store.close()
    store = StateStore(str(tmp_path / "state.db"))
    assert boot(store)._stopped_threads == set()
    store.close()


def test_stop_note_stays_when_the_reply_could_not_be_posted(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["done"])
    agent._mark_thread_stopped("C1:100.0")
    attempts = []

    async def flaky_post(_channel, _thread_ts, result):
        attempts.append(result)
        if len(attempts) == 1:
            raise RuntimeError("slack is down")
        rec.posts.append(result)

    agent._post_result = flaky_post
    _activate(agent, _event(ts="101.0"))  # the failed post is logged, not raised
    assert "C1:100.0" in agent._stopped_threads  # nothing reached the thread
    _activate(agent, _event(ts="103.0"))
    assert "C1:100.0" not in agent._stopped_threads
    assert "操作者が途中で停止しました" in rec.turns[1]


def test_slack_name_is_reread_periodically(tmp_path, monkeypatch):
    import multi_app

    monkeypatch.setattr(multi_app, "SLACK_IDENTITY_REFRESH_SECONDS", 0.01)
    agent = _build_agent(tmp_path, monkeypatch)
    calls = []

    async def refresh():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("slack hiccup")  # a failure never ends the loop
        return {}

    agent.refresh_slack_identity = refresh

    async def scenario():
        loop = asyncio.create_task(agent.slack_identity_loop())
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) >= 3


def test_sigterm_cancels_the_main_task_instead_of_killing_the_process():
    """make stop sends SIGTERM; running turns must unwind and reap their children."""
    import signal

    import multi_app

    registered = {}

    class Loop:
        def add_signal_handler(self, sig, callback):
            registered[sig] = callback

    async def scenario():
        main = asyncio.create_task(asyncio.sleep(3600))
        multi_app.install_shutdown_signals(Loop(), main)
        registered[signal.SIGTERM]()  # what the loop calls on SIGTERM
        try:
            await main
        except asyncio.CancelledError:
            return "cancelled"
        return "kept running"

    assert asyncio.run(scenario()) == "cancelled"


def test_shutdown_signals_tolerate_loops_without_signal_support():
    import multi_app

    class Loop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        multi_app.install_shutdown_signals(Loop(), task)  # must not raise
        await task

    asyncio.run(scenario())


def test_shutdown_closes_every_connection_even_if_one_fails():
    import multi_app

    closed = []

    class Handler:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def close_async(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("socket already gone")

    class Agent:
        def __init__(self, name):
            self.name = name

        async def close_client(self):
            closed.append(self.name)

    asyncio.run(
        multi_app.close_slack_connections(
            [Handler("socket-a", fail=True), Handler("socket-b")],
            [Agent("dev"), Agent("reviewer")],
        )
    )
    assert closed == ["socket-a", "socket-b", "dev", "reviewer"]


def test_shutdown_cancels_running_turns_before_closing_slack():
    """A turn's children are reaped before shutdown spends time on Slack."""
    import multi_app

    order = []

    async def scenario():
        async def turn():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("turn reaped its children")
                raise

        class Agent:
            name = "dev"

            def __init__(self):
                self._tasks = {asyncio.create_task(turn())}
                self._patrol_round = None

            async def close_client(self):
                order.append("slack closed")

        agent = Agent()
        await asyncio.sleep(0)
        await multi_app.cancel_running_turns([agent])
        await multi_app.close_slack_connections([], [agent])

    asyncio.run(scenario())
    assert order == ["turn reaped its children", "slack closed"]


def test_shut_down_takes_no_new_work_and_reaps_turns_before_closing_slack(
    tmp_path, monkeypatch
):
    """Ordered stop: loops and turns unwind first; a late trigger is noted, not run."""
    import multi_app
    from state_store import StateStore

    agent = _build_agent(tmp_path, monkeypatch)
    agent._store = StateStore(str(tmp_path / "state.db"))
    _wire(agent, ["unused"])
    order = []

    async def scenario():
        async def turn():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("turn reaped")
                raise

        async def background_loop():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("loop cancelled")
                raise

        agent._tasks.add(asyncio.create_task(turn()))
        loop_task = asyncio.ensure_future(background_loop())
        await asyncio.sleep(0)

        async def close_client():
            # A Slack event still arriving while connections close starts nothing.
            before = {t for t in agent._tasks if not t.done()}
            await agent._on_message({"event_id": "late"}, _event(ts="200.0"), object(), _say)
            started = {t for t in agent._tasks if not t.done()} - before
            order.append(f"slack closed, {len(started)} new turn(s)")

        agent.close_client = close_client
        await multi_app.shut_down([loop_task], [], [agent])

    asyncio.run(scenario())
    assert order == ["loop cancelled", "turn reaped", "slack closed, 0 new turn(s)"]
    # The late trigger is in the ledger, so the next start tells the thread to resend it.
    rows = agent._store.take_interrupted_activations(
        "dev", team_id="T_TEST", max_age_seconds=3600, limit=10
    )
    assert [row["trigger_ts"] for row in rows] == ["200.0"]
    agent._store.close()
