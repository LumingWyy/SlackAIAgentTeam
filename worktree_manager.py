"""Deterministic, persistent Git worktrees for Slack root threads.

Only application-owned Git control operations run here. Provider runtimes may
run branch-local Git commands in the returned worktree, but must not run
``git worktree``, ``git gc`` or ``git prune`` themselves.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from state_store import StateStore


_MAX_WORKTREES_PER_REPO = 256
_MANAGED_WORKTREE_RE = re.compile(r"sat-t-[0-9a-f]{64}\Z")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MANAGED_BRANCH_PREFIX = "refs/heads/slack-agent-wt/"
_MANAGED_BRANCH_REF_RE = re.compile(
    r"refs/heads/slack-agent-wt/([0-9a-f]{64})\Z"
)
_CONTROL_DIRECTORY_NAME = "slack-agent-team-worktree-control"


class WorktreeError(RuntimeError):
    """A safe, fail-closed worktree validation or control error."""


class _LockBusy(RuntimeError):
    """Internal signal for a deliberately nonblocking flock attempt."""


class _RestoreHeadMismatch(WorktreeError):
    """Internal restore failure whose safe status was already persisted."""


@dataclass(frozen=True)
class RepoSpec:
    """Validated base-repository control identity and capacity policy."""

    base_workspace: str
    top_level: str
    common_dir: str
    repo_digest: str
    worktree_root: str
    base_ref: str
    base_oid: str
    max_per_repo: int


@dataclass(frozen=True)
class WorktreePlan:
    """Pure deterministic mapping from a Slack root thread to Git resources."""

    repo: RepoSpec
    team_id: str
    channel_id: str
    root_thread_ts: str
    identity_digest: str
    path: str
    branch: str


@dataclass(frozen=True)
class WorktreeRecord:
    """Verified persistent mapping returned to the scheduler."""

    identity_digest: str
    repo_digest: str
    common_dir: str
    base_workspace: str
    team_id: str
    channel_id: str
    root_thread_ts: str
    owner: str
    path: str
    branch: str
    base_ref: str
    base_oid: str
    retained_head_oid: str
    status: str
    last_error: str
    created_at: float
    last_used_at: float


@dataclass(frozen=True)
class WorktreeLease:
    """One active runtime claim on a verified worktree."""

    record: WorktreeRecord

    @property
    def path(self) -> str:
        return self.record.path

    @property
    def branch(self) -> str:
        return self.record.branch


@dataclass(frozen=True)
class _GitWorktree:
    path: str
    head: str = ""
    branch_ref: str = ""
    prunable: bool = False


def _safe_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "1"
    return env


def _run_git_sync(workspace: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", workspace, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_safe_git_env(),
        )
    except OSError as exc:
        raise WorktreeError(f"cannot execute Git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise WorktreeError(f"Git command failed{suffix}")
    return result.stdout.strip()


def validate_base_ref_text(base_ref: Any) -> str:
    """Validate a branch-like ref before it is ever passed to Git."""
    if not isinstance(base_ref, str):
        raise WorktreeError("worktree base_ref must be a string")
    value = base_ref.strip()
    if value != base_ref or not value or len(value) > 255:
        raise WorktreeError("worktree base_ref is invalid")
    if value.startswith("-") or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise WorktreeError("worktree base_ref is invalid")
    if (
        ".." in value
        or "@{" in value
        or "\\" in value
        or any(char in value for char in " ~^:?*[")
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
        or any(part.startswith(".") for part in value.split("/"))
    ):
        raise WorktreeError("worktree base_ref is invalid")
    return value


def validate_max_per_repo(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorktreeError("worktree max_per_repo must be an integer")
    if value <= 0:
        raise WorktreeError("worktree max_per_repo must be positive")
    if value > _MAX_WORKTREES_PER_REPO:
        raise WorktreeError(
            f"worktree max_per_repo must be at most {_MAX_WORKTREES_PER_REPO}"
        )
    return value


def _validate_no_symlink_components(path: str) -> None:
    """Reject every currently existing symlink in an absolute path."""
    if not os.path.isabs(path):
        raise WorktreeError("worktree root must be an absolute path")
    current = os.path.sep
    for part in Path(path).parts[1:]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise WorktreeError(
                f"cannot inspect worktree root component: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise WorktreeError(
                "worktree root must not contain symlink components"
            )


def normalize_worktree_root(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorktreeError("worktrees.root is required")
    expanded = os.path.expanduser(value.strip())
    if not os.path.isabs(expanded):
        raise WorktreeError("worktrees.root must be an absolute path")
    if any(ord(char) < 32 or ord(char) == 127 for char in expanded):
        raise WorktreeError("worktrees.root contains control characters")
    absolute = os.path.abspath(expanded)
    _validate_no_symlink_components(absolute)
    return os.path.realpath(absolute)


def _paths_overlap(first: str, second: str) -> bool:
    try:
        common = os.path.commonpath((first, second))
    except ValueError:
        return False
    return common == first or common == second


def discover_repo_spec(
    *,
    workspace: str,
    worktree_root: str,
    base_ref: str,
    max_per_repo: int,
) -> RepoSpec:
    """Resolve and validate a local Git repository without changing it."""
    maximum = validate_max_per_repo(max_per_repo)
    root = normalize_worktree_root(worktree_root)
    ref = validate_base_ref_text(base_ref)
    base = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(workspace)))
    )
    try:
        top_level = os.path.realpath(
            _run_git_sync(base, "rev-parse", "--show-toplevel")
        )
    except WorktreeError as exc:
        raise WorktreeError(
            f"workspace is not a Git repository: {base}"
        ) from exc
    common_raw = _run_git_sync(top_level, "rev-parse", "--git-common-dir")
    common_dir = os.path.realpath(
        common_raw
        if os.path.isabs(common_raw)
        else os.path.join(top_level, common_raw)
    )
    if _paths_overlap(root, top_level):
        raise WorktreeError(
            "worktree root and base Git repository must be disjoint"
        )
    _run_git_sync(top_level, "check-ref-format", "--branch", ref)
    try:
        base_oid = _run_git_sync(
            top_level,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        )
    except WorktreeError as exc:
        raise WorktreeError(
            f"worktree base_ref does not resolve to a commit: {ref}"
        ) from exc
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", base_oid):
        raise WorktreeError("worktree base_ref resolved to an invalid object")
    repo_digest = hashlib.sha256(
        json.dumps(
            ["SAT-REPO-v1", common_dir],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RepoSpec(
        base_workspace=top_level,
        top_level=top_level,
        common_dir=common_dir,
        repo_digest=repo_digest,
        worktree_root=root,
        base_ref=ref,
        base_oid=base_oid.lower(),
        max_per_repo=maximum,
    )


def _ensure_secure_directory(path: str) -> None:
    """Create an owner-only directory while refusing existing symlinks."""
    _validate_no_symlink_components(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise WorktreeError(f"cannot create worktree directory: {exc}") from exc
    _validate_no_symlink_components(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise WorktreeError(f"cannot inspect worktree directory: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise WorktreeError("worktree directory is not a real directory")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise WorktreeError("worktree directory must be owned by this process user")
    if info.st_mode & 0o022:
        raise WorktreeError(
            "worktree directory must not be writable by group or others"
        )


def _ensure_control_directory(repo: RepoSpec) -> str:
    """Return the fixed owner-only control directory for one common-dir."""
    common_dir = os.path.realpath(repo.common_dir)
    if common_dir != repo.common_dir or not os.path.isabs(common_dir):
        raise WorktreeError("Git common-dir is not canonical")
    control_dir = os.path.join(common_dir, _CONTROL_DIRECTORY_NAME)
    _ensure_secure_directory(control_dir)
    try:
        info = os.lstat(control_dir)
    except OSError as exc:
        raise WorktreeError(
            f"cannot inspect worktree control directory: {exc}"
        ) from exc
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise WorktreeError(
            "worktree control directory mode was tampered with"
        )
    return control_dir


class _AsyncFileLock:
    """Cancellation-safe nonblocking flock with an owner-only lock file."""

    def __init__(
        self,
        path: str,
        *,
        exclusive: bool = True,
        wait: bool = True,
    ) -> None:
        self.path = path
        self.exclusive = exclusive
        self.wait = wait
        self.fd: int | None = None

    async def __aenter__(self) -> _AsyncFileLock:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        old_umask = os.umask(0o077)
        try:
            try:
                self.fd = os.open(self.path, flags, 0o600)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise WorktreeError(
                        "worktree control lock is a symlink"
                    ) from exc
                raise WorktreeError(
                    f"cannot open worktree control lock: {exc}"
                ) from exc
        finally:
            os.umask(old_umask)
        try:
            fd_info = os.fstat(self.fd)
            path_info = os.lstat(self.path)
            if (
                not stat.S_ISREG(fd_info.st_mode)
                or not stat.S_ISREG(path_info.st_mode)
                or stat.S_ISLNK(path_info.st_mode)
                or (fd_info.st_dev, fd_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or fd_info.st_nlink != 1
                or stat.S_IMODE(fd_info.st_mode) != 0o600
            ):
                raise WorktreeError(
                    "worktree control lock was tampered with"
                )
            if (
                hasattr(os, "geteuid")
                and fd_info.st_uid != os.geteuid()
            ):
                raise WorktreeError(
                    "worktree control lock must be process-owned"
                )
            operation = (
                fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            )
            while True:
                try:
                    fcntl.flock(
                        self.fd, operation | fcntl.LOCK_NB
                    )
                    return self
                except BlockingIOError:
                    if not self.wait:
                        raise _LockBusy
                    await asyncio.sleep(0.02)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    async def __aexit__(self, *_exc_info: object) -> None:
        fd = self.fd
        self.fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class WorktreeManager:
    """One per process; serializes control Git by canonical common-dir."""

    def __init__(self, state_store: StateStore) -> None:
        if not state_store.enabled:
            raise WorktreeError(
                "persistent StateStore is required for thread worktrees"
            )
        self._state_store = state_store
        self._identity_locks: dict[str, asyncio.Lock] = {}
        self._identity_lock_refs: dict[str, int] = {}
        self._active_leases: dict[str, int] = {}

    @staticmethod
    def plan(
        repo: RepoSpec,
        *,
        team_id: str,
        channel_id: str,
        root_thread_ts: str,
    ) -> WorktreePlan:
        identity_data = json.dumps(
            [
                "SAT-WT-v1",
                repo.common_dir,
                str(team_id),
                str(channel_id),
                str(root_thread_ts),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity_data).hexdigest()
        repo_dir = os.path.join(
            repo.worktree_root, f"sat-r-{repo.repo_digest[:16]}"
        )
        return WorktreePlan(
            repo=repo,
            team_id=str(team_id),
            channel_id=str(channel_id),
            root_thread_ts=str(root_thread_ts),
            identity_digest=digest,
            path=os.path.join(repo_dir, f"sat-t-{digest}"),
            branch=f"slack-agent-wt/{digest}",
        )

    async def ensure(
        self, plan: WorktreePlan, *, owner: str
    ) -> WorktreeRecord:
        """Create or verify one worktree under singleflight and repo locks."""
        self._validate_plan(plan)
        digest = plan.identity_digest
        identity_lock = self._identity_locks.setdefault(
            digest, asyncio.Lock()
        )
        self._identity_lock_refs[digest] = (
            self._identity_lock_refs.get(digest, 0) + 1
        )
        try:
            async with identity_lock:
                lock_dir = _ensure_control_directory(plan.repo)
                lock_path = os.path.join(
                    lock_dir, f"repo-{plan.repo.repo_digest}.lock"
                )
                async with _AsyncFileLock(lock_path):
                    return await self._ensure_locked(
                        plan, owner=str(owner)
                    )
        finally:
            remaining = self._identity_lock_refs.get(digest, 1) - 1
            if remaining <= 0:
                self._identity_lock_refs.pop(digest, None)
                if self._identity_locks.get(digest) is identity_lock:
                    self._identity_locks.pop(digest, None)
            else:
                self._identity_lock_refs[digest] = remaining

    @asynccontextmanager
    async def lease(
        self, plan: WorktreePlan, *, owner: str
    ):
        """Hold a crash-safe identity lease for ensure plus runtime execution."""
        self._validate_plan(plan)
        lock_dir = _ensure_control_directory(plan.repo)
        lease_path = os.path.join(
            lock_dir, f"lease-{plan.identity_digest}.lock"
        )
        async with _AsyncFileLock(lease_path, exclusive=True):
            digest = plan.identity_digest
            self._active_leases[digest] = (
                self._active_leases.get(digest, 0) + 1
            )
            try:
                record = await self.ensure(plan, owner=owner)
                yield WorktreeLease(record)
            finally:
                remaining = self._active_leases.get(digest, 1) - 1
                if remaining <= 0:
                    self._active_leases.pop(digest, None)
                else:
                    self._active_leases[digest] = remaining

    def active_lease_count(self, identity_digest: str | None = None) -> int:
        """Return process-local active/ensuring lease count for tests/status."""
        if identity_digest is None:
            return sum(self._active_leases.values())
        return self._active_leases.get(identity_digest, 0)

    def status_snapshot(
        self, *, owner: str | None = None
    ) -> list[dict[str, Any]]:
        """Return owner-scoped, credential-free worktree monitor records."""
        rows = self._state_store.list_worktree_mappings()
        result: list[dict[str, Any]] = []
        for row in rows:
            mapping_owner = str(row.get("owner") or "")
            if owner is not None and mapping_owner != owner:
                continue
            digest = str(row.get("identity_digest") or "")
            result.append(
                {
                    "identity_digest": digest,
                    "repo_digest": str(row.get("repo_digest") or ""),
                    "team_id": str(row.get("team_id") or ""),
                    "channel_id": str(row.get("channel_id") or ""),
                    "root_thread_ts": str(
                        row.get("root_thread_ts") or ""
                    ),
                    "owner": mapping_owner,
                    "path": str(row.get("path") or ""),
                    "branch": str(row.get("branch") or ""),
                    "base_ref": str(row.get("base_ref") or ""),
                    "status": str(row.get("status") or ""),
                    "last_error": str(row.get("last_error") or ""),
                    "active_leases": self.active_lease_count(digest),
                    "created_at": float(row.get("created_at") or 0),
                    "last_used_at": float(row.get("last_used_at") or 0),
                    "updated_at": float(row.get("updated_at") or 0),
                }
            )
        return result

    def managed_attachment_roots(
        self,
        repo: RepoSpec,
        *,
        owner: str,
        limit: int = _MAX_WORKTREES_PER_REPO,
    ) -> list[str]:
        """Return existing attachment roots from exact, owner-scoped mappings.

        The registry is untrusted input for cleanup purposes. Reconstructing
        every deterministic plan prevents a tampered row from turning the
        attachment sweeper into an arbitrary-path deleter.
        """
        maximum = min(
            _MAX_WORKTREES_PER_REPO,
            max(0, int(limit)),
        )
        if maximum == 0:
            return []
        rows = self._state_store.list_worktree_mappings(
            common_dir=repo.common_dir,
            owner=str(owner),
            limit=maximum,
        )
        try:
            worktree_output = _run_git_sync(
                repo.base_workspace,
                "worktree",
                "list",
                "--porcelain",
            )
        except WorktreeError:
            return []
        registered: dict[str, str] = {}
        for block in worktree_output.split("\n\n"):
            values: dict[str, str] = {}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                if key:
                    values[key] = value
            path = values.get("worktree")
            if path:
                registered[os.path.realpath(path)] = values.get(
                    "branch", ""
                )
        roots: list[str] = []
        for mapping in rows:
            if len(roots) >= maximum:
                break
            if str(mapping.get("owner") or "") != str(owner):
                continue
            if str(mapping.get("status") or "") == "removed":
                continue
            try:
                plan = self.plan(
                    repo,
                    team_id=str(mapping.get("team_id") or ""),
                    channel_id=str(mapping.get("channel_id") or ""),
                    root_thread_ts=str(
                        mapping.get("root_thread_ts") or ""
                    ),
                )
                self._validate_mapping(mapping, plan, str(owner))
                if os.path.realpath(plan.path) != plan.path:
                    raise WorktreeError(
                        "managed worktree path is not canonical"
                    )
                if registered.get(plan.path) != (
                    f"refs/heads/{plan.branch}"
                ):
                    raise WorktreeError(
                        "managed worktree is not registered at its branch"
                    )
                _validate_no_symlink_components(plan.path)
                path_info = os.lstat(plan.path)
                git_info = os.lstat(os.path.join(plan.path, ".git"))
                if (
                    stat.S_ISLNK(path_info.st_mode)
                    or not stat.S_ISDIR(path_info.st_mode)
                    or stat.S_ISLNK(git_info.st_mode)
                    or not stat.S_ISREG(git_info.st_mode)
                ):
                    raise WorktreeError(
                        "managed worktree path failed filesystem validation"
                    )
                if (
                    hasattr(os, "geteuid")
                    and path_info.st_uid != os.geteuid()
                ):
                    raise WorktreeError(
                        "managed worktree must be process-owned"
                    )
            except (OSError, WorktreeError):
                continue
            roots.append(plan.path)
        return roots

    async def remove(
        self, plan: WorktreePlan, *, owner: str
    ) -> WorktreeRecord:
        """Remove one verified clean worktree while retaining its branch."""
        self._validate_plan(plan)
        digest = plan.identity_digest
        if self.active_lease_count(digest):
            raise WorktreeError(
                "worktree is active and cannot be removed"
            )
        lock_dir = _ensure_control_directory(plan.repo)
        lease_path = os.path.join(lock_dir, f"lease-{digest}.lock")
        try:
            async with _AsyncFileLock(
                lease_path, exclusive=True, wait=False
            ):
                return await self._remove_singleflight(
                    plan, owner=str(owner)
                )
        except _LockBusy as exc:
            raise WorktreeError(
                "worktree is active in another process and cannot be removed"
            ) from exc

    async def _remove_singleflight(
        self, plan: WorktreePlan, *, owner: str
    ) -> WorktreeRecord:
        digest = plan.identity_digest
        identity_lock = self._identity_locks.setdefault(
            digest, asyncio.Lock()
        )
        self._identity_lock_refs[digest] = (
            self._identity_lock_refs.get(digest, 0) + 1
        )
        try:
            async with identity_lock:
                lock_dir = _ensure_control_directory(plan.repo)
                lock_path = os.path.join(
                    lock_dir, f"repo-{plan.repo.repo_digest}.lock"
                )
                async with _AsyncFileLock(lock_path):
                    return await self._remove_locked(plan, owner=owner)
        finally:
            remaining = self._identity_lock_refs.get(digest, 1) - 1
            if remaining <= 0:
                self._identity_lock_refs.pop(digest, None)
                if self._identity_locks.get(digest) is identity_lock:
                    self._identity_locks.pop(digest, None)
            else:
                self._identity_lock_refs[digest] = remaining

    async def _remove_locked(
        self, plan: WorktreePlan, *, owner: str
    ) -> WorktreeRecord:
        mapping = self._state_store.load_worktree_mapping(
            plan.identity_digest
        )
        if mapping is None:
            raise WorktreeError("worktree mapping does not exist")
        self._validate_mapping(mapping, plan, owner)
        entries = await self._worktree_entries(plan.repo)
        expected_entry = self._entry_at(entries, plan.path)
        try:
            path_info = os.lstat(plan.path)
        except FileNotFoundError:
            path_info = None
        except OSError as exc:
            raise WorktreeError(
                f"cannot inspect planned worktree path: {exc}"
            ) from exc

        if path_info is None:
            if expected_entry is not None:
                raise WorktreeError(
                    "worktree path is missing but remains registered"
                )
            status = str(mapping.get("status") or "")
            if status not in {"removed", "removing"}:
                raise WorktreeError(
                    "worktree path is missing or has been tampered with"
                )
            retained_head = await self._validate_removed_retained_head(
                mapping, plan
            )
            if status == "removing":
                if not self._state_store.mark_worktree_removed(
                    identity_digest=plan.identity_digest,
                    expected_path=plan.path,
                    expected_status="removing",
                    retained_head_oid=retained_head,
                ):
                    raise WorktreeError(
                        "cannot atomically finalize interrupted "
                        "worktree removal"
                    )
                mapping = self._state_store.load_worktree_mapping(
                    plan.identity_digest
                )
                if mapping is None:
                    raise WorktreeError(
                        "cannot reload finalized worktree removal"
                    )
            return self._record_from_mapping(mapping, status="removed")

        if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISDIR(
            path_info.st_mode
        ):
            raise WorktreeError(
                "worktree path is a symlink or has been tampered with"
            )
        if str(mapping.get("status") or "") == "removed":
            raise WorktreeError(
                "removed worktree path unexpectedly exists; refusing deletion"
            )
        if expected_entry is None:
            raise WorktreeError(
                "worktree path exists outside Git control metadata"
            )
        await self._verify_existing(plan, expected_entry)

        porcelain = await self._run_git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
            cwd=plan.path,
        )
        if porcelain:
            raise WorktreeError(
                "worktree has dirty or untracked changes and cannot be removed"
            )

        head = await self._run_git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
            cwd=plan.path,
        )
        if not _HEX_DIGEST_RE.fullmatch(head) and not re.fullmatch(
            r"[0-9a-fA-F]{40}", head
        ):
            raise WorktreeError("worktree HEAD is invalid")
        head = head.lower()
        seed = str(mapping.get("base_oid") or plan.repo.base_oid).lower()
        if head != seed:
            remote_refs = await self._run_git(
                "for-each-ref",
                "--format=%(refname)",
                f"--contains={head}",
                "refs/remotes",
                cwd=plan.path,
            )
            if not any(
                line.startswith("refs/remotes/")
                for line in remote_refs.splitlines()
            ):
                raise WorktreeError(
                    "worktree contains unpushed commits and cannot be removed"
                )

        branch_head = await self._exact_branch_head(plan)
        if branch_head != head:
            raise WorktreeError(
                "managed worktree branch changed before removal"
            )
        mapping_status = str(mapping.get("status") or "")
        if mapping_status == "removing":
            retained_head = self._retained_head_from_mapping(mapping)
            if retained_head != head:
                raise WorktreeError(
                    "interrupted removal worktree HEAD changed from "
                    "its retained HEAD"
                )
        else:
            if not self._state_store.begin_worktree_removal(
                identity_digest=plan.identity_digest,
                expected_path=plan.path,
                expected_status=mapping_status,
                retained_head_oid=head,
            ):
                raise WorktreeError(
                    "cannot atomically persist worktree removing transition"
                )
        await self._run_git(
            "worktree",
            "remove",
            plan.path,
            cwd=plan.repo.base_workspace,
        )
        try:
            os.lstat(plan.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorktreeError(
                f"cannot verify removed worktree path: {exc}"
            ) from exc
        else:
            raise WorktreeError("Git left the removed worktree path behind")
        refreshed = await self._worktree_entries(plan.repo)
        if self._entry_at(refreshed, plan.path) is not None:
            raise WorktreeError(
                "Git left the removed worktree registered"
            )
        post_remove_branch_error: WorktreeError | None = None
        try:
            branch_head = await self._exact_branch_head(plan)
        except WorktreeError as exc:
            post_remove_branch_error = exc
            branch_head = ""
        if post_remove_branch_error is not None or branch_head != head:
            if not self._state_store.mark_worktree_removed(
                identity_digest=plan.identity_digest,
                expected_path=plan.path,
                expected_status="removing",
                retained_head_oid=head,
            ):
                raise WorktreeError(
                    "cannot atomically persist removed worktree HEAD "
                    "after branch changed during removal"
                ) from post_remove_branch_error
            raise WorktreeError(
                "managed worktree branch changed during removal; "
                "the original removed HEAD was retained"
            ) from post_remove_branch_error
        if not self._state_store.mark_worktree_removed(
            identity_digest=plan.identity_digest,
            expected_path=plan.path,
            expected_status="removing",
            retained_head_oid=head,
        ):
            raise WorktreeError(
                "cannot atomically persist removed worktree HEAD"
            )
        saved = self._state_store.load_worktree_mapping(
            plan.identity_digest
        )
        if saved is None:
            raise WorktreeError("cannot reload removed worktree mapping")
        return self._record_from_mapping(saved, status="removed")

    async def _exact_branch_head(self, plan: WorktreePlan) -> str:
        branch_head = await self._run_git(
            "show-ref",
            "--verify",
            "--hash",
            f"refs/heads/{plan.branch}",
            cwd=plan.repo.base_workspace,
        )
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", branch_head):
            raise WorktreeError(
                "managed worktree branch is missing or has been tampered with"
            )
        return branch_head.lower()

    @staticmethod
    def _retained_head_from_mapping(mapping: dict[str, Any]) -> str:
        retained = str(mapping.get("retained_head_oid") or "")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", retained):
            raise WorktreeError(
                "removed worktree has no valid retained HEAD; "
                "legacy or tampered mappings cannot be restored"
            )
        return retained

    async def _validate_removed_retained_head(
        self,
        mapping: dict[str, Any],
        plan: WorktreePlan,
    ) -> str:
        retained = self._retained_head_from_mapping(mapping)
        branch_head = await self._exact_branch_head(plan)
        if branch_head != retained:
            raise WorktreeError(
                "managed branch HEAD changed from the retained removed HEAD"
            )
        return retained

    async def _required_restore_head(
        self,
        mapping: dict[str, Any] | None,
        plan: WorktreePlan,
    ) -> str:
        if mapping is None:
            return ""
        status = str(mapping.get("status") or "")
        retained = str(mapping.get("retained_head_oid") or "")
        if status in {"removed", "removing"} or (
            status in {"creating", "error"} and retained
        ):
            return await self._validate_removed_retained_head(
                mapping, plan
            )
        return ""

    async def _worktree_head(self, plan: WorktreePlan) -> str:
        head = await self._run_git(
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
            cwd=plan.path,
        )
        if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", head):
            raise WorktreeError("restored worktree HEAD is invalid")
        return head.lower()

    async def _validate_worktree_retained_head(
        self,
        plan: WorktreePlan,
        retained_head: str,
    ) -> None:
        worktree_head = await self._worktree_head(plan)
        branch_head = await self._exact_branch_head(plan)
        if (
            worktree_head != retained_head
            or branch_head != retained_head
        ):
            raise WorktreeError(
                "restored worktree HEAD changed from retained HEAD"
            )

    @staticmethod
    def _record_from_mapping(
        mapping: dict[str, Any],
        *,
        status: str | None = None,
        base_oid: str | None = None,
    ) -> WorktreeRecord:
        return WorktreeRecord(
            identity_digest=str(mapping["identity_digest"]),
            repo_digest=str(mapping["repo_digest"]),
            common_dir=str(mapping["common_dir"]),
            base_workspace=str(mapping["base_workspace"]),
            team_id=str(mapping["team_id"]),
            channel_id=str(mapping["channel_id"]),
            root_thread_ts=str(mapping["root_thread_ts"]),
            owner=str(mapping["owner"]),
            path=str(mapping["path"]),
            branch=str(mapping["branch"]),
            base_ref=str(mapping["base_ref"]),
            base_oid=(
                str(base_oid)
                if base_oid is not None
                else str(mapping["base_oid"])
            ),
            retained_head_oid=str(
                mapping.get("retained_head_oid") or ""
            ),
            status=(
                str(status)
                if status is not None
                else str(mapping["status"])
            ),
            last_error=str(mapping.get("last_error") or ""),
            created_at=float(mapping["created_at"]),
            last_used_at=float(mapping["last_used_at"]),
        )

    @staticmethod
    def _validate_plan(plan: WorktreePlan) -> None:
        if not _HEX_DIGEST_RE.fullmatch(plan.identity_digest):
            raise WorktreeError("worktree identity digest is invalid")
        expected = WorktreeManager.plan(
            plan.repo,
            team_id=plan.team_id,
            channel_id=plan.channel_id,
            root_thread_ts=plan.root_thread_ts,
        )
        if expected != plan:
            raise WorktreeError("worktree plan failed deterministic validation")
        _validate_no_symlink_components(plan.repo.worktree_root)
        if _paths_overlap(plan.repo.worktree_root, plan.repo.top_level):
            raise WorktreeError(
                "worktree root and base Git repository must be disjoint"
            )

    async def _ensure_locked(
        self, plan: WorktreePlan, *, owner: str
    ) -> WorktreeRecord:
        repo_dir = str(Path(plan.path).parent)
        _ensure_secure_directory(repo_dir)
        existing_mapping = self._state_store.load_worktree_mapping(
            plan.identity_digest
        )
        entries = await self._worktree_entries(plan.repo)
        existing_mapping = await self._prepare_mapping_for_plan(
            existing_mapping,
            plan,
            owner=owner,
            entries=entries,
        )
        self._validate_mapping(existing_mapping, plan, owner)
        retained_restore_head = await self._required_restore_head(
            existing_mapping,
            plan,
        )
        expected_entry = self._entry_at(entries, plan.path)

        try:
            path_info = os.lstat(plan.path)
        except FileNotFoundError:
            path_info = None
        except OSError as exc:
            raise WorktreeError(
                f"cannot inspect planned worktree path: {exc}"
            ) from exc

        if path_info is not None:
            if stat.S_ISLNK(path_info.st_mode):
                raise WorktreeError(
                    "worktree path is a symlink or has been tampered with"
                )
            if not stat.S_ISDIR(path_info.st_mode):
                raise WorktreeError(
                    "worktree path exists but is not a directory"
                )
            if expected_entry is None:
                raise WorktreeError(
                    "worktree path exists outside Git control metadata"
                )
            await self._verify_existing(plan, expected_entry)
            if retained_restore_head:
                await self._validate_worktree_retained_head(
                    plan, retained_restore_head
                )
            return self._persist_ready(plan, owner, existing_mapping)

        if expected_entry is not None:
            # A manually removed directory may leave only prunable metadata.
            await self._run_git(
                "worktree", "prune", cwd=plan.repo.base_workspace
            )
            entries = await self._worktree_entries(plan.repo)
            expected_entry = self._entry_at(entries, plan.path)
            if expected_entry is not None:
                raise WorktreeError(
                    "planned worktree path is missing but still registered"
                )

        expected_branch_ref = f"refs/heads/{plan.branch}"
        for entry in entries:
            if (
                entry.branch_ref == expected_branch_ref
                and os.path.realpath(entry.path)
                != os.path.realpath(plan.path)
            ):
                raise WorktreeError(
                    "planned worktree branch is already checked out at "
                    "another path"
                )

        branch_hash = await self._run_git(
            "show-ref",
            "--verify",
            "--hash",
            expected_branch_ref,
            cwd=plan.repo.base_workspace,
            # Git versions disagree between 1 and 128 for an absent exact ref.
            # The ref text is already strictly validated and an empty stdout is
            # the authoritative "not found" result in either case.
            allowed_returncodes=(0, 1, 128),
        )
        branch_exists = bool(branch_hash.strip())
        if retained_restore_head and (
            not branch_exists
            or branch_hash.strip().lower() != retained_restore_head
        ):
            raise WorktreeError(
                "managed branch HEAD changed before removed worktree restore"
            )
        self._check_capacity(entries, plan)
        self._persist_status(
            plan,
            owner,
            status="creating",
            existing=existing_mapping,
        )
        try:
            if branch_exists:
                await self._run_git(
                    "worktree",
                    "add",
                    plan.path,
                    plan.branch,
                    cwd=plan.repo.base_workspace,
                )
            else:
                await self._run_git(
                    "worktree",
                    "add",
                    "-b",
                    plan.branch,
                    plan.path,
                    # Pin the exact commit validated by discover_repo_spec.
                    # A concurrent branch update must not change the seed.
                    plan.repo.base_oid,
                    cwd=plan.repo.base_workspace,
                )
            refreshed = await self._worktree_entries(plan.repo)
            created_entry = self._entry_at(refreshed, plan.path)
            if created_entry is None:
                raise WorktreeError(
                    "Git did not register the created worktree"
                )
            await self._verify_existing(plan, created_entry)
            if retained_restore_head:
                try:
                    await self._validate_worktree_retained_head(
                        plan, retained_restore_head
                    )
                except WorktreeError:
                    cleaned = await self._cleanup_failed_restore(plan)
                    self._persist_status(
                        plan,
                        owner,
                        status=("removed" if cleaned else "error"),
                        existing=existing_mapping,
                        last_error=(
                            "retained HEAD changed during restore"
                        ),
                    )
                    raise _RestoreHeadMismatch(
                        "restored worktree HEAD changed from retained HEAD"
                    )
            return self._persist_ready(plan, owner, existing_mapping)
        except asyncio.CancelledError:
            try:
                self._persist_status(
                    plan,
                    owner,
                    status="error",
                    existing=existing_mapping,
                    last_error="cancelled",
                )
            except WorktreeError:
                # Cancellation must not be converted into a registry error.
                # The next ensure will revalidate Git/path state fail-closed.
                pass
            raise
        except Exception as exc:
            if isinstance(exc, _RestoreHeadMismatch):
                raise
            message = (
                str(exc)
                if isinstance(exc, WorktreeError)
                else type(exc).__name__
            )
            self._persist_status(
                plan,
                owner,
                status="error",
                existing=existing_mapping,
                last_error=message[:500],
            )
            raise

    async def _prepare_mapping_for_plan(
        self,
        mapping: dict[str, Any] | None,
        plan: WorktreePlan,
        *,
        owner: str,
        entries: list[_GitWorktree],
    ) -> dict[str, Any] | None:
        """Fail closed on live old roots; CAS-rehome removed mappings only."""
        if mapping is None or str(mapping.get("path") or "") == plan.path:
            return mapping
        expected_without_path = {
            "identity_digest": plan.identity_digest,
            "repo_digest": plan.repo.repo_digest,
            "common_dir": plan.repo.common_dir,
            "base_workspace": plan.repo.base_workspace,
            "team_id": plan.team_id,
            "channel_id": plan.channel_id,
            "root_thread_ts": plan.root_thread_ts,
            "owner": owner,
            "branch": plan.branch,
        }
        mismatches = [
            key
            for key, value in expected_without_path.items()
            if str(mapping.get(key, "")) != value
        ]
        if mismatches:
            raise WorktreeError(
                "persisted worktree mapping failed identity validation: "
                + ", ".join(sorted(mismatches))
            )
        status = str(mapping.get("status") or "")
        if status in {"ready", "creating"}:
            raise WorktreeError(
                "worktree mapping uses an old worktree root; restart with "
                "the old root and perform a clean remove first"
            )
        if status != "removed":
            raise WorktreeError(
                "only a removed worktree mapping can move to a new root"
            )

        old_path = str(mapping.get("path") or "")
        if not os.path.isabs(old_path):
            raise WorktreeError(
                "removed worktree path was tampered with"
            )
        expected_name = f"sat-t-{plan.identity_digest}"
        expected_repo_dir = f"sat-r-{plan.repo.repo_digest[:16]}"
        if (
            os.path.basename(old_path) != expected_name
            or os.path.basename(os.path.dirname(old_path))
            != expected_repo_dir
        ):
            raise WorktreeError(
                "removed worktree path layout was tampered with"
            )
        _validate_no_symlink_components(old_path)
        try:
            os.lstat(old_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorktreeError(
                f"cannot inspect removed worktree path: {exc}"
            ) from exc
        else:
            raise WorktreeError(
                "removed worktree path still exists or was tampered with"
            )
        if self._entry_at(entries, old_path) is not None:
            raise WorktreeError(
                "removed worktree remains registered at its old root"
            )
        expected_branch_ref = f"refs/heads/{plan.branch}"
        if any(
            entry.branch_ref == expected_branch_ref for entry in entries
        ):
            raise WorktreeError(
                "removed worktree branch is active or checked out"
            )
        await self._validate_removed_retained_head(mapping, plan)
        if self._entry_at(entries, plan.path) is not None:
            raise WorktreeError(
                "new worktree root already has registered metadata"
            )
        try:
            new_info = os.lstat(plan.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise WorktreeError(
                f"cannot inspect new worktree path: {exc}"
            ) from exc
        else:
            if stat.S_ISLNK(new_info.st_mode):
                raise WorktreeError(
                    "new worktree path is a symlink or was tampered with"
                )
            raise WorktreeError(
                "new worktree path already exists"
            )
        if not self._state_store.rehome_removed_worktree_mapping(
            identity_digest=plan.identity_digest,
            old_path=old_path,
            new_path=plan.path,
        ):
            raise WorktreeError(
                "cannot atomically rehome removed worktree mapping"
            )
        refreshed = self._state_store.load_worktree_mapping(
            plan.identity_digest
        )
        if refreshed is None:
            raise WorktreeError(
                "cannot reload rehomed worktree mapping"
            )
        return refreshed

    @staticmethod
    def _validate_mapping(
        mapping: dict[str, Any] | None,
        plan: WorktreePlan,
        owner: str,
    ) -> None:
        if mapping is None:
            return
        expected = {
            "identity_digest": plan.identity_digest,
            "repo_digest": plan.repo.repo_digest,
            "common_dir": plan.repo.common_dir,
            "base_workspace": plan.repo.base_workspace,
            "team_id": plan.team_id,
            "channel_id": plan.channel_id,
            "root_thread_ts": plan.root_thread_ts,
            "owner": owner,
            "path": plan.path,
            "branch": plan.branch,
        }
        mismatches = [
            key
            for key, value in expected.items()
            if str(mapping.get(key, "")) != value
        ]
        if mismatches:
            raise WorktreeError(
                "persisted worktree mapping failed identity validation: "
                + ", ".join(sorted(mismatches))
            )

    async def _verify_existing(
        self, plan: WorktreePlan, entry: _GitWorktree
    ) -> None:
        git_file = os.path.join(plan.path, ".git")
        try:
            git_info = os.lstat(git_file)
        except OSError as exc:
            raise WorktreeError(
                f"worktree .git control file is missing: {exc}"
            ) from exc
        if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISREG(git_info.st_mode):
            raise WorktreeError(
                "worktree .git control file is a symlink or has been tampered with"
            )
        top = os.path.realpath(
            await self._run_git(
                "rev-parse", "--show-toplevel", cwd=plan.path
            )
        )
        if top != os.path.realpath(plan.path):
            raise WorktreeError("worktree top-level path failed validation")
        common_raw = await self._run_git(
            "rev-parse", "--git-common-dir", cwd=plan.path
        )
        common = os.path.realpath(
            common_raw
            if os.path.isabs(common_raw)
            else os.path.join(plan.path, common_raw)
        )
        if common != plan.repo.common_dir:
            raise WorktreeError(
                "worktree Git common-dir failed validation"
            )
        branch_ref = await self._run_git(
            "symbolic-ref", "-q", "HEAD", cwd=plan.path
        )
        expected_ref = f"refs/heads/{plan.branch}"
        if branch_ref != expected_ref or entry.branch_ref != expected_ref:
            raise WorktreeError(
                "worktree branch has been changed or tampered with"
            )
        if os.path.realpath(entry.path) != os.path.realpath(plan.path):
            raise WorktreeError("Git worktree path failed validation")

    async def _cleanup_failed_restore(self, plan: WorktreePlan) -> bool:
        """Remove only the clean worktree just created by a failed restore."""
        try:
            entries = await self._worktree_entries(plan.repo)
            entry = self._entry_at(entries, plan.path)
            try:
                path_info = os.lstat(plan.path)
            except FileNotFoundError:
                return entry is None
            if (
                entry is None
                or stat.S_ISLNK(path_info.st_mode)
                or not stat.S_ISDIR(path_info.st_mode)
            ):
                return False
            await self._verify_existing(plan, entry)
            porcelain = await self._run_git(
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
                cwd=plan.path,
            )
            if porcelain:
                return False
            await self._run_git(
                "worktree",
                "remove",
                plan.path,
                cwd=plan.repo.base_workspace,
            )
            try:
                os.lstat(plan.path)
            except FileNotFoundError:
                pass
            else:
                return False
            refreshed = await self._worktree_entries(plan.repo)
            return self._entry_at(refreshed, plan.path) is None
        except (OSError, WorktreeError):
            return False

    def _check_capacity(
        self, entries: list[_GitWorktree], plan: WorktreePlan
    ) -> None:
        count = 0
        for entry in entries:
            branch_ref = entry.branch_ref
            if not branch_ref.startswith(_MANAGED_BRANCH_PREFIX):
                continue
            branch_match = _MANAGED_BRANCH_REF_RE.fullmatch(branch_ref)
            if branch_match is None:
                raise WorktreeError(
                    "managed worktree branch prefix was tampered with"
                )
            digest = branch_match.group(1)
            entry_path = os.path.abspath(entry.path)
            expected_name = f"sat-t-{digest}"
            expected_repo_dir = f"sat-r-{plan.repo.repo_digest[:16]}"
            if (
                os.path.basename(entry_path) != expected_name
                or os.path.basename(os.path.dirname(entry_path))
                != expected_repo_dir
            ):
                raise WorktreeError(
                    "managed worktree path layout was tampered with"
                )
            try:
                info = os.lstat(entry.path)
            except FileNotFoundError:
                raise WorktreeError(
                    "managed worktree path is missing or tampered"
                )
            except OSError as exc:
                raise WorktreeError(
                    f"cannot inspect managed worktree capacity: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorktreeError(
                    "managed worktree capacity contains a tampered path"
                )
            try:
                git_info = os.lstat(os.path.join(entry.path, ".git"))
            except OSError as exc:
                raise WorktreeError(
                    f"managed worktree control file is tampered: {exc}"
                ) from exc
            if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISREG(
                git_info.st_mode
            ):
                raise WorktreeError(
                    "managed worktree control file was tampered with"
                )
            count += 1
        if count >= plan.repo.max_per_repo:
            raise WorktreeError(
                f"worktree capacity reached for repository "
                f"({count}/{plan.repo.max_per_repo})"
            )

    async def _worktree_entries(
        self, repo: RepoSpec
    ) -> list[_GitWorktree]:
        output = await self._run_git(
            "worktree",
            "list",
            "--porcelain",
            cwd=repo.base_workspace,
        )
        entries: list[_GitWorktree] = []
        for block in output.split("\n\n"):
            values: dict[str, str] = {}
            prunable = False
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                if key == "prunable":
                    prunable = True
                elif key:
                    values[key] = value
            if values.get("worktree"):
                entries.append(
                    _GitWorktree(
                        path=values["worktree"],
                        head=values.get("HEAD", ""),
                        branch_ref=values.get("branch", ""),
                        prunable=prunable,
                    )
                )
        return entries

    @staticmethod
    def _entry_at(
        entries: list[_GitWorktree], path: str
    ) -> _GitWorktree | None:
        expected = os.path.realpath(path)
        for entry in entries:
            if os.path.realpath(entry.path) == expected:
                return entry
        return None

    def _persist_ready(
        self,
        plan: WorktreePlan,
        owner: str,
        existing: dict[str, Any] | None,
    ) -> WorktreeRecord:
        now = time.time()
        created_at = (
            float(existing["created_at"]) if existing is not None else now
        )
        base_oid = (
            str(existing["base_oid"])
            if existing is not None and existing.get("base_oid")
            else plan.repo.base_oid
        )
        # Once a worktree is verified ready, removal integrity is no longer
        # pending. A future clean remove captures a fresh exact HEAD.
        retained_head_oid = ""
        if not self._state_store.save_worktree_mapping(
            identity_digest=plan.identity_digest,
            repo_digest=plan.repo.repo_digest,
            common_dir=plan.repo.common_dir,
            base_workspace=plan.repo.base_workspace,
            team_id=plan.team_id,
            channel_id=plan.channel_id,
            root_thread_ts=plan.root_thread_ts,
            owner=owner,
            path=plan.path,
            branch=plan.branch,
            base_ref=plan.repo.base_ref,
            base_oid=base_oid,
            retained_head_oid=retained_head_oid,
            status="ready",
            created_at=created_at,
            last_used_at=now,
        ):
            raise WorktreeError("cannot persist ready worktree mapping")
        return WorktreeRecord(
            identity_digest=plan.identity_digest,
            repo_digest=plan.repo.repo_digest,
            common_dir=plan.repo.common_dir,
            base_workspace=plan.repo.base_workspace,
            team_id=plan.team_id,
            channel_id=plan.channel_id,
            root_thread_ts=plan.root_thread_ts,
            owner=owner,
            path=plan.path,
            branch=plan.branch,
            base_ref=plan.repo.base_ref,
            base_oid=base_oid,
            retained_head_oid=retained_head_oid,
            status="ready",
            last_error="",
            created_at=created_at,
            last_used_at=now,
        )

    def _persist_status(
        self,
        plan: WorktreePlan,
        owner: str,
        *,
        status: str,
        existing: dict[str, Any] | None,
        last_error: str = "",
    ) -> None:
        now = time.time()
        created_at = (
            float(existing["created_at"]) if existing is not None else now
        )
        base_oid = (
            str(existing["base_oid"])
            if existing is not None and existing.get("base_oid")
            else plan.repo.base_oid
        )
        retained_head_oid = (
            str(existing.get("retained_head_oid") or "")
            if existing is not None
            else ""
        )
        if not self._state_store.save_worktree_mapping(
            identity_digest=plan.identity_digest,
            repo_digest=plan.repo.repo_digest,
            common_dir=plan.repo.common_dir,
            base_workspace=plan.repo.base_workspace,
            team_id=plan.team_id,
            channel_id=plan.channel_id,
            root_thread_ts=plan.root_thread_ts,
            owner=owner,
            path=plan.path,
            branch=plan.branch,
            base_ref=plan.repo.base_ref,
            base_oid=base_oid,
            retained_head_oid=retained_head_oid,
            status=status,
            last_error=last_error,
            created_at=created_at,
            last_used_at=now,
        ):
            raise WorktreeError("cannot persist worktree mapping status")

    async def _run_git(
        self,
        *args: str,
        cwd: str,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_git_env(),
            )
        except OSError as exc:
            raise WorktreeError(f"cannot execute Git: {exc}") from exc
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            try:
                process.terminate()
            except (ProcessLookupError, OSError):
                pass
            while process.returncode is None:
                try:
                    await asyncio.shield(process.wait())
                except asyncio.CancelledError:
                    continue
                except (ProcessLookupError, OSError):
                    break
            raise
        if process.returncode not in allowed_returncodes:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise WorktreeError(f"Git command failed{suffix}")
        return stdout.decode("utf-8", "replace").strip()
