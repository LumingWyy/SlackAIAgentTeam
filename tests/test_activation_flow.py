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
