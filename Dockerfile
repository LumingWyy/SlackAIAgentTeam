# SlackAgentTeam runtime image: Python + Claude Code CLI + Codex CLI + gh
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# git / curl / gh CLI / Node.js 22 (Claude Code and Codex CLI need node)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh nodejs \
    && npm install -g @anthropic-ai/claude-code @openai/codex \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Run as non-root (part of credential isolation)
RUN useradd -m -u 1000 agent
ENV HOME=/home/agent

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY multi_app.py multi_core.py state_store.py transcript_store.py control_auth.py worktree_manager.py webui.py issue_claim.py agent_guard.py agents.yaml ./
COPY agent_guard_bin/ agent_guard_bin/
COPY tests/ tests/
# Compose mounts agent-state → /app/data; pre-create so non-root can write
RUN mkdir -p /app/data /app/worktrees \
    && chown -R agent:agent /app \
    && chmod 700 /app/data /app/worktrees

USER agent
CMD ["python", "multi_app.py"]
