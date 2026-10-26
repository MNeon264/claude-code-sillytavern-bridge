# Claude Code ↔ SillyTavern Bridge

## Fork notes

This fork of [MissSinful/claude-code-sillytavern-bridge](https://github.com/MissSinful/claude-code-sillytavern-bridge) adapts the bridge for a headless Linux VPS and changes how prompts reach Claude (SillyTavern's prompt is the system prompt; the bridge's own roleplay prompt is optional). Changes vs upstream:

- **Listens on `127.0.0.1` by default** instead of `0.0.0.0`. Override with `BRIDGE_HOST`; `BRIDGE_PORT` overrides the GUI port setting.
- **Optional auth**: `BRIDGE_API_KEY` requires `Authorization: Bearer <key>` on `/v1/*`, and `BRIDGE_DASHBOARD_PASSWORD` puts HTTP Basic auth on the dashboard and `/api/*`. Both are off unless set.
- **CORS is no longer allow-all.** Only origins listed in `BRIDGE_CORS_ORIGINS` get CORS headers. Allow-all is kept only when bound to `0.0.0.0`, and SillyTavern doesn't need CORS either way.
- **Core requirements only**: `requirements.txt` (flask, flask-cors) is all the bridge needs. Upstream's optional `requirements-memory.txt` (sentence-transformers, numpy) is gone with the memory that used it.
- **Linux run support**: `run_bridge.sh`, a hardened systemd unit in `deploy/`, and an env file template. See [Linux / VPS deployment](#linux--vps-deployment).
- **`claude` calls load user settings only** (`--setting-sources user`), so a `CLAUDE.md` in the bridge directory or a parent directory is never sent to the model. Background Sonnet calls (memory, lorebook) also run with no tools (`--tools ""`) instead of Claude Code's full toolset.
- **SillyTavern's prompt is the system prompt.** SillyTavern's system messages before the chat (preset, card, persona, lore) go into Claude's real system prompt, after a short bridge frame that explains the message layout. Post-History Instructions stay after the chat and in-chat notes stay in place, instead of everything being moved to the top of the first message. The upstream roleplay prompt is no longer sent by default; the Prompts tab can load it as optional style notes.
- **CLI sessions keep their system prompt.** A resumed session re-sends the exact system prompt it started with, so Claude's earlier thinking stays valid (rebuilding it mid-session is a history edit, rejected for newer accounts on current Opus models) and the prompt cache stays warm. Lore or notes SillyTavern adds or changes mid-session are sent with that turn in a `<sillytavern_update>` block, and changed Post-History Instructions are re-sent. A change to the bridge's own part (style notes, Creativity, tools) starts a new session.
- **Group chats get a CLI session per character.** In a SillyTavern group chat each character's turn has its own prompt (its card, and a main prompt that names it). Upstream kept one session per chat with the first speaker's system prompt and sent other characters' cards as updates, so a turn could carry two characters' instructions, and switching back to a character sent nothing at all. Now each character has its own session, used only while its SillyTavern prompt is still sent unchanged, and a resumed turn includes the other characters' replies since that character last spoke. Speaker names sent in the `name` field (Character Names Behavior "Message Names") are shown in the transcript.
- **CLI sessions follow edits anywhere in the chat.** Sessions are found by the bridge's own last reply rather than by a character fingerprint. A turn resumes the session whose last reply is still in the chat and whose earlier messages are still exactly what it saw, in the same order, and sends everything after that reply. Editing, deleting, hiding or moving any message the session saw, however old, or swiping its reply, starts a new session; upstream compared only the latest messages and missed edits and deletions of the last reply. A branch resumes a session whose reply it contains and otherwise starts its own, swiping back to an earlier reply resumes that reply's session, and long chats whose greeting has scrolled out of SillyTavern's context still resume. Two changes still look like the normal flow: deleting the oldest messages still in context looks like SillyTavern trimming the context, and a preset message SillyTavern re-inserts at a fixed depth stays where the session first saw it. A session the CLI can no longer resume, or whose turn was cancelled, is dropped instead of failing every turn.
- **Offline session tests.** `tests/session_scenarios.py` replays solo and group chats (swipes, edits, deletions, moves, branches, added members, cancelled turns) against a scratch bridge that runs the real `claude` CLI against a fake Messages API, and checks on every turn what the model received. It uses no quota.
- **Thinking goes to SillyTavern's reasoning block.** No prompt asks Claude to write `<think>` reasoning into the reply. With Include Thinking on, the bridge requests the CLI's summarized thinking (`--thinking-display summarized`) and returns it as `reasoning_content`, which SillyTavern shows when "Request model reasoning" is on. Effort controls how much Claude thinks.
- **Replies stream.** When SillyTavern requests a stream, the roleplay turn runs with `--include-partial-messages` and the bridge forwards each token delta as the CLI writes it: the thinking summary as `reasoning_content` chunks, then the reply as `content` chunks. Upstream sent the whole reply as one chunk once Claude had finished.
- **Character Memory recaps chats, and brings them only where you ask.** It replaces upstream's per-character database of desires, needs, relationships, body state, secrets and NPCs, which a Sonnet bootstrap seeded from the card, a ranking call filtered before each reply and a maintenance call updated after each kept reply. That database belonged to the character as a whole, so a scene's body state, a desire marked "inevitable" or the relationship with "the user" carried into every other chat and every persona, and its card entries repeated what SillyTavern already sends. Now a memory belongs to the chat it happened in. A character remembers another chat only through a **connection** (any chat in SillyTavern remembered in another chat, by everyone in it or only the group members picked) or a **persona binding** (in every chat with that persona, the chats you tick for it, and each new chat with it from the day you bind). A chat can be kept out of bindings everywhere, or left out of one chat. Chats of the same lineage (a chat and its branches) never come in through a binding, as they're alternate timelines. Personas are told apart by avatar, not name. Memory says what happened; it doesn't track needs or tell the character how to feel.
- **Recaps come from SillyTavern's chat files.** The bridge reads chats from SillyTavern's data folder and Sonnet recaps them 40 messages per part, with the previous part's recap for context, leaving hidden and empty messages out. Each message is headed by its number, and a recap is written one paragraph per scene, each opening with the number of the message its scene starts at (`[#52]`); the model reads recaps without those marks. A part the character has a message in is recapped from their point of view; any other part as a plain account that names no point of view, shared by every character who isn't in it, so nobody is written into a scene they weren't in. A chat a character isn't in at all reaches them as background, marked as such. So chats from before memory was switched on can be remembered too. Parts are cached by their text: only an edited part is recapped again, and a branch shares its parent's recaps up to the branch point. Recaps are built in the background when a chat that needs them is used or opened, or when you ask on the Memory tab. Nothing runs per reply. When SillyTavern drops a chat's oldest messages to fit the context, the recap of those messages comes in instead. It is made of whole parts, the same ones the chat's own recap and its branches use, so each part is recapped once rather than again whenever the context's start moves; the last part overlaps the start of the transcript.
- **The Bridge Memory extension.** `st-extension/` is a small SillyTavern extension (`st-extension/install.sh` copies it into SillyTavern's user data folder). It adds a `bridge_memory` field to every request to the Custom endpoint, through Include Body Parameters, with the chat's integrity id and file, the speaking card's avatar, the persona's avatar, the first message SillyTavern included and the generation type; nothing else in a request says which chat or persona it's for. Quiet and impersonate generations get no memory. It also shows conflict alerts and lists a chat's memories. It authenticates to the bridge's `/ext/memory/*` routes with a random token it sends in that field, which the bridge accepts once it has arrived on an authenticated `/v1` request, as the browser can't send the API key or the dashboard password.
- **Memory goes to a CLI session once.** A new session gets a `<character_memory>` block before the transcript; later turns carry only recaps that are new or changed, in `<character_memory_update>`, and a note when one no longer applies. A recap still being rebuilt after its chat grew stays as the session has it until it's done. Upstream appended its whole memory block to every turn, so a resumed session collected a copy per turn. Of other chats, the newest get their full recap while 24,000 characters last, then their short one. The block tells the model the memories count over the card's description and scenario about the character's history and relationships.
- **Group reply.** The Bridge Memory extension adds "Group reply (all unmuted)" to a group's Reply strategy. Each turn is one reply written jointly by every unmuted member: their cards are joined as with "Join character cards (exclude muted)", whatever the group's handling mode says, and `{{char}}` (in the main prompt, the group nudge, presets) is their names joined by commas ("Alice, Bob"), which is also the name the reply is saved under. Swipe and continue keep it joint, and SillyTavern's group trimming is off for those turns, so a reply isn't cut at a member's `Name:` line. SillyTavern's files are untouched: the extension stores the choice in its own settings and changes the group in memory around each turn, putting it back afterwards. The reply carries the first unmuted member's avatar, and Character Memory treats it as that card's. Auto mode, `/trigger` and impersonate work as before.
- **Conflict alerts.** Once a chat with memories from other chats has your first message, Sonnet compares its opening (greeting, first message, Author's Note) with the memories of chats the character was in (background isn't checked). If they clash, the extension shows it in SillyTavern after a reply: keep the memories, or untick the ones to leave out of that chat. After a decision, only chats added later are checked again.
- **Memory tab rewritten.** A **Connections** list shows each chat with the chats it remembers joined under it, who remembers each, whom it's background for, and its recap per member with chapters to tick; a four-step form connects any chat to any other, saying who remembers it and whether it comes as a synopsis or chapters (`GET /api/memory/chats` searches every chat file, `GET /api/memory/connect/preview` lists who'd remember it with a cost estimate, `POST /api/memory/link` with `who`). Pick any character card, add other cards of the same person, bind personas and tick their chats (with Select all and a cost estimate), and open any chat to read or rewrite its recap part by part, connect other chats to it, or leave a binding's chat out of it. It shows whether the extension has been heard from, what memory did on the latest turn, and the recap jobs running. Chats are listed by their card's or group's name (not the file name), their branch ("Branch #3") or own title, the day they were made and their last message, and can be searched by any of those; personas by name, title and description, with the ones in the character's chats first. A chat's recap reads as text when the chat is opened, and opens in place under it wherever it's remembered (another chat's memories, a binding).
- **Synopsis or chapters, with a word target.** Other chats don't get a remembered chat's 40-message part recaps but what's written from them: one synopsis of the whole chat in about a set number of words (400 by default), or chapters. Each connection and each binding chooses which (`links.mode`, `bindings.mode`; `mode` on `POST /api/memory/link` and `POST /api/memory/binding`, which takes `action: "mode"` to change a binding's), in the connect form's fourth step, with **Edit** on a connection, and with a switch in each binding; the Memory tab's setting is the default for new ones, and connections and bindings made before keep the mode that was set then. A chat used both ways has both written from the same part recaps. In chapters mode Sonnet divides a chat of three parts or more into chapters by event, at the scene breaks its part recaps mark, so a chapter can start in the middle of a part, once per chat for every character (`plans`), titles them, and writes each from its own scenes in about a set number of words (250 by default); a shorter chat comes as its synopsis. Each connection and each binding chooses which chapters it passes on (`links.chapters_off`, `binding_chats.chapters_off`; `POST /api/memory/chapters`), so a chat's private scene can reach one chat and not another. When the chat grows, only its last chapter is divided again, so earlier chapters keep their place and the choices made about them. Scene recaps already within the target go as they are, with no call. Synopses and chapter recaps are cached in `memory.db` by the part recaps they're written from and the word target, can be rewritten on the Memory tab (`POST /api/memory/condensed`), and replace the old 150-word short recap. A chat whose chapters don't all fit the 24,000-character budget loses its earliest ones. Settings: `memory_mode` (the default), `memory_synopsis_words`, `memory_chapter_words`.
- **Offline memory tests.** `tests/memory_scenarios.py` runs connections (for one character, for everyone in a group chat, of chats a character isn't in), bindings, group privacy, conflicts, the in-chat recap, the budget, synopses and chapters, and the Memory tab's endpoints against a scratch bridge, a fake SillyTavern data folder and the fake Messages API, and uses no quota.
- **Restyled dashboard with a prompt preview.** The dashboard uses a plain sans-serif layout instead of the serif/italic editorial styling. The Prompts tab previews what a roleplay turn sends to the CLI, for a new session and for a resumed turn: the bridge's own text placed around SillyTavern's prompt order, which `GET /api/prompt_preview` reads from SillyTavern's `settings.json` (found next to the bridge's folder, or via the Lorebook tab's worlds path). Card, lore and chat-history entries show as placeholders.
- **Editable Creativity texts.** The Claude CLI has no temperature setting, and Creativity (Precise / Balanced / Creative / Wild) only adds the selected mode's style text to the system prompt. The Prompts tab shows each mode's text and lets you edit it, Balanced included (it adds nothing by default), with Restore default per mode. Edits are kept in `bridge_settings.json` (`creativity_prompts`); `POST /api/settings` takes `{"creativity_prompts": {"<mode>": "<text>" | null}}`, where null restores the default. Editing the mode in use starts a new CLI session on each character's next turn, like any change to the bridge's part of the system prompt. SillyTavern's own Temperature, Top P and penalty settings are still ignored.
- **History and Usage tabs.** Every `claude -p` call the bridge makes (roleplay turns, Character Memory, lorebook and image calls) is recorded in `prompt_history.db` with its argv, system prompt, stdin text, reply, thinking summary, and the token counts from the CLI's `result` event. The History tab lists them, and marks a call during which the CLI reported a rate-limit warning with its window and share used, such as "Weekly pace · 50%": Claude Code warns when a window is being used faster than it refills (half the weekly limit in under 35% of the week, say), not only near the limit. For each roleplay turn the bridge also reads the account's 5-hour and weekly usage just before the turn and a few seconds after it, and shows the change. It reads them from the endpoint behind Claude Code's `/usage` (`GET https://api.anthropic.com/api/oauth/usage`) with the CLI's own login token from `~/.claude/.credentials.json`; the bridge never refreshes that token itself, and the reads use no quota. The endpoint reports whole percents and counts everything that used the account meanwhile, so per-turn figures are approximate. The Usage tab shows the current limits and the bridge's token totals for the current windows. Text is kept for the last 500 calls and token counts for 30 days.
- **No summaries.** Upstream's auto-summary (a per-character running digest that replaced older messages), one-shot chunking, summaries generated from a chat file, the summary cache, `/v1/chunked/process`, the Tools tab and the `prompts/` templates are gone. Character Memory recaps a chat's messages that no longer fit the context instead, part by part from the chat file, and CLI session reuse keeps resumed turns small.

- **Own version numbers.** This fork is run as a separate project and doesn't track or merge upstream releases. Versions are by date, `YEAR.MONTH.BUILD`, where the build counts that month's builds from 1 (`2026.10.1`, `2026.10.2`, then `2026.11.1`), and each build is tagged `v<version>`. The update checker reads this fork's version tags instead of upstream's GitHub releases, so the dashboard shows when the running bridge is older than the newest tagged build.

With no environment variables set, the network and auth changes are the default listen address and the usage reads above.

---

**A Flask-based roleplay bridge that wraps the Claude Code CLI as a SillyTavern-compatible backend.**

Sits between SillyTavern and the Claude Code CLI, translating OpenAI-compatible API requests into `claude -p` subprocess calls, injecting narrative-focused system prompts, and layering a full roleplay feature stack on top — Character Memory, auto-lorebook generation, image handling via Claude Code's `Read` tool, and a GUI dashboard to configure everything.

Uses your **Claude Code subscription**. No API keys, no per-token billing, no credential impersonation. The actual `claude` CLI does the work; the bridge is just a polite translator with features.

![Bridge GUI](docs/screenshot-gui.png)

---

## Why this exists

SillyTavern is an excellent frontend for creative writing and roleplay, but it speaks OpenAI's API format. Claude Code CLI is how you access Claude's best models on a subscription plan — but it's designed for coding, not long-form fiction. This bridge makes them talk to each other, and adds the things you actually want for long RPs that coding assistants don't care about:

- **Character Memory** — characters remember the chats you connect, and the chats of a persona you bind them to, as recaps from their point of view
- **SillyTavern's prompt as the system prompt**, replacing Claude Code's built-in "you are a coding assistant" framing
- **Image handling** via Claude Code's native `Read` tool — share reference images in SillyTavern and Claude actually sees them
- **Auto-lorebook** generation that builds World Info entries from your roleplay in the background

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint (SillyTavern just points at it)
- GUI dashboard at `http://localhost:5001/` with tabs for Settings, System Prompt, Tools, Lorebook, Memory, and Test
- **Model picker** — every current Claude model (Fable, Opus, Sonnet and Haiku, pinned versions or "latest" aliases), selected in the bridge GUI; or set it to "Use SillyTavern's model" to pick per connection in SillyTavern
- **Effort levels** — Low / Medium / High / xHigh / Max for the Claude CLI's reasoning budget. xHigh and Max require Opus; Sonnet 4.6 and 4.5 are clamped to Medium as they produce no meaningful narrative above that level.
- **Character Memory** — Sonnet recaps chats from SillyTavern's folder from a character's point of view, and the parts they aren't in as plain accounts. Connect any chat to another, for everyone in it or chosen group members, or bind a character to a persona so they remember the chats with it you pick, and every new one. A small SillyTavern extension (`st-extension/`) says which chat and persona each request is for, shows when a chat's opening contradicts what the character remembers, and lets you read the recaps a chat brings in. Read and edit every recap on the Memory tab.
- **CLI session reuse** — captures the Claude CLI's session and resumes via `--resume` on follow-up turns, sending only what came after the session's last reply (plus any lore SillyTavern added or changed) with the session's original system prompt. Big input-token savings on long RPs. Group chats get a session per character. Starts a new session on swipes, edits, deletions, or changes to the bridge's own prompt settings.
- **Per-character image pipeline** — SillyTavern base64 images get saved to `temp_images/` and injected as file paths so Claude Code's `Read` tool can view them directly
- **Auto-lorebook** generation after each response using Sonnet for efficiency, plus a Deep Analysis mode that scans a full chat file, and a manual entry editor
- **Creativity modes** (Precise / Balanced / Creative / Wild) — prompt-based style control since Claude Code CLI doesn't expose temperature
- **Thinking summaries** — when `include_thinking` is on, Claude's summarized thinking is returned as `reasoning_content` for SillyTavern's reasoning block; nothing asks the model to write its reasoning into the reply
- **Test tab** — fire a quick test message at the bridge from the GUI without going through SillyTavern, useful for sanity checks
- **Settings persistence** — model, effort, creativity, thresholds, and port all survive bridge restarts via `bridge_settings.json`
- **Configurable port** from the GUI (default `:5001`)
- **Tool calling fallback** for SillyTavern extensions like TunnelVision
- **Update checker** — checks GitHub on startup and warns if a newer version is available
- **Debug logging** with structured output, token usage panels, narrative-failure detection, and `stop_reason` capture

## What it actually produces

The GUI shot above is what the tool looks like. This is what it *does* — a mid-RP scene rendered in SillyTavern, pulled from a **142-message roleplay** with the bridge running Opus at high effort and the default system prompt (upstream's, with the auto-summary this fork has since replaced with Character Memory):

![Narrative output in SillyTavern](docs/screenshot-narrative.png)

A few things worth noticing that come directly from the bridge's systems:

- **HTML embeds render inline.** The "Guild of Bounties & Bonds" card mid-scene is a rendered block, not escaped text. The default system prompt's `[Tools]` section tells Claude it can use colored dialogs, CSS blocks, and HTML visual elements — and the bridge passes them through without stripping, so SillyTavern renders them live.
- **Color-coded inline spans.** "Red for high threat. Amber for moderate. Green for low." isn't plain text — those are styled spans the model emitted and the pipeline preserved end-to-end.
- **Memory across 100+ turns.** The troll-ear callback ("you left a troll ear on somebody's paperwork") references an event that happened dozens of turns earlier — long enough to have fallen out of any context window without memory management. It survived because upstream's auto-summary kept *specific details* (the troll ear, the paperwork, the sardonic framing). In this fork, Character Memory's recap of a chat's earlier messages does that job.
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
```

For Character Memory, also install the SillyTavern extension: `st-extension/install.sh` (or copy `st-extension/` to `SillyTavern/data/<user>/extensions/bridge-memory/`), then reload SillyTavern.

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
.venv/bin/pip install -r requirements.txt
```

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

**Enable Character Memory** in the Settings tab, and install the Bridge Memory extension (`st-extension/install.sh`, then reload SillyTavern), if you want characters to remember other chats. On the Memory tab, **Connect chats** picks the chat that remembers, the chat it remembers (any chat in SillyTavern) and, in a group chat, who remembers it; or bind a character to a persona and tick the chats they should remember, and new chats with that persona join by themselves. The bridge recaps those chats in the background with Sonnet, and each CLI session gets the recaps once, then only what changed. If a chat's opening contradicts what the character remembers, SillyTavern shows it after a reply, and you keep the memories or drop some from that chat.

**The roleplay system prompt** is your SillyTavern prompt, behind a short frame (`BRIDGE_FRAME` in `claude_bridge.py`) that explains how SillyTavern's messages are laid out. Optional style notes from the Prompts tab (persisted to `bridge_settings.json`) are added before SillyTavern's prompt; "Load upstream prompt" fills in the upstream project's roleplay prompt (`UPSTREAM_STYLE_PROMPT`) as a starting point.

## Known limitations

These are **architectural**, not bugs — they're properties of running Claude Code CLI as a subprocess per request, and there's no clean fix inside the current CLI version.

- **Only plain replies stream.** A request with tools (tool calling) or a `response_format` schema still arrives in one piece at the end: a tool call can only be parsed out of the finished reply. A streamed reply is sent exactly as Claude writes it, so the clean-up that merges stray `<think>` blocks in a whole reply doesn't apply to it; SillyTavern's own reasoning auto-parse can handle those.
- **No temperature / sampling parameters.** Claude Code CLI doesn't expose `temperature`, `top_p`, or `top_k`. The Creativity setting (Precise / Balanced / Creative / Wild) is a prompt-based style modifier — Claude follows the style instructions, but it's not the same as a real temperature slider.
- **Per-request subprocess overhead.** Every RP turn spawns a fresh `claude -p` process, which adds startup latency compared to a persistent HTTP client. Not a problem for the long thinking times typical of high-effort requests, but noticeable for small ones. CLI session reuse mitigates this on follow-up turns.
- **No prompt caching control.** Claude Code manages its own caching internally; the bridge can't directly set cache breakpoints. CLI session reuse mitigates this by keeping the stable prefix large and the variable suffix small.

If any of these become dealbreakers for you, the right move is an alternative backend mode that hits the Anthropic SDK directly — the groundwork is there, but it's not currently implemented.

## Project structure

```
claude-code-sillytavern-bridge/
├── claude_bridge.py           # Main Flask server and subprocess wrapper
├── memory.py                  # Character Memory: chat recaps, connections, persona bindings
├── prompt_history.py          # History and Usage tabs: recorded calls + plan usage reads
├── requirements.txt           # Core Python dependencies
├── run_bridge.bat             # Windows launcher
├── run_bridge.sh              # Linux / macOS launcher (uses ./.venv if present)
├── deploy/
│   ├── claude-bridge.service  # Hardened systemd unit
│   └── claude-bridge.env.example  # Env vars for the unit (copy to /etc/claude-bridge.env)
├── st-extension/              # Bridge Memory, the SillyTavern extension Character Memory needs
│   └── install.sh             # Copies it into SillyTavern's user data folder
├── tests/
│   ├── session_scenarios.py   # Offline CLI session-reuse tests (real CLI, fake Messages API)
│   └── memory_scenarios.py    # Offline Character Memory tests (fake SillyTavern folder too)
├── templates/
│   └── index.html             # GUI dashboard (single-page, vanilla JS)
├── cache/                     # Image descriptions, cached (gitignored)
├── character_memory/          # memory.db: recaps, connections, bindings (gitignored)
├── temp_images/               # SillyTavern base64 image dumps (gitignored)
├── bridge_settings.json       # Persisted runtime settings (gitignored)
├── bridge_sessions.json       # Captured CLI session IDs for --resume (gitignored)
├── prompt_history.db          # Recorded calls for the History tab (gitignored)
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
