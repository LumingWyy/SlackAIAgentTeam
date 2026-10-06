"""agents.yaml is local; the tracked agents.example.yaml seeds it."""

from pathlib import Path

import yaml

from local_config import AGENTS_EXAMPLE, ensure_agents_config

ROOT = Path(__file__).resolve().parent.parent


def test_missing_agents_yaml_is_created_from_the_template(tmp_path):
    example = tmp_path / "agents.example.yaml"
    example.write_text("agents: []\n", encoding="utf-8")
    target = tmp_path / "conf" / "agents.yaml"
    assert ensure_agents_config(target, example=example) is True
    assert target.read_text(encoding="utf-8") == "agents: []\n"


def test_existing_agents_yaml_is_never_overwritten(tmp_path):
    example = tmp_path / "agents.example.yaml"
    example.write_text("agents: []\n", encoding="utf-8")
    target = tmp_path / "agents.yaml"
    target.write_text("mine: true\n", encoding="utf-8")
    assert ensure_agents_config(target, example=example) is False
    assert target.read_text(encoding="utf-8") == "mine: true\n"


def test_node_configs_are_not_invented_from_the_template(tmp_path):
    example = tmp_path / "agents.example.yaml"
    example.write_text("agents: []\n", encoding="utf-8")
    target = tmp_path / "agents.alice.yaml"
    assert ensure_agents_config(target, example=example) is False
    assert not target.exists()


def test_tracked_template_loads_and_carries_no_personal_repo(
    tmp_path, monkeypatch
):
    from multi_app import load_agents_config

    raw = yaml.safe_load(AGENTS_EXAMPLE.read_text(encoding="utf-8"))
    assert raw["github"]["repo"] == ""
    assert all("github_repo" not in agent for agent in raw["agents"])
    for name in ("DEV", "REVIEWER"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.delenv("CLAUDE_WORKSPACE", raising=False)
    configs, gcfg = load_agents_config(str(AGENTS_EXAMPLE))
    assert {cfg.name for cfg in configs} >= {"dev", "reviewer"}
    assert gcfg.github_repo is None


def test_image_ships_the_template_not_a_local_agents_yaml():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = " ".join(
        line for line in dockerfile.splitlines() if line.startswith("COPY ")
    ).split()
    assert "agents.example.yaml" in copied
    assert "local_config.py" in copied
    assert "agents.yaml" not in copied


def test_template_reviewer_writes_its_review_onto_the_pr():
    raw = yaml.safe_load(AGENTS_EXAMPLE.read_text(encoding="utf-8"))
    persona = next(a for a in raw["agents"] if a["name"] == "reviewer")["persona"]
    assert "repos/{owner}/{repo}/pulls/<N>/reviews" in persona
    assert '"event": "COMMENT"' in persona
    assert "/replies" in persona  # re-review answers in the existing thread
    assert "Verdict: PASS" in persona and "Verdict: CHANGES REQUESTED" in persona


def test_every_generated_agents_yaml_is_gitignored():
    """AGENTS_CONFIG may point anywhere; a root-only pattern would miss it."""
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "agents.yaml" in lines
    assert "/agents.yaml" not in lines


def test_template_personas_route_by_role_not_by_agent_name():
    """Teams rename agents (alice_dev, ...); a persona naming @dev would break."""
    import re

    raw = yaml.safe_load(AGENTS_EXAMPLE.read_text(encoding="utf-8"))
    for agent in raw["agents"]:
        mentions = set(re.findall(r"@([a-z_]+)", agent.get("persona") or ""))
        assert mentions <= {"agent"}, (agent["name"], mentions)  # "!status <@agent>" is a placeholder


def test_channel_template_fits_slack_limits_once_filled():
    import re

    text = (ROOT / "channel-rules-template.md").read_text(encoding="utf-8")
    topic, description = re.findall(r"```\n(.*?)\n```", text, re.S)[:2]
    project = "x" * 30
    repo = "some-organisation/some-long-repository-name"
    assert len(topic.replace("{{PROJECT}}", project)) <= 250
    assert len(description.replace("{{OWNER/REPO}}", repo)) <= 250


def test_repo_agents_template_tells_how_claude_reads_it():
    text = (ROOT / "templates" / "repo-AGENTS.md").read_text(encoding="utf-8")
    assert "CLAUDE.md" in text and "`@AGENTS.md`" in text
    assert "--assignee @me" in text
