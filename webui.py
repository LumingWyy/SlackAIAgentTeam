"""Agent config Web UI (local-only).

Run `python webui.py` and open http://127.0.0.1:8765.
- List agents from agents.yaml and token configuration status
- Add agents / edit personas. Writes are atomic (tmp + rename) and serialized by a
  lock; agents.yaml.bak keeps one backup generation. safe_dump re-serializes, so
  YAML comments are lost.
- Saving config triggers a best-effort hot reload of the running multi_app
  (admin POST /reload); adding/removing agents still needs a restart.
- Hot-swaps done from the monitor (model / runtime / effort / reply language) are
  persisted back to agents.yaml so they survive a restart.
- Slack App setup wizard:
  auto-generate manifest -> copy -> create on api.slack.com -> paste tokens -> verify -> save to .env
- Auth tab: Claude OAuth, Codex device login, GitHub device login + GH_TOKEN
  write/import (host keychain does not enter the Docker container)
- Tokens are handled only on 127.0.0.1; external traffic is limited to Slack API verification calls

Security: do not expose on LAN (bind is fixed to 127.0.0.1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import shutil
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

import yaml
from aiohttp import ClientSession, web

from control_auth import (
    ControlAuthenticator,
    ControlPrincipal,
    require_slack_human_id,
)
from multi_core import (
    canonical_github_repo,
    default_slack_token_env_names,
    github_repo_from_remote,
)

logger = logging.getLogger("webui")

# Empty string is a deliberate "engine default" for these keys — persist it so
# restart does not re-merge team defaults over the user's choice.
_EXPLICIT_EMPTY_KEYS = frozenset(
    {"claude_model", "codex_model", "openai_model", "effort"}
)

BASE_DIR = Path(__file__).resolve().parent
AGENTS_YAML = Path(
    os.environ.get("AGENTS_CONFIG") or BASE_DIR / "agents.yaml"
).expanduser()
ENV_FILE = Path(
    os.environ.get("AGENT_ENV_FILE") or BASE_DIR / ".env"
).expanduser()
MANIFEST_TEMPLATE = BASE_DIR / "slack-app-manifest-agent.yaml"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")

# Running multi_app admin API (source for monitor / hot-swap model)
ADMIN_BASE = os.environ.get("ADMIN_BASE", "http://127.0.0.1:8766")
CODEX_CONFIG = Path(os.path.expanduser("~/.codex/config.toml"))
CLAUDE_SETTINGS = [
    Path(os.path.expanduser("~/.claude/settings.json")),
    Path(os.path.expanduser("~/.claude.json")),
]

# Claude Code CLI model aliases (--model; always tracks the latest for each family)
CLAUDE_ALIASES = ["default", "fable", "opus", "sonnet", "haiku"]

# reasoning effort choices (per-runtime subsets; "" = engine default)
CLAUDE_EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["", "minimal", "low", "medium", "high", "xhigh"]
OPENAI_EFFORTS = ["", "none", "low", "medium", "high", "xhigh", "max"]
# Built-in catalog for official OpenAI + common CLI Proxy (Grok / Antigravity) models.
# Free-form model ids are always allowed; this list only seeds the dropdown.
OPENAI_MODELS = [
    "",
    "gpt-5.6-sol",
    "grok-4.5",
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "gemini-3-pro-preview",
]
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"

# Model list cache ((claude_list, codex_list, source)), reused within TTL
_models_cache: dict[str, Any] = {}
_CONTROL_AUTHORIZATION: ContextVar[str] = ContextVar(
    "webui_control_authorization", default=""
)
_CONTROL_PRINCIPAL_KEY = web.RequestKey(
    "control_principal", ControlPrincipal
)


def _roster_path(raw: dict[str, Any]) -> Path | None:
    configured: Any = os.environ.get("ROSTER_CONFIG") or raw.get("roster")
    if isinstance(configured, dict):
        configured = configured.get("path")
    value = str(configured or "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = AGENTS_YAML.parent / path
    return path.resolve()


def _read_roster(raw: dict[str, Any]) -> dict[str, Any]:
    path = _roster_path(raw)
    if path is None:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise RuntimeError(f"cannot read roster {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("roster root must be a mapping")
    return loaded


def _roster_entry(
    name: str, raw: dict[str, Any]
) -> dict[str, Any] | None:
    for entry in _read_roster(raw).get("agents") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "") == name:
            return entry
    return None


def _entry_owner(
    entry: dict[str, Any], raw: dict[str, Any] | None = None
) -> str:
    values = {
        str(entry.get(key) or "").strip()
        for key in ("owner", "owner_user_id", "owner_slack_user_id")
        if str(entry.get(key) or "").strip()
    }
    if len(values) > 1:
        raise RuntimeError(
            f"agent {entry.get('name') or '(unknown)'} owner aliases conflict"
        )
    owner = next(iter(values), "")
    if not owner and raw is not None:
        roster_entry = _roster_entry(
            str(entry.get("name") or ""), raw
        )
        if roster_entry is not None:
            owner = str(roster_entry.get("owner") or "").strip()
    if owner:
        require_slack_human_id(
            owner,
            field_name=f"agent {entry.get('name') or '(unknown)'} owner",
        )
    return owner


def _local_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    node_cfg = raw.get("node") or {}
    if not isinstance(node_cfg, dict):
        raise RuntimeError("node must be a mapping")
    node_id = str(
        node_cfg.get("id") or os.environ.get("AGENT_NODE_ID") or ""
    ).strip()
    separate_roster = _roster_path(raw) is not None
    result: list[dict[str, Any]] = []
    for entry in raw.get("agents") or []:
        if not isinstance(entry, dict):
            raise RuntimeError("each agent entry must be a mapping")
        explicit = entry.get("local") if "local" in entry else None
        if explicit is not None and not isinstance(explicit, bool):
            raise RuntimeError(
                f"agent {entry.get('name')}: local must be a boolean"
            )
        local = True if separate_roster else (
            explicit
            if explicit is not None
            else (
                str(entry.get("node_id") or "").strip() == node_id
                if node_id
                else True
            )
        )
        if local:
            result.append(entry)
    return result


def _build_webui_authenticator(
    raw: dict[str, Any],
    environ: dict[str, str],
) -> ControlAuthenticator:
    roster_raw = _read_roster(raw)
    access_source = roster_raw if roster_raw else raw
    access = access_source.get("access") or {}
    if not isinstance(access, dict):
        raise RuntimeError("access must be a mapping")
    admin_values = access.get(
        "admins", access_source.get("admins")
    ) or []
    if not isinstance(admin_values, list):
        raise RuntimeError("access.admins must be a list")
    admins = {
        require_slack_human_id(item, field_name="access admin")
        for item in admin_values
    }

    security = raw.get("security") or {}
    if not isinstance(security, dict):
        raise RuntimeError("security must be a mapping")
    mode = str(security.get("control_auth") or "auto").strip().lower()
    if mode not in {"auto", "required", "legacy-localhost"}:
        raise RuntimeError(
            "security.control_auth must be auto, required, or "
            "legacy-localhost"
        )

    all_entries = raw.get("agents") or []
    owners_by_name = {
        str(entry.get("name") or ""): _entry_owner(entry, raw)
        for entry in all_entries
        if isinstance(entry, dict)
    }
    owner_mode = any(owners_by_name.values())
    node_cfg = raw.get("node") or {}
    node_id = str(
        (node_cfg.get("id") if isinstance(node_cfg, dict) else "")
        or os.environ.get("AGENT_NODE_ID")
        or ""
    ).strip()
    local_entries = _local_entries(raw)
    distributed = bool(node_id) or len(local_entries) != len(all_entries)
    if owner_mode or distributed:
        missing = sorted(
            name for name, owner in owners_by_name.items() if not owner
        )
        if missing:
            raise RuntimeError(
                "owner is required for every agent in owner/distributed mode: "
                + ", ".join(missing)
            )
    if distributed and mode == "legacy-localhost":
        raise RuntimeError(
            "distributed mode cannot use legacy-localhost control auth"
        )
    required = mode == "required" or (
        mode == "auto" and (owner_mode or distributed)
    )
    local_owners = {
        _entry_owner(entry, raw)
        for entry in local_entries
        if _entry_owner(entry, raw)
    }
    return ControlAuthenticator.from_environment(
        owner_user_ids=local_owners,
        admin_user_ids=admins,
        required=required,
        environ=environ,
    )


def _principal(request: web.Request | None) -> ControlPrincipal:
    if request is None:
        return ControlPrincipal(user_id="", is_admin=True, legacy=True)
    return request[_CONTROL_PRINCIPAL_KEY]


def _visible_entries(
    request: web.Request | None, raw: dict[str, Any]
) -> list[dict[str, Any]]:
    actor = _principal(request)
    return [
        entry
        for entry in raw.get("agents") or []
        if isinstance(entry, dict)
        and (actor.is_admin or actor.user_id == _entry_owner(entry, raw))
    ]


def _require_agent_access(
    request: web.Request,
    name: str,
    raw: dict[str, Any],
    *,
    allow_admin_create: bool = False,
) -> dict[str, Any] | None:
    entry = next(
        (
            item
            for item in raw.get("agents") or []
            if isinstance(item, dict) and item.get("name") == name
        ),
        None,
    )
    actor = _principal(request)
    if entry is None:
        if actor.is_admin and allow_admin_create:
            return None
        raise web.HTTPForbidden(text="forbidden")
    if not actor.is_admin and actor.user_id != _entry_owner(entry, raw):
        raise web.HTTPForbidden(text="forbidden")
    return entry


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


# Serializes every read-modify-write of agents.yaml / .env within this process
# (concurrent POSTs would otherwise lose updates between read_yaml and write_yaml)
_CONFIG_LOCK = asyncio.Lock()


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file + os.replace so a crash never leaves a torn file.

    mode: chmod applied to the new file; None = keep the existing file's mode
    (0o644 for a new file).
    """
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_yaml() -> dict[str, Any]:
    with open(AGENTS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_yaml(data: dict[str, Any]) -> None:
    """Write agents.yaml atomically; backup to agents.yaml.bak first (single generation)."""
    if AGENTS_YAML.exists():
        shutil.copy2(AGENTS_YAML, AGENTS_YAML.parent / "agents.yaml.bak")
    text = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    _atomic_write(AGENTS_YAML, text)


def read_env_file() -> dict[str, str]:
    """Parse .env (KEY=VALUE lines; no expansion beyond quotes)."""
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result


def upsert_env(updates: dict[str, str]) -> None:
    """Update/append .env keys; keep existing lines and comments; backup to .env.bak.

    Atomic write with 0o600 (tokens must never be world-readable, even transiently).
    """
    lines: list[str] = []
    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, ENV_FILE.parent / ".env.bak")
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        replaced = False
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                replaced = True
        if not replaced:
            out.append(line)
    if remaining:
        out.append("")
        out.extend(f"{k}={v}" for k, v in remaining.items())
    _atomic_write(ENV_FILE, "\n".join(out) + "\n", mode=0o600)


def token_env_names(
    entry: dict[str, Any], defaults: dict[str, Any] | None = None
) -> tuple[str, str]:
    defaults = defaults or {}
    default_bot, default_app = default_slack_token_env_names(entry["name"])
    bot = (
        entry.get("bot_token_env")
        or defaults.get("bot_token_env")
        or default_bot
    )
    app = (
        entry.get("app_token_env")
        or defaults.get("app_token_env")
        or default_app
    )
    return bot, app


def build_manifest(name: str, description: str) -> str:
    with open(MANIFEST_TEMPLATE, encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    manifest["display_information"]["name"] = f"Agent ({name})"
    manifest["features"]["bot_user"]["display_name"] = name
    if description:
        agent_view = manifest.get("features", {}).get("agent_view")
        if agent_view is not None:
            agent_view["agent_description"] = description
    return yaml.safe_dump(
        manifest, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


# ---------------------------------------------------------------------------
# Slack token verification
# ---------------------------------------------------------------------------


async def validate_tokens(bot_token: str, app_token: str) -> dict[str, Any]:
    """auth.test verifies xoxb; apps.connections.open verifies xapp. Returns a detail dict."""
    result: dict[str, Any] = {"bot_ok": False, "app_ok": False}
    async with ClientSession() as session:
        if bot_token:
            async with session.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {bot_token}"},
            ) as resp:
                data = await resp.json()
            result["bot_ok"] = bool(data.get("ok"))
            result["bot_detail"] = (
                f"{data.get('team', '')} / {data.get('user', '')}"
                if data.get("ok")
                else data.get("error", "unknown")
            )
        if app_token:
            async with session.post(
                "https://slack.com/api/apps.connections.open",
                headers={"Authorization": f"Bearer {app_token}"},
            ) as resp:
                data = await resp.json()
            result["app_ok"] = bool(data.get("ok"))
            result["app_detail"] = (
                "connections:write OK" if data.get("ok") else data.get("error", "unknown")
            )
    return result


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def _resolve_openai_base_url(entry: dict, defaults: dict, env: dict) -> str:
    """Resolve openai_base_url: agent → defaults → .env/process OPENAI_BASE_URL."""
    if "openai_base_url" in entry:
        return str(entry.get("openai_base_url") or "").strip().rstrip("/")
    if "openai_base_url" in defaults:
        return str(defaults.get("openai_base_url") or "").strip().rstrip("/")
    return str(
        env.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    ).strip().rstrip("/")


async def h_state(request: web.Request) -> web.Response:
    raw = read_yaml()
    env = read_env_file()
    defaults = raw.get("defaults") or {}
    default_runtime = defaults.get("runtime") or "claude"
    global_repo = (raw.get("github") or {}).get("repo")
    default_base_url = _resolve_openai_base_url({}, defaults, env)
    agents = []
    for entry in _visible_entries(request, raw):
        bot_env, app_env = token_env_names(entry, defaults)
        openai_key_env = (
            entry.get("openai_api_key_env")
            or defaults.get("openai_api_key_env")
            or "OPENAI_API_KEY"
        )
        base_url = _resolve_openai_base_url(entry, defaults, env)
        agents.append(
            {
                "name": entry.get("name"),
                "owner": _entry_owner(entry, raw),
                "runtime": entry.get("runtime") or default_runtime,
                "optional": bool(entry.get("optional", False)),
                "persona": entry.get("persona") or "",
                "card": entry.get("card") or "",
                "workspace": entry.get("workspace") or "",
                "github_repo_explicit": str(entry.get("github_repo") or ""),
                "github_repo": (
                    entry.get("github_repo")
                    if "github_repo" in entry
                    else defaults.get("github_repo", global_repo)
                )
                or "",
                "allowed_tools": entry.get("allowed_tools"),
                "max_turns": entry.get("max_turns"),
                "bot_env": bot_env,
                "app_env": app_env,
                "bot_set": bool(env.get(bot_env) or os.environ.get(bot_env)),
                "app_set": bool(env.get(app_env) or os.environ.get(app_env)),
                "openai_api_key_env": openai_key_env,
                "openai_api_key_set": bool(
                    env.get(openai_key_env) or os.environ.get(openai_key_env)
                ),
                "openai_base_url": base_url,
            }
        )
    return web.json_response(
        {
            "agents": agents,
            "defaults": raw.get("defaults") or {},
            "budget": raw.get("budget") or {},
            "github": raw.get("github") or {},
            "openai_base_url": default_base_url,
        }
    )


async def h_manifest(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    raw = read_yaml()
    _require_agent_access(request, name, raw)
    persona = ""
    for entry in raw.get("agents") or []:
        if entry.get("name") == name:
            persona = (entry.get("persona") or "").strip().splitlines()
            persona = persona[0] if persona else ""
            break
    manifest = build_manifest(name, persona)
    if request.query.get("format") == "json":
        # For Slack's prefilled create link:
        # https://api.slack.com/apps?new_app=1&manifest_json=<url-encoded>
        return web.json_response({"manifest": yaml.safe_load(manifest)})
    return web.Response(text=manifest, content_type="text/plain")


async def h_validate(request: web.Request) -> web.Response:
    body = await request.json()
    result = await validate_tokens(
        (body.get("bot_token") or "").strip(), (body.get("app_token") or "").strip()
    )
    return web.json_response(result)


async def h_save_tokens(request: web.Request) -> web.Response:
    body = await request.json()
    name = (body.get("name") or "").strip()
    bot_token = (body.get("bot_token") or "").strip()
    app_token = (body.get("app_token") or "").strip()
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    raw = read_yaml()
    _require_agent_access(request, name, raw)
    if not (bot_token.startswith("xoxb-") and app_token.startswith("xapp-")):
        return web.json_response(
            {"ok": False, "error": "token の形式が不正です (xoxb-/xapp-)"}
        )
    check = await validate_tokens(bot_token, app_token)
    if not (check["bot_ok"] and check["app_ok"]):
        return web.json_response({"ok": False, "error": "検証に失敗しました", **check})
    async with _CONFIG_LOCK:
        raw = read_yaml()
        _require_agent_access(request, name, raw)
        entry = next(
            (e for e in raw.get("agents") or [] if e.get("name") == name),
            {"name": name},
        )
        bot_env, app_env = token_env_names(
            entry, raw.get("defaults") or {}
        )
        upsert_env({bot_env: bot_token, app_env: app_token})
    return web.json_response({"ok": True, **check})


async def h_save_agent(request: web.Request) -> web.Response:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not NAME_RE.match(name):
        return web.json_response(
            {"ok": False, "error": "name は英小文字始まり [a-z0-9_]{1,16}"}
        )
    runtime = (body.get("runtime") or "").strip().lower()
    if runtime and runtime not in {"claude", "codex", "openai"}:
        return web.json_response(
            {
                "ok": False,
                "error": "runtime は claude / codex / openai のみ",
            }
        )
    async with _CONFIG_LOCK:
        raw = read_yaml()
        existing = _require_agent_access(
            request, name, raw, allow_admin_create=True
        )
        agents: list[dict[str, Any]] = raw.setdefault("agents", [])
        entry = existing
        if entry is None:
            entry = {"name": name}
            agents.append(entry)
        if runtime:
            entry["runtime"] = runtime
        entry["optional"] = bool(body.get("optional", True))
        persona = (body.get("persona") or "").strip()
        if persona:
            entry["persona"] = persona + "\n"
        # Optional L1 teammate card (empty / omitted → omit key → persona first line)
        if "card" in body:
            card = (body.get("card") or "").strip()
            if card:
                entry["card"] = card
            else:
                entry.pop("card", None)
        for key in ("workspace", "max_turns"):
            value = body.get(key)
            if value in (None, ""):
                entry.pop(key, None)
            else:
                entry[key] = int(value) if key == "max_turns" else str(value)
        tools = body.get("allowed_tools")
        if isinstance(tools, list) and tools:
            entry["allowed_tools"] = [str(t) for t in tools]
        elif tools in ([], ""):
            entry.pop("allowed_tools", None)
        write_yaml(raw)
    return web.json_response({"ok": True, "reload": await _admin_reload()})


async def h_update_persona(request: web.Request) -> web.Response:
    """Update an existing agent's persona and optional card; keep other fields."""
    name = request.match_info["name"]
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    raw = read_yaml()
    _require_agent_access(request, name, raw)
    body = await request.json()
    persona = (body.get("persona") or "").strip()
    async with _CONFIG_LOCK:
        raw = read_yaml()
        entry = _require_agent_access(request, name, raw)
        if entry is None:
            return web.json_response({"ok": False, "error": "unknown agent"})
        if persona:
            entry["persona"] = persona + "\n"
        else:
            entry.pop("persona", None)
        # card is optional in the body; when provided, write or clear it
        if "card" in body:
            card = (body.get("card") or "").strip()
            if card:
                entry["card"] = card
            else:
                entry.pop("card", None)
        write_yaml(raw)
    return web.json_response({"ok": True, "reload": await _admin_reload()})


async def _inspect_workspace(path: str) -> dict[str, Any]:
    """Git facts about a local directory: is it a repo, its branch and origin."""
    rc, out, _err = await _run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"], timeout=10
    )
    if rc != 0 or out.strip() != "true":
        return {"git": False, "branch": "", "origin_repo": ""}
    # symbolic-ref also names the unborn branch of a repo with no commits yet
    rc, out, _err = await _run(
        ["git", "-C", path, "symbolic-ref", "--quiet", "--short", "HEAD"], timeout=10
    )
    branch = out.strip() if rc == 0 else ""
    rc, out, _err = await _run(
        ["git", "-C", path, "remote", "get-url", "origin"], timeout=10
    )
    origin = out.strip() if rc == 0 else ""
    return {
        "git": True,
        "branch": branch,
        "origin_repo": (github_repo_from_remote(origin) or "") if origin else "",
    }


async def h_update_workspace(request: web.Request) -> web.Response:
    """Check (``dry_run``) or save one agent's local workspace and GitHub repo.

    ``github_repo`` omitted leaves the setting alone; an empty string clears
    the agent's own value so it inherits defaults / github.repo again. The
    response reports what the directory's git origin points at, and whether
    it disagrees with the effective repo (preflight would disable GitHub).
    """
    name = request.match_info["name"]
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    raw = read_yaml()
    if _require_agent_access(request, name, raw) is None:
        return web.json_response({"ok": False, "error": "unknown agent"})
    body = await request.json()
    workspace = str(body.get("workspace") or "").strip()
    if not workspace:
        return web.json_response({"ok": False, "error": "workspace is required"})
    expanded = os.path.expanduser(workspace)
    if not os.path.isabs(expanded):
        return web.json_response(
            {"ok": False, "error": "use an absolute path or ~/…"}
        )
    path = os.path.realpath(expanded)
    if not os.path.isdir(path):
        return web.json_response(
            {"ok": False, "error": "directory does not exist", "path": path}
        )
    repo_given = "github_repo" in body
    repo = str(body.get("github_repo") or "").strip()
    if repo:
        try:
            repo = canonical_github_repo(repo)
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "github_repo must be OWNER/REPO"}
            )
    entry = _require_agent_access(request, name, raw) or {}
    defaults = raw.get("defaults") or {}
    inherited = str(
        defaults.get("github_repo", (raw.get("github") or {}).get("repo")) or ""
    )
    if repo_given:
        effective = repo or inherited
    else:
        effective = str(entry.get("github_repo") or inherited)
    facts = await _inspect_workspace(path)
    result: dict[str, Any] = {
        "ok": True,
        "path": path,
        **facts,
        "github_repo": effective,
        "mismatch": bool(
            facts["origin_repo"]
            and effective
            and facts["origin_repo"].lower() != effective.lower()
        ),
    }
    if body.get("dry_run"):
        return web.json_response(result)
    async with _CONFIG_LOCK:
        raw = read_yaml()
        entry = _require_agent_access(request, name, raw)
        if entry is None:
            return web.json_response({"ok": False, "error": "unknown agent"})
        entry["workspace"] = workspace
        if repo_given:
            if repo:
                entry["github_repo"] = repo
            else:
                entry.pop("github_repo", None)
        write_yaml(raw)
    result["reload"] = await _admin_reload()
    return web.json_response(result)


def _anthropic_key() -> str:
    """Get a usable ANTHROPIC_API_KEY from the environment or .env (comment lines ignored)."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key.startswith("sk-ant-"):
        return key
    env = read_env_file()
    key = (env.get("ANTHROPIC_API_KEY") or "").strip()
    return key if key.startswith("sk-ant-") else ""


def _read_claude_current() -> str:
    """Read Claude Code current default model from ~/.claude/settings.json (e.g. claude-fable-5[1m])."""
    for path in CLAUDE_SETTINGS:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                model = (data.get("model") or "").strip() if isinstance(data, dict) else ""
                if model:
                    return model
        except (OSError, ValueError):
            continue
    return ""


def _dedup(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


async def _fetch_claude_models() -> tuple[list[str], str, str]:
    """Return (model candidates [empty string first], source label, current default model).

    Current default = model from ~/.claude/settings.json (used when agent model is empty).
    Candidates = current default + CLI aliases (fable/opus/sonnet/haiku) + (if key) Anthropic /v1/models.
    """
    current = _read_claude_current()
    base = _dedup([""] + ([current] if current else []) + CLAUDE_ALIASES)
    key = _anthropic_key()
    if key:
        try:
            async with ClientSession() as session:
                async with session.get(
                    "https://api.anthropic.com/v1/models?limit=100",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    timeout=8,
                ) as resp:
                    data = await resp.json()
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            if ids:
                return _dedup(base + ids), "settings.json + Anthropic API", current
        except Exception:
            pass
    src = "~/.claude/settings.json + CLI aliases" if current else "CLI aliases (API key not set)"
    return base, src, current


def _read_codex_config() -> tuple[str, str, dict[str, str]]:
    """Read ~/.codex/config.toml: return (current model, current effort, migration map old->new)."""
    model = effort = ""
    migrations: dict[str, str] = {}
    try:
        if CODEX_CONFIG.exists():
            text = CODEX_CONFIG.read_text(encoding="utf-8")
            m = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', text)
            model = m.group(1) if m else ""
            e = re.search(
                r'(?m)^\s*model_reasoning_effort\s*=\s*"([^"]+)"', text
            )
            effort = e.group(1) if e else ""
            # [notice.model_migrations] section: "old" = "new"
            seg = re.search(
                r"\[notice\.model_migrations\](.*?)(?:\n\[|\Z)", text, re.S
            )
            if seg:
                for old, new in re.findall(
                    r'"?([\w.\-]+)"?\s*=\s*"([^"]+)"', seg.group(1)
                ):
                    migrations[old] = new
    except OSError:
        pass
    return model, effort, migrations


def _codex_models() -> tuple[list[str], str, str]:
    """Return (candidates [incl. empty string], source, current default).

    Accuracy: only real info from config -- current model + migration map **target** models
    (deprecated old names are omitted; if selected, codex migrates automatically).
    """
    model, _effort, migrations = _read_codex_config()
    source = "~/.codex/config.toml" if model else "builtin"
    # Prefer migration targets (new models); drop deprecated old names
    targets = list(dict.fromkeys(migrations.values()))
    candidates = ([model] if model else []) + targets
    # Drop old names deprecated by migration
    candidates = [c for c in candidates if c not in migrations]
    merged = _dedup([""] + candidates)
    return merged, source, model


def _codex_default_effort() -> str:
    _model, effort, _mig = _read_codex_config()
    return effort


async def _fetch_openai_compatible_models(
    base_url: str, api_key: str = ""
) -> tuple[list[str], str]:
    """GET {base_url}/models from an OpenAI-compatible endpoint (CLI Proxy).

    Returns (model_ids, source_label). On failure returns ([], "").
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return [], ""
    url = f"{base}/models"
    headers: dict[str, str] = {}
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=5
            ) as resp:
                if resp.status >= 400:
                    logger.debug(
                        "openai-compatible /models %s -> %s", url, resp.status
                    )
                    return [], ""
                data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 — best-effort catalog enrich
        logger.debug("openai-compatible /models failed for %s: %s", url, exc)
        return [], ""
    ids: list[str] = []
    items = data.get("data") if isinstance(data, dict) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                mid = str(item.get("id") or "").strip()
                if mid:
                    ids.append(mid)
            elif isinstance(item, str) and item.strip():
                ids.append(item.strip())
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
            elif isinstance(item, dict):
                mid = str(item.get("id") or "").strip()
                if mid:
                    ids.append(mid)
    # De-dupe preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            ordered.append(mid)
    source = f"proxy {base}"
    return ordered, source


