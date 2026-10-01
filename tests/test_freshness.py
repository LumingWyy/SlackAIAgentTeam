"""Unit tests for the post-time reply freshness gate (multi_core layer)."""

from multi_core import (
    FRESHNESS_KEEP_SENTINEL,
    FRESHNESS_SKIP_SENTINEL,
    build_freshness_recheck_prompt,
    filter_context_messages,
    is_verbatim_duplicate,
    latest_message_ts,
    latest_non_self_text,
    parse_freshness_decision,
    reply_fingerprint,
    select_freshness_messages,
)

SELF_USER = "U_SELF"
SELF_BOT = "B_SELF"
PEER_BOT = "B_PEER"
FEED_BOT = "B_FEED"
HUMAN = "U_HUMAN"


def _msg(ts: str, text: str, *, user: str | None = None, bot_id: str | None = None) -> dict:
    msg: dict = {"ts": ts, "text": text}
    if user is not None:
        msg["user"] = user
    if bot_id is not None:
        msg["bot_id"] = bot_id
    return msg


# ---------------------------------------------------------------------------
# latest_message_ts / latest_non_self_text
# ---------------------------------------------------------------------------


def test_latest_message_ts_picks_numeric_max_and_ignores_garbage():
    messages = [
        _msg("100.1", "a", user=HUMAN),
        _msg("not-a-ts", "b", user=HUMAN),
        _msg("100.9", "c", user=HUMAN),
        _msg("100.5", "d", user=HUMAN),
    ]
    assert latest_message_ts(messages) == "100.9"
    assert latest_message_ts([]) == ""
    assert latest_message_ts([{"text": "no ts"}]) == ""


def test_latest_non_self_text_skips_self_and_empty():
    messages = [
        _msg("1", "peer says hi", bot_id=PEER_BOT),
        _msg("2", "self reply", bot_id=SELF_BOT),
        _msg("3", "   ", user=HUMAN),
        _msg("4", "self as user", user=SELF_USER),
    ]
    assert (
        latest_non_self_text(
            messages, self_user_id=SELF_USER, self_bot_id=SELF_BOT
        )
        == "peer says hi"
    )
    assert (
        latest_non_self_text([], self_user_id=SELF_USER, self_bot_id=SELF_BOT)
        == ""
    )


# ---------------------------------------------------------------------------
# select_freshness_messages
# ---------------------------------------------------------------------------


def test_select_freshness_keeps_only_newer_peer_and_human_messages():
    messages = [
        _msg("100.0", "old human line", user=HUMAN),
        _msg("101.0", "new human correction", user=HUMAN),
        _msg("102.0", "new peer progress", bot_id=PEER_BOT),
    ]
    fresh = select_freshness_messages(
        messages,
        baseline_ts="100.0",
        self_user_id=SELF_USER,
        self_bot_id=SELF_BOT,
        peer_bot_ids={PEER_BOT},
        feed_bot_ids={FEED_BOT},
    )
    assert [m["ts"] for m in fresh] == ["101.0", "102.0"]


def test_select_freshness_excludes_self_guest_feed_commands_and_self_mentions():
    guest = _msg("103.0", "[guest] drive-by comment", user="U_GUEST")
    guest["_context_role"] = "guest"
    messages = [
        _msg("101.0", "my own line", bot_id=SELF_BOT),
        _msg("102.0", "my own as user", user=SELF_USER),
        guest,
        _msg("104.0", "feed notification", bot_id=FEED_BOT),
        _msg("105.0", f"<@{SELF_USER}> please redo it", user=HUMAN),
        _msg("106.0", f"!status <@{SELF_USER}>", user=HUMAN),
        _msg("107.0", "", user=HUMAN),
    ]
    fresh = select_freshness_messages(
        messages,
        baseline_ts="100.0",
        self_user_id=SELF_USER,
        self_bot_id=SELF_BOT,
        peer_bot_ids={PEER_BOT},
        feed_bot_ids={FEED_BOT},
    )
    assert fresh == []


def test_select_freshness_peer_wins_over_feed_membership():
    messages = [_msg("101.0", "dual-registered bot", bot_id=PEER_BOT)]
    fresh = select_freshness_messages(
        messages,
        baseline_ts="100.0",
        self_user_id=SELF_USER,
        self_bot_id=SELF_BOT,
        peer_bot_ids={PEER_BOT},
        feed_bot_ids={PEER_BOT, FEED_BOT},
    )
    assert [m["ts"] for m in fresh] == ["101.0"]


