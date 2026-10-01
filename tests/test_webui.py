"""Regression tests for webui INDEX_HTML.

Guards a real bug class: JS embedded in a Python triple-quoted string where
double escaping turned `\\n` inside a JS string literal into a real newline ->
the whole <script> became a syntax error -> setLang undefined -> language switch broken.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

import webui


def test_configured_github_repos_follow_agent_default_global_precedence():
    raw = {
        "github": {"repo": "acme/global"},
        "defaults": {"github_repo": "acme/default"},
        "agents": [
            {"name": "a", "github_repo": "acme/a"},
            {"name": "b"},
            {"name": "c", "github_repo": ""},
            {"name": "d", "github_repo": "acme/a"},
        ],
    }

    assert webui._configured_github_repos(raw) == [
        "acme/a",
        "acme/default",
    ]

    del raw["defaults"]["github_repo"]
    assert webui._configured_github_repos(raw) == [
        "acme/a",
        "acme/global",
    ]


def test_issues_cache_has_hard_limit(monkeypatch):
    current = {"repo": "acme/repo-0"}
    monkeypatch.setattr(
        webui,
        "read_yaml",
        lambda: {
            "agents": [
                {"name": "a", "github_repo": current["repo"]}
            ]
        },
    )
    monkeypatch.setattr(webui.shutil, "which", lambda _name: "/bin/gh")

    async def fake_issues(repo):
        return [{"number": 1, "title": repo, "url": "https://example.test"}], ""

    monkeypatch.setattr(webui, "_gh_issues", fake_issues)
    webui._issues_cache.clear()

    async def scenario():
        for index in range(webui.ISSUES_CACHE_MAX + 7):
            current["repo"] = f"acme/repo-{index}"
            await webui.h_issues(None)

    asyncio.run(scenario())
    assert len(webui._issues_cache) <= webui.ISSUES_CACHE_MAX
    assert "acme/repo-0" not in webui._issues_cache


def test_gh_issues_timeout_kills_and_reaps_process(monkeypatch):
    class Process:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True
            self.returncode = -9
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(webui.asyncio, "create_subprocess_exec", create)

    issues, error = asyncio.run(webui._gh_issues("acme/repo"))

    assert issues == []
    assert error == "gh issue list timeout"
    assert process.killed is True
    assert process.reaped is True


def test_run_cancellation_kills_and_reaps_process(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        never = asyncio.Event()

        class Process:
            returncode = None
            killed = False
            reaped = False

            async def communicate(self):
                started.set()
                await never.wait()

            def kill(self):
                self.killed = True

            async def wait(self):
                self.reaped = True
                self.returncode = -9
                return self.returncode

        process = Process()

        async def create(*_args, **_kwargs):
            return process

        monkeypatch.setattr(webui.asyncio, "create_subprocess_exec", create)
        task = asyncio.create_task(webui._run(["gh", "status"]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return process

    process = asyncio.run(scenario())
    assert process.killed is True
    assert process.reaped is True


def test_run_cleanup_falls_back_when_create_task_fails(monkeypatch):
    class Process:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self):
            raise TimeoutError

        def kill(self):
            self.killed = True

        async def wait(self):
            self.reaped = True
            self.returncode = -9
            return self.returncode

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    def reject_task(_coro):
        raise RuntimeError("event loop is closing")

    async def scenario():
        monkeypatch.setattr(
            webui.asyncio, "create_subprocess_exec", create
        )
        monkeypatch.setattr(webui.asyncio, "create_task", reject_task)
        return await webui._run(["gh", "status"])

    result = asyncio.run(scenario())
    assert result == (-1, "", "timeout")
    assert process.killed is True
    assert process.reaped is True


def test_token_env_names_match_runtime_normalization_and_defaults():
    assert webui.token_env_names({"name": "a-b"}, {}) == (
        "A_B_SLACK_BOT_TOKEN",
        "A_B_SLACK_APP_TOKEN",
    )
    assert webui.token_env_names(
        {"name": "a-b"},
        {
            "bot_token_env": "TEAM_BOT_TOKEN",
            "app_token_env": "TEAM_APP_TOKEN",
        },
    ) == ("TEAM_BOT_TOKEN", "TEAM_APP_TOKEN")
    assert webui.token_env_names(
        {
            "name": "a-b",
            "bot_token_env": "AGENT_BOT_TOKEN",
            "app_token_env": "AGENT_APP_TOKEN",
        },
        {
            "bot_token_env": "TEAM_BOT_TOKEN",
            "app_token_env": "TEAM_APP_TOKEN",
        },
    ) == ("AGENT_BOT_TOKEN", "AGENT_APP_TOKEN")


def test_state_and_token_save_honor_default_token_envs(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    env_file = tmp_path / ".env"
    target.write_text(
        """
defaults:
  bot_token_env: TEAM_BOT_TOKEN
  app_token_env: TEAM_APP_TOKEN
agents:
  - name: a_b
    persona: x
