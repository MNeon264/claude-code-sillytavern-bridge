# Claude Code ↔ SillyTavern Bridge

## Fork notes

This fork of [MissSinful/claude-code-sillytavern-bridge](https://github.com/MissSinful/claude-code-sillytavern-bridge) adapts the bridge for a headless Linux VPS. Prompts, memory logic and RP behaviour are unchanged. Changes vs upstream:

- **Listens on `127.0.0.1` by default** instead of `0.0.0.0`. Override with `BRIDGE_HOST`; `BRIDGE_PORT` overrides the GUI port setting.
- **Optional auth**: `BRIDGE_API_KEY` requires `Authorization: Bearer <key>` on `/v1/*`, and `BRIDGE_DASHBOARD_PASSWORD` puts HTTP Basic auth on the dashboard and `/api/*`. Both are off unless set.
- **CORS is no longer allow-all.** Only origins listed in `BRIDGE_CORS_ORIGINS` get CORS headers. Allow-all is kept only when bound to `0.0.0.0`, and SillyTavern doesn't need CORS either way.
- **Split requirements**: `requirements.txt` is core only (flask, flask-cors); `requirements-memory.txt` adds sentence-transformers and numpy for Character Memory. `run_bridge.bat` still installs both.
- **Linux run support**: `run_bridge.sh`, a hardened systemd unit in `deploy/`, and an env file template. See [Linux / VPS deployment](#linux--vps-deployment).

With no environment variables set, the only behaviour change is the default listen address.

---

**A Flask-based roleplay bridge that wraps the Claude Code CLI as a SillyTavern-compatible backend.**

Sits between SillyTavern and the Claude Code CLI, translating OpenAI-compatible API requests into `claude -p` subprocess calls, injecting narrative-focused system prompts, and layering a full roleplay feature stack on top — per-character running summaries, auto-lorebook generation, image handling via Claude Code's `Read` tool, editable prompt templates, and a GUI dashboard to configure everything.

Uses your **Claude Code subscription**. No API keys, no per-token billing, no credential impersonation. The actual `claude` CLI does the work; the bridge is just a polite translator with features.

![Bridge GUI](docs/screenshot-gui.png)

---

## Why this exists

SillyTavern is an excellent frontend for creative writing and roleplay, but it speaks OpenAI's API format. Claude Code CLI is how you access Claude's best models on a subscription plan — but it's designed for coding, not long-form fiction. This bridge makes them talk to each other, and adds the things you actually want for long RPs that coding assistants don't care about:

- **Per-character running summaries** so 200-message conversations don't re-send the whole backlog every turn
- **Persistent Character Memory** — structured per-character DB so characters actually remember desires, relationships, and recurring NPCs across sessions
- **Narrative-focused system prompt injection** that overrides Claude Code's built-in "you are a coding assistant" framing
- **Image handling** via Claude Code's native `Read` tool — share reference images in SillyTavern and Claude actually sees them
- **Auto-lorebook** generation that builds World Info entries from your roleplay in the background
- **Live-editable prompts** in the `prompts/` directory — tune summarization and condensation behavior without touching Python

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint (SillyTavern just points at it)
- GUI dashboard at `http://localhost:5001/` with tabs for Settings, System Prompt, Tools, Lorebook, Memory, and Test
- **Model picker** — Opus 4.8, Opus 4.6, or Sonnet, selected in the bridge GUI (SillyTavern's model selector is ignored)
- **Effort levels** — Low / Medium / High / xHigh / Max for the Claude CLI's reasoning budget. xHigh and Max require Opus; Sonnet is clamped to Medium as it produces no meaningful narrative above that level.
- **Character Memory** — structured per-character SQLite + sentence-transformers embeddings + Sonnet librarian. Tracks desires, events, facts, rules, relationships, traits, places, possessions, body state, and secrets across sessions. Sonnet curates relevant memories before each Opus turn (in-band) and updates the DB after (background). Persistent NPCs introduced mid-RP get their own sub-DBs. Inspect/edit everything in the Memory tab. See `MEMORY_DESIGN.md` for the architecture.
- **Per-character auto-summary** — each character's narrative digest lives in its own cache slot, keyed by a hash of the greeting. Switching characters auto-swaps summaries; no manual cache clearing.
- **CLI session reuse** — captures the Claude CLI's session and resumes via `--resume` on follow-up turns, sending only the latest user message instead of the full history. Big input-token savings on long RPs. Auto-invalidates on swipes, edits, or prefix changes.
- **Chunking mode** — one-shot reset that rebuilds a character's summary from an imported chat file
- **Editable prompt templates** at `prompts/*.md` for summarization, condensation, and chunk processing. Hot-reloads on every request — no server restart.
- **Per-character image pipeline** — SillyTavern base64 images get saved to `temp_images/` and injected as file paths so Claude Code's `Read` tool can view them directly
- **Auto-lorebook** generation after each response using Sonnet for efficiency, plus a Deep Analysis mode that scans a full chat file, and a manual entry editor
- **Creativity modes** (Precise / Balanced / Creative / Wild) — prompt-based style control since Claude Code CLI doesn't expose temperature
- **Conditional thinking guidance** — when `include_thinking` is on, the bridge injects a lightweight planning instruction so the model uses `<think>...</think>` without you needing a heavy CoT template in your preset
- **`char_key` pinning** — pin a character's key from the Memory tab so small card edits don't change the auto-derived hash and orphan the existing memory DB or CLI session
- **Test tab** — fire a quick test message at the bridge from the GUI without going through SillyTavern, useful for sanity checks
- **Settings persistence** — model, effort, creativity, thresholds, and port all survive bridge restarts via `bridge_settings.json`
- **Configurable port** from the GUI (default `:5001`)
- **Tool calling fallback** for SillyTavern extensions like TunnelVision
- **Update checker** — checks GitHub releases on startup and warns if a newer version is available
- **Debug logging** with structured output, token usage panels, narrative-failure detection, and `stop_reason` capture

## What it actually produces

The GUI shot above is what the tool looks like. This is what it *does* — a mid-RP scene rendered in SillyTavern, pulled from a **142-message roleplay** with the bridge running Opus at high effort, auto-summary active, and the default system prompt:

![Narrative output in SillyTavern](docs/screenshot-narrative.png)

A few things worth noticing that come directly from the bridge's systems:

- **HTML embeds render inline.** The "Guild of Bounties & Bonds" card mid-scene is a rendered block, not escaped text. The default system prompt's `[Tools]` section tells Claude it can use colored dialogs, CSS blocks, and HTML visual elements — and the bridge passes them through without stripping, so SillyTavern renders them live.
- **Color-coded inline spans.** "Red for high threat. Amber for moderate. Green for low." isn't plain text — those are styled spans the model emitted and the pipeline preserved end-to-end.
- **Memory across 100+ turns.** The troll-ear callback ("you left a troll ear on somebody's paperwork") references an event that happened dozens of turns earlier — long enough to have fallen out of any context window without memory management. It survives because the per-character auto-summary preserves *specific details* (the troll ear, the paperwork, the sardonic framing) instead of flattening events into generic recaps. At 142 messages deep, this is exactly the failure mode the summary system is built to prevent.
- **Character integrity holds.** Physical tics, speech patterns, decision-making — Grimya stays Grimya across the whole scene and across the whole chat. The system prompt's character-integrity rules are doing real work here.
- **No slop.** Specific tactile detail, concrete observations, earned dialogue beats. That's the combination of the creativity-mode prompt + the system prompt's explicit rules against soft hedge-writing.

This is what the bridge is *for*. The features in the list above all exist to make scenes like this one possible and consistent across long RPs.

## Requirements

- **Python 3.10+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated (run `claude` once interactively to confirm it works)
- **Claude subscription** (Pro, Max, or equivalent) with Claude Code access
- **SillyTavern** installation (for the actual UI; the bridge is backend-only)

## Install

```bash
git clone https://github.com/MissSinful/claude-code-sillytavern-bridge.git
cd claude-code-sillytavern-bridge
pip install -r requirements.txt
pip install -r requirements-memory.txt   # optional, for Character Memory
```

`requirements.txt` is all the bridge needs to run. `requirements-memory.txt` adds `sentence-transformers` and `numpy` for Character Memory's semantic retrieval (`run_bridge.bat` installs both). Without them, the bridge runs normally and Character Memory logs "sentence-transformers not installed" and skips semantic search. The first time the model is needed (when you enable Character Memory in the GUI), it downloads `all-MiniLM-L6-v2` (~80MB) into `~/.cache/huggingface`. If you don't enable Character Memory, the model never loads.

Start the bridge:

**Windows:**
```cmd
run_bridge.bat
```

**macOS / Linux:**
```bash
./run_bridge.sh        # uses ./.venv if present; or: python3 claude_bridge.py
```

The bridge starts on `http://localhost:5001`, listening on `127.0.0.1` only. Open it in a browser to see the dashboard. To reach it from other machines, set `BRIDGE_HOST=0.0.0.0` and a `BRIDGE_API_KEY` (see [Linux / VPS deployment](#linux--vps-deployment)).

## SillyTavern setup

1. In SillyTavern, open **API Connections**
2. Select **Chat Completion** → **OpenAI Compatible** (or "Custom OpenAI" depending on your ST version)
3. Set the endpoint to `http://localhost:5001/v1`
4. Enter any API key — the bridge doesn't check it unless `BRIDGE_API_KEY` is set (then enter that value), but SillyTavern requires the field to be non-empty. `sk-placeholder` works.
5. Model selector in SillyTavern is ignored — pick your model in the Settings tab of the bridge GUI instead
6. Save and connect

Send a test message. If it works, you're set. If not, check the bridge terminal for logs — debug output is on by default.

## Linux / VPS deployment

This assumes SillyTavern and the bridge run on the same server (as user `st` here), with SillyTavern pointed at `http://127.0.0.1:5001/v1`. SillyTavern calls the bridge from its Node server, so the bridge only needs to listen on loopback.

### 1. Install

As the user that will run the bridge, install [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (the native installer puts `claude` in `~/.local/bin`). Run `claude` once interactively to log in, then check `claude --version` works.

```bash
sudo apt install python3-venv git                 # Debian / Ubuntu
cd ~
git clone <this repo> claude-code-sillytavern-bridge
cd claude-code-sillytavern-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt         # core: enough to run the bridge
```

Optional, for Character Memory: install CPU-only PyTorch **first**, so pip doesn't pull in ~2GB of CUDA wheels a VPS can't use. Then install the memory extras:

```bash
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements-memory.txt
mkdir -p ~/.cache/huggingface                     # model download location (must exist for the systemd sandbox)
```

Without them, the bridge runs normally and Character Memory logs `sentence-transformers not installed` and skips semantic search.

### 2. Configure

All settings are optional environment variables:

| Variable | Default | Effect |
|---|---|---|
| `BRIDGE_HOST` | `127.0.0.1` | Address to listen on. `0.0.0.0` exposes it on every interface. |
| `BRIDGE_PORT` | GUI setting (5001) | Port to listen on. Overrides the port saved in the Settings tab. |
| `BRIDGE_API_KEY` | unset | Every `/v1/*` route requires `Authorization: Bearer <key>`. Put the same value in SillyTavern's API key field. |
| `BRIDGE_DASHBOARD_PASSWORD` | unset | The dashboard and `/api/*` require HTTP Basic auth: any username, this password. |
| `BRIDGE_CORS_ORIGINS` | unset | Comma-separated browser origins (or `*`) allowed to call the bridge cross-origin. Not needed for SillyTavern. When unset, there are no CORS headers, except with `BRIDGE_HOST=0.0.0.0`, where any origin is allowed (upstream behaviour). |

Behind Tailscale with SillyTavern on the same box, loopback alone keeps the bridge private. Setting `BRIDGE_API_KEY` and `BRIDGE_DASHBOARD_PASSWORD` too is cheap defence in depth against other local processes. With both set, the GUI's Test tab still works: requests to `/v1` also accept the dashboard login.

Generate secrets with `openssl rand -hex 32`. To try it in the foreground:

```bash
BRIDGE_API_KEY=... BRIDGE_DASHBOARD_PASSWORD=... ./run_bridge.sh
```

### 3. Run as a systemd service

`deploy/claude-bridge.service` runs the bridge as user `st` from `/home/st/claude-code-sillytavern-bridge` using the venv's Python. Edit `User=`, `Group=` and the paths if yours differ.

```bash
sudo cp deploy/claude-bridge.service /etc/systemd/system/
sudo install -m 600 deploy/claude-bridge.env.example /etc/claude-bridge.env
sudoedit /etc/claude-bridge.env                   # uncomment and set BRIDGE_API_KEY etc.
sudo systemctl daemon-reload
sudo systemctl enable --now claude-bridge
journalctl -u claude-bridge -f                    # logs
```

How the unit is set up:

- **PATH** includes `/home/st/.local/bin`, so `claude` is found. The startup banner shows which `claude` binary the bridge resolved.
- **Secrets** go in `/etc/claude-bridge.env` (root-only, read by systemd before it drops privileges). Values there override the unit's `Environment=` lines.
- **Sandbox**: `ProtectSystem=strict` makes the filesystem read-only except for the bridge directory, the `claude` CLI's state (`~/.claude`, `~/.cache/claude`, `~/.cache/claude-cli-nodejs`, `~/.local/state/claude`), `~/.cache/huggingface`, and `~/SillyTavern/data` (auto-lorebook writes its World Info file into SillyTavern's worlds folder). If your Lorebook path lives elsewhere, add a `ReadWritePaths=` line for it.
- **`~/.claude.json` stays read-only.** `claude` rewrites it through a lock directory and temp files created directly in your home directory. Allowing that would mean making all of `/home/st` writable. `claude` treats the failure as non-fatal; the unit's comments show the one-line opt-out if that ever changes.
- **Auto-update is disabled** inside the service, because the `claude` binary sits on a read-only path there. Update with `claude update` from a normal shell, then `sudo systemctl restart claude-bridge`.

### 4. Reach the dashboard

The dashboard listens on the server's loopback only. From your own machine, either:

- **SSH tunnel:** `ssh -L 5001:127.0.0.1:5001 user@host`, then open `http://localhost:5001`.
- **Tailscale Serve:** on the server, run `sudo tailscale serve --bg 5001`. The dashboard is then at `https://<machine-name>.<tailnet>.ts.net/`, visible only to your tailnet. Turn it off with `sudo tailscale serve reset`. Serve exposes the whole bridge to your tailnet, `/v1` included, so set both `BRIDGE_DASHBOARD_PASSWORD` and `BRIDGE_API_KEY` if anyone else is on it.

Keep the SillyTavern connection itself on `http://127.0.0.1:5001/v1`.

## Using it

Most features work automatically once the bridge is running and configured. Highlights:

**Enable auto-summary** early in a new chat. Tools tab → toggle Auto-Summary on. The default threshold updates the summary every 20 new messages, which is a reasonable balance for Opus-effort-high usage caps. Without it, SillyTavern re-sends your entire message history on every turn, which eats through usage limits fast on long RPs.

**Enable Character Memory** in the Settings tab if you want characters to actually remember things across sessions — desires, relationships, learned rules about you, recurring NPCs. The first turn for a new character runs a one-time Sonnet bootstrap (~5s) that seeds the DB from the card. After that, every turn is enriched with a curated memory block in the prompt, and a background Sonnet pass updates the DB after the response. Inspect everything in the Memory tab. If you've got old `state.md` / `diary.md` / `rules.md` files from earlier versions of the bridge, they auto-migrate to the new DB on first use.

**Editing prompts.** The bridge's internal prompts live as markdown files in `prompts/`:

| File | What it does |
|---|---|
| `summarize_incremental.md` | Appended each time auto-summary updates |
| `condense.md` | Condenses a summary when it grows past the max-length threshold |
| `summarize_chunk.md` | Per-chunk summary for both live chunking and file-based chunking |
| `condense_chronological.md` | Final chronological condensation for file-based summary generation |

Edit any of these, save, and the next request picks up the change. No server restart. Placeholders use Python `{variable}` syntax — escape a literal brace as `{{` / `}}`.

**The main RP system prompt** is *not* in the `prompts/` folder — it's the `DEFAULT_BRIDGE_SYSTEM_PROMPT` constant in `claude_bridge.py`. Edit it via the System Prompt tab in the GUI, which persists to `bridge_settings.json`.

## Known limitations

These are **architectural**, not bugs — they're properties of running Claude Code CLI as a subprocess per request, and there's no clean fix inside the current CLI version.

- **No streaming.** Claude Code CLI doesn't emit incremental `content_block_delta` events for subprocess callers — it ships the full response in one `assistant` event at the end. The bridge waits for the full response and returns it in one payload. When SillyTavern requests `stream: true`, it gets a valid SSE stream containing one content chunk + stop + `[DONE]`. Earlier versions tried to pace the response through SSE to look streamed; that silently dropped content on Opus 4.7 and was removed.
- **No temperature / sampling parameters.** Claude Code CLI doesn't expose `temperature`, `top_p`, or `top_k`. The Creativity setting (Precise / Balanced / Creative / Wild) is a prompt-based style modifier — Claude follows the style instructions, but it's not the same as a real temperature slider.
- **Per-request subprocess overhead.** Every RP turn spawns a fresh `claude -p` process, which adds startup latency compared to a persistent HTTP client. Not a problem for the long thinking times typical of high-effort requests, but noticeable for small ones. CLI session reuse mitigates this on follow-up turns.
- **No prompt caching control.** Claude Code manages its own caching internally; the bridge can't directly set cache breakpoints. Auto-summary and CLI session reuse both mitigate this by keeping the stable prefix large and the variable suffix small.

If any of these become dealbreakers for you, the right move is an alternative backend mode that hits the Anthropic SDK directly — the groundwork is there, but it's not currently implemented.

## Project structure

```
claude-code-sillytavern-bridge/
├── claude_bridge.py           # Main Flask server and subprocess wrapper
├── memory_v2.py               # Character Memory: SQLite + embeddings + Sonnet librarian
├── modify_preset.py           # Standalone utility for SillyTavern preset tweaks
├── requirements.txt           # Core Python dependencies
├── requirements-memory.txt    # Optional: Character Memory embeddings
├── run_bridge.bat             # Windows launcher
├── run_bridge.sh              # Linux / macOS launcher (uses ./.venv if present)
├── deploy/
│   ├── claude-bridge.service  # Hardened systemd unit
│   └── claude-bridge.env.example  # Env vars for the unit (copy to /etc/claude-bridge.env)
├── templates/
│   └── index.html             # GUI dashboard (single-page, vanilla JS)
├── prompts/                   # Editable prompt templates (hot-reloaded)
│   ├── summarize_incremental.md
│   ├── condense.md
│   ├── summarize_chunk.md
│   └── condense_chronological.md
├── cache/                     # Per-character summary cache (gitignored)
├── character_memory/          # Per-character memory DBs + needs + NPCs (gitignored)
├── temp_images/               # SillyTavern base64 image dumps (gitignored)
├── bridge_settings.json       # Persisted runtime settings (gitignored)
├── bridge_sessions.json       # Captured CLI session IDs for --resume (gitignored)
└── CLAUDE.md                  # Project notes for Claude Code itself
```

## Content note

The default system prompt framing is for **adult collaborative fiction**. It includes explicit instructions for how to handle intimate scenes authentically rather than theatrically, alongside instructions for character integrity, narrative risk-taking, and structured thinking. This is intentional — the bridge is built for adult RP and storytelling, and the prompt is what makes Claude not default to sanitized boilerplate when the story calls for more.

If that doesn't match your use case, replace `DEFAULT_BRIDGE_SYSTEM_PROMPT` in `claude_bridge.py` with your own framing, or override it via the System Prompt tab in the GUI.

## Policy & responsibility

This tool doesn't modify or bypass Anthropic's safety systems — it pipes your prompts to the official `claude` CLI using your own subscription via its intended auth flow. Anything Claude would refuse in `claude.ai` it'll still refuse here; the default system prompt steers tone and framing but can't (and doesn't try to) override model-level safety training. Content you generate through this bridge is subject to [Anthropic's Acceptable Use Policy](https://www.anthropic.com/aup), and responsibility for what you prompt and what the model produces stays with you as the subscriber.

## Maintenance

**Personal project shared for anyone who finds it useful.** I built this for my own SillyTavern + Claude Code workflow and polished it enough to be worth putting up. The architecture is stable, the features work, and the known limitations are documented above. PRs welcome; issues may or may not get responses depending on how relevant they are to my own usage.

If you want to take any of the architectural gaps (real streaming via the Anthropic SDK, proper temperature control, prompt caching) and turn them into a PR, I'll review it. But don't expect a responsive upstream. This is closer to "published for reference" than "actively maintained."

## License

MIT. See `LICENSE`.
