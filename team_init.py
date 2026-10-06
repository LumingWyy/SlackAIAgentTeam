"""Generate a multi-person team's configs from one credential-free team.yaml.

    python team_init.py build team.yaml            # -> team/ (roster + per-person files)
    python team_init.py ids team.yaml --person NAME [--env .env] [--write]

team.yaml (see team.example.yaml) lists the people; every person runs a
developer, a reviewer and a QA agent (developer -> reviewer -> QA -> a human
merges). ``team_agents`` adds roles the whole team shares, such as one pm and
one dx, each running on one person's machine. ``build`` writes the shared ``team-roster.yaml`` and,
per person, an ``agents.yaml`` and an ``env.example`` naming the variables to
fill. Slack user / bot ids exist only after each Slack App is installed:
``ids`` reads them with that person's bot tokens (auth.test) and prints them,
or writes them into team.yaml with ``--write``.

No token is ever written to an output file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

from multi_core import canonical_github_repo, default_slack_token_env_names

BASE_DIR = Path(__file__).resolve().parent
AGENTS_TEMPLATE = BASE_DIR / "agents.example.yaml"
ROSTER_FILE = "team-roster.yaml"

# role -> (agent-name suffix, persona taken from agents.example.yaml, runtime)
ROLES: dict[str, tuple[str, str, str]] = {
    "dev": ("dev", "dev", "claude"),
    "rev": ("rev", "reviewer", "claude"),
    "qa": ("qa", "qa", "claude"),
}
# Roles the whole team shares (one agent each, on one person's machine):
# role -> (persona taken from agents.example.yaml, runtime). The agent is
# named after the role.
TEAM_ROLES: dict[str, tuple[str, str]] = {
    "pm": ("pm", "claude"),
    "dx": ("dx", "claude"),
}
_KEY_RE = re.compile(r"[a-z][a-z0-9]{0,11}")  # leaves room for "_dev" in 16 chars
_CHANNEL_RE = re.compile(r"[CG][A-Z0-9]{6,}")
_HUMAN_RE = re.compile(r"[UW][A-Z0-9]{6,}")
_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


class TeamError(ValueError):
    """team.yaml is incomplete or inconsistent."""


def agent_name(key: str, role: str) -> str:
    return f"{key}_{ROLES[role][0]}"


def _person_label(person: dict[str, Any]) -> str:
    return str(person.get("name") or person["key"])


def load_team(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        team = yaml.safe_load(f) or {}
    validate_team(team)
    return team


def validate_team(team: dict[str, Any]) -> None:
    if not isinstance(team, dict):
        raise TeamError("team.yaml must be a mapping")
    for field in ("project", "repo", "channel", "people"):
        if not team.get(field):
            raise TeamError(f"team.yaml needs {field}")
    if not _PROJECT_RE.fullmatch(str(team["project"])):
        raise TeamError("project must be lowercase letters, digits or -")
    try:
        canonical_github_repo(str(team["repo"]))
    except ValueError as exc:
        raise TeamError(str(exc)) from exc
    if not _CHANNEL_RE.fullmatch(str(team["channel"])):
        raise TeamError("channel must be a Slack channel id like C0123456789")
    people = team["people"]
    if not isinstance(people, list):
        raise TeamError("people must be a list")
    keys: set[str] = set()
    nodes: set[str] = set()
    for person in people:
        if not isinstance(person, dict):
            raise TeamError("each person must be a mapping")
        key = str(person.get("key") or "")
        if not _KEY_RE.fullmatch(key):
            raise TeamError(
                f"person key {key!r}: 1-12 lowercase letters/digits, starting with a letter"
            )
        if key in keys:
            raise TeamError(f"duplicate person key {key}")
        keys.add(key)
        if not _HUMAN_RE.fullmatch(str(person.get("slack_user_id") or "")):
            raise TeamError(f"{key}: slack_user_id must be the person's Slack user id")
        node = str(person.get("node_id") or "")
        if not node:
            raise TeamError(f"{key}: node_id is required (their machine's name)")
        if node in nodes:
            raise TeamError(f"duplicate node_id {node}")
        nodes.add(node)
        if not str(person.get("workspace") or "").strip():
            raise TeamError(f"{key}: workspace (their local clone of the repo) is required")
    admins = team.get("admins")
    if admins is not None and (
        not isinstance(admins, list)
        or not all(_HUMAN_RE.fullmatch(str(item)) for item in admins)
    ):
        raise TeamError("admins must be a list of Slack user ids")
    daily = team.get("daily_total_tokens")
    if daily:
        reservation = team.get("reservation_tokens")
        if reservation and int(reservation) > int(daily):
            raise TeamError("reservation_tokens cannot exceed daily_total_tokens")
    shared = team.get("team_agents") or []
    if not isinstance(shared, list):
        raise TeamError("team_agents must be a list")
    roles: set[str] = set()
    for entry in shared:
        role = str((entry or {}).get("role") or "")
        if role not in TEAM_ROLES:
            raise TeamError(
                f"team_agents role {role!r}: one of {', '.join(TEAM_ROLES)}"
            )
        if role in roles:
            raise TeamError(f"team_agents has {role} twice; the team shares one")
        roles.add(role)
        if str(entry.get("host") or "") not in keys:
            raise TeamError(f"team_agents {role}: host must be one of the people's keys")


def team_agents(team: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(entry) for entry in team.get("team_agents") or []]


def hosted(team: dict[str, Any], person: dict[str, Any]) -> list[dict[str, Any]]:
    """Every agent that runs on this person's machine, in a stable order."""
    agents = [
        {
            "name": agent_name(person["key"], role),
            "persona": persona,
            "runtime": str((person.get("runtime") or {}).get(role) or runtime),
            "ids": (person.get("ids") or {}).get(role) or {},
            "card": _card(person, role),
        }
        for role, (_suffix, persona, runtime) in ROLES.items()
    ]
    for entry in team_agents(team):
        if entry["host"] != person["key"]:
            continue
        persona, runtime = TEAM_ROLES[entry["role"]]
        agents.append(
            {
                "name": entry["role"],
                "persona": persona,
                "runtime": str(entry.get("runtime") or runtime),
                "ids": entry.get("ids") or {},
                "card": _TEAM_CARDS[entry["role"]],
            }
        )
    return agents