""",
        encoding="utf-8",
    )
    env_file.write_text(
        "TEAM_BOT_TOKEN=xoxb-old\nTEAM_APP_TOKEN=xapp-old\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)

    async def valid(_bot, _app):
        return {
            "bot_ok": True,
            "app_ok": True,
            "bot_detail": "ok",
            "app_detail": "ok",
        }

    monkeypatch.setattr(webui, "validate_tokens", valid)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(webui.make_app())) as client:
            state = await (await client.get("/api/state")).json()
            saved = await client.post(
                "/api/tokens",
                json={
                    "name": "a_b",
                    "bot_token": "xoxb-new",
                    "app_token": "xapp-new",
                },
            )
            return state, saved.status, await saved.json()

    state, status, saved = asyncio.run(scenario())
    assert state["agents"][0]["bot_env"] == "TEAM_BOT_TOKEN"
    assert state["agents"][0]["app_env"] == "TEAM_APP_TOKEN"
    assert state["agents"][0]["bot_set"] is True
    assert state["agents"][0]["app_set"] is True
    assert status == 200 and saved["ok"] is True
    contents = env_file.read_text(encoding="utf-8")
    assert "TEAM_BOT_TOKEN=xoxb-new" in contents
    assert "TEAM_APP_TOKEN=xapp-new" in contents
    assert "A_B_SLACK_BOT_TOKEN" not in contents


def _main_script(html: str) -> str:
    """Extract the main <script> JS body from INDEX_HTML."""
    i = html.index("const $=")
    start = html.rindex("<script>", 0, i) + len("<script>")
    end = html.index("</script>", i)
    return html[start:end]


def test_single_script_close():
    """Page has exactly one </script>; embedding </script> in a JS string would truncate parsing."""
    assert webui.INDEX_HTML.count("</script>") == 1


def test_escape_proof_constructs_present():
    """Newline-related constructs use the safe backslash-free form and did not regress to the fragile form."""
    script = _main_script(webui.INDEX_HTML)
    # Safe newline split (was .split('\\n'), broken by double escaping)
    assert "String.fromCharCode(10)" in script
    # Must not reintroduce single-quoted string containing a real newline
    assert ".split('\n')" not in script  # here \n is a real newline


def test_three_languages_present():
    """Sample strings for all three language dicts are present so switching has content."""
    script = _main_script(webui.INDEX_HTML)
    for sample in ("智能体运行状况", "エージェント運用状況", "Agent Operations"):
        assert sample in script


def test_guide_is_a_five_step_first_visit_runbook():
    """The static guide teaches the ordered path before protected control calls."""
    html = webui.INDEX_HTML
    script = _main_script(html)

    assert 'id="tab-guide"' in html
    assert 'id="panel-guide" class="panel on"' in html
    assert 'id="panel-mon" class="panel"' in html
    assert "TABS=['guide','mon','cfg','auth']" in script
    assert "GUIDE_STEPS=['roles','slack','local','prompts','first-task']" in script
    for step in ("roles", "slack", "local", "prompts", "first-task"):
        assert f'data-guide-step="{step}"' in html
        assert f'data-guide-check="{step}"' in html

    assert 'role="progressbar"' in html
    assert "showTab(seen?'mon':'guide')" in script
    guide_branch = script.index("if(tab==='guide'){renderGuide();return;}")
    control_prompt = script.index("ensureControlToken();", guide_branch)
    assert guide_branch < control_prompt
    assert "(async()=>{applyI18n();" in script


def test_guide_i18n_and_prompt_templates_cover_all_languages():
    """Every guide label and copyable prompt is localised in zh/ja/en."""
    import re

    html = webui.INDEX_HTML
    script = _main_script(html)
    keys = {
        key
        for key in re.findall(r'data-i18n="([^"]+)"', html)
        if key == "nav.guide" or key.startswith("guide.")
    }
    assert len(keys) >= 60
    for key in keys:
        assert script.count(f"'{key}':") >= 3, f"missing guide i18n key: {key}"

    assert "const GUIDE_PROMPTS={" in script
    for lang in ("zh:{", "ja:{", "en:{"):
        assert lang in script
    for prompt in ("channel", "dev", "reviewer", "planner", "task", "handoff"):
        assert script.count(f"{prompt}:") >= 3
        assert f'data-guide-prompt="{prompt}"' in html
    assert script.count('HANDOFF {"target_agent_id"') == 3


def test_guide_progress_and_copy_are_local_only_and_text_safe():
    """Checklist state stays in localStorage and prompt examples use textContent."""
    script = _main_script(webui.INDEX_HTML)

    assert "slackagent.guide.progress.v1" in script
    assert "localStorage.setItem(GUIDE_PROGRESS_KEY" in script
    assert "localStorage.setItem(GUIDE_SEEN_KEY" in script
    assert "navigator.clipboard.writeText(value)" in script
    assert "document.createElement('textarea')" in script
    assert "document.execCommand('copy')" in script
    assert "finally{area.remove();}" in script
    assert script.count("'guide.copyselect':") >= 3
    assert "function selectGuidePrompt(name)" in script
    assert "document.createRange()" in script
    assert "range.selectNodeContents(block)" in script
    assert "selection.removeAllRanges()" in script
    assert "selection.addRange(range)" in script
    assert "el.textContent=prompts[el.dataset.guidePrompt]||''" in script
    assert "innerHTML=prompts[" not in script
    prompts = script[
        script.index("const GUIDE_PROMPTS={") : script.index("let LANG=")
    ]
    assert "OPENAI_API_KEY=" not in prompts
    assert "xoxb-" not in prompts
    assert "ghp_" not in prompts


def test_monitor_renders_transcript_health_without_message_content():
    script = _main_script(webui.INDEX_HTML)
    for key in (
        "transcript.threads",
        "transcript.messages",
        "transcript.persisted",
        "transcript.memory",
        "transcript.database",
        "transcript.warm",
        "transcript.partial",
        "transcript.evictions",
        "transcript.calls",
        "transcript.failures",
    ):
        assert script.count(f"'{key}'") >= 3
    for field in (
        "warm_threads",
        "partial_threads",
        "persisted_messages",
        "memory_bytes",
        "persisted_bytes",
        "evicted_threads",
        "backfill_calls",
        "backfill_failures",
    ):
        assert field in script
    assert "transcript.messages.map" not in script
    assert "fmtBytes" in script


def test_monitor_renders_owner_quota_fields_and_three_language_labels():
    script = _main_script(webui.INDEX_HTML)
    for key in (
        "quota.title",
        "quota.used",
        "quota.limit",
        "quota.remaining",
        "quota.reserved",
        "quota.denied",
        "quota.errors",
        "quota.agent",
        "quota.runtime",
        "quota.turns",
        "quota.estimated",
    ):
        assert script.count(f"'{key}'") >= 3, f"missing i18n key {key}"
    renderer = script[
        script.index("function renderOwnerQuotas"):
        script.index("const REPLY_LANGS")
    ]
    for field in (
        "total_tokens",
        "daily_total_token_limit",
        "remaining_total_tokens",
        "active_reserved_tokens",
        "denied",
        "errors",
        "breakdown",
        "agent",
        "runtime",
        "estimated_turns",
    ):
        assert field in renderer
    assert "prompt" not in renderer
    assert ".text" not in renderer


def test_live_state_defense_in_depth_scopes_owner_usage_but_admin_sees_all(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    env_file = tmp_path / ".env"
    owner_token = "owner-" + ("x" * 40)
    admin_token = "admin-" + ("x" * 40)
    target.write_text(
        """
access:
  admins: [U00ADMIN]
agents:
  - name: alice
    owner: U01ALICE
    persona: alice
  - name: bob
    owner: U02BOB
    persona: bob