def _openai_api_key_for_models(env: dict) -> str:
    """Pick a key for proxy /models (env file or process env)."""
    for name in (
        "OPENAI_API_KEY",
        "CLIPROXY_API_KEY",
    ):
        val = (env.get(name) or os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


async def h_models(request: web.Request) -> web.Response:
    """Model candidates for UI dropdowns -- prefer real sources, fall back on failure, 60s cache."""
    import time as _t

    now = _t.monotonic()
    actor = _principal(request)
    use_shared_cache = actor.is_admin
    if (
        use_shared_cache
        and _models_cache
        and now - _models_cache.get("t", 0) < 60
    ):
        return web.json_response(_models_cache["payload"])
    claude, claude_src, claude_cur = await _fetch_claude_models()
    codex, codex_src, codex_cur = _codex_models()

    env = read_env_file()
    raw = read_yaml()
    visible = _visible_entries(request, raw)
    defaults = raw.get("defaults") or {}
    base_url = _resolve_openai_base_url({}, defaults, env)
    # Also try first agent-level base_url if defaults empty
    if not base_url:
        for entry in visible:
            base_url = _resolve_openai_base_url(entry, defaults, env)
            if base_url:
                break
    proxy_models, proxy_src = await _fetch_openai_compatible_models(
        base_url, _openai_api_key_for_models(env)
    )
    openai_list: list[str] = []
    seen_m: set[str] = set()
    for mid in OPENAI_MODELS + proxy_models:
        if mid not in seen_m:
            seen_m.add(mid)
            openai_list.append(mid)
    openai_src = (
        f"{proxy_src} + built-in" if proxy_src else "OpenAI / CLI Proxy catalog"
    )

    payload = {
        "claude": claude,
        "codex": codex,
        "openai": openai_list,
        "sources": {
            "claude": claude_src,
            "codex": codex_src,
            "openai": openai_src,
        },
        "current": {
            "claude": claude_cur,
            "codex": codex_cur,
            "openai": DEFAULT_OPENAI_MODEL,
        },
        "efforts": {
            "claude": CLAUDE_EFFORTS,
            "codex": CODEX_EFFORTS,
            "openai": OPENAI_EFFORTS,
        },
        "effort_default": {
            "claude": "",
            "codex": _codex_default_effort(),
            "openai": "",
        },
        "openai_base_url": base_url,
    }
    if use_shared_cache:
        _models_cache["t"] = now
        _models_cache["payload"] = payload
    return web.json_response(payload)


async def h_live_state(request: web.Request) -> web.Response:
    """Proxy the running multi_app /state (live monitor data source).

    When disconnected return online=False so the UI shows offline rather than an error.
    """
    try:
        authorization = _CONTROL_AUTHORIZATION.get()
        headers = (
            {"Authorization": authorization}
            if authorization
            else None
        )
        async with ClientSession() as session:
            async with session.get(
                f"{ADMIN_BASE}/state",
                headers=headers,
                timeout=3,
            ) as resp:
                payload = await resp.json()
                # Keep owner isolation at both control-plane hops. The runtime
                # API already scopes /state, but the WebUI must not become a
                # cross-owner disclosure path if an older/misconfigured
                # upstream returns a broader payload.
                actor = _principal(request)
                if (
                    isinstance(payload, dict)
                    and not actor.is_admin
                    and not actor.legacy
                ):
                    agents = payload.get("agents")
                    if isinstance(agents, list):
                        payload["agents"] = [
                            item
                            for item in agents
                            if isinstance(item, dict)
                            and item.get("owner") == actor.user_id
                        ]
                    usage = payload.get("owner_usage")
                    if isinstance(usage, dict):
                        own = usage.get(actor.user_id)
                        payload["owner_usage"] = (
                            {actor.user_id: own}
                            if isinstance(own, dict)
                            else {}
                        )
                    worktrees = payload.get("worktrees")
                    if isinstance(worktrees, list):
                        payload["worktrees"] = [
                            item
                            for item in worktrees
                            if isinstance(item, dict)
                            and item.get("owner") == actor.user_id
                        ]
                return web.json_response(payload)
    except Exception:
        return web.json_response(
            {
                "online": False,
                "agents": [],
                "owner_usage": {},
                "worktrees": [],
            }
        )


ISSUES_CACHE_TTL_SECONDS = 30.0
ISSUES_CACHE_MAX = 64
_issues_cache: dict[str, Any] = {}


def _configured_github_repos(
    raw: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return distinct effective per-agent repos in deterministic order."""
    defaults = raw.get("defaults") or {}
    global_repo = (raw.get("github") or {}).get("repo")
    repos: list[str] = []
    for entry in (
        raw.get("agents") or [] if entries is None else entries
    ):
        if not isinstance(entry, dict):
            continue
        if "github_repo" in entry:
            value = entry.get("github_repo")
        elif "github_repo" in defaults:
            value = defaults.get("github_repo")
        else:
            value = global_repo
        if not value:
            continue
        repo = canonical_github_repo(str(value))
        if repo not in repos:
            repos.append(repo)
    return repos


async def _kill_and_reap_subprocess(proc: Any) -> None:
    """Kill a child if still running and always wait away its process handle."""
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


async def _finish_subprocess_cleanup(proc: Any) -> None:
    """Shield process reaping from request cancellation and loop shutdown."""
    cleanup_coro = _kill_and_reap_subprocess(proc)
    try:
        cleanup = asyncio.create_task(cleanup_coro)
    except BaseException:
        cleanup_coro.close()
        while True:
            try:
                await _kill_and_reap_subprocess(proc)
                return
            except asyncio.CancelledError:
                continue
            except BaseException:
                logger.warning(
                    "failed inline subprocess reap", exc_info=True
                )
                return
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        cleanup.result()
    except BaseException:
        logger.warning("failed to reap subprocess", exc_info=True)


async def _gh_issues(repo: str) -> tuple[list[dict], str]:
    """`gh issue list` for open issues (task board). Returns (issues, error)."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "gh", "issue", "list", "--repo", repo, "--state", "open",
        "--limit", "30", "--json", "number,title,state,labels,assignees,url",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except BaseException as exc:
        await _finish_subprocess_cleanup(proc)
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return [], "gh issue list timeout"
        raise
    if proc.returncode != 0:
        return [], (err.decode(errors="replace").strip() or "gh error")
    try:
        raw = json.loads(out.decode(errors="replace") or "[]")
    except ValueError:
        return [], "failed to parse gh output"
    issues = [
        {
            "number": it.get("number"),
            "title": it.get("title") or "",
            "url": it.get("url") or "",
            "labels": [lb.get("name") for lb in (it.get("labels") or [])],
            "assignees": [a.get("login") for a in (it.get("assignees") or [])],
        }
        for it in raw
    ]
    return issues, ""


async def h_issues(request: web.Request) -> web.Response:
    """Monitor open issues across every effective per-agent repo (30s cache)."""
    import time as _t

    try:
        raw = read_yaml()
        repos = _configured_github_repos(
            raw, _visible_entries(request, raw)
        )
    except ValueError as exc:
        return web.json_response(
            {"repo": "", "repos": [], "issues": [], "error": str(exc)}
        )
    if not repos:
        return web.json_response(
            {
                "repo": "",
                "repos": [],
                "issues": [],
                "error": "no agent github_repo configured",
            }
        )
    now = _t.monotonic()
    expired = [
        key
        for key, value in _issues_cache.items()
        if now - float(value.get("t") or 0) >= ISSUES_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _issues_cache.pop(key, None)
    cache_key = "\0".join(repos)
    c = _issues_cache.get(cache_key)
    if c and now - c["t"] < ISSUES_CACHE_TTL_SECONDS:
        return web.json_response(c["payload"])
    if not shutil.which("gh"):
        return web.json_response(
            {
                "repo": ", ".join(repos),
                "repos": repos,
                "issues": [],
                "error": "gh not installed",
            }
        )
    results = await asyncio.gather(*(_gh_issues(repo) for repo in repos))
    issues: list[dict[str, Any]] = []
    errors: list[str] = []
    for repo, (repo_issues, error) in zip(repos, results):
        issues.extend({**issue, "repo": repo} for issue in repo_issues)
        if error:
            errors.append(f"{repo}: {error}")
    payload = {
        "repo": ", ".join(repos),
        "repos": repos,
        "issues": issues,
        "error": "; ".join(errors),
    }
    _issues_cache[cache_key] = {"t": now, "payload": payload}
    while len(_issues_cache) > ISSUES_CACHE_MAX:
        oldest = min(
            _issues_cache,
            key=lambda key: float(_issues_cache[key].get("t") or 0),
        )
        _issues_cache.pop(oldest, None)
    return web.json_response(payload)


CHANNEL_RULES_TEMPLATE = BASE_DIR / "channel-rules-template.md"


async def h_channel_rules(request: web.Request) -> web.Response:
    """Shared channel-rules template (for Slack topic/purpose); fill {{repo}} from config."""
    try:
        text = CHANNEL_RULES_TEMPLATE.read_text(encoding="utf-8")
    except OSError:
        return web.json_response({"template": "", "error": "template not found"})
    repo = (read_yaml().get("github") or {}).get("repo") or ""
    if repo:
        text = text.replace("{{OWNER/REPO}}", repo)
    return web.json_response({"template": text, "repo": repo, "error": ""})


async def _admin_post(path: str, body: dict) -> tuple[dict, int]:
    """POST to the running multi_app admin API; returns (json, status).

    Connection failure → ({ok: False, error}, 502) so callers can degrade gracefully.
    """
    try:
        authorization = _CONTROL_AUTHORIZATION.get()
        headers = (
            {"Authorization": authorization}
            if authorization
            else None
        )
        async with ClientSession() as session:
            async with session.post(
                f"{ADMIN_BASE}{path}",
                json=body,
                headers=headers,
                timeout=5,
            ) as resp:
                return await resp.json(), resp.status
    except Exception as exc:
        return (
            {"ok": False, "error": f"multi_app に接続できません: {exc}"},
            502,
        )


async def _admin_reload() -> dict:
    """Best-effort hot reload of the running multi_app after a config save.

    Offline is not an error — the yaml is already written and applies on next start.
    """
    data, _status = await _admin_post("/reload", {})
    return data


def _persist_agent_fields_unlocked(name: str, updates: dict[str, Any]) -> bool:
    """Write hot-swapped fields to agents.yaml (caller must hold _CONFIG_LOCK).

    For model/effort, empty string is kept as an explicit engine-default override
    so load does not re-merge team defaults. Other empty values still pop the key.
    Returns False when the agent has no yaml entry (nothing persisted).
    """
    raw = read_yaml()
    entry = next(
        (e for e in raw.get("agents") or [] if e.get("name") == name), None
    )
    if entry is None:
        return False
    for key, value in updates.items():
        if value is None:
            entry.pop(key, None)
        elif value == "" and key not in _EXPLICIT_EMPTY_KEYS:
            entry.pop(key, None)
        else:
            entry[key] = value
    write_yaml(raw)
    return True


async def _persist_agent_fields(name: str, updates: dict[str, Any]) -> bool:
    """Write hot-swapped fields back to agents.yaml so they survive a restart."""
    async with _CONFIG_LOCK:
        return _persist_agent_fields_unlocked(name, updates)


async def _live_set_and_persist(
    request: web.Request,
    endpoint: str,
    to_updates: Callable[[dict], dict[str, Any]],
) -> web.Response:
    """Proxy a hot-swap POST to the admin API; on success persist to agents.yaml.

    Live apply and YAML write share _CONFIG_LOCK so concurrent POSTs cannot end
    with live=B / YAML=A. Disk failure after a successful live apply is reported
    via persisted=false + persist_error (caller can retry save).
    to_updates maps the admin response to yaml fields (the admin echoes the applied
    values, so what we persist is exactly what the process is running with).
    """
    name = request.match_info["name"]
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    _require_agent_access(request, name, read_yaml())
    body = await request.json()
    async with _CONFIG_LOCK:
        data, status = await _admin_post(f"/agents/{name}/{endpoint}", body)
        if status == 200 and isinstance(data, dict) and data.get("ok"):
            try:
                data["persisted"] = _persist_agent_fields_unlocked(
                    name, to_updates(data)
                )
            except Exception as exc:
                logger.exception(
                    "live set ok but yaml persist failed for %s/%s", name, endpoint
                )
                data["persisted"] = False
                data["persist_error"] = str(exc)
        return web.json_response(data, status=status)


async def h_live_set_model(request: web.Request) -> web.Response:
    return await _live_set_and_persist(
        request,
        "model",
        lambda d: {
            (
                "codex_model"
                if d.get("runtime") == "codex"
                else (
                    "openai_model"
                    if d.get("runtime") == "openai"
                    else "claude_model"
                )
            ): d.get("model") or ""
        },
    )


async def h_live_set_runtime(request: web.Request) -> web.Response:
    return await _live_set_and_persist(
        request, "runtime", lambda d: {"runtime": d.get("runtime")}
    )


async def h_live_restart(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not NAME_RE.match(name):
        raise web.HTTPBadRequest(text="invalid name")
    _require_agent_access(request, name, read_yaml())
    data, status = await _admin_post(f"/agents/{name}/restart", {})
    return web.json_response(data, status=status)


async def h_live_remove_worktree(request: web.Request) -> web.Response:
    identity_digest = request.match_info["identity_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", identity_digest):
        raise web.HTTPBadRequest(text="invalid worktree identity")
    data, status = await _admin_post(
        f"/worktrees/{identity_digest}/remove", {}
    )
    return web.json_response(data, status=status)


async def h_live_set_reply_language(request: web.Request) -> web.Response:
    return await _live_set_and_persist(
        request,
        "reply_language",
        lambda d: {"reply_language": d.get("reply_language") or ""},
    )


async def h_live_set_effort(request: web.Request) -> web.Response:
    return await _live_set_and_persist(
        request, "effort", lambda d: {"effort": d.get("effort") or ""}
    )


async def h_live_reload(request: web.Request) -> web.Response:
    """Manually push agents.yaml (e.g. hand-edited) into the running multi_app."""
    if not _principal(request).is_admin:
        raise web.HTTPForbidden(text="forbidden")
    data, status = await _admin_post("/reload", {})
    return web.json_response(data, status=status)


# ---------------------------------------------------------------------------
# Credentials: Claude / Codex / GitHub sign-in
#
# Agents run in Docker when available (claude-home volume, ~/.codex mount,
# GH_TOKEN via env_file). WebUI drives the matching CLI through
# `docker compose exec`, falling back to a host install for `make run`.
#
# Flows
#   Claude : interactive OAuth — URL + pasted code (PTY kept alive for PKCE)
#   Codex  : device-code — show URL + one-time code, wait for browser approval
#   GitHub : host device-code — show URL + code, then write GH_TOKEN into .env
#            (container cannot use the host keychain; env is the reliable path)
#
# Security: argv is fixed. Caller-supplied secrets only reach a process via
# stdin (never argv / shell). Secrets written to .env are never echoed back.
# ---------------------------------------------------------------------------

COMPOSE_SERVICE = os.environ.get("COMPOSE_SERVICE", "multi-app")

# OSC (\x1b]…BEL/ST), CSI (\x1b[…), charset selects, and any other stray escape.
# The OSC-8 hyperlink wrapper carries the URL twice; dropping the OSC payload
# leaves the visible copy, which is what we parse.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][B0]|\x1b."
)
_URL_RE = re.compile(r"https://[^\s\x00-\x20\"'<>]+")
# Printable ASCII, no whitespace — a pasted OAuth code or token, nothing that
# could inject a second line into the child's stdin.
_SECRET_RE = re.compile(r"\A[\x21-\x7e]{8,512}\Z")
# Device codes shown by Codex / GitHub (e.g. AB12-CD34, ABCDE-12345).
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")
_LOGIN_OK_RE = re.compile(
    r"(login successful|successfully logged in|logged in using|✓\s*logged in)",
    re.I,
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _find_url(text: str, *needles: str) -> str:
    """First https URL containing any needle (case-sensitive)."""
    for url in _URL_RE.findall(text):
        if any(n in url for n in needles):
            return url.rstrip(").,;'\"")
    return ""


def _find_auth_url(text: str) -> str:
    """Claude OAuth authorize URL."""
    return _find_url(text, "/oauth/authorize")


def _find_device_code(text: str) -> str:
    m = _DEVICE_CODE_RE.search(text)
    return m.group(1) if m else ""


def _normalize_oauth_code(raw: str) -> str:
    """Accept a bare code or a full redirect/authorize URL containing code=."""
    s = (raw or "").strip()
    if not s:
        return ""
    # User sometimes pastes the whole callback / authorize URL.
    if "code=" in s:
        from urllib.parse import parse_qs, urlparse

        # Works for full URLs and bare query strings.
        query = s.split("?", 1)[-1] if "://" not in s and s.startswith("code=") else (
            urlparse(s).query if "://" in s else s
        )
        if "://" in s:
            qs = parse_qs(urlparse(s).query)
        elif "=" in s:
            qs = parse_qs(s if "code=" in s else query)
        else:
            qs = {}
        code = (qs.get("code") or [""])[0].strip()
        if code:
            return code
    # Drop accidental surrounding quotes / whitespace.
    return s.strip().strip("'\"")


async def _run(
    argv: list[str], timeout: float = 15.0
) -> tuple[int, str, str]:
    """Run argv with no shell. Returns (rc, stdout, stderr); rc -1 on timeout / missing binary."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(BASE_DIR),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return -1, "", f"{argv[0]} not found"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as exc:
        await _finish_subprocess_cleanup(proc)
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return -1, "", "timeout"
        raise
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def _compose_up() -> bool:
    rc, _, _ = await _run(
        ["docker", "compose", "exec", "-T", COMPOSE_SERVICE, "true"], timeout=20
    )
    return rc == 0


async def _cli_mode(binary: str) -> str:
    """Where a CLI lives for agents: "docker" | "host" | "none"."""
    if await _compose_up():
        return "docker"
    return "host" if shutil.which(binary) else "none"


def _cli_cmd(mode: str, binary: str, args: list[str], *, tty: bool) -> list[str]:
    if mode == "docker":
        return [
            "docker", "compose", "exec",
            "-it" if tty else "-T",
            COMPOSE_SERVICE, binary, *args,
        ]
    return [binary, *args]


# --- status probes (never raise; never return secrets) ----------------------

async def _claude_status(mode: str) -> dict[str, Any]:
    if mode == "none":
        return {
            "available": False,
            "logged_in": False,
            "auth_method": "",
            "error": "claude CLI not found (container down and no host install)",
        }
    rc, out, err = await _run(
        _cli_cmd(mode, "claude", ["auth", "status"], tty=False), timeout=30
    )
    try:
        data = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        detail = (err or out).strip() or f"claude auth status failed (rc={rc})"
        return {
            "available": True,
            "logged_in": False,
            "auth_method": "",
            "error": detail[:300],
        }
    return {
        "available": True,
        "logged_in": bool(data.get("loggedIn")),
        "auth_method": str(data.get("authMethod") or ""),
        "error": "",
    }


async def _codex_status(mode: str) -> dict[str, Any]:
    if mode == "none":
        return {
            "available": False,
            "logged_in": False,
            "auth_method": "",
            "error": "codex CLI not found (container down and no host install)",
        }
    rc, out, err = await _run(
        _cli_cmd(mode, "codex", ["login", "status"], tty=False), timeout=30
    )
    text = (out or err).strip()
    low = text.lower()
    logged_in = rc == 0 and "not logged in" not in low and bool(text)
    return {
        "available": True,
        "logged_in": logged_in,
        "auth_method": text[:120] if logged_in else "",
        "error": "" if (logged_in or "not logged in" in low) else (text[:300] or f"rc={rc}"),
    }


def _gh_token_set() -> bool:
    return bool(read_env_file().get("GH_TOKEN") or os.environ.get("GH_TOKEN"))


async def _gh_host_status() -> dict[str, Any]:
    """Host `gh auth status` — used for device login + import."""
    if not shutil.which("gh"):
        return {"available": False, "logged_in": False, "user": "", "error": "gh not installed on this host"}
    rc, out, err = await _run(["gh", "auth", "status"], timeout=15)
    text = (out or err).strip()
    logged_in = rc == 0 and "logged in" in text.lower()
    user = ""
    m = re.search(r"account\s+(\S+)", text, re.I)
    if m:
        user = m.group(1).strip("()")
    return {
        "available": True,
        "logged_in": logged_in,
        "user": user,
        "error": "" if logged_in else (text[:300] or f"rc={rc}"),
    }


# --- PTY session ------------------------------------------------------------

class CliAuthSession:
    """One live CLI auth process held open across HTTP requests (PKCE / device)."""

    TTL_SECONDS = 900
    BUF_CAP = 256_000

    def __init__(self, kind: str, mode: str, argv: list[str]) -> None:
        self.kind = kind  # claude | codex | gh
        self.mode = mode
        self.argv = argv
        self.started = 0.0
        self.url = ""
        self.device_code = ""
        self.done = False
        self.exit_code: int | None = None
        self._buf = b""
        self._master = -1
        self._proc: asyncio.subprocess.Process | None = None
        self._event = asyncio.Event()
        self._watcher: asyncio.Task[None] | None = None
        self._git_answered = False

    @property
    def expired(self) -> bool:
        loop = asyncio.get_running_loop()
        return loop.time() - self.started > self.TTL_SECONDS

    def text(self) -> str:
        return _strip_ansi(self._buf.decode(errors="replace"))

    def _parse_progress(self) -> None:
        t = self.text()
        if self.kind == "claude":
            if not self.url:
                self.url = _find_auth_url(t)
        elif self.kind == "codex":
            if not self.url:
                self.url = _find_url(t, "auth.openai.com/codex/device", "auth.openai.com")
            if not self.device_code:
                self.device_code = _find_device_code(t)
        elif self.kind == "gh":
            if not self.url:
                self.url = _find_url(t, "github.com/login/device", "github.com/login")
            if not self.device_code:
                self.device_code = _find_device_code(t)
            # Skip the interactive "Authenticate Git …?" prompt.
            if not self._git_answered and "Authenticate Git" in t:
                try:
                    os.write(self._master, b"n\r")
                    self._git_answered = True
                except OSError:
                    pass

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self._master, 65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stop_reading()
        else:
            self._buf += chunk[: max(0, self.BUF_CAP - len(self._buf))]
            self._parse_progress()
        self._event.set()

    def _stop_reading(self) -> None:
        if self._master >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self._master)
            except (OSError, RuntimeError):
                pass

    async def _wait_until(self, ready: Callable[[], bool], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            self._event.clear()
            if ready():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except TimeoutError:
                return False

    async def start(self, ready: Callable[[], bool], timeout: float = 60.0) -> bool:
        """Spawn the CLI on a pty; return True when ready() is satisfied."""
        loop = asyncio.get_running_loop()
        self.started = loop.time()
        master, slave = pty.openpty()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(BASE_DIR),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            os.close(slave)
            return False
        finally:
            os.close(slave)
        self._master = master
        os.set_blocking(master, False)
        loop.add_reader(master, self._on_readable)
        ok = await self._wait_until(ready, timeout)
        # Device flows keep running until the user finishes in the browser.
        if self.kind in {"codex", "gh"} and self._proc is not None:
            self._watcher = asyncio.create_task(self._watch_exit())
        return ok

    async def _watch_exit(self) -> None:
        if self._proc is None:
            return
        try:
            self.exit_code = await self._proc.wait()
        except Exception:
            self.exit_code = -1
        self.done = True
        self._event.set()

    async def submit_code(self, code: str, timeout: float = 90.0) -> tuple[bool, str]:
        """Feed a pasted OAuth code (Claude). Verifies via status + CLI text."""
        if self._proc is None or self._master < 0:
            return False, "login session is no longer running"
        try:
            os.write(self._master, code.encode() + b"\r")
        except OSError as exc:
            return False, f"could not reach the sign-in process: {exc}"
        try:
            await asyncio.wait_for(self._proc.wait(), timeout)
        except TimeoutError:
            # CLI may already have printed success while still hanging.
            tail = self.text().strip()
            if _LOGIN_OK_RE.search(tail):
                return True, "login successful"
            return False, "timed out waiting for sign-in to finish"
        self.done = True
        self.exit_code = self._proc.returncode
        # Prefer status, but accept CLI success banner (status can lag / flakily fail).
        for _ in range(4):
            status = await _claude_status(self.mode)
            if status["logged_in"]:
                return True, status["auth_method"] or "logged_in"
            await asyncio.sleep(0.4)
        tail = self.text().strip()
        if _LOGIN_OK_RE.search(tail):
            return True, "login successful"
        return False, (tail[-300:] or "sign-in did not complete")

    async def wait_done(self, timeout: float = 0.0) -> bool:
        """Wait until the device-code process exits. timeout=0 → non-blocking snapshot."""
        if self.done:
            return True
        if timeout <= 0:
            return False
        return await self._wait_until(lambda: self.done, timeout)

    async def close(self) -> None:
        if self._watcher is not None and not self._watcher.done():
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):
                pass
            self._watcher = None
        self._stop_reading()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.kill()
            try:
                await asyncio.wait_for(self._proc.wait(), 5)
            except TimeoutError:
                pass
        if self._master >= 0:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = -1


# Single-flight per provider (PKCE / device state is per-process).
_sessions: dict[str, CliAuthSession] = {}
_LOGIN_LOCK = asyncio.Lock()


async def _drop_session(kind: str) -> None:
    sess = _sessions.pop(kind, None)
    if sess is not None:
        await sess.close()


async def _drop_all_sessions() -> None:
    for kind in list(_sessions):
        await _drop_session(kind)


def _session_public(sess: CliAuthSession | None) -> dict[str, Any]:
    if sess is None or sess.expired:
        return {"pending": False}
    return {
        "pending": not sess.done,
        "done": sess.done,
        "url": sess.url,
        "device_code": sess.device_code,
        "mode": sess.mode,
    }


async def h_auth_state(request: web.Request) -> web.Response:
    """Credential status for the auth panel. Never returns secret values."""
    claude_mode = await _cli_mode("claude")
    codex_mode = await _cli_mode("codex")
    claude = await _claude_status(claude_mode)
    claude["mode"] = claude_mode
    codex = await _codex_status(codex_mode)
    codex["mode"] = codex_mode
    gh_host = await _gh_host_status()
    async with _LOGIN_LOCK:
        # Finalize completed device sessions so the badge flips without a second call.
        for kind, checker in (
            ("codex", lambda: _codex_status(codex_mode)),
            ("gh", None),
        ):
            sess = _sessions.get(kind)
            if sess is not None and sess.done:
                if kind == "codex":
                    st = await checker()  # type: ignore[misc]
                    if st["logged_in"]:
                        await _drop_session(kind)
                elif kind == "gh":
                    # Import host token into .env once device login finishes.
                    if shutil.which("gh"):
                        rc, out, err = await _run(["gh", "auth", "token"], timeout=15)
                        token = out.strip()
                        if rc == 0 and _SECRET_RE.match(token):
                            async with _CONFIG_LOCK:
                                upsert_env({"GH_TOKEN": token})
                    await _drop_session(kind)
        claude_pub = _session_public(_sessions.get("claude"))
        codex_pub = _session_public(_sessions.get("codex"))
        gh_pub = _session_public(_sessions.get("gh"))
    claude.update(claude_pub)
    codex.update(codex_pub)
    return web.json_response(
        {
            "claude": claude,
            "codex": codex,
            "gh": {
                "token_set": _gh_token_set(),
                "host_gh": bool(shutil.which("gh")),
                "host_logged_in": gh_host["logged_in"],
                "host_user": gh_host.get("user") or "",
                "available": bool(shutil.which("gh")),
                "error": gh_host.get("error") or "",
                **gh_pub,
            },
        }
    )


async def h_claude_login_start(request: web.Request) -> web.Response:
    async with _LOGIN_LOCK:
        await _drop_session("claude")
        mode = await _cli_mode("claude")
        if mode == "none":
            return web.json_response(
                {"ok": False, "error": "claude CLI not found"}, status=400
            )
        sess = CliAuthSession(
            "claude", mode, _cli_cmd(mode, "claude", ["auth", "login"], tty=True)
        )
        ok = await sess.start(lambda: bool(sess.url), timeout=60.0)
        if not ok or not sess.url:
            await sess.close()
            return web.json_response(
                {"ok": False, "error": "sign-in did not produce an authorize URL"},
                status=502,
            )
        _sessions["claude"] = sess
        return web.json_response({"ok": True, "url": sess.url, "mode": mode})


async def h_claude_login_code(request: web.Request) -> web.Response:
    body = await request.json()
    code = _normalize_oauth_code(str(body.get("code") or ""))
    if not _SECRET_RE.match(code):
        return web.json_response(
            {"ok": False, "error": "invalid code format"}, status=400
        )
    async with _LOGIN_LOCK:
        sess = _sessions.get("claude")
        if sess is None:
            return web.json_response(
                {"ok": False, "error": "no sign-in in progress"}, status=409
            )
        if sess.expired:
            await _drop_session("claude")
            return web.json_response(
                {"ok": False, "error": "sign-in expired; start again"}, status=409
            )
        ok, message = await sess.submit_code(code)
        await _drop_session("claude")
        # Re-check so the response can include verified status.
        mode = await _cli_mode("claude")
        status = await _claude_status(mode)
        if ok and not status["logged_in"]:
            # CLI said success; surface verified even if status JSON lags.
            status = {
                "available": True,
                "logged_in": True,
                "auth_method": message or "logged_in",
                "error": "",
                "mode": mode,
            }
        else:
            status["mode"] = mode
    return web.json_response(
        {"ok": ok, "message": message, "claude": status},
        status=200 if ok else 400,
    )


async def h_claude_login_cancel(request: web.Request) -> web.Response:
    async with _LOGIN_LOCK:
        await _drop_session("claude")
    return web.json_response({"ok": True})


async def h_codex_login_start(request: web.Request) -> web.Response:
    """Start Codex device-code login (works headless / inside Docker)."""
    async with _LOGIN_LOCK:
        await _drop_session("codex")
        mode = await _cli_mode("codex")
        if mode == "none":
            return web.json_response(
                {"ok": False, "error": "codex CLI not found"}, status=400
            )
        sess = CliAuthSession(
            "codex",
            mode,
            _cli_cmd(mode, "codex", ["login", "--device-auth"], tty=True),
        )
        ok = await sess.start(
            lambda: bool(sess.url and sess.device_code), timeout=45.0
        )
        if not ok:
            tail = sess.text().strip()[-200:]
            await sess.close()
            return web.json_response(
                {
                    "ok": False,
                    "error": tail or "device login did not produce a code",
                },
                status=502,
            )
        _sessions["codex"] = sess
        return web.json_response(
            {
                "ok": True,
                "url": sess.url,
                "device_code": sess.device_code,
                "mode": mode,
            }
        )


async def h_codex_login_wait(request: web.Request) -> web.Response:
    """Poll / short-wait for Codex device login completion."""
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        wait = min(max(float((body or {}).get("wait") or 0), 0.0), 25.0)
    except (TypeError, ValueError):
        wait = 0.0
    async with _LOGIN_LOCK:
        sess = _sessions.get("codex")
        if sess is None:
            mode = await _cli_mode("codex")
            st = await _codex_status(mode)
            st["mode"] = mode
            return web.json_response({"ok": st["logged_in"], "pending": False, "codex": st})
        if sess.expired:
            await _drop_session("codex")
            return web.json_response(
                {"ok": False, "pending": False, "error": "sign-in expired"}, status=409
            )
        if wait and not sess.done:
            # Release lock while waiting so status can be polled? Keep lock short —
            # wait outside by copying reference.
            pass
    # Wait outside the lock so concurrent /state can still read.
    if sess is not None and wait and not sess.done:
        await sess.wait_done(wait)
    async with _LOGIN_LOCK:
        sess = _sessions.get("codex")
        if sess is None:
            mode = await _cli_mode("codex")
            st = await _codex_status(mode)
            st["mode"] = mode
            return web.json_response({"ok": st["logged_in"], "pending": False, "codex": st})
        if not sess.done:
            return web.json_response(
                {
                    "ok": False,
                    "pending": True,
                    "url": sess.url,
                    "device_code": sess.device_code,
                }
            )
        mode = sess.mode
        await _drop_session("codex")
    # Verify after process exit.
    st = {"logged_in": False, "auth_method": "", "error": "", "available": True}
    for _ in range(5):
        st = await _codex_status(mode)
        if st["logged_in"]:
            break
        await asyncio.sleep(0.5)
    st["mode"] = mode
    ok = bool(st["logged_in"])
    return web.json_response(
        {"ok": ok, "pending": False, "codex": st, "message": st.get("auth_method") or ""},
        status=200 if ok else 400,
    )


async def h_codex_login_cancel(request: web.Request) -> web.Response:
    async with _LOGIN_LOCK:
        await _drop_session("codex")
    return web.json_response({"ok": True})


async def h_gh_login_start(request: web.Request) -> web.Response:
    """Start GitHub device login on the *host*, then we copy the token into .env."""
    if not shutil.which("gh"):
        return web.json_response(
            {"ok": False, "error": "gh not installed on this host"}, status=400
        )
    async with _LOGIN_LOCK:
        await _drop_session("gh")
        sess = CliAuthSession(
            "gh",
            "host",
            [
                "gh", "auth", "login",
                "-h", "github.com",
                "-p", "https",
                "-w",
                "--skip-ssh-key",
            ],
        )
        ok = await sess.start(
            lambda: bool(sess.url and sess.device_code), timeout=45.0
        )
        if not ok:
            tail = sess.text().strip()[-200:]
            await sess.close()
            return web.json_response(
                {
                    "ok": False,
                    "error": tail or "device login did not produce a code",
                },
                status=502,
            )
        _sessions["gh"] = sess
        return web.json_response(
            {
                "ok": True,
                "url": sess.url,
                "device_code": sess.device_code,
                "mode": "host",
            }
        )


async def h_gh_login_wait(request: web.Request) -> web.Response:
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        wait = min(max(float((body or {}).get("wait") or 0), 0.0), 25.0)
    except (TypeError, ValueError):
        wait = 0.0
    async with _LOGIN_LOCK:
        sess = _sessions.get("gh")
        if sess is None:
            host = await _gh_host_status()
            return web.json_response(
                {
                    "ok": _gh_token_set() or host["logged_in"],
                    "pending": False,
                    "gh": {
                        "token_set": _gh_token_set(),
                        "host_logged_in": host["logged_in"],
                        "host_user": host.get("user") or "",
                    },
                }
            )
        if sess.expired:
            await _drop_session("gh")
            return web.json_response(
                {"ok": False, "pending": False, "error": "sign-in expired"}, status=409
            )
    if sess is not None and wait and not sess.done:
        await sess.wait_done(wait)
    async with _LOGIN_LOCK:
        sess = _sessions.get("gh")
        if sess is None:
            return web.json_response({"ok": _gh_token_set(), "pending": False})
        if not sess.done:
            return web.json_response(
                {
                    "ok": False,
                    "pending": True,
                    "url": sess.url,
                    "device_code": sess.device_code,
                }
            )
        await _drop_session("gh")
    # Device finished — copy host token into .env for the container.
    rc, out, err = await _run(["gh", "auth", "token"], timeout=15)
    token = out.strip()
    if rc != 0 or not _SECRET_RE.match(token):
        detail = err.strip() or "gh auth token returned nothing after login"
        return web.json_response({"ok": False, "pending": False, "error": detail[:300]}, status=400)
    async with _CONFIG_LOCK:
        upsert_env({"GH_TOKEN": token})
    host = await _gh_host_status()
    return web.json_response(
        {
            "ok": True,
            "pending": False,
            "message": "GH_TOKEN written to .env",
            "gh": {
                "token_set": True,
                "host_logged_in": host["logged_in"],
                "host_user": host.get("user") or "",
            },
        }
    )


async def h_gh_login_cancel(request: web.Request) -> web.Response:
    async with _LOGIN_LOCK:
        await _drop_session("gh")
    return web.json_response({"ok": True})


async def h_gh_token(request: web.Request) -> web.Response:
    """Write GH_TOKEN to .env so the container's gh can authenticate."""
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not _SECRET_RE.match(token):
        return web.json_response(
            {"ok": False, "error": "invalid token format"}, status=400
        )
    async with _CONFIG_LOCK:
        upsert_env({"GH_TOKEN": token})
    return web.json_response({"ok": True})


async def h_gh_token_import(request: web.Request) -> web.Response:
    """Copy the host's `gh auth token` into .env (never sent to the browser)."""
    if not shutil.which("gh"):
        return web.json_response(
            {"ok": False, "error": "gh not installed on this host"}, status=400
        )
    rc, out, err = await _run(["gh", "auth", "token"], timeout=15)
    token = out.strip()
    if rc != 0 or not _SECRET_RE.match(token):
        detail = err.strip() or "gh auth token returned nothing (run GitHub sign-in first)"
        return web.json_response({"ok": False, "error": detail[:300]}, status=400)
    async with _CONFIG_LOCK:
        upsert_env({"GH_TOKEN": token})
    return web.json_response({"ok": True})


async def h_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def h_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


INDEX_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SlackAgentTeam</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cal+Sans&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* control deck — after rare-ui: quiet monochrome surfaces, one orange signal,
   step-player tracks, and motion that only ever reports a state change */
:root{
  color-scheme:light;
  --bg:oklch(0.993 0.002 70);--surface:oklch(0.976 0.003 70);--tile:oklch(0.948 0.003 70);
  --tile-2:oklch(0.925 0.004 70);
  --ink:oklch(0.235 0.004 60);--ink-2:oklch(0.37 0.005 60);--muted:oklch(0.5 0.006 60);
  --faint:oklch(0.64 0.006 60);
  --line:oklch(0.915 0.004 70);--line-2:oklch(0.865 0.005 70);
  --accent:oklch(0.662 0.2215 36.9);--accent-ink:oklch(0.555 0.19 37);
  --accent-wash:oklch(0.662 0.2215 36.9 / 0.1);
  --good:oklch(0.6 0.13 152);--warn:oklch(0.7 0.14 72);--crit:oklch(0.59 0.2 27);
  --on-ink:var(--bg);
  --shadow-sm:0 1px 2px oklch(0.2 0.01 60 / 0.06),0 1px 3px oklch(0.2 0.01 60 / 0.07);
  --shadow-lg:0 2px 4px oklch(0.2 0.01 60 / 0.05),0 12px 32px -10px oklch(0.2 0.01 60 / 0.22);
  --apple:none;
  --r-sm:4px;--r:6px;--r-lg:10px;--r-xl:14px;--pill:999px;
  --display:"Cal Sans","Geist",system-ui,-apple-system,"PingFang SC","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
  --sans:"Geist",system-ui,-apple-system,"PingFang SC","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
  --mono:"Geist Mono",ui-monospace,"SFMono-Regular",Menlo,monospace;
  --body:var(--sans);--data:var(--mono);--disp:var(--display);
  --out:cubic-bezier(.22,1,.36,1);--out-quart:cubic-bezier(.25,1,.5,1);
  /* damped springs (bounce .18 / .28, as rare-ui's motion springs) */
  --spring:linear(0, 0.0241, 0.0847, 0.1674, 0.2614, 0.3589, 0.4544, 0.5441, 0.6259, 0.6985, 0.7617, 0.8155, 0.8605, 0.8975, 0.9273, 0.9509, 0.9692, 0.9831, 0.9933, 1.0006, 1.0055, 1.0086, 1.0103, 1.011, 1.011, 1.0105, 1.0097, 1.0087, 1.0076, 1.0065, 1.0055, 1.0046, 1.0037, 1.003, 1.0023, 1.0018, 1);
  --spring-pop:linear(0, 0.0312, 0.1093, 0.2147, 0.3325, 0.452, 0.5655, 0.6683, 0.7578, 0.833, 0.894, 0.9417, 0.9777, 1.0035, 1.0209, 1.0315, 1.0369, 1.0384, 1.0372, 1.0342, 1.0301, 1.0256, 1.021, 1.0166, 1.0127, 1.0093, 1.0064, 1.004, 1.0022, 1.0008, 0.9999, 0.9992, 0.9988, 0.9986, 0.9985, 0.9986, 1);
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --bg:oklch(0.135 0.002 60);--surface:oklch(0.168 0.003 60);--tile:oklch(0.2 0.003 60);
  --tile-2:oklch(0.235 0.004 60);
  --ink:oklch(0.935 0.003 70);--ink-2:oklch(0.82 0.004 70);--muted:oklch(0.68 0.005 70);
  --faint:oklch(0.53 0.005 70);
  --line:oklch(0.245 0.003 60);--line-2:oklch(0.315 0.004 60);
  --accent:oklch(0.662 0.2215 36.9);--accent-ink:oklch(0.73 0.17 42);
  --accent-wash:oklch(0.662 0.2215 36.9 / 0.14);
  --good:oklch(0.72 0.13 152);--warn:oklch(0.79 0.13 76);--crit:oklch(0.68 0.18 28);
  --shadow-sm:0 1px 2px oklch(0 0 0 / 0.5);--shadow-lg:0 16px 40px -12px oklch(0 0 0 / 0.7);
  --apple:inset 0 0 0 1px oklch(1 0 0 / 0.06),inset 0 1px 0 0 oklch(1 0 0 / 0.09),inset 0 -1px 0 0 oklch(0 0 0 / 0.3);
}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
  color-scheme:dark;
  --bg:oklch(0.135 0.002 60);--surface:oklch(0.168 0.003 60);--tile:oklch(0.2 0.003 60);
  --tile-2:oklch(0.235 0.004 60);
  --ink:oklch(0.935 0.003 70);--ink-2:oklch(0.82 0.004 70);--muted:oklch(0.68 0.005 70);
  --faint:oklch(0.53 0.005 70);
  --line:oklch(0.245 0.003 60);--line-2:oklch(0.315 0.004 60);
  --accent:oklch(0.662 0.2215 36.9);--accent-ink:oklch(0.73 0.17 42);
  --accent-wash:oklch(0.662 0.2215 36.9 / 0.14);
  --good:oklch(0.72 0.13 152);--warn:oklch(0.79 0.13 76);--crit:oklch(0.68 0.18 28);
  --shadow-sm:0 1px 2px oklch(0 0 0 / 0.5);--shadow-lg:0 16px 40px -12px oklch(0 0 0 / 0.7);
  --apple:inset 0 0 0 1px oklch(1 0 0 / 0.06),inset 0 1px 0 0 oklch(1 0 0 / 0.09),inset 0 -1px 0 0 oklch(0 0 0 / 0.3);
}}
*{box-sizing:border-box;margin:0}
html{-webkit-text-size-adjust:100%;scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}
body{font-family:var(--sans);background:var(--bg);color:var(--ink);font-size:15px;line-height:1.6;
  font-feature-settings:"ss01","cv11";-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--accent-wash);color:var(--ink)}
.wrap{max-width:1080px;margin:0 auto;padding:0 clamp(18px,4.5vw,44px)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--r-sm)}

