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
    return team


def test_generated_config_is_accepted_by_multi_app(tmp_path, monkeypatch):
    from multi_app import load_agents_config

    team = _team(tmp_path)
    team_init.build(team, tmp_path / "out")
    for name in ("ALICE_DEV", "ALICE_REV", "ALICE_QA"):
        monkeypatch.setenv(f"{name}_SLACK_BOT_TOKEN", "xoxb-x")
        monkeypatch.setenv(f"{name}_SLACK_APP_TOKEN", "xapp-x")
    monkeypatch.delenv("AGENT_NODE_ID", raising=False)

    configs, gcfg = load_agents_config(str(tmp_path / "out" / "alice" / "agents.yaml"))

    assert [c.name for c in configs] == ["alice_dev", "alice_rev", "alice_qa"]  # only her own
    assert {c.workspace_mode for c in configs} == {"thread_worktree"}
    assert {c.github_repo for c in configs} == {"acme/product-x"}
    assert {c.owner for c in configs} == {"U0AAAAAAA"}
    cards = {c.name: c.card for c in configs}
    assert "asks alice_rev for review" in cards["alice_dev"]
    assert "hands the PR to the QA of the PR's owner" in cards["alice_rev"]
    assert "on PASS asks a human to merge" in cards["alice_qa"]
    assert sorted(a.name for a in gcfg.logical_agents) == [
        "alice_dev", "alice_qa", "alice_rev", "bob_dev", "bob_qa", "bob_rev",
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
    }


def test_build_without_ids_still_writes_and_lists_what_is_missing(tmp_path, capsys):
    team = _team(tmp_path, with_ids=False)
    team_path = tmp_path / "team.yaml"
    team_path.write_text(yaml.safe_dump(team), encoding="utf-8")
    assert team_init.main(["build", str(team_path), "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "alice_dev, alice_rev, alice_qa, bob_dev, bob_rev, bob_qa" in out
    assert (tmp_path / "out" / "alice" / "env.example").exists()


def test_env_example_names_the_variables_and_holds_no_values(tmp_path):
    team = _team(tmp_path)
    text = team_init.env_example(team["people"][0])
    for var in (
        "ALICE_DEV_SLACK_BOT_TOKEN=", "ALICE_DEV_SLACK_APP_TOKEN=",
        "ALICE_REV_SLACK_BOT_TOKEN=", "ALICE_REV_SLACK_APP_TOKEN=",
        "ALICE_QA_SLACK_BOT_TOKEN=", "ALICE_QA_SLACK_APP_TOKEN=",
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
    }
    ids = team_init.read_ids(team, "alice", env, call=fake_call)
    assert ids == {
        "dev": {"slack_user_id": "UDEV1", "slack_bot_id": "BDEV1"},
        "rev": {"slack_user_id": "UREV1", "slack_bot_id": "BREV1"},
        "qa": {"slack_user_id": "UQA01", "slack_bot_id": "BQA01"},
    }
    assert seen == [
        ("xoxb-dev1", "auth.test"), ("xoxb-rev1", "auth.test"), ("xoxb-qa01", "auth.test"),
    ]


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
        "ALICE_QA_SLACK_BOT_TOKEN=xoxb-cccc\n",
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
    assert "xoxb-" not in team_path.read_text(encoding="utf-8")


def test_generated_files_are_gitignored():
    lines = (team_init.BASE_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("team.yaml", "team-roster.yaml", "/team/"):
        assert pattern in lines