""".strip()
        + "\n",
        encoding="utf-8",
    )
    env_file.write_text(
        "\n".join(
            (
                f"SLACK_AGENT_CONTROL_TOKEN_U01ALICE={owner_token}",
                "SLACK_AGENT_CONTROL_TOKEN_U02BOB=bob-" + ("x" * 40),
                f"SLACK_AGENT_CONTROL_TOKEN_U00ADMIN={admin_token}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)
    seen_authorization = []
    upstream_payload = {
        "online": True,
        "owner_usage": {
            "U01ALICE": {
                "total_tokens": 10,
                "breakdown": [{"agent": "alice", "runtime": "codex"}],
            },
            "U02BOB": {
                "total_tokens": 20,
                "breakdown": [{"agent": "bob", "runtime": "claude"}],
            },
        },
        "agents": [
            {"name": "alice", "owner": "U01ALICE"},
            {"name": "bob", "owner": "U02BOB"},
        ],
        "worktrees": [
            {
                "identity_digest": "a" * 64,
                "owner": "U01ALICE",
                "status": "ready",
            },
            {
                "identity_digest": "b" * 64,
                "owner": "U02BOB",
                "status": "ready",
            },
        ],
    }

    async def upstream_state(request):
        seen_authorization.append(request.headers.get("Authorization", ""))
        return webui.web.json_response(upstream_payload)

    async def scenario():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        upstream = web.Application()
        upstream.router.add_get("/state", upstream_state)
        async with TestServer(upstream) as server:
            monkeypatch.setattr(
                webui, "ADMIN_BASE", str(server.make_url(""))
            )
            async with TestClient(TestServer(webui.make_app())) as client:
                owner_response = await client.get(
                    "/api/live/state",
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                admin_response = await client.get(
                    "/api/live/state",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                return (
                    owner_response.status,
                    await owner_response.json(),
                    admin_response.status,
                    await admin_response.json(),
                )

    owner_status, owner_state, admin_status, admin_state = asyncio.run(
        scenario()
    )
    assert owner_status == admin_status == 200
    assert set(owner_state["owner_usage"]) == {"U01ALICE"}
    assert [item["name"] for item in owner_state["agents"]] == ["alice"]
    assert [
        item["identity_digest"] for item in owner_state["worktrees"]
    ] == ["a" * 64]
    assert set(admin_state["owner_usage"]) == {"U01ALICE", "U02BOB"}
    assert {item["name"] for item in admin_state["agents"]} == {
        "alice",
        "bob",
    }
    assert {
        item["identity_digest"] for item in admin_state["worktrees"]
    } == {"a" * 64, "b" * 64}
    assert seen_authorization == [
        f"Bearer {owner_token}",
        f"Bearer {admin_token}",
    ]


def test_live_worktree_remove_validates_digest_and_forwards_bearer(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    env_file = tmp_path / ".env"
    owner_token = "owner-" + ("x" * 40)
    digest = "a" * 64
    target.write_text(
        """
agents:
  - name: alice
    owner: U01ALICE
    persona: alice