def _ids(agent: dict[str, Any]) -> tuple[str, str]:
    entry = agent.get("ids") or {}
    return str(entry.get("slack_user_id") or ""), str(entry.get("slack_bot_id") or "")


def missing_ids(team: dict[str, Any]) -> list[str]:
    """Agent names whose Slack user / bot ids are not filled in yet."""
    return [
        agent["name"]
        for person in team["people"]
        for agent in hosted(team, person)
        if not all(_ids(agent))
    ]


def _card(person: dict[str, Any], role: str) -> str:
    """Who each agent is and who it hands to: the team list routes by these."""
    label = _person_label(person)
    if role == "dev":
        return (
            f"{label}'s developer. Implements issues assigned to {label}; asks "
            f"{agent_name(person['key'], 'rev')} for review unless a human names "
            "another reviewer; fixes what review or QA sends back."
        )
    if role == "rev":
        return (
            f"{label}'s reviewer. Reviews any PR it is asked to and writes the "
            "findings on the PR; on PASS hands the PR to the QA of the PR's owner."
        )
    return (
        f"{label}'s QA. Checks {label}'s PRs after review passes, on the PR's exact "
        "commit; posts QA: PASS/FAIL with evidence on the PR and in Slack; on PASS "
        "asks a human to merge."
    )


_TEAM_CARDS = {
    "pm": (
        "Team PM. Turns a vague request into issues with done criteria and one "
        "assignee each, then hands each issue to its assignee's developer; writes "
        "no code; signs off product questions after QA passes."
    ),
    "dx": (
        "Team DX. Watches threads for loops, duplicate delegation and stuck agents; "
        "narrows each to one next step and uses !status / !reset; writes no code "
        "and stays silent unless called."
    ),
}


