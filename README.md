# SlackAgentTeam (multi_app)

**Languages:** [English](README.md) · [日本語](README.ja.md) · [中文](README.zh.md)

Multi-agent Slack bots powered by local Claude Code, Codex CLI, or the OpenAI Responses API. Each agent is a separate Slack App (own bot identity and tokens). Mention-driven handoffs, turn budgets to prevent loops, context continuation, and GitHub Issues + push handoffs for multi-machine collaboration.

## Architecture

Multiple agents run Socket Mode in one Python process. In channels, an agent speaks only when `@`-mentioned. Agents can hand work off by mentioning each other in replies. Pure logic (sender classification, activation, turn budget, dedup, prompts) lives in `multi_core.py`; Slack / Claude / Codex wiring is in `multi_app.py`.

## Files

| File | Role |
|------|------|
| `multi_app.py` | Multi-agent entry: config load, Roster, SlackAgent, Socket Mode |
| `multi_core.py` | Pure logic layer (no slack/claude package deps) |
| `state_store.py` | SQLite thread-state persistence (sessions / summaries survive restarts) |
| `issue_claim.py` | Host-side GitHub issue claim tool (v2 lease: `claim` / `renew` / `release` / `verify`) |
| `agents.yaml` | Agent definitions (`card` / persona, ownership, node, projects) |
| `agents.distributed.example.yaml` | Two humans × two local agents distributed example |
| `slack-app-manifest-agent.yaml` | Slack App manifest template (one App per agent) |
| `.env.example` | Env var template (multi-agent tokens) |
| `webui.py` | Local console (agent CRUD, Slack App wizard, live monitor, hot-swap) |

## Agent roles

Each agent has two persona layers in `agents.yaml`:

- **`card` (L1, optional)** — teammate-facing interface (when to call me / what to hand off / what I deliver / what not to ask). Shown in `!roles`, each agent’s system-prompt peer roster, and the webui. Prefer ≤6 lines / 400 chars (soft warning only).
- **`persona` (L2)** — self-facing behaviour constraints; can be long. Injected only into that agent’s own system prompt.
- **Fallback:** empty/missing `card` → first line of `persona` (legacy behaviour). `defaults.card` is ignored (card is always per-agent). Hot-reloadable without restart.

| name | Responsibility | optional | Token env vars |
|------|----------------|----------|----------------|
| `dev` | Implement / fix / smoke-check; then `@reviewer` / `@qa` as needed | no | `DEV_SLACK_BOT_TOKEN` / `DEV_SLACK_APP_TOKEN` |
| `reviewer` | Code review; LGTM or concrete notes; re-assign `@dev` if needed | no | `REVIEWER_SLACK_BOT_TOKEN` / `REVIEWER_SLACK_APP_TOKEN` |
| `pm` | Requirements, task split; one `@` handoff at a time; report to humans (no code) | yes | `PM_SLACK_BOT_TOKEN` / `PM_SLACK_APP_TOKEN` |
| `qa` | Test perspective; run pytest etc.; fail back to `@dev` with repro steps | yes | `QA_SLACK_BOT_TOKEN` / `QA_SLACK_APP_TOKEN` |
| `designer` | UI/UX proposals and review; hand specs to `@dev` for implementation | yes | `DESIGNER_SLACK_BOT_TOKEN` / `DESIGNER_SLACK_APP_TOKEN` |
| `dx` | Thread traffic control & agent health (`!status` / `!reset`); read-only tools | yes | `DX_SLACK_BOT_TOKEN` / `DX_SLACK_APP_TOKEN` |

`dx` `allowed_tools` is limited to `[Read, Glob, Grep]`.

## Runtime: claude / codex / openai

Each agent can pick an engine (`runtime` field: agent → defaults → `claude`):

```yaml
agents:
  - name: dev
    runtime: codex          # this agent uses Codex CLI
    codex_sandbox: workspace-write   # read-only | workspace-write | danger-full-access
    # codex_model: gpt-5.2-codex     # omit = CLI default model
  - name: reviewer          # omit → claude
  - name: planner
    runtime: openai
    openai_model: gpt-5.6-sol
    openai_api_key_env: ALICE_OPENAI_API_KEY  # env name only, never the key
  - name: grok-reviewer
    runtime: openai
    openai_model: grok-4.5
    # Point at a local OpenAI-compatible proxy (CLIProxyAPI / Antigravity bridge):
    openai_base_url: http://127.0.0.1:8317/v1
```

| Item | claude | codex | openai |
|------|--------|-------|--------|
| Execution | claude-agent-sdk | local `codex exec --json` | OpenAI Responses API (official or CLI Proxy) |
| Thread continuity | session resume | thread resume | `previous_response_id` |
| System prompt | SDK option | prepended on new session | sent as `instructions` each turn |
| Local tools | Claude Code tools | local Codex sandbox | **none**; text analysis, review, and handoff only |
| Timeout / budget | `claude_timeout` and turn budget apply to every runtime | same | same |
| Model | `claude_model` | `codex_model` | `openai_model` (default `gpt-5.6-sol`; free-form, e.g. `grok-4.5`) |
| Credential | local Claude auth/key | local Codex login | local env, default `OPENAI_API_KEY` |
| Endpoint | — | — | official OpenAI, or `openai_base_url` / `OPENAI_BASE_URL` |