""".strip()
        + "\n",
        encoding="utf-8",
    )
    env_file.write_text(
        f"SLACK_AGENT_CONTROL_TOKEN_U01ALICE={owner_token}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)
    seen = []

    async def upstream_remove(request):
        seen.append(
            (
                request.match_info["identity_digest"],
                request.headers.get("Authorization", ""),
            )
        )
        return webui.web.json_response(
            {
                "ok": True,
                "identity_digest": digest,
                "status": "removed",
            }
        )

    async def scenario():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        upstream = web.Application()
        upstream.router.add_post(
            "/worktrees/{identity_digest}/remove", upstream_remove
        )
        async with TestServer(upstream) as server:
            monkeypatch.setattr(
                webui, "ADMIN_BASE", str(server.make_url(""))
            )
            async with TestClient(TestServer(webui.make_app())) as client:
                invalid = await client.post(
                    "/api/live/worktrees/not-a-digest/remove",
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                valid = await client.post(
                    f"/api/live/worktrees/{digest}/remove",
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                return (
                    invalid.status,
                    await invalid.text(),
                    valid.status,
                    await valid.json(),
                )

    invalid_status, invalid_body, valid_status, valid_body = asyncio.run(
        scenario()
    )
    assert invalid_status == 400
    assert "invalid" in invalid_body
    assert valid_status == 200
    assert valid_body["status"] == "removed"
    assert seen == [(digest, f"Bearer {owner_token}")]


def test_worktree_monitor_has_three_language_labels_and_safe_remove_action():
    script = webui.INDEX_HTML
    for key in (
        "worktree.title",
        "worktree.empty",
        "worktree.active",
        "worktree.remove",
        "worktree.confirm",
        "worktree.removed",
    ):
        assert script.count(f"'{key}'") >= 3, f"missing i18n key {key}"
    assert 'id="worktrees"' in script
    assert "function renderWorktrees" in script
    assert "function removeWorktree" in script
    assert "/api/live/worktrees/" in script
    assert "identity_digest" in script


def test_lang_buttons_match_dict_keys():
    """Top-bar language buttons have data-lang exactly zh/ja/en."""
    html = webui.INDEX_HTML
    for lang in ("zh", "ja", "en"):
        assert f'data-lang="{lang}"' in html


def test_script_compiles_as_js():
    """Compile the main script with node (no execute) to ensure no syntax errors. Skip if no node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable; skip JS syntax check")
    script = _main_script(webui.INDEX_HTML)
    # new Function(src) only compiles the function body; SyntaxError -> non-zero exit
    result = subprocess.run(
        [node, "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
        input=script,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"main script has JS syntax error:\n{result.stderr}"


def test_dynamic_agent_markup_has_no_inline_code_or_name_based_ids():
    """Stored YAML names stay data, never JavaScript source or selector syntax."""
    import re

    script = _main_script(webui.INDEX_HTML)
    dynamic_code_or_id = re.compile(
        r'(?:onclick|onchange|oninput|id)="[^"]*\$\{a\.name\}'
    )
    assert dynamic_code_or_id.search(script) is None
    assert 'data-agent="${esc(a.name)}"' in script
    assert "addEventListener('click'" in script
    assert "addEventListener('change'" in script
    assert "addEventListener('input'" in script
    assert "PENDING=Object.create(null)" in script
    assert ".innerHTML=t('confirm.q'" not in script


def test_dynamic_links_allow_only_http_and_https():
    script = _main_script(webui.INDEX_HTML)
    assert "function safeHttpUrl" in script
    assert "u.protocol==='http:'||u.protocol==='https:'" in script
    assert 'href="${esc(it.url)}"' not in script
    assert "window.open(r.url" not in script
    assert 'rel="noopener noreferrer"' in script


# ---------------------------------------------------------------------------
# storage layer: atomic writes / hot-swap write-back
# ---------------------------------------------------------------------------

import asyncio
import os

import yaml


def _no_tmp_litter(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_write_yaml_atomic_and_backup(tmp_path, monkeypatch):
    """write_yaml: valid yaml out, one .bak generation, no temp-file litter."""
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)

    webui.write_yaml({"agents": [{"name": "a"}]})
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {
        "agents": [{"name": "a"}]
    }
    assert not (tmp_path / "agents.yaml.bak").exists()  # nothing to back up yet

    webui.write_yaml({"agents": [{"name": "b"}]})
    bak = yaml.safe_load(
        (tmp_path / "agents.yaml.bak").read_text(encoding="utf-8")
    )
    assert bak == {"agents": [{"name": "a"}]}
    assert _no_tmp_litter(tmp_path)


def test_upsert_env_keeps_comments_and_0600(tmp_path, monkeypatch):
    """upsert_env: comments survive, atomic write, secrets stay 0600."""
    env = tmp_path / ".env"
    monkeypatch.setattr(webui, "ENV_FILE", env)
    env.write_text("# comment\nFOO=1\n", encoding="utf-8")

    webui.upsert_env({"FOO": "2", "BAR": "x"})

    text = env.read_text(encoding="utf-8")
    assert "# comment" in text
    assert "FOO=2" in text
    assert "BAR=x" in text
    assert (os.stat(env).st_mode & 0o777) == 0o600
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "# comment\nFOO=1\n"
    assert _no_tmp_litter(tmp_path)


def test_persist_agent_fields_roundtrip(tmp_path, monkeypatch):
    """Hot-swap write-back: model/effort empty stays explicit engine default."""
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    target.write_text("agents:\n- name: a\n  effort: high\n", encoding="utf-8")

    ok = asyncio.run(
        webui._persist_agent_fields(
            "a", {"claude_model": "claude-opus-4-8", "effort": ""}
        )
    )
    assert ok is True
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]
    assert entry["claude_model"] == "claude-opus-4-8"
    # empty effort is persisted so restart does not re-merge defaults.effort
    assert entry["effort"] == ""

    assert (
        asyncio.run(webui._persist_agent_fields("ghost", {"runtime": "codex"}))
        is False
    )


# ---------------------------------------------------------------------------
# Auth panel: Claude / Codex / GitHub (helpers + handlers)
# ---------------------------------------------------------------------------


def test_auth_panel_and_routes_present():
    """Auth tab, panel markup, and API routes cover Claude + Codex + GitHub."""
    html = webui.INDEX_HTML
    assert 'id="tab-auth"' in html
    assert 'id="panel-auth"' in html
    assert "claudeStart()" in html
    assert "codexStart()" in html
    assert "ghStart()" in html
    assert "showVerified" in html
    assert "TABS=['guide','mon','cfg','auth']" in html

    app = webui.make_app()
    paths = {r.resource.canonical for r in app.router.routes()}
    for path in (
        "/api/auth/state",
        "/api/auth/claude/start",
        "/api/auth/claude/code",
        "/api/auth/claude/cancel",
        "/api/auth/codex/start",
        "/api/auth/codex/wait",
        "/api/auth/codex/cancel",
        "/api/auth/gh/start",
        "/api/auth/gh/wait",
        "/api/auth/gh/cancel",
        "/api/auth/gh-token",
        "/api/auth/gh-token/import",
    ):
        assert path in paths


def test_auth_i18n_keys_in_all_languages():
    """Auth strings exist in zh/ja/en so the new tab is fully localised."""
    script = _main_script(webui.INDEX_HTML)
    required = (
        "nav.auth",
        "auth.title",
        "auth.claude",
        "auth.codex",
        "auth.gh",
        "auth.signin",
        "auth.verified",
        "auth.devicecode",
        "auth.ghimport",
        "auth.ok",
        "auth.ghsaved",
    )
    for key in required:
        assert script.count(f"'{key}':") >= 3, f"missing i18n key: {key}"


def test_strip_ansi_and_find_auth_url():
    """OAuth / device URLs recovered from plain text and OSC-8 CLI output."""
    plain = "visit: https://claude.ai/oauth/authorize?code_challenge=abc&state=1"
    assert "/oauth/authorize" in webui._find_auth_url(plain)

    osc = (
        "\x1b]8;;https://claude.ai/oauth/authorize?x=1\x07"
        "https://claude.ai/oauth/authorize?x=1"
        "\x1b]8;;\x07"
    )
    cleaned = webui._strip_ansi(osc)
    assert webui._find_auth_url(cleaned).startswith("https://")
    assert webui._find_auth_url("no link here") == ""
    assert webui._find_auth_url("see https://example.com/docs") == ""

    device = "Open https://auth.openai.com/codex/device and enter PU5V-DQ68E"
    assert "codex/device" in webui._find_url(device, "auth.openai.com/codex/device")
    assert webui._find_device_code(device) == "PU5V-DQ68E"


def test_normalize_oauth_code_from_url():
    """Bare codes and pasted redirect URLs both yield a usable code."""
    assert webui._normalize_oauth_code("  abcdefghij  ") == "abcdefghij"
    url = "https://console.anthropic.com/oauth/code/callback?code=skcode123456&state=xyz"
    assert webui._normalize_oauth_code(url) == "skcode123456"
    assert webui._normalize_oauth_code("code=onlycode99&state=1") == "onlycode99"


def test_secret_re_rejects_injection():
    """Pasted codes/tokens must be single-line printable ASCII (no stdin injection)."""
    assert webui._SECRET_RE.match("abcdefgh")
    assert webui._SECRET_RE.match("ghp_" + "x" * 36)
    assert not webui._SECRET_RE.match("short")
    assert not webui._SECRET_RE.match("has space")
    assert not webui._SECRET_RE.match("line1\nline2")
    assert not webui._SECRET_RE.match("tab\there")


def test_cli_cmd_docker_vs_host():
    """docker mode uses compose exec; host mode is a bare binary argv."""
    d = webui._cli_cmd("docker", "claude", ["auth", "status"], tty=False)
    assert d[:4] == ["docker", "compose", "exec", "-T"]
    assert d[-3:] == ["claude", "auth", "status"]
    d_tty = webui._cli_cmd("docker", "codex", ["login", "--device-auth"], tty=True)
    assert "-it" in d_tty
    assert d_tty[-1] == "--device-auth"
    h = webui._cli_cmd("host", "claude", ["auth", "status"], tty=False)
    assert h == ["claude", "auth", "status"]


def test_gh_token_handler_writes_env(tmp_path, monkeypatch):
    """POST /api/auth/gh-token writes GH_TOKEN to .env and never echoes it back."""
    env = tmp_path / ".env"
    monkeypatch.setattr(webui, "ENV_FILE", env)
    env.write_text("FOO=1\n", encoding="utf-8")

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        app = webui.make_app()
        async with TestClient(TestServer(app)) as client:
            bad = await client.post(
                "/api/auth/gh-token",
                json={"token": "nope"},  # too short / invalid
                headers={"Host": "127.0.0.1"},
            )
            assert bad.status == 400

            ok = await client.post(
                "/api/auth/gh-token",
                json={"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
                headers={"Host": "127.0.0.1"},
            )
            body = await ok.json()
            assert ok.status == 200
            assert body == {"ok": True}
            assert "ghp_" not in await ok.text()

    asyncio.run(_run())
    text = env.read_text(encoding="utf-8")
    assert "GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789" in text
    assert "FOO=1" in text
    assert (os.stat(env).st_mode & 0o777) == 0o600


def test_gh_token_import_from_host(tmp_path, monkeypatch):
    """Import path shells out to `gh auth token` and writes the result."""
    env = tmp_path / ".env"
    monkeypatch.setattr(webui, "ENV_FILE", env)

    async def fake_run(argv, timeout=15.0):
        if argv[:2] == ["gh", "auth"] and argv[2:3] == ["token"]:
            return 0, "gho_importedtokenvalue0000000000000001\n", ""
        return -1, "", "unexpected"

    monkeypatch.setattr(webui, "_run", fake_run)
    monkeypatch.setattr(
        webui.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None
    )

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        app = webui.make_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/api/auth/gh-token/import",
                headers={"Host": "127.0.0.1"},
            )
            assert r.status == 200
            assert (await r.json()) == {"ok": True}

    asyncio.run(_run())
    assert "GH_TOKEN=gho_importedtokenvalue0000000000000001" in env.read_text(
        encoding="utf-8"
    )


def test_auth_state_never_leaks_secrets(monkeypatch):
    """/api/auth/state reports booleans/modes only — no token values."""

    async def fake_mode(binary="claude"):
        return "host"

    async def fake_claude_status(mode):
        return {
            "available": True,
            "logged_in": True,
            "auth_method": "claudeai",
            "error": "",
        }

    async def fake_codex_status(mode):
        return {
            "available": True,
            "logged_in": True,
            "auth_method": "Logged in using ChatGPT",
            "error": "",
        }

    async def fake_gh_host():
        return {
            "available": True,
            "logged_in": True,
            "user": "alice",
            "error": "",
        }

    monkeypatch.setattr(webui, "_cli_mode", fake_mode)
    monkeypatch.setattr(webui, "_claude_status", fake_claude_status)
    monkeypatch.setattr(webui, "_codex_status", fake_codex_status)
    monkeypatch.setattr(webui, "_gh_host_status", fake_gh_host)
    monkeypatch.setattr(webui, "_gh_token_set", lambda: True)
    monkeypatch.setattr(
        webui.shutil, "which", lambda name: "/bin/gh" if name == "gh" else None
    )

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        await webui._drop_all_sessions()
        app = webui.make_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/api/auth/state", headers={"Host": "127.0.0.1"})
            data = await r.json()
            assert r.status == 200
            assert data["claude"]["logged_in"] is True
            assert data["claude"]["mode"] == "host"
            assert data["codex"]["logged_in"] is True
            assert data["gh"]["token_set"] is True
            assert data["gh"]["host_user"] == "alice"
            raw = await r.text()
            assert "sk-" not in raw
            assert "ghp_" not in raw
            assert "gho_" not in raw

    asyncio.run(_run())


def test_claude_login_start_without_cli(monkeypatch):
    """Start fails cleanly when neither container nor host has claude."""

    async def fake_mode(binary="claude"):
        return "none"

    monkeypatch.setattr(webui, "_cli_mode", fake_mode)

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        app = webui.make_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/api/auth/claude/start",
                headers={"Host": "127.0.0.1"},
            )
            body = await r.json()
            assert r.status == 400
            assert body["ok"] is False
            assert "claude" in body["error"].lower()

            r2 = await client.post(
                "/api/auth/codex/start",
                headers={"Host": "127.0.0.1"},
            )
            assert r2.status == 400
            assert (await r2.json())["ok"] is False

    asyncio.run(_run())


