"""Agents open PRs; humans merge: the guard shims on the agent PATH."""

import os
import stat
import subprocess
import sys

import pytest

from multi_core import gh_merge_violation, git_push_protected_targets

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_BIN = os.path.join(HERE, "agent_guard_bin")


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "merge", "12"],
        ["pr", "merge", "--auto", "--squash", "12"],
        ["pr", "--repo", "acme/widgets", "merge", "12"],
        ["api", "-X", "PUT", "repos/acme/widgets/pulls/3/merge"],
        ["api", "repos/acme/widgets/merges", "-f", "base=main"],
        ["api", "graphql", "-f", "query=mutation { mergePullRequest(input: {}) { clientMutationId } }"],
        ["api", "graphql", "-f", "query=mutation { enablePullRequestAutoMerge(input: {}) { clientMutationId } }"],
        ["alias", "set", "m", "pr merge"],
    ],
)
def test_gh_merge_shapes_are_refused(args):
    assert gh_merge_violation(args)


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "create", "--title", "x"],
        ["pr", "view", "12", "--json", "mergedAt"],
        ["pr", "review", "12", "--comment", "-b", "lgtm"],
        ["issue", "list"],
        ["api", "repos/acme/widgets/pulls/3"],
    ],
)
def test_ordinary_gh_use_is_allowed(args):
    assert gh_merge_violation(args) == ""


@pytest.mark.parametrize(
    "args,current,expected",
    [
        (["push"], "main", ["main"]),
        (["push"], "slack-agent-wt/abc", []),
        (["push", "origin", "HEAD:main"], "x", ["main"]),
        (["push", "origin", "HEAD"], "master", ["master"]),
        (["push", "-u", "origin", "feature"], "x", []),
        (["-C", "/repo", "push", "origin", "+x:refs/heads/main"], "x", ["main"]),
        (["push", "--all", "origin"], "x", ["main", "master"]),
        (["push", "origin", ":main"], "x", ["main"]),
        (["push", "--delete", "origin", "main"], "x", ["main"]),
        (["push", "https://github.com/a/b.git", "abc:refs/heads/slack-agent-claims/issue-7"], "x", []),
        (["push", "origin", "v1:refs/tags/main"], "x", []),
        (["commit", "-m", "push"], "main", []),
        (["status"], "main", []),
    ],
)
def test_git_push_protected_targets(args, current, expected):
    assert (
        git_push_protected_targets(
            args, current_branch=current, protected=["main", "master"]
        )
        == expected
    )


def _fake_binary(tmp_path, name):
    """A stand-in real binary that records its argv and exits 0."""
    log = tmp_path / f"{name}.log"
    path = tmp_path / f"real-{name}"
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "rev-parse" ]; then echo feature; exit 0; fi\n'
        f'echo "$@" >> "{log}"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path), log


def _run_shim(tmp_path, tool, args):
    real_gh, gh_log = _fake_binary(tmp_path, "gh")
    real_git, git_log = _fake_binary(tmp_path, "git")
    env = {
        **os.environ,
        "PATH": GUARD_BIN + os.pathsep + os.environ.get("PATH", ""),
        "SLACK_AGENT_GUARD": os.path.join(HERE, "agent_guard.py"),
        "SLACK_AGENT_PYTHON": sys.executable,
        "SLACK_AGENT_REAL_GH": real_gh,
        "SLACK_AGENT_REAL_GIT": real_git,
        "SLACK_AGENT_PROTECTED_BRANCHES": "main",
    }
    proc = subprocess.run(
        [os.path.join(GUARD_BIN, tool), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    log = gh_log if tool == "gh" else git_log
    return proc, (log.read_text() if log.exists() else "")


def test_gh_shim_refuses_merge_and_passes_the_rest(tmp_path):
    proc, log = _run_shim(tmp_path, "gh", ["pr", "merge", "3"])
    assert proc.returncode == 1
    assert "human-only" in proc.stderr
    assert log == ""
    proc, log = _run_shim(tmp_path, "gh", ["pr", "create", "--title", "t"])
    assert proc.returncode == 0
    assert log.strip() == "pr create --title t"


def test_git_shim_refuses_push_to_protected_branch(tmp_path):
    proc, log = _run_shim(tmp_path, "git", ["push", "origin", "HEAD:main"])
    assert proc.returncode == 1
    assert "pushing to main is a merge" in proc.stderr
    assert log == ""
    proc, log = _run_shim(tmp_path, "git", ["push", "-u", "origin", "feature"])
    assert proc.returncode == 0
    assert log.strip() == "push -u origin feature"
    proc, log = _run_shim(tmp_path, "git", ["status", "--short"])
    assert proc.returncode == 0
    assert "status --short" in log


def test_agent_env_puts_guard_first_and_finds_real_binaries(monkeypatch):
    import multi_app

    env = multi_app.agent_guard_env(thread_url="https://acme.slack.com/archives/C1/p1")
    assert env["PATH"].split(os.pathsep)[0] == multi_app.AGENT_GUARD_BIN
    assert env["SLACK_AGENT_THREAD_URL"].endswith("/p1")
    assert os.path.realpath(os.path.dirname(env["SLACK_AGENT_REAL_GIT"])) != (
        os.path.realpath(multi_app.AGENT_GUARD_BIN)
    )
    # Even when the host PATH already contains the shim dir, the real
    # binary is resolved past it (no self-recursion).
    monkeypatch.setenv(
        "PATH", multi_app.AGENT_GUARD_BIN + os.pathsep + os.environ["PATH"]
    )
    assert "agent_guard_bin" not in multi_app._real_binary("git")
