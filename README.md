# PersonalMem

Topic-thread routing + summarization on top of OpenChronicle's AX captures.

## What it does

1. Reads raw screen captures from OpenChronicle's daemon (`~/.openchronicle/index.db` + `capture-buffer/`)
2. Coalesces near-duplicate captures (same app/window within 60s)
3. Routes each surviving capture into a topic thread via LLM
4. Summarizes each thread once routing is complete
5. Writes one Markdown file per thread

PersonalMem only **reads** OpenChronicle's data — it doesn't run its own capture daemon.

## Setup

```bash
cd ~/PersonalMem
uv venv
source .venv/bin/activate
uv pip install -e .

# Write default config
personalmem init

# Edit ~/.personalmem/config.toml as needed (model + auth)
```

## Run

```bash
# Replay everything since the daemon started
personalmem run

# Specific window
personalmem run --since 2026-04-26T19:11 --until 2026-04-26T20:09

# Wipe state and re-run
personalmem run --since ... --reset
```

Output goes to `~/.personalmem/threads/thr_*.md` (one file per topic).

## Config

`~/.personalmem/config.toml` controls:
- **`[models.*]`**: which model handles routing vs. summarization. Supports Anthropic OAuth (Haiku/Sonnet via Claude.com subscription, no API key) or any litellm-supported provider (Ollama / OpenAI / etc.)
- **`[coalesce]`**: dedup window
- **`[router]`**: top-K open threads shown per decision
- **`[source]`**: where to read AX captures from
- **`[storage]`**: where PersonalMem writes its own state

## Auth (Anthropic OAuth)

When `auth_type = "anthropic_oauth"`, PersonalMem reads the OAuth token stored by [GuardClaw](https://github.com/Einsia/GuardClaw) at `~/.guardclaw/oauth-tokens.json`. Run GuardClaw and authenticate Claude once; the token auto-refreshes.

## Status

This is extracted from in-flight experimental work. The default v14 pipeline (`coalesce → route → summarize`) produces clean topic threads on hour-scale workloads. Multi-day scaling and edge-case routing (active-vs-passive activity classification, low-content WeChat captures) are still being tuned.
