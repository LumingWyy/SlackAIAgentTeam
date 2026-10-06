"""agents.yaml is local; the tracked agents.example.yaml seeds it."""

import os
from pathlib import Path

import pytest
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


# Skip only inside the image (it sets SLACK_AGENT_IMAGE); in a checkout a
# missing Dockerfile or .gitignore must fail these tests, not skip them.
_repo_only = pytest.mark.skipif(
    os.environ.get("SLACK_AGENT_IMAGE") == "1",
    reason="repository files are not in the image",
)


@_repo_only
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


@_repo_only
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
    repo = "some-organisation/some-long-repository-name"
    assert len(topic.replace("{{OWNER/REPO}}", repo)) <= 250
    assert len(description.replace("{{OWNER/REPO}}", repo)) <= 250
    assert "QA" in topic and "QA" in description  # developer -> reviewer -> QA -> human


def test_repo_agents_template_tells_how_claude_reads_it():
    text = (ROOT / "templates" / "repo-AGENTS.md").read_text(encoding="utf-8")
    assert "CLAUDE.md" in text and "`@AGENTS.md`" in text
    assert "--assignee @me" in text
    assert "claim tool" in text  # the assignee picks the issue; the claim lease decides who works on it


def test_template_qa_checks_the_pr_commit_and_writes_back():
    raw = yaml.safe_load(AGENTS_EXAMPLE.read_text(encoding="utf-8"))
    personas = {a["name"]: a["persona"] for a in raw["agents"]}
    qa = personas["qa"]
    assert "headRefOid" in qa and "git archive" in qa  # the PR's exact commit, clean copy
    assert "mktemp -d" in qa and "/tmp/qa-" not in qa  # fresh private dir, not a guessable path
    assert "gh pr comment" in qa and "QA: PASS" in qa and "QA: FAIL" in qa
    assert "Never merge" in qa
    assert "hand the PR to the QA agent" in personas["reviewer"]


def test_concurrent_first_starts_never_see_a_partial_agents_yaml(tmp_path, monkeypatch):
    """multi_app and the console may both create it at once on the first run."""
    import os

    import local_config

    example = tmp_path / "agents.example.yaml"
    example.write_text("agents: []\n" * 500, encoding="utf-8")
    target = tmp_path / "agents.yaml"
    seen = []
    real_link = os.link

    def racing_link(src, dst):
        # Just before publishing, the target does not exist yet: nobody can
        # have opened a partial file, because nothing is at the path.
        seen.append(target.exists())
        real_link(src, dst)

    monkeypatch.setattr(local_config.os, "link", racing_link)
    assert ensure_agents_config(target, example=example) is True
    assert seen == [False]
    assert target.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".agents.yaml.")] == []
    # The second starter loses the race cleanly and leaves the file alone.
    assert ensure_agents_config(target, example=example) is False
    assert target.read_text(encoding="utf-8") == example.read_text(encoding="utf-8")


def test_a_failed_copy_leaves_no_empty_agents_yaml(tmp_path, monkeypatch):
    import local_config

    example = tmp_path / "agents.example.yaml"
    example.write_text("agents: []\n", encoding="utf-8")
    target = tmp_path / "agents.yaml"

    def broken_copy(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(local_config.shutil, "copyfileobj", broken_copy)
    with pytest.raises(OSError):
        ensure_agents_config(target, example=example)
    assert not target.exists()  # the next start retries instead of loading an empty file
    assert list(tmp_path.iterdir()) == [example]
