"""Suite-wide test isolation."""

import os
import sys

import pytest

# Runtime configuration the deployment sets (docker-compose sets most of these
# for the agent node, and `make test-docker` runs the suite in that node's
# container). A test that needs one sets it itself; inherited values would
# point every config load at the node's real roster and node id.
_RUNTIME_ENV = (
    "AGENTS_CONFIG",
    "ROSTER_CONFIG",
    "AGENT_NODE_ID",
    "STATE_DB",
    "WORKTREE_ROOT",
    "WORKTREE_BASE_REF",
    "WORKTREE_MAX_PER_REPO",
    "ADMIN_BIND",
    "ADMIN_PORT",
    "ADMIN_BASE",
)
_RUNTIME_ENV_PREFIXES = ("SLACK_AGENT_CONTROL_TOKEN_",)


@pytest.fixture(autouse=True)
def _isolate_runtime_env(tmp_path_factory, monkeypatch):
    for name in _RUNTIME_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith(_RUNTIME_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    # webui resolves AGENTS_CONFIG once, at import, before this fixture runs.
    webui = sys.modules.get("webui")
    if webui is not None:
        monkeypatch.setattr(webui, "AGENTS_YAML", webui.BASE_DIR / "agents.yaml")
    # Per-turn attachment outboxes go to a temp dir, never the real home.
    monkeypatch.setenv(
        "SLACK_AGENT_OUTBOX_ROOT", str(tmp_path_factory.mktemp("outbox"))
    )