def test_select_freshness_ignores_unparseable_baseline_comparisons():
    messages = [_msg("bad-ts", "hello", user=HUMAN)]
    assert (
        select_freshness_messages(
            messages,
            baseline_ts="100.0",
            self_user_id=SELF_USER,
            self_bot_id=SELF_BOT,
        )
        == []
    )


def test_select_freshness_composes_with_authority_filter():
    """filter_context_messages output feeds the gate: guests stay excluded."""
    raw = [
        _msg("101.0", "denied human line", user="U_DENIED"),
        _msg("102.0", "allowed human line", user=HUMAN),
    ]
    filtered = filter_context_messages(
        raw,
        self_bot_id=SELF_BOT,
        self_user_id=SELF_USER,
        peer_bot_ids={PEER_BOT},
        allowed_humans={HUMAN},
        allow_any_human=False,
    )
    fresh = select_freshness_messages(
        filtered,
        baseline_ts="100.0",
        self_user_id=SELF_USER,
        self_bot_id=SELF_BOT,
        peer_bot_ids={PEER_BOT},
    )
    assert [m["ts"] for m in fresh] == ["102.0"]


# ---------------------------------------------------------------------------
# verbatim duplicate fingerprinting
# ---------------------------------------------------------------------------


def test_reply_fingerprint_collapses_whitespace_only():
    assert reply_fingerprint("  a \n b\t c ") == "a b c"
    assert reply_fingerprint("") == ""


def test_is_verbatim_duplicate_matches_whitespace_variants():
    assert is_verbatim_duplicate("done: all tests pass", "done:  all tests\npass")
    assert not is_verbatim_duplicate("done: all tests pass", "done: tests pass")
    # Empty drafts never count as duplicates.
    assert not is_verbatim_duplicate("", "")
    assert not is_verbatim_duplicate("   ", "")


# ---------------------------------------------------------------------------
# recheck prompt + decision parsing
# ---------------------------------------------------------------------------


def test_build_freshness_recheck_prompt_embeds_draft_and_context():
    prompt = build_freshness_recheck_prompt("my draft", "[human] new info")
    assert "my draft" in prompt
    assert "[human] new info" in prompt
    assert FRESHNESS_KEEP_SENTINEL in prompt
    assert FRESHNESS_SKIP_SENTINEL in prompt


def test_parse_freshness_decision_keep_variants():
    for raw in (
        "POST_ORIGINAL",
        "`POST_ORIGINAL`",
        "**POST_ORIGINAL**",
        "post_original",
        "POST_ORIGINAL。",
        "\n POST_ORIGINAL \n(理由: 内容は依然正しい)",
    ):
        assert parse_freshness_decision(raw) == ("keep", ""), raw


def test_parse_freshness_decision_skip_variants():
    for raw in ("NO_REPLY", "`NO_REPLY`", "no_reply.", "NO_REPLY\nすでに対応済み"):
        assert parse_freshness_decision(raw) == ("skip", ""), raw


def test_parse_freshness_decision_revise_returns_full_text():
    revised = "修正版: テストは全て通りました。\n<@U123> レビューをお願いします"
    decision, text = parse_freshness_decision(revised)
    assert decision == "revise"
    assert text == revised


def test_parse_freshness_decision_empty_fails_open_to_keep():
    assert parse_freshness_decision("") == ("keep", "")
    assert parse_freshness_decision("   \n  ") == ("keep", "")


def test_parse_freshness_decision_sentinel_not_on_first_line_is_revision():
    raw = "了解しました。\nPOST_ORIGINAL"
    decision, text = parse_freshness_decision(raw)
    assert decision == "revise"
    assert text == raw


# ---------------------------------------------------------------------------
# SlackAgent._freshness_gate integration (stubbed transcript + runtime)
# ---------------------------------------------------------------------------


class _FakeTranscriptStore:
    def __init__(self, messages, *, fail: bool = False):
        self.messages = messages
        self.fail = fail

    def read_thread(self, team_id, channel_id, thread_ts):
        from types import SimpleNamespace

        if self.fail:
            raise RuntimeError("transcript unavailable")
        return SimpleNamespace(messages=list(self.messages))


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
    agent.user_id = SELF_USER
    agent.bot_id = SELF_BOT
    agent.team_id = "T_TEST"
    return agent


def _run_gate(agent, result, *, baseline_ts="100.0"):
    import asyncio

    return asyncio.run(
        agent._freshness_gate(
            result,
            channel="C1",
            thread_ts="100.0",
            baseline_ts=baseline_ts,
            thread_key="C1:100.0",
            gen=(0, 0),
            allowed_agent_names=None,
            project_id="",
        )
    )


