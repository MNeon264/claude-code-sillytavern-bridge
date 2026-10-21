#!/usr/bin/env python3
"""Offline tests for Character Memory (memory.py and how claude_bridge sends it).

Like tests/session_scenarios.py, a scratch copy of the bridge runs the real
`claude` CLI against a fake Messages API in this process, so nothing reaches
Anthropic. The fake answers the memory calls too: a recap lists the markers
(e.g. AEVT3) of the messages it was given, a short recap starts with SHORT,
and the conflict check reports a conflict when a chat's opening says
STRANGERS. A fake SillyTavern data folder (cards, personas, chats, a group)
sits next to the scratch bridge, where it looks for SillyTavern.

    .venv/bin/python tests/memory_scenarios.py              # every scenario
    .venv/bin/python tests/memory_scenarios.py binding      # some of them
    .venv/bin/python tests/memory_scenarios.py --keep       # keep the scratch dir

Each turn is sent the way the Bridge Memory extension sends it: SillyTavern's
messages plus a `bridge_memory` field naming the chat, card and persona. The
reply is then appended to the chat file, as SillyTavern would save it.
"""
import argparse
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_scenarios import REPO, Bridge, FakeAPI, _text  # noqa: E402

TOKEN = "t0ken" * 8
OLD = "2025-03-01T10:00:00.000Z"
MARK = re.compile(r"\b[A-Z]EVT\d+\b")


def fake_reply(body):
    """Answers for the memory calls; None leaves roleplay turns to FakeAPI."""
    text = "\n".join(_text(m.get("content")) for m in body.get("messages", []) if m.get("role") == "user")
    if "memory of a roleplay. Below is part" in text:
        part = text.split("=== CHAT PART", 1)[1]
        pad = " " + "x" * 5000 if "LONGCHAT" in part else ""
        return "RECAP " + " ".join(dict.fromkeys(MARK.findall(part))) + pad
    if "Condense them into one recap" in text:
        return "SHORT " + " ".join(list(dict.fromkeys(MARK.findall(text)))[:5])
    if "contradicts those memories" in text:
        opening = text.split("=== HOW THE NEW CHAT STARTS ===", 1)[1]
        clash = "STRANGERS" in opening
        return json.dumps({"conflict": clash, "explanation": "They meet as strangers." if clash else "No clash."})
    return None


# --- fake SillyTavern data folder ---------------------------------------------

def _png_with_card(name):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    card = base64.b64encode(json.dumps({"spec": "chara_card_v2", "data": {"name": name}}).encode())
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"tEXt", b"chara\x00" + card)
            + chunk(b"IEND", b""))


