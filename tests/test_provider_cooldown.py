"""Unit tests for the provider rate-limit cooldown (multi_core + multi_app)."""

import asyncio

import pytest

from multi_core import (
    ProviderCooldown,
    ProviderRateLimitedError,
    is_rate_limit_signal,
    parse_codex_events,
)


# ---------------------------------------------------------------------------
# is_rate_limit_signal
# ---------------------------------------------------------------------------


def test_rate_limit_signal_positives():
    for text in (
        "Rate limit reached for gpt-5.6-sol",
        "rate_limit_error",
        "HTTP 429 Too Many Requests",
        "Error code: 429 - {'error': {'message': 'Rate limit reached'}}",
        "overloaded_error: Anthropic API is overloaded",
        "Claude AI usage limit reached|1756702800",
        "You've hit your usage limit",
        "RESOURCE_EXHAUSTED",
        "quota exceeded for this billing period",
    ):
        assert is_rate_limit_signal(text), text


def test_rate_limit_signal_negatives():
    for text in (
        "",
        "connection refused",
        "file not found",
        "issue 4290 closed",  # \b429\b must not match inside 4290
        "fixed bug in module429x",
    ):
        assert not is_rate_limit_signal(text), text


# ---------------------------------------------------------------------------
# ProviderCooldown
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _cooldown(**kwargs) -> tuple[ProviderCooldown, _FakeClock]:
    clock = _FakeClock()
    return ProviderCooldown(clock=clock, **kwargs), clock


def test_cooldown_doubles_and_caps():
    cd, _clock = _cooldown(base_seconds=60.0, max_seconds=480.0)
    assert cd.note_rate_limited() == 60.0
    assert cd.note_rate_limited() == 120.0
    assert cd.note_rate_limited() == 240.0
    assert cd.note_rate_limited() == 480.0
    assert cd.note_rate_limited() == 480.0  # capped
    assert cd.strikes == 5


def test_cooldown_success_resets_streak_and_deadline():
    cd, _clock = _cooldown(base_seconds=60.0, max_seconds=480.0)
    cd.note_rate_limited()
    cd.note_rate_limited()
    assert cd.remaining() > 0
    cd.note_success()
    assert cd.remaining() == 0.0
    assert cd.strikes == 0
    assert cd.note_rate_limited() == 60.0  # back to base


def test_cooldown_remaining_tracks_clock():
    cd, clock = _cooldown(base_seconds=60.0, max_seconds=480.0)
    cd.note_rate_limited()
    assert cd.remaining() == pytest.approx(60.0)
    clock.now += 45.0
    assert cd.remaining() == pytest.approx(15.0)
    clock.now += 30.0
    assert cd.remaining() == 0.0


def test_cooldown_honors_larger_retry_after_up_to_hard_cap():
    cd, _clock = _cooldown(
        base_seconds=60.0, max_seconds=480.0, hard_cap_seconds=900.0
    )
    assert cd.note_rate_limited(retry_after=600.0) == 600.0
    cd.note_success()
    assert cd.note_rate_limited(retry_after=30.0) == 60.0  # smaller ignored
    cd.note_success()
    assert cd.note_rate_limited(retry_after=5000.0) == 900.0  # hard cap


def test_cooldown_deadline_never_moves_backwards():
    cd, clock = _cooldown(base_seconds=60.0, max_seconds=480.0)
    cd.note_rate_limited(retry_after=600.0)
    clock.now += 1.0
    cd.note_rate_limited()  # 120s doubling < 599s already armed
    assert cd.remaining() == pytest.approx(599.0)


def test_cooldown_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        ProviderCooldown(base_seconds=0)
    with pytest.raises(ValueError):
        ProviderCooldown(base_seconds=10, max_seconds=5)