/* masthead: brand, sliding tab pill, language, theme */
.top{position:sticky;top:0;z-index:20;background:color-mix(in oklch,var(--bg) 86%,transparent);
  backdrop-filter:saturate(1.3) blur(12px);-webkit-backdrop-filter:saturate(1.3) blur(12px);
  border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:18px;height:60px}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--display);font-size:1.12rem;
  letter-spacing:-.01em;white-space:nowrap}
.brand .mk{width:22px;height:22px;border-radius:7px;background:var(--ink);display:grid;place-items:center;
  box-shadow:var(--apple)}
.brand .mk::after{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 3px oklch(0.662 0.2215 36.9 / 0.22)}
.nav{margin-left:auto;display:flex;align-items:center;gap:10px}
/* gooey tabs: the selected tab lifts out of the bar, the rest close up */
.tabs{display:flex;align-items:center}
.tabs button{position:relative;appearance:none;border:0;background:var(--tile);font-family:var(--sans);font-size:.86rem;
  font-weight:500;color:var(--muted);cursor:pointer;padding:7px 14px;border-radius:0;margin:0;box-shadow:var(--apple);
  transition:margin .55s var(--spring),border-radius .55s var(--spring),background .25s var(--out),color .2s var(--out)}
.tabs button:first-of-type{border-radius:var(--pill) 0 0 var(--pill)}
.tabs button:last-of-type{border-radius:0 var(--pill) var(--pill) 0}
.tabs button:hover{color:var(--ink)}
.tabs button.on{margin:0 6px;border-radius:var(--pill);background:var(--ink);color:var(--on-ink);box-shadow:var(--shadow-sm)}
.tabs button.on:first-of-type{margin-left:0}.tabs button.on:last-of-type{margin-right:0}
.tabs button:has(+ button.on){border-top-right-radius:var(--pill);border-bottom-right-radius:var(--pill)}
.tabs button.on+button{border-top-left-radius:var(--pill);border-bottom-left-radius:var(--pill)}
.tabs button.on::before,.tabs button.on::after{content:"";position:absolute;top:22%;bottom:22%;width:8px;background:var(--tile);
  border-radius:var(--pill);animation:neck .5s var(--out) both;pointer-events:none}
.tabs button.on::before{right:100%}.tabs button.on::after{left:100%}
.tabs button.on:first-of-type::before,.tabs button.on:last-of-type::after{display:none}
@keyframes neck{from{transform:scaleY(1);opacity:1}to{transform:scaleY(0);opacity:0}}
.lang{display:inline-flex;gap:1px;padding:2px;border-radius:var(--r);border:1px solid var(--line-2)}
.lang button{appearance:none;border:0;background:transparent;font-family:var(--mono);font-size:.72rem;
  color:var(--muted);cursor:pointer;padding:4px 7px;border-radius:var(--r-sm);line-height:1.2;transition:.18s}
.lang button:hover{color:var(--ink)}
.lang button.on{background:var(--tile);color:var(--ink)}
.theme{width:32px;height:32px;border-radius:var(--r);border:1px solid var(--line-2);background:transparent;
  color:var(--muted);cursor:pointer;display:grid;place-items:center;padding:0;transition:.18s}
.theme:hover{color:var(--ink);background:var(--surface)}
.theme svg{width:16px;height:16px;grid-area:1/1;transition:transform .45s var(--out),opacity .25s}
.theme .moon{opacity:0;transform:rotate(-70deg) scale(.6)}
:root[data-theme="dark"] .theme .sun{opacity:0;transform:rotate(70deg) scale(.6)}
:root[data-theme="dark"] .theme .moon{opacity:1;transform:none}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]) .theme .sun{opacity:0;transform:rotate(70deg) scale(.6)}
  :root:not([data-theme="light"]) .theme .moon{opacity:1;transform:none}}

/* panels enter once per tab switch; live re-renders inside never replay it */
.panel{display:none}
.panel.on{display:block}
.panel.on>*{animation:rise .55s var(--out) both}
.panel.on>*:nth-child(2){animation-delay:.04s}.panel.on>*:nth-child(3){animation-delay:.08s}
.panel.on>*:nth-child(4){animation-delay:.12s}.panel.on>*:nth-child(n+5){animation-delay:.16s}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* shared type */
.kicker,.guide-kicker{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.7rem;
  letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);padding:4px 10px 4px 8px;
  border:1px solid var(--line-2);border-radius:var(--pill);background:var(--surface)}
.guide-kicker::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--accent)}
.lede{padding:clamp(34px,6vw,56px) 0 22px;border-bottom:1px solid var(--line)}
.lede h1{font-family:var(--display);font-weight:600;font-size:clamp(1.9rem,4.2vw,2.6rem);
  letter-spacing:-.02em;line-height:1.08;text-wrap:balance}
.lede .sub,.card .sub,.sub{color:var(--muted);font-size:.95rem;max-width:64ch}
.lede .sub{margin-top:8px;font-size:1rem}
.src{font-family:var(--mono);font-size:.7rem;font-weight:400;letter-spacing:0;color:var(--faint);white-space:nowrap}
.empty{color:var(--faint);font-size:.88rem;padding:12px 0}
a{color:var(--accent-ink);text-decoration:underline;text-decoration-color:color-mix(in oklch,var(--accent) 35%,transparent);
  text-underline-offset:3px}
a:hover{text-decoration-color:var(--accent)}

/* status line */
.statusline{display:flex;align-items:center;gap:12px 16px;flex-wrap:wrap;margin-top:20px}
.beacon{display:inline-flex;align-items:center;gap:8px;font-size:.82rem;font-weight:500;color:var(--ink-2);
  padding:4px 11px 4px 9px;border-radius:var(--pill);background:var(--tile);box-shadow:var(--apple)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint);position:relative}
.beacon.live .dot{background:var(--good)}
.beacon.live .dot::after{content:"";position:absolute;inset:-4px;border-radius:50%;
  border:1.5px solid var(--good);opacity:.5;animation:ping 2.2s var(--out) infinite}
.beacon.down .dot{background:var(--crit)}
@keyframes ping{0%{transform:scale(.5);opacity:.6}100%{transform:scale(1.7);opacity:0}}
.metaline{font-family:var(--mono);font-size:.74rem;color:var(--faint)}
.statusline .right{margin-left:auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap;justify-content:flex-end}
.sw{display:inline-flex;align-items:center;gap:8px;font-size:.8rem;color:var(--muted);white-space:nowrap;cursor:pointer}
.sw input{appearance:none;width:30px;height:18px;padding:0;border:0;border-radius:var(--pill);background:var(--tile-2);
  position:relative;cursor:pointer;transition:background .25s var(--out);flex:none;box-shadow:var(--apple)}
.sw input::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;
  background:var(--bg);box-shadow:var(--shadow-sm);transition:transform .45s var(--spring-pop)}
.sw input:checked{background:var(--accent)}
.sw input:checked::after{transform:translateX(12px);background:oklch(0.99 0.002 70)}
.linkbtn{appearance:none;background:transparent;border:0;font-family:var(--sans);font-size:.8rem;font-weight:500;
  color:var(--muted);cursor:pointer;padding:4px 2px;border-radius:var(--r-sm)}
.linkbtn:hover{color:var(--ink)}

/* figures: one strip, hairline cells, rolling digits */
.figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin:22px 0 4px;border:1px solid var(--line);
  border-radius:var(--r-lg);overflow:hidden;background:var(--surface)}
.fig{padding:16px 18px 14px;border-left:1px solid var(--line)}
.fig:first-child{border-left:0}
.fig .n{font-family:var(--display);font-size:clamp(1.7rem,3.4vw,2.2rem);line-height:1.2;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;display:flex}
.fig .n.hi{color:var(--accent-ink)}
.fig .l{font-family:var(--mono);font-size:.68rem;color:var(--muted);margin-top:8px;letter-spacing:.06em;text-transform:uppercase}
/* odometer: one wheel of faces per digit, faded at the window's edges */
.odo{display:inline-flex;align-items:flex-start}
.odo-col{display:inline-block;height:1.2em;line-height:1.2em;overflow:hidden;
  -webkit-mask-image:linear-gradient(to bottom,transparent 0,rgb(0 0 0 / .5) 9%,#000 20%,#000 80%,rgb(0 0 0 / .5) 91%,transparent 100%);
  mask-image:linear-gradient(to bottom,transparent 0,rgb(0 0 0 / .5) 9%,#000 20%,#000 80%,rgb(0 0 0 / .5) 91%,transparent 100%)}
.odo-wheel{display:flex;flex-direction:column;transition:transform .8s var(--spring)}
.odo-wheel span{display:block;height:1.2em;text-align:center}
.odo-mark{display:inline-block;line-height:1.2em;white-space:pre}
.odo-col.enter,.odo-mark.enter{animation:odoIn .45s var(--out) both}
@keyframes odoIn{from{opacity:0;transform:translateY(30%)}to{opacity:1;transform:none}}
.transcript-health{display:flex;gap:6px;flex-wrap:wrap;padding:14px 0;color:var(--muted);font-size:.74rem}
.transcript-health .metric{font-family:var(--mono);padding:3px 8px;border-radius:var(--r-sm);background:var(--surface);
  border:1px solid var(--line)}
.transcript-health .metric b{color:var(--ink);font-weight:500;font-variant-numeric:tabular-nums;margin-left:2px}

/* section headings inside monitor */
.seccap,.quota-title,.worktree-title{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-family:var(--display);
  font-size:1.08rem;font-weight:600;letter-spacing:-.01em;color:var(--ink);padding:22px 0 10px}
.quota-section,.worktree-section{padding:4px 0 18px;border-bottom:1px solid var(--line)}
.tasksec{padding:4px 0 14px;border-bottom:1px solid var(--line)}

/* quota */
.quota-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.quota-card{padding:16px 18px;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface);min-width:0;
  box-shadow:var(--apple)}
.quota-card .qhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.quota-card .qowner{font-family:var(--mono);color:var(--ink);font-size:.84rem}
.quota-card .qday{margin-left:auto;font-family:var(--mono);color:var(--faint);font-size:.7rem}
.quota-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 14px;margin-top:12px}
.quota-metric .v{font-family:var(--display);font-size:1.15rem;font-variant-numeric:tabular-nums;color:var(--ink)}
.quota-metric .k{font-family:var(--mono);font-size:.66rem;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
.quota-bar{height:6px;background:var(--tile);border-radius:var(--pill);overflow:hidden;margin:14px 0 10px}
.quota-bar span{display:block;height:100%;background:var(--good);border-radius:inherit;transition:width .6s var(--out)}
.quota-bar span.warn{background:var(--warn)}.quota-bar span.crit{background:var(--crit)}
.quota-breakdown{width:100%;border-collapse:collapse;font-size:.74rem;color:var(--muted);font-variant-numeric:tabular-nums}
.quota-breakdown th{text-align:left;font-weight:500;color:var(--faint);padding:5px 8px 4px 0;border-bottom:1px solid var(--line);
  font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.04em}
.quota-breakdown td{padding:6px 8px 2px 0;vertical-align:top}
.quota-breakdown th:not(:first-child),.quota-breakdown td:not(:first-child){text-align:right}
.quota-breakdown .qruntime{font-family:var(--mono);color:var(--ink-2)}

/* worktrees */
.worktree-list{display:grid;gap:8px}
.worktree-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 18px;align-items:center;padding:11px 14px;
  border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface)}
.worktree-main{min-width:0}
.worktree-branch{font-family:var(--mono);font-size:.8rem;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.worktree-meta{font-family:var(--mono);font-size:.7rem;color:var(--muted);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.worktree-actions{display:flex;align-items:center;gap:10px}
.worktree-actions .state{white-space:nowrap}

/* tracked issues */
.irow{display:grid;grid-template-columns:auto 1fr auto auto;gap:14px;align-items:center;padding:9px 2px;
  border-top:1px solid var(--line);font-size:.88rem}
.irow:first-child{border-top:0}
.inum{font-family:var(--mono);font-size:.74rem;color:var(--accent-ink);padding:2px 7px;border-radius:var(--pill);
  background:var(--accent-wash);text-decoration:none}
.ititle{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ititle small{color:var(--faint);font-family:var(--mono);font-size:.7rem}
.ilabel{font-family:var(--mono);font-size:.68rem;color:var(--ink-2);padding:2px 7px;border-radius:var(--pill);
  border:1px solid var(--line-2)}
.ilabel:empty{display:none}
.iasg{font-size:.74rem;color:var(--faint);font-family:var(--mono)}

/* agent nodes: each agent is a unit; a step-player rail shows work */
#roster{display:grid;gap:12px;padding-bottom:12px}
.node{padding:18px 20px;border:1px solid var(--line);border-radius:var(--r-xl);background:var(--surface);box-shadow:var(--apple)}
.node .head{display:flex;align-items:center;gap:10px 14px;flex-wrap:wrap}
.node .nm{font-family:var(--display);font-size:1.3rem;font-weight:600;letter-spacing:-.015em}
.state{font-size:.74rem;font-weight:500;color:var(--muted);display:inline-flex;align-items:center;gap:7px;
  padding:3px 10px 3px 8px;border-radius:var(--pill);background:var(--tile)}
.state::before{content:"";width:6px;height:6px;border-radius:var(--pill);background:var(--faint);flex:none}
.state.work{color:var(--warn);background:oklch(0.7 0.14 72 / 0.12)}
.state.work::before{width:16px;background:linear-gradient(90deg,var(--warn) 0 40%,oklch(0.7 0.14 72 / 0.35) 40% 100%);
  background-size:200% 100%;animation:rail 1.4s linear infinite}
@keyframes rail{from{background-position:100% 0}to{background-position:-100% 0}}
.state.idle{color:var(--good);background:oklch(0.6 0.13 152 / 0.12)}.state.idle::before{background:var(--good)}
.state.off{color:var(--crit);background:oklch(0.59 0.2 27 / 0.12)}.state.off::before{background:var(--crit)}
.rtline{font-family:var(--mono);font-size:.76rem;color:var(--ink-2)}
.node .aux{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint)}

/* control drawer */
.ctl{display:grid;grid-template-columns:auto 1fr auto;gap:12px 18px;align-items:center;margin-top:16px;padding:16px 18px;
  background:var(--bg);border-radius:var(--r-lg);border:1px solid var(--line)}
.ctl .lbl,.lbl{font-family:var(--mono);font-size:.68rem;color:var(--muted);letter-spacing:.05em;text-transform:uppercase}
.field{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
.field .model-custom{min-width:10rem;flex:1;max-width:16rem}
select,input,textarea{font-family:var(--sans);font-size:.9rem;color:var(--ink);background:var(--bg);
  border:1px solid var(--line-2);border-radius:var(--r);padding:8px 11px;width:100%;
  transition:border-color .18s,box-shadow .18s}
select:hover,input:hover,textarea:hover{border-color:color-mix(in oklch,var(--line-2) 60%,var(--muted))}
select:focus,input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-wash)}
select{cursor:pointer;appearance:none;padding-right:32px;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),
  linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 15px) 52%,calc(100% - 10px) 52%;
  background-size:5px 5px;background-repeat:no-repeat}
textarea{line-height:1.55;resize:vertical}
.seg{display:inline-flex;gap:2px;padding:3px;border-radius:var(--r);background:var(--tile);box-shadow:var(--apple);justify-self:start}
.rt{appearance:none;font-family:var(--mono);font-size:.76rem;color:var(--muted);border:0;background:transparent;
  border-radius:var(--r-sm);padding:6px 12px;cursor:pointer;transition:color .18s,background .18s}
.rt:hover{color:var(--ink)}
.rt.on{color:var(--ink);background:var(--bg);box-shadow:var(--shadow-sm),var(--apple)}
.confirm{grid-column:1/-1;display:none;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 14px;
  border-radius:var(--r);background:var(--accent-wash)}
.confirm.fresh{animation:armIn .5s var(--spring) both}
@keyframes armIn{from{opacity:0;transform:translateX(14px) scaleX(.97)}to{opacity:1;transform:none}}
/* in-place confirm (rare-ui delete-button): no dialog, Escape backs out */
.arm-slot{grid-column:1/-1;min-width:0}
.arm-slot:empty{display:none}
.confirm.arm{background:oklch(0.59 0.2 27 / 0.08);border:1px solid oklch(0.59 0.2 27 / 0.22);transform-origin:right center}
.confirm.arm.leaving{animation:armOut .2s var(--out) both}
@keyframes armOut{to{opacity:0;transform:translateX(10px)}}
.arm-btn{appearance:none;width:32px;height:32px;padding:0;border-radius:var(--r);display:grid;place-items:center;cursor:pointer;
  border:1px solid var(--line-2);background:var(--bg);color:var(--ink);transition:transform .45s var(--spring-pop),background .2s}
.arm-btn:hover{transform:scale(1.06)}.arm-btn:active{transform:scale(.9)}
.arm-btn svg{width:15px;height:15px}
.arm-btn.yes{background:var(--crit);border-color:var(--crit);color:oklch(0.99 0.002 70)}
.arm-btn.yes path{stroke-dasharray:1;stroke-dashoffset:0}
.confirm.arm.confirmed .arm-btn.yes{transform:scale(1.12)}
.confirm.arm.confirmed .arm-btn.yes path{animation:draw .38s var(--out) both}
.confirm.arm.confirmed .arm-btn.no{opacity:.35}
@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
.worktree-row .arm-slot{grid-column:1/-1}
.confirm.on{display:flex}
.confirm .q{color:var(--ink);font-size:.9rem}
.confirm .q b{font-weight:600;color:var(--accent-ink)}
.spacer{flex:1}

/* buttons */
.btn{appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:7px;font-family:var(--sans);
  font-size:.84rem;font-weight:500;cursor:pointer;border-radius:var(--r);padding:8px 14px;border:1px solid transparent;
  line-height:1.2;white-space:nowrap;text-decoration:none;
  transition:background .18s,color .18s,border-color .18s,transform .15s var(--out-quart)}
.btn:active{transform:scale(.97)}
.btn.solid{background:var(--ink);color:var(--on-ink);box-shadow:var(--shadow-sm),var(--apple)}
.btn.solid:hover{background:color-mix(in oklch,var(--ink) 86%,var(--accent))}
.btn.line{background:var(--bg);color:var(--ink);border-color:var(--line-2);box-shadow:var(--shadow-sm)}
.btn.line:hover{background:var(--surface)}
.btn.text{background:transparent;color:var(--muted);padding:6px 10px}
.btn.text:hover{color:var(--ink);background:var(--tile)}
.btn:disabled{opacity:.45;cursor:default;transform:none}
.rowbtns{display:flex;gap:8px}

/* thread readout */
.threads{margin-top:14px}
.tcap{font-family:var(--mono);font-size:.68rem;color:var(--faint);margin-bottom:4px;letter-spacing:.04em;text-transform:uppercase}
.trow{display:grid;grid-template-columns:1fr auto auto auto;gap:16px;align-items:baseline;padding:7px 0;
  border-top:1px solid var(--line);font-size:.8rem}
.trow .tid{color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono);font-size:.76rem}
.trow .run{color:var(--warn);font-weight:500}.trow .wait{color:var(--faint)}
.trow .num{color:var(--muted);font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:.74rem}

/* toast */
.toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,14px) scale(.98);opacity:0;background:var(--ink);
  color:var(--on-ink);font-size:.86rem;font-weight:500;padding:11px 18px;border-radius:var(--r-lg);pointer-events:none;
  transition:opacity .3s var(--out),transform .4s var(--out);z-index:40;box-shadow:var(--shadow-lg),var(--apple);max-width:min(92vw,520px)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast.err{background:var(--crit);color:oklch(0.99 0.002 70)}

/* config / auth: one card per entity */
.note{color:var(--muted);font-size:.92rem;margin:24px 0 8px;max-width:64ch}
.card{padding:20px 22px;margin-top:14px;border:1px solid var(--line);border-radius:var(--r-xl);background:var(--surface);
  box-shadow:var(--apple)}
