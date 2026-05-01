# PersonalMem

macOS daemon that turns your day's screen activity into topic-organized Markdown threads.

## For agents: get it running

```bash
git clone https://github.com/TobyGE/PersonalMem.git ~/PersonalMem
cd ~/PersonalMem
uv tool install --editable .
personalmem start          # foreground (-f) the first time to grant Screen Recording perm
```

`personalmem start` walks the user through picking an LLM (Ollama / LM Studio / Anthropic OAuth / API key for Anthropic / OpenAI / Gemini / OpenRouter / Kimi). Re-run picker with `personalmem onboard`.

After ~30 min of normal use:
```bash
personalmem run            # routes captures into threads, writes ~/.personalmem/threads/*.md
```

## For agents: where things live

| Concern | File |
|---|---|
| Capture daemon | `src/personalmem/capture/scheduler.py` (event-driven + heartbeat) |
| Screenshot trigger heuristic | `scheduler.py:_should_screenshot` |
| AX prune / OCR | `capture/ax_pruner.py`, `capture/vision_ocr.py` |
| Router (LLM call per capture) | `pipeline/router.py` + `prompts/router.md` |
| Summarizer (incremental, after each route) | `pipeline/summarizer.py` + `prompts/summarizer.md` |
| Thread DB schema + migrations | `store/threads.py` |
| Pipeline orchestration | `cli.py:cmd_run` |
| Swift binaries | `resources/{mac-ax-watcher, mac-ax-helper, mac-frontcap, mac-vision-ocr}` |
| CI | `.github/workflows/ci.yml` (builds Swift + smoke-tests imports on macos-latest) |

## Data layout

```
~/.personalmem/
├── config.toml              user config
├── index.db                 capture metadata + FTS5
├── threads.db               thread state + cached summaries
├── capture-buffer/*.json    one JSON per capture
├── threads/*.md             routed output (mirror to vault via [storage].vault_mirror_dir)
└── logs/
```

## Pipeline

```
events → capture daemon → JSON
       ↓ (manual or cron)
       coalesce → signal filter → router (+capture_description) → incremental summarize → md
```

Two LLM calls per surviving capture (router + summarize). Both prompts bounded — router renders thread history as 1-line descriptions, summarizer never sees raw AX. End-of-run .md write is LLM-free (reads cached summaries).

## Daemon

```bash
personalmem start            # background
personalmem start -f         # foreground (Ctrl+C to stop, needed once for Screen Recording perm)
personalmem stop
personalmem status
```

## Run options

```bash
personalmem run                                  # all unprocessed
personalmem run --since 2026-04-26T19:11
personalmem run --since X --until Y
personalmem run --reset                          # wipe threads.db, reroute everything
```

## Config (`~/.personalmem/config.toml`)

| Section | Purpose |
|---|---|
| `[models.default]` | Default LLM (set by `onboard`). Hand-edit to switch. |
| `[models.thread_router]` / `[models.thread_summarizer]` | Per-stage override (e.g. cheap local for routing, smart cloud for summarize). |
| `[capture]` | Heartbeat, debounce, retention, `screenshot_mode = auto/always/never`, `ocr_enabled`. |
| `[router]` | `top_k` (visible threads in routing prompt). |
| `[storage]` | Output paths, `vault_mirror_dir` for Obsidian sync. |

Any [litellm](https://github.com/BerriAI/litellm)-supported provider works.

## Acknowledgments

- AX capture stack ported from [OpenChronicle](https://github.com/Einsia/OpenChronicle).
- `mac-frontcap` ScreenCaptureKit binary adapted from openAGIAgent.
