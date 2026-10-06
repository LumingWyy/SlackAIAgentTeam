"""team_init.py builds a multi-person team's configs that multi_app accepts."""

import subprocess
from pathlib import Path

import pytest
import yaml

import team_init
import webui
from team_init import TeamError

IDS = {
    "alice": {"dev": ("U0ALDEV1", "B0ALDEV1"), "rev": ("U0ALREV1", "B0ALREV1"), "qa": ("U0ALQA01", "B0ALQA01")},
    "bob": {"dev": ("U0BODEV1", "B0BODEV1"), "rev": ("U0BOREV1", "B0BOREV1"), "qa": ("U0BOQA01", "B0BOQA01")},
}
TEAM_IDS = {"pm": ("U0PM0001", "B0PM0001"), "dx": ("U0DX0001", "B0DX0001")}


def _git_clone(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin",
         "https://github.com/acme/product-x.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    return path


def _team(tmp_path, *, with_ids=True):
    team = yaml.safe_load(
        (team_init.BASE_DIR / "team.example.yaml").read_text(encoding="utf-8")
    )
    for person in team["people"]:
        person["workspace"] = str(_git_clone(tmp_path / f"{person['key']}-clone"))
        person["worktree_root"] = str(tmp_path / f"{person['key']}-worktrees")
        if with_ids:
            person["ids"] = {
                role: {"slack_user_id": user, "slack_bot_id": bot}
                for role, (user, bot) in IDS[person["key"]].items()
            }
    if with_ids:
        for entry in team["team_agents"]:
            user, bot = TEAM_IDS[entry["role"]]
            entry["ids"] = {"slack_user_id": user, "slack_bot_id": bot}
    return team


def test_generated_config_is_accepted_by_multi_app(tmp_path, monkeypatch):
    from multi_app import load_agents_config

    team = _team(tmp_path)
    team_init.build(team, tmp_path / "out")
    for name in ("ALICE_DEV", "ALICE_REV", "ALICE_QA", "PM"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.delenv("AGENT_NODE_ID", raising=False)

    configs, gcfg = load_agents_config(str(tmp_path / "out" / "alice" / "agents.yaml"))

    # her own three plus the team PM she hosts; the DX runs on Bob's machine
    assert [c.name for c in configs] == ["alice_dev", "alice_rev", "alice_qa", "pm"]
    assert {c.workspace_mode for c in configs} == {"thread_worktree"}
    assert {c.github_repo for c in configs} == {"acme/product-x"}
    assert {c.owner for c in configs} == {"U0AAAAAAA"}
    cards = {c.name: c.card for c in configs}
    assert "asks alice_rev for review" in cards["alice_dev"]
    assert "hands the PR to the QA of the PR's owner" in cards["alice_rev"]
    assert "on PASS asks a human to merge" in cards["alice_qa"]
    assert "assignee's developer" in cards["pm"]
    assert sorted(a.name for a in gcfg.logical_agents) == [
        "alice_dev", "alice_qa", "alice_rev", "bob_dev", "bob_qa", "bob_rev", "dx", "pm",
    ]
    assert gcfg.max_agent_rounds == 10
    assert gcfg.owner_daily_total_token_limits == {"U0AAAAAAA": 250000, "U0BBBBBBB": 250000}


def test_agent_names_work_in_the_console():
    """The console's per-agent buttons only accept lowercase, digits and _."""
    for key in ("alice", "a1b2c3d4e5f6"):
        for role in team_init.ROLES:
            assert webui.NAME_RE.fullmatch(team_init.agent_name(key, role))


def test_personas_come_from_the_shared_template(tmp_path):
    team = _team(tmp_path)
    team_init.build(team, tmp_path / "out")
    config = yaml.safe_load((tmp_path / "out" / "bob" / "agents.yaml").read_text(encoding="utf-8"))
    template = {
        a["name"]: a["persona"]
        for a in yaml.safe_load(team_init.AGENTS_TEMPLATE.read_text(encoding="utf-8"))["agents"]
    }
    personas = {a["name"]: a["persona"] for a in config["agents"]}
    assert personas == {
        "bob_dev": template["dev"], "bob_rev": template["reviewer"], "bob_qa": template["qa"],
        "dx": template["dx"],
    }


def test_build_without_ids_still_writes_and_lists_what_is_missing(tmp_path, capsys):
    team = _team(tmp_path, with_ids=False)
    team_path = tmp_path / "team.yaml"
    team_path.write_text(yaml.safe_dump(team), encoding="utf-8")
    assert team_init.main(["build", str(team_path), "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "alice_dev, alice_rev, alice_qa, pm, bob_dev, bob_rev, bob_qa, dx" in out
    assert (tmp_path / "out" / "alice" / "env.example").exists()


def test_env_example_names_the_variables_and_holds_no_values(tmp_path):
    team = _team(tmp_path)
    text = team_init.env_example(team, team["people"][0])
    for var in (
        "ALICE_DEV_SLACK_BOT_TOKEN=", "ALICE_DEV_SLACK_APP_TOKEN=",
        "ALICE_REV_SLACK_BOT_TOKEN=", "ALICE_REV_SLACK_APP_TOKEN=",
        "ALICE_QA_SLACK_BOT_TOKEN=", "ALICE_QA_SLACK_APP_TOKEN=",
        "PM_SLACK_BOT_TOKEN=", "PM_SLACK_APP_TOKEN=",
        "SLACK_AGENT_CONTROL_TOKEN_U0AAAAAAA=",
    ):
        assert var + "\n" in text
    assert "xoxb-" not in text and "xapp-" not in text


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda t: t["people"][0].update(key="Alice"), "lowercase"),
        (lambda t: t["people"][1].update(key="alice"), "duplicate person key"),
        (lambda t: t["people"][1].update(node_id="alice-mac"), "duplicate node_id"),
        (lambda t: t.update(channel="#general"), "channel id"),
        (lambda t: t["people"][0].update(slack_user_id="alice"), "Slack user id"),
        (lambda t: t.update(repo="not a repo"), "(?i)repo"),
        (lambda t: t["team_agents"].append({"role": "pm", "host": "bob"}), "twice"),
        (lambda t: t["team_agents"][0].update(host="carol"), "host must be"),
        (lambda t: t["team_agents"][0].update(role="ceo"), "one of pm, dx"),
        (lambda t: t.update(team_agents=["pm", "dx"]), "must be a mapping"),
    ],
)
def test_bad_team_files_are_rejected(tmp_path, change, message):
    team = _team(tmp_path)
    change(team)
    with pytest.raises(TeamError, match=message):
        team_init.validate_team(team)


def test_ids_reads_each_bot_with_its_own_token(tmp_path):
    team = _team(tmp_path, with_ids=False)
    seen = []

    def fake_call(token, method):
        seen.append((token, method))
        return {"ok": True, "user_id": f"U{token[-4:].upper()}", "bot_id": f"B{token[-4:].upper()}"}

    env = {
        "ALICE_DEV_SLACK_BOT_TOKEN": "xoxb-dev1",
        "ALICE_REV_SLACK_BOT_TOKEN": "xoxb-rev1",
        "ALICE_QA_SLACK_BOT_TOKEN": "xoxb-qa01",
        "PM_SLACK_BOT_TOKEN": "xoxb-pm01",
    }
    ids = team_init.read_ids(team, "alice", env, call=fake_call)
    assert ids == {
        "alice_dev": {"slack_user_id": "UDEV1", "slack_bot_id": "BDEV1"},
        "alice_rev": {"slack_user_id": "UREV1", "slack_bot_id": "BREV1"},
        "alice_qa": {"slack_user_id": "UQA01", "slack_bot_id": "BQA01"},
        "pm": {"slack_user_id": "UPM01", "slack_bot_id": "BPM01"},
    }
    assert [token for token, _method in seen] == ["xoxb-dev1", "xoxb-rev1", "xoxb-qa01", "xoxb-pm01"]


def test_ids_explains_a_missing_token(tmp_path, monkeypatch):
    team = _team(tmp_path, with_ids=False)
    monkeypatch.delenv("ALICE_DEV_SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(TeamError, match="ALICE_DEV_SLACK_BOT_TOKEN is not set"):
        team_init.read_ids(team, "alice", {}, call=lambda *_: {"ok": True})


def test_ids_write_stores_them_in_team_yaml(tmp_path, monkeypatch, capsys):
    team = _team(tmp_path, with_ids=False)
    team_path = tmp_path / "team.yaml"
    team_path.write_text(yaml.safe_dump(team), encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ALICE_DEV_SLACK_BOT_TOKEN=xoxb-aaaa\nALICE_REV_SLACK_BOT_TOKEN=xoxb-bbbb\n"
        "ALICE_QA_SLACK_BOT_TOKEN=xoxb-cccc\nPM_SLACK_BOT_TOKEN=xoxb-dddd\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        team_init, "_slack_auth_test",
        lambda token, method="auth.test": {"ok": True, "user_id": "U0" + token[-4:].upper(), "bot_id": "B0" + token[-4:].upper()},
    )
    code = team_init.main(["ids", str(team_path), "--person", "alice", "--env", str(env_path), "--write"])
    assert code == 0
    stored = yaml.safe_load(team_path.read_text(encoding="utf-8"))
    alice = next(p for p in stored["people"] if p["key"] == "alice")
    assert alice["ids"]["dev"] == {"slack_user_id": "U0AAAA", "slack_bot_id": "B0AAAA"}
    pm = next(t for t in stored["team_agents"] if t["role"] == "pm")
    assert pm["ids"] == {"slack_user_id": "U0DDDD", "slack_bot_id": "B0DDDD"}  # the agent she hosts
    assert "xoxb-" not in team_path.read_text(encoding="utf-8")


def test_generated_files_are_gitignored():
    lines = (team_init.BASE_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("team.yaml", "team-roster.yaml", "/team/"):
        assert pattern in lines


def test_team_skills_reach_every_agent_and_load(tmp_path, monkeypatch):
    from multi_app import load_agents_config

    team = _team(tmp_path)
    team["skills"] = ["answer-me-with-html"]
    team_init.build(team, tmp_path / "out")
    for name in ("BOB_DEV", "BOB_REV", "BOB_QA", "DX"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    configs, _ = load_agents_config(str(tmp_path / "out" / "bob" / "agents.yaml"))
    assert {c.name: c.skills for c in configs} == {
        name: ["answer-me-with-html"] for name in ("bob_dev", "bob_rev", "bob_qa", "dx")
    }


def test_explicitly_empty_admins_stay_empty(tmp_path):
    team = _team(tmp_path)
    team["admins"] = []
    roster = team_init.build_roster(team)
    assert roster["access"]["admins"] == [] and roster["projects"][0]["admins"] == []
    del team["admins"]
    assert team_init.build_roster(team)["access"]["admins"] == ["U0AAAAAAA", "U0BBBBBBB"]


def test_a_small_daily_quota_still_produces_a_loadable_roster(tmp_path, monkeypatch):
    from multi_app import load_agents_config

    team = _team(tmp_path)
    team["daily_total_tokens"] = 10000
    roster = team_init.build_roster(team)
    assert roster["quotas"]["reservation_tokens"]["U0AAAAAAA"] == 10000  # not the 20000 default
    team_init.build(team, tmp_path / "out")
    for name in ("ALICE_DEV", "ALICE_REV", "ALICE_QA", "PM"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    _configs, gcfg = load_agents_config(str(tmp_path / "out" / "alice" / "agents.yaml"))
    assert gcfg.owner_daily_total_token_limits["U0AAAAAAA"] == 10000
    team["reservation_tokens"] = 20000
    with pytest.raises(TeamError, match="cannot exceed"):
        team_init.validate_team(team)


def test_team_agents_keep_the_template_tool_limits(tmp_path):
    """dx is read-only in agents.example.yaml; generating it must not widen that."""
    team = _team(tmp_path)
    team_init.build(team, tmp_path / "out")
    bob = yaml.safe_load((tmp_path / "out" / "bob" / "agents.yaml").read_text(encoding="utf-8"))
    tools = {a["name"]: a.get("allowed_tools") for a in bob["agents"]}
    assert tools["dx"] == ["Read", "Glob", "Grep"]
    assert tools["bob_dev"] is None  # roles without a limit use the defaults


def test_developer_card_says_to_claim_before_working(tmp_path):
    team = _team(tmp_path)
    roster = team_init.build_roster(team)
    card = next(a["card"] for a in roster["agents"] if a["name"] == "alice_dev")
    assert "claiming each with the claim tool first" in card
