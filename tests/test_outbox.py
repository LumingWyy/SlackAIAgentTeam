"""Per-turn outbox: files an agent saves there are attached to its Slack reply."""

import asyncio
import os

import pytest

from multi_core import OUTBOX_MAX_FILES, parse_skills, select_outbox_files
from tests.test_activation_flow import _build_agent, _event, _say, _wire


def test_only_allowed_regular_files_are_selected():
    accepted, rejected = select_outbox_files(
        [
            ("report.html", 2048, "file"),
            ("chart.PNG", 10, "file"),
            (".env", 40, "file"),
            ("run.sh", 40, "file"),
            ("link.html", 1, "symlink"),
            ("nested", 0, "other"),
            ("huge.pdf", 11 * 1024 * 1024, "file"),
            ("blank.md", 0, "file"),
        ]
    )
    assert accepted == ["chart.PNG", "report.html"]
    assert dict(rejected) == {
        ".env": "file type not allowed",
        "run.sh": "file type not allowed",
        "link.html": "not a regular file",
        "nested": "not a regular file",
        "huge.pdf": "larger than 10 MB",
        "blank.md": "empty",
    }


def test_at_most_five_files_are_attached():
    names = [(f"p{i}.html", 10, "file") for i in range(OUTBOX_MAX_FILES + 2)]
    accepted, rejected = select_outbox_files(names)
    assert len(accepted) == OUTBOX_MAX_FILES
    assert [reason for _name, reason in rejected] == ["more than 5 files"] * 2


def test_skills_are_validated():
    assert parse_skills(None, agent="a") == []
    assert parse_skills(["answer-me-with-html", "answer-me-with-html"], agent="a") == [
        "answer-me-with-html"
    ]
    with pytest.raises(ValueError, match="list"):
        parse_skills("answer-me-with-html", agent="a")
    with pytest.raises(ValueError, match="invalid skill name"):
        parse_skills(["../etc"], agent="a")


class _Uploads:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def files_upload_v2(self, **kwargs):
        # Snapshot what would be sent while the files still exist.
        self.calls.append(
            {**kwargs, "contents": [open(f["file"]).read() for f in kwargs["file_uploads"]]}
        )
        if self.fail:
            raise RuntimeError("slack said no")
        return {"ok": True}


def _with_app(agent, uploads):
    class App:
        client = uploads

    agent.app = App()


def _turn_writing(files):
    """A fake runtime turn that writes ``files`` into its outbox, then answers."""
    import multi_app

    seen = {}

    async def run_turn(prompt, thread_key, gen, **kwargs):
        outbox = multi_app._CURRENT_OUTBOX.get()
        seen["outbox"] = outbox
        seen["env"] = dict(seen.get("env") or {})
        for name, text in files.items():
            with open(os.path.join(outbox, name), "w") as f:
                f.write(text)
        return "summary in 2 lines"

    return run_turn, seen


def test_reply_carries_the_report_and_the_outbox_is_removed(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["unused"])
    uploads = _Uploads()
    _with_app(agent, uploads)
    run_turn, seen = _turn_writing({"report.html": "<html>ok</html>"})
    agent._run_turn = run_turn

    asyncio.run(agent._activate_inner(_event(ts="101.0"), object(), _say))

    assert rec.posts == ["summary in 2 lines"]
    [call] = uploads.calls
    assert call["channel"] == "C1" and call["thread_ts"] == "100.0"
    assert [f["filename"] for f in call["file_uploads"]] == ["report.html"]
    assert call["contents"] == ["<html>ok</html>"]
    assert seen["outbox"].startswith(os.environ["SLACK_AGENT_OUTBOX_ROOT"])
    assert not os.path.exists(seen["outbox"])  # nothing lingers after the turn


