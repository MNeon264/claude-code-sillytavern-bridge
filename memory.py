"""Character Memory: recaps of what happened in SillyTavern chats, carried
into other chats only where the user asks for them.

A memory belongs to the chat it happened in. The bridge reads SillyTavern's
chat files from its data folder and has Sonnet recap them, part by part, from
one character's point of view. Nothing is recorded per turn.

What a character remembers in a chat (their "sources"):
  - Earlier in the same chat: recaps of the messages SillyTavern no longer
    sends because they don't fit the context.
  - Connected chats (links): chats the user connected to this chat for this
    character on the Memory tab.
  - A persona binding: once a character is bound to a persona, every chat
    the user picks for the binding, plus each new (non-branch) chat with that
    character and persona, is remembered in the other chats with that
    persona. A chat can be left out of bindings everywhere (encounter
    `excluded`), or in one chat (exclusions).
Chats of the same lineage as the current one (its parent, branches of it)
are never brought in through a binding: they are alternate timelines.

Requests identify the chat, persona and speaking card through the
`bridge_memory` field the SillyTavern extension (st-extension/) adds to every
chat completion request. Without it memory skips the turn.

Recaps are cached by the text of the part they recap (SEGMENT_SIZE messages
per part), so a branch shares its parent's recaps up to the branch point and
an edit only invalidates the part it's in. They're built in the background
by one worker thread, when a chat that needs them is used or opened, or when
the user asks on the Memory tab; a turn uses what's built so far.

When a chat with sources starts, Sonnet compares its opening (and Author's
Note) with the memories. A conflict becomes an alert the extension shows in
SillyTavern, where the user keeps the memories or drops some from the chat.

The bridge passes the memory to the model as a <character_memory> block once
per CLI session, then only the parts that changed (see claude_bridge).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from datetime import datetime
from typing import Callable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_ROOT = os.path.join(_THIS_DIR, "character_memory")
DB_PATH = os.path.join(MEMORY_ROOT, "memory.db")

# Chat messages per recapped part. Parts start at fixed message indices, so
# a branch and its parent share every part before the branch point.
SEGMENT_SIZE = 40
# Characters of other chats' recaps per turn. Newest chats first get their
# full recap; when that doesn't fit, the short one; then they're left out.
SOURCE_BUDGET = 24000
# A chat whose recap is longer than this also gets a short one.
SHORT_FROM = 1500
# Most messages of a chat's opening (its greetings up to the user's first
# message) compared with its memories for conflicts.
OPENING_MESSAGES = 6
# Longest message text sent to a recap call.
_MESSAGE_CAP = 12000
RECAP_TIMEOUT = 300
CHECK_TIMEOUT = 120

# Generation types that aren't a character's reply: background prompts from
# extensions, and the user's own message written by the model.
_SKIP_TYPES = {"quiet", "impersonate"}

# ---------------------------------------------------------------------------
# Wiring (set by the bridge)
# ---------------------------------------------------------------------------

_log_fn: Optional[Callable] = None
_claude_exe = os.environ.get("CLAUDE_EXE", "claude")
_call_recorder: Optional[Callable] = None
_st_dir_fn: Optional[Callable[[], Optional[str]]] = None


def configure(log=None, claude_exe=None, call_recorder=None, st_user_dir=None):
    """log(message, level); claude_exe: path to the CLI; call_recorder:
    prompt_history.start_call; st_user_dir: callable returning SillyTavern's
    data/default-user folder or None."""
    global _log_fn, _claude_exe, _call_recorder, _st_dir_fn
    if log:
        _log_fn = log
    if claude_exe:
        _claude_exe = claude_exe
    if call_recorder:
        _call_recorder = call_recorder
    if st_user_dir:
        _st_dir_fn = st_user_dir


def log(msg: str, level: str = "INFO"):
    if _log_fn:
        _log_fn(f"[memory] {msg}", level)
    else:
        print(f"[memory] {level}: {msg}")


def _hash(text: str, n: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- A character is one or more cards (aliases); its id is its first card's avatar.
CREATE TABLE IF NOT EXISTS characters (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards (avatar TEXT PRIMARY KEY, character_id TEXT NOT NULL, name TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cards_character ON cards(character_id);
-- Chat files seen live or scanned on disk. chat_id is the file's path in
-- SillyTavern's user folder (rel): SillyTavern's integrity id isn't unique,
-- as older versions copied it into branches. It's kept to follow a renamed
-- chat. personas: {avatar: {n, name}}. created_ts: when a branch named after
-- its parent was made (_created), else the first message's time; last_msg:
-- {name, text} of its last message, for the Memory tab.
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY, rel TEXT NOT NULL, integrity TEXT, group_id TEXT, group_name TEXT, main_chat TEXT,
    title TEXT, first_ts REAL, last_ts REAL, n INTEGER, personas TEXT, speakers TEXT,
    mtime REAL, size INTEGER, live_ts REAL, created_ts REAL, last_msg TEXT);
CREATE INDEX IF NOT EXISTS chats_rel ON chats(rel);
-- One character's memory of one chat: its short recap and flags. The
-- recaps of its parts are in segments, keyed by the card and the text.
CREATE TABLE IF NOT EXISTS encounters (
    character_id TEXT NOT NULL, chat_id TEXT NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0,
    short TEXT, short_key TEXT, short_edited INTEGER NOT NULL DEFAULT 0,
    built_ts REAL, error TEXT,
    PRIMARY KEY (character_id, chat_id));
-- pov: the card the chat is recapped from (a recap stays valid when cards
-- are added to or removed from a character).
CREATE TABLE IF NOT EXISTS segments (
    key TEXT PRIMARY KEY, pov TEXT NOT NULL, recap TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bindings (
    character_id TEXT NOT NULL, persona TEXT NOT NULL, persona_name TEXT, created_ts REAL NOT NULL,
    PRIMARY KEY (character_id, persona));
-- how: chosen (picked on the Memory tab), auto (a new chat), removed (taken
-- out; never auto-added again).
CREATE TABLE IF NOT EXISTS binding_chats (
    character_id TEXT NOT NULL, persona TEXT NOT NULL, chat_id TEXT NOT NULL,
    how TEXT NOT NULL, added_ts REAL NOT NULL,
    PRIMARY KEY (character_id, persona, chat_id));
CREATE TABLE IF NOT EXISTS links (
    character_id TEXT NOT NULL, target_chat TEXT NOT NULL, source_chat TEXT NOT NULL, created_ts REAL NOT NULL,
    PRIMARY KEY (character_id, target_chat, source_chat));
CREATE TABLE IF NOT EXISTS exclusions (
    character_id TEXT NOT NULL, target_chat TEXT NOT NULL, source_chat TEXT NOT NULL, created_ts REAL NOT NULL,
    PRIMARY KEY (character_id, target_chat, source_chat));
-- The latest conflict check of a chat's opening against its memories.
-- status: clear, open (a conflict to show), kept, applied, failed. sig:
-- hash of the opening; sources: every chat checked so far; shown: the ones
-- the open conflict is about.
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, character_id TEXT NOT NULL, chat_id TEXT NOT NULL,
    sig TEXT NOT NULL, status TEXT NOT NULL, explanation TEXT, sources TEXT, shown TEXT,
    created_ts REAL NOT NULL,
    UNIQUE (character_id, chat_id));
CREATE TABLE IF NOT EXISTS ext_tokens (hash TEXT PRIMARY KEY, last_ts REAL NOT NULL);
"""

