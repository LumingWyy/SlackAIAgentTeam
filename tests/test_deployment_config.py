"""Deployment template must isolate one human's local agent node from another."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _mounts(service: dict) -> set[str]:
    return {str(item) for item in service.get("volumes", [])}


def test_compose_defines_isolated_alice_and_bob_nodes():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert {"alice", "bob"} <= set(services)
    alice = services["alice"]
    bob = services["bob"]

    assert "alice" in alice["profiles"]
    assert "bob" in bob["profiles"]
    assert alice["env_file"] != bob["env_file"]
    assert alice["environment"]["AGENT_NODE_ID"] != bob["environment"][
        "AGENT_NODE_ID"
    ]
    assert alice["environment"]["AGENTS_CONFIG"] != bob["environment"][
        "AGENTS_CONFIG"
    ]
    assert alice["ports"] != bob["ports"]
    assert alice["user"] != "root" and bob["user"] != "root"

    alice_mounts = _mounts(alice)
    bob_mounts = _mounts(bob)
    shared_roster = "./roster.yaml:/app/config/roster.yaml:ro"
    assert shared_roster in alice_mounts
    assert shared_roster in bob_mounts
    assert any("alice-state:/app/data" in item for item in alice_mounts)
    assert any("bob-state:/app/data" in item for item in bob_mounts)
    assert not any("bob-" in item for item in alice_mounts)
    assert not any("alice-" in item for item in bob_mounts)
    for owner, mounts in (("alice", alice_mounts), ("bob", bob_mounts)):
        assert any(f"{owner}-codex:" in item for item in mounts)
        assert any(f"{owner}-claude:" in item for item in mounts)
        assert any(f"{owner}-gh:" in item for item in mounts)
        assert any(f"{owner}-workspace:" in item for item in mounts)
        assert any(
            f"{owner}-worktrees:/app/worktrees" in item for item in mounts
        )
        assert (
            services[owner]["environment"]["WORKTREE_ROOT"]
            == "/app/worktrees"
        )
        assert not any(
            f"{owner}-state:/app/worktrees" in item for item in mounts
        )

    assert "alice-worktrees" in compose["volumes"]
    assert "bob-worktrees" in compose["volumes"]


def test_compose_examples_contain_no_shared_or_real_credentials():
    alice = (ROOT / ".env.alice.example").read_text()
    bob = (ROOT / ".env.bob.example").read_text()
    assert alice != bob
    assert "SLACK_AGENT_CONTROL_TOKEN_U01ALICE=" in alice
    assert "SLACK_AGENT_CONTROL_TOKEN_U02BOB=" in bob
    for text in (alice, bob):
        assert "xoxb-" not in text
        assert "xapp-" not in text
        assert "sk-" not in text


def test_runtime_image_contains_auth_modules_and_finishes_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "control_auth.py" in dockerfile
    assert "transcript_store.py" in dockerfile
    assert "worktree_manager.py" in dockerfile
    assert "/app/worktrees" in dockerfile
    assert "chmod 700 /app/data /app/worktrees" in dockerfile
    assert dockerfile.rfind("USER agent") > dockerfile.rfind("COPY ")


def test_all_readmes_document_thread_worktree_safety_and_lifecycle():
    for name in ("README.md", "README.zh.md", "README.ja.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "thread_worktree" in text
        assert "WORKTREE_ROOT" in text
        assert "git worktree" in text
        assert "unpushed" in text
        assert "branch" in text