def build_roster(team: dict[str, Any]) -> dict[str, Any]:
    people = team["people"]
    members = [str(person["slack_user_id"]) for person in people]
    # An explicit empty list means no team-wide admins (owners still control
    # their own agents); only a missing key falls back to every member.
    admins = [str(item) for item in (team["admins"] if "admins" in team else members)]
    names = [agent["name"] for person in people for agent in hosted(team, person)]
    roster: dict[str, Any] = {
        "version": 1,
        "access": {"admins": admins},
        "projects": [
            {
                "id": str(team["project"]),
                "channels": [str(team["channel"])],
                "members": members,
                "admins": admins,
                "agents": names,
            }
        ],
        "agents": [],
    }
    daily = team.get("daily_total_tokens")
    if daily:
        # multi_app reserves this much per turn before it knows the real usage;
        # a daily limit is not accepted without it.
        reservation = int(team.get("reservation_tokens") or min(20000, int(daily)))
        roster["quotas"] = {
            "daily_total_tokens": {member: int(daily) for member in members},
            "reservation_tokens": {member: reservation for member in members},
        }
    for person in people:
        for agent in hosted(team, person):
            user_id, bot_id = _ids(agent)
            roster["agents"].append(
                {
                    "name": agent["name"],
                    "slack_user_id": user_id,
                    "slack_bot_id": bot_id,
                    "owner": str(person["slack_user_id"]),
                    "node_id": str(person["node_id"]),
                    "card": agent["card"],
                }
            )
    return roster


def _template_personas() -> dict[str, str]:
    raw = yaml.safe_load(AGENTS_TEMPLATE.read_text(encoding="utf-8")) or {}
    return {
        str(entry.get("name")): str(entry.get("persona") or "")
        for entry in raw.get("agents") or []
        if isinstance(entry, dict)
    }


def build_person_config(
    team: dict[str, Any], person: dict[str, Any], personas: dict[str, str]
) -> dict[str, Any]:
    workspace = str(person["workspace"]).strip()
    defaults: dict[str, Any] = {
        "workspace": workspace,
        "workspace_mode": "thread_worktree",
        "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"],
        "max_turns": 25,
        "context_rollover_tokens": 60000,
    }
    if person.get("reply_language"):
        defaults["reply_language"] = str(person["reply_language"])
    if team.get("skills"):
        # Claude skills every agent may use (each person installs them on their machine)
        defaults["skills"] = [str(name) for name in team["skills"]]
    agents = [
        {
            "name": agent["name"],
            "runtime": agent["runtime"],
            "persona": personas.get(agent["persona"], ""),
        }
        for agent in hosted(team, person)
    ]
    return {
        "roster": ROSTER_FILE,
        "node": {"id": str(person["node_id"]), "max_concurrency": 2, "max_queue": 10},
        "security": {"control_auth": "required"},
        "budget": {"max_agent_rounds": int(team.get("max_agent_rounds") or 10)},
        "github": {"repo": canonical_github_repo(str(team["repo"]))},
        # Each Slack thread gets its own git worktree, so the developer and the
        # reviewer on this machine never edit the same checkout.
        "worktrees": {
            "root": str(person.get("worktree_root") or "~/.slack-agent-team/worktrees"),
            "base_ref": str(team.get("base_ref") or "main"),
            "max_per_repo": int(team.get("max_worktrees") or 16),
        },
        "defaults": defaults,
        "agents": agents,
    }


def env_example(team: dict[str, Any], person: dict[str, Any]) -> str:
    lines = [
        f"# {_person_label(person)}: merge into SlackAgentTeam/.env on your machine.",
        "# Never commit or share the filled-in values.",
        "",
    ]
    for agent in hosted(team, person):
        bot, app = default_slack_token_env_names(agent["name"])
        lines += [f"# {agent['name']}", f"{bot}=", f"{app}="]
    lines += [
        "",
        "# Console / admin API bearer: 32+ random printable characters, e.g.",
        "#   python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"",
        f"SLACK_AGENT_CONTROL_TOKEN_{person['slack_user_id']}=",
        "",
    ]
    return "\n".join(lines)


