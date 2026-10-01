"""slack-agent-claim v2: pure lease logic and the host-side claim tool."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from issue_claim import ClaimUnknown, IssueClaimer, run_operation
from multi_core import (
    ClaimLease,
    claim_is_stale,
    claim_lease_live,
    evaluate_claim,
    parse_claim_fields,
    parse_claim_markers,
)

REPO = "acme/widgets"
REF_PREFIX = f"repos/{REPO}/git/ref/heads/slack-agent-claims/issue-"
T0 = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)


class FakeGitHub:
    """Just enough of git + gh for the claim protocol, with CAS refs."""

    def __init__(self, now=T0):
        self.now = now
        self.refs: dict[int, str] = {}
        self.commits: dict[str, str] = {}
        self.comments: dict[int, list[str]] = {}
        self.fail: set[str] = set()
        self.timeout: set[str] = set()
        self.before_push = None
        self._next = 0
        self.calls: list[list[str]] = []

    def new_sha(self) -> str:
        self._next += 1
        return f"{self._next:040x}"

    def __call__(self, cmd, cwd, timeout):
        self.calls.append(cmd)
        if cmd[0] == "gh":
            return self._gh(cmd[2:])
        return self._git(cmd[1:])

    def _gh(self, args):
        if args[:2] == ["--include", "rate_limit"]:
            if "time" in self.fail:
                return 1, "", "boom"
            return 0, f"HTTP/2.0 200 OK\nDate: {format_datetime(self.now, usegmt=True)}\n\n{{}}", ""
        if args[0] == "-X":
            issue = int(args[2].split("/")[-2])
            if "comment" in self.fail:
                return 1, "", "HTTP 502"
            self.comments.setdefault(issue, []).append(args[4][len("body="):])
            return 0, "{}", ""
        if args[0] == "--paginate":
            issue = int(args[1].split("/")[-2])
            if "comments" in self.fail:
                return 1, "", "HTTP 500"
            lines = [json.dumps(body) for body in self.comments.get(issue, [])]
            return 0, "\n".join(lines), ""
        path = args[0]
        if path.startswith(REF_PREFIX):
            issue = int(path[len(REF_PREFIX):])
            if "ref" in self.fail:
                return 1, "", "HTTP 500"
            if issue not in self.refs:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps({"object": {"sha": self.refs[issue]}}), ""
        if "/git/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            return 0, json.dumps({"message": self.commits[sha]}), ""
        raise AssertionError(f"unexpected gh call {args}")

    def _git(self, args):
        if args[0] == "fetch":
            return 0, "", ""
        if args[0] == "rev-parse":
            return 0, "base\n", ""
        if "commit-tree" in args:
            message = args[args.index("-m") + 1]
            sha = self.new_sha()
            self.commits[sha] = message
            return 0, sha + "\n", ""
        if args[0] == "push":
            if "push" in self.timeout:
                raise ClaimUnknown("git push timed out")
            if self.before_push is not None:
                hook, self.before_push = self.before_push, None
                hook()
            lease = next(a for a in args if a.startswith("--force-with-lease="))
            ref, expected = lease[len("--force-with-lease="):].rsplit(":", 1)
            issue = int(ref.rsplit("-", 1)[-1])
            refspec = args[-1]
            current = self.refs.get(issue, "")
            if current != expected:
                return 1, "", "! [rejected] (stale info)"
            new_sha = refspec.split(":", 1)[0]
            if new_sha:
                self.refs[issue] = new_sha
            else:
                self.refs.pop(issue, None)
            return 0, "", ""
        raise AssertionError(f"unexpected git call {args}")


def _claimer(github, nonces=None):
    pending = list(nonces or [f"{i:032x}" for i in range(1, 50)])
    return IssueClaimer(
        REPO, cwd="/repo", runner=github, nonce_factory=lambda: pending.pop(0)
    )


def _seed_claim(github, issue, agent, node, *, now, marker=True):
    lease = ClaimLease.new(issue, agent, node, nonce="f" * 32, now=now)
    sha = github.new_sha()
    github.commits[sha] = lease.commit_message()
    github.refs[issue] = sha
    if marker:
        github.comments.setdefault(issue, []).append(lease.comment_body(sha))
    return sha, lease


# ---------------------------------------------------------------------------
# Pure lease logic
# ---------------------------------------------------------------------------


def test_marker_matches_the_v2_wire_format():
    lease = ClaimLease.new(7, "alice/dev", "node --> $(bad)", nonce="ab" * 16, now=T0)
    marker = lease.marker("deadbeef")
    assert marker == (
        "<!-- slack-agent-claim:v2 issue=7 agent=alice/dev "
        "node=node%20--%3E%20%24%28bad%29 nonce=" + "ab" * 16 + " "
        "claimed_at=2026-10-01T03:00:00Z lease_until=2026-10-01T03:30:00Z "
        "ref_sha=deadbeef -->"
    )
    assert parse_claim_markers([lease.comment_body("deadbeef")]) == [
        {**lease.fields(), "ref_sha": "deadbeef"}
    ]


def test_hand_written_commit_message_still_parses():
    message = (
        "slack-agent-claim:v2\n"
        "issue: 7\nagent: dev\nnode: n1\nnonce: abc\n"
        "claimed_at: 2026-10-01T03:00:00Z\nlease_until: 2026-10-01T03:30:00Z\n"
    )
    assert parse_claim_fields(message)["lease_until"] == "2026-10-01T03:30:00Z"


def test_evaluate_claim_requires_exactly_one_matching_marker():
    lease = ClaimLease.new(7, "dev", "n1", nonce="a" * 32, now=T0)
    good = lease.comment_body("sha1")
    fields, reason = evaluate_claim(
        issue=7, ref_sha="sha1", commit_message=lease.commit_message(),
        comment_bodies=[good],
    )
    assert fields is not None and reason == ""
    for bodies, expected in (
        ([], "no claim comment marker"),
        ([good, good], "duplicate claim markers"),
        ([good.replace("n1", "n2")], "node does not match"),
    ):
        fields, reason = evaluate_claim(
            issue=7, ref_sha="sha1", commit_message=lease.commit_message(),
            comment_bodies=bodies,
        )
        assert fields is None and expected in reason
    fields, reason = evaluate_claim(
        issue=8, ref_sha="sha1", commit_message=lease.commit_message(),
        comment_bodies=[good],
    )
    assert fields is None and "another issue" in reason


def test_lease_liveness_and_staleness_boundaries():
    fields = ClaimLease.new(7, "dev", "n1", nonce="a", now=T0).fields()
    assert claim_lease_live(fields, T0 + timedelta(seconds=1799))
    assert not claim_lease_live(fields, T0 + timedelta(seconds=1800))
    # Stale only strictly after lease + 300s grace.
    assert not claim_is_stale(fields, T0 + timedelta(seconds=2100))
    assert claim_is_stale(fields, T0 + timedelta(seconds=2101))
    assert not claim_is_stale({"lease_until": "garbage"}, T0 + timedelta(days=9))


# ---------------------------------------------------------------------------
# IssueClaimer against a fake GitHub
# ---------------------------------------------------------------------------


def test_first_claim_installs_ref_comment_and_verifies():
    github = FakeGitHub()
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "claimed"
    assert github.refs[7] == result["ref_sha"]
    assert result["lease_until"] == "2026-10-01T03:30:00Z"
    [body] = github.comments[7]
    assert f"ref_sha={result['ref_sha']}" in body
    assert body.endswith("claimed by dev on n1")
    push = next(c for c in github.calls if c[:2] == ["git", "push"])
    assert "--force-with-lease=refs/heads/slack-agent-claims/issue-7:" in push
    assert push[-2] == "https://github.com/acme/widgets.git"


def test_live_claim_by_another_agent_is_not_taken():
    github = FakeGitHub()
    _seed_claim(github, 7, "qa", "n2", now=T0)
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "failed"
    assert "held by qa on n2" in result["reason"]


def test_stale_claim_is_taken_over_with_cas_on_old_sha():
    github = FakeGitHub(now=T0 + timedelta(seconds=2101))
    old_sha, _ = _seed_claim(github, 7, "qa", "n2", now=T0)
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "claimed"
    assert github.refs[7] != old_sha
    push = next(c for c in github.calls if c[:2] == ["git", "push"])
    assert f"--force-with-lease=refs/heads/slack-agent-claims/issue-7:{old_sha}" in push


def test_unverifiable_old_claim_is_never_stale():
    github = FakeGitHub(now=T0 + timedelta(days=30))
    _seed_claim(github, 7, "qa", "n2", now=T0, marker=False)
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "failed"
    assert "unverifiable" in result["reason"]
    assert not any(c[:2] == ["git", "push"] for c in github.calls)


def test_losing_the_race_is_fail_closed():
    github = FakeGitHub()

    def rival():
        _seed_claim(github, 7, "qa", "n2", now=T0)

    github.before_push = rival
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "failed"
    assert "lost the claim race" in result["reason"]
    assert github.comments[7][0].endswith("claimed by qa on n2")


def test_unverifiable_install_hands_the_ref_back():
    github = FakeGitHub()
    github.fail.add("comment")
    result = _claimer(github).claim(7, "dev", "n1")
    assert result["status"] == "failed"
    assert "not verifiable" in result["reason"]
    assert 7 not in github.refs


def test_push_timeout_is_unknown_not_success():
    github = FakeGitHub()
    github.timeout.add("push")
    result = run_operation(
        "claim", repo=REPO, issue=7, agent="dev", node="n1", cwd="/repo",
        runner=github,
    )
    assert result["status"] == "unknown"


def test_unreadable_server_time_fails_closed():
    github = FakeGitHub()
    github.fail.add("time")
    result = run_operation(
        "claim", repo=REPO, issue=7, agent="dev", node="n1", cwd="/repo",
        runner=github,
    )
    assert result["status"] == "failed"


def test_claim_is_idempotent_for_the_live_owner():
    github = FakeGitHub()
    claimer = _claimer(github)
    first = claimer.claim(7, "dev", "n1")
    again = claimer.claim(7, "dev", "n1")
    assert again["status"] == "claimed"
    assert again["ref_sha"] == first["ref_sha"]
    assert "already owned" in again["reason"]


def test_renew_moves_the_lease_and_keeps_claimed_at():
    github = FakeGitHub()
    claimer = _claimer(github)
    first = claimer.claim(7, "dev", "n1")
    github.now = T0 + timedelta(seconds=900)
    renewed = claimer.renew(7, "dev", "n1")
    assert renewed["status"] == "renewed"
    assert renewed["ref_sha"] != first["ref_sha"]
    assert renewed["lease_until"] == "2026-10-01T03:45:00Z"
    fields = parse_claim_fields(github.commits[renewed["ref_sha"]])
    assert fields["claimed_at"] == "2026-10-01T03:00:00Z"


def test_renew_refuses_other_owner_and_expired_lease():
    github = FakeGitHub()
    _seed_claim(github, 7, "qa", "n2", now=T0)
    assert _claimer(github).renew(7, "dev", "n1")["status"] == "failed"
    github = FakeGitHub(now=T0 + timedelta(seconds=1801))
    _seed_claim(github, 7, "dev", "n1", now=T0)
    result = _claimer(github).renew(7, "dev", "n1")
    assert result["status"] == "failed"
    assert "expired" in result["reason"]


def test_release_is_conditional_on_verified_ownership():
    github = FakeGitHub()
    claimer = _claimer(github)
    claimer.claim(7, "dev", "n1")
    assert claimer.release(7, "qa", "n2")["status"] == "failed"
    assert claimer.release(7, "dev", "n1")["status"] == "released"
    assert 7 not in github.refs
    assert claimer.release(7, "dev", "n1")["status"] == "released"


def test_verify_reports_ownership_read_only():
    github = FakeGitHub()
    claimer = _claimer(github)
    assert claimer.verify(7, "dev", "n1")["status"] == "not_owned"
    claimer.claim(7, "dev", "n1")
    pushes = sum(c[:2] == ["git", "push"] for c in github.calls)
    assert claimer.verify(7, "dev", "n1")["status"] == "owned"
    assert claimer.verify(7, "qa", "n2")["status"] == "not_owned"
    assert sum(c[:2] == ["git", "push"] for c in github.calls) == pushes


def test_invalid_repo_is_rejected_before_any_command():
    github = FakeGitHub()
    result = run_operation(
        "claim", repo="acme/widgets; rm -rf /", issue=7, agent="dev",
        node="n1", cwd="/repo", runner=github,
    )
    assert result["status"] == "failed"
    assert github.calls == []


@pytest.mark.parametrize("status,code", [("claimed", 0), ("failed", 1), ("unknown", 2)])
def test_cli_prints_one_json_object_and_exit_code(monkeypatch, capsys, status, code):
    import issue_claim

    monkeypatch.setattr(
        issue_claim,
        "run_operation",
        lambda action, **kwargs: {"status": status, "issue": 7},
    )
    assert issue_claim.main(
        ["claim", "--repo", REPO, "--issue", "7", "--agent", "dev"]
    ) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


# ---------------------------------------------------------------------------
# multi_app: host-claim patrol
# ---------------------------------------------------------------------------


def _patrol_agent(tmp_path, monkeypatch):
    import asyncio  # noqa: F401

    from multi_app import Roster, SlackAgent, load_agents_config
    from multi_core import TurnBudget

    yaml_path = tmp_path / "agents.yaml"
    yaml_path.write_text("agents:\n  - name: dev\n    persona: x\n", encoding="utf-8")
    monkeypatch.setenv("DEV_SLACK_BOT_TOKEN", "xoxb-dev")
    monkeypatch.setenv("DEV_SLACK_APP_TOKEN", "xapp-dev")
    monkeypatch.delenv("CLAUDE_WORKSPACE", raising=False)
    configs, _ = load_agents_config(str(yaml_path))
    agent = SlackAgent(
        configs[0], budget=TurnBudget(8), roster=Roster(), allowed_humans=set()
    )
    agent.user_id = "U_SELF"
    agent.bot_id = "B_SELF"
    agent.team_id = "T_TEST"
    agent.github_repo = REPO
    turns: list[str] = []
    posts: list[str] = []

    async def members(*_args, **_kwargs):
        return {"dev"}

    async def provider_turn(prompt, **_kwargs):
        turns.append(prompt)
        return "worked on it"

    async def post(_channel, _thread_ts, text):
        posts.append(text)

    agent._channel_agent_names = members
    agent._patrol_provider_turn = provider_turn
    agent._post_result = post
    return agent, turns, posts


def test_idle_patrol_spends_no_provider_turn(tmp_path, monkeypatch):
    import asyncio

    agent, turns, posts = _patrol_agent(tmp_path, monkeypatch)

    async def no_candidates(*_args):
        return []

    agent._patrol_candidates = no_candidates
    asyncio.run(agent._run_patrol_once(None, "C-PATROL"))
    assert turns == [] and posts == []


def test_patrol_claims_first_claimable_issue_then_works(tmp_path, monkeypatch):
    import asyncio

    agent, turns, posts = _patrol_agent(tmp_path, monkeypatch)
    attempts: list[int] = []

    async def candidates(*_args):
        return [{"number": 3, "title": "taken"}, {"number": 5, "title": "Add CSV export"}]

    async def claim_tool(action, *, repo, issue, config):
        attempts.append(issue)
        if issue == 3:
            return {"status": "failed", "reason": "held by qa"}
        return {"status": "claimed", "issue": issue, "ref_sha": "abc"}

    agent._patrol_candidates = candidates
    agent._run_claim_tool = claim_tool
    asyncio.run(agent._run_patrol_once(None, "C-PATROL"))
    assert attempts == [3, 5]
    [prompt] = turns
    assert "#5" in prompt and "Add CSV export" in prompt
    assert "release --repo acme/widgets --issue 5 --agent dev" in prompt
    assert posts == ["worked on it"]


def test_patrol_gh_failure_counts_toward_the_fence(tmp_path, monkeypatch):
    import asyncio

    agent, turns, _posts = _patrol_agent(tmp_path, monkeypatch)

    async def broken(*_args):
        raise RuntimeError("gh issue list failed: authentication required")

    agent._patrol_candidates = broken
    with pytest.raises(RuntimeError):
        asyncio.run(agent._run_patrol_once(None, "C-PATROL"))
    assert agent._patrol_fence.streak == 1
    assert agent._patrol_fence.category == "auth"
    assert turns == []


def test_lost_lease_cancels_the_turn(tmp_path, monkeypatch):
    import asyncio

    from multi_app import ClaimLeaseLostError

    agent, _turns, _posts = _patrol_agent(tmp_path, monkeypatch)
    cancelled = asyncio.Event()

    async def scenario():
        async def long_work():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return "never"

        renewals = iter([{"status": "renewed"}, {"status": "failed"}])

        async def renew():
            return next(renewals)

        with pytest.raises(ClaimLeaseLostError) as info:
            await agent._with_claim_renewal(
                long_work(), issue=5, renew=renew, renew_every=0.01
            )
        assert info.value.status == "failed"

    asyncio.run(scenario())
    assert cancelled.is_set()


def test_renewal_keeps_running_until_work_finishes(tmp_path, monkeypatch):
    import asyncio

    agent, _turns, _posts = _patrol_agent(tmp_path, monkeypatch)
    renewals: list[int] = []

    async def scenario():
        async def work():
            await asyncio.sleep(0.05)
            return "done"

        async def renew():
            renewals.append(1)
            return {"status": "renewed"}

        return await agent._with_claim_renewal(
            work(), issue=5, renew=renew, renew_every=0.01
        )

    assert asyncio.run(scenario()) == "done"
    assert len(renewals) >= 2


def test_claim_tool_without_verdict_is_unknown(tmp_path, monkeypatch):
    import asyncio

    import multi_app

    agent, _turns, _posts = _patrol_agent(tmp_path, monkeypatch)

    async def garbage(*_args, **_kwargs):
        return 1, "Traceback (most recent call last): ...", "boom"

    monkeypatch.setattr(multi_app, "run_host_command", garbage)
    result = asyncio.run(
        agent._run_claim_tool("claim", repo=REPO, issue=5, config=agent.cfg)
    )
    assert result["status"] == "unknown"


def test_run_host_command_times_out_and_reaps():
    import asyncio
    import sys

    from multi_app import run_host_command

    rc, _out, err = asyncio.run(
        run_host_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=".",
            timeout=0.3,
        )
    )
    assert rc == -1 and err == "timed out"


def test_claim_tool_cli_runs_as_a_script(tmp_path):
    import subprocess
    import sys

    import multi_app

    proc = subprocess.run(
        [sys.executable, multi_app.ISSUE_CLAIM_TOOL, "claim", "--repo", "bad repo",
         "--issue", "1", "--agent", "dev"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["status"] == "failed"