def test_rejected_or_failed_attachments_are_reported(tmp_path, monkeypatch):
    agent = _build_agent(tmp_path, monkeypatch)
    rec = _wire(agent, ["unused"])
    _with_app(agent, _Uploads(fail=True))
    run_turn, _seen = _turn_writing({"report.html": "<p>x</p>", "notes.sh": "echo"})
    agent._run_turn = run_turn

    asyncio.run(agent._activate_inner(_event(ts="101.0"), object(), _say))

    assert rec.posts[0] == "summary in 2 lines"
    assert "report.html（upload failed）" in rec.posts[1]
    assert "notes.sh（file type not allowed）" in rec.posts[1]


def test_a_failed_turn_attaches_nothing(tmp_path, monkeypatch):
    import multi_app

    agent = _build_agent(tmp_path, monkeypatch)
    _wire(agent, ["unused"])
    uploads = _Uploads()
    _with_app(agent, uploads)
    seen = {}

    async def failing_turn(prompt, thread_key, gen, **kwargs):
        seen["outbox"] = multi_app._CURRENT_OUTBOX.get()
        with open(os.path.join(seen["outbox"], "half.html"), "w") as f:
            f.write("partial")
        raise RuntimeError("provider blew up")

    agent._run_turn = failing_turn
    asyncio.run(agent._activate_inner(_event(ts="101.0"), object(), _say))
    assert uploads.calls == []
    assert not os.path.exists(seen["outbox"])


def test_runtime_sees_the_outbox_only_during_a_slack_turn(tmp_path, monkeypatch):
    import multi_app

    agent = _build_agent(tmp_path, monkeypatch)
    assert "SLACK_AGENT_OUTBOX" not in agent._agent_env()  # e.g. patrol
    token = multi_app._CURRENT_OUTBOX.set("/tmp/box")
    try:
        assert agent._agent_env()["SLACK_AGENT_OUTBOX"] == "/tmp/box"
    finally:
        multi_app._CURRENT_OUTBOX.reset(token)


def test_prompt_explains_attachments_and_the_report_skill_only_when_usable(
    tmp_path, monkeypatch
):
    import multi_app

    agent = _build_agent(tmp_path, monkeypatch)
    assert "SLACK_AGENT_OUTBOX" not in agent._system_prompt(set())
    token = multi_app._CURRENT_OUTBOX.set("/tmp/box")
    try:
        plain = agent._system_prompt(set())
        assert "SLACK_AGENT_OUTBOX" in plain
        assert "answer-me-with-html" not in plain  # skill not enabled for this agent
        agent.cfg.skills = ["answer-me-with-html"]
        with_skill = agent._system_prompt(set())
        assert '--no-open -o "$SLACK_AGENT_OUTBOX/' in with_skill
    finally:
        multi_app._CURRENT_OUTBOX.reset(token)


def test_claude_turn_enables_only_the_configured_skills(tmp_path, monkeypatch):
    import multi_app
    from claude_agent_sdk import ResultMessage

    agent = _build_agent(tmp_path, monkeypatch)
    captured = []

    async def fake_query(*, prompt, options):
        captured.append(options)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", result="ok", usage={},
        )

    monkeypatch.setattr(multi_app, "query", fake_query)
    asyncio.run(agent._run_claude("p", "C1:1.0", agent._turn_generation("C1:1.0")))
    agent.cfg.skills = ["answer-me-with-html"]
    asyncio.run(agent._run_claude("p", "C1:1.0", agent._turn_generation("C1:1.0")))
    assert captured[0].skills is None  # none configured: CLI defaults, as before
    assert captured[1].skills == ["answer-me-with-html"]


def test_skills_load_from_defaults_and_agent_entries(tmp_path, monkeypatch):
    from multi_app import load_agents_config

    path = tmp_path / "agents.yaml"
    path.write_text(
        "defaults:\n  skills: [answer-me-with-html]\n"
        "agents:\n  - name: a\n    persona: x\n  - name: b\n    persona: y\n    skills: []\n",
        encoding="utf-8",
    )
    for name in ("A", "B"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    configs, _ = load_agents_config(str(path))
    assert {c.name: c.skills for c in configs} == {"a": ["answer-me-with-html"], "b": []}
