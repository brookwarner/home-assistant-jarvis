# Contributing to Jarvis

Thanks for your interest in improving Jarvis! This is a personal home-automation
project, but contributions are welcome. This guide covers how to get a dev
environment running and what to keep in mind when sending a PR.

## Development setup

You don't need a running Home Assistant instance to develop or run the tests —
the test suite mocks the HA API.

```bash
git clone https://github.com/brookwarner/home-assistant-jarvis.git jarvis
cd jarvis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> The package imports as `jarvis` (e.g. `from jarvis.config import config`). On a
> deployed box the directory is literally `/homeassistant/jarvis`; for tests,
> `conftest.py` registers the working tree as the `jarvis` package automatically,
> so the folder you clone into can have any name.

## Running tests

```bash
.venv/bin/pytest -q
```

CI runs the same suite on every pull request (see `.github/workflows/ci.yml`).
Please make sure tests pass locally before opening a PR, and add tests for new
behaviour where it's practical.

## Project layout

| Path | What it is |
|---|---|
| `bot.py` | Telegram entrypoint — wires triage, conversation, scheduler, webhook |
| `agents/` | `triage.py`, `conversation.py`, `briefing.py` — the model-facing logic |
| `ha_client.py` | Home Assistant REST + statistics client |
| `scheduler.py` | APScheduler jobs (briefings, proactive polling) |
| `anomaly.py` | Daily anomaly-detection engine |
| `webhook_server.py` | Receives HA events via `rest_command` |
| `config.py` | Env/`.env` configuration |
| `scripts/onboard.py` | Interactive first-run setup |
| `addon/` | Home Assistant local add-on packaging |
| `docs/superpowers/` | Design specs and implementation plans |
| `tests/` | Pytest suite (HA API is mocked) |

## Coding conventions

- **Python 3.12+.** Match the style of the surrounding code — type hints on new
  functions, `from __future__ import annotations` where the file already uses it.
- Keep model strings configurable via env vars (`TRIAGE_MODEL`,
  `CONVERSATION_MODEL`, ...) rather than hard-coding them.
- New tools for the conversation agent should be small, well-described, and
  covered by a test in `tests/`.

## Never commit personal data

This repo is public. Instance-specific and secret files are gitignored and must
**stay** untracked:

- `.env` — tokens and keys
- `soul.md`, `memory.md`, `ha_entities.md` — your personal persona/home data
- `jarvis.log`, `jarvis.pid`

Use the tracked `.example` templates (`.env.example`, `soul.example.md`,
`ha_entities.example.md`) when documenting or testing. Before committing, please
double-check your diff for real entity IDs, tokens, names, or locations — use
generic placeholders instead.

## Pull requests

1. Branch off `main`.
2. Keep PRs focused; describe the change and why.
3. Make sure `pytest -q` passes (CI will check).
4. Reference any related issue.

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
