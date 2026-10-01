"""Unit tests for the node-wide adaptive turn pacer (multi_core + multi_app)."""

import asyncio
import time

import pytest

from multi_core import AdaptivePacer, ProviderCooldown, ProviderRateLimitedError


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pacer(**kwargs) -> tuple[AdaptivePacer, _FakeClock]:
    clock = _FakeClock()
    return AdaptivePacer(clock=clock, **kwargs), clock


# ---------------------------------------------------------------------------
# AdaptivePacer: slot reservation
# ---------------------------------------------------------------------------


def test_reserve_spaces_consecutive_starts_by_interval():
    pacer, _clock = _pacer(base_seconds=0.5, max_seconds=8.0)
    assert pacer.reserve() == pytest.approx(0.0)
    assert pacer.reserve() == pytest.approx(0.5)
    assert pacer.reserve() == pytest.approx(1.0)


def test_reserve_after_idle_period_needs_no_wait():
    pacer, clock = _pacer(base_seconds=0.5, max_seconds=8.0)
    pacer.reserve()
    pacer.reserve()
    clock.now += 60.0
    assert pacer.reserve() == pytest.approx(0.0)


def test_reserve_disabled_pacer_is_free():
    pacer, _clock = _pacer(base_seconds=0.0)
    assert pacer.enabled is False
    for _ in range(3):
        assert pacer.reserve() == 0.0
    assert pacer.note_rate_limited() == 0.0
    assert pacer.note_clean_turn() == 0.0


# ---------------------------------------------------------------------------
# AdaptivePacer: adaptive interval
# ---------------------------------------------------------------------------


def test_rate_limited_doubles_interval_up_to_cap():
    pacer, _clock = _pacer(base_seconds=0.5, max_seconds=8.0)
    assert pacer.note_rate_limited() == 1.0
    assert pacer.note_rate_limited() == 2.0
    assert pacer.note_rate_limited() == 4.0
    assert pacer.note_rate_limited() == 8.0
    assert pacer.note_rate_limited() == 8.0  # capped


def test_clean_turns_halve_interval_back_to_base():
    pacer, _clock = _pacer(
        base_seconds=0.5, max_seconds=8.0, clean_threshold=5
    )
    for _ in range(4):
        pacer.note_rate_limited()
    assert pacer.interval == 8.0
    for _ in range(4):
        assert pacer.note_clean_turn() == 8.0  # below threshold, no change
    assert pacer.note_clean_turn() == 4.0  # fifth clean turn halves
    for _ in range(5):
        pacer.note_clean_turn()
    assert pacer.interval == 2.0
    for _ in range(5):
        pacer.note_clean_turn()
    for _ in range(5):
        pacer.note_clean_turn()
    assert pacer.interval == 0.5  # floored at base
    pacer.note_clean_turn()  # at base: streak resets, interval unchanged
    assert pacer.interval == 0.5


def test_rate_limit_resets_clean_streak():
    pacer, _clock = _pacer(
        base_seconds=0.5, max_seconds=8.0, clean_threshold=5
    )
    pacer.note_rate_limited()  # 1.0
    for _ in range(4):
        pacer.note_clean_turn()
    pacer.note_rate_limited()  # 2.0, streak back to 0
    for _ in range(4):
        assert pacer.note_clean_turn() == 2.0
    assert pacer.note_clean_turn() == 1.0  # full threshold needed again


def test_pacer_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        AdaptivePacer(base_seconds=-1)
    with pytest.raises(ValueError):
        AdaptivePacer(base_seconds=2.0, max_seconds=1.0)
    with pytest.raises(ValueError):
        AdaptivePacer(clean_threshold=0)
    AdaptivePacer(base_seconds=0.0, max_seconds=0.0)  # disabled is valid


def test_pacer_snapshot_shape():
    pacer, _clock = _pacer(base_seconds=0.5, max_seconds=8.0)
    assert pacer.snapshot() == {
        "enabled": True,
        "interval_seconds": 0.5,
        "base_seconds": 0.5,
        "max_seconds": 8.0,
        "clean_streak": 0,
        "clean_threshold": 5,
    }
    pacer.note_rate_limited()
    assert pacer.snapshot()["interval_seconds"] == 1.0


# ---------------------------------------------------------------------------
# multi_app: env parsing
# ---------------------------------------------------------------------------


