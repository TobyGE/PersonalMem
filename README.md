# PersonalMem

A self-contained "personal memory" pipeline for macOS.

## What it does

1. **Captures** screen activity via macOS Accessibility API (own daemon, own data dir at `~/.personalmem/`)
2. **Coalesces** near-duplicate captures (same app/window within 60s, sub-context aware for chat apps)
3. **Routes** each surviving capture into a topic thread via LLM (router sees the full activity history of each open thread, not just titles or summaries)
4. **Summarizes** each thread once routing is complete — title, narrative, key events, outcome
5. Writes one Markdown file per thread

PersonalMem is fully standalone: it has its own AX-watcher subprocess, its own `index.db`, its own `capture-buffer/`. No external daemon required.

## Setup

```bash
cd ~/PersonalMem
uv venv
source .venv/bin/activate
uv pip install -e .

# Build the AX-watcher Swift binaries (needs Xcode CLT)
bash resources/build-mac-ax-watcher.sh
bash resources/build-mac-ax-helper.sh

# Write default config + start the capture daemon
personalmem init
personalmem start
```

## Daemon control

```bash
personalmem start         # daemon in background
personalmem start -f      # foreground (Ctrl+C to stop)
personalmem stop          # SIGTERM
personalmem status        # daemon state + capture count
```

## Routing replay

```bash
# Replay everything in the index
personalmem run

# A specific window
personalmem run --since 2026-04-26T19:11 --until 2026-04-26T20:09

# Wipe state and re-run
personalmem run --since ... --reset
```

Output goes to `~/.personalmem/threads/thr_*.md` (one file per topic).

## Config

`~/.personalmem/config.toml` controls:
- **`[models.*]`**: LLM provider per stage (router, summarizer). Any provider [litellm](https://github.com/BerriAI/litellm) supports — Ollama (local, free), OpenAI, Anthropic API, Gemini.
- **`[capture]`**: AX capture daemon knobs (heartbeat interval, debounce, dedup windows, screenshot retention)
- **`[coalesce]`**: pre-routing dedup window
- **`[router]`**: top-K open threads shown to router per decision
- **`[source]`**: where to read captures from (defaults to PersonalMem's own paths)
- **`[storage]`**: where outputs land

## Status

Extracted from in-flight experimental work. The default pipeline (`coalesce → route → summarize`) produces clean topic threads on hour-scale workloads. Multi-day scaling and edge-case routing (active-vs-passive activity classification, low-content captures) are still being tuned.

## Acknowledgments

The capture stack — AX tree walking, mac-ax-watcher / mac-ax-helper Swift binaries, S1 markdown rendering, screenshot capture — is ported from [OpenChronicle](https://github.com/Einsia/OpenChronicle).