_db_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    """The shared connection. Use it with _db_lock held."""
    global _conn
    if _conn is None:
        os.makedirs(MEMORY_ROOT, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(chats)")}
        for col, kind in (("created_ts", "REAL"), ("last_msg", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE chats ADD COLUMN {col} {kind}")
        _conn = conn
    return _conn


def _q(sql: str, args=()) -> list[sqlite3.Row]:
    with _db_lock:
        return _db().execute(sql, args).fetchall()


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    with _db_lock:
        return _db().execute(sql, args).fetchone()


def _x(sql: str, args=()) -> int:
    with _db_lock:
        return _db().execute(sql, args).rowcount


def _meta_set(key: str, value):
    _x("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
       (key, json.dumps(value)))


def _meta_get(key: str, default=None):
    row = _q1("SELECT value FROM meta WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# SillyTavern's data folder
# ---------------------------------------------------------------------------

def st_dir() -> Optional[str]:
    return _st_dir_fn() if _st_dir_fn else None


def _stem(name: str) -> str:
    return name[:-4] if name.lower().endswith(".png") else os.path.splitext(name)[0]


def _safe_rel(rel: str) -> Optional[str]:
    """`rel` if it names a file inside SillyTavern's folder."""
    rel = (rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or any(p in ("", "..") for p in rel.split("/")):
        return None
    return rel


def chat_rel(chat_file: str, group_id: str, card_avatar: str) -> Optional[str]:
    """Path of a chat file relative to SillyTavern's user folder: group chats
    are in "group chats/", a card's chats in "chats/<avatar without .png>/"."""
    if not chat_file or "/" in chat_file or "\\" in chat_file:
        return None
    if group_id:
        return _safe_rel(f"group chats/{chat_file}.jsonl")
    if not card_avatar or "/" in card_avatar:
        return None
    return _safe_rel(f"chats/{_stem(card_avatar)}/{chat_file}.jsonl")


def _abs(rel: str) -> Optional[str]:
    base = st_dir()
    rel = _safe_rel(rel)
    return os.path.join(base, *rel.split("/")) if base and rel else None


_TAG_RE = re.compile(r"<(style|script)\b[\s\S]*?</\1\s*>|</?[A-Za-z][^<>]{0,500}>", re.IGNORECASE)


def _clean(text: str) -> str:
    """Message text for a recap call: HTML styling removed, capped."""
    text = _TAG_RE.sub("", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_MESSAGE_CAP] + (" [...]" if len(text) > _MESSAGE_CAP else "")


def _parse_ts(value) -> Optional[float]:
    """SillyTavern's send_date: ISO, epoch milliseconds, or the old
    "September 10, 2025 1:10pm"."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y %I:%M%p", "%B %d, %Y %I:%M %p", "%Y-%m-%d @%Hh %Mm %Ss %fms", "%Y-%m-%d@%Hh%Mm%Ss"):
        try:
            return datetime.strptime(v, fmt).timestamp()
        except ValueError:
            continue
    return None


def _persona_of(msg: dict) -> str:
    """The persona avatar a user message was sent with ("" if unknown)."""
    fa = msg.get("force_avatar")
    if not isinstance(fa, str) or not fa:
        return ""
    if "file=" in fa:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(fa).query)
        return (q.get("file") or [""])[0]
    if fa.startswith("User Avatars/"):
        return fa.split("/", 1)[1]
    return ""


class ChatFile:
    """A parsed chat file. messages: dicts with i (index in the chat, as
    SillyTavern numbers them), name, user, hidden, text, persona, ts. Its
    chat id is `rel`."""
    __slots__ = ("rel", "integrity", "main_chat", "note", "messages", "mtime", "size", "segs")


_chat_cache: "OrderedDict[str, tuple]" = OrderedDict()
_chat_cache_lock = threading.Lock()


def read_chat(rel: str) -> Optional[ChatFile]:
    """Parse a chat file (cached while its mtime and size are unchanged)."""
    path = _abs(rel)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _chat_cache_lock:
        hit = _chat_cache.get(rel)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            _chat_cache.move_to_end(rel)
            return hit[2]
    cf = ChatFile()
    cf.rel, cf.mtime, cf.size = rel, st.st_mtime, st.st_size
    cf.integrity, cf.main_chat, cf.note, cf.messages, cf.segs = None, None, "", [], {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if n == 0 and "mes" not in obj:
                    meta = obj.get("chat_metadata") if isinstance(obj.get("chat_metadata"), dict) else {}
                    cf.integrity = meta.get("integrity") or None
                    cf.main_chat = meta.get("main_chat") or None
                    cf.note = str(meta.get("note_prompt") or "").strip()
                    continue
                if "mes" not in obj:
                    continue
                cf.messages.append({
                    "i": len(cf.messages),
                    "name": str(obj.get("name") or ""),
                    "user": bool(obj.get("is_user")),
                    "hidden": bool(obj.get("is_system")),
                    "text": str(obj.get("mes") or ""),
                    "persona": _persona_of(obj) if obj.get("is_user") else "",
                    "ts": _parse_ts(obj.get("send_date")),
                })
    except OSError as e:
        log(f"couldn't read {rel}: {e}", "WARN")
        return None
    with _chat_cache_lock:
        _chat_cache[rel] = (cf.mtime, cf.size, cf)
        while len(_chat_cache) > 48:
            _chat_cache.popitem(last=False)
    return cf


_NAME_DATE_RE = re.compile(r"(?:^|\s-\s)(\d{4})-(\d{1,2})-(\d{1,2}) ?@(\d{1,2})h ?(\d{1,2})m ?(\d{1,2})s(?: ?\d{1,3}ms)?$")
_BRANCH_END_RE = re.compile(r" - Branch #(\d+)$")
_BRANCH_START_RE = re.compile(r"^Branch #(\d+) - ")
_PREVIEW_CAP = 300


def _stem_parts(stem: str) -> tuple[str, str, Optional[datetime], bool]:
    """A chat file's name taken apart: (the text before its date, "Branch #N"
    or "", the date, whether the date is the file's own). SillyTavern names
    chats "<card name> - <date>", checkpoints "<name> - <date>", and branches
    "Branch #N - <date>" (older) or "<parent's name> - Branch #N", whose date
    is the parent's. The date is the browser's local time, so it's returned
    naive; older ones read "2025-9-4 @03h 21m 44s 317ms"."""
    s = re.sub(r" imported$", "", stem)
    branch, own = "", True
    m = _BRANCH_END_RE.search(s)
    if m:
        branch, own, s = f"Branch #{m[1]}", False, s[:m.start()]
    m = _BRANCH_START_RE.match(s)
    if m:
        branch, s = branch or f"Branch #{m[1]}", s[m.end():]
    when = None
    m = _NAME_DATE_RE.search(s)
    if m:
        try:
            when = datetime(*(int(g) for g in m.groups()))
        except ValueError:
            pass
        s = s[:m.start()]
    return s.strip(), branch, when, own and when is not None


def _created(cf: ChatFile) -> Optional[float]:
    """When a branch named after its parent was made: when it parted from
    the parent (its first message the parent doesn't have). Other chats have
    the date in their name, or none."""
    _, _, _, own = _stem_parts(os.path.splitext(os.path.basename(cf.rel))[0])
    if own or not cf.main_chat:
        return None
    parent = read_chat(f"{os.path.dirname(cf.rel)}/{cf.main_chat}.jsonl")
    if not parent:
        return None
    pm = parent.messages
    for m in cf.messages:
        p = pm[m["i"]] if m["i"] < len(pm) else None
        if not p or p["text"] != m["text"] or p["ts"] != m["ts"]:
            return m["ts"]
    stamps = [m["ts"] for m in cf.messages if m["ts"]]
    return stamps[-1] if stamps else None


def _last_message(cf: ChatFile) -> dict:
    """The chat's last message with text (an empty one is a reply that
    failed or was stopped), plain, to tell chats and branches apart."""
    for m in reversed(cf.messages):
        text = " ".join(_TAG_RE.sub("", m["text"]).replace("*", "").split())
        if text:
            return {"name": m["name"], "text": text[:_PREVIEW_CAP] + ("…" if len(text) > _PREVIEW_CAP else "")}
    return {}


def _summarize(cf: ChatFile, group: Optional[dict]) -> dict:
    """The chats-table row for a parsed chat file."""
    personas: dict = {}
    speakers: list = []
    for m in cf.messages:
        if m["hidden"]:
            continue
        if m["user"]:
            p = personas.setdefault(m["persona"], {"n": 0, "name": m["name"]})
            p["n"] += 1
            p["name"] = m["name"] or p["name"]
        if m["name"] and m["name"] not in speakers:
            speakers.append(m["name"])
    stem = os.path.splitext(os.path.basename(cf.rel))[0]
    stamps = [m["ts"] for m in cf.messages if m["ts"]]
    return {
        "chat_id": cf.rel, "rel": cf.rel, "integrity": cf.integrity,
        "group_id": group.get("id") if group else None,
        "group_name": group.get("name") if group else None,
        "main_chat": cf.main_chat,
        "title": f"{group.get('name') or 'Group'}: {stem}" if group else stem,
        "first_ts": stamps[0] if stamps else cf.mtime,
        "last_ts": stamps[-1] if stamps else cf.mtime,
        "n": len(cf.messages),
        "personas": json.dumps(personas, ensure_ascii=False),
        "speakers": json.dumps(speakers[:30], ensure_ascii=False),
        "mtime": cf.mtime, "size": cf.size,
        "created_ts": _created(cf) or (stamps[0] if stamps else cf.mtime),
        "last_msg": json.dumps(_last_message(cf), ensure_ascii=False),
    }


_CHAT_COLS = ("chat_id", "rel", "integrity", "group_id", "group_name", "main_chat", "title", "first_ts", "last_ts",
              "n", "personas", "speakers", "mtime", "size", "created_ts", "last_msg")


# Old chat id -> new, for chats renamed while the bridge runs (get_chat).
_renamed: dict = {}


def _store_chat(row: dict):
    """Insert or refresh a chats row. A file new to the bridge whose
    integrity id belongs to exactly one known chat whose file is gone is
    that chat renamed, and takes over its memory."""
    with _db_lock:
        known = _q1("SELECT 1 FROM chats WHERE chat_id = ?", (row["chat_id"],))
        if not known and row.get("integrity"):
            gone = [r["chat_id"] for r in _q("SELECT chat_id, rel FROM chats WHERE integrity = ?", (row["integrity"],))
                    if not os.path.exists(_abs(r["rel"]) or "")]
            if len(gone) == 1:
                _rename_chat(gone[0], row["chat_id"])
        cols = ", ".join(_CHAT_COLS)
        marks = ", ".join("?" * len(_CHAT_COLS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in _CHAT_COLS if c != "chat_id")
        _x(f"INSERT INTO chats ({cols}) VALUES ({marks}) ON CONFLICT(chat_id) DO UPDATE SET {updates}",
           tuple(row[c] for c in _CHAT_COLS))


def _rename_chat(old: str, new: str):
    with _db_lock:
        conn = _db()
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM chats WHERE chat_id = ?", (old,))
            for table, cols in (("encounters", ("chat_id",)), ("binding_chats", ("chat_id",)),
                                ("links", ("target_chat", "source_chat")),
                                ("exclusions", ("target_chat", "source_chat")), ("checks", ("chat_id",))):
                for col in cols:
                    conn.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?", (new, old))
                    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (old,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    _renamed[old] = new
    log(f"chat {old} was renamed to {new}")


def scan_chat(rel: str, group: Optional[dict] = None) -> Optional[dict]:
    """The chats row for a file, re-read only when it changed."""
    path = _abs(rel)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (rel,))
    if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size and row["last_msg"] is not None:
        return dict(row)
    cf = read_chat(rel)
    if not cf:
        return None
    if group is None and rel.startswith("group chats/"):
        stem = os.path.splitext(os.path.basename(rel))[0]
        group = next((g for g in list_groups() if stem in g["chats"]), None)
    data = _summarize(cf, group)
    _store_chat(data)
    return dict(_q1("SELECT * FROM chats WHERE chat_id = ?", (data["chat_id"],)))


_missing: dict = {}  # chat_id -> when a rescan last failed to find it


def get_chat(chat_id: str, rescan_for: Optional[str] = None) -> Optional[dict]:
    """The chats row for an id, refreshed from its file. When the file has
    moved (renamed chat), the character `rescan_for`'s chats are scanned to
    find it again, at most every 10 minutes per chat."""
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    if row:
        fresh = scan_chat(row["rel"])
        if fresh and fresh["chat_id"] == chat_id:
            return fresh
    if rescan_for and time.time() - _missing.get(chat_id, 0) > 600:
        chats = character_chats(rescan_for)
        new_id = _renamed.get(chat_id, chat_id)
        for c in chats:
            if c["chat_id"] == new_id:
                _missing.pop(chat_id, None)
                return c
        _missing[chat_id] = time.time()
    return dict(row) if row else None


_group_cache: dict = {"mtime": None, "groups": [], "checked": 0.0}


def list_groups() -> list[dict]:
    """SillyTavern's groups: id, name, members (card avatars), chats (file
    stems). Re-checked on disk at most every few seconds: listing a
    character's chats asks for every one of their group chats."""
    base = st_dir()
    if not base:
        return []
    if time.time() - _group_cache["checked"] < 5:
        return _group_cache["groups"]
    _group_cache["checked"] = time.time()
    gdir = os.path.join(base, "groups")
    try:
        names = sorted(os.listdir(gdir))
        stamp = (os.stat(gdir).st_mtime, tuple((n, os.stat(os.path.join(gdir, n)).st_mtime) for n in names))
    except OSError:
        return []
    if _group_cache["mtime"] == stamp:
        return _group_cache["groups"]
    groups = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(gdir, n), "r", encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(g, dict) and g.get("id"):
            groups.append({
                "id": str(g["id"]), "name": str(g.get("name") or ""),
                "members": [str(m) for m in g.get("members") or []],
                "chats": [str(c) for c in g.get("chats") or []],
            })
    _group_cache.update(mtime=stamp, groups=groups)
    return groups


def _group(group_id: str) -> Optional[dict]:
    return next((g for g in list_groups() if g["id"] == str(group_id)), None)


_card_cache: dict = {}  # avatar -> (mtime, name)


def _card_name(path: str) -> Optional[str]:
    """The character name stored in a card PNG's chara/ccv3 text chunk."""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                length, ctype = struct.unpack(">I4s", head)
                if ctype == b"tEXt":
                    key, _, val = f.read(length).partition(b"\0")
                    f.seek(4, 1)
                    if key in (b"chara", b"ccv3"):
                        try:
                            card = json.loads(base64.b64decode(val))
                            name = ((card.get("data") or card).get("name") or "").strip()
                            if name:
                                return name
                        except (ValueError, AttributeError):
                            continue
                elif ctype == b"IEND":
                    return None
                else:
                    f.seek(length + 4, 1)
    except OSError:
        return None


def list_cards() -> list[dict]:
    """Every character card in SillyTavern: avatar and name."""
    base = st_dir()
    if not base:
        return []
    cdir = os.path.join(base, "characters")
    try:
        names = os.listdir(cdir)
    except OSError:
        return []
    out = []
    for n in names:
        if not n.lower().endswith(".png"):
            continue
        p = os.path.join(cdir, n)
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            continue
        hit = _card_cache.get(n)
        if not hit or hit[0] != mtime:
            hit = (mtime, _card_name(p) or _stem(n))
            _card_cache[n] = hit
        out.append({"avatar": n, "name": hit[1]})
    out.sort(key=lambda c: (c["name"].lower(), c["avatar"]))
    return out


def list_personas() -> list[dict]:
    """SillyTavern's personas: avatar, name, title, the start of the
    description with {{user}} filled in and its brackets dropped."""
    base = st_dir()
    if not base:
        return []
    try:
        with open(os.path.join(base, "settings.json"), "r", encoding="utf-8") as f:
            pu = (json.load(f).get("power_user") or {})
    except (OSError, ValueError):
        return []
    descs = pu.get("persona_descriptions") or {}
    out = []
    for avatar, name in (pu.get("personas") or {}).items():
        d = descs.get(avatar) if isinstance(descs.get(avatar), dict) else {}
        name = str(name or "")
        desc = " ".join(str(d.get("description") or "").split())
        if desc.startswith("[") and desc.endswith("]") and "[" not in desc[1:] and "]" not in desc[:-1]:
            desc = desc[1:-1].strip()
        desc = re.sub(r"\{\{user\}\}", lambda _: name or "{{user}}", desc, flags=re.IGNORECASE)
        out.append({"avatar": avatar, "name": name, "title": str(d.get("title") or "").strip(),
                    "description": desc[:_PREVIEW_CAP] + ("…" if len(desc) > _PREVIEW_CAP else "")})
    out.sort(key=lambda p: (p["name"].lower(), p["avatar"]))
    return out


def _persona_name(avatar: str) -> str:
    return next((p["name"] for p in list_personas() if p["avatar"] == avatar), "") or _stem(avatar)


# ---------------------------------------------------------------------------
# Characters and cards
# ---------------------------------------------------------------------------

def ensure_card(avatar: str, name: str = "") -> str:
    """The character a card belongs to, creating both on first sight."""
    name = (name or "").strip() or _stem(avatar)
    with _db_lock:
        row = _q1("SELECT character_id, name FROM cards WHERE avatar = ?", (avatar,))
        if row:
            if name != row["name"]:
                _x("UPDATE cards SET name = ? WHERE avatar = ?", (name, avatar))
                _x("UPDATE characters SET name = ? WHERE id = ? AND id = ?", (name, row["character_id"], avatar))
            return row["character_id"]
        _x("INSERT OR IGNORE INTO characters (id, name, created_ts) VALUES (?, ?, ?)", (avatar, name, time.time()))
        _x("INSERT INTO cards (avatar, character_id, name) VALUES (?, ?, ?)", (avatar, avatar, name))
        return avatar


def character_cards(cid: str) -> list[dict]:
    return [dict(r) for r in _q("SELECT avatar, name FROM cards WHERE character_id = ? ORDER BY avatar = ? DESC, avatar",
                                (cid, cid))]


def character_name(cid: str) -> str:
    row = _q1("SELECT name FROM characters WHERE id = ?", (cid,))
    return row["name"] if row else _stem(cid)


def character_chats(cid: str) -> list[dict]:
    """Every chat of the character's cards on disk: their own chats and the
    group chats of groups they're in."""
    base = st_dir()
    if not base:
        return []
    cards = [c["avatar"] for c in character_cards(cid)] or [cid]
    rows, seen = [], set()
    for avatar in cards:
        folder = os.path.join(base, "chats", _stem(avatar))
        try:
            files = sorted(f for f in os.listdir(folder) if f.endswith(".jsonl"))
        except OSError:
            files = []
        for f in files:
            row = scan_chat(f"chats/{_stem(avatar)}/{f}")
            if row and row["chat_id"] not in seen:
                seen.add(row["chat_id"])
                rows.append(row)
    for g in list_groups():
        if not any(a in g["members"] for a in cards):
            continue
        for stem in g["chats"]:
            row = scan_chat(f"group chats/{stem}.jsonl", g)
            if row and row["chat_id"] not in seen:
                seen.add(row["chat_id"])
                rows.append(row)
    return rows


def _move_character_rows(old: str, new: str, chat_ids: Optional[set] = None):
    """Move a character's memory rows to another character: all of them, or
    those of the chats in `chat_ids`."""
    def cond(col):
        if chat_ids is None:
            return "", ()
        ids = list(chat_ids) or [""]
        return f" AND {col} IN ({','.join('?' * len(ids))})", tuple(ids)

    with _db_lock:
        conn = _db()
        conn.execute("BEGIN")
        try:
            for table, col in (("encounters", "chat_id"), ("links", "target_chat"),
                               ("exclusions", "target_chat"), ("checks", "chat_id")):
                extra, args = cond(col)
                conn.execute(f"UPDATE OR IGNORE {table} SET character_id = ? WHERE character_id = ?{extra}",
                             (new, old) + args)
                conn.execute(f"DELETE FROM {table} WHERE character_id = ?{extra}", (old,) + args)
            if chat_ids is None:
                conn.execute("UPDATE OR IGNORE bindings SET character_id = ? WHERE character_id = ?", (new, old))
                conn.execute("DELETE FROM bindings WHERE character_id = ?", (old,))
                conn.execute("UPDATE OR IGNORE binding_chats SET character_id = ? WHERE character_id = ?", (new, old))
                conn.execute("DELETE FROM binding_chats WHERE character_id = ?", (old,))
            else:
                extra, args = cond("chat_id")
                conn.execute(f"DELETE FROM binding_chats WHERE character_id = ?{extra}", (old,) + args)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def add_card(cid: str, avatar: str) -> tuple[bool, str]:
    """Make another card part of this character (the same person on several
    cards). Its memory moves over. Only a card that is a character on its
    own can be added."""
    if avatar == cid:
        return False, "that card is this character"
    names = {c["avatar"]: c["name"] for c in list_cards()}
    if avatar not in names:
        return False, "no such card in SillyTavern"
    if not _q1("SELECT 1 FROM characters WHERE id = ?", (cid,)):
        if cid not in names:
            return False, "no such character"
        cid = ensure_card(cid, names[cid])
    old = ensure_card(avatar, names[avatar])
    if old == cid:
        return False, "that card is already part of this character"
    if len(character_cards(old)) > 1:
        return False, "that card belongs to a character with several cards; remove it there first"
    _move_character_rows(old, cid)
    with _db_lock:
        _x("UPDATE cards SET character_id = ? WHERE avatar = ?", (cid, avatar))
        _x("DELETE FROM characters WHERE id = ?", (old,))
    log(f"card {avatar} is now part of {character_name(cid)}")
    return True, ""


def remove_card(avatar: str) -> tuple[bool, str]:
    """Make a card a character of its own again. The memory of its chats
    goes with it; it leaves the persona bindings of the character."""
    row = _q1("SELECT character_id, name FROM cards WHERE avatar = ?", (avatar,))
    if not row:
        return False, "unknown card"
    cid = row["character_id"]
    if cid == avatar:
        return False, "the character's first card can't be removed"
    folder_chats = set()
    for c in character_chats(cid):
        if c["rel"].startswith(f"chats/{_stem(avatar)}/"):
            folder_chats.add(c["chat_id"])
        elif c.get("group_id"):
            g = _group(c["group_id"])
            if g and avatar in g["members"] and not any(
                    a["avatar"] in g["members"] for a in character_cards(cid) if a["avatar"] != avatar):
                folder_chats.add(c["chat_id"])
    with _db_lock:
        _x("INSERT OR IGNORE INTO characters (id, name, created_ts) VALUES (?, ?, ?)", (avatar, row["name"], time.time()))
        _x("UPDATE cards SET character_id = ? WHERE avatar = ?", (avatar, avatar))
    _move_character_rows(cid, avatar, folder_chats)
    return True, ""


# ---------------------------------------------------------------------------
# Recaps
# ---------------------------------------------------------------------------

def _pov(cid: str, row: dict) -> tuple[str, str]:
    """(avatar, name) of the character's card in a chat: the card whose
    folder it's in, or the character's card among the group's members."""
    cards = character_cards(cid) or [{"avatar": cid, "name": character_name(cid)}]
    for c in cards:
        if row["rel"].startswith(f"chats/{_stem(c['avatar'])}/"):
            return c["avatar"], c["name"]
    g = _group(row["group_id"]) if row.get("group_id") else None
    for c in cards:
        if g and c["avatar"] in g["members"]:
            return c["avatar"], c["name"]
    return cards[0]["avatar"], cards[0]["name"]


def _segments(cf: ChatFile, pov: str, upto: Optional[int] = None) -> list[dict]:
    """The chat's parts: SEGMENT_SIZE messages each from the start, cut at
    `upto` (a message index) when given. Hidden messages are left out of the
    text; a part with no visible message is skipped. Keyed by the card
    they're recapped from and their text."""
    cache_key = (pov, upto)
    if cache_key in cf.segs:
        return cf.segs[cache_key]
    n = len(cf.messages) if upto is None else max(0, min(upto, len(cf.messages)))
    out = []
    for start in range(0, n, SEGMENT_SIZE):
        end = min(start + SEGMENT_SIZE, n)
        part = [m for m in cf.messages[start:end] if not m["hidden"] and m["text"].strip()]
        if not part:
            continue
        text = "\n\n".join(f"{m['name'] or ('User' if m['user'] else 'Character')}: {_clean(m['text'])}"
                           for m in part)
        out.append({"start": start, "end": end, "text": text,
                    "key": _hash(f"{pov}\n{text}"), "full": end - start == SEGMENT_SIZE})
    cf.segs[cache_key] = out
    return out


def _segment_recaps(keys: list[str]) -> dict:
    if not keys:
        return {}
    rows = []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        rows += _q(f"SELECT key, recap, edited FROM segments WHERE key IN ({','.join('?' * len(chunk))})", chunk)
    return {r["key"]: dict(r) for r in rows}


def _encounter(cid: str, chat_id: str) -> dict:
    row = _q1("SELECT * FROM encounters WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    return dict(row) if row else {"character_id": cid, "chat_id": chat_id, "excluded": 0,
                                  "short": None, "short_key": None, "short_edited": 0}


def _ensure_encounter(cid: str, chat_id: str):
    _x("INSERT OR IGNORE INTO encounters (character_id, chat_id) VALUES (?, ?)", (cid, chat_id))


_keys_cache: "OrderedDict[tuple, list]" = OrderedDict()


def recap_state(cid: str, chat_id: str, upto: Optional[int] = None, row: Optional[dict] = None) -> dict:
    """How much of a chat is recapped for a character: parts, done, missing.
    The parts' keys are cached per version of the file, so listing a
    character's chats doesn't parse every file again."""
    row = row or get_chat(chat_id, rescan_for=cid)
    if not row:
        return {"parts": 0, "done": 0, "missing": 0, "available": False}
    pov = _pov(cid, row)[0]
    ck = (row["rel"], row.get("mtime"), row.get("size"), pov, upto)
    keys = _keys_cache.get(ck)
    if keys is None:
        cf = read_chat(row["rel"])
        if not cf:
            return {"parts": 0, "done": 0, "missing": 0, "available": False}
        keys = [s["key"] for s in _segments(cf, pov, upto)]
        _keys_cache[ck] = keys
        while len(_keys_cache) > 5000:
            _keys_cache.popitem(last=False)
    have = _segment_recaps(keys)
    done = sum(1 for k in keys if k in have)
    return {"parts": len(keys), "done": done, "missing": len(keys) - done, "available": True}


def _chat_people(cf: ChatFile, char_name: str) -> tuple[str, list[str]]:
    """(the user's name in the chat, the other speakers)."""
    users = [m["name"] for m in cf.messages if m["user"] and not m["hidden"] and m["name"]]
    user = max(set(users), key=users.count) if users else "the user"
    others = []
    for m in cf.messages:
        if not m["user"] and not m["hidden"] and m["name"] and m["name"] != char_name and m["name"] not in others:
            others.append(m["name"])
    return user, others


_RECAP_PROMPT = """You keep {char}'s memory of a roleplay. Below is part {part} of {total} of a chat{where}. {user} is the user's character.{others}
{before}
Write a recap of this part as {char} would remember it:
- What happened, in order: what {char} did, saw and learned; who was there; what was said that matters; promises, revelations, decisions and how things stand at the end of the part.
- Past tense, third person, plain prose, at most {words} words. Use names, not "the user".
- Only events. Don't describe anyone's looks or personality, and don't restate the setting or world lore unless something happened to it.
- Leave out what {char} wasn't there for and couldn't know, when the text makes that clear.
- Write only the recap: no heading, no preamble, no commentary.

=== CHAT PART {part} ===
{text}
=== END OF PART ===
"""

_SHORT_PROMPT = """Below are recaps of the consecutive parts of a roleplay chat, from {char}'s point of view. {user} is the user's character.

Condense them into one recap of the whole chat in at most 150 words: the key events, revelations and promises, and how things stood at the end. Past tense, third person, names rather than "the user". Write only the recap.

{recaps}
"""


def _build_recaps(job: dict) -> None:
    """Recap every missing part of a chat for a character, then the short
    recap when the chat is recapped whole."""
    cid, chat_id, upto = job["cid"], job["chat_id"], job.get("upto")
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if not cf:
        job["error"] = "chat file not found"
        return
    pov, char = _pov(cid, row)
    user, others = _chat_people(cf, char)
    segs = _segments(cf, pov, upto)
    have = _segment_recaps([s["key"] for s in segs])
    missing = [s for s in segs if s["key"] not in have]
    job["total"], job["done"] = len(missing), 0
    _ensure_encounter(cid, chat_id)
    prev = None
    for idx, s in enumerate(segs):
        if job.get("cancel"):
            return
        if s["key"] in have:
            prev = have[s["key"]]["recap"]
            continue
        before = f"\nWhat happened before this part, as recapped earlier:\n{prev}\n" if prev else ""
        prompt = _RECAP_PROMPT.format(
            char=char, part=idx + 1, total=len(segs), user=user,
            where=f' ("{row["title"]}")' if row.get("title") else "",
            others=(f" Others in the chat: {', '.join(others[:12])}." if others else ""),
            before=before, words=250 if s["full"] else 180, text=s["text"])
        recap = _call_sonnet(prompt, timeout=RECAP_TIMEOUT,
                             label=f"Character Memory: recap {char} — {row['title']} ({idx + 1}/{len(segs)})")
        if not recap or not recap.strip():
            job["error"] = "the recap call failed (see the History tab)"
            _x("UPDATE encounters SET error = ? WHERE character_id = ? AND chat_id = ?", (job["error"], cid, chat_id))
            return
        recap = recap.strip()
        _x("INSERT OR REPLACE INTO segments (key, pov, recap, edited, created_ts) VALUES (?, ?, ?, 0, ?)",
           (s["key"], pov, recap, time.time()))
        have[s["key"]] = {"recap": recap, "edited": 0}
        prev = recap
        job["done"] += 1
    _x("UPDATE encounters SET built_ts = ?, error = NULL WHERE character_id = ? AND chat_id = ?",
       (time.time(), cid, chat_id))
    if upto is None and not job.get("cancel"):
        _ensure_short(cid, chat_id, [have[s["key"]]["recap"] for s in segs if s["key"] in have], char, user)


def _ensure_short(cid: str, chat_id: str, recaps: list[str], char: str, user: str):
    full = "\n\n".join(recaps)
    if len(full) <= SHORT_FROM:
        return
    key = _hash(full)
    enc = _encounter(cid, chat_id)
    if enc.get("short_edited") or enc.get("short_key") == key:
        return
    numbered = "\n\n".join(f"Part {i + 1}:\n{r}" for i, r in enumerate(recaps))
    short = _call_sonnet(_SHORT_PROMPT.format(char=char, user=user, recaps=numbered), timeout=RECAP_TIMEOUT,
                         label=f"Character Memory: short recap {char}")
    if short and short.strip():
        _x("UPDATE encounters SET short = ?, short_key = ? WHERE character_id = ? AND chat_id = ?",
           (short.strip(), key, cid, chat_id))


def _full_recap(pov: str, cf: ChatFile, skip_keys: set) -> tuple[str, bool, list[str]]:
    """(text of the recapped parts not in skip_keys, whether every part is
    recapped, the keys used)."""
    segs = _segments(cf, pov)
    have = _segment_recaps([s["key"] for s in segs])
    texts, keys = [], []
    for s in segs:
        if s["key"] in skip_keys or s["key"] not in have:
            continue
        texts.append(have[s["key"]]["recap"])
        keys.append(s["key"])
    return "\n\n".join(texts), all(s["key"] in have for s in segs), keys


def set_segment_recap(key: str, recap: Optional[str]) -> bool:
    """Rewrite one part's recap (None: delete it, so it's recapped again)."""
    if recap is None:
        return _x("DELETE FROM segments WHERE key = ?", (key,)) > 0
    return _x("UPDATE segments SET recap = ?, edited = 1 WHERE key = ?", (recap.strip(), key)) > 0


def set_short_recap(cid: str, chat_id: str, text: Optional[str]):
    _ensure_encounter(cid, chat_id)
    if text is None or not text.strip():
        _x("UPDATE encounters SET short = NULL, short_key = NULL, short_edited = 0 WHERE character_id = ? AND chat_id = ?",
           (cid, chat_id))
    else:
        _x("UPDATE encounters SET short = ?, short_edited = 1 WHERE character_id = ? AND chat_id = ?",
           (text.strip(), cid, chat_id))


def clear_recap(cid: str, chat_id: str):
    """Forget a chat's recaps for a character (they're built again when needed)."""
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if cf:
        keys = [s["key"] for s in _segments(cf, _pov(cid, row)[0])]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            _x(f"DELETE FROM segments WHERE key IN ({','.join('?' * len(chunk))})", chunk)
    _x("UPDATE encounters SET short = NULL, short_key = NULL, short_edited = 0, built_ts = NULL, error = NULL "
       "WHERE character_id = ? AND chat_id = ?", (cid, chat_id))


# ---------------------------------------------------------------------------
# Sonnet calls
# ---------------------------------------------------------------------------

def _call_sonnet(prompt: str, timeout: int = 120, label: str = "Character Memory") -> Optional[str]:
    """One `claude -p --model sonnet` call; the reply text, or None."""
    cmd = [
        _claude_exe, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", "sonnet", "--effort", "low",
        # No tools, and no CLAUDE.md files from the bridge directory or its parents.
        "--tools", "",
        "--setting-sources", "user",
    ]
    record = _call_recorder("memory", label, argv=cmd, prompt=prompt) if _call_recorder else None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
    except FileNotFoundError:
        log(f"claude CLI not found at {_claude_exe!r}", "ERROR")
        if record:
            record.finish(status="error", error=f"claude CLI not found at {_claude_exe!r}")
        return None
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        log(f"{label}: timed out after {timeout}s", "WARN")
        if record:
            record.finish(status="error", error=f"timed out after {timeout}s")
        return None
    if record:
        record.observe_stdout(stdout, proc.returncode, stderr)
        record.finish()
    text = ""
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                return None
            text = event.get("result", "") or text
            break
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "") or text
    return text or None


def _extract_json(text: str) -> Optional[dict]:
    """The first JSON object in a reply."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

class _Jobs:
    """One worker thread running recap builds and conflict checks in order."""

    def __init__(self):
        self.cv = threading.Condition()
        self.queue: deque = deque()
        self.current: Optional[dict] = None
        self.recent: deque = deque(maxlen=20)
        self.thread: Optional[threading.Thread] = None

    def submit(self, job: dict) -> bool:
        with self.cv:
            if (self.current and self.current["key"] == job["key"]) or any(j["key"] == job["key"] for j in self.queue):
                return False
            job.setdefault("queued_ts", time.time())
            self.queue.append(job)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="memory-jobs")
                self.thread.start()
            self.cv.notify()
            return True

    def cancel(self, cid: Optional[str] = None, chat_id: Optional[str] = None) -> int:
        def match(j):
            return (cid is None or j.get("cid") == cid) and (chat_id is None or j.get("chat_id") == chat_id)
        with self.cv:
            n = len(self.queue)
            self.queue = deque(j for j in self.queue if not match(j))
            n -= len(self.queue)
            if self.current and match(self.current):
                self.current["cancel"] = True
                n += 1
            return n

    def pending(self, cid: str, chat_id: str) -> Optional[dict]:
        with self.cv:
            for j in ([self.current] if self.current else []) + list(self.queue):
                if j.get("cid") == cid and j.get("chat_id") == chat_id and j["kind"] == "recap":
                    return dict(j)
        return None

    def status(self) -> dict:
        def view(j):
            return {k: j.get(k) for k in ("kind", "cid", "chat_id", "title", "done", "total", "error", "upto")}
        with self.cv:
            return {"current": view(self.current) if self.current else None,
                    "queued": [view(j) for j in self.queue],
                    "recent": [view(j) for j in self.recent]}

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                self.current = self.queue.popleft()
            job = self.current
            try:
                if job["kind"] == "recap":
                    _build_recaps(job)
                elif job["kind"] == "check":
                    _run_check(job)
            except Exception as e:
                job["error"] = f"{type(e).__name__}: {e}"
                log(f"{job['kind']} job failed: {job['error']}", "ERROR")
            with self.cv:
                self.recent.appendleft(job)
                self.current = None


_jobs = _Jobs()


def schedule_recap(cid: str, chat_id: str, upto: Optional[int] = None, title: str = "") -> bool:
    return _jobs.submit({"kind": "recap", "cid": cid, "chat_id": chat_id, "upto": upto, "title": title,
                         "key": f"recap\0{cid}\0{chat_id}\0{upto}"})


def cancel_jobs(cid: Optional[str] = None, chat_id: Optional[str] = None) -> int:
    return _jobs.cancel(cid, chat_id)


def jobs_status() -> dict:
    st = _jobs.status()
    names = {}
    for j in [st["current"]] + st["queued"] + st["recent"]:
        if j and j.get("cid"):
            j["character"] = names.setdefault(j["cid"], character_name(j["cid"]))
    return st


# ---------------------------------------------------------------------------
# Sources: what a character remembers in a chat
# ---------------------------------------------------------------------------

def _lineage(row: dict) -> tuple:
    """Chats with the same lineage are a chat and its branches, theirs
    included: same folder or group, same root chat. A branch's main_chat
    names the chat it was made from, so the chain is followed up."""
    folder = os.path.dirname(row["rel"])
    stem = os.path.splitext(os.path.basename(row["rel"]))[0]
    parent, seen = row.get("main_chat"), {stem}
    while parent and parent not in seen and len(seen) < 30:
        seen.add(parent)
        stem = parent
        prow = scan_chat(f"{folder}/{parent}.jsonl")
        parent = prow.get("main_chat") if prow else None
    return row.get("group_id") or folder, stem


def binding(cid: str, persona: str) -> Optional[dict]:
    row = _q1("SELECT * FROM bindings WHERE character_id = ? AND persona = ?", (cid, persona)) if persona else None
    return dict(row) if row else None


def sources(cid: str, chat_id: str, persona: str, chat_row: Optional[dict] = None) -> list[dict]:
    """The other chats a character remembers in this chat: [{chat_id, how}],
    how being "link" or "binding"."""
    links = {r["source_chat"] for r in _q(
        "SELECT source_chat FROM links WHERE character_id = ? AND target_chat = ?", (cid, chat_id))}
    bound = set()
    if binding(cid, persona):
        bound = {r["chat_id"] for r in _q(
            "SELECT chat_id FROM binding_chats WHERE character_id = ? AND persona = ? AND how != 'removed'",
            (cid, persona))}
        bound -= {r["chat_id"] for r in _q(
            "SELECT chat_id FROM encounters WHERE character_id = ? AND excluded = 1", (cid,))}
        if chat_row:
            mine = _lineage(chat_row)
            for other in list(bound):
                r = _q1("SELECT rel, group_id, main_chat FROM chats WHERE chat_id = ?", (other,))
                if r and _lineage(dict(r)) == mine:
                    bound.discard(other)
    excluded = {r["source_chat"] for r in _q(
        "SELECT source_chat FROM exclusions WHERE character_id = ? AND target_chat = ?", (cid, chat_id))}
    out = [{"chat_id": c, "how": "link"} for c in sorted(links - excluded - {chat_id})]
    out += [{"chat_id": c, "how": "binding"} for c in sorted(bound - links - excluded - {chat_id})]
    return out


def _date(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def _attr(v: str) -> str:
    return str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


class TurnMemory:
    """Memory for one character's turn in one chat."""

    def __init__(self, cid, pov, name, chat_id, chat_row, persona, persona_name, is_group, first_in_context, srcs):
        """pov: the speaking card's avatar, name: its name."""
        self.cid, self.pov, self.name, self.chat_id, self.chat_row = cid, pov, name, chat_id, chat_row
        self.persona, self.persona_name, self.is_group = persona, persona_name, is_group
        self.first_in_context = first_in_context
        self.sources = srcs
        # Ids of pieces (from the last pieces() call) with parts not yet
        # recapped. A session that has an earlier version keeps it until
        # they are: a chat that grew loses its last part's recap meanwhile.
        self.incomplete: set = set()

    def pieces(self, floor: Optional[int]) -> list[tuple[str, str, str]]:
        """[(piece id, title, text)]: the recap of this chat before `floor`
        (the first message the CLI session has), then the other chats'
        recaps. Missing recaps are scheduled; what exists is used."""
        out = []
        self.incomplete = set()
        cf = read_chat(self.chat_row["rel"]) if self.chat_row else None
        # Parts of this chat aren't repeated from a chat it shares them with
        # (a connected parent of a branch).
        own_keys = {s["key"] for s in _segments(cf, self.pov)} if cf else set()
        if cf and floor and floor > 0:
            segs = _segments(cf, self.pov, floor)
            have = _segment_recaps([s["key"] for s in segs])
            if any(s["key"] not in have for s in segs):
                schedule_recap(self.cid, self.chat_id, floor, self.chat_row.get("title") or "")
                self.incomplete.add("inchat")
            texts = [have[s["key"]]["recap"] for s in segs if s["key"] in have]
            own_keys |= {s["key"] for s in segs}
            if texts:
                out.append(("inchat", "earlier in this chat",
                            '<memory source="earlier in this chat">\nWhat happened in this chat before the '
                            'part of the transcript shown here.\n\n' + "\n\n".join(texts) + "\n</memory>"))
        # Other chats, newest first while allocating the budget, oldest first in the text.
        rows = []
        for s in self.sources:
            row = get_chat(s["chat_id"], rescan_for=self.cid)
            src = read_chat(row["rel"]) if row else None
            if src:
                rows.append((row, src))
        rows.sort(key=lambda rs: rs[0].get("last_ts") or 0, reverse=True)
        budget, used_keys, chosen, left_out = SOURCE_BUDGET, set(own_keys), [], 0
        for row, src in rows:
            full, complete, keys = _full_recap(_pov(self.cid, row)[0], src, used_keys)
            if not complete:
                schedule_recap(self.cid, row["chat_id"], None, row.get("title") or "")
                self.incomplete.add(f"chat:{row['chat_id']}")
            enc = _encounter(self.cid, row["chat_id"])
            text, short = full, False
            if len(full) > budget and enc.get("short") and len(enc["short"]) <= budget:
                text, short = enc["short"], True
            if not text.strip() or len(text) > budget:
                left_out += 1 if full.strip() else 0
                continue
            budget -= len(text)
            used_keys |= set(keys)
            chosen.append((row, src, text, short))
        for row, src, text, short in sorted(chosen, key=lambda c: c[0].get("first_ts") or 0):
            user, others = _chat_people(src, self.name)
            present = [self.name, user] if not row.get("group_id") else [self.name] + [
                n for n in json.loads(row.get("speakers") or "[]") if n != self.name]
            note = f"Present: {', '.join(present[:12])}."
            if self.is_group:
                note += " The others in this chat know about it only if they were there or were told."
            if short:
                note += " (A short recap: the chat is longer.)"
            title = row.get("title") or row["chat_id"]
            date = _date(row.get("first_ts"))
            out.append((f"chat:{row['chat_id']}", title,
                        f'<memory source="{_attr(title)}"' + (f' date="{date}"' if date else "")
                        + f">\n{note}\n\n{text}\n</memory>"))
        if left_out:
            log(f"{left_out} older chat(s) didn't fit {self.name}'s memory budget", "INFO")
        return out


def render_block(name: str, pieces: list[tuple[str, str, str]]) -> str:
    if not pieces:
        return ""
    intro = (f"{name}'s memories of events that aren't in the transcript: from earlier in this chat, or from "
             f"other chats. They happened. Where the character card's description or scenario says something "
             f"different about {name}'s history or relationships, these memories win.")
    return (f'<character_memory character="{_attr(name)}">\n{intro}\n\n'
            + "\n\n".join(p[2] for p in pieces) + "\n</character_memory>")


def render_update(name: str, changed: list[tuple[str, str, str]], removed_titles: list[str]) -> str:
    if not changed and not removed_titles:
        return ""
    parts = [f"Changes to {name}'s memories. Each <memory> replaces the one with the same source."]
    parts += [p[2] for p in changed]
    parts += [f'<memory source="{_attr(t)}" removed="yes">This memory no longer applies to this chat. '
              f"Disregard it.</memory>" for t in removed_titles]
    return f'<character_memory_update character="{_attr(name)}">\n' + "\n\n".join(parts) + "\n</character_memory_update>"


def piece_hash(piece: tuple[str, str, str]) -> str:
    return _hash(piece[2], 16)


# ---------------------------------------------------------------------------
# The request's identity (bridge_memory field from the extension)
# ---------------------------------------------------------------------------

def parse_request_field(raw) -> Optional[dict]:
    """Validate the `bridge_memory` field; None when it's missing or unusable."""
    if not isinstance(raw, dict):
        return None

    def s(key, limit=300):
        v = raw.get(key)
        return v.strip()[:limit] if isinstance(v, str) else ""

    def n(key):
        v = raw.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None

    info = {
        "token": s("token", 200), "version": s("v", 20) or str(raw.get("v") or ""),
        "chat_id": s("chat_id", 100), "chat_file": s("chat_file", 400), "group_id": s("group_id", 100),
        "main_chat": s("main_chat", 400), "char_avatar": s("char_avatar", 300), "char_name": s("char_name", 200),
        "persona": s("persona", 300), "persona_name": s("persona_name", 200), "type": s("type", 40) or "normal",
        "first_in_context": n("first_in_context"), "chat_length": n("chat_length"),
    }
    if not info["chat_file"] or not info["char_avatar"]:
        return info if info["token"] else None
    return info


def register_token(token: str):
    if token and 16 <= len(token) <= 200:
        _x("INSERT INTO ext_tokens (hash, last_ts) VALUES (?, ?) ON CONFLICT(hash) DO UPDATE SET last_ts = excluded.last_ts",
           (_hash(token, 64), time.time()))
        _x("DELETE FROM ext_tokens WHERE hash NOT IN (SELECT hash FROM ext_tokens ORDER BY last_ts DESC LIMIT 10)")


def token_ok(token: str) -> bool:
    return bool(token) and _q1("SELECT 1 FROM ext_tokens WHERE hash = ?", (_hash(token, 64),)) is not None


def note_request(info: dict):
    """A chat request carried the extension's field: learn its token and
    when it was last heard from (for the Memory tab). Memory on or off."""
    if info.get("token"):
        register_token(info["token"])
    _meta_set("extension", {"version": info.get("version"), "ts": time.time()})


def _register_live_chat(info: dict) -> Optional[dict]:
    """The chats row of the request's chat, refreshed from its file."""
    rel = chat_rel(info["chat_file"], info["group_id"], info["char_avatar"])
    if not rel:
        return None
    group = _group(info["group_id"]) if info["group_id"] else None
    row = scan_chat(rel, group)
    if not row:
        # Not saved yet (a brand-new chat): remember what the request says.
        cid = rel
        _store_chat({"chat_id": cid, "rel": rel, "integrity": info["chat_id"] or None, "group_id": info["group_id"] or None,
                     "group_name": group["name"] if group else None, "main_chat": info["main_chat"] or None,
                     "title": f"{group['name']}: {info['chat_file']}" if group else info["chat_file"],
                     "first_ts": time.time(), "last_ts": time.time(), "n": info["chat_length"] or 0,
                     "personas": "{}", "speakers": "[]", "mtime": 0, "size": 0,
                     "created_ts": time.time(), "last_msg": None})
        row = dict(_q1("SELECT * FROM chats WHERE chat_id = ?", (cid,)))
    _x("UPDATE chats SET live_ts = ? WHERE chat_id = ?", (time.time(), row["chat_id"]))
    return row


def _auto_bind(cid: str, persona: str, row: dict):
    """A new chat with a bound persona joins the binding. Branches don't:
    they're alternate timelines of a chat the user may or may not want."""
    b = binding(cid, persona)
    if not b or row.get("main_chat"):
        return
    if _q1("SELECT 1 FROM binding_chats WHERE character_id = ? AND persona = ? AND chat_id = ?",
           (cid, persona, row["chat_id"])):
        return
    if (row.get("first_ts") or 0) >= b["created_ts"] - 60:
        _x("INSERT OR IGNORE INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'auto', ?)",
           (cid, persona, row["chat_id"], time.time()))
        log(f"{character_name(cid)}: {row.get('title')} joins the binding with {b.get('persona_name') or persona}")


def prepare_turn(info: dict, summarized: bool = False) -> Optional[TurnMemory]:
    """Memory for a roleplay turn, or None when there's none to give.
    `summarized`: the bridge's auto-summary replaced the older messages, so
    the chat's own earlier part isn't recapped here."""
    if info.get("type") in _SKIP_TYPES or not info.get("chat_file") or not info.get("char_avatar"):
        return None
    if not st_dir():
        log("SillyTavern's data folder wasn't found, so memory can't read chats", "WARN")
        return None
    cid = ensure_card(info["char_avatar"], info["char_name"])
    row = _register_live_chat(info)
    if not row:
        return None
    persona = info.get("persona") or ""
    if persona:
        _auto_bind(cid, persona, row)
    srcs = sources(cid, row["chat_id"], persona, row)
    tm = TurnMemory(cid, info["char_avatar"], (info.get("char_name") or "").strip() or character_name(cid),
                    row["chat_id"], row, persona, info.get("persona_name") or "", bool(info.get("group_id")),
                    None if summarized else info.get("first_in_context"), srcs)
    if srcs:
        _maybe_check(tm)
    return tm


# ---------------------------------------------------------------------------
# Conflict checks
# ---------------------------------------------------------------------------

_CHECK_PROMPT = """{char} is a character in a roleplay. {char} remembers earlier chats, recapped below, and a new chat has started{with_whom}. Decide whether the way the new chat starts contradicts those memories about {char}'s relationships or shared history with the people in them.

Contradictions: the new chat treats people who share history in the memories as strangers meeting for the first time; it gives them a different relationship (siblings in one, rivals or lovers in the other); it says something happened, or didn't, against the memories.
Not contradictions: a new place, setting or time; a time skip; a different mood; things the memories don't cover; the character card's own background.

Answer with only a JSON object: {{"conflict": true or false, "explanation": "one or two sentences for the user saying what clashes, naming the chat"}}

=== MEMORIES ===
{memories}

=== HOW THE NEW CHAT STARTS ===
{opening}
"""


def _opening(cf: ChatFile) -> str:
    """How a chat starts: its greeting(s) and the user's first message, and
    its Author's Note. Empty until the user has written: it's checked once
    the chat has its premise, and doesn't change as the chat goes on."""
    msgs = [m for m in cf.messages if not m["hidden"] and m["text"].strip()]
    first_user = next((i for i, m in enumerate(msgs) if m["user"]), None)
    if first_user is None:
        return ""
    msgs = msgs[:min(first_user + 1, OPENING_MESSAGES)]
    text = "\n\n".join(f"{m['name'] or 'Character'}: {_clean(m['text'])[:3000]}" for m in msgs)
    if cf.note:
        text = f"Author's Note for this chat: {cf.note[:2000]}\n\n{text}"
    return text


def _check_sig(tm: TurnMemory, cf: ChatFile) -> tuple[str, list[str]]:
    """What the check depends on: a hash of the chat's opening, and the
    sources that have recaps."""
    ready = []
    for s in tm.sources:
        row = _q1("SELECT rel, group_id FROM chats WHERE chat_id = ?", (s["chat_id"],))
        src = read_chat(row["rel"]) if row else None
        if src and _full_recap(_pov(tm.cid, dict(row))[0], src, set())[0].strip():
            ready.append(s["chat_id"])
    return _hash(_opening(cf)), sorted(ready)


def _maybe_check(tm: TurnMemory):
    """Check the chat's opening against its memories when either changed.
    After the user kept memories or dropped some, only new ones (or a new
    opening) are checked again, so the ones they chose to keep don't keep
    raising the same conflict."""
    cf = read_chat(tm.chat_row["rel"]) if tm.chat_row else None
    if not cf:
        return
    sig, ready = _check_sig(tm, cf)
    if not ready or not _opening(cf):
        return
    row = _q1("SELECT sig, status, sources FROM checks WHERE character_id = ? AND chat_id = ?", (tm.cid, tm.chat_id))
    if row and row["sig"] == sig:
        checked = set(json.loads(row["sources"] or "[]"))
        if row["status"] in ("kept", "applied") and set(ready) <= checked:
            return
        if set(ready) == checked:
            return
    _jobs.submit({"kind": "check", "cid": tm.cid, "chat_id": tm.chat_id, "persona": tm.persona,
                  "title": tm.chat_row.get("title") or "", "key": f"check\0{tm.cid}\0{tm.chat_id}"})


def _run_check(job: dict):
    """Ask Sonnet whether the chat's opening contradicts its memories. Once
    the user has decided (kept or dropped memories), only chats added since
    are checked, and a decision made while the check ran wins."""
    cid, chat_id = job["cid"], job["chat_id"]
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if not cf:
        return
    pov, name = _pov(cid, row)
    tm = TurnMemory(cid, pov, name, chat_id, row, job.get("persona") or "", "",
                    bool(row.get("group_id")), None, sources(cid, chat_id, job.get("persona") or "", row))
    sig, ready = _check_sig(tm, cf)
    prev = _q1("SELECT id, sig, status, sources FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    decided = bool(prev) and prev["status"] in ("kept", "applied") and prev["sig"] == sig
    checked = set(json.loads(prev["sources"] or "[]")) if decided else set()
    to_check = [c for c in ready if c not in checked]
    if not to_check:
        return
    mems = []
    for src_id in to_check:
        srow = get_chat(src_id, rescan_for=cid)
        src = read_chat(srow["rel"]) if srow else None
        if not src:
            continue
        enc = _encounter(cid, src_id)
        text = enc.get("short") or _full_recap(_pov(cid, srow)[0], src, set())[0]
        mems.append(f"--- {srow.get('title') or src_id} ---\n{text[:5000]}")
    user, _ = _chat_people(cf, tm.name)
    reply = _call_sonnet(_CHECK_PROMPT.format(
        char=tm.name, with_whom=f" with {user}" if user != "the user" else "",
        memories="\n\n".join(mems), opening=_opening(cf)),
        timeout=CHECK_TIMEOUT, label=f"Character Memory: conflict check {tm.name} — {row.get('title')}")
    parsed = _extract_json(reply or "")
    if not parsed or not isinstance(parsed.get("conflict"), bool):
        status, explanation = "failed", "The conflict check didn't return an answer."
    elif parsed["conflict"]:
        status, explanation = "open", str(parsed.get("explanation") or "").strip()[:1000]
    else:
        status, explanation = (prev["status"] if decided else "clear"), str(parsed.get("explanation") or "").strip()[:1000]
    with _db_lock:
        now = _q1("SELECT id, status FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
        if (tuple(now) if now else None) != ((prev["id"], prev["status"]) if prev else None):
            return  # the user decided meanwhile; the next turn checks again if needed
        _x("DELETE FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
        _x("INSERT INTO checks (character_id, chat_id, sig, status, explanation, sources, shown, created_ts) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
           (cid, chat_id, sig, status, explanation, json.dumps(sorted(checked | set(ready))),
            json.dumps(to_check), time.time()))
    if status == "open":
        log(f"{tm.name}'s memories conflict with how {row.get('title')} starts: {explanation}", "WARN")


def get_check(cid: str, chat_id: str) -> Optional[dict]:
    row = _q1("SELECT * FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    if not row:
        return None
    out = dict(row)
    # The chats the conflict is about, for the popup and the Memory tab.
    names = _card_names(cid)
    out["sources"] = [_brief(cid, c, names) for c in json.loads(out.pop("shown", None) or out.get("sources") or "[]")]
    return out


def resolve_check(check_id: int, action: str, drop: list[str]) -> tuple[bool, str]:
    """keep: the memories stay. drop: the listed chats' memories leave this
    chat (a connection is removed; a binding's chat is excluded here)."""
    row = _q1("SELECT * FROM checks WHERE id = ?", (int(check_id),))
    if not row:
        return False, "no such check"
    cid, chat_id = row["character_id"], row["chat_id"]
    if action == "drop":
        for src in drop:
            if not isinstance(src, str):
                continue
            if _x("DELETE FROM links WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, chat_id, src)):
                continue
            set_exclusion(cid, chat_id, src, True)
    _x("UPDATE checks SET status = ? WHERE id = ?", ("applied" if action == "drop" else "kept", int(check_id)))
    return True, ""


# ---------------------------------------------------------------------------
# Changes from the Memory tab and the extension
# ---------------------------------------------------------------------------

def _chat_title(chat_id: str) -> str:
    row = _q1("SELECT title FROM chats WHERE chat_id = ?", (chat_id,))
    return row["title"] if row and row["title"] else chat_id


def bind(cid: str, persona: str) -> dict:
    _x("INSERT OR IGNORE INTO bindings (character_id, persona, persona_name, created_ts) VALUES (?, ?, ?, ?)",
       (cid, persona, _persona_name(persona), time.time()))
    return binding(cid, persona)


def unbind(cid: str, persona: str):
    with _db_lock:
        _x("DELETE FROM bindings WHERE character_id = ? AND persona = ?", (cid, persona))
        _x("DELETE FROM binding_chats WHERE character_id = ? AND persona = ?", (cid, persona))


def set_binding_chats(cid: str, persona: str, add: list[str], remove: list[str], build: bool = True) -> int:
    """Pick chats for a binding (and recap them), or take them out."""
    if not binding(cid, persona):
        return 0
    now, n = time.time(), 0
    for c in add:
        n += _x("INSERT INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'chosen', ?) "
                "ON CONFLICT(character_id, persona, chat_id) DO UPDATE SET how = 'chosen'", (cid, persona, c, now))
        if build:
            schedule_recap(cid, c, None, _chat_title(c))
    for c in remove:
        n += _x("INSERT INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'removed', ?) "
                "ON CONFLICT(character_id, persona, chat_id) DO UPDATE SET how = 'removed'", (cid, persona, c, now))
    return n


def set_link(cid: str, target: str, source: str, on: bool, build: bool = True):
    if target == source:
        return
    if on:
        _x("INSERT OR IGNORE INTO links (character_id, target_chat, source_chat, created_ts) VALUES (?, ?, ?, ?)",
           (cid, target, source, time.time()))
        _x("DELETE FROM exclusions WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, target, source))
        if build:
            schedule_recap(cid, source, None, _chat_title(source))
    else:
        _x("DELETE FROM links WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, target, source))


def set_exclusion(cid: str, target: str, source: str, on: bool):
    if on:
        _x("INSERT OR IGNORE INTO exclusions (character_id, target_chat, source_chat, created_ts) VALUES (?, ?, ?, ?)",
           (cid, target, source, time.time()))
    else:
        _x("DELETE FROM exclusions WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, target, source))


def set_excluded(cid: str, chat_id: str, on: bool):
    """Leave a chat out of every binding (connections still bring it in)."""
    _ensure_encounter(cid, chat_id)
    _x("UPDATE encounters SET excluded = ? WHERE character_id = ? AND chat_id = ?", (1 if on else 0, cid, chat_id))


# ---------------------------------------------------------------------------
# Views for the Memory tab and the extension
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    """A name compared with a file name: letters and digits, as SillyTavern
    drops characters like | and : from file names."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def _card_names(cid: str) -> dict:
    """Chat folder -> card name, for the character's cards."""
    return {_stem(c["avatar"]): c["name"] for c in character_cards(cid)}


def _chat_view(row: dict, cid: str, recaps: bool = True, names: Optional[dict] = None) -> dict:
    """A chat for the Memory tab. name: its card's or group's name; label:
    its branch ("Branch #3") or its own title (a checkpoint's name, a renamed
    file). A chat's file name otherwise starts with the card's name when the
    chat began, which may be an old one. created: the day the file was made,
    as its name says it in the user's time, or else created_ts (the
    dashboard shows it in the browser's time zone); parent: the chat id of
    the chat it's a branch of."""
    personas = json.loads(row.get("personas") or "{}")
    names = _card_names(cid) if names is None else names
    folder = os.path.basename(os.path.dirname(row["rel"]))
    name = (row.get("group_name") or "Group") if row["rel"].startswith("group chats/") else names.get(folder, folder)
    main = row.get("main_chat") or None
    prefix, branch, when, own = _stem_parts(os.path.splitext(os.path.basename(row["rel"]))[0])
    known = {_norm_name(n) for n in (name, folder, *names, *names.values())}
    own_title = (main or when is None) and _norm_name(prefix) not in known
    created_ts = None if own else (row.get("created_ts") or row.get("first_ts"))
    out = {
        "chat_id": row["chat_id"], "title": row.get("title") or row["chat_id"], "rel": row["rel"],
        "name": name, "label": branch or (prefix if own_title else ""),
        "group": row.get("group_name") or None, "branch_of": main,
        "parent": f"{os.path.dirname(row['rel'])}/{main}.jsonl" if main else None,
        "created": when.strftime("%Y-%m-%d") if own else _date(created_ts), "created_ts": created_ts,
        "last_msg": json.loads(row.get("last_msg") or "{}"),
        "first": _date(row.get("first_ts")), "last": _date(row.get("last_ts")),
        "last_ts": row.get("last_ts") or 0, "messages": row.get("n") or 0,
        "personas": [{"avatar": a, "name": p.get("name") or "", "messages": p.get("n", 0)}
                     for a, p in sorted(personas.items(), key=lambda kv: -kv[1].get("n", 0))],
    }
    if recaps:
        st = recap_state(cid, row["chat_id"], row=row)
        enc = _encounter(cid, row["chat_id"])
        job = _jobs.pending(cid, row["chat_id"])
        out.update(recap=st, excluded=bool(enc.get("excluded")), error=enc.get("error"),
                   building={"done": job.get("done"), "total": job.get("total")} if job else None)
    return out


_BRIEF_KEYS = ("chat_id", "title", "name", "label", "created", "created_ts", "last_msg", "group", "branch_of")


def _brief(cid: str, chat_id: str, names: Optional[dict] = None) -> dict:
    """A chat as a list names it (see _chat_view), from what's stored."""
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    if not row:
        return {"chat_id": chat_id, "title": chat_id}
    view = _chat_view(dict(row), cid, recaps=False, names=names)
    return {k: view[k] for k in _BRIEF_KEYS}


def _recap_parts(cid: str, row: dict) -> list[dict]:
    """A character's recap of a chat, part by part: key, from, to (message
    numbers), recap (None until recapped), edited."""
    cf = read_chat(row["rel"])
    if not cf:
        return []
    segs = _segments(cf, _pov(cid, row)[0])
    have = _segment_recaps([s["key"] for s in segs])
    return [{"key": s["key"], "from": s["start"], "to": s["end"] - 1,
             "recap": (have.get(s["key"]) or {}).get("recap"), "edited": bool((have.get(s["key"]) or {}).get("edited"))}
            for s in segs]


def recap_view(cid: str, chat_id: str) -> dict:
    """A character's recap of a chat, to read where another chat uses it."""
    row = get_chat(chat_id, rescan_for=cid)
    if not row:
        return {}
    enc = _encounter(cid, chat_id)
    return {**_chat_view(row, cid, recaps=False), "parts": _recap_parts(cid, row),
            "short": enc.get("short"), "short_edited": bool(enc.get("short_edited"))}


def overview() -> dict:
    """Characters with any memory setup, the extension's last contact, jobs."""
    chars = []
    for r in _q("SELECT id, name FROM characters ORDER BY name COLLATE NOCASE"):
        cid = r["id"]
        n_b = _q1("SELECT COUNT(*) AS n FROM bindings WHERE character_id = ?", (cid,))["n"]
        n_l = _q1("SELECT COUNT(*) AS n FROM links WHERE character_id = ?", (cid,))["n"]
        cards = [c["avatar"] for c in character_cards(cid)]
        n_s = _q1(f"SELECT COUNT(*) AS n FROM segments WHERE pov IN ({','.join('?' * len(cards))})",
                  cards)["n"] if cards else 0
        n_o = _q1("SELECT COUNT(*) AS n FROM checks WHERE character_id = ? AND status = 'open'", (cid,))["n"]
        chars.append({"id": cid, "name": r["name"], "cards": cards,
                      "bindings": n_b, "links": n_l, "recaps": n_s, "alerts": n_o})
    return {"characters": chars, "extension": _meta_get("extension"), "jobs": jobs_status(),
            "st_found": bool(st_dir())}


def character_view(cid: str) -> dict:
    """Everything the Memory tab shows for a character."""
    if not _q1("SELECT 1 FROM characters WHERE id = ?", (cid,)):
        card = next((c for c in list_cards() if c["avatar"] == cid), None)
        if not card:
            return {}
        cid = ensure_card(card["avatar"], card["name"])
    chats = character_chats(cid)
    chats.sort(key=lambda r: r.get("last_ts") or 0, reverse=True)
    names = _card_names(cid)
    views = [_chat_view(r, cid, names=names) for r in chats]
    by_id = {v["chat_id"]: v for v in views}
    bindings = []
    for b in _q("SELECT * FROM bindings WHERE character_id = ? ORDER BY created_ts", (cid,)):
        members = {r["chat_id"]: r["how"] for r in _q(
            "SELECT chat_id, how FROM binding_chats WHERE character_id = ? AND persona = ?", (cid, b["persona"]))}
        bindings.append({"persona": b["persona"], "persona_name": b["persona_name"] or _persona_name(b["persona"]),
                         "created": _date(b["created_ts"]), "chats": members})
    links = [dict(r) for r in _q("SELECT target_chat, source_chat FROM links WHERE character_id = ?", (cid,))]
    exclusions = [dict(r) for r in _q("SELECT target_chat, source_chat FROM exclusions WHERE character_id = ?", (cid,))]
    checks = {r["chat_id"]: get_check(cid, r["chat_id"]) for r in _q(
        "SELECT chat_id FROM checks WHERE character_id = ? AND status IN ('open', 'failed')", (cid,))}
    for c in by_id.values():
        c["check"] = checks.get(c["chat_id"])
    return {"id": cid, "name": character_name(cid), "cards": character_cards(cid), "bindings": bindings,
            "links": links, "exclusions": exclusions, "chats": views}


def chat_detail(cid: str, chat_id: str) -> dict:
    """One chat for a character: its recap, part by part, and the memories
    it uses."""
    row = get_chat(chat_id, rescan_for=cid)
    if not row:
        return {}
    view = _chat_view(row, cid)
    parts = _recap_parts(cid, row)
    enc = _encounter(cid, chat_id)
    full = "\n\n".join(p["recap"] for p in parts if p["recap"])
    short_stale = bool(enc.get("short_edited") and enc.get("short_key") and enc["short_key"] != _hash(full))
    personas = [p["avatar"] for p in view["personas"] if p["avatar"]]
    used = []
    for persona in personas or [""]:
        for s in sources(cid, chat_id, persona, row):
            if all(u["chat_id"] != s["chat_id"] for u in used):
                used.append({**s, "title": _chat_title(s["chat_id"]),
                             "persona": persona if s["how"] == "binding" else None})
    excluded = [{"chat_id": r["source_chat"], "title": _chat_title(r["source_chat"])} for r in _q(
        "SELECT source_chat FROM exclusions WHERE character_id = ? AND target_chat = ?", (cid, chat_id))]
    used_in = [{"chat_id": r["target_chat"], "title": _chat_title(r["target_chat"])} for r in _q(
        "SELECT target_chat FROM links WHERE character_id = ? AND source_chat = ?", (cid, chat_id))]
    in_bindings = [{"persona": r["persona"], "how": r["how"]} for r in _q(
        "SELECT persona, how FROM binding_chats WHERE character_id = ? AND chat_id = ?", (cid, chat_id))]
    return {**view, "parts": parts, "short": enc.get("short"), "short_edited": bool(enc.get("short_edited")),
            "short_stale": short_stale, "sources": used, "excluded_sources": excluded, "used_in": used_in,
            "in_bindings": in_bindings, "check": get_check(cid, chat_id)}


def extension_status(info: dict, recaps: bool = False) -> dict:
    """For the extension, when a chat is opened or a reply arrives: per
    character, the memories this chat uses and any open conflict; with
    `recaps`, their recap texts too. Starts missing recaps and the conflict
    check, so they're ready for the first reply."""
    out = []
    if not st_dir():
        return {"characters": out, "st_found": False}
    group = _group(info["group_id"]) if info.get("group_id") else None
    cards = list(group["members"]) if group else ([info["char_avatar"]] if info.get("char_avatar") else [])
    names = {c["avatar"]: c["name"] for c in list_cards()} if group else {info["char_avatar"]: info.get("char_name")}
    persona = info.get("persona") or ""
    for avatar in cards:
        rel = chat_rel(info["chat_file"], info.get("group_id") or "", avatar)
        if not rel:
            continue
        row = scan_chat(rel, group)
        if not row:
            continue
        cid = ensure_card(avatar, names.get(avatar) or "")
        if persona:
            _auto_bind(cid, persona, row)
        srcs = sources(cid, row["chat_id"], persona, row)
        tm = TurnMemory(cid, avatar, names.get(avatar) or character_name(cid), row["chat_id"], row, persona,
                        info.get("persona_name") or "", bool(group), None, srcs)
        items = []
        card_names = _card_names(cid)
        for s in srcs:
            srow = get_chat(s["chat_id"], rescan_for=cid)
            if not srow:
                continue
            st = recap_state(cid, s["chat_id"], row=srow)
            if st["missing"]:
                schedule_recap(cid, s["chat_id"], None, srow.get("title") or "")
            view = _chat_view(srow, cid, recaps=False, names=card_names)
            item = {**{k: view[k] for k in _BRIEF_KEYS}, "how": s["how"], "recap": st}
            if recaps:
                item.update(parts=[{k: p[k] for k in ("from", "to", "recap")} for p in _recap_parts(cid, srow)],
                            short=_encounter(cid, s["chat_id"]).get("short"))
            items.append(item)
        if srcs:
            _maybe_check(tm)
        check = get_check(cid, row["chat_id"])
        out.append({"avatar": avatar, "character_id": cid, "name": tm.name,
                    "bound": bool(binding(cid, persona)), "sources": items,
                    "check": check if check and check["status"] == "open" else None})
        if recaps:
            out[-1]["own"] = {"parts": [{k: p[k] for k in ("from", "to", "recap")} for p in _recap_parts(cid, row)],
                              "short": _encounter(cid, row["chat_id"]).get("short")}
    return {"characters": out, "st_found": True}
