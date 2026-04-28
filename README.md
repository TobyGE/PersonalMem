# PersonalMem

A self-contained "personal memory" pipeline for macOS.

## What it does

1. **Captures** screen activity via macOS Accessibility API
2. **Coalesces** near-duplicate captures
3. **Routes** each capture into a topic thread via LLM
4. **Summarizes** each thread — title, narrative, key events, outcome

One Markdown file per topic. All data lives at `~/.personalmem/`.

## Quick start

```bash
git clone https://github.com/TobyGE/PersonalMem.git ~/PersonalMem
cd ~/PersonalMem
uv tool install --editable .   # one-time install; `personalmem` is on $PATH
personalmem start              # first run: pick provider, then daemon starts
```

On first run you'll be asked to pick an LLM provider:

- **Ollama** — local, free
- **Anthropic API key** — paste an `sk-ant-...`
- **Anthropic OAuth** — Claude.com Pro/Max subscription (browser-based PKCE)

Re-run anytime with `personalmem onboard`.

When you want to turn captures into topic threads:

```bash
personalmem run                # processes everything captured so far
```

Outputs land in `~/.personalmem/threads/thr_*.md`.

## Daemon control

```bash
personalmem start         # background
personalmem start -f      # foreground (Ctrl+C to stop)
personalmem stop
personalmem status        # daemon state + capture count
```

## Routing replay options

```bash
personalmem run                                       # all captures
personalmem run --since 2026-04-26T19:11              # window start
personalmem run --since X --until Y --reset           # rerun a window
```

## Config

Edit `~/.personalmem/config.toml`. Key sections:

- **`[models.default]`** — LLM provider, written by `personalmem onboard`. Hand-edit to switch to OpenAI / Gemini / any other [litellm](https://github.com/BerriAI/litellm)-supported provider.
- **`[capture]`** — AX capture knobs (heartbeat, debounce, retention, screenshot toggle)
- **`[router]`** — top-K open threads shown to router
- **`[storage]`** — where outputs land

## macOS permissions

On first run macOS will prompt for **Accessibility** (so AX can read app UIs). Grant once.

If you flip `include_screenshot = true`, you'll also need **Screen Recording** permission. Run `personalmem start -f` once so the prompt appears (background-launched daemons can't show prompts).

## Rebuilding the Swift binaries (rare)

The `mac-ax-watcher` and `mac-ax-helper` binaries ship pre-compiled in `resources/`. You only need to rebuild if you're modifying the Swift sources:

```bash
bash resources/build-mac-ax-watcher.sh
bash resources/build-mac-ax-helper.sh
```

## Acknowledgments

The capture stack (AX tree walking, mac-ax-watcher / mac-ax-helper Swift binaries, S1 markdown rendering) is ported from [OpenChronicle](https://github.com/Einsia/OpenChronicle).
