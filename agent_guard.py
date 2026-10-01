"""Command guard on the PATH of agent runtimes: agents never merge.

``agent_guard_bin/gh`` and ``agent_guard_bin/git`` route here (git only for
invocations that mention ``push``). A merge-shaped command — ``gh pr merge``,
the merge API, or a push that updates a protected branch — is refused with
exit 1; everything else execs the real binary unchanged.

This is a guard rail for agents that share one GitHub identity, not a
security boundary: a process that bypasses PATH can still merge. Only a
GitHub identity without merge rights (plus branch protection) prevents that.
"""

from __future__ import annotations

import os
import subprocess
import sys

from multi_core import (
    DEFAULT_PROTECTED_BRANCHES,
    gh_merge_violation,
    git_push_protected_targets,
)


def _real_binary(name: str) -> str:
    path = os.environ.get(f"SLACK_AGENT_REAL_{name.upper()}", "")
    if not path:
        print(f"agent guard: the real {name} binary is unknown", file=sys.stderr)
        raise SystemExit(127)
    return path


def _protected_branches() -> list[str]:
    raw = os.environ.get("SLACK_AGENT_PROTECTED_BRANCHES", "")
    names = [name.strip() for name in raw.split(",") if name.strip()]
    return names or list(DEFAULT_PROTECTED_BRANCHES)


def _current_branch(real_git: str, args: list[str]) -> str:
    """Current branch of the repository the push runs in (honors -C)."""
    cmd = [real_git]
    index = 0
    while index < len(args) and args[index] != "push":
        arg = args[index]
        if arg == "-C" and index + 1 < len(args):
            cmd += ["-C", args[index + 1]]
            index += 2
            continue
        if arg.startswith(("--git-dir=", "--work-tree=")):
            cmd.append(arg)
        index += 1
    proc = subprocess.run(
        [*cmd, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def violation(tool: str, args: list[str]) -> str:
    """Why ``tool args`` is refused; "" when it may run."""
    if tool == "gh":
        return gh_merge_violation(args)
    if tool == "git":
        hits = git_push_protected_targets(
            args,
            current_branch=_current_branch(_real_binary("git"), args),
            protected=_protected_branches(),
        )
        if hits:
            return (
                f"pushing to {', '.join(hits)} is a merge; push a work branch, "
                "open a PR, and let a human merge it"
            )
        return ""
    return f"unsupported tool {tool!r}"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: agent_guard.py gh|git ARGS...", file=sys.stderr)
        return 2
    tool, args = argv[0], argv[1:]
    reason = violation(tool, args)
    if reason:
        print(f"agent guard: {reason}", file=sys.stderr)
        return 1
    real = _real_binary(tool)
    os.execv(real, [real, *args])
    return 0  # unreachable


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