def _clear_pacer_env(monkeypatch):
    for name in (
        "PROVIDER_PACER_BASE_SECONDS",
        "PROVIDER_PACER_MAX_SECONDS",
        "PROVIDER_PACER_CLEAN_TURNS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_pacer_from_env_defaults(monkeypatch):
    from multi_app import provider_pacer_from_env

    _clear_pacer_env(monkeypatch)
    snap = provider_pacer_from_env().snapshot()
    assert snap["base_seconds"] == 0.5
    assert snap["max_seconds"] == 8.0
    assert snap["clean_threshold"] == 5
    assert snap["enabled"] is True


def test_provider_pacer_from_env_custom_and_disabled(monkeypatch):
    from multi_app import provider_pacer_from_env

    _clear_pacer_env(monkeypatch)
    monkeypatch.setenv("PROVIDER_PACER_BASE_SECONDS", "1.5")
    monkeypatch.setenv("PROVIDER_PACER_MAX_SECONDS", "30")
    monkeypatch.setenv("PROVIDER_PACER_CLEAN_TURNS", "3")
    snap = provider_pacer_from_env().snapshot()
    assert snap["base_seconds"] == 1.5
    assert snap["max_seconds"] == 30.0
    assert snap["clean_threshold"] == 3

    monkeypatch.setenv("PROVIDER_PACER_BASE_SECONDS", "0")
    assert provider_pacer_from_env().enabled is False


def test_provider_pacer_from_env_invalid(monkeypatch):
    from multi_app import provider_pacer_from_env

    _clear_pacer_env(monkeypatch)
    monkeypatch.setenv("PROVIDER_PACER_BASE_SECONDS", "abc")
    with pytest.raises(RuntimeError):
        provider_pacer_from_env()
    monkeypatch.setenv("PROVIDER_PACER_BASE_SECONDS", "-1")
    with pytest.raises(RuntimeError):
        provider_pacer_from_env()
    monkeypatch.setenv("PROVIDER_PACER_BASE_SECONDS", "10")
    monkeypatch.setenv("PROVIDER_PACER_MAX_SECONDS", "5")
    with pytest.raises(RuntimeError):
        provider_pacer_from_env()
    monkeypatch.setenv("PROVIDER_PACER_MAX_SECONDS", "20")
    monkeypatch.setenv("PROVIDER_PACER_CLEAN_TURNS", "0")
    with pytest.raises(RuntimeError):
        provider_pacer_from_env()
    monkeypatch.setenv("PROVIDER_PACER_CLEAN_TURNS", "x")
    with pytest.raises(RuntimeError):
        provider_pacer_from_env()


# ---------------------------------------------------------------------------
# multi_app: wiring (hooks in _run_turn, pacing before provider calls)
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
    _clear_pacer_env(monkeypatch)
    configs, _ = load_agents_config(str(yaml_path))
    agent = SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
    )
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


def test_run_turn_adjusts_pacer_on_rate_limit_and_success(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    assert agent._turn_pacer.interval == 0.5
    _stub_dispatch(agent, [ProviderRateLimitedError("429"), "ok"])
    result = asyncio.run(agent._run_turn("p", "C1:1.0", (0, 0)))
    assert result == "ok"
    # One rate-limited attempt doubled the interval; the clean retry counted
    # one clean turn toward halving (threshold 5 keeps the interval for now).
    snap = agent._turn_pacer.snapshot()
    assert snap["interval_seconds"] == 1.0
    assert snap["clean_streak"] == 1


def test_run_turn_clean_turns_keep_base_interval(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    _stub_dispatch(agent, ["ok"])
    for _ in range(3):
        asyncio.run(agent._run_turn("p", "C1:1.0", (0, 0)))
    assert agent._turn_pacer.interval == 0.5
    assert agent._turn_pacer.snapshot()["clean_streak"] == 0


class _SpyPacer(AdaptivePacer):
    def __init__(self) -> None:
        super().__init__(base_seconds=0.5, max_seconds=8.0)
        self.reserve_calls = 0

    def reserve(self) -> float:
        self.reserve_calls += 1
        return 0.0


def test_openai_response_paces_before_provider_call(tmp_path, monkeypatch):
    from types import SimpleNamespace

    agent = _build_agent(tmp_path, monkeypatch)
    spy = _SpyPacer()
    agent._turn_pacer = spy

    class FakeResponses:
        async def create(self, **kwargs):
            return SimpleNamespace(id="resp-1", output_text="hi", usage=None)

    agent._openai_client = SimpleNamespace(responses=FakeResponses())
    text, response_id, _tokens = asyncio.run(
        agent._run_openai_response(
            "prompt", system_prompt="sp", previous_response_id=None
        )
    )
    assert text == "hi"
    assert response_id == "resp-1"
    assert spy.reserve_calls == 1


def test_pace_turn_start_sleeps_for_reserved_delay(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent._turn_pacer = AdaptivePacer(base_seconds=0.05, max_seconds=8.0)

    async def two_paced_starts() -> float:
        start = time.monotonic()
        await agent._pace_turn_start()
        await agent._pace_turn_start()
        return time.monotonic() - start

    elapsed = asyncio.run(two_paced_starts())
    assert elapsed >= 0.04  # second start waited out the 50ms interval