def _dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


def build(team: dict[str, Any], out: Path) -> list[Path]:
    """Write every generated file under ``out``; returns the paths written."""
    personas = _template_personas()
    written: list[Path] = []
    out.mkdir(parents=True, exist_ok=True)
    roster_text = _dump(build_roster(team))
    roster_path = out / ROSTER_FILE
    roster_path.write_text(roster_text, encoding="utf-8")
    written.append(roster_path)
    for person in team["people"]:
        folder = out / person["key"]
        folder.mkdir(exist_ok=True)
        files = {
            "agents.yaml": _dump(build_person_config(team, person, personas)),
            ROSTER_FILE: roster_text,
            "env.example": env_example(team, person),
        }
        for name, text in files.items():
            path = folder / name
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written


SlackCall = Callable[[str, str], dict[str, Any]]


def _slack_auth_test(token: str, method: str = "auth.test") -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urllib.parse.urlencode({}).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_ids(
    team: dict[str, Any],
    key: str,
    env: dict[str, str],
    call: SlackCall | None = None,
) -> dict[str, dict[str, str]]:
    """``{agent name: {slack_user_id, slack_bot_id}}`` for every bot on this
    person's machine (their own and any team agent they host)."""
    call = call or (lambda token, method: _slack_auth_test(token, method))
    person = next((p for p in team["people"] if p["key"] == key), None)
    if person is None:
        raise TeamError(f"no person with key {key}")
    found: dict[str, dict[str, str]] = {}
    for agent in hosted(team, person):
        name = agent["name"]
        bot_env, _app_env = default_slack_token_env_names(name)
        token = env.get(bot_env) or os.environ.get(bot_env) or ""
        if not token:
            raise TeamError(f"{bot_env} is not set; save {name}'s Bot token first")
        result = call(token, "auth.test")
        if not result.get("ok"):
            raise TeamError(f"{name}: Slack auth.test failed: {result.get('error')}")
        found[name] = {
            "slack_user_id": str(result.get("user_id") or ""),
            "slack_bot_id": str(result.get("bot_id") or ""),
        }
    return found


def store_ids(team: dict[str, Any], key: str, found: dict[str, dict[str, str]]) -> None:
    """Put ids read by ``read_ids`` where team.yaml keeps them."""
    person = next(p for p in team["people"] if p["key"] == key)
    for role in ROLES:
        name = agent_name(key, role)
        if name in found:
            person.setdefault("ids", {})[role] = found[name]
    for entry in team.get("team_agents") or []:
        if entry.get("host") == key and entry.get("role") in found:
            entry["ids"] = found[entry["role"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build", help="write roster and per-person configs")
    p_build.add_argument("team")
    p_build.add_argument("--out", default="team")
    p_ids = sub.add_parser("ids", help="read one person's Slack bot ids")
    p_ids.add_argument("team")
    p_ids.add_argument("--person", required=True)
    p_ids.add_argument("--env", default=".env")
    p_ids.add_argument("--write", action="store_true", help="store them in team.yaml")
    args = parser.parse_args(argv)
    try:
        team = load_team(args.team)
        if args.command == "build":
            for path in build(team, Path(args.out)):
                print(f"wrote {path}")
            missing = missing_ids(team)
            if missing:
                print(
                    "\nSlack ids still missing for: " + ", ".join(missing)
                    + "\nOnce each person's Slack Apps are installed and tokens saved, run"
                    + "\n  python team_init.py ids team.yaml --person <key> --write"
                    + "\nthen build again. multi_app will not start until the roster is complete."
                )
            return 0
        ids = read_ids(team, args.person, _read_env(Path(args.env)))
        if args.write:
            store_ids(team, args.person, ids)
            Path(args.team).write_text(_dump(team), encoding="utf-8")
            print(f"stored ids for {args.person} in {args.team} (comments are not kept)")
        print(_dump({"ids": ids}), end="")
        return 0
    except (TeamError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
