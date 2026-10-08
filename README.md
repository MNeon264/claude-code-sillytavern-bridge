# Claude Code ↔ SillyTavern Bridge

## Fork notes

This fork of [MissSinful/claude-code-sillytavern-bridge](https://github.com/MissSinful/claude-code-sillytavern-bridge) adapts the bridge for a headless Linux VPS and changes how prompts reach Claude (SillyTavern's prompt is the system prompt; the bridge's own roleplay prompt is optional). Changes vs upstream:

- **Listens on `127.0.0.1` by default** instead of `0.0.0.0`. Override with `BRIDGE_HOST`; `BRIDGE_PORT` overrides the GUI port setting.
- **Optional auth**: `BRIDGE_API_KEY` requires `Authorization: Bearer <key>` on `/v1/*`, and `BRIDGE_DASHBOARD_PASSWORD` puts HTTP Basic auth on the dashboard and `/api/*`. Both are off unless set.
- **CORS is no longer allow-all.** Only origins listed in `BRIDGE_CORS_ORIGINS` get CORS headers. Allow-all is kept only when bound to `0.0.0.0`, and SillyTavern doesn't need CORS either way.
- **Split requirements**: `requirements.txt` is core only (flask, flask-cors); `requirements-memory.txt` adds sentence-transformers and numpy for Character Memory. `run_bridge.bat` still installs both.
- **Linux run support**: `run_bridge.sh`, a hardened systemd unit in `deploy/`, and an env file template. See [Linux / VPS deployment](#linux--vps-deployment).
- **`claude` calls load user settings only** (`--setting-sources user`), so a `CLAUDE.md` in the bridge directory or a parent directory is never sent to the model. Background Sonnet calls (memory, lorebook) also run with no tools (`--tools ""`) instead of Claude Code's full toolset.
- **SillyTavern's prompt is the system prompt.** SillyTavern's system messages before the chat (preset, card, persona, lore) go into Claude's real system prompt, after a short bridge frame that explains the message layout. Post-History Instructions stay after the chat and in-chat notes stay in place, instead of everything being moved to the top of the first message. The upstream roleplay prompt is no longer sent by default; the Prompts tab can load it as optional style notes.
- **CLI sessions keep their system prompt.** A resumed session re-sends the exact system prompt it started with, so Claude's earlier thinking stays valid (rebuilding it mid-session is a history edit, rejected for newer accounts on current Opus models) and the prompt cache stays warm. Lore or notes SillyTavern adds or changes mid-session are sent with that turn in a `<sillytavern_update>` block, and changed Post-History Instructions are re-sent. A change to the bridge's own part (style notes, Creativity, tools) starts a new session.
- **Thinking goes to SillyTavern's reasoning block.** No prompt asks Claude to write `<think>` reasoning into the reply. With Include Thinking on, the bridge requests the CLI's summarized thinking (`--thinking-display summarized`) and returns it as `reasoning_content`, which SillyTavern shows when "Request model reasoning" is on. Effort controls how much Claude thinks.
- **Character Memory keeps its must-keep rows itself.** Among a turn's candidate memories, desires with intensity 4 or higher, secrets, and relationships with characters in the scene are injected first, without going through Sonnet's ranking; upstream asked Sonnet to keep them. Sonnet ranks only the other candidates for the remaining slots, and its answer is checked against a JSON schema (`--json-schema`).
- **Restyled dashboard with a prompt preview.** The dashboard uses a plain sans-serif layout instead of the serif/italic editorial styling. The Prompts tab previews what a roleplay turn sends to the CLI, for a new session and for a resumed turn: the bridge's own text placed around SillyTavern's prompt order, which `GET /api/prompt_preview` reads from SillyTavern's `settings.json` (found next to the bridge's folder, or via the Lorebook tab's worlds path). Card, lore and chat-history entries show as placeholders.

With no environment variables set, the only network or auth change is the default listen address.

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
- **SillyTavern's prompt as the system prompt**, replacing Claude Code's built-in "you are a coding assistant" framing
- **Image handling** via Claude Code's native `Read` tool — share reference images in SillyTavern and Claude actually sees them
- **Auto-lorebook** generation that builds World Info entries from your roleplay in the background
- **Live-editable prompts** in the `prompts/` directory — tune summarization and condensation behavior without touching Python

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint (SillyTavern just points at it)
- GUI dashboard at `http://localhost:5001/` with tabs for Settings, System Prompt, Tools, Lorebook, Memory, and Test
- **Model picker** — every current Claude model (Fable, Opus, Sonnet and Haiku, pinned versions or "latest" aliases), selected in the bridge GUI; or set it to "Use SillyTavern's model" to pick per connection in SillyTavern
- **Effort levels** — Low / Medium / High / xHigh / Max for the Claude CLI's reasoning budget. xHigh and Max require Opus; Sonnet 4.6 and 4.5 are clamped to Medium as they produce no meaningful narrative above that level.
- **Character Memory** — structured per-character SQLite + sentence-transformers embeddings + Sonnet librarian. Tracks desires, events, facts, rules, relationships, traits, places, possessions, body state, and secrets across sessions. Sonnet curates relevant memories before each Opus turn (in-band) and updates the DB after (background). Persistent NPCs introduced mid-RP get their own sub-DBs. Inspect/edit everything in the Memory tab. See `MEMORY_DESIGN.md` for the architecture.
- **Per-character auto-summary** — each character's narrative digest lives in its own cache slot, keyed by a hash of the greeting. Switching characters auto-swaps summaries; no manual cache clearing.
- **CLI session reuse** — captures the Claude CLI's session and resumes via `--resume` on follow-up turns, sending only the new turn (plus any lore SillyTavern added or changed) with the session's original system prompt. Big input-token savings on long RPs. Starts a new session on swipes, edits, or changes to the bridge's own prompt settings.
- **Chunking mode** — one-shot reset that rebuilds a character's summary from an imported chat file
- **Editable prompt templates** at `prompts/*.md` for summarization, condensation, and chunk processing. Hot-reloads on every request — no server restart.
- **Per-character image pipeline** — SillyTavern base64 images get saved to `temp_images/` and injected as file paths so Claude Code's `Read` tool can view them directly
- **Auto-lorebook** generation after each response using Sonnet for efficiency, plus a Deep Analysis mode that scans a full chat file, and a manual entry editor
- **Creativity modes** (Precise / Balanced / Creative / Wild) — prompt-based style control since Claude Code CLI doesn't expose temperature
- **Thinking summaries** — when `include_thinking` is on, Claude's summarized thinking is returned as `reasoning_content` for SillyTavern's reasoning block; nothing asks the model to write its reasoning into the reply
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
5. Pick your model in the Settings tab of the bridge GUI. SillyTavern's model selector is ignored unless the bridge's Model is set to "Use SillyTavern's model"
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
- **Sandbox**: `ProtectSystem=strict` makes the filesystem read-only except for `/home/st`. The whole home directory is writable because `claude` rewrites `~/.claude.json` through a lock directory and temp files created directly in it; with only the CLI's state directories writable, every call logs "continuing without persisting". That also covers the bridge directory, `~/.cache/huggingface` and `~/SillyTavern/data` (auto-lorebook writes its World Info file into SillyTavern's worlds folder). If your Lorebook path lives outside your home directory, add a `ReadWritePaths=` line for it.
- **claude.ai connectors are off** for the bridge's `claude -p` calls (`ENABLE_CLAUDEAI_MCP_SERVERS=false`), so the roleplay model can't reach Gmail, Drive, Calendar or other connectors on your account. Interactive `claude` sessions are unaffected.
- **Auto-update is disabled** inside the service, so a background update never swaps the `claude` binary under a running bridge. Update with `claude update` from a normal shell, then `sudo systemctl restart claude-bridge`.

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

**The roleplay system prompt** is your SillyTavern prompt, behind a short frame (`BRIDGE_FRAME` in `claude_bridge.py`) that explains how SillyTavern's messages are laid out. Optional style notes from the Prompts tab (persisted to `bridge_settings.json`) are added before SillyTavern's prompt; "Load upstream prompt" fills in the upstream project's roleplay prompt (`UPSTREAM_STYLE_PROMPT`) as a starting point.

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

In this fork, how Claude writes comes from your SillyTavern prompt. The upstream project's roleplay prompt (`UPSTREAM_STYLE_PROMPT` in `claude_bridge.py`) is framed for **adult collaborative fiction**, with explicit instructions for intimate scenes, character integrity, and narrative risk-taking. It is only sent if you load it into the Prompts tab's style notes and save.

## Policy & responsibility

This tool doesn't modify or bypass Anthropic's safety systems — it pipes your prompts to the official `claude` CLI using your own subscription via its intended auth flow. Anything Claude would refuse in `claude.ai` it'll still refuse here; the default system prompt steers tone and framing but can't (and doesn't try to) override model-level safety training. Content you generate through this bridge is subject to [Anthropic's Acceptable Use Policy](https://www.anthropic.com/aup), and responsibility for what you prompt and what the model produces stays with you as the subscriber.

## Maintenance

**Personal project shared for anyone who finds it useful.** I built this for my own SillyTavern + Claude Code workflow and polished it enough to be worth putting up. The architecture is stable, the features work, and the known limitations are documented above. PRs welcome; issues may or may not get responses depending on how relevant they are to my own usage.

If you want to take any of the architectural gaps (real streaming via the Anthropic SDK, proper temperature control, prompt caching) and turn them into a PR, I'll review it. But don't expect a responsive upstream. This is closer to "published for reference" than "actively maintained."

## License

MIT. See `LICENSE`.
