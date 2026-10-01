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
    ) == (True, 12.0)
    assert classify_provider_rate_limit(TimeoutError()) == (False, None)
    assert classify_provider_rate_limit(
        RuntimeError("Rate limit reached")
    ) == (True, None)
    assert classify_provider_rate_limit(RuntimeError("boom")) == (
        False,
        None,
    )


def test_classify_provider_rate_limit_reads_retry_after_header():
    from types import SimpleNamespace

    from multi_app import classify_provider_rate_limit

    exc = RuntimeError("HTTP 429 Too Many Requests")
    exc.response = SimpleNamespace(headers={"retry-after": "37"})
    assert classify_provider_rate_limit(exc) == (True, 37.0)
    exc.response = SimpleNamespace(headers={"retry-after": "not-a-number"})
    assert classify_provider_rate_limit(exc) == (True, None)


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
    agent._provider_cooldown = ProviderCooldown(
        base_seconds=0.01, max_seconds=0.02
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
    assert agent._provider_cooldown.strikes == 0
    assert agent._provider_cooldown.remaining() == 0.0


def test_run_turn_raises_after_second_rate_limit(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(
        agent,
        [ProviderRateLimitedError("429"), ProviderRateLimitedError("429")],
    )
    with pytest.raises(ProviderRateLimitedError):
        _run(agent)
    assert len(calls) == 2
    assert agent._provider_cooldown.strikes == 2
    assert agent._provider_cooldown.remaining() > 0


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
    assert agent._provider_cooldown.strikes == 0


def test_run_turn_timeout_is_not_retried(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    calls = _stub_dispatch(agent, [TimeoutError()])
    with pytest.raises(TimeoutError):
        _run(agent)
    assert len(calls) == 1
    assert agent._provider_cooldown.strikes == 0


def test_run_turn_waits_out_preexisting_cooldown(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent._provider_cooldown.note_rate_limited()  # armed before the turn
    calls = _stub_dispatch(agent, ["ok"])
    assert _run(agent) == "ok"
    assert len(calls) == 1
    assert agent._provider_cooldown.remaining() == 0.0
