"""Environment-only bearer authentication for the local control plane.

Bearer values are deliberately kept out of YAML, SQLite, responses, and reprs.
The token itself is the sole credential used to resolve a Slack principal:
caller-provided identity headers are never trusted.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass

CONTROL_TOKEN_ENV_PREFIX = "SLACK_AGENT_CONTROL_TOKEN_"
MIN_CONTROL_TOKEN_LENGTH = 32
SLACK_HUMAN_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")


def is_slack_human_id(value: str) -> bool:
    """Return whether *value* is an explicit Slack human/user identity."""
    return bool(SLACK_HUMAN_ID_RE.fullmatch(str(value or "")))


def require_slack_human_id(value: str, *, field_name: str) -> str:
    """Normalize one Slack human id or raise a safe configuration error."""
    normalized = str(value or "").strip()
    if not is_slack_human_id(normalized):
        raise RuntimeError(
            f"{field_name} must be an explicit Slack human ID "
            "(starting with U or W)"
        )
    return normalized


def control_token_env_name(user_id: str) -> str:
    """Deterministic env name for one control-plane principal."""
    normalized = require_slack_human_id(
        user_id, field_name="control principal"
    )
    return f"{CONTROL_TOKEN_ENV_PREFIX}{normalized}"


@dataclass(frozen=True)
class ControlPrincipal:
    """Authenticated control-plane identity."""

    user_id: str
    is_admin: bool = False
    legacy: bool = False

    def can_manage(self, owner_user_id: str) -> bool:
        return self.is_admin or (
            bool(owner_user_id) and self.user_id == owner_user_id
        )


class ControlAuthenticator:
    """Resolve bearer values to principals using constant-time comparisons."""

    __slots__ = ("required", "_credentials", "_env_names")

    def __init__(
        self,
        *,
        required: bool,
        credentials: tuple[tuple[str, ControlPrincipal], ...] = (),
        env_names: tuple[str, ...] = (),
    ) -> None:
        self.required = bool(required)
        self._credentials = credentials
        self._env_names = env_names

    def __repr__(self) -> str:
        return (
            "ControlAuthenticator("
            f"required={self.required!r}, principals={len(self._credentials)})"
        )

    @classmethod
    def from_environment(
        cls,
        *,
        owner_user_ids: set[str] | frozenset[str],
        admin_user_ids: set[str] | frozenset[str],
        required: bool,
        environ: Mapping[str, str],
    ) -> "ControlAuthenticator":
        owners = {
            require_slack_human_id(item, field_name="agent owner")
            for item in owner_user_ids
            if str(item or "").strip()
        }
        admins = {
            require_slack_human_id(item, field_name="admin")
            for item in admin_user_ids
            if str(item or "").strip()
        }
        if not required:
            return cls(required=False)

        principal_ids = sorted(owners | admins)
        if not principal_ids:
            raise RuntimeError(
                "control authentication requires at least one owner or admin"
            )

        credentials: list[tuple[str, ControlPrincipal]] = []
        env_names: list[str] = []
        seen_tokens: set[str] = set()
        for user_id in principal_ids:
            env_name = control_token_env_name(user_id)
            raw = environ.get(env_name)
            if raw is None or raw == "":
                # Every local owner must be able to control their own node.
                # A top-level admin is optional per node: installing that
                # admin's node-specific bearer grants access there without
                # forcing one shared team credential onto every person's host.
                if user_id in owners:
                    raise RuntimeError(
                        f"missing control bearer env var: {env_name}"
                    )
                continue
            env_names.append(env_name)
            token = str(raw)
            if token != token.strip() or any(
                ord(char) < 0x21 or ord(char) > 0x7E for char in token
            ):
                raise RuntimeError(
                    f"{env_name} must contain printable non-whitespace ASCII"
                )
            if len(token) < MIN_CONTROL_TOKEN_LENGTH:
                raise RuntimeError(
                    f"{env_name} must be at least "
                    f"{MIN_CONTROL_TOKEN_LENGTH} characters"
                )
            if token in seen_tokens:
                raise RuntimeError(
                    "duplicate control bearer tokens are not allowed"
                )
            seen_tokens.add(token)
            credentials.append(
                (
                    token,
                    ControlPrincipal(
                        user_id=user_id,
                        is_admin=user_id in admins,
                    ),
                )
            )
        if not credentials:
            raise RuntimeError(
                "control authentication has no configured bearer token"
            )
        return cls(
            required=True,
            credentials=tuple(credentials),
            env_names=tuple(env_names),
        )

    @property
    def env_names(self) -> tuple[str, ...]:
        return self._env_names

    def authenticate(self, authorization: str | None) -> ControlPrincipal | None:
        """Resolve an Authorization header without trusting any identity hint."""
        if not self.required:
            return ControlPrincipal(user_id="", is_admin=True, legacy=True)

        header = str(authorization or "")
        scheme, separator, candidate = header.partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not candidate
            or candidate != candidate.strip()
            or " " in candidate
        ):
            return None

        matched: ControlPrincipal | None = None
        # Intentionally visit every configured credential even after a match.
        for token, principal in self._credentials:
            if secrets.compare_digest(candidate, token):
                matched = principal
        return matched

    def scrub_environment(
        self, environ: MutableMapping[str, str]
    ) -> list[str]:
        """Remove every control bearer candidate before children inherit env.

        The resolver has already copied validated local credentials into its
        private in-memory tuple. Remote, stale, and even malformed-suffix
        variables are still secrets and must not leak merely because this node
        did not load them as a principal.
        """
        removed: list[str] = []
        for env_name in sorted(
            name
            for name in list(environ)
            if name.startswith(CONTROL_TOKEN_ENV_PREFIX)
        ):
            environ.pop(env_name, None)
            removed.append(env_name)
        return removed