def test_claude_login_code_requires_active_session():
    """Code redeem returns 409 when no login process is held open."""

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        await webui._drop_all_sessions()
        app = webui.make_app()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/api/auth/claude/code",
                json={"code": "validcode123"},
                headers={"Host": "127.0.0.1"},
            )
            assert r.status == 409
            body = await r.json()
            assert body["ok"] is False

            bad = await client.post(
                "/api/auth/claude/code",
                json={"code": "bad"},
                headers={"Host": "127.0.0.1"},
            )
            assert bad.status == 400

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# card (L1) via webui state / save / i18n
# ---------------------------------------------------------------------------


def test_i18n_card_keys_in_all_languages():
    """cfg.card / cardhint / cardlong present in zh, ja, en."""
    script = _main_script(webui.INDEX_HTML)
    for key in ("cfg.card", "cfg.cardhint", "cfg.cardlong"):
        # each language block should include the key three times total (zh/ja/en)
        assert script.count(f"'{key}'") >= 3, f"missing i18n key {key}"


def test_h_state_returns_card(tmp_path, monkeypatch):
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    target.write_text(
        "agents:\n- name: a\n  persona: p\n  card: my card\n",
        encoding="utf-8",
    )

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/state")
            data = await resp.json()
            assert resp.status == 200
            assert data["agents"][0]["card"] == "my card"
            assert data["agents"][0]["persona"] == "p"

    asyncio.run(_run())


def test_update_persona_saves_card(tmp_path, monkeypatch):
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    target.write_text(
        "agents:\n- name: a\n  persona: old\n",
        encoding="utf-8",
    )

    async def _fake_reload():
        return {"ok": False, "error": "offline"}

    monkeypatch.setattr(webui, "_admin_reload", _fake_reload)

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/agents/a/persona",
                json={"persona": "new persona", "card": "new card\nline2"},
            )
            data = await resp.json()
            assert data["ok"] is True
        entry = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]
        assert "new persona" in entry["persona"]
        assert entry["card"] == "new card\nline2"

    asyncio.run(_run())