def test_cooldown_snapshot_shape():
    cd, _clock = _cooldown(base_seconds=60.0, max_seconds=480.0)
    snap = cd.snapshot()
    assert snap == {
        "active": False,
        "remaining_seconds": 0.0,
        "strikes": 0,
        "base_seconds": 60.0,
        "max_seconds": 480.0,
    }
    cd.note_rate_limited()
    snap = cd.snapshot()
    assert snap["active"] is True
    assert snap["strikes"] == 1
    assert snap["remaining_seconds"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# parse_codex_events error capture
# ---------------------------------------------------------------------------


def test_parse_codex_events_captures_error_event():
    jsonl = (
        '{"type":"thread.started","thread_id":"t1"}\n'
        '{"type":"error","message":"429 Too Many Requests"}'
    )
    parsed = parse_codex_events(jsonl)
    assert parsed["error_message"] == "429 Too Many Requests"
    assert parsed["thread_id"] == "t1"


def test_parse_codex_events_captures_turn_failed():
    jsonl = '{"type":"turn.failed","error":{"message":"Rate limit reached"}}'
    assert (
        parse_codex_events(jsonl)["error_message"] == "Rate limit reached"
    )


def test_parse_codex_events_error_message_defaults_empty():
    assert parse_codex_events("")["error_message"] == ""
    malformed = '{"type":"error"}\n{"type":"turn.failed","error":"x"}'
    assert parse_codex_events(malformed)["error_message"] == ""


# ---------------------------------------------------------------------------
# multi_app: classification + env parsing
# ---------------------------------------------------------------------------


def test_classify_provider_rate_limit():
    from multi_app import classify_provider_rate_limit

    assert classify_provider_rate_limit(
        ProviderRateLimitedError("x", retry_after=12.0)
    ) == (True, 12.0, True)
    assert classify_provider_rate_limit(
        ProviderRateLimitedError("x", replay_safe=False)
    ) == (True, None, False)
    assert classify_provider_rate_limit(TimeoutError()) == (
        False,
        None,
        False,
    )
    assert classify_provider_rate_limit(
        RuntimeError("Rate limit reached")
    ) == (True, None, True)
    assert classify_provider_rate_limit(RuntimeError("boom")) == (
        False,
        None,
        False,
    )


def test_classify_provider_rate_limit_reads_retry_after_header():
    from types import SimpleNamespace

    from multi_app import classify_provider_rate_limit

    exc = RuntimeError("HTTP 429 Too Many Requests")
    exc.response = SimpleNamespace(headers={"retry-after": "37"})
    assert classify_provider_rate_limit(exc) == (True, 37.0, True)
    exc.response = SimpleNamespace(headers={"retry-after": "not-a-number"})
    assert classify_provider_rate_limit(exc) == (True, None, True)


def test_provider_cooldown_from_env(monkeypatch):
    from multi_app import provider_cooldown_from_env

    monkeypatch.delenv("PROVIDER_COOLDOWN_BASE_SECONDS", raising=False)
    monkeypatch.delenv("PROVIDER_COOLDOWN_MAX_SECONDS", raising=False)
    snap = provider_cooldown_from_env().snapshot()
    assert snap["base_seconds"] == 60.0
    assert snap["max_seconds"] == 480.0

    monkeypatch.setenv("PROVIDER_COOLDOWN_BASE_SECONDS", "5")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MAX_SECONDS", "20")
    snap = provider_cooldown_from_env().snapshot()
    assert snap["base_seconds"] == 5.0
    assert snap["max_seconds"] == 20.0

    monkeypatch.setenv("PROVIDER_COOLDOWN_BASE_SECONDS", "abc")
    with pytest.raises(RuntimeError):
        provider_cooldown_from_env()
    monkeypatch.setenv("PROVIDER_COOLDOWN_BASE_SECONDS", "0")
    with pytest.raises(RuntimeError):
        provider_cooldown_from_env()
    monkeypatch.setenv("PROVIDER_COOLDOWN_BASE_SECONDS", "30")
    monkeypatch.setenv("PROVIDER_COOLDOWN_MAX_SECONDS", "10")
    with pytest.raises(RuntimeError):
        provider_cooldown_from_env()


# ---------------------------------------------------------------------------
# multi_app: _run_turn retry integration (stubbed dispatch)
# ---------------------------------------------------------------------------


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
    configs, _ = load_agents_config(str(yaml_path))
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
    )
    # Fast cooldown so retry tests do not sleep for real minutes.
    from multi_core import ProviderCooldownRegistry

    agent._provider_cooldowns = ProviderCooldownRegistry(
        lambda: ProviderCooldown(base_seconds=0.01, max_seconds=0.02)
    )
    return agent