class World:
    """SillyTavern's data/default-user next to the scratch bridge."""

    def __init__(self, work):
        self.dir = os.path.join(work, "SillyTavern", "data", "default-user")
        for sub in ("characters", "chats", "groups", "group chats"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            json.dump({"power_user": {"personas": {"p1.png": "V", "p2.png": "V"},
                                      "persona_descriptions": {
                                          "p1.png": {"description": "[{{user}} lives in the city.]", "title": "City"},
                                          "p2.png": {"description": "V of the sea"}}}}, f)
        for name in ("Alice", "Bob", "Mira", "Lena"):
            self.card(name)

    def card(self, name, avatar=None):
        """A card PNG named `avatar` (default: the name) holding `name`."""
        with open(os.path.join(self.dir, "characters", f"{avatar or name}.png"), "wb") as f:
            f.write(_png_with_card(name))

    def chat(self, char, stem, messages, main_chat=None, group=None, integrity=None):
        """Write a chat file. messages: (name, is_user, text, persona or None, date).
        integrity: to share one, as older SillyTavern did for branches."""
        rel = f"group chats/{stem}.jsonl" if group else f"chats/{char}/{stem}.jsonl"
        path = os.path.join(self.dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        meta = {"integrity": integrity or str(uuid.uuid4()), "note_prompt": ""}
        if main_chat:
            meta["main_chat"] = main_chat
        with open(path, "w") as f:
            f.write(json.dumps({"chat_metadata": meta, "user_name": "unused", "character_name": "unused"}) + "\n")
            for m in messages:
                f.write(json.dumps(_msg(*m)) + "\n")
        return Chat(self, char, stem, rel, meta["integrity"], group)

    def group(self, gid, name, members, chats):
        with open(os.path.join(self.dir, "groups", f"{gid}.json"), "w") as f:
            json.dump({"id": gid, "name": name, "members": members, "chats": chats, "chat_id": chats[0]}, f)


def _msg(name, is_user, text, persona=None, date=None):
    m = {"name": name, "is_user": is_user, "is_system": False, "mes": text,
         "send_date": date or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
    if is_user and persona:
        m["force_avatar"] = f"/thumbnail?type=persona&file={persona}"
    if not is_user:
        m["original_avatar"] = f"{name}.png"
    return m


def scripted(n, char, persona, marker, date=OLD, extra=""):
    """n alternating messages, each carrying marker+index (e.g. AEVT7)."""
    return [(("V", True) if i % 2 else (char, False)) + (f"{marker}{i} {extra}line {i} of the scene.",
                                                         persona if i % 2 else None, date) for i in range(n)]


class Chat:
    """A chat file. Its chat id on the bridge is its path in the user folder."""

    def __init__(self, world, char, stem, rel, integrity, group):
        self.world, self.char, self.stem, self.rel, self.integrity, self.group = world, char, stem, rel, integrity, group

    @property
    def chat_id(self):
        return self.rel

    @property
    def path(self):
        return os.path.join(self.world.dir, *self.rel.split("/"))

    def messages(self):
        with open(self.path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        return [m for m in lines if "mes" in m]

    def append(self, name, is_user, text, persona=None):
        with open(self.path, "a") as f:
            f.write(json.dumps(_msg(name, is_user, text, persona)) + "\n")

    def request(self, speaker, persona, first=0, gen_type="normal"):
        """SillyTavern's messages for `speaker`'s reply and the extension's field."""
        msgs = [{"role": "system", "content": f"[SYSTEM PROMPT]\nWrite {speaker}'s next reply in a fictional roleplay."},
                {"role": "system", "content": f'[Character("{speaker}")]\n{speaker} is a character.'}]
        hist = self.messages()
        for m in hist[first:]:
            label = f"{m['name']}: " if self.group and not m["is_user"] else ""
            msgs.append({"role": "user" if m["is_user"] else "assistant", "content": label + m["mes"]})
        info = {"v": "1", "token": TOKEN, "chat_id": self.integrity, "chat_file": self.stem,
                "group_id": self.group or "", "main_chat": "", "char_avatar": f"{speaker}.png", "char_name": speaker,
                "persona": persona, "persona_name": "V", "type": gen_type,
                "first_in_context": first, "chat_length": len(hist)}
        return msgs, {"bridge_memory": info}


# --- harness ------------------------------------------------------------------

class Ctx:
    def __init__(self, api, bridge, world):
        self.api, self.bridge, self.world = api, bridge, world
        self.results = []

    def record(self, label, ok, why=()):
        self.results.append((label, ok, list(why)))

    def check(self, label, cond, detail=""):
        self.record(label, bool(cond), [] if cond else [detail or "check failed"])
        return bool(cond)

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.bridge.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)

    def post(self, path, body, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.bridge.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait_jobs(self, timeout=240):
        """Until the memory worker has nothing running or queued."""
        idle = 0
        end = time.time() + timeout
        while time.time() < end:
            _, j, _ = self.get("/api/memory/jobs")
            idle = idle + 1 if not j.get("current") and not j.get("queued") else 0
            if idle >= 2:
                return True
            time.sleep(0.5)
        self.record("memory jobs finish", False, [f"still busy after {timeout}s"])
        return False

    def turn(self, chat, speaker, persona, first=0, gen_type="normal", user_text=None):
        """Send a turn; append the user's message (if any) before and the
        reply after, as SillyTavern does. Returns what the model saw: (the
        whole view, the turn's last user message)."""
        if user_text:
            chat.append("V", True, user_text, persona)
        msgs, extra = chat.request(speaker, persona, first, gen_type)
        n = self.api.count()
        reply = self.bridge.send(msgs, extra)
        with self.api.lock:
            calls = [b for b in self.api.calls[n:] if "<sillytavern_prompt>" in _text(b.get("system") or "")]
        body = calls[-1] if calls else None
        if gen_type == "normal" and reply:
            chat.append(speaker, False, reply)
        if body is None:
            return "", ""
        view = _text(body.get("system") or "") + "\n" + "\n".join(_text(m["content"]) for m in body["messages"])
        last = next((_text(m["content"]) for m in reversed(body["messages"]) if m["role"] == "user"), "")
        return view, last


def memory_block(text):
    m = re.search(r"<character_memory\b[\s\S]*?</character_memory>", text)
    return m.group(0) if m else ""


# --- scenarios ----------------------------------------------------------------

def no_extension(ctx):
    w = ctx.world
    chat = w.chat("Alice", "plain", [("Alice", False, "Hello there.", None, None)])
    msgs, _ = chat.request("Alice", "p1.png")
    msgs.append({"role": "user", "content": "Hi."})
    n = ctx.api.count()
    reply = ctx.bridge.send(msgs)
    ctx.check("a turn without bridge_memory still gets a reply", "reply" in reply, reply[:80])
    body = ctx.api.last_since(n)
    view = "\n".join(_text(m["content"]) for m in (body or {}).get("messages", []))
    ctx.check("and no memory block", "<character_memory" not in view)
    _, o, _ = ctx.get("/api/memory/overview")
    ctx.check("the Memory tab says the turn had no extension field",
              (o.get("last_turn") or {}).get("skipped") == "no_extension", str(o.get("last_turn")))


def link(ctx):
    w = ctx.world
    old = w.chat("Alice", "Alice - old night", scripted(50, "Alice", "p1.png", "AEVT"))
    new = w.chat("Alice", "Alice - new day", [("Alice", False, "Morning.", None, None)])
    status, _ = ctx.post("/api/memory/link", {"character": "Alice.png", "target": new.chat_id,
                                              "source": old.chat_id, "on": True})
    ctx.check("connect a chat on the Memory tab", status == 200)
    ctx.wait_jobs()
    _, detail, _ = ctx.get(f"/api/memory/chat?character=Alice.png&chat={urllib.request.quote(old.chat_id)}")
    parts = detail.get("parts") or []
    ctx.check("the connected chat is recapped in two parts", len(parts) == 2 and all(p["recap"] for p in parts),
              json.dumps(parts)[:200])
    ctx.check("each part's recap covers its own messages",
              len(parts) == 2 and "AEVT0" in parts[0]["recap"] and "AEVT39" in parts[0]["recap"]
              and "AEVT40" in parts[1]["recap"] and "AEVT40" not in parts[0]["recap"])

    view, last = ctx.turn(new, "Alice", "p1.png", user_text="What do you remember?")
    block = memory_block(last)
    ctx.check("a new session gets the connected chat's recap before the transcript",
              "RECAP AEVT0" in block and "RECAP AEVT40" in block and last.index("<character_memory") < last.index("Human:"),
              last[:300])
    ctx.check("the block names who was there", "Present: Alice, V." in block, block[:300])
    ctx.check("the frame explains the memory block", "<character_memory> before the transcript" in view)

    view, last = ctx.turn(new, "Alice", "p1.png", user_text="And then?")
    ctx.check("a resumed turn doesn't send the memory again",
              "<character_memory" not in last and view.count("RECAP AEVT0") == 1, last[:200])

    for i in range(50, 53):
        old.append("V" if i % 2 else "Alice", bool(i % 2), f"AEVT{i} the old chat went on.", "p1.png" if i % 2 else None)
    view, last = ctx.turn(new, "Alice", "p1.png", user_text="Anything new?")
    ctx.check("while the grown chat's new part is recapped, the session keeps its version",
              "<character_memory_update" not in last, last[:300])
    ctx.wait_jobs()
    view, last = ctx.turn(new, "Alice", "p1.png", user_text="Tell me.")
    upd = re.search(r"<character_memory_update[\s\S]*?</character_memory_update>", last)
    ctx.check("once the source chat has grown and is recapped, a resumed turn gets an update",
              bool(upd) and "AEVT51" in upd.group(0), last[:300])

    status, _ = ctx.post("/api/memory/link", {"character": "Alice.png", "target": new.chat_id,
                                              "source": old.chat_id, "on": False})
    view, last = ctx.turn(new, "Alice", "p1.png", user_text="Forget it.")
    ctx.check("disconnecting sends a note that the memory no longer applies",
              'removed="yes"' in last and "Alice - old night" in last, last[:300])
    view, last = ctx.turn(new, "Alice", "p1.png", user_text="Fine.")
    ctx.check("and only once", "removed=" not in last)


def binding(ctx):
    w = ctx.world
    m1 = w.chat("Mira", "Mira - first", scripted(6, "Mira", "p1.png", "MEVT"))
    w.chat("Mira", "Mira - other V", scripted(6, "Mira", "p2.png", "NEVT"))
    m3 = w.chat("Mira", "Mira - first - Branch #1", scripted(4, "Mira", "p1.png", "MEVT") + [
        ("V", True, "BEVT1 the branch goes elsewhere.", "p1.png", OLD)], main_chat="Mira - first")
    status, _ = ctx.post("/api/memory/binding", {"character": "Mira.png", "persona": "p1.png", "action": "add"})
    ctx.check("bind Mira to persona p1", status == 200)
    ctx.post("/api/memory/binding/chats", {"character": "Mira.png", "persona": "p1.png",
                                           "add": [m1.chat_id, m3.chat_id], "remove": []})
    ctx.wait_jobs()

    m4 = w.chat("Mira", "Mira - later", [("Mira", False, "We meet again, V.", None, None)])
    view, last = ctx.turn(m4, "Mira", "p1.png", user_text="Hello again.")
    block = memory_block(last)
    ctx.check("a new chat with the bound persona remembers the chosen chats",
              "RECAP MEVT0" in block and "BEVT1" in block, block[:300])
    ctx.check("but not a chat with another persona of the same name", "NEVT" not in block)
    _, cv, _ = ctx.get("/api/memory/character?id=Mira.png")
    b = (cv.get("bindings") or [{}])[0]
    ctx.check("the new chat joined the binding by itself", b.get("chats", {}).get(m4.chat_id) == "auto",
              json.dumps(b.get("chats")))

    m5 = w.chat("Mira", "Mira - p2 day", [("Mira", False, "Who are you?", None, None)])
    view, last = ctx.turn(m5, "Mira", "p2.png", user_text="Hi.")
    ctx.check("a chat with an unbound persona gets no memory", "<character_memory" not in last)

    view, last = ctx.turn(m3, "Mira", "p1.png", user_text="In the branch.")
    block = memory_block(last)
    ctx.check("a branch doesn't remember its own parent through the binding", "RECAP MEVT" not in block, block[:300])
    m3b = w.chat("Mira", "Mira - first - Branch #1 - Branch #2", scripted(4, "Mira", "p1.png", "MEVT"),
                 main_chat="Mira - first - Branch #1")
    view, last = ctx.turn(m3b, "Mira", "p1.png", user_text="In the branch of the branch.")
    block = memory_block(last)
    ctx.check("nor does a branch of a branch remember the chats it came from",
              "RECAP MEVT" not in block and "BEVT" not in block, block[:300])

    ctx.post("/api/memory/excluded", {"character": "Mira.png", "chat": m1.chat_id, "on": True})
    view, last = ctx.turn(m4, "Mira", "p1.png", user_text="Still there?")
    ctx.check("keeping a chat out of bindings drops it from a resumed session",
              '<memory source="Mira - first" removed="yes">' in last, last[:300])

    view, last = ctx.turn(m4, "Mira", "p1.png", gen_type="quiet")
    ctx.check("a quiet generation gets no memory", "<character_memory" not in last)


def group(ctx):
    w = ctx.world
    private = w.chat("Bob", "Bob - workshop", scripted(4, "Bob", "p1.png", "WEVT"))
    w.group("g1", "Crew", ["Alice.png", "Bob.png"], ["Crew - deck"])
    gchat = w.chat(None, "Crew - deck", [("Alice", False, "Everyone's here.", None, None)], group="g1")
    ctx.post("/api/memory/link", {"character": "Bob.png", "target": gchat.chat_id, "source": private.chat_id, "on": True})
    ctx.wait_jobs()
    view, last = ctx.turn(gchat, "Bob", "p1.png", user_text="Bob, Alice, hello.")
    block = memory_block(last)
    ctx.check("Bob gets his private chat in the group", "RECAP WEVT0" in block, last[:300])
    ctx.check("and is told the others only know if they were there or told", "only if they were there" in block)
    view, last = ctx.turn(gchat, "Alice", "p1.png")
    ctx.check("Alice doesn't get Bob's memory", "WEVT" not in view, view[-300:])


def conflict(ctx):
    w = ctx.world
    src = w.chat("Mira", "Mira - festival", scripted(4, "Mira", "p1.png", "FEVT"))
    ctx.post("/api/memory/binding", {"character": "Mira.png", "persona": "p1.png", "action": "add"})
    ctx.post("/api/memory/binding/chats", {"character": "Mira.png", "persona": "p1.png", "add": [src.chat_id], "remove": []})
    # Everything the binding brings in is recapped first: a chat whose recap
    # arrives after a decision is checked on its own, and would raise its own alert.
    _, cv, _ = ctx.get("/api/memory/character?id=Mira.png")
    for b in cv.get("bindings", []):
        for chat_id, how in b["chats"].items():
            if how != "removed":
                ctx.post("/api/memory/recap/build", {"character": "Mira.png", "chat": chat_id})
    ctx.wait_jobs()
    chat = w.chat("Mira", "Mira - strangers", [("Mira", False, "STRANGERS: Mira has never met V before.", None, None)])
    ctx.turn(chat, "Mira", "p1.png", user_text="Hello, stranger.")
    ctx.wait_jobs()
    q = urllib.parse.urlencode({"chat_file": chat.stem, "char_avatar": "Mira.png", "char_name": "Mira",
                                "persona": "p1.png", "persona_name": "V"})
    status, _, _ = ctx.get(f"/ext/memory/status?{q}")
    ctx.check("the extension's routes need its token", status == 401, str(status))
    status, st, headers = ctx.get(f"/ext/memory/status?{q}", {"X-Bridge-Memory-Token": TOKEN,
                                                               "Origin": "http://127.0.0.1:8000"})
    ch = (st.get("characters") or [{}])[0]
    ctx.check("with it, they report the conflict", status == 200 and ch.get("check")
              and "strangers" in (ch["check"].get("explanation") or ""), json.dumps(st)[:300])
    ctx.check("and allow SillyTavern's page to read them",
              headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8000", str(headers))
    if not ch.get("check"):
        return
    shown = (ch["check"].get("sources") or [{}])[0]
    ctx.check("the popup names the remembered chat by its card and title",
              [shown.get("name"), shown.get("label")] == ["Mira", "Mira - festival"], json.dumps(shown))
    status, _ = ctx.post(f"/ext/memory/check/{ch['check']['id']}", {"action": "drop", "drop": [src.chat_id]},
                         {"X-Bridge-Memory-Token": TOKEN})
    ctx.check("dropping a memory from the conflict popup", status == 200)
    view, last = ctx.turn(chat, "Mira", "p1.png", user_text="Go on.")
    ctx.check("then leaves it out of that chat", '<memory source="Mira - festival" removed="yes">' in last, last[:300])
    _, st, _ = ctx.get(f"/ext/memory/status?{q}", {"X-Bridge-Memory-Token": TOKEN})
    ch = (st.get("characters") or [{}])[0]
    ctx.check("and the conflict is settled", not ch.get("check") and all(
        s["chat_id"] != src.chat_id for s in ch.get("sources", [])), json.dumps(ch)[:300])
    late = w.chat("Mira", "Mira - rooftop", scripted(4, "Mira", "p1.png", "TEVT"))
    ctx.post("/api/memory/binding/chats", {"character": "Mira.png", "persona": "p1.png", "add": [late.chat_id], "remove": []})
    ctx.wait_jobs()
    ctx.turn(chat, "Mira", "p1.png", user_text="Something new?")
    ctx.wait_jobs()
    _, st, _ = ctx.get(f"/ext/memory/status?{q}", {"X-Bridge-Memory-Token": TOKEN})
    ch = (st.get("characters") or [{}])[0]
    shown = [s["title"] for s in (ch.get("check") or {}).get("sources", [])]
    ctx.check("a chat added after the decision is checked on its own", shown == ["Mira - rooftop"], str(shown))


def recap_calls(ctx, since):
    with ctx.api.lock:
        return sum("memory of a roleplay. Below is part" in _text(m.get("content"))
                   for b in ctx.api.calls[since:] for m in b.get("messages", []) if m.get("role") == "user")


def swipe(chat):
    """Drop the chat's last message (the reply), so the next turn starts a new session."""
    with open(chat.path) as f:
        lines = f.readlines()
    with open(chat.path, "w") as f:
        f.writelines(lines[:-1])


def in_chat(ctx):
    w = ctx.world
    chat = w.chat("Alice", "Alice - long", scripted(90, "Alice", "p1.png", "LEVT"))
    view, last = ctx.turn(chat, "Alice", "p1.png", first=70, user_text="Where were we?")
    ctx.wait_jobs()
    _, detail, _ = ctx.get(f"/api/memory/chat?character=Alice.png&chat={urllib.request.quote(chat.chat_id)}")
    ctx.check("the Memory tab shows the chat's own parts as recapped",
              (detail.get("recap") or {}).get("done") == 2 and [p["recap"] is not None for p in detail.get("parts", [])]
              == [True, True, False], json.dumps(detail.get("recap")))
    swipe(chat)
    view, last = ctx.turn(chat, "Alice", "p1.png", first=70)
    block = memory_block(last)
    ctx.check("a new session gets the recap of the messages before the context",
              'source="earlier in this chat"' in block and "LEVT0" in block and "LEVT69" in block, block[:300])
    ctx.check("up to the end of the part the context starts in",
              "LEVT79" in block and "LEVT80" not in block and "can overlap" in block, block[-300:])
    swipe(chat)
    n = ctx.api.count()
    view, last = ctx.turn(chat, "Alice", "p1.png", first=75)
    ctx.wait_jobs()
    ctx.check("a context start that moves within the part needs no new recap",
              recap_calls(ctx, n) == 0 and "LEVT79" in memory_block(last), f"{recap_calls(ctx, n)} recap call(s)")
    swipe(chat)
    ctx.turn(chat, "Alice", "p1.png", first=85)
    ctx.wait_jobs()
    swipe(chat)
    view, last = ctx.turn(chat, "Alice", "p1.png", first=85)
    block = memory_block(last)
    ctx.check("while that part is unfinished, the recap stops at the context",
              "LEVT84" in block and "LEVT85" not in block and "can overlap" not in block, block[-300:])

    # A resumed session keeps its recap while it's rebuilt, even with none of it ready.
    chat = w.chat("Alice", "Alice - kept", scripted(60, "Alice", "p1.png", "KEVT"))
    ctx.turn(chat, "Alice", "p1.png", first=30, user_text="Hello again.")
    ctx.wait_jobs()
    view, last = ctx.turn(chat, "Alice", "p1.png", user_text="And then?", first=30)
    ctx.check("a resumed session gets the recap once it's built",
              "<character_memory_update" in last and "KEVT0" in last, last[:300])
    with open(chat.path) as f:
        lines = f.readlines()
    lines[1] = lines[1].replace("KEVT0 ", "ZEVT0 rewritten, ")
    with open(chat.path, "w") as f:
        f.writelines(lines)
    view, last = ctx.turn(chat, "Alice", "p1.png", user_text="Still there?", first=30)
    ctx.check("an edit before the context doesn't drop the recap while it's redone",
              "<character_memory_update" not in last and 'removed="yes"' not in last, last[:300])
    ctx.wait_jobs()
    view, last = ctx.turn(chat, "Alice", "p1.png", user_text="Now?", first=30)
    ctx.check("then the redone recap replaces it", "<character_memory_update" in last and "ZEVT0" in last, last[:300])


def budget(ctx):
    w = ctx.world
    ids = []
    for k in range(1, 6):
        c = w.chat("Lena", f"Lena - {k}", scripted(4, "Lena", "p1.png", f"{chr(64 + k)}EVT", extra="LONGCHAT ",
                                                    date=f"2025-0{k}-01T10:00:00.000Z"))
        ids.append(c.chat_id)
    ctx.post("/api/memory/binding", {"character": "Lena.png", "persona": "p1.png", "action": "add"})
    ctx.post("/api/memory/binding/chats", {"character": "Lena.png", "persona": "p1.png", "add": ids, "remove": []})
    ctx.wait_jobs()
    chat = w.chat("Lena", "Lena - now", [("Lena", False, "Hi.", None, None)])
    view, last = ctx.turn(chat, "Lena", "p1.png", user_text="Remember everything?")
    block = memory_block(last)
    sections = re.findall(r'<memory source="([^"]+)"[^>]*>([\s\S]*?)</memory>', block)
    titles = [t for t, _ in sections]
    ctx.check("the bound chats come oldest first", titles == [f"Lena - {k}" for k in range(1, 6)], str(titles))
    if len(sections) == 5:
        ctx.check("the oldest gets its short recap once the full ones fill the budget",
                  "SHORT" in sections[0][1] and "RECAP" in sections[-1][1],
                  str([s[1][:40] for s in sections]))
    ctx.check("the block stays within the budget", len(block) < 26000, str(len(block)))


def dashboard(ctx):
    w = ctx.world
    _, cards, _ = ctx.get("/api/memory/cards")
    names = {c["avatar"]: c["name"] for c in cards.get("cards", [])}
    ctx.check("cards are listed with the names in their PNGs", names.get("Mira.png") == "Mira", str(names))
    _, ps, _ = ctx.get("/api/memory/personas")
    ctx.check("personas are listed by avatar, same names and all",
              [p["avatar"] for p in ps.get("personas", [])] == ["p1.png", "p2.png"], str(ps))

    w.card("Rin")
    w.card("Rin Alt")
    w.chat("Rin", "Rin - one", scripted(4, "Rin", "p1.png", "REVT"))
    alt = w.chat("Rin Alt", "Rin Alt - two", scripted(4, "Rin Alt", "p1.png", "SEVT"))
    target = w.chat("Rin", "Rin - three", [("Rin", False, "Hey.", None, None)])
    status, err = ctx.post("/api/memory/card", {"character": "Rin.png", "avatar": "Rin Alt.png", "action": "add"})
    ctx.check("add another card to a character", status == 200, str(err))
    _, cv, _ = ctx.get("/api/memory/character?id=" + urllib.parse.quote("Rin Alt.png"))
    titles = {c["title"] for c in cv.get("chats", [])}
    ctx.check("the added card's chats are the character's", cv.get("id") == "Rin.png"
              and {"Rin - one", "Rin Alt - two", "Rin - three"} <= titles, str(titles))
    ctx.post("/api/memory/link", {"character": "Rin.png", "target": target.chat_id, "source": alt.chat_id, "on": True})
    ctx.wait_jobs()
    _, d, _ = ctx.get(f"/api/memory/chat?character=Rin.png&chat={urllib.parse.quote(alt.chat_id)}")
    part = (d.get("parts") or [{}])[0]
    ctx.check("the added card's chat is recapped from its own card's point of view", "SEVT0" in (part.get("recap") or ""))
    status, _ = ctx.post("/api/memory/segment", {"key": part.get("key"), "recap": "EDITED: Rin Alt fixed the clock."})
    ctx.check("rewrite a part's recap", status == 200)
    view, last = ctx.turn(target, "Rin", "p1.png", user_text="Remember the clock?")
    ctx.check("the rewritten recap is what the model gets", "EDITED: Rin Alt fixed the clock." in memory_block(last),
              memory_block(last)[:300])
    status, err = ctx.post("/api/memory/card", {"character": "Rin.png", "avatar": "Rin Alt.png", "action": "remove"})
    ctx.check("remove the card again", status == 200, str(err))
    _, cv, _ = ctx.get("/api/memory/character?id=Rin.png")
    ctx.check("its chats leave with it", "Rin Alt - two" not in {c["title"] for c in cv.get("chats", [])})
    _, cv2, _ = ctx.get("/api/memory/character?id=" + urllib.parse.quote("Rin Alt.png"))
    ctx.check("and it's a character of its own", cv2.get("id") == "Rin Alt.png", str(cv2.get("id")))


def chat_names(ctx):
    """The Memory tab names a chat by its card or group, its branch or own
    title and when its file was made, and shows its last message. A persona
    comes with its title and a readable description."""
    w = ctx.world
    _, ps, _ = ctx.get("/api/memory/personas")
    p1 = next((p for p in ps.get("personas", []) if p["avatar"] == "p1.png"), {})
    ctx.check("a persona comes with its title", p1.get("title") == "City", str(p1))
    ctx.check("its description has {{user}} filled in, brackets dropped", p1.get("description") == "V lives in the city.",
              str(p1))

    w.card("Zoe", avatar="Zoe_1")
    main = "Zoe (old card name) - 2025-03-01@10h00m00s"
    shared = [("Zoe", False, "Hello there.", None, "2025-03-01T10:00:00.000Z"),
              ("V", True, "Hi.", "p1.png", "2025-03-01T10:01:00.000Z")]
    w.chat("Zoe_1", main, shared + [("Zoe", False, "<p>*She waves.*</p>\n\n  Bye!", None, "2025-03-02T09:00:00.000Z"),
                                    ("Zoe", False, "", None, "2025-03-02T09:05:00.000Z")])  # a stopped reply
    w.chat("Zoe_1", "Branch #3 - 2025-03-05@12h30m00s", shared, main_chat=main)
    w.chat("Zoe_1", main + " - Branch #1", shared + [("V", True, "Another way.", "p1.png", "2025-04-02T08:00:00.000Z")],
           main_chat=main)
    w.chat("Zoe_1", "useless - 2025-03-07@09h00m00s", shared, main_chat=main)
    w.chat("Zoe_1", "Zoe - 2025-3-9 @14h 29m 44s 406ms", shared)
    w.chat("Zoe_1", "Picnic", [("Zoe", False, "Sandwiches.", None, "2025-05-05T12:00:00.000Z")])
    w.group("gzoe", "Zoe's Club", ["Zoe_1.png"], ["2025-06-01@10h00m00s"])
    w.chat(None, "2025-06-01@10h00m00s", [("Zoe", False, "Welcome.", None, "2025-06-01T10:00:00.000Z")], group="gzoe")
    time.sleep(5.2)  # memory.list_groups() looks at the groups folder again after 5 s
    _, cv, _ = ctx.get("/api/memory/character?id=Zoe_1.png")
    got = {c["rel"].rsplit("/", 1)[-1][:-len(".jsonl")]: c for c in cv.get("chats", [])}
    seen = {stem: [c.get("name"), c.get("label"), c.get("created")] for stem, c in got.items()}
    want = {
        main: ["Zoe", "", "2025-03-01"],                                # the old card name in the file is left out
        "Branch #3 - 2025-03-05@12h30m00s": ["Zoe", "Branch #3", "2025-03-05"],
        main + " - Branch #1": ["Zoe", "Branch #1", "2025-04-02"],      # its name has the parent's date
        "useless - 2025-03-07@09h00m00s": ["Zoe", "useless", "2025-03-07"],
        "Zoe - 2025-3-9 @14h 29m 44s 406ms": ["Zoe", "", "2025-03-09"],
        "Picnic": ["Zoe", "Picnic", "2025-05-05"],
        "2025-06-01@10h00m00s": ["Zoe's Club", "", "2025-06-01"],
    }
    ctx.check("chats: card or group name, branch or own title, date made", seen == want,
              json.dumps(seen, ensure_ascii=False))
    ctx.check("a chat's last message with text comes as plain text",
              got.get(main, {}).get("last_msg") == {"name": "Zoe", "text": "She waves. Bye!"},
              json.dumps(got.get(main, {}).get("last_msg")))
    ctx.check("a branch names the chat it came from",
              got.get("useless - 2025-03-07@09h00m00s", {}).get("parent") == got.get(main, {}).get("chat_id"),
              str(got.get("useless - 2025-03-07@09h00m00s", {}).get("parent")))


def recap_views(ctx):
    """A chat's recap can be read where it's remembered: on the Memory tab,
    and in the extension's list of the memories a chat brings in."""
    w = ctx.world
    w.card("Nia")
    src = w.chat("Nia", "Nia - 2025-03-01@10h00m00s", scripted(4, "Nia", "p1.png", "NEVT"))
    target = w.chat("Nia", "Nia - 2025-03-02@10h00m00s", [("Nia", False, "Hello again.", None, None)])
    ctx.post("/api/memory/link", {"character": "Nia.png", "target": target.chat_id, "source": src.chat_id, "on": True})
    ctx.wait_jobs()
    _, r, _ = ctx.get(f"/api/memory/recap?character=Nia.png&chat={urllib.parse.quote(src.chat_id)}")
    ctx.check("the Memory tab reads one chat's recap", r.get("name") == "Nia"
              and any("NEVT0" in (p.get("recap") or "") for p in r.get("parts", [])), json.dumps(r)[:300])
    status, _, _ = ctx.get("/api/memory/recap?character=Nia.png&chat=" + urllib.parse.quote("chats/Nia/gone.jsonl"))
    ctx.check("a chat that isn't there is a 404", status == 404, str(status))

    ctx.turn(target, "Nia", "p1.png", user_text="Remember?")
    args = {"chat_file": target.stem, "char_avatar": "Nia.png", "char_name": "Nia", "persona": "p1.png",
            "persona_name": "V"}
    _, st, _ = ctx.get(f"/ext/memory/status?{urllib.parse.urlencode({**args, 'recaps': '1'})}",
                       {"X-Bridge-Memory-Token": TOKEN})
    ch = (st.get("characters") or [{}])[0]
    s = (ch.get("sources") or [{}])[0]
    ctx.check("the extension gets the remembered chat's name, date and recap",
              [s.get("name"), s.get("label"), s.get("created")] == ["Nia", "", "2025-03-01"]
              and any("NEVT0" in (p.get("recap") or "") for p in s.get("parts", [])), json.dumps(ch)[:400])
    ctx.check("and this chat's own recap", isinstance(ch.get("own"), dict) and "parts" in ch["own"], json.dumps(ch)[:300])
    _, st, _ = ctx.get(f"/ext/memory/status?{urllib.parse.urlencode(args)}", {"X-Bridge-Memory-Token": TOKEN})
    ch = (st.get("characters") or [{}])[0]
    ctx.check("only when it asks for them", "own" not in ch and all("parts" not in s for s in ch.get("sources", [])),
              json.dumps(ch)[:300])


def identity(ctx):
    """Chats are told apart by file, though older SillyTavern gave branches
    their parent's integrity id; a renamed chat keeps its memory."""
    w = ctx.world
    w.card("Kai")
    shared = str(uuid.uuid4())
    a = w.chat("Kai", "Kai - main", scripted(4, "Kai", "p1.png", "KEVT"), integrity=shared)
    b = w.chat("Kai", "Kai - main - Branch #1", scripted(2, "Kai", "p1.png", "KEVT") + [
        ("V", True, "JEVT1 only in the branch.", "p1.png", OLD)], main_chat="Kai - main", integrity=shared)
    target = w.chat("Kai", "Kai - elsewhere", [("Kai", False, "Hello.", None, None)])
    _, cv, _ = ctx.get("/api/memory/character?id=Kai.png")
    ids = sorted(c["chat_id"] for c in cv.get("chats", []))
    ctx.check("two chats sharing an integrity id are two chats", ids == sorted([a.rel, b.rel, target.rel]), str(ids))
    ctx.post("/api/memory/link", {"character": "Kai.png", "target": target.chat_id, "source": b.chat_id, "on": True})
    ctx.wait_jobs()
    view, last = ctx.turn(target, "Kai", "p1.png", user_text="Remember the branch?")
    ctx.check("the connected branch is the one remembered", "JEVT1" in memory_block(last), memory_block(last)[:300])
    renamed = os.path.join(os.path.dirname(target.path), "Kai - elsewhere (renamed).jsonl")
    os.rename(target.path, renamed)
    target.stem, target.rel = "Kai - elsewhere (renamed)", "chats/Kai/Kai - elsewhere (renamed).jsonl"
    ctx.turn(target, "Kai", "p1.png", user_text="After the rename?")
    _, d, _ = ctx.get(f"/api/memory/chat?character=Kai.png&chat={urllib.parse.quote(target.chat_id)}")
    ctx.check("a renamed chat keeps its connection", [x["chat_id"] for x in d.get("sources", [])] == [b.chat_id],
              json.dumps(d.get("sources")))


SCENARIOS = {f.__name__: f for f in (no_extension, link, binding, group, conflict, in_chat, budget, dashboard,
                                     chat_names, recap_views, identity)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*", metavar="scenario", help=f"any of: {', '.join(SCENARIOS)} (default: all)")
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

    work = tempfile.mkdtemp(prefix="bridge-memory-test-")
    world = World(work)
    api = FakeAPI(reply=fake_reply)
    bridge = Bridge(os.path.abspath(args.bridge_dir), work, api.port, python,
                    settings={"character_memory_enabled": True})
    failed = 0
    try:
        for name in args.scenarios or SCENARIOS:
            ctx = Ctx(api, bridge, world)
            print(f"\n== {name}", flush=True)
            try:
                SCENARIOS[name](ctx)
            except Exception as e:
                ctx.record("scenario crashed", False, [f"{type(e).__name__}: {e}"])
            for label, ok, why in ctx.results:
                print(f"  {'ok  ' if ok else 'FAIL'} {label:<78} {'; '.join(why)}", flush=True)
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
