#!/usr/bin/env python3
"""Offline tests for CLI session reuse: which session a turn resumes
(claude_bridge._decide_resume) and what the resumed turn sends.

A scratch copy of the bridge runs on a free port with the real `claude` CLI,
pointed at a fake Messages API inside this process (ANTHROPIC_BASE_URL, a
dummy API key and a scratch CLAUDE_CONFIG_DIR). Nothing reaches Anthropic or
uses the subscription, and the live bridge's state is not touched.

    .venv/bin/python tests/session_scenarios.py                 # every scenario
    .venv/bin/python tests/session_scenarios.py branches solo   # some of them
    .venv/bin/python tests/session_scenarios.py --bridge-dir DIR  # another checkout
    .venv/bin/python tests/session_scenarios.py --keep          # keep the scratch dir

Each scenario replays a chat the way SillyTavern 1.19 sends it with Character
Names Behavior "Default": group replies are labeled "Name: ", group
generation mode "swap" sends only the speaker's card, {{char}} names the
speaker in the main prompt and in the Post-History Instructions, and an
author's note sits at depth 4 as an in-chat system message.

On every turn the request the CLI sent to the fake API (the system prompt
plus every turn of the resumed CLI session, i.e. all the model saw) must:
  - name only the current speaker in the main prompt, Post-History
    Instructions and character card;
  - contain every chat message SillyTavern sent, in SillyTavern's order;
  - contain no message that is no longer in the chat (edited, deleted,
    hidden, swiped away, or only in another branch);
  - contain every SillyTavern system block of the request;
and the turn must resume or start a session as the scenario expects.
"""
import argparse
import copy
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEY = "testkey"
USER = "V"

CARDS = {
    "Alice": "Alice is a cheerful botanist who talks to her plants and hums while she works in the greenhouse on deck three.",
    "Bob": "Bob is a gruff mechanic, terse and practical, who distrusts anything he can't take apart with a wrench.",
    "Carol": "Carol is a quiet archivist with a dry sense of humour who keeps meticulous notes on everyone aboard.",
    "Mira": "Mira is a wandering cartographer who maps cities that move overnight and never sleeps in the same place twice.",
}
GREETINGS = {
    "Alice": "Alice looked up from a tray of seedlings, soil on her gloves, and waved the trowel in greeting. 'Oh! Mind the ferns, they bruise easily. You must be the new recruit everyone keeps mentioning at lunch.' She beamed, wiping a smudge of dirt across her cheek without noticing it at all.",
    "Mira": "Mira unrolled a map across the cafe table, pinning its corners with two cups and a stone. 'The street you came in on won't be there tomorrow,' she said without looking up, tracing a line that shimmered faintly. 'So you'd better tell me now where you were trying to go, before it moves again.'",
}
PERSONA = f"[{USER} is a young man with dirty blonde hair and a filtration mask.]"
AUTHORS_NOTE = "[Author's note: it is raining outside.]"
CONTINUE_NUDGE = "[Continue your last message without repeating its original content.]"
SPEAKER_LABEL = re.compile(r"^(?:Alice|Bob|Carol|Mira): ")


# --- fake Messages API ------------------------------------------------------