Model and runtime can be **hot-swapped** on the webui Monitor tab while running (confirm dialog; next turn; persisted back to `agents.yaml` so restarts keep them). Switching to `openai` requires its key (or a configured `openai_base_url` for a local CLI Proxy) to have been available when the client was built; restart once if the key or base URL was added later.

### Local CLI Proxy (Grok / Antigravity / …)

The `openai` runtime accepts any **OpenAI-compatible** Responses endpoint, including [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) on the host:

```yaml
defaults:
  runtime: openai
  openai_base_url: http://127.0.0.1:8317/v1   # or set OPENAI_BASE_URL in .env
  openai_model: grok-4.5
  openai_api_key_env: OPENAI_API_KEY          # proxy client key if required
```

- `openai_base_url` (agent → defaults → `OPENAI_BASE_URL`) redirects the SDK away from `api.openai.com`.
- `openai_model` is free-form: `grok-4.5`, Antigravity/Gemini ids exposed by the proxy, etc.
- With only a base URL (no real key), a local placeholder key is used so the SDK can start; set `OPENAI_API_KEY` to match the proxy’s `api-keys` when it enforces auth.
- WebUI model dropdown seeds common names and merges `GET {base_url}/models` when the proxy is up; you can also type a custom model id on the Monitor tab.

The shared YAML stores only the `openai_api_key_env` variable name (and optional `openai_base_url`). The key is
read from the local `.env`, removed from the parent environment after startup,
and never written to Slack, SQLite, or the remote roster. Slack prompt/context
is sent to the Responses API and stored response IDs continue each thread. Use
Claude/Codex when an agent must inspect or mutate the local checkout.

## State persistence

Per-thread state (session resume ids, handoff summaries, token stats, last-seen) is written through to SQLite — `STATE_DB`, default `state.db`; set empty to disable only when finite owner quotas are not configured — and restored on startup, so a restart no longer wipes agent memory. A configured finite quota fails startup if `STATE_DB` is empty or cannot be opened. Rows are scoped by the authenticated Slack team id and project id; a session from another/unknown Slack workspace, or from a channel's former project, is never silently resumed. Existing databases are migrated safely, with legacy rows left in the empty legacy scope. Deployments without projects keep the stable team-only scope for compatibility. Resume ids are engine/cwd-bound: after a runtime or workspace change the id is dropped and the summary hands over instead. `!reset` deletes the persisted row too; rows idle past 48h are swept. Turn budget is deliberately not persisted. In a channel it resets when an allowed human mentions an eligible project agent or sends a valid structured handoff to one; an allowed human DM resets it without an `@`. Guest, casual, unknown-target, and out-of-project messages do not reset it. In Docker the DB lives on the `agent-state` volume.

`StateStore` holds a non-blocking OS lock at `${STATE_DB}.lock` for its full
lifetime. A second node pointed at the same database fails startup instead of
silently sharing sessions. The DB, lock, WAL, and SHM files are private; every
owner/node must use its own state path or volume.

## Shared local-first transcript

All local agents in one `multi_app` process share one bounded transcript keyed by
Slack team, channel, and thread. Every delivered root/reply/edit/delete event is
ingested before deduplication, project routing, authorization, or activation
returns, so two or three local agents do not each refetch the same thread.
Stored messages are timestamp ordered and keep only the fields needed for
context classification; the current roster, project ACL, human allowlist, and
guest/feed labels are applied when reading, not frozen when writing.

The normal context hot path is local-only: observing a live root makes the
thread context-warm with **zero** `conversations.replies` calls, but live
observation alone is not canonical authority. Canonical handoff verification
beyond the latest full-snapshot watermark triggers one process-shared
same-thread backfill. A cold or incomplete thread likewise uses one same-thread
singleflight backfill, cursor pagination, and `limit=15`; a restored thread is
fully revalidated from its root once per process boot (no `oldest` shortcut)
before becoming authoritative. In-process memory-LRU reloads retain that boot's
validation, while a new boot treats persisted rows as unknown until the one
full revalidation completes. Only a terminal cursor **and a root present in the complete
snapshot** mark it complete. The successful snapshot updates offline edits and
tombstones persisted messages deleted while the process was down; per-message
compare-and-swap guards ensure live new/edit/delete events arriving during the
backfill win. A failure, missing root, ambiguous coverage, or HTTP 429 leaves
the local suffix usable but partial, performs no missing-message deletion,
honors `Retry-After` (or the configured cooldown), and does not spin retries.
Full restart validation costs up to `ceil(thread messages / 15)` paginated
calls once; under the reduced 1-request/minute tier a long cold thread can take
minutes, while subsequent warm activations still make zero history calls.
Every retained record is capped at 64 KiB by default; oversized structured
payloads are dropped and text is UTF-8-safely truncated. Any truncated record
that could affect canonical ordering makes that snapshot non-authoritative,
even after a successful remote fetch. Canonical distributed handoff/budget
reconciliation never treats partial history as authoritative. If old records
were count/age-capacity-truncated, it is
authoritative only when the current-policy driving-human reset is newer than
the truncation boundary; otherwise handoff fails closed.

