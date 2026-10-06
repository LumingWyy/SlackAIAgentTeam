"""Suite-wide test isolation."""

import pytest


@pytest.fixture(autouse=True)
def _outbox_in_tmp(tmp_path_factory, monkeypatch):
    """Per-turn attachment outboxes go to a temp dir, never the real home."""
    monkeypatch.setenv(
        "SLACK_AGENT_OUTBOX_ROOT", str(tmp_path_factory.mktemp("outbox"))
    )
