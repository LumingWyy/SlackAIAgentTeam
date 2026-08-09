"""Control-plane bearer authentication regression tests."""

from __future__ import annotations

import secrets

import pytest

from control_auth import ControlAuthenticator


def _token(label: str) -> str:
    return f"{label}-" + ("x" * 40)


def test_bearer_token_resolves_principal_without_identity_header():
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE"},
        admin_user_ids={"U01ADMIN"},
        required=True,
        environ={
            "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": _token("alice"),
            "SLACK_AGENT_CONTROL_TOKEN_U01ADMIN": _token("admin"),
        },
    )

    owner = auth.authenticate(f"Bearer {_token('alice')}")
    admin = auth.authenticate(f"Bearer {_token('admin')}")

    assert owner is not None
    assert owner.user_id == "U01ALICE"
    assert owner.is_admin is False
    assert admin is not None
    assert admin.user_id == "U01ADMIN"
    assert admin.is_admin is True
    assert auth.authenticate("Bearer wrong") is None


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        (
            {
                "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": "too-short",
                "SLACK_AGENT_CONTROL_TOKEN_U01ADMIN": _token("admin"),
            },
            "at least",
        ),
        (
            {
                "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": _token("same"),
                "SLACK_AGENT_CONTROL_TOKEN_U01ADMIN": _token("same"),
            },
            "duplicate",
        ),
    ],
)
def test_short_and_duplicate_control_tokens_fail_closed(environ, message):
    with pytest.raises(RuntimeError, match=message):
        ControlAuthenticator.from_environment(
            owner_user_ids={"U01ALICE"},
            admin_user_ids={"U01ADMIN"},
            required=True,
            environ=environ,
        )


def test_authentication_compares_every_candidate(monkeypatch):
    calls: list[tuple[str, str]] = []
    real_compare = secrets.compare_digest

    def tracked_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr("control_auth.secrets.compare_digest", tracked_compare)
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE", "U02BOB"},
        admin_user_ids={"U01ADMIN"},
        required=True,
        environ={
            "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": _token("alice"),
            "SLACK_AGENT_CONTROL_TOKEN_U02BOB": _token("bob"),
            "SLACK_AGENT_CONTROL_TOKEN_U01ADMIN": _token("admin"),
        },
    )

    assert auth.authenticate(f"Bearer {_token('alice')}").user_id == "U01ALICE"
    assert len(calls) == 3


def test_legacy_authenticator_is_an_explicit_admin_compatibility_principal():
    auth = ControlAuthenticator.from_environment(
        owner_user_ids=set(),
        admin_user_ids=set(),
        required=False,
        environ={},
    )

    principal = auth.authenticate("")

    assert principal is not None
    assert principal.is_admin is True
    assert principal.legacy is True


def test_local_owner_token_is_required_but_remote_admin_token_is_optional():
    owner_token = _token("alice")
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE"},
        admin_user_ids={"U01ADMIN"},
        required=True,
        environ={
            "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": owner_token,
        },
    )

    assert auth.authenticate(f"Bearer {owner_token}").user_id == "U01ALICE"
    with pytest.raises(RuntimeError, match="U01ALICE"):
        ControlAuthenticator.from_environment(
            owner_user_ids={"U01ALICE"},
            admin_user_ids={"U01ADMIN"},
            required=True,
            environ={},
        )


def test_scrub_removes_every_control_prefix_secret_after_resolver_is_built():
    owner_token = _token("alice")
    environ = {
        "SLACK_AGENT_CONTROL_TOKEN_U01ALICE": owner_token,
        "SLACK_AGENT_CONTROL_TOKEN_U02REMOTE": _token("remote"),
        "SLACK_AGENT_CONTROL_TOKEN_STALE": "stale-even-if-invalid",
        "SLACK_AGENT_CONTROL_TOKEN_": "invalid-empty-suffix",
        "SLACK_AGENT_CONTROL_TOKENS_U03KEEP": "similar-but-not-prefix",
        "OTHER_SLACK_AGENT_CONTROL_TOKEN_U04KEEP": "unrelated",
        "PATH": "/usr/bin",
    }
    auth = ControlAuthenticator.from_environment(
        owner_user_ids={"U01ALICE"},
        admin_user_ids=set(),
        required=True,
        environ=environ,
    )

    removed = auth.scrub_environment(environ)

    assert removed == sorted(
        {
            "SLACK_AGENT_CONTROL_TOKEN_U01ALICE",
            "SLACK_AGENT_CONTROL_TOKEN_U02REMOTE",
            "SLACK_AGENT_CONTROL_TOKEN_STALE",
            "SLACK_AGENT_CONTROL_TOKEN_",
        }
    )
    assert all(
        not key.startswith("SLACK_AGENT_CONTROL_TOKEN_")
        for key in environ
    )
    assert environ["SLACK_AGENT_CONTROL_TOKENS_U03KEEP"] == (
        "similar-but-not-prefix"
    )
    assert environ["OTHER_SLACK_AGENT_CONTROL_TOKEN_U04KEEP"] == "unrelated"
    # The resolver copied the valid credential into private in-memory state
    # before scrubbing the child-process environment.
    principal = auth.authenticate(f"Bearer {owner_token}")
    assert principal is not None
    assert principal.user_id == "U01ALICE"
