"""Host-side GitHub issue claim tool (slack-agent-claim v2 lease protocol).

Agents run this instead of executing the multi-step git/gh protocol by hand
(the claim becomes a tool result, not a remembered procedure)::

    python issue_claim.py claim   --repo OWNER/REPO --issue N --agent A --node B
    python issue_claim.py renew   ...   # while working, at least every 900s
    python issue_claim.py release ...   # when done or giving up
    python issue_claim.py verify  ...   # read-only ownership check

Run it from a checkout of the repository. It prints exactly one JSON object.
Only ``claimed`` / ``renewed`` / ``released`` / ``owned`` are successes;
``failed``, ``not_owned`` and ``unknown`` are fail-closed: do no work and
change no issue state. Refs, commit messages and comment markers match the
prompt-driven v2 protocol, so nodes on either version interoperate.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from multi_core import (
    ClaimLease,
    canonical_github_repo,
    claim_is_stale,
    claim_lease_live,
    claim_owned_by,
    claim_ref_name,
    decode_claim_value,
    evaluate_claim,
)

COMMAND_TIMEOUT_SECONDS = 60.0
SUCCESS_STATUSES = frozenset({"claimed", "renewed", "released", "owned"})
CLAIM_COMMIT_IDENTITY = (
    "-c",
    "user.name=slack-agent-claim",
    "-c",
    "user.email=slack-agent-claim@users.noreply.github.com",
)

Runner = Callable[[list[str], str, float], tuple[int, str, str]]


class ClaimUnknown(Exception):
    """An operation's outcome is unknown (timeout); never a success."""


class ClaimReadError(Exception):
    """A required read failed, so the claim state is unverifiable."""