#agents .card:first-child{margin-top:14px}
.card .head{display:flex;align-items:center;gap:10px 12px;flex-wrap:wrap}
.card .nm{font-family:var(--display);font-size:1.2rem;font-weight:600;letter-spacing:-.015em}
.chip{font-family:var(--mono);font-size:.7rem;color:var(--ink-2);padding:2px 8px;border-radius:var(--pill);background:var(--tile)}
.tok{font-family:var(--mono);font-size:.74rem;color:var(--faint)}
.ok{color:var(--good)}.ng{color:var(--crit)}
.persona{color:var(--ink-2);font-size:.92rem;margin-top:8px;max-width:72ch}
.hint{color:var(--muted);font-size:.8rem;margin:4px 0 8px;max-width:72ch}
textarea.warn{border-color:var(--warn)!important;box-shadow:0 0 0 3px oklch(0.7 0.14 72 / 0.16)!important}
.cardlong{color:var(--warn);font-size:.78rem;margin:4px 0 0;display:none}
.cardlong.on{display:block}
.wiz{display:none;margin-top:16px;padding:18px;background:var(--bg);border-radius:var(--r-lg);border:1px solid var(--line)}
.wiz.on{display:block;animation:rise .35s var(--out) both}
.wiz p{color:var(--ink-2);font-size:.9rem;margin:12px 0 6px}
.wiz .lbl{display:block;margin:14px 0 6px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;max-height:260px;overflow:auto;
  font-size:.76rem;font-family:var(--mono);line-height:1.65;color:var(--ink-2)}
