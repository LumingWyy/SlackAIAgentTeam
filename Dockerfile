# SlackAgentTeam runtime image: Python + Claude Code CLI + Codex CLI + gh
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    SLACK_AGENT_IMAGE=1

# Networks that inspect TLS (e.g. Cloudflare Zero Trust / Gateway) re-sign HTTPS
# with their own root CA. Put that CA as PEM in certs/<name>.crt (gitignored,
# see certs/README.md) and the image trusts it at build and run time: apt, curl,
# pip, Python and Node (Claude Code, Codex) all read the system bundle. Without
# a .crt file the image is built as before.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# git / curl / gh CLI / Node.js 22 (Claude Code and Codex CLI need node).
# The NodeSource script is saved before it runs: piped into bash, a failed
# download ran an empty script and apt quietly installed Debian's nodejs,
# which has no npm.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh \
    && bash /tmp/nodesource_setup.sh \
    && rm -f /tmp/nodesource_setup.sh \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh nodejs \
    && node --version && npm --version \
    && npm install -g @anthropic-ai/claude-code @openai/codex \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Run as non-root (part of credential isolation)
RUN useradd -m -u 1000 agent
ENV HOME=/home/agent

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY multi_app.py multi_core.py state_store.py transcript_store.py control_auth.py worktree_manager.py webui.py issue_claim.py agent_guard.py local_config.py team_init.py agents.example.yaml team.example.yaml channel-rules-template.md slack-app-manifest.yaml slack-app-manifest-agent.yaml ./
COPY agent_guard_bin/ agent_guard_bin/
COPY templates/ templates/
COPY tests/ tests/
# Compose mounts agent-state → /app/data; pre-create so non-root can write
RUN mkdir -p /app/data /app/worktrees \
    && chown -R agent:agent /app \
    && chmod 700 /app/data /app/worktrees

USER agent
CMD ["python", "multi_app.py"]