Slack's exact reduced-rate condition matters: for commercially distributed,
non-Marketplace apps newly created or newly installed from **May 29, 2025**,
`conversations.history` and `conversations.replies` are limited to **1 request
per minute** and **15 objects per request**. Marketplace apps and internal
customer-built apps retain Tier 3; existing unlisted distributed installations
are not subject to the posted reduced limits. See Slack's
[`conversations.replies` reference](https://docs.slack.dev/reference/methods/conversations.replies/)
and [rate-limit announcement](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/).
This architecture therefore never relies on a history call for each activation
or each local peer.

| Setting | Default | Bound |
|---------|---------|-------|
| `TRANSCRIPT_MAX_THREADS` | `512` | in-memory LRU threads per process |
| `TRANSCRIPT_MAX_MESSAGES_PER_THREAD` | `50` | newest records per thread in memory and SQLite |
| `TRANSCRIPT_DB_MAX_THREADS` | `2048` | transcript threads in SQLite; agent/session rows are untouched |
| `TRANSCRIPT_MAX_RECORD_BYTES` | `65536` | logical bytes per normalized record (min 256 B, hard max 1 MiB) |
| `TRANSCRIPT_MEMORY_MAX_BYTES` | `33554432` (32 MiB) | process-wide transcript memory bytes (hard max 512 MiB) |
| `TRANSCRIPT_DB_MAX_BYTES` | `268435456` (256 MiB) | SQLite logical transcript bytes (hard max 4 GiB) |
| `TRANSCRIPT_TTL_SECONDS` | `604800` (7 days) | transcript age retention |
| `TRANSCRIPT_RETRY_COOLDOWN_SECONDS` | `60` | minimum failed-backfill retry delay |

The memory and SQLite byte caps must each be at least the per-record cap;
invalid or inverted values fail startup without including message content in
the error.

`/state` and the WebUI show aggregate in-memory/persisted message and byte
capacity, complete/partial, eviction, backfill-call, and failure counters
only—never message text, user content, or credentials.

## Reply language

`reply_language` (agent → defaults → `日本語`) is injected into the system prompt.  
Current `agents.yaml` default is often `English`; set `中文` / `日本語` / `English` as needed.  
**Also hot-swappable** per agent on the webui Monitor page (next turn; persisted to `agents.yaml`).

## Console (Web UI): config + monitor + hot-swap

```bash
make webui        # = .venv/bin/python webui.py → http://127.0.0.1:8765
```

Four tabs:

**Guide** (the first-visit start page)

- Five ordered stages: assign roles → create Slack Apps → connect private local accounts → write role prompts → run the first task/handoff
- Browser-local progress checklist; no setup state is sent to the server
- Copy-ready Slack channel rules, human task kickoff, structured `HANDOFF`, and dev/reviewer/planner persona templates
- Agent responsibility matrix makes local-tool and OpenAI no-tool boundaries explicit
- Protected tabs ask for the control Bearer only when opened; the static guide itself needs no credentials

**Monitor** (data from running `multi_app` admin API)

- **Tracked issues**: open GitHub issues across the agents' effective repositories (number / title / status labels / assignee); 30s cache; needs local `gh` login
- Top status: online / offline, agent count, busy threads, sessions, connections
- Per-agent “node”: status rail, runtime·model, sessions / patrol, recent threads (busy / tokens / remaining budget / idle)
- **Hot-swap model** (with confirm): applies to the running process next turn, no restart
- **Hot-swap runtime** (with confirm): claude / codex / openai; drops incompatible sessions
- **Reply language** dropdown (中文 / 日本語 / English): next turn
- **Reasoning effort** by runtime (openai: none…max; claude: low…max; codex: minimal…xhigh); empty = engine default
- **Session restart**: clear all sessions for that agent (summaries kept for handoff)
- Model lists from real engine config (`~/.claude/settings.json`, Anthropic API if keyed, `~/.codex/config.toml`)
- **UI language** 中 / 日 / EN (browser-local; independent of agent `reply_language`)
- Auto-refresh every 5s (pauses during pending confirm dialogs)

**Config** (writes `agents.yaml` / `.env`; saves hot-reload the running `multi_app` — adding/removing agents still needs a restart)

- Agent list, token status (bot / app), runtime, role summary
- **Setup wizard**: generate per-agent manifest → copy → create on api.slack.com → paste tokens → verify (`auth.test` + `apps.connections.open`) → write `.env`
- **Edit persona** / **Add agent**

**Auth** verifies the Claude, Codex, and GitHub login in the environment that
actually runs the agent (Docker when available, otherwise the host). Each owner
connects only their own accounts.

Webui binds **127.0.0.1 only** (Host header check against DNS rebinding). Live data comes from admin API (`ADMIN_BASE`, default `http://127.0.0.1:8766`). Owner/distributed configurations require an environment-only Bearer named `SLACK_AGENT_CONTROL_TOKEN_<SLACK_USER_ID>` (32+ unique random printable characters). The browser keeps it in `sessionStorage` and sends `Authorization: Bearer ...`; identity headers are ignored. Owners see/change only their agents, top-level admins can manage all agents, and global reload is admin-only. `/healthz` remains public; sensitive reads and every write require authentication. Saves create `.bak` backups (gitignored); YAML comments in `agents.yaml` are lost on save.

## Slack App setup (manual)

(Skip if using the Web UI wizard.) Each agent needs its **own Slack App**.

1. [https://api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest**
2. Paste `slack-app-manifest-agent.yaml`; change three fields per App:
   - `display_information.name` (e.g. `Agent (dev)`)
   - `features.agent_view.agent_description`
   - `bot_user.display_name` (e.g. `dev` / `pm`)
3. **Install to Workspace**
4. Tokens:
   - **Bot User OAuth Token** (`xoxb-...`)
   - **App-Level Token** (`xapp-...`, scope `connections:write`)
5. Write into `.env` as `{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN`
6. Repeat for each agent

Manifest includes **`agent_view`** (Agent messaging UX). Requires `slack-bolt>=1.29.0`.

### optional semantics

- `optional: true` (pm / qa / designer / dx): missing tokens → **warn and skip**, process continues
- Configure tokens and restart to load
- Non-optional (dev / reviewer) missing tokens → `RuntimeError`
- All agents skipped → `RuntimeError` (no valid agents)

### Scopes

| Group | Scopes | Notes |
|-------|--------|-------|
| Core | `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `im:history`, `mpim:history` | Receive / reply / thread context |
| Agent UX | `assistant:write` | AI sidebar / assistant threads |
| Info | `channels:read`, `groups:read`, `im:read`, `im:write`, `mpim:read`, `mpim:write`, `users:read` | Channel membership (`conversations.members`) and user metadata |
| Reactions | `reactions:read`, `reactions:write` | Status on trigger msg (⏳ working → ✅ / ❌) |
| Files | `files:read`, `files:write` | Attachment ingest (`files:read`); write optional |
| Customize | `chat:write.customize` | Phase 2 dynamic persona; optional |

**After scope/manifest changes, Reinstall to Workspace** or new scopes/events will not apply.

### Slack-native extras

- `trusted_feed_bots: [B0XXXXXXX]` in `agents.yaml` — allowlisted Slack app bots (e.g. GitHub) become read-only `[feed]` context; they never activate agents.
- Human-attached files on the triggering message are downloaded to `<workspace>/.slack-files/` (max 3 files, 10MB each; swept after 48h). Agents read them with local tools.
- Status uses reactions on the trigger message (📥 queued behind a busy thread or full node → ⏳ while working → ✅ done / ❌ failed / 🤐 reply withdrawn by the freshness recheck); no "working…" placeholder post. A provider cooldown wait of 30s+ posts one notice with the expected resume time.
- Failures get an actionable notice by category (context too long → `!reset`; auth / billing → node owner). Patrol stops after 3 consecutive failures of one category, posts one notice to its channel, probes every 6th round, and resumes on the first success (`/state`: `patrol_fence`, `last_failure`).
- A restart no longer leaves ⏳ forever: admitted activations are recorded in the state DB (`activation_ledger`), and on startup each one the previous process left unfinished gets ⚠️ and a thread notice asking the requester to check and re-mention. It is never re-run automatically, because the cut-off turn may already have pushed or commented.
- Triggers that queue in one thread (e.g. "@dev add X", then "@dev also Y" while it is busy) are answered together in one turn, with every trigger listed in order, instead of a second turn re-answering what the first already saw as context.
- A registered agent written as plain-text `@name` notifies nobody; the post gets a one-line warning instead of a silently stalled handoff.

## .env

See `.env.example`. Minimum: required agents; add optional as needed:

```bash
# Required
DEV_SLACK_BOT_TOKEN=xoxb-...
DEV_SLACK_APP_TOKEN=xapp-...
REVIEWER_SLACK_BOT_TOKEN=xoxb-...
REVIEWER_SLACK_APP_TOKEN=xapp-...

# Optional (skipped at start if unset)
# PM_SLACK_BOT_TOKEN=...
# QA_SLACK_BOT_TOKEN=...
# DESIGNER_SLACK_BOT_TOKEN=...
# DX_SLACK_BOT_TOKEN=...

# Strongly recommended: allowlisted human Slack user IDs
ALLOWED_SLACK_USERS=U01ABCDEF

# Optional Claude workspace when agents.yaml omits workspace
CLAUDE_WORKSPACE=/path/to/project
```

Override env names in `agents.yaml` with `bot_token_env` / `app_token_env`.

## Start

### Docker (recommended)

```bash
cp .env.alice.example .env.alice   # Alice's machine only; fill Alice values
NODE=alice make up                 # builds and starts only Alice's node
NODE=alice make logs
NODE=alice make restart
NODE=alice make down
NODE=alice make test-docker
make help
```

Compose notes:

- **One profile per person**: Bob uses `.env.bob` and `NODE=bob`; do not start
  another person's profile on your machine.
- **Credentials**: each profile has independent Claude, Codex, and gh volumes
  plus its own env file. Nothing is baked into the image or mounted from the
  other owner.
- **State/workspaces**: Alice and Bob use different state and workspace volumes.
  The only shared mount is credential-free `roster.yaml`, read-only.
- **Isolation**: both images finish as the non-root `agent` user. Admin ports
  bind only to localhost (`8766` for Alice, `8767` for Bob).

### Local venv

```bash
.venv/bin/python -m pip install -r requirements.txt
make run     # = .venv/bin/python multi_app.py
make test
make webui
```

Startup logs print the roster (name / user_id / workspace). Optional agents without tokens log a skip warning.

## Usage

1. Invite bots into a shared channel (at least `dev`, `reviewer`)
2. e.g. `@dev implement bar() in foo.py, then have reviewer check`
3. `dev` finishes and `@reviewer`; `reviewer` activates on mention
4. With `pm`: `@pm` to split work first
5. An allowed human’s eligible agent `@mention` or valid structured handoff **resets** that thread’s handoff budget (DMs do not require `@`)

In DMs, humans can talk without `@` (peers do not hand off in DMs).

## Context auto-management

When an agent’s latest turn input context (input + cache tokens) exceeds the threshold:

1. **Summarize** (one tool-free turn, ≤800 chars handoff summary)
2. **Switch** session (drop Claude session_id)
3. **Handoff** inject summary on next activation

| Item | Detail |
|------|--------|
| Threshold | `context_rollover_tokens`, default **60000** |
| Resolution | agent field → `defaults` → `60000` |
| Missing usage | treated as 0 tokens; **no** rollover |

## Reliability

| Mechanism | Detail |
|-----------|--------|
| Hard timeout | `claude_timeout` (default **900**s); releases lock and notifies thread |
| Slack 429 | `AsyncRateLimitErrorRetryHandler` (Retry-After, up to 2 retries) |
| Memory reclaim | Thread state idle **48h** reclaimed (scan ~every 10 min) |
| Long threads | Shared bounded local transcript; only cold/incomplete threads backfill with cursor pages of at most 15. When the context block must be truncated, the thread root (usually the task definition) is kept and the middle is dropped |
| Reply freshness gate | Before posting, peer/allowed-human messages that arrived mid-turn trigger exactly one re-decide pass (`POST_ORIGINAL` / revised text / `NO_REPLY`); a verbatim duplicate of the latest non-self message is never posted. Reads only the local transcript (zero extra Slack API calls) and fails open on any gate error. Disable with `FRESHNESS_RECHECK=0` |
| Provider rate-limit cooldown | A rate-limited AI turn (429 / usage-limit / overloaded signals from Claude, Codex, or the OpenAI API; `Retry-After` honored) arms a cooldown shared by every local agent on the same provider account (all Claude agents share the local Claude login, all Codex agents the Codex login; OpenAI is per endpoint + key variable) — base **60s**, doubling per consecutive strike up to **480s**. The turn is retried once after it expires only when that is provably safe: no side-effecting tool (shell, edit, MCP) ran, the provider's reset time fits within 15 minutes, and the thread was not reset; otherwise the thread gets a specific notice instead of a silent replay. A cooldown wait hands its node slot back so other agents keep running. Turns starting during a cooldown wait first; patrol rounds are skipped instead (next epoch retries). A clean turn resets the streak. State appears in `/state` as `provider_cooldown`; tune with `PROVIDER_COOLDOWN_BASE_SECONDS` / `PROVIDER_COOLDOWN_MAX_SECONDS` |
| Adaptive turn pacer | All local agents share one node-wide timeline of provider turn starts, spaced by an adaptive interval — base **0.5s**, doubling on each rate-limited turn up to **8s**, halving back toward base after **5** consecutive clean turns — so 2-3 agents on one shared provider account never fire in lockstep. Applies to Slack, freshness-recheck, and patrol turns; the wait happens before the per-turn timeout window opens and does not reduce total concurrency. State appears in `/state` as `turn_pacer`; tune with `PROVIDER_PACER_BASE_SECONDS` (0 disables) / `PROVIDER_PACER_MAX_SECONDS` / `PROVIDER_PACER_CLEAN_TURNS` |
| Socket delivery gap | A Socket Mode link that stays down (or a host that sleeps) for **120s+** may outlast Slack's redelivery window, so every warm shared transcript is revalidated from the thread root on its next read instead of serving the hole as context. Tune with `SOCKET_GAP_REVALIDATE_SECONDS` |

## Security

| Mechanism | Detail |
|-----------|--------|
| Credential isolation | After load, all `*_SLACK_BOT/APP_TOKEN` keys are scrubbed from process env (Claude children inherit `os.environ`); `.env` mode 0600 |
| Control-plane auth | Owner/distributed mode resolves principal solely from unique environment-only Bearers; control tokens are scrubbed before AI/CLI children start. WebUI/admin writes are owner-scoped and global reload is top-level-admin only |
| Context auth filter | Self / current-channel peers / allowed humans retain normal context; non-authoritative humans appear only as continuation-safe `[guest]` information, while unknown bots/system events stay excluded |
| State DB permissions | `STATE_DB` (default `state.db`) stores session state and the bounded local transcript. Directory is `0700`, DB/WAL/SHM are `0600`. Treat like secrets; do not share the file or volume across untrusted users |

Residual risk: agent Bash shares the OS user and can still read `.env` and the state DB on disk. Rotate tokens; use separate OS users/containers in production.

### Ops commands

Mention the target agent with a command (**no** turn-budget cost; does **not** trigger normal activation):

| Command | Effect |
|---------|--------|
| `!status <@agent>` | Session / recent tokens / turns / handoff summary / remaining budget |
| `!reset <@agent>` | Full reset of session, stats, and summary for that thread |
| `!roles <@agent>` | Current-channel agent roster + role summaries (`card`, or persona first line if card is empty) |

Example: `!status <@U_DEV>`. Mainly for humans and `dx`.

## Channel guidance (topic / purpose)

On activation, agents load channel **topic** and **purpose** (5 min cache) as channel ops rules at the top of the prompt. Put rules in the channel topic/description.

## Collaboration friction controls

| Control | Behavior |
|---------|----------|
| One ask per message | Single handoff per message; no multi-agent same-task spam |
| No rehash | Do not repeat thread-known facts |
| No filler replies | No ack-only chatter; keep replies to the point |
| Turn budget | `budget.max_agent_rounds` (default **12**) |
| dx traffic control | Short corrections when loops / idle chatter appear |

## Loop prevention

| Mechanism | Behavior |
|-----------|----------|
| Speak when `@`’d | Public/private channels require `<@user_id>` mention |
| Turn budget | Max agent→agent handoffs per thread |
| Human reset | Allowed human + eligible project-agent `@mention` or valid structured target; an allowed human DM needs no `@` |

On exhaust, one pause notice per thread until an allowed human sends a driving message. Guest and casual channel messages do not restore the budget.
Guest lines remain visible as read-only `[guest]` context, but cannot activate,
reset, run commands, authorize handoffs, or supply instructions to an agent.

## Common pitfalls

1. **Scope changes need reinstall** or history/context APIs fail silently (code degrades to empty context).
2. **Bot-to-bot `@` does not reliably fire `app_mention`** — activation is on `message` events.
3. **Token env names** default to `{NAME}_SLACK_BOT_TOKEN` / `{NAME}_SLACK_APP_TOKEN`.
4. **ALLOWED_SLACK_USERS** empty = any human can trigger; set a whitelist in production.
5. **Workspace permissions** — Edit/Write/Bash can change the machine; lock down workspace + allowlist.
6. **`slack-bolt` ≥ 1.29.0** for `agent_view`.
7. **Remote agent classified as unknown bot** — every host needs that logical entry's `slack_user_id` and `slack_bot_id`.

## Multi-host deploy

Use the decentralized Slack-bus layout in [`roster.yaml`](roster.yaml),
[`agents.alice.yaml`](agents.alice.yaml), and
[`agents.bob.yaml`](agents.bob.yaml). Ship the same credential-free roster to
every host, but ship/use only that person's local runtime file and env file on
their node. `roster.yaml` is the identity/routing truth (name, Slack user/bot
ids, owner, node, card, project ACL); it rejects token/env, workspace, runtime,
GitHub, and tool fields. The local file contains only this node's 2–3 runtime
definitions and credential variable names.

Each person keeps only the Slack App token pairs for their own agents.
Claude/Codex login state, OpenAI API keys, GitHub auth, workspaces, and SQLite
remain on that person's machine. Cross-node coordination and context transfer
happen only through Slack messages/handoffs; AI accounts, login directories,
local sessions, filesystems, and state databases are never shared.
Every logical agent declares canonical `owner: U...`; each host must set its
local owner's control Bearer. A top-level-admin Bearer is optional per node and
grants access only where installed, so no shared team token is copied across
hosts. Missing owner, short, or duplicate installed Bearers fail startup.

The shared roster may also set one UTC daily token budget per owner:

```yaml
quotas:
  daily_total_tokens:
    U01ALICE: 250000
    U02BOB: 250000
  reservation_tokens:
    U01ALICE: 20000
    U02BOB: 20000
```

All 2–3 local agents owned by the same person share that ledger and must be
assigned to one `node_id`; a shared roster that splits one owner across nodes
is rejected. Before taking
a node/queue slot, a turn atomically reserves capacity in SQLite with
`BEGIN IMMEDIATE`; admission requires
`used + active reservations + new reservation <= daily limit`. Queue-full,
task-creation failure, and cancellation before provider start release the
reservation. A started provider turn settles its exact reported usage, including
output tokens. Exact usage may exceed the reservation, so at most that finishing
turn can overshoot the daily limit; later turns are denied. A started turn that
fails or returns incomplete usage is conservatively charged at least its
reservation and marked `estimated`/`error`. Startup recovery releases stale
unstarted reservations and conservatively settles stale started ones.

`input_tokens` includes cache input for quota accounting, `output_tokens` is
always added to the daily total, and `cache_tokens` is also retained as a
diagnostic subset of input. Claude reports direct + cache-read + cache-creation
input separately; Codex reports direct + cached input separately; OpenAI
Responses already includes cached tokens in `input_tokens`, so its cached count
is not added twice. If output usage is absent, the turn is estimated rather
than treated as zero. `/state` and the WebUI show limit, used, remaining, active
reserved, denied, errors, and agent/runtime breakdowns without prompts or
message text. An owner sees only their ledger; an installed top-level admin sees
all ledgers on that node.

At startup all logical agents are registered for routing, including remote
`slack_user_id` / `slack_bot_id`, owner, node and card. Only matching local
agents with both Slack tokens start Socket Mode and an AI runtime. This fixes
remote bot handoffs without sharing AI accounts.

Remote roster add/remove/card/owner changes hot-replace the in-memory routing
maps without reconnecting local Slack sockets. Local Slack identity, owner, or
node changes are restart-only and retain the already authenticated identity
until restart. Invalid or conflicting roster snapshots cause zero mutation.

Each host needs:

1. Python env (`.venv` + `requirements.txt`)
2. Its own `claude` / `codex` login or `OPENAI_API_KEY`, and, if used, its own `gh auth login`
3. A **separate clone** per writable agent with `git config user.name "<agent>-agent"`; when GitHub is enabled, every `origin` fetch and push URL must resolve to the configured canonical `OWNER/REPO`, otherwise GitHub workflow and patrol are disabled for that workspace; lease CAS pushes ignore mutable remote names and use the explicit canonical `https://github.com/OWNER/REPO.git`, so configure non-interactive HTTPS credentials first (for example, `gh auth setup-git`)
4. `AGENT_NODE_ID=<this-host>` and only local `{AGENT}_SLACK_*` variables

AI and GitHub credentials are deliberately node-local: every person signs in
to Claude/Codex/OpenAI and `gh` on their own machine. No account or token is
shared. Repositories resolve independently for every agent in this order:
`agents[].github_repo` → `defaults.github_repo` → legacy `github.repo`.
An explicit empty `github_repo: ""` disables GitHub for that agent. Values must
be canonical `OWNER/REPO` slugs.

`projects` binds Slack channel ids to member human ids, admins, and allowed
logical agents. When projects are configured, unmapped channels and
out-of-project humans/agents cannot exercise authority. The handoff roster is
the configured project-agent set intersected with complete
`conversations.members` results (all logical agents are the configured set
without projects), cached per Slack team/channel for 5 minutes. System prompts,
`!roles`, inbound routing, result handoffs, and patrol use that same view;
absent targets are rejected once. Incomplete/error/cursor-loop lookups warn and
fall back only to the configured boundary, never beyond a project ACL. A 1:1
DM exposes self only as an agent target. `!reset` is owner/project-admin only.
A project admin must be listed explicitly under `projects[].admins`, remain a
human allowed by `ALLOWED_SLACK_USERS`, and has destructive agent-management
authority for that project; ordinary project membership never inherits it.

`node.max_concurrency` (default 2, also `NODE_MAX_CONCURRENCY`) caps local AI
runs. `node.max_queue` (default 10, also `NODE_MAX_QUEUE`) is the exact number
of jobs allowed to wait; `0` forbids waiting. A job waits when it cannot reserve
both a node slot and its realpath workspace immediately. A workspace-blocked
job does not consume a node execution slot, so independent workspaces can still
run. Excess Slack activations receive one busy reply instead of creating a
task. Queue/running and node capacity counts appear in `/state`.

Config reload first parses and validates the whole YAML without mutation, then
handles each running agent independently. Safe idle changes apply immediately;
a busy agent's runtime/workspace/repository snapshot is deferred in full, with
the newest reload replacing any older pending snapshot. It is consumed after
all running or queued Slack/patrol work exits, including error and cancellation.
The exact `(realpath workspace, repository)` pair is preflighted before a
repository/workspace snapshot becomes active. Failure keeps the complete old
config and sessions. `/reload` reports `applied`, `deferred`, `skipped`,
`restart_required`, and `failed` per agent; `/state` exposes pending state.
Agent additions/removals, token-variable changes, identity/node/project
changes, and patrol scheduling changes still require a process restart.

Generated and incoming handoffs are single-target. If one message mentions
multiple registered agents, the first target wins deterministically and Slack
receives a visible warning; no silent fan-out occurs. In a structured handoff,
`target_agent_id` is authoritative even without an `@`; incidental mentions are
ignored, and an unknown/out-of-project target activates nobody. Agents may use:

```text
HANDOFF {"target_agent_id":"bob/reviewer","task_id":"TASK-12","goal":"Review PR 12","done_criteria":["tests pass"],"artifact":"https://.../pull/12"}
<@U02BOBREVIEW>
```

**Budget convergence:** before a peer activation, every host reads the complete
Slack thread and ranks eligible handoffs after the latest allowed-human message
by Slack source timestamp. Only the first `max_agent_rounds` may run. Missing or
incomplete history fails closed. The Slack bus is the shared ordering authority;
no additional coordinator or shared AI account is required.

| Host | Agents | Tokens in `.env` |
|------|--------|------------------|
| alice-mac | alice/dev, alice/reviewer | `ALICE_DEV_*`, `ALICE_REVIEWER_*` |
| bob-mac | bob/dev, bob/reviewer | `BOB_DEV_*`, `BOB_REVIEWER_*` |

Agent ids containing `/` or `-` are normalized to `_` in default token env
names. `optional` remains a legacy local-runtime feature; use `node_id` for
distributed ownership. Once `AGENT_NODE_ID`/`node.id` is set, every agent must
declare `node_id` or explicit `local: true/false`; ambiguous ownership is rejected.
A remote logical entry must include both Slack ids.

## Per-thread Git worktrees

For code agents that share one local repository, `thread_worktree` gives each
Slack root thread a deterministic branch and worktree. All local agents on that
node reuse the same path for the same team/channel/root thread, so their turns
are serialized by one lease; different threads can run concurrently up to the
node limit. The mapping lives in `STATE_DB` and app-owned `git worktree`
operations are serialized across processes.

```yaml
worktrees:
  root: /app/worktrees
  base_ref: main
  max_per_repo: 16

defaults:
  workspace_mode: thread_worktree
```

The three values are mandatory when the mode is enabled. They can instead come
from `WORKTREE_ROOT`, `WORKTREE_BASE_REF`, and `WORKTREE_MAX_PER_REPO`.
`WORKTREE_ROOT` must be absolute, outside the base repository, and free of
symlink components. Compose mounts a separate owner-specific worktree volume
(`alice-worktrees` / `bob-worktrees`); it is not the state volume and is never
shared between people. Claude and Codex run with the worktree as `cwd`. OpenAI
API agents hold the same scheduler lease but receive no local filesystem tool.
Patrol and serial-mode jobs stay in the configured base workspace.

`WORKTREE_ROOT` is restart-only. A reload reports `worktree_root` under
`restart_required` and keeps the live root, repository spec, mappings, and
sessions unchanged; `base_ref` and `max_per_repo` retain their existing
idle/deferred hot-reload behavior. On restart, a `ready` or `creating` mapping
still pointing at the old root fails closed: restart once with the old
configuration and clean-remove it first. A mapping is rehomed to the new
deterministic path only when its status is `removed`, the old path is absent,
the retained managed branch is intact and not checked out, and neither root
shows conflicting Git metadata. Dirty, active, missing-branch, or tampered
mappings are never migrated.

Repository-control and per-thread lease files live in a fixed owner-only
directory under the canonical Git common-dir, not under `WORKTREE_ROOT`.
Consequently, separate processes using different roots or state databases for
the same repository still serialize creation/removal and the same thread
runtime. Capacity is derived from `git worktree list --porcelain` across every
root and counts only exact `refs/heads/slack-agent-wt/<64-lowercase-hex>`
mappings; malformed managed-prefix entries fail closed.

The monitor lists only the authenticated owner's mappings (admins see all).
Manual removal is owner/admin-only and fail-closed: active, dirty, untracked,
unpushed, missing/tampered path, `.git`, or branch state is rejected. A clean
removal removes only the worktree and retains its branch; the next turn restores
the same branch and path. There is no automatic destructive cleanup.

Provider runtimes are still trusted local processes. The app does not install a
`git`/`gh` wrapper: the system prompt forbids agents from running
`git worktree`, `git gc`, `git prune`, switching away from the managed branch,
or destructively changing shared refs. Unrestricted Bash can technically bypass
that instruction, so enable this mode only for agents and repositories you
trust. AI subscriptions, CLI login directories, Slack tokens, GitHub tokens,
state databases, base workspaces, and worktree roots remain per-person local;
none are shared to make cross-host collaboration work.

## GitHub workflow pilot

Each agent uses its effective `github_repo`; top-level `github.repo` remains a
backward-compatible fallback. Channel flow: issue → PR → review.

Sample ask:

> `@dev work on issue #1, then ask reviewer for review`

Git discipline (in system prompt):

- **refresh remote information before reading local code** (`git fetch`; serial
  workspaces may use `git pull`)
- **commit & push + PR URL before other agents look** — unpushed work does not exist on other hosts
- in `thread_worktree`, keep the managed branch: never use
  `gh issue develop --checkout` or `gh pr checkout`

Expected path:

1. **dev** atomically claims issue with its issue-specific Git-ref lease, then records the matching agent/node/timestamp marker
2. **dev** reads the issue with `gh issue view`; in `thread_worktree` it works
   on the already-managed branch without checkout
3. **dev** fix → commit/push → `gh pr create`
4. **dev** posts PR URL and `@reviewer`
5. **reviewer** uses `gh pr view` / `gh pr diff` (and the shared thread
   worktree when local) without switching the managed branch
6. **reviewer** LGTM → merge + `gh issue close`

### Patrol

Agents can periodically scan `status:todo` issues, atomically claim one with an issue-specific Git-ref lease, and progress. The lease protocol runs in `issue_claim.py` (`claim` / `renew` / `release` / `verify`, one JSON verdict; only `claimed` / `renewed` is ownership), not in the model: patrol lists todo issues and claims the oldest claimable one on the host, so an idle round spends no provider turn, and the host renews the lease while the turn works — a failed or unknown renewal cancels the turn. Interactive turns call the same tool; refs and markers are unchanged, so nodes on the previous prompt-driven protocol interoperate. The lease is 30 minutes, is renewed at least every 15 minutes, and becomes takeover-eligible only after a 5-minute grace measured from GitHub's server `Date`. Create, renewal, stale takeover, and release use `--force-with-lease` against an absent or exactly observed ref SHA. Before work, the ref, metadata commit, and marker comment must agree on issue, agent, node, nonce, timestamps, and SHA. Any read failure, missing/mismatched marker, failed command, timeout, or uncertain result is fail-closed. Operators should inspect and conditionally recover stale refs; never delete them unconditionally. The shared GitHub assignee is not ownership.

Patrol phases use epoch-aligned absolute deadlines and the globally stable logical-agent roster, so different nodes share the same wall-clock schedule. A long run skips missed periods instead of catching up in a burst. Patrol and Slack turns share the same node concurrency limiter and realpath workspace lock.

```yaml
github:
  repo: OWNER/DEFAULT-REPO     # legacy fallback
  patrol_channel: C0XXXXXXXX   # where patrol posts

defaults:
  github_repo: OWNER/TEAM-REPO

agents:
  - name: dev
    github_repo: OWNER/DEV-REPO # per-agent override
    patrol_interval: 300       # seconds; 0 = off

  - name: reviewer
    github_repo: ""             # explicit GitHub disable
```

Repository/workspace changes are hot-applied only after target preflight (or
deferred until idle). Restart after changing patrol interval/channel. Logs and
`/state` show active repository, pending config, and patrol state.