def _stub_run_turn(agent, outputs):
    """Replace agent._run_turn; returns the list capturing call prompts."""
    calls: list[str] = []

    async def fake_run_turn(prompt, thread_key, gen, **kwargs):
        calls.append(prompt)
        response = outputs[min(len(calls) - 1, len(outputs) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    agent._run_turn = fake_run_turn
    return calls


def test_gate_no_mid_turn_messages_posts_original(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [_msg("100.0", "please do the task", user=HUMAN)]
    )
    calls = _stub_run_turn(agent, ["POST_ORIGINAL"])
    assert _run_gate(agent, "the answer") == ("the answer", False)
    assert calls == []


def test_gate_recheck_keep_posts_original_and_embeds_context(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please do the task", user=HUMAN),
            _msg("101.0", "actually use approach B", user=HUMAN),
        ]
    )
    calls = _stub_run_turn(agent, ["POST_ORIGINAL"])
    assert _run_gate(agent, "the answer") == ("the answer", False)
    assert len(calls) == 1
    assert "the answer" in calls[0]
    assert "actually use approach B" in calls[0]


def test_gate_recheck_withdraws_reply(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please do the task", user=HUMAN),
            _msg("101.0", "never mind, already fixed", user=HUMAN),
        ]
    )
    _stub_run_turn(agent, ["NO_REPLY"])
    result, skip = _run_gate(agent, "the answer")
    assert skip is True


def test_gate_recheck_revises_reply(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please do the task", user=HUMAN),
            _msg("101.0", "also cover the edge case", user=HUMAN),
        ]
    )
    _stub_run_turn(agent, ["revised answer covering the edge case"])
    assert _run_gate(agent, "the answer") == (
        "revised answer covering the edge case",
        False,
    )


def test_gate_recheck_failure_fails_open(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please do the task", user=HUMAN),
            _msg("101.0", "one more thing", user=HUMAN),
        ]
    )
    _stub_run_turn(agent, [RuntimeError("runtime blew up")])
    assert _run_gate(agent, "the answer") == ("the answer", False)


def test_gate_transcript_failure_fails_open(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore([], fail=True)
    calls = _stub_run_turn(agent, ["POST_ORIGINAL"])
    assert _run_gate(agent, "the answer") == ("the answer", False)
    assert calls == []


def test_gate_suppresses_verbatim_duplicate_of_latest_peer(
    tmp_path, monkeypatch
):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please report status", user=HUMAN),
            _msg("101.0", "done:  all tests\npass", user=HUMAN),
        ]
    )
    _stub_run_turn(agent, ["POST_ORIGINAL"])
    result, skip = _run_gate(agent, "done: all tests pass")
    assert skip is True


def test_freshness_baseline_prefers_newest_local_ts(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [_msg("100.0", "a", user=HUMAN), _msg("102.0", "b", user=HUMAN)]
    )
    assert agent._freshness_baseline("C1", "100.0", "101.0") == "102.0"
    assert agent._freshness_baseline("C1", "100.0", "103.0") == "103.0"
    assert agent._freshness_baseline("C1", None, "101.0") == "101.0"
    agent.transcript_store = _FakeTranscriptStore([], fail=True)
    assert agent._freshness_baseline("C1", "100.0", "101.0") == "101.0"


def test_freshness_recheck_enabled_env_toggle(monkeypatch):
    from multi_app import SlackAgent

    monkeypatch.delenv("FRESHNESS_RECHECK", raising=False)
    assert SlackAgent._freshness_recheck_enabled() is True
    for value in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("FRESHNESS_RECHECK", value)
        assert SlackAgent._freshness_recheck_enabled() is False
    monkeypatch.setenv("FRESHNESS_RECHECK", "1")
    assert SlackAgent._freshness_recheck_enabled() is True


def test_parse_freshness_decision_sentinel_with_trailing_reason():
    # Models often explain the decision on the same line; that commentary
    # must never be posted as the "revised" reply.
    assert parse_freshness_decision("`NO_REPLY` — 同事已答复") == ("skip", "")
    assert parse_freshness_decision("- POST_ORIGINAL (still correct)") == (
        "keep",
        "",
    )
    assert parse_freshness_decision("**NO_REPLY**: duplicate") == ("skip", "")
    assert parse_freshness_decision("NO_REPLYING is not a sentinel")[0] == (
        "revise"
    )


