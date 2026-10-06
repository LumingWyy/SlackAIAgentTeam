"""This machine's agents.yaml, created from the tracked template on first run.

agents.yaml holds local workspaces and GitHub repos, so it is gitignored like
``.env``; ``agents.example.yaml`` is the shared template.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

AGENTS_EXAMPLE = Path(__file__).resolve().with_name("agents.example.yaml")


def ensure_agents_config(
    path: str | Path, *, example: Path = AGENTS_EXAMPLE
) -> bool:
    """Copy the template to ``path`` when it is a missing ``agents.yaml``.

    Returns True when the file was created. Other names (a node's
    ``agents.alice.yaml``) are never invented from the single-node template.
    """
    target = Path(path).expanduser()
    if target.name != "agents.yaml" or target.exists() or not example.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    # Copy into a temp file first and publish it with a hard link: the link
    # appears complete or not at all and fails if the target exists, so a
    # second process starting at the same moment never reads a half-written
    # file and an existing agents.yaml is never overwritten.
    fd, tmp = tempfile.mkstemp(prefix=".agents.yaml.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out, open(
            example, encoding="utf-8"
        ) as src:
            shutil.copyfileobj(src, out)
        try:
            os.link(tmp, target)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)
