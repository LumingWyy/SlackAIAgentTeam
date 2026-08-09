# Repository Guidelines

## Project Structure & Module Organization

`multi_app.py` is the runtime entry point and contains Slack, Claude, and Codex integration. Keep reusable decision logic in `multi_core.py`; `webui.py` serves the local configuration and monitoring console. Agent definitions live in `agents.yaml`, while Slack setup templates are in `slack-app-manifest*.yaml`. Tests mirror these boundaries in `tests/test_multi_core.py`, `tests/test_multi_app_config.py`, and `tests/test_webui.py`. Docker support is defined by `Dockerfile` and `docker-compose.yml`.

## Build, Test, and Development Commands

- `.venv/bin/python -m pip install -r requirements.txt` installs local dependencies.
- `make run` starts the multi-agent Slack process from the local virtual environment.
- `make webui` serves the console at `http://127.0.0.1:8765`.
- `make test` runs the full pytest suite; use `.venv/bin/python -m pytest tests/test_multi_core.py -q` for a focused run.
- `make build` builds the container image, and `make up`, `make logs`, and `make down` manage it.
- `make test-docker` verifies the suite inside the image. Run `make help` for all supported targets.

## Coding Style & Naming Conventions

Use four-space indentation and follow the existing PEP 8-style layout. Name functions and variables `snake_case`, classes `PascalCase`, and constants `UPPER_SNAKE_CASE`. Add type hints to public data flows and short docstrings where behavior or precedence is non-obvious. Keep pure parsing, activation, budgeting, and formatting logic in `multi_core.py`; isolate network and process side effects in integration modules. No formatter or linter is pinned, so match nearby import grouping and line wrapping.

## Testing Guidelines

Tests use pytest 8+. Name files `test_<area>.py` and functions `test_<behavior>`. Add regression coverage for every bug fix, including failure paths and configuration precedence. Prefer fixtures such as `tmp_path` and `monkeypatch` over real credentials or external Slack calls. There is no documented coverage percentage; reviewers expect relevant tests to pass locally and, for image changes, in Docker.

## Commit & Pull Request Guidelines

Recent history follows Conventional Commit-style subjects: `feat:`, `feat(webui):`, `fix(webui):`, `test:`, and `chore:`. Keep commits focused and use an imperative summary. Pull requests should explain the user-visible change, link the issue when applicable, list verification commands, and include screenshots for console UI changes. Call out configuration, manifest, scope, or credential-handling changes explicitly.

## Security & Configuration

Copy `.env.example` for local setup and never commit `.env`, tokens, auth files, or generated backups. Keep secrets in environment variables; `agents.yaml` should reference token variable names, not token values. Preserve localhost-only bindings for the web UI and admin API unless a security review approves broader exposure.