.msg{font-size:.86rem;margin-top:10px;white-space:pre-wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid2 .lbl{display:block;margin-bottom:6px}
.foot{padding:44px 0 24px;color:var(--faint);font-family:var(--mono);font-size:.7rem;letter-spacing:.06em;text-align:center;
  text-transform:uppercase}
/* local workspace row on each agent card */
.wsrow{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.wspath{font-family:var(--mono);font-size:.8rem;color:var(--ink);overflow-wrap:anywhere}
.wspath .faint{color:var(--faint)}
.wsedit{margin-top:12px;padding:16px;border:1px solid var(--line);border-radius:var(--r-lg);background:var(--bg)}
.wsedit[hidden]{display:none}
.wsedit.open{animation:rise .35s var(--out) both}
.wsinfo{display:flex;align-items:center;gap:6px 10px;flex-wrap:wrap;font-family:var(--mono);font-size:.74rem;color:var(--muted);margin-top:12px}
.wsinfo:empty{display:none}
.wsinfo .warn{color:var(--warn)}.wsinfo .ng{color:var(--crit)}.wsinfo .ok{color:var(--good)}
/* control token: asked in place, only after the server said it needs one */
.unlock{display:none;margin-top:18px;padding:16px 18px;border-radius:var(--r-lg);background:var(--accent-wash);
  border:1px solid color-mix(in oklch,var(--accent) 30%,var(--line))}
.unlock.on{display:block;animation:armIn .5s var(--spring) both}
.unlock .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.unlock .row input{flex:1;min-width:220px;max-width:420px}
.unlock b{font-family:var(--display);font-weight:600;font-size:1rem}
.unlock .msg{margin-top:8px}
#cx-code,#gh-code{font-family:var(--mono)!important;font-size:1.4rem!important;letter-spacing:.18em!important;
  color:var(--ink)!important;font-weight:600!important;display:inline-block;padding:8px 14px;border-radius:var(--r);
  background:var(--tile);box-shadow:var(--apple)}

/* guide: an operational runbook with a step-player track */
.guide-hero{padding:clamp(40px,8vw,84px) 0 30px;display:grid;grid-template-columns:minmax(0,1.5fr) minmax(240px,.8fr);
  gap:28px 48px;align-items:end;border-bottom:1px solid var(--line)}
.guide-hero>div:first-child{grid-column:1/-1;justify-self:start}
.guide-hero h1{grid-column:1;font-family:var(--display);font-weight:600;font-size:clamp(2.4rem,6vw,4.25rem);
  line-height:1.02;letter-spacing:-.03em;max-width:16ch;text-wrap:balance}
.guide-hero .sub{grid-column:1;color:var(--muted);font-size:1.06rem;max-width:56ch;margin-top:-10px}
.guide-boundary{grid-column:2;grid-row:2/4;align-self:end;display:flex;align-items:flex-start;gap:12px;
  padding:16px 18px;border-radius:var(--r-lg);background:var(--surface);border:1px solid var(--line);
  color:var(--ink-2);font-size:.86rem;line-height:1.55;box-shadow:var(--apple)}
.guide-boundary .seal{flex:none;width:22px;height:22px;border-radius:var(--r);display:grid;place-items:center;
  font-size:.78rem;color:var(--accent-ink);background:var(--accent-wash)}
.guide-progress{display:grid;grid-template-columns:auto minmax(0,1fr);gap:18px 32px;align-items:center;padding:24px 0;
  border-bottom:1px solid var(--line)}
.guide-progress .count{font-family:var(--display);font-size:2.1rem;line-height:1;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:6px}
.guide-progress .count .of{font-size:1rem;color:var(--faint)}
.guide-progress .count #guide-count{display:inline-flex;overflow:hidden;height:1.05em}
.guide-progress .caption{font-family:var(--mono);font-size:.66rem;color:var(--muted);letter-spacing:.06em;
  text-transform:uppercase;margin-top:8px}
.guide-progress .next{font-size:.92rem;color:var(--ink-2);margin-bottom:10px}
.guide-track{display:flex;gap:6px;padding:7px 9px;border-radius:var(--pill);background:var(--tile);box-shadow:var(--apple);
  max-width:420px}
.gseg{position:relative;flex:1;height:6px;border-radius:var(--pill);background:var(--tile-2);overflow:hidden}
.gseg::after{content:"";position:absolute;inset:0;border-radius:inherit;background:var(--ink);transform:scaleX(0);
  transform-origin:left center;transition:transform .5s var(--out)}
.gseg.done::after{transform:scaleX(1)}
.gseg.current::after{background:var(--accent);transform:scaleX(.32);animation:breathe 2.4s var(--out) infinite alternate}
@keyframes breathe{from{transform:scaleX(.14)}to{transform:scaleX(.42)}}
.guide-map{display:flex;align-items:center;gap:0;padding:22px 0;border-bottom:1px solid var(--line);font-size:.8rem}
.guide-map .stop{padding:7px 13px;border-radius:var(--pill);border:1px solid var(--line-2);color:var(--ink);
  background:var(--bg);white-space:nowrap;box-shadow:var(--shadow-sm)}
.guide-map .arrow{flex:1;min-width:34px;height:1px;margin:0 6px;position:relative;font-size:0;
  background:repeating-linear-gradient(90deg,var(--line-2) 0 4px,transparent 4px 8px)}
.guide-map .arrow::after{content:"";position:absolute;top:-2.5px;left:0;width:6px;height:6px;border-radius:50%;
  background:var(--accent);animation:travel 3.2s var(--out) infinite}
.guide-map .arrow:nth-of-type(4)::after{animation-delay:1.1s}
@keyframes travel{0%{left:0;opacity:0}15%{opacity:1}85%{opacity:1}100%{left:calc(100% - 6px);opacity:0}}
.runbook{list-style:none;padding:0;margin-top:8px}
.guide-step{position:relative;display:grid;grid-template-columns:52px minmax(0,1fr);gap:16px 24px;padding:clamp(26px,4.5vw,40px) 0}
.guide-step+.guide-step{border-top:1px solid var(--line)}
.guide-step .index{width:34px;height:34px;border-radius:var(--r-lg);display:grid;place-items:center;font-family:var(--mono);
  font-size:.78rem;font-weight:500;color:var(--ink-2);background:var(--tile);box-shadow:var(--apple);
  transition:background .3s var(--out),color .3s}
.guide-step.done .index{background:var(--ink);color:var(--on-ink)}
.guide-step.ticking.done .index{animation:pop .55s var(--spring-pop) both}
@keyframes pop{from{transform:scale(.78)}to{transform:none}}
.guide-step-head{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
.guide-step h2{font-family:var(--display);font-weight:600;font-size:clamp(1.4rem,2.6vw,1.75rem);line-height:1.12;letter-spacing:-.02em}
.guide-step .eyebrow{display:block;font-family:var(--mono);color:var(--accent-ink);font-size:.66rem;letter-spacing:.1em;
  text-transform:uppercase;margin-bottom:8px}
.guide-step .body{color:var(--ink-2);max-width:66ch;margin-top:10px}
.guide-done{margin-left:auto;display:inline-flex;align-items:center;gap:9px;color:var(--ink-2);font-size:.8rem;font-weight:500;
  cursor:pointer;white-space:nowrap;padding:6px 12px 6px 8px;border-radius:var(--pill);border:1px solid var(--line-2);
  background:var(--bg);transition:border-color .2s,background .2s}
.guide-done:hover{background:var(--surface)}
.guide-done input{appearance:none;width:18px;height:18px;padding:0;margin:0;border:1.5px solid var(--line-2);border-radius:50%;
  display:grid;place-items:center;cursor:pointer;
  transition:background .3s var(--out),border-color .25s,transform .5s var(--spring-pop)}
.guide-done input:active{transform:scale(.86)}
.guide-done input::after{content:"";width:9px;height:4.5px;border:2px solid oklch(0.99 0.002 70);border-top:0;border-right:0;
  transform:translate(.5px,-1px) rotate(-45deg);clip-path:inset(0 100% 0 0);transition:clip-path .34s var(--out) .06s}
.guide-done input:checked{background:var(--accent);border-color:var(--accent)}
.guide-done input:checked::after{clip-path:inset(0 0 0 0)}
.guide-step.done .guide-done{border-color:color-mix(in oklch,var(--accent) 40%,var(--line-2));color:var(--accent-ink)}
.guide-evidence{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:16px 28px;margin-top:20px;padding:16px 18px;
  border-radius:var(--r-lg);background:var(--surface);border:1px solid var(--line);align-items:start}
.guide-evidence h3{font-family:var(--mono);font-size:.66rem;color:var(--muted);font-weight:500;letter-spacing:.08em;
  text-transform:uppercase;margin-bottom:8px}
.guide-evidence ul{list-style:none;padding:0;color:var(--ink-2);font-size:.88rem}
/* task-list: tick, then strike, then a small nudge; staggered per item */
.guide-evidence li{position:relative;padding-left:18px;--i:0}
.guide-evidence li:nth-child(2){--i:1}.guide-evidence li:nth-child(3){--i:2}.guide-evidence li:nth-child(4){--i:3}
.guide-evidence li::before{content:"";position:absolute;left:2px;top:.62em;width:6px;height:6px;border-radius:2px;
  border:1.5px solid var(--faint);transition:background .2s,border-color .2s,transform .45s var(--spring-pop);
  transition-delay:calc(var(--i) * 70ms)}
.guide-step.done .guide-evidence li::before{background:var(--accent);border-color:var(--accent)}
.guide-step.ticking.done .guide-evidence li::before{transform:scale(1.35)}
.guide-evidence li>span{background:linear-gradient(currentColor,currentColor) 0 58%/0 1px no-repeat;
  -webkit-box-decoration-break:clone;box-decoration-break:clone;
  transition:background-size .42s var(--out),color .3s var(--out);transition-delay:calc(var(--i) * 70ms + 120ms)}
.guide-step.done .guide-evidence li>span{background-size:100% 1px;color:var(--muted)}
.guide-step.ticking.done .guide-evidence li{animation:nudge .55s var(--spring-pop) both;animation-delay:calc(var(--i) * 70ms + 420ms)}
@keyframes nudge{0%{transform:none}35%{transform:translateX(3px)}100%{transform:none}}
.guide-evidence li+li{margin-top:5px}
.guide-actions{display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.prompt-stack{margin-top:18px;display:grid;gap:8px}
.prompt-row{border:1px solid var(--line);border-radius:var(--r-lg);background:var(--surface);overflow:hidden}
.prompt-row summary{display:flex;align-items:center;gap:10px;cursor:pointer;padding:9px 10px 9px 14px;color:var(--ink);
  font-size:.86rem;font-weight:500;list-style:none}
.prompt-row summary::-webkit-details-marker{display:none}
.prompt-row summary::before{content:"";width:7px;height:7px;border:1.5px solid var(--muted);border-top:0;border-left:0;
  transform:rotate(-45deg);transition:transform .3s var(--out);margin-right:2px;flex:none}
.prompt-row[open] summary::before{transform:rotate(45deg) translate(-2px,-1px)}
.prompt-row summary .btn{margin-left:auto;font-family:var(--mono);font-size:.72rem;padding:5px 10px;border:1px solid var(--line-2);
  background:var(--bg)}
.prompt-row summary .btn.copied{color:var(--good);border-color:color-mix(in oklch,var(--good) 45%,var(--line-2))}
/* copy springs into a check (rare-ui code-block) */
.copy-btn .ic-wrap{display:grid;width:13px;height:13px;flex:none}
.copy-btn .ic-wrap svg{grid-area:1/1;width:13px;height:13px;transition:transform .5s var(--spring-pop),opacity .18s}
.copy-btn .ic-check{opacity:0;transform:scale(.3) rotate(-20deg)}
.copy-btn .ic-check path{stroke-dasharray:1;stroke-dashoffset:1;transition:stroke-dashoffset .36s var(--out) .1s}
.copy-btn.copied .ic-copy{opacity:0;transform:scale(.3)}
.copy-btn.copied .ic-check{opacity:1;transform:none}
.copy-btn.copied .ic-check path{stroke-dashoffset:0}
.copy-btn.copied{color:var(--good)}
.prompt-row pre{max-height:none;margin:0;border:0;border-top:1px solid var(--line);border-radius:0;background:var(--bg);
  white-space:pre-wrap}
.agent-guide{width:100%;border-collapse:separate;border-spacing:0;margin-top:20px;font-size:.86rem;border:1px solid var(--line);
  border-radius:var(--r-lg);overflow:hidden}
.agent-guide th{text-align:left;color:var(--muted);font-weight:500;font-family:var(--mono);font-size:.66rem;letter-spacing:.06em;
  text-transform:uppercase;padding:9px 14px;background:var(--surface);border-bottom:1px solid var(--line)}
.agent-guide td{vertical-align:top;padding:12px 14px;border-bottom:1px solid var(--line);color:var(--ink-2)}
.agent-guide tr:last-child td{border-bottom:0}
.agent-guide td:first-child{font-family:var(--mono);font-size:.82rem;color:var(--ink);font-weight:500}
.agent-guide .runtime{font-family:var(--mono);font-size:.76rem;color:var(--accent-ink);white-space:nowrap}
.agent-guide .never{color:var(--muted)}

@media(max-width:760px){
  .top .wrap{height:auto;min-height:56px;flex-wrap:wrap;gap:8px 10px;padding-top:10px;padding-bottom:10px}
  .nav{margin-left:0;width:100%;justify-content:space-between}
  .tabs button{padding:6px 11px;font-size:.82rem}
  .figs{grid-template-columns:repeat(2,minmax(0,1fr))}
  .fig:nth-child(3){border-left:0}.fig:nth-child(n+3){border-top:1px solid var(--line)}
  .ctl{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}
  .trow{grid-template-columns:1fr auto}.node .aux{margin-left:0;width:100%}
  .irow{grid-template-columns:auto 1fr}.irow .iasg,.irow .ilabel{grid-column:2}
  .worktree-row{grid-template-columns:1fr}.worktree-actions{justify-content:space-between}
  .quota-list{grid-template-columns:1fr}.quota-metrics{grid-template-columns:repeat(2,1fr)}
  .guide-hero{grid-template-columns:1fr}.guide-hero .sub,.guide-boundary{grid-column:1;grid-row:auto}
  .guide-hero .sub{margin-top:0}
  .guide-progress{grid-template-columns:1fr;gap:14px}
  .guide-map{flex-direction:column;align-items:flex-start}
  .guide-map .arrow{width:1px;height:22px;min-width:0;flex:none;margin:4px 0 4px 18px;
    background:repeating-linear-gradient(180deg,var(--line-2) 0 4px,transparent 4px 8px)}
  .guide-map .arrow::after{display:none}
  .guide-step{grid-template-columns:38px minmax(0,1fr);gap:12px 14px}
  .guide-step-head{display:block}.guide-done{margin:14px 0 0}
  .guide-evidence{grid-template-columns:1fr}.guide-actions{justify-content:flex-start}
  .agent-guide,.agent-guide tbody,.agent-guide tr,.agent-guide td{display:block;width:100%}
  .agent-guide{border:0;border-radius:0}
  .agent-guide thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  .agent-guide tr{padding:14px 16px;margin-bottom:8px;border:1px solid var(--line);border-radius:var(--r-lg)}
  .agent-guide td{position:relative;border:0;padding:5px 0 5px 92px;min-height:28px}
  .agent-guide td:first-child{padding:0 0 8px;font-size:.92rem}
  .agent-guide td:not(:first-child)::before{content:attr(data-guide-label);position:absolute;left:0;top:7px;width:84px;
    color:var(--faint);font-family:var(--mono);font-size:.62rem;letter-spacing:.05em;text-transform:uppercase}
  .agent-guide .runtime{white-space:normal}
}
/* first paint shows saved state without replaying state-change motion */
.booting *,.booting *::before,.booting *::after{transition:none!important}
.booting .tabs button::before,.booting .tabs button::after{animation:none!important}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top"><div class="wrap">
  <div class="brand"><span class="mk"></span>SlackAgentTeam</div>
  <nav class="nav">
    <div class="tabs" id="tabs">
    <button id="tab-guide" class="on" onclick="showTab('guide')" data-i18n="nav.guide">指引</button>
    <button id="tab-mon" onclick="showTab('mon')" data-i18n="nav.mon">監視</button>
    <button id="tab-cfg" onclick="showTab('cfg')" data-i18n="nav.cfg">構成</button>
    <button id="tab-auth" onclick="showTab('auth')" data-i18n="nav.auth">認証</button>
    </div>
    <span class="lang" id="lang">
      <button data-lang="zh" onclick="setLang('zh')">中</button>
      <button data-lang="ja" onclick="setLang('ja')">日</button>
      <button data-lang="en" onclick="setLang('en')">EN</button>
    </span>
    <button class="theme" id="themebtn" title="theme" aria-label="theme" onclick="toggleTheme()">
      <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4L6 18M18 6l1.4-1.4"/></svg>
      <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>
    </button>
  </nav>
</div></header>

<main class="wrap">
  <div class="unlock" id="unlock" role="region" aria-labelledby="unlock-title">
    <b id="unlock-title" data-i18n="unlock.title">需要控制令牌</b>
    <div class="sub" data-i18n="unlock.hint">这个节点启用了控制认证。</div>
    <div class="row">
      <input id="unlock-token" type="password" autocomplete="off" spellcheck="false" placeholder="Bearer token"
        onkeydown="if(event.key==='Enter')unlockControl()">
      <button class="btn solid" onclick="unlockControl()" data-i18n="unlock.save">解锁</button>
    </div>
    <div class="msg ng" id="unlock-msg"></div>
  </div>
  <section id="panel-guide" class="panel on">
    <div class="guide-hero">
      <div class="guide-kicker" data-i18n="guide.kicker">START HERE · OWNER RUNBOOK</div>
      <h1 data-i18n="guide.title">从本机账号，到第一次安全交接</h1>
      <div class="sub" data-i18n="guide.sub">按顺序完成五步。每个人只配置自己的节点；团队通过 Slack 协调，通过 GitHub 交付代码。</div>
      <div class="guide-boundary"><span class="seal">◎</span>
        <span data-i18n="guide.boundary">只共享无凭据 roster、Slack 消息与 PR URL。不要共享 AI 登录、API Key、Slack Token、GitHub Token、状态库或工作区。</span></div>
    </div>

    <div class="guide-progress">
      <div>
        <div class="count"><span id="guide-count">0</span><span class="of">/ 5</span></div>
        <div class="caption" data-i18n="guide.progress">本浏览器的上手进度</div>
      </div>
      <div>
        <div class="next" id="guide-next"></div>
        <div class="guide-track" role="progressbar" aria-valuemin="0" aria-valuemax="5"
             aria-valuenow="0" id="guide-progress">
          <span class="gseg" data-gseg="roles"></span><span class="gseg" data-gseg="slack"></span>
          <span class="gseg" data-gseg="local"></span><span class="gseg" data-gseg="prompts"></span>
          <span class="gseg" data-gseg="first-task"></span></div>
      </div>
    </div>

    <div class="guide-map" aria-label="SlackAgentTeam trust boundary">
      <span class="stop" data-i18n="guide.map.slack">共享 Slack 线程</span>
      <span class="arrow">→</span>
      <span class="stop" data-i18n="guide.map.local">每人独立本地节点</span>
      <span class="arrow">→</span>
      <span class="stop" data-i18n="guide.map.artifact">GitHub PR / Issue</span>
    </div>

    <ol class="runbook">
      <li class="guide-step" data-guide-step="roles">
        <div class="index">01</div>
        <div>
          <div class="guide-step-head">
            <div><span class="eyebrow" data-i18n="guide.roles.eye">TEAM CONTRACT</span>
              <h2 data-i18n="guide.roles.title">先分清谁负责什么</h2></div>
            <label class="guide-done"><input type="checkbox" data-guide-check="roles">
              <span data-i18n="guide.done">标记完成</span></label>
          </div>
          <p class="body" data-i18n="guide.roles.body">每人保留 2–3 个 agent。developer 写代码，reviewer 独立审查，planner/pm 只拆任务；OpenAI agent 默认没有本地文件工具。</p>
          <table class="agent-guide">
            <thead><tr><th data-i18n="guide.agent.role">角色</th><th>runtime</th>
              <th data-i18n="guide.agent.must">必须做到</th><th data-i18n="guide.agent.never">不要做</th></tr></thead>
            <tbody>
              <tr><td>developer</td><td class="runtime" data-guide-label-key="lbl.runtime">Claude / Codex</td>
                <td data-guide-label-key="guide.agent.must" data-i18n="guide.dev.must">确认完成标准；实现并测试；commit、push、贴 PR；只交给一个 reviewer。</td>
                <td class="never" data-guide-label-key="guide.agent.never" data-i18n="guide.dev.never">不要在别人的节点登录；不要把未 push 的本地路径当成交付物。</td></tr>
              <tr><td>reviewer</td><td class="runtime" data-guide-label-key="lbl.runtime">不同模型优先</td>
                <td data-guide-label-key="guide.agent.must" data-i18n="guide.reviewer.must">独立查看 PR/diff；按严重度和文件行号报告；明确通过或退回。</td>
                <td class="never" data-guide-label-key="guide.agent.never" data-i18n="guide.reviewer.never">未经要求不要直接改代码；不要用实现者结论代替验证。</td></tr>
              <tr><td>planner / pm</td><td class="runtime" data-guide-label-key="lbl.runtime">OpenAI</td>
                <td data-guide-label-key="guide.agent.must" data-i18n="guide.planner.must">澄清目标、约束、完成标准和单一下一位 agent。</td>
                <td class="never" data-guide-label-key="guide.agent.never" data-i18n="guide.planner.never">不要声称读过本地仓库；不要同时 fan-out 给多个 agent。</td></tr>
              <tr><td>OpenAI agent</td><td class="runtime" data-guide-label-key="lbl.runtime">Responses API</td>
                <td data-guide-label-key="guide.agent.must" data-i18n="guide.openai.must">处理 Slack 中可见的文本、分析和评审；需要文件时 handoff。</td>
                <td class="never" data-guide-label-key="guide.agent.never" data-i18n="guide.openai.never">不要假装拥有 Bash、git、gh 或本地文件访问。</td></tr>
            </tbody>
          </table>
          <div class="guide-evidence">
            <div><h3 data-i18n="guide.evidence">完成证据</h3>
              <ul><li><span data-i18n="guide.roles.ev1">roster.yaml 中每个 agent 都有唯一 owner 与 node_id</span></li>
                <li><span data-i18n="guide.roles.ev2">每个人的本地配置只列自己的 2–3 个 runtime</span></li></ul></div>
            <div class="guide-actions"><button class="btn line" onclick="showTab('cfg')" data-i18n="guide.goto.cfg">去团队构成</button></div>
          </div>
        </div>
      </li>

      <li class="guide-step" data-guide-step="slack">
        <div class="index">02</div>
        <div>
          <div class="guide-step-head">
            <div><span class="eyebrow" data-i18n="guide.slack.eye">SLACK SURFACE</span>
              <h2 data-i18n="guide.slack.title">一个 agent，一个 Slack App</h2></div>
            <label class="guide-done"><input type="checkbox" data-guide-check="slack">
              <span data-i18n="guide.done">标记完成</span></label>
          </div>
          <p class="body" data-i18n="guide.slack.body">从 manifest 创建 App，安装到 workspace，保存 xoxb/xapp，并把所有 bot 邀请进项目频道。scope 变化后必须重新安装。</p>
          <div class="guide-evidence">
            <div><h3 data-i18n="guide.do">按这个顺序</h3>
              <ul><li><span data-i18n="guide.slack.do1">在「团队构成」为本机每个 agent 复制 manifest</span></li>
                <li><span data-i18n="guide.slack.do2">Install to Workspace，粘贴并验证 Bot/App Token</span></li>
                <li><span data-i18n="guide.slack.do3">邀请全部本地与远端 bot 进入共享项目频道</span></li>
                <li><span data-i18n="guide.slack.do5">也可以在 Claude Code 里运行 /slack-app-setup，由 agent 操作浏览器完成，只在关键步骤请你确认</span></li>
                <li><span data-i18n="guide.slack.do4">把频道规则贴到 topic/说明：一任务一线程、一次只叫一个 agent</span></li></ul></div>
            <div class="guide-actions"><a class="btn line" href="https://api.slack.com/apps" target="_blank" rel="noopener noreferrer"><span data-i18n="guide.open.slack">打开 api.slack.com/apps</span> ↗</a>
              <button class="btn line" onclick="showTab('cfg')" data-i18n="guide.goto.slack">去设置 Slack App</button></div>
          </div>
          <div class="prompt-stack">
            <details class="prompt-row"><summary><span data-i18n="guide.prompt.channel">Slack 频道规则模板</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('channel')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="channel"></pre></details>
          </div>
        </div>
      </li>

      <li class="guide-step" data-guide-step="local">
        <div class="index">03</div>
        <div>
          <div class="guide-step-head">
            <div><span class="eyebrow" data-i18n="guide.local.eye">PRIVATE NODE</span>
              <h2 data-i18n="guide.local.title">只连接这台机器自己的账号</h2></div>
            <label class="guide-done"><input type="checkbox" data-guide-check="local">
              <span data-i18n="guide.done">标记完成</span></label>
          </div>
          <p class="body" data-i18n="guide.local.body">登录本人 Claude/Codex/GitHub，OpenAI Key 只放本机 env；创建独立 workspace、state 和 worktree volume。绝不挂载另一位 owner 的认证目录。</p>
          <div class="guide-evidence">
            <div><h3 data-i18n="guide.evidence">完成证据</h3>
              <ul><li><span data-i18n="guide.local.ev1">「认证」显示所选 runtime 与 GitHub 已验证</span></li>
                <li><span data-i18n="guide.local.ev2">本机 env 只含本人 Slack Token、控制 Bearer 和 AI Key</span></li>
                <li><span data-i18n="guide.local.ev3">仓库 origin 与 agent 的 canonical OWNER/REPO 一致</span></li></ul></div>
            <div class="guide-actions"><button class="btn solid" onclick="showTab('auth')" data-i18n="guide.goto.auth">去认证</button>
              <button class="btn line" onclick="showTab('cfg')" data-i18n="guide.goto.cfg">去团队构成</button></div>
          </div>
        </div>
      </li>

      <li class="guide-step" data-guide-step="prompts">
        <div class="index">04</div>
        <div>
          <div class="guide-step-head">
            <div><span class="eyebrow" data-i18n="guide.prompts.eye">PROMPT CONTRACT</span>
              <h2 data-i18n="guide.prompts.title">Prompt 写职责，不写口号</h2></div>
            <label class="guide-done"><input type="checkbox" data-guide-check="prompts">
              <span data-i18n="guide.done">标记完成</span></label>
          </div>
          <p class="body" data-i18n="guide.prompts.body">card 让队友知道何时找它、交什么、拿回什么；persona 约束它如何工作。推荐模板可复制后按项目改写。</p>
          <div class="prompt-stack">
            <details class="prompt-row"><summary><span data-i18n="guide.prompt.dev">developer persona 推荐</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('dev')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="dev"></pre></details>
            <details class="prompt-row"><summary><span data-i18n="guide.prompt.reviewer">reviewer persona 推荐</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('reviewer')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="reviewer"></pre></details>
            <details class="prompt-row"><summary><span data-i18n="guide.prompt.planner">planner / pm persona 推荐</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('planner')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="planner"></pre></details>
          </div>
          <div class="guide-evidence">
            <div><h3 data-i18n="guide.evidence">完成证据</h3>
              <ul><li><span data-i18n="guide.prompts.ev1">每个 card 能在 6 行内说清输入、输出和禁区</span></li>
                <li><span data-i18n="guide.prompts.ev2">reviewer 与 developer 使用独立验证标准，OpenAI persona 明示无本地工具</span></li></ul></div>
            <div class="guide-actions"><button class="btn line" onclick="showTab('cfg')" data-i18n="guide.goto.prompt">去编辑 Prompt</button></div>
          </div>
        </div>
      </li>

      <li class="guide-step" data-guide-step="first-task">
        <div class="index">05</div>
        <div>
          <div class="guide-step-head">
            <div><span class="eyebrow" data-i18n="guide.task.eye">FIRST LIVE LOOP</span>
              <h2 data-i18n="guide.task.title">在一个 Slack 线程跑完整闭环</h2></div>
            <label class="guide-done"><input type="checkbox" data-guide-check="first-task">
              <span data-i18n="guide.done">标记完成</span></label>
          </div>
          <p class="body" data-i18n="guide.task.body">人类给一个明确任务；developer 在本地 worktree 实现并产出 PR；reviewer 独立验证。跨机器只传 Slack 上下文和 durable artifact。</p>
          <div class="prompt-stack">
            <details class="prompt-row" open><summary><span data-i18n="guide.prompt.task">人类启动任务模板</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('task')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="task"></pre></details>
            <details class="prompt-row"><summary><span data-i18n="guide.prompt.handoff">结构化 HANDOFF 模板</span>
              <button class="btn text copy-btn" onclick="event.preventDefault();copyGuidePrompt('handoff')"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="guide.copy">复制</span></button></summary>
              <pre data-guide-prompt="handoff"></pre></details>
          </div>
          <div class="guide-evidence">
            <div><h3 data-i18n="guide.evidence">完成证据</h3>
              <ul><li><span data-i18n="guide.task.ev1">同一个根线程里能看到目标、测试结果、PR URL 和单目标 handoff</span></li>
                <li><span data-i18n="guide.task.ev2">reviewer 给出明确通过，或带严重度与文件行号的退回意见</span></li></ul></div>
            <div class="guide-actions"><button class="btn solid" onclick="showTab('mon')" data-i18n="guide.goto.mon">去监视运行</button></div>
          </div>
        </div>
      </li>
    </ol>
  </section>

  <section id="panel-mon" class="panel">
    <div class="lede">
      <h1 data-i18n="mon.title">エージェント運用状況</h1>
      <div class="sub" data-i18n="mon.sub">稼働中の各エージェントの状態・モデルをひと目で。</div>
      <div class="statusline">
        <span id="beacon" class="beacon down"><span class="dot"></span><span id="beacon-t"></span></span>
        <span class="metaline" id="meta"></span>
        <span class="right">
          <label class="sw"><input type="checkbox" id="auto" checked> <span data-i18n="sw.auto">5秒毎に更新</span></label>
          <button class="linkbtn" onclick="loadLive()" data-i18n="sw.now">今すぐ更新</button>
        </span>
      </div>
    </div>
    <div class="figs" id="figs"></div>
    <section class="quota-section" id="quota-section">
      <div class="quota-title"><span data-i18n="quota.title">オーナー別デイリー quota</span>
        <span class="src" data-i18n="quota.utc">UTC 日次・provider usage</span></div>
      <div class="quota-list" id="owner-quotas"></div>
    </section>
    <div class="transcript-health" id="transcript-health"></div>
    <section class="worktree-section">
      <div class="worktree-title">
        <span data-i18n="worktree.title">スレッド worktree</span>
        <span class="src" data-i18n="worktree.sub">clean な worktree のみ手動削除できます</span>
      </div>
      <div class="worktree-list" id="worktrees"></div>
    </section>
    <div class="tasksec">
      <div class="seccap"><span data-i18n="tasks.title">注目タスク</span>
        <span class="src" id="tasks-repo"></span>
        <span style="flex:1"></span>
        <button class="linkbtn" onclick="loadIssues()" data-i18n="sw.now">今すぐ更新</button></div>
      <div id="issues"></div>
    </div>
    <div class="seccap" style="margin-top:26px"><span data-i18n="agents.title">エージェント</span></div>
    <div id="roster"></div>
  </section>

  <section id="panel-cfg" class="panel">
    <div class="lede"><h1 data-i18n="cfg.title">チーム構成</h1>
      <div class="sub" data-i18n="cfg.sub">agents.yaml と .env の編集。保存すると稼働中の multi_app に自動反映されます（agent の追加/削除は再起動が必要）。</div></div>
    <div class="card">
      <div class="head"><span class="nm" data-i18n="rules.title">共通チャンネルのルール（テンプレート）</span>
        <span style="flex:1"></span>
        <button class="btn text copy-btn" id="rules-copy" onclick="copyRules()"><span class="ic-wrap" aria-hidden="true"><svg class="ic-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11" height="11" rx="2.5"/><path d="M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/></svg><svg class="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><span data-i18n="rules.copy">コピー</span></button></div>
      <div class="sub" data-i18n="rules.sub" style="margin:6px 0 10px">Slack 共通チャンネルの topic/説明に貼ると、各 agent がこのルールに従います。</div>
      <pre id="rules-pre" style="max-height:320px">…</pre>
    </div>
    <div id="agents"></div>
    <div class="card">
      <div class="head"><span class="nm" data-i18n="cfg.newagent">新しい agent</span></div>
      <div class="grid2" style="margin-top:16px">
        <div><span class="lbl" data-i18n="cfg.name">name</span><input id="new-name" placeholder="qa"></div>
        <div><span class="lbl" data-i18n="cfg.workspace">workspace</span><input id="new-ws" placeholder="~/workspace/agent-sandbox-qa"></div>
      </div>
      <span class="lbl" style="display:block;margin:16px 0 6px" data-i18n="lbl.runtime">runtime</span>
      <div class="seg" id="new-rt-wrap">
        <button type="button" class="rt on" data-rt="claude" onclick="pickNewRt('claude')">claude</button>
        <button type="button" class="rt" data-rt="codex" onclick="pickNewRt('codex')">codex</button>
        <button type="button" class="rt" data-rt="openai" onclick="pickNewRt('openai')">openai api</button>
      </div>
      <span class="lbl" style="display:block;margin:16px 0 6px" data-i18n="cfg.card">card（队友接口卡）</span>
      <div class="hint" data-i18n="cfg.cardhint">给队友看的接口卡：何时找我 / handoff 带什么 / 我交付什么 / 什么别找我。留空则用 persona 首行</div>
      <textarea id="new-card" rows="4" placeholder="…" oninput="watchCard(this,'new-card-long')"></textarea>
      <div class="cardlong" id="new-card-long" data-i18n="cfg.cardlong">建议 ≤400 字符 / 6 行（仅警告）</div>
      <span class="lbl" style="display:block;margin:16px 0 6px" data-i18n="cfg.persona">persona（自己的行为约束）</span>
      <textarea id="new-persona" rows="3" placeholder="…"></textarea>
      <div style="margin-top:16px"><button class="btn solid" onclick="addAgent()" data-i18n="cfg.save">保存</button>
      <span class="msg" id="new-msg"></span></div>
    </div>
  </section>

  <section id="panel-auth" class="panel">
    <div class="lede"><h1 data-i18n="auth.title">認証</h1>
      <div class="sub" data-i18n="auth.sub">エージェントが実際に動く環境（Docker コンテナ、なければこのホスト）の認証です。</div></div>

    <div class="card" id="card-claude">
      <div class="head"><span class="nm" data-i18n="auth.claude">Claude</span>
        <span style="flex:1"></span>
        <span class="state" id="cl-state"></span></div>
      <div class="sub" id="cl-meta" style="margin:6px 0 14px"></div>
      <div id="cl-step1">
        <button class="btn solid" id="cl-start" onclick="claudeStart()" data-i18n="auth.signin">ログイン開始</button>
        <span class="msg" id="cl-msg"></span>
      </div>
      <div id="cl-step2" style="display:none">
        <div class="sub" data-i18n="auth.openurl" style="margin-bottom:8px">下のリンクで承認し、表示された code を貼り付けてください。</div>
        <a id="cl-url" class="btn text" target="_blank" rel="noopener noreferrer"
           data-i18n="auth.authorize">承認ページを開く</a>
        <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap">
          <input id="cl-code" placeholder="code" autocomplete="off" spellcheck="false" style="min-width:220px;flex:1">
          <button class="btn solid" id="cl-submit" onclick="claudeSubmit()" data-i18n="auth.submit">送信して検証</button>
          <button class="btn text" onclick="claudeCancel()" data-i18n="btn.cancel">キャンセル</button>
        </div>
        <span class="msg" id="cl-msg2"></span>
      </div>
      <div id="cl-step3" style="display:none">
        <div class="msg ok" id="cl-verified" style="display:block;margin-bottom:8px"></div>
        <div class="sub" id="cl-verified-detail" style="margin-bottom:12px"></div>
        <button class="btn text" onclick="loadAuth()" data-i18n="auth.refresh">状態を更新</button>
      </div>
    </div>

    <div class="card" id="card-codex">
      <div class="head"><span class="nm" data-i18n="auth.codex">Codex</span>
        <span style="flex:1"></span>
        <span class="state" id="cx-state"></span></div>
      <div class="sub" id="cx-meta" style="margin:6px 0 14px"></div>
      <div id="cx-step1">
        <button class="btn solid" id="cx-start" onclick="codexStart()" data-i18n="auth.signin">ログイン開始</button>
        <span class="msg" id="cx-msg"></span>
      </div>
      <div id="cx-step2" style="display:none">
        <div class="sub" data-i18n="auth.devicehint" style="margin-bottom:10px">下のリンクを開き、表示されたワンタイムコードを入力してください。ブラウザで承認が終わると自動で検証します。</div>
        <a id="cx-url" class="btn text" target="_blank" rel="noopener noreferrer"
           data-i18n="auth.authorize">承認ページを開く</a>
        <div class="sub" style="margin:14px 0 6px" data-i18n="auth.devicecode">ワンタイムコード</div>
        <div id="cx-code" style="font-family:var(--data);font-size:1.35rem;letter-spacing:.12em;color:var(--accent);font-weight:600"></div>
        <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <span class="msg" id="cx-msg2" style="display:inline"></span>
          <button class="btn text" onclick="codexCancel()" data-i18n="btn.cancel">キャンセル</button>
        </div>
      </div>
      <div id="cx-step3" style="display:none">
        <div class="msg ok" id="cx-verified" style="display:block;margin-bottom:8px"></div>
        <div class="sub" id="cx-verified-detail" style="margin-bottom:12px"></div>
        <button class="btn text" onclick="loadAuth()" data-i18n="auth.refresh">状態を更新</button>
      </div>
    </div>

    <div class="card" id="card-gh">
      <div class="head"><span class="nm" data-i18n="auth.gh">GitHub</span>
        <span style="flex:1"></span>
        <span class="state" id="gh-state"></span></div>
      <div class="sub" id="gh-meta" style="margin:6px 0 14px" data-i18n="auth.ghsub">ホストでデバイス認証し、GH_TOKEN を .env に書き込みます（コンテナは keychain を使えません）。</div>
      <div id="gh-step1">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
          <button class="btn solid" id="gh-start" onclick="ghStart()" data-i18n="auth.signin">ログイン開始</button>
          <button class="btn text" id="gh-import" onclick="ghImport()" data-i18n="auth.ghimport">ホストの gh から取り込む</button>
        </div>
        <div class="sub" data-i18n="auth.ghtokenalt" style="margin-bottom:8px">またはトークンを直接貼り付け：</div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <input id="gh-token" type="password" placeholder="ghp_… / gho_…" autocomplete="off" spellcheck="false" style="min-width:220px;flex:1">
          <button class="btn solid" onclick="ghSave()" data-i18n="cfg.save">保存</button>
        </div>
        <span class="msg" id="gh-msg"></span>
        <div class="sub" data-i18n="auth.ghhint" style="margin-top:12px">コンテナへ反映するには再作成が必要です（make up）。</div>
      </div>
      <div id="gh-step2" style="display:none">
        <div class="sub" data-i18n="auth.devicehint" style="margin-bottom:10px">下のリンクを開き、表示されたワンタイムコードを入力してください。ブラウザで承認が終わると自動で検証します。</div>
        <a id="gh-url" class="btn text" target="_blank" rel="noopener noreferrer"
           data-i18n="auth.authorize">承認ページを開く</a>
        <div class="sub" style="margin:14px 0 6px" data-i18n="auth.devicecode">ワンタイムコード</div>
        <div id="gh-code" style="font-family:var(--data);font-size:1.35rem;letter-spacing:.12em;color:var(--accent);font-weight:600"></div>
        <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <span class="msg" id="gh-msg2" style="display:inline"></span>
          <button class="btn text" onclick="ghCancel()" data-i18n="btn.cancel">キャンセル</button>
        </div>
      </div>
      <div id="gh-step3" style="display:none">
        <div class="msg ok" id="gh-verified" style="display:block;margin-bottom:8px"></div>
        <div class="sub" id="gh-verified-detail" style="margin-bottom:12px"></div>
        <button class="btn text" onclick="loadAuth()" data-i18n="auth.refresh">状態を更新</button>
      </div>
    </div>
  </section>

  <div class="foot">SlackAgentTeam · control deck</div>
</main>
<div class="toast" id="toast"></div>

<script>
const $=s=>document.querySelector(s);
document.documentElement.classList.add('booting');
window.addEventListener('load',()=>requestAnimationFrame(()=>requestAnimationFrame(()=>
  document.documentElement.classList.remove('booting'))));
const CONTROL_TOKEN_KEY='slackagent.control_token';
function controlToken(){try{return sessionStorage.getItem(CONTROL_TOKEN_KEY)||''}catch(e){return ''}}
function saveControlToken(value){try{sessionStorage.setItem(CONTROL_TOKEN_KEY,String(value||''))}catch(e){}}
function withControlAuth(options){
  const next=Object.assign({},options||{}), headers=new Headers(next.headers||{});
  const token=controlToken();if(token)headers.set('Authorization','Bearer '+token);
  next.headers=headers;return next;
}
// a token is asked for only after the server answers 401: nodes without
// control auth (local legacy mode) never show the prompt at all
let CONTROL_REQUIRED=false;
const apiFetch=async(u,o)=>{
  const r=await fetch(u,withControlAuth(o));
  if(r.status===401){CONTROL_REQUIRED=true;showUnlock(!!controlToken());}
  return r;
};
const j=async(u,o)=>(await apiFetch(u,o)).json();
function showUnlock(rejected){
  if(rejected)saveControlToken('');
  const bar=$('#unlock');if(!bar)return;
  $('#unlock-msg').textContent=rejected?t('unlock.bad'):'';
  if(!bar.classList.contains('on')){bar.classList.add('on');const input=$('#unlock-token');if(input)input.focus();}
}
function unlockControl(){
  const input=$('#unlock-token'), value=(input.value||'').trim();
  if(!value)return;
  saveControlToken(value);input.value='';$('#unlock').classList.remove('on');
  showTab(TABS.find(k=>$('#panel-'+k).classList.contains('on'))||'mon');
}
function ensureControlToken(){
  if(!CONTROL_REQUIRED||controlToken())return;
  showUnlock(false);
}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function safeHttpUrl(value){
  try{const u=new URL(String(value||''),location.origin);
    return (u.protocol==='http:'||u.protocol==='https:')?u.href:'';}catch(e){return '';}
}
let MODELS={claude:[''],codex:[''],openai:[''],sources:{},current:{}}, MODELS_READY=false,
  MODELS_LOADING=null, NEWRT='claude', PENDING=Object.create(null);

const I18N={
 zh:{'nav.guide':'指引','nav.mon':'监视','nav.cfg':'构成','tasks.title':'关注中的任务','agents.title':'智能体','tasks.empty':'没有开放的 issue','tasks.unassigned':'未认领','rules.title':'共通频道规则（模板）','rules.sub':'贴到 Slack 共通频道的 topic/说明，各 agent 会遵守这些规则。','rules.copy':'复制','rules.copied':'规则已复制',
   'guide.kicker':'从这里开始 · OWNER 运行手册','guide.title':'从本机账号，到第一次安全交接','guide.sub':'按顺序完成五步。每个人只配置自己的节点；团队通过 Slack 协调，通过 GitHub 交付代码。',
   'guide.boundary':'只共享无凭据 roster、Slack 消息与 PR URL。不要共享 AI 登录、API Key、Slack Token、GitHub Token、状态库或工作区。',
   'guide.progress':'本浏览器的上手进度','guide.map.slack':'共享 Slack 线程','guide.map.local':'每人独立本地节点','guide.map.artifact':'GitHub PR / Issue',
   'guide.done':'标记完成','guide.evidence':'完成证据','guide.do':'按这个顺序','guide.copy':'复制','guide.copied':'模板已复制','guide.copyselect':'浏览器禁止复制；模板已全选，请按系统复制键','guide.copyfail':'复制失败',
   'guide.next':'下一步：{n}','guide.complete':'上手完成。现在按“一任务一线程”开始协作。','guide.next.roles':'确定角色与 owner/node','guide.next.slack':'创建并邀请 Slack Apps','guide.next.local':'连接本机账号与仓库','guide.next.prompts':'为每个 agent 写职责 Prompt','guide.next.first-task':'跑通第一次任务与交接',
   'guide.goto.cfg':'去团队构成','guide.goto.slack':'去设置 Slack App','guide.open.slack':'打开 api.slack.com/apps','guide.slack.do5':'也可以在 Claude Code 里运行 /slack-app-setup，由 agent 操作浏览器完成，只在关键步骤请你确认','guide.goto.auth':'去认证','guide.goto.prompt':'去编辑 Prompt','guide.goto.mon':'去监视运行',
   'guide.roles.eye':'团队契约','guide.roles.title':'先分清谁负责什么','guide.roles.body':'每人保留 2–3 个 agent。developer 写代码，reviewer 独立审查，planner/pm 只拆任务；OpenAI agent 默认没有本地文件工具。',
   'guide.agent.role':'角色','guide.agent.must':'必须做到','guide.agent.never':'不要做',
   'guide.dev.must':'确认完成标准；实现并测试；commit、push、贴 PR；只交给一个 reviewer。','guide.dev.never':'不要在别人的节点登录；不要把未 push 的本地路径当成交付物。',
   'guide.reviewer.must':'独立查看 PR/diff；按严重度和文件行号报告；明确通过或退回。','guide.reviewer.never':'未经要求不要直接改代码；不要用实现者结论代替验证。',
   'guide.planner.must':'澄清目标、约束、完成标准和单一下一位 agent。','guide.planner.never':'不要声称读过本地仓库；不要同时 fan-out 给多个 agent。',
   'guide.openai.must':'处理 Slack 中可见的文本、分析和评审；需要文件时 handoff。','guide.openai.never':'不要假装拥有 Bash、git、gh 或本地文件访问。',
   'guide.roles.ev1':'roster.yaml 中每个 agent 都有唯一 owner 与 node_id','guide.roles.ev2':'每个人的本地配置只列自己的 2–3 个 runtime',
   'guide.slack.eye':'SLACK 入口','guide.slack.title':'一个 agent，一个 Slack App','guide.slack.body':'从 manifest 创建 App，安装到 workspace，保存 xoxb/xapp，并把所有 bot 邀请进项目频道。scope 变化后必须重新安装。',
   'guide.slack.do1':'在「团队构成」为本机每个 agent 复制 manifest','guide.slack.do2':'Install to Workspace，粘贴并验证 Bot/App Token','guide.slack.do3':'邀请全部本地与远端 bot 进入共享项目频道','guide.slack.do4':'把频道规则贴到 topic/说明：一任务一线程、一次只叫一个 agent',
   'guide.prompt.channel':'Slack 频道规则模板','guide.local.eye':'私有节点','guide.local.title':'只连接这台机器自己的账号','guide.local.body':'登录本人 Claude/Codex/GitHub，OpenAI Key 只放本机 env；创建独立 workspace、state 和 worktree volume。绝不挂载另一位 owner 的认证目录。',
   'guide.local.ev1':'「认证」显示所选 runtime 与 GitHub 已验证','guide.local.ev2':'本机 env 只含本人 Slack Token、控制 Bearer 和 AI Key','guide.local.ev3':'仓库 origin 与 agent 的 canonical OWNER/REPO 一致',
   'guide.prompts.eye':'PROMPT 契约','guide.prompts.title':'Prompt 写职责，不写口号','guide.prompts.body':'card 让队友知道何时找它、交什么、拿回什么；persona 约束它如何工作。推荐模板可复制后按项目改写。',
   'guide.prompt.dev':'developer persona 推荐','guide.prompt.reviewer':'reviewer persona 推荐','guide.prompt.planner':'planner / pm persona 推荐',
   'guide.prompts.ev1':'每个 card 能在 6 行内说清输入、输出和禁区','guide.prompts.ev2':'reviewer 与 developer 使用独立验证标准，OpenAI persona 明示无本地工具',
   'guide.task.eye':'第一次真实闭环','guide.task.title':'在一个 Slack 线程跑完整闭环','guide.task.body':'人类给一个明确任务；developer 在本地 worktree 实现并产出 PR；reviewer 独立验证。跨机器只传 Slack 上下文和 durable artifact。',
   'guide.prompt.task':'人类启动任务模板','guide.prompt.handoff':'结构化 HANDOFF 模板','guide.task.ev1':'同一个根线程里能看到目标、测试结果、PR URL 和单目标 handoff','guide.task.ev2':'reviewer 给出明确通过，或带严重度与文件行号的退回意见',
   'mon.title':'智能体运行状况','mon.sub':'一览各智能体的状态与模型；模型和 runtime 可在此免重启切换。',
   'status.live':'运行中','status.down':'未启动','status.dis':'未连接',
   'meta.budget':'往复预算','meta.offline':'请启动 multi_app（make run）',
   'sw.auto':'每 5 秒刷新','sw.now':'立即刷新',
   'fig.agents':'智能体','fig.busy':'运行中','fig.session':'会话','fig.conn':'已连接',
   'quota.title':'所有者每日配额','quota.utc':'按 UTC 日统计 · provider usage','quota.empty':'未配置所有者配额',
   'quota.used':'已使用','quota.limit':'上限','quota.remaining':'剩余','quota.reserved':'活跃预留',
   'quota.denied':'拒绝','quota.errors':'错误','quota.agent':'agent','quota.runtime':'runtime',
   'quota.turns':'轮次','quota.estimated':'估算',
   'worktree.title':'线程 worktree','worktree.sub':'仅可手动移除 clean、无未推送提交的 worktree','worktree.empty':'没有线程 worktree','worktree.active':'活跃','worktree.remove':'移除','worktree.confirm':'移除该 clean worktree？branch 会保留。','worktree.removed':'worktree 已移除，branch 已保留',
   'transcript.threads':'转录线程','transcript.messages':'内存消息','transcript.persisted':'持久消息','transcript.memory':'内存','transcript.database':'SQLite','transcript.warm':'完整','transcript.partial':'待补全','transcript.evictions':'淘汰','transcript.backfills':'回填','transcript.calls':'Slack 调用','transcript.failures':'失败',
   'empty.noagents':'没有智能体。请在「构成」添加并启动 multi_app。',
   'st.busy':'运行中','st.idle':'待机','st.off':'未连接','model.def':'既定模型',
   'model.custom':'自定义模型 id','model.custom.hint':'任意模型名，如 grok-4.5 / Antigravity 转出模型；回车确认',
   'lbl.runtime':'runtime','lbl.model':'模型','lbl.replylang':'回复语言','toast.lang':'✓ {n} 回复语言 → {l}','lbl.effort':'推理强度','toast.effort':'✓ {n} 推理强度 → {e}','btn.restart':'会话重启',
   'confirm.q':'{n} 切换到 {m}？','btn.apply':'应用','btn.cancel':'取消',
   'threads.cap':'线程 {a} / {b}','th.run':'运行中','th.wait':'待机','th.left':'剩余',
   'aux':'会话 {s} · 巡逻 {p}','tk':'tok',
   'toast.sw':'✓ {n} → {m}（下一轮生效）','toast.swf':'切换失败',
   'rt.confirm':'将 {n} 的 runtime 切换为 {r}。 现有会话将被丢弃。确定吗？','toast.rt':'✓ {n} runtime → {r}',
   'rs.confirm':'重启 {n} 的会话。 进行中的会话上下文将被丢弃，以新模型重开。','toast.rs':'↻ {n} 重启（丢弃 {c} 个会话）',
   'toast.fail':'操作失败','def.as':'— 既定（{m}）—','def.plain':'— 既定 —',
   'cfg.title':'团队构成','cfg.sub':'编辑 agents.yaml 与 .env；保存时自动热加载到运行中的 multi_app（新增/删除 agent 需重启）。','cfg.reloaded':'✓ 已保存并同步到运行中的 multi_app','cfg.deferred':'已保存；agent 空闲后自动应用','cfg.failed':'已保存，但运行配置应用失败','cfg.pending':'配置待应用',
   'cfg.newagent':'新增 agent','cfg.name':'name','cfg.workspace':'workspace','cfg.persona':'persona（自己的行为约束）',
   'cfg.card':'card（队友接口卡）','cfg.cardhint':'给队友看的接口卡：何时找我 / handoff 带什么 / 我交付什么 / 什么别找我。留空则用 persona 首行','cfg.cardlong':'建议 ≤400 字符 / 6 行（仅警告，不阻止保存）',
   'cfg.save':'保存','cfg.setup':'设置','cfg.retoken':'重设 token','cfg.required':'必需','cfg.optional':'可选',
   'wz.s1':'1. 点「用此 manifest 在 Slack 创建」（或打开 api.slack.com/apps →「From a manifest」贴入下方）→ 选择 workspace → Create → Install to Workspace',
   'wz.copy':'复制 manifest','wz.create':'用此 manifest 在 Slack 创建','wz.s2':'2. 粘贴 Bot Token (xoxb-) 与 App-Level Token (xapp-, connections:write)：',
   'wz.save':'验证并写入 .env','tok.copied':'manifest 已复制','saved':'✓ 已保存','savefail':'保存失败','unlock.title':'需要控制令牌','unlock.hint':'这个节点启用了控制认证。输入 .env 里为你（owner）配置的控制 Bearer 令牌；只保存在本标签页的会话里。','unlock.save':'解锁','unlock.bad':'令牌不正确，请重新输入。','ws.title':'本地 workspace','ws.edit':'修改','ws.path':'目录（绝对路径或 ~/…）','ws.repo':'GitHub 仓库（OWNER/REPO，留空沿用默认）','ws.check':'检查','ws.unset':'未设置（使用 CLAUDE_WORKSPACE 或启动目录）','ws.nogit':'不是 git 仓库，GitHub 协作不可用','ws.branch':'分支','ws.mismatch':'origin 与 GitHub 仓库不一致，GitHub 协作会被禁用','ws.useorigin':'改用 {r}','cfg.editpersona':'编辑内容','cfg.restarthint':'重启 multi_app 生效','cfg.worktreerootrestart':'worktree root 仅在重启后生效；重启前请先用旧 root clean remove 仍存活的映射',
      'nav.auth':'认证','auth.title':'认证','auth.sub':'智能体实际运行环境（优先 Docker 容器，否则本机）的登录凭据。',
   'auth.claude':'Claude','auth.codex':'Codex','auth.gh':'GitHub',
   'auth.signin':'开始登录','auth.openurl':'打开下方链接完成授权，再把页面给出的 code 粘贴回来。',
   'auth.authorize':'打开授权页','auth.submit':'提交并验证','auth.refresh':'刷新状态',
   'auth.devicehint':'打开下方链接，输入一次性代码。浏览器完成授权后会自动验证。',
   'auth.devicecode':'一次性代码','auth.waiting':'等待浏览器授权…',
   'auth.ghsub':'在本机走设备授权，并把 GH_TOKEN 写入 .env（容器无法使用本机 keychain）。',
   'auth.ghimport':'从本机 gh 导入','auth.ghtokenalt':'或直接粘贴 token：',
   'auth.ghhint':'写入容器需重建（make up）。',
   'auth.checking':'检查中…','auth.failed':'读取失败','auth.in':'已登录','auth.out':'未登录',
   'auth.docker':'Docker 容器','auth.host':'本机','auth.nocli':'找不到 CLI',
   'auth.set':'已配置 GH_TOKEN','auth.unset':'未配置 GH_TOKEN',
   'auth.starting':'启动中…','auth.needcode':'请填写 code','auth.verifying':'验证中…',
   'auth.ok':'✓ 已验证登录','auth.needtoken':'请填写 token',
   'auth.ghsaved':'✓ GH_TOKEN 已写入 .env','auth.importing':'导入中…',
   'auth.verified':'✓ 验证成功','auth.verified.detail':'运行环境已可用 · {m}',
   'auth.method':'方式：{m}','auth.user':'账号：{u}'},
 ja:{'nav.guide':'ガイド','nav.mon':'監視','nav.cfg':'構成','tasks.title':'注目タスク','agents.title':'エージェント','tasks.empty':'オープンな issue はありません','tasks.unassigned':'未割り当て','rules.title':'共通チャンネルのルール（テンプレート）','rules.sub':'Slack 共通チャンネルの topic/説明に貼ると各 agent が従います。','rules.copy':'コピー','rules.copied':'ルールをコピー',
   'guide.kicker':'ここから開始 · OWNER ランブック','guide.title':'ローカル認証から、最初の安全な引き継ぎまで','guide.sub':'5つの手順を順番に進めます。各自は自分のノードだけを構成し、Slack で調整、GitHub でコードを受け渡します。',
   'guide.boundary':'共有するのは認証情報を含まない roster、Slack メッセージ、PR URL だけです。AI ログイン、API Key、Slack/GitHub Token、状態 DB、workspace は共有しません。',
   'guide.progress':'このブラウザのセットアップ進捗','guide.map.slack':'共有 Slack スレッド','guide.map.local':'各自の独立ローカルノード','guide.map.artifact':'GitHub PR / Issue',
   'guide.done':'完了にする','guide.evidence':'完了の証拠','guide.do':'この順で実施','guide.copy':'コピー','guide.copied':'テンプレートをコピーしました','guide.copyselect':'browser がコピーを拒否しました。テンプレートを全選択したのでコピーキーを押してください','guide.copyfail':'コピーできませんでした',
   'guide.next':'次：{n}','guide.complete':'準備完了です。「1タスク・1スレッド」で運用を始めてください。','guide.next.roles':'role と owner/node を決める','guide.next.slack':'Slack App を作成して招待する','guide.next.local':'このノードの認証と repo を接続する','guide.next.prompts':'agent ごとの責務 Prompt を書く','guide.next.first-task':'最初のタスクと引き継ぎを完走する',
   'guide.goto.cfg':'チーム構成へ','guide.goto.slack':'Slack App 設定へ','guide.open.slack':'api.slack.com/apps を開く','guide.slack.do5':'Claude Code で /slack-app-setup を実行すると、agent がブラウザを操作して設定し、要所だけ確認を求めます','guide.goto.auth':'認証へ','guide.goto.prompt':'Prompt 編集へ','guide.goto.mon':'運用監視へ',
   'guide.roles.eye':'チーム契約','guide.roles.title':'最初に責務を分ける','guide.roles.body':'各自 2〜3 agent を持ちます。developer は実装、reviewer は独立レビュー、planner/pm は分解のみ。OpenAI agent は既定でローカルファイルを扱いません。',
   'guide.agent.role':'role','guide.agent.must':'必須','guide.agent.never':'しないこと',
   'guide.dev.must':'完了条件を確認し、実装・テスト・commit・push・PR を行い、1人の reviewer に渡す。','guide.dev.never':'他人のノードへログインしない。未 push のローカルパスを成果物にしない。',
   'guide.reviewer.must':'PR/diff を独立確認し、重大度とファイル行を付け、承認か差し戻しかを明示する。','guide.reviewer.never':'依頼なしに直接修正しない。実装者の結論を検証の代わりにしない。',
   'guide.planner.must':'目的・制約・完了条件・次の単一 agent を明確にする。','guide.planner.never':'ローカル repo を読んだと主張しない。複数 agent へ同時 fan-out しない。',
   'guide.openai.must':'Slack 上のテキストで分析・レビューし、ファイルが必要なら handoff する。','guide.openai.never':'Bash、git、gh、ローカルファイルが使えるふりをしない。',
   'guide.roles.ev1':'roster.yaml の全 agent に一意の owner と node_id がある','guide.roles.ev2':'各自のローカル設定には本人の 2〜3 runtime だけがある',
   'guide.slack.eye':'SLACK 入口','guide.slack.title':'1 agent に 1 Slack App','guide.slack.body':'manifest から App を作成し、workspace にインストール、xoxb/xapp を保存し、全 bot をプロジェクトチャンネルへ招待します。scope 変更後は再インストールが必要です。',
   'guide.slack.do1':'「チーム構成」でローカル agent ごとの manifest をコピー','guide.slack.do2':'Install to Workspace 後、Bot/App Token を貼って検証','guide.slack.do3':'ローカルとリモートの全 bot を共有プロジェクトチャンネルへ招待','guide.slack.do4':'topic/説明へルールを貼る：1タスク1スレッド、呼ぶ agent は1人',
   'guide.prompt.channel':'Slack チャンネルルール','guide.local.eye':'プライベートノード','guide.local.title':'このマシンの自分のアカウントだけを接続','guide.local.body':'自分の Claude/Codex/GitHub にログインし、OpenAI Key はローカル env のみに保存します。workspace、state、worktree volume は owner ごとに分離します。',
   'guide.local.ev1':'「認証」で利用 runtime と GitHub が検証済み','guide.local.ev2':'ローカル env には本人の Slack Token、control Bearer、AI Key だけがある','guide.local.ev3':'repo origin が agent の canonical OWNER/REPO と一致する',
   'guide.prompts.eye':'PROMPT 契約','guide.prompts.title':'Prompt には責務を書く','guide.prompts.body':'card は、いつ呼ぶか・何を渡すか・何を返すかを仲間へ示します。persona は作業手順を制約します。テンプレートをコピーしてプロジェクトに合わせてください。',
   'guide.prompt.dev':'developer persona 推奨','guide.prompt.reviewer':'reviewer persona 推奨','guide.prompt.planner':'planner / pm persona 推奨',
   'guide.prompts.ev1':'各 card は6行以内で入力・出力・禁止事項が分かる','guide.prompts.ev2':'reviewer と developer は別の検証基準を持ち、OpenAI persona はローカルツールなしと明記',
   'guide.task.eye':'最初の実運用ループ','guide.task.title':'1つの Slack スレッドで完走する','guide.task.body':'人が明確なタスクを渡し、developer はローカル worktree で実装して PR を作成、reviewer が独立検証します。ノード間では Slack 文脈と永続成果物だけを渡します。',
   'guide.prompt.task':'人が開始するタスクのテンプレート','guide.prompt.handoff':'構造化 HANDOFF テンプレート','guide.task.ev1':'同じルートスレッドに目的、テスト結果、PR URL、単一 target の handoff がある','guide.task.ev2':'reviewer が明確に承認、または重大度とファイル行付きで差し戻す',
   'mon.title':'エージェント運用状況','mon.sub':'稼働中の各エージェントの状態・モデルをひと目で。モデルと runtime はここから再起動なしで切り替えられます。',
   'status.live':'稼働中','status.down':'未起動','status.dis':'未接続',
   'meta.budget':'往復予算','meta.offline':'multi_app を起動してください（make run）',
   'sw.auto':'5秒毎に更新','sw.now':'今すぐ更新',
   'fig.agents':'エージェント','fig.busy':'実行中','fig.session':'セッション','fig.conn':'接続',
   'quota.title':'オーナー別デイリー quota','quota.utc':'UTC 日次 · provider usage','quota.empty':'オーナー quota は未設定です',
   'quota.used':'使用済み','quota.limit':'上限','quota.remaining':'残り','quota.reserved':'有効な予約',
   'quota.denied':'拒否','quota.errors':'エラー','quota.agent':'agent','quota.runtime':'runtime',
   'quota.turns':'ターン','quota.estimated':'推定',
   'worktree.title':'スレッド worktree','worktree.sub':'clean かつ未 push commit のない worktree だけ手動削除できます','worktree.empty':'スレッド worktree はありません','worktree.active':'使用中','worktree.remove':'削除','worktree.confirm':'この clean worktree を削除しますか？branch は保持されます。','worktree.removed':'worktree を削除し、branch を保持しました',
   'transcript.threads':'履歴スレッド','transcript.messages':'メモリ内メッセージ','transcript.persisted':'永続メッセージ','transcript.memory':'メモリ','transcript.database':'SQLite','transcript.warm':'完全','transcript.partial':'補完待ち','transcript.evictions':'退避','transcript.backfills':'バックフィル','transcript.calls':'Slack 呼出','transcript.failures':'失敗',
   'empty.noagents':'エージェントがありません。「構成」で追加し multi_app を起動してください。',
   'st.busy':'実行中','st.idle':'待機','st.off':'未接続','model.def':'既定モデル',
   'model.custom':'カスタムモデル id','model.custom.hint':'任意のモデル名（例: grok-4.5 / Antigravity）。Enter で確定',
   'lbl.runtime':'runtime','lbl.model':'モデル','lbl.replylang':'返信言語','toast.lang':'✓ {n} 返信言語 → {l}','lbl.effort':'推論強度','toast.effort':'✓ {n} 推論強度 → {e}','btn.restart':'セッション再起動',
   'confirm.q':'{n} を {m} に切り替えますか？','btn.apply':'適用','btn.cancel':'取消',
   'threads.cap':'スレッド {a} / {b}','th.run':'実行中','th.wait':'待機','th.left':'残',
   'aux':'セッション {s} · 巡回 {p}','tk':'tok',
   'toast.sw':'✓ {n} → {m}（次ターンから）','toast.swf':'切替失敗',
   'rt.confirm':'{n} の runtime を {r} に切り替えます。 既存セッションは破棄されます。よろしいですか？','toast.rt':'✓ {n} runtime → {r}',
   'rs.confirm':'{n} のセッションを再起動します。 進行中の会話コンテキストは破棄され、新モデルで開始します。','toast.rs':'↻ {n} を再起動（{c} セッション破棄）',
   'toast.fail':'操作に失敗しました','def.as':'— 既定（{m}）—','def.plain':'— 既定 —',
   'cfg.title':'チーム構成','cfg.sub':'agents.yaml と .env の編集。保存すると稼働中の multi_app に自動反映されます（agent の追加/削除は再起動が必要）。','cfg.reloaded':'✓ 保存し、稼働中の multi_app へ反映しました','cfg.deferred':'保存済み。agent が待機状態になったら自動適用します','cfg.failed':'保存済みですが、稼働設定の適用に失敗しました','cfg.pending':'設定適用待ち',
   'cfg.newagent':'新しいエージェント','cfg.name':'name','cfg.workspace':'workspace','cfg.persona':'persona（自分向けの行動制約）',
   'cfg.card':'card（仲間向けカード）','cfg.cardhint':'仲間向けの窓口：いつ呼ぶか / handoff に何を含めるか / 何を返すか / 何は扱わないか。空なら persona 1行目','cfg.cardlong':'目安 ≤400 文字 / 6 行（警告のみ、保存は可）',
   'cfg.save':'保存','cfg.setup':'セットアップ','cfg.retoken':'token 再設定','cfg.required':'必須','cfg.optional':'任意',
   'wz.s1':'1.「この manifest で Slack に作成」を押す（または api.slack.com/apps →「From a manifest」に下記を貼付）→ workspace を選択 → Create → Install to Workspace',
   'wz.copy':'manifest をコピー','wz.create':'この manifest で Slack に作成','wz.s2':'2. Bot Token (xoxb-) と App-Level Token (xapp-, connections:write) を貼付:',
   'wz.save':'検証して .env に保存','tok.copied':'manifest コピー','saved':'✓ 保存','savefail':'保存に失敗しました','unlock.title':'コントロールトークンが必要です','unlock.hint':'このノードはコントロール認証が有効です。.env に自分（owner）用に設定したコントロール Bearer トークンを入力してください。このタブのセッションにだけ保存されます。','unlock.save':'ロック解除','unlock.bad':'トークンが正しくありません。もう一度入力してください。','ws.title':'ローカル workspace','ws.edit':'変更','ws.path':'ディレクトリ（絶対パスまたは ~/…）','ws.repo':'GitHub リポジトリ（OWNER/REPO、空欄なら既定を使用）','ws.check':'確認','ws.unset':'未設定（CLAUDE_WORKSPACE または起動ディレクトリ）','ws.nogit':'git リポジトリではないため GitHub 連携は使えません','ws.branch':'ブランチ','ws.mismatch':'origin と GitHub リポジトリが一致しないため GitHub 連携は無効になります','ws.useorigin':'{r} を使う','cfg.editpersona':'内容を編集','cfg.restarthint':'multi_app 再起動で反映','cfg.worktreerootrestart':'worktree root は再起動時だけ反映されます。稼働中の mapping は先に旧 root 設定で clean remove してください',
      'nav.auth':'認証','auth.title':'認証','auth.sub':'エージェントが実際に動く環境（優先：Docker コンテナ／なければこのホスト）のログイン情報です。',
   'auth.claude':'Claude','auth.codex':'Codex','auth.gh':'GitHub',
   'auth.signin':'ログイン開始','auth.openurl':'下のリンクで承認し、表示された code を貼り付けてください。',
   'auth.authorize':'承認ページを開く','auth.submit':'送信して検証','auth.refresh':'状態を更新',
   'auth.devicehint':'下のリンクを開き、表示されたワンタイムコードを入力してください。ブラウザで承認が終わると自動で検証します。',
   'auth.devicecode':'ワンタイムコード','auth.waiting':'ブラウザでの承認を待っています…',
   'auth.ghsub':'ホスト側でデバイス認証し、GH_TOKEN を .env に書き込みます（コンテナは keychain を使えません）。',
   'auth.ghimport':'ホストの gh から取り込む','auth.ghtokenalt':'またはトークンを直接貼り付け：',
   'auth.ghhint':'コンテナへ反映するには再作成が必要です（make up）。',
   'auth.checking':'確認中…','auth.failed':'状態を取得できませんでした','auth.in':'ログイン済み','auth.out':'未ログイン',
   'auth.docker':'Docker コンテナ','auth.host':'このホスト','auth.nocli':'CLI が見つかりません',
   'auth.set':'GH_TOKEN 設定済み','auth.unset':'GH_TOKEN 未設定',
   'auth.starting':'開始中…','auth.needcode':'code を入力してください','auth.verifying':'検証中…',
   'auth.ok':'✓ ログインを確認しました','auth.needtoken':'token を入力してください',
   'auth.ghsaved':'✓ GH_TOKEN を .env に保存しました','auth.importing':'取り込み中…',
   'auth.verified':'✓ 検証成功','auth.verified.detail':'実行環境で利用できます · {m}',
   'auth.method':'方式：{m}','auth.user':'アカウント：{u}'},
 en:{'nav.guide':'Guide','nav.mon':'Monitor','nav.cfg':'Setup','tasks.title':'Watched tasks','agents.title':'Agents','tasks.empty':'No open issues','tasks.unassigned':'unassigned','rules.title':'Shared channel rules (template)','rules.sub':'Paste into the shared Slack channel topic/description; agents will follow these rules.','rules.copy':'Copy','rules.copied':'rules copied',
   'guide.kicker':'START HERE · OWNER RUNBOOK','guide.title':'From local accounts to a safe first handoff','guide.sub':'Complete five steps in order. Each person configures only their node; coordinate in Slack and deliver code through GitHub.',
   'guide.boundary':'Share only the credential-free roster, Slack messages, and PR URLs. Never share AI logins, API keys, Slack or GitHub tokens, state databases, or workspaces.',
   'guide.progress':'Setup progress in this browser','guide.map.slack':'Shared Slack thread','guide.map.local':'One private node per person','guide.map.artifact':'GitHub PR / Issue',
   'guide.done':'Mark complete','guide.evidence':'Completion evidence','guide.do':'Do this in order','guide.copy':'Copy','guide.copied':'template copied','guide.copyselect':'Browser copy is blocked; the template is selected—use the system copy shortcut','guide.copyfail':'copy failed',
   'guide.next':'Next: {n}','guide.complete':'Setup complete. Start collaborating with one task per thread.','guide.next.roles':'assign roles and owner/node','guide.next.slack':'create and invite Slack Apps','guide.next.local':'connect this node’s accounts and repository','guide.next.prompts':'write a responsibility prompt for each agent','guide.next.first-task':'complete the first task and handoff',
   'guide.goto.cfg':'Open Team setup','guide.goto.slack':'Set up Slack Apps','guide.open.slack':'Open api.slack.com/apps','guide.slack.do5':'Or run /slack-app-setup in Claude Code: an agent drives the browser and asks you to confirm only the key steps','guide.goto.auth':'Open Auth','guide.goto.prompt':'Edit prompts','guide.goto.mon':'Monitor the run',
   'guide.roles.eye':'TEAM CONTRACT','guide.roles.title':'Assign responsibility first','guide.roles.body':'Keep two or three agents per person. developer writes code, reviewer verifies independently, and planner/pm only scopes work. OpenAI agents have no local file tools by default.',
   'guide.agent.role':'role','guide.agent.must':'must do','guide.agent.never':'do not',
   'guide.dev.must':'Confirm done criteria; implement and test; commit, push, link a PR; hand off to one reviewer.','guide.dev.never':'Never sign in on someone else’s node or treat an unpushed local path as a deliverable.',
   'guide.reviewer.must':'Inspect the PR/diff independently; report severity and file lines; clearly approve or return it.','guide.reviewer.never':'Do not edit unless asked or substitute the implementer’s conclusion for verification.',
   'guide.planner.must':'Clarify goal, constraints, done criteria, and exactly one next agent.','guide.planner.never':'Do not claim local repository access or fan out to multiple agents at once.',
   'guide.openai.must':'Analyze and review text visible in Slack; hand off when local files are required.','guide.openai.never':'Never pretend to have Bash, git, gh, or local filesystem access.',
   'guide.roles.ev1':'Every agent in roster.yaml has one unique owner and node_id','guide.roles.ev2':'Each local config lists only that owner’s two or three runtimes',
   'guide.slack.eye':'SLACK SURFACE','guide.slack.title':'One Slack App per agent','guide.slack.body':'Create each App from the manifest, install it to the workspace, save xoxb/xapp, and invite every bot to the project channel. Reinstall after scope changes.',
   'guide.slack.do1':'Copy a manifest for every local agent under Team setup','guide.slack.do2':'Install to Workspace, then paste and verify the Bot/App tokens','guide.slack.do3':'Invite every local and remote bot to the shared project channel','guide.slack.do4':'Put the channel rules in its topic/description: one task per thread, one target at a time',
   'guide.prompt.channel':'Slack channel rules','guide.local.eye':'PRIVATE NODE','guide.local.title':'Connect only this machine’s accounts','guide.local.body':'Sign in to your own Claude/Codex/GitHub and keep the OpenAI key in local env only. Use separate workspace, state, and worktree volumes; never mount another owner’s auth directory.',
   'guide.local.ev1':'Auth shows the chosen runtime and GitHub as verified','guide.local.ev2':'Local env contains only this owner’s Slack tokens, control Bearer, and AI key','guide.local.ev3':'Repository origin matches the agent’s canonical OWNER/REPO',
   'guide.prompts.eye':'PROMPT CONTRACT','guide.prompts.title':'Write responsibilities, not slogans','guide.prompts.body':'The card tells teammates when to call an agent, what to hand over, and what comes back. The persona constrains how it works. Copy a template and adapt it to the project.',
   'guide.prompt.dev':'Recommended developer persona','guide.prompt.reviewer':'Recommended reviewer persona','guide.prompt.planner':'Recommended planner / pm persona',
   'guide.prompts.ev1':'Each card explains input, output, and boundaries in six lines or fewer','guide.prompts.ev2':'reviewer and developer use independent standards; an OpenAI persona states that local tools are unavailable',
   'guide.task.eye':'FIRST LIVE LOOP','guide.task.title':'Complete one loop in one Slack thread','guide.task.body':'A human provides one clear task; developer implements in the local worktree and publishes a PR; reviewer verifies independently. Across machines, pass only Slack context and durable artifacts.',
   'guide.prompt.task':'Human task kickoff template','guide.prompt.handoff':'Structured HANDOFF template','guide.task.ev1':'One root thread contains the goal, test result, PR URL, and a single-target handoff','guide.task.ev2':'reviewer clearly approves or returns findings with severity and file lines',
   'mon.title':'Agent Operations','mon.sub':'Every agent’s status and model at a glance. Switch model and runtime here without restarting.',
   'status.live':'Online','status.down':'Offline','status.dis':'Disconnected',
   'meta.budget':'round budget','meta.offline':'Start multi_app (make run)',
   'sw.auto':'Refresh every 5s','sw.now':'Refresh now',
   'fig.agents':'agents','fig.busy':'busy','fig.session':'sessions','fig.conn':'connected',
   'quota.title':'Daily quota by owner','quota.utc':'UTC day · provider usage','quota.empty':'No owner quota configured',
   'quota.used':'used','quota.limit':'limit','quota.remaining':'remaining','quota.reserved':'active reserved',
   'quota.denied':'denied','quota.errors':'errors','quota.agent':'agent','quota.runtime':'runtime',
   'quota.turns':'turns','quota.estimated':'estimated',
   'worktree.title':'Thread worktrees','worktree.sub':'Only clean worktrees with no unpushed commits can be removed manually','worktree.empty':'No thread worktrees','worktree.active':'active','worktree.remove':'Remove','worktree.confirm':'Remove this clean worktree? Its branch will be retained.','worktree.removed':'Worktree removed; branch retained',
   'transcript.threads':'transcript threads','transcript.messages':'memory messages','transcript.persisted':'persisted messages','transcript.memory':'memory','transcript.database':'SQLite','transcript.warm':'complete','transcript.partial':'partial','transcript.evictions':'evictions','transcript.backfills':'backfills','transcript.calls':'Slack calls','transcript.failures':'failures',
   'empty.noagents':'No agents. Add one under Setup and start multi_app.',
   'st.busy':'Running','st.idle':'Idle','st.off':'Offline','model.def':'default model',
   'model.custom':'custom model id','model.custom.hint':'Any model id (e.g. grok-4.5 / Antigravity). Press Enter',
   'lbl.runtime':'runtime','lbl.model':'model','lbl.replylang':'Reply language','toast.lang':'✓ {n} reply language → {l}','lbl.effort':'Reasoning effort','toast.effort':'✓ {n} effort → {e}','btn.restart':'Restart session',
   'confirm.q':'Switch {n} to {m}?','btn.apply':'Apply','btn.cancel':'Cancel',
   'threads.cap':'threads {a} / {b}','th.run':'running','th.wait':'idle','th.left':'left',
   'aux':'sessions {s} · patrol {p}','tk':'tok',
   'toast.sw':'✓ {n} → {m} (next turn)','toast.swf':'switch failed',
   'rt.confirm':'Switch {n} runtime to {r}. Existing sessions will be discarded. Continue?','toast.rt':'✓ {n} runtime → {r}',
   'rs.confirm':'Restart {n}’s sessions. In-flight context is discarded; it reopens on the new model.','toast.rs':'↻ {n} restarted ({c} sessions cleared)',
   'toast.fail':'failed','def.as':'— default ({m}) —','def.plain':'— default —',
   'cfg.title':'Team','cfg.sub':'Edit agents.yaml and .env; saves hot-reload into the running multi_app (adding/removing agents needs a restart).','cfg.reloaded':'✓ Saved and applied to the running multi_app','cfg.deferred':'Saved; applies automatically when the agent is idle','cfg.failed':'Saved, but applying the live config failed','cfg.pending':'config pending',
   'cfg.newagent':'New agent','cfg.name':'name','cfg.workspace':'workspace','cfg.persona':'persona (self-facing behaviour constraints)',
   'cfg.card':'card (teammate interface)','cfg.cardhint':'For teammates: when to call me / what to hand off / what I deliver / what not to ask. Empty = persona first line','cfg.cardlong':'Prefer ≤400 chars / 6 lines (warning only; save still works)',
   'cfg.save':'Save','cfg.setup':'Set up','cfg.retoken':'Reset tokens','cfg.required':'required','cfg.optional':'optional',
   'wz.s1':'1. Click "Create in Slack from this manifest" (or open api.slack.com/apps → "From a manifest" and paste below) → pick the workspace → Create → Install to Workspace',
   'wz.copy':'Copy manifest','wz.create':'Create in Slack from this manifest','wz.s2':'2. Paste Bot Token (xoxb-) and App-Level Token (xapp-, connections:write):',
   'wz.save':'Verify & save to .env','tok.copied':'manifest copied','saved':'✓ Saved','savefail':'save failed','unlock.title':'Control token required','unlock.hint':'This node requires control authentication. Enter the control bearer token configured for you (the owner) in .env; it is kept in this tab session only.','unlock.save':'Unlock','unlock.bad':'That token was rejected. Try again.','ws.title':'Local workspace','ws.edit':'Change','ws.path':'Directory (absolute path or ~/…)','ws.repo':'GitHub repo (OWNER/REPO; empty inherits the default)','ws.check':'Check','ws.unset':'not set (falls back to CLAUDE_WORKSPACE or the launch directory)','ws.nogit':'not a git repo; GitHub collaboration is unavailable','ws.branch':'branch','ws.mismatch':'origin differs from the GitHub repo; GitHub collaboration will be disabled','ws.useorigin':'Use {r}','cfg.editpersona':'Edit content','cfg.restarthint':'restart multi_app to apply','cfg.worktreerootrestart':'worktree root applies only after restart; clean-remove live mappings with the old root first',
      'nav.auth':'Auth','auth.title':'Authentication','auth.sub':'Sign-in for the runtime agents actually use (Docker when up, otherwise this host).',
   'auth.claude':'Claude','auth.codex':'Codex','auth.gh':'GitHub',
   'auth.signin':'Start sign-in','auth.openurl':'Open the link below to authorize, then paste the code it shows.',
   'auth.authorize':'Open authorize page','auth.submit':'Submit & verify','auth.refresh':'Refresh status',
   'auth.devicehint':'Open the link and enter the one-time code. We verify automatically after browser approval.',
   'auth.devicecode':'One-time code','auth.waiting':'Waiting for browser approval…',
   'auth.ghsub':'Device sign-in on this host, then write GH_TOKEN to .env (the container cannot use the host keychain).',
   'auth.ghimport':'Import from host gh','auth.ghtokenalt':'Or paste a token directly:',
   'auth.ghhint':'Recreate the container to apply (make up).',
   'auth.checking':'checking…','auth.failed':'could not load status','auth.in':'signed in','auth.out':'signed out',
   'auth.docker':'Docker container','auth.host':'this host','auth.nocli':'CLI not found',
   'auth.set':'GH_TOKEN set','auth.unset':'GH_TOKEN not set',
   'auth.starting':'starting…','auth.needcode':'enter the code','auth.verifying':'verifying…',
   'auth.ok':'✓ Sign-in verified','auth.needtoken':'enter a token',
   'auth.ghsaved':'✓ GH_TOKEN written to .env','auth.importing':'importing…',
   'auth.verified':'✓ Verified','auth.verified.detail':'Ready on the runtime · {m}',
   'auth.method':'method: {m}','auth.user':'account: {u}'}
};
const GUIDE_NL=String.fromCharCode(10);
const GUIDE_PROMPTS={
  zh:{
    channel:[
      '协作规则：一项任务使用一个 Slack 根线程。',
      '人类每次只 @ 一个 agent，并写清目标、约束、完成标准与交付物。',
      'agent 每次只 handoff 给一个 target；跨机器成果必须是已 push 的 PR / Issue URL。',
      '不要在 Slack、roster 或 YAML 里粘贴 AI、Slack、GitHub 凭据。',
      'OpenAI agent 没有本地文件工具；需要代码检查时交给 Claude/Codex。'
    ].join(GUIDE_NL),
    dev:[
      '你是本节点的实现 agent。',
      '开始前确认目标、约束与可验证的完成标准；信息不足时向人类指出缺口。',
      '只在配置的 workspace / managed worktree 工作，不切换或删除受管 worktree。',
      '实现后运行相关测试，报告命令与结果；跨节点交付前必须 commit、push 并给出 PR URL。',
      '完成后只 handoff 给一个 reviewer；不得暴露 token、登录目录、状态库或本地绝对路径。'
    ].join(GUIDE_NL),
    reviewer:[
      '你是独立 reviewer，不复述实现者结论。',
      '先读取任务完成标准，再检查 PR/diff、失败路径、并发与安全边界，并运行适当验证。',
      '按 Critical / Important / Suggestion 报告问题，附文件与行号、影响和可复现证据。',
      '有 Critical/Important 时明确退回给一个 developer；没有时明确写“通过”并列出验证。',
      '未经要求不要直接改实现；没有本地工具时必须说明，并基于 Slack 中可见材料评审。'
    ].join(GUIDE_NL),
    planner:[
      '你是 planner / pm，只负责把需求变成可执行任务。',
      '输出：目标、非目标、约束、完成标准、依赖、风险、唯一下一位 target agent。',
      '一次只拆出一个可独立验收的下一步；不要同时 @ 多个 agent。',
      '不要写代码，也不要声称访问过本地仓库；需要事实时要求提供 Issue、PR 或文件摘录。'
    ].join(GUIDE_NL),
    task:[
      '@alice/developer 处理 TASK-123',
      '',
      '目标：<最终要得到什么>',
      '背景：<相关 Issue / PR / 现状>',
      '约束：<不能改什么、兼容性、安全边界>',
      '完成标准：',
      '1. <可验证结果一>',
      '2. <测试与失败路径>',
      '交付物：PR URL + 测试命令与结果',
      '完成后：只 handoff 给 bob/reviewer'
    ].join(GUIDE_NL),
    handoff:'HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-123","goal":"独立审查 PR","done_criteria":["测试通过","无 Critical/Important","给出文件行号证据"],"artifact":"https://github.com/ORG/REPO/pull/123"}'
  },
  ja:{
    channel:[
      '運用ルール：1タスクにつき1つの Slack ルートスレッドを使います。',
      '人は毎回1人の agent だけを @ し、目的・制約・完了条件・成果物を書きます。',
      'agent の handoff target は毎回1人だけ。ノード間の成果物は push 済み PR / Issue URL にします。',
      'AI、Slack、GitHub の認証情報を Slack、roster、YAML に貼らないでください。',
      'OpenAI agent はローカルファイルを扱えません。コード確認は Claude/Codex に渡します。'
    ].join(GUIDE_NL),
    dev:[
      'あなたはこのノードの実装 agent です。',
      '開始前に目的・制約・検証可能な完了条件を確認し、不足は人へ明示します。',
      '設定済み workspace / managed worktree だけで作業し、受管 worktree を切替・削除しません。',
      '関連テストを実行し、コマンドと結果を報告します。ノード間の受け渡し前に commit、push、PR URL を用意します。',
      '完了後は reviewer 1人だけへ handoff し、token、認証ディレクトリ、状態 DB、ローカル絶対パスを公開しません。'
    ].join(GUIDE_NL),
    reviewer:[
      'あなたは独立 reviewer です。実装者の結論をそのまま採用しません。',
      '完了条件を読み、PR/diff、失敗経路、並行性、安全境界を確認し、適切な検証を実行します。',
      'Critical / Important / Suggestion で分類し、ファイル行、影響、再現証拠を付けます。',
      'Critical/Important があれば developer 1人へ差し戻し、なければ「承認」と検証内容を明記します。',
      '依頼なしに実装を変更しません。ローカルツールがなければ明示し、Slack 上の材料だけで評価します。'
    ].join(GUIDE_NL),
    planner:[
      'あなたは planner / pm で、要求を実行可能なタスクへ分解します。',
      '目的、非目標、制約、完了条件、依存、リスク、次の単一 target agent を出力します。',
      '一度に独立検収できる次の1ステップだけを作り、複数 agent を同時に @ しません。',
      'コードを書かず、ローカル repo を見たと主張しません。必要なら Issue、PR、抜粋を要求します。'
    ].join(GUIDE_NL),
    task:[
      '@alice/developer TASK-123 を対応してください',
      '',
      '目的：<最終的に得たいもの>',
      '背景：<Issue / PR / 現状>',
      '制約：<変更禁止、互換性、安全境界>',
      '完了条件：',
      '1. <検証可能な結果>',
      '2. <テストと失敗経路>',
      '成果物：PR URL + テストコマンドと結果',
      '完了後：bob/reviewer 1人だけへ handoff'
    ].join(GUIDE_NL),
    handoff:'HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-123","goal":"PR を独立レビュー","done_criteria":["テスト合格","Critical/Important なし","ファイル行の証拠"],"artifact":"https://github.com/ORG/REPO/pull/123"}'
  },
  en:{
    channel:[
      'Working rule: use one Slack root thread per task.',
      'A human mentions one agent at a time and states the goal, constraints, done criteria, and deliverable.',
      'An agent hands off to exactly one target; cross-node deliverables must be pushed PR or Issue URLs.',
      'Never paste AI, Slack, or GitHub credentials into Slack, the roster, or YAML.',
      'OpenAI agents have no local file tools; hand code inspection to Claude or Codex.'
    ].join(GUIDE_NL),
    dev:[
      'You are the implementation agent on this node.',
      'Before starting, confirm the goal, constraints, and verifiable done criteria; call out missing information.',
      'Work only in the configured workspace or managed worktree; never switch or remove managed worktrees.',
      'Run relevant tests and report commands and results. Before cross-node delivery, commit, push, and provide a PR URL.',
      'Hand off to exactly one reviewer. Never expose tokens, auth directories, state databases, or local absolute paths.'
    ].join(GUIDE_NL),
    reviewer:[
      'You are an independent reviewer; do not repeat the implementer’s conclusion.',
      'Read the done criteria, inspect the PR/diff, failure paths, concurrency, and security boundaries, then run proportionate checks.',
      'Classify findings as Critical, Important, or Suggestion, with file lines, impact, and reproducible evidence.',
      'Return Critical/Important findings to one developer. Otherwise explicitly approve and list verification.',
      'Do not edit unless asked. If local tools are unavailable, say so and review only material visible in Slack.'
    ].join(GUIDE_NL),
    planner:[
      'You are a planner / pm. Turn a request into an executable task.',
      'Output the goal, non-goals, constraints, done criteria, dependencies, risks, and one next target agent.',
      'Create one independently verifiable next step at a time; never mention multiple agents at once.',
      'Do not write code or claim local repository access. Ask for an Issue, PR, or excerpt when facts are needed.'
    ].join(GUIDE_NL),
    task:[
      '@alice/developer handle TASK-123',
      '',
      'Goal: <the final outcome>',
      'Context: <Issue / PR / current behavior>',
      'Constraints: <do-not-change, compatibility, security boundary>',
      'Done criteria:',
      '1. <verifiable result>',
      '2. <tests and failure path>',
      'Deliverable: PR URL + test commands and results',
      'When done: hand off only to bob/reviewer'
    ].join(GUIDE_NL),
    handoff:'HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-123","goal":"Independently review the PR","done_criteria":["tests pass","no Critical/Important findings","file-line evidence included"],"artifact":"https://github.com/ORG/REPO/pull/123"}'
  }
};
let LANG='zh';
try{const s=localStorage.getItem('deck-lang');if(s&&Object.hasOwn(I18N,s))LANG=s;}catch(e){}
function t(k,p){let s=(I18N[LANG]&&I18N[LANG][k])||(I18N.ja[k])||k;
  if(p)for(const kk in p)s=s.split('{'+kk+'}').join(p[kk]);return s;}
function applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.getAttribute('data-i18n')));
  document.querySelectorAll('#lang button').forEach(b=>b.classList.toggle('on',b.dataset.lang===LANG));
  document.documentElement.lang=LANG;
}
function setLang(l){if(!Object.hasOwn(I18N,l))return;LANG=l;try{localStorage.setItem('deck-lang',l)}catch(e){}
  applyI18n();showTab(TABS.find(k=>$('#panel-'+k).classList.contains('on'))||'mon');}

