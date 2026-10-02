"""Context rollover measures the live context window, not the turn's usage sum."""

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from multi_core import context_window_tokens

THREAD = "C1:1.0"


def _build_agent(tmp_path, monkeypatch, rollover_tokens=60000):
    from multi_app import Roster, SlackAgent, load_agents_config
    from multi_core import TurnBudget

    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text(
        "agents:\n  - name: dev\n    persona: x\n"
        f"    context_rollover_tokens: {rollover_tokens}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEV_SLACK_BOT_TOKEN", "xoxb-dev")
    monkeypatch.setenv("DEV_SLACK_APP_TOKEN", "xapp-dev")
    monkeypatch.delenv("CLAUDE_WORKSPACE", raising=False)
    configs, _ = load_agents_config(str(yaml_path))
    return SlackAgent(
        configs[0],
        budget=TurnBudget(8),
        roster=Roster(),
        allowed_humans=set(),
    )


def _assistant(usage, parent=None):
    return AssistantMessage(
        content=[TextBlock(text="step")],
        model="m",
        parent_tool_use_id=parent,
        usage=usage,
    )


def _result(usage, *, result="done", num_turns=3):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=num_turns,
        session_id="sess-1",
        result=result,
        usage=usage,
    )


def _fake_stream(monkeypatch, messages):
    import multi_app

    async def fake_query(*, prompt, options):
        for message in messages:
            yield message

    monkeypatch.setattr(multi_app, "query", fake_query)


def test_context_window_tokens_counts_one_calls_prompt():
    assert context_window_tokens(
        {
            "input_tokens": 300,
            "cache_read_input_tokens": 21000,
            "cache_creation_input_tokens": 200,
            "output_tokens": 900,
        }
    ) == 21500
    assert context_window_tokens(None) == 0


def test_claude_turn_records_last_top_level_call_as_context(
    tmp_path, monkeypatch
):
    """Each tool step re-reads the context; the turn's summed usage is no gauge.

    Before the fix this three-step turn recorded ~132k "context" tokens and
    rolled the session over although the window held ~21k.
    """
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_stream(
        monkeypatch,
        [
            SystemMessage(subtype="init", data={"session_id": "sess-1"}),
            _assistant(
                {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 20000,
                    "cache_creation_input_tokens": 1000,
                }
            ),
            # A subagent call runs in its own window.
            _assistant({"input_tokens": 90000}, parent="toolu_1"),
            _assistant(
                {
                    "input_tokens": 300,
                    "cache_read_input_tokens": 21000,
                    "cache_creation_input_tokens": 200,
                }
            ),
            _result(
                {
                    "input_tokens": 90305,
                    "cache_read_input_tokens": 41000,
                    "cache_creation_input_tokens": 1200,
                    "output_tokens": 900,
                }
            ),
        ],
    )
    reply = asyncio.run(
        agent._run_claude("p", THREAD, agent._turn_generation(THREAD))
    )
    assert reply == "done"
    assert agent.thread_stats[THREAD] == {"input_tokens": 21500, "num_turns": 3}

    notices = []

    async def say(**kwargs):
        notices.append(kwargs)

    asyncio.run(agent._maybe_rollover(THREAD, "1.0", say))
    assert notices == []
    assert agent.sessions[THREAD] == "sess-1"


def test_claude_turn_without_per_call_usage_falls_back_to_result(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    _fake_stream(
        monkeypatch,
        [
            _assistant(None),
            _result(
                {"input_tokens": 40, "cache_read_input_tokens": 2},
                num_turns=1,
            ),
        ],
    )
    asyncio.run(agent._run_claude("p", THREAD, agent._turn_generation(THREAD)))
    assert agent.thread_stats[THREAD]["input_tokens"] == 42


def test_rollover_notice_states_the_measured_size(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch, rollover_tokens=60000)
    agent.sessions[THREAD] = "sess-1"
    agent.thread_stats[THREAD] = {"input_tokens": 61234, "num_turns": 2}
    _fake_stream(monkeypatch, [_result({}, result="summary", num_turns=1)])
    notices = []

    async def say(**kwargs):
        notices.append(kwargs)

    asyncio.run(agent._maybe_rollover(THREAD, "1.0", say))
    assert THREAD not in agent.sessions
    assert agent.thread_summaries[THREAD] == "summary"
    assert len(notices) == 1
    assert "61,234 tokens" in notices[0]["text"]
    assert "60,000" in notices[0]["text"]