def _stub_dispatch(agent, outputs):
    calls: list[str] = []

    async def fake_dispatch(prompt, thread_key, gen, **kwargs):
        calls.append(prompt)
        response = outputs[min(len(calls) - 1, len(outputs) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    agent._dispatch_turn = fake_dispatch
    return calls


def _run(agent, prompt="p"):
    return asyncio.run(
        agent._run_turn(prompt, "C1:1.0", (0, 0))
    )


def test_run_turn_retries_once_after_rate_limit(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent, [ProviderRateLimitedError("429"), "recovered"]
    )
    assert _run(agent) == "recovered"
    assert len(calls) == 2
    # Clean turn reset the streak and the deadline.
    assert agent._cooldown_for().strikes == 0
    assert agent._cooldown_for().remaining() == 0.0


def test_run_turn_raises_after_second_rate_limit(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent,
        [ProviderRateLimitedError("429"), ProviderRateLimitedError("429")],
    )
    with pytest.raises(ProviderRateLimitedError):
        _run(agent)
    assert len(calls) == 2
    assert agent._cooldown_for().strikes == 2
    assert agent._cooldown_for().remaining() > 0


def test_run_turn_classifies_generic_rate_limit_text(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent, [RuntimeError("HTTP 429 Too Many Requests"), "ok"]
    )
    assert _run(agent) == "ok"
    assert len(calls) == 2


def test_run_turn_non_rate_limit_error_raises_immediately(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(agent, [RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        _run(agent)
    assert len(calls) == 1
    assert agent._cooldown_for().strikes == 0


def test_run_turn_timeout_is_not_retried(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(agent, [TimeoutError()])
    with pytest.raises(TimeoutError):
        _run(agent)
    assert len(calls) == 1
    assert agent._cooldown_for().strikes == 0


def test_run_turn_waits_out_preexisting_cooldown(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent._cooldown_for().note_rate_limited()  # armed before the turn
    calls = _stub_dispatch(agent, ["ok"])
    assert _run(agent) == "ok"
    assert len(calls) == 1
    assert agent._cooldown_for().remaining() == 0.0


def test_run_turn_does_not_replay_a_turn_that_ran_tools(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent,
        [ProviderRateLimitedError("usage limit", replay_safe=False), "dup"],
    )
    with pytest.raises(ProviderRateLimitedError) as info:
        _run(agent)
    assert len(calls) == 1
    assert info.value.replay_safe is False
    # The cooldown is still armed for the next turn.
    assert agent._cooldown_for().strikes == 1


def test_run_turn_does_not_retry_when_reset_exceeds_cap(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent,
        [ProviderRateLimitedError("five hour window", retry_after=4 * 3600.0)],
    )
    with pytest.raises(ProviderRateLimitedError) as info:
        _run(agent)
    assert len(calls) == 1
    assert info.value.retry_after == 4 * 3600.0


def test_run_turn_does_not_retry_after_thread_reset(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(agent, [ProviderRateLimitedError("429"), "late"])
    monkeypatch.setattr(agent, "_turn_generation", lambda _key: (9, 9))
    with pytest.raises(ProviderRateLimitedError):
        _run(agent)
    assert len(calls) == 1


def test_run_turn_wraps_untyped_rate_limit_after_retry(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _stub_dispatch(agent, [RuntimeError("HTTP 429 Too Many Requests")])
    with pytest.raises(ProviderRateLimitedError) as info:
        _run(agent)
    assert info.value.replay_safe is True
    assert isinstance(info.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# TurnSignals / notices (pure)
# ---------------------------------------------------------------------------


def test_turn_signals_require_rate_limit_evidence():
    from multi_core import TurnSignals

    assert TurnSignals(error_text="Prompt is too long").rate_limit_error(
        now=0.0
    ) is None
    error = TurnSignals(rate_limit_seen=True).rate_limit_error(
        "Claude Code returned an error result: success", now=0.0
    )
    assert isinstance(error, ProviderRateLimitedError)
    assert error.replay_safe is True
    assert error.retry_after is None


def test_turn_signals_reset_time_and_tool_activity():
    from multi_core import TurnSignals

    signals = TurnSignals(
        rate_limit_seen=True, resets_at=1600.0, tool_activity=True
    )
    error = signals.rate_limit_error(now=1000.0)
    assert error.retry_after == 600.0
    assert error.replay_safe is False
    # Text alone is enough evidence (e.g. "usage limit reached").
    error = TurnSignals(error_text="Claude AI usage limit reached").rate_limit_error(
        now=0.0
    )
    assert error is not None


def test_side_effect_tool_classification():
    from multi_core import is_side_effect_tool

    assert not is_side_effect_tool("Read")
    assert not is_side_effect_tool("Grep")
    assert is_side_effect_tool("Bash")
    assert is_side_effect_tool("Edit")
    assert is_side_effect_tool("mcp__github__create_issue")


def test_rate_limit_notice_variants():
    from multi_core import format_rate_limit_notice

    assert "自動再試行はしていません" in format_rate_limit_notice(
        retry_after=None, replay_safe=False
    )
    assert "約 60 分後" in format_rate_limit_notice(
        retry_after=3541.0, replay_safe=True
    )
    assert "しばらく待って" in format_rate_limit_notice(
        retry_after=None, replay_safe=True
    )


def test_cooldown_exceeds_cap():
    cd, _clock = _cooldown(
        base_seconds=60.0, max_seconds=480.0, hard_cap_seconds=900.0
    )
    assert not cd.exceeds_cap(None)
    assert not cd.exceeds_cap(900.0)
    assert cd.exceeds_cap(901.0)


# ---------------------------------------------------------------------------
# Claude SDK stream: the CLI exits non-zero right after an error result
# ---------------------------------------------------------------------------


def _claude_error_result(**overrides):
    from claude_agent_sdk import ResultMessage

    fields = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="sess-new",
        result="API Error: Request rejected",
        usage={"input_tokens": 10, "output_tokens": 2},
        api_error_status=429,
    )
    fields.update(overrides)
    return ResultMessage(**fields)


def _fake_claude_stream(monkeypatch, messages):
    import multi_app

    async def fake_query(*, prompt, options):
        for message in messages:
            yield message
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(multi_app, "query", fake_query)


def test_run_claude_detects_rate_limit_after_sdk_error_exit(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_claude_stream(monkeypatch, [_claude_error_result()])
    with pytest.raises(ProviderRateLimitedError) as info:
        asyncio.run(agent._run_claude("p", "C1:1.0", (0, 0)))
    assert info.value.replay_safe is True
    # No session write-back: a retry resumes the pre-turn session.
    assert "C1:1.0" not in agent.sessions


def test_run_claude_marks_turn_with_side_effect_tool_unsafe(
    tmp_path, monkeypatch
):
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    agent = _build_agent(tmp_path, monkeypatch)
    tool_turn = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git push"})],
        model="m",
    )
    _fake_claude_stream(
        monkeypatch, [tool_turn, _claude_error_result(api_error_status=None, result="Claude AI usage limit reached")]
    )
    with pytest.raises(ProviderRateLimitedError) as info:
        asyncio.run(agent._run_claude("p", "C1:1.0", (0, 0)))
    assert info.value.replay_safe is False


def test_run_claude_reads_rejected_window_reset(tmp_path, monkeypatch):
    import time as time_module

    from claude_agent_sdk import RateLimitEvent
    from claude_agent_sdk.types import RateLimitInfo

    agent = _build_agent(tmp_path, monkeypatch)
    resets_at = int(time_module.time()) + 3 * 3600
    event = RateLimitEvent(
        rate_limit_info=RateLimitInfo(status="rejected", resets_at=resets_at),
        uuid="u",
        session_id="s",
    )
    _fake_claude_stream(
        monkeypatch,
        [event, _claude_error_result(api_error_status=None, result="")],
    )
    with pytest.raises(ProviderRateLimitedError) as info:
        asyncio.run(agent._run_claude("p", "C1:1.0", (0, 0)))
    assert info.value.retry_after == pytest.approx(3 * 3600, abs=5)


def test_run_claude_non_rate_limit_error_stays_generic(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_claude_stream(
        monkeypatch,
        [_claude_error_result(api_error_status=None, result="Prompt is too long")],
    )
    with pytest.raises(Exception) as info:
        asyncio.run(agent._run_claude("p", "C1:1.0", (0, 0)))
    assert not isinstance(info.value, ProviderRateLimitedError)
    # The CLI's own message survives so the failure can be classified.
    assert "Prompt is too long" in str(info.value)


# ---------------------------------------------------------------------------
# Codex: only a failed turn is a rate limit
# ---------------------------------------------------------------------------


def test_parse_codex_events_tracks_turn_status_and_tools():
    jsonl = (
        '{"type":"item.started","item":{"id":"i1","type":"command_execution"}}\n'
        '{"type":"error","message":"Reconnecting... 429 Too Many Requests"}\n'
        '{"type":"item.completed","item":{"id":"i2","type":"agent_message","text":"done"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}'
    )
    parsed = parse_codex_events(jsonl)
    assert parsed["tool_activity"] is True
    assert parsed["turn_completed"] is True
    assert parsed["turn_failed"] is False
    assert parsed["last_message"] == "done"
    quiet = parse_codex_events(
        '{"type":"item.completed","item":{"id":"i0","type":"error","message":"config warning"}}\n'
        '{"type":"turn.failed","error":{"message":"Rate limit reached"}}'
    )
    assert quiet["tool_activity"] is False
    assert quiet["turn_failed"] is True


class _FakeCodexProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


def _fake_codex(monkeypatch, proc):
    import multi_app

    async def fake_create(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(multi_app.asyncio, "create_subprocess_exec", fake_create)


def test_codex_recovered_stream_error_is_not_a_rate_limit(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_codex(
        monkeypatch,
        _FakeCodexProcess(
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"error","message":"Reconnecting... 2/5 429 Too Many Requests"}\n'
            '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1}}'
        ),
    )
    text, thread_id, *_ = asyncio.run(agent._run_codex_exec("p", None))
    assert text == "ok"
    assert thread_id == "t1"


def test_codex_failed_turn_with_tools_is_not_replay_safe(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_codex(
        monkeypatch,
        _FakeCodexProcess(
            '{"type":"item.started","item":{"id":"i1","type":"file_change"}}\n'
            '{"type":"turn.failed","error":{"message":"Rate limit reached"}}',
            returncode=1,
        ),
    )
    with pytest.raises(ProviderRateLimitedError) as info:
        asyncio.run(agent._run_codex_exec("p", None))
    assert info.value.replay_safe is False


# ---------------------------------------------------------------------------
# NodeRuntimeLimiter: a cooldown wait hands the slot back
# ---------------------------------------------------------------------------


def test_sleep_released_frees_capacity_for_other_agents():
    from multi_app import NodeRuntimeLimiter

    async def scenario():
        limiter = NodeRuntimeLimiter(max_concurrency=1, max_queue=2)
        cooling = limiter.try_admit("a", "/ws/a")
        assert cooling.state == "running"
        other_ran = asyncio.Event()

        async def other():
            async with limiter.slot("b", "/ws/b"):
                other_ran.set()

        sleeper = asyncio.create_task(limiter.sleep_released(cooling, 0.05))
        await asyncio.sleep(0)
        assert limiter.snapshot("a")["paused"] == 1
        await asyncio.wait_for(other(), timeout=1)
        assert other_ran.is_set()
        await asyncio.wait_for(sleeper, timeout=1)
        assert cooling.state == "running"
        assert limiter.snapshot("a")["running"] == 1
        limiter.release_admission(cooling)
        assert limiter.snapshot("a")["node_running"] == 0

    asyncio.run(scenario())


def test_sleep_released_requeues_at_head_when_node_is_full():
    from multi_app import NodeRuntimeLimiter

    async def scenario():
        limiter = NodeRuntimeLimiter(max_concurrency=1, max_queue=2)
        cooling = limiter.try_admit("a", "/ws/a")
        sleeper = asyncio.create_task(limiter.sleep_released(cooling, 0.01))
        await asyncio.sleep(0)
        busy = limiter.try_admit("b", "/ws/b")
        assert busy.state == "running"
        late = limiter.try_admit("c", "/ws/c")
        assert late.state == "queued"
        await asyncio.sleep(0.05)
        assert cooling.state == "queued"
        limiter.release_admission(busy)
        await asyncio.wait_for(sleeper, timeout=1)
        # The paused job regains the slot ahead of the later arrival.
        assert cooling.state == "running"
        assert late.state == "queued"
        limiter.release_admission(cooling)
        assert late.state == "running"
        limiter.release_admission(late)

    asyncio.run(scenario())


def test_sleep_released_cancellation_releases_cleanly():
    from multi_app import NodeRuntimeLimiter

    async def scenario():
        limiter = NodeRuntimeLimiter(max_concurrency=1, max_queue=1)

        async def job():
            async with limiter.slot("a", "/ws/a") as admission:
                await limiter.sleep_released(admission, 10)

        task = asyncio.create_task(job())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snap = limiter.snapshot("a")
        assert snap["node_running"] == 0
        assert snap["node_admitted"] == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Per-account cooldown sharing
# ---------------------------------------------------------------------------


def test_provider_account_key_groups_local_logins():
    from multi_core import provider_account_key

    assert provider_account_key("claude") == "claude:local"
    assert provider_account_key("codex") == "codex:local"
    assert provider_account_key(
        "openai", openai_api_key_env="OPENAI_API_KEY"
    ) == "openai:https://api.openai.com/v1:OPENAI_API_KEY"
    assert provider_account_key(
        "openai",
        openai_base_url="http://127.0.0.1:8317/v1/",
        openai_api_key_env="OPENAI_API_KEY",
    ) == "openai:http://127.0.0.1:8317/v1:OPENAI_API_KEY"


def test_cooldown_registry_shares_one_instance_per_account():
    from multi_core import ProviderCooldownRegistry

    registry = ProviderCooldownRegistry(ProviderCooldown)
    assert registry.get("claude:local") is registry.get("claude:local")
    assert registry.get("claude:local") is not registry.get("codex:local")


def test_local_claude_agents_share_a_cooldown(tmp_path, monkeypatch):
    from dataclasses import replace

    from multi_core import ProviderCooldownRegistry

    first = _build_agent(tmp_path, monkeypatch)
    second = _build_agent(tmp_path, monkeypatch)
    shared = ProviderCooldownRegistry(
        lambda: ProviderCooldown(base_seconds=60.0, max_seconds=60.0)
    )
    first._provider_cooldowns = shared
    second._provider_cooldowns = shared
    first._cooldown_for().note_rate_limited()
    assert second._cooldown_for().remaining() > 0
    codex_cfg = replace(second.cfg, runtime="codex")
    assert second._cooldown_for(codex_cfg).remaining() == 0.0


def test_skipped_patrol_does_not_reset_strikes(tmp_path, monkeypatch):
    import time as time_module

    agent = _build_agent(tmp_path, monkeypatch)
    cooldown = agent._cooldown_for(agent.cfg)
    cooldown.note_rate_limited()
    time_module.sleep(0.03)  # deadline passes; the strike streak remains
    assert cooldown.remaining() == 0.0

    async def skipped_inner(*_args, **_kwargs):
        return False

    agent._run_patrol_once_inner = skipped_inner
    asyncio.run(agent._run_patrol_once("p", "C-PATROL"))
    assert cooldown.strikes == 1

    async def ran_inner(*_args, **_kwargs):
        return True

    agent._run_patrol_once_inner = ran_inner
    asyncio.run(agent._run_patrol_once("p", "C-PATROL"))
    assert cooldown.strikes == 0