function toast(m,err){const t2=$('#toast');t2.textContent=m;t2.className='toast on'+(err?' err':'');
  clearTimeout(t2._h);t2._h=setTimeout(()=>t2.className='toast',2600);}
// odometer (rare-ui animated-counter): every digit is a wheel of faces;
// columns are matched from the right, so gaining a place adds one on the left
function reducedMotion(){return matchMedia('(prefers-reduced-motion: reduce)').matches;}
function odoColumn(){
  const col=document.createElement('span');col.className='odo-col';
  const wheel=document.createElement('span');wheel.className='odo-wheel';
  for(let d=0;d<=10;d++){const face=document.createElement('span');face.textContent=String(d%10);wheel.appendChild(face);}
  col.appendChild(wheel);return col;
}
function turnWheel(col,digit,instant){
  const wheel=col.firstChild, cur=col.dataset.d===undefined?digit:Number(col.dataset.d);
  col.dataset.d=String(digit);
  const place=face=>{wheel.style.transform='translateY('+(-1.2*face)+'em)';};
  if(instant){wheel.style.transition='none';place(digit);void wheel.offsetHeight;wheel.style.transition='';return;}
  if(cur===9&&digit===0){
    // roll forward onto the trailing 0, then settle on the first one
    place(10);
    wheel.addEventListener('transitionend',()=>{if(col.dataset.d!=='0')return;
      wheel.style.transition='none';place(0);void wheel.offsetHeight;wheel.style.transition='';},{once:true});
    return;
  }
  if(cur===0&&digit===9){wheel.style.transition='none';place(10);void wheel.offsetHeight;wheel.style.transition='';}
  place(digit);
}
function odometer(el,value){
  if(!el)return;value=String(value);
  if(el.dataset.v===value)return;
  const first=el.dataset.v===undefined, instant=first||reducedMotion();
  el.dataset.v=value;el.classList.add('odo');
  const old=[...el.children], chars=[...value], next=[];
  chars.forEach((ch,i)=>{
    const prev=old[old.length-(chars.length-i)];
    if(ch>='0'&&ch<='9'){
      const col=prev&&prev.classList.contains('odo-col')?prev:odoColumn();
      if(col!==prev&&!instant)col.classList.add('enter');
      next.push([col,Number(ch)]);
    }else{
      const mark=prev&&prev.classList.contains('odo-mark')&&prev.textContent===ch?prev:document.createElement('span');
      if(mark!==prev){mark.className='odo-mark'+(instant?'':' enter');mark.textContent=ch;}
      next.push([mark,null]);
    }
  });
  el.replaceChildren(...next.map(([node])=>node));
  next.forEach(([node,digit])=>{if(digit!==null)turnWheel(node,digit,instant||node.dataset.d===undefined);});
}
function rollText(el,value){odometer(el,value);}
function flashCopied(btn){
  if(!btn)return;
  btn.classList.add('copied');clearTimeout(btn._copied);
  btn._copied=setTimeout(()=>btn.classList.remove('copied'),1600);
}
function toggleTheme(){const r=document.documentElement;
  const cur=r.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  const nx=cur==='dark'?'light':'dark';r.setAttribute('data-theme',nx);try{localStorage.setItem('deck-theme',nx)}catch(e){}}