def run_command(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    """Run one git/gh command non-interactively."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaimUnknown(f"{cmd[0]} {cmd[1]} timed out") from exc
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


class IssueClaimer:
    """One repository's claim operations over an injectable command runner."""

    def __init__(
        self,
        repo: str,
        *,
        cwd: str,
        runner: Runner = run_command,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self.repo = canonical_github_repo(repo)
        self.cwd = cwd
        self.url = f"https://github.com/{self.repo}.git"
        self._runner = runner
        self._nonce = nonce_factory

    # -- reads ---------------------------------------------------------------

    def _run(self, cmd: list[str]) -> tuple[int, str, str]:
        return self._runner(cmd, self.cwd, COMMAND_TIMEOUT_SECONDS)

    def server_now(self) -> datetime:
        """GitHub's clock from a fresh ``Date`` header (never the local one)."""
        rc, out, _err = self._run(["gh", "api", "--include", "rate_limit"])
        if rc != 0:
            raise ClaimReadError("GitHub server time is unavailable")
        for line in out.splitlines()[1:]:
            if not line.strip():
                break
            name, _, value = line.partition(":")
            if name.strip().lower() == "date":
                try:
                    return parsedate_to_datetime(value.strip()).astimezone(
                        timezone.utc
                    )
                except (TypeError, ValueError) as exc:
                    raise ClaimReadError("GitHub Date header is malformed") from exc
        raise ClaimReadError("GitHub Date header is missing")

    def read_ref(self, issue: int) -> str | None:
        """Current claim ref SHA, or None when GitHub says it does not exist."""
        rc, out, err = self._run(
            [
                "gh",
                "api",
                f"repos/{self.repo}/git/ref/heads/slack-agent-claims/issue-{issue}",
            ]
        )
        if rc != 0:
            if "HTTP 404" in f"{err}{out}":
                return None
            raise ClaimReadError("claim ref read failed")
        try:
            sha = (json.loads(out).get("object") or {}).get("sha")
        except (ValueError, AttributeError) as exc:
            raise ClaimReadError("claim ref response is malformed") from exc
        if not isinstance(sha, str) or not sha:
            raise ClaimReadError("claim ref response lacks a SHA")
        return sha

    def read_commit_message(self, sha: str) -> str:
        rc, out, _err = self._run(
            ["gh", "api", f"repos/{self.repo}/git/commits/{sha}"]
        )
        if rc != 0:
            raise ClaimReadError("claim commit read failed")
        try:
            message = json.loads(out).get("message")
        except (ValueError, AttributeError) as exc:
            raise ClaimReadError("claim commit response is malformed") from exc
        if not isinstance(message, str):
            raise ClaimReadError("claim commit lacks a message")
        return message

    def read_comment_bodies(self, issue: int) -> list[str]:
        rc, out, _err = self._run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{self.repo}/issues/{issue}/comments?per_page=100",
                "--jq",
                ".[].body | @json",
            ]
        )
        if rc != 0:
            raise ClaimReadError("issue comments read failed")
        bodies: list[str] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                body = json.loads(line)
            except ValueError as exc:
                raise ClaimReadError("issue comments are malformed") from exc
            bodies.append(body if isinstance(body, str) else "")
        return bodies

    def _evaluate(self, issue: int, ref_sha: str) -> tuple[dict | None, str]:
        return evaluate_claim(
            issue=issue,
            ref_sha=ref_sha,
            commit_message=self.read_commit_message(ref_sha),
            comment_bodies=self.read_comment_bodies(issue),
        )

    # -- writes --------------------------------------------------------------

    def _lease_commit(self, lease: ClaimLease) -> str:
        """A unique metadata commit on the default branch tip (worktree untouched)."""
        rc, _out, _err = self._run(
            ["git", "fetch", "--no-tags", "--quiet", self.url, "HEAD"]
        )
        if rc != 0:
            raise ClaimReadError("default branch fetch failed")
        rc, out, _err = self._run(["git", "rev-parse", "FETCH_HEAD"])
        base = out.strip()
        if rc != 0 or not base:
            raise ClaimReadError("default branch tip is unknown")
        rc, out, _err = self._run(
            [
                "git",
                *CLAIM_COMMIT_IDENTITY,
                "commit-tree",
                f"{base}^{{tree}}",
                "-p",
                base,
                "-m",
                lease.commit_message(),
            ]
        )
        sha = out.strip()
        if rc != 0 or not sha:
            raise ClaimReadError("claim commit could not be created")
        return sha

    def _push(self, issue: int, expected_sha: str, new_sha: str | None) -> bool:
        """One atomic compare-and-swap of the claim ref; exit 0 is the only win.

        ``expected_sha == ""`` requires the ref to be absent; ``new_sha is
        None`` deletes it.
        """
        ref = claim_ref_name(issue)
        refspec = f"{new_sha}:{ref}" if new_sha else f":{ref}"
        rc, _out, _err = self._run(
            [
                "git",
                "push",
                "--quiet",
                f"--force-with-lease={ref}:{expected_sha}",
                self.url,
                refspec,
            ]
        )
        return rc == 0

    def _post_comment(self, issue: int, body: str) -> bool:
        rc, _out, _err = self._run(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{self.repo}/issues/{issue}/comments",
                "-f",
                f"body={body}",
            ]
        )
        return rc == 0

    def _verify_installed(
        self, issue: int, sha: str, lease: ClaimLease
    ) -> tuple[bool, str]:
        if self.read_ref(issue) != sha:
            return False, "claim ref moved after the push"
        fields, reason = self._evaluate(issue, sha)
        if fields is None:
            return False, reason
        if fields.get("nonce") != lease.nonce or not claim_owned_by(
            fields, lease.agent, lease.node
        ):
            return False, "claim owner or nonce does not match"
        if not claim_lease_live(fields, self.server_now()):
            return False, "lease already expired"
        return True, ""

    def _install(
        self, issue: int, lease: ClaimLease, expected_sha: str, success: str
    ) -> dict:
        new_sha = self._lease_commit(lease)
        if not self._push(issue, expected_sha, new_sha):
            return _result("failed", issue, reason="lost the claim race")
        self._post_comment(issue, lease.comment_body(new_sha))
        verified, reason = self._verify_installed(issue, new_sha, lease)
        if verified:
            return _result(
                success, issue, ref_sha=new_sha, lease_until=lease.lease_until
            )
        # The ref is ours but ownership cannot be proven: hand it back
        # (conditionally) so it does not block others until it goes stale.
        self._push(issue, new_sha, None)
        return _result(
            "failed", issue, reason=f"claim not verifiable after push: {reason}"
        )

    # -- operations ----------------------------------------------------------

    def claim(self, issue: int, agent: str, node: str) -> dict:
        now = self.server_now()
        ref_sha = self.read_ref(issue)
        expected = ""
        claimed_at = ""
        if ref_sha:
            fields, reason = self._evaluate(issue, ref_sha)
            if fields is None:
                # Missing or malformed data is never stale.
                return _result(
                    "failed", issue, reason=f"existing claim unverifiable: {reason}"
                )
            if claim_owned_by(fields, agent, node):
                if claim_lease_live(fields, now):
                    return _result(
                        "claimed",
                        issue,
                        ref_sha=ref_sha,
                        lease_until=fields["lease_until"],
                        reason="already owned by this claimant",
                    )
                claimed_at = fields["claimed_at"]
            elif not claim_is_stale(fields, now):
                return _result(
                    "failed",
                    issue,
                    reason=(
                        f"held by {decode_claim_value(fields['agent'])} on "
                        f"{decode_claim_value(fields['node'])} until "
                        f"{fields['lease_until']}"
                    ),
                )
            expected = ref_sha
        lease = ClaimLease.new(
            issue, agent, node, nonce=self._nonce(), now=now, claimed_at=claimed_at
        )
        return self._install(issue, lease, expected, "claimed")

    def renew(self, issue: int, agent: str, node: str) -> dict:
        now = self.server_now()
        ref_sha = self.read_ref(issue)
        if not ref_sha:
            return _result("failed", issue, reason="no claim to renew")
        fields, reason = self._evaluate(issue, ref_sha)
        if fields is None:
            return _result("failed", issue, reason=f"claim unverifiable: {reason}")
        if not claim_owned_by(fields, agent, node):
            return _result("failed", issue, reason="claim is owned by another agent")
        if not claim_lease_live(fields, now):
            return _result("failed", issue, reason="lease expired; stop work")
        lease = ClaimLease.new(
            issue,
            agent,
            node,
            nonce=self._nonce(),
            now=now,
            claimed_at=fields["claimed_at"],
        )
        return self._install(issue, lease, ref_sha, "renewed")

    def release(self, issue: int, agent: str, node: str) -> dict:
        ref_sha = self.read_ref(issue)
        if not ref_sha:
            return _result("released", issue, reason="no claim ref present")
        fields, reason = self._evaluate(issue, ref_sha)
        if fields is None:
            return _result(
                "failed", issue, reason=f"claim unverifiable; recover manually: {reason}"
            )
        if not claim_owned_by(fields, agent, node):
            return _result("failed", issue, reason="claim is owned by another agent")
        if not self._push(issue, ref_sha, None):
            return _result(
                "failed", issue, reason="conditional delete rejected; verify again"
            )
        return _result("released", issue, ref_sha=ref_sha)

    def verify(self, issue: int, agent: str, node: str) -> dict:
        now = self.server_now()
        ref_sha = self.read_ref(issue)
        if not ref_sha:
            return _result("not_owned", issue, reason="no claim ref present")
        fields, reason = self._evaluate(issue, ref_sha)
        if fields is None:
            return _result("not_owned", issue, reason=reason)
        if not claim_owned_by(fields, agent, node):
            return _result("not_owned", issue, reason="claim is owned by another agent")
        if not claim_lease_live(fields, now):
            return _result("not_owned", issue, reason="lease expired")
        return _result(
            "owned", issue, ref_sha=ref_sha, lease_until=fields["lease_until"]
        )


def _result(
    status: str,
    issue: int,
    *,
    reason: str = "",
    ref_sha: str = "",
    lease_until: str = "",
) -> dict:
    return {
        "status": status,
        "issue": int(issue),
        "reason": reason,
        "ref_sha": ref_sha,
        "lease_until": lease_until,
    }


def run_operation(
    action: str,
    *,
    repo: str,
    issue: int,
    agent: str,
    node: str,
    cwd: str,
    runner: Runner = run_command,
) -> dict:
    """Run one operation; every failure mode maps to a fail-closed status."""
    try:
        claimer = IssueClaimer(repo, cwd=cwd, runner=runner)
        operation = getattr(claimer, action)
        return operation(int(issue), agent, node)
    except ClaimUnknown as exc:
        return _result("unknown", issue, reason=str(exc))
    except (ClaimReadError, ValueError) as exc:
        return _result("failed", issue, reason=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("claim", "renew", "release", "verify"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--node", default="unspecified")
    args = parser.parse_args(argv)
    result = run_operation(
        args.action,
        repo=args.repo,
        issue=args.issue,
        agent=args.agent,
        node=args.node,
        cwd=os.getcwd(),
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] in SUCCESS_STATUSES:
        return 0
    return 2 if result["status"] == "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
