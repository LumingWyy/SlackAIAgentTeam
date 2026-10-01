"""Host-side GitHub issue claim tool (slack-agent-claim v2 lease protocol).

Agents run this instead of executing the multi-step git/gh protocol by hand
(the claim becomes a tool result, not a remembered procedure)::

    python issue_claim.py claim   --repo OWNER/REPO --issue N --agent A --node B
    python issue_claim.py renew   ...   # while working, at least every 900s
    python issue_claim.py open-pr ... --title T --body-file F   # hand off
    python issue_claim.py release ...   # giving up without a PR
    python issue_claim.py verify  ...   # read-only ownership check
    python issue_claim.py gc      --repo OWNER/REPO   # drop refs of closed issues

Run it from a checkout of the repository. It prints exactly one JSON object.
Only ``claimed`` / ``renewed`` / ``opened`` / ``released`` / ``owned`` /
``ok`` are successes; ``failed``, ``not_owned`` and ``unknown`` are
fail-closed: do no work and change no issue state. Refs, commit messages and
comment markers match the prompt-driven v2 protocol, so nodes on either
version interoperate.

The tool also owns the status labels so the board always matches the lease:
``claim`` moves an issue status:todo → status:in-progress, ``open-pr`` moves
it to status:in-review (the PR says ``Closes #N``; a human merges it) and
releases the claim, ``release`` puts an unfinished issue back to
status:todo. Label edits are best-effort: the lease, not the label, is
ownership.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from multi_core import (
    STATUS_IN_PROGRESS_LABEL,
    STATUS_IN_REVIEW_LABEL,
    STATUS_TODO_LABEL,
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
SUCCESS_STATUSES = frozenset(
    {"claimed", "renewed", "opened", "released", "owned", "ok"}
)
# One gc run inspects at most this many claim refs.
GC_MAX_REFS = 50
_CLAIM_REF_RE = re.compile(r"refs/heads/slack-agent-claims/issue-(\d+)")
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

    def read_issue(self, issue: int) -> dict:
        """``{"state": ..., "labels": [...]}`` of one issue."""
        rc, out, _err = self._run(["gh", "api", f"repos/{self.repo}/issues/{issue}"])
        if rc != 0:
            raise ClaimReadError("issue read failed")
        try:
            data = json.loads(out)
            labels = [
                str(label.get("name") or "")
                for label in data.get("labels") or []
                if isinstance(label, dict)
            ]
            return {"state": str(data.get("state") or ""), "labels": labels}
        except (ValueError, AttributeError) as exc:
            raise ClaimReadError("issue response is malformed") from exc

    def list_claim_refs(self) -> list[tuple[int, str]]:
        rc, out, err = self._run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{self.repo}/git/matching-refs/heads/slack-agent-claims/",
                "--jq",
                ".[] | [.ref, .object.sha] | @json",
            ]
        )
        if rc != 0:
            raise ClaimReadError("claim ref listing failed")
        refs: list[tuple[int, str]] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                ref, sha = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise ClaimReadError("claim ref listing is malformed") from exc
            match = _CLAIM_REF_RE.fullmatch(str(ref))
            if match and isinstance(sha, str) and sha:
                refs.append((int(match.group(1)), sha))
        return refs

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

    def _edit_labels(
        self, issue: int, *, add: list[str], remove: list[str]
    ) -> str:
        """Best-effort status label move; returns "updated" or "failed"."""
        cmd = ["gh", "issue", "edit", str(issue), "--repo", self.repo]
        for label in add:
            cmd += ["--add-label", label]
        for label in remove:
            cmd += ["--remove-label", label]
        try:
            rc, _out, _err = self._run(cmd)
        except ClaimUnknown:
            return "failed"
        return "updated" if rc == 0 else "failed"

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
        result = self._install(issue, lease, expected, "claimed")
        if result["status"] == "claimed":
            result["labels"] = self._edit_labels(
                issue, add=[STATUS_IN_PROGRESS_LABEL], remove=[STATUS_TODO_LABEL]
            )
        return result

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
        result = _result("released", issue, ref_sha=ref_sha)
        result["labels"] = self._return_unfinished(issue)
        return result

    def _return_unfinished(self, issue: int) -> str:
        """An in-progress issue released without a PR goes back to todo."""
        try:
            labels = self.read_issue(issue)["labels"]
        except (ClaimReadError, ClaimUnknown):
            return "failed"
        if STATUS_IN_PROGRESS_LABEL not in labels:
            return "unchanged"
        return self._edit_labels(
            issue, add=[STATUS_TODO_LABEL], remove=[STATUS_IN_PROGRESS_LABEL]
        )

    def open_pr(
        self,
        issue: int,
        agent: str,
        node: str,
        *,
        title: str,
        body: str,
        thread_url: str = "",
        draft: bool = False,
    ) -> dict:
        """Open the hand-off PR for a claimed issue, then release the claim.

        The PR body closes the issue on merge and links the Slack thread; the
        issue moves to status:in-review. Merging stays with a human.
        """
        now = self.server_now()
        ref_sha = self.read_ref(issue)
        if not ref_sha:
            return _result("failed", issue, reason="no claim; claim the issue first")
        fields, reason = self._evaluate(issue, ref_sha)
        if fields is None:
            return _result("failed", issue, reason=f"claim unverifiable: {reason}")
        if not claim_owned_by(fields, agent, node) or not claim_lease_live(
            fields, now
        ):
            return _result("failed", issue, reason="claim is not held by this agent")
        if not title.strip():
            return _result("failed", issue, reason="a PR title is required")
        rc, out, _err = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        branch = out.strip()
        if rc != 0 or not branch or branch == "HEAD":
            return _result("failed", issue, reason="check out a work branch first")
        rc, out, _err = self._run(
            [
                "gh",
                "repo",
                "view",
                self.repo,
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ]
        )
        base = out.strip()
        if rc != 0 or not base:
            raise ClaimReadError("default branch is unknown")
        if branch == base:
            return _result(
                "failed", issue, reason="commit on a work branch, not the default branch"
            )
        rc, local_sha, _err = self._run(["git", "rev-parse", "HEAD"])
        rc_remote, remote, _err = self._run(
            ["git", "ls-remote", self.url, f"refs/heads/{branch}"]
        )
        remote_sha = remote.split()[0] if remote.split() else ""
        if rc != 0 or rc_remote != 0 or remote_sha != local_sha.strip():
            return _result(
                "failed",
                issue,
                reason=f"push {branch} first: the remote branch is not at HEAD",
            )
        pr_body = body.rstrip() + f"\n\nCloses #{int(issue)}"
        if thread_url:
            pr_body += f"\nSlack thread: {thread_url}"
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            self.repo,
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title.strip(),
            "--body",
            pr_body,
        ]
        if draft:
            cmd.append("--draft")
        rc, out, err = self._run(cmd)
        if rc == 0:
            pr_url = out.strip().splitlines()[-1] if out.strip() else ""
        elif "already exists" in f"{err}{out}":
            rc, out, _err = self._run(
                ["gh", "pr", "view", branch, "--repo", self.repo, "--json", "url",
                 "--jq", ".url"]
            )
            pr_url = out.strip() if rc == 0 else ""
        else:
            return _result("failed", issue, reason="gh pr create failed")
        result = _result("opened", issue, ref_sha=ref_sha)
        result["pr_url"] = pr_url
        result["labels"] = self._edit_labels(
            issue,
            add=[STATUS_IN_REVIEW_LABEL],
            remove=[STATUS_IN_PROGRESS_LABEL, STATUS_TODO_LABEL],
        )
        # The work is handed off: free the ref instead of letting it go stale.
        result["released"] = self._push(issue, ref_sha, None)
        return result

    def gc(self) -> dict:
        """Conditionally delete claim refs whose issue is already closed."""
        deleted: list[int] = []
        refs = self.list_claim_refs()
        for issue, sha in refs[:GC_MAX_REFS]:
            try:
                if self.read_issue(issue)["state"] != "closed":
                    continue
            except ClaimReadError:
                continue
            if self._push(issue, sha, None):
                deleted.append(issue)
        return {"status": "ok", "deleted": deleted, "inspected": min(len(refs), GC_MAX_REFS)}

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
    issue: int = 0,
    agent: str = "",
    node: str = "unspecified",
    cwd: str,
    runner: Runner = run_command,
    **pr_options: object,
) -> dict:
    """Run one operation; every failure mode maps to a fail-closed status."""
    try:
        claimer = IssueClaimer(repo, cwd=cwd, runner=runner)
        if action == "gc":
            return claimer.gc()
        if action == "open-pr":
            return claimer.open_pr(int(issue), agent, node, **pr_options)
        operation = getattr(claimer, action)
        return operation(int(issue), agent, node)
    except ClaimUnknown as exc:
        return _result("unknown", issue, reason=str(exc))
    except (ClaimReadError, ValueError) as exc:
        return _result("failed", issue, reason=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "action",
        choices=("claim", "renew", "open-pr", "release", "verify", "gc"),
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue", type=int)
    parser.add_argument("--agent")
    parser.add_argument("--node", default="unspecified")
    parser.add_argument("--title", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--draft", action="store_true")
    args = parser.parse_args(argv)
    if args.action != "gc" and (args.issue is None or not args.agent):
        parser.error(f"{args.action} requires --issue and --agent")
    pr_options: dict[str, object] = {}
    if args.action == "open-pr":
        body = args.body
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as handle:
                body = handle.read()
        pr_options = {
            "title": args.title,
            "body": body,
            "thread_url": os.environ.get("SLACK_AGENT_THREAD_URL", ""),
            "draft": args.draft,
        }
    result = run_operation(
        args.action,
        repo=args.repo,
        issue=args.issue or 0,
        agent=args.agent or "",
        node=args.node,
        cwd=os.getcwd(),
        **pr_options,
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] in SUCCESS_STATUSES:
        return 0
    return 2 if result["status"] == "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
