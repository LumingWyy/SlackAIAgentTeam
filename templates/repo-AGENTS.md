# Agent rules

<!-- Template from SlackAgentTeam (templates/repo-AGENTS.md). Copy into the
target repository as AGENTS.md, replace every {{...}}, delete what does not
apply, and add CLAUDE.md containing the single line `@AGENTS.md` so Claude
reads the same file (Codex reads AGENTS.md directly). Changes take effect for
an agent once they are on the branch its worktrees start from. -->

Rules for the developer and reviewer agents working on this repository from Slack.
The channel topic and description are a short summary; this file is the source of truth.

## Context

- What this repository is: {{one or two sentences; link the plan issue if there is one}}
- Checks before every push: {{e.g. make fmt && make vet && make test}}

## Scope

- Implement only issues assigned to you (`gh issue list --assignee @me`). Do not change code for someone else's issue.
- Review is the exception: review any PR you are asked to review, whoever owns it.
- One Slack thread is one issue. Do not extend the work past that issue's done criteria; suggest follow-ups in the thread instead.
- {{decisions the team has not made yet, e.g. "Do not add libraries that issues #2-#11 have not chosen yet; ask a human."}}

## Developer

1. Read the issue (and the plan it belongs to). If the done criteria are unclear, ask once in the thread before writing code.
2. Follow the existing structure and style.
3. Run the checks above before pushing; never push failing checks.
4. In the PR body: what changed / why / how you verified it (commands and results) / {{project-specific item, e.g. which legacy behaviour it matches}}.
5. Ask one reviewer, once. By default your owner's reviewer; if the issue or thread names another reviewer, ask that one (it runs on its owner's quota, so a human decides). After changes, push to the same PR and ask the same reviewer again.

## Reviewer

- Fetch the PR diff and verify it yourself; do not take the developer's summary as evidence. Run the checks yourself.
- Focus on: {{what matters most here, e.g. compatibility, error handling, tests, done criteria}}.
- Write findings on the PR (inline, one thread per finding) with `Verdict: PASS` or `Verdict: CHANGES REQUESTED`; post only the verdict and the PR link in Slack.
- Do not fix the code yourself; send changes back to the PR's developer, whoever owns it.

## Stop and ask a human

- The issue does not say what is wanted.
- {{risky areas for this project, e.g. API compatibility, database schema and migrations, production settings, authentication}}.
- The same finding comes back after two rounds of changes.

## Never write down

- Tokens, `.env` contents, or production connection details: not in code, issues, PRs, or Slack.