def test_save_agent_with_card(tmp_path, monkeypatch):
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    target.write_text("agents: []\n", encoding="utf-8")

    async def _fake_reload():
        return {"ok": False}

    monkeypatch.setattr(webui, "_admin_reload", _fake_reload)

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/agents",
                json={
                    "name": "qa",
                    "optional": True,
                    "runtime": "claude",
                    "persona": "qa persona",
                    "card": "call for tests",
                    "workspace": "/ws/qa",
                },
            )
            data = await resp.json()
            assert data["ok"] is True
        agents = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"]
        assert agents[0]["name"] == "qa"
        assert agents[0]["card"] == "call for tests"

    asyncio.run(_run())


def test_openai_runtime_is_available_in_webui(tmp_path, monkeypatch):
    """The console can create an OpenAI agent and advertises its API controls."""
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    target.write_text("agents: []\n", encoding="utf-8")

    async def _fake_reload():
        return {"ok": False}

    monkeypatch.setattr(webui, "_admin_reload", _fake_reload)

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/agents",
                json={
                    "name": "planner",
                    "optional": True,
                    "runtime": "openai",
                    "persona": "planning and review",
                    "workspace": "/ws/planner",
                },
            )
            assert (await response.json())["ok"] is True
        entry = yaml.safe_load(
            target.read_text(encoding="utf-8")
        )["agents"][0]
        assert entry["runtime"] == "openai"

    asyncio.run(_run())
    assert ">openai api</button>" in webui.INDEX_HTML
    assert "openai_api_key_set" in webui.INDEX_HTML