def test_gate_empty_recheck_placeholder_keeps_draft(tmp_path, monkeypatch):
    from multi_app import CLAUDE_EMPTY_REPLY

    agent = _build_agent(tmp_path, monkeypatch)
    agent.transcript_store = _FakeTranscriptStore(
        [
            _msg("100.0", "please do the task", user=HUMAN),
            _msg("101.0", "one more thing", user=HUMAN),
        ]
    )
    _stub_run_turn(agent, [CLAUDE_EMPTY_REPLY])
    assert _run_gate(agent, "the answer") == ("the answer", False)


# ---------------------------------------------------------------------------
# _activate_inner wiring: baseline timing, reset skip, withdrawn reaction
# ---------------------------------------------------------------------------


def _wire_activation(agent, store, outputs, *, during_guidance=None):
    """Stub every side effect of _activate_inner; returns call recorders."""
    agent.transcript_store = store
    calls: list[str] = []
    posts: list[str] = []
    reactions: list[dict] = []

    async def fake_fetch_context(*_args, **_kwargs):
        return "[human] please do the task"

    async def fake_guidance(*_args, **_kwargs):
        if during_guidance is not None:
            during_guidance()
        return ""

    async def no_files(_event):
        return ""

    async def fake_run_turn(prompt, thread_key, gen, **kwargs):
        calls.append(prompt)
        return outputs[min(len(calls) - 1, len(outputs) - 1)]

    async def fake_post(_channel, _thread_ts, result):
        posts.append(result)

    async def fake_reaction(_client, _channel, _ts, add=None, remove=None):
        reactions.append({"add": add, "remove": remove})

    async def members(*_args, **_kwargs):
        return {"dev"}

    async def no_rollover(*_args, **_kwargs):
        return None

    agent._fetch_context = fake_fetch_context
    agent._fetch_channel_guidance = fake_guidance
    agent._ingest_files = no_files
    agent._run_turn = fake_run_turn
    agent._post_result = fake_post
    agent._set_reaction = fake_reaction
    agent._channel_agent_names = members
    agent._maybe_rollover = no_rollover
    return calls, posts, reactions


def _thread_event():
    return {
        "channel": "C1",
        "ts": "100.0",
        "thread_ts": "100.0",
        "text": f"<@{SELF_USER}> please do the task",
        "user": HUMAN,
    }


async def _say(**_kwargs):
    return None


def test_message_during_guidance_fetch_still_triggers_recheck(
    tmp_path, monkeypatch
):
    import asyncio

    agent = _build_agent(tmp_path, monkeypatch)
    store = _FakeTranscriptStore([_msg("100.0", "please do the task", user=HUMAN)])
    calls, posts, _reactions = _wire_activation(
        agent,
        store,
        ["draft", "POST_ORIGINAL"],
        during_guidance=lambda: store.messages.append(
            _msg("101.0", "switch to plan B", user=HUMAN)
        ),
    )
    asyncio.run(agent._activate_inner(_thread_event(), object(), _say))
    # The arrival was not in the prompt, so it must reach the recheck.
    assert len(calls) == 2
    assert "switch to plan B" in calls[1]
    assert posts == ["draft"]


def test_gate_is_skipped_after_mid_turn_reset(tmp_path, monkeypatch):
    import asyncio

    agent = _build_agent(tmp_path, monkeypatch)
    store = _FakeTranscriptStore([_msg("100.0", "please do the task", user=HUMAN)])
    generations = iter([(0, 0)])
    monkeypatch.setattr(
        agent, "_turn_generation", lambda _key: next(generations, (1, 1))
    )
    calls, posts, _reactions = _wire_activation(
        agent,
        store,
        ["draft", "POST_ORIGINAL"],
        during_guidance=lambda: store.messages.append(
            _msg("101.0", "new info", user=HUMAN)
        ),
    )
    asyncio.run(agent._activate_inner(_thread_event(), object(), _say))
    assert len(calls) == 1
    assert posts == ["draft"]


def test_withdrawn_reply_gets_distinct_reaction(tmp_path, monkeypatch):
    import asyncio

    agent = _build_agent(tmp_path, monkeypatch)
    store = _FakeTranscriptStore([_msg("100.0", "please do the task", user=HUMAN)])
    calls, posts, reactions = _wire_activation(
        agent,
        store,
        ["draft", "NO_REPLY"],
        during_guidance=lambda: store.messages.append(
            _msg("101.0", "never mind, done", user=HUMAN)
        ),
    )
    asyncio.run(agent._activate_inner(_thread_event(), object(), _say))
    assert posts == []
    assert reactions[-1] == {
        "add": "zipper_mouth_face",
        "remove": "hourglass_flowing_sand",
    }