(function(){try{const th=localStorage.getItem('deck-theme');if(th)document.documentElement.setAttribute('data-theme',th)}catch(e){}})();
const GUIDE_PROGRESS_KEY='slackagent.guide.progress.v1';
const GUIDE_SEEN_KEY='slackagent.guide.seen.v1';
const GUIDE_STEPS=['roles','slack','local','prompts','first-task'];
function guideCompleted(){
  try{const raw=JSON.parse(localStorage.getItem(GUIDE_PROGRESS_KEY)||'[]');
    return new Set(Array.isArray(raw)?raw.filter(s=>GUIDE_STEPS.includes(s)):[]);}catch(e){return new Set();}
}
function setGuideStep(step,done){
  if(!GUIDE_STEPS.includes(step))return;
  const completed=guideCompleted();
  if(done)completed.add(step);else completed.delete(step);
  try{localStorage.setItem(GUIDE_PROGRESS_KEY,JSON.stringify([...completed]))}catch(e){}
  renderGuide();
}
function renderGuide(){
  const completed=guideCompleted(), count=completed.size;
  document.querySelectorAll('[data-guide-step]').forEach(row=>{
    const done=completed.has(row.dataset.guideStep);
    row.classList.toggle('done',done);
    const box=row.querySelector('[data-guide-check]');if(box)box.checked=done;
  });
  rollText($('#guide-count'),String(count));
  const progress=$('#guide-progress');
  progress.setAttribute('aria-valuenow',String(count));
  const next=GUIDE_STEPS.find(step=>!completed.has(step));
  document.querySelectorAll('[data-gseg]').forEach(seg=>{
    seg.classList.toggle('done',completed.has(seg.dataset.gseg));
    seg.classList.toggle('current',seg.dataset.gseg===next);
    seg.title=t('guide.next.'+seg.dataset.gseg);
  });
  $('#guide-next').textContent=next?t('guide.next',{n:t('guide.next.'+next)}):t('guide.complete');
  const prompts=GUIDE_PROMPTS[LANG]||GUIDE_PROMPTS.en;
  document.querySelectorAll('[data-guide-prompt]').forEach(el=>{
    el.textContent=prompts[el.dataset.guidePrompt]||'';
  });
  document.querySelectorAll('[data-guide-label-key]').forEach(el=>{
    el.dataset.guideLabel=t(el.dataset.guideLabelKey);
  });
}
async function copyGuidePrompt(name){
  const prompts=GUIDE_PROMPTS[LANG]||GUIDE_PROMPTS.en, value=prompts[name]||'';
  if(!value)return;
  try{await copyGuideText(value);toast(t('guide.copied'));markCopied(name);}
  catch(e){
    if(selectGuidePrompt(name))toast(t('guide.copyselect'));
    else toast(t('guide.copyfail'),1);
  }
}
async function copyGuideText(value){
  try{
    if(navigator.clipboard&&navigator.clipboard.writeText){
      await navigator.clipboard.writeText(value);return;
    }
  }catch(e){}
  const area=document.createElement('textarea');
  area.value=value;area.setAttribute('readonly','');
  area.style.position='fixed';area.style.opacity='0';area.style.pointerEvents='none';
  document.body.appendChild(area);
  try{
    area.focus();area.select();
    if(!document.execCommand('copy'))throw new Error('copy unavailable');
  }finally{area.remove();}
}
function markCopied(name){
  const block=[...document.querySelectorAll('[data-guide-prompt]')]
    .find(el=>el.dataset.guidePrompt===name);
  flashCopied(block&&block.closest('details')&&block.closest('details').querySelector('summary .btn'));
}
function selectGuidePrompt(name){
  const block=[...document.querySelectorAll('[data-guide-prompt]')]
    .find(el=>el.dataset.guidePrompt===name);
  if(!block)return false;
  const details=block.closest('details');if(details)details.open=true;
  const range=document.createRange(), selection=window.getSelection();
  range.selectNodeContents(block);selection.removeAllRanges();selection.addRange(range);
  block.scrollIntoView({block:'nearest'});return true;
}
document.addEventListener('change',event=>{
  const box=event.target.closest&&event.target.closest('[data-guide-check]');
  if(!box)return;
  // only a user's own toggle plays the tick / strike / nudge sequence
  const row=box.closest('[data-guide-step]');
  if(row){row.classList.add('ticking');clearTimeout(row._tick);
    row._tick=setTimeout(()=>row.classList.remove('ticking'),1500);}
  setGuideStep(box.dataset.guideCheck,box.checked);
});
const TABS=['guide','mon','cfg','auth'];
async function ensureModels(){
  if(MODELS_READY)return;
  if(!MODELS_LOADING)MODELS_LOADING=j('/api/models').then(value=>{
    MODELS=value;MODELS_READY=true;
  }).catch(()=>{}).finally(()=>{MODELS_LOADING=null;});
  await MODELS_LOADING;
}
async function loadProtectedTab(tab){
  await ensureModels();
  if(!$('#panel-'+tab).classList.contains('on'))return;
  if(tab==='cfg')loadCfg();else if(tab==='auth')loadAuth();else loadLive();
}
function showTab(tab){if(!TABS.includes(tab))tab='guide';
  for(const k of TABS){$('#tab-'+k).classList.toggle('on',k===tab);
  $('#panel-'+k).classList.toggle('on',k===tab);}
  if(tab==='guide'){renderGuide();return;}
  ensureControlToken();try{localStorage.setItem(GUIDE_SEEN_KEY,'1')}catch(e){}
  loadProtectedTab(tab);}
function fmtIdle(s){return s<60?s+'s':s<3600?Math.floor(s/60)+'m':Math.floor(s/3600)+'h';}
function fmtBytes(n){
  n=Math.max(0,Number(n)||0);
  if(n<1024)return Math.round(n)+' B';
  if(n<1024*1024)return (n/1024).toFixed(1)+' KiB';
  if(n<1024*1024*1024)return (n/(1024*1024)).toFixed(1)+' MiB';
  return (n/(1024*1024*1024)).toFixed(1)+' GiB';
}
function fmtTokens(n){return Math.max(0,Number(n)||0).toLocaleString();}
function renderOwnerQuotas(raw){
  const entries=Object.entries((raw&&typeof raw==='object')?raw:{})
    .filter(([,q])=>q&&typeof q==='object').sort(([a],[b])=>a.localeCompare(b));
  const root=$('#owner-quotas');
  if(!entries.length){
    root.innerHTML=`<div class="empty">${t('quota.empty')}</div>`;
    return;
  }
  root.innerHTML=entries.map(([owner,q])=>{
    const used=Math.max(0,Number(q.total_tokens)||0);
    const reserved=Math.max(0,Number(q.active_reserved_tokens)||0);
    const finiteLimit=q.daily_total_token_limit!==null&&q.daily_total_token_limit!==undefined;
    const limit=finiteLimit?Math.max(0,Number(q.daily_total_token_limit)||0):null;
    const remaining=q.remaining_total_tokens===null||q.remaining_total_tokens===undefined
      ?null:Math.max(0,Number(q.remaining_total_tokens)||0);
    const pct=limit?Math.min(100,Math.round(((used+reserved)/limit)*100)):0;
    const tone=pct>=100?'crit':(pct>=80?'warn':'');
    const rows=(Array.isArray(q.breakdown)?q.breakdown:[]).map(row=>`
      <tr><td>${esc(row.agent||'—')}</td><td class="qruntime">${esc(row.runtime||'—')}</td>
      <td>${fmtTokens(row.total_tokens)}</td><td>${fmtTokens(row.turns)}</td>
      <td>${fmtTokens(row.denied)}</td><td>${fmtTokens(row.errors)}</td>
      <td>${fmtTokens(row.estimated_turns)}</td></tr>`).join('');
    return `<article class="quota-card">
      <div class="qhead"><span class="qowner">${esc(owner)}</span>
        <span class="qday">${esc(q.utc_day||'')}</span></div>
      <div class="quota-metrics">
        <div class="quota-metric"><div class="v">${fmtTokens(used)}</div><div class="k">${t('quota.used')}</div></div>
        <div class="quota-metric"><div class="v">${limit===null?'∞':fmtTokens(limit)}</div><div class="k">${t('quota.limit')}</div></div>
        <div class="quota-metric"><div class="v">${remaining===null?'∞':fmtTokens(remaining)}</div><div class="k">${t('quota.remaining')}</div></div>
        <div class="quota-metric"><div class="v">${fmtTokens(reserved)}</div><div class="k">${t('quota.reserved')}</div></div>
        <div class="quota-metric"><div class="v">${fmtTokens(q.denied)}</div><div class="k">${t('quota.denied')}</div></div>
        <div class="quota-metric"><div class="v">${fmtTokens(q.errors)}</div><div class="k">${t('quota.errors')}</div></div>
      </div>
      ${limit===null?'':`<div class="quota-bar" title="${pct}%"><span class="${tone}" style="width:${pct}%"></span></div>`}
      <table class="quota-breakdown"><thead><tr>
        <th>${t('quota.agent')}</th><th>${t('quota.runtime')}</th><th>${t('quota.used')}</th>
        <th>${t('quota.turns')}</th><th>${t('quota.denied')}</th>
        <th>${t('quota.errors')}</th><th>${t('quota.estimated')}</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </article>`;
  }).join('');
}

// in-place confirm (rare-ui delete-button): survives the 5s re-render, no dialog
const ARMED=Object.create(null);
const ICON_CHECK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path pathLength="1" d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const ICON_CROSS='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';
function armQuestion(a){
  if(a.kind==='runtime')return t('rt.confirm',{n:a.agent,r:a.value});
  if(a.kind==='restart')return t('rs.confirm',{n:a.agent});
  return t('worktree.confirm');
}
function armBar(key){
  const a=ARMED[key];if(!a)return '';
  const fresh=a.fresh;a.fresh=false;const q=armQuestion(a);
  return `<div class="confirm arm on${fresh?' fresh':''}" data-arm-key="${esc(key)}" role="group" aria-label="${esc(q)}">
    <span class="q">${esc(q)}</span><span class="spacer"></span>
    <button class="arm-btn yes" data-arm="yes" aria-label="${esc(t('btn.apply'))}">${ICON_CHECK}</button>
    <button class="arm-btn no" data-arm="no" aria-label="${esc(t('btn.cancel'))}">${ICON_CROSS}</button></div>`;
}
function arm(key,action,slot){
  ARMED[key]=Object.assign({},action,{fresh:true});
  if(!slot)return;
  slot.innerHTML=armBar(key);
  const no=slot.querySelector('[data-arm="no"]');if(no)no.focus();
}
function disarm(key,bar){
  delete ARMED[key];if(!bar)return;
  if(reducedMotion()){bar.remove();return;}
  bar.classList.add('leaving');setTimeout(()=>bar.remove(),200);
}
async function fireArmed(key,bar){
  const a=ARMED[key];if(!a)return;delete ARMED[key];
  bar.classList.add('confirmed');
  await new Promise(done=>setTimeout(done,reducedMotion()?0:420));
  if(a.kind==='runtime')await switchRuntime(a.agent,a.value);
  else if(a.kind==='restart')await restartSessions(a.agent);
  else if(a.kind==='worktree')await doRemoveWorktree(a.digest);
}
document.addEventListener('click',event=>{
  const btn=event.target.closest&&event.target.closest('[data-arm]');
  const bar=btn&&btn.closest('[data-arm-key]');
  if(!bar)return;
  if(btn.dataset.arm==='yes')fireArmed(bar.dataset.armKey,bar);else disarm(bar.dataset.armKey,bar);
});
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  document.querySelectorAll('[data-arm-key]').forEach(bar=>disarm(bar.dataset.armKey,bar));
});
function renderWorktrees(items){
  const root=$('#worktrees'), rows=Array.isArray(items)?items:[];
  const valid=rows.filter(w=>/^[0-9a-f]{64}$/.test(String(w.identity_digest||'')));
  if(!valid.length){
    root.innerHTML=`<div class="empty">${t('worktree.empty')}</div>`;
    return;
  }
  root.innerHTML=valid.map(w=>{
    const d=String(w.identity_digest), active=Math.max(0,Number(w.active_leases)||0);
    const status=String(w.status||'unknown'), removable=status==='ready'&&!active;
    const identity=[w.team_id,w.channel_id,w.root_thread_ts].filter(Boolean).join(' · ');
    const detail=[identity,w.path].filter(Boolean).join(' · ');
    return `<article class="worktree-row" data-digest="${d}">
      <div class="worktree-main">
        <div class="worktree-branch" title="${esc(w.branch||'')}">${esc(w.branch||d.slice(0,16))}</div>
        <div class="worktree-meta" title="${esc(detail)}">${esc(detail||d)}</div>
      </div>
      <div class="worktree-actions">
        <span class="state ${active?'work':(status==='ready'?'idle':'off')}">${active?t('worktree.active'):esc(status)}</span>
        ${status==='removed'?'':`<button class="btn text" ${removable?'':'disabled'} onclick="removeWorktree('${d}')">${t('worktree.remove')}</button>`}
      </div>
      <div class="arm-slot">${armBar('wt:'+d)}</div>
    </article>`;
  }).join('');
}
function removeWorktree(identityDigest){
  if(!/^[0-9a-f]{64}$/.test(String(identityDigest||'')))return;
  arm('wt:'+identityDigest,{kind:'worktree',digest:identityDigest},
    document.querySelector('.worktree-row[data-digest="'+identityDigest+'"] .arm-slot'));
}
async function doRemoveWorktree(identityDigest){
  if(!/^[0-9a-f]{64}$/.test(String(identityDigest||'')))return;
  try{
    const r=await j('/api/live/worktrees/'+identityDigest+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    if(r.ok){toast(t('worktree.removed'));loadLive();}
    else toast('✕ '+(r.error||t('toast.fail')),1);
  }catch(e){toast('✕ '+t('toast.fail'),1);}
}

const REPLY_LANGS=['中文','日本語','English'];
function langOpts(cur){const list=REPLY_LANGS.slice();if(cur&&!list.includes(cur))list.push(cur);
  return list.map(l=>`<option value="${esc(l)}"${l===cur?' selected':''}>${esc(l)}</option>`).join('');}
async function setReplyLang(n,l){
  const r=await j('/api/live/'+encodeURIComponent(n)+'/reply_language',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reply_language:l})});
  if(r.ok){toast(t('toast.lang',{n:n,l:r.reply_language}));loadLive();}else toast('✕ '+(r.error||t('toast.fail')),1);}
function effortOpts(rt,cur){const list=((MODELS.efforts||{})[rt]||['']).slice();
  if(cur&&!list.includes(cur))list.push(cur);
  const def=(MODELS.effort_default||{})[rt]||'';
  return list.map(e=>`<option value="${esc(e)}"${e===cur?' selected':''}>${e?esc(e):(def?t('def.as',{m:def}):t('def.plain'))}</option>`).join('');}
async function setEffort(n,e){
  const r=await j('/api/live/'+encodeURIComponent(n)+'/effort',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({effort:e})});
  if(r.ok){toast(t('toast.effort',{n:n,e:r.effort||t('def.plain')}));loadLive();}else toast('✕ '+(r.error||t('toast.fail')),1);}
function defLabel(rt){const c=(MODELS.current||{})[rt];return c?t('def.as',{m:c}):t('def.plain');}
function modelOpts(rt,cur){const list=(MODELS[rt]||['']).slice();
  if(cur&&!list.includes(cur))list.push(cur);
  return list.map(m=>`<option value="${esc(m)}"${m===cur?' selected':''}>${m?esc(m):defLabel(rt)}</option>`).join('');}
function stageCustomModel(n,el,root){
  const m=(el.value||'').trim();
  if(!m)return;
  stageModel(n,m,root);
}
async function loadIssues(){
  let d; try{d=await j('/api/issues');}catch(e){d={issues:[],error:'fetch failed'};}
  $('#tasks-repo').textContent=d.repo||'';
  const its=d.issues||[];
  if(d.error){$('#issues').innerHTML=`<div class="empty">${esc(d.error)}</div>`;return;}
  if(!its.length){$('#issues').innerHTML=`<div class="empty">${t('tasks.empty')}</div>`;return;}
  $('#issues').innerHTML=its.map(it=>{
    const status=(it.labels||[]).filter(l=>/^status:/.test(l)).map(l=>l.replace('status:','')).join(', ');
    const other=(it.labels||[]).filter(l=>!/^status:/.test(l));
    const asg=(it.assignees||[]).length?('@'+it.assignees.join(', @')):t('tasks.unassigned');
    const issueUrl=safeHttpUrl(it.url);
    const issueNumber='#'+esc(it.number);
    return `<div class="irow">
      ${issueUrl?`<a class="inum" href="${esc(issueUrl)}" target="_blank" rel="noopener noreferrer">${issueNumber}</a>`:`<span class="inum">${issueNumber}</span>`}
      <span class="ititle" title="${esc((it.repo||'')+' · '+it.title)}">${it.repo?`<small>${esc(it.repo)} · </small>`:''}${esc(it.title)}</span>
      <span class="ilabel">${esc(status||other.join(', '))}</span>
      <span class="iasg">${esc(asg)}</span></div>`;
  }).join('');
}
async function copyRules(){const p=$('#rules-pre');navigator.clipboard.writeText(p.textContent);toast(t('rules.copied'));
  flashCopied($('#rules-copy'));}
async function loadRules(){try{const d=await j('/api/channel-rules');$('#rules-pre').textContent=d.template||d.error||'';}catch(e){}}
async function loadLive(){
  loadIssues();
  const st=await j('/api/live/state'), on=st.online, ags=st.agents||[];
  $('#beacon').className='beacon '+(on?'live':'down');
  $('#beacon-t').textContent=on?t('status.live'):t('status.down');
  const liveRepos=[...new Set(ags.map(a=>a.github_repo).filter(Boolean))];
  $('#meta').textContent=on?`${t('meta.budget')} ${st.max_agent_rounds??'-'} · ${liveRepos.join(', ')||st.github_repo||'—'}`:t('meta.offline');
  const busy=ags.reduce((n,a)=>n+(a.busy_threads||0),0), sess=ags.reduce((n,a)=>n+(a.session_count||0),0);
  const figs=[['fig.agents',ags.length,0],['fig.busy',busy,busy>0],['fig.session',sess,0],
    ['fig.conn',ags.filter(a=>a.connected).length+' / '+ags.length,0]];
  const figRoot=$('#figs');
  if(figRoot.children.length!==figs.length||figRoot.dataset.lang!==LANG){
    figRoot.innerHTML=figs.map(([l])=>`<div class="fig"><div class="n"></div><div class="l">${t(l)}</div></div>`).join('');
    figRoot.dataset.lang=LANG;
  }
  figs.forEach(([,n,h],i)=>{const cell=figRoot.children[i].querySelector('.n');
    cell.classList.toggle('hi',!!h);rollText(cell,n);});
  renderOwnerQuotas(st.owner_usage||{});
  renderWorktrees(st.worktrees||[]);
  const tr=st.transcript||{};
  const trCap=tr.capacity||{};
  $('#transcript-health').innerHTML=[
    ['transcript.threads',tr.threads||0],['transcript.messages',tr.messages||0],
    ['transcript.persisted',tr.persisted_messages||0],
    ['transcript.memory',`${fmtBytes(tr.memory_bytes)}/${fmtBytes(trCap.memory_bytes)}`],
    ['transcript.database',`${fmtBytes(tr.persisted_bytes)}/${fmtBytes(trCap.persisted_bytes)}`],
    ['transcript.warm',tr.warm_threads||0],['transcript.partial',tr.partial_threads||0],
    ['transcript.evictions',tr.evicted_threads||0],
    ['transcript.backfills',tr.backfill_attempts||0],['transcript.calls',tr.backfill_calls||0],
    ['transcript.failures',tr.backfill_failures||0]]
    .map(([l,n])=>`<span class="metric">${t(l)} <b>${n}</b></span>`).join('');
  if(!ags.length){$('#roster').innerHTML=`<div class="node"><div class="empty">${t('empty.noagents')}</div></div>`;return;}
  $('#roster').innerHTML=ags.map((a,i)=>{
    const c=a.busy_threads>0?'work':(a.connected?'idle':'off');
    const s=a.busy_threads>0?t('st.busy'):(a.connected?t('st.idle'):t('st.off'));
    const dispModel=a.model?esc(a.model):((MODELS.current||{})[a.runtime]?esc((MODELS.current)[a.runtime]):t('model.def'));
    const pend=PENDING[a.name];
    const cfgPending=((a.config_reload||{}).pending||null);
    const rows=(a.threads||[]).slice(0,6).map(x=>`<div class="trow">
      <span class="tid" title="${esc(x.thread)}">${esc(x.thread.slice(-28))}</span>
      <span class="${x.busy?'run':'wait'}">${x.busy?t('th.run'):t('th.wait')}</span>
      <span class="num">${x.input_tokens.toLocaleString()} ${t('tk')}</span>
      <span class="num">${t('th.left')} ${x.budget_remaining??'–'} · ${fmtIdle(x.idle_seconds)}</span></div>`).join('')
      ||`<div class="empty">—</div>`;
    return `<div class="node" data-agent="${esc(a.name)}" data-runtime="${esc(a.runtime)}" style="animation-delay:${i*60}ms">
      <div class="head">
        <span class="nm">${esc(a.name)}</span>
        <span class="state ${c}">${s}</span>
        ${cfgPending?`<span class="state work" title="${esc((cfgPending.fields||[]).join(', '))}">${t('cfg.pending')}</span>`:''}
        <span class="rtline">${esc(a.runtime)} · ${dispModel}</span>
        <span class="aux">${t('aux',{s:a.session_count,p:a.patrol?'on':'off'})} · ${esc(a.github_repo||'no repo')}</span>
      </div>
      <div class="ctl">
        <span class="lbl">${t('lbl.runtime')}</span>
        <div class="seg">
          <button class="rt ${a.runtime==='claude'?'on':''}" data-action="runtime" data-value="claude">claude</button>
          <button class="rt ${a.runtime==='codex'?'on':''}" data-action="runtime" data-value="codex">codex</button>
          <button class="rt ${a.runtime==='openai'?'on':''}" data-action="runtime" data-value="openai">openai api</button>
        </div>
        <div class="rowbtns"><button class="btn line" data-action="restart">${t('btn.restart')}</button></div>
        <span class="lbl">${t('lbl.model')}</span>
        <div class="field">
          <select data-action="model">${modelOpts(a.runtime,a.model||'')}</select>
          ${a.runtime==='openai'?`<input type="text" class="model-custom" data-action="model-custom" placeholder="${esc(t('model.custom'))}" value="" title="${esc(t('model.custom.hint'))}">`:''}
          <span class="src">${esc((MODELS.sources||{})[a.runtime]||'')}${(a.runtime==='openai'&&a.openai_base_url)?` · ${esc(a.openai_base_url)}`:((MODELS.openai_base_url)?` · ${esc(MODELS.openai_base_url)}`:'')}</span>
        </div><span></span>
        <span class="lbl">${t('lbl.effort')}</span>
        <div class="field">
          <select data-action="effort">${effortOpts(a.runtime,a.effort||'')}</select>
        </div><span></span>
        <span class="lbl">${t('lbl.replylang')}</span>
        <div class="field">
          <select data-action="reply-language">${langOpts(a.reply_language||'')}</select>
        </div><span></span>
        <div class="arm-slot">${armBar('agent:'+a.name)}</div>
        <div class="confirm ${pend!==undefined?'on':''}">
          <span class="q">${esc(t('confirm.q',{n:a.name,m:pend!==undefined?(pend||t('def.plain')):''}))}</span>
          <span class="spacer"></span>
          <button class="btn solid" data-action="model-apply">${t('btn.apply')}</button>
          <button class="btn text" data-action="model-cancel">${t('btn.cancel')}</button>
        </div>
      </div>
      <div class="threads"><div class="tcap">${t('threads.cap',{a:Math.min((a.threads||[]).length,6),b:(a.threads||[]).length})}</div>${rows}</div>
    </div>`;}).join('');
}
function stageModel(n,m,root){PENDING[n]=m;const cf=root.querySelector('.confirm');
  cf.querySelector('.q').textContent=t('confirm.q',{n:n,m:m||t('def.plain')});cf.classList.add('on','fresh');}