def test_models_endpoint_includes_openai_catalog(monkeypatch, tmp_path):
    """Model and effort pickers expose OpenAI / CLI Proxy catalog values."""
    import json

    async def _fake_claude_models():
        return [""], "test", ""

    async def _fake_proxy_models(base_url, api_key=""):
        assert base_url == "http://127.0.0.1:8317/v1"
        return ["grok-4.5", "gemini-3.1-pro-from-proxy"], "proxy test"

    target = tmp_path / "agents.yaml"
    target.write_text(
        "defaults:\n  openai_base_url: http://127.0.0.1:8317/v1\nagents: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(webui, "_fetch_claude_models", _fake_claude_models)
    monkeypatch.setattr(webui, "_codex_models", lambda: ([""], "test", ""))
    monkeypatch.setattr(webui, "_codex_default_effort", lambda: "")
    monkeypatch.setattr(
        webui, "_fetch_openai_compatible_models", _fake_proxy_models
    )
    webui._models_cache.clear()

    response = asyncio.run(webui.h_models(None))
    payload = json.loads(response.text)

    assert "" in payload["openai"]
    assert "gpt-5.6-sol" in payload["openai"]
    assert "grok-4.5" in payload["openai"]
    assert "gemini-3.1-pro-from-proxy" in payload["openai"]
    assert payload["current"]["openai"] == "gpt-5.6-sol"
    assert payload["openai_base_url"] == "http://127.0.0.1:8317/v1"
    assert payload["efforts"]["openai"] == [
        "",
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


def test_state_exposes_openai_base_url(tmp_path, monkeypatch):
    """Config state advertises per-agent CLI Proxy base_url."""
    target = tmp_path / "agents.yaml"
    target.write_text(
        """
defaults:
  openai_base_url: http://127.0.0.1:8317/v1
agents:
  - name: planner
    runtime: openai
    openai_model: grok-4.5
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/state")
            data = await response.json()
            assert data["openai_base_url"] == "http://127.0.0.1:8317/v1"
            assert data["agents"][0]["openai_base_url"] == (
                "http://127.0.0.1:8317/v1"
            )
            assert data["agents"][0]["runtime"] == "openai"

    asyncio.run(_run())


def test_live_openai_model_persists_to_openai_field(
    tmp_path, monkeypatch
):
    """A live model switch must not overwrite Claude/Codex model fields."""
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    target.write_text(
        "agents:\n- name: planner\n  runtime: openai\n",
        encoding="utf-8",
    )

    async def _fake_admin_post(path, body):
        assert path == "/agents/planner/model"
        assert body == {"model": "gpt-5.6-sol"}
        return (
            {
                "ok": True,
                "runtime": "openai",
                "model": "gpt-5.6-sol",
            },
            200,
        )

    monkeypatch.setattr(webui, "_admin_post", _fake_admin_post)

    async def _run():
        app = webui.make_app()
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/live/planner/model",
                json={"model": "gpt-5.6-sol"},
            )
            payload = await response.json()
            assert payload["ok"] is True
            assert payload["persisted"] is True

    asyncio.run(_run())
    entry = yaml.safe_load(
        target.read_text(encoding="utf-8")
    )["agents"][0]
    assert entry["openai_model"] == "gpt-5.6-sol"
    assert "claude_model" not in entry
    assert "codex_model" not in entry


def test_webui_owner_auth_filters_state_and_blocks_cross_owner_write(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    env_file = tmp_path / ".env"
    alice_token = "alice-" + ("a" * 40)
    bob_token = "bob-" + ("b" * 40)
    admin_token = "admin-" + ("z" * 40)
    target.write_text(
        """
access:
  admins: [U01ADMIN]
agents:
  - name: alice
    owner: U01ALICE
    persona: alice-private
    workspace: /alice/private
  - name: bob
    owner: U02BOB
    persona: bob-private
    workspace: /bob/private
""".strip()
        + "\n",
        encoding="utf-8",
    )
    env_file.write_text(
        "\n".join(
            (
                f"SLACK_AGENT_CONTROL_TOKEN_U01ALICE={alice_token}",
                f"SLACK_AGENT_CONTROL_TOKEN_U02BOB={bob_token}",
                f"SLACK_AGENT_CONTROL_TOKEN_U01ADMIN={admin_token}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(webui.make_app())) as client:
            health = await client.get("/healthz")
            missing = await client.get("/api/state")
            spoofed = await client.get(
                "/api/state",
                headers={"X-Slack-User-ID": "U01ADMIN"},
            )
            owner_response = await client.get(
                "/api/state",
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            owner_state = await owner_response.json()
            before = target.read_text(encoding="utf-8")
            forbidden = await client.post(
                "/api/agents/bob/persona",
                data="{not-json",
                headers={
                    "Authorization": f"Bearer {alice_token}",
                    "Content-Type": "application/json",
                },
            )
            after = target.read_text(encoding="utf-8")
            admin_response = await client.get(
                "/api/state",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            admin_state = await admin_response.json()
            return (
                health,
                missing,
                spoofed,
                owner_response,
                owner_state,
                forbidden,
                before,
                after,
                admin_response,
                admin_state,
            )

    (
        health,
        missing,
        spoofed,
        owner_response,
        owner_state,
        forbidden,
        before,
        after,
        admin_response,
        admin_state,
    ) = asyncio.run(scenario())
    assert health.status == 200
    assert missing.status == 401
    assert spoofed.status == 401
    assert owner_response.status == 200
    assert [item["name"] for item in owner_state["agents"]] == ["alice"]
    assert owner_state["agents"][0]["owner"] == "U01ALICE"
    assert "bob-private" not in repr(owner_state)
    assert "/bob/private" not in repr(owner_state)
    assert forbidden.status == 403
    assert after == before
    assert admin_response.status == 200
    assert {item["name"] for item in admin_state["agents"]} == {
        "alice",
        "bob",
    }


def test_webui_resolves_local_owner_from_separate_roster(
    tmp_path, monkeypatch
):
    token = "alice-" + ("r" * 40)
    local = tmp_path / "agents.alice.yaml"
    roster = tmp_path / "roster.yaml"
    env_file = tmp_path / ".env.alice"
    local.write_text(
        """
roster: roster.yaml
node:
  id: alice-node
security:
  control_auth: required
agents:
  - name: alice
    persona: local only
""",
        encoding="utf-8",
    )
    roster.write_text(
        """
version: 1
access:
  admins: [U00ADMIN]
agents:
  - name: alice
    slack_user_id: UALICEBOT
    slack_bot_id: BALICEBOT
    owner: U01ALICE
    node_id: alice-node
  - name: bob
    slack_user_id: UBOBBOT
    slack_bot_id: BBOBBOT
    owner: U02BOB
    node_id: bob-node
""",
        encoding="utf-8",
    )
    env_file.write_text(
        f"SLACK_AGENT_CONTROL_TOKEN_U01ALICE={token}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", local)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)
    monkeypatch.delenv("ROSTER_CONFIG", raising=False)

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(webui.make_app())) as client:
            denied = await client.get("/api/state")
            allowed = await client.get(
                "/api/state",
                headers={"Authorization": f"Bearer {token}"},
            )
            return denied.status, allowed.status, await allowed.json()

    denied_status, allowed_status, state = asyncio.run(scenario())
    assert denied_status == 401
    assert allowed_status == 200
    assert [(item["name"], item["owner"]) for item in state["agents"]] == [
        ("alice", "U01ALICE")
    ]


def test_webui_forwards_authorization_and_owner_cannot_global_reload(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    env_file = tmp_path / ".env"
    owner_token = "owner-" + ("x" * 40)
    target.write_text(
        """
agents:
  - name: alice
    owner: U01ALICE
    persona: alice
""".strip()
        + "\n",
        encoding="utf-8",
    )
    env_file.write_text(
        f"SLACK_AGENT_CONTROL_TOKEN_U01ALICE={owner_token}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", env_file)
    captured: list[str] = []

    async def admin_restart(request):
        captured.append(request.headers.get("Authorization", ""))
        return webui.web.json_response({"ok": True, "cleared_sessions": 0})

    async def scenario():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        upstream = web.Application()
        upstream.router.add_post("/agents/alice/restart", admin_restart)
        async with TestServer(upstream) as server:
            monkeypatch.setattr(webui, "ADMIN_BASE", str(server.make_url("")))
            async with TestClient(TestServer(webui.make_app())) as client:
                restart = await client.post(
                    "/api/live/alice/restart",
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                reload_response = await client.post(
                    "/api/live/reload",
                    data="{not-json",
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                return restart, reload_response

    restart, reload_response = asyncio.run(scenario())
    assert restart.status == 200
    assert captured == [f"Bearer {owner_token}"]
    assert reload_response.status == 403


def test_webui_live_reload_preserves_restart_only_report_fields(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    target.write_text("agents: []\n", encoding="utf-8")
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")

    async def admin_reload(_request):
        return webui.web.json_response(
            {
                "ok": True,
                "global_changed": [],
                "global_restart_required": [
                    "roster_path",
                    "worktree_root",
                ],
            }
        )

    async def scenario():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        upstream = web.Application()
        upstream.router.add_post("/reload", admin_reload)
        async with TestServer(upstream) as server:
            monkeypatch.setattr(webui, "ADMIN_BASE", str(server.make_url("")))
            async with TestClient(TestServer(webui.make_app())) as client:
                response = await client.post("/api/live/reload")
                return response.status, await response.json()

    status, payload = asyncio.run(scenario())

    assert status == 200
    assert payload["global_changed"] == []
    assert payload["global_restart_required"] == [
        "roster_path",
        "worktree_root",
    ]
    assert "global_restart_required" in webui.INDEX_HTML
    assert "cfg.worktreerootrestart" in webui.INDEX_HTML


def test_webui_scrubs_control_token_before_launching_child_tools(
    tmp_path, monkeypatch
):
    target = tmp_path / "agents.yaml"
    token = "owner-" + ("x" * 40)
    target.write_text(
        "agents:\n- name: alice\n  owner: U01ALICE\n  persona: alice\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv(
        "SLACK_AGENT_CONTROL_TOKEN_U01ALICE", token
    )
    monkeypatch.setenv(
        "SLACK_AGENT_CONTROL_TOKEN_U99REMOTE", "remote-stale"
    )
    monkeypatch.setenv(
        "SLACK_AGENT_CONTROL_TOKEN_INVALID-SUFFIX", "invalid-stale"
    )
    monkeypatch.setenv(
        "SLACK_AGENT_CONTROL_TOKENS_KEEP", "unrelated"
    )

    app = webui.make_app()

    assert app is not None
    assert "SLACK_AGENT_CONTROL_TOKEN_U01ALICE" not in webui.os.environ
    assert "SLACK_AGENT_CONTROL_TOKEN_U99REMOTE" not in webui.os.environ
    assert (
        "SLACK_AGENT_CONTROL_TOKEN_INVALID-SUFFIX"
        not in webui.os.environ
    )
    assert webui.os.environ["SLACK_AGENT_CONTROL_TOKENS_KEEP"] == "unrelated"


def test_webui_security_headers_and_browser_token_storage():
    html = webui.INDEX_HTML
    assert "sessionStorage" in html
    assert "Authorization" in html
    assert "SLACK_AGENT_CONTROL_TOKEN" not in html
    assert "document.cookie" not in html
    assert "localStorage.setItem('slackagent.control_token'" not in html

    async def scenario():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(webui.make_app())) as client:
            return await client.get("/")

    response = asyncio.run(scenario())
    assert response.status == 200
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_webui_security_headers_cover_generic_500_and_http_exception(caplog):
    async def scenario():
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def boom(_request):
            raise RuntimeError("private exception detail")

        async def forbidden(_request):
            raise web.HTTPForbidden(text="preserved forbidden body")

        app = web.Application(middlewares=[webui.security_headers])
        app.router.add_get("/boom", boom)
        app.router.add_get("/forbidden", forbidden)
        async with TestClient(TestServer(app)) as client:
            boom_response = await client.get("/boom")
            boom_body = await boom_response.text()
            forbidden_response = await client.get("/forbidden")
            forbidden_body = await forbidden_response.text()
            return (
                boom_response,
                boom_body,
                forbidden_response,
                forbidden_body,
            )

    with caplog.at_level("ERROR", logger="webui"):
        boom_response, boom_body, forbidden_response, forbidden_body = (
            asyncio.run(scenario())
        )

    for response in (boom_response, forbidden_response):
        assert "default-src 'self'" in response.headers[
            "Content-Security-Policy"
        ]
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
    assert boom_response.status == 500
    assert boom_body == "internal server error"
    assert "private exception detail" not in boom_body
    assert forbidden_response.status == 403
    assert forbidden_body == "preserved forbidden body"
    assert "unhandled webui request path=/boom" in caplog.text


# ---------------------------------------------------------------------------
# Local workspace settings and the on-demand control token
# ---------------------------------------------------------------------------


def _workspace_app(tmp_path, monkeypatch, yaml_text):
    target = tmp_path / "agents.yaml"
    monkeypatch.setattr(webui, "AGENTS_YAML", target)
    monkeypatch.setattr(webui, "ENV_FILE", tmp_path / ".env")
    target.write_text(yaml_text, encoding="utf-8")

    async def _fake_reload():
        return {"ok": False, "error": "offline"}

    monkeypatch.setattr(webui, "_admin_reload", _fake_reload)
    return target


def _git_repo(path, origin):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def _post_workspace(body, name="a"):
    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(webui.make_app())) as client:
            resp = await client.post(f"/api/agents/{name}/workspace", json=body)
            if resp.content_type != "application/json":
                return {"ok": False, "status": resp.status}
            return await resp.json()

    return asyncio.run(_run())


def test_workspace_check_reports_git_facts_and_mismatch(tmp_path, monkeypatch):
    _workspace_app(
        tmp_path, monkeypatch,
        "github:\n  repo: acme/old\nagents:\n- name: a\n  persona: p\n",
    )
    repo = _git_repo(tmp_path / "widgets", "https://github.com/acme/widgets.git")
    data = _post_workspace({"workspace": str(repo), "dry_run": True})
    assert data["ok"] is True and data["git"] is True
    assert data["branch"] == "main"
    assert data["origin_repo"] == "acme/widgets"
    # The inherited github.repo disagrees with origin: preflight would disable it.
    assert data["github_repo"] == "acme/old" and data["mismatch"] is True
    data = _post_workspace(
        {"workspace": str(repo), "github_repo": "acme/widgets", "dry_run": True}
    )
    assert data["mismatch"] is False


def test_workspace_save_writes_path_and_repo(tmp_path, monkeypatch):
    target = _workspace_app(
        tmp_path, monkeypatch,
        "github:\n  repo: acme/old\nagents:\n- name: a\n  persona: p\n  github_repo: acme/x\n",
    )
    repo = _git_repo(tmp_path / "widgets", "git@github.com:acme/widgets.git")
    data = _post_workspace({"workspace": str(repo), "github_repo": "acme/widgets"})
    assert data["ok"] is True
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]
    assert entry["workspace"] == str(repo)
    assert entry["github_repo"] == "acme/widgets"
    # Empty clears the agent's own repo so it inherits again; omitted keeps it.
    _post_workspace({"workspace": str(repo), "github_repo": ""})
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]
    assert "github_repo" not in entry
    _post_workspace({"workspace": str(repo), "github_repo": "acme/widgets"})
    _post_workspace({"workspace": str(repo)})
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]
    assert entry["github_repo"] == "acme/widgets"


def test_workspace_rejects_bad_input_without_writing(tmp_path, monkeypatch):
    target = _workspace_app(
        tmp_path, monkeypatch, "agents:\n- name: a\n  persona: p\n  workspace: /keep\n"
    )
    plain = tmp_path / "plain"
    plain.mkdir()
    for body, error in (
        ({"workspace": ""}, "required"),
        ({"workspace": "relative/dir"}, "absolute"),
        ({"workspace": str(tmp_path / "missing")}, "does not exist"),
        ({"workspace": str(plain), "github_repo": "not a repo"}, "OWNER/REPO"),
    ):
        data = _post_workspace(body)
        assert data["ok"] is False and error in data["error"], body
    # An agent this caller cannot see is refused before anything is read.
    assert _post_workspace({"workspace": str(plain)}, name="ghost")["ok"] is False
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["agents"][0]["workspace"] == "/keep"
    # A directory that is not a git repo is allowed but flagged.
    data = _post_workspace({"workspace": str(plain), "dry_run": True})
    assert data["ok"] is True and data["git"] is False


def test_control_token_is_asked_in_place_only_after_a_401():
    script = _main_script(webui.INDEX_HTML)
    assert "window.prompt" not in script
    assert "if(r.status===401){CONTROL_REQUIRED=true;showUnlock(!!controlToken());}" in script
    assert "if(!CONTROL_REQUIRED||controlToken())return;" in script
    # A rejected token is dropped so it is not resent on every poll.
    assert "if(rejected)saveControlToken('');" in script
    for key in ("unlock.title", "unlock.hint", "unlock.save", "unlock.bad"):
        assert script.count(f"'{key}':") >= 3, key


def test_workspace_editor_is_localised():
    script = _main_script(webui.INDEX_HTML)
    for key in ("ws.title", "ws.edit", "ws.path", "ws.repo", "ws.check", "ws.unset",
                "ws.nogit", "ws.branch", "ws.mismatch", "ws.useorigin"):
        assert script.count(f"'{key}':") >= 3, key
    assert "/workspace'" in script and "dry_run" in script
