# PersonalMem

A self-contained "personal memory" pipeline for macOS that turns your day's screen activity into topic-organized Markdown threads.

## What you get

1. A background daemon **captures** what's on your screen via the macOS Accessibility API (text-based, not pixels — privacy-friendly by default). Auto screenshots only when AX is sparse (videos / canvas apps), with on-device Apple Vision OCR.
2. A second pass (manual or scheduled) **routes** captures into topic threads via an LLM and **summarizes** each one — short title, running narrative, key events, outcome.
3. Output is one Markdown file per topic at `~/.personalmem/threads/thr_*.md`. Optionally mirrored into an Obsidian vault.

All processing is local-first: capture daemon never calls the network, routing/summarization happens via whichever LLM you configure (local Ollama / LM Studio, or any cloud API).

## Quick start (for humans and agents)

```bash
git clone https://github.com/TobyGE/PersonalMem.git ~/PersonalMem
cd ~/PersonalMem
uv tool install --editable .    # one-time install; `personalmem` is on $PATH
personalmem start               # interactive onboarding on first run
```

The onboarding flow asks you to pick an LLM provider:

| Choice | Best for |
|---|---|
| **Ollama** | local, free; probes `localhost:11434` and lists installed models |
| **Anthropic OAuth** | Claude.com Pro/Max subscribers — uses subscription, no API key needed |
| **API key** | Anthropic / OpenAI / Google Gemini / OpenRouter / Kimi |

Re-run picker anytime with `personalmem onboard`.

After 30+ minutes of normal computer use:

```bash
personalmem run                 # turn captures into topic threads
ls ~/.personalmem/threads/      # see the .md output
```

## Architecture (one-screen overview)

```
                ┌─────────────────────┐
   AX events    │  capture daemon     │  ~/.personalmem/capture-buffer/
   ────────────►│   personalmem start │  one JSON per capture
                └─────────────────────┘
                          │
                          ▼ (manual or cron)
                ┌─────────────────────┐
                │  personalmem run    │
                │   ┌───────────────┐ │
                │   │ coalesce      │ │  fold redundant frames
                │   │ signal filter │ │  drop pure-noise captures
                │   │ router (LLM)  │ │  decide: continue / new
                │   │ +description  │ │  cache 1-line activity log
                │   │ summarizer    │ │  per-thread title + narrative
                │   │ (LLM)         │ │  cached after each routing call
                │   └───────────────┘ │
                └─────────────────────┘
                          │
                          ▼
                ~/.personalmem/threads/*.md
                (optionally mirrored to Obsidian vault)
```

Two LLM calls per surviving capture (router + incremental summarize). Both prompts are bounded — router sees 1-line descriptions of past activity, summarizer never re-renders raw AX. Cost on Haiku 4.5: ~$0.005-0.01 per 100 captures.

## Daemon control

```bash
personalmem start          # background (double-fork detached)
personalmem start -f       # foreground (Ctrl+C to stop)
personalmem stop
personalmem status         # daemon state + capture count
```

## Run options

```bash
personalmem run                                       # all unprocessed captures
personalmem run --since 2026-04-26T19:11              # window start
personalmem run --since X --until Y                   # bounded window
personalmem run --reset                               # wipe threads.db, reroute everything
```

## Config

`~/.personalmem/config.toml` (written on first run):

| Section | What it controls |
|---|---|
| `[models.default]` | Which LLM the pipeline uses. Set by `personalmem onboard`; hand-edit to switch providers. |
| `[models.thread_router]` / `[models.thread_summarizer]` | Override default per-stage. Useful if you want cheap local model for routing + smarter cloud model for summarize, or vice versa. |
| `[capture]` | Heartbeat interval, debounce, retention, screenshot mode (`auto` / `always` / `never`), OCR toggle. |
| `[router]` | `top_k` (visible threads in routing prompt) |
| `[storage]` | Output paths. Set `vault_mirror_dir` to e.g. `~/Documents/Obsidian Vault/personalmem` to auto-copy `.md` after each run. |

Any [litellm](https://github.com/BerriAI/litellm)-supported provider works (Ollama, OpenAI-compatible endpoints, Anthropic, Gemini, OpenRouter, Kimi/Moonshot, …).

## macOS permissions

First run prompts for two permissions:

1. **Accessibility** — required (so AX can read app UIs). Grant in System Settings → Privacy & Security → Accessibility.
2. **Screen Recording** — only needed if `screenshot_mode != "never"`. Background daemons can't show the macOS permission prompt, so run `personalmem start -f` once in the foreground to trigger it, grant access, then `Ctrl+C` and use `personalmem start` normally.

## Bundled Swift binaries

These ship pre-compiled in `resources/`:

| Binary | Purpose |
|---|---|
| `mac-ax-watcher` | Streams AX events (mouse click, focus change, value change) to the daemon |
| `mac-ax-helper` | Snapshots the AX tree of the frontmost app |
| `mac-frontcap` | Active-window screenshot via ScreenCaptureKit |
| `mac-vision-ocr` | Apple Vision OCR on a JPEG |

Rebuild if you modify the Swift sources:

```bash
bash resources/build-mac-ax-watcher.sh
bash resources/build-mac-ax-helper.sh
bash resources/build-mac-frontcap.sh
bash resources/build-mac-vision-ocr.sh
```

CI builds them all on `macos-latest` (`.github/workflows/ci.yml`) so PRs auto-verify the source compiles.

## Data layout

```
~/.personalmem/
├── config.toml              user-edited config
├── index.db                 SQLite — capture metadata + FTS5 index
├── threads.db               SQLite — thread state (incremental summary cached here)
├── capture-buffer/          one JSON per capture (AX tree + screenshot + OCR)
├── threads/                 .md output, one per thread + _decisions.json + _report.json
└── logs/                    daemon log files
```

`config.toml`'s `vault_mirror_dir` mirrors the `threads/` folder into an Obsidian vault on every `personalmem run`.

## Hacking notes (for agents)

- **Capture daemon** lives in `src/personalmem/capture/`. It's event-driven (`mac-ax-watcher` stdout → `event_dispatcher.py`) plus a 10-min heartbeat. `screenshot.py` calls `mac-frontcap`; `vision_ocr.py` calls `mac-vision-ocr`. Edit `scheduler.py:_should_screenshot` for the screenshot trigger heuristic.
- **Routing** lives in `src/personalmem/pipeline/router.py`. The LLM also emits a per-capture description on each call; it's cached on `thread_captures.description` so future router calls render history as one-liners.
- **Summarize** in `src/personalmem/pipeline/summarizer.py` runs incrementally — once after each routing decision, on just the affected thread. Output is cached on the thread row (title / narrative / key_events_json / outcome). End-of-run `write_thread_mds` reads cache → writes `.md` (zero LLM calls at that step).
- **Prompts** are in `src/personalmem/prompts/router.md` and `src/personalmem/prompts/summarizer.md`.
- **Schema migrations** are additive — `store/threads.py:ensure_schema()` handles in-place column adds for older dbs.
- The pipeline is fully replayable: `personalmem run --since X --until Y --reset` rebuilds threads from the capture buffer at any time. Useful for prompt iteration.

## Acknowledgments

- AX capture stack (`mac-ax-watcher`, `mac-ax-helper`, S1 markdown rendering) is ported from [OpenChronicle](https://github.com/Einsia/OpenChronicle).
- `mac-frontcap` ScreenCaptureKit binary is adapted from openAGIAgent (FrontCap.swift).