function cancelModel(n,root){delete PENDING[n];root.querySelector('.confirm').classList.remove('on');loadLive();}
async function applyModel(n){const m=PENDING[n]||'';
  const r=await j('/api/live/'+encodeURIComponent(n)+'/model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:m})});
  if(r.ok){delete PENDING[n];toast(t('toast.sw',{n:n,m:m||t('def.plain')}));loadLive();}else toast('✕ '+(r.error||t('toast.swf')),1);}
function askRuntime(n,rt,cur,root){if(rt===cur)return;
  arm('agent:'+n,{kind:'runtime',agent:n,value:rt},root&&root.querySelector('.arm-slot'));}
function askRestart(n,root){arm('agent:'+n,{kind:'restart',agent:n},root&&root.querySelector('.arm-slot'));}
async function switchRuntime(n,rt){
  const r=await j('/api/live/'+encodeURIComponent(n)+'/runtime',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({runtime:rt})});
  if(r.ok){toast(t('toast.rt',{n:n,r:r.runtime}));loadLive();}else toast('✕ '+(r.error||t('toast.fail')),1);}
async function restartSessions(n){
  const r=await j('/api/live/'+encodeURIComponent(n)+'/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  if(r.ok){toast(t('toast.rs',{n:n,c:r.cleared_sessions}));loadLive();}else toast('✕ '+(r.error||t('toast.fail')),1);}

function pickNewRt(rt){NEWRT=rt;document.querySelectorAll('#new-rt-wrap .rt').forEach(b=>b.classList.toggle('on',b.dataset.rt===rt));}
function cardPreview(a){
  const c=(a.card||'').trim();
  if(c) return esc(c.split(String.fromCharCode(10))[0]);
  const p=(a.persona||'').split(String.fromCharCode(10))[0];
  return esc(p)||'<span style="opacity:.5">—</span>';
}
function watchCard(el, longId){
  const v=el.value||'';
  const long=v.length>400||v.split(String.fromCharCode(10)).length>6;
  el.classList.toggle('warn', long);
  const h=typeof longId==='string'?document.getElementById(longId):longId;
  if(h) h.classList.toggle('on', long);
}
async function loadCfg(){loadRules();const st=await j('/api/state');
  $('#agents').innerHTML=st.agents.map(a=>`
  <div class="card" data-agent="${esc(a.name)}">
    <div class="head">
      <span class="nm">${esc(a.name)}</span>
      <span class="chip">${esc(a.runtime)} · ${a.optional?t('cfg.optional'):t('cfg.required')}</span>
      <span class="tok">bot <span class="${a.bot_set?'ok':'ng'}">${a.bot_set?'✓':'—'}</span>
        · app <span class="${a.app_set?'ok':'ng'}">${a.app_set?'✓':'—'}</span>
        ${a.runtime==='openai'?` · api <span class="${a.openai_api_key_set||a.openai_base_url?'ok':'ng'}">${a.openai_api_key_set||a.openai_base_url?'✓':'—'}</span>${a.openai_base_url?` · proxy <span class="ok" title="${esc(a.openai_base_url)}">✓</span>`:''}`:''}</span>
      <span class="aux" style="margin-left:auto"><button class="btn line" data-action="setup">${a.bot_set&&a.app_set?t('cfg.retoken'):t('cfg.setup')}</button></span>
    </div>
    <div class="persona">
      <div>${cardPreview(a)}</div>
      <button class="btn text" style="padding-left:0" data-action="edit-persona">✎ ${t('cfg.editpersona')}</button>
      <div class="pedit" style="display:none;margin-top:8px">
        <span class="lbl" style="display:block;margin:0 0 4px">${t('cfg.card')}</span>
        <div class="hint">${t('cfg.cardhint')}</div>
        <textarea class="card-input" rows="4" data-action="card-watch">${esc(a.card||'')}</textarea>
        <div class="cardlong">${t('cfg.cardlong')}</div>
        <span class="lbl" style="display:block;margin:12px 0 4px">${t('cfg.persona')}</span>
        <textarea class="persona-input" rows="7">${esc(a.persona||'')}</textarea>
        <div style="margin-top:8px"><button class="btn solid" data-action="save-persona">${t('cfg.save')}</button>
        <span class="msg persona-msg"></span></div>
      </div>
    </div>
    <div class="wsrow">
      <span class="lbl">${t('ws.title')}</span>
      <span class="wspath">${a.workspace?esc(a.workspace):`<span class="faint">${t('ws.unset')}</span>`}</span>
      ${a.github_repo?`<span class="chip">${esc(a.github_repo)}</span>`:''}
      <button class="btn text" data-action="edit-workspace">✎ ${t('ws.edit')}</button>
    </div>
    <div class="wsedit" hidden>
      <div class="grid2">
        <div><span class="lbl">${t('ws.path')}</span>
          <input class="ws-input" value="${esc(a.workspace||'')}" placeholder="~/workspace/my-repo" spellcheck="false" autocomplete="off"></div>
        <div><span class="lbl">${t('ws.repo')}</span>
          <input class="repo-input" value="${esc(a.github_repo_explicit||'')}" placeholder="${esc(a.github_repo||'OWNER/REPO')}" spellcheck="false" autocomplete="off"></div>
      </div>
      <div class="wsinfo"></div>
      <div class="rowbtns" style="margin-top:12px">
        <button class="btn line" data-action="check-workspace">${t('ws.check')}</button>
        <button class="btn solid" data-action="save-workspace">${t('cfg.save')}</button>
      </div>
      <span class="msg ws-msg"></span>
    </div>
    <div class="wiz">
      <p>${t('wz.s1')}</p>
      <pre class="manifest">…</pre>
      <div class="rowbtns">
        <a class="btn solid create-app" href="https://api.slack.com/apps?new_app=1" target="_blank" rel="noopener noreferrer">${t('wz.create')} ↗</a>
        <button class="btn text" data-action="copy-manifest">${t('wz.copy')}</button>
      </div>
      <p>${t('wz.s2')}</p>
      <span class="lbl">Bot User OAuth Token</span><input class="bot-token" placeholder="xoxb-...">
      <span class="lbl">App-Level Token</span><input class="app-token" placeholder="xapp-...">
      <div style="margin-top:14px"><button class="btn solid" data-action="save-tokens">${t('wz.save')}</button></div>
      <div class="msg token-msg"></div>
    </div>
  </div>`).join('');
  document.querySelectorAll('#agents .card').forEach(card=>{
    const el=card.querySelector('.card-input');
    if(el)watchCard(el,card.querySelector('.cardlong'));
  });
}
function editP(card){const e=card.querySelector('.pedit');e.style.display=e.style.display==='none'?'block':'none';}
function editWorkspace(card){const e=card.querySelector('.wsedit');e.hidden=!e.hidden;e.classList.toggle('open',!e.hidden);
  if(!e.hidden)card.querySelector('.ws-input').focus();}
function workspaceBody(card,dryRun){
  return JSON.stringify({workspace:card.querySelector('.ws-input').value,
    github_repo:card.querySelector('.repo-input').value,dry_run:!!dryRun});
}
function renderWorkspaceFacts(card,r){
  const info=card.querySelector('.wsinfo');
  if(!r.ok){info.innerHTML=`<span class="ng">✕ ${esc(r.error||t('savefail'))}</span>`;return;}
  const parts=[`<span class="ok">✓ ${esc(r.path)}</span>`];
  if(!r.git)parts.push(`<span class="warn">${t('ws.nogit')}</span>`);
  else{
    if(r.branch)parts.push(`${t('ws.branch')} ${esc(r.branch)}`);
    if(r.origin_repo)parts.push(`origin ${esc(r.origin_repo)}`);
  }
  if(r.mismatch){
    parts.push(`<span class="warn">${t('ws.mismatch')}</span>`);
    parts.push(`<button class="btn text" data-action="use-origin" data-repo="${esc(r.origin_repo)}">${t('ws.useorigin',{r:esc(r.origin_repo)})}</button>`);
  }
  info.innerHTML=parts.join(' · ');
}
async function checkWorkspace(n,card){
  const r=await j('/api/agents/'+encodeURIComponent(n)+'/workspace',{method:'POST',
    headers:{'Content-Type':'application/json'},body:workspaceBody(card,true)});
  renderWorkspaceFacts(card,r);
}
async function saveWorkspace(n,card){const m=card.querySelector('.ws-msg');m.textContent='…';m.className='msg ws-msg';
  const r=await j('/api/agents/'+encodeURIComponent(n)+'/workspace',{method:'POST',
    headers:{'Content-Type':'application/json'},body:workspaceBody(card,false)});
  renderWorkspaceFacts(card,r);
  if(r.ok){m.className='msg '+(savedLiveOk(r,n)?'ok':'ng');m.textContent=savedMsg(r,n);
    setTimeout(loadCfg,1200);}
  else{m.className='msg ng';m.textContent='✕ '+(r.error||t('savefail'));}}
function savedMsg(r,n){const rl=r.reload||{};
  const ar=((rl.agents||{})[n]||{});
  if(rl.ok&&(ar.failed||[]).length)return t('cfg.failed')+': '+ar.failed.join(', ');
  if(rl.ok&&(ar.deferred||[]).length)return t('cfg.deferred');
  if(rl.ok&&!(rl.skipped_new||[]).includes(n)&&!(ar.restart_required||[]).length
    &&!(rl.global_restart_required||[]).length)return t('cfg.reloaded');
  if((rl.global_restart_required||[]).includes('worktree_root')
    ||(ar.restart_required||[]).includes('worktree_root'))
    return t('saved')+' · '+t('cfg.worktreerootrestart');
  return t('saved')+' · '+t('cfg.restarthint');}
function savedLiveOk(r,n){const ar=(((r.reload||{}).agents||{})[n]||{});
  return !((ar.failed||[]).length);}
async function saveP(n,card){const m=card.querySelector('.persona-msg');m.textContent='…';m.className='msg persona-msg';
  const r=await j('/api/agents/'+encodeURIComponent(n)+'/persona',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({persona:card.querySelector('.persona-input').value,card:card.querySelector('.card-input').value})});
  if(r.ok){m.className='msg '+(savedLiveOk(r,n)?'ok':'ng');m.textContent=savedMsg(r,n);loadCfg();}
  else{m.className='msg ng';m.textContent='✕ '+(r.error||t('savefail'));}}
async function toggle(n,card){const el=card.querySelector('.wiz');el.classList.toggle('on');
  if(!el.classList.contains('on'))return;
  card.querySelector('.manifest').textContent=await apiFetch('/api/manifest/'+encodeURIComponent(n)).then(r=>r.text());
  // Slack opens "create from manifest" already filled in with this agent's manifest
  try{const m=await j('/api/manifest/'+encodeURIComponent(n)+'?format=json');
    if(m&&m.manifest)card.querySelector('.create-app').href='https://api.slack.com/apps?new_app=1&manifest_json='
      +encodeURIComponent(JSON.stringify(m.manifest));}catch(e){}
}
function copyMf(card){navigator.clipboard.writeText(card.querySelector('.manifest').textContent);toast(t('tok.copied'));}
async function saveTokens(n,card){const m=card.querySelector('.token-msg');m.textContent='…';m.className='msg token-msg';
  const r=await j('/api/tokens',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:n,bot_token:card.querySelector('.bot-token').value,app_token:card.querySelector('.app-token').value})});
  if(r.ok){m.className='msg ok';m.textContent=t('saved')+' ('+(r.bot_detail||'')+')';loadCfg();}
  else{m.className='msg ng';m.textContent='✕ '+(r.error||'')+' bot='+(r.bot_detail||'-')+' app='+(r.app_detail||'-');}}
async function addAgent(){const m=$('#new-msg');const nm=($('#new-name').value||'').trim();
  const r=await j('/api/agents',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:nm,optional:true,runtime:NEWRT,persona:$('#new-persona').value,card:$('#new-card').value,workspace:$('#new-ws').value})});
  if(r.ok){m.className='msg '+(savedLiveOk(r,nm)?'ok':'ng');m.textContent=savedMsg(r,nm);loadCfg();}else{m.className='msg ng';m.textContent='✕ '+(r.error||t('savefail'));}}

document.addEventListener('click',event=>{
  const control=event.target.closest&&event.target.closest('[data-action]');
  const root=control&&control.closest('[data-agent]');
  if(!control||!root)return;
  const n=root.dataset.agent, action=control.dataset.action;
  if(action==='runtime')askRuntime(n,control.dataset.value,root.dataset.runtime,root);
  else if(action==='restart')askRestart(n,root);
  else if(action==='model-apply')applyModel(n);
  else if(action==='model-cancel')cancelModel(n,root);
  else if(action==='setup')toggle(n,root);
  else if(action==='edit-persona')editP(root);
  else if(action==='edit-workspace')editWorkspace(root);
  else if(action==='check-workspace')checkWorkspace(n,root);
  else if(action==='save-workspace')saveWorkspace(n,root);
  else if(action==='use-origin'){root.querySelector('.repo-input').value=control.dataset.repo||'';checkWorkspace(n,root);}
  else if(action==='save-persona')saveP(n,root);
  else if(action==='copy-manifest')copyMf(root);
  else if(action==='save-tokens')saveTokens(n,root);
});
document.addEventListener('change',event=>{
  const control=event.target.closest&&event.target.closest('[data-action]');
  const root=control&&control.closest('[data-agent]');
  if(!control||!root)return;
  const n=root.dataset.agent, action=control.dataset.action;
  if(action==='model')stageModel(n,control.value,root);
  else if(action==='model-custom')stageCustomModel(n,control,root);
  else if(action==='effort')setEffort(n,control.value);
  else if(action==='reply-language')setReplyLang(n,control.value);
});
document.addEventListener('keydown',event=>{
  if(event.key!=='Enter')return;
  const control=event.target.closest&&event.target.closest('[data-action="model-custom"]');
  const root=control&&control.closest('[data-agent]');
  if(!control||!root)return;
  event.preventDefault();
  stageCustomModel(root.dataset.agent,control,root);
});
document.addEventListener('input',event=>{
  const control=event.target;
  if(!control.matches||!control.matches('[data-action="card-watch"]'))return;
  const root=control.closest('[data-agent]');
  if(root)watchCard(control,root.querySelector('.cardlong'));
});

/* --- auth panel: Claude / Codex / GitHub --- */
let AUTH_POLL=null;
function stopAuthPoll(){if(AUTH_POLL){clearInterval(AUTH_POLL);AUTH_POLL=null;}}
function whereLabel(mode){
  return mode==='docker'?t('auth.docker'):mode==='host'?t('auth.host'):t('auth.nocli');
}
function showAuthSteps(prefix, step){
  for(const n of [1,2,3]){
    const el=$('#'+prefix+'-step'+n);
    if(el) el.style.display=(n===step)?'':'none';
  }
}
function setBadge(id, on, onText, offText){
  const el=$(id); el.className='state '+(on?'idle':'off');
  el.textContent=on?onText:offText;
}
function showVerified(prefix, detail){
  showAuthSteps(prefix, 3);
  $('#'+prefix+'-verified').textContent=t('auth.verified');
  $('#'+prefix+'-verified-detail').textContent=detail||'';
}
async function loadAuth(){
  stopAuthPoll();
  const clSt=$('#cl-state'), clMeta=$('#cl-meta');
  const cxSt=$('#cx-state'), cxMeta=$('#cx-meta');
  const ghSt=$('#gh-state');
  clSt.className='state'; clSt.textContent=t('auth.checking');
  cxSt.className='state'; cxSt.textContent=t('auth.checking');
  ghSt.className='state'; ghSt.textContent=t('auth.checking');
  let r; try{r=await j('/api/auth/state');}catch(e){
    clSt.textContent=t('auth.failed'); cxSt.textContent=t('auth.failed'); ghSt.textContent=t('auth.failed');
    return;
  }
  const c=r.claude||{}, x=r.codex||{}, g=r.gh||{};

  setBadge('#cl-state', !!c.logged_in, t('auth.in'), t('auth.out'));
  clMeta.textContent=whereLabel(c.mode)
    +(c.auth_method&&c.auth_method!=='none'?' · '+c.auth_method:'')
    +(c.error?' · '+c.error:'');
  $('#cl-start').disabled=!c.available;
  if(c.pending && c.url){
    $('#cl-url').href=safeHttpUrl(c.url)||'#'; showAuthSteps('cl', 2);
  } else {
    showAuthSteps('cl', 1); $('#cl-msg').textContent=''; $('#cl-msg2').textContent=''; $('#cl-code').value='';
  }

  setBadge('#cx-state', !!x.logged_in, t('auth.in'), t('auth.out'));
  cxMeta.textContent=whereLabel(x.mode)
    +(x.auth_method?' · '+x.auth_method:'')
    +(x.error?' · '+x.error:'');
  $('#cx-start').disabled=!x.available;
  if(x.pending && x.device_code){
    $('#cx-url').href=safeHttpUrl(x.url)||'#'; $('#cx-code').textContent=x.device_code;
    showAuthSteps('cx', 2); $('#cx-msg2').textContent=t('auth.waiting');
    startDevicePoll('codex');
  } else {
    showAuthSteps('cx', 1); $('#cx-msg').textContent='';
  }

  const ghOn=!!(g.token_set||g.host_logged_in);
  setBadge('#gh-state', ghOn, g.token_set?t('auth.set'):t('auth.in'), t('auth.unset'));
  const ghBits=[];
  if(g.host_user) ghBits.push(t('auth.user',{u:g.host_user}));
  if(g.token_set) ghBits.push(t('auth.set'));
  else if(g.host_logged_in) ghBits.push(t('auth.in')+' (host)');
  if(ghBits.length) $('#gh-meta').textContent=ghBits.join(' · ');
  else $('#gh-meta').textContent=t('auth.ghsub');
  $('#gh-start').disabled=!g.available;
  $('#gh-import').style.display=g.host_gh?'':'none';
  if(g.pending && g.device_code){
    $('#gh-url').href=safeHttpUrl(g.url)||'#'; $('#gh-code').textContent=g.device_code;
    showAuthSteps('gh', 2); $('#gh-msg2').textContent=t('auth.waiting');
    startDevicePoll('gh');
  } else {
    showAuthSteps('gh', 1); $('#gh-msg').textContent=''; $('#gh-token').value='';
  }
}
function startDevicePoll(kind){
  stopAuthPoll();
  AUTH_POLL=setInterval(()=>deviceWait(kind,0), 2500);
}
async function deviceWait(kind, wait){
  const path=kind==='codex'?'/api/auth/codex/wait':'/api/auth/gh/wait';
  let r; try{
    r=await j(path,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({wait:wait||0})});
  }catch(e){return;}
  if(r.pending){
    const prefix=kind==='codex'?'cx':'gh';
    $('#'+prefix+'-msg2').textContent=t('auth.waiting');
    return;
  }
  stopAuthPoll();
  if(r.ok){
    const prefix=kind==='codex'?'cx':'gh';
    const detail=kind==='codex'
      ? t('auth.verified.detail',{m:(r.codex&&r.codex.auth_method)||'codex'})
      : t('auth.verified.detail',{m:(r.gh&&r.gh.host_user)||'GH_TOKEN'});
    toast(t('auth.ok'));
    showVerified(prefix, detail);
    // Refresh badges without resetting verified step
    try{
      const st=await j('/api/auth/state');
      if(kind==='codex'){
        const x=st.codex||{};
        setBadge('#cx-state', !!x.logged_in, t('auth.in'), t('auth.out'));
        $('#cx-meta').textContent=whereLabel(x.mode)+(x.auth_method?' · '+x.auth_method:'');
      } else {
        const g=st.gh||{};
        setBadge('#gh-state', !!(g.token_set||g.host_logged_in), g.token_set?t('auth.set'):t('auth.in'), t('auth.unset'));
      }
    }catch(e){}
  } else {
    const prefix=kind==='codex'?'cx':'gh';
    showAuthSteps(prefix, 1);
    $('#'+prefix+'-msg').className='msg ng';
    $('#'+prefix+'-msg').textContent='✕ '+(r.error||r.message||t('savefail'));
  }
}
async function claudeStart(){
  stopAuthPoll();
  const m=$('#cl-msg'); m.className='msg'; m.textContent=t('auth.starting');
  $('#cl-start').disabled=true;
  let r; try{r=await j('/api/auth/claude/start',{method:'POST'});}
  catch(e){r={ok:false,error:String(e)};}
  $('#cl-start').disabled=false;
  if(!r.ok){m.className='msg ng'; m.textContent='✕ '+(r.error||t('savefail')); return;}
  const authUrl=safeHttpUrl(r.url);
  if(!authUrl){m.className='msg ng';m.textContent='✕ invalid authorize URL';return;}
  m.textContent=''; $('#cl-url').href=authUrl; $('#cl-code').value='';
  showAuthSteps('cl', 2);
  window.open(authUrl,'_blank','noopener'); $('#cl-code').focus();
}
async function claudeSubmit(){
  const code=$('#cl-code').value.trim(), m=$('#cl-msg2');
  if(!code){m.className='msg ng'; m.textContent=t('auth.needcode'); return;}
  m.className='msg'; m.textContent=t('auth.verifying'); $('#cl-submit').disabled=true;
  let r; try{r=await j('/api/auth/claude/code',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code})});}
  catch(e){r={ok:false,error:String(e)};}
  $('#cl-submit').disabled=false;
  if(r.ok){
    const c=r.claude||{};
    setBadge('#cl-state', true, t('auth.in'), t('auth.out'));
    $('#cl-meta').textContent=whereLabel(c.mode)
      +(c.auth_method&&c.auth_method!=='none'?' · '+c.auth_method:'');
    toast(t('auth.ok'));
    showVerified('cl', t('auth.verified.detail',{m:c.auth_method||r.message||'claude'}));
  } else {
    m.className='msg ng'; m.textContent='✕ '+(r.error||r.message||t('savefail'));
  }
}
async function claudeCancel(){await j('/api/auth/claude/cancel',{method:'POST'}); loadAuth();}
async function codexStart(){
  stopAuthPoll();
  const m=$('#cx-msg'); m.className='msg'; m.textContent=t('auth.starting');
  $('#cx-start').disabled=true;
  let r; try{r=await j('/api/auth/codex/start',{method:'POST'});}
  catch(e){r={ok:false,error:String(e)};}
  $('#cx-start').disabled=false;
  if(!r.ok){m.className='msg ng'; m.textContent='✕ '+(r.error||t('savefail')); return;}
  const authUrl=safeHttpUrl(r.url);
  if(!authUrl){m.className='msg ng';m.textContent='✕ invalid authorize URL';return;}
  m.textContent=''; $('#cx-url').href=authUrl; $('#cx-code').textContent=r.device_code||'';
  showAuthSteps('cx', 2); $('#cx-msg2').className='msg'; $('#cx-msg2').textContent=t('auth.waiting');
  window.open(authUrl,'_blank','noopener');
  startDevicePoll('codex');
  deviceWait('codex', 8);
}
async function codexCancel(){stopAuthPoll(); await j('/api/auth/codex/cancel',{method:'POST'}); loadAuth();}
async function ghStart(){
  stopAuthPoll();
  const m=$('#gh-msg'); m.className='msg'; m.textContent=t('auth.starting');
  $('#gh-start').disabled=true;
  let r; try{r=await j('/api/auth/gh/start',{method:'POST'});}
  catch(e){r={ok:false,error:String(e)};}
  $('#gh-start').disabled=false;
  if(!r.ok){m.className='msg ng'; m.textContent='✕ '+(r.error||t('savefail')); return;}
  const authUrl=safeHttpUrl(r.url);
  if(!authUrl){m.className='msg ng';m.textContent='✕ invalid authorize URL';return;}
  m.textContent=''; $('#gh-url').href=authUrl; $('#gh-code').textContent=r.device_code||'';
  showAuthSteps('gh', 2); $('#gh-msg2').className='msg'; $('#gh-msg2').textContent=t('auth.waiting');
  window.open(authUrl,'_blank','noopener');
  startDevicePoll('gh');
  deviceWait('gh', 8);
}
async function ghCancel(){stopAuthPoll(); await j('/api/auth/gh/cancel',{method:'POST'}); loadAuth();}
async function ghSave(){
  const tok=$('#gh-token').value.trim(), m=$('#gh-msg');
  if(!tok){m.className='msg ng'; m.textContent=t('auth.needtoken'); return;}
  m.className='msg'; m.textContent='';
  const r=await j('/api/auth/gh-token',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
  if(r.ok){$('#gh-token').value=''; toast(t('auth.ghsaved'));
    showVerified('gh', t('auth.verified.detail',{m:'GH_TOKEN'}));
    setBadge('#gh-state', true, t('auth.set'), t('auth.unset'));
  } else {m.className='msg ng'; m.textContent='✕ '+(r.error||t('savefail'));}
}
async function ghImport(){
  const m=$('#gh-msg'); m.className='msg'; m.textContent=t('auth.importing');
  const r=await j('/api/auth/gh-token/import',{method:'POST'});
  if(r.ok){toast(t('auth.ghsaved'));
    showVerified('gh', t('auth.verified.detail',{m:'GH_TOKEN'}));
    setBadge('#gh-state', true, t('auth.set'), t('auth.unset'));
  } else {m.className='msg ng'; m.textContent='✕ '+(r.error||t('savefail'));}
}

(async()=>{applyI18n();let seen=false;try{seen=localStorage.getItem(GUIDE_SEEN_KEY)==='1'}catch(e){}
  showTab(seen?'mon':'guide');
  setInterval(()=>{if($('#auto').checked&&$('#panel-mon').classList.contains('on')&&!Object.keys(PENDING).length)loadLive();},5000);})();
</script></body></html>
"""


@web.middleware
async def host_guard(request: web.Request, handler: Any) -> web.StreamResponse:
    """Accept only Host headers for 127.0.0.1 / localhost (mitigate DNS rebinding)."""
    host = (request.host or "").split(":")[0]
    if host not in {"127.0.0.1", "localhost"}:
        raise web.HTTPForbidden(text="forbidden host")
    return await handler(request)


@web.middleware
async def security_headers(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    def harden(response: web.StreamResponse) -> web.StreamResponse:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    try:
        response = await handler(request)
    except web.HTTPException as exc:
        harden(exc)
        raise
    except Exception:
        logger.exception(
            "unhandled webui request path=%s",
            request.path,
        )
        return harden(
            web.Response(
                status=500,
                text="internal server error",
                content_type="text/plain",
            )
        )
    return harden(response)


def _principal_controls_node(
    request: web.Request, raw: dict[str, Any]
) -> bool:
    actor = _principal(request)
    if actor.is_admin:
        return True
    local_owners = {
        owner
        for entry in _local_entries(raw)
        if (owner := _entry_owner(entry, raw))
    }
    return local_owners == {actor.user_id}


def make_app(
    *,
    authenticator: ControlAuthenticator | None = None,
) -> web.Application:
    if authenticator is None:
        env = read_env_file()
        env.update(os.environ)
        authenticator = _build_webui_authenticator(read_yaml(), env)
    # Keep control-plane credentials in the in-memory resolver only. WebUI
    # launches gh/git/Claude/Codex helpers, so its child environment must not
    # retain the bearer values either.
    authenticator.scrub_environment(os.environ)

    @web.middleware
    async def authenticate_control_request(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        if request.path in {"/", "/healthz"}:
            return await handler(request)
        actor = authenticator.authenticate(
            request.headers.get("Authorization")
        )
        if actor is None:
            return web.json_response(
                {"ok": False, "error": "authentication required"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        request[_CONTROL_PRINCIPAL_KEY] = actor
        if (
            request.path.startswith("/api/auth/")
            and not _principal_controls_node(request, read_yaml())
        ):
            raise web.HTTPForbidden(text="forbidden")
        authorization_token = _CONTROL_AUTHORIZATION.set(
            request.headers.get("Authorization", "")
        )
        try:
            return await handler(request)
        finally:
            _CONTROL_AUTHORIZATION.reset(authorization_token)

    app = web.Application(
        middlewares=[
            security_headers,
            host_guard,
            authenticate_control_request,
        ]
    )
    app.router.add_get("/", h_index)
    app.router.add_get("/healthz", h_health)
    app.router.add_get("/api/state", h_state)
    app.router.add_get("/api/models", h_models)
    app.router.add_get("/api/manifest/{name}", h_manifest)
    app.router.add_post("/api/validate", h_validate)
    app.router.add_post("/api/tokens", h_save_tokens)
    app.router.add_post("/api/agents", h_save_agent)
    app.router.add_post("/api/agents/{name}/persona", h_update_persona)
    app.router.add_post("/api/agents/{name}/workspace", h_update_workspace)
    app.router.add_get("/api/issues", h_issues)
    app.router.add_get("/api/channel-rules", h_channel_rules)
    app.router.add_get("/api/live/state", h_live_state)
    app.router.add_post(
        "/api/live/worktrees/{identity_digest}/remove",
        h_live_remove_worktree,
    )
    app.router.add_post("/api/live/reload", h_live_reload)
    app.router.add_post("/api/live/{name}/model", h_live_set_model)
    app.router.add_post("/api/live/{name}/runtime", h_live_set_runtime)
    app.router.add_post("/api/live/{name}/restart", h_live_restart)
    app.router.add_post("/api/live/{name}/reply_language", h_live_set_reply_language)
    app.router.add_post("/api/live/{name}/effort", h_live_set_effort)
    app.router.add_get("/api/auth/state", h_auth_state)
    app.router.add_post("/api/auth/claude/start", h_claude_login_start)
    app.router.add_post("/api/auth/claude/code", h_claude_login_code)
    app.router.add_post("/api/auth/claude/cancel", h_claude_login_cancel)
    app.router.add_post("/api/auth/codex/start", h_codex_login_start)
    app.router.add_post("/api/auth/codex/wait", h_codex_login_wait)
    app.router.add_post("/api/auth/codex/cancel", h_codex_login_cancel)
    app.router.add_post("/api/auth/gh/start", h_gh_login_start)
    app.router.add_post("/api/auth/gh/wait", h_gh_login_wait)
    app.router.add_post("/api/auth/gh/cancel", h_gh_login_cancel)
    app.router.add_post("/api/auth/gh-token", h_gh_token)
    app.router.add_post("/api/auth/gh-token/import", h_gh_token_import)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("WEBUI_PORT", "8765"))
    print(f"⚙️  SlackAgentTeam config console: http://127.0.0.1:{port}")
    web.run_app(make_app(), host="127.0.0.1", port=port)