class FakeAPI:
    """Records each /v1/messages body and answers with a short streamed reply
    naming the speaker the *system prompt* asks for. A request whose last
    two messages contain "SLOW" is answered after 6 seconds. `reply(body)`,
    when given, can answer instead: it returns the text, or None for the
    default."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._json({})

            def _json(self, obj):
                data = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                    return self._json({"input_tokens": 100})
                m = re.search(r"Write (\w*)'s next reply", _text(body.get("system") or ""))
                with api.lock:
                    api.calls.append(body)
                    n = len(api.calls)
                text = (api.reply(body) if api.reply else None) or f"{m.group(1) if m else '?'} reply #{n}: the scene moves on."
                if any("SLOW" in _text(x.get("content", "")) for x in body.get("messages", [])[-2:]):
                    time.sleep(6)
                if not body.get("stream"):
                    return self._json({
                        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model"),
                        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                        "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}})
                try:
                    self._stream(n, body, text)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the bridge killed the CLI (client disconnect)

            def _stream(self, n, body, text):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def ev(name, data):
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                ev("message_start", {"type": "message_start", "message": {
                    "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model"),
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}})
                ev("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}})
                for piece in (text[:10], text[10:]):
                    ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": piece}})
                ev("content_block_stop", {"type": "content_block_stop", "index": 0})
                ev("message_delta", {"type": "message_delta", "usage": {"output_tokens": 10},
                                     "delta": {"stop_reason": "end_turn", "stop_sequence": None}})
                ev("message_stop", {"type": "message_stop"})
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self):
        with self.lock:
            return len(self.calls)

    def last_since(self, n):
        with self.lock:
            return self.calls[-1] if len(self.calls) > n else None


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


# --- scratch bridge -----------------------------------------------------------

class Bridge:
    def __init__(self, src, work, api_port, python, settings=None):
        self.work = work
        d = os.path.join(work, "bridge")
        os.makedirs(d)
        for name in ("claude_bridge.py", "memory.py", "prompt_history.py"):
            shutil.copy(os.path.join(src, name), d)
        for name in ("templates", "prompts"):  # prompts/: older checkouts' summary prompts
            if os.path.isdir(os.path.join(src, name)):
                shutil.copytree(os.path.join(src, name), os.path.join(d, name))
        with open(os.path.join(d, "bridge_settings.json"), "w") as f:
            json.dump({"update_check_enabled": False, "debug_output": True, "cli_session_reuse": True,
                       "include_thinking": False, "show_thinking_console": False,
                       "model": "claude-opus-5-5", "effort_level": "low",
                       "character_memory_enabled": False, **(settings or {})}, f)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BRIDGE_", "ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_DIR", "CLAUDECODE"))}
        env.update({
            "BRIDGE_HOST": "127.0.0.1", "BRIDGE_PORT": str(self.port), "BRIDGE_API_KEY": API_KEY,
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api_port}", "ANTHROPIC_API_KEY": "sk-ant-test-dummy",
            "CLAUDE_CONFIG_DIR": os.path.join(work, "claude-config"),
            "DISABLE_AUTOUPDATER": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "PYTHONUNBUFFERED": "1",
        })
        self.log = open(os.path.join(work, "bridge.log"), "w")
        self.proc = subprocess.Popen([python, "claude_bridge.py"], cwd=d, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1)
                return
            except Exception:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"scratch bridge didn't start; see {self.log.name}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def send(self, msgs, extra=None):
        body = json.dumps({"model": "claude-opus-5-5", "stream": True, "messages": msgs, **(extra or {})}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=body, headers={
            "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
        out = []
        with urllib.request.urlopen(req, timeout=180) as r:
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                for ch in json.loads(line[5:]).get("choices", []):
                    out.append((ch.get("delta") or {}).get("content") or "")
        return "".join(out).strip()

    def send_and_disconnect(self, msgs):
        """Start a streamed turn and drop the connection after the first keepalive."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     json.dumps({"model": "claude-opus-5-5", "stream": True, "messages": msgs}),
                     {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
        r = conn.getresponse()
        r.read1(64)
        conn.sock.shutdown(socket.SHUT_RDWR)
        r.close()
        conn.close()


# --- SillyTavern chat model ---------------------------------------------------

class Chat:
    """A SillyTavern chat and the requests ST would send for it. Chats forked
    from one another (branches) share `texts`, every message text any of them
    has had, so text that is only in another branch counts as stale too."""

    def __init__(self, ctx, group, mode="swap", texts=None):
        self.ctx = ctx
        self.group = group
        self.mode = mode
        self.members = []
        self.entries = []         # {"name", "is_user", "mes", "hidden"}
        self.texts = texts if texts is not None else set()
        self.trim = None          # keep only the last N chat messages (context budget)
        self.names_field = False  # Character Names Behavior "Message Names": `name`, no label
        self.lore = None          # world info block before the card
        self.marker = None        # (text, depth): a preset's user-role message at a fixed depth

    def add(self, name, is_user, mes):
        self.entries.append({"name": name, "is_user": is_user, "mes": mes, "hidden": False})
        self.texts.add(mes)

    def greet(self, name):
        self.add(name, False, GREETINGS[name])

    def user(self, text):
        self.add(USER, True, text)

    def fork(self, upto=None):
        """A branch with the messages up to index `upto` (inclusive)."""
        b = Chat(self.ctx, self.group, self.mode, self.texts)
        b.members = list(self.members)
        b.entries = copy.deepcopy(self.entries if upto is None else self.entries[:upto + 1])
        b.trim, b.names_field, b.lore, b.marker = self.trim, self.names_field, self.lore, self.marker
        return b

    def system_blocks(self, speaker):
        blocks = [
            f"[SYSTEM PROMPT]\nWrite {speaker}'s next reply in a fictional roleplay. "
            f"Stay in character as {speaker}.\n[NSFW]\nAnything goes.",
            PERSONA,
        ]
        if self.lore:
            blocks.append(self.lore)
        for name in ([speaker] if self.mode == "swap" else self.members):
            blocks.append(f'[Character("{name}")]\n{CARDS[name]}')
        return blocks + ["[Example Chat]", f'"Example line for {speaker}."']

    def build(self, speaker, trailing=()):
        msgs = [{"role": "system", "content": b} for b in self.system_blocks(speaker)]
        hist = []
        for e in self.entries:
            if e["hidden"]:
                continue
            role = "user" if e["is_user"] else "assistant"
            if self.names_field:
                hist.append({"role": role, "content": e["mes"], "name": e["name"]})
            else:
                label = f"{e['name']}: " if (self.group and not e["is_user"]) else ""
                hist.append({"role": role, "content": label + e["mes"]})
        if self.trim:
            hist = hist[-self.trim:]
        if self.marker:
            text, depth = self.marker
            hist.insert(len(hist) - depth, {"role": "user", "content": text})
        if len(hist) >= 4:
            hist.insert(len(hist) - 4, {"role": "system", "content": AUTHORS_NOTE})
        msgs += hist
        msgs.append({"role": "system", "content": f"[CORE ROLEPLAY]\nWrite ONLY for {speaker}. Never write for other characters."})
        msgs += [{"role": "system", "content": t} for t in trailing]
        return msgs

    def stale(self):
        """Texts that must not reach the model: no longer in what ST sends."""
        shown = [e["mes"] for e in self.entries if not e["hidden"]]
        return {t for t in self.texts if not any(t in s for s in shown)}

    def turn(self, speaker, label, expect, kind="normal"):
        """kind: normal (append the reply), swipe (replace the last reply),
        continue (extend the last reply), error (the call must fail)."""
        if kind == "swipe":
            self.entries.pop()
        msgs = self.build(speaker, [CONTINUE_NUDGE] if kind == "continue" else ())
        n = self.ctx.api.count()
        reply = self.ctx.bridge.send(msgs)
        body = self.ctx.api.last_since(n)
        if kind == "error":
            ok = "Error" in reply[:200]
            self.ctx.record(label, ok, [] if ok else [f"expected an error, got {reply[:60]!r}"])
            return reply
        cards = {speaker} if self.mode == "swap" else set(self.members)
        why = check(speaker, cards, body, msgs, self.stale(), {self.marker[0]} if self.marker else set())
        resumed = body is not None and sum(1 for m in body["messages"] if m["role"] == "user") > 1
        if resumed != (expect == "resumed"):
            why.append(f"expected a {expect} session")
        self.ctx.record(label, not why, why, "resumed" if resumed else "new")
        self.texts.add(reply)
        if kind == "continue":
            self.entries[-1]["mes"] += " " + reply
            self.texts.add(self.entries[-1]["mes"])
        else:
            self.add(speaker, False, reply)
        return reply

    def edit(self, index, new_text):
        self.entries[index]["mes"] = new_text
        self.texts.add(new_text)

    def delete(self, index=-1):
        self.entries.pop(index)

    def move_up(self, index):
        e = self.entries
        e[index - 1], e[index] = e[index], e[index - 1]

    def select_swipe(self, text):
        self.entries[-1]["mes"] = text


def check(speaker, cards, body, msgs, stale, markers=()):
    """The checks listed in the module docstring. `markers` are preset
    messages SillyTavern re-inserts at a fixed depth every turn: a resumed
    session has them where it first saw them (ones after its last reply are
    sent again), so they only have to be present."""
    if body is None:
        return ["the CLI made no API call"]
    sys_text = _text(body.get("system") or "")
    view = "\n".join(_text(m["content"]) for m in body["messages"] if m["role"] in ("user", "assistant"))
    full = sys_text + "\n" + view
    why = []
    for what, pattern, want in (("main prompt", r"Write (\w*)'s next reply", {speaker}),
                                ("post-history", r"Write ONLY for (\w*)\.", {speaker}),
                                ("cards", r'\[Character\("(\w+)"\)\]', cards)):
        names = set(re.findall(pattern, full))
        if names != want:
            why.append(f"{what} name {sorted(names)}")
    pos = 0
    for m in msgs:
        if m["role"] == "system":
            continue
        if m["content"] in markers:
            if m["content"] not in view:
                why.append(f"preset marker missing: {m['content'][:40]!r}")
            continue
        text = SPEAKER_LABEL.sub("", m["content"])
        i = view.find(text, pos)
        if i < 0:
            why.append(f"missing or out of order: {text[:40]!r}")
            break
        pos = i + len(text)
    lost = [m["content"] for m in msgs if m["role"] == "system" and m["content"] not in full]
    if lost:
        why.append(f"SillyTavern block never delivered: {lost[0][:40]!r}")
    leaked = sorted(t for t in stale if t in view)
    if leaked:
        why.append(f"stale text reached the model: {leaked[0][:40]!r}")
    return why


class Ctx:
    def __init__(self, api, bridge):
        self.api, self.bridge = api, bridge
        self.results = []

    def record(self, label, ok, why, mode=""):
        self.results.append((label, ok, why, mode))


# --- scenarios ----------------------------------------------------------------

def group_swap(ctx):
    c = Chat(ctx, group=True)
    c.members = ["Alice", "Bob"]
    c.greet("Alice")
    c.user("(1) V waves at Alice and Bob.")
    c.turn("Alice", "Alice answers", "new")
    c.turn("Bob", "Bob follows (auto, no user message)", "new")
    c.user("(2) V asks Alice about the ferns.")
    c.turn("Alice", "Alice gets Bob's reply and the user's", "resumed")
    c.turn("Bob", "Bob follows", "resumed")
    c.turn("Bob", "swipe Bob", "new", kind="swipe")
    c.user("(3) V hands Bob a wrench.")
    c.turn("Alice", "Alice answers", "resumed")
    c.edit(-1, "Alice (edited by user) drops the trowel and runs to the door.")
    c.user("(4) V follows Alice to the door.")
    c.turn("Bob", "Bob gets Alice's edited reply", "resumed")
    c.turn("Alice", "Alice after her own reply was edited", "new")
    c.delete()
    c.user("(5) V decides to stay inside instead.")
    c.turn("Alice", "Alice after her last reply was deleted", "new")
    c.members.append("Carol")
    c.user("(6) V notices someone new: Carol.")
    c.turn("Carol", "Carol added to the group, first turn", "new")
    c.turn("Alice", "Alice after Carol", "resumed")
    c.turn("Bob", "Bob after Alice and Carol", "resumed")
    c.user("(7) V asks everyone what happens next.")
    c.turn("Carol", "Carol again", "resumed")


def group_join(ctx):
    c = Chat(ctx, group=True, mode="join")
    c.members = ["Alice", "Bob"]
    c.greet("Alice")
    c.user("(1) V says hello to both.")
    c.turn("Alice", "Alice (all cards sent)", "new")
    c.turn("Bob", "Bob (all cards sent)", "new")
    c.user("(2) V asks Bob a question.")
    c.turn("Bob", "Bob again", "resumed")
    c.turn("Alice", "Alice", "resumed")


def group_names_field(ctx):
    c = Chat(ctx, group=True)
    c.names_field = True
    c.members = ["Alice", "Bob"]
    c.lore = "[Lore: the greenhouse is on deck three.]"
    c.greet("Alice")
    c.user("(1) V says hi.")
    c.turn("Alice", "Alice (Message Names)", "new")
    c.turn("Bob", "Bob (Message Names)", "new")
    c.user("(2) V asks Alice.")
    c.turn("Alice", "Alice resumes", "resumed")
    c.turn("Bob", "Bob resumes", "resumed")
    c.lore = "[Lore: the greenhouse flooded last week.]"
    c.user("(3) V asks about the flood.")
    c.turn("Alice", "lore changed: Alice", "new")
    c.turn("Bob", "lore changed: Bob", "new")
    c.user("(4) V leaves.")
    c.turn("Alice", "Alice resumes again", "resumed")
    body = ctx.api.calls[-1]
    view = "\n".join(_text(m["content"]) for m in body["messages"] if m["role"] in ("user", "assistant"))
    ok = "Assistant: Bob: Bob reply" in view
    ctx.record("Bob's replies are labeled in Alice's view", ok, [] if ok else ["no 'Assistant: Bob:' label"])


def group_history(ctx):
    """Edits, deletions and moves of older messages in a group chat."""
    c = Chat(ctx, group=True)
    c.members = ["Alice", "Bob"]
    c.greet("Alice")
    for i in range(1, 4):
        c.user(f"({i}) V keeps talking.")
        c.turn("Alice", f"round {i}: Alice", "new" if i == 1 else "resumed")
        c.turn("Bob", f"round {i}: Bob", "new" if i == 1 else "resumed")
    c.edit(3, "Bob (edited by user, long ago) never touched the wrench.")
    c.user("(4) V asks about the wrench.")
    c.turn("Alice", "Alice after an old Bob reply was edited", "new")
    c.turn("Bob", "Bob after his old reply was edited", "new")
    c.user("(5) V shrugs.")
    c.turn("Alice", "Alice resumes", "resumed")
    c.move_up(6)
    c.user("(6) V looks around.")
    c.turn("Bob", "Bob after two old messages were swapped", "new")
    c.turn("Alice", "Alice after the swap", "new")
    c.delete(1)
    c.user("(7) V forgets what he said first.")
    c.turn("Bob", "Bob after the first user message was deleted", "new")
    c.turn("Alice", "Alice after the deletion", "new")
    c.user("(8) V carries on.")
    c.turn("Bob", "Bob resumes", "resumed")


def solo(ctx):
    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    c.user("(1) V points at the river on the map.")
    c.turn("Mira", "first turn", "new")
    c.user("(2) V asks where the river goes.")
    c.turn("Mira", "next turn", "resumed")
    c.turn("Mira", "swipe", "new", kind="swipe")
    c.user("(3) V buys Mira a coffee.")
    c.turn("Mira", "next turn after the swipe", "resumed")
    c.edit(-1, "Mira (edited by user) folds the map away and leaves the cafe.")
    c.user("(4) V runs after her.")
    c.turn("Mira", "after the user edited her last reply", "new")
    c.user("(5) V catches up at the corner.")
    c.turn("Mira", "next turn", "resumed")
    c.delete()
    c.delete()
    c.user("(5b) V loses her in the crowd instead.")
    c.turn("Mira", "after deleting her reply and the user's message", "new")
    c.turn("Mira", "continue", "new", kind="continue")
    c.user("(6) V asks for directions.")
    c.turn("Mira", "next turn after continue", "new")
    c.user("(7) V thanks her.")
    c.turn("Mira", "next turn", "resumed")


def solo_trimmed(ctx):
    """A long chat that no longer fits the context: SillyTavern drops the
    oldest messages, greeting included, and sessions still resume."""
    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    c.trim = 6
    for i in range(1, 7):
        c.user(f"({i}) V keeps walking.")
        c.turn("Mira", f"trimmed history, turn {i}", "new" if i == 1 else "resumed")


def solo_history(ctx):
    """Edits, deletions, hiding and moving of older messages, and a preset
    marker inside the history."""
    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    for i in range(1, 6):
        c.user(f"({i}) V walks on.")
        c.turn("Mira", f"turn {i}", "new" if i == 1 else "resumed")
    c.edit(2, "Mira (edited by user, long ago) refused to draw the map.")
    c.user("(6) V asks about the map.")
    c.turn("Mira", "after an old reply was edited", "new")
    c.user("(7) V nods.")
    c.turn("Mira", "next turn", "resumed")
    c.delete(3)
    c.user("(8) V forgets something.")
    c.turn("Mira", "after an old user message was deleted", "new")
    c.user("(9) V nods again.")
    c.turn("Mira", "next turn", "resumed")
    c.move_up(4)
    c.user("(10) V looks back.")
    c.turn("Mira", "after two old messages were swapped", "new")
    c.entries[5]["hidden"] = True
    c.user("(11) V hides a memory.")
    c.turn("Mira", "after an old message was hidden", "new")
    c.entries[5]["hidden"] = False
    c.user("(12) V remembers it again.")
    c.turn("Mira", "after it was unhidden (the pre-hide session fits again)", "resumed")
    c.marker = ("<latest_turn_end>", 2)
    c.user("(13) V waits.")
    c.turn("Mira", "a preset marker appears at depth 2", "new")
    c.user("(14) V waits more.")
    c.turn("Mira", "the marker moved; the session resumes", "resumed")
    c.user("(15) V leaves.")
    c.turn("Mira", "and again", "resumed")


def solo_edge(ctx):
    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    c.user("(1) V points at the river.")
    c.turn("Mira", "first turn", "new")
    c.user("(2) V asks where it goes.")
    r2 = c.turn("Mira", "next turn", "resumed")
    c.turn("Mira", "swipe", "new", kind="swipe")
    c.select_swipe(r2)
    c.user("(3) V picks the first answer and asks more.")
    c.turn("Mira", "after swiping back to the first reply", "resumed")
    c.lore = "[Lore: the river is cursed.]"
    c.user("(4) V mentions the river.")
    c.turn("Mira", "a lore entry appears (sent as an update)", "resumed")
    c.lore = "[Lore: the bridge is cursed.]"
    c.user("(5) V mentions the bridge.")
    c.turn("Mira", "the lore entry changes (sent as an update)", "resumed")
    c.marker = ("<latest_turn_end>", 0)
    c.user("(6) V nods.")
    c.turn("Mira", "a preset marker after the chat", "resumed")
    c.user("(7) V nods again.")
    c.turn("Mira", "the marker moved to the new end", "resumed")
    for f in _session_files(ctx):
        os.unlink(f)
    c.user("(8) V asks one more thing.")
    c.turn("Mira", "the CLI's session file is gone", "", kind="error")
    c.turn("Mira", "retry after the error", "new")


def branches(ctx):
    o = Chat(ctx, group=False)
    o.members = ["Mira"]
    o.greet("Mira")
    for i in range(1, 4):
        o.user(f"({i}) V follows the main road.")
        o.turn("Mira", f"original, turn {i}", "new" if i == 1 else "resumed")
    old = o.fork(upto=2)  # greeting, (1), Mira's first reply
    old.user("(2') V takes the side road instead.")
    old.turn("Mira", "branch from an older message", "new")
    o.user("(4) V keeps to the main road.")
    o.turn("Mira", "back in the original", "resumed")
    tip = o.fork()
    tip.user("(5b) V turns back in the branch.")
    tip.turn("Mira", "branch from the latest message", "resumed")
    o.user("(5) V goes on in the original.")
    o.turn("Mira", "original after the branch went on", "new")
    tip.user("(6b) V keeps going back.")
    tip.turn("Mira", "the branch again", "resumed")
    o.user("(6) V goes on.")
    o.turn("Mira", "the original again", "resumed")
    old.user("(3') V reaches the end of the side road.")
    old.turn("Mira", "the older branch again", "resumed")


def group_branches(ctx):
    o = Chat(ctx, group=True)
    o.members = ["Alice", "Bob"]
    o.greet("Alice")
    o.user("(1) V says hi.")
    o.turn("Alice", "original: Alice", "new")
    o.turn("Bob", "original: Bob", "new")
    b = o.fork()
    b.user("(2b) V asks Bob in the branch.")
    b.turn("Bob", "branch: Bob", "resumed")
    b.turn("Alice", "branch: Alice", "resumed")
    o.user("(2) V asks Alice in the original.")
    o.turn("Alice", "original: Alice after the branch went on", "new")
    o.turn("Bob", "original: Bob after the branch went on", "new")
    b.user("(3b) V carries on in the branch.")
    b.turn("Alice", "branch: Alice again", "resumed")


def cancelled(ctx):
    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    c.user("(1) V says hi.")
    c.turn("Mira", "first turn", "new")
    c.user("(2) SLOW V asks a long question.")
    ctx.bridge.send_and_disconnect(c.build("Mira"))
    time.sleep(4)  # the bridge notices on its next keepalive writes
    c.turn("Mira", "retry after a cancelled resumed turn", "new")
    body = ctx.api.calls[-1]
    view = "\n".join(_text(m["content"]) for m in body["messages"] if m["role"] in ("user", "assistant"))
    k = view.count("(2) SLOW V asks a long question.")
    ctx.record("the cancelled user turn reaches the model once", k == 1, [] if k == 1 else [f"{k} times"])


def creativity(ctx):
    """The Creativity text follows the Prompts tab's edits. Editing the mode
    in use changes the bridge's part of the system prompt, so the next turn
    starts a new session; editing another mode doesn't."""
    def settings(body):
        req = urllib.request.Request(f"http://127.0.0.1:{ctx.bridge.port}/api/settings", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()

    def system_has(label, *texts, absent=()):
        sys_text = _text(ctx.api.calls[-1].get("system") or "")
        why = [f"missing {t!r}" for t in texts if t not in sys_text] + [f"has {t!r}" for t in absent if t in sys_text]
        ctx.record(label, not why, why)

    c = Chat(ctx, group=False)
    c.members = ["Mira"]
    c.greet("Mira")
    try:
        settings({"creativity": "wild", "creativity_prompts": {"wild": "CREATIVITY-ONE"}})
        c.user("(1) V asks Mira to surprise them.")
        c.turn("Mira", "first turn, Wild text edited", "new")
        system_has("the edited Wild text is sent", "CREATIVITY-ONE", absent=["WILD MODE"])
        settings({"creativity_prompts": {"precise": "CREATIVITY-UNUSED"}})
        c.user("(2) V waits.")
        c.turn("Mira", "after editing a mode not in use", "resumed")
        settings({"creativity_prompts": {"wild": "CREATIVITY-TWO"}})
        c.user("(3) V waits some more.")
        c.turn("Mira", "after editing the mode in use", "new")
        system_has("with the new text", "CREATIVITY-TWO", absent=["CREATIVITY-ONE", "CREATIVITY-UNUSED"])
        settings({"creativity_prompts": {"wild": None}})
        c.user("(4) V gives up waiting.")
        c.turn("Mira", "after restoring the default", "new")
        system_has("with the default text", "WILD MODE", absent=["CREATIVITY-TWO"])
    finally:
        settings({"creativity": "balanced", "creativity_prompts": {"wild": None, "precise": None}})


def _session_files(ctx):
    root = os.path.join(ctx.bridge.work, "claude-config", "projects")
    return [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith(".jsonl")]


SCENARIOS = {f.__name__: f for f in (
    group_swap, group_join, group_names_field, group_history, group_branches,
    solo, solo_trimmed, solo_history, solo_edge, branches, cancelled, creativity)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*", metavar="scenario",
                    help=f"any of: {', '.join(SCENARIOS)} (default: all)")
    ap.add_argument("--bridge-dir", default=REPO, help="bridge checkout to test (default: this repo)")
    ap.add_argument("--python", default=None, help="Python for the bridge (default: the repo's .venv)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = ap.parse_args()
    unknown = [n for n in args.scenarios if n not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    python = args.python or next((p for p in (os.path.join(REPO, ".venv", "bin", "python"),) if os.path.exists(p)),
                                 sys.executable)
    if not shutil.which("claude"):
        sys.exit("the claude CLI isn't on PATH")

    work = tempfile.mkdtemp(prefix="bridge-session-test-")
    api = FakeAPI()
    bridge = Bridge(os.path.abspath(args.bridge_dir), work, api.port, python)
    failed = 0
    try:
        for name in args.scenarios or SCENARIOS:
            ctx = Ctx(api, bridge)
            print(f"\n== {name}", flush=True)
            try:
                SCENARIOS[name](ctx)
            except Exception as e:
                ctx.record("scenario crashed", False, [f"{type(e).__name__}: {e}"])
            for label, ok, why, mode in ctx.results:
                print(f"  {'ok  ' if ok else 'FAIL'} {mode:<8}{label:<52} {'; '.join(why)}", flush=True)
                failed += not ok
    finally:
        bridge.stop()
        api.server.shutdown()
        if args.keep:
            print(f"\nscratch directory: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{failed} failing check(s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
